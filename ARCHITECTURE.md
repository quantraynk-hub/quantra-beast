
QUANTRA BEAST — COMPLETE SYSTEM ARCHITECTURE
============================================
Version: 2.0 | Built for NSE Options Trading
Architecture: Production-grade, Stage-by-Stage

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
FOLDER STRUCTURE
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

quantra_v2/
│
├── core/
│   ├── data_fetcher.py       ← Market data (NSE via ScraperAPI)
│   ├── cache.py              ← In-memory + file cache
│   └── scheduler.py          ← Continuous data refresh loop
│
├── engines/
│   ├── trend_engine.py       ← EMA, price action, momentum
│   ├── options_engine.py     ← PCR, OI buildup, IV analysis
│   ├── gamma_engine.py       ← GEX calculation per strike
│   ├── volatility_engine.py  ← VIX levels, IV percentile
│   ├── regime_engine.py      ← Market regime classifier
│   ├── sentiment_engine.py   ← A/D ratio, breadth, FII/DII
│   └── flow_engine.py        ← Institutional flow tracker
│
├── signals/
│   ├── fusion_engine.py      ← Weighted signal combiner
│   ├── signal_validator.py   ← Filters weak/conflicting signals
│   └── strike_selector.py    ← Optimal strike + expiry picker
│
├── monitoring/
│   ├── trade_monitor.py      ← Live position tracker
│   ├── exit_engine.py        ← SL/TP/trailing exit logic
│   └── alert_engine.py       ← Price level alerts
│
├── capital/
│   ├── position_sizer.py     ← Lot calculation per risk%
│   ├── risk_manager.py       ← Max loss/day, drawdown rules
│   └── portfolio_tracker.py  ← Open positions + P&L
│
├── database/
│   ├── db.py                 ← SQLite connection + schema
│   ├── trade_store.py        ← Save/load trades
│   ├── signal_store.py       ← Save every signal generated
│   └── performance.py        ← Win rate, engine accuracy stats
│
├── api/
│   └── server.py             ← Flask API (connects to dashboard)
│
├── learning/
│   └── weight_adjuster.py    ← Adjusts engine weights over time
│
├── requirements.txt
├── render.yaml
└── .python-version

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
STAGE BUILD PLAN
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

STAGE 1 — DATA PIPELINE (Day 1)
--------------------------------
Files: core/data_fetcher.py, core/cache.py
What: Reliable NSE data via ScraperAPI
Data fetched:
  - NIFTY/BANKNIFTY/FINNIFTY spot price
  - Full options chain (all strikes, all expiries)
  - India VIX
  - FII/DII flows
  - Top gainers/losers
  - Advance/Decline ratio
Caching: 60 second TTL (no hammering NSE)
Error handling: retry logic, fallback values
Output: clean structured dict every 60 seconds

