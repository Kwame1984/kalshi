"""Paper executor: simulates fills against the live orderbook.

Fill model (deliberately simple and conservative):
  * Marketable orders cross the spread at the single best level, up to that
    level's available size, and pay the quadratic TAKER fee.
  * If ``Signal.limit_price`` is set and the order is not marketable, it is
    recorded as a resting order and re-checked each engine cycle; when the
    book crosses the limit it fills at the limit price with the MAKER fee.
  * ``limit_price`` is always in YES terms (max yes buy price for 'yes'
    signals; min yes sell price for 'no' signals), matching the backtester.

Positions are one signed yes-contract ledger per ticker (buying NO records
negative yes-contracts priced at the yes bid). Cash starts at
``starting_cash`` and resumes from the last bot_equity snapshot for
mode='paper' if one exists.
"""

from __future__ import annotations

import itertools
import sqlite3
from dataclasses import dataclass

from kalshi_bot.api.client import KalshiClient
from kalshi_bot.backtest.fees import maker_fee, taker_fee
from kalshi_bot.execution import state
from kalshi_bot.execution.util import Book, parse_orderbook
from kalshi_bot.strategies.base import Signal

MODE = "paper"


@dataclass
class RestingOrder:
    order_id: str
    ticker: str
    side: str          # 'yes' | 'no'
    contracts: float
    limit_price: float  # YES terms
    strategy: str


@dataclass
class PaperFill:
    ticker: str
    side: str
    contracts: float   # positive, in this side's units
    price: float       # price paid per contract for this side
    yes_price: float   # execution price in YES terms
    fee: float
    resting: bool = False


class PaperExecutor:
    def __init__(
        self,
        client: KalshiClient,
        conn: sqlite3.Connection,
        starting_cash: float = 1000.0,
    ):
        self.client = client
        self.conn = conn
        self.mode = MODE
        last = state.latest_equity(conn, MODE)
        self.cash = last["cash"] if last else starting_cash
        self.resting: list[RestingOrder] = []
        self._oid = itertools.count(1)

    # -- fills ----------------------------------------------------------------

    async def execute(
        self, sig: Signal, contracts: float, book: Book | None = None,
    ) -> PaperFill | None:
        """Simulate execution of `contracts` of `sig` against `book`.

        Returns a PaperFill (resting=True if parked), or None if unfillable.
        """
        if book is None:
            book = parse_orderbook(await self.client.get_orderbook(sig.ticker))
        if contracts < 1:
            return None

        if sig.side == "yes":
            level = book.asks[0] if book.asks else None
            marketable = level is not None and (
                sig.limit_price is None or level[0] <= sig.limit_price
            )
        else:
            level = book.bids[0] if book.bids else None
            marketable = level is not None and (
                sig.limit_price is None or level[0] >= sig.limit_price
            )

        if marketable:
            yes_price, avail = level
            qty = min(contracts, avail)
            if qty < 1:
                return None
            return self._fill(sig.ticker, sig.side, qty, yes_price, sig.strategy,
                              taker=True)

        if sig.limit_price is not None:
            order = RestingOrder(
                order_id=f"paper-rest-{next(self._oid)}",
                ticker=sig.ticker, side=sig.side, contracts=contracts,
                limit_price=sig.limit_price, strategy=sig.strategy,
            )
            self.resting.append(order)
            return PaperFill(sig.ticker, sig.side, contracts, 0.0, sig.limit_price,
                             0.0, resting=True)
        return None

    def check_resting(self, books: dict[str, Book]) -> list[PaperFill]:
        """Fill resting orders whose limit the book has crossed (maker fee)."""
        fills, still = [], []
        for o in self.resting:
            book = books.get(o.ticker)
            crossed = book is not None and (
                (o.side == "yes" and book.best_ask is not None
                 and book.best_ask <= o.limit_price)
                or (o.side == "no" and book.best_bid is not None
                    and book.best_bid >= o.limit_price)
            )
            if crossed:
                fills.append(self._fill(o.ticker, o.side, o.contracts,
                                        o.limit_price, o.strategy, taker=False,
                                        order_id=o.order_id))
            else:
                still.append(o)
        self.resting = still
        return fills

    def _fill(
        self, ticker: str, side: str, qty: float, yes_price: float,
        strategy: str, taker: bool, order_id: str = "",
    ) -> PaperFill:
        side_price = yes_price if side == "yes" else round(1.0 - yes_price, 4)
        fee_fn = taker_fee if taker else maker_fee
        fee = fee_fn(side_price, qty)
        self.cash -= qty * side_price + fee
        oid = order_id or f"paper-{next(self._oid)}"
        state.record_fill(self.conn, MODE, strategy, ticker, side, qty,
                          side_price, fee, order_id=oid)
        signed = qty if side == "yes" else -qty
        state.apply_fill_to_position(self.conn, MODE, ticker, signed, yes_price,
                                     strategy)
        return PaperFill(ticker, side, qty, side_price, yes_price, fee)

    # -- equity ------------------------------------------------------------------

    def snapshot_equity(self, marks: dict[str, float]) -> float:
        """Persist an equity point. marks: ticker -> yes mark price.

        Long YES marks at the yes price; long NO marks at (1 - yes price).
        Positions with no mark fall back to their cost basis.
        """
        value = 0.0
        for t, p in state.get_positions(self.conn, MODE).items():
            qty, avg = p["contracts"], p["avg_price"]
            mark = marks.get(t)
            if mark is None:
                mark = avg
            value += qty * mark if qty >= 0 else -qty * (1.0 - mark)
        exposure = state.total_exposure(self.conn, MODE)
        equity = self.cash + value
        state.record_equity(self.conn, MODE, self.cash, exposure, equity)
        return equity
