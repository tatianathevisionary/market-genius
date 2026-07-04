#!/usr/bin/env python3
"""
BTC Genius — SIMULATED paper-trading agent (launchd KeepAlive daemon).

Places pretend 5-minute BTC up/down bets — no real money, no exchange keys,
no orders ever leave this machine. Exists so the /console page has a live,
honest "what would the signal engine's bias + short-term momentum have done"
track record. Every artifact it writes is stamped "simulated".

Rule (deterministic, auditable):
  fast = EMA8 of 1m closes, slow = EMA24 (from price_stream's rolling ring)
  mom  = (fast - slow) / slow                    # short-term momentum
  bias = signal engine verdict bias (up=+1, down=-1, else 0)
  score = 0.7 * clamp(mom / 0.05%) + 0.3 * bias  # momentum-weighted blend
  direction = UP if score >= 0 else DOWN
  stake = balance * (1% + 3% * |score|)          # conviction sizing, capped

Settlement is binary at even money minus a 4% haircut (models spread/fees):
win pays +0.96 * stake, loss costs -stake, unchanged price is a push.

On first run it backfills from the ~2h candle ring so the console isn't
empty, then trades forward one bet per 5-minute wall-clock window.

Outputs:
  data/paper/state.json    - account, open bet, equity ring (atomic replace)
  data/paper/trades.jsonl  - one line per settled bet

Stdlib only; Python 3.9+.
"""

import json
import logging
import os
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = BASE_DIR / "data"
PAPER_DIR = DATA_DIR / "paper"
STATE_FILE = PAPER_DIR / "state.json"
TRADES_FILE = PAPER_DIR / "trades.jsonl"
PRICE_FILE = DATA_DIR / "market" / "price_stream.json"
SIGNAL_FILE = DATA_DIR / "state.json"
LOG_FILE = BASE_DIR / "logs" / "paper_trader.log"

START_BALANCE = 1000.0
CYCLE_S = 300          # one bet per 5-minute window
POLL_S = 5
WIN_PAYOUT = 0.96      # even-money binary minus fees/spread haircut
STAKE_BASE = 0.01      # 1% of balance floor ...
STAKE_EDGE = 0.03      # ... +3% at max conviction
STAKE_CAP = 100.0
MOM_FULL = 0.0005      # |mom| that counts as full-conviction momentum
EQUITY_RING = 1440     # ~5 days of 5-min points
PRICE_STALE_S = 180


def now_iso():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def ema(values, period):
    if not values:
        return None
    k = 2 / (period + 1)
    e = values[0]
    for v in values[1:]:
        e = v * k + e * (1 - k)
    return e


def read_json(path):
    try:
        return json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return None


def write_state(state):
    state["updated_at"] = now_iso()
    tmp = STATE_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(state) + "\n")
    os.replace(tmp, STATE_FILE)


def append_trade(trade):
    with TRADES_FILE.open("a") as f:
        f.write(json.dumps(trade) + "\n")


def fresh_state():
    return {
        "mode": "SIMULATED",
        "disclaimer": "paper trading only — no real orders, no real money",
        "started_at": now_iso(),
        "start_balance": START_BALANCE,
        "balance": START_BALANCE,
        "realized_pnl": 0.0,
        "wins": 0, "losses": 0, "pushes": 0, "trades": 0,
        "win_rate_pct": 0.0,
        "streak": 0, "best_streak": 0, "worst_streak": 0,
        "biggest_win": 0.0, "biggest_loss": 0.0,
        "open": None,
        "last_settle": None,
        "equity": [{"t": int(time.time()), "bal": START_BALANCE}],
    }


def signal_bias():
    """Signal engine's directional bias: up=+1, down=-1, unknown=0."""
    s = read_json(SIGNAL_FILE) or {}
    bias = ((s.get("verdict") or {}).get("bias") or "").lower()
    return 1 if bias == "up" else -1 if bias == "down" else 0


def decide(closes, bias):
    """Momentum+bias blend -> (direction, score, mom). Deterministic."""
    fast, slow = ema(closes, 8), ema(closes, 24)
    if not fast or not slow or not slow > 0:
        return None
    mom = (fast - slow) / slow
    m = max(-1.0, min(1.0, mom / MOM_FULL))
    score = 0.7 * m + 0.3 * bias
    return ("UP" if score >= 0 else "DOWN"), score, mom


def stake_for(balance, score):
    return round(min(balance * (STAKE_BASE + STAKE_EDGE * abs(score)), STAKE_CAP), 2)


