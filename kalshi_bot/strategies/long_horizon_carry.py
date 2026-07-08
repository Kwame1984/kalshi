"""Long-horizon carry: buy mid-priced YES 45+ days out, hold to settlement.

Empirical basis (scripts/long_horizon_study.py, 2026-07-07, one sample per
settled market per horizon, spread <= 0.12):

- The Le (2026) slope story does NOT replicate here: implied calibration
  slope is 0.89-1.08 across 30/60/90/180d horizons (not 1.32). What does
  show up is a positive YES-side intercept at long horizons (+0.17 at 60d,
  +0.26 at 90d, +0.49 at 180d) — long-dated mid-priced YES wins more than
  it costs.
- Actionable band (>= 60d to close, one sample per market at its longest
  horizon, edges measured against the ASK, n=257): ask 0.20-0.60 priced
  avg 0.391, won 45.5% -> +6.4c ask-side edge, Wilson 95% CI [+0.5, +12.6]c.
  Era split: settle <= 2024 +4.0c (n=140), >= 2025 +9.4c (n=117) — positive
  in both regimes, fatter in the recent one.
- 30d horizon is well calibrated (edges <= ~2c): entry is gated to
  >= 45 days out, where the mispricing lives. Entry is also capped at
  <= 180 days out: the study measured nothing beyond 180d, and backtest
  entries out to 413d dragged the 5y taker return from +3.3% to +0.6%.
- Honesty note: per-fixed-horizon band edges are weaker and individually
  insignificant (+1.3c at 60d, +4.8c at 90d, +3.9c at 180d vs ask, 95% CIs
  all spanning 0); the pooled +6.4c uses each market's longest horizon.
  Treat this as a small, fragile edge that clears costs only at maker fees.

Risk shape (why the caps are tight): all long-horizon depth is Economics,
and the band edge is concentrated — KXPAYROLLS +25.7c, KXFED +17.8c, while
KXU3 ran -27.0c (labor-market strength was one correlated macro bet, not
independent samples). Per-series cap 2 and one position per event (KXFED /
KXCPI ladders are cumulative thresholds, not partitions) bound that
concentration; max 8 concurrent bounds the book.

Carry: Kalshi pays ~3.25-4% APY on cash AND open positions, so the
multi-month lockup earns interest instead of costing it. That carry is
reported separately by the study/backtest tooling, never baked into edge.

Execution: maker-preference — the signal carries limit_price at the bid so
the live executor rests there (maker fee is 25% of taker). The backtester
fills at the ask with taker fees, so backtest numbers are the conservative
taker-cost bound (assumed_extra_win 0.04 is also below the measured +6.4c).
Positions are held to settlement; the strategy never exits early.
"""

from __future__ import annotations

from kalshi_bot.backtest.fees import taker_fee
from kalshi_bot.strategies.base import Context, MarketState, Signal, Strategy

# no measured long-horizon edge outside econ/politics/entertainment;
# sports close in-play and crypto brackets are bot-priced
EXCLUDED_PREFIXES = (
    "KXHIGH", "KXLOW",                                        # weather
    "KXMLB", "KXATP", "KXWTA", "KXUFC", "KXWNBA", "KXNBA",    # sports
    "KXNFL", "KXNHL",
    "KXBTC", "KXETH",                                         # crypto
)


class LongHorizonCarry(Strategy):
    name = "long_horizon_carry"

    def __init__(
        self,
        min_price: float = 0.20,          # ask band from the study
        max_price: float = 0.60,
        min_hours_to_close: float = 1080.0,  # 45 days
        max_hours_to_close: float = 4320.0,  # 180 days: study's support
        max_spread: float = 0.12,         # matches the study's sample filter
        assumed_extra_win: float = 0.04,  # measured +6.4c; assume the low end
        min_net_edge: float = 0.02,
        contracts: int = 12,
        max_per_event: int = 1,           # ladder events are one macro bet
        max_per_series: int = 2,
        max_concurrent: int = 8,
        prefer_maker: bool = True,        # live: rest at the bid; backtest
                                          # runs prefer_maker=False (taker)
    ):
        self.min_price = min_price
        self.max_price = max_price
        self.min_hours_to_close = min_hours_to_close
        self.max_hours_to_close = max_hours_to_close
        self.max_spread = max_spread
        self.assumed_extra_win = assumed_extra_win
        self.min_net_edge = min_net_edge
        self.contracts = contracts
        self.max_per_event = max_per_event
        self.max_per_series = max_per_series
        self.max_concurrent = max_concurrent
        self.prefer_maker = prefer_maker

    @staticmethod
    def _series_key(t: str) -> str:
        """Series identity that survives the 2026 KX-prefix rename."""
        root = t.split("-")[0]
        return root[2:] if root.startswith("KX") else root

    def generate_signals(self, state: MarketState, ctx: Context) -> list[Signal]:
        if ctx.positions.get(state.ticker):
            return []  # hold to settlement, never add
        series = state.meta.get("series_ticker", "") or state.ticker
        if any(series.startswith(p) or state.ticker.startswith(p)
               for p in EXCLUDED_PREFIXES):
            return []
        htc = state.hours_to_close()
        if htc is None or not (self.min_hours_to_close <= htc <= self.max_hours_to_close):
            return []
        bid, ask = state.yes_bid, state.yes_ask
        if bid is None or ask is None or bid <= 0.0 or ask >= 1.0:
            return []
        if ask - bid > self.max_spread:
            return []
        if not (self.min_price <= ask <= self.max_price):
            return []

        # concentration caps
        live = [t for t, q in ctx.positions.items() if q]
        if len(live) >= self.max_concurrent:
            return []
        event = state.meta.get("event_ticker") or state.ticker.rsplit("-", 1)[0]
        if sum(1 for t in live if t.startswith(event + "-")) >= self.max_per_event:
            return []
        skey = self._series_key(series)
        if sum(1 for t in live if self._series_key(t) == skey) >= self.max_per_series:
            return []

        # edge net of taker fee at the ask — the conservative entry bound;
        # a maker fill at the bid only improves on this
        fair = min(ask + self.assumed_extra_win, 0.97)
        # epsilon: the band's typical net edge sits exactly at min_net_edge,
        # and float error (0.339999...+0.04) must not reject the boundary case
        if fair - ask - taker_fee(ask, 1) < self.min_net_edge - 1e-9:
            return []
        limit = bid if self.prefer_maker else ask
        return [Signal(
            ticker=state.ticker, side="yes", contracts=self.contracts,
            fair_value=fair,
            limit_price=limit,
            reason=(f"lh carry ask={ask:.2f} spread={ask - bid:.2f} "
                    f"htc={htc / 24:.0f}d"),
            strategy=self.name,
        )]
