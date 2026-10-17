# Task 6 implementation handoff — 2026-10-17

Status: **review revisions R1–R5 implemented; independent re-review pending**.
All six real candidates remain ineligible; only complete synthetic proof fixtures can pass admission. No
provider, venue, credential, or network operation was performed by this worker.

## Scope and baseline

Baseline HEAD: `60dff6c48fe56a7f709697d18b6098574835cadc`.
Changes are uncommitted working-tree files. Do not interpret HEAD as containing
this implementation. Existing Task 6 WIP was preserved and hardened, not replaced.

Owned files:

- `research/autonomous/__init__.py`
- `research/autonomous/evidence.py`
- `research/autonomous/valuation.py`
- `config/autonomous/schemas/aave.json`
- `config/autonomous/schemas/hype.json`
- `config/autonomous/schemas/pendle.json`
- `config/autonomous/schemas/aero.json`
- `config/autonomous/schemas/ldo.json`
- `config/autonomous/schemas/ondo.json`
- `tests/autonomous/test_research.py`
- this report

Shared contracts, ledger, state, Jev, risk, execution, release and candidate
integration were not modified by this worker.

## Implemented behavior

`EvidenceSource` consumes caller-reviewed source definitions and injected
adapters; it does not discover sources or relabel secondary aggregates as primary.
Bindings include URL, metric, exact source unit, adapter identity, source-definition
hash, maximum age and finality policy. Raw bytes must match their SHA-256.

The source now requires an explicit local `artifact_root`. It preserves exact
capture bytes before issuing observations, using directory-relative opens with
no symlink following, exclusive creation, file/directory fsync, and exact-byte
comparison of existing captures. A conflicting path is never overwritten.
Storage errors propagate; they do not issue an admitted observation. Whole batches
are validated before any observation is issued.

Exact causal bounds distinguish observation event, publication, request, receipt,
period and valuation cutoff. Receipt upper bounds cannot substitute for exact
receipt. Event/publication cannot follow receipt; period end cannot follow the
observation event. Chain evidence requires complete chain, contract and canonical
block identity. The metadata decoder validates exact integer types before time
comparisons and rejects malformed/noncausal nullable timestamps.

`missing_evidence` rejects null, booleans, nonnumeric objects and nonfinite numbers.
A negative measured net revenue remains a measured value; it cannot create an
admission because the independent thesis gates still block. Quantities and units
are not silently inferred from arbitrary API strings.

The six research JSON Schemas retain the approved decision enums, explicit
unknown/status types and candidate-specific source-gap rows. They now also expose
the exact canonical thesis clauses as metadata. Research row IDs remain explicitly
unmapped; schemas explicitly label production admission disabled. AAVE current
budget/pause proof, HYPE fee allocation/issuance, PENDLE cash/points and physical
versus virtual units, AERO incentives/locked accrual, LDO NEST deficit, and ONDO
entitlement/spot restrictions remain visible gaps.

## Verification

All test values are synthetic fixtures, never current market measurements.

1. Existing baseline:
   `.venv/bin/python -m unittest tests.autonomous.test_research -v` — 13 tests PASS.
   This interpreter is Python 3.14.4, so it was not used for final compatibility evidence.
2. New tests first exposed missing raw persistence, acceptance of arbitrary
   quantitative objects, noncausal capture times and incomplete chain identity.
   The first targeted run had 1 failure/4 errors for the missing artifact-root API;
   after introducing the constructor argument, the two causal/chain tests failed
   with `ValueError not raised`. Decoder timestamp tests exposed a `TypeError`
   before the type-validation fix. The new schema metadata test first exposed
   the missing canonical-clause key.
3. Final supported-runtime command:
   `.venv-autonomous/bin/python -m unittest tests.autonomous.test_research -v`
   — **20 tests PASS**, Python 3.12.14, 0.026 seconds.

