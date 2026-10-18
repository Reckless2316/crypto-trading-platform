"""BRAIN port for nightly review. Proposals are text only.

This module never places orders, opens a database, or loads API keys. A configured
model id is supplied by the caller. This process is not that model.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from .contracts import ProviderUnavailable


ALLOWED_ACTIONS = frozenset({"no_change", "propose_schema"})


@dataclass(frozen=True)
class BrainProposal:
    action: str
    finding: str
    hypothesis: str
    evidence_refs: tuple[str, ...]
    schema_diff: str | None = None
    model_id: str = "unconfigured"


class BrainPort(Protocol):
    model_id: str

    def review(self, *, job_id: str, archive_hash: str, metrics: dict) -> BrainProposal: ...


class DeterministicBrain:
    """Metrics-only reviewer used when no model credential is configured."""

    model_id = "unavailable"

    def review(self, *, job_id: str, archive_hash: str, metrics: dict) -> BrainProposal:
        if not job_id or not archive_hash:
            raise ValueError("review job needs an id and archive hash")
        return BrainProposal(
            action="no_change",
            finding="Deterministic session metrics only.",
            hypothesis="No model was configured, so no schema change is proposed.",
            evidence_refs=(archive_hash,),
            schema_diff=None,
            model_id=self.model_id,
        )


class UnavailableBrain:
    """Provider outage. The caller still keeps the deterministic metrics."""

    model_id = "unavailable"

    def review(self, *, job_id: str, archive_hash: str, metrics: dict) -> BrainProposal:
        raise ProviderUnavailable("brain provider unavailable")
