"""Command line interface: `padpilot <command>`."""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
import venv
from pathlib import Path

from . import client
from . import config as C

USAGE = """padpilot - drive Claude Code / Codex with a DualSense controller

  padpilot run <cmd> [args]   run an agent (claude, codex, ...) with controller input
  padpilot status             controller + session status (JSON)
  padpilot tutorial           open the tutorial page (practice mode while it is open)
  padpilot manual             open the manual (Chinese / English)
  padpilot watch              print controller inputs as they happen
  padpilot demo               light + rumble demo
  padpilot rumble <pattern>   play a rumble pattern (tick, success, attention, error, ...)
  padpilot press <control>    act as if a control was pressed (cross, circle, ps_hold, ...)
  padpilot send <text>        type text + Enter into the focused session (--no-enter: text only)
  padpilot desktop            macOS: let the buttons drive the Claude desktop app (permission + status)
  padpilot keys               the button layout and the keys each button sends (reads your shortcuts)
  padpilot start | stop       start / stop the background daemon
  padpilot daemon [-f]        run the daemon in this process (-f: log to stderr)
  padpilot setup              create the venv with hidapi (needed by the daemon)
  padpilot doctor             check the installation
  padpilot install-codex      add padpilot hooks to $CODEX_HOME/hooks.json (default ~/.codex)
  padpilot uninstall-codex    remove them again
  padpilot hook <claude|codex>   hook entry point (reads hook JSON on stdin)
"""

CODEX_EVENTS = ["SessionStart", "SessionEnd", "UserPromptSubmit", "PreToolUse", "PermissionRequest",
                "PostToolUse", "PreCompact", "PostCompact", "SubagentStop", "Stop", "Interrupt"]
# The one package padpilot installs (from PyPI, into ~/.padpilot/venv), pinned to an exact version.
HIDAPI = "hidapi==0.15.0"
MARK = "padpilot hook codex"
OLD_MARK = "dsagent hook codex"  # hooks installed before 0.4, when the command was called dsagent


def main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv or argv[0] in ("-h", "--help", "help"):
        print(USAGE)
        return 0
    cmd, rest = argv[0], argv[1:]
    if cmd == "hook":
        from .hook import run_hook
        return run_hook(rest[0] if rest else "claude")
    if cmd == "codex-notify":
        from .hook import run_codex_notify
        return run_codex_notify(rest)
    if cmd == "run":
        from .wrapper import run_wrapped
        if rest and rest[0] == "--":
            rest = rest[1:]
        return run_wrapped(rest)
    if cmd == "daemon":
        from .daemon import run_daemon
        return run_daemon(foreground="-f" in rest)
    if cmd == "start":
        ok = client.start_daemon(wait=4.0)
        print("daemon running" if ok else f"daemon failed to start, see {C.LOG_PATH} and {C.HOME / 'daemon.stderr'}")
        return 0 if ok else 1
    if cmd == "stop":
        r = client.request({"op": "shutdown"})
        # Wait until the old daemon has exited, so a `start` right after really starts one.
        if r:
            client.wait_gone(5.0)
        print("stopped" if r else "not running")
        return 0
    if cmd == "status":
        r = client.request({"op": "status"})
        if r is None:
            print(json.dumps({"daemon": "not running"}))
            return 1
        print(json.dumps(r, indent=2, ensure_ascii=False))
        return 0
    if cmd == "demo":
        client.start_daemon()
        return _ok(client.request({"op": "demo", "seconds": float(rest[0]) if rest else 3}))
    if cmd == "rumble":
        client.start_daemon()
        return _ok(client.request({"op": "rumble", "pattern": rest[0] if rest else "tick"}))
    if cmd == "press":
        return _ok(client.request({"op": "control", "name": rest[0] if rest else ""}))
    if cmd == "send":
        no_enter = "--no-enter" in rest
        text = " ".join(a for a in rest if a != "--no-enter")
        r = client.request({"op": "send", "text": text, "no_enter": no_enter})
        return _ok(r and r.get("ok"))
    if cmd == "desktop":
        return desktop()
    if cmd == "keys":
        return keys_table()
    if cmd == "tutorial":
        return tutorial(open_browser="--no-open" not in rest)
    if cmd == "manual":
        return tutorial(open_browser="--no-open" not in rest, page="manual")
    if cmd == "watch":  # --practice: nothing is sent to apps
        return _watch(practice="--practice" in rest)
    if cmd == "setup":
        return setup(quiet="--quiet" in rest, then_daemon="--then-daemon" in rest)
    if cmd == "doctor":
        return doctor()
    if cmd == "install-codex":
        return install_codex()
    if cmd == "uninstall-codex":
        return uninstall_codex()
    print(USAGE)
    return 2


