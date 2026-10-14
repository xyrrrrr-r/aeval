"""Declaration-driven control-stack facts for the read-only wiring check.

The core doctor must stay agent-agnostic: it may not import a concrete adapter
package, and it may not name one. The adapter-owned half of its control-stack
checks therefore reaches it the same way every other control behavior does —
through the flavor registry — and this module is the neutral half that gets it
there:

* it loads the adapters the ``agents/`` declarations name (an ``import_path``
  string, never a package import; loading the adapter's package IS how its
  flavor registers), then
* it collects the control stacks those adapters declare and calls each
  registered flavor's ``doctor_checks`` hook with the uniform keyword contract
  (``CONTROL_FLAVOR_DOCTOR_KEYS``), and
* it merges what the hooks return.

No agent is named here. Whatever the declarations select is what is asked, and
no declaration means no hook — the caller then says so instead of guessing.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Mapping

__all__ = ["ControlDoctorFacts", "collect_control_doctor_facts"]

#: One check as a flavor reports it: ``(status, detail, hint)``.
ControlCheckPayload = tuple[str, str, "str | None"]


@dataclass(frozen=True)
class ControlDoctorFacts:
    """What the declared control stacks say about this installation."""

    #: ``check id -> (status, detail, hint)``
    checks: Mapping[str, ControlCheckPayload] = field(default_factory=dict)
    #: The runtime window the stack's package declares (node ``engines``), or
    #: ``None`` when no declared stack declares one.
    node_engines: str | None = None
    #: The compiled control package this host resolved, when one was resolved at
    #: all. A caller that has to WRITE a broker spec needs it: the spec must name
    #: where the broker entry lives, and resolving that is the adapter's
    #: knowledge, never the caller's.
    control_root: Path | None = None
    #: Why a stack's facts are absent or partial (empty when everything ran).
    notes: tuple[str, ...] = ()

    def merged(self, other: "ControlDoctorFacts") -> "ControlDoctorFacts":
        return ControlDoctorFacts(
            checks={**self.checks, **other.checks},
            node_engines=other.node_engines or self.node_engines,
            control_root=other.control_root or self.control_root,
            notes=(*self.notes, *other.notes),
        )


def _declared_control_stacks(agents_dir: Path) -> tuple[list[str], list[str]]:
    """The control stacks the declarations under ``agents_dir`` select.

    Returns ``(stacks, notes)``: stacks in declaration order, and one note per
    declaration that could not be resolved (a broken declaration is the agents
    check's subject, not this one's to fail on).
    """
    from aeval.agents.contract import control_stack_of
    from aeval.agents.declaration import (
        discover_agent_declarations,
        resolve_agent_declaration,
    )
    from aeval.suite_loader.paths import suite_path

    root = Path(agents_dir)
    stacks: list[str] = []
    notes: list[str] = []
    try:
        ids = discover_agent_declarations(root)
    except OSError as exc:
        return [], [f"cannot read the declarations under {root}: {exc}"]
    for agent_id in ids:
        try:
            resolved = resolve_agent_declaration(
                suite_path(root.resolve(), f"{agent_id}.yaml"), agents_root=root
            )
            stack = control_stack_of(resolved.declaration.adapter_class())
        except Exception as exc:                      # noqa: BLE001 - reported as a note
            notes.append(f"declaration {agent_id!r} selects no control stack ({exc})")
            continue
        if stack and stack not in stacks:
            stacks.append(stack)
    return stacks, notes


def collect_control_doctor_facts(
    *, spec: Mapping[str, Any] | None, env: Mapping[str, str], agents_dir: Path
) -> ControlDoctorFacts:
    """Ask every declared control stack's flavor for its doctor facts."""
    from aeval.control.flavors import control_flavor

    stacks, notes = _declared_control_stacks(Path(agents_dir))
    if not stacks:
        return ControlDoctorFacts(notes=tuple(notes))

    facts = ControlDoctorFacts(notes=tuple(notes))
    seen: set[int] = set()
    for stack in stacks:
        flavor = control_flavor(stack)
        hook: Callable[..., ControlDoctorFacts] | None = (
            flavor.doctor_checks if flavor is not None else None
        )
        if hook is None:
            continue
        if id(hook) in seen:
            continue
        seen.add(id(hook))
        try:
            partial = hook(spec=spec, env=env, agents_dir=Path(agents_dir))
        except Exception as exc:                      # noqa: BLE001 - reported as a note
            facts = facts.merged(
                ControlDoctorFacts(notes=(f"the {stack} flavor's doctor hook failed: {exc}",))
            )
            continue
        facts = facts.merged(partial)
    return facts
