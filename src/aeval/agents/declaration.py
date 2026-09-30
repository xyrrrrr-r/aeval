"""Declarative agent adapters: ``agents/<id>.yaml`` on the shared merge engine.

An adapter's facts live in one declaration that inherits from
``agents/_base/agent.base.yaml`` — the **same** engine and semantics as suite
declarations (``extends``/``remove``, deep-merged mappings, union lists, keyed
lists, depth cap, cycle detection). Onboarding an agent therefore costs a
declaration, not a core change.

The declaration is checked against the adapter class it names, so the two can
never drift silently: a disagreement is an error, never a silent winner. The
class stays the runtime source (gates read it), which is safe *because* the
declaration is proven to agree — and it means an adapter written before this
layer existed still runs unchanged.
"""

from __future__ import annotations

import re
from pathlib import Path, PurePosixPath

from importlib import import_module
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from aeval.contracts import AdapterSpec, TranscriptCapability
from aeval.agents.contract import (
    MODEL_ROUTING_ENV_SLOTS,
    MODEL_ROUTING_PROTOCOLS,
    session_record_slot_well_formed,
)
from aeval.suite_loader.inheritance import (
    MAX_INHERITANCE_DEPTH,
    merge_declarations,
    read_declaration,
)
from aeval.suite_models import SuiteError, SuiteSource

AGENT_DECLARATION_SCHEMA_VERSION = 1
AGENT_DECLARATIONS_DIRNAME = "agents"
AGENT_BASE_DIRNAME = "_base"
AGENT_BASE_FILENAME = "agent.base.yaml"
AGENT_DECLARATION_FILENAME = "agent.yaml"
# Consumed by this resolver, never inherited as facts.
RESOLVER_KEYS = ("extends", "remove")
# Lists that append and de-duplicate rather than replace.
AGENT_UNION_PATHS = frozenset({("provides",), ("observations",)})
# Keyed lists: same key replaces, new keys append.
AGENT_KEYED_LISTS = {"artifacts": "name"}
# The module-attribute prefix of a declaration's materialized adapter class
# (``aeval.agents.openai_acp:agent_<id>``).
_SYNTH_ATTR_PREFIX = "agent_"

# Materialized classes, keyed by the declaration's canonical JSON: resolving
# the same declaration twice yields the SAME class object, so identity checks
# and caches stay stable within a process.
_MATERIALIZED: dict[tuple[str, str], type] = {}


def declaration_slug(agent_id: str) -> str:
    """The module-attribute slug of a declaration's materialized class.

    Shared by both ends of the synth path: ``runtime_import_path`` builds
    ``<module>:agent_<slug>`` and the module's lazy ``__getattr__`` takes it
    apart again — one rule, defined here (the generic side), so the generic
    declaration machinery never imports a concrete adapter family.
    """
    return re.sub(r"[^a-z0-9_]", "_", agent_id.strip().lower())


def _materialized_class_name(agent_id: str) -> str:
    """``fake-openai`` -> ``FakeOpenai``; ``my-agent-v2`` -> ``MyAgentV2``."""
    parts = [part for part in re.split(r"[^0-9a-zA-Z]+", agent_id) if part]
    return "".join(part[:1].upper() + part[1:] for part in parts) or "DeclaredAgent"


