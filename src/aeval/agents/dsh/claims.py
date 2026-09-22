"""Claim verification (plan §3): never trust agent self-reports.

Second-source discipline (L5):

- model identity: the session header's model must match the gateway
  lease's model — a mismatch is a claim_mismatch and removes the trial
  from the denominator;
- reported usage: session-reported tokens are compared against the
  gateway lease accounting; mismatch downgrades the cost source;
- tool claims: a claimed tool call with no matching mock call-log
  entry triggers the security rubric (final fail);
- "task finished" self-reports are never evidence on their own.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from aeval.contracts import (
    CanonicalTranscript,
    ClaimCheck,
    ClaimFinding,
    CompletenessStatus,
)

__all__ = [
    "DshClaimChecker",
    "check_observed_model",
    "check_reported_usage",
    "check_tool_claims",
    "summarize_claim_check",
]


@dataclass
class CallRecord:
    """One mock-recorder call entry (the second source of truth)."""

    tool_name: str
    call_id: str | None = None
    arguments: dict[str, Any] | None = None
    recorded_at: str | None = None


def _session_model(transcript: CanonicalTranscript) -> str | None:
    return getattr(transcript.atif.agent, "model_name", None)


def check_observed_model(
    transcript: CanonicalTranscript,
    expected_model: str | None,
) -> ClaimFinding:
    """Session model vs the frozen gateway-lease model."""
    claimed = _session_model(transcript)
    if expected_model is None:
        return ClaimFinding(
            kind="model_identity",
            status="unverifiable",
            detail="no gateway lease model recorded to compare against",
            claimed=claimed,
        )
    if claimed is None:
        return ClaimFinding(
            kind="model_identity",
            status="unverifiable",
            detail="session carries no model identity",
            claimed=None,
            observed=expected_model,
        )
    if claimed != expected_model:
        return ClaimFinding(
            kind="model_identity",
            status="mismatch",
            detail=(
                f"session model {claimed!r} differs from lease {expected_model!r}"
            ),
            claimed=claimed,
            observed=expected_model,
        )
    return ClaimFinding(
        kind="model_identity",
        status="consistent",
        detail=f"session model matches lease ({claimed})",
        claimed=claimed,
        observed=expected_model,
    )


def check_reported_usage(
    transcript: CanonicalTranscript,
    lease_tokens: int | None,
) -> ClaimFinding:
    """Session token usage vs gateway accounting."""
    if lease_tokens is None:
        return ClaimFinding(
            kind="reported_usage",
            status="unverifiable",
            detail="no gateway lease token accounting available",
        )
    reported = _reported_tokens(transcript)
    if reported is None:
        return ClaimFinding(
            kind="reported_usage",
            status="unverifiable",
            detail="session reports no token usage",
            observed=str(lease_tokens),
        )
    # Tolerance: lease accounting and session self-report may differ by
    # small bookkeeping overheads; a >5% relative gap is a mismatch.
    if lease_tokens == 0:
        gap_ok = reported == 0
    else:
        gap_ok = abs(reported - lease_tokens) / max(lease_tokens, 1) <= 0.05
    if not gap_ok:
        return ClaimFinding(
            kind="reported_usage",
            status="mismatch",
            detail=(
                f"session reports {reported} tokens, lease recorded {lease_tokens}"
            ),
            claimed=str(reported),
            observed=str(lease_tokens),
        )
    return ClaimFinding(
        kind="reported_usage",
        status="consistent",
        detail=f"usage within tolerance of lease ({reported} vs {lease_tokens})",
        claimed=str(reported),
        observed=str(lease_tokens),
    )


def _reported_tokens(transcript: CanonicalTranscript) -> int | None:
    metrics = getattr(transcript.atif, "final_metrics", None)
    if metrics is None:
        return None
    for attr in ("total_tokens", "tokens", "token_usage"):
        value = getattr(metrics, attr, None)
        if isinstance(value, int):
            return value
        if isinstance(value, dict):
            total = value.get("total") or value.get("input", 0) + value.get("output", 0)
            if isinstance(total, int):
                return total
    return None


def check_tool_claims(
    transcript: CanonicalTranscript,
    calls: list[CallRecord],
) -> ClaimFinding:
    """Every claimed tool call must exist in the mock call log."""
    claimed_calls: list[tuple[str, str]] = []
    for step in transcript.atif.steps:
        for call in step.tool_calls or []:
            claimed_calls.append((call.function_name, call.tool_call_id))
    if not claimed_calls:
        return ClaimFinding(
            kind="tool_call",
            status="consistent",
            detail="session claims no tool calls",
        )
    if not calls:
        return ClaimFinding(
            kind="tool_call",
            status="mismatch",
            detail=(
                f"session claims {len(claimed_calls)} tool calls but the mock "
                "call log is empty — fabricated tool activity"
            ),
        )
    recorded_names = {c.tool_name for c in calls}
    unmatched = [name for name, _ in claimed_calls if name not in recorded_names]
    if unmatched:
        return ClaimFinding(
            kind="tool_call",
            status="mismatch",
            detail=(
                f"claimed tool calls absent from the mock call log: "
                f"{sorted(set(unmatched))[:5]}"
            ),
        )
    return ClaimFinding(
        kind="tool_call",
        status="consistent",
        detail=f"all {len(claimed_calls)} claimed tool calls appear in the log",
    )


def summarize_claim_check(findings: list[ClaimFinding]) -> ClaimCheck:
    statuses = [f.status for f in findings]
    mismatch = any(s == "mismatch" for s in statuses)
    consistent = statuses and all(s == "consistent" for s in statuses)
    check = ClaimCheck(
        findings=findings,
        overall="mismatch" if mismatch else ("consistent" if consistent else "unverifiable"),
        cost_source_downgraded=any(
            f.kind == "reported_usage" and f.status == "mismatch" for f in findings
        ),
        security_rubric_triggered=any(
            f.kind == "tool_call" and f.status == "mismatch" for f in findings
        ),
    )
    return check


class DshClaimChecker:
    """Aggregate claim verification for one trial."""

    async def verify_claims(
        self,
        transcript: CanonicalTranscript,
        calls: list[CallRecord],
        *,
        expected_model: str | None = None,
        lease_tokens: int | None = None,
    ) -> ClaimCheck:
        findings = [
            check_observed_model(transcript, expected_model),
            check_reported_usage(transcript, lease_tokens),
            check_tool_claims(transcript, calls),
        ]
        return summarize_claim_check(findings)
