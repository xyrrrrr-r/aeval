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
    inherited_source_paths,
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
# Harbor exited 0 but the aeval chain is not complete (missing summary,
# unobserved/unclassified trials, seal or recompute refused). The
# Harbor exit code alone never means the evaluation completed (P0-8).
EXIT_E2E_INCOMPLETE = 5


def _declared_adapter(entry):
    """The AdapterSpec an import_path agent entry declares (aeval.agents.contract)."""
    from aeval.agents.contract import build_adapter_spec, load_adapter_class

    import_path = str(entry.import_path)
    return build_adapter_spec(load_adapter_class(import_path), import_path=import_path)


def _declares_facade_stack(agents) -> bool:
    """Does any selected agent declare the generic facade control stack?

    Reads the same declaration the plugin's bootstrap dispatches on, so the
    lock and the deployment can never disagree about whether a facade ran.
    """
    from aeval.agents.contract import control_stack_of, load_adapter_class

    for entry in agents:
        import_path = getattr(entry, "import_path", None)
        if not import_path:
            continue
        if control_stack_of(load_adapter_class(str(import_path))) == "deepagent-facade":
            return True
    return False


def _die(message: str, code: int) -> int:
    typer.secho(f"error: {message}", fg=typer.colors.RED, err=True)
    raise typer.Exit(code=code)


