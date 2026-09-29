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

import json
import shlex
import shutil
import tempfile
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING, Any

from aeval.contracts import (
    TrialBinding,
    TrialPaths,
    control_config_digest,
)
from aeval.agents.contract import control_stack_of
from aeval.agents.dsh.agent import SESSIONS_DIRNAME
from aeval.control.broker import ModelBrokerProcess

if TYPE_CHECKING:  # pragma: no cover - typing only
    # Imported for annotations only: a module-level import of aeval.hooks
    # closes a cycle (hooks -> plugin -> control.bootstrap -> hooks) that
    # crashes whenever this module is imported first.
    from aeval.hooks.context import EvaluationContext

__all__ = [
    "BootstrapError",
    "SANDBOX_TOKEN_PATH",
    "CONTROL_DIR_NAME",
    "compose_control_config",
    "deploy_control_stack",
    "bootstrap_trial_control",
    "teardown_trial_control",
]


# Where the control stack is deployed inside the sandbox: a SIBLING of the
# nested ``@deepseek-ai`` package directory, so node's ESM resolver finds
# the harness packages by walking up from the plugin files.
CONTROL_DIR_NAME = "aeval-control"


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
    reasoning_effort: str | None = None,
    limits: dict[str, int] | None = None,
    owner_finalize: bool = True,
    auxiliary_policy: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Compose the control config handed to the sandboxed DSH runtime.

    ``refuseAuxiliaryCalls`` pins the routing: no default provider, no
    title/auxiliary model calls — everything goes through the broker.
    ``auxiliary_policy`` (D47) overrides that blanket decision per purpose:
    a purpose set to ``allow`` is dispatched and metered by the broker, and
    the dispatch ledger beside the descriptor is the accounting evidence the
    transcript reducer merges. Purposes not listed keep the blanket refusal.

    ``reasoning_effort``, ``limits`` and ``auxiliary_policy`` MUST mirror the
    broker lease exactly: the sandbox adapter compares the broker's ``/info``
    against this config field by field, so a lease with ``maxSteps`` set and a
    config without it fails with ``AEVAL_LEASE_MISMATCH`` (found by
    running the real sandbox against a real broker).
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
    if reasoning_effort is not None:
        config["reasoningEffort"] = reasoning_effort
    for key in ("maxSteps", "maxTokens"):
        value = (limits or {}).get(key)
        if value is not None:
            config[key] = value
    if auxiliary_policy:
        config["auxiliaryPolicy"] = dict(auxiliary_policy)
    if owner_finalize:
        # Inside the sandbox no external party can reach ``evalControl``,
        # so the harness process performs the owner's durable-then-finalize
        # sequence itself; without it a completed run can only report
        # stop_reason=infra_error (real chain).
        config["ownerFinalize"] = True
    config["configDigest"] = control_config_digest(config)
    return config


