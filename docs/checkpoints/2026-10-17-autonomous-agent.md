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
| 1 Domain contracts/defaults | pending |
| 2 Durable journal/reservations | pending |
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
