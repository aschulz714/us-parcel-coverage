"""Tiny static server with HTTP Range support, for the parcel research map.

Python's built-in http.server ignores Range headers, but FlatGeobuf streaming depends on
them (the browser fetches only the byte ranges for parcels in the viewport). This adds
minimal single-range support and serves the output/ directory.

Usage:  python src/serve_map.py   ->  http://localhost:8008/parcel_map.html
"""
import http.server, os, re, sys

ROOT = os.path.dirname(os.path.dirname(__file__))
PORT = int(sys.argv[1]) if len(sys.argv) > 1 else 8008


class RangeHandler(http.server.SimpleHTTPRequestHandler):
    def __init__(self, *a, **kw):
        super().__init__(*a, directory=os.path.join(ROOT, "output"), **kw)

    def end_headers(self):
        # research map data (states.json/counties_status.geojson) is regenerated in place;
        # without this the browser serves a stale disk-cached copy even through Ctrl+Shift+R.
        self.send_header("Cache-Control", "no-store, no-cache, must-revalidate, max-age=0")
        super().end_headers()

    def send_head(self):
        m = re.match(r"bytes=(\d+)-(\d*)", self.headers.get("Range") or "")
        if not m:
            return super().send_head()
        path = self.translate_path(self.path)
        if not os.path.isfile(path):
            self.send_error(404)
            return None
        size = os.path.getsize(path)
        start = int(m.group(1))
        end = min(int(m.group(2)) if m.group(2) else size - 1, size - 1)
        if start >= size:
            self.send_error(416)
            return None
        f = open(path, "rb")
        f.seek(start)
        self.send_response(206)
        self.send_header("Content-Type", self.guess_type(path))
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        self.send_header("Content-Length", str(end - start + 1))
        self.end_headers()
        # SimpleHTTPRequestHandler copies the file object to the socket; wrap so only the
        # requested window is sent.
        return _Window(f, end - start + 1)


class _Window:
    def __init__(self, f, remaining):
        self.f, self.remaining = f, remaining

    def read(self, n=-1):
        if self.remaining <= 0:
            return b""
        n = self.remaining if n < 0 else min(n, self.remaining)
        data = self.f.read(n)
        self.remaining -= len(data)
        return data

    def close(self):
        self.f.close()


if __name__ == "__main__":
    with http.server.ThreadingHTTPServer(("127.0.0.1", PORT), RangeHandler) as srv:
        print(f"serving output/ at http://localhost:{PORT}/parcel_map.html  (Ctrl+C to stop)")
        srv.serve_forever()
