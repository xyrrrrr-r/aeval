"""deepAgent adapter: the deepagents-code CLI driven over ACP stdio (P2-5a).

Launch shape (source-backed facts in docs/TESTS/DEEPAGENTS-FACTS.md):

- Harbor's generic ``AcpAgent`` builds an in-sandbox launcher from an inline
  registry entry. This adapter's default entry selects the uvx distribution
  ``deepagents-code==0.1.78 --acp`` — the product coding agent exposing
  itself as an ACP server (deepagents ``libs/acp`` README, "dcode --acp".
  The package ships both the ``deepagents-code`` and ``dcode`` console
  scripts, so ``uvx deepagents-code==… --acp`` resolves without ``--from``).
  Nothing of ours runs inside the sandbox.
- Harbor's in-sandbox runner records every ``session/update`` event
  (``acp-events.jsonl``) and a summary (``acp-summary.json``); its
  ``populate_context_post_run`` converts the events into an ATIF trajectory
  (``logs_dir/trajectory.json``, harbor acp.py ``_write_trajectory``). This
  adapter reads that trajectory back as the canonical transcript: the
  "bridge" in ``native_session_via_bridge`` is Harbor's own ACP runner.

Honest declarations (each one is a refusal to overclaim):

- ``BUDGET_ENFORCEMENT = "none"``: our broker speaks ``aeval-model-broker/3``
  (GET /info, POST /stream) and is not an OpenAI-compatible endpoint, while
  dcode is. Metered runs need a dedicated control stack (P2-5b); until then
  a capped suite refusing this agent is the P1-3 budget gate working as
  designed.
- ``REQUIRED_OBSERVATIONS = ()``: dcode is Python (uvx); the Node observation
  is DSH-specific.
- no ``resume``: dcode persists sessions (``sessions.db``), but this adapter
  neither pins nor adopts a session, so the capability is not claimed.
- no ``CONTROL_STACK``: nothing DSH-specific is deployed for this agent.
- ``token_usage`` is ``partial``: Harbor fills usage from the ACP summary
  (``prompt_response.usage``) when the server provides it; live coverage of
  ``dcode --acp`` is not yet verified (DEEPAGENTS-FACTS.md 未核实项).
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
    CanonicalTranscript,
    CompletenessRecord,
    FieldCompleteness,
    StopReason,
    TranscriptCapability,
)

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
_SESSION_ARTIFACT_DIR = "deepagent-home"

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


class DeepgentRunError(RuntimeError):
    """The deepAgent trial could not be read back fail-closed."""


@dataclass(frozen=True)
class DeepgentTrialPaths:
    """One trial's deepAgent logs: sandbox view and synced host view."""

    environment_logs_dir: PurePosixPath
    logs_dir: Path

    @property
    def sandbox_home(self) -> PurePosixPath:
        return PurePosixPath(_SANDBOX_HOME)

    @property
    def trajectory_path(self) -> Path:
        return self.logs_dir / _TRAJECTORY_FILENAME

    @property
    def summary_path(self) -> Path:
        return self.logs_dir / _SUMMARY_FILENAME


def default_deepagent_registry_entry() -> dict[str, Any]:
    """Inline ACP registry entry: the pinned dcode CLI as an ACP server.

    Harbor's launcher runs ``<runner-venv>/bin/uvx <package> <args...>``
    (acp.py ``_build_launcher_script``), so ``package`` is the uvx tool spec
    and the version pin is part of it. Model routing (``OPENAI_BASE_URL``,
    ``OPENAI_API_KEY``, …) reaches the server through the distribution env —
    see ``model_env`` on the adapter.
    """
    return {
        "id": "deepagents-code",
        "name": "Deep Agents Code",
        "version": _DEEPAGENTS_CODE_VERSION,
        "description": (
            "deepagents-code (dcode) exposed as an ACP server (dcode --acp)"
        ),
        "distribution": {
            "uvx": {
                "package": f"deepagents-code=={_DEEPAGENTS_CODE_VERSION}",
                "args": ["--acp"],
                "env": {},
            },
        },
    }


