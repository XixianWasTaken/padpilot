"""Paths and configuration. User config in ~/.dualsense-agent/config.json is deep-merged over DEFAULTS."""
from __future__ import annotations

import copy
import json
import os
from pathlib import Path

HOME = Path(os.environ.get("DSAGENT_HOME") or Path.home() / ".dualsense-agent")
SOCKET_PATH = HOME / "daemon.sock"
PID_PATH = HOME / "daemon.pid"
LOG_PATH = HOME / "daemon.log"
CONFIG_PATH = HOME / "config.json"
VENV_PATH = HOME / "venv"

# Colors are [r, g, b]. Light kinds: solid, breathe, blink, rainbow.
# Rumble patterns are lists of [left(strong), right(weak), milliseconds].
DEFAULTS = {
    "brightness": 1.0,
    "states": {
        "offline":    {"light": {"kind": "solid", "color": [0, 0, 0]}},
        "idle":       {"light": {"kind": "breathe", "color": [90, 90, 110], "period": 5.0, "min": 0.15}},
        # While the agent works, L2 has light resistance: pulling it through = Esc (interrupt).
        "thinking":   {"light": {"kind": "breathe", "color": [0, 90, 255], "period": 2.2, "min": 0.2},
                       "trigger_left": {"type": "feedback", "position": 4, "strength": 2}},
        # "tool" takes the color of the tool kind (see tool_kinds) when one matches.
        "tool":       {"light": {"kind": "breathe", "color": [0, 200, 255], "period": 1.2, "min": 0.35},
                       "trigger_left": {"type": "feedback", "position": 4, "strength": 2}},
        "permission": {"light": {"kind": "blink", "color": [255, 120, 0], "period": 0.7},
                       "rumble": "attention", "repeat_rumble_s": 20, "repeat_max": 3,
                       "trigger_right": {"type": "weapon", "start": 3, "end": 6, "strength": 7},
                       "trigger_left": {"type": "feedback", "position": 2, "strength": 3}},
        "waiting":    {"light": {"kind": "breathe", "color": [255, 200, 0], "period": 3.0, "min": 0.3},
                       "rumble": "nudge"},
        # Stays until you press any button; one reminder after done_reminder_s.
        "done":       {"light": {"kind": "solid", "color": [0, 255, 60]}, "rumble": "success"},
        "error":      {"light": {"kind": "blink", "color": [255, 0, 0], "period": 1.2}, "rumble": "error",
                       "decay_to": "idle", "decay_s": 30},
        "compacting": {"light": {"kind": "breathe", "color": [170, 0, 255], "period": 1.6, "min": 0.25},
                       "trigger_left": {"type": "feedback", "position": 4, "strength": 2}},
    },
    # Any app is recording from a microphone (e.g. Claude's dictation): white pulse + mic LED.
    "listening": {"light": {"kind": "breathe", "color": [255, 255, 255], "period": 1.0, "min": 0.3},
                  "rumble_on": "listen", "rumble_off": "tick"},
    # PreToolUse: flash + rumble by tool kind; the "tool" state breathes in that color.
    # First matching regex wins (Claude and Codex tool names).
    "tool_kinds": [
        ["^(Bash|BashOutput|KillShell|shell|exec_command|local_shell)$", {"color": [255, 130, 0], "rumble": "tick_strong"}],
        ["^(Edit|MultiEdit|Write|NotebookEdit|apply_patch)$", {"color": [255, 0, 170], "rumble": "thud"}],
        ["^(Read|Grep|Glob|LS|view_image)$", {"color": [0, 200, 255], "rumble": "tick"}],
        ["^(WebFetch|WebSearch|web_search)$", {"color": [40, 90, 255], "rumble": "tick"}],
        ["^(Task|Agent)$", {"color": [170, 0, 255], "rumble": "nudge"}],
        ["^mcp__", {"color": [0, 255, 150], "rumble": "tick"}],
    ],
    # Player LEDs fill up with the number of tool calls in the current turn (when no
    # several `dsagent run` sessions need them to show the focus). Thresholds per LED step.
    "progress_leds": [1, 3, 6, 11, 21],
    # A turn that ran at least this long ends with a rainbow + fanfare instead of plain green.
    "celebrate_after_s": 60,
    "low_battery": {"percent": 15, "every_s": 300},
    "done_reminder_s": 600,
    # Context used by the last request, from the session transcript. At context_warn or more,
    # the "done" light fades between green and amber, and PS (tap) shows it on the player LEDs.
    # Windows by model id prefix; a model not listed uses "default", and one seen using more
    # than its window is taken to have 1M.
    "context_windows": {"default": 200000, "claude-opus-5": 1000000},
    "context_warn": 0.8,
    "context_warn_light": {"kind": "fade", "color": [0, 255, 60], "color2": [255, 140, 0], "period": 3.0},
    # false: the idle light is off (PS + square toggles it).
    "idle_light": True,
    # Sound cues on the controller's speaker (USB only). Values are macOS system sounds
    # (/System/Library/Sounds) or file paths. PS + △ turns them on/off.
    "sounds": {"enabled": True, "volume": 100, "device": "DualSense Wireless Controller",
               "channel": 1, "channels": 4, "gain": 1.0, "hold_s": 2.5, "tail_ms": 4000,
               "cues": {"done": "Glass", "permission": "Ping", "error": "Basso",
                        "sounds_on": "Pop"}},
    # Hold triangle, point the right stick at one of 8 directions (up first, clockwise),
    # release: the prompt is typed into the message box (not sent). Tap triangle = continue.
    "prompt_wheel": [
        "继续", "运行测试并修复失败的用例", "解释一下你刚才做了什么", "先停下来，问我再继续",
        "提交代码", "给这部分写测试", "总结一下改动", "继续修这个 bug",
    ],
    # Buttons that combine with others ("ps+up", "r1+cross"). Their own tap fires on release,
    # and only if no combo was used.
    "modifiers": ["ps", "r1", "l1"],
    # Pressed = the action; released after ptt_ms = the "<name>_up" action (push to talk).
    "push_to_talk": ["mic"],
    "ptt_ms": 500,
    "brightness_levels": [0.1, 0.25, 0.4, 0.6, 0.8, 1.0],
    "rumble_levels": [0.0, 0.3, 0.6, 1.0, 1.4],
    "flashes": {
        "tool_start": {"color": [255, 255, 255], "ms": 160, "rumble": "tick"},
        "tool_fail":  {"color": [255, 0, 0], "ms": 400, "rumble": "buzz"},
        "subagent":   {"color": [200, 0, 255], "ms": 250, "rumble": "tick"},
        "focus":      {"color": [255, 255, 255], "ms": 250, "rumble": "tick"},
        "celebrate":  {"kind": "rainbow", "ms": 2400, "rumble": "fanfare"},
        "low_battery": {"color": [255, 0, 0], "ms": 1200, "rumble": "low_battery"},
        # A button that cannot do anything right now (no such action here, nothing to retry...).
        "denied":     {"color": [255, 0, 0], "ms": 300, "rumble": "denied"},
        "setting":    {"color": [255, 255, 255], "ms": 500, "rumble": "tick"},
        "done_reminder": {"color": [0, 255, 60], "ms": 900, "rumble": "nudge"},
    },
    "rumble_patterns": {
        "tick":      [[0, 50, 35]],
        "nudge":     [[60, 0, 90]],
        "buzz":      [[0, 140, 120]],
        "attention": [[200, 120, 120], [0, 0, 90], [200, 120, 120]],
        "success":   [[0, 110, 70], [0, 0, 60], [0, 170, 110]],
        "error":     [[255, 0, 450]],
        "hello":     [[0, 70, 60], [0, 0, 40], [70, 0, 60]],
        "tick_strong": [[0, 100, 45]],
        "thud":      [[90, 0, 45], [0, 0, 35], [90, 0, 45]],
        "listen":    [[0, 45, 25]],
        "fanfare":   [[0, 80, 60], [0, 0, 40], [0, 120, 60], [0, 0, 40], [110, 200, 180]],
        "low_battery": [[120, 0, 150], [0, 0, 150], [120, 0, 150]],
        "denied":    [[0, 170, 60], [0, 0, 60], [0, 170, 60]],
        "choices":   [[0, 60, 40], [0, 0, 50], [0, 60, 40]],
    },
    "rumble_scale": 1.0,
    # When a session asks for permission, input focus jumps to it.
    "attention_steals_focus": True,
    # Hook-based approvals (used when the agent is NOT running under `dsagent run`,
    # e.g. Claude desktop / IDE). The hook waits this long for a button, then falls back
    # to the normal on-screen dialog. 0 disables hook approvals.
    "hook_approval_timeout_s": 45,
    "approve_buttons": ["cross", "r2_full"],
    "deny_buttons": ["circle", "l2_full"],
    "defer_buttons": ["triangle"],
    # Let other apps read the controller too (browsers' Gamepad API). Practice mode does this anyway.
    # Off by default: shared, macOS also sees the PS button and opens its Games app.
    "share_controller": False,
    # The tutorial page (`dsagent tutorial`) is served on http://127.0.0.1:<port>/; 0 turns it off.
    "tutorial_port": 8737,
    "idle_shutdown_minutes": 240,
    "stick_threshold": 0.6,
    "trigger_full": 235,
    "repeat_delay_ms": 350,
    "repeat_rate_ms": 70,
    "hold_ms": 600,
    "sequence_gap_ms": 90,
    "swipe_min": 350,
    # Controller layout, the same for the Claude desktop app and the CLIs (`dsagent run`).
    # Values: "act:<action>" (resolved to the keys the destination really uses, including the
    # user's ~/.claude/keybindings.json and Claude's dictation shortcut; see bindings.py),
    # plain key names (enter, esc, up, ctrl+c, cmd+k, ...), "text:...", "@builtin" actions,
    # or a list of those to run in sequence. An action the destination does not have (e.g.
    # dictation in a terminal) flashes red.
    "mapping": {
        "cross": "act:submit",
        "circle": "act:cancel",
        "square": "act:clear_input",
        "triangle": ["text:continue", "act:submit"],
        "up": "up", "down": "down", "left": "left", "right": "right",
        "stick_up": "up", "stick_down": "down", "stick_left": "left", "stick_right": "right",
        "rstick_up": "pageup", "rstick_down": "pagedown",
        # Desktop app: previous / next session (Cmd+Shift+[ / ]); CLIs: the next `dsagent run`.
        "rstick_left": "@prev_session", "rstick_right": "@next_session",
        "l1": "act:cycle_mode",
        "r1": "text:/",
        "r2_full": "act:submit",
        "l2_full": "act:cancel",
        "options": "act:review",
        "create": "@cycle_focus",
        "create_hold": ["act:clear_input?", "act:new_session"],
        "ps": "@battery",
        "ps_hold": "act:interrupt",
        "mic": "act:dictation",
        "mic_up": "act:dictation",
        "touchpad": "act:rewind",
        "swipe_up": "pageup", "swipe_down": "pagedown",
        "swipe_left": "act:history_search", "swipe_right": "@none",
        "l3": "act:menu",
        "r3": "@retry",
        # R1 + face button: pick a reply button on Claude's next-step card (1st..4th).
        "r1+cross": "@choose:1", "r1+circle": "@choose:2", "r1+square": "@choose:3", "r1+triangle": "@choose:4",
        # PS layer: settings (saved to config.json).
        "ps+up": "@brightness_up", "ps+down": "@brightness_down",
        "ps+right": "@rumble_up", "ps+left": "@rumble_down",
        "ps+square": "@idle_light", "ps+triangle": "@sounds", "ps+mic": "@toggle_lock",
        # L1 layer: less common agent actions. L1 alone (tap) cycles the permission mode.
        "l1+cross": "act:queue_submit", "l1+circle": "act:kill_agents",
        "l1+square": "act:undo", "l1+triangle": "act:external_editor",
        "l1+up": "act:model_picker", "l1+down": "act:thinking",
    },
    # Optional per-destination overrides of "mapping": "claude", "codex" (by wrapped command
    # name) and "desktop". Empty by default so every surface has the same layout.
    "profiles": {
        "claude": {},
        "codex": {},
        "desktop": {},
    },
    # macOS: with no `dsagent run` session attached, send the buttons as real key presses to
    # the frontmost app, but only if it is one of these. Needs Accessibility permission for
    # Python (run `dsagent desktop`).
    "desktop": {
        "enabled": True,
        "apps": ["com.anthropic.claudefordesktop"],
    },
}


# Where a null value means "nothing bound here" rather than "use the default".
NULL_OK = ("mapping", "profiles")


def deep_merge(base, over, null_ok: bool = False):
    """`over` on top of `base`. A value of the wrong shape (null, or a non-object where an
    object belongs) keeps the default, so an odd config.json cannot crash the daemon or hooks."""
    out = copy.deepcopy(base)
    if not isinstance(over, dict):
        return out
    for k, v in over.items():
        cur = out.get(k)
        if isinstance(v, dict) and isinstance(cur, dict):
            out[k] = deep_merge(cur, v, null_ok or k in NULL_OK)
        elif isinstance(cur, dict) or (v is None and k in out and not null_ok):
            continue
        else:
            out[k] = copy.deepcopy(v)
    return out


def load_config() -> dict:
    user = {}
    try:
        with open(CONFIG_PATH) as f:
            user = json.load(f)
    except FileNotFoundError:
        pass
    except Exception as e:  # keep running on a broken config
        import sys
        print(f"dsagent: ignoring invalid {CONFIG_PATH}: {e}", file=sys.stderr)
    return deep_merge(DEFAULTS, user)


def ensure_home() -> None:
    HOME.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(HOME, 0o700)
    except OSError:
        pass
