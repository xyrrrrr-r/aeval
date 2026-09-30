"""The shipped suites inherit the shared convention base — and still say their own facts.

This is the regression guard for the base extraction: if someone edits
``suites/_base/harbor.base.yaml`` in a way that breaks the conventions, or
moves a suite-specific fact (graders, veto, workspace, k) into the base,
these assertions catch it. It also pins the discovery rule that a base under
``_base/`` is never treated as a suite.
"""

from __future__ import annotations

from pathlib import Path

import yaml

from aeval.suite_loader.loader import discover_suites, inherited_source_paths, load_suite

SUITES = Path(__file__).parents[2] / "suites"
BASE = "_base/harbor.base.yaml"
SIX_REQUIREMENTS = [
    "input_complete",
    "agent_finished",
    "integration_valid",
    "render_valid",
    "judge_finished",
    "artifact_schema_ok",
]


def _suite(name: str):
    return load_suite(SUITES / name)


def test_both_shipped_suites_extend_the_shared_base():
    for name in ("e2e-hello", "tbench-pilot"):
        suite = _suite(name)
        assert suite.extends == [BASE], name
        assert [source.role for source in suite.sources] == ["base", "child"]
        assert inherited_source_paths(suite) == [SUITES / BASE]


def test_inherited_conventions_reach_both_suites():
    for name in ("e2e-hello", "tbench-pilot"):
        overlay = _suite(name).overlay
        assert overlay.clock.mode == "real", name
        assert overlay.verdict.requirements == SIX_REQUIREMENTS, name
        assert overlay.harbor.dataset == "datasets/local.yaml", name
        assert [obs.name for obs in overlay.observables][0] == "ready", name
        assert [base.id for base in overlay.baselines] == ["ready"], name
        assert [(m.id, m.kind) for m in overlay.metrics] == [("reliability", "pass_pow_k")], name


def test_the_base_carries_no_agent_capability_requirements():
    """A capability requirement is a suite fact (what THIS eval needs from the
    agent), never a shared convention: putting one in the base turns some
    agent's feature into a global default (DSH's sdk_jsonrpc used to live there
    and forced every other family to patch it out with ``remove.require``)."""
    base = SUITES / "_base" / "harbor.base.yaml"
    assert "driver" not in yaml.safe_load(base.read_text())

    for name in ("e2e-hello", "tbench-pilot"):
        overlay = _suite(name).overlay
        # declared by the suite itself: only what the task really needs
        assert overlay.driver.require == ["acp_stdio", "shell"], name
        assert overlay.driver.session_record == "dsh_session", name


def test_suite_specific_facts_stay_in_the_suite():
    pilot = _suite("tbench-pilot").overlay
    assert [obs.name for obs in pilot.observables] == ["ready", "reward"]
    assert pilot.verdict.graders["trajectory"].veto is True
    assert pilot.driver.workspace_dir == "/app"
    assert pilot.driver.stage_tests_before_collect is True
    assert pilot.metrics[0].k == 3
    assert pilot.harbor.job == "jobs/tbench-m0.yaml"
    assert pilot.provenance.license == "Apache-2.0"

    hello = _suite("e2e-hello").overlay
    assert [obs.name for obs in hello.observables] == ["ready", "result"]
    assert set(hello.verdict.graders) == {"default"}
    assert hello.metrics[0].k == 5
    assert hello.harbor.job == "jobs/e2e-hello.yaml"
    assert hello.provenance.source == "authored-internally"


def test_identity_covers_the_shared_base():
    for name in ("e2e-hello", "tbench-pilot"):
        suite = _suite(name)
        # Chain digest differs from the raw child bytes: the base is part of
        # identity, which is what the manifest gate and comparability use.
        assert suite.identity_digest != suite.suite_yaml_digest, name
        assert suite.identity() == (suite.id, suite.version, suite.identity_digest)


def test_bases_are_not_discovered_as_suites():
    discovered = discover_suites([SUITES])
    names = sorted(path.name for path in discovered)
    assert names == [
        "deepagent-budget",
        "deepagent-hello",
        "e2e-hello",
        "tbench-pilot",
    ]
    assert all(path.name != "_base" for path in discovered)
