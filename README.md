# kalshi-bot

Algorithmic trading system for [Kalshi](https://kalshi.com) event contracts.
Focus: economics/finance and weather markets, retail-scale sizing, demo-first
with hard-gated live trading.

**Every strategy must earn its way up the ladder: backtest (positive after
taker fees, ≥90 days) → paper → demo → live. Each promotion is manual.**

## Quick start

```bash
python3 -m venv .venv && .venv/bin/pip install -e ".[dev]"
cp .env.example .env            # fill in credentials when you have them

# collect data (no credentials needed)
.venv/bin/python -m kalshi_bot.data.collector backfill --days 120
.venv/bin/python -m kalshi_bot.data.external.forecast_backfill --days 120
.venv/bin/python -m kalshi_bot.data.external.calibrate_weather

# backtest a strategy
.venv/bin/python -m kalshi_bot.backtest.runner --strategy theta_harvest \
    --series KXCPI KXCPIYOY KXFED KXPAYROLLS KXU3

# paper-trade against live markets (no orders placed anywhere)
.venv/bin/python -m kalshi_bot.execution.engine --mode paper \
    --strategies theta_harvest --series KXCPI KXFED

# dashboard at http://localhost:8420
.venv/bin/python -m kalshi_bot.ui.server --port 8420

# structural arb scanner (read-only)
.venv/bin/python -m scripts.scan_structural_arb
```

## Strategy status

| Strategy | Evidence | Status |
|----------|----------|--------|
| theta_harvest — econ favorites 94-99c near close | 120d: +0.74% (8/8); **5y: −9.2% (175/185)** — 94.6% hit rate doesn't clear the ~96.5% breakeven | gated; awaiting bucket-level calibration verdict |
| structural arb scanner — partition + monotonicity | live scanner; correctly rejects threshold-ladder false positives | overlay |
| options_rv — Deribit RND vs BTC brackets | live: all brackets within taker fees (bot-crowded) | scanner only; accumulating RND history |
| weather_ensemble — GEFS mean+spread fair value | market wins all 7 cities (LL 0.319 vs 0.428, n=1896) | rejected |
| weather_edge — deterministic forecast | log-loss 0.408 vs market 0.325 → no edge | rejected |
| expiry_convergence — naive favorite buying | −5.8% on weather brackets | rejected |

Five of six tested strategies failed validation so far. That is the system
working as designed: each rejection happened in the backtester or a live
read-only scanner, at zero cost.

Rejected/gated strategies are kept in-repo deliberately: they document what
does not work and why. See docs/superpowers/specs/ for the full design and
research findings (fee math, domain-dependent calibration, API migration
notes).

## Safety rails

- `TRADING_MODE` ∈ demo | paper | live; live orders additionally require
  `LIVE_TRADING_ACK=yes`. The live executor refuses the production host
  otherwise.
- Hard caps (.env): per-market $50, total exposure $300, daily loss $25.
- Kill switch: dashboard toggle or `bot_status.kill_switch`, honored every
  engine cycle.
- Quarter-Kelly sizing capped by per-market limit.

## Getting API keys

Market data and paper trading need no credentials. For demo trading: sign up
at demo.kalshi.co, generate an API key in settings, put the key ID in .env and
the PEM at `~/.kalshi/`. For live: same at kalshi.com — plus flip the two
gates above, deliberately.
