# Task 2 independent review

## Final review — commit `57509ea`

Reviewed the complete Task 2 change set `42feb60..57509ea`, the final anchor-validation delta from `4612c39`, the updated implementation evidence, and the previously established requirement/safety boundaries. Intervening documentation commits were excluded from implementation scope. No production files were edited and no unchanged tests were rerun. The latest supplied evidence is targeted RED **2 tests / 1 failure**, followed by full GREEN **49 tests passing in 0.654s**.

**Specification compliance verdict: PASS for the Task 2 journal/schema/reservation-storage boundary.** All R1–R5 findings are closed. The final change requires the exact account-bound completed reconciliation to be present in the chosen committed replay payload, with causal timestamps at or before the cutoff. It validates the account creation and historical revision/generation anchors as well. These checks run before session persistence, on cached-session return, and during startup integrity scanning. The last R2 counterexample now rejects without writing a session; the later cutoff succeeds with its reconciliation included.

**Quality/accounting/concurrency/security verdict: PASS for this Task 2 boundary.** No remaining Critical, Important or Minor findings were identified in the reviewed change set. This verdict relies on static review plus the attributed implementer test evidence; it does not claim an independent test rerun or live trading proof.

| Finding | Final disposition |
|---|---|
| R1 — durable dispatch | Closed: dispatch rejects nesting and returns only after its own immediate transaction commits. |
| R2 — truthful causal session export | Closed: committed table prefixes bind evidence to the chosen revision; shared timestamp policies include nested venue receipt; source-only commits advance prefix history; account/revision/generation/reconciliation anchors resolve inside the payload. |
| R3 — deterministic order replay | Closed: durable append sequence determines order independently of clock ties/regression and lexical IDs. |
| R4 — exact manifest identities | Closed: explicit table primary keys, canonical composite keys and duplicate detection replace foreign-key-based identities. |
| R5 — exact Decimal boundaries | Closed: explicit precision-100 contexts cover risk and cumulative-event arithmetic; arithmetic errors become contract violations. |

The implemented boundary retains the substantive safeguards established in the original review: balanced signed-int64 USD postings; content-bound duplicate handling; atomic revision/risk/reservation writes; cross-release economic-action uniqueness; fail-closed missing assessor/readiness; capacity retention across uncertain dispatch and terminal status; startup disarming; schema/checksum/FK integrity; immutable evidence; and historical archive reference validation.

Approved integration limits remain explicit: accounting and validated import collaborators must write through `ledger.transaction()`; authority must commit reservation before non-nestable dispatch; future compaction must preserve/migrate committed row-prefix identities; incompatible build databases fail closed. Full holdings/fill accounting, deterministic risk, reduction/replacement allocation, control/authentication, role isolation, release qualification and transport remain their assigned later tasks. This review clears Task 2 for dependent build work, not deployment, release activation, live adoption or orders.

## Prior re-review — commit `4612c39`

The following verdicts are historical and superseded by the final review above.

Reviewed the regenerated full task-owned diff through `4612c39`, its delta from `98fffac`, and the committed-evidence-prefix report. Only the three Task 2 implementation/test files were evaluated; intervening documentation commits remain outside implementation scope. No production edits or test reruns were performed. Supplied evidence is RED **46 tests / 3 failures**, a source-only RED **1 test / 1 failure**, and final GREEN **47 tests passing in 0.638s**.

**Specification compliance verdict: changes required.** The three previously reported R2 revision/order scenarios are fixed, but one required session reference remains outside the selected historical prefix.

**Quality/accounting/concurrency/security verdict: changes required — one Important finding remains; no Critical findings identified.** R1/R3/R4/R5 remain closed. Per-table committed membership, complete timestamp-policy reuse, and source-only history capture now correctly prevent later order/source evidence from entering an earlier revision's payload. The documented transaction/import seam and rowid-preserving retention obligations are explicit and acceptable for this intermediate component.

### R2 remaining — Important: the session reconciliation anchor bypasses prefix membership

**Locations:** `bot/autonomous/ledger.py:704`, `bot/autonomous/ledger.py:707`, `bot/autonomous/ledger.py:778`, `bot/autonomous/ledger.py:795`.

`cutoff.reconcile_id` is validated against the current unrestricted `reconciliations` table before historical prefix selection. After reading prefix-bounded evidence, nothing verifies that this exact reconciliation is present in `export['reconciliations']`. The concrete-FK scan checks only rows inside the export; the new `review_sessions` row and `SessionArchive.cutoff` are outside that scan.

