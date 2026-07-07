"""Live structural-arbitrage scanner.

For each open event in the target series, pull real orderbooks and look for:
- buy-all-YES: sum of executable YES asks across an exhaustive bracket set
  < $1 minus total fees -> guaranteed profit at settlement;
- buy-all-NO: sum of executable YES bids > $1 plus fees (buy NO everywhere);
- monotonicity violations on threshold ladders (P(>a) < P(>b) for a < b).

Sizing is capped by the thinnest leg. Fees assume taker on every leg.

Usage: python -m scripts.scan_structural_arb [--series KXFED KXCPIYOY ...]
"""

from __future__ import annotations

import argparse
import asyncio
import datetime as dt

from kalshi_bot.api.client import KalshiClient
from kalshi_bot.backtest.fees import taker_fee
from kalshi_bot.data.collector import TARGET_SERIES


def best_levels(orderbook: dict) -> tuple[float | None, float, float | None, float]:
    """(yes_bid, bid_size, yes_ask, ask_size) from orderbook_fp dollars.

    Kalshi books list bids only: yes_dollars are YES bids, no_dollars are NO
    bids; best YES ask = 1 - best NO bid.
    """
    ob = orderbook.get("orderbook_fp") or {}
    yes = ob.get("yes_dollars") or []
    no = ob.get("no_dollars") or []
    yes_bid, bid_sz = (float(yes[-1][0]), float(yes[-1][1])) if yes else (None, 0.0)
    if no:
        no_bid, no_sz = float(no[-1][0]), float(no[-1][1])
        yes_ask, ask_sz = 1.0 - no_bid, no_sz
    else:
        yes_ask, ask_sz = None, 0.0
    return yes_bid, bid_sz, yes_ask, ask_sz


async def scan_event(client: KalshiClient, event: dict) -> list[str]:
    markets = [m for m in event.get("markets", []) if m.get("status") == "active"]
    if len(markets) < 3:
        return []
    books = {}
    for m in markets:
        try:
            books[m["ticker"]] = await client.get_orderbook(m["ticker"], depth=5)
        except Exception:
            return []  # can't price the full set -> no structural claim
    legs = {}
    for t, ob in books.items():
        yb, bs, ya, asz = best_levels(ob)
        legs[t] = {"bid": yb, "bid_sz": bs, "ask": ya, "ask_sz": asz}

    findings = []
    findings += _partition_arbs(event, markets, legs)
    findings += _monotonicity_arbs(event, markets, legs)
    return findings


def _is_exclusive_partition(markets: list[dict]) -> bool:
    """True only for a verified single-winner set: contiguous 'between'
    brackets plus exactly one 'less' and one 'greater' tail. Threshold
    ladders (all-'greater') are CUMULATIVE — many legs resolve YES — and
    must never be treated as a partition."""
    betweens = sorted(
        (m for m in markets if m.get("strike_type") == "between"),
        key=lambda m: m.get("floor_strike") or 0,
    )
    less = [m for m in markets if m.get("strike_type") == "less"]
    greater = [m for m in markets if m.get("strike_type") == "greater"]
    if len(betweens) < 2 or len(less) != 1 or len(greater) != 1:
        return False
    if len(betweens) + 2 != len(markets):
        return False
    # contiguity: each bracket starts one step after the previous ends,
    # tails meet the first/last brackets
    steps = []
    for a, b in zip(betweens, betweens[1:]):
        if a.get("cap_strike") is None or b.get("floor_strike") is None:
            return False
        steps.append(b["floor_strike"] - a["cap_strike"])
    if not steps or max(steps) != min(steps) or steps[0] <= 0:
        return False
    step = steps[0]
    if less[0].get("cap_strike") != betweens[0].get("floor_strike"):
        return False
    if greater[0].get("floor_strike") != betweens[-1].get("cap_strike"):
        return False
    return True


def _partition_arbs(event: dict, markets: list[dict], legs: dict) -> list[str]:
    if not _is_exclusive_partition(markets):
        return []
    findings = []
    asks = [(t, l["ask"], l["ask_sz"]) for t, l in legs.items()]
    bids = [(t, l["bid"], l["bid_sz"]) for t, l in legs.items()]
    if all(a is not None for _, a, _ in asks):
        sum_ask = sum(a for _, a, _ in asks)
        fees = sum(taker_fee(a, 1) for _, a, _ in asks)
        if sum_ask + fees < 0.995:
            size = min(s for _, _, s in asks)
            profit = (1.0 - sum_ask - fees) * size
            findings.append(
                f"BUY-ALL-YES {event['event_ticker']}: sum_ask={sum_ask:.3f} "
                f"fees={fees:.3f} size={size:.0f} profit=${profit:.2f}"
            )
    if all(b is not None for _, b, _ in bids):
        sum_bid = sum(b for _, b, _ in bids)
        fees = sum(taker_fee(1 - b, 1) for _, b, _ in bids)
        if sum_bid - fees > 1.005:
            size = min(s for _, _, s in bids)
            profit = (sum_bid - 1.0 - fees) * size
            findings.append(
                f"BUY-ALL-NO {event['event_ticker']}: sum_bid={sum_bid:.3f} "
                f"fees={fees:.3f} size={size:.0f} profit=${profit:.2f}"
            )
    return findings


def _monotonicity_arbs(event: dict, markets: list[dict], legs: dict) -> list[str]:
    """On a 'greater' ladder, P(>a) >= P(>b) for a < b. If ask(>a) < bid(>b),
    buy YES(>a) + buy NO(>b): worst-case payout $1 >= cost. Same logic
    mirrored for 'less' ladders."""
    findings = []
    ladder = sorted(
        (m for m in markets if m.get("strike_type") == "greater"
         and m.get("floor_strike") is not None),
        key=lambda m: m["floor_strike"],
    )
    for lo, hi in zip(ladder, ladder[1:]):
        a = legs[lo["ticker"]]["ask"]
        b = legs[hi["ticker"]]["bid"]
        if a is None or b is None:
            continue
        fees = taker_fee(a, 1) + taker_fee(1 - b, 1)
        if b - a - fees > 0.005:
            size = min(legs[lo["ticker"]]["ask_sz"], legs[hi["ticker"]]["bid_sz"])
            findings.append(
                f"MONO {event['event_ticker']}: buy {lo['ticker']}@{a:.2f} "
                f"sell {hi['ticker']}@{b:.2f} fees={fees:.3f} size={size:.0f} "
                f"profit>=${(b - a - fees) * size:.2f}"
            )
    return findings


async def main(series_list: list[str]) -> None:
    client = KalshiClient()
    found_any = False
    try:
        for st in series_list:
            resp = await client.get_events(
                series_ticker=st, status="open", with_nested_markets=True, limit=50
            )
            for event in resp.get("events", []):
                for f in await scan_event(client, event):
                    print(f"{dt.datetime.now():%H:%M:%S} {f}")
                    found_any = True
        if not found_any:
            print(f"{dt.datetime.now():%H:%M:%S} no structural arbs across "
                  f"{len(series_list)} series")
    finally:
        await client.aclose()


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--series", nargs="*", default=TARGET_SERIES)
    args = ap.parse_args()
    asyncio.run(main(args.series))