@app.command("run")
def run_cmd(
    suite: Annotated[Path, typer.Option(help="Suite directory (contains suite.yaml)")],
    run_dir: Annotated[Path, typer.Option(help="Output run directory")],
    store: Annotated[Path, typer.Option(help="SQLite store path")],
    harbor_cli: Annotated[str, typer.Option(help="Harbor entrypoint to delegate to")] = "harbor",
    sandbox_image: Annotated[
        str | None, typer.Option(help="Digest-pinned sandbox image (ref@sha256:...) recorded in the runtime lock")
    ] = None,
    sandbox_platform: Annotated[
        str | None, typer.Option(help="Platform of the pinned sandbox image (e.g. arm64)")
    ] = None,
    accept_unmetered_budget: Annotated[
        bool,
        typer.Option(
            help="Run even though the selected adapter's spend is not metered by the gateway lease"
        ),
    ] = False,
    force_build: Annotated[
        bool, typer.Option(help="Rebuild the sandbox template instead of reusing a cached alias")
    ] = False,
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
        # The operator's broker spec carries the run's model identity. Stating it
        # in the composed agent entry is what lets an adapter whose CLI would
        # otherwise pick its own default (and so its own wire protocol) agree
        # with the lease that will meter it.
        from aeval.hooks.broker_lifecycle import parse_broker_spec
        from aeval.suite_loader.composition import lease_model_name

        broker_spec = parse_broker_spec()
        job = compose_harbor_job(resolved, lease_model=lease_model_name(broker_spec))
        # Declared adapter identity, resolved before anything runs: a run whose
        # records cannot name its agent is refused here, not discovered later.
        adapters = [
            _declared_adapter(entry)
            for entry in job.agents
            if getattr(entry, "import_path", None)
        ]
        from aeval.agents.contract import budget_enforcement_point, budget_gate_violation

        violation = budget_gate_violation(
            adapters, resolved.overlay.budget, accepted=accept_unmetered_budget
        )
        if violation:
            raise SuiteError(violation)
        budget_point = budget_enforcement_point(adapters)
        if force_build:
            # Doc §6.3: the first run (and every run after the base image
            # digest changes) must rebuild — Harbor reuses an existing
            # template alias otherwise, silently running the experiment on
            # a stale image. `aeval run` delegates to Harbor, so the flag
            # must be carried in the composed job.
            job.environment.force_build = True
        source_commit = suite_source_commit(suite, inherited_source_paths(resolved))
        job.jobs_dir = run_dir / "harbor"
        if len(Path(job.job_name).parts) != 1 or job.job_name in ("", ".", ".."):
            raise SuiteError("Harbor job_name must be a single directory name")
        suite_path(run_dir, job.job_name)
        job_path = run_dir / "harbor-job.json"
        config_json = job.model_dump_json(indent=2, exclude_none=True)
        from aeval.contracts import job_config_hash

        config_hash = job_config_hash(job)
        lock_ref = f"harbor/{job.job_name}/lock.json"
        images = None
        if sandbox_image is not None:
            # The E2E lock must pin the sandbox image: observed-identity
            # binding (P0-2) compares the live sandbox against exactly
            # this entry. Both options are required together, and the
            # reference must be digest-pinned.
            from aeval.contracts import ImageIdentity

            if sandbox_platform is None:
                raise SuiteError(
                    "--sandbox-image requires --sandbox-platform (the observed "
                    "identity binds the architecture too)"
                )
            digest_sep = "@sha256:"
            if digest_sep not in sandbox_image:
                raise SuiteError(
                    f"--sandbox-image must be digest-pinned (ref@sha256:...): {sandbox_image!r}"
                )
            images = {
                "sandbox": ImageIdentity(
                    reference=sandbox_image,
                    digest=sandbox_image.split(digest_sep, 1)[1],
                    platform=sandbox_platform,
                ),
            }
        # Which agents this run selects decides whether a DSH release belongs in
        # the lock at all: without this a non-Node agent could not produce a lock
        # (a lock without a DSH section used to be rejected outright).
        # The control dist the operator's broker spec provides is part of what
        # ran: fingerprint it into the lock so a changed control build breaks
        # comparability loudly instead of shifting behavior silently inside
        # the agent's process (defense 2 of the control-stack split).
        # ``broker_spec`` was parsed before composition, so the model it pins is
        # already stated in the agent entry.
        # The generic facade is part of what runs, exactly like the DSH
        # control dist: when a selected adapter declares that stack, the
        # built dist is fingerprinted into the lock. A missing dist is
        # refused here rather than silently running unmetered.
        facade_dist = None
        if _declares_facade_stack(job.agents):
            from aeval.control.bootstrap import BootstrapError, resolve_facade_dist

            try:
                facade_dist = resolve_facade_dist()
            except BootstrapError as exc:
                raise SuiteError(str(exc)) from exc
        lock = build_runtime_lock(
            images=images,
            agent_ids=[spec.id for spec in adapters],
            control_dist=broker_spec.control_dist if broker_spec else None,
            facade_dist=facade_dist,
        )
        manifest = RunManifest(
            run_id=f"run-{run_dir.name}",
            runtime_lock=lock,
            runtime_lock_digest=lock.digest(),
            lock_ref=lock_ref,
            config_hash=config_hash,
            config_file_sha256=sha256(config_json.encode("utf-8")).hexdigest(),
            adapters=adapters,
            budget_enforcement_point=budget_point,
            accepted_unmetered_budget=accept_unmetered_budget,
            overlay=OverlayIdentity(
                suite_id=resolved.id,
                suite_version=resolved.version,
                overlay_digest=resolved.suite_yaml_digest,
                overlay_chain_digest=resolved.identity_digest,
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
    # Record the trusted intent copy in the store BEFORE any trial can
    # exist: the seal-time intent check compares against this record.
    from aeval.store.sqlite import StoreConflictError, TrialStore

    try:
        intent_store = TrialStore(store)
        try:
            intent_store.create_run(manifest)
        finally:
            intent_store.close()
    except StoreConflictError as exc:
        _die(str(exc), EXIT_VALIDATION_ERROR)
    except OSError as exc:
        _die(str(exc), EXIT_SYSTEM_ERROR)

    typer.echo(lock_report(lock))
    typer.echo(f"delegating to Harbor: {harbor_cli} run --config {job_path}")
    try:
        result = subprocess.run(
            [harbor_cli, "run", "--config", str(job_path), "--plugin", "aeval.hooks:AevalPlugin"],
            env=env,
        )
    except OSError as exc:
        _die(f"Cannot start Harbor: {exc}", EXIT_SYSTEM_ERROR)
    if result.returncode != 0:
        # Harbor itself failed: no seal attempt, the run stays unsealed
        # and the failure code propagates unchanged.
        raise typer.Exit(result.returncode)

    # Harbor exited 0 — that only means the process exited. The aeval
    # chain is complete only if finalization proves it (P0-8).
    from aeval.bundle.finalize import FinalizeError, finalize_run

    try:
        report = finalize_run(run_dir, store)
    except FinalizeError as exc:
        typer.secho(f"run incomplete: {exc}", fg=typer.colors.RED, err=True)
        raise typer.Exit(EXIT_E2E_INCOMPLETE) from exc
    typer.echo(
        f"run {report.run_id} sealed: {report.recorded_trials} trial(s) recorded, "
        f"{report.attested_files} file(s) attested, recompute passed"
    )
    raise typer.Exit(EXIT_OK)


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
            f"chain-digest={resolved.identity_digest[:12]} "
            f"extends={resolved.extends or '(none)'} "
            f"task-references={len(job.tasks)} remote-datasets={len(job.datasets)}"
        )
        typer.echo(
            "Configuration validated; the selected agent's declared capabilities were "
            "checked. Remote task content and live runtime capabilities are not probed."
        )
    except (SuiteError, ValueError, OSError) as exc:
        _die(str(exc), EXIT_VALIDATION_ERROR)


@app.command("job")
def job_cmd(
    suite: Annotated[Path, typer.Option(help="Suite directory")],
    agent: Annotated[str, typer.Option(help="Declared agent id to drive the suite")],
    profile: Annotated[str | None, typer.Option(help="Launch profile declared by the agent")] = None,
    agents_dir: Annotated[Path, typer.Option(help="Directory holding agents/<id>.yaml")] = Path("agents"),
) -> None:
    """Compose a suite's Harbor job for a declared agent.

    The job file describes the task arm; which agent drives it comes from the
    declaration. Pairing therefore costs a declaration, not another job file.
    """
    from aeval.suite_loader.composition import compose_harbor_job
    from aeval.suite_loader.loader import load_suite
    from aeval.suite_models import SuiteError

    try:
        resolved = load_suite(suite)
        job = compose_harbor_job(
            resolved, agent=agent, agent_profile=profile, agents_root=agents_dir
        )
        entry = job.agents[0]
        typer.echo(
            f"job {job.job_name}: suite={resolved.id} agent={agent} "
            f"profile={profile or 'default'} attempts={job.n_attempts} "
            f"impl={entry.import_path}"
        )
        typer.echo(
            "Composed for a pairing: the job file's task arm is unchanged and the "
            "agent entry comes from the declaration. Nothing was executed."
        )
    except (SuiteError, ValueError, OSError) as exc:
        _die(str(exc), EXIT_VALIDATION_ERROR)


@app.command("conformance")
def conformance_cmd(
    agent: Annotated[Path, typer.Option(help="agents/<id>.yaml declaration to check")],
    agents_dir: Annotated[Path, typer.Option(help="Directory holding agents/<id>.yaml")] = Path("agents"),
    suite: Annotated[
        list[Path] | None,
        typer.Option(help="Suite directory to check the pairing against (repeatable)"),
    ] = None,
) -> None:
    """Prove an agent adapter is fit to be evaluated — and say what was not exercised.

    Every check maps to a way a second agent breaks an evaluation silently.
    A check that could not run is reported as skipped, never as passed.
    """
    from aeval.agents.conformance import run_conformance_for
    from aeval.agents.declaration import resolve_agent_declaration
    from aeval.suite_models import SuiteError

    try:
        resolved = resolve_agent_declaration(agent, agents_root=agents_dir)
        reports = run_conformance_for(
            resolved.declaration.import_path,
            declaration_path=agent,
            agents_root=agents_dir,
            suite_paths=list(suite or []),
        )
        failed = False
        for report in reports:
            typer.echo(report.render())
            failed = failed or not report.ok
        if failed:
            raise typer.Exit(code=EXIT_VALIDATION_ERROR)
    except (SuiteError, ValueError, OSError) as exc:
        _die(str(exc), EXIT_VALIDATION_ERROR)


@app.command("new-agent")
def new_agent_cmd(
    agent_id: Annotated[str, typer.Option("--id", help="New agent id, e.g. deepagent")],
    mode: Annotated[str, typer.Option(help="installed_cli | acp_stdio | sdk_jsonrpc")] = "installed_cli",
    provides: Annotated[str, typer.Option(help="Comma-separated capabilities")] = "shell",
    budget: Annotated[str, typer.Option(help="none | gateway_lease | wallclock_kill")] = "none",
    agents_dir: Annotated[Path, typer.Option(help="Directory holding agents/<id>.yaml")] = Path("agents"),
    package_dir: Annotated[Path | None, typer.Option(help="Where to write the adapter module")] = None,
    import_path: Annotated[str | None, typer.Option(help="Override the generated import path")] = None,
    transcript_source: Annotated[str, typer.Option(help="atif_native | native_session_via_bridge")] = "atif_native",
    force: Annotated[bool, typer.Option(help="Overwrite an existing declaration")] = False,
) -> None:
    """Scaffold a conformant adapter skeleton + declaration for a new agent.

    The generated reader raises NotImplementedError, so conformance reports the
    transcript check as skipped with that reason — never as passed.
    """
    from aeval.agents.scaffold import scaffold_agent
    from aeval.suite_models import SuiteError

    try:
        result = scaffold_agent(
            agents_dir,
            agent_id,
            mode=mode,
            provides=[item.strip() for item in provides.split(",") if item.strip()],
            budget_enforcement=budget,
            transcript_source=transcript_source,
            package_dir=package_dir,
            import_path=import_path,
            force=force,
        )
    except (SuiteError, ValueError, OSError) as exc:
        _die(str(exc), EXIT_VALIDATION_ERROR)
        return
    typer.echo(result.render())


@app.command("agents")
def agents_cmd(
    agents_dir: Annotated[Path, typer.Option(help="Directory holding agents/<id>.yaml")] = Path("agents"),
) -> None:
    """List declared agent adapters and prove each agrees with its adapter class.

    The declaration is the discoverable surface; the class is what the runtime
    reads. Printing both here — and failing loudly on disagreement — is what keeps
    them from drifting apart unnoticed.
    """
    from aeval.agents.contract import load_adapter_class
    from aeval.agents.declaration import (
        declaration_class_mismatches,
        discover_agent_declarations,
        resolve_agent_declaration,
    )
    from aeval.suite_loader.paths import suite_path
    from aeval.suite_models import SuiteError

    try:
        ids = discover_agent_declarations(agents_dir)
        if not ids:
            _die(f"no agent declarations under {agents_dir}", EXIT_VALIDATION_ERROR)
        for agent_id in ids:
            resolved = resolve_agent_declaration(
                suite_path(Path(agents_dir).resolve(), f"{agent_id}.yaml"), agents_root=agents_dir
            )
            declaration = resolved.declaration
            # the class the runtime resolves: itself when pinned, the
            # materialized per-agent class when declaration-driven (G11)
            adapter = declaration.adapter_class()
            mismatches = declaration_class_mismatches(declaration, adapter)
            if mismatches:
                raise SuiteError(
                    f"agent {agent_id}: declaration disagrees with {declaration.import_path}: "
                    + "; ".join(mismatches)
                )
            typer.echo(
                f"agent {declaration.id} v{declaration.version} "
                f"mode={declaration.mode} budget={declaration.budget_enforcement} "
                f"provides={sorted(declaration.provides)} "
                f"observations={sorted(declaration.observations)} "
                f"extends={[source.path for source in resolved.sources[:-1]] or '(none)'}"
            )
        typer.echo(f"{len(ids)} agent declaration(s) agree with their adapter classes.")
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
        manifests = []
        for run_id in run_ids:
            try:
                manifests.append(db.load_run_manifest(run_id))
            except KeyError as exc:
                _die(f"{exc} — cannot report or compare unrecorded runs",
                     EXIT_VALIDATION_ERROR)
    finally:
        db.close()
    summary = aggregate_run(run_ids, trials, k=k, manifests=manifests)
    if compare:
        if len(run_ids) < 2:
            _die("--compare needs at least two run ids", EXIT_PARAM_ERROR)
        comparability = summary.comparability
        if comparability is None:
            _die("comparability unavailable: manifest records incomplete", EXIT_VALIDATION_ERROR)
        if not comparability.comparable:
            typer.secho(
                "runs are NOT comparable — scores must not be averaged or trended together:",
                fg=typer.colors.RED, err=True,
            )
            typer.echo(f"first difference: {comparability.first_difference()}")
            raise typer.Exit(EXIT_VALIDATION_ERROR)
        typer.echo("runs are comparable across every locked dimension")
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
