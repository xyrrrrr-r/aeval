"""轨迹分析面板（turn 切面渲染）。

消费 :mod:`aeval.verdict.trajectory.turns` 的 ``TurnAnalysis``——
本模块不计算任何分数，只渲染：每个 trial 一节，每轮一张卡（用户消
息 → 回复 → 工具调用 → 归因判分 → turn 分）。自包含单文件 HTML，
与运行面板同一套样式纪律（内联 CSS/SVG、零脚本、字节确定）。
"""

from __future__ import annotations

from dataclasses import dataclass
from html import escape
from typing import Iterable, Sequence

from aeval.metrics.dashboard import _CSS, _e
from aeval.verdict.trajectory.metrics import MetricOutcome
from aeval.verdict.trajectory.quality import ProbeDetail, redact_snippet
from aeval.verdict.trajectory.turns import Turn, TurnAnalysis, TurnScore

__all__ = ["TrialView", "render_trajectory_html"]


@dataclass(frozen=True)
class TrialView:
    """一个 trial 的面板视图：坐标与判定（显示层）+ turn 切面。"""

    heading: str        # e.g. "试次 1/3 · memory.tenant_isolation-a1"
    meta: str           # e.g. "run-aeval-intel-1 · aeval-intel@0.3.0"
    verdict: str
    stop_reason: str
    analysis: TurnAnalysis

_TURN_CSS = """
.trial { background: var(--panel); border: 1px solid var(--line);
         border-radius: 10px; padding: 16px; margin-top: 14px; }
.trial h2 { margin: 0 0 4px; font-size: 16px; color: var(--ink); }
.trial .meta { color: var(--muted); font-size: 12.5px; }
.metrics { display: flex; flex-wrap: wrap; gap: 6px; margin-top: 10px; }
.mchip { background: var(--chipbg); border-radius: 6px; padding: 3px 8px;
         font-size: 12px; }
.mchip b { font-variant-numeric: tabular-nums; }
.mchip.skip { opacity: .62; }
.turns { display: grid; gap: 10px; margin-top: 12px; }
.turn { border: 1px solid var(--line); border-radius: 10px; padding: 12px 14px; }
.turn.copied { background: color-mix(in srgb, var(--muted) 7%, transparent);
               border-style: dashed; }
.thead { display: flex; align-items: center; gap: 10px; flex-wrap: wrap; }
.tno { font-weight: 700; font-size: 14px; }
.tbadge { font-size: 11.5px; border-radius: 12px; padding: 2px 9px;
          background: var(--chipbg); color: var(--muted); }
.tbadge.base { border: 1px dashed var(--muted); }
.tscore { margin-left: auto; font-size: 15px; font-weight: 700;
          font-variant-numeric: tabular-nums; }
.tscore.green { color: var(--pass); }
.tscore.yellow { color: #c08a2d; }
.tscore.red { color: var(--fail); }
.tscore.gray { color: var(--muted); }
.bubble { margin-top: 8px; padding: 8px 12px; border-radius: 9px;
          font-size: 13.5px; white-space: pre-wrap; word-break: break-word; }
.bubble .who { font-size: 11.5px; color: var(--muted); margin-bottom: 2px; }
.bubble.user { background: var(--chipbg); }
.bubble.agent { border: 1px solid var(--line); }
.tools { margin-top: 8px; font-size: 12.5px; color: var(--muted);
         display: grid; gap: 3px; }
.tools .ok::before { content: "✓ "; color: var(--pass); }
.tools .missing::before { content: "✗ "; color: var(--fail); }
.attrib { margin-top: 8px; display: grid; gap: 4px; }
.chip { display: flex; gap: 8px; align-items: baseline; font-size: 12.5px;
        background: var(--chipbg); border-radius: 7px; padding: 4px 10px; }
.chip .dim { min-width: 9.5em; color: var(--muted); }
.chip .sc { font-weight: 700; font-variant-numeric: tabular-nums; }
.chip .sc.green { color: var(--pass); } .chip .sc.yellow { color: #c08a2d; }
.chip .sc.red { color: var(--fail); }
footer { margin-top: 40px; color: var(--muted); font-size: 12.5px;
         border-top: 1px solid var(--line); padding-top: 12px; }
"""

_SNIPPET = 360  # 面板文本截断长度（完整原文在密封证据里）


def _snippet(text: str, limit: int = _SNIPPET) -> str:
    """脱敏 + 截断——面板展示片段，原文以密封证据为准。"""
    safe = redact_snippet(text)
    if len(safe) <= limit:
        return safe
    return safe[: limit - 1] + "…"


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


def _metric_chips(outcomes: Sequence[MetricOutcome]) -> str:
    chips = []
    for outcome in outcomes:
        if outcome.score is None:
            chips.append(
                f'<span class="mchip skip">{_e(outcome.name)}：跳过</span>'
            )
        else:
            cls = _score_class(outcome.score)
            color = {"green": "var(--pass)", "yellow": "#c08a2d",
                     "red": "var(--fail)"}[cls]
            chips.append(
                f'<span class="mchip">{_e(outcome.name)}：<b '
                f'style="color:{color}">{outcome.score:.2f}</b></span>'
            )
    return f'<div class="metrics">{"".join(chips)}</div>'


