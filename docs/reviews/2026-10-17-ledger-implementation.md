# Task 2 implementation report

Status: DONE for the journal/schema/reservation storage boundary, pending the independent review requested by conductor. Commit `9977b03` (`feat: add durable autonomous journal`). Only the three owned implementation/test paths were committed. No external calls, credentials, model calls, deployment, or live adoption occurred.

## Files and interfaces

- `data/migrations/001_autonomous_ledger.sql`: every architecture §7 table, concrete FKs, integer USD columns, Decimal TEXT, STRICT storage types, typed enum/bool/range/JSON checks, uniqueness constraints, causal indexes, append-only triggers, and immutable intent identity. Additional `schema_migrations` and `account_readiness` tables support migration checksums and conservative mutable readiness.
- `bot/autonomous/ledger.py`: implements `open`, `close`, `create_paper_account`, `post`, `reserve_intent`, `mark_dispatching`, `append_order_event`, `account_view`, `append_review_job`, and `export_session` with Task 1 frozen records. `candidate_wire_id` maps internal string IDs to the checked numeric DB registry.
- `tests/autonomous/test_ledger.py`: 33 deterministic stdlib unittest tests, temporary SQLite databases, inert paper fixtures, no network.

Journal posting validates signed int64 USD and exact balance inside one immediate transaction. Transaction ID reuse compares account/kind and ordered posting content. A posting failure rolls back the transaction and disables dispatch. Nested failures poison the outer transaction even if a collaborator catches the inner exception. SQLite busy/commit failure disables dispatch until reopening/reconciliation.

Opening uses FK ON, WAL, FULL synchronous, bounded busy timeout/cache/checkpoint/SQL and row length. Migration marker checksum, actual table/index/trigger SQL, SQLite integrity/FKs, journal balance/source existence, canonical JSON/Decimals, source/correction/label payload hashes, release core hash, and persisted session content hashes are scanned. Startup revokes the boot lease/permit, increments generation, preserves halts, and projects DISPATCHING to UNKNOWN with append-only recovery events, retaining reservations.

Reservation and dispatch read current durable control/release/account state in `BEGIN IMMEDIATE`. Checks include revision, generation, epoch, boot/deadman/deadline, readiness/reconciliation/heartbeat/valuation, candidate pause/eligibility/decision reference, USD venue increments/fee bounds/depth, unreserved cash/base, position/gross/loss budgets, and current NAV cross-products. A typed assessor verdict must match current account revision/proposal hash and carry no pending halt/escalation transition. `DISPATCHING` commits before the method returns; no async/network code exists here.

Typed order events validate account/client identity, content-bound event/venue-key idempotency, legal transition, cumulative quantities and filled completeness. Status changes cannot release reservations or journal money. Review jobs are immutable and content-bound by both job ID and dedupe key. Session identity is account/cutoff and exact reconciliation; stored archive/export are immutable. Session source timestamps and event/control evidence respect cutoff. Order state is rebuilt from cutoff events, mutable labels export stable identity plus label events, concrete exported refs must resolve, and raw projection tables without historical proof are omitted.

## Exact TDD evidence

Command throughout: `.venv-autonomous/bin/python -m unittest tests.autonomous.test_ledger -v`.

1. Initial RED: 20 tests failed with explicit `AssertionError: durable Ledger has not been implemented`; no production ledger existed when tests were first run.
2. First GREEN: `Ran 20 tests ... OK` after schema and ledger implementation.
3. Added nested-transaction, dropped-trigger, future-control export tests: RED `FAILED (failures=3)`; fixed transaction poisoning, complete expected-schema comparison and cutoff filtering. GREEN: 27 tests.
4. Added release content and negative Decimal startup tests: RED `FAILED (failures=2)`; fixed core release identity and negative/canonical Decimal validation. GREEN: 30 tests.
5. Added dispatch own-reservation and FILLED completeness tests: RED `FAILED (failures=2)`; fixed assessor input to exclude the proposal's already committed reservation without changing authoritative revision and reject remaining quantity on FILLED. GREEN: 32 tests.
6. Added session order-state cutoff test: RED (`reserved != dispatching`); implemented cutoff event replay. Final full command: **Ran 33 tests in 0.475s; OK**.

