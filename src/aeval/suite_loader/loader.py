"""Suite discovery and thin-overlay loading (plan §1).

A suite is a directory containing ``suite.yaml`` under a suites root.
Loading is fail-loud: duplicates, overlaps with Harbor-owned facts,
missing mandatory sections and bad provenance all raise SuiteError.
"""

from __future__ import annotations

from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Sequence

import yaml

from aeval.suite_loader.inheritance import (
    chain_digest_of,
    check_no_harbor_overlap,
    default_suites_root,
    resolve_inheritance,
)
from aeval.suite_models import (
    HarborInputs,
    ResolvedSuite,
    SuiteError,
    SuiteOverlay,
    overlay_digest_of,
)
from aeval.contracts import (
    DshReleaseLock,
    HarborLock,
    ImageIdentity,
    PluginIdentity,
    PythonEnvironmentLock,
    RuntimeLock,
)

__all__ = [
    "discover_suites",
    "load_suite",
    "resolve_harbor_inputs",
    "overlay_digest",
    "inherited_source_paths",
    "assert_unique_suite_identity",
    "render_suite_explanation",
]


def discover_suites(suites_dirs: Sequence[Path]) -> list[Path]:
    """Return suite directories (containing suite.yaml), deepest-first stable.

    Every directory that has a suite.yaml counts; nested suites are
    independent. Any path segment starting with ``_`` is skipped: that is
    where shared convention *bases* live (``_base/*.base.yaml``), and a base
    must never be loaded or identity-checked as a suite. A suites root that
    does not exist is an error — silent empty discovery would let a run
    start with zero suites and look successful.
    """
    found: list[Path] = []
    for root in suites_dirs:
        root = Path(root)
        if not root.is_dir():
            raise SuiteError(f"suites directory does not exist: {root}")
        for candidate in sorted(root.rglob("suite.yaml")):
            if any(part.startswith("_") for part in candidate.relative_to(root).parts[:-1]):
                continue
            found.append(candidate.parent)
    return found


def _read_titles_file(path: Path) -> dict[str, str]:
    """读一个 task 标题文件：缺失返回空；内容必须是纯 str→str 映射。

    坏文件立刻报错而不是静默忽略——显示名错了和判分锚错了一样误
    导人。
    """
    if not path.is_file():
        return {}
    try:
        data = yaml.safe_load(path.read_text("utf-8")) or {}
    except yaml.YAMLError as exc:
        raise SuiteError(f"{path}: invalid YAML: {exc}") from exc
    if not isinstance(data, dict) or not all(
        isinstance(key, str) and isinstance(value, str) for key, value in data.items()
    ):
        raise SuiteError(
            f"{path}: expected a flat task_id -> title string mapping"
        )
    return data


def _load_task_titles(suite_dir: Path) -> dict[str, str]:
    """套件的中文任务显示名（task_id → 标题），报告渲染用。

    两个来源、互不干扰：``task_titles.yaml`` 是套件自写的（它自有任
    务的显示名），``task_titles.cases.yaml`` 是共享用例库注入器生成
    的（库属任务，标题来自各类别 checker 的 CASES）。合并时套件侧
    优先——混合套件里同名任务的命名权在套件。
    """
    titles: dict[str, str] = {}
    titles.update(_read_titles_file(suite_dir / "task_titles.cases.yaml"))
    titles.update(_read_titles_file(suite_dir / "task_titles.yaml"))
    return titles


def _load_task_categories(suite_dir: Path) -> tuple[dict[str, str], str | None]:
    """类别聚合声明：task_categories.yaml（套件自写）与注入器生成的
    task_categories.cases.yaml 合并，套件侧优先。

    结构：``categories:``（类别键 → 中文显示名）+ 可选 ``default:``
    （无点分前缀任务归到的类别键）。报告按 task_id 首个点前的前缀
    归组，无点的归 default——确定性规则，无需逐任务映射。
    """
    merged: dict[str, str] = {}
    default: str | None = None
    for name in ("task_categories.cases.yaml", "task_categories.yaml"):
        path = suite_dir / name
        if not path.is_file():
            continue
        try:
            data = yaml.safe_load(path.read_text("utf-8")) or {}
        except yaml.YAMLError as exc:
            raise SuiteError(f"{path}: invalid YAML: {exc}") from exc
        if not isinstance(data, dict):
            raise SuiteError(f"{path}: expected a mapping with 'categories'")
        categories = data.get("categories")
        if not isinstance(categories, dict) or not all(
            isinstance(key, str) and isinstance(value, str)
            for key, value in categories.items()
        ):
            raise SuiteError(
                f"{path}: categories must be a flat key -> name string mapping"
            )
        merged.update(categories)
        raw_default = data.get("default")
        if raw_default is not None:
            if not isinstance(raw_default, str):
                raise SuiteError(f"{path}: default must be a category key")
            default = raw_default
    return merged, default


