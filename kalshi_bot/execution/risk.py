"""Pre-trade risk checks and position sizing.

Every check reads bot_status fresh so an operator (or the UI) flipping
kill_switch takes effect on the next signal, not the next restart.
"""

from __future__ import annotations

import math
import sqlite3
from dataclasses import dataclass

from kalshi_bot.config import Settings
from kalshi_bot.execution import state
from kalshi_bot.strategies.base import Signal

KELLY_FRACTION = 0.25  # quarter-Kelly


@dataclass
class RiskDecision:
    allowed: bool
    contracts: float  # sized contracts (0 when blocked)
    reason: str = ""


class RiskManager:
    def __init__(
        self,
        settings: Settings,
        conn: sqlite3.Connection,
        mode: str,
        bankroll: float = 1000.0,
    ):
        self.settings = settings
        self.conn = conn
        self.mode = mode
        self.bankroll = bankroll

    # -- kill switch ---------------------------------------------------------

    def kill_switch_on(self) -> bool:
        return (state.get_status(self.conn, "kill_switch", "off") or "off").lower() in (
            "on", "true", "1",
        )

    def halt(self, reason: str) -> None:
        state.set_status(self.conn, "kill_switch", "on")
        state.set_status(self.conn, "halt_reason", reason)

    def daily_loss_breached(self) -> bool:
        raw = state.get_status(self.conn, "daily_pnl", "0") or "0"
        try:
            daily_pnl = float(raw)
        except ValueError:
            daily_pnl = 0.0
        return daily_pnl <= -self.settings.max_daily_loss

    # -- sizing ---------------------------------------------------------------

    def size(self, sig: Signal, price: float) -> float:
        """Quarter-Kelly contract count, capped by the per-market limit.

        `price` is the YES price the order would execute at. For a 'no'
        signal we bet on NO: effective entry price is (1 - price) and the
        win probability is (1 - fair_value), since Signal.fair_value is
        always the YES probability.

        Binary-contract Kelly at entry price p with win probability f:
            f* = (f - p) / (1 - p)
        Dollar size = KELLY_FRACTION * f* * bankroll; contracts = size / p.
        """
        if sig.side == "yes":
            p, f = price, sig.fair_value
        else:
            p, f = 1.0 - price, 1.0 - sig.fair_value
        if p <= 0.0 or p >= 1.0:
            return 0.0
        kelly = (f - p) / (1.0 - p)
        if kelly <= 0:
            return 0.0
        dollars = KELLY_FRACTION * kelly * self.bankroll
        cap = self.settings.max_position_per_market - state.market_exposure(
            self.conn, self.mode, sig.ticker
        )
        budget = min(cap, dollars)
        if budget <= 0:
            return 0.0
        return float(min(sig.contracts, math.floor(budget / p)))

    # -- pre-trade check --------------------------------------------------------

    def check(self, sig: Signal, price: float) -> RiskDecision:
        """Full pre-trade gate. `price` is the YES execution price."""
        if self.kill_switch_on():
            return RiskDecision(False, 0.0, "kill_switch on: "
                                + (state.get_status(self.conn, "halt_reason", "") or ""))
        if self.daily_loss_breached():
            self.halt(f"daily loss limit (${self.settings.max_daily_loss:.2f}) breached")
            return RiskDecision(False, 0.0, "daily loss halt")

        unit_cost = price if sig.side == "yes" else (1.0 - price)
        if unit_cost <= 0.0 or unit_cost >= 1.0:
            return RiskDecision(False, 0.0, f"bad price {price}")

        contracts = self.size(sig, price)
        if contracts < 1:
            return RiskDecision(False, 0.0, "sized to zero (edge/cap)")

        cost = contracts * unit_cost
        market_exp = state.market_exposure(self.conn, self.mode, sig.ticker)
        if market_exp + cost > self.settings.max_position_per_market + 1e-9:
            return RiskDecision(
                False, 0.0,
                f"per-market cap ${self.settings.max_position_per_market:.0f} "
                f"(have ${market_exp:.2f}, want ${cost:.2f})",
            )
        total_exp = state.total_exposure(self.conn, self.mode)
        if total_exp + cost > self.settings.max_total_exposure + 1e-9:
            return RiskDecision(
                False, 0.0,
                f"total exposure cap ${self.settings.max_total_exposure:.0f} "
                f"(have ${total_exp:.2f}, want ${cost:.2f})",
            )
        return RiskDecision(True, contracts, "ok")
