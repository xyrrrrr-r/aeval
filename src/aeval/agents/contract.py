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

import re
from dataclasses import dataclass, field
from importlib import import_module
from pathlib import PurePosixPath
from typing import get_args
from typing import Any, Mapping, Protocol, runtime_checkable

from pydantic import ValidationError

from aeval.contracts import AdapterSpec, TranscriptCapability
from aeval.suite_models import SuiteError

__all__ = [
    "KNOWN_CAPABILITIES",
    "AgentAdapter",
    "ModelRouting",
    "model_routing_of",
    "adapter_classes_recorded_in",
    "control_stack_of",
    "stack_serves_protocol",
    "stack_serves_any_openai",
    "OFFICIAL_RELEASE_LOCK_ATTR",
    "official_release_lock_of",
    "capabilities_of",
    "declared_capabilities",
    "session_record_output_of",
    "session_record_output_path_of",
    "session_record_slot_well_formed",
    "terminal_descriptor_owner",
    "sandbox_session_record",
    "session_id_from_record",
    "locate_session_record",
    "load_adapter_class",
    "required_capability_gaps",
    "adapter_declaration_gap",
    "adapter_member_gap",
    "declared_observations",
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
#: for artifact locations, read_trial_session() for grading, read_session_record()
#: for the official session record, SESSION_RECORD_OUTPUT for the collect slot
#: that record belongs to) plus a conversation session id under either the
#: contract name or the deprecated alias. Missing one used to mean an
#: AttributeError in the middle of collection; now it is refused before the
#: run starts.
REQUIRED_MEMBERS = (
    "name",
    "version",
    "paths",
    "read_trial_session",
    "read_session_record",
    "SESSION_RECORD_OUTPUT",
)
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

SESSION_RECORD_OUTPUT_ATTR = "SESSION_RECORD_OUTPUT"
#: The session-record slots the FRAMEWORK itself knows a fixed path for.
#: Kept for bundles and plans sealed before slots became declarable: a new
#: adapter may declare its own slot name (a well-formed slug) together with
#: its fixed path, and the suite/adapter pairing is still exact-match — the
#: built-ins are compatibility, not a gate.
SESSION_RECORD_OUTPUTS = frozenset({"dsh_session", "agent_session_record"})

#: Contract hook: the fixed path of THIS adapter's session record inside the
#: trial dir (relative, no escapes). Optional for the built-in slots — their
#: historical paths are the framework's compatibility table — but REQUIRED
#: for a declared slot the framework has never known: without it nothing
#: could tell the collector where the bytes belong.
SESSION_RECORD_OUTPUT_PATH_ATTR = "SESSION_RECORD_OUTPUT_PATH"

#: A slot name is a lowercase slug: it names a collect output that appears in
#: plans, manifests and the evidence bundle's fixed-path table.
_SESSION_RECORD_SLOT_PATTERN = re.compile(r"^[a-z][a-z0-9_]*$")


def session_record_slot_well_formed(slot: str) -> bool:
    """Whether ``slot`` is a well-formed session-record output name."""
    return (
        isinstance(slot, str)
        and bool(_SESSION_RECORD_SLOT_PATTERN.match(slot))
        and not slot.startswith("observable:")
    )


def session_record_output_path_of(adapter: type) -> str | None:
    """The fixed trial-dir path of this adapter's session record, if declared.

    Validated the moment it is read: a path that is absolute, empty or
    escapes the trial dir is a configuration error, refused here rather than
    discovered mid-collection.
    """
    value = getattr(adapter, SESSION_RECORD_OUTPUT_PATH_ATTR, None)
    if value is None:
        return None
    if (
        not isinstance(value, str)
        or not value.strip()
        or value.startswith("/")
        or any(part == ".." for part in PurePosixPath(value).parts)
    ):
        raise SuiteError(
            f"Agent adapter {describe_adapter(adapter)} declares "
            f"{SESSION_RECORD_OUTPUT_PATH_ATTR}={value!r}; it must be a "
            "relative path inside the trial directory that cannot escape it."
        )
    return value

#: Contract hook: locate THIS adapter's official session record. Session-record
#: shape is adapter-flavored (DSH nests one file per session id in a project
#: tree; an ACP runner writes a single summary whose own session id is the
#: identity), so the framework asks the adapter instead of encoding its first
#: agent's layout a second time in the gate. Signature:
#: ``(record_root: Path, session_id: str) -> Path | None``.
SESSION_RECORD_LOCATOR_ATTR = "locate_session_record"

#: Contract hook: the session id the record itself carries, or None when the
#: adapter's identity is the id aeval minted and handed to the stack (DSH).
#: Only an adapter whose record is inscribed by a foreign runtime needs this:
#: it is how the owner learns which session the record belongs to. Signature:
#: ``(record_bytes: bytes) -> str | None``.
SESSION_ID_FROM_RECORD_ATTR = "session_id_from_record"

#: Declaration: the sandbox path of the official record when it is one known
#: file (so the owner can observe its identity before Harbor downloads the
#: agent logs). Absent means the record is a tree the host never reads live.
SANDBOX_SESSION_RECORD_ATTR = "SANDBOX_SESSION_RECORD"

#: Declaration: which component can state the trial's terminal session outcome,
#: and therefore writes the bundle descriptor. ``sandbox`` is the historical
#: default — the in-sandbox control stack owns the session, so it owns the
#: statement (DSH). ``host`` is for a stack that only proxies model traffic and
#: never observes an exit: then the owner observes what it can and states it,
#: because the evidence gate refuses to grade without a descriptor.
TERMINAL_DESCRIPTOR_OWNER_ATTR = "TERMINAL_DESCRIPTOR_OWNER"
TERMINAL_DESCRIPTOR_OWNERS = frozenset({"sandbox", "host"})
TERMINAL_DESCRIPTOR_OWNERS_DEFAULT = "sandbox"


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


def control_stack_of(adapter: type) -> str | None:
    """The in-sandbox control stack this adapter needs, if any.

    Optional by design: most adapters need none, and a framework that demands a
    DSH-specific stack from every agent is what made the second adapter expensive.
    """
    declared = getattr(adapter, "CONTROL_STACK", None)
    if declared is None:
        return None
    if not isinstance(declared, str) or not declared.strip():
        raise SuiteError(
            f"{describe_adapter(adapter)}.CONTROL_STACK must be a non-empty string or absent"
        )
    return declared.strip()


def _registered_flavor(stack: str | None):
    """The flavor registered under ``stack``, ensuring core flavors exist.

    A capability question must never be a name question, so this resolves the
    registry entry and lets the caller read ``serves_protocols`` — whatever
    flavor is registered under the declared name answers, and an unregistered
    name answers nothing. Importing ``aeval.control.bootstrap`` here is what
    makes the agent-neutral facade flavor resolvable even when this module
    was imported first (it registers at import); adapter flavors register
    when their package loads, which by discipline happens before any adapter
    class is inspected. The import is lazy because bootstrap imports this
    module — at call time both are fully loaded, so the cycle never closes.
    """
    if stack is None:
        return None
    from aeval.control.flavors import control_flavor

    try:
        from aeval.control import bootstrap as _bootstrap  # noqa: F401
    except ImportError:  # pragma: no cover - bootstrap is always importable
        pass
    return control_flavor(stack)


def stack_serves_protocol(stack: str | None, protocol: str) -> bool:
    """Does the declared stack's flavor translate ``protocol`` for the agent?

    The registry answers (a flavor declares ``serves_protocols``): an absent
    stack, an unregistered name, or a registered flavor that does not carry
    the protocol all answer False — the caller words the refusal, the
    registry supplies the fact. No check compares a flavor NAME.
    """
    flavor = _registered_flavor(stack)
    return flavor is not None and protocol in flavor.serves_protocols


def stack_serves_any_openai(stack: str | None) -> bool:
    """Does the declared stack's flavor translate any OpenAI client wire?"""
    flavor = _registered_flavor(stack)
    return flavor is not None and bool(flavor.serves_protocols)


#: Declaration: the pinned release this adapter ships, when it has one. The
#: value is a frozen lock model (a class attribute) — either the generic
#: ``AgentReleaseLock`` or, for an agent whose historical lock section predates
#: the generic shape, the legacy ``DshReleaseLock``. The runtime lock's agent
#: sections are built from whatever the SELECTED adapters declare here, so
#: the core never names an agent when assembling supply-chain identity.
OFFICIAL_RELEASE_LOCK_ATTR = "official_release_lock"


def official_release_lock_of(adapter: type) -> Any:
    """The release lock this adapter pins, when it declares one.

    Either shape is accepted: a generic ``AgentReleaseLock`` (stored in the
    lock's ``agents`` section) or a legacy ``DshReleaseLock`` (stored in the
    legacy ``dsh`` section, byte-identical to what dsh runs recorded before
    the generic section existed). Anything else is a refusal — a value the
    runtime lock cannot store would silently drop the agent's supply-chain
    identity.
    """
    value = getattr(adapter, OFFICIAL_RELEASE_LOCK_ATTR, None)
    if value is None:
        return None
    from aeval.contracts import AgentReleaseLock, DshReleaseLock

    if not isinstance(value, (DshReleaseLock, AgentReleaseLock)):
        raise SuiteError(
            f"{describe_adapter(adapter)}.{OFFICIAL_RELEASE_LOCK_ATTR} must be "
            "a DshReleaseLock or AgentReleaseLock instance (or absent)"
        )
    return value


def adapter_classes_recorded_in(lock: Any) -> tuple[list[type], list[str]]:
    """Resolve a runtime lock's recorded agent ids to adapter classes.

    Each recorded id is read through its declaration (``agents/<id>.yaml``),
    the same resolution the plugin uses at run start — so anything that needs
    "which adapter is this run about" without a live handle (the evidence
    gate's record owner, the broker lifecycle's config flavor) asks the
    recorded lock instead of assuming the first agent. An id whose
    declaration or import cannot be resolved is reported in the second
    element, not silently dropped — the caller decides whether that is fatal.

    The class is the declaration's ``adapter_class()``: a pinned import_path
    resolves to itself, while a declaration-driven base materializes into the
    complete per-agent class (a declared agent has no code of its own to
    import — the declaration IS its facts).
    """
    from aeval.agents.declaration import default_agents_root, resolve_agent_declaration

    try:
        ids = sorted(lock.agent_locks())
    except Exception:  # noqa: BLE001 - an unreadable lock is the caller's error
        return [], []
    classes: list[type] = []
    unresolved: list[str] = []
    for agent_id in ids:
        path = default_agents_root() / f"{agent_id}.yaml"
        try:
            declaration = resolve_agent_declaration(path).declaration
            resolved = declaration.adapter_class()
        except Exception:  # noqa: BLE001 - resolution failure is reported, not raised here
            unresolved.append(agent_id)
            continue
        if resolved not in classes:
            classes.append(resolved)
    return classes, unresolved


#: Declaration: which client protocol the agent itself speaks for model
#: traffic. ``gateway_native`` means the
#: control stack's transport already speaks the broker wire (DSH); the
#: ``openai_*`` values mean the agent speaks an OpenAI protocol and the
#: in-sandbox facade must serve the matching endpoint.
MODEL_ROUTING_ATTR = "MODEL_ROUTING"
MODEL_ROUTING_PROTOCOLS = frozenset({"gateway_native", "openai_chat", "openai_responses"})
#: The env-spelling slots a declaration may name (which env var carries the
#: base URL, an alternate spelling of it, and the API key).
MODEL_ROUTING_ENV_SLOTS = ("base_url", "alt_base_url", "api_key")


@dataclass(frozen=True)
class ModelRouting:
    """The agent-side model-routing declaration (class ``MODEL_ROUTING``).

    ``env`` maps the logical slots to the env var NAMES the agent's runtime
    actually reads (``{"base_url": "OPENAI_BASE_URL", …}``) — the spellings
    are an agent fact, which is exactly why they are declared instead of
    hardcoded in the core.
    """

    agent_protocol: str
    env: Mapping[str, str] = field(default_factory=dict)


def model_routing_of(adapter: type) -> ModelRouting | None:
    """The model routing this adapter declares, validated (None when absent).

    Fail closed on a malformed declaration: a protocol outside the vocabulary
    or a misspelled env name would otherwise surface mid-trial as an agent
    quietly talking past its own facade.
    """
    declared = getattr(adapter, MODEL_ROUTING_ATTR, None)
    if declared is None:
        return None
    if not isinstance(declared, Mapping):
        raise SuiteError(
            f"{describe_adapter(adapter)}.{MODEL_ROUTING_ATTR} must be a mapping "
            f"(agent_protocol + env) or absent, got {type(declared).__name__}"
        )
    protocol = declared.get("agent_protocol")
    if protocol not in MODEL_ROUTING_PROTOCOLS:
        raise SuiteError(
            f"{describe_adapter(adapter)}.{MODEL_ROUTING_ATTR}.agent_protocol must be one "
            f"of {sorted(MODEL_ROUTING_PROTOCOLS)}, got {protocol!r}"
        )
    raw_env = declared.get("env", {})
    if raw_env is None:
        raw_env = {}
    if not isinstance(raw_env, Mapping):
        raise SuiteError(
            f"{describe_adapter(adapter)}.{MODEL_ROUTING_ATTR}.env must be a mapping of "
            "slot -> env var name"
        )
    env: dict[str, str] = {}
    for slot, name in raw_env.items():
        if slot not in MODEL_ROUTING_ENV_SLOTS:
            raise SuiteError(
                f"{describe_adapter(adapter)}.{MODEL_ROUTING_ATTR}.env names unknown slot "
                f"{slot!r}; known slots: {list(MODEL_ROUTING_ENV_SLOTS)}"
            )
        if not isinstance(name, str) or not name.isidentifier():
            raise SuiteError(
                f"{describe_adapter(adapter)}.{MODEL_ROUTING_ATTR}.env.{slot} must be an "
                f"environment variable name, got {name!r}"
            )
        env[str(slot)] = name
    if protocol != "gateway_native":
        for required in ("base_url", "api_key"):
            if required not in env:
                raise SuiteError(
                    f"{describe_adapter(adapter)}.{MODEL_ROUTING_ATTR}: protocol "
                    f"{protocol!r} needs env.{required} (the env var the agent's "
                    "runtime reads for the facade)"
                )
    return ModelRouting(agent_protocol=str(protocol), env=env)


def facade_protocols_for(routing: ModelRouting) -> list[str]:
    """The facade endpoints a routing needs (the ``AEVAL_FACADE_PROTOCOLS`` set).

    Pure derivation, no defaults: the deployment serves exactly what the
    selected agent speaks, and nothing else.
    """
    if routing.agent_protocol == "gateway_native":
        return []
    if routing.agent_protocol == "openai_chat":
        return ["chat_completions"]
    if routing.agent_protocol == "openai_responses":
        return ["responses"]
    raise SuiteError(
        f"model routing declares unknown protocol {routing.agent_protocol!r}; "
        f"known: {sorted(MODEL_ROUTING_PROTOCOLS)}"
    )


def capabilities_of(adapter: type) -> frozenset[str]:
    """Capabilities a resolved adapter class declares via ``PROVIDES``."""
    declared = getattr(adapter, PROVIDES_ATTR, None)
    if declared is None:
        raise SuiteError(
            f"Agent adapter {describe_adapter(adapter)} declares no {PROVIDES_ATTR}. Declare the "
            f"capabilities it offers, e.g. {PROVIDES_ATTR} = frozenset({{\"shell\"}}), "
            "so a suite's driver.require can be checked before a trial starts."
        )
    if isinstance(declared, str) or not isinstance(declared, (frozenset, set, tuple, list)):
        raise SuiteError(
            f"{describe_adapter(adapter)}.{PROVIDES_ATTR} must be a collection of capability names, "
            f"got {type(declared).__name__}"
        )
    names = set()
    for item in declared:
        if not isinstance(item, str) or not item.strip():
            raise SuiteError(
                f"{describe_adapter(adapter)}.{PROVIDES_ATTR} entries must be non-empty strings"
            )
        names.add(item.strip())
    return frozenset(names)


def session_record_output_of(adapter: type) -> str:
    """The session-record collect output this adapter produces (fail closed).

    Every adapter declares ``SESSION_RECORD_OUTPUT`` — the collect slot its
    ``read_session_record()`` bytes belong to — and the suite declares the
    matching flavor (``driver.session_record``). The built-in slots
    (``dsh_session``, ``agent_session_record``) keep their historical fixed
    paths; any other well-formed slug is a DECLARED slot and must come with
    ``SESSION_RECORD_OUTPUT_PATH`` so the collector knows where the bytes
    belong. A malformed value is a configuration error, refused here rather
    than mid-collection.
    """
    value = getattr(adapter, SESSION_RECORD_OUTPUT_ATTR, None)
    if not isinstance(value, str) or not session_record_slot_well_formed(value):
        raise SuiteError(
            f"Agent adapter {describe_adapter(adapter)} declares no valid "
            f"{SESSION_RECORD_OUTPUT_ATTR}. Declare a lowercase slug (built-"
            f"ins: {sorted(SESSION_RECORD_OUTPUTS)}) — the collect output "
            "your read_session_record() bytes belong to."
        )
    if value not in SESSION_RECORD_OUTPUTS and getattr(
        adapter, SESSION_RECORD_OUTPUT_PATH_ATTR, None
    ) is None:
        raise SuiteError(
            f"Agent adapter {describe_adapter(adapter)} declares session-"
            f"record slot {value!r}, which the framework has no built-in path "
            f"for — declare {SESSION_RECORD_OUTPUT_PATH_ATTR} with its fixed "
            "path inside the trial directory."
        )
    return value


def terminal_descriptor_owner(adapter: type) -> str:
    """Which component must write this adapter's terminal bundle descriptor.

    Optional, defaulting to ``sandbox``: an adapter whose control stack owns the
    session also owns the terminal statement about it, and an adapter with no
    declaration cannot silently appoint the host as the observer.
    """
    value = getattr(adapter, TERMINAL_DESCRIPTOR_OWNER_ATTR, TERMINAL_DESCRIPTOR_OWNERS_DEFAULT)
    if not isinstance(value, str) or value not in TERMINAL_DESCRIPTOR_OWNERS:
        raise SuiteError(
            f"Agent adapter {describe_adapter(adapter)} declares "
            f"{TERMINAL_DESCRIPTOR_OWNER_ATTR}={value!r}. Declare one of "
            f"{sorted(TERMINAL_DESCRIPTOR_OWNERS)} — the component that can "
            "state the terminal session outcome."
        )
    return value


def sandbox_session_record(adapter: type) -> str | None:
    """The sandbox path of the adapter's official record, when it is one file."""
    value = getattr(adapter, SANDBOX_SESSION_RECORD_ATTR, None)
    if value is None:
        return None
    if not isinstance(value, str) or not value.startswith("/"):
        raise SuiteError(
            f"Agent adapter {describe_adapter(adapter)} declares "
            f"{SANDBOX_SESSION_RECORD_ATTR}={value!r}; it must be an absolute "
            "sandbox path, or absent."
        )
    return value


def session_id_from_record(adapter: type, record_bytes: bytes) -> str | None:
    """The session id inscribed in the adapter's own record, if it has one."""
    hook = getattr(adapter, SESSION_ID_FROM_RECORD_ATTR, None)
    if hook is None:
        return None
    if not callable(hook):
        raise SuiteError(
            f"Agent adapter {describe_adapter(adapter)} declares "
            f"{SESSION_ID_FROM_RECORD_ATTR} as a non-callable; it must be a "
            "function of the record bytes."
        )
    value = hook(record_bytes)
    if value is not None and not isinstance(value, str):
        raise SuiteError(
            f"{describe_adapter(adapter)}.{SESSION_ID_FROM_RECORD_ATTR} returned "
            f"{type(value).__name__}; return the session id string or None."
        )
    return value


def locate_session_record(adapter: type, record_root: Any, session_id: str) -> Any:
    """The adapter's official session record for ``session_id``, or None.

    Fail closed on a missing hook rather than assuming a layout: an adapter that
    declares a session-record output but cannot say where its record lives would
    otherwise be graded against whatever file happened to be there.
    """
    hook = getattr(adapter, SESSION_RECORD_LOCATOR_ATTR, None)
    if not callable(hook):
        raise SuiteError(
            f"Agent adapter {describe_adapter(adapter)} declares "
            f"SESSION_RECORD_OUTPUT="
            f"{getattr(adapter, SESSION_RECORD_OUTPUT_ATTR, None)!r} without a "
            f"{SESSION_RECORD_LOCATOR_ATTR}() — the framework cannot verify a "
            "record it cannot locate."
        )
    return hook(record_root, session_id)


def declared_capabilities(import_path: str) -> frozenset[str]:
    """Capabilities the adapter at ``import_path`` declares via ``PROVIDES``.

    Missing or malformed declaration is an error: an adapter that cannot say what
    it offers cannot be paired with a suite that states requirements.
    """
    adapter = load_adapter_class(import_path)
    return capabilities_of(adapter)


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
    """Declarations this adapter is missing or that contradict each other.

    Empty = complete. Beyond missing attributes this refuses an unkeepable
    promise: the gateway lease is enforced *inside* the sandbox by the control
    stack, so an adapter that claims ``gateway_lease`` without declaring a stack
    would run unmetered while looking metered. The same holds for the declared
    model routing: an ``openai_*`` protocol needs
    a stack whose flavor translates it, and a translating stack with no
    ``openai_*`` protocol would serve an endpoint the agent never calls. Both
    rules consult the flavor registry's ``serves_protocols`` capability —
    never a flavor's name, so a second translating stack needs no edit here.
    """
    missing = [name for name in REQUIRED_DECLARATIONS if getattr(adapter, name, None) is None]
    stack = control_stack_of(adapter)
    if not missing and getattr(adapter, "BUDGET_ENFORCEMENT", None) == "gateway_lease":
        if stack is None:
            missing.append(
                "CONTROL_STACK (BUDGET_ENFORCEMENT='gateway_lease' is enforced by the "
                "in-sandbox control stack; without it the spend would never be measured)"
            )
    routing = model_routing_of(adapter)
    if routing is not None and routing.agent_protocol != "gateway_native":
        if not stack_serves_protocol(stack, routing.agent_protocol):
            where = (
                f"the declared control stack {stack!r} does not"
                if stack is not None
                else "no CONTROL_STACK is declared to"
            )
            missing.append(
                f"CONTROL_STACK (MODEL_ROUTING.agent_protocol={routing.agent_protocol!r} "
                f"speaks an OpenAI wire; {where} translate it — the agent "
                "would run unmetered)"
            )
    if stack_serves_any_openai(stack) and (
        routing is None or routing.agent_protocol == "gateway_native"
    ):
        missing.append(
            "MODEL_ROUTING (the declared control stack translates an openai_* "
            "protocol; without that declaration the stack would serve "
            "nothing the agent speaks)"
        )
    return missing


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


def declared_observations(adapter: type) -> frozenset[str]:
    """Runtime facts the adapter requires to be OBSERVED in the sandbox.

    Names are an open vocabulary (``node``, ``python``, ``dsh_npm_packages``…).
    An adapter that pins nothing declares nothing — a lock that does pin a runtime
    still forces the matching observation, so this cannot be used to opt out.
    """
    declared = getattr(adapter, "REQUIRED_OBSERVATIONS", ()) or ()
    return frozenset(str(item) for item in declared)


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
