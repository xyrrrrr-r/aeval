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
from typing import Any, Protocol, runtime_checkable

from aeval.suite_models import SuiteError

__all__ = [
    "KNOWN_CAPABILITIES",
    "AgentAdapter",
    "declared_capabilities",
    "load_adapter_class",
    "required_capability_gaps",
]

#: Recommended capability vocabulary. Not enforced: an adapter may introduce a new
#: capability, and a suite that requires the wrong name then simply fails to match.
KNOWN_CAPABILITIES = frozenset(
    {"acp_stdio", "sdk_jsonrpc", "shell", "file_tools", "resume", "atif_native"}
)

PROVIDES_ATTR = "PROVIDES"
SESSION_ID_ATTR = "agent_session_id"
LEGACY_SESSION_ID_ATTR = "dsh_session_id"


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
