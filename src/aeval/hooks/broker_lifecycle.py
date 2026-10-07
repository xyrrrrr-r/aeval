"""Per-trial model broker lifecycle inside the Harbor plugin (P0-4/5).

The operator prepares ONE broker spec (path in ``AEVAL_BROKER_JSON``)
describing the pinned upstream identity, limits, and a PINNED loopback
listen port. The plugin then, per trial:

1. composes the control config (``compose_control_config``) — the
   gateway URL is known because the port is pinned, so the config's
   digest exists BEFORE the broker starts;
2. starts a broker whose lease identity carries that exact digest;
3. stops the broker — with token cleanup by the bin — when the trial
   reaches any terminal state.

Fail-closed: a broker that cannot start marks the trial infra_invalid
BEFORE the model phase — the trial must not run with uncontrolled model
routing (P0-4: 插件激活失败必须阻止未受控运行). A broker that shuts
down uncleanly is recorded as an infra issue on the trial.

The pinned port is what makes the identity chain non-circular: an
ephemeral port would only be known after listening, but the control
config digest must be pinned into the broker config before the process
exists. Concurrent trials therefore need distinct pinned ports (one
spec per concurrent slot is the operator's contract).
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any, Mapping

if TYPE_CHECKING:  # pragma: no cover - typing only
    from aeval.control.flavors import ControlFlavor

from aeval.control.bootstrap import compose_control_config
from aeval.control.broker import (
    ModelBrokerProcess,
    broker_bin_candidates,
    find_node,
    write_broker_config,
)
from aeval.contracts import TrialPaths, control_config_digest
from aeval.hooks.context import EvaluationContext, TrialState

__all__ = [
    "BrokerSpecError",
    "BrokerSpec",
    "parse_broker_spec",
    "start_trial_broker",
    "stop_trial_broker",
    "trial_control_paths",
    "BROKER_SPEC_ENV",
    "BROKER_DIRNAME",
]

BROKER_SPEC_ENV = "AEVAL_BROKER_JSON"
BROKER_DIRNAME = "brokers"

# Fixed sandbox-side conventions (bundle writer contract): the DSH home
# layout Harbor syncs and the descriptor location inside it.
SANDBOX_DSH_HOME = "/logs/agent/dsh-home"
# Historical DSH convention, used when no live agent declares its own.
SANDBOX_SESSION_DIR = "dsh-home"
SANDBOX_BUNDLE_PATH = "/logs/agent/bundle_descriptor.json"


class BrokerSpecError(RuntimeError):
    """The operator's broker spec cannot be used as written."""


@dataclass(frozen=True)
class BrokerSpec:
    """Operator-prepared, run-wide broker inputs.

    Contains ONLY host-side facts: where the compiled bin is, the pinned
    upstream identity, the credential ENV NAME (never the value), the
    lease limits, and the pinned loopback port. Per-trial identity
    (run binding, trial/session ids, token path) is added by the plugin.
    """

    broker_js: Path
    listen_port: int
    upstream: Mapping[str, Any]
    identity: Mapping[str, Any]
    limits: Mapping[str, int]
    max_output_tokens: int
    listen_host: str = "127.0.0.1"
    listen_tls: Mapping[str, str] | None = None
    control_dist: Path | None = None
    control_ca: Path | None = None
    node_bin: str | None = None
    token_count: Mapping[str, Any] | None = None
    timeout_ms: int | None = None
    token_ttl_ms: int | None = None
    # D47: per-purpose decisions for advisory model calls ('compaction',
    # 'session-title') -> 'refuse' | 'allow'. Missing purposes keep the
    # broker's blanket default (refuse everything before dispatch).
    auxiliary_policy: Mapping[str, str] | None = None
    ready_timeout_sec: float = 30.0
    stop_timeout_sec: float = 10.0

    @property
    def gateway_url(self) -> str:
        scheme = "https" if self.listen_tls else "http"
        return f"{scheme}://{self.listen_host}:{self.listen_port}"