def _ok(r) -> int:
    print("ok" if r else "daemon not reachable")
    return 0 if r else 1


def _watch(practice: bool = False) -> int:
    """Print what the controller does. When the daemon restarts (an update, a crash) the watch
    reconnects by itself: a practice run that quietly ended would let the next presses reach the
    app for real."""
    import time as _time
    t0 = _time.monotonic()

    def items_text(items):
        return " ".join(repr(i["text"]) if "text" in i else i["key"] if "key" in i else
                        ("question box: submit" if i.get("question") is None else f"question box: option {i['question']}")
                        for i in items)

    def show(msg):
        ts = f"{_time.monotonic() - t0:7.2f}"
        op = msg.get("op")
        if op == "raw":
            held = "" if msg["down"] else f"  held {msg['held_s']:.2f}s"
            print(f"{ts}    {'down' if msg['down'] else 'up  '} {msg['name']}{held}", flush=True)
        elif op == "control":
            print(f"{ts}  => {msg['name']}", flush=True)
        elif op == "would_send":
            print(f"{ts}     would send to {msg['dest']}: {items_text(msg['items'])}", flush=True)
        elif op == "denied":
            print(f"{ts}     DENIED (double buzz): {msg['reason']}", flush=True)
        elif "controller" in msg:
            print("controller:", "connected" if msg["controller"] else "NOT connected", flush=True)

    first = True
    try:
        while True:
            client.start_daemon()
            s = client.connect(timeout=1.0)
            if s is None:
                if first:
                    print("daemon not reachable")
                    return 1
                print("daemon not reachable, retrying...", flush=True)
                _time.sleep(1.0)
                continue
            s.settimeout(None)
            s.sendall((json.dumps({"op": "watch", "practice": practice}) + "\n").encode())
            if first:
                if practice:
                    print("PRACTICE MODE: lights and rumble work, but no keys are sent to any app.")
                print("press buttons on the controller (Ctrl+C to quit)", flush=True)
            else:
                print("reconnected" + (" (still in practice mode)" if practice else ""), flush=True)
            first = False
            buf = b""
            while True:
                try:
                    chunk = s.recv(4096)
                except OSError:
                    chunk = b""
                if not chunk:
                    break
                buf += chunk
                while b"\n" in buf:
                    line, buf = buf.split(b"\n", 1)
                    try:
                        show(json.loads(line))
                    except (ValueError, KeyError):
                        pass
            s.close()
            print("daemon restarted, reconnecting..."
                  + (" (presses may reach the app until it is back)" if practice else ""), flush=True)
            _time.sleep(0.3)
    except KeyboardInterrupt:
        return 0


