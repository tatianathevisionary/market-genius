#!/usr/bin/env python3
"""
BTC Genius — OIL track: crude futures + geopolitical event collector.

Oil is the most politics-driven major asset: OPEC+ quota decisions, sanctions,
wars near producing regions, and chokepoint incidents (Strait of Hormuz moves
~20% of global supply) reprice the futures curve within minutes. This daemon
builds the dataset the /oil page overlays:

  1. Brent (BZ=F) + WTI (CL=F) front-month futures — live quote and 2 years
     of daily closes (Yahoo Finance chart API, keyless, same source family as
     market_collector).
  2. Geopolitical oil events from GDELT's free DOC 2.0 API (keyless global
     news firehose): one query per driver category, keyword-tagged, deduped
     by URL into an append-only ledger.
  3. A 0-100 "risk pulse" per run: how loud the geopolitical tape is right
     now vs a quiet baseline, weighted toward supply-threat categories.

Outputs (data/oil/):
  state.json     - quotes, spread, risk pulse, latest tagged events
  history.json   - 2y daily Brent/WTI closes for charting
  events.jsonl   - append-only event ledger (chart markers; deduped)

First run backfills ~2 months of events so the chart isn't empty.
Runs every 30 min via launchd. Stdlib only; Python 3.9+.
"""

import json
import logging
import time
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
OIL_DIR = BASE_DIR / "data" / "oil"
STATE_FILE = OIL_DIR / "state.json"
HISTORY_FILE = OIL_DIR / "history.json"
EVENTS_FILE = OIL_DIR / "events.jsonl"
LOG_FILE = BASE_DIR / "logs" / "oil_collector.log"

UA = "btc-genius-oil/0.1"
TIMEOUT = 20
GDELT = "https://api.gdeltproject.org/api/v2/doc/doc"

# Driver categories -> GDELT query. Terms are ANDed; quoted phrases exact.
# weight = how much a hit moves the risk pulse (supply threats > chatter).
CATEGORIES = {
    "opec":       {"q": '"OPEC" oil production', "weight": 2.0,
                   "label": "OPEC+ supply policy"},
    "sanctions":  {"q": '"oil sanctions"', "weight": 2.5,
                   "label": "Sanctions on producers"},
    "chokepoint": {"q": '"strait of hormuz" OR "suez canal" oil', "weight": 3.0,
                   "label": "Chokepoint threat"},
    "conflict":   {"q": '"oil" (attack OR strike OR war) (refinery OR pipeline OR tanker OR oilfield)',
                   "weight": 3.0, "label": "Attacks on oil infrastructure"},
    "spr":        {"q": '"strategic petroleum reserve"', "weight": 1.5,
                   "label": "SPR releases/refills"},
    "demand":     {"q": '"oil demand" (china OR recession OR forecast)', "weight": 1.0,
                   "label": "Demand outlook"},
}
def now_iso():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def get_json(url):
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
        return json.load(r)


def fetch_future(symbol, rng="2y"):
    """Front-month quote + daily closes from Yahoo's chart API."""
    d = get_json("https://query1.finance.yahoo.com/v8/finance/chart/"
                 f"{urllib.parse.quote(symbol)}?range={rng}&interval=1d")
    res = d["chart"]["result"][0]
    meta = res["meta"]
    closes = res["indicators"]["quote"][0].get("close") or []
    days = [{"d": datetime.fromtimestamp(t, tz=timezone.utc).strftime("%Y-%m-%d"),
             "c": round(c, 2)}
            for t, c in zip(res.get("timestamp") or [], closes) if c]
    price = meta.get("regularMarketPrice")
    # previous DAILY close = second-to-last bar (meta's previousClose is the
    # close from the start of the requested range — 2 years ago here).
    prev = days[-2]["c"] if len(days) >= 2 else None
    return {
        "price": price,
        "change_pct": round((price - prev) / prev * 100, 2) if price and prev else None,
        "days": days,
    }


def fetch_gdelt(query, timespan="48h", maxrecords=40):
    """One GDELT query, respecting the 1-per-5s limit (retry once on refusal)."""
    url = (f"{GDELT}?query={urllib.parse.quote(query + ' sourcelang:english')}"
           f"&mode=artlist&maxrecords={maxrecords}&timespan={timespan}&format=json")
    for attempt in (1, 2):
        try:
            return get_json(url).get("articles") or []
        except Exception as e:  # noqa: BLE001 - rate-limit text isn't JSON
            if attempt == 1:
                time.sleep(12)   # refusal usually means we were too fast
            else:
                logging.warning("gdelt query failed (%s): %s", query[:40], e)
    return []


