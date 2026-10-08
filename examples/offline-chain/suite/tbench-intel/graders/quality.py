"""Versioned conversation-quality grader for the tbench-intel variant.

Thin wrapper around ``build_conversation_quality_grader`` (the P1
preset): twelve score-only dimensions over the conversation surface,
folded with the variant's pass threshold (aggregate < 0.6 ⇒ this layer
fails, carrying the same valid score and per-metric breakdown).

Anchor discipline on terminal tasks: these are single-instruction
tasks, so the multi-turn dimensions (identity/capability probes,
clarification, context retention, scope handling, complexity planning,
noise robustness, fork memory) have no anchors declared and skip
themselves with reasons — never a guessed score. The dimensions that
DO apply to a terminal trajectory (response brevity, tool selection,
instruction following, hallucination screening) carry per-task anchors
below and are judged for real.

Identity contract (aeval.verdict.loader):
- GRADER_ID / GRADER_VERSION / LAYER must match the suite declaration
  (``graders/quality.py@v1``, trajectory);
- ``VETO`` mirrors the suite declaration (this layer carries no veto:
  a quality miss lowers the score and can fail the layer via the
  threshold, but it never overturns another layer's pass — that is
  reserved for integrity).
"""

from __future__ import annotations

from typing import Any

from aeval.verdict.trajectory.presets import build_conversation_quality_grader
from aeval.verdict.trajectory.quality import (
    FormatSpec,
    HallucinationAnchor,
    QualityAnchors,
    ToolExpectation,
)

GRADER_ID = "tbench-quality"
GRADER_VERSION = "v1"
LAYER = "trajectory"
REQUIRED_FIELDS = ["events", "token_usage"]

# Mirrors the suite declaration (quality grader carries no veto).
VETO = False

# 综合分阈值：各维度平均分低于此值 ⇒ 本层判 fail（积分仍随结果携带）。
THRESHOLD = 0.6

# --- per-task rubric anchors (P1 channel: keyed by task_id) ---------------

ANCHORS: dict[str, QualityAnchors] = {
    "hello-world": QualityAnchors(
        tool_expectations=(
            # 建文件类指令应当经由 shell 工具完成（而非凭空作答）。
            ToolExpectation(
                trigger=r"file called hello\.txt|create a file",
                functions=("bash", "shell", "write"),
            ),
        ),
        format_specs=(
            # 指令点名 hello.txt；跟随回复应答到这个文件。
            FormatSpec(instruction=r"hello\.txt", validators=(r"hello\.txt",)),
        ),
        hallucination_anchors=(
            # topic 须在用户消息中出现（指令点名 files/folders），
            # 跟随回复里声称"建了 N 个文件"即编造。
            HallucinationAnchor(
                topic=r"files?|folders",
                invented_patterns=(r"created (?:\d+|several|three) files", r"hello\.py"),
                honest_patterns=(r"hello\.txt",),
            ),
        ),
    ),
    "sqlite-db-truncate": QualityAnchors(
        tool_expectations=(
            ToolExpectation(trigger=r"sqlite|database|trunc\.db", functions=("bash",)),
        ),
        format_specs=(
            # 交付物是 /app/recover.json；回复应答到交付物。
            FormatSpec(instruction=r"recover\.json", validators=(r"recover\.json",)),
        ),
        hallucination_anchors=(
            # 损坏库的恢复量不可声称"全部/100%"——只有恢复出的部分是
            # 事实；对损坏程度的探查性表述属诚实措辞。
            HallucinationAnchor(
                topic=r"rows|recover|corrupt",
                invented_patterns=(
                    r"all \d+ rows",
                    r"every (?:single )?row",
                    r"100% of (?:the )?rows",
                    r"fully restored the database",
                ),
                honest_patterns=(r"recover|corrupt|rows",),
            ),
        ),
    ),
    "openssl-selfsigned-cert": QualityAnchors(
        tool_expectations=(
            ToolExpectation(trigger=r"certificate|openssl|key", functions=("bash",)),
        ),
        format_specs=(
            # 指令点名 key 与证书文件、以及证书的 Common Name；
            # 跟随回复应答到这两项规格。
            FormatSpec(instruction=r"server\.key", validators=(r"server\.key",)),
            FormatSpec(
                instruction=r"Common Name",
                validators=(r"dev-internal\.company\.local",),
            ),
        ),
        hallucination_anchors=(
            # 证书规格是 2048-bit / 365 天——声称其他规格即编造。
            HallucinationAnchor(
                topic=r"certificate|key",
                invented_patterns=(r"4096-bit", r"730 days", r"two years"),
                honest_patterns=(r"2048-bit|365|server\.(?:key|crt)",),
            ),
        ),
    ),
}

_IMPLS = {
    task: build_conversation_quality_grader(
        GRADER_ID,
        GRADER_VERSION,
        veto=VETO,
        threshold=THRESHOLD,
        anchors=anchors,
    )
    for task, anchors in ANCHORS.items()
}


async def grade(record: Any) -> Any:
    from aeval.contracts import GradeResult, Score

    task_id = (
        getattr(getattr(record, "coordinates", None), "task_id", None)
        or "unknown"
    )
    impl = _IMPLS.get(task_id)
    if impl is None:
        reason = (
            f"no conversation-quality anchors declared for task {task_id!r} — "
            "this layer refuses to judge without a rubric"
        )
        return GradeResult(
            grader_id=GRADER_ID,
            grader_version=GRADER_VERSION,
            layer=LAYER,
            veto=VETO,
            score=Score(value=None, valid=False, invalid_reasons=[reason]),
            status="cannot_judge",
            reasons=[reason],
            metrics=None,
        )
    return await impl.grade(record)
