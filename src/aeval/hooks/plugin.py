"""Harbor job owner; job-end summary failures remain warnings, not a verdict gate."""

from __future__ import annotations

import base64
import json
import os
import shlex
from hashlib import sha256
from pathlib import Path, PurePosixPath
from typing import Any

from aeval.agents.contract import (
    sandbox_session_record,
    session_id_from_record,
    terminal_descriptor_owner,
)
from aeval.bundle.manifest import _atomic_write_json
from aeval.contracts import OverlayIdentity, RunBinding, RunManifest, RuntimeLock, job_config_hash
from aeval.hooks.baseline_arrival import on_environment_started
from aeval.suite_models import ResolvedSuite
from aeval.hooks.collection import CollectionError, collect_trial_evidence
from aeval.control.bootstrap import BootstrapError, bootstrap_trial_control
from aeval.hooks.broker_lifecycle import (
    note_broker_unexpected_exit,
    BrokerSpecError,
    SANDBOX_BUNDLE_PATH,
    parse_broker_spec,
    start_trial_broker,
    stop_trial_broker,
    trial_control_paths,
)
from aeval.hooks.context import EvaluationContext, LifecycleError
from aeval.hooks.environment_access import (
    EnvironmentAccessError,
    TrialEnvironmentRegistry,
    install_trial_capture,
)
from aeval.hooks.evidence import (
    EvidenceIntegrityError, finalize_trial_record, gate_verification,
)
from aeval.provenance import verify_runtime_lock
from aeval.suite_loader.loader import load_suite

__all__ = ["AevalPlugin", "create_run_context", "register_trial_hooks"]


class HookRegistrationError(RuntimeError):
    pass


def suite_identity_matches(manifest_overlay: OverlayIdentity, suite: ResolvedSuite) -> bool:
    """Fail-closed suite identity gate: raw bytes AND (when recorded) the chain.

    Inherited content is part of identity, so a base edited between writing
    the intent manifest and starting the job must fail registration rather
    than run silently. Manifests sealed before inheritance existed carry no
    chain digest; for those the raw-bytes check alone still applies.
    """
    if (
        manifest_overlay.suite_id != suite.id
        or manifest_overlay.suite_version != suite.version
        or manifest_overlay.overlay_digest != suite.suite_yaml_digest
    ):
        return False
    if manifest_overlay.overlay_chain_digest is None:
        return True
    return manifest_overlay.overlay_chain_digest == suite.identity_digest


def create_run_context(job: Any) -> EvaluationContext:
    suite_dir = os.environ.get("AEVAL_SUITE_DIR")
    run_dir = os.environ.get("AEVAL_RUN_DIR")
    store_path = os.environ.get("AEVAL_STORE_PATH")
    lock_json = os.environ.get("AEVAL_RUNTIME_LOCK")
    run_id = os.environ.get("AEVAL_RUN_ID")
    if not all((suite_dir, run_dir, store_path, lock_json, run_id)):
        raise HookRegistrationError("AEVAL_SUITE_DIR/RUN_DIR/STORE_PATH/RUNTIME_LOCK/RUN_ID must be set by aeval")

    root = Path(run_dir).resolve()
    suite = load_suite(Path(suite_dir))
    lock = RuntimeLock.model_validate_json(Path(lock_json).read_bytes())
    verify_runtime_lock(lock)
    raw_manifest = json.loads((root / "run_manifest.json").read_bytes())
    if raw_manifest.get("sealed") is not False:
        raise HookRegistrationError("job requires an unsealed intent manifest")
    manifest = RunManifest.model_validate(raw_manifest)
    binding = RunBinding(
        run_id=manifest.run_id,
        job_config_hash=manifest.config_hash,
        config_file_sha256=manifest.config_file_sha256,
        runtime_lock_digest=manifest.runtime_lock_digest,
    )
    if binding.run_id != run_id:
        raise HookRegistrationError("run id differs from intent manifest")
    if lock.digest() != binding.runtime_lock_digest or manifest.runtime_lock.digest() != lock.digest():
        raise HookRegistrationError("runtime lock differs from intent manifest")
    config_bytes = (root / "harbor-job.json").read_bytes()
    if sha256(config_bytes).hexdigest() != binding.config_file_sha256:
        raise HookRegistrationError("job config file digest differs from intent manifest")
    if job_config_hash(job.config) != binding.job_config_hash:
        raise HookRegistrationError("effective Job config hash differs from intent manifest")
    declared_config = json.loads(config_bytes)
    declared_dir = Path(declared_config["jobs_dir"]) / declared_config["job_name"]
    if Path(job.job_dir).resolve() != declared_dir.resolve():
        raise HookRegistrationError("Job output directory differs from intent config")
    if not suite_identity_matches(manifest.overlay, suite):
        raise HookRegistrationError("suite identity differs from intent manifest")
    _require_adapter_contract(job, suite.overlay.budget, accepted=manifest.accepted_unmetered_budget)
    trials_dir = Path(job.job_dir).resolve()
    if trials_dir == root or not trials_dir.is_relative_to(root):
        raise HookRegistrationError("Harbor job directory must be inside the run directory")
    context = EvaluationContext(
        run_id=run_id, runtime_lock=lock, suite=suite, run_dir=root,
        store_path=Path(store_path), run_binding=binding, job_id=str(job.id),
        trials_dir=trials_dir,
    )
    # P0-4: controlled model routing is opt-in via the operator's broker
    # spec; a BROKEN spec fails registration rather than silently
    # running trials with uncontrolled model access.
    context.broker_spec = parse_broker_spec()
    return context


