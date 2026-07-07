"""Tail seller: buy NO on overpriced deep-tail brackets near settlement.

Empirical basis (category_calibration over 2.06M candles, 2026-07):
- Politics 24h: 0-10c brackets priced avg 2.7c win 1.0% (n=694, 157 events)
- Politics 1h/6h, Econ 1h, Weather 24h: same shape, all TRADEABLE-flagged
- Sports excluded: the 1h horizon there is in-play (close = game end) and
  pre-game tails are fairly priced.

Within a multi-bracket event at most one bracket resolves YES, so multiple
NO-side tail positions in one event can lose at most once — intra-event
stacking is bounded, and the cap below exists to limit the size of that one
possible loss, not correlation.

Execution note: the edge is 1-2c off mid, so entry quality is the whole
trade. We only sell into a bid at/above min_premium (taker into richness);
live, prefer_maker rests an ask instead.
"""

from __future__ import annotations

from kalshi_bot.backtest.fees import taker_fee
from kalshi_bot.strategies.base import Context, MarketState, Signal, Strategy

# sports moneylines behave differently (in-play close); exclude by prefix
EXCLUDED_PREFIXES = (
    "KXMLB", "KXATP", "KXWTA", "KXUFC", "KXWNBA", "KXNBA", "KXNFL", "KXNHL",
    "KXBTC", "KXETH",  # crypto brackets are bot-priced; no measured tail edge
)


class TailSeller(Strategy):
    name = "tail_seller"

    def __init__(
        self,
        min_premium: float = 0.03,   # only sell into a yes bid >= this
        max_tail_ask: float = 0.10,  # market must still be tail-priced
        assumed_tail_prob: float = 0.012,  # measured 0.3-1.7%; take the high end
        min_net_edge: float = 0.005,
        # 24h matches the calibration horizons; 48h backtests +6.3% vs +13.2%
        # at 24h (the extra day adds adverse selection, not edge)
        max_hours_to_close: float = 24.0,
        contracts: int = 25,
        max_per_event: int = 3,      # >=2 is safe: max one bracket/event loses
        max_per_series: int = 6,
    ):
        self.min_premium = min_premium
        self.max_tail_ask = max_tail_ask
        self.assumed_tail_prob = assumed_tail_prob
        self.min_net_edge = min_net_edge
        self.max_hours_to_close = max_hours_to_close
        self.contracts = contracts
        self.max_per_event = max_per_event
        self.max_per_series = max_per_series

    @staticmethod
    def _series_key(t: str) -> str:
        root = t.split("-")[0]
        return root[2:] if root.startswith("KX") else root

    def generate_signals(self, state: MarketState, ctx: Context) -> list[Signal]:
        if ctx.positions.get(state.ticker):
            return []
        series = state.meta.get("series_ticker", "") or state.ticker
        if any(series.startswith(p) or state.ticker.startswith(p)
               for p in EXCLUDED_PREFIXES):
            return []
        htc = state.hours_to_close()
        if htc is None or htc > self.max_hours_to_close or htc < 0:
            return []
        bid, ask = state.yes_bid, state.yes_ask
        if bid is None or ask is None:
            return []
        if bid < self.min_premium or ask > self.max_tail_ask:
            return []

        event = state.meta.get("event_ticker") or state.ticker.rsplit("-", 1)[0]
        same_event = sum(
            1 for t, q in ctx.positions.items() if q and t.startswith(event + "-")
        )
        same_series = sum(
            1 for t, q in ctx.positions.items()
            if q and self._series_key(t) == self._series_key(state.ticker)
        )
        if same_event >= self.max_per_event or same_series >= self.max_per_series:
            return []

        no_price = 1 - bid
        edge = bid - self.assumed_tail_prob - taker_fee(no_price, 1)
        if edge < self.min_net_edge:
            return []
        return [Signal(
            ticker=state.ticker, side="no", contracts=self.contracts,
            fair_value=self.assumed_tail_prob,
            limit_price=self.min_premium,  # yes-price floor for the fill
            reason=f"tail no@{no_price:.2f} bid={bid:.2f} htc={htc:.0f}h",
            strategy=self.name,
        )]
