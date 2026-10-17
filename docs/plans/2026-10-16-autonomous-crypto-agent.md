# Autonomous Crypto Agent Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use subagent-driven-development (recommended) or executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build the approved autonomous crypto system with causal numeric state, typed Jev judgments, deterministic accounting/risk/execution, and governed research and overnight improvement.

**Architecture:** Four process roles separate authority, REFLEX, BRAIN and dashboard. One account authority owns exchange writes and the durable SQLite journal; models return proposals. Paper and live share the same order/risk path, while live promotion requires independently verified evidence and operator authorization.

**Tech Stack:** Existing Python 3.12 container, standard-library unittest/asyncio/Decimal/sqlite3, FastAPI/Pydantic, CCXT and HTTPX. Use HTTPX for explicit asynchronous Jev timeouts without hidden retries. No new agent framework, database service or frontend framework.

## Global Constraints

- Prepared October 17, 2026; filename retains the approved October 16 design-series identity.
- Status: **APPROVED — operator explicitly authorized continuation and implementation after saving progress on October 17, 2026.** Specification and Architecture are also approved. Build execution and results are tracked in `docs/checkpoints/2026-10-17-autonomous-agent.md`; examples below are requirements, not claimed test results.
- The approved spec and architecture are the controlling requirements: `docs/specs/2026-10-16-autonomous-crypto-agent-design.md` and `docs/architecture/2026-10-16-autonomous-crypto-agent.md`.
- “All arithmetic, eligibility, direction-to-action mapping, sizing, order types, routing, and limits belong to code.”
- “Serialized state must be under 400 provider tokens for the entire call”. Strictly less than 400, including all included instruments, using verified provider token accounting.
- “setup_quality >=2”; “direction_confidence >0.80”; “risk_state == safe”. Confidence <0.60 on a critical Choice escalates; crisis inhibits account entries immediately.
- Drawdown >=15%, UTC daily loss >=2%, position <=5%, gross <=20%, modeled entry loss <=0.25%, fraction <=quarter Kelly; architecture adds <=1% aggregate pending modeled loss. All thresholds use unrounded authoritative values.
- One approved spot venue, USD accounting, hourly decisions, <=5 eligible longs. ONDO is research-only. No synthetic profit probabilities, short openings, leverage, market-order fallback, or treating USDT/USDC as USD cash.
- Four role-specific processes; only authority has venue credentials and writes the ledger. Hard kill also forbids new exit orders. Reductions under risk halt require their own valid permit.
- All source event, receipt and knowledge timestamps must satisfy causal cutoffs. Execution risk uses current reconciled state independently of the model snapshot.
- Restart is unarmed. Drawdown and review latches survive restarts, flows, releases and account closure. UNKNOWN orders keep reservations until reconciliation.
- Preserve existing dirty operator changes. Build uses an isolated worktree prepared using the git-worktrees skill; transfer relevant uncommitted source changes explicitly, with a recorded diff, rather than assuming a fresh worktree contains them.
- No live keys, paid authenticated model calls, real orders, remote deployment, merge or release activation are implied by Plan approval. Local deterministic build/review checks and explicitly inert paper replay are part of Build. Credential-dependent proof remains an explicit promotion condition.
- Research captured on October 16 is historical evidence, not current approval for a candidate. All six remain ineligible until fresh primary evidence and validation qualify them.

---

## Ownership, dependency order and file map

Implement in dependency order. Internal task reviews are engineering checks; the operator gates remain Plan → Build acceptance → Review acceptance → Ship choice → live activation. No new user approval is required for each reversible local code edit after Plan approval.

| Milestone | Tasks | Result |
|---|---|---|
| A: authoritative state | 1–5 | Strict types, durable accounting, immutable causal records and bounded state encoding |
| B: judgments | 6–11 | Evidence-gated schemas, validated Jev, matured outcomes, calibrated sizing, policy and risk |
| C: execution | 12–17 | Paper/live transport, recovery, controls, releases, worker and dashboard integration |
| D: evidence and delivery | 18–22 | Nightly review, replay/promotion evidence, role deployment, independent review and shipping gates |

Production owners: backend owns `bot/autonomous/contracts.py`, `state.py`, `jev_client.py`, `policy.py`, `sizing.py`, `execution.py`, `control.py`, `release.py`, `worker.py`; data owns `ledger.py`, `data/migrations/001_autonomous_ledger.sql`; quantitative/research owns `calibration.py`, `nightly.py`, `brain.py`, `research/autonomous/evidence.py`, `valuation.py`, and six `config/autonomous/schemas/*.json`; infrastructure owns `deploy/autonomous.compose.yml`, `Dockerfile`, `.github/workflows/autonomous-agent.yml` and the runbook. These are sequential responsibility assignments, not permission to overwrite another worker's edits.

The architecture's exact file tree applies. The plan additionally places contract/evidence tests at `tests/autonomous/test_contracts.py`, `test_research.py`, `test_calibration.py`, `test_worker.py`, `test_dashboard_authority.py`, `test_replay.py`, and `test_deployment.py`; shared inert builders belong in `tests/autonomous/support.py`. No production module is created merely to hold a trivial test helper.

## Execution convention

Every code task below has a failing acceptance example, a production kernel or concrete integration recipe, additional mandatory behaviors, and exact focused test commands. The kernel is part of the component; a passing kernel test alone does not complete its integration or the listed failure matrix. Complete each task's RED → minimal complete behavior → GREEN → refactor → task review → focused commit before its dependents.

Use the repository's existing unittest style. For each command below, **RED** must fail for the named missing/incorrect behavior, not a broken interpreter or missing third-party dependency; **GREEN** must pass with no skipped mandatory cases. Create package `__init__.py` files with their first owning task. Test examples use only their shown imports plus completed prior-task interfaces. All fixture prices, balances, probabilities and latencies are synthetic test data, never research metrics.

During Build, establish Python 3.12 and install the existing requirements in the worktree's `.venv`; move `httpx>=0.27,<1` from dev-only to runtime requirements when Task 7 needs it. Generate an exact resolved dependency lock and record its hash in the bundle; do not claim the current broad requirements file is reproducible. New `config/autonomous/risk.yaml` and `deploy/autonomous.compose.yml` use JSON syntax, a valid YAML subset, and are parsed with the standard library in unit tests; no unplanned YAML parser dependency is needed. Do not install packages during this Plan gate. Commands below use `.venv/bin/python` after that environment exists.

## Task 1: domain types, canonical encoding and immutable defaults

**Files:** create `bot/autonomous/__init__.py`, `bot/autonomous/contracts.py`, `config/autonomous/risk.yaml`, `tests/autonomous/__init__.py`, `tests/autonomous/test_contracts.py`, `tests/autonomous/support.py`.

**Consumes:** approved architecture §3 types and §6 controls. **Produces:** frozen dataclasses and enums for every architecture boundary record, all typed errors, `canonical_bytes(value: object) -> bytes`, `content_hash(value: object) -> str`, `checked_money(value: int) -> int`, `decimal_value(value: str) -> Decimal`, `RiskLimits`, and a parsed versioned field dictionary. Types include `SourceEvent`, `CausalCutoffs`, `NumericSnapshot`, `SharedContext`, `FieldDictionary`, `ProviderTokenCounter`, `TokenCount`, `EncodedState`, `QuestionBatch`, `JudgeRequest`, `DecisionBatch`, `CandidateDecision`, `CalibrationArtifact`, `EdgeEstimate`, `UnavailableEdge`, `AccountView`, `AccountRevision`, `MarketRules`, `OrderProposal`, `PreparedIntent`, `ControlState`, `RiskVerdict`, `PolicyResult`, `SubmitOrderCommand`, `CancelOrderCommand`, `ControlCommand`, `IntentReceipt`, `ControlReceipt`, `ReconcileReason`, `ReconcileReport`, `OrderReference`, `VenueOrderResult`, `VenueAccountSnapshot`, `FillCursor`, `FillPage`, `DecisionReference`, `OrderEvent`, `AppendResult`, `SessionCutoff`, `SessionArchive`, `EvidenceRequest`, `EvidenceObservation`, `EvidenceManifest`, `CandidateDefinition`, `ThesisAssessment`, `ReviewJob`, `ReviewProposal`, `ReviewReport`, `EvaluationSplit`, `EvaluationResult`, `CandidateBundle`, `ApprovedBundle`, `GateApproval`, `BundleRejection`, `WorkerConfig`, `WorkerDependencies`, `IngestResult`, `SizedProposal`, `SizeRejection`, `Hold`, `RiskRejection`, and `PendingReconciliation`. Each gets the fields/units and invariants in architecture §3/§7; do not silently accept arbitrary dicts at these boundaries.

