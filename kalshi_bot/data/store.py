"""SQLite storage for market history and external data.

All prices are stored as floats in dollars (0.0–1.0 per contract).
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from kalshi_bot.config import DB_PATH

SCHEMA = """
CREATE TABLE IF NOT EXISTS series (
    ticker TEXT PRIMARY KEY,
    category TEXT,
    title TEXT,
    fee_type TEXT,
    fee_multiplier REAL,
    raw JSON
);

CREATE TABLE IF NOT EXISTS markets (
    ticker TEXT PRIMARY KEY,
    event_ticker TEXT,
    series_ticker TEXT,
    title TEXT,
    subtitle TEXT,
    market_type TEXT,
    strike_type TEXT,
    floor_strike REAL,
    cap_strike REAL,
    open_time TEXT,
    close_time TEXT,
    expiration_time TEXT,
    status TEXT,
    result TEXT,            -- '', 'yes', 'no' once settled
    last_price REAL,
    volume REAL,
    open_interest REAL,
    liquidity REAL,
    raw JSON,
    fetched_at TEXT DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_markets_series ON markets(series_ticker, status);
CREATE INDEX IF NOT EXISTS idx_markets_event ON markets(event_ticker);

CREATE TABLE IF NOT EXISTS candles (
    market_ticker TEXT,
    period_minutes INTEGER,
    end_ts INTEGER,
    open REAL, high REAL, low REAL, close REAL, mean REAL,
    yes_bid_close REAL,
    yes_ask_close REAL,
    volume REAL,
    open_interest REAL,
    PRIMARY KEY (market_ticker, period_minutes, end_ts)
);

CREATE TABLE IF NOT EXISTS trades (
    trade_id TEXT PRIMARY KEY,
    market_ticker TEXT,
    created_time TEXT,
    yes_price REAL,
    count REAL,
    taker_side TEXT
);
CREATE INDEX IF NOT EXISTS idx_trades_market ON trades(market_ticker, created_time);

-- External signals: one row per (source, location/series key, target date, issued time)
CREATE TABLE IF NOT EXISTS external_signals (
    source TEXT,            -- 'nws_forecast', 'open_meteo', 'nws_obs', 'fred', ...
    key TEXT,               -- e.g. 'KNYC.high_temp', 'CPIAUCSL'
    target_date TEXT,       -- date the value applies to
    issued_at TEXT,         -- when the forecast/reading was published
    value REAL,
    raw JSON,
    PRIMARY KEY (source, key, target_date, issued_at)
);

CREATE TABLE IF NOT EXISTS collector_state (
    key TEXT PRIMARY KEY,
    value TEXT
);
"""


def connect(db_path: Path | None = None) -> sqlite3.Connection:
    path = db_path or DB_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.executescript(SCHEMA)
    return conn


def dollars(v) -> float | None:
    """Parse Kalshi's *_dollars string fields ('0.8700') or legacy cent ints."""
    if v is None:
        return None
    if isinstance(v, str):
        return float(v)
    # legacy integer cents
    return float(v) / 100.0
