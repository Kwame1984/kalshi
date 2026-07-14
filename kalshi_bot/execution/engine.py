"""Execution engine: poll markets -> strategies -> risk -> executor.

Usage:
    python -m kalshi_bot.execution.engine --mode paper \
        --strategies expiry_convergence --series KXHIGHNY

Each cycle (default 60s):
  heartbeat -> refresh open markets for the configured series -> build a
  MarketState per market from the live orderbook (skip empty books) ->
  run enabled strategies with external signals + current positions ->
  risk-check every signal -> route approved ones to the executor ->
  persist signals/fills/positions/equity and daily_pnl to the state tables.

The engine NEVER routes orders while kill_switch=on (it keeps heart-beating
so the UI can see it alive). SIGINT/SIGTERM stop the loop gracefully.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import datetime as dt
import logging
import signal as _signal
import sqlite3
import sys

from kalshi_bot.api.client import DEMO_BASE, PROD_BASE, KalshiClient
from kalshi_bot.backtest.runner import load_context
from kalshi_bot.config import Settings, TradingMode, get_settings
from kalshi_bot.data.collector import collect_markets
from kalshi_bot.execution import state
from kalshi_bot.execution.live import LiveExecutor
from kalshi_bot.execution.paper import PaperExecutor
from kalshi_bot.execution.risk import RiskManager
from kalshi_bot.execution import settle
from kalshi_bot.execution.util import Book, now_iso, parse_orderbook, today_utc
from kalshi_bot.strategies import STRATEGIES
from kalshi_bot.strategies.base import MarketState, Strategy

log = logging.getLogger("kalshi_bot.engine")

DEFAULT_SERIES = ["KXHIGHNY"]


class ExecutionEngine:
    def __init__(
        self,
        settings: Settings,
        mode: str,
        strategies: list[Strategy],
        series: list[str],
        client: KalshiClient,
        executor,  # PaperExecutor | LiveExecutor
        conn: sqlite3.Connection | None = None,
        interval: float = 60.0,
        bankroll: float = 1000.0,
    ):
        self.settings = settings
        self.mode = mode
        self.strategies = strategies
        self.series = series
        self.client = client
        self.executor = executor
        self.conn = conn or state.connect()
        self.interval = interval
        self.risk = RiskManager(settings, self.conn, mode, bankroll=bankroll)
        self._stop = asyncio.Event()

    # -- lifecycle -------------------------------------------------------------

    def request_stop(self) -> None:
        self._stop.set()

    async def run(self, max_seconds: float | None = None) -> None:
        loop = asyncio.get_running_loop()
        for s in (_signal.SIGINT, _signal.SIGTERM):
            with contextlib.suppress(NotImplementedError):
                loop.add_signal_handler(s, self.request_stop)
        deadline = (
            loop.time() + max_seconds if max_seconds is not None else None
        )
        state.set_status(self.conn, "engine_running", "true")
        state.set_status(self.conn, "mode", self.mode)
        try:
            while not self._stop.is_set():
                try:
                    await self.cycle()
                except Exception:
                    log.exception("cycle failed")
                if deadline is not None and loop.time() >= deadline:
                    break
                with contextlib.suppress(asyncio.TimeoutError):
                    await asyncio.wait_for(self._stop.wait(), timeout=self.interval)
        finally:
            state.set_status(self.conn, "engine_running", "false")
            log.info("engine stopped")

    # -- one cycle ------------------------------------------------------------------

    async def cycle(self) -> None:
        now = dt.datetime.now(dt.UTC)
        state.set_status(self.conn, "last_heartbeat", now_iso())
        state.set_status(self.conn, "engine_running", "true")
        state.set_status(self.conn, "mode", self.mode)

        # 0. settle any resolved positions before marking equity.
        # Only for executors that track cash locally (paper); live/demo
        # reconcile positions and balance from the exchange itself.
        if hasattr(self.executor, "cash"):
            try:
                settled = await settle.settle_open_positions(
                    self.client, self.conn, self.mode)
                if settled:
                    self.executor.cash += sum(r.payout for r in settled)
                    log.info("settled %d positions, +%.2f payout",
                             len(settled), sum(r.payout for r in settled))
            except Exception:
                log.exception("settlement step failed")

        # 1. refresh open markets for the configured series
        markets: list[dict] = []
        for st in self.series:
            markets.extend(await collect_markets(self.client, self.conn, st, status="open"))

        # 2. orderbooks -> MarketStates (skip empty books)
        books: dict[str, Book] = {}
        states: list[MarketState] = []
        marks: dict[str, float] = {}
        for m in markets:
            ticker = m["ticker"]
            try:
                book = parse_orderbook(await self.client.get_orderbook(ticker))
            except Exception:
                log.warning("orderbook fetch failed for %s", ticker)
                continue
            if book.empty:
                continue
            books[ticker] = book
            if book.best_bid is not None:
                marks[ticker] = book.best_bid
            states.append(MarketState(
                ticker=ticker, ts=now,
                yes_bid=book.best_bid, yes_ask=book.best_ask,
                last=_f(m.get("last_price_dollars")),
                volume=float(m.get("volume_fp") or m.get("volume") or 0),
                open_interest=float(m.get("open_interest_fp") or m.get("open_interest") or 0),
                meta=m,
            ))

        # 3. resting orders from previous cycles
        killed = self.risk.kill_switch_on()
        if not killed:
            for f in self.executor.check_resting(books):
                log.info("resting fill: %s %s x%.1f @ %.2f", f.ticker, f.side,
                         f.contracts, f.price)

        # 4. mark equity, refresh daily pnl (before gating new trades)
        equity = self.executor.snapshot_equity(marks)
        day_start = state.equity_at_day_start(self.conn, self.mode, today_utc())
        daily_pnl = equity - day_start if day_start is not None else 0.0
        state.set_status(self.conn, "daily_pnl", f"{daily_pnl:.2f}")

        # 5. strategies -> risk -> executor
        ctx = load_context()
        ctx.positions = {
            t: p["contracts"] for t, p in state.get_positions(self.conn, self.mode).items()
        }
        n_signals = n_acted = 0
        for ms in states:
            book = books[ms.ticker]
            for strat in self.strategies:
                for sig in strat.generate_signals(ms, ctx):
                    n_signals += 1
                    exec_price = book.best_ask if sig.side == "yes" else book.best_bid
                    acted = False
                    if killed or self.risk.kill_switch_on():
                        reason = "kill_switch on"
                    elif exec_price is None:
                        reason = "no opposing liquidity"
                    else:
                        decision = self.risk.check(sig, exec_price)
                        reason = decision.reason
                        if decision.allowed:
                            result = await self.executor.execute(
                                sig, decision.contracts, book
                            )
                            acted = result is not None
                    state.record_signal(
                        self.conn, sig.strategy, sig.ticker, sig.side,
                        sig.contracts, sig.fair_value,
                        exec_price, f"{sig.reason} | risk: {reason}", acted,
                    )
                    n_acted += int(acted)

        log.info("cycle done: %d markets, %d books, %d signals, %d acted, "
                 "equity=%.2f daily_pnl=%.2f%s",
                 len(markets), len(books), n_signals, n_acted, equity, daily_pnl,
                 " [KILL SWITCH ON]" if killed else "")


def _f(v) -> float | None:
    try:
        return float(v) if v is not None else None
    except (TypeError, ValueError):
        return None


# -- CLI ------------------------------------------------------------------------


def build(args: argparse.Namespace, settings: Settings) -> ExecutionEngine:
    mode = args.mode or settings.trading_mode.value
    if mode == TradingMode.LIVE.value and not settings.live_trading_allowed:
        sys.exit("REFUSED: --mode live requires TRADING_MODE=live and "
                 "LIVE_TRADING_ACK=yes in settings.")

    conn = state.connect()
    if mode == "paper":
        client = KalshiClient(base_url=PROD_BASE)  # public data only, no creds
        executor = PaperExecutor(client, conn, starting_cash=args.bankroll)
    else:
        base = DEMO_BASE if mode == "demo" else PROD_BASE
        key_id, key_path = settings.credentials_for(TradingMode(mode))
        client = KalshiClient(
            api_key_id=key_id,
            private_key_path=key_path,
            base_url=base,
        )
        executor = LiveExecutor(client, conn, settings, mode)

    strategies = [STRATEGIES[name]() for name in args.strategies]
    return ExecutionEngine(
        settings=settings, mode=mode, strategies=strategies, series=args.series,
        client=client, executor=executor, conn=conn, interval=args.interval,
        bankroll=args.bankroll,
    )


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    ap = argparse.ArgumentParser(description="Kalshi execution engine")
    ap.add_argument("--mode", choices=[m.value for m in TradingMode], default=None,
                    help="override settings.trading_mode (live still requires ack)")
    ap.add_argument("--strategies", nargs="+", default=["expiry_convergence"],
                    choices=sorted(STRATEGIES))
    ap.add_argument("--series", nargs="+", default=DEFAULT_SERIES)
    ap.add_argument("--interval", type=float, default=60.0)
    ap.add_argument("--bankroll", type=float, default=1000.0)
    ap.add_argument("--duration", type=float, default=None,
                    help="stop after N seconds (default: run until SIGINT)")
    args = ap.parse_args()

    settings = get_settings()
    engine = build(args, settings)

    async def _run() -> None:
        try:
            await engine.run(max_seconds=args.duration)
        finally:
            await engine.client.aclose()
            engine.conn.close()

    asyncio.run(_run())


if __name__ == "__main__":
    main()
