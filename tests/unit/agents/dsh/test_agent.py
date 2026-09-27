"""Run-chain tests for DshAgent: install pin, headless launch, collection.

The environment is a recording fake: these tests pin the *contract* the
adapter has with Harbor and with the official headless CLI, and never call
a model.
"""

from __future__ import annotations

from pathlib import Path, PurePosixPath
from typing import Any

import pytest
from harbor.environments.base import ExecResult
from harbor.models.agent.context import AgentContext
from harbor.models.trajectories import Agent as AtifAgent, FinalMetrics, Step, Trajectory

from aeval.agents.dsh.agent import (
    DshAgent,
    DshRunError,
    DshTrialPaths,
    build_headless_command,
    host_session_root,
    resolve_session_reader,
    session_id_from_stream,
    session_reader_candidates,
)
from aeval.agents.dsh.bridge import DshReaderResponse
from aeval.contracts import CanonicalTranscript
from aeval.provenance import OFFICIAL_DSH_TAG

LOCKED_VERSION = OFFICIAL_DSH_TAG.removeprefix("dsh-v")
SESSION_ID = "0f6f1a2b-3c4d-5e6f-7a8b-9c0d1e2f3a4b"
PINNED_SESSION_ID = "session-aeval-trial-0001"
RUN_STREAM = (
    f'{{"type":"session","sessionId":"{SESSION_ID}","cwd":"/home/node/app"}}\n'
    '{"type":"final","text":"done"}\n'
)


class RecordingEnvironment:
    """Stands in for a Harbor environment: records calls, replays results."""

    default_user = "agent"

    def __init__(self, results: dict[str, ExecResult] | None = None) -> None:
        self.commands: list[dict[str, Any]] = []
        self._results = results or {}

    async def exec(
        self,
        command: str,
        cwd: str | None = None,
        env: dict[str, str] | None = None,
        timeout_sec: int | None = None,
        user: str | int | None = None,
    ) -> ExecResult:
        self.commands.append(
            {
                "command": command,
                "cwd": cwd,
                "env": env,
                "timeout_sec": timeout_sec,
                "user": user,
            }
        )
        for needle, result in self._results.items():
            if needle in command:
                return result
        return ExecResult(stdout="", stderr="", return_code=0)

    def matching(self, needle: str) -> dict[str, Any]:
        hits = [call for call in self.commands if needle in call["command"]]
        assert len(hits) == 1, f"expected one command containing {needle!r}, got {len(hits)}"
        return hits[0]


def make_agent(tmp_path: Path, **kwargs: Any) -> DshAgent:
    return DshAgent(
        logs_dir=tmp_path / "agent-logs",
        environment_logs_dir=PurePosixPath("/logs/agent"),
        **kwargs,
    )


def test_agent_satisfies_the_harbor_agent_interface(tmp_path: Path) -> None:
    agent = make_agent(tmp_path)
    assert DshAgent.name() == "dsh"
    assert not agent.__abstractmethods__
    assert agent.capabilities.atif is False
    assert agent.capabilities.windows is False, "only Linux containers are verified"
    assert agent.official_tag == OFFICIAL_DSH_TAG
    assert agent.experimental is True


def test_trial_paths_isolate_the_harness_home_under_the_trial_logs(tmp_path: Path) -> None:
    paths = DshTrialPaths(PurePosixPath("/logs/agent"), tmp_path)
    assert paths.dsh_home == PurePosixPath("/logs/agent/dsh-home")
    assert paths.env() == {"DSH_HOME": "/logs/agent/dsh-home"}
    assert paths.container_session_root == PurePosixPath("/logs/agent/dsh-home/sessions")
    # The harness home is per trial, so two trials can never share a session
    # store even when they reuse the same image and cwd.
    assert paths == DshTrialPaths(PurePosixPath("/logs/agent"), tmp_path)
    # Harbor syncs the agent logs dir verbatim, so the host copy keeps the
    # same layout: that is the only root the reader may be pointed at.
    assert host_session_root(tmp_path) == tmp_path / "dsh-home" / "sessions"


