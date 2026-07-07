"""Statistical risk profile for a backtested strategy.

Turns backtest results into a rigorous per-strategy risk profile:
distribution stats, VaR/CVaR, bootstrapped drawdown distributions,
empirical Kelly sizing, risk of ruin, and annualized figures.

Usage:
    # run a fresh backtest and profile it
    python scripts/risk_profile.py --strategy tail_seller --series KXCPI KXFED ...

    # profile a saved run (data/backtests/*.json)
    python scripts/risk_profile.py --from-json data/backtests/20260707-022606-tail_seller.json
    # saved JSONs lack per-trade pnls; add --reconstruct to re-run the
    # backtest (same strategy/series) and recover them
    python scripts/risk_profile.py --from-json ... --reconstruct

Writes data/risk_profiles/<strategy>-<stamp>.json.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import math
from collections import defaultdict

from kalshi_bot.backtest import risk
from kalshi_bot.backtest.engine import BacktestResult
from kalshi_bot.backtest.runner import run as run_backtest
from kalshi_bot.config import PROJECT_ROOT
from kalshi_bot.strategies import STRATEGIES

SMALL_N = 30
RUIN_SIZINGS = (0.05, 0.10, 0.25)  # fractions of equity staked per trade


# ---------------------------------------------------------------------------
# extraction: BacktestResult -> per-trade pnls / stakes / equity curve
# ---------------------------------------------------------------------------


def extract_trades(result: BacktestResult) -> tuple[list[float], list[float]]:
    """Per-market net P&Ls and stakes from a BacktestResult.

    Engine settlements record pnl gross of fees (payout - entry cost), so
    fees paid on each market's fills are subtracted here to get the P&L
    that actually hit the account. Stake = dollars at risk on the market:
    max possible loss of the position (long yes: cost of yes contracts;
    short yes / long no: 1-price per contract) plus fees.
    """
    fees_by_ticker: dict[str, float] = defaultdict(float)
    stake_by_ticker: dict[str, float] = defaultdict(float)
    for f in result.fills:
        fees_by_ticker[f.ticker] += f.fee
        at_risk = f.contracts * f.price if f.contracts > 0 else -f.contracts * (1.0 - f.price)
        stake_by_ticker[f.ticker] += at_risk
    pnls, stakes = [], []
    for ticker, _res, pnl in result.settlements:
        net = pnl - fees_by_ticker[ticker]
        stake = stake_by_ticker[ticker] + fees_by_ticker[ticker]
        if stake <= 0:
            continue  # no recorded fills for this settlement; cannot size it
        pnls.append(net)
        stakes.append(stake)
    return pnls, stakes


# ---------------------------------------------------------------------------
# profile computation
# ---------------------------------------------------------------------------


def build_profile(
    pnls: list[float] | None,
    stakes: list[float] | None,
    equity_curve: list[tuple[dt.datetime, float]] | None,
    starting_cash: float = 1000.0,
) -> dict:
    profile: dict = {}
    small_sample = pnls is not None and len(pnls) < SMALL_N

    if pnls:
        profile["summary"] = risk.summary_stats(pnls)
        profile["var_cvar"] = {
            "95": risk.var_cvar(pnls, alpha=0.95),
            "99": risk.var_cvar(pnls, alpha=0.99),
        }
        profile["drawdown_bootstrap"] = risk.bootstrap_drawdown(
            pnls, n_paths=10_000, starting_equity=starting_cash
        )
        profile["kelly"] = risk.kelly_fraction(pnls, stakes=stakes)
        profile["risk_of_ruin"] = {
            f"{f:.2f}": risk.risk_of_ruin(
                pnls, sizing_fraction=f, ruin_level=0.5, stakes=stakes,
                starting_equity=starting_cash, n_trades=1000,
            )
            for f in RUIN_SIZINGS
        }
        kelly = profile["kelly"].get("half_kelly")
        if kelly and kelly > 0:
            profile["risk_of_ruin"]["half_kelly"] = risk.risk_of_ruin(
                pnls, sizing_fraction=kelly, ruin_level=0.5, stakes=stakes,
                starting_equity=starting_cash, n_trades=1000,
            )
        if small_sample:
            profile["small_sample"] = {
                "n": len(pnls),
                "mean_ci90": risk.bootstrap_ci(pnls, "mean", ci=0.90),
                "median_ci90": risk.bootstrap_ci(pnls, "median", ci=0.90),
                "win_rate_ci90": risk.bootstrap_ci(pnls, "win_rate", ci=0.90),
            }

    if equity_curve:
        ann = risk.annualized_from_curve(equity_curve)
        if ann:
            profile["annualized"] = ann
    return profile


# ---------------------------------------------------------------------------
# formatting
# ---------------------------------------------------------------------------


def _fmt(v: float, nd: int = 2, pct: bool = False) -> str:
    if v is None:
        return "n/a"
    if isinstance(v, float) and math.isinf(v):
        return "inf"
    return f"{v * 100:.{nd}f}%" if pct else f"{v:.{nd}f}"


def format_report(name: str, profile: dict, warnings: list[str]) -> str:
    L: list[str] = []
    bar = "=" * 64
    L.append(bar)
    L.append(f"RISK PROFILE — {name}")
    L.append(bar)

    for w in warnings:
        L.append(f"!!! {w}")
    if warnings:
        L.append("")

    s = profile.get("summary")
    if s:
        L.append("PER-TRADE P&L (net of fees, $ per settled market)")
        L.append(f"  n trades        {s['n']}")
        L.append(f"  mean / median   {_fmt(s['mean'])} / {_fmt(s['median'])}")
        L.append(f"  std             {_fmt(s['std'])}")
        L.append(f"  skew / ex-kurt  {_fmt(s['skew'])} / {_fmt(s['excess_kurtosis'], 1)}")
        L.append(f"  win rate        {_fmt(s['win_rate'], 1, pct=True)}")
        L.append(f"  avg win / loss  {_fmt(s['avg_win'])} / {_fmt(s['avg_loss'])}")
        L.append(f"  profit factor   {_fmt(s['profit_factor'])}")
        L.append(f"  max single loss {_fmt(s['max_single_loss'])}")
        L.append(f"  total pnl       {_fmt(s['total_pnl'])}")
        L.append("")

    vc = profile.get("var_cvar")
    if vc:
        L.append("PER-TRADE VaR / CVaR (historical, $ loss)")
        for a in ("95", "99"):
            v = vc[a]
            L.append(
                f"  {a}%:  VaR {_fmt(v['var'])}   CVaR {_fmt(v['cvar'])}"
                f"   (tail n={v['tail_n']})"
            )
        L.append("")

    db = profile.get("drawdown_bootstrap")
    if db:
        L.append(
            f"BOOTSTRAPPED DRAWDOWN  (${db['starting_equity']:.0f} account, "
            f"{db['n_paths']} paths x {db['horizon']} trades)"
        )
        for scheme, label in (("iid", "iid resample"), ("block", f"block={db['block_len']}")):
            d, t = db[scheme]["max_drawdown"], db[scheme]["terminal_equity"]
            L.append(f"  {label:<14} max drawdown  "
                     f"p5 {_fmt(d['p5'], 1, pct=True)}  p25 {_fmt(d['p25'], 1, pct=True)}  "
                     f"p50 {_fmt(d['p50'], 1, pct=True)}  p75 {_fmt(d['p75'], 1, pct=True)}  "
                     f"p95 {_fmt(d['p95'], 1, pct=True)}")
            L.append(f"  {'':<14} terminal eq   "
                     f"p5 {_money(t['p5'])}  p25 {_money(t['p25'])}  p50 {_money(t['p50'])}  "
                     f"p75 {_money(t['p75'])}  p95 {_money(t['p95'])}")
        L.append("")

    k = profile.get("kelly")
    if k:
        L.append(f"KELLY SIZING  (stake basis: {k.get('stake_basis', 'n/a')})")
        if k.get("full_kelly") is None:
            L.append(f"  undefined — {k.get('note', '')}")
        else:
            L.append(f"  mean return per $ staked  {_fmt(k['mean_return_per_dollar_staked'], 2, pct=True)}")
            for label, fk, gk in (
                ("full", "full_kelly", "growth_per_trade_full"),
                ("half", "half_kelly", "growth_per_trade_half"),
                ("quarter", "quarter_kelly", "growth_per_trade_quarter"),
            ):
                L.append(
                    f"  {label:<8} f = {_fmt(k[fk], 3)}   "
                    f"E[log growth]/trade = {k[gk]:.5f}"
                )
        L.append("")

    rr = profile.get("risk_of_ruin")
    if rr:
        L.append("RISK OF RUIN  (P[equity < 50% of start] within 1000 trades)")
        for key, r in rr.items():
            label = "half-Kelly" if key == "half_kelly" else f"{float(key) * 100:.0f}% / trade"
            L.append(
                f"  {label:<14} f={r['sizing_fraction']:.3f}   "
                f"P(ruin) = {_fmt(r['prob_ruin'], 2, pct=True)}   "
                f"median terminal eq = {r['median_terminal_equity']:.0f}"
            )
        L.append("")

    ss = profile.get("small_sample")
    if ss:
        L.append(f"SMALL-SAMPLE 90% BOOTSTRAP CIs  (n={ss['n']})")
        for key, label in (("mean_ci90", "mean pnl"), ("median_ci90", "median pnl"),
                           ("win_rate_ci90", "win rate")):
            c = ss[key]
            L.append(f"  {label:<12} {_fmt(c['point'])}  [{_fmt(c['lo'])}, {_fmt(c['hi'])}]")
        L.append("")

    a = profile.get("annualized")
    if a:
        L.append(f"ANNUALIZED (equity curve {a['start']} -> {a['end']}, "
                 f"{a['years']:.2f}y)")
        L.append(f"  total return    {_fmt(a['total_return'], 2, pct=True)}")
        L.append(f"  ann. return     {_fmt(a['annualized_return'], 2, pct=True)}")
        L.append(f"  ann. vol        {_fmt(a['annualized_vol'], 2, pct=True)}")
        L.append(f"  Sharpe          {_fmt(a['sharpe'])}")
        L.append(f"  Sortino         {_fmt(a['sortino'])}")
        L.append(f"  max drawdown    {_fmt(a['max_drawdown'], 1, pct=True)}")
        L.append(f"  Calmar          {_fmt(a['calmar'])}")
        L.append("")

    L.append(bar)
    return "\n".join(L)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _parse_curve(raw: list) -> list[tuple[dt.datetime, float]]:
    return [(dt.datetime.fromisoformat(ts), float(v)) for ts, v in raw]


def _json_safe(obj):
    if isinstance(obj, dict):
        return {k: _json_safe(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_json_safe(v) for v in obj]
    if isinstance(obj, float) and not math.isfinite(obj):
        return None
    return obj


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    src = ap.add_mutually_exclusive_group(required=True)
    src.add_argument("--strategy", choices=sorted(STRATEGIES))
    src.add_argument("--from-json", help="saved backtest JSON (data/backtests/)")
    ap.add_argument("--series", nargs="+", help="series tickers (with --strategy)")
    ap.add_argument("--cash", type=float, default=1000.0)
    ap.add_argument("--max-position", type=float, default=50.0)
    ap.add_argument("--reconstruct", action="store_true",
                    help="with --from-json: re-run the backtest to recover "
                         "per-trade pnls the saved file lacks")
    ap.add_argument("--no-save", action="store_true",
                    help="skip writing data/risk_profiles/ JSON")
    args = ap.parse_args()

    warnings: list[str] = []
    pnls = stakes = None
    curve = None
    meta: dict = {}

    def _run(strategy: str, series: list[str], cash: float) -> None:
        nonlocal pnls, stakes, curve
        result = run_backtest(STRATEGIES[strategy](), series, cash, args.max_position)
        pnls, stakes = extract_trades(result)
        curve = result.equity_curve
        meta["backtest_metrics"] = result.metrics()

    if args.strategy:
        if not args.series:
            ap.error("--series is required with --strategy")
        name = args.strategy
        meta.update({"strategy": name, "series": args.series, "source": "fresh backtest"})
        _run(name, args.series, args.cash)
        starting_cash = args.cash
    else:
        saved = json.loads(open(args.from_json).read())
        name = saved.get("strategy", "unknown")
        series = saved.get("series", [])
        starting_cash = float(saved.get("starting_cash", args.cash))
        meta.update({"strategy": name, "series": series,
                     "source": args.from_json, "saved_metrics": saved.get("metrics")})
        curve = _parse_curve(saved.get("equity_curve", []))
        if args.reconstruct:
            if name not in STRATEGIES or not series:
                ap.error("cannot reconstruct: saved JSON lacks a known strategy/series")
            _run(name, series, starting_cash)
        else:
            warnings.append(
                "saved JSON has no per-trade pnls: only equity-curve figures "
                "computed; pass --reconstruct to re-run the backtest and get "
                "the full profile"
            )

    if pnls is not None and len(pnls) < SMALL_N:
        warnings.append(
            f"SMALL SAMPLE: only {len(pnls)} settled trades (<{SMALL_N}). "
            "Point estimates below are unreliable — read the 90% bootstrap "
            "CIs instead, and treat Kelly/ruin numbers as indicative only."
        )
    if pnls is not None and len(pnls) == 0:
        warnings.append("no settled trades: per-trade sections skipped")

    profile = build_profile(pnls, stakes, curve, starting_cash)
    report = format_report(name, profile, warnings)
    print(report)

    if not args.no_save:
        out_dir = PROJECT_ROOT / "data" / "risk_profiles"
        out_dir.mkdir(parents=True, exist_ok=True)
        stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
        path = out_dir / f"{name}-{stamp}.json"
        payload = {
            **meta,
            "ran_at": dt.datetime.now(dt.UTC).isoformat(),
            "starting_cash": starting_cash,
            "n_trades": len(pnls) if pnls is not None else None,
            "warnings": warnings,
            "profile": _json_safe(profile),
        }
        path.write_text(json.dumps(payload, indent=1))
        print(f"saved {path}")


if __name__ == "__main__":
    main()
