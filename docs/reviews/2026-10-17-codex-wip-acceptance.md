# Codex WIP acceptance review: Tasks 13, 15, 17 (2026-10-17)

Reviewer: claude-opus-5.5. Scope: commit 2c294cb (marked NOT ACCEPTED) plus the
previously untracked `dashboard/backend/authority_client.py`. Requirements come from
`docs/plans/2026-10-16-autonomous-crypto-agent.md` (Tasks 13, 15, 17),
`master-context.md` and `.agenkit/sdd/global-constraints.md`. Every scenario used fake
transports only. No exchange call, order or secret read took place.

## Verdict

The kernels are **accepted as inert, standalone components** with the fixes below.
**None of Tasks 13, 15 or 17 is complete.** The blockers listed at the end are still open.

## Findings and fixes

Each fix was written test-first: the failing test came first, then the change.

| # | Sev | Module | Finding (reproduced with fakes) | Fix |
|---|-----|--------|----------------------------------|-----|
| 1 | Critical | live_venue.py `submit` | `create_order` ran with the client's own `maxRetriesOnFailure` (3 in the scenario), so the SDK could retry the effectful submit. | Under `client_lock`, `submit` forces `maxRetriesOnFailure=0` and restores it afterwards, including on failure. |
| 2 | Important | live_venue.py `submit` | The submit SDK timeout was the client default (30 s), not bounded by the intent deadline or the 10 s cap. | Timeout = `max(1ms, min(10s, intent deadline remaining))`, then restored. |
| 3 | Important | live_venue.py `submit` | A second `submit` of the same intent/client order ID reached the venue again, including after a timeout with an unknown outcome. | One dispatch attempt per client order ID per instance. Any repeat raises `LiveTradingRejected` before the transport is called. |
| 4 | Important | authority_client.py | Each status GET used up one single-use control nonce. Status polling could exhaust them, and kill then returned 503 (verified: 3 status reads, then kill refused). | The last nonce is reserved for kill (`KILL_RESERVED_NONCES=1`). Status returns 503 rather than consume it. |
| 5 | Important | authority_client.py | Client-side `ControlRejected` raised after the send (TRUNCATED_FRAME, MESSAGE_SIZE, INVALID_JSON) was reported as 409 "rejected", losing the uncertainty of a kill that may have committed. Unexpected client exceptions returned 500. | Post-send failures and unexpected exceptions return 202 `AUTHORITY_OUTCOME_UNKNOWN`. Pre-send refusals (WRONG_SERVER_PEER) return 503. Only a bound `rejected` reply maps to 409. |
| 6 | Important | release.py | Plan requirement missing: no activation logic. It had no operator authentication, no unarmed/reconciled/UNKNOWN check, no boundary atomicity, no handling of judgments or action keys, and no rollback/lineage rules. | Added the pure `plan_activation` planner, `ActivationState`, `ActivationPlan` (`plan_hash`, `accepts_judgment`) and `entries_allowed`. It requires an operator `ControlSession` for the same account, always re-runs `validate_bundle`, refuses ARMED / UNRECONCILED / PENDING_UNKNOWN_ORDER / BOUNDARY_IN_PROGRESS, needs a future hour boundary and a bundle that outlives it, invalidates in-flight judgments, carries forward pending action keys and risk latches unchanged, and only appends to history. A forward release must name the active bundle as parent. A rollback must be a previously active bundle that still validates. |
| 7 | Important | release.py | Self-approval relied entirely on the verifier. A verifier that listed `brain` as an operator actor let a generated bundle approve itself. | Approvals whose actor is a role principal (`brain`, `reflex`, `reader`, `nightly`, `system`, `authority`, or any `<name>:` prefix of them) are refused with `NON_OPERATOR_APPROVER`, whatever the verifier says. |
| 8 | Important | repo | `authority_client.py` was untracked, but the committed `app.py` (42952f6) imports it, so a clean checkout of HEAD fails to import. | The file is committed with this review. |

Checked and found sound: reads are capped at ≤3 attempts within ≤10 s or the caller's deadline; cancel is never retried; a cancel acknowledgment is not terminal; a client-ID not-found is never treated as proof of absence; client-ID collisions and portfolio mismatches fail closed; fill pagination with overlap uses account+venue+trade-ID dedupe, and a changed payload is surfaced as a conflict rather than overwritten; float financial fields are refused; deadlines are checked after each call; validation binds approvals to the bundle hash and the verifier issuer to the request hash; expiry windows cannot outlive their evidence; browser cookies are HttpOnly and SameSite=Strict; mutations require operator role, Origin and CSRF; a missing gateway never falls back to a default user or database.

Minor findings (not fixed):
- In `lookup`/`cancel`, `_reference` runs outside the try block, so an account mismatch raises instead of returning UNKNOWN evidence.
- Fill window checks use `trade_time`, while Coinbase filters on sequence time. The result is conservative but can mark batches incomplete.
- `ReleaseActivationTests` inherits the base tests, so those run twice.

## Still blocking acceptance (HANDOFF markers in code)

- **Task 13:** transfers are never read and `reconciliation_ready` is always False. No source yields `fees_final=True`. The dispatch guard is in-process only; across restarts the guard is the authority's DISPATCHING/UNKNOWN state. The payload size bound applies after the SDK has buffered the response. Not done: wiring into execution.py/ledger importer (journals, corrections, compensating entries, releasing reservations), the "failed balance match" and "late fill after terminal" reconciliation, and the `tests/test_live_adapter.py` / execution-recovery GREEN scenarios. The legacy `place_market_order` managed-account rejection was not reviewed (it is in live_coinbase_adapter.py, outside this scope).
- **Task 15:** the ledger release registry, a durable atomic commit of `ActivationPlan` and the control `activate_release` handler are missing. Migration of conversational approvals into Ship evidence is also outstanding. The only verifier is in test fixtures; no production verifier exists.
- **Task 17:** only status and kill are forwarded. Not done: flatten/reduce permits (including "flatten under hard kill returns the actual rejection"), orders, arm and session bootstrap/provisioning (all in app.py, owned by the lead). Nonces are not persisted across web restarts and per-boot issuance is required.

## Verification

- Targeted: `tests.autonomous.test_live_venue`, `test_release_promotion`, `test_dashboard_authority`, `tests.test_platform_api`, `tests.test_live_adapter`: 116 tests OK.
- Full suite: 724 tests. The only failures are 4 tests in the **untracked, concurrently edited** `tests/autonomous/test_runtime_authority.py` (`RuntimeConfig(accounting=...)` is not yet supported by runtime.py, which another agent owns). None of them import the modules reviewed here. Before these changes the suite was 680/680 green; that file did not exist then.