def test_headless_command_feeds_the_task_on_stdin_not_argv() -> None:
    command = build_headless_command(
        stream_path=PurePosixPath("/logs/agent/dsh-run.jsonl"),
        task_env_var="AEVAL_DSH_TASK_deadbeef",
        patch_files=["/tmp/my overlay.yml", "/tmp/second.yml"],
    )
    # The launcher owns --profile and --patch and stops recognising flags at
    # the app's --json, so a patch after it never reaches the loader.
    assert " | dsh --profile headless --patch '/tmp/my overlay.yml' " \
        "--patch /tmp/second.yml --json | tee " in command
    assert "printf '%s' \"${AEVAL_DSH_TASK_deadbeef}\"" in command
    assert command.endswith("| tee /logs/agent/dsh-run.jsonl")


def test_session_id_comes_from_the_opening_stream_event() -> None:
    assert session_id_from_stream(RUN_STREAM) == SESSION_ID


def test_launcher_noise_before_the_session_event_is_ignored() -> None:
    stdout = "booting profile\nnot-json {\n" + RUN_STREAM
    assert session_id_from_stream(stdout) == SESSION_ID


def test_a_stream_without_the_session_announcement_fails_closed() -> None:
    with pytest.raises(DshRunError, match="no opening session event"):
        session_id_from_stream('{"type":"final","text":"done"}\n')


@pytest.mark.parametrize(
    "stdout",
    [
        '{"type":"session"}\n',
        '{"type":"session","sessionId":""}\n',
        '{"type":"session","sessionId":42}\n',
    ],
)
def test_an_unusable_session_announcement_fails_closed(stdout: str) -> None:
    with pytest.raises(DshRunError, match="no sessionId"):
        session_id_from_stream(stdout)


async def test_run_boots_headless_once_with_the_isolated_home(tmp_path: Path) -> None:
    agent = make_agent(tmp_path)
    environment = RecordingEnvironment(
        results={"dsh --profile headless": ExecResult(stdout=RUN_STREAM, return_code=0)}
    )
    context = AgentContext()

    await agent.run("secret instruction", environment, context)  # type: ignore[arg-type]

    call = environment.matching("dsh --profile headless")
    assert "secret instruction" not in call["command"]
    assert call["user"] is None, "the agent user is set by the orchestrator"
    assert call["env"] is not None and call["env"]["DSH_HOME"] == "/logs/agent/dsh-home"
    task_vars = [key for key in call["env"] if key.startswith("AEVAL_DSH_TASK_")]
    assert len(task_vars) == 1
    assert call["env"][task_vars[0]] == "secret instruction"
    assert agent.session_id == SESSION_ID
    # P0-5 fix: run() must NOT write context.metadata — a non-empty
    # context makes Harbor skip populate_context_post_run entirely.
    assert context.metadata is None


async def test_a_second_run_forgets_the_previous_session(tmp_path: Path) -> None:
    agent = make_agent(tmp_path)
    environment = RecordingEnvironment(
        results={"dsh --profile headless": ExecResult(stdout=RUN_STREAM, return_code=0)}
    )

    await agent.run("first", environment, AgentContext())  # type: ignore[arg-type]
    assert agent.session_id == SESSION_ID
    agent._transcript = CanonicalTranscript(atif=_trajectory(), stop_reason="agent_claimed_done")

    failed = RecordingEnvironment(
        results={"dsh --profile headless": ExecResult(stdout="no events", return_code=0)}
    )
    with pytest.raises(DshRunError):
        await agent.run("second", failed, AgentContext())  # type: ignore[arg-type]
    # A run that never announced a session must not inherit the old trial's
    # identity or its cached transcript.
    assert agent.session_id is None
    assert agent._transcript is None


async def test_a_pinned_trial_adopts_the_session_it_named(tmp_path: Path) -> None:
    agent = make_agent(tmp_path, session_id=PINNED_SESSION_ID)
    environment = RecordingEnvironment(
        results={
            "dsh --profile headless": ExecResult(
                stdout=RUN_STREAM.replace(SESSION_ID, PINNED_SESSION_ID), return_code=0
            )
        }
    )

    await agent.run("task", environment, AgentContext())  # type: ignore[arg-type]

    call = environment.matching("dsh --profile headless")
    # --session-id belongs to the headless program, so it follows --json.
    assert "--json --session-id " + PINNED_SESSION_ID in call["command"]
    assert agent.session_id == PINNED_SESSION_ID


