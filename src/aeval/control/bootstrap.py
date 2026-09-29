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

import asyncio
import json
import shlex
import shutil
import tempfile
import time
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING, Any

from aeval.contracts import (
    FACADE_PORT,
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
    "FACADE_SANDBOX_ROOT",
    "DEFAULT_FACADE_PORT",
    "facade_dist_candidates",
    "resolve_facade_dist",
    "compose_control_config",
    "deploy_control_stack",
    "deploy_generic_facade",
    "bootstrap_trial_control",
    "teardown_trial_control",
]


# Where the control stack is deployed inside the sandbox: a SIBLING of the
# nested ``@deepseek-ai`` package directory, so node's ESM resolver finds
# the harness packages by walking up from the plugin files.
CONTROL_DIR_NAME = "aeval-control"

# Where the GENERIC facade flavor is deployed: a self-contained tree the
# agent-neutral deployment owns end to end (upload → start → health). The
# task image must ship node; the port is fixed so the suite's task env can
# point the agent at http://127.0.0.1:<port>/v1 without per-trial plumbing.
FACADE_SANDBOX_ROOT = PurePosixPath("/opt/aeval-facade")
# The port is a shared contract, not a private default: the adapter points the
# agent at the same URL (contracts.FACADE_BASE_URL), so the two sides cannot
# drift apart.
DEFAULT_FACADE_PORT = FACADE_PORT

# The runtime closure ships with the facade: the neutral gateway-lease client
# imports the pinned @deepseek-ai packages, so the sandbox tree needs them
# beside the facade's own dist. Development-only trees never ship.
FACADE_NODE_MODULES_EXCLUDE = {"typescript", ".bin", ".package-lock.json", ".test-dist"}


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


def facade_dist_candidates(start: Path | None = None) -> list[Path]:
    """Where the built deepagent facade dist lives, most specific first.

    The operator override (``AEVAL_FACADE_DIST``) wins; otherwise the
    sibling ``deepagents-eval-control/dist`` of the aeval checkout — the
    same layout the lab uses for the neutral control package.
    """
    import os

    candidates: list[Path] = []
    override = os.environ.get("AEVAL_FACADE_DIST", "").strip()
    if override:
        candidates.append(Path(override))
    anchor = Path(start) if start is not None else Path(__file__).resolve()
    for parent in [anchor, *anchor.parents]:
        sibling = parent / "deepagents-eval-control" / "dist"
        if sibling not in candidates:
            candidates.append(sibling)
    return candidates


def resolve_facade_dist(start: Path | None = None) -> Path:
    """First facade dist candidate that actually holds the built entry."""
    for candidate in facade_dist_candidates(start):
        if (candidate / "facade_main.js").is_file():
            return candidate
    raise BootstrapError(
        "no built deepagent facade dist found — build deepagents-eval-control "
        "(npm run build) or point AEVAL_FACADE_DIST at its dist/ directory"
    )


def _facade_runtime_files(facade_dist: Path) -> list[tuple[Path, str]]:
    """The (host path, sandbox-relative path) pairs the facade needs to run.

    Flat dist ``.js`` files plus the pinned runtime ``node_modules`` closure;
    development-only trees (typescript, @types, .bin) never ship.
    """
    files: list[tuple[Path, str]] = []
    for source in sorted(facade_dist.glob("*.js")):
        files.append((source, f"dist/{source.name}"))
    package_json = facade_dist.parent / "package.json"
    if package_json.is_file():
        files.append((package_json, "package.json"))
    modules_root = facade_dist.parent / "node_modules"
    if modules_root.is_dir():
        for source in sorted(modules_root.rglob("*")):
            if not source.is_file() or source.is_symlink():
                continue
            relative = source.relative_to(modules_root)
            parts = relative.parts
            if any(part in FACADE_NODE_MODULES_EXCLUDE for part in parts):
                continue
            if "@types" in parts or parts[0].startswith("@types"):
                continue
            files.append((source, f"node_modules/{relative.as_posix()}"))
    return files