def register_trial_hooks(job: Any, context: EvaluationContext) -> None:
    async def _trial_started(event: Any) -> None:
        state = context.start_trial(event)
        if context.broker_spec is None:
            return
        try:
            start_trial_broker(context.broker_spec, context, state)
        except Exception as exc:
            # fail-closed: the model phase must not run without the
            # controlled routing the operator asked for
            state.mark_infra_invalid(f"model broker startup failed: {exc}")

    async def _environment_started(event: Any) -> None:
        # Harbor emits ENVIRONMENT_START *before* environment.start(),
        # so the sandbox does not exist yet and no probe can observe
        # anything here. The baseline/policy audit runs at AGENT_START
        # (see _agent_started) where the started handle is available.
        state = context.state_for_event(event)
        if state.terminal:
            return

    async def _agent_started(event: Any) -> None:
        state = context.state_for_event(event)
        if state.terminal:
            return
        # P0-2 audit at the first point where the sandbox is really up:
        # the handle comes from the owner's trial registry, never from
        # the (environment-less) hook event.
        env_handle = None
        if context.environments is not None:
            env_handle = context.environments.environment(state.trial_id)
        try:
            await on_environment_started(event, context, env_handle)
        except Exception as exc:
            state.mark_infra_invalid(f"environment audit failed: {exc}")
        if state.infra_invalid_reasons:
            # P0-2: the model phase started on a tainted trial. The
            # real hard block is the owner refusing to hand out a model
            # token (P0-4); this record makes the violation visible in
            # the run summary no matter what.
            issue = "agent started despite recorded infra failures"
            if issue not in state.evidence_issues:
                state.evidence_issues.append(issue)
            state.mark_infra_invalid(issue)
            return
        # P0-4 owner side: deploy the job token and create the trusted
        # control binding. This is the first point where the sandbox
        # exists (AGENT_START), and it is deliberately skipped for a
        # tainted trial — the owner must not hand out a model token to
        # a trial that already failed its environment audit.
        if context.broker_spec is None or state.broker is None:
            return
        try:
            # One agent handle, used twice: the adapter declares SANDBOX_HOME /
            # SESSION_ARTIFACT_DIR, hooks/broker_lifecycle.py composes the
            # authoritative control config from those paths, and the owner
            # refuses a binding whose paths differ from them. Deriving the paths
            # here WITHOUT the agent silently fell back to the historical DSH
            # defaults, so any adapter declaring different ones (deepagent:
            # /root/.deepagents, deepagent-home) got its binding refused on a
            # real sandbox — a failure the local harness cannot show, because
            # there the same paths are handed to both sides.
            agent_handle = (
                context.environments.agent(state.trial_id)
                if context.environments is not None else None
            )
            binding, config = await bootstrap_trial_control(
                environment=env_handle,
                context=context,
                trial_id=state.trial_id,
                paths=trial_control_paths(
                    state, context.run_dir,
                    getattr(getattr(context.suite, "overlay", None), "driver", None),
                    agent=agent_handle,
                ),
                broker=state.broker,
                provider=str(context.broker_spec.identity.get("provider", "")),
                model=str(context.broker_spec.identity.get("model", "")),
                # in-sandbox control stack (verified deployment, §7.5/7.6)
                agent=agent_handle,
                control_dist=getattr(context.broker_spec, "control_dist", None),
                control_ca=getattr(context.broker_spec, "control_ca", None),
                # The generic facade flavor resolves its own dist (env override,
                # else the sibling deepagents-eval-control build) — the same
                # artifact `aeval run` fingerprints into the lock, and the
                # deployment re-fingerprints it against that lock before
                # uploading, so locked bytes are the bytes that run.
                reasoning_effort=context.broker_spec.identity.get("reasoningEffort"),
                limits=dict(getattr(context.broker_spec, "limits", {}) or {}),
            )
        except BootstrapError as exc:
            state.mark_infra_invalid(f"control bootstrap failed: {exc}")
            return
        state.binding = binding
        state.control_config = config

    async def _agent_ended(event: Any) -> None:
        state = context.state_for_event(event)
        if state.terminal:
            return
        # Terminal descriptor: written here only when the adapter declares the
        # OWNER as the terminal observer (a stack with no sandbox-side witness).
        # example-lab: without it every real run died at the evidence gate with
        # "bundle descriptor missing (host-side control plugin)".
        await _write_terminal_descriptor(context, state)
        if state.infra_invalid_reasons:
            issue = "agent ended with infra failures recorded"
            if issue not in state.evidence_issues:
                state.evidence_issues.append(issue)
        await _stage_task_tests(event, context, state)

    async def _verification_started(event: Any) -> None:
        state = context.state_for_event(event)
        try:
            if state.terminal:
                raise LifecycleError("verification started after trial termination")
            if state.binding is None:
                raise LifecycleError("trial has no trusted control binding")
            # P0-6 real producer: trust first, then collect the fixed
            # evidence outputs from the live trial. Collection still runs
            # for trials that will fail later checks, so failures and
            # cancellations leave locatable evidence behind.
            environments = context.environments
            environment = (
                environments.environment(state.trial_id) if environments else None
            )
            agent = environments.agent(state.trial_id) if environments else None
            if state.trial_dir is not None:
                try:
                    await collect_trial_evidence(
                        trial_dir=state.trial_dir,
                        trial_id=state.trial_id,
                        suite=context.suite,
                        environment=environment,
                        agent=agent,
                        runtime_lock=context.runtime_lock,
                        session_id=state.session_id,
                    )
                except CollectionError as exc:
                    state.evidence_ok = False
                    state.mark_infra_invalid(f"evidence collection failed: {exc}")
                    raise EvidenceIntegrityError(str(exc)) from exc
            await gate_verification(event, context)
            bundle = context.artifacts[state.trial_id]
            if bundle.trial_id != state.trial_id or bundle.bundle_descriptor is None:
                raise LifecycleError("evidence is missing its bound trial descriptor")
            context.verify_descriptor(state.trial_id, bundle.bundle_descriptor)
        except (LifecycleError, EvidenceIntegrityError) as exc:
            state.evidence_ok = False
            state.mark_infra_invalid(str(exc))
            # D52: block the verifier (score validity is unchanged) but
            # still leave an explicit, reasoned exclusion record — a trial
            # with no record at all makes the whole run unsealable.
            await _record_unjudgeable_exclusion(event, context, state, str(exc))
            raise EvidenceIntegrityError(str(exc)) from exc

    async def _finish(event: Any, *, cancelled: bool = False) -> None:
        state = context.state_for_event(event)
        exception = event.result.exception_info
        cancelled = cancelled or (exception is not None and exception.exception_type == "CancelledError")
        # Record a broker that died on its own BEFORE stopping it: that
        # closes the lease and makes every later model call fail with
        # AEVAL_LEASE_CLOSED, which is otherwise inexplicable from the
        # trial log (found on the real chain).
        unexpected = note_broker_unexpected_exit(state)
        if unexpected is not None:
            state.mark_infra_invalid(unexpected)
        stop_trial_broker(state)
        if state.finish(exception, cancelled=cancelled):
            await finalize_trial_record(event, context)
            await _grade_and_record(event, context, state)
            # D52: an observed trial must never vanish from the store — the
            # sealer refuses to seal a run holding a record-less trial and
            # every other trial's evidence is lost with it. No-op when
            # grading already recorded the trial.
            reason = "; ".join(
                state.infra_invalid_reasons or state.evidence_issues
            ) or "trial ended without a verified evidence bundle"
            await _record_unjudgeable_exclusion(event, context, state, reason)
        if context.environments is not None:
            context.environments.forget(state.trial_id)

    async def _trial_ended(event: Any) -> None:
        await _finish(event)

    async def _trial_cancelled(event: Any) -> None:
        await _finish(event, cancelled=True)

    try:
        job.on_trial_started(_trial_started)
        job.on_environment_started(_environment_started)
        job.on_agent_started(_agent_started)
        job.on_agent_ended(_agent_ended)
        job.on_verification_started(_verification_started)
        job.on_trial_ended(_trial_ended)
        job.on_trial_cancelled(_trial_cancelled)
    except Exception as exc:
        raise HookRegistrationError(f"failed to register trial hooks on the job: {exc}") from exc


