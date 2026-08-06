# kalshi-bot

Algorithmic trading system for [Kalshi](https://kalshi.com) event contracts.
Focus: economics/finance and weather markets, retail-scale position sizing,
demo-first with hard-gated live trading.

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
| **tail_seller** — buy NO on bid-up deep-tail brackets ≤24h out (politics/econ/weather/entertainment) | ~3y backtest: **+13.2%**, 95.5% hit, max DD 16.2%, Sharpe 0.30 at full taker fees; derived from own calibration study (0-10c brackets win 0.3-1.7% vs 1.4-3.1c prices, 5 TRADEABLE flags). First settled paper cohort (2026-07-07 weather, 16 fills): **15/16 hit but −$11.11 net** — one tail (LAX B73.5) hit and its 20:1 loss swamped the fifteen wins (n=1 cohort, illustrative not conclusive) | **paper trading** |
| theta_harvest — econ favorites 94-99c near close | 120d: +0.74% (8/8); 5y: −9.2% (175/185) — hit rate below ~96.5% breakeven | rejected as taker; maker variant may revisit |
| sports_devig — book-line fair value vs Kalshi MLB | pre-game divergence mean 0.55c, max 1.3c — inside fees vs soft book | scanner; needs Pinnacle (The Odds API key) |
| options_rv — Deribit RND vs BTC brackets | all brackets within taker fees (bot-crowded) | scanner; accumulating RND history |
| structural arb scanner — partition + monotonicity | live scanner; correctly rejects threshold-ladder false positives | overlay |
| weather_ensemble — GEFS mean+spread fair value | market wins all 7 cities (LL 0.319 vs 0.428) | rejected |
| weather_edge — deterministic forecast | log-loss 0.408 vs market 0.325 → no edge | rejected |
| expiry_convergence — naive favorite buying | −5.8% on weather brackets | rejected |

One strategy earned paper promotion out of eight tested; the rest were
rejected in the backtester or ship as read-only scanners. Every rejection
cost $0. Watchlist from the calibration study: politics favorites 0.3-0.8
(directionally +3 to +14c, underpowered n — recheck after more weekly
settlements), entertainment 90c+ favorites (+2-3c, near-significant),
in-play sports favorites (fee-clearing but unproven).

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

## High-risk program (2026-07-07)

| Strategy | Verdict | Risk profile (bootstrap) |
|----------|---------|--------------------------|
| cumulative_momentum — buy re-priced tails in trending series | **validated, paper trading**; +32%/$ staked, but 90% CI on mean P&L still spans zero (n=25) | positive skew; ruin ~1% at 5%/trade; full Kelly 18% (do not use); half-Kelly ruin 11% |
| perception_gap — fade Kalshi-vs-Pinnacle divergence (maker) | live paper, event-driven (dormant until gaps ≥3c) | untested — awaiting live gap events |
| tail_seller (contrast: the low-risk mirror) | paper trading | skew −4.3, avg loss 20x avg win, CVaR95 $21/trade, median max DD 17%, p95 36.5%, ruin 0.04% at half-Kelly |
| panic_fade — buy abandoned in-play favorites | **rejected at taker** (−9c/contract; touch edges were hindsight artifacts); maker ≤25c variant pending L2 fill data | n/a |
| insider_surge — tailgate volume surges | **rejected on testable categories** (surges anti-predictive in politics/entertainment; insider wins live in geopolitics we haven't backfilled) | n/a |
| catalyst_follow — political hard-news follow / debate fade | research-only (needs catalyst calendar; tiny n) | n/a |

Sizing law from the profiles: 5%/trade is the ceiling for the momentum family
(ruin 1.1%); tail-selling tolerates more trades but its 20:1 loss ratio makes
per-event caps the binding constraint.