Concrete supported-seam scenario: an account already has a committed historical prefix. Through `with ledger.transaction()`, insert a new completed reconciliation R whose start/finish/recording timestamps are T, while history capture is T+1. Export `SessionCutoff(account, T, R)` after that transaction commits. The live-table reconciliation check passes; history selection correctly chooses the prior prefix; the exported reconciliation table correctly excludes R. Nevertheless the method persists a **completed** session and archive naming R. Journal sequence can remain unchanged, so the journal consistency check does not catch the mismatch. The archive claims an exact reconciliation that its replay payload cannot resolve.

**Required correction:** validate the requested reconciliation against the selected prefix and the same complete causal predicates, or explicitly require that the exact account-bound completed reconciliation appears in the finished exported payload before constructing/persisting the archive. Reject a cutoff whose requested anchor is outside that prefix. Keep the initial current-database lookup only as an optional preliminary validation; it cannot establish historical membership.

**Acceptance case:** commit R with row timestamp T and history capture T+1, assert export at cutoff T with R fails without inserting a session, then assert export at T+1 succeeds and contains R. This is a narrow remaining session-reference defect, not a request for future reconciliation accounting. The new order/source prefix tests do not cover the session's own reconciliation reference.

## Prior re-review — commit `98fffac`

The following verdicts are historical and superseded by the current re-review above.

Reviewed the updated full three-file diff for `42feb60..98fffac`, the remediation delta from `9977b03`, and the updated implementation report. The intervening documentation commits are outside implementation scope. No tests were rerun and no production files were edited. Reported remediation evidence is RED **43 tests, 8 failures and 2 errors**, then GREEN **43 tests passing in 0.564s**.

**Specification compliance verdict: changes required.** R1, R3, R4 and R5 are addressed. R2 is partly addressed but the export can still combine evidence from a later commit with an earlier historical account revision.

**Quality/accounting/concurrency/security verdict: changes required — one Important finding remains; no remaining Critical findings identified.** The new non-nestable dispatch gate closes the original durability blocker. The documented requirement for accounting collaborators to use `ledger.transaction()` is a valid integration seam; this review's remaining scenarios use the supported ledger APIs and do not depend on bypassing that seam.

| Original finding | Re-review disposition |
|---|---|
| R1, nested dispatch before commit | Closed. `mark_dispatching` rejects an existing transaction before mutation and owns its commit; nested journal posting remains supported. |
| R2, causal historical export | Partly fixed. Explicit table policies, historical generation/revision, and journal watermark validation improve the boundary; the remaining issue is detailed below. |
| R3, lexical/timestamp event ordering | Closed for the original ordering defect. Events now have a durable unique append sequence and replay uses it. Cutoff membership must still be aligned with revision history as part of R2. |
| R4, manifest identity collisions | Closed. Per-table primary keys/composite keys are used and duplicate manifest record IDs reject. |
| R5, ambient Decimal precision | Closed. Risk and order-event arithmetic run in a fresh precision-100 context and Decimal errors become contract violations. |

### R2 remaining — Important: export visibility is not bound to the selected committed revision

**Current locations:** `bot/autonomous/ledger.py:621`, `bot/autonomous/ledger.py:624`, `bot/autonomous/ledger.py:720`, `bot/autonomous/ledger.py:728`, `bot/autonomous/ledger.py:733`, `bot/autonomous/ledger.py:739`.

The exporter selects individual rows by their timestamps and separately selects a historical account revision. It checks consistency only for the journal sequence. Revision history has no order-event/transaction membership watermark, so order events and order-intent state can cross the boundary of the selected revision without rejection.

Concrete supported-API scenario: commit a reservation, then dispatch it. The dispatch event is stamped at T; `_record_revisions()` samples the clock later at T+1 immediately before commit. A delayed first export with cutoff T includes the `DISPATCHING` event and reconstructs a dispatching intent, but selects the preceding reservation revision because the dispatch history's `known_at_us` is T+1. Journal sequence is unchanged, so its consistency check passes. This produces an archive whose account revision and order state describe different committed states. Distinct clock readings inside a transaction are normal and require no raw SQL mutation.

The new clock-regression support exposes the same gap more starkly: reserve/dispatch at time 80, append OPEN at time 100, then append PARTIAL after UTC regresses to 90. At cutoff 95 the OPEN and both later revision-history rows are excluded by the monotone knowledge time, but PARTIAL is included by its own timestamps. Replay reports PARTIAL under the earlier DISPATCHING revision, and even its projected version omits the excluded OPEN transition. The added regression test exports at a cutoff after all rows, so it does not cover this boundary.

There is also a policy mismatch within this finding: the export checks `payload_json.result.received_at_us` at line 724, but `_record_revisions()` derives order-event knowledge from the outer event/receipt/recording columns only (line 624). An `OrderEvent` with an outer receipt at T and a nested `VenueOrderResult.received_at_us` at T+100 is currently type-valid. Its new revision can be visible at T+10 while its event is excluded. The contracts validate intent/state identity but do not equate these timestamps.

