"""P0-6 real producer tests: collection driven against a live trial.

The producers themselves are covered in test_collectors.py; this file
covers the WIRING — what gets collected, from where, and what happens
when a required output cannot be produced (fail closed).
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from aeval.contracts import RuntimeLock
from aeval.hooks.collection import (
    CollectionError,
    collect_trial_evidence,
    load_broker_calls,
    observe_runtime_dump,
)
from aeval.hooks.evidence import (
    FIXED_OUTPUT_PATHS,
    build_required_collect_plan,
    load_collection_manifest,
)


class FakeExec:
    """Emits Harbor-shaped results (``return_code``, not ``exit_code``)."""

    def __init__(self, outputs: dict[str, str], *, fail: bool = False):
        self.outputs = outputs
        self.fail = fail

    async def exec(self, command: str):
        if self.fail:
            return SimpleNamespace(return_code=1, stdout="", stderr="boom")
        for key, value in self.outputs.items():
            if key in command:
                return SimpleNamespace(return_code=0, stdout=value, stderr="")
        # any file-backed observable reads back its own value
        if command.startswith("cat /workspace/"):
            name = command.rsplit("/", 1)[-1]
            return SimpleNamespace(
                return_code=0, stdout=f"observed:{name}", stderr=""
            )
        return SimpleNamespace(return_code=127, stdout="", stderr="not found")


class FakeEnvironment:
    """A started sandbox: exec works, policy is observable."""

    def __init__(self, *, fail_exec: bool = False):
        self._exec = FakeExec(
            {"uname -m": "aarch64",
             "uname -sr": "Linux 6.6.0", "node --version": "v24.20.0"},
            fail=fail_exec,
        )
        self.network_policy = SimpleNamespace(
            network_mode="no-network", allowed_hosts=[]
        )

    async def exec(self, command: str):
        return await self._exec.exec(command)


class UndialableEnvironment:
    """A sandbox whose out-of-band reads all fail."""

    async def exec(self, command: str):
        raise RuntimeError("sandbox is gone")


class FakeAgent:
    """Stands in for DshAgent: has paths(), session_id, read_trial_session()."""

    def __init__(self, logs_dir: Path, session_id: str, *, record: bytes | None = b"session-bytes"):
        self._logs_dir = logs_dir
        self.dsh_session_id = session_id
        if record is not None:
            session_dir = logs_dir / "dsh-home" / "sessions" / session_id
            session_dir.mkdir(parents=True, exist_ok=True)
            (session_dir / "session.v4.jsonl.zstd").write_bytes(record)

    def paths(self):
        return SimpleNamespace(logs_dir=self._logs_dir)

    def read_trial_session(self):
        return SimpleNamespace(
            model_dump_json=lambda indent=2: json.dumps({"session": self.dsh_session_id})
        )


def _suite(names=("result",)):
    overlay = SimpleNamespace(
        observables=[
            SimpleNamespace(name=n, type="string", source=f"file:/workspace/{n}")
            for n in names
        ],
    )
    return SimpleNamespace(overlay=overlay)


async def test_observation_uses_only_measured_facts(tmp_path):
    env = FakeEnvironment()
    dump = await observe_runtime_dump(env, trial_id="t1", session_id="s1")
    assert dump["machine"] == "aarch64"
    assert dump["kernel"] == "Linux 6.6.0"
    assert dump["node_version"] == "v24.20.0"
    assert dump["backend"] == "e2b"
    # the SDK-present fact is observed, not assumed: assert consistency
    # with what this host actually has installed
    assert isinstance(dump["e2b_sdk_present"], bool)
    if dump["e2b_sdk_present"]:
        assert isinstance(dump.get("e2b_sdk_version"), str)
    else:
        assert "e2b_sdk_version" not in dump
    assert dump["trial_id"] == "t1" and dump["session_id"] == "s1"


async def test_observation_omits_unmeasurable_fields():
    """Reads that fail must OMIT the field, never invent one."""
    dump = await observe_runtime_dump(
        UndialableEnvironment(), trial_id="t1", session_id="s1"
    )
    assert "machine" not in dump and "kernel" not in dump
    assert dump["backend"] == "e2b"  # the owner knows its own backend


async def test_collects_every_planned_output_and_writes_manifest(
    tmp_path, runtime_lock
):
    trial_dir = tmp_path / "trial"
    env = FakeEnvironment()
    agent = FakeAgent(tmp_path / "agent-logs", "sess-1")
    manifest = await collect_trial_evidence(
        trial_dir=trial_dir,
        trial_id="t1",
        suite=_suite(),
        environment=env,
        agent=agent,
        runtime_lock=runtime_lock,
        session_id="sess-1",
    )

    plan = {"runtime_dump", "mock_call_log", "dsh_session",
            "canonical_transcript", "observable:result"}
    assert {o.name for o in manifest.outcomes} == plan
    assert manifest.runtime_lock_digest == runtime_lock.digest()
    for name, rel in FIXED_OUTPUT_PATHS.items():
        assert (trial_dir / rel).is_file(), name
    assert (trial_dir / "observables" / "result.json").is_file()

    # the observable artifact carries the PROBED value, not a placeholder
    value = json.loads((trial_dir / "observables" / "result.json").read_text())
    assert value == {"name": "result", "value": "observed:result"}

    # no broker ran, so the call log records that fact explicitly
    log = (trial_dir / FIXED_OUTPUT_PATHS["mock_call_log"]).read_text().strip()
    parsed = json.loads(log)
    assert parsed["event"] == "no_calls_observed"
    assert "broker" in parsed["reason"]

    # the manifest on disk is the one the gate will read
    on_disk = load_collection_manifest(trial_dir)
    assert on_disk.trial_id == manifest.trial_id
    assert on_disk.runtime_lock_digest == runtime_lock.digest()
    # and it is never an artifact of itself
    assert "collection_manifest" not in {r.path.split("/")[0] for r in manifest.artifacts}


async def test_collects_declared_broker_calls_when_present(tmp_path, runtime_lock):
    trial_dir = tmp_path / "trial"
    manifest = await collect_trial_evidence(
        trial_dir=trial_dir,
        trial_id="t1",
        suite=_suite(),
        environment=FakeEnvironment(),
        agent=FakeAgent(tmp_path / "agent-logs", "s"),
        runtime_lock=runtime_lock,
        session_id="s",
        broker_calls=[{"event": "dispatch", "tokens": 12}],
    )
    log = (trial_dir / FIXED_OUTPUT_PATHS["mock_call_log"]).read_text().strip()
    assert json.loads(log) == {"event": "dispatch", "tokens": 12}
    assert any(o.name == "mock_call_log" for o in manifest.outcomes)


async def test_missing_environment_handle_fails_closed(tmp_path, runtime_lock):
    with pytest.raises(CollectionError, match="no live environment handle"):
        await collect_trial_evidence(
            trial_dir=tmp_path / "t",
            trial_id="t1",
            suite=_suite(),
            environment=None,
            agent=FakeAgent(tmp_path / "logs", "s"),
            runtime_lock=runtime_lock,
            session_id="s",
        )


async def test_unprobeable_observable_fails_closed(tmp_path, runtime_lock):
    with pytest.raises(CollectionError, match="observable 'result' could not be probed"):
        await collect_trial_evidence(
            trial_dir=tmp_path / "t",
            trial_id="t1",
            suite=_suite(),
            environment=UndialableEnvironment(),
            agent=FakeAgent(tmp_path / "logs", "s"),
            runtime_lock=runtime_lock,
            session_id="s",
        )


async def test_missing_official_session_record_fails_closed(tmp_path, runtime_lock):
    with pytest.raises(CollectionError, match="not found under"):
        await collect_trial_evidence(
            trial_dir=tmp_path / "t",
            trial_id="t1",
            suite=_suite(),
            environment=FakeEnvironment(),
            agent=FakeAgent(tmp_path / "logs", "s", record=None),
            runtime_lock=runtime_lock,
            session_id="s",
        )


async def test_agent_without_a_dsh_session_fails_closed(tmp_path, runtime_lock):
    """A nop/oracle agent has no official session — evidence must not be
    fabricated for it."""

    class PlainAgent:
        dsh_session_id = None

    with pytest.raises(CollectionError, match="no DSH session"):
        await collect_trial_evidence(
            trial_dir=tmp_path / "t",
            trial_id="t1",
            suite=_suite(),
            environment=FakeEnvironment(),
            agent=PlainAgent(),
            runtime_lock=runtime_lock,
            session_id="s",
        )


async def test_broken_session_read_fails_closed(tmp_path, runtime_lock):
    class BadReadAgent(FakeAgent):
        def read_trial_session(self):
            raise RuntimeError("synced session root missing")

    with pytest.raises(CollectionError, match="canonical transcript could not be built"):
        await collect_trial_evidence(
            trial_dir=tmp_path / "t",
            trial_id="t1",
            suite=_suite(),
            environment=FakeEnvironment(),
            agent=BadReadAgent(tmp_path / "logs", "s"),
            runtime_lock=runtime_lock,
            session_id="s",
        )


def test_load_broker_calls_tolerates_missing_and_bad_lines(tmp_path):
    assert load_broker_calls(None) == []
    assert load_broker_calls(tmp_path / "nope.jsonl") == []
    path = tmp_path / "calls.jsonl"
    path.write_text('{"a": 1}\n\nnot json\n[1,2]\n{"b": 2}\n', encoding="utf-8")
    assert load_broker_calls(path) == [{"a": 1}, {"b": 2}]


async def test_collection_plan_matches_the_suite_declaration(tmp_path, runtime_lock):
    """The collected set must equal the plan the suite validation built."""
    suite = _suite(names=("result", "status"))
    trial_dir = tmp_path / "trial"
    manifest = await collect_trial_evidence(
        trial_dir=trial_dir,
        trial_id="t1",
        suite=suite,
        environment=FakeEnvironment(),
        agent=FakeAgent(tmp_path / "logs", "s"),
        runtime_lock=runtime_lock,
        session_id="s",
    )
    assert {o.name for o in manifest.outcomes} == {
        "runtime_dump", "mock_call_log", "dsh_session", "canonical_transcript",
        "observable:result", "observable:status",
    }


async def test_absent_observable_is_recorded_not_treated_as_infrastructure(
    tmp_path, runtime_lock
):
    """A probe the environment ANSWERS with "not there" is a fact about
    the run (the task was not done), so it is collected explicitly and
    scored — only an unobservable probe blocks the trial."""
    class NothingProduced:
        """Answers every read with "not there" — the task was not done."""

        network_policy = SimpleNamespace(network_mode="no-network", allowed_hosts=[])

        async def exec(self, command: str):
            return SimpleNamespace(return_code=1, stdout="", stderr="no such file")

    env = NothingProduced()
    manifest = await collect_trial_evidence(
        trial_dir=tmp_path / "trial", trial_id="t1", suite=_suite(),
        environment=env, agent=FakeAgent(tmp_path / "logs", "s"),
        runtime_lock=runtime_lock, session_id="s",
    )
    assert any(o.name == "observable:result" for o in manifest.outcomes)
    artifact = json.loads(
        (tmp_path / "trial" / "observables" / "result.json").read_text()
    )
    assert artifact["value"]["present"] is False
    assert "exited" in artifact["value"]["reason"]
