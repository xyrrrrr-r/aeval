"""The OpenAI-protocol ACP adapter base: behavior for a whole agent family.

Any ACP-stdio CLI whose model traffic speaks an OpenAI wire — chat
completions or the responses protocol — is the same adapter once the facts
vary: which registry entry launches the CLI, which env spellings point it at
the in-sandbox facade, which protocol the facade must serve. Before this
module those facts were pinned inside ``DcodeAgent`` (G11), so a sibling
CLI could not be reused by declaration alone.

The split (AGENT-ABSTRACTION-2 stage 4.1):

- **Behavior lives here.** Constructor (registry entry + model routing env,
  merged into the launcher), read-back (the ACP runner's summary and
  trajectory), stop-reason mapping, session-record location — all facts of
  Harbor's generic ACP runner, not of any one CLI.
- **Facts live in a declaration.** A declaration whose ``import_path`` names
  :class:`OpenAiAcpAgent` is *materialized* into a complete adapter class by
  ``aeval.agents.declaration`` (see ``DECLARATION_DRIVEN``): the declaration's
  own fields become the class attributes every contract seam reads — specs,
  gates, conformance — so the runtime source of the agent's facts is still a
  class, built from the declaration alone. Zero adapter code.
- **A pinned subclass stays possible.** :class:`DcodeAgent` (dcode) keeps
  its pins in code and its mirror in ``agents/deepagent.yaml``; nothing about
  it changes.

Materialized classes are importable as ``aeval.agents.openai_acp:agent_<id>``
through this module's ``__getattr__`` (PEP 562): Harbor resolves agent
``import_path`` strings as ``module:attr``, and a lazily materialized module
attribute satisfies that in ANY process — the declaration file is the only
state, so composing and running can happen in different processes.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any

from harbor.agents.installed.acp import AcpAgent
from harbor.models.trajectories import Trajectory

from aeval.contracts import (
    FACADE_API_KEY_PLACEHOLDER,
    FACADE_BASE_URL,
    CanonicalTranscript,
    CompletenessRecord,
    FieldCompleteness,
    StopReason,
    TranscriptCapability,
)
from aeval.suite_models import SuiteError

# Mirror of AcpAgent's artifacts (harbor acp.py:377-378, :1622). Defined here
# so a Harbor rename fails loudly at read time instead of silently reading a
# stale path.
_SUMMARY_FILENAME = "acp-summary.json"
_TRAJECTORY_FILENAME = "trajectory.json"

# ACP stopReason (session/prompt response) → aeval stop reason. end_turn and
# refusal are the agent finishing its turn — a refusal is still a completed
# claim, not an infrastructure failure. max_tokens/max_steps are the agent
# exhausting its own cap. Everything else — and a missing stop reason —
# fails closed to infra_error, the same honesty rule as DSH's
# derive_stop_reason (exit codes prove nothing about the session).
_ACP_STOP_REASON_MAP: Mapping[str, StopReason] = {
    "end_turn": "agent_claimed_done",
    "refusal": "agent_claimed_done",
    "max_tokens": "budget_exhausted",
    "max_steps": "budget_exhausted",
}


class OpenAiAcpRunError(RuntimeError):
    """An OpenAI-protocol ACP trial could not be read back fail-closed."""


@dataclass(frozen=True)
class AcpTrialPaths:
    """One trial's ACP-runner logs: sandbox view and synced host view."""

    environment_logs_dir: PurePosixPath
    logs_dir: Path

    @property
    def trajectory_path(self) -> Path:
        return self.logs_dir / _TRAJECTORY_FILENAME

    @property
    def summary_path(self) -> Path:
        return self.logs_dir / _SUMMARY_FILENAME


def _lease_model_name(declared: str | None) -> str | None:
    """The bare model name the CLI must be told (``provider/name`` -> ``name``).

    Harbor states a model as ``provider/name`` while an HTTP request carries the
    bare name; the lease serves exactly one name and the facade refuses any
    other, so the bare form is what has to reach the CLI.
    """
    if not isinstance(declared, str) or not declared.strip():
        return None
    name = declared.strip()
    if "/" in name:
        name = name.split("/", 1)[1].strip()
    return name or None


