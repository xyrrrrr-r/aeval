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

from pathlib import Path

from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from aeval.contracts import AdapterSpec, TranscriptCapability
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


class LaunchProfile(BaseModel):
    """How this adapter is launched in a deployment (Harbor agent entry minus the id).

    These are *deployment* facts, not framework facts: an install prefix or a
    timeout binds to a cluster, not to an agent's identity. Keeping them in the
    declaration is what lets a job file stop carrying an agent.
    """

    model_config = ConfigDict(extra="forbid")

    override_setup_timeout_sec: int | None = None
    kwargs: dict[str, Any] = Field(default_factory=dict)


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
    #: the framework's fixed logical-name table.
    artifacts: list[dict] = Field(default_factory=list)
    #: In-sandbox control stack needed, if any (absent = none).
    control_stack: str | None = None
    #: Named launch profiles (``default`` is used when none is named).
    launch: dict[str, LaunchProfile] = Field(default_factory=dict)

    def launch_entry(self, profile: str | None = None) -> dict[str, Any]:
        """The Harbor agent entry that selects this adapter.

        A job file describes the task arm; which agent drives it is not part of
        that description. This is the seam that removes a job file per pairing.
        """
        if not self.launch:
            return {"import_path": self.import_path}
        name = profile or ("default" if "default" in self.launch else next(iter(self.launch)))
        if name not in self.launch:
            raise SuiteError(
                f"agent {self.id!r} declares no launch profile {name!r}; "
                f"declared profiles: {sorted(self.launch)}"
            )
        chosen = self.launch[name]
        entry: dict[str, Any] = {"import_path": self.import_path}
        if chosen.override_setup_timeout_sec is not None:
            entry["override_setup_timeout_sec"] = chosen.override_setup_timeout_sec
        if chosen.kwargs:
            entry["kwargs"] = dict(chosen.kwargs)
        return entry

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
