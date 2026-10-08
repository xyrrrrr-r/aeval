"""The dsh control-stack flavor: the DSH-specific half of its deployment.

The registry (``aeval.control.flavors``) is agent-agnostic by construction;
this module owns what is genuinely DSH's:

- the owner-assigned session mint (``dsh --session-id`` only adopts an
  existing session) in the official session-store layout;
- the adapter interface the plugin stack needs (a CLI install prefix to
  graft into, patch injection to mount it);
- the graft mechanism itself: WHERE inside the CLI's nested ``node_modules``
  the control tree lands, and the Cordis patch rows that mount it
  (moved out of ``aeval.control.bootstrap`` so the core bootstrap stays
  agent-neutral — a third graft-shaped flavor owns its own target layout
  and patch format instead of editing the core).

It lives in the adapter package — not core — precisely so the core never
imports a concrete adapter: importing ``aeval.agents.dsh`` IS the flavor's
registration.
"""

from __future__ import annotations

import json
import shlex
import shutil
import tempfile
from collections.abc import Awaitable, Callable
from pathlib import Path, PurePosixPath
from collections.abc import Mapping
from typing import TYPE_CHECKING, Any

from aeval.agents.dsh.agent import SESSIONS_DIRNAME
from aeval.control.bootstrap import BootstrapError
from aeval.control.flavors import ControlFlavor, register_control_flavor

if TYPE_CHECKING:  # pragma: no cover - typing only
    from aeval.contracts import TrialPaths

__all__ = [
    "mint_owner_session",
    "dsh_config_fields",
    "deploy_control_stack",
    "CONTROL_DIR_NAME",
]

# Where the control stack is deployed inside the sandbox: a SIBLING of the
# nested ``@deepseek-ai`` package directory, so node's ESM resolver finds
# the harness packages by walking up from the plugin files.
CONTROL_DIR_NAME = "aeval-control"


def dsh_config_fields(
    *, paths: TrialPaths, auxiliary_policy: dict[str, str] | None = None,
) -> dict[str, Any]:
    """The DSH control plugin's half of the composed control config.

    ``sessionRoot``/``bundlePath`` tell the in-sandbox plugin where the
    session store and the bundle descriptor live; ``refuseAuxiliaryCalls``
    pins the routing (no default provider, no title/auxiliary model calls —
    everything goes through the broker). ``auxiliary_policy`` overrides
    that blanket decision per purpose and must mirror what the broker's
    ``/info`` serves, or the sandbox adapter fails the lease identity check.

    ``ownerFinalize`` is always set: inside the sandbox no external party can
    reach ``evalControl``, so the harness process performs the owner's
    durable-then-finalize sequence itself; without it a completed run can only
    report stop_reason=infra_error (a real run).
    """
    fields: dict[str, Any] = {
        "sessionRoot": paths.session_root,
        "bundlePath": paths.bundle_path,
        "refuseAuxiliaryCalls": True,
        "ownerFinalize": True,
    }
    if auxiliary_policy:
        fields["auxiliaryPolicy"] = dict(auxiliary_policy)
    return fields


async def mint_owner_session(
    *, environment: Any, paths: TrialPaths, target: PurePosixPath,
    trial_id: str, config: dict[str, Any],
) -> None:
    """Create the owner-assigned session so the run can resume it.

    The control plugin's model-identity injection is scoped to the id in
    the control config, and ``dsh --session-id`` only adopts an existing
    session — so the trial's own id must exist before the run starts. The
    stub writes it through the official persistence backend.
    """
    session_id = str(config["sessionId"])
    # The official session store lives at <agent_home>/sessions (the layout
    # the session reader and host_session_root() use — for this adapter,
    # agent_home IS the DSH home). ``paths.session_root`` is
    # descriptor-relative, NOT relative to agent_home, so it must not be
    # joined here. TrialPaths carries POSIX strings, not Path objects.
    root = PurePosixPath(paths.agent_home) / SESSIONS_DIRNAME
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


#: The option namespace this flavor owns (``driver.control_options.dsh``).
#: Validated here, not in the framework: a knob only DSH reads stays a DSH fact.
_DSH_OPTION_KEYS = {"permission_mode"}
_DSH_PERMISSION_MODES = {"read-only", "workspace-write", "danger-full-access"}


