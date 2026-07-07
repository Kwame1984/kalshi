"""Insider-signature volume-surge study.

Hypothesis: in quiet event markets where private information can exist
(Politics / Entertainment series), a sudden aggressive one-sided volume surge
on the cheap side (3-35c) accompanied by a price jump predicts resolution YES
far above the entry-price-implied probability. Control: Economics / Weather
(no insiders on CPI prints or thermometers) should show NO such lift.

Subcommands
    screen    candle-level screen over every settled market with candles.
              Flags: hourly volume z>=Z vs trailing 168h (zero-filled grid),
              close move >= +3c on the flag candle, pre-surge price in
              [0.03, 0.35], hours_to_close > 6. Entry proxy = next candle's
              yes_ask_close (within 3h). Writes data/insider_surge/flags.json
              and prints resolution-vs-implied tables by category group.
    tape      pull the REST trade tape for flagged markets in the chosen
              categories, compute taker-side imbalance inside the surge hour,
              and compare one-sided (>=80% yes-taker) vs mixed flow.
              Caches to data/insider_surge/tape_trades.json.
    backtest  run InsiderSurge through the standard Backtester on the signal
              (or control) series, feeding hourly candles with volume
              converted to cumulative (matching the live engine's semantics).

Usage
    .venv/bin/python scripts/insider_surge_study.py screen
    .venv/bin/python scripts/insider_surge_study.py tape --categories Politics Entertainment --max-markets 150
    .venv/bin/python scripts/insider_surge_study.py backtest --group signal
"""

from __future__ import annotations

import argparse
import asyncio
import datetime as dt
import json
import math
import sqlite3
import sys
from collections import deque
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from kalshi_bot.backtest.fees import taker_fee  # noqa: E402

DB_PATH = PROJECT_ROOT / "data" / "kalshi.db"
OUT_DIR = PROJECT_ROOT / "data" / "insider_surge"

SIGNAL_CATS = ("Politics", "Entertainment")
CONTROL_CATS = ("Economics", "Climate and Weather")
SPORTS_CATS = ("Sports",)

# --- screen parameters -------------------------------------------------------
Z_MIN = 4.0
STD_FLOOR = 1.0          # contracts; avoids z blowups on dead books
MIN_HISTORY_H = 24       # trailing hours required before a flag can fire
WINDOW_H = 168           # trailing baseline window
MIN_MOVE = 0.03          # close_t - close_{t-1}
BAND_LO, BAND_HI = 0.03, 0.35   # pre-surge price band
MIN_HTC = 6.0            # hours to close at flag time
ENTRY_MAX_GAP_H = 3      # next candle with an ask must be within this many hours


def connect_ro() -> sqlite3.Connection:
    conn = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)
    conn.execute("PRAGMA busy_timeout=30000")
    conn.row_factory = sqlite3.Row
    return conn


def wilson(k: int, n: int, z: float = 1.96) -> tuple[float, float, float]:
    if n == 0:
        return (float("nan"), 0.0, 1.0)
    p = k / n
    d = 1 + z * z / n
    c = (p + z * z / (2 * n)) / d
    h = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / d
    return p, max(0.0, c - h), min(1.0, c + h)


def parse_close_ts(close_time: str | None) -> int | None:
    if not close_time:
        return None
    try:
        return int(
            dt.datetime.fromisoformat(str(close_time).replace("Z", "+00:00")).timestamp()
        )
    except ValueError:
        return None


# ------------------------------------------------------------------------------
# screen
# ------------------------------------------------------------------------------

