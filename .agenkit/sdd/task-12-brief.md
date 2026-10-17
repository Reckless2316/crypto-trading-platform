## Task 12: order authority and paper lifecycle

**Files:** create `bot/autonomous/execution.py`, `tests/autonomous/test_execution_recovery.py`, `tests/autonomous/fixtures/partial_fill_cancel.jsonl`, `uncertain_submit.jsonl`; modify `bot/paper_coinbase_adapter.py` only at the managed-account submission boundary.

**Consumes:** risk/ledger/decision and market contracts. **Produces:** `OrderAuthority.submit/cancel/control/reconcile` architecture signatures, `VenuePort` protocol, `PaperVenue`, and `economic_action_key(account_id,candle_close_us,candidate_id,action_slot) -> str`.

- [ ] RED: release changes cannot create a second economic entry.

```python
import unittest
from bot.autonomous.execution import economic_action_key

class ExecutionTests(unittest.TestCase):
    def test_action_key_is_stable(self):
        key = economic_action_key("paper-1", 1000000, 1, "entry")
        self.assertEqual(key, economic_action_key("paper-1", 1000000, 1, "entry"))
        self.assertNotEqual(key, economic_action_key("paper-1", 1000000, 2, "entry"))
```

- [ ] Run `.venv/bin/python -m unittest tests.autonomous.test_execution_recovery -v`; expect RED.
- [ ] Implement stable keys and the full reserve → final check → committed DISPATCHING → venue lifecycle with injected clock/venue. Paper emits venue-like events; it does not mutate independent positions/cash.

```python
from bot.autonomous.contracts import content_hash

def economic_action_key(account_id, candle_close_us, candidate_id, action_slot):
    if (not account_id or type(candle_close_us) is not int or candle_close_us < 0
            or type(candidate_id) is not int or candidate_id <= 0
            or not action_slot):
        raise ValueError("invalid economic action")
    return content_hash([account_id, candle_close_us, candidate_id, action_slot])
```

Authority assigns semantic slots; callers cannot invent random entries. Include ledger duplicate tests for the same key under two releases, not just hash equality. Use bounded IOC fills against supplied causal depth with adverse fees/slippage, partials/nonfills and terminal remainders. Keep per-account dispatch mutex, control epoch check and atomic reservation. Record all source/intent/fill outcomes; admission unsupported means an audited no-trade paper session, not relaxed gates.
- [ ] GREEN: duplicate/cross-release entries, risk veto no submit, reservation rollback, partial fills/cancel races, oversell prevention, unknown status and persistence failure. Commit: `feat: execute paper orders through one authority`.


Safety acceptance supplement: read the Task 12 row and applicable cross-task cases in .agenkit/sdd/safety-audit.md. They make the already approved accounting/control/recovery requirements concrete; they are not certification that unimplemented code passes.


## Requirements added 2026-10-17 (Task 9 review I-C)
- `size_entry` verifies the edge from ledger evidence but takes `ExecutableBook` and `MarketRules` (depth, `available_buy_notional_usd_u`, `capability_hash`) as given. Authority must construct the book only from the state engine's live, sequence-verified book (`StateEngine.book`), with `lineage_hash` bound to the source event ids. It must construct `MarketRules` only from a verified venue capability snapshot. A caller-built book or capability must never reach `size_entry` or `risk.assess`.
- At dispatch, authority must re-check book freshness and venue capacity (`risk.assess` already vetoes stale books and capacity overruns) and refuse when the book lineage is not committed.
- Open (M-b): reject when spread + fee + modeled impact exceed the cost convention the edge was calibrated under (`cost_hash`).
- Required test: a caller-built `ExecutableBook` or `MarketRules` (lineage not committed, capability not verified) is refused before reservation (Task 9 review condition for deferring I-C).
- (Task 10 review T10-4) Before reserving, authority must recompute `size_entry` from the referenced persisted decision, live book and current account. It must refuse a proposal whose quantity, limit price, fee bound or loss budget differ. `risk.assess` cannot verify per-entry Kelly, `loss_budget >= notional*L`, spread, impact or depth participation.
- (T10-5/T10-6, Minor) `OrderProposal.decision.snapshot_hash` comes from the caller's snapshot and is not bound to the persisted row. `control_epoch`/`deadline_mono_ns` come from context and are checked only by `risk.assess` (epoch equality, deadline within lease). Bind or derive these at authority.