def _dsh_run_env(control_options: Mapping[str, Any] | None) -> dict[str, str]:
    """Translate this flavor's suite options into the agent's run env.

    ``permission_mode`` is DSH's own confinement posture
    (``DSH_PERMISSION_MODE``): the CLI reads it in its composed profile and, for
    ``danger-full-access``, skips probing for a bwrap/Landlock runner the sealed
    pilot image does not ship — without it every shell call is refused. Unknown
    keys or values are refused rather than ignored.
    """
    options = dict(control_options or {})
    unknown = sorted(set(options) - _DSH_OPTION_KEYS)
    if unknown:
        raise BootstrapError(
            f"control_options.dsh declares {unknown}; known options: "
            f"{sorted(_DSH_OPTION_KEYS)}"
        )
    mode = options.get("permission_mode")
    if mode is None:
        return {}
    if mode not in _DSH_PERMISSION_MODES:
        raise BootstrapError(
            f"control_options.dsh.permission_mode must be one of "
            f"{sorted(_DSH_PERMISSION_MODES)}, got {mode!r}"
        )
    return {"DSH_PERMISSION_MODE": str(mode)}


async def deploy_control_stack(
    *,
    environment: Any,
    agent: Any,
    paths: TrialPaths,
    config: dict[str, Any],
    control_dist: Path,
    control_ca: Path | None,
    trial_id: str,
    run_env: Mapping[str, str] | None = None,
    mint_session: Callable[..., Awaitable[None]] | None = None,
) -> str:
    """Graft the in-sandbox control stack into the DSH CLI tree.

    The shape is fixed by environment verification on the real target host:

    - plugin files live inside the DSH install tree, because the CLI
      installs its dependencies NESTED (``@deepseek-ai/dsh/node_modules``)
      and node's ESM resolver only finds them for a sibling package;
    - the Cordis patch lists the transport entry first, then the control
      plugin (which injects ``evalBroker``);
    - a privately signed broker needs ``NODE_EXTRA_CA_CERTS`` in the run;
    - environment the deploying flavor's stack needs in the agent's run is
      passed as ``run_env`` — set by this flavor from its own
      ``control_options`` namespace (e.g. ``DSH_PERMISSION_MODE``).

    ``mint_session`` creates the owner-assigned session the run must resume
    (``dsh --session-id`` only adopts an existing one); the default is
    this module's own :func:`mint_owner_session`. Everything here — target
    layout, patch rows, the interface the DSH CLI exposes — is DSH
    knowledge; the core bootstrap contributes nothing but the dispatch.
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

        if mint_session is not None:
            # The flavor that owns the session-store layout mints the
            # owner-assigned session the run will resume.
            await mint_session(
                environment=environment, paths=paths, target=target,
                trial_id=trial_id, config=config,
            )

        patch_path = (target / "cordis.patch.yml").as_posix()
        # add_patch_file is DSH's own method (Harbor's BaseInstalledAgent has no
        # such API), so a non-DSH adapter used to die here with a bare
        # AttributeError. Silently skipping is not an option either: that would
        # drop the control stack without saying so — the flavor's ``requires``
        # names it so the gap is refused before anything is uploaded.
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
        for name, value in (run_env or {}).items():
            # The deploying flavor names these: the DSH CLI reads
            # DSH_PERMISSION_MODE in its composed profile (dsh-base's
            # cordis.patch.yml) and skips confinement for danger-full-access
            # instead of probing for a runner the image does not ship.
            agent.set_run_env(str(name), str(value))
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


async def _deploy_dsh_stack(
    *, environment: Any, context: Any, agent: Any, paths: TrialPaths,
    config: dict[str, Any], trial_id: str,
    control_dist: Path | None, control_ca: Path | None,
    facade_dist: Path | None, facade_root: PurePosixPath | None = None,
    control_options: Mapping[str, Any] | None = None,
) -> None:
    """The dsh flavor: graft the plugin tree into the CLI's own modules.

    Looked up through the module (not captured at import) so a monkeypatched
    ``deploy_control_stack`` in tests keeps intercepting the real dispatch.
    """
    if control_dist is None:
        raise BootstrapError(
            "the agent declares the dsh control stack but the broker spec "
            "carries no controlDist — the trial would run unmetered"
        )
    await deploy_control_stack(
        environment=environment, agent=agent, paths=paths, config=config,
        control_dist=Path(control_dist), control_ca=control_ca, trial_id=trial_id,
        run_env=_dsh_run_env(control_options),
        mint_session=mint_owner_session,
    )


register_control_flavor(ControlFlavor(
    name="dsh",
    deploy=_deploy_dsh_stack,
    # the plugin tree grafts into the CLI's install prefix and mounts through
    # a Cordis patch — an adapter without either cannot accept the stack
    requires=("cli_bin_dir", "add_patch_file"),
    # the DSH plugin consumes more than the neutral config: its half of the
    # composed control config is declared here, not hardcoded in the composer
    config_fields=dsh_config_fields,
))
