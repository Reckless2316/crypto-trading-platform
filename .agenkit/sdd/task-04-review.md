# Task 4 independent review

Final review: **PASS — Stage 1 and Stage 2**, no open Critical, Important or Minor findings. Reviewed final commit `26dfa8dc56da3f91ad41f7472889c2ae1b0816cb` (`feat: persist causal source history`). Requested base `4e57061a10fa16a3a9131a9954e831d225a20c1c`; initial inspected checkout HEAD `c0f510375e98f9bdea19bb1040679ff108c6004f` plus Task 4 working files. The initial findings below are retained as resolved review evidence; the final verdict follows them. Scope: ledger integration, state engine, synthetic fixture, causality tests and implementation report. Read requesting-code-review skill, local-memory fallback, Task 4 brief/guidance/integration map/global constraints, safety audit and controlling causal/archive/recovery architecture. No source/test changes or commits by reviewer; only this report and inert temporary execution.

## Initial Stage 1: specification compliance

Initially blocked by four independently reproduced Important findings. No Critical finding. Three-cutoff predicate, immutable source/revision rows, separate explicit source policies, bounded canonical compressed format, durable publication ordering, committed-only replay, orphan non-adoption, cutoff exports and existing entry hooks are otherwise present. Numeric features/provider/live integration are outside Task 4.

1. **Important — stale engine can commit conflicting sequence history.** `bot/autonomous/ledger.py:198` replacement/rebinding policy and `bot/autonomous/state.py:284`–`307` validate continuity against each object's private projection rather than a single current owner. Reproduction: engine A admits snapshot sequence 1; construct same-config engine B; B admits delta sequence 2; A admits a different delta sequence 2 under a new event/logical identity. Both return accepted and ledger entry readiness is true. Recovery then reports `book sequence discontinuity`. The SQL schema has no sequence-identity uniqueness, so this violates immutable continuous recoverable history. Fence stale owners or prove projection matches durable tip before any write.

2. **Important — recovery clears lost-update health after archive failure.** `bot/autonomous/state.py:295`–`296` marks only volatile archive failure; `:218`–`248` can replay the previous committed prefix and clear it. Reproduction: admit snapshot sequence 1; inject OSError in archive.write for delta sequence 2; failure inhibits entries; call recover() without admitting missing delta or a new full snapshot; entries become ready with the old snapshot. A lost known update cannot be repaired merely by verifying older bytes. Preserve a durable source-health finding requiring a validated later snapshot after the failure.

3. **Important — checkpoint lineage invalidation omits predecessor dependencies.** `bot/autonomous/ledger.py:214`–`224` only matches the invalidated event directly or inside a segment's own sequence interval. Reproduction: snapshot s1, delta d2; persist snapshot_inputs referencing only d2's segment (2..2); invalidate s1. `_source_lineage_invalid` returns false although d2's archived checkpoint derives from s1. Admit fresh reset snapshot10; global health becomes ready while that pending snapshot still appears valid. The same risk applies to direct delta references. Follow the checkpoint's base snapshot and intermediate delta dependencies through the next full reset, or conservatively invalidate affected lineage.

All four reproductions ran locally against actual Ledger/StateEngine objects and real temporary SQLite/archive files; none used a provider/network/service. Findings sent to worker and lead for test-first fixes.

## Initial Stage 2: quality and verification

Initial verdict: request changes for the above Important correctness/recovery defects. No additional Minor issue recorded. Reviewer independently ran all 23 initial causality tests: exit 0, `Ran 23 tests in 0.223s`, `OK`. Existing tests do not cover the reproduced failures. Preserve existing Tasks 1/2/3 contracts/accounting, migration/export policies and immutable evidence while fixing them. Final re-review and regression results follow below when available.