def settle(state, exit_price, ts_settle):
    """Resolve the open bet against exit_price and update the account."""
    o = state["open"]
    state["open"] = None
    moved_up = exit_price > o["entry"]
    moved_down = exit_price < o["entry"]
    if not moved_up and not moved_down:
        result, pnl = "PUSH", 0.0
        state["pushes"] += 1
    elif (o["dir"] == "UP") == moved_up:
        result, pnl = "WIN", round(o["stake"] * WIN_PAYOUT, 2)
        state["wins"] += 1
        state["streak"] = max(state["streak"], 0) + 1
        state["best_streak"] = max(state["best_streak"], state["streak"])
        state["biggest_win"] = max(state["biggest_win"], pnl)
    else:
        result, pnl = "LOSS", round(-o["stake"], 2)
        state["losses"] += 1
        state["streak"] = min(state["streak"], 0) - 1
        state["worst_streak"] = min(state["worst_streak"], state["streak"])
        state["biggest_loss"] = min(state["biggest_loss"], pnl)
    state["trades"] += 1
    state["balance"] = round(state["balance"] + pnl, 2)
    state["realized_pnl"] = round(state["balance"] - state["start_balance"], 2)
    decided = state["wins"] + state["losses"]
    state["win_rate_pct"] = round(100 * state["wins"] / decided, 1) if decided else 0.0
    state["equity"].append({"t": ts_settle, "bal": state["balance"]})
    state["equity"] = state["equity"][-EQUITY_RING:]
    trade = {
        "id": o["id"], "simulated": True,
        "ts_open": o["ts_open"], "ts_settle": ts_settle,
        "dir": o["dir"], "entry": o["entry"], "exit": exit_price,
        "stake": o["stake"], "pnl": pnl, "result": result,
        "mom_pct": o["mom_pct"], "bias": o["bias"],
        "balance_after": state["balance"],
    }
    state["last_settle"] = trade
    return trade


def open_bet(state, price, closes, ts):
    d = decide(closes, signal_bias())
    if d is None:
        return None
    direction, score, mom = d
    bias = signal_bias()
    state["open"] = {
        "id": uuid.uuid4().hex[:8], "simulated": True,
        "ts_open": ts, "settle_ts": ts + CYCLE_S,
        "dir": direction, "entry": price,
        "stake": stake_for(state["balance"], score),
        "score": round(score, 3), "mom_pct": round(mom * 100, 4), "bias": bias,
        "reason": f"EMA8/24 mom {mom * 100:+.3f}% · engine bias {bias:+d}",
    }
    return state["open"]


def backfill(state, ring):
    """Seed history by replaying the ~2h 1m-candle ring in 5-min steps."""
    closes = [c["c"] for c in ring]
    times = [c["t"] for c in ring]
    n = 0
    for i in range(30, len(ring) - 5, 5):
        d = decide(closes[:i], 0)  # no historical engine bias available
        if d is None:
            continue
        direction, score, _ = d
        state["open"] = {
            "id": uuid.uuid4().hex[:8], "simulated": True, "backfill": True,
            "ts_open": times[i - 1], "settle_ts": times[i + 4],
            "dir": direction, "entry": closes[i - 1],
            "stake": stake_for(state["balance"], score),
            "score": round(score, 3),
            "mom_pct": round(d[2] * 100, 4), "bias": 0,
        }
        trade = settle(state, closes[i + 4], times[i + 4])
        trade["backfill"] = True
        append_trade(trade)
        n += 1
    return n


def main():
    PAPER_DIR.mkdir(parents=True, exist_ok=True)
    LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(filename=str(LOG_FILE), level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")
    state = read_json(STATE_FILE) or fresh_state()

    if not state["trades"] and not TRADES_FILE.exists():
        ps = read_json(PRICE_FILE) or {}
        ring = ps.get("recent_1m") or []
        if len(ring) >= 40:
            n = backfill(state, ring)
            logging.info("backfilled %d simulated bets from candle ring", n)
    write_state(state)
    logging.info("paper trader up · balance $%.2f · %d trades (SIMULATED)",
                 state["balance"], state["trades"])

    while True:
        time.sleep(POLL_S)
        ps = read_json(PRICE_FILE)
        if not ps:
            continue
        try:
            age = time.time() - datetime.fromisoformat(
                ps["updated_at"]).timestamp()
        except (KeyError, ValueError):
            continue
        if age > PRICE_STALE_S:
            continue  # stream is down; never bet on a stale price
        price = float(ps["price"])
        closes = [c["c"] for c in ps.get("recent_1m") or []]
        now = int(time.time())

        if state["open"] and now >= state["open"]["settle_ts"]:
            trade = settle(state, price, now)
            append_trade(trade)
            write_state(state)
            logging.info("settle %s %s pnl %+.2f bal %.2f (SIM)",
                         trade["dir"], trade["result"], trade["pnl"],
                         trade["balance_after"])

        if state["open"] is None and len(closes) >= 30:
            o = open_bet(state, price, closes, now)
            if o:
                write_state(state)
                logging.info("open %s $%.2f @ %.2f (%s) (SIM)",
                             o["dir"], o["stake"], o["entry"], o["reason"])


if __name__ == "__main__":
    main()
