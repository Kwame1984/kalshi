import asyncio

from kalshi_bot.execution import settle, state


class FakeClient:
    def __init__(self, results):
        self.results = results

    async def get_market(self, ticker):
        return self.results[ticker]


def test_engine_step0_credits_cash_and_closes(tmp_path):
    """settle_open_positions + caller cash-apply mirrors engine step 0."""
    conn = state.connect(tmp_path / "t.db")
    state.apply_fill_to_position(conn, "paper", "KXHIGHCHI-26JUL07-T81",
                                 -25.0, 0.03, "tail_seller")
    client = FakeClient({"KXHIGHCHI-26JUL07-T81":
                         {"status": "settled", "result": "no"}})

    class Exec:
        cash = 975.69
    ex = Exec()
    records = asyncio.run(settle.settle_open_positions(client, conn, "paper"))
    ex.cash += sum(r.payout for r in records)

    assert round(ex.cash, 2) == round(975.69 + 25.0, 2)
    assert state.get_positions(conn, "paper") == {}
