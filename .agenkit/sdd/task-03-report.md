# Task 3 implementation report

Status: **DONE_WITH_CONCERNS** — Task 3 implementation and local acceptance complete; independent conductor review pending. Concerns below identify later-task acquisition/authorization proof and conservative operational limitations, not claimed live readiness.

## Scope and commits

- Implementation/task source HEAD: **`08270b33e5911a0dd42dd8ad862b99f5b0c2af0c`**, `feat: account for equity and persistent risk loss`.
- Original task base supplied by conductor: `67ad62e2ddf0644fbffcd56aa5957e13e2957384`. Accepted Task 1/2 were retained, including Task 2's committed-prefix export and non-nestable dispatch fixes.
- Continued the existing uncommitted partial Task 3 implementation; no recovery copy or accepted work was discarded. The earlier reported 87 tests were not used as current evidence. Recovery first reproduced a fresh 95-test GREEN baseline, then added/fixed the acceptance gaps below.
- Worked in the existing `feat/autonomous-crypto-agent` checkout, as explicitly authorized after native worktree creation was blocked. Preserved all other operator/conductor files. No all-files git add, unrelated commit, merge, remote push, deployment, arming, credential access, authenticated/paid API, or venue/model call occurred.
- Owned paths committed: `bot/autonomous/ledger.py`, `bot/autonomous/risk.py`, `bot/autonomous/contracts.py`, `data/migrations/002_autonomous_accounting.sql`, `tests/autonomous/test_ledger.py`, `tests/autonomous/test_risk_limits.py`, `tests/autonomous/test_contracts.py`, `tests/autonomous/fixtures/external_flows.jsonl`.
- Conductor explicitly extended ownership to `tests/autonomous/support.py` for the additive exact-NAV ready fixture; a focused contract regression now exercises it. This report is committed separately so it can name the full implementation commit; the conductor handoff also supplies the final report commit HEAD.

## Interfaces and persisted authority

`nav_after_flow(equity: Decimal, units: Decimal, flow: Decimal)` retains the public Decimal interface and 50-digit deterministic report context. `loss_fractions(nav, high_water, day_open)` produces deterministic Decimal reports. Neither report determines persisted units or loss authority. `loss_triggers(Fraction, Fraction, Fraction)` compares exact integer cross-products at drawdown `>=15%` and UTC daily loss `>=2%`, including equality, independently of ambient Decimal context.

Added frozen `ExactRatio` and `ExactNavEvidence`; `AccountView.exact_nav` defaults absent for construction compatibility. A ready view requires positive reconciliation/baselines and exact evidence bound to account identity, current revision, equity, mark identity and valuation watermark. Its Decimal unit/NAV/HWM/day projections must equal the deterministic bounded projections of that authority. Immutable exact companion records retain the mark revision; the current account view explicitly binds unchanged economic evidence to its current read revision and requires a matching journal watermark before readiness.

Rational components are canonical reduced signed integer TEXT with a positive denominator, at most 4096 characters/component (approximately 13.6 kbits). Validation occurs before parsing/allocation; chunked conversion avoids changing Python's global integer-string setting. Fractions are reduced after operations. Native Decimal input/projections preserve the frozen 38-digit/38-place contract; USD postings/projections are checked signed int64 microdollars. Display projections use an explicit local floor context. Repeating unit issuance is persisted exactly, including repeated deposits/withdrawals; no float, unit truncation, fraction rounding, widened Decimal contract, or rounding-account substitution determines loss authority. Real resource/display failures retain factual transfer evidence, append an incident and inhibit readiness.

Migration 002 adds append-only `accounting_evidence`, `accounting_operations`, `exact_marks`, `exact_flows`, `accounting_findings`, and `accounting_rounding`, plus mutable checked `accounting_state` projections. Existing migration 001/checksum is unchanged. Exact marks reference concrete equity marks; exact flows reference the concrete external flow and exact preflow mark. Startup verifies schema protections, canonical content, reduced ratios, equity/NAV identity, mark projections, exact flow deltas/after-units/closure chain, mutable unit/HWM/day/UTC-boundary state, replayed balances/basis, USD journal and native journal projections, and bounded rounding residuals.

## Local economic import and atomicity

The explicit seam is paper-only retained evidence: `import_local_evidence(account_id, evidence_id, kind, payload, *, event_at_us, received_at_us, known_at_us)`, followed by `initialize_account`, `record_fill`, `correct_fill`, `record_flow`, or `record_mark`. Input identity binds retained canonical payload bytes, content hash, account/kind/provenance and causal timestamps. A changed identity rejects. An account switched to live cannot use this seam; a Boolean or arbitrary supplied digest never constitutes authenticated venue evidence. Callers can wrap import plus admission in `ledger.transaction()` when one outer transaction is needed.

Every economic admission uses the Task 2 immediate/nestable transaction seam: source identity, immutable fill/flow/correction, journal/native postings, basis and balance projections, reservation consumption, revisions/readiness, exact units/marks and loss/control transitions commit together. Nested failures roll back the entire outer operation; SQLite uncertainty disables dispatch. `mark_dispatching()` remains non-nestable and must run only after the reservation transaction commits. There is no network or await in an economic transaction.

Fill economics use reported settled USD quote, with a documented maximum one-microdollar deviation from gross base times price. Unknown semantics, basis or insufficient assets cannot fabricate balances. Buy/sell weighted-average basis removes the remaining residual on final disposal. USD fees are expensed once. Base-paid fees explicitly acquire gross base before fee disposal, support net-reported buy delivery, and require extra base availability on a sell. Third-asset fees require retained, directional, fresh FX legs; disposal removes their basis and records fee expense plus the disposal profit difference. Missing final fees/FX retains confirmed principal/native facts and suspense, retains fee/loss capacity, creates a durable review incident and prevents readiness. Invalid unknown fill semantics retains the source and incident without inventing principal economics.

Each USD transaction balances exactly. Native conservation is checked separately for every asset, with corresponding settlement/capital/fee legs. Partial basis allocation floors and keeps the residual in remaining basis; fee valuation ceilings and conversion/settlement residuals are explicitly retained in reduced-rational `accounting_rounding` evidence. Journal rounding accounts are explicit zero legs when no monetary residual needs a posting; they never plug an unexplained difference. Sub-microdollar USD principal or fees are rejected rather than silently rounding native cash.

Correction identity binds predecessor content and immutable trade identity. A unique compensation replays corrected acquisitions through subsequent sales, rebuilding current balance/basis, journal/native differences and reservations. Original fills/marks remain immutable. Identical corrections are no-ops; stale/conflicting chains reject. A correction appends review/current valuation evidence and cannot clear old drawdown/review incidents. Late fills after a terminal projection still journal exactly once and create an incident rather than releasing capacity from status alone.

Reservations consume confirmed principal quantities/gross notional; remaining principal capacity is recalculated at the original limit. Full fee bounds and pending modeled-loss capacity remain reserved pending authenticated terminal settlement proof. Reserved cash continues to count as owned liquidation equity.

## Capital, liquidation marks and persistent loss state

Verified local flow identity includes transfer key, signed native amount, signed USD capital value, fee, and current exact preflow mark. USD native/value equality is exact at microdollar precision. Non-USD capital requires retained FX conversion evidence; stablecoins are distinct assets. Deposits add asset basis/capital, withdrawals dispose owned basis with the corresponding capital/disposal profit legs, and transfer costs remain fee losses. Changed/unknown/unvalued transfers append incidents; unexplained balance changes never become capital or profit.

Flow issuance/redemption is exact: `delta_U = old_U * flow_usd_u / preflow_equity_usd_u`. Retain the preflow mark and original HWM/day/incident history. Full withdrawal records an explicit closure and zero-unit state before any NAV division. Closure requires all non-USD holdings/costs settled. A positive redeposit uses the retained preclosure NAV and preserves latches; it does not reseed a new account. Current equity excludes reservations only from spendable capacity, not from owned equity.

Marks reconcile every native holding and open intent; require full executable bid depth in descending order, current directional FX when quote is not USD, positive equity/units/baselines, private observation, and explicit liabilities/remaining exit costs. Already paid fees reduce holdings/cash and are not deducted twice. Missing assets/depth/FX, insufficient depth, mismatched balances/orders, stale heartbeat and missing midnight evidence inhibit readiness; unknown holdings are never silently worth zero. Books expire after 2 seconds, heartbeat after 5 seconds, reconciliation after 15 seconds. New economics invalidate earlier reconciliation; derived marks can latch losses but cannot restore readiness.

Derived valuation reuses retained depth across flows with its original freshness, binds the new economic event, and marks at current local knowledge time. It never fabricates a new venue observation. Drawdown/daily breaches persist in the same mark/economic transaction, including while unarmed and at nonpositive equity. HWM is monotone across capital flows and favorable corrections.

UTC opening baselines require genuine midnight evidence. Later book observations cannot be relabeled midnight. A retained genuine boundary observation can restore the baseline after an outage only when no later economics invalidate it; that historical mark remains unready. Durable rollover-pending state keeps the old daily inhibition until fresh current reconciliation. Missing boundary evidence remains unready. Drawdown, review, crisis, hard-kill and restart-unarmed state survive midnight, flows, corrections, closure and reopen.

## Historical export integration

All six immutable accounting tables have explicit causal timestamp and primary-key policies, participate in the outer-commit evidence prefix, and are concrete-reference checked in exports. Mutable accounting state is omitted. Accounting sources, rational companions, rounding and compensating operations cannot cross an earlier committed cutoff merely because an independently supplied event time is earlier.

There is one explicit backward compatibility policy: migration-001 history maps have zero prefixes for the six named migration-002 tables that did not exist then. Future/unrecognized/malformed policy maps fail closed. The acceptance test creates a genuine migration-001 database/history, applies 002 through normal open, and exports the original committed prefix successfully. Other acceptance cases prove flow and correction/rounding evidence stays outside earlier cutoffs and original fill economics remain intact.

## Test-first recovery evidence

AgenKit TDD/systematic debugging skills were read and applied. The retained partial implementation was continued under the conductor's explicit recovery instruction. Fresh recovery baseline: 95 tests, OK. New regression results before their fixes:

1. First accounting batch: 21 tests, **FAILED (failures=3, errors=3)**. Reproduced loss-latch gaps after flows, missing-FX exception leaving previous readiness, non-USD flow rejection, unknown semantics without durable inhibition, and mutable HWM tampering. After fixes: 21 tests, OK.
2. Exact contract/support batch: 4 tests, **FAILED (failures=1, errors=1)**. Reproduced unbound Decimal projection acceptance and ready fixture without exact evidence. After fixes, exact plus accounting: 25 tests, OK.
3. Expanded native fee/late fill/correction/export cases: 28 accounting tests, **FAILED (failures=1)** on missing after-unit chain validation. Resource overflow regression separately reproduced `ContractViolation` after economic work; fixed by preflight bounded projection and durable incident.
4. UTC pair: 2 tests, **FAILED (failures=2)**. Reproduced future depth relabeled midnight and inability to restore genuine retained boundary after outage; fixed observation time validation plus current reconciliation/reset state. Accounting suite: 30 tests, OK.
5. Migration/fault/fixture batch: 6 tests, **FAILED (errors=1)** on the actual legacy committed watermark map. Fixed the explicit 002 empty-prefix migration policy. Twelve separate write-fault subcases all proved identical before/after economic DB contents and disabled dispatch.
6. UTC projection tampering: 1 test, **FAILED (failures=1)**; fixed startup reconstruction of genuine boundary identity.
7. Final precision regression: 1 test, **FAILED (failures=1)** because submicro USD native principal was accepted; restored exact USD precision admission and durable readiness inhibition.

Final covering command after the last production/test edit, using Python 3.12 from `.venv-autonomous`:

```text
.venv-autonomous/bin/python -m unittest tests.autonomous.test_contracts tests.autonomous.test_ledger tests.autonomous.test_risk_limits -v
Ran 117 tests in 1.761s
OK
exit status: 0
```

Full untruncated captured output: `/tmp/task03-final-tests.log`, also embedded below. There are 30 contract tests, 83 ledger tests (49 preserved Task 2 tests plus 34 accounting tests), and 4 risk/flow tests. The write-fault test additionally executes 12 table-specific subcases. `git diff --check` and `git diff --cached --check` passed before the implementation commit. No broader suite or live verification is claimed.

## Concerns, practical limits and later-task integration

- **Task 13:** authenticated venue acquisition, genuine live heartbeat/depth/FX/fee/transfer/terminal-cumulative proof, transport and terminal reservation release are not supplied by local fixtures. The paper seam explicitly refuses live mode. These tests are implementation evidence only. Unknown final fees/review findings continue to inhibit readiness; original fee and modeled-loss capacity is conservatively retained until the later validated settlement/control paths resolve it.
- **Tasks 11/12/14:** full proposal/Kelly/portfolio assessment, authority-generated reduction/replacement slots, authenticated permits, incident review resolution and cancellation/transport actions retain their approved ownership. Economic readiness neither arms nor authorizes reductions. No halt places an exit order. Existing Task 2 initial-entry-only admission remains in force.
- A historical boundary can be restored locally only when retained complete observations still cover the current economic projection. If intervening economics prevent that proof, it remains unready rather than fabricating a baseline; later archival/reconciliation acquisition must provide appropriate retained proof. Full closure requires asset/cost settlement first.
- Rational/Decimal/int64 bounds are documented and fail closed with retained facts. Normal repeating issuance is accepted; genuine resource exhaustion cannot silently look successful.
- Exact evidence/projections and committed history require retained append-only tables/row prefixes. No compaction/rowid rewrite/VACUUM retention migration was added. Existing incompatible development databases with a different unfinished 002 checksum are not silently adopted.
- Weighted-average correction replay is deliberately synchronous and complete over retained operations for deterministic correctness. Production history growth may later justify a reviewed checkpoint/index strategy without weakening lineage; no performance benchmark is claimed.
- Independent review has not yet accepted this implementation. No known Task 3 requirement was deliberately scoped out; the boundaries above are explicit acquisition/assessment/authorization ownership and conservative proof limits.

## Full final covering output

