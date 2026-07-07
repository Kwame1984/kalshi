"""Execution layer tests. No network: KalshiClient is faked throughout."""

from __future__ import annotations

import pytest

from kalshi_bot.api.client import DEMO_BASE, PROD_BASE
from kalshi_bot.backtest.fees import maker_fee, taker_fee
from kalshi_bot.config import Settings
from kalshi_bot.execution import state
from kalshi_bot.execution.live import LiveExecutor
from kalshi_bot.execution.paper import PaperExecutor
from kalshi_bot.execution.risk import RiskManager
from kalshi_bot.execution.util import Book, parse_orderbook
from kalshi_bot.strategies.base import Signal


class FakeClient:
    """Stands in for KalshiClient; never touches the network."""

    def __init__(self, base_url: str = DEMO_BASE, orderbooks: dict | None = None):
        self.base_url = base_url
        self.orderbooks = orderbooks or {}
        self.orders: list[dict] = []

    async def get_orderbook(self, ticker: str, depth: int = 20) -> dict:
        return self.orderbooks[ticker]

    async def create_order(self, ticker, side, count, price, **kw) -> dict:
        self.orders.append(
            {"ticker": ticker, "side": side, "count": count, "price": price, **kw}
        )
        return {"order": {"order_id": f"ord-{len(self.orders)}"}}


def make_settings(**over) -> Settings:
    base = dict(
        trading_mode="paper", live_trading_ack="no",
        max_position_per_market=50.0, max_daily_loss=25.0,
        max_total_exposure=300.0,
    )
    base.update(over)
    return Settings(_env_file=None, **base)


@pytest.fixture
def conn(tmp_path):
    c = state.connect(tmp_path / "test.db")
    yield c
    c.close()


def sig_yes(ticker="MKT-A", contracts=25.0, fair=0.97, limit=None) -> Signal:
    return Signal(ticker=ticker, side="yes", contracts=contracts,
                  fair_value=fair, limit_price=limit, strategy="test")


def sig_no(ticker="MKT-A", contracts=25.0, fair=0.03, limit=None) -> Signal:
    # fair_value is the YES probability; fair=0.03 => NO is 97% likely
    return Signal(ticker=ticker, side="no", contracts=contracts,
                  fair_value=fair, limit_price=limit, strategy="test")


# -- orderbook parsing ---------------------------------------------------------


def test_parse_orderbook_fp_format():
    raw = {"orderbook_fp": {
        "yes_dollars": [["0.0500", "40.00"], ["0.0400", "10.00"]],
        "no_dollars": [["0.9400", "183.00"], ["0.9500", "120.00"]],
    }}
    book = parse_orderbook(raw)
    assert book.best_bid == 0.05
    # best ask = 1 - best (highest) no bid = 1 - 0.95
    assert book.best_ask == pytest.approx(0.05)
    assert book.asks[0] == (pytest.approx(0.05), 120.0)


def test_parse_orderbook_legacy_cents():
    raw = {"orderbook": {"yes": [[93, 10]], "no": [[5, 20]]}}
    book = parse_orderbook(raw)
    assert book.best_bid == pytest.approx(0.93)
    assert book.best_ask == pytest.approx(0.95)


# -- risk: caps ------------------------------------------------------------------


def test_per_market_cap_blocks(conn):
    rm = RiskManager(make_settings(), conn, "paper")
    # existing position eats the whole $50 per-market cap
    state.apply_fill_to_position(conn, "paper", "MKT-A", 53, 0.95, "test")
    assert state.market_exposure(conn, "paper", "MKT-A") == pytest.approx(50.35)
    d = rm.check(sig_yes("MKT-A"), price=0.94)
    assert not d.allowed
    assert d.contracts == 0


def test_per_market_cap_sizes_down(conn):
    rm = RiskManager(make_settings(), conn, "paper")
    # $40 already committed -> only ~$10 of room
    state.apply_fill_to_position(conn, "paper", "MKT-A", 50, 0.80, "test")
    d = rm.check(sig_yes("MKT-A", contracts=100), price=0.94)
    assert d.allowed
    assert d.contracts <= 10 / 0.94 + 1e-9
    assert d.contracts >= 1


