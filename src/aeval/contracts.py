"""aeval — agent trajectory evaluation framework built on Harbor.

Domain contracts shared across every aeval module. This package is the
single source of truth for the record shapes that flow through the
pipeline: adapter declarations, canonical transcripts (ATIF + extras),
claim checks, requirement bitmaps, evidence bundles, grade results,
trial records and run manifests.

Design rules enforced here (from the approved plan):

- ``CanonicalTranscript`` uses a *valid ATIF* ``Trajectory`` as its
  underlying representation; aeval-specific completeness/claims/evidence
  metadata is stored in the ATIF ``extra`` namespace, never by inventing
  sibling fields.
- ``GradeResult.score`` is ``{value, valid, invalid_reasons}`` — an
  unjudgeable outcome must carry ``valid=False``; ``0`` must never
  impersonate "cannot judge".
- ``Verdict`` distinguishes ``infra_invalid`` (infrastructure/evidence
  failure, excluded from the valid denominator) and ``cannot_judge``
  (evidence present but insufficient for the rubric).
- ``RequirementBitmap`` has exactly six fixed requirements.
- ``ImageIdentity`` forbids mutable tags: only digest references.
"""

from __future__ import annotations

from datetime import datetime, timezone
from hashlib import sha256
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator, model_validator

from harbor.models.trajectories import Trajectory

__all__ = [
    "STOP_REASONS",
    "VERDICTS",
    "REQUIREMENT_FIELDS",
    "StopReason",
    "Verdict",
    "CompletenessStatus",
    "RequirementStatus",
    "AEVAL_EXTRA_KEY",
    "DSH_EXTRA_KEY",
    "DSH_IGNORABLE_EXTRA_KEY",
    "RequirementBitmap",
    "FieldCompleteness",
    "CompletenessRecord",
    "ClaimFinding",
    "ClaimCheck",
    "ArtifactRef",
    "TranscriptCapability",
    "AdapterSpec",
    "CanonicalTranscript",
    "ObservedModel",
    "BudgetSnapshot",
    "ForkLineage",
    "BundleDescriptor",
    "CollectOutcome",
    "CollectionManifest",
    "EvidenceBundle",
    "Score",
    "CoverageSummary",
    "GradeResult",
    "TrialCoordinates",
    "VersionsBundle",
    "TrialRecord",
    "ExclusionSummary",
    "ImageIdentity",
    "PythonEnvironmentLock",
    "HarborLock",
    "PluginIdentity",
    "NpmPackageLock",
    "DshReleaseLock",
    "RuntimeLock",
    "OverlayIdentity",
    "RunManifest",
    "canonical_json",
]

StopReason = Literal[
    "agent_exit_0",
    "agent_exit_nonzero",
    "agent_claimed_done",
    "budget_exhausted",
    "timeout_killed",
    "crashed",
    "infra_error",
]
STOP_REASONS: tuple[str, ...] = (
    "agent_exit_0",
    "agent_exit_nonzero",
    "agent_claimed_done",
    "budget_exhausted",
    "timeout_killed",
    "crashed",
    "infra_error",
)

Verdict = Literal["pass", "fail", "infra_invalid", "cannot_judge"]
VERDICTS: tuple[str, ...] = ("pass", "fail", "infra_invalid", "cannot_judge")

CompletenessStatus = Literal["ok", "partial", "unavailable"]
RequirementStatus = Literal["ok", "partial", "unavailable"]

REQUIREMENT_FIELDS: tuple[str, ...] = (
    "input_complete",
    "agent_finished",
    "integration_valid",
    "render_valid",
    "judge_finished",
    "artifact_schema_ok",
)

AEVAL_EXTRA_KEY = "aeval"
DSH_EXTRA_KEY = "dsh"
DSH_IGNORABLE_EXTRA_KEY = "dsh_ignorable_events"


