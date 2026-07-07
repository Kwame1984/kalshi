"""Long-horizon calibration surface for settled Kalshi markets.

Motivation: Le (2026) finds Kalshi's calibration slope is ~1.32 beyond one
month — long-dated prices are compressed toward 50c, so long-dated favorites
trade cheap and long-dated longshots trade dear. With Kalshi now paying
~3.25-4% APY on cash AND open positions, a multi-month lockup carries
positively, making the long-horizon side of the surface actionable.

Method: one sample per settled market per horizon in {30, 60, 90, 180} days
before close — the candle nearest to (close_time - horizon) within +/-7 days
that has a two-sided quote with spread <= 0.12. Samples are bucketed by mid
price decile; per (horizon, bucket) we report n, avg mid, win rate, edge
(win rate - avg mid), a 95% Wilson CI on the win rate (edge CI = Wilson
bounds - avg mid), and the ask-side edge (win rate - avg ask) because
point-in-time mid edges have repeatedly died at ask-side execution.

Also fits the implied calibration slope per horizon: logistic regression of
outcome on logit(mid), P(yes) = sigmoid(a + b * logit(price)), by Newton
iteration (no statsmodels dependency). b > 1 => prices compressed toward 50c
(favorites cheap); b < 1 => prices too extreme.

Splits: category (Economics vs everything else) and settle era
(<= 2024 vs >= 2025) to check regime robustness.

Usage:
    python scripts/long_horizon_study.py [--min-n 8]
"""

from __future__ import annotations

import argparse
import datetime as dt
import math
from collections import defaultdict
from dataclasses import dataclass

from kalshi_bot.data.store import connect

HORIZONS_DAYS = (30, 60, 90, 180)
WINDOW_S = 7 * 86400          # nearest candle within +/-7 days of target
MAX_SPREAD = 0.12             # acceptable at long horizons
PERIOD_MINUTES = 60


@dataclass
class Sample:
    horizon: int
    ticker: str
    series: str
    category: str
    era: str        # 'le2024' | 'ge2025'
    mid: float
    bid: float
    ask: float
    outcome: int    # 1 = settled yes


# ---------------------------------------------------------------------------
# sampling
# ---------------------------------------------------------------------------


def _close_ts(close_time: str) -> int | None:
    try:
        return int(
            dt.datetime.fromisoformat(
                str(close_time).replace("Z", "+00:00")
            ).timestamp()
        )
    except (ValueError, TypeError):
        return None


def collect_samples(conn) -> list[Sample]:
    markets = []
    q = """
        SELECT m.ticker, m.series_ticker, m.close_time, m.result,
               COALESCE(s.category, 'Unknown') AS category
        FROM markets m LEFT JOIN series s ON s.ticker = m.series_ticker
        WHERE m.result IN ('yes', 'no') AND m.close_time IS NOT NULL
    """
    for r in conn.execute(q):
        ts = _close_ts(r["close_time"])
        if ts is None:
            continue
        year = int(str(r["close_time"])[:4])
        markets.append((
            r["ticker"], r["series_ticker"] or "", r["category"],
            "le2024" if year <= 2024 else "ge2025",
            ts, 1 if r["result"] == "yes" else 0,
        ))

    samples: list[Sample] = []
    cur = conn.cursor()
    for ticker, series, category, era, close_ts, outcome in markets:
        for h in HORIZONS_DAYS:
            target = close_ts - h * 86400
            rows = cur.execute(
                """SELECT end_ts, yes_bid_close, yes_ask_close
                   FROM candles
                   WHERE market_ticker = ? AND period_minutes = ?
                     AND end_ts BETWEEN ? AND ?""",
                (ticker, PERIOD_MINUTES, target - WINDOW_S, target + WINDOW_S),
            ).fetchall()
            best = None
            for end_ts, bid, ask in rows:
                if bid is None or ask is None:
                    continue
                if bid <= 0.0 or ask >= 1.0 or ask < bid:
                    continue
                if ask - bid > MAX_SPREAD:
                    continue
                d = abs(end_ts - target)
                if best is None or d < best[0]:
                    best = (d, bid, ask)
            if best is None:
                continue
            _, bid, ask = best
            samples.append(Sample(
                horizon=h, ticker=ticker, series=series, category=category,
                era=era, mid=(bid + ask) / 2, bid=bid, ask=ask, outcome=outcome,
            ))
    return samples


# ---------------------------------------------------------------------------
# statistics
# ---------------------------------------------------------------------------


def wilson_ci(wins: int, n: int, z: float = 1.96) -> tuple[float, float]:
    if n == 0:
        return (0.0, 1.0)
    p = wins / n
    denom = 1 + z * z / n
    center = (p + z * z / (2 * n)) / denom
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / denom
    return (center - half, center + half)


def logit(p: float, eps: float = 0.01) -> float:
    p = min(max(p, eps), 1 - eps)
    return math.log(p / (1 - p))