```text
test_book_delta_deletions_and_signed_metrics (tests.autonomous.test_contracts.ContractTests.test_book_delta_deletions_and_signed_metrics) ... ok
test_canonical_and_finite (tests.autonomous.test_contracts.ContractTests.test_canonical_and_finite) ... ok
test_canonical_rejects_lossy_keys_and_unsupported_values (tests.autonomous.test_contracts.ContractTests.test_canonical_rejects_lossy_keys_and_unsupported_values) ... ok
test_continuous_wire_slots_obey_dictionary_ranges (tests.autonomous.test_contracts.ContractTests.test_continuous_wire_slots_obey_dictionary_ranges) ... ok
test_decimal_instrument_scale_supports_full_precision (tests.autonomous.test_contracts.ContractTests.test_decimal_instrument_scale_supports_full_precision) ... ok
test_decimal_precision_scale_and_explicit_rounding (tests.autonomous.test_contracts.ContractTests.test_decimal_precision_scale_and_explicit_rounding) ... ok
test_defaults_disable_exposure_and_fix_approved_limits (tests.autonomous.test_contracts.ContractTests.test_defaults_disable_exposure_and_fix_approved_limits) ... ok
test_dictionary_covers_all_compact_slots (tests.autonomous.test_contracts.ContractTests.test_dictionary_covers_all_compact_slots) ... ok
test_dictionary_documents_wire_metadata_and_thesis_bit_width (tests.autonomous.test_contracts.ContractTests.test_dictionary_documents_wire_metadata_and_thesis_bit_width) ... ok
test_direction_distribution_and_toxic_threshold (tests.autonomous.test_contracts.ContractTests.test_direction_distribution_and_toxic_threshold) ... ok
test_discrete_wire_slots_have_exact_integer_domains (tests.autonomous.test_contracts.ContractTests.test_discrete_wire_slots_have_exact_integer_domains) ... ok
test_eligibility_needs_evidence_and_ondo_cannot_activate (tests.autonomous.test_contracts.ContractTests.test_eligibility_needs_evidence_and_ondo_cannot_activate) ... ok
test_encoded_state_checks_complete_numeric_shape (tests.autonomous.test_contracts.ContractTests.test_encoded_state_checks_complete_numeric_shape) ... ok
test_every_architecture_record_is_frozen_and_typed (tests.autonomous.test_contracts.ContractTests.test_every_architecture_record_is_frozen_and_typed) ... ok
test_frozen_strict_roundtrip (tests.autonomous.test_contracts.ContractTests.test_frozen_strict_roundtrip) ... ok
test_hash_type_id_and_unknown_enum_are_rejected_at_wire_boundary (tests.autonomous.test_contracts.ContractTests.test_hash_type_id_and_unknown_enum_are_rejected_at_wire_boundary) ... ok
test_immutable_nested_values_and_declared_probability_bounds (tests.autonomous.test_contracts.ContractTests.test_immutable_nested_values_and_declared_probability_bounds) ... ok
test_invalid_measurements_are_zero_and_snapshot_masks_match (tests.autonomous.test_contracts.ContractTests.test_invalid_measurements_are_zero_and_snapshot_masks_match) ... ok
test_late_batch_requires_rejection_without_decisions (tests.autonomous.test_contracts.ContractTests.test_late_batch_requires_rejection_without_decisions) ... ok
test_late_model_batch_and_unverified_token_accounting_refuse (tests.autonomous.test_contracts.ContractTests.test_late_model_batch_and_unverified_token_accounting_refuse) ... ok
test_nonnegative_account_magnitudes_and_revisions (tests.autonomous.test_contracts.ContractTests.test_nonnegative_account_magnitudes_and_revisions) ... ok
test_numeric_candidate_mapping_is_fixed_and_exact (tests.autonomous.test_contracts.ContractTests.test_numeric_candidate_mapping_is_fixed_and_exact) ... ok
test_order_reference_and_authorization_are_not_implicit (tests.autonomous.test_contracts.ContractTests.test_order_reference_and_authorization_are_not_implicit) ... ok
test_source_payload_is_typed_hashed_and_immutable (tests.autonomous.test_contracts.ContractTests.test_source_payload_is_typed_hashed_and_immutable) ... ok
test_timestamps_enums_and_nested_records (tests.autonomous.test_contracts.ContractTests.test_timestamps_enums_and_nested_records) ... ok
test_wire_masks_have_bounded_integer_bit_widths (tests.autonomous.test_contracts.ContractTests.test_wire_masks_have_bounded_integer_bit_widths) ... ok
test_canonical_reduced_rational_and_resource_bound (tests.autonomous.test_contracts.ExactNavTests.test_canonical_reduced_rational_and_resource_bound) ... ok
test_decimal_projections_cannot_disagree_with_exact_authority (tests.autonomous.test_contracts.ExactNavTests.test_decimal_projections_cannot_disagree_with_exact_authority) ... ok
test_ready_fixture_carries_exact_revision_mark_and_equity (tests.autonomous.test_contracts.ExactNavTests.test_ready_fixture_carries_exact_revision_mark_and_equity) ... ok
test_ready_view_requires_bound_exact_evidence (tests.autonomous.test_contracts.ExactNavTests.test_ready_view_requires_bound_exact_evidence) ... ok
test_accounting_write_boundary_failures_roll_back_every_projection (tests.autonomous.test_ledger.AccountingTests.test_accounting_write_boundary_failures_roll_back_every_projection) ... ok
test_additive_accounting_migration_preserves_legacy_committed_export (tests.autonomous.test_ledger.AccountingTests.test_additive_accounting_migration_preserves_legacy_committed_export) ... ok
test_buy_net_base_fee_and_sell_extra_base_fee_apply_once (tests.autonomous.test_ledger.AccountingTests.test_buy_net_base_fee_and_sell_extra_base_fee_apply_once) ... ok
test_buy_sell_native_fees_basis_and_reservation_conservation (tests.autonomous.test_ledger.AccountingTests.test_buy_sell_native_fees_basis_and_reservation_conservation) ... ok
test_closure_redeposit_and_reopen_keep_halts_and_exact_history (tests.autonomous.test_ledger.AccountingTests.test_closure_redeposit_and_reopen_keep_halts_and_exact_history) ... ok
test_correction_replays_old_buy_basis_after_partial_sell (tests.autonomous.test_ledger.AccountingTests.test_correction_replays_old_buy_basis_after_partial_sell) ... ok
test_deposit_loss_closure_restart_and_redeposit_preserve_latch (tests.autonomous.test_ledger.AccountingTests.test_deposit_loss_closure_restart_and_redeposit_preserve_latch) ... ok
test_economic_failure_rolls_back_and_prefix_excludes_future_mark (tests.autonomous.test_ledger.AccountingTests.test_economic_failure_rolls_back_and_prefix_excludes_future_mark) ... ok
test_exact_accounting_events_are_cutoff_prefix_bound (tests.autonomous.test_ledger.AccountingTests.test_exact_accounting_events_are_cutoff_prefix_bound) ... ok
test_exact_flow_after_units_are_checked_on_restart (tests.autonomous.test_ledger.AccountingTests.test_exact_flow_after_units_are_checked_on_restart) ... ok
test_fill_after_flow_still_latches_loss_in_economic_transaction (tests.autonomous.test_ledger.AccountingTests.test_fill_after_flow_still_latches_loss_in_economic_transaction) ... ok
test_fill_known_cash_fee_can_latch_before_another_reconciliation (tests.autonomous.test_ledger.AccountingTests.test_fill_known_cash_fee_can_latch_before_another_reconciliation) ... ok
test_flow_with_missing_fx_keeps_evidence_and_inhibits_entries (tests.autonomous.test_ledger.AccountingTests.test_flow_with_missing_fx_keeps_evidence_and_inhibits_entries) ... ok
test_genuine_boundary_restored_after_outage_requires_current_reconciliation (tests.autonomous.test_ledger.AccountingTests.test_genuine_boundary_restored_after_outage_requires_current_reconciliation) ... ok
test_late_fill_changed_retry_and_stale_correction_preserve_history (tests.autonomous.test_ledger.AccountingTests.test_late_fill_changed_retry_and_stale_correction_preserve_history) ... ok
test_later_depth_cannot_be_relabeled_as_midnight (tests.autonomous.test_ledger.AccountingTests.test_later_depth_cannot_be_relabeled_as_midnight) ... ok
test_local_evidence_cannot_claim_live_provenance_or_changed_identity (tests.autonomous.test_ledger.AccountingTests.test_local_evidence_cannot_claim_live_provenance_or_changed_identity) ... ok
test_missing_fx_liquidation_invalidates_previous_ready_mark (tests.autonomous.test_ledger.AccountingTests.test_missing_fx_liquidation_invalidates_previous_ready_mark) ... ok
test_missing_or_stale_depth_unknown_holding_and_boundary_are_unready (tests.autonomous.test_ledger.AccountingTests.test_missing_or_stale_depth_unknown_holding_and_boundary_are_unready) ... ok
test_non_usd_external_capital_requires_fx_and_preserves_nav (tests.autonomous.test_ledger.AccountingTests.test_non_usd_external_capital_requires_fx_and_preserves_nav) ... ok
test_projection_failure_retains_factual_transfer_and_inhibits_ready_view (tests.autonomous.test_ledger.AccountingTests.test_projection_failure_retains_factual_transfer_and_inhibits_ready_view) ... ok
test_repeated_capital_flow_keeps_depth_for_following_loss_event (tests.autonomous.test_ledger.AccountingTests.test_repeated_capital_flow_keeps_depth_for_following_loss_event) ... ok
test_repeating_units_repeated_flows_and_exact_view (tests.autonomous.test_ledger.AccountingTests.test_repeating_units_repeated_flows_and_exact_view) ... ok
test_rounding_and_compensation_export_cutoff_keeps_original_economics (tests.autonomous.test_ledger.AccountingTests.test_rounding_and_compensation_export_cutoff_keeps_original_economics) ... ok
test_stale_depth_heartbeat_and_unexplained_holdings_do_not_refresh_readiness (tests.autonomous.test_ledger.AccountingTests.test_stale_depth_heartbeat_and_unexplained_holdings_do_not_refresh_readiness) ... ok
test_startup_rejects_day_boundary_projection_tampering (tests.autonomous.test_ledger.AccountingTests.test_startup_rejects_day_boundary_projection_tampering) ... ok
test_startup_rejects_highwater_and_native_projection_tampering (tests.autonomous.test_ledger.AccountingTests.test_startup_rejects_highwater_and_native_projection_tampering) ... ok
test_startup_rejects_mutable_unit_projection_tampering (tests.autonomous.test_ledger.AccountingTests.test_startup_rejects_mutable_unit_projection_tampering) ... ok
test_submicro_basis_residual_and_duplicate_fill_evidence (tests.autonomous.test_ledger.AccountingTests.test_submicro_basis_residual_and_duplicate_fill_evidence) ... ok
test_submicro_usd_transfer_cannot_silently_round_native_cash (tests.autonomous.test_ledger.AccountingTests.test_submicro_usd_transfer_cannot_silently_round_native_cash) ... ok
test_third_asset_fee_fx_and_pending_unknown_conversion (tests.autonomous.test_ledger.AccountingTests.test_third_asset_fee_fx_and_pending_unknown_conversion) ... ok
test_transfer_cost_triggers_daily_halt_atomically_and_utc_rollover (tests.autonomous.test_ledger.AccountingTests.test_transfer_cost_triggers_daily_halt_atomically_and_utc_rollover) ... ok
test_unknown_fill_semantics_records_incident_and_invalidates_ready_view (tests.autonomous.test_ledger.AccountingTests.test_unknown_fill_semantics_records_incident_and_invalidates_ready_view) ... ok
test_zero_equity_still_persists_breach_while_unarmed (tests.autonomous.test_ledger.AccountingTests.test_zero_equity_still_persists_breach_while_unarmed) ... ok
test_busy_failure_disables_dispatch_without_partial_write (tests.autonomous.test_ledger.LedgerTests.test_busy_failure_disables_dispatch_without_partial_write) ... ok
test_cash_competition_rechecks_pending_reservations (tests.autonomous.test_ledger.LedgerTests.test_cash_competition_rechecks_pending_reservations) ... ok
test_changed_release_content_fails_startup_integrity (tests.autonomous.test_ledger.LedgerTests.test_changed_release_content_fails_startup_integrity) ... ok
test_changed_venue_event_key_payload_is_rejected (tests.autonomous.test_ledger.LedgerTests.test_changed_venue_event_key_payload_is_rejected) ... ok
test_correction_and_label_keys_are_unique (tests.autonomous.test_ledger.LedgerTests.test_correction_and_label_keys_are_unique) ... ok
test_crash_after_dispatch_keeps_reservation_and_becomes_unknown (tests.autonomous.test_ledger.LedgerTests.test_crash_after_dispatch_keeps_reservation_and_becomes_unknown) ... ok
test_dispatch_assessor_does_not_count_own_reservation_twice (tests.autonomous.test_ledger.LedgerTests.test_dispatch_assessor_does_not_count_own_reservation_twice) ... ok
test_dispatch_marker_commits_and_rechecks_control (tests.autonomous.test_ledger.LedgerTests.test_dispatch_marker_commits_and_rechecks_control) ... ok
test_duplicate_correction_retains_original_and_rejects_changed_key (tests.autonomous.test_ledger.LedgerTests.test_duplicate_correction_retains_original_and_rejects_changed_key) ... ok
test_failed_posting_rolls_back_entire_journal (tests.autonomous.test_ledger.LedgerTests.test_failed_posting_rolls_back_entire_journal) ... ok
test_failed_reservation_rolls_back_intent (tests.autonomous.test_ledger.LedgerTests.test_failed_reservation_rolls_back_intent) ... ok
test_filled_status_requires_complete_cumulative_quantity (tests.autonomous.test_ledger.LedgerTests.test_filled_status_requires_complete_cumulative_quantity) ... ok
test_foreign_keys_and_snapshot_exactly_one_reference (tests.autonomous.test_ledger.LedgerTests.test_foreign_keys_and_snapshot_exactly_one_reference) ... ok
test_forged_assessor_verdict_never_authorizes (tests.autonomous.test_ledger.LedgerTests.test_forged_assessor_verdict_never_authorizes) ... ok
test_invalid_money_and_missing_account_rejected (tests.autonomous.test_ledger.LedgerTests.test_invalid_money_and_missing_account_rejected) ... ok
test_migration_checksum_tampering_refuses_open (tests.autonomous.test_ledger.LedgerTests.test_migration_checksum_tampering_refuses_open) ... ok
test_nested_transaction_failure_cannot_commit_partial_post (tests.autonomous.test_ledger.LedgerTests.test_nested_transaction_failure_cannot_commit_partial_post) ... ok
test_open_never_adopts_or_arms_account (tests.autonomous.test_ledger.LedgerTests.test_open_never_adopts_or_arms_account) ... ok
test_order_events_are_typed_idempotent_and_keep_capacity (tests.autonomous.test_ledger.LedgerTests.test_order_events_are_typed_idempotent_and_keep_capacity) ... ok
test_reopen_preserves_journal_and_disarms (tests.autonomous.test_ledger.LedgerTests.test_reopen_preserves_journal_and_disarms) ... ok
test_reopen_rejects_removed_evidence_protection (tests.autonomous.test_ledger.LedgerTests.test_reopen_rejects_removed_evidence_protection) ... ok
test_reservation_commits_and_cross_release_action_is_unique (tests.autonomous.test_ledger.LedgerTests.test_reservation_commits_and_cross_release_action_is_unique) ... ok
test_reservation_duplicate_is_content_bound (tests.autonomous.test_ledger.LedgerTests.test_reservation_duplicate_is_content_bound) ... ok
test_reservation_rechecks_revision_and_capacity (tests.autonomous.test_ledger.LedgerTests.test_reservation_rechecks_revision_and_capacity) ... ok
test_reservation_unready_fails_closed (tests.autonomous.test_ledger.LedgerTests.test_reservation_unready_fails_closed) ... ok
test_review_job_dedupe_and_missing_reference (tests.autonomous.test_ledger.LedgerTests.test_review_job_dedupe_and_missing_reference) ... ok
test_review_r1_nested_dispatch_rejects_before_mutation (tests.autonomous.test_ledger.LedgerTests.test_review_r1_nested_dispatch_rejects_before_mutation) ... ok
test_review_r1_uncommitted_reservation_cannot_dispatch (tests.autonomous.test_ledger.LedgerTests.test_review_r1_uncommitted_reservation_cannot_dispatch) ... ok
test_review_r2_all_event_receipt_knowledge_and_segment_endpoints_are_causal (tests.autonomous.test_ledger.LedgerTests.test_review_r2_all_event_receipt_knowledge_and_segment_endpoints_are_causal) ... ok
test_review_r2_clock_regression_cutoff_preserves_committed_event_prefix (tests.autonomous.test_ledger.LedgerTests.test_review_r2_clock_regression_cutoff_preserves_committed_event_prefix) ... ok
test_review_r2_delayed_first_export_uses_historical_revision_and_generation (tests.autonomous.test_ledger.LedgerTests.test_review_r2_delayed_first_export_uses_historical_revision_and_generation) ... ok
test_review_r2_event_before_commit_capture_uses_previous_committed_prefix (tests.autonomous.test_ledger.LedgerTests.test_review_r2_event_before_commit_capture_uses_previous_committed_prefix) ... ok
test_review_r2_nested_result_receipt_participates_in_revision_visibility (tests.autonomous.test_ledger.LedgerTests.test_review_r2_nested_result_receipt_participates_in_revision_visibility) ... ok
test_review_r2_non_economic_import_commits_new_prefix_without_revision_change (tests.autonomous.test_ledger.LedgerTests.test_review_r2_non_economic_import_commits_new_prefix_without_revision_change) ... ok
test_review_r2_reconciliation_anchor_requires_selected_committed_prefix (tests.autonomous.test_ledger.LedgerTests.test_review_r2_reconciliation_anchor_requires_selected_committed_prefix) ... ok
test_review_r2_session_account_anchor_requires_creation_prefix (tests.autonomous.test_ledger.LedgerTests.test_review_r2_session_account_anchor_requires_creation_prefix) ... ok
test_review_r3_clock_regression_and_reverse_ids_preserve_order (tests.autonomous.test_ledger.LedgerTests.test_review_r3_clock_regression_and_reverse_ids_preserve_order) ... ok
test_review_r3_same_timestamp_order_replay_uses_durable_append_order (tests.autonomous.test_ledger.LedgerTests.test_review_r3_same_timestamp_order_replay_uses_durable_append_order) ... ok
test_review_r4_duplicate_manifest_record_id_is_rejected (tests.autonomous.test_ledger.LedgerTests.test_review_r4_duplicate_manifest_record_id_is_rejected) ... ok
test_review_r4_manifest_ids_use_real_primary_keys (tests.autonomous.test_ledger.LedgerTests.test_review_r4_manifest_ids_use_real_primary_keys) ... ok
test_review_r5_cumulative_quantity_is_exact_under_low_ambient_precision (tests.autonomous.test_ledger.LedgerTests.test_review_r5_cumulative_quantity_is_exact_under_low_ambient_precision) ... ok
test_review_r5_high_scale_order_ignores_ambient_decimal_precision (tests.autonomous.test_ledger.LedgerTests.test_review_r5_high_scale_order_ignores_ambient_decimal_precision) ... ok
test_schema_settings_and_append_only_protection (tests.autonomous.test_ledger.LedgerTests.test_schema_settings_and_append_only_protection) ... ok
test_session_export_does_not_include_future_source_or_control (tests.autonomous.test_ledger.LedgerTests.test_session_export_does_not_include_future_source_or_control) ... ok
test_session_export_is_immutable_and_idempotent (tests.autonomous.test_ledger.LedgerTests.test_session_export_is_immutable_and_idempotent) ... ok
test_session_order_projection_is_rebuilt_at_cutoff (tests.autonomous.test_ledger.LedgerTests.test_session_order_projection_is_rebuilt_at_cutoff) ... ok
test_startup_rejects_negative_decimal_projection (tests.autonomous.test_ledger.LedgerTests.test_startup_rejects_negative_decimal_projection) ... ok
test_transaction_identity_is_content_bound (tests.autonomous.test_ledger.LedgerTests.test_transaction_identity_is_content_bound) ... ok
test_unbalanced_journal_is_rejected (tests.autonomous.test_ledger.LedgerTests.test_unbalanced_journal_is_rejected) ... ok
test_deposit_cannot_hide_drawdown (tests.autonomous.test_risk_limits.FlowTests.test_deposit_cannot_hide_drawdown) ... ok
test_exact_thresholds_ignore_decimal_context (tests.autonomous.test_risk_limits.FlowTests.test_exact_thresholds_ignore_decimal_context) ... ok
test_external_flow_fixture_and_adjacent_exact_boundaries (tests.autonomous.test_risk_limits.FlowTests.test_external_flow_fixture_and_adjacent_exact_boundaries) ... ok
test_invalid_and_closure_reject_before_division (tests.autonomous.test_risk_limits.FlowTests.test_invalid_and_closure_reject_before_division) ... ok

----------------------------------------------------------------------
Ran 117 tests in 1.761s

OK
```


