"""The daemon owns the controller. It turns agent events into light/rumble/trigger feedback and
forwards controller input to the focused `padpilot run` wrapper as keystrokes, or (macOS, no
wrapper attached) to an allowed frontmost desktop app as real key presses. A question box open in
the desktop app in front takes its own buttons even while a wrapper is attached.

Protocol: newline-delimited JSON over a Unix socket (see client.py).
"""
from __future__ import annotations

import colorsys
import fcntl
import math
import json
import logging
import logging.handlers
import os
import queue
import re
import select
import signal
import socket
import statistics
import struct
import sys
import threading
import time
from typing import Callable, Dict, List, Optional

from . import __version__
from . import client
from . import config as C
from . import macos
from . import transcript as T
from .bindings import Resolver
from .controls import WHEEL_BUTTON, ControlTracker
from .sound import DEVICE, Speaker
from .dualsense import PLAYER_PATTERNS, DualSense, OutputState, make_trigger, parse_input
from .effects import Flash, RumblePlayer, light_at, mix, scale
from .hook import QUESTION_TOOL, is_choice_tool
from .keys import encode_key, split_action, to_items

log = logging.getLogger("padpilot")

FRAME_S = 1 / 30.0
IDLE_FRAME_S = 1 / 15.0
STALE_SESSION_S = 30 * 60   # hook-only session without events
STALE_KEPT_S = 12 * 3600    # ... when its turn is finished and it waits for you (green light, card)
STUCK_BUSY_S = 10 * 60      # 'thinking' with no event at all for this long -> idle
BUSY = ("thinking", "tool", "compacting", "permission")
# PS combos that change a saved setting; practice mode only shows them.
SETTING_ACTIONS = ("@brightness_up", "@brightness_down", "@rumble_up", "@rumble_down", "@sounds", "@idle_light")

# Desktop keys that only do their job in the message box: typing, sending, clearing, undo, and
# Esc, which interrupts from the message box but not from a focused button (tested). Menus and
# dialogs keep focus, so Esc still closes them.
BOX_KEYS = ("enter", "shift+enter", "cmd+enter", "cmd+a", "backspace", "cmd+z", "esc")


# (where, kind of exception) -> [when it was last logged, how often it happened since]
_errors: Dict[tuple, list] = {}
ERROR_EVERY_S = 60.0


def log_error(where: str) -> None:
    """Log the exception being handled with its traceback, but the same kind in the same place
    only once a minute; the others are counted. A fault that strikes on every input report or
    every frame (a wrong value in config.json) would write hundreds of tracebacks a second."""
    kind = sys.exc_info()[0]
    key = (where, kind.__name__ if kind else "")
    now = time.monotonic()
    seen = _errors.get(key)
    if seen is not None and now - seen[0] < ERROR_EVERY_S:
        seen[1] += 1
        return
    if len(_errors) > 200:
        _errors.clear()
    more = f" ({seen[1]} more like it since the last report)" if seen and seen[1] else ""
    _errors[key] = [now, 0]
    log.exception("error in %s%s", where, more)


def needs_message_box(items: List[dict]) -> bool:
    return any("text" in i or i.get("key") in BOX_KEYS for i in items)


def _describe(item: dict) -> str:
    """A short form of a desktop key item for the log. Typed text is logged by length only, so
    no prompt or reply text ends up in the log."""
    if "text" in item:
        return f"text({len(str(item['text']))} chars)"
    if "question" in item:
        return "question box: " + ("submit" if item["question"] is None else f"option {item['question']}")
    return str(item.get("key", item))

class Session:
    def __init__(self, key: str, agent: str, label: str):
        self.key = key
        self.agent = agent
        self.label = label
        self.state = "idle"
        self.prev_state = "idle"
        self.since = time.monotonic()
        self.updated = self.since
        self.rumbles = 0
        self.last_rumble = 0.0
        self.last_event = ""
        self.tool: Optional[str] = None   # name of the running tool
        self.tools = 0                    # tool calls in the current turn
        self.turn_start = 0.0
        self.last_prompt: Optional[str] = None
        # Buttons of the last next-step card: (label, is_app_action), in screen order.
        self.choices: List[tuple] = []
        self.choices_at = 0.0
        # An open question box (AskUserQuestion): [{"options": [label], "multiSelect": bool}].
        self.questions: List[dict] = []
        self.questions_at = 0.0
        self.reminded = False
        self.transcript: Optional[str] = None
        # Where it runs, as its hooks say: the entry point ("claude-desktop", "cli", ...; "" if
        # they do not say) and whether someone attends it (None if they do not say).
        self.entry = ""
        self.attended: Optional[bool] = None
        self.context: Optional[float] = None  # share of the context window the last request used

    def replies(self) -> int:
        return sum(1 for label, app in self.choices if label and not app)


class Target:
    """A connected `padpilot run` wrapper that can receive keystrokes."""
    practice_mode = False  # a `padpilot watch --practice` or the tutorial page

    def _encode(self, msg: Optional[dict]) -> bytes:
        return (json.dumps(msg) + "\n").encode()

    def __init__(self, tid: str, agent: str, label: str, conn: socket.socket):
        self.id = tid
        self.agent = agent
        self.label = label
        self.conn = conn
        self.send_lock = threading.Lock()

    def send(self, msg: dict, timeout: float = 0.5) -> bool:
        """Send one line. A peer that stops reading (a paused terminal, a suspended `watch`)
        must not block the daemon: after `timeout` the connection is dropped instead, and the
        wrapper reconnects later."""
        data = memoryview(self._encode(msg))
        deadline = time.monotonic() + timeout
        if not hasattr(self.conn, "fileno"):  # a test double
            try:
                self.conn.sendall(bytes(data))
                return True
            except OSError:
                return False
        try:
            with self.send_lock:
                while data:
                    left = deadline - time.monotonic()
                    if left <= 0:
                        raise socket.timeout("peer not reading")
                    _, w, _ = select.select([], [self.conn], [], left)
                    if w:
                        # Writable means at least SO_SNDLOWAT (2048) bytes are free; a larger
                        # send can still block (MSG_DONTWAIT does not help on macOS sockets).
                        data = data[self.conn.send(data[:1024]):]
            return True
        except (OSError, ValueError):
            try:
                self.conn.shutdown(socket.SHUT_RDWR)  # half a line may be out: end the link
            except OSError:
                pass
            return False


class Approval:
    def __init__(self, session_key: str, tool: str):
        self.session_key = session_key
        self.tool = tool
        self.event = threading.Event()
        self.decision: Optional[str] = None
        self.created = time.monotonic()