Other covered cases: journal imbalance/overflow/bool/float, same ID changed postings, injected second-posting/reservation failure with no partial rows, cash competition across proposals, cross-release action uniqueness, stale revision, forged assessor binding, missing FK/reference and exactly-one snapshot input, duplicate correction schema keys/immutable originals, SQLite busy failure, journal reopen persistence, restart latch retention, DISPATCHING crash/reopen UNKNOWN with capacity retained, immutable job/session duplicate behavior, and changed venue-event content. `git diff --cached --check` passed before commit. No live proof is claimed.

## Integration seams and deliberate fail-closed limits

The conductor approved deferring admission of reductions/replacements until Task 12 implements authority-issued semantic slots/parent/ordinal allocation. `reserve_intent` currently admits only the initial `entry` slot and rejects reducing/replacement proposals. The base reservation schema/check logic exists and capacity is preserved, but no caller can mint an arbitrary exit/replacement slot. Task 12 must add durable authority allocation and enable only those verified slots before any reduction can be sent.

Trusted dependency injection is:

```python
ledger.configure_risk_checks(
    *, limits: RiskLimits,
    market: Callable[[str], MarketRules],
    assess: Callable[[OrderProposal, AccountView, ControlState,
                      RiskLimits, MarketRules, int], RiskVerdict],
    boot_id: str,
    clock_us: Callable[[], int] | None = None,
    monotonic_ns: Callable[[], int] | None = None,
) -> None
```

Only Authority may wire this in-process; never accept a callback, code path, limits replacement, or market capability from control/HTTP/provider JSON. Unconfigured denies reservations/dispatch. UTC `clock_us` and boot-bound `monotonic_ns` remain separate. Hooks run synchronously within the short immediate transaction and must not perform I/O/await. At dispatch the assessor receives pending reservations excluding the order being rechecked; its AccountRevision remains current and includes that stored order.

Accounting/reconciliation persistence seam: `with ledger.transaction() as connection:` joins one synchronous immediate transaction, including nested journal operations; `ledger.connection` is the authority-owned sqlite connection; `ledger.invalidate_account(account_id)` clears readiness/increments its version after economics change. `_revision(account_id)` and `_account_view(account_id)` are internal projection helpers. `account_view` supplies a consistent read snapshot externally. No high-level mark/flow/heartbeat/reconciliation proof-acceptance methods were fabricated; those typed methods belong to Tasks 3/13 and should be added there.

Task 3 must persist verified `balance_positions`, immutable `equity_marks`, completed `reconciliations`, and `account_readiness` (private heartbeat, valuation version, readiness), with control's reconciled ID, in the same accounting transaction. It must provide full held-asset conservative liquidation evidence: current `account_view` is deliberately unready whenever a nonzero non-USD balance exists, and emits no invented Holding marks. Economic posting clears readiness. Task 3 must implement fill/native conservation, cost/fee/flow/NAV calculations and atomic compensations; this task supplies the immutable schema and USD primitive, not an opaque pretend fill-accounting success method. Authoritative terminal/fill/fee reconciliation must explicitly reduce/release reservation capacities; status alone never does.

Task 11 must supply full deterministic risk/Kelly/edge assessment and persist appropriate latches on mark/rejection boundaries. DB guards remain independent; incomplete or unbound risk never approves. Current `account_view` does not manufacture positive baselines or a private heartbeat from process uptime.

Startup can verify stored canonical JSON and the content identities for which this schema stores content. Remote event segment bytes, external code/config/dataset/tokenizer artifacts and semantic evidence observations require their owning validated import/archive registries in later tasks; this implementation does not claim to verify absent external bytes. Release core hashing excludes embedded gate approvals, per conductor's Task 15 clarification; Task 15 must use the same core definition and reconcile the older full-package `CandidateBundle.bundle_hash` property.