async def deploy_control_stack(
    *,
    environment: Any,
    agent: Any,
    paths: TrialPaths,
    config: dict[str, Any],
    control_dist: Path,
    control_ca: Path | None,
    trial_id: str,
    sandbox_mode: str | None = None,
) -> str:
    """Deploy the in-sandbox control stack; return the patch file path.

    The shape is fixed by environment verification (example-lab full-chain
    report §7.5–7.6):

    - plugin files live inside the DSH install tree, because the CLI
      installs its dependencies NESTED (``@deepseek-ai/dsh/node_modules``)
      and node's ESM resolver only finds them for a sibling package;
    - the Cordis patch lists the transport entry first, then the control
      plugin (which injects ``evalBroker``);
    - the owner-assigned session must already EXIST, because
      ``dsh --session-id`` only resumes one (D15) — the stub mints it;
    - a privately signed broker needs ``NODE_EXTRA_CA_CERTS`` in the run;
    - the suite's declared confinement posture is passed as
      ``DSH_PERMISSION_MODE`` (see ``DriverSpec.sandbox_mode``).
    """
    bin_dir = agent.cli_bin_dir() if hasattr(agent, "cli_bin_dir") else None
    if not bin_dir:
        raise BootstrapError(
            "the agent reports no CLI install prefix — the control stack "
            "cannot be placed inside the sandbox's DSH tree"
        )
    prefix = PurePosixPath(bin_dir).parent          # <prefix>/bin -> <prefix>
    target = (prefix / "lib" / "node_modules" / "@deepseek-ai" / "dsh"
              / "node_modules" / CONTROL_DIR_NAME)

    upload = getattr(environment, "upload_file", None)
    if not callable(upload):
        raise BootstrapError("environment exposes no upload_file for the control stack")
    dist_files = sorted(Path(control_dist).glob("*.js"))
    if not dist_files:
        raise BootstrapError(f"control dist has no built .js files: {control_dist}")

    staging = Path(tempfile.mkdtemp(prefix="aeval-control-"))
    try:
        await environment.exec(f"mkdir -p {shlex.quote((target / 'dist').as_posix())}")
        payload = {"package.json": json.dumps({
            "name": CONTROL_DIR_NAME, "version": "0.1.0", "type": "module",
            "main": "dist/index.js",
        })}
        for source in dist_files:
            payload[f"dist/{source.name}"] = source.read_text(encoding="utf-8")
        payload["config.json"] = json.dumps(config, indent=2)
        if control_ca is not None:
            payload["ca.crt"] = Path(control_ca).read_text(encoding="utf-8")
        payload["cordis.patch.yml"] = _control_patch_yaml(target, config, control_ca)
        for relative, text in payload.items():
            local = staging / relative.replace("/", "__")
            local.write_text(text, encoding="utf-8")
            await upload(str(local), (target / relative).as_posix())

        await _mint_owner_session(
            environment=environment, paths=paths, target=target,
            trial_id=trial_id, config=config,
        )

        patch_path = (target / "cordis.patch.yml").as_posix()
        # add_patch_file is DSH's own method (Harbor's BaseInstalledAgent has no
        # such API), so a non-DSH adapter used to die here with a bare
        # AttributeError. Silently skipping is not an option either: that would
        # drop the control stack without saying so. Declarative injection
        # arrives with the adapter contract (P2-4).
        if not hasattr(agent, "add_patch_file"):
            raise BootstrapError(
                f"agent adapter {type(agent).__name__} cannot accept patch injection "
                f"({patch_path}); the control stack cannot be applied to it"
            )
        agent.add_patch_file(patch_path)
        if hasattr(agent, "pin_session"):
            # the run must adopt the trial's own session, not mint one
            agent.pin_session(str(config["sessionId"]))
        if hasattr(agent, "set_workspace_dir"):
            # the session was minted in this cwd; DSH refuses to resume a
            # session recorded elsewhere, and the task workspace is where
            # the run belongs
            agent.set_workspace_dir(str(paths.sandbox_cwd))
        if control_ca is not None:
            agent.set_run_env("NODE_EXTRA_CA_CERTS", (target / "ca.crt").as_posix())
        if sandbox_mode is not None:
            # The CLI reads this in its composed profile (dsh-base's
            # cordis.patch.yml): the bash executor skips confinement for
            # danger-full-access instead of probing for a runner the image
            # does not ship.
            agent.set_run_env("DSH_PERMISSION_MODE", str(sandbox_mode))
        return patch_path
    finally:
        shutil.rmtree(staging, ignore_errors=True)


def _control_patch_yaml(
    target: PurePosixPath, config: dict[str, Any], control_ca: Path | None
) -> str:
    """The two-row Cordis patch that mounts the control stack."""
    rows = [
        "- insert:",
        "    - id: aeval-broker-transport",
        "      name: './dist/sandbox_entry.js'",
        "      config:",
        f"        controlConfigPath: '{(target / 'config.json').as_posix()}'",
        f"        jobTokenPath: '{config['jobTokenFile']}'",
        "    - id: aeval-eval-control",
        "      name: './dist/index.js'",
        "      config:",
    ]
    rows.extend(f"        {key}: {json.dumps(value)}" for key, value in config.items())
    return "\n".join(rows) + "\n"


