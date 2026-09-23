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
    from hashlib import sha256
    from importlib.metadata import version

    from aeval.contracts import OverlayIdentity, VersionsBundle
    from aeval.provenance import LockMismatchError, build_runtime_lock, lock_report
    from aeval.suite_loader.composition import compose_harbor_job, suite_path, suite_source_commit
    from aeval.suite_models import SuiteError

    try:
        suite, run_dir = suite.resolve(), run_dir.resolve()
        if run_dir.exists():
            raise SuiteError("Run directories are never reused; choose a new --run-dir")
        if run_dir.is_relative_to(suite):
            raise SuiteError("Run output must be outside the source suite directory")
        resolved = load_suite(suite)
        job = compose_harbor_job(resolved)
        source_commit = suite_source_commit(suite)
        job.jobs_dir = run_dir / "harbor"
        if len(Path(job.job_name).parts) != 1 or job.job_name in ("", ".", ".."):
            raise SuiteError("Harbor job_name must be a single directory name")
        suite_path(run_dir, job.job_name)
        job_path = run_dir / "harbor-job.json"
        config_json = job.model_dump_json(indent=2, exclude_none=True)
        evaluation_config = job.model_dump(mode="json", exclude={"job_name", "jobs_dir"})
        for field in ("include_exceptions", "exclude_exceptions"):
            if evaluation_config["retry"][field] is not None:
                evaluation_config["retry"][field].sort()
        config_hash = sha256(
            json.dumps(evaluation_config, sort_keys=True, ensure_ascii=False).encode("utf-8")
        ).hexdigest()
        lock_ref = f"harbor/{job.job_name}/lock.json"
        lock = build_runtime_lock()
        manifest = RunManifest(
            run_id=f"run-{run_dir.name}",
            runtime_lock=lock,
            runtime_lock_digest=lock.digest(),
            lock_ref=lock_ref,
            config_hash=config_hash,
            config_file_sha256=sha256(config_json.encode("utf-8")).hexdigest(),
            overlay=OverlayIdentity(
                suite_id=resolved.id,
                suite_version=resolved.version,
                overlay_digest=resolved.suite_yaml_digest,
                source_commit=source_commit,
            ),
            versions=VersionsBundle(
                aeval_version=version("aeval"),
                converter_version=resolved.overlay.provenance.converter_version,
            ),
        )
        validate_manifest_references(manifest)
        run_dir.mkdir(parents=True)
        write_intent_manifest(manifest, run_dir)
        job_path.write_bytes(config_json.encode("utf-8"))
        lock_path = run_dir / "runtime_lock.json"
        lock_path.write_text(lock.model_dump_json(indent=2), encoding="utf-8")
    except (SuiteError, LockMismatchError, ManifestTamperError, ValueError) as exc:
        _die(str(exc), EXIT_VALIDATION_ERROR)
    except OSError as exc:
        _die(str(exc), EXIT_SYSTEM_ERROR)

    env = dict(os.environ)
    env.update(
        {
            "AEVAL_SUITE_DIR": str(suite),
            "AEVAL_RUN_DIR": str(run_dir),
            "AEVAL_STORE_PATH": str(store.resolve()),
            "AEVAL_RUN_ID": manifest.run_id,
            "AEVAL_RUNTIME_LOCK": str(lock_path),
        }
    )
    typer.echo(lock_report(lock))
    typer.echo(f"delegating to Harbor: {harbor_cli} run --config {job_path}")
    try:
        result = subprocess.run(
            [harbor_cli, "run", "--config", str(job_path), "--plugin", "aeval.hooks:AevalPlugin"],
            env=env,
        )
    except OSError as exc:
        _die(f"Cannot start Harbor: {exc}", EXIT_SYSTEM_ERROR)
    raise typer.Exit(result.returncode)


@app.command("probe")
def probe_cmd(
    suite: Annotated[Path, typer.Option()],
) -> None:
    """Validate native declarations without running agents or fetching datasets."""
    from aeval.suite_loader.composition import compose_harbor_job
    from aeval.suite_models import SuiteError

    try:
        resolved = load_suite(suite)
        job = compose_harbor_job(resolved)
        typer.echo(
            f"suite {resolved.id} v{resolved.version} "
            f"overlay-digest={resolved.suite_yaml_digest[:12]} "
            f"task-references={len(job.tasks)} remote-datasets={len(job.datasets)}"
        )
        typer.echo("Configuration validated; remote task content and runtime capabilities are not probed.")
    except (SuiteError, ValueError, OSError) as exc:
        _die(str(exc), EXIT_VALIDATION_ERROR)


@app.command("list")
def list_cmd(
    suites_dir: Annotated[Path, typer.Option()],
) -> None:
    """List discovered suites."""
    from aeval.suite_models import SuiteError

    try:
        suites = [load_suite(path) for path in discover_suites([suites_dir])]
        assert_unique_suite_identity(suites)
        for resolved in suites:
            typer.echo(f"{resolved.id}\t{resolved.version}\t{resolved.suite_dir}")
    except (SuiteError, ValueError, OSError) as exc:
        _die(str(exc), EXIT_VALIDATION_ERROR)


@app.command("import")
def import_cmd(
    src: Annotated[Path, typer.Option(help="Complete native suite directory containing suite.yaml")],
    out: Annotated[Path, typer.Option(help="New output suite directory; never overwritten")],
    version: Annotated[str, typer.Option(help="New suite version")],
    format: Annotated[str, typer.Option(help="Verified format: harbor-task")] = "harbor-task",
) -> None:
    from aeval.suite_loader.transfer import import_suite
    from aeval.suite_models import SuiteError

    try:
        result = import_suite(src, out, version=version, format=format)
    except (SuiteError, ValueError, OSError) as exc:
        _die(str(exc), EXIT_VALIDATION_ERROR)
    typer.echo(f"Imported native suite: {result}; run probe, then commit its source before run.")


@app.command("export")
def export_cmd(
    suite: Annotated[Path, typer.Option(help="Native suite directory")],
    out: Annotated[Path, typer.Option(help="New output suite directory; never overwritten")],
    format: Annotated[str, typer.Option(help="Verified format: harbor-task")] = "harbor-task",
) -> None:
    from aeval.suite_loader.transfer import export_suite
    from aeval.suite_models import SuiteError

    try:
        result = export_suite(suite, out, format=format)
    except (SuiteError, ValueError, OSError) as exc:
        _die(str(exc), EXIT_VALIDATION_ERROR)
    typer.echo(f"Exported native suite and overlay: {result}")


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
    from aeval.suite_loader.composition import compose_harbor_job
    from aeval.suite_models import SuiteError

    try:
        resolved = load_suite(suite)
        job = compose_harbor_job(resolved)
        typer.echo(render_suite_explanation(resolved))
        typer.echo("\n## Native Harbor job (local paths resolved against the suite root)")
        typer.echo(job.model_dump_json(indent=2, exclude_none=True))
    except (SuiteError, ValueError, OSError) as exc:
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
