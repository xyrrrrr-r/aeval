"""Scaffold a new agent adapter: the first two onboarding steps as one command.

Writing an adapter means getting the contract right — members, declarations, a
transcript reader, and a declaration file that agrees with the class. Getting it
wrong is not a loud failure: the run starts and dies in the middle of evidence
collection. This generator emits a skeleton that already satisfies the contract,
so the only thing left is the part nobody can generate: reading the agent's own
session format.

The generated ``read_trial_session`` raises ``NotImplementedError`` on purpose.
Conformance then reports the transcript check as **skipped with that reason** —
never as passed — which is exactly the honest state of a new adapter.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from importlib.resources import files as _resource_files
from pathlib import Path

from aeval.agents.declaration import AGENT_BASE_FILENAME, resolve_agent_declaration
from aeval.suite_models import SuiteError

DEFAULT_PACKAGE_DIRNAME = Path("src/aeval/agents")

#: The declarations a fresh project needs to run the shipped DSH adapter:
#: the base every declaration extends, the runtime→image table composition
#: checks, and the adapter itself. ``aeval agents --init`` writes these.
STARTER_DECLARATIONS = (
    "_base/agent.base.yaml",
    "_runtime/images.yaml",
    "dsh.yaml",
)

#: Where the build puts a copy of the repo's ``agents/`` (pyproject
#: ``force-include``), i.e. where an installed aeval reads the starters from.
SHIPPED_TEMPLATES = Path("_templates") / "agents"

_MODES = ("installed_cli", "acp_stdio", "sdk_jsonrpc")
_BUDGETS = ("none", "gateway_lease", "wallclock_kill")
_WRITE_SURFACES = ("ephemeral_overlay", "persistent")
_TRANSCRIPT_SOURCES = ("atif_native", "native_session_via_bridge")


@dataclass
class ScaffoldResult:
    agent_id: str
    declaration_path: Path
    module_path: Path
    import_path: str = ""
    files: list[Path] = field(default_factory=list)

    def render(self) -> str:
        lines = [f"scaffolded agent {self.agent_id}:"]
        for path in self.files:
            lines.append(f"  wrote {path}")
        # Printed because it is the one generated fact that depends on where you
        # put the module, and the one a run fails on if it is wrong.
        lines.append(f"  import path: {self.import_path}")
        lines.append(
            "next: implement read_trial_session(), then run "
            f"`aeval agents` and `aeval conformance --agent {self.declaration_path}`"
        )
        return "\n".join(lines)


def starter_templates_dir() -> Path | None:
    """The ``agents/`` tree the starters are copied from, or None.

    An installed aeval has no ``agents/`` beside it: the build copies the
    repo's declarations into the package (pyproject ``force-include``), and
    that copy is authoritative. A development checkout has no such copy, so
    the repo's own ``agents/`` — the very directory the copy is made from — is
    used instead. Both are the same bytes by construction.
    """
    installed = Path(_resource_files("aeval")) / SHIPPED_TEMPLATES
    if (installed / "dsh.yaml").is_file():
        return installed
    for parent in Path(__file__).resolve().parents:
        candidate = parent / "agents"
        if (candidate / "dsh.yaml").is_file():
            return candidate
    return None


def write_starter_declarations(target_dir: Path, *, force: bool = False) -> list[Path]:
    """Write the shipped starter declarations into ``target_dir``.

    A pip install ships no declarations, and they are the operator's input
    (``--agents-dir``), not ours to place silently. Without this the first
    command a new user runs is ``aeval agents`` failing with "no agent
    declarations under agents".
    """
    source = starter_templates_dir()
    if source is None:
        raise SuiteError(
            "no shipped agent declarations found — this aeval has no "
            f"{SHIPPED_TEMPLATES} and no repo agents/ above it"
        )
    written: list[Path] = []
    for relative in STARTER_DECLARATIONS:
        origin = source / relative
        if not origin.is_file():
            raise SuiteError(f"shipped declaration is missing: {origin}")
        destination = Path(target_dir) / relative
        if destination.exists() and not force:
            raise SuiteError(
                f"{destination} exists — pass --force to overwrite it"
            )
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(origin.read_bytes())
        written.append(destination)
    return written


def _class_name(agent_id: str) -> str:
    parts = [part for part in agent_id.replace("-", "_").split("_") if part]
    return "".join(part[:1].upper() + part[1:] for part in parts) + "Agent"


def _module_source(
    agent_id: str, *, mode: str, provides: list[str], budget: str,
    transcript_source: str,
) -> str:
    class_name = _class_name(agent_id)
    provides_src = ", ".join(f'"{item}"' for item in sorted(provides))
    return f'''"""The {agent_id} adapter.

