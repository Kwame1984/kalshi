"""Insider-signature volume-surge tailgating: buy YES at the ask when a quiet
Politics/Entertainment market prints an abnormal one-sided volume surge on the
cheap side (3-35c) with a rising price — the hypothesized footprint of
informed flow.

MEASURED (scripts/insider_surge_study.py; 14,260 settled markets, 2.06M hourly
candles; flag = hourly volume z>=4 vs trailing 168h zero-filled baseline,
close move >= +3c, pre-surge price 3-35c, >6h to close; entry = next candle's
ask; first flag per market; Wilson 95% CIs):

    signal group (Politics/Entertainment, n=658):
        win 22.8% [19.8, 26.2] vs entry 26.8c  -> lift -4.0c, EV -5.6c net
    control group (Econ/Weather, n=897):
        win 32.4% [29.5, 35.6] vs entry 32.6c  -> lift -0.2c (clean, no lift)
    sports (n=67): lift +3.6c, CI wide.

    Strongest post-hoc cell (z>=8, surge>=1,000 contracts, move>=+5c),
    signal group n=40: win 37.5% [24.2, 53.0] vs entry 27.9c -> +9.6c lift,
    NOT significant (CI includes the entry price), and Sports shows the same
    bump (+12.6c, n=20) — consistent with a generic big-surge effect or a
    multiple-comparisons artifact, not private information.

    Trade-tape refinement (64 recent flags with tape): >=80% one-sided
    yes-taker flow in the surge hour (n=52) wins 19.2% [10.8, 31.9] vs entry
    24.6c -> lift -5.4c. The "insider signature" makes results WORSE, the
    opposite of the hypothesis.

VERDICT: the hypothesis FAILED validation. Surge-chasing in these event
series is adversely selected (you buy the overshoot). Defaults below are
pinned to the strongest studied cell, but the measured evidence does not
support deploying this strategy with real money; it exists for paper-mode
observation only.

Mechanics: the live engine passes MarketState.volume as the CUMULATIVE market
volume each ~60s cycle. The strategy tracks per-ticker volume deltas between
calls, accumulates them into hourly buckets (missed hours backfilled as
zeros), and compares the in-progress bucket to a trailing 168-bucket
baseline. In backtests, feed hourly candles with per-candle volume converted
to a cumulative series (see insider_surge_study.py backtest) so the deltas
reproduce per-hour volume exactly.
"""

from __future__ import annotations

import math
from collections import deque
from dataclasses import dataclass, field

from kalshi_bot.strategies.base import Context, MarketState, Signal, Strategy

# Event classes where private information could plausibly exist.
INSIDER_PREFIXES = (
    "KXAPRPOTUS", "KXTRUTHSOCIAL", "KXNETFLIXRANKMOVIE", "KXALBUMEQUIV",
)

_BUCKET_S = 3600.0


@dataclass
class _Track:
    last_vol: float
    bucket_start: float
    bucket_vol: float = 0.0
    hist: deque = field(default_factory=lambda: deque(maxlen=168))
    hsum: float = 0.0
    hsq: float = 0.0
    prev_bucket_price: float | None = None
    last_price: float | None = None

    def push(self, v: float) -> None:
        if len(self.hist) == self.hist.maxlen:
            old = self.hist[0]
            self.hsum -= old
            self.hsq -= old * old
        self.hist.append(v)
        self.hsum += v
        self.hsq += v * v

    def zscore(self, v: float, std_floor: float) -> float | None:
        n = len(self.hist)
        if n == 0:
            return None
        mean = self.hsum / n
        var = max(0.0, self.hsq / n - mean * mean)
        return (v - mean) / max(math.sqrt(var), std_floor)


