"""Every import inside the package must resolve inside the tree it ships in.

Cheap guard against a failure that actually happened: a commit that picked up a
colleague's *in-flight* edit left the tree importing a name that existed only in
that colleague's working copy (``contract.declared_artifacts``,
``contract.claims_verification_gap``). Nothing in the unit suite noticed — the
imports live inside functions that only a real trial executes — and it surfaced
five minutes into a lab run as ``ImportError: cannot import name
'declared_artifacts' from 'aeval.agents.contract'`` at VERIFICATION_START.

Parsing the tree costs milliseconds, so the guard is a test rather than a
convention. It reads the installed package (``aeval.__file__``), so it checks the
sources that this interpreter would actually import.
"""

from __future__ import annotations

import ast
from pathlib import Path

import aeval

PKG = Path(aeval.__file__).parent


def _module_path(module: str) -> Path | None:
    """``aeval.hooks.collectors`` -> its file, or None when it does not exist."""
    rel = module.split(".", 1)[1] if module.startswith("aeval.") else module
    for candidate in (PKG / f"{rel.replace('.', '/')}.py",
                      PKG / rel.replace(".", "/") / "__init__.py"):
        if candidate.is_file():
            return candidate
    return None


def _defined_names(path: Path) -> set[str]:
    """Names a module binds at top level (definitions, assignments, imports)."""
    names: set[str] = set()
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))

    def collect(node: ast.AST) -> None:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            names.add(node.name)
        elif isinstance(node, ast.Assign):
            names.update(t.id for t in node.targets if isinstance(t, ast.Name))
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            names.add(node.target.id)
        elif isinstance(node, ast.ImportFrom):
            names.update(a.asname or a.name for a in node.names)
        elif isinstance(node, ast.Import):
            names.update((a.asname or a.name).split(".")[0] for a in node.names)

    for node in tree.body:
        collect(node)
        # imports guarded by try/except or if/then are still module-level bindings
        if isinstance(node, (ast.Try, ast.If)):
            for sub in ast.walk(node):
                collect(sub)
    return names


def test_every_aeval_import_resolves_within_the_package():
    modules = sorted(PKG.rglob("*.py"))
    assert len(modules) > 20, "the package should not be nearly empty"
    defined = {path: _defined_names(path) for path in modules}

    unresolved: list[str] = []
    for path in modules:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.ImportFrom):
                continue
            if not node.module or not node.module.startswith("aeval"):
                continue
            target = _module_path(node.module)
            if target is None:
                unresolved.append(f"{path.name}:{node.lineno} -> {node.module} (no module)")
                continue
            for alias in node.names:
                if alias.name == "*":
                    continue
                if alias.name in defined[target]:
                    continue
                if _module_path(f"{node.module}.{alias.name}") is not None:
                    continue  # importing a submodule, not a name
                unresolved.append(
                    f"{path.name}:{node.lineno} -> {node.module}.{alias.name} (undefined)"
                )

    assert not unresolved, "imports that do not resolve in this tree:\n" + "\n".join(unresolved)
