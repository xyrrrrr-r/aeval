"""Suite inheritance (schema 2, additive): ``extends`` shared convention bases.

A suite stays a *thin overlay* over Harbor-native declarations. What this
module adds is a second, lower layer: aeval-owned **convention facts**
(clock, the baseline seed observable, the fixed requirement list, driver
capabilities, metric kinds, the datasets path convention) can live in one
or more base manifests under the suites root, and each suite inherits them
and overrides/appends what it needs.

Three rules keep that layer honest:

1. **A base may never restate a Harbor-owned fact.** Bases go through the
   same overlap check as suites, so a base cannot become a laundering
   channel for ``trials``/``budget``/``attempts``/… that must stay in the
   Harbor task/job files.
2. **Merge semantics are declared per field, never a generic deep merge.**
   Scalars replace, keyed lists merge by identity, union lists append, and
   ``remove`` is the only way to drop an inherited entry — so "what did
   this suite actually get" is decidable by reading the code below.
3. **Identity covers the whole chain.** The resolved overlay plus every
   source file's digest feed ``chain_digest_of`` — editing a base changes
   the identity of every suite that inherits it, which is what the
   existing fail-closed manifest gate needs.

Never inherited: ``id``, ``version``, ``harbor.job`` (identity and job
selection are per-suite) and ``provenance`` unless the child opts in with
``provenance: inherit`` (inheriting someone else's license claim silently
would launder provenance). A grader with ``veto: true`` must be declared by
the child itself: veto changes what a score means.
"""

from __future__ import annotations

import json
from copy import deepcopy
from hashlib import sha256
from pathlib import Path
from typing import Any, Literal, Sequence

import yaml
from pydantic import BaseModel, ConfigDict

from aeval.suite_loader.paths import suite_path
from aeval.suite_models import HARBOR_OWNED_TOP_KEYS, SuiteError, SuiteSource

__all__ = [
    "MAX_INHERITANCE_DEPTH",
    "InheritanceResolution",
    "check_no_harbor_overlap",
    "merge_declarations",
    "chain_digest_of",
    "default_suites_root",
    "read_declaration",
    "resolve_inheritance",
]

MAX_INHERITANCE_DEPTH = 4

# Consumed by this resolver; never facts, never merged into the overlay.
RESOLVER_KEYS = ("extends", "remove")
# A base holds conventions. These select *which* suite runs, so they stay local.
NEVER_INHERITED = ("id", "version", "harbor.job")
# Keyed lists: same key replaces, new keys append (order-stable).
KEYED_LISTS = {"baselines": "id", "observables": "name", "metrics": "id"}
# Union lists: append + de-duplicate.
UNION_PATHS = {("verdict", "requirements"), ("driver", "require")}
REMOVABLE = ("baselines", "observables", "metrics", "graders", "requirements", "require")


class InheritanceResolution(BaseModel):
    model_config = ConfigDict(arbitrary_types_allowed=True, extra="forbid")

    data: dict[str, Any]  # merged overlay declaration (validated by the caller)
    sources: list[SuiteSource]
    suite_root: Path


def default_suites_root(suite_dir: Path) -> Path:
    """The suites root a suite's ``extends`` references resolve against.

    Walks up to the nearest ancestor named ``suites`` (the repo layout);
    otherwise the suite's own parent. Bases therefore live at
    ``<suites>/_base/…`` and are referenced without traversal.
    """
    suite_dir = Path(suite_dir).resolve()
    for parent in suite_dir.parents:
        if parent.name == "suites":
            return parent
    return suite_dir.parent


def read_declaration(path: Path) -> dict[str, Any]:
    try:
        text = path.read_text(encoding="utf-8")
        value = yaml.safe_load(text)
    except (OSError, UnicodeError, yaml.YAMLError) as exc:
        raise SuiteError(f"Cannot read suite manifest {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise SuiteError(f"suite manifest must be a mapping: {path}")
    return value


def check_no_harbor_overlap(data: dict[str, Any], path: Path) -> None:
    """Refuse a declaration that restates a Harbor-owned fact."""
    overlap = sorted(set(data) & HARBOR_OWNED_TOP_KEYS)
    if overlap:
        raise SuiteError(
            f"{path}: declaration restates Harbor-owned facts {overlap}; "
            "write them in the Harbor task/job files instead "
            "(only image.pin/image.rebuild narrowing is allowed)"
        )


def _chain_digest(sources: Sequence[SuiteSource], resolved: dict[str, Any]) -> str:
    payload = {
        "sources": [[s.role, s.path, s.digest] for s in sources],
        "resolved": _without_resolver_keys(resolved),
    }
    blob = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str)
    return sha256(blob.encode("utf-8")).hexdigest()