## Retained review finding fixes — October 17, 2026

Status: **fix implementation complete; fresh independent full Task 3 review pending**. This appendix is implementation evidence, not independent acceptance. Root leads AgenKit directly; the retired orchestrator and canceled Notion work were not invoked.

Source commit: **`e2056db`**, `fix: age valuation sources and recover closed account deposits`. Fix base: `767ac6ec0457a94d18fddc3391bb1c21f67f16e9`; original full Task 3 review base remains `67ad62e2ddf0644fbffcd56aa5957e13e2957384`. Only `bot/autonomous/ledger.py` and `tests/autonomous/test_ledger.py` were changed in the source commit. This report appendix is committed separately to identify that source commit. Existing operator/root changes and untracked files were preserved.

### Root causes and resulting behavior

1. **Current source freshness:** `record_mark` checked held depth and FX against the valuation event, while current views/admission aged only the newly created equity mark. At observation T, mark T+1,900,000us and admission T+2,100,000us, stale held depth/FX could pass view, reserve and committed dispatch. `_valuation_sources_current` now ages retained book, FX, valuation and private observation evidence against current admission time. It follows flow-mark preflow lineage without refreshing observation timestamps. `record_mark`, current `AccountView` (therefore reserve and dispatch risk checks), and open-account flow admission use the same check. Historical valid marks remain immutable history after inputs expire. Normal open-account freshness/revision checks remain in force. The old cash-only Task 2 ready fixture now retains its private-observation timestamp explicitly instead of an empty payload.
2. **Closed recovery:** the common fresh-mark guard rejected delayed redeposit before reaching closed-state issuance, and a zero-unit closed account could not create a replacement mark. Closed recovery now uses the retained exact last positive NAV as its issuance baseline, requires a positive transfer whose retained event evidence is currently within 2,000,000us, and preserves HWM/day baselines and latches. New economics still invalidate readiness; the flow-derived mark is unready. Current complete valuation/reconciliation remains required after reopening. A stale transfer creates a retained review incident; accepting a subsequent fresh transfer does not erase that incident.

### RED → GREEN mapping

All regressions below were added and observed failing on the fix base **before either production fix**. An initial fixture setup attempt used an incomplete positional `candidate_results` insert and errored; it was corrected to explicit columns before the RED observations counted below.

- `test_held_book_and_fx_expire_independently_of_mark_for_readiness`: RED for both book and FX (ready incorrectly true); GREEN after source-current check.
- `test_held_book_and_fx_expiry_refuses_new_reservation`: RED for both book and FX (no refusal); GREEN after source-current check, with no intent written.
- `test_committed_reservation_rechecks_held_book_and_fx_at_dispatch`: RED for both book and FX (no refusal); GREEN after source-current check, preserving committed reserved state.
- `test_open_flow_cannot_refresh_expired_held_valuation_sources`: RED for book and FX (flow incorrectly accepted); GREEN after source-current check.
- `test_delayed_mark_application_cannot_refresh_held_book_or_fx`: RED for book and FX (persisted exact mark ready incorrectly 1); GREEN after source-current check.
- `test_delayed_closed_redeposit_preserves_exact_loss_history_and_requires_new_mark`: RED (fresh delayed redeposit rejected); GREEN after closed recovery guard. Covers a failed zero-unit mark attempt, restart before redeposit, exact preclosure NAV 85, reopening units 1000/17, unchanged exact NAV/HWM/day evidence, unready until a current mark, retained drawdown/daily/crisis/hard-kill latches, unarmed permit and restart reconstruction.
- `test_closed_redeposit_requires_fresh_transfer_not_fresh_preclosure_mark`: RED (subsequent fresh redeposit also rejected); GREEN after closed recovery guard. Stale transfer stays rejected with durable review inhibition after fresh redeposit and mark.
- Positive preservation regression `test_current_accounting_mark_rebinds_only_non_economic_revisions` passed before and after fixes: reserve changes revision while retaining the same exact mark, exact evidence binds current revision, current dispatch succeeds, and a subsequent capital flow invalidates readiness.

First RED command ran the readiness/reserve/dispatch tests plus positive preservation: `Ran 4 tests in 0.107s`, `FAILED (failures=6)`, exit 1. Second RED command ran open-flow/delayed-mark and both recovery tests: `Ran 4 tests in 0.098s`, `FAILED (failures=6)`, exit 1. Focused freshness GREEN: 6 tests in 0.166s, OK. Focused recovery plus existing closure/repeating-flow/depth regression GREEN: 5 tests in 0.451s, OK.

### Final covering execution and limits

Python version: **3.12.14**. After the final production/test edit, executed exactly:

```text
.venv-autonomous/bin/python -m unittest tests.autonomous.test_contracts tests.autonomous.test_ledger tests.autonomous.test_risk_limits -v > /tmp/task03-fix-final-tests.log 2>&1
```

Exit status: **0**. Full captured output is embedded below and retained at `/tmp/task03-fix-final-tests.log`. `git diff --check` and `git diff --cached --check` also exited 0 before the source commit.

The earlier report's Task 11/12/13/14 integration limits remain applicable. These local paper fixtures provide no authenticated live transfer/book/FX proof or live authorization. Recovery on a later UTC day still requires the genuine boundary/current proof required by existing accounting; retaining old exact day history does not synthesize a new boundary. Fresh source checks do not authorize or arm entries. Risk thresholds, public contracts, migrations and production risk policy were not changed. Root will prepare the original-base full diff and assign a fresh independent spec/quality review before Task 4; these two retained findings were not an exhaustive independent review.

### Full fix covering output

