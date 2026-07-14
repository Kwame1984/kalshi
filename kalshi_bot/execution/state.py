"""Execution-state tables in data/kalshi.db.

This schema is a CONTRACT shared with the UI — do not rename columns.

Conventions:
  * bot_fills.side is the signal side ('yes'|'no'); contracts is positive;
    price is the price paid per contract for THAT side (no-side fills store
    the no price, i.e. 1 - yes_price).
  * bot_positions.contracts is SIGNED yes-contracts (+long YES / -long NO),
    matching the backtester's single-ledger convention; avg_price is the
    average YES price basis of the open position.
  * bot_status keys: engine_running, mode, kill_switch, last_heartbeat,
    daily_pnl, halt_reason.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from kalshi_bot.data.store import connect as _connect
from kalshi_bot.execution.util import now_iso

STATE_SCHEMA = """
CREATE TABLE IF NOT EXISTS bot_fills (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT,
    mode TEXT,
    strategy TEXT,
    ticker TEXT,
    side TEXT,
    contracts REAL,
    price REAL,
    fee REAL,
    order_id TEXT
);

CREATE TABLE IF NOT EXISTS bot_positions (
    ticker TEXT,
    mode TEXT,
    contracts REAL,
    avg_price REAL,
    strategy TEXT,
    updated_at TEXT,
    PRIMARY KEY (ticker, mode)
);

CREATE TABLE IF NOT EXISTS bot_signals (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT,
    strategy TEXT,
    ticker TEXT,
    side TEXT,
    contracts REAL,
    fair_value REAL,
    market_price REAL,
    reason TEXT,
    acted INTEGER
);

CREATE TABLE IF NOT EXISTS bot_equity (
    ts TEXT,
    mode TEXT,
    cash REAL,
    exposure REAL,
    equity REAL
);

CREATE TABLE IF NOT EXISTS bot_status (
    key TEXT PRIMARY KEY,
    value TEXT
);

