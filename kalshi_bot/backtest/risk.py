"""Statistical risk profiling for backtest results.

Pure functions over a list of per-trade P&Ls (settlement pnl per market,
in dollars) and/or an equity curve. No I/O, no engine imports — callers
(scripts/risk_profile.py) are responsible for extracting the inputs from
a BacktestResult or a saved JSON.

Conventions
-----------
- pnls: 1-D sequence of per-trade dollar P&Ls, one entry per settled market.
- stakes: optional 1-D sequence, dollars at risk per trade (max possible
  loss, i.e. entry cost of the position). When absent, functions that need
  a per-dollar return approximate every stake by the largest observed
  single-trade loss — a deliberately conservative normalization.
- equity_curve: sequence of (datetime, equity_dollars) points.
- All bootstrap functions take a `seed` for reproducibility.
"""

from __future__ import annotations

import datetime as dt
import math
from typing import Sequence

import numpy as np

TRADING_DAYS_PER_YEAR = 365  # Kalshi settles every calendar day

# ---------------------------------------------------------------------------
# summary statistics
# ---------------------------------------------------------------------------


def summary_stats(pnls: Sequence[float]) -> dict:
    """Distributional summary of per-trade P&Ls (dollars)."""
    x = np.asarray(pnls, dtype=float)
    n = x.size
    if n == 0:
        return {"n": 0}
    mean = float(x.mean())
    std = float(x.std(ddof=1)) if n > 1 else 0.0
    wins = x[x > 0]
    losses = x[x < 0]
    gross_win = float(wins.sum())
    gross_loss = float(-losses.sum())
    if std > 0:
        z = (x - mean) / std
        skew = float(np.mean(z**3))
        kurt = float(np.mean(z**4)) - 3.0
    else:
        skew, kurt = 0.0, 0.0
    return {
        "n": int(n),
        "mean": mean,
        "median": float(np.median(x)),
        "std": std,
        "skew": skew,
        "excess_kurtosis": kurt,
        "win_rate": float(wins.size / n),
        "avg_win": float(wins.mean()) if wins.size else 0.0,
        "avg_loss": float(losses.mean()) if losses.size else 0.0,
        "profit_factor": (gross_win / gross_loss) if gross_loss > 0 else float("inf"),
        "max_single_loss": float(x.min()) if n else 0.0,
        "total_pnl": float(x.sum()),
    }


# ---------------------------------------------------------------------------
# VaR / CVaR
# ---------------------------------------------------------------------------


def var_cvar(pnls: Sequence[float], alpha: float = 0.95) -> dict:
    """Historical per-trade VaR/CVaR at confidence `alpha`.

    Uses the empirical k worst observations, k = ceil((1-alpha)*n):
    VaR = -(k-th worst pnl), CVaR = -mean(k worst pnls). Both reported as
    positive dollar losses.
    """
    x = np.sort(np.asarray(pnls, dtype=float))
    n = x.size
    if n == 0:
        return {"alpha": alpha, "var": 0.0, "cvar": 0.0, "tail_n": 0}
    k = max(1, int(math.ceil((1.0 - alpha) * n - 1e-9)))
    return {
        "alpha": alpha,
        "var": float(-x[k - 1]),
        "cvar": float(-x[:k].mean()),
        "tail_n": k,
    }


# ---------------------------------------------------------------------------
# drawdown
# ---------------------------------------------------------------------------


def max_drawdown(equity: Sequence[float]) -> float:
    """Max peak-to-trough drawdown of an equity series, as a fraction of
    the running peak (0.25 = lost 25% from a high)."""
    eq = np.asarray(equity, dtype=float)
    if eq.size < 2:
        return 0.0
    peaks = np.maximum.accumulate(eq)
    with np.errstate(divide="ignore", invalid="ignore"):
        dd = np.where(peaks > 0, (peaks - eq) / peaks, 0.0)
    return float(dd.max())


