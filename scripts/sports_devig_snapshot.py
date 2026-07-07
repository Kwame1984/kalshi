"""Live sports-devig snapshot: Kalshi MLB game markets vs sportsbook devig.

Loop-once integration proof for the sports_devig strategy:
1. pull today's (ET) open KXMLBGAME events + top-of-book from Kalshi;
2. one ESPN scoreboard call -> the partner book's (DraftKings) moneylines;
3. devig each game three ways (multiplicative / power / Shin);
4. match games to Kalshi events by team codes + start time (the event
   ticker encodes the ET slate time: ...26JUL071910PHICIN = 19:10 ET);
5. print kalshi bid/ask vs devig fair with net edge after TAKER fee (buy
   the ask) and after MAKER fee (rest at mid), plus what SportsDevig would
   actually fire on;
6. persist pre-game fair probs to external_signals (source =
   'sportsbook_devig', key = event ticker, target_date = ET game date,
   raw JSON = {market_ticker: fair_mult, ...} + underscore metadata).

HONESTY: the devig source is a single soft retail book, NOT a sharp line
(Pinnacle via The Odds API would be, but needs a signup key). Today's
purpose is measuring the Kalshi-vs-book divergence distribution, not
harvesting it. In-play games are shown but never compared or persisted:
pre-game book lines vs in-play Kalshi prices are not comparable.

Usage: .venv/bin/python -m scripts.sports_devig_snapshot [--date YYYY-MM-DD]
"""

from __future__ import annotations

import argparse
import asyncio
import datetime as dt
import json
import sqlite3
import statistics
import time
from zoneinfo import ZoneInfo

from kalshi_bot.api.client import KalshiClient
from kalshi_bot.backtest.fees import maker_fee, taker_fee
from kalshi_bot.data import store
from kalshi_bot.data.external import sportsbook
from kalshi_bot.strategies.base import Context, MarketState
from kalshi_bot.strategies.sports_devig import (
    SOURCE,
    SportsDevig,
    _floor_cent,
    parse_event_start,
)

SERIES = "KXMLBGAME"
ET = ZoneInfo("America/New_York")
MATCH_TOLERANCE_MIN = 90  # ticker slate time vs ESPN start, doubleheader-safe


def best_levels(orderbook: dict) -> tuple[float | None, float | None]:
    """(yes_bid, yes_ask) from orderbook_fp dollars; books list bids only."""
    ob = orderbook.get("orderbook_fp") or {}
    yes = ob.get("yes_dollars") or []
    no = ob.get("no_dollars") or []
    yes_bid = float(yes[-1][0]) if yes else None
    yes_ask = 1.0 - float(no[-1][0]) if no else None
    return yes_bid, yes_ask


async def fetch_todays_events(kalshi: KalshiClient, game_date: dt.date) -> list[dict]:
    """All open KXMLBGAME events whose ticker encodes `game_date` (ET)."""
    events, cursor = [], None
    while True:
        resp = await kalshi.get_events(series_ticker=SERIES, status="open",
                                       with_nested_markets=True, cursor=cursor)
        events.extend(resp.get("events", []))
        cursor = resp.get("cursor")
        if not cursor:
            break
    out = []
    for ev in events:
        start = parse_event_start(ev["event_ticker"])
        if start and start.astimezone(ET).date() == game_date:
            ev["_start_utc"] = start
            out.append(ev)
    return out


def event_teams(ev: dict) -> tuple[str, str] | None:
    """(away, home) Kalshi codes; the ticker tail is AWAY||HOME and each
    market ticker ends in -TEAMCODE, which disambiguates the split."""
    codes = [m["ticker"].rsplit("-", 1)[1] for m in ev.get("markets", [])]
    if len(codes) != 2:
        return None
    tail = ev["event_ticker"].split("-", 1)[1][11:]  # strip YYMMMDDHHMM
    # doubleheaders append G1/G2 after the team codes (…1415MILSTLG1)
    if len(tail) > 2 and tail[-2] == "G" and tail[-1].isdigit():
        tail = tail[:-2]
    for a, h in ((codes[0], codes[1]), (codes[1], codes[0])):
        if a + h == tail:
            return a, h
    return None


