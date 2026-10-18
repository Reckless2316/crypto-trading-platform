"""Ledger-mode formal pipeline: causal ingestion, paper outcome evidence, calibration attestation job.

Every public book the runtime fetches is also ingested into the formal ``StateEngine`` as an immutable
``SourceEvent`` (exchange time, receipt time, exchange sequence, durable archive). Formal snapshots for a
REFLEX judgment therefore come from the real causal pipeline (``snapshot`` -> ``encode`` ->
``persist_encoded_state``), never from the runtime's own paper snapshot, which stays the paper loop's state.

Paper outcome labels are resolved only from those archived public books (``PaperBookOutcomeVerifier``), and
calibration attestation runs only over the ledger's resolved labels once ``build_calibration_artifact``'s floors
are met. Attestation is evidence: a release that names the calibrator still needs operator approval and
activation. Nothing here arms, activates or fabricates.

The one production blocker that remains for paper entries is the provider tokenizer proof required by
``encode_state``; without a ``ProviderTokenCounter`` every formal decision reports
``SNAPSHOT_TOKEN_PROOF_UNAVAILABLE``.
"""
from __future__ import annotations

import base64
import json
from dataclasses import dataclass
from decimal import ROUND_FLOOR, Decimal, localcontext
from pathlib import Path

from .calibration import (ATTESTATION_METHOD, LABEL_DEFINITION, OutcomeEvidenceVerifier, VerifiedOutcomeEvidence, attest_calibration_artifact,
                          build_calibration_artifact)
from .contracts import (CANDIDATE_WIRE_IDS, BookPayload, CausalCutoffs, ContractViolation, DecisionBatch, Feature, Regime,
                        SharedContext, SourceEvent, StateHoldDraft, TokenAccountingUnavailable, canonical_bytes, content_hash,
                        default_field_dictionary)
from .state import SourceClockPolicy, StateEngine

PAPER_SOURCE, PAPER_STREAM = "coinbase-public", "book-l2"
PAPER_BOOK_POLICY = SourceClockPolicy("paper-public-book-v1", PAPER_SOURCE, PAPER_STREAM, 10_000_000, 2_000_000)
PAPER_EVIDENCE = "paper-public-book"
HOUR_US = 3_600_000_000
CLOSE_SAMPLE_US = 5_000_000  # a horizon close is observed by the first archived book within 5 s after it
REFERENCE_NOTIONAL_USD = Decimal(100)
_LOT = Decimal("0.00000001")
_MONEY = Decimal("0.0000000001")


def paper_execution_policy_hash(fee_rate: Decimal, notional: Decimal = REFERENCE_NOTIONAL_USD) -> str:
    """Identity of the paper label convention: reference notional, fee bound, top-of-book with walked slippage."""
    return content_hash((PAPER_EVIDENCE, "top_of_book_plus_walked_slippage_v1", str(fee_rate), str(notional)))


def _candidate_of(instrument: str) -> tuple[str, int] | None:
    return next(((cid, wire) for cid, wire in CANDIDATE_WIRE_IDS if cid.upper() + "-USD" == instrument), None)


@dataclass(frozen=True)
class FormalDecision:
    """A formal decision persisted over a persisted causal snapshot, or the precise reasons it could not be."""
    reasons: tuple[str, ...]
    decision_id: str | None = None
    batch: DecisionBatch | None = None
    decision: object = None
    snapshot: object = None


