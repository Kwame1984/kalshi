"""Cumulative-process momentum: buy former tails being re-priced upward in
series whose underlying accumulates (approval averages, post counts, sales).

Measured (no-lookahead threshold-crossing test, 2,688 settled markets):
    cross 0.10: win 13.6% at 16.1c  -> EV -2.5c
    cross 0.20: win 27.8% at 27.0c  -> EV +0.7c
    cross 0.30: win 46.7% at 37.3c  -> EV +9.4c   (n=45)
The same test on mean-reverting series (weather) is -5c at every threshold —
the underlying process must TREND for the re-pricing to have follow-through.

HIGH RISK: ~53% of trades lose the full ~37c stake; the edge estimate rests
on n=45. Sized small; this strategy's live run is its out-of-sample test.
"""

from __future__ import annotations

from kalshi_bot.backtest.fees import taker_fee
from kalshi_bot.strategies.base import Context, MarketState, Signal, Strategy

CUMULATIVE_PREFIXES = (
    "KXTRUTHSOCIAL", "KXAPRPOTUS", "KXALBUMEQUIV", "KXNETFLIXRANKMOVIE",
)


class CumulativeMomentum(Strategy):
    name = "cumulative_momentum"

    def __init__(
        self,
        cross_threshold: float = 0.30,
        max_entry_ask: float = 0.42,
        tail_qualify_ask: float = 0.08,  # must have been a tail earlier
        tail_qualify_hours: float = 48.0,
        max_hours_to_close: float = 48.0,
        assumed_win: float = 0.42,       # measured 46.7%; assume less
        min_net_edge: float = 0.02,
        contracts: int = 12,
        max_per_event: int = 1,
        max_concurrent: int = 4,
    ):
        self.cross_threshold = cross_threshold
        self.max_entry_ask = max_entry_ask
        self.tail_qualify_ask = tail_qualify_ask
        self.tail_qualify_hours = tail_qualify_hours
        self.max_hours_to_close = max_hours_to_close
        self.assumed_win = assumed_win
        self.min_net_edge = min_net_edge
        self.contracts = contracts
        self.max_per_event = max_per_event
        self.max_concurrent = max_concurrent
        # ticker -> True once seen at tail prices outside the entry window
        self._was_tail: dict[str, bool] = {}

    def generate_signals(self, state: MarketState, ctx: Context) -> list[Signal]:
        if not state.ticker.startswith(CUMULATIVE_PREFIXES):
            return []
        htc = state.hours_to_close()
        if htc is None or htc < 0:
            return []
        ask = state.yes_ask
        # tail qualification happens ahead of the entry window
        if (htc > self.max_hours_to_close and ask is not None
                and ask <= self.tail_qualify_ask):
            self._was_tail[state.ticker] = True
            return []
        if ctx.positions.get(state.ticker):
            return []
        if htc > self.max_hours_to_close:
            return []
        if not self._was_tail.get(state.ticker):
            return []
        if sum(1 for q in ctx.positions.values() if q) >= self.max_concurrent:
            return []
        event = state.meta.get("event_ticker") or state.ticker.rsplit("-", 1)[0]
        if any(q and t.startswith(event + "-") for t, q in ctx.positions.items()):
            return []
        bid = state.yes_bid
        if bid is None or ask is None:
            return []
        if bid < self.cross_threshold or ask > self.max_entry_ask:
            return []
        if self.assumed_win - ask - taker_fee(ask, 1) < self.min_net_edge:
            return []
        return [Signal(
            ticker=state.ticker, side="yes", contracts=self.contracts,
            fair_value=self.assumed_win,
            reason=f"momentum cross bid={bid:.2f} ask={ask:.2f} htc={htc:.0f}h",
            strategy=self.name,
        )]
