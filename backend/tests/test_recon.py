"""Unit tests - one per scenario in the exercise, plus validation/matching edge cases."""
import sqlite3
import unittest
from datetime import date
from decimal import Decimal as Dec

from app import recon

TODAY = date(2025, 8, 16)
TXN_DATE = date(2025, 8, 14)


def run(amount, debits, credits, txn_status="COMPLETED", n=1, txn_date=TXN_DATE, today=TODAY):
    status, net = recon.compute_status(amount, debits, credits, txn_status)
    return status, net, recon.detect_issues(amount, status, txn_status, net, n, txn_date, today)


class Scenarios(unittest.TestCase):
    def test1_perfect_match(self):
        s, net, issues = run("100.00", "100.00", "0")
        self.assertEqual((s, net, issues), ("FULLY_SETTLED", Dec("100.00"), []))

    def test2_partial_warning(self):
        s, _, issues = run("100.00", "80.00", "0")
        self.assertEqual(s, "PARTIAL")
        self.assertEqual(recon.flag(issues), "WARNING")

    def test3_debit_plus_credit_no_issue(self):
        s, net, issues = run("100.00", "100.00", "20.00")
        self.assertEqual((s, net), ("REFUNDED", Dec("80.00")))
        self.assertEqual(issues, [])

    def test4_over_settled_critical(self):
        s, _, issues = run("100.00", "120.00", "0")
        self.assertEqual(s, "OVER_SETTLED")
        self.assertEqual(recon.flag(issues), "CRITICAL")

    def test6_status_transitions(self):
        self.assertEqual(recon.compute_status("100", "0", "0")[0], "PENDING")
        self.assertEqual(recon.compute_status("100", "50", "0")[0], "PARTIAL")
        self.assertEqual(recon.compute_status("100", "100", "0"), ("FULLY_SETTLED", Dec("100.00")))

    def test7_complex_refund(self):
        self.assertEqual(recon.compute_status("100", "100", "30"), ("REFUNDED", Dec("70.00")))

    def test_no_float_drift(self):
        self.assertEqual(recon.compute_status("0.30", "0.10", "0")[0], "PARTIAL")
        self.assertEqual(recon.compute_status("0.30", "0.30", "0")[0], "FULLY_SETTLED")

    def test_no_settlement_after_7_days(self):
        _, _, issues = run("100", "0", "0", n=0, txn_date=date(2025, 8, 7))
        self.assertEqual(issues[0]["code"], "NO_SETTLEMENT")
        _, _, recent = run("100", "0", "0", n=0, txn_date=date(2025, 8, 14))
        self.assertEqual(recent, [])

    def test_failed_txn_with_settlement_is_critical(self):
        s, _, issues = run("100", "100", "0", txn_status="FAILED")
        self.assertEqual(s, "NOT_APPLICABLE")
        self.assertEqual(recon.flag(issues), "CRITICAL")


class Matching(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("CREATE TABLE transactions (transaction_id, lifecycle_id, account_id, merchant_name, "
                          "transaction_date, currency)")
        self.conn.execute("INSERT INTO transactions VALUES ('T1','LC1','A1','Shop','2025-08-14','USD')")

    def row(self, **kw):
        return {"lifecycle_id": None, "account_id": "A1", "merchant_name": "Shop",
                "transaction_date": "2025-08-14", **kw}

    def test5_fallback_without_lifecycle_id(self):
        t, how = recon.find_transaction(self.conn, self.row())
        self.assertEqual((t["transaction_id"], how), ("T1", "fallback"))

    def test_primary_by_lifecycle_id(self):
        t, how = recon.find_transaction(self.conn, self.row(lifecycle_id="LC1", account_id="X"))
        self.assertEqual((t["transaction_id"], how), ("T1", "lifecycle_id"))

    def test_unmatched(self):
        t, _ = recon.find_transaction(self.conn, self.row(lifecycle_id="LC9", account_id="ZZ"))
        self.assertIsNone(t)


class Validation(unittest.TestCase):
    HEAD = ",".join(recon.REQUIRED_COLUMNS) + "\n"

    def test_bad_rows_are_collected_not_fatal(self):
        csv_text = self.HEAD + ("S1,,A,M,2025-08-14,2025-08-15,10.00,DEBIT,USD\n"
                                "S2,,A,M,2025-08-14,2025-08-15,-5,DEBIT,USD\n"
                                "S3,,A,M,2025-08-14,2025-08-15,abc,DEBIT,USD\n"
                                "S4,,A,M,not-a-date,2025-08-15,5,DEBIT,USD\n"
                                "S5,,A,M,2025-08-14,2025-08-15,5,REFUND,USD\n")
        rows, errors = recon.parse_csv(csv_text)
        self.assertEqual([r["settlement_id"] for r in rows], ["S1"])
        self.assertEqual(len(errors), 4)

    def test_missing_columns(self):
        with self.assertRaises(ValueError):
            recon.parse_csv("settlement_id,amount\nS1,1\n")


if __name__ == "__main__":
    unittest.main()
