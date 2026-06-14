#!/usr/bin/env python3
"""
BTC Genius — single SQLite store for everything the pipeline produces.

One file: data/genius.db. Every collector dual-writes here alongside its
existing JSONL (the dashboards still read JSONL; the DB is the queryable
archive). All writes are best-effort: a DB hiccup logs a warning and never
kills a collector run.

Backfill + inspect:
  python3 db.py                          # import all existing JSONL history
  python3 db.py "SELECT regime, COUNT(*) FROM signals_history GROUP BY 1"

Tables:
  market_snapshots  5-min market pulse        (raw snapshot JSON + btc price)
  reddit_snapshots  20-min sentiment runs     (raw snapshot JSON)
  signals_history   hourly engine output      (price, regime, A/B, corr)
  alerts            TradingView webhooks
  journal           human + model calls       (outcome upserted when graded)
  whale_ledger      large prints/transfers    (deduped by exchange/tx id)

Stdlib only; Python 3.9+.
"""

import json
import logging
import sqlite3
import sys
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent  # src/ -> project root
DATA_DIR = BASE_DIR / "data"
DB_FILE = DATA_DIR / "genius.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS market_snapshots(
    fetched_at TEXT PRIMARY KEY, btc_price REAL, data TEXT);
CREATE TABLE IF NOT EXISTS reddit_snapshots(
    fetched_at TEXT PRIMARY KEY, data TEXT);
CREATE TABLE IF NOT EXISTS signals_history(
    ts TEXT PRIMARY KEY, price REAL, regime TEXT,
    a_score INTEGER, b_score INTEGER, corr_nq REAL);
CREATE TABLE IF NOT EXISTS alerts(
    received_at TEXT, source TEXT, payload TEXT,
    UNIQUE(received_at, payload));
CREATE TABLE IF NOT EXISTS journal(
    logged_at TEXT, author TEXT, call TEXT, snapshot TEXT, outcome TEXT,
    UNIQUE(logged_at, author));
CREATE TABLE IF NOT EXISTS whale_ledger(
    id TEXT PRIMARY KEY, ts TEXT, source TEXT, side TEXT,
    usd INTEGER, btc REAL, price REAL, url TEXT,
    flow TEXT, venue TEXT);
CREATE TABLE IF NOT EXISTS btc_history(
    day TEXT PRIMARY KEY, ts INTEGER, open REAL, high REAL, low REAL,
    close REAL, volume REAL, source TEXT);
CREATE TABLE IF NOT EXISTS addr_seen(
    addr TEXT PRIMARY KEY, times_seen INTEGER, total_usd INTEGER, last_seen TEXT);
CREATE TABLE IF NOT EXISTS learned_labels(
    addr TEXT PRIMARY KEY, venue TEXT, via TEXT, learned_at TEXT, source_tx TEXT);
