"""Backfill ensemble forecast data (Open-Meteo) for weather series.

DATA AVAILABILITY (probed 2026-07-06/07, free API):
- ensemble-api.open-meteo.com serves raw members (gfs025: 30+ctrl,
  ecmwf_ifs025: 50+ctrl) but archives them only ~3.5 days back, and its
  temperature_2m_previous_dayN member variables return all nulls. There is
  NO free archive of raw ensemble members for past dates.
- previous-runs-api.open-meteo.com DOES archive the NCEP HGEFS 0.25deg
  ENSEMBLE MEAN and ENSEMBLE SPREAD (model 'ncep_hgefs025_ensemble_mean',
  variables temperature_2m[_previous_dayN] and
  temperature_2m_spread[_previous_dayN]) back to ~2026-02-01, with gaps:
  lead-1 mean ~111 of 156 days, lead-1 spread only ~54 days
  (2026-03-30..04-06, 05-07..06-05, 06-21..). ecmwf_ifs025_ensemble_mean is
  accepted by the API but returns all nulls.

So this module stores two things per (series, target_date):
- source='open_meteo_ens', key='<SERIES>.high_f_ens': archived HGEFS
  ensemble-mean daily high and spread per lead 0-2,
  raw = {"lead_N": {"mean_high": F, "spread": F}} (fields may be null).
- source='open_meteo_ens', key='<SERIES>.high_f_members': raw member daily
  highs from the live ensemble API for the dates it covers (~today-2 ..
  today+3), raw = {"lead_N": [member_high, ...]} merged across runs so a
  target date accumulates one member set per lead. Only usable live / for
  the last few days -- kept for the live path and member-vs-spread checks.

Usage: python -m kalshi_bot.data.external.ensemble_backfill --days 160
"""

from __future__ import annotations

import argparse
import asyncio
import datetime as dt
import json
from collections import defaultdict
from typing import Any

import httpx

from kalshi_bot.data.external.weather import STATIONS
from kalshi_bot.data.store import connect

PREV_RUNS_URL = "https://previous-runs-api.open-meteo.com/v1/forecast"
ENSEMBLE_URL = "https://ensemble-api.open-meteo.com/v1/ensemble"
HGEFS_MODEL = "ncep_hgefs025_ensemble_mean"
MEMBER_MODELS = ["gfs025", "ecmwf_ifs025"]
MAX_LEAD = 2
MIN_HOURS = 18          # hours required to trust a daily max
AFTERNOON = range(10, 19)  # local hours used for spread when mean is absent


def _var(lead: int, spread: bool = False) -> str:
    base = "temperature_2m_spread" if spread else "temperature_2m"
    return base if lead == 0 else f"{base}_previous_day{lead}"


async def fetch_hgefs_mean_spread(
    client: httpx.AsyncClient, lat: float, lon: float, start: dt.date, end: dt.date
) -> dict[str, dict[str, dict[str, float | None]]]:
    """{date_iso: {"lead_N": {"mean_high": x|None, "spread": s|None}}}.

    mean_high = daily max of the ensemble-mean hourly temps; spread = the
    ensemble spread at the hour of that max (or afternoon max spread when the
    mean series is missing for that day/lead).
    """
    hourly = [_var(le, sp) for le in range(MAX_LEAD + 1) for sp in (False, True)]
    r = await client.get(PREV_RUNS_URL, params={
        "latitude": lat, "longitude": lon,
        "models": HGEFS_MODEL,
        "hourly": ",".join(hourly),
        "temperature_unit": "fahrenheit", "timezone": "auto",
        "start_date": start.isoformat(), "end_date": end.isoformat(),
    })
    r.raise_for_status()
    d = r.json()
    h = d.get("hourly", {})
    times = h.get("time", [])

    out: dict[str, dict[str, dict[str, float | None]]] = defaultdict(dict)
    for lead in range(MAX_LEAD + 1):
        temps = h.get(_var(lead)) or []
        sprds = h.get(_var(lead, spread=True)) or []
        by_day: dict[str, list[tuple[int, float | None, float | None]]] = defaultdict(list)
        for i, t in enumerate(times):
            hour = int(t[11:13])
            tv = temps[i] if i < len(temps) else None
            sv = sprds[i] if i < len(sprds) else None
            by_day[t[:10]].append((hour, tv, sv))
        for day, rows in by_day.items():
            tvals = [(tv, hour, sv) for hour, tv, sv in rows if tv is not None]
            mean_high = spread = None
            if len(tvals) >= MIN_HOURS:
                mean_high, max_hour, spread_at_max = max(tvals)
                spread = spread_at_max
            if spread is None:
                pm = [sv for hour, tv, sv in rows if sv is not None and hour in AFTERNOON]
                if len(pm) >= 4:
                    spread = max(pm)
            if mean_high is not None or spread is not None:
                out[day][f"lead_{lead}"] = {"mean_high": mean_high, "spread": spread}
    return out