Coverage includes raw capture preservation/conflict, parent-symlink escape,
primary-source bindings, unknown receipt/hash failures, stale/future/unit mismatch,
timestamp integer types, publication/period causality, complete chain identity,
mutated payload/finality bindings, disclosure versus quantitative proof,
condition mapping inhibition, evidence expiry and six schema contracts.

## Review revisions R1–R5

The original review correctly rejected a permanently inhibited evaluator and
incomplete provenance/normalization support. Those omissions are now addressed
without modifying the accepted shared contracts.

- **R1 — real deterministic admission path:** typed `GateKind` and
  `AdmissionGateProof` records are built from registry-verified primary
  observations. Independent thesis approval, bear-model approval, contradiction
  clearance, listing/depth, FDV denominator convention, financial measurements
  and exact canonical thesis-clause proofs are required. Bear value is explicitly
  classified as an **estimate**, never an executed market fact; its model approval
  is a separate executed approval record. Bear discount must survive the cost
  fraction. Market cap/FDV must reconcile to their explicit supply denominators.
  Financial periods must cover exactly 90 days and user counts exactly 30 days;
  fabricated unit labels cannot replace period bounds. Expiry is checked again
  at explicit `evaluated_at_us`. Synthetic complete evidence passes, and removing
  any independent gate rejects it. Research-gap row IDs remain unmapped.
- **R2 — normalization and offline ingestion:** pure `normalize_supply`,
  `normalize_cashflow`, `normalize_tvl` and `normalize_concentration` functions
  reconcile excluded balances and forward sellable unlocks, preserve reserve
  purchases separately from burns, subtract rebates/referrals/incentives/costs
  from gross fees, require qualified cash attribution, value unique economic
  claims with USD marks at one cutoff and reject unproved beneficial ownership.
  Unknown inputs stay `None`. `CapturedProtocolAdapter` ingests immutable captured
  contract/API JSON through locally reviewed injected decoders; it preserves raw
  bytes, omits unknowns and rejects duplicate JSON keys and ambiguous RPC IDs.
- **R3 — bounded freshness:** envelope expiry cannot exceed event timestamp plus
  the trusted source maximum age. The registry repeats that check on replay.
- **R4 — durable verification:** `EvidenceRegistry` owns a distinct local SQLite
  database, version 1, directly within an explicitly configured artifact root.
  Every operation opens/closes its own connection. Registry writes use atomic
  FULL-synchronous transactions; unsupported versions reject. Replay requires
  locally supplied trusted source definitions plus a registered exact observation
  hash and reverified artifact bytes. A database does not promote its own source
  definitions to trust. Missing trust, changed bytes, changed registry metadata or
  unsupported versions reject. No process-global issuance set remains.
- **R5 — pinned source identity:** source binding hashes include chain ID,
  contract and instrument, and captures/replay must match all three exactly.
  Complete wrong-chain, wrong-contract and wrong-instrument tests reject.

Final supported-runtime command after revisions:

`.venv-autonomous/bin/python -m unittest tests.autonomous.test_research -v`

**36 tests PASS**, Python 3.12.14, 0.547 seconds. The suite includes an initial
failing stale-expiry test, missing pinned-identity API, synthetic admission failure,
and a failing short-window relabelling test before their corresponding fixes.
Two asyncio diagnostic lines report slow synthetic test coroutines because they
perform deliberately synchronous fsync/SQLite writes; there are no test failures.

## Integration and operational limits

1. Callers must supply `EvidenceSource(..., artifact_root=...)`. To assess/replay
   persisted evidence, pass the explicitly configured `EvidenceRegistry` to
   `evaluate_thesis(..., registry=..., evaluated_at_us=...)`. Omitting registry
   trust fails closed. The registry is research-owned and separate from the
   execution ledger; it has no account credentials or order authority.
2. The database/artifact directory and source definitions are trusted local
   configuration. Hashes detect accidental/partial tampering; this component does
   not claim to resist an attacker who can rewrite all trusted code/configuration
   and the complete database and artifacts together. File permissions and process
   deployment boundaries remain the operator/authority layer's responsibility.