# Where the task's own verifier expects its tests inside the sandbox
# (Harbor's ``EnvironmentPaths.tests_dir`` on Linux, and where the
# Terminal-Bench test scripts look for themselves).
TEST_STAGE_DIR = "/tests"


async def _observed_session_identity(
    exec_fn: Any, adapter: type, record_path: str
) -> tuple[str | None, bool]:
    """Read the adapter's own record from the live sandbox before it downloads.

    Returns ``(observed_session_id, recorded)``. When the adapter's recorder is
    a foreign runtime that mints its own session id (the ACP runner), the record
    is the only place that identity exists — reading it here is what lets the
    owner write a descriptor the adapter itself can later locate and verify.
    """
    read = await exec_fn(f"cat {shlex.quote(record_path)}")
    code = getattr(read, "return_code", getattr(read, "exit_code", 1))
    if code != 0:
        return None, False
    stdout = getattr(read, "stdout", "") or ""
    payload = stdout if isinstance(stdout, bytes) else stdout.encode("utf-8", "replace")
    return session_id_from_record(adapter, payload), True


async def _write_terminal_descriptor(context: EvaluationContext, state: Any) -> None:
    """Write the bundle descriptor when the OWNER is the terminal observer.

    WHICH component can state a trial's terminal outcome is the adapter's
    declaration (``aeval.agents.contract.TERMINAL_DESCRIPTOR_OWNER``), not a
    flavor branch here: a stack that owns the session states it from inside the
    sandbox (DSH writes its own descriptor, and overwriting that from the host
    would replace a first-hand statement with a second-hand one); a stack that
    only proxies model traffic never observes an exit, so the owner observes
    what it can and states it. The timing is forced by Harbor: AGENT_END is
    emitted and only THEN is the agent log directory downloaded, so this hook is
    the last moment before the evidence gate looks for the descriptor.
    """
    if state.binding is None:
        return
    environments = context.environments
    handle = environments.agent(state.trial_id) if environments is not None else None
    if handle is None:
        return
    adapter = handle if isinstance(handle, type) else type(handle)
    try:
        if terminal_descriptor_owner(adapter) != "host":
            return
        record_path = sandbox_session_record(adapter)
    except Exception as exc:  # noqa: BLE001 - a bad declaration cannot be skipped
        state.mark_infra_invalid(f"bundle descriptor: adapter declaration invalid: {exc}")
        return
    if record_path is None:
        state.mark_infra_invalid(
            "bundle descriptor: the adapter appoints the host as the terminal "
            "observer but declares no sandbox session record to observe"
        )
        return
    environment = (
        environments.environment(state.trial_id) if environments is not None else None
    )
    exec_fn = getattr(environment, "exec", None)
    if not callable(exec_fn):
        state.mark_infra_invalid(
            "bundle descriptor: the sandbox exposes no exec — the descriptor the "
            "evidence gate requires cannot be written"
        )
        return
    try:
        observed, recorded = await _observed_session_identity(
            exec_fn, adapter, record_path
        )
        if not recorded:
            reason = "crashed"
        elif observed is None:
            state.mark_infra_invalid(
                f"bundle descriptor: the adapter's record at {record_path} "
                "carries no session id, so this trial's session cannot be stated"
            )
            reason = "infra_error"
        else:
            # The owner watched this trial's own record complete: the only
            # completion claim it can defend. Nothing observed → no claim;
            # infra failures suppress the judge (infra_error), as everywhere.
            state.observed_agent_session_id = observed
            reason = "agent_exit_0"
        if state.infra_invalid_reasons:
            reason = "infra_error"
        descriptor = {
            "schema_version": 2,
            "run": state.binding.run.model_dump(mode="json"),
            "trial_id": state.binding.trial_id,
            "session_id": observed or state.binding.session_id,
            "session_root": state.binding.paths.session_root,
            "stop_reason": reason,
            "config_digest": state.binding.config_digest,
        }
        payload = json.dumps(descriptor, indent=2, sort_keys=True) + "\n"
        encoded = base64.b64encode(payload.encode("utf-8")).decode("ascii")
        target = SANDBOX_BUNDLE_PATH
        command = (
            f"mkdir -p {shlex.quote(str(PurePosixPath(target).parent))} && "
            f"printf %s {shlex.quote(encoded)} | base64 -d > {shlex.quote(target)}.tmp && "
            f"mv {shlex.quote(target)}.tmp {shlex.quote(target)}"
        )
        written = await exec_fn(command)
    except Exception as exc:  # noqa: BLE001 - fail closed, never break the hook chain
        state.mark_infra_invalid(f"bundle descriptor could not be written: {exc}")
        return
    code = getattr(written, "return_code", getattr(written, "exit_code", 0))
    if code not in (0, None):
        detail = str(
            getattr(written, "stderr", "") or getattr(written, "stdout", "") or ""
        ).strip()
        state.mark_infra_invalid(
            f"bundle descriptor could not be written to {target} (exit {code})"
            + (f": {detail[:200]}" if detail else "")
        )