```text
test_book_delta_deletions_and_signed_metrics (tests.autonomous.test_contracts.ContractTests.test_book_delta_deletions_and_signed_metrics) ... ok
test_canonical_and_finite (tests.autonomous.test_contracts.ContractTests.test_canonical_and_finite) ... ok
test_canonical_rejects_lossy_keys_and_unsupported_values (tests.autonomous.test_contracts.ContractTests.test_canonical_rejects_lossy_keys_and_unsupported_values) ... ok
test_continuous_wire_slots_obey_dictionary_ranges (tests.autonomous.test_contracts.ContractTests.test_continuous_wire_slots_obey_dictionary_ranges) ... ok
test_decimal_instrument_scale_supports_full_precision (tests.autonomous.test_contracts.ContractTests.test_decimal_instrument_scale_supports_full_precision) ... ok
test_decimal_precision_scale_and_explicit_rounding (tests.autonomous.test_contracts.ContractTests.test_decimal_precision_scale_and_explicit_rounding) ... ok
test_defaults_disable_exposure_and_fix_approved_limits (tests.autonomous.test_contracts.ContractTests.test_defaults_disable_exposure_and_fix_approved_limits) ... ok
test_dictionary_covers_all_compact_slots (tests.autonomous.test_contracts.ContractTests.test_dictionary_covers_all_compact_slots) ... ok
test_dictionary_documents_wire_metadata_and_thesis_bit_width (tests.autonomous.test_contracts.ContractTests.test_dictionary_documents_wire_metadata_and_thesis_bit_width) ... ok
test_direction_distribution_and_toxic_threshold (tests.autonomous.test_contracts.ContractTests.test_direction_distribution_and_toxic_threshold) ... ok
test_discrete_wire_slots_have_exact_integer_domains (tests.autonomous.test_contracts.ContractTests.test_discrete_wire_slots_have_exact_integer_domains) ... ok
test_eligibility_needs_evidence_and_ondo_cannot_activate (tests.autonomous.test_contracts.ContractTests.test_eligibility_needs_evidence_and_ondo_cannot_activate) ... ok
test_encoded_state_checks_complete_numeric_shape (tests.autonomous.test_contracts.ContractTests.test_encoded_state_checks_complete_numeric_shape) ... ok
test_every_architecture_record_is_frozen_and_typed (tests.autonomous.test_contracts.ContractTests.test_every_architecture_record_is_frozen_and_typed) ... ok
test_frozen_strict_roundtrip (tests.autonomous.test_contracts.ContractTests.test_frozen_strict_roundtrip) ... ok
test_hash_type_id_and_unknown_enum_are_rejected_at_wire_boundary (tests.autonomous.test_contracts.ContractTests.test_hash_type_id_and_unknown_enum_are_rejected_at_wire_boundary) ... ok
test_immutable_nested_values_and_declared_probability_bounds (tests.autonomous.test_contracts.ContractTests.test_immutable_nested_values_and_declared_probability_bounds) ... ok
test_invalid_measurements_are_zero_and_snapshot_masks_match (tests.autonomous.test_contracts.ContractTests.test_invalid_measurements_are_zero_and_snapshot_masks_match) ... ok
test_late_batch_requires_rejection_without_decisions (tests.autonomous.test_contracts.ContractTests.test_late_batch_requires_rejection_without_decisions) ... ok
test_late_model_batch_and_unverified_token_accounting_refuse (tests.autonomous.test_contracts.ContractTests.test_late_model_batch_and_unverified_token_accounting_refuse) ... ok
test_nonnegative_account_magnitudes_and_revisions (tests.autonomous.test_contracts.ContractTests.test_nonnegative_account_magnitudes_and_revisions) ... ok
test_numeric_candidate_mapping_is_fixed_and_exact (tests.autonomous.test_contracts.ContractTests.test_numeric_candidate_mapping_is_fixed_and_exact) ... ok
test_order_reference_and_authorization_are_not_implicit (tests.autonomous.test_contracts.ContractTests.test_order_reference_and_authorization_are_not_implicit) ... ok
test_source_payload_is_typed_hashed_and_immutable (tests.autonomous.test_contracts.ContractTests.test_source_payload_is_typed_hashed_and_immutable) ... ok
test_timestamps_enums_and_nested_records (tests.autonomous.test_contracts.ContractTests.test_timestamps_enums_and_nested_records) ... ok
test_wire_masks_have_bounded_integer_bit_widths (tests.autonomous.test_contracts.ContractTests.test_wire_masks_have_bounded_integer_bit_widths) ... ok
test_canonical_reduced_rational_and_resource_bound (tests.autonomous.test_contracts.ExactNavTests.test_canonical_reduced_rational_and_resource_bound) ... ok
test_decimal_projections_cannot_disagree_with_exact_authority (tests.autonomous.test_contracts.ExactNavTests.test_decimal_projections_cannot_disagree_with_exact_authority) ... ok
test_ready_fixture_carries_exact_revision_mark_and_equity (tests.autonomous.test_contracts.ExactNavTests.test_ready_fixture_carries_exact_revision_mark_and_equity) ... ok
test_ready_view_requires_bound_exact_evidence (tests.autonomous.test_contracts.ExactNavTests.test_ready_view_requires_bound_exact_evidence) ... ok
test_accounting_write_boundary_failures_roll_back_every_projection (tests.autonomous.test_ledger.AccountingTests.test_accounting_write_boundary_failures_roll_back_every_projection) ... ok
test_additive_accounting_migration_preserves_legacy_committed_export (tests.autonomous.test_ledger.AccountingTests.test_additive_accounting_migration_preserves_legacy_committed_export) ... ok
test_buy_net_base_fee_and_sell_extra_base_fee_apply_once (tests.autonomous.test_ledger.AccountingTests.test_buy_net_base_fee_and_sell_extra_base_fee_apply_once) ... ok
test_buy_sell_native_fees_basis_and_reservation_conservation (tests.autonomous.test_ledger.AccountingTests.test_buy_sell_native_fees_basis_and_reservation_conservation) ... ok
test_closed_redeposit_requires_fresh_transfer_not_fresh_preclosure_mark (tests.autonomous.test_ledger.AccountingTests.test_closed_redeposit_requires_fresh_transfer_not_fresh_preclosure_mark) ... ok
test_closure_redeposit_and_reopen_keep_halts_and_exact_history (tests.autonomous.test_ledger.AccountingTests.test_closure_redeposit_and_reopen_keep_halts_and_exact_history) ... ok
test_committed_reservation_rechecks_held_book_and_fx_at_dispatch (tests.autonomous.test_ledger.AccountingTests.test_committed_reservation_rechecks_held_book_and_fx_at_dispatch) ... ok
test_correction_replays_old_buy_basis_after_partial_sell (tests.autonomous.test_ledger.AccountingTests.test_correction_replays_old_buy_basis_after_partial_sell) ... ok
test_current_accounting_mark_rebinds_only_non_economic_revisions (tests.autonomous.test_ledger.AccountingTests.test_current_accounting_mark_rebinds_only_non_economic_revisions) ... ok
test_delayed_closed_redeposit_preserves_exact_loss_history_and_requires_new_mark (tests.autonomous.test_ledger.AccountingTests.test_delayed_closed_redeposit_preserves_exact_loss_history_and_requires_new_mark) ... ok
test_delayed_mark_application_cannot_refresh_held_book_or_fx (tests.autonomous.test_ledger.AccountingTests.test_delayed_mark_application_cannot_refresh_held_book_or_fx) ... ok
test_deposit_loss_closure_restart_and_redeposit_preserve_latch (tests.autonomous.test_ledger.AccountingTests.test_deposit_loss_closure_restart_and_redeposit_preserve_latch) ... ok
test_economic_failure_rolls_back_and_prefix_excludes_future_mark (tests.autonomous.test_ledger.AccountingTests.test_economic_failure_rolls_back_and_prefix_excludes_future_mark) ... ok
test_exact_accounting_events_are_cutoff_prefix_bound (tests.autonomous.test_ledger.AccountingTests.test_exact_accounting_events_are_cutoff_prefix_bound) ... ok
test_exact_flow_after_units_are_checked_on_restart (tests.autonomous.test_ledger.AccountingTests.test_exact_flow_after_units_are_checked_on_restart) ... ok
test_fill_after_flow_still_latches_loss_in_economic_transaction (tests.autonomous.test_ledger.AccountingTests.test_fill_after_flow_still_latches_loss_in_economic_transaction) ... ok
test_fill_known_cash_fee_can_latch_before_another_reconciliation (tests.autonomous.test_ledger.AccountingTests.test_fill_known_cash_fee_can_latch_before_another_reconciliation) ... ok
test_flow_with_missing_fx_keeps_evidence_and_inhibits_entries (tests.autonomous.test_ledger.AccountingTests.test_flow_with_missing_fx_keeps_evidence_and_inhibits_entries) ... ok
test_genuine_boundary_restored_after_outage_requires_current_reconciliation (tests.autonomous.test_ledger.AccountingTests.test_genuine_boundary_restored_after_outage_requires_current_reconciliation) ... ok
test_held_book_and_fx_expire_independently_of_mark_for_readiness (tests.autonomous.test_ledger.AccountingTests.test_held_book_and_fx_expire_independently_of_mark_for_readiness) ... ok
test_held_book_and_fx_expiry_refuses_new_reservation (tests.autonomous.test_ledger.AccountingTests.test_held_book_and_fx_expiry_refuses_new_reservation) ... ok
test_late_fill_changed_retry_and_stale_correction_preserve_history (tests.autonomous.test_ledger.AccountingTests.test_late_fill_changed_retry_and_stale_correction_preserve_history) ... ok
test_later_depth_cannot_be_relabeled_as_midnight (tests.autonomous.test_ledger.AccountingTests.test_later_depth_cannot_be_relabeled_as_midnight) ... ok
test_local_evidence_cannot_claim_live_provenance_or_changed_identity (tests.autonomous.test_ledger.AccountingTests.test_local_evidence_cannot_claim_live_provenance_or_changed_identity) ... ok
test_missing_fx_liquidation_invalidates_previous_ready_mark (tests.autonomous.test_ledger.AccountingTests.test_missing_fx_liquidation_invalidates_previous_ready_mark) ... ok
test_missing_or_stale_depth_unknown_holding_and_boundary_are_unready (tests.autonomous.test_ledger.AccountingTests.test_missing_or_stale_depth_unknown_holding_and_boundary_are_unready) ... ok
test_non_usd_external_capital_requires_fx_and_preserves_nav (tests.autonomous.test_ledger.AccountingTests.test_non_usd_external_capital_requires_fx_and_preserves_nav) ... ok
test_open_flow_cannot_refresh_expired_held_valuation_sources (tests.autonomous.test_ledger.AccountingTests.test_open_flow_cannot_refresh_expired_held_valuation_sources) ... ok
test_projection_failure_retains_factual_transfer_and_inhibits_ready_view (tests.autonomous.test_ledger.AccountingTests.test_projection_failure_retains_factual_transfer_and_inhibits_ready_view) ... ok
test_repeated_capital_flow_keeps_depth_for_following_loss_event (tests.autonomous.test_ledger.AccountingTests.test_repeated_capital_flow_keeps_depth_for_following_loss_event) ... ok
test_repeating_units_repeated_flows_and_exact_view (tests.autonomous.test_ledger.AccountingTests.test_repeating_units_repeated_flows_and_exact_view) ... ok
test_rounding_and_compensation_export_cutoff_keeps_original_economics (tests.autonomous.test_ledger.AccountingTests.test_rounding_and_compensation_export_cutoff_keeps_original_economics) ... ok
test_stale_depth_heartbeat_and_unexplained_holdings_do_not_refresh_readiness (tests.autonomous.test_ledger.AccountingTests.test_stale_depth_heartbeat_and_unexplained_holdings_do_not_refresh_readiness) ... ok
test_startup_rejects_day_boundary_projection_tampering (tests.autonomous.test_ledger.AccountingTests.test_startup_rejects_day_boundary_projection_tampering) ... ok
test_startup_rejects_highwater_and_native_projection_tampering (tests.autonomous.test_ledger.AccountingTests.test_startup_rejects_highwater_and_native_projection_tampering) ... ok
test_startup_rejects_mutable_unit_projection_tampering (tests.autonomous.test_ledger.AccountingTests.test_startup_rejects_mutable_unit_projection_tampering) ... ok
test_submicro_basis_residual_and_duplicate_fill_evidence (tests.autonomous.test_ledger.AccountingTests.test_submicro_basis_residual_and_duplicate_fill_evidence) ... ok
test_submicro_usd_transfer_cannot_silently_round_native_cash (tests.autonomous.test_ledger.AccountingTests.test_submicro_usd_transfer_cannot_silently_round_native_cash) ... ok
test_third_asset_fee_fx_and_pending_unknown_conversion (tests.autonomous.test_ledger.AccountingTests.test_third_asset_fee_fx_and_pending_unknown_conversion) ... ok
test_transfer_cost_triggers_daily_halt_atomically_and_utc_rollover (tests.autonomous.test_ledger.AccountingTests.test_transfer_cost_triggers_daily_halt_atomically_and_utc_rollover) ... ok
test_unknown_fill_semantics_records_incident_and_invalidates_ready_view (tests.autonomous.test_ledger.AccountingTests.test_unknown_fill_semantics_records_incident_and_invalidates_ready_view) ... ok
test_zero_equity_still_persists_breach_while_unarmed (tests.autonomous.test_ledger.AccountingTests.test_zero_equity_still_persists_breach_while_unarmed) ... ok
test_busy_failure_disables_dispatch_without_partial_write (tests.autonomous.test_ledger.LedgerTests.test_busy_failure_disables_dispatch_without_partial_write) ... ok
test_cash_competition_rechecks_pending_reservations (tests.autonomous.test_ledger.LedgerTests.test_cash_competition_rechecks_pending_reservations) ... ok
test_changed_release_content_fails_startup_integrity (tests.autonomous.test_ledger.LedgerTests.test_changed_release_content_fails_startup_integrity) ... ok
test_changed_venue_event_key_payload_is_rejected (tests.autonomous.test_ledger.LedgerTests.test_changed_venue_event_key_payload_is_rejected) ... ok
test_correction_and_label_keys_are_unique (tests.autonomous.test_ledger.LedgerTests.test_correction_and_label_keys_are_unique) ... ok
test_crash_after_dispatch_keeps_reservation_and_becomes_unknown (tests.autonomous.test_ledger.LedgerTests.test_crash_after_dispatch_keeps_reservation_and_becomes_unknown) ... ok
test_dispatch_assessor_does_not_count_own_reservation_twice (tests.autonomous.test_ledger.LedgerTests.test_dispatch_assessor_does_not_count_own_reservation_twice) ... ok
test_dispatch_marker_commits_and_rechecks_control (tests.autonomous.test_ledger.LedgerTests.test_dispatch_marker_commits_and_rechecks_control) ... ok
test_duplicate_correction_retains_original_and_rejects_changed_key (tests.autonomous.test_ledger.LedgerTests.test_duplicate_correction_retains_original_and_rejects_changed_key) ... ok
test_failed_posting_rolls_back_entire_journal (tests.autonomous.test_ledger.LedgerTests.test_failed_posting_rolls_back_entire_journal) ... ok
test_failed_reservation_rolls_back_intent (tests.autonomous.test_ledger.LedgerTests.test_failed_reservation_rolls_back_intent) ... ok
test_filled_status_requires_complete_cumulative_quantity (tests.autonomous.test_ledger.LedgerTests.test_filled_status_requires_complete_cumulative_quantity) ... ok
test_foreign_keys_and_snapshot_exactly_one_reference (tests.autonomous.test_ledger.LedgerTests.test_foreign_keys_and_snapshot_exactly_one_reference) ... ok
test_forged_assessor_verdict_never_authorizes (tests.autonomous.test_ledger.LedgerTests.test_forged_assessor_verdict_never_authorizes) ... ok
test_invalid_money_and_missing_account_rejected (tests.autonomous.test_ledger.LedgerTests.test_invalid_money_and_missing_account_rejected) ... ok
test_migration_checksum_tampering_refuses_open (tests.autonomous.test_ledger.LedgerTests.test_migration_checksum_tampering_refuses_open) ... ok
test_nested_transaction_failure_cannot_commit_partial_post (tests.autonomous.test_ledger.LedgerTests.test_nested_transaction_failure_cannot_commit_partial_post) ... ok
test_open_never_adopts_or_arms_account (tests.autonomous.test_ledger.LedgerTests.test_open_never_adopts_or_arms_account) ... ok
test_order_events_are_typed_idempotent_and_keep_capacity (tests.autonomous.test_ledger.LedgerTests.test_order_events_are_typed_idempotent_and_keep_capacity) ... ok
test_reopen_preserves_journal_and_disarms (tests.autonomous.test_ledger.LedgerTests.test_reopen_preserves_journal_and_disarms) ... ok
test_reopen_rejects_removed_evidence_protection (tests.autonomous.test_ledger.LedgerTests.test_reopen_rejects_removed_evidence_protection) ... ok
test_reservation_commits_and_cross_release_action_is_unique (tests.autonomous.test_ledger.LedgerTests.test_reservation_commits_and_cross_release_action_is_unique) ... ok
test_reservation_duplicate_is_content_bound (tests.autonomous.test_ledger.LedgerTests.test_reservation_duplicate_is_content_bound) ... ok
test_reservation_rechecks_revision_and_capacity (tests.autonomous.test_ledger.LedgerTests.test_reservation_rechecks_revision_and_capacity) ... ok
test_reservation_unready_fails_closed (tests.autonomous.test_ledger.LedgerTests.test_reservation_unready_fails_closed) ... ok
test_review_job_dedupe_and_missing_reference (tests.autonomous.test_ledger.LedgerTests.test_review_job_dedupe_and_missing_reference) ... ok
test_review_r1_nested_dispatch_rejects_before_mutation (tests.autonomous.test_ledger.LedgerTests.test_review_r1_nested_dispatch_rejects_before_mutation) ... ok
test_review_r1_uncommitted_reservation_cannot_dispatch (tests.autonomous.test_ledger.LedgerTests.test_review_r1_uncommitted_reservation_cannot_dispatch) ... ok
test_review_r2_all_event_receipt_knowledge_and_segment_endpoints_are_causal (tests.autonomous.test_ledger.LedgerTests.test_review_r2_all_event_receipt_knowledge_and_segment_endpoints_are_causal) ... ok
test_review_r2_clock_regression_cutoff_preserves_committed_event_prefix (tests.autonomous.test_ledger.LedgerTests.test_review_r2_clock_regression_cutoff_preserves_committed_event_prefix) ... ok
test_review_r2_delayed_first_export_uses_historical_revision_and_generation (tests.autonomous.test_ledger.LedgerTests.test_review_r2_delayed_first_export_uses_historical_revision_and_generation) ... ok
test_review_r2_event_before_commit_capture_uses_previous_committed_prefix (tests.autonomous.test_ledger.LedgerTests.test_review_r2_event_before_commit_capture_uses_previous_committed_prefix) ... ok
test_review_r2_nested_result_receipt_participates_in_revision_visibility (tests.autonomous.test_ledger.LedgerTests.test_review_r2_nested_result_receipt_participates_in_revision_visibility) ... ok
test_review_r2_non_economic_import_commits_new_prefix_without_revision_change (tests.autonomous.test_ledger.LedgerTests.test_review_r2_non_economic_import_commits_new_prefix_without_revision_change) ... ok
test_review_r2_reconciliation_anchor_requires_selected_committed_prefix (tests.autonomous.test_ledger.LedgerTests.test_review_r2_reconciliation_anchor_requires_selected_committed_prefix) ... ok
test_review_r2_session_account_anchor_requires_creation_prefix (tests.autonomous.test_ledger.LedgerTests.test_review_r2_session_account_anchor_requires_creation_prefix) ... ok
test_review_r3_clock_regression_and_reverse_ids_preserve_order (tests.autonomous.test_ledger.LedgerTests.test_review_r3_clock_regression_and_reverse_ids_preserve_order) ... ok
test_review_r3_same_timestamp_order_replay_uses_durable_append_order (tests.autonomous.test_ledger.LedgerTests.test_review_r3_same_timestamp_order_replay_uses_durable_append_order) ... ok
test_review_r4_duplicate_manifest_record_id_is_rejected (tests.autonomous.test_ledger.LedgerTests.test_review_r4_duplicate_manifest_record_id_is_rejected) ... ok
test_review_r4_manifest_ids_use_real_primary_keys (tests.autonomous.test_ledger.LedgerTests.test_review_r4_manifest_ids_use_real_primary_keys) ... ok
test_review_r5_cumulative_quantity_is_exact_under_low_ambient_precision (tests.autonomous.test_ledger.LedgerTests.test_review_r5_cumulative_quantity_is_exact_under_low_ambient_precision) ... ok
test_review_r5_high_scale_order_ignores_ambient_decimal_precision (tests.autonomous.test_ledger.LedgerTests.test_review_r5_high_scale_order_ignores_ambient_decimal_precision) ... ok
test_schema_settings_and_append_only_protection (tests.autonomous.test_ledger.LedgerTests.test_schema_settings_and_append_only_protection) ... ok
test_session_export_does_not_include_future_source_or_control (tests.autonomous.test_ledger.LedgerTests.test_session_export_does_not_include_future_source_or_control) ... ok
test_session_export_is_immutable_and_idempotent (tests.autonomous.test_ledger.LedgerTests.test_session_export_is_immutable_and_idempotent) ... ok
test_session_order_projection_is_rebuilt_at_cutoff (tests.autonomous.test_ledger.LedgerTests.test_session_order_projection_is_rebuilt_at_cutoff) ... ok
test_startup_rejects_negative_decimal_projection (tests.autonomous.test_ledger.LedgerTests.test_startup_rejects_negative_decimal_projection) ... ok
test_transaction_identity_is_content_bound (tests.autonomous.test_ledger.LedgerTests.test_transaction_identity_is_content_bound) ... ok
test_unbalanced_journal_is_rejected (tests.autonomous.test_ledger.LedgerTests.test_unbalanced_journal_is_rejected) ... ok
test_deposit_cannot_hide_drawdown (tests.autonomous.test_risk_limits.FlowTests.test_deposit_cannot_hide_drawdown) ... ok
test_exact_thresholds_ignore_decimal_context (tests.autonomous.test_risk_limits.FlowTests.test_exact_thresholds_ignore_decimal_context) ... ok
test_external_flow_fixture_and_adjacent_exact_boundaries (tests.autonomous.test_risk_limits.FlowTests.test_external_flow_fixture_and_adjacent_exact_boundaries) ... ok
test_invalid_and_closure_reject_before_division (tests.autonomous.test_risk_limits.FlowTests.test_invalid_and_closure_reject_before_division) ... ok

----------------------------------------------------------------------
Ran 125 tests in 1.960s

OK
```


