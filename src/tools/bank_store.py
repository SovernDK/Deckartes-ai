"""The bank's transactions kept on this computer, so a question reads Enable Banking only for days
not stored yet or recent enough to have changed. enable_banking_api.py decides what to fetch; this
module only stores and reads.

It lives in the spending database (config.spending_path, or the sandbox's), which also holds
spending.py's labels and totals, readable only by the user. Each fetch of an account's days
replaces the rows it fetched before for those days, so a pending payment that books, or one that
changed, is never kept twice. A day counts as synced only once a read of it completed. Details
are in docs/tools-banking.md.
"""

import datetime as dt
import json
import sqlite3
from collections.abc import Generator
from contextlib import contextmanager

from core import clock
from core.config import config

_SCHEMA = """
CREATE TABLE IF NOT EXISTS transactions (
    account TEXT NOT NULL,
    -- The transaction's own date (booking, else value, else transaction date)
    day TEXT NOT NULL,
    -- The fetched day it's replaced with: its date, kept inside the range it was fetched in,
    -- since the bank may return one dated just outside the range asked for
    slot TEXT NOT NULL,
    status TEXT,
    direction TEXT,
    amount TEXT,
    currency TEXT,
    payee TEXT,
    description TEXT,
    -- The bank's whole entry, for fields not read today
    raw TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS transactions_by_day ON transactions (account, day);
CREATE INDEX IF NOT EXISTS transactions_by_slot ON transactions (account, slot);
CREATE TABLE IF NOT EXISTS synced_days (
    account TEXT NOT NULL,
    day TEXT NOT NULL,
    synced_at TEXT NOT NULL,
    PRIMARY KEY (account, day)
);
"""

@contextmanager
def database(schema: str) -> Generator[sqlite3.Connection, None, None]:
    """A connection to the spending database with schema applied, whose changes are committed
    together at the end, or not at all."""
    path = config.spending_sandbox_path if config.enable_banking_sandbox else config.spending_path
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path, timeout=30)
    try:
        # It holds the user's transactions
        path.chmod(0o600)
        connection.execute("PRAGMA foreign_keys = ON")
        connection.executescript(_SCHEMA + schema)
        with connection:
            yield connection
    finally:
        connection.close()

def _days(start: dt.date, end: dt.date) -> list[str]:
    return [(start + dt.timedelta(days=offset)).isoformat() for offset in range((end - start).days + 1)]

def synced(account: str, start: dt.date, end: dt.date) -> dict[dt.date, dt.datetime]:
    """When each synced day between start and end was last read from the bank."""
    with database("") as db:
        rows = db.execute("SELECT day, synced_at FROM synced_days WHERE account = ? AND day BETWEEN ? AND ?",
                          (account, start.isoformat(), end.isoformat()))
        return {dt.date.fromisoformat(day): dt.datetime.fromisoformat(at) for day, at in rows}

def has_any(account: str) -> bool:
    with database("") as db:
        return db.execute("SELECT 1 FROM synced_days WHERE account = ? LIMIT 1", (account,)).fetchone() is not None

def replace(account: str, start: dt.date, end: dt.date, entries: list[tuple[str, dict]], *, complete: bool) -> None:
    """Store what the bank returned for an account's days start to end, as (date, entry) pairs,
    in place of what was stored for them; the days count as synced only when the read was complete."""
    first, last = start.isoformat(), end.isoformat()
    rows = []
    for day, entry in entries:
        money = entry.get("transaction_amount") or {}
        outgoing = entry.get("credit_debit_indicator") == "DBIT"
        party = (entry.get("creditor") if outgoing else entry.get("debtor")) or {}
        rows.append((account, day or first, min(max(day or first, first), last), entry.get("status"),
                     entry.get("credit_debit_indicator"), money.get("amount"), money.get("currency"),
                     party.get("name"), " ".join(entry.get("remittance_information") or []), json.dumps(entry)))
    with database("") as db:
        db.execute("DELETE FROM transactions WHERE account = ? AND slot BETWEEN ? AND ?", (account, first, last))
        db.executemany("INSERT INTO transactions VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", rows)
        if complete:
            stamp = clock.now().isoformat(timespec="seconds")
            db.executemany("INSERT OR REPLACE INTO synced_days VALUES (?, ?, ?)",
                           [(account, day, stamp) for day in _days(start, end)])

def entries(account: str, start: dt.date, end: dt.date) -> list[dict]:
    """The stored entries of an account dated start to end, as the bank gave them."""
    with database("") as db:
        rows = db.execute("SELECT raw FROM transactions WHERE account = ? AND day BETWEEN ? AND ?",
                          (account, start.isoformat(), end.isoformat()))
        return [json.loads(raw) for (raw,) in rows]
