#!/usr/bin/env python3
"""
BTC Genius — verdict backtester.

Replays the EXACT live framework (signal_engine.compute_signals + model_call)
day by day over real daily history, then compares three BUY policies on the
same signal stream so the trade-offs are visible in dollars, not opinions:

  conservative  the strict doctrine shipping today: BUY only on a constructive
                composite (>=4) WITH a seller-exhaustion signal (B>=1).
  accumulation  the new tier added to model_call: also BUY when deep-value
                (Mayer < 0.85) AND macro releasing (A>=3) AND no froth — i.e.
                scale in before capitulation confirms.
  deep_value    a deliberately reckless reference policy (NOT shipped): BUY any
                day Mayer < 0.80, ignoring macro and trend. Shows the cost of
                "just buy when it's cheap" — it catches knives.

For each policy we DCA a fixed stake on every BUY day and mark the book to the
latest close. The point isn't to pick a winner blindly — it's to answer
"when should I have bought, and what does taking more risk actually cost?"

Each series is sliced by DATE (not index) before every evaluation, so the
macro inputs are aligned to what was actually known on that day — no lookahead.

Usage: python3 backtest.py [years]   (default 5 — Yahoo serves ~5y of daily
                                      macro futures, enough for a full cycle)
Stdlib only; Python 3.9+.
"""

import bisect
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import signal_engine as se

BASE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = BASE_DIR / "data"
OUT = DATA_DIR / "backtest.json"

WARMUP = 200          # bars needed before ma200 (and the framework) are valid
STAKE = 100.0         # DCA this many dollars on each BUY day, per policy


def slice_series(s, cutoff_ts):
    """Return a copy of one OHLCV series truncated to bars with ts <= cutoff."""
    n = bisect.bisect_right(s["ts"], cutoff_ts)
    return {k: s[k][:n] for k in ("ts", "open", "high", "low", "close", "volume")}


def replay(years):
    data = {}
    for name, sym in se.SERIES.items():
        data[name] = se.fetch_daily(sym, rng=f"{years}y", interval="1d")
    btc_ts = data["btc"]["ts"]

    rows = []
    for i in range(WARMUP, len(btc_ts)):
        cutoff = btc_ts[i]
        sliced = {name: slice_series(s, cutoff) for name, s in data.items()}
        if len(sliced["btc"]["close"]) < WARMUP:
            continue
        s = se.compute_signals(sliced)
        v = se.model_call(s)
        mayer = s["price"] / s["ma200"] if s.get("ma200") else None
        day = datetime.fromtimestamp(cutoff, timezone.utc).date().isoformat()
        rows.append({
            "day": day, "price": round(s["price"], 2), "regime": s["regime"],
            "a": s["a_score"], "b": s["b_score"], "score": v["score"],
            "mayer": round(mayer, 3) if mayer else None,
            "direction": v["direction"], "tier": v.get("tier"),
            # the three policies, evaluated on the same signal stream:
            "buy_conservative": v["direction"] == "BUY" and v.get("tier") == "confirmed",
            "buy_accumulation": v["direction"] == "BUY",  # confirmed OR accumulation
            "buy_deep_value": mayer is not None and mayer < 0.80,
        })
    return rows


def simulate(rows, flag):
    """DCA STAKE on each day where `flag` is set; mark to the latest close."""
    buys = [r for r in rows if r[flag]]
    if not buys:
        return {"buys": 0}
    last_price = rows[-1]["price"]
    units = sum(STAKE / b["price"] for b in buys)
    cost = STAKE * len(buys)
    value = units * last_price
    avg_price = cost / units
    # worst close-to-date drawdown experienced after the first buy (knife pain)
    first_i = next(i for i, r in enumerate(rows) if r[flag])
    after = [r["price"] for r in rows[first_i:]]
    peak_dd = min(0.0, *( (p / buys[0]["price"] - 1) * 100 for p in after ))
    return {
        "buys": len(buys),
        "first_day": buys[0]["day"], "first_price": buys[0]["price"],
        "avg_price": round(avg_price, 2),
        "return_pct": round((value / cost - 1) * 100, 1),
        "max_drawdown_after_first_pct": round(peak_dd, 1),
    }


def main():
    years = int(sys.argv[1]) if len(sys.argv) > 1 else 5
    print(f"Replaying the framework over {years}y of daily history "
          f"(warmup {WARMUP} bars)…\n")
    rows = replay(years)
    if not rows:
        print("Not enough history to replay.")
        return
    last = rows[-1]

    policies = {
        "conservative (shipping)": "buy_conservative",
        "accumulation (new tier)": "buy_accumulation",
        "deep_value (reckless ref)": "buy_deep_value",
    }
    results = {name: simulate(rows, flag) for name, flag in policies.items()}

    # Buy-and-hold the whole window, for reference.
    bh_first, bh_last = rows[0]["price"], last["price"]
    bh_ret = round((bh_last / bh_first - 1) * 100, 1)

    span = f"{rows[0]['day']} → {last['day']}"
    print(f"Window: {span}   ${bh_first:,.0f} → ${bh_last:,.0f} "
          f"(buy & hold {bh_ret:+.1f}%)\n")
    hdr = f"{'policy':<26}{'buys':>5}{'first buy':>22}{'avg $':>10}{'return':>9}{'max DD':>9}"
    print(hdr)
    print("-" * len(hdr))
    for name, r in results.items():
        if not r.get("buys"):
            print(f"{name:<26}{0:>5}   (never triggered in window)")
            continue
        first = f"{r['first_day']} @ ${r['first_price']:,.0f}"
        print(f"{name:<26}{r['buys']:>5}{first:>22}"
              f"{r['avg_price']:>10,.0f}{r['return_pct']:>8.1f}%{r['max_drawdown_after_first_pct']:>8.1f}%")

    # Locate the most recent cycle low for the "should I have bought there?" read.
    lo = min(rows, key=lambda r: r["price"])
    print(f"\nLowest close in window: ${lo['price']:,.0f} on {lo['day']} "
          f"(A {lo['a']}/4, B {lo['b']}/3, Mayer {lo['mayer']}, "
          f"verdict {lo['direction']}{'/'+lo['tier'] if lo['tier'] else ''})")
    print(f"Latest: ${last['price']:,.0f} on {last['day']} "
          f"(A {last['a']}/4, B {last['b']}/3, Mayer {last['mayer']}, "
          f"verdict {last['direction']}{'/'+last['tier'] if last['tier'] else ''})")

    OUT.write_text(json.dumps({
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "window": span, "buy_and_hold_return_pct": bh_ret,
        "stake_per_buy": STAKE, "policies": results,
        "lowest_close": {"day": lo["day"], "price": lo["price"],
                         "a": lo["a"], "b": lo["b"], "mayer": lo["mayer"],
                         "verdict": lo["direction"], "tier": lo["tier"]},
        "rows": rows,
    }, indent=2) + "\n")
    print(f"\nFull day-by-day replay → {OUT.relative_to(BASE_DIR)}")


if __name__ == "__main__":
    main()