def known_urls(limit=4000):
    if not EVENTS_FILE.exists():
        return set()
    lines = EVENTS_FILE.read_text().strip().splitlines()[-limit:]
    out = set()
    for ln in lines:
        try:
            out.add(json.loads(ln)["url"])
        except (json.JSONDecodeError, KeyError):
            continue
    return out


def art_to_event(a, cat):
    seen = a.get("seendate", "")
    # GDELT: 20260705T021500Z -> ISO
    ts = (f"{seen[0:4]}-{seen[4:6]}-{seen[6:8]}T{seen[9:11]}:{seen[11:13]}:00Z"
          if len(seen) >= 15 else now_iso())
    return {
        "ts": ts, "date": ts[:10], "cat": cat,
        "title": (a.get("title") or "")[:180],
        "url": a.get("url"), "source": a.get("domain"),
    }


def ledger_cats():
    """Which categories already have events in the ledger (for backfill)."""
    cats = set()
    if EVENTS_FILE.exists():
        for ln in EVENTS_FILE.read_text().strip().splitlines():
            try:
                cats.add(json.loads(ln)["cat"])
            except (json.JSONDecodeError, KeyError):
                continue
    return cats


def collect_events():
    """One GDELT pass across all categories -> (new events, per-cat counts).

    Categories with no ledger history yet get a 60-day backfill window;
    established ones just scan the last 48h.
    """
    seen, have = known_urls(), ledger_cats()
    seen_titles = set()
    fresh, counts = [], {}
    for cat, cfg in CATEGORIES.items():
        backfill = cat not in have
        arts = fetch_gdelt(cfg["q"], timespan="60d" if backfill else "48h",
                           maxrecords=100 if backfill else 40)
        counts[cat] = len(arts)
        for a in arts:
            u, tkey = a.get("url"), (a.get("title") or "")[:60].lower()
            if not u or u in seen or (tkey and tkey in seen_titles):
                continue   # dedupe by URL and by near-identical headline
            seen.add(u)
            seen_titles.add(tkey)
            fresh.append(art_to_event(a, cat))
        time.sleep(6)          # GDELT free tier: one request per 5 seconds
    fresh.sort(key=lambda e: e["ts"])
    if fresh:
        with EVENTS_FILE.open("a") as f:
            for e in fresh:
                f.write(json.dumps(e) + "\n")
    return fresh, counts


def risk_pulse(counts):
    """Weighted news intensity -> 0-100 saturating curve (50 ≈ busy tape)."""
    raw = sum(counts.get(c, 0) * cfg["weight"] for c, cfg in CATEGORIES.items())
    return round(100 * raw / (raw + 60))


def main():
    OIL_DIR.mkdir(parents=True, exist_ok=True)
    LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(filename=str(LOG_FILE), level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")

    brent = fetch_future("BZ=F")
    wti = fetch_future("CL=F")
    HISTORY_FILE.write_text(json.dumps({
        "updated_at": now_iso(),
        "brent": brent["days"], "wti": wti["days"],
    }))

    fresh, counts = collect_events()

    # latest events for the feed (tail of the ledger, newest first)
    tail = []
    for ln in EVENTS_FILE.read_text().strip().splitlines()[-40:]:
        try:
            tail.append(json.loads(ln))
        except json.JSONDecodeError:
            continue

    spread = (round(brent["price"] - wti["price"], 2)
              if brent["price"] and wti["price"] else None)
    STATE_FILE.write_text(json.dumps({
        "updated_at": now_iso(),
        "brent": {k: brent[k] for k in ("price", "change_pct")},
        "wti": {k: wti[k] for k in ("price", "change_pct")},
        "brent_wti_spread": spread,
        "risk_pulse": risk_pulse(counts),
        "category_counts_48h": counts,
        "categories": {c: cfg["label"] for c, cfg in CATEGORIES.items()},
        "latest_events": list(reversed(tail)),
    }))
    logging.info("oil update: brent %.2f wti %.2f pulse %s · %d new events",
                 brent["price"] or 0, wti["price"] or 0, risk_pulse(counts),
                 len(fresh))


if __name__ == "__main__":
    main()