async def deploy_generic_facade(
    *,
    environment: Any,
    facade_dist: Path,
    gateway_url: str,
    token_file: str = SANDBOX_TOKEN_PATH.as_posix(),
    port: int = DEFAULT_FACADE_PORT,
    session_id: str | None = None,
    node_bin: str = "node",
    health_timeout_sec: float = 30.0,
    root: PurePosixPath | None = None,
    control_ca: Path | None = None,
) -> str:
    """Deploy and start the OpenAI-compatible facade inside the sandbox.

    The generic flavor the deepagent control stack uses (P2-5b): unlike the
    DSH flavor there is no plugin tree to graft into and no patch to apply —
    the agent process is launched by its own runner, so the ONLY mechanism
    fully in our control is: upload a self-contained tree, start it in the
    background, and health-check it before the run is allowed to continue.
    Returns the facade's base URL (``http://127.0.0.1:<port>``).
    """
    # The production root is /opt/aeval-facade; the override exists so the
    # whole flavor can be exercised on a host where /opt is not writable
    # (the verification harness) without changing the production path. A
    # string is accepted because the override travels through the same
    # PurePosixPath-shaped call sites (fakered environments pass strings).
    root = FACADE_SANDBOX_ROOT if root is None else PurePosixPath(root)
    upload = getattr(environment, "upload_file", None)
    exec_fn = getattr(environment, "exec", None)
    if not callable(upload) or not callable(exec_fn):
        raise BootstrapError("environment exposes no upload_file/exec for the facade")

    files = _facade_runtime_files(Path(facade_dist))
    shipped = {relative for _, relative in files}
    if "dist/facade_main.js" not in shipped:
        raise BootstrapError(f"facade dist has no built facade_main.js: {facade_dist}")
    # The neutral gateway-lease client imports the pinned @deepseek-ai packages
    # (the FacadeOptions source does too). Shipping the dist without its closure
    # uploads a tree that dies inside the sandbox with ERR_MODULE_NOT_FOUND —
    # found on example-lab, where the checkout had no node_modules — so the missing
    # closure is refused here, with the remedy, before anything is uploaded.
    if "node_modules/@deepseek-ai/dsh-llm/package.json" not in shipped:
        raise BootstrapError(
            f"the facade runtime closure is not installed beside {facade_dist} — "
            "run `npm ci` in deepagents-eval-control so the pinned packages ship "
            "with the dist (the sandbox has no registry access)"
        )

    # One tarball, one upload, one extract: the runtime closure is dozens of
    # files and per-file uploads would be both slow and partial-failure-prone.
    import tarfile
    import tempfile

    staging = Path(tempfile.mkdtemp(prefix="aeval-facade-"))
    try:
        await exec_fn(f"mkdir -p {shlex.quote(root.as_posix())}")
        tar_path = staging / "facade.tar.gz"
        with tarfile.open(tar_path, "w:gz") as tar:
            for source, relative in files:
                tar.add(source, arcname=relative)
            if control_ca is not None:
                # A privately signed broker listener is the production
                # topology (example-lab pins listenHost to a public address), so the
                # facade's outbound TLS needs the same trust anchor the DSH
                # control tree ships as ca.crt — without it every model call
                # dies on certificate verification inside the sandbox.
                tar.add(Path(control_ca), arcname="ca.crt")
        sandbox_tar = "/tmp/aeval-facade.tar.gz"
        await upload(str(tar_path), sandbox_tar)
        extracted = await exec_fn(
            f"tar -xzf {shlex.quote(sandbox_tar)} -C {shlex.quote(root.as_posix())}"
            f" && rm -f {shlex.quote(sandbox_tar)}"
        )
        code = getattr(extracted, "return_code", getattr(extracted, "exit_code", None))
        if code != 0:
            raise BootstrapError(f"facade tree could not be extracted in the sandbox (exit {code})")

        run_env = (
            f"AEVAL_GATEWAY_URL={shlex.quote(gateway_url)} "
            f"AEVAL_TRIAL_TOKEN_FILE={shlex.quote(token_file)} "
            f"AEVAL_FACADE_PORT={port} "
            + (
                f"NODE_EXTRA_CA_CERTS={shlex.quote((root / 'ca.crt').as_posix())} "
                if control_ca is not None
                else ""
            )
            + (f"AEVAL_FACADE_SESSION_ID={shlex.quote(session_id)} " if session_id else "")
        )
        log = "/tmp/aeval-facade.log"
        # ``setsid --fork`` both detaches (new session, so no SIGHUP when the
        # exec's shell goes away) and RETURNS: it forks the child and exits.
        # A trailing ``&`` does not — the exec then waits on a shell that holds
        # the command's pipes, which hung the real deployment on example-lab.
        started = await exec_fn(
            f"cd {shlex.quote(root.as_posix())} && {run_env}"
            f"setsid --fork {shlex.quote(node_bin)} dist/facade_main.js"
            f" >{log} 2>&1 < /dev/null"
        )
        code = getattr(started, "return_code", getattr(started, "exit_code", None))
        if code != 0:
            raise BootstrapError(f"facade could not be started in the sandbox (exit {code})")

        # Health gate: the run must not reach the agent with a facade that is
        # still starting (or already dead). Node is required anyway — the
        # facade itself runs on it — so the probe needs nothing extra.
        probe_js = (
            'fetch("http://127.0.0.1:' + str(port) + '/healthz")'
            ".then(r=>r.json()).then(j=>process.exit(j&&j.ok?0:1))"
            ".catch(()=>process.exit(1))"
        )
        probe = f"{shlex.quote(node_bin)} -e {shlex.quote(probe_js)}"
        deadline = time.monotonic() + health_timeout_sec
        last_output = ""
        while time.monotonic() < deadline:
            result = await exec_fn(probe)
            code = getattr(result, "return_code", getattr(result, "exit_code", None))
            if code == 0:
                return f"http://127.0.0.1:{port}"
            last_output = str(getattr(result, "stdout", "") or "")[:200]
            await asyncio.sleep(0.5)
        log_result = await exec_fn(f"cat {shlex.quote(log)} 2>/dev/null || true")
        log_text = str(getattr(log_result, "stdout", "") or "")[-400:]
        raise BootstrapError(
            f"facade did not become healthy on port {port} within "
            f"{health_timeout_sec:.0f}s — probe: {last_output!r}; log tail: {log_text!r}"
        )
    finally:
        shutil.rmtree(staging, ignore_errors=True)


