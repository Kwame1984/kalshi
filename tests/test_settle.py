import asyncio

from kalshi_bot.execution import settle, state


class FakeClient:
    """get_market returns canned market dicts keyed by ticker."""
    def __init__(self, results: dict):
        self.results = results  # ticker -> {"status":..., "result":...}
        self.calls = []

    async def get_market(self, ticker: str) -> dict:
        self.calls.append(ticker)
        return self.results[ticker]


def _conn(tmp_path):
    return state.connect(tmp_path / "t.db")


def _seed_no_position(conn, ticker="KXHIGHCHI-26JUL07-T81"):
    # long 25 NO @ NO-price 0.97  ->  contracts=-25, avg(YES)=0.03
    state.apply_fill_to_position(conn, "paper", ticker, -25.0, 0.03, "tail_seller")


def test_no_result_wins_no_position(tmp_path):
    conn = _conn(tmp_path)
    _seed_no_position(conn)
    client = FakeClient({"KXHIGHCHI-26JUL07-T81": {"status": "settled", "result": "no"}})
    recs = asyncio.run(settle.settle_open_positions(client, conn, "paper"))
    assert len(recs) == 1
    r = recs[0]
    assert r.result == "no"
    assert r.payout == 25.0
    assert round(r.cost_basis, 4) == 24.25
    assert round(r.realized_pnl, 4) == 0.75
    assert state.get_positions(conn, "paper") == {}
    assert conn.execute("SELECT COUNT(*) c FROM bot_settlements").fetchone()["c"] == 1


def test_yes_result_loses_no_position(tmp_path):
    conn = _conn(tmp_path)
    _seed_no_position(conn)
    client = FakeClient({"KXHIGHCHI-26JUL07-T81": {"status": "settled", "result": "yes"}})
    recs = asyncio.run(settle.settle_open_positions(client, conn, "paper"))
    assert recs[0].payout == 0.0
    assert round(recs[0].realized_pnl, 4) == -24.25
    assert state.get_positions(conn, "paper") == {}


def test_yes_result_wins_yes_position(tmp_path):
    conn = _conn(tmp_path)
    state.apply_fill_to_position(conn, "paper", "KXFOO", 10.0, 0.40, "s")  # long 10 YES @0.40
    client = FakeClient({"KXFOO": {"status": "settled", "result": "yes"}})
    recs = asyncio.run(settle.settle_open_positions(client, conn, "paper"))
    assert recs[0].payout == 10.0
    assert round(recs[0].cost_basis, 4) == 4.0
    assert round(recs[0].realized_pnl, 4) == 6.0


def test_no_result_loses_yes_position(tmp_path):
    conn = _conn(tmp_path)
    state.apply_fill_to_position(conn, "paper", "KXFOO", 10.0, 0.40, "s")
    client = FakeClient({"KXFOO": {"status": "settled", "result": "no"}})
    recs = asyncio.run(settle.settle_open_positions(client, conn, "paper"))
    assert recs[0].payout == 0.0
    assert round(recs[0].realized_pnl, 4) == -4.0


def test_void_refunds_cost_basis(tmp_path):
    conn = _conn(tmp_path)
    _seed_no_position(conn)
    client = FakeClient({"KXHIGHCHI-26JUL07-T81": {"status": "settled", "result": "void"}})
    recs = asyncio.run(settle.settle_open_positions(client, conn, "paper"))
    assert round(recs[0].payout, 4) == 24.25
    assert round(recs[0].realized_pnl, 4) == 0.0
    assert state.get_positions(conn, "paper") == {}


def test_undetermined_left_open(tmp_path):
    conn = _conn(tmp_path)
    _seed_no_position(conn)
    client = FakeClient({"KXHIGHCHI-26JUL07-T81": {"status": "active", "result": ""}})
    recs = asyncio.run(settle.settle_open_positions(client, conn, "paper"))
    assert recs == []
    assert "KXHIGHCHI-26JUL07-T81" in state.get_positions(conn, "paper")
    assert conn.execute("SELECT COUNT(*) c FROM bot_settlements").fetchone()["c"] == 0


def test_idempotent_second_run(tmp_path):
    conn = _conn(tmp_path)
    _seed_no_position(conn)
    client = FakeClient({"KXHIGHCHI-26JUL07-T81": {"status": "settled", "result": "no"}})
    asyncio.run(settle.settle_open_positions(client, conn, "paper"))
    recs2 = asyncio.run(settle.settle_open_positions(client, conn, "paper"))
    assert recs2 == []
    assert conn.execute("SELECT COUNT(*) c FROM bot_settlements").fetchone()["c"] == 1


def test_apply_to_equity_reconciles_cash(tmp_path):
    conn = _conn(tmp_path)
    # prior snapshot: cash 900, one open unrelated position worth ~cost
    state.apply_fill_to_position(conn, "paper", "KXOTHER", -10.0, 0.05, "s")  # NO basis
    state.record_equity(conn, "paper", cash=900.0, exposure=9.5, equity=909.5)
    # a settlement worth +25 payout
    recs = [settle.SettlementRecord("KXX", "s", -25.0, "no", 25.0, 24.25, 0.75)]
    equity = settle.apply_to_equity(conn, "paper", recs)
    # new cash = 900 + 25 = 925; remaining KXOTHER marks at 10*(1-0.05)=9.5
    assert round(equity, 4) == round(925.0 + 9.5, 4)
    last = state.latest_equity(conn, "paper")
    assert round(last["cash"], 4) == 925.0
