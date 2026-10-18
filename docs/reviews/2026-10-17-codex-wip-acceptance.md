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

## Follow-up pass (2026-10-18): Task 13 transfers, fee finality and bounds; Task 17 forwarding

Scope: `bot/autonomous/live_venue.py`, `bot/live_coinbase_adapter.py`, `dashboard/backend/authority_client.py`, `dashboard/backend/app.py`, `dashboard/frontend/index.html` and their tests. Every change was written test-first against fakes; no exchange call or secret read took place.

### Task 13

- **Transfers.** `read_transfers` reads Coinbase v2 per-wallet transactions newest first, with bounded pages (≤100 per page and `max_pages` in total) and the read budget (≤3 attempts within ≤10 s or the caller's deadline). It stops at the window start and refuses unordered history. Each transfer is checked against its wallet currency and amount sign. Fills are skipped (the fills endpoint covers them); every other type is `FOREIGN_ACTIVITY` and makes the batch incomplete. Results are deduplicated by account+venue+transaction ID, and a changed payload becomes a conflict. Non-final statuses clear `all_final`.
- **Balance reconciliation.** `reconcile_balances` computes, per asset, opening + fills (base ± qty, USD ∓ quote − fee) + effective transfers, and compares it exactly with observed available+hold. It is ready only when all inputs are complete, final, conflict-free, issued by this venue, cover the same window and match exactly. Unexpected holdings show up as differences.
- **Fee finality.** `fee_finality(order, fills)` passes only when all of the following hold. `releasable` then delegates to `reservation_releasable`.
  - The order is complete, settled and terminal, and reports a total fee.
  - The fill batch is complete and has no conflict for this order.
  - The fill read started after the terminal observation, which rules out a missed late fill.
  - The fill window covers the order's creation.
  - Every fill carries a USD fee.
  - Fill quantities and fees sum exactly to the order's values.
  - All evidence was issued by this venue.
- **Streamed bound.** `ManagedBoundedTransport` wraps the client's HTTP session in `BoundedSession`. Responses are requested with `stream=True`. A declared Content-Length over 1 MiB is refused before reading; otherwise reading stops within one chunk of the cap and the connection closes. Verified against real CCXT 4.5.88 `fetch` with a fake HTTP session. The overflow raises `ResponseTooLarge`, which is not a network error, so neither CCXT nor `LiveVenue` retries it (`SOURCE_PAYLOAD_SIZE`). Reads and submit fail closed (`UNBOUNDED_TRANSPORT`) if the session has been replaced.
- **Legacy market path.** It previously ran for any account built without `managed=True`. Now `place_market_order` refuses unless the account was built with explicit `legacy_opt_in=True` (Important, fixed). `managed` and `legacy_opt_in` are read-only and mutually exclusive. Any client registered by a `ManagedBoundedTransport` is unreachable from a legacy account even with opt-in. Nothing in the repo opts in.

### Task 17

- **Forwarding.** arm (`/api/control/arm`, `/api/live/arm`), disarm (`/api/control/disarm`, `/api/live/disarm`) and flatten (`reduce`) now go through `AuthorityGateway.forward` to the control protocol. Live routes need a live-mode control session (`MODE_UNAVAILABLE` otherwise), and live arm needs the exact acknowledgement. Arm needs a 64-hex release hash.
- **Typed receipts.** Every response now carries one of `completed` (200), `pending` (202), `unknown` (202, `AUTHORITY_OUTCOME_UNKNOWN`), `rejected` (409, the authority's actual reason, e.g. `COMMAND_FORBIDDEN` for flatten under hard kill) or `unavailable` (409). `unavailable` covers both an authority `HANDLER_UNAVAILABLE` and routes with no control command (orders, live orders, automation, strategy execute). Nothing is mutated locally.
- **Durable nonces.** `DurableNonceStore` is a 0600 file in a 0700 directory owned by this user. It refuses unsafe permissions and is written atomically with fsync. It records consumed nonces, request identities and used bootstrap tokens; it never stores the control token or the raw envelope. Each nonce is claimed durably before the send. After a web restart a retry of the same request ID rebuilds the identical envelope, and a new request ID never reuses a nonce. The gateway refuses to start without a store.
- **Session setup.** `POST /api/session/bootstrap` exchanges a one-time, launcher-printed token (stored as a sha256 hash and bound to a provisioned session) for the HttpOnly SameSite=Strict cookie. It is Origin-bound and single-use across restarts.
- **Origin/CSRF on every POST.** Managed mutations keep operator role + session Origin + session CSRF. All other POST routes now require a loopback (or explicitly configured) Host, `Origin` equal to the server's own origin, and `X-CSRF-Token` equal to the per-process token (`GET /api/csrf`, Host-checked) or the session token. A test enumerates every POST route. The frontend `api()` helper now sends CSRF, request-ID and epoch headers and shows receipts.
- **Read-only diagnostics.** Every `/tmp` reader was already `mode=ro`. The remaining writable reader (`/api/market/book-observations`) now opens read-only, and a static test asserts that every `sqlite3.connect` in app.py outside `db()` uses `mode=ro` with `uri=True`. Note: `bot/autonomous/worker.py` `load_book_tops`/`load_eth_tops` (called by app routes) still open `data/autonomous/book_observations.sqlite3` writable but never write. That file was outside this pass's ownership.

## Still blocking acceptance (HANDOFF markers in code)

- **Task 13:**
  - The execution.py/ledger importer is missing. It must journal this evidence (corrections and compensating entries for conflicts), supply authoritative opening balances, and release reservations only when fee finality and balance reconciliation both pass.
  - Startup/periodic reconciliation of every UNKNOWN intent is missing.
  - There is no production `VenueCapabilityVerifier`.
  - The dispatch guard is in-process only; across restarts the authority's ledger state must do that job.
  - Coinbase v2 transaction type coverage comes from documentation and must be confirmed against real history before live (unknown types fail closed).
  - The streaming bound covers only the sync requests session.
- **Task 15:** unchanged from the first pass. There is no ledger release registry, no durable atomic commit of `ActivationPlan`, no `activate_release` handler and no production verifier. Migration of conversational approvals is also outstanding.
- **Task 17:**
  - The control protocol has no session issuance or epoch discovery (control.py is outside this scope). A stale-epoch status is rejected without returning the current epoch, so the UI starts at epoch 0.
  - Real arm/disarm/reduce handlers are not implemented in the authority, so these routes currently return `unavailable`.
  - Launcher wiring that constructs `AuthorityGateway` with sessions, the store and bootstrap tokens is missing.
  - `worker.py` readers should open read-only.

## Verification

- First pass: targeted suites OK. The only failures were 4 tests in the then-untracked, concurrently edited `tests/autonomous/test_runtime_authority.py`.
- Follow-up pass (2026-10-18): full suite `.venv/bin/python -m unittest discover -s tests`: 843 tests, OK (1 skipped).