async def _stage_task_tests(
    event: Any, context: EvaluationContext, state: Any
) -> None:
    """Upload the task's ``tests/`` into the sandbox before collection.

    Terminal-Bench tasks publish their reward from ``tests/test.sh``, and
    a terminal-bench suite's collect command runs that script. Harbor
    uploads ``tests/`` only at verification time — which is AFTER the
    collect phase — so a suite that reads a verifier-published observable
    must stage the tests itself (``driver.stage_tests_before_collect``).

    The upload happens once the agent has stopped, so the benchmark
    property Harbor protects is preserved: the agent never sees the
    tests. Failures are recorded on the trial and left to fail the
    collect step loudly rather than producing a rewardless trial that
    still looks gradable.
    """
    driver = getattr(getattr(context.suite, "overlay", None), "driver", None)
    if driver is None or not getattr(driver, "stage_tests_before_collect", False):
        return
    task_path = getattr(getattr(event.config, "task", None), "path", None)
    if task_path is None:
        state.evidence_issues.append("cannot stage task tests: no task path on the trial config")
        return
    tests_dir = Path(str(task_path)) / "tests"
    if not tests_dir.is_dir():
        state.evidence_issues.append(f"cannot stage task tests: {tests_dir} is missing")
        return
    environment = (
        context.environments.environment(state.trial_id)
        if context.environments is not None
        else None
    )
    if environment is None:
        state.evidence_issues.append("cannot stage task tests: no environment handle")
        return
    upload = getattr(environment, "upload_dir", None)
    if not callable(upload):
        state.evidence_issues.append("cannot stage task tests: environment has no upload_dir")
        return
    try:
        await upload(source_dir=tests_dir, target_dir=TEST_STAGE_DIR)
    except Exception as exc:  # noqa: BLE001 - recorded, then fatal at collection
        state.evidence_issues.append(f"staging task tests failed: {exc}")


