"""Static file server + CORS proxy for toplivo.tbank.ru API.

Run:  python server.py  ->  http://localhost:8000
"""
import http.cookiejar
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
ALFA_HOST = "https://alfabank.ru"
OSRM_HOST = "https://router.project-osrm.org"

# alfabank fronts its API with a bot check: 307 to the same URL plus session
# cookies, and 403 for anything that doesn't look like a browser. The opener
# follows the redirect and replays the cookies; keep the jar for the process.
OPENER = urllib.request.build_opener(
    urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))
BROWSER_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/150.0.0.0 Safari/537.36")


class Handler(http.server.SimpleHTTPRequestHandler):
    def do_GET(self):
        if self.path.startswith("/api/"):
            self.proxy(API_HOST + self.path)
        elif self.path.startswith("/sber/"):
            self.proxy(SBER_HOST + self.path[len("/sber"):])
        elif self.path.startswith("/alfa/"):
            self.proxy(ALFA_HOST + self.path[len("/alfa"):])
        elif self.path.startswith("/osrm/"):
            self.proxy(OSRM_HOST + self.path[len("/osrm"):])
        else:
            super().do_GET()

    def proxy(self, url):
        alfa = url.startswith(ALFA_HOST)
        req = urllib.request.Request(url, headers={
            "User-Agent": BROWSER_UA if alfa else "Mozilla/5.0 (fuel-map local proxy)",
            "Accept": "application/json",
            # alfa's country-wide dump is 21 MB raw vs ~3 MB gzip — pass the
            # compressed bytes straight through to the browser
            "Accept-Encoding": "gzip",
        })
        try:
            with OPENER.open(req, timeout=60 if alfa else 20) as resp:
                data = resp.read()
                gzipped = resp.headers.get("Content-Encoding", "") == "gzip"
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            if gzipped:
                self.send_header("Content-Encoding", "gzip")
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
