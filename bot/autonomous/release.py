"""Standalone immutable release validation and a pure activation planner.

This module never arms, writes the ledger or swaps a live bundle. `plan_activation`
returns the exact transition the authority must commit atomically at the boundary.

HANDOFF(claude-opus-5.5 2026-10-18): the ledger registry (Ledger.register_release /
schedule_activation / apply_due_activation, append-only control_events + release_events)
and the operator-only control `activate_release` handler now commit plans at their hourly
boundary; entries consult `entries_allowed` (Ledger.entries_allowed_now). Still open: a
production ReleaseEvidenceVerifier and trusted release catalog (authenticated operator
approval events), migration of conversational gate approvals into operator-reviewed
release evidence at Ship, and invalidated judgments are recorded but not yet rejected by
id at the decision layer (they fail naturally: their release is no longer active).

Approval records are claims until authenticated by an authority-injected verifier.
The verifier is trusted process wiring, never a JSON/provider supplied dependency.
Core identity excludes embedded approvals; package identity includes their exact
content. ApprovedBundle carries the approval-free core so existing hash contracts
remain usable without a circular approval hash.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, replace
from hashlib import sha256
import re

from .contracts import ApprovedBundle, CandidateBundle, GateApproval, ManifestEntry, content_hash
from .control import ControlSession

GATES = ('spec', 'architecture', 'plan', 'build', 'review', 'ship')
ARTIFACT_ROLES = ('schema', 'dictionary', 'tokenizer', 'policy', 'risk', 'calibrator', 'code', 'evidence')
_HASH = re.compile(r'[0-9a-f]{64}')
HOUR_US = 3_600_000_000
# Process principals that produce proposals/judgments. They can never approve a
# gate, whatever a verifier reports: a generated bundle cannot approve itself.
_ROLE_PRINCIPALS = ('brain', 'reflex', 'reader', 'nightly', 'system', 'authority')


def _role_principal(actor):
    return type(actor) is not str or any(actor == p or actor.startswith(p + ':') for p in _ROLE_PRINCIPALS)


def missing_approvals(approved: set[str]) -> tuple[str, ...]:
    names = set(approved)
    if 'specification' in names:
        names.add('spec')
    return tuple(gate for gate in GATES if gate not in names)


def _gate(name):
    return 'spec' if name == 'specification' else name


def _core(bundle: CandidateBundle) -> CandidateBundle:
    if type(bundle) is not CandidateBundle:
        raise ValueError('typed candidate bundle required')
    return replace(bundle, gate_approvals=())


def core_bundle_hash(bundle: CandidateBundle) -> str:
    return _core(bundle).bundle_hash


def _ordered(approvals):
    return tuple(sorted(approvals, key=lambda a: (a.gate, a.actor, a.evidence_hash, a.approved_at_us)))


def package_hash(bundle: CandidateBundle, approvals: tuple[GateApproval, ...]) -> str:
    """Content identity for the approval-free core plus exact approval records."""
    return content_hash(('release-package-v1', core_bundle_hash(bundle), _ordered(approvals)))


@dataclass(frozen=True)
class ReleaseArtifact:
    """Immutable artifact bytes and verifier-authenticated validity metadata.

    Identity is SHA256 of exact bytes (canonical JSON bytes for JSON artifacts).
    The trusted verifier must authenticate expiry and check semantic compatibility,
    not merely confirm that these bytes hash to the submitted identity.
    """
    role: str
    payload: bytes
    expires_at_us: int

    @property
    def content_hash(self) -> str:
        return sha256(self.payload).hexdigest()

    @property
    def manifest_hash(self) -> str:
        return content_hash((self.role, self.content_hash, self.expires_at_us))


def compatibility_manifest(bundle: CandidateBundle) -> tuple[ManifestEntry, ...]:
    """Exact identities the verifier must prove compatible with deployed workers."""
    return (ManifestEntry('model', content_hash(bundle.model_id)), *(
        ManifestEntry(role, getattr(bundle, 'evidence_manifest_hash' if role == 'evidence' else role + '_hash'))
        for role in ARTIFACT_ROLES), *(
        (ManifestEntry('dependency-lock', bundle.dependency_lock_hash),) if bundle.dependency_lock_hash else ()))


@dataclass(frozen=True)
class ReleaseVerificationRequest:
    bundle: CandidateBundle
    approvals: tuple[GateApproval, ...]
    artifacts: tuple[ReleaseArtifact, ...]
    as_of_us: int

    @property
    def request_hash(self) -> str:
        manifests = tuple(sorted((artifact.role, artifact.manifest_hash) for artifact in self.artifacts))
        return content_hash(('release-verification-v1', package_hash(self.bundle, self.approvals), manifests, self.as_of_us))


class ReleaseEvidenceVerifier(ABC):
    """Trusted local verifier of immutable artifacts and authenticated operator events.

    Implementations must independently authenticate approval-event provenance,
    actors and exact GateApproval content; load artifact bytes/expiry from trusted
    records; and verify model/schema/dictionary/tokenizer/calibration compatibility
    with deployed code/policy/risk (and dependency lock when named). A model response,
    a claimed actor, a caller boolean, or content hashes alone are never evidence.
    This module supplies no permissive production verifier. Code already executing
    within authority is outside the untrusted proposal boundary.
    """
    @abstractmethod
    def verify(self, request: ReleaseVerificationRequest) -> VerifiedReleaseEvidence | None:
        raise NotImplementedError


@dataclass(frozen=True)
class VerifiedReleaseEvidence:
    issuer: ReleaseEvidenceVerifier
    request_hash: str
    compatible_artifacts: tuple[ManifestEntry, ...]
    operator_actors: tuple[str, ...]
    approval_event_hashes: tuple[str, ...]
    verified_at_us: int
    expires_at_us: int


@dataclass(frozen=True)
class BundleRejection:
    reason_codes: tuple[str, ...]


def _expected_artifacts(bundle):
    entries = {role: getattr(bundle, 'evidence_manifest_hash' if role == 'evidence' else role + '_hash')
               for role in ARTIFACT_ROLES}
    if bundle.dependency_lock_hash is not None:
        entries['dependency-lock'] = bundle.dependency_lock_hash
    for candidate in bundle.candidates:
        if candidate.evidence_manifest_hash is not None:
            entries['candidate-evidence:' + candidate.candidate_id] = candidate.evidence_manifest_hash
    for evaluation in bundle.evaluation_manifests:
        role = 'evaluation:' + evaluation.record_id
        if role in entries:
            raise ValueError('duplicate evaluation identity')
        entries[role] = evaluation.content_hash
    return entries


def validate_bundle(bundle: CandidateBundle, approvals: tuple[GateApproval, ...], *,
                    artifacts: tuple[ReleaseArtifact, ...] = (),
                    verifier: ReleaseEvidenceVerifier | None = None,
                    now_us: int) -> ApprovedBundle | BundleRejection:
    """Authenticate all six gates and exact compatible artifacts without side effects.

    Revalidate through this function at activation time; a constructible/deserialized
    ApprovedBundle alone is not proof. The core must expire no later than any of its
    verified artifact/approval evidence, so its declared window cannot outlive proof.
    """
    reject = lambda code: BundleRejection((code,))
    if (type(bundle) is not CandidateBundle or type(approvals) is not tuple
            or any(type(a) is not GateApproval for a in approvals)
            or type(artifacts) is not tuple or any(type(a) is not ReleaseArtifact for a in artifacts)
            or type(now_us) is not int or now_us < 0):
        return reject('INVALID_INPUT')
    core = _core(bundle)
    if core.expires_at_us <= now_us:
        return reject('BUNDLE_EXPIRED')
    if bundle.gate_approvals and _ordered(bundle.gate_approvals) != _ordered(approvals):
        return reject('APPROVAL_SET_CONFLICT')
    gates = tuple(_gate(a.gate) for a in approvals)
    if len(set(gates)) != len(gates) or any(gate not in GATES for gate in gates):
        return reject('INVALID_APPROVALS')
    if missing_approvals(set(gates)):
        return reject('MISSING_APPROVALS')
    if any(a.bundle_hash != core.bundle_hash or a.approved_at_us > now_us for a in approvals):
        return reject('INVALID_APPROVALS')
    if any(_role_principal(a.actor) for a in approvals):
        return reject('NON_OPERATOR_APPROVER')
    if not isinstance(verifier, ReleaseEvidenceVerifier):
        return reject('VERIFIER_UNAVAILABLE')
    try:
        expected = _expected_artifacts(core)
        if any(type(value) is not str or not _HASH.fullmatch(value) for value in expected.values()):
            return reject('INVALID_INPUT')
        roles = tuple(a.role for a in artifacts)
        if len(set(roles)) != len(roles) or set(roles) != set(expected):
            return reject('ARTIFACT_SET_MISMATCH')
        for artifact in artifacts:
            if (type(artifact.role) is not str or type(artifact.payload) is not bytes
                    or not artifact.payload or type(artifact.expires_at_us) is not int):
                return reject('INVALID_INPUT')
            if artifact.expires_at_us <= now_us:
                return reject('ARTIFACT_EXPIRED')
            if artifact.content_hash != expected[artifact.role]:
                return reject('ARTIFACT_IDENTITY_MISMATCH')
            if core.expires_at_us > artifact.expires_at_us:
                return reject('BUNDLE_OUTLIVES_EVIDENCE')
        ordered = _ordered(approvals)
        request = ReleaseVerificationRequest(core, ordered, artifacts, now_us)
        evidence = verifier.verify(request)
    except Exception:
        return reject('VERIFICATION_UNAVAILABLE')
    if (type(evidence) is not VerifiedReleaseEvidence or evidence.issuer is not verifier
            or evidence.request_hash != request.request_hash):
        return reject('UNBOUND_VERIFICATION')
    if (type(evidence.verified_at_us) is not int or type(evidence.expires_at_us) is not int
            or not 0 <= evidence.verified_at_us <= now_us < evidence.expires_at_us
            or any(a.approved_at_us > evidence.verified_at_us for a in ordered)):
        return reject('INVALID_VERIFIED_WINDOW')
    if core.expires_at_us > evidence.expires_at_us:
        return reject('BUNDLE_OUTLIVES_EVIDENCE')
    if (type(evidence.compatible_artifacts) is not tuple
            or any(type(e) is not ManifestEntry for e in evidence.compatible_artifacts)
            or len({e.record_id for e in evidence.compatible_artifacts}) != len(evidence.compatible_artifacts)
            or set(evidence.compatible_artifacts) != set(compatibility_manifest(core))):
        return reject('INCOMPATIBLE_ARTIFACTS')
    if (type(evidence.operator_actors) is not tuple or not evidence.operator_actors
            or any(type(actor) is not str or not actor for actor in evidence.operator_actors)
            or any(a.actor not in evidence.operator_actors for a in ordered)
            or type(evidence.approval_event_hashes) is not tuple
            or len(evidence.approval_event_hashes) != len(ordered)
            or any(type(h) is not str or not _HASH.fullmatch(h) for h in evidence.approval_event_hashes)
            or set(evidence.approval_event_hashes) != {content_hash(a) for a in ordered}):
        return reject('UNAUTHENTICATED_APPROVALS')
    manifest_hash = content_hash(('validated-release-v1', request.request_hash,
        evidence.compatible_artifacts, evidence.operator_actors, tuple(sorted(evidence.approval_event_hashes)),
        evidence.verified_at_us, evidence.expires_at_us))
    return ApprovedBundle(core, ordered, now_us, manifest_hash)


def entries_allowed(active: ApprovedBundle | None, now_us: int) -> bool:
    """An absent, untyped or expired active bundle blocks new entries (exits are not gated here)."""
    return (type(active) is ApprovedBundle and type(now_us) is int and 0 <= now_us < active.bundle.expires_at_us)


@dataclass(frozen=True)
class ActivationState:
    """Authority-read current state. `history` and `risk_latches` are append-only."""
    account_id: str
    active: ApprovedBundle | None
    armed: bool
    reconciled: bool
    unknown_intent_ids: tuple[str, ...]
    pending_action_keys: tuple[str, ...]
    in_flight_judgment_ids: tuple[str, ...]
    risk_latches: tuple[str, ...]
    history: tuple[str, ...]
    boundary_in_progress: bool


@dataclass(frozen=True)
class ActivationPlan:
    """The exact transition to commit in one authority transaction at `effective_at_us`."""
    approved: ApprovedBundle
    previous_hash: str | None
    effective_at_us: int
    actor: str
    rollback: bool
    invalidated_judgment_ids: tuple[str, ...]
    retained_action_keys: tuple[str, ...]
    risk_latches: tuple[str, ...]
    history: tuple[str, ...]

    @property
    def plan_hash(self) -> str:
        return content_hash(('release-activation-v1', self.approved.bundle_hash, self.approved.validation_manifest_hash,
            self.previous_hash, self.effective_at_us, self.actor, self.rollback, self.invalidated_judgment_ids,
            self.retained_action_keys, self.risk_latches, self.history))

    def accepts_judgment(self, bundle_hash: str, boundary_us: int) -> bool:
        return (bundle_hash == self.approved.bundle_hash and type(boundary_us) is int
            and boundary_us >= self.effective_at_us)


def _strings(values):
    return type(values) is tuple and all(type(v) is str and v for v in values)


def plan_activation(*, bundle: CandidateBundle, approvals: tuple[GateApproval, ...],
        artifacts: tuple[ReleaseArtifact, ...], verifier: ReleaseEvidenceVerifier | None,
        now_us: int, state: ActivationState, session: ControlSession,
        boundary_us: int) -> ActivationPlan | BundleRejection:
    """Plan an operator-authenticated activation at the next hourly boundary.

    Always revalidates the bundle now. Requires an unarmed, reconciled account with
    no UNKNOWN intent and no boundary decision in flight. In-flight judgments are
    invalidated; pending economic action keys, risk latches and release history are
    carried forward unchanged (history only grows). A forward release must name the
    active bundle as its parent; a rollback must be a previously active bundle and
    still pass full validation. Nothing here persists or applies the plan.
    """
    reject = lambda code: BundleRejection((code,))
    if type(state) is not ActivationState:
        return reject('INVALID_INPUT')
    if (type(session) is not ControlSession or session.role != 'operator'
            or session.account_id != state.account_id or _role_principal(session.actor)):
        return reject('OPERATOR_SESSION_REQUIRED')
    if (not all(_strings(v) for v in (state.unknown_intent_ids, state.pending_action_keys,
            state.in_flight_judgment_ids, state.risk_latches, state.history))
            or any(type(v) is not bool for v in (state.armed, state.reconciled, state.boundary_in_progress))
            or state.active is not None and type(state.active) is not ApprovedBundle):
        return reject('INVALID_INPUT')
    if state.armed:
        return reject('ARMED')
    if state.unknown_intent_ids:
        return reject('PENDING_UNKNOWN_ORDER')
    if not state.reconciled:
        return reject('UNRECONCILED')
    if state.boundary_in_progress:
        return reject('BOUNDARY_IN_PROGRESS')
    approved = validate_bundle(bundle, approvals, artifacts=artifacts, verifier=verifier, now_us=now_us)
    if type(approved) is not ApprovedBundle:
        return approved
    if type(boundary_us) is not int or boundary_us <= now_us or boundary_us % HOUR_US:
        return reject('INVALID_BOUNDARY')
    if approved.bundle.expires_at_us <= boundary_us:
        return reject('BUNDLE_EXPIRES_BEFORE_BOUNDARY')
    previous = state.active.bundle_hash if state.active is not None else None
    if approved.bundle_hash == previous:
        return reject('ALREADY_ACTIVE')
    rollback = approved.bundle_hash in state.history
    if not rollback and previous is not None and approved.bundle.parent_hash != previous:
        return reject('LINEAGE_MISMATCH')
    return ActivationPlan(approved, previous, boundary_us, session.actor, rollback,
        state.in_flight_judgment_ids, state.pending_action_keys, state.risk_latches,
        state.history + (approved.bundle_hash,))
