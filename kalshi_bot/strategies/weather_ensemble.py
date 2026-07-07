"""Ensemble weather edge: day-specific forecast uncertainty vs market prices.

Hypothesis under test: real ensemble information (NCEP HGEFS/GEFS) prices
daily-high brackets better than the deterministic model's fixed per-station
sigma, especially on high-uncertainty days.

Data reality (see data/external/ensemble_backfill.py): the free Open-Meteo
stack does NOT archive raw ensemble members for past dates. What is archived
(previous-runs API, model ncep_hgefs025_ensemble_mean, ~Feb 2026 onward with
gaps) is the HGEFS ensemble MEAN and ensemble SPREAD per lead. So:

- live / recent dates: if raw member daily highs are stored
  (key '<SERIES>.high_f_members'), probability of a bracket = bias-corrected
  smoothed ECDF of member highs (Gaussian kernel, default 1.0F, so finite
  member counts never emit 0/1 probabilities);
- backtest / archive dates: probability = Normal(mean_high + bias, sigma_day)
  with sigma_day = beta_lead * spread_day -- i.e. the deterministic model's
  fixed sigma is replaced by the day's actual ensemble spread, scaled by a
  walk-forward underdispersion factor beta.

All calibration is walk-forward: per-station bias and the pooled beta are
recomputed from external_signals rows strictly BEFORE the decision date
(iem_cli actual highs vs archived ensemble means). Nothing is fit on future
data, and data/weather_calibration.json is deliberately NOT used here.

Trade gating mirrors WeatherEdge: single best-edge bracket per event,
min_net_edge after taker fees, fee death zone floor, extra hurdle on
favorites.
"""

from __future__ import annotations

import datetime as dt
import math

from kalshi_bot.backtest.fees import taker_fee
from kalshi_bot.strategies.base import Context, MarketState, Signal, Strategy
from kalshi_bot.strategies.weather_edge import (
    prob_in_range,
    strike_bounds,
    ticker_target_date,
)

SERIES_PREFIX = "KXHIGH"
MAX_CAL_LEAD = 2


