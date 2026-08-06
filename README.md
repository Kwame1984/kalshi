# kalshi-bot

Algorithmic trading system for [Kalshi](https://kalshi.com) event contracts:
collects market history into SQLite, backtests strategies against it, and runs
the same strategy code live in paper, demo, or real-money mode behind a web
dashboard.

Requires Python 3.12+.

## Setup

```bash
python3 -m venv .venv && .venv/bin/pip install -e ".[dev]"
cp .env.example .env      # only needed for demo/live trading
```

Market data collection, backtesting, and paper trading need **no credentials**.
For demo trading, sign up at demo.kalshi.co, generate an API key in settings,
and put the key ID plus the PEM path in `.env`. For live, the same at
kalshi.com. Keep the PEM outside the repo (`~/.kalshi/` by convention) — `.env`
and `*.pem` are gitignored.

Relevant `.env` keys (see `.env.example` for the full list):

| Key | Purpose |
|-----|---------|
| `KALSHI_API_KEY_ID`, `KALSHI_PRIVATE_KEY_PATH` | Production credentials |
| `KALSHI_DEMO_API_KEY_ID`, `KALSHI_DEMO_PRIVATE_KEY_PATH` | Demo credentials |
| `TRADING_MODE` | `demo` \| `paper` \| `live` |
| `LIVE_TRADING_ACK` | Must be `yes` before live orders are routed |
| `MAX_POSITION_PER_MARKET`, `MAX_TOTAL_EXPOSURE`, `MAX_DAILY_LOSS` | Risk caps, in dollars |
| `ODDS_API_KEY` | Optional; sportsbook lines for `sports_devig` |

## Running

```bash
# 1. collect history into data/kalshi.db (no credentials needed)
.venv/bin/python -m kalshi_bot.data.collector backfill --days 120
.venv/bin/python -m kalshi_bot.data.external.forecast_backfill --days 120
.venv/bin/python -m kalshi_bot.data.external.calibrate_weather

# 2. backtest a strategy over the collected history
.venv/bin/python -m kalshi_bot.backtest.runner --strategy theta_harvest \
    --series KXCPI KXCPIYOY KXFED KXPAYROLLS KXU3

# 3. paper-trade against live prices (simulated fills, no orders sent anywhere)
.venv/bin/python -m kalshi_bot.execution.engine --mode paper \
    --strategies theta_harvest --series KXCPI KXFED

# 4. dashboard at http://localhost:8420
.venv/bin/python -m kalshi_bot.ui.server --port 8420

# read-only scanner, places nothing
.venv/bin/python -m scripts.scan_structural_arb
```

Tests: `.venv/bin/python -m pytest`

## Code layout

```
kalshi_bot/
  api/client.py         Kalshi REST client; RSA-PSS request signing
  config.py             Settings (pydantic-settings); picks demo vs prod creds
  data/
    store.py            SQLite schema + connect(); series, markets, candles, trades
    collector.py        Backfill and incremental market/candle collection
    orderbook_recorder.py  L2 snapshot recorder
    external/           Non-Kalshi inputs: weather forecasts, Deribit, sportsbooks
  strategies/
    base.py             Strategy ABC + MarketState / Signal / Context
    __init__.py         STRATEGIES registry — source of truth for runner + engine
    <name>.py           One module per strategy
  backtest/
    engine.py           Replays stored history through a strategy
    fees.py             Kalshi taker/maker fee model
    risk.py             Bootstrap risk profiling (drawdown, ruin, CVaR)
    runner.py           CLI: --strategy / --series
  execution/
    engine.py           The live loop (see below)
    paper.py            Simulated fills against the real book, local cash
    live.py             Real orders; refuses prod host without both gates set
    risk.py             Pre-trade checks + quarter-Kelly sizing
    settle.py           Settles resolved positions, credits cash
    state.py            bot_signals / bot_fills / bot_positions / bot_equity /
                        bot_status / bot_settlements tables
  ui/server.py          FastAPI dashboard + kill switch
scripts/                One-off research and scanner CLIs
tests/                  pytest suite
```

## How it works

**Strategy interface** (`strategies/base.py`). A strategy implements one
method:

```python
def generate_signals(self, state: MarketState, ctx: Context) -> list[Signal]
```

`MarketState` is a point-in-time view of a single market — prices in dollars
(0–1, YES side), plus `mid`, `spread`, and `hours_to_close()`. `Context` gives
access to external data (forecasts, sportsbook lines) and current positions. A
`Signal` names a ticker, side (`yes`/`no`), contract count, and a `fair_value`
probability estimate; the caller sizes and filters on `fair_value - price` net
of fees. `limit_price=None` means take the market.

The same interface is driven by both the backtester and the live engine, so a
strategy runs unchanged across backtest, paper, demo, and live.

**Execution cycle** (`execution/engine.py`, default 60s):

1. Settle resolved positions and credit cash (paper only — demo/live reconcile
   from the exchange).
2. Refresh open markets for the configured series.
3. Build a `MarketState` per market from the live orderbook, skipping empty books.
4. Check resting orders from previous cycles.
5. Mark equity and refresh daily P&L, *before* gating new trades.
6. Run enabled strategies → risk-check each signal → route approved ones to the
   executor → persist signals, fills, positions, and equity.

**Executors.** `PaperExecutor` simulates fills against the real book and tracks
cash locally. `LiveExecutor` sends real orders and refuses the production host
unless `TRADING_MODE=live` *and* `LIVE_TRADING_ACK=yes`.

**Storage.** Everything is one SQLite file, `data/kalshi.db` (gitignored).
Market history lives in `series`, `markets`, `candles`, `trades`,
`external_signals`; bot activity in the `bot_*` tables.

### Adding a strategy

Add a module under `kalshi_bot/strategies/` subclassing `Strategy`, then
register it in the `STRATEGIES` dict in `strategies/__init__.py`. The
backtest runner and execution engine both resolve strategies by that key, so
no other wiring is needed.

## Safety rails

- `TRADING_MODE` ∈ `demo` | `paper` | `live`; live orders additionally require
  `LIVE_TRADING_ACK=yes`. The live executor refuses the production host
  otherwise.
- Hard caps from `.env`: per-market, total exposure, and daily loss.
- Kill switch: dashboard toggle or `bot_status.kill_switch`, honored every
  cycle. The engine keeps heart-beating while stopped so the UI sees it alive.
- Quarter-Kelly sizing, capped by the per-market limit.
- Every strategy must earn its way up the ladder — backtest (positive after
  taker fees, ≥90 days) → paper → demo → live. Each promotion is manual.

## Research notes

Several strategies in the registry are retained deliberately after being
rejected in the backtester; they document what does not work and why, and the
scanners ship read-only. Full design notes, fee math, calibration studies, and
per-strategy verdicts live in `docs/superpowers/specs/` and
`docs/superpowers/plans/`. Bootstrap risk profiles are in `data/risk_profiles/`.
