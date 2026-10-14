"""自包含、离线交互的密封轨迹分析面板。"""

from __future__ import annotations

import html as _html
import json
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Sequence

from aeval.metrics.dashboard import _CSS
from aeval.verdict.trajectory.quality import ProbeDetail
from aeval.verdict.trajectory.stats import TrajectoryStats
from aeval.verdict.trajectory.turns import TurnAnalysis

if TYPE_CHECKING:  # pragma: no cover - typing only
    from aeval.verdict.trajectory.failures import FailureReport

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
    #: 失败摘要（判分器理由 / 任务 verifier 的失败断言 / 相关步骤）。
    #: None = 该试次不是失败，或没有失败信息可陈述——面板不编原因。
    failure: "FailureReport | None" = None


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
    involved_steps = set(panel.failure.implicated_steps) if panel.failure else set()
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
                "first_token_ms": step.first_token_ms,
                "tool_names": list(step.tool_names),
                "tool_calls": step.tool_calls,
                "observation_missing": step.observation_missing,
                "involved": step.step_id in involved_steps,
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
    failure = None
    if panel.failure is not None:
        failure = {
            "verdict": panel.failure.verdict,
            "verdict_label": _VERDICT_ZH.get(
                panel.failure.verdict or "", panel.failure.verdict or "未判定"
            ),
            "reasons": list(panel.failure.reasons),
            "failing_tests": [
                {"name": test.name, "message": test.message}
                for test in panel.failure.failing_tests
            ],
            "implicated_steps": list(panel.failure.implicated_steps),
            "path_steps": list(panel.failure.path_steps),
            "composed_steps": list(panel.failure.composed_steps),
            "grader_steps": list(panel.failure.grader_steps),
            "step_basis": panel.failure.step_basis,
            "step_basis_label": {
                "path": "失败详情里的路径与该步工具调用参数字面相等",
                "composed": "失败详情里路径的目录与文件名分别出现在该步参数里",
                "grader": "判分器观测到工具调用失败的步骤",
            }.get(panel.failure.step_basis or "", None),
            "verifier_source": panel.failure.verifier_source,
            "verifier_sha256": panel.failure.verifier_sha256,
            "verifier_excerpt": panel.failure.verifier_excerpt,
            "unavailable": list(panel.failure.unavailable),
            "summary": panel.failure.summary,
        }
    return {
        "id": f"trial-{position}",
        "heading": panel.heading,
        # 页内序号（1..N）：统计带的逐试次行用它，行内 tooltip 里才出现完整标题。
        "label": f"试次 {position + 1}",
        "meta": panel.meta,
        "failure": failure,
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
    """返回可绘制点；密集模式仍携带六轨聚合事实和所有原始事件 ID。"""
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
        first_tokens = [
            event["first_token_ms"] for event in items
            if event.get("first_token_ms") is not None
        ]
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
                "involved": any(event.get("involved") for event in items),
                "duration": sum(durations) if durations else None,
                # 桶内的首 token 时延：给最快/最慢与出现次数，不给"平均值"——
                # 平均值会被读成一个真实观测，而它只是把不同调用混在一起。
                "first_token_ms": min(first_tokens) if first_tokens else None,
                "first_token_max_ms": max(first_tokens) if first_tokens else None,
                "first_token_count": len(first_tokens),
                "copied": any(event["copied"] for event in items),
            }
        )
    return output


#: 关注项的类别与呈现顺序：同类事实收敛成一张卡，卡内逐试次一行。
_INSIGHT_ORDER = (
    "failure", "no_observation", "metric", "retry", "peak_context",
    "longest_step", "unavailable",
)


def _trial_short_label(heading: str) -> str:
    """`试次 2/3 · <id>` → `试次 2/3`（卡内行标签用短名，完整标题留在 title 上）。"""
    return heading.split(" · ", 1)[0].strip() or heading


