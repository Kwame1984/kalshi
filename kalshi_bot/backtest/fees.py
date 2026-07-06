"""Kalshi fee model.

Oct-2025 schedule: taker fee = ceil_cents(0.07 * C * P * (1-P)),
maker fee = ceil_cents(0.0175 * C * P * (1-P)), where C = contracts,
P = price in dollars. Some series carry a fee_multiplier. Settlement is
fee-free.
"""

from __future__ import annotations

import math

TAKER_RATE = 0.07
MAKER_RATE = 0.0175


def _quadratic_fee(rate: float, price: float, contracts: float, multiplier: float) -> float:
    if contracts <= 0:
        return 0.0
    raw = rate * multiplier * contracts * price * (1.0 - price)
    # Kalshi rounds up to the next cent
    return math.ceil(raw * 100.0) / 100.0


def taker_fee(price: float, contracts: float, multiplier: float = 1.0) -> float:
    """Fee in dollars for a taker fill at `price` (0-1) for `contracts`."""
    return _quadratic_fee(TAKER_RATE, price, contracts, multiplier)


def maker_fee(price: float, contracts: float, multiplier: float = 1.0) -> float:
    """Fee in dollars for a resting-order fill at `price` (0-1)."""
    return _quadratic_fee(MAKER_RATE, price, contracts, multiplier)


def round_trip_cost(price: float, contracts: float = 1.0, multiplier: float = 1.0) -> float:
    """Worst-case fee drag entering at `price` and exiting at the same price."""
    return 2 * taker_fee(price, contracts, multiplier)
