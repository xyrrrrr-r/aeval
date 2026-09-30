"""Core modules must not import concrete agent adapters (ratchet, G5/G6).

The agent layer's whole point (AGENT-ABSTRACTION-2-PLAN.md §6 red line I3) is
that the framework core stays agent-agnostic: behavior differences are
declarations the core reads, never ``if agent == "…"`` branches and never a
direct dependency on one adapter's module. A core import of a concrete adapter
is how the FIRST agent quietly becomes a hardcoded default again.

The two historical violations are GONE (plan stage 2.2, 2026-09-30):

* ``control/bootstrap.py`` used to import ``SESSIONS_DIRNAME`` from the DSH
  adapter — the session mint now lives in the dsh flavor
  (``aeval.agents.dsh.control_flavor``) and is injected by the flavor registry
  (``aeval.control.flavors``), so bootstrap deploys trees without knowing any
  agent's session-store layout;
* ``hooks/evidence.py`` used to fall back to ``DshAgent`` as the offline/
  replay record owner — the fallback now resolves the adapter the runtime
  lock recorded through its declaration, fail-closed when missing or
  ambiguous.

The debt list is now EMPTY and must stay empty: a new violation fails this
suite. The ratchet only ever moves one way.

Scope notes:

* "Core" is every module under ``aeval`` OUTSIDE the ``agents`` subtree. The
  agents layer (contract/declaration/scaffold/conformance) may of course talk
  about adapters — that is its job.
* The framework half of ``aeval.agents`` (``contract``, ``declaration``,
  ``scaffold``, ``conformance``) stays importable from core; only the CONCRETE
  adapter packages (``dsh``, ``deepagent``, ``testing``) are banned. Importing
  through an ``import_path`` string (declaration loading) is not an import and
  is not affected.
* Relative imports are resolved, so ``from ..agents.dsh import X`` cannot sneak
  past the absolute-name check.
"""

from __future__ import annotations

import ast
from pathlib import Path

import aeval

PKG = Path(aeval.__file__).parent

#: Concrete adapter packages core must never import.
BANNED_ADAPTER_PACKAGES = (
    "aeval.agents.dsh",
    "aeval.agents.deepagent",
    "aeval.agents.testing",
    "aeval.agents.openai_acp",
)

#: Known core→adapter imports. EMPTIED by plan stage 2.2 (2026-09-30): it must
#: stay empty — an entry is ``"<importing module> -> <banned module>"`` and the
#: set must match reality exactly in BOTH directions (see the assertions).
KNOWN_DEBT: frozenset[str] = frozenset()


def _module_of(path: Path) -> str:
    """``…/src/aeval/hooks/plugin.py`` -> ``aeval.hooks.plugin``."""
    rel = path.resolve().relative_to(PKG.parent)
    parts = list(rel.with_suffix("").parts)
    if parts and parts[-1] == "__init__":
        parts = parts[:-1]
    return ".".join(parts)


def _core_imports_of_adapters() -> set[str]:
    """Every core→adapter import, as ``"<module> -> <banned target>"`` strings."""
    found: set[str] = set()
    for path in sorted(PKG.rglob("*.py")):
        relative = path.relative_to(PKG)
        if relative.parts[0] == "agents":
            continue  # the agents layer is out of scope by definition
        module = _module_of(path)
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            targets: list[str] = []
            if isinstance(node, ast.ImportFrom):
                target = node.module or ""
                if node.level:
                    # Resolve a relative import against the importing module's
                    # own package, so ``from ..agents.dsh import X`` in
                    # aeval.hooks.plugin resolves to aeval.agents.dsh.
                    base = module.split(".")
                    for _ in range(node.level - 1):
                        base = base[:-1]
                    target = ".".join([*base, target] if target else base)
                targets.append(target)
            elif isinstance(node, ast.Import):
                targets.extend(alias.name for alias in node.names)
            for target in targets:
                if any(
                    target == banned or target.startswith(banned + ".")
                    for banned in BANNED_ADAPTER_PACKAGES
                ):
                    found.add(f"{module} -> {target}")
    return found


def test_core_does_not_import_concrete_adapters():
    actual = _core_imports_of_adapters()
    added = sorted(actual - KNOWN_DEBT)
    assert not added, (
        "new core→adapter import(s) — the framework core must stay "
        "agent-agnostic (declare the behavior instead, "
        "AGENT-ABSTRACTION-2-PLAN.md §6 I3):\n  " + "\n  ".join(added)
    )


