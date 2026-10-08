"""自包含、离线交互的密封轨迹分析面板。"""

from __future__ import annotations

import html as _html
import json
from dataclasses import dataclass
from typing import Any, Sequence

from aeval.metrics.dashboard import _CSS
from aeval.verdict.trajectory.quality import ProbeDetail
from aeval.verdict.trajectory.stats import TrajectoryStats
from aeval.verdict.trajectory.turns import TurnAnalysis

__all__ = ["TrialPanel", "render_trajectory_html"]

_VERDICT_ZH = {
    "pass": "通过(pass)",
    "fail": "失败(fail)",
    "cannot_judge": "不可判(cannot_judge)",
}
_TIMELINE_W = 1000
_TIMELINE_LABEL_W = 190
_TIMELINE_PAD = 32
_POINT_BUDGET = 2000
_BUCKET_COUNT = 80
_SNIPPET = 360


@dataclass(frozen=True)
class TrialPanel:
    """一个试次的面板输入：标题、元信息、采集结果与 turn 切面。"""

    heading: str
    meta: str
    stats: TrajectoryStats
    turn_analysis: TurnAnalysis | None = None
    #: 密封证据自携带的上下文窗口（来自 ATIF agent 块 extra.dsh.context_window，
    #: 由 dsh driver 经 request/context 事件写入）。调用方未用 --context-window
    #: 声明时，面板回退到它换算占用率；None 表示证据未携带，绝不臆造。
    context_window: int | None = None


def _e(text: object) -> str:
    return _html.escape(str(text), quote=True)


def _int(value: int | None) -> str:
    return f"{value:,}" if value is not None else "—"


def _secs(value: float | None) -> str:
    return f"{value:.1f}s" if value is not None else "—"


def _mmss(seconds: float) -> str:
    seconds = max(0, int(seconds))
    return f"{seconds // 60}:{seconds % 60:02d}"


def _snippet(text: str, limit: int = _SNIPPET) -> str:
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _score_class(value: float | None) -> str:
    if value is None:
        return "gray"
    if value >= 1.0:
        return "green"
    if value >= 0.9:
        return "yellow"
    return "red"


def _score_text(value: float | None) -> str:
    return "未评测" if value is None else f"{value:.2f}"


def _safe_json(value: object) -> str:
    """JSON 载荷可嵌入 script 标签，且同输入保持同字节。"""
    return (
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        .replace("&", "\\u0026")
        .replace("<", "\\u003c")
        .replace(">", "\\u003e")
        .replace("\u2028", "\\u2028")
        .replace("\u2029", "\\u2029")
    )


def _time_axis(trials: Sequence[TrialPanel]) -> tuple[str, float, str]:
    """返回共享坐标轴模式、最大值和不可用说明。"""
    nonempty = [panel.stats.steps for panel in trials if panel.stats.steps]
    if not nonempty:
        return "step", 1.0, "所有试次均无步骤，无法绘制时间轴。"
    spans: list[float] = []
    for steps in nonempty:
        values = [step.seconds_from_start for step in steps]
        if (
            any(value is None or value < 0 for value in values)
            or any(b < a for a, b in zip(values, values[1:]))
        ):
            return "step", float(max(len(items) - 1 for items in nonempty) or 1), (
                "部分试次缺少或包含无效时间戳，已统一按步骤序对齐。"
            )
        span = float(values[-1] or 0.0)
        if span <= 0:
            return "step", float(max(len(items) - 1 for items in nonempty) or 1), (
                "部分试次没有有效的时间跨度，已统一按步骤序对齐。"
            )
        spans.append(span)
    return "time", max(spans), "所有试次按各自起点的相对执行时间对齐。"


def _tool_view(event: Any) -> dict[str, Any]:
    return {
        "name": event.function_name,
        "arguments": dict(event.arguments),
        "observation_present": event.observation_present,
    }


def _messages_and_turns(
    analysis: TurnAnalysis | None,
) -> tuple[dict[str, dict[str, Any]], dict[str, dict[str, Any]], dict[int, str]]:
    if analysis is None:
        return {}, {}, {}
    messages: dict[str, dict[str, Any]] = {}
    turns: dict[str, dict[str, Any]] = {}
    step_turn: dict[int, str] = {}
    for message in analysis.prologue:
        messages[str(message.step_id)] = {
            "source": message.source,
            "text": _snippet(message.text),
            "copied": message.copied,
        }
    for turn, score in zip(analysis.turns, analysis.scores):
        turn_id = str(turn.user_step_id)
        messages[turn_id] = {
            "source": "user",
            "text": _snippet(turn.user_text),
            "copied": turn.copied,
        }
        reply_ids: list[int] = []
        for message in turn.replies:
            messages[str(message.step_id)] = {
                "source": message.source,
                "text": _snippet(message.text),
                "copied": message.copied,
            }
            reply_ids.append(message.step_id)
        system_ids: list[int] = []
        for message in turn.system_messages:
            messages[str(message.step_id)] = {
                "source": message.source,
                "text": _snippet(message.text),
                "copied": message.copied,
            }
            system_ids.append(message.step_id)
        turns[turn_id] = {
            "id": turn_id,
            "index": turn.index,
            "copied": turn.copied,
            "user_step_id": turn.user_step_id,
            "reply_ids": reply_ids,
            "system_ids": system_ids,
            "turn_marker": turn.turn_marker,
            "score": score.score,
            "answered": score.answered,
            "reply_chars": score.reply_chars,
            "tool_calls": score.tool_calls,
            "tool_loop": score.tool_loop,
            "structure": score.structure,
            "details": [
                {
                    "metric": detail.metric,
                    "label": detail.label,
                    "score": detail.score,
                    "reason": _snippet(detail.reason),
                    "reply_step_id": detail.reply_step_id,
                }
                for detail in score.details
            ],
        }
        step_turn[turn.user_step_id] = turn_id
    return messages, turns, step_turn


def _turn_for_step(step_id: int, turn_starts: dict[int, str]) -> str | None:
    starts = [start for start in turn_starts if start <= step_id]
    return turn_starts[max(starts)] if starts else None