- [x] RED: add strict accounting/canonicalization tests, including this complete acceptance example.

```python
import unittest
from bot.autonomous.contracts import canonical_bytes, checked_money

class ContractTests(unittest.TestCase):
    def test_canonical_and_finite(self):
        self.assertEqual(canonical_bytes({"b": 2, "a": 1}), b'{"a":1,"b":2}')
        with self.assertRaises(ValueError):
            canonical_bytes({"p": float("nan")})
        for value in (True, 1.5, 2**63):
            with self.assertRaises(ValueError):
                checked_money(value)
```

- [x] Run `.venv/bin/python -m unittest tests.autonomous.test_contracts -v`; expect RED on absent/unsafe contracts.
- [x] Implement canonical primitives and the architecture's frozen records; reject unknown JSON fields, bool-as-int, unsupported enum values, nonfinite/probability violations, invalid timestamps and excessive decimals before domain construction.

```python
import hashlib
import json

def canonical_bytes(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=True, allow_nan=False).encode("ascii")

def content_hash(value: object) -> str:
    return hashlib.sha256(canonical_bytes(value)).hexdigest()

def checked_money(value: int) -> int:
    if type(value) is not int or not -(2**63) <= value < 2**63:
        raise ValueError("invalid USD microdollars")
    return value
```

Configuration fixes approved limits, 1,000ms Jev deadline, 2,000ms snapshot/book age, 5s private heartbeat, 15s reconciliation age, 10-bps imbalance band, 60 one-minute log returns, four-candle outcome horizon, Noul threshold 0.50, zero Jev retries, and default `mode=paper`, `armed=false`, `candidate_eligible=false`. Decimal text conversion uses explicit scale/rounding; authorization never reads rounded display values. Missing live/tokenizer/calibration configuration disables exposure.
- [x] GREEN: rerun the focused command; include mutation/round-trip/default-disable cases. Commit only the six task-owned files: `feat: define autonomous domain contracts`.

## Task 2: durable schema, journal and atomic reservations

**Files:** create `data/migrations/001_autonomous_ledger.sql`, `bot/autonomous/ledger.py`, `tests/autonomous/test_ledger.py`.

**Consumes:** Task 1 types. **Produces:** `Ledger.open(path: Path) -> Ledger`, `Ledger.close() -> None`, `Ledger.create_paper_account(account_id: str) -> None`, `Ledger.reserve_intent(proposal, expected, generation, decision) -> PreparedIntent`, `Ledger.mark_dispatching(intent_id, generation) -> PreparedIntent`, `Ledger.append_order_event(event) -> AppendResult`, `Ledger.account_view(account_id) -> AccountView`, `Ledger.append_review_job(job) -> AppendResult`, `Ledger.export_session(cutoff) -> SessionArchive`, plus `Ledger.post(account_id: str, transaction_id: str, postings: tuple[tuple[str,int], ...]) -> bool` as the internal balanced USD journal primitive. Opening the database performs migrations/integrity checks, not implicit account adoption or arming.

- [x] RED: test schema atomicity and crash recovery, beginning with:

```python
import tempfile
import unittest
from pathlib import Path
from bot.autonomous.ledger import Ledger

class LedgerTests(unittest.TestCase):
    def test_unbalanced_journal_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            ledger = Ledger.open(Path(directory) / "ledger.sqlite3")
            try:
                ledger.create_paper_account("paper-test")
                with self.assertRaises(ValueError):
                    ledger.post("paper-test", "bad", (("cash", 100), ("capital", -99)))
                self.assertTrue(ledger.post("paper-test", "ok", (("cash", 100), ("capital", -100))))
                self.assertFalse(ledger.post("paper-test", "ok", (("cash", 100), ("capital", -100))))
            finally:
                ledger.close()
```

- [x] Run `.venv/bin/python -m unittest tests.autonomous.test_ledger -v`; expect RED.
- [x] Implement every architecture §7 table with typed CHECKs, FKs, indexes, explicit migrations/checksums, append-only protections and bounded database settings. Journal payload comparison must reject a reused transaction ID with different content. Implement the complete transaction boundary around balance validation and postings:

```python
from bot.autonomous.contracts import checked_money

def validate_postings(postings: tuple[tuple[str, int], ...]) -> None:
    if len(postings) < 2 or any(not name for name, _ in postings):
        raise ValueError("invalid postings")
    for _, amount in postings:
        checked_money(amount)
    if sum(amount for _, amount in postings) != 0:
        raise ValueError("unbalanced journal")
```

Schema SQL maps architecture names exactly; `snapshot_inputs` has separate nullable event/segment FKs and an exactly-one CHECK. Economic action uniqueness is `(account_id,candle_close_us,candidate_id,action_slot)`, independent of release. Fill, correction, transfer, label-event and session keys are individually idempotent. Reservations and risk recheck commit in one `BEGIN IMMEDIATE`; `DISPATCHING` commits before network. No await occurs inside database transactions. Set foreign keys ON, journal_mode WAL, synchronous FULL; database busy/commit errors raise `PersistenceUnavailable` and disable dispatch.
- [x] GREEN: require rollback on injected posting/reservation failure, duplicate correction/session handling, cross-release action uniqueness, missing-reference rejection, and reopen persistence. Commit task paths: `feat: add durable autonomous journal`.

## Task 3: liquidation equity, cash flows and persistent loss latches

**Files:** modify `bot/autonomous/ledger.py`; create initial `bot/autonomous/risk.py`; extend `tests/autonomous/test_ledger.py`; create `tests/autonomous/test_risk_limits.py` and `tests/autonomous/fixtures/external_flows.jsonl`.

**Consumes:** journal/projections from Task 2. **Produces:** `nav_after_flow(equity: Decimal, units: Decimal, flow: Decimal) -> tuple[Decimal,Decimal,Decimal]`, immutable equity marks and high-water/day-open projections; fill/native-fee accounting; `loss_fractions(nav: Decimal, high_water: Decimal, day_open: Decimal) -> tuple[Decimal,Decimal]`.

- [ ] RED: test that adding capital preserves a loss rather than resetting it.

```python
import unittest
from decimal import Decimal as D
from bot.autonomous.risk import loss_fractions
from bot.autonomous.ledger import nav_after_flow

class FlowTests(unittest.TestCase):
    def test_deposit_cannot_hide_drawdown(self):
        equity, units, nav = nav_after_flow(D("8500"), D("100"), D("8500"))
        self.assertEqual((equity, units, nav), (D("17000"), D("200"), D("85")))
        drawdown, daily = loss_fractions(nav, D("100"), D("100"))
        self.assertEqual((drawdown, daily), (D("0.15"), D("0.15")))
```

- [ ] Run `.venv/bin/python -m unittest tests.autonomous.test_ledger tests.autonomous.test_risk_limits -v`; expect RED on NAV/latch behavior.
- [ ] Implement the unit issuance and loss calculations exactly; persist preflow mark, units, HWM, day boundary and latches in the same economic event transaction.

