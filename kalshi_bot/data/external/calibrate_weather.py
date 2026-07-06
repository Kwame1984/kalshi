"""Calibrate forecast bias and error sigma per station/lead.

Pulls actual CLI-reported highs (IEM) for the backfilled window, joins to
archived forecast leads, and writes per-station calibration to
data/weather_calibration.json:
    {series: {"bias": {lead: mean_err}, "sigma": {lead: std_err}, "n": count}}
where err = actual_high - forecast_high (positive = forecast runs cold).

Also stores actuals in external_signals (source='iem_cli').
"""

from __future__ import annotations

import asyncio
import datetime as dt
import json
import statistics

import httpx

from kalshi_bot.config import PROJECT_ROOT
from kalshi_bot.data.external.settlement import IEM_STATIONS
from kalshi_bot.data.store import connect

OUT_PATH = PROJECT_ROOT / "data" / "weather_calibration.json"


async def fetch_cli_year(station: str, year: int) -> dict[str, float]:
    async with httpx.AsyncClient(timeout=60) as client:
        r = await client.get(
            "https://mesonet.agron.iastate.edu/json/cli.py",
            params={"station": station, "year": year, "fmt": "json"},
        )
        r.raise_for_status()
        data = r.json()
    out = {}
    for row in data.get("results", []):
        high = row.get("high")
        if high is None or high == "M":  # IEM uses 'M' for missing
            continue
        out[row["valid"]] = float(high)
    return out


async def main() -> None:
    conn = connect()
    calib: dict[str, dict] = {}
    year = dt.date.today().year
    for series, station in IEM_STATIONS.items():
        actuals = await fetch_cli_year(station, year)
        if dt.date.today().month <= 5:
            actuals.update(await fetch_cli_year(station, year - 1))
        conn.executemany(
            """INSERT OR REPLACE INTO external_signals
               (source, key, target_date, issued_at, value, raw)
               VALUES ('iem_cli', ?, ?, 'final', ?, NULL)""",
            [(f"{series}.actual_high", day, v) for day, v in actuals.items()],
        )
        conn.commit()

        errs: dict[int, list[float]] = {i: [] for i in range(5)}
        rows = conn.execute(
            "SELECT target_date, raw FROM external_signals "
            "WHERE source='open_meteo' AND key=?",
            (f"{series}.high_f",),
        ).fetchall()
        for r in rows:
            actual = actuals.get(r["target_date"])
            if actual is None or not r["raw"]:
                continue
            leads = json.loads(r["raw"])
            for i in range(5):
                f = leads.get(f"high_f_lead_{i}")
                if f is not None:
                    errs[i].append(actual - f)
        calib[series] = {
            "bias": {i: round(statistics.mean(v), 2) for i, v in errs.items() if len(v) > 10},
            "sigma": {i: round(statistics.stdev(v), 2) for i, v in errs.items() if len(v) > 10},
            "n": len(errs[0]),
        }
        b0 = calib[series]["bias"].get(0)
        s0 = calib[series]["sigma"].get(0)
        print(f"{series} ({station}): n={calib[series]['n']} "
              f"lead0 bias={b0} sigma={s0} "
              f"lead2 bias={calib[series]['bias'].get(2)} sigma={calib[series]['sigma'].get(2)}")
    OUT_PATH.write_text(json.dumps(calib, indent=2))
    print(f"wrote {OUT_PATH}")
    conn.close()


if __name__ == "__main__":
    asyncio.run(main())
