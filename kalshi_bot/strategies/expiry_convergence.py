"""Expiry convergence: buy heavy favorites close to settlement.

The favorite-longshot bias in prediction markets leaves favorites slightly
cheap: a contract at 95c within hours of close wins more than 95% of the
time historically. The edge is small, so fees and selectivity dominate —
we require a minimum net edge after taker fees and only trade near close.
"""

from __future__ import annotations

from kalshi_bot.backtest.fees import taker_fee
from kalshi_bot.strategies.base import Context, MarketState, Signal, Strategy


class ExpiryConvergence(Strategy):
    name = "expiry_convergence"

    def __init__(
        self,
        min_price: float = 0.90,
        max_price: float = 0.985,
        max_hours_to_close: float = 6.0,
        assumed_win_rate_margin: float = 0.02,  # assumed true P = price + margin
        min_net_edge: float = 0.005,
        contracts: int = 25,
    ):
        self.min_price = min_price
        self.max_price = max_price
        self.max_hours_to_close = max_hours_to_close
        self.margin = assumed_win_rate_margin
        self.min_net_edge = min_net_edge
        self.contracts = contracts

    def generate_signals(self, state: MarketState, ctx: Context) -> list[Signal]:
        if ctx.positions.get(state.ticker):
            return []
        htc = state.hours_to_close()
        if htc is None or htc > self.max_hours_to_close or htc < 0:
            return []
        out: list[Signal] = []
        # favorite on the YES side
        if state.yes_ask is not None and self.min_price <= state.yes_ask <= self.max_price:
            out.append(self._make(state, "yes", state.yes_ask))
        # favorite on the NO side (yes bid very low -> no ask = 1 - yes_bid)
        elif state.yes_bid is not None and (1 - state.yes_bid) >= self.min_price and (
            1 - state.yes_bid
        ) <= self.max_price:
            out.append(self._make(state, "no", 1 - state.yes_bid))
        return [s for s in out if s is not None]

    def _make(self, state: MarketState, side: str, price: float) -> Signal | None:
        fee = taker_fee(price, 1)
        fair = min(price + self.margin, 1.0)
        net_edge = fair - price - fee
        if net_edge < self.min_net_edge:
            return None
        return Signal(
            ticker=state.ticker,
            side=side,
            contracts=self.contracts,
            fair_value=fair if side == "yes" else 1 - fair,
            reason=f"favorite {side}@{price:.2f} htc={state.hours_to_close():.1f}",
            strategy=self.name,
        )