```python
from decimal import Decimal, localcontext

def nav_after_flow(equity: Decimal, units: Decimal,
                   flow: Decimal) -> tuple[Decimal, Decimal, Decimal]:
    if not all(x.is_finite() for x in (equity, units, flow)):
        raise ValueError("nonfinite flow")
    if equity <= 0 or units <= 0 or equity + flow <= 0:
        raise ValueError("invalid account NAV")
    with localcontext() as ctx:
        ctx.prec = 50
        nav = equity / units
        return equity + flow, units + flow / nav, nav

def loss_fractions(nav: Decimal, high_water: Decimal,
                   day_open: Decimal) -> tuple[Decimal, Decimal]:
    if not all(x.is_finite() and x > 0 for x in (nav, high_water, day_open)):
        raise ValueError("invalid loss baseline")
    return max(Decimal(0), 1-nav/high_water), max(Decimal(0), 1-nav/day_open)
```

Account closure records the withdrawal and durable closed state separately before any NAV division; it does not erase incidents. Include buy/sell basis, third-asset and base-paid fees, verified FX provenance, fee corrections via compensating postings, and explicit rounding account. Reconcile every holding. Missing boundary NAV, stale liquidation depth, unverified transfer, unknown basis/fee or nonpositive equity means unready. No fee double-counting or deposit-as-profit.
- [ ] GREEN: include 14.999%/15% drawdown, 1.999%/2% daily loss, withdrawals, round-trip basis/fees, reopen/closure/deposit latch survival and UTC rollover. Commit: `feat: account for equity and persistent risk loss`.

## Task 4: immutable causal observations and book archives

**Files:** modify `bot/autonomous/ledger.py`; create `bot/autonomous/state.py`, `tests/autonomous/test_causality.py`, `tests/autonomous/fixtures/causal_book.jsonl`.

**Consumes:** Task 1 `SourceEvent/CausalCutoffs` and Task 2 persistence. **Produces:** `StateEngine.ingest(event) -> IngestResult`, source revision/invalidations selection, archive manifests and `causal_visible(event_at_us, received_at_us, known_at_us, candle_close_us, knowledge_cutoff_us) -> bool`.

- [ ] RED: ensure event time alone cannot admit a late observation.

```python
import unittest
from bot.autonomous.state import causal_visible

class CausalityTests(unittest.TestCase):
    def test_late_receipt_is_excluded(self):
        self.assertFalse(causal_visible(90, 111, 111, 100, 110))
        self.assertFalse(causal_visible(101, 109, 109, 100, 110))
        self.assertTrue(causal_visible(90, 109, 109, 100, 110))
```

- [ ] Run `.venv/bin/python -m unittest tests.autonomous.test_causality -v`; expect RED.
- [ ] Implement the three-cutoff predicate, immutable revision insertion and sequence-aware local book reconstruction.

```python
def causal_visible(event_at_us: int, received_at_us: int, known_at_us: int,
                   candle_close_us: int, knowledge_cutoff_us: int) -> bool:
    values = (event_at_us, received_at_us, known_at_us,
              candle_close_us, knowledge_cutoff_us)
    if any(type(x) is not int or x < 0 for x in values):
        return False
    return (event_at_us <= candle_close_us
            and received_at_us <= knowledge_cutoff_us
            and received_at_us <= known_at_us <= knowledge_cutoff_us)
```

Select latest eligible `(known_at_us,revision,event_id)` per logical key; append invalidations, never mutate prior source rows. Reject source-clock drift beyond the release's source-specific allowance, crossed/empty books and sequence gaps. Archive compressed deltas/checkpoints via fsync + atomic rename before committing manifests. Reconstruct using only committed segments. Orphans need hash verification on recovery. Imported legacy candles get import-time knowledge timestamps, not fictional historical receipt times.
- [ ] GREEN: cover post-cutoff revisions, same-event different receipt, duplicate/gap/out-of-order sequences, reorg invalidation and archive-write failure. Commit: `feat: persist causal source history`.

## Task 5: deterministic state features and strict token budget

**Files:** extend `bot/autonomous/state.py`; create `tests/autonomous/test_state.py`.

**Consumes:** causal engine, field dictionary, ledger AccountView, verified `ProviderTokenCounter`. **Produces:** `StateEngine.snapshot(symbol, cutoffs) -> NumericSnapshot`, `encode_state(snapshots, context, dictionary, token_counter) -> EncodedState`, `book_metrics(bids: tuple[tuple[float,float],...], asks: tuple[tuple[float,float],...], band_bps: float=10.0) -> tuple[float,float,float]`, and `require_token_budget(count: int) -> None`.

- [ ] RED: test numeric results and the strict 399/400 boundary.

```python
import unittest
from bot.autonomous.state import book_metrics, require_token_budget

class StateTests(unittest.TestCase):
    def test_book_math_and_budget(self):
        mid, spread, imbalance = book_metrics(((99.99, 2.0),), ((100.01, 1.0),))
        self.assertEqual(mid, 100.0)
        self.assertAlmostEqual(spread, 2.0)
        self.assertAlmostEqual(imbalance, 1/3)
        require_token_budget(399)
        with self.assertRaises(ValueError):
            require_token_budget(400)
```

- [ ] Run `.venv/bin/python -m unittest tests.autonomous.test_state tests.autonomous.test_causality -v`; expect RED on missing features/encoding.
- [ ] Implement canonical feature math, validity masks and deterministic candidate selection.

```python
import math

def book_metrics(bids, asks, band_bps=10.0):
    if not bids or not asks or not math.isfinite(band_bps) or band_bps <= 0:
        raise ValueError("invalid book")
    if any(not math.isfinite(x) or x <= 0 for levels in (bids, asks)
           for level in levels for x in level):
        raise ValueError("invalid level")
    bid, ask = max(p for p, _ in bids), min(p for p, _ in asks)
    if bid >= ask:
        raise ValueError("crossed book")
    mid = (bid + ask)/2
    lower, upper = mid*(1-band_bps/10000), mid*(1+band_bps/10000)
    b = math.fsum(q for p, q in bids if lower <= p <= mid)
    a = math.fsum(q for p, q in asks if mid <= p <= upper)
    if not all(math.isfinite(x) for x in (mid, b, a, b+a)) or b+a <= 0:
        raise ValueError("invalid depth")
    result = mid, 10000*(ask-bid)/mid, (b-a)/(b+a)
    if not all(math.isfinite(x) for x in result):
        raise ValueError("nonfinite book feature")
    return result

def require_token_budget(count: int) -> None:
    if type(count) is not int or not 0 <= count < 400:
        raise ValueError("state token budget exceeded")
```

Use 61 contiguous completed one-minute mids for 60 log returns; sample stdev, never irregular tick returns or zero warmup volatility. Shared/candidate array positions exactly match architecture §4. Numeric precision is a dictionary property, validity bits distinguish missing/zero. Do not round equity for hard controls. Provider-approved state token proof is mandatory before real evaluation; a fake counter exists only in tests. Greedily remove lowest-ranked candidate until the **whole** state fits, retaining all mandatory fields. Persist snapshots/lineage before dispatch; zero eligible candidates produces HOLD without a provider call.
- [ ] GREEN: verify math against hand-computed fixtures; warmup, overflow, gaps, stable hash, entire five-row budget, unavailable tokenizer, exclusion audit and old inventory versus current execution AccountView. Commit: `feat: derive bounded causal numeric state`.

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

## Task 7: typed parallel Jev client and answer validation

**Files:** create `bot/autonomous/jev_client.py`, `tests/autonomous/test_jev_contract.py`, `tests/autonomous/fixtures/jev_responses.json`; modify `requirements.txt`, `requirements-dev.txt`; create resolved `requirements.lock` during Build.

**Consumes:** encoded batch, approved schema definitions and pinned model. **Produces:** `compile_questions(bundle,state) -> QuestionBatch`, `JevClient.judge(state,bundle,request) -> DecisionBatch`, `validate_choice(probabilities: dict[str,float], choice: str, confidence: float) -> None`.

