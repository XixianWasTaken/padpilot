"""Talking to the daemon, and starting it on demand."""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Optional

from . import config as C

PKG_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def connect(timeout: float = 0.3) -> Optional[socket.socket]:
    try:
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.settimeout(timeout)
        s.connect(str(C.SOCKET_PATH))
        return s
    except OSError:
        return None


def request(msg: dict, timeout: float = 0.5, wait_reply: bool = True) -> Optional[dict]:
    s = connect(timeout)
    if s is None:
        return None
    try:
        s.settimeout(timeout)
        s.sendall((json.dumps(msg) + "\n").encode())
        if not wait_reply:
            return {"ok": True}
        buf = b""
        while not buf.endswith(b"\n"):
            chunk = s.recv(65536)
            if not chunk:
                break
            buf += chunk
        return json.loads(buf) if buf.strip() else None
    except (OSError, ValueError):
        return None
    finally:
        s.close()


def daemon_python() -> str:
    """The daemon needs hidapi, which lives in our venv."""
    venv_py = C.VENV_PATH / "bin" / "python3"
    return str(venv_py) if venv_py.exists() else sys.executable


def _stage_package() -> str:
    """Copy the dsagent package to $DSAGENT_HOME/runtime/<content hash>/ and return that root."""
    src = os.path.join(PKG_ROOT, "dsagent")
    names = sorted(n for n in os.listdir(src) if n.endswith((".py", ".html")))
    h = hashlib.sha1()
    blobs = {}
    for n in names:
        with open(os.path.join(src, n), "rb") as f:
            blobs[n] = f.read()
        h.update(n.encode() + b"\0" + blobs[n] + b"\0")
    runtime = C.HOME / "runtime"
    root = runtime / h.hexdigest()[:12]
    if not (root / "dsagent" / "__init__.py").exists():
        tmp = Path(tempfile.mkdtemp(prefix=".stage-", dir=str(C.HOME)))
        (tmp / "dsagent").mkdir()
        for n, data in blobs.items():
            (tmp / "dsagent" / n).write_bytes(data)
        runtime.mkdir(exist_ok=True)
        try:
            os.rename(tmp, root)
        except OSError:  # another client staged the same version first
            shutil.rmtree(tmp, ignore_errors=True)
    # Old versions go after an hour; the running daemon touches its own dir every 10 minutes.
    try:
        for old in runtime.iterdir():
            try:
                stale = old != root and time.time() - old.stat().st_mtime > 3600
            except OSError:  # removed by another client meanwhile
                continue
            if stale:
                shutil.rmtree(old, ignore_errors=True)
    except OSError:
        pass
    return str(root)


def start_daemon(wait: float = 2.0) -> bool:
    """Start the daemon in the background if it is not already running."""
    if request({"op": "ping"}, timeout=0.2):
        return True
    C.ensure_home()
    venv_py = C.VENV_PATH / "bin" / "python3"
    # First run: build the venv (installs hidapi) and then become the daemon.
    args = ["-m", "dsagent", "daemon"] if venv_py.exists() else ["-m", "dsagent", "setup", "--quiet", "--then-daemon"]
    env = dict(os.environ)
    env["PYTHONPATH"] = PKG_ROOT + os.pathsep + env.get("PYTHONPATH", "")
    spawned = False
    if sys.platform == "darwin":
        # Own "responsible process", so the Accessibility grant for desktop-app control
        # belongs to Python.app whoever started the daemon. Python.app on its own may not
        # read ~/Desktop or ~/Documents (privacy protection), so run a staged copy of the code.
        from .macos import spawn_disclaimed
        try:
            env["PYTHONPATH"] = _stage_package()
            spawn_disclaimed([daemon_python()] + args, env, str(C.HOME), str(C.HOME / "daemon.stderr"))
            spawned = True
        except OSError:
            env["PYTHONPATH"] = PKG_ROOT + os.pathsep + os.environ.get("PYTHONPATH", "")
    if not spawned:
        with open(os.devnull, "rb") as devnull_in, open(C.HOME / "daemon.stderr", "ab") as err:
            subprocess.Popen(
                [daemon_python()] + args,
                stdin=devnull_in, stdout=err, stderr=err, env=env, cwd=str(C.HOME),
                start_new_session=True, close_fds=True,
            )
    deadline = time.monotonic() + wait
    while time.monotonic() < deadline:
        if request({"op": "ping"}, timeout=0.2):
            return True
        time.sleep(0.05)
    return False