async def _deploy_declared_stack(
    *,
    environment: Any,
    context: EvaluationContext,
    agent: Any,
    paths: TrialPaths,
    config: dict[str, Any],
    control_dist: Path | None,
    control_ca: Path | None,
    facade_dist: Path | None,
    trial_id: str,
    facade_root: PurePosixPath | None = None,
) -> None:
    """Deploy the control stack the adapter declared, by flavor.

    Two flavors exist and they are NOT interchangeable:

    - ``dsh``: the DSH plugin tree (transport + control plugin) is grafted
      into the CLI's own ``node_modules`` and mounted through a Cordis patch,
      because that is the only way a DSH-managed process loads it;
    - ``deepagent-facade``: the agent's runner starts deepagents itself, so
      there is nothing to graft — a self-contained facade tree is uploaded,
      started in the background and health-gated.

    A declared stack aeval cannot deploy is an error, never a silent skip:
    that would run the trial looking metered while nothing measures it.
    """
    stack = control_stack_of(type(agent))
    if stack is None:
        return
    if stack == "dsh":
        if control_dist is None:
            raise BootstrapError(
                "the agent declares the dsh control stack but the broker spec "
                "carries no controlDist — the trial would run unmetered"
            )
        driver = getattr(getattr(context.suite, "overlay", None), "driver", None)
        await deploy_control_stack(
            environment=environment, agent=agent, paths=paths, config=config,
            control_dist=Path(control_dist), control_ca=control_ca, trial_id=trial_id,
            sandbox_mode=getattr(driver, "sandbox_mode", None),
        )
        return
    if stack == "deepagent-facade":
        resolved = Path(facade_dist) if facade_dist is not None else resolve_facade_dist()
        # The lock covers exactly the bytes that get uploaded: re-fingerprint
        # against the recorded value so a dist edited after `aeval run` took
        # the lock is refused instead of silently running uncovered code.
        locked = getattr(getattr(context, "runtime_lock", None), "facade_dist", None)
        if locked is not None:
            from aeval.provenance import fingerprint_control_dist

            actual = fingerprint_control_dist(resolved)
            if actual.sha256 != locked.sha256:
                raise BootstrapError(
                    "facade dist changed after the run lock was taken: locked "
                    f"{str(locked.sha256)[:12]}… but {resolved} now fingerprints to "
                    f"{actual.sha256[:12]}… — refusing to deploy bytes the lock does not cover"
                )
        await deploy_generic_facade(
            environment=environment,
            facade_dist=resolved,
            gateway_url=str(config["gatewayUrl"]),
            token_file=str(config["jobTokenFile"]),
            root=facade_root,
            control_ca=control_ca,
        )
        return
    raise BootstrapError(
        f"agent declares control stack {stack!r}, which aeval cannot deploy "
        "(known flavors: 'dsh', 'deepagent-facade')"
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
    facade_dist: Path | None = None,
    facade_root: PurePosixPath | None = None,
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

    # The upload surface does not carry the host's 0600: e2b's files.write lands
    # the bytes as root with the daemon's default mode. The control stack
    # refuses a job token that is not "owned by me, mode 0600", so without this
    # the sandbox starts a facade that exits immediately — a failure only a real
    # sandbox exposes, because a local copy preserves the mode. Restore the
    # invariant through the same exec surface the deployment uses, and refuse
    # when there is none rather than hand the agent a stack that cannot start.
    exec_fn = getattr(environment, "exec", None)
    if not callable(exec_fn):
        raise BootstrapError(
            "environment exposes no exec — cannot pin the job token to 0600"
        )
    pinned = await exec_fn(f"chmod 600 {shlex.quote(job_token_file)}")
    code = getattr(pinned, "return_code", getattr(pinned, "exit_code", 0))
    if code not in (0, None):
        raise BootstrapError(
            f"could not pin the job token to 0600 in the sandbox (exit {code})"
        )

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
    if agent is not None:
        await _deploy_declared_stack(
            environment=environment,
            context=context,
            agent=agent,
            paths=paths,
            config=config,
            control_dist=control_dist,
            control_ca=control_ca,
            facade_dist=facade_dist,
            facade_root=facade_root,
            trial_id=trial_id,
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
