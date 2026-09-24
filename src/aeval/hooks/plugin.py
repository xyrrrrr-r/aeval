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
from aeval.hooks.context import EvaluationContext, LifecycleError
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
    return EvaluationContext(
        run_id=run_id, runtime_lock=lock, suite=suite, run_dir=root,
        store_path=Path(store_path), run_binding=binding, job_id=str(job.id),
        trials_dir=trials_dir,
    )


def register_trial_hooks(job: Any, context: EvaluationContext) -> None:
    async def _trial_started(event: Any) -> None:
        context.start_trial(event)

    async def _environment_started(event: Any) -> None:
        state = context.state_for_event(event)
        if state.terminal:
            return
        try:
            await on_environment_started(event, context)
        except Exception as exc:
            state.mark_infra_invalid(f"environment_started audit failed: {exc}")

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
        if state.finish(exception, cancelled=cancelled):
            await finalize_trial_record(event, context)

    async def _trial_ended(event: Any) -> None:
        await _finish(event)

    async def _trial_cancelled(event: Any) -> None:
        await _finish(event, cancelled=True)

    try:
        job.on_trial_started(_trial_started)
        job.on_environment_started(_environment_started)
        job.on_agent_ended(_agent_ended)
        job.on_verification_started(_verification_started)
        job.on_trial_ended(_trial_ended)
        job.on_trial_cancelled(_trial_cancelled)
    except Exception as exc:
        raise HookRegistrationError(f"failed to register trial hooks on the job: {exc}") from exc


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
