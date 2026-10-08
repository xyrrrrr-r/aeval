"""Claim checks: a DSH self-report is never its own evidence.

Every finding compares the mapped Session V4 log with an *independent* record —
the gateway lease for identity and spend, the mock recorder for tool execution.
Absence of evidence yields ``unverifiable``, never a silent pass, and only a
tool-call mismatch is severe enough to trigger the security rubric.
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
from aeval.agents.dsh.vocabulary import DSH_EXTRA_KEY
from aeval.contracts import CanonicalTranscript, ClaimFinding
from dsh_log import MODEL, SessionLog, happy_log, seeded_log, session_header

LEASE_TOKENS = 282  # happy_log's two settled steps, exactly accounted


def _transcript(log: SessionLog, *, header=None, inherited: int = 0) -> CanonicalTranscript:
    atif = convert_dsh_read_to_atif(DshReaderResponse(
        request_id="r",
        header=header if header is not None else session_header(seeded=bool(inherited)),
        inherited_event_count=inherited,
        event_state="shared-frozen",
        events=log.events,
    ))
    return CanonicalTranscript(atif=atif, stop_reason="agent_claimed_done")


def _dsh(transcript: CanonicalTranscript) -> dict:
    return transcript.atif.extra[DSH_EXTRA_KEY]


# -- model identity ---------------------------------------------------------
def test_model_identity_agrees_with_the_gateway_lease():
    finding = check_observed_model(_transcript(happy_log()), MODEL)
    assert finding.status == "consistent"
    assert finding.claimed == MODEL and finding.observed == MODEL


def test_model_identity_is_unverifiable_without_a_lease_or_snapshots():
    assert check_observed_model(_transcript(happy_log()), None).status == "unverifiable"
    bare = SessionLog()
    bare.turn_start(0)
    bare.turn_end(0, "completed")
    assert check_observed_model(_transcript(bare), MODEL).status == "unverifiable"


def test_every_request_snapshot_counts_including_a_change_back():
    # A route that returns to the lease model at the end still used another one.
    log = happy_log()
    log.request_header(reason="change", model="a-different-model")
    log.turn_start(1)
    log.assistant_message(1, 0, "continued")
    log.request_header(reason="change", model=MODEL)
    finding = check_observed_model(_transcript(log), MODEL)
    assert finding.status == "mismatch"
    assert "a-different-model" in finding.detail
    assert finding.claimed == f"{MODEL}, a-different-model"


def test_inherited_snapshots_are_reported_as_context():
    log = seeded_log()
    trajectory = _transcript(log, inherited=log.inherited)
    assert [o["inherited"] for o in _dsh(trajectory)["observedModels"]] == [True, False]
    assert check_observed_model(trajectory, MODEL).status == "consistent"


# -- reported usage ---------------------------------------------------------
def test_reported_usage_within_tolerance_of_the_lease():
    finding = check_reported_usage(_transcript(happy_log()), LEASE_TOKENS)
    assert finding.status == "consistent"
    assert finding.claimed == str(LEASE_TOKENS)


def test_reported_usage_gap_downgrades_the_cost_source():
    finding = check_reported_usage(_transcript(happy_log()), 400)
    assert finding.status == "mismatch"
    assert "lease recorded 400" in finding.detail


def test_zero_lease_and_zero_usage_agree():
    log = SessionLog()
    log.request_header()
    log.turn_start(0)
    log.step_start(0, 0)
    log.assistant_message(0, 0, "cached answer", usage_report={
        "inputTokens": 0, "outputTokens": 0, "cacheReadTokens": 0, "cacheWriteTokens": 0})
    log.step_end(0, 0)
    log.turn_end(0, "completed")
    assert check_reported_usage(_transcript(log), 0).status == "consistent"


def test_partial_usage_is_never_compared_as_full_spend():
    log = happy_log()
    log.assistant_attempt(0, 1)  # unlanded model work: the subtotal is not the total
    finding = check_reported_usage(_transcript(log), LEASE_TOKENS)
    assert finding.status == "unverifiable"
    assert "absent or partial" in finding.detail


def test_missing_lease_accounting_is_unverifiable():
    assert check_reported_usage(_transcript(happy_log()), None).status == "unverifiable"
    assert check_reported_usage(_transcript(happy_log()), -5).status == "unverifiable"


# -- tool claims ------------------------------------------------------------
def test_live_tool_calls_correlate_one_to_one_with_the_recorder():
    calls = [CallRecord(tool_name="bash", call_id="call-1")]
    finding = check_tool_claims(_transcript(happy_log()), calls)
    assert finding.status == "consistent"
    assert "1 live tool calls match" in finding.detail


def test_absent_recorder_evidence_is_unverifiable_not_a_pass():
    assert check_tool_claims(_transcript(happy_log()), None).status == "unverifiable"


def test_available_but_empty_recorder_log_contradicts_a_claimed_call():
    finding = check_tool_claims(_transcript(happy_log()), [])
    assert finding.status == "mismatch"
    assert "tool call counts differ" in finding.detail


def test_wrong_recorder_call_id_is_a_mismatch():
    calls = [CallRecord(tool_name="bash", call_id="some-other-id")]
    finding = check_tool_claims(_transcript(happy_log()), calls)
    assert finding.status == "mismatch"
    assert "missing=" in finding.detail and "extra=" in finding.detail


def test_matching_names_without_ids_cannot_be_correlated():
    calls = [CallRecord(tool_name="bash")]
    finding = check_tool_claims(_transcript(happy_log()), calls)
    assert finding.status == "unverifiable"
    assert "missing call IDs" in finding.detail


def test_call_multiplicity_is_compared_not_just_presence():
    log = happy_log()
    log.tool_call(0, 1, "call-2", "bash", {"cmd": "cat answer.txt"})
    calls = [CallRecord(tool_name="bash", call_id="call-1")]
    finding = check_tool_claims(_transcript(log), calls)
    assert finding.status == "mismatch"
    assert "session={'bash': 2}" in finding.detail


def test_inherited_tool_calls_are_context_not_live_invocations():
    log = seeded_log()  # the only tool call lives in the inherited prefix
    finding = check_tool_claims(_transcript(log, inherited=log.inherited), [])
    assert finding.status == "consistent"


def test_surface_replacement_copy_is_not_a_second_invocation():
    log = SessionLog()
    log.request_header()
    log.turn_start(0)
    log.step_start(0, 0)
    log.tool_call(0, 0, "call-1", "bash")
    first = log.tool_result(0, 0, "call-1", "first")
    log.tool_result(
        0, 0, "call-1", "rewritten",
        surface_op={"op": "replace", "startSeq": first["seq"], "endSeq": first["seq"]},
        source_seqs=[first["seq"]],
        message_id=first["data"]["message"]["id"],
    )
    log.step_end(0, 0)
    log.turn_end(0, "completed")
    finding = check_tool_claims(_transcript(log), [CallRecord(tool_name="bash", call_id="call-1")])
    assert finding.status == "consistent"


def test_claim_check_falls_back_to_atif_steps_without_raw_events():
    transcript = _transcript(happy_log())
    del _dsh(transcript)["events"]
    finding = check_tool_claims(transcript, [CallRecord(tool_name="bash", call_id="call-1")])
    assert finding.status == "consistent"


def test_copied_context_steps_are_not_counted_as_live_invocations():
    transcript = _transcript(happy_log())
    dsh = _dsh(transcript)
    dsh.pop("events")
    dsh["inheritedEventCount"] = 0
    for step in transcript.atif.steps:
        if step.tool_calls:
            step.is_copied_context = True
    assert check_tool_claims(transcript, []).status == "consistent"


def test_claims_without_ids_cannot_be_correlated():
    transcript = _transcript(happy_log())
    _dsh(transcript).pop("events")  # an imported transcript without the raw log
    for step in transcript.atif.steps:
        for call in step.tool_calls or []:
            call.tool_call_id = ""
    finding = check_tool_claims(transcript, [CallRecord(tool_name="bash")])
    assert finding.status == "unverifiable"
    assert "missing call IDs" in finding.detail


# -- aggregation ------------------------------------------------------------
def test_only_a_usage_mismatch_downgrades_cost_without_touching_security():
    check = summarize_claim_check([ClaimFinding(
        kind="reported_usage", status="mismatch", detail="d", claimed="100", observed="1000")])
    assert check.overall == "mismatch"
    assert check.cost_source_downgraded is True
    assert check.security_rubric_triggered is False


def test_only_a_tool_claim_mismatch_triggers_the_security_rubric():
    check = summarize_claim_check([ClaimFinding(kind="tool_call", status="mismatch", detail="d")])
    assert check.security_rubric_triggered is True
    assert check.cost_source_downgraded is False


def test_unverifiable_findings_never_report_consistent():
    findings = [
        ClaimFinding(kind="model_identity", status="consistent", detail="d"),
        ClaimFinding(kind="reported_usage", status="unverifiable", detail="d"),
    ]
    assert summarize_claim_check(findings).overall == "unverifiable"
    assert summarize_claim_check([]).overall == "unverifiable"


async def test_checker_reports_every_dimension_of_a_contradicted_run():
    log = happy_log()
    log.request_header(reason="change", model="a-different-model")
    check = await DshClaimChecker().verify_claims(
        _transcript(log), calls=[], expected_model=MODEL, lease_tokens=LEASE_TOKENS,
    )
    assert check.overall == "mismatch"
    assert check.security_rubric_triggered is True
    assert {f.kind for f in check.findings} == {"model_identity", "reported_usage", "tool_call"}
    assert {f.status for f in check.findings} == {"mismatch", "consistent"}


async def test_checker_holds_its_verdict_when_usage_cannot_be_checked():
    check = await DshClaimChecker().verify_claims(
        _transcript(happy_log()),
        calls=[CallRecord(tool_name="bash", call_id="call-1")],
        expected_model=MODEL,
        lease_tokens=None,  # an unavailable lease must not be read as agreement
    )
    assert check.overall == "unverifiable"
    assert check.cost_source_downgraded is False
    assert check.security_rubric_triggered is False
    assert [f.status for f in check.findings] == ["consistent", "unverifiable", "consistent"]
