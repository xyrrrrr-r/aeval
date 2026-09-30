"""P2-2: the conformance kit — and its honesty rule.

Six checks, each mapping to a way a second agent breaks an evaluation silently.
The rule that matters most: a check that could not run reports ``skipped``, never
``pass``. "Could not exercise" and "works" are different facts.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from aeval.agents.conformance import (
    check_accounting,
    check_capabilities,
    check_contract,
    check_declaration,
    check_model_routing,
    check_transcript,
    run_conformance,
    run_conformance_for,
)
from aeval.agents.testing.fake_agent import FakeAtifAgent
from aeval.contracts import BudgetSnapshot

REPO = Path(__file__).resolve().parents[3]
AGENTS = REPO / "agents"
FAKE = "aeval.agents.testing.fake_agent:FakeAtifAgent"
DSH = "aeval.agents.dsh.agent:DshAgent"


def test_the_shipped_dsh_adapter_is_conformant():
    from aeval.agents.contract import load_adapter_class

    report = run_conformance(
        load_adapter_class(DSH),
        import_path=DSH,
        declaration_path=AGENTS / "dsh.yaml",
        agents_root=AGENTS,
        required_capabilities=["acp_stdio", "sdk_jsonrpc"],
    )
    assert report.failures == [], report.render()
    assert [check.name for check in report.checks] == [
        "declaration",
        "contract",
        "model_routing",
        "capabilities",
        "accounting",
        "transcript",
    ]


def test_a_capability_mismatch_is_caught_before_any_sandbox_exists():
    """The suite's require is checked against the declaration, not discovered late."""
    reports = run_conformance_for(
        FAKE, declaration_path=AGENTS / "fakeagent.yaml", agents_root=AGENTS,
        suite_paths=[REPO / "suites" / "e2e-hello"],
    )
    assert len(reports) == 1
    failed = {check.name for check in reports[0].failures}
    assert failed == {"capabilities"}
    assert "missing" in reports[0].failures[0].detail


def test_an_unmeterable_adapter_under_a_cap_fails_the_accounting_check():
    from aeval.agents.contract import load_adapter_class

    check = check_accounting(load_adapter_class(FAKE), BudgetSnapshot(max_tokens=1000))
    assert check.status == "fail"
    assert "budget" in check.detail or "lease" in check.detail or "metered" in check.detail
    # an unmeterable adapter with no cap is not a failure, only unexercised
    assert check_accounting(load_adapter_class(FAKE), None).status == "skipped"
    # the gateway adapter passes the same cap
    assert check_accounting(load_adapter_class(DSH), BudgetSnapshot(max_tokens=1000)).status == "pass"


def test_a_supplied_instance_lets_the_transcript_check_run_for_real():
    """DshAgent needs logs_dir; a real instance is how that check gets exercised."""
    from aeval.agents.contract import load_adapter_class

    # no instance: the check is honest about not having run
    assert check_transcript(load_adapter_class(DSH)).status == "skipped"
    # with one: it runs, and says which instance it used
    report = run_conformance(
        load_adapter_class(FAKE), import_path=FAKE, instance=FakeAtifAgent()
    )
    transcript = next(check for check in report.checks if check.name == "transcript")
    assert transcript.status == "pass"
    assert "supplied instance" in transcript.detail


def test_a_supplied_instance_that_lies_still_fails():
    class Liar:
        def read_trial_session(self):
            return {"ok": True}

    assert check_transcript(Liar, Liar()).status == "fail"


def test_a_check_that_cannot_run_is_skipped_not_passed():
    class Unbuildable:
        def __init__(self, logs_dir):  # noqa: ANN001 - a required arg is the point
            self.logs_dir = logs_dir

    check = check_transcript(Unbuildable)
    assert check.status == "skipped"
    assert "not instantiable" in check.detail


def test_a_transcript_that_is_not_canonical_is_a_failure():
    class Liar:
        def read_trial_session(self):
            return {"steps": [], "ok": True}  # looks complete, is not ATIF

    check = check_transcript(Liar)
    assert check.status == "fail"
    assert "CanonicalTranscript" in check.detail


def test_a_raising_read_is_skipped_with_its_reason():
    class Empty:
        def read_trial_session(self):
            raise FileNotFoundError("no session recorded yet")

    check = check_transcript(Empty)
    assert check.status == "skipped"
    assert "FileNotFoundError" in check.detail