STAGE 2 — ANALYSIS ENGINES (Days 2-3)
--------------------------------------
Files: engines/*.py
Each engine is independent, takes market data, returns:
  {
    score: -100 to +100,
    signal: BULL / BEAR / NEUTRAL,
    confidence: 0-100,
    reasons: [list of strings],
    data: {raw metrics}
  }

Engine details:

TREND ENGINE
  Inputs: spot price, open, high, low, prev close
  Logic:
    - Price vs previous close (momentum)
    - Position in day's range (high-low %)
    - Gap up/down detection
    - Opening range breakout
  Score: +80 = strong bull, -80 = strong bear

OPTIONS ENGINE  
  Inputs: full option chain OI data
  Logic:
    - PCR (Put-Call Ratio) analysis
    - Max pain calculation
    - OI concentration at strikes
    - OI change (buildup vs unwinding)
    - IV skew (CE IV vs PE IV)
  Score: based on PCR + OI buildup direction

GAMMA ENGINE
  Inputs: OI per strike, spot price
  Logic:
    - GEX = OI × gamma approximation per strike
    - Positive GEX = market maker pins price
    - Negative GEX = market maker amplifies moves
    - Net GEX level and direction
  Score: based on net GEX at current spot

VOLATILITY ENGINE
  Inputs: India VIX, historical IV data
  Logic:
    - VIX level classification (low/normal/high/extreme)
    - VIX direction (rising = bearish, falling = bullish)
    - IV Rank (current IV vs 52-week range)
    - Option buying vs selling recommendation
  Score: adjusts signal strength, not direction

REGIME ENGINE
  Inputs: VIX, trend score, range data
  Logic:
    - Trending bull: VIX low, price rising
    - Trending bear: VIX rising, price falling
    - Ranging: price oscillating, VIX flat
    - Volatile: VIX high, large swings
  Output: regime label (affects all other engines)

SENTIMENT ENGINE
  Inputs: FII/DII data, advance/decline ratio
  Logic:
    - FII net buy/sell value and direction
    - DII net activity
    - Market breadth (advancing vs declining stocks)
    - Combined institutional bias
  Score: +85 = strong FII buying, -85 = heavy selling

FLOW ENGINE
  Inputs: OI change data, large strike activity
  Logic:
    - Identify strikes with sudden large OI addition
    - Detect unusual call/put buying
    - Track which side institutions are writing
    - Unwinding vs fresh buildup
  Score: based on smart money direction

STAGE 3 — SIGNAL FUSION (Day 4)
---------------------------------
File: signals/fusion_engine.py

DEFAULT WEIGHTS (adjustable):
  trend_engine:       0.18
  options_engine:     0.20
  gamma_engine:       0.12
  volatility_engine:  0.10  (modifier only)
  regime_engine:      0.08  (modifier only)
  sentiment_engine:   0.15
  flow_engine:        0.17

FUSION LOGIC:
  1. Collect all 7 engine scores
  2. Apply weights → composite score
  3. Regime adjusts confidence (not score)
  4. Volatility adjusts strike selection (not direction)
  5. Threshold logic:
     composite > 25  → BUY CE
     composite < -25 → BUY PE
     abs < 25        → WAIT or HEDGE
     abs(conf) < 50  → WAIT (low confidence)

SIGNAL OUTPUT:
  {
    action: BUY CE / BUY PE / BUY BOTH / WAIT,
    composite_score: float,
    confidence: 0-100,
    engine_scores: {all 7},
    reasons: [top 5 reasons],
    ce_strike: int or null,
    pe_strike: int or null,
    expiry: date string,
    signal_id: uuid,
    timestamp: datetime,
  }

STAGE 4 — TRADE MONITORING (Day 5)
------------------------------------
File: monitoring/trade_monitor.py

After user confirms trade:
  Every 60 seconds check:
    - Current option LTP vs entry price
    - Current P&L (lots × qty × price diff)
    - % move from entry
    - Time decay warning (DTE < 2 days)
    - VIX spike warning

EXIT TRIGGERS:
  - Target hit: LTP >= target price → EXIT signal
  - SL hit: LTP <= SL price → EXIT signal  
  - Trailing SL: if profit > 50% → trail SL to breakeven
  - Time-based: DTE = 0 → force exit warning
  - VIX spike: VIX > 20 suddenly → partial exit alert
  - Trend reversal: signal flips → alert user

ACTIONS displayed to user:
  HOLD — within normal range
  BOOK PARTIAL — 50% profit, trail rest
  EXIT NOW — SL or target hit
  REVERSE — strong signal flip detected

STAGE 5 — CAPITAL MANAGEMENT (Day 6)
--------------------------------------
File: capital/risk_manager.py

RULES ENGINE:
  Per trade:
    - max risk = capital × risk%
    - lots = max_risk / (entry_price × lot_size × sl%)
    - never more than 30% capital in one trade

  Per day:
    - max loss/day = capital × 3% (hard stop)
    - max trades = user setting (default 3)
    - if max_daily_loss hit → NO MORE SIGNALS today
    - if 2 losses in a row → reduce position size 50%

  Drawdown protection:
    - if drawdown > 10% → pause system, alert user
    - if drawdown > 15% → system locked until next day

STAGE 6 — DATABASE (Day 7)
----------------------------
File: database/db.py (SQLite — free, no setup)

TABLES:
  trades:
    id, signal_id, action, index, ce_strike, pe_strike,
    lots, entry_price, exit_price, pnl, exit_type,
    entry_time, exit_time, capital_before, capital_after

  signals:
    id, timestamp, action, composite_score, confidence,
    engine_scores (JSON), reasons (JSON), market_data (JSON),
    was_taken (bool), result (WIN/LOSS/SKIP/OPEN)

  capital_log:
    id, timestamp, event, amount, balance, note

  performance:
    date, total_signals, taken, wins, losses, net_pnl,
    win_rate, avg_win, avg_loss, best_engine, worst_engine

STAGE 7 — LEARNING ENGINE (Week 2)
------------------------------------
File: learning/weight_adjuster.py

After every 10 trades:
  1. Look at which engines were right vs wrong
  2. Calculate per-engine accuracy score
  3. Increase weight of accurate engines
  4. Decrease weight of inaccurate engines
  5. Constraints: no engine < 0.05, no engine > 0.35
  6. Save new weights to database
  7. Dashboard shows weight evolution over time

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
RISK MANAGEMENT RULES (NON-NEGOTIABLE)
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

1. Never risk more than 2% per trade (default)
2. Never exceed 3 open trades simultaneously  
3. Hard stop at 3% daily loss
4. System pauses after 2 consecutive losses
5. No trading in first 15 min (9:15–9:30)
6. No trading in last 30 min on expiry day
7. VIX > 25 → only PE buying or no trade
8. Expiry day → reduce position size 50%

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
DATA FLOW (RUNTIME)
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

Every 60 seconds:
  data_fetcher.py fetches NSE data
        ↓
  cache.py stores with timestamp
        ↓
  All 7 engines recalculate scores
        ↓
  fusion_engine.py combines → composite score
        ↓
  If score crosses threshold:
    signal_validator.py checks quality
        ↓
    signal stored in database
        ↓
    Dashboard shows signal to user
        ↓
  If active trade exists:
    trade_monitor.py checks P&L + exits
        ↓
    If exit condition met → alert user
        ↓
  capital/risk_manager.py updates exposure
        ↓
  database saves all activity

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
DEPLOYMENT
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

Backend: Render free tier (Python/Flask)
Database: SQLite file on Render disk
Dashboard: HTML file (open in browser)
Keep-alive: cron-job.org pings every 5 min
Data source: NSE via ScraperAPI (free tier)

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