def parse_broker_spec(path_env: str | None = None) -> BrokerSpec | None:
    """Read the operator's broker spec; ``None`` when none is configured.

    A configured-but-broken spec is an ERROR, not a silent skip: the
    operator asked for controlled routing and must get it or nothing.
    """
    raw = path_env if path_env is not None else os.environ.get(BROKER_SPEC_ENV)
    if not raw:
        return None
    path = Path(raw)
    if not path.is_file():
        raise BrokerSpecError(f"broker spec {BROKER_SPEC_ENV}={raw} is not a file")
    try:
        data = json.loads(path.read_bytes())
    except (OSError, json.JSONDecodeError) as exc:
        raise BrokerSpecError(f"broker spec is unreadable: {exc}") from exc
    if not isinstance(data, dict):
        raise BrokerSpecError("broker spec must be a JSON object")

    unknown = set(data) - {
        "brokerJs", "nodeBin", "controlRoot", "upstream", "identity", "limits",
        "maxOutputTokens", "listenHost", "listenPort", "listenTls", "tokenCount",
        "timeoutMs", "tokenTtlMs", "readyTimeoutSec", "stopTimeoutSec",
        "controlDist", "controlCa", "auxiliaryPolicy",
    }
    if unknown:
        raise BrokerSpecError(f"broker spec has unknown keys: {sorted(unknown)}")

    broker_js = data.get("brokerJs")
    if not isinstance(broker_js, str) or not broker_js:
        if isinstance(data.get("controlRoot"), str):
            for candidate in broker_bin_candidates(Path(data["controlRoot"])):
                if candidate.is_file():
                    broker_js = str(candidate)
                    break
        if not broker_js:
            raise BrokerSpecError(
                "broker spec needs brokerJs (or controlRoot with a built dist)"
            )
    listen_port = data.get("listenPort")
    if not isinstance(listen_port, int) or isinstance(listen_port, bool) \
            or not 1 <= listen_port <= 65535:
        raise BrokerSpecError(
            "broker spec listenPort must be a pinned TCP port (1-65535): the "
            "control config digest must exist before the broker starts"
        )
    for key in ("upstream", "identity", "limits"):
        if not isinstance(data.get(key), dict):
            raise BrokerSpecError(f"broker spec {key} must be an object")
    upstream = dict(data["upstream"])
    if not isinstance(upstream.get("apiKeyEnv"), str) or not upstream["apiKeyEnv"]:
        raise BrokerSpecError(
            "broker spec upstream.apiKeyEnv is required (the env var NAME — "
            "the key itself never enters config files)"
        )
    # The upstream wire protocol selects which provider endpoint the broker
    # drives (chat_completions — the default, byte-identical to a spec written
    # before this key existed — or the OpenAI Responses API). Refuse anything
    # else here rather than at broker startup inside a running trial.
    if "protocol" in upstream and upstream["protocol"] not in ("chat_completions", "responses"):
        raise BrokerSpecError(
            "broker spec upstream.protocol must be 'chat_completions' or 'responses'"
        )
    max_output = data.get("maxOutputTokens")
    if not isinstance(max_output, int) or isinstance(max_output, bool) or max_output < 1:
        raise BrokerSpecError("broker spec maxOutputTokens must be a positive integer")

    listen_tls = data.get("listenTls")
    if listen_tls is not None:
        if not isinstance(listen_tls, dict) or not listen_tls.get("key") or not listen_tls.get("cert"):
            raise BrokerSpecError(
                "broker spec listenTls needs {key, cert} file paths (PEM paths, not PEM text)"
            )
    control_dist = data.get("controlDist")
    if control_dist is not None and not Path(str(control_dist)).is_dir():
        raise BrokerSpecError(f"broker spec controlDist is not a directory: {control_dist}")
    control_ca = data.get("controlCa")
    if control_ca is not None and not Path(str(control_ca)).is_file():
        raise BrokerSpecError(f"broker spec controlCa is not a file: {control_ca}")
    auxiliary_policy = data.get("auxiliaryPolicy")
    if auxiliary_policy is not None:
        if not isinstance(auxiliary_policy, dict):
            raise BrokerSpecError("broker spec auxiliaryPolicy must be an object")
        unknown_purposes = set(auxiliary_policy) - {"compaction", "session-title"}
        if unknown_purposes:
            raise BrokerSpecError(
                f"broker spec auxiliaryPolicy has unknown purposes: {sorted(unknown_purposes)}")
        for purpose, decision in auxiliary_policy.items():
            if not isinstance(decision, str) or decision not in ("refuse", "allow"):
                raise BrokerSpecError(
                    f"broker spec auxiliaryPolicy.{purpose} must be 'refuse' or 'allow'")
    return BrokerSpec(
        broker_js=Path(broker_js),
        listen_port=listen_port,
        listen_tls={k: str(v) for k, v in listen_tls.items()} if listen_tls else None,
        control_dist=Path(str(control_dist)) if control_dist else None,
        control_ca=Path(str(control_ca)) if control_ca else None,
        node_bin=str(data["nodeBin"]) if isinstance(data.get("nodeBin"), str) else None,
        upstream=upstream,
        identity=dict(data["identity"]),
        limits={k: v for k, v in dict(data["limits"]).items() if v is not None},
        max_output_tokens=max_output,
        listen_host=str(data.get("listenHost") or "127.0.0.1"),
        token_count=dict(data["tokenCount"]) if isinstance(data.get("tokenCount"), dict) else None,
        timeout_ms=data.get("timeoutMs") if isinstance(data.get("timeoutMs"), int) else None,
        token_ttl_ms=data.get("tokenTtlMs") if isinstance(data.get("tokenTtlMs"), int) else None,
        ready_timeout_sec=float(data.get("readyTimeoutSec") or 30.0),
        stop_timeout_sec=float(data.get("stopTimeoutSec") or 10.0),
        auxiliary_policy=dict(auxiliary_policy) if auxiliary_policy is not None else None,
    )


