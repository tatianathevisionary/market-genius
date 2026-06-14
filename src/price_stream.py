#!/usr/bin/env python3
"""
BTC Genius — always-on real-time price stream (launchd KeepAlive daemon).

The market_collector only samples BTC every 5 minutes, so intra-window moves
(and the per-minute volume that B1 capitulation-detection wants) are invisible
between runs. This daemon holds a live Binance websocket — the same keyless,
no-package feed already at the heart of the collector's fallback chain — and
records every closed 1-minute candle the instant it prints.

It reuses the minimal RFC-6455 client from whale_stream (no third-party deps)
and writes to its OWN file, data/market/price_stream.json, so it never races
the 5-min collector's latest.json. Reconnects forever with backoff.

Stdlib only; Python 3.9+.
"""

import json
import logging
import time
from collections import deque
from datetime import datetime, timezone
from pathlib import Path

from whale_stream import WebSocket   # reuse the stdlib websocket client

BASE_DIR = Path(__file__).resolve().parent.parent
OUT = BASE_DIR / "data" / "market" / "price_stream.json"
LOG_FILE = BASE_DIR / "logs" / "price_stream.log"

WS_HOST = "stream.binance.com"
WS_PORT = 9443
WS_PATH = "/ws/btcusdt@kline_1m"

RING = 120            # keep the last N closed 1m candles (~2h rolling window)
_closes = deque(maxlen=RING)


def write_state(k):
    """Persist the latest closed candle plus a rolling 1m series."""
    close = float(k["c"])
    _closes.append({"t": k["T"] // 1000, "c": close, "v": float(k["v"])})
    vols = [c["v"] for c in _closes]
    state = {
        "updated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "source": "binance ws btcusdt@kline_1m",
        "price": close,
        "candle_open_ms": k["t"],
        "minute_volume_btc": round(float(k["v"]), 4),
        "window_minutes": len(_closes),
        "minute_vol_vs_window_avg": (round(float(k["v"]) / (sum(vols) / len(vols)), 2)
                                     if vols and sum(vols) else None),
        "recent_1m": list(_closes),
    }
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(state) + "\n")


def run_once():
    ws = WebSocket(WS_HOST, WS_PORT, WS_PATH)
    ws.sock.settimeout(90)        # Binance pings ~every 3m; settle well above that
    logging.info("connected: %s%s", WS_HOST, WS_PATH)
    while True:
        msg = json.loads(ws.recv())
        k = msg.get("k")
        if k and k.get("x"):       # only act on a CLOSED candle
            write_state(k)
            logging.info("1m close $%.0f vol %.3f", float(k["c"]), float(k["v"]))


def main():
    LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(filename=str(LOG_FILE), level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")
    backoff = 2
    while True:
        try:
            run_once()
        except Exception as e:  # noqa: BLE001 - any failure -> reconnect
            logging.warning("stream error: %s; reconnect in %ds", e, backoff)
            time.sleep(backoff)
            backoff = min(backoff * 2, 60)


if __name__ == "__main__":
    main()
