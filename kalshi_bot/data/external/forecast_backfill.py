"""Backfill archived forecasts (Open-Meteo previous runs) for weather series.

Stores, per (series, target_date), the forecast daily high at each lead time
0-4 days, into external_signals with source='open_meteo'.

Usage: python -m kalshi_bot.data.external.forecast_backfill --days 120
"""

from __future__ import annotations

import argparse
import asyncio
import datetime as dt
import json
from collections import defaultdict

import httpx

from kalshi_bot.data.external.weather import STATIONS
from kalshi_bot.data.store import connect

MAX_LEAD = 4


async def fetch_station_history(
    lat: float, lon: float, start: dt.date, end: dt.date
) -> dict[str, dict[int, float]]:
    """Returns {date_iso: {lead: high_f}} for the range."""
    hourly_vars = ["temperature_2m"] + [
        f"temperature_2m_previous_day{i}" for i in range(1, MAX_LEAD + 1)
    ]
    async with httpx.AsyncClient(timeout=60) as client:
        r = await client.get(
            "https://previous-runs-api.open-meteo.com/v1/forecast",
            params={
                "latitude": lat, "longitude": lon,
                "hourly": ",".join(hourly_vars),
                "temperature_unit": "fahrenheit",
                "timezone": "auto",
                "start_date": start.isoformat(),
                "end_date": end.isoformat(),
            },
        )
        r.raise_for_status()
        d = r.json()
    hourly = d.get("hourly", {})
    times = hourly.get("time", [])
    out: dict[str, dict[int, float]] = defaultdict(dict)
    for lead in range(0, MAX_LEAD + 1):
        var = "temperature_2m" if lead == 0 else f"temperature_2m_previous_day{lead}"
        vals = hourly.get(var) or []
        by_day: dict[str, list[float]] = defaultdict(list)
        for t, v in zip(times, vals):
            if v is not None:
                by_day[t[:10]].append(v)
        for day, temps in by_day.items():
            out[day][lead] = max(temps)
    return out


async def backfill(days: int) -> None:
    conn = connect()
    end = dt.date.today()
    start = end - dt.timedelta(days=days)
    for series, (station, lat, lon) in STATIONS.items():
        try:
            history = await fetch_station_history(lat, lon, start, end)
        except httpx.HTTPStatusError as e:
            print(f"{series}: fetch failed {e.response.status_code}")
            continue
        rows = []
        for day, leads in history.items():
            payload = {f"high_f_lead_{k}": v for k, v in sorted(leads.items())}
            rows.append((
                "open_meteo", f"{series}.high_f", day, "archive",
                payload.get("high_f_lead_0"), json.dumps(payload),
            ))
        conn.executemany(
            """INSERT OR REPLACE INTO external_signals
               (source, key, target_date, issued_at, value, raw)
               VALUES (?,?,?,?,?,?)""",
            rows,
        )
        conn.commit()
        print(f"{series} ({station}): {len(rows)} days of forecast leads stored")
        await asyncio.sleep(1.0)  # be polite to the free API
    conn.close()


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=120)
    args = ap.parse_args()
    asyncio.run(backfill(args.days))


if __name__ == "__main__":
    main()