def _workspace_insights(views: Sequence[dict[str, Any]], context_window: int | None,
                        window_source: str = "caller") -> list[dict[str, Any]]:
    """从已有密封事实挑出可导航的关注项，**按类别收敛、卡内按试次逐条列出**。

    同一类事实只有一张卡（试次失败 / 工具无观测 / 峰值上下文 / 最长含工具步骤 /
    数据不可用 …），每张卡的 `rows` 按试次序排列，行内是该试次的实测值；
    收敛只做分组与排序，不做跨试次平均——不同试次的值不会被压成一个数字。
    """
    total_trials = len(views)
    groups: dict[str, dict[str, Any]] = {}

    def add(category: str, kind: str, trial: dict[str, Any], detail: str,
            event_id: str | None = None, **values: Any) -> None:
        group = groups.setdefault(category, {"kind": kind, "rows": [], "values": []})
        row: dict[str, Any] = {
            "label": trial.get("label") or _trial_short_label(trial["heading"]),
            "detail": detail,
            "heading": trial["heading"],
            "trial_id": trial["id"],
        }
        if event_id:
            row["event_id"] = event_id
        group["rows"].append(row)
        group["values"].append(values)

    for trial in views:
        stats = trial["stats"]
        for event in trial["events"]:
            if not event["observation_missing"]:
                continue
            add(
                "no_observation", "danger", trial,
                f'步骤 {event["step_id"]} · ' + "、".join(event["tool_names"]),
                event_id=event["id"],
                tools=tuple(event["tool_names"]),
            )
        if stats["tool_retries_total"]:
            add("retry", "warn", trial, "相邻同名同参调用",
                retries=stats["tool_retries_total"])
        tool_events = [
            event for event in trial["events"]
            if event["tool_calls"] and event["duration"] is not None
        ]
        if tool_events:
            longest = max(tool_events, key=lambda event: float(event["duration"] or 0.0))
            add(
                "longest_step", "info", trial,
                f'步骤 {longest["step_id"]} · {_secs(longest["duration"])}（含模型推理）',
                event_id=longest["id"],
                duration=longest["duration"],
            )
        if stats["peak_step_id"] is not None:
            peak = next(
                (event for event in trial["events"] if event["step_id"] == stats["peak_step_id"]),
                None,
            )
            if peak is not None:
                occupancy = None
                if context_window and stats["peak_input_tokens"] is not None:
                    occupancy = stats["peak_input_tokens"] / context_window
                suffix = ""
                if occupancy is not None:
                    window_noun = (
                        "证据携带窗口" if window_source == "evidence" else "声明窗口"
                    )
                    suffix = f' · 占{window_noun} {occupancy:.0%}'
                add(
                    "peak_context",
                    "warn" if occupancy is not None and occupancy >= 0.7 else "info",
                    trial,
                    f'步骤 {peak["step_id"]} · {_int(stats["peak_input_tokens"])} tokens{suffix}',
                    event_id=peak["id"],
                    tokens=stats["peak_input_tokens"],
                    occupancy=occupancy,
                    step_id=peak["step_id"],
                )
        for turn in trial["turns"].values():
            for detail in turn["details"]:
                if detail["score"] >= 0.9 or detail["reply_step_id"] is None:
                    continue
                reply = next(
                    (event for event in trial["events"]
                     if event["step_id"] == detail["reply_step_id"]),
                    None,
                )
                if reply is not None:
                    add(
                        f'metric:{detail["metric"]}', "danger", trial,
                        f'{detail["label"]} · 得分 {detail["score"]:.2f}',
                        event_id=reply["id"],
                        metric=detail["metric"],
                        score=detail["score"],
                    )
        failure = trial.get("failure")
        if failure is not None:
            steps = failure["implicated_steps"]
            detail = failure["summary"]
            if steps:
                detail += " · 相关步骤 " + "、".join(str(step) for step in steps)
            # 行目标就是试次本身：点开要看的是失败原因卡（卡内再提供步骤 chip）。
            # 直接跳到"第一个相关步骤"会让读者看不到原因，只有步骤详情。
            add(
                "failure", "danger", trial, detail,
                steps=tuple(steps),
            )
        for reason in stats["missing"]:
            add("unavailable", "muted", trial, reason, reason=reason)

    cards: list[dict[str, Any]] = []
    for category, group in groups.items():
        rows = group["rows"]
        values = group["values"]
        trials_with = len({row["trial_id"] for row in rows})

        def span(key: str, fmt) -> str:
            seen = [value[key] for value in values if value.get(key) is not None]
            if not seen:
                return ""
            low, high = min(seen), max(seen)
            return fmt(low) if low == high else f"{fmt(low)}–{fmt(high)}"

        if category == "failure":
            title, kind = "试次失败", "danger"
            located = sum(1 for value in values if value.get("steps"))
            located_steps = sum(len(value["steps"]) for value in values if value.get("steps"))
            detail = f"{trials_with}/{total_trials} 试次失败"
            detail += (
                f" · {located}/{trials_with} 试次已定位相关步骤（共 {located_steps} 步）"
                if located else " · 相关步骤未定位"
            )
        elif category == "no_observation":
            names = sorted({name for value in values for name in value["tools"]})
            title, kind = "工具无观测信号", "danger"
            detail = (
                f"{len(rows)} 处 · {trials_with}/{total_trials} 试次"
                + (f' · 工具 {"、".join(names)}' if names else "")
            )
        elif category.startswith("metric:"):
            name = category.split(":", 1)[1]
            title, kind = f"turn 指标 {name} 低于阈值", "danger"
            detail = f"{trials_with}/{total_trials} 试次 · 得分 {span('score', lambda value: f'{value:.2f}')}"
        elif category == "retry":
            title, kind = "工具重试", "warn"
            detail = (
                f"{trials_with}/{total_trials} 试次 · 每试次 "
                + span("retries", lambda value: f"{value} 次")
            )
        elif category == "peak_context":
            title, kind = "峰值上下文", "info" if group["kind"] == "info" else "warn"
            peaks = [
                (value["tokens"], row["label"]) for value, row in zip(values, rows)
                if value.get("tokens") is not None
            ]
            highest, highest_label = max(peaks)
            detail = f"各试次峰值 {span('tokens', _int)} tokens · {trials_with}/{total_trials} 试次"
            if len(peaks) > 1:
                detail += f" · 最高 {_int(highest)}（{highest_label}）"
            if context_window:
                window_noun = "证据携带窗口" if window_source == "evidence" else "声明窗口"
                occupancy = max(
                    (value["occupancy"] for value in values if value.get("occupancy") is not None),
                    default=0.0,
                )
                detail += f" · 最高占{window_noun} {occupancy:.0%}"
        elif category == "longest_step":
            title, kind = "最长含工具步骤", "info"
            durations = [
                (value["duration"], row["label"]) for value, row in zip(values, rows)
                if value.get("duration") is not None
            ]
            longest, longest_label = max(durations)
            detail = (
                f"各试次最长 {span('duration', _secs)} · {trials_with}/{total_trials} 试次"
                "（含模型推理）"
            )
            if len(durations) > 1:
                detail += f" · 最长 {_secs(longest)}（{longest_label}）"
        else:
            title, kind = "数据不可用", "muted"
            reasons_with = len({value["reason"] for value in values if value.get("reason")})
            detail = (
                f"{len(rows)} 条 · {trials_with}/{total_trials} 试次"
                + (f" · {reasons_with} 类原因" if reasons_with else "")
            )
        cards.append((category, {"kind": kind, "title": title, "detail": detail, "rows": rows}))

    def rank(category: str) -> int:
        if category in _INSIGHT_ORDER:
            return _INSIGHT_ORDER.index(category)
        # `metric:<name>` cards sit right after the fixed categories, by name
        return len(_INSIGHT_ORDER)

    cards.sort(key=lambda item: (rank(item[0]), item[0]))
    return [card for _category, card in cards][:12]


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
        "failure_summary": {
            "failed_trials": sum(1 for view in views if view.get("failure")),
            "located_trials": sum(
                1 for view in views
                if view.get("failure") and view["failure"]["implicated_steps"]
            ),
            "verifier_details": sum(
                1 for view in views
                if view.get("failure") and view["failure"]["failing_tests"]
            ),
            "implicated_steps": sum(
                len(view["failure"]["implicated_steps"])
                for view in views if view.get("failure")
            ),
        },
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
    if item.get("involved"):
        classes.append("is-involved")
        label += " · 失败归因相关步骤"
    ring = f'<circle class="failure-ring" cx="{x:.1f}" cy="{y:.1f}" r="9"/>' if item.get("observation_missing") else ""
    if item.get("involved"):
        ring += f'<circle class="involvement-ring" cx="{x:.1f}" cy="{y:.1f}" r="11"/>'
    return _event_group(item, label, "execution", ring + shape, " ".join(classes))