CREATE TABLE IF NOT EXISTS bot_settlements (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT,
    mode TEXT,
    strategy TEXT,
    ticker TEXT,
    contracts REAL,
    result TEXT,
    payout REAL,
    cost_basis REAL,
    realized_pnl REAL
);
"""


def connect(db_path: Path | None = None) -> sqlite3.Connection:
    """Open the shared kalshi.db and ensure the execution tables exist."""
    conn = _connect(db_path)
    conn.executescript(STATE_SCHEMA)
    return conn


def init_state(conn: sqlite3.Connection) -> None:
    conn.executescript(STATE_SCHEMA)
    conn.commit()


# -- bot_status -------------------------------------------------------------

def set_status(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute(
        "INSERT INTO bot_status (key, value) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (key, str(value)),
    )
    conn.commit()


def get_status(conn: sqlite3.Connection, key: str, default: str | None = None) -> str | None:
    row = conn.execute("SELECT value FROM bot_status WHERE key=?", (key,)).fetchone()
    return row["value"] if row else default


# -- writes -----------------------------------------------------------------

def record_fill(
    conn: sqlite3.Connection,
    mode: str,
    strategy: str,
    ticker: str,
    side: str,
    contracts: float,
    price: float,
    fee: float,
    order_id: str = "",
    ts: str | None = None,
) -> int:
    cur = conn.execute(
        "INSERT INTO bot_fills (ts, mode, strategy, ticker, side, contracts, price, fee, order_id)"
        " VALUES (?,?,?,?,?,?,?,?,?)",
        (ts or now_iso(), mode, strategy, ticker, side, contracts, price, fee, order_id),
    )
    conn.commit()
    return int(cur.lastrowid)


def record_signal(
    conn: sqlite3.Connection,
    strategy: str,
    ticker: str,
    side: str,
    contracts: float,
    fair_value: float,
    market_price: float | None,
    reason: str,
    acted: bool,
    ts: str | None = None,
) -> int:
    cur = conn.execute(
        "INSERT INTO bot_signals (ts, strategy, ticker, side, contracts, fair_value,"
        " market_price, reason, acted) VALUES (?,?,?,?,?,?,?,?,?)",
        (ts or now_iso(), strategy, ticker, side, contracts, fair_value,
         market_price, reason, int(acted)),
    )
    conn.commit()
    return int(cur.lastrowid)


def record_equity(
    conn: sqlite3.Connection, mode: str, cash: float, exposure: float, equity: float,
) -> None:
    conn.execute(
        "INSERT INTO bot_equity (ts, mode, cash, exposure, equity) VALUES (?,?,?,?,?)",
        (now_iso(), mode, cash, exposure, equity),
    )
    conn.commit()


def record_settlement(
    conn: sqlite3.Connection,
    mode: str,
    strategy: str,
    ticker: str,
    contracts: float,
    result: str,
    payout: float,
    cost_basis: float,
    realized_pnl: float,
    ts: str | None = None,
) -> int:
    cur = conn.execute(
        "INSERT INTO bot_settlements (ts, mode, strategy, ticker, contracts,"
        " result, payout, cost_basis, realized_pnl) VALUES (?,?,?,?,?,?,?,?,?)",
        (ts or now_iso(), mode, strategy, ticker, contracts, result,
         payout, cost_basis, realized_pnl),
    )
    conn.commit()
    return int(cur.lastrowid)


def apply_fill_to_position(
    conn: sqlite3.Connection,
    mode: str,
    ticker: str,
    signed_yes_contracts: float,
    yes_price: float,
    strategy: str,
) -> None:
    """Merge a fill (signed yes-contracts at a yes price) into bot_positions."""
    row = conn.execute(
        "SELECT contracts, avg_price FROM bot_positions WHERE ticker=? AND mode=?",
        (ticker, mode),
    ).fetchone()
    old_qty = row["contracts"] if row else 0.0
    old_avg = row["avg_price"] if row else 0.0
    new_qty = old_qty + signed_yes_contracts
    if abs(new_qty) < 1e-9:
        conn.execute(
            "DELETE FROM bot_positions WHERE ticker=? AND mode=?", (ticker, mode)
        )
        conn.commit()
        return
    same_direction = old_qty == 0 or (old_qty > 0) == (signed_yes_contracts > 0)
    if same_direction:
        avg = (abs(old_qty) * old_avg + abs(signed_yes_contracts) * yes_price) / abs(new_qty)
    elif (new_qty > 0) == (old_qty > 0):
        avg = old_avg  # partial close keeps original basis
    else:
        avg = yes_price  # flipped through zero: new basis
    conn.execute(
        "INSERT INTO bot_positions (ticker, mode, contracts, avg_price, strategy, updated_at)"
        " VALUES (?,?,?,?,?,?)"
        " ON CONFLICT(ticker, mode) DO UPDATE SET contracts=excluded.contracts,"
        " avg_price=excluded.avg_price, strategy=excluded.strategy,"
        " updated_at=excluded.updated_at",
        (ticker, mode, new_qty, avg, strategy, now_iso()),
    )
    conn.commit()


# -- reads ------------------------------------------------------------------

def get_positions(conn: sqlite3.Connection, mode: str) -> dict[str, dict]:
    """ticker -> {contracts (signed yes), avg_price, strategy}."""
    return {
        r["ticker"]: dict(r)
        for r in conn.execute("SELECT * FROM bot_positions WHERE mode=?", (mode,))
    }


def total_exposure(conn: sqlite3.Connection, mode: str) -> float:
    """Dollars of cost basis committed across all open positions.

    Long YES costs avg_price per contract; long NO (negative yes-contracts)
    costs (1 - avg_price) per contract.
    """
    exp = 0.0
    for r in conn.execute(
        "SELECT contracts, avg_price FROM bot_positions WHERE mode=?", (mode,)
    ):
        qty, avg = r["contracts"], r["avg_price"]
        exp += qty * avg if qty >= 0 else -qty * (1.0 - avg)
    return exp


def market_exposure(conn: sqlite3.Connection, mode: str, ticker: str) -> float:
    row = conn.execute(
        "SELECT contracts, avg_price FROM bot_positions WHERE mode=? AND ticker=?",
        (mode, ticker),
    ).fetchone()
    if not row:
        return 0.0
    qty, avg = row["contracts"], row["avg_price"]
    return qty * avg if qty >= 0 else -qty * (1.0 - avg)


def latest_equity(conn: sqlite3.Connection, mode: str) -> dict | None:
    row = conn.execute(
        "SELECT * FROM bot_equity WHERE mode=? ORDER BY ts DESC, rowid DESC LIMIT 1",
        (mode,),
    ).fetchone()
    return dict(row) if row else None


def equity_at_day_start(conn: sqlite3.Connection, mode: str, day_iso: str) -> float | None:
    """First equity snapshot recorded on `day_iso` (UTC date), if any."""
    row = conn.execute(
        "SELECT equity FROM bot_equity WHERE mode=? AND ts >= ? ORDER BY ts, rowid LIMIT 1",
        (mode, day_iso),
    ).fetchone()
    return row["equity"] if row else None
