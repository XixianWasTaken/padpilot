"""`padpilot run <cmd...>`: run an agent CLI inside a PTY and inject controller keystrokes.

The terminal is passed through untouched. The child gets PADPILOT_WRAPPER_ID so its hooks
report to the right session.
"""
from __future__ import annotations

import errno
import fcntl
import json
import os
import pty
import re
import select
import signal
import struct
import sys
import termios
import time
import tty
import uuid
from typing import List, Optional, Tuple

from . import client
from .keys import encode_key

RECONNECT_S = 3.0
KITTY_RE = re.compile(rb"\x1b\[([<>=])(\d*)(?:;\d+)?u")
MODE_TAIL = 16  # longer than any sequence tracked above


def _winsize(fd: int) -> Optional[bytes]:
    try:
        return fcntl.ioctl(fd, termios.TIOCGWINSZ, b"\0" * 8)
    except OSError:
        return None


def _write_all(fd: int, data: bytes) -> None:
    while data:
        try:
            n = os.write(fd, data)
            data = data[n:]
        except InterruptedError:
            continue
        except BlockingIOError:
            select.select([], [fd], [], 1.0)


def _reap(pid: int) -> int:
    """Wait for the child (a few seconds, then SIGTERM, then SIGKILL); its shell-style exit code."""
    for sig in (None, signal.SIGTERM, signal.SIGKILL):
        if sig is not None:
            try:
                os.kill(pid, sig)
            except OSError:
                pass
        deadline = time.monotonic() + 3.0
        while time.monotonic() < deadline:
            try:
                done, status = os.waitpid(pid, os.WNOHANG)
            except ChildProcessError:
                return 0
            if done:
                if os.WIFSIGNALED(status):
                    return 128 + os.WTERMSIG(status)
                return os.WEXITSTATUS(status)
            time.sleep(0.05)
    return 1


def _classify_local(data: bytes) -> List[str]:
    """Rough classification of keyboard input so the daemon can follow along."""
    if data == b"\x1b":
        return ["esc"]
    if data in (b"\r", b"\n"):
        return ["enter"]
    return ["other"]


