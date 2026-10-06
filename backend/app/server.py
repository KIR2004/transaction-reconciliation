"""REST API + static frontend using only the standard library.
Run:  python -m app.server   (from /backend)"""
import json
import os
from datetime import date
from decimal import Decimal
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from . import recon
from .db import DB_PATH, ROOT, connect, init_db

DEFAULT_CSV = ROOT / "data" / "settlement_report.csv"
INDEX = ROOT / "frontend" / "index.html"


def _json_default(o):
    if isinstance(o, Decimal):
        return float(o)
    raise TypeError(type(o))


def make_server(db_path=DB_PATH, host="127.0.0.1", port=8000, today_fn=date.today, csv_path=DEFAULT_CSV):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def _send(self, code, body, ctype="application/json"):
            data = body if isinstance(body, bytes) else json.dumps(body, default=_json_default).encode()
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def _err(self, code, msg):
            self._send(code, {"error": msg})

        def do_GET(self):
            url = urlparse(self.path)
            parts = [p for p in url.path.split("/") if p]
            conn = connect(db_path)
            try:
                if not parts:
                    return self._send(200, INDEX.read_bytes(), "text/html; charset=utf-8")
                if parts == ["transactions"]:
                    status = parse_qs(url.query).get("status", [None])[0]
                    return self._send(200, recon.list_transactions(conn, today_fn(), status))
                if len(parts) == 2 and parts[0] == "transactions":
                    t = recon.get_transaction(conn, parts[1], today_fn())
                    return self._send(200, t) if t else self._err(404, f"Transaction {parts[1]} not found")
                if parts == ["dashboard", "summary"]:
                    return self._send(200, recon.summary(conn, today_fn()))
                self._err(404, "Not found")
            finally:
                conn.close()

        def do_POST(self):
            if urlparse(self.path).path.rstrip("/") != "/reconcile":
                return self._err(404, "Not found")
            body = self.rfile.read(int(self.headers.get("Content-Length") or 0))
            try:
                text = body.decode("utf-8-sig") if body.strip() else Path(csv_path).read_text()
            except UnicodeDecodeError:
                return self._err(400, "CSV must be UTF-8 encoded")
            conn = connect(db_path)
            try:
                self._send(200, recon.reconcile(conn, text))
            except ValueError as e:
                self._err(400, str(e))
            finally:
                conn.close()

    return ThreadingHTTPServer((host, port), Handler)


def main():
    init_db(DB_PATH)
    fixed = os.environ.get("RECON_TODAY")  # e.g. RECON_TODAY=2025-08-16 for a fixed 'as of' date
    today_fn = (lambda: date.fromisoformat(fixed)) if fixed else date.today
    port = int(os.environ.get("PORT", 8000))
    print(f"Reconciliation dashboard: http://127.0.0.1:{port}  (as of {fixed or 'today'})")
    make_server(port=port, today_fn=today_fn).serve_forever()


if __name__ == "__main__":
    main()
