#!/usr/bin/env python3
"""
pm_control.py - a tiny LAN HTTP control server (V2f) so a Stream Deck (via Bitfocus Companion's
Generic HTTP module, or a plain web-request button) can drive the mirror remotely.

The app passes a `dispatch(path, query) -> (status_int, body_dict)` callback; dispatch is responsible
for marshalling any AppKit action to the main thread. GET and POST both route the same way, so a
one-shot URL press works. LAN-only, no auth (LAN) - keep it that way; do not expose it to
the internet. Off by default; the app enables it on request.

Routes the app wires (see pm_app):
  /status                      -> current state (mirroring, paused, mode, quality, projectors, sent)
  /start /stop /pause /resume  -> mirror lifecycle
  /mode?m=hq|fast              -> picture mode
  /vdisplay?on=1|0             -> virtual display
  /blank?on=1|0                -> AV-mute (PJLink)
  /power?on=1|0  /input        -> projector power / NETWORK input (PJLink)
"""
import threading, json, socket
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs


def lan_ip():
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("192.168.1.1", 1)); ip = s.getsockname()[0]; s.close(); return ip
    except Exception:
        return "127.0.0.1"


class ControlServer:
    def __init__(self, dispatch, port=8765, host="0.0.0.0"):
        self.dispatch = dispatch
        self.port = port
        self.host = host
        self._srv = None

    def url(self):
        return "http://%s:%d" % (lan_ip(), self.port)

    def start(self):
        disp = self.dispatch

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):  # silence
                pass

            def _handle(self):
                u = urlparse(self.path)
                q = {k: v[0] for k, v in parse_qs(u.query).items()}
                try:
                    status, body = disp(u.path.rstrip("/") or "/", q)
                except Exception as e:
                    status, body = 500, {"error": str(e)}
                data = json.dumps(body).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                try: self.wfile.write(data)
                except Exception: pass

            do_GET = _handle
            do_POST = _handle

        self._srv = ThreadingHTTPServer((self.host, self.port), H)
        threading.Thread(target=self._srv.serve_forever, name="ctl-http", daemon=True).start()
        return self.url()

    def stop(self):
        if self._srv:
            try: self._srv.shutdown()
            except Exception: pass
            try: self._srv.server_close()
            except Exception: pass
            self._srv = None


if __name__ == "__main__":   # smoke: serve a status route
    import time
    srv = ControlServer(lambda p, q: (200, {"path": p, "query": q}))
    print("serving at", srv.start(), "(Ctrl-C to stop)")
    try:
        while True: time.sleep(1)
    except KeyboardInterrupt:
        srv.stop()
