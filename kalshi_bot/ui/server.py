"""FastAPI server for the Kalshi bot dashboard.

Run:  python -m kalshi_bot.ui.server --port 8420

Reads the shared SQLite store (markets/candles plus the execution engine's
bot_* state tables). Every bot_* table may not exist yet — the execution
engine creates them — so all reads degrade to empty results.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import sqlite3
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from kalshi_bot.config import DB_PATH, PROJECT_ROOT, get_settings

STATIC_DIR = Path(__file__).resolve().parent / "static"
BACKTEST_DIR = PROJECT_ROOT / "data" / "backtests"

BOT_STATUS_SCHEMA = "CREATE TABLE IF NOT EXISTS bot_status (key TEXT PRIMARY KEY, value TEXT)"


def _connect(db_path: Path) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    return conn


def _query(db_path: Path, sql: str, params: tuple = ()) -> list[dict]:
    """Run a read query; a missing table yields [] instead of an error."""
    conn = _connect(db_path)
    try:
        return [dict(r) for r in conn.execute(sql, params)]
    except sqlite3.OperationalError:
        return []
    finally:
        conn.close()


class KillSwitchBody(BaseModel):
    on: bool


def create_app(db_path: Path | None = None) -> FastAPI:
    db = db_path or DB_PATH
    app = FastAPI(title="Kalshi Bot Dashboard")

    @app.get("/api/status")
    def api_status() -> dict:
        rows = _query(db, "SELECT key, value FROM bot_status")
        status = {r["key"]: r["value"] for r in rows}
        settings = get_settings()
        return {
            "status": status,
            "trading_mode": settings.trading_mode.value,
            "risk_limits": {
                "max_position_per_market": settings.max_position_per_market,
                "max_daily_loss": settings.max_daily_loss,
                "max_total_exposure": settings.max_total_exposure,
            },
            "server_time": dt.datetime.now(dt.UTC).isoformat(),
        }

    @app.get("/api/positions")
    def api_positions(mode: str | None = None) -> dict:
        where, params = "", ()
        if mode:
            where, params = "WHERE p.mode = ?", (mode,)
        rows = _query(
            db,
            f"""SELECT p.ticker, p.mode, p.contracts, p.avg_price, p.strategy,
                       p.updated_at, m.last_price, m.title, m.close_time
                FROM bot_positions p LEFT JOIN markets m ON m.ticker = p.ticker
                {where} ORDER BY p.updated_at DESC""",
            params,
        )
        for r in rows:
            last, avg, n = r.get("last_price"), r.get("avg_price"), r.get("contracts") or 0
            r["unrealized_pnl"] = (
                round(n * (last - avg), 4) if last is not None and avg is not None else None
            )
        return {"positions": rows}

    @app.get("/api/fills")
    def api_fills(mode: str | None = None, limit: int = 50) -> dict:
        where, params = "", ()
        if mode:
            where, params = "WHERE mode = ?", (mode,)
        rows = _query(
            db,
            f"""SELECT id, ts, mode, strategy, ticker, side, contracts, price, fee, order_id
                FROM bot_fills {where} ORDER BY ts DESC LIMIT ?""",
            (*params, max(1, min(limit, 500))),
        )
        return {"fills": rows}

    @app.get("/api/signals")
    def api_signals(limit: int = 50) -> dict:
        rows = _query(
            db,
            """SELECT id, ts, strategy, ticker, side, contracts, fair_value,
                      market_price, reason, acted
               FROM bot_signals ORDER BY ts DESC LIMIT ?""",
            (max(1, min(limit, 500)),),
        )
        return {"signals": rows}

    @app.get("/api/equity")
    def api_equity(mode: str | None = None) -> dict:
        where, params = "", ()
        if mode:
            where, params = "WHERE mode = ?", (mode,)
        rows = _query(
            db,
            f"""SELECT ts, mode, cash, exposure, equity
                FROM bot_equity {where} ORDER BY ts ASC""",
            params,
        )
        return {"equity": rows}

    @app.get("/api/markets")
    def api_markets(series: str | None = None, status: str | None = None) -> dict:
        clauses, params = [], []
        if series:
            clauses.append("series_ticker = ?")
            params.append(series)
        if status:
            clauses.append("status = ?")
            params.append(status)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        rows = _query(
            db,
            f"""SELECT ticker, series_ticker, title, subtitle, status, result,
                       last_price, volume, open_interest, close_time
                FROM markets {where}
                ORDER BY series_ticker, close_time, ticker LIMIT 1000""",
            tuple(params),
        )
        return {"markets": rows}

    @app.get("/api/backtests")
    def api_backtests() -> dict:
        results = []
        if BACKTEST_DIR.is_dir():
            for f in sorted(BACKTEST_DIR.glob("*.json")):
                try:
                    payload = json.loads(f.read_text())
                except (OSError, json.JSONDecodeError):
                    continue
                results.append({"name": f.stem, "result": payload})
        return {"backtests": results}

    @app.post("/api/kill_switch")
    def api_kill_switch(body: KillSwitchBody) -> dict:
        value = "on" if body.on else "off"
        halt_reason = "manual via UI" if body.on else ""
        conn = _connect(db)
        try:
            conn.execute(BOT_STATUS_SCHEMA)
            conn.execute(
                "INSERT INTO bot_status (key, value) VALUES ('kill_switch', ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (value,),
            )
            conn.execute(
                "INSERT INTO bot_status (key, value) VALUES ('halt_reason', ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (halt_reason,),
            )
            conn.commit()
        except sqlite3.OperationalError as exc:
            raise HTTPException(status_code=500, detail=f"failed to write kill switch: {exc}")
        finally:
            conn.close()
        return {"kill_switch": value, "halt_reason": halt_reason}

    app.mount("/", StaticFiles(directory=STATIC_DIR, html=True), name="static")
    return app


app = create_app()


def main() -> None:
    import uvicorn

    ap = argparse.ArgumentParser(description="Kalshi bot dashboard server")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8420)
    args = ap.parse_args()
    uvicorn.run("kalshi_bot.ui.server:app", host=args.host, port=args.port)


if __name__ == "__main__":
    main()