def _path_max_drawdowns(paths: np.ndarray, start: float) -> np.ndarray:
    """Vectorized max drawdown per row of cumulative-pnl paths."""
    equity = start + np.cumsum(paths, axis=1)
    equity = np.concatenate(
        [np.full((equity.shape[0], 1), start), equity], axis=1
    )
    peaks = np.maximum.accumulate(equity, axis=1)
    with np.errstate(divide="ignore", invalid="ignore"):
        dd = np.where(peaks > 0, (peaks - equity) / peaks, 1.0)
    return dd.max(axis=1)


def _percentiles(x: np.ndarray) -> dict:
    p = np.percentile(x, [5, 25, 50, 75, 95])
    return {"p5": float(p[0]), "p25": float(p[1]), "p50": float(p[2]),
            "p75": float(p[3]), "p95": float(p[4])}


def bootstrap_drawdown(
    pnls: Sequence[float],
    n_paths: int = 10_000,
    horizon: int | None = None,
    starting_equity: float = 1000.0,
    block: int = 10,
    seed: int | None = 0,
) -> dict:
    """Bootstrap the distribution of max drawdown and terminal equity.

    Resamples trade sequences of length `horizon` (default: len(pnls))
    two ways: iid draws with replacement, and a circular block bootstrap
    with block length `block` to preserve short-range serial correlation.
    Equity paths are additive dollars on a `starting_equity` account.

    Returns percentiles (p5/p25/p50/p75/p95) of max drawdown (fraction)
    and terminal equity (dollars) for both schemes.
    """
    x = np.asarray(pnls, dtype=float)
    n = x.size
    if n == 0:
        return {"n_trades": 0}
    h = int(horizon) if horizon else n
    rng = np.random.default_rng(seed)

    out: dict = {
        "n_trades": n, "horizon": h, "n_paths": n_paths,
        "starting_equity": starting_equity, "block": block,
    }

    # --- iid bootstrap ---
    idx = rng.integers(0, n, size=(n_paths, h))
    paths = x[idx]
    out["iid"] = {
        "max_drawdown": _percentiles(_path_max_drawdowns(paths, starting_equity)),
        "terminal_equity": _percentiles(starting_equity + paths.sum(axis=1)),
    }

    # --- circular block bootstrap ---
    b = max(1, min(block, n))
    n_blocks = int(math.ceil(h / b))
    starts = rng.integers(0, n, size=(n_paths, n_blocks))
    # each block: indices start, start+1, ... start+b-1 (mod n)
    offsets = np.arange(b)
    block_idx = (starts[:, :, None] + offsets[None, None, :]) % n  # (paths, blocks, b)
    block_idx = block_idx.reshape(n_paths, n_blocks * b)[:, :h]
    bpaths = x[block_idx]
    out["block"] = {
        "max_drawdown": _percentiles(_path_max_drawdowns(bpaths, starting_equity)),
        "terminal_equity": _percentiles(starting_equity + bpaths.sum(axis=1)),
    }
    return out


# ---------------------------------------------------------------------------
# Kelly
# ---------------------------------------------------------------------------


def _per_dollar_returns(
    pnls: Sequence[float], stakes: Sequence[float] | None
) -> np.ndarray | None:
    """r_i = pnl_i / stake_i. Without stakes, approximate every stake by the
    largest observed single-trade loss (conservative). Returns None when no
    valid normalization exists (no losses and no stakes -> Kelly unbounded)."""
    x = np.asarray(pnls, dtype=float)
    if x.size == 0:
        return None
    if stakes is not None:
        s = np.asarray(stakes, dtype=float)
        if s.shape != x.shape:
            raise ValueError("stakes must align 1:1 with pnls")
        if np.any(s <= 0):
            raise ValueError("stakes must be positive")
        return x / s
    worst = -x.min()
    if worst <= 0:
        return None  # no losing trades observed: empirical Kelly is unbounded
    return x / worst


def _expected_log_growth(f: float, r: np.ndarray) -> float:
    arg = 1.0 + f * r
    if np.any(arg <= 0):
        return -np.inf
    return float(np.mean(np.log(arg)))