def trial_control_paths(
    state: TrialState, run_dir: Path, driver: Any = None, agent: Any = None
) -> TrialPaths:
    """The owner-side TrialPaths for one trial, from fixed conventions.

    ``sandbox_cwd`` is the suite's ``driver.workspace_dir`` (default
    ``/workspace``): the trial's session is minted there and the agent
    runs there, and the task's tests assume that same directory.

    The agent home and session artifact directory come from the adapter's
    declaration (``SANDBOX_HOME``/``SESSION_ARTIFACT_DIR``), falling back to the
    historical DSH conventions when the live agent is not available.
    """
    if state.trial_dir is None:
        raise BrokerSpecError("trial has no directory — paths cannot be derived")
    download_root = (state.trial_dir / "agent").relative_to(run_dir).as_posix()
    return TrialPaths(
        sandbox_cwd=str(getattr(driver, "workspace_dir", None) or "/workspace"),
        agent_home=str(getattr(agent, "SANDBOX_HOME", None) or SANDBOX_DSH_HOME),
        bundle_path=SANDBOX_BUNDLE_PATH,
        session_root=str(
            getattr(agent, "SESSION_ARTIFACT_DIR", None) or SANDBOX_SESSION_DIR
        ),
        download_root=download_root,
    )


def _config_flavor_for(agent: Any, lock: Any) -> ControlFlavor | None:
    """The control flavor that shapes this trial's config (G7).

    The live agent's declared stack when there is one; otherwise the adapter
    the runtime lock recorded, resolved through its declaration (the same
    discipline the evidence gate uses for the record owner) — never a
    hardcoded first agent. ``None`` means the trial's agent declares no
    control stack, so the config is the neutral identity alone. A DECLARED
    stack nothing registered is an error: the config would be shaped for a
    flavor that cannot exist.
    """
    from aeval.agents.contract import adapter_classes_recorded_in, control_stack_of
    from aeval.control.flavors import control_flavor

    if agent is not None:
        stack = control_stack_of(type(agent))
        if stack is None:
            return None
        flavor = control_flavor(stack)
        if flavor is None:
            raise BrokerSpecError(
                f"agent declares control stack {stack!r} which nothing "
                "registered — the control config cannot be shaped for it"
            )
        return flavor
    classes, _unresolved = adapter_classes_recorded_in(lock)
    if not classes:
        return None
    if len(classes) > 1:
        raise BrokerSpecError(
            "the runtime lock records several agents and no live handle is "
            "available — the control config cannot be shaped for one of them"
        )
    stack = control_stack_of(classes[0])
    if stack is None:
        return None
    flavor = control_flavor(stack)
    if flavor is None:
        raise BrokerSpecError(
            f"the recorded agent declares control stack {stack!r} which "
            "nothing registered — the control config cannot be shaped for it"
        )
    return flavor