def chain_digest_of(sources: Sequence[SuiteSource], resolved: dict[str, Any]) -> str:
    """Stable digest of the resolved overlay **and** every source file.

    Base-less suites get a deterministic content digest too, so the value is
    always present; ``ResolvedSuite.suite_yaml_digest`` (raw child bytes)
    stays as-is so previously sealed evidence can still be recomputed.
    """
    return _chain_digest(sources, resolved)


def _without_resolver_keys(data: dict[str, Any]) -> dict[str, Any]:
    out = deepcopy(data)
    for key in RESOLVER_KEYS:
        out.pop(key, None)
    return out


def _extends_of(declaration: dict[str, Any], path: Path) -> list[str]:
    value = declaration.get("extends")
    if value is None:
        return []
    if isinstance(value, str):
        return [value]
    if isinstance(value, list) and all(isinstance(item, str) and item for item in value):
        return list(value)
    raise SuiteError(f"{path}: extends must be a path or a list of paths")


def _remove_of(declaration: dict[str, Any], path: Path) -> dict[str, list[str]]:
    value = declaration.get("remove")
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise SuiteError(f"{path}: remove must be a mapping of section -> identities")
    out: dict[str, list[str]] = {}
    for section, identities in value.items():
        if section not in REMOVABLE:
            raise SuiteError(
                f"{path}: remove.{section} is not removable; allowed: {list(REMOVABLE)}"
            )
        if not isinstance(identities, list) or not all(
            isinstance(item, str) and item for item in identities
        ):
            raise SuiteError(f"{path}: remove.{section} must be a list of identities")
        out[section] = list(identities)
    return out


def _check_base_declaration(declaration: dict[str, Any], path: Path) -> None:
    for dotted in NEVER_INHERITED:
        head, _, tail = dotted.partition(".")
        section = declaration.get(head)
        if tail:
            if isinstance(section, dict) and tail in section:
                raise SuiteError(
                    f"{path}: base declares {dotted}; that selects which suite runs "
                    "and must stay in the suite's own suite.yaml"
                )
        elif head in declaration:
            raise SuiteError(
                f"{path}: base declares {dotted}; bases hold conventions only — "
                "each suite states its own identity"
            )


def _require_item_key(item: Any, key: str, where: str) -> str:
    if not isinstance(item, dict) or not isinstance(item.get(key), str) or not item[key]:
        raise SuiteError(f"{where}: every entry must be a mapping with a non-empty {key!r}")
    return item[key]


def _merge_keyed(
    base_items: list[Any], child_items: list[Any], key: str, where: str
) -> list[Any]:
    merged: dict[str, Any] = {}
    for item in base_items:
        merged[_require_item_key(item, key, where)] = deepcopy(item)
    for item in child_items:
        merged[_require_item_key(item, key, where)] = deepcopy(item)  # same key replaces
    return list(merged.values())


def _union(base_items: list[Any], child_items: list[Any], where: str) -> list[Any]:
    out = list(base_items)
    for item in child_items:
        if not isinstance(item, str) or not item:
            raise SuiteError(f"{where}: entries must be non-empty strings")
        if item not in out:
            out.append(item)
    return out


def _merge_declarations(
    base: dict[str, Any],
    child: dict[str, Any],
    path: tuple[str, ...] = (),
    *,
    union_paths: frozenset[tuple[str, ...]] = UNION_PATHS,
    keyed_lists: dict[str, str] = KEYED_LISTS,
) -> dict[str, Any]:
    """Deep-merge one declaration onto another under an explicit policy.

    Policy lives in the arguments (not in the code path) because two layers use
    this engine: suite declarations and agent declarations. One engine means a
    base file behaves the same way wherever it is inherited from.
    """
    out = deepcopy(base)
    for key, value in child.items():
        if key in RESOLVER_KEYS:
            continue
        here = path + (key,)
        current = out.get(key)
        where = ".".join(here)
        if here in union_paths and isinstance(current, list) and isinstance(value, list):
            out[key] = _union(current, value, where)
        elif key in keyed_lists and isinstance(current, list) and isinstance(value, list):
            out[key] = _merge_keyed(current, value, keyed_lists[key], where)
        elif isinstance(current, dict) and isinstance(value, dict):
            out[key] = _merge_declarations(
                current, value, here, union_paths=union_paths, keyed_lists=keyed_lists
            )
        elif key == "graders" and isinstance(current, list) and isinstance(value, list):
            out[key] = [*deepcopy(current), *deepcopy(value)]
        else:
            out[key] = deepcopy(value)
    return out


