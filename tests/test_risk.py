"""Tests for kalshi_bot.backtest.risk against synthetic distributions
with known ground truth."""

from __future__ import annotations

import datetime as dt
import math

import numpy as np
import pytest

from kalshi_bot.backtest.risk import (
    annualized_from_curve,
    bootstrap_ci,
    bootstrap_drawdown,
    kelly_fraction,
    max_drawdown,
    risk_of_ruin,
    summary_stats,
    var_cvar,
)

# ---------------------------------------------------------------------------
# summary_stats
# ---------------------------------------------------------------------------


def test_summary_stats_known_values():
    pnls = [10.0, -5.0, 10.0, -5.0, 20.0]
    s = summary_stats(pnls)
    assert s["n"] == 5
    assert s["mean"] == pytest.approx(6.0)
    assert s["median"] == pytest.approx(10.0)
    assert s["std"] == pytest.approx(np.std(pnls, ddof=1))
    assert s["win_rate"] == pytest.approx(3 / 5)
    assert s["avg_win"] == pytest.approx(40 / 3)
    assert s["avg_loss"] == pytest.approx(-5.0)
    assert s["profit_factor"] == pytest.approx(40 / 10)
    assert s["max_single_loss"] == pytest.approx(-5.0)
    assert s["total_pnl"] == pytest.approx(30.0)


def test_summary_stats_symmetric_has_zero_skew():
    pnls = [-2.0, -1.0, 0.0, 1.0, 2.0]
    s = summary_stats(pnls)
    assert s["skew"] == pytest.approx(0.0, abs=1e-12)


def test_summary_stats_empty():
    assert summary_stats([]) == {"n": 0}


# ---------------------------------------------------------------------------
# VaR / CVaR: known discrete distribution
# ---------------------------------------------------------------------------


def test_var_cvar_discrete_ground_truth():
    # 100 trades: worst five are -5,-4,-3,-2,-1; the rest +1.
    # At alpha=0.95 the tail is exactly k=5 observations:
    # VaR = -(5th worst) = 1, CVaR = mean(5,4,3,2,1) = 3.
    pnls = [-5.0, -4.0, -3.0, -2.0, -1.0] + [1.0] * 95
    out = var_cvar(pnls, alpha=0.95)
    assert out["tail_n"] == 5
    assert out["var"] == pytest.approx(1.0)
    assert out["cvar"] == pytest.approx(3.0)


def test_var_cvar_99_uses_single_worst():
    pnls = [-50.0] + [1.0] * 99
    out = var_cvar(pnls, alpha=0.99)
    assert out["tail_n"] == 1
    assert out["var"] == pytest.approx(50.0)
    assert out["cvar"] == pytest.approx(50.0)


# ---------------------------------------------------------------------------
# drawdown
# ---------------------------------------------------------------------------


def test_max_drawdown_deterministic():
    # peak 120 -> trough 90: dd = 30/120 = 0.25
    assert max_drawdown([100, 120, 90, 130]) == pytest.approx(0.25)
    # monotone rise: no drawdown
    assert max_drawdown([1, 2, 3, 4]) == 0.0
    assert max_drawdown([100]) == 0.0


def test_bootstrap_drawdown_deterministic_when_one_value():
    # every trade loses exactly $10: any resampled path of length 50 is
    # deterministic; max dd of 1000 -> 500 is 0.5, terminal is 500.
    out = bootstrap_drawdown([-10.0] * 20, n_paths=200, horizon=50, seed=1)
    for scheme in ("iid", "block"):
        dd = out[scheme]["max_drawdown"]
        te = out[scheme]["terminal_equity"]
        assert dd["p5"] == pytest.approx(0.5)
        assert dd["p95"] == pytest.approx(0.5)
        assert te["p50"] == pytest.approx(500.0)


def test_bootstrap_drawdown_all_wins_never_draws_down():
    out = bootstrap_drawdown([5.0, 5.0, 5.0], n_paths=100, seed=2)
    assert out["iid"]["max_drawdown"]["p95"] == 0.0
    assert out["iid"]["terminal_equity"]["p50"] == pytest.approx(1015.0)


def test_bootstrap_drawdown_percentiles_ordered():
    rng = np.random.default_rng(3)
    pnls = rng.normal(0.5, 5.0, size=200)
    out = bootstrap_drawdown(pnls, n_paths=2000, seed=3)
    for scheme in ("iid", "block"):
        d = out[scheme]["max_drawdown"]
        assert d["p5"] <= d["p25"] <= d["p50"] <= d["p75"] <= d["p95"]


# ---------------------------------------------------------------------------
# Kelly: Bernoulli payoffs with closed-form f* = p - q/b
# ---------------------------------------------------------------------------


def test_kelly_bernoulli_even_money():
    # p=0.6, b=1 (win $1 per $1 staked, lose stake): f* = 0.6 - 0.4/1 = 0.2
    pnls = [1.0] * 60 + [-1.0] * 40
    stakes = [1.0] * 100
    out = kelly_fraction(pnls, stakes=stakes)
    assert out["full_kelly"] == pytest.approx(0.2, abs=1e-3)
    assert out["half_kelly"] == pytest.approx(0.1, abs=1e-3)
    assert out["quarter_kelly"] == pytest.approx(0.05, abs=1e-3)
    # growth at full Kelly: p*log(1+f) + q*log(1-f)
    g = 0.6 * math.log(1.2) + 0.4 * math.log(0.8)
    assert out["growth_per_trade_full"] == pytest.approx(g, abs=1e-6)


