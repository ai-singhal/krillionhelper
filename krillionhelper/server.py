"""Local web page plus Server-Sent Events, on the standard library only."""

import json
import queue
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

STATIC_DIR = Path(__file__).resolve().parent / "static"
MAX_BODY = 4096


class Broadcaster:
    def __init__(self):
        self.lock = threading.Lock()
        self.clients = set()

    def subscribe(self):
        q = queue.Queue(maxsize=200)
        with self.lock:
            self.clients.add(q)
        return q

    def unsubscribe(self, q):
        with self.lock:
            self.clients.discard(q)

    def publish(self, data):
        with self.lock:
            clients = list(self.clients)
        for q in clients:
            try:
                q.put_nowait(data)
            except queue.Full:
                pass  # a stalled tab; it gets the full state on its next event


def makeHandler(app, broadcaster):
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *args):
            pass

        def do_GET(self):
            if self.path in ("/", "/index.html"):
                self._send(200, "text/html; charset=utf-8", (STATIC_DIR / "index.html").read_bytes())
            elif self.path.startswith("/api/snapshot.png"):
                if app.screenSnapshot:
                    self._send(200, "image/png", app.screenSnapshot)
                else:
                    self._send(404, "text/plain", b"no snapshot")
            elif self.path == "/api/state":
                self._send(200, "application/json", app.snapshot().encode())
            elif self.path == "/events":
                self._events()
            else:
                self._send(404, "text/plain", b"not found")

        def do_POST(self):
            length = int(self.headers.get("Content-Length") or 0)
            if length > MAX_BODY:
                return self._send(413, "text/plain", b"too large")
            try:
                body = json.loads(self.rfile.read(length) or b"{}")
            except json.JSONDecodeError:
                return self._send(400, "text/plain", b"bad json")
            if self.path == "/api/prompt":
                app.submitPrompt(str(body.get("prompt", ""))[:300], "manual", force=bool(body.get("force")))
            elif self.path == "/api/regenerate":
                app.regenerate()
            elif self.path == "/api/feedback":
                app.recordFeedback(str(body.get("answer", ""))[:80], str(body.get("verdict", "")))
            elif self.path == "/api/used":
                app.recordMyAnswer(str(body.get("answer", ""))[:80])
            elif self.path == "/api/snapshot":
                # The delay gives time to switch to the game before the screen is captured.
                time.sleep(min(max(float(body.get("delayMs", 0)), 0), 10000) / 1000)
                return self._send(200, "application/json", json.dumps(app.takeSnapshot()).encode())
            elif self.path == "/api/region":
                try:
                    app.setRegion(body, "game" if body.get("kind") == "game" else "prompt")
                except (KeyError, TypeError, ValueError) as e:
                    return self._send(400, "text/plain", str(e).encode())
            else:
                return self._send(404, "text/plain", b"not found")
            self._send(200, "application/json", b'{"ok":true}')

        def _send(self, code, contentType, body):
            self.send_response(code)
            self.send_header("Content-Type", contentType)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def _events(self):
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Connection", "keep-alive")
            self.end_headers()
            q = broadcaster.subscribe()
            try:
                self._write(app.snapshot())
                while not app.stopEvent.is_set():
                    try:
                        data = q.get(timeout=15)
                    except queue.Empty:
                        self.wfile.write(b": ping\n\n")
                        self.wfile.flush()
                        continue
                    while not q.empty():  # skip straight to the newest state
                        data = q.get_nowait()
                    self._write(data)
            except (BrokenPipeError, ConnectionResetError, TimeoutError):
                pass
            finally:
                broadcaster.unsubscribe(q)
                self.close_connection = True

        def _write(self, data):
            self.wfile.write(b"data: " + data.encode() + b"\n\n")
            self.wfile.flush()

    return Handler


def createServer(app, broadcaster, port):
    server = ThreadingHTTPServer(("127.0.0.1", port), makeHandler(app, broadcaster))
    server.daemon_threads = True
    return server
