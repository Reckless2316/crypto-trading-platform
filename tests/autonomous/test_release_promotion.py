"""Standalone authenticated release validation; no activation or provider traffic."""
import importlib.util
import unittest
from dataclasses import replace

from bot.autonomous.contracts import CandidateBundle, CandidateDefinition, GateApproval, ManifestEntry, ApprovedBundle, content_hash


class ReleaseModuleTests(unittest.TestCase):
    def test_release_validator_exists(self):
        self.assertIsNotNone(importlib.util.find_spec('bot.autonomous.release'))


class ReleasePromotionTests(unittest.TestCase):
    def setUp(self):
        from bot.autonomous import release as r
        self.r = r
        self.now = 100
        self.artifacts = tuple(r.ReleaseArtifact(role, ('real-' + role).encode(), 1000)
            for role in r.ARTIFACT_ROLES)
        hashes = {a.role: a.content_hash for a in self.artifacts}
        self.bundle = CandidateBundle('model-v1', hashes['schema'], hashes['dictionary'], hashes['tokenizer'],
            hashes['policy'], hashes['risk'], hashes['calibrator'], hashes['code'], hashes['evidence'], 900,
            (CandidateDefinition('aave', 'AAVE-USD', False, True),), ())
        self.approvals = tuple(GateApproval('specification' if gate == 'spec' else gate, 'operator:alice',
            self.bundle.bundle_hash, content_hash(('signed-event', gate)), 50) for gate in r.GATES)
        owner = self
        class Verifier(r.ReleaseEvidenceVerifier):
            def verify(self, request):
                return r.VerifiedReleaseEvidence(self, request.request_hash,
                    owner.r.compatibility_manifest(owner.bundle), ('operator:alice',),
                    tuple(content_hash(a) for a in owner.approvals), 90, 1000)
        self.verifier = Verifier()

    def validate(self, **overrides):
        args = dict(bundle=self.bundle, approvals=self.approvals, artifacts=self.artifacts,
                    verifier=self.verifier, now_us=self.now)
        args.update(overrides)
        return self.r.validate_bundle(**args)

    def rejected(self, result, code):
        self.assertIsInstance(result, self.r.BundleRejection)
        self.assertIn(code, result.reason_codes)

    def test_ship_approval_is_required(self):
        self.assertEqual(self.r.missing_approvals(set(self.r.GATES) - {'ship'}), ('ship',))
        self.assertEqual(self.r.missing_approvals({'specification', *self.r.GATES[1:]}), ())

    def test_nonempty_compatible_authenticated_release_is_approved(self):
        result = self.validate()
        self.assertIsInstance(result, ApprovedBundle)
        self.assertEqual(result.bundle_hash, self.bundle.bundle_hash)
        self.assertEqual(len(result.approvals), 6)

    def test_each_required_gate_is_checked(self):
        for omitted in self.approvals:
            with self.subTest(omitted.gate):
                self.rejected(self.validate(approvals=tuple(a for a in self.approvals if a != omitted)), 'MISSING_APPROVALS')

    def test_approvals_cannot_authorize_themselves(self):
        self.rejected(self.validate(verifier=None), 'VERIFIER_UNAVAILABLE')
        self.rejected(self.validate(verifier=lambda request: True), 'VERIFIER_UNAVAILABLE')

    def test_wrong_actor_evidence_or_bundle_hash_is_refused(self):
        for change in ({'actor': 'brain'}, {'evidence_hash': 'f'*64}, {'bundle_hash': 'f'*64}, {'approved_at_us': 101}):
            with self.subTest(change):
                forged = (replace(self.approvals[0], **change), *self.approvals[1:])
                result = self.validate(approvals=forged)
                self.assertIsInstance(result, self.r.BundleRejection)

    def test_each_changed_artifact_is_refused(self):
        for i, artifact in enumerate(self.artifacts):
            with self.subTest(artifact.role):
                altered = list(self.artifacts); altered[i] = replace(artifact, payload=b'changed')
                self.rejected(self.validate(artifacts=tuple(altered)), 'ARTIFACT_IDENTITY_MISMATCH')

    def test_each_missing_artifact_is_refused(self):
        for artifact in self.artifacts:
            with self.subTest(artifact.role):
                self.rejected(self.validate(artifacts=tuple(a for a in self.artifacts if a != artifact)), 'ARTIFACT_SET_MISMATCH')

    def test_each_deployed_identity_must_match(self):
        original = self.bundle
        for field in ('model_id', 'schema_hash', 'dictionary_hash', 'tokenizer_hash', 'policy_hash', 'risk_hash',
                      'calibrator_hash', 'code_hash', 'evidence_manifest_hash'):
            with self.subTest(field):
                # The verifier reports a real but different deployed configuration.
                self.bundle = replace(original, **{field: 'other-model' if field == 'model_id' else 'f'*64})
                self.rejected(self.validate(bundle=original), 'INCOMPATIBLE_ARTIFACTS')
        self.bundle = original

    def test_expiry_equality_and_future_proofs_are_refused(self):
        self.rejected(self.validate(now_us=900), 'BUNDLE_EXPIRED')
        self.rejected(self.validate(artifacts=(replace(self.artifacts[0], expires_at_us=100), *self.artifacts[1:])), 'ARTIFACT_EXPIRED')
        self.rejected(self.validate(now_us=89), 'INVALID_VERIFIED_WINDOW')
        good = self.verifier.verify
        self.verifier.verify = lambda request: replace(good(request), expires_at_us=self.now)
        self.rejected(self.validate(), 'INVALID_VERIFIED_WINDOW')

    def test_expired_candidate_thesis_is_refused(self):
        a = self.r.ReleaseArtifact('candidate-evidence:aave', b'candidate-proof', 100)
        self.bundle = replace(self.bundle, candidates=(CandidateDefinition('aave', 'AAVE-USD', True, False, (), a.content_hash),))
        self.approvals = tuple(replace(x, bundle_hash=self.bundle.bundle_hash) for x in self.approvals)
        self.rejected(self.validate(artifacts=(*self.artifacts, a)), 'ARTIFACT_EXPIRED')

    def test_eligible_candidate_requires_its_exact_evidence(self):
        a = self.r.ReleaseArtifact('candidate-evidence:aave', b'candidate-proof', 1000)
        self.bundle = replace(self.bundle, candidates=(CandidateDefinition('aave', 'AAVE-USD', True, False, (), a.content_hash),))
        self.approvals = tuple(replace(x, bundle_hash=self.bundle.bundle_hash) for x in self.approvals)
        self.rejected(self.validate(), 'ARTIFACT_SET_MISMATCH')
        self.assertIsInstance(self.validate(artifacts=(*self.artifacts, a)), ApprovedBundle)

    def test_evaluation_and_dependency_lock_content_is_verified(self):
        for role, field in (('evaluation:holdout', 'evaluation_manifests'), ('dependency-lock', 'dependency_lock_hash')):
            with self.subTest(role):
                artifact = self.r.ReleaseArtifact(role, b'proof-content', 1000)
                old_bundle, old_approvals = self.bundle, self.approvals
                value = (ManifestEntry('holdout', artifact.content_hash),) if field == 'evaluation_manifests' else artifact.content_hash
                self.bundle = replace(self.bundle, **{field: value})
                self.approvals = tuple(replace(x, bundle_hash=self.bundle.bundle_hash) for x in self.approvals)
                self.rejected(self.validate(), 'ARTIFACT_SET_MISMATCH')
                self.assertIsInstance(self.validate(artifacts=(*self.artifacts, artifact)), ApprovedBundle)
                self.bundle, self.approvals = old_bundle, old_approvals

    def test_embedded_approvals_do_not_create_a_circular_core_hash(self):
        packaged = replace(self.bundle, gate_approvals=self.approvals)
        self.assertNotEqual(packaged.bundle_hash, self.bundle.bundle_hash)
        self.assertEqual(self.r.core_bundle_hash(packaged), self.bundle.bundle_hash)
        self.assertIsInstance(self.validate(bundle=packaged), ApprovedBundle)
        self.assertEqual(self.r.package_hash(packaged, self.approvals), self.r.package_hash(self.bundle, self.approvals))
        altered = (replace(self.approvals[0], evidence_hash='f'*64), *self.approvals[1:])
        self.assertNotEqual(self.r.package_hash(self.bundle, altered), self.r.package_hash(self.bundle, self.approvals))

    def test_duplicate_gate_or_conflicting_embedded_approvals_are_refused(self):
        self.rejected(self.validate(approvals=(*self.approvals, self.approvals[0])), 'INVALID_APPROVALS')
        self.rejected(self.validate(bundle=replace(self.bundle, gate_approvals=self.approvals[:-1])), 'APPROVAL_SET_CONFLICT')

    def test_foreign_issuer_request_and_boolean_proofs_are_refused(self):
        good = self.verifier.verify
        for mutate in (lambda proof: True, lambda proof: replace(proof, issuer=object()),
                       lambda proof: replace(proof, request_hash='f'*64),
                       lambda proof: replace(proof, approval_event_hashes=()),
                       lambda proof: replace(proof, approval_event_hashes=([],)*6),
                       lambda proof: replace(proof, operator_actors=('brain',))):
            with self.subTest(mutate=mutate):
                self.verifier.verify = lambda req, mutate=mutate: mutate(good(req))
                self.assertIsInstance(self.validate(), self.r.BundleRejection)
        self.verifier.verify = good

    def test_declared_bundle_window_cannot_outlive_verified_artifacts(self):
        self.rejected(self.validate(artifacts=(replace(self.artifacts[0], expires_at_us=899), *self.artifacts[1:])),
                      'BUNDLE_OUTLIVES_EVIDENCE')
        good = self.verifier.verify
        self.verifier.verify = lambda request: replace(good(request), expires_at_us=899)
        self.rejected(self.validate(), 'BUNDLE_OUTLIVES_EVIDENCE')

    def test_invalid_times_and_artifact_duplicates_refuse(self):
        self.rejected(self.validate(now_us=True), 'INVALID_INPUT')
        self.rejected(self.validate(artifacts=(*self.artifacts, self.artifacts[0])), 'ARTIFACT_SET_MISMATCH')


if __name__ == '__main__':
    unittest.main()