class Daemon:
    def __init__(self, cfg: dict, hid_module=None, desktop=None, mic_probe=None, resolver=None,
                 settings_path=None, speak=None, desktop_focus=None, desktop_question=None,
                 keyboard_idle=None):
        self.cfg = cfg
        # Spoken cues on the controller speaker; tests pass a fake (then USB is not required).
        self.speak_any = speak is not None
        self.speak = speak or Speaker().play
        self.speaker_until = 0.0  # the speaker is switched on until then (a cue is playing)
        self.after_send: List[Callable[[], None]] = []  # run once a button's keys went out
        self.settings_path = settings_path or C.CONFIG_PATH  # where PS-layer settings are saved
        self.hid = hid_module
        self.resolver = resolver or Resolver(encodable=lambda k: _can(encode_key, k),
                                             desktop_encodable=lambda k: _can(macos.key_event, k))
        # Desktop key injection: (frontmost_bundle_id, trusted, post_items); tests pass fakes.
        self.desktop = desktop or (macos.frontmost_bundle_id, macos.accessibility_trusted, macos.post_items)
        self.desktop_fake = desktop is not None
        # Answers the question box (bundle id, questions, option or None) -> what happened.
        self.desktop_question = desktop_question or (None if self.desktop_fake else macos.answer_question)
        # Moves focus to the app's message box (bundle id -> what happened); tests pass a fake.
        self.desktop_focus = desktop_focus or (None if self.desktop_fake else macos.focus_message_box)
        self.desktop_focus_note = ""  # last result, for the log only when it changes
        # Seconds since keyboard or mouse were last used (None: unknown); tests pass a fake.
        self.keyboard_idle = keyboard_idle or ((lambda: None) if self.desktop_fake else macos.keyboard_idle_s)
        self.last_input = -1e12  # when the real controller was last pressed
        self.desktop_q: Optional[queue.Queue] = None
        self.desktop_warned = False
        self.lock = threading.RLock()
        self.sessions: Dict[str, Session] = {}
        self.targets: List[Target] = []
        self.focus: Optional[str] = None
        self.approvals: List[Approval] = []
        self.flashes: List[Flash] = []
        self.rumble = RumblePlayer()
        self.locked = False
        self.overlay_until = 0.0
        self.overlay = None  # ("battery", ...)
        self.dev: Optional[DualSense] = None
        self.input_state = None
        self.battery = None
        self.charging = None
        self.last_activity = time.monotonic()
        self.running = True
        self.demo_until = 0.0
        self.tracker = ControlTracker(cfg, self._has_hold)
        self.watchers: List[Target] = []
        self.imu_samples: Optional[List[int]] = None  # debug: accelerometer capture
        self.mic_probe = mic_probe or macos.mic_in_use
        self.listening = False
        self.listen_since = 0.0
        self.last_mic_poll = -1e9
        self.last_low_battery = -1e9
        self.retry_armed_until = 0.0
        self.practice: Optional[Target] = None  # `padpilot watch --practice`: report, don't send
        self.preview: Optional[tuple] = None    # (light spec, since): the tutorial page shows a state
        self.ptt_sent: Dict[str, bool] = {}  # push-to-talk control -> its press was delivered
        self.started = time.monotonic()
        self.started_at = time.time()
        self.code = ""  # names the code this process runs (client.code_id); set by run_daemon
        self.question_submitted_at = 0.0  # when a question-box page was last submitted
        # `padpilot run` sessions: the wrapped agent's own session id, and the agents started
        # inside it as (wrapper id, session id). See _nested_event.
        self.wrapper_owner: Dict[str, str] = {}
        self.nested: set = set()

    # ------------------------------------------------------------ helpers
    def _mapping_for(self, target: Optional[Target]) -> dict:
        m = dict(self.cfg["mapping"])
        if target is not None:
            m.update(self.cfg.get("profiles", {}).get(target.agent, {}))
        elif self._desktop_enabled():
            m.update(self.cfg.get("profiles", {}).get("desktop", {}))
        return m

    def _desktop_enabled(self) -> bool:
        return bool((self.cfg.get("desktop") or {}).get("enabled")) and (macos.IS_MAC or self.desktop_fake)

    def _desktop_send(self, items: List[dict]) -> bool:
        """Post items to the frontmost app if it is an allowed one and we may post events."""
        front, trusted, _ = self.desktop
        if not self._desktop_enabled():
            return False
        app = front()
        if app not in (self.cfg.get("desktop") or {}).get("apps", []):
            log.info("desktop keys not sent: frontmost app is %s", app)
            return False
        if not trusted():
            if not self.desktop_warned:
                log.warning("desktop keys: no Accessibility permission (run `padpilot desktop`)")
                self.desktop_warned = True
            return False
        if self.desktop_q is None:
            self.desktop_q = queue.Queue()
            threading.Thread(target=self._desktop_worker, daemon=True).start()
        log.info("desktop -> %s", " ".join(_describe(i) for i in items))
        self.desktop_q.put((items, self.cfg.get("sequence_gap_ms", 90), app))
        return True

    def _desktop_worker(self) -> None:
        front, _, post = self.desktop
        while True:
            items, gap, app = self.desktop_q.get()
            # Typing can take seconds (or wait behind other presses): stop if the user switched
            # to another app meanwhile, instead of typing into it.
            try:
                if items and "question" in items[0]:
                    q = items[0]
                    if self.desktop_question is None:
                        continue
                    if q.get("at", 0.0) < self.question_submitted_at:
                        # Pressed while the page before was still being submitted (that takes
                        # about a second): it was aimed at that page, not the one on screen now.
                        log.info("question box: press dropped, made before the last page was submitted")
                        continue
                    res = self.desktop_question(app, q["questions"], q["question"])
                    log.info("question box: %s", res)
                    if res.endswith("submitted"):
                        self.question_submitted_at = time.monotonic()
                    elif res == "question box not found" and q.get("name"):
                        # No box on screen: it went away without an event saying so, or it is
                        # in a session that is not the one on screen. The press must not be
                        # lost; it does what it does when there is no question box.
                        with self.lock:
                            self._press_without_box(q["name"], bool(q.get("injected")))
                    elif res == "another question box is on screen":
                        with self.lock:
                            self._flash("denied")
                    continue
                if self.desktop_focus is not None and needs_message_box(items):
                    note = self.desktop_focus(app)
                    if not note.startswith("kept") or note != self.desktop_focus_note:
                        log.info("message box: %s", note)
                    self.desktop_focus_note = note
                apps = (self.cfg.get("desktop") or {}).get("apps", [])
                # The real post sends to this app's process alone and looks again while it types
                # (macos.post_items); a test double takes just the items and the gap.
                more = {} if self.desktop_fake else {"bundle_id": app, "ok": lambda: front() in apps}
                for i in range(0, len(items), 8):
                    if i:
                        time.sleep(gap / 1000.0)
                    if front() not in apps or post(items[i:i + 8], gap, **more) is False:
                        log.info("desktop keys stopped: another app came to the front")
                        break
            except Exception:
                log.exception("desktop keys failed")

    def _desktop_sessions(self) -> List[Session]:
        """The sessions a key press in the desktop app can belong to. Not one in a `padpilot run`
        terminal, not one that says it runs elsewhere (a terminal, an editor), and not one nobody
        attends. A session that does not say where it runs (an older Claude Code) counts."""
        wrapped = {t.id for t in self.targets}
        return [s for s in self.sessions.values()
                if s.key not in wrapped and s.attended is not False and s.entry in ("", "claude-desktop")]

    def _desktop_session(self) -> Optional[str]:
        """The session a desktop key press most likely belongs to: the latest of those."""
        cands = self._desktop_sessions()
        return max(cands, key=lambda s: s.updated).key if cands else None

    def _has_hold(self, name: str) -> bool:
        v = self._mapping_for(self._focused()).get(name + "_hold")
        return bool(v) and v != "@none"

    def _focused(self) -> Optional[Target]:
        for t in self.targets:
            if t.id == self.focus:
                return t
        return None

    def _session(self, key: str, agent: str = "", label: str = "") -> Session:
        s = self.sessions.get(key)
        if s is None:
            s = Session(key, agent, label)
            self.sessions[key] = s
        if agent:
            s.agent = agent
        if label:
            s.label = label
        return s

    def _play(self, name: Optional[str]) -> None:
        if not name:
            return
        pat = self.cfg["rumble_patterns"].get(name)
        if pat:
            self.rumble.play(pat, time.monotonic(), float(self.cfg.get("rumble_scale", 1.0)))

    def _notify(self, msg: dict) -> None:
        self.watchers = [w for w in self.watchers if w.send(msg)]

    # Watchers (call with the lock held). Practice mode lasts while any practice watcher is left.
    def add_watcher(self, w: Target, practice: bool = False) -> None:
        self.watchers.append(w)
        if practice:
            w.practice_mode = True
            if self.practice is None:
                log.info("practice mode on")
            self.practice = w
            for a in self.approvals:  # practice answers no real request: the screen takes them
                a.event.set()

    def remove_watcher(self, w: Target) -> None:
        self.watchers = [x for x in self.watchers if x is not w]
        if self.practice is w:
            left = [x for x in self.watchers if x.practice_mode]
            self.practice = left[-1] if left else None
            if self.practice is None:
                self.preview = None
                log.info("practice mode off")

    def web_rumble(self, pattern: str) -> bool:
        with self.lock:
            if pattern not in self.cfg["rumble_patterns"]:
                return False
            self._play(pattern)
            return True

    def web_light(self, state: str) -> bool:
        """Show a state's light while practicing ("" or "off" goes back to the real state)."""
        with self.lock:
            if state in ("", "off"):
                self.preview = None
                return True
            if self.practice is None:
                return False
            spec = (self.cfg.get("listening") if state == "listening" else self.cfg["states"].get(state)) or {}
            if "light" not in spec:
                return False
            self.preview = (spec["light"], time.monotonic())
            return True

    def _deny(self, name: str, reason: str) -> bool:
        log.info("%s: %s", name, reason)
        self._flash("denied")
        self._notify({"op": "denied", "name": name, "reason": reason})
        return False

    def _flash(self, name: str, color=None, rumble=None) -> None:
        f = self.cfg["flashes"].get(name)
        if not f:
            return
        self.flashes.append(Flash(color or f.get("color", [255, 255, 255]), f.get("ms", 200), time.monotonic(),
                                  f.get("kind", "solid")))
        self._play(rumble or f.get("rumble"))

    def _tool_kind(self, tool: Optional[str]) -> Optional[dict]:
        for entry in self.cfg.get("tool_kinds") or []:
            try:
                pattern, kind = entry
                if tool and isinstance(kind, dict) and re.search(pattern, tool):
                    return kind
            except (re.error, TypeError, ValueError):  # an entry of another shape: skip it
                continue
        return None

    def set_state(self, s: Session, state: str, now: Optional[float] = None) -> None:
        now = time.monotonic() if now is None else now
        s.updated = now
        if s.state == state:
            return
        if state not in self.cfg["states"]:
            return
        s.prev_state = s.state
        s.state = state
        s.since = now
        s.rumbles = 0
        s.reminded = False
        spec = self.cfg["states"][state]
        if spec.get("rumble"):
            self._play(spec["rumble"])
            s.rumbles = 1
            s.last_rumble = now
        log.info("session %s -> %s", s.key[:24], state)
        self._cue(state)

    def _cue(self, name: str) -> None:
        """Play the cue for `name` on the controller speaker, if sounds are on and it is on USB."""
        snd = self.cfg.get("sounds") or {}
        sound = (snd.get("cues") or {}).get(name)
        if not snd.get("enabled") or not sound:
            return
        if not self.speak_any and (self.dev is None or getattr(self.dev, "bluetooth", True)):
            return
        self.speaker_until = time.monotonic() + float(snd.get("hold_s", 2.5))
        self.speak(sound, device=snd.get("device") or DEVICE, channel=int(snd.get("channel", 1)),
                   channels=int(snd.get("channels", 4)), gain=float(snd.get("gain", 1.0)),
                   tail_ms=int(snd.get("tail_ms", 4000)))

    # ------------------------------------------------------------ events
    def handle_event(self, msg: dict) -> None:
        ev = msg.get("event", "")
        data = msg.get("data") or {}
        source = msg.get("source", "agent")
        key = msg.get("wrapper") or f"{source}:{msg.get('session') or 'default'}"
        label = os.path.basename(msg.get("cwd") or "") or source
        with self.lock:
            self.last_activity = time.monotonic()
            if msg.get("wrapper") and self._nested_event(msg["wrapper"], msg.get("session") or "", ev, data):
                return  # an agent started inside a `padpilot run` session: not that session's event
            if ev == "SessionEnd":
                self.sessions.pop(key, None)
                return
            s = self._session(key, source, label)
            s.last_event = ev
            if isinstance(msg.get("entry"), str) and msg["entry"]:
                s.entry = msg["entry"]
            if isinstance(msg.get("attended"), bool):
                s.attended = msg["attended"]
            if isinstance(data.get("transcript_path"), str):
                s.transcript = data["transcript_path"]
            if ev == "SessionStart":
                # After compacting, the turn goes on: keep the state.
                if s.state not in ("idle", "done") and data.get("source") != "compact":
                    self.set_state(s, "idle")
                if data.get("source") in (None, "startup"):
                    self._play("hello")
            elif ev == "UserPromptSubmit":
                s.tools, s.tool, s.turn_start = 0, None, time.monotonic()
                s.choices, s.questions = [], []
                if isinstance(data.get("prompt"), str) and data["prompt"].strip():
                    s.last_prompt = data["prompt"]
                self.set_state(s, "thinking")
            elif ev == "PreToolUse":
                s.tools += 1
                s.tool = data.get("tool_name")
                if s.state != "permission":
                    self.set_state(s, "tool")
                kind = self._tool_kind(s.tool) or {}
                rumble = kind.get("rumble")
                ti = data.get("tool_input")
                if is_choice_tool(s.tool) and isinstance(ti, dict):
                    s.choices = [(str(a.get("label") or ""), bool(a.get("action")))
                                 for a in ti.get("actions") or [] if isinstance(a, dict)][:4]
                    s.choices_at = time.monotonic()
                    if s.replies():
                        rumble = "choices"
                if s.tool == QUESTION_TOOL and isinstance(ti, dict):
                    s.questions = [q for q in ti.get("questions") or []
                                   if isinstance(q, dict) and isinstance(q.get("options"), list)]
                    s.questions_at = time.monotonic()
                    if s.questions:
                        rumble = "choices"
                self._flash("tool_start", kind.get("color"), rumble)
            elif ev in ("PostToolUse", "PostToolBatch"):
                if data.get("tool_name") == QUESTION_TOOL:
                    s.questions = []
                if s.state in ("tool", "permission", "waiting", "idle"):
                    self.set_state(s, "thinking")
            elif ev == "PostToolUseFailure":
                if data.get("tool_name") == QUESTION_TOOL:
                    s.questions = []
                if data.get("is_interrupt"):
                    self.set_state(s, "idle")
                else:
                    self._flash("tool_fail")
                    self.set_state(s, "thinking")
            elif ev == "PermissionDenied":
                self._flash("tool_fail")
                self.set_state(s, "thinking")
            elif ev == "PermissionRequest":
                if data.get("tool_name") == QUESTION_TOOL:
                    self.set_state(s, "waiting")  # a question box: waiting for your answer
                else:
                    self._attention(s, key)
            elif ev == "Notification":
                nt = data.get("notification_type", "")
                if nt == "permission_prompt" and s.questions:
                    self.set_state(s, "waiting")  # the question box, not an approval
                elif nt == "permission_prompt":
                    self._attention(s, key)
                elif nt in ("idle_prompt", "agent_needs_input", "elicitation_dialog", "elicitation_url_dialog"):
                    # "done" stays until a button press (the idle reminder follows Stop).
                    if s.state not in ("permission",) and not (nt == "idle_prompt" and s.state == "done"):
                        self.set_state(s, "waiting")
                elif nt == "agent_completed":
                    self.set_state(s, "done")
            elif ev == "Elicitation":
                self.set_state(s, "waiting")
            elif ev == "Stop":
                s.questions = []
                try:
                    self._update_context(s)
                except Exception:  # the context light is a nicety: never at the cost of "done"
                    log_error("_update_context")
                long_turn = s.turn_start and time.monotonic() - s.turn_start >= self.cfg.get("celebrate_after_s", 60)
                self.set_state(s, "done")
                if long_turn and s.state == "done":
                    self._flash("celebrate")
                s.turn_start = 0.0
            elif ev == "StopFailure":
                self.set_state(s, "error")
            elif ev in ("SubagentStop", "TaskCompleted"):
                self._flash("subagent")
            elif ev == "PreCompact":
                self.set_state(s, "compacting")
            elif ev == "PostCompact":
                self.set_state(s, s.prev_state if s.prev_state != "compacting" else "idle")
            elif ev == "Interrupt":
                self.set_state(s, "idle")
            elif ev == "AgentTurnComplete":  # codex `notify`
                self.set_state(s, "done")

    def _nested_event(self, wid: str, sid: str, ev: str, data: dict) -> bool:
        """An event carries the id of the `padpilot run` session it comes from, and an agent
        that session starts itself (`claude -p` from its shell tool) inherits that id. True
        for such a nested agent's events, which must not change the wrapped session.

        The wrapped agent's own session id is the first one seen. A fresh start under another
        id while it is in the middle of a turn is its child. Any other new id is taken to be
        the wrapped agent itself moving on (/clear, /resume, a restart), as before."""
        if not sid:
            return False
        if (wid, sid) in self.nested:
            if ev == "SessionEnd":
                self.nested.discard((wid, sid))
            return True
        owner = self.wrapper_owner.get(wid)
        if owner == sid:
            if ev == "SessionEnd":
                self.wrapper_owner.pop(wid, None)
            return False
        s = self.sessions.get(wid)
        if (owner is not None and ev == "SessionStart" and data.get("source") == "startup"
                and s is not None and s.state in BUSY):
            self.nested.add((wid, sid))
            return True
        self.wrapper_owner[wid] = sid
        return False

    def _attention(self, s: Session, key: str) -> None:
        self.set_state(s, "permission")
        if self.cfg.get("attention_steals_focus") and any(t.id == key for t in self.targets):
            cur = self.sessions.get(self.focus or "")
            if cur is None or cur.state != "permission" or self.focus == key:
                if self.focus != key:
                    self.focus = key
                    self._flash("focus")

    # ------------------------------------------------------------ approvals (hook mode)
    def request_approval(self, msg: dict) -> Optional[str]:
        timeout = float(msg.get("timeout") or self.cfg.get("hook_approval_timeout_s", 45))
        self.handle_event(dict(msg, event="PermissionRequest"))
        source = msg.get("source", "agent")
        key = msg.get("wrapper") or f"{source}:{msg.get('session') or 'default'}"
        # Right after a cold start the device thread may still be opening the controller. Later
        # on, no controller means nobody to ask, and a request must not wait for one.
        deadline = self.started + 2.0
        while self.dev is None and self.hid is not None and time.monotonic() < deadline:
            time.sleep(0.05)
        with self.lock:
            # Practice mode answers no real request either (see handle_control).
            if self.dev is None or timeout <= 0 or self.locked or self.practice is not None:
                return None  # the on-screen dialog takes it
            if not self._pad_in_use():
                log.info("permission request not held: the controller is not what is being used")
                return None
            a = Approval(key, (msg.get("data") or {}).get("tool_name", ""))
            self.approvals.append(a)
        a.event.wait(timeout)
        with self.lock:
            if a in self.approvals:
                self.approvals.remove(a)
            s = self.sessions.get(key)
            if s is not None and s.state == "permission":
                self.set_state(s, "tool" if a.decision == "allow" else ("idle" if a.decision == "deny" else s.state))
        return a.decision

    # ------------------------------------------------------------ input
    def _pad_in_use(self) -> bool:
        """Is the controller what the user is working with? Only then is a permission request
        held for a button press: someone at the keyboard, with the controller merely plugged
        in, would wait for the dialog for nothing. Yes if it was pressed lately, or if it is
        what was touched last, however long ago (watching a long turn from the couch)."""
        window = float(self.cfg.get("hook_approval_pad_idle_s", 300))
        if window <= 0:
            return True
        idle = time.monotonic() - self.last_input
        if idle <= window:
            return True
        other = self.keyboard_idle()
        return other is not None and idle < other

    def _grace(self) -> float:
        """Seconds after a permission request appears in which a press cannot answer it: such a
        press was already on its way to something else."""
        return float(self.cfg.get("approval_grace_ms", 600)) / 1000.0

    def _scripted_ok(self) -> bool:
        return bool(self.cfg.get("scripted_input_answers_permission"))

    def asking(self, target: Optional[Target]) -> bool:
        """True while the session that keys for `target` would reach (None: one in the desktop
        app) is asking for permission."""
        if target is not None:
            s = self.sessions.get(target.id)
            return bool(s and s.state == "permission")
        return any(s.state == "permission" for s in self._desktop_sessions())

    def handle_control(self, name: str, injected: bool = False) -> None:
        """A control the tracker reported, or with `injected` one that came over the socket
        (`padpilot press`). A scripted press never answers a permission request."""
        with self.lock:
            self.last_activity = time.monotonic()
            self._notify({"op": "control", "name": name})
            if not name.startswith("wheel:") and not name.endswith("_hold_ready"):
                log.info("button %s%s", name, " (practice)" if self.practice is not None else "")
            if name.endswith("_hold_ready") or name.startswith("wheel:"):
                if not self.locked:
                    self._play("tick")  # hold reached / wheel moved to another direction
                return
            target = self._question_box_takes(name, self._focused())
            mapping = self._mapping_for(target)
            if name.startswith("wheel_pick:"):
                action = self._wheel_steps(int(name.split(":", 1)[1]))
            else:
                action = mapping.get(name)
            if action == "@toggle_lock":
                self.locked = not self.locked
                if self.locked:  # nobody will press: hand pending approvals to the screen
                    for a in self.approvals:
                        a.event.set()
                self._play("tick")
                return
            if self.locked:
                return
            self._ack_done()
            # Practice never answers a real request, and neither does a scripted press.
            if self.approvals and self.practice is None and (not injected or self._scripted_ok()):
                a = self.approvals[0]
                verdict = None
                if name in self.cfg.get("approve_buttons", []):
                    verdict = "allow"
                elif name in self.cfg.get("deny_buttons", []):
                    verdict = "deny"
                elif name in self.cfg.get("defer_buttons", []):
                    verdict = "defer"
                if verdict in ("allow", "deny") and time.monotonic() - a.created < self._grace():
                    # Pressed as the request appeared, so meant for something else: it answers
                    # nothing and does nothing. The next press counts.
                    self._deny(name, "a permission request has just appeared; press again to answer it")
                    return
                if verdict:
                    a.decision = None if verdict == "defer" else verdict
                    self.approvals.remove(a)
                    a.event.set()
                    self._play("success" if verdict == "allow" else "tick")
                    return
            # Push to talk: the release only toggles back if the press got through.
            if name.endswith("_up") and name[:-3] in self.tracker.push_to_talk:
                if not self.ptt_sent.pop(name[:-3], False):
                    return
            ok = self._run(name, split_action(action), target, injected)
            if name in self.tracker.push_to_talk:
                self.ptt_sent[name] = ok

    def _press_without_box(self, name: str, injected: bool) -> None:
        """A press went to a question box that turned out not to be on screen: handle it again
        as if there were none. (Call with the lock held.)"""
        target = self._focused()
        action = self._mapping_for(target).get(name)
        steps = split_action(action) if isinstance(action, (str, list)) else []
        self._run(name, steps, target, injected, box=False)

    def _run(self, name: str, steps: List[str], target: Optional[Target], injected: bool = False,
             box: bool = True) -> bool:
        """Run a mapping's steps on the focused wrapper or the desktop app. False (and a
        "denied" buzz) if it could not be done. `box=False`: leave the question box out."""
        self.after_send: List[Callable[[], None]] = []
        desktop = target is None and self._desktop_enabled()
        dest = "desktop" if target is None else target.agent
        skey = target.id if target is not None else self._desktop_session()
        out: List[str] = []
        for st in steps:
            if st.startswith("@"):
                r = self._builtin(st, target, box)
                if r is False:
                    return self._deny(name, f"{st} cannot run now")
                out += r or []
            else:
                out.append(st)
        sess = self.sessions.get(skey) if skey else None
        qsess = self._question_session() if desktop and box else None
        if qsess is not None and out == ["act:submit"]:
            out = ["ask:submit"]  # ✕ / R2 press the question box's Submit
        if qsess is not None and len(out) == 1 and out[0].startswith("ask:"):
            arg = out[0][4:]
            item = {"question": None if arg == "submit" else int(arg), "questions": qsess.questions}
            if self.practice is not None:
                self._notify({"op": "would_send", "name": name, "dest": dest, "items": [item]})
                return True
            if not self._desktop_send([dict(item, at=time.monotonic(), name=name, injected=injected)]):
                return self._deny(name, "Claude app is not in front (or no Accessibility permission)")
            self._play("tick")  # it takes a moment to show on screen: say the press was taken
            return True
        if desktop:
            # The latest session may not be the one asking; any desktop session asking will do.
            asking = [x for x in self._desktop_sessions() if x.state == "permission"]
            if asking:
                sess = max(asking, key=lambda x: x.updated)
        # "act:<action>" steps become the keys this destination really uses.
        expanded, acts = [], []
        asking = bool(sess and sess.state == "permission")
        for st in out:
            if st.startswith("act:"):
                # "act:x?" is optional: skipped where the destination has no such action.
                act = st[4:].rstrip("?")
                if act == "submit" and asking and time.monotonic() - sess.since < self._grace():
                    # The prompt appeared as this was pressed: Enter would confirm it unseen.
                    return self._deny(name, "a permission prompt has just appeared; press again to answer it")
                keys = self.resolver.resolve(act, dest, permission=asking)
                if not keys:
                    if st.endswith("?"):
                        continue
                    return self._deny(name, f"{dest} has no {act!r} action")
                expanded += keys
                acts.append(act)
            elif st.startswith("text:") and "\n" in st and desktop:
                # A newline would submit in the app; Shift+Enter starts a new line instead.
                for i, line in enumerate(st[5:].split("\n")):
                    if i:
                        expanded.append("shift+enter")
                    if line:
                        expanded.append("text:" + line)
            else:
                expanded.append(st)
        try:
            items = to_items(expanded, macos.key_event if desktop else encode_key)
        except KeyError as e:
            return self._deny(name, f"unknown key {e} in the mapping")
        if not items:
            return True
        if injected and asking and not self._scripted_ok():
            return self._deny(name, "a scripted press does not answer a permission request")
        if self.practice is not None:
            self._notify({"op": "would_send", "name": name, "dest": dest, "items": items})
            return True
        if target is None:
            if not self._desktop_send(items):
                return self._deny(name, "Claude app is not in front (or no Accessibility permission)")
            if sess is not None or skey:
                self._guess_from_keys(sess.key if sess else skey, [i.get("key") for i in items], acts)
        else:
            if not target.send({"op": "type", "items": items, "gap_ms": self.cfg.get("sequence_gap_ms", 90)}):
                return self._deny(name, "the terminal session is not reading")
            self._guess_from_keys(target.id, [i.get("key") for i in items], acts)
        for fn in self.after_send:
            fn()
        return True

    def _wheel_steps(self, n: int) -> List[str]:
        presets = self.cfg.get("prompt_wheel") or []
        if not 0 <= n < len(presets) or not presets[n]:
            return ["@denied"]
        return ["act:clear_input?", "text:" + presets[n]]

    def _ack_done(self) -> None:
        """Any button press acknowledges a finished turn: its green light goes back to idle."""
        for s in self.sessions.values():
            if s.state == "done":
                self.set_state(s, "idle")

    def _question_box_takes(self, name: str, target: Optional[Target]) -> Optional[Target]:
        """A `padpilot run` session gets every button, but a question box open in the Claude app
        in front takes its own buttons (R1 + face button, Submit): None sends them there. Not
        while the terminal session is asking or waiting itself."""
        if target is None or not self._desktop_enabled() or self._question_session() is None:
            return target
        mine = self.sessions.get(target.id)
        if mine is not None and (mine.questions or mine.state in ("permission", "waiting")):
            return target
        action = self._mapping_for(None).get(name)
        steps = split_action(action) if isinstance(action, (str, list)) else []
        if not (steps == ["act:submit"] or (len(steps) == 1 and steps[0].startswith("@choose:"))):
            return target
        try:
            front = self.desktop[0]()
        except Exception:  # noqa: BLE001 - lsappinfo failing must not stop the terminal's keys
            return target
        return None if front in (self.cfg.get("desktop") or {}).get("apps", []) else target

    def _question_session(self) -> Optional[Session]:
        """The desktop session with an open question box, if any."""
        cands = [s for s in self._desktop_sessions() if s.questions]
        return max(cands, key=lambda s: s.questions_at) if cands else None

    def _reply_session(self, target: Optional[Target]) -> Optional[Session]:
        if target is not None:
            return self.sessions.get(target.id)
        cands = [s for s in self._desktop_sessions() if s.replies()]
        return max(cands, key=lambda s: s.choices_at) if cands else None

    def _update_context(self, s: Session) -> None:
        tokens, model = T.context_tokens(s.transcript)
        if not tokens:
            return
        windows = self.cfg.get("context_windows") or {}
        window = next((int(v) for k, v in sorted(windows.items(), key=lambda kv: -len(kv[0]))
                       if k != "default" and model and model.startswith(k)), int(windows.get("default", 200000)))
        if tokens > window:
            window = 1_000_000
        s.context = tokens / float(window)

    def _guess_from_keys(self, key: str, keys: List[Optional[str]], acts: Optional[List[str]] = None) -> None:
        """Hooks don't report interrupts/denials, so infer them from the action (or Esc/Enter)."""
        s = self.sessions.get(key)
        if s is None or not keys:
            return
        first = acts[0] if acts else ("cancel" if keys[0] in ("esc", "escape") else None)
        last = acts[-1] if acts else ("submit" if keys[-1] in ("enter", "return") else None)
        if first in ("cancel", "interrupt") and s.state in ("permission", "thinking", "tool", "waiting", "compacting"):
            self.set_state(s, "idle")
        elif last == "submit" and s.state == "permission":
            self.set_state(s, "tool")

    def _builtin(self, name: str, target: Optional[Target] = None, box: bool = True):
        """Run a "@..." step. Returns more steps to run, None, or False when it cannot be done."""
        now = time.monotonic()
        if name in SETTING_ACTIONS and self.practice is not None:
            # Practice (the tutorial page): show the press worked, but keep the real setting.
            self._flash("setting")
            return None
        if name == "@cycle_focus":
            if self.targets:
                ids = [t.id for t in self.targets]
                i = ids.index(self.focus) if self.focus in ids else -1
                self.focus = ids[(i + 1) % len(ids)]
                self._flash("focus")
        elif name == "@battery":
            self.overlay = "battery"
            self.overlay_until = now + 2.5
            self._play("tick")
        elif name == "@toggle_lock":
            self.locked = not self.locked
        elif name == "@denied":
            return False
        elif name in ("@prev_session", "@next_session"):
            if target is None and not self.targets:
                return ["act:" + name[1:]]  # desktop app
            if len(self.targets) < 2:
                return False
            ids = [t.id for t in self.targets]
            i = ids.index(self.focus) if self.focus in ids else 0
            self.focus = ids[(i + (1 if name == "@next_session" else -1)) % len(ids)]
            self._flash("focus")
        elif name.startswith("@choose:"):
            if box and target is None and self._desktop_enabled() and self._question_session() is not None:
                return ["ask:" + name.split(":", 1)[1]]  # an option of the open question box
            # Clicking a reply button sends its label as your message; do the same.
            s = self._reply_session(target)
            try:
                label, app = s.choices[int(name.split(":", 1)[1]) - 1] if s else ("", False)
            except (IndexError, ValueError):
                return False
            if not label or app:
                return False  # app actions (commit, PR, ...) only work by clicking
            self.after_send.append(lambda s=s: setattr(s, "choices", []))  # used up once sent
            return ["act:clear_input?", "text:" + label, "act:submit"]
        elif name == "@retry":
            s = self.sessions.get(target.id if target is not None else (self._desktop_session() or ""))
            if s is None or not s.last_prompt:
                return False
            # Still sending hook events: it really is running (maybe in another window than the
            # one in front), so pressing again does not force it.
            if s.state in BUSY and now - s.updated < float(self.cfg.get("retry_live_s", 8)):
                self.retry_armed_until = 0.0
                return False
            # Looks busy but quiet: refuse, unless pressed again within 2 s. (Stopping a turn
            # with the mouse sends no hook event, so the state can be stale.)
            if s.state in BUSY and now > self.retry_armed_until:
                self.retry_armed_until = now + 2.0
                return False
            self.retry_armed_until = 0.0
            if target is None:
                # The Claude app does not say which session is on screen, so the prompt may be
                # another session's: put it in the box and let ✕ send it.
                return ["act:clear_input?", "text:" + s.last_prompt]
            return ["act:clear_input?", "text:" + s.last_prompt, "act:submit"]
        elif name in ("@brightness_up", "@brightness_down"):
            if not self._step_setting("brightness", "brightness_levels", name.endswith("_up")):
                return False
            self._flash("setting")
        elif name in ("@rumble_up", "@rumble_down"):
            if not self._step_setting("rumble_scale", "rumble_levels", name.endswith("_up")):
                return False
            self._play("tick_strong")
        elif name == "@sounds":
            on = not (self.cfg.get("sounds") or {}).get("enabled")
            self._save_setting("sounds", on, sub="enabled")
            self._flash("setting")
            if on:
                self._cue("sounds_on")
        elif name == "@idle_light":
            self._save_setting("idle_light", not self.cfg.get("idle_light", True))
            self._flash("setting")
        return None

    def _step_setting(self, key: str, levels_key: str, up: bool) -> bool:
        cur = float(self.cfg.get(key, 1.0))
        levels = sorted(float(x) for x in self.cfg.get(levels_key) or [])
        nxt = [x for x in levels if x > cur + 1e-6] if up else [x for x in levels if x < cur - 1e-6]
        if not nxt:
            return False
        self._save_setting(key, nxt[0] if up else nxt[-1])
        return True

    def _save_setting(self, key: str, value, sub: Optional[str] = None) -> None:
        """Apply a setting now and keep it in the user's config.json (other keys untouched).
        With `sub`, set cfg[key][sub] instead."""
        if sub:
            self.cfg[key] = dict(self.cfg.get(key) or {}, **{sub: value})
        else:
            self.cfg[key] = value
        path = self.settings_path
        try:
            data = json.loads(path.read_text()) if path.exists() else {}
        except (OSError, ValueError) as e:
            log.warning("not saving %s: cannot read %s (%s)", key, path, e)
            return
        if not isinstance(data, dict):
            return
        if sub:
            cur = data.get(key) if isinstance(data.get(key), dict) else {}
            data[key] = dict(cur, **{sub: value})
        else:
            data[key] = value
        try:
            C.ensure_home()
            tmp = path.with_name(path.name + ".tmp")
            tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n")
            os.replace(tmp, path)
        except OSError as e:
            log.warning("not saving %s: %s", key, e)

    # ------------------------------------------------------------ wrappers
    def attach(self, t: Target) -> None:
        with self.lock:
            self.targets = [x for x in self.targets if x.id != t.id] + [t]
            self.focus = t.id
            s = self._session(t.id, t.agent, t.label)
            s.updated = time.monotonic()
            self.last_activity = time.monotonic()
            log.info("attached %s (%s, %s)", t.id, t.agent, t.label)

    def detach(self, t) -> None:
        """Drop a wrapper whose connection ended: that Target, or by id whichever has it. A
        wrapper that reconnects is attached again under the same id, and its old connection
        may be cleaned up only after that: the old Target is then no longer the attached one,
        and dropping it must not take the new one (and the session's state) with it."""
        with self.lock:
            if isinstance(t, Target):
                if not any(x is t for x in self.targets):
                    return
                tid = t.id
            else:
                tid = t
            self.targets = [x for x in self.targets if x.id != tid]
            self.sessions.pop(tid, None)
            self.wrapper_owner.pop(tid, None)
            self.nested = {p for p in self.nested if p[0] != tid}
            if self.focus == tid:
                self.focus = self.targets[-1].id if self.targets else None
            self.last_activity = time.monotonic()
            log.info("detached %s", tid)

    def local_input(self, tid: str, keys: List[str]) -> None:
        with self.lock:
            if any(t.id == tid for t in self.targets):
                self.focus = tid
            self._guess_from_keys(tid, keys)

    # ------------------------------------------------------------ rendering
    def displayed(self) -> Optional[Session]:
        pend = [s for s in self.sessions.values() if s.state == "permission"]
        if pend:
            return min(pend, key=lambda s: s.since)
        if self.focus and self.focus in self.sessions:
            return self.sessions[self.focus]
        if self.sessions:
            return max(self.sessions.values(), key=lambda s: s.updated)
        return None

    def _tidy(self, now: float) -> None:
        """Housekeeping that has to happen whether a controller is connected or not: drop the
        sessions that went away without saying so, reset the states that are stuck, forget the
        flashes that are over. (Call with the lock held.)"""
        # Hook-only sessions can miss SessionEnd (crash, Ctrl+C); expire them. A finished one
        # waits for you ("done" stays until a button press, its card until it is used), so
        # it is kept much longer.
        for k in [k for k, s in self.sessions.items()
                  if not any(t.id == k for t in self.targets) and now - s.updated > (
                      STALE_KEPT_S if s.state == "done" or s.replies() else STALE_SESSION_S)]:
            del self.sessions[k]
        for s in self.sessions.values():
            if s.state in ("thinking", "tool", "compacting") and now - s.updated > STUCK_BUSY_S:
                self.set_state(s, "idle", now)
            spec = self.cfg["states"].get(s.state, {})
            if spec.get("decay_to") and now - s.since > spec.get("decay_s", 20):
                self.set_state(s, spec["decay_to"], now)
        self.flashes = [f for f in self.flashes if now - f.start < f.dur]

    def tidy(self, now: float) -> None:
        with self.lock:
            self._tidy(now)

    def frame(self, now: float) -> OutputState:
        with self.lock:
            self._tidy(now)
            # Reminders and repeated rumbles are only worth giving while there is a controller
            # to give them on, so they are part of the frame.
            for s in self.sessions.values():
                spec = self.cfg["states"].get(s.state, {})
                remind = float(self.cfg.get("done_reminder_s") or 0)
                if s.state == "done" and remind and not s.reminded and now - s.since >= remind:
                    s.reminded = True
                    self._flash("done_reminder")
                rep = spec.get("repeat_rumble_s")
                if rep and s.rumbles < spec.get("repeat_max", 1) and now - s.last_rumble >= rep:
                    self._play(spec.get("rumble"))
                    s.rumbles += 1
                    s.last_rumble = now
            o = OutputState()
            snd = self.cfg.get("sounds") or {}
            if snd.get("enabled"):
                o.speaker_volume = int(snd.get("volume", 100)) if now < self.speaker_until else 0
            if now < self.demo_until:
                rgb = light_at({"kind": "rainbow", "period": 2.0}, now)
                state = "demo"
            elif self.preview is not None and self.practice is not None:
                rgb = light_at(self.preview[0], now - self.preview[1])
            else:
                s = self.displayed()
                state = s.state if s else "idle"
                spec = self.cfg["states"].get(state, self.cfg["states"]["idle"])
                light = spec["light"]
                if state == "tool" and s is not None:
                    kind = self._tool_kind(s.tool)
                    if kind and kind.get("color"):
                        light = dict(light, color=kind["color"])
                rgb = light_at(light, now - (s.since if s else 0))
                if (state == "done" and s is not None and s.context is not None
                        and s.context >= float(self.cfg.get("context_warn", 0.8))):
                    light = self.cfg.get("context_warn_light") or light
                    rgb = light_at(light, now - s.since)
                if state == "idle" and not self.cfg.get("idle_light", True):
                    rgb = (0, 0, 0)
                if self.approvals:
                    spec = self.cfg["states"]["permission"]
                o.trigger_right = make_trigger(spec.get("trigger_right"))
                o.trigger_left = make_trigger(spec.get("trigger_left"))
                if self.listening and state != "permission" and not self.approvals:
                    rgb = light_at(self.cfg["listening"]["light"], now - self.listen_since)
                if self.tracker.wheel is not None and WHEEL_BUTTON in self.tracker.down:
                    r, g, b = colorsys.hsv_to_rgb(self.tracker.wheel / 8.0, 1.0, 1.0)
                    rgb = (int(r * 255), int(g * 255), int(b * 255))
            alive = []
            for f in self.flashes:
                r = f.apply(rgb, now)
                if r is not None:
                    rgb = r
                    alive.append(f)
            self.flashes = alive
            ids = [t.id for t in self.targets]
            s = self.displayed()
            steps = self.cfg.get("progress_leds") or []
            if s is not None and s.replies() and s.state not in BUSY:
                o.player_leds = PLAYER_PATTERNS[min(5, s.replies())]  # reply buttons waiting
            elif len(ids) >= 2 and self.focus in ids:
                o.player_leds = PLAYER_PATTERNS[min(ids.index(self.focus) + 1, 5)]
            elif steps and s is not None and s.tools and s.state != "idle":
                o.player_leds = PLAYER_PATTERNS[min(5, sum(1 for x in steps if s.tools >= x))]
            elif self.focus in ids:
                o.player_leds = PLAYER_PATTERNS[min(ids.index(self.focus) + 1, 5)]
            lb = self.cfg.get("low_battery") or {}
            if (self.battery is not None and self.charging not in ("charging", "full")
                    and self.battery <= lb.get("percent", 15) and now - self.last_low_battery >= lb.get("every_s", 300)):
                self.last_low_battery = now
                self._flash("low_battery")
            if self.overlay == "battery" and now < self.overlay_until and self.battery is not None:
                lvl = self.battery / 100.0
                rgb = mix((255, 0, 0), (0, 255, 0), lvl)
                n = max(1, min(5, int(round(lvl * 5))))
                o.player_leds = (1 << n) - 1
                if s is not None and s.context is not None:  # player LEDs: context used, in fifths
                    k = max(1, min(5, int(math.ceil(s.context * 5 - 1e-9))))
                    o.player_leds = (1 << k) - 1
            o.rgb = scale(rgb, float(self.cfg.get("brightness", 1.0)))
            o.rumble_left, o.rumble_right = self.rumble.at(now)
            o.mic_led = 1 if (self.locked or self.listening) else 0
            return o

    def poll_mic(self, now: float) -> None:
        """Track whether some app records from a microphone (Claude dictation and the like)."""
        if now - self.last_mic_poll < 0.4:
            return
        self.last_mic_poll = now
        try:
            live = bool(self.mic_probe())
        except Exception:
            live = False
        with self.lock:
            if live != self.listening:
                self.listening = live
                self.listen_since = now
                self._play(self.cfg["listening"].get("rumble_on" if live else "rumble_off"))
                log.info("microphone %s", "in use" if live else "released")

    # ------------------------------------------------------------ device loop
    def _controller_lost(self) -> None:
        with self.lock:
            for a in self.approvals:
                a.event.set()
            # Buttons held when it went away must not "release" on reconnect.
            self.tracker = ControlTracker(self.cfg, self._has_hold)
            self.ptt_sent.clear()
            self._notify({"op": "controller", "controller": False})
        if self.dev:
            try:
                self.dev.close()
            except Exception:
                pass
        self.dev = None

    def _want_shared(self) -> bool:
        """Share the controller with other apps (browsers' Gamepad API) only when configured: shared,
        macOS sees the PS button too and opens its Games app. Practice mode does not need it any
        more; the tutorial page gets the buttons from the daemon."""
        return bool(self.cfg.get("share_controller"))

    def _reopen(self) -> None:
        """Close the controller so the loop opens it again (in the other sharing mode)."""
        with self.lock:
            self.tracker = ControlTracker(self.cfg, self._has_hold)
            self.ptt_sent.clear()
        if self.dev:
            try:
                self.dev.close()
            except Exception:
                pass
        self.dev = None

    def _guarded(self, fn, *args):
        """Run one step of the device loop; a bug in it is logged, not taken for a lost pad."""
        try:
            return fn(*args)
        except Exception:
            log_error(getattr(fn, "__name__", str(fn)))
            return None

    def _handle_input(self, st, now: float) -> None:
        names = self._guarded(self.tracker.update, st, now) or []
        raw, self.tracker.raw = self.tracker.raw, []
        if names or raw:
            self.last_input = now  # a press on the controller itself: it is in use
        if self.watchers:
            with self.lock:
                for n, down, held in raw:
                    self._notify({"op": "raw", "name": n, "down": down, "held_s": round(held, 3)})
        for name in names:
            self._guarded(self.handle_control, name)

    def device_loop(self) -> None:
        last_frame = 0.0
        last_try = 0.0
        last_key = b""
        last_sig = None
        last_write = 0.0
        last_status = 0.0
        last_tidy = 0.0
        while self.running:
            now = time.monotonic()
            if self.dev is None:
                # No controller, so no frames: the housekeeping a frame starts with is still due.
                if now - last_tidy >= 1.0:
                    last_tidy = now
                    self._guarded(self.tidy, now)
                if now - last_try < 2.0 or self.hid is None:
                    time.sleep(0.2)
                    continue
                last_try = now
                try:
                    infos = DualSense.find(self.hid)
                    if infos:
                        self.dev = DualSense(self.hid, infos[0], shared=self._want_shared())
                        log.info("controller connected (%s%s)", "bluetooth" if self.dev.bluetooth else "usb",
                                 ", shared" if self.dev.shared else "")
                        with self.lock:
                            self._play("hello")
                            self._notify({"op": "controller", "controller": True})
                except Exception as e:
                    log.warning("open failed: %s", e)
                    self.dev = None
                continue
            if getattr(self.dev, "shared", False) != self._want_shared():
                log.info("reopening the controller %s", "shared" if self._want_shared() else "exclusively")
                self._reopen()
                last_try = 0.0
                continue
            try:
                data = self.dev.read(10)
                if data:
                    if self.imu_samples is not None:
                        off = 22 if data[0] == 0x01 else 23
                        if len(data) >= off + 2:
                            self.imu_samples.append(struct.unpack_from("<h", data, off)[0])
                    # Sticks/buttons/triggers/touch only; the IMU part changes on every report.
                    key = data[1:12] + data[33:42] if data[0] == 0x01 else data[2:13] + data[34:43]
                    if key != last_key or self.tracker.down or now - last_status > 5:
                        last_key = key
                        st = parse_input(data)
                        if st is not None:
                            if st.battery is not None and now - last_status > 5:
                                last_status = now
                                self.battery, self.charging = st.battery, st.charging
                            self._handle_input(st, now)
                self._guarded(self.poll_mic, now)
                busy = self.rumble.active or self.flashes or self.listening
                if now - last_frame >= (FRAME_S if busy else IDLE_FRAME_S):
                    last_frame = now
                    o = self._guarded(self.frame, now) or OutputState()
                    sig = (o.rgb, o.rumble_left, o.rumble_right, o.player_leds, o.mic_led,
                           o.trigger_left, o.trigger_right, o.speaker_volume)
                    # Skip identical frames, but refresh once a second in case the pad reset.
                    if sig != last_sig or now - last_write > 1.0:
                        self.dev.write(o)
                        last_sig, last_write = sig, now
            except Exception as e:
                log.warning("controller lost: %s", e)
                self._controller_lost()
        if self.dev is not None:
            try:
                self.dev.write(OutputState(), release=True)
            except Exception:
                pass
            self.dev.close()

    # ------------------------------------------------------------ status
    def status(self) -> dict:
        with self.lock:
            return {
                "controller": None if self.dev is None else {
                    "transport": "bluetooth" if self.dev.bluetooth else "usb",
                    "battery": self.battery, "charging": self.charging,
                },
                "locked": self.locked,
                "listening": self.listening,
                "focus": self.focus,
                "targets": [{"id": t.id, "agent": t.agent, "label": t.label} for t in self.targets],
                "sessions": [{"key": s.key, "agent": s.agent, "label": s.label, "state": s.state,
                              "last_event": s.last_event, "age_s": round(time.monotonic() - s.since, 1),
                              "tool": s.tool, "tools_this_turn": s.tools,
                              "choices": [label for label, app in s.choices if label and not app],
                              "can_retry": bool(s.last_prompt) and s.state not in BUSY,
                              "entry": s.entry, "attended": s.attended,
                              "context": None if s.context is None else round(s.context, 3)}
                             for s in self.sessions.values()],
                "pending_approvals": len(self.approvals),
                "pad_idle_s": None if self.last_input < 0 else round(time.monotonic() - self.last_input, 1),
                "version": __version__,
                "code": self.code,
                "desktop": self.desktop_status(),
                "keybindings": self.resolver.describe(),
                "pid": os.getpid(),
            }

    def desktop_status(self, prompt: bool = False) -> dict:
        front, trusted, _ = self.desktop
        if not self._desktop_enabled():
            return {"enabled": False}
        return {"enabled": True, "apps": (self.cfg.get("desktop") or {}).get("apps", []),
                "trusted": trusted(prompt) if prompt else trusted(), "frontmost": front()}