Session exports persist actual canonical replay payload in `review_sessions.export_json`, not a revision-only placeholder. They conservatively include shared causal source/artifact rows, reject cutoff-incomplete concrete references, omit nonhistorical readiness/balance/reservation/candidate projections, and retain causal journal/order/label evidence for rebuilding them. Later replay must reconstruct omitted projections and consume the stored payload. It must not reinterpret `database_export_hash` as a raw sqlite-file digest.

The single authority process/file lock, authenticated boundaries, typed validated import methods, release gate activation, full accounting/reconciliation, and complete deterministic risk remain owned by their later approved tasks. No runtime trading readiness is claimed for this intermediate component.

## Independent review remediation — R1–R5

All five findings in `.agenkit/sdd/task-02-review.md` addressed in commit **`98fffac`**, `fix: make ledger dispatch and historical exports durable`. Only `bot/autonomous/ledger.py`, `data/migrations/001_autonomous_ledger.sql`, and `tests/autonomous/test_ledger.py` changed in the commit. The earlier 33-test result above remains the historical initial implementation evidence; the remediation suite has **43 tests**.

- **R1:** `mark_dispatching()` rejects an existing SQLite transaction immediately, before any write or state mutation. It owns and completes its immediate transaction before returning a dispatching result. It never commits the caller's accounting transaction. Tests cover a previously committed reservation inside an outer transaction (state remains reserved, no dispatch event, and a subsequent nested journal posting still succeeds), and reserve-plus-dispatch in one outer transaction (rejected and rolled back).
- **R2:** `_EXPORT_TIMES` defines an explicit causal policy for every exported table. Observation, event, receipt, knowledge and segment range endpoints must be visible by cutoff; metadata such as future expiry/deadline/maturity is not incorrectly filtered. The nested venue result receipt in order-event JSON also must be causal. Unknown future tables fail closed until a policy is provided. Current `account_control` is omitted. New append-only `account_revision_history` stores journal sequence/version/generation atomically within every outer write commit; records have monotone account knowledge time incorporating observed economic/control timestamps. Session revision and account generation come from the latest cutoff-visible historical evidence, and the included journal watermark must agree. A delayed first export after later postings and reopen retains the original revision/generation. No export-time generation/revision is described as historical.
- **R3:** every order evidence row has a unique positive `append_sequence`, allocated durably inside the same immediate transaction as its evidence. `_append_order_evidence` is the common insertion primitive for reservation, dispatch, venue status and crash recovery. Replay filters by temporal visibility and then orders by this sequence, independent of UTC clocks and lexical IDs. Tests include one-tick reserve/dispatch and a regressing clock with reverse lexical OPEN/PARTIAL identifiers.
- **R4:** `_MANIFEST_KEYS` explicitly maps each manifest table to its actual primary key; snapshot inputs use the canonical ordered `(snapshot_id,line_no)` composite. `_manifest_entries` rejects duplicate record IDs. Long identities use a bounded SHA-256 representation of the full PK tuple. Tests cover repeated snapshots/evaluation runs under one release, multiple label events for one label, invalidations for one source event, composite snapshot input keys, and explicit duplicate rejection.
- **R5:** risk and order-event arithmetic boundaries run under a fresh explicit `Context(prec=100)`, covering venue modulo, notional/fee/cap arithmetic and cumulative quantity sums. They do not inherit the caller's precision or traps; Decimal failures become `ContractViolation`. Tests exercise 29-place aligned price/quantity values and exact cumulative sums with ambient precision six.

### Remediation RED/GREEN evidence

Command: `.venv-autonomous/bin/python -m unittest tests.autonomous.test_ledger -v`.

1. Added all ten regression cases before implementation. RED: **43 tests; FAILED (failures=8, errors=2)**. The failures reproduced nested dispatch authorization, future invalidation visibility, export-time revision, clock/ID replay ordering, manifest collisions/missing duplicate validation; the Decimal errors were the reviewed `decimal.InvalidOperation: DivisionImpossible` on valid high-scale modulo.
2. Implemented the five corrections. Intermediate run: one high-scale cumulative test fixture was correctly rejected for insufficient rounded fee bound after its earlier modulo failure was resolved. Raised that inert fixture's fee allowance from 1,000,000 to 1,000,001 microdollars; production fee-bound enforcement was preserved.
3. Final full command: **Ran 43 tests in 0.564s; OK**. `git diff --check` and `git diff --cached --check` passed before the remediation commit.

