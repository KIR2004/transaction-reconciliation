"""Integration tests: real HTTP server on a temp seeded DB, fixed 'today' for determinism."""
import json
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from datetime import date
from pathlib import Path

from app.db import init_db
from app.server import make_server

TODAY = date(2025, 8, 16)


class ApiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        db = Path(cls.tmp.name) / "t.db"
        init_db(db)
        cls.srv = make_server(db, port=0, today_fn=lambda: TODAY)
        cls.base = f"http://127.0.0.1:{cls.srv.server_address[1]}"
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()
        cls.report = cls.call("POST", "/reconcile")[1]

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()
        cls.srv.server_close()
        cls.tmp.cleanup()

    @classmethod
    def call(cls, method, path, body=None):
        req = urllib.request.Request(cls.base + path, data=body, method=method)
        try:
            with urllib.request.urlopen(req) as r:
                return r.status, json.loads(r.read())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read())

    def txn(self, tid):
        return self.call("GET", f"/transactions/{tid}")[1]

    def test_reconcile_report(self):
        r = self.report
        self.assertEqual((r["inserted"], r["matched_by_lifecycle_id"], r["matched_by_fallback"]), (19, 14, 3))
        self.assertEqual(sorted(u["settlement_id"] for u in r["unmatched"]), ["SET016", "SET018"])
        self.assertEqual(r["errors"], [])

    def test_reconcile_is_idempotent(self):
        r = self.call("POST", "/reconcile")[1]
        self.assertEqual((r["inserted"], len(r["duplicates"])), (0, 19))
        self.assertEqual(self.txn("TXN001")["total_settled_amount"], 125.99)

    def test_statuses_and_totals(self):
        expected = {"TXN001": ("FULLY_SETTLED", 125.99), "TXN002": ("REFUNDED", 3.25),
                    "TXN003": ("PARTIAL", 35.30), "TXN004": ("REFUNDED", 879.99),
                    "TXN005": ("FULLY_SETTLED", 234.50), "TXN007": ("OVER_SETTLED", 180.78),
                    "TXN008": ("REFUNDED", 1000.00), "TXN010": ("PENDING", 0.0),
                    "TXN013": ("FULLY_SETTLED", 32.45), "TXN015": ("NOT_APPLICABLE", 199.99)}
        for tid, (status, total) in expected.items():
            t = self.txn(tid)
            self.assertEqual((t["settlement_status"], t["total_settled_amount"]), (status, total), tid)
        self.assertEqual(self.txn("TXN008")["last_settlement_date"], "2025-08-08")

    def test_flags(self):
        flags = {t["transaction_id"]: t["issue_flag"] for t in self.call("GET", "/transactions")[1]}
        self.assertEqual({k for k, v in flags.items() if v == "CRITICAL"}, {"TXN007", "TXN010", "TXN015"})
        self.assertEqual({k for k, v in flags.items() if v == "WARNING"}, {"TXN003"})

    def test_filter_and_detail(self):
        rows = self.call("GET", "/transactions?status=REFUNDED")[1]
        self.assertEqual(len(rows), 4)
        d = self.txn("TXN005")
        self.assertEqual(len(d["settlements"]), 2)
        self.assertEqual(self.txn("TXN007")["issues"][0]["code"], "OVER_SETTLED")
        self.assertEqual(self.call("GET", "/transactions/NOPE")[0], 404)

    def test_summary(self):
        s = self.call("GET", "/dashboard/summary")[1]
        self.assertEqual((s["total_transactions"], s["total_settlements"], s["unmatched_settlements"]), (14, 19, 2))
        self.assertEqual((s["critical_issues"], s["warnings"]), (3, 1))
        self.assertEqual(s["outstanding_amount"], 25.99)
        self.assertEqual(s["by_status"]["FULLY_SETTLED"], 5)
        self.assertEqual(s["avg_days_to_settle"], 1.0)

    def test_bad_csv_returns_400(self):
        self.assertEqual(self.call("POST", "/reconcile", b"foo,bar\n1,2\n")[0], 400)

    def test_bad_rows_reported(self):
        head = "settlement_id,lifecycle_id,account_id,merchant_name,transaction_date,settlement_date," \
               "settlement_amount,settlement_type,currency\n"
        body = (head + "BAD1,LC009,ACC131,Nike,2025-08-14,2025-08-15,-3,DEBIT,USD\n"
                       "BAD2,LC009,ACC131,Nike,2025-08-14,2025-08-15,3,DEBIT,EUR\n").encode()
        r = self.call("POST", "/reconcile", body)[1]
        self.assertEqual((r["inserted"], len(r["errors"])), (0, 2))


if __name__ == "__main__":
    unittest.main()