def load_suite(path: Path, suites_root: Path | None = None) -> ResolvedSuite:
    """Load a suite: resolve its inheritance chain, then validate the overlay.

    Every file in the chain goes through the Harbor-overlap check (a base
    may not restate a Harbor-owned fact either), and the chain's digests
    feed ``overlay_chain_digest`` so inherited content is part of run
    identity — see ``aeval.suite_loader.inheritance``.
    """
    path = Path(path)
    suite_yaml = path / "suite.yaml"
    if not suite_yaml.is_file():
        raise SuiteError(f"not a suite directory (missing suite.yaml): {path}")
    resolution = resolve_inheritance(suite_yaml, suites_root)
    try:
        overlay = SuiteOverlay.model_validate(resolution.data)
    except SuiteError:
        raise
    except Exception as exc:
        raise SuiteError(f"{suite_yaml}: invalid suite overlay: {exc}") from exc
    category_names, default_category = _load_task_categories(path)
    return ResolvedSuite(
        overlay=overlay,
        suite_dir=path,
        # Raw child bytes: unchanged meaning, so pre-inheritance evidence
        # still recomputes.
        suite_yaml_digest=overlay_digest_of(suite_yaml),
        overlay_chain_digest=chain_digest_of(resolution.sources, resolution.data),
        sources=list(resolution.sources),
        task_titles=_load_task_titles(path),
        category_names=category_names,
        default_category=default_category,
    )


def resolve_harbor_inputs(suite: ResolvedSuite) -> HarborInputs:
    """Resolve and verify the Harbor files the overlay points at.

    They must be portable relative paths to TOML, JSON or YAML mappings.
    Resolve symlinks before checking containment; record digests so run
    identity covers the referenced declarations.
    """
    from hashlib import sha256
    import json
    import tomllib

    import yaml

    root = Path(suite.suite_dir).resolve()

    def _resolve(reference: str) -> Path:
        posix = PurePosixPath(reference)
        windows = PureWindowsPath(reference)
        if ".." in posix.parts or ".." in windows.parts:
            raise SuiteError(
                f"Harbor reference escapes the suite directory "
                f"(traversal is forbidden): {reference!r}"
            )
        if (
            not posix.parts
            or posix.is_absolute()
            or windows.drive
            or windows.root
            or any(c in '<>:"\\|?*' or ord(c) < 32 for c in reference)
            or any(
                part.endswith((".", " ")) or PureWindowsPath(part).is_reserved()
                for part in posix.parts
            )
        ):
            raise SuiteError(
                f"Harbor reference must be a portable relative path "
                f"using forward slashes: {reference!r}"
            )
        try:
            resolved = (root / reference).resolve()
            resolved.relative_to(root)
        except ValueError as exc:
            raise SuiteError(
                f"Harbor reference escapes the suite directory: {reference!r}"
            ) from exc
        except (OSError, RuntimeError) as exc:
            raise SuiteError(f"Cannot resolve Harbor reference {reference!r}: {exc}") from exc
        if not resolved.is_file():
            raise SuiteError(f"Harbor file not found: {resolved}")
        return resolved

    def _parse_and_digest(path: Path) -> str:
        try:
            raw = path.read_bytes()
            text = raw.decode("utf-8")
            suffix = path.suffix.lower()
            if suffix == ".toml":
                data = tomllib.loads(text)
            elif suffix == ".json":
                data = json.loads(text)
            elif suffix in (".yaml", ".yml"):
                data = yaml.safe_load(text)
            else:
                raise SuiteError(f"Unsupported Harbor file format: {path}")
        except (OSError, UnicodeError, ValueError, yaml.YAMLError) as exc:
            raise SuiteError(f"Invalid Harbor file {path}: {exc}") from exc
        if not isinstance(data, dict):
            raise SuiteError(f"Harbor file must be a mapping: {path}")
        return sha256(raw).hexdigest()

    inputs = suite.overlay.harbor
    dataset_path = _resolve(inputs.dataset)
    job_path = _resolve(inputs.job)
    dataset_digest = _parse_and_digest(dataset_path)
    job_digest = _parse_and_digest(job_path)
    for name, expected, actual in (
        ("dataset", inputs.dataset_digest, dataset_digest),
        ("job", inputs.job_digest, job_digest),
    ):
        if expected is not None and expected != actual:
            raise SuiteError(f"Harbor {name} digest mismatch: expected {expected}, actual {actual}")
    return HarborInputs(
        dataset=inputs.dataset,
        job=inputs.job,
        dataset_digest=dataset_digest,
        job_digest=job_digest,
    )


