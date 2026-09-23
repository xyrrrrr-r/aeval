"""Compare DSH claims with independent gateway and mock-recorder evidence.

Absence of accounting, recorder IDs, or a recorder log is not corroboration.
Model identity includes every request/header snapshot, not just the last route.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from typing import Any

from aeval.contracts import CanonicalTranscript, ClaimCheck, ClaimFinding, DSH_EXTRA_KEY

__all__ = [
    "CallRecord", "DshClaimChecker", "check_observed_model", "check_reported_usage",
    "check_tool_claims", "summarize_claim_check",
]


@dataclass
class CallRecord:
    """One independent mock-recorder call entry; absent IDs cannot prove identity."""

    tool_name: str
    call_id: str | None = None
    arguments: dict[str, Any] | None = None
    recorded_at: str | None = None


def _dsh(transcript: CanonicalTranscript) -> dict[str, Any]:
    return (transcript.atif.extra or {}).get(DSH_EXTRA_KEY, {})


def check_observed_model(transcript: CanonicalTranscript, expected_model: str | None) -> ClaimFinding:
    """Check ALL logged request configs, including changes back to the lease model."""
    snapshots = _dsh(transcript).get("observedModels", [])
    models = list(dict.fromkeys(s["config"]["model"] for s in snapshots))
    claimed = ", ".join(models) if models else None
    if expected_model is None:
        return ClaimFinding(kind="model_identity", status="unverifiable",
                            detail="no gateway lease model recorded to compare against", claimed=claimed)
    if not models:
        return ClaimFinding(kind="model_identity", status="unverifiable",
                            detail="session has no request/header model identity", observed=expected_model)
    mismatches = [model for model in models if model != expected_model]
    return ClaimFinding(
        kind="model_identity", status="mismatch" if mismatches else "consistent",
        detail=(f"request/header models {mismatches!r} differ from lease {expected_model!r}" if mismatches
                else f"all {len(snapshots)} request/header snapshots match lease ({expected_model})"),
        claimed=claimed, observed=expected_model,
    )


def _reported_tokens(transcript: CanonicalTranscript) -> int | None:
    usage = _dsh(transcript).get("usage", {})
    total = usage.get("totalTokens")
    # Subtotals from incomplete sessions must not be compared as full lease usage.
    if usage.get("status") == "ok" and type(total) is int and total >= 0:
        return total
    return None


def check_reported_usage(transcript: CanonicalTranscript, lease_tokens: int | None) -> ClaimFinding:
    if type(lease_tokens) is not int or lease_tokens < 0:
        return ClaimFinding(kind="reported_usage", status="unverifiable",
                            detail="no valid gateway lease token accounting available")
    reported = _reported_tokens(transcript)
    if reported is None:
        return ClaimFinding(kind="reported_usage", status="unverifiable",
                            detail="session exact live token usage is absent or partial", observed=str(lease_tokens))
    gap_ok = reported == 0 if lease_tokens == 0 else abs(reported - lease_tokens) / lease_tokens <= 0.05
    return ClaimFinding(
        kind="reported_usage", status="consistent" if gap_ok else "mismatch",
        detail=(f"usage within tolerance of lease ({reported} vs {lease_tokens})" if gap_ok
                else f"session reports {reported} tokens, lease recorded {lease_tokens}"),
        claimed=str(reported), observed=str(lease_tokens),
    )


def check_tool_claims(transcript: CanonicalTranscript, calls: list[CallRecord] | None) -> ClaimFinding:
    """One-to-one ID AND name matching, with multiplicity, against a complete log.

    None means unavailable evidence; [] is an explicitly available empty log.
    Inherited context and surface replacement copies are not live invocations.
    """
    dsh = _dsh(transcript)
    events = dsh.get("events")
    if events is not None:
        inherited = dsh.get("inheritedEventCount", 0)
        claimed = [(e["data"]["name"], e["data"].get("callId")) for e in events[inherited:]
                   if e["type"] == "tool/call"]
    else:
        claimed = [(c.function_name, c.tool_call_id) for s in transcript.atif.steps
                   if not s.is_copied_context for c in s.tool_calls or []]
    if calls is None:
        return ClaimFinding(kind="tool_call", status="unverifiable", detail="mock call log is unavailable")
    recorded = [(c.tool_name, c.call_id) for c in calls]
    claimed_names = Counter(name for name, _ in claimed)
    recorded_names = Counter(name for name, _ in recorded)
    if claimed_names != recorded_names:
        return ClaimFinding(
            kind="tool_call", status="mismatch",
            detail=f"tool call counts differ: session={dict(claimed_names)!r}, mock={dict(recorded_names)!r}",
        )
    if not claimed:
        return ClaimFinding(kind="tool_call", status="consistent", detail="session and available mock log both contain no live tool calls")
    # Known IDs can disprove a match even when other rows have missing IDs.
    known_claims = Counter((name, call_id) for name, call_id in claimed if call_id)
    known_records = Counter((name, call_id) for name, call_id in recorded if call_id)
    unidentified_claims = Counter(name for name, call_id in claimed if not call_id)
    unidentified_records = Counter(name for name, call_id in recorded if not call_id)
    missing = known_claims - known_records
    extra = known_records - known_claims
    missing_by_name = Counter()
    extra_by_name = Counter()
    for (name, _), count in missing.items():
        missing_by_name[name] += count
    for (name, _), count in extra.items():
        extra_by_name[name] += count
    if missing_by_name - unidentified_records or extra_by_name - unidentified_claims:
        return ClaimFinding(kind="tool_call", status="mismatch",
                            detail=f"tool call IDs/counts differ: missing={dict(missing)!r}, extra={dict(extra)!r}")
    if unidentified_claims or unidentified_records:
        return ClaimFinding(kind="tool_call", status="unverifiable",
                            detail="tool names/counts agree but missing call IDs prevent correlation")
    return ClaimFinding(kind="tool_call", status="consistent",
                        detail=f"all {len(claimed)} live tool calls match recorder IDs, names and counts")


def summarize_claim_check(findings: list[ClaimFinding]) -> ClaimCheck:
    statuses = [f.status for f in findings]
    mismatch = any(s == "mismatch" for s in statuses)
    consistent = bool(statuses) and all(s == "consistent" for s in statuses)
    return ClaimCheck(
        findings=findings,
        overall="mismatch" if mismatch else ("consistent" if consistent else "unverifiable"),
        cost_source_downgraded=any(f.kind == "reported_usage" and f.status == "mismatch" for f in findings),
        security_rubric_triggered=any(f.kind == "tool_call" and f.status == "mismatch" for f in findings),
    )


class DshClaimChecker:
    async def verify_claims(
        self, transcript: CanonicalTranscript, calls: list[CallRecord] | None, *,
        expected_model: str | None = None, lease_tokens: int | None = None,
    ) -> ClaimCheck:
        return summarize_claim_check([
            check_observed_model(transcript, expected_model),
            check_reported_usage(transcript, lease_tokens),
            check_tool_claims(transcript, calls),
        ])
