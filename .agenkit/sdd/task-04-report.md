# Task 4 implementation report

Task: immutable causal source history and durable sequence-aware book archives.
Implementation base: `4e57061a10fa16a3a9131a9954e831d225a20c1c` (recorded requested base); accepted Task 3 through `a4fbd8daf554e39372f429aa12954d4924073a3a`. Checkout HEAD before this commit: `c0f510375e98f9bdea19bb1040679ff108c6004f`. Existing authorized writable checkout fallback was used. Other work and untracked operator files were preserved.

Read using-agenkit (subagent stop applies), subagent-driven-development, test-driven-development and verification-before-completion skills; read local memory fallback, Task 4 brief/guidance/integration map, global constraints, relevant architecture causal/archive/retention/restart sections and safety audit. No child agent dispatched. Independent spec/quality review is performed by the parent workflow.

## Implemented scope

Only `bot/autonomous/ledger.py`, new `bot/autonomous/state.py`, new `tests/autonomous/test_causality.py`, new `tests/autonomous/fixtures/causal_book.jsonl` and this report are owned/committed. No schema or contract file changes. Existing migration checksums, `_EXPORT_TIMES`, `_MANIFEST_KEYS`, accounting, real manifest identity and historical-prefix compatibility policies remain intact.

- `causal_visible` matches the controlling five-argument predicate, including exact nonnegative-int checks and receipt <= knowledge. Frozen `CausalCutoffs` are required for selection. Selection rejects an open transaction, chooses latest eligible `(known_at_us, revision, event_id)` per `(source, stream, logical_key)`, and applies knowledge- and recording-timed invalidations without changing prior source rows.
- Explicit frozen typed `SourceClockPolicy(policy_id, source, stream, max_age_us, max_future_skew_us)` is required, with no default source clock allowance. The configured policy tuple and read-only mapping are retained; each book segment archives the exact policy. Historical candle import stamps receipt/knowledge at local import time and still requires an explicit policy permitting its historical age.
- Exact duplicate source delivery is idempotent. Both event-ID and source/logical-key/revision identity collisions reject changed receipt, timestamps or content. Supersession must target a prior revision of the same logical source/instrument.
- Books require a sequenced instrument snapshot. Deltas are contiguous per `(source, stream, instrument)`. Deletions are applied before revalidating a complete `BookPayload`; crossed or empty resulting books reject. Different identities at the same sequence, gaps and out-of-order updates fail closed. Findings invalidate the last admitted source event durably, so reopening cannot restore the old reconstruction as healthy. A full snapshot known strictly after the finding reestablishes continuity; recovery produces the same health result.
- A read-only ledger readiness property derives health from its attached engine. Construction failure makes the source dependency required even with an empty DB. Persisted source rows without a recovered engine block entry. Reserve and dispatch recheck current health. Current source invalidation follows `snapshot_inputs` event/segment lineage and rejects eligibility without updating immutable snapshots or decisions. `invalidated_snapshots()` exposes affected IDs for pending-judgment consumers. Because the existing segment table omits stream, segment invalidation is conservative across matching source/instrument/sequence ranges.

## Archive format and durability

`causal-book-v1` is canonical ASCII JSON with exactly `format`, original typed `event`, full post-event `checkpoint`, and typed `policy`, compressed using standard-library zlib level 9. Version 1 writes one event per segment and a full checkpoint after every accepted event; this is a deliberately conservative bounded format, not a claim of production storage throughput. Checkpoint identity is SHA-256 of canonical `('book-checkpoint-v1', BookPayload)`; segment identity is SHA-256 of the actual compressed bytes. These hashes have distinct domains and purposes.

Default maximum compressed and decoded segment sizes are each 1,048,576 bytes; injected smaller positive bounds are supported. File type/size is checked before bounded reading. Decoder output is capped before materialization; truncated, trailing, oversized, noncanonical and unknown-format data fail closed. Typed event/checkpoint/policy decode and manifest source/instrument/sequence/event/receipt/byte/checkpoint comparisons precede replay.

Archive keys are exactly `<64 lowercase SHA-256 hex>.zlib`, relative to the injected authority-owned root. Traversal, absolute keys, pre-existing root/ancestor symlinks and symlink leaves reject. File opens and rename use directory descriptors and `O_NOFOLLOW`; leaf file types must be regular. The root is an authority-owned configuration boundary, not an event path input.