3. Actual current protocol endpoint contracts, completeness/finality coverage,
   supply exclusions and beneficial-owner evidence still need independent
   qualification before configuring production decoders. The injectable adapter
   and normalization code do not invent those missing inputs. No current source
   gaps were filled and no real candidate was admitted.
4. Full provenance remains in the versioned `SourceEvent.stream` envelope to
   preserve existing accepted boundary compatibility. An explicit provenance
   field migration can be planned later; current records retain and hash all
   metadata, rather than dropping it.
5. No operator release, schema activation, deploy or live-trading readiness is
   implied by synthetic admission tests. The six research schema files explicitly
   retain `disabled_pending_verified_evidence_and_release`.

Required next step: independent spec and contrarian quality re-review of the
refreshed complete scoped diff before marking Task 6 accepted.

## Third-review revision checkpoint (paused for cross-task snapshot)

R3 review residual work is mid-edit, not ready for acceptance. Added typed forecast
coverage/accounting-basis/approval-subject/model-version envelope metadata, source
accounting lag policy, strict existing-registry version handling, and evaluator
checks for common cash intervals, lag, exact 90-day coverage and subject-bound
approvals. New tests exposed version-zero acceptance before its fix. The existing
complete synthetic fixture still needs its new metadata/subject bindings updated;
the current intermediate tree is not claimed green. Concrete pinned six-protocol
decoders and remaining adversarial tests are still in progress. No live schema
activation or shared-contract edit occurred. Writes paused on coordinator request.


## R3 completion and R4 remediation (2026-10-17, root)

The checkpoint above is superseded. The R3 tree froze green at 44 tests (`.agenkit/sdd/task-06-r3-freeze.sha256`). The independent R3 re-review returned spec PASS / quality CHANGES REQUIRED. All Important and most Minor findings are now fixed test-first:

- **I1 forged issuance:** there is no public `EvidenceRegistry.record`. `_record` is called only by `EvidenceSource.fetch`. For each `(request, envelope, observation)` it re-validates the envelope against its trusted definition, checks the raw bytes against the declared SHA-256, and requires the observation to equal `_observation_from(envelope)`, the single shared construction. The reviewer's forge reproduction is now a test.
- **I2 tests that did not bite:** new tests cover four guards: claim status (`unconfirmed:revenue`, and `bear_case_value` marked execution_confirmed), the FDV convention gate (`gate_not_verified:fdv_supply_convention_verified`, also added to the independent-gate loop), RPC single id with a duplicated response, and edited ticker bytes. A scratch mutation run kills all four mutants with assertion failures; an unmutated control passes.
- **Minors:** empty TVL claims now return None (unknown), not 0. Forward supply counts an unlock at exactly the cutoff and requires a horizon of exactly 90 days. The shifted-interval test now asserts `cashflow_intervals_incompatible`.
- **Remaining:** registry table-shape check on open; partial raw-artifact writes on a mid-batch conflict (not a safety issue); value re-decode from artifact bytes inside `verify`. The last one needs a per-adapter byte decoder registry, which only the captured-protocol adapters have today. Within the trust model stated above, issuance is reachable only through `fetch`.

Verification: `tests.autonomous.test_research` 49 tests OK (an earlier draft said 50, which was wrong); autonomous suite 326 OK (Python 3.12.14).

**Accepted 2026-10-17** after independent R4 re-review: spec PASS, quality APPROVED, Minor notes only; 7 mutants killed independently. **Activation prerequisite (blocking any schema release):** bind each source definition to a decoder or code hash and re-decode the value from the artifact bytes inside `EvidenceRegistry.verify`. Today a trusted adapter-identity string can assert any value over genuine bytes (reviewer repro `test_lying_adapter`). Open Minors: `_record` re-validation has no direct test (defence in depth), registry table-shape check, artifact written but not issued after a mid-batch conflict.