def test_the_report_never_claims_a_skipped_check_as_proof():
    report = run_conformance(FakeAtifAgent, import_path=FAKE)
    # no declaration given, no suite given: those two are skipped, not passed
    assert {"declaration", "capabilities", "accounting"} <= {c.name for c in report.skipped}
    assert report.ok is True
    rendered = report.render()
    assert "[skipped] declaration" in rendered
    assert "skipped" in rendered.rsplit("\n", 1)[-1]


def test_a_declaration_that_disagrees_with_the_class_fails_here(tmp_path):
    import yaml

    from aeval.agents.declaration import AGENT_BASE_FILENAME

    root = tmp_path
    (root / "_base").mkdir()
    base = yaml.safe_load((AGENTS / "_base" / AGENT_BASE_FILENAME).read_text())
    (root / "_base" / AGENT_BASE_FILENAME).write_text(yaml.safe_dump(base))
    lying = {
        **base,
        "id": "fakeagent",
        "version": "1",
        "import_path": FAKE,
        # fakeagent does not provide resume — the declaration would be a lie
        "provides": ["shell", "resume"],
        "transcript": {"source": "atif_native", "reader": "r", "capabilities": [], "fields_available": {}},
    }
    (root / "fakeagent.yaml").write_text(yaml.safe_dump(lying))

    check = check_declaration(root / "fakeagent.yaml", FakeAtifAgent, root)
    assert check.status == "fail"
    assert "disagrees" in check.detail
    # a broken declaration is a finding, not a crash
    report = run_conformance(
        FakeAtifAgent, import_path=FAKE, declaration_path=root / "fakeagent.yaml", agents_root=root
    )
    assert report.ok is False
    assert report.failures[0].name == "declaration"
    assert "disagrees" in report.failures[0].detail


def test_contract_check_names_what_is_missing():
    class Bare:
        pass

    check = check_contract(Bare)
    assert check.status == "fail"
    assert "missing members" in check.detail or "missing declarations" in check.detail


# --- stage 4.2: the routing check -------------------------------------------


def test_the_shipped_routings_agree_with_their_control_stacks():
    """dsh speaks the broker wire itself; dcode speaks OpenAI and the facade
    stack translates it. Both must report the wire the deployment serves."""
    from aeval.agents.contract import load_adapter_class
    from aeval.agents.deepagent.agent import DcodeAgent

    dsh = check_model_routing(load_adapter_class(DSH))
    assert dsh.status == "pass"
    assert "gateway_native" in dsh.detail

    dcode = check_model_routing(DcodeAgent)
    assert dcode.status == "pass"
    assert "openai_responses" in dcode.detail
    assert "['responses']" in dcode.detail
    assert "OPENAI_API_KEY" in dcode.detail


def test_an_openai_wire_without_the_facade_stack_is_refused():
    """The quiet overclaim: an unmetered agent the manifest still calls metered."""
    class Unrouted:
        MODEL_ROUTING = {
            "agent_protocol": "openai_chat",
            "env": {"base_url": "OPENAI_BASE_URL", "api_key": "OPENAI_API_KEY"},
        }
        CONTROL_STACK = None

    check = check_model_routing(Unrouted)
    assert check.status == "fail"
    assert "unmetered" in check.detail


def test_a_facade_stack_without_a_routing_is_refused():
    """The mirror case: a facade serving an endpoint nobody calls."""
    class EmptyFacade:
        CONTROL_STACK = "deepagent-facade"

    check = check_model_routing(EmptyFacade)
    assert check.status == "fail"
    assert "never calls" in check.detail


def test_an_adapter_with_no_routing_reports_skipped_not_pass():
    from aeval.agents.contract import load_adapter_class

    check = check_model_routing(load_adapter_class(FAKE))
    assert check.status == "skipped"
    assert "no MODEL_ROUTING" in check.detail


def test_the_routing_check_runs_in_the_kit_and_can_fail_it():
    """A real adapter with its facade stack removed: the kit must catch the
    unmetered routing, not just the standalone check."""
    from aeval.agents.deepagent.agent import DcodeAgent

    class Unrouted(DcodeAgent):
        CONTROL_STACK = None

    report = run_conformance(Unrouted, import_path="tests:Unrouted")
    assert not report.ok
    failed = {check.name for check in report.failures}
    assert "model_routing" in failed
    routing = next(c for c in report.failures if c.name == "model_routing")
    assert "unmetered" in routing.detail
    # and the report is TOTAL: one broken adapter must not crash the kit
    # (accounting has no spec to judge — it says so instead of raising)
    statuses = {check.name: check.status for check in report.checks}
    assert statuses["accounting"] == "skipped"
    assert statuses["contract"] == "fail"