def tutorial(open_browser: bool = True, page: str = "") -> int:
    """Open a page the daemon serves: the tutorial (practice mode while it is open) or, with
    page="manual", the manual (no practice mode)."""
    import urllib.request
    port = int(C.load_config().get("tutorial_port") or 0)
    if not port:
        print('the tutorial page is turned off ("tutorial_port": 0 in the config)')
        return 1
    if not client.start_daemon(wait=4.0):
        print(f"daemon failed to start, see {C.LOG_PATH}")
        return 1
    url = f"http://127.0.0.1:{port}/{page}"
    deadline = time.monotonic() + 3
    while True:
        try:
            with urllib.request.urlopen(url, timeout=1) as r:
                head = r.read(4096).lower()
                if r.headers.get("Server", "").startswith("padpilot") and b"<title" in head:
                    break
                err = f"another program answers on port {port}"
        except OSError as e:
            err = str(e)
        if time.monotonic() > deadline:
            print(f"tutorial page not reachable at {url}: {err}")
            print(f"see {C.LOG_PATH}; another port can be set with \"tutorial_port\" in the config")
            return 1
        time.sleep(0.2)
    print(url)
    if open_browser:
        subprocess.run(["open" if sys.platform == "darwin" else "xdg-open", url], check=False)
    return 0


# ---------------------------------------------------------------- setup / doctor
def _venv_has_hid() -> bool:
    py = C.VENV_PATH / "bin" / "python3"
    if not py.exists():
        return False
    return subprocess.run([str(py), "-c", "import hid"], capture_output=True).returncode == 0


def setup(quiet: bool = False, then_daemon: bool = False) -> int:
    C.ensure_home()
    import fcntl
    lock = open(C.HOME / "setup.lock", "w")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | (fcntl.LOCK_NB if then_daemon else 0))
    except OSError:
        return 0  # another setup is already running and will start the daemon
    say = (lambda *a: None) if quiet else print
    if not _venv_has_hid():
        say(f"creating venv in {C.VENV_PATH} ...")
        venv.EnvBuilder(with_pip=True, clear=False).create(str(C.VENV_PATH))
        py = str(C.VENV_PATH / "bin" / "python3")
        say("installing hidapi ...")
        r = subprocess.run([py, "-m", "pip", "install", "--disable-pip-version-check", "-q", HIDAPI],
                           capture_output=quiet)
        if r.returncode != 0 or not _venv_has_hid():
            print("padpilot setup: installing hidapi failed", file=sys.stderr)
            if quiet and r.stderr:
                sys.stderr.write(r.stderr.decode(errors="replace"))
            return 1
    say("venv ok")
    if then_daemon:
        py = str(C.VENV_PATH / "bin" / "python3")
        os.execv(py, [py, "-m", "padpilot", "daemon"])
    return 0


def doctor() -> int:
    ok = True

    def line(good, text):
        nonlocal ok
        ok = ok and good
        print(("  ok   " if good else "  FAIL ") + text)

    print(f"python: {sys.executable} {sys.version.split()[0]}")
    line(sys.version_info >= (3, 8), "python >= 3.8")
    problems = []
    C.load_config(problems)
    line(not problems, f"settings in {C.CONFIG_PATH}" + ("" if not problems else " (the daemon runs with the defaults for these):"))
    for p in problems:
        print("         " + p)
    line(_venv_has_hid(), f"hidapi installed in {C.VENV_PATH} (fix: padpilot setup)")
    if _venv_has_hid():
        py = str(C.VENV_PATH / "bin" / "python3")
        code = ("import hid,sys; sys.path.insert(0, %r); from padpilot.dualsense import DualSense; "
                "print(len(DualSense.find(hid)))" % client.PKG_ROOT)
        r = subprocess.run([py, "-c", code], capture_output=True, text=True)
        n = r.stdout.strip()
        line(r.returncode == 0 and n not in ("", "0"), f"DualSense found via HID (count={n or r.stderr.strip()[-200:]})")
    st = client.request({"op": "status"})
    line(st is not None, "daemon running (fix: padpilot start)")
    if st:
        line(st.get("controller") is not None, f"daemon has the controller: {st.get('controller')}")
        dk = st.get("desktop") or {}
        if dk.get("enabled") and sys.platform == "darwin":
            line(bool(dk.get("trusted")), "Accessibility permission for desktop-app control (fix: padpilot desktop)")
    return 0 if ok else 1


