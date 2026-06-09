#!/usr/bin/env python3
"""
BTC Genius — signal engine (launchd, hourly).

Pulls 1y of daily history for the Tier-1 series and computes the SIGNALS.md
framework: regime, A-score (macro release), B-score (seller exhaustion),
rolling correlations, froth/X flags, and multi-method support/resistance.
Merges in the Reddit sentiment digest and TradingView alerts, then writes:

  data/dashboard.md       - the human-readable dashboard (the product)
  data/state.json         - machine-readable current state
  data/signals_history.jsonl - one line per run (trend the scores over time)

Stdlib only; Python 3.9+.
"""

import json
import logging
import time
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

import db

BASE_DIR = Path(__file__).resolve().parent.parent  # src/ -> project root
DATA_DIR = BASE_DIR / "data"
DASHBOARD = DATA_DIR / "dashboard.md"
STATE = DATA_DIR / "state.json"
SERIES_OUT = DATA_DIR / "series.json"
HISTORY = DATA_DIR / "signals_history.jsonl"
JOURNAL = DATA_DIR / "journal.jsonl"
REDDIT_SNAPSHOTS = DATA_DIR / "reddit" / "snapshots.jsonl"
ALERTS = DATA_DIR / "alerts.jsonl"
LOG_FILE = BASE_DIR / "logs" / "signal_engine.log"

UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) btc-genius/0.1"
TIMEOUT = 20

SERIES = {
    "btc": "BTC-USD",
    "brent": "BZ=F",
    "us10y": "^TNX",
    "nasdaq": "NQ=F",
    "dxy": "DX-Y.NYB",
    "gold": "GC=F",
}

# Chart-only extras (not part of the signal framework): crypto majors.
EXTRA_SERIES = {
    "eth": "ETH-USD",
    "sol": "SOL-USD",
    "link": "LINK-USD",
    "usdcad": "CAD=X",
}

PIVOT_N = 5          # bars each side for swing high/low (R1)
LEVEL_MERGE_PCT = 1.0   # merge pivots within 1% into one zone
ROUND_STEP = 5000    # R3 round-number step for BTC
CONFLUENCE_PCT = 1.5    # a method within 1.5% of a level counts as confluence


# --- Data ---------------------------------------------------------------------


def fetch_daily(symbol, rng="1y", interval="1d"):
    """OHLCV history from Yahoo (default 1y daily). Returns dict of parallel lists."""
    url = (f"https://query1.finance.yahoo.com/v8/finance/chart/"
           f"{urllib.parse.quote(symbol)}?range={rng}&interval={interval}")
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
        r = json.load(resp)["chart"]["result"][0]
    q = r["indicators"]["quote"][0]
    rows = [(t, o, h, l, c, v) for t, o, h, l, c, v in zip(
        r["timestamp"], q["open"], q["high"], q["low"], q["close"], q["volume"])
        if c is not None]
    ts, o, h, l, c, v = (list(x) for x in zip(*rows))
    return {"ts": ts, "open": o, "high": h, "low": l, "close": c, "volume": v}


# --- Math (pure python) ---------------------------------------------------------


def sma(xs, n):
    return sum(xs[-n:]) / min(n, len(xs))


def roc(xs, n):
    return (xs[-1] - xs[-1 - n]) / xs[-1 - n] * 100 if len(xs) > n else 0.0


def correlation(a, b, n):
    """Pearson correlation of the last n aligned points."""
    a, b = a[-n:], b[-n:]
    m = min(len(a), len(b))
    a, b = a[-m:], b[-m:]
    ma_, mb = sum(a) / m, sum(b) / m
    cov = sum((x - ma_) * (y - mb) for x, y in zip(a, b))
    va = sum((x - ma_) ** 2 for x in a)
    vb = sum((y - mb) ** 2 for y in b)
    return cov / (va * vb) ** 0.5 if va > 0 and vb > 0 else 0.0