def test_known_debt_is_still_present():
    actual = _core_imports_of_adapters()
    stale = sorted(KNOWN_DEBT - actual)
    assert not stale, (
        "a listed core→adapter import is gone — remove it from KNOWN_DEBT so "
        "the ratchet keeps shrinking (AGENT-ABSTRACTION-2-PLAN.md stage 2.2):\n  "
        + "\n  ".join(stale)
    )


def test_the_flavor_registry_is_the_extension_point():
    """Stage 2.1: a new control flavor registers itself, core stays untouched.

    Registering a fake flavor and deploying a fake agent that declares it must
    reach the fake deploy with the uniform kwargs — no core edit, no
    agent-name branch anywhere in the dispatch path.
    """
    import asyncio

    from aeval.control.flavors import (
        ControlFlavor,
        control_flavor,
        known_control_flavors,
        register_control_flavor,
    )

    seen: dict[str, object] = {}

    async def _deploy(**kwargs):
        seen.update(kwargs)

    flavor = ControlFlavor(name="fake-flavor", deploy=_deploy, requires=("flavor_hook",))
    register_control_flavor(flavor)
    try:
        assert control_flavor("fake-flavor") is flavor
        assert "fake-flavor" in known_control_flavors()

        class _FakeAgent:
            CONTROL_STACK = "fake-flavor"

            @staticmethod
            def flavor_hook():
                return True

        from types import SimpleNamespace

        from aeval.control import bootstrap as bootstrap_module

        asyncio.run(bootstrap_module._deploy_declared_stack(
            environment=object(), context=SimpleNamespace(), agent=_FakeAgent(),
            paths=SimpleNamespace(), config={"gatewayUrl": "http://x", "jobTokenFile": "/t"},
            control_dist=None, control_ca=None, facade_dist=None, trial_id="t",
        ))
        assert seen["trial_id"] == "t"
        assert isinstance(seen["agent"], _FakeAgent)
    finally:
        # a duplicate name is refused, never overwritten — and the fake flavor
        # must not leak into other tests' registry view
        try:
            register_control_flavor(flavor)
        except ValueError as exc:
            assert "already registered" in str(exc)
        else:
            raise AssertionError("re-registering a flavor name must be refused")
        from aeval.control import flavors as flavors_module

        flavors_module._REGISTRY.pop("fake-flavor", None)


def test_a_flavor_refuses_an_adapter_missing_its_interface():
    """The declared ``requires`` is checked before anything is uploaded."""
    import asyncio
    from types import SimpleNamespace

    from aeval.control import bootstrap as bootstrap_module
    from aeval.control.flavors import ControlFlavor, register_control_flavor

    async def _deploy(**kwargs):  # pragma: no cover - must not run
        raise AssertionError("deploy must not run")

    register_control_flavor(ControlFlavor(name="hookless-flavor", deploy=_deploy,
                                          requires=("flavor_hook",)))
    try:

        class _Hookless:
            CONTROL_STACK = "hookless-flavor"

        try:
            asyncio.run(bootstrap_module._deploy_declared_stack(
                environment=object(), context=SimpleNamespace(), agent=_Hookless(),
                paths=SimpleNamespace(), config={}, control_dist=None,
                control_ca=None, facade_dist=None, trial_id="t",
            ))
        except bootstrap_module.BootstrapError as exc:
            assert "flavor_hook" in str(exc)
            assert "_Hookless" in str(exc)
        else:
            raise AssertionError("a hookless adapter must be refused")
    finally:
        from aeval.control import flavors as flavors_module

        flavors_module._REGISTRY.pop("hookless-flavor", None)


def test_an_unregistered_stack_is_refused_not_skipped():
    import asyncio
    from types import SimpleNamespace

    from aeval.control import bootstrap as bootstrap_module

    class _Mystery:
        CONTROL_STACK = "mystery-stack"

    try:
        asyncio.run(bootstrap_module._deploy_declared_stack(
            environment=object(), context=SimpleNamespace(), agent=_Mystery(),
            paths=SimpleNamespace(), config={}, control_dist=None,
            control_ca=None, facade_dist=None, trial_id="t",
        ))
    except bootstrap_module.BootstrapError as exc:
        assert "mystery-stack" in str(exc)
        assert "registered flavors" in str(exc)
    else:
        raise AssertionError("an unregistered stack must be refused")
