"""Load stored history and run a strategy through the backtester.

Usage:
    python -m kalshi_bot.backtest.runner --strategy expiry_convergence --series KXHIGHNY KXHIGHCHI
"""

from __future__ import annotations

import argparse
import json

from kalshi_bot.backtest.engine import Backtester, BacktestResult
from kalshi_bot.data.store import connect
from kalshi_bot.strategies import STRATEGIES
from kalshi_bot.strategies.base import Context, Strategy


def load_context() -> Context:
    """Load all stored external signals into a Context keyed by
    (source, key, target_date); dict payloads are parsed from raw JSON."""
    conn = connect()
    signals = {}
    for r in conn.execute("SELECT source, key, target_date, raw, value FROM external_signals"):
        payload = json.loads(r["raw"]) if r["raw"] else r["value"]
        signals[(r["source"], r["key"], r["target_date"])] = payload
    conn.close()
    return Context(signals=signals)


def load_data(series: list[str], settled_only: bool = True):
    conn = connect()
    ph = ",".join("?" * len(series))
    where = f"series_ticker IN ({ph})"
    if settled_only:
        where += " AND result IN ('yes','no')"
    markets = {
        r["ticker"]: dict(r)
        for r in conn.execute(f"SELECT * FROM markets WHERE {where}", series)
    }
    candles: dict[str, list[dict]] = {}
    if markets:
        mph = ",".join("?" * len(markets))
        rows = conn.execute(
            f"""SELECT * FROM candles WHERE market_ticker IN ({mph})
                ORDER BY market_ticker, end_ts""",
            list(markets),
        )
        for r in rows:
            candles.setdefault(r["market_ticker"], []).append(dict(r))
    conn.close()
    return markets, candles


def run(
    strategy: Strategy,
    series: list[str],
    starting_cash: float = 1000.0,
    max_position: float = 50.0,
) -> BacktestResult:
    markets, candles = load_data(series)
    bt = Backtester(starting_cash=starting_cash, max_position_per_market=max_position)
    return bt.run(strategy, markets, candles, context=load_context())


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--strategy", required=True, choices=sorted(STRATEGIES))
    ap.add_argument("--series", nargs="+", required=True)
    ap.add_argument("--cash", type=float, default=1000.0)
    ap.add_argument("--max-position", type=float, default=50.0)
    ap.add_argument("--save", action="store_true",
                    help="write result JSON to data/backtests/ for the dashboard")
    args = ap.parse_args()

    strat = STRATEGIES[args.strategy]()
    result = run(strat, args.series, args.cash, args.max_position)
    print(json.dumps(result.metrics(), indent=2))
    losses = sorted(result.settlements, key=lambda s: s[2])[:5]
    if losses:
        print("worst settlements:", [(t, r, round(p, 2)) for t, r, p in losses])
    if args.save:
        import datetime as dt

        from kalshi_bot.config import PROJECT_ROOT

        out_dir = PROJECT_ROOT / "data" / "backtests"
        out_dir.mkdir(parents=True, exist_ok=True)
        stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
        payload = {
            "strategy": args.strategy,
            "series": args.series,
            "ran_at": dt.datetime.now(dt.UTC).isoformat(),
            "starting_cash": args.cash,
            "metrics": result.metrics(),
            "equity_curve": [
                (ts.isoformat(), round(v, 2)) for ts, v in result.equity_curve
            ],
        }
        path = out_dir / f"{stamp}-{args.strategy}.json"
        path.write_text(json.dumps(payload))
        print(f"saved {path}")


if __name__ == "__main__":
    main()
