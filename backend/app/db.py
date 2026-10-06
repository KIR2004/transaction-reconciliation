"""Database helpers: connection + schema/seed from the SQL scripts in /db."""
import sqlite3
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
DB_PATH = ROOT / "db" / "reconciliation.db"


def connect(path=DB_PATH):
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db(path=DB_PATH, seed=True, reset=False):
    path = Path(path)
    if reset and path.exists():
        path.unlink()
    conn = connect(path)
    conn.executescript((ROOT / "db" / "schema.sql").read_text())
    if seed and conn.execute("SELECT COUNT(*) FROM transactions").fetchone()[0] == 0:
        conn.executescript((ROOT / "db" / "seed.sql").read_text())
    conn.commit()
    conn.close()


if __name__ == "__main__":  # python -m app.db  -> (re)create seeded DB
    init_db(reset=True)
    print(f"Created {DB_PATH}")