- [ ] RED: malformed distributions or invented confidence are rejected.

```python
import unittest
from bot.autonomous.jev_client import validate_choice

class JevTests(unittest.TestCase):
    def test_confidence_is_not_top_probability(self):
        probabilities = {"long": .9, "short": .05, "neutral": .05}
        validate_choice(probabilities, "long", .85)
        with self.assertRaises(ValueError):
            validate_choice(probabilities, "long", .9)
```

- [ ] Run `.venv/bin/python -m unittest tests.autonomous.test_jev_contract -v`; expect RED.
- [ ] Implement exact Choice consistency, four-option quality Choice, Noul boolean conversion and single-call transport.

```python
import math

def validate_choice(probabilities, choice, confidence):
    ps = tuple(probabilities.values())
    if len(ps) < 2 or choice not in probabilities:
        raise ValueError("invalid Choice options")
    if any(type(p) not in (int, float) or not math.isfinite(p)
           or not 0 <= p <= 1 for p in ps):
        raise ValueError("invalid probability")
    if abs(math.fsum(ps)-1) > 1e-6 or probabilities[choice] != max(ps):
        raise ValueError("inconsistent Choice")
    expected = (max(ps)-1/len(ps))/(1-1/len(ps))
    if (type(confidence) not in (int, float) or not math.isfinite(confidence)
            or not 0 <= confidence <= 1 or abs(confidence-expected) > 1e-6):
        raise ValueError("inconsistent confidence")
```

Generate exactly five questions per included candidate with explicit row/ID/horizon/dictionary and independent rubrics. One POST with model/state/questions, server-side key, explicit total deadline <=1,000ms, zero retries. Use HTTPX injected transport for tests; no network is needed for RED/GREEN. Treat all errors/late/wrong model/missing/extra answers as HOLD. Authority validates the response again. Provider-tokenizer or model availability changes block promotion rather than switch aliases. Capture full-call latency/usage and state-only proof separately.
- [ ] GREEN: test all candidate schemas, quality 2/3 strings mapped to ints, toxic threshold exactly .50, invalid bool probabilities, nonfinite values, wrong answer set/model/state/deadline, 429/5xx, no retries and one call for all candidates. Commit: `feat: add validated parallel Jev judgments`.

## Task 8: immutable outcome labels and calibrated edge artifacts

**Files:** create `bot/autonomous/calibration.py`, `tests/autonomous/test_calibration.py`; modify `bot/autonomous/ledger.py` for label events/projections.

**Consumes:** recorded judgments/probabilities, executable postdecision observations and fixed four-candle policy. **Produces:** `estimate_edge(forecast,snapshot,artifact) -> EdgeEstimate | UnavailableEdge`, `binary_brier(probabilities: tuple[float,...], outcomes: tuple[int,...]) -> float`, versioned matured label events and supported cohort estimates.

- [ ] RED: exact Brier and invalid/pending labels.

```python
import unittest
from bot.autonomous.calibration import binary_brier

class CalibrationTests(unittest.TestCase):
    def test_brier_uses_outcome_probability(self):
        self.assertAlmostEqual(binary_brier((.8, .3), (1, 0)), .065)
        with self.assertRaises(ValueError):
            binary_brier((.8,), ())
        with self.assertRaises(ValueError):
            binary_brier((.8,), (2,))
```

- [ ] Run `.venv/bin/python -m unittest tests.autonomous.test_calibration -v`; expect RED.
- [ ] Implement strict metrics, frozen horizon labels, matching version support and chronological calibration.

```python
import math

def binary_brier(probabilities, outcomes):
    if not probabilities or len(probabilities) != len(outcomes):
        raise ValueError("incomplete outcomes")
    if any(type(y) is not int or y not in (0, 1) for y in outcomes):
        raise ValueError("invalid outcome")
    if any(type(p) not in (int, float) or not math.isfinite(p)
           or not 0 <= p <= 1 for p in probabilities):
        raise ValueError("invalid probability")
    return math.fsum((p-y)**2 for p, y in zip(probabilities, outcomes))/len(outcomes)
```

Initial calibrator is a versioned bin map from Jev's probability of the proposed direction and approved regime/instrument cohort to net-positive outcomes. Define at most four quantile bins using training observations only, freeze boundaries before calibration/test, and require at least 100 independent calibration outcomes per admitted bin; unsupported bins remain disabled with no interpolation/extrapolation. Conservative p is the 95% one-sided Wilson lower bound; conservative W is a lower conditional-win estimate, L an upper conditional-loss estimate from independent training/calibration blocks. Store method, sample count, payoff tails/cost convention and exact artifact compatibility. Separate binary policy labels from terminal direction labels. Outcome revisions append label_events, preserve original session versions and recompute current projections. Missing quote/depth/fees, short borrow or matured data stays unresolved. No valid artifact means `UnavailableEdge`, including at bootstrap; shadow forecasts can still collect labels without trades.
- [ ] GREEN: future labels excluded, label corrections retained, no confidence-as-p, all HOLD/veto forecasts included, version mismatch rejects, no-supported-bin rejects, net fees/slippage affect labels and Brier denominators include only resolved labels with missingness reported. Commit: `feat: record outcome calibration evidence`.

## Task 9: fractional Kelly sizing with deterministic capacity caps

**Files:** create `bot/autonomous/sizing.py`, `tests/autonomous/test_sizing.py`.

**Consumes:** validated `EdgeEstimate`, current `AccountView`, `MarketRules`, `RiskLimits`. **Produces:** `size_entry(edge,account,market,limits) -> SizedProposal | SizeRejection` and `kelly_loss_budget(equity,p,win,loss) -> Decimal`.

- [ ] RED: show quarter-Kelly and the harder per-entry cap.

```python
import unittest
from decimal import Decimal as D
from bot.autonomous.sizing import kelly_loss_budget

class SizingTests(unittest.TestCase):
    def test_loss_budget_is_capped(self):
        self.assertEqual(kelly_loss_budget(D("10000"), D(".6"), D(".02"), D(".01")), D("25"))
        self.assertEqual(kelly_loss_budget(D("10000"), D(".2"), D(".02"), D(".01")), D("0"))
```

- [ ] Run `.venv/bin/python -m unittest tests.autonomous.test_sizing -v`; expect RED.
- [ ] Implement exact loss-budget convention and convert to notional only after validated positive payoff/cost support.

```python
from decimal import Decimal, localcontext

def kelly_loss_budget(equity, p, win, loss):
    if not all(x.is_finite() for x in (equity, p, win, loss)):
        raise ValueError("nonfinite Kelly input")
    if equity <= 0 or not 0 <= p <= 1 or win <= 0 or loss <= 0:
        raise ValueError("invalid Kelly input")
    with localcontext() as ctx:
        ctx.prec = 50
        fraction = max(Decimal(0), p-(1-p)/(win/loss))
        return equity*min(Decimal(".25")*fraction, Decimal(".0025"))
```

`size_entry` caps loss budget by remaining 1% pending loss, notional by 5% asset/20% gross residual room, current free cash minus reservations and max fees, and verified depth. Convert to base, round quantity down to lot, round buy price up/sell down within a bounded permissible limit, then recompute all caps; rounding must not expand authorization. Reject missing calibration/tail suitability, negative expectation, costs counted twice, unknown fee or minimum notional violations. Proposed initial entry execution guardrails: spread <=10bps, modeled additional impact <=10bps and participation <=1% of displayed executable depth inside the bound. These conservative plan parameters require measured validation before live admission; no liquidity numbers are assumed today.
- [ ] GREEN: cover exact caps, tiny L, nonfinite, unsupported edges, rounding/minimums, already-reserved capacity and insufficient depth. Commit: `feat: cap calibrated Kelly allocations`.

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

## Task 11: complete pretrade risk authority