def inherited_source_paths(suite: ResolvedSuite) -> list[Path]:
    """Absolute paths of the base files this suite extends (chain order).

    Provenance must cover them: with inheritance, part of a suite's
    effective configuration lives outside its own directory.
    """
    root = default_suites_root(Path(suite.suite_dir))
    paths: list[Path] = []
    for source in suite.sources:
        if source.role != "base":
            continue
        candidate = root / source.path
        if candidate.is_file():
            paths.append(candidate)
    return paths


def overlay_digest(suite_yaml: Path) -> str:
    suite_yaml = Path(suite_yaml)
    if not suite_yaml.is_file():
        raise SuiteError(f"suite.yaml not found: {suite_yaml}")
    return overlay_digest_of(suite_yaml)


def assert_unique_suite_identity(suites: Sequence[ResolvedSuite]) -> None:
    """Reject duplicate suite ids, including different versions or content.

    Selection by id must never silently choose one of several suites.
    Version and digest still participate in each suite's run identity.
    """
    seen: dict[str, tuple[str, ResolvedSuite]] = {}
    for suite in suites:
        key = suite.id
        if key in seen:
            prev_digest, prev = seen[key]
            if prev_digest != suite.identity_digest:
                raise SuiteError(
                    f"duplicate suite identity {key!r} with different content:\n"
                    f"  {prev.suite_dir} (digest {prev_digest[:12]})\n"
                    f"  {suite.suite_dir} (digest {suite.identity_digest[:12]})"
                )
            raise SuiteError(
                f"duplicate suite identity {key!r} at {prev.suite_dir} and "
                f"{suite.suite_dir}"
            )
        seen[key] = (suite.identity_digest, suite)


def render_suite_explanation(suite: ResolvedSuite) -> str:
    """Read-only composed view of overlay + Harbor files (plan §9.1).

    The rendered page is an artifact, never an input — it annotates
    where each fact comes from so a reader never mistakes the render
    for the source of truth.
    """
    o = suite.overlay
    lines = [
        f"# Suite {o.id} v{o.version}",
        "",
        f"- suite.yaml digest: {suite.suite_yaml_digest[:12]}…",
        f"- overlay chain digest: {suite.identity_digest[:12]}…",
        f"- Harbor dataset: {o.harbor.dataset}",
        f"- Harbor job: {o.harbor.job}",
        f"- clock: {o.clock.mode}" + (f" epoch={o.clock.epoch}" if o.clock.epoch else ""),
        f"- driver.require: {', '.join(o.driver.require) or '(none)'}",
        f"- provenance: source={o.provenance.source} license={o.provenance.license}"
        f" data_imported={o.provenance.data_imported}",
        "",
        "## Sources (inheritance chain, base first)",
    ]
    for source in suite.sources:
        lines.append(f"- [{source.role}] {source.path} (sha256 {source.digest[:12]}…)")
    if not suite.sources or len(suite.sources) == 1:
        lines.append("- (no extends: this suite.yaml is the only source)")
    lines += [
        "",
        "## Baselines (from the resolved overlay)",
    ]
    for b in o.baselines:
        if b.assert_expr is not None:
            lines.append(f"- {b.id}: assert={b.assert_expr!r}")
        else:
            lines.append(f"- {b.id}: probe={b.probe!r} equals={b.equals!r}")
    lines.append("")
    lines.append("## Observables (from the resolved overlay)")
    for obs in o.observables:
        lines.append(f"- {obs.name}: {obs.type} <- {obs.source}")
    lines.append("")
    lines.append("## Verdict (from the resolved overlay)")
    lines.append(f"- requirements: {', '.join(o.verdict.requirements)}")
    for g in o.verdict.resolved_graders():
        lines.append(
            f"- grader {g.impl} layer={g.layer} veto={g.veto}"
        )
    lines.append("")
    lines.append("## Image narrowing (overlay, replaces Harbor values)")
    for name, action in o.image.items():
        if action.pin:
            lines.append(f"- {name}: pin {action.original or '?'} -> {action.pin}")
        elif action.rebuild:
            lines.append(f"- {name}: force rebuild via our CI")
    if not o.image:
        lines.append("- (none)")
    lines.append("")
    lines.append("## Metrics (net-new; pass@k comes from Harbor)")
    for m in o.metrics:
        lines.append(f"- {m.id}: {m.kind}" + (f" k={m.k}" if m.k else ""))
    lines.append("")
    lines.append(
        "NOTE: environment image, seeds, egress, budget, k/parallel/retry "
        "live in the Harbor task/job files referenced above; convention facts "
        "may come from an extended base, listed under Sources — this page is "
        "a rendered artifact, not an input."
    )
    return "\n".join(lines)
