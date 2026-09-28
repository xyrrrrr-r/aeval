"""Harbor job owner; job-end summary failures remain warnings, not a verdict gate."""

from __future__ import annotations

import json
import os
from hashlib import sha256
from pathlib import Path
from typing import Any

from aeval.bundle.manifest import _atomic_write_json
from aeval.contracts import RunBinding, RunManifest, RuntimeLock, job_config_hash
from aeval.hooks.baseline_arrival import on_environment_started
from aeval.hooks.collection import CollectionError, collect_trial_evidence
from aeval.control.bootstrap import BootstrapError, bootstrap_trial_control
from aeval.hooks.broker_lifecycle import (
    note_broker_unexpected_exit,
    BrokerSpecError,
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
    if (manifest.overlay.suite_id != suite.id or manifest.overlay.suite_version != suite.version
            or manifest.overlay.overlay_digest != suite.suite_yaml_digest):
        raise HookRegistrationError("suite identity differs from intent manifest")
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
            binding, config = await bootstrap_trial_control(
                environment=env_handle,
                context=context,
                trial_id=state.trial_id,
                paths=trial_control_paths(state, context.run_dir),
                broker=state.broker,
                provider=str(context.broker_spec.identity.get("provider", "")),
                model=str(context.broker_spec.identity.get("model", "")),
                # in-sandbox control stack (verified deployment, §7.5/7.6)
                agent=(
                    context.environments.agent(state.trial_id)
                    if context.environments is not None else None
                ),
                control_dist=getattr(context.broker_spec, "control_dist", None),
                control_ca=getattr(context.broker_spec, "control_ca", None),
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
        if state.infra_invalid_reasons:
            issue = "agent ended with infra failures recorded"
            if issue not in state.evidence_issues:
                state.evidence_issues.append(issue)

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
