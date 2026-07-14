# Paper/Live Settlement Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Book realized P&L when a position's market settles — credit cash by the payout, close the position, and record an auditable settlement row — driven both by the engine loop and a standalone CLI.

**Architecture:** A new `kalshi_bot/execution/settle.py` holds `settle_open_positions()` (cash-agnostic: writes a `bot_settlements` row and deletes the position per settled market, atomically) plus a `main()` CLI that applies cash to a fresh equity snapshot. `state.py` gains the `bot_settlements` table and a `record_settlement` writer. `engine.cycle()` calls settlement as step 0 and applies the payout to `executor.cash`.

**Tech Stack:** Python 3.12, asyncio, sqlite3, httpx (via existing `KalshiClient`), pytest.

## Global Constraints

- Do not rename or drop existing columns of `bot_fills` / `bot_positions` / `bot_equity` (UI contract).
- `bot_positions.contracts` is signed YES-contracts; `avg_price` is YES terms.
- Only book `result ∈ {yes, no, void}`; skip and log anything else.
- Payout: `max(qty,0)` if `yes`, `max(-qty,0)` if `no`, `cost_basis` if `void`.
- `cost_basis = qty*avg` if `qty>0` else `-qty*(1-avg)`.
- `realized_pnl = payout - cost_basis` (gross of entry fees).

---

### Task 1: `bot_settlements` table + `record_settlement` writer

**Files:**
- Modify: `kalshi_bot/execution/state.py` (add table to `STATE_SCHEMA`; add writer)
- Test: `tests/test_settlement_state.py`

**Interfaces:**
- Produces: `record_settlement(conn, mode, strategy, ticker, contracts, result, payout, cost_basis, realized_pnl, ts=None) -> int`
- Produces: table `bot_settlements(id, ts, mode, strategy, ticker, contracts, result, payout, cost_basis, realized_pnl)`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_settlement_state.py
from kalshi_bot.execution import state


def _conn(tmp_path):
    return state.connect(tmp_path / "t.db")


