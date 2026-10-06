"""Core reconciliation logic: CSV validation, matching, status, issue detection, stats.
Money is handled with Decimal (2dp) - never floats."""
import csv
import io
from datetime import date
from decimal import Decimal, InvalidOperation

CENTS = Decimal("0.01")
ZERO = Decimal("0.00")
STALE_DAYS = 7
REQUIRED_COLUMNS = ["settlement_id", "lifecycle_id", "account_id", "merchant_name", "transaction_date",
                    "settlement_date", "settlement_amount", "settlement_type", "currency"]


def D(value):
    return Decimal(str(value if value is not None else 0)).quantize(CENTS)


def _date(value, field):
    try:
        return date.fromisoformat(value)
    except ValueError:
        raise ValueError(f"{field} '{value}' is not a valid YYYY-MM-DD date")


# ---------- CSV parsing & validation ----------
def validate_row(r):
    for f in ("settlement_id", "account_id", "merchant_name", "transaction_date", "settlement_date",
              "settlement_amount", "settlement_type", "currency"):
        if not r.get(f):
            raise ValueError(f"missing required value '{f}'")
    try:
        amount = Decimal(r["settlement_amount"])
    except InvalidOperation:
        raise ValueError(f"settlement_amount '{r['settlement_amount']}' is not a number")
    if not amount.is_finite() or amount <= 0:
        raise ValueError("settlement_amount must be a positive number")
    stype = r["settlement_type"].upper()
    if stype not in ("DEBIT", "CREDIT"):
        raise ValueError(f"settlement_type '{r['settlement_type']}' must be DEBIT or CREDIT")
    cur = r["currency"].upper()
    if len(cur) != 3 or not cur.isalpha():
        raise ValueError(f"currency '{r['currency']}' must be a 3-letter code")
    return {**r, "lifecycle_id": r.get("lifecycle_id") or None, "settlement_amount": amount.quantize(CENTS),
            "settlement_type": stype, "currency": cur,
            "transaction_date": _date(r["transaction_date"], "transaction_date").isoformat(),
            "settlement_date": _date(r["settlement_date"], "settlement_date").isoformat()}


def parse_csv(text):
    """Return (valid_rows, errors). Raises ValueError if header columns are missing."""
    reader = csv.DictReader(io.StringIO(text.lstrip("\ufeff")))
    missing = [c for c in REQUIRED_COLUMNS if c not in (reader.fieldnames or [])]
    if missing:
        raise ValueError("CSV is missing columns: " + ", ".join(missing))
    rows, errors = [], []
    for line, raw in enumerate(reader, start=2):
        r = {k: (v or "").strip() for k, v in raw.items() if k}
        try:
            rows.append(validate_row(r))
        except ValueError as e:
            errors.append({"line": line, "settlement_id": r.get("settlement_id") or None, "error": str(e)})
    return rows, errors


# ---------- Matching ----------
def find_transaction(conn, row):
    """Primary: lifecycle_id. Fallback: account_id + merchant_name + transaction_date (must be unique).
    Returns (txn_row | None, method | reason)."""
    if row["lifecycle_id"]:
        t = conn.execute("SELECT * FROM transactions WHERE lifecycle_id=?", (row["lifecycle_id"],)).fetchall()
        if len(t) == 1:
            return t[0], "lifecycle_id"
    t = conn.execute("SELECT * FROM transactions WHERE account_id=? AND merchant_name=? AND transaction_date=?",
                     (row["account_id"], row["merchant_name"], row["transaction_date"])).fetchall()
    if len(t) == 1:
        return t[0], "fallback"
    return None, "ambiguous fallback match" if len(t) > 1 else "no matching transaction"