class InsiderSurge(Strategy):
    name = "insider_surge"

    def __init__(
        self,
        z_min: float = 8.0,             # strongest studied cell; z>=4 was -4c EV
        min_surge_vol: float = 1000.0,  # contracts in the surge hour
        min_move: float = 0.05,         # price rise vs previous hourly bucket
        band_lo: float = 0.03,          # pre-surge price band (cheap side)
        band_hi: float = 0.35,
        max_entry_ask: float = 0.60,    # don't chase a surge past this
        min_hours_to_close: float = 6.0,
        min_history_h: int = 24,        # trailing buckets before we can flag
        std_floor: float = 1.0,         # contracts; avoids z blowups on dead books
        assumed_win: float = 0.33,      # measured 37.5% on n=40; assume less
        contracts: int = 12,
        max_concurrent: int = 3,
    ):
        self.z_min = z_min
        self.min_surge_vol = min_surge_vol
        self.min_move = min_move
        self.band_lo = band_lo
        self.band_hi = band_hi
        self.max_entry_ask = max_entry_ask
        self.min_hours_to_close = min_hours_to_close
        self.min_history_h = min_history_h
        self.std_floor = std_floor
        self.assumed_win = assumed_win
        self.contracts = contracts
        self.max_concurrent = max_concurrent
        self.allow_prefixes: tuple[str, ...] = INSIDER_PREFIXES
        self._track: dict[str, _Track] = {}
        self._fired: set[str] = set()   # one entry per market, like the study

    def generate_signals(self, state: MarketState, ctx: Context) -> list[Signal]:
        if not state.ticker.startswith(self.allow_prefixes):
            return []
        ts = state.ts.timestamp()
        price = state.mid if state.mid is not None else state.last

        st = self._track.get(state.ticker)
        if st is None:
            # first sight: cumulative volume includes unseen history — skip it
            self._track[state.ticker] = _Track(
                last_vol=state.volume, bucket_start=ts, last_price=price,
            )
            return []

        dv = state.volume - st.last_vol
        if dv < 0:  # feed reset / non-cumulative input; drop the delta
            dv = 0.0
        st.last_vol = state.volume

        elapsed = int((ts - st.bucket_start) // _BUCKET_S)
        if elapsed >= 1:
            st.push(st.bucket_vol)
            for _ in range(elapsed - 1):     # fully-missed hours were quiet
                st.push(0.0)
            st.bucket_start += _BUCKET_S * elapsed
            st.bucket_vol = 0.0
            st.prev_bucket_price = st.last_price
        st.bucket_vol += dv
        prev_price = st.prev_bucket_price
        st.last_price = price if price is not None else st.last_price

        # ---- gates -----------------------------------------------------------
        if state.ticker in self._fired or ctx.positions.get(state.ticker):
            return []
        if len(st.hist) < self.min_history_h:
            return []
        htc = state.hours_to_close()
        if htc is None or htc <= self.min_hours_to_close:
            return []
        ask = state.yes_ask
        if ask is None or price is None or prev_price is None:
            return []
        if not (self.band_lo <= prev_price <= self.band_hi):
            return []
        if price - prev_price < self.min_move:          # price must be rising
            return []
        if ask > self.max_entry_ask:
            return []
        if st.bucket_vol < self.min_surge_vol:
            return []
        z = st.zscore(st.bucket_vol, self.std_floor)
        if z is None or z < self.z_min:
            return []
        if sum(1 for q in ctx.positions.values() if q) >= self.max_concurrent:
            return []
        event = state.meta.get("event_ticker") or state.ticker.rsplit("-", 1)[0]
        if any(q and t.startswith(str(event)) for t, q in ctx.positions.items()):
            return []

        self._fired.add(state.ticker)
        return [Signal(
            ticker=state.ticker, side="yes", contracts=self.contracts,
            fair_value=self.assumed_win,
            limit_price=None,  # taker: speed over fee for this signal
            reason=(f"vol surge z={z:.1f} dv={st.bucket_vol:.0f} "
                    f"px {prev_price:.2f}->{price:.2f} htc={htc:.0f}h"),
            strategy=self.name,
        )]