def test_total_exposure_cap_blocks(conn):
    rm = RiskManager(make_settings(max_total_exposure=10.0), conn, "paper")
    state.apply_fill_to_position(conn, "paper", "MKT-B", 10, 0.80, "test")  # $8
    d = rm.check(sig_yes("MKT-A", contracts=10), price=0.94)  # wants >$3 more
    assert not d.allowed
    assert "total exposure" in d.reason


# -- risk: kill switch & daily loss -----------------------------------------------


def test_kill_switch_halts(conn):
    rm = RiskManager(make_settings(), conn, "paper")
    assert rm.check(sig_yes(), price=0.94).allowed  # sanity: passes when off
    state.set_status(conn, "kill_switch", "on")
    d = rm.check(sig_yes(), price=0.94)
    assert not d.allowed
    assert "kill_switch" in d.reason


def test_halt_sets_status(conn):
    rm = RiskManager(make_settings(), conn, "paper")
    rm.halt("manual stop")
    assert state.get_status(conn, "kill_switch") == "on"
    assert state.get_status(conn, "halt_reason") == "manual stop"
    assert rm.kill_switch_on()


def test_daily_loss_halt_triggers(conn):
    rm = RiskManager(make_settings(), conn, "paper")  # max_daily_loss = 25
    state.set_status(conn, "daily_pnl", "-26.00")
    d = rm.check(sig_yes(), price=0.94)
    assert not d.allowed
    assert state.get_status(conn, "kill_switch") == "on"
    assert "daily loss" in (state.get_status(conn, "halt_reason") or "")
    # and it stays blocked via the kill switch afterwards
    assert not rm.check(sig_yes(), price=0.94).allowed


# -- risk: sizing -------------------------------------------------------------------


def test_quarter_kelly_sizing(conn):
    rm = RiskManager(make_settings(), conn, "paper", bankroll=1000.0)
    # fair 0.97 @ 0.94: kelly = 0.03/0.06 = 0.5 -> $125 quarter-Kelly, capped at $50
    n = rm.size(sig_yes(contracts=100), price=0.94)
    assert n == 53  # floor(50 / 0.94)
    # never exceeds what the signal asked for
    assert rm.size(sig_yes(contracts=25), price=0.94) == 25
    # no edge -> zero
    assert rm.size(sig_yes(fair=0.90), price=0.94) == 0


def test_sizing_no_side_uses_no_price(conn):
    rm = RiskManager(make_settings(), conn, "paper", bankroll=1000.0)
    # NO at yes bid 0.06 -> no price 0.94, no fair 0.97: same math as above
    n = rm.size(sig_no(contracts=100, fair=0.03), price=0.06)
    assert n == 53


# -- paper executor ---------------------------------------------------------------


async def test_paper_fill_math_yes(conn):
    client = FakeClient()
    ex = PaperExecutor(client, conn, starting_cash=1000.0)
    book = Book(bids=[(0.90, 50.0)], asks=[(0.94, 100.0)])
    fill = await ex.execute(sig_yes(contracts=10), 10, book)
    assert fill is not None and not fill.resting
    assert fill.price == 0.94
    expected_fee = taker_fee(0.94, 10)
    assert fill.fee == pytest.approx(expected_fee)
    assert ex.cash == pytest.approx(1000.0 - 10 * 0.94 - expected_fee)
    pos = state.get_positions(conn, "paper")["MKT-A"]
    assert pos["contracts"] == 10
    assert pos["avg_price"] == pytest.approx(0.94)
    fills = conn.execute("SELECT * FROM bot_fills WHERE mode='paper'").fetchall()
    assert len(fills) == 1
    assert fills[0]["side"] == "yes" and fills[0]["price"] == pytest.approx(0.94)


