"""A stand-in agent: echoes raw input bytes and fires hooks like Claude Code would."""
import json, os, subprocess, sys, termios, tty

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

def hook(event, **extra):
    p = dict(hook_event_name=event, session_id="fake-1", cwd=os.getcwd(), **extra)
    subprocess.run([os.path.join(ROOT, "bin", "padpilot"), "hook", "claude"], input=json.dumps(p).encode())

tty.setraw(0)
sys.stdout.write("\x1b[?1h")  # application cursor mode, like many TUIs
sys.stdout.write("READY wrapper=%s\r\n" % os.environ.get("PADPILOT_WRAPPER_ID")); sys.stdout.flush()
hook("SessionStart", source="startup")
while True:
    b = os.read(0, 1024)
    sys.stdout.write("GOT %r\r\n" % b); sys.stdout.flush()
    if b == b"\r":
        hook("UserPromptSubmit", prompt="x")
        hook("PermissionRequest", tool_name="Bash")
    if b == b"q":
        hook("Stop")
        break