def _trial_view(
    panel: TrialPanel,
    position: int,
    axis_mode: str,
) -> dict[str, Any]:
    stats = panel.stats
    messages, turns, turn_starts = _messages_and_turns(panel.turn_analysis)
    tools_by_step: dict[int, list[dict[str, Any]]] = {}
    if panel.turn_analysis is not None:
        for turn in panel.turn_analysis.turns:
            for tool in turn.tool_events:
                tools_by_step.setdefault(tool.step_id, []).append(_tool_view(tool))
    events: list[dict[str, Any]] = []
    for step_position, step in enumerate(stats.steps):
        step_key = str(step.step_id)
        events.append(
            {
                "id": f"trial-{position}-step-{step.step_id}",
                "step_id": step.step_id,
                "source": step.source or "unknown",
                "copied": step.copied,
                "timestamp": step.timestamp,
                "seconds": step.seconds_from_start,
                "duration": step.duration_seconds,
                "prompt_tokens": step.prompt_tokens,
                "completion_tokens": step.completion_tokens,
                "cached_tokens": step.cached_tokens,
                "tool_names": list(step.tool_names),
                "tool_calls": step.tool_calls,
                "observation_missing": step.observation_missing,
                "message_step_id": step_key if step_key in messages else None,
                "turn_id": _turn_for_step(step.step_id, turn_starts),
                "axis_value": (
                    step.seconds_from_start if axis_mode == "time" else step_position
                ),
            }
        )
    metrics = []
    if panel.turn_analysis is not None:
        metrics = [
            {
                "name": outcome.name,
                "status": outcome.status,
                "score": outcome.score,
                "reasons": [_snippet(reason) for reason in outcome.reasons],
            }
            for outcome in panel.turn_analysis.metric_outcomes
        ]
    return {
        "id": f"trial-{position}",
        "heading": panel.heading,
        "meta": panel.meta,
        "stats": {
            "trial_id": stats.trial_id,
            "verdict": stats.verdict,
            "verdict_label": _VERDICT_ZH.get(stats.verdict or "", stats.verdict or "未判定"),
            "model": stats.model,
            "started_at": stats.started_at,
            "wall_clock_seconds": stats.wall_clock_seconds,
            "total_steps": stats.total_steps,
            "total_tokens": stats.total_tokens,
            "peak_input_tokens": stats.peak_input_tokens,
            "peak_step_id": stats.peak_step_id,
            "live_turns": stats.live_turns,
            "copied_turns": stats.copied_turns,
            "stop_reason": stats.stop_reason,
            "missing": list(stats.missing),
            "tools": [
                {
                    "name": tool.name,
                    "calls": tool.calls,
                    "observed": tool.observed,
                    "missing_obs": tool.missing_obs,
                    "retries": tool.retries,
                    "avg_seconds": tool.avg_seconds,
                    "longest_seconds": tool.longest_seconds,
                }
                for tool in stats.tools
            ],
            "tool_calls_total": stats.tool_calls_total,
            "tool_missing_obs_total": stats.tool_missing_obs_total,
            "tool_retries_total": stats.tool_retries_total,
            "tool_time_share": stats.tool_time_share,
        },
        "events": events,
        "messages": messages,
        "turns": turns,
        "metrics": metrics,
        "tools_by_step": {str(key): value for key, value in tools_by_step.items()},
    }


def _display_items(
    trial: dict[str, Any], axis_max: float, dense: bool
) -> list[dict[str, Any]]:
    """返回可绘制点；密集模式仍携带五轨聚合事实和所有原始事件 ID。"""
    events = trial["events"]
    if not dense:
        return [{"kind": "event", **event} for event in events]
    buckets: dict[int, list[dict[str, Any]]] = {}
    for event in events:
        ratio = min(1.0, max(0.0, float(event["axis_value"] or 0.0) / axis_max))
        key = min(_BUCKET_COUNT - 1, int(ratio * _BUCKET_COUNT))
        buckets.setdefault(key, []).append(event)
    output: list[dict[str, Any]] = []
    for bucket, items in sorted(buckets.items()):
        prompt_events = [event for event in items if event["prompt_tokens"] is not None]
        prompts = [event["prompt_tokens"] for event in prompt_events]
        completions = [event["completion_tokens"] for event in items if event["completion_tokens"] is not None]
        cached = [event["cached_tokens"] for event in prompt_events if event["cached_tokens"] is not None]
        cached_complete = bool(prompt_events) and len(cached) == len(prompt_events)
        durations = [event["duration"] for event in items if event["duration"] is not None]
        tool_names = sorted({name for event in items for name in event["tool_names"]})
        output.append(
            {
                "kind": "bucket",
                "id": f'{trial["id"]}-bucket-{bucket}',
                "axis_value": sum(float(event["axis_value"] or 0.0) for event in items) / len(items),
                "count": len(items),
                "event_ids": [event["id"] for event in items],
                "prompt_tokens": sum(prompts) if prompts else None,
                "completion_tokens": sum(completions) if completions else None,
                "cached_tokens": sum(cached) if cached_complete else None,
                "context_peak": max(prompts) if prompts else None,
                "tool_calls": sum(event["tool_calls"] for event in items),
                "tool_names": tool_names,
                "observation_missing": any(event["observation_missing"] for event in items),
                "duration": sum(durations) if durations else None,
                "copied": any(event["copied"] for event in items),
            }
        )
    return output


