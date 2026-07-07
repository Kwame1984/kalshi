"""Panic-fade into a held pre-game favorite (Choi-Hui 2014 reflexivity trade).

Hypothesis: when a strong pre-game favorite (anchor mid >= 0.65 at 5-12h to
close) trades down into the 0.22-0.48 range in-play, retail overreaction has
pushed the price below the true win probability; buy YES and hold to
settlement.

Calibration (scripts/panic_fade_study.py, 4,053 anchored settled markets
across KXMLBGAME/KXWNBAGAME/KXATPMATCH/KXUFCFIGHT, hourly candles,
2026-07-07):

- Touch-based bands LOOK great but are hindsight: favorites whose in-play
  candle LOW landed in [0.20,0.30) settled 0.500 (n=104, CI 0.406-0.594)
  vs breakeven 0.273 at the low; [0.30,0.40) settled 0.482 vs be 0.377;
  [0.40,0.50) settled 0.604 vs be 0.478.  Conditioning on the low is
  look-ahead plus band survivorship (crash-through games are excluded).
- Honest resting-limit sim (fill whenever any in-play low <= L, entry = L,
  taker fee + 1.5c): L=0.25 edge +0.027 (n=311, settle 0.305, CI
  0.257-0.359 vs be 0.278); L=0.30 +0.008; L>=0.35 NEGATIVE (-0.017 to
  -0.061).  Any residual edge lives only at the deepest touches and is
  within noise.
- What this strategy can actually fill (hourly close ask, anchor >= 0.65,
  ask <= anchor-0.25, ask in [0.22,0.48]): settle 0.286 (n=98, CI
  0.206-0.382) vs avg ask 0.361 + fee -> edge -0.091.  Buying the fallen
  favorite once the panic PERSISTS to the top of the hour LOSES ~9c per
  contract; worst in ATP (-0.158, n=50).
- Mirror (dogs anchor <= 0.40 spiking, in-play HIGH in [0.55,0.70)):
  settle 0.424 vs touch 0.621 (n=210) — spikes overshoot at the extreme
  too, but the resting fade nets only +0.012 to +0.033 at H=0.60-0.65.

VERDICT: the overshoot exists only at intra-hour extremes you cannot
systematically capture as a taker; at fillable prices the fallen favorite is
fairly-to-over-priced (in-play favorite-longshot bias).  fair_value below is
the calibrated settlement estimate fair ~= 0.16 + 0.59*ask, which is BELOW
the ask across the whole band — so any executor gating on (fair - price)
will correctly refuse to trade this live.  Kept for backtest/documentation;
DO NOT promote to paper without tick-level evidence.
"""

from __future__ import annotations

from kalshi_bot.strategies.base import Context, MarketState, Signal, Strategy

SPORTS_PREFIXES = ("KXMLBGAME", "KXWNBAGAME", "KXATPMATCH", "KXUFCFIGHT")


class PanicFade(Strategy):
    name = "panic_fade"

    def __init__(
        self,
        anchor_min: float = 0.65,
        anchor_window: tuple[float, float] = (5.0, 12.0),
        anchor_max_spread: float = 0.06,
        min_drop: float = 0.25,
        band: tuple[float, float] = (0.22, 0.48),
        inplay_max_hours: float = 4.0,
        contracts: int = 12,
        max_concurrent: int = 3,
    ):
        self.anchor_min = anchor_min
        self.anchor_window = anchor_window
        self.anchor_max_spread = anchor_max_spread
        self.min_drop = min_drop
        self.band = band
        self.inplay_max_hours = inplay_max_hours
        self.contracts = contracts
        self.max_concurrent = max_concurrent
        self._anchors: dict[str, float] = {}  # ticker -> pre-game mid

    def _fair(self, ask: float) -> float:
        """Calibrated settlement rate of fallen favorites at price `ask`
        (linear fit to the resting-limit sim: 0.305@0.25 ... 0.422@0.45).
        Note: below `ask` everywhere in the band — measured edge is negative."""
        return max(0.0, min(1.0, 0.16 + 0.59 * ask))

    def generate_signals(self, state: MarketState, ctx: Context) -> list[Signal]:
        if not state.ticker.startswith(SPORTS_PREFIXES):
            return []
        htc = state.hours_to_close()
        if htc is None:
            return []
        bid, ask = state.yes_bid, state.yes_ask
        # pre-game: record/refresh the anchor, never trade
        if self.anchor_window[0] <= htc <= self.anchor_window[1]:
            if (
                bid is not None and ask is not None
                and (ask - bid) <= self.anchor_max_spread
            ):
                self._anchors[state.ticker] = (bid + ask) / 2.0
            return []
        if htc > self.inplay_max_hours or htc < 0:
            return []
        # in-play
        anchor = self._anchors.get(state.ticker)
        if anchor is None or anchor < self.anchor_min:
            return []
        if ask is None:
            return []
        if ctx.positions.get(state.ticker):
            return []
        if sum(1 for q in ctx.positions.values() if q) >= self.max_concurrent:
            return []
        # one position per event (each game has two mirrored markets)
        event = state.meta.get("event_ticker") or state.ticker.rsplit("-", 1)[0]
        if any(q and t.startswith(event + "-") for t, q in ctx.positions.items()):
            return []
        if ask > anchor - self.min_drop:
            return []
        if not (self.band[0] <= ask <= self.band[1]):
            return []
        return [Signal(
            ticker=state.ticker,
            side="yes",
            contracts=self.contracts,
            fair_value=self._fair(ask),
            limit_price=self.band[1],  # band top: take anything at/below it
            reason=(
                f"panic fade: anchor={anchor:.2f} ask={ask:.2f} "
                f"drop={anchor - ask:.2f} htc={htc:.1f}h"
            ),
            strategy=self.name,
        )]
