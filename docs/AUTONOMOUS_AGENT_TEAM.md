# Autonomous trading agent engineering team

Created October 17, 2026 under the operator's explicit request for agents/subagents. The project uses the installed AgenKit workflow and its subagent-driven-development skill.

## Assigned responsibilities

| Agent | Responsibility | Current evidence |
|---|---|---|
| AgenKit lead (`/root`) | Directly lead the approved AgenKit plan, assign scoped specialists, preserve progress and enforce review gates | [Build checkpoint](checkpoints/2026-10-17-autonomous-agent.md); separate orchestrator retired by operator instruction |
| Core implementer (`build_task_02`) | Own the ledger schema, persistence code and focused tests for the current task; fix review findings | Ledger accepted through `57509ea`; 49 focused tests passing; next task is accounting |
| Accounting/debugging specialist | Complete native fill/basis/fee accounting, exact capital-flow NAV, evidence-backed readiness and persistent loss latches | Implementation `08270b3`;117 focused tests pass; accounting review blockers require fixes before acceptance |
| Independent reviewer (`review_task_02`) | Check specification compliance, then quality, durability and correctness; reject incomplete work | All five initial ledger findings closed; specification and quality review pass |
| Market researcher (`market_research`) | Retrieve public primary evidence; distinguish facts, estimates, speculation and missing data | [Dated research](research/2026-10-17-autonomous-agent/RESEARCH.md) and completed focused AAVE/PENDLE supplement; all candidates ineligible |
| Accounting specialist (`accounting_design`) | Define economic invariants and acceptance cases before the accounting build | [Accounting guidance](reviews/2026-10-17-accounting-implementation-guidance.md) |
| Safety specialist (`safety_audit`) | Audit deterministic controls, recovery and promotion evidence requirements | [Safety acceptance audit](reviews/2026-10-17-autonomous-safety-contracts.md) |
| Integration specialist (`integration_audit`) | Map existing order-entry paths and credential boundaries for the later authority/dashboard/deployment work | [Integration checklist](reviews/2026-10-17-integration-entrypoints.md) |

Completed agents remain available for scoped follow-up. A fresh implementer receives each subsequent task and explicit ownership. Advisory work may run in parallel; one implementer owns the core at a time. Independent review follows every implementation task and the whole branch receives a final review.

The operator canceled the separate orchestrator and Notion work. AgenKit is the sole engineering workflow; root assigns its specialists directly. No Notion pages were created. Jev schemas, client, calibration, policy and nightly proposals are built and reviewed under the same AgenKit gates; Jev remains the runtime REFLEX judge.

## How work advances

1. Orient through AgenKit local memory.
2. Use the approved specification, architecture and implementation plan.
3. Build the current task with observed failing tests, then passing tests.
4. Review specification compliance and code quality independently.
5. Fix blocking findings and repeat review before advancing.
6. Save accepted work, test evidence, remaining gaps and the next task.

The remaining work covers accounting, causal market state, bounded model inputs, research/schema definitions, Jev validation, outcome calibration, sizing, policy, hard risk, execution/reconciliation, authenticated controls, release approvals, process separation, dashboard integration, nightly review, replay evidence and operations. The [approved plan](plans/2026-10-16-autonomous-crypto-agent.md) defines exact paths and acceptance requirements; the checkpoint records actual completion.

## Runtime separation

These are engineering agents. The eventual runtime has distinct BRAIN, REFLEX, authority and dashboard roles. Only authority may hold venue credentials or create orders. REFLEX judges bounded state; code controls sizes, thresholds and side effects. BRAIN proposes research and release changes through review.

Build approval does not activate live trading, qualify a strategy, establish model calibration or approve deployment. All six research candidates currently remain ineligible. Ship/live approval and the required evidence remain separate gates.
