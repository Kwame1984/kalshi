"""Politics favorite buying: modal brackets in politics events near close.

Calibration finding: politics 24h buckets 0.3-0.8 all show positive edge
(+3c to +14c) — favorites in weekly politics brackets (presidential approval,
post-count ranges) are systematically underpriced. Matches Le (2026)
calibration intercept +0.151 for politics. Sample is small (n=14-36 per
bucket) — this strategy IS the out-of-sample test, sized accordingly.

HIGH RISK: buys 30-80c contracts held to settlement; losses are large
relative to stake and the edge estimate itself is uncertain.
"""

from __future__ import annotations

from kalshi_bot.backtest.fees import taker_fee
from kalshi_bot.strategies.base import Context, MarketState, Signal, Strategy

POLITICS_PREFIXES = ("KXAPRPOTUS", "KXTRUTHSOCIAL")


class PoliticsFavorite(Strategy):
    name = "politics_favorite"

    def __init__(
        self,
        min_price: float = 0.35,
        max_price: float = 0.80,
        max_hours_to_close: float = 30.0,
        assumed_extra_win: float = 0.05,   # measured +3..14c; assume the low end
        min_net_edge: float = 0.02,
        max_spread: float = 0.08,
        contracts: int = 15,
        max_per_event: int = 1,
        max_concurrent: int = 4,
    ):
        self.min_price = min_price
        self.max_price = max_price
        self.max_hours_to_close = max_hours_to_close
        self.assumed_extra_win = assumed_extra_win
        self.min_net_edge = min_net_edge
        self.max_spread = max_spread
        self.contracts = contracts
        self.max_per_event = max_per_event
        self.max_concurrent = max_concurrent

    def generate_signals(self, state: MarketState, ctx: Context) -> list[Signal]:
        if ctx.positions.get(state.ticker):
            return []
        if not state.ticker.startswith(POLITICS_PREFIXES):
            return []
        htc = state.hours_to_close()
        if htc is None or htc > self.max_hours_to_close or htc < 0:
            return []
        if sum(1 for q in ctx.positions.values() if q) >= self.max_concurrent:
            return []
        event = state.meta.get("event_ticker") or state.ticker.rsplit("-", 1)[0]
        if any(q and t.startswith(event + "-") for t, q in ctx.positions.items()):
            return []
        bid, ask = state.yes_bid, state.yes_ask
        if bid is None or ask is None or ask - bid > self.max_spread:
            return []
        if not (self.min_price <= ask <= self.max_price):
            return []
        fair = min(ask + self.assumed_extra_win, 0.98)
        if fair - ask - taker_fee(ask, 1) < self.min_net_edge:
            return []
        return [Signal(
            ticker=state.ticker, side="yes", contracts=self.contracts,
            fair_value=fair,
            reason=f"politics fav ask={ask:.2f} htc={htc:.0f}h",
            strategy=self.name,
        )]
