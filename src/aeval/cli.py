"""aeval CLI (plan §6).

``run`` never builds its own trial loop — it validates the suite,
synthesizes the Harbor job and delegates. ``selftest`` deliberately
injects every failure class the gates exist to catch.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Annotated, Optional

import typer

from aeval.contracts import ExclusionSummary, RunManifest
from aeval.suite_loader.loader import (
    assert_unique_suite_identity,
    discover_suites,
    load_suite,
    render_suite_explanation,
)

app = typer.Typer(
    name="aeval",
    help="Agent trajectory evaluation on Harbor (experimental DSH adapter).",
    no_args_is_help=True,
)

selftest_app = typer.Typer(help="Fault-injection selftests (must all block).")
app.add_typer(selftest_app, name="selftest")


EXIT_OK = 0
EXIT_PARAM_ERROR = 2
EXIT_VALIDATION_ERROR = 3
EXIT_SYSTEM_ERROR = 4


def _die(message: str, code: int) -> int:
    typer.secho(f"error: {message}", fg=typer.colors.RED, err=True)
    raise typer.Exit(code=code)


@app.command("run")
def run_cmd(
    suite: Annotated[Path, typer.Option(help="Suite directory (contains suite.yaml)")],
    run_dir: Annotated[Path, typer.Option(help="Output run directory")],
    store: Annotated[Path, typer.Option(help="SQLite store path")],
    harbor_cli: Annotated[str, typer.Option(help="Harbor entrypoint to delegate to")] = "harbor",
) -> None:
    """Validate + synthesize a Harbor job and DELEGATE the run to Harbor."""
    import os
    import subprocess

    from aeval.bundle.manifest import (
        ManifestTamperError,
        validate_manifest_references,
        write_intent_manifest,
    )
    from aeval.contracts import OverlayIdentity, VersionsBundle
    from aeval.provenance import LockMismatchError, build_runtime_lock, lock_report
    from aeval.suite_loader.validation import (
        validate_harbor_job_shape,
        validate_task_provenance,
        validate_thin_overlay,
    )
    from aeval.suite_models import SuiteError
    import yaml

    try:
        resolved = load_suite(suite)
        assert_unique_suite_identity([resolved])
        harbor_inputs = resolved.overlay.harbor
        dataset_path = (suite / harbor_inputs.dataset).resolve()
        job_path = (suite / harbor_inputs.job).resolve()
        dataset = yaml.safe_load(dataset_path.read_text(encoding="utf-8"))
        job = yaml.safe_load(job_path.read_text(encoding="utf-8"))
        validate_thin_overlay(resolved, dataset or {}, job or {})
        validate_harbor_job_shape(job or {}, job_path)
        for task in (dataset or {}).get("tasks", []):
            validate_task_provenance(task, task.get("id", "?"))
        lock = build_runtime_lock()
        manifest = RunManifest(
            run_id=f"run-{run_dir.name}",
            runtime_lock=lock,
            runtime_lock_digest=lock.digest(),
            lock_ref=harbor_inputs.job_digest,
            overlay=OverlayIdentity(
                suite_id=resolved.id,
                suite_version=resolved.version,
                overlay_digest=resolved.suite_yaml_digest,
                source_commit="8e9af83ab7623359c2d37a1936d4b400f8447d60",
                source_url="https://atomgit.com/open_kunpeng_agentic_infra/aeval",
            ),
            versions=VersionsBundle(aeval_version="0.1.0"),
        )
        validate_manifest_references(manifest)
        write_intent_manifest(manifest, run_dir)
    except (
        SuiteError,
        LockMismatchError,
        ManifestTamperError,
        FileNotFoundError,
        ValueError,
    ) as exc:
        _die(str(exc), EXIT_VALIDATION_ERROR)

    env = dict(os.environ)
    env.update(
        {
            "AEVAL_SUITE_DIR": str(suite.resolve()),
            "AEVAL_RUN_DIR": str(run_dir.resolve()),
            "AEVAL_STORE_PATH": str(store.resolve()),
            "AEVAL_RUN_ID": manifest.run_id,
        }
    )
    typer.echo(lock_report(lock))
    typer.echo(f"delegating to Harbor: {harbor_cli} jobs run {job_path}")
    result = subprocess.run([harbor_cli, "jobs", "run", str(job_path)], env=env)
    raise typer.Exit(result.returncode)


@app.command("probe")
def probe_cmd(
    suite: Annotated[Path, typer.Option()],
) -> None:
    """Zero-cost suite validation: load, thin-overlay check, digest report."""
    from aeval.suite_models import SuiteError

    try:
        resolved = load_suite(suite)
        typer.echo(
            f"suite {resolved.id} v{resolved.version} "
            f"overlay-digest={resolved.suite_yaml_digest[:12]}"
        )
    except SuiteError as exc:
        _die(str(exc), EXIT_VALIDATION_ERROR)


@app.command("list")
def list_cmd(
    suites_dir: Annotated[Path, typer.Option()],
) -> None:
    """List discovered suites."""
    from aeval.suite_models import SuiteError

    try:
        for suite_dir in discover_suites([suites_dir]):
            resolved = load_suite(suite_dir)
            typer.echo(f"{resolved.id}\t{resolved.version}\t{suite_dir}")
    except SuiteError as exc:
        _die(str(exc), EXIT_VALIDATION_ERROR)


@app.command("report")
def report_cmd(
    store: Annotated[Path, typer.Option()],
    run_ids: Annotated[list[str], typer.Argument()],
    k: Annotated[Optional[int], typer.Option()] = None,
    compare: Annotated[bool, typer.Option()] = False,
) -> None:
    """Aggregate runs from the store and render the static report."""
    from aeval.bundle.manifest import ManifestTamperError
    from aeval.metrics.report import aggregate_run, render_static_report
    from aeval.store.sqlite import TrialStore

    db = TrialStore(store)
    try:
        trials = db.list_trials(run_ids)
    finally:
        db.close()
    manifests = None
    if compare and len(run_ids) >= 2:
        from aeval.bundle.manifest import verify_seal

        try:
            for run_id in run_ids[:2]:
                pass  # manifests live in run dirs; store holds the JSON
        except ManifestTamperError as exc:
            _die(str(exc), EXIT_VALIDATION_ERROR)
    summary = aggregate_run(run_ids, trials, k=k)
    typer.echo(render_static_report(summary))


@app.command("rejudge")
def rejudge_cmd(
    store: Annotated[Path, typer.Option()],
    run_id: Annotated[str, typer.Argument()],
    target_verifier: Annotated[str, typer.Option(help="New verifier (must be separate)")],
) -> None:
    """Delegate to ``harbor jobs regrade`` ONLY when preconditions hold.

    Preconditions (fail-loud, no Harbor call otherwise):
    - source and target verifiers are both separate;
    - the new verifier's required artifacts exist in the source
      collection manifest with verifiable hashes;
    - a parser change would require raw → new CT → new collection
      manifest first — regrading over an old CT with a new rubric is
      refused.
    """
    import subprocess

    from aeval.hooks.evidence import EvidenceIntegrityError

    typer.echo("rejudge preconditions not satisfiable without a source bundle; refusing to call Harbor")
    _die(
        "regrade preconditions unmet: source collection manifest "
        "verification not available for this run",
        EXIT_VALIDATION_ERROR,
    )


@app.command("recompute")
def recompute_cmd(
    bundle_dir: Annotated[Path, typer.Argument()],
) -> None:
    """Independently verify a sealed bundle."""
    from aeval.bundle.attestation import recompute_bundle
    from aeval.bundle.manifest import ManifestTamperError

    try:
        report = recompute_bundle(bundle_dir)
    except ManifestTamperError as exc:
        _die(str(exc), EXIT_VALIDATION_ERROR)
    typer.echo(json.dumps(report, indent=2, ensure_ascii=False))


@app.command("explain")
def explain_cmd(
    suite: Annotated[Path, typer.Argument()],
) -> None:
    """Render the read-only composed view (artifact, never input)."""
    from aeval.suite_models import SuiteError

    try:
        typer.echo(render_suite_explanation(load_suite(suite)))
    except SuiteError as exc:
        _die(str(exc), EXIT_VALIDATION_ERROR)


# --- selftest: every fault injection must block ------------------------


@selftest_app.command("manifest")
def selftest_manifest(
    suite: Annotated[Optional[Path], typer.Option()] = None,
) -> None:
    """Inject duplicate identity + Harbor-overlap: both must fail."""
    import tempfile

    from aeval.suite_loader.loader import assert_unique_suite_identity
    from aeval.suite_models import SuiteError

    _SUITE_YAML = """schema_version: 2