def _workspace_insights(views: Sequence[dict[str, Any]], context_window: int | None,
                        window_source: str = "caller") -> list[dict[str, str]]:
    """只从已有密封事实挑选可导航的关注项，跨试次聚合同类事实。"""
    raw_insights: list[dict[str, Any]] = []
    for trial in views:
        stats = trial["stats"]
        missing = [event for event in trial["events"] if event["observation_missing"]]
        for event in missing:
            raw_insights.append(
                {
                    "kind": "danger",
                    "title": "工具无观测信号",
                    "detail": f'{trial["heading"]} · 步骤 {event["step_id"]} · ' + "、".join(event["tool_names"]),
                    "event_id": event["id"],
                    "trial_id": trial["id"],
                    "tool_names": tuple(event["tool_names"]),
                }
            )
        if stats["tool_retries_total"]:
            raw_insights.append(
                {
                    "kind": "warn",
                    "title": f'重试 {stats["tool_retries_total"]} 次',
                    "detail": f'{trial["heading"]} · 相邻同名同参调用',
                    "trial_id": trial["id"],
                    "retries": stats["tool_retries_total"],
                }
            )
        tool_events = [
            event for event in trial["events"]
            if event["tool_calls"] and event["duration"] is not None
        ]
        if tool_events:
            longest = max(tool_events, key=lambda event: float(event["duration"] or 0.0))
            raw_insights.append(
                {
                    "kind": "info",
                    "title": f'最长含工具步骤 {_secs(longest["duration"])}',
                    "detail": f'{trial["heading"]} · 步骤 {longest["step_id"]}（含模型推理）',
                    "event_id": longest["id"],
                    "trial_id": trial["id"],
                    "duration": longest["duration"],
                }
            )
        if stats["peak_step_id"] is not None:
            peak = next(
                (event for event in trial["events"] if event["step_id"] == stats["peak_step_id"]),
                None,
            )
            if peak is not None:
                suffix = ""
                if context_window and stats["peak_input_tokens"] is not None:
                    window_noun = "证据携带窗口" if window_source == "evidence" else "声明窗口"
                    suffix = f' · 占{window_noun} {stats["peak_input_tokens"] / context_window:.0%}'
                raw_insights.append(
                    {
                        "kind": "warn" if context_window and stats["peak_input_tokens"] and stats["peak_input_tokens"] / context_window >= 0.7 else "info",
                        "title": f'峰值上下文 {_int(stats["peak_input_tokens"])} tokens',
                        "detail": f'{trial["heading"]} · 步骤 {peak["step_id"]}{suffix}',
                        "event_id": peak["id"],
                        "trial_id": trial["id"],
                        "peak_tokens": stats["peak_input_tokens"],
                    }
                )
        for turn in trial["turns"].values():
            for detail in turn["details"]:
                if detail["score"] >= 0.9 or detail["reply_step_id"] is None:
                    continue
                reply = next(
                    (event for event in trial["events"] if event["step_id"] == detail["reply_step_id"]),
                    None,
                )
                if reply is not None:
                    raw_insights.append(
                        {
                            "kind": "danger",
                            "title": f'{detail["metric"]} {detail["score"]:.2f}',
                            "detail": f'{trial["heading"]} · {detail["label"]}',
                            "event_id": reply["id"],
                            "trial_id": trial["id"],
                            "metric": detail["metric"],
                            "score": detail["score"],
                        }
                    )
        for reason in stats["missing"]:
            raw_insights.append(
                {
                    "kind": "muted",
                    "title": "数据不可用",
                    "detail": f'{trial["heading"]} · {reason}',
                    "trial_id": trial["id"],
                    "reason": reason,
                }
            )
    
    aggregated: list[dict[str, str]] = []
    total_trials = len(views)
    
    groups: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for insight in raw_insights:
        key = (insight["kind"], insight["title"])
        groups.setdefault(key, []).append(insight)
    
    for (kind, title), items in groups.items():
        if len(items) == 1:
            item = items[0]
            aggregated.append({
                "kind": kind,
                "title": title,
                "detail": item["detail"],
                **({"event_id": item["event_id"]} if "event_id" in item else {"trial_id": item["trial_id"]}),
            })
            continue
        
        if title == "工具无观测信号":
            tool_names_set: set[tuple[str, ...]] = {item["tool_names"] for item in items}
            if len(tool_names_set) == 1:
                tool_names = next(iter(tool_names_set))
                trial_ids = sorted({item["trial_id"] for item in items})
                event_ids = [item["event_id"] for item in items]
                aggregated.append({
                    "kind": kind,
                    "title": f'{len(items)}/{total_trials} 试次 {"、".join(tool_names)} 无观测',
                    "detail": f'步骤 {", ".join(str(item.get("event_id", "").split("-")[-1]) for item in items[:3])}{"…" if len(items) > 3 else ""} 等 {len(items)} 处',
                    "event_id": event_ids[0],
                })
            else:
                for item in items:
                    aggregated.append({
                        "kind": kind,
                        "title": title,
                        "detail": item["detail"],
                        "event_id": item["event_id"],
                    })
        elif title.startswith("峰值上下文"):
            peak_values = [item["peak_tokens"] for item in items if item.get("peak_tokens") is not None]
            if peak_values and max(peak_values) - min(peak_values) <= 100:
                avg_peak = sum(peak_values) // len(peak_values)
                event_ids = [item["event_id"] for item in items]
                aggregated.append({
                    "kind": kind,
                    "title": f'{len(items)}/{total_trials} 试次 峰值上下文 ~{_int(avg_peak)} tokens',
                    "detail": f'步骤 {", ".join(str(item.get("event_id", "").split("-")[-1]) for item in items[:3])}{"…" if len(items) > 3 else ""}',
                    "event_id": event_ids[0],
                })
            else:
                for item in items:
                    aggregated.append({
                        "kind": kind,
                        "title": title,
                        "detail": item["detail"],
                        "event_id": item["event_id"],
                    })
        elif title.startswith("重试"):
            retry_counts = [item["retries"] for item in items if item.get("retries")]
            if retry_counts and max(retry_counts) == min(retry_counts):
                trial_ids = sorted({item["trial_id"] for item in items})
                aggregated.append({
                    "kind": kind,
                    "title": f'{len(items)}/{total_trials} 试次 {title}',
                    "detail": "相邻同名同参调用",
                    "trial_id": items[0]["trial_id"],
                })
            else:
                for item in items:
                    aggregated.append({
                        "kind": kind,
                        "title": title,
                        "detail": item["detail"],
                        "trial_id": item["trial_id"],
                    })
        elif title.startswith("最长含工具步骤"):
            durations = [item["duration"] for item in items if item.get("duration") is not None]
            if durations and max(durations) - min(durations) <= 2.0:
                avg_duration = sum(durations) / len(durations)
                event_ids = [item["event_id"] for item in items]
                aggregated.append({
                    "kind": kind,
                    "title": f'{len(items)}/{total_trials} 试次 最长含工具步骤 ~{_secs(avg_duration)}',
                    "detail": "含模型推理",
                    "event_id": event_ids[0],
                })
            else:
                for item in items:
                    aggregated.append({
                        "kind": kind,
                        "title": title,
                        "detail": item["detail"],
                        "event_id": item["event_id"],
                    })
        else:
            for item in items:
                aggregated.append({
                    "kind": kind,
                    "title": title,
                    "detail": item["detail"],
                    **({"event_id": item["event_id"]} if "event_id" in item else {"trial_id": item["trial_id"]}),
                })
    
    priority = {"danger": 0, "warn": 1, "info": 2, "muted": 3}
    return sorted(aggregated, key=lambda item: (priority[item["kind"]], item["title"], item["detail"]))[:12]


def _workspace_view(
    trials: Sequence[TrialPanel], context_window: int | None
) -> dict[str, Any]:
    axis_mode, axis_max, axis_note = _time_axis(trials)
    views = [_trial_view(panel, index, axis_mode) for index, panel in enumerate(trials)]
    dense = sum(len(trial["events"]) for trial in views) > _POINT_BUDGET
    for trial in views:
        trial["display_items"] = _display_items(trial, axis_max, dense)
    token_values = [
        (event["prompt_tokens"] or 0) + (event["completion_tokens"] or 0)
        for trial in views for event in trial["events"]
        if event["prompt_tokens"] is not None or event["completion_tokens"] is not None
    ]
    context_values = [
        event["prompt_tokens"] for trial in views for event in trial["events"]
        if event["prompt_tokens"] is not None
    ]
    tool_usage: dict[str, int] = {}
    for trial in views:
        for tool in trial["stats"].get("tools", []):
            name = tool.get("name", "")
            calls = tool.get("calls", 0)
            if name and calls > 0:
                tool_usage[name] = tool_usage.get(name, 0) + calls
    valid_window = context_window if context_window and context_window > 0 else None
    # Window source precedence: an explicit caller-declared flag wins (it is the
    # operator's own statement); otherwise fall back to the window the sealed
    # evidence carries. The evidence fallback is only used when every trial that
    # carries a window agrees on it — a shared occupancy axis over disagreeing
    # windows would be a fabricated scale, so disagreement yields no window.
    evidence_windows = {
        panel.context_window for panel in trials
        if panel.context_window and panel.context_window > 0
    }
    if valid_window is not None:
        window_source = "caller"
    elif len(evidence_windows) == 1:
        valid_window = next(iter(evidence_windows))
        window_source = "evidence"
    else:
        window_source = "none"
    return {
        "axis": {
            "mode": axis_mode,
            "max": axis_max,
            "note": axis_note,
            "label": "相对执行时间" if axis_mode == "time" else "步骤序",
        },
        "scales": {
            "token_max": max(token_values, default=1),
            "context_max": max([*context_values, valid_window or 0], default=1),
        },
        "context_window": valid_window,
        "context_window_source": window_source,
        "dense": dense,
        "trials": views,
        "insights": _workspace_insights(views, valid_window, window_source),
        "tool_usage": tool_usage,
    }


def _axis_ticks(axis_mode: str, axis_max: float) -> list[tuple[float, str]]:
    return [
        (
            axis_max * index / 4,
            _mmss(axis_max * index / 4) if axis_mode == "time" else f"步骤 {round(axis_max * index / 4) + 1}",
        )
        for index in range(5)
    ]


def _target_attrs(item: dict[str, Any], label: str, track: str) -> str:
    target = "data-bucket" if item["kind"] == "bucket" else "data-event"
    return (
        f'class="timeline-event track-mark track-{track}" role="button" tabindex="0" '
        f'aria-label="{_e(label)}，点击查看详情" aria-selected="false" '
        f'{target}="{_e(item["id"])}"'
    )


def _x_of(value: float, axis_max: float, plot_start: float, plot_width: float) -> float:
    return plot_start + value / axis_max * plot_width


