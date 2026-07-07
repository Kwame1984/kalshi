"""Calibration study for the in-play "panic fade" hypothesis (Choi-Hui 2014).

Question: when a strong PRE-GAME favorite trades down hard in-play, does the
market overshoot (settlement rate > touched price + costs)?  Mirror: do
pre-game dogs that spike up in-play settle below the touched price
(overreaction) or above it (informed flow)?

Method
------
- Universe: settled KXMLBGAME / KXWNBAGAME / KXATPMATCH / KXUFCFIGHT markets.
- Pre-game anchor: last hourly candle with hours-to-close (htc) in [5, 12]
  and quoted spread <= 0.06; anchor = bid/ask mid.
- In-play window: candles with htc in [0, 4].
- Favorites (anchor >= 0.60): a market "touches" a band if any in-play
  candle LOW falls inside [0.20,0.30) / [0.30,0.40) / [0.40,0.50).
  First touching candle per market per band is used.
- Dogs (anchor <= 0.40): touch if any in-play candle HIGH falls in [0.55,0.70).
- Entry proxies per touching candle:
    best case      = the candle low (high for dogs) itself
    conservative   = min(candle close, low + 0.03)   (max(close, high-0.03) for dogs)
- Breakeven per observation = entry + taker fee at entry + 0.015 half-spread
  proxy.  Edge = settlement rate - mean breakeven.  Wilson 95% CI on the
  settlement rate.
- "Strategy proxy" table replicates what the backtest engine can actually
  fill (candle CLOSE ask): anchor >= 0.65, close <= anchor - 0.25,
  close in [0.22, 0.48].

Granularity caveat: hourly candles give only ~2-4 in-play observations per
game; the candle low/high captures the intra-hour extreme but not its timing,
sequencing, or the quote at that moment.  Lows are trade-print extremes and
may be thin-book artifacts.

Run: .venv/bin/python scripts/panic_fade_study.py
"""

from __future__ import annotations

import datetime as dt
import math
import sqlite3
from collections import defaultdict
from pathlib import Path

DB = Path(__file__).resolve().parents[1] / "data" / "kalshi.db"
SERIES = ("KXMLBGAME", "KXWNBAGAME", "KXATPMATCH", "KXUFCFIGHT")

ANCHOR_HTC = (5.0, 12.0)
ANCHOR_MAX_SPREAD = 0.06
INPLAY_MAX_HTC = 4.0
FAV_ANCHOR = 0.60
DOG_ANCHOR = 0.40
FAV_BANDS = [(0.20, 0.30), (0.30, 0.40), (0.40, 0.50)]
DOG_BAND = (0.55, 0.70)
HALF_SPREAD = 0.015
TAKER_RATE = 0.07


def fee(p: float) -> float:
    """Per-contract taker fee, unrounded (rounding is negligible at size>=10)."""
    return TAKER_RATE * p * (1.0 - p)


def wilson(k: int, n: int, z: float = 1.96) -> tuple[float, float]:
    if n == 0:
        return (0.0, 1.0)
    p = k / n
    denom = 1 + z * z / n
    center = (p + z * z / (2 * n)) / denom
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / denom
    return (max(0.0, center - half), min(1.0, center + half))


def load(conn: sqlite3.Connection):
    """Return list of (series, ticker, result, close_ts, candles)."""
    ph = ",".join("?" * len(SERIES))
    mkts = conn.execute(
        f"""SELECT ticker, series_ticker, result, close_time FROM markets
            WHERE series_ticker IN ({ph}) AND result IN ('yes','no')""",
        SERIES,
    ).fetchall()
    rows = conn.execute(
        f"""SELECT c.market_ticker, c.end_ts, c.low, c.high, c.close,
                   c.yes_bid_close, c.yes_ask_close
            FROM candles c JOIN markets m ON m.ticker = c.market_ticker
            WHERE m.series_ticker IN ({ph}) AND m.result IN ('yes','no')
            ORDER BY c.market_ticker, c.end_ts""",
        SERIES,
    )
    by_ticker: dict[str, list] = defaultdict(list)
    for r in rows:
        by_ticker[r["market_ticker"]].append(r)
    out = []
    for m in mkts:
        ct = m["close_time"]
        if not ct:
            continue
        close_ts = int(
            dt.datetime.fromisoformat(str(ct).replace("Z", "+00:00")).timestamp()
        )
        out.append(
            (m["series_ticker"], m["ticker"], m["result"], close_ts,
             by_ticker.get(m["ticker"], []))
        )
    return out


def anchor_of(candles, close_ts) -> float | None:
    anchor = None
    for c in candles:
        htc = (close_ts - c["end_ts"]) / 3600.0
        if ANCHOR_HTC[0] <= htc <= ANCHOR_HTC[1]:
            b, a = c["yes_bid_close"], c["yes_ask_close"]
            if b is not None and a is not None and (a - b) <= ANCHOR_MAX_SPREAD:
                anchor = (a + b) / 2.0
    return anchor