async def test_a_pinned_trial_refuses_to_collect_another_session(tmp_path: Path) -> None:
    agent = make_agent(tmp_path, session_id=PINNED_SESSION_ID)
    environment = RecordingEnvironment(
        results={"dsh --profile headless": ExecResult(stdout=RUN_STREAM, return_code=0)}
    )

    with pytest.raises(DshRunError, match="different session"):
        await agent.run("task", environment, AgentContext())  # type: ignore[arg-type]
    # The run drove a session nobody named; collecting it would grade the
    # wrong trajectory.
    assert agent.session_id is None


@pytest.mark.parametrize(
    "candidate",
    ["", " two words", "../elsewhere", "quote'd", "a;rm -rf /", "a" * 129, "ünicode"],
)
def test_a_pinned_session_id_must_survive_the_shell_and_the_disk(
    tmp_path: Path, candidate: str
) -> None:
    with pytest.raises(DshRunError, match="invalid DSH trial session id"):
        make_agent(tmp_path, session_id=candidate)


async def test_install_refuses_any_drift_from_the_frozen_slice(tmp_path: Path) -> None:
    agent = make_agent(tmp_path)
    environment = RecordingEnvironment(
        results={"dsh --version": ExecResult(stdout="0.1.6\n", return_code=0)}
    )

    with pytest.raises(DshRunError, match="version drift"):
        await agent.install(environment)  # type: ignore[arg-type]

    install = environment.matching("npm install --global")
    assert f"@deepseek-ai/dsh@{LOCKED_VERSION}" in install["command"]
    assert install["user"] == "root"


async def test_install_records_the_locked_version(tmp_path: Path) -> None:
    agent = make_agent(tmp_path)
    environment = RecordingEnvironment(
        results={"dsh --version": ExecResult(stdout=f"{LOCKED_VERSION}\n", return_code=0)}
    )

    await agent.install(environment)  # type: ignore[arg-type]

    assert agent.version() == LOCKED_VERSION


