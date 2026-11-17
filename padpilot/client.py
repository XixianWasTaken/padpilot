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
from typing import Dict, List, Optional, Tuple

from . import __version__
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


def daemon_alive() -> bool:
    """Is there a daemon process? It holds a lock on its pid file from start to exit, so this
    also sees one that no longer answers because it is shutting down: closing the controller
    and the tutorial page's server takes it a moment."""
    try:
        import fcntl
        fd = os.open(C.PID_PATH, os.O_RDWR)
    except (ImportError, OSError):
        return False
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        fcntl.flock(fd, fcntl.LOCK_UN)
        return False
    except OSError:
        return True
    finally:
        os.close(fd)


def wait_gone(timeout: float) -> bool:
    """Wait for a daemon that was told to stop to exit. False if it is still there after
    `timeout` seconds."""
    deadline = time.monotonic() + timeout
    while daemon_alive() or request({"op": "ping"}, timeout=0.2):
        if time.monotonic() >= deadline:
            return False
        time.sleep(0.05)
    return True


def daemon_python() -> str:
    """The daemon needs hidapi, which lives in our venv."""
    venv_py = C.VENV_PATH / "bin" / "python3"
    return str(venv_py) if venv_py.exists() else sys.executable


def _package_files() -> List[str]:
    """The files the daemon runs: the package's .py and .html files and the pages' fonts."""
    src = os.path.join(PKG_ROOT, "padpilot")
    names = sorted(n for n in os.listdir(src) if n.endswith((".py", ".html")))
    fonts = os.path.join(src, "fonts")
    if os.path.isdir(fonts):
        names += sorted("fonts/" + n for n in os.listdir(fonts) if n.endswith((".woff2", ".txt")))
    return names


def _package() -> Tuple[str, Dict[str, bytes]]:
    """(content hash, {file name: content}) of those files."""
    src = os.path.join(PKG_ROOT, "padpilot")
    h = hashlib.sha1()
    blobs = {}
    for n in _package_files():
        with open(os.path.join(src, n), "rb") as f:
            blobs[n] = f.read()
        h.update(n.encode() + b"\0" + blobs[n] + b"\0")
    return h.hexdigest()[:12], blobs


def code_id() -> str:
    """Names the code of this package by its content. The daemon reports the one it runs."""
    return _package()[0]


def _newest_mtime() -> float:
    src = os.path.join(PKG_ROOT, "padpilot")
    return max(os.stat(os.path.join(src, n)).st_mtime for n in _package_files())


def _stage_package() -> str:
    """Copy the padpilot package to $PADPILOT_HOME/runtime/<content hash>/ and return that root."""
    digest, blobs = _package()
    runtime = C.HOME / "runtime"
    root = runtime / digest
    if not (root / "padpilot" / "__init__.py").exists():
        tmp = Path(tempfile.mkdtemp(prefix=".stage-", dir=str(C.HOME)))
        (tmp / "padpilot" / "fonts").mkdir(parents=True)
        for n, data in blobs.items():
            (tmp / "padpilot" / n).write_bytes(data)
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


def _version(s) -> tuple:
    try:
        return tuple(int(x) for x in str(s).split("."))
    except ValueError:
        return ()


def _outdated(ping: dict) -> bool:
    """Does the running daemon run other, older code than this package? The daemon is started
    once and then kept, so after a plugin update it would go on running the old code.

    True when this package has a higher version, or the same version and files changed since
    the daemon started. A daemon from before it reported its code is always older. A copy that
    is neither newer nor changed never replaces a daemon (two copies in use at once must not
    take turns restarting it), and nothing is replaced while it holds a permission request for
    a button press."""
    if ping.get("approvals"):
        return False
    if "code" not in ping:
        return True
    mine, theirs = _version(__version__), _version(ping.get("version"))
    if not (mine > theirs or (mine == theirs and _newest_mtime() > float(ping.get("started") or 0))):
        return False
    return code_id() != ping.get("code")


def _importable() -> bool:
    """Can the daemon's Python import this package's daemon? A half-saved edit or a broken
    update must not take a working daemon down."""
    root = PKG_ROOT
    if sys.platform == "darwin":
        try:
            root = _stage_package()  # what would run: see _spawn_daemon
        except OSError:
            pass
    env = dict(os.environ)
    env["PYTHONPATH"] = root
    try:
        r = subprocess.run([daemon_python(), "-c", "import padpilot.daemon, padpilot.web"], env=env,
                           cwd=str(C.HOME), capture_output=True, timeout=3)
        ok, why = r.returncode == 0, r.stderr.decode(errors="replace")[-600:]
    except (OSError, subprocess.SubprocessError) as e:
        ok, why = False, repr(e)
    if not ok:
        try:
            with open(C.HOME / "daemon.stderr", "a") as f:
                f.write(f"padpilot: keeping the running daemon, the newer code does not import:\n{why}\n")
        except OSError:
            pass
    return ok


def start_daemon(wait: float = 2.0) -> bool:
    """Start the daemon in the background if it is not running, and replace one that runs
    older code than this package (see _outdated)."""
    ping = request({"op": "ping"}, timeout=0.2)
    if ping:
        try:
            stale = _outdated(ping)
        except (OSError, ValueError, TypeError):
            stale = False
        if not stale or not _importable():
            return True
        request({"op": "shutdown"}, timeout=0.5)
        # It stops answering before it has exited. A new daemon started in between finds it
        # "already running" and gives up, and then there is none at all. (Not longer than
        # this: a hook has 5 seconds. The new daemon waits a little for the lock itself.)
        wait_gone(3.0)
    _spawn_daemon()
    deadline = time.monotonic() + wait
    while time.monotonic() < deadline:
        if request({"op": "ping"}, timeout=0.2):
            return True
        time.sleep(0.05)
    return False


def _spawn_daemon() -> None:
    C.ensure_home()
    venv_py = C.VENV_PATH / "bin" / "python3"
    # First run: build the venv (installs hidapi) and then become the daemon.
    args = ["-m", "padpilot", "daemon"] if venv_py.exists() else ["-m", "padpilot", "setup", "--quiet", "--then-daemon"]
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
