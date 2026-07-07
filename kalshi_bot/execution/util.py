"""Shared helpers for the execution layer.

Orderbook conventions (Kalshi V2):
    GET /markets/{ticker}/orderbook returns either
        {"orderbook_fp": {"yes_dollars": [["0.95","183.0"], ...],
                          "no_dollars":  [...]}}                (current)
    or  {"orderbook":    {"yes": [[95, 183], ...], "no": [...]}} (legacy cents)

    Each level is a resting BUY order for that side:
      * ``yes`` levels are bids to buy YES  -> the YES bid side.
      * ``no``  levels are bids to buy NO   -> selling to them = selling YES,
        so the YES ask side is (1 - no_price) with the no level's size.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field

from kalshi_bot.data.store import dollars


def now_iso() -> str:
    """UTC timestamp string used across all state tables."""
    return dt.datetime.now(dt.UTC).isoformat(timespec="seconds")


def today_utc() -> str:
    return dt.datetime.now(dt.UTC).date().isoformat()


@dataclass
class Book:
    """Normalized YES-side view of an orderbook.

    bids: [(yes_price, size)] best (highest) first  -- resting YES buyers
    asks: [(yes_price, size)] best (lowest)  first  -- derived from NO buyers
    """

    bids: list[tuple[float, float]] = field(default_factory=list)
    asks: list[tuple[float, float]] = field(default_factory=list)

    @property
    def best_bid(self) -> float | None:
        return self.bids[0][0] if self.bids else None

    @property
    def best_ask(self) -> float | None:
        return self.asks[0][0] if self.asks else None

    @property
    def empty(self) -> bool:
        return not self.bids and not self.asks


def parse_orderbook(raw: dict) -> Book:
    """Normalize a GetOrderbook response (either shape) into a ``Book``."""
    ob = raw.get("orderbook_fp") or raw.get("orderbook") or raw
    yes_levels = ob.get("yes_dollars") if ob.get("yes_dollars") is not None else ob.get("yes")
    no_levels = ob.get("no_dollars") if ob.get("no_dollars") is not None else ob.get("no")

    def norm(levels) -> list[tuple[float, float]]:
        out = []
        for lvl in levels or []:
            price, size = lvl[0], lvl[1]
            out.append((float(dollars(price)), float(size)))
        return out

    yes_bids = sorted(norm(yes_levels), key=lambda x: -x[0])
    no_bids = sorted(norm(no_levels), key=lambda x: -x[0])
    asks = [(round(1.0 - p, 4), sz) for p, sz in no_bids]  # best (lowest) first
    return Book(bids=yes_bids, asks=asks)
