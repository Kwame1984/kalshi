# Kalshi Algorithmic Trading System — Design

Date: 2026-07-06
Constraints from user: retail-scale account; demo-first (live trading gated
behind explicit ack); focus on economics/finance and weather markets; Kalshi
account exists, API key to be generated when needed.

## Goal

A bot + web UI that trades Kalshi event contracts algorithmically, with every
strategy validated against historical data before it may touch paper trading,
and paper-validated before it may touch demo/live.

## Research conclusions that shape the design

1. **Fees dominate at this scale.** Taker fee = ceil_cents(0.07·C·P·(1−P)),
   maker = 25% of that (since Jul 2025). Round-up-per-fill makes contracts
   under ~12–15c a "fee death zone." Strategies must be fee-aware in signal
   generation, prefer maker execution, and prefer hold-to-settlement
   (settlement is fee-free).
2. **The favorite-longshot bias is domain-dependent** (Le 2026): favorites
   underpriced in politics (+0.151 calibration intercept), OVERpriced in
   weather (−0.086). Strategy sign must be per-domain; our own backtest
   confirmed naive favorite-buying loses on weather brackets.
3. **Weather is the best-evidenced retail edge** (documented live bot with
   Sharpe ~2.2) but requires ensemble forecasts. Our measured result: a
   deterministic Open-Meteo blend with per-station bias/sigma calibration has
   log-loss 0.408 vs the market's 0.325 at lead 1 (n=2,814) — no edge. The
   ensemble upgrade is built and validated separately; the strategy stays
   disabled unless it beats the market out-of-sample.
4. **Macro fair-value vs nowcasts is a trap**: Fed research shows Kalshi
   already beats fed funds futures and survey consensus. Econ/finance edge
   comes instead from: theta/calibration harvesting, structural arb, and
   options-implied RV on index brackets (INX/Nasdaq have HALF fees, 0.035).
5. **Kalshi API specifics**: live API window is ~3 months (older data via
   /historical/*); order placement is V2 single-book (side=bid/ask, dollar
   strings, mandatory self-trade prevention); WebSocket needs auth even for
   public channels; rate limits token-bucket (Basic ≈ 20 reads/s).
6. **No public L2 orderbook archive exists.** Recording our own book depth is
   a durable proprietary asset; start as soon as API keys exist.
7. **Liquidity Incentive Program** pays for resting depth through Sept 2026 —
   a time-boxed reason to build maker/MM capability later.

## Architecture

```
kalshi_bot/
  api/client.py        async Kalshi client (public + RSA-PSS signed), prod/demo hosts
  config.py            Settings (.env), TradingMode {demo,paper,live}, risk limits
  data/
    store.py           SQLite (data/kalshi.db): markets, candles, trades,
                       external_signals, bot_* state tables
    collector.py       backfill + snapshot for target series
    external/          weather forecasts (Open-Meteo prev-runs + ensemble),
                       IEM CLI settlement truth, calibration
  backtest/
    fees.py            taker/maker quadratic fee model
    engine.py          event-driven candle replay, conservative taker fills,
                       settlement P&L, metrics (return/Sharpe/DD/hit/fees)
    runner.py          load SQLite -> run strategy -> metrics JSON
  strategies/
    base.py            Strategy / MarketState / Signal / Context interfaces
    <one file each>    every strategy consumes the same interfaces so the
                       backtester and live engine run identical code
  execution/
    state.py           bot_* tables (fills, positions, signals, equity, status)
    risk.py            hard caps, kill switch, quarter-Kelly sizing
    paper.py           simulated fills vs live orderbook
    live.py            V2 orders; demo host by default; PROD refuses without
                       TRADING_MODE=live + LIVE_TRADING_ACK=yes
    engine.py          async loop: markets -> strategies -> risk -> executor
  ui/server.py+static  FastAPI + vanilla-JS dashboard: equity, positions,
                       fills, signals, watchlist, kill switch
```

Data flow: collector/external fetchers → SQLite → (backtest runner | execution
engine) → state tables → UI. The Strategy interface is the seam: identical
strategy code in backtest and live.

## Strategy roster (status at design time)

| # | Strategy | Domain | Status |
|---|----------|--------|--------|
| 1 | Weather ensemble fair value | weather | built; **gated on validation** (must beat market log-loss OOS) |
| 2 | Theta/expiry convergence (95–99c, domain-filtered, correlation-capped) | econ + all | to build; naive version backtested negative on weather — domain filter is the fix |
| 3 | Calibration harvest (domain-aware FLB: fade weather longshots, buy non-weather favorites) | cross | to build on same scanner |
| 4 | Intra-event structural arb (bracket sums vs 100c, monotonicity) | econ + weather | to build as scanner overlay; maker-only economics |
| 5 | Index bracket RV vs options-implied density | finance (INX/Nasdaq, half fees) | later phase; needs options feed |
| 6 | Low-liquidity maker reversion | cross | later; entry-timing overlay only |
| 7 | LIP depth harvesting | cross | later; time-boxed to Sept 2026 |

Rule: **no strategy is enabled for paper/demo/live unless its backtest is
positive after taker fees over ≥90 days and its signal adds information vs
the market price** (log-loss test where applicable). Paper → demo → live each
require explicit human promotion.

## Risk rails (all enforced in code, not convention)

- Hard caps from .env: per-market $50, total exposure $300, daily loss $25.
- Kill switch in bot_status, honored every engine cycle, settable from UI.
- Quarter-Kelly sizing off (fair_value − price) edge, capped by per-market cap.
- LIVE requires TRADING_MODE=live AND LIVE_TRADING_ACK=yes; LiveExecutor
  refuses the prod host otherwise. Demo host is the default authed target.
- One position per event for bracket strategies (no doubling across brackets).

## Testing

- Unit: fee math, risk caps, order mapping, strike-bound semantics (verified
  against settled markets), UI endpoints.
- Backtests are the strategy test harness; results stored as JSON in
  data/backtests/ and surfaced in the UI.
- Paper mode against live books is the integration test for execution.

## Known gaps / next phases

- WebSocket L2 recorder (needs API key) — start immediately after key exists.
- Deep history via /historical/* + HF trade archive for multi-year validation.
- Ensemble validation may fail — weather then stays research-only.
- Options-implied RV needs a data source decision (Deribit free vs paid OPRA).
