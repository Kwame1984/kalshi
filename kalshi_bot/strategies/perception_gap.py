"""Perception-gap fade: reflexivity trade on sports markets.

Soros framing: on a retail-heavy venue, price IS perception. The
Pinnacle-devigged line is the closest observable to reality (sharp,
low-vig, positionally accountable). When Kalshi diverges from the anchor,
the divergence direction identifies emotional flow; fade it and hold
through the reality anchor's reassertion (line convergence or settlement).

Depends on scripts/sports_devig_snapshot.py runs persisting
('sportsbook_devig', <event>, <date>) -> {market_ticker: fair_prob} into
external_signals. Measured baseline (2026-07-07): pre-game MLB gap mean
0.55c, max 1.3c — quiet days offer nothing. The strategy therefore only
acts on outlier gaps (>= 3c net of fees), which appear around injury news,
lineup releases, and heavy public events. Maker-only: the whole point is
being the reality-anchored side that emotional takers hit.

HIGH RISK: fading perception means buying what the crowd is selling;
entries are 30-70c, losses large, and the anchor can be wrong when the
crowd actually has information (that is the reflexive tail we accept).
"""

from __future__ import annotations

import datetime as dt

from kalshi_bot.backtest.fees import maker_fee
from kalshi_bot.strategies.base import Context, MarketState, Signal, Strategy

SPORTS_PREFIXES = ("KXMLBGAME", "KXWNBAGAME", "KXATPMATCH", "KXUFCFIGHT")


class PerceptionGap(Strategy):
    name = "perception_gap"

    def __init__(
        self,
        min_gap: float = 0.03,        # kalshi mid vs anchor fair, net of maker fee
        min_price: float = 0.25,
        max_price: float = 0.75,
        max_spread: float = 0.05,
        contracts: int = 12,
        max_concurrent: int = 4,
        pre_game_only: bool = True,   # in-play anchor staleness is disqualifying
    ):
        self.min_gap = min_gap
        self.min_price = min_price
        self.max_price = max_price
        self.max_spread = max_spread
        self.contracts = contracts
        self.max_concurrent = max_concurrent
        self.pre_game_only = pre_game_only

    def generate_signals(self, state: MarketState, ctx: Context) -> list[Signal]:
        if ctx.positions.get(state.ticker):
            return []
        if not state.ticker.startswith(SPORTS_PREFIXES):
            return []
        if sum(1 for q in ctx.positions.values() if q) >= self.max_concurrent:
            return []
        event = state.meta.get("event_ticker") or state.ticker.rsplit("-", 1)[0]
        if any(q and t.startswith(event + "-") for t, q in ctx.positions.items()):
            return []
        # anchor fair value from the most recent devig snapshot
        game_date = str(state.meta.get("close_time", ""))[:10]
        snap = ctx.get_external("sportsbook_devig", event, game_date)
        if not isinstance(snap, dict):
            return []
        fair = snap.get(state.ticker)
        if fair is None:
            return []
        bid, ask = state.yes_bid, state.yes_ask
        if bid is None or ask is None or ask - bid > self.max_spread:
            return []
        mid = (bid + ask) / 2
        if self.pre_game_only:
            # devig snapshots carry only pre-game lines; require the game
            # not to have started (close_time - typical game len > now)
            htc = state.hours_to_close()
            if htc is None or htc < 3.5:
                return []
        out: list[Signal] = []
        # Kalshi rich vs reality -> perception is long this side -> fade: buy NO
        gap_rich = mid - fair - maker_fee(1 - bid, 1)
        if gap_rich >= self.min_gap and self.min_price <= 1 - bid <= self.max_price:
            out.append(Signal(
                ticker=state.ticker, side="no", contracts=self.contracts,
                fair_value=fair,
                limit_price=bid,  # rest inside: sell yes at the bid... maker at bid
                reason=f"fade rich mid={mid:.2f} fair={fair:.2f} gap={mid-fair:+.3f}",
                strategy=self.name,
            ))
        # Kalshi cheap vs reality -> perception is short -> buy YES
        gap_cheap = fair - mid - maker_fee(ask, 1)
        if gap_cheap >= self.min_gap and self.min_price <= ask <= self.max_price:
            out.append(Signal(
                ticker=state.ticker, side="yes", contracts=self.contracts,
                fair_value=fair,
                limit_price=ask,
                reason=f"fade cheap mid={mid:.2f} fair={fair:.2f} gap={mid-fair:+.3f}",
                strategy=self.name,
            ))
        return out[:1]
