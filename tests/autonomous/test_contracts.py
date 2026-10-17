import dataclasses
import hashlib
import json
import unittest
from decimal import Decimal
from pathlib import Path

try:
    from bot.autonomous import contracts as c
except ImportError:
    c = None


class ContractTests(unittest.TestCase):
    def setUp(self):
        self.assertIsNotNone(c, "autonomous domain contracts are required")

    def test_canonical_and_finite(self):
        self.assertEqual(c.canonical_bytes({"b": 2, "a": 1}), b'{"a":1,"b":2}')
        with self.assertRaises(ValueError):
            c.canonical_bytes({"p": float("nan")})
        for value in (True, 1.5, 2**63):
            with self.assertRaises(ValueError):
                c.checked_money(value)

    def test_canonical_rejects_lossy_keys_and_unsupported_values(self):
        for value in ({1: "x"}, {"x": object()}, {"x": float("inf")}):
            with self.assertRaises(ValueError):
                c.canonical_bytes(value)
        self.assertEqual(c.content_hash({"a": 1}), hashlib.sha256(b'{"a":1}').hexdigest())

    def test_decimal_precision_scale_and_explicit_rounding(self):
        self.assertEqual(c.decimal_value("0.000001"), Decimal("0.000001"))
        for value in (True, 1.5, "NaN", "Infinity", "1e-1000", "1.12345678901234567890123456789012345678"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                c.decimal_value(value)
        self.assertEqual(c.decimal_text(Decimal("1.2300")), "1.23")
        self.assertEqual(c.to_microdollars("1.0000009", rounding="ROUND_DOWN"), 1000000)
        with self.assertRaises(ValueError):
            c.to_microdollars("1.0000009")
        with self.assertRaises(ValueError):
            c.check_scale(Decimal("1.001"), 2)

    def test_frozen_strict_roundtrip(self):
        revision = c.AccountRevision(1, 2)
        with self.assertRaises(dataclasses.FrozenInstanceError):
            revision.version = 3
        self.assertEqual(c.AccountRevision.from_json(revision.to_json()), revision)
        for raw in ('{"journal_sequence":1,"version":2,"extra":0}', '{"journal_sequence":true,"version":2}', '{"journal_sequence":1,"journal_sequence":2,"version":2}', '{"journal_sequence":NaN,"version":2}'):
            with self.subTest(raw=raw), self.assertRaises(ValueError):
                c.AccountRevision.from_json(raw)

    def test_timestamps_enums_and_nested_records(self):
        with self.assertRaises(ValueError):
            c.CausalCutoffs(-1, 2, 3, "boot")
        with self.assertRaises(ValueError):
            c.CausalCutoffs(3, 2, 3, "boot")
        with self.assertRaises(ValueError):
            c.ControlState(mode="live")
        with self.assertRaises(ValueError):
            c.AccountRevision(True, 0)
        with self.assertRaises(ValueError):
            c.SharedContext((1.0,), 1)

    def test_direction_distribution_and_toxic_threshold(self):
        decision = c.CandidateDecision("aave", c.Regime.TRENDING, c.Direction.LONG, 2, c.AdvisoryRisk.SAFE,
            c.DirectionDistribution(0.9, 0.05, 0.05), 0.9, 0.9, 0.9, 0.9, 0.5)
        self.assertTrue(decision.toxic_flow)
        self.assertEqual(c.CandidateDecision.from_json(decision.to_json()), decision)
        with self.assertRaises(ValueError):
            dataclasses.replace(decision, direction_confidence=float("nan"))
        with self.assertRaises(ValueError):
            dataclasses.replace(decision, setup_quality=True)
        with self.assertRaises(ValueError):
            c.DirectionDistribution(0.9, 0.2, 0.1)

    def test_source_payload_is_typed_hashed_and_immutable(self):
        payload = c.BookPayload(((Decimal("99"), Decimal("1")),), ((Decimal("101"), Decimal("2")),))
        event = c.SourceEvent("e", "venue", "book", "AAVE-USD", "book-1", 0, 1, 2, 2, payload, c.content_hash(payload))
        self.assertEqual(c.SourceEvent.from_json(event.to_json()), event)
        with self.assertRaises(ValueError):
            dataclasses.replace(event, payload={"bids": []})
        with self.assertRaises(ValueError):
            dataclasses.replace(event, payload_hash="0" * 64)

    def test_defaults_disable_exposure_and_fix_approved_limits(self):
        limits = c.RiskLimits()
        self.assertEqual((limits.max_drawdown, limits.max_daily_loss, limits.max_position_fraction, limits.max_gross_fraction),
                         (Decimal("0.15"), Decimal("0.02"), Decimal("0.05"), Decimal("0.20")))
        self.assertEqual((limits.max_entry_loss_fraction, limits.max_pending_loss_fraction, limits.kelly_fraction),
                         (Decimal("0.0025"), Decimal("0.01"), Decimal("0.25")))
        self.assertFalse(limits.exposure_enabled)
        self.assertFalse(c.ControlState().armed)
        self.assertEqual(c.WorkerConfig().mode, c.Mode.PAPER)
        self.assertFalse(c.CandidateDefinition("aave", "AAVE-USD").eligible)
        path = Path(__file__).resolve().parents[2] / "config/autonomous/risk.yaml"
        self.assertEqual(c.RiskLimits.from_dict(json.loads(path.read_text())), limits)
        for field, bad in (("max_drawdown", Decimal("0.16")), ("jev_retries", 1), ("armed", True)):
            with self.assertRaises(ValueError):
                dataclasses.replace(limits, **{field: bad})

    def test_dictionary_covers_all_compact_slots(self):
        dictionary = c.default_field_dictionary()
        self.assertEqual(len(dictionary.shared_fields), 17)
        self.assertEqual(len(dictionary.candidate_fields), 16)
        self.assertEqual(c.FieldDictionary.from_json(dictionary.to_json()), dictionary)
        self.assertEqual(dictionary.shared_fields[0].name, "dictionary_version")
        with self.assertRaises(ValueError):
            dataclasses.replace(dictionary, candidate_fields=dictionary.candidate_fields[:-1])

    def test_every_architecture_record_is_frozen_and_typed(self):
        names = "SourceEvent CausalCutoffs NumericSnapshot SharedContext FieldDictionary TokenCount EncodedState QuestionBatch JudgeRequest DecisionBatch CandidateDecision CalibrationArtifact EdgeEstimate AccountView AccountRevision MarketRules OrderProposal PreparedIntent ControlState RiskVerdict PolicyResult SubmitOrderCommand CancelOrderCommand ControlCommand IntentReceipt ControlReceipt ReconcileReason ReconcileReport OrderReference VenueOrderResult VenueAccountSnapshot FillCursor FillPage DecisionReference OrderEvent AppendResult SessionCutoff SessionArchive EvidenceRequest EvidenceObservation EvidenceManifest CandidateDefinition ThesisAssessment ReviewJob ReviewProposal ReviewReport EvaluationSplit EvaluationResult CandidateBundle ApprovedBundle GateApproval WorkerConfig WorkerDependencies IngestResult SizedProposal SizeRejection Hold RiskRejection PendingReconciliation UnavailableEdge BundleRejection".split()
        for name in names:
            with self.subTest(name=name):
                cls = getattr(c, name)
                if name == "ReconcileReason":
                    continue
                self.assertTrue(dataclasses.is_dataclass(cls))
                self.assertTrue(cls.__dataclass_params__.frozen)
                self.assertTrue(dataclasses.fields(cls))

    def test_immutable_nested_values_and_declared_probability_bounds(self):
        for probabilities in ((1.0, 0.0, float("inf")), (True, 0.0, 0.0), (-0.1, 0.1, 1.0)):
            with self.subTest(probabilities=probabilities), self.assertRaises(ValueError):
                c.DirectionDistribution(*probabilities)
        with self.assertRaises(ValueError):
            c.BookPayload([(Decimal("99"), Decimal("1"))], ((Decimal("101"), Decimal("1")),))
        with self.assertRaises(ValueError):
            c.CalibrationBin(0.0, 1.0, 1.01, 10)

    def test_book_delta_deletions_and_signed_metrics(self):
        self.assertEqual(c.MetricPayload("return", Decimal("-0.01"), "fraction").value, Decimal("-0.01"))
        deletion = c.BookPayload(((Decimal("99"), Decimal("0")),), (), is_snapshot=False)
        self.assertFalse(deletion.is_snapshot)
        with self.assertRaises(ValueError):
            c.BookPayload(((Decimal("99"), Decimal("1")), (Decimal("100"), Decimal("1"))), ((Decimal("101"), Decimal("1")),))

    def test_nonnegative_account_magnitudes_and_revisions(self):
        with self.assertRaises(ValueError):
            c.Balance("USD", Decimal("1"), -1)
        with self.assertRaises(ValueError):
            c.Reservation("intent", 1, Decimal("1"), 1, 1, Decimal("2"), c.IntentState.RESERVED)
        with self.assertRaises(ValueError):
            c.AccountView("paper", ready=True)
        with self.assertRaises(ValueError):
            c.AccountRevision(-1, 0)

    def test_order_reference_and_authorization_are_not_implicit(self):
        from tests.autonomous.support import order_proposal
        proposal = order_proposal()
        self.assertEqual(c.OrderProposal.from_json(proposal.to_json()), proposal)
        for changes in ({"side": c.Side.SELL}, {"decision": None}, {"base_quantity": Decimal("0")}, {"release_hash": "f" * 64}):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                dataclasses.replace(proposal, **changes)
        with self.assertRaises(ValueError):
            c.SubmitOrderCommand("submit", proposal, c.AccountRevision(), 1, None)
        with self.assertRaises(ValueError):
            c.ControlState(armed=True)

    def test_eligibility_needs_evidence_and_ondo_cannot_activate(self):
        with self.assertRaises(ValueError):
            c.CandidateDefinition("aave", "AAVE-USD", eligible=True)
        with self.assertRaises(ValueError):
            c.CandidateDefinition("ondo", "ONDO-USD", eligible=True, evidence_manifest_hash="a" * 64)
        with self.assertRaises(ValueError):
            c.EvidenceManifest("aave", (), "a" * 64, 10, 11, eligible=True)

    def test_late_model_batch_and_unverified_token_accounting_refuse(self):
        with self.assertRaises(c.TokenAccountingUnavailable):
            c.TokenCount(10, "tok", "provider", "character_heuristic", "a" * 64)
        with self.assertRaises(ValueError):
            c.EvaluationSplit("a" * 64, "b" * 64, "holdout", 10, 9, 20, 30, 40)
        with self.assertRaises(ValueError):
            c.ReconcileReport("rec", "paper", c.ReconcileReason.STARTUP, 20, 10, True, (), c.AccountRevision())

    def test_decimal_instrument_scale_supports_full_precision(self):
        value = Decimal("1234567890123456789012345678.120")
        self.assertEqual(c.check_scale(value, 2), value)
        with self.assertRaises(ValueError):
            c.decimal_value("123456789012345678901234567890123456789")

    def test_hash_type_id_and_unknown_enum_are_rejected_at_wire_boundary(self):
        for raw in ('{"mode":"surprise"}', '{"mode":"paper","armed":1}', '{"mode":"paper","active_release_hash":"bad"}'):
            with self.subTest(raw=raw), self.assertRaises(ValueError):
                c.ControlState.from_json(raw)
        with self.assertRaises(ValueError):
            c.AccountView("x" * 257)

    def test_encoded_state_checks_complete_numeric_shape(self):
        cutoffs = c.CausalCutoffs(1, 2, 3, "boot")
        snapshot = c.NumericSnapshot("aave", "AAVE-USD", "a" * 64, cutoffs, (), 0, "b" * 64, c.AccountRevision())
        proof = c.TokenCount(399, "tok", "provider", "pinned_provider_tokenizer", "c" * 64)
        for state in ({"bad": 1}, [[0] * 17, [["wrong"] + [0] * 15]], [[0] * 17, [["aave", "text"] + [0] * 14]]):
            raw = c.canonical_bytes(state).decode("ascii")
            with self.subTest(state=state), self.assertRaises(ValueError):
                c.EncodedState(raw, c.content_hash(state), "d" * 64, ("aave",), proof, (snapshot,))
        state = [[1] + [0] * 16, [[1] + [0] * 15]]
        encoded = c.EncodedState(c.canonical_bytes(state).decode("ascii"), c.content_hash(state), "d" * 64, ("aave",), proof, (snapshot,))
        self.assertEqual(c.EncodedState.from_json(encoded.to_json()), encoded)
        with self.assertRaises(ValueError):
            dataclasses.replace(encoded, token_count=dataclasses.replace(proof, state_tokens=400))

    def test_numeric_candidate_mapping_is_fixed_and_exact(self):
        from tests.autonomous.support import encoded_state
        mapping = (("aave", 1), ("hype", 2), ("pendle", 3), ("aero", 4), ("ldo", 5), ("ondo", 6))
        for name, wire_id in mapping:
            with self.subTest(name=name):
                encoded = encoded_state(candidate_id=name, wire_id=wire_id)
                self.assertEqual(c.CANDIDATE_WIRE_IDS, mapping)
                self.assertEqual(c.candidate_wire_id(name), wire_id)
                self.assertNotIn(name, encoded.canonical_state_json)
                self.assertEqual(c.EncodedState.from_json(encoded.to_json()), encoded)
        for wire_id in ("aave", True, 1.0, 0, 2, 7):
            with self.subTest(wire_id=wire_id), self.assertRaises(ValueError):
                encoded_state(wire_id=wire_id)
        with self.assertRaises(ValueError):
            encoded_state(candidate_id="unknown", wire_id=1)

    def test_discrete_wire_slots_have_exact_integer_domains(self):
        from tests.autonomous.support import encoded_state
        for index, invalid_values in ((5, (-1, 0.5, 61, True)), (9, (-1, 0.5, 2, True)),
                                      (12, (-1, 0.5, 64, True))):
            for invalid in invalid_values:
                row = [1] + [0] * 15
                row[index] = invalid
                row[15] = 1 << index
                with self.subTest(index=index, invalid=invalid), self.assertRaises(ValueError):
                    encoded_state(row=row, snapshot_mask=row[15])
        for index, invalid_values in ((0, (1.0, True)), (1, (0.5, -1, True)),
                                      (4, (-2, 0.5, 2, True)), (5, (-2, 0.5, 2, True))):
            for invalid in invalid_values:
                shared = [1] + [0] * 16
                shared[index] = invalid
                shared[16] = 1 << index
                with self.subTest(index=index, invalid=invalid), self.assertRaises(ValueError):
                    encoded_state(shared=shared)

    def test_wire_masks_have_bounded_integer_bit_widths(self):
        from tests.autonomous.support import encoded_state
        for index, width in ((15, 16), (16, 17)):
            for invalid in (-1, 0.5, 1.0, True, 1 << width):
                row, shared = [1] + [0] * 15, [1] + [0] * 16
                (row if index == 15 else shared)[index] = invalid
                with self.subTest(index=index, invalid=invalid), self.assertRaises(ValueError):
                    encoded_state(row=row, shared=shared)
        row = [1] + [0] * 14 + [(1 << 16) - 1]
        shared = [1] + [0] * 15 + [(1 << 17) - 1]
        encoded_state(row=row, shared=shared, snapshot_mask=(1 << 16) - 1)

    def test_invalid_measurements_are_zero_and_snapshot_masks_match(self):
        from tests.autonomous.support import encoded_state
        for index in range(1, 15):
            row = [1] + [0] * 15
            row[index] = 1
            with self.subTest(candidate_slot=index), self.assertRaises(ValueError):
                encoded_state(row=row)
        for index in range(1, 16):
            shared = [1] + [0] * 16
            shared[index] = 1
            with self.subTest(shared_slot=index), self.assertRaises(ValueError):
                encoded_state(shared=shared)
        with self.assertRaises(ValueError):
            encoded_state(row=[1] + [0] * 14 + [2], snapshot_mask=0)
        row = [1] + [0] * 15
        row[5], row[9], row[12] = 60, 1, 63
        row[15] = (1 << 5) | (1 << 9) | (1 << 12)
        encoded_state(row=row, snapshot_mask=row[15])

    def test_continuous_wire_slots_obey_dictionary_ranges(self):
        from tests.autonomous.support import encoded_state
        for index, invalid in ((1, -0.1), (2, -0.1), (3, -1.1), (3, 1.1), (4, -0.1), (13, 1.1)):
            row = [1] + [0] * 15
            row[index], row[15] = invalid, 1 << index
            with self.subTest(index=index, invalid=invalid), self.assertRaises(ValueError):
                encoded_state(row=row, snapshot_mask=row[15])
        for index in (11, 13, 14, 15):
            shared = [1] + [0] * 16
            shared[index], shared[16] = 1.1, 1 << index
            with self.subTest(index=index), self.assertRaises(ValueError):
                encoded_state(shared=shared)

    def test_dictionary_documents_wire_metadata_and_thesis_bit_width(self):
        dictionary = c.default_field_dictionary()
        for field in (dictionary.shared_fields[0], dictionary.shared_fields[16],
                      dictionary.candidate_fields[0], dictionary.candidate_fields[15]):
            self.assertIn("validity_exempt", field.encoding)
        self.assertEqual(dictionary.candidate_fields[12].maximum, 63.0)

    def test_late_batch_requires_rejection_without_decisions(self):
        decision = c.CandidateDecision("aave", c.Regime.TRENDING, c.Direction.LONG, 2, c.AdvisoryRisk.SAFE,
            c.DirectionDistribution(0.9, 0.05, 0.05), 0.9, 0.9, 0.9, 0.9, 0.1)
        with self.assertRaises(ValueError):
            c.DecisionBatch("request", "a" * 64, "b" * 64, "model", 10, 11, 100, (decision,), "c" * 64)
        with self.assertRaises(ValueError):
            c.DecisionBatch("request", "a" * 64, "b" * 64, "model", 10, 11, 100, (), "c" * 64)


if __name__ == "__main__":
    unittest.main()