def _lane_label(trial: dict[str, Any], index: int, y: float) -> str:
    verdict = trial["stats"]["verdict"] or "unknown"
    return (
        f'<g class="timeline-trial" role="button" tabindex="0" aria-selected="false" '
        f'aria-label="{_e(trial["heading"])}，点击查看试次摘要" data-trial="{_e(trial["id"])}">'
        f'<text class="track-trial-label" x="{_TIMELINE_LABEL_W - 10}" y="{y + 4:.1f}" '
        f'text-anchor="end">试次 {index + 1}</text><text class="track-trial-verdict status-{_e(verdict)}" '
        f'x="{_TIMELINE_LABEL_W - 10}" y="{y + 17:.1f}" text-anchor="end">'
        f'{_e(trial["stats"]["verdict_label"])}</text></g>'
    )


def _execution_mark(item: dict[str, Any], x: float, y: float) -> str:
    if item["kind"] == "bucket":
        label = f'{item["count"]} 步聚合'
        shape = f'<rect class="event-shape" x="{x - 7:.1f}" y="{y - 7:.1f}" width="14" height="14" rx="3"/>'
    else:
        label = f'步骤 {item["step_id"]} · {item["source"]}'
        if item["copied"]:
            label += " · 记忆基底"
        if item["tool_names"]:
            label += " · 工具 " + "、".join(item["tool_names"])
        if item["observation_missing"]:
            label += " · 无观测"
        if item["source"] == "user":
            shape = f'<rect class="event-shape" x="{x - 5:.1f}" y="{y - 5:.1f}" width="10" height="10" rx="1"/>'
        elif item["source"] in {"system", "developer"}:
            shape = f'<path class="event-shape" d="M {x:.1f} {y - 7:.1f} L {x + 7:.1f} {y:.1f} L {x:.1f} {y + 7:.1f} L {x - 7:.1f} {y:.1f} Z"/>'
        else:
            shape = f'<circle class="event-shape" cx="{x:.1f}" cy="{y:.1f}" r="5"/>'
    source = item.get("source", "bucket")
    classes = [f"source-{source}"]
    if item.get("copied"):
        classes.append("is-copied")
    if item.get("observation_missing"):
        classes.append("is-failed")
    ring = f'<circle class="failure-ring" cx="{x:.1f}" cy="{y:.1f}" r="9"/>' if item.get("observation_missing") else ""
    return _event_group(item, label, "execution", ring + shape, " ".join(classes))


def _event_group(item: dict[str, Any], label: str, track: str, content: str, extra: str = "") -> str:
    attrs = _target_attrs(item, label, track)
    if extra:
        attrs = attrs.replace('class="timeline-event', f'class="timeline-event {extra}')
    return f'<g {attrs}><title>{_e(label)}</title>{content}</g>'