def merge_declarations(
    base: dict[str, Any],
    child: dict[str, Any],
    *,
    union_paths: frozenset[tuple[str, ...]] = UNION_PATHS,
    keyed_lists: dict[str, str] = KEYED_LISTS,
) -> dict[str, Any]:
    """Merge two declarations with the shared inheritance engine."""
    return _merge_declarations(
        base, child, (), union_paths=union_paths, keyed_lists=keyed_lists
    )


def _apply_remove(merged: dict[str, Any], remove: dict[str, list[str]], path: Path) -> None:
    for section, identities in remove.items():
        if section == "graders":
            graders = merged.get("verdict", {}).get("graders")
            if not isinstance(graders, dict):
                raise SuiteError(
                    f"{path}: remove.graders needs a mapping form of verdict.graders"
                )
            for slot in identities:
                if slot not in graders:
                    raise SuiteError(f"{path}: remove.graders names an inherited grader that is absent: {slot!r}")
                del graders[slot]
            continue
        if section in ("requirements", "require"):
            holder = merged if section == "require" else merged.get("verdict", None)
            section_key = "require" if section == "require" else "requirements"
            # driver.require is a union list; verdict.requirements is a union list.
            target = holder.get(section_key) if isinstance(holder, dict) else None
            if not isinstance(target, list):
                raise SuiteError(f"{path}: remove.{section} needs an inherited list to remove from")
            for identity in identities:
                if identity not in target:
                    raise SuiteError(f"{path}: remove.{section} names an inherited entry that is absent: {identity!r}")
                target.remove(identity)
            continue
        items = merged.get(section)
        key = KEYED_LISTS[section]
        if not isinstance(items, list):
            raise SuiteError(f"{path}: remove.{section} needs an inherited {section} list")
        kept = []
        removed = set()
        for item in items:
            identity = _require_item_key(item, key, section)
            if identity in identities:
                removed.add(identity)
                continue
            kept.append(item)
        missing = [i for i in identities if i not in removed]
        if missing:
            raise SuiteError(
                f"{path}: remove.{section} names inherited entries that are absent: {missing}"
            )
        merged[section] = kept


def _grader_impls(declaration: dict[str, Any]) -> set[str]:
    verdict = declaration.get("verdict")
    if not isinstance(verdict, dict):
        return set()
    graders = verdict.get("graders")
    entries: list[Any] = []
    if isinstance(graders, dict):
        for value in graders.values():
            entries.extend(value if isinstance(value, list) else [value])
    elif isinstance(graders, list):
        entries.extend(graders)
    return {
        item["impl"] for item in entries if isinstance(item, dict) and isinstance(item.get("impl"), str)
    }


def _inherited_veto_graders(merged: dict[str, Any], child: dict[str, Any]) -> list[str]:
    declared = _grader_impls(child)
    offenders = []
    verdict = merged.get("verdict")
    graders = verdict.get("graders") if isinstance(verdict, dict) else None
    entries: list[Any] = []
    if isinstance(graders, dict):
        for value in graders.values():
            entries.extend(value if isinstance(value, list) else [value])
    elif isinstance(graders, list):
        entries.extend(graders)
    for item in entries:
        if isinstance(item, dict) and item.get("veto") and item.get("impl") not in declared:
            offenders.append(str(item.get("impl")))
    return offenders


