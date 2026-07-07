"""Dashboard API tests — run against a throwaway SQLite db.

Covers: sane shapes with empty/missing bot_* tables, data pass-through once
tables exist, and the kill-switch write round-trip.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from kalshi_bot.ui.server import create_app


@pytest.fixture()
def db_path(tmp_path: Path) -> Path:
    return tmp_path / "test.db"


@pytest.fixture()
def client(db_path: Path) -> TestClient:
    return TestClient(create_app(db_path=db_path))


def _seed(db_path: Path) -> None:
    conn = sqlite3.connect(db_path)
    conn.executescript(
        """
        CREATE TABLE markets (
            ticker TEXT PRIMARY KEY, event_ticker TEXT, series_ticker TEXT,
            title TEXT, subtitle TEXT, market_type TEXT, strike_type TEXT,
            floor_strike REAL, cap_strike REAL, open_time TEXT, close_time TEXT,
            expiration_time TEXT, status TEXT, result TEXT, last_price REAL,
            volume REAL, open_interest REAL, liquidity REAL, raw JSON,
            fetched_at TEXT
        );
        CREATE TABLE bot_fills (
            id INTEGER PRIMARY KEY, ts TEXT, mode TEXT, strategy TEXT,
            ticker TEXT, side TEXT, contracts REAL, price REAL, fee REAL,
            order_id TEXT
        );
        CREATE TABLE bot_positions (
            ticker TEXT, mode TEXT, contracts REAL, avg_price REAL,
            strategy TEXT, updated_at TEXT
        );
        CREATE TABLE bot_signals (
            id INTEGER PRIMARY KEY, ts TEXT, strategy TEXT, ticker TEXT,
            side TEXT, contracts REAL, fair_value REAL, market_price REAL,
            reason TEXT, acted INTEGER
        );
        CREATE TABLE bot_equity (ts TEXT, mode TEXT, cash REAL, exposure REAL, equity REAL);
        CREATE TABLE bot_status (key TEXT PRIMARY KEY, value TEXT);

        INSERT INTO markets (ticker, series_ticker, title, subtitle, status, last_price, close_time)
        VALUES
          ('KXHIGHNY-26JUL07-T82', 'KXHIGHNY', 'NYC high >82', '83 or above', 'active', 0.35, '2026-07-08T04:59:00Z'),
          ('KXHIGHCHI-26JUL07-T90', 'KXHIGHCHI', 'CHI high >90', '91 or above', 'finalized', 0.99, '2026-07-08T04:59:00Z');

        INSERT INTO bot_fills (ts, mode, strategy, ticker, side, contracts, price, fee, order_id)
        VALUES ('2026-07-06T12:00:00Z', 'paper', 'weather_edge', 'KXHIGHNY-26JUL07-T82', 'yes', 10, 0.30, 0.02, 'ord-1');

        INSERT INTO bot_positions (ticker, mode, contracts, avg_price, strategy, updated_at)
        VALUES ('KXHIGHNY-26JUL07-T82', 'paper', 10, 0.30, 'weather_edge', '2026-07-06T12:00:00Z');

        INSERT INTO bot_signals (ts, strategy, ticker, side, contracts, fair_value, market_price, reason, acted)
        VALUES ('2026-07-06T12:00:00Z', 'weather_edge', 'KXHIGHNY-26JUL07-T82', 'yes', 10, 0.45, 0.30, 'forecast edge', 1);

        INSERT INTO bot_equity (ts, mode, cash, exposure, equity) VALUES
          ('2026-07-06T11:00:00Z', 'paper', 1000.0, 0.0, 1000.0),
          ('2026-07-06T12:00:00Z', 'paper', 997.0, 3.5, 1000.5),
          ('2026-07-06T12:00:00Z', 'live', 500.0, 0.0, 500.0);

        INSERT INTO bot_status (key, value) VALUES
          ('engine_running', 'true'), ('mode', 'paper'), ('kill_switch', 'off'),
          ('last_heartbeat', '2026-07-06T12:00:05Z'), ('daily_pnl', '0.5');
        """
    )
    conn.commit()
    conn.close()


# ---------------------------------------------------------------- empty / missing tables


def test_status_empty_db(client: TestClient) -> None:
    r = client.get("/api/status")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == {}
    assert body["trading_mode"] in ("paper", "demo", "live")
    assert "max_daily_loss" in body["risk_limits"]
    assert "server_time" in body


@pytest.mark.parametrize(
    ("url", "key"),
    [
        ("/api/positions", "positions"),
        ("/api/positions?mode=paper", "positions"),
        ("/api/fills", "fills"),
        ("/api/fills?mode=live&limit=5", "fills"),
        ("/api/signals?limit=10", "signals"),
        ("/api/equity", "equity"),
        ("/api/equity?mode=paper", "equity"),
        ("/api/markets", "markets"),
        ("/api/markets?series=KXHIGHNY&status=active", "markets"),
    ],
)
def test_endpoints_empty_db(client: TestClient, url: str, key: str) -> None:
    r = client.get(url)
    assert r.status_code == 200
    assert r.json() == {key: []}


def test_backtests_empty_dir(client: TestClient, tmp_path: Path, monkeypatch) -> None:
    # isolate from real data/backtests, which fills up as backtests are saved
    from kalshi_bot.ui import server as ui_server

    monkeypatch.setattr(ui_server, "BACKTEST_DIR", tmp_path / "none")
    r = client.get("/api/backtests")
    assert r.status_code == 200
    assert r.json() == {"backtests": []}


# ---------------------------------------------------------------- with data


def test_status_with_data(db_path: Path, client: TestClient) -> None:
    _seed(db_path)
    body = client.get("/api/status").json()
    assert body["status"]["engine_running"] == "true"
    assert body["status"]["kill_switch"] == "off"
    assert body["status"]["daily_pnl"] == "0.5"


def test_positions_join_and_pnl(db_path: Path, client: TestClient) -> None:
    _seed(db_path)
    rows = client.get("/api/positions?mode=paper").json()["positions"]
    assert len(rows) == 1
    p = rows[0]
    assert p["ticker"] == "KXHIGHNY-26JUL07-T82"
    assert p["last_price"] == 0.35
    assert p["unrealized_pnl"] == pytest.approx(10 * (0.35 - 0.30))
    assert client.get("/api/positions?mode=live").json()["positions"] == []


def test_fills_filter_and_limit(db_path: Path, client: TestClient) -> None:
    _seed(db_path)
    fills = client.get("/api/fills?mode=paper&limit=10").json()["fills"]
    assert len(fills) == 1
    assert fills[0]["order_id"] == "ord-1"
    assert client.get("/api/fills?mode=live").json()["fills"] == []


def test_signals_shape(db_path: Path, client: TestClient) -> None:
    _seed(db_path)
    sig = client.get("/api/signals").json()["signals"][0]
    assert sig["fair_value"] == 0.45
    assert sig["market_price"] == 0.30
    assert sig["acted"] == 1


def test_equity_series_filtered_and_ordered(db_path: Path, client: TestClient) -> None:
    _seed(db_path)
    eq = client.get("/api/equity?mode=paper").json()["equity"]
    assert [p["equity"] for p in eq] == [1000.0, 1000.5]
    assert eq[0]["ts"] <= eq[1]["ts"]
    assert len(client.get("/api/equity").json()["equity"]) == 3


def test_markets_filters(db_path: Path, client: TestClient) -> None:
    _seed(db_path)
    rows = client.get("/api/markets?status=active").json()["markets"]
    assert [m["ticker"] for m in rows] == ["KXHIGHNY-26JUL07-T82"]
    assert rows[0]["last_price"] == 0.35
    rows = client.get("/api/markets?series=KXHIGHCHI").json()["markets"]
    assert [m["series_ticker"] for m in rows] == ["KXHIGHCHI"]


# ---------------------------------------------------------------- kill switch


def test_kill_switch_round_trip(db_path: Path, client: TestClient) -> None:
    # works even before the execution engine has created bot_status
    r = client.post("/api/kill_switch", json={"on": True})
    assert r.status_code == 200
    assert r.json() == {"kill_switch": "on", "halt_reason": "manual via UI"}

    status = client.get("/api/status").json()["status"]
    assert status["kill_switch"] == "on"
    assert status["halt_reason"] == "manual via UI"

    r = client.post("/api/kill_switch", json={"on": False})
    assert r.json()["kill_switch"] == "off"
    status = client.get("/api/status").json()["status"]
    assert status["kill_switch"] == "off"
    assert status["halt_reason"] == ""


def test_kill_switch_preserves_other_status_keys(db_path: Path, client: TestClient) -> None:
    _seed(db_path)
    client.post("/api/kill_switch", json={"on": True})
    status = client.get("/api/status").json()["status"]
    assert status["engine_running"] == "true"  # untouched
    assert status["kill_switch"] == "on"


def test_kill_switch_rejects_bad_body(client: TestClient) -> None:
    assert client.post("/api/kill_switch", json={}).status_code == 422


# ---------------------------------------------------------------- static SPA


def test_index_served(client: TestClient) -> None:
    r = client.get("/")
    assert r.status_code == 200
    assert "KALSHI" in r.text
    assert client.get("/app.js").status_code == 200
    assert client.get("/style.css").status_code == 200
