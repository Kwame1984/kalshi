"""Validate the ensemble weather model against the market, then backtest.

Diagnostic (before any backtest): at lead-1 snapshots of all settled
KXHIGH* brackets, compare log-loss / Brier of
  (a) market mid,
  (b) ensemble model (WeatherEnsemble.model_probability, walk-forward),
  (c) 50/50 blend,
  (d) logistic blend of (a)+(b) fit on the FIRST part of the sample only
      (walk-forward split by target date), evaluated on the rest,
plus the deterministic Gaussian model (weather_calibration.json) as the
established baseline on the same rows.

Then run the backtester on all 7 series with taker fills for
min_net_edge in {0.05, 0.08, 0.12}.

Usage: .venv/bin/python scripts/validate_weather_ensemble.py
"""

from __future__ import annotations

import datetime as dt
import json
import math
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import numpy as np

from kalshi_bot.backtest.engine import Backtester
from kalshi_bot.backtest.runner import load_context, load_data
from kalshi_bot.config import PROJECT_ROOT
from kalshi_bot.data.external.weather import STATIONS
from kalshi_bot.strategies.weather_edge import (
    prob_in_range,
    strike_bounds,
    ticker_target_date,
)
from kalshi_bot.strategies.weather_ensemble import WeatherEnsemble

SERIES = list(STATIONS)
LEAD = 1
EPS = 1e-3
FIT_DAYS = 30  # distinct target dates used to fit the logistic blend


def clip(p: float) -> float:
    return min(1.0 - EPS, max(EPS, p))


def log_loss(rows, key) -> float:
    return -sum(
        math.log(clip(r[key])) if r["y"] else math.log(1 - clip(r[key]))
        for r in rows
    ) / len(rows)


def brier(rows, key) -> float:
    return sum((clip(r[key]) - r["y"]) ** 2 for r in rows) / len(rows)


def logit(p: float) -> float:
    p = clip(p)
    return math.log(p / (1 - p))


def fit_logistic(X: np.ndarray, y: np.ndarray, iters: int = 50) -> np.ndarray:
    w = np.zeros(X.shape[1])
    for _ in range(iters):
        z = X @ w
        p = 1 / (1 + np.exp(-z))
        W = p * (1 - p) + 1e-9
        grad = X.T @ (y - p)
        H = (X * W[:, None]).T @ X + 1e-6 * np.eye(X.shape[1])
        step = np.linalg.solve(H, grad)
        w += step
        if np.abs(step).max() < 1e-8:
            break
    return w


def build_snapshots() -> list[dict]:
    """One lead-1 snapshot per settled bracket: last candle (with bid+ask)
    whose UTC date is target_date - 1."""
    markets, candles = load_data(SERIES)
    ctx = load_context()
    ens = WeatherEnsemble()

    # deterministic baseline params (the established finding's model)
    cal_path = PROJECT_ROOT / "data" / "weather_calibration.json"
    det_cal = json.loads(cal_path.read_text()) if cal_path.exists() else {}

    rows = []
    for ticker, m in markets.items():
        target = ticker_target_date(ticker)
        if target is None or m.get("result") not in ("yes", "no"):
            continue
        low, high = strike_bounds(m)
        if low is None and high is None:
            continue
        snap_day = target - dt.timedelta(days=LEAD)
        snap = None
        for c in candles.get(ticker, []):
            ts = dt.datetime.fromtimestamp(c["end_ts"], dt.UTC)
            if ts.date() == snap_day and c["yes_bid_close"] is not None \
                    and c["yes_ask_close"] is not None:
                snap = c
        if snap is None:
            continue
        p_mkt = (snap["yes_bid_close"] + snap["yes_ask_close"]) / 2
        if p_mkt <= 0.0 or p_mkt >= 1.0:
            continue
        series = m["series_ticker"]
        p_ens = ens.model_probability(series, target, LEAD, snap_day, ctx, low, high)

        # deterministic reference
        p_det = None
        fc = ctx.get_external("open_meteo", f"{series}.high_f", target.isoformat())
        if isinstance(fc, dict) and fc.get(f"high_f_lead_{LEAD}") is not None:
            cal = det_cal.get(series, {})
            bias = cal.get("bias", {}).get(str(LEAD), 0.0)
            sigma = (cal.get("sigma", {}).get(str(LEAD)) or 2.8) * 1.10
            p_det = prob_in_range(fc[f"high_f_lead_{LEAD}"] + bias, sigma, low, high)

        rows.append({
            "ticker": ticker, "series": series, "date": target.isoformat(),
            "y": 1 if m["result"] == "yes" else 0,
            "mkt": p_mkt, "ens": p_ens, "det": p_det,
        })
    return rows


