"""L2 orderbook recorder — Kalshi WebSocket -> SQLite append-only log.

No public historical orderbook-depth archive exists for Kalshi; this recorder
builds ours. Subscribes to orderbook_delta (snapshot + deltas), ticker, and
trade channels for the most liquid open markets and appends raw messages.

The WS session must be authenticated (RSA-PSS on the upgrade request) even
for market-data channels.

Usage:
    python -m kalshi_bot.data.orderbook_recorder --top 40
    python -m kalshi_bot.data.orderbook_recorder --tickers KXBTC-... --duration 3600
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import time
import zlib

import websockets

from kalshi_bot.api.client import KalshiClient
from kalshi_bot.config import get_settings
from kalshi_bot.data.store import connect

log = logging.getLogger("kalshi_bot.recorder")

WS_URL = "wss://api.elections.kalshi.com/trade-api/ws/v2"
WS_PATH = "/trade-api/ws/v2"

SCHEMA = """
CREATE TABLE IF NOT EXISTS orderbook_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    recv_ts_ms INTEGER,
    channel TEXT,
    ticker TEXT,
    seq INTEGER,
    payload JSON
);
CREATE INDEX IF NOT EXISTS idx_oblog_ticker ON orderbook_log(ticker, recv_ts_ms);
"""


async def pick_top_markets(n: int) -> list[str]:
    """Most liquid open markets across our strategy series.

    Exchange-wide GET /markets is useless here: the first 1000+ open markets
    are all auto-generated KXMVE parlay combos, so a global page + filter
    returns nothing. Query the series we actually trade instead.
    """
    from kalshi_bot.data.collector import SPORTS_CULTURE_SERIES, TARGET_SERIES

    series = list(dict.fromkeys(TARGET_SERIES + SPORTS_CULTURE_SERIES))
    client = KalshiClient()
    markets: list[dict] = []
    try:
        for st in series:
            resp = await client.get_markets(
                series_ticker=st, status="open", limit=1000
            )
            markets.extend(resp.get("markets", []))
    finally:
        await client.aclose()
    markets.sort(
        key=lambda m: float(m.get("volume_24h_fp") or m.get("volume_fp") or 0),
        reverse=True,
    )
    return [m["ticker"] for m in markets[:n]]


def _auth_headers() -> dict[str, str]:
    """RSA-PSS headers for the WS upgrade. Signed path is /trade-api/ws/v2,
    which differs from the REST prefix, so this signs directly rather than
    through KalshiClient._sign."""
    import base64

    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.asymmetric import padding

    s = get_settings()
    client = KalshiClient(s.kalshi_api_key_id, s.kalshi_private_key_path)
    ts = str(int(time.time() * 1000))
    msg = f"{ts}GET{WS_PATH}".encode()
    sig = client._private_key.sign(
        msg,
        padding.PSS(mgf=padding.MGF1(hashes.SHA256()),
                    salt_length=padding.PSS.DIGEST_LENGTH),
        hashes.SHA256(),
    )
    return {
        "KALSHI-ACCESS-KEY": s.kalshi_api_key_id,
        "KALSHI-ACCESS-SIGNATURE": base64.b64encode(sig).decode(),
        "KALSHI-ACCESS-TIMESTAMP": ts,
    }


async def record(tickers: list[str], duration: float | None = None) -> None:
    conn = connect()
    conn.executescript(SCHEMA)
    stop_at = time.time() + duration if duration else None
    backoff = 1.0
    total = 0
    while stop_at is None or time.time() < stop_at:
        try:
            async with websockets.connect(
                WS_URL, additional_headers=_auth_headers(), ping_interval=10
            ) as ws:
                backoff = 1.0
                # One subscribe per channel: combined multi-channel commands
                # error ("Params required") and 'trade' then falls back to an
                # exchange-wide subscription. ticker channel omitted entirely.
                for i, channel in enumerate(["orderbook_delta", "trade"], start=1):
                    await ws.send(json.dumps({
                        "id": i,
                        "cmd": "subscribe",
                        "params": {
                            "channels": [channel],
                            "market_tickers": tickers,
                        },
                    }))
                log.info("subscribes sent for %d tickers", len(tickers))
                batch = []
                last_commit = time.time()
                async for raw in ws:
                    now_ms = int(time.time() * 1000)
                    msg = json.loads(raw)
                    mtype = msg.get("type", "")
                    if mtype in ("subscribed", "error", "ok"):
                        log.info("control msg: %s", raw[:300])
                        continue
                    m = msg.get("msg", {})
                    if mtype in ("orderbook_snapshot", "orderbook_delta", "trade"):
                        payload = raw if isinstance(raw, str) else raw.decode()
                        batch.append((
                            now_ms, mtype, m.get("market_ticker") or m.get("ticker"),
                            msg.get("seq"),
                            zlib.compress(payload.encode(), level=6),
                        ))
                    if batch and (len(batch) >= 200 or time.time() - last_commit > 5):
                        conn.executemany(
                            "INSERT INTO orderbook_log (recv_ts_ms, channel, ticker,"
                            " seq, payload) VALUES (?,?,?,?,?)", batch)
                        conn.commit()
                        total += len(batch)
                        batch = []
                        last_commit = time.time()
                        if total and total % 5000 < 200:
                            log.info("recorded %d messages", total)
                    if stop_at and time.time() >= stop_at:
                        break
        except (websockets.exceptions.WebSocketException, OSError) as e:
            log.warning("ws error: %s — reconnecting in %.0fs", e, backoff)
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 60)
    conn.close()
    log.info("recorder stopped, %d messages total", total)


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
    ap = argparse.ArgumentParser()
    ap.add_argument("--tickers", nargs="*")
    ap.add_argument("--top", type=int, default=40,
                    help="if no tickers given, record the N most liquid markets")
    ap.add_argument("--duration", type=float, default=None, help="seconds")
    args = ap.parse_args()
    tickers = args.tickers or asyncio.run(pick_top_markets(args.top))
    print(f"recording {len(tickers)} markets: {', '.join(tickers[:8])}...")
    asyncio.run(record(tickers, args.duration))


if __name__ == "__main__":
    main()
