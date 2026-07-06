"""Event-driven backtester for Kalshi binary contracts.

Replays hourly candles for settled markets in time order. Fill model is
deliberately conservative:
- market orders fill at the candle's closing ask (buy yes) / bid (sell yes),
  and pay the quadratic taker fee;
- positions held to settlement pay out $1 (yes) or $0 per the recorded result;
- a market with no bid/ask data at a step is skipped.

Positions are tracked in signed yes-contracts (buying NO = selling YES
short at 1 - no_price, which is equivalent P&L-wise and keeps one ledger).
"""

from __future__ import annotations

import datetime as dt
import math
from collections import defaultdict
from dataclasses import dataclass, field

from kalshi_bot.backtest.fees import taker_fee
from kalshi_bot.strategies.base import Context, MarketState, Signal, Strategy


@dataclass
class Fill:
    ts: dt.datetime
    ticker: str
    contracts: float  # signed yes-contracts (+ = bought yes / sold no)
    price: float      # yes price paid/received
    fee: float
    strategy: str
    reason: str


@dataclass
class BacktestResult:
    fills: list[Fill] = field(default_factory=list)
    equity_curve: list[tuple[dt.datetime, float]] = field(default_factory=list)
    settlements: list[tuple[str, str, float]] = field(default_factory=list)  # ticker, result, pnl
    starting_cash: float = 1000.0

    @property
    def total_fees(self) -> float:
        return sum(f.fee for f in self.fills)

    @property
    def final_equity(self) -> float:
        return self.equity_curve[-1][1] if self.equity_curve else self.starting_cash

    @property
    def total_return(self) -> float:
        return self.final_equity / self.starting_cash - 1.0

    def metrics(self) -> dict:
        eq = [v for _, v in self.equity_curve]
        if len(eq) < 2:
            return {"trades": len(self.fills), "return": 0.0}
        peak, max_dd = eq[0], 0.0
        for v in eq:
            peak = max(peak, v)
            max_dd = max(max_dd, (peak - v) / peak if peak > 0 else 0.0)
        # daily returns from (ts, equity) points
        daily: dict[str, float] = {}
        for ts, v in self.equity_curve:
            daily[ts.date().isoformat()] = v
        vals = list(daily.values())
        rets = [(b / a - 1.0) for a, b in zip(vals, vals[1:]) if a > 0]
        mean = sum(rets) / len(rets) if rets else 0.0
        var = sum((r - mean) ** 2 for r in rets) / len(rets) if rets else 0.0
        sharpe = (mean / math.sqrt(var) * math.sqrt(365)) if var > 0 else 0.0
        wins = sum(1 for _, _, pnl in self.settlements if pnl > 0)
        return {
            "trades": len(self.fills),
            "markets_settled": len(self.settlements),
            "hit_rate": wins / len(self.settlements) if self.settlements else 0.0,
            "return": self.total_return,
            "final_equity": round(self.final_equity, 2),
            "total_fees": round(self.total_fees, 2),
            "max_drawdown": round(max_dd, 4),
            "sharpe_daily_ann": round(sharpe, 2),
        }