def openai_facade_routing_env(routing: Any) -> dict[str, str]:
    """The env that points an OpenAI-protocol CLI at the in-sandbox facade.

    Part of the adapter's control-stack contract, not an operator errand: an
    operator who forgot it would get an agent talking to a vendor directly —
    metered in the manifest and unmetered in reality, which is exactly the
    overclaim the declaration exists to prevent. An explicit ``model_env``
    still wins (an unmetered smoke deliberately points elsewhere).

    The spellings come from the declared model routing
    (``MODEL_ROUTING`` on the adapter class — pinned, or materialized from a
    declaration): both base spellings are set when the routing names an
    ``alt_base_url`` (deepagents' ModelSpec reads ``OPENAI_API_BASE`` for some
    providers while Harbor's integration forwards ``OPENAI_BASE_URL``,
    DEEPAGENTS-FACTS.md §6). A gateway-native (or absent) routing injects
    nothing.
    """
    if routing is None or routing.agent_protocol == "gateway_native":
        return {}
    env = dict(routing.env)
    out = {env["base_url"]: FACADE_BASE_URL}
    if "alt_base_url" in env:
        out[env["alt_base_url"]] = FACADE_BASE_URL
    out[env["api_key"]] = FACADE_API_KEY_PLACEHOLDER
    return out


def _with_distribution_env(
    entry: Mapping[str, Any], env: Mapping[str, str]
) -> dict[str, Any]:
    """Merge owner-supplied env (model routing) into the launcher env.

    Whichever distribution kind the entry declares carries the env; the
    adapter must not care whether that is the local console script or a uvx
    launch, only that the routing reaches the process.
    """
    merged = json.loads(json.dumps(dict(entry)))
    distribution = merged.get("distribution") or {}
    kinds = [kind for kind in ("local", "binary", "uvx", "npx") if distribution.get(kind)]
    if len(kinds) != 1:
        raise SuiteError(
            "the ACP registry entry must declare exactly one ACP "
            f"distribution kind, found {sorted(kinds)}"
        )
    kind = kinds[0]
    target = dict(distribution[kind])
    combined = dict(target.get("env") or {})
    combined.update({str(key): str(value) for key, value in env.items()})
    target["env"] = combined
    distribution[kind] = target
    merged["distribution"] = distribution
    return merged


