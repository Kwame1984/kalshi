"""Deribit BTC option chains -> risk-neutral bracket probabilities.

Public REST only (no auth): one /public/get_book_summary_by_currency call
returns mark_iv for every listed option, which is Deribit's own fitted mid
smile — far more robust than inverting noisy top-of-book quotes ourselves.

Pipeline:
  chains  = await fetch_option_chains()          # expiry -> OptionChain
  smile   = target_smile(chains, target_dt, spot)  # time-interp to Kalshi close
  p       = smile.bracket_prob(low, high)

Kalshi BTC brackets settle at 21:00 UTC while Deribit expiries sit at 08:00
UTC, so there is never an exact expiry match. We interpolate TOTAL VARIANCE
linearly in time at fixed log-moneyness between the two bracketing expiries
(standard calendar interpolation); if the target falls before the first or
after the last expiry we scale the nearest smile's total variance by t
(constant-IV extrapolation). Digitals come from -dC/dK on the interpolated
smile (central difference with strike-dependent vol, so the skew correction
to N(d2) is included), which is Breeden-Litzenberger one integral up.

Caveat for consumers: risk-neutral densities overweight tails (variance risk
premium), so tail-bracket probabilities here are biased HIGH relative to the
physical measure. Strategies must demand extra edge before buying tails on
the strength of these numbers.
"""

from __future__ import annotations

import datetime as dt
import math
from dataclasses import dataclass, field

import httpx

DERIBIT_BASE = "https://www.deribit.com/api/v2"
YEAR_SECONDS = 365.0 * 86400.0  # crypto trades 24/7
_MONTHS = {
    "JAN": 1, "FEB": 2, "MAR": 3, "APR": 4, "MAY": 5, "JUN": 6,
    "JUL": 7, "AUG": 8, "SEP": 9, "OCT": 10, "NOV": 11, "DEC": 12,
}


# ---------------------------------------------------------------------------
# math helpers (no scipy in this venv)
# ---------------------------------------------------------------------------

def _norm_cdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def black_call(forward: float, strike: float, vol: float, t: float) -> float:
    """Undiscounted Black-76 call (r=0; Deribit settles in BTC, drift ~0)."""
    if t <= 0 or vol <= 0:
        return max(forward - strike, 0.0)
    sd = vol * math.sqrt(t)
    d1 = (math.log(forward / strike) + 0.5 * sd * sd) / sd
    return forward * _norm_cdf(d1) - strike * _norm_cdf(d1 - sd)


class MonotoneCubic:
    """Fritsch-Carlson monotone cubic (PCHIP) with flat extrapolation.

    Monotone-in-segment interpolation avoids the spline overshoot that turns
    a discretely sampled smile into a negative-density generator.
    """

    def __init__(self, xs: list[float], ys: list[float]):
        if len(xs) < 2:
            raise ValueError("need >= 2 knots")
        self.xs, self.ys = list(xs), list(ys)
        n = len(xs)
        h = [xs[i + 1] - xs[i] for i in range(n - 1)]
        d = [(ys[i + 1] - ys[i]) / h[i] for i in range(n - 1)]
        m = [0.0] * n
        m[0], m[-1] = d[0], d[-1]
        for i in range(1, n - 1):
            if d[i - 1] * d[i] <= 0:
                m[i] = 0.0
            else:  # weighted harmonic mean keeps monotonicity
                w1, w2 = 2 * h[i] + h[i - 1], h[i] + 2 * h[i - 1]
                m[i] = (w1 + w2) / (w1 / d[i - 1] + w2 / d[i])
        self.h, self.d, self.m = h, d, m

    def __call__(self, x: float) -> float:
        xs, ys, m = self.xs, self.ys, self.m
        if x <= xs[0]:
            return ys[0]  # flat extrapolation
        if x >= xs[-1]:
            return ys[-1]
        lo, hi = 0, len(xs) - 1
        while hi - lo > 1:
            mid = (lo + hi) // 2
            if xs[mid] <= x:
                lo = mid
            else:
                hi = mid
        h = self.h[lo]
        s = (x - xs[lo]) / h
        h00 = (1 + 2 * s) * (1 - s) ** 2
        h10 = s * (1 - s) ** 2
        h01 = s * s * (3 - 2 * s)
        h11 = s * s * (s - 1)
        return (h00 * ys[lo] + h10 * h * m[lo]
                + h01 * ys[lo + 1] + h11 * h * m[lo + 1])


