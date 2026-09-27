"""Evaluation context owned by one Harbor job plugin instance."""

from __future__ import annotations

from dataclasses import dataclass, field
from hashlib import sha256
from pathlib import Path
from typing import Any, Literal
from uuid import uuid4

from aeval.contracts import (
    BundleDescriptor, RunBinding, RuntimeLock, TrialBinding, TrialPaths,
    canonical_json, control_config_digest,
)
from aeval.suite_models import ResolvedSuite


class LifecycleError(RuntimeError):
    pass


@dataclass
class TrialState:
    trial_id: str
    baseline_ok: bool = True
    baseline_failures: list[str] = field(default_factory=list)
    evidence_ok: bool = True
    evidence_issues: list[str] = field(default_factory=list)
    stop_reason: str | None = None
    infra_invalid_reasons: list[str] = field(default_factory=list)
    session_id: str = field(default_factory=lambda: str(uuid4()))
    phase: Literal["created", "running", "ended", "failed", "cancelled"] = "created"
    trial_dir: Path | None = None
    event_identity: str | None = None
    binding: TrialBinding | None = None
    exception: dict[str, Any] | None = None
    # owner-side handle + composed config of the trial's model broker
    # (hooks/broker_lifecycle.py); runtime state, never serialized.
    broker: Any = None
    control_config: dict[str, Any] | None = None

    @property
    def terminal(self) -> bool:
        return self.phase in ("ended", "failed", "cancelled")

    def mark_infra_invalid(self, reason: str) -> None:
        if reason not in self.infra_invalid_reasons:
            self.infra_invalid_reasons.append(reason)
        self.stop_reason = "infra_error"

    def finish(self, exception: Any = None, *, cancelled: bool = False) -> bool:
        if self.phase == "created":
            raise LifecycleError("trial ended before start")
        phase = "cancelled" if cancelled else "failed" if exception else "ended"
        if self.terminal:
            if self.phase != "ended" or phase == "ended":
                return False
        self.phase = phase
        if exception is not None:
            self.exception = exception.model_dump(mode="json")
        if cancelled:
            self.mark_infra_invalid("Harbor trial cancelled")
        elif exception is not None:
            self.stop_reason = self.stop_reason or "crashed"
        return True


@dataclass
class EvaluationContext:
    run_id: str
    runtime_lock: RuntimeLock
    suite: ResolvedSuite
    run_dir: Path
    store_path: Path
    trials: dict[str, TrialState] = field(default_factory=dict)
    artifacts: dict[str, Any] = field(default_factory=dict)
    run_binding: RunBinding | None = None
    job_id: str | None = None
    trials_dir: Path | None = None
    closed: bool = False
    broker_spec: Any = None

    def trial_state(self, trial_id: str) -> TrialState:
        if not trial_id:
            raise LifecycleError("empty trial identity")
        if trial_id not in self.trials:
            if self.closed:
                raise LifecycleError("run is closed")
            self.trials[trial_id] = TrialState(trial_id=trial_id)
        return self.trials[trial_id]

    def _event_identity(self, event: Any) -> tuple[Path, str]:
        config = event.config
        if (event.result.config.model_dump(mode="json") != config.model_dump(mode="json")
                or event.result.trial_name != config.trial_name
                or event.result.task_name != event.task_name):
            raise LifecycleError("hook event and result identities differ")
        return self._trial_identity(config, event.task_name)

    def _trial_identity(self, config: Any, task_name: str) -> tuple[Path, str]:
        if str(config.job_id) != self.job_id:
            raise LifecycleError("trial event belongs to another job")
        name = config.trial_name
        if not name or name in (".", "..") or any(c in name for c in ("/", "\\", ":")):
            raise LifecycleError("trial_name must be a single directory name")
        parent = Path(config.trials_dir).resolve()
        path = (parent / name).resolve()
        if (self.trials_dir is None or parent != self.trials_dir.resolve()
                or not path.is_relative_to(parent) or path == parent):
            raise LifecycleError("trial path differs from the job-owned directory")
        if not path.is_relative_to(self.run_dir.resolve()):
            raise LifecycleError("trial path escapes the run directory")
        identity = sha256(canonical_json({
            "task_name": task_name,
            "config": config.model_dump(mode="json"),
        })).hexdigest()
        return path, identity

    def start_trial(self, event: Any) -> TrialState:
        if self.closed:
            raise LifecycleError("run is closed")
        path, identity = self._event_identity(event)
        trial_id = str(event.trial_id)
        for other in self.trials.values():
            if other.trial_id != trial_id and other.trial_dir == path:
                raise LifecycleError("trial directory is already owned by another trial")
        state = self.trial_state(trial_id)
        if state.phase != "created":
            self.state_for_event(event)
            if state.terminal:
                raise LifecycleError("terminal trial cannot be restarted")
            return state
        state.trial_dir = path
        state.event_identity = identity
        state.phase = "running"
        return state

    def state_for_event(self, event: Any) -> TrialState:
        if self.closed:
            raise LifecycleError("run is closed")
        state = self.trials.get(str(event.trial_id))
        if state is None or state.phase == "created":
            raise LifecycleError("trial event arrived before start")
        path, identity = self._event_identity(event)
        if path != state.trial_dir or identity != state.event_identity:
            raise LifecycleError("trial identity changed after start")
        return state

    def state_for_result(self, result: Any) -> TrialState:
        state = self.trials.get(str(result.id))
        if state is None:
            raise LifecycleError("JobResult contains an unowned trial")
        path, identity = self._trial_identity(result.config, result.task_name)
        if (result.trial_name != result.config.trial_name or path != state.trial_dir
                or identity != state.event_identity):
            raise LifecycleError("JobResult trial identity differs from start")
        return state

    def bind_control(self, trial_id: str, config: dict[str, Any], paths: TrialPaths) -> TrialBinding:
        state = self.trials.get(trial_id)
        if self.closed or state is None or state.phase != "running":
            raise LifecycleError("control binding requires an active trial")
        if self.run_binding is None or RunBinding.model_validate(config["run"]) != self.run_binding:
            raise LifecycleError("control run binding differs from the trusted run")
        if config["trialId"] != trial_id or config["sessionId"] != state.session_id:
            raise LifecycleError("control trial/session differs from owner identity")
        digest = control_config_digest(config)
        if config["configDigest"] != digest:
            raise LifecycleError("control config digest mismatch")
        if config["bundlePath"] != paths.bundle_path or config["sessionRoot"] != paths.session_root:
            raise LifecycleError("control paths differ from owner paths")
        download = (self.run_dir / paths.download_root).resolve()
        if (not download.is_relative_to(self.run_dir.resolve())
                or state.trial_dir is None or download != (state.trial_dir / "agent").resolve()):
            raise LifecycleError("download root differs from the trial agent directory")
        binding = TrialBinding(
            run=self.run_binding, trial_id=trial_id, session_id=state.session_id,
            config_digest=digest, paths=paths,
        )
        if state.binding is not None and state.binding != binding:
            raise LifecycleError("control binding cannot be replaced")
        state.binding = binding
        return binding

    def verify_descriptor(self, trial_id: str, descriptor: BundleDescriptor) -> None:
        state = self.trials[trial_id]
        if state.binding is None:
            raise LifecycleError("trial has no trusted control binding")
        try:
            state.binding.verify_descriptor(descriptor)
        except ValueError as exc:
            raise LifecycleError(str(exc)) from exc

    def exclusion_lines(self) -> list[str]:
        return [
            f"{state.trial_id}: {reason}"
            for state in self.trials.values()
            for reason in state.infra_invalid_reasons
        ]