async def test_paper_fill_no_side(conn):
    ex = PaperExecutor(FakeClient(), conn, starting_cash=1000.0)
    book = Book(bids=[(0.06, 40.0)], asks=[(0.08, 10.0)])
    fill = await ex.execute(sig_no(contracts=20), 20, book)
    # buys NO at 1 - yes_bid = 0.94
    assert fill.price == pytest.approx(0.94)
    expected_fee = taker_fee(0.94, 20)
    assert ex.cash == pytest.approx(1000.0 - 20 * 0.94 - expected_fee)
    pos = state.get_positions(conn, "paper")["MKT-A"]
    assert pos["contracts"] == -20  # signed yes-contracts
    assert pos["avg_price"] == pytest.approx(0.06)


async def test_paper_fill_capped_by_book_size(conn):
    ex = PaperExecutor(FakeClient(), conn, starting_cash=1000.0)
    book = Book(bids=[], asks=[(0.50, 7.0)])
    fill = await ex.execute(sig_yes(contracts=25, fair=0.60), 25, book)
    assert fill.contracts == 7  # only best-level size available


async def test_paper_resting_then_maker_fill(conn):
    ex = PaperExecutor(FakeClient(), conn, starting_cash=1000.0)
    book = Book(bids=[(0.88, 10.0)], asks=[(0.93, 10.0)])
    res = await ex.execute(sig_yes(contracts=10, limit=0.90), 10, book)
    assert res.resting
    assert ex.cash == 1000.0  # nothing spent yet
    assert not state.get_positions(conn, "paper")  # no position yet

    # book has not crossed: still resting
    assert ex.check_resting({"MKT-A": book}) == []
    # book crosses the limit -> fill at limit with MAKER fee
    crossed = Book(bids=[(0.85, 10.0)], asks=[(0.89, 20.0)])
    fills = ex.check_resting({"MKT-A": crossed})
    assert len(fills) == 1
    f = fills[0]
    assert f.yes_price == 0.90
    assert f.fee == pytest.approx(maker_fee(0.90, 10))
    assert ex.cash == pytest.approx(1000.0 - 10 * 0.90 - f.fee)
    assert ex.resting == []


async def test_paper_marketable_limit_fills_immediately(conn):
    ex = PaperExecutor(FakeClient(), conn, starting_cash=1000.0)
    book = Book(bids=[], asks=[(0.93, 10.0)])
    fill = await ex.execute(sig_yes(contracts=5, limit=0.95), 5, book)
    assert not fill.resting
    assert fill.price == 0.93  # fills at the better book price


def test_paper_equity_snapshot(conn):
    ex = PaperExecutor(FakeClient(), conn, starting_cash=1000.0)
    state.apply_fill_to_position(conn, "paper", "MKT-A", 10, 0.90, "test")
    ex.cash = 1000.0 - 9.0
    equity = ex.snapshot_equity({"MKT-A": 0.95})
    assert equity == pytest.approx(991.0 + 10 * 0.95)
    row = state.latest_equity(conn, "paper")
    assert row["exposure"] == pytest.approx(9.0)
    assert row["equity"] == pytest.approx(equity)


# -- live executor: hard gate -------------------------------------------------------


def test_live_executor_refuses_prod_without_ack(conn):
    client = FakeClient(base_url=PROD_BASE)
    with pytest.raises(RuntimeError, match="REFUSED"):
        LiveExecutor(client, conn, make_settings(), mode="live")


def test_live_executor_refuses_prod_even_in_demo_mode(conn):
    client = FakeClient(base_url=PROD_BASE)
    with pytest.raises(RuntimeError, match="REFUSED"):
        LiveExecutor(client, conn, make_settings(), mode="demo")


def test_live_executor_allows_demo_base(conn):
    ex = LiveExecutor(FakeClient(base_url=DEMO_BASE), conn, make_settings(), mode="demo")
    assert ex.mode == "demo"


def test_live_executor_allows_prod_with_full_ack(conn):
    settings = make_settings(trading_mode="live", live_trading_ack="yes")
    assert settings.live_trading_allowed
    ex = LiveExecutor(FakeClient(base_url=PROD_BASE), conn, settings, mode="live")
    assert ex.mode == "live"


