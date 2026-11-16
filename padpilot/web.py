"""The tutorial page, served by the daemon on 127.0.0.1.

While the page is open it is a practice-mode watcher: button presses stream to it (Server-Sent
Events) and nothing is sent to any app. The controller stays opened exclusively, so macOS never
sees the PS button (no Games app) and the page can read the mic button, which browsers cannot.
Closing the tab ends practice mode; no terminal has to stay open.

Only pages from this server may use it: the Host header must be 127.0.0.1/localhost (no DNS
rebinding) and a request's Origin, when sent, must be this server's own.
"""
from __future__ import annotations

import json
import logging
import os
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Optional

log = logging.getLogger("padpilot")

PAGE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "tutorial.html")
HEARTBEAT_S = 1.0
PREVIEW_MAX_S = 3600.0


def _make_handler(d, port: int):
    from .daemon import Target

    hosts = {f"127.0.0.1:{port}", f"localhost:{port}"}
    origins = {"http://" + h for h in hosts}

    class SseTarget(Target):
        """A practice watcher whose messages are Server-Sent Events."""
        practice_mode = True

        def _encode(self, msg: Optional[dict]) -> bytes:
            if msg is None:
                return b": ping\n\n"
            return b"data: " + json.dumps(msg).encode() + b"\n\n"

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"
        server_version = "padpilot"

        def log_message(self, fmt, *args):  # the daemon log has enough
            pass

        def _allowed(self, need_origin_check: bool) -> bool:
            if self.headers.get("Host", "") not in hosts:
                return False
            origin = self.headers.get("Origin")
            if need_origin_check and origin is not None and origin not in origins:
                return False
            return True

        def _send(self, code: int, body: bytes, ctype: str = "application/json") -> None:
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            path = self.path.split("?", 1)[0]
            if not self._allowed(path == "/events"):
                return self._send(403, b'{"error":"forbidden"}')
            if path in ("/", "/tutorial", "/tutorial.html"):
                try:
                    with open(PAGE, "rb") as f:
                        body = f.read()
                except OSError:
                    return self._send(404, b'{"error":"tutorial.html missing"}')
                if not body.lstrip()[:9].lower().startswith(b"<!doctype"):
                    body = b"<!doctype html>\n" + body  # standards mode
                return self._send(200, body, "text/html; charset=utf-8")
            if path == "/events":
                return self._events()
            return self._send(404, b'{"error":"not found"}')

        def do_POST(self):
            path = self.path.split("?", 1)[0]
            if not self._allowed(True) or self.headers.get("Origin") is None:
                return self._send(403, b'{"error":"forbidden"}')
            try:
                n = min(int(self.headers.get("Content-Length") or 0), 4096)
                msg = json.loads(self.rfile.read(n) or b"{}")
                if not isinstance(msg, dict):
                    raise ValueError
            except (ValueError, OSError):
                return self._send(400, b'{"error":"bad request"}')
            if path == "/rumble":
                ok = d.web_rumble(str(msg.get("pattern", "")))
            elif path == "/light":
                ok = d.web_light(str(msg.get("state", "")))
            else:
                return self._send(404, b'{"error":"not found"}')
            return self._send(200 if ok else 400, json.dumps({"ok": ok}).encode())

        def _events(self):
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Connection", "close")
            self.end_headers()
            self.close_connection = True
            w = SseTarget("web", "", "", self.connection)
            with d.lock:
                d.add_watcher(w, practice=True)
                hello = {"op": "hello", "controller": d.dev is not None, "practice": True}
            try:
                if not w.send(hello):
                    return
                while d.running and w.send(None):
                    with d.lock:
                        if w not in d.watchers:  # dropped by a failed send meanwhile
                            break
                    time.sleep(HEARTBEAT_S)
            finally:
                with d.lock:
                    d.remove_watcher(w)

    return Handler


def start(d, port: int) -> Optional[ThreadingHTTPServer]:
    """Serve the page on 127.0.0.1:port in a background thread; None if the port is taken."""
    if not port:
        return None
    try:
        srv = ThreadingHTTPServer(("127.0.0.1", port), _make_handler(d, port))
    except OSError as e:
        log.warning("tutorial page not served: port %s: %s", port, e)
        return None
    srv.daemon_threads = True
    threading.Thread(target=srv.serve_forever, kwargs={"poll_interval": 0.5},
                     name="web", daemon=True).start()
    log.info("tutorial page on http://127.0.0.1:%s/", port)
    return srv
