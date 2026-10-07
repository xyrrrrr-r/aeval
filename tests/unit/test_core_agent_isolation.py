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


# ── the literal ratchet (agent-neutrality cleanup, red line I3) ─────────────
#
# Imports are half the coupling surface; the other half is name-keyed
# behavior: a core module branching on a flavor id, defaulting to a slot
# named after one agent, or probing for one vendor's package. This ratchet
# scans CORE string literals (the same "outside aeval.agents" scope as the
# import check above) for agent-flavored names.
#
# Scope choices, deliberately:
#
# * Docstrings are EXCLUDED — documentation prose is not behavioral
#   coupling, and misleading docs are fixed where they stand (D-group),
#   not whitelisted.
# * Substring probes: the npm scope of the DSH release train and the two
#   repo-named control packages. Exact probes: the bare agent id "dsh"
#   (the legacy lock-section key) and the built-in slot "dsh_session"
#   (the sealed historical plan default).
# * Identifier NAMES (DshReleaseLock, SANDBOX_DSH_HOME, …) are not
#   scanned: they are the sealed lock/plan FORMAT, retained on purpose
#   and covered by their own tests.

#: Substring probes over core string literals.
AGENT_FLAVORED_SUBSTRINGS = (
    "@deepseek-ai",
    "deepagent-facade",
    "dsh-eval-control",
    "deepagents-eval-control",
)

#: Exact-value probes over core string literals.
AGENT_FLAVORED_EXACT = ("dsh", "dsh_session")

#: Known intentional retentions, as ``"<module>:<literal>"``. Must match
#: reality in BOTH directions: a new hit fails until justified here, and a
#: removed retention fails until deleted from this set — the list can only
#: shrink by decision, never by drift.
KNOWN_RETENTION: frozenset[str] = frozenset({
    # Sealed lock format: the legacy ``dsh`` section is part of the
    # runtime-lock bytes old locks must keep loading, digesting and
    # comparing identically (I1/I2).
    "aeval.bundle.attestation:dsh",
    "aeval.contracts:dsh",
    "aeval.provenance:@deepseek-ai/dsh",
    # Sealed plan format: the built-in ``dsh_session`` slot default keeps
    # every suite sealed before the slot vocabulary opened behaving
    # byte-identically (I2).
    "aeval.cli:dsh_session",
    "aeval.hooks.collection:dsh_session",
    "aeval.hooks.collectors:dsh_session",
    "aeval.hooks.evidence:dsh_session",
    "aeval.suite_loader.composition:dsh_session",
    "aeval.suite_models:dsh_session",
    # The facade flavor's registry id: an opaque legacy-flavored slug
    # carried over from the first agent family that needed it (rename
    # deferred; capability questions go through serves_protocols).
    "aeval.control.bootstrap:deepagent-facade",
    # Facade dist discovery names its default package (a repo-naming fact,
    # B3): overridable via AEVAL_FACADE_DIST, and the remedy messages must
    # name the repo an operator actually builds.
    "aeval.control.bootstrap:deepagents-eval-control",
    "aeval.control.bootstrap:no built deepagent facade dist found — build deepagents-eval-control (npm run build) or point AEVAL_FACADE_DIST at its dist/ directory",
    "aeval.control.bootstrap: — run `npm ci` in deepagents-eval-control so the pinned packages ship with the dist (the sandbox has no registry access)",
    # The facade's runtime-closure probe: the neutral facade builds on the
    # pinned @deepseek-ai/dsh-llm library (A1/A2 decision: keep as a pinned
    # library dependency, do not vendor).
    "aeval.control.bootstrap:node_modules/@deepseek-ai/dsh-llm/package.json",
    # The legacy-format gate's refusal messages name the package they
    # validate (the messages are part of the gate's contract with the
    # operator; the gate itself is sealed-format validation, not agent
    # selection).
    "aeval.provenance:@deepseek-ai/dsh",
    "aeval.provenance:dsh lock is missing @deepseek-ai/dsh",
    "aeval.provenance:@deepseek-ai/dsh lock is missing its npm integrity hash",
})


def _docstring_ids(tree: ast.AST) -> set[int]:
    """Ids of the Constant nodes that sit in docstring position."""
    ids: set[int] = set()

    def first_stmt_doc(body) -> None:
        if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant):
            value = body[0].value
            if isinstance(value.value, str):
                ids.add(id(value))

    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            first_stmt_doc(node.body)
    return ids


def _agent_flavored_core_literals() -> set[str]:
    """Every agent-flavored string literal in core, as ``"<module>:<literal>"``."""
    found: set[str] = set()
    for path in sorted(PKG.rglob("*.py")):
        relative = path.relative_to(PKG)
        if relative.parts[0] == "agents":
            continue  # the agents layer is out of scope by definition
        module = _module_of(path)
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        docs = _docstring_ids(tree)
        for node in ast.walk(tree):
            if not isinstance(node, ast.Constant) or not isinstance(node.value, str):
                continue
            if id(node) in docs:
                continue
            value = node.value
            if any(probe in value for probe in AGENT_FLAVORED_SUBSTRINGS) or value in AGENT_FLAVORED_EXACT:
                found.add(f"{module}:{value}")
    return found


def test_core_names_no_concrete_agent():
    actual = _agent_flavored_core_literals()
    added = sorted(actual - KNOWN_RETENTION)
    assert not added, (
        "new agent-flavored literal(s) in core — declare the behavior "
        "(a capability, a slot, a hook) instead of naming the agent "
        "(AGENT-ABSTRACTION-2-PLAN.md §6 I3), or justify the retention in "
        "KNOWN_RETENTION:\n  " + "\n  ".join(added)
    )


def test_literal_retentions_are_still_present():
    actual = _agent_flavored_core_literals()
    stale = sorted(KNOWN_RETENTION - actual)
    assert not stale, (
        "a listed retention is gone from core — remove it from "
        "KNOWN_RETENTION so the ratchet keeps shrinking:\n  "
        + "\n  ".join(stale)
    )