BUTTONS = [
    ("cross", "✕"), ("circle", "○"), ("square", "□"), ("triangle", "△ (tap)"),
    ("wheel_pick:0", "△ (hold) + right stick"),
    ("r2_full", "R2 (full)"), ("l2_full", "L2 (full)"), ("l1", "L1 (tap)"), ("r1", "R1 (tap)"),
    ("l1+cross", "L1 + ✕"), ("l1+circle", "L1 + ○"), ("l1+square", "L1 + □"), ("l1+triangle", "L1 + △"),
    ("l1+up", "L1 + ↑"), ("l1+down", "L1 + ↓"),
    ("r1+cross", "R1 + ✕ / ○ / □ / △"),
    ("up", "D-pad / left stick"), ("rstick_up", "right stick ↑↓"), ("rstick_right", "right stick → / ←"), ("options", "Options"),
    ("create", "Create"), ("create_hold", "Create (hold)"), ("touchpad", "Touchpad press"),
    ("swipe_up", "Swipe ↑↓"), ("swipe_left", "Swipe ←"),
    ("mic", "Mic (tap / hold = push to talk)"), ("l3", "L3"), ("r3", "R3"),
    ("ps", "PS (tap)"), ("ps_hold", "PS (hold)"), ("ps+up", "PS + ↑ / ↓"), ("ps+right", "PS + → / ←"),
    ("ps+square", "PS + □"), ("ps+mic", "PS + Mic"),
]
BUILTINS = {
    "@cycle_focus": "next `padpilot run` session", "@battery": "battery level",
    "@toggle_lock": "lock / unlock the controller", "@retry": "send your last message again",
    "@brightness_up": "lightbar brighter / dimmer", "@rumble_up": "rumble stronger / weaker",
    "@idle_light": "idle light on / off", "@sounds": "spoken cues on the controller speaker on / off", "@next_session": "next / previous session",
}


def keys_table() -> int:
    from .bindings import Resolver
    from .daemon import _can
    from .keys import encode_key, split_action
    from . import macos
    cfg = C.load_config()
    r = Resolver(encodable=lambda k: _can(encode_key, k), desktop_encodable=lambda k: _can(macos.key_event, k))
    info = r.describe()

    def show(name: str, dest: str) -> str:
        m = dict(cfg["mapping"])
        m.update(cfg.get("profiles", {}).get(dest, {}))
        out = []
        if name.startswith("wheel_pick:"):
            return "clear, type a preset (not sent)"
        for st in split_action(m.get(name)):
            if st == "@none":
                continue
            if st.startswith("@choose:"):
                out.append("reply button 1-4" if dest == "desktop" else "-")
                continue
            if st == "@next_session":
                out.append("cmd+shift+] / [" if dest == "desktop" else "next `padpilot run`")
                continue
            if st in BUILTINS:
                out.append("(same)")
                continue
            if st.startswith("act:"):
                ks = r.resolve(st[4:].rstrip("?"), dest)
                if ks or not st.endswith("?"):
                    out.append(" ".join(ks) if ks else "-")
            elif st.startswith("text:"):
                out.append(repr(st[5:]))
            else:
                out.append(st)
        return ", ".join(out) or "-"

    def action(name: str) -> str:
        if name.startswith("wheel_pick:"):
            return "prompt wheel (8 presets)"
        steps = [s for s in split_action(cfg["mapping"].get(name)) if s != "@none"]
        if steps and steps[0].startswith("@choose:"):
            return "pick a next-step card button"
        return " + ".join(s[4:].rstrip("?") if s.startswith("act:") else BUILTINS.get(s, s) for s in steps) or "-"

    rows = [("button", "action", "Claude desktop", "Claude CLI", "Codex CLI")]
    rows += [(label, action(n), show(n, "desktop"), show(n, "claude"), show(n, "codex")) for n, label in BUTTONS]
    w = [max(len(r_[i]) for r_ in rows) for i in range(5)]
    for i, row in enumerate(rows):
        print("  ".join(c.ljust(w[j]) for j, c in enumerate(row)).rstrip())
        if i == 0:
            print("  ".join("-" * x for x in w))
    print()
    print("Claude CLI shortcuts:", info["claude_keybindings"],
          "(found, applied)" if info["claude_keybindings_found"] else "(none, defaults)")
    if info["claude_keybindings_error"]:
        print("  ignored, invalid:", info["claude_keybindings_error"])
    print("Claude desktop dictation:", info["desktop_dictation"] or "unavailable", f"({info['desktop_dictation_source']})")
    print("\"-\" = that app has no such action; pressing it flashes the lightbar red.")
    print("While a permission prompt is open, ✕ / ○ use Claude's Confirmation shortcuts.")
    print("Hold PS or R1 and press another button for the combos; their own tap fires on release.")
    return 0


