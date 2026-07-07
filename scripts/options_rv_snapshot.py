"""Live options-RV snapshot: Kalshi BTC brackets vs Deribit-implied probs.

Loop-once integration proof for the options_rv strategy:
1. find open Kalshi BTC bracket events (KXBTC range; KXBTCD above/below);
2. pull top-of-book for every bracket with non-trivial mass;
3. one Deribit book-summary call -> mark-IV chains; build a smile at each
   event's 21:00 UTC close via total-variance time interpolation between the
   two bracketing 08:00 UTC Deribit expiries;
4. print kalshi bid/ask vs deribit prob with net-of-taker-fee edge per side,
   plus what OptionsRV would actually fire on;
5. persist probs to external_signals (source='deribit_rnd', key=event
   ticker, target_date=close date, raw JSON = {market_ticker: prob, ...}).

Usage: .venv/bin/python -m scripts.options_rv_snapshot [--series KXBTC ...]
"""

from __future__ import annotations

import argparse
import asyncio
import datetime as dt
import json

from kalshi_bot.api.client import KalshiClient
from kalshi_bot.backtest.fees import taker_fee
from kalshi_bot.data import store
from kalshi_bot.data.external import deribit
from kalshi_bot.strategies.base import Context, MarketState
from kalshi_bot.strategies.options_rv import SOURCE, OptionsRV

DEFAULT_SERIES = ["KXBTC", "KXBTCD"]
MIN_MASS = 0.002  # hide brackets where both kalshi and deribit see ~zero


def best_levels(orderbook: dict) -> tuple[float | None, float | None]:
    """(yes_bid, yes_ask) from orderbook_fp dollars; books list bids only."""
    ob = orderbook.get("orderbook_fp") or {}
    yes = ob.get("yes_dollars") or []
    no = ob.get("no_dollars") or []
    yes_bid = float(yes[-1][0]) if yes else None
    yes_ask = 1.0 - float(no[-1][0]) if no else None
    return yes_bid, yes_ask


def bracket_bounds(m: dict) -> tuple[float | None, float | None]:
    """(low, high) for P(low < S <= high); None = open tail."""
    st = m.get("strike_type")
    if st == "between":
        return m.get("floor_strike"), m.get("cap_strike")
    if st == "greater":
        return m.get("floor_strike"), None
    if st == "less":
        return None, m.get("cap_strike")
    return None, None


