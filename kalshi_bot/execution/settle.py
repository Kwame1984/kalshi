"""Settlement: book realized P&L when a position's market resolves.

Positions freeze at cost basis because the engine only polls OPEN markets;
once a market settles it drops out of the poll list and never gets a final
mark. This module drives settlement off OPEN POSITIONS instead: it fetches
each position's market result and, when decisive, writes a bot_settlements
row and closes the position.

Position convention (see state.py): bot_positions.contracts is signed
yes-contracts (+long YES / -long NO); avg_price is the YES-terms basis.

Payout for a settled binary market (qty signed, avg in YES terms):
    cost_basis = qty*avg           if qty > 0   (long YES)
               = -qty*(1 - avg)    if qty < 0   (long NO)
    payout = max(qty, 0)           if result == "yes"
           = max(-qty, 0)          if result == "no"
           = cost_basis            if result == "void"  (refund)
    realized_pnl = payout - cost_basis   (gross of entry fees)

This function is cash-agnostic: cash lives in the PaperExecutor / equity
ledger, and the CALLER applies sum(payout). See engine.cycle (step 0) and
main() below.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sqlite3
from dataclasses import dataclass

from kalshi_bot.api.client import PROD_BASE, KalshiClient
from kalshi_bot.execution import state

log = logging.getLogger("kalshi_bot.settle")

DECISIVE = {"yes", "no", "void"}


@dataclass
class SettlementRecord:
    ticker: str
    strategy: str
    contracts: float   # signed yes-contracts settled
    result: str        # 'yes' | 'no' | 'void'
    payout: float
    cost_basis: float
    realized_pnl: float


def _payout(qty: float, avg: float, result: str) -> tuple[float, float, float]:
    """Return (cost_basis, payout, realized_pnl) for a decisive result."""
    cost_basis = qty * avg if qty > 0 else -qty * (1.0 - avg)
    if result == "yes":
        payout = max(qty, 0.0)
    elif result == "no":
        payout = max(-qty, 0.0)
    else:  # void -> refund
        payout = cost_basis
    return cost_basis, payout, payout - cost_basis


async def settle_open_positions(
    client, conn: sqlite3.Connection, mode: str,
) -> list[SettlementRecord]:
    """Settle every open position in `mode` whose market has resolved.

    Cash-agnostic. For each decisive market: writes a bot_settlements row and
    deletes the position (one committed transaction per position), then returns
    the records booked. Non-decisive or fetch-failed positions are left open.
    """
    records: list[SettlementRecord] = []
    for ticker, pos in state.get_positions(conn, mode).items():
        qty, avg, strategy = pos["contracts"], pos["avg_price"], pos["strategy"]
        try:
            market = await client.get_market(ticker)
        except Exception:
            log.warning("settle: get_market failed for %s; leaving open", ticker)
            continue
        result = (market.get("result") or "").lower()
        if result not in DECISIVE:
            log.info("settle: %s not decisive (status=%s result=%r); skip",
                     ticker, market.get("status"), result)
            continue
        cost_basis, payout, pnl = _payout(qty, avg, result)
        state.record_settlement(conn, mode, strategy, ticker, qty, result,
                                payout, cost_basis, pnl)
        conn.execute("DELETE FROM bot_positions WHERE ticker=? AND mode=?",
                     (ticker, mode))
        conn.commit()
        records.append(SettlementRecord(ticker, strategy, qty, result,
                                        payout, cost_basis, pnl))
        log.info("settle: %s result=%s payout=%.2f pnl=%+.2f",
                 ticker, result, payout, pnl)
    return records


def apply_to_equity(
    conn: sqlite3.Connection, mode: str, records: list[SettlementRecord],
) -> float:
    """Apply settlement payouts to cash and write a fresh equity snapshot.

    Used by the standalone CLI (no live executor). Reads the last snapshot's
    cash, adds sum(payout), marks remaining open positions at cost basis
    (their avg), and records a new bot_equity row. Returns the new equity.
    """
    last = state.latest_equity(conn, mode)
    cash = (last["cash"] if last else 1000.0) + sum(r.payout for r in records)
    value = 0.0
    for p in state.get_positions(conn, mode).values():
        qty, avg = p["contracts"], p["avg_price"]
        value += qty * avg if qty >= 0 else -qty * (1.0 - avg)
    exposure = state.total_exposure(conn, mode)
    equity = cash + value
    state.record_equity(conn, mode, cash, exposure, equity)
    return equity


async def _run(mode: str) -> None:
    conn = state.connect()
    client = KalshiClient(base_url=PROD_BASE)  # public data only, no creds
    try:
        records = await settle_open_positions(client, conn, mode)
    finally:
        await client.aclose()
    if not records:
        print(f"No positions settled ({mode}).")
        conn.close()
        return
    equity = apply_to_equity(conn, mode, records)
    print(f"{'ticker':<28} {'result':>6} {'payout':>9} {'pnl':>9}")
    for r in sorted(records, key=lambda r: r.realized_pnl):
        print(f"{r.ticker:<28} {r.result:>6} {r.payout:>9.2f} {r.realized_pnl:>+9.2f}")
    tot_pnl = sum(r.realized_pnl for r in records)
    tot_pay = sum(r.payout for r in records)
    print(f"{'—' * 28} {'':>6} {'—' * 9} {'—' * 9}")
    print(f"{'TOTAL ' + str(len(records)) + ' settled':<28} {'':>6} "
          f"{tot_pay:>9.2f} {tot_pnl:>+9.2f}")
    print(f"new equity ({mode}): {equity:.2f}")
    conn.close()


def main() -> None:
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s %(message)s")
    ap = argparse.ArgumentParser(description="Settle resolved positions")
    ap.add_argument("--mode", default="paper", choices=["paper", "demo", "live"])
    args = ap.parse_args()
    asyncio.run(_run(args.mode))


if __name__ == "__main__":
    main()