def _event_group(item: dict[str, Any], label: str, track: str, content: str, extra: str = "") -> str:
    attrs = _target_attrs(item, label, track)
    if extra:
        attrs = attrs.replace('class="timeline-event', f'class="timeline-event {extra}')
    return f'<g {attrs}><title>{_e(label)}</title>{content}</g>'


def _timeline_svg(workspace: dict[str, Any]) -> str:
    """一条共享 X 轴上的执行、工具、Token、输入Token、上下文、首Token六轨。"""
    trials = workspace["trials"]
    axis = workspace["axis"]
    scales = workspace["scales"]
    lanes = max(1, len(trials))
    execution_h = 52 + lanes * 34
    tool_h = 54 + lanes * 34
    token_h = 62 + lanes * 38
    input_token_h = 64 + lanes * 42
    context_h = 64 + lanes * 42
    latency_h = 64 + lanes * 42
    top = 48
    execution_top = top
    tool_top = execution_top + execution_h + 12
    token_top = tool_top + tool_h + 12
    input_token_top = token_top + token_h + 12
    context_top = input_token_top + input_token_h + 12
    latency_top = context_top + context_h + 12
    h = latency_top + latency_h + 20
    plot_start = _TIMELINE_LABEL_W
    plot_end = _TIMELINE_W - _TIMELINE_PAD
    plot_width = plot_end - plot_start
    axis_max = float(axis["max"])
    parts = [
        f'<svg class="trajectory-timeline" viewBox="0 0 {_TIMELINE_W} {h}" '
        f'role="img" aria-label="执行、工具、Token、输入Token、上下文压力与首 Token 时延六轨联动图">'
    ]
    for value, label in _axis_ticks(axis["mode"], axis_max):
        x = _x_of(value, axis_max, plot_start, plot_width)
        parts.append(
            f'<line class="timeline-grid" x1="{x:.1f}" y1="38" x2="{x:.1f}" y2="{h - 16}"/>'
            f'<text class="timeline-tick" x="{x:.1f}" y="30" text-anchor="middle">{_e(label)}</text>'
        )
    parts.append(f'<text class="timeline-axis-label" x="{plot_start}" y="14">{_e(axis["label"])}（六轨共享）</text>')

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
            # 缓存 token 占输入 token 的比例：柱下直接标百分比（有缓存计量时），
            # 悬停标题里也写全（缓存 X / 输入 Y）。没有缓存计量就标"—"，
            # 不写 0%——"未拆分"和"缓存为 0"是两件事。
            ratio_note = ""
            ratio = None
            if prompt is not None and float(prompt) > 0:
                if cached is None:
                    ratio_note = "缓存占输入 —（未拆分）"
                else:
                    ratio = min(float(cached), float(prompt)) / float(prompt)
                    ratio_note = (
                        f"缓存占输入 {ratio:.0%}（缓存 {_int(cached)} / 输入 {_int(prompt)}）"
                    )
            if ratio_note:
                label += " · " + ratio_note
            if ratio is not None:
                shapes.append(
                    f'<text class="token-ratio" x="{x:.1f}" y="{base + 12:.1f}" '
                    f'text-anchor="middle">{ratio:.0%}</text>'
                )
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

    # 首 token 时延：每个模型调用从发出到首个输出增量到达的墙钟时间。
    latency_values = [
        item["first_token_ms"]
        for trial in trials for item in trial["display_items"]
        if item.get("first_token_ms") is not None
    ]
    if latency_values:
        latency_note = (
            f"{len(latency_values)} 次调用有记录；最快 {int(min(latency_values))}ms · "
            f"最慢 {int(max(latency_values))}ms"
        )
    else:
        latency_note = "证据未记录首个输出增量（facade 调用账本无 first_token_ms）——不可用"
    heading(latency_top, "首 Token 时延", latency_note)
    latency_max = max(latency_values) if latency_values else 1.0
    for index, trial in enumerate(trials):
        base = latency_top + 66 + index * 42
        parts.append(_lane_label(trial, index, base))
        parts.append(f'<line class="track-baseline" x1="{plot_start}" y1="{base:.1f}" x2="{plot_end}" y2="{base:.1f}"/>')
        for item in trial["display_items"]:
            value = item.get("first_token_ms")
            if value is None:
                continue
            x = _x_of(float(item["axis_value"] or 0.0), axis_max, plot_start, plot_width)
            height = min(1.0, float(value) / latency_max) * 30.0
            if item["kind"] == "bucket":
                label = (
                    f'{item.get("count", 1)} 步聚合 · 首 token 最快 {int(value)}ms'
                    + (f' · 最慢 {int(item["first_token_max_ms"])}ms'
                       if item.get("first_token_max_ms") is not None else '')
                    + f'（{item.get("first_token_count", 0)}/{item.get("count", 1)} 次有记录）'
                )
            else:
                label = f'步骤 {item["step_id"]} · 首 token {int(value)}ms'
            parts.append(
                _event_group(
                    item, label, "latency",
                    f'<rect class="latency-bar" x="{x - 3:.1f}" y="{base - height:.1f}" '
                    f'width="6" height="{height:.1f}"/>',
                )
            )
    parts.append("</svg>")
    return "".join(parts)


