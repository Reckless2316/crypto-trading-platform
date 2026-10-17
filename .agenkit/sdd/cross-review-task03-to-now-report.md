# Cross-task contrarian review — Task3 through Task9 WIP

Date: 2026-10-17 UTC. Reviewer: independent high-capability agent, medium reasoning. Review was read-only against frozen snapshots; no tests or network calls.

## Reviewed snapshots

- Main diff: `.agenkit/sdd/cross-review-task03-to-current.patch`, base `67ad62e2ddf0644fbffcd56aa5957e13e2957384`, SHA-256 `b83e17b7a61214a2f38920978b2bf00460a6d88801fded6841d5444cd9146e7d`.
- Task6 source supplement: `.agenkit/sdd/cross-review-task06-source-supplement.patch`, SHA-256 `bed374593fb9ec24320ee07b51cbba5541c394bf0c4b1c5c73b4884b8c076354`.
- **Verdict: changes required.** This was a prioritized static audit, not exhaustive verification. It did not reproduce failures, benchmark longevity, verify provider behavior, or re-run tests. No Critical issue was confirmed. It does not accept any WIP.

## Findings, prioritized

### Important — historical source selection can include records committed after the cutoff

**Files:** `bot/autonomous/state.py:742`, `bot/autonomous/state.py:789–800`.

`ingest` accepts supplied `known_at_us` values before actual database insertion. `StateEngine.select` applies event/receipt/knowledge cutoffs but does not constrain `source_events.recorded_at_us` before ranking revisions. Reproduction: evaluate cutoff 100; at clock 200 ingest a source event with event/receipt/known timestamps 90/95/99; evaluate cutoff 100 again. The new event appears; a higher late revision can replace the previously selected historical version.

**Required fix:** filter source rows by `recorded_at_us <= knowledge_cutoff_us` before revision ranking, or use a durable committed-prefix watermark. Preserve the existing recorded-time filtering for invalidations. Add regressions for both a late first event and a late superseding revision.

**Impact:** Tasks4/5 local causal correctness; snapshot reconstruction, lineage, Jev state, downstream forecasts and replay. This finding overrides the earlier Task4 PASS for the affected invariant.

### Important — state snapshot requires exchange events exactly on minute boundaries

**Files:** `bot/autonomous/state.py:449`, `:461`, `:466`.

Only books whose event timestamp equals one of 61 minute boundaries contribute. A continuous exchange feed with observations at `xx:xx:59.900` and `xx:xx+1:00.100` can fail to supply the required boundary midpoint and volatility row.

**Required fix:** add a causally bounded boundary sampler with age/sequence requirements and preserved source lineage, or explicitly enforce and implement a boundary-sampler input contract before integration. Never rewrite exchange timestamps.

**Impact:** production promotion; unresolved Task4→5 data integration dependency.

### Important — state lifetime storage and replay work are unbounded

**Files:** `bot/autonomous/state.py:403`, `:529`, `:673`, `:789`.

Input/snapshot/encoding/HOLD caches have no eviction, selection reads all historical source events, and each snapshot gathers historical book references. A continuous process accumulates memory and increasingly large replay work, threatening 24/7 freshness deadlines.

**Required fix:** bounded working caches, durable retry lookup, and checkpoint/window reconstruction that preserves the required ancestry. Add measured longevity evidence after remediation.

**Impact:** 24/7 production promotion.

### Important — Tasks8/9 are partial, fail-closed implementations

**Files:** `bot/autonomous/calibration.py:271`, `bot/autonomous/sizing.py:54`, `bot/autonomous/policy.py:83`.

Edge estimation always returns unavailable; sizing always rejects; policy never creates an authorized proposal. A verified production outcome resolver, persisted calibration attestations, executable-book contract, and sizing integration remain absent.

**Impact:** full Task8/9 acceptance and any finished-product claim. Tests proving refusal are useful safety tests but do not establish a complete trading loop.

## Limited positive findings

The inspected Task6 code distinguishes provenance, units, freshness, approval subjects and missing evidence. No additional consequential defect was found in that limited pass. The Jev client showed strict parsing, bounded response size, deadline checks and sanitized exception categories.

