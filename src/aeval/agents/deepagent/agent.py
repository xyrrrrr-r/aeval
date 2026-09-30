"""deepAgent adapter: the deepagents-code CLI driven over ACP stdio (P2-5a).

This is the PINNED member of the OpenAI-protocol ACP family: the behavior
base (:class:`aeval.agents.openai_acp.OpenAiAcpAgent`) carries everything
true of any OpenAI-wire ACP CLI, and this subclass pins the dcode facts —
identity, version pin, model-routing spellings, sandbox layout, the default
registry entry. A sibling CLI needs none of this: a declaration naming the
base is materialized into a complete adapter with zero code
(AGENT-ABSTRACTION-2 G11, stage 4.1).

Launch shape (source-backed facts in docs/TESTS/DEEPAGENTS-FACTS.md):

- Harbor's generic ``AcpAgent`` builds an in-sandbox launcher from an inline
  registry entry. This adapter's default entry selects the pinned
  ``dcode --acp`` console script from the task image — the product coding
  agent exposing itself as an ACP server (deepagents ``libs/acp`` README,
  "dcode --acp"). Nothing of ours runs inside the sandbox.
- Harbor's in-sandbox runner records every ``session/update`` event
  (``acp-events.jsonl``) and a summary (``acp-summary.json``); its
  ``populate_context_post_run`` converts the events into an ATIF trajectory
  (``logs_dir/trajectory.json``, harbor acp.py ``_write_trajectory``). This
  adapter reads that trajectory back as the canonical transcript: the
  "bridge" in ``native_session_via_bridge`` is Harbor's own ACP runner.

Honest declarations (each one is a refusal to overclaim):

- ``BUDGET_ENFORCEMENT = "gateway_lease"``: dcode speaks OpenAI while our
  broker speaks ``aeval-model-broker/3`` (GET /info, POST /stream), so the two
  cannot talk directly — the in-sandbox facade (P2-5b, declared here as
  ``CONTROL_STACK = "deepagent-facade"``) translates, and every model call is
  then metered by the broker lease. The claim is only honest because the
  declaration and the deployment move together: ``MODEL_ROUTING`` below says
  dcode speaks ``openai_responses``, the deployment serves exactly that
  facade endpoint (``AEVAL_FACADE_PROTOCOLS``), and ``facade_routing_env()``
  points the agent at it — so the claim cannot be silently unfulfilled.
- ``REQUIRED_OBSERVATIONS = ()``: dcode is Python; the Node observation
  is DSH-specific.
- no ``resume``: dcode persists sessions (``sessions.db``), but this adapter
  neither pins nor adopts a session, so the capability is not claimed.
- ``token_usage`` is ``partial``: Harbor fills usage from the ACP summary
  (``prompt_response.usage``) when the server provides it; live coverage of
  ``dcode --acp`` is not yet verified (DEEPAGENTS-FACTS.md 未核实项).
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Any

from aeval.agents.openai_acp import (
    AcpTrialPaths,
    OpenAiAcpAgent,
    OpenAiAcpRunError,
    _lease_model_name,
    _SUMMARY_FILENAME,
    _TRAJECTORY_FILENAME,
    openai_facade_routing_env,
)
from aeval.contracts import TranscriptCapability

# Pinned product version. The registry entry below must name exactly this
# version so every launch is reproducible (DEEPAGENTS-FACTS.md: version pins
# matter for any mapper). Bump both together, never one alone.
_DEEPAGENTS_CODE_VERSION = "0.1.78"

# Where dcode keeps its state inside the sandbox (sessions.db, history.jsonl,
# conversation_history/; deepagents_code/_paths.py:848-850). Declared so the
# framework never hands this agent a DSH home it knows nothing about (P1-4);
# the state is not part of the sealed transcript (the trajectory comes from
# the host-side runner), so nothing downloads it in P2-5a.
_SANDBOX_HOME = "/root/.deepagents"
# The session artifact directory RELATIVE TO THE DESCRIPTOR's directory, i.e.
# to Harbor's agent log directory (the framework's "/logs/agent", which Harbor
# downloads to <trial_dir>/agent/). The ACP runner writes its summary at that
# root — there is no deepagent-home tree, and declaring one made every real run
# fail with "session_root does not exist" until a strong test caught it (the
# declared artifact dir must be where the adapter's record actually is).
_SESSION_ARTIFACT_DIR = "."

# Where the ACP runner's artifacts live INSIDE the sandbox (harbor
# acp.py:377-378 mirrors this as the agent environment's log dir).
_SANDBOX_LOGS_DIR = "/logs/agent"


class DcodeRunError(OpenAiAcpRunError):
    """The deepAgent trial could not be read back fail-closed."""


@dataclass(frozen=True)
class DcodeTrialPaths(AcpTrialPaths):
    """One trial's deepAgent logs: sandbox view and synced host view."""

    @property
    def sandbox_home(self) -> PurePosixPath:
        return PurePosixPath(_SANDBOX_HOME)


