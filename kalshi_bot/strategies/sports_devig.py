"""Sportsbook-devig relative value for Kalshi MLB game-winner markets.

Fair value is a devigged sportsbook moneyline (kalshi_bot.data.external
.sportsbook), precomputed by a snapshot/collector job and injected via
ctx.external so the strategy stays pure/backtestable:

    ctx.external[("sportsbook_devig", <EVENT_TICKER>, <game date YYYY-MM-DD>)]
        = {market_ticker: fair_prob, ...,          # devigged P(team wins)
           "_start_time": iso-utc,                 # first pitch (optional)
           "_provider": "DraftKings", ...}         # underscore = metadata

The game date key is the ET calendar date encoded in the event ticker
(KXMLBGAME-26JUL071910PHICIN -> 2026-07-07), NOT the market close_time,
which Kalshi pads ~5 days past first pitch for MLB.

HONESTY: today's feed devigs a single soft book (DraftKings via ESPN), not
a sharp line. A soft-book devig is a divergence *measure*, not an exploit
signal — hence the default gates stay conservative and this ships as a
scanner. Pre-game lines are only comparable to pre-game Kalshi prices: once
first pitch passes, Kalshi trades in-play while the stored line is stale,
so the strategy refuses to fire after start time.

Gates (theta_harvest-style):
  * min_net_edge 0.04 net of the applicable fee;
  * fee floor: never buy a side below 0.12 (quadratic-fee death zone);
  * prefer_maker: quote a resting limit at mid (maker fee 0.0175 vs taker
    0.07) instead of taking; emitted via Signal.limit_price, priced in the
    bought side's terms;
  * one position per game, at most max_events (4) concurrent games.
"""

from __future__ import annotations

import datetime as dt
import math
from zoneinfo import ZoneInfo

from kalshi_bot.backtest.fees import maker_fee, taker_fee
from kalshi_bot.strategies.base import Context, MarketState, Signal, Strategy

SOURCE = "sportsbook_devig"

_ET = ZoneInfo("America/New_York")
_MONTHS = {
    "JAN": 1, "FEB": 2, "MAR": 3, "APR": 4, "MAY": 5, "JUN": 6,
    "JUL": 7, "AUG": 8, "SEP": 9, "OCT": 10, "NOV": 11, "DEC": 12,
}


def parse_event_start(event_ticker: str) -> dt.datetime | None:
    """'KXMLBGAME-26JUL071910PHICIN' -> first pitch as aware UTC.

    The ticker encodes YYMMMDD + HHMM in US/Eastern (game local slate time).
    """
    try:
        code = event_ticker.split("-", 1)[1]
        year = 2000 + int(code[:2])
        mon = _MONTHS[code[2:5]]
        day = int(code[5:7])
        hh, mm = int(code[7:9]), int(code[9:11])
        local = dt.datetime(year, mon, day, hh, mm, tzinfo=_ET)
        return local.astimezone(dt.timezone.utc)
    except (IndexError, KeyError, ValueError):
        return None


def _floor_cent(x: float) -> float:
    return math.floor(x * 100.0 + 1e-9) / 100.0


class SportsDevig(Strategy):
    name = "sports_devig"

    def __init__(
        self,
        min_net_edge: float = 0.04,      # required edge net of fee
        min_entry_price: float = 0.12,   # fee floor: never buy below this
        prefer_maker: bool = True,       # rest at mid (maker fee) vs take
        max_events: int = 4,             # concurrent games with positions
        contracts: int = 10,
    ):
        self.min_net_edge = min_net_edge
        self.min_entry_price = min_entry_price
        self.prefer_maker = prefer_maker
        self.max_events = max_events
        self.contracts = contracts

    # -- helpers -------------------------------------------------------------

    @staticmethod
    def _event_of(ticker: str) -> str:
        return ticker.rsplit("-", 1)[0]

    @staticmethod
    def _game_date(event_ticker: str) -> str | None:
        """ET calendar date encoded in the ticker, as YYYY-MM-DD."""
        start = parse_event_start(event_ticker)
        if start is None:
            return None
        return start.astimezone(_ET).date().isoformat()

    def _event_blocked(self, state: MarketState, ctx: Context) -> bool:
        """One position per game; at most max_events games concurrently."""
        event = state.meta.get("event_ticker") or self._event_of(state.ticker)
        open_events = {
            self._event_of(t) for t, qty in ctx.positions.items() if qty
        }
        if event in open_events:
            return True
        return len(open_events) >= self.max_events

    @staticmethod
    def _start_time(event: str, probs: dict) -> dt.datetime | None:
        raw = probs.get("_start_time")
        if raw:
            try:
                return dt.datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
            except ValueError:
                pass
        return parse_event_start(event)

    # -- Strategy ------------------------------------------------------------

    def generate_signals(self, state: MarketState, ctx: Context) -> list[Signal]:
        if ctx.positions.get(state.ticker):
            return []
        event = state.meta.get("event_ticker") or self._event_of(state.ticker)
        game_date = self._game_date(event)
        if game_date is None:
            return []
        probs = ctx.get_external(SOURCE, event, game_date)
        if not isinstance(probs, dict):
            return []
        fair = probs.get(state.ticker)
        if fair is None:
            return []
        # pre-game only: in-play Kalshi vs a stored pre-game book line is
        # not a comparison, it's a mistake.
        start = self._start_time(event, probs)
        if start is None or state.ts >= start:
            return []
        if self._event_blocked(state, ctx):
            return []

        bid, ask = state.yes_bid, state.yes_ask
        mid = state.mid if (bid is not None and ask is not None) else None

        # (side, price paid for that side, net edge, limit price or None)
        candidates: list[tuple[str, float, float, float | None]] = []
        if self.prefer_maker and mid is not None:
            yes_px = _floor_cent(mid)            # join/inside quote, passive
            no_px = _floor_cent(1.0 - mid)
            if 0 < yes_px < 1:
                candidates.append((
                    "yes", yes_px,
                    fair - yes_px - maker_fee(yes_px, 1), yes_px,
                ))
            if 0 < no_px < 1:
                candidates.append((
                    "no", no_px,
                    (1.0 - fair) - no_px - maker_fee(no_px, 1), no_px,
                ))
        else:
            if ask is not None and ask > 0:
                candidates.append((
                    "yes", ask, fair - ask - taker_fee(ask, 1), None,
                ))
            if bid is not None and bid < 1:
                no_cost = 1.0 - bid
                candidates.append((
                    "no", no_cost,
                    (1.0 - fair) - no_cost - taker_fee(no_cost, 1), None,
                ))

        best: Signal | None = None
        best_net = 0.0
        for side, price, edge, limit in candidates:
            if price < self.min_entry_price:     # fee/adverse-selection floor
                continue
            if edge < self.min_net_edge:
                continue
            if edge > best_net:
                best_net = edge
                mode = "maker" if limit is not None else "taker"
                best = Signal(
                    ticker=state.ticker,
                    side=side,
                    contracts=self.contracts,
                    fair_value=fair,             # YES probability, both sides
                    limit_price=limit,           # priced in the bought side
                    reason=(
                        f"sportsbook_devig p={fair:.3f} {side}@{price:.2f} "
                        f"({mode}) edge={edge:+.3f}"
                    ),
                    strategy=self.name,
                )
        return [best] if best else []