def _timeline_svg(workspace: dict[str, Any]) -> str:
    """一条共享 X 轴上的执行、工具、Token、输入Token、上下文五轨。"""
    trials = workspace["trials"]
    axis = workspace["axis"]
    scales = workspace["scales"]
    lanes = max(1, len(trials))
    execution_h = 52 + lanes * 34
    tool_h = 54 + lanes * 34
    token_h = 62 + lanes * 38
    input_token_h = 64 + lanes * 42
    context_h = 64 + lanes * 42
    top = 48
    execution_top = top
    tool_top = execution_top + execution_h + 12
    token_top = tool_top + tool_h + 12
    input_token_top = token_top + token_h + 12
    context_top = input_token_top + input_token_h + 12
    h = context_top + context_h + 20
    plot_start = _TIMELINE_LABEL_W
    plot_end = _TIMELINE_W - _TIMELINE_PAD
    plot_width = plot_end - plot_start
    axis_max = float(axis["max"])
    parts = [
        f'<svg class="trajectory-timeline" viewBox="0 0 {_TIMELINE_W} {h}" '
        f'role="img" aria-label="执行、工具、Token、输入Token 与上下文压力五轨联动图">'
    ]
    for value, label in _axis_ticks(axis["mode"], axis_max):
        x = _x_of(value, axis_max, plot_start, plot_width)
        parts.append(
            f'<line class="timeline-grid" x1="{x:.1f}" y1="38" x2="{x:.1f}" y2="{h - 16}"/>'
            f'<text class="timeline-tick" x="{x:.1f}" y="30" text-anchor="middle">{_e(label)}</text>'
        )
    parts.append(f'<text class="timeline-axis-label" x="{plot_start}" y="14">{_e(axis["label"])}（五轨共享）</text>')

    def heading(y: float, title: str, note: str) -> None:
        parts.append(
            f'<text class="track-heading" x="12" y="{y + 15:.1f}">{_e(title)}</text>'
            f'<text class="track-note" x="12" y="{y + 29:.1f}">{_e(note)}</text>'
        )

    heading(execution_top, "执行图谱", "角色、记忆基底与无观测")
    for index, trial in enumerate(trials):
        y = execution_top + 48 + index * 34
        parts.append(_lane_label(trial, index, y))
        parts.append(f'<line class="track-baseline" x1="{plot_start}" y1="{y:.1f}" x2="{plot_end}" y2="{y:.1f}"/>')
        copied = [item for item in trial["display_items"] if item.get("copied")]
        if copied:
            xs = [_x_of(float(item["axis_value"] or 0.0), axis_max, plot_start, plot_width) for item in copied]
            parts.append(f'<line class="copied-range" x1="{min(xs):.1f}" y1="{y:.1f}" x2="{max(xs):.1f}" y2="{y:.1f}"/>')
        offsets: dict[str, int] = {}
        for item in trial["display_items"]:
            x = _x_of(float(item["axis_value"] or 0.0), axis_max, plot_start, plot_width)
            key = f"{x:.1f}"
            offset = offsets.get(key, 0)
            offsets[key] = offset + 1
            parts.append(_execution_mark(item, x, y + ((offset % 3) - 1) * 8))

    tool_calls_total = sum(trial["stats"]["tool_calls_total"] for trial in trials)
    heading(tool_top, "工具调用", f'{tool_calls_total} 次调用；横条 = 含工具调用的步骤耗时（含模型推理）')
    for index, trial in enumerate(trials):
        y = tool_top + 48 + index * 34
        parts.append(_lane_label(trial, index, y))
        parts.append(f'<line class="track-baseline" x1="{plot_start}" y1="{y:.1f}" x2="{plot_end}" y2="{y:.1f}"/>')
        for item in trial["display_items"]:
            if not item.get("tool_calls"):
                continue
            x = _x_of(float(item["axis_value"] or 0.0), axis_max, plot_start, plot_width)
            duration = item.get("duration") if axis["mode"] == "time" else None
            bar = ""
            if duration is not None:
                width = max(3.0, min(90.0, float(duration) / axis_max * plot_width))
                bar = f'<rect class="tool-duration" x="{x:.1f}" y="{y - 5:.1f}" width="{width:.1f}" height="10" rx="3"/>'
            else:
                bar = f'<rect class="tool-duration unknown-duration" x="{x - 3:.1f}" y="{y - 5:.1f}" width="6" height="10" rx="2"/>'
            label = (
                f'{item.get("count", 1)} 步聚合 · 工具 {"、".join(item["tool_names"])}'
                if item["kind"] == "bucket"
                else f'步骤 {item["step_id"]} · 工具 {"、".join(item["tool_names"])}'
            )
            if item.get("observation_missing"):
                label += " · 含无观测信号"
            content = bar + f'<text class="tool-name" x="{x:.1f}" y="{y + 14:.1f}">{_e(item["tool_names"][0][:12])}</text>'
            extra = "is-failed" if item.get("observation_missing") else ""
            parts.append(_event_group(item, label, "tools", content, extra))

    heading(token_top, "Token 脉冲", "按模型调用拆分")
    token_max = float(scales["token_max"])
    for index, trial in enumerate(trials):
        base = token_top + 56 + index * 38
        plot_h = 25.0
        parts.append(_lane_label(trial, index, base))
        parts.append(f'<line class="track-baseline" x1="{plot_start}" y1="{base:.1f}" x2="{plot_end}" y2="{base:.1f}"/>')
        for item in trial["display_items"]:
            prompt = item.get("prompt_tokens")
            completion = item.get("completion_tokens")
            if prompt is None and completion is None:
                continue
            x = _x_of(float(item["axis_value"] or 0.0), axis_max, plot_start, plot_width)
            width = 8.0 if item["kind"] == "event" else 12.0
            y = base
            segments: list[tuple[float, str]] = []
            cached = item.get("cached_tokens")
            if prompt is not None:
                if cached is None:
                    segments.append((float(prompt), "token-unsegmented"))
                else:
                    segments.append((min(float(cached), float(prompt)), "token-cached"))
                    segments.append((max(0.0, float(prompt) - float(cached)), "token-uncached"))
            if completion is not None:
                segments.append((float(completion), "token-output"))
            shapes: list[str] = []
            for value, cls in segments:
                if value <= 0:
                    continue
                height = value / token_max * plot_h
                y -= height
                shapes.append(f'<rect class="{cls}" x="{x - width / 2:.1f}" y="{y:.1f}" width="{width:.1f}" height="{height:.1f}"/>')
            label = f'{item.get("count", 1)} 步 Token 聚合' if item["kind"] == "bucket" else f'步骤 {item["step_id"]} Token'
            parts.append(_event_group(item, label, "tokens", "".join(shapes)))

    heading(input_token_top, "输入 Token", "绝对值")
    input_token_max = float(scales["context_max"])
    input_token_plot_h = 28.0
    for index, trial in enumerate(trials):
        base = input_token_top + 66 + index * 42
        parts.append(_lane_label(trial, index, base))
        parts.append(f'<line class="track-baseline" x1="{plot_start}" y1="{base:.1f}" x2="{plot_end}" y2="{base:.1f}"/>')
        path: list[str] = []
        for item in trial["display_items"]:
            value = item.get("context_peak") if item["kind"] == "bucket" else item.get("prompt_tokens")
            if value is None:
                path.append("M")
                continue
            x = _x_of(float(item["axis_value"] or 0.0), axis_max, plot_start, plot_width)
            y = base - float(value) / input_token_max * input_token_plot_h
            command = "L" if path and path[-1] != "M" else "M"
            path.append(f"{command}{x:.1f},{y:.1f}")
        cleaned = " ".join(part for part in path if part != "M")
        if cleaned:
            parts.append(f'<path class="context-line context-line-{index % 4}" d="{cleaned}"/>')
        for item in trial["display_items"]:
            value = item.get("context_peak") if item["kind"] == "bucket" else item.get("prompt_tokens")
            if value is None:
                continue
            x = _x_of(float(item["axis_value"] or 0.0), axis_max, plot_start, plot_width)
            y = base - float(value) / input_token_max * input_token_plot_h
            label = f'{item.get("count", 1)} 步上下文聚合' if item["kind"] == "bucket" else f'步骤 {item["step_id"]} · 输入 {_int(value)} tokens'
            peak = item.get("context_peak") == trial["stats"]["peak_input_tokens"] if item["kind"] == "bucket" else item.get("step_id") == trial["stats"]["peak_step_id"]
            cls = "context-point peak-context" if peak else "context-point"
            parts.append(_event_group(item, label, "context", f'<circle class="{cls}" cx="{x:.1f}" cy="{y:.1f}" r="4"/>'))

    window = workspace["context_window"]
    if window:
        context_note = ("相对上下文窗口（证据携带）"
                        if workspace.get("context_window_source") == "evidence"
                        else "相对上下文窗口（调用方声明）")
    else:
        context_note = "无窗口声明"
    heading(context_top, "上下文压力", context_note)
    context_plot_h = 28.0
    if window:
        for ratio, label in ((0.7, "70%"), (0.9, "90%")):
            y = context_top + 38 + (1 - ratio) * context_plot_h
            parts.append(f'<line class="context-threshold" x1="{plot_start}" y1="{y:.1f}" x2="{plot_end}" y2="{y:.1f}"/><text class="threshold-label" x="{plot_end + 4}" y="{y + 3:.1f}">{label}</text>')
        for index, trial in enumerate(trials):
            base = context_top + 66 + index * 42
            parts.append(_lane_label(trial, index, base))
            parts.append(f'<line class="track-baseline" x1="{plot_start}" y1="{base:.1f}" x2="{plot_end}" y2="{base:.1f}"/>')
            path: list[str] = []
            for item in trial["display_items"]:
                value = item.get("context_peak") if item["kind"] == "bucket" else item.get("prompt_tokens")
                if value is None:
                    path.append("M")
                    continue
                x = _x_of(float(item["axis_value"] or 0.0), axis_max, plot_start, plot_width)
                ratio = float(value) / window
                y = base - ratio * context_plot_h
                command = "L" if path and path[-1] != "M" else "M"
                path.append(f"{command}{x:.1f},{y:.1f}")
            cleaned = " ".join(part for part in path if part != "M")
            if cleaned:
                parts.append(f'<path class="context-line context-line-{index % 4}" d="{cleaned}"/>')
            for item in trial["display_items"]:
                value = item.get("context_peak") if item["kind"] == "bucket" else item.get("prompt_tokens")
                if value is None:
                    continue
                x = _x_of(float(item["axis_value"] or 0.0), axis_max, plot_start, plot_width)
                ratio = float(value) / window
                y = base - ratio * context_plot_h
                label = f'{item.get("count", 1)} 步上下文聚合' if item["kind"] == "bucket" else f'步骤 {item["step_id"]} · 占用 {ratio:.0%}'
                peak = item.get("context_peak") == trial["stats"]["peak_input_tokens"] if item["kind"] == "bucket" else item.get("step_id") == trial["stats"]["peak_step_id"]
                cls = "context-point peak-context" if peak else "context-point"
                parts.append(_event_group(item, label, "context", f'<circle class="{cls}" cx="{x:.1f}" cy="{y:.1f}" r="4"/>'))
    parts.append("</svg>")
    return "".join(parts)


