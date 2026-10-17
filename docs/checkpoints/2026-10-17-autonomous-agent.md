# Autonomous crypto agent checkpoint

## Authorization and saved state

The operator approved the specification, architecture, and implementation plan, then explicitly requested saving progress before continuing and authorized autonomous engineering execution. The proposed 22-task plan is approved. No additional product requirements were supplied after the explanation. Separate deployment, shipping and real-money gates remain in force; no live credentials or orders are authorized by this checkpoint.

Saved documents:

- `docs/specs/2026-10-16-autonomous-crypto-agent-design.md`
- `docs/architecture/2026-10-16-autonomous-crypto-agent.md`
- `docs/ADR/2026-10-16-brain-reflex-boundaries.md`
- `docs/plans/2026-10-16-autonomous-crypto-agent.md`
- `docs/research/2026-10-16-autonomous-agent/RESEARCH.md` and dated evidence

Research contains six provisional candidates and zero qualified live trades. No current valuation edge, calibrated profit probability, provider tokenizer compliance, authenticated model access, measured Jev latency, or bounded venue capability is certified.

## Engineering state before Build

Original checkout: `/home/reckless/crypto-trading-platform`, branch `main`, starting HEAD `71ab6c2`. Existing operator changes in `.gitignore`, `AGENTS.md`, `README.md`, dashboard, Binance collector and deployment files must be preserved. Kit/memory/config directories are preexisting untracked work. Only explicit project documents are included in the initial checkpoint commit.

The installed `.venv` uses Python 3.14.4; the deployment image specifies Python 3.12. Record baseline results separately and verify supported deployment Python before shipping. Use native managed worktree tooling if available. Uncommitted source changes do not automatically transfer to a new worktree: retain a local binary patch and apply/record it deliberately.

## Task ledger

### Build setup evidence

- Approved design/research/plan checkpoint committed as `bf75801` before implementation.
- Native managed worktree created at `/home/reckless/.codex/worktrees/bd91/crypto-trading-platform`; the preserved operator patch was applied there with approval. Persistent sandbox restrictions prevent normal edits there. Following the worktree skill's sandbox fallback, implementation uses the writable original checkout on `feat/autonomous-crypto-agent`, with all operator changes preserved. The worktree remains a backup, not an active second implementation.
- Original tracked operator changes preserved in `/tmp/autonomous-agent-operator-20261017.patch`; a separate feature-branch baseline commit records them before task commits.
- Baseline command `.venv/bin/python -m unittest discover -s tests -v`: 32 tests pass outside sandbox under Python 3.14.4. Same sandboxed command stalls in FastAPI TestClient and was interrupted; no production change made to work around it. Preexisting Starlette/httpx deprecation warning recorded.
- Created `.venv-autonomous` using installed CPython 3.12.14 with `uv`, matching the deployment Python minor version. Installed existing development requirements. Under Python 3.12 all 32 original tests pass; concurrent Task 1's 10 new tests showed expected missing-contract RED failures. No original-test regression.
- Task-specific briefs, verbatim global constraints, reports and task review packages are stored in `.agenkit/sdd/`; durable progress is `.agenkit/sdd/progress.md`.

| Task | State |
|---|---|
| 1 Domain contracts/defaults | complete; commits a77407a + 76e0527; 26 tests Python 3.12; independent spec/quality review passes |
| 2 Durable journal/reservations | complete through 57509ea; 49 focused tests pass; independent spec/quality review passes |
| 3 Equity/flows/latches | pending |
| 4 Causal source/archive | pending |
| 5 Numeric state/token proof | pending |
| 6 Research/six schema definitions | pending |
| 7 Jev client/validation | pending |
| 8 Outcomes/calibration | pending |
| 9 Kelly sizing | pending |
| 10 Policy/escalation | pending |
| 11 Hard risk | pending |
| 12 Authority/paper execution | pending |
| 13 Live transport/reconciliation | pending |
| 14 Authenticated controls | pending |
| 15 Release approvals | pending |
| 16 Role workers/streams | pending |
| 17 Dashboard bypass closure | pending |
| 18 Nightly/BRAIN proposals | pending |
| 19 Replay/promotion evidence | pending |
| 20 Deployment/operations | pending |
| 21 Independent review/acceptance | pending |
| 22 Ship/live gates | not authorized yet |

Each task requires observed RED/GREEN checks and task review. Baseline and task results will be appended with actual commands, working directory, outcomes, commit identities and unresolved findings. A unit-test pass cannot substitute for actual market evidence or live promotion approval.

### Task 1 acceptance

Strict frozen domain records, canonical serialization, monetary validation, safe configuration and numeric field dictionary implemented. RED/GREEN evidence: initial10 missing-contract failures, implementation20 passing tests, then independent review identified numeric-ID and discrete-mask issues; test-first fixes increased coverage to26 passing Python3.12 tests. Both independent review stages now pass with no open findings. Task15 must implement core-manifest identity excluding embedded approvals and a separate package hash; this is recorded in its task brief. Full reports/diffs: `.agenkit/sdd/task-01-*`.

### Team research and safety artifacts

The operator explicitly requested a set of agents. A sequential core implementer and independent code reviewer are supported by separate market-research and safety-audit agents. Fresh advisory research is saved at `docs/research/2026-10-17-autonomous-agent/RESEARCH.md` with31 public evidence artifacts, whose hashes were verified locally. All six candidates remain ineligible. The safety acceptance audit is saved at `docs/reviews/2026-10-17-autonomous-safety-contracts.md`; its requirements are handed to owning task briefs. It clarifies activation requires unarmed AND reconciled state and distinguishes retrospective model replay from genuinely prospective forecast evidence. These reports are not implementation certification.

Task2 commit9977b03 implements schema/journal/reservation storage;33 focused Python3.12 tests pass. Task3 must add actual native fill/basis/fee/flow/liquidation marks; Task12 must add authority-issued reduction/replacement slots; Task13 final fill/fee reconciliation; Task11 full pure risk. Intermediate components remain failclosed and unarmed.

Independent Task2 review found one Critical dispatch-durability issue, three Important historical export/order/manifest issues, and a Minor Decimal precision issue. The implementer is fixing all five before independent re-review. Task2 is not accepted yet. Review evidence is saved at `.agenkit/sdd/task-02-review.md`. The separate research agent is checking official AAVE/PENDLE evidence; unverified current metrics and catalysts remain unknown.

An additional accounting specialist produced `docs/reviews/2026-10-17-accounting-implementation-guidance.md`. Repeating unit issuance/NAV cannot use rounded Decimal projections as hard-risk authority. Task3 may add a versioned accounting migration, append-only exact rational evidence and the minimal typed AccountView companion plus tests; Task11 must consume the exact evidence. This implements the approved exact comparison requirement. It does not qualify any account for trading. Ownership and acceptance details are recorded in Task3/11 briefs.

### Task 2 acceptance

Accepted through `57509ea` after independent review and three fix rounds. All R1–R5 closed;49 focused Python3.12 tests pass. Dispatch returns only after its own durable commit. Historical exports bind account revision and all required anchors to a cutoff-visible committed evidence prefix; complete causal policies and durable event order prevent mixed historical states. All collaborators/imports must use `ledger.transaction()`. Evidence identities must survive any future storage compaction. Full report/review remain `.agenkit/sdd/task-02-report.md` and `task-02-review.md`. Task3 is next; no live readiness is claimed.