# ---------------------------------------------------------------------------
# chain / smile objects
# ---------------------------------------------------------------------------

@dataclass
class OptionChain:
    """One Deribit expiry: mark-IV smile snapshot."""

    expiry: dt.datetime
    t: float                       # years from snapshot to expiry
    forward: float                 # Deribit underlying_price for the expiry
    strikes: list[float] = field(default_factory=list)
    ivs: list[float] = field(default_factory=list)  # decimals, e.g. 0.63


@dataclass
class Smile:
    """IV smile pinned to one horizon; prices digitals and brackets."""

    t: float
    forward: float
    _w: MonotoneCubic              # total variance vs log-moneyness

    def total_variance(self, strike: float) -> float:
        return max(self._w(math.log(strike / self.forward)), 1e-12)

    def iv(self, strike: float) -> float:
        return math.sqrt(self.total_variance(strike) / self.t)

    def call(self, strike: float) -> float:
        return black_call(self.forward, strike, self.iv(strike), self.t)

    def sigma_usd(self) -> float:
        """One ATM standard deviation in dollars at this horizon."""
        return self.forward * math.sqrt(self.total_variance(self.forward))

    def prob_above(self, strike: float) -> float:
        """Risk-neutral P(S_T > K) = -dC/dK, skew-adjusted digital."""
        h = max(strike * 5e-4, 0.5)
        p = (self.call(strike - h) - self.call(strike + h)) / (2 * h)
        return min(max(p, 0.0), 1.0)

    def bracket_prob(self, low: float | None, high: float | None) -> float:
        """P(low < S_T <= high); low/high None -> open-ended tail."""
        p_lo = self.prob_above(low) if low is not None else 1.0
        p_hi = self.prob_above(high) if high is not None else 0.0
        return min(max(p_lo - p_hi, 0.0), 1.0)


# ---------------------------------------------------------------------------
# fetch
# ---------------------------------------------------------------------------

def parse_instrument(name: str) -> tuple[dt.datetime, float, str] | None:
    """'BTC-8JUL26-59000-P' -> (expiry 08:00 UTC, strike, 'P')."""
    parts = name.split("-")
    if len(parts) != 4:
        return None
    try:
        ds = parts[1]
        day = int(ds[:-5])
        mon = _MONTHS[ds[-5:-2]]
        year = 2000 + int(ds[-2:])
        expiry = dt.datetime(year, mon, day, 8, 0, tzinfo=dt.timezone.utc)
        return expiry, float(parts[2]), parts[3]
    except (ValueError, KeyError):
        return None


async def fetch_index_price(index_name: str = "btc_usd") -> float:
    async with httpx.AsyncClient(timeout=30, base_url=DERIBIT_BASE) as client:
        r = await client.get("/public/get_index_price",
                             params={"index_name": index_name})
        r.raise_for_status()
        return float(r.json()["result"]["index_price"])