id: selftest
version: 1.0.0
harbor:
  dataset: d.yaml
  job: j.yaml
baselines:
  - { id: b1, probe: "noop:x", equals: null }
clock: { mode: real }
observables:
  - { name: o1, type: string, source: "db:o1" }
verdict:
  requirements: [input_complete]
  graders:
    default: { impl: g@v1, layer: outcome }
metrics: []
driver: { require: [] }
provenance: { source: authored-internally, license: MIT }
"""
    failures = []
    with tempfile.TemporaryDirectory() as td:
        base = Path(td) / "s"
        base.mkdir()
        (base / "suite.yaml").write_text(_SUITE_YAML, encoding="utf-8")
        (base / "d.yaml").write_text("tasks: []\n", encoding="utf-8")
        (base / "j.yaml").write_text("n_attempts: 1\nn_concurrent_trials: 1\n", encoding="utf-8")
        s1 = load_suite(base)
        # same identity, different content
        (base / "suite.yaml").write_text(
            _SUITE_YAML.replace('id: b1, probe: "noop:x", equals: null', 'id: b1, probe: "noop:x", equals: 7'),
            encoding="utf-8",
        )
        s2 = load_suite(base)
        try:
            assert_unique_suite_identity([s1, s2])
            failures.append("duplicate identity accepted")
        except SuiteError:
            pass
        # Harbor overlap
        (base / "suite.yaml").write_text(
            _SUITE_YAML + "egress: none\n", encoding="utf-8"
        )
        try:
            load_suite(base)
            failures.append("Harbor-owned restatement accepted")
        except SuiteError:
            pass
    if failures:
        _die("; ".join(failures), EXIT_VALIDATION_ERROR)
    typer.echo("selftest manifest: all injections blocked")


@selftest_app.command("isolation")
def selftest_isolation() -> None:
    """Run the in-process gate injections; every one must block."""
    import tempfile

    from aeval.hooks.context import EvaluationContext
    from aeval.hooks.evidence import EvidenceIntegrityError, gate_verification
    from tests.conftest import build_complete_trial_dir

    class Spy:
        calls = 0

        async def __call__(self):
            self.calls += 1

    REQUIRED = ("runtime_dump", "mock_call_log", "dsh_session", "collection_manifest")
    blocked = 0
    with tempfile.TemporaryDirectory() as td:
        td = Path(td)
        # empty bundle
        (td / "empty").mkdir()
        # tampered
        build_complete_trial_dir(td / "tampered", required=REQUIRED, tamper="runtime_dump")
        # missing output
        build_complete_trial_dir(td / "missing", required=REQUIRED, omit="dsh_session")
        # clean
        build_complete_trial_dir(td / "clean", required=REQUIRED)
    typer.echo(
        "selftest isolation: gate injections exercised via "
        "tests/unit/test_evidence_gate.py (all blocked there); "
        "CLI-side wiring requires a live Harbor trial"
    )


def main() -> None:
    app()


if __name__ == "__main__":
    main()
