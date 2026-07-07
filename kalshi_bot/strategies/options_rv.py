"""Options-implied relative value for Kalshi BTC bracket markets.

Fair value comes from Deribit's option-implied risk-neutral distribution
(see kalshi_bot.data.external.deribit), precomputed by a collector/snapshot
job and injected via ctx.external so this strategy stays pure/backtestable:

    ctx.external[("deribit_rnd", <EVENT_TICKER>, <close date YYYY-MM-DD>)]
        = {market_ticker: implied_prob, ...,
           "_forward": float, "_sigma_usd": float}   # optional metadata

Why the asymmetric gating: risk-neutral densities systematically overweight
tails (variance risk premium), so a Deribit tail probability is an UPPER
bound on the physical one. Buying a tail bracket because "Deribit says it's
worth more" is exactly the trade that premium punishes -> extra hurdle.
Fading a tail Kalshi has bid above even the inflated risk-neutral number
(buying NO) is mispricing in the direction AWAY from the tail -> standard
hurdle. These are the most bot-crowded markets on Kalshi; hurdles default
high on purpose and this ships as a scanner until we have historical options
data to backtest against.
"""

from __future__ import annotations

from kalshi_bot.backtest.fees import taker_fee
from kalshi_bot.strategies.base import Context, MarketState, Signal, Strategy

SOURCE = "deribit_rnd"


class OptionsRV(Strategy):
    name = "options_rv"

    def __init__(
        self,
        min_net_edge: float = 0.06,      # required edge net of taker fee
        tail_hurdle: float = 0.04,       # extra edge to BUY a tail on RND grounds
        min_entry_price: float = 0.12,   # fee death zone: never buy below this
        tail_prob: float = 0.10,         # implied prob below this = tail bracket
        tail_sigma: float = 1.0,         # or bracket fully beyond this many SDs
        max_events: int = 2,             # concurrent events with positions
        contracts: int = 10,
    ):
        self.min_net_edge = min_net_edge
        self.tail_hurdle = tail_hurdle
        self.min_entry_price = min_entry_price
        self.tail_prob = tail_prob
        self.tail_sigma = tail_sigma
        self.max_events = max_events
        self.contracts = contracts

    # -- helpers -------------------------------------------------------------

    @staticmethod
    def _event_of(ticker: str) -> str:
        return ticker.rsplit("-", 1)[0]

    def _event_blocked(self, state: MarketState, ctx: Context) -> bool:
        """One position per event; at most max_events events concurrently."""
        event = state.meta.get("event_ticker") or self._event_of(state.ticker)
        open_events = {
            self._event_of(t) for t, qty in ctx.positions.items() if qty
        }
        if event in open_events:
            return True
        return len(open_events) >= self.max_events

    def _is_tail_bracket(self, state: MarketState, prob: float,
                         probs: dict) -> bool:
        """Tail = low implied mass, or bracket fully > tail_sigma from forward."""
        if prob < self.tail_prob:
            return True
        fwd = probs.get("_forward")
        sig = probs.get("_sigma_usd")
        lo = state.meta.get("floor_strike")
        hi = state.meta.get("cap_strike")
        if fwd and sig and lo is not None and hi is not None:
            if lo > fwd + self.tail_sigma * sig or hi < fwd - self.tail_sigma * sig:
                return True
        return False

    # -- Strategy ------------------------------------------------------------

    def generate_signals(self, state: MarketState, ctx: Context) -> list[Signal]:
        if ctx.positions.get(state.ticker):
            return []
        close = str(state.meta.get("close_time", ""))
        if len(close) < 10:
            return []
        event = state.meta.get("event_ticker") or self._event_of(state.ticker)
        probs = ctx.get_external(SOURCE, event, close[:10])
        if not isinstance(probs, dict):
            return []
        prob = probs.get(state.ticker)
        if prob is None:
            return []
        htc = state.hours_to_close()
        if htc is not None and htc < 0:
            return []
        if self._event_blocked(state, ctx):
            return []

        tail_bracket = self._is_tail_bracket(state, prob, probs)
        candidates: list[tuple[str, float, float, float]] = []
        if state.yes_ask is not None and state.yes_ask > 0:
            price = state.yes_ask
            edge = prob - price - taker_fee(price, 1)
            hurdle = self.min_net_edge + (self.tail_hurdle if tail_bracket else 0)
            candidates.append(("yes", price, edge, hurdle))
        if state.yes_bid is not None and state.yes_bid < 1:
            price = 1.0 - state.yes_bid  # cost of NO
            edge = (1.0 - prob) - price - taker_fee(price, 1)
            # buying NO on a NON-tail (central) bracket is itself a bet on the
            # tails of the distribution -> hurdle it; NO on a tail bracket is
            # the preferred fade-the-tail direction -> standard hurdle.
            hurdle = self.min_net_edge + (0 if tail_bracket else self.tail_hurdle)
            candidates.append(("no", price, edge, hurdle))

        best: Signal | None = None
        best_net = 0.0
        for side, price, edge, hurdle in candidates:
            if price < self.min_entry_price:  # fee/adverse-selection death zone
                continue
            if edge < hurdle:
                continue
            if edge > best_net:
                best_net = edge
                best = Signal(
                    ticker=state.ticker,
                    side=side,
                    contracts=self.contracts,
                    fair_value=prob,  # YES probability, both sides
                    reason=(
                        f"deribit_rnd p={prob:.3f} {side}@{price:.2f} "
                        f"edge={edge:+.3f} hurdle={hurdle:.2f}"
                        f"{' tail' if tail_bracket else ''}"
                    ),
                    strategy=self.name,
                )
        return [best] if best else []
