"""Suite models: thin overlay over Harbor-native declarations.

A suite is a directory (plan §9.1): ``suite.yaml`` declares ONLY the
four things Harbor does not (baselines, clock, observables, verdict,
plus metrics/driver/provenance). Any fact Harbor already declares in
its own task.yaml/job.yaml must NOT appear here — duplication is a CI
error, not a merge.
"""

from __future__ import annotations

from hashlib import sha256
import json
import re
from typing import Any, Literal, Self

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from aeval.contracts import BudgetSnapshot

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
    "SuiteSource",
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
        # "budget" is deliberately NOT here: Harbor's JobConfig has no budget
        # field at all (verified), so there is nothing to defer to. A spend cap
        # is an evaluation fact and belongs in the suite overlay as
        # ``budget: {max_tokens: ...}``.
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


class _SuiteModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ImagePinAction(_SuiteModel):
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
        if v is not None and re.fullmatch(r"[^@\s]+@sha256:[0-9a-fA-F]{64}", v) is None:
            raise SuiteError(f"image.pin must be digest-pinned with a full sha256: {v!r}")
        return v

    @model_validator(mode="after")
    def _one_action(self) -> Self:
        if (self.pin is not None) == self.rebuild:
            raise SuiteError("image must set exactly one of pin or rebuild=true")
        return self


class BaselineAssertion(_SuiteModel):
    model_config = ConfigDict(populate_by_name=True)

    id: str
    probe: str | None = Field(default=None, min_length=1)
    equals: Any = None
    assert_expr: str | None = Field(default=None, alias="assert", min_length=1)

    @model_validator(mode="after")
    def _one_assertion(self) -> Self:
        if (self.probe is not None) == (self.assert_expr is not None):
            raise SuiteError(
                f"baseline {self.id!r} must set exactly one of probe or assert"
            )
        return self


class ClockSpec(_SuiteModel):
    mode: Literal["virtual_offset", "real"]
    epoch: str | None = None


class ObservableSpec(_SuiteModel):
    name: str
    type: Literal["string", "number", "boolean", "json"]
    source: str  # db:/file:/screenshot:/dom:


class RubricDeclaration(_SuiteModel):
    rubric: str
    veto: bool = False


class GraderDeclaration(_SuiteModel):
    impl: str
    layer: Literal["outcome", "trajectory", "both"] = "outcome"
    veto: bool = False
    version: str | None = None


class VerdictSpec(_SuiteModel):
    requirements: list[str] = Field(min_length=1)
    graders: (
        dict[str, GraderDeclaration | list[GraderDeclaration]] | list[GraderDeclaration]
    ) = Field(default_factory=dict)

    @field_validator("graders")
    @classmethod
    def _extra_graders_list(cls, graders):
        if isinstance(graders, dict):
            for key, grader in graders.items():
                if isinstance(grader, list) and key != "extra":
                    raise SuiteError(
                        f"verdict.graders.{key} must be a grader declaration; "
                        "only extra accepts a list"
                    )
        return graders

    def resolved_graders(self) -> list[GraderDeclaration]:
        if isinstance(self.graders, dict):
            out: list[GraderDeclaration] = []
            keys = ["default"] if "default" in self.graders else []
            keys.extend(key for key in self.graders if key != "default")
            for key in keys:
                grader = self.graders[key]
                out.extend(grader if isinstance(grader, list) else [grader])
            return out
        return list(self.graders)


class MetricDeclaration(_SuiteModel):
    id: str
    kind: Literal["pass_pow_k", "cost_normalized", "exclusion_rate"]
    k: int | None = None


class DriverSpec(_SuiteModel):
    require: list[str] = Field(default_factory=list)
    # Which session-record collect output this suite's evidence bundles use.
    # The slot generalised off the DSH-only hardcode: an adapter declares the
    # slot it can produce (contract ``SESSION_RECORD_OUTPUT``) and the suite
    # declares the slot its tasks' collect commands name — a pairing where
    # the two disagree is refused at composition, not discovered mid-collection.
    # Any well-formed slug is a declared slot (its adapter must then carry the
    # fixed path); the DEFAULT is untouched so every suite sealed before the
    # vocabulary opened keeps parsing and behaving byte-identically (I2).
    session_record: str = "dsh_session"

    @field_validator("session_record")
    @classmethod
    def _session_record_slot(cls, value: str) -> str:
        from aeval.agents.contract import session_record_slot_well_formed

        if not session_record_slot_well_formed(value):
            raise ValueError(
                "must be a lowercase slot slug (built-ins: dsh_session, "
                f"agent_session_record), got {value!r}"
            )
        return value
    # Stage the task's `tests/` directory into the sandbox right after the
    # agent ends and BEFORE evidence collection.
    #
    # Needed when the suite's collect command runs the task's own verifier
    # and the suite reads the reward it publishes: Terminal-Bench tasks
    # write /logs/verifier/reward.txt from tests/test.sh, and aeval
    # collects observables during the collect phase — but Harbor uploads
    # `tests/` only at verification time, i.e. AFTER collection. Staging
    # here closes that ordering gap. It does not leak the tests to the
    # agent: the upload happens once the agent has stopped.
    stage_tests_before_collect: bool = False
    # Working directory inside the sandbox for the agent's run — and for
    # the DSH session the control plugin mints. It must equal the cwd the
    # task's own verifier assumes, i.e. the environment Dockerfile's
    # WORKDIR: Terminal-Bench tasks hardcode absolute paths (`/app/...`,
    # and hello-world says "the current directory"), so an agent that runs
    # in the owner's default `/workspace` writes its answer where the
    # tests will never look (measured on the pilot: every trial failed
    # with `FileNotFoundError: /app/hello.txt`).
    #
    # It is one value, not two: DSH refuses to resume a session whose
    # recorded cwd differs from the run's, so the mint and the run must
    # agree.
    workspace_dir: str = "/workspace"
    # Per-flavor options for the in-sandbox control stack, namespaced by the
    # registered flavor name (``control_stack``). The framework never
    # interprets them: the flavor that owns the namespace validates its own
    # keys and values (fail-closed), so a family-specific knob stays a family
    # fact instead of becoming a framework field only one agent ever reads —
    # DSH's permission mode used to be ``driver.sandbox_mode`` here.
    #
    # Example (DSH, whose sealed pilot image ships no bwrap/Landlock runner, so
    # its shell would refuse every command without this):
    #
    #     control_options:
    #       dsh: {permission_mode: danger-full-access}
    control_options: dict[str, dict[str, Any]] = Field(default_factory=dict)

    @field_validator("control_options")
    @classmethod
    def _option_namespaces(
        cls, value: dict[str, dict[str, Any]]
    ) -> dict[str, dict[str, Any]]:
        import re as _re

        for name, options in value.items():
            if not _re.match(r"^[a-z][a-z0-9-]*$", str(name)):
                raise ValueError(
                    f"control_options names {name!r}; namespaces are control "
                    "flavor names (lowercase slugs)"
                )
            if not isinstance(options, dict):
                raise ValueError(
                    f"control_options.{name} must be a mapping of option -> value"
                )
        return value


