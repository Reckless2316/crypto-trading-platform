## Task 16: role workers, market/private ingestion and hourly loop

**Files:** create `bot/autonomous/worker.py`, `tests/autonomous/test_worker.py`; integrate existing `data/collectors/coinbase_collector.py` and `data/collectors/collector_base.py` only through explicit event adapters; extend state/authority as needed.

**Consumes:** Tasks 1–15 interfaces. **Produces:** `run_worker(config,dependencies) -> None`, authority/reflex entrypoints, independent deadlines, continuous marking and durable hourly scheduling; `role_secret_names(role: str) -> frozenset[str]`.

**Current milestone:** collection and daily reviews only. `--serve` and `--serve-seconds` do not run hourly decisions or submit orders. Keep Task 16 incomplete until an entrypoint integration test proves the approved hourly workflow and durable candle deduplication across restart.

- [ ] RED: roles cannot inherit venue/model credentials indiscriminately.

```python
import unittest
from bot.autonomous.worker import role_secret_names

class WorkerTests(unittest.TestCase):
    def test_reflex_has_no_venue_key(self):
        self.assertEqual(role_secret_names("reflex"), frozenset({"TYPESAFE_API_KEY"}))
        self.assertNotIn("TYPESAFE_API_KEY", role_secret_names("authority"))
```

- [ ] Run `.venv/bin/python -m unittest tests.autonomous.test_worker -v`; expect RED.
- [ ] Implement explicit role configuration and startup state machine; create live adapter only inside authority, only after the approved live activation conditions.

```python
def role_secret_names(role):
    names = {
        "authority": {"SURGE_COINBASE_API_KEY", "SURGE_COINBASE_API_SECRET"},
        "reflex": {"TYPESAFE_API_KEY"},
        "brain": {"SURGE_BRAIN_API_KEY"},
        "dashboard": set(),
    }
    if role not in names:
        raise ValueError("unknown role")
    return frozenset(names[role])
```

Control tokens are role-specific file-mounted capabilities, separate from this provider-secret allowlist. Strip unrelated provider env/secrets on role startup and mount isolation in Task 20. Hold process flock for life. Startup order matches architecture §9. Public depth and private fills use verified venue stream/sequence semantics; a five-minute REST candle poll is not a live order book. Maintain current equity/risk between candles. At hourly close freeze one knowledge cutoff, persist snapshot, request one batch, validate returned identity/deadline, assess current account and dispatch eligible intents. Persist candle scheduling/dedupe and terminal HOLD outcomes; no replay of missed historical entries after restart. Graceful stop inhibits dispatch, preserves reservations and reconciles/cancels per control state.
- [ ] GREEN: two workers competing lock, missing keys, wrong role environment, stale streams, clock jump, next-bar closure, duplicate candle, model delay, fresh fills during inference, disk pressure and crash recovery. Commit: `feat: run isolated autonomous decision workers`.


Safety acceptance supplement: read the Task 16 row and applicable cross-task cases in .agenkit/sdd/safety-audit.md. They make the already approved accounting/control/recovery requirements concrete; they are not certification that unimplemented code passes.

Integration handoff: read `docs/reviews/2026-10-17-integration-entrypoints.md` for concrete existing route/call/credential paths and acceptance cases. It is a static source map, not proof of active runtime or future integration. Task16 owns worker/role isolation, Task17 all managed legacy order/control/dashboard bypass closure, Task20 actual compose/environment/mount isolation. Preserve existing operator UI edits. The map has no production changes or test certification.

Jev boundary handoff: Task7's final report must provide actual immutable raw-audit access or full typed answer evidence for independent authority revalidation. Recompute all Choice distribution/confidence checks and Noul mapping against the expected request/schema/state/model before policy. Keep provider response-header request ID distinct from local correlation; the public vendor contract does not attest local state/bundle/deadline echoes. Use the authenticated role channel and local outstanding-operation binding. Do not treat a raw hash or copied local identity fields as independent validation.


## State-engine book input contract (added 2026-10-17 from cross-review remediation)

`StateEngine.snapshot` replays only `[close − 60 min − 5 min, close]` and samples each minute boundary as-of, with a 5 s maximum sample age. Collectors feeding the state engine must therefore:
- publish a full-book checkpoint (`BookPayload(is_snapshot=True)`, top-N levels) on each state stream at least every 5 minutes, and preferably every 1 s. Persisting every level-2 delta into `source_events` is not the state-engine contract, because the decode cache (8192 rows) is sized for one window at 1 s cadence;
- keep sequence numbers contiguous and exchange time non-decreasing within a stream; any break invalidates boundaries until the next checkpoint;
- never synthesize or round exchange timestamps onto minute boundaries.
- configure exactly one book stream per instrument for the state engine. Any second book stream for the instrument inside the 65-minute replay window, including one left over from a failover, makes mid, RV and return invalid for that hour (fail closed). Failover must switch streams only together with a fresh checkpoint, and accept an hour of unavailable features;
- keep a logical key on one instrument across all of its revisions (ingest rejects changes).
