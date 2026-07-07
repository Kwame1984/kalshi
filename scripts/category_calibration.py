"""Category-level calibration report across ALL settled markets in the DB.

For every settled market, take ONE sample per horizon (~1h, ~6h, ~24h before
close): the nearest hourly candle within +/-1.5h whose bid/ask spread is
<= 0.10. Bucket samples by (category, horizon, price decile) and report n,
avg_price, win_rate, edge (win_rate - avg_price), the taker fee at that
price, and the 95% Wilson interval on win_rate. A bucket is significant when
the Wilson interval excludes the average price (i.e. the CI on edge excludes
zero); it is fee-clearing when |edge| > taker_fee(avg_price). Buckets with
n >= 100 AND a significant, fee-clearing edge are flagged TRADEABLE.

Category comes from the `series` table (populated from GET /series/{ticker});
series without a row fall back to a static map, then 'Unknown'.

Usage:
    python scripts/category_calibration.py [--category Sports ...] [--min-n 1]
"""

from __future__ import annotations

import argparse
import datetime as dt
import math
import sqlite3
import time
from collections import defaultdict

from kalshi_bot.backtest.fees import taker_fee
from kalshi_bot.data.store import connect

HORIZONS_H = (1, 6, 24)
TOLERANCE_S = int(1.5 * 3600)   # candle must sit within +/-1.5h of target
MAX_SPREAD = 0.10               # require a real two-sided quote
MIN_N_TRADEABLE = 100

# Fallback categories for series collected before the `series` table existed.
FALLBACK_CATEGORY = {
    "KXHIGHNY": "Climate and Weather", "KXHIGHCHI": "Climate and Weather",
    "KXHIGHMIA": "Climate and Weather", "KXHIGHAUS": "Climate and Weather",
    "KXHIGHDEN": "Climate and Weather", "KXHIGHLAX": "Climate and Weather",
    "KXHIGHPHIL": "Climate and Weather",
    "KXFED": "Economics", "KXCPIYOY": "Economics", "KXCPI": "Economics",
    "KXPAYROLLS": "Economics", "KXU3": "Economics", "KXGDP": "Economics",
    "KXJOBLESS": "Economics",
    "KXMLBGAME": "Sports", "KXWNBAGAME": "Sports", "KXATPMATCH": "Sports",
    "KXUFCFIGHT": "Sports",
    "KXNETFLIXRANKMOVIE": "Entertainment", "KXALBUMEQUIV": "Entertainment",
    "KXAPRPOTUS": "Politics", "KXTRUTHSOCIAL": "Politics",
}


def _iso_to_ts(iso: str | None) -> int | None:
    if not iso:
        return None
    return int(dt.datetime.fromisoformat(iso.replace("Z", "+00:00")).timestamp())


def _retrying(conn: sqlite3.Connection, sql: str, params=()) -> list[sqlite3.Row]:
    """Run a query, retrying patiently on 'database is locked'."""
    for attempt in range(20):
        try:
            return conn.execute(sql, params).fetchall()
        except sqlite3.OperationalError as e:
            if "locked" not in str(e):
                raise
            time.sleep(3)
    raise RuntimeError("database stayed locked")


def wilson(wins: float, n: int, z: float = 1.96) -> tuple[float, float]:
    """95% Wilson score interval for a binomial proportion."""
    if n == 0:
        return 0.0, 1.0
    p = wins / n
    denom = 1 + z * z / n
    centre = p + z * z / (2 * n)
    spread = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n))
    return (centre - spread) / denom, (centre + spread) / denom


def load_categories(conn: sqlite3.Connection) -> dict[str, str]:
    cats = dict(FALLBACK_CATEGORY)
    for r in _retrying(conn, "SELECT ticker, category FROM series"):
        if r["category"]:
            cats[r["ticker"]] = r["category"]
    return cats


def collect_samples(conn: sqlite3.Connection, categories: dict[str, str]):
    """One (category, horizon, mid, won, event) sample per settled market/horizon."""
    markets = _retrying(
        conn,
        """SELECT ticker, event_ticker, series_ticker, close_time, result
           FROM markets WHERE result IN ('yes','no') AND close_time IS NOT NULL""",
    )
    samples = []
    for m in markets:
        close_ts = _iso_to_ts(m["close_time"])
        if not close_ts:
            continue
        cat = categories.get(m["series_ticker"], "Unknown")
        won = 1.0 if m["result"] == "yes" else 0.0
        for h in HORIZONS_H:
            target = close_ts - h * 3600
            rows = _retrying(
                conn,
                """SELECT end_ts, yes_bid_close, yes_ask_close FROM candles
                   WHERE market_ticker = ? AND period_minutes = 60
                     AND end_ts BETWEEN ? AND ?
                   ORDER BY ABS(end_ts - ?) LIMIT 8""",
                (m["ticker"], target - TOLERANCE_S, target + TOLERANCE_S, target),
            )
            # nearest candle with a valid two-sided quote and sane spread
            for c in rows:
                bid, ask = c["yes_bid_close"], c["yes_ask_close"]
                if (bid is None or ask is None or not (0 <= bid < ask <= 1)
                        or ask - bid > MAX_SPREAD):
                    continue
                samples.append({
                    "cat": cat, "h": h, "mid": (bid + ask) / 2, "won": won,
                    "series": m["series_ticker"], "event": m["event_ticker"],
                })
                break
    return samples


