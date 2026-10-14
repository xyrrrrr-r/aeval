"""The DSH control stack's read-only wiring facts.

What lives here is DSH's own, and it is here — not in the core doctor —
precisely because the core may not name an adapter package:

* the compiled control package an installation carries (``controlRoot`` in the
  broker spec, or ``AEVAL_DSH_CONTROL_ROOT``), and the two entry files a run
  demands of it: the broker entry and the official session reader — the two
  recorded failures are ``official DSH session reader not found`` and
  ``carries no controlDist and no controlRoot with a built dist/``;
* the packages that package imports DIRECTLY, against the pinned release
  slice: a marketplace copy installs no peers, which reaches the operator as
  ``Cannot find package '@deepseek-ai/dsh-llm'`` mid-run;
* the peer window a DSH profile must satisfy, read from the installed control
  package's own ``peerDependencies`` — a profile outside it makes the DSH
  plugin silently skip the whole bundle.

The hook is registered on the DSH control flavor
(``aeval.agents.dsh.control_flavor``), so the flavor registry is the only path
from core to it.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Mapping

from aeval.agents.control_doctor import ControlDoctorFacts
from aeval.agents.dsh.agent import (
    SESSION_READER_ENV,
    SESSION_READER_ROOT_ENV,
    DshRunError,
)
from aeval.agents.dsh.release import DSH_CONTROL_DIRECT_IMPORTS

__all__ = ["dsh_control_doctor"]

#: The npm package the operator installs for the in-sandbox stack, and the peer
#: a DSH profile must carry for it.
CONTROL_PACKAGE = "dsh-eval-control"
PEER_PACKAGE = "@deepseek-ai/dsh"
#: The window aeval is pinned against; used only when no installed control
#: package declares its own peers.
FALLBACK_PEER_WINDOW = ">=0.2.0-0 <0.3.0-0"


def _package_json(root: Path) -> dict[str, Any] | None:
    import json

    try:
        loaded = json.loads((Path(root) / "package.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return loaded if isinstance(loaded, dict) else None


def _control_package(
    spec: Mapping[str, Any] | None, env: Mapping[str, str]
) -> tuple[Path | None, Path | None, str | None]:
    """``(package root, dist dir, where it came from)`` — one operator input.

    The broker spec names the package (``controlRoot`` for the package,
    ``controlDist`` for its built ``dist/``); the environment variable is the
    documented fallback when no spec is in play.
    """
    raw = spec or {}
    declared = raw.get("controlRoot")
    if isinstance(declared, str) and declared:
        return Path(declared), Path(declared) / "dist", "broker spec controlRoot"
    dist = raw.get("controlDist")
    if isinstance(dist, str) and dist:
        return Path(dist).parent, Path(dist), "broker spec controlDist"
    override = env.get(SESSION_READER_ROOT_ENV, "").strip()
    if override:
        return Path(override), Path(override) / "dist", SESSION_READER_ROOT_ENV
    return None, None, None


def _resolve_control_package(
    spec: Mapping[str, Any] | None, env: Mapping[str, str]
) -> tuple[Path | None, Path | None, str | None]:
    """``(package root, dist dir, where it came from)``.

    Declared first (the broker spec's ``controlRoot``/``controlDist``, then the
    documented environment variable); when nothing declares one, the resolution
    a run itself performs — the sibling checkout, the cwd's node_modules, the
    DSH profiles and the npm global root — answers, so a spec written from this
    fact names the package the run will actually load.
    """
    root, dist, source = _control_package(spec, env)
    if root is not None:
        return root, dist, source
    try:
        # module attribute, not a bound name: resolving a session reader is
        # exactly what a hermetic test replaces
        from aeval.agents.dsh import agent as agent_mod

        reader = Path(agent_mod.resolve_session_reader())
    except (DshRunError, OSError):
        return None, None, None
    return reader.parent.parent, reader.parent, None


def _check_control_dist(
    root: Path | None, dist: Path | None, source: str | None
) -> tuple[str, str, str | None]:
    if root is None:
        return (
            "fail",
            "official DSH session reader not found and no controlRoot is declared — "
            "the in-sandbox control stack cannot be located",
            f"export {SESSION_READER_ROOT_ENV}=/abs/path/to/node_modules/{CONTROL_PACKAGE}"
            "   # or set controlRoot in the broker spec; then npm install && npm run build",
        )
    if source is None:
        return "ok", f"{dist} (resolved, not declared)", None
    built = dist if dist is not None else Path(root) / "dist"
    missing = [
        name
        for name in ("broker_main.js", "session_reader.js")
        if not (built / name).is_file()
    ]
    if missing:
        detail = (
            f"{built} (from {source}) is missing {', '.join(missing)}"
            if built.is_dir()
            else f"{built} (from {source}) is not a built dist/"
        )
        return "fail", detail, f"cd {root} && npm install --no-audit --no-fund && npm run build"
    return "ok", f"{built} (broker_main.js, session_reader.js)", None


def _check_control_deps(root: Path | None) -> tuple[str, str, str | None]:
    if root is None:
        return "warn", "not checked — no control package is named", None
    if _package_json(root) is None:
        return (
            "warn",
            f"{root} has no readable package.json — not an npm package install",
            None,
        )
    modules = Path(root) / "node_modules"
    missing = [
        name
        for name in DSH_CONTROL_DIRECT_IMPORTS
        if not (modules / name / "package.json").is_file()
    ]
    if missing:
        shown = ", ".join(missing[:6]) + ("…" if len(missing) > 6 else "")
        return (
            "fail",
            f"{root} is missing {len(missing)} package(s) the control stack imports "
            f"directly ({shown}) — a marketplace copy installs no peers",
            f"cd {root} && npm install --no-audit --no-fund",
        )
    return "ok", f"{len(DSH_CONTROL_DIRECT_IMPORTS)} direct package(s) present", None


def _profile_installs() -> list[tuple[str, str | None]]:
    """Every installed peer package in a DSH profile or npm global root.

    The marketplace route puts the plugin — and the peer DSH it needs — inside
    a DSH profile's ``node_modules``; an npm global install is the other place
    an operator ends up with one. Both are read; neither is written.
    """
    from aeval.agents.dsh.agent import _dsh_home_dirs, _npm_global_prefixes

    scope, _, name = PEER_PACKAGE.partition("/")
    candidates: list[Path] = []
    for home in _dsh_home_dirs():
        profiles = home / "profiles"
        if profiles.is_dir():
            for profile in sorted(p for p in profiles.iterdir() if p.is_dir()):
                candidates.append(profile / "node_modules" / scope / name)
        candidates.append(home / "node_modules" / scope / name)
    for prefix in _npm_global_prefixes():
        candidates.append(prefix / "lib" / "node_modules" / scope / name)
    installs: list[tuple[str, str | None]] = []
    seen: set[Path] = set()
    for package in candidates:
        package_json = package / "package.json"
        if not package_json.is_file() or package in seen:
            continue
        seen.add(package)
        data = _package_json(package)
        if data is None:
            installs.append((str(package), None))
            continue
        version = data.get("version")
        installs.append((str(package), version if isinstance(version, str) else None))
    return installs


def _peer_window(root: Path | None) -> tuple[str, str]:
    if root is not None:
        data = _package_json(root) or {}
        peers = data.get("peerDependencies")
        declared = peers.get(PEER_PACKAGE) if isinstance(peers, dict) else None
        if isinstance(declared, str) and declared:
            return declared, f"{root}/package.json"
    return FALLBACK_PEER_WINDOW, "the aeval pin"


def _version_satisfies(version: str, range_text: str) -> bool:
    from aeval.doctor import version_satisfies

    return version_satisfies(version, range_text)


def _check_profile(root: Path | None) -> tuple[str, str, str | None]:
    peer_window, source = _peer_window(root)
    installs = _profile_installs()
    if not installs:
        return (
            "warn",
            "no peer package install found under the DSH homes or npm global roots — "
            "the control plugin is added into a DSH profile, so there is nothing to compare",
            f"dsh plugin add {CONTROL_PACKAGE}   # then re-run: aeval doctor",
        )
    mismatches: list[str] = []
    unknown: list[str] = []
    for where, version in installs:
        if version is None:
            unknown.append(where)
        elif not _version_satisfies(version, peer_window):
            mismatches.append(f"{where} has {PEER_PACKAGE}@{version}")
    if mismatches:
        return (
            "fail",
            "; ".join(mismatches)
            + f" — outside the peer window {peer_window} (from {source}); a mismatched "
            "profile makes the plugin skip the whole bundle",
            f"dsh plugin add {CONTROL_PACKAGE}   # reinstall so the profile gets a peer "
            f"satisfying {peer_window}",
        )
    if unknown:
        return "warn", f"unreadable {PEER_PACKAGE} version: {', '.join(unknown)}", None
    return (
        "ok",
        "; ".join(f"{where} @ {version}" for where, version in installs)
        + f" (peer {peer_window})",
        None,
    )


def dsh_control_doctor(
    *, spec: Mapping[str, Any] | None, env: Mapping[str, str], agents_dir: Path
) -> ControlDoctorFacts:
    """The DSH control stack's three adapter-owned checks."""
    del agents_dir          # the facts are host-side, not declaration-side
    root, dist, source = _resolve_control_package(spec, env)
    # Only a DECLARED package (a spec or the documented variable) drives the
    # direct-import and peer-window checks: those ask what the operator pinned,
    # and "nothing is named" is the honest answer when nothing is named. The
    # resolved package still answers where the broker entry lives.
    declared_root = root if source is not None else None
    engines = (_package_json(root) or {}).get("engines") if root is not None else None
    node_engines = (
        engines.get("node") if isinstance(engines, dict)
        and isinstance(engines.get("node"), str) else None
    )
    return ControlDoctorFacts(
        checks={
            "control-dist": _check_control_dist(root, dist, source),
            "control-deps": _check_control_deps(declared_root),
            "dsh-profile": _check_profile(declared_root),
        },
        node_engines=node_engines,
        control_root=root,
    )
