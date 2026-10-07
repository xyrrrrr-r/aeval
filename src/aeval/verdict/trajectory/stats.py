"""轨迹采集（trajectory stats）——从密封轨迹提取报告与面板的数据面。

只陈述密封证据（sha256 校验过的 canonical transcript）里的事实：
每个字段要么来自 transcript/record，要么为 None 并附 missing 原因
（skip-with-reason 纪律，不编造）。两处消费同一份 ``TrajectoryStats``：

- 运行报告（markdown）：run 级聚合的「轨迹采集」节；
- 轨迹面板（HTML）：trial 级逐版块渲染（执行图谱/Token 脉冲/
  上下文压力/工具矩阵…）。

口径说明（诚实边界）：
- 「失败」= 工具调用无观测（observation 缺失）——密封证据里没有
  显式错误码，这是唯一可陈述的失败信号；
- 「重试」= 相邻的同名同参调用（后者计为重试）；
- 步骤耗时 = 相邻步骤时间戳差，含模型推理与工具执行，归因到该步
  的工具调用时是近似口径（面板如实标注「步骤耗时」）；
- 上下文占用率需要窗口大小——record/证据里没有；仅当调用方显式
  声明窗口时才计算（面板标注声明来源）。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Sequence

from aeval.contracts import TrialRecord
from aeval.verdict.trajectory.base import TrajectoryEvidence

__all__ = [
    "StepPoint",
    "ToolStat",
    "TrajectoryStats",
    "collect_stats",
]


def _parse_ts(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


@dataclass(frozen=True)
class StepPoint:
    """一步的采集点（时间线/Token 脉冲/上下文压力的共用序列）。"""

    step_id: int
    source: str                     # user | agent | system | developer
    copied: bool                    # fork 复制上下文（记忆基底）
    timestamp: str | None
    seconds_from_start: float | None
    duration_seconds: float | None  # 到下一步的时间差；末步 None
    prompt_tokens: int | None
    completion_tokens: int | None
    cached_tokens: int | None
    tool_names: tuple[str, ...]
    tool_calls: int
    observation_missing: bool       # 有调用但无观测


@dataclass(frozen=True)
class ToolStat:
    """一种工具的聚合（工具结果矩阵的一行）。"""

    name: str
    calls: int
    observed: int          # 有观测
    missing_obs: int       # 无观测（诚实失败信号）
    retries: int           # 相邻同名同参调用（后者计为重试）
    step_seconds: float | None      # 含该工具调用的步骤耗时合计
    avg_seconds: float | None
    longest_seconds: float | None


@dataclass(frozen=True)
class TrajectoryStats:
    """一个 trial 的轨迹采集结果。"""

    trial_id: str
    task_id: str
    model: str | None
    verdict: str | None
    stop_reason: str | None
    started_at: str | None
    wall_clock_seconds: float | None
    total_steps: int
    live_turns: int
    copied_turns: int
    total_prompt_tokens: int | None
    total_completion_tokens: int | None
    total_cached_tokens: int | None
    peak_input_tokens: int | None       # 单步最大输入（上下文峰值）
    peak_step_id: int | None
    steps: tuple[StepPoint, ...]
    tools: tuple[ToolStat, ...]
    tool_calls_total: int
    tool_missing_obs_total: int
    tool_retries_total: int
    longest_call: tuple[str, float] | None   # (工具, 步骤耗时秒)
    tool_time_share: float | None            # 工具步骤耗时 / 会话总时长
    missing: tuple[str, ...] = field(default_factory=tuple)

    @property
    def total_tokens(self) -> int | None:
        if self.total_prompt_tokens is None or self.total_completion_tokens is None:
            return None
        return self.total_prompt_tokens + self.total_completion_tokens


def collect_stats(
    record: TrialRecord, evidence: TrajectoryEvidence
) -> TrajectoryStats:
    """从密封证据采集一个 trial 的轨迹数据（纯函数、确定性）。"""
    missing: list[str] = []
    steps_raw = list(evidence.transcript.atif.steps or [])

    # --- 时间轴 ---
    timestamps = [_parse_ts(getattr(s, "timestamp", None)) for s in steps_raw]
    start = next((t for t in timestamps if t is not None), None)
    if start is None:
        missing.append("无可用时间戳：时长/耗时版块不可用")
    seconds_from_start = [
        (t - start).total_seconds() if (t is not None and start is not None)
        else None
        for t in timestamps
    ]
    durations: list[float | None] = []
    for i, t in enumerate(timestamps):
        nxt = timestamps[i + 1] if i + 1 < len(timestamps) else None
        if t is not None and nxt is not None and (nxt - t).total_seconds() >= 0:
            durations.append((nxt - t).total_seconds())
        else:
            durations.append(None)
    wall = None
    if start is not None:
        ends = [t for t in timestamps if t is not None]
        if len(ends) >= 2:
            span = (ends[-1] - ends[0]).total_seconds()
            wall = span if span >= 0 else None
    # 总时长以证据层的权威口径为准（与判分层同源），本地推导仅兜底。
    if evidence.wall_clock_seconds is not None:
        wall = evidence.wall_clock_seconds
    if wall is None:
        missing.append("时间戳不足两个端点：总时长不可用")

    # --- 工具事件（含重试与观测缺失） ---
    tool_events = list(evidence.tool_events)
    obs_by_call = {e.call_id: e.observation_present for e in tool_events}

    # 相邻同名同参 = 重试（按事件顺序比较 function_name + arguments）。
    retries_by_tool: dict[str, int] = {}
    prev: tuple[str, str] | None = None
    for event in tool_events:
        key_args = json.dumps(event.arguments, sort_keys=True,
                              ensure_ascii=False, default=str)
        sig = (event.function_name, key_args)
        if prev is not None and sig == prev:
            retries_by_tool[event.function_name] = (
                retries_by_tool.get(event.function_name, 0) + 1
            )
        prev = sig

    # 步骤耗时归因：含该工具调用的步骤，其 duration 记入该工具。
    seconds_by_tool: dict[str, list[float]] = {}
    longest_by_tool: dict[str, float] = {}
    for s, dur in zip(steps_raw, durations):
        names = {c.function_name for c in (s.tool_calls or [])}
        if not names or dur is None:
            continue
        for name in names:
            seconds_by_tool.setdefault(name, []).append(dur)
            longest_by_tool[name] = max(longest_by_tool.get(name, 0.0), dur)

    calls_by_tool: dict[str, int] = {}
    obs_by_tool: dict[str, int] = {}
    miss_by_tool: dict[str, int] = {}
    for event in tool_events:
        calls_by_tool[event.function_name] = (
            calls_by_tool.get(event.function_name, 0) + 1
        )
        if event.observation_present:
            obs_by_tool[event.function_name] = (
                obs_by_tool.get(event.function_name, 0) + 1
            )
        else:
            miss_by_tool[event.function_name] = (
                miss_by_tool.get(event.function_name, 0) + 1
            )
    tools = tuple(
        ToolStat(
            name=name,
            calls=calls_by_tool[name],
            observed=obs_by_tool.get(name, 0),
            missing_obs=miss_by_tool.get(name, 0),
            retries=retries_by_tool.get(name, 0),
            step_seconds=(
                sum(seconds_by_tool[name]) if name in seconds_by_tool else None
            ),
            avg_seconds=(
                sum(seconds_by_tool[name]) / len(seconds_by_tool[name])
                if name in seconds_by_tool else None
            ),
            longest_seconds=longest_by_tool.get(name),
        )
        for name in sorted(calls_by_tool)
    )
    longest_call: tuple[str, float] | None = None
    if longest_by_tool:
        name = max(longest_by_tool, key=lambda n: longest_by_tool[n])
        longest_call = (name, longest_by_tool[name])

    tool_seconds_total = (
        sum(sum(v) for v in seconds_by_tool.values()) if seconds_by_tool else None
    )
    tool_time_share = (
        tool_seconds_total / wall
        if (tool_seconds_total is not None and wall)
        else None
    )

    # --- Token 序列与峰值 ---
    step_points: list[StepPoint] = []
    peak_input: int | None = None
    peak_step: int | None = None
    live_turns = copied_turns = 0
    for s, ts_raw, sfs, dur in zip(
        steps_raw, timestamps, seconds_from_start, durations
    ):
        metrics = getattr(s, "metrics", None)
        prompt = getattr(metrics, "prompt_tokens", None) if metrics else None
        completion = (
            getattr(metrics, "completion_tokens", None) if metrics else None
        )
        cached = getattr(metrics, "cached_tokens", None) if metrics else None
        calls = list(s.tool_calls or [])
        obs_missing = bool(calls) and not any(
            obs_by_call.get(c.tool_call_id, False) for c in calls
        )
        copied = bool(getattr(s, "is_copied_context", False))
        source = s.source or ""
        text = (s.message or "").strip()
        if source == "user" and text:
            if copied:
                copied_turns += 1
            else:
                live_turns += 1
        if prompt is not None and (peak_input is None or prompt > peak_input):
            peak_input = prompt
            peak_step = s.step_id
        step_points.append(
            StepPoint(
                step_id=s.step_id,
                source=source,
                copied=copied,
                timestamp=getattr(s, "timestamp", None),
                seconds_from_start=sfs,
                duration_seconds=dur,
                prompt_tokens=prompt,
                completion_tokens=completion,
                cached_tokens=cached,
                tool_names=tuple(c.function_name for c in calls),
                tool_calls=len(calls),
                observation_missing=obs_missing,
            )
        )
    if peak_input is None:
        missing.append("步骤无 token 计量：Token/上下文版块不可用")

    final = getattr(evidence.transcript.atif, "final_metrics", None)
    total_prompt = getattr(final, "total_prompt_tokens", None) if final else None
    total_completion = (
        getattr(final, "total_completion_tokens", None) if final else None
    )
    total_cached = getattr(final, "total_cached_tokens", None) if final else None
    if total_prompt is None or total_completion is None:
        # 回退：逐步求和（仍然只来自密封证据）。
        p = [st.prompt_tokens for st in step_points if st.prompt_tokens is not None]
        c = [st.completion_tokens for st in step_points
             if st.completion_tokens is not None]
        if total_prompt is None and p:
            total_prompt = sum(p)
        if total_completion is None and c:
            total_completion = sum(c)
        if total_prompt is None or total_completion is None:
            missing.append("无 token 总量：Token 汇总不可用")

    model = None
    if record.observed_model is not None:
        model = record.observed_model.model or record.observed_model.provider

    return TrajectoryStats(
        trial_id=record.trial_id,
        task_id=record.coordinates.task_id,
        model=model,
        verdict=record.verdict,
        stop_reason=record.stop_reason,
        started_at=(
            start.isoformat() if start is not None else None
        ),
        wall_clock_seconds=wall,
        total_steps=len(steps_raw),
        live_turns=live_turns,
        copied_turns=copied_turns,
        total_prompt_tokens=total_prompt,
        total_completion_tokens=total_completion,
        total_cached_tokens=total_cached,
        peak_input_tokens=peak_input,
        peak_step_id=peak_step,
        steps=tuple(step_points),
        tools=tools,
        tool_calls_total=len(tool_events),
        tool_missing_obs_total=sum(miss_by_tool.values()),
        tool_retries_total=sum(retries_by_tool.values()),
        longest_call=longest_call,
        tool_time_share=tool_time_share,
        missing=tuple(missing),
    )