## October 17 resumed review fixes: R1–R3

Source/test commit: **`07ecc179d816b30a480e51bef85c82b45090b224`** (`fix: preserve replay and native accounting invariants`). Changes remain limited to ledger.py and test_ledger.py. This appendix records resumed implementation verification; independent review remains pending.

Root causes and corrections:

- R1: full-history economic replay incorrectly treated unchanged terminal-order totals as newly admitted fills. Compare prior and new exact cumulative quantities before reservation updates or late-fill findings. Two regressions complete/reconcile an order, admit an unrelated deposit or another intent's fill, preserve the old reservation byte-for-byte, and restore readiness with a complete current mark. A separate genuine terminal-fill/cumulative-overfill guard proves one durable incident, one journal application, and inhibited readiness on retry.
- R2: an authoritative native correction delta was serialized with the bounded floor display projection. Split exact differences into bounded integer/fractional native legs; verify each leg round-trips and compare the actual serialized per-account/per-asset posting aggregate with exact replay before commit. The 10-to-1e-38 correction succeeds, retains exactly 1e-38 inventory, conserves the whole native journal, and reopens with identical balances and its review incident intact.
- R3: admission accepted bounded Decimal text but persisted noncanonical source forms that startup rejected. Normalize fill base/quote/price/fee and flow quantity columns using the same decimal_text(decimal_value(...)) rule startup enforces. Preserve original immutable payload/hash and retry identity. Regressions cover trailing zeros, same-evidence and same-payload/new-evidence retries, unchanged economics, successful reopening and fresh reconciliation.

TDD evidence is reconstructed honestly: these source/test edits already existed when this resume began, so this appendix does not claim their original edit order was observed. Original committed ledger source from aa18c18 was copied to /tmp/task03-red-ledger.py and executed only in a separate Python process's ledger module namespace, keeping its real __file__ for migrations. Shared production source was never overwritten. Running the six new regressions against that source produced five expected assertion failures (both false late-fill scenarios, native-journal mismatch, and both noncanonical-storage cases); the genuine-late-fill guard already passed and protects retained behavior. Reproduced on Python **3.12.14**, with zero errors.

Reproduction command used to capture the following RED output:

```text
PYTHONPATH=. .venv-autonomous/bin/python /tmp/task03-record-evidence.py
```

The temporary runner obtains `git show aa18c18:bot/autonomous/ledger.py`, compiles it in the imported ledger module, and loads the six AccountingTests named below. Its own exit 0 confirms the expected 6-test/5-failure/0-error shape, not a passing original implementation.

```text
test_terminal_fill_replay_does_not_change_reservation_on_unrelated_deposit (tests.autonomous.test_ledger.AccountingTests.test_terminal_fill_replay_does_not_change_reservation_on_unrelated_deposit) ... FAIL
test_terminal_fill_replay_does_not_change_reservation_on_another_intent_fill (tests.autonomous.test_ledger.AccountingTests.test_terminal_fill_replay_does_not_change_reservation_on_another_intent_fill) ... FAIL
test_new_terminal_fill_and_open_cumulative_overfill_journal_once (tests.autonomous.test_ledger.AccountingTests.test_new_terminal_fill_and_open_cumulative_overfill_journal_once) ... ok
test_wide_native_correction_preserves_exact_posting_targets_and_reopens (tests.autonomous.test_ledger.AccountingTests.test_wide_native_correction_preserves_exact_posting_targets_and_reopens) ... FAIL
test_trailing_zero_flow_is_canonical_idempotent_and_restartable (tests.autonomous.test_ledger.AccountingTests.test_trailing_zero_flow_is_canonical_idempotent_and_restartable) ... FAIL
test_trailing_zero_fill_fields_are_canonical_idempotent_and_restartable (tests.autonomous.test_ledger.AccountingTests.test_trailing_zero_fill_fields_are_canonical_idempotent_and_restartable) ... FAIL

======================================================================
FAIL: test_terminal_fill_replay_does_not_change_reservation_on_unrelated_deposit (tests.autonomous.test_ledger.AccountingTests.test_terminal_fill_replay_does_not_change_reservation_on_unrelated_deposit)
----------------------------------------------------------------------
Traceback (most recent call last):
  File "/home/reckless/crypto-trading-platform/tests/autonomous/test_ledger.py", line 1143, in test_terminal_fill_replay_does_not_change_reservation_on_unrelated_deposit
    self.assertEqual([],list(self.ledger.connection.execute(
AssertionError: Lists differ: [] != [<sqlite3.Row object at 0x73857bfebf70>]

Second list contains 1 additional elements.
First extra element 0:
<sqlite3.Row object at 0x73857bfebf70>

- []
+ [<sqlite3.Row object at 0x73857bfebf70>]

======================================================================
FAIL: test_terminal_fill_replay_does_not_change_reservation_on_another_intent_fill (tests.autonomous.test_ledger.AccountingTests.test_terminal_fill_replay_does_not_change_reservation_on_another_intent_fill)
----------------------------------------------------------------------
Traceback (most recent call last):
  File "/home/reckless/crypto-trading-platform/tests/autonomous/test_ledger.py", line 1161, in test_terminal_fill_replay_does_not_change_reservation_on_another_intent_fill
    self.assertEqual([],list(self.ledger.connection.execute(
AssertionError: Lists differ: [] != [<sqlite3.Row object at 0x73857c5fc5b0>]

Second list contains 1 additional elements.
First extra element 0:
<sqlite3.Row object at 0x73857c5fc5b0>

- []
+ [<sqlite3.Row object at 0x73857c5fc5b0>]

======================================================================
FAIL: test_wide_native_correction_preserves_exact_posting_targets_and_reopens (tests.autonomous.test_ledger.AccountingTests.test_wide_native_correction_preserves_exact_posting_targets_and_reopens)
----------------------------------------------------------------------
Traceback (most recent call last):
  File "/home/reckless/crypto-trading-platform/tests/autonomous/test_ledger.py", line 1206, in test_wide_native_correction_preserves_exact_posting_targets_and_reopens
    self.assertEqual({k:v for k,v in target.items() if v},{k:v for k,v in actual.items() if v})
AssertionError: {('cash', 'USD'): Fraction(9999999999, 1000000[244 chars]000)} != {('capital', 'USD'): Fraction(-10000, 1), ('ca[161 chars]000)}
  {('capital', 'USD'): Fraction(-10000, 1),
   ('cash', 'USD'): Fraction(9999999999, 1000000),
-  ('inventory:AAVE', 'AAVE'): Fraction(1, 100000000000000000000000000000000000000),
-  ('settlement', 'AAVE'): Fraction(-1, 100000000000000000000000000000000000000),
?                                        -

+  ('settlement', 'AAVE'): Fraction(-1, 10000000000000000000000000000000000000),
   ('settlement', 'USD'): Fraction(1, 1000000)}

======================================================================
FAIL: test_trailing_zero_flow_is_canonical_idempotent_and_restartable (tests.autonomous.test_ledger.AccountingTests.test_trailing_zero_flow_is_canonical_idempotent_and_restartable)
----------------------------------------------------------------------
Traceback (most recent call last):
  File "/home/reckless/crypto-trading-platform/tests/autonomous/test_ledger.py", line 1222, in test_trailing_zero_flow_is_canonical_idempotent_and_restartable
    self.assertEqual('1',self.ledger.connection.execute('SELECT native_qty FROM external_flows').fetchone()[0])
AssertionError: '1' != '1.0'
- 1
+ 1.0


======================================================================
FAIL: test_trailing_zero_fill_fields_are_canonical_idempotent_and_restartable (tests.autonomous.test_ledger.AccountingTests.test_trailing_zero_fill_fields_are_canonical_idempotent_and_restartable)
----------------------------------------------------------------------
Traceback (most recent call last):
  File "/home/reckless/crypto-trading-platform/tests/autonomous/test_ledger.py", line 1246, in test_trailing_zero_fill_fields_are_canonical_idempotent_and_restartable
    self.assertEqual(('1','100','100','0.01'),tuple(row)[:4])
AssertionError: Tuples differ: ('1', '100', '100', '0.01') != ('1.0', '100.00', '100.000', '0.0100')

First differing element 0:
'1'
'1.0'

- ('1', '100', '100', '0.01')
+ ('1.0', '100.00', '100.000', '0.0100')
?    ++       +++       ++++        ++


----------------------------------------------------------------------
Ran 6 tests in 0.100s

FAILED (failures=5)

```

The first resumed covering run found 2 failures among 131 tests: both new formatting tests incorrectly expected immediate readiness after reopen. Startup intentionally clears readiness and heartbeat. Only those tests were adjusted to assert unready after reopen, then require a fresh complete mark before ready=True. Production behavior was preserved.

Final covering command, run after the final source/test edit (captured through subprocess with stdout/stderr combined; exit **0**):

```text
.venv-autonomous/bin/python -m unittest tests.autonomous.test_contracts tests.autonomous.test_ledger tests.autonomous.test_risk_limits -v
```

Exact final output:

```text
test_book_delta_deletions_and_signed_metrics (tests.autonomous.test_contracts.ContractTests.test_book_delta_deletions_and_signed_metrics) ... ok
test_canonical_and_finite (tests.autonomous.test_contracts.ContractTests.test_canonical_and_finite) ... ok
test_canonical_rejects_lossy_keys_and_unsupported_values (tests.autonomous.test_contracts.ContractTests.test_canonical_rejects_lossy_keys_and_unsupported_values) ... ok
test_continuous_wire_slots_obey_dictionary_ranges (tests.autonomous.test_contracts.ContractTests.test_continuous_wire_slots_obey_dictionary_ranges) ... ok
test_decimal_instrument_scale_supports_full_precision (tests.autonomous.test_contracts.ContractTests.test_decimal_instrument_scale_supports_full_precision) ... ok
test_decimal_precision_scale_and_explicit_rounding (tests.autonomous.test_contracts.ContractTests.test_decimal_precision_scale_and_explicit_rounding) ... ok
test_defaults_disable_exposure_and_fix_approved_limits (tests.autonomous.test_contracts.ContractTests.test_defaults_disable_exposure_and_fix_approved_limits) ... ok
test_dictionary_covers_all_compact_slots (tests.autonomous.test_contracts.ContractTests.test_dictionary_covers_all_compact_slots) ... ok
test_dictionary_documents_wire_metadata_and_thesis_bit_width (tests.autonomous.test_contracts.ContractTests.test_dictionary_documents_wire_metadata_and_thesis_bit_width) ... ok
test_direction_distribution_and_toxic_threshold (tests.autonomous.test_contracts.ContractTests.test_direction_distribution_and_toxic_threshold) ... ok
test_discrete_wire_slots_have_exact_integer_domains (tests.autonomous.test_contracts.ContractTests.test_discrete_wire_slots_have_exact_integer_domains) ... ok
test_eligibility_needs_evidence_and_ondo_cannot_activate (tests.autonomous.test_contracts.ContractTests.test_eligibility_needs_evidence_and_ondo_cannot_activate) ... ok
test_encoded_state_checks_complete_numeric_shape (tests.autonomous.test_contracts.ContractTests.test_encoded_state_checks_complete_numeric_shape) ... ok
test_every_architecture_record_is_frozen_and_typed (tests.autonomous.test_contracts.ContractTests.test_every_architecture_record_is_frozen_and_typed) ... ok
test_frozen_strict_roundtrip (tests.autonomous.test_contracts.ContractTests.test_frozen_strict_roundtrip) ... ok
test_hash_type_id_and_unknown_enum_are_rejected_at_wire_boundary (tests.autonomous.test_contracts.ContractTests.test_hash_type_id_and_unknown_enum_are_rejected_at_wire_boundary) ... ok
test_immutable_nested_values_and_declared_probability_bounds (tests.autonomous.test_contracts.ContractTests.test_immutable_nested_values_and_declared_probability_bounds) ... ok
test_invalid_measurements_are_zero_and_snapshot_masks_match (tests.autonomous.test_contracts.ContractTests.test_invalid_measurements_are_zero_and_snapshot_masks_match) ... ok
test_late_batch_requires_rejection_without_decisions (tests.autonomous.test_contracts.ContractTests.test_late_batch_requires_rejection_without_decisions) ... ok
test_late_model_batch_and_unverified_token_accounting_refuse (tests.autonomous.test_contracts.ContractTests.test_late_model_batch_and_unverified_token_accounting_refuse) ... ok
test_nonnegative_account_magnitudes_and_revisions (tests.autonomous.test_contracts.ContractTests.test_nonnegative_account_magnitudes_and_revisions) ... ok
test_numeric_candidate_mapping_is_fixed_and_exact (tests.autonomous.test_contracts.ContractTests.test_numeric_candidate_mapping_is_fixed_and_exact) ... ok
test_order_reference_and_authorization_are_not_implicit (tests.autonomous.test_contracts.ContractTests.test_order_reference_and_authorization_are_not_implicit) ... ok
test_source_payload_is_typed_hashed_and_immutable (tests.autonomous.test_contracts.ContractTests.test_source_payload_is_typed_hashed_and_immutable) ... ok
test_timestamps_enums_and_nested_records (tests.autonomous.test_contracts.ContractTests.test_timestamps_enums_and_nested_records) ... ok
test_wire_masks_have_bounded_integer_bit_widths (tests.autonomous.test_contracts.ContractTests.test_wire_masks_have_bounded_integer_bit_widths) ... ok
test_canonical_reduced_rational_and_resource_bound (tests.autonomous.test_contracts.ExactNavTests.test_canonical_reduced_rational_and_resource_bound) ... ok
test_decimal_projections_cannot_disagree_with_exact_authority (tests.autonomous.test_contracts.ExactNavTests.test_decimal_projections_cannot_disagree_with_exact_authority) ... ok
test_ready_fixture_carries_exact_revision_mark_and_equity (tests.autonomous.test_contracts.ExactNavTests.test_ready_fixture_carries_exact_revision_mark_and_equity) ... ok
test_ready_view_requires_bound_exact_evidence (tests.autonomous.test_contracts.ExactNavTests.test_ready_view_requires_bound_exact_evidence) ... ok
test_accounting_write_boundary_failures_roll_back_every_projection (tests.autonomous.test_ledger.AccountingTests.test_accounting_write_boundary_failures_roll_back_every_projection) ... ok
test_additive_accounting_migration_preserves_legacy_committed_export (tests.autonomous.test_ledger.AccountingTests.test_additive_accounting_migration_preserves_legacy_committed_export) ... ok
test_buy_net_base_fee_and_sell_extra_base_fee_apply_once (tests.autonomous.test_ledger.AccountingTests.test_buy_net_base_fee_and_sell_extra_base_fee_apply_once) ... ok
test_buy_sell_native_fees_basis_and_reservation_conservation (tests.autonomous.test_ledger.AccountingTests.test_buy_sell_native_fees_basis_and_reservation_conservation) ... ok
test_closed_redeposit_requires_fresh_transfer_not_fresh_preclosure_mark (tests.autonomous.test_ledger.AccountingTests.test_closed_redeposit_requires_fresh_transfer_not_fresh_preclosure_mark) ... ok
test_closure_redeposit_and_reopen_keep_halts_and_exact_history (tests.autonomous.test_ledger.AccountingTests.test_closure_redeposit_and_reopen_keep_halts_and_exact_history) ... ok
test_committed_reservation_rechecks_held_book_and_fx_at_dispatch (tests.autonomous.test_ledger.AccountingTests.test_committed_reservation_rechecks_held_book_and_fx_at_dispatch) ... ok
test_correction_replays_old_buy_basis_after_partial_sell (tests.autonomous.test_ledger.AccountingTests.test_correction_replays_old_buy_basis_after_partial_sell) ... ok
test_current_accounting_mark_rebinds_only_non_economic_revisions (tests.autonomous.test_ledger.AccountingTests.test_current_accounting_mark_rebinds_only_non_economic_revisions) ... ok
test_delayed_closed_redeposit_preserves_exact_loss_history_and_requires_new_mark (tests.autonomous.test_ledger.AccountingTests.test_delayed_closed_redeposit_preserves_exact_loss_history_and_requires_new_mark) ... ok
test_delayed_mark_application_cannot_refresh_held_book_or_fx (tests.autonomous.test_ledger.AccountingTests.test_delayed_mark_application_cannot_refresh_held_book_or_fx) ... ok
test_deposit_loss_closure_restart_and_redeposit_preserve_latch (tests.autonomous.test_ledger.AccountingTests.test_deposit_loss_closure_restart_and_redeposit_preserve_latch) ... ok
test_economic_failure_rolls_back_and_prefix_excludes_future_mark (tests.autonomous.test_ledger.AccountingTests.test_economic_failure_rolls_back_and_prefix_excludes_future_mark) ... ok
test_exact_accounting_events_are_cutoff_prefix_bound (tests.autonomous.test_ledger.AccountingTests.test_exact_accounting_events_are_cutoff_prefix_bound) ... ok
test_exact_flow_after_units_are_checked_on_restart (tests.autonomous.test_ledger.AccountingTests.test_exact_flow_after_units_are_checked_on_restart) ... ok
test_fill_after_flow_still_latches_loss_in_economic_transaction (tests.autonomous.test_ledger.AccountingTests.test_fill_after_flow_still_latches_loss_in_economic_transaction) ... ok
test_fill_known_cash_fee_can_latch_before_another_reconciliation (tests.autonomous.test_ledger.AccountingTests.test_fill_known_cash_fee_can_latch_before_another_reconciliation) ... ok
test_flow_with_missing_fx_keeps_evidence_and_inhibits_entries (tests.autonomous.test_ledger.AccountingTests.test_flow_with_missing_fx_keeps_evidence_and_inhibits_entries) ... ok
test_genuine_boundary_restored_after_outage_requires_current_reconciliation (tests.autonomous.test_ledger.AccountingTests.test_genuine_boundary_restored_after_outage_requires_current_reconciliation) ... ok
test_held_book_and_fx_expire_independently_of_mark_for_readiness (tests.autonomous.test_ledger.AccountingTests.test_held_book_and_fx_expire_independently_of_mark_for_readiness) ... ok
test_held_book_and_fx_expiry_refuses_new_reservation (tests.autonomous.test_ledger.AccountingTests.test_held_book_and_fx_expiry_refuses_new_reservation) ... ok
test_late_fill_changed_retry_and_stale_correction_preserve_history (tests.autonomous.test_ledger.AccountingTests.test_late_fill_changed_retry_and_stale_correction_preserve_history) ... ok
test_later_depth_cannot_be_relabeled_as_midnight (tests.autonomous.test_ledger.AccountingTests.test_later_depth_cannot_be_relabeled_as_midnight) ... ok
test_local_evidence_cannot_claim_live_provenance_or_changed_identity (tests.autonomous.test_ledger.AccountingTests.test_local_evidence_cannot_claim_live_provenance_or_changed_identity) ... ok
test_missing_fx_liquidation_invalidates_previous_ready_mark (tests.autonomous.test_ledger.AccountingTests.test_missing_fx_liquidation_invalidates_previous_ready_mark) ... ok
test_missing_or_stale_depth_unknown_holding_and_boundary_are_unready (tests.autonomous.test_ledger.AccountingTests.test_missing_or_stale_depth_unknown_holding_and_boundary_are_unready) ... ok
test_new_terminal_fill_and_open_cumulative_overfill_journal_once (tests.autonomous.test_ledger.AccountingTests.test_new_terminal_fill_and_open_cumulative_overfill_journal_once) ... ok
test_non_usd_external_capital_requires_fx_and_preserves_nav (tests.autonomous.test_ledger.AccountingTests.test_non_usd_external_capital_requires_fx_and_preserves_nav) ... ok
test_open_flow_cannot_refresh_expired_held_valuation_sources (tests.autonomous.test_ledger.AccountingTests.test_open_flow_cannot_refresh_expired_held_valuation_sources) ... ok
test_projection_failure_retains_factual_transfer_and_inhibits_ready_view (tests.autonomous.test_ledger.AccountingTests.test_projection_failure_retains_factual_transfer_and_inhibits_ready_view) ... ok
test_repeated_capital_flow_keeps_depth_for_following_loss_event (tests.autonomous.test_ledger.AccountingTests.test_repeated_capital_flow_keeps_depth_for_following_loss_event) ... ok
test_repeating_units_repeated_flows_and_exact_view (tests.autonomous.test_ledger.AccountingTests.test_repeating_units_repeated_flows_and_exact_view) ... ok
test_rounding_and_compensation_export_cutoff_keeps_original_economics (tests.autonomous.test_ledger.AccountingTests.test_rounding_and_compensation_export_cutoff_keeps_original_economics) ... ok
test_stale_depth_heartbeat_and_unexplained_holdings_do_not_refresh_readiness (tests.autonomous.test_ledger.AccountingTests.test_stale_depth_heartbeat_and_unexplained_holdings_do_not_refresh_readiness) ... ok
test_startup_rejects_day_boundary_projection_tampering (tests.autonomous.test_ledger.AccountingTests.test_startup_rejects_day_boundary_projection_tampering) ... ok
test_startup_rejects_highwater_and_native_projection_tampering (tests.autonomous.test_ledger.AccountingTests.test_startup_rejects_highwater_and_native_projection_tampering) ... ok
test_startup_rejects_mutable_unit_projection_tampering (tests.autonomous.test_ledger.AccountingTests.test_startup_rejects_mutable_unit_projection_tampering) ... ok
test_submicro_basis_residual_and_duplicate_fill_evidence (tests.autonomous.test_ledger.AccountingTests.test_submicro_basis_residual_and_duplicate_fill_evidence) ... ok
test_submicro_usd_transfer_cannot_silently_round_native_cash (tests.autonomous.test_ledger.AccountingTests.test_submicro_usd_transfer_cannot_silently_round_native_cash) ... ok
test_terminal_fill_replay_does_not_change_reservation_on_another_intent_fill (tests.autonomous.test_ledger.AccountingTests.test_terminal_fill_replay_does_not_change_reservation_on_another_intent_fill) ... ok
test_terminal_fill_replay_does_not_change_reservation_on_unrelated_deposit (tests.autonomous.test_ledger.AccountingTests.test_terminal_fill_replay_does_not_change_reservation_on_unrelated_deposit) ... ok
test_third_asset_fee_fx_and_pending_unknown_conversion (tests.autonomous.test_ledger.AccountingTests.test_third_asset_fee_fx_and_pending_unknown_conversion) ... ok
test_trailing_zero_fill_fields_are_canonical_idempotent_and_restartable (tests.autonomous.test_ledger.AccountingTests.test_trailing_zero_fill_fields_are_canonical_idempotent_and_restartable) ... ok
test_trailing_zero_flow_is_canonical_idempotent_and_restartable (tests.autonomous.test_ledger.AccountingTests.test_trailing_zero_flow_is_canonical_idempotent_and_restartable) ... ok
test_transfer_cost_triggers_daily_halt_atomically_and_utc_rollover (tests.autonomous.test_ledger.AccountingTests.test_transfer_cost_triggers_daily_halt_atomically_and_utc_rollover) ... ok
test_unknown_fill_semantics_records_incident_and_invalidates_ready_view (tests.autonomous.test_ledger.AccountingTests.test_unknown_fill_semantics_records_incident_and_invalidates_ready_view) ... ok
test_wide_native_correction_preserves_exact_posting_targets_and_reopens (tests.autonomous.test_ledger.AccountingTests.test_wide_native_correction_preserves_exact_posting_targets_and_reopens) ... ok
test_zero_equity_still_persists_breach_while_unarmed (tests.autonomous.test_ledger.AccountingTests.test_zero_equity_still_persists_breach_while_unarmed) ... ok
test_busy_failure_disables_dispatch_without_partial_write (tests.autonomous.test_ledger.LedgerTests.test_busy_failure_disables_dispatch_without_partial_write) ... ok
test_cash_competition_rechecks_pending_reservations (tests.autonomous.test_ledger.LedgerTests.test_cash_competition_rechecks_pending_reservations) ... ok
test_changed_release_content_fails_startup_integrity (tests.autonomous.test_ledger.LedgerTests.test_changed_release_content_fails_startup_integrity) ... ok
test_changed_venue_event_key_payload_is_rejected (tests.autonomous.test_ledger.LedgerTests.test_changed_venue_event_key_payload_is_rejected) ... ok
test_correction_and_label_keys_are_unique (tests.autonomous.test_ledger.LedgerTests.test_correction_and_label_keys_are_unique) ... ok
test_crash_after_dispatch_keeps_reservation_and_becomes_unknown (tests.autonomous.test_ledger.LedgerTests.test_crash_after_dispatch_keeps_reservation_and_becomes_unknown) ... ok
test_dispatch_assessor_does_not_count_own_reservation_twice (tests.autonomous.test_ledger.LedgerTests.test_dispatch_assessor_does_not_count_own_reservation_twice) ... ok
test_dispatch_marker_commits_and_rechecks_control (tests.autonomous.test_ledger.LedgerTests.test_dispatch_marker_commits_and_rechecks_control) ... ok
test_duplicate_correction_retains_original_and_rejects_changed_key (tests.autonomous.test_ledger.LedgerTests.test_duplicate_correction_retains_original_and_rejects_changed_key) ... ok
test_failed_posting_rolls_back_entire_journal (tests.autonomous.test_ledger.LedgerTests.test_failed_posting_rolls_back_entire_journal) ... ok
test_failed_reservation_rolls_back_intent (tests.autonomous.test_ledger.LedgerTests.test_failed_reservation_rolls_back_intent) ... ok
test_filled_status_requires_complete_cumulative_quantity (tests.autonomous.test_ledger.LedgerTests.test_filled_status_requires_complete_cumulative_quantity) ... ok
test_foreign_keys_and_snapshot_exactly_one_reference (tests.autonomous.test_ledger.LedgerTests.test_foreign_keys_and_snapshot_exactly_one_reference) ... ok
test_forged_assessor_verdict_never_authorizes (tests.autonomous.test_ledger.LedgerTests.test_forged_assessor_verdict_never_authorizes) ... ok
test_invalid_money_and_missing_account_rejected (tests.autonomous.test_ledger.LedgerTests.test_invalid_money_and_missing_account_rejected) ... ok
test_migration_checksum_tampering_refuses_open (tests.autonomous.test_ledger.LedgerTests.test_migration_checksum_tampering_refuses_open) ... ok
test_nested_transaction_failure_cannot_commit_partial_post (tests.autonomous.test_ledger.LedgerTests.test_nested_transaction_failure_cannot_commit_partial_post) ... ok
test_open_never_adopts_or_arms_account (tests.autonomous.test_ledger.LedgerTests.test_open_never_adopts_or_arms_account) ... ok
test_order_events_are_typed_idempotent_and_keep_capacity (tests.autonomous.test_ledger.LedgerTests.test_order_events_are_typed_idempotent_and_keep_capacity) ... ok
test_reopen_preserves_journal_and_disarms (tests.autonomous.test_ledger.LedgerTests.test_reopen_preserves_journal_and_disarms) ... ok
test_reopen_rejects_removed_evidence_protection (tests.autonomous.test_ledger.LedgerTests.test_reopen_rejects_removed_evidence_protection) ... ok
test_reservation_commits_and_cross_release_action_is_unique (tests.autonomous.test_ledger.LedgerTests.test_reservation_commits_and_cross_release_action_is_unique) ... ok
test_reservation_duplicate_is_content_bound (tests.autonomous.test_ledger.LedgerTests.test_reservation_duplicate_is_content_bound) ... ok
test_reservation_rechecks_revision_and_capacity (tests.autonomous.test_ledger.LedgerTests.test_reservation_rechecks_revision_and_capacity) ... ok
test_reservation_unready_fails_closed (tests.autonomous.test_ledger.LedgerTests.test_reservation_unready_fails_closed) ... ok
test_review_job_dedupe_and_missing_reference (tests.autonomous.test_ledger.LedgerTests.test_review_job_dedupe_and_missing_reference) ... ok
test_review_r1_nested_dispatch_rejects_before_mutation (tests.autonomous.test_ledger.LedgerTests.test_review_r1_nested_dispatch_rejects_before_mutation) ... ok
test_review_r1_uncommitted_reservation_cannot_dispatch (tests.autonomous.test_ledger.LedgerTests.test_review_r1_uncommitted_reservation_cannot_dispatch) ... ok
test_review_r2_all_event_receipt_knowledge_and_segment_endpoints_are_causal (tests.autonomous.test_ledger.LedgerTests.test_review_r2_all_event_receipt_knowledge_and_segment_endpoints_are_causal) ... ok
test_review_r2_clock_regression_cutoff_preserves_committed_event_prefix (tests.autonomous.test_ledger.LedgerTests.test_review_r2_clock_regression_cutoff_preserves_committed_event_prefix) ... ok
test_review_r2_delayed_first_export_uses_historical_revision_and_generation (tests.autonomous.test_ledger.LedgerTests.test_review_r2_delayed_first_export_uses_historical_revision_and_generation) ... ok
test_review_r2_event_before_commit_capture_uses_previous_committed_prefix (tests.autonomous.test_ledger.LedgerTests.test_review_r2_event_before_commit_capture_uses_previous_committed_prefix) ... ok
test_review_r2_nested_result_receipt_participates_in_revision_visibility (tests.autonomous.test_ledger.LedgerTests.test_review_r2_nested_result_receipt_participates_in_revision_visibility) ... ok
test_review_r2_non_economic_import_commits_new_prefix_without_revision_change (tests.autonomous.test_ledger.LedgerTests.test_review_r2_non_economic_import_commits_new_prefix_without_revision_change) ... ok
test_review_r2_reconciliation_anchor_requires_selected_committed_prefix (tests.autonomous.test_ledger.LedgerTests.test_review_r2_reconciliation_anchor_requires_selected_committed_prefix) ... ok
test_review_r2_session_account_anchor_requires_creation_prefix (tests.autonomous.test_ledger.LedgerTests.test_review_r2_session_account_anchor_requires_creation_prefix) ... ok
test_review_r3_clock_regression_and_reverse_ids_preserve_order (tests.autonomous.test_ledger.LedgerTests.test_review_r3_clock_regression_and_reverse_ids_preserve_order) ... ok
test_review_r3_same_timestamp_order_replay_uses_durable_append_order (tests.autonomous.test_ledger.LedgerTests.test_review_r3_same_timestamp_order_replay_uses_durable_append_order) ... ok
test_review_r4_duplicate_manifest_record_id_is_rejected (tests.autonomous.test_ledger.LedgerTests.test_review_r4_duplicate_manifest_record_id_is_rejected) ... ok
test_review_r4_manifest_ids_use_real_primary_keys (tests.autonomous.test_ledger.LedgerTests.test_review_r4_manifest_ids_use_real_primary_keys) ... ok
test_review_r5_cumulative_quantity_is_exact_under_low_ambient_precision (tests.autonomous.test_ledger.LedgerTests.test_review_r5_cumulative_quantity_is_exact_under_low_ambient_precision) ... ok
test_review_r5_high_scale_order_ignores_ambient_decimal_precision (tests.autonomous.test_ledger.LedgerTests.test_review_r5_high_scale_order_ignores_ambient_decimal_precision) ... ok
test_schema_settings_and_append_only_protection (tests.autonomous.test_ledger.LedgerTests.test_schema_settings_and_append_only_protection) ... ok
test_session_export_does_not_include_future_source_or_control (tests.autonomous.test_ledger.LedgerTests.test_session_export_does_not_include_future_source_or_control) ... ok
test_session_export_is_immutable_and_idempotent (tests.autonomous.test_ledger.LedgerTests.test_session_export_is_immutable_and_idempotent) ... ok
test_session_order_projection_is_rebuilt_at_cutoff (tests.autonomous.test_ledger.LedgerTests.test_session_order_projection_is_rebuilt_at_cutoff) ... ok
test_startup_rejects_negative_decimal_projection (tests.autonomous.test_ledger.LedgerTests.test_startup_rejects_negative_decimal_projection) ... ok
test_transaction_identity_is_content_bound (tests.autonomous.test_ledger.LedgerTests.test_transaction_identity_is_content_bound) ... ok
test_unbalanced_journal_is_rejected (tests.autonomous.test_ledger.LedgerTests.test_unbalanced_journal_is_rejected) ... ok
test_deposit_cannot_hide_drawdown (tests.autonomous.test_risk_limits.FlowTests.test_deposit_cannot_hide_drawdown) ... ok
test_exact_thresholds_ignore_decimal_context (tests.autonomous.test_risk_limits.FlowTests.test_exact_thresholds_ignore_decimal_context) ... ok
test_external_flow_fixture_and_adjacent_exact_boundaries (tests.autonomous.test_risk_limits.FlowTests.test_external_flow_fixture_and_adjacent_exact_boundaries) ... ok
test_invalid_and_closure_reject_before_division (tests.autonomous.test_risk_limits.FlowTests.test_invalid_and_closure_reject_before_division) ... ok

----------------------------------------------------------------------
Ran 131 tests in 2.208s

OK
```