def start_trial_broker(
    spec: BrokerSpec, context: EvaluationContext, state: TrialState
) -> tuple[ModelBrokerProcess, dict[str, Any]]:
    """Start one broker for one trial; attach handle + config to the state.

    The control config is composed FIRST (its digest pins the broker's
    lease identity), then the broker starts. Raises on any failure —
    the caller marks the trial infra_invalid; the trial must not
    proceed to the model phase.
    """
    if context.run_binding is None:
        raise BrokerSpecError("run has no trusted binding — no broker identity to pin")
    agent = (
        context.environments.agent(state.trial_id)
        if context.environments is not None
        else None
    )
    paths = trial_control_paths(
        state, context.run_dir,
        getattr(getattr(context.suite, "overlay", None), "driver", None),
        agent,
    )
    flavor = _config_flavor_for(agent, getattr(context, "runtime_lock", None))
    flavor_fields = (
        flavor.config_fields(
            paths=paths,
            # the served auxiliary policy must equal what /info reports, or
            # the sandbox adapter fails the lease identity check (D47)
            auxiliary_policy=dict(spec.auxiliary_policy) if spec.auxiliary_policy else None,
        )
        if flavor is not None and flavor.config_fields is not None else None
    )
    config = compose_control_config(
        run_binding=context.run_binding.model_dump(),
        trial_id=state.trial_id,
        session_id=state.session_id,
        gateway_url=spec.gateway_url,
        provider=str(spec.identity.get("provider", "")),
        model=str(spec.identity.get("model", "")),
        # the lease identity must match the control config field by field
        reasoning_effort=spec.identity.get("reasoningEffort"),
        limits=dict(spec.limits),
        flavor_fields=flavor_fields,
    )
    digest = control_config_digest(config)

    trial_dir = context.run_dir / BROKER_DIRNAME / state.trial_id
    config_path = write_broker_config(
        trial_dir / "broker.json",
        run=context.run_binding.model_dump(),
        trial_id=state.trial_id,
        session_id=state.session_id,
        config_digest=digest,
        identity=dict(spec.identity),
        limits=dict(spec.limits),
        max_output_tokens=spec.max_output_tokens,
        listen_host=spec.listen_host,
        listen_port=spec.listen_port,
        listen_tls=spec.listen_tls,
        token_out=trial_dir / "job-token",
        upstream=dict(spec.upstream),
        token_count=dict(spec.token_count) if spec.token_count else None,
        timeout_ms=spec.timeout_ms,
        token_ttl_ms=spec.token_ttl_ms,
        auxiliary_policy=dict(spec.auxiliary_policy) if spec.auxiliary_policy else None,
    )
    broker = ModelBrokerProcess(
        node_bin=spec.node_bin or find_node(),
        broker_js=spec.broker_js,
        config_path=config_path,
        ready_timeout_sec=spec.ready_timeout_sec,
        stop_timeout_sec=spec.stop_timeout_sec,
    )
    broker.start()
    if broker.url != spec.gateway_url:
        broker.stop("url_mismatch")
        raise BrokerSpecError(
            f"broker announced {broker.url} but the control config pins "
            f"{spec.gateway_url} — identity chain broken"
        )
    state.broker = broker
    state.control_config = config
    return broker, config


def note_broker_unexpected_exit(state: TrialState) -> str | None:
    """Describe a broker that died on its own; ``None`` when it is alive.

    A broker that exits without the owner stopping it closes its lease,
    and every later model call in the sandbox fails with
    ``AEVAL_LEASE_CLOSED`` for a reason the trial log does not explain
    (observed on the real chain). Only its stderr tail explains it.
    """
    broker = getattr(state, "broker", None)
    process = getattr(broker, "process", None)
    if process is None:
        return None
    code = process.poll()
    if code is None:
        return None
    tail = str(getattr(broker, "_stderr_tail", "") or "").strip()
    return (
        f"broker exited on its own with code {code} before the trial ended"
        + (f": {tail[-400:]}" if tail else " (no stderr captured)")
    )


def stop_trial_broker(state: TrialState, *, reason: str = "trial_terminal") -> None:
    """Stop the trial's broker; record unclean shutdowns as infra issues."""
    broker = state.broker
    if broker is None:
        return
    state.broker = None
    try:
        code = broker.stop(reason)
    except Exception as exc:  # never let teardown mask the trial outcome
        state.mark_infra_invalid(f"model broker teardown failed: {exc}")
        return
    if code not in (0, None):
        state.mark_infra_invalid(f"model broker exited with code {code} on stop")
    # Keep the broker's own account of any lease it closed: an unexplained
    # closed lease is otherwise impossible to attribute (real-chain).
    tail = str(getattr(broker, "_stderr_tail", "") or "").strip()
    if tail and tail not in state.broker_diagnostics:
        state.broker_diagnostics.append(tail)
        # Pair it with the owner's own teardown time: a broker stop earlier
        # than this line cannot have come from the normal trial-end stop, so
        # an external signal is distinguishable from our own teardown.
        state.broker_diagnostics.append(
            f"owner stopped broker at={datetime.now(timezone.utc).isoformat()} reason={reason}"
        )