Publication order: exclusive same-directory temporary file -> write -> flush -> file fsync -> atomic same-filesystem rename -> directory fsync -> short SQLite transaction inserting original source row and manifest together. No archive I/O or compression occurs inside the SQLite write transaction. Failure after durable rename can leave an orphan, never a committed manifest referring to partial bytes. A failed/uncertain DB commit inhibits entries. Recovery verifies committed manifests and source identity, reconstructs only those segments, verifies digest-named orphan bytes without adopting them, and reports corruption/missing/unknown/partial files through `recovery_errors` while inhibiting entry. Repair and explicit recovery are required; it does not delete evidence.

## Observed TDD and verification

Interpreter: `.venv-autonomous/bin/python --version` -> `Python 3.12.14`.

Initial RED command:

```
.venv-autonomous/bin/python -m unittest tests.autonomous.test_causality -v
Ran 11 tests in 0.002s
FAILED (failures=11)
```

All 11 failures were explicit `AssertionError: causal state implementation absent`, rather than an accidental test import error. First implementation run exposed an SQL insert placeholder count bug (3 failures, 2 errors); correction produced:

```
.venv-autonomous/bin/python -m unittest tests.autonomous.test_causality -v
Ran 11 tests in 0.094s
OK
```

Next RED/GREEN group added immutable invalidation, entry health and exports. Fixture setup initially used an incorrect release column layout/manifest field; those test setup errors were corrected before feature verification. Observed remaining failures were lack of source-health checks, late-recorded invalidation visibility, and the initial attempted UPDATE rejected by existing immutable snapshot triggers. The implementation was corrected to derive invalidation eligibility without any UPDATE. Then:

```
.venv-autonomous/bin/python -m unittest tests.autonomous.test_causality -v
Ran 15 tests in 0.126s
OK
```

Self-review RED introduced durable sequence-gap restart and committed-selection requirements:

```
.venv-autonomous/bin/python -m unittest tests.autonomous.test_causality -q
Ran 18 tests in 0.155s
FAILED (failures=2)
```

Failures were old reconstruction incorrectly healthy after gap recovery, and selection inside an uncommitted transaction. After durable findings and committed-read guard:

```
.venv-autonomous/bin/python -m unittest tests.autonomous.test_causality -q
Ran 18 tests in 0.160s
OK
```

Final group added root-construction failclosed RED and the synthetic fixture. The other tests verify real COMMIT uncertainty, current invalidation at dispatch, and compression/version boundaries:

```
.venv-autonomous/bin/python -m unittest tests.autonomous.test_causality -q
Ran 23 tests in 0.215s
FAILED (failures=2)
```

Failures were source readiness remaining true after failed root construction and absent fixture. After implementation/fixture:

```
.venv-autonomous/bin/python -m unittest tests.autonomous.test_causality -q
Ran 23 tests in 0.219s
OK
```

Full-regression command was initially mistyped with nonexistent `tests.autonomous.test_risk`; that run returned `ModuleNotFoundError` (145 tests, 1 error), not a passing verification. Correct module is `test_risk_limits`. The intermediate corrected run passed 148 tests. Pre-review observed full command:

```
.venv-autonomous/bin/python -m unittest tests.autonomous.test_contracts tests.autonomous.test_ledger tests.autonomous.test_risk_limits tests.autonomous.test_causality -v > /tmp/task04-final-tests.log 2>&1
Ran 156 tests in 2.392s
OK
```

Exit code 0. That run included all 133 prior contract/ledger/risk tests plus 23 causality tests; the current `/tmp` log was subsequently replaced by the final run below. `git diff --check` exited 0 without output.

Concrete failure/recovery cases include write, flush, file fsync, rename and directory fsync injection; source/manifest transaction capture failure; actual SQLite `COMMIT` failure with dispatch disabled; orphan verification/no adoption; corrupt and missing committed bytes; digest/path/symlink rejection; compressed and decompressed bounds; zlib bombs, truncation and trailing bytes; unknown encoding version; byte-count/checkpoint/range manifest mismatch; post-cutoff revisions; same-event receipt conflict; equal-known revision ordering; knowledge/record invalidation cutoffs; duplicate/gap/out-of-order sequences; crossed/empty reconstructed deletion state; full snapshot reset; reopening without recovery; source+segment committed-prefix export; immutable pending decision/snapshot evidence; current invalidation blocking dispatch.