def _can(check, key: str) -> bool:
    try:
        check(key)
        return True
    except KeyError:
        return False


# ---------------------------------------------------------------- server
def _handle_msg(d: Daemon, conn: socket.socket, msg: dict, reply, link: dict) -> None:
    """One message of a connection. `link["target"]` is the wrapper attached over it, if any."""
    target: Optional[Target] = link["target"]
    op = msg.get("op")
    if op == "event":
        d.handle_event(msg)
        reply({"ok": True})
    elif op == "approval":
        reply({"decision": d.request_approval(msg)})
    elif op == "attach":
        link["target"] = target = Target(msg["wrapper"], msg.get("agent", ""), msg.get("label", ""), conn)
        d.attach(target)
        reply({"ok": True, "controller": d.dev is not None})
    elif op == "local" and target is not None:
        d.local_input(target.id, msg.get("keys") or [])
    elif op == "control":  # a scripted press (`padpilot press`), for tests and scripts
        d.handle_control(msg.get("name", ""), injected=True)
        reply({"ok": True})
    elif op == "send":  # type text into the focused wrapper (or desktop app), then Enter
        items = [{"text": msg.get("text", "")}] + ([] if msg.get("no_enter") else [{"key": "enter"}])
        with d.lock:
            t = d._focused()
            if d.asking(t) and not d._scripted_ok():
                ok = False  # typed text is not the answer to a permission request
                log.info("send: refused, the session is asking for permission")
            elif t is not None:
                ok = t.send({"op": "type", "gap_ms": d.cfg.get("sequence_gap_ms", 90), "items": items})
            else:
                ok = d._desktop_send(items)
        reply({"ok": ok})
    elif op == "desktop":  # desktop-key status; prompt=True shows the macOS permission dialog
        reply(d.desktop_status(prompt=bool(msg.get("prompt"))))
    elif op == "status":
        reply(d.status())
    elif op == "watch":
        w = Target("watch", "", "", conn)
        with d.lock:
            d.add_watcher(w, practice=bool(msg.get("practice")))
        reply({"ok": True, "controller": d.dev is not None, "practice": bool(msg.get("practice"))})
    elif op == "demo":
        with d.lock:
            d.demo_until = time.monotonic() + float(msg.get("seconds", 3))
            d._play("success")
        reply({"ok": True})
    elif op == "rumble":
        with d.lock:
            d._play(msg.get("pattern", "tick"))
        reply({"ok": True})
    elif op == "imu":  # debug: accelerometer jitter, used to verify rumble
        d.imu_samples = []
        time.sleep(float(msg.get("seconds", 0.5)))
        xs, d.imu_samples = d.imu_samples, None
        reply({"n": len(xs), "stdev": round(statistics.pstdev(xs), 1) if len(xs) > 1 else None})
    elif op == "shutdown":
        reply({"ok": True})
        d.running = False
        try:  # wake the accept loop, so stopping does not wait for its timeout
            s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            s.settimeout(0.2)
            s.connect(str(C.SOCKET_PATH))
            s.close()
        except OSError:
            pass
    elif op == "ping":
        # A client with newer code replaces this daemon (client.start_daemon); it goes
        # by the code and start time reported here, and waits while a request is held.
        reply({"ok": True, "pid": os.getpid(), "version": __version__, "code": d.code,
               "started": d.started_at, "approvals": len(d.approvals)})


