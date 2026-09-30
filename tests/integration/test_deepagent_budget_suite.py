"""P2-5b budget suite: the metered acceptance carrier.

``deepagent-hello`` proves the chain runs; this suite proves the chain is
*accountable* — it declares spend caps, and the adapter now declares
``budget_enforcement: gateway_lease`` with the control stack that makes that
claim true. These tests pin the pairing that the lab run will exercise, so a
suite that silently lost its caps (or an adapter that lost its stack) fails
here instead of surfacing as an unmetered run that looked metered.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from aeval.agents.contract import (
    budget_enforcement_point,
    budget_gate_violation,
    build_adapter_spec,
    load_adapter_class,
)
from aeval.hooks.evidence import (
    FIXED_OUTPUT_PATHS,
    SESSION_RECORD_OUTPUTS,
    build_required_collect_plan,
)
from aeval.suite_loader.composition import compose_harbor_job
from aeval.suite_loader.loader import load_suite

REPO = Path(__file__).resolve().parents[2]
SUITE = REPO / "suites" / "deepagent-budget"
HELLO = REPO / "suites" / "deepagent-hello"
AGENTS = REPO / "agents"


@pytest.fixture(scope="module")
def suite():
    return load_suite(SUITE)


def _deepagent_specs():
    return [
        build_adapter_spec(
            load_adapter_class("aeval.agents.deepagent.agent:DcodeAgent"),
            import_path="aeval.agents.deepagent.agent:DcodeAgent",
        )
    ]


def test_the_suite_declares_real_caps_at_the_gateway_lease(suite):
    budget = suite.overlay.budget
    assert budget is not None, "the metered carrier must declare a budget"
    assert budget.enforcement_point == "gateway_lease"
    assert budget.max_tokens == 200000
    assert budget.max_steps == 40
    assert budget.max_seconds == 1800


def test_the_capped_suite_is_now_allowed_for_the_leased_adapter(suite):
    """The P1-3 gate's refusal flipped because the adapter earned the claim.

    Before P2-5b this same call was a violation (deepagent could not be
    metered); with the facade stack declared it is allowed — and the run-level
    enforcement point says where the spend is actually stopped.
    """
    specs = _deepagent_specs()
    assert budget_gate_violation(specs, suite.overlay.budget, accepted=False) is None
    assert budget_enforcement_point(specs) == "gateway_lease"


def test_an_unmetered_adapter_is_still_refused_by_this_suite(suite):
    """The gate is not weakened: the nop/fake adapter cannot serve caps."""
    from aeval.agents.testing.fake_agent import FakeAtifAgent

    specs = [build_adapter_spec(FakeAtifAgent)]
    violation = budget_gate_violation(specs, suite.overlay.budget, accepted=False)
    assert violation is not None
    assert "budget_enforcement" in violation


def test_the_smoke_suite_stays_uncapped_while_the_carrier_is_capped():
    """Two suites, two jobs: the smoke must not gain caps, nor lose them."""
    assert load_suite(HELLO).overlay.budget is None
    assert load_suite(SUITE).overlay.budget is not None


def test_the_budget_suite_keeps_the_generic_session_slot(suite):
    """Same evidence shape as the smoke suite — only the caps differ."""
    plan = build_required_collect_plan(suite)
    fixed = {n for n in FIXED_OUTPUT_PATHS if n not in SESSION_RECORD_OUTPUTS}
    assert set(plan) == fixed | {"agent_session_record"} | {
        f"observable:{o.name}" for o in suite.overlay.observables
    }
    assert "dsh_session" not in plan


def test_both_images_bake_the_cli_version_the_adapter_declares():
    """The sandbox egress allowlist carries only the broker host, so a PyPI
    fetch at agent setup is impossible: the pinned CLI ships in the image. The
    pin therefore exists twice, and these assertions keep the two copies equal."""
    from aeval.agents.deepagent.agent import (
        _DEEPAGENTS_CODE_VERSION,
        default_deepagent_registry_entry,
    )

    pinned = f"deepagents-code=={_DEEPAGENTS_CODE_VERSION}"
    for suite in (SUITE, HELLO):
        dockerfile = (suite / "tasks" / "hello" / "environment" / "Dockerfile").read_text("utf-8")
        assert pinned in dockerfile, f"{suite.name} does not bake {pinned}"
        assert "ln -sf /opt/deepagents/bin/dcode /usr/local/bin/dcode" in dockerfile
        # the install must be offline: the lab has no PyPI egress, and the
        # wheelhouse it needs is prepared by tools/fetch_deepagent_wheelhouse.py
        assert "COPY wheelhouse /opt/wheelhouse" in dockerfile
        assert "pip install --no-index --find-links /opt/wheelhouse" in dockerfile
    # the entry runs the console script the image links, not a downloaded tool
    entry = default_deepagent_registry_entry()
    assert entry["distribution"]["local"]["cmd"] == "dcode"
    assert entry["distribution"]["local"]["args"] == ["--acp"]
    assert "uvx" not in entry["distribution"]


def test_the_image_ships_node_for_the_facade():
    """The declared control stack runs inside the sandbox: without node in the
    image, aeval's health gate would fail every trial after uploading."""
    dockerfile = (SUITE / "tasks" / "hello" / "environment" / "Dockerfile").read_text("utf-8")
    assert "nodejs" in dockerfile
    # the smoke suite needs it for the same reason
    hello_dockerfile = (HELLO / "tasks" / "hello" / "environment" / "Dockerfile").read_text("utf-8")
    assert "nodejs" in hello_dockerfile


def test_the_capped_suite_composes_with_the_deepagent_declaration(suite):
    driven = compose_harbor_job(suite, agent="deepagent", agents_root=AGENTS)
    assert driven.agents[0].import_path == "aeval.agents.deepagent.agent:DcodeAgent"
    assert driven.n_attempts == 5
    # the placeholder job keeps the suite loadable with no adapter installed
    placeholder = compose_harbor_job(suite)
    assert [a.name for a in placeholder.agents] == ["nop"]
    assert (SUITE / "jobs" / "deepagent-budget.yaml").is_file()