RESTING_FAV = [0.25, 0.30, 0.35, 0.40, 0.45]
RESTING_DOG = [0.55, 0.60, 0.65]


def collect(markets):
    """obs[(direction, series, band)] = list of dicts per touched market."""
    obs: dict[tuple, list[dict]] = defaultdict(list)
    strat: dict[str, list[dict]] = defaultdict(list)  # strategy-proxy fills
    resting: dict[tuple, list[int]] = defaultdict(list)  # (dir, series, level) -> won
    n_anchored = 0
    for series, ticker, result, close_ts, candles in markets:
        anchor = anchor_of(candles, close_ts)
        if anchor is None:
            continue
        n_anchored += 1
        won = 1 if result == "yes" else 0
        inplay = [
            c for c in candles
            if 0.0 <= (close_ts - c["end_ts"]) / 3600.0 <= INPLAY_MAX_HTC
        ]
        if anchor >= FAV_ANCHOR:
            for lo, hi in FAV_BANDS:
                for c in inplay:
                    low = c["low"]
                    if low is None or not (lo <= low < hi):
                        continue
                    close = c["close"] if c["close"] is not None else low
                    obs[("fav", series, (lo, hi))].append({
                        "won": won,
                        "best": low,
                        "cons": min(close, low + 0.03),
                        "anchor": anchor,
                    })
                    break  # first touch only
            # strategy proxy: what the hourly-close backtest can fill
            if anchor >= 0.65:
                for c in inplay:
                    px = c["yes_ask_close"]
                    if px is None:
                        continue
                    if px <= anchor - 0.25 and 0.22 <= px <= 0.48:
                        strat[series].append({"won": won, "px": px})
                        break
            # resting-limit sim: buy YES limit at L, filled if any in-play
            # low <= L (includes games that crash straight through the level)
            lows = [c["low"] for c in inplay if c["low"] is not None]
            if lows:
                min_low = min(lows)
                for lvl in RESTING_FAV:
                    if min_low <= lvl:
                        resting[("fav", series, lvl)].append(won)
        elif anchor <= DOG_ANCHOR:
            lo, hi = DOG_BAND
            for c in inplay:
                high = c["high"]
                if high is None or not (lo <= high < hi):
                    continue
                close = c["close"] if c["close"] is not None else high
                obs[("dog", series, (lo, hi))].append({
                    "won": won,
                    "best": high,
                    "cons": max(close, high - 0.03),
                    "anchor": anchor,
                })
                break
            highs = [c["high"] for c in inplay if c["high"] is not None]
            if highs:
                max_high = max(highs)
                for lvl in RESTING_DOG:
                    if max_high >= lvl:
                        resting[("dog", series, lvl)].append(won)
    return obs, strat, resting, n_anchored


def summarize(rows: list[dict], direction: str) -> dict:
    n = len(rows)
    k = sum(r["won"] for r in rows)
    rate = k / n if n else float("nan")
    lo_ci, hi_ci = wilson(k, n)
    out = {"n": n, "k": k, "rate": rate, "ci": (lo_ci, hi_ci)}
    for kind in ("best", "cons"):
        px = [r[kind] for r in rows]
        mean_px = sum(px) / n if n else float("nan")
        if direction == "fav":
            # buy YES at px: breakeven settle rate = px + fee + half-spread
            be = sum(p + fee(p) + HALF_SPREAD for p in px) / n if n else float("nan")
            edge = rate - be
        else:
            # dog spike: fade = buy NO at (1-px); profitable iff rate < px - costs
            be = sum(p - fee(1 - p) - HALF_SPREAD for p in px) / n if n else float("nan")
            edge = be - rate  # positive = overreaction (fade pays)
        out[kind] = {"mean_px": mean_px, "breakeven": be, "edge": edge}
    return out


def fmt_row(label: str, s: dict) -> str:
    if s["n"] == 0:
        return f"  {label:<12} n=0"
    ci = s["ci"]
    return (
        f"  {label:<12} n={s['n']:<4} settle={s['rate']:.3f} "
        f"CI=[{ci[0]:.3f},{ci[1]:.3f}]  "
        f"touch={s['best']['mean_px']:.3f} be={s['best']['breakeven']:.3f} "
        f"edge={s['best']['edge']:+.3f}  | cons: px={s['cons']['mean_px']:.3f} "
        f"be={s['cons']['breakeven']:.3f} edge={s['cons']['edge']:+.3f}"
    )


