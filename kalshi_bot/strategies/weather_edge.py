"""Weather model edge: calibrated forecast probabilities vs market prices.

For daily-high bracket markets, convert the archived/live forecast into a
probability using a normal error model with PER-STATION bias and sigma
measured against official NWS CLI settlement values (see
data/external/calibrate_weather.py). Trade only the single best-edge bracket
per event, at prices outside the fee death zone.

Key empirical facts baked in (calibration 2026-03..2026-07):
- station biases run 0.6-1.8F — uncorrected they generate false edges;
- sigma roughly doubles from lead 0 to lead 2 and varies 3x across stations;
- weather favorites are slightly OVERpriced on Kalshi (Le 2026), so the
  model requires a larger edge to buy the favorite side than the dog side.
"""

from __future__ import annotations

import datetime as dt
import json
import math
import re
from pathlib import Path

from kalshi_bot.backtest.fees import taker_fee
from kalshi_bot.config import PROJECT_ROOT
from kalshi_bot.strategies.base import Context, MarketState, Signal, Strategy

CALIBRATION_PATH = PROJECT_ROOT / "data" / "weather_calibration.json"

# fallback when a station has no calibration yet
DEFAULT_SIGMA_BY_LEAD = {0: 2.0, 1: 2.8, 2: 4.2, 3: 4.8, 4: 5.5}

_TICKER_DATE = re.compile(r"-(\d{2})([A-Z]{3})(\d{2})-")
_MONTHS = {m: i + 1 for i, m in enumerate(
    ["JAN", "FEB", "MAR", "APR", "MAY", "JUN", "JUL", "AUG", "SEP", "OCT", "NOV", "DEC"]
)}


def ticker_target_date(ticker: str) -> dt.date | None:
    """KXHIGHNY-26JUL07-B75.5 -> 2026-07-07"""
    m = _TICKER_DATE.search(ticker)
    if not m:
        return None
    yy, mon, dd = m.groups()
    return dt.date(2000 + int(yy), _MONTHS[mon], int(dd))


def strike_bounds(meta: dict) -> tuple[float | None, float | None]:
    """Continuous (low, high) bounds of the YES outcome.

    Kalshi reports integer temperatures; strike fields are integer-inclusive:
    - between, floor=88 cap=89 = "88 to 89"  -> T in [87.5, 89.5)
    - greater, floor=91        = "92 or above" -> T >= 91.5
    - less,    cap=84          = "83 or below" -> T < 83.5
    (verified against settled markets' yes_sub_title fields)
    """
    st = meta.get("strike_type")
    floor, cap = meta.get("floor_strike"), meta.get("cap_strike")
    if st == "between" and floor is not None and cap is not None:
        return floor - 0.5, cap + 0.5
    if st == "greater" and floor is not None:
        return floor + 0.5, None
    if st == "less" and cap is not None:
        return None, cap - 0.5
    return None, None


def prob_in_range(
    mean: float, sigma: float, low: float | None, high: float | None
) -> float:
    def cdf(x: float) -> float:
        return 0.5 * (1 + math.erf((x - mean) / (sigma * math.sqrt(2))))

    lo = cdf(low) if low is not None else 0.0
    hi = cdf(high) if high is not None else 1.0
    return max(0.0, hi - lo)


class WeatherEdge(Strategy):
    name = "weather_edge"

    def __init__(
        self,
        min_net_edge: float = 0.08,
        favorite_extra_edge: float = 0.04,  # extra hurdle to buy sides priced > 0.5
        max_price: float = 0.85,
        min_price: float = 0.12,            # fee death zone floor
        contracts: int = 20,
        max_lead: int = 2,
        sigma_inflation: float = 1.10,
        calibration_path: Path | None = None,
    ):
        self.min_net_edge = min_net_edge
        self.favorite_extra_edge = favorite_extra_edge
        self.max_price = max_price
        self.min_price = min_price
        self.contracts = contracts
        self.max_lead = max_lead
        self.sigma_inflation = sigma_inflation
        path = calibration_path or CALIBRATION_PATH
        self.calibration: dict = {}
        if path.exists():
            self.calibration = json.loads(path.read_text())

    def _model_params(self, series: str, lead: int) -> tuple[float, float]:
        """(bias, sigma) for this station/lead."""
        cal = self.calibration.get(series, {})
        bias = cal.get("bias", {}).get(str(lead), 0.0)
        sigma = cal.get("sigma", {}).get(str(lead)) or DEFAULT_SIGMA_BY_LEAD.get(lead, 5.0)
        return bias, sigma * self.sigma_inflation

    def generate_signals(self, state: MarketState, ctx: Context) -> list[Signal]:
        series = state.meta.get("series_ticker", "")
        if not series.startswith("KXHIGH"):
            return []
        # one position per event: skip if we already hold any bracket of it
        event = state.meta.get("event_ticker") or ""
        if any(
            qty and t.startswith(event) for t, qty in ctx.positions.items()
        ) or ctx.positions.get(state.ticker):
            return []
        target = ticker_target_date(state.ticker)
        if target is None:
            return []
        lead = max(0, (target - state.ts.date()).days)
        if lead > self.max_lead:
            return []
        fc = ctx.get_external("open_meteo", f"{series}.high_f", target.isoformat())
        if not isinstance(fc, dict):
            return []
        forecast_high = fc.get(f"high_f_lead_{lead}")
        if forecast_high is None:
            return []
        bias, sigma = self._model_params(series, lead)
        mean = forecast_high + bias
        low, high = strike_bounds(state.meta)
        if low is None and high is None:
            return []
        p_model = prob_in_range(mean, sigma, low, high)

        candidates: list[tuple[float, Signal]] = []
        if state.yes_ask is not None and self.min_price <= state.yes_ask <= self.max_price:
            hurdle = self.min_net_edge + (
                self.favorite_extra_edge if state.yes_ask > 0.5 else 0.0
            )
            edge = p_model - state.yes_ask - taker_fee(state.yes_ask, 1)
            if edge >= hurdle:
                candidates.append((edge, Signal(
                    ticker=state.ticker, side="yes", contracts=self.contracts,
                    fair_value=p_model,
                    reason=f"model={p_model:.2f} ask={state.yes_ask:.2f} "
                           f"mean={mean:.1f} sig={sigma:.1f} lead={lead}",
                    strategy=self.name,
                )))
        if state.yes_bid is not None:
            no_price = 1 - state.yes_bid
            if self.min_price <= no_price <= self.max_price:
                hurdle = self.min_net_edge + (
                    self.favorite_extra_edge if no_price > 0.5 else 0.0
                )
                edge = (1 - p_model) - no_price - taker_fee(no_price, 1)
                if edge >= hurdle:
                    candidates.append((edge, Signal(
                        ticker=state.ticker, side="no", contracts=self.contracts,
                        fair_value=p_model,
                        reason=f"model={p_model:.2f} no@{no_price:.2f} "
                               f"mean={mean:.1f} sig={sigma:.1f} lead={lead}",
                        strategy=self.name,
                    )))
        if not candidates:
            return []
        candidates.sort(key=lambda c: -c[0])
        return [candidates[0][1]]
