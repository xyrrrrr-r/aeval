"""Turn-level trajectory analysis（轨迹分析模块）.

以 **turn**（一轮 = 一条用户消息 + 其后的全部 agent 回复与工具调
用）为切面拆开轨迹，并给每个 turn 打分。分数只来自两处，没有第三
套逻辑：

- **归因分**：质量指标的 ``details()``（``ProbeDetail``）——同一批
  metric 对象、同一份密封证据，轨迹级 ``evaluate`` 折叠的就是这些
  判分，turn 面板只是把它们落回各自的轮次。
- **结构分**：只陈述密封事实（是否应答、工具调用是否闭环、回复长
  度），不新增判分语义；fork 复制上下文轮标记为「记忆基底」，不进
  live 归因（与幻觉判分的 live-only 口径一致）。

渲染层（:mod:`aeval.metrics.trajectory_panel`）只消费本模块的输
出，不参与计算。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Sequence

from aeval.verdict.trajectory.base import (
    ToolEvent,
    TrajectoryEvidence,
    TrajectoryMessage,
    _turn_marker_of,
    user_messages_from_steps,
)
from aeval.verdict.trajectory.metrics import MetricOutcome
from aeval.verdict.trajectory.quality import ProbeDetail, collect_turn_details

__all__ = [
    "Turn",
    "TurnScore",
    "TurnAnalysis",
    "segment_turns",
    "analyze_turns",
]


@dataclass(frozen=True)
class Turn:
    """一轮对话：用户消息 + 其后的回复与工具调用。

    ``copied`` = 用户消息是 fork 复制上下文（父会话引入的事实 = 记
    忆基底）；live 轮才是被测的会话面。
    """

    index: int  # 1-based，时间序连续编号（含记忆基底轮）
    copied: bool
    user_step_id: int
    user_text: str
    replies: tuple[TrajectoryMessage, ...]
    tool_events: tuple[ToolEvent, ...]
    turn_marker: int | None


@dataclass(frozen=True)
class TurnScore:
    """一个 turn 的打分：归因分 + 结构事实。

    ``score`` 是归因分均值（该轮命中的探针/锚点判分）；没有命中任
    何锚点的轮次为 ``None``（未评测，不得着色成任何状态）。归因到
    记忆基底轮的判分不存在——幻觉/探针判分面是 live 会话。
    """

    details: tuple[ProbeDetail, ...]
    score: float | None
    answered: bool
    reply_chars: int
    tool_calls: int
    tool_loop: float | None  # 工具闭环比例；无调用为 None
    structure: float | None  # 结构分：应答(1/0) 与工具闭环的均值


@dataclass(frozen=True)
class TurnAnalysis:
    """一个 trial 的 turn 切面 + 轨迹级维度折叠（同对象 evaluate）。"""

    turns: tuple[Turn, ...]
    scores: tuple[TurnScore, ...]  # 与 turns 按下标对齐
    metric_outcomes: tuple[MetricOutcome, ...]


def segment_turns(evidence: TrajectoryEvidence) -> tuple[Turn, ...]:
    """按用户面消息切分 turn。

    规则确定性：每条非空 user 面消息开一轮，其后到下一条 user 面消
    息之前的 agent 回复与工具事件都归该轮；首条用户消息之前的步骤
    （系统提示等）不构成 turn。``copied`` 取该轮用户步骤的
    ``is_copied_context``。
    """
    steps = list(evidence.transcript.atif.steps or [])
    user_steps = {
        message.step_id: message
        for message in user_messages_from_steps(steps)
    }
    if not user_steps:
        return ()
    boundaries = sorted(user_steps)
    tools_by_step: dict[int, list[ToolEvent]] = {}
    for event in evidence.tool_events:
        tools_by_step.setdefault(event.step_id, []).append(event)
    agent_by_step = {m.step_id: m for m in evidence.agent_messages}

    turns: list[Turn] = []
    for position, start in enumerate(boundaries):
        end = (
            boundaries[position + 1] - 1
            if position + 1 < len(boundaries)
            else steps[-1].step_id
        )
        user_step = next(
            s for s in steps if s.step_id == start
        )
        replies = tuple(
            agent_by_step[step.step_id]
            for step in steps
            if start < step.step_id <= end
            and step.step_id in agent_by_step
        )
        tool_events = tuple(
            event
            for step_id in range(start, end + 1)
            for event in tools_by_step.get(step_id, ())
        )
        turns.append(
            Turn(
                index=len(turns) + 1,
                copied=bool(getattr(user_step, "is_copied_context", False)),
                user_step_id=start,
                user_text=user_steps[start].text,
                replies=replies,
                tool_events=tool_events,
                turn_marker=_turn_marker_of(user_step),
            )
        )
    return tuple(turns)


def analyze_turns(
    evidence: TrajectoryEvidence,
    metrics: Sequence[Any] = (),
) -> TurnAnalysis:
    """切分 turn 并打分：归因分按 user_step_id 落回轮次。"""
    turns = segment_turns(evidence)
    details = collect_turn_details(list(metrics), evidence)
    by_user: dict[int, list[ProbeDetail]] = {}
    for detail in details:
        by_user.setdefault(detail.user_step_id, []).append(detail)

    scores: list[TurnScore] = []
    for turn in turns:
        attributed = tuple(by_user.get(turn.user_step_id, ()))
        score = (
            sum(d.score for d in attributed) / len(attributed)
            if attributed
            else None
        )
        answered = bool(turn.replies)
        reply_chars = sum(len(r.text) for r in turn.replies)
        tool_calls = len(turn.tool_events)
        tool_loop = (
            (
                sum(1 for e in turn.tool_events if e.observation_present)
                / tool_calls
            )
            if tool_calls
            else None
        )
        components = [1.0 if answered else 0.0]
        if tool_loop is not None:
            components.append(tool_loop)
        scores.append(
            TurnScore(
                details=attributed,
                score=score,
                answered=answered,
                reply_chars=reply_chars,
                tool_calls=tool_calls,
                tool_loop=tool_loop,
                structure=sum(components) / len(components),
            )
        )
    outcomes = tuple(metric.evaluate(evidence) for metric in metrics)
    return TurnAnalysis(
        turns=turns,
        scores=tuple(scores),
        metric_outcomes=outcomes,
    )
