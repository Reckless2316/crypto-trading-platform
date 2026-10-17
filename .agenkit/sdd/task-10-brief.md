## Task 10: entry policy, protective exits and durable escalation

**Files:** create `bot/autonomous/policy.py`, `tests/autonomous/test_policy_boundaries.py`; extend ledger review-job/candidate-control handling.

**Consumes:** validated decisions, approved bundle, causal snapshot, current account and edge. **Produces:** `evaluate_candidate(decision,snapshot,bundle,edge,account) -> PolicyResult`, `entry_gate(quality,confidence,risk_state,toxic,direction) -> bool`, and atomic candidate pause/account crisis events plus deduplicated review jobs.

- [ ] RED: strict confidence entry boundary.

```python
import unittest
from bot.autonomous.policy import entry_gate

class PolicyTests(unittest.TestCase):
    def test_confidence_boundary(self):
        self.assertFalse(entry_gate(2, .80, "safe", False, "long"))
        self.assertTrue(entry_gate(2, .800001, "safe", False, "long"))
        self.assertFalse(entry_gate(2, .99, "reduce", False, "long"))
        self.assertFalse(entry_gate(3, .99, "safe", True, "long"))
```

- [ ] Run `.venv/bin/python -m unittest tests.autonomous.test_policy_boundaries -v`; expect RED.
- [ ] Implement the primitive gate, then compose it with required eligibility/freshness/version/calibration/current-account checks. This helper alone is not authorization.

```python
import math

def entry_gate(quality, confidence, risk_state, toxic, direction):
    return (type(quality) is int and 2 <= quality <= 3
            and type(confidence) in (int, float) and math.isfinite(confidence)
            and .80 < confidence <= 1 and risk_state == "safe"
            and toxic is False and direction == "long")
```

Confidence <.60 on any critical Choice persists candidate pause; crisis persists account entry halt immediately. At exactly .60 do not trigger that low-confidence rule; it still fails the .80 entry gate. Store dedupe key `(account,candidate,release,incident_epoch,kind)`. Later confidence/release cannot clear pauses. Approved horizon/crisis/loss exits create only bounded reducing proposals; hard kill always wins. Review pending/unavailable pauses the affected scope and cannot arm it.
- [ ] GREEN: test all conditions independently, explicit neutral/short rejection, low-confidence dedupe/persistence, crisis across candidates, BRAIN failure, horizon reduction and manual entry lacking decision reference. Commit: `feat: gate decisions and escalate uncertain state`.

