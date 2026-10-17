# ADR: BRAIN / REFLEX boundaries and one order authority

Status: accepted with the operator's Architecture approval on October 17, 2026. See the [architecture](../architecture/2026-10-16-autonomous-crypto-agent.md) and approved [specification](../specs/2026-10-16-autonomous-crypto-agent-design.md).

## Context

The existing dashboard can instantiate a live adapter directly; its flatten path bypasses the kill switch. Existing adapters do not provide the durable cash/equity journal, uncertainty recovery and shared authority required by the new agent. Model confidence is also not evidence of a profitable executable trade.

## Decision

Use four process roles: authority, Jev REFLEX, research/review BRAIN, and dashboard. Only authority receives exchange trading credentials and can write authoritative accounting/risk state. Model roles return judgments and proposals through typed bounded protocols. Only an authenticated operator can arm, reset permissible controls, or activate a fully approved release.

Use one local account-volume process lock and SQLite transactions for one account on one host. Serialize control and order dispatch. Store intents and reservations before network writes; uncertain submission outcomes keep their reservations until reconciled. No automatic multi-replica failover is allowed because the venue does not honor a database fencing epoch.

Apply policy/risk to all managed-account entry, exit, manual, dashboard and paper paths. Hard kill forbids new orders including exits; a distinct authorized risk halt can allow reductions. Previously sent orders can still fill and require cancellation/reconciliation. Use bounded verified venue execution; never fall back to unbounded market orders.

Use unitized liquidation NAV for deposit-neutral drawdown and daily loss, a persistent double-entry journal, and immutable source/decision/release lineage. Pin Jev/schema/tokenizer/calibrator versions. Unsupported calibration or token accounting produces zero new risk.

## Alternatives

Keeping submission inside the dashboard makes web restarts and multiple workers potential execution races. Independent distributed execution services would add coordination without venue-enforced fencing. A standalone replacement would duplicate existing market/display/deployment integrations. The selected design gives one clear execution owner while reusing the repository.

## Consequences

The service needs a new journal, control protocol, recovery state machine and role-specific deployment secrets. Initial availability depends on one host, and restart always requires reconciliation and renewed arming. BRAIN can prepare improvements overnight but cannot promote them without AgenKit approvals. Existing direct submission paths must be replaced before managed-account rollout.

## Validation required before shipping

Prove common-path hard limits, cash/basis conservation, cash-flow-neutral NAV, under-400-token encoding, version compatibility, uncertain-order recovery, kill ordering, late fills, source causality and gate-protected release promotion. Measure actual provider latency, venue execution behavior and net unseen trading evidence. Until then this remains an architecture decision, not a production claim.
