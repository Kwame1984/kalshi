from kalshi_bot.execution import state


def _conn(tmp_path):
    return state.connect(tmp_path / "t.db")


def test_record_settlement_roundtrip(tmp_path):
    conn = _conn(tmp_path)
    rid = state.record_settlement(
        conn, "paper", "tail_seller", "KXHIGHCHI-26JUL07-T81",
        contracts=-25.0, result="no", payout=25.0,
        cost_basis=24.25, realized_pnl=0.75,
    )
    assert rid > 0
    row = conn.execute(
        "SELECT * FROM bot_settlements WHERE id=?", (rid,)
    ).fetchone()
    assert row["mode"] == "paper"
    assert row["strategy"] == "tail_seller"
    assert row["result"] == "no"
    assert row["payout"] == 25.0
    assert row["cost_basis"] == 24.25
    assert round(row["realized_pnl"], 4) == 0.75