**Required correction:** bind exported account evidence/projections to a proven committed transaction prefix or durable evidence watermarks stored with the selected revision. Ensure the revision knowledge calculation and row visibility use the same complete timestamp policy, including the nested result receipt, or reject contradictory receipt combinations at ingestion. Fail closed when an exact historical state cannot be established; do not persist a completed archive with mismatched revision and order state. Add targeted acceptance cases for a cutoff between event write and revision capture, a cutoff inside a regressing-clock sequence, and different outer/nested receipt times. These are one consistency problem, not requests to implement future accounting or transport.

The schema change to the initial migration is acceptable for the documented build-only database boundary: incompatible development databases fail closed. The reported RED/GREEN evidence supports the addressed cases; it does not cover the remaining export/revision consistency cases above.

## Original review — historical findings at `9977b03`

The verdicts and findings below record the original review and are superseded by the current re-review above.

Reviewed commit range `42feb60..9977b03` against the Task 2 brief, verbatim global constraints, implementation report, Task 2/capacity cases in the safety audit, and architecture §7. Review covers the three implementation/test files in the supplied diff. No implementation files were edited and no tests were run. The reported final result is **33 tests passing**; that is implementer evidence, not a reviewer rerun.

## 1. Specification compliance verdict: changes required

The required interfaces and architecture tables are present. Integer USD validation, balanced journal posting, content-bound journal/action/event/job identities, cross-release action uniqueness, immediate write transactions, reservation retention, startup disarming, checksum/schema checks and append-only evidence protections are substantially implemented.

The committed-before-dispatch guarantee is not upheld for the documented nested transaction seam (R1). Historical session exports still violate their causal/state/identity contracts (R2–R4). These are defects in implemented Task 2 behavior, rather than demands for future trading functionality.

The documented fail-closed deferrals are acceptable for this review: no reductions/replacements before authority allocates slots; no ready account with nonzero non-USD holdings before accounting provides liquidation evidence; no admission without a trusted synchronous risk assessor; no fabricated fill-accounting, live adoption, role authentication, release approval or transport implementation. Tasks 3/11–17 retain their stated integration obligations.

## 2. Quality, accounting, concurrency and security verdict: changes required

**Blocking findings: one Critical and three Important.** One additional Minor precision issue is recorded below. The storage boundary should be revised and reviewed before dependent authority/dispatch work relies on its return values or exports.

### R1 — Critical: nested dispatch can return before its durable marker commits

**Locations:** `bot/autonomous/ledger.py:493`, `bot/autonomous/ledger.py:507`; transaction behavior at `bot/autonomous/ledger.py:114` and `bot/autonomous/ledger.py:121`.

`mark_dispatching()` enters the general nesting-aware transaction helper. If a caller already holds `with ledger.transaction():`, the helper skips both `BEGIN IMMEDIATE` and `COMMIT`. The method nevertheless returns a `PreparedIntent` in `DISPATCHING`, with no indication that its marker remains uncommitted.

Concrete scenario: an authority collaborator reserves and marks an intent inside one outer transaction, then uses the successful `mark_dispatching()` return to submit. A crash or later outer rollback removes both the reservation and dispatch evidence even though the venue may have accepted the order. If reservation was committed earlier, recovery instead sees `RESERVED`, not the required `UNKNOWN`. No live transport exists yet, but the Task 2 API already promises the durable precondition that its future caller needs.

**Required correction:** make `mark_dispatching()` own and finish its transaction, rejecting an existing transaction before any mutation, or introduce an explicit post-commit dispatch capability that cannot escape before commit. Do not silently commit a caller's unrelated accounting transaction. Keep supported nested journal posting intact. Acceptance should include an outer transaction and prove that no successful dispatch authorization escapes before its marker is durable.

### R2 — Important: historical export still incorporates state and evidence beyond the cutoff

**Locations:** `bot/autonomous/ledger.py:604`, `bot/autonomous/ledger.py:608`, `bot/autonomous/ledger.py:659`; current account generation is copied by the generic export at `bot/autonomous/ledger.py:612`.

Only `source_events` receives event/receipt/knowledge predicates. `event_segments` checks commit time but not its event/receipt range; `source_invalidations` does not check `known_at_us`; order events and fills do not check event/receipt time. The schema does not make those missing predicates redundant, and the implemented `append_order_event()` accepts a typed event whose event/receipt timestamp exceeds the local recording clock. Such an event is exported and applied when `recorded_at_us <= cutoff`, even if its receipt is after the cutoff. The existing future-source test intentionally demonstrates the same timestamp mismatch pattern, but only for `source_events`.