class OpenAiAcpAgent(AcpAgent):
    """Base adapter for an OpenAI-protocol ACP CLI (declaration-driven).

    The class carries the family's behavior and the facts that are true for
    EVERY member; the per-agent facts (identity, transcript capability, model
    routing spellings, sandbox layout, the registry entry that launches the
    CLI) come from the declaration that names this class — materialized into
    a complete subclass by ``aeval.agents.declaration`` — or from a pinned
    subclass like :class:`aeval.agents.deepagent.agent.DcodeAgent`.

    Used directly this class is not an agent: it is the family base, and the
    contract checks refuse it (that is on purpose — "which CLI" is a fact
    only a declaration or a pin can state).
    """

    #: Marker (aeval.agents.declaration): per-agent facts come from the
    #: declaration naming this class; materialize instead of compare.
    DECLARATION_DRIVEN = True
    #: Where materialized subclasses are importable (``module:agent_<id>``).
    SYNTH_MODULE = "aeval.agents.openai_acp"

    #: The error read-back raises — subclass pins its own name.
    RUN_ERROR = OpenAiAcpRunError

    # Family facts (a declaration may override any of them; the materialized
    # class carries whatever the declaration says, so the declaration↔class
    # check agrees by construction).
    ADAPTER_ID = "openai-acp"
    ADAPTER_VERSION = "1"
    ADAPTER_MODE = "acp_stdio"
    BUDGET_ENFORCEMENT = "gateway_lease"
    WRITE_SURFACE = "ephemeral_overlay"
    SERVER_SIDE_SESSION = "forbidden"
    TRANSCRIPT_CAPABILITY = TranscriptCapability(
        source="native_session_via_bridge",
        reader="harbor-acp-runner",
        capabilities=["atif_via_bridge", "token_usage"],
        fields_available={"events": "ok", "token_usage": "partial"},
    )
    PROVIDES = frozenset({"acp_stdio", "shell", "file_tools"})
    REQUIRED_OBSERVATIONS: tuple[str, ...] = ()

    # The in-sandbox control stack every member needs: the generic facade
    # deployment aeval knows how to upload, start and health-gate. Without it
    # the agent could not be metered at all.
    CONTROL_STACK = "deepagent-facade"

    # The generic facade only proxies model traffic: nothing inside the
    # sandbox ever sees an exit, so the owner observes what it can and writes
    # the descriptor itself.
    TERMINAL_DESCRIPTOR_OWNER = "host"

    # The official record, as one known sandbox file — per-agent (the
    # declaration names it), so the base leaves it unset.
    SANDBOX_SESSION_RECORD = None

    # The collect slot this family's official session record belongs to
    # (aeval.agents.contract): Harbor's ACP runner summary, not a DSH session
    # file, so it takes the generic slot.
    SESSION_RECORD_OUTPUT = "agent_session_record"

    def __init__(
        self,
        logs_dir: Path,
        *args: Any,
        registry_entry: Mapping[str, Any] | str | None = None,
        model_env: Mapping[str, str] | None = None,
        **kwargs: Any,
    ) -> None:
        # Which CLI this adapter launches is a DECLARED fact: the registry
        # entry arrives as a launch kwarg (the declaration's launch profile),
        # or from the subclass's own pin (DcodeAgent's dcode entry). The
        # base has no default to give — refusing here is the honest failure.
        entry = (
            dict(registry_entry)
            if registry_entry is not None
            else self.default_registry_entry(
                # Harbor passes the job's model as ``model_name``
                # (``provider/name``); the owner composes it into the agent
                # entry so this adapter never has to guess what the lease
                # serves.
                _lease_model_name(kwargs.get("model_name"))
            )
        )
        if entry is None:
            raise SuiteError(
                f"agent adapter {type(self).__name__} launches no CLI: no "
                "registry_entry kwarg (declare it in the agent's launch "
                "profile) and no default_registry_entry() pin — the "
                "declaration must say which ACP server to run"
            )
        # The facade routing is the declared control stack's other half; an
        # explicit model_env overrides it field by field.
        from aeval.agents.contract import model_routing_of

        routing: dict[str, str] = openai_facade_routing_env(
            model_routing_of(type(self))
        )
        routing.update({str(k): str(v) for k, v in (model_env or {}).items()})
        entry = _with_distribution_env(entry, routing)
        self._transcript: CanonicalTranscript | None = None
        self._summary: dict[str, Any] | None = None
        # registry_entry is AcpAgent's first named parameter, BEFORE its
        # *args: a positional logs_dir would land in it. Binding logs_dir by
        # keyword keeps it flowing into BaseInstalledAgent's own slot.
        super().__init__(
            registry_entry=entry, logs_dir=logs_dir, *args, **kwargs
        )

    def default_registry_entry(self, model: str | None = None) -> dict[str, Any] | None:
        """The pinned registry entry, when the subclass carries one.

        The base pins nothing: which CLI to launch is a per-agent fact, so a
        declaration-driven agent MUST pass ``registry_entry`` as a launch
        kwarg. A pinned subclass overrides this (DcodeAgent's dcode entry).
        """
        return None

    @staticmethod
    def name() -> str:
        # the base is never launched; the materialized class and every pinned
        # subclass carry their own identity
        return "openai-acp"

    def version(self) -> str | None:
        return getattr(type(self), "ADAPTER_VERSION", None)

    def paths(self) -> AcpTrialPaths:
        return AcpTrialPaths(
            environment_logs_dir=self.environment_logs_dir,
            logs_dir=self.logs_dir,
        )

    @property
    def agent_session_id(self) -> str | None:
        """The ACP session id of the last run, from the runner's summary."""
        summary = self._load_summary()
        if not isinstance(summary, dict):
            return None
        session = summary.get("session")
        if isinstance(session, dict):
            value = session.get("sessionId")
            if isinstance(value, str) and value:
                return value
        return None

    @staticmethod
    def session_id_from_record(record_bytes: bytes) -> str | None:
        """The session id inscribed in the runner's summary, or None.

        The ACP runtime mints its own session id; aeval's trial session id is
        the control-wire identity and never appears in the record. So the record
        is the only source of the identity the descriptor has to carry —
        declared here instead of parsed in the framework (P1-2b: observed
        identity belongs to the adapter that observes it).
        """
        try:
            summary = json.loads(record_bytes.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            return None
        if not isinstance(summary, dict):
            return None
        session = summary.get("session")
        if not isinstance(session, dict):
            return None
        session_id = session.get("sessionId")
        return session_id if isinstance(session_id, str) and session_id else None

    @classmethod
    def locate_session_record(cls, record_root: Any, session_id: str) -> Path | None:
        """``<record_root>/acp-summary.json`` when it records ``session_id``.

        The ACP runner writes exactly one summary per run at the root of the
        agent log directory — the same directory the descriptor is written
        to — so the record is found here.
        """
        record = Path(record_root) / _SUMMARY_FILENAME
        if not record.is_file():
            return None
        try:
            recorded = cls.session_id_from_record(record.read_bytes())
        except OSError:
            return None
        return record if recorded == session_id else None

    def read_session_record(self) -> bytes:
        """Contract member: Harbor's official session record for the last run.

        The ACP runner's ``acp-summary.json`` (session id, stop reason, token
        usage, instruction) is the official per-session record; the full event
        stream it summarizes reaches the evidence bundle as the canonical
        transcript (Harbor's ATIF conversion). Fails closed: a missing or
        unreadable summary means the run or its log sync did not complete.
        """
        path = self.logs_dir / _SUMMARY_FILENAME
        if not path.is_file():
            raise type(self).RUN_ERROR(
                f"official session record {path} is missing — the ACP run or "
                "its log sync did not complete"
            )
        try:
            return path.read_bytes()
        except OSError as exc:
            raise type(self).RUN_ERROR(
                f"official session record {path} could not be read: {exc}"
            ) from exc

    def _load_summary(self) -> dict[str, Any] | None:
        """Read the runner's summary once; a missing or broken file is None."""
        if self._summary is not None:
            return self._summary
        path = self.logs_dir / _SUMMARY_FILENAME
        if not path.is_file():
            return None
        try:
            self._summary = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        return self._summary

    def _stop_reason(self, summary: Any) -> StopReason:
        """Only a recorded ACP stop reason may report completion."""
        if not isinstance(summary, dict):
            return "infra_error"
        response = summary.get("prompt_response")
        if not isinstance(response, dict):
            return "infra_error"
        return _ACP_STOP_REASON_MAP.get(response.get("stopReason"), "infra_error")

    def read_trial_session(self) -> CanonicalTranscript:
        """Read Harbor's ATIF conversion of the recorded ACP session updates.

        Fails closed: a missing or unparsable trajectory means the ACP run or
        its log sync did not complete, and grading a truncated trajectory
        would be silently wrong. Read once and cached, so grading and
        reporting cannot disagree about the same trial.
        """
        if self._transcript is not None:
            return self._transcript
        trajectory_path = self.logs_dir / _TRAJECTORY_FILENAME
        if not trajectory_path.is_file():
            raise type(self).RUN_ERROR(
                f"ACP trajectory missing: {trajectory_path} (the runner writes "
                "it in populate_context_post_run; a missing file means the "
                "run or its log sync did not complete)"
            )
        try:
            atif = Trajectory.model_validate(
                json.loads(trajectory_path.read_text(encoding="utf-8"))
            )
        except (OSError, ValueError) as exc:
            raise type(self).RUN_ERROR(
                f"cannot parse ACP trajectory {trajectory_path}: {exc}"
            ) from exc
        summary = self._load_summary()
        self._transcript = CanonicalTranscript.build(
            atif=atif,
            stop_reason=self._stop_reason(summary),
            evidence_uri=None,
            completeness=CompletenessRecord(
                fields=[
                    FieldCompleteness(field="events", status="ok"),
                    FieldCompleteness(
                        field="token_usage",
                        status="partial",
                        reason=(
                            "usage comes from the ACP summary when the server "
                            "provides it; live coverage of the ACP server is "
                            "not yet verified"
                        ),
                    ),
                ]
            ),
        )
        return self._transcript


# --- declaration materialization (PEP 562) ----------------------------------
#
# ``aeval.agents.openai_acp:agent_<id>`` resolves lazily to the complete
# adapter class materialized from ``agents/<id>.yaml``. The declaration file
# is the only state, so the attribute materializes identically in any
# process that imports this module — composing and running may be separate
# commands.

_SYNTH_PREFIX = "agent_"
_SYNTHESIZED: dict[str, type] = {}


def __getattr__(name: str) -> Any:
    if not name.startswith(_SYNTH_PREFIX):
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    from aeval.agents.declaration import (
        declaration_slug,
        default_agents_root,
        discover_agent_declarations,
        materialize_declaration_adapter,
        resolve_agent_declaration,
    )

    slug = name[len(_SYNTH_PREFIX):]
    root = default_agents_root()
    for agent_id in discover_agent_declarations(root):
        if declaration_slug(agent_id) != slug:
            continue
        resolved = resolve_agent_declaration(
            root / f"{agent_id}.yaml", agents_root=root
        )
        return materialize_declaration_adapter(resolved.declaration)
    raise AttributeError(
        f"module {__name__!r} has no attribute {name!r}: no agent "
        f"declaration under {root} materializes it"
    )


__all__ = [
    "AcpTrialPaths",
    "OpenAiAcpAgent",
    "OpenAiAcpRunError",
    "openai_facade_routing_env",
]
