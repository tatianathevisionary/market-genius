# BTC Genius — Entry/Exit Signal Framework

Companion to [INDICATORS.md](INDICATORS.md). That doc defines *what we watch*;
this one defines *what we do with it*: how to detect when a selloff is exhausting,
when conditions favor entry, and when they favor exit.

Core principle: **no single signal is tradeable — confluence is.** Each signal
below is a boolean we can compute from data we already track. Entries/exits fire
when several flip at once. (These are conditions that historically precede
turns, not guarantees — the framework's job is to stack odds, not predict.)

---

## 1. First, name the regime

Every signal means something different depending on regime. Classify each day:

| Regime | Definition (computable) |
|---|---|
| **Macro-driven selloff** | BTC ↓ AND (oil ↑ or 10Y ↑ or DXY ↑) AND Nasdaq ↓ — BTC moving *with* the chain. Today's pattern. |
| **Crypto-specific selloff** | BTC ↓ while Nasdaq flat/up and macro calm — driver is internal (ETF outflows, momentum rotation). The May pattern. |
| **Basing** | Price flat (±3% over 2 weeks), realized vol falling, funding ~0 |
| **Risk-on uptrend** | BTC ↑ with rising ETF inflows and positive (but not extreme) funding |
| **Froth** | BTC ↑ with extreme funding, vertical price, dominance falling |

The current market is a **macro-driven selloff layered on a crypto-specific bear**
(momentum rotated to AI/IPOs since October). Both have to resolve.

---

## 2. "When will this stop?" — the bottoming checklist

A bottom needs two things: **the macro pressure releases** AND **sellers exhaust**.
Score each item 0/1; the more that flip, the closer the turn.

### A. Macro pressure release (the cause stops)

| # | Signal | Condition | Why it matters |
|---|---|---|---|
| A1 | Oil rolls over | Brent falls >5% from its spike high, or Strait headlines de-escalate | Removes the inflation-shock input at the top of the chain |
| A2 | Yields peak | 10Y makes a lower high over 5+ sessions / falls back below the breakout level (4.5% today) | The transmission mechanism relaxes |
| A3 | Rate-hike odds fade | Fed funds futures shift back from hike toward hold/cut | The single sentence that drove today's selloff, reversed |
| A4 | DXY tops | DXY lower high / rolls over | Dollar liquidity loosening |
| A5 | Equities stabilize | Nasdaq holds a higher low for 3+ sessions | Risk appetite returning — BTC won't bottom while tech is still falling |

### B. Seller exhaustion / capitulation (the sellers stop)

| # | Signal | Condition | Why it matters |
|---|---|---|---|
| B1 | Capitulation volume | Daily volume >2× 30d average on a down day with a long lower wick | Forced/panic sellers clear out in one event |
| B2 | Funding deeply negative | Perp funding < −0.01%/8h across major venues | Crowd is short → squeeze fuel; bottoms rarely form on positive funding |
| B3 | ETF outflows climax → flip | Largest outflow day of the cycle, then first net-inflow day | Institutional selling exhausts; the May 26 $1.26B IBIT block was this kind of event |
| B4 | Top-buyer cohort done selling | Realized-loss spikes from the >$90k cohort fade (the Compass Point "26% of sales" signal) | The cohort that capitulates marks the late stage of a bear — when they're done, supply dries up |
| B5 | Correlation re-couples | 30d BTC↔Nasdaq correlation turns back up from its breakdown | BTC rejoining the risk complex = idiosyncratic selling finished |
| B6 | Failed new low | Price undercuts the prior low (e.g., $62.6k) and reclaims it within 1–2 sessions | Classic spring/stop-run — last sellers trapped |

**Rule of thumb:** the *durable* bottom needs ≥3 from column A and ≥3 from column B.
Column B alone (capitulation without macro relief) tends to produce a bounce, not a bottom.

---

## 3. Entry signals

Two distinct entry types — don't mix their rules.

### Entry type 1: Capitulation reversal (aggressive, counter-trend)
Fires during a selloff. Higher risk, best R:R.
- **Trigger:** B1 + B2 + B6 within the same ~3-session window
- **Confirmation:** funding stays negative while price stops falling (shorts pressing into a market that won't go down)
- **Invalidation (hard exit):** a daily close below the capitulation wick low
- **Sizing logic:** smallest size of any entry type — you're catching a knife with a checklist

### Entry type 2: Basing breakout (conservative, trend-following)
Fires weeks after the low. Lower R:R, much higher hit rate.
- **Trigger:** regime = Basing for ≥2 weeks, THEN price breaks above the range high on >1.5× average volume
- **Confirmation:** ETF flows net-positive over trailing 5 days; A-column score ≥3 (macro tailwind, not headwind)
- **Invalidation:** close back inside the base range
- **Seasonality filter:** summer is historically BTC's weakest period — demand an extra confirmation (e.g., A3) for breakouts triggering June–August; the same setup in Q4 needs less

### Entry veto (overrides everything)
Do not enter — even on a perfect checklist — while:
- Oil is making new spike highs (the shock is still live), or
- 10Y is rising >10bps/day (bond market repricing in progress), or
- A live headline-risk window is open (active strait escalation, Fed meeting <48h away)

---

## 4. Exit signals

### Exit type 1: Invalidation (defensive — always armed)
- Price closes below the entry's invalidation level → exit, no discretion.
- Macro re-deterioration: A-column score drops back below 2 (e.g., oil re-spikes) → tighten stops or cut.

### Exit type 2: Froth / distribution (profit-taking in uptrends)

| # | Signal | Condition | Why it matters |
|---|---|---|---|
| X1 | Funding extreme positive | > +0.05%/8h sustained for days | Longs crowded and paying heavily — squeeze risk now points down |
| X2 | Vertical extension | Price >25–30% above its 50d average | Momentum exhaustion zone; chasers are the last buyers |
| X3 | ETF inflow climax | Record inflow day that fails to lift price | Demand exhausted — biggest buy pressure of the cycle absorbed with no progress |
| X4 | Dominance breakdown + alt mania | BTC dominance falling fast while low-quality alts go vertical | Late-cycle rotation into trash = classic top texture |
| X5 | Momentum competitor returns | Capital visibly rotating to the *next* hot narrative (the AI/IPO dynamic in reverse) | BTC is a momentum asset; when momentum leaves, price follows — Schwab's whole thesis |
| X6 | Hedge-test failure at highs | Gold ↑ + yields ↑ while BTC stalls at resistance | Macro turning hostile while price can't progress |

**Rule of thumb:** scale out on 2 froth signals, fully exit on 3+. Exits are
gradual and signal-stacked; invalidation exits are binary and immediate.

---

## 5. What the MVP must compute (derived-signal spec)

Everything above reduces to a small set of computed values on top of the
INDICATORS.md feeds:

| Computation | Inputs | Cadence | Feeds signals |
|---|---|---|---|
| Rolling 30d BTC↔Nasdaq correlation | BTC, NQ daily closes | daily | regime, B5 |
| Rolling 30d BTC↔gold correlation + BTC/gold ratio | BTC, XAU | daily | regime, X6 |
| 5-day rate-of-change per macro series | Brent, 10Y, DXY | daily | A1, A2, A4 |
| Volume vs. 30d average | BTC volume | real-time | B1, basing breakout |
| Funding rate aggregate (Binance+Bybit+OKX) | Tier-2 feed | hourly | B2, X1 |
| ETF net flow, trailing 5d sum | Tier-2 feed | daily | B3, X3, entry confirm |
| Distance from 50d average / range highs-lows | BTC price | real-time | X2, B6, breakout |
| **Regime classifier** | all of the above | daily | everything |
| **A-score and B-score (0–6 each)** | all of the above | daily | the dashboard headline |

The MVP dashboard headline is three numbers: **Regime, A-score, B-score.**
Today would read: `Macro-driven selloff | A: 0/5 | B: ~2/6` — i.e., cause still
live, capitulation partially in, no entry. When that drifts toward
`A: 3+ | B: 3+`, the framework starts flagging entry type 1 setups.

---

## 6. Identifying support & resistance (computable methods)

Resistance = price zones where selling has repeatedly overwhelmed buying;
support = the reverse. They matter because entries/exits key off them (B6's
"failed new low", the basing breakout's "range high", X6's "stalls at
resistance"). Five methods, all computable from price/volume history — **a level
confirmed by 2+ independent methods is a real level; a level from one method is
a guess.**

| # | Method | How to compute | Why it works |
|---|---|---|---|
| R1 | **Swing highs/lows** | Local extremes on the daily chart (a high with N lower highs on both sides, e.g., N=5). Recent examples: $74k (June breakdown origin) = resistance; $62.6k (today's low) = support being tested. | Trapped buyers at a prior high sell "to get out at breakeven" when price returns — Ferraioli described exactly this cohort behavior. |
| R2 | **Volume profile (high-volume nodes)** | Histogram of volume *by price level* over a lookback window (90d/1y). Peaks = HVNs (heavy two-way trade → strong S/R); valleys = LVNs (price slips through fast). This is the #1 reason we aggregate volume in Tier 1. | Lots of coins changed hands there → lots of cost bases anchored there → lots of decisions made when price revisits. |
| R3 | **Round numbers** | $60k, $65k, $70k, prior ATH. Trivial to enumerate. | Pure psychology + clustered limit orders and options strikes. Weak alone, strong when coinciding with R1/R2. |
| R4 | **Moving averages** | 50d and 200d simple MAs. Price below both (now) = both act as overhead resistance; reclaiming the 50d is the first trend-repair signal. | Self-fulfilling: enough systematic flows key off them that they behave like levels. |
| R5 | **On-chain cost basis** | Realized-price bands: the average on-chain acquisition price of cohorts (e.g., short-term holders, the >$90k buyer cohort, overall realized price). | The most crypto-native method — it measures *actual* breakeven points, not chart guesses. The $90k cohort capitulating below their cost basis is the Compass Point signal. |

**MVP implementation order:** R1 + R3 + R4 are computable from day one with just
OHLCV history. R2 needs the aggregated volume feed (now in Tier 1). R5 needs an
on-chain data source (Tier 3).

**How the levels plug into the framework:**
- Nearest support below + nearest resistance above = the live trading range →
  defines breakout/breakdown triggers and invalidation levels for §3 entries.
- B6 (failed new low) only counts at an R1/R2-confirmed support.
- X6 (stall) only counts at a 2+-method resistance.
- A break of a major level **on >1.5× average volume** is a regime input;
  the same break on thin volume is a fakeout candidate.

---

## 7. Honest limitations

- **Geopolitics is not in the data.** A strait closure or a ceasefire moves
  everything in minutes; the framework reacts, it cannot anticipate. (A
  prediction-market feed — e.g., odds on oil reaching $120 vs $55 — is the
  closest computable proxy and a decent Tier-3 addition.)
- **Capitulation can repeat.** Bear markets produce multiple "26%-cohort"
  flush events. That's why column A is required, not optional.
- **Correlations are regime-dependent.** The BTC↔Nasdaq link broke this cycle;
  any signal built on it must use the *rolling* value, never the historical
  assumption.