async def snapshot_event(
    kalshi: KalshiClient,
    event_ticker: str,
    markets: list[dict],
    chains: dict[dt.datetime, deribit.OptionChain],
    spot: float,
    strategy: OptionsRV,
) -> tuple[dict, list[str]]:
    close = dt.datetime.fromisoformat(
        markets[0]["close_time"].replace("Z", "+00:00")
    )
    smile = deribit.target_smile(chains, close, spot)
    ordered = sorted(chains.values(), key=lambda c: c.t)
    below = [c for c in ordered if c.t <= smile.t]
    above = [c for c in ordered if c.t > smile.t]
    expiry_note = (
        f"interp {below[-1].expiry:%d%b %H:%M} <-> {above[0].expiry:%d%b %H:%M} UTC"
        if below and above
        else f"const-IV rescale of {(above or below)[0 if above else -1].expiry:%d%b}"
    )
    sigma = smile.sigma_usd()

    probs: dict[str, float] = {}
    rows = []
    for m in sorted(markets, key=lambda x: x.get("floor_strike") or -1):
        low, high = bracket_bounds(m)
        if low is None and high is None:
            continue
        p = smile.bracket_prob(low, high)
        probs[m["ticker"]] = round(p, 5)
        bid = store.dollars(m.get("yes_bid_dollars"))
        ask = store.dollars(m.get("yes_ask_dollars"))
        # hide brackets both sides agree are ~0 or ~1 (dead tails / deep ITM)
        alive_rnd = MIN_MASS <= p <= 1 - MIN_MASS
        alive_kalshi = (bid is not None and ask is not None
                        and bid >= MIN_MASS and ask <= 1 - MIN_MASS)
        if not (alive_rnd or alive_kalshi):
            continue
        # refresh top-of-book from the live orderbook for displayed rows
        try:
            ob = await kalshi.get_orderbook(m["ticker"], depth=1)
            ob_bid, ob_ask = best_levels(ob)
            bid = ob_bid if ob_bid is not None else bid
            ask = ob_ask if ob_ask is not None else ask
        except Exception:
            pass  # markets-endpoint quotes are still top-of-book
        yes_edge = (p - ask - taker_fee(ask, 1)) if ask else None
        no_edge = ((1 - p) - (1 - bid) - taker_fee(1 - bid, 1)) if bid else None
        rows.append((m, low, high, p, bid, ask, yes_edge, no_edge))

    # what would the strategy fire on?
    ctx = Context(signals={(SOURCE, event_ticker, close.date().isoformat()): {
        **probs, "_forward": smile.forward, "_sigma_usd": sigma,
    }})
    now = dt.datetime.now(dt.timezone.utc)
    fired: dict[str, str] = {}
    for m, low, high, p, bid, ask, *_ in rows:
        state = MarketState(
            ticker=m["ticker"], ts=now, yes_bid=bid, yes_ask=ask,
            last=store.dollars(m.get("last_price_dollars")),
            meta={"event_ticker": event_ticker, "close_time": m["close_time"],
                  "floor_strike": m.get("floor_strike"),
                  "cap_strike": m.get("cap_strike"),
                  "series_ticker": event_ticker.split("-")[0]},
        )
        for sig in strategy.generate_signals(state, ctx):
            fired[m["ticker"]] = f"{sig.side.upper()} <- {sig.reason}"

    # ---- print table -------------------------------------------------------
    print(f"\n=== {event_ticker}  close {close:%Y-%m-%d %H:%M} UTC "
          f"({(close - now).total_seconds() / 3600:.1f}h) ===")
    print(f"    deribit spot {spot:,.0f} | fwd@close {smile.forward:,.0f} | "
          f"1sd {sigma:,.0f} USD | {expiry_note}")
    hdr = (f"    {'bracket':>22} {'k.bid':>6} {'k.ask':>6} {'drbt.p':>7} "
           f"{'yesEdge':>8} {'noEdge':>8}  signal")
    print(hdr)
    for m, low, high, p, bid, ask, ye, ne in rows:
        name = (f"{low:,.0f}-{high:,.0f}" if low is not None and high is not None
                else f">{low:,.0f}" if low is not None else f"<{high:,.0f}")
        fmt = lambda v, w=6: f"{v:>{w}.3f}" if v is not None else " " * (w - 3) + "  -"
        print(f"    {name:>22} {fmt(bid)} {fmt(ask)} {p:>7.4f} "
              f"{fmt(ye, 8)} {fmt(ne, 8)}  {fired.get(m['ticker'], '')}")
    fired_lines = [f"{event_ticker}: {t} {v}" for t, v in fired.items()]

    # raw JSON is exactly the dict OptionsRV expects at
    # ctx.external[("deribit_rnd", event_ticker, target_date)]:
    # {market_ticker: prob, ...} plus underscore-prefixed metadata.
    payload = {**probs,
               "_forward": round(smile.forward, 2),
               "_sigma_usd": round(sigma, 2),
               "_spot": spot,
               "_expiry_note": expiry_note,
               "_close_time": markets[0]["close_time"]}
    return {"event_ticker": event_ticker,
            "target_date": close.date().isoformat(),
            "payload": payload}, fired_lines


def persist(snapshots: list[dict], spot: float) -> None:
    """Short WAL-friendly write: one INSERT batch, immediate commit."""
    issued = dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")
    conn = store.connect()
    try:
        conn.executemany(
            "INSERT OR REPLACE INTO external_signals "
            "(source, key, target_date, issued_at, value, raw) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            [(SOURCE, s["event_ticker"], s["target_date"], issued, spot,
              json.dumps(s["payload"])) for s in snapshots],
        )
        conn.commit()
    finally:
        conn.close()


async def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--series", nargs="*", default=DEFAULT_SERIES)
    args = ap.parse_args()

    kalshi = KalshiClient()
    try:
        chains, spot = await asyncio.gather(
            deribit.fetch_option_chains(), deribit.fetch_index_price()
        )
        print(f"deribit: {len(chains)} expiries, BTC index {spot:,.1f}")

        strategy = OptionsRV()
        snapshots, all_fired = [], []
        for series in args.series:
            resp = await kalshi.get_events(series_ticker=series, status="open",
                                           with_nested_markets=True)
            for ev in resp.get("events", []):
                markets = [m for m in ev.get("markets", [])
                           if m.get("status") == "active"]
                if not markets:
                    continue
                snap, fired = await snapshot_event(
                    kalshi, ev["event_ticker"], markets, chains, spot, strategy
                )
                snapshots.append(snap)
                all_fired.extend(fired)

        if snapshots:
            persist(snapshots, spot)
            print(f"\nstored {len(snapshots)} event snapshot(s) to "
                  f"external_signals (source={SOURCE})")
        print("\nsignals passing OptionsRV gates:"
              + ("\n  " + "\n  ".join(all_fired) if all_fired else " NONE"))
    finally:
        await kalshi.aclose()


if __name__ == "__main__":
    asyncio.run(main())