**Files:** extend `bot/autonomous/risk.py`, `tests/autonomous/test_risk_limits.py`.

**Consumes:** proposals, current account/control, limits, market rules and time. **Produces:** `assess(proposal,account,control,limits,market,now_us) -> RiskVerdict`; durable risk transitions are committed by ledger/authority, not the pure function.

- [ ] RED: hard kill applies equally to entry and exit; equality at loss limits inhibits entry. Add these cases to RiskTests as table-driven `subTest`s with Task 1 inert AccountView/ControlState builders: killed buy, killed reduce, disarmed buy, expired lease, new boot lease, stale book, private heartbeat, reconciliation, missing equity/day baseline, >=15% drawdown, >=2% daily loss, >5% asset, >20% gross, >1% pending loss, sell beyond owned units, unknown orders and unverified bounded-order capability. Expect a stable veto reason and zero dispatch eligibility for each.

```python
import unittest
from decimal import Decimal as D
from bot.autonomous.risk import loss_limit_reasons

class RiskBoundaryTests(unittest.TestCase):
    def test_loss_thresholds_are_inclusive(self):
        self.assertEqual(loss_limit_reasons(D(".14999"), D(".01999")), ())
        self.assertIn("DRAWDOWN_LATCH", loss_limit_reasons(D(".15"), D("0")))
        self.assertIn("DAILY_HALT", loss_limit_reasons(D("0"), D(".02")))
```

- [ ] Run `.venv/bin/python -m unittest tests.autonomous.test_risk_limits -v`; expect RED on the missing vetoes.
- [ ] Implement named checks and return all relevant reason codes. Use this exact threshold kernel inside the full assessment:

```python
from decimal import Decimal

def loss_limit_reasons(drawdown: Decimal, daily_loss: Decimal) -> tuple[str, ...]:
    if not all(x.is_finite() and x >= 0 for x in (drawdown, daily_loss)):
        return ("INVALID_EQUITY",)
    reasons = []
    if drawdown >= Decimal(".15"):
        reasons.append("DRAWDOWN_LATCH")
    if daily_loss >= Decimal(".02"):
        reasons.append("DAILY_HALT")
    return tuple(reasons)
```

Pure checks evaluate current equity, reservations, reducing semantics, permissions, freshness, quote/fee/lot and capacity. Use exact cross-product comparisons `nav <= .85*high_water` and `nav <= .98*day_open` under the validated Decimal precision for the authoritative loss threshold, and integer cross-products for monetary capacity caps; a rounded reporting ratio never grants approval. The ratio helper above is tested for consistency, not used to loosen those exact comparisons. A reduce is valid only if it decreases current gross units and cannot cross zero. Persist latches atomically even when returning a veto; independent continuous mark events also assess loss limits without waiting for a candle/model. External flow/unknown fill incidents invalidate readiness. A hard cap does not claim to prevent market-value drift or losses after a gap.
- [ ] GREEN: exact threshold neighbors, simultaneous proposals with only one residual capacity reservation, stale revision rejection, no unconfirmed-reduction credit and all-order kill coverage. Commit: `feat: enforce deterministic account risk`.

## Task 12: order authority and paper lifecycle

**Files:** create `bot/autonomous/execution.py`, `tests/autonomous/test_execution_recovery.py`, `tests/autonomous/fixtures/partial_fill_cancel.jsonl`, `uncertain_submit.jsonl`; modify `bot/paper_coinbase_adapter.py` only at the managed-account submission boundary.

**Consumes:** risk/ledger/decision and market contracts. **Produces:** `OrderAuthority.submit/cancel/control/reconcile` architecture signatures, `VenuePort` protocol, `PaperVenue`, and `economic_action_key(account_id,candle_close_us,candidate_id,action_slot) -> str`.

- [ ] RED: release changes cannot create a second economic entry.

```python
import unittest
from bot.autonomous.execution import economic_action_key

class ExecutionTests(unittest.TestCase):
    def test_action_key_is_stable(self):
        key = economic_action_key("paper-1", 1000000, 1, "entry")
        self.assertEqual(key, economic_action_key("paper-1", 1000000, 1, "entry"))
        self.assertNotEqual(key, economic_action_key("paper-1", 1000000, 2, "entry"))
```

- [ ] Run `.venv/bin/python -m unittest tests.autonomous.test_execution_recovery -v`; expect RED.
- [ ] Implement stable keys and the full reserve → final check → committed DISPATCHING → venue lifecycle with injected clock/venue. Paper emits venue-like events; it does not mutate independent positions/cash.

```python
from bot.autonomous.contracts import content_hash

def economic_action_key(account_id, candle_close_us, candidate_id, action_slot):
    if (not account_id or type(candle_close_us) is not int or candle_close_us < 0
            or type(candidate_id) is not int or candidate_id <= 0
            or not action_slot):
        raise ValueError("invalid economic action")
    return content_hash([account_id, candle_close_us, candidate_id, action_slot])
```

Authority assigns semantic slots; callers cannot invent random entries. Include ledger duplicate tests for the same key under two releases, not just hash equality. Use bounded IOC fills against supplied causal depth with adverse fees/slippage, partials/nonfills and terminal remainders. Keep per-account dispatch mutex, control epoch check and atomic reservation. Record all source/intent/fill outcomes; admission unsupported means an audited no-trade paper session, not relaxed gates.
- [ ] GREEN: duplicate/cross-release entries, risk veto no submit, reservation rollback, partial fills/cancel races, oversell prevention, unknown status and persistence failure. Commit: `feat: execute paper orders through one authority`.

## Task 13: bounded live transport and uncertain-order reconciliation

**Files:** modify `bot/live_coinbase_adapter.py` at `LiveCoinbaseAccount.place_market_order` and add the managed bounded transport; extend `bot/autonomous/execution.py`, `tests/autonomous/test_execution_recovery.py`, `tests/test_live_adapter.py`.

**Consumes:** prepared intents and verified venue capabilities. **Produces:** live `VenuePort.submit/lookup/cancel/read_account/read_fills/market_rules`; `reservation_releasable(terminal,filled,accounted,fees_final) -> bool` and startup/periodic reconciliation.

- [ ] RED: cancel acknowledgment and missing fees must retain capacity.

```python
import unittest
from decimal import Decimal as D
from bot.autonomous.execution import reservation_releasable

class ReconcileTests(unittest.TestCase):
    def test_cancel_ack_is_insufficient(self):
        self.assertFalse(reservation_releasable(True, D("2"), D("1"), True))
        self.assertFalse(reservation_releasable(True, D("2"), D("2"), False))
        self.assertFalse(reservation_releasable(False, D("0"), D("0"), True))
        self.assertTrue(reservation_releasable(True, D("2"), D("2"), True))
```

- [ ] Run `.venv/bin/python -m unittest tests.autonomous.test_execution_recovery tests.test_live_adapter -v`; expect RED on new managed transport/recovery behavior.
- [ ] Implement terminal/cumulative-fee proof and live transport with explicit verified client ID and bounded price/IOC semantics. Keep legacy behavior out of managed-account dispatch; a managed-account call to the old market primitive rejects.

```python
def reservation_releasable(terminal, filled, accounted, fees_final):
    return (terminal is True and fees_final is True
            and filled.is_finite() and accounted.is_finite()
            and filled >= 0 and accounted >= 0 and filled == accounted)
```

Submit timeout becomes UNKNOWN; no submit retry. REST reads get <=3 attempts within <=10s/current deadline. Paginate fills with overlap and account+venue trade-ID dedupe. A changed duplicate payload appends a correction and compensating journal, never overwrites original. A single client-ID not-found is not absence proof. Reconcile balances, transfers, open orders, terminal cumulative fills/fees and every unknown intent before readiness or releasing reservations. Foreign orders/holdings halt adoption. Capability evidence must verify tick/lot/minimum, IOC behavior, client-ID length/idempotency/lookup, fee currencies and fill history. Unverified capabilities keep live disabled; tests use fake exchange transport only.
- [ ] GREEN: inject crash immediately before/after dispatch, timeout after acceptance, cancel/fill race, late fill after terminal, corrected fee, pagination overlap, failed balance match and absent client-ID lookup. Assert no second submit and conservative retained capacity. Commit: `feat: reconcile bounded live order intents`.