class WeatherEnsemble(Strategy):
    name = "weather_ensemble"

    def __init__(
        self,
        min_net_edge: float = 0.08,
        favorite_extra_edge: float = 0.04,  # extra hurdle on sides priced > 0.5
        max_price: float = 0.85,
        min_price: float = 0.12,            # fee death zone floor
        contracts: int = 20,
        max_lead: int = 2,
        kernel_width: float = 1.0,   # F; ECDF smoothing for raw members
        beta_prior: float = 2.0,     # prior spread->sigma underdispersion factor
        beta_prior_n: int = 15,      # pseudo-observations behind the prior
        spread_floor: float = 0.5,   # F; avoid dividing/multiplying tiny spreads
        sigma_floor: float = 1.5,    # F; never claim less total uncertainty
        bias_shrink_n: int = 3,      # shrink per-station bias toward 0
        min_members: int = 10,
    ):
        self.min_net_edge = min_net_edge
        self.favorite_extra_edge = favorite_extra_edge
        self.max_price = max_price
        self.min_price = min_price
        self.contracts = contracts
        self.max_lead = max_lead
        self.kernel_width = kernel_width
        self.beta_prior = beta_prior
        self.beta_prior_n = beta_prior_n
        self.spread_floor = spread_floor
        self.sigma_floor = sigma_floor
        self.bias_shrink_n = bias_shrink_n
        self.min_members = min_members
        self._index_for: int | None = None  # id(ctx.external) the index was built on
        self._hist: dict = {}
        self._calib_cache: dict[dt.date, dict] = {}

    # ---------------------------------------------------------------- data
    def _build_index(self, ctx: Context) -> None:
        """One pass over ctx.external -> per-series history tables."""
        key = id(ctx.external)
        if self._index_for == key:
            return
        self._index_for = key
        self._calib_cache = {}
        hist: dict[str, dict] = {}

        def series_slot(series: str) -> dict:
            return hist.setdefault(
                series, {"actual": {}, "ens": {}, "det": {}}
            )

        for (source, k, target_date), payload in ctx.external.items():
            if "." not in k:
                continue
            series, field = k.split(".", 1)
            if not series.startswith(SERIES_PREFIX):
                continue
            if source == "iem_cli" and field == "actual_high":
                if isinstance(payload, (int, float)):
                    series_slot(series)["actual"][target_date] = float(payload)
            elif source == "open_meteo_ens" and field == "high_f_ens":
                if isinstance(payload, dict):
                    series_slot(series)["ens"][target_date] = payload
            elif source == "open_meteo" and field == "high_f":
                if isinstance(payload, dict):
                    series_slot(series)["det"][target_date] = payload
        self._hist = hist

    def _calibration(self, asof: dt.date) -> dict:
        """Walk-forward params from history strictly before `asof`.

        Returns {"bias_ens": {(series, lead): b}, "bias_det": {...},
                 "beta": {lead: beta}}.
        """
        cached = self._calib_cache.get(asof)
        if cached is not None:
            return cached
        asof_iso = asof.isoformat()
        bias_ens: dict[tuple[str, int], float] = {}
        bias_det: dict[tuple[str, int], float] = {}
        resid_sprd: dict[int, list[tuple[float, float]]] = {le: [] for le in range(MAX_CAL_LEAD + 1)}

        for series, tables in self._hist.items():
            actual = tables["actual"]
            for lead in range(MAX_CAL_LEAD + 1):
                # ensemble-mean bias (shrunk toward 0)
                errs = []
                for day, leads in tables["ens"].items():
                    if day >= asof_iso or day not in actual:
                        continue
                    entry = leads.get(f"lead_{lead}") or {}
                    m = entry.get("mean_high")
                    if m is not None:
                        errs.append((actual[day] - m, entry.get("spread")))
                b = sum(e for e, _ in errs) / (len(errs) + self.bias_shrink_n) if errs else 0.0
                bias_ens[(series, lead)] = b
                for e, s in errs:
                    if s is not None:
                        resid_sprd[lead].append((e - b, s))
                # deterministic blend bias (fallback mean source)
                errs_d = [
                    actual[day] - fc[f"high_f_lead_{lead}"]
                    for day, fc in tables["det"].items()
                    if day < asof_iso and day in actual
                    and fc.get(f"high_f_lead_{lead}") is not None
                ]
                bias_det[(series, lead)] = (
                    sum(errs_d) / (len(errs_d) + self.bias_shrink_n) if errs_d else 0.0
                )

        beta: dict[int, float] = {}
        for lead, pairs in resid_sprd.items():
            num = self.beta_prior_n * self.beta_prior ** 2
            den = self.beta_prior_n
            for resid, spread in pairs:
                sf = max(spread, self.spread_floor)
                num += (resid / sf) ** 2
                den += 1
            beta[lead] = math.sqrt(num / den)

        calib = {"bias_ens": bias_ens, "bias_det": bias_det, "beta": beta}
        self._calib_cache[asof] = calib
        return calib

    # --------------------------------------------------------------- model
    def model_probability(
        self,
        series: str,
        target: dt.date,
        lead: int,
        asof: dt.date,
        ctx: Context,
        low: float | None,
        high: float | None,
    ) -> float | None:
        """P(daily high in (low, high)) from ensemble data, or None if the
        day has no usable ensemble information."""
        self._build_index(ctx)
        calib = self._calibration(asof)
        cal_lead = min(lead, MAX_CAL_LEAD)
        bias = calib["bias_ens"].get((series, cal_lead), 0.0)

        # 1) raw members when stored (live path / recent dates)
        members_raw = ctx.get_external(
            "open_meteo_ens", f"{series}.high_f_members", target.isoformat()
        )
        if isinstance(members_raw, dict):
            members = members_raw.get(f"lead_{lead}")
            if isinstance(members, list) and len(members) >= self.min_members:
                h = self.kernel_width
                p = 0.0
                for m in members:
                    p += prob_in_range(m + bias, h, low, high)
                return p / len(members)

        # 2) archived ensemble mean + spread
        ens = ctx.get_external(
            "open_meteo_ens", f"{series}.high_f_ens", target.isoformat()
        )
        if not isinstance(ens, dict):
            return None
        entry = ens.get(f"lead_{lead}") or {}
        spread = entry.get("spread")
        if spread is None:
            return None  # no uncertainty info -> this strategy has no opinion
        mean = entry.get("mean_high")
        if mean is not None:
            mean += bias
        else:
            det = ctx.get_external("open_meteo", f"{series}.high_f", target.isoformat())
            if not isinstance(det, dict):
                return None
            det_high = det.get(f"high_f_lead_{lead}")
            if det_high is None:
                return None
            mean = det_high + calib["bias_det"].get((series, cal_lead), 0.0)
        beta = calib["beta"].get(cal_lead, self.beta_prior)
        sigma = max(self.sigma_floor, beta * max(spread, self.spread_floor))
        return prob_in_range(mean, sigma, low, high)

    # ------------------------------------------------------------- signals
    def generate_signals(self, state: MarketState, ctx: Context) -> list[Signal]:
        series = state.meta.get("series_ticker", "")
        if not series.startswith(SERIES_PREFIX):
            return []
        # one position per event
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
        low, high = strike_bounds(state.meta)
        if low is None and high is None:
            return []
        p_model = self.model_probability(
            series, target, lead, state.ts.date(), ctx, low, high
        )
        if p_model is None:
            return []

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
                    reason=f"ens={p_model:.2f} ask={state.yes_ask:.2f} lead={lead}",
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
                        reason=f"ens={p_model:.2f} no@{no_price:.2f} lead={lead}",
                        strategy=self.name,
                    )))
        if not candidates:
            return []
        candidates.sort(key=lambda c: -c[0])
        return [candidates[0][1]]
