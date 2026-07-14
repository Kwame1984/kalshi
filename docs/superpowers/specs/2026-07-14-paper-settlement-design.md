# Paper/live settlement — design

**Date:** 2026-07-14
**Status:** approved, pending implementation

## Problem

The execution engine only refreshes markets with `status="open"`. Once a
market settles it drops out of the poll list, so `PaperExecutor.snapshot_equity`
never sees a final mark for that ticker and falls back to the position's cost
basis. Open positions therefore **freeze at cost basis** and never realize their
win/loss.

Observed live: the tail_seller paper cohort of 2026-07-07 (16 fills, ~303 NO
contracts across weather high-temp brackets) shows a last equity of $998.95 —
which is exactly the $1.05 of entry fees and nothing else. The markets settled
that evening, but the ledger booked no result. The engine has also been stopped
since 2026-07-09.

## Goal

When a position's market settles, book the realized outcome: credit cash by the
payout, close the position, and record an auditable realized-P&L row. Do this
both automatically inside the engine loop and via a standalone command that
works while the engine is down (so it can backfill the stuck 2026-07-07 cohort).

Non-goals: changing strategy logic, changing the fill model, or altering the
existing UI-contract columns of `bot_fills` / `bot_positions` / `bot_equity`.

## Conventions (confirmed from `paper.py` / `state.py`)

- `bot_positions.contracts` is **signed YES-contracts** (`+` long YES, `-` long
  NO). `avg_price` is always in **YES terms**.
- A NO buy of `q` contracts at NO-price `p` is stored as `contracts = -q`,
  `avg_price = 1 - p`; cash was debited `q·p + fee` at entry.
- Verified against the live row `KXHIGHCHI-26JUL07-T81`: fill `side=no,
  contracts=25, price=0.97` → stored `contracts=-25, avg_price=0.03`.

## Payout math

For a settled binary market with position `qty` (signed YES-contracts) and basis
`avg` (YES terms):

```
cost_basis = qty*avg          if qty > 0   (long YES)
           = -qty*(1 - avg)   if qty < 0   (long NO)

payout = max(qty, 0)          if result == "yes"
       = max(-qty, 0)         if result == "no"
       = cost_basis           if result == "void"   (refund)

realized_pnl = payout - cost_basis
```

The `max()` form makes the losing side fall out automatically: a long-NO
position on a `yes` result yields `max(qty,0)=0`; a long-YES on `no` yields
`max(-qty,0)=0`. No branching on side is required.

`realized_pnl` is gross of the entry fee. Entry fees already sit in `bot_fills`
and are already reflected in cash, so equity stays correct and net-of-fee cohort
P&L = `Σ realized_pnl − Σ entry_fees`.

### Worked example — live CHI-T81 NO position (`-25 @ 0.03`)

`cost_basis = 25 × (1 − 0.03) = $24.25` (matches the $24.25 + $0.06 fee debited
at entry).

| Outcome | payout | realized_pnl (gross) | net after $0.06 fee |
|---------|--------|----------------------|---------------------|
| `result=no`   (tail missed → bet wins) | `$25`    | **+$0.75**  | **+$0.69**  |
| `result=yes`  (tail hit → bet loses)   | `$0`     | **−$24.25** | **−$24.31** |
| `result=void`                          | `$24.25` | `$0`        | −$0.06      |

## Equity accounting (traced end-to-end)

Let `C0` = cash before entry. After entry: `cash = C0 − 24.31`, position marks at
cost `24.25`, so `equity = C0 − 0.06` — this is the frozen state observed today.

Settlement credits `cash += payout` and **deletes the position**:

- Win: `cash = C0 + 0.69`, position gone → `equity = C0 + 0.69` ✓
- Loss: `cash = C0 − 24.31`, position gone → `equity = C0 − 24.31` ✓

No double-count: the position's mark-value leaves equity exactly as the payout
enters cash.

## Architecture

### New module: `kalshi_bot/execution/settle.py`

