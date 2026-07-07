"""Kalshi Trade API v2 client.

Market-data endpoints are public; portfolio/order endpoints require RSA-PSS
request signing (KALSHI-ACCESS-* headers). Prices are parsed from the
``*_dollars`` string fields the API now returns alongside legacy cent ints.
"""

from __future__ import annotations

import asyncio
import base64
import time
from pathlib import Path
from typing import Any

import httpx
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa

PROD_BASE = "https://api.elections.kalshi.com/trade-api/v2"
PROD_BASE_NEW = "https://external-api.kalshi.com/trade-api/v2"  # canonical since May 2026
DEMO_BASE = "https://external-api.demo.kalshi.co/trade-api/v2"


class KalshiAPIError(Exception):
    def __init__(self, status: int, body: str):
        self.status = status
        self.body = body
        super().__init__(f"Kalshi API {status}: {body[:300]}")


class KalshiClient:
    """Async client. Instantiate without credentials for public market data."""

    def __init__(
        self,
        api_key_id: str | None = None,
        private_key_path: Path | str | None = None,
        base_url: str = PROD_BASE,
        requests_per_second: float = 8.0,
    ):
        self.base_url = base_url.rstrip("/")
        self.api_key_id = api_key_id
        self._private_key: rsa.RSAPrivateKey | None = None
        if private_key_path:
            key_file = Path(private_key_path).expanduser()
            if key_file.exists():
                self._private_key = serialization.load_pem_private_key(
                    key_file.read_bytes(), password=None
                )
        self._client = httpx.AsyncClient(timeout=30.0)
        self._min_interval = 1.0 / requests_per_second
        self._last_request = 0.0
        self._throttle_lock = asyncio.Lock()

    async def aclose(self) -> None:
        await self._client.aclose()

    # -- auth ---------------------------------------------------------------

    def _sign(self, method: str, path: str) -> dict[str, str]:
        if not (self.api_key_id and self._private_key):
            return {}
        ts = str(int(time.time() * 1000))
        # Signature covers the path WITHOUT the query string.
        msg = f"{ts}{method}{'/trade-api/v2' + path}".encode()
        sig = self._private_key.sign(
            msg,
            padding.PSS(
                mgf=padding.MGF1(hashes.SHA256()),
                salt_length=padding.PSS.DIGEST_LENGTH,
            ),
            hashes.SHA256(),
        )
        return {
            "KALSHI-ACCESS-KEY": self.api_key_id,
            "KALSHI-ACCESS-SIGNATURE": base64.b64encode(sig).decode(),
            "KALSHI-ACCESS-TIMESTAMP": ts,
        }

    # -- transport ----------------------------------------------------------

    async def _request(
        self,
        method: str,
        path: str,
        params: dict[str, Any] | None = None,
        json: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        async with self._throttle_lock:
            wait = self._min_interval - (time.monotonic() - self._last_request)
            if wait > 0:
                await asyncio.sleep(wait)
            self._last_request = time.monotonic()

        url = self.base_url + path
        for attempt in range(4):
            resp = await self._client.request(
                method, url, params=params, json=json, headers=self._sign(method, path)
            )
            if resp.status_code == 429 or resp.status_code >= 500:
                await asyncio.sleep(0.5 * 2**attempt)
                continue
            if resp.status_code >= 400:
                raise KalshiAPIError(resp.status_code, resp.text)
            return resp.json()
        raise KalshiAPIError(resp.status_code, resp.text)

    # -- public market data ---------------------------------------------------

    async def get_exchange_status(self) -> dict:
        return await self._request("GET", "/exchange/status")

    async def get_series_list(self, category: str | None = None) -> list[dict]:
        params = {"category": category} if category else None
        return (await self._request("GET", "/series", params=params))["series"]

    async def get_series(self, series_ticker: str) -> dict:
        return (await self._request("GET", f"/series/{series_ticker}"))["series"]

    async def get_events(
        self,
        series_ticker: str | None = None,
        status: str | None = None,
        with_nested_markets: bool = False,
        limit: int = 100,
        cursor: str | None = None,
    ) -> dict:
        params: dict[str, Any] = {"limit": limit}
        if series_ticker:
            params["series_ticker"] = series_ticker
        if status:
            params["status"] = status
        if with_nested_markets:
            params["with_nested_markets"] = "true"
        if cursor:
            params["cursor"] = cursor
        return await self._request("GET", "/events", params=params)

    async def get_markets(
        self,
        series_ticker: str | None = None,
        event_ticker: str | None = None,
        status: str | None = None,
        tickers: list[str] | None = None,
        limit: int = 100,
        cursor: str | None = None,
    ) -> dict:
        params: dict[str, Any] = {"limit": limit}
        if series_ticker:
            params["series_ticker"] = series_ticker
        if event_ticker:
            params["event_ticker"] = event_ticker
        if status:
            params["status"] = status
        if tickers:
            params["tickers"] = ",".join(tickers)
        if cursor:
            params["cursor"] = cursor
        return await self._request("GET", "/markets", params=params)

    async def get_market(self, ticker: str) -> dict:
        return (await self._request("GET", f"/markets/{ticker}"))["market"]

    async def get_orderbook(self, ticker: str, depth: int = 20) -> dict:
        return await self._request(
            "GET", f"/markets/{ticker}/orderbook", params={"depth": depth}
        )

    async def get_trades(
        self,
        ticker: str | None = None,
        limit: int = 100,
        cursor: str | None = None,
        min_ts: int | None = None,
        max_ts: int | None = None,
    ) -> dict:
        params: dict[str, Any] = {"limit": limit}
        if ticker:
            params["ticker"] = ticker
        if cursor:
            params["cursor"] = cursor
        if min_ts:
            params["min_ts"] = min_ts
        if max_ts:
            params["max_ts"] = max_ts
        return await self._request("GET", "/markets/trades", params=params)

    async def get_candlesticks(
        self,
        series_ticker: str,
        market_ticker: str,
        start_ts: int,
        end_ts: int,
        period_interval: int = 60,
    ) -> list[dict]:
        """period_interval in minutes: 1, 60, or 1440."""
        resp = await self._request(
            "GET",
            f"/series/{series_ticker}/markets/{market_ticker}/candlesticks",
            params={
                "start_ts": start_ts,
                "end_ts": end_ts,
                "period_interval": period_interval,
            },
        )
        return resp["candlesticks"]

    # -- historical tier (public; markets settled before the live rolling window) --

    async def get_historical_cutoff(self) -> dict:
        """Thresholds separating the historical tier from the live API."""
        return await self._request("GET", "/historical/cutoff")

    async def get_historical_markets(
        self,
        series_ticker: str | None = None,
        event_ticker: str | None = None,
        limit: int = 1000,
        cursor: str | None = None,
    ) -> dict:
        params: dict[str, Any] = {"limit": limit}
        if series_ticker:
            params["series_ticker"] = series_ticker
        if event_ticker:
            params["event_ticker"] = event_ticker
        if cursor:
            params["cursor"] = cursor
        return await self._request("GET", "/historical/markets", params=params)

    async def get_historical_candlesticks(
        self,
        market_ticker: str,
        start_ts: int,
        end_ts: int,
        period_interval: int = 60,
    ) -> list[dict]:
        """period_interval in minutes: 1, 60, or 1440. Unlike the live endpoint,
        prices come back as plain decimal strings without the ``_dollars`` suffix."""
        resp = await self._request(
            "GET",
            f"/historical/markets/{market_ticker}/candlesticks",
            params={
                "start_ts": start_ts,
                "end_ts": end_ts,
                "period_interval": period_interval,
            },
        )
        return resp["candlesticks"]

    # -- authenticated: portfolio & orders ------------------------------------

    async def get_balance(self) -> dict:
        return await self._request("GET", "/portfolio/balance")

    async def get_positions(self, **params: Any) -> dict:
        return await self._request("GET", "/portfolio/positions", params=params or None)

    async def get_fills(self, **params: Any) -> dict:
        return await self._request("GET", "/portfolio/fills", params=params or None)

    async def get_orders(self, **params: Any) -> dict:
        return await self._request("GET", "/portfolio/orders", params=params or None)

    async def create_order(
        self,
        ticker: str,
        side: str,  # "bid" = buy YES exposure, "ask" = sell YES (single-book V2 model)
        count: float,
        price: float,  # yes price in dollars, e.g. 0.87
        time_in_force: str = "good_till_canceled",
        client_order_id: str | None = None,
        expiration_time: int | None = None,
        post_only: bool = False,
        reduce_only: bool = False,
    ) -> dict:
        """Create Order V2 (legacy /portfolio/orders was deprecated Jun 2026)."""
        body: dict[str, Any] = {
            "ticker": ticker,
            "side": side,
            "count": f"{count:.2f}",
            "price": f"{price:.4f}",
            "time_in_force": time_in_force,
            "self_trade_prevention_type": "maker",
        }
        if client_order_id:
            body["client_order_id"] = client_order_id
        if expiration_time:
            body["expiration_time"] = expiration_time
        if post_only:
            body["post_only"] = True
        if reduce_only:
            body["reduce_only"] = True
        return await self._request("POST", "/portfolio/events/orders", json=body)

    async def cancel_order(self, order_id: str) -> dict:
        return await self._request("DELETE", f"/portfolio/events/orders/{order_id}")