def desktop() -> int:
    if sys.platform != "darwin":
        print("desktop-app control is only available on macOS")
        return 1
    if not client.start_daemon(wait=4.0):
        print("daemon not reachable")
        return 1
    # The daemon asks for itself: it is the process that posts the key presses.
    st = client.request({"op": "desktop", "prompt": True}, timeout=3.0) or {}
    if not st.get("enabled"):
        print('desktop-app control is off ("desktop": {"enabled": false} in config)')
        return 1
    print("apps:", ", ".join(st.get("apps", [])))
    print("frontmost app:", st.get("frontmost"))
    if st.get("trusted"):
        print("Accessibility permission: ok. With no `padpilot run` session open, the buttons now "
              "go to those apps while they are in front.")
        return 0
    print("Accessibility permission: MISSING.\n"
          "macOS should now show a dialog. Otherwise open System Settings > Privacy & Security > "
          "Accessibility and turn on \"Python\" (" + client.daemon_python() + ").\n"
          "Then run `padpilot stop` and `padpilot desktop` again.")
    return 1


# ---------------------------------------------------------------- codex
def _codex_hooks_path() -> Path:
    return Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex") / "hooks.json"


def _launcher() -> str:
    return str(Path(client.PKG_ROOT) / "bin" / "padpilot")


def install_codex() -> int:
    path = _codex_hooks_path()
    data = {}
    if path.exists():
        data = json.loads(path.read_text() or "{}")
        backup = path.with_suffix(".json.bak-padpilot")
        if not backup.exists():
            backup.write_text(path.read_text())
    hooks = data.setdefault("hooks", {})
    cmd = f'"{_launcher()}" hook codex'
    for ev in CODEX_EVENTS:
        groups = [g for g in hooks.get(ev, []) if not _is_ours(g)]
        entry = {"type": "command", "command": cmd, "timeout": 60 if ev == "PermissionRequest" else 5}
        groups.append({"hooks": [entry]})
        hooks[ev] = groups
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2) + "\n")
    print(f"wrote {path}. In Codex, run /hooks once to review and trust the new hooks.")
    return 0


def uninstall_codex() -> int:
    path = _codex_hooks_path()
    if not path.exists():
        print("nothing to do")
        return 0
    data = json.loads(path.read_text() or "{}")
    hooks = data.get("hooks", {})
    for ev in list(hooks):
        hooks[ev] = [g for g in hooks[ev] if not _is_ours(g)]
        if not hooks[ev]:
            del hooks[ev]
    path.write_text(json.dumps(data, indent=2) + "\n")
    print(f"removed padpilot hooks from {path}")
    return 0


def _is_ours(group: dict) -> bool:
    for h in group.get("hooks", []):
        cmd = (h.get("command") or "").replace('"', "")
        if any(m in cmd for m in (MARK, OLD_MARK)) or (("/padpilot" in cmd or "/dsagent" in cmd)
                                                        and cmd.endswith(" hook codex")):
            return True
    return False
