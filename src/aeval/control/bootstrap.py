"""Sandbox bootstrap for controlled model routing (P0-4 offline half).

Production order for one trial:

1. the owner (the plugin) has a session id and trusted ``TrialPaths``;
2. ``ModelBrokerProcess`` starts the host-side broker (control/broker.py)
   which writes a job token file on the HOST;
3. THIS module uploads the token into the sandbox and composes the
   control config: gateway URL, token path, pinned identity,
   ``refuseAuxiliaryCalls`` so no default route can bypass the broker;
4. the config digest is computed and the owner binding is created via
   ``EvaluationContext.bind_control`` — the one place that compares the
   control claim against the trusted run identity.

The upstream API key never enters the sandbox: the broker reads it on
the host. The sandbox receives only the broker URL and the job token.

What still needs the real environment (documented boundary): deploying
the official session-persistence plugin/overlay into ``DSH_HOME`` and
verifying the sandbox actually adopted them (upload ownership/mode are
unmeasured on e2b), plus one real tool action through the stub. Those
are environment-verification phase, not offline code.
"""

from __future__ import annotations

from pathlib import Path, PurePosixPath
from typing import Any

from aeval.contracts import (
    TrialBinding,
    TrialPaths,
    control_config_digest,
)
from aeval.control.broker import ModelBrokerProcess
from aeval.hooks.context import EvaluationContext

__all__ = [
    "BootstrapError",
    "SANDBOX_TOKEN_PATH",
    "compose_control_config",
    "bootstrap_trial_control",
    "teardown_trial_control",
]


class BootstrapError(RuntimeError):
    """The sandbox could not be bootstrapped for controlled routing."""


# Fixed in-sandbox location of the job token (fixed-name contract; the
# control config points at this exact path).
SANDBOX_TOKEN_PATH = PurePosixPath("/run/aeval/trial-token")


def compose_control_config(
    *,
    run_binding: dict[str, Any],
    trial_id: str,
    session_id: str,
    paths: TrialPaths,
    gateway_url: str,
    job_token_file: str = SANDBOX_TOKEN_PATH.as_posix(),
    provider: str,
    model: str,
) -> dict[str, Any]:
    """Compose the control config handed to the sandboxed DSH runtime.

    ``refuseAuxiliaryCalls`` pins the routing: no default provider, no
    title/auxiliary model calls — everything goes through the broker.
    """
    config = {
        "run": dict(run_binding),
        "trialId": trial_id,
        "sessionId": session_id,
        "sessionRoot": paths.session_root,
        "bundlePath": paths.bundle_path,
        "gatewayUrl": gateway_url,
        "jobTokenFile": job_token_file,
        "provider": provider,
        "model": model,
        "refuseAuxiliaryCalls": True,
    }
    config["configDigest"] = control_config_digest(config)
    return config


async def bootstrap_trial_control(
    *,
    environment: Any,
    context: EvaluationContext,
    trial_id: str,
    paths: TrialPaths,
    broker: ModelBrokerProcess,
    provider: str,
    model: str,
    job_token_file: str = SANDBOX_TOKEN_PATH.as_posix(),
) -> tuple[TrialBinding, dict[str, Any]]:
    """Upload the token, bind the control config; return (binding, config).

    Fail-closed: no broker URL/token, no upload surface, or an owner
    binding mismatch each raise ``BootstrapError`` — the trial must not
    run with uncontrolled model routing.
    """
    if broker.url is None or broker.token_path is None:
        raise BootstrapError(
            "broker is not ready — start it before bootstrapping the sandbox"
        )
    if not broker.token_path.is_file():
        raise BootstrapError(
            f"broker did not write its job token at {broker.token_path}"
        )
    token = broker.token_path.read_text(encoding="utf-8").strip()
    if not token:
        raise BootstrapError("broker job token is empty")

    state = context.trials.get(trial_id)
    if state is None or state.session_id is None:
        raise BootstrapError(
            "owner has no session id for this trial — bind_control must come "
            "after trial start"
        )

    upload = getattr(environment, "upload_file", None)
    if not callable(upload):
        raise BootstrapError(
            "environment exposes no upload_file — cannot deploy the job token"
        )
    # Host-side temp copy; the environment API takes a host path and a
    # sandbox target path.
    import os
    import tempfile

    fd, tmp = tempfile.mkstemp(prefix="aeval-token-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(token)
            f.flush()
            os.fsync(f.fileno())
        os.chmod(tmp, 0o600)
        await upload(tmp, job_token_file)
    finally:
        Path(tmp).unlink(missing_ok=True)

    # The plugin's broker lifecycle (hooks/broker_lifecycle.py) composes
    # the control config BEFORE the broker starts — that config, not a
    # recomposition, is the one the trial runs under. When present it is
    # authoritative: the broker URL must already be pinned inside it.
    if state.control_config is not None:
        config = state.control_config
        if config.get("gatewayUrl") != broker.url:
            raise BootstrapError(
                f"control config pins gateway {config.get('gatewayUrl')!r} but "
                f"the broker serves {broker.url!r}"
            )
    else:
        config = compose_control_config(
            run_binding=context.run_binding.model_dump() if context.run_binding else {},
            trial_id=trial_id,
            session_id=state.session_id,
            paths=paths,
            gateway_url=broker.url,
            job_token_file=job_token_file,
            provider=provider,
            model=model,
        )
    try:
        binding = context.bind_control(trial_id, config, paths)
    except Exception as exc:
        raise BootstrapError(f"owner refused the control binding: {exc}") from exc
    return binding, config


async def teardown_trial_control(
    broker: ModelBrokerProcess, *, reason: str = "trial_end"
) -> int:
    """Stop the broker after the trial; report the exit code (never raise).

    The bin performs its own token cleanup on normal shutdown (exit 0);
    a nonzero code here is recorded by the caller as an infra issue.
    """
    return broker.stop(reason)