def canonical_json(value: Any) -> bytes:
    """Deterministic JSON encoding used for all content digests."""
    import json

    return json.dumps(
        value,
        sort_keys=True,
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")


def _digest(value: Any) -> str:
    return sha256(canonical_json(value)).hexdigest()


class RequirementBitmap(BaseModel):
    """The six fixed P0 requirements every trial is measured against."""

    input_complete: bool = False
    agent_finished: bool = False
    integration_valid: bool = False
    render_valid: bool = False
    judge_finished: bool = False
    artifact_schema_ok: bool = False

    @property
    def satisfied_count(self) -> int:
        return sum(1 for f in REQUIREMENT_FIELDS if getattr(self, f))

    @property
    def all_satisfied(self) -> bool:
        return self.satisfied_count == len(REQUIREMENT_FIELDS)

    def to_dict(self) -> dict[str, bool]:
        return {f: getattr(self, f) for f in REQUIREMENT_FIELDS}


class FieldCompleteness(BaseModel):
    field: str
    status: CompletenessStatus
    reason: str | None = None


class CompletenessRecord(BaseModel):
    fields: list[FieldCompleteness] = Field(default_factory=list)

    @property
    def worst_status(self) -> CompletenessStatus:
        if any(f.status == "unavailable" for f in self.fields):
            return "unavailable"
        if any(f.status == "partial" for f in self.fields):
            return "partial"
        return "ok"

    def status_of(self, field: str) -> CompletenessStatus:
        for f in self.fields:
            if f.field == field:
                return f.status
        return "unavailable"


class ClaimFinding(BaseModel):
    kind: Literal["model_identity", "reported_usage", "tool_call", "task_finished"]
    status: Literal["consistent", "mismatch", "unverifiable"]
    detail: str
    observed: str | None = None
    claimed: str | None = None


class ClaimCheck(BaseModel):
    findings: list[ClaimFinding] = Field(default_factory=list)
    overall: Literal["consistent", "mismatch", "unverifiable"] = "unverifiable"
    cost_source_downgraded: bool = False
    security_rubric_triggered: bool = False


class ArtifactRef(BaseModel):
    """Content-addressed artifact reference.

    ``path`` is relative to the owning bundle directory and must never be
    absolute or contain ``..`` segments (verified by EvidenceBundle
    consumers).
    """

    media_type: str
    sha256: str = Field(min_length=64, max_length=64)
    size_bytes: int = Field(ge=0)
    path: str

    @field_validator("sha256")
    @classmethod
    def _sha256_hex(cls, v: str) -> str:
        if any(c not in "0123456789abcdef" for c in v.lower()):
            raise ValueError("sha256 must be lowercase hex")
        return v.lower()

    @field_validator("path")
    @classmethod
    def _relative_path(cls, v: str) -> str:
        import ntpath
        import posixpath

        for p in (ntpath, posixpath):
            if p.isabs(v):
                raise ValueError(f"artifact path must be relative: {v!r}")
        parts = v.replace("\\", "/").split("/")
        if ".." in parts:
            raise ValueError(f"artifact path must not escape the bundle: {v!r}")
        return v


class TranscriptCapability(BaseModel):
    """What an adapter can deliver, per field — drives cannot_judge logic."""

    source: Literal["native_session_via_bridge", "atif_native"]
    reader: str | None = None
    capabilities: list[str] = Field(default_factory=list)
    fields_available: dict[str, CompletenessStatus] = Field(default_factory=dict)


class AdapterSpec(BaseModel):
    """Declarative adapter contract (mirrors agent.yaml, no argv details)."""

    id: str
    version: str
    impl: str
    impl_version: str
    mode: Literal["installed_cli", "acp_stdio", "sdk_jsonrpc"] = "installed_cli"
    transcript: TranscriptCapability
    budget_enforcement: Literal["gateway_lease", "wallclock_kill", "none"] = "none"
    write_surface: Literal["ephemeral_overlay", "persistent"] = "ephemeral_overlay"
    server_side_session: Literal["forbidden", "allowed_declared"] = "forbidden"


class CanonicalTranscript(BaseModel):
    """A valid ATIF trajectory plus the aeval metadata envelope.

    The ATIF ``extra`` dict is the storage location for the aeval
    namespace (``extra.aeval`` = stop_reason, evidence_uri, completeness,
    claims) and for vendor namespaces (``extra.dsh`` …). ``to_json_dict``
    writes them into the trajectory; ``from_json_dict`` reads them back,
    so the serialized artifact is always a self-contained ATIF document.
    """

    atif: Trajectory
    stop_reason: StopReason = "infra_error"
    evidence_uri: str | None = None
    completeness: CompletenessRecord | None = None
    claims: ClaimCheck | None = None

    def to_json_dict(self) -> dict[str, Any]:
        atif_dict = self.atif.to_json_dict(exclude_none=True)
        extra = dict(atif_dict.get("extra") or {})
        extra[AEVAL_EXTRA_KEY] = {
            "stop_reason": self.stop_reason,
            "evidence_uri": self.evidence_uri,
            "completeness": (
                self.completeness.model_dump(exclude_none=True)
                if self.completeness
                else None
            ),
            "claims": (
                self.claims.model_dump(exclude_none=True) if self.claims else None
            ),
        }
        atif_dict["extra"] = extra
        return atif_dict

    @classmethod
    def from_json_dict(cls, data: dict[str, Any]) -> CanonicalTranscript:
        extra = dict(data.get("extra") or {})
        aeval = dict(extra.get(AEVAL_EXTRA_KEY) or {})
        atif_data = {k: v for k, v in data.items() if k != "extra"}
        if extra:
            atif_data["extra"] = {
                k: v for k, v in extra.items() if k != AEVAL_EXTRA_KEY
            }
        return cls(
            atif=Trajectory.model_validate(atif_data),
            stop_reason=aeval.get("stop_reason", "infra_error"),
            evidence_uri=aeval.get("evidence_uri"),
            completeness=(
                CompletenessRecord.model_validate(aeval["completeness"])
                if aeval.get("completeness")
                else None
            ),
            claims=(
                ClaimCheck.model_validate(aeval["claims"])
                if aeval.get("claims")
                else None
            ),
        )

    @classmethod
    def build(
        cls,
        atif: Trajectory,
        stop_reason: StopReason,
        evidence_uri: str | None = None,
        completeness: CompletenessRecord | None = None,
        claims: ClaimCheck | None = None,
    ) -> CanonicalTranscript:
        return cls(
            atif=atif,
            stop_reason=stop_reason,
            evidence_uri=evidence_uri,
            completeness=completeness,
            claims=claims,
        )


class ObservedModel(BaseModel):
    provider: str | None = None
    model: str | None = None
    source: Literal["gateway_lease", "session_header", "claim"] = "session_header"


class BudgetSnapshot(BaseModel):
    max_seconds: float | None = None
    max_tokens: int | None = None
    max_steps: int | None = None
    used_tokens: int | None = None
    used_steps: int | None = None
    enforcement_point: Literal["gateway_lease", "wallclock_kill", "none"] = "none"


class ForkLineage(BaseModel):
    parent_session_id: str | None = None
    parent_trial_id: str | None = None
    fork_step: int | None = None


class BundleDescriptor(BaseModel):
    """File contract emitted by the host-side runner (dsh-eval-control).

    Python consumes this only as untrusted input: it must never carry an
    absolute session root or a path that escapes the declared root.
    """

    schema_version: int = 1
    trial_id: str
    session_id: str
    session_root: str
    stop_reason: StopReason
    config_digest: str
    lineage: ForkLineage | None = None

    @field_validator("schema_version")
    @classmethod
    def _known_schema(cls, v: int) -> int:
        if v != 1:
            raise ValueError(
                f"bundle descriptor schema_version {v!r} is unknown — this "
                "aeval only consumes schema 1 descriptors from the host runner"
            )
        return v

    @field_validator("session_root")
    @classmethod
    def _relative_root(cls, v: str) -> str:
        import ntpath
        import posixpath

        for p in (ntpath, posixpath):
            if p.isabs(v):
                raise ValueError(f"session_root must be relative: {v!r}")
        parts = v.replace("\\", "/").split("/")
        if ".." in parts:
            raise ValueError(f"session_root must not escape: {v!r}")
        return v


class CollectOutcome(BaseModel):
    """Result of one ``[[verifier.collect]]`` command, as recorded by aeval."""

    name: str
    command: str
    exit_code: int | None = None
    exception: str | None = None
    started_at: datetime
    finished_at: datetime | None = None
    output_path: str | None = None
    sha256: str | None = None
    atomic: bool = True


class CollectionManifest(BaseModel):
    schema_version: int = 1
    trial_id: str
    outcomes: list[CollectOutcome] = Field(default_factory=list)
    artifacts: list[ArtifactRef] = Field(default_factory=list)


class EvidenceBundle(BaseModel):
    """Sealed, verified evidence for one trial — the only grader input."""

    trial_id: str
    stop_reason: StopReason
    requirements: RequirementBitmap = Field(default_factory=RequirementBitmap)
    artifacts: dict[str, ArtifactRef] = Field(default_factory=dict)
    collection_manifest: CollectionManifest | None = None
    bundle_descriptor: BundleDescriptor | None = None
    issues: list[str] = Field(default_factory=list)


class Score(BaseModel):
    """A grader score. ``valid=False`` marks unjudgeable — never 0."""

    value: float | None = None
    valid: bool = True
    invalid_reasons: list[str] = Field(default_factory=list)

    @field_validator("value")
    @classmethod
    def _finite(cls, v: float | None) -> float | None:
        if v is None:
            return None
        import math

        if math.isnan(v) or math.isinf(v):
            raise ValueError("score value must be finite")
        return v

    @model_validator(mode="after")
    def _shape(self) -> "Score":
        if self.valid and self.value is None:
            raise ValueError("a valid score must carry a value")
        if not self.valid:
            if self.value is not None:
                raise ValueError("an invalid score must not carry a value")
            if not self.invalid_reasons:
                raise ValueError("an invalid score must state invalid_reasons")
        return self


class CoverageSummary(BaseModel):
    """Which evidence fields a grader's verdict depended on."""

    required_fields: list[str] = Field(default_factory=list)
    satisfied_fields: list[str] = Field(default_factory=list)
    missing_fields: list[str] = Field(default_factory=list)
    degraded_fields: list[str] = Field(default_factory=list)


class GradeResult(BaseModel):
    grader_id: str
    grader_version: str
    layer: Literal["outcome", "trajectory", "both"]
    veto: bool = False
    score: Score
    status: Literal["pass", "fail", "cannot_judge"]
    reasons: list[str] = Field(default_factory=list)
    coverage: CoverageSummary | None = None
    produced_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class TrialCoordinates(BaseModel):
    run_id: str
    suite_id: str
    suite_version: str
    task_id: str
    trial_index: int = Field(ge=0)


class VersionsBundle(BaseModel):
    aeval_version: str
    mapper_version: str | None = None
    grader_versions: dict[str, str] = Field(default_factory=dict)
    converter_version: str | None = None
    parser_version: str | None = None


class TrialRecord(BaseModel):
    """The atomic per-trial record — the only unit of aggregation."""

    trial_id: str
    coordinates: TrialCoordinates
    stop_reason: StopReason
    baseline_ok: bool = True
    requirements: RequirementBitmap = Field(default_factory=RequirementBitmap)
    observed_model: ObservedModel | None = None
    budget: BudgetSnapshot | None = None
    adapter: AdapterSpec | None = None
    claim: ClaimCheck | None = None
    artifacts: dict[str, ArtifactRef] = Field(default_factory=dict)
    transcript_extra: dict[str, Any] | None = None
    grades: list[GradeResult] = Field(default_factory=list)
    verdict: Verdict | None = None
    fork: ForkLineage | None = None
    versions: VersionsBundle | None = None
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class ExclusionSummary(BaseModel):
    total: int = 0
    valid: int = 0
    excluded: dict[str, int] = Field(default_factory=dict)
    excluded_trial_ids: dict[str, list[str]] = Field(default_factory=dict)

    @property
    def exclusion_rate(self) -> float:
        if self.total == 0:
            return 0.0
        return (self.total - self.valid) / self.total


class ImageIdentity(BaseModel):
    """Digest-pinned OCI image identity. Mutable tags are forbidden."""

    reference: str
    digest: str
    platform: str
    pinned: bool = True
    original_tag: str | None = None

    @field_validator("reference")
    @classmethod
    def _digest_ref(cls, v: str) -> str:
        if "@sha256:" not in v and "@sha512:" not in v:
            raise ValueError(
                f"image reference must be digest-pinned (repo@sha256:...): {v!r}"
            )
        return v


class PythonEnvironmentLock(BaseModel):
    python_version: str
    uv_version: str | None = None
    platform: str
    docker_version: str | None = None
    buildkit_version: str | None = None


class HarborLock(BaseModel):
    version: str
    commit: str | None = None
    source_clean: bool = True
    shallow: bool = False
    pyproject_sha256: str | None = None
    uv_lock_sha256: str | None = None


class PluginIdentity(BaseModel):
    distribution: str
    version: str
    import_path: str
    wheel_sha256: str | None = None
    parameter_digest: str | None = None


class NpmPackageLock(BaseModel):
    name: str
    version: str
    integrity: str | None = None


class DshReleaseLock(BaseModel):
    """Exact official DSH preview slice — never auto-upgrade."""

    official_tag: str
    commit: str
    packages: list[NpmPackageLock] = Field(default_factory=list)
    node_versions: list[str] = Field(default_factory=list)
    cordis_version: str | None = None
    acp_sdk_version: str | None = None
    lockfile_sha256: str | None = None
    experimental: bool = True


class RuntimeLock(BaseModel):
    """Complete identity of the software and images a run executes on."""

    schema_version: int = 1
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    harbor: HarborLock
    python_env: PythonEnvironmentLock
    images: dict[str, ImageIdentity] = Field(default_factory=dict)
    plugin: PluginIdentity | None = None
    dsh: DshReleaseLock | None = None
    harbor_lock_ref: str | None = None

    def digest(self) -> str:
        return _digest(self.model_dump(mode="json", exclude={"created_at"}))


class OverlayIdentity(BaseModel):
    suite_id: str
    suite_version: str
    overlay_digest: str
    source_commit: str
    source_url: str | None = None


class RunManifest(BaseModel):
    """Intent manifest written at job start, sealed with exclusions at end."""

    schema_version: int = 1
    run_id: str
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    runtime_lock: RuntimeLock
    runtime_lock_digest: str = ""
    lock_ref: str | None = None
    overlay: OverlayIdentity
    versions: VersionsBundle
    argv_hash: str = ""
    env_hash: str = ""
    config_hash: str = ""
    budget_enforcement_point: str = "none"
    artifact_digest: str | None = None
    exclusions: ExclusionSummary | None = None

    def digest(self) -> str:
        return _digest(self.model_dump(mode="json", exclude={"created_at"}))
