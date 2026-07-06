"""Load stored history and run a strategy through the backtester.

Usage:
    python -m kalshi_bot.backtest.runner --strategy expiry_convergence --series KXHIGHNY KXHIGHCHI
"""

from __future__ import annotations

import argparse
import json

from kalshi_bot.backtest.engine import Backtester, BacktestResult
from kalshi_bot.data.store import connect
from kalshi_bot.strategies.base import Strategy
from kalshi_bot.strategies.expiry_convergence import ExpiryConvergence

STRATEGIES: dict[str, type[Strategy]] = {
    "expiry_convergence": ExpiryConvergence,
}


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
    return bt.run(strategy, markets, candles)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--strategy", required=True, choices=sorted(STRATEGIES))
    ap.add_argument("--series", nargs="+", required=True)
    ap.add_argument("--cash", type=float, default=1000.0)
    ap.add_argument("--max-position", type=float, default=50.0)
    args = ap.parse_args()

    strat = STRATEGIES[args.strategy]()
    result = run(strat, args.series, args.cash, args.max_position)
    print(json.dumps(result.metrics(), indent=2))
    losses = sorted(result.settlements, key=lambda s: s[2])[:5]
    if losses:
        print("worst settlements:", [(t, r, round(p, 2)) for t, r, p in losses])


if __name__ == "__main__":
    main()
