"""Competitive-flow analysis: fingerprint other bots in recorded L2/trade data.

Premise: many participants run similar public strategies (theta farming at
97-99c, weather-model repricing at 06/12/18Z, A-S market making). Their
footprints are detectable, and crowding is information:
- resting walls at signature prices (96-99c) = crowded theta/tail-selling
- repricing bursts at fixed minutes (model releases) = weather/data bots
- uniform order sizes repeated across markets = single-bot fingerprints
- one-sided aggressive flow = the informed flow our momentum strategy follows

Usage: python -m scripts.flow_analysis [--hours 24]
Reads orderbook_log (zlib-compressed WS messages) + trades tables.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import zlib
from collections import Counter, defaultdict

from kalshi_bot.data.store import connect


def load_messages(conn, hours: float):
    cutoff_ms = int((dt.datetime.now(dt.UTC).timestamp() - hours * 3600) * 1000)
    for row in conn.execute(
        "SELECT recv_ts_ms, channel, ticker, payload FROM orderbook_log "
        "WHERE recv_ts_ms > ? ORDER BY recv_ts_ms", (cutoff_ms,)
    ):
        try:
            payload = row["payload"]
            text = zlib.decompress(payload) if isinstance(payload, bytes) else payload
            yield row["recv_ts_ms"], row["channel"], row["ticker"], json.loads(text)
        except Exception:
            continue


def analyze(hours: float) -> None:
    conn = connect()
    n_msgs = 0
    # signature-price resting depth (from snapshots): where do walls sit?
    wall_depth = Counter()          # price-cents bucket -> total resting contracts
    delta_minutes = Counter()       # minute-of-hour -> delta count (burst detection)
    trade_sizes = Counter()         # exact size strings -> count (bot fingerprints)
    aggr_side = defaultdict(lambda: [0.0, 0.0])  # ticker -> [buy_vol, sell_vol]

    for ts_ms, channel, ticker, msg in load_messages(conn, hours):
        n_msgs += 1
        m = msg.get("msg", {})
        if channel == "orderbook_snapshot":
            for side_key in ("yes_dollars_fp", "no_dollars_fp",
                             "yes_dollars", "no_dollars", "yes", "no"):
                for lvl in (m.get(side_key) or []):
                    try:
                        price = float(lvl[0])
                        qty = float(lvl[1])
                    except (TypeError, ValueError, IndexError):
                        continue
                    if price > 1:  # legacy cents
                        price /= 100
                    if side_key.startswith("no"):
                        price = 1 - price
                    wall_depth[round(price * 100)] += qty
        elif channel == "orderbook_delta":
            minute = dt.datetime.fromtimestamp(ts_ms / 1000, dt.UTC).minute
            delta_minutes[minute] += 1
        elif channel == "trade":
            count = m.get("count_fp") or m.get("count") or "0"
            trade_sizes[str(count)] += 1
            side = m.get("taker_side") or m.get("taker_outcome_side") or ""
            vol = float(count or 0)
            if side == "yes":
                aggr_side[ticker][0] += vol
            elif side == "no":
                aggr_side[ticker][1] += vol

    span_note = f"last {hours:.0f}h, {n_msgs} messages"
    print(f"=== COMPETITIVE FLOW ANALYSIS ({span_note}) ===\n")

    print("-- Resting depth by yes-price (walls = crowded strategies) --")
    total = sum(wall_depth.values()) or 1
    for cents in sorted(wall_depth):
        share = wall_depth[cents] / total
        if share >= 0.03:
            bar = "#" * int(share * 100)
            print(f"  {cents:3d}c: {wall_depth[cents]:>10.0f} ({share:5.1%}) {bar}")
    theta_zone = sum(v for c, v in wall_depth.items() if c >= 94) / total
    tail_zone = sum(v for c, v in wall_depth.items() if c <= 6) / total
    print(f"  crowding: >=94c zone {theta_zone:.1%} | <=6c zone {tail_zone:.1%}\n")

    print("-- Orderbook delta bursts by minute-of-hour (bot repricing cadence) --")
    if delta_minutes:
        avg = sum(delta_minutes.values()) / 60
        spikes = {m: c for m, c in delta_minutes.items() if c > 2.5 * avg}
        for m, c in sorted(spikes.items()):
            print(f"  minute :{m:02d} -> {c} deltas ({c/avg:.1f}x average)")
        if not spikes:
            print("  no strong fixed-minute cadence detected yet")
    print()

    print("-- Trade-size fingerprints (repeated exact sizes = bots) --")
    for size, cnt in trade_sizes.most_common(8):
        print(f"  size {size}: {cnt} trades")
    print()

    print("-- Event-axis net flow (informed-flow candidates) --")
    # Kalshi retail is ~85% YES-taker everywhere, so per-market YES share is
    # not informative. Normalize to a per-EVENT axis: YES-taker volume on
    # market X and NO-taker volume on X's sibling both count as flow INTO X.
    event_flow: dict[str, Counter] = defaultdict(Counter)
    for t, (buy, sell) in aggr_side.items():
        event = t.rsplit("-", 1)[0]
        event_flow[event][t] += buy       # yes-takers on this market
        event_flow[event]["_sell_" + t] = sell
    scored = []
    for event, flows in event_flow.items():
        markets = [k for k in flows if not k.startswith("_sell_")]
        if len(markets) != 2:
            continue  # axis defined only for two-outcome events
        a, b = markets
        into_a = flows[a] + flows["_sell_" + b]
        into_b = flows[b] + flows["_sell_" + a]
        vol = into_a + into_b
        if vol >= 100:
            net = into_a - into_b
            target = a if net > 0 else b
            scored.append((abs(net) / vol, target, vol))
    for imb, target, vol in sorted(scored, reverse=True)[:8]:
        print(f"  {target}: {imb:.0%} of event flow (vol {vol:.0f})")
    if not scored:
        print("  (no two-outcome events with enough volume yet)")
    conn.close()


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--hours", type=float, default=24.0)
    args = ap.parse_args()
    analyze(args.hours)