Separately, the archive's `account_revision` is read from the **current** account at line 659. A journal posting after cutoff C but before the first export of C changes the archived journal sequence despite that transaction being excluded from the payload. Reopening after C also changes `accounts.generation`, which the generic exporter copies without historical reconstruction. Thus a delayed first export of the same historical state can contain later revision/generation information. Idempotency only freezes the first result; it does not make that result historical.

**Required correction:** define explicit per-table causal policies covering event, receipt, knowledge and applicable segment endpoints; reconstruct or omit mutable fields without cutoff evidence. Bind the archive revision to proven cutoff state, or change the interface/documented meaning explicitly if it is intentionally an export-time revision. Do not present a current revision as the revision replayed by a historical payload. Acceptance should exercise a first export after later postings/reopen, future-dated order receipts, invalidations, and segment ranges.

### R3 — Important: order replay uses lexical IDs to resolve timestamp ties

**Location:** `bot/autonomous/ledger.py:617`.

Order events are replayed by `(recorded_at_us, event_id)`, but event IDs do not encode append order. With a fixed/injected clock, reservation and dispatch have the same timestamp. `dispatch-...` sorts before `reserved-...`; replay then overwrites `DISPATCHING` with `RESERVED`. The same problem applies to multiple acknowledgments in one clock tick, and wall-clock regression can reverse events even without a tie. The export can therefore contradict the actual committed state while all event rows are included.

**Required correction:** persist and replay a deterministic append sequence or per-intent event ordinal. Use timestamps to decide visibility and the durable order to reconstruct transitions. Acceptance should cover same-timestamp reservation/dispatch and multiple status events with deliberately reversed lexical IDs.

### R4 — Important: manifest record IDs select foreign keys instead of row identities

**Location:** `bot/autonomous/ledger.py:652`.

The generic manifest identity selection checks a fixed list of column names. For a snapshot it selects `release_hash` rather than `snapshot_id`; two snapshots of one release therefore receive the same `snapshots:<release_hash>` record ID with different hashes. For label events it selects `label_id` rather than `label_event_id`, so pending/resolved/corrected revisions collide. Source invalidations similarly use their referenced `event_id`, and evaluation runs choose `release_hash` before `run_id`.

The full export hash still binds the complete payload, so this is not silent corruption of that stored JSON. It does, however, produce an ambiguous artifact/event/outcome manifest that cannot reliably identify the exact row to retrieve or compare. Ordinary repeated snapshots and label revisions trigger it; no malicious data is needed.

**Required correction:** map each exported table to its actual primary key (canonical composite keys where necessary), and reject duplicate record IDs within a manifest. Acceptance should include multiple snapshots/evaluations under one release and multiple label events for one label.

### R5 — Minor: venue increment checks run outside the explicit Decimal context

**Location:** `bot/autonomous/ledger.py:410`; related cumulative arithmetic at `bot/autonomous/ledger.py:524`.

Task 1 allows 38 decimal digits/scale, but price/quantity modulo runs before the local precision-100 context. Under Python's usual precision 28, a valid price such as `1.00000000000000000000000000001` with tick `0.00000000000000000000000000001` requires a 30-digit integer quotient and can raise `decimal.InvalidOperation`, even though the amount is small and exactly tick-aligned. The event cumulative sum also relies on ambient precision rather than the contract's supported precision.

**Suggested correction:** put all Decimal arithmetic at these boundaries inside an adequate explicit context, and translate arithmetic failures into the documented validation exception. Cover supported high-scale values and an altered ambient context. This is a valid-input availability/precision issue; the current modulo failure does not approve an unsafe order.

## Evidence and boundaries

The supplied tests substantiate rollback after a failed posting/reservation, nested-error poisoning, SQLite busy failure, exact duplicate/content conflicts, disarming and latch retention on reopen, UNKNOWN recovery with capacity retained, missing FK checks, schema tampering detection, and several export cutoff checks. They do not demonstrate the scenarios above. No test rerun was requested or performed.

The trusted assessor is explicitly an in-process authority dependency; this review does not treat a deliberately approving fixture callback as an exposed production bypass. Similarly, direct authority SQL used to seed tests is not evidence of an authenticated public mutation endpoint. Full economic reconciliation, held-asset accounting, latches, source/artifact import validation and single-authority process locking remain tracked future tasks, rather than findings against nonexistent Task 2 APIs.

Only this review document was authored for the review. AgenKit memory was refreshed as required before locating source; operator implementation changes were not altered. No deployment, external messages, credentials or orders were used.