_PANEL_CSS = """
.trajectory-workbench{margin:20px 0;border:1px solid #d7dce3;border-radius:12px;background:#fff;overflow:hidden}.workbench-head{padding:16px 18px 12px;border-bottom:1px solid #e4e8ed}.workbench-head h2{margin:0;font-size:18px;color:#1f2937}.workbench-head p{margin:5px 0 0;color:#5b6470;font-size:13px}.workbench-note{margin:0;padding:9px 18px;background:#f8fafc;border-bottom:1px solid #e4e8ed;color:#475569;font-size:12px}
.insights{display:grid;grid-template-columns:repeat(auto-fit,minmax(180px,1fr));gap:8px;padding:12px 18px;border-bottom:1px solid #e4e8ed;background:#fbfcfe}.insight{border:1px solid #dfe6ed;border-left-width:3px;border-radius:8px;padding:8px 10px;background:#fff;text-align:left;cursor:pointer;color:#1e293b;font:inherit}.insight b,.insight span{display:block}.insight b{font-size:12px}.insight span{margin-top:3px;color:#64748b;font-size:11px;line-height:1.35}.insight.danger{border-left-color:#dc2626}.insight.warn{border-left-color:#d97706}.insight.info{border-left-color:#2563eb}.insight.muted{border-left-color:#94a3b8}.insight:hover,.insight:focus{border-color:#2563eb;background:#eff6ff;outline:none}
.panel-page{max-width:1600px;margin:0 auto;padding:24px 24px 40px}
.workbench-grid{display:grid;grid-template-columns:minmax(0,3fr) minmax(300px,1fr);min-height:520px}.timeline-scroll{overflow-x:hidden;overflow-y:auto;padding:12px;border-right:1px solid #e4e8ed}.trajectory-timeline{width:100%;height:auto;display:block}.timeline-grid{stroke:#e8edf2;stroke-width:1}.timeline-tick,.timeline-axis-label{fill:#64748b;font-size:11px}.timeline-axis-label{font-weight:600}.track-heading{fill:#1e293b;font-size:14px;font-weight:700}.track-note{fill:#64748b;font-size:10px}.track-baseline{stroke:#c9d3de;stroke-width:1}.track-trial-label{fill:#475569;font-size:10px;font-weight:600}.track-trial-verdict{font-size:9px}.status-pass{fill:#17834f}.status-fail{fill:#c53030}.status-cannot_judge{fill:#a16207}.copied-range{stroke:#94a3b8;stroke-width:8;stroke-linecap:round;stroke-dasharray:3 4;opacity:.45}
.timeline-event,.timeline-trial{cursor:pointer;outline:none}.timeline-trial:hover .track-trial-label,.timeline-trial:focus .track-trial-label,.timeline-trial.is-selected .track-trial-label{fill:#1d4ed8;text-decoration:underline}.timeline-event .event-shape{fill:#16805a;stroke:#fff;stroke-width:2}.timeline-event.source-user .event-shape{fill:#2563eb}.timeline-event.source-system .event-shape,.timeline-event.source-developer .event-shape{fill:#7c3aed}.timeline-event.is-copied .event-shape{fill:#94a3b8;stroke-dasharray:2 1}.timeline-event.is-failed .event-shape{fill:#dc2626}.failure-ring{fill:none;stroke:#991b1b;stroke-width:2}.tool-duration{fill:#6474dc;opacity:.75}.timeline-event.is-failed .tool-duration{fill:#dc2626}.unknown-duration{fill:#94a3b8}.tool-name{fill:#6d28d9;font-size:9px}.token-cached{fill:#2563eb}.token-uncached{fill:#93c5fd}.token-output{fill:#f59e0b}.token-unsegmented{fill:#94a3b8}.context-line{fill:none;stroke-width:1.8;opacity:.75}.context-line-0{stroke:#2563eb}.context-line-1{stroke:#16a34a}.context-line-2{stroke:#7c3aed}.context-line-3{stroke:#d97706}.context-point{fill:#fff;stroke:#2563eb;stroke-width:1.8}.peak-context{fill:#dc2626;stroke:#7f1d1d}.context-threshold{stroke:#dc2626;stroke-dasharray:4 3;stroke-width:1;opacity:.55}.threshold-label{fill:#b91c1c;font-size:9px}.timeline-event:hover .event-shape,.timeline-event:focus .event-shape,.timeline-event.is-selected .event-shape{stroke:#111827;stroke-width:3}.timeline-event:hover .tool-duration,.timeline-event:focus .tool-duration,.timeline-event.is-selected .tool-duration{stroke:#111827;stroke-width:2}.timeline-event:hover .token-cached,.timeline-event:hover .token-uncached,.timeline-event:hover .token-output,.timeline-event:hover .token-unsegmented,.timeline-event:focus .token-cached,.timeline-event:focus .token-uncached,.timeline-event:focus .token-output,.timeline-event:focus .token-unsegmented,.timeline-event.is-selected .token-cached,.timeline-event.is-selected .token-uncached,.timeline-event.is-selected .token-output,.timeline-event.is-selected .token-unsegmented{stroke:#111827;stroke-width:2}.timeline-event:hover .context-point,.timeline-event:focus .context-point,.timeline-event.is-selected .context-point{stroke:#111827;stroke-width:3}
.inspector{padding:16px;min-width:0}.inspector h3{margin:0 0 6px;font-size:16px}.inspector h4{margin:18px 0 7px;font-size:13px;color:#334155}.inspector p{margin:5px 0;color:#475569;font-size:13px}.inspector-empty{color:#64748b}.inspector-facts{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:7px;margin-top:12px}.inspector-fact{border:1px solid #e2e8f0;border-radius:7px;padding:7px;background:#f8fafc}.inspector-fact b{display:block;font-size:11px;font-weight:500;color:#64748b}.inspector-fact span{display:block;margin-top:2px;font-size:12px;color:#1e293b;overflow-wrap:anywhere}.inspector-message{white-space:pre-wrap;overflow-wrap:anywhere;background:#f8fafc;border:1px solid #e2e8f0;border-radius:8px;padding:9px;font:12px/1.5 -apple-system,"PingFang SC",sans-serif}.inspector-list{display:grid;gap:7px}.inspector-detail{border-left:3px solid #cbd5e1;padding:6px 8px;background:#f8fafc;font-size:12px;color:#334155}.inspector-detail.fail{border-color:#dc2626}.inspector-detail.pass{border-color:#16a34a}.inspector table{border-collapse:collapse;width:100%;font-size:11px}.inspector th,.inspector td{padding:5px;text-align:left;border-bottom:1px solid #e2e8f0}.inspector th{color:#64748b;font-weight:500}.inspector button{border:1px solid #cbd5e1;background:#fff;border-radius:6px;padding:4px 7px;color:#1d4ed8;font:inherit;cursor:pointer}.inspector button:hover,.inspector button:focus{border-color:#2563eb;background:#eff6ff}.inspector-warning{padding:8px;border-radius:7px;background:#fff7ed;color:#9a3412;font-size:12px}.legend{padding:10px 18px;border-top:1px solid #e4e8ed;color:#64748b;font-size:12px}.legend span{display:inline-flex;align-items:center;margin-right:12px}.legend i{display:inline-block;width:10px;height:10px;margin-right:4px;border:1px solid #64748b}.legend .legend-agent{border-radius:50%;background:#16805a}.legend .legend-user{background:#2563eb}.legend .legend-system{background:#7c3aed;transform:rotate(45deg)}.legend .legend-failed{border-radius:50%;background:#dc2626}.legend .legend-token-cached{background:#2563eb}.legend .legend-token-uncached{background:#93c5fd}.legend .legend-token-output{background:#f59e0b}.legend .legend-token-visible{background:#16a34a}
@media (max-width:760px){.workbench-grid{grid-template-columns:1fr}.timeline-scroll{border-right:0;border-bottom:1px solid #e4e8ed}.inspector-facts{grid-template-columns:1fr}.insights{grid-template-columns:1fr}}
@media (prefers-reduced-motion:reduce){.timeline-event,.timeline-trial{transition:none}}
"""


