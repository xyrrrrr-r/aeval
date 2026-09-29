"""aeval's agent-adapter contract: what an adapter can deliver to the evidence chain.

Harbor owns the *execution* abstraction (``BaseInstalledAgent``: install a CLI into
the sandbox and run it). What Harbor cannot know is what an adapter delivers to
*this* framework — which channel it speaks, whether it can be resumed, how its
transcript is read, whether its model traffic can be metered.

That is the contract this module names. Facts are declared by the adapter as class
attributes and validated **before a trial starts**, because a fact discovered late
(or duplicated in the core) is how a framework ends up hardcoding its first agent:

* ``PROVIDES`` — capabilities offered, matched against a suite's ``driver.require``.
  Capability names are an open vocabulary (add ``"browser"`` when an adapter ships
  one); a mismatch is refused, never warned about, so a typo on either side fails
  closed instead of silently pairing an agent with a suite it cannot serve.
* ``agent_session_id`` — the agent's *conversation* session. It is deliberately
  **not** called ``session_id``: Harbor's ``BaseAgent`` owns that attribute and
  assigns its own sandbox identifier (``<trial_name>__agent``) to it. Shadowing it
  with a read-only property killed every real trial with "property 'session_id' of
  'DshAgent' object has no setter" during environment verification, so the two ids
  must not share a name. ``dsh_session_id`` stays as a deprecated alias.

Only adapters selected by ``import_path`` are checked: a Harbor-native ``name:``
agent (``nop``, ``oracle``) is a placeholder for suite-shape validation and makes
no capability claim we could verify. That exemption is deliberate and visible here
rather than an implicit pass.
"""

from __future__ import annotations

from importlib import import_module
from typing import get_args
from typing import Any, Protocol, runtime_checkable

from pydantic import ValidationError

from aeval.contracts import AdapterSpec, TranscriptCapability
from aeval.suite_models import SuiteError

__all__ = [
    "KNOWN_CAPABILITIES",
    "AgentAdapter",
    "declared_capabilities",
    "load_adapter_class",
    "required_capability_gaps",
    "adapter_declaration_gap",
    "adapter_member_gap",
    "budget_enforcement_point",
    "budget_gate_violation",
    "build_adapter_spec",
    "describe_adapter",
]

#: Recommended capability vocabulary. Not enforced: an adapter may introduce a new
#: capability, and a suite that requires the wrong name then simply fails to match.
KNOWN_CAPABILITIES = frozenset(
    {"acp_stdio", "sdk_jsonrpc", "shell", "file_tools", "resume", "atif_native"}
)

PROVIDES_ATTR = "PROVIDES"
#: Declarations required to build the recorded adapter identity (``AdapterSpec``).
#: Missing any of these means a run whose records cannot say which agent produced
#: them — refused at run start rather than discovered at comparison time.
#: Members the evidence chain actually reads (name/version for identity, paths()
#: for artifact locations, read_trial_session() for grading) plus a conversation
#: session id under either the contract name or the deprecated alias. Missing one
#: used to mean an AttributeError in the middle of collection; now it is refused
#: before the run starts.
REQUIRED_MEMBERS = ("name", "version", "paths", "read_trial_session")
REQUIRED_DECLARATIONS = (
    "ADAPTER_ID",
    "ADAPTER_VERSION",
    "ADAPTER_MODE",
    "TRANSCRIPT_CAPABILITY",
    "BUDGET_ENFORCEMENT",
)
SESSION_ID_ATTR = "agent_session_id"
LEGACY_SESSION_ID_ATTR = "dsh_session_id"
SESSION_ID_MEMBERS = (SESSION_ID_ATTR, LEGACY_SESSION_ID_ATTR)


