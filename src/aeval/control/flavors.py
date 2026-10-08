"""The control-stack flavor registry.

A control flavor owns ONE deployment mechanism: how its in-sandbox stack is
placed, and which adapter interface it needs. The registry is the extension
point — a new flavor registers itself instead of editing a dispatcher, and the
dispatcher never names a flavor.

Who registers what is the dependency rule:

- core (this module) only owns the registry — it imports no adapter package;
- an agent-neutral deployment registers itself where its mechanism lives
  (the generic OpenAI-wire facade in ``aeval.control.bootstrap`` — its
  registry name is a legacy-flavored opaque id carried over from the first
  agent family that needed it; capability questions go through
  ``serves_protocols``, never the name);
- an adapter-specific deployment registers itself from the adapter's own
  package (``aeval.agents.dsh.control_flavor``), so the core never imports a
  concrete adapter — importing the adapter package IS the registration.

A stack the registry does not know is refused, never guessed.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

__all__ = [
    "ControlFlavor",
    "register_control_flavor",
    "control_flavor",
    "known_control_flavors",
    "CONTROL_FLAVOR_DEPLOY_KEYS",
]

#: The exact keyword contract every flavor's ``deploy`` accepts: one uniform
#: surface, each flavor takes what its mechanism needs and ignores the rest.
CONTROL_FLAVOR_DEPLOY_KEYS = (
    "environment", "context", "agent", "paths", "config", "trial_id",
    "control_dist", "control_ca", "facade_dist", "facade_root",
    "control_options",
)

#: ``control_options`` carries ONLY the calling flavor's own namespace from the
#: suite's ``driver.control_options`` (the framework resolves the namespace and
#: interprets nothing); a flavor validates its own keys and values.

#: A flavor's deployment coroutine: async, keyword-only, uniform keys above.
ControlFlavorDeploy = Callable[..., Awaitable[None]]

#: A flavor's config-shape hook: the fields its in-sandbox stack consumes on
#: top of the neutral control config (called with ``paths`` and, when the
#: caller has one, ``auxiliary_policy``). ``None`` means the flavor's stack
#: consumes nothing beyond the neutral half.
ControlFlavorConfigFields = Callable[..., dict[str, Any]]


@dataclass(frozen=True)
class ControlFlavor:
    """One deployable control stack.

    ``requires`` names the adapter attributes (methods or callables) the
    deployment needs — checked before anything is uploaded, so an adapter that
    cannot accept the stack is refused with the missing name, not discovered
    mid-deploy. ``config_fields`` is the flavor's half of the composed control
    config: the neutral identity is every flavor's contract with the
    broker lease, and whatever a flavor's plugin additionally consumes — the
    DSH plugin's session/bundle paths and routing policy — is declared here
    by the flavor, never hardcoded in the composer.

    ``serves_protocols`` is the flavor's CAPABILITY, and the only thing the
    declaration-gap and conformance checks may consult: the client wires
    (``openai_chat`` / ``openai_responses``) this flavor's in-sandbox stack
    translates for the agent. Empty means the stack expects the agent to
    speak the broker wire natively (no translation). Validation logic asks
    the registry for this — never a flavor's NAME — so a second translating
    stack registers itself and every check keeps working.
    """

    name: str
    deploy: ControlFlavorDeploy
    requires: tuple[str, ...] = ()
    config_fields: ControlFlavorConfigFields | None = None
    serves_protocols: frozenset[str] = frozenset()


_REGISTRY: dict[str, ControlFlavor] = {}


def register_control_flavor(flavor: ControlFlavor) -> None:
    """Register a flavor; a duplicate name is refused, never overwritten."""
    if not isinstance(flavor.name, str) or not flavor.name.strip():
        raise ValueError("a control flavor needs a non-empty name")
    if flavor.name in _REGISTRY:
        raise ValueError(f"control flavor {flavor.name!r} is already registered")
    _REGISTRY[flavor.name] = flavor


def control_flavor(name: str) -> ControlFlavor | None:
    """The registered flavor by name, or None when nothing registered it."""
    return _REGISTRY.get(name)


def known_control_flavors() -> tuple[str, ...]:
    """Every registered flavor name (for refusal messages and conformance)."""
    return tuple(sorted(_REGISTRY))