def kelly_fraction(
    pnls: Sequence[float],
    stakes: Sequence[float] | None = None,
    max_fraction: float = 10.0,
) -> dict:
    """Full Kelly from the empirical distribution of per-dollar-staked
    returns: argmax_f E[log(1 + f*r)], solved numerically (grid + golden
    section; no scipy). Also reports half and quarter Kelly with the
    expected per-trade log growth at each.

    `stakes` should be the dollars at risk per trade; when omitted, stake
    is approximated by the max observed single-trade loss.
    """
    r = _per_dollar_returns(pnls, stakes)
    if r is None:
        return {"full_kelly": None, "note": "undefined: no losses and no stakes given"}
    if r.max() <= 0:
        # never a winning trade: optimal f is 0
        return _kelly_report(0.0, r, stakes is not None)

    # feasible region: 1 + f*r > 0 for all r  =>  f < 1/max(-r) when losses exist
    worst_r = -r.min()
    f_hi = min(max_fraction, (1.0 / worst_r) * (1 - 1e-9)) if worst_r > 0 else max_fraction

    # coarse grid then golden-section refine
    grid = np.linspace(0.0, f_hi, 2001)
    vals = [_expected_log_growth(f, r) for f in grid]
    i = int(np.argmax(vals))
    lo = grid[max(0, i - 1)]
    hi = grid[min(len(grid) - 1, i + 1)]
    phi = (math.sqrt(5) - 1) / 2
    a, b = lo, hi
    c, d = b - phi * (b - a), a + phi * (b - a)
    for _ in range(80):
        if _expected_log_growth(c, r) >= _expected_log_growth(d, r):
            b, d = d, c
            c = b - phi * (b - a)
        else:
            a, c = c, d
            d = a + phi * (b - a)
    f_star = (a + b) / 2
    if _expected_log_growth(0.0, r) >= _expected_log_growth(f_star, r):
        f_star = 0.0  # negative edge
    return _kelly_report(f_star, r, stakes is not None)


def _kelly_report(f_star: float, r: np.ndarray, exact_stakes: bool) -> dict:
    def g(f: float) -> float:
        v = _expected_log_growth(f, r)
        return v if math.isfinite(v) else float("nan")

    return {
        "full_kelly": f_star,
        "half_kelly": f_star / 2,
        "quarter_kelly": f_star / 4,
        "growth_per_trade_full": g(f_star),
        "growth_per_trade_half": g(f_star / 2),
        "growth_per_trade_quarter": g(f_star / 4),
        "mean_return_per_dollar_staked": float(r.mean()),
        "stake_basis": "per-trade stakes" if exact_stakes else "max observed loss",
    }


# ---------------------------------------------------------------------------
# risk of ruin
# ---------------------------------------------------------------------------


def risk_of_ruin(
    pnls: Sequence[float],
    sizing_fraction: float,
    ruin_level: float = 0.5,
    stakes: Sequence[float] | None = None,
    starting_equity: float = 1000.0,
    n_trades: int = 1000,
    n_paths: int = 10_000,
    seed: int | None = 0,
) -> dict:
    """Bootstrap P(account ever drops below ruin_level * starting_equity
    within n_trades) when each trade stakes `sizing_fraction` of current
    equity. Per-trade return r is pnl per dollar staked (see kelly_fraction);
    equity compounds as E *= 1 + sizing_fraction * r.
    """
    r = _per_dollar_returns(pnls, stakes)
    if r is None:
        return {"prob_ruin": 0.0, "note": "no losing trades in sample"}
    rng = np.random.default_rng(seed)
    idx = rng.integers(0, r.size, size=(n_paths, n_trades))
    growth = 1.0 + sizing_fraction * r[idx]
    growth = np.maximum(growth, 0.0)  # equity floors at zero
    log_eq = np.log(starting_equity) + np.cumsum(
        np.log(np.maximum(growth, 1e-300)), axis=1
    )
    ruined_by_wipeout = (growth <= 0).any(axis=1)
    min_eq = np.exp(log_eq.min(axis=1))
    ruined = ruined_by_wipeout | (min_eq < ruin_level * starting_equity)
    return {
        "sizing_fraction": sizing_fraction,
        "ruin_level": ruin_level,
        "n_trades": n_trades,
        "n_paths": n_paths,
        "prob_ruin": float(ruined.mean()),
        "median_terminal_equity": float(np.median(np.exp(log_eq[:, -1]))),
    }


