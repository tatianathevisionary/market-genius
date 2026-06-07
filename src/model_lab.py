#!/usr/bin/env python3
"""
BTC Genius — model lab: train/test a predictive model on historic data.

Builds a daily feature matrix from up to 10y of history (BTC + the macro
transmission series), targets the SIGN of BTC's forward 7-day return, does a
CHRONOLOGICAL 80/20 split (never shuffle time series — that leaks the future
into training), trains pure-Python logistic regression, and reports honestly:

  - train vs test accuracy (vs majority-class baseline)
  - a toy strategy backtest on the held-out 20% (long when P(up) > threshold)
  - ranked feature weights (what the model thinks matters)

Run: python3 model_lab.py            (writes data/model_report.json + prints)
Stdlib only; Python 3.9+.
"""

import json
import math
import time
import urllib.parse
import urllib.request
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent  # src/ -> project root
REPORT = BASE_DIR / "data" / "model_report.json"

UA = "Mozilla/5.0 btc-genius-model/0.1"
SERIES = {
    "btc": "BTC-USD", "brent": "BZ=F", "us10y": "^TNX",
    "nasdaq": "NQ=F", "dxy": "DX-Y.NYB", "gold": "GC=F",
}
HORIZON = 7          # predict sign of forward 7d return
TRAIN_FRAC = 0.80
THRESH = 0.55        # strategy: long when P(up) > this


