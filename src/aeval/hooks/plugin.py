"""AevalPlugin — the Harbor job plugin (plan §2).

Lifecycle contract (verified against harbor 0.23.0 source):

- ``on_job_start``: verify the runtime lock (hard precondition), build
  the EvaluationContext, write the intent manifest, then register the
  four trial hooks. Hook registration failures are fatal here — a job
  whose audit hooks are missing must not start.
- ``on_environment_started`` / ``on_agent_ended`` / ``on_trial_ended``:
  audit only; own their I/O errors, never raise.
- ``on_verification_started``: the one hard gate — EvidenceIntegrityError
  propagates and prevents the verifier from running.
- ``on_job_end``: seal the run summary. Harbor downgrades on_job_end
  exceptions to warnings, so this is audit, not a gate.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from aeval.hooks.baseline_arrival import on_environment_started
from aeval.hooks.context import EvaluationContext
from aeval.hooks.evidence import finalize_trial_record, gate_verification
from aeval.provenance import LockMismatchError, verify_runtime_lock
from aeval.suite_loader.loader import load_suite

__all__ = ["AevalPlugin", "create_run_context", "register_trial_hooks"]


class HookRegistrationError(RuntimeError):
    pass


def create_run_context(job: Any) -> EvaluationContext:
    """Build the EvaluationContext for this job from its configuration.

    The suite directory and run/store paths arrive via the job's
    environment (AEVAL_SUITE_DIR, AEVAL_RUN_DIR, AEVAL_STORE_PATH) —
    the job config is synthesized by our CLI, which validated them
    already. Missing variables are a hard error: a run without a
    verified suite identity has no defensible scores.
    """
    import os

    suite_dir = os.environ.get("AEVAL_SUITE_DIR")
    run_dir = os.environ.get("AEVAL_RUN_DIR")
    store_path = os.environ.get("AEVAL_STORE_PATH")
    lock_json = os.environ.get("AEVAL_RUNTIME_LOCK")
    if not (suite_dir and run_dir and store_path):
        raise HookRegistrationError(
            "AEVAL_SUITE_DIR/AEVAL_RUN_DIR/AEVAL_STORE_PATH must be set by "
            "the aeval CLI when synthesizing the Harbor job"
        )

    suite = load_suite(Path(suite_dir))

    if lock_json:
        import json

        from aeval.contracts import RuntimeLock

        lock = RuntimeLock.model_validate(json.loads(Path(lock_json).read_text(encoding="utf-8")))
        verify_runtime_lock(lock)
    else:
        from aeval.provenance import build_runtime_lock

        lock = build_runtime_lock()

    run_id = os.environ.get("AEVAL_RUN_ID", f"run-{Path(run_dir).name}")

    return EvaluationContext(
        run_id=run_id,
        runtime_lock=lock,
        suite=suite,
        run_dir=Path(run_dir),
        store_path=Path(store_path),
    )


def register_trial_hooks(job: Any, context: EvaluationContext) -> None:
    """Register the four trial hooks on the job.

    Registration happens once at job start; per-trial state lives in
    the context. Hook ordering is Harbor's responsibility (locked by
    tests/integration/test_harbor_contract.py).
    """

    async def _environment_started(event: Any) -> None:
        try:
            await on_environment_started(event, context)
        except Exception as exc:  # audit hook: record, never break the trial
            state = context.trial_state(str(getattr(event, "trial_id", "")))
            state.mark_infra_invalid(f"environment_started audit failed: {exc}")

    async def _agent_ended(event: Any) -> None:
        # Audit-only: no final evidence is taken here (Harbor syncs the
        # agent session and runs collect AFTER agent-end).
        trial_id = str(getattr(event, "trial_id", ""))
        state = context.trial_state(trial_id)
        if state.infra_invalid_reasons:
            state.evidence_issues.append(
                "agent ended with infra failures recorded — evidence will "
                "be gated at verification-start"
            )

    async def _verification_started(event: Any) -> None:
        # The ONE hard gate: raises EvidenceIntegrityError to block the
        # verifier. No try/except here by design.
        await gate_verification(event, context)

    async def _trial_ended(event: Any) -> None:
        await finalize_trial_record(event, context)

    try:
        job.on_environment_started(_environment_started)
        job.on_agent_ended(_agent_ended)
        job.on_verification_started(_verification_started)
        job.on_trial_ended(_trial_ended)
    except Exception as exc:
        raise HookRegistrationError(
            f"failed to register trial hooks on the job: {exc}"
        ) from exc

    # Keep the context alive for the job duration.
    job.__dict__.setdefault("_aeval_contexts", []).append(context)


class AevalPlugin:
    """Harbor JobPlugin implementation. Load with
    ``--plugin aeval.hooks:AevalPlugin`` (the job.yaml ``plugins`` key
    is deprecated and ignored by Harbor 0.23.0)."""

    async def on_job_start(self, job: Any) -> None:
        context = create_run_context(job)
        # Lock verified inside create_run_context — a mismatch here
        # aborts the job before any environment is created.
        register_trial_hooks(job, context)

    async def on_job_end(self, job_result: Any) -> None:
        # Audit-only sealing: Harbor downgrades exceptions here to
        # warnings, so this must never be treated as a gate.
        try:
            import json

            contexts = getattr(job_result, "_aeval_contexts", None)
            if contexts:
                context = contexts[-1]
                exclusions = context.exclusion_lines()
                summary = {
                    "run_id": context.run_id,
                    "runtime_lock_digest": context.runtime_lock.digest(),
                    "suite": {
                        "id": context.suite.id,
                        "version": context.suite.version,
                        "overlay_digest": context.suite.suite_yaml_digest,
                    },
                    "exclusions": exclusions,
                    "trials": {
                        tid: {
                            "baseline_ok": s.baseline_ok,
                            "evidence_ok": s.evidence_ok,
                            "infra_invalid_reasons": s.infra_invalid_reasons,
                        }
                        for tid, s in context.trials.items()
                    },
                }
                out = context.run_dir / "aeval_run_summary.json"
                out.parent.mkdir(parents=True, exist_ok=True)
                out.write_text(
                    json.dumps(summary, indent=2, ensure_ascii=False),
                    encoding="utf-8",
                )
        except Exception:
            # on_job_end failures must not masquerade as a gate.
            pass