class FormalPipeline:
    def __init__(self, adapter, archive_root: Path, *, token_counter=None, fee_rate: Decimal = Decimal("0.006")):
        self.adapter, self.ledger = adapter, adapter.ledger
        self.engine = StateEngine(adapter.ledger, Path(archive_root), (PAPER_BOOK_POLICY,), account_id=adapter.account_id)
        self.token_counter = token_counter  # production: None until a provider tokenizer proof exists
        self.fee_rate = fee_rate
        self.cost_hash = paper_execution_policy_hash(fee_rate)

    # ---------- causal ingestion ----------
    def ingest_book(self, instrument: str, bids, asks, *, event_us: int, received_us: int, sequence) -> str:
        """Ingest one fetched public book as an immutable full-book SourceEvent. Returns 'accepted' or a reason."""
        if type(sequence) is not int or sequence < 0:
            return "SEQUENCE_UNAVAILABLE"
        current = self.engine._books.get((PAPER_SOURCE, PAPER_STREAM, instrument))
        if current is not None and sequence <= current[0]:
            return "UNCHANGED_SEQUENCE"  # the exchange book has not advanced; nothing new to record
        def levels(rows, reverse):
            merged = {}
            for price, qty in rows:
                p, q = Decimal(str(price)), Decimal(str(qty))
                if p > 0 and q > 0:
                    merged[p] = merged.get(p, Decimal(0)) + q
            return tuple(sorted(merged.items(), reverse=reverse))
        try:
            payload = BookPayload(levels(bids, True), levels(asks, False))
            event = SourceEvent(f"{PAPER_SOURCE}:{instrument}:{sequence}", PAPER_SOURCE, PAPER_STREAM, instrument,
                                f"{instrument}:{sequence}", 0, event_us, received_us, received_us, payload,
                                content_hash(payload), sequence)
        except ContractViolation as exc:
            return "INVALID_BOOK:" + str(exc)[:40]
        result = self.engine.ingest(event)
        return "accepted" if result.accepted else "REJECTED:" + ",".join(result.reason_codes or ("unknown",))

    # ---------- formal decisions ----------
    def decide(self, instrument: str, judgment, *, close_us: int, now_us: int, mono_ns: int, gate_open: bool,
               boot_id: str, raw_response_hash: str | None = None) -> FormalDecision:
        """Persist a REFLEX judgment as a formal decision over the causal snapshot of the activated release."""
        candidate = _candidate_of(instrument)
        if candidate is None:
            return FormalDecision(("NOT_A_CANDIDATE",))
        approved = self.ledger.active_approved(self.adapter.account_id)
        if approved is None:
            return FormalDecision(("RELEASE_NOT_REGISTERED",))
        definitions = {c.candidate_id: c for c in approved.bundle.candidates}
        if candidate[0] not in definitions:
            return FormalDecision(("NOT_IN_RELEASE",))
        formal = getattr(judgment, "formal", None)
        if formal is None or formal.candidate_id != candidate[0]:
            return FormalDecision(("FORMAL_JUDGMENT_UNAVAILABLE",))
        if self.token_counter is None:
            return FormalDecision(("SNAPSHOT_TOKEN_PROOF_UNAVAILABLE",))
        cutoffs = CausalCutoffs(close_us, now_us, mono_ns + 1_000_000_000, boot_id)
        snapshots = tuple(self.engine.snapshot(c.instrument, cutoffs, eligibility=c.eligible) for c in approved.bundle.candidates)
        dictionary = default_field_dictionary()
        context = SharedContext(tuple(Feature(f.name, 0.0, False) for f in dictionary.shared_fields), 0)
        try:
            encoded = self.engine.encode(snapshots, context, dictionary, self.token_counter,
                                         candidate_order=tuple(c.candidate_id for c in approved.bundle.candidates),
                                         ranked_bundle=approved)
        except TokenAccountingUnavailable:
            return FormalDecision(("SNAPSHOT_TOKEN_PROOF_UNAVAILABLE",))
        persisted = self.engine.persist_encoded_state(encoded)
        if type(encoded) is StateHoldDraft:
            return FormalDecision(("STATE_HOLD:" + encoded.reason,))
        snapshot = next((s for s in persisted.snapshots if s.candidate_id == candidate[0]), None)
        if snapshot is None:
            reason = dict(persisted.excluded_candidate_reasons).get(candidate[0], "excluded")
            return FormalDecision(("STATE_EXCLUDED:" + reason,))
        batch = DecisionBatch(f"runtime-{judgment.reflex_id}-{instrument}-{close_us}", persisted.state_hash,
                              approved.bundle_hash, judgment.reflex_id, mono_ns, mono_ns, now_us, (formal,),
                              raw_response_hash, schema_hash=approved.bundle.schema_hash)
        decision_id = self.ledger.record_formal_decision(snapshot_id=snapshot.snapshot_id, batch=batch,
                                                         candidate_wire=candidate[1], eligible=gate_open)
        try:
            self.ledger.append_forecast(forecast_id="forecast-" + decision_id, decision_id=decision_id,
                                        candidate_id=candidate[1], side="buy", execution_policy_hash=self.cost_hash,
                                        eligibility=gate_open, recorded_at_us=now_us)
        except ContractViolation:
            pass  # a forecast that cannot be recorded (e.g. outside its window) never blocks the decision record
        return FormalDecision((), decision_id, batch, formal, snapshot)

    # ---------- labels and attestation ----------
    def resolve_matured_labels(self, now_us: int) -> int:
        """Resolve every matured pending label through the configured paper verifier; missing evidence stays pending."""
        resolved = 0
        rows = self.ledger.connection.execute(
            "SELECT label_id FROM outcome_labels WHERE status='pending' AND maturity_us+? <= ? ORDER BY maturity_us",
            (CLOSE_SAMPLE_US, now_us)).fetchall()
        for row in rows:
            try:
                self.ledger.revise_outcome_label(row[0], "resolved", {}, recorded_at_us=now_us)
                resolved += 1
            except Exception:
                continue
        return resolved

    def attestation_job(self, *, instrument: str, regime: Regime, model_id: str, now_us: int,
                        validity_us: int = 30 * 24 * HOUR_US, out_dir: Path | None = None) -> dict:
        """Build and attest a calibrator from REAL resolved labels once the floors are met.

        If an activated-or-registered release already names the attested artifact as its calibrator, the
        attestation is recorded against it; otherwise the artifact is written as a proposal for the operator.
        Nothing is applied to a release."""
        approved = self.ledger.active_approved(self.adapter.account_id)
        if approved is None:
            return {"status": "RELEASE_NOT_REGISTERED"}
        cohort = dict(instrument=instrument, regime=regime, side="buy", model_id=model_id,
                      schema_hash=approved.bundle.schema_hash, dictionary_hash=default_field_dictionary().dictionary_hash,
                      label_hash=content_hash(LABEL_DEFINITION), cost_hash=self.cost_hash)
        rows = sorted(self.ledger.calibration_rows(before_us=now_us, **cohort), key=lambda r: r["decision_at_us"])
        split = self._split(rows)
        if split is None:
            return {"status": "CALIBRATION_FLOOR_UNMET", "resolved_rows": len(rows)}
        training_cutoff, calibration_cutoff = split
        args = dict(cohort, training_cutoff_us=training_cutoff, calibration_cutoff_us=calibration_cutoff,
                    valid_from_us=calibration_cutoff + 1, expires_at_us=calibration_cutoff + 1 + validity_us)
        training = self.ledger.calibration_rows(before_us=training_cutoff, **cohort)
        calibration = self.ledger.calibration_rows(before_us=calibration_cutoff, after_us=training_cutoff, **cohort)
        try:
            base = build_calibration_artifact(training=training, calibration=calibration, **args)
            attested, metrics = attest_calibration_artifact(base, training=training, calibration=calibration, **args)
        except ValueError as exc:
            return {"status": "CALIBRATION_CHECKS_FAILED", "detail": str(exc)[:120], "resolved_rows": len(rows)}
        named = self.ledger.connection.execute(
            "SELECT release_hash FROM release_bundles WHERE json_extract(manifest_json,'$.calibrator_hash')=?",
            (attested.artifact_hash,)).fetchall()
        recorded = [self.ledger.record_calibration_attestation(release_hash=r[0], **args) for r in named]
        # The release `calibrator` artifact whose sha256 is the attested artifact hash (for an operator release).
        payload = canonical_bytes((ATTESTATION_METHOD, base.artifact_hash, metrics))
        result = {"status": "ATTESTED_RECORDED" if recorded else "ATTESTED_PROPOSAL_ONLY",
                  "artifact_hash": attested.artifact_hash,
                  "calibrator_payload_b64": base64.b64encode(payload).decode(), "training": len(training), "calibration": len(calibration),
                  "training_cutoff_us": training_cutoff, "calibration_cutoff_us": calibration_cutoff,
                  "releases": [r[0] for r in named]}
        if out_dir is not None:
            Path(out_dir).mkdir(parents=True, exist_ok=True)
            (Path(out_dir) / f"calibration-candidate-{attested.artifact_hash[:16]}.json").write_text(json.dumps(
                dict(result, artifact=attested.to_dict(), metrics=metrics,
                     governance="EVIDENCE ONLY: a release naming this calibrator needs operator approval and activation."),
                indent=2, default=str))
        return result

    @staticmethod
    def _split(rows) -> tuple[int, int] | None:
        """Chronological training/calibration cutoffs: each block >= 100 rows, calibration decisions after training."""
        n = len(rows)
        if n < 200:
            return None
        for k in range(n // 2, n - 99):
            if k < 100:
                continue
            training_cutoff = max(max(r["resolved_at_us"] for r in rows[:k]), max(r["horizon_close_us"] for r in rows[:k]))
            if rows[k]["decision_at_us"] > training_cutoff:
                return training_cutoff, max(r["resolved_at_us"] for r in rows)
        return None


class PaperBookOutcomeVerifier(OutcomeEvidenceVerifier):
    """Resolves a buy forecast's fixed four-hour outcome from archived public books only. PAPER accounts only.

    Entry: the first archived book after the forecast (best ask, walked for the reference notional). Each hourly close
    is observed by the first archived book within 5 s after it; the exit is the final close (best bid, walked). Fees
    use the paper fee bound; walked-book impact is charged separately as slippage. Every book must be known by the
    label's as-of time. Labeled ``paper-public-book`` evidence; never accepted for a live account."""

    def __init__(self, ledger, *, fee_rate: Decimal, notional_usd: Decimal = REFERENCE_NOTIONAL_USD):
        self.ledger, self.fee_rate, self.notional = ledger, fee_rate, notional_usd
        self.policy_hash = paper_execution_policy_hash(fee_rate, notional_usd)

    def _books(self, instrument: str, since_us: int, as_of_us: int):
        return self.ledger.connection.execute(
            "SELECT e.* FROM source_events e WHERE e.source=? AND e.stream=? AND e.instrument=? AND e.event_at_us>? "
            "AND e.known_at_us<=? AND e.recorded_at_us<=? AND EXISTS (SELECT 1 FROM event_segments g WHERE "
            "g.source=e.source AND g.instrument=e.instrument AND e.sequence BETWEEN g.first_sequence AND g.last_sequence) "
            "ORDER BY e.event_at_us",
            (PAPER_SOURCE, PAPER_STREAM, instrument, since_us, as_of_us, as_of_us)).fetchall()

    @staticmethod
    def _walk(levels, qty: Decimal) -> Decimal | None:
        remaining, cost = qty, Decimal(0)
        for price, size in levels:
            take = min(remaining, Decimal(size))
            cost += take * Decimal(price)
            remaining -= take
            if remaining <= 0:
                return cost / qty
        return None

    def verify(self, request):
        c = self.ledger.connection
        account = c.execute("SELECT a.mode FROM decisions d JOIN snapshots s USING(snapshot_id) JOIN accounts a "
                            "ON a.account_id=s.account_id WHERE d.decision_id=?", (request.decision_id,)).fetchone()
        if account is None or account[0] != "paper":
            return None  # paper public-book evidence is never evidence for a live account
        if request.side != "buy" or request.execution_policy_hash != self.policy_hash:
            return None
        names = {wire: name for name, wire in CANDIDATE_WIRE_IDS}
        instrument = names[request.candidate_id].upper() + "-USD"
        books = [dict(r) for r in self._books(instrument, request.forecast_at_us, request.as_of_us)]
        if not books:
            return None
        entry = books[0]
        closes = tuple((entry["event_at_us"] // HOUR_US + i) * HOUR_US for i in range(1, 5))
        observed = []
        for close in closes:
            book = next((b for b in books if close <= b["event_at_us"] <= close + CLOSE_SAMPLE_US
                         and b["received_at_us"] >= close), None)
            if book is None:
                return None  # incomplete hourly evidence: the label stays pending
            observed.append(book)
        exit_book = observed[-1]
        entry_payload, exit_payload = (BookPayload.from_json(b["payload_json"]) for b in (entry, exit_book))
        best_ask, best_bid = entry_payload.asks[0][0], exit_payload.bids[0][0]
        with localcontext() as ctx:
            ctx.prec = 50
            qty = (self.notional / best_ask).quantize(_LOT, rounding=ROUND_FLOOR)
            if qty <= 0:
                return None
            entry_vwap, exit_vwap = self._walk(entry_payload.asks, qty), self._walk(exit_payload.bids, qty)
            if entry_vwap is None or exit_vwap is None:
                return None
            entry_fee = (qty * entry_vwap * self.fee_rate).quantize(_MONEY)
            exit_fee = (qty * exit_vwap * self.fee_rate).quantize(_MONEY)
            slippage = ((entry_vwap - best_ask) * qty + (best_bid - exit_vwap) * qty).quantize(_MONEY)
        return VerifiedOutcomeEvidence(self, request.request_hash, entry["event_at_us"], entry["received_at_us"],
                                       entry["known_at_us"], closes, closes[-1], exit_book["received_at_us"],
                                       exit_book["known_at_us"], request.as_of_us, qty, best_ask, best_bid,
                                       entry_fee, exit_fee, max(slippage, Decimal(0)), self.policy_hash,
                                       tuple(b["payload_hash"] for b in [entry] + observed), None)


def configure_paper_verifier(ledger, verifier: PaperBookOutcomeVerifier) -> None:
    """Bind the paper verifier only to a ledger whose accounts are all paper."""
    if ledger.connection.execute("SELECT 1 FROM accounts WHERE mode!='paper' LIMIT 1").fetchone():
        raise ContractViolation("paper public-book outcome evidence is never configured for a live account")
    ledger.configure_outcome_verifier(verifier)