def main() -> None:
    conn = sqlite3.connect(DB)
    conn.execute("PRAGMA busy_timeout=10000")
    conn.row_factory = sqlite3.Row
    markets = load(conn)
    conn.close()
    obs, strat, resting, n_anchored = collect(markets)
    print(f"settled markets: {len(markets)}; with valid pre-game anchor: {n_anchored}")
    print(f"(anchor = last mid with htc in {ANCHOR_HTC}h, spread <= {ANCHOR_MAX_SPREAD})")

    print("\n=== FAVORITES (anchor >= 0.60) — in-play LOW dips into band ===")
    print("edge = settlement rate - (entry + taker fee + 1.5c half-spread); "
          "'best' entry = candle low, 'cons' = min(close, low+0.03)")
    for lo, hi in FAV_BANDS:
        print(f"\nband [{lo:.2f},{hi:.2f}):")
        pooled = []
        for series in SERIES:
            rows = obs.get(("fav", series, (lo, hi)), [])
            pooled += rows
            print(fmt_row(series, summarize(rows, "fav")))
        print(fmt_row("POOLED", summarize(pooled, "fav")))

    print("\n=== DOGS (anchor <= 0.40) — in-play HIGH spikes into [0.55,0.70) ===")
    print("edge (+) = settle rate BELOW touch net of costs -> overreaction, fade pays;"
          "\nedge (-) with settle >= touch -> informed move.  "
          "'cons' entry = max(close, high-0.03)")
    pooled = []
    for series in SERIES:
        rows = obs.get(("dog", series, DOG_BAND), [])
        pooled += rows
        print(fmt_row(series, summarize(rows, "dog")))
    print(fmt_row("POOLED", summarize(pooled, "dog")))

    print("\n=== STRATEGY PROXY (fillable at hourly close ask) ===")
    print("anchor >= 0.65, close-ask <= anchor - 0.25, ask in [0.22,0.48]")
    pooled = []
    for series in SERIES:
        rows = strat.get(series, [])
        pooled += rows
        n = len(rows)
        if not n:
            print(f"  {series:<12} n=0")
            continue
        k = sum(r["won"] for r in rows)
        px = sum(r["px"] for r in rows) / n
        be = sum(r["px"] + fee(r["px"]) for r in rows) / n
        lo_ci, hi_ci = wilson(k, n)
        print(f"  {series:<12} n={n:<4} settle={k / n:.3f} CI=[{lo_ci:.3f},{hi_ci:.3f}] "
              f"avg_ask={px:.3f} be(ask+fee)={be:.3f} edge={k / n - be:+.3f}")
    n = len(pooled)
    if n:
        k = sum(r["won"] for r in pooled)
        be = sum(r["px"] + fee(r["px"]) for r in pooled) / n
        lo_ci, hi_ci = wilson(k, n)
        print(f"  {'POOLED':<12} n={n:<4} settle={k / n:.3f} CI=[{lo_ci:.3f},{hi_ci:.3f}] "
              f"avg_ask={sum(r['px'] for r in pooled) / n:.3f} be={be:.3f} "
              f"edge={k / n - be:+.3f}")

    print("\n=== RESTING-LIMIT SIM (the honest touch test) ===")
    print("Limit buy YES at L placed pre-game on favorites (anchor >= 0.60); "
          "fills whenever ANY in-play candle low <= L, incl. crash-throughs.\n"
          "Entry = L; breakeven = L + fee(L) + 1.5c (adverse-selection floor).")
    for lvl in RESTING_FAV:
        parts, pooled_w = [], []
        for series in SERIES:
            w = resting.get(("fav", series, lvl), [])
            pooled_w += w
            parts.append(f"{series.replace('KX', '')}: n={len(w)} "
                         f"r={sum(w) / len(w):.3f}" if w else f"{series.replace('KX', '')}: n=0")
        n, k = len(pooled_w), sum(pooled_w)
        if n:
            be = lvl + fee(lvl) + HALF_SPREAD
            lo_ci, hi_ci = wilson(k, n)
            print(f"  L={lvl:.2f}  n={n:<4} settle={k / n:.3f} CI=[{lo_ci:.3f},{hi_ci:.3f}] "
                  f"be={be:.3f} edge={k / n - be:+.3f}   [{'; '.join(parts)}]")
    print("\nMirror: resting limit SELL (buy NO) on dogs (anchor <= 0.40) at H; "
          "fills whenever any in-play high >= H.\nEdge (+) = settle rate below "
          "H - fee(1-H) - 1.5c -> fading the spike pays.")
    for lvl in RESTING_DOG:
        parts, pooled_w = [], []
        for series in SERIES:
            w = resting.get(("dog", series, lvl), [])
            pooled_w += w
            parts.append(f"{series.replace('KX', '')}: n={len(w)} "
                         f"r={sum(w) / len(w):.3f}" if w else f"{series.replace('KX', '')}: n=0")
        n, k = len(pooled_w), sum(pooled_w)
        if n:
            be = lvl - fee(1 - lvl) - HALF_SPREAD
            lo_ci, hi_ci = wilson(k, n)
            print(f"  H={lvl:.2f}  n={n:<4} settle={k / n:.3f} CI=[{lo_ci:.3f},{hi_ci:.3f}] "
                  f"be={be:.3f} edge={be - k / n:+.3f}   [{'; '.join(parts)}]")


if __name__ == "__main__":
    main()
