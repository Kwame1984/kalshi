"""Settlement truth for weather markets: NWS CLI climate reports via IEM.

Kalshi daily-high markets settle on the NWS CLI report for the station.
Iowa Environmental Mesonet parses these into JSON.
"""

from __future__ import annotations

import datetime as dt

import httpx

# station used in IEM cli.py per Kalshi series
IEM_STATIONS = {
    "KXHIGHNY": "KNYC",
    "KXHIGHCHI": "KMDW",
    "KXHIGHMIA": "KMIA",
    "KXHIGHAUS": "KAUS",
    "KXHIGHDEN": "KDEN",
    "KXHIGHLAX": "KLAX",
    "KXHIGHPHIL": "KPHL",
}


async def cli_high_temp(station: str, date: dt.date) -> float | None:
    """Official CLI-reported high (°F) for a station/date, or None if absent."""
    async with httpx.AsyncClient(timeout=30) as client:
        r = await client.get(
            "https://mesonet.agron.iastate.edu/json/cli.py",
            params={"station": station, "year": date.year, "fmt": "json"},
        )
        r.raise_for_status()
        data = r.json()
    for row in data.get("results", []):
        if row.get("valid") == date.isoformat():
            high = row.get("high")
            return float(high) if high is not None else None
    return None