async def _grade_and_record(event: Any, context: EvaluationContext, state: Any) -> None:
    """Run the grading pipeline and persist the trial record (P0-7).

    This is the production wiring P0-7 needs: without it no trial ever
    reaches the store and ``finalize_run`` refuses to seal the run
    (found on the real chain: "trial(s) without a store record").

    Only a trial whose evidence actually passed the gate is graded; a
    trial without verified evidence stays unrecorded on purpose, because
    a fabricated record would let an incomplete run seal. Grader-side
    failures are already persisted as ``infra_invalid`` by the pipeline;
    a pipeline-level error is recorded on the trial.
    """
    from aeval.contracts import TrialCoordinates
    from aeval.store.sqlite import TrialStore
    from aeval.verdict.pipeline import GradingPipelineError, grade_and_record
    from aeval.verdict.progress import RequirementProgress

    bundle = context.artifacts.get(state.trial_id)
    if bundle is None or getattr(bundle, "bundle_descriptor", None) is None:
        return

    # Requirement bitmap from the stages that actually ran. judge_finished
    # is deliberately absent: only the pipeline may set it.
    progress = RequirementProgress()
    progress.mark("input_complete")        # evidence inputs verified
    progress.mark("artifact_schema_ok")    # fixed-path/schema discipline passed
    progress.mark("integration_valid")     # binding + descriptor verified
    if state.phase == "ended":
        progress.mark("agent_finished")
    transcript_extra = _transcript_extra(context, state)
    if transcript_extra is not None:
        progress.mark("render_valid")      # canonical transcript readable

    store = TrialStore(context.store_path)
    try:
        await grade_and_record(
            suite=context.suite,
            trial_id=state.trial_id,
            coordinates=TrialCoordinates(
                run_id=context.run_id,
                suite_id=context.suite.id,
                suite_version=context.suite.version,
                task_id=str(getattr(event, "task_name", "unknown")),
                trial_index=context.next_trial_index(),
            ),
            stop_reason=bundle.stop_reason,
            baseline_ok=state.baseline_ok,
            progress=progress,
            evidence=bundle,
            transcript_extra=transcript_extra,
            store=store,
            adapter=_observed_adapter(context, state),
            # Runtime-only base for sealed artifact paths: trajectory
            # graders read the sealed canonical transcript from it.
            artifact_base=(
                str(state.trial_dir) if state.trial_dir is not None else None
            ),
        )
    except GradingPipelineError as exc:
        state.mark_infra_invalid(f"grading failed: {exc}")
    finally:
        store.close()


