"""Research ingestion fixtures are synthetic and are not current measurements."""
from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from dataclasses import replace
from decimal import Decimal
from pathlib import Path

from bot.autonomous.contracts import CandidateDefinition, EvidenceManifest, EvidenceRequest, MetricPayload, content_hash
from research.autonomous.evidence import EvidenceEnvelope, EvidenceSource, EvidenceRegistry, SourceDefinition, decode_verified_observation
from research.autonomous.valuation import CONDITION_NAMES, REQUIRED_EVIDENCE, SCHEMA_CONDITION_MAPPING, evaluate_thesis, missing_evidence


def envelope(**changes):
    raw = b"synthetic fixture only; not a current measurement"
    item = EvidenceEnvelope(
        observation_id="fixture-observation", candidate_id="aave", source_id="synthetic-primary",
        source_url="https://fixture.invalid/primary", primary=True, metric="price", value=Decimal("1"),
        unit="USD/token", cutoff_us=100, event_at_us=90, publication_at_us=95,
        request_started_at_us=96, received_at_us=97, received_at_upper_bound_us=None,
        period_start_us=80, period_end_us=90, method="synthetic", parser_version="fixture-v1",
        raw_path="tests/fixtures/synthetic.raw", raw_bytes=raw,
        raw_sha256=hashlib.sha256(raw).hexdigest(), finality="issuer-published",
        chain_id=None, contract=None, instrument="AAVE-USD", block_height=None, block_hash=None,
        expires_at_us=110, valid=True, invalid_reason=None, claim_status="execution_confirmed",
        adapter_identity="synthetic-fixture-adapter-v1", source_definition_hash="",
    )
    return replace(item, **changes)


class FixtureAdapter:
    adapter_identity = "synthetic-fixture-adapter-v1"
    def __init__(self, items): self.items = items
    def __call__(self, _request): return self.items


class ResearchTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.artifacts = tempfile.TemporaryDirectory()
        self.addCleanup(self.artifacts.cleanup)
        self.registry = EvidenceRegistry(Path(self.artifacts.name), ())

    def _source(self, **kwargs):
        return EvidenceSource(artifact_root=Path(self.artifacts.name), registry=self.registry, **kwargs)

    def _definition(self, item):
        return SourceDefinition(
            source_id=item.source_id, primary=True, allowed_urls=(item.source_url,),
            allowed_metrics=(item.metric,), metric_units={item.metric: (item.unit,)},
            adapter_identity=FixtureAdapter.adapter_identity, max_age_us=20, finality_policy=item.finality, instrument=item.instrument,
            chain_id=item.chain_id, contract=item.contract,
            accounting_max_lag_us=86400 * 1_000_000,
        )

    async def _fetch(self, item):
        definition = self._definition(item)
        bound = replace(item, source_definition_hash=definition.binding_hash)
        source = self._source(adapters={item.source_id: FixtureAdapter((bound,))}, definitions={item.source_id: definition})
        return await source.fetch(EvidenceRequest(bound.candidate_id, bound.source_id, (bound.metric,), bound.cutoff_us))

    async def test_registry_cannot_issue_value_not_derived_from_validated_envelope(self):
        obs = (await self._fetch(envelope()))[0]
        self.assertFalse(hasattr(self.registry, "record"))
        data = json.loads(obs.source_event.stream.split("|", 1)[1])
        val = Decimal("1000")
        payload = MetricPayload(metric=obs.metric, value=val, unit=obs.unit)
        data["observation_id"] = "forged"
        canon = json.dumps(data, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
        h = hashlib.sha256(canon.encode()).hexdigest()
        event = replace(obs.source_event, payload=payload, payload_hash=content_hash(payload), event_id="forged",
                        stream="evidence-v1|" + canon, logical_key=f"forged:{h[:32]}")
        forged = replace(obs, observation_id="forged", value=val, source_event=event, methodology_hash=h)
        item = envelope()
        definition = self._definition(item)
        bound = replace(item, source_definition_hash=definition.binding_hash)
        request = EvidenceRequest(bound.candidate_id, bound.source_id, (bound.metric,), bound.cutoff_us)
        with self.assertRaisesRegex(ValueError, "derived"):
            self.registry._record(((request, bound, forged),))
        self.assertFalse(self.registry.verify(forged))
        self.assertTrue(self.registry.verify(obs))

    def _manifest(self, observations, **changes):
        material = "\n".join(sorted(f"{o.observation_id}:{o.methodology_hash}:{o.source_event.payload_hash}" for o in observations))
        values = dict(candidate_id="aave", observations=observations, methodology_hash=hashlib.sha256(material.encode()).hexdigest(),
                      cutoff_us=100, expires_at_us=200, eligible=True)
        values.update(changes)
        return EvidenceManifest(**values)

    def _candidate(self, manifest, **changes):
        values = dict(candidate_id="aave", instrument="AAVE-USD", eligible=False,
                      condition_names=CONDITION_NAMES["aave"], evidence_manifest_hash=manifest.manifest_hash)
        values.update(changes)
        return CandidateDefinition(**values)

    def test_unknown_required_values_stay_missing_not_zero(self):
        missing = missing_evidence({"price": 1, "token_accrual": None})
        self.assertIn("token_accrual", missing)
        self.assertIn("concentration", missing)
        self.assertIn("forward_supply", missing)

    def test_non_numeric_values_cannot_fill_quantitative_evidence(self):
        for value in ("unknown", {}, [], object(), True):
            self.assertIn("price", missing_evidence({"price": value}))
        self.assertNotIn("revenue", missing_evidence({"revenue": Decimal("-1")}))

    async def test_exact_raw_artifact_is_preserved_without_overwrite(self):
        item = envelope()
        await self._fetch(item)
        artifact = Path(self.artifacts.name) / item.raw_path
        self.assertEqual(artifact.read_bytes(), item.raw_bytes)
        artifact.write_bytes(b"conflicting capture")
        with self.assertRaises(ValueError):
            await self._fetch(item)
        self.assertEqual(artifact.read_bytes(), b"conflicting capture")

    async def test_source_rejects_capture_before_event_publication_or_period_end(self):
        for item in (envelope(event_at_us=98), envelope(publication_at_us=98),
                     envelope(period_end_us=91)):
            with self.assertRaises(ValueError):
                await self._fetch(item)

    async def test_evidence_expiry_cannot_exceed_source_freshness(self):
        with self.assertRaises(ValueError):
            await self._fetch(envelope(expires_at_us=111))

    async def test_instrument_identity_is_pinned_by_source_definition(self):
        item = envelope(instrument="WRONG-USD")
        definition = replace(self._definition(item), instrument="AAVE-USD")
        bound = replace(item, source_definition_hash=definition.binding_hash)
        with self.assertRaises(ValueError):
            await self._source(adapters={item.source_id: FixtureAdapter((bound,))},
                               definitions={item.source_id: definition}).fetch(
                                   EvidenceRequest("aave", item.source_id, ("price",), 100))

    async def test_complete_chain_capture_cannot_switch_chain_contract_or_instrument(self):
        item = envelope(chain_id=1, contract="0x" + "1"*40, block_height=1, block_hash="0x" + "a"*64)
        definition = self._definition(item)
        for changes in ({"chain_id": 2}, {"contract": "0x" + "2"*40}, {"instrument": "WRONG-USD"}):
            altered = replace(item, source_definition_hash=definition.binding_hash, **changes)
            source = self._source(adapters={item.source_id: FixtureAdapter((altered,))}, definitions={item.source_id: definition})
            with self.assertRaises(ValueError):
                await source.fetch(EvidenceRequest("aave", item.source_id, ("price",), 100))

    async def test_registry_replay_requires_trusted_definition_and_untampered_capture(self):
        item = envelope()
        observation = (await self._fetch(item))[0]
        self.assertFalse(EvidenceRegistry(Path(self.artifacts.name), ()).verify(observation))
        replay = EvidenceRegistry(Path(self.artifacts.name), (self._definition(item),))
        self.assertTrue(replay.verify(observation))
        (Path(self.artifacts.name) / item.raw_path).write_bytes(b"tampered capture")
        self.assertFalse(replay.verify(observation))

    async def test_registry_metadata_tamper_and_unsupported_version_reject(self):
        import sqlite3
        observation = (await self._fetch(envelope()))[0]
        with sqlite3.connect(self.registry.database_path) as db:
            db.execute("UPDATE issued SET raw_path='wrong.raw'")
        self.assertFalse(self.registry.verify(observation))
        with sqlite3.connect(self.registry.database_path) as db:
            db.execute("PRAGMA user_version=2")
        with self.assertRaises(ValueError):
            EvidenceRegistry(Path(self.artifacts.name), ())

    def test_existing_unversioned_or_downgraded_registry_rejects(self):
        import sqlite3
        with sqlite3.connect(self.registry.database_path) as db:
            db.execute("PRAGMA user_version=0")
        with self.assertRaises(ValueError):
            EvidenceRegistry(Path(self.artifacts.name), ())

    async def test_offline_protocol_adapter_preserves_capture_and_omits_unknowns(self):
        from research.autonomous.evidence import CapturedProtocolAdapter
        raw = b'{"price": "1", "unit": "USD/token"}'
        item = envelope(raw_bytes=raw, raw_sha256=hashlib.sha256(raw).hexdigest())
        definition = self._definition(item)
        item = replace(item, source_definition_hash=definition.binding_hash)
        def decoder(document):
            return {"price": (Decimal(document["price"]), document["unit"]),
                    "unknown": (None, "USD")}
        adapter = CapturedProtocolAdapter(item, decoder, adapter_identity=definition.adapter_identity)
        source = self._source(adapters={item.source_id: adapter}, definitions={item.source_id: definition})
        observations = await source.fetch(EvidenceRequest("aave", item.source_id, ("price",), 100))
        self.assertEqual(len(observations), 1)
        self.assertTrue(self.registry.verify(observations[0]))
        self.assertEqual((Path(self.artifacts.name) / item.raw_path).read_bytes(), raw)

    def test_rpc_adapter_rejects_duplicate_request_and_response_ids(self):
        from research.autonomous.evidence import CapturedProtocolAdapter
        for ids, raw in (((10, 10), b'[{"id":10,"result":"1"},{"id":10,"result":"2"}]'),
                         ((10, 11), b'[{"id":10,"result":"1"},{"id":10,"result":"2"}]')):
            item = envelope(raw_bytes=raw, raw_sha256=hashlib.sha256(raw).hexdigest())
            adapter = CapturedProtocolAdapter(item, lambda _: {}, adapter_identity="fixture", rpc_request_ids=ids)
            with self.assertRaises(ValueError):
                adapter(EvidenceRequest("aave", item.source_id, ("price",), 100))

    async def _complete_admission(self, claim_status_for=None, **changes):
        from research.autonomous.valuation import ADMISSION_UNITS, thesis_definition_hash, bear_estimate_subject_hash
        values = {name: Decimal("1") for name in ADMISSION_UNITS}
        values.update(price=Decimal("1"), bear_case_value=Decimal("2"),
                      execution_costs=Decimal("0.01"), concentration=Decimal("0.2"),
                      circulating_supply=Decimal("10"), total_supply=Decimal("20"),
                      fdv_supply=Decimal("20"), market_cap=Decimal("10"), fdv=Decimal("20"))
        values.update(changes)
        observations = []
        cutoff = 100 * 86400 * 1_000_000
        for name, value in values.items():
            if value is None:
                continue
            window = 90 if name in {"revenue", "fees", "incentives", "token_accrual"} else 30 if name == "active_users" else None
            subject = None
            if name == "thesis_approval":
                subject = thesis_definition_hash(CandidateDefinition(candidate_id="aave", instrument="AAVE-USD", condition_names=CONDITION_NAMES["aave"]))
            if name == "bear_model_approval":
                estimates = [item for item in observations if item.metric == "bear_case_value"]
                subject = bear_estimate_subject_hash(estimates[0]) if estimates else None
            observations.extend(await self._fetch(envelope(observation_id="proof-" + name,
                metric=name, unit=ADMISSION_UNITS[name], value=value, cutoff_us=cutoff,
                claim_status=(claim_status_for or {}).get(name, "estimate" if name == "bear_case_value" else "execution_confirmed"),
                event_at_us=cutoff-10, publication_at_us=cutoff-5, request_started_at_us=cutoff-4,
                received_at_us=cutoff-3, expires_at_us=cutoff+10,
                approval_subject_hash=subject, model_version="synthetic-bear-v1" if name == "bear_case_value" else None,
                accounting_basis="cash" if window else None,
                forecast_start_us=cutoff if name == "forward_supply" else None,
                forecast_end_us=cutoff+90*86400*1_000_000 if name == "forward_supply" else None,
                forecast_coverage_complete=name == "forward_supply",
                period_start_us=(cutoff-10-window*86400*1_000_000) if window else None,
                period_end_us=(cutoff-10) if window else None)))
        manifest = self._manifest(tuple(observations), cutoff_us=cutoff, expires_at_us=cutoff+10)
        return self._candidate(manifest, eligible=True), manifest

    async def test_complete_verified_synthetic_admission_and_canonical_bits(self):
        candidate, manifest = await self._complete_admission()
        result = evaluate_thesis(candidate, manifest, registry=self.registry)
        self.assertTrue(result.eligible, result.reason_codes)
        self.assertEqual(result.condition_bits, (1 << len(CONDITION_NAMES["aave"])) - 1)

    async def test_each_independent_admission_gate_is_required(self):
        for name in ("thesis_approval", "bear_case_value", "contradiction_clear", "fdv_supply_convention_verified",
                     "listing_verified", "executable_depth_usd", "bear_model_approval", *CONDITION_NAMES["aave"]):
            candidate, manifest = await self._complete_admission(**{name: None})
            self.assertFalse(evaluate_thesis(candidate, manifest, registry=self.registry).eligible, name)

    async def test_expired_approval_contradiction_and_costs_over_discount_reject(self):
        for changes in ({"thesis_approval": Decimal("0")}, {"contradiction_clear": Decimal("0")},
                        {"bear_case_value": Decimal("0.9")}, {"execution_costs": Decimal("0.6")},
                        {"executable_depth_usd": Decimal("0")}):
            candidate, manifest = await self._complete_admission(**changes)
            self.assertFalse(evaluate_thesis(candidate, manifest, registry=self.registry).eligible, changes)

    async def test_fully_qualified_record_with_unconfirmed_claim_status_rejects(self):
        for status in ("mechanism_confirmed", "proposal", "estimate", "speculation"):
            candidate, manifest = await self._complete_admission(claim_status_for={"revenue": status})
            result = evaluate_thesis(candidate, manifest, registry=self.registry)
            self.assertIn("unconfirmed:revenue", result.reason_codes, status)
            self.assertFalse(result.eligible)
        candidate, manifest = await self._complete_admission(claim_status_for={"bear_case_value": "execution_confirmed"})
        self.assertIn("unconfirmed:bear_case_value", evaluate_thesis(candidate, manifest, registry=self.registry).reason_codes)

    async def test_fdv_supply_convention_must_be_verified_true(self):
        candidate, manifest = await self._complete_admission(fdv_supply_convention_verified=Decimal("0"))
        result = evaluate_thesis(candidate, manifest, registry=self.registry)
        self.assertIn("gate_not_verified:fdv_supply_convention_verified", result.reason_codes)
        self.assertFalse(result.eligible)

    def test_rpc_single_request_rejects_duplicated_response(self):
        from research.autonomous.evidence import CapturedProtocolAdapter
        raw = b'[{"id":10,"result":"1"},{"id":10,"result":"1"}]'
        item = envelope(raw_bytes=raw, raw_sha256=hashlib.sha256(raw).hexdigest())
        adapter = CapturedProtocolAdapter(item, lambda _: {}, adapter_identity="fixture", rpc_request_ids=(10,))
        with self.assertRaisesRegex(ValueError, "RPC"):
            adapter(EvidenceRequest("aave", item.source_id, ("price",), 100))

    def test_edited_ticker_capture_bytes_reject(self):
        from research.autonomous.evidence import decode_candidate_capture
        root = Path(__file__).resolve().parents[2]/"docs/research/2026-10-17-autonomous-agent/research-evidence"
        raw = (root/"aave_ticker.raw").read_bytes()
        document = json.loads(raw)
        document["price"] = str(Decimal(document["price"]) * 2)
        edited = json.dumps(document).encode()
        with self.assertRaises(ValueError):
            decode_candidate_capture("aave", edited, format_version="coinbase-ticker-v1", unit="USD/token")

    async def test_assessment_rechecks_expiry_at_explicit_evaluation_time(self):
        candidate, manifest = await self._complete_admission()
        self.assertFalse(evaluate_thesis(candidate, manifest, registry=self.registry, evaluated_at_us=manifest.expires_at_us+1).eligible)

    async def test_cashflow_window_cannot_be_relabelled_as_ninety_days(self):
        candidate, manifest = await self._complete_admission()
        revenues = next(item for item in manifest.observations if item.metric == "revenue")
        raw = json.loads(revenues.source_event.stream.split("|", 1)[1])
        bad = envelope(observation_id="wrong-window", metric="revenue", value=Decimal("1"), unit="USD/90d",
            cutoff_us=manifest.cutoff_us, event_at_us=manifest.cutoff_us-10,
            publication_at_us=manifest.cutoff_us-5, request_started_at_us=manifest.cutoff_us-4,
            received_at_us=manifest.cutoff_us-3, expires_at_us=manifest.cutoff_us+10,
            period_start_us=manifest.cutoff_us-20, period_end_us=manifest.cutoff_us-10)
        replacement = (await self._fetch(bad))[0]
        updated = self._manifest(tuple(replacement if item.metric == "revenue" else item for item in manifest.observations),
            cutoff_us=manifest.cutoff_us, expires_at_us=manifest.expires_at_us)
        result = evaluate_thesis(self._candidate(updated, eligible=True), updated, registry=self.registry)
        self.assertFalse(result.eligible)
        self.assertIn("window_mismatch:revenue", result.reason_codes)

    async def test_same_duration_shifted_cashflow_interval_rejects(self):
        candidate, manifest = await self._complete_admission()
        original = next(item for item in manifest.observations if item.metric == "revenue")
        metadata = json.loads(original.source_event.stream.split("|", 1)[1])
        replacement = (await self._fetch(envelope(observation_id="shifted-revenue", metric="revenue", value=original.value,
            unit=original.unit, cutoff_us=manifest.cutoff_us, event_at_us=manifest.cutoff_us-10,
            received_at_us=manifest.cutoff_us-3, publication_at_us=manifest.cutoff_us-5,
            request_started_at_us=manifest.cutoff_us-4, expires_at_us=manifest.cutoff_us+10,
            period_start_us=metadata["period_start_us"]-1, period_end_us=metadata["period_end_us"]-1,
            accounting_basis="cash")))[0]
        updated = self._manifest(tuple(replacement if item.metric == "revenue" else item for item in manifest.observations),
            cutoff_us=manifest.cutoff_us, expires_at_us=manifest.expires_at_us)
        result = evaluate_thesis(self._candidate(updated, eligible=True), updated, registry=self.registry)
        self.assertIn("cashflow_intervals_incompatible", result.reason_codes)
        self.assertFalse(result.eligible)

    async def _replace_proofs(self, manifest, changes):
        updated = []
        for original in manifest.observations:
            if original.metric not in changes:
                updated.append(original)
                continue
            metadata = json.loads(original.source_event.stream.split("|", 1)[1])
            item = EvidenceEnvelope(**metadata, value=original.value,
                raw_bytes=(Path(self.artifacts.name)/metadata["raw_path"]).read_bytes())
            updated.extend(await self._fetch(replace(item, **changes[original.metric])))
        return self._manifest(tuple(updated), cutoff_us=manifest.cutoff_us, expires_at_us=manifest.expires_at_us)

    async def test_forward_supply_requires_exact_typed_ninety_day_coverage(self):
        _, manifest = await self._complete_admission()
        for changes in ({"forecast_coverage_complete": False}, {"forecast_start_us": None, "forecast_end_us": None},
                        {"forecast_end_us": manifest.cutoff_us+30*86400*1_000_000}):
            updated = await self._replace_proofs(manifest, {"forward_supply": changes})
            result = evaluate_thesis(self._candidate(updated, eligible=True), updated, registry=self.registry)
            self.assertFalse(result.eligible)
            self.assertIn("forward_supply_coverage_unproven", result.reason_codes)

    async def test_approval_subject_substitution_is_rejected(self):
        _, manifest = await self._complete_admission()
        for metric in ("thesis_approval", "bear_model_approval"):
            updated = await self._replace_proofs(manifest, {metric: {"approval_subject_hash": "f"*64}})
            result = evaluate_thesis(self._candidate(updated, eligible=True), updated, registry=self.registry)
            self.assertFalse(result.eligible)
            self.assertIn(metric + "_subject_mismatch", result.reason_codes)
        updated = await self._replace_proofs(manifest, {"bear_case_value": {"model_version": "substituted-bear-v2"}})
        self.assertFalse(evaluate_thesis(self._candidate(updated, eligible=True), updated, registry=self.registry).eligible)

    async def test_fresh_publication_cannot_refresh_old_accounting_period(self):
        _, manifest = await self._complete_admission()
        changes = {}
        for original in manifest.observations:
            if original.metric in {"revenue", "fees", "incentives", "token_accrual"}:
                metadata = json.loads(original.source_event.stream.split("|", 1)[1])
                changes[original.metric] = {name: metadata[name]-2*86400*1_000_000 for name in ("period_start_us", "period_end_us")}
        updated = await self._replace_proofs(manifest, changes)
        result = evaluate_thesis(self._candidate(updated, eligible=True), updated, registry=self.registry)
        self.assertFalse(result.eligible)
        self.assertIn("accounting_period_stale:revenue", result.reason_codes)

    def test_six_static_protocol_document_decoders_keep_current_metrics_unknown(self):
        from research.autonomous.evidence import decode_candidate_capture
        files = {"aave": "aave_docs.raw", "hype": "hype_fees_docs.raw", "pendle": "pendle_tokenomics.raw",
                 "aero": "aero_contract_spec.raw", "ldo": "lido_h1_2026.raw", "ondo": "ondo_docs.raw"}
        root = Path(__file__).resolve().parents[2]/"docs/research/2026-10-17-autonomous-agent/research-evidence"
        for candidate, filename in files.items():
            raw = (root/filename).read_bytes()
            result = decode_candidate_capture(candidate, raw, format_version="official-document-20261017-v1")
            self.assertTrue(all(value is None for value in result.metrics.values()))
            self.assertTrue(result.disclosures)
            with self.assertRaises(ValueError):
                decode_candidate_capture(candidate, raw+b"changed", format_version="official-document-20261017-v1")

    def test_six_concrete_ticker_decoders_reject_unknown_versions_and_wrong_units(self):
        from research.autonomous.evidence import decode_candidate_capture
        root = Path(__file__).resolve().parents[2]/"docs/research/2026-10-17-autonomous-agent/research-evidence"
        for candidate in CONDITION_NAMES:
            raw = (root/f"{candidate}_ticker.raw").read_bytes()
            result = decode_candidate_capture(candidate, raw, format_version="coinbase-ticker-v1", unit="USD/token")
            self.assertGreater(result.metrics["price"], 0)
            self.assertIsNone(result.metrics["total_supply"])
            with self.assertRaises(ValueError):
                decode_candidate_capture(candidate, raw, format_version="coinbase-ticker-v2", unit="USD/token")
            with self.assertRaises(ValueError):
                decode_candidate_capture(candidate, raw, format_version="coinbase-ticker-v1", unit="ETH/token")

    def test_pendle_epoch_decoder_preserves_unqualified_units_and_coverage(self):
        from research.autonomous.evidence import decode_candidate_capture
        raw = (Path(__file__).resolve().parents[2]/"docs/research/2026-10-17-autonomous-agent/research-deep/pendle_api_spendle.raw").read_bytes()
        result = decode_candidate_capture("pendle", raw, format_version="pendle-spendle-epochs-v1")
        self.assertIsNone(result.metrics["fees"])
        self.assertIsNone(result.metrics["revenue"])
        self.assertIn("epoch_units_and_settlement_unqualified", result.disclosures)

    def test_supply_reconciles_exclusions_and_unlocks_without_claiming_reserve_burn(self):
        from research.autonomous.evidence import SupplyEvent, normalize_supply
        events = (SupplyEvent(Decimal("3"), 120, "unlock", True, True),
                  SupplyEvent(Decimal("4"), 120, "reserve_purchase", True, False),
                  SupplyEvent(Decimal("5"), 100, "unlock", True, True))
        result = normalize_supply(total=Decimal("100"), excluded={"treasury": Decimal("20")},
            required_exclusions=("treasury",), events=events, cutoff_us=100, horizon_us=90*86400*1_000_000,
            unit="token", complete=True)
        self.assertEqual(result["circulating_supply"], Decimal("80"))
        # An unlock effective exactly at the cutoff is still sellable forward supply.
        self.assertEqual(result["forward_supply"], Decimal("8"))
        short = normalize_supply(total=Decimal("100"), excluded={"treasury": Decimal("20")},
            required_exclusions=("treasury",), events=events, cutoff_us=100, horizon_us=30,
            unit="token", complete=True)
        self.assertIsNone(short["forward_supply"])
        self.assertEqual(result["total_supply"], Decimal("100"))
        self.assertIsNone(normalize_supply(total=Decimal("100"), excluded={},
            required_exclusions=("treasury",), events=(), cutoff_us=100, horizon_us=30,
            unit="token", complete=False)["forward_supply"])

    def test_cashflow_does_not_add_points_or_mislabel_gross_fees_as_revenue(self):
        from research.autonomous.evidence import normalize_cashflow
        result = normalize_cashflow(gross_fees=Decimal("100"), rebates=Decimal("10"),
            referrals=Decimal("5"), incentives=Decimal("20"), expenses=Decimal("15"),
            bad_debt=Decimal("3"), attributable_cash=Decimal("4"),
            token_entitlement_verified=True, unit="USD", complete=True)
        self.assertEqual(result["revenue"], Decimal("47"))
        self.assertEqual(result["fees"], Decimal("100"))
        unknown = normalize_cashflow(gross_fees=Decimal("100"), rebates=None,
            referrals=Decimal("0"), incentives=Decimal("0"), expenses=Decimal("0"),
            bad_debt=Decimal("0"), attributable_cash=Decimal("0"),
            token_entitlement_verified=False, unit="points", complete=True)
        self.assertTrue(all(value is None for value in unknown.values()))

    def test_tvl_requires_usd_marks_and_deduplicated_claims_at_one_cutoff(self):
        from research.autonomous.evidence import AssetClaim, normalize_tvl
        claim = AssetClaim("ETH", "deposit-1", Decimal("2"), Decimal("3"), "USD", 100)
        self.assertEqual(normalize_tvl((claim,), cutoff_us=100, complete=True), Decimal("6"))
        self.assertIsNone(normalize_tvl((claim, claim), cutoff_us=100, complete=True))
        self.assertIsNone(normalize_tvl((replace(claim, quote_unit="ETH"),), cutoff_us=100, complete=True))
        self.assertIsNone(normalize_tvl((), cutoff_us=100, complete=True))

    def test_holder_clusters_require_verified_beneficial_ownership_not_wallet_counts(self):
        from research.autonomous.evidence import HolderCluster, normalize_concentration
        owners = (HolderCluster("wallet1", Decimal("6"), "beneficial", "entity1", "proof1"),
                  HolderCluster("wallet2", Decimal("4"), "beneficial", "entity1", "proof2"))
        self.assertEqual(normalize_concentration(owners, supply=Decimal("10"), complete=True), Decimal("1"))
        custody = (replace(owners[0], category="custody"), owners[1])
        self.assertIsNone(normalize_concentration(custody, supply=Decimal("10"), complete=True))

    async def test_chain_evidence_requires_complete_block_identity(self):
        for item in (envelope(chain_id=1), envelope(block_height=1),
                     envelope(block_hash="not-a-hash")):
            with self.assertRaises(ValueError):
                await self._fetch(item)

    async def test_decoder_returns_reason_for_noninteger_receipt_not_type_error(self):
        from research.autonomous.evidence import _canonical
        obs = (await self._fetch(envelope()))[0]
        data = json.loads(obs.source_event.stream.split("|", 1)[1])
        data["received_at_us"] = "97"
        encoded = _canonical(data)
        digest = hashlib.sha256(encoded.encode()).hexdigest()
        event = replace(obs.source_event, stream="evidence-v1|" + encoded,
                        logical_key=f"{data['observation_id']}:{digest[:32]}")
        _, error = decode_verified_observation(replace(obs, source_event=event, methodology_hash=digest))
        self.assertEqual(error, "untrusted_observation_times")

    async def test_raw_artifact_parent_symlink_cannot_escape_storage(self):
        with tempfile.TemporaryDirectory() as outside:
            (Path(self.artifacts.name) / "tests").symlink_to(outside, target_is_directory=True)
            with self.assertRaises(OSError):
                await self._fetch(envelope())
            self.assertEqual(list(Path(outside).iterdir()), [])

    async def test_decoder_rejects_noncausal_or_invalid_nullable_times(self):
        from research.autonomous.evidence import _canonical
        obs = (await self._fetch(envelope()))[0]
        for field, value in (("publication_at_us", "95"), ("publication_at_us", 98),
                             ("request_started_at_us", 98), ("period_end_us", 91),
                             ("period_start_us", None)):
            data = json.loads(obs.source_event.stream.split("|", 1)[1])
            data[field] = value
            encoded = _canonical(data)
            digest = hashlib.sha256(encoded.encode()).hexdigest()
            event = replace(obs.source_event, stream="evidence-v1|" + encoded,
                            logical_key=f"{data['observation_id']}:{digest[:32]}")
            _, error = decode_verified_observation(replace(obs, source_event=event, methodology_hash=digest))
            self.assertIsNotNone(error, field)

    async def test_source_binds_local_source_definition_and_adapter_identity(self):
        item = envelope()
        definition = self._definition(item)
        bound = replace(item, source_definition_hash=definition.binding_hash)
        adapter = FixtureAdapter((bound,))
        source = self._source(adapters={item.source_id: adapter}, definitions={item.source_id: definition})
        obs = await source.fetch(EvidenceRequest("aave", item.source_id, ("price",), 100))
        self.assertEqual(len(obs), 1)
        source.adapters[item.source_id] = lambda _request: (bound,)
        with self.assertRaises(ValueError):
            await source.fetch(EvidenceRequest("aave", item.source_id, ("price",), 100))

    async def test_source_rejects_secondary_unknown_receipt_and_bad_hash(self):
        item = envelope(primary=False)
        definition = self._definition(item)
        source = self._source(adapters={item.source_id: FixtureAdapter((item,))}, definitions={item.source_id: definition})
        with self.assertRaises(ValueError): await source.fetch(EvidenceRequest("aave", item.source_id, ("price",), 100))
        item = envelope(received_at_us=None)
        definition = self._definition(item)
        item = replace(item, source_definition_hash=definition.binding_hash)
        source = self._source(adapters={item.source_id: FixtureAdapter((item,))}, definitions={item.source_id: definition})
        with self.assertRaises(ValueError): await source.fetch(EvidenceRequest("aave", item.source_id, ("price",), 100))
        item = envelope(raw_sha256="0" * 64)
        definition = self._definition(item)
        item = replace(item, source_definition_hash=definition.binding_hash)
        source = self._source(adapters={item.source_id: FixtureAdapter((item,))}, definitions={item.source_id: definition})
        with self.assertRaises(ValueError): await source.fetch(EvidenceRequest("aave", item.source_id, ("price",), 100))

    async def test_exact_integer_period_and_receipt_bounds_reject_bool_and_float(self):
        for field, value in (("period_start_us", True), ("period_end_us", 90.0), ("received_at_upper_bound_us", 99.0)):
            item = envelope(**{field: value})
            definition = self._definition(item)
            item = replace(item, source_definition_hash=definition.binding_hash)
            src = self._source(adapters={item.source_id: FixtureAdapter((item,))}, definitions={item.source_id: definition})
            with self.assertRaises(ValueError): await src.fetch(EvidenceRequest("aave", item.source_id, ("price",), 100))

    async def test_decoder_rejects_bool_and_float_period_and_receipt_bounds(self):
        import json
        from research.autonomous.evidence import _canonical
        obs = (await self._fetch(envelope()))[0]
        for field, value in (("period_start_us", True), ("period_end_us", 90.0), ("received_at_upper_bound_us", 99.0)):
            data = json.loads(obs.source_event.stream.split("|", 1)[1])
            data[field] = value
            encoded = _canonical(data)
            digest = hashlib.sha256(encoded.encode()).hexdigest()
            event = replace(obs.source_event, stream="evidence-v1|" + encoded,
                            logical_key=f"{data['observation_id']}:{digest[:32]}")
            forged = replace(obs, source_event=event, methodology_hash=digest)
            _, error = decode_verified_observation(forged)
            self.assertEqual(error, "untrusted_observation_times")

    async def test_stale_future_unit_mismatch_and_source_outage_fail_closed(self):
        for bad in (envelope(event_at_us=50), envelope(event_at_us=101), envelope(unit="tokens")):
            definition = self._definition(envelope())
            bound = replace(bad, source_definition_hash=definition.binding_hash)
            src = self._source(adapters={bad.source_id: FixtureAdapter((bound,))}, definitions={bad.source_id: definition})
            with self.assertRaises(ValueError): await src.fetch(EvidenceRequest("aave", bad.source_id, ("price",), 100))
        item = envelope()
        definition = self._definition(item)
        src = self._source(adapters={item.source_id: FixtureAdapter(())}, definitions={item.source_id: definition})
        self.assertEqual(await src.fetch(EvidenceRequest("aave", item.source_id, ("price",), 100)), ())

    async def test_mechanism_disclosure_does_not_satisfy_quantitative_forward_supply(self):
        item = envelope(metric="forward_supply", claim_status="mechanism_confirmed")
        obs = await self._fetch(item)
        manifest = self._manifest(obs)
        result = evaluate_thesis(self._candidate(manifest), manifest, registry=self.registry)
        self.assertIn("missing:forward_supply", result.reason_codes)
        self.assertFalse(result.eligible)

    async def test_metadata_finality_instrument_and_block_mismatch_is_rejected(self):
        observation = (await self._fetch(envelope()))[0]
        variants = (
            replace(observation, finality_policy_hash="f" * 64),
            replace(observation, source_event=replace(observation.source_event, instrument="OTHER")),
            replace(observation, source_event=replace(observation.source_event, sequence=55)),
        )
        for forged in variants:
            manifest = self._manifest((forged,))
            result = evaluate_thesis(self._candidate(manifest), manifest, registry=self.registry)
            self.assertEqual(result.condition_bits, 0)
            self.assertTrue(any("untrusted" in reason for reason in result.reason_codes))

    async def test_mutated_issued_observation_value_is_not_trusted(self):
        observation = (await self._fetch(envelope()))[0]
        zero_payload = MetricPayload(metric=observation.metric, value=Decimal("0"), unit=observation.unit)
        zero_event = replace(observation.source_event, payload=zero_payload, payload_hash=content_hash(zero_payload))
        forged = replace(observation, value=Decimal("0"), source_event=zero_event)
        manifest = self._manifest((forged,))
        result = evaluate_thesis(self._candidate(manifest), manifest, registry=self.registry)
        self.assertIn("untrusted_observation_source_binding", result.reason_codes)
        self.assertEqual(result.condition_bits, 0)

    async def test_condition_bits_are_rubric_slots_and_hash_links_manifest(self):
        name = CONDITION_NAMES["aave"][0]
        item = envelope(metric=name, value=Decimal("0"))
        obs = await self._fetch(item)
        manifest = self._manifest(obs)
        # Candidate name is correct, but execution of a text rubric cannot
        # replace fourteen quantitative evidence requirements or approval.
        candidate = self._candidate(manifest)
        result = evaluate_thesis(candidate, manifest, registry=self.registry)
        self.assertEqual(result.condition_bits, 0)
        self.assertLessEqual(result.condition_bits, 63)
        self.assertEqual(result.evidence_manifest_hash, manifest.manifest_hash)
        self.assertFalse(result.eligible)

    def test_mapping_inputs_and_ad_hoc_candidate_rubrics_are_rejected(self):
        manifest = self._manifest((), eligible=False)
        with self.assertRaises(TypeError): evaluate_thesis({"candidate_id": "aave"}, manifest)
        result = evaluate_thesis(CandidateDefinition(candidate_id="aave", instrument="AAVE-USD",
            condition_names=("caller_claims_all_conditions",)), manifest)
        self.assertEqual(result.condition_bits, 0)
        self.assertFalse(result.eligible)

    async def test_assessment_expiry_is_bounded_by_manifest_and_observations(self):
        obs = await self._fetch(envelope(expires_at_us=105))
        manifest = self._manifest(obs, expires_at_us=180)
        result = evaluate_thesis(self._candidate(manifest), manifest, registry=self.registry)
        self.assertEqual(result.expires_at_us, 105)

    def test_six_schemas_have_exact_model_rubrics_and_no_admission_boolean(self):
        schema_dir = Path(__file__).resolve().parents[2] / "config" / "autonomous" / "schemas"
        expected = set(CONDITION_NAMES)
        schemas = {p.stem: json.loads(p.read_text()) for p in schema_dir.glob("*.json")}
        self.assertEqual(set(schemas), expected)
        for name, schema in schemas.items():
            props = schema["properties"]
            self.assertEqual(props["candidate_id"]["const"], name)
            self.assertEqual(schema["x-canonical-rubric-mapping"], "unmapped_inert_research_rows")
            self.assertEqual(schema["x-canonical-thesis-clauses"], list(CONDITION_NAMES[name]))
            self.assertEqual(schema["x-production-admission"], "disabled_pending_verified_evidence_and_release")
            condition_ids = props["conditions"]["items"]["properties"]["condition_id"]["enum"]
            self.assertEqual(set(SCHEMA_CONDITION_MAPPING[name]), set(condition_ids))
            self.assertTrue(all(value is None for value in SCHEMA_CONDITION_MAPPING[name].values()))
            self.assertNotIn("eligible", props)
            rubric = props["decision_rubric"]["properties"]
            self.assertEqual(rubric["regime"]["enum"], ["trending", "mean_reverting", "high_vol", "crisis"])
            self.assertEqual(rubric["direction"]["enum"], ["long", "short", "neutral"])
            self.assertEqual(rubric["setup_quality"]["enum"], [0, 1, 2, 3])
            self.assertEqual(rubric["risk_state"]["enum"], ["safe", "near_limit", "reduce"])
            self.assertEqual(props["required_evidence"]["const"], list(REQUIRED_EVIDENCE))


if __name__ == "__main__": unittest.main()
