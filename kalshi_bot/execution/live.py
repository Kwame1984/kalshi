"""Live/demo executor: places real orders through the Kalshi V2 API.

V2 single-book order semantics
------------------------------
Kalshi V2 keeps ONE book per market, quoted in YES prices:
  * ``side='bid'``  = buy YES exposure at ``price`` (yes dollars).
  * ``side='ask'``  = sell YES exposure at ``price`` — which is exactly how
    you BUY NO: selling YES at yes-price P is buying NO at (1 - P).

Signal mapping (Signal.limit_price is always in YES terms, matching the
backtester: max yes buy price for 'yes' signals, min yes sell price for
'no' signals):

  Signal side 'yes' (buy YES)  -> create_order(side='bid',
      price = limit_price if set else current best YES ask)   # cross
  Signal side 'no'  (buy NO)   -> create_order(side='ask',
      price = limit_price if set else current best YES bid)   # cross
      The NO price paid per contract is (1 - price).

HARD GATE: construction against a production base URL raises unless
``settings.live_trading_allowed`` (trading_mode=live AND live_trading_ack=yes).
Demo mode must point at DEMO_BASE — a demo executor on a prod URL is refused.
"""

from __future__ import annotations

import sqlite3
import uuid

from kalshi_bot.api.client import DEMO_BASE, KalshiClient
from kalshi_bot.backtest.fees import maker_fee, taker_fee
from kalshi_bot.config import Settings
from kalshi_bot.execution import state
from kalshi_bot.execution.util import Book, parse_orderbook
from kalshi_bot.strategies.base import Signal

_PROD_HOSTS = ("api.elections.kalshi.com", "external-api.kalshi.com")


def _is_prod(base_url: str) -> bool:
    return any(h in base_url for h in _PROD_HOSTS)


class LiveExecutor:
    """Order placement for BOTH demo (DEMO_BASE) and live (PROD_BASE)."""

    def __init__(
        self,
        client: KalshiClient,
        conn: sqlite3.Connection,
        settings: Settings,
        mode: str,  # 'demo' | 'live'
    ):
        if mode not in ("demo", "live"):
            raise ValueError(f"LiveExecutor mode must be 'demo' or 'live', got {mode!r}")
        if _is_prod(client.base_url) and not settings.live_trading_allowed:
            raise RuntimeError(
                "REFUSED: LiveExecutor pointed at production without "
                "live_trading_allowed (set TRADING_MODE=live and "
                "LIVE_TRADING_ACK=yes to enable real-money trading)."
            )
        if mode == "demo" and DEMO_BASE.rstrip("/") not in client.base_url:
            raise RuntimeError(
                "REFUSED: demo mode requires the client to use DEMO_BASE "
                f"(got {client.base_url})."
            )
        if mode == "live" and not _is_prod(client.base_url):
            raise RuntimeError(
                f"REFUSED: live mode requires a production base URL (got {client.base_url})."
            )
        self.client = client
        self.conn = conn
        self.settings = settings
        self.mode = mode

    async def execute(
        self, sig: Signal, contracts: float, book: Book | None = None,
    ) -> dict | None:
        """Place the order mapped from `sig`; returns the API response order.

        Fills/positions are recorded optimistically at the submitted price
        (crossing orders should fill there or better); reconcile with
        GET /portfolio/fills for exact accounting.
        """
        if contracts < 1:
            return None
        if book is None:
            book = parse_orderbook(await self.client.get_orderbook(sig.ticker))

        if sig.side == "yes":
            order_side = "bid"
            price = sig.limit_price if sig.limit_price is not None else book.best_ask
        else:
            order_side = "ask"
            price = sig.limit_price if sig.limit_price is not None else book.best_bid
        if price is None or not (0.0 < price < 1.0):
            return None

        crossing = sig.limit_price is None
        resp = await self.client.create_order(
            ticker=sig.ticker,
            side=order_side,
            count=contracts,
            price=price,
            client_order_id=str(uuid.uuid4()),
        )
        order = resp.get("order", resp)
        order_id = order.get("order_id") or order.get("id") or ""

        side_price = price if sig.side == "yes" else round(1.0 - price, 4)
        fee = (taker_fee if crossing else maker_fee)(side_price, contracts)
        state.record_fill(self.conn, self.mode, sig.strategy, sig.ticker,
                          sig.side, contracts, side_price, fee, order_id=order_id)
        signed = contracts if sig.side == "yes" else -contracts
        state.apply_fill_to_position(self.conn, self.mode, sig.ticker, signed,
                                     price, sig.strategy)
        return order

    def check_resting(self, books: dict[str, Book]) -> list:
        """Real resting orders live on the exchange; nothing to simulate."""
        return []

    def snapshot_equity(self, marks: dict[str, float]) -> float:
        """Persist an equity point from local position state and marks."""
        cash = 0.0
        last = state.latest_equity(self.conn, self.mode)
        if last:
            cash = last["cash"]
        value = 0.0
        for t, p in state.get_positions(self.conn, self.mode).items():
            qty, avg = p["contracts"], p["avg_price"]
            mark = marks.get(t, avg)
            value += qty * mark if qty >= 0 else -qty * (1.0 - mark)
        exposure = state.total_exposure(self.conn, self.mode)
        equity = cash + value
        state.record_equity(self.conn, self.mode, cash, exposure, equity)
        return equity