async def _record_unjudgeable_exclusion(
    event: Any, context: EvaluationContext, state: Any, reason: str
) -> bool:
    """Persist an explicit, reasoned exclusion for an unverifiable trial.

    D52 (found on the real chain): a trial whose agent died before the
    official session record existed — e.g. the DSH run hit the
    single-response token cap mid-turn — left no store record at all,
    because only a trial that passed the evidence gate is graded. The run
    then refused to seal ("trial(s) without a store record") and the
    evidence of every *other* trial was lost with it.

    The score-validity invariant is unchanged: the verifier still never
    runs for this trial (the gate keeps raising), so it can never enter
    the valid denominator. What changes is that the trial is *recorded*
    as ``cannot_judge`` with the reason attached, so the seal reports an
    explicit exclusion instead of an unexplained gap.

    Returns True when a record for the trial now exists in the store.
    """
    from aeval.contracts import BudgetSnapshot, TrialCoordinates, TrialRecord
    from aeval.store.sqlite import TrialStore

    observed = _observed_adapter(context, state)
    store = TrialStore(context.store_path)
    try:
        try:
            store.load_trial(state.trial_id)
            return True  # already graded and recorded
        except KeyError:
            pass
        record = TrialRecord(
            trial_id=state.trial_id,
            coordinates=TrialCoordinates(
                run_id=context.run_id,
                suite_id=context.suite.id,
                suite_version=context.suite.version,
                task_id=str(getattr(event, "task_name", "unknown")),
                trial_index=context.next_trial_index(),
            ),
            stop_reason=state.stop_reason or "crashed",
            baseline_ok=state.baseline_ok,
            adapter=observed,
            budget=(
                BudgetSnapshot(enforcement_point=observed.budget_enforcement)
                if observed is not None
                else None
            ),
            verdict="cannot_judge",
            transcript_extra={
                "aeval": {
                    "exclusion_reason": reason,
                    "evidence_issues": list(state.evidence_issues),
                    "infra_invalid_reasons": list(state.infra_invalid_reasons),
                }
            },
        )
        store.persist_trial_with_grades(record, [])
        return True
    except Exception as exc:  # a recording gap must never crash the hook
        state.evidence_issues.append(
            f"explicit exclusion record could not be written: {exc}"
        )
        return False
    finally:
        store.close()


