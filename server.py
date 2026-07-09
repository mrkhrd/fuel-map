"""Static file server + CORS proxy for toplivo.tbank.ru API.

Run:  python server.py  ->  http://localhost:8000
"""
import http.server
import os
import socketserver
import sys
import urllib.request

# serve files from the app dir (next to the exe when frozen by PyInstaller)
_base = sys.executable if getattr(sys, "frozen", False) else os.path.abspath(__file__)
os.chdir(os.path.dirname(_base))

PORT = 8000
API_HOST = "https://toplivo.tbank.ru"
OSRM_HOST = "https://router.project-osrm.org"


class Handler(http.server.SimpleHTTPRequestHandler):
    def do_GET(self):
        if self.path.startswith("/api/"):
            self.proxy(API_HOST + self.path)
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