_INTERACTION_JS = """(() => {
  const workspace = JSON.parse(document.getElementById("trajectory-data").textContent);
  const inspector = document.getElementById("trajectory-inspector");
  const trialById = new Map(workspace.trials.map((trial) => [trial.id, trial]));
  const eventById = new Map();
  const bucketById = new Map();
  workspace.trials.forEach((trial) => {
    trial.events.forEach((event) => eventById.set(event.id, { trial, event }));
    trial.display_items.filter((item) => item.kind === "bucket").forEach((bucket) => bucketById.set(bucket.id, { trial, bucket }));
  });
  let selectedTrial = null;
  let selectedEvent = null;
  let selectedBucket = null;

  function node(tag, text, className) {
    const element = document.createElement(tag);
    if (text !== undefined && text !== null) element.textContent = text;
    if (className) element.className = className;
    return element;
  }
  function append(parent, tag, text, className) {
    const element = node(tag, text, className);
    parent.append(element);
    return element;
  }
  function number(value) {
    return value === null || value === undefined ? "—" : String(value);
  }
  function seconds(value) {
    return value === null || value === undefined ? "—" : `${Number(value).toFixed(1)}s`;
  }
  function score(value) {
    return value === null || value === undefined ? "未评测" : Number(value).toFixed(2);
  }
  function role(value) {
    return ({ user: "用户", agent: "助手", system: "系统", developer: "开发者" })[value] || value;
  }
  function verdictClass(value) {
    return value === "pass" ? "pass" : value === "fail" ? "fail" : "";
  }
  function facts(parent, pairs) {
    const grid = node("div", undefined, "inspector-facts");
    pairs.forEach(([label, value]) => {
      const fact = node("div", undefined, "inspector-fact");
      append(fact, "b", label);
      append(fact, "span", value);
      grid.append(fact);
    });
    parent.append(grid);
  }
  function clear(title, intro) {
    inspector.replaceChildren();
    append(inspector, "h3", title);
    append(inspector, "p", intro, "inspector-empty");
  }
  function setSelection(trialId, eventId, bucketId) {
    selectedTrial = trialId;
    selectedEvent = eventId || null;
    selectedBucket = bucketId || null;
    document.querySelectorAll(".is-selected").forEach((element) => element.classList.remove("is-selected"));
    document.querySelectorAll("[aria-selected='true']").forEach((element) => element.setAttribute("aria-selected", "false"));
    document.querySelectorAll(`[data-trial='${trialId}']`).forEach((element) => { element.classList.add("is-selected"); element.setAttribute("aria-selected", "true"); });
    let selectedMarks = [];
    if (eventId) selectedMarks = [...document.querySelectorAll(`[data-event='${eventId}']`)];
    if (!selectedMarks.length && eventId) {
      const containing = [...bucketById.entries()].find(([, item]) => item.trial.id === trialId && item.bucket.event_ids.includes(eventId));
      if (containing) selectedBucket = containing[0];
    }
    if (selectedBucket) selectedMarks.push(...document.querySelectorAll(`[data-bucket='${selectedBucket}']`));
    selectedMarks.forEach((element) => { element.classList.add("is-selected"); element.setAttribute("aria-selected", "true"); });
  }
  function renderTools(parent, tools) {
    if (!tools.length) { append(parent, "p", "该试次无工具调用。"); return; }
    const table = node("table");
    const header = node("tr");
    ["工具", "调用", "无观测", "重试", "平均耗时"].forEach((value) => append(header, "th", value));
    table.append(header);
    tools.forEach((tool) => {
      const row = node("tr");
      [tool.name, number(tool.calls), number(tool.missing_obs), number(tool.retries), seconds(tool.avg_seconds)].forEach((value) => append(row, "td", value));
      table.append(row);
    });
    parent.append(table);
  }
  function renderMetrics(parent, metrics) {
    if (!metrics.length) { append(parent, "p", "该套件未声明可展示的 turn 指标。"); return; }
    const list = node("div", undefined, "inspector-list");
    metrics.forEach((metric) => {
      const item = node("div", undefined, `inspector-detail ${verdictClass(metric.status === "ok" ? "pass" : metric.status === "violated" ? "fail" : "")}`);
      append(item, "b", `${metric.name} · ${score(metric.score)}`);
      append(item, "div", metric.status);
      metric.reasons.forEach((reason) => append(item, "div", reason));
      list.append(item);
    });
    parent.append(list);
  }
  function renderTrial(trialId) {
    const trial = trialById.get(trialId);
    if (!trial) return;
    setSelection(trialId, null);
    clear(trial.heading, trial.meta);
    const stats = trial.stats;
    facts(inspector, [
      ["判定", stats.verdict_label], ["总时长", seconds(stats.wall_clock_seconds)],
      ["总 Token", number(stats.total_tokens)], ["峰值上下文", number(stats.peak_input_tokens)],
      ["步骤", number(stats.total_steps)], ["工具调用", number(stats.tool_calls_total)],
      ["无观测", number(stats.tool_missing_obs_total)], ["重试", number(stats.tool_retries_total)],
    ]);
    if (stats.missing.length) append(inspector, "p", `部分数据不可用：${stats.missing.join("；")}`, "inspector-warning");
    append(inspector, "h4", "工具结果");
    renderTools(inspector, stats.tools);
    append(inspector, "h4", "轨迹级指标");
    renderMetrics(inspector, trial.metrics);
  }
  function renderMessage(parent, title, message) {
    if (!message) return;
    append(parent, "h4", title);
    append(parent, "div", message.text, "inspector-message");
  }
  function renderEvent(eventId) {
    const selected = eventById.get(eventId);
    if (!selected) return;
    const { trial, event } = selected;
    setSelection(trial.id, eventId);
    clear(`步骤 ${event.step_id}`, `${trial.heading} · ${role(event.source)}`);
    const windowLabel = workspace.context_window_source === "evidence" ? "证据携带窗口" : "调用方声明窗口";
    const occupancy = workspace.context_window && event.prompt_tokens !== null && event.prompt_tokens !== undefined
      ? `${Math.round(event.prompt_tokens / workspace.context_window * 100)}%（${windowLabel}）` : "未声明窗口";
    facts(inspector, [
      ["坐标", workspace.axis.mode === "time" ? seconds(event.seconds) : `步骤序 ${Number(event.axis_value) + 1}`],
      ["时间戳", number(event.timestamp)], ["角色", role(event.source)],
      ["复制上下文", event.copied ? "是（记忆基底）" : "否"],
      ["输入 Token", number(event.prompt_tokens)], ["缓存 Token", event.cached_tokens === null || event.cached_tokens === undefined ? "未拆分" : number(event.cached_tokens)],
      ["输出 Token", number(event.completion_tokens)], ["上下文占用", occupancy],
      ["工具调用", number(event.tool_calls)], ["步骤耗时", event.duration === null || event.duration === undefined ? "—" : `${seconds(event.duration)}（含模型推理）`],
      ["观测", event.observation_missing ? "无观测信号" : "有或不涉及工具"],
    ]);
    renderMessage(inspector, "消息", trial.messages[event.message_step_id]);
    const tools = trial.tools_by_step[String(event.step_id)] || [];
    if (tools.length) {
      append(inspector, "h4", "本步骤工具调用");
      const list = node("div", undefined, "inspector-list");
      tools.forEach((tool) => {
        const item = node("div", `${tool.name} · ${tool.observation_present ? "有观测" : "无观测"}`, `inspector-detail ${tool.observation_present ? "pass" : "fail"}`);
        append(item, "div", JSON.stringify(tool.arguments));
        list.append(item);
      });
      inspector.append(list);
    }
    const turn = event.turn_id ? trial.turns[event.turn_id] : null;
    if (!turn) { append(inspector, "p", "该步骤无 turn 归因数据。"); return; }
    append(inspector, "h4", `T${turn.index} 归因`);
    facts(inspector, [
      ["turn 分", score(turn.score)], ["结构", score(turn.structure)],
      ["已应答", turn.answered ? "是" : "否"], ["回复字数", number(turn.reply_chars)],
      ["工具闭环", turn.tool_loop === null ? "—" : `${Math.round(turn.tool_loop * 100)}%`], ["记忆基底", turn.copied ? "是" : "否"],
    ]);
    renderMessage(inspector, "该轮用户消息", trial.messages[String(turn.user_step_id)]);
    if (turn.details.length) {
      append(inspector, "h4", "判分理由");
      const list = node("div", undefined, "inspector-list");
      turn.details.forEach((detail) => {
        const item = node("div", undefined, `inspector-detail ${detail.score >= 0.9 ? "pass" : "fail"}`);
        append(item, "b", `${detail.metric} · ${detail.label} · ${score(detail.score)}`);
        append(item, "div", detail.reason);
        if (detail.reply_step_id !== null && detail.reply_step_id !== undefined) {
          const reply = trial.events.find((candidate) => candidate.step_id === detail.reply_step_id);
          if (reply) {
            const jump = node("button", `定位到步骤 ${detail.reply_step_id}`);
            jump.dataset.event = reply.id;
            item.append(jump);
          }
        }
        list.append(item);
      });
      inspector.append(list);
    }
  }
  function renderBucket(bucketId) {
    const selected = bucketById.get(bucketId);
    if (!selected) return;
    const { trial, bucket } = selected;
    setSelection(trial.id, null, bucketId);
    clear("步骤密度桶", `${trial.heading} · ${bucket.count} 个步骤；可继续定位原始步骤。`);
    facts(inspector, [
      ["工具调用", number(bucket.tool_calls)], ["无观测", bucket.observation_missing ? "含无观测信号" : "无"],
      ["输入 Token 总量", number(bucket.prompt_tokens)], ["输出 Token 总量", number(bucket.completion_tokens)],
      ["上下文峰值", number(bucket.context_peak)], ["已知步骤耗时", seconds(bucket.duration)],
    ]);
    if (bucket.tool_names.length) append(inspector, "p", `工具：${bucket.tool_names.join("、")}`);
    const list = node("div", undefined, "inspector-list");
    bucket.event_ids.forEach((eventId) => {
      const event = eventById.get(eventId).event;
      const button = node("button", `步骤 ${event.step_id} · ${role(event.source)}`);
      button.dataset.event = eventId;
      list.append(button);
    });
    inspector.append(list);
  }
  function activate(target) {
    const eventId = target.closest("[data-event]")?.dataset.event;
    if (eventId) { renderEvent(eventId); return; }
    const bucketId = target.closest("[data-bucket]")?.dataset.bucket;
    if (bucketId) { renderBucket(bucketId); return; }
    const trialId = target.closest("[data-trial]")?.dataset.trial;
    if (trialId) renderTrial(trialId);
  }
  document.addEventListener("click", (event) => activate(event.target));
  document.addEventListener("keydown", (event) => {
    if ((event.key === "Enter" || event.key === " ") && event.target.closest("[data-event],[data-bucket],[data-trial]")) {
      event.preventDefault();
      activate(event.target);
    }
  });
  clear("详情检查器", workspace.dense ? "当前轨迹已按密度分桶；选择轨道或桶继续下钻。" : "选择试次或时间轴上的步骤，查看密封证据与判分归因。");
})();"""