_PANEL_CSS = """
.trajectory-workbench{margin:20px 0;border:1px solid #d7dce3;border-radius:12px;background:#fff;overflow:hidden}.workbench-head{padding:16px 18px 12px;border-bottom:1px solid #e4e8ed}.workbench-head h2{margin:0;font-size:18px;color:#1f2937}.workbench-head p{margin:5px 0 0;color:#5b6470;font-size:13px}.workbench-note{margin:0;padding:9px 18px;background:#f8fafc;border-bottom:1px solid #e4e8ed;color:#475569;font-size:12px}
.insights{display:grid;grid-template-columns:repeat(auto-fit,minmax(230px,1fr));gap:8px;padding:12px 18px;border-bottom:1px solid #e4e8ed;background:#fbfcfe}.insight{border:1px solid #dfe6ed;border-left-width:3px;border-radius:8px;padding:8px 10px;background:#fff;text-align:left;cursor:pointer;color:#1e293b;font:inherit}.insight b,.insight span{display:block}.insight b{font-size:12px}.insight span{margin-top:3px;color:#64748b;font-size:11px;line-height:1.35}.insight.danger{border-left-color:#dc2626}.insight.warn{border-left-color:#d97706}.insight.info{border-left-color:#2563eb}.insight.muted{border-left-color:#94a3b8}.insight:hover,.insight:focus{border-color:#2563eb;background:#eff6ff;outline:none}.insight-rows{display:flex;flex-direction:column;gap:2px;margin-top:6px;max-height:136px;overflow:auto}.insight-row{display:block;width:100%;text-align:left;border:0;background:transparent;padding:2px 4px;border-radius:4px;cursor:pointer;color:#1e293b;font:inherit}.insight-row:hover,.insight-row:focus{background:#eff6ff;outline:none}.insight-row b{font-size:11px;font-weight:600;color:#475569}.insight-row span{display:block;margin-top:1px;color:#334155;font-size:11px;line-height:1.3}
.panel-page{max-width:1600px;margin:0 auto;padding:24px 24px 40px}
.workbench-grid{display:grid;grid-template-columns:minmax(0,3fr) minmax(300px,1fr);min-height:520px}.timeline-scroll{overflow-x:hidden;overflow-y:auto;padding:12px;border-right:1px solid #e4e8ed}.trajectory-timeline{width:100%;height:auto;display:block}.timeline-grid{stroke:#e8edf2;stroke-width:1}.timeline-tick,.timeline-axis-label{fill:#64748b;font-size:11px}.timeline-axis-label{font-weight:600}.track-heading{fill:#1e293b;font-size:14px;font-weight:700}.track-note{fill:#64748b;font-size:10px}.track-baseline{stroke:#c9d3de;stroke-width:1}.track-trial-label{fill:#475569;font-size:10px;font-weight:600}.track-trial-verdict{font-size:9px}.status-pass{fill:#17834f}.status-fail{fill:#c53030}.status-cannot_judge{fill:#a16207}.copied-range{stroke:#94a3b8;stroke-width:8;stroke-linecap:round;stroke-dasharray:3 4;opacity:.45}
.timeline-event,.timeline-trial{cursor:pointer;outline:none}.timeline-trial:hover .track-trial-label,.timeline-trial:focus .track-trial-label,.timeline-trial.is-selected .track-trial-label{fill:#1d4ed8;text-decoration:underline}.timeline-event .event-shape{fill:#16805a;stroke:#fff;stroke-width:2}.timeline-event.source-user .event-shape{fill:#2563eb}.timeline-event.source-system .event-shape,.timeline-event.source-developer .event-shape{fill:#7c3aed}.timeline-event.is-copied .event-shape{fill:#94a3b8;stroke-dasharray:2 1}.timeline-event.is-failed .event-shape{fill:#dc2626}.failure-ring{fill:none;stroke:#991b1b;stroke-width:2}.involvement-ring{fill:none;stroke:#b45309;stroke-width:2;stroke-dasharray:3 2}.timeline-event.is-involved .event-shape{stroke:#b45309;stroke-width:2.5}.failure-card{border:1px solid #f0c390;background:#fffbf3;border-radius:8px;padding:8px 10px;margin:6px 0}.failure-card h5{margin:0 0 6px;font-size:12px;color:#92400e}.failure-card .failure-verdict{font-weight:600;color:#b91c1c}.failure-card .failure-test{font-family:ui-monospace,Menlo,monospace;font-size:11px;color:#7f1d1d;margin:3px 0;white-space:pre-wrap}.failure-card .failure-reason{font-size:11px;color:#78350f;margin:3px 0}.failure-card .failure-note{font-size:11px;color:#6b7280;margin:3px 0}.failure-card .failure-excerpt{font-family:ui-monospace,Menlo,monospace;font-size:10px;line-height:1.35;background:#fff;border:1px solid #f0e0c8;border-radius:6px;padding:6px;margin:4px 0;max-height:220px;overflow:auto;white-space:pre-wrap}.failure-steps{display:flex;flex-wrap:wrap;gap:6px;margin:6px 0 2px}.failure-chip{border:1px solid #b45309;background:#fff;color:#92400e;border-radius:999px;padding:1px 8px;font-size:11px;cursor:pointer}.failure-chip:hover{background:#fef3c7}.tool-duration{fill:#6474dc;opacity:.75}.timeline-event.is-failed .tool-duration{fill:#dc2626}.unknown-duration{fill:#94a3b8}.tool-name{fill:#6d28d9;font-size:9px}.token-cached{fill:#2563eb}.token-uncached{fill:#93c5fd}.token-output{fill:#f59e0b}.token-unsegmented{fill:#94a3b8}.token-ratio{fill:#475569;font-size:8px}.context-line{fill:none;stroke-width:1.8;opacity:.75}.context-line-0{stroke:#2563eb}.context-line-1{stroke:#16a34a}.context-line-2{stroke:#7c3aed}.context-line-3{stroke:#d97706}.context-point{fill:#fff;stroke:#2563eb;stroke-width:1.8}.peak-context{fill:#dc2626;stroke:#7f1d1d}.context-threshold{stroke:#dc2626;stroke-dasharray:4 3;stroke-width:1;opacity:.55}.latency-bar{fill:#0e7490}.timeline-event:hover .latency-bar,.timeline-event:focus .latency-bar,.timeline-event.is-selected .latency-bar{stroke:#111827;stroke-width:2}.threshold-label{fill:#b91c1c;font-size:9px}.timeline-event:hover .event-shape,.timeline-event:focus .event-shape,.timeline-event.is-selected .event-shape{stroke:#111827;stroke-width:3}.timeline-event:hover .tool-duration,.timeline-event:focus .tool-duration,.timeline-event.is-selected .tool-duration{stroke:#111827;stroke-width:2}.timeline-event:hover .token-cached,.timeline-event:hover .token-uncached,.timeline-event:hover .token-output,.timeline-event:hover .token-unsegmented,.timeline-event:focus .token-cached,.timeline-event:focus .token-uncached,.timeline-event:focus .token-output,.timeline-event:focus .token-unsegmented,.timeline-event.is-selected .token-cached,.timeline-event.is-selected .token-uncached,.timeline-event.is-selected .token-output,.timeline-event.is-selected .token-unsegmented{stroke:#111827;stroke-width:2}.timeline-event:hover .context-point,.timeline-event:focus .context-point,.timeline-event.is-selected .context-point{stroke:#111827;stroke-width:3}
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
    // 深链：把当前选择写进 URL fragment，复制链接即可定位到同一试次/步骤，
    // 截图与文档引用也能指向具体失败步骤。
    const anchor = eventId || bucketId || trialId;
    if (anchor && location.hash !== `#${anchor}`) {
      history.replaceState(null, "", `#${anchor}`);
    }
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
  function latencyText(event) {
    const value = event.first_token_ms;
    if (value === null || value === undefined) return "未记录";
    return `${number(value)}ms`;
  }
  function cachedRatio(event) {
    const cached = event.cached_tokens;
    const prompt = event.prompt_tokens;
    if (cached === null || cached === undefined) return "未拆分";
    if (prompt === null || prompt === undefined || prompt === 0) return "—";
    return `${Math.round(Math.min(cached, prompt) / prompt * 100)}%（${number(cached)}/${number(prompt)}）`;
  }
  function renderFailure(parent, trial) {
    const failure = trial.failure;
    if (!failure) return;
    const card = node("div", undefined, "failure-card");
    append(card, "h5", "失败原因");
    append(card, "div", `判定：${failure.verdict_label}`, "failure-verdict");
    if (failure.failing_tests.length) {
      append(card, "div", "任务自带 verifier 报出的失败用例：", "failure-note");
      failure.failing_tests.forEach((test) => {
        append(card, "div", `${test.name}: ${test.message}`, "failure-test");
      });
    } else if (failure.summary) {
      append(card, "div", failure.summary, "failure-test");
    }
    if (failure.reasons.length) {
      append(card, "div", "判分器理由（理由文本逐字引用；前缀「判分器@版本」或「判分器@版本 · 指标名」是来源标签）：", "failure-note");
      failure.reasons.forEach((reason) => append(card, "div", reason, "failure-reason"));
    }
    function chipRow(label, stepIds) {
      if (!stepIds.length) return;
      append(card, "div", label, "failure-note");
      const chips = node("div", undefined, "failure-steps");
      stepIds.forEach((stepId) => {
        const event = trial.events.find((candidate) => candidate.step_id === stepId);
        const chip = node("button", `步骤 ${stepId}`, "failure-chip");
        if (event) { chip.dataset.event = event.id; }
        chips.append(chip);
      });
      card.append(chips);
    }
    chipRow("相关步骤 · 路径字面相等（失败详情里的路径完整出现在该步工具调用参数里）——点击定位：", failure.path_steps || []);
    chipRow("相关步骤候选 · 目录+文件名分别出现（证据未写明解析后的路径，故只作候选）：", failure.composed_steps || []);
    chipRow("相关步骤 · 判分器观测到工具调用失败（相关，不等于成因）：", failure.grader_steps || []);
    if (!failure.implicated_steps.length) {
      append(card, "div", "相关步骤：未定位（失败详情里的路径没有完整出现、其目录与文件名也没有分别出现在任一步的参数里，判分器证据也没有给出 step 编号）", "failure-note");
    }
    (failure.unavailable || []).forEach((note) => append(card, "div", note, "failure-note"));
    if (failure.verifier_source) {
      const digest = (failure.verifier_sha256 || "").slice(0, 12);
      append(card, "div", `任务 verifier 输出：${failure.verifier_source}（sha256 ${digest}…，与密封清单一致）`, "failure-note");
      if (failure.verifier_excerpt) {
        const details = node("details");
        append(details, "summary", "展开 verifier 原始输出（截断）", "failure-note");
        const pre = node("pre", failure.verifier_excerpt, "failure-excerpt");
        details.append(pre);
        card.append(details);
      }
    }
    parent.append(card);
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
    renderFailure(inspector, trial);
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
    const involved = trial.failure && trial.failure.implicated_steps.includes(event.step_id);
    const windowLabel = workspace.context_window_source === "evidence" ? "证据携带窗口" : "调用方声明窗口";
    const occupancy = workspace.context_window && event.prompt_tokens !== null && event.prompt_tokens !== undefined
      ? `${Math.round(event.prompt_tokens / workspace.context_window * 100)}%（${windowLabel}）` : "未声明窗口";
    facts(inspector, [
      ["坐标", workspace.axis.mode === "time" ? seconds(event.seconds) : `步骤序 ${Number(event.axis_value) + 1}`],
      ["时间戳", number(event.timestamp)], ["角色", role(event.source)],
      ["复制上下文", event.copied ? "是（记忆基底）" : "否"],
      ["输入 Token", number(event.prompt_tokens)], ["缓存 Token", event.cached_tokens === null || event.cached_tokens === undefined ? "未拆分" : number(event.cached_tokens)],
      ["缓存/输入", cachedRatio(event)],
      ["首 token 时延", latencyText(event)],
      ["输出 Token", number(event.completion_tokens)], ["上下文占用", occupancy],
      ["工具调用", number(event.tool_calls)], ["步骤耗时", event.duration === null || event.duration === undefined ? "—" : `${seconds(event.duration)}（含模型推理）`],
      ["观测", event.observation_missing ? "无观测信号" : "有或不涉及工具"],
    ]);
    if (involved) {
      // 被标记为失败相关的步骤：把失败原因卡直接放在这一步的详情里，
      // 读者不用先跳回试次才知道为什么。
      renderFailure(inspector, trial);
      const isPath = (trial.failure.path_steps || []).includes(event.step_id);
      const isComposed = (trial.failure.composed_steps || []).includes(event.step_id);
      const why = isPath
        ? "失败详情提到的路径完整出现在该步的工具调用参数里"
        : isComposed
          ? "失败详情里路径的目录与文件名分别出现在该步参数里（候选，非字面相等）"
          : "判分器观测到该步有工具调用失败（相关，不等于成因）";
      append(inspector, "p", `该步骤被标记为失败相关步骤：${why}。`, "inspector-warning");
    }
    if (trial.failure && !involved) {
      const jump = node("button", "查看该试次失败原因");
      jump.dataset.trial = trial.id;
      inspector.append(jump);
    }
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
  function applyFragment() {
    const anchor = decodeURIComponent((location.hash || "").replace(/^#/, ""));
    if (!anchor) return false;
    if (eventById.has(anchor)) { renderEvent(anchor); return true; }
    const bucket = bucketById.get(anchor);
    if (bucket) { renderBucket(anchor); return true; }
    if (trialById.has(anchor)) { renderTrial(anchor); return true; }
    return false;
  }
  window.addEventListener("hashchange", () => { if (!applyFragment()) clear("详情检查器", "链接指向的试次或步骤不存在。"); });
  if (!applyFragment()) {
    clear("详情检查器", workspace.dense ? "当前轨迹已按密度分桶；选择轨道或桶继续下钻。" : "选择试次或时间轴上的步骤，查看密封证据与判分归因。");
  }
})();"""


