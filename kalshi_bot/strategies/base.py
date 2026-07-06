"""Strategy interface shared by the backtester and live/paper executors."""

from __future__ import annotations

import datetime as dt
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any


@dataclass
class MarketState:
    """Point-in-time view of one market. Prices in dollars (0-1), yes side."""

    ticker: str
    ts: dt.datetime
    yes_bid: float | None
    yes_ask: float | None
    last: float | None
    volume: float = 0.0
    open_interest: float = 0.0
    meta: dict[str, Any] = field(default_factory=dict)  # market row: strikes, close_time, ...

    @property
    def mid(self) -> float | None:
        if self.yes_bid is not None and self.yes_ask is not None:
            return (self.yes_bid + self.yes_ask) / 2
        return self.last

    @property
    def spread(self) -> float | None:
        if self.yes_bid is not None and self.yes_ask is not None:
            return self.yes_ask - self.yes_bid
        return None

    def hours_to_close(self) -> float | None:
        ct = self.meta.get("close_time")
        if not ct:
            return None
        close = dt.datetime.fromisoformat(str(ct).replace("Z", "+00:00"))
        return (close - self.ts).total_seconds() / 3600.0


@dataclass
class Signal:
    """Desired trade. side is 'yes' or 'no'; contracts > 0.

    fair_value is the strategy's probability estimate for YES — the executor
    and backtester use (fair_value - price) net of fees to size/filter.
    """

    ticker: str
    side: str
    contracts: float
    fair_value: float
    limit_price: float | None = None  # None = take the market
    reason: str = ""
    strategy: str = ""


class Context:
    """External data access for strategies (forecasts, nowcasts, positions)."""

    def __init__(self, signals: dict[str, Any] | None = None):
        self.external = signals or {}
        self.positions: dict[str, float] = {}  # ticker -> signed yes-contracts

    def get_external(self, source: str, key: str, target_date: str) -> Any:
        return self.external.get((source, key, target_date))


class Strategy(ABC):
    name: str = "base"

    @abstractmethod
    def generate_signals(self, state: MarketState, ctx: Context) -> list[Signal]:
        """Called once per market per time step."""