def test_kelly_bernoulli_2_to_1():
    # p=0.5, b=2: f* = 0.5 - 0.5/2 = 0.25
    pnls = [2.0] * 50 + [-1.0] * 50
    out = kelly_fraction(pnls, stakes=[1.0] * 100)
    assert out["full_kelly"] == pytest.approx(0.25, abs=1e-3)


def test_kelly_negative_edge_is_zero():
    pnls = [1.0] * 40 + [-1.0] * 60
    out = kelly_fraction(pnls, stakes=[1.0] * 100)
    assert out["full_kelly"] == 0.0
    assert out["growth_per_trade_full"] == pytest.approx(0.0)


def test_kelly_without_stakes_uses_max_loss_normalization():
    # normalized by max loss (=1), identical to stakes of 1
    pnls = [1.0] * 60 + [-1.0] * 40
    out = kelly_fraction(pnls)
    assert out["stake_basis"] == "max observed loss"
    assert out["full_kelly"] == pytest.approx(0.2, abs=1e-3)


def test_kelly_no_losses_is_undefined_without_stakes():
    out = kelly_fraction([1.0, 2.0, 3.0])
    assert out["full_kelly"] is None


# ---------------------------------------------------------------------------
# risk of ruin
# ---------------------------------------------------------------------------


def test_risk_of_ruin_certain_for_always_losing():
    pnls = [-1.0] * 50  # every trade loses the full stake fraction
    out = risk_of_ruin(pnls, sizing_fraction=0.1, n_trades=100, n_paths=500, seed=4)
    assert out["prob_ruin"] == 1.0


def test_risk_of_ruin_tiny_for_strong_edge_small_size():
    pnls = [1.0] * 90 + [-1.0] * 10
    out = risk_of_ruin(
        pnls, sizing_fraction=0.01, n_trades=1000, n_paths=2000, seed=5
    )
    assert out["prob_ruin"] < 0.001


def test_risk_of_ruin_monotone_in_sizing():
    rng = np.random.default_rng(6)
    pnls = list(rng.normal(0.05, 1.0, size=300))
    probs = [
        risk_of_ruin(pnls, sizing_fraction=f, n_trades=500, n_paths=2000, seed=7)[
            "prob_ruin"
        ]
        for f in (0.02, 0.10, 0.30)
    ]
    assert probs[0] <= probs[1] <= probs[2]


# ---------------------------------------------------------------------------
# annualized figures
# ---------------------------------------------------------------------------


def _curve(values: list[float], start="2024-01-01") -> list[tuple[dt.datetime, float]]:
    d0 = dt.datetime.fromisoformat(start).replace(tzinfo=dt.UTC)
    return [(d0 + dt.timedelta(days=i), v) for i, v in enumerate(values)]


def test_annualized_return_exact_one_year():
    # 1000 -> 1100 over exactly 365.25-ish days: use 366 points => 365 days
    vals = list(np.linspace(1000, 1100, 366))
    out = annualized_from_curve(_curve(vals))
    assert out["total_return"] == pytest.approx(0.10)
    # 365 days / 365.25 ~ 1 year: annualized ~ total
    assert out["annualized_return"] == pytest.approx(0.10, abs=0.005)
    assert out["max_drawdown"] == 0.0
    assert out["calmar"] == float("inf")
    assert out["sortino"] == float("inf")  # no down days


def test_annualized_vol_and_sharpe_signs():
    rng = np.random.default_rng(8)
    rets = rng.normal(0.001, 0.01, size=400)
    vals = 1000 * np.cumprod(1 + rets)
    out = annualized_from_curve(_curve(list(vals)))
    assert out["annualized_vol"] == pytest.approx(
        np.std(rets, ddof=1) * math.sqrt(365), rel=0.05
    )
    assert out["sharpe"] > 0
    assert 0 <= out["max_drawdown"] < 1


def test_annualized_empty_and_short():
    assert annualized_from_curve([]) == {}
    assert annualized_from_curve(_curve([1000.0])) == {}


# ---------------------------------------------------------------------------
# bootstrap CI
# ---------------------------------------------------------------------------


def test_bootstrap_ci_contains_truth():
    rng = np.random.default_rng(9)
    pnls = list(rng.normal(2.0, 1.0, size=500))
    out = bootstrap_ci(pnls, stat="mean", ci=0.95, n_boot=4000, seed=9)
    assert out["lo"] < 2.0 < out["hi"]
    assert out["lo"] < out["point"] < out["hi"]


def test_bootstrap_ci_win_rate():
    pnls = [1.0] * 70 + [-1.0] * 30
    out = bootstrap_ci(pnls, stat="win_rate", ci=0.90, n_boot=4000, seed=10)
    assert out["point"] == pytest.approx(0.70)
    assert out["lo"] < 0.70 < out["hi"]
