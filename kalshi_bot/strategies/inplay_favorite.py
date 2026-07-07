"""In-play sports favorite buying.

Calibration finding (category_calibration, sports 1h horizon = in-game for
moneyline markets): favorites priced 0.70-0.90 during play win more than
priced (edges +1.4c to +3.3c, fee-clearing), while in-play longshots
underperform. Interpretation: retail overreacts to in-game adversity and
undersells the leading side; win-probability decay is nonlinear late.

HIGH RISK: each loss is 70-90c per contract against wins of 10-30c. The
edge is a few cents on a coin-sized stake — drawdowns are structural.
"""

from __future__ import annotations

from kalshi_bot.backtest.fees import taker_fee
from kalshi_bot.strategies.base import Context, MarketState, Signal, Strategy

SPORTS_PREFIXES = ("KXMLBGAME", "KXWNBAGAME", "KXATPMATCH", "KXUFCFIGHT")


class InPlayFavorite(Strategy):
    name = "inplay_favorite"

    def __init__(
        self,
        min_price: float = 0.70,
        max_price: float = 0.90,
        max_hours_to_close: float = 2.5,  # in-game window
        assumed_extra_win: float = 0.025,
        min_net_edge: float = 0.005,
        max_spread: float = 0.04,
        contracts: int = 15,
        max_concurrent: int = 3,
    ):
        self.min_price = min_price
        self.max_price = max_price
        self.max_hours_to_close = max_hours_to_close
        self.assumed_extra_win = assumed_extra_win
        self.min_net_edge = min_net_edge
        self.max_spread = max_spread
        self.contracts = contracts
        self.max_concurrent = max_concurrent

    def generate_signals(self, state: MarketState, ctx: Context) -> list[Signal]:
        if ctx.positions.get(state.ticker):
            return []
        if not state.ticker.startswith(SPORTS_PREFIXES):
            return []
        htc = state.hours_to_close()
        if htc is None or htc > self.max_hours_to_close or htc < 0:
            return []
        if sum(1 for q in ctx.positions.values() if q) >= self.max_concurrent:
            return []
        bid, ask = state.yes_bid, state.yes_ask
        if bid is None or ask is None or ask - bid > self.max_spread:
            return []
        # the event has two mirrored markets (one per team); trade only via the
        # YES side of whichever market is the favorite to avoid double entry
        event = state.meta.get("event_ticker") or state.ticker.rsplit("-", 1)[0]
        if any(q and t.startswith(event + "-") for t, q in ctx.positions.items()):
            return []
        if not (self.min_price <= ask <= self.max_price):
            return []
        fair = min(ask + self.assumed_extra_win, 0.99)
        if fair - ask - taker_fee(ask, 1) < self.min_net_edge:
            return []
        return [Signal(
            ticker=state.ticker, side="yes", contracts=self.contracts,
            fair_value=fair,
            reason=f"inplay fav ask={ask:.2f} htc={htc:.1f}h",
            strategy=self.name,
        )]