def _resolve_one(
    suite_yaml: Path,
    root: Path,
    *,
    depth: int,
    seen: tuple[Path, ...],
    sources: list[SuiteSource],
) -> dict[str, Any]:
    if depth > MAX_INHERITANCE_DEPTH:
        raise SuiteError(
            f"{suite_yaml}: inheritance chain deeper than {MAX_INHERITANCE_DEPTH} "
            f"(cycle or over-nesting: {[str(p) for p in seen]})"
        )
    resolved = suite_yaml.resolve()
    if resolved in seen:
        chain = " -> ".join(str(p) for p in (*seen, resolved))
        raise SuiteError(f"Suite inheritance cycle: {chain}")
    declaration = read_declaration(suite_yaml)
    check_no_harbor_overlap(declaration, suite_yaml)

    role: Literal["base", "child"] = "child" if depth == 0 else "base"
    if role == "base":
        _check_base_declaration(declaration, suite_yaml)

    merged: dict[str, Any] = {}
    for reference in _extends_of(declaration, suite_yaml):
        base_path = suite_path(root, reference)
        if base_path.name == "suite.yaml":
            raise SuiteError(
                f"{suite_yaml}: extends {reference!r} — a base must not be named suite.yaml "
                "(suite discovery would treat it as a suite); use e.g. _base/<name>.base.yaml"
            )
        if not base_path.is_file():
            raise SuiteError(f"{suite_yaml}: extended base not found: {base_path}")
        base_declaration = _resolve_one(
            base_path,
            root,
            depth=depth + 1,
            seen=(*seen, resolved),
            sources=sources,
        )
        # Later bases win over earlier ones; the child still wins over all.
        merged = _merge_declarations(merged, base_declaration)

    child = {key: value for key, value in declaration.items() if key != "provenance"}
    inherit_provenance = declaration.get("provenance") == "inherit"
    if declaration.get("provenance") is not None and not (
        isinstance(declaration.get("provenance"), dict) or inherit_provenance
    ):
        raise SuiteError(
            f"{suite_yaml}: provenance must be a mapping or the literal `inherit`"
        )
    child_merged = _merge_declarations(merged, child)

    provenance = declaration.get("provenance")
    if isinstance(provenance, dict):
        child_merged["provenance"] = deepcopy(provenance)
    elif inherit_provenance:
        if not isinstance(merged.get("provenance"), dict):
            raise SuiteError(
                f"{suite_yaml}: provenance: inherit but no extended base declares provenance"
            )
    if depth == 0 and not isinstance(provenance, dict) and not inherit_provenance:
        # Never launder a base's provenance claim: an un-opted-in child gets
        # no provenance at all, and the check below refuses the load.
        child_merged.pop("provenance", None)
    if depth == 0 and not isinstance(child_merged.get("provenance"), dict):
        # A base is a convention layer and needs no provenance of its own; the
        # suite that actually runs must state where its tasks came from.
        raise SuiteError(
            f"{suite_yaml}: declares no provenance — a suite must state its own "
            "(source/license), or opt in explicitly with `provenance: inherit`"
        )
    # Removal runs before the veto check: dropping an inherited veto grader is
    # the legitimate way to say "this suite does not apply that veto".
    _apply_remove(child_merged, _remove_of(declaration, suite_yaml), suite_yaml)
    if depth == 0:
        harbor = child_merged.get("harbor")
        if not isinstance(harbor, dict) or not harbor.get("job"):
            raise SuiteError(
                f"{suite_yaml}: must select its own harbor.job (job selection is per-suite identity)"
            )
        offenders = _inherited_veto_graders(child_merged, declaration)
        if offenders:
            raise SuiteError(
                f"{suite_yaml}: grader(s) {offenders} carry veto=true but are inherited — "
                "a suite must declare its own veto policy"
            )
    # Base-first, child-last: the chain reads in application order.
    sources.append(
        SuiteSource(
            path=_relative_to_root(resolved, root),
            digest=sha256(suite_yaml.read_bytes()).hexdigest(),
            role=role,
        )
    )
    return child_merged


def _relative_to_root(path: Path, root: Path) -> str:
    try:
        return path.relative_to(root).as_posix()
    except ValueError:
        return path.name


def resolve_inheritance(
    suite_yaml: Path, suites_root: Path | None = None
) -> InheritanceResolution:
    """Resolve ``extends`` chains into one merged, still-unvalidated overlay."""
    suite_yaml = Path(suite_yaml)
    root = Path(suites_root).resolve() if suites_root is not None else default_suites_root(suite_yaml.parent)
    sources: list[SuiteSource] = []
    merged = _resolve_one(suite_yaml, root, depth=0, seen=(), sources=sources)
    return InheritanceResolution(data=merged, sources=sources, suite_root=root)