def screen_market(rows: list[sqlite3.Row], close_ts: int | None) -> list[dict]:
    """Return all flag events for one market's hourly candles (sorted by end_ts)."""
    by_ts = {int(r["end_ts"]): r for r in rows}
    ts_sorted = sorted(by_ts)
    first, last = ts_sorted[0], ts_sorted[-1]

    window: deque[float] = deque(maxlen=WINDOW_H)
    wsum = 0.0
    wsq = 0.0
    prev_close: float | None = None
    flags: list[dict] = []

    t = first
    while t <= last:
        c = by_ts.get(t)
        v = float(c["volume"] or 0.0) if c is not None else 0.0
        close = None
        if c is not None and c["close"] is not None:
            close = float(c["close"])

        if (
            c is not None
            and len(window) >= MIN_HISTORY_H
            and prev_close is not None
            and close is not None
        ):
            n = len(window)
            mean = wsum / n
            var = max(0.0, wsq / n - mean * mean)
            std = max(math.sqrt(var), STD_FLOOR)
            z = (v - mean) / std
            move = close - prev_close
            htc = ((close_ts - t) / 3600.0) if close_ts else None
            if (
                z >= Z_MIN
                and move >= MIN_MOVE
                and BAND_LO <= prev_close <= BAND_HI
                and htc is not None
                and htc > MIN_HTC
            ):
                # entry proxy: next stored candle's ask within ENTRY_MAX_GAP_H
                entry = None
                entry_ts = None
                for k in range(1, ENTRY_MAX_GAP_H + 1):
                    nxt = by_ts.get(t + k * 3600)
                    if nxt is not None and nxt["yes_ask_close"] is not None:
                        a = float(nxt["yes_ask_close"])
                        if 0.0 < a < 1.0:
                            entry = a
                            entry_ts = t + k * 3600
                            break
                flags.append({
                    "flag_ts": t,
                    "z": round(z, 2),
                    "surge_vol": round(v, 1),
                    "trail_mean": round(mean, 2),
                    "prev_close": prev_close,
                    "flag_close": close,
                    "move": round(move, 4),
                    "htc": round(htc, 1),
                    "entry_ask": entry,
                    "entry_ts": entry_ts,
                })

        # roll the window forward (zero for missing hours)
        if len(window) == window.maxlen:
            old = window[0]
            wsum -= old
            wsq -= old * old
        window.append(v)
        wsum += v
        wsq += v * v
        if close is not None:
            prev_close = close
        t += 3600
    return flags


def cmd_screen(args: argparse.Namespace) -> None:
    conn = connect_ro()
    mrows = conn.execute(
        """SELECT m.ticker, m.series_ticker, m.event_ticker, m.result,
                  m.close_time, m.volume AS mkt_volume, s.category
           FROM markets m JOIN series s ON s.ticker = m.series_ticker
           WHERE m.result IN ('yes','no')"""
    ).fetchall()
    meta = {r["ticker"]: r for r in mrows}
    print(f"settled markets with series category: {len(meta)}", flush=True)

    flags: list[dict] = []
    cur = conn.execute(
        """SELECT market_ticker, end_ts, close, yes_ask_close, volume
           FROM candles WHERE period_minutes = 60
           ORDER BY market_ticker, end_ts"""
    )
    batch: list[sqlite3.Row] = []
    current = None
    done = 0

    def flush() -> None:
        nonlocal done
        if not batch or current not in meta:
            return
        m = meta[current]
        for f in screen_market(batch, parse_close_ts(m["close_time"])):
            f.update({
                "ticker": current,
                "series": m["series_ticker"],
                "event": m["event_ticker"],
                "category": m["category"],
                "result": m["result"],
                "mkt_volume": m["mkt_volume"],
            })
            flags.append(f)
        done += 1
        if done % 2000 == 0:
            print(f"  screened {done} markets, {len(flags)} flags so far", flush=True)

    for r in cur:
        if r["market_ticker"] != current:
            flush()
            batch = []
            current = r["market_ticker"]
        batch.append(r)
    flush()
    conn.close()

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    (OUT_DIR / "flags.json").write_text(json.dumps(flags))
    print(f"total flag events: {len(flags)} -> {OUT_DIR / 'flags.json'}\n")
    report_flags(flags, min_vol=args.min_vol)


def first_flag_per_market(flags: list[dict]) -> list[dict]:
    seen: dict[str, dict] = {}
    for f in sorted(flags, key=lambda x: (x["ticker"], x["flag_ts"])):
        seen.setdefault(f["ticker"], f)
    return list(seen.values())


def group_of(category: str) -> str:
    if category in SIGNAL_CATS:
        return "SIGNAL (Politics/Entertainment)"
    if category in CONTROL_CATS:
        return "CONTROL (Econ/Weather)"
    return "SPORTS"