def _with_distribution_env(
    entry: Mapping[str, Any], env: Mapping[str, str]
) -> dict[str, Any]:
    """Merge owner-supplied env (model routing) into the uvx launcher env."""
    merged = json.loads(json.dumps(dict(entry)))
    distribution = merged.get("distribution") or {}
    uvx = distribution.get("uvx") or {}
    combined = dict(uvx.get("env") or {})
    combined.update({str(key): str(value) for key, value in env.items()})
    uvx["env"] = combined
    distribution["uvx"] = uvx
    merged["distribution"] = distribution
    return merged


class DeepgentAgent(AcpAgent):
    """ACP-stdio adapter for the deepagents-code product agent (``dcode``)."""

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

    # Where the agent's own state lives inside the sandbox, and where its
    # session artifact would land in the bundle (P1-4: declared rather than
    # inheriting the DSH default). No CONTROL_STACK: the DSH control stack
    # (job token, session minting, cordis patch) must never deploy into
    # another agent, and nothing here needs it.
    SANDBOX_HOME = _SANDBOX_HOME
    SESSION_ARTIFACT_DIR = _SESSION_ARTIFACT_DIR

    # The collect slot this adapter's official session record belongs to
    # (aeval.agents.contract). deepagent's record is Harbor's ACP runner
    # summary — not a DSH session file, so it takes the generic slot.
    SESSION_RECORD_OUTPUT = "agent_session_record"

    ADAPTER_ID = "deepagent"
    ADAPTER_VERSION = "1"
    ADAPTER_MODE = "acp_stdio"
    TRANSCRIPT_CAPABILITY = TranscriptCapability(
        source="native_session_via_bridge",
        reader="harbor-acp-runner",
        capabilities=["atif_via_bridge", "token_usage"],
        fields_available={"events": "ok", "token_usage": "partial"},
    )
    # Honest: model traffic does NOT go through our broker (the broker is
    # not OpenAI-compatible; DEEPAGENTS-FACTS.md §6). A suite that caps
    # spend must refuse this agent until P2-5b's control stack exists.
    BUDGET_ENFORCEMENT = "none"
    WRITE_SURFACE = "ephemeral_overlay"

    DEEPAGENTS_CODE_VERSION = _DEEPAGENTS_CODE_VERSION

    def __init__(
        self,
        logs_dir: Path,
        *args: Any,
        registry_entry: Mapping[str, Any] | str | None = None,
        model_env: Mapping[str, str] | None = None,
        **kwargs: Any,
    ) -> None:
        entry = (
            dict(registry_entry)
            if registry_entry is not None
            else default_deepagent_registry_entry()
        )
        if model_env:
            entry = _with_distribution_env(entry, model_env)
        self._transcript: CanonicalTranscript | None = None
        self._summary: dict[str, Any] | None = None
        # registry_entry is AcpAgent's first named parameter, BEFORE its
        # *args: a positional logs_dir would land in it. Binding logs_dir by
        # keyword keeps it flowing into BaseInstalledAgent's own slot.
        super().__init__(
            registry_entry=entry, logs_dir=logs_dir, *args, **kwargs
        )

    @staticmethod
    def name() -> str:
        return "deepagent"

    def version(self) -> str | None:
        return _DEEPAGENTS_CODE_VERSION

    def paths(self) -> DeepgentTrialPaths:
        return DeepgentTrialPaths(
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
            raise DeepgentRunError(
                f"official session record {path} is missing — the ACP run or "
                "its log sync did not complete"
            )
        try:
            return path.read_bytes()
        except OSError as exc:
            raise DeepgentRunError(
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
            raise DeepgentRunError(
                f"ACP trajectory missing: {trajectory_path} (the runner writes "
                "it in populate_context_post_run; a missing file means the "
                "run or its log sync did not complete)"
            )
        try:
            atif = Trajectory.model_validate(
                json.loads(trajectory_path.read_text(encoding="utf-8"))
            )
        except (OSError, ValueError) as exc:
            raise DeepgentRunError(
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
                            "provides it; live coverage of dcode --acp is not "
                            "yet verified (DEEPAGENTS-FACTS.md 未核实项)"
                        ),
                    ),
                ]
            ),
        )
        return self._transcript