def test_record_settlement_roundtrip(tmp_path):
    conn = _conn(tmp_path)
    rid = state.record_settlement(
        conn, "paper", "tail_seller", "KXHIGHCHI-26JUL07-T81",
        contracts=-25.0, result="no", payout=25.0,
        cost_basis=24.25, realized_pnl=0.75,
    )
    assert rid > 0
    row = conn.execute(
        "SELECT * FROM bot_settlements WHERE id=?", (rid,)
    ).fetchone()
    assert row["mode"] == "paper"
    assert row["strategy"] == "tail_seller"
    assert row["result"] == "no"
    assert row["payout"] == 25.0
    assert row["cost_basis"] == 24.25
    assert round(row["realized_pnl"], 4) == 0.75
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/pytest tests/test_settlement_state.py -v`
Expected: FAIL — `AttributeError: module 'kalshi_bot.execution.state' has no attribute 'record_settlement'` (and/or `no such table: bot_settlements`).

- [ ] **Step 3: Add the table to `STATE_SCHEMA`**

In `kalshi_bot/execution/state.py`, append inside the `STATE_SCHEMA` string (after the `bot_status` block, before the closing `"""`):

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

- [ ] **Step 4: Add the `record_settlement` writer**

In `kalshi_bot/execution/state.py`, after `record_equity` (around line 157), add:

```python
def record_settlement(
    conn: sqlite3.Connection,
    mode: str,
    strategy: str,
    ticker: str,
    contracts: float,
    result: str,
    payout: float,
    cost_basis: float,
    realized_pnl: float,
    ts: str | None = None,
) -> int:
    cur = conn.execute(
        "INSERT INTO bot_settlements (ts, mode, strategy, ticker, contracts,"
        " result, payout, cost_basis, realized_pnl) VALUES (?,?,?,?,?,?,?,?,?)",
        (ts or now_iso(), mode, strategy, ticker, contracts, result,
         payout, cost_basis, realized_pnl),
    )
    conn.commit()
    return int(cur.lastrowid)
```

- [ ] **Step 5: Run test to verify it passes**

Run: `.venv/bin/pytest tests/test_settlement_state.py -v`
Expected: PASS

- [ ] **Step 6: Commit**

```bash
git add kalshi_bot/execution/state.py tests/test_settlement_state.py
git commit -m "feat(settle): bot_settlements table + record_settlement writer"
```

---

### Task 2: `settle_open_positions` core logic

**Files:**
- Create: `kalshi_bot/execution/settle.py`
- Test: `tests/test_settle.py`

**Interfaces:**
- Consumes: `state.get_positions(conn, mode)`, `state.record_settlement(...)`, `client.get_market(ticker)`.
- Produces: `dataclass SettlementRecord(ticker, strategy, contracts, result, payout, cost_basis, realized_pnl)`
- Produces: `async settle_open_positions(client, conn, mode) -> list[SettlementRecord]`
- Produces: helper `_payout(qty, avg, result) -> tuple[float, float, float]` returning `(cost_basis, payout, realized_pnl)`.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_settle.py
import pytest

from kalshi_bot.execution import settle, state


class FakeClient:
    """get_market returns canned market dicts keyed by ticker."""
    def __init__(self, results: dict):
        self.results = results  # ticker -> {"status":..., "result":...}
        self.calls = []

    async def get_market(self, ticker: str) -> dict:
        self.calls.append(ticker)
        return self.results[ticker]


def _conn(tmp_path):
    return state.connect(tmp_path / "t.db")


def _seed_no_position(conn, ticker="KXHIGHCHI-26JUL07-T81"):
    # long 25 NO @ NO-price 0.97  ->  contracts=-25, avg(YES)=0.03
    state.apply_fill_to_position(conn, "paper", ticker, -25.0, 0.03, "tail_seller")


def test_no_result_wins_no_position(tmp_path):
    conn = _conn(tmp_path)
    _seed_no_position(conn)
    client = FakeClient({"KXHIGHCHI-26JUL07-T81": {"status": "settled", "result": "no"}})
    recs = __import__("asyncio").run(
        settle.settle_open_positions(client, conn, "paper")
    )
    assert len(recs) == 1
    r = recs[0]
    assert r.result == "no"
    assert r.payout == 25.0
    assert round(r.cost_basis, 4) == 24.25
    assert round(r.realized_pnl, 4) == 0.75
    # position closed
    assert state.get_positions(conn, "paper") == {}
    # row persisted
    assert conn.execute("SELECT COUNT(*) c FROM bot_settlements").fetchone()["c"] == 1


def test_yes_result_loses_no_position(tmp_path):
    conn = _conn(tmp_path)
    _seed_no_position(conn)
    client = FakeClient({"KXHIGHCHI-26JUL07-T81": {"status": "settled", "result": "yes"}})
    recs = __import__("asyncio").run(settle.settle_open_positions(client, conn, "paper"))
    assert recs[0].payout == 0.0
    assert round(recs[0].realized_pnl, 4) == -24.25
    assert state.get_positions(conn, "paper") == {}


def test_yes_result_wins_yes_position(tmp_path):
    conn = _conn(tmp_path)
    state.apply_fill_to_position(conn, "paper", "KXFOO", 10.0, 0.40, "s")  # long 10 YES @0.40
    client = FakeClient({"KXFOO": {"status": "settled", "result": "yes"}})
    recs = __import__("asyncio").run(settle.settle_open_positions(client, conn, "paper"))
    assert recs[0].payout == 10.0
    assert round(recs[0].cost_basis, 4) == 4.0
    assert round(recs[0].realized_pnl, 4) == 6.0


def test_no_result_loses_yes_position(tmp_path):
    conn = _conn(tmp_path)
    state.apply_fill_to_position(conn, "paper", "KXFOO", 10.0, 0.40, "s")
    client = FakeClient({"KXFOO": {"status": "settled", "result": "no"}})
    recs = __import__("asyncio").run(settle.settle_open_positions(client, conn, "paper"))
    assert recs[0].payout == 0.0
    assert round(recs[0].realized_pnl, 4) == -4.0


def test_void_refunds_cost_basis(tmp_path):
    conn = _conn(tmp_path)
    _seed_no_position(conn)
    client = FakeClient({"KXHIGHCHI-26JUL07-T81": {"status": "settled", "result": "void"}})
    recs = __import__("asyncio").run(settle.settle_open_positions(client, conn, "paper"))
    assert round(recs[0].payout, 4) == 24.25
    assert round(recs[0].realized_pnl, 4) == 0.0
    assert state.get_positions(conn, "paper") == {}


def test_undetermined_left_open(tmp_path):
    conn = _conn(tmp_path)
    _seed_no_position(conn)
    client = FakeClient({"KXHIGHCHI-26JUL07-T81": {"status": "active", "result": ""}})
    recs = __import__("asyncio").run(settle.settle_open_positions(client, conn, "paper"))
    assert recs == []
    assert "KXHIGHCHI-26JUL07-T81" in state.get_positions(conn, "paper")
    assert conn.execute("SELECT COUNT(*) c FROM bot_settlements").fetchone()["c"] == 0


def test_idempotent_second_run(tmp_path):
    conn = _conn(tmp_path)
    _seed_no_position(conn)
    client = FakeClient({"KXHIGHCHI-26JUL07-T81": {"status": "settled", "result": "no"}})
    __import__("asyncio").run(settle.settle_open_positions(client, conn, "paper"))
    recs2 = __import__("asyncio").run(settle.settle_open_positions(client, conn, "paper"))
    assert recs2 == []
    assert conn.execute("SELECT COUNT(*) c FROM bot_settlements").fetchone()["c"] == 1
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/pytest tests/test_settle.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'kalshi_bot.execution.settle'`.

- [ ] **Step 3: Write `settle.py` core**

Create `kalshi_bot/execution/settle.py`:

```python
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
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/pytest tests/test_settle.py -v`
Expected: PASS (7 passed).

- [ ] **Step 5: Commit**

```bash
git add kalshi_bot/execution/settle.py tests/test_settle.py
git commit -m "feat(settle): settle_open_positions core + payout math"
```

---

### Task 3: CLI (`python -m kalshi_bot.execution.settle`) with cash reconciliation

**Files:**
- Modify: `kalshi_bot/execution/settle.py` (add `apply_to_equity` + `main`)
- Test: `tests/test_settle.py` (add cash-reconciliation test)

**Interfaces:**
- Consumes: `state.latest_equity(conn, mode)`, `state.get_positions`, `state.total_exposure`, `state.record_equity`.
- Produces: `apply_to_equity(conn, mode, records) -> float` — reads prior cash, adds `Σ payout`, marks remaining positions at cost basis, writes a fresh equity snapshot, returns new equity.
- Produces: `main()` CLI entry.

- [ ] **Step 1: Write the failing test**

```python
# append to tests/test_settle.py
def test_apply_to_equity_reconciles_cash(tmp_path):
    conn = _conn(tmp_path)
    # prior snapshot: cash 900, one open unrelated position worth ~cost
    state.apply_fill_to_position(conn, "paper", "KXOTHER", -10.0, 0.05, "s")  # NO basis
    state.record_equity(conn, "paper", cash=900.0, exposure=9.5, equity=909.5)
    # a settlement worth +25 payout
    recs = [settle.SettlementRecord("KXX", "s", -25.0, "no", 25.0, 24.25, 0.75)]
    equity = settle.apply_to_equity(conn, "paper", recs)
    # new cash = 900 + 25 = 925; remaining KXOTHER marks at 10*(1-0.05)=9.5
    assert round(equity, 4) == round(925.0 + 9.5, 4)
    last = state.latest_equity(conn, "paper")
    assert round(last["cash"], 4) == 925.0
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/pytest tests/test_settle.py::test_apply_to_equity_reconciles_cash -v`
Expected: FAIL — `AttributeError: module 'kalshi_bot.execution.settle' has no attribute 'apply_to_equity'`.

- [ ] **Step 3: Add `apply_to_equity` and `main`**

Append to `kalshi_bot/execution/settle.py`:

```python
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
    # remaining open positions marked at cost basis
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
    print(f"{'—'*28} {'':>6} {'—'*9} {'—'*9}")
    print(f"{'TOTAL '+str(len(records))+' settled':<28} {'':>6} "
          f"{tot_pay:>9.2f} {tot_pnl:>+9.2f}")
    print(f"new equity ({mode}): {equity:.2f}")
    conn.close()


def main() -> None:
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s %(message)s")
    ap = argparse.ArgumentParser(description="Settle resolved positions")
    ap.add_argument("--mode", default="paper",
                    choices=["paper", "demo", "live"])
    args = ap.parse_args()
    asyncio.run(_run(args.mode))


if __name__ == "__main__":
    main()
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/pytest tests/test_settle.py -v`
Expected: PASS (8 passed).

- [ ] **Step 5: Commit**

```bash
git add kalshi_bot/execution/settle.py tests/test_settle.py
git commit -m "feat(settle): CLI + cash reconciliation via equity snapshot"
```

---

### Task 4: Wire settlement into the engine cycle

**Files:**
- Modify: `kalshi_bot/execution/engine.py` (import + step 0 in `cycle()`)
- Test: `tests/test_engine_settlement.py`

**Interfaces:**
- Consumes: `settle.settle_open_positions(self.client, self.conn, self.mode)`.
- Effect: `self.executor.cash += sum(r.payout for r in records)` before the equity snapshot.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_engine_settlement.py
import asyncio

from kalshi_bot.execution import settle, state


class FakeClient:
    def __init__(self, results):
        self.results = results

    async def get_market(self, ticker):
        return self.results[ticker]


def test_engine_step0_credits_cash_and_closes(tmp_path):
    """settle_open_positions + caller cash-apply mirrors engine step 0."""
    conn = state.connect(tmp_path / "t.db")
    state.apply_fill_to_position(conn, "paper", "KXHIGHCHI-26JUL07-T81",
                                 -25.0, 0.03, "tail_seller")
    client = FakeClient({"KXHIGHCHI-26JUL07-T81":
                         {"status": "settled", "result": "no"}})

    class Exec:
        cash = 975.69
    ex = Exec()
    records = asyncio.run(settle.settle_open_positions(client, conn, "paper"))
    ex.cash += sum(r.payout for r in records)

    assert round(ex.cash, 2) == round(975.69 + 25.0, 2)
    assert state.get_positions(conn, "paper") == {}
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/pytest tests/test_engine_settlement.py -v`
Expected: PASS actually is possible here since it only exercises settle — but the engine wiring is not yet present. To assert the wiring, first confirm the import is missing:

Run: `.venv/bin/python -c "import kalshi_bot.execution.engine as e; import inspect; assert 'settle_open_positions' in inspect.getsource(e.ExecutionEngine.cycle), 'not wired'"`
Expected: FAIL — `AssertionError: not wired`.

- [ ] **Step 3: Wire step 0 into `engine.py`**

In `kalshi_bot/execution/engine.py`, add to the imports (near the other `from kalshi_bot.execution import ...`):

```python
from kalshi_bot.execution import settle
```

Then in `cycle()`, immediately after the three `set_status` heartbeat lines (after `state.set_status(self.conn, "mode", self.mode)`, ~line 105) and before the `# 1. refresh open markets` comment, insert:

```python
        # 0. settle any resolved positions before marking equity
        try:
            settled = await settle.settle_open_positions(
                self.client, self.conn, self.mode)
            if settled:
                self.executor.cash += sum(r.payout for r in settled)
                log.info("settled %d positions, +%.2f payout",
                         len(settled), sum(r.payout for r in settled))
        except Exception:
            log.exception("settlement step failed")
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/pytest tests/test_engine_settlement.py -v`
Expected: PASS

Run: `.venv/bin/python -c "import kalshi_bot.execution.engine as e; import inspect; assert 'settle_open_positions' in inspect.getsource(e.ExecutionEngine.cycle)"`
Expected: exit 0 (wired).

- [ ] **Step 5: Full suite + commit**

Run: `.venv/bin/pytest -q`
Expected: all pass.

```bash
git add kalshi_bot/execution/engine.py tests/test_engine_settlement.py
git commit -m "feat(settle): engine cycle step 0 settles + credits executor cash"
```

---

### Task 5: Backfill the stuck 2026-07-07 cohort (verification on live data)

**Files:** none (operational run)

- [ ] **Step 1: Dry-run inspection of open paper positions**

Run:
```bash
.venv/bin/python -c "
from kalshi_bot.execution import state
c=state.connect()
ps=state.get_positions(c,'paper')
print(len(ps),'open paper positions')
for t,p in ps.items(): print(' ',t,p['contracts'],p['avg_price'])
"
```
Expected: the 16 tail_seller weather positions listed.

- [ ] **Step 2: Run the settlement CLI against live results**

Run: `.venv/bin/python -m kalshi_bot.execution.settle --mode paper`
Expected: a per-ticker table of results + cohort totals + new equity. Tickers whose markets are settled book a result; any not-yet-decisive are logged and left open.

- [ ] **Step 3: Confirm the ledger booked realized P&L**

Run:
```bash
.venv/bin/python -c "
from kalshi_bot.execution import state
c=state.connect()
n,pnl,pay=c.execute('SELECT COUNT(*),ROUND(SUM(realized_pnl),2),ROUND(SUM(payout),2) FROM bot_settlements WHERE mode=\"paper\"').fetchone()
print('settlements',n,'realized_pnl',pnl,'payout',pay)
print('open now',len(state.get_positions(c,'paper')))
print('equity',state.latest_equity(c,'paper'))
"
```
Expected: settlement count > 0, a non-zero realized P&L, fewer (ideally zero) open positions, and an equity that reflects the booked cohort rather than the frozen $998.95.

- [ ] **Step 4: Update README strategy note**

In `README.md`, update the tail_seller row / paper-trading note to reference realized settlement (replace the implied "frozen at fees" state with the booked cohort P&L observed in Step 3). Keep it one line.

- [ ] **Step 5: Commit**

```bash
git add README.md data/kalshi.db
git commit -m "chore(settle): backfill 2026-07-07 tail_seller cohort; book realized P&L"
```

Note: `data/kalshi.db` may be gitignored; if `git add` reports it ignored, skip it (the DB change is operational state, not source).
```