4. **Important — admitted source revision leaves pending predecessor judgments eligible.** Initial `bot/autonomous/state.py:279`–`282` validates supersedes identity but ingestion never appends a revision finding; ledger eligibility sees only explicit invalidations. Reproduction: metric a revision0, snapshot_inputs referencing a, then admit changed b revision1 same logical key with supersedes_id=a. Acceptance is true, source_invalidations count is zero, and `_source_lineage_invalid(snapshot)` remains false. Controlling specification says superseded decisions must not act; architecture failure matrix requires delayed revisions to append findings and invalidate pending judgments. Lead confirmed this is required Task 4 behavior. Preserve pre-revision historical selection and immutable decision evidence; enforce current eligibility when a newer eligible revision supersedes the predecessor rather than depending on an optional caller invalidation.


## Final Stage 1: PASS — specification compliance

Re-reviewed final source commit `26dfa8dc56da3f91ad41f7472889c2ae1b0816cb` against original base `4e57061a10fa16a3a9131a9954e831d225a20c1c` and controlling Task 4 requirements. No open Critical or Important finding.

- Finding 1 resolved: `state.py:203`, `:208`, `:214` establish a single current ledger owner and fence replaced engines before read/mutation/recovery. Regression tests reproduce the formerly conflicting sequence admission.
- Finding 2 resolved: `state.py:264` persists ordinary source-health invalidation on failed book/archive update. Recovery retains the finding; a later validated full snapshot is required. Verified uncommitted orphan evidence also establishes sequence/knowledge floors (`state.py:247`) when a failed/uncertain database cannot persist a finding. Orphans are not adopted.
- Finding 3 resolved: `ledger.py:214` derives predecessor ancestry for direct delta and segment references, stopping at a full reset. A fresh full reset restores current feed health while affected old frozen snapshots remain invalid.
- Finding 4 resolved: `state.py:324` atomically appends informational supersession findings ranked by `(known_at_us, revision, event_id)`. `ledger.py:214` checks their knowledge/record boundaries against frozen snapshot cutoffs. Per lead clarification, informational supersession affects outstanding eligibility while source selection retains historical/fallback behavior; ordinary invalidations still exclude source observations. Older-rank arrival cannot invalidate newer lineage. Public `invalidate` rejects the internal reserved reason (`state.py:354`), closing the reproduced follow-up where arbitrary callers could create an invalidation ignored by recovery.

Required scope is present: exact three-cutoff predicate, typed explicit per-stream source clocks, immutable identity conflict rejection and historical import times, knowledge-timed revision/invalidations, sequence continuity and validated deletion reconstruction, separate archived-byte/checkpoint hashes, versioned canonical bounded compressed encoding, confined digest paths, file flush/fsync/rename/directory-fsync before SQLite insertion, committed-only verification/replay, bounded verified orphans without adoption, persisted entry inhibition, concrete lineage eligibility, and committed-prefix export preservation. Schema/migration checksums, explicit export policies and prior accounting/risk contracts are unchanged. Only owned source/test/report paths were included in the implementation commit.

## Final Stage 2: PASS — quality and independent verification

No open Critical, Important or Minor findings. Code keeps archive I/O outside SQLite write transactions, uses strict typed decoding and bounded output before allocation growth, preserves immutable decision evidence, and exposes expected failures through rejection/recovery health. Synthetic fixtures and deterministic failure injection exercise the required boundaries. Existing authority startup still needs to construct StateEngine; source-bearing ledgers fail closed before recovery. Numeric features, token/provider verification and live startup integration remain the later tasks assigned by the approved plan.

Reviewer independently ran at the final SHA:

```
.venv-autonomous/bin/python -m unittest tests.autonomous.test_contracts tests.autonomous.test_ledger tests.autonomous.test_risk_limits tests.autonomous.test_causality -v
Ran 164 tests in 2.475s
OK
```

Exit code 0: all 133 accepted prior tests plus 31 Task 4 tests. Reviewed the new regression cases for all findings and the uncertain-commit/orphan boundary. `git diff --check` exited 0; `git diff HEAD --` on all four source/fixture/test paths was empty, proving verification covered the committed source rather than further working changes. No providers, network, credentials, live orders, services, deployments, Task 5 edits or source/test commits were performed by the reviewer. The review itself remains a local report for the lead workflow.
