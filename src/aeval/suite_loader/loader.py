"""Suite discovery and thin-overlay loading (plan §1).

A suite is a directory containing ``suite.yaml`` under a suites root.
Loading is fail-loud: duplicates, overlaps with Harbor-owned facts,
missing mandatory sections and bad provenance all raise SuiteError.
"""

from __future__ import annotations

from pathlib import Path
from typing import Sequence

from aeval.suite_models import (
    HARBOR_OWNED_TOP_KEYS,
    HarborInputs,
    ResolvedSuite,
    SuiteError,
    SuiteOverlay,
    load_suite_yaml,
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
    "assert_unique_suite_identity",
    "render_suite_explanation",
]


def discover_suites(suites_dirs: Sequence[Path]) -> list[Path]:
    """Return suite directories (containing suite.yaml), deepest-first stable.

    Every directory that has a suite.yaml counts; nested suites are
    independent. A suites root that does not exist is an error — silent
    empty discovery would let a run start with zero suites and look
    successful.
    """
    found: list[Path] = []
    for root in suites_dirs:
        root = Path(root)
        if not root.is_dir():
            raise SuiteError(f"suites directory does not exist: {root}")
        for candidate in sorted(root.rglob("suite.yaml")):
            found.append(candidate.parent)
    return found


def _check_no_harbor_overlap(data: dict, path: Path) -> None:
    overlap = sorted(set(data) & HARBOR_OWNED_TOP_KEYS)
    if overlap:
        raise SuiteError(
            f"{path}: suite.yaml restates Harbor-owned facts {overlap}; "
            "write them in the Harbor task/job files instead "
            "(only image.pin/image.rebuild narrowing is allowed)"
        )


def load_suite(path: Path) -> ResolvedSuite:
    path = Path(path)
    suite_yaml = path / "suite.yaml"
    if not suite_yaml.is_file():
        raise SuiteError(f"not a suite directory (missing suite.yaml): {path}")
    data = load_suite_yaml(suite_yaml)
    _check_no_harbor_overlap(data, suite_yaml)
    try:
        overlay = SuiteOverlay.model_validate(data)
    except SuiteError:
        raise
    except Exception as exc:
        raise SuiteError(f"{suite_yaml}: invalid suite overlay: {exc}") from exc
    digest = overlay_digest_of(suite_yaml)
    return ResolvedSuite(
        overlay=overlay,
        suite_dir=path,
        suite_yaml_digest=digest,
    )


def resolve_harbor_inputs(suite: ResolvedSuite) -> HarborInputs:
    """Resolve and verify the Harbor files the overlay points at.

    The overlay references Harbor-native declarations by relative path;
    they must exist and be parseable YAML, and their digests are
    recorded so run identity covers them.
    """
    from hashlib import sha256

    def _digest(p: Path) -> str:
        return sha256(p.read_bytes()).hexdigest()

    def _parse(p: Path) -> dict:
        import yaml

        text = p.read_text(encoding="utf-8")
        data = yaml.safe_load(text)
        if not isinstance(data, dict):
            raise SuiteError(f"Harbor file must be a mapping: {p}")
        return data

    inputs = suite.overlay.harbor
    base = Path(suite.suite_dir)
    dataset_path = (base / inputs.dataset).resolve()
    job_path = (base / inputs.job).resolve()
    root = base.resolve()
    for resolved in (dataset_path, job_path):
        if not str(resolved).startswith(str(root)):
            raise SuiteError(
                f"Harbor reference escapes the suite directory: {resolved}"
            )
        if not resolved.is_file():
            raise SuiteError(f"Harbor file not found: {resolved}")
    dataset = _parse(dataset_path)
    job = _parse(job_path)
    return HarborInputs(
        dataset=inputs.dataset,
        job=inputs.job,
        dataset_digest=_digest(dataset_path),
        job_digest=_digest(job_path),
    )


def overlay_digest(suite_yaml: Path) -> str:
    suite_yaml = Path(suite_yaml)
    if not suite_yaml.is_file():
        raise SuiteError(f"suite.yaml not found: {suite_yaml}")
    return overlay_digest_of(suite_yaml)


def assert_unique_suite_identity(suites: Sequence[ResolvedSuite]) -> None:
    """Same (id, version) with different digests → hard error.

    Two suites with the same id and version but different content are
    the classic self-deception: a score comparison that silently
    compares different tasks. Never pick one — refuse.
    """
    seen: dict[tuple[str, str], tuple[str, ResolvedSuite]] = {}
    for suite in suites:
        key = (suite.id, suite.version)
        if key in seen:
            prev_digest, prev = seen[key]
            if prev_digest != suite.suite_yaml_digest:
                raise SuiteError(
                    f"duplicate suite identity {key!r} with different content:\n"
                    f"  {prev.suite_dir} (digest {prev_digest[:12]})\n"
                    f"  {suite.suite_dir} (digest {suite.suite_yaml_digest[:12]})"
                )
            raise SuiteError(
                f"duplicate suite identity {key!r} at {prev.suite_dir} and "
                f"{suite.suite_dir}"
            )
        seen[key] = (suite.suite_yaml_digest, suite)


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
        f"- Harbor dataset: {o.harbor.dataset}",
        f"- Harbor job: {o.harbor.job}",
        f"- clock: {o.clock.mode}" + (f" epoch={o.clock.epoch}" if o.clock.epoch else ""),
        f"- driver.require: {', '.join(o.driver.require) or '(none)'}",
        f"- provenance: source={o.provenance.source} license={o.provenance.license}"
        f" data_imported={o.provenance.data_imported}",
        "",
        "## Baselines (from suite.yaml overlay)",
    ]
    for b in o.baselines:
        lines.append(f"- {b.id}: probe={b.probe!r} equals={b.equals!r}")
    lines.append("")
    lines.append("## Observables (from suite.yaml overlay)")
    for obs in o.observables:
        lines.append(f"- {obs.name}: {obs.type} <- {obs.source}")
    lines.append("")
    lines.append("## Verdict (from suite.yaml overlay)")
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
        "live in the Harbor task/job files referenced above — this page is "
        "a rendered artifact, not an input."
    )
    return "\n".join(lines)
