"""Versioned outcome grader for the sbench-pilot suite.

Grades the ONE outcome every service case declares: the case's
execution script (the per-category generated ``tests/check_<category>.py``,
run by ``tests/test.sh``) publishing the observable ``reward``
(``file:/logs/verifier/reward.txt``) with the collector's exact
serialization ``{"name": "reward", "value": <string>}``.

Content-address grading, exactly like the tbench/aeval-intel outcome
graders: the sealed record's artifact reference is the evidence, so
grading never re-reads the sandbox. Three outcomes:

* value ``"1"`` — the case's checks passed: score 1.0, status ``pass``;
* value ``"0"`` — the checks ran and failed (including "engine
  unreachable" — a deployment dependency missing, reason recorded in
  the verifier log): score 0.0, ``fail``;
* anything else, including a missing artifact — the script never
  produced a decidable reward: ``cannot_judge``.

Identity contract (aeval.verdict.loader): GRADER_ID/GRADER_VERSION
must be non-empty and match the suite declaration
(``graders/sbench_outcome.py@v1``).
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

GRADER_ID = "sbench-outcome"
GRADER_VERSION = "v1"
LAYER = "outcome"
REQUIRED_FIELDS = ["events", "token_usage"]

OBSERVABLE_NAME = "reward"
PASS_VALUE = "1"
FAIL_VALUE = "0"


def _serialization(value: Any) -> bytes:
    """The collector's exact serialization of one observable artifact."""
    return json.dumps(
        {"name": OBSERVABLE_NAME, "value": value},
        sort_keys=True, ensure_ascii=False,
    ).encode("utf-8")


EXPECTED_ARTIFACT_SHA256 = {
    PASS_VALUE: hashlib.sha256(_serialization(PASS_VALUE)).hexdigest(),
    FAIL_VALUE: hashlib.sha256(_serialization(FAIL_VALUE)).hexdigest(),
}


def _result(score: float | None, status: str, reason: str) -> Any:
    from aeval.contracts import GradeResult, Score

    return GradeResult(
        grader_id=GRADER_ID,
        grader_version=GRADER_VERSION,
        layer=LAYER,
        score=(
            Score(value=score) if score is not None
            else Score(valid=False, invalid_reasons=[reason])
        ),
        status=status,
        reasons=[reason],
    )


async def grade(record: Any) -> Any:
    """Score the sealed trial record from the case script's reward."""
    artifacts = getattr(record, "artifacts", None) or {}
    ref = artifacts.get(f"observable:{OBSERVABLE_NAME}")
    if ref is None:
        return _result(
            None,
            "cannot_judge",
            f"observable:{OBSERVABLE_NAME} is missing from the sealed record — "
            "the case script never published a reward, so the trial has "
            "no decidable outcome",
        )

    observed_digest = getattr(ref, "sha256", "")
    for value, expected in EXPECTED_ARTIFACT_SHA256.items():
        if observed_digest == expected:
            if value == PASS_VALUE:
                return _result(
                    1.0, "pass",
                    "case execution script reward is 1 (all checks passed)",
                )
            return _result(
                0.0, "fail",
                "case execution script reward is 0 (the case's checks "
                "failed — see the verifier log for reasons)",
            )

    return _result(
        None,
        "cannot_judge",
        f"observable:{OBSERVABLE_NAME} content digest {observed_digest} is "
        f"neither the pass ({PASS_VALUE!r}) nor the fail ({FAIL_VALUE!r}) "
        "serialization — the reward artifact is malformed",
    )