def bucketize(samples):
    """(category, horizon, decile) -> aggregate row."""
    groups: dict[tuple, list] = defaultdict(list)
    for s in samples:
        decile = min(int(s["mid"] * 10), 9)
        groups[(s["cat"], s["h"], decile)].append(s)
    out = []
    for (cat, h, dec), rows in sorted(groups.items()):
        n = len(rows)
        wins = sum(r["won"] for r in rows)
        avg_px = sum(r["mid"] for r in rows) / n
        win_rate = wins / n
        edge = win_rate - avg_px
        fee = taker_fee(avg_px, 1)
        lo, hi = wilson(wins, n)
        sig = lo > avg_px or hi < avg_px           # CI on edge excludes zero
        fee_clear = abs(edge) > fee
        n_events = len({r["event"] for r in rows})
        tradeable = n >= MIN_N_TRADEABLE and sig and fee_clear
        out.append({
            "cat": cat, "h": h, "dec": dec, "n": n, "n_events": n_events,
            "avg_px": avg_px, "win_rate": win_rate, "edge": edge, "fee": fee,
            "wilson_lo": lo, "wilson_hi": hi, "sig": sig,
            "fee_clear": fee_clear, "tradeable": tradeable,
        })
    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--category", nargs="*", default=None,
                    help="restrict output to these categories")
    ap.add_argument("--min-n", type=int, default=1,
                    help="hide buckets with fewer samples")
    args = ap.parse_args()

    conn = connect()
    conn.execute("PRAGMA busy_timeout=30000")
    categories = load_categories(conn)
    samples = collect_samples(conn, categories)
    conn.close()

    rows = bucketize(samples)
    if args.category:
        allowed = set(args.category)
        rows = [r for r in rows if r["cat"] in allowed]
    rows = [r for r in rows if r["n"] >= args.min_n]

    print(f"samples: {len(samples)}  "
          f"(one per settled market per horizon; spread <= {MAX_SPREAD:.2f}, "
          f"nearest candle within +/-{TOLERANCE_S // 60}min)")
    hdr = (f"{'category':<20} {'hz':>3} {'bucket':>9} {'n':>6} {'nev':>5} "
           f"{'avg_px':>7} {'win%':>7} {'edge':>7} {'fee':>6} "
           f"{'w_lo':>6} {'w_hi':>6} {'flags'}")
    print(hdr)
    print("-" * len(hdr))
    for r in rows:
        flags = []
        if r["sig"]:
            flags.append("sig")
        if r["fee_clear"]:
            flags.append("fee+")
        if r["tradeable"]:
            flags.append("TRADEABLE")
        print(f"{r['cat']:<20} {r['h']:>2}h "
              f"{r['dec'] / 10:.1f}-{(r['dec'] + 1) / 10:.1f} "
              f"{r['n']:>6} {r['n_events']:>5} {r['avg_px']:>7.3f} "
              f"{r['win_rate'] * 100:>6.1f}% {r['edge']:>+7.3f} {r['fee']:>6.3f} "
              f"{r['wilson_lo']:>6.3f} {r['wilson_hi']:>6.3f} {' '.join(flags)}")

    trade = [r for r in rows if r["tradeable"]]
    print(f"\nTRADEABLE buckets (n >= {MIN_N_TRADEABLE}, Wilson 95% CI excludes "
          f"zero edge, |edge| > taker fee): {len(trade)}")
    for r in trade:
        side = "buy YES" if r["edge"] > 0 else "buy NO"
        print(f"  {r['cat']} {r['h']}h {r['dec'] / 10:.1f}-{(r['dec'] + 1) / 10:.1f}: "
              f"n={r['n']} (events={r['n_events']}) avg_px={r['avg_px']:.3f} "
              f"win={r['win_rate']:.3f} edge={r['edge']:+.3f} fee={r['fee']:.3f} -> {side}")
    if not trade:
        print("  none")
    print("\nNote: n counts markets, nev counts distinct events; brackets of the "
          "same event settle together, so when n >> nev the effective sample is "
          "closer to nev and significance is overstated.")


if __name__ == "__main__":
    main()