def test_reader_resolution_prefers_the_explicit_override(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    mine = tmp_path / "reader.js"
    mine.write_text("// reader\n")
    monkeypatch.setenv("AEVAL_DSH_SESSION_READER", str(tmp_path / "from-env.js"))
    assert resolve_session_reader(mine) == mine


def test_a_configured_reader_must_exist(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    missing = tmp_path / "not-built-yet.js"
    monkeypatch.setenv("AEVAL_DSH_SESSION_READER", str(missing))
    # Falling back to some other script would mean the trial's session was
    # read by an unknown tool, so this fails instead.
    with pytest.raises(DshRunError, match="configured DSH session reader not found"):
        resolve_session_reader()


def test_reader_fallback_probes_the_configured_root_then_the_sibling_checkout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("AEVAL_DSH_SESSION_READER", raising=False)
    monkeypatch.setenv("AEVAL_DSH_CONTROL_ROOT", str(tmp_path / "dsh-eval-control"))
    candidates = session_reader_candidates()
    assert candidates[0] == tmp_path / "dsh-eval-control" / "dist" / "session_reader.js"
    assert any(
        candidate.parts[-3:-1] == ("dsh-eval-control", "dist") for candidate in candidates[1:]
    ), candidates


def test_missing_reader_names_the_environment_hint(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("AEVAL_DSH_SESSION_READER", raising=False)
    monkeypatch.delenv("AEVAL_DSH_CONTROL_ROOT", raising=False)
    monkeypatch.setattr(
        "aeval.agents.dsh.agent.session_reader_candidates",
        lambda: [tmp_path / "absent" / "session_reader.js"],
    )
    with pytest.raises(DshRunError, match="AEVAL_DSH_SESSION_READER"):
        resolve_session_reader()


def _trajectory(**metrics: Any) -> Trajectory:
    return Trajectory(
        agent=AtifAgent(name="dsh", version=LOCKED_VERSION),
        session_id=SESSION_ID,
        steps=[Step(step_id=1, source="agent", message="done")],
        final_metrics=FinalMetrics(**metrics) if metrics else None,
    )


def _response() -> DshReaderResponse:
    return DshReaderResponse(
        request_id="req-1",
        header={"version": 4, "id": SESSION_ID, "createdAt": 1_730_000_000_000, "isSeeded": False},
        inherited_event_count=0,
        event_state="shared-frozen",
        events=[],
    )


def test_collection_reads_the_official_root_with_the_announced_session_id(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    agent = make_agent(tmp_path)
    agent._session_id = SESSION_ID
    session_root = host_session_root(tmp_path / "agent-logs")
    session_dir = session_root / SESSION_ID
    session_dir.mkdir(parents=True)
    (session_dir / "session.v4.jsonl.zstd").write_bytes(b"official record")
    captured: list[Any] = []

    def fake_read(request: Any) -> DshReaderResponse:
        captured.append(request)
        return _response()

    monkeypatch.setattr("aeval.agents.dsh.agent.read_dsh_session_via_bridge", fake_read)
    monkeypatch.setattr(
        "aeval.agents.dsh.agent.convert_dsh_read_to_atif", lambda response: _trajectory()
    )

    transcript = agent.read_trial_session()

    request = captured[0]
    assert request.session_id == SESSION_ID
    assert request.source_root == tmp_path / "agent-logs" / "dsh-home" / "sessions"
    # The trial's own logs dir is the containment boundary: the reader may
    # not walk into a sibling trial.
    assert request.allowed_base == tmp_path / "agent-logs"
    assert transcript.atif.session_id == SESSION_ID
    assert agent.read_trial_session() is transcript, "one session read per trial"


def test_collection_without_a_synced_session_root_fails_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    agent = make_agent(tmp_path)
    agent._session_id = SESSION_ID
    monkeypatch.setattr(
        "aeval.agents.dsh.agent.resolve_session_reader", lambda explicit=None: Path("reader.js")
    )
    with pytest.raises(DshRunError, match="session root missing"):
        agent.read_trial_session()


def test_collection_without_a_run_fails_closed(tmp_path: Path) -> None:
    with pytest.raises(DshRunError, match="must complete first"):
        make_agent(tmp_path).read_trial_session()


def test_context_usage_comes_from_the_session_not_the_stream(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    agent = make_agent(tmp_path)
    agent._transcript = CanonicalTranscript(
        atif=_trajectory(
            total_prompt_tokens=300, total_completion_tokens=20, total_cached_tokens=100
        ),
        stop_reason="agent_claimed_done",
    )
    context = AgentContext()

    agent.populate_context_post_run(context)

    # Harbor counts cached tokens inside n_input_tokens, which is exactly what
    # the mapper's prompt subtotal already is.
    assert (context.n_input_tokens, context.n_output_tokens, context.n_cache_tokens) == (
        300,
        20,
        100,
    )
    assert context.cost_usd is None, "cost is never inferred from tokens"


def test_partial_usage_leaves_the_context_empty(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    agent = make_agent(tmp_path)
    agent._transcript = CanonicalTranscript(
        atif=_trajectory(total_prompt_tokens=None, total_completion_tokens=20),
        stop_reason="infra_error",
    )
    context = AgentContext()

    agent.populate_context_post_run(context)

    assert context.n_input_tokens is None
    assert context.n_output_tokens is None


def test_post_run_populates_metadata_and_usage(tmp_path, monkeypatch):
    """P0-5 fix: metadata now lands in populate_context_post_run, so
    Harbor's empty-context backfill callback actually runs."""
    from harbor.models.agent.context import AgentContext

    agent = make_agent(tmp_path)
    agent._session_id = SESSION_ID
    session_dir = host_session_root(tmp_path / "agent-logs") / SESSION_ID
    session_dir.mkdir(parents=True)
    (session_dir / "session.v4.jsonl.zstd").write_bytes(b"official record")

    monkeypatch.setattr(
        "aeval.agents.dsh.agent.read_dsh_session_via_bridge", lambda request: _response()
    )
    trajectory = _trajectory()
    monkeypatch.setattr(
        "aeval.agents.dsh.agent.convert_dsh_read_to_atif", lambda response: trajectory
    )

    context = AgentContext()
    agent.populate_context_post_run(context)
    assert context.metadata == {
        "dsh_session_id": SESSION_ID,
        "dsh_run_stream": "/logs/agent/dsh-run.jsonl",
        "dsh_home": "/logs/agent/dsh-home",
    }


def test_post_run_backfills_usage_from_final_metrics(tmp_path, monkeypatch):
    from harbor.models.agent.context import AgentContext

    agent = make_agent(tmp_path)
    agent._session_id = SESSION_ID
    session_dir = host_session_root(tmp_path / "agent-logs") / SESSION_ID
    session_dir.mkdir(parents=True)
    (session_dir / "session.v4.jsonl.zstd").write_bytes(b"official record")
    monkeypatch.setattr(
        "aeval.agents.dsh.agent.read_dsh_session_via_bridge", lambda request: _response()
    )
    trajectory = _trajectory()
    monkeypatch.setattr(
        "aeval.agents.dsh.agent.convert_dsh_read_to_atif", lambda response: trajectory
    )

    class Metrics:
        total_prompt_tokens = 111
        total_completion_tokens = 22
        total_cached_tokens = 5

    class Atif:
        session_id = SESSION_ID
        final_metrics = Metrics()

    class TranscriptWithMetrics:
        atif = Atif()

    agent._transcript = None
    monkeypatch.setattr(
        "aeval.agents.dsh.agent.build_canonical_transcript",
        lambda atif, evidence, stop_reason: TranscriptWithMetrics(),
    )
    context = AgentContext()
    agent.populate_context_post_run(context)
    assert context.n_input_tokens == 111
    assert context.n_output_tokens == 22
    assert context.n_cache_tokens == 5


# --- P0-5: download completeness (fail closed on partial syncs) -------


def test_read_fails_closed_when_session_dir_missing(tmp_path, monkeypatch):
    agent = make_agent(tmp_path)
    agent._session_id = SESSION_ID
    host_session_root(tmp_path / "agent-logs").mkdir(parents=True)  # root but no session
    monkeypatch.setattr(
        "aeval.agents.dsh.agent.resolve_session_reader", lambda explicit=None: Path("reader.js")
    )
    with pytest.raises(DshRunError, match="session directory missing"):
        agent.read_trial_session()


def test_read_fails_closed_when_record_missing(tmp_path, monkeypatch):
    agent = make_agent(tmp_path)
    agent._session_id = SESSION_ID
    (host_session_root(tmp_path / "agent-logs") / SESSION_ID).mkdir(parents=True)
    monkeypatch.setattr(
        "aeval.agents.dsh.agent.resolve_session_reader", lambda explicit=None: Path("reader.js")
    )
    with pytest.raises(DshRunError, match="session record missing"):
        agent.read_trial_session()


def test_read_fails_closed_on_duplicate_records(tmp_path, monkeypatch):
    agent = make_agent(tmp_path)
    agent._session_id = SESSION_ID
    session_dir = host_session_root(tmp_path / "agent-logs") / SESSION_ID
    session_dir.mkdir(parents=True)
    (session_dir / "session.v4.jsonl.zstd").write_bytes(b"one")
    (session_dir / "session.v9.jsonl.zstd").write_bytes(b"two")
    monkeypatch.setattr(
        "aeval.agents.dsh.agent.resolve_session_reader", lambda explicit=None: Path("reader.js")
    )
    with pytest.raises(DshRunError, match="expected exactly one"):
        agent.read_trial_session()


def test_read_accepts_official_layout_with_lease_artifact(tmp_path, monkeypatch):
    """The official backend leaves an empty session.lock lease file on
    POSIX — it must not be mistaken for a second record."""
    agent = make_agent(tmp_path)
    agent._session_id = SESSION_ID
    session_dir = host_session_root(tmp_path / "agent-logs") / SESSION_ID
    session_dir.mkdir(parents=True)
    (session_dir / "session.v4.jsonl.zstd").write_bytes(b"record")
    (session_dir / "session.lock").write_bytes(b"")
    monkeypatch.setattr(
        "aeval.agents.dsh.agent.read_dsh_session_via_bridge", lambda request: _response()
    )
    monkeypatch.setattr(
        "aeval.agents.dsh.agent.convert_dsh_read_to_atif", lambda response: _trajectory()
    )
    transcript = agent.read_trial_session()
    assert transcript.atif.session_id == SESSION_ID
