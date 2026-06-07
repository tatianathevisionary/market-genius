# BTC Genius — MVP Indicator Tracking Plan

Goal: monitor Bitcoin in real time alongside a small set of macro indicators that
explain *why* BTC is moving, not just *that* it's moving. Today's market (Jun 2026)
is a perfect example of the chain we want to capture:

> Middle East conflict → Brent crude spikes → inflation fears → 10Y Treasury yield
> rises → rate-hike odds up → risk assets (Nasdaq, S&P) sell off → BTC sells off harder.

If we only tracked BTC price, we'd see a -6% candle with no context. Tracking the
chain lets us see the move coming one or two links earlier.

---

## Tier 1 — The MVP (track these first)

Six series. Each one is a link in the macro → crypto transmission chain.

### 1. Bitcoin price + volume (BTC/USD) — the subject
- **What:** Spot price AND traded volume, real-time (websocket from an exchange
  or aggregator). Volume ideally aggregated across major venues (Binance,
  Coinbase, Bybit) — single-exchange volume can mislead.
- **Why price:** The thing we're monitoring. Also derive 24h %, 7d %, and distance
  from recent high/low — "lowest since March" is a *level* story, not just a % story.
- **Why volume:** Price tells you *what* happened; volume tells you *how much
  conviction* was behind it. A -6% day on 3× average volume is capitulation; the
  same -6% on thin volume is apathy that can keep drifting. Volume also validates
  levels: a breakout or breakdown without a volume expansion is suspect, and
  high-volume price zones become future support/resistance (see SIGNALS.md §6).

### 2. Brent crude oil — the shock originator
- **What:** Front-month Brent futures (the global benchmark; WTI is the US one).
- **Economic significance:** Oil is the classic *exogenous inflation shock*.
  Geopolitical supply disruption (e.g., Strait of Hormuz ≈ 20% of world supply)
  pushes oil up regardless of what central banks do.
- **Relationship to BTC:** *Indirect but powerful.* Oil ↑ → expected inflation ↑ →
  yields ↑ → rate cuts priced out → speculative assets (BTC) ↓. Oil rarely moves
  BTC directly; it moves the things that move BTC.

### 3. US 10-Year Treasury yield — the transmission mechanism
- **What:** 10Y yield (and ideally the 2Y for the short-rate/Fed-expectations view).
- **Economic significance:** The "risk-free rate" that everything is priced
  against. It encodes inflation expectations + Fed policy expectations in one number.
- **Relationship to BTC:** *Strongly inverse for risk appetite.* Yields ↑ means
  (a) holding a zero-yield asset like BTC costs more in opportunity terms, and
  (b) markets expect tighter policy → less liquidity → less speculation.
  The 10Y at 4.5% was the headline number in today's selloff.

### 4. Nasdaq-100 / S&P 500 — the risk-appetite proxy
- **What:** Index level (real-time futures: NQ / ES give 24h coverage, matching
  BTC's always-open market better than cash indices).
- **Economic significance:** Equities, especially tech, are the broad measure of
  risk-on vs. risk-off. BTC has historically traded as a *high-beta tech stock*.
- **Relationship to BTC:** Usually *positively correlated*… until it isn't.
  The current regime is the interesting case: S&P +5% on the month while BTC -16%.
  **Correlation breakdown is itself a signal** — it tells you BTC is being driven
  by crypto-specific flows (ETF outflows, momentum rotation) rather than macro.
  Track the rolling 30d correlation, not just the levels.

### 5. US Dollar Index (DXY) — the denominator
- **What:** DXY (dollar vs. basket of major currencies).
- **Economic significance:** Global liquidity gauge. A strong dollar tightens
  financial conditions worldwide.
- **Relationship to BTC:** *Historically inverse.* BTC is priced in dollars and
  partly sold as a dollar-debasement hedge; DXY ↑ tends to mean BTC ↓.
  Also closes the loop with #3: yields ↑ → dollar ↑ → BTC ↓.

### 6. Gold — the hedge competitor
- **What:** Spot gold (XAU/USD).
- **Economic significance:** The incumbent safe-haven / inflation hedge.
- **Relationship to BTC:** The *"digital gold" test*. In a geopolitical/inflation
  scare, if gold ↑ while BTC ↓ (exactly today's pattern), the market is saying BTC
  is trading as a **risk asset, not a hedge**. The gold/BTC ratio is a clean
  one-number summary of which narrative is winning.

### The relationship map

```
            geopolitical shock
                   │
                   ▼
        ┌──── Brent crude ↑ ────┐
        │                       │
        ▼                       ▼
  inflation expectations ↑   Gold ↑ (safe haven)
        │                       ▲
        ▼                       │  divergence = BTC failing
  10Y yield ↑ ──→ DXY ↑         │  the "digital gold" test
        │           │           │
        ▼           ▼           │
   Nasdaq/S&P ↓ ←───┘           │
        │                       │
        ▼                       │
       BTC ↓ ───────────────────┘
   (high-beta risk asset)
```

---

## Tier 2 — Crypto-internal signals (add after MVP works)

These explain the *crypto-specific* part of moves that macro can't (the current
regime, where BTC falls while stocks rally, is mostly a Tier 2 story).

| Indicator | What it tells you |
|---|---|
| **Spot BTC ETF net flows** (IBIT etc., daily) | Institutional/retail demand. The $1.26B IBIT block sale on May 26 was a bigger BTC-specific event than anything macro that day. |
| **ETH/BTC ratio** | Risk appetite *within* crypto. Falling = defensive even inside crypto. |
| **Perp funding rates** | Leverage and positioning. Deeply negative = crowded shorts (squeeze fuel); high positive = froth. |
| **Stablecoin total market cap** | Dry powder sitting inside the crypto ecosystem. Growing = money waiting to deploy. |
| **BTC dominance %** | Whether capital is rotating into BTC (flight to quality) or out of crypto entirely. |

## Tier 3 — Later / nice-to-have

- **Fed funds futures (rate-hike/cut odds)** — the cleanest read on policy
  expectations; today "higher odds of a hike than a cut" was the key sentence.
- **VIX** — equity fear gauge; spikes lead risk-off cascades.
- **2Y/10Y spread (yield curve)** — recession vs. inflation regime.
- **On-chain cohorts (realized losses, top-buyer capitulation)** — e.g., the
  Compass Point "26% of sales from >$90k buyers" capitulation signal.

---

## Why this exact MVP set

1. **It covers the full transmission chain** — shock origin (oil), mechanism
   (yields, DXY), risk appetite (equities), competitor narrative (gold), subject (BTC).
2. **Every series is free and real-time-ish** — exchange websockets for BTC,
   futures quotes or 1-min-delayed feeds for the rest. No paid terminal needed.
3. **Six series is enough to compute the derived signals that matter:**
   - rolling BTC↔Nasdaq correlation (regime detection)
   - rolling BTC↔gold correlation (hedge vs. risk-asset test)
   - BTC/gold ratio
   - "macro-explained vs. unexplained" — when BTC moves and *nothing* in the
     other five does, the cause is crypto-internal → check Tier 2.

## Suggested cadence

| Series | Refresh |
|---|---|
| BTC | real-time (websocket) |
| Brent, NQ/ES futures, DXY, gold | 1–5 min |
| 10Y yield | 5–15 min (it moves slowly intraday) |
| Derived correlations | recompute hourly on daily closes (30d window) |