def match_events(
    games: list[sportsbook.GameOdds], events: list[dict]
) -> list[tuple[sportsbook.GameOdds, dict | None]]:
    """Pair ESPN games with Kalshi events by (away, home) + nearest start."""
    pool = list(events)
    out: list[tuple[sportsbook.GameOdds, dict | None]] = []
    for g in games:
        best, best_dt = None, None
        for ev in pool:
            teams = event_teams(ev)
            if teams != (g.kalshi_away, g.kalshi_home):
                continue
            delta = abs((ev["_start_utc"] - g.start_time).total_seconds()) / 60
            if delta <= MATCH_TOLERANCE_MIN and (best_dt is None or delta < best_dt):
                best, best_dt = ev, delta
        if best is not None:
            pool.remove(best)
        out.append((g, best))
    return out


def persist(snapshots: list[dict]) -> None:
    """Short write, retried: the collector may hold the WAL lock briefly."""
    issued = dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")
    rows = [(SOURCE, s["event_ticker"], s["target_date"], issued,
             s["value"], json.dumps(s["payload"])) for s in snapshots]
    for attempt in range(6):
        conn = store.connect()
        try:
            conn.executemany(
                "INSERT OR REPLACE INTO external_signals "
                "(source, key, target_date, issued_at, value, raw) "
                "VALUES (?, ?, ?, ?, ?, ?)", rows)
            conn.commit()
            return
        except sqlite3.OperationalError as exc:
            if "lock" not in str(exc).lower() or attempt == 5:
                raise
            time.sleep(0.25 * (attempt + 1))
        finally:
            conn.close()


