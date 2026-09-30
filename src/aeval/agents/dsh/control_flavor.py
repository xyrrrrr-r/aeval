"""The dsh control-stack flavor: the DSH-specific half of its deployment.

The registry (``aeval.control.flavors``) is agent-agnostic by construction;
this module owns what is genuinely DSH's:

- the owner-assigned session mint (``dsh --session-id`` only adopts an
  existing session, D15) in the official session-store layout, and
- the adapter interface the plugin stack needs (a CLI install prefix to
  graft into, patch injection to mount it).

It lives in the adapter package — not core — precisely so the core never
imports a concrete adapter: importing ``aeval.agents.dsh`` IS the flavor's
registration.
"""

from __future__ import annotations

import json
import shlex
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING, Any

from aeval.agents.dsh.agent import SESSIONS_DIRNAME
from aeval.control import bootstrap as _bootstrap
from aeval.control.bootstrap import BootstrapError
from aeval.control.flavors import ControlFlavor, register_control_flavor

if TYPE_CHECKING:  # pragma: no cover - typing only
    from aeval.contracts import TrialPaths

__all__ = ["mint_owner_session", "dsh_config_fields"]


def dsh_config_fields(
    *, paths: TrialPaths, auxiliary_policy: dict[str, str] | None = None,
) -> dict[str, Any]:
    """The DSH control plugin's half of the composed control config (G7).

    ``sessionRoot``/``bundlePath`` tell the in-sandbox plugin where the
    session store and the bundle descriptor live; ``refuseAuxiliaryCalls``
    pins the routing (no default provider, no title/auxiliary model calls —
    everything goes through the broker). ``auxiliary_policy`` (D47) overrides
    that blanket decision per purpose and must mirror what the broker's
    ``/info`` serves, or the sandbox adapter fails the lease identity check.

    ``ownerFinalize`` is always set: inside the sandbox no external party can
    reach ``evalControl``, so the harness process performs the owner's
    durable-then-finalize sequence itself; without it a completed run can only
    report stop_reason=infra_error (real chain).
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


async def _deploy_dsh_stack(
    *, environment: Any, context: Any, agent: Any, paths: TrialPaths,
    config: dict[str, Any], trial_id: str,
    control_dist: Path | None, control_ca: Path | None,
    facade_dist: Path | None, facade_root: PurePosixPath | None = None,
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
    driver = getattr(getattr(context.suite, "overlay", None), "driver", None)
    await _bootstrap.deploy_control_stack(
        environment=environment, agent=agent, paths=paths, config=config,
        control_dist=Path(control_dist), control_ca=control_ca, trial_id=trial_id,
        sandbox_mode=getattr(driver, "sandbox_mode", None),
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