def _serve_conn(d: Daemon, conn: socket.socket) -> None:
    f = conn.makefile("rb")
    link: dict = {"target": None}

    def reply(obj):
        try:
            conn.sendall((json.dumps(obj) + "\n").encode())
        except OSError:
            pass

    try:
        for line in f:
            try:
                msg = json.loads(line)
            except ValueError:
                continue
            if not isinstance(msg, dict):
                continue
            try:
                _handle_msg(d, conn, msg, reply, link)
            except OSError:
                raise
            except Exception:  # a message we cannot handle must not end the connection
                log_error("message %r" % (msg.get("op"),))
                reply({"ok": False})
    except OSError:
        pass
    finally:
        if link["target"] is not None:
            d.detach(link["target"])
        with d.lock:
            for w in [w for w in d.watchers if w.conn is conn]:
                d.remove_watcher(w)
        try:
            conn.close()
        except OSError:
            pass


def _acquire_singleton():
    C.ensure_home()
    fd = os.open(C.PID_PATH, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        os.close(fd)
        return None
    os.ftruncate(fd, 0)
    os.write(fd, str(os.getpid()).encode())
    return fd


def run_daemon(foreground: bool = False) -> int:
    lock_fd = _acquire_singleton()
    if lock_fd is None:
        print("padpilot daemon already running")
        return 0
    handlers = [logging.StreamHandler()] if foreground else []
    try:  # daemon.log, and daemon.log.1 with what came before: 2 MB at most
        handlers.append(logging.handlers.RotatingFileHandler(C.LOG_PATH, maxBytes=1_000_000, backupCount=1))
    except OSError:
        pass
    try:  # our stderr (what Python itself prints, notes from clients) is appended to this file
        err = C.HOME / "daemon.stderr"
        if err.stat().st_size > 1_000_000:
            os.truncate(err, 0)
    except OSError:
        pass
    logging.basicConfig(level=logging.INFO, handlers=handlers,
                        format="%(asctime)s %(levelname)s %(message)s")
    try:
        import hid  # type: ignore
    except Exception as e:
        log.error("python 'hidapi' module missing (%s); run `padpilot setup`", e)
        hid = None
    problems: List[str] = []
    d = Daemon(C.load_config(problems), hid)
    for p in problems:
        log.warning("config.json: %s", p)
    try:
        d.code = client.code_id()
    except OSError:
        pass

    if C.SOCKET_PATH.exists():
        C.SOCKET_PATH.unlink()
    srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    srv.bind(str(C.SOCKET_PATH))
    os.chmod(C.SOCKET_PATH, 0o600)
    srv.listen(16)
    srv.settimeout(1.0)

    def stop(*_):
        d.running = False
    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)

    dev_thread = threading.Thread(target=d.device_loop, name="device", daemon=True)
    dev_thread.start()
    log.info("daemon started pid=%s", os.getpid())
    from . import web
    web_srv = web.start(d, int(d.cfg.get("tutorial_port") or 0))
    idle_limit = float(d.cfg.get("idle_shutdown_minutes", 240)) * 60
    # Clients delete staged code dirs untouched for an hour; keep ours fresh while it runs (the
    # speaker child process and late imports still load from it).
    code_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    staged = os.path.dirname(code_root) == str(C.HOME / "runtime")
    touched = 0.0
    try:
        while d.running:
            if staged and time.monotonic() - touched > 600:
                touched = time.monotonic()
                try:
                    os.utime(code_root)
                except OSError:
                    pass
            try:
                conn, _ = srv.accept()
            except socket.timeout:
                with d.lock:
                    # A `padpilot watch` (practice mode) keeps the daemon alive too.
                    idle = not d.targets and not d.watchers and time.monotonic() - d.last_activity > idle_limit
                if idle_limit > 0 and idle:
                    log.info("idle for %.0f min, exiting", idle_limit / 60)
                    d.running = False
                continue
            threading.Thread(target=_serve_conn, args=(d, conn), daemon=True).start()
    finally:
        d.running = False
        if web_srv is not None:
            web_srv.shutdown()
            web_srv.server_close()
        dev_thread.join(timeout=2)
        try:
            srv.close()
            C.SOCKET_PATH.unlink()
        except OSError:
            pass
        log.info("daemon stopped")
    return 0