def align_by_day(s1, s2):
    """Align two daily series on shared calendar days (crypto trades 7d/wk)."""
    d2 = {t // 86400: c for t, c in zip(s2["ts"], s2["close"])}
    a, b = [], []
    for t, c in zip(s1["ts"], s1["close"]):
        day = t // 86400
        if day in d2:
            a.append(c)
            b.append(d2[day])
    return a, b


# --- Support / Resistance (SIGNALS.md §6) ---------------------------------------


def pivot_levels(highs, lows, n):
    """R1: swing highs/lows over the last ~120 bars."""
    levels = []
    lo = max(n, len(highs) - 120)
    for i in range(lo, len(highs) - n):
        if highs[i] == max(highs[i - n:i + n + 1]):
            levels.append(("swing_high", highs[i]))
        if lows[i] == min(lows[i - n:i + n + 1]):
            levels.append(("swing_low", lows[i]))
    return levels


def build_sr_levels(daily):
    """Merge R1 pivots into zones; tag confluence with R3 rounds and R4 MAs."""
    closes, price = daily["close"], daily["close"][-1]
    pivots = pivot_levels(daily["high"], daily["low"], PIVOT_N)
    ma50, ma200 = sma(closes, 50), sma(closes, 200)

    zones = []  # merge pivots within LEVEL_MERGE_PCT into zones
    for kind, lvl in sorted(pivots, key=lambda x: x[1]):
        if zones and abs(lvl - zones[-1]["level"]) / zones[-1]["level"] * 100 < LEVEL_MERGE_PCT:
            z = zones[-1]
            z["level"] = (z["level"] * z["touches"] + lvl) / (z["touches"] + 1)
            z["touches"] += 1
        else:
            zones.append({"level": lvl, "touches": 1})

    near = lambda a, b: abs(a - b) / b * 100 < CONFLUENCE_PCT
    for z in zones:
        methods = [f"R1 x{z['touches']}"]
        rounded = round(z["level"] / ROUND_STEP) * ROUND_STEP
        if near(z["level"], rounded):
            methods.append(f"R3 ${rounded:,.0f}")
        if near(z["level"], ma50):
            methods.append("R4 50d MA")
        if near(z["level"], ma200):
            methods.append("R4 200d MA")
        z["methods"] = methods
        z["strength"] = z["touches"] + len(methods) - 1
        z["side"] = "resistance" if z["level"] > price else "support"

    supports = sorted([z for z in zones if z["side"] == "support"],
                      key=lambda z: -z["level"])[:3]
    resistances = sorted([z for z in zones if z["side"] == "resistance"],
                         key=lambda z: z["level"])[:3]
    return supports, resistances, ma50, ma200


# --- Signal computation (SIGNALS.md §2-§4) ---------------------------------------


def compute_signals(data):
    btc, brent, tnx = data["btc"], data["brent"], data["us10y"]
    nq, dxy, gold = data["nasdaq"], data["dxy"], data["gold"]
    closes, vols = btc["close"], btc["volume"]
    price = closes[-1]

    btc_a, nq_a = align_by_day(btc, nq)
    btc_g, gold_a = align_by_day(btc, gold)
    corr_nq = correlation(btc_a, nq_a, 30)
    corr_nq_prev = correlation(btc_a[:-5], nq_a[:-5], 30)
    corr_gold = correlation(btc_g, gold_a, 30)

    # A-column: macro pressure release
    A = {
        "A1_oil_rolls_over": roc(brent["close"], 5) < -5.0,
        "A2_yields_peak": (tnx["close"][-1] < max(tnx["close"][-20:])
                           and roc(tnx["close"], 5) < 0),
        "A4_dxy_tops": roc(dxy["close"], 5) < 0,
        "A5_equities_stable": nq["close"][-1] > min(nq["close"][-8:-3]),
    }  # A3 (Fed funds futures) not in free data — tracked manually

    # B-column: seller exhaustion
    vol_avg30 = sma(vols, 30)
    o, c, l = btc["open"][-1], closes[-1], btc["low"][-1]
    lower_wick = min(o, c) - l
    prior30_low = min(btc["low"][-31:-1])
    B = {
        "B1_capitulation_volume": (vols[-1] > 2.0 * vol_avg30 and c < o
                                   and lower_wick > abs(c - o)),
        "B5_correlation_recouples": corr_nq > corr_nq_prev and corr_nq < 0.5,
        "B6_failed_new_low": btc["low"][-1] < prior30_low and c > prior30_low,
    }  # B2 funding / B3 ETF flows / B4 on-chain need paid/secondary feeds

    # X-column: froth/exit
    ma50 = sma(closes, 50)
    X = {
        "X2_extended_above_50d": price > ma50 * 1.25,
        "X6_hedge_test_failure": (roc(gold["close"], 5) > 0 and roc(tnx["close"], 5) > 0
                                  and price < max(closes[-21:-1])),
    }

    # Regime (§1)
    btc_down = roc(closes, 5) < -3
    macro_hot = (roc(brent["close"], 5) > 3 or roc(tnx["close"], 5) > 2
                 or roc(dxy["close"], 5) > 1)
    if btc_down and macro_hot:
        regime = "Macro-driven selloff"
    elif btc_down:
        regime = "Crypto-specific selloff"
    elif X["X2_extended_above_50d"]:
        regime = "Froth"
    elif abs(roc(closes, 5)) < 1.5:
        regime = "Basing"
    else:
        regime = "Trending"

    supports, resistances, ma50, ma200 = build_sr_levels(btc)

    return {
        "price": price,
        "regime": regime,
        "A": A, "a_score": sum(A.values()), "a_max": len(A),
        "B": B, "b_score": sum(B.values()), "b_max": len(B),
        "X": X,
        "corr_btc_nasdaq_30d": round(corr_nq, 3),
        "corr_btc_gold_30d": round(corr_gold, 3),
        "btc_gold_ratio": round(price / gold["close"][-1], 3),
        "roc5": {k: round(roc(data[k]["close"], 5), 2) for k in SERIES},
        "volume_vs_30d_avg": round(vols[-1] / vol_avg30, 2) if vol_avg30 else None,
        "ma50": round(ma50, 0), "ma200": round(ma200, 0),
        "dist_from_50d_pct": round((price / ma50 - 1) * 100, 1),
        "supports": supports, "resistances": resistances,
    }


def guidance(s):
    """Plain-language read of the checklist state, per SIGNALS.md §3/§4."""
    a, b = s["a_score"], s["b_score"]
    lines = []
    if "selloff" in s["regime"].lower():
        if a >= 3 and b >= 2:
            lines.append("⚡ ENTRY WATCH: A and B confluence per §3 — review the "
                         "capitulation-reversal checklist (small size, hard invalidation).")
        elif b >= 2:
            lines.append("Capitulation evidence building (B) but macro cause still "
                         "live (A low) — historically a bounce setup, not a bottom. Wait.")
        elif a >= 3:
            lines.append("Macro pressure releasing but no seller-exhaustion evidence "
                         "yet — watch for B1/B6 events at the support zones below.")
        else:
            lines.append("Cause still live, sellers not exhausted — no entry. "
                         "Track A-score daily; oil and 10Y are the leads.")
    if s["regime"] == "Basing":
        lines.append("Basing regime: arm the §3 breakout entry — trigger is the range "
                     "high on >1.5x volume with 5d ETF inflows positive.")
    froth = sum(s["X"].values())
    if froth >= 1:
        lines.append(f"{froth} froth/exit signal(s) active — see §4 before adding risk.")
    if s["corr_btc_nasdaq_30d"] < 0.2:
        lines.append("BTC↔Nasdaq correlation broken (<0.2): moves are crypto-specific — "
                     "weight ETF flows and sentiment over macro right now.")
    return lines


# --- Auto model calls (the system predicts, then grades itself) -------------------


def model_call(s):
    """Composite BUY/SELL/HOLD model: each component votes in [-2,+2]; the
    weighted sum maps to an action. Components ship in the verdict so the
    dashboard can show WHY, and every call is auto-graded in the journal."""
    a, b = s["a_score"], s["b_score"]
    mayer = round(s["price"] / s["ma200"], 2) if s.get("ma200") else None
    froth = sum(s["X"].values())
    price, ma50, ma200 = s["price"], s.get("ma50"), s.get("ma200")
    vol = s.get("volume_vs_30d_avg") or 1.0
    selloff = "selloff" in s["regime"].lower()

    comp = {
        # cheap vs own history pays the patient (Mayer zones)
        "valuation": (2 if mayer and mayer < 0.8 else 1 if mayer and mayer < 1.0
                      else -2 if mayer and mayer > 2.4 else -1 if mayer and mayer > 1.6 else 0),
        # don't fight the tape
        "trend": (2 if price > ma50 > ma200 else 1 if price > ma50
                  else -2 if price < ma50 < ma200 else -1),
        # the selloff cause stopping (A-checklist)
        "macro_release": 2 if a >= 3 else 1 if a == 2 else 0 if a == 1 else -1,
        # capitulation evidence (B-checklist)
        "exhaustion": 2 if b >= 2 else 1 if b == 1 else 0,
        # exit signals (X-checklist)
        "froth": -2 * froth,
        # thin-volume selloff = no capitulation yet; penalize only in selloffs
        "volume": -1 if selloff and vol < 1.2 else 0,
    }
    weights = {"valuation": 1.0, "trend": 1.5, "macro_release": 1.5,
               "exhaustion": 1.5, "froth": 1.0, "volume": 1.0}
    score = round(sum(comp[k] * weights[k] for k in comp), 1)

    # Three actions only — BUY / HOLD / SELL — so the call is never ambiguous.
    # The old WAIT collapsed into HOLD (both meant "don't transact"). The nuance it
    # carried — be defensive with NEW money during a live selloff — now rides in
    # `stance`, a separate descriptor, not a phantom fourth action. `bias` is the
    # model's directional lean (what gets graded), independent of the action.
    # Doctrine guardrail: valuation alone can't trigger BUY — zone says where, the
    # B-score says when (needs at least one exhaustion signal).
    if score >= 4 and b >= 1:
        d, bias = "BUY", "up"
    elif score <= -4:
        d, bias = "SELL", "down"
    else:
        d, bias = "HOLD", "up" if score > 0 else "down" if score < 0 else "neutral"
    stance = ("defensive — selloff still live; don't deploy new money yet"
              if selloff and score < 0 else
              "constructive — leaning toward adds once a B-trigger fires"
              if score > 0 else
              "neutral — no edge either way; let the triggers decide")
    conf = max(50, min(85, round(50 + abs(score) * 4)))
    why = ", ".join(f"{k} {v:+d}" for k, v in comp.items() if v)
    return {"direction": d, "bias": bias, "confidence_pct": conf,
            "score": score, "components": comp, "stance": stance,
            "thesis": f"[auto] composite {score:+.1f} — {why or 'all components neutral'}",
            "horizon_days": 7,
            "invalidation": "auto-graded at horizon: up=+2%, down=-2%, neutral=within ±2%"}


def auto_journal(s):
    """Resolve matured calls against current price; log a new model call when it changes."""
    entries = []
    if JOURNAL.exists():
        for line in JOURNAL.read_text().strip().splitlines():
            try:
                entries.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    now = datetime.now(timezone.utc)
    changed = False

    for e in entries:  # grade matured, ungraded calls (model and human alike)
        c = e.get("call", {})
        hd = c.get("horizon_days") or {"days": 3, "weeks": 14, "months": 60,
                                       "years": 365}.get(c.get("horizon"), None)
        if not hd or "outcome" in e or not e.get("snapshot", {}).get("price"):
            continue
        t0 = datetime.fromisoformat(e["logged_at"])
        if (now - t0).total_seconds() >= hd * 86400:
            chg = (s["price"] / e["snapshot"]["price"] - 1) * 100
            bias = c.get("bias") or {"ACCUMULATE": "up", "LONG (trade)": "up", "BUY": "up",
                                     "REDUCE": "down", "SHORT (trade)": "down", "SELL": "down",
                                     "WAIT": "down", "HOLD": "up"}.get(c.get("direction"))
            correct = ((bias == "up" and chg > 2) or (bias == "down" and chg < -2)
                       or (bias == "neutral" and abs(chg) <= 2))
            e["outcome"] = {"resolved_at": now.isoformat(timespec="seconds"),
                            "change_pct": round(chg, 2), "correct": bool(correct)}
            changed = True

    call = model_call(s)
    model_entries = [e for e in entries if e.get("author") == "model"]
    last = model_entries[-1] if model_entries else None
    fresh = last and (now - datetime.fromisoformat(last["logged_at"])).total_seconds() < 86400
    same = last and (last["call"].get("direction"), last["call"].get("bias")) == \
                    (call["direction"], call["bias"])
    if not (fresh and same):  # log when the call changes, or at most daily
        entries.append({
            "logged_at": now.isoformat(timespec="seconds"), "author": "model",
            "call": call,
            "snapshot": {"price": s["price"], "regime": s["regime"],
                         "a_score": s["a_score"], "b_score": s["b_score"],
                         "mayer": round(s["price"] / s["ma200"], 2) if s.get("ma200") else None,
                         "corr_btc_nasdaq_30d": s["corr_btc_nasdaq_30d"]},
        })
        changed = True
        logging.info("model call logged: %s/%s (%d%%)",
                     call["direction"], call["bias"], call["confidence_pct"])
    if changed:
        JOURNAL.write_text("\n".join(json.dumps(e) for e in entries) + "\n")
        db.save_journal(entries)


# --- Context merge ---------------------------------------------------------------


def latest_reddit():
    if not REDDIT_SNAPSHOTS.exists():
        return None
    try:
        snap = json.loads(REDDIT_SNAPSHOTS.read_text().strip().splitlines()[-1])
        return {"fetched_at": snap.get("fetched_at"),
                "buckets": snap.get("totals", {}).get("buckets"),
                "spikes": snap.get("spikes", {})}
    except (json.JSONDecodeError, IndexError):
        return None


def recent_alerts(n=5):
    if not ALERTS.exists():
        return []
    out = []
    for line in ALERTS.read_text().strip().splitlines()[-n:]:
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return out


# --- Output ----------------------------------------------------------------------


def fmt_check(d):
    return "\n".join(f"- {'✅' if v else '⬜'} {k.replace('_', ' ')}" for k, v in d.items())


def write_dashboard(s, reddit, alerts, ts):
    L = [
        "# BTC Genius Dashboard",
        "",
        f"_Updated {ts} (hourly via launchd) — framework: [SIGNALS.md](../docs/SIGNALS.md)_",
        "",
        f"## `{s['regime']}` | A: {s['a_score']}/{s['a_max']} | B: {s['b_score']}/{s['b_max']}",
        "",
        f"**BTC ${s['price']:,.0f}** | 5d {s['roc5']['btc']:+.1f}% | "
        f"vol {s['volume_vs_30d_avg']}x avg | "
        f"{s['dist_from_50d_pct']:+.1f}% vs 50d MA (${s['ma50']:,.0f})",
        "",
    ]
    for g in guidance(s):
        L.append(f"> {g}")
        L.append("")
    L += [
        "## Macro chain (5-day moves)",
        "",
        "| Series | 5d % | Chain role |",
        "|---|---|---|",
        f"| Brent | {s['roc5']['brent']:+.1f}% | shock originator |",
        f"| US 10Y | {s['roc5']['us10y']:+.1f}% | transmission |",
        f"| DXY | {s['roc5']['dxy']:+.1f}% | denominator |",
        f"| Nasdaq fut | {s['roc5']['nasdaq']:+.1f}% | risk appetite |",
        f"| Gold | {s['roc5']['gold']:+.1f}% | hedge competitor |",
        f"| **BTC** | **{s['roc5']['btc']:+.1f}%** | subject |",
        "",
        f"Correlations (30d): BTC↔Nasdaq **{s['corr_btc_nasdaq_30d']}** | "
        f"BTC↔Gold **{s['corr_btc_gold_30d']}** | BTC/Gold ratio {s['btc_gold_ratio']}",
        "",
        "## A-score — macro pressure release",
        "",
        fmt_check(s["A"]),
        "- ➖ A3 rate-hike odds: not in free data (check CME FedWatch manually)",
        "",
        "## B-score — seller exhaustion",
        "",
        fmt_check(s["B"]),
        "- ➖ B2 funding / B3 ETF flows / B4 on-chain: Tier-2 feeds, not yet wired",
        "",
        "## Support / Resistance (confluence-rated)",
        "",
        "| Side | Level | Methods | Strength |",
        "|---|---|---|---|",
    ]
    for z in s["resistances"][::-1]:
        L.append(f"| resistance | ${z['level']:,.0f} | {', '.join(z['methods'])} | {z['strength']} |")
    L.append(f"| **price** | **${s['price']:,.0f}** | | |")
    for z in s["supports"]:
        L.append(f"| support | ${z['level']:,.0f} | {', '.join(z['methods'])} | {z['strength']} |")
    L.append("")

    L.append("## Reddit sentiment")
    L.append("")
    if reddit and reddit.get("buckets"):
        for k, v in reddit["buckets"].items():
            spike = " ⚠ SPIKE" if k in (reddit.get("spikes") or {}) else ""
            L.append(f"- {k}: {v}{spike}")
        L.append(f"- _as of {reddit['fetched_at']}_")
    else:
        L.append("_No data yet (Reddit currently blocks this network — tracker is "
                 "armed and will populate when reachable)._")
    L.append("")

    L.append("## Recent TradingView alerts")
    L.append("")
    if alerts:
        for a in alerts[::-1]:
            p = a.get("payload", {})
            L.append(f"- `{a.get('received_at','')}` — "
                     f"{p.get('signal', p.get('raw_text', json.dumps(p)[:80]))}")
    else:
        L.append("_None yet — wire btc_genius.pine alerts to the webhook listener._")
    L.append("")
    DASHBOARD.write_text("\n".join(L))


def main():
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(filename=str(LOG_FILE), level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")
    logging.info("run start")

    data = {}
    for name, sym in SERIES.items():
        try:
            data[name] = fetch_daily(sym)
        except Exception as e:  # noqa: BLE001
            logging.error("history fetch failed for %s (%s): %s", name, sym, e)
            return  # all six are required for the framework; retry next hour
        time.sleep(0.5)

    s = compute_signals(data)
    ts = datetime.now(timezone.utc).isoformat(timespec="seconds")

    reddit = latest_reddit()
    alerts = recent_alerts()
    write_dashboard(s, reddit, alerts, ts)

    state = {"updated_at": ts, **{k: v for k, v in s.items()
                                  if k not in ("supports", "resistances")},
             "verdict": model_call(s),
             "supports": [{"level": round(z["level"]), "strength": z["strength"]}
                          for z in s["supports"]],
             "resistances": [{"level": round(z["level"]), "strength": z["strength"]}
                             for z in s["resistances"]]}
    STATE.write_text(json.dumps(state, indent=2) + "\n")

    # Raw daily series for the chart dashboard (served by webhook_listener).
    extras = {}
    for name, sym in EXTRA_SERIES.items():
        try:
            extras[name] = fetch_daily(sym)
        except Exception as e:  # noqa: BLE001 - chart extras are best-effort
            logging.warning("extra series %s (%s) failed: %s", name, sym, e)
        time.sleep(0.5)
    series = {name: {"ts": d["ts"], "close": [round(c, 2) for c in d["close"]]}
              for name, d in {**data, **extras}.items()}
    series["btc"]["volume"] = data["btc"]["volume"]
    series["btc"]["sr"] = ([{"level": round(z["level"]), "side": "support",
                             "strength": z["strength"]} for z in s["supports"]] +
                           [{"level": round(z["level"]), "side": "resistance",
                             "strength": z["strength"]} for z in s["resistances"]])
    SERIES_OUT.write_text(json.dumps(series) + "\n")

    # Long history (weekly, 10y — Yahoo degrades 'max' to monthly bars) for the
    # 5Y/ALL chart toggles — refreshed at most daily since old bars never change.
    long_out = DATA_DIR / "series_long.json"
    stale = (not long_out.exists()
             or time.time() - long_out.stat().st_mtime > 24 * 3600)
    if stale:
        longs = {}
        for name, sym in {**SERIES, **EXTRA_SERIES}.items():
            try:
                d = fetch_daily(sym, rng="10y", interval="1wk")
                longs[name] = {"ts": d["ts"],
                               "close": [round(c, 2) for c in d["close"]]}
            except Exception as e:  # noqa: BLE001 - chart extras are best-effort
                logging.warning("long series %s (%s) failed: %s", name, sym, e)
            time.sleep(0.5)
        if longs:
            long_out.write_text(json.dumps(longs) + "\n")
    auto_journal(s)
    hist_row = {"ts": ts, "price": s["price"], "regime": s["regime"],
                "a_score": s["a_score"], "b_score": s["b_score"],
                "corr_nq": s["corr_btc_nasdaq_30d"]}
    with HISTORY.open("a") as f:
        f.write(json.dumps(hist_row) + "\n")
    db.save_signal(hist_row)
    logging.info("run end: regime=%s A=%d/%d B=%d/%d price=%.0f",
                 s["regime"], s["a_score"], s["a_max"],
                 s["b_score"], s["b_max"], s["price"])


if __name__ == "__main__":
    main()
