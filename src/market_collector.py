#!/usr/bin/env python3
"""
BTC Genius — Tier 1 market data collector (launchd, every 5 min).

Collects the six INDICATORS.md Tier-1 series:
  BTC/USD price+volume (Coinbase -> Binance -> CoinGecko fallback chain)
  Brent crude, US 10Y yield, Nasdaq futures, DXY, Gold (Yahoo Finance)

Outputs (under data/market/):
  snapshots.jsonl - one JSON line per run (the intraday time series)
  latest.json     - most recent values (read by dashboards / other tools)
Stdlib only; Python 3.9+.
"""

import json
import logging
import time
import urllib.request
import urllib.error
import urllib.parse
from datetime import datetime, timezone
from pathlib import Path

import db

BASE_DIR = Path(__file__).resolve().parent.parent  # src/ -> project root
DATA_DIR = BASE_DIR / "data" / "market"
SNAPSHOTS = DATA_DIR / "snapshots.jsonl"
LATEST = DATA_DIR / "latest.json"
WHALE_LEDGER = BASE_DIR / "data" / "whale_ledger.jsonl"
LEDGER_LOCK = BASE_DIR / "data" / ".whale_ledger.lock"  # cross-process write lock
LOG_FILE = BASE_DIR / "logs" / "market_collector.log"

PRINT_MIN_USD = 250_000       # single Binance print -> ledger entry
ONCHAIN_MIN_USD = 1_000_000   # single on-chain transfer -> ledger entry
LEDGER_MAX_LINES = 1000

TIMEOUT = 15
UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) btc-genius/0.1"

YAHOO_SYMBOLS = {
    "brent": "BZ=F",
    "us10y": "^TNX",
    "nasdaq_fut": "NQ=F",
    "dxy": "DX-Y.NYB",
    "gold": "GC=F",
}


def get_json(url, headers=None):
    req = urllib.request.Request(url, headers={"User-Agent": UA, **(headers or {})})
    with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
        return json.load(resp)


# --- BTC: fallback chain ------------------------------------------------------


def btc_coinbase():
    t = get_json("https://api.exchange.coinbase.com/products/BTC-USD/ticker")
    return {"price": float(t["price"]), "volume_24h_btc": float(t["volume"]),
            "source": "coinbase"}


def btc_binance():
    t = get_json("https://api.binance.com/api/v3/ticker/24hr?symbol=BTCUSDT")
    return {"price": float(t["lastPrice"]), "volume_24h_btc": float(t["volume"]),
            "change_24h_pct": float(t["priceChangePercent"]), "source": "binance"}


def btc_coingecko():
    t = get_json("https://api.coingecko.com/api/v3/simple/price"
                 "?ids=bitcoin&vs_currencies=usd"
                 "&include_24hr_vol=true&include_24hr_change=true")["bitcoin"]
    return {"price": t["usd"], "volume_24h_usd": t["usd_24h_vol"],
            "change_24h_pct": t["usd_24h_change"], "source": "coingecko"}


def fetch_btc():
    for fn in (btc_coinbase, btc_binance, btc_coingecko):
        try:
            return fn()
        except Exception as e:  # noqa: BLE001 - any source failure -> next source
            logging.warning("BTC source %s failed: %s", fn.__name__, e)
    return None


def fetch_whales():
    """Sample the Binance trade tape for large prints (whale activity).

    aggTrades 'm' flag = isBuyerMaker: True -> the SELLER was the aggressor
    (market sell), False -> aggressive buy. Large aggressive-sell dominance
    while price falls = distribution / sell-off signature.
    """
    raw = get_json("https://api.binance.com/api/v3/aggTrades?symbol=BTCUSDT&limit=1000")
    trades = [(float(x["p"]), float(x["q"]), x["m"], x["T"], x["a"]) for x in raw]
    big = [(p * q, m) for p, q, m, _, _ in trades if p * q >= 100_000]
    span_s = (trades[-1][3] - trades[0][3]) / 1000 if len(trades) > 1 else 0
    summary = {
        "window_s": round(span_s),
        "largest_usd": round(max((p * q for p, q, _, _, _ in trades), default=0)),
        "big_count": len(big),
        "big_buy_usd": round(sum(u for u, m in big if not m)),
        "big_sell_usd": round(sum(u for u, m in big if m)),
    }
    # Individual whale prints for the persistent ledger ('m' True = seller aggressed).
    prints = [{
        "ts": datetime.fromtimestamp(t / 1000, timezone.utc).isoformat(timespec="seconds"),
        "source": "binance", "side": "sell" if m else "buy",
        "usd": round(p * q), "btc": round(q, 3), "price": round(p),
        "id": f"binance-{aid}",
    } for p, q, m, t, aid in trades if p * q >= PRINT_MIN_USD]
    return summary, prints