def _require_adapter_contract(job: Any, budget: Any = None, *, accepted: bool = False) -> None:
    """Refuse a run whose selected adapter cannot describe itself, be read, or be metered.

    The recorded adapter identity (``AdapterSpec``) is what keeps a second agent
    distinguishable from this one in the store and in comparability. Checked at
    run-context creation so the failure is a refusal, not a gap discovered in the
    evidence months later. Harbor-native ``name:`` agents (nop/oracle) make no
    such declaration and are exempt — they never enter the evaluation chain.
    """
    from aeval.agents.contract import (
        adapter_declaration_gap,
        adapter_member_gap,
        budget_gate_violation,
        build_adapter_spec,
        load_adapter_class,
    )

    selected = []
    for entry in getattr(job.config, "agents", None) or []:
        import_path = getattr(entry, "import_path", None)
        if not import_path:
            continue
        adapter_class = load_adapter_class(import_path)
        member_gap = adapter_member_gap(adapter_class)
        if member_gap:
            raise HookRegistrationError(
                f"agent adapter {import_path} is missing required members {member_gap} — "
                "the evidence chain reads them at trial end (see "
                "aeval.agents.contract.AgentAdapter)"
            )
        gap = adapter_declaration_gap(adapter_class)
        if gap:
            raise HookRegistrationError(
                f"agent adapter {import_path} declares no {gap} — the run could not "
                "record which adapter produced its trials; declare them on the class "
                "(see aeval.agents.contract)"
            )
        selected.append(build_adapter_spec(adapter_class, import_path=import_path))

    if selected:
        violation = budget_gate_violation(selected, budget, accepted=accepted)
        if violation:
            raise HookRegistrationError(violation)


def _observed_adapter(context: EvaluationContext, state: Any) -> Any:
    """AdapterSpec of the live agent that ran this trial (None when unreadable).

    Observation, not declaration: the manifest records what the run *intended*
    (resolved from the job), this records what actually produced the trial — the
    agent's reported version included. A failure here is appended to the trial's
    evidence issues rather than swallowed.
    """
    from aeval.agents.contract import build_adapter_spec

    # Recording identity must never cost us the record itself (D52: a trial that
    # vanishes from the store refuses the whole seal), so every failure here is
    # appended to the trial's evidence issues and the record is still written.
    try:
        environments = getattr(context, "environments", None)
        agent = environments.agent(state.trial_id) if environments is not None else None
        if agent is None:
            return None
        version = agent.version() if callable(getattr(agent, "version", None)) else None
        return build_adapter_spec(agent, version=version)
    except Exception as exc:  # noqa: BLE001 - identity is best-effort, the record is not
        state.evidence_issues.append(f"adapter identity could not be recorded: {exc}")
        return None