def report(rows: list[dict], keys: list[str], label: str) -> None:
    print(f"\n  {label} (n={len(rows)}, "
          f"{len({r['date'] for r in rows})} target days)")
    print(f"    {'model':<12}{'log-loss':>10}{'brier':>10}")
    for k in keys:
        print(f"    {k:<12}{log_loss(rows, k):>10.4f}{brier(rows, k):>10.4f}")


def main() -> None:
    rows = build_snapshots()
    print(f"settled lead-{LEAD} bracket snapshots: {len(rows)}")

    covered = [r for r in rows if r["ens"] is not None]
    for r in covered:
        r["blend"] = 0.5 * (r["mkt"] + r["ens"])
    print(f"with ensemble (HGEFS lead-1 spread) coverage: {len(covered)} "
          f"({len({r['date'] for r in covered})} target days: "
          f"{min(r['date'] for r in covered)} .. {max(r['date'] for r in covered)})")

    det_rows = [r for r in covered if r["det"] is not None]
    report(det_rows, ["mkt", "det", "ens", "blend"], "FULL ensemble-covered sample")

    # per-city
    print("\n  per city (full covered sample): log-loss")
    print(f"    {'series':<12}{'n':>5}{'market':>9}{'ens':>9}{'blend':>9}")
    for s in SERIES:
        sub = [r for r in covered if r["series"] == s]
        if not sub:
            continue
        print(f"    {s:<12}{len(sub):>5}{log_loss(sub, 'mkt'):>9.4f}"
              f"{log_loss(sub, 'ens'):>9.4f}{log_loss(sub, 'blend'):>9.4f}")

    # walk-forward logistic blend: fit on first FIT_DAYS distinct dates
    days = sorted({r["date"] for r in covered})
    fit_dates = set(days[:FIT_DAYS])
    train = [r for r in covered if r["date"] in fit_dates]
    test = [r for r in covered if r["date"] not in fit_dates]
    if train and test:
        X = np.array([[1.0, logit(r["ens"]), logit(r["mkt"])] for r in train])
        y = np.array([float(r["y"]) for r in train])
        w = fit_logistic(X, y)
        for r in test:
            z = w[0] + w[1] * logit(r["ens"]) + w[2] * logit(r["mkt"])
            r["logit_blend"] = 1 / (1 + math.exp(-z))
        print(f"\n  logistic blend fit on first {len(fit_dates)} days "
              f"(n={len(train)}), weights [b, ens, mkt] = "
              f"[{w[0]:.3f}, {w[1]:.3f}, {w[2]:.3f}]")
        report(test, ["mkt", "ens", "blend", "logit_blend"],
               "OUT-OF-SAMPLE eval (remaining days)")

    # ------------------------------------------------------------ backtests
    print("\nbacktests (taker fills, all 7 series, $1000 cash, $50/market cap):")
    markets, candles = load_data(SERIES)
    for mne in (0.05, 0.08, 0.12):
        strat = WeatherEnsemble(min_net_edge=mne)
        bt = Backtester(starting_cash=1000.0, max_position_per_market=50.0)
        res = bt.run(strat, markets, candles, context=load_context())
        m = res.metrics()
        print(f"\n  min_net_edge={mne:.2f}: {json.dumps(m)}")
        losses = sorted(res.settlements, key=lambda s: s[2])[:3]
        if losses:
            print("    worst:", [(t, r, round(p, 2)) for t, r, p in losses])


if __name__ == "__main__":
    main()
