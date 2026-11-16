"""Interactive end-to-end test: real `claude` under `dsagent run`, driven only through the daemon."""
import json, os, pty, re, select, subprocess, sys, time, fcntl, termios, struct

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DS = os.path.join(ROOT, "bin", "dsagent")
WORK = "/tmp/dsagent-e2e"
KITTY = "kitty" in sys.argv
seen_push = []
os.makedirs(WORK, exist_ok=True)
proof = os.path.join(WORK, "dsagent-proof.txt")
if os.path.exists(proof):
    os.remove(proof)

def ds(*a):
    return subprocess.run([DS, *a], capture_output=True, text=True).stdout.strip()

def states():
    st = json.loads(ds("status"))
    return [(s["key"][:12], s["state"], s["last_event"]) for s in st["sessions"]]

pid, fd = pty.fork()
if pid == 0:
    os.chdir(WORK)
    for k in [k for k in os.environ if k.startswith("CLAUDE_CODE_") or k == "CLAUDECODE"]:
        os.environ.pop(k)
    os.execv(DS, [DS, "run", "claude", "--plugin-dir", ROOT, "--model", "claude-haiku-5-5", "--permission-mode", "default"])
fcntl.ioctl(fd, termios.TIOCSWINSZ, struct.pack("HHHH", 40, 120, 0, 0))
buf = b""
def pump(t, until=None):
    global buf
    end = time.time() + t
    while time.time() < end:
        r, _, _ = select.select([fd], [], [], 0.1)
        if r:
            try: chunk = os.read(fd, 65536)
            except OSError: return False
            buf += chunk
            if KITTY and b"\x1b[?u" in chunk:
                os.write(fd, b"\x1b[?0u")  # behave like a kitty-protocol terminal
            if KITTY and b"\x1b[c" in chunk:
                os.write(fd, b"\x1b[?62;22c")
            if KITTY and re.search(rb"\x1b\[>\d+u", chunk):
                seen_push.append(True)
        if until and re.search(until, clean(buf[-20000:])):
            return True
    return False
def clean(b):
    return re.sub(r"\x1b\[[0-9;?]*[ -/]*[@-~]|\x1b[\]P].*?(\x07|\x1b\\)|\x1b.", "", b.decode(errors="replace"))

log = []
def step(name, ok):
    log.append((name, ok)); print(f"{'PASS' if ok else 'FAIL'}  {name}", flush=True)

PROMPT = r'for ?shortcuts|Try"|shift\+tab ?to ?cycle'
pump(6)
for _ in range(6):
    screen = clean(buf[-6000:])
    if re.search(r"trust ?this ?folder", screen, re.I) and "trusted" not in log:
        buf = b""; log.append(("trusted", True))
        ds("press", "down"); pump(0.8); ds("press", "cross"); pump(5); continue
    if re.search(r"browser ?tools", screen, re.I) and "browser" not in log:
        buf = b""; log.append(("browser", True))
        ds("press", "circle"); pump(4); continue
    if re.search(PROMPT, screen):
        break
    pump(3)
log[:] = [x for x in log if x[0] not in ("trusted", "browser")]
step("reached the prompt using only controller presses", bool(re.search(PROMPT, clean(buf[-6000:]))))
step("wrapper attached", any(t["agent"] == "claude" for t in json.loads(ds("status"))["targets"]))
ds("send", "Use the Bash tool to run exactly this command: touch dsagent-proof.txt   Then reply with the single word: finished")
got = pump(60, r"Do you want to proceed|Allow|proceed\?")
step("permission dialog shown", got)
time.sleep(0.5)
st = states(); print("   states:", st)
step("daemon state = permission", any(s[1] == "permission" for s in st))
ds("press", "r2_full")
pump(60, r"finished")
pump(3)
step("file created after R2 approval", os.path.exists(proof))
st = states(); print("   states:", st)
step("daemon state = done", any(s[1] == "done" for s in st))
if KITTY:
    step("claude enabled the kitty keyboard protocol", bool(seen_push))
buf = b""
ds("send", "Write the numbers from 1 to 300, one per line, with a short fun fact about each.")
pump(25, r"Interrupt|esc ?to ?interrupt")
pump(1.5); n0 = len(buf); pump(3)
print("   output bytes in 3s while running:", len(buf) - n0)
buf = b""
ds("press", "circle")
# Newer Claude Code puts the prompt back into the input box instead of printing "Interrupted",
# so check that the spinner stopped: a running turn redraws the screen continuously.
pump(2.5); n0 = len(buf); pump(3); quiet_bytes = len(buf) - n0
print("   output bytes in 3s after Esc:", quiet_bytes)
ok_int = quiet_bytes < 300 and "esctointerrupt" not in clean(buf[n0:]).replace(" ", "")
step("✕/○: circle (Esc) interrupted the running turn", ok_int)
if not ok_int:
    print("   screen:", clean(buf)[-700:].replace("\n", " "))
st = states(); print("   states:", st)
step("daemon state = idle after interrupt", any(s[1] == "idle" for s in st))

LONG = "Write the numbers from 1 to 300, one per line, with a short fun fact about each."
def last_user_text():
    import glob
    files = glob.glob(os.path.expanduser("~/.claude/projects/*dsagent-e2e*/*.jsonl"))
    if not files:
        return None
    last = None
    for line in open(max(files, key=os.path.getmtime)):
        try:
            m = json.loads(line)
        except ValueError:
            continue
        c = (m.get("message") or {}).get("content") if m.get("type") == "user" else None
        if isinstance(c, list):
            c = " ".join(x.get("text", "") for x in c if isinstance(x, dict) and x.get("type") == "text")
        if isinstance(c, str) and c.strip() and not c.startswith("<") and "interrupted" not in c.lower():
            last = c
    return last

buf = b""
ds("press", "r3")  # retry the interrupted prompt
got = False
for _ in range(50):  # wait for the hook to report the new turn
    pump(0.5)
    st = states()
    if any(s[0].startswith("w-") and s[1] in ("thinking", "tool") for s in st):
        got = True
        break
print("   states:", st)
step("R3 re-sent the interrupted prompt (turn running again)",
     got and any(s[0].startswith("w-") and s[1] in ("thinking", "tool") for s in st))
pump(2)
lu = last_user_text(); print("   last user message:", repr(lu))
step("retried message is the prompt exactly once (input cleared first)", lu == LONG)
ds("press", "circle"); pump(4)
buf = b""
ds("press", "create_hold")  # new session: /clear
pump(8)
st = json.loads(ds("status"))["sessions"]; print("   sessions:", [(s["state"], s["last_event"]) for s in st])
step("Create (hold) started a new conversation (/clear -> SessionStart)",
     any(s["key"].startswith("w-") and s["last_event"] == "SessionStart" for s in st))
ds("press", "ps_hold"); pump(0.4)
ds("press", "ps_hold"); pump(3)
try:
    p, status = os.waitpid(pid, os.WNOHANG)
except ChildProcessError:
    p = pid
if p == 0:
    os.kill(pid, 9); os.waitpid(pid, 0)
    step("claude exited via PS-hold (Ctrl+C x2)", False)
else:
    step("claude exited via PS-hold (Ctrl+C x2)", True)
print("--- last screen ---")
print(clean(buf[-2500:])[-1500:])
sys.exit(0 if all(ok for _, ok in log) else 1)