## Task 14: authenticated controls, kill ordering and persistent review resolution

**Files:** create `bot/autonomous/control.py`, `tests/autonomous/test_control.py`; extend authority/ledger control handling.

**Consumes:** ControlCommand, epochs, incident/review references and authority mutex. **Produces:** bounded Unix-socket server/client, `command_allowed(role: str, kind: str, hard_killed: bool) -> bool`, operator arm/disarm/kill/reduce/resolve_review and read-only status/export capabilities.

- [ ] RED: models cannot arm or bypass a hard kill.

```python
import unittest
from bot.autonomous.control import command_allowed

class ControlTests(unittest.TestCase):
    def test_roles_and_kill(self):
        self.assertFalse(command_allowed("brain", "arm", False))
        self.assertFalse(command_allowed("reflex", "activate_release", False))
        self.assertFalse(command_allowed("operator", "reduce", True))
        self.assertTrue(command_allowed("operator", "kill", True))
```

- [ ] Run `.venv/bin/python -m unittest tests.autonomous.test_control -v`; expect RED.
- [ ] Implement role allowlists, bounded JSON protocol, actor/session authentication and kill's durable local inhibition before cancellation. This primitive is necessary but not sufficient authorization:

```python
def command_allowed(role, kind, hard_killed):
    allowed = {
        "operator": {"status", "arm", "disarm", "kill", "reduce", "cancel",
                     "resolve_review", "activate_release", "reconcile", "export_session"},
        "reflex": {"submit_judgment"},
        "brain": {"import_proposal", "import_label_events"},
        "reader": {"status"},
    }
    if kind not in allowed.get(role, set()):
        return False
    return not (hard_killed and kind in {"arm", "reduce"})
```

Reject oversized/extra-field/malformed messages, wrong peer/token/role, stale epoch, expired nonce and duplicate ID with changed payload. Same request+payload returns original receipt. Kill and final dispatch share the mutex; report local inhibition separately from pending venue cancellations. Operator deadman renewals are external to daemon health, invalidate across boot and cannot originate in BRAIN/REFLEX. `resolve_review` needs operator, linked finding, fresh risk/reconciliation; it cannot clear drawdown or arm. Hard-kill reset has no automatic endpoint in this release.
- [ ] GREEN: assert durable kill before acknowledgment, no later dispatch authorization, in-flight orders remain visible/canceled, no exit on hard kill, stale/replayed command rejection, restarts unarmed and model role unable to resolve itself. Commit: `feat: secure operator control and kill state`.

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

## Task 16: role workers, market/private ingestion and hourly loop

**Files:** create `bot/autonomous/worker.py`, `tests/autonomous/test_worker.py`; integrate existing `data/collectors/coinbase_collector.py` and `data/collectors/collector_base.py` only through explicit event adapters; extend state/authority as needed.

**Consumes:** Tasks 1–15 interfaces. **Produces:** `run_worker(config,dependencies) -> None`, authority/reflex entrypoints, independent deadlines, continuous marking and durable hourly scheduling; `role_secret_names(role: str) -> frozenset[str]`.

- [ ] RED: roles cannot inherit venue/model credentials indiscriminately.

```python
import unittest
from bot.autonomous.worker import role_secret_names

class WorkerTests(unittest.TestCase):
    def test_reflex_has_no_venue_key(self):
        self.assertEqual(role_secret_names("reflex"), frozenset({"TYPESAFE_API_KEY"}))
        self.assertNotIn("TYPESAFE_API_KEY", role_secret_names("authority"))
```

- [ ] Run `.venv/bin/python -m unittest tests.autonomous.test_worker -v`; expect RED.
- [ ] Implement explicit role configuration and startup state machine; create live adapter only inside authority, only after the approved live activation conditions.

```python
def role_secret_names(role):
    names = {
        "authority": {"SURGE_COINBASE_API_KEY", "SURGE_COINBASE_API_SECRET"},
        "reflex": {"TYPESAFE_API_KEY"},
        "brain": {"SURGE_BRAIN_API_KEY"},
        "dashboard": set(),
    }
    if role not in names:
        raise ValueError("unknown role")
    return frozenset(names[role])
```

Control tokens are role-specific file-mounted capabilities, separate from this provider-secret allowlist. Strip unrelated provider env/secrets on role startup and mount isolation in Task 20. Hold process flock for life. Startup order matches architecture §9. Public depth and private fills use verified venue stream/sequence semantics; a five-minute REST candle poll is not a live order book. Maintain current equity/risk between candles. At hourly close freeze one knowledge cutoff, persist snapshot, request one batch, validate returned identity/deadline, assess current account and dispatch eligible intents. Persist candle scheduling/dedupe and terminal HOLD outcomes; no replay of missed historical entries after restart. Graceful stop inhibits dispatch, preserves reservations and reconciles/cancels per control state.
- [ ] GREEN: two workers competing lock, missing keys, wrong role environment, stale streams, clock jump, next-bar closure, duplicate candle, model delay, fresh fills during inference, disk pressure and crash recovery. Commit: `feat: run isolated autonomous decision workers`.

## Task 17: close existing dashboard and adapter bypasses

**Files:** modify `dashboard/backend/app.py` at execute_order (~217), hourly automation (~355), live order (~463), paper order (~653), arm/kill/flatten (~662–690); modify `bot/risk_gates.py`, `bot/live_coinbase_adapter.py`, `bot/paper_coinbase_adapter.py` only for managed-account integration; create `tests/autonomous/test_dashboard_authority.py`; update `tests/test_platform_api.py`, `tests/test_live_adapter.py` only when changed behavior requires it.

**Consumes:** authenticated local control client and authority read models. **Produces:** HTTP routes as authenticated command/read-model clients, no independent managed-account trading or accounting.

- [ ] RED: unauthenticated legacy arming is rejected.

```python
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from fastapi.testclient import TestClient
from dashboard.backend import app as platform_app

class DashboardAuthorityTests(unittest.TestCase):
    def test_anonymous_arm_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            with patch.object(platform_app, "DB_FILE", Path(directory)/"test.sqlite3"):
                platform_app.init_db()
                client = TestClient(platform_app.app)
                try:
                    response = client.post("/api/control/arm", json={"duration_minutes": 5})
                    self.assertEqual(response.status_code, 401)
                finally:
                    client.close()
```

The client is deliberately not entered as a context manager, so the existing startup collector does not run; DB_FILE is isolated. Extend the test with injected control/session dependencies for authenticated cases. New managed integration must preserve that isolation and never fall back to the operator's database if its dependency is absent.
- [ ] Run `.venv/bin/python -m unittest tests.autonomous.test_dashboard_authority tests.test_platform_api -v`; expect RED for existing bypass routes.
- [ ] Replace direct account constructors/database mutations with authenticated control forwarding and typed receipt/status handling. Route-side behavior is `401` without session, `403` for role/Origin/CSRF failure, `409` for typed risk/control rejection and `202` for pending accepted commands. HttpOnly SameSite=Strict sessions, CSRF nonce, localhost binding and role-scoped actor attribution are required. No browser route supplies caller-chosen risk approval callbacks or action slots. Flatten under hard kill returns the actual rejection; an authorized reducing permit is distinct from a live-entry permit. Old independent collectors/display may continue but cannot own managed account execution.
- [ ] GREEN: mock old direct submission methods to raise if called and exercise every order/strategy/flatten/live route. Verify no anonymous arm/kill/order path, no dashboard venue secret, all managed writes go to authority, no hidden hourly worker duplicate, existing read-only API behavior and preserved operator frontend changes. Commit: `refactor: route managed trading through authority`.