### Updated integration notes

`mark_dispatching` is explicitly non-nestable. Accounting may continue to nest `post` within `transaction`. Authority must reserve/commit, then call `mark_dispatching` separately, then submit only after it returns. No caller-owned transaction is silently committed.

The outer ledger transaction captures revision history immediately before COMMIT. Accounting/control collaborators must use `ledger.transaction()` for writes, rather than bypassing it with an independently committed raw connection; history and journal effects then share the same durability boundary. `account_revision_history` is new immutable schema evidence with its own append sequence and timestamp policy. Session revision denotes the latest proven cutoff revision, never the export-time current revision. The build-only initial migration checksum changed with the schema correction; old incompatible development DBs fail closed rather than being silently adopted or claiming new evidence existed historically.

The earlier fail-closed boundaries for reduction/replacement allocation, non-USD full valuation and the trusted assessor remain unchanged and were accepted by independent review. None of R1–R5 was deferred to those later tasks. No external or live calls were made.

## R2 re-review correction — committed evidence prefixes

Remaining R2 finding from the re-review at `98fffac` addressed in **`4612c39`**, `fix: bind historical ledger exports to committed evidence prefixes`. This commit changes only the same three owned files. R1/R3/R4/R5 behavior remains covered by the full suite.

The earlier history format proved the account revision but did not bind membership of other evidence to that commit. `account_revision_history.evidence_watermarks_json` now records the exact per-table SQLite row prefix for every exported evidence table, atomically with journal sequence/version/generation in the outer transaction. The history table itself is explicitly excluded from its watermark map, preventing a circular self-advancing prefix. Export selects a cutoff-visible committed history record **before** reading evidence; every other exported table is bounded by its watermark from that record, then by its explicit causal predicates. History rows in the payload are bounded by the selected history sequence. A timestamp-visible row from a later commit cannot enter an earlier revision's archive. Journal watermark agreement and concrete-reference checks remain mandatory.

History knowledge time is derived from the same complete `_EXPORT_TIMES` policies used by export, and also from nested `OrderEvent.result.received_at_us`. A history row for an outer receipt at T and nested receipt at T+100 cannot become visible before T+100. Knowledge remains monotone across clock regression. Evidence-table prefix changes cause history capture even when `AccountRevision` and generation are unchanged; a transaction with no revision or prefix change creates no duplicate history row.

### Exact additional TDD evidence

The following three tests were added first; full run was RED: **46 tests, FAILED (failures=3)**.

- `test_review_r2_event_before_commit_capture_uses_previous_committed_prefix`: dispatch row stamped T, history capture T+1, cutoff T; previously exported DISPATCHING under the RESERVED revision. Now returns RESERVED with only its committed reservation evidence.
- `test_review_r2_clock_regression_cutoff_preserves_committed_event_prefix`: reserve/dispatch at 80, OPEN at 100, later PARTIAL at regressed 90, cutoff 95; previously exported PARTIAL under the DISPATCHING revision. Now returns DISPATCHING with exactly the RESERVED/DISPATCHING evidence prefix.
- `test_review_r2_nested_result_receipt_participates_in_revision_visibility`: outer receipt T and nested venue receipt T+100; previously selected the later revision while excluding its event. Now the early cutoff retains the prior revision/state; cutoff T+100 includes both the later revision and OPEN evidence.

Then added `test_review_r2_non_economic_import_commits_new_prefix_without_revision_change`; targeted RED was **1 test, FAILED (failures=1)** because source-only import did not create history. It now proves that a committed new source observation is exportable despite unchanged journal/version, while a subsequent no-op transaction leaves history count unchanged.

After implementing the complete prefix binding, full command `.venv-autonomous/bin/python -m unittest tests.autonomous.test_ledger -v` was GREEN: **Ran 47 tests in 0.638s; OK**. Both diff whitespace checks passed before commit. No external calls, credentials, orders, or deployment occurred.