def default_deepagent_registry_entry(model: str | None = None) -> dict[str, Any]:
    """Inline ACP registry entry: the pinned dcode CLI as an ACP server.

    The distribution is ``local``: the task image ships the package pinned by
    :data:`_DEEPAGENTS_CODE_VERSION` and this entry runs its console script, so
    a PyPI fetch at agent setup never exists. Model routing
    (``OPENAI_BASE_URL``, ``OPENAI_API_KEY``, …) reaches the server through the
    distribution env — see ``model_env`` on the adapter.

    ``model`` is the lease's model name, appended as ``--model``. It cannot be
    baked into this entry (it is per-job) and it must not be omitted: dcode then
    falls back to its own codex-profile default, whose requests carry
    responses-only arguments (``reasoning``, builtin tools) and may name a
    model the lease does not serve — the facade would refuse that name anyway.
    example-lab: exactly how the first real run of the generic facade flavor died
    (P2-5b); the responses endpoint itself is now served per the declared
    ``MODEL_ROUTING`` (AGENT-ABSTRACTION-2 §4.5).
    """
    return {
        "id": "deepagents-code",
        "name": "Deep Agents Code",
        "version": _DEEPAGENTS_CODE_VERSION,
        "description": (
            "deepagents-code (dcode) exposed as an ACP server (dcode --acp)"
        ),
        "distribution": {
            # The pinned CLI is baked into the task image (see the deepagent
            # suites' Dockerfiles), because every suite here runs with
            # ``network_mode = "no-network"`` and a uvx distribution would need
            # PyPI at agent setup time. The version pin therefore lives in two
            # places on purpose, and a test keeps them equal.
            "local": {
                "cmd": "dcode",
                "args": ["--acp"] + (["--model", model] if model else []),
                "env": {},
            },
        },
    }


def facade_routing_env(routing: Any = None) -> dict[str, str]:
    """The env that points dcode at the in-sandbox facade.

    The family-generic behavior lives on the base
    (:func:`aeval.agents.openai_acp.openai_facade_routing_env`); this wrapper
    keeps dcode's no-argument default (the declared ``DcodeAgent`` routing)
    for the adapter's own call sites and tests. Part of the control-stack
    contract, not an operator errand: an operator who forgot it would get an
    agent talking to a vendor directly — metered in the manifest and unmetered
    in reality. An explicit ``model_env`` still wins (an unmetered smoke
    deliberately points elsewhere).
    """
    from aeval.agents.contract import model_routing_of

    return openai_facade_routing_env(
        model_routing_of(DcodeAgent) if routing is None else routing
    )