## Task 18: complete-session nightly review and BRAIN proposals

**Files:** create `bot/autonomous/brain.py`, `bot/autonomous/nightly.py`, `tests/autonomous/test_nightly.py`; extend ledger export/label import/review-job persistence.

**Consumes:** SessionArchive, matured labels, calibration metrics, BrainPort and EvaluationRunner. **Produces:** `BrainPort.review(job,archive) -> ReviewProposal`, `review_session(archive,brain,evaluation) -> ReviewReport`, brain role scheduler and bounded typed candidate artifacts; `label_mature(maturity_us: int, cutoff_us: int) -> bool`.

- [ ] RED: a nightly cutoff cannot consume future outcomes.

```python
import unittest
from bot.autonomous.nightly import label_mature

class NightlyTests(unittest.TestCase):
    def test_pending_outcome_is_not_a_loss(self):
        self.assertFalse(label_mature(101, 100))
        self.assertTrue(label_mature(100, 100))
```

- [ ] Run `.venv/bin/python -m unittest tests.autonomous.test_nightly -v`; expect RED.
- [ ] Implement cutoff predicate, read-only export processing, deterministic full-session metrics, provider adapter and candidate proposal validation.

```python
def label_mature(maturity_us, cutoff_us):
    if any(type(t) is not int or t < 0 for t in (maturity_us, cutoff_us)):
        raise ValueError("invalid label timestamp")
    return maturity_us <= cutoff_us
```

At 00:15 UTC create a unique account/cutoff session and process only fixed-cutoff records. Resolve matured labels, retain exact event revisions in manifest, report every fill/partial/nonfill/rejection/veto/miss/error/HOLD, separately report hypothetical executable P&L and realized results. Compute binary/multiclass Brier, baseline, reliability intervals, missingness, cohort counts, costs, tail losses, latency and calibration drift. If BRAIN credentials/model are unavailable, still produce deterministic metrics and a pending review job. The external model ID is configured and verified; do not claim this Codex process is Opus or hardcode an unverified model name.

BRAIN output has cited findings, hypothesis, bounded schema/calibrator diff and evidence references; no shell/SQL/exchange action. Up to three attempts per run, <=60s per request; schema failure remains pending. A no-change result is valid. Candidate changes become AgenKit work items and must pass Task 15 gates; no overnight self-approval. Expired approved evidence stops entries if review is late.
- [ ] GREEN: fills plus misses, no-trade night, pending labels, wrong probability denominator, duplicate scheduler/retry, provider failure, malicious proposal action, schema proposal without approval, corrected historical label retaining old report and next-open-free scheduling. Commit: `feat: review sessions and propose governed improvements`.

## Task 19: causal replay, statistical evidence and promotion criteria

**Files:** extend `data/backtesting.py` with separate `AutonomousReplay` using the new state/policy/risk/execution path; create `tests/autonomous/test_replay.py`; extend `bot/autonomous/calibration.py`, `release.py`, `nightly.py` for split/evaluation manifests.

**Consumes:** immutable source segments, deployed code/bundle hashes, explicit EvaluationSplit and paper VenuePort. **Produces:** `EvaluationRunner.evaluate(bundle,archive,split) -> EvaluationResult`, deterministic full replay report and `minimum_holdout_blocks(delta: float=.05, alpha: float=.05, power: float=.80) -> int`.

- [ ] RED: sample requirements cannot be replaced by a profitable tiny sample.

```python
import unittest
from bot.autonomous.calibration import minimum_holdout_blocks

class EvidenceTests(unittest.TestCase):
    def test_holdout_floor(self):
        self.assertGreaterEqual(minimum_holdout_blocks(), 785)
        self.assertGreater(minimum_holdout_blocks(delta=.025), minimum_holdout_blocks())
```

- [ ] Run `.venv/bin/python -m unittest tests.autonomous.test_replay tests.autonomous.test_calibration tests.autonomous.test_release_promotion -v`; expect RED on replay/evidence integration.
- [ ] Implement a precommitted chronological train/calibrate/test split, label purging and a >=four-hour embargo at every split, sealed holdout identity/exposure register and exact code reuse. Independent sample unit is a nonoverlapping four-hour time block across correlated assets, not one row per token or every overlapping candle. Require >=1,000 training blocks, >=400 calibration blocks (and >=100 independent outcomes per admitted bin), and >=max(800, calculated requirement) untouched test blocks for each admitted support cohort. These are proposed conservative validation requirements, not claims that such data currently exists.

```python
import math
from statistics import NormalDist

def minimum_holdout_blocks(delta=.05, alpha=.05, power=.80):
    if not all(math.isfinite(x) for x in (delta, alpha, power)):
        raise ValueError("nonfinite power input")
    if not 0 < delta < 1 or not 0 < alpha < .5 or not .5 < power < 1:
        raise ValueError("invalid power input")
    normal = NormalDist()
    z = normal.inv_cdf(1-alpha/2) + normal.inv_cdf(power)
    return max(500, math.ceil(z*z*.25/(delta*delta)))
```

The formula is a normal-approximation Bernoulli design check for detecting a five-percentage-point change under independence, not a power guarantee for profits or Brier differences. Report remaining autocorrelation and reduce effective counts; if independence/support fails, do not certify. For each promotion, choose the primary cohort/method/costs before unsealing data, apply Holm correction across tested hypotheses, and require: zero hard-limit violations; no unresolved accounting/causality/recovery failures; paired block-bootstrap 95% upper confidence bound for Brier difference versus a training-frozen base-rate/deterministic baseline <0; 95% lower confidence bound of mean net executable P&L >0; positive net result under doubled fees and modeled slippage; stress replay at least including gaps, delist/unavailable book, private outage, cancellations and correlated selloff. Use 10,000 deterministic-seed block-bootstrap replications and record seed/block length/method in the evaluation manifest. A reused holdout cannot be called unseen; a changed proposal waits for a new sealed cohort. Also report nominal drawdown overshoot in gaps rather than treating a trigger as a guarantee.

No evidence is invented to meet these floors. Local test fixtures prove implementation behavior only. Insufficient data can complete the software Build but cannot authorize live strategy eligibility. Existing legacy backtests retain their own label and never count as verified causal evidence without receipt/depth/cost provenance.
- [ ] GREEN: future-field perturbation cannot change earlier decisions; same manifest/seed gives same replay; fees/slippage/latency/nonfills matter; purging and embargo remove overlap; toy success remains promotion-ineligible; holdout reuse rejected. Commit: `feat: validate replay and promotion evidence`.

## Task 20: reproducible deployment, operational readiness and CI

**Files:** create `deploy/autonomous.compose.yml`, `.github/workflows/autonomous-agent.yml`, `docs/runbooks/autonomous-agent.md`, `tests/autonomous/test_deployment.py`; modify `Dockerfile`, `requirements.lock` and `deploy/docker-compose.yml` only where the explicit overlay needs it; preserve current operator edits.

**Consumes:** tested roles and exact dependency/artifact identities. **Produces:** one-host four-role deployment, restricted mounts/secrets, readiness endpoints, metrics and a usable recovery/kill/rollback runbook.

- [ ] RED: deployment contract tests inspect the resolved configuration and built entrypoints to prove authority is single-replica, only it receives venue secret files/writable ledger, REFLEX receives only Jev secret, BRAIN has read-only exports, no network port exposes the control socket, dashboard initially binds localhost and all roles default unarmed/paper. Fail if a role gets unrelated provider secrets, a writable approval bundle, or its own independent trading loop.