def materialize_declaration_adapter(declaration: "AgentDeclaration") -> type:
    """The complete adapter class built from a declaration-driven base.

    The declaration's own facts become the class attributes every contract
    seam reads (``build_adapter_spec``, the evidence gates, conformance), so
    a declared agent needs zero adapter code (G11): the declaration names a
    behavior base (``OpenAiAcpAgent``) and carries every per-agent fact
    itself. The declaration↔class check then agrees by construction — what
    it validates for a materialized agent is that this synthesis is faithful.

    A declaration naming a PINNED adapter is refused here: pinned adapters
    are their own class (``declaration.adapter_class()`` returns it), and
    silently wrapping one would hide a drift the check exists to catch.
    """
    from aeval.agents.contract import load_adapter_class

    base = load_adapter_class(declaration.import_path)
    if not getattr(base, "DECLARATION_DRIVEN", False):
        raise SuiteError(
            f"declaration {declaration.id!r} names {declaration.import_path!r}, "
            "which is a pinned adapter class, not a declaration-driven base — "
            "use adapter_class() to select it"
        )
    key = (base.__name__, declaration.model_dump_json())
    cached = _MATERIALIZED.get(key)
    if cached is not None:
        return cached
    slot_entry = next(
        (entry for entry in declaration.artifacts if entry.slot is not None), None
    )
    routing = None
    if declaration.model_routing is not None:
        routing = {
            "agent_protocol": declaration.model_routing.agent_protocol,
            "env": dict(declaration.model_routing.env),
        }
    attrs: dict[str, Any] = {
        "ADAPTER_ID": declaration.id,
        "ADAPTER_VERSION": declaration.version,
        "ADAPTER_MODE": declaration.mode,
        "BUDGET_ENFORCEMENT": declaration.budget_enforcement,
        "WRITE_SURFACE": declaration.write_surface,
        "SERVER_SIDE_SESSION": declaration.server_side_session,
        "TRANSCRIPT_CAPABILITY": declaration.transcript,
        "PROVIDES": frozenset(declaration.provides),
        "REQUIRED_OBSERVATIONS": tuple(declaration.observations),
        "CONTROL_STACK": declaration.control_stack,
        "TERMINAL_DESCRIPTOR_OWNER": declaration.terminal_descriptor_owner,
        "SANDBOX_HOME": declaration.sandbox_home,
        "SESSION_ARTIFACT_DIR": declaration.session_artifact_dir,
        "SANDBOX_SESSION_RECORD": declaration.sandbox_session_record,
        "MODEL_ROUTING": routing,
        # a declared slot carries its fixed path with it (stage 3)
        "SESSION_RECORD_OUTPUT": (
            slot_entry.slot if slot_entry is not None else "agent_session_record"
        ),
        "SESSION_RECORD_OUTPUT_PATH": (
            slot_entry.path if slot_entry is not None else None
        ),
        "name": staticmethod(lambda did=declaration.id: did),
        "DECLARATION": declaration,
        # Present the materialized class as a member of the behavior base's
        # family (repr, adapter descriptions) rather than as a stray local of
        # this module.
        "__module__": getattr(base, "SYNTH_MODULE", base.__module__),
        "__qualname__": _materialized_class_name(declaration.id),
        "__doc__": (
            f"Declaration-materialized adapter for {declaration.id!r} "
            f"(base {base.__name__}); the declaration "
            f"{declaration.import_path!r} is the single source of its facts."
        ),
    }
    cls = type(_materialized_class_name(declaration.id), (base,), attrs)
    _MATERIALIZED[key] = cls
    return cls


class LaunchProfile(BaseModel):
    """How this adapter is launched in a deployment (Harbor agent entry minus the id).

    These are *deployment* facts, not framework facts: an install prefix or a
    timeout binds to a cluster, not to an agent's identity. Keeping them in the
    declaration is what lets a job file stop carrying an agent.
    """

    model_config = ConfigDict(extra="forbid")

    override_setup_timeout_sec: int | None = None
    kwargs: dict[str, Any] = Field(default_factory=dict)


class ModelRoutingDeclaration(BaseModel):
    """How the agent's model traffic reaches the broker (``model_routing``).

    ``agent_protocol`` names the wire the agent itself speaks
    (AGENT-ABSTRACTION-2 §4.1): ``gateway_native`` — the control stack's
    transport already speaks the broker wire, no facade involved — or
    ``openai_chat`` / ``openai_responses``, which the in-sandbox facade
    translates. ``env`` names the env VARIABLES the agent's runtime reads for
    the facade's base URL and key: the spellings are an agent fact (dcode
    reads ``OPENAI_BASE_URL``/``OPENAI_API_BASE``, the next agent may read
    something else), which is why they are declared instead of hardcoded.
    """

    model_config = ConfigDict(extra="forbid")

    agent_protocol: str
    env: dict[str, str] = Field(default_factory=dict)

    @field_validator("agent_protocol")
    @classmethod
    def _protocol_in_vocabulary(cls, value: str) -> str:
        if value not in MODEL_ROUTING_PROTOCOLS:
            raise ValueError(
                f"must be one of {sorted(MODEL_ROUTING_PROTOCOLS)}, got {value!r}"
            )
        return value

    @field_validator("env")
    @classmethod
    def _env_slots_and_names(cls, value: dict[str, str]) -> dict[str, str]:
        for slot, name in value.items():
            if slot not in MODEL_ROUTING_ENV_SLOTS:
                raise ValueError(
                    f"env names unknown slot {slot!r}; known slots: "
                    f"{list(MODEL_ROUTING_ENV_SLOTS)}"
                )
            if not name.isidentifier():
                raise ValueError(
                    f"env.{slot} must be an environment variable name, got {name!r}"
                )
        return value

    @model_validator(mode="after")
    def _openai_needs_both_spellings(self) -> "ModelRoutingDeclaration":
        if self.agent_protocol != "gateway_native":
            for required in ("base_url", "api_key"):
                if required not in self.env:
                    raise ValueError(
                        f"protocol {self.agent_protocol!r} needs env.{required} "
                        "(the env var the agent's runtime reads for the facade)"
                    )
        return self