def fetch_daily(symbol, rng="10y"):
    url = (f"https://query1.finance.yahoo.com/v8/finance/chart/"
           f"{urllib.parse.quote(symbol)}?range={rng}&interval=1d")
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=30) as resp:
        r = json.load(resp)["chart"]["result"][0]
    q = r["indicators"]["quote"][0]
    out = {}
    for t, c, v in zip(r["timestamp"], q["close"], q["volume"]):
        if c is not None:
            out[t // 86400] = (c, v or 0)
    return out  # day -> (close, volume)


# --- feature helpers (operate on lists aligned to BTC days) ---------------------


def roc(xs, i, n):
    return (xs[i] / xs[i - n] - 1) * 100 if i >= n and xs[i - n] else 0.0


def sma_at(xs, i, n):
    lo = max(0, i - n + 1)
    w = xs[lo:i + 1]
    return sum(w) / len(w)


def rsi_at(xs, i, n=14):
    if i < n + 1:
        return 50.0
    g = l = 0.0
    for j in range(i - n + 1, i + 1):
        ch = xs[j] - xs[j - 1]
        g += max(ch, 0)
        l += max(-ch, 0)
    return 100.0 if l == 0 else 100 - 100 / (1 + g / l)


def corr_at(a, b, i, n=30):
    if i < n:
        return 0.0
    xa, xb = a[i - n + 1:i + 1], b[i - n + 1:i + 1]
    ma_, mb = sum(xa) / n, sum(xb) / n
    cov = sum((x - ma_) * (y - mb) for x, y in zip(xa, xb))
    va = sum((x - ma_) ** 2 for x in xa)
    vb = sum((y - mb) ** 2 for y in xb)
    return cov / math.sqrt(va * vb) if va > 0 and vb > 0 else 0.0


def build_dataset():
    print("fetching 10y history for 6 series…")
    raw = {}
    for name, sym in SERIES.items():
        raw[name] = fetch_daily(sym)
        time.sleep(0.5)

    days = sorted(raw["btc"].keys())
    # forward-fill macro series onto BTC's 7-day calendar
    cols = {}
    for name in SERIES:
        vals, last = [], None
        for d in days:
            if d in raw[name]:
                last = raw[name][d][0]
            vals.append(last)
        cols[name] = vals
    vols = [raw["btc"][d][1] for d in days]

    # first index where every series has a value and lookbacks are satisfied
    start = max(220, next(i for i in range(len(days))
                          if all(cols[n][i] is not None for n in SERIES)))

    names = ["btc_roc5", "btc_roc20", "brent_roc5", "brent_roc20", "tnx_chg5",
             "dxy_roc5", "nq_roc5", "nq_roc20", "gold_roc5", "vol_ratio",
             "mayer", "rsi14", "corr_nq30", "dd_1y", "dow"]
    X, y, ts = [], [], []
    btc = cols["btc"]
    for i in range(start, len(days) - HORIZON):
        vol_avg = sma_at(vols, i, 30) or 1
        feats = [
            roc(btc, i, 5), roc(btc, i, 20),
            roc(cols["brent"], i, 5), roc(cols["brent"], i, 20),
            cols["us10y"][i] - cols["us10y"][i - 5],
            roc(cols["dxy"], i, 5),
            roc(cols["nasdaq"], i, 5), roc(cols["nasdaq"], i, 20),
            roc(cols["gold"], i, 5),
            vols[i] / vol_avg,
            btc[i] / sma_at(btc, i, 200),
            rsi_at(btc, i),
            corr_at(btc, cols["nasdaq"], i),
            (btc[i] / max(btc[max(0, i - 365):i + 1]) - 1) * 100,
            days[i] % 7,
        ]
        X.append(feats)
        y.append(1 if btc[i + HORIZON] > btc[i] else 0)
        ts.append(days[i] * 86400)
    return names, X, y, ts, btc, days


# --- pure-python logistic regression ----------------------------------------------


def zscore_fit(X):
    n, m = len(X), len(X[0])
    mu = [sum(r[j] for r in X) / n for j in range(m)]
    sd = [math.sqrt(sum((r[j] - mu[j]) ** 2 for r in X) / n) or 1 for j in range(m)]
    return mu, sd


def zscore_apply(X, mu, sd):
    return [[(r[j] - mu[j]) / sd[j] for j in range(len(mu))] for r in X]


def train_logreg(X, y, epochs=400, lr=0.05, l2=1e-3):
    n, m = len(X), len(X[0])
    w, b = [0.0] * m, 0.0
    for _ in range(epochs):
        gw, gb = [0.0] * m, 0.0
        for xi, yi in zip(X, y):
            z = b + sum(wj * xj for wj, xj in zip(w, xi))
            p = 1 / (1 + math.exp(-max(-30, min(30, z))))
            e = p - yi
            gb += e
            for j in range(m):
                gw[j] += e * xi[j]
        b -= lr * gb / n
        for j in range(m):
            w[j] -= lr * (gw[j] / n + l2 * w[j])
    return w, b


def predict(X, w, b):
    return [1 / (1 + math.exp(-max(-30, min(30, b + sum(wj * xj for wj, xj in zip(w, r))))))
            for r in X]


def main():
    names, X, y, ts, btc, days = build_dataset()
    split = int(len(X) * TRAIN_FRAC)
    Xtr, ytr, Xte, yte = X[:split], y[:split], X[split:], y[split:]
    mu, sd = zscore_fit(Xtr)                       # fit scaler on TRAIN only
    w, b = train_logreg(zscore_apply(Xtr, mu, sd), ytr)

    ptr = predict(zscore_apply(Xtr, mu, sd), w, b)
    pte = predict(zscore_apply(Xte, mu, sd), w, b)
    acc = lambda p, yy: sum((pi > 0.5) == bool(yi) for pi, yi in zip(p, yy)) / len(yy)
    base_te = max(sum(yte), len(yte) - sum(yte)) / len(yte)

    # toy strategy on held-out 20%: long next 7d when P(up) > THRESH (no overlap mgmt
    # subtleties — directional evaluation only, no fees/slippage; educational)
    strat = hold = 1.0
    trades = wins = 0
    i0 = split
    i = 0
    while i < len(Xte) - 1:
        fwd = btc[i0 + i + HORIZON] / btc[i0 + i] if i0 + i + HORIZON < len(btc) else 1
        if pte[i] > THRESH:
            strat *= fwd
            trades += 1
            wins += fwd > 1
            i += HORIZON
        else:
            i += 1
    hold = btc[min(i0 + len(Xte) - 1 + HORIZON, len(btc) - 1)] / btc[i0]

    ranked = sorted(zip(names, w), key=lambda t: -abs(t[1]))
    report = {
        "samples": len(X), "train": len(Xtr), "test": len(Xte),
        "period_test": f"last ~{len(Xte)} trading days (chronological holdout)",
        "horizon_days": HORIZON,
        "train_accuracy": round(acc(ptr, ytr), 3),
        "test_accuracy": round(acc(pte, yte), 3),
        "majority_baseline_test": round(base_te, 3),
        "strategy_test": {"threshold": THRESH, "trades": trades,
                          "win_rate": round(wins / trades, 3) if trades else None,
                          "strategy_return_x": round(strat, 3),
                          "buy_hold_return_x": round(hold, 3)},
        "feature_weights_ranked": [(n_, round(w_, 3)) for n_, w_ in ranked],
        "honest_notes": [
            "Test accuracy within ~3pts of the majority baseline = no real edge.",
            "7d direction is close to a coin flip for everyone; edges this small die to fees.",
            "The value is in WHICH features carry weight - that tells you what matters.",
            "Next upgrades: FOMC/CPI event-day dummies, funding rates, ETF flows as features.",
        ],
    }
    REPORT.parent.mkdir(parents=True, exist_ok=True)
    REPORT.write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