def _stat_line(label: str, rows: list[dict]) -> str:
    rows = [r for r in rows if r["entry_ask"] is not None]
    n = len(rows)
    if n == 0:
        return f"  {label:<44} n=0"
    k = sum(1 for r in rows if r["result"] == "yes")
    p, lo, hi = wilson(k, n)
    entry = sum(r["entry_ask"] for r in rows) / n
    fee = sum(taker_fee(r["entry_ask"], 1) for r in rows) / n
    ev = p - entry - fee
    return (f"  {label:<44} n={n:<5} win={p:.3f} [{lo:.3f},{hi:.3f}]  "
            f"entry={entry:.3f}  lift={p - entry:+.3f}  EV/ct(net)={ev:+.3f}")


def report_flags(flags: list[dict], min_vol: float = 0.0) -> None:
    for mv in sorted({0.0, 100.0, 300.0, min_vol}):
        sub = [f for f in flags if f["surge_vol"] >= mv]
        firsts = first_flag_per_market(sub)
        print(f"=== min surge volume {mv:.0f} contracts "
              f"(flags={len(sub)}, markets={len(firsts)}) — FIRST flag per market ===")
        by_group: dict[str, list[dict]] = {}
        by_cat: dict[str, list[dict]] = {}
        by_series: dict[str, list[dict]] = {}
        for f in firsts:
            by_group.setdefault(group_of(f["category"]), []).append(f)
            by_cat.setdefault(f["category"], []).append(f)
            if f["category"] in SIGNAL_CATS:
                by_series.setdefault(f["series"], []).append(f)
        for g in ("SIGNAL (Politics/Entertainment)", "CONTROL (Econ/Weather)", "SPORTS"):
            print(_stat_line(g, by_group.get(g, [])))
        print("  --- by category ---")
        for c in sorted(by_cat):
            print(_stat_line(c, by_cat[c]))
        if by_series:
            print("  --- signal group by series ---")
            for s in sorted(by_series):
                print(_stat_line(s, by_series[s]))
        print()


def cmd_report(args: argparse.Namespace) -> None:
    flags = json.loads((OUT_DIR / "flags.json").read_text())
    report_flags(flags, min_vol=args.min_vol)


# ------------------------------------------------------------------------------
# tape
# ------------------------------------------------------------------------------

async def pull_tape(flags: list[dict]) -> dict[str, dict]:
    from kalshi_bot.api.client import PROD_BASE, KalshiClient

    cache_path = OUT_DIR / "tape_trades.json"
    cache: dict[str, dict] = {}
    if cache_path.exists():
        cache = json.loads(cache_path.read_text())

    client = KalshiClient(base_url=PROD_BASE)
    try:
        for i, f in enumerate(flags):
            key = f"{f['ticker']}@{f['flag_ts']}"
            if key in cache:
                continue
            t0, t1 = f["flag_ts"] - 3600, f["flag_ts"]
            trades: list[dict] = []
            cursor = None
            ok = True
            try:
                while True:
                    resp = await client.get_trades(
                        ticker=f["ticker"], min_ts=t0, max_ts=t1,
                        limit=1000, cursor=cursor,
                    )
                    trades.extend(resp.get("trades") or [])
                    cursor = resp.get("cursor")
                    if not cursor or not resp.get("trades"):
                        break
            except Exception as e:  # market too old for the live tape, etc.
                print(f"  [{i}] {f['ticker']}: tape fetch failed ({e})", flush=True)
                ok = False
            yes_v = no_v = 0.0
            for t in trades:
                cnt = float(t.get("count_fp") or t.get("count") or 0)
                if t.get("taker_side") == "yes":
                    yes_v += cnt
                elif t.get("taker_side") == "no":
                    no_v += cnt
            cache[key] = {
                "ok": ok, "n_trades": len(trades),
                "yes_vol": yes_v, "no_vol": no_v,
            }
            if (i + 1) % 10 == 0:
                print(f"  tape progress: {i + 1}/{len(flags)}", flush=True)
                cache_path.write_text(json.dumps(cache))
    finally:
        await client.aclose()
        OUT_DIR.mkdir(parents=True, exist_ok=True)
        cache_path.write_text(json.dumps(cache))
    return cache