```python
@dataclass
class SettlementRecord:
    ticker: str
    strategy: str
    contracts: float     # signed yes-contracts settled
    result: str          # 'yes' | 'no' | 'void'
    payout: float
    cost_basis: float
    realized_pnl: float

async def settle_open_positions(client, conn, mode) -> list[SettlementRecord]:
    """For each open position in `mode`, fetch its market result and, if
    decisive, write a bot_settlements row and delete the position (one atomic
    transaction per position). Returns the settlement records booked this run.

    Cash-agnostic: does NOT mutate any cash figure. The caller applies the
    payout (see reconciliation)."""
```

Behaviour per open position:

1. `market = await client.get_market(ticker)` → read `status`, `result`.
2. Decide the outcome:
   - `result in {"yes", "no"}` → binary payout.
   - `result == "void"` → refund cost basis.
   - anything else (empty result, undetermined, still open) → **skip and log**;
     leave the position open. Correctness guard: never guess a payout from
     `status` alone.
3. Compute `cost_basis`, `payout`, `realized_pnl` per the formula above.
4. In one transaction: `INSERT` the `bot_settlements` row and `DELETE` the
   `bot_positions` row.
5. Append a `SettlementRecord`.

Market fetch failure for one ticker logs a warning and continues with the rest
(each settlement commits independently; no partial-corruption path).

### Cash reconciliation

Cash is not a table; it is `PaperExecutor.self.cash`, seeded from the latest
`bot_equity` snapshot at init and decremented per fill. `settle_open_positions`
stays cash-agnostic; the caller applies `Σ payout`:

- **Engine** (`engine.py`, new step 0 at the top of `cycle()`):
  ```python
  records = await settle_open_positions(self.client, self.conn, self.mode)
  self.executor.cash += sum(r.payout for r in records)
  ```
  Settling first means the existing step-4 `snapshot_equity` / `daily_pnl` picks
  up the booked results in the same cycle.
- **CLI** (`python -m kalshi_bot.execution.settle`, engine down): read
  `latest_equity` cash → add `Σ payout` → recompute remaining-position value →
  write a fresh `bot_equity` snapshot.

These paths never run in the same process. Settlement deletes the position and
the executor re-seeds cash from the latest snapshot on next start, so there is no
path to a double credit even if both are used across time.

### New table (additive; UI contract untouched)

```sql
CREATE TABLE IF NOT EXISTS bot_settlements (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT,
    mode TEXT,
    strategy TEXT,
    ticker TEXT,
    contracts REAL,
    result TEXT,
    payout REAL,
    cost_basis REAL,
    realized_pnl REAL
);
```

Added to `STATE_SCHEMA` in `state.py` so `connect()` creates it. New helper
`record_settlement(conn, mode, rec)` mirrors the other `record_*` writers.

### CLI: `kalshi_bot/execution/settle.py::main`

```
python -m kalshi_bot.execution.settle --mode paper
```

Builds a public-data `KalshiClient` (prod base, no creds — same as paper
engine), runs `settle_open_positions`, applies cash to a fresh equity snapshot,
and prints a per-ticker table plus cohort totals (Σ realized_pnl, Σ payout,
count). This run backfills the stuck 2026-07-07 cohort.

## Idempotency & edge cases

- **Idempotent by construction**: settling deletes the position, so a re-run
  cannot reprocess it. Re-running the CLI is safe.
- **Cross-process safety**: the insert+delete is one committed transaction; a
  ticker settled by the engine is already gone when the CLI reads positions.
- **Void/cancel** → refund cost basis, `realized_pnl = 0` (entry fee treated as
  sunk; conservative).
- **Market fetch fails / undetermined** → skip that ticker, continue others.

## Testing

Unit tests (`tests/test_settle.py`) with a fake client returning canned
`get_market` results:

1. `result=no` on a long-NO position → payout `|qty|`, position deleted,
   `bot_settlements` row with correct `realized_pnl`.
2. `result=yes` on a long-NO position (loss) → payout 0, full-stake loss booked.
3. `result=yes` on a long-YES position (win) → payout `qty`.
4. `result=no` on a long-YES position (loss) → payout 0.
5. `result=void` → refund cost basis, `realized_pnl == 0`.
6. Undetermined/empty result → position untouched, no settlement row.
7. Idempotency: second run over the same DB books nothing new.
8. Cash reconciliation: CLI path writes a fresh equity snapshot equal to
   `prior_cash + Σ payout` over the reduced position set.
```