def test_live_executor_refuses_demo_base_in_live_mode(conn):
    settings = make_settings(trading_mode="live", live_trading_ack="yes")
    with pytest.raises(RuntimeError, match="REFUSED"):
        LiveExecutor(FakeClient(base_url=DEMO_BASE), conn, settings, mode="live")


# -- live executor: signal -> V2 order mapping ----------------------------------------


async def test_v2_mapping_yes_limit(conn):
    client = FakeClient(base_url=DEMO_BASE)
    ex = LiveExecutor(client, conn, make_settings(), mode="demo")
    book = Book(bids=[(0.90, 10.0)], asks=[(0.94, 10.0)])
    order = await ex.execute(sig_yes(contracts=10, limit=0.92), 10, book)
    assert order["order_id"] == "ord-1"
    o = client.orders[0]
    # buy YES = bid on the single yes-priced book, at the yes limit
    assert o["side"] == "bid"
    assert o["price"] == 0.92
    assert o["count"] == 10


async def test_v2_mapping_yes_market_crosses_ask(conn):
    client = FakeClient(base_url=DEMO_BASE)
    ex = LiveExecutor(client, conn, make_settings(), mode="demo")
    book = Book(bids=[(0.90, 10.0)], asks=[(0.94, 10.0)])
    await ex.execute(sig_yes(contracts=10), 10, book)
    assert client.orders[0]["side"] == "bid"
    assert client.orders[0]["price"] == 0.94  # takes the ask


async def test_v2_mapping_no_side(conn):
    client = FakeClient(base_url=DEMO_BASE)
    ex = LiveExecutor(client, conn, make_settings(), mode="demo")
    book = Book(bids=[(0.06, 40.0)], asks=[(0.08, 10.0)])
    # buy NO = sell YES exposure = 'ask'; no limit -> cross at the yes bid.
    await ex.execute(sig_no(contracts=20), 20, book)
    o = client.orders[0]
    assert o["side"] == "ask"
    assert o["price"] == 0.06  # yes price; NO costs 1 - 0.06 = 0.94
    # recorded fill stores the NO price actually paid
    row = conn.execute("SELECT * FROM bot_fills").fetchone()
    assert row["side"] == "no"
    assert row["price"] == pytest.approx(0.94)
    pos = state.get_positions(conn, "demo")["MKT-A"]
    assert pos["contracts"] == -20


async def test_v2_mapping_no_side_with_limit(conn):
    client = FakeClient(base_url=DEMO_BASE)
    ex = LiveExecutor(client, conn, make_settings(), mode="demo")
    book = Book(bids=[(0.06, 40.0)], asks=[(0.08, 10.0)])
    # limit is in YES terms: min yes sell price 0.05 (max NO price 0.95)
    await ex.execute(sig_no(contracts=20, limit=0.05), 20, book)
    o = client.orders[0]
    assert o["side"] == "ask"
    assert o["price"] == 0.05


async def test_live_executor_rejects_sub_one_contract(conn):
    client = FakeClient(base_url=DEMO_BASE)
    ex = LiveExecutor(client, conn, make_settings(), mode="demo")
    book = Book(bids=[(0.90, 10.0)], asks=[(0.94, 10.0)])
    assert await ex.execute(sig_yes(), 0, book) is None
    assert client.orders == []


# -- state schema contract ----------------------------------------------------------


def test_state_schema_contract(conn):
    cols = {
        "bot_fills": ["id", "ts", "mode", "strategy", "ticker", "side",
                      "contracts", "price", "fee", "order_id"],
        "bot_positions": ["ticker", "mode", "contracts", "avg_price",
                          "strategy", "updated_at"],
        "bot_signals": ["id", "ts", "strategy", "ticker", "side", "contracts",
                        "fair_value", "market_price", "reason", "acted"],
        "bot_equity": ["ts", "mode", "cash", "exposure", "equity"],
        "bot_status": ["key", "value"],
    }
    for table, expected in cols.items():
        actual = [r["name"] for r in conn.execute(f"PRAGMA table_info({table})")]
        assert actual == expected, f"{table}: {actual}"