async def fetch_live_members(
    client: httpx.AsyncClient, lat: float, lon: float
) -> dict[str, list[float]]:
    """{date_iso: [member daily highs]} from the live ensemble API.

    Covers roughly today-2 .. today+3; past days are stitched short-lead
    data, future days are the current run at lead (date - today).
    """
    r = await client.get(ENSEMBLE_URL, params={
        "latitude": lat, "longitude": lon,
        "models": ",".join(MEMBER_MODELS),
        "hourly": "temperature_2m",
        "temperature_unit": "fahrenheit", "timezone": "auto",
        "past_days": 2, "forecast_days": 4,
    })
    r.raise_for_status()
    d = r.json()
    h = d.get("hourly", {})
    times = h.get("time", [])
    member_keys = [k for k in h if k.startswith("temperature_2m") and k != "time"]

    out: dict[str, list[float]] = defaultdict(list)
    for k in member_keys:
        vals = h.get(k) or []
        by_day: dict[str, list[float]] = defaultdict(list)
        for t, v in zip(times, vals):
            if v is not None:
                by_day[t[:10]].append(v)
        for day, temps in by_day.items():
            if len(temps) >= MIN_HOURS:
                out[day].append(round(max(temps), 2))
    return out


def _merge_members(conn, series: str, day: str, lead: int, members: list[float]) -> None:
    key = f"{series}.high_f_members"
    row = conn.execute(
        """SELECT raw FROM external_signals
           WHERE source='open_meteo_ens' AND key=? AND target_date=? AND issued_at='archive'""",
        (key, day),
    ).fetchone()
    raw: dict[str, Any] = json.loads(row["raw"]) if row and row["raw"] else {}
    raw[f"lead_{lead}"] = members
    conn.execute(
        """INSERT OR REPLACE INTO external_signals
           (source, key, target_date, issued_at, value, raw)
           VALUES ('open_meteo_ens', ?, ?, 'archive', ?, ?)""",
        (key, day, sorted(members)[len(members) // 2], json.dumps(raw)),
    )


async def backfill(days: int, members: bool = True) -> None:
    conn = connect()
    today = dt.date.today()
    start = today - dt.timedelta(days=days)
    async with httpx.AsyncClient(timeout=120) as client:
        for series, (station, lat, lon) in STATIONS.items():
            # 1) archived HGEFS ensemble mean + spread (one batched request)
            try:
                hist = await fetch_hgefs_mean_spread(client, lat, lon, start, today)
            except (httpx.HTTPError, KeyError, ValueError) as e:
                print(f"{series}: hgefs fetch failed: {e}")
                hist = {}
            rows = []
            for day, leads in sorted(hist.items()):
                l1 = leads.get("lead_1") or {}
                rows.append((
                    "open_meteo_ens", f"{series}.high_f_ens", day, "archive",
                    l1.get("mean_high"), json.dumps({"model": HGEFS_MODEL, **leads}),
                ))
            if rows:
                conn.executemany(
                    """INSERT OR REPLACE INTO external_signals
                       (source, key, target_date, issued_at, value, raw)
                       VALUES (?,?,?,?,?,?)""",
                    rows,
                )
                conn.commit()
            n_l1 = sum(
                1 for _, leads in hist.items()
                if (leads.get("lead_1") or {}).get("spread") is not None
            )
            print(f"{series} ({station}): {len(rows)} days ens mean/spread "
                  f"({n_l1} with lead-1 spread)")
            await asyncio.sleep(1.0)

            # 2) live raw members for the recent window
            if members:
                try:
                    live = await fetch_live_members(client, lat, lon)
                except (httpx.HTTPError, KeyError, ValueError) as e:
                    print(f"{series}: member fetch failed: {e}")
                    live = {}
                n = 0
                for day, mems in sorted(live.items()):
                    lead = max(0, (dt.date.fromisoformat(day) - today).days)
                    if lead > MAX_LEAD + 2 or len(mems) < 10:
                        continue
                    _merge_members(conn, series, day, lead, mems)
                    n += 1
                conn.commit()
                print(f"{series} ({station}): members stored for {n} dates")
                await asyncio.sleep(1.0)
    conn.close()


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=160)
    ap.add_argument("--no-members", action="store_true",
                    help="skip the live raw-member fetch")
    args = ap.parse_args()
    asyncio.run(backfill(args.days, members=not args.no_members))


if __name__ == "__main__":
    main()
