## Task 15: immutable release manifests and approval-bound activation

**Files:** create `bot/autonomous/release.py`, `tests/autonomous/test_release_promotion.py`; extend ledger release registry.

**Consumes:** immutable bundle/artifact hashes and operator gate approvals. **Produces:** `validate_bundle(bundle,approvals) -> ApprovedBundle | BundleRejection`, `missing_approvals(approved: set[str]) -> tuple[str,...]`, atomic next-boundary activation through authority.

- [ ] RED: a generated nightly schema cannot approve itself.

```python
import unittest
from bot.autonomous.release import missing_approvals

class ReleaseTests(unittest.TestCase):
    def test_ship_approval_is_required(self):
        approvals = {"spec", "architecture", "plan", "build", "review"}
        self.assertEqual(missing_approvals(approvals), ("ship",))
```

- [ ] Run `.venv/bin/python -m unittest tests.autonomous.test_release_promotion -v`; expect RED.
- [ ] Implement gate checking against authenticated content-bound approval events and compatibility/expiry evidence.

```python
GATES = ("spec", "architecture", "plan", "build", "review", "ship")

def missing_approvals(approved: set[str]) -> tuple[str, ...]:
    return tuple(gate for gate in GATES if gate not in approved)
```

All gate events must refer to the bundle's reviewed artifact identities; free-form text claiming approval is insufficient. Persist migration from conversational gate approvals as explicit operator-reviewed release evidence at Ship, not a forged runtime approval. No candidate JSON alone changes limits. Verify deployed code/policy/risk/model/dictionary/tokenizer/calibrator/evidence manifests; code changes require deployment, never hot import. Activation is operator-authenticated, atomic at a boundary, invalidates in-flight judgments, retains pending economic action keys and requires unarmed/reconciled safe transition. Expired old bundle blocks entries; rollback requires a still-compatible approved bundle and never clears risk history.
- [ ] GREEN: missing gate, forged actor/hash, expired thesis, incompatible tokenizer/model/code, old-policy schema, pending unknown order, race with candle and rollback preserving latches. Commit: `feat: enforce release approval lineage`.


## Conductor interface clarification

Release identity is the SHA-256 of the canonical core manifest excluding gate_approvals. GateApproval.bundle_hash refers to that core identity. Embedded approval records are validated independently; the full delivered package also receives a separate package hash for audit. Do not attempt a self-referential hash or treat unsigned embedded approvals as authorization. This is an implementation convention for the approved content-bound gate requirement, not a relaxation of approvals.

Task1 review follow-up: CandidateBundle.bundle_hash and ApprovedBundle approval comparisons currently use the complete manifest hash. Change both paths to the core identity defined above, add a separate complete-package hash, and test nonempty embedded approvals. Review file: .agenkit/sdd/task-01-review.md.

Activation safety clarification: the later approved Plan requires BOTH unarmed and all pending intents reconciled before a boundary activation. Implement that conjunction; architecture wording has been aligned. Activation never rearms the account or clears loss/review latches.

Safety acceptance supplement: read the Task 15 row and applicable cross-task cases in .agenkit/sdd/safety-audit.md. They make the already approved accounting/control/recovery requirements concrete; they are not certification that unimplemented code passes.


## Prerequisites added 2026-10-17
- Release manifests carry `calibrator_hash` = the attested calibration artifact hash. `Ledger.record_calibration_attestation` refuses releases that do not name it, and `estimate_edge` requires it.
- No candidate schema may be activated until evidence source definitions are bound to a decoder or code hash and `EvidenceRegistry.verify` re-decodes values from the artifact bytes (Task 6 acceptance condition).