async def fetch_option_chains(
    currency: str = "BTC",
    min_strikes: int = 5,
) -> dict[dt.datetime, OptionChain]:
    """Snapshot every listed expiry's mark-IV smile in one API call."""
    async with httpx.AsyncClient(timeout=60, base_url=DERIBIT_BASE) as client:
        r = await client.get(
            "/public/get_book_summary_by_currency",
            params={"currency": currency, "kind": "option"},
        )
        r.raise_for_status()
        rows = r.json()["result"]

    now = dt.datetime.now(dt.timezone.utc)
    # (expiry, strike) -> list of (iv, underlying)
    grouped: dict[dt.datetime, dict[float, list[tuple[float, float]]]] = {}
    for row in rows:
        parsed = parse_instrument(row.get("instrument_name", ""))
        iv = row.get("mark_iv")
        und = row.get("underlying_price")
        if not parsed or not iv or not und or iv <= 0:
            continue
        expiry, strike, _ = parsed
        grouped.setdefault(expiry, {}).setdefault(strike, []).append(
            (float(iv) / 100.0, float(und))
        )

    chains: dict[dt.datetime, OptionChain] = {}
    for expiry, by_strike in grouped.items():
        t = (expiry - now).total_seconds() / YEAR_SECONDS
        if t <= 0 or len(by_strike) < min_strikes:
            continue
        unds = [u for vals in by_strike.values() for _, u in vals]
        forward = sorted(unds)[len(unds) // 2]  # median underlying_price
        strikes = sorted(by_strike)
        ivs = [sum(v for v, _ in by_strike[k]) / len(by_strike[k])
               for k in strikes]  # avg call/put mark IV (they match closely)
        chains[expiry] = OptionChain(expiry, t, forward, strikes, ivs)
    return chains


# ---------------------------------------------------------------------------
# smile construction + time interpolation
# ---------------------------------------------------------------------------

def smile_from_chain(chain: OptionChain, t_target: float | None = None) -> Smile:
    """Smile at the chain's own expiry, or constant-IV rescaled to t_target."""
    t = chain.t if t_target is None else t_target
    scale = t / chain.t
    ks = [math.log(k / chain.forward) for k in chain.strikes]
    ws = [iv * iv * chain.t * scale for iv in chain.ivs]
    return Smile(t=t, forward=chain.forward, _w=MonotoneCubic(ks, ws))


def interpolated_smile(
    near: OptionChain, far: OptionChain, t_target: float, spot: float
) -> Smile:
    """Linear total-variance interpolation in time at fixed log-moneyness."""
    if not near.t < t_target < far.t:
        raise ValueError("t_target must lie between the two expiries")
    a = (t_target - near.t) / (far.t - near.t)
    fwd = math.exp((1 - a) * math.log(near.forward) + a * math.log(far.forward))
    s_near, s_far = smile_from_chain(near), smile_from_chain(far)
    # sample on a moneyness grid covering both chains
    ks = sorted({round(math.log(k / fwd), 6)
                 for c in (near, far) for k in c.strikes})
    floor_w = 1e-8
    ws = []
    for k in ks:
        strike = fwd * math.exp(k)
        w = (1 - a) * s_near.total_variance(strike) \
            + a * s_far.total_variance(strike)
        # calendar-arb guard: total variance must grow with t
        ws.append(max(w, s_near.total_variance(strike), floor_w))
    return Smile(t=t_target, forward=fwd, _w=MonotoneCubic(ks, ws))


def target_smile(
    chains: dict[dt.datetime, OptionChain],
    target: dt.datetime,
    spot: float,
) -> Smile:
    """Smile at an arbitrary target datetime (e.g. Kalshi 21:00 UTC close).

    Uses the two bracketing Deribit expiries when they exist; otherwise
    rescales the nearest chain at constant IV.
    """
    if not chains:
        raise ValueError("no option chains")
    now = dt.datetime.now(dt.timezone.utc)
    t_target = (target - now).total_seconds() / YEAR_SECONDS
    if t_target <= 0:
        raise ValueError(f"target {target} is in the past")
    ordered = sorted(chains.values(), key=lambda c: c.t)
    before = [c for c in ordered if c.t <= t_target]
    after = [c for c in ordered if c.t > t_target]
    if before and after:
        return interpolated_smile(before[-1], after[0], t_target, spot)
    nearest = after[0] if after else before[-1]
    return smile_from_chain(nearest, t_target=t_target)


def rnd_bracket_prob(
    chain: OptionChain | Smile,
    low: float | None,
    high: float | None,
    spot: float,
    t_expiry: float,
) -> float:
    """Risk-neutral P(low < S_T <= high) at horizon t_expiry (years).

    `chain` may be a single OptionChain (its smile is constant-IV rescaled
    to t_expiry) or a prebuilt Smile (e.g. from target_smile) whose horizon
    already matches. `spot` is only a sanity anchor for degenerate inputs.
    """
    if isinstance(chain, Smile):
        smile = chain
    else:
        smile = smile_from_chain(chain, t_target=t_expiry)
    if smile.forward <= 0 and spot > 0:  # defensive; never expected
        smile = Smile(t=smile.t, forward=spot, _w=smile._w)
    return smile.bracket_prob(low, high)
