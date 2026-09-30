"""The control-stack flavor registry (AGENT-ABSTRACTION-2, G7/G9).

A control flavor owns ONE deployment mechanism: how its in-sandbox stack is
placed, and which adapter interface it needs. The registry is the extension
point — a new flavor registers itself instead of editing a dispatcher, and the
dispatcher never names a flavor.

Who registers what is the dependency rule:

- core (this module) only owns the registry — it imports no adapter package;
- an agent-neutral deployment registers itself where its mechanism lives
  (``deepagent-facade`` in ``aeval.control.bootstrap``: the generic facade any
  ``openai_*`` agent can be pointed at);
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
)

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
    config (G7): the neutral identity is every flavor's contract with the
    broker lease, and whatever a flavor's plugin additionally consumes — the
    DSH plugin's session/bundle paths and routing policy — is declared here
    by the flavor, never hardcoded in the composer.
    """

    name: str
    deploy: ControlFlavorDeploy
    requires: tuple[str, ...] = ()
    config_fields: ControlFlavorConfigFields | None = None


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