class DcodeAgent(OpenAiAcpAgent):
    """ACP-stdio adapter for the deepagents-code product agent (``dcode``).

    The pinned member of the family: every fact below is a dcode fact,
    mirrored in ``agents/deepagent.yaml`` and cross-checked against this class
    at load, so the declaration and the runtime can never disagree.
    """

    # Capabilities this adapter offers, matched against a suite's
    # driver.require before any trial starts (aeval.agents.contract).
    # Deliberately NOT claimed: sdk_jsonrpc (a DSH channel this agent has
    # nothing to serve) and resume (dcode persists sessions, but this
    # adapter neither pins nor adopts one).
    PROVIDES = frozenset({"acp_stdio", "shell", "file_tools"})

    # dcode is a Python package run through uvx; the Node observation is
    # DSH-specific. A lock that pins a runtime would still force its own
    # observation regardless of this tuple.
    REQUIRED_OBSERVATIONS: tuple[str, ...] = ()

    # A pinned adapter is NOT declaration-driven: every fact below lives in
    # THIS class (and agents/deepagent.yaml mirrors it, cross-checked at
    # load). Overriding the family base's marker keeps the runtime resolving
    # this class itself instead of materializing a declaration-driven twin —
    # which would drop the dcode registry entry the launch depends on.
    DECLARATION_DRIVEN = False

    # The in-sandbox control stack this adapter needs (P2-5b): NOT the DSH
    # flavor (no plugin tree, no cordis patch — dcode is not DSH-managed), but
    # the generic facade deployment aeval knows how to upload, start and
    # health-gate. Without it the agent could not be metered at all.
    CONTROL_STACK = "deepagent-facade"

    # Which wire dcode itself speaks for model traffic
    # (AGENT-ABSTRACTION-2 §4.1): the codex-profile provider posts the OpenAI
    # Responses protocol (``POST /v1/responses``; dcode model_config.py, and
    # DEEPAGENTS-FACTS.md records the same for the default profile), so the
    # facade must serve the responses endpoint and the broker's upstream
    # speaks ``responses`` to the provider's responses base (DeepSeek:
    # https://api.deepseek.com). The env spellings below are dcode facts; the
    # composition injects them with the facade's base URL and placeholder key.
    MODEL_ROUTING = {
        "agent_protocol": "openai_responses",
        "env": {
            "base_url": "OPENAI_BASE_URL",
            "alt_base_url": "OPENAI_API_BASE",
            "api_key": "OPENAI_API_KEY",
        },
    }

    # Where the agent's own state lives inside the sandbox, and where its
    # session artifact would land in the bundle (P1-4: declared rather than
    # inheriting the DSH default).
    SANDBOX_HOME = _SANDBOX_HOME
    SESSION_ARTIFACT_DIR = _SESSION_ARTIFACT_DIR

    # Which component can state the trial's terminal session outcome
    # (aeval.agents.contract.TERMINAL_DESCRIPTOR_OWNER_ATTR): the generic facade
    # only proxies model traffic — nothing inside the sandbox ever sees an exit
    # — so the owner observes what it can and writes the descriptor itself.
    TERMINAL_DESCRIPTOR_OWNER = "host"

    # The official record, as one known sandbox file: the owner reads it at
    # agent end (before Harbor downloads the logs) to learn the ACP session id
    # and to state whether the session completed.
    SANDBOX_SESSION_RECORD = f"{_SANDBOX_LOGS_DIR}/{_SUMMARY_FILENAME}"

    # The collect slot this adapter's official session record belongs to
    # (aeval.agents.contract). deepagent's record is Harbor's ACP runner
    # summary — not a DSH session file, so it takes the generic slot.
    SESSION_RECORD_OUTPUT = "agent_session_record"

    # Read-back fails closed under this name (the family raises
    # OpenAiAcpRunError; dcode's own name keeps the historical error type).
    RUN_ERROR = DcodeRunError

    ADAPTER_ID = "deepagent"
    ADAPTER_VERSION = "1"
    ADAPTER_MODE = "acp_stdio"
    TRANSCRIPT_CAPABILITY = TranscriptCapability(
        source="native_session_via_bridge",
        reader="harbor-acp-runner",
        capabilities=["atif_via_bridge", "token_usage"],
        fields_available={"events": "ok", "token_usage": "partial"},
    )
    # Model traffic goes through the gateway lease via the in-sandbox facade
    # (CONTROL_STACK above): the broker meters and caps every call, so a suite
    # that declares a spend cap is no longer refused — it is enforced.
    BUDGET_ENFORCEMENT = "gateway_lease"
    WRITE_SURFACE = "ephemeral_overlay"

    DEEPAGENTS_CODE_VERSION = _DEEPAGENTS_CODE_VERSION

    def default_registry_entry(self, model: str | None = None) -> dict[str, Any] | None:
        """The pinned dcode entry (the base's hook — see G11)."""
        return default_deepagent_registry_entry(model)

    @staticmethod
    def name() -> str:
        return "deepagent"

    def version(self) -> str | None:
        return _DEEPAGENTS_CODE_VERSION

    def paths(self) -> DcodeTrialPaths:
        return DcodeTrialPaths(
            environment_logs_dir=self.environment_logs_dir,
            logs_dir=self.logs_dir,
        )
