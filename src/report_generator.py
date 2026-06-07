#!/usr/bin/env python3
"""
BTC Genius — daily analyst report skeleton generator.

Pulls the current engine state, whale ledger, and news wire into a dated
markdown skeleton under data/reports/YYYY-MM-DD.md with the numbers filled
in and ANALYST NOTE placeholders where human (or Claude) synthesis goes.
Run, then edit the file — or ask Claude to "complete today's report".

Usage: python3 report_generator.py [--force]
Stdlib only; Python 3.9+.
"""

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent  # src/ -> project root
DATA_DIR = BASE_DIR / "data"
REPORTS = DATA_DIR / "reports"

NOTE = "> **ANALYST NOTE:** _fill in synthesis here._"


def read_json(p, default=None):
    try:
        return json.loads(p.read_text())
    except (OSError, json.JSONDecodeError):
        return default


def read_jsonl(p, n=50):
    if not p.exists():
        return []
    out = []
    for line in p.read_text().strip().splitlines()[-n:]:
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return out


def main():
    today = datetime.now(timezone.utc).date().isoformat()
    out_path = REPORTS / f"{today}.md"
    if out_path.exists() and "--force" not in sys.argv:
        print(f"{out_path} already exists (use --force to overwrite)")
        return

    s = read_json(DATA_DIR / "state.json", {})
    v = s.get("verdict", {})
    r5 = s.get("roc5", {})
    whales = read_jsonl(DATA_DIR / "whale_ledger.jsonl", 20)
    news = read_json(DATA_DIR / "x_cache.json", {})  # social tone, best-effort

    mayer = round(s.get("price", 0) / s["ma200"], 2) if s.get("ma200") else "—"
    res = " · ".join(f"${z['level']:,} (×{z['strength']})" for z in s.get("resistances", []))
    sup = " · ".join(f"${z['level']:,} (×{z['strength']})" for z in s.get("supports", [])) or "none — at 1y lows"

    whale_lines = "\n".join(
        f"- {w.get('ts', '')} · {w.get('source', '')} · {w.get('side', '')} · ${w.get('usd', 0):,}"
        for w in whales[-10:]) or "- no large transactions logged"

    tone = ""
    if news.get("tweets"):
        c = {"bull": 0, "bear": 0, "neutral": 0}
        for t in news["tweets"]:
            c[t.get("sentiment", "neutral")] = c.get(t.get("sentiment", "neutral"), 0) + 1
        tone = f"Social tone ({news.get('source', '?')}): 🟢{c['bull']} / 🔴{c['bear']} / ⚪{c['neutral']}"

    REPORTS.mkdir(parents=True, exist_ok=True)
    out_path.write_text(f"""# BTC Daily Analyst Report — {today}

**Price:** ${s.get('price', 0):,.0f} · **5d:** {r5.get('btc', '—')}% · **Regime:** {s.get('regime', '—')} · \
**Verdict:** {v.get('direction', '—')} (bias {v.get('bias', '—')}, {v.get('confidence_pct', '—')}%, {v.get('horizon_days', '—')}d horizon)
**Scores:** A {s.get('a_score', '—')}/{s.get('a_max', '—')} · B {s.get('b_score', '—')}/{s.get('b_max', '—')} · \
**Mayer:** {mayer} · **vs 50d MA:** {s.get('dist_from_50d_pct', '—')}%

---

## Executive summary

{NOTE}

## Market structure

- **Resistance:** {res}
- **Support:** {sup}
- **Volume vs 30d avg:** {s.get('volume_vs_30d_avg', '—')}×
- **MAs:** 50d ${s.get('ma50', 0):,.0f} · 200d ${s.get('ma200', 0):,.0f}

{NOTE}

## Macro transmission

| Series | 5d | Read |
|---|---|---|
| Brent | {r5.get('brent', '—')}% | |
| US 10Y | {r5.get('us10y', '—')}% | |
| DXY | {r5.get('dxy', '—')}% | |
| Nasdaq fut | {r5.get('nasdaq', '—')}% | |
| Gold | {r5.get('gold', '—')}% | |
| **BTC** | **{r5.get('btc', '—')}%** | |

Correlations 30d: BTC↔Nasdaq **{s.get('corr_btc_nasdaq_30d', '—')}** · BTC↔Gold **{s.get('corr_btc_gold_30d', '—')}**

{NOTE}

## Flow & whale ledger (latest)

{whale_lines}

{NOTE}

## Sentiment

{tone}

{NOTE}

## Scenarios & triggers

| Scenario | Trigger | Action |
|---|---|---|
| | | |

## Verdict

**{v.get('direction', '—')}.** {v.get('thesis', '').replace('[auto] ', '')}

{NOTE}

---

*Educational model output + analyst synthesis. Not financial advice. Auto-graded against
price at horizon in the journal.*
""")
    print(f"wrote {out_path}")


if __name__ == "__main__":
    main()