async def _mint_owner_session(
    *, environment: Any, paths: TrialPaths, target: PurePosixPath,
    trial_id: str, config: dict[str, Any],
) -> None:
    """Create the owner-assigned session so the run can resume it (D15).

    The control plugin's model-identity injection is scoped to the id in
    the control config, and ``dsh --session-id`` only adopts an existing
    session — so the trial's own id must exist before the run starts. The
    stub writes it through the official persistence backend.
    """
    session_id = str(config["sessionId"])
    # The official session store lives at <DSH_HOME>/sessions (the layout the
    # session reader and host_session_root() use). ``paths.session_root`` is
    # descriptor-relative, NOT relative to dsh_home, so it must not be joined
    # here. TrialPaths carries POSIX strings, not Path objects.
    root = PurePosixPath(paths.dsh_home) / SESSIONS_DIRNAME
    envelope = json.dumps({
        "protocolVersion": 1, "requestId": f"mint-{trial_id}",
        "operation": "mint", "sessionId": session_id, "cwd": paths.sandbox_cwd,
    })
    stub = (target / "dist" / "session_stub.js").as_posix()
    exec_fn = getattr(environment, "exec", None)
    if not callable(exec_fn):
        raise BootstrapError("environment exposes no exec to mint the owner session")
    # The stub requires an EXISTING, absolute, link-free session root; DSH
    # creates it only on its first run, which happens after this bootstrap
    # (found on the real trial: ROOT_DENIED before any DSH start).
    prepared = await exec_fn(f"mkdir -p {shlex.quote(root.as_posix())}")
    prepared_code = getattr(prepared, "return_code", getattr(prepared, "exit_code", None))
    if prepared_code != 0:
        raise BootstrapError(
            f"could not prepare the sandbox session root {root.as_posix()} "
            f"(exit {prepared_code})"
        )
    command = (
        f"printf '%s' {shlex.quote(envelope)} | "
        f"node {shlex.quote(stub)} --root {shlex.quote(root.as_posix())} "
        "--compression zstd"
    )
    result = await exec_fn(command)
    code = getattr(result, "return_code", getattr(result, "exit_code", None))
    stdout = str(getattr(result, "stdout", "") or "")
    if code != 0 or '"ok":true' not in stdout.replace(" ", ""):
        raise BootstrapError(
            "the owner session could not be minted in the sandbox "
            f"(exit {code}): {stdout.strip()[:200]}"
        )


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
    agent: Any = None,
    control_dist: Path | None = None,
    control_ca: Path | None = None,
    reasoning_effort: str | None = None,
    limits: dict[str, int] | None = None,
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
    if state.infra_invalid_reasons:
        # The owner is the hard block: a trial already judged infra_invalid
        # (baseline/isolation/egress/evidence failure) must not receive a
        # model token. Recording the failure is not enough by itself.
        raise BootstrapError(
            "refusing to deploy a model token to a tainted trial: "
            + "; ".join(state.infra_invalid_reasons[:3])
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
            # the sandbox adapter compares these against the broker /info
            reasoning_effort=reasoning_effort,
            limits=dict(limits or {}),
        )
    if agent is not None and control_dist is not None and control_stack_of(type(agent)):
        driver = getattr(getattr(context.suite, "overlay", None), "driver", None)
        await deploy_control_stack(
            environment=environment, agent=agent, paths=paths, config=config,
            control_dist=Path(control_dist), control_ca=control_ca, trial_id=trial_id,
            sandbox_mode=getattr(driver, "sandbox_mode", None),
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
