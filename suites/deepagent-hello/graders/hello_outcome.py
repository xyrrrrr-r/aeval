"""Versioned outcome grader for the e2e-hello suite.

Grades the ONE outcome that matters in this suite: the observable
``result`` — collected as ``observable:result`` at
``observables/result.json`` with the exact serialization
``{"name": "result", "value": <string>}`` — must contain the string
``hello``. The grader verifies the CONTENT ADDRESS (sha256) of the
collected artifact against the digest of the expected serialization,
so grading never depends on re-reading files: the sealed record's
artifact reference is the evidence.

Identity contract (aeval.verdict.loader):
- GRADER_ID / GRADER_VERSION must be non-empty and match the suite
  declaration (``graders/hello_outcome.py@v1``);
- LAYER must agree with the suite's declared layer;
- REQUIRED_FIELDS are transcript completeness fields: this grader
  attributes an outcome to a run only when the canonical transcript's
  events AND token usage are fully captured (a partial token count can
  flip cost metrics — partial is blocking, not ignorable);
- ``grade`` must be a coroutine function taking the sealed TrialRecord.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

GRADER_ID = "hello-outcome"
GRADER_VERSION = "v1"
LAYER = "outcome"
REQUIRED_FIELDS = ["events", "token_usage"]

OBSERVABLE_NAME = "result"
EXPECTED_VALUE = "hello"
EXPECTED_ARTIFACT_SHA256 = hashlib.sha256(
    json.dumps(
        {"name": OBSERVABLE_NAME, "value": EXPECTED_VALUE},
        sort_keys=True, ensure_ascii=False,
    ).encode("utf-8")
).hexdigest()


def _artifact_serialization(value: Any) -> bytes:
    """The collector's exact serialization of one observable artifact."""
    return json.dumps(
        {"name": OBSERVABLE_NAME, "value": value},
        sort_keys=True, ensure_ascii=False,
    ).encode("utf-8")


async def grade(record: Any) -> Any:
    """Score the sealed trial record: 1.0 iff the result is exactly hello."""
    from aeval.contracts import GradeResult, Score

    artifacts = getattr(record, "artifacts", None) or {}
    ref = artifacts.get(f"observable:{OBSERVABLE_NAME}")
    if ref is None:
        return GradeResult(
            grader_id=GRADER_ID,
            grader_version=GRADER_VERSION,
            layer=LAYER,
            score=Score(value=0.0),
            status="fail",
            reasons=[
                f"observable:{OBSERVABLE_NAME} artifact is missing from the "
                "sealed record — the trial produced no gradeable result"
            ],
        )

    observed_digest = getattr(ref, "sha256", "")
    if observed_digest != EXPECTED_ARTIFACT_SHA256:
        return GradeResult(
            grader_id=GRADER_ID,
            grader_version=GRADER_VERSION,
            layer=LAYER,
            score=Score(value=0.0),
            status="fail",
            reasons=[
                f"observable:{OBSERVABLE_NAME} content digest "
                f"{observed_digest} != expected {EXPECTED_ARTIFACT_SHA256} "
                f"(expected value {EXPECTED_VALUE!r})"
            ],
        )

    return GradeResult(
        grader_id=GRADER_ID,
        grader_version=GRADER_VERSION,
        layer=LAYER,
        score=Score(value=1.0),
        status="pass",
        reasons=[
            f"observable:{OBSERVABLE_NAME} content digest matches the "
            f"expected serialization of {EXPECTED_VALUE!r}"
        ],
    )
