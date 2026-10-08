"""A spend cap nobody can enforce must block the run, not silently fail.

Before this, budget enforcement was vestigial end to end: ``BudgetSnapshot`` /
``RunManifest.budget_enforcement_point`` were never written, no config surface
could declare a cap, and an adapter that routes model traffic around the gateway
lease (its own API key) would overspend with the lease none the wiser — the gap
only surfaced later as a partial verdict.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from aeval.agents.contract import (
    budget_enforcement_point,
    budget_gate_violation,
    build_adapter_spec,
    load_adapter_class,
)
from aeval.agents.dsh.agent import DshAgent
from aeval.agents.testing.fake_agent import REFERENCE_AGENT_IMPORT_PATH
from aeval.contracts import AdapterSpec, BudgetSnapshot
from aeval.hooks.plugin import HookRegistrationError
from aeval.hooks.plugin import _require_adapter_contract  # noqa: PLC2701


def _spec(**overrides) -> AdapterSpec:
    base = build_adapter_spec(load_adapter_class(REFERENCE_AGENT_IMPORT_PATH))
    return base.model_copy(update=overrides)


def test_enforcement_point_is_the_adapters_when_they_agree():
    dsh = build_adapter_spec(DshAgent)
    assert budget_enforcement_point([dsh]) == "gateway_lease"
    assert budget_enforcement_point([_spec()]) == "none"
    assert budget_enforcement_point([dsh, _spec()]) == "mixed"
    # no adapter selected: claim nothing
    assert budget_enforcement_point([]) == "none"


def test_no_cap_means_no_gate():
    assert budget_gate_violation([_spec()], None, accepted=False) is None
    # a snapshot with no numbers is not a cap
    assert budget_gate_violation([_spec()], BudgetSnapshot(), accepted=False) is None


@pytest.mark.parametrize(
    "budget", [BudgetSnapshot(max_tokens=1000), BudgetSnapshot(max_seconds=60), BudgetSnapshot(max_steps=5)]
)
def test_unmeterable_adapter_is_refused_when_a_cap_is_declared(budget):
    violation = budget_gate_violation([_spec()], budget, accepted=False)
    assert violation is not None
    assert "gateway lease" in violation
    assert "--accept-unmetered-budget" in violation


def test_a_metered_adapter_satisfies_the_cap():
    dsh = build_adapter_spec(DshAgent)
    assert budget_gate_violation([dsh], BudgetSnapshot(max_tokens=1000), accepted=False) is None


def test_the_operator_can_accept_the_gap_explicitly():
    budget = BudgetSnapshot(max_tokens=1000)
    assert budget_gate_violation([_spec()], budget, accepted=True) is None


def test_run_start_refuses_a_capped_suite_with_an_unmeterable_adapter():
    budget = BudgetSnapshot(max_tokens=1000)
    job = SimpleNamespace(config=SimpleNamespace(
        agents=[SimpleNamespace(import_path=REFERENCE_AGENT_IMPORT_PATH)]
    ))
    with pytest.raises(HookRegistrationError, match="overspend silently"):
        _require_adapter_contract(job, budget, accepted=False)
    # the recorded acceptance is what lets it through
    _require_adapter_contract(job, budget, accepted=True)
    # the shipped adapter is metered, so it is never gated
    _require_adapter_contract(
        SimpleNamespace(config=SimpleNamespace(
            agents=[SimpleNamespace(import_path="aeval.agents.dsh.agent:DshAgent")]
        )),
        budget,
        accepted=False,
    )


def test_trial_record_records_the_enforcement_point():
    from aeval.contracts import TrialCoordinates
    from aeval.verdict.pipeline import build_trial_record
    from aeval.verdict.progress import RequirementProgress

    coordinates = TrialCoordinates(
        run_id="r", suite_id="s", suite_version="1", task_id="task", trial_index=0
    )

    def _build(adapter):
        return build_trial_record(
            trial_id="t",
            coordinates=coordinates,
            stop_reason="agent_exit_0",
            baseline_ok=True,
            progress=RequirementProgress(),
            artifacts={},
            transcript_extra=None,
            grader_versions={},
            adapter=adapter,
        )

    dsh = _build(build_adapter_spec(DshAgent))
    assert dsh.budget.enforcement_point == "gateway_lease"
    assert dsh.budget.used_tokens is None      # measured or unavailable, never invented
    # no adapter recorded: claim no enforcement point at all
    assert _build(None).budget is None
