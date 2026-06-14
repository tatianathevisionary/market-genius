#!/usr/bin/env python3
"""
BTC Genius — deep historical OHLCV backfill (one-shot / occasional).

The signal engine only pulls ~1y of daily history from Yahoo, which is enough
to compute the live framework but too shallow to backtest it. This script
builds a *deep* daily BTC/USD history from Binance Vision — Binance's official
public data dump at data.binance.vision — which serves complete daily klines
for BTCUSDT back to its 2017 listing as plain zipped CSV. No API key, no
third-party packages: just HTTPS + zipfile + csv over the stdlib.

Coverage: full months come from the monthly archives; the current (partial)
month is topped up from the Binance REST klines endpoint so the file always
runs right up to yesterday's close.

Outputs:
  data/btc_history.json   parallel lists {ts, open, high, low, close, volume}
  genius.db:btc_history   same, queryable (dual-write, matches project ethos)

Usage:
  python3 backfill_history.py            # 2017-08 -> today
  python3 backfill_history.py 2020-01    # from a later start month

Stdlib only; Python 3.9+.
"""

import csv
import io
import json
import sys
import urllib.error
import urllib.request
import zipfile
from datetime import datetime, timezone, date
from pathlib import Path

import db

BASE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = BASE_DIR / "data"
OUT = DATA_DIR / "btc_history.json"

SYMBOL = "BTCUSDT"
INTERVAL = "1d"
FIRST_MONTH = (2017, 8)        # BTCUSDT spot listed Aug 2017 on Binance
SOURCE = "Binance Vision spot BTCUSDT 1d"
UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) btc-genius/0.1"
TIMEOUT = 60

VISION_MONTHLY = ("https://data.binance.vision/data/spot/monthly/klines/"
                  f"{SYMBOL}/{INTERVAL}/{SYMBOL}-{INTERVAL}-%04d-%02d.zip")
REST_KLINES = ("https://api.binance.com/api/v3/klines"
               f"?symbol={SYMBOL}&interval={INTERVAL}&startTime=%d&limit=1000")


def _row_from_kline(k):
    """Map a Binance kline (CSV row or REST list) to a normalized day row.

    Kline layout: [openTime(ms), open, high, low, close, volume, ...]. Recent
    Vision CSVs carry a header line — unparseable openTime -> skip.
    """
    try:
        open_ms = int(k[0])
    except (ValueError, TypeError):
        return None
    if open_ms > 1e14:            # Binance switched 2025+ dumps to microseconds
        open_ms //= 1000
    d = datetime.fromtimestamp(open_ms / 1000, timezone.utc).date()
    return {"day": d.isoformat(), "ts": open_ms // 1000,
            "open": float(k[1]), "high": float(k[2]), "low": float(k[3]),
            "close": float(k[4]), "volume": float(k[5]), "source": SOURCE}


def fetch_month(year, month):
    """Daily rows for one month from the Vision monthly archive ([] if absent)."""
    url = VISION_MONTHLY % (year, month)
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
            blob = resp.read()
    except urllib.error.HTTPError as e:
        if e.code == 404:        # month not published yet (current/future month)
            return []
        raise
    rows = []
    with zipfile.ZipFile(io.BytesIO(blob)) as zf:
        with zf.open(zf.namelist()[0]) as fh:
            for k in csv.reader(io.TextIOWrapper(fh, "utf-8")):
                row = _row_from_kline(k)
                if row:
                    rows.append(row)
    return rows


def fetch_rest_since(start_ms):
    """Top up recent days the monthly archives don't cover yet, via REST."""
    rows, cursor = [], start_ms
    while True:
        url = REST_KLINES % cursor
        req = urllib.request.Request(url, headers={"User-Agent": UA})
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
            batch = json.load(resp)
        if not batch:
            break
        for k in batch:
            row = _row_from_kline(k)
            if row:
                rows.append(row)
        if len(batch) < 1000:
            break
        cursor = int(batch[-1][0]) + 1
    return rows


def month_range(start, end):
    """Yield (year, month) from start (inclusive) to end (inclusive)."""
    y, m = start
    while (y, m) <= end:
        yield y, m
        y, m = (y + 1, 1) if m == 12 else (y, m + 1)


def main():
    start = FIRST_MONTH
    if len(sys.argv) > 1:                    # optional YYYY-MM start override
        y, m = sys.argv[1].split("-")
        start = (int(y), int(m))

    today = datetime.now(timezone.utc).date()
    by_day = {}
    print(f"Backfilling {SYMBOL} {INTERVAL} from {start[0]}-{start[1]:02d} "
          f"via Binance Vision …")
    for year, month in month_range(start, (today.year, today.month)):
        rows = fetch_month(year, month)
        for r in rows:
            by_day[r["day"]] = r
        if rows:
            print(f"  {year}-{month:02d}: {len(rows):>3} days "
                  f"(running total {len(by_day)})")

    # Top up the part of the current month the monthly archive hasn't published.
    if by_day:
        last_ms = max(r["ts"] for r in by_day.values()) * 1000
        topup = fetch_rest_since(last_ms + 86_400_000)
        for r in topup:
            if r["day"] < today.isoformat():   # only completed daily candles
                by_day[r["day"]] = r
        if topup:
            print(f"  REST top-up: +{len(topup)} recent day(s)")

    rows = [by_day[d] for d in sorted(by_day)]
    if not rows:
        print("No data fetched — aborting.")
        return

    series = {"source": SOURCE,
              "fetched_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
              "ts": [r["ts"] for r in rows]}
    for f in ("open", "high", "low", "close", "volume"):
        series[f] = [r[f] for r in rows]

    DATA_DIR.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(series) + "\n")
    db.save_btc_history(rows)

    first, last = rows[0], rows[-1]
    print(f"\nWrote {len(rows):,} daily bars → {OUT.relative_to(BASE_DIR)}")
    print(f"  range : {first['day']} (${first['close']:,.0f}) "
          f"→ {last['day']} (${last['close']:,.0f})")
    print(f"  mirror: genius.db:btc_history (dual-written)")


if __name__ == "__main__":
    main()
