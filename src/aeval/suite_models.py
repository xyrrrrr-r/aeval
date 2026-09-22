"""Suite models: thin overlay over Harbor-native declarations.

A suite is a directory (plan §9.1): ``suite.yaml`` declares ONLY the
four things Harbor does not (baselines, clock, observables, verdict,
plus metrics/driver/provenance). Any fact Harbor already declares in
its own task.yaml/job.yaml must NOT appear here — duplication is a CI
error, not a merge.
"""

from __future__ import annotations

from hashlib import sha256
from typing import Any, Literal

import yaml
from pydantic import BaseModel, Field, field_validator

__all__ = [
    "SuiteError",
    "OVERLAY_SCHEMA_VERSION",
    "HARBOR_OWNED_TOP_KEYS",
    "ImagePinAction",
    "BaselineAssertion",
    "ClockSpec",
    "ObservableSpec",
    "RubricDeclaration",
    "GraderDeclaration",
    "VerdictSpec",
    "MetricDeclaration",
    "DriverSpec",
    "ProvenanceInfo",
    "HarborInputs",
    "SuiteOverlay",
    "ResolvedSuite",
    "load_suite_yaml",
    "overlay_digest_of",
]

OVERLAY_SCHEMA_VERSION = 2

# Facts owned by Harbor's native files. If suite.yaml restates any of
# these (outside image.pin/image.rebuild narrowing) → SuiteError.
HARBOR_OWNED_TOP_KEYS = frozenset(
    {
        "environment",
        "egress",
        "writable_roots",
        "mocks",
        "budget",
        "trials",
        "tasks",
        "parallel",
        "k",
        "attempts",
        "retry",
        "timeout",
    }
)

HARBOR_OWNED_TASK_KEYS = frozenset(
    {"image", "seed_ref", "setup_script", "seed", "env", "setup"}
)


class SuiteError(RuntimeError):
    """Any suite-level validation failure. Always fail-loud, never merge."""


class ImagePinAction(BaseModel):
    """The single allowed overlap with Harbor-owned facts: narrowing.

    Either pin a mutable tag to a digest, or force a rebuild published
    by our CI. Both REPLACE the Harbor value at compose time; the
    replaced original is recorded.
    """

    pin: str | None = None
    rebuild: bool = False
    original: str | None = None

    @field_validator("pin")
    @classmethod
    def _pin_is_digest(cls, v: str | None) -> str | None:
        if v is not None and "@sha256:" not in v:
            raise SuiteError(f"image.pin must be digest-pinned: {v!r}")
        return v


class BaselineAssertion(BaseModel):
    id: str
    probe: str | None = None
    equals: Any = None
    assert_expr: str | None = None


class ClockSpec(BaseModel):
    mode: Literal["virtual_offset", "real"]
    epoch: str | None = None


class ObservableSpec(BaseModel):
    name: str
    type: Literal["string", "number", "boolean", "json"]
    source: str  # db:/file:/screenshot:/dom:


class RubricDeclaration(BaseModel):
    rubric: str
    veto: bool = False


class GraderDeclaration(BaseModel):
    impl: str
    layer: Literal["outcome", "trajectory", "both"] = "outcome"
    veto: bool = False
    version: str | None = None


class VerdictSpec(BaseModel):
    requirements: list[str] = Field(min_length=1)
    graders: dict[str, GraderDeclaration] | list[GraderDeclaration] = Field(
        default_factory=dict
    )

    def resolved_graders(self) -> list[GraderDeclaration]:
        if isinstance(self.graders, dict):
            out = [self.graders["default"]] if "default" in self.graders else []
            for key, g in self.graders.items():
                if key != "default":
                    out.append(g)
            return out
        return list(self.graders)


class MetricDeclaration(BaseModel):
    id: str
    kind: Literal["pass_pow_k", "cost_normalized", "exclusion_rate"]
    k: int | None = None


class DriverSpec(BaseModel):
    require: list[str] = Field(default_factory=list)


class ProvenanceInfo(BaseModel):
    source: str
    source_url: str | None = None
    original_id: str | None = None
    imported_at: str | None = None
    converter_version: str | None = None
    license: Literal["CC0", "MIT", "Apache-2.0", "NONE_DECLARED", "UNKNOWN"]
    data_imported: bool = False
    rewritten_by_us: bool = False

    @field_validator("license")
    @classmethod
    def _license_gate(cls, v: str, info) -> str:
        if v in ("NONE_DECLARED", "UNKNOWN"):
            data = info.data or {}
            if data.get("data_imported"):
                raise SuiteError(
                    "provenance with license NONE_DECLARED/UNKNOWN must set "
                    "data_imported=false — format skeleton only, no data"
                )
        return v


class HarborInputs(BaseModel):
    """Resolved references to Harbor-native declarations (not copies)."""

    dataset: str
    job: str
    dataset_digest: str | None = None
    job_digest: str | None = None


class SuiteOverlay(BaseModel):
    schema_version: int
    id: str
    version: str
    harbor: HarborInputs
    image: dict[str, ImagePinAction] = Field(default_factory=dict)
    baselines: list[BaselineAssertion] = Field(min_length=1)
    clock: ClockSpec
    observables: list[ObservableSpec] = Field(min_length=1)
    verdict: VerdictSpec
    metrics: list[MetricDeclaration] = Field(default_factory=list)
    driver: DriverSpec = Field(default_factory=DriverSpec)
    provenance: ProvenanceInfo

    @field_validator("schema_version")
    @classmethod
    def _schema(cls, v: int) -> int:
        if v != OVERLAY_SCHEMA_VERSION:
            raise SuiteError(
                f"suite.yaml schema_version: expected {OVERLAY_SCHEMA_VERSION}, got {v}"
            )
        return v


class ResolvedSuite(BaseModel):
    model_config = {"arbitrary_types_allowed": True}

    overlay: SuiteOverlay
    suite_dir: Any  # Path
    suite_yaml_digest: str

    @property
    def id(self) -> str:
        return self.overlay.id

    @property
    def version(self) -> str:
        return self.overlay.version

    def identity(self) -> tuple[str, str, str]:
        return (self.overlay.id, self.overlay.version, self.suite_yaml_digest)


def load_suite_yaml(path) -> dict[str, Any]:
    text = path.read_text(encoding="utf-8")
    data = yaml.safe_load(text)
    if not isinstance(data, dict):
        raise SuiteError(f"suite manifest must be a mapping: {path}")
    return data


def overlay_digest_of(path) -> str:
    """Stable digest of the suite.yaml file — part of run identity."""
    raw = path.read_bytes()
    return sha256(raw).hexdigest()