class ProvenanceInfo(_SuiteModel):
    source: str
    source_url: str | None = None
    original_id: str | None = None
    imported_at: str | None = None
    converter_version: str | None = None
    license: Literal["CC0", "MIT", "Apache-2.0", "NONE_DECLARED", "UNKNOWN"]
    data_imported: bool = False
    rewritten_by_us: bool = False

    @field_validator("license", mode="before")
    @classmethod
    def _normalize_license(cls, v: Any) -> Any:
        return "CC0" if v == "CC0-1.0" else v

    @model_validator(mode="after")
    def _license_gate(self) -> Self:
        if self.license in ("NONE_DECLARED", "UNKNOWN") and self.data_imported:
            raise SuiteError(
                "provenance with license NONE_DECLARED/UNKNOWN must set "
                "data_imported=false — format skeleton only, no data"
            )
        return self


class HarborInputs(_SuiteModel):
    """Resolved references to Harbor-native declarations (not copies)."""

    dataset: str
    job: str
    dataset_digest: str | None = None
    job_digest: str | None = None


class SuiteOverlay(_SuiteModel):
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
    # Optional spend cap for a trial (max_tokens/max_seconds/max_steps). Declaring
    # one makes the enforcement point load-bearing: an adapter that cannot be
    # metered is refused at run start (see aeval.agents.contract.budget_gate_violation).
    budget: BudgetSnapshot | None = None
    provenance: ProvenanceInfo

    @field_validator("schema_version")
    @classmethod
    def _schema(cls, v: int) -> int:
        if v != OVERLAY_SCHEMA_VERSION:
            raise SuiteError(
                f"suite.yaml schema_version: expected {OVERLAY_SCHEMA_VERSION}, got {v}"
            )
        return v


class SuiteSource(_SuiteModel):
    """One manifest in a suite's inheritance chain (base first, child last).

    ``path`` is portable and relative to the suites root; ``digest`` is the
    sha256 of that file's raw bytes. The chain is what makes an inherited
    fact visible to run identity: editing a base changes every dependent
    suite's ``overlay_chain_digest``.
    """

    path: str
    digest: str
    role: Literal["base", "child"]


class ResolvedSuite(_SuiteModel):
    model_config = {"arbitrary_types_allowed": True}

    overlay: SuiteOverlay
    suite_dir: Any  # Path
    # Raw bytes of THIS suite's suite.yaml. Kept unchanged (and still computed)
    # so evidence sealed before inheritance existed can be recomputed.
    suite_yaml_digest: str
    # Digest of the resolved overlay PLUS every source file in the chain.
    # This is what identity and comparability use.
    overlay_chain_digest: str = ""
    sources: list[SuiteSource] = Field(default_factory=list)

    @property
    def id(self) -> str:
        return self.overlay.id

    @property
    def version(self) -> str:
        return self.overlay.version

    @property
    def extends(self) -> list[str]:
        """Base files this suite inherits from, in application order."""
        return [source.path for source in self.sources if source.role == "base"]

    def identity(self) -> tuple[str, str, str]:
        return (self.overlay.id, self.overlay.version, self.identity_digest)

    @property
    def identity_digest(self) -> str:
        """Chain digest, falling back to the raw file digest for legacy values."""
        return self.overlay_chain_digest or self.suite_yaml_digest

    def resolved_payload(self) -> str:
        """Canonical JSON of the resolved overlay declaration.

        Part of the chain digest; exposed so the explanation page can show
        exactly what the merge produced.
        """
        return json.dumps(
            self.overlay.model_dump(mode="json", exclude_none=True),
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        )


def load_suite_yaml(path) -> dict[str, Any]:
    try:
        text = path.read_text(encoding="utf-8")
        data = yaml.safe_load(text)
    except (OSError, UnicodeError, yaml.YAMLError) as exc:
        raise SuiteError(f"Cannot read suite manifest {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise SuiteError(f"suite manifest must be a mapping: {path}")
    return data


def overlay_digest_of(path) -> str:
    """Stable digest of the suite.yaml file — part of run identity."""
    raw = path.read_bytes()
    return sha256(raw).hexdigest()