EXCHANGE_LABELS_FILE = BASE_DIR / "data" / "exchange_addresses.json"
SERVICE_ADDR_MIN_SEEN = 3   # an addr seen this often in whale txs = service wallet


def load_exchange_labels():
    """Curated seed wallets ∪ addresses learned via CIOH co-spending."""
    try:
        labels = json.loads(EXCHANGE_LABELS_FILE.read_text())
        labels = {k: v for k, v in labels.items() if not k.startswith("_")}
    except (OSError, json.JSONDecodeError):
        labels = {}
    try:
        learned = db.learned_labels()
        for addr, venue in learned.items():
            labels.setdefault(addr, venue)  # curated labels win on conflict
    except Exception as e:  # noqa: BLE001 - learned set is best-effort
        logging.warning("learned labels load failed: %s", e)
    return labels


def learn_from_cospend(in_addrs, labels, tx_hash, ts):
    """CIOH: if any input is a known exchange wallet, every co-spent input is
    the same entity. Teach the unknowns so the label book grows itself."""
    known = next((labels[a] for a in in_addrs if a in labels), None)
    if not known or len(in_addrs) < 2:
        return
    fresh = [a for a in in_addrs if a not in labels]
    if not fresh:
        return
    n = db.learn_labels(fresh, known, tx_hash, ts)
    if n:
        for a in fresh:
            labels[a] = known  # apply within this run too
        logging.info("CIOH: learned %d new %s wallet(s) from %s", n, known, tx_hash[:12])


def classify_transfer(in_addrs, out_addrs, labels):
    """Best-effort intent for an on-chain transfer.

    inflow   - external coins arriving AT a labeled exchange (deposit; bearish)
    outflow  - exchange coins leaving TO external addrs (withdrawal; bullish)
    shuffle  - movement stays within exchange/service wallets (housekeeping)
    unknown  - no labeled/recognized address on either side

    The direction is read from which SIDE the exchange is on, ignoring the
    change-output that batch withdrawals return to themselves — so an exchange
    paying out many customers reads as 'outflow', not a self-shuffle.
    """
    in_hit = next((labels[a] for a in in_addrs if a in labels), None)
    out_hit = next((labels[a] for a in out_addrs if a in labels), None)
    ext_in = any(a not in labels for a in in_addrs)    # an external sender?
    ext_out = any(a not in labels for a in out_addrs)  # an external recipient?

    deposit = out_hit and ext_in     # outside money landing at an exchange
    withdraw = in_hit and ext_out    # exchange money leaving to the outside
    if deposit and not withdraw:
        return "inflow", out_hit
    if withdraw and not deposit:
        return "outflow", in_hit
    if in_hit or out_hit:            # exchange-touching but not directional
        return "shuffle", in_hit or out_hit
    try:  # behavioral: repeat players in the whale feed are service wallets
        counts = db.addr_counts(in_addrs + out_addrs)
        if counts and max(counts.values()) >= SERVICE_ADDR_MIN_SEEN:
            return "shuffle", "unlabeled service wallet"
    except Exception as e:  # noqa: BLE001 - classification is best-effort
        logging.warning("addr classify failed: %s", e)
    return "unknown", None


def onchain_entry(tx, btc_price, labels, ts=None):
    """Turn one mempool tx into a classified ledger entry (or None if sub-$1M).

    Accepts the blockchain.info shape used by both the REST mempool feed and
    the websocket 'utx' stream: tx['inputs'][].prev_out.addr and tx['out'][].addr.
    Side effects (CIOH learning, address frequency) run here so the batch
    collector and the live stream stay byte-for-byte consistent.
    """
    btc = sum(o.get("value", 0) for o in tx.get("out", [])) / 1e8
    usd = btc * btc_price
    if usd < ONCHAIN_MIN_USD:
        return None
    in_addrs = [i.get("prev_out", {}).get("addr") for i in tx.get("inputs", [])]
    out_addrs = [o.get("addr") for o in tx.get("out", [])]
    in_addrs = [a for a in in_addrs if a]
    out_addrs = [a for a in out_addrs if a]
    if ts is None:
        ts = datetime.fromtimestamp(tx.get("time") or 0, timezone.utc).isoformat(timespec="seconds") \
            if tx.get("time") else datetime.now(timezone.utc).isoformat(timespec="seconds")
    learn_from_cospend(in_addrs, labels, tx["hash"], ts)  # grow the book first
    flow, venue = classify_transfer(in_addrs, out_addrs, labels)
    db.bump_addrs(in_addrs + out_addrs, usd, ts)
    return {
        "ts": ts,
        "source": "onchain", "side": "transfer",
        "flow": flow, "venue": venue,
        "usd": round(usd), "btc": round(btc, 3),
        "id": tx["hash"], "url": f"https://mempool.space/tx/{tx['hash']}",
    }