`git diff --check -- bot/autonomous/ledger.py tests/autonomous/test_ledger.py` exited 0. No provider/network calls, orders, deployment, release activation or arming occurred. No Task 4 work was started.


## Additional review finding: unsupported correction admission

Final source/test commit: **`a4fbd8daf554e39372f429aa12954d4924073a3a`** (`fix: inhibit unsupported fill corrections atomically`), following R1–R3 source commit `07ecc179d816b30a480e51bef85c82b45090b224`.

Independent review then reproduced an additional Important issue: a retained correction with a missing fee holding raised ContractViolation, rolled back its attempted economics, and left the previous complete mark ready. Root cause: correct_fill lacked the rejected-economics finding path used for unsupported fills/flows. Merely preflighting replay would not cover native serialization failures that occur after journal mutation begins.

The correction economic insert and application now run inside a SQLite savepoint. Unsupported replay/serialization (ContractViolation, KeyError or TypeError) rolls back all attempted compensation before committing an unresolved incident against the retained evidence and returning False. Original identity/predecessor rejection and SQLite uncertain-failure semantics are preserved. Successful corrections retain their normal fresh-review finding. The change does not suppress failed SQL or uncertain commits.

Two regressions were written and executed before this source edit. Initial unhandled ContractViolation outputs were converted to explicit assertion failures, then the RED command below produced exactly 2 failures, 0 errors, as expected:

```text
.venv-autonomous/bin/python -m unittest tests.autonomous.test_ledger.AccountingTests.test_unknown_correction_fee_retains_evidence_and_inhibits_without_economics tests.autonomous.test_ledger.AccountingTests.test_correction_serialization_failure_rolls_back_before_retaining_incident -v

AssertionError: unsupported correction must retain an inhibition: insufficient asset for disposal/fee
AssertionError: unsupported serialization must retain an inhibition: native posting scale exceeded
Ran 2 tests in 0.042s
FAILED (failures=2)
```

The real unknown-fee case verifies retained immutable evidence, one finding across retries, unchanged journal count/balances, no accepted correction row, persistent inhibition after a complete mark, and successful restart with unchanged economics. A native serialization fault injected after journal creation verifies exact rollback of journal transactions/postings, correction rows, accounting operations and balance projections, while retaining the evidence and unresolved state.

The following final covering run supersedes the earlier 131-test result. Python remains **3.12.14**; command exit **0**; full captured output follows:

```text
.venv-autonomous/bin/python -m unittest tests.autonomous.test_contracts tests.autonomous.test_ledger tests.autonomous.test_risk_limits -v
```

