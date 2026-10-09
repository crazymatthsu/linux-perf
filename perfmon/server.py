"""Read-only web dashboard: serves the UI and tails the run CSV files.

The browser asks for each CSV from a byte offset, so live updates only move
the new rows. Nothing here writes to disk or runs commands.
"""

import base64
import gzip
import hmac
import json
import os
import re
import socket
import socketserver
import sys
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import parse_qs, unquote, urlparse

from .recorder import FILES

WEB = os.path.join(os.path.dirname(os.path.abspath(__file__)), "web")
STATIC = {"app.js": "application/javascript", "chart.js": "application/javascript",
          "style.css": "text/css"}
RUN_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
MAX_CHUNK = 8 * 1024 * 1024


def run_is_live(run_dir, meta):
    if meta.get("ended"):
        return False
    newest = 0
    for f in ("host.csv", "processes.csv", "containers.csv"):
        try:
            newest = max(newest, os.path.getmtime(os.path.join(run_dir, f)))
        except OSError:
            pass
    return time.time() - newest < max(15.0, 5 * float(meta.get("interval") or 1))


def read_meta(run_dir):
    with open(os.path.join(run_dir, "meta.json")) as f:
        meta = json.load(f)
    meta["live"] = run_is_live(run_dir, meta)
    return meta


def list_runs(base):
    out = []
    if not os.path.isdir(base):
        return out
    for d in os.listdir(base):
        path = os.path.join(base, d)
        if d == "latest" or not RUN_ID.match(d) or os.path.islink(path):
            continue
        try:
            m = read_meta(path)
        except (OSError, ValueError):
            continue
        out.append({"id": d, "name": m.get("name", d), "started": m.get("started"),
                    "ended": m.get("ended"), "live": m["live"], "host": m.get("host"),
                    "interval": m.get("interval")})
    out.sort(key=lambda r: r["started"] or 0, reverse=True)
    return out


class _Server(socketserver.ThreadingMixIn, HTTPServer):
    daemon_threads = True
    allow_reuse_address = True


class Handler(BaseHTTPRequestHandler):
    server_version = "perfmon"
    sys_version = ""
    data_dir = None
    auth = None

    def log_message(self, fmt, *args):
        if args and str(args[1])[:1] in ("4", "5"):
            sys.stderr.write("%s %s\n" % (self.address_string(), fmt % args))

    def _send(self, code, body, ctype="text/plain; charset=utf-8", headers=None):
        if isinstance(body, str):
            body = body.encode("utf-8")
        if len(body) > 4096 and "gzip" in self.headers.get("Accept-Encoding", ""):
            body = gzip.compress(body, 5)
            headers = dict(headers or {}, **{"Content-Encoding": "gzip"})
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _authorized(self):
        if not self.auth:
            return True
        h = self.headers.get("Authorization", "")
        if h.startswith("Basic "):
            try:
                given = base64.b64decode(h[6:]).decode("utf-8")
            except (ValueError, UnicodeDecodeError):
                given = ""
            if hmac.compare_digest(given, self.auth):
                return True
        self.send_response(401)
        self.send_header("WWW-Authenticate", 'Basic realm="perfmon"')
        self.send_header("Content-Length", "0")
        self.end_headers()
        return False

    def do_HEAD(self):
        self.do_GET()

    def do_GET(self):
        if not self._authorized():
            return
        url = urlparse(self.path)
        path = unquote(url.path)
        query = parse_qs(url.query)
        try:
            if path in ("/", "/index.html"):
                with open(os.path.join(WEB, "index.html"), "rb") as f:
                    return self._send(200, f.read(), "text/html; charset=utf-8")
            if path.startswith("/static/"):
                name = path[len("/static/"):]
                if name in STATIC:
                    with open(os.path.join(WEB, name), "rb") as f:
                        return self._send(200, f.read(), STATIC[name] + "; charset=utf-8")
            if path == "/api/runs":
                return self._json(list_runs(self.data_dir))
            m = re.match(r"^/api/runs/([^/]+)/([a-z]+)\.(json|csv|html)$", path)
            if m and RUN_ID.match(m.group(1)):
                run_dir = os.path.join(self.data_dir, m.group(1))
                if os.path.isdir(run_dir):
                    return self._run_file(run_dir, m.group(2), m.group(3), query)
            return self._send(404, "not found")
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _json(self, obj):
        self._send(200, json.dumps(obj), "application/json")

    def _run_file(self, run_dir, name, ext, query):
        if ext == "json" and name == "meta":
            return self._json(read_meta(run_dir))
        if ext == "html" and name == "report":
            from .report import build_report_html
            html = build_report_html(run_dir)
            return self._send(200, html, "text/html; charset=utf-8", {
                "Content-Disposition": 'attachment; filename="%s_report.html"' % os.path.basename(run_dir)})
        if ext != "csv" or name not in FILES:
            return self._send(404, "not found")
        path = os.path.join(run_dir, name + ".csv")
        if not os.path.exists(path):
            return self._send(404, "no %s.csv in this run" % name)
        size = os.path.getsize(path)
        if "offset" not in query:   # plain download of the whole file
            with open(path, "rb") as f:
                return self._send(200, f.read(), "text/csv; charset=utf-8", {
                    "Content-Disposition": 'attachment; filename="%s_%s.csv"' % (os.path.basename(run_dir), name)})
        try:
            offset = max(0, int(query["offset"][0]))
        except ValueError:
            offset = 0
        if offset > size:   # file replaced: start over
            offset = 0
        with open(path, "rb") as f:
            f.seek(offset)
            chunk = f.read(MAX_CHUNK)
        cut = chunk.rfind(b"\n") + 1   # only complete lines
        chunk = chunk[:cut]
        self._send(200, chunk, "text/csv; charset=utf-8",
                   {"X-Next-Offset": str(offset + len(chunk)), "X-File-Size": str(size)})


def _addresses():
    ips = set()
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("10.255.255.255", 1))   # no packet is sent; picks the outbound interface
        ips.add(s.getsockname()[0])
        s.close()
    except OSError:
        pass
    try:
        for ip in socket.gethostbyname_ex(socket.gethostname())[2]:
            if not ip.startswith("127."):
                ips.add(ip)
    except OSError:
        pass
    return sorted(ips)


def serve(cfg, bind="0.0.0.0", port=8080, auth=None):
    Handler.data_dir = cfg.output_dir
    Handler.auth = auth
    httpd = _Server((bind, port), Handler)
    host = socket.gethostname()
    print("perfmon dashboard serving %s" % cfg.output_dir)
    if bind in ("0.0.0.0", ""):
        print("  open from your desktop:  http://%s:%d/" % (host, port))
        for ip in _addresses():
            print("                           http://%s:%d/" % (ip, port))
    else:
        print("  listening on http://%s:%d/" % (bind, port))
    print("  blocked by a firewall? tunnel it:  ssh -L %d:localhost:%d <user>@%s  then open http://localhost:%d/"
          % (port, port, host, port))
    if auth:
        print("  basic auth enabled (user: %s)" % auth.split(":", 1)[0])
    sys.stdout.flush()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()
