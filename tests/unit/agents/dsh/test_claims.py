"""Claim check tests (plan §7 rows 7/9): self-reports are never evidence.

- model mismatch ⇒ claim_mismatch ⇒ out of the denominator;
- usage mismatch ⇒ cost source downgraded (never a denominator change);
- claimed tool call with no mock log entry ⇒ security rubric fail.
"""

from __future__ import annotations

from aeval.agents.dsh.atif_mapper import convert_dsh_read_to_atif
from aeval.agents.dsh.bridge import DshReaderResponse
from aeval.agents.dsh.claims import (
    CallRecord,
    DshClaimChecker,
    check_observed_model,
    check_reported_usage,
    check_tool_claims,
    summarize_claim_check,
)
from aeval.contracts import CanonicalTranscript


def _transcript(events, header=None) -> CanonicalTranscript:
    response = DshReaderResponse(
        request_id="r",
        header=header or {"sessionId": "s", "agent": "dsh"},
        inherited_event_count=0,
        event_state="shared-frozen",
        events=events,
    )
    atif = convert_dsh_read_to_atif(response)
    return CanonicalTranscript(atif=atif, stop_reason="agent_exit_0")


def test_observed_model_mismatch():
    ct = _transcript([], header={"sessionId": "s", "model": "vendor-b"})
    finding = check_observed_model(ct, expected_model="vendor-a")
    assert finding.status == "mismatch"
    assert finding.claimed == "vendor-b"
    assert finding.observed == "vendor-a"


def test_observed_model_consistent_and_unverifiable():
    ct = _transcript([], header={"sessionId": "s", "model": "vendor-a"})
    assert check_observed_model(ct, "vendor-a").status == "consistent"
    # no lease recorded → unverifiable, never a silent pass
    assert check_observed_model(ct, None).status == "unverifiable"
    bare = _transcript([])  # header without model
    assert check_observed_model(bare, "vendor-a").status == "unverifiable"


def test_usage_mismatch_downgrades_cost_source_only():
    from aeval.contracts import ClaimFinding

    finding = ClaimFinding(
        kind="reported_usage", status="mismatch", detail="d",
        claimed="100", observed="1000",
    )
    check = summarize_claim_check([finding])
    assert check.overall == "mismatch"
    assert check.cost_source_downgraded is True
    assert check.security_rubric_triggered is False


def test_tool_claim_without_mock_log_is_security_fail():
    ct = _transcript([
        {"type": "message", "source": "user", "text": "q"},
        {"type": "tool_call", "callId": "c1", "name": "lookup"},
    ])
    finding = check_tool_claims(ct, calls=[])
    assert finding.status == "mismatch"
    assert "mock call log is empty" in finding.detail
    check = summarize_claim_check([finding])
    assert check.security_rubric_triggered is True


def test_tool_claim_absent_from_log_is_security_fail():
    ct = _transcript([
        {"type": "tool_call", "callId": "c1", "name": "lookup"},
    ])
    finding = check_tool_claims(ct, [CallRecord(tool_name="other_tool")])
    assert finding.status == "mismatch"
    assert "lookup" in finding.detail


def test_tool_claims_matching_log_pass():
    ct = _transcript([
        {"type": "tool_call", "callId": "c1", "name": "lookup"},
        {"type": "tool_call", "callId": "c2", "name": "search"},
    ])
    calls = [CallRecord(tool_name="lookup"), CallRecord(tool_name="search")]
    assert check_tool_claims(ct, calls).status == "consistent"


def test_no_tool_claims_is_consistent_even_with_empty_log():
    ct = _transcript([{"type": "message", "source": "user", "text": "q"}])
    assert check_tool_claims(ct, calls=[]).status == "consistent"


async def test_checker_aggregates_findings():
    ct = _transcript([
        {"type": "message", "source": "user", "text": "q"},
        {"type": "tool_call", "callId": "c1", "name": "lookup"},
    ], header={"sessionId": "s", "model": "vendor-b"})
    checker = DshClaimChecker()
    check = await checker.verify_claims(
        ct, calls=[], expected_model="vendor-a", lease_tokens=100,
    )
    assert check.overall == "mismatch"
    assert check.security_rubric_triggered is True
    kinds = {f.kind for f in check.findings}
    assert kinds == {"model_identity", "reported_usage", "tool_call"}


async def test_checker_all_consistent():
    ct = _transcript([
        {"type": "tool_call", "callId": "c1", "name": "lookup"},
    ], header={"sessionId": "s", "model": "vendor-a"})
    checker = DshClaimChecker()
    check = await checker.verify_claims(
        ct,
        [CallRecord(tool_name="lookup")],
        expected_model="vendor-a",
        lease_tokens=None,  # unverifiable usage does not flip the verdict
    )
    assert check.overall == "unverifiable"  # usage unverifiable drags overall
    assert check.cost_source_downgraded is False
    assert check.security_rubric_triggered is False
