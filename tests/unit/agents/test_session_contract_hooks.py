"""The session half of the adapter contract.

Session-record shape and session identity are adapter-flavored: DSH persists one
record per id that aeval minted, while an ACP runner mints its own id and leaves
a single summary. The framework must ask the adapter for both facts instead of
encoding its first agent's layout a second time — these tests pin that seam, and
pin that a missing hook fails closed rather than being guessed.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from aeval.agents.contract import (
    locate_session_record,
    sandbox_session_record,
    session_id_from_record,
    terminal_descriptor_owner,
)
from aeval.agents.deepagent.agent import DcodeAgent
from aeval.agents.dsh.agent import DshAgent, find_session_record
from aeval.suite_models import SuiteError


class _Plain:
    """An adapter that declares nothing about its session."""

    SESSION_RECORD_OUTPUT = "dsh_session"


class _DeclaresOnlyAnOutput:
    SESSION_RECORD_OUTPUT = "agent_session_record"


class _BadOwner:
    TERMINAL_DESCRIPTOR_OWNER = "sandbox-side"


class _BadRecordPath:
    SANDBOX_SESSION_RECORD = "logs/agent/summary.json"


class _BadIdHook:
    session_id_from_record = "agent_session_id"


class _IdHookReturnsInt:
    @staticmethod
    def session_id_from_record(record_bytes: bytes):
        return 7


def _summary(tmp_path: Path, session_id: str) -> Path:
    record = tmp_path / "acp-summary.json"
    record.write_text(json.dumps({"session": {"sessionId": session_id}}), encoding="utf-8")
    return record


def test_the_terminal_observer_defaults_to_the_stack_that_owns_the_session():
    """An adapter declaring nothing cannot silently appoint the host."""
    assert terminal_descriptor_owner(_Plain) == "sandbox"
    assert terminal_descriptor_owner(DshAgent) == "sandbox"
    # the facade flavor: nothing in the sandbox ever sees an exit
    assert terminal_descriptor_owner(DcodeAgent) == "host"


def test_an_unknown_terminal_observer_is_refused():
    with pytest.raises(SuiteError, match="TERMINAL_DESCRIPTOR_OWNER"):
        terminal_descriptor_owner(_BadOwner)


def test_the_sandbox_session_record_is_optional_and_must_be_absolute():
    assert sandbox_session_record(_Plain) is None
    assert sandbox_session_record(DshAgent) is None
    # the sandbox path belongs to the adapter; the framework never guesses it
    assert sandbox_session_record(DcodeAgent) == "/logs/agent/acp-summary.json"
    with pytest.raises(SuiteError, match="absolute sandbox path"):
        sandbox_session_record(_BadRecordPath)


def test_only_the_adapter_whose_recorder_is_foreign_inscribes_an_identity(tmp_path):
    record = _summary(tmp_path, "b97fd0d2")
    # DSH's identity is the id aeval minted and handed to the stack
    assert session_id_from_record(DshAgent, record.read_bytes()) is None
    assert session_id_from_record(DcodeAgent, record.read_bytes()) == "b97fd0d2"


def test_a_broken_identity_hook_fails_closed(tmp_path):
    record = _summary(tmp_path, "x")
    with pytest.raises(SuiteError, match="non-callable"):
        session_id_from_record(_BadIdHook, record.read_bytes())
    with pytest.raises(SuiteError, match="return the session id string"):
        session_id_from_record(_IdHookReturnsInt, record.read_bytes())
    # a record that is not the adapter's summary is None, never an exception
    assert session_id_from_record(DcodeAgent, b"not json") is None
    assert session_id_from_record(DcodeAgent, json.dumps({}).encode()) is None


def test_an_adapter_with_a_session_output_must_say_where_the_record_is():
    """Fail closed: a record the framework cannot locate cannot be verified."""
    with pytest.raises(SuiteError, match="cannot verify a record it cannot locate"):
        locate_session_record(_DeclaresOnlyAnOutput, Path("/tmp"), "whatever")


def test_the_locator_is_the_adapters_own_rule(tmp_path):
    record = _summary(tmp_path, "acp-9")
    located = locate_session_record(DcodeAgent, tmp_path, "acp-9")
    assert located == record
    # a record for another session is not this session's record
    assert locate_session_record(DcodeAgent, tmp_path, "other") is None
    # DSH keeps the historical search, unchanged, through the same hook
    assert locate_session_record(DshAgent, tmp_path, "acp-9") == find_session_record(
        tmp_path, "acp-9"
    )


def test_the_declared_session_root_is_where_the_record_actually_is():
    """The declared artifact dir must be the one the locator reads (a real
    target host surfaced this: a
    shipped "deepagent-home" made every run fail with "session_root does not
    exist" because nothing created it)."""
    assert DcodeAgent.SESSION_ARTIFACT_DIR == "."
    assert DshAgent.SESSION_ARTIFACT_DIR == "dsh-home"