```python
import json
import unittest
from pathlib import Path

class DeploymentTests(unittest.TestCase):
    def test_reflex_has_no_venue_secret_mount(self):
        config = json.loads(Path("deploy/autonomous.compose.yml").read_text())
        reflex = config["services"]["reflex"]
        secret_names = {item if isinstance(item, str) else item["source"]
                        for item in reflex.get("secrets", [])}
        self.assertIn("jev_api_key", secret_names)
        self.assertNotIn("coinbase_api_key", secret_names)
        self.assertNotIn("coinbase_api_secret", secret_names)
        self.assertTrue(reflex["read_only"])
```

- [ ] Run `.venv/bin/python -m unittest tests.autonomous.test_deployment -v`; expect RED until deployment contract exists.
- [ ] Implement a separate compose overlay using existing image conventions, explicit role commands, nonroot users, read-only application filesystem, account-local persistent volume and role-specific paths. Add required config/migration/research files to the image. Validate `python -m bot.autonomous.worker --help` and `python -m bot.autonomous.nightly --help` as inert parser checks; neither loads keys or calls a venue. Install from the exact lock; publish no image or remote service during local Build.

CI commands after dependencies exist:

```bash
.venv/bin/python -m unittest discover -s tests/autonomous -t . -v
.venv/bin/python -m unittest discover -s tests -t . -v
docker compose -f deploy/docker-compose.yml -f deploy/autonomous.compose.yml config --quiet
```

The second command is the existing regression gate and may include tests already found by the first; do not repeat it again without a change/failure. If existing repository tests contact external services, identify and isolate them explicitly instead of leaking credentials or silently skipping required coverage. CI blocks external network/model/venue writes. Read-only public capability verification and paid/inert authenticated canaries are separate Ship evidence steps when access is authorized.

Runbook includes startup, backup/restore, retention, storage-pressure shutdown, venue/API outage, hard kill versus risk halt, UNKNOWN intent reconciliation, residual inventory, session import, review resolution, bundle expiry, rollback and explicit live arming. Metrics include role liveness versus trading readiness, source/account ages, sequence gaps, current equity/limits/permits, unresolved orders/reservations, reject reasons, provider end-to-end latency p50/p95/p99, state/request tokens, fills/costs, Brier counts and review/promotion failures. Remove secrets from logs/prompts/exports. No claim of 81ms is made without deployment measurements.
- [ ] GREEN: full local checks, consistency backup/restore and simulated kill/restart/unknown-order drill. Commit: `build: package autonomous roles and operations`.

## Task 21: independent engineering review and Build acceptance

**Files:** create `docs/reviews/2026-10-16-autonomous-crypto-agent.md`; update only implementation files cited by actual review findings.

**Consumes:** complete local Build, original requirements, task records and deterministic acceptance evidence. **Produces:** two-stage specialist review and a concrete Build gate result.

- [ ] Refresh AgenKit memory and record commit/artifact/test identities. Mark every task with actual commands/results, never change an expected GREEN into a claimed pass.
- [ ] Obtain independent spec-compliance review first: every original requirement maps to a task and implemented behavior, including all six schemas and deliberate ineligibility where evidence is missing.
- [ ] Obtain code/accounting/security/reliability review second: file:line findings classified Critical/Important/Minor. Critical and Important findings block acceptance. Verify no model authority, no legacy submit bypass, no future data, no cash/unit/reset error, no UNKNOWN reservation release and no secret/approval injection.
- [ ] Fix cited findings in focused changes, run only affected checks plus required final regression when changes justify it, and record evidence. User accepts the Build gate before the separate Review/Ship acceptance step; combine the review evidence presentation where useful without assuming approval.
- [ ] Report software readiness separately from strategy/live readiness. Missing market edge, tokenizer proof or capabilities cannot become a fabricated pass. Do not merge, publish, deploy remotely or arm funds at this step.

## Task 22: shipping choice and separately armed live progression

**Files:** update `docs/runbooks/autonomous-agent.md`, `docs/reviews/2026-10-16-autonomous-crypto-agent.md` with actual approved release and operational evidence.

**Consumes:** accepted Build/Review, operator Ship choice and verified environment. **Produces:** the concrete selected PR/merge/kept branch/deployment outcome; live remains a separate authorization.

- [ ] Invoke AgenKit finishing-a-development-branch and present concrete reviewable options/PR content with exact validation and remaining limitations. The operator chooses PR/merge/keep/discard and any deployment target; no choice is presumed.
- [ ] For approved deployment, first run credential-free paper with deterministic fixtures, then real-data shadow while orders remain disabled. Synthetic paper forecasts cannot qualify live strategies.
- [ ] Obtain provider-approved state token accounting, verify actual pinned Jev/BRAIN availability using authorized access, measure whole-call latency and verify venue bounded/client-ID/fill-history capabilities. Missing proof keeps entries disabled. Never request keys in chat or log values; document environment/secret-file names.
- [ ] Refresh dated primary research and complete the required quantitative/causal evidence for every live-admitted candidate. Publish at most the independently qualified universe, including an empty universe. Do not label six hypotheses as proven asymmetric trades.
- [ ] Before live arming, require explicit operator-approved venue/account, capital allocation, per-order cap, entry/reducing permit duration and external renewal procedure, approved bundle, reconciled opening balances/bases/foreign orders, and drill evidence. Until those values exist, no live submission is possible. Deploying code never arms it.
- [ ] Start only the approved limited live exposure, monitor reconciled fills/equity and retain operator kill/rollback controls. Overnight improvements remain gated. No guarantee of profit, loss ceiling, flawless uptime or a next-open promotion is issued.

## Coverage and Plan self-review

| Approved requirement | Implementing tasks and acceptance evidence |
|---|---|
| AgenKit phases and explicit operator gates | 1–22; this Plan gate, isolated Build, independent Review, explicit Ship/live choice |
| Separate BRAIN and REFLEX | 7, 14–18, 20: process credentials, numeric-only state and proposal-only models |
| Current primary research and 5–10 candidate coverage | 6 and 22: six cited provisional candidates, fresh complete evidence before admission, no invented metrics |
| All five Jev fields, parallel one-call judgments | 6–7: six definitions, five independent questions per included candidate, exact enums and 0–3 integer quality |
| Causal state <400 tokens with deterministic features | 4–5, 16, 19: three cutoffs, revision archives, hand-checks and provider token proof |
| Strict thresholds and calibrated quarter Kelly | 8–11: probability/confidence distinction, boundary tests, supported cost-positive sizing |
| 15% drawdown, daily/position caps and every-order kill | 2–3, 11–14, 17: unit NAV, durable latches, atomic reservations and old-route closure |
| Confidence <.60 or crisis to deep review | 10, 14, 18: persistent pauses, deduplicated jobs, no model rearming |
| Nightly full-session review and Brier | 8, 18–19: matured labels, complete-session reports, missingness and unseen comparisons |
| No ungated overnight promotion | 15, 18–22: immutable manifests and operator gate evidence |
| Survival through costs/failures rather than appearance | 12–13, 19–22: adverse executable fills, recovery drills, insufficient-data vetoes |

Self-review completed for scope, type/signature ownership, source/knowledge time, threshold equality, release-independent action identity, fee/label corrections, accounting units, role permissions, exact file ownership, and tests versus live evidence. The plan introduces conservative execution guards and explicit sample requirements for operator review; it does not silently change approved hard ceilings.

**Plan approval request:** approve this task sequence and validation requirements to start the isolated test-first Build. Recommended execution is task-by-task specialist workers with review between tasks; inline batches using executing-plans are available. No actual test result, deployment readiness or qualified trade is claimed by the examples in this plan.

## WHAT COULD I BE WRONG ABOUT?

The full state may not fit the provider tokenizer, useful probabilities may never calibrate under the supported regimes, current venue semantics may prevent safe recovery, the independent sample floors may require months of shadow data, and costs may eliminate all six candidates. Bin calibration, payoff estimates and bootstrapped intervals may still fail during structural change. The service can be correctly implemented while producing no eligible trades; relaxing evidence or delegating a hard limit to a model is not an acceptable shortcut.
