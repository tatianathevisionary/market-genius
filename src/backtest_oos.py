#!/usr/bin/env python3
"""
BTC Genius — OUT-OF-SAMPLE validation of the deep-value buy threshold.

backtest.py is honest about dollars but dishonest about method: it scores a
rule whose threshold was chosen by looking at the very period it's scored on.
That's in-sample fitting — the returns are optimistic by construction, exactly
the trap of "testing on the data you trained on."

This script does it properly:

  1. TRAIN  — on the training window only, scan candidate Mayer thresholds and
              pick the one that best predicts strong forward returns (the
              "pattern" we claim exists: deep value precedes recovery).
  2. FREEZE — lock that threshold. It never sees the test window.
  3. TEST   — apply the frozen threshold to the held-out window and compare the
              forward returns of its buy-days against the buy-anytime baseline
              over the same window.

The tell for overfitting is the TRAIN→TEST gap: if the edge evaporates out of
sample, the "pattern" was noise. We use the deep Binance history (2017+, two
full cycles) so train and test each contain a distinct bear bottom.

This is price-only (Mayer = close / 200d-SMA) so it isn't capped by the ~5y of
free macro history the full framework needs — the macro-dependent A/B rules
deserve the same treatment but can only be split within one cycle today.

Usage: python3 backtest_oos.py [split_date] [horizon_days]
       defaults: split 2022-01-01, horizon 180
Reads data/btc_history.json (run backfill_history.py first).
Stdlib only; Python 3.9+.
"""

import json
import sys
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = BASE_DIR / "data"
HIST = DATA_DIR / "btc_history.json"
OUT = DATA_DIR / "backtest_oos.json"

DEFAULT_SPLIT = "2022-01-01"
DEFAULT_HORIZON = 180          # forward days we care about (medium-term hold)
MIN_SIGNALS = 30               # ignore thresholds too rare to trust in train
CANDIDATES = [round(0.50 + 0.025 * i, 3) for i in range(29)]  # 0.50 … 1.20


def load_days():
    h = json.loads(HIST.read_text())
    ts, close = h["ts"], h["close"]
    days = []
    for i in range(len(close)):
        days.append({"i": i, "ts": ts[i], "close": close[i]})
    # Mayer multiple = close / 200d simple MA (needs 200 prior closes)
    for i in range(len(days)):
        if i >= 199:
            ma200 = sum(close[i - 199:i + 1]) / 200
            days[i]["mayer"] = close[i] / ma200 if ma200 else None
        else:
            days[i]["mayer"] = None
    return days, close


def fwd_return(close, i, h):
    """Forward return from day i over h days, or None if it runs off the end."""
    j = i + h
    return (close[j] / close[i] - 1) if j < len(close) else None


def from_iso(s):
    """YYYY-MM-DD -> epoch seconds (UTC midnight), no imports beyond stdlib."""
    from datetime import datetime, timezone
    y, m, d = (int(x) for x in s.split("-"))
    return int(datetime(y, m, d, tzinfo=timezone.utc).timestamp())


def evaluate(days, close, lo, hi, horizon, thresh):
    """Mean forward return for buy-days (mayer<thresh) vs buy-anytime, in [lo,hi)."""
    sig, base = [], []
    for dy in days:
        if dy["mayer"] is None or not (lo <= dy["ts"] < hi):
            continue
        fr = fwd_return(close, dy["i"], horizon)
        if fr is None:
            continue
        base.append(fr)
        if dy["mayer"] < thresh:
            sig.append(fr)
    mean = lambda xs: sum(xs) / len(xs) if xs else None
    return {"signals": len(sig), "eval_days": len(base),
            "signal_mean_fwd": mean(sig), "baseline_mean_fwd": mean(base)}


def main():
    if not HIST.exists():
        print("data/btc_history.json missing — run: python3 backfill_history.py")
        return
    split = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_SPLIT
    horizon = int(sys.argv[2]) if len(sys.argv) > 2 else DEFAULT_HORIZON
    split_ts = from_iso(split)

    days, close = load_days()
    lo_all, hi_all = days[0]["ts"], days[-1]["ts"] + 1

    # 1+2. TRAIN: pick the threshold with the best mean forward return on the
    #             training window (subject to a minimum signal count), then FREEZE.
    best = None
    for thresh in CANDIDATES:
        r = evaluate(days, close, lo_all, split_ts, horizon, thresh)
        if r["signals"] >= MIN_SIGNALS and r["signal_mean_fwd"] is not None:
            if best is None or r["signal_mean_fwd"] > best["signal_mean_fwd"]:
                best = {**r, "thresh": thresh}
    if not best:
        print("Not enough training signals — widen the window or lower MIN_SIGNALS.")
        return
    tau = best["thresh"]

    # 3. TEST: apply the frozen threshold to the held-out window only.
    train = evaluate(days, close, lo_all, split_ts, horizon, tau)
    test = evaluate(days, close, split_ts, hi_all, horizon, tau)

    pct = lambda x: f"{x*100:+.1f}%" if x is not None else "n/a"
    edge = lambda r: (None if r["signal_mean_fwd"] is None or r["baseline_mean_fwd"] is None
                      else r["signal_mean_fwd"] - r["baseline_mean_fwd"])

    print(f"Deep-value OOS test — Mayer threshold learned on TRAIN, scored on TEST")
    print(f"  history : {days[0]['i'] and ''}{len(close)} daily bars "
          f"({split} split, {horizon}d forward horizon)\n")
    print(f"  LEARNED threshold (train only): Mayer < {tau}")
    print(f"  (train picked it from {best['signals']} qualifying buy-days)\n")
    hdr = f"  {'window':<8}{'buy-days':>10}{'signal fwd':>13}{'baseline fwd':>15}{'edge':>10}"
    print(hdr); print("  " + "-" * (len(hdr) - 2))
    for label, r in (("TRAIN", train), ("TEST", test)):
        print(f"  {label:<8}{r['signals']:>10}{pct(r['signal_mean_fwd']):>13}"
              f"{pct(r['baseline_mean_fwd']):>15}{pct(edge(r)):>10}")

    te, tr = edge(test), edge(train)
    print()
    if te is None:
        print("  Verdict: TEST horizon runs off the end — shorten horizon to score it.")
    elif te > 0 and tr and te >= 0.4 * tr:
        print(f"  Verdict: edge HOLDS out of sample ({pct(te)} vs train {pct(tr)}). "
              f"The deep-value pattern generalized.")
    elif te > 0:
        print(f"  Verdict: edge SHRINKS out of sample ({pct(te)} vs train {pct(tr)}) "
              f"but stays positive — partly real, partly fit.")
    else:
        print(f"  Verdict: edge VANISHES out of sample ({pct(te)}). "
              f"The in-sample result was overfit.")

    OUT.write_text(json.dumps({
        "split": split, "horizon_days": horizon, "learned_threshold": tau,
        "train": train, "test": test,
        "train_edge": tr, "test_edge": te,
    }, indent=2) + "\n")
    print(f"\n  → {OUT.relative_to(BASE_DIR)}")


if __name__ == "__main__":
    main()