def cmd_tape(args: argparse.Namespace) -> None:
    flags = json.loads((OUT_DIR / "flags.json").read_text())
    sub = [f for f in flags
           if f["category"] in args.categories
           and f["surge_vol"] >= args.min_vol
           and f["entry_ask"] is not None]
    firsts = first_flag_per_market(sub)
    firsts.sort(key=lambda f: -f["surge_vol"])
    firsts = firsts[: args.max_markets]
    print(f"pulling trade tape for {len(firsts)} flagged markets "
          f"(categories={args.categories}, min_vol={args.min_vol})", flush=True)
    cache = asyncio.run(pull_tape(firsts))

    buckets: dict[str, list[dict]] = {"one_sided_yes": [], "mixed": [], "one_sided_no": []}
    missing = 0
    for f in firsts:
        rec = cache.get(f"{f['ticker']}@{f['flag_ts']}")
        if not rec or not rec["ok"] or (rec["yes_vol"] + rec["no_vol"]) <= 0:
            missing += 1
            continue
        frac = rec["yes_vol"] / (rec["yes_vol"] + rec["no_vol"])
        f = dict(f, yes_frac=frac)
        if frac >= 0.8:
            buckets["one_sided_yes"].append(f)
        elif frac <= 0.2:
            buckets["one_sided_no"].append(f)
        else:
            buckets["mixed"].append(f)
    print(f"\n=== taker-flow split in surge hour (missing/no-tape: {missing}) ===")
    for b, rows in buckets.items():
        print(_stat_line(b, rows))
    (OUT_DIR / "tape_result.json").write_text(json.dumps(buckets))


# ------------------------------------------------------------------------------
# backtest
# ------------------------------------------------------------------------------

def cmd_backtest(args: argparse.Namespace) -> None:
    from kalshi_bot.backtest.engine import Backtester
    from kalshi_bot.backtest.runner import load_data
    from kalshi_bot.strategies.insider_surge import InsiderSurge

    conn = connect_ro()
    cats = SIGNAL_CATS if args.group == "signal" else CONTROL_CATS
    series = [r["ticker"] for r in conn.execute(
        "SELECT ticker FROM series WHERE category IN (%s)" % ",".join("?" * len(cats)),
        list(cats),
    )]
    conn.close()

    markets, candles = load_data(series)
    # live engine feeds CUMULATIVE market volume; candles store per-hour volume.
    for rows in candles.values():
        cum = 0.0
        for c in rows:
            cum += c.get("volume") or 0.0
            c["volume"] = cum
    strat = InsiderSurge()
    if args.group != "signal":
        # let the strategy run on control series to test the confound
        strat.allow_prefixes = tuple(series)
    bt = Backtester(starting_cash=args.cash, max_position_per_market=50.0)
    res = bt.run(strat, markets, candles)
    print(json.dumps(res.metrics(), indent=2))
    if res.settlements:
        worst = sorted(res.settlements, key=lambda s: s[2])[:5]
        print("worst settlements:", [(t, r, round(p, 2)) for t, r, p in worst])
        best = sorted(res.settlements, key=lambda s: -s[2])[:5]
        print("best settlements:", [(t, r, round(p, 2)) for t, r, p in best])


# ------------------------------------------------------------------------------

def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("screen")
    p.add_argument("--min-vol", type=float, default=100.0)
    p.set_defaults(fn=cmd_screen)

    p = sub.add_parser("report")
    p.add_argument("--min-vol", type=float, default=100.0)
    p.set_defaults(fn=cmd_report)

    p = sub.add_parser("tape")
    p.add_argument("--categories", nargs="+", default=list(SIGNAL_CATS))
    p.add_argument("--min-vol", type=float, default=100.0)
    p.add_argument("--max-markets", type=int, default=150)
    p.set_defaults(fn=cmd_tape)

    p = sub.add_parser("backtest")
    p.add_argument("--group", choices=["signal", "control"], default="signal")
    p.add_argument("--cash", type=float, default=1000.0)
    p.set_defaults(fn=cmd_backtest)

    args = ap.parse_args()
    args.fn(args)


if __name__ == "__main__":
    main()
