"""Static file server + CORS proxy for toplivo.tbank.ru API.

Run:  python server.py  ->  http://localhost:8000
"""
import http.server
import os
import socketserver
import sys
import urllib.request

def _app_dir():
    if getattr(sys, "frozen", False):  # PyInstaller exe
        exe_dir = os.path.dirname(sys.executable)
        # index.html next to the exe wins (easy to customize) …
        if os.path.exists(os.path.join(exe_dir, "index.html")):
            return exe_dir
        # … otherwise use the copy bundled inside the exe
        return sys._MEIPASS
    return os.path.dirname(os.path.abspath(__file__))


os.chdir(_app_dir())

PORT = 8000
API_HOST = "https://toplivo.tbank.ru"
SBER_HOST = "https://sberazs.ru"
OSRM_HOST = "https://router.project-osrm.org"


class Handler(http.server.SimpleHTTPRequestHandler):
    def do_GET(self):
        if self.path.startswith("/api/"):
            self.proxy(API_HOST + self.path)
        elif self.path.startswith("/sber/"):
            self.proxy(SBER_HOST + self.path[len("/sber"):])
        elif self.path.startswith("/osrm/"):
            self.proxy(OSRM_HOST + self.path[len("/osrm"):])
        else:
            super().do_GET()

    def proxy(self, url):
        req = urllib.request.Request(url, headers={
            "User-Agent": "Mozilla/5.0 (fuel-map local proxy)",
            "Accept": "application/json",
        })
        try:
            with urllib.request.urlopen(req, timeout=20) as resp:
                data = resp.read()
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(data)
        except Exception as e:
            self.send_error(502, f"Upstream error: {e}")

    def log_message(self, fmt, *args):
        pass  # keep console quiet


if __name__ == "__main__":
    socketserver.ThreadingTCPServer.allow_reuse_address = True
    with socketserver.ThreadingTCPServer(("", PORT), Handler) as httpd:
        print(f"Serving on http://localhost:{PORT}")
        httpd.serve_forever()