### Archive/import semantics for Tasks 4/8 and accounting collaborators

Validated source/snapshot/artifact/label imports must commit through `with ledger.transaction() as connection:`. Import-only commits produce a new evidence-prefix history record even without economic state changes. Raw autocommit insertion does not backdate committed proof and is not an archive import interface. Existing seed/manifest fixtures were updated to use this transaction seam with explicit fixture clocks, preserving their intended committed cutoff rather than sampling a cutoff before autocommitted writes.

An archive is the latest complete committed prefix proven knowable by its cutoff. It does not blend independently timestamp-visible rows from later commits into that prefix. If an exact prefix cannot be established, absent/invalid proof, journal mismatch or unresolved concrete references fail closed. The explicit visibility policies still distinguish future deadlines/expiry/maturity metadata from observations/receipt/knowledge timestamps. New exported table types require explicit policy and participate in the watermark map; history is the sole explicit circularity exclusion.

The row prefixes assume retained append-only evidence tables and the existing immutable-row protections. No evidence reordering, rowid rewriting, VACUUM-based table rewrite, or destructive retention operation is provided by this component. Any future storage compaction/migration must preserve or deliberately migrate these committed prefix identities before enabling exports. Persisted completed archive JSON remains immutable. The initial build-only schema checksum changes again; incompatible development databases continue to fail closed.

## R2 session-anchor closure — commit `57509ea`

Addressed the final re-review finding at `4612c39` in **`57509ea`**, `fix: require session anchors inside committed export prefix`. Only owned `bot/autonomous/ledger.py` and `tests/autonomous/test_ledger.py` changed; no schema change was necessary.

The unrestricted current-table reconciliation lookup was removed. After prefix-bounded evidence selection and concrete-row reference checks, `_validate_session_anchors` now requires the exact account-bound completed reconciliation named by `SessionCutoff` to exist inside the exported payload and satisfy every reconciliation causal timestamp predicate. It also validates the account creation anchor and the cutoff-visible revision/generation proof against the payload's latest included history record. New sessions are constructed/persisted only after these checks. The same validator runs when returning a cached session and when scanning immutable sessions at startup, so a previously stored archive cannot pass merely because its JSON hashes are internally consistent while its anchors are absent.

Self-review of this API's concrete anchors covered `cutoff.account_id`, `cutoff.reconcile_id`, and `SessionArchive.account_revision`/historical generation. Account creation must be present by cutoff; the reconciliation must belong to that account and be completed within the selected prefix; revision/generation must match included historical evidence. Other concrete row references remain checked by the existing payload FK scan. Session/manifest hashes bind the resulting payload and scalar cutoff. No unrelated reconciliation/accounting functionality was added.

### Exact test-first evidence

Before implementation, added:

- `test_review_r2_reconciliation_anchor_requires_selected_committed_prefix`: insert R through `ledger.transaction()` with row timestamps T and history capture T+1; cutoff T must reject without a session row; cutoff T+1 must succeed with R in the payload.
- `test_review_r2_session_account_anchor_requires_creation_prefix`: pre-creation cutoff and wrong account reject without a session row.

RED command:

```text
.venv-autonomous/bin/python -m unittest tests.autonomous.test_ledger.LedgerTests.test_review_r2_reconciliation_anchor_requires_selected_committed_prefix tests.autonomous.test_ledger.LedgerTests.test_review_r2_session_account_anchor_requires_creation_prefix -v
Ran 2 tests in 0.016s
FAILED (failures=1)
```

The reconciliation regression failed with `AssertionError: ContractViolation not raised`; the analogous account-anchor cases already passed and remain covered.

GREEN command and output after the fix:

```text
.venv-autonomous/bin/python -m unittest tests.autonomous.test_ledger -v
Ran 49 tests in 0.654s
OK
```

`git diff --check` and `git diff --cached --check` passed before commit. No external calls, live credentials, orders, or deployment occurred. No additional integration restriction or task deferral was introduced.