## Self-review and boundaries

The implementation was checked against the controlling Task 4 brief/guidance. Self-review found and corrected the durable-gap recovery and root-construction readiness gaps through observed RED/GREEN tests. Numeric features, compact encoding and provider token counting remain Task 5; none is implemented. The fixture is synthetic offline software evidence, not live market, causal historical availability, source finality or predictive validation evidence. No source clock policy is a release approval. No provider/network, credentials, orders, services, deployment, activation, Notion, merge or Task 5 action occurred.

Authority startup integration must construct `StateEngine` with its configured root/policies before source-based entry. A reopened ledger with persisted source evidence is inhibited until that recovery. Existing empty-source paper fixture compatibility remains; no arming/reconciliation behavior was changed. Source health does not grant an entry permit, reset any account latch, or prove freshness/calibration/tokenizer eligibility. One-event/full-checkpoint archive storage is conservative and may need a future versioned batching format after measurement. No retention deletion or orphan adoption policy is introduced.

## Independent review corrections and final verification

The independent reviewer reproduced four Important blockers in the first source draft. All were reproduced test-first and corrected within the five owned files:

1. A replaced old engine could admit from a stale projection. The current ledger engine owns mutation and reads; old instances now fail before ingest/recover/select/book/invalidation and cannot rebind themselves via a health update.
2. Archive failure inhibition could disappear on recovery. A failed update appends a durable ordinary invalidation of the last admitted event when persistence remains available, requiring a full snapshot known after the finding. When persistence is unavailable or uncertain, a verified later orphan itself establishes a source sequence/knowledge floor. Recovery never adopts it or restores the older book as healthy; a fresh full snapshot must overtake the orphan before health can recover.
3. A delta checkpoint depends on its base snapshot and intervening deltas. Derived invalidation now follows ancestors within the same source/stream/instrument until the next full snapshot. A later reset restores new source health while leaving the old frozen decision lineage invalid, for both direct delta event and segment references.
4. Automatic source supersession was missing. New observations and reason-coded `source_revision_superseded` findings commit together. Ranking is `(known_at_us,revision,event_id)`, so a late older rank never supersedes the newer observation. Informational supersession findings invalidate snapshots frozen before their knowledge/record boundary; ordinary source selection ignores this informational reason, preserving pre-revision history and deliberate fresh fallback after a newer revision is invalidated. Public `invalidate` rejects the reserved internal reason, preventing callers from disguising an ordinary source invalidation.

Observed review RED:

```
.venv-autonomous/bin/python -m unittest tests.autonomous.test_causality -q
Ran 26 tests in 0.251s
FAILED (failures=3)
```

Those were stale-engine admission, archive recovery clearing inhibition, and transitive lineage. After fixes and adjusting the existing recovery test to stop calling a deliberately fenced old owner: 26 tests in 0.240s, OK; 159 full tests in 2.402s, OK.

Supersession RED: same causal command, 28 tests in 0.266s, `FAILED (failures=1)` at missing pending-lineage invalidation. After atomic informational findings and frozen-boundary derivation: 28 in 0.264s, OK.

Reserved-reason/orphan RED: same causal command, 31 in 0.296s, `FAILED (failures=3)` (forged informational reason and two later-orphan recovery cases). After reason reservation and orphan sequence/knowledge floors:

```
.venv-autonomous/bin/python -m unittest tests.autonomous.test_causality -q
Ran 31 tests in 0.287s
OK
```

Final full observed command, exit 0:

```
.venv-autonomous/bin/python -m unittest tests.autonomous.test_contracts tests.autonomous.test_ledger tests.autonomous.test_risk_limits tests.autonomous.test_causality -v > /tmp/task04-final-tests.log 2>&1
Ran 164 tests in 2.458s
OK
```

This is all 133 accepted prior tests plus 31 causal/archive tests. The verbose log is retained locally in `/tmp/task04-final-tests.log`; command exit and final output were inspected. `git diff --check` again exited 0 with no output. Independent reviewer will verify the resulting commit separately. No Task 5 work was started.