def fetch_onchain_whales(btc_price):
    """Large on-chain BTC transfers from blockchain.info's free mempool feed."""
    d = get_json("https://blockchain.info/unconfirmed-transactions?format=json")
    labels = load_exchange_labels()
    out = []
    for tx in d.get("txs", []):
        entry = onchain_entry(tx, btc_price, labels)
        if entry:
            out.append(entry)
    return out


def append_ledger(entries):
    """Append new (deduped by id) whale events; keep the file bounded.

    Guarded by an exclusive file lock so the 5-min collector and the always-on
    whale_stream daemon can both write the same ledger without clobbering.
    """
    if not entries:
        return
    import fcntl
    LEDGER_LOCK.parent.mkdir(parents=True, exist_ok=True)
    with open(LEDGER_LOCK, "w") as lk:
        fcntl.flock(lk, fcntl.LOCK_EX)
        try:
            lines = WHALE_LEDGER.read_text().strip().splitlines() if WHALE_LEDGER.exists() else []
            seen = set()
            for ln in lines:
                try:
                    seen.add(json.loads(ln).get("id"))
                except json.JSONDecodeError:
                    continue
            fresh = [e for e in entries if e["id"] not in seen]
            if not fresh:
                return
            lines = lines + [json.dumps(e) for e in sorted(fresh, key=lambda e: e["ts"])]
            WHALE_LEDGER.write_text("\n".join(lines[-LEDGER_MAX_LINES:]) + "\n")
            logging.info("whale ledger: +%d entries", len(fresh))
        finally:
            fcntl.flock(lk, fcntl.LOCK_UN)


# --- Macro: Yahoo chart meta ---------------------------------------------------


def fetch_yahoo(symbol):
    d = get_json(f"https://query1.finance.yahoo.com/v8/finance/chart/"
                 f"{urllib.parse.quote(symbol)}?range=1d&interval=5m")
    meta = d["chart"]["result"][0]["meta"]
    price = meta.get("regularMarketPrice")
    prev = meta.get("chartPreviousClose") or meta.get("previousClose")
    out = {"price": price}
    if price is not None and prev:
        out["change_pct"] = round((price - prev) / prev * 100, 3)
    return out


def main():
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(filename=str(LOG_FILE), level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")

    snapshot = {"fetched_at": datetime.now(timezone.utc).isoformat(timespec="seconds")}

    btc = fetch_btc()
    if btc:
        snapshot["btc"] = btc
    else:
        logging.error("all BTC sources failed")

    ledger = []
    try:
        snapshot["whales"], ledger = fetch_whales()
    except Exception as e:  # noqa: BLE001 - whale tape is best-effort
        logging.warning("whale tape failed: %s", e)
    if btc:
        try:
            ledger += fetch_onchain_whales(btc["price"])
        except Exception as e:  # noqa: BLE001 - on-chain feed is best-effort
            logging.warning("on-chain whales failed: %s", e)
    try:
        append_ledger(ledger)
    except Exception as e:  # noqa: BLE001
        logging.warning("whale ledger write failed: %s", e)

    for name, sym in YAHOO_SYMBOLS.items():
        try:
            snapshot[name] = fetch_yahoo(sym)
        except Exception as e:  # noqa: BLE001
            logging.warning("yahoo %s (%s) failed: %s", name, sym, e)
        time.sleep(0.5)  # politeness between Yahoo calls

    ok = [k for k in list(YAHOO_SYMBOLS) + ["btc"] if k in snapshot]
    if len(ok) <= 1:
        logging.error("run end: nothing collected")
        return

    with SNAPSHOTS.open("a") as f:
        f.write(json.dumps(snapshot) + "\n")
    LATEST.write_text(json.dumps(snapshot, indent=2) + "\n")
    db.save_market_snapshot(snapshot)
    db.save_whales(ledger)
    logging.info("run end: collected %d/%d series (%s)",
                 len(ok), len(YAHOO_SYMBOLS) + 1, ", ".join(sorted(ok)))


if __name__ == "__main__":
    main()