async def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--date", help="ET game date YYYY-MM-DD (default: today ET)")
    args = ap.parse_args()
    game_date = (dt.date.fromisoformat(args.date) if args.date
                 else dt.datetime.now(ET).date())
    now = dt.datetime.now(dt.timezone.utc)

    kalshi = KalshiClient()
    try:
        events, games = await asyncio.gather(
            fetch_todays_events(kalshi, game_date),
            sportsbook.fetch_mlb_moneylines(game_date),
        )
        provs = sorted({g.provider for g in games if g.provider})
        print(f"{game_date} ET: espn {len(games)} games "
              f"(providers: {', '.join(provs) or 'none'}), "
              f"kalshi {len(events)} {SERIES} events")
        print("NOTE: single soft-book line (not Pinnacle-sharp); "
              "divergence measurement, not fair value.")

        strategy = SportsDevig()
        strategy_taker = SportsDevig(prefer_maker=False)
        snapshots, fired_lines, divergences = [], [], []
        pregame = skipped_live = skipped_noml = 0

        for g, ev in match_events(games, events):
            label = f"{g.away}@{g.home}"
            if ev is None:
                print(f"\n--- {label} {g.start_time:%H:%M}Z: "
                      f"NO KALSHI MATCH (extend ESPN_TO_KALSHI?)")
                continue
            if not g.has_moneyline():
                skipped_noml += 1
                print(f"\n--- {label} {g.start_time:%H:%M}Z: no book line yet "
                      f"({ev['event_ticker']}) — skipped")
                continue
            if g.state != "pre" or g.start_time <= now:
                skipped_live += 1
                print(f"\n--- {label}: in-play/finished (state={g.state}) — "
                      f"pre-game line not comparable, skipped")
                continue
            pregame += 1

            pa = sportsbook.american_to_prob(g.ml_away)
            ph = sportsbook.american_to_prob(g.ml_home)
            mult = sportsbook.devig_multiplicative(ph, pa)
            powr = sportsbook.devig_power(ph, pa)
            shin = sportsbook.devig_shin(ph, pa)
            fair_by_code = {  # kalshi team code -> (mult, power, shin)
                g.kalshi_home: (mult[0], powr[0], shin[0]),
                g.kalshi_away: (mult[1], powr[1], shin[1]),
            }

            hrs = (g.start_time - now).total_seconds() / 3600
            print(f"\n=== {ev['event_ticker']}  {label}  first pitch "
                  f"{g.start_time.astimezone(ET):%H:%M} ET ({hrs:.1f}h) "
                  f"[pre-game]  {g.provider} ml {g.away} {g.ml_away:+.0f} / "
                  f"{g.home} {g.ml_home:+.0f} "
                  f"(overround {(pa + ph - 1) * 100:.1f}%)")
            hdr = (f"    {'market':>6} {'k.bid':>6} {'k.ask':>6} {'mid':>6} "
                   f"{'fair.m':>7} {'fair.p':>7} {'fair.s':>7} "
                   f"{'tkrEdge':>8} {'mkrEdge':>8}  signal")
            print(hdr)

            payload: dict = {}
            ctx = Context()
            rows = []
            for m in ev.get("markets", []):
                code = m["ticker"].rsplit("-", 1)[1]
                if code not in fair_by_code:
                    continue
                fm, fp, fs = fair_by_code[code]
                bid = store.dollars(m.get("yes_bid_dollars"))
                ask = store.dollars(m.get("yes_ask_dollars"))
                try:
                    ob_bid, ob_ask = best_levels(
                        await kalshi.get_orderbook(m["ticker"], depth=1))
                    bid = ob_bid if ob_bid is not None else bid
                    ask = ob_ask if ob_ask is not None else ask
                except Exception:
                    pass  # markets-endpoint quotes are still top-of-book
                payload[m["ticker"]] = round(fm, 5)
                rows.append((m, code, fm, fp, fs, bid, ask))

            payload.update({
                "_fair_power": {m["ticker"]: round(fp, 5)
                                for m, _, _, fp, _, _, _ in rows},
                "_fair_shin": {m["ticker"]: round(fs, 5)
                               for m, _, _, _, fs, _, _ in rows},
                "_ml_home": g.ml_home, "_ml_away": g.ml_away,
                "_provider": g.provider,
                "_start_time": g.start_time.isoformat(),
                "_espn_id": g.espn_id,
            })
            ctx.external[(SOURCE, ev["event_ticker"], game_date.isoformat())] = payload

            for m, code, fm, fp, fs, bid, ask in rows:
                mid = (bid + ask) / 2 if bid is not None and ask is not None else None
                tkr = fm - ask - taker_fee(ask, 1) if ask else None
                mkr = None
                if mid is not None:
                    px = _floor_cent(mid)
                    mkr = fm - px - maker_fee(px, 1)
                if mid is not None:
                    divergences.append(abs(mid - fm))

                state = MarketState(
                    ticker=m["ticker"], ts=now, yes_bid=bid, yes_ask=ask,
                    last=store.dollars(m.get("last_price_dollars")),
                    meta={"event_ticker": ev["event_ticker"],
                          "close_time": m.get("close_time"),
                          "series_ticker": SERIES},
                )
                sig = ""
                for s in strategy.generate_signals(state, ctx):
                    sig = f"{s.side.upper()} {s.reason.split(' ', 1)[1]}"
                for s in strategy_taker.generate_signals(state, ctx):
                    sig = sig or f"{s.side.upper()} {s.reason.split(' ', 1)[1]}"
                if sig:
                    fired_lines.append(f"{m['ticker']}: {sig}")

                fmt = lambda v, w=6: (f"{v:>{w}.3f}" if v is not None
                                      else " " * (w - 3) + "  -")
                print(f"    {code:>6} {fmt(bid)} {fmt(ask)} {fmt(mid)} "
                      f"{fm:>7.4f} {fp:>7.4f} {fs:>7.4f} "
                      f"{fmt(tkr, 8)} {fmt(mkr, 8)}  {sig}")

            snapshots.append({
                "event_ticker": ev["event_ticker"],
                "target_date": game_date.isoformat(),
                "value": round(mult[0], 5),  # home team devigged (mult) prob
                "payload": payload,
            })

        # ---- summary ---------------------------------------------------
        print(f"\npre-game games compared: {pregame} | in-play skipped: "
              f"{skipped_live} | no book line: {skipped_noml}")
        if divergences:
            print(f"|kalshi mid - devig fair (mult)| across "
                  f"{len(divergences)} markets: "
                  f"mean {statistics.mean(divergences):.4f}, "
                  f"median {statistics.median(divergences):.4f}, "
                  f"max {max(divergences):.4f}")
        if snapshots:
            persist(snapshots)
            print(f"stored {len(snapshots)} event snapshot(s) to "
                  f"external_signals (source={SOURCE})")
        print("\nsignals passing SportsDevig gates (maker or taker):"
              + ("\n  " + "\n  ".join(fired_lines) if fired_lines else " NONE"))
    finally:
        await kalshi.aclose()


if __name__ == "__main__":
    asyncio.run(main())