def _transcript_extra(context: EvaluationContext, state: Any) -> dict[str, Any] | None:
    """The ATIF ``extra`` envelope for grading, read through the official
    session path (never parsed by hand); ``None`` when unreadable.

    A failure here is recorded on the trial instead of being swallowed:
    the grader then reports ``cannot_judge`` (its required completeness
    fields are unavailable), and the reason must be visible in the audit
    rather than inferred (found on the real chain: the verdict was
    cannot_judge with nothing explaining why).
    """
    environments = context.environments
    agent = environments.agent(state.trial_id) if environments is not None else None
    if agent is None or not hasattr(agent, "read_trial_session"):
        state.evidence_issues.append(
            "grading has no agent to read the official session from"
        )
        return None
    try:
        transcript = agent.read_trial_session()
    except Exception as exc:
        state.evidence_issues.append(
            f"official session read failed at grading time: {exc}"
        )
        return None
    extra: dict[str, Any] = {"aeval": {}}
    completeness = getattr(transcript, "completeness", None)
    if completeness is not None:
        extra["aeval"]["completeness"] = completeness.model_dump(mode="json")
    extra["aeval"]["stop_reason"] = getattr(transcript, "stop_reason", None)
    return extra


class AevalPlugin:
    def __init__(self) -> None:
        self._context: EvaluationContext | None = None
        self._summary_written = False

    async def on_job_start(self, job: Any) -> None:
        if self._context is not None:
            raise LifecycleError("plugin instance already owns a job")
        context = create_run_context(job)
        if context.job_id != str(job.id):
            raise LifecycleError("context belongs to another job")
        context.environments = TrialEnvironmentRegistry()
        try:
            install_trial_capture(job, context.environments)
        except EnvironmentAccessError as exc:
            raise HookRegistrationError(str(exc)) from exc
        self._context = context
        register_trial_hooks(job, context)

    async def on_job_end(self, job_result: Any) -> None:
        context = self._context
        if context is None or context.job_id != str(job_result.id):
            raise LifecycleError("JobResult does not belong to this plugin instance")
        if self._summary_written:
            return
        states = [(context.state_for_result(result), result) for result in job_result.trial_results]
        for state, result in states:
            if result.exception_info is not None:
                state.finish(
                    result.exception_info,
                    cancelled=result.exception_info.exception_type == "CancelledError",
                )
        for state in context.trials.values():
            # A broker that exited on its own closed its lease; record why
            # before the owner stops it (only its stderr tail explains it).
            unexpected = note_broker_unexpected_exit(state)
            if unexpected is not None:
                state.mark_infra_invalid(unexpected)
            stop_trial_broker(state)
            if not state.terminal:
                state.mark_infra_invalid("job ended without a terminal trial event")
                state.phase = "failed"
        context.closed = True
        unobserved = job_result.n_total_trials - len(context.trials)
        summary = {
            "run_id": context.run_id,
            "job_id": context.job_id,
            "run_binding": context.run_binding.model_dump() if context.run_binding else None,
            "runtime_lock_digest": context.runtime_lock.digest(),
            "suite": {
                "id": context.suite.id,
                "version": context.suite.version,
                "overlay_digest": context.suite.suite_yaml_digest,
                "overlay_chain_digest": context.suite.identity_digest,
                "sources": [source.model_dump() for source in context.suite.sources],
            },
            "unobserved_trials": unobserved,
            "exclusions": context.exclusion_lines(),
            "trials": {
                tid: {
                    "session_id": state.session_id,
                    "phase": state.phase,
                    "binding": state.binding.model_dump() if state.binding else None,
                    "baseline_ok": state.baseline_ok,
                    "evidence_ok": state.evidence_ok,
                    "evidence_issues": state.evidence_issues,
                    "infra_invalid_reasons": state.infra_invalid_reasons,
                    "stop_reason": state.stop_reason,
                    "exception": state.exception,
                }
                for tid, state in context.trials.items()
            },
        }
        _atomic_write_json(context.run_dir / "aeval_run_summary.json", summary)
        self._summary_written = True