class AgentArtifactDeclaration(BaseModel):
    """One evidence artifact this adapter produces (``artifacts:`` entry).

    An entry carrying ``slot`` declares the fixed trial-dir path of a
    session-record collect slot — the declaration's mirror of the adapter
    class's ``SESSION_RECORD_OUTPUT_PATH`` (the class stays the runtime
    source; the declaration is validated against it, so the two cannot
    drift). Entries without ``slot`` are free-form artifact facts (name and
    where it lands), for artifacts beyond the session record.
    """

    model_config = ConfigDict(extra="forbid")

    name: str
    slot: str | None = None
    path: str

    @field_validator("name")
    @classmethod
    def _name_required(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("an artifact entry needs a non-empty name")
        return value

    @field_validator("slot")
    @classmethod
    def _slot_well_formed(cls, value: str | None) -> str | None:
        if value is None:
            return value
        if not session_record_slot_well_formed(value):
            raise ValueError(
                f"slot must be a lowercase slot slug, got {value!r}"
            )
        return value

    @field_validator("path")
    @classmethod
    def _path_inside_trial_dir(cls, value: str) -> str:
        candidate = PurePosixPath(value)
        if value.startswith("/") or not value.strip() or ".." in candidate.parts:
            raise ValueError(
                f"path must be relative and inside the trial dir, got {value!r}"
            )
        return value


class AgentDeclaration(BaseModel):
    """The declared facts of one agent adapter (mirrors ``AdapterSpec``)."""

    model_config = ConfigDict(extra="forbid")

    schema_version: int
    id: str
    version: str
    import_path: str
    provides: list[str] = Field(default_factory=list)
    observations: list[str] = Field(default_factory=list)
    mode: str = "installed_cli"
    budget_enforcement: str = "none"
    write_surface: str = "ephemeral_overlay"
    server_side_session: str = "forbidden"
    transcript: TranscriptCapability
    sandbox_home: str | None = None
    session_artifact_dir: str | None = None
    #: Evidence artifacts this adapter produces, declared rather than assumed by
    #: the framework's fixed logical-name table. An entry with ``slot``
    #: declares the fixed path of a session-record collect slot.
    artifacts: list[AgentArtifactDeclaration] = Field(default_factory=list)
    #: In-sandbox control stack needed, if any (absent = none).
    control_stack: str | None = None
    #: Which component states the trial's terminal outcome and therefore writes
    #: the bundle descriptor (aeval.agents.contract): ``sandbox`` when the stack
    #: owns the session, ``host`` when it only proxies model traffic.
    terminal_descriptor_owner: str = "sandbox"
    #: The sandbox file holding the official session record, when it is one
    #: known file (so the owner can observe its identity before log download).
    sandbox_session_record: str | None = None
    #: How the agent's model traffic reaches the broker (absent = the agent
    #: declares no routing and its control stack, if any, is gateway-native).
    model_routing: ModelRoutingDeclaration | None = None
    #: Named launch profiles (``default`` is used when none is named).
    launch: dict[str, LaunchProfile] = Field(default_factory=dict)

    def launch_entry(self, profile: str | None = None) -> dict[str, Any]:
        """The Harbor agent entry that selects this adapter.

        A job file describes the task arm; which agent drives it is not part of
        that description. This is the seam that removes a job file per pairing.

        A declaration whose ``import_path`` names a declaration-driven base
        (``DECLARATION_DRIVEN``, e.g. ``OpenAiAcpAgent``) launches the
        MATERIALIZED class — the complete adapter built from this declaration —
        not the base, so every import_path consumer (Harbor's launcher, the
        plugin's contract check, the composition pairing) sees one and the
        same complete adapter.
        """
        entry_import = self.runtime_import_path()
        if not self.launch:
            return {"import_path": entry_import}
        name = profile or ("default" if "default" in self.launch else next(iter(self.launch)))
        if name not in self.launch:
            raise SuiteError(
                f"agent {self.id!r} declares no launch profile {name!r}; "
                f"declared profiles: {sorted(self.launch)}"
            )
        chosen = self.launch[name]
        entry: dict[str, Any] = {"import_path": entry_import}
        if chosen.override_setup_timeout_sec is not None:
            entry["override_setup_timeout_sec"] = chosen.override_setup_timeout_sec
        if chosen.kwargs:
            entry["kwargs"] = dict(chosen.kwargs)
        return entry

    def runtime_import_path(self) -> str:
        """The import_path the runtime should resolve for this declaration.

        A pinned adapter (``…:DcodeAgent``) is its own import path. A
        declaration-driven base is not a complete agent, so the runtime
        resolves the materialized class instead — importable as
        ``<SYNTH_MODULE>:agent_<id>`` (materialized lazily from this very
        declaration, in any process). An unresolvable import_path is returned
        as-is: the composition and conformance checks refuse it with their
        own precise errors.
        """
        from aeval.agents.contract import load_adapter_class

        try:
            base = load_adapter_class(self.import_path)
        except Exception:  # noqa: BLE001 - unresolvable paths fail at their own seam
            return self.import_path
        if not getattr(base, "DECLARATION_DRIVEN", False):
            return self.import_path
        synth_module = getattr(base, "SYNTH_MODULE", None)
        if not isinstance(synth_module, str) or not synth_module:
            raise SuiteError(
                f"declaration {self.id!r} names the declaration-driven base "
                f"{self.import_path!r}, which declares no SYNTH_MODULE — the "
                "materialized class would not be importable"
            )
        return f"{synth_module}:{_SYNTH_ATTR_PREFIX}{declaration_slug(self.id)}"

    def adapter_class(self) -> type:
        """The complete adapter class this declaration selects at runtime.

        A pinned import_path IS the class. A declaration-driven base is
        materialized: this declaration's own facts become the class
        attributes every contract seam reads (spec, gates, conformance), so
        the runtime source of a declared agent's facts is a class built from
        the declaration — zero adapter code (AGENT-ABSTRACTION-2 G11).
        """
        from aeval.agents.contract import load_adapter_class

        base = load_adapter_class(self.import_path)
        if not getattr(base, "DECLARATION_DRIVEN", False):
            return base
        return materialize_declaration_adapter(self)

    def to_adapter_spec(self) -> AdapterSpec:
        """Validate and convert to the runtime spec (enforces the vocabularies)."""
        return AdapterSpec(
            id=self.id,
            version=self.version,
            impl=self.import_path,
            impl_version=self.version,
            mode=self.mode,  # type: ignore[arg-type]
            transcript=self.transcript,
            budget_enforcement=self.budget_enforcement,  # type: ignore[arg-type]
            write_surface=self.write_surface,  # type: ignore[arg-type]
            server_side_session=self.server_side_session,  # type: ignore[arg-type]
        )


class ResolvedAgentDeclaration(BaseModel):
    model_config = ConfigDict(arbitrary_types_allowed=True, extra="forbid")

    declaration: AgentDeclaration
    sources: list[SuiteSource]
    path: Path


def default_agents_root(start: Path | None = None) -> Path:
    """The ``agents`` directory declarations resolve ``extends`` against.

    Walks up for an ancestor directory literally named ``agents`` (repo layout);
    falls back to ``<cwd>/agents``. Kept explicit so a caller can always name the
    root instead of relying on discovery.
    """
    here = Path(start or Path.cwd()).resolve()
    for candidate in (here, *here.parents):
        if candidate.name == AGENT_DECLARATIONS_DIRNAME:
            return candidate
    return here / AGENT_DECLARATIONS_DIRNAME


def _declaration_path(root: Path, reference: str) -> Path:
    """Resolve an ``extends`` reference or a declaration path inside ``root``."""
    raw = Path(reference)
    if raw.is_absolute() or ".." in raw.parts:
        raise SuiteError(f"agent declaration path must be relative and traversal-free: {reference!r}")
    path = (root / raw).resolve()
    if not path.is_relative_to(root.resolve()):
        raise SuiteError(f"agent declaration escapes the agents root: {reference!r}")
    if path.suffix in (".yaml", ".yml"):
        return path
    if path.is_dir():
        return path / AGENT_DECLARATION_FILENAME
    return path


def _extends_of(declaration: dict, path: Path) -> list[str]:
    value = declaration.get("extends")
    if value is None:
        return []
    items = value if isinstance(value, list) else [value]
    out = []
    for item in items:
        if not isinstance(item, str) or not item.strip():
            raise SuiteError(f"{path}: extends entries must be non-empty strings")
        out.append(item.strip())
    return out


def _remove_of(declaration: dict, path: Path) -> dict[str, list[str]]:
    value = declaration.get("remove")
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise SuiteError(f"{path}: remove must be a mapping of key -> entries")
    out: dict[str, list[str]] = {}
    for key, entries in value.items():
        items = entries if isinstance(entries, list) else [entries]
        for item in items:
            if not isinstance(item, str) or not item.strip():
                raise SuiteError(f"{path}: remove.{key} entries must be non-empty strings")
        out[str(key)] = [str(item).strip() for item in items]
    return out


def _apply_remove(merged: dict, remove: dict[str, list[str]], path: Path) -> None:
    """Drop inherited entries. A missing entry is an error, never a no-op."""
    for key, entries in remove.items():
        if key not in merged:
            raise SuiteError(f"{path}: remove names an inherited key that is absent: {key!r}")
        current = merged[key]
        if isinstance(current, list):
            for item in entries:
                if item not in current:
                    raise SuiteError(f"{path}: remove.{key} names an inherited entry that is absent: {item!r}")
                current.remove(item)
        else:
            del merged[key]


def _resolve(
    path: Path, root: Path, chain: tuple[Path, ...], sources: list[SuiteSource]
) -> dict:
    if len(chain) >= MAX_INHERITANCE_DEPTH:
        raise SuiteError(
            f"{path}: agent declaration inheritance is deeper than {MAX_INHERITANCE_DEPTH}"
        )
    if path in chain:
        cycle = " -> ".join(item.name for item in (*chain, path))
        raise SuiteError(f"{path}: agent declaration inheritance cycle: {cycle}")
    declaration = read_declaration(path)
    merged: dict = {}
    for reference in _extends_of(declaration, path):
        base_path = _declaration_path(root, reference)
        if not base_path.is_file():
            raise SuiteError(f"{path}: extends references a missing agent declaration: {reference!r}")
        relative = base_path.relative_to(root)
        if not any(part.startswith("_") for part in relative.parts):
            raise SuiteError(
                f"{path}: an agent declaration may only extend a base file (a path under "
                f"an _-prefixed directory such as {AGENT_BASE_DIRNAME}/), never another "
                f"agent's own declaration: {reference!r}"
            )
        base_sources: list[SuiteSource] = []
        merged = merge_declarations(
            merged,
            _resolve(base_path, root, (*chain, path), base_sources),
            union_paths=AGENT_UNION_PATHS,
            keyed_lists=AGENT_KEYED_LISTS,
        )
        for source in base_sources:
            if source.path not in {item.path for item in sources}:
                sources.append(source)
    merged = merge_declarations(
        merged, declaration, union_paths=AGENT_UNION_PATHS, keyed_lists=AGENT_KEYED_LISTS
    )
    _apply_remove(merged, _remove_of(declaration, path), path)
    for key in RESOLVER_KEYS:
        merged.pop(key, None)
    digest = _file_digest(path)
    # `SuiteSource.role` speaks the suite vocabulary ("base"/"child"); an agent
    # declaration is a child declaration in that sense. The display path says
    # which layer it came from.
    sources.append(
        SuiteSource(
            path=_display_path(path, root),
            digest=digest,
            role="base" if path.name == AGENT_BASE_FILENAME else "child",
        )
    )
    return merged


def _display_path(path: Path, root: Path) -> str:
    try:
        return path.resolve().relative_to(root.resolve()).as_posix()
    except ValueError:
        return path.name


def _file_digest(path: Path) -> str:
    from hashlib import sha256

    return sha256(path.read_bytes()).hexdigest()


def resolve_agent_declaration(
    path: Path, *, agents_root: Path | None = None
) -> ResolvedAgentDeclaration:
    """Read one ``agents/<id>.yaml`` (or base) and resolve its inheritance chain."""
    path = Path(path)
    if not path.is_absolute():
        path = (Path.cwd() / path).resolve()
    if not path.is_file():
        raise SuiteError(f"Cannot read agent declaration {path}: not a file")
    root = Path(agents_root).resolve() if agents_root else default_agents_root(path.parent)
    sources: list[SuiteSource] = []
    merged = _resolve(path, root, (), sources)
    declaration = AgentDeclaration.model_validate(merged)
    if declaration.schema_version != AGENT_DECLARATION_SCHEMA_VERSION:
        raise SuiteError(
            f"{path}: schema_version: expected {AGENT_DECLARATION_SCHEMA_VERSION}, "
            f"got {declaration.schema_version}"
        )
    # convert once here so a bad vocabulary fails at load, not at run start
    declaration.to_adapter_spec()
    return ResolvedAgentDeclaration(declaration=declaration, sources=sources, path=path)


def declaration_paths(root: Path) -> list[Path]:
    """Every agent declaration under ``root``, in a stable order.

    Two layouts are supported — ``<root>/<id>.yaml`` and ``<root>/<id>/agent.yaml``
    — and any path starting with ``_`` is a base or a fragment, never an agent.
    """
    root = Path(root)
    if not root.is_dir():
        return []
    found = [path for path in root.rglob("*.yaml") if path.name != AGENT_BASE_FILENAME]
    paths = []
    for path in sorted(found):
        relative = path.relative_to(root)
        if any(part.startswith("_") for part in relative.parts):
            continue
        if path.name != AGENT_DECLARATION_FILENAME and len(relative.parts) != 1:
            continue
        paths.append(path)
    return paths


def discover_agent_declarations(root: Path) -> list[str]:
    """Agent ids declared under ``root`` (``_``-prefixed paths are never agents)."""
    root = Path(root)
    ids = []
    for path in declaration_paths(root):
        relative = path.relative_to(root)
        ids.append(relative.parts[0] if len(relative.parts) > 1 else path.stem)
    return ids


def find_declaration_for(import_path: str, *, agents_root: Path) -> ResolvedAgentDeclaration | None:
    """The declaration that names ``import_path`` (None when none does)."""
    root = Path(agents_root)
    if not root.is_dir():
        return None
    for path in declaration_paths(root):
        resolved = resolve_agent_declaration(path, agents_root=root)
        if resolved.declaration.import_path == import_path:
            return resolved
    return None


def declaration_class_mismatches(
    declaration: AgentDeclaration, adapter: type
) -> list[str]:
    """Where a declaration and the adapter class it names disagree."""
    expected = declaration.to_adapter_spec()
    from aeval.agents.contract import (
        adapter_declaration_gap,
        build_adapter_spec,
        capabilities_of,
        declared_observations,
    )

    gap = adapter_declaration_gap(adapter)
    if gap:
        return [f"adapter declares no {gap}"]
    declared = build_adapter_spec(adapter)
    mismatches = []
    for field in ("id", "version", "mode", "budget_enforcement", "write_surface", "server_side_session"):
        left, right = getattr(declaration, field, None), getattr(declared, field, None)
        if field == "version":
            # the declaration states the adapter's own version; an instance reports
            # its observed version, which the record carries separately
            continue
        if left != right:
            mismatches.append(f"{field}: declaration={left!r} adapter={right!r}")
    if expected.transcript != declared.transcript:
        mismatches.append(
            f"transcript: declaration={expected.transcript!r} adapter={declared.transcript!r}"
        )
    declared_provides = capabilities_of(adapter)
    if frozenset(declaration.provides) != declared_provides:
        mismatches.append(
            f"provides: declaration={sorted(declaration.provides)} adapter={sorted(declared_provides)}"
        )
    declared_obs = declared_observations(adapter)
    if frozenset(declaration.observations) != declared_obs:
        mismatches.append(
            f"observations: declaration={sorted(declaration.observations)} adapter={sorted(declared_obs)}"
        )
    from aeval.agents.contract import control_stack_of as _control_stack_of

    if declaration.control_stack != (_control_stack_of(adapter)):
        mismatches.append(
            f"control_stack: declaration={declaration.control_stack!r} "
            f"adapter={_control_stack_of(adapter)!r}"
        )
    # The routing declaration and the class attribute must agree field by
    # field: the deployment derives the facade's served endpoints and the
    # agent's injected env from them, so a silent winner would deploy a
    # facade the agent never calls (or point the agent at one that serves a
    # different wire).
    from aeval.agents.contract import model_routing_of as _model_routing_of

    routing = _model_routing_of(adapter)
    if declaration.model_routing is None:
        if routing is not None:
            mismatches.append(
                f"model_routing: declaration=None adapter={routing.agent_protocol!r}"
            )
    elif routing is None:
        mismatches.append(
            f"model_routing: declaration={declaration.model_routing.agent_protocol!r} "
            "adapter=None"
        )
    else:
        if declaration.model_routing.agent_protocol != routing.agent_protocol:
            mismatches.append(
                "model_routing.agent_protocol: "
                f"declaration={declaration.model_routing.agent_protocol!r} "
                f"adapter={routing.agent_protocol!r}"
            )
        if dict(declaration.model_routing.env) != dict(routing.env):
            mismatches.append(
                f"model_routing.env: declaration={sorted(declaration.model_routing.env.items())} "
                f"adapter={sorted(routing.env.items())}"
            )
    contract = import_module("aeval.agents.contract")
    for name, declared, on_class in (
        ("control_stack", declaration.control_stack, _control_stack_of(adapter)),
        (
            "terminal_descriptor_owner",
            declaration.terminal_descriptor_owner,
            contract.terminal_descriptor_owner(adapter),
        ),
        (
            "sandbox_session_record",
            declaration.sandbox_session_record,
            contract.sandbox_session_record(adapter),
        ),
    ):
        if declared != on_class:
            mismatches.append(
                f"{name}: declaration={declared!r} adapter={on_class!r}"
            )
    # The session-record slot's fixed path: the declaration mirrors it as an
    # ``artifacts:`` slot entry, the class carries it as
    # SESSION_RECORD_OUTPUT_PATH. Presence and value must agree — the
    # collector and the evidence gate read the class, onboarding reads the
    # declaration, and a silent winner would put the record where the other
    # side never looks.
    declared_slot_path = next(
        (entry.path for entry in declaration.artifacts if entry.slot is not None),
        None,
    )
    adapter_slot_path = contract.session_record_output_path_of(adapter)
    if declared_slot_path != adapter_slot_path:
        mismatches.append(
            f"artifacts slot path: declaration={declared_slot_path!r} "
            f"adapter={adapter_slot_path!r}"
        )
    declared_slot = next(
        (entry.slot for entry in declaration.artifacts if entry.slot is not None),
        None,
    )
    if declared_slot is not None and declared_slot != contract.session_record_output_of(
        adapter
    ):
        mismatches.append(
            f"artifacts slot: declaration={declared_slot!r} "
            f"adapter={contract.session_record_output_of(adapter)!r}"
        )
    for attr, value in (
        ("SANDBOX_HOME", declaration.sandbox_home),
        ("SESSION_ARTIFACT_DIR", declaration.session_artifact_dir),
    ):
        on_class = getattr(adapter, attr, None)
        if on_class != value:
            mismatches.append(f"{attr}: declaration={value!r} adapter={on_class!r}")
    return mismatches


def check_declaration_matches_adapter(
    declaration: AgentDeclaration, adapter: type
) -> None:
    """Refuse a declaration that disagrees with the adapter class it names."""
    mismatches = declaration_class_mismatches(declaration, adapter)
    if mismatches:
        raise SuiteError(
            f"agent declaration for {declaration.id!r} disagrees with "
            f"{declaration.import_path}: " + "; ".join(mismatches)
        )