```text
test_book_delta_deletions_and_signed_metrics (tests.autonomous.test_contracts.ContractTests.test_book_delta_deletions_and_signed_metrics) ... ok
test_canonical_and_finite (tests.autonomous.test_contracts.ContractTests.test_canonical_and_finite) ... ok
test_canonical_rejects_lossy_keys_and_unsupported_values (tests.autonomous.test_contracts.ContractTests.test_canonical_rejects_lossy_keys_and_unsupported_values) ... ok
test_continuous_wire_slots_obey_dictionary_ranges (tests.autonomous.test_contracts.ContractTests.test_continuous_wire_slots_obey_dictionary_ranges) ... ok
test_decimal_instrument_scale_supports_full_precision (tests.autonomous.test_contracts.ContractTests.test_decimal_instrument_scale_supports_full_precision) ... ok
test_decimal_precision_scale_and_explicit_rounding (tests.autonomous.test_contracts.ContractTests.test_decimal_precision_scale_and_explicit_rounding) ... ok
test_defaults_disable_exposure_and_fix_approved_limits (tests.autonomous.test_contracts.ContractTests.test_defaults_disable_exposure_and_fix_approved_limits) ... ok
test_dictionary_covers_all_compact_slots (tests.autonomous.test_contracts.ContractTests.test_dictionary_covers_all_compact_slots) ... ok
test_dictionary_documents_wire_metadata_and_thesis_bit_width (tests.autonomous.test_contracts.ContractTests.test_dictionary_documents_wire_metadata_and_thesis_bit_width) ... ok
test_direction_distribution_and_toxic_threshold (tests.autonomous.test_contracts.ContractTests.test_direction_distribution_and_toxic_threshold) ... ok
test_discrete_wire_slots_have_exact_integer_domains (tests.autonomous.test_contracts.ContractTests.test_discrete_wire_slots_have_exact_integer_domains) ... ok
test_eligibility_needs_evidence_and_ondo_cannot_activate (tests.autonomous.test_contracts.ContractTests.test_eligibility_needs_evidence_and_ondo_cannot_activate) ... ok
test_encoded_state_checks_complete_numeric_shape (tests.autonomous.test_contracts.ContractTests.test_encoded_state_checks_complete_numeric_shape) ... ok
test_every_architecture_record_is_frozen_and_typed (tests.autonomous.test_contracts.ContractTests.test_every_architecture_record_is_frozen_and_typed) ... ok
test_frozen_strict_roundtrip (tests.autonomous.test_contracts.ContractTests.test_frozen_strict_roundtrip) ... ok
test_hash_type_id_and_unknown_enum_are_rejected_at_wire_boundary (tests.autonomous.test_contracts.ContractTests.test_hash_type_id_and_unknown_enum_are_rejected_at_wire_boundary) ... ok
test_immutable_nested_values_and_declared_probability_bounds (tests.autonomous.test_contracts.ContractTests.test_immutable_nested_values_and_declared_probability_bounds) ... ok
test_invalid_measurements_are_zero_and_snapshot_masks_match (tests.autonomous.test_contracts.ContractTests.test_invalid_measurements_are_zero_and_snapshot_masks_match) ... ok
test_late_batch_requires_rejection_without_decisions (tests.autonomous.test_contracts.ContractTests.test_late_batch_requires_rejection_without_decisions) ... ok
test_late_model_batch_and_unverified_token_accounting_refuse (tests.autonomous.test_contracts.ContractTests.test_late_model_batch_and_unverified_token_accounting_refuse) ... ok
test_nonnegative_account_magnitudes_and_revisions (tests.autonomous.test_contracts.ContractTests.test_nonnegative_account_magnitudes_and_revisions) ... ok
test_numeric_candidate_mapping_is_fixed_and_exact (tests.autonomous.test_contracts.ContractTests.test_numeric_candidate_mapping_is_fixed_and_exact) ... ok
test_order_reference_and_authorization_are_not_implicit (tests.autonomous.test_contracts.ContractTests.test_order_reference_and_authorization_are_not_implicit) ... ok
test_source_payload_is_typed_hashed_and_immutable (tests.autonomous.test_contracts.ContractTests.test_source_payload_is_typed_hashed_and_immutable) ... ok
test_timestamps_enums_and_nested_records (tests.autonomous.test_contracts.ContractTests.test_timestamps_enums_and_nested_records) ... ok
test_wire_masks_have_bounded_integer_bit_widths (tests.autonomous.test_contracts.ContractTests.test_wire_masks_have_bounded_integer_bit_widths) ... ok
test_canonical_reduced_rational_and_resource_bound (tests.autonomous.test_contracts.ExactNavTests.test_canonical_reduced_rational_and_resource_bound) ... ok
test_decimal_projections_cannot_disagree_with_exact_authority (tests.autonomous.test_contracts.ExactNavTests.test_decimal_projections_cannot_disagree_with_exact_authority) ... ok
test_ready_fixture_carries_exact_revision_mark_and_equity (tests.autonomous.test_contracts.ExactNavTests.test_ready_fixture_carries_exact_revision_mark_and_equity) ... ok
test_ready_view_requires_bound_exact_evidence (tests.autonomous.test_contracts.ExactNavTests.test_ready_view_requires_bound_exact_evidence) ... ok
test_accounting_write_boundary_failures_roll_back_every_projection (tests.autonomous.test_ledger.AccountingTests.test_accounting_write_boundary_failures_roll_back_every_projection) ... ok
test_additive_accounting_migration_preserves_legacy_committed_export (tests.autonomous.test_ledger.AccountingTests.test_additive_accounting_migration_preserves_legacy_committed_export) ... ok
test_buy_net_base_fee_and_sell_extra_base_fee_apply_once (tests.autonomous.test_ledger.AccountingTests.test_buy_net_base_fee_and_sell_extra_base_fee_apply_once) ... ok
test_buy_sell_native_fees_basis_and_reservation_conservation (tests.autonomous.test_ledger.AccountingTests.test_buy_sell_native_fees_basis_and_reservation_conservation) ... ok
test_closed_redeposit_requires_fresh_transfer_not_fresh_preclosure_mark (tests.autonomous.test_ledger.AccountingTests.test_closed_redeposit_requires_fresh_transfer_not_fresh_preclosure_mark) ... ok
test_closure_redeposit_and_reopen_keep_halts_and_exact_history (tests.autonomous.test_ledger.AccountingTests.test_closure_redeposit_and_reopen_keep_halts_and_exact_history) ... ok
test_committed_reservation_rechecks_held_book_and_fx_at_dispatch (tests.autonomous.test_ledger.AccountingTests.test_committed_reservation_rechecks_held_book_and_fx_at_dispatch) ... ok
test_correction_replays_old_buy_basis_after_partial_sell (tests.autonomous.test_ledger.AccountingTests.test_correction_replays_old_buy_basis_after_partial_sell) ... ok
test_correction_serialization_failure_rolls_back_before_retaining_incident (tests.autonomous.test_ledger.AccountingTests.test_correction_serialization_failure_rolls_back_before_retaining_incident) ... ok
test_current_accounting_mark_rebinds_only_non_economic_revisions (tests.autonomous.test_ledger.AccountingTests.test_current_accounting_mark_rebinds_only_non_economic_revisions) ... ok
test_delayed_closed_redeposit_preserves_exact_loss_history_and_requires_new_mark (tests.autonomous.test_ledger.AccountingTests.test_delayed_closed_redeposit_preserves_exact_loss_history_and_requires_new_mark) ... ok
test_delayed_mark_application_cannot_refresh_held_book_or_fx (tests.autonomous.test_ledger.AccountingTests.test_delayed_mark_application_cannot_refresh_held_book_or_fx) ... ok
test_deposit_loss_closure_restart_and_redeposit_preserve_latch (tests.autonomous.test_ledger.AccountingTests.test_deposit_loss_closure_restart_and_redeposit_preserve_latch) ... ok
test_economic_failure_rolls_back_and_prefix_excludes_future_mark (tests.autonomous.test_ledger.AccountingTests.test_economic_failure_rolls_back_and_prefix_excludes_future_mark) ... ok
test_exact_accounting_events_are_cutoff_prefix_bound (tests.autonomous.test_ledger.AccountingTests.test_exact_accounting_events_are_cutoff_prefix_bound) ... ok
test_exact_flow_after_units_are_checked_on_restart (tests.autonomous.test_ledger.AccountingTests.test_exact_flow_after_units_are_checked_on_restart) ... ok
test_fill_after_flow_still_latches_loss_in_economic_transaction (tests.autonomous.test_ledger.AccountingTests.test_fill_after_flow_still_latches_loss_in_economic_transaction) ... ok
test_fill_known_cash_fee_can_latch_before_another_reconciliation (tests.autonomous.test_ledger.AccountingTests.test_fill_known_cash_fee_can_latch_before_another_reconciliation) ... ok
test_flow_with_missing_fx_keeps_evidence_and_inhibits_entries (tests.autonomous.test_ledger.AccountingTests.test_flow_with_missing_fx_keeps_evidence_and_inhibits_entries) ... ok
test_genuine_boundary_restored_after_outage_requires_current_reconciliation (tests.autonomous.test_ledger.AccountingTests.test_genuine_boundary_restored_after_outage_requires_current_reconciliation) ... ok
test_held_book_and_fx_expire_independently_of_mark_for_readiness (tests.autonomous.test_ledger.AccountingTests.test_held_book_and_fx_expire_independently_of_mark_for_readiness) ... ok
test_held_book_and_fx_expiry_refuses_new_reservation (tests.autonomous.test_ledger.AccountingTests.test_held_book_and_fx_expiry_refuses_new_reservation) ... ok
test_late_fill_changed_retry_and_stale_correction_preserve_history (tests.autonomous.test_ledger.AccountingTests.test_late_fill_changed_retry_and_stale_correction_preserve_history) ... ok
test_later_depth_cannot_be_relabeled_as_midnight (tests.autonomous.test_ledger.AccountingTests.test_later_depth_cannot_be_relabeled_as_midnight) ... ok
test_local_evidence_cannot_claim_live_provenance_or_changed_identity (tests.autonomous.test_ledger.AccountingTests.test_local_evidence_cannot_claim_live_provenance_or_changed_identity) ... ok
test_missing_fx_liquidation_invalidates_previous_ready_mark (tests.autonomous.test_ledger.AccountingTests.test_missing_fx_liquidation_invalidates_previous_ready_mark) ... ok
test_missing_or_stale_depth_unknown_holding_and_boundary_are_unready (tests.autonomous.test_ledger.AccountingTests.test_missing_or_stale_depth_unknown_holding_and_boundary_are_unready) ... ok
test_new_terminal_fill_and_open_cumulative_overfill_journal_once (tests.autonomous.test_ledger.AccountingTests.test_new_terminal_fill_and_open_cumulative_overfill_journal_once) ... ok
test_non_usd_external_capital_requires_fx_and_preserves_nav (tests.autonomous.test_ledger.AccountingTests.test_non_usd_external_capital_requires_fx_and_preserves_nav) ... ok
test_open_flow_cannot_refresh_expired_held_valuation_sources (tests.autonomous.test_ledger.AccountingTests.test_open_flow_cannot_refresh_expired_held_valuation_sources) ... ok
test_projection_failure_retains_factual_transfer_and_inhibits_ready_view (tests.autonomous.test_ledger.AccountingTests.test_projection_failure_retains_factual_transfer_and_inhibits_ready_view) ... ok
test_repeated_capital_flow_keeps_depth_for_following_loss_event (tests.autonomous.test_ledger.AccountingTests.test_repeated_capital_flow_keeps_depth_for_following_loss_event) ... ok
test_repeating_units_repeated_flows_and_exact_view (tests.autonomous.test_ledger.AccountingTests.test_repeating_units_repeated_flows_and_exact_view) ... ok
test_rounding_and_compensation_export_cutoff_keeps_original_economics (tests.autonomous.test_ledger.AccountingTests.test_rounding_and_compensation_export_cutoff_keeps_original_economics) ... ok
test_stale_depth_heartbeat_and_unexplained_holdings_do_not_refresh_readiness (tests.autonomous.test_ledger.AccountingTests.test_stale_depth_heartbeat_and_unexplained_holdings_do_not_refresh_readiness) ... ok
test_startup_rejects_day_boundary_projection_tampering (tests.autonomous.test_ledger.AccountingTests.test_startup_rejects_day_boundary_projection_tampering) ... ok
test_startup_rejects_highwater_and_native_projection_tampering (tests.autonomous.test_ledger.AccountingTests.test_startup_rejects_highwater_and_native_projection_tampering) ... ok
test_startup_rejects_mutable_unit_projection_tampering (tests.autonomous.test_ledger.AccountingTests.test_startup_rejects_mutable_unit_projection_tampering) ... ok
test_submicro_basis_residual_and_duplicate_fill_evidence (tests.autonomous.test_ledger.AccountingTests.test_submicro_basis_residual_and_duplicate_fill_evidence) ... ok
test_submicro_usd_transfer_cannot_silently_round_native_cash (tests.autonomous.test_ledger.AccountingTests.test_submicro_usd_transfer_cannot_silently_round_native_cash) ... ok
test_terminal_fill_replay_does_not_change_reservation_on_another_intent_fill (tests.autonomous.test_ledger.AccountingTests.test_terminal_fill_replay_does_not_change_reservation_on_another_intent_fill) ... ok
test_terminal_fill_replay_does_not_change_reservation_on_unrelated_deposit (tests.autonomous.test_ledger.AccountingTests.test_terminal_fill_replay_does_not_change_reservation_on_unrelated_deposit) ... ok
test_third_asset_fee_fx_and_pending_unknown_conversion (tests.autonomous.test_ledger.AccountingTests.test_third_asset_fee_fx_and_pending_unknown_conversion) ... ok
test_trailing_zero_fill_fields_are_canonical_idempotent_and_restartable (tests.autonomous.test_ledger.AccountingTests.test_trailing_zero_fill_fields_are_canonical_idempotent_and_restartable) ... ok
test_trailing_zero_flow_is_canonical_idempotent_and_restartable (tests.autonomous.test_ledger.AccountingTests.test_trailing_zero_flow_is_canonical_idempotent_and_restartable) ... ok
test_transfer_cost_triggers_daily_halt_atomically_and_utc_rollover (tests.autonomous.test_ledger.AccountingTests.test_transfer_cost_triggers_daily_halt_atomically_and_utc_rollover) ... ok
test_unknown_correction_fee_retains_evidence_and_inhibits_without_economics (tests.autonomous.test_ledger.AccountingTests.test_unknown_correction_fee_retains_evidence_and_inhibits_without_economics) ... ok
test_unknown_fill_semantics_records_incident_and_invalidates_ready_view (tests.autonomous.test_ledger.AccountingTests.test_unknown_fill_semantics_records_incident_and_invalidates_ready_view) ... ok
test_wide_native_correction_preserves_exact_posting_targets_and_reopens (tests.autonomous.test_ledger.AccountingTests.test_wide_native_correction_preserves_exact_posting_targets_and_reopens) ... ok
test_zero_equity_still_persists_breach_while_unarmed (tests.autonomous.test_ledger.AccountingTests.test_zero_equity_still_persists_breach_while_unarmed) ... ok
test_busy_failure_disables_dispatch_without_partial_write (tests.autonomous.test_ledger.LedgerTests.test_busy_failure_disables_dispatch_without_partial_write) ... ok
test_cash_competition_rechecks_pending_reservations (tests.autonomous.test_ledger.LedgerTests.test_cash_competition_rechecks_pending_reservations) ... ok
test_changed_release_content_fails_startup_integrity (tests.autonomous.test_ledger.LedgerTests.test_changed_release_content_fails_startup_integrity) ... ok
test_changed_venue_event_key_payload_is_rejected (tests.autonomous.test_ledger.LedgerTests.test_changed_venue_event_key_payload_is_rejected) ... ok
test_correction_and_label_keys_are_unique (tests.autonomous.test_ledger.LedgerTests.test_correction_and_label_keys_are_unique) ... ok
test_crash_after_dispatch_keeps_reservation_and_becomes_unknown (tests.autonomous.test_ledger.LedgerTests.test_crash_after_dispatch_keeps_reservation_and_becomes_unknown) ... ok
test_dispatch_assessor_does_not_count_own_reservation_twice (tests.autonomous.test_ledger.LedgerTests.test_dispatch_assessor_does_not_count_own_reservation_twice) ... ok
test_dispatch_marker_commits_and_rechecks_control (tests.autonomous.test_ledger.LedgerTests.test_dispatch_marker_commits_and_rechecks_control) ... ok
test_duplicate_correction_retains_original_and_rejects_changed_key (tests.autonomous.test_ledger.LedgerTests.test_duplicate_correction_retains_original_and_rejects_changed_key) ... ok
test_failed_posting_rolls_back_entire_journal (tests.autonomous.test_ledger.LedgerTests.test_failed_posting_rolls_back_entire_journal) ... ok
test_failed_reservation_rolls_back_intent (tests.autonomous.test_ledger.LedgerTests.test_failed_reservation_rolls_back_intent) ... ok
test_filled_status_requires_complete_cumulative_quantity (tests.autonomous.test_ledger.LedgerTests.test_filled_status_requires_complete_cumulative_quantity) ... ok
test_foreign_keys_and_snapshot_exactly_one_reference (tests.autonomous.test_ledger.LedgerTests.test_foreign_keys_and_snapshot_exactly_one_reference) ... ok
test_forged_assessor_verdict_never_authorizes (tests.autonomous.test_ledger.LedgerTests.test_forged_assessor_verdict_never_authorizes) ... ok
test_invalid_money_and_missing_account_rejected (tests.autonomous.test_ledger.LedgerTests.test_invalid_money_and_missing_account_rejected) ... ok
test_migration_checksum_tampering_refuses_open (tests.autonomous.test_ledger.LedgerTests.test_migration_checksum_tampering_refuses_open) ... ok
test_nested_transaction_failure_cannot_commit_partial_post (tests.autonomous.test_ledger.LedgerTests.test_nested_transaction_failure_cannot_commit_partial_post) ... ok
test_open_never_adopts_or_arms_account (tests.autonomous.test_ledger.LedgerTests.test_open_never_adopts_or_arms_account) ... ok
test_order_events_are_typed_idempotent_and_keep_capacity (tests.autonomous.test_ledger.LedgerTests.test_order_events_are_typed_idempotent_and_keep_capacity) ... ok
test_reopen_preserves_journal_and_disarms (tests.autonomous.test_ledger.LedgerTests.test_reopen_preserves_journal_and_disarms) ... ok
test_reopen_rejects_removed_evidence_protection (tests.autonomous.test_ledger.LedgerTests.test_reopen_rejects_removed_evidence_protection) ... ok
test_reservation_commits_and_cross_release_action_is_unique (tests.autonomous.test_ledger.LedgerTests.test_reservation_commits_and_cross_release_action_is_unique) ... ok
test_reservation_duplicate_is_content_bound (tests.autonomous.test_ledger.LedgerTests.test_reservation_duplicate_is_content_bound) ... ok
test_reservation_rechecks_revision_and_capacity (tests.autonomous.test_ledger.LedgerTests.test_reservation_rechecks_revision_and_capacity) ... ok
test_reservation_unready_fails_closed (tests.autonomous.test_ledger.LedgerTests.test_reservation_unready_fails_closed) ... ok
test_review_job_dedupe_and_missing_reference (tests.autonomous.test_ledger.LedgerTests.test_review_job_dedupe_and_missing_reference) ... ok
test_review_r1_nested_dispatch_rejects_before_mutation (tests.autonomous.test_ledger.LedgerTests.test_review_r1_nested_dispatch_rejects_before_mutation) ... ok
test_review_r1_uncommitted_reservation_cannot_dispatch (tests.autonomous.test_ledger.LedgerTests.test_review_r1_uncommitted_reservation_cannot_dispatch) ... ok
test_review_r2_all_event_receipt_knowledge_and_segment_endpoints_are_causal (tests.autonomous.test_ledger.LedgerTests.test_review_r2_all_event_receipt_knowledge_and_segment_endpoints_are_causal) ... ok
test_review_r2_clock_regression_cutoff_preserves_committed_event_prefix (tests.autonomous.test_ledger.LedgerTests.test_review_r2_clock_regression_cutoff_preserves_committed_event_prefix) ... ok
test_review_r2_delayed_first_export_uses_historical_revision_and_generation (tests.autonomous.test_ledger.LedgerTests.test_review_r2_delayed_first_export_uses_historical_revision_and_generation) ... ok
test_review_r2_event_before_commit_capture_uses_previous_committed_prefix (tests.autonomous.test_ledger.LedgerTests.test_review_r2_event_before_commit_capture_uses_previous_committed_prefix) ... ok
test_review_r2_nested_result_receipt_participates_in_revision_visibility (tests.autonomous.test_ledger.LedgerTests.test_review_r2_nested_result_receipt_participates_in_revision_visibility) ... ok
test_review_r2_non_economic_import_commits_new_prefix_without_revision_change (tests.autonomous.test_ledger.LedgerTests.test_review_r2_non_economic_import_commits_new_prefix_without_revision_change) ... ok
test_review_r2_reconciliation_anchor_requires_selected_committed_prefix (tests.autonomous.test_ledger.LedgerTests.test_review_r2_reconciliation_anchor_requires_selected_committed_prefix) ... ok
test_review_r2_session_account_anchor_requires_creation_prefix (tests.autonomous.test_ledger.LedgerTests.test_review_r2_session_account_anchor_requires_creation_prefix) ... ok
test_review_r3_clock_regression_and_reverse_ids_preserve_order (tests.autonomous.test_ledger.LedgerTests.test_review_r3_clock_regression_and_reverse_ids_preserve_order) ... ok
test_review_r3_same_timestamp_order_replay_uses_durable_append_order (tests.autonomous.test_ledger.LedgerTests.test_review_r3_same_timestamp_order_replay_uses_durable_append_order) ... ok
test_review_r4_duplicate_manifest_record_id_is_rejected (tests.autonomous.test_ledger.LedgerTests.test_review_r4_duplicate_manifest_record_id_is_rejected) ... ok
test_review_r4_manifest_ids_use_real_primary_keys (tests.autonomous.test_ledger.LedgerTests.test_review_r4_manifest_ids_use_real_primary_keys) ... ok
test_review_r5_cumulative_quantity_is_exact_under_low_ambient_precision (tests.autonomous.test_ledger.LedgerTests.test_review_r5_cumulative_quantity_is_exact_under_low_ambient_precision) ... ok
test_review_r5_high_scale_order_ignores_ambient_decimal_precision (tests.autonomous.test_ledger.LedgerTests.test_review_r5_high_scale_order_ignores_ambient_decimal_precision) ... ok
test_schema_settings_and_append_only_protection (tests.autonomous.test_ledger.LedgerTests.test_schema_settings_and_append_only_protection) ... ok
test_session_export_does_not_include_future_source_or_control (tests.autonomous.test_ledger.LedgerTests.test_session_export_does_not_include_future_source_or_control) ... ok
test_session_export_is_immutable_and_idempotent (tests.autonomous.test_ledger.LedgerTests.test_session_export_is_immutable_and_idempotent) ... ok
test_session_order_projection_is_rebuilt_at_cutoff (tests.autonomous.test_ledger.LedgerTests.test_session_order_projection_is_rebuilt_at_cutoff) ... ok
test_startup_rejects_negative_decimal_projection (tests.autonomous.test_ledger.LedgerTests.test_startup_rejects_negative_decimal_projection) ... ok
test_transaction_identity_is_content_bound (tests.autonomous.test_ledger.LedgerTests.test_transaction_identity_is_content_bound) ... ok
test_unbalanced_journal_is_rejected (tests.autonomous.test_ledger.LedgerTests.test_unbalanced_journal_is_rejected) ... ok
test_deposit_cannot_hide_drawdown (tests.autonomous.test_risk_limits.FlowTests.test_deposit_cannot_hide_drawdown) ... ok
test_exact_thresholds_ignore_decimal_context (tests.autonomous.test_risk_limits.FlowTests.test_exact_thresholds_ignore_decimal_context) ... ok
test_external_flow_fixture_and_adjacent_exact_boundaries (tests.autonomous.test_risk_limits.FlowTests.test_external_flow_fixture_and_adjacent_exact_boundaries) ... ok
test_invalid_and_closure_reject_before_division (tests.autonomous.test_risk_limits.FlowTests.test_invalid_and_closure_reject_before_division) ... ok

----------------------------------------------------------------------
Ran 133 tests in 2.265s

OK
```

Final owned source/test/report `git diff --check` passed. Independent reviewer is evaluating this final source endpoint; this report does not grant review approval. No providers, network, orders, deployment or Task 4 actions were used.