class Backtester:
    def __init__(
        self,
        starting_cash: float = 1000.0,
        fee_multiplier: float = 1.0,
        max_position_per_market: float = 50.0,  # dollars of cost basis
    ):
        self.starting_cash = starting_cash
        self.fee_multiplier = fee_multiplier
        self.max_position_per_market = max_position_per_market

    def run(
        self,
        strategy: Strategy,
        markets: dict[str, dict],
        candles: dict[str, list[dict]],
        context: Context | None = None,
    ) -> BacktestResult:
        """markets: ticker -> market row (must include 'result' for settlement).
        candles: ticker -> list of candle rows sorted by end_ts
                 (keys: end_ts, close, yes_bid_close, yes_ask_close, volume, open_interest).
        """
        ctx = context or Context()
        result = BacktestResult(starting_cash=self.starting_cash)
        cash = self.starting_cash
        pos: dict[str, float] = defaultdict(float)        # signed yes-contracts
        cost_basis: dict[str, float] = defaultdict(float)  # dollars committed

        # Build a global time-ordered event stream
        events: list[tuple[int, str, dict]] = []
        for ticker, rows in candles.items():
            if ticker not in markets:
                continue
            for c in rows:
                events.append((c["end_ts"], ticker, c))
        events.sort(key=lambda e: e[0])

        settle_at: dict[str, int] = {}
        for ticker, m in markets.items():
            ct = m.get("close_time")
            if ct:
                close = dt.datetime.fromisoformat(str(ct).replace("Z", "+00:00"))
                settle_at[ticker] = int(close.timestamp())

        last_mark: dict[str, float] = {}
        settled: set[str] = set()

        def mark_equity(ts: dt.datetime) -> None:
            mtm = sum(pos[t] * last_mark.get(t, 0.0) for t in pos)
            result.equity_curve.append((ts, cash + mtm))

        for ts_int, ticker, c in events:
            ts = dt.datetime.fromtimestamp(ts_int, dt.UTC)
            m = markets[ticker]
            bid, ask = c.get("yes_bid_close"), c.get("yes_ask_close")
            if bid is not None:
                last_mark[ticker] = bid if pos[ticker] >= 0 else (ask or bid)
            state = MarketState(
                ticker=ticker, ts=ts,
                yes_bid=bid, yes_ask=ask, last=c.get("close"),
                volume=c.get("volume") or 0.0,
                open_interest=c.get("open_interest") or 0.0,
                meta=m,
            )
            ctx.positions = dict(pos)
            for sig in strategy.generate_signals(state, ctx):
                fill = self._execute(sig, state, cash, pos, cost_basis)
                if fill:
                    cash -= fill.contracts * fill.price + fill.fee
                    pos[ticker] += fill.contracts
                    cost_basis[ticker] += abs(fill.contracts * fill.price)
                    result.fills.append(fill)
            mark_equity(ts)

        # settle everything with a recorded result
        final_ts = (
            result.equity_curve[-1][0] if result.equity_curve else dt.datetime.now(dt.UTC)
        )
        for ticker, contracts in list(pos.items()):
            if contracts == 0 or ticker in settled:
                continue
            res = markets[ticker].get("result")
            if res not in ("yes", "no"):
                # unsettled: liquidate at last mark
                cash += contracts * last_mark.get(ticker, 0.0)
                continue
            payout = 1.0 if res == "yes" else 0.0
            pnl = contracts * payout
            cash += pnl
            entry_cost = sum(
                f.contracts * f.price for f in result.fills if f.ticker == ticker
            )
            result.settlements.append((ticker, res, pnl - entry_cost))
            settled.add(ticker)
            pos[ticker] = 0.0
        result.equity_curve.append((final_ts, cash))
        return result

    def _execute(
        self,
        sig: Signal,
        state: MarketState,
        cash: float,
        pos: dict[str, float],
        cost_basis: dict[str, float],
    ) -> Fill | None:
        # Normalize: 'no' side = negative yes-contracts at yes-side pricing
        want = sig.contracts if sig.side == "yes" else -sig.contracts
        buying_yes = want > 0
        price = state.yes_ask if buying_yes else state.yes_bid
        if price is None or price <= 0.0 or price >= 1.0:
            return None
        if sig.limit_price is not None:
            if buying_yes and price > sig.limit_price:
                return None
            if not buying_yes and price < (sig.limit_price):
                return None
        # cost: buying yes costs price; buying no costs (1 - price) per contract
        unit_cost = price if buying_yes else (1.0 - price)
        # cap by per-market exposure and available cash
        budget = min(
            self.max_position_per_market - cost_basis[sig.ticker],
            cash,
        )
        max_contracts = math.floor(budget / unit_cost) if unit_cost > 0 else 0
        contracts = min(abs(want), max_contracts)
        if contracts < 1:
            return None
        fee_price = price if buying_yes else (1.0 - price)
        fee = taker_fee(fee_price, contracts, self.fee_multiplier)
        signed = contracts if buying_yes else -contracts
        return Fill(
            ts=state.ts, ticker=sig.ticker, contracts=signed,
            price=price, fee=fee, strategy=sig.strategy, reason=sig.reason,
        )
