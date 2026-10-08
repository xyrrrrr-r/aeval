"""Owner-side access to the LIVE trial and its started environment.

Harbor's hook events carry only ``event/task_name/config/result/lock`` —
never the environment object — so the owner (this plugin) has no public
way to run the out-of-band probes the isolation checks require (``await env.exec``,
``env.network_policy``) or to produce the collection artifacts from
the real sandbox. This module is the narrow, defensive integration seam
that fixes that:

- ``TrialQueue._setup_hooks(trial)`` is the one place where the queue
  wires its (and therefore our) hooks onto a concrete ``Trial``
  instance. We wrap it, remember the trial, and leave the original
  behaviour untouched.
- The environment object exists on the trial from ``Trial.create``
  onwards, but it is only STARTED after ``ENVIRONMENT_START`` is
  emitted. Therefore the handle is only usable from ``AGENT_START``
  (sandbox created + healthcheck passed + agent installed, agent not
  yet running).

Fail-closed contract: if Harbor's private surface is missing or shaped
differently, installation raises immediately at registration time (the
operator sees it before any trial runs) and lookups return ``None`` so
every caller keeps its "no environment handle → block" semantics.
Nothing here silently degrades into a passing check.
"""

from __future__ import annotations

from typing import Any, Callable

__all__ = [
    "EnvironmentAccessError",
    "TrialEnvironmentRegistry",
    "install_trial_capture",
]


class EnvironmentAccessError(RuntimeError):
    """The owner cannot reach live trials on this Harbor build."""


class TrialEnvironmentRegistry:
    """Trial objects captured from the queue, keyed by trial id.

    The registry holds strong references only for the duration of the
    run (trials hold sandbox handles); ``forget`` drops one when its
    trial reaches a terminal state.
    """

    def __init__(self) -> None:
        self._trials: dict[str, Any] = {}

    def capture(self, trial: Any) -> None:
        trial_id = getattr(trial, "id", None)
        if trial_id is None:
            return
        self._trials[str(trial_id)] = trial

    def forget(self, trial_id: str) -> None:
        self._trials.pop(str(trial_id), None)

    def trial(self, trial_id: str) -> Any | None:
        return self._trials.get(str(trial_id))

    def environment(self, trial_id: str) -> Any | None:
        """The trial's environment object, or ``None`` when unreachable.

        ``None`` means "cannot observe" — callers must treat that as a
        blocking condition, never as a pass.
        """
        trial = self._trials.get(str(trial_id))
        if trial is None:
            return None
        return getattr(trial, "agent_environment", None)

    def agent(self, trial_id: str) -> Any | None:
        trial = self._trials.get(str(trial_id))
        if trial is None:
            return None
        return getattr(trial, "agent", None)

    def __len__(self) -> int:
        return len(self._trials)

    def __contains__(self, trial_id: object) -> bool:
        return str(trial_id) in self._trials


def install_trial_capture(job: Any, registry: TrialEnvironmentRegistry) -> None:
    """Wrap the job's trial queue so every trial is captured as it starts.

    Verified against Harbor 0.23.0: ``Job._trial_queue._setup_hooks`` is
    called with the constructed ``Trial`` right before ``trial.run()``.
    The wrapper is additive: the original is always invoked first and
    its result/exception propagates unchanged.
    """
    queue = getattr(job, "_trial_queue", None)
    if queue is None:
        raise EnvironmentAccessError(
            "Harbor job exposes no _trial_queue — the owner cannot reach "
            "live trials on this build; environment probes would be "
            "unverifiable, so the run is refused"
        )
    original: Callable[..., Any] | None = getattr(queue, "_setup_hooks", None)
    if not callable(original):
        raise EnvironmentAccessError(
            "Harbor trial queue exposes no _setup_hooks — the owner cannot "
            "capture trials on this build; refusing to run with unverifiable "
            "environment probes"
        )
    if getattr(queue, "_aeval_capture_installed", False):
        return

    def _setup_hooks_with_capture(trial: Any, *args: Any, **kwargs: Any) -> Any:
        result = original(trial, *args, **kwargs)
        registry.capture(trial)
        return result

    queue._setup_hooks = _setup_hooks_with_capture
    queue._aeval_capture_installed = True