# ---------------------------------------------------------------------------
# annualized figures from a dated equity curve
# ---------------------------------------------------------------------------


def annualized_from_curve(
    equity_curve: Sequence[tuple[dt.datetime, float]],
    risk_free: float = 0.0,
) -> dict:
    """Annualized return, vol, Sharpe, Sortino, Calmar from a (datetime,
    equity) curve. The curve is collapsed to one point per calendar day
    (last observation) before computing daily returns."""
    if len(equity_curve) < 2:
        return {}
    daily: dict[str, float] = {}
    for ts, v in equity_curve:
        daily[ts.date().isoformat()] = float(v)
    days = sorted(daily)
    vals = np.array([daily[d] for d in days], dtype=float)
    if vals.size < 2 or vals[0] <= 0:
        return {}

    t0 = dt.date.fromisoformat(days[0])
    t1 = dt.date.fromisoformat(days[-1])
    years = max((t1 - t0).days, 1) / 365.25

    total_return = vals[-1] / vals[0] - 1.0
    cagr = (vals[-1] / vals[0]) ** (1 / years) - 1.0 if vals[-1] > 0 else -1.0

    with np.errstate(divide="ignore", invalid="ignore"):
        rets = vals[1:] / vals[:-1] - 1.0
    rets = rets[np.isfinite(rets)]
    mean = float(rets.mean()) if rets.size else 0.0
    vol_d = float(rets.std(ddof=1)) if rets.size > 1 else 0.0
    ann = math.sqrt(TRADING_DAYS_PER_YEAR)
    rf_d = risk_free / TRADING_DAYS_PER_YEAR
    downside = rets[rets < rf_d] - rf_d
    dvol = float(np.sqrt(np.mean(downside**2))) if downside.size else 0.0
    mdd = max_drawdown(vals)
    return {
        "start": days[0],
        "end": days[-1],
        "years": years,
        "total_return": float(total_return),
        "annualized_return": float(cagr),
        "annualized_vol": vol_d * ann,
        "sharpe": ((mean - rf_d) / vol_d * ann) if vol_d > 0 else 0.0,
        "sortino": ((mean - rf_d) / dvol * ann) if dvol > 0 else float("inf"),
        "max_drawdown": mdd,
        "calmar": (cagr / mdd) if mdd > 0 else float("inf"),
    }


# ---------------------------------------------------------------------------
# small-sample bootstrap CIs
# ---------------------------------------------------------------------------


def bootstrap_ci(
    pnls: Sequence[float],
    stat: str = "mean",
    ci: float = 0.90,
    n_boot: int = 10_000,
    seed: int | None = 0,
) -> dict:
    """Percentile-bootstrap CI for a per-trade statistic ('mean', 'median',
    'win_rate'). Used to report honest interval estimates when n is small."""
    x = np.asarray(pnls, dtype=float)
    if x.size == 0:
        return {}
    fns = {
        "mean": lambda a: a.mean(axis=1),
        "median": lambda a: np.median(a, axis=1),
        "win_rate": lambda a: (a > 0).mean(axis=1),
    }
    fn = fns[stat]
    rng = np.random.default_rng(seed)
    samples = x[rng.integers(0, x.size, size=(n_boot, x.size))]
    stats = fn(samples)
    lo, hi = np.percentile(stats, [(1 - ci) / 2 * 100, (1 + ci) / 2 * 100])
    return {
        "stat": stat,
        "point": float(fn(x[None, :])[0]),
        "ci": ci,
        "lo": float(lo),
        "hi": float(hi),
    }