def load_adapter_class(import_path: str) -> type:
    """Import the adapter class named by a Harbor ``import_path``."""
    module_name, separator, attribute = import_path.partition(":")
    if not separator or not module_name.strip() or not attribute.strip():
        raise SuiteError(f"Agent import_path must be 'module:Class'; got {import_path!r}")
    try:
        module = import_module(module_name)
    except Exception as exc:  # noqa: BLE001 - any import failure is a config error
        raise SuiteError(f"Cannot import agent adapter {import_path!r}: {exc}") from exc
    target: Any = module
    try:
        for part in attribute.split("."):
            target = getattr(target, part)
    except AttributeError as exc:
        raise SuiteError(f"Agent adapter {import_path!r} has no attribute {attribute!r}") from exc
    if not isinstance(target, type):
        raise SuiteError(f"Agent adapter {import_path!r} must name a class, got {type(target).__name__}")
    return target


def declared_capabilities(import_path: str) -> frozenset[str]:
    """Capabilities the adapter at ``import_path`` declares via ``PROVIDES``.

    Missing or malformed declaration is an error: an adapter that cannot say what
    it offers cannot be paired with a suite that states requirements.
    """
    adapter = load_adapter_class(import_path)
    declared = getattr(adapter, PROVIDES_ATTR, None)
    if declared is None:
        raise SuiteError(
            f"Agent adapter {import_path} declares no {PROVIDES_ATTR}. Declare the "
            f"capabilities it offers, e.g. {PROVIDES_ATTR} = frozenset({{\"shell\"}}), "
            "so a suite's driver.require can be checked before a trial starts."
        )
    if isinstance(declared, str) or not isinstance(declared, (frozenset, set, tuple, list)):
        raise SuiteError(
            f"{import_path}.{PROVIDES_ATTR} must be a collection of capability names, "
            f"got {type(declared).__name__}"
        )
    names = set()
    for item in declared:
        if not isinstance(item, str) or not item.strip():
            raise SuiteError(f"{import_path}.{PROVIDES_ATTR} entries must be non-empty strings")
        names.add(item.strip())
    return frozenset(names)


def required_capability_gaps(
    import_paths: list[str], required: list[str]
) -> tuple[dict[str, frozenset[str]], list[str]]:
    """Per-adapter provided capabilities plus the union of unmet requirements.

    Requirements are checked against **every** declared adapter, not just the
    first: a job that lists several agents must not silently satisfy the suite's
    requirements with only one of them.
    """
    needed = {item.strip() for item in required if item.strip()}
    provided: dict[str, frozenset[str]] = {}
    missing: list[str] = []
    for import_path in import_paths:
        capabilities = declared_capabilities(import_path)
        provided[import_path] = capabilities
        missing.extend(sorted(needed - capabilities))
    return provided, sorted(set(missing))


@runtime_checkable
class AgentAdapter(Protocol):
    """Members the aeval chain requires of an agent adapter.

    ``Protocol`` (not a base class) on purpose: adapters extend Harbor's
    ``BaseInstalledAgent``, and a second inheritance edge would fight it. The
    framework validates these at run-context creation, so a missing member is a
    refusal at start-up instead of an ``AttributeError`` during collection.
    """

    name: str
    version: str

    @property
    def agent_session_id(self) -> str | None:
        """The agent's own conversation session id (never Harbor's sandbox session)."""
        ...


def describe_adapter(adapter: type) -> str:
    """Stable human name of an adapter class (used in error messages)."""
    return f"{adapter.__module__}:{adapter.__qualname__}"


def adapter_declaration_gap(adapter: type) -> list[str]:
    """Required declarations this adapter is missing (empty = complete)."""
    return [name for name in REQUIRED_DECLARATIONS if getattr(adapter, name, None) is None]