CREATE INDEX IF NOT EXISTS idx_whale_ts ON whale_ledger(ts);
CREATE INDEX IF NOT EXISTS idx_journal_ts ON journal(logged_at);
"""

MIGRATIONS = [  # additive column changes for DBs created before the column existed
    "ALTER TABLE whale_ledger ADD COLUMN flow TEXT",
    "ALTER TABLE whale_ledger ADD COLUMN venue TEXT",
]


def connect():
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(DB_FILE, timeout=10)
    con.execute("PRAGMA journal_mode=WAL")  # collectors write concurrently
    con.executescript(SCHEMA)
    for mig in MIGRATIONS:
        try:
            con.execute(mig)
        except sqlite3.OperationalError:
            pass  # column already exists
    return con


def _safe(fn):
    """DB writes must never kill a collector run — log and move on."""
    def wrapper(*args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except Exception as e:  # noqa: BLE001
            logging.warning("db: %s failed: %s", fn.__name__, e)
    return wrapper


@_safe
def save_market_snapshot(snap):
    with connect() as con:
        con.execute(
            "INSERT OR REPLACE INTO market_snapshots VALUES(?,?,?)",
            (snap.get("fetched_at"), snap.get("btc", {}).get("price"),
             json.dumps(snap)))


@_safe
def save_reddit_snapshot(snap):
    with connect() as con:
        con.execute("INSERT OR REPLACE INTO reddit_snapshots VALUES(?,?)",
                    (snap.get("fetched_at"), json.dumps(snap)))


@_safe
def save_signal(row):
    with connect() as con:
        con.execute(
            "INSERT OR REPLACE INTO signals_history VALUES(?,?,?,?,?,?)",
            (row.get("ts"), row.get("price"), row.get("regime"),
             row.get("a_score"), row.get("b_score"), row.get("corr_nq")))


@_safe
def save_alert(rec):
    with connect() as con:
        con.execute("INSERT OR IGNORE INTO alerts VALUES(?,?,?)",
                    (rec.get("received_at"), rec.get("source"),
                     json.dumps(rec.get("payload"))))


@_safe
def save_journal(entries):
    """Upsert journal entries (outcome lands later, so REPLACE on the key)."""
    if isinstance(entries, dict):
        entries = [entries]
    with connect() as con:
        con.executemany(
            "INSERT INTO journal VALUES(?,?,?,?,?) "
            "ON CONFLICT(logged_at, author) DO UPDATE SET "
            "call=excluded.call, snapshot=excluded.snapshot, outcome=excluded.outcome",
            [(e.get("logged_at"), e.get("author"),
              json.dumps(e.get("call")), json.dumps(e.get("snapshot")),
              json.dumps(e["outcome"]) if e.get("outcome") else None)
             for e in entries])


@_safe
def save_whales(entries):
    if not entries:
        return
    with connect() as con:
        con.executemany(
            "INSERT OR IGNORE INTO whale_ledger VALUES(?,?,?,?,?,?,?,?,?,?)",
            [(e.get("id"), e.get("ts"), e.get("source"), e.get("side"),
              e.get("usd"), e.get("btc"), e.get("price"), e.get("url"),
              e.get("flow"), e.get("venue"))
             for e in entries])


@_safe
def save_btc_history(rows):
    """Dual-write the deep daily OHLCV backfill (one row per UTC day)."""
    if not rows:
        return
    with connect() as con:
        con.executemany(
            "INSERT OR REPLACE INTO btc_history VALUES(?,?,?,?,?,?,?,?)",
            [(r["day"], r["ts"], r["open"], r["high"], r["low"],
              r["close"], r["volume"], r.get("source")) for r in rows])


@_safe
def bump_addrs(addrs, usd, ts):
    """Track how often each address shows up in whale transfers."""
    if not addrs:
        return
    with connect() as con:
        con.executemany(
            "INSERT INTO addr_seen VALUES(?,1,?,?) "
            "ON CONFLICT(addr) DO UPDATE SET times_seen=times_seen+1, "
            "total_usd=total_usd+excluded.total_usd, last_seen=excluded.last_seen",
            [(a, int(usd), ts) for a in set(addrs) if a])


def addr_counts(addrs):
    """times_seen per address (0 if never seen). Read-only; raises are fine."""
    addrs = [a for a in set(addrs) if a]
    if not addrs:
        return {}
    with connect() as con:
        q = ",".join("?" * len(addrs))
        rows = con.execute(
            f"SELECT addr, times_seen FROM addr_seen WHERE addr IN ({q})", addrs)
        found = dict(rows)
    return {a: found.get(a, 0) for a in addrs}


# --- CIOH auto-labeling -------------------------------------------------------
# Common Input Ownership Heuristic: every address spent as an input to one tx is
# controlled by the same entity. So if a KNOWN exchange address co-spends with
# unknown addresses, those unknowns belong to that exchange too. The seed list
# grows itself — the foundational heuristic behind every chain-analytics firm.


def learned_labels():
    """All addresses learned via CIOH so far: {addr: venue}."""
    try:
        with connect() as con:
            return dict(con.execute("SELECT addr, venue FROM learned_labels"))
    except Exception as e:  # noqa: BLE001 - read-only, best-effort
        logging.warning("db: learned_labels read failed: %s", e)
        return {}


@_safe
def learn_labels(addrs, venue, source_tx, ts):
    """Record co-spent addresses as belonging to `venue` (skip if already known)."""
    addrs = [a for a in set(addrs) if a]
    if not addrs or not venue:
        return 0
    with connect() as con:
        cur = con.executemany(
            "INSERT OR IGNORE INTO learned_labels VALUES(?,?,?,?,?)",
            [(a, venue, "CIOH co-spend", ts, source_tx) for a in addrs])
        return cur.rowcount


# --- Backfill from the existing JSONL files (idempotent) ----------------------


def _read_jsonl(path):
    if not path.exists():
        return []
    out = []
    for line in path.read_text().strip().splitlines():
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return out


def backfill():
    for snap in _read_jsonl(DATA_DIR / "market" / "snapshots.jsonl"):
        save_market_snapshot(snap)
    for snap in _read_jsonl(DATA_DIR / "reddit" / "snapshots.jsonl"):
        save_reddit_snapshot(snap)
    for row in _read_jsonl(DATA_DIR / "signals_history.jsonl"):
        save_signal(row)
    for rec in _read_jsonl(DATA_DIR / "alerts.jsonl"):
        save_alert(rec)
    save_journal(_read_jsonl(DATA_DIR / "journal.jsonl"))
    save_whales(_read_jsonl(DATA_DIR / "whale_ledger.jsonl"))

    with connect() as con:
        for (table,) in con.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"):
            n = con.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            print(f"{table:18} {n:6} rows")


def main():
    if len(sys.argv) > 1:  # ad-hoc query: python3 db.py "SELECT ..."
        with connect() as con:
            con.row_factory = sqlite3.Row
            rows = [dict(r) for r in con.execute(sys.argv[1])]
        print(json.dumps(rows, indent=2))
    else:
        backfill()


if __name__ == "__main__":
    main()