# ---------- Status & issues (pure functions) ----------
def compute_status(amount, debits, credits, txn_status="COMPLETED"):
    """Return (settlement_status, net_settled).
    REFUNDED = credits exist and the full amount was debited, yet net < amount (a genuine refund).
    PARTIAL  = net < amount otherwise (under-collected)."""
    amount, debits, credits = D(amount), D(debits), D(credits)
    net = (debits - credits).quantize(CENTS)
    if txn_status != "COMPLETED":
        return "NOT_APPLICABLE", net
    if debits == 0 and credits == 0:
        return "PENDING", net
    if net > amount:
        return "OVER_SETTLED", net
    if net == amount:
        return "FULLY_SETTLED", net
    if credits > 0 and debits >= amount:
        return "REFUNDED", net
    return "PARTIAL", net


def detect_issues(amount, status, txn_status, net, n_settlements, txn_date, today):
    amount, net = D(amount), D(net)
    issues = []

    def add(sev, code, msg, rec):
        issues.append({"severity": sev, "code": code, "message": msg, "recommendation": rec})

    if txn_status != "COMPLETED":
        if n_settlements:
            add("CRITICAL", "SETTLED_NON_COMPLETED", f"A {txn_status} transaction has {n_settlements} settlement(s) "
                f"totalling {net}.", "Investigate with the acquirer and reverse the settlement.")
        return issues
    if status == "OVER_SETTLED":
        add("CRITICAL", "OVER_SETTLED", f"Net settled {net} exceeds transaction amount {amount} by {net - amount}.",
            f"Recover the {net - amount} overpayment from the acquirer or issue a credit.")
    if status == "PENDING" and (today - txn_date).days > STALE_DAYS:
        add("CRITICAL", "NO_SETTLEMENT", f"No settlement received {(today - txn_date).days} days after the "
            f"transaction (limit {STALE_DAYS}).", "Escalate to the acquirer and confirm the transaction was submitted.")
    if status == "PARTIAL":
        add("WARNING", "PARTIAL", f"Net settled {net} is below transaction amount {amount}; {amount - net} outstanding.",
            f"Wait for the remaining {amount - net} or follow up if it is not received.")
    return issues


def flag(issues):
    sev = {i["severity"] for i in issues}
    return "CRITICAL" if "CRITICAL" in sev else "WARNING" if "WARNING" in sev else None


# ---------- Persistence ----------
def recompute(conn, transaction_id):
    t = conn.execute("SELECT * FROM transactions WHERE transaction_id=?", (transaction_id,)).fetchone()
    debits = credits = ZERO
    last = None
    for s in conn.execute("SELECT * FROM settlement_history WHERE transaction_id=?", (transaction_id,)):
        if s["settlement_type"] == "DEBIT":
            debits += D(s["settlement_amount"])
        else:
            credits += D(s["settlement_amount"])
        last = max(last or s["settlement_date"], s["settlement_date"])
    status, net = compute_status(t["transaction_amount"], debits, credits, t["status"])
    conn.execute("UPDATE transactions SET settlement_status=?, total_settled_amount=?, last_settlement_date=? "
                 "WHERE transaction_id=?", (status, float(net), last, transaction_id))


def reconcile(conn, csv_text):
    """Insert new settlements, match them, recompute affected transactions - all in ONE DB transaction.
    Idempotent: settlement_ids that already exist are skipped."""
    rows, errors = parse_csv(csv_text)
    rep = {"rows_in_file": len(rows) + len(errors), "inserted": 0, "matched_by_lifecycle_id": 0,
           "matched_by_fallback": 0, "unmatched": [], "duplicates": [], "errors": errors}
    touched = set()
    with conn:
        for r in rows:
            if conn.execute("SELECT 1 FROM settlement_history WHERE settlement_id=?", (r["settlement_id"],)).fetchone():
                rep["duplicates"].append(r["settlement_id"])
                continue
            txn, how = find_transaction(conn, r)
            if txn is not None and txn["currency"] != r["currency"]:
                errors.append({"line": None, "settlement_id": r["settlement_id"],
                               "error": f"currency {r['currency']} does not match transaction currency {txn['currency']}"})
                continue
            conn.execute("INSERT INTO settlement_history (settlement_id, transaction_id, lifecycle_id, settlement_date, "
                         "settlement_amount, settlement_type, currency) VALUES (?,?,?,?,?,?,?)",
                         (r["settlement_id"], txn["transaction_id"] if txn else None,
                          r["lifecycle_id"] or (txn["lifecycle_id"] if txn else None), r["settlement_date"],
                          float(r["settlement_amount"]), r["settlement_type"], r["currency"]))
            rep["inserted"] += 1
            if txn is None:
                rep["unmatched"].append({"settlement_id": r["settlement_id"], "reason": how})
            else:
                rep["matched_by_lifecycle_id" if how == "lifecycle_id" else "matched_by_fallback"] += 1
                touched.add(txn["transaction_id"])
        for tid in touched:
            recompute(conn, tid)
    return rep


