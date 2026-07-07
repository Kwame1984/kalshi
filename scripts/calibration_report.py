"""Price-bucket calibration for settled econ markets.

For each settled market, sample the order book once per horizon (~24h and
~72h before close, nearest hourly candle within a tolerance) and tabulate
win rate vs price — a calibration table. Also reports the fee-aware,
executable 94-99c favorite trade (buy YES at ask / buy NO at 1-bid) with a
per-series breakdown.

Usage:
    python scripts/calibration_report.py [--series KXCPI KXFED ...]
"""

from __future__ import annotations

import argparse
import datetime as dt
import math
from collections import defaultdict

from kalshi_bot.backtest.fees import taker_fee
from kalshi_bot.data.collector import HIST_ECON_SERIES
from kalshi_bot.data.store import connect

HORIZONS_H = (24, 72)
TOLERANCE_S = 6 * 3600  # candle must sit within this window of the target time
BUCKETS = [
    (0.00, 0.01), (0.01, 0.03), (0.03, 0.06), (0.06, 0.10),
    (0.10, 0.20), (0.20, 0.40), (0.40, 0.60), (0.60, 0.80),
    (0.80, 0.90), (0.90, 0.94), (0.94, 0.97), (0.97, 0.99), (0.99, 1.01),
]


def _iso_to_ts(iso: str | None) -> int | None:
    if not iso:
        return None
    return int(dt.datetime.fromisoformat(iso.replace("Z", "+00:00")).timestamp())


def load(series: list[str]):
    conn = connect()
    ph = ",".join("?" * len(series))
    markets = [
        dict(r) for r in conn.execute(
            f"""SELECT ticker, series_ticker, close_time, result FROM markets
                WHERE series_ticker IN ({ph}) AND result IN ('yes','no')""",
            series,
        )
    ]
    candles: dict[str, list[dict]] = defaultdict(list)
    rows = conn.execute(
        """SELECT market_ticker, end_ts, close, yes_bid_close, yes_ask_close
           FROM candles WHERE period_minutes = 60 ORDER BY end_ts"""
    )
    tickers = {m["ticker"] for m in markets}
    for r in rows:
        if r["market_ticker"] in tickers:
            candles[r["market_ticker"]].append(dict(r))
    conn.close()
    return markets, candles


def nearest_candle(rows: list[dict], target_ts: int) -> dict | None:
    best, best_d = None, TOLERANCE_S + 1
    for c in rows:  # rows sorted by end_ts
        d = abs(c["end_ts"] - target_ts)
        if d < best_d:
            best, best_d = c, d
        elif c["end_ts"] > target_ts + TOLERANCE_S:
            break
    return best


def sample_all(markets, candles):
    """One sample per market per horizon: (series, horizon, mid, bid, ask, won)."""
    samples = []
    for m in markets:
        close_ts = _iso_to_ts(m["close_time"])
        rows = candles.get(m["ticker"])
        if not close_ts or not rows:
            continue
        won = 1.0 if m["result"] == "yes" else 0.0
        for h in HORIZONS_H:
            c = nearest_candle(rows, close_ts - h * 3600)
            if c is None:
                continue
            bid, ask = c["yes_bid_close"], c["yes_ask_close"]
            if bid is not None and ask is not None and 0 < ask <= 1 and bid < ask:
                mid = (bid + ask) / 2
            elif c["close"] is not None:
                mid, bid, ask = c["close"], bid, ask
            else:
                continue
            samples.append({
                "series": m["series_ticker"], "ticker": m["ticker"], "h": h,
                "mid": mid, "bid": bid, "ask": ask, "won": won,
            })
    return samples


def wilson_low(wins: int, n: int, z: float = 1.96) -> float:
    if n == 0:
        return 0.0
    p = wins / n
    denom = 1 + z * z / n
    centre = p + z * z / (2 * n)
    spread = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n))
    return (centre - spread) / denom


def print_calibration(samples, horizon):
    print(f"\n== Calibration (mid price, ~{horizon}h before close) ==")
    print(f"{'bucket':>12} {'n':>5} {'avg_px':>7} {'win%':>7} {'edge':>7}")
    for lo, hi in BUCKETS:
        rows = [s for s in samples if s["h"] == horizon and lo <= s["mid"] < hi]
        if not rows:
            continue
        n = len(rows)
        avg = sum(s["mid"] for s in rows) / n
        win = sum(s["won"] for s in rows) / n
        print(f"{lo:5.2f}-{hi:4.2f} {n:>5} {avg:7.3f} {win*100:6.1f}% {win-avg:+7.3f}")


def executable_9499(samples, horizon, by_series=False):
    """Fee-aware favorite trade: buy YES at ask, or NO at 1-bid, in [0.94, 0.99]."""
    trades = defaultdict(list)  # key -> (entry_price, won)
    for s in samples:
        if s["h"] != horizon:
            continue
        if s["ask"] is not None and 0.94 <= s["ask"] <= 0.99:
            trades[s["series"] if by_series else "ALL"].append((s["ask"], s["won"]))
        if s["bid"] is not None and 0.94 <= 1 - s["bid"] <= 0.99:
            trades[s["series"] if by_series else "ALL"].append((1 - s["bid"], 1 - s["won"]))
    out = []
    for key in sorted(trades):
        rows = trades[key]
        n = len(rows)
        wins = sum(w for _, w in rows)
        avg_px = sum(p for p, _ in rows) / n
        # per-contract P&L: win -> 1 - px - fee ; lose -> -px - fee
        pnl = sum((w - p) - taker_fee(p, 1) for p, w in rows)
        out.append((key, n, int(wins), avg_px, wins / n, pnl / n, wilson_low(int(wins), n)))
    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--series", nargs="*", default=HIST_ECON_SERIES)
    args = ap.parse_args()

    markets, candles = load(args.series)
    samples = sample_all(markets, candles)
    n_markets = len({s["ticker"] for s in samples})
    closes = sorted(m["close_time"] for m in markets if m.get("close_time"))
    print(f"series: {', '.join(args.series)}")
    print(f"settled markets: {len(markets)}, sampled: {n_markets}, "
          f"close range {closes[0][:10]}..{closes[-1][:10]}" if closes else "no data")

    for h in HORIZONS_H:
        print_calibration(samples, h)

    for h in HORIZONS_H:
        print(f"\n== Executable 94-99c favorite (taker, fee-adjusted, ~{h}h out) ==")
        print(f"{'series':>12} {'n':>5} {'wins':>5} {'avg_px':>7} {'win%':>7} "
              f"{'net_pnl/ct':>10} {'wilson_lo':>9}")
        rows = executable_9499(samples, h) + executable_9499(samples, h, by_series=True)
        for key, n, wins, avg_px, wr, unit_pnl, wlo in rows:
            be = avg_px + taker_fee(avg_px, 1)  # breakeven win rate
            flag = " *" if wlo > be else ""
            print(f"{key:>12} {n:>5} {wins:>5} {avg_px:7.3f} {wr*100:6.1f}% "
                  f"{unit_pnl:+10.4f} {wlo*100:8.1f}%{flag}")
        print("  (* = Wilson 95% lower bound on win rate clears the fee-adjusted breakeven)")


if __name__ == "__main__":
    main()