class Wrapper:
    def __init__(self, argv: List[str]):
        self.argv = argv
        self.agent = os.path.basename(argv[0])
        self.id = f"w-{self.agent}-{uuid.uuid4().hex[:8]}"
        self.label = os.path.basename(os.getcwd())
        self.sock = None
        self.sock_buf = b""
        self.last_connect_try = 0.0
        self.app_cursor = False
        self.mode_tail = b""
        self.kitty: List[int] = []  # kitty keyboard protocol flag stack
        self.queue: List[Tuple[float, bytes]] = []  # (due time, bytes) for sequenced keys
        self.last_local_report = 0.0
        self.master = -1
        self.to_child = bytearray()  # input not yet taken by the child (the master is non-blocking)

    # ---------------------------------------------------------------- daemon link
    def _connect(self, start: bool = False) -> None:
        """Attach to the daemon. Only the first attempt may start it (that can block ~1.5s)."""
        self.last_connect_try = time.monotonic()
        if start and not client.start_daemon(wait=1.5):
            return
        s = client.connect(timeout=0.2)
        if s is None:
            return
        try:
            s.sendall((json.dumps({"op": "attach", "wrapper": self.id, "agent": self.agent,
                                   "label": self.label}) + "\n").encode())
            s.setblocking(False)
            self.sock = s
        except OSError:
            s.close()

    def _drop(self) -> None:
        if self.sock is not None:
            try:
                self.sock.close()
            except OSError:
                pass
        self.sock = None

    def _send(self, msg: dict) -> None:
        if self.sock is None:
            return
        try:
            self.sock.sendall((json.dumps(msg) + "\n").encode())
        except OSError:
            self._drop()

    def _on_daemon(self) -> None:
        try:
            chunk = self.sock.recv(65536)
        except BlockingIOError:
            return
        except OSError:
            chunk = b""
        if not chunk:
            self._drop()
            return
        self.sock_buf += chunk
        while b"\n" in self.sock_buf:
            line, self.sock_buf = self.sock_buf.split(b"\n", 1)
            try:
                msg = json.loads(line)
            except ValueError:
                continue
            if msg.get("op") == "type":
                self._schedule(msg.get("items") or [], float(msg.get("gap_ms", 90)) / 1000.0)

    def _schedule(self, items: List[dict], gap: float) -> None:
        t = max([time.monotonic()] + [due for due, _ in self.queue])
        for i, it in enumerate(items):
            if "text" in it:
                data = it["text"].encode()
            else:
                try:
                    data = encode_key(it.get("key", ""), self.app_cursor,
                                      self.kitty[-1] if self.kitty else 0).encode()
                except KeyError:
                    continue
            self.queue.append((t, data))
            t += gap

    def _flush_queue(self) -> None:
        now = time.monotonic()
        while self.queue and self.queue[0][0] <= now:
            _, data = self.queue.pop(0)
            self._write_child(data)

    # ---------------------------------------------------------------- child io
    def _write_child(self, data: bytes = b"") -> None:
        """Queue input for the child and write what it takes now. Never blocks: a big paste
        or a long retried prompt must not stop us reading the child's output (both would wait
        on each other); the rest goes out when the master is writable again."""
        self.to_child += data
        while self.to_child:
            try:
                n = os.write(self.master, bytes(self.to_child[:65536]))
            except InterruptedError:
                continue
            except BlockingIOError:
                return
            except OSError:
                self.to_child.clear()
                return
            del self.to_child[:n]

    def _track_modes(self, out: bytes) -> None:
        # The end of the previous chunk is scanned again, so a sequence split across two reads
        # is still seen; a sequence that ended inside that carried part was handled last time.
        carried = len(self.mode_tail)
        buf = self.mode_tail + out
        self.mode_tail = buf[-MODE_TAIL:]
        # DECCKM decides whether arrows are sent as ESC[A or ESC O A.
        on, off = buf.rfind(b"\x1b[?1h"), buf.rfind(b"\x1b[?1l")
        if max(on, off) + 5 > carried:
            self.app_cursor = on > off
        # Kitty keyboard protocol: push (>N u), pop (<N u), set (=N;M u).
        if b"u" in buf and (b"\x1b[>" in buf or b"\x1b[<" in buf or b"\x1b[=" in buf):
            for m in KITTY_RE.finditer(buf):
                if m.end() <= carried:
                    continue
                op, num = m.group(1), m.group(2)
                n = int(num) if num else (1 if op != b"=" else 0)
                if op == b">":
                    self.kitty.append(n)
                    del self.kitty[:-16]
                elif op == b"<":
                    del self.kitty[len(self.kitty) - min(n, len(self.kitty)):]
                elif self.kitty:
                    self.kitty[-1] = n
                else:
                    self.kitty.append(n)

    def run(self) -> int:
        stdin_fd = sys.stdin.fileno()
        stdout_fd = sys.stdout.fileno()
        is_tty = os.isatty(stdin_fd)
        env_id = self.id

        pid, master = pty.fork()
        if pid == 0:  # child
            os.environ["PADPILOT_WRAPPER_ID"] = env_id
            os.environ["PADPILOT_AGENT"] = self.agent
            try:
                os.execvp(self.argv[0], self.argv)
            except OSError as e:
                sys.stderr.write(f"padpilot: cannot run {self.argv[0]}: {e}\n")
                os._exit(127)
        self.master = master

        def sync_size(*_):
            ws = _winsize(stdin_fd) if is_tty else None
            if ws:
                try:
                    fcntl.ioctl(master, termios.TIOCSWINSZ, ws)
                except OSError:
                    pass
                try:
                    os.kill(pid, signal.SIGWINCH)
                except OSError:
                    pass
        sync_size()
        signal.signal(signal.SIGWINCH, sync_size)

        def on_term(signum, _frame):
            raise SystemExit(128 + signum)  # run the finally below: restore the terminal

        signal.signal(signal.SIGTERM, on_term)
        signal.signal(signal.SIGHUP, on_term)
        os.set_blocking(master, False)
        old_attrs = None
        exit_code = 0
        try:
            if is_tty:
                old_attrs = termios.tcgetattr(stdin_fd)
                tty.setraw(stdin_fd)
            self._connect(start=True)
            stdin_open = True
            while True:
                fds = [master]
                if stdin_open:
                    fds.append(stdin_fd)
                if self.sock is not None:
                    fds.append(self.sock)
                timeout = 0.5
                if self.queue:
                    timeout = max(0.0, min(timeout, self.queue[0][0] - time.monotonic()))
                try:
                    r, w, _ = select.select(fds, [master] if self.to_child else [], [], timeout)
                except InterruptedError:
                    continue
                except OSError as e:
                    if e.errno == errno.EINTR:
                        continue
                    raise
                if master in w:
                    self._write_child()
                if master in r:
                    try:
                        out = os.read(master, 65536)
                    except BlockingIOError:
                        continue
                    except OSError:
                        out = b""
                    if not out:
                        break
                    self._track_modes(out)
                    _write_all(stdout_fd, out)
                if stdin_open and stdin_fd in r:
                    data = os.read(stdin_fd, 65536)
                    if not data:
                        stdin_open = False
                        if not is_tty:  # piped input ended: pass the end of file on (^D)
                            self._write_child(b"\x04")
                    else:
                        self._write_child(data)
                        now = time.monotonic()
                        kinds = _classify_local(data)
                        if kinds != ["other"] or now - self.last_local_report > 1.0:
                            self.last_local_report = now
                            self._send({"op": "local", "keys": kinds})
                if self.sock is not None and self.sock in r:
                    self._on_daemon()
                self._flush_queue()
                if self.sock is None and time.monotonic() - self.last_connect_try > RECONNECT_S:
                    self._connect()
        finally:
            if old_attrs is not None:
                termios.tcsetattr(stdin_fd, termios.TCSAFLUSH, old_attrs)
            self._drop()
            # Close the master first: the child gets a hangup instead of blocking on a full
            # terminal, so waiting for it cannot hang.
            os.close(master)
            exit_code = _reap(pid) if exit_code == 0 else exit_code
        return exit_code


def run_wrapped(argv: List[str]) -> int:
    if not argv:
        sys.stderr.write("usage: padpilot run <command> [args...]   e.g. padpilot run claude\n")
        return 2
    return Wrapper(argv).run()