# ---------- Read models ----------
def _view(t, n_set, today):
    net = D(t["total_settled_amount"])
    issues = detect_issues(t["transaction_amount"], t["settlement_status"], t["status"], net, n_set,
                           date.fromisoformat(t["transaction_date"]), today)
    return {"transaction_id": t["transaction_id"], "lifecycle_id": t["lifecycle_id"], "account_id": t["account_id"],
            "merchant_name": t["merchant_name"], "transaction_date": t["transaction_date"],
            "transaction_amount": float(D(t["transaction_amount"])), "currency": t["currency"], "status": t["status"],
            "settlement_status": t["settlement_status"], "total_settled_amount": float(net),
            "last_settlement_date": t["last_settlement_date"], "settlement_count": n_set,
            "issue_flag": flag(issues), "issues": issues}


def list_transactions(conn, today, status=None):
    counts = dict(conn.execute("SELECT transaction_id, COUNT(*) FROM settlement_history "
                               "WHERE transaction_id IS NOT NULL GROUP BY transaction_id").fetchall())
    out = [_view(t, counts.get(t["transaction_id"], 0), today)
           for t in conn.execute("SELECT * FROM transactions ORDER BY transaction_id")]
    return [v for v in out if not status or v["settlement_status"] == status.upper()]


def get_transaction(conn, tid, today):
    t = conn.execute("SELECT * FROM transactions WHERE transaction_id=?", (tid,)).fetchone()
    if not t:
        return None
    sets = [dict(s) for s in conn.execute("SELECT * FROM settlement_history WHERE transaction_id=? "
                                          "ORDER BY settlement_date, settlement_id", (tid,))]
    return {**_view(t, len(sets), today), "settlements": sets}


def summary(conn, today):
    txns = list_transactions(conn, today)
    by_status = {}
    for t in txns:
        by_status[t["settlement_status"]] = by_status.get(t["settlement_status"], 0) + 1
    completed = [t for t in txns if t["status"] == "COMPLETED"]
    settled = [t for t in completed if t["settlement_count"]]
    outstanding = sum((D(t["transaction_amount"]) - D(t["total_settled_amount"]) for t in completed
                       if t["settlement_status"] in ("PENDING", "PARTIAL")), ZERO)
    days = [(date.fromisoformat(r["first"]) - date.fromisoformat(r["txn"])).days for r in conn.execute(
        "SELECT MIN(s.settlement_date) first, t.transaction_date txn FROM settlement_history s JOIN transactions t "
        "ON t.transaction_id=s.transaction_id WHERE t.status='COMPLETED' GROUP BY t.transaction_id")]
    return {"total_transactions": len(txns),
            "total_settlements": conn.execute("SELECT COUNT(*) FROM settlement_history").fetchone()[0],
            "unmatched_settlements": conn.execute("SELECT COUNT(*) FROM settlement_history "
                                                  "WHERE transaction_id IS NULL").fetchone()[0],
            "by_status": by_status,
            "critical_issues": sum(t["issue_flag"] == "CRITICAL" for t in txns),
            "warnings": sum(t["issue_flag"] == "WARNING" for t in txns),
            "outstanding_amount": float(outstanding),
            "avg_days_to_settle": round(sum(days) / len(days), 2) if days else None,
            "settlement_rate": round(len(settled) / len(completed), 4) if completed else None,
            "as_of": today.isoformat()}
