"""Run `dsagent run <cmd>` in a pseudo-terminal and drive it via the daemon. Prints a transcript."""
import os, pty, select, subprocess, sys, time, json

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DS = os.path.join(ROOT, "bin", "dsagent")

def ds(*args):
    return subprocess.run([DS, *args], capture_output=True, text=True).stdout.strip()

pid, fd = pty.fork()
if pid == 0:
    os.execv(DS, [DS, "run", sys.executable, os.path.join(ROOT, "tests", "fake_agent.py")])

out = b""
def pump(t):
    global out
    end = time.time() + t
    while time.time() < end:
        r, _, _ = select.select([fd], [], [], 0.05)
        if r:
            try: out += os.read(fd, 4096)
            except OSError: return

pump(1.5)
st = json.loads(ds("status"))
print("targets:", [t["agent"] for t in st["targets"]], "focus set:", st["focus"] is not None)
for c in ["up", "square", "ctrl_test", "cross"]:
    if c != "ctrl_test":
        ds("press", c); pump(0.5)
pump(1.0)
st = json.loads(ds("status"))
print("session states after Enter:", [(s["key"][:10], s["state"], s["last_event"]) for s in st["sessions"]])
ds("send", "hi"); pump(0.8)
os.write(fd, b"q"); pump(1.5)
print("transcript:")
print(out.decode(errors="replace"))
os.waitpid(pid, 0)
st = json.loads(ds("status"))
print("after exit targets:", st["targets"], "sessions:", st["sessions"])
