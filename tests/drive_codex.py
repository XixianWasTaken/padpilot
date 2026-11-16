"""Interactive check: real `codex` TUI under `padpilot run`, driven through the daemon."""
import json, os, pty, re, select, subprocess, sys, time, fcntl, termios, struct

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DS = os.path.join(ROOT, "bin", "padpilot")
WORK = "/tmp/padpilot-e2e"
L = DS
ov = []
for ev in ["SessionStart", "UserPromptSubmit", "PreToolUse", "PostToolUse", "Stop"]:
    ov += ["-c", 'hooks.%s=[{hooks=[{type="command",command="\'%s\' hook codex",timeout=5}]}]' % (ev, L)]

def ds(*a):
    return subprocess.run([DS, *a], capture_output=True, text=True).stdout.strip()
def states():
    return [(s["key"][:12], s["state"], s["last_event"]) for s in json.loads(ds("status"))["sessions"]]
def clean(b):
    return re.sub(r"\x1b\[[0-9;?<>=]*[ -/]*[@-~]|\x1b[\]P].*?(\x07|\x1b\\)|\x1b.", "", b.decode(errors="replace"))

pid, fd = pty.fork()
if pid == 0:
    os.chdir(WORK)
    os.execv(DS, [DS, "run", "codex", "--dangerously-bypass-hook-trust", *ov])
fcntl.ioctl(fd, termios.TIOCSWINSZ, struct.pack("HHHH", 40, 120, 0, 0))
buf = b""
def pump(t, until=None):
    global buf
    end = time.time() + t
    while time.time() < end:
        r, _, _ = select.select([fd], [], [], 0.1)
        if r:
            try:
                chunk = os.read(fd, 65536)
            except OSError:
                return False
            buf += chunk
            # Answer terminal queries the way a real terminal would (cursor position / DA1).
            if b"\x1b[6n" in chunk:
                os.write(fd, b"\x1b[1;1R")
            if b"\x1b[c" in chunk:
                os.write(fd, b"\x1b[?62c")
        if until and re.search(until, clean(buf[-20000:]), re.I):
            return True
    return False

results = []
def step(name, ok):
    results.append(ok); print(("PASS  " if ok else "FAIL  ") + name, flush=True)

pump(8)
mode = sys.argv[1] if len(sys.argv) > 1 else "run"
if mode == "screen":
    print(clean(buf)[-3000:]); os.kill(pid, 9); sys.exit(0)
for _ in range(5):
    sc = clean(buf[-8000:])
    if re.search(r"trust", sc, re.I) and re.search(r"1\.", sc):
        buf = b""; ds("press", "cross"); pump(5); continue
    break
step("codex wrapper attached", any(t["agent"] == "codex" for t in json.loads(ds("status"))["targets"]))
ds("send", "Reply with the single word: pong")
pump(60, r"pong")
pump(3)
st = states(); print("   states:", st)
step("codex turn completed (state done)", any(s[1] == "done" for s in st))
ds("press", "ps_hold"); pump(0.4); ds("press", "ps_hold"); pump(3)
p, _ = os.waitpid(pid, os.WNOHANG)
if p == 0:
    os.kill(pid, 9); os.waitpid(pid, 0)
step("codex exited via PS-hold", p != 0)
print("--- last screen ---"); print(clean(buf)[-1200:])
sys.exit(0 if all(results) else 1)
