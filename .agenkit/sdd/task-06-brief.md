## Task 6: primary evidence, conservative valuation and six schema definitions

**Files:** create `research/autonomous/__init__.py`, `research/autonomous/evidence.py`, `research/autonomous/valuation.py`, `config/autonomous/schemas/aave.json`, `hype.json`, `pendle.json`, `aero.json`, `ldo.json`, `ondo.json`, `tests/autonomous/test_research.py`.

**Consumes:** evidence records and candidate rubrics from the approved research/spec. **Produces:** `EvidenceSource.fetch(request) -> tuple[EvidenceObservation,...]`, `evaluate_thesis(candidate, evidence) -> ThesisAssessment`, and `missing_evidence(values: dict[str,object]) -> tuple[str,...]`. Schema definitions contain exact enum rubrics and candidate row semantics; they are not live approval flags.

- [ ] RED: missing token accrual, concentration or forward supply cannot count as zero risk.

```python
import unittest
from research.autonomous.valuation import missing_evidence

class ResearchTests(unittest.TestCase):
    def test_unknowns_block_admission(self):
        missing = missing_evidence({"price": 1, "token_accrual": None})
        self.assertIn("token_accrual", missing)
        self.assertIn("concentration", missing)
        self.assertIn("forward_supply", missing)
```

- [ ] Run `.venv/bin/python -m unittest tests.autonomous.test_research -v`; expect RED.
- [ ] Implement required-field admission and primary-source observations with units, cutoffs, method/finality, validity and evidence hashes.

```python
REQUIRED_EVIDENCE = (
    "price", "market_cap", "fdv", "circulating_supply", "total_supply",
    "forward_supply", "revenue", "fees", "incentives", "token_accrual",
    "tvl_or_aum", "active_users", "concentration", "execution_costs",
)

def missing_evidence(values: dict[str, object]) -> tuple[str, ...]:
    return tuple(key for key in REQUIRED_EVIDENCE
                 if key not in values or values[key] is None)
```

Admission also requires independent causal primary proof for every quantitative field, consistent valuation cutoff, active thesis approval, positive bear-case valuation discount, no contradiction and executable listing/depth. Classify confirmed mechanism/executed transaction/proposal/estimate/speculation separately; governance proposal is not execution. Normalize TVL/AUM and holder clusters without inventing beneficial owners. Protocol adapters ingest verified contracts/APIs and preserve raw evidence; source access failures return missing fields. No generic web-scraped number becomes primary fact by relabeling it. Pending verification leaves eligibility false. Include AAVE budget conflict, HYPE fee allocation/issuance, PENDLE cash versus points and sPENDLE, AERO incentives/locked accrual, LDO NEST deficit and ONDO nonentitlement/spot restriction. Test reports must never present fixtures as current measurements.
- [ ] GREEN: stale/future/inconsistent units, no-inflation-but-unlocks, treasury redistribution versus burn, misleading wallet clustering, fees versus revenue, protocol success without token entitlement and expired approvals all reject admission. Commit: `feat: compile evidence-gated candidate definitions`.


Fresh advisory input: docs/research/2026-10-17-autonomous-agent/RESEARCH.md and research-evidence. Read its Task6 source-adapter corrections and gap matrix. All6 remain ineligible. Deribit OI/funding units and Circle HTML unit extraction must follow primary contracts. Research captures with receipt=null and only request start/upper bound are not production-causal observations; never fabricate exact receipt. Publication and period dates differ, especially postperiod Lido reporting. No current valuation/forward-supply/revenue/concentration gaps may be silently filled from stale docs.
Focused research supplement: `docs/research/2026-10-17-autonomous-agent/research-deep/REPORT.md` and its verified 12-file manifest. Treat RPC batch responses with duplicate request IDs as rejected evidence; no current supply/float is certified. AAVE's April pause disclosure does not establish October pause/resumption. PENDLE epoch API generic fees/revenues/purchases lack certified units/settlement coverage; zero fields are not proof of zero executions. Keep unknowns null and eligibility false until qualified proof exists. The corrective retrieval was interrupted without a response; do not infer success.

