"""P2-5a: the deepAgent adapter (deepagents-code over ACP stdio).

Every assertion here is a lock on an honest declaration: the adapter must not
overclaim (no metering, no resume, no sdk_jsonrpc), the shipped declaration
must agree with the class, and the transcript read must fail closed.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from harbor.models.trajectories import Agent, Step, Trajectory

from aeval.contracts import (
    FACADE_API_KEY_PLACEHOLDER,
    FACADE_BASE_URL,
    FACADE_PORT,
)
from aeval.agents.contract import adapter_member_gap
from aeval.agents.declaration import (
    check_declaration_matches_adapter,
    resolve_agent_declaration,
)
from aeval.agents.deepagent.agent import (
    DeepgentAgent,
    DeepgentRunError,
    default_deepagent_registry_entry,
    facade_routing_env,
)

AGENTS_ROOT = Path(__file__).resolve().parents[3] / "agents"
DECLARATION_PATH = AGENTS_ROOT / "deepagent.yaml"


def _write_summary(logs_dir: Path, stop_reason: str = "end_turn") -> None:
    (logs_dir / "acp-summary.json").write_text(
        json.dumps(
            {
                "session": {"sessionId": "acme-session-1"},
                "prompt_response": {
                    "stopReason": stop_reason,
                    "usage": {"inputTokens": 10, "outputTokens": 5},
                },
                "instruction": "do the thing",
            }
        ),
        encoding="utf-8",
    )


def _write_trajectory(logs_dir: Path) -> None:
    trajectory = Trajectory(
        session_id="acme-session-1",
        agent=Agent(name="deepagent", version="0.1.78"),
        steps=[Step(step_id=1, source="agent", message="working on it")],
    )
    (logs_dir / "trajectory.json").write_text(
        json.dumps(trajectory.to_json_dict()), encoding="utf-8"
    )


def _agent(logs_dir: Path, **kwargs) -> DeepgentAgent:
    return DeepgentAgent(logs_dir, **kwargs)


def _distribution_env(agent: DeepgentAgent) -> dict[str, str]:
    """The launcher env the adapter handed to Harbor.

    AcpAgent parses whatever the adapter passed into its own
    ``AcpRegistryEntry``, so the assertion reads the model Harbor will
    actually launch from — not the dict the adapter built.
    """
    entry = getattr(agent, "_registry_entry", None)
    assert entry is not None, "the adapter exposed no registry entry"
    assert entry.distribution.local is not None, "the entry is not the local distribution"
    return dict(entry.distribution.local.env)


class TestContract:
    def test_members_complete(self) -> None:
        assert adapter_member_gap(DeepgentAgent) == []

    def test_shipped_declaration_matches_class(self) -> None:
        resolved = resolve_agent_declaration(DECLARATION_PATH)
        check_declaration_matches_adapter(resolved.declaration, DeepgentAgent)

    def test_provides_is_the_honest_set(self) -> None:
        # The honesty lock: nothing beyond what P2-5a actually delivers.
        # resume needs session pinning we do not do; sdk_jsonrpc is a DSH
        # channel this agent has nothing to serve.
        assert DeepgentAgent.PROVIDES == frozenset(
            {"acp_stdio", "shell", "file_tools"}
        )

    def test_metering_is_declared_with_the_stack_that_delivers_it(self) -> None:
        # The two declarations are one claim: the gateway lease can only be
        # honoured because the generic facade stack is deployed for this agent.
        assert DeepgentAgent.BUDGET_ENFORCEMENT == "gateway_lease"
        assert DeepgentAgent.CONTROL_STACK == "deepagent-facade"

    def test_facade_routing_env_points_the_agent_at_the_facade(self) -> None:
        env = facade_routing_env()
        assert env["OPENAI_BASE_URL"] == FACADE_BASE_URL
        assert env["OPENAI_API_BASE"] == FACADE_BASE_URL
        assert env["OPENAI_API_KEY"] == FACADE_API_KEY_PLACEHOLDER
        assert FACADE_BASE_URL == f"http://127.0.0.1:{FACADE_PORT}/v1"

    def test_the_adapter_injects_routing_and_lets_an_operator_override_it(
        self, tmp_path: Path
    ) -> None:
        default = _agent(tmp_path)
        env = _distribution_env(default)
        assert env["OPENAI_BASE_URL"] == FACADE_BASE_URL
        assert env["OPENAI_API_KEY"] == FACADE_API_KEY_PLACEHOLDER

        # an explicit model_env wins field by field (unmetered smoke runs)
        explicit = _agent(
            tmp_path,
            model_env={"OPENAI_BASE_URL": "https://vendor.example/v1",
                       "OPENAI_API_KEY": "sk-real"},
        )
        overridden = _distribution_env(explicit)
        assert overridden["OPENAI_BASE_URL"] == "https://vendor.example/v1"
        assert overridden["OPENAI_API_KEY"] == "sk-real"
        # the field the operator did not name still carries the facade routing
        assert overridden["OPENAI_API_BASE"] == FACADE_BASE_URL

    def test_default_registry_entry_is_pinned(self) -> None:
        entry = default_deepagent_registry_entry()
        # local, not uvx: the suites run with no network, so the CLI is baked
        # into the image and the entry only names its console script
        local = entry["distribution"]["local"]
        assert local["cmd"] == "dcode"
        assert local["args"] == ["--acp"]
        assert entry["version"] == DeepgentAgent.DEEPAGENTS_CODE_VERSION == "0.1.78"

    def test_identity(self, tmp_path: Path) -> None:
        agent = _agent(tmp_path)
        assert DeepgentAgent.name() == "deepagent"
        assert agent.version() == "0.1.78"


class TestReadTrialSession:
    def test_builds_canonical_transcript(self, tmp_path: Path) -> None:
        _write_trajectory(tmp_path)
        _write_summary(tmp_path)
        agent = _agent(tmp_path)

        transcript = agent.read_trial_session()

        assert transcript.atif.session_id == "acme-session-1"
        assert transcript.stop_reason == "agent_claimed_done"
        assert transcript.completeness is not None
        assert transcript.completeness.status_of("events") == "ok"
        assert transcript.completeness.status_of("token_usage") == "partial"
        assert transcript.completeness.worst_status == "partial"
        assert agent.agent_session_id == "acme-session-1"

    def test_read_is_cached_per_trial(self, tmp_path: Path) -> None:
        _write_trajectory(tmp_path)
        _write_summary(tmp_path)
        agent = _agent(tmp_path)

        assert agent.read_trial_session() is agent.read_trial_session()

    def test_missing_trajectory_fails_closed(self, tmp_path: Path) -> None:
        agent = _agent(tmp_path)

        with pytest.raises(DeepgentRunError):
            agent.read_trial_session()

    def test_unparsable_trajectory_fails_closed(self, tmp_path: Path) -> None:
        (tmp_path / "trajectory.json").write_text("{not json", encoding="utf-8")

        with pytest.raises(DeepgentRunError):
            _agent(tmp_path).read_trial_session()

    def test_no_summary_fails_closed_on_stop_reason(
        self, tmp_path: Path
    ) -> None:
        _write_trajectory(tmp_path)

        transcript = _agent(tmp_path).read_trial_session()

        # A missing summary cannot prove completion — infra_error, and no
        # session id to report either.
        assert transcript.stop_reason == "infra_error"

    def test_stop_reason_mapping(self, tmp_path: Path) -> None:
        _write_trajectory(tmp_path)

        def stop_reason(acp_reason: str) -> str:
            _write_summary(tmp_path, stop_reason=acp_reason)
            agent = _agent(tmp_path)
            return agent.read_trial_session().stop_reason

        assert stop_reason("end_turn") == "agent_claimed_done"
        assert stop_reason("refusal") == "agent_claimed_done"
        assert stop_reason("max_tokens") == "budget_exhausted"
        assert stop_reason("max_steps") == "budget_exhausted"
        # Anything the map does not know fails closed.
        assert stop_reason("mystery") == "infra_error"