def build_adapter_spec(
    adapter: type | Any,
    *,
    import_path: str | None = None,
    version: str | None = None,
) -> AdapterSpec:
    """Build the adapter identity recorded in the manifest and trial store.

    ``version`` is the *observed* agent version when a live instance is available
    (what actually ran); otherwise the declaration's version is used and the
    record says so by omitting observation. Accepting either a class or an
    instance keeps the run-start (declared) and trial-end (observed) call sites
    on one code path.
    """
    adapter_class = adapter if isinstance(adapter, type) else type(adapter)
    missing = adapter_declaration_gap(adapter_class)
    if missing:
        raise SuiteError(
            f"Agent adapter {describe_adapter(adapter_class)} declares no {missing}. "
            "A run must record which adapter produced its trials (AdapterSpec); "
            "declare id/version/mode/transcript capability/budget enforcement."
        )
    try:
        return _adapter_spec(adapter_class, import_path=import_path, version=version)
    except ValidationError as exc:
        raise SuiteError(
            f"Agent adapter {describe_adapter(adapter_class)} declares a value outside the "
            f"contract's vocabulary: {exc}. Allowed: "
            f"mode={_allowed(AdapterSpec, 'mode')}, "
            f"budget_enforcement={_allowed(AdapterSpec, 'budget_enforcement')}, "
            f"write_surface={_allowed(AdapterSpec, 'write_surface')}, "
            f"server_side_session={_allowed(AdapterSpec, 'server_side_session')}, "
            f"transcript.source={_allowed(TranscriptCapability, 'source')}"
        ) from exc


def _allowed(model: type, field_name: str) -> list[str]:
    """Allowed values of a Literal-typed model field (for actionable errors)."""
    annotation = model.model_fields[field_name].annotation
    return [str(value) for value in get_args(annotation)]


def _adapter_spec(
    adapter_class: type, *, import_path: str | None, version: str | None
) -> AdapterSpec:
    return AdapterSpec(
        id=str(adapter_class.ADAPTER_ID),
        version=str(version or adapter_class.ADAPTER_VERSION),
        impl=import_path or describe_adapter(adapter_class),
        impl_version=str(adapter_class.ADAPTER_VERSION),
        mode=adapter_class.ADAPTER_MODE,
        transcript=adapter_class.TRANSCRIPT_CAPABILITY,
        budget_enforcement=adapter_class.BUDGET_ENFORCEMENT,
        write_surface=getattr(adapter_class, "WRITE_SURFACE", "ephemeral_overlay"),
        server_side_session=getattr(adapter_class, "SERVER_SIDE_SESSION", "forbidden"),
    )


def adapter_member_gap(adapter: type) -> list[str]:
    """Required contract members this adapter is missing (empty = complete)."""
    missing = [member for member in REQUIRED_MEMBERS if not hasattr(adapter, member)]
    if not any(hasattr(adapter, member) for member in SESSION_ID_MEMBERS):
        missing.append(f"{SESSION_ID_ATTR} (or legacy {LEGACY_SESSION_ID_ATTR})")
    return missing


def budget_enforcement_point(specs: list[AdapterSpec]) -> str:
    """The run-level place where spend is actually enforced.

    Mirrors ``AdapterSpec.budget_enforcement`` when every selected adapter agrees,
    ``"none"`` when none was selected, and ``"mixed"`` when a job selects adapters
    with different guarantees — a run must not claim one point it does not have.
    """
    points = {spec.budget_enforcement for spec in specs}
    if not points:
        return "none"
    if len(points) == 1:
        return next(iter(points))
    return "mixed"


def budget_gate_violation(
    specs: list[AdapterSpec], budget: Any, *, accepted: bool
) -> str | None:
    """Why a capped run must not start with these adapters (None = allowed).

    An adapter that does not route model traffic through the gateway lease has no
    measured spend: with a cap declared, the cap would never fire and the run
    would silently overspend (only surfacing later as a partial verdict). The
    refusal is the point — the alternative is an evaluation that cannot say what
    it spent.
    """
    if budget is None:
        return None
    caps = {
        name: getattr(budget, name, None)
        for name in ("max_tokens", "max_seconds", "max_steps")
    }
    declared = {name: value for name, value in caps.items() if value is not None}
    if not declared:
        return None
    offenders = [spec for spec in specs if spec.budget_enforcement != "gateway_lease"]
    if not offenders or accepted:
        return None
    described = ", ".join(
        f"{spec.id} declares budget_enforcement={spec.budget_enforcement!r}"
        for spec in offenders
    )
    return (
        f"suite caps spend ({declared}) but {described} — its model traffic is not metered "
        "by the gateway lease, so the cap cannot fire and the run would overspend silently. "
        "Run with --accept-unmetered-budget to record the gap explicitly instead."
    )