def _insights_html(insights: Sequence[dict[str, Any]]) -> str:
    """一张卡一个类别，卡内每行一个试次（行可点击定位到对应步骤/试次）。

    没有 `rows` 的形状（外部调用方直接给的单项）退化成旧的单按钮卡片。
    """
    if not insights:
        return '<div class="insights"><p class="insight muted">未发现可从密封证据直接陈述的异常或关注项。</p></div>'
    cards = []
    for insight in insights:
        title = f'<b>{_e(insight["title"])}</b><span>{_e(insight["detail"])}</span>'
        rows = insight.get("rows") or []
        if not rows:
            target = (
                f'data-event="{_e(insight["event_id"])}"'
                if "event_id" in insight
                else f'data-trial="{_e(insight["trial_id"])}"'
            )
            cards.append(
                f'<button type="button" class="insight {_e(insight["kind"])}" {target}>{title}</button>'
            )
            continue
        row_html = []
        for row in rows:
            target = (
                f'data-event="{_e(row["event_id"])}"'
                if row.get("event_id")
                else f'data-trial="{_e(row["trial_id"])}"'
            )
            label = row.get("heading") or row["label"]
            row_html.append(
                f'<button type="button" class="insight-row" {target} '
                f'title="{_e(label)}"><b>{_e(row["label"])}</b>'
                f'<span>{_e(row["detail"])}</span></button>'
            )
        cards.append(
            f'<div class="insight {_e(insight["kind"])}">{title}'
            f'<div class="insight-rows" aria-label="{_e(insight["title"])}·逐试次">'
            f'{"".join(row_html)}</div></div>'
        )
    return '<div class="insights" aria-label="异常与关注项">' + "".join(cards) + "</div>"