Generated skeleton: the contract is already satisfied, so the framework will
record this adapter, check its capabilities before a sandbox exists, and hold it
to its declared budget enforcement. What is left is the part only you can write:
turning this agent's own session format into ATIF.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from aeval.contracts import CanonicalTranscript, TranscriptCapability


class {class_name}:
    """Adapter for {agent_id}."""

    # --- declarations the framework reads at run start -----------------------
    ADAPTER_ID = "{agent_id}"
    ADAPTER_VERSION = "1"
    ADAPTER_MODE = "{mode}"
    PROVIDES = frozenset({{{provides_src}}})
    BUDGET_ENFORCEMENT = "{budget}"
    WRITE_SURFACE = "ephemeral_overlay"
    SERVER_SIDE_SESSION = "forbidden"
    # Must agree with this adapter's `transcript:` block in
    # agents/{agent_id}.yaml, field for field: a declaration that drifts from
    # its class is refused at load, not silently obeyed.
    # `unavailable` is the honest starting point: nothing has been observed yet,
    # and a fresh adapter must not claim a field it has not been shown to expose.
    TRANSCRIPT_CAPABILITY = TranscriptCapability(
        source="{transcript_source}",
        reader="{agent_id}-reader",
        capabilities=["{transcript_source}"],
        fields_available={{"events": "unavailable", "token_usage": "unavailable"}},
    )
    # The collect slot your official session record belongs to; must match
    # the suite's driver.session_record.
    SESSION_RECORD_OUTPUT = "agent_session_record"
    # Facts you must observe in the live sandbox and bind to the lock.
    REQUIRED_OBSERVATIONS = ()

    # --- members the evidence chain reads at trial end ----------------------
    name = "{agent_id}"
    version = "1"

    def __init__(self, **kwargs):
        self.kwargs = dict(kwargs)
        # Your run() sets this once the agent has started a conversation.
        self._agent_session_id: str | None = None

    @property
    def agent_session_id(self) -> str | None:
        """This agent's own conversation session id, never Harbor's sandbox id."""
        return self._agent_session_id

    def paths(self) -> Any:
        """Where this trial's logs live, in both views.

        Adapt the fields to your runner: an installed CLI usually receives its
        log directory through the constructor kwargs.
        """
        logs_dir = Path(self.kwargs.get("logs_dir", "."))
        return type(
            "{class_name}Paths", (), {{"logs_dir": logs_dir, "session_root": logs_dir}}
        )()

    def read_session_record(self) -> bytes:
        """Bytes of this agent's official session record.

        Until this works, collection fails closed with this message rather
        than recording a fabricated record.
        """
        raise NotImplementedError(
            "read the {agent_id} official session record and return its bytes"
        )

    def read_trial_session(self) -> CanonicalTranscript:
        """Turn this agent's session artifact into ATIF.

        Until this works, conformance reports the transcript check as skipped
        with this message rather than pretending the adapter is ready.
        """
        raise NotImplementedError(
            "read the {agent_id} session format and map it to ATIF "
            "(events, token usage, stop reason)"
        )
'''


def _derive_import_path(module_dir: Path, class_name: str) -> str:
    """The dotted path that actually reaches the module just written.

    Derived from where the files landed rather than assumed: ``--package-dir
    src/aeval/agents`` (the default) and ``--package-dir src`` put the SAME
    module under different source roots — ``aeval.agents.<id>.agent`` versus
    ``<id>.agent`` — and a declaration naming the wrong one fails at import
    with "No module named ...". The walk stops at the first directory that is
    not a package, which is exactly the import root.
    """
    parts: list[str] = []
    # Absolute, and with an explicit root guard: walking up from a *relative*
    # path never terminates once it reaches ".", because Path(".").parent is
    # Path(".") and its .name is "".
    current = Path(module_dir).resolve()
    while (current / "__init__.py").is_file():
        parts.insert(0, current.name)
        parent = current.parent
        if parent == current:  # filesystem root
            break
        current = parent
    if not parts:  # defensive: not importable as a package
        return f"{Path(module_dir).name}.agent:{class_name}"
    return f"{'.'.join(parts)}.agent:{class_name}"


def _declaration_source(
    agent_id: str, *, import_path: str, mode: str, provides: list[str],
    budget: str, write_surface: str, transcript_source: str,
) -> str:
    provides_block = "\n".join(f"  - {item}" for item in sorted(provides)) or "  []"
    return f'''# Generated by `aeval new-agent`. Values must agree with the adapter class:
# a declaration that drifts from its class is an error, not a silent winner.
extends: _base/{AGENT_BASE_FILENAME}

id: {agent_id}
version: "1"
import_path: "{import_path}"

mode: {mode}
budget_enforcement: {budget}
write_surface: {write_surface}
provides:
{provides_block}

transcript:
  source: {transcript_source}
  reader: {agent_id}-reader
  capabilities:
    - {transcript_source}
  # `unavailable` is the honest starting point: nothing has been observed yet,
  # and a fresh adapter must not claim a field it has not been shown to expose.
  fields_available:
    events: unavailable
    token_usage: unavailable
'''


def scaffold_agent(
    agents_root: Path,
    agent_id: str,
    *,
    import_path: str | None = None,
    package_dir: Path | None = None,
    mode: str = "installed_cli",
    provides: list[str] | None = None,
    budget_enforcement: str = "none",
    write_surface: str = "ephemeral_overlay",
    transcript_source: str = "atif_native",
    force: bool = False,
) -> ScaffoldResult:
    """Write a conformant declaration + adapter skeleton for a new agent."""
    if not agent_id or not all(part.isalnum() or part in "-_" for part in agent_id):
        raise SuiteError(f"agent id must be a simple identifier, got {agent_id!r}")
    if mode not in _MODES:
        raise SuiteError(f"mode must be one of {list(_MODES)}, got {mode!r}")
    if budget_enforcement not in _BUDGETS:
        raise SuiteError(
            f"budget_enforcement must be one of {list(_BUDGETS)}, got {budget_enforcement!r}"
        )
    if write_surface not in _WRITE_SURFACES:
        raise SuiteError(
            f"write_surface must be one of {list(_WRITE_SURFACES)}, got {write_surface!r}"
        )
    if transcript_source not in _TRANSCRIPT_SOURCES:
        raise SuiteError(
            f"transcript source must be one of {list(_TRANSCRIPT_SOURCES)}, "
            f"got {transcript_source!r}"
        )
    if budget_enforcement == "gateway_lease":
        raise SuiteError(
            "budget_enforcement='gateway_lease' is enforced by an in-sandbox control "
            "stack, which this generator cannot write for you; declare 'none' and add "
            "CONTROL_STACK + CONTROL_STACK_IMPL once you have the stack"
        )

    root = Path(agents_root)
    if not (root / "_base" / AGENT_BASE_FILENAME).is_file():
        raise SuiteError(
            f"{root} has no _base/{AGENT_BASE_FILENAME} — run the generator from a repo "
            "that holds the agent declaration base (see docs/guides/adding-an-agent.md)"
        )
    declaration_path = root / f"{agent_id}.yaml"
    if declaration_path.exists() and not force:
        raise SuiteError(f"{declaration_path} already exists (use force=True to overwrite)")

    provides = list(provides or [])
    module_dir = Path(package_dir or DEFAULT_PACKAGE_DIRNAME) / agent_id
    module_path = module_dir / "agent.py"

    module_dir.mkdir(parents=True, exist_ok=True)
    module_path.write_text(
        _module_source(
            agent_id,
            mode=mode,
            provides=provides,
            budget=budget_enforcement,
            transcript_source=transcript_source,
        ),
        encoding="utf-8",
    )
    init_path = module_dir / "__init__.py"
    if not init_path.exists():
        init_path.write_text(
            f'"""The {agent_id} adapter package."""\n', encoding="utf-8"
        )
    # Derived AFTER the files exist: the answer depends on which of the
    # surrounding directories are packages, so it cannot be computed in advance.
    resolved_import = import_path or _derive_import_path(
        module_dir, _class_name(agent_id)
    )
    declaration_path.write_text(
        _declaration_source(
            agent_id,
            import_path=resolved_import,
            mode=mode,
            provides=provides,
            budget=budget_enforcement,
            write_surface=write_surface,
            transcript_source=transcript_source,
        ),
        encoding="utf-8",
    )

    result = ScaffoldResult(
        agent_id=agent_id,
        declaration_path=declaration_path,
        module_path=module_path,
        import_path=resolved_import,
        files=[module_path, declaration_path],
    )
    # the generated declaration must already be valid on its own
    resolve_agent_declaration(declaration_path, agents_root=root)
    return result
