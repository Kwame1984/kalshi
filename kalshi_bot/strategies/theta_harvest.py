"""Theta harvest: buy near-certain econ/finance outcomes close to settlement.

Empirical basis (our 120-day sample + Whelan 2025 transaction-level study):
- econ favorites at ~0.98 won 48/48 with +2.0c avg edge 24h out;
- econ longshots under 0.05 went 0-for-92 (the NO side of the same trade);
- weather shows no such edge after fees (market is sharp) -> domain filter.

Tail control is the entire game (a 97c loser costs 32x one winner):
- correlation caps: max positions per series and per settlement day;
- price floor 0.94 (below that, win-rate evidence is thinner) and ceiling
  0.99 (above, fee + tail risk kill it);
- sized so one full loss stays within the daily loss budget.
"""

from __future__ import annotations

import datetime as dt

from kalshi_bot.backtest.fees import taker_fee
from kalshi_bot.strategies.base import Context, MarketState, Signal, Strategy

EXCLUDED_PREFIXES = ("KXHIGH", "KXLOW")  # weather: no measurable theta edge


class ThetaHarvest(Strategy):
    name = "theta_harvest"

    def __init__(
        self,
        min_price: float = 0.94,
        max_price: float = 0.99,
        max_hours_to_close: float = 72.0,
        assumed_extra_win: float = 0.015,  # empirically ~0.02; margin of humility
        contracts: int = 25,
        max_per_series: int = 2,
        max_per_close_date: int = 3,
    ):
        self.min_price = min_price
        self.max_price = max_price
        self.max_hours_to_close = max_hours_to_close
        self.assumed_extra_win = assumed_extra_win
        self.contracts = contracts
        self.max_per_series = max_per_series
        self.max_per_close_date = max_per_close_date

    def _correlation_blocked(self, state: MarketState, ctx: Context) -> bool:
        series = state.meta.get("series_ticker") or state.ticker.split("-")[0]
        close_day = str(state.meta.get("close_time", ""))[:10]
        same_series = same_day = 0
        for t, qty in ctx.positions.items():
            if not qty:
                continue
            if t.split("-")[0] == series.split("-")[0]:
                same_series += 1
            meta_close = ctx.external.get(("_close_day", t, ""), "")
            if meta_close and meta_close == close_day:
                same_day += 1
        return same_series >= self.max_per_series or same_day >= self.max_per_close_date

    def generate_signals(self, state: MarketState, ctx: Context) -> list[Signal]:
        if ctx.positions.get(state.ticker):
            return []
        series = state.meta.get("series_ticker", "")
        if any(series.startswith(p) for p in EXCLUDED_PREFIXES):
            return []
        htc = state.hours_to_close()
        if htc is None or htc > self.max_hours_to_close or htc < 0:
            return []
        if self._correlation_blocked(state, ctx):
            return []
        # record close day for future correlation checks
        ctx.external[("_close_day", state.ticker, "")] = str(
            state.meta.get("close_time", "")
        )[:10]

        candidates = []
        if state.yes_ask is not None and self.min_price <= state.yes_ask <= self.max_price:
            candidates.append(("yes", state.yes_ask))
        if state.yes_bid is not None:
            no_price = 1 - state.yes_bid
            if self.min_price <= no_price <= self.max_price:
                candidates.append(("no", no_price))
        out = []
        for side, price in candidates:
            fair = min(price + self.assumed_extra_win, 0.999)
            if fair - price - taker_fee(price, 1) <= 0:
                continue
            out.append(Signal(
                ticker=state.ticker, side=side, contracts=self.contracts,
                fair_value=fair if side == "yes" else 1 - fair,
                reason=f"theta {side}@{price:.3f} htc={htc:.0f}h",
                strategy=self.name,
            ))
        return out[:1]