def calibration_slope(samples: list[Sample]) -> tuple[float, float] | None:
    """Fit P(yes) = sigmoid(a + b * logit(mid)) by Newton-Raphson.
    Returns (a, b) or None if the fit is degenerate."""
    xs = [logit(s.mid) for s in samples]
    ys = [s.outcome for s in samples]
    n = len(xs)
    if n < 20 or len(set(ys)) < 2:
        return None
    a, b = 0.0, 1.0
    for _ in range(50):
        g0 = g1 = 0.0            # gradient
        h00 = h01 = h11 = 0.0    # Hessian (negative log-lik is convex)
        for x, y in zip(xs, ys):
            z = a + b * x
            z = min(max(z, -30.0), 30.0)
            p = 1.0 / (1.0 + math.exp(-z))
            w = p * (1 - p)
            e = p - y
            g0 += e
            g1 += e * x
            h00 += w
            h01 += w * x
            h11 += w * x * x
        det = h00 * h11 - h01 * h01
        if abs(det) < 1e-12:
            return None
        da = (h11 * g0 - h01 * g1) / det
        db = (h00 * g1 - h01 * g0) / det
        a -= da
        b -= db
        if abs(da) < 1e-9 and abs(db) < 1e-9:
            break
    return (a, b)


def bucket_table(samples: list[Sample]) -> list[dict]:
    buckets: dict[int, list[Sample]] = defaultdict(list)
    for s in samples:
        buckets[min(int(s.mid * 10), 9)].append(s)
    rows = []
    for k in sorted(buckets):
        ss = buckets[k]
        n = len(ss)
        wins = sum(s.outcome for s in ss)
        avg_mid = sum(s.mid for s in ss) / n
        avg_ask = sum(s.ask for s in ss) / n
        wr = wins / n
        lo, hi = wilson_ci(wins, n)
        rows.append({
            "bucket": f"{k / 10:.1f}-{(k + 1) / 10:.1f}",
            "n": n, "avg_mid": avg_mid, "avg_ask": avg_ask,
            "win_rate": wr, "edge_mid": wr - avg_mid, "edge_ask": wr - avg_ask,
            "ci_lo": lo - avg_mid, "ci_hi": hi - avg_mid,
        })
    return rows


# ---------------------------------------------------------------------------
# reporting
# ---------------------------------------------------------------------------


def print_table(title: str, samples: list[Sample], min_n: int) -> None:
    print(f"\n### {title}  (n={len(samples)})")
    if not samples:
        return
    hdr = (f"{'bucket':>9} {'n':>5} {'avg_mid':>8} {'win%':>7} "
           f"{'edge_mid':>9} {'edge_ask':>9} {'95% CI(edge_mid)':>18}")
    print(hdr)
    for r in bucket_table(samples):
        flag = "" if r["n"] >= min_n else "  (thin)"
        print(f"{r['bucket']:>9} {r['n']:>5} {r['avg_mid']:>8.3f} "
              f"{r['win_rate'] * 100:>6.1f}% {r['edge_mid'] * 100:>+8.1f}c "
              f"{r['edge_ask'] * 100:>+8.1f}c "
              f"[{r['ci_lo'] * 100:>+6.1f}c,{r['ci_hi'] * 100:>+6.1f}c]{flag}")
    fit = calibration_slope(samples)
    if fit:
        a, b = fit
        print(f"  implied calibration: P(yes) = sigmoid({a:+.3f} "
              f"{b:+.3f} * logit(price))   slope={b:.3f}")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--min-n", type=int, default=8,
                    help="flag buckets thinner than this")
    args = ap.parse_args()

    conn = connect()
    conn.execute("PRAGMA busy_timeout = 15000")
    conn.execute("PRAGMA query_only = ON")
    samples = collect_samples(conn)
    conn.close()

    print(f"total samples: {len(samples)} "
          f"(one per settled market per horizon, spread <= {MAX_SPREAD})")

    for h in HORIZONS_DAYS:
        hs = [s for s in samples if s.horizon == h]
        print("\n" + "=" * 72)
        print(f"HORIZON {h}d")
        print("=" * 72)
        print_table("pooled", hs, args.min_n)
        econ = [s for s in hs if s.category == "Economics"]
        rest = [s for s in hs if s.category != "Economics"]
        print_table("Economics", econ, args.min_n)
        print_table("non-Economics", rest, args.min_n)
        for era, label in (("le2024", "settle year <= 2024"),
                           ("ge2025", "settle year >= 2025")):
            print_table(label, [s for s in hs if s.era == era], args.min_n)

    print("\n" + "=" * 72)
    print("CALIBRATION SLOPE SUMMARY (pooled, and by era)")
    print("=" * 72)
    print(f"{'horizon':>8} {'segment':>22} {'n':>6} {'intercept':>10} {'slope':>7}")
    for h in HORIZONS_DAYS:
        hs = [s for s in samples if s.horizon == h]
        segs = [("pooled", hs),
                ("Economics", [s for s in hs if s.category == "Economics"]),
                ("era<=2024", [s for s in hs if s.era == "le2024"]),
                ("era>=2025", [s for s in hs if s.era == "ge2025"])]
        for label, seg in segs:
            fit = calibration_slope(seg)
            if fit:
                a, b = fit
                print(f"{h:>7}d {label:>22} {len(seg):>6} {a:>+10.3f} {b:>7.3f}")
            else:
                print(f"{h:>7}d {label:>22} {len(seg):>6} {'--':>10} {'--':>7}")


if __name__ == "__main__":
    main()
