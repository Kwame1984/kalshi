"""Weather forecast data for Kalshi temperature markets.

Two sources:
- Open-Meteo previous-runs API: archived model forecasts — lets the backtester
  ask "what did the forecast say N hours before settlement?"
- NWS (api.weather.gov): current operational forecast, closest to what
  settlement (NWS CLI report) will read.

Kalshi daily high markets settle on the NWS climate report for a specific
station; coordinates below are those stations.
"""

from __future__ import annotations

import datetime as dt
from typing import Any

import httpx

# series_ticker -> (station, lat, lon) for the settlement station
STATIONS = {
    "KXHIGHNY":   ("KNYC", 40.783, -73.967),   # Central Park
    "KXHIGHCHI":  ("KMDW", 41.786, -87.752),   # Midway
    "KXHIGHMIA":  ("KMIA", 25.788, -80.317),
    "KXHIGHAUS":  ("KAUS", 30.183, -97.680),
    "KXHIGHDEN":  ("KDEN", 39.847, -104.656),
    "KXHIGHLAX":  ("KLAX", 33.938, -118.389),
    "KXHIGHPHIL": ("KPHL", 39.868, -75.231),
}


async def open_meteo_forecast_high(
    lat: float, lon: float, target_date: dt.date
) -> dict[str, Any]:
    """Current forecast daily max temp (°F) for target_date, all models blend."""
    async with httpx.AsyncClient(timeout=30) as client:
        r = await client.get(
            "https://api.open-meteo.com/v1/forecast",
            params={
                "latitude": lat, "longitude": lon,
                "daily": "temperature_2m_max",
                "temperature_unit": "fahrenheit",
                "timezone": "auto",
                "start_date": target_date.isoformat(),
                "end_date": target_date.isoformat(),
            },
        )
        r.raise_for_status()
        d = r.json()
    return {
        "high_f": d["daily"]["temperature_2m_max"][0],
        "raw": d,
    }


async def open_meteo_previous_runs(
    lat: float, lon: float, target_date: dt.date, days_back: int = 4
) -> dict[str, Any]:
    """Forecast for target_date as issued 0..days_back days earlier.

    Returns {'high_f_lead_0': ..., 'high_f_lead_1': ...} where lead N is the
    forecast made N days before. The previous-runs API only serves hourly
    variables, so the daily high is the max over the target date's hours.
    """
    hourly_vars = ["temperature_2m"] + [
        f"temperature_2m_previous_day{i}" for i in range(1, days_back + 1)
    ]
    async with httpx.AsyncClient(timeout=30) as client:
        r = await client.get(
            "https://previous-runs-api.open-meteo.com/v1/forecast",
            params={
                "latitude": lat, "longitude": lon,
                "hourly": ",".join(hourly_vars),
                "temperature_unit": "fahrenheit",
                "timezone": "auto",
                "start_date": target_date.isoformat(),
                "end_date": target_date.isoformat(),
            },
        )
        r.raise_for_status()
        d = r.json()
    out: dict[str, Any] = {"raw": d}
    hourly = d.get("hourly", {})
    for i in range(0, days_back + 1):
        var = "temperature_2m" if i == 0 else f"temperature_2m_previous_day{i}"
        vals = [v for v in (hourly.get(var) or []) if v is not None]
        out[f"high_f_lead_{i}"] = max(vals) if vals else None
    return out


async def nws_forecast_high(
    lat: float, lon: float, target_date: dt.date
) -> float | None:
    """NWS operational forecast max temp (°F) for target_date."""
    headers = {"User-Agent": "kalshi-bot (research)"}
    async with httpx.AsyncClient(timeout=30, headers=headers) as client:
        r = await client.get(f"https://api.weather.gov/points/{lat},{lon}")
        r.raise_for_status()
        forecast_url = r.json()["properties"]["forecast"]
        r = await client.get(forecast_url)
        r.raise_for_status()
        periods = r.json()["properties"]["periods"]
    for p in periods:
        if p["isDaytime"] and p["startTime"][:10] == target_date.isoformat():
            return float(p["temperature"])
    return None
