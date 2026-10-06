# Transaction Reconciliation System

Matches transactions to settlement data, flags problems, and shows them on a dashboard.
Python 3.9+ (standard library only), SQLite, vanilla JS + Chart.js. Nothing to `pip install`.

## Run
```bash
cd backend
python -m app.db          # optional: (re)create db/reconciliation.db from schema.sql + seed.sql
python -m app.server      # http://127.0.0.1:8000  (creates + seeds the DB if missing)
```
Open the URL, click **Reconcile default report** (or **Upload CSV**). Sample data is from Aug 2025, so
the "as of" date matters for the 7-day rule; pin it with `RECON_TODAY=2025-08-16 python -m app.server`.
Change the port with `PORT=9000`.

## Test
```bash
cd backend && python -m unittest discover -s tests -t .
```
`tests/test_recon.py` - unit tests (Scenarios 1-7, validation, matching).
`tests/test_api.py` - integration tests (real HTTP server, temp DB seeded with the sample data).

## Structure
```
backend/app/recon.py    business logic: CSV validation, matching, status, issue detection, stats (pure + DB)
backend/app/db.py       connection, schema/seed loader
backend/app/server.py   REST API + serves the frontend
db/schema.sql, seed.sql tables and test data     data/settlement_report.csv  sample CSV
frontend/index.html     dashboard, transaction list, detail + timeline
```
**Approach:** `compute_status` and `detect_issues` are pure functions (easy to test). `reconcile()` validates the CSV,
inserts new settlements, matches each, then recomputes every touched transaction from its full settlement history,
all in one DB transaction. Issue flags are derived at read time, so changing the as-of date never needs a re-run.

## API
| Endpoint | Description |
|---|---|
| `POST /reconcile` | Body: CSV text (`Content-Type: text/csv`). Empty body = use `data/settlement_report.csv`. Returns `{rows_in_file, inserted, matched_by_lifecycle_id, matched_by_fallback, unmatched[], duplicates[], errors[]}`. `400` if the header is invalid. |
| `GET /transactions?status=PARTIAL` | All transactions with `settlement_status`, `total_settled_amount`, `issue_flag` (`CRITICAL`/`WARNING`/null), `issues[]`. |
| `GET /transactions/{id}` | One transaction + `settlements[]` + issues with recommendations. `404` if unknown. |
| `GET /dashboard/summary` | Totals, `by_status`, `critical_issues`, `warnings`, `outstanding_amount`, `avg_days_to_settle`, `settlement_rate`. |

## Assumptions
1. **Net settled** = sum(DEBIT) - sum(CREDIT), computed in `Decimal` (never floats).
2. **PARTIAL vs REFUNDED** overlap in the spec (both are net < amount). Rule used: if credits exist **and** debits >= transaction
   amount, it is **REFUNDED** (a genuine refund, no issue per Scenarios 3 and 7); otherwise **PARTIAL** (under-collected, warning).
3. **Matching:** `lifecycle_id` first; if absent or not found, fall back to account + merchant + date. The fallback must match
   exactly one transaction; zero or several = unmatched.
4. **Unmatched settlements** (e.g. SET016, SET018) are stored with `transaction_id = NULL`, reported by `/reconcile`, and counted
   on the dashboard. They are not silently dropped.
5. **Failed/declined transactions** keep status `NOT_APPLICABLE`. A settlement against one (SET017 -> TXN015) is a **critical** issue.
6. **No settlement after 7 days** = PENDING and more than 7 days between transaction date and the as-of date (default: today). Only applies to COMPLETED transactions.
7. **Idempotent:** a `settlement_id` already in the DB is skipped, so re-running a report never double-counts.
8. **Validation:** a bad row (non-positive/non-numeric amount, bad date, type not DEBIT/CREDIT, currency mismatch with the
   transaction) is reported in `errors` and skipped; the rest of the file still processes.
9. **Outstanding amount** = (amount - net settled) for COMPLETED transactions that are PENDING or PARTIAL. **Settlement rate** =
   completed transactions with any settlement / completed transactions. **Avg days to settle** = first settlement date - transaction date.
10. `settlement_history.transaction_id` is nullable (needed for unmatched rows); otherwise the schema is as provided.

## Known limitations
- Single-currency math only; mixed currencies are rejected rather than converted.
- No auth, pagination, or concurrency control beyond SQLite's own locking; `http.server` is for demo use, not production.
- Settlements are never edited or removed; corrections would need a reversal workflow.
- Sample CSV has no negative-amount refunds; credits are modelled with `settlement_type = CREDIT`, as specified.
- The frontend loads Chart.js and the font from a CDN (needs internet in the browser).
- Frontend has no automated tests.
