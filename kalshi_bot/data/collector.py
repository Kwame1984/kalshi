"""Historical + ongoing data collection for target series.

Usage:
    python -m kalshi_bot.data.collector backfill --series KXHIGHNY --days 90
    python -m kalshi_bot.data.collector snapshot   # refresh open markets
"""

from __future__ import annotations

import argparse
import asyncio
import datetime as dt
import json
import sqlite3

from kalshi_bot.api.client import KalshiClient
from kalshi_bot.data.store import connect, dollars

# Initial focus: weather dailies + econ/rates. Extend freely.
WEATHER_SERIES = [
    "KXHIGHNY", "KXHIGHCHI", "KXHIGHMIA", "KXHIGHAUS", "KXHIGHDEN",
    "KXHIGHLAX", "KXHIGHPHIL",
]
ECON_SERIES = ["KXFED", "KXCPIYOY", "KXCPI", "KXPAYROLLS", "KXU3"]
TARGET_SERIES = WEATHER_SERIES + ECON_SERIES


def upsert_market(conn: sqlite3.Connection, m: dict, series_ticker: str) -> None:
    conn.execute(
        """INSERT INTO markets (ticker, event_ticker, series_ticker, title, subtitle,
               market_type, strike_type, floor_strike, cap_strike, open_time, close_time,
               expiration_time, status, result, last_price, volume, open_interest,
               liquidity, raw)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
           ON CONFLICT(ticker) DO UPDATE SET
               status=excluded.status, result=excluded.result,
               last_price=excluded.last_price, volume=excluded.volume,
               open_interest=excluded.open_interest, liquidity=excluded.liquidity,
               raw=excluded.raw, fetched_at=datetime('now')""",
        (
            m["ticker"], m.get("event_ticker"), series_ticker, m.get("title"),
            m.get("yes_sub_title") or m.get("subtitle"),
            m.get("market_type"), m.get("strike_type"),
            m.get("floor_strike"), m.get("cap_strike"),
            m.get("open_time"), m.get("close_time"), m.get("expiration_time"),
            m.get("status"), m.get("result"),
            dollars(m.get("last_price_dollars") or m.get("last_price")),
            float(m.get("volume_fp") or m.get("volume") or 0),
            float(m.get("open_interest_fp") or m.get("open_interest") or 0),
            dollars(m.get("liquidity_dollars")) if m.get("liquidity_dollars") else m.get("liquidity"),
            json.dumps(m),
        ),
    )


async def collect_markets(
    client: KalshiClient, conn: sqlite3.Connection, series_ticker: str,
    status: str | None = None,
) -> list[dict]:
    """Page through all markets for a series; upsert and return them."""
    out: list[dict] = []
    cursor = None
    while True:
        resp = await client.get_markets(
            series_ticker=series_ticker, status=status, limit=1000, cursor=cursor
        )
        markets = resp.get("markets", [])
        for m in markets:
            upsert_market(conn, m, series_ticker)
        out.extend(markets)
        cursor = resp.get("cursor")
        if not cursor or not markets:
            break
    conn.commit()
    return out


async def collect_candles(
    client: KalshiClient, conn: sqlite3.Connection,
    series_ticker: str, market: dict, period_minutes: int = 60,
) -> int:
    """Fetch full candle history for one market."""
    open_ts = _iso_to_ts(market.get("open_time"))
    close_ts = _iso_to_ts(market.get("close_time"))
    if not open_ts or not close_ts:
        return 0
    now_ts = int(dt.datetime.now(dt.UTC).timestamp())
    end = min(close_ts, now_ts)
    try:
        candles = await client.get_candlesticks(
            series_ticker, market["ticker"], open_ts, end, period_minutes
        )
    except Exception:
        return 0
    rows = []
    for c in candles:
        p = c.get("price", {})
        rows.append((
            market["ticker"], period_minutes, c["end_period_ts"],
            dollars(p.get("open_dollars")), dollars(p.get("high_dollars")),
            dollars(p.get("low_dollars")), dollars(p.get("close_dollars")),
            dollars(p.get("mean_dollars")),
            dollars((c.get("yes_bid") or {}).get("close_dollars")),
            dollars((c.get("yes_ask") or {}).get("close_dollars")),
            float(c.get("volume_fp") or c.get("volume") or 0),
            float(c.get("open_interest_fp") or c.get("open_interest") or 0),
        ))
    conn.executemany(
        """INSERT OR REPLACE INTO candles
           (market_ticker, period_minutes, end_ts, open, high, low, close, mean,
            yes_bid_close, yes_ask_close, volume, open_interest)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
        rows,
    )
    conn.commit()
    return len(rows)


def _iso_to_ts(iso: str | None) -> int | None:
    if not iso:
        return None
    return int(dt.datetime.fromisoformat(iso.replace("Z", "+00:00")).timestamp())


async def backfill(series_list: list[str], days: int, period_minutes: int = 60) -> None:
    """Backfill settled + open markets and their candle history."""
    conn = connect()
    client = KalshiClient()
    cutoff = dt.datetime.now(dt.UTC) - dt.timedelta(days=days)
    try:
        for st in series_list:
            markets = await collect_markets(client, conn, st)
            recent = [
                m for m in markets
                if (_iso_to_ts(m.get("close_time")) or 0) >= cutoff.timestamp()
            ]
            print(f"{st}: {len(markets)} markets, {len(recent)} within {days}d")
            n = 0
            for m in recent:
                n += await collect_candles(client, conn, st, m, period_minutes)
            print(f"{st}: {n} candles stored")
    finally:
        await client.aclose()
        conn.close()


async def snapshot() -> None:
    """Refresh status/prices of open markets in target series."""
    conn = connect()
    client = KalshiClient()
    try:
        for st in TARGET_SERIES:
            markets = await collect_markets(client, conn, st, status="open")
            print(f"{st}: {len(markets)} open markets refreshed")
    finally:
        await client.aclose()
        conn.close()


def main() -> None:
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    bf = sub.add_parser("backfill")
    bf.add_argument("--series", nargs="*", default=TARGET_SERIES)
    bf.add_argument("--days", type=int, default=90)
    bf.add_argument("--period", type=int, default=60, choices=[1, 60, 1440])
    sub.add_parser("snapshot")
    args = ap.parse_args()
    if args.cmd == "backfill":
        asyncio.run(backfill(args.series, args.days, args.period))
    else:
        asyncio.run(snapshot())


if __name__ == "__main__":
    main()
