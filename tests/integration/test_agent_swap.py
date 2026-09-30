"""Ad-hoc agent pairing: the recorded slot override, end to end at the seam.

The suite still DECLARES the evidence shape it expects (C: auditable default),
and an operator may replace it for one run (B) — but only with the shape the
selected adapter actually produces, and the replacement is recorded. This is
what makes "try another agent without editing sealed bytes" possible without
turning the pairing rule into a suggestion.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from aeval.hooks.evidence import build_required_collect_plan, output_path_for
from aeval.suite_loader.composition import compose_harbor_job
from aeval.suite_loader.loader import load_suite
from aeval.suite_models import SuiteError

REPO = Path(__file__).resolve().parents[2]
AGENTS = REPO / "agents"


def test_the_suite_declares_its_expected_shape():
    """C: the expectation stays in the versioned artifact."""
    suite = load_suite(REPO / "suites" / "tbench-pilot")
    assert suite.overlay.driver.session_record == "dsh_session"


def test_an_ad_hoc_pairing_needs_the_recorded_override():
    suite = load_suite(REPO / "suites" / "tbench-pilot")
    with pytest.raises(SuiteError, match="session-record flavor mismatch"):
        compose_harbor_job(suite, agent="deepagent", agents_root=AGENTS)

    job = compose_harbor_job(
        suite,
        agent="deepagent",
        agents_root=AGENTS,
        session_record="agent_session_record",
    )
    assert job.agents[0].import_path.endswith("DcodeAgent")


def test_the_override_must_match_the_selected_adapters_slot():
    suite = load_suite(REPO / "suites" / "tbench-pilot")
    with pytest.raises(SuiteError, match="session-record flavor mismatch"):
        compose_harbor_job(
            suite, agent="deepagent", agents_root=AGENTS, session_record="dsh_session"
        )


def test_the_collect_plan_follows_the_override():
    suite = load_suite(REPO / "suites" / "tbench-pilot")
    declared = build_required_collect_plan(suite)
    assert "dsh_session" in declared and "agent_session_record" not in declared

    overridden = build_required_collect_plan(suite, "agent_session_record")
    assert "agent_session_record" in overridden
    assert "dsh_session" not in overridden
    # the fixed path travels with the slot: the plan names the output, the
    # bundle keeps the slot's own path
    assert output_path_for("agent_session_record") == "agent_session/record"
    assert output_path_for("dsh_session") == "sessions/session.v4.jsonl.zstd"
