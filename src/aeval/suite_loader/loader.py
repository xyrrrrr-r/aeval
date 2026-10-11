"""Suite discovery and thin-overlay loading.

A suite is a directory containing ``suite.yaml`` under a suites root.
Loading is fail-loud: duplicates, overlaps with Harbor-owned facts,
missing mandatory sections and bad provenance all raise SuiteError.

New format (``families:`` listed): the family declarations in
``families/<key>.yaml`` are loaded here too — strict parsing
(unknown fields are errors, P9), one-way locking (a listed file must
exist and parse, D6), key-conflict detection, (case, engine) join
uniqueness (P4) and guard/family no-double-run. The activated files'
bytes join ``overlay_chain_digest`` (v4 design) — editing a family changes
run identity; unactivated library files are inert.
"""

from __future__ import annotations

from hashlib import sha256
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any, Sequence

import yaml

from aeval.suite_loader.catalog import bind_metric_catalog
from aeval.suite_loader.inheritance import (
    chain_digest_of,
    check_no_harbor_overlap,
    default_suites_root,
    resolve_inheritance,
)
from aeval.suite_models import (
    FamilySpec,
    GraderDeclaration,
    HarborInputs,
    ResolvedSuite,
    SuiteError,
    SuiteOverlay,
    SuiteSource,
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


def _load_families(
    suite_dir: Path, declared: Sequence[str], baseline_impl: str | None
) -> tuple[dict[str, FamilySpec], dict[str, str], list[SuiteSource]]:
    """加载激活的族声明（单向锁定，D6；严格解析，P9）。

    返回 ``(族键 → 规格, 族键 → 相对路径, 激活文件的 chain 来源)``。
    逐条 fail-closed（v4 加载期表）：

    - 列出的文件缺失 / 非 YAML 映射 / 解析失败 → 报错；
    - 未知字段（pydantic ``extra="forbid"``）→ 报错；
    - 同套件内类别键冲突（两路径同文件名主干）→ 报错；
    - outcome 层引擎但 impl ≠ 官方基线 → 报错（分组式类别是 D7
      特例的唯一豁免：引擎 = default 槽 impl）；
    - 同一 (用例, 引擎 impl) 被两个类别声明 → 报错（join 唯一，P4）；
    - 同一 impl 同时出现在守卫与族 → 在 load_suite 汇总处报错。
    """
    families: dict[str, FamilySpec] = {}
    paths: dict[str, str] = {}
    sources: list[SuiteSource] = []
    seen_cases: dict[tuple[str, str], str] = {}  # (task_id, impl) -> family key
    for reference in declared:
        if not isinstance(reference, str) or not reference.strip():
            raise SuiteError(
                "families entries must be non-empty relative paths"
            )
        rel = PurePosixPath(reference)
        if (
            ".." in rel.parts
            or rel.is_absolute()
            or PureWindowsPath(reference).is_absolute()
            or len(rel.parts) != 2
            or rel.parts[0] != "families"
            or rel.suffix not in (".yaml", ".yml")
        ):
            raise SuiteError(
                f"families entry must be families/<key>.yaml "
                f"(portable relative path): {reference!r}"
            )
        key = rel.stem
        path = suite_dir / rel
        if not path.is_file():
            raise SuiteError(
                f"families lists {reference!r} but the file is missing "
                "(one-way locking: listing is activation, activation requires "
                "the declaration)"
            )
        if key in families:
            raise SuiteError(
                f"duplicate grade category key {key!r} — two activated "
                "declarations share a file stem"
            )
        try:
            raw = yaml.safe_load(path.read_text("utf-8")) or {}
        except (OSError, UnicodeError, yaml.YAMLError) as exc:
            raise SuiteError(f"{path}: cannot read grade category: {exc}") from exc
        if not isinstance(raw, dict):
            raise SuiteError(f"{path}: grade category must be a mapping")
        try:
            spec = FamilySpec.model_validate(raw)
        except SuiteError:
            raise
        except Exception as exc:
            raise SuiteError(f"{path}: invalid grade category {key!r}: {exc}") from exc
        # 分组式类别豁免：outcome 层族引擎必须与官方基线同 impl（D7）。
        if spec.engine.layer in ("outcome", "both"):
            if baseline_impl is None or spec.engine.impl != baseline_impl:
                raise SuiteError(
                    f"grade category {key!r} declares an outcome-layer engine "
                    f"{spec.engine.impl!r} — only the grouping-category special "
                    "case allows that, and only with the baseline's own impl"
                )
        # v5：指标清单是判分真源，空清单只允许分组式类别（引擎 = 官方
        # 基线、族自带零指标）。其余族漏写 metrics 是声明错误，不是
        # "用引擎里的清单兜底"。
        if not spec.metrics and spec.engine.impl != baseline_impl:
            raise SuiteError(
                f"grade category {key!r} declares no metrics — a family's "
                "metric list is the judging truth; only the grouping-style "
                "category (engine = the official baseline) may declare none"
            )
        for task_id in spec.task_ids():
            owner = seen_cases.get((task_id, spec.engine.impl))
            if owner is not None:
                raise SuiteError(
                    f"case {task_id!r} with engine {spec.engine.impl!r} is declared "
                    f"by both {owner!r} and {key!r} — (case, engine) ownership must "
                    "stay unique so report joins are unambiguous"
                )
            seen_cases[(task_id, spec.engine.impl)] = key
        families[key] = spec
        paths[key] = reference
        sources.append(
            SuiteSource(
                path=reference,
                digest=sha256(path.read_bytes()).hexdigest(),
                role="family",
            )
        )
    return families, paths, sources


def _check_family_compatibility(
    suite_dir: Path, overlay: SuiteOverlay, families: dict[str, FamilySpec]
) -> None:
    """族声明的互斥、防双跑与按需通道约束（加载期）。"""
    if not families:
        return
    # 锚点密封通道按需强制：族里有指标吃输入 ⇒ 必须有密封通道（判分只
    # 从密封副本读输入，重判分不依赖套件仓当前状态）。结构判定族（没有
    # 指标吃输入）不需要这条通道，也不因缺它报错。
    needs_anchors = any(
        any(bool(metric.inputs) for metric in spec.metrics)
        for spec in families.values()
    )
    if needs_anchors and overlay.verdict.anchors != "task_anchors":
        raise SuiteError(
            "verdict.anchors: task_anchors is required when a family's metrics "
            "carry per-task inputs — judging reads inputs from the sealed copy "
            "(a structural family that consumes no inputs does not need it)"
        )
    for legacy_name in ("task_categories.yaml", "task_categories.cases.yaml",
                        "task_titles.yaml", "task_titles.cases.yaml"):
        if (suite_dir / legacy_name).is_file():
            raise SuiteError(
                f"new-format suites keep grading declarations in one place: "
                f"{legacy_name} coexists with families/ — move its "
                "values into the family declarations"
            )
    guard_impls = {guard.impl for guard in overlay.verdict.guards}
    for key, spec in families.items():
        if spec.engine.impl in guard_impls:
            raise SuiteError(
                f"grader impl {spec.engine.impl!r} appears both as a guard and "
                f"as family {key!r}'s engine — a grader runs once per trial"
            )
        if spec.engine.veto and spec.engine.layer == "outcome":
            raise SuiteError(
                f"family {key!r} engine is outcome-layer with veto — veto is a "
                "trajectory-guard/family concern; the baseline never vetoes"
            )


def load_suite(path: Path, suites_root: Path | None = None) -> ResolvedSuite:
    """Load a suite: resolve its inheritance chain, then validate the overlay.

    Every file in the chain goes through the Harbor-overlap check (a base
    may not restate a Harbor-owned fact either), and the chain's digests
    feed ``overlay_chain_digest`` so inherited content is part of run
    identity — see ``aeval.suite_loader.inheritance``. Activated family
    declarations (new format) load the same way: their bytes join the
    chain, so a family edit is a run-identity change (v4 design).
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
    # 族声明（新格式）：单向锁定加载 + 结构校验 + 字节进 chain digest。
    baseline_impl = None
    graders = overlay.verdict.graders
    if isinstance(graders.get("default"), GraderDeclaration):
        baseline_impl = graders["default"].impl
    families, family_paths, family_sources = _load_families(
        path, overlay.families, baseline_impl
    )
    # v5 Step 6：指标目录绑定（一处定义、两处引用）。族文件与守卫只写
    # id 引用，定义来自 verdict.metric_catalog；绑定发生在密封模型与锚点
    # 投影之前，所以下游读到的仍是完整定义。
    overlay, families = bind_metric_catalog(
        overlay, families, suite_label=str(suite_yaml)
    )
    _check_family_compatibility(path, overlay, families)
    sources = [*resolution.sources, *family_sources]

    # v5：判分声明以族为家，维度模型是密封映射
    # （families/baseline/guards）——没有第二条来源，也没有
    # 套件级 task_categories/task_titles 侧车文件。显示名与标题从族声明
    # 的 scope 投影。grader_id 由运行入口解析模块后补
    # （resolve_grader_identities）：报告 join 只读密封映射，不 import
    # 判分器（P4）。families 为空（baseline-only）时也走同一条路。
    task_titles: dict[str, str] = {}
    for spec in families.values():
        for task_id, binding in spec.scope.items():
            if binding.title:
                task_titles.setdefault(task_id, binding.title)
    dimension_model: dict[str, object] = {
        "families": {
            key: spec.model_dump(mode="json", exclude_none=True)
            for key, spec in families.items()
        },
        "blocks": dict(overlay.blocks),
        "baseline": (
            {"impl": baseline_impl}
            if baseline_impl is not None
            else None
        ),
        "guards": [
            guard.model_dump(mode="json", exclude_none=True)
            for guard in overlay.verdict.guards
        ],
    }
    return ResolvedSuite(
        overlay=overlay,
        suite_dir=path,
        # Raw child bytes: unchanged meaning, so pre-inheritance evidence
        # still recomputes.
        suite_yaml_digest=overlay_digest_of(suite_yaml),
        overlay_chain_digest=chain_digest_of(sources, resolution.data),
        sources=sources,
        task_titles=task_titles,
        dimension_model=dimension_model,
        families=families,
        family_paths=family_paths,
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
    """Read-only composed view of overlay + Harbor files.

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
    graders = o.verdict.graders
    lines.append(
        f"- baseline (official): {graders['default'].impl}"
        if "default" in graders
        else "- baseline (official): ?"
    )
    for guard in o.verdict.guards:
        lines.append(
            f"- guard {guard.impl} layer={guard.layer} veto={guard.veto}"
            " (suite-wide validity gate, no category row)"
        )
    if not o.verdict.guards:
        lines.append("- guards: (none)")
    lines.append(f"- anchors channel: {o.verdict.anchors}")
    if suite.has_families:
        lines.append("")
        lines.append("## Grade categories (families, activation order)")
        for key, spec in suite.families.items():
            engine = spec.engine
            veto_mark = " veto" if engine.veto else ""
            lines.append(
                f"- {key} ({spec.name}, block={spec.block}, weight={spec.weight:g}, "
                f"threshold={spec.threshold:g}"
                + (", redline" if spec.redline else "")
                + f") — engine {engine.impl} layer={engine.layer}{veto_mark}"
            )
            for task_id in spec.task_ids():
                binding = spec.scope.get(task_id)
                title = f"「{binding.title}」" if binding and binding.title else ""
                inputs = spec.inputs_for(task_id)
                inputs_mark = (
                    " · 输入 " + "/".join(sorted(inputs)) if inputs else ""
                )
                lines.append(f"    · {task_id} [{case.role}] {title}{inputs_mark}")
        lines.append("")
        lines.append("## Blocks (radar axes)")
        for block, name in o.blocks.items():
            lines.append(f"- {block}: {name}")
        if not o.blocks:
            lines.append("- (none)")
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