def _insights_html(insights: Sequence[dict[str, str]]) -> str:
    if not insights:
        return '<div class="insights"><p class="insight muted">未发现可从密封证据直接陈述的异常或关注项。</p></div>'
    cards = []
    for insight in insights:
        target = (
            f'data-event="{_e(insight["event_id"])}"'
            if "event_id" in insight
            else f'data-trial="{_e(insight["trial_id"])}"'
        )
        cards.append(
            f'<button type="button" class="insight {_e(insight["kind"])}" {target}>'
            f'<b>{_e(insight["title"])}</b><span>{_e(insight["detail"])}</span></button>'
        )
    return '<div class="insights" aria-label="异常与关注项">' + "".join(cards) + "</div>"


def _workbench_html(workspace: dict[str, Any]) -> str:
    dense_note = (
        "步骤超过交互点预算，五轨展示密度桶；点击桶可继续进入原始步骤。"
        if workspace["dense"]
        else "点击任一轨道标记可同步高亮同一步，并在右侧查看消息、工具观测与 turn 判分理由。"
    )
    return (
        '<section class="trajectory-workbench"><div class="workbench-head">'
        '<h2>五轨联动执行视图</h2><p>执行、工具、Token、输入Token 与上下文压力共享横轴；纵向轨道只作对比，不表示并发。</p>'
        f'</div><p class="workbench-note">{_e(workspace["axis"]["note"])} {dense_note}</p>'
        f'{_insights_html(workspace["insights"])}'
        '<div class="workbench-grid"><div class="timeline-scroll">'
        f'{_timeline_svg(workspace)}</div><aside id="trajectory-inspector" class="inspector" '
        'aria-live="polite" aria-label="轨迹详情检查器"></aside></div>'
        '<div class="legend"><span><i class="legend-agent"></i>助手</span>'
        '<span><i class="legend-user"></i>用户</span><span><i class="legend-system"></i>系统/开发者</span>'
        '<span><i class="legend-failed"></i>工具无观测</span>'
        '<span><i class="legend-token-cached"></i>缓存输入</span>'
        '<span><i class="legend-token-uncached"></i>未缓存输入</span>'
        '<span><i class="legend-token-output"></i>推理</span>'
        '<span><i class="legend-token-visible"></i>可见输出</span>'
        '<span>虚线轨道为记忆基底</span></div></section>'
        f'<script id="trajectory-data" type="application/json">{_safe_json(workspace)}</script>'
        f'<script>{_INTERACTION_JS}</script>'
    )


def render_trajectory_html(
    *,
    title: str,
    meta_lines: Sequence[str],
    trials: Sequence[TrialPanel],
    context_window: int | None = None,
) -> str:
    """渲染单文件、离线可交互的轨迹分析面板。"""
    workspace = _workspace_view(trials, context_window)
    head_meta = "".join(f"<li>{_e(line)}</li>" for line in meta_lines)
    return (
        "<!doctype html><html lang=\"zh\"><head><meta charset=\"utf-8\">"
        "<meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">"
        f"<title>{_e(title)}</title><style>{_CSS}{_PANEL_CSS}</style></head><body>"
        "<div class=\"panel-page\">"
        f"<h1>{_e(title)}</h1><ul class=\"meta\">{head_meta}</ul>"
        f"{_workbench_html(workspace)}"
        '<footer><p>aeval 轨迹面板 · 数据全部来自 sha256 校验的密封轨迹证据；'
        '缺失处如实标注不可用，不编造。turn 分和判分理由复用轨迹级判分的同一条路径；'
        '记忆基底轮不进 live 归因。对话正文超长时截断至 '
        f'{_SNIPPET} 字，并以文本节点安全展示。</p></footer></div></body></html>'
    )
