# Cursor reflector review — 2026-10-17

Verdict: changes required for the uncommitted execution/worker/dashboard integration.
This is a review checkpoint, not acceptance of Tasks 11–22 or authorization to trade.

## Scope and provenance

- Committed baseline: `b2555dc`.
- Reviewed working-tree additions: `bot/autonomous/execution.py`, `worker.py`,
  `paper_cycle.py`, and their focused tests; relevant edits in `ledger.py`,
  `state.py`, and `dashboard/backend/app.py`.
- These additions and edits were uncommitted during review. Publishing this report
  does not publish or approve that source. Paths/line numbers below describe the
  local working tree and can move while Cursor edits it.
- Requirements: Task 12 order authority, Task 16 hourly worker, and Task 17 legacy
  dashboard/control integration, as recorded in the implementation plan and task briefs.
- Source review and offline synthetic reproduction only. No exchange/provider
  requests, service activation, control changes to running accounts, or deployment.

## Findings

### R1 — Important: replacement dispatch book skips sizing validation

`bot/autonomous/execution.py:221` invokes `entry_verifier` on the first book.
Lines 238–248 fetch another book, check its identity/age, and send it to the venue
without rerunning that verifier. The authority also retains the initial market
rules object for the venue call. The ledger's risk checks are a separate boundary;
they do not repeat the entry spread/impact/depth-participation calculation against
this replacement book.

Offline reproduction using `AuthorityTests` and its temporary ledger:

1. Return ask depth `5` on the first book-source call and `0.01` on the second.
2. Install a verifier that records depth and refuses depth below `5`.
3. Submit the existing fixture proposal.

Observed: receipt `completed/canceled`, verifier depths `["5"]`, two book reads,
one venue call, and one fill of `0.01` at `100`. The configured verifier would
refuse the actual dispatch book, but never sees it. The reproduction uses a simple
injected verifier, not the full calibration fixture. Production sizing's
`depth_qty * MAX_DEPTH_PARTICIPATION` rule (`sizing.py:143`) establishes why this
matters: the order can consume 100% of the replacement visible depth despite the
1% entry participation rule.

Acceptance: apply the entry economics checks to the book and current verified
capabilities actually dispatched. Exclude this intent's own reservation when
recomputing capacity, while retaining competing reservations. A changing-book
regression must prove zero venue calls on refusal, correct undispatched release,
and no accidental rejection from double-counting the intent's own capacity.

### R2 — Important: dashboard kill has a fixed ledger/account target

`dashboard/backend/app.py:1097–1112` updates the legacy dashboard database, then
conditionally kills `/tmp/paper-serve-ledger.sqlite3`, account `paper-test`.
`worker.py:489–490` accepts arbitrary `--ledger` and `--account` values.
Consequently a worker using another configuration is not controlled by this
button. If the fixed file is absent, the route still returns `status="killed"`
with `ledger_hard_killed=false`.

Acceptance: route control through the configured authority/account and report
unavailable delivery as a failure. Tests must use a nondefault temporary ledger
and account, verify the intended hard-kill latch, and cover missing authority.
Opening an authority ledger for a control request also invokes startup recovery;
avoid introducing collateral recovery/disarming of unrelated accounts.

### R3 — Important specification gap: executable worker lacks hourly decisions

`worker.py:569` constructs `PaperWorker` with a `SimpleNamespace` containing only
a paper-venue label. `serve()` at line 299 observes books and runs clock reviews;
it never invokes `on_close`, constructs decision inputs, or submits an order.
Helper tests that call `on_close`/`dispatch_proposal` directly do not establish an
executable hourly trading worker. Collection-only behavior is valid as a partial
milestone, but Task 16 remains incomplete.

Acceptance: either explicitly retain a collection-only milestone or implement
the approved startup/hourly workflow with injected dependencies, durable candle
deduplication, and restart behavior. An entrypoint-level test must demonstrate
the actual loop reaching the decision boundary once per new close.

### R4 — Important specification gap: committed book lineage is not enforced

`execution.py:194–199` checks type, instrument and observation age, but does not
verify committed source lineage. Its constructor documents trusted source wiring;
the Task 12 requirement also calls for refusing uncommitted lineage. Existing
authority fixtures dispatch a book with fabricated `"e" * 64` lineage.

This demonstrates a missing enforcement/integration test, not an established
remote exploit: submit callers cannot directly pass a book. Acceptance requires
production engine/capability wiring and an integration regression proving that
uncommitted, invalidated, or unverified input cannot reach reservation/dispatch.

## Verification

The working-tree suites below passed: **62 tests, zero failures/errors**.
Worker debug logging was mocked during this run; no production source was changed.

```text
tests.autonomous.test_worker
tests.autonomous.test_paper_cycle
tests.autonomous.test_execution_recovery
```

The R1 reproduction independently produced the output documented above. Passing
these existing suites does not close R1–R4 or establish complete plan compliance.

The committed baseline was separately exported with `git archive b2555dc` into
`/tmp/reflector-baseline-b2555dc` and tested without the working-tree additions:
`python -m unittest discover -s tests/autonomous -t . -q` passed **360 tests**.
That run emitted unclosed-SQLite ResourceWarnings; these were not test failures.
This is the autonomous suite, not a full repository/API/deployment verification.
An independent reviewer checked this report's findings and scope distinctions
and returned no blocking corrections.

## Next review batch

1. Close R1 with the changing-book regression.
2. Close R2 with a configured-account control test.
3. Keep R3/R4 explicit in task acceptance until integration tests pass.
4. Recheck the prior Task 11 asset-cap and exit-latch findings against current
   source; this review does not claim they are resolved.
5. Commit each accepted implementation batch with its own test evidence. Keep
   Cursor's unreviewed additions, local agent state and recovery artifacts out of
   the review-only commit.