def _detail_chips(details: Sequence[ProbeDetail]) -> str:
    chips = []
    for detail in details:
        cls = _score_class(detail.score)
        chips.append(
            f'<div class="chip"><span class="dim">{_e(detail.metric)}</span>'
            f'<span class="sc {cls}">{detail.score:.2f}</span>'
            f"<span>{_e(detail.reason)}</span></div>"
        )
    return f'<div class="attrib">{"".join(chips)}</div>' if chips else ""


def _turn_card(turn: Turn, score: TurnScore) -> str:
    kind = (
        '<span class="tbadge base">记忆基底（fork 复制上下文）</span>'
        if turn.copied
        else '<span class="tbadge">对话轮</span>'
    )
    marker = (
        f'<span class="tbadge">turn {turn.turn_marker}</span>'
        if turn.turn_marker is not None
        else ""
    )
    facts = (
        f"{'已应答' if score.answered else '未应答'}"
        f" · 回复 {score.reply_chars} 字"
        + (f" · 工具 {score.tool_calls} 次"
           f"（闭环 {score.tool_loop:.0%}）" if score.tool_calls else "")
        + f" · 结构 {score.structure:.2f}"
    )
    head = (
        f'<div class="thead"><span class="tno">T{turn.index}</span>'
        f"{kind}{marker}"
        f'<span class="tbadge">{_e(facts)}</span>'
        f'<span class="tscore {_score_class(score.score)}">'
        f"turn 分 {_score_text(score.score)}</span></div>"
    )
    body = (
        f'<div class="bubble user"><div class="who">用户</div>'
        f"{_e(_snippet(turn.user_text))}</div>"
    )
    for reply in turn.replies:
        body += (
            f'<div class="bubble agent"><div class="who">助手</div>'
            f"{_e(_snippet(reply.text))}</div>"
        )
    if turn.tool_events:
        rows = "".join(
            f'<div class="{"ok" if event.observation_present else "missing"}">'
            f"{_e(event.function_name)}"
            f"({_e(', '.join(sorted(event.arguments)) or '')})"
            f"{' → 有观测' if event.observation_present else ' → 无观测'}</div>"
            for event in turn.tool_events
        )
        body += f'<div class="tools">{rows}</div>'
    body += _detail_chips(score.details)
    cls = "turn copied" if turn.copied else "turn"
    return f'<div class="{cls}">{head}{body}</div>'


def _verdict_badge(verdict: str) -> str:
    colors = {
        "pass": "var(--pass)", "fail": "var(--fail)",
        "cannot_judge": "#c08a2d", "infra_invalid": "#c77832",
        "unfinalized": "var(--muted)",
    }
    zh = {"pass": "通过", "fail": "失败", "cannot_judge": "不可判",
          "infra_invalid": "基础设施无效", "unfinalized": "未终局"}
    return (
        f'<span class="badge" style="background:'
        f'{colors.get(verdict, "var(--muted)")}">'
        f"{zh.get(verdict, verdict)}({verdict})</span>"
    )


def render_trajectory_html(
    *,
    title: str,
    meta_lines: Iterable[str],
    trials: Sequence[TrialView],
) -> str:
    """渲染轨迹分析面板（每个 trial 一节，每轮一张卡）。"""
    sections = []
    for view in trials:
        analysis = view.analysis
        verdict = (
            f'<div style="margin-top:6px">{_verdict_badge(view.verdict)}'
            f'<span class="meta"> · 停止原因 {_e(view.stop_reason)}</span></div>'
        )
        turns_html = "".join(
            _turn_card(turn, score)
            for turn, score in zip(analysis.turns, analysis.scores)
        )
        sections.append(
            f'<section class="trial"><h2>{_e(view.heading)}</h2>'
            f'<div class="meta">{_e(view.meta)}</div>{verdict}'
            f"{_metric_chips(analysis.metric_outcomes)}"
            f'<div class="turns">{turns_html}</div></section>'
        )
    meta = "".join(f'<div class="meta">{_e(line)}</div>' for line in meta_lines)
    return (
        '<!doctype html>\n<html lang="zh-CN">\n<head>\n<meta charset="utf-8">'
        '\n<meta name="viewport" content="width=device-width, initial-scale=1">'
        f"\n<title>{_e(title)}</title>\n<style>{_CSS}{_TURN_CSS}</style>\n"
        "</head>\n<body>\n<div class=\"wrap\">\n"
        f"<header><h1>{_e(title)}</h1>{meta}</header>\n"
        f"{''.join(sections)}\n"
        "<footer>turn 分 = 该轮命中探针/锚点的判分均值（与轨迹级指标同"
        "一条判分路径）；结构分只陈述密封事实（应答/工具闭环）。未命中"
        "任何锚点的轮次记「未评测」，不着色。文本为脱敏截断片段，完整"
        "原文以密封证据（canonical_transcript，sha256 校验）为准。</foot"
        "er>\n</div>\n</body>\n</html>\n"
    )