def _workbench_html(workspace: dict[str, Any]) -> str:
    dense_note = (
        "步骤超过交互点预算，六轨展示密度桶；点击桶可继续进入原始步骤。"
        if workspace["dense"]
        else "点击任一轨道标记可同步高亮同一步，并在右侧查看消息、工具观测与 turn 判分理由。"
    )
    return (
        '<section class="trajectory-workbench"><div class="workbench-head">'
        '<h2>六轨联动执行视图</h2><p>执行、工具、Token、输入Token、上下文压力与首 Token 时延共享横轴；纵向轨道只作对比，不表示并发。首 token 时延来自 facade 调用账本的 first_token_ms（首个输出增量），未记录时该轨写"不可用"而不是 0。</p>'
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
        '缺失处如实标注不可用，不编造。失败试次给出「失败原因」卡：判分器理由逐字引用，'
        '任务 verifier 的失败断言只在 sha256 与运行级密封清单一致时读取；相关步骤用'
        '琥珀色虚线圈标在轨道上，并按规则分开标注：路径字面相等 / 目录+文件名分别出现'
        '（候选）/ 判分器观测到工具调用失败，三条都不成立就写「未定位」；'
        'URL fragment（如 #trial-2）可深链到某个试次或步骤。'
        'turn 分和判分理由复用轨迹级判分的同一条路径；'
        '记忆基底轮不进 live 归因。对话正文超长时截断至 '
        f'{_SNIPPET} 字，并以文本节点安全展示。</p></footer></div></body></html>'
    )
