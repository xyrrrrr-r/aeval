"""轨迹面板（Trajectory 分析视图）——密封轨迹的逐试次可视化。

版式参照「Trajectory V2 实时分析」：会话信息栏 → 执行图谱 → 工具
调用 → Token 脉冲 → 上下文压力 → 工具失败与重试/时间消耗 → 工具
结果矩阵 → 耗时分布（按工具）→ **逐轮打分（turn 切面）**。前八个
版块消费 ``TrajectoryStats``（采集层）；逐轮打分消费
``TurnAnalysis``（turns 层）——归因分与轨迹级判分同一条路径（指标
的 ``details()``，``evaluate`` 折叠同一路径），结构分只陈述密封事
实；fork 复制上下文轮标记「记忆基底」，不进 live 归因。本模块不
计算任何数字，缺失的数据如实标注不可用原因，不编造。

纪律与 dashboard 相同：自包含单文件 HTML、内联 CSS/SVG、零脚本、
零外部资源、同输入同字节；所有 suite 派生文本转义，正文片段脱敏
截断（完整原文以密封证据为准）。

与实时面板的差异（诚实声明）：本面板是离线静态渲染，没有播放游
标/搜索交互；上下文占用率仅在调用方显式声明窗口大小时绘制（证据
里没有窗口字段），并标注「声明窗口」。
"""

from __future__ import annotations

import html as _html
from dataclasses import dataclass
from typing import Sequence

from aeval.metrics.dashboard import _CSS
from aeval.verdict.trajectory.metrics import MetricOutcome
from aeval.verdict.trajectory.quality import ProbeDetail
from aeval.verdict.trajectory.stats import TrajectoryStats
from aeval.verdict.trajectory.turns import Turn, TurnAnalysis, TurnScore

__all__ = ["TrialPanel", "render_trajectory_html"]

_VERDICT_ZH = {
    "pass": "通过(pass)",
    "fail": "失败(fail)",
    "cannot_judge": "不可判(cannot_judge)",
}

_W = 760          # svg viewBox 宽
_PAD = 46         # 左右留白（轴标签）


@dataclass(frozen=True)
class TrialPanel:
    """一个试次的面板输入：标题 + 元信息 + 采集结果 + turn 切面。

    ``turn_analysis`` 为 None（默认）时不渲染逐轮打分——纯 stats 面
    板向后兼容；有值但 ``metric_outcomes`` 为空表示套件判分器未声
    明 turn 指标，退化为仅结构切面（如实标注，不编造归因分）。
    """

    heading: str
    meta: str
    stats: TrajectoryStats
    turn_analysis: TurnAnalysis | None = None


def _e(text: object) -> str:
    return _html.escape(str(text), quote=True)


def _int(value: int | None) -> str:
    return f"{value:,}" if value is not None else "—"


def _secs(value: float | None) -> str:
    return f"{value:.1f}s" if value is not None else "—"


def _mmss(seconds: float) -> str:
    seconds = max(0, int(seconds))
    return f"{seconds // 60}:{seconds % 60:02d}"


def _pct(part: int, whole: int) -> str:
    return f"{part / whole:.0%}" if whole else "—"


def _note(text: str) -> str:
    return f'<p class="note">不可用：{_e(text)}</p>'


# --- SVG 图表（纯字符串拼接，确定性） ----------------------------------


def _timeline_svg(stats: TrajectoryStats) -> str | None:
    """执行图谱：步骤节点沿时间轴（无时间戳时退化为等距步序）。"""
    steps = stats.steps
    if not steps:
        return None
    h = 150
    mid = 78
    plot_w = _W - 2 * _PAD
    span = None
    if all(s.seconds_from_start is not None for s in steps):
        span = max(s.seconds_from_start or 0.0 for s in steps)

    def x_of(i: int, s) -> float:
        if span is not None and s.seconds_from_start is not None:
            return _PAD + (s.seconds_from_start / span if span else 0.0) * plot_w
        return _PAD + (i / (len(steps) - 1) if len(steps) > 1 else 0.5) * plot_w

    out = [
        f'<svg viewBox="0 0 {_W} {h}" role="img" '
        f'aria-label="执行图谱">',
        f'<line x1="{_PAD}" y1="{mid}" x2="{_W - _PAD}" y2="{mid}" '
        'class="axis"/>',
    ]
    # 记忆基底区段（fork 复制上下文的前缀步骤）。
    copied_xs = [x_of(i, s) for i, s in enumerate(steps) if s.copied]
    if copied_xs:
        x0, x1 = min(copied_xs), max(copied_xs)
        out.append(
            f'<rect x="{x0 - 6:.1f}" y="{mid - 26}" width="{x1 - x0 + 12:.1f}" '
            f'height="52" class="copiedband"/>'
            f'<text x="{(x0 + x1) / 2:.1f}" y="{mid - 32}" class="bandlabel" '
            f'text-anchor="middle">记忆基底</text>'
        )
    # 轴刻度：约 5 个（时间或步序），去重避免小步数时的重复刻度。
    tick_indices = sorted(
        {
            round(t * (len(steps) - 1) / 4) if len(steps) > 1 else 0
            for t in range(5)
        }
    )
    for i in tick_indices:
        x = x_of(i, steps[i])
        label = (
            _mmss(steps[i].seconds_from_start or 0.0)
            if span is not None and steps[i].seconds_from_start is not None
            else f"#{steps[i].step_id}"
        )
        out.append(
            f'<line x1="{x:.1f}" y1="{mid - 4}" x2="{x:.1f}" y2="{mid + 4}" '
            f'class="tick"/><text x="{x:.1f}" y="{h - 8}" class="ticklabel" '
            f'text-anchor="middle">{_e(label)}</text>'
        )
    # 节点：user 方块在轴上方，agent 圆点在轴上；失败红环；工具名标注。
    for i, s in enumerate(steps):
        x = x_of(i, s)
        cls = "node"
        if s.copied:
            cls += " copied"
        if s.observation_missing:
            cls += " failed"
        tip = (
            f"step {s.step_id} · {s.source}"
            + ("（记忆基底）" if s.copied else "")
            + (f" · 工具 {'、'.join(s.tool_names)}" if s.tool_names else "")
            + (
                f" · 输入 {s.prompt_tokens} / 输出 {s.completion_tokens}"
                if s.prompt_tokens is not None
                else ""
            )
            + (
                f" · 耗时 {s.duration_seconds:.1f}s"
                if s.duration_seconds is not None
                else ""
            )
            + (" · 无观测（失败）" if s.observation_missing else "")
        )
        if s.source == "user":
            out.append(
                f'<rect x="{x - 4.5:.1f}" y="{mid - 22}" width="9" height="9" '
                f'class="{cls} user"><title>{_e(tip)}</title></rect>'
            )
        else:
            out.append(
                f'<circle cx="{x:.1f}" cy="{mid}" r="5" class="{cls}">'
                f"<title>{_e(tip)}</title></circle>"
            )
        if s.tool_names:
            out.append(
                f'<text x="{x:.1f}" y="{mid + 24}" class="toollabel" '
                f'text-anchor="middle">{_e(s.tool_names[0])}</text>'
            )
    out.append("</svg>")
    return "".join(out)


def _bars_svg(rows: Sequence[tuple[str, float, str]], color: str) -> str | None:
    """通用横向条形：(标签, 值, 右侧文本)。"""
    rows = [r for r in rows if r[1] > 0]
    if not rows:
        return None
    row_h = 26
    h = row_h * len(rows) + 8
    plot_w = _W - 2 * _PAD - 150
    vmax = max(r[1] for r in rows)
    out = [f'<svg viewBox="0 0 {_W} {h}" role="img" aria-label="条形图">']
    for i, (label, value, text) in enumerate(rows):
        y = 6 + i * row_h
        w = (value / vmax) * plot_w if vmax else 0
        out.append(
            f'<text x="{_PAD - 6}" y="{y + 14}" class="barlabel" '
            f'text-anchor="end">{_e(label)}</text>'
            f'<rect x="{_PAD}" y="{y + 3}" width="{w:.1f}" height="14" '
            f'rx="3" fill="{color}"/>'
            f'<text x="{_PAD + w + 8:.1f}" y="{y + 14}" class="barvalue">'
            f"{_e(text)}</text>"
        )
    out.append("</svg>")
    return "".join(out)


def _pulse_svg(stats: TrajectoryStats) -> str | None:
    """Token 脉冲：每步堆叠（缓存输入/未缓存输入/输出）。"""
    points = [
        s for s in stats.steps
        if s.prompt_tokens is not None or s.completion_tokens is not None
    ]
    if not points:
        return None
    h = 170
    plot_w = _W - 2 * _PAD
    plot_h = h - 52
    vmax = max(
        (s.prompt_tokens or 0) + (s.completion_tokens or 0) for s in points
    )
    if vmax <= 0:
        return None
    n = len(points)
    bw = max(2.0, min(28.0, plot_w / n - 3))
    out = [
        f'<svg viewBox="0 0 {_W} {h}" role="img" aria-label="Token 脉冲">',
        f'<line x1="{_PAD}" y1="{h - 30}" x2="{_W - _PAD}" y2="{h - 30}" '
        'class="axis"/>',
    ]
    for i, s in enumerate(points):
        x = _PAD + (i + 0.5) * plot_w / n - bw / 2
        prompt = s.prompt_tokens or 0
        cached = min(s.cached_tokens or 0, prompt)
        uncached = prompt - cached
        completion = s.completion_tokens or 0
        y = h - 30
        for value, color in (
            (cached, "#93c5fd"),
            (uncached, "#2563eb"),
            (completion, "#f59e0b"),
        ):
            if value <= 0:
                continue
            bh = value / vmax * plot_h
            y -= bh
            out.append(
                f'<rect x="{x:.1f}" y="{y:.1f}" width="{bw:.1f}" '
                f'height="{bh:.1f}" fill="{color}">'
                f"<title>step {s.step_id} · 缓存 {cached:,} / 未缓存 "
                f"{uncached:,} / 输出 {completion:,}</title></rect>"
            )
        out.append(
            f'<text x="{x + bw / 2:.1f}" y="{h - 14}" class="ticklabel" '
            f'text-anchor="middle">{s.step_id}</text>'
        )
    out.append(
        f'<text x="{_PAD}" y="14" class="bandlabel">峰值 {_int(vmax)} '
        "tokens/步</text>"
    )
    out.append("</svg>")
    return "".join(out)


def _context_svg(stats: TrajectoryStats, window: int | None) -> str | None:
    """上下文压力：输入 Token 走势 + 峰值；声明窗口时加占用率阈值线。"""
    points = [
        (i, s) for i, s in enumerate(stats.steps) if s.prompt_tokens is not None
    ]
    if not points:
        return None
    h = 190
    plot_w = _W - 2 * _PAD
    plot_h = h - 58
    peak = max(s.prompt_tokens or 0 for _, s in points)
    ymax = max(window or 0, peak) * 1.08 or 1
    n_steps = max(1, len(stats.steps) - 1)

    def x_of(i: int) -> float:
        return _PAD + (i / n_steps) * plot_w

    def y_of(v: float) -> float:
        return h - 34 - (v / ymax) * plot_h

    out = [
        f'<svg viewBox="0 0 {_W} {h}" role="img" aria-label="上下文压力">',
        f'<line x1="{_PAD}" y1="{h - 34}" x2="{_W - _PAD}" y2="{h - 34}" '
        'class="axis"/>',
    ]
    if window:
        for ratio, label in ((0.7, "70% 阈值"), (0.9, "90% 阈值")):
            y = y_of(window * ratio)
            out.append(
                f'<line x1="{_PAD}" y1="{y:.1f}" x2="{_W - _PAD}" '
                f'y2="{y:.1f}" class="threshold"/>'
                f'<text x="{_W - _PAD + 4}" y="{y + 4:.1f}" '
                f'class="ticklabel">{_e(label)}</text>'
            )
    coords = " ".join(
        f"{x_of(i):.1f},{y_of(s.prompt_tokens or 0):.1f}" for i, s in points
    )
    out.append(f'<polyline points="{coords}" class="ctxline"/>')
    for i, s in points:
        if s.prompt_tokens == peak:
            out.append(
                f'<circle cx="{x_of(i):.1f}" cy="{y_of(peak):.1f}" r="4" '
                f'class="peakdot"><title>step {s.step_id} · 峰值 '
                f"{peak:,} tokens</title></circle>"
                f'<text x="{x_of(i):.1f}" y="{y_of(peak) - 10:.1f}" '
                f'class="bandlabel" text-anchor="middle">峰值 '
                f"{_e(_int(peak))}</text>"
            )
            break
    out.append(
        f'<text x="{_PAD}" y="14" class="bandlabel">最大输入 '
        f"{_e(_int(peak))} tokens"
        + (
            f" · 声明窗口 {_e(_int(window))}（占用率 {_e(_pct(peak, window or 0))}）"
            if window
            else " · 证据未声明窗口——纵轴为绝对输入 Token"
        )
        + "</text>"
    )
    out.append("</svg>")
    return "".join(out)


# --- 版块 -----------------------------------------------------------------

_PANEL_CSS = """
.trial{border:1px solid #d7dce3;border-radius:10px;padding:14px 16px;
margin:18px 0;background:#fff}
.trial h2{margin:0 0 2px;font-size:17px}
.trial .meta{color:#5b6470;font-size:12px;margin-bottom:10px}
.facts{display:flex;flex-wrap:wrap;gap:6px;margin:8px 0 4px}
.fact{background:#f2f5f9;border:1px solid #dfe5ec;border-radius:999px;
padding:3px 10px;font-size:12px;color:#26313f}
.fact b{color:#0f172a}
.trial h3{margin:16px 0 6px;font-size:13px;color:#334155;
border-left:3px solid #2563eb;padding-left:8px}
.note{color:#8a5a00;background:#fff8e6;border:1px solid #f0dca0;
border-radius:8px;padding:6px 10px;font-size:12px;margin:6px 0}
.legend{font-size:12px;color:#5b6470;margin:2px 0 6px}
.sw{display:inline-block;width:10px;height:10px;border-radius:2px;
margin:0 4px 0 10px;vertical-align:-1px}
svg{width:100%;height:auto;display:block}
.axis{stroke:#c3cad4;stroke-width:1}
.tick{stroke:#c3cad4;stroke-width:1}
.ticklabel{font-size:10px;fill:#5b6470}
.barlabel{font-size:11px;fill:#26313f}
.barvalue{font-size:11px;fill:#5b6470}
.bandlabel{font-size:11px;fill:#5b6470}
.copiedband{fill:#f1f5f9;stroke:#cbd5e1;stroke-dasharray:4 3}
.node{fill:#16a34a;stroke:#fff;stroke-width:1.5}
.node.user{fill:#2563eb}
.node.copied{fill:#9ca3af}
.node.failed{fill:#dc2626;stroke:#7f1d1d;stroke-width:2}
.toollabel{font-size:10px;fill:#7c3aed}
.threshold{stroke:#dc2626;stroke-width:1;stroke-dasharray:5 4}
.ctxline{fill:none;stroke:#16a34a;stroke-width:2}
.peakdot{fill:#dc2626}
table.matrix{border-collapse:collapse;width:100%;font-size:12px}
table.matrix th,table.matrix td{border:1px solid #dfe5ec;padding:4px 8px;
text-align:right}
table.matrix th{background:#f2f5f9;color:#334155}
table.matrix td:first-child,table.matrix th:first-child{text-align:left}
.badge{display:inline-block;border-radius:999px;padding:2px 10px;
font-size:12px;font-weight:600}
.badge.pass{background:#dcfce7;color:#166534}
.badge.fail{background:#fee2e2;color:#991b1b}
.badge.gray{background:#e5e7eb;color:#374151}
.summary{font-size:12.5px;color:#26313f;margin:4px 0}
/* --- 逐轮打分（turn 切面） --- */
.metrics{display:flex;flex-wrap:wrap;gap:6px;margin-top:4px}
.mchip{background:#f2f5f9;border:1px solid #dfe5ec;border-radius:6px;
padding:3px 8px;font-size:12px}
.mchip b{font-variant-numeric:tabular-nums}
.mchip.skip{opacity:.62}
.turns{display:grid;gap:10px;margin-top:12px}
.turn{border:1px solid #dfe5ec;border-radius:10px;padding:12px 14px;
background:#fff}
.turn.copied{background:#f8fafc;border-style:dashed}
.thead{display:flex;align-items:center;gap:10px;flex-wrap:wrap}
.tno{font-weight:700;font-size:14px}
.tbadge{font-size:11.5px;border-radius:12px;padding:2px 9px;
background:#f2f5f9;color:#5b6470}
.tbadge.base{border:1px dashed #9ca3af}
.tscore{margin-left:auto;font-size:15px;font-weight:700;
font-variant-numeric:tabular-nums}
.tscore.green{color:#16a34a}
.tscore.yellow{color:#c08a2d}
.tscore.red{color:#dc2626}
.tscore.gray{color:#5b6470}
.bubble{margin-top:8px;padding:8px 12px;border-radius:9px;
font-size:13.5px;white-space:pre-wrap;word-break:break-word}
.bubble .who{font-size:11.5px;color:#5b6470;margin-bottom:2px}
.bubble.user{background:#f2f5f9}
.bubble.agent{border:1px solid #dfe5ec}
.tools{margin-top:8px;font-size:12.5px;color:#5b6470;display:grid;gap:3px}
.tools .ok::before{content:"✓ ";color:#16a34a}
.tools .missing::before{content:"✗ ";color:#dc2626}
.attrib{margin-top:8px;display:grid;gap:4px}
.chip{display:flex;gap:8px;align-items:baseline;font-size:12.5px;
background:#f2f5f9;border-radius:7px;padding:4px 10px}
.chip .dim{min-width:9.5em;color:#5b6470}
.chip .sc{font-weight:700;font-variant-numeric:tabular-nums}
.chip .sc.green{color:#16a34a}
.chip .sc.yellow{color:#c08a2d}
.chip .sc.red{color:#dc2626}
"""


def _facts_html(stats: TrajectoryStats) -> str:
    badge_cls = {"pass": "pass", "fail": "fail"}.get(stats.verdict or "", "gray")
    badge_zh = _VERDICT_ZH.get(stats.verdict or "", stats.verdict or "未判定")
    facts = [
        f'<span class="fact">判定 <b class="badge {badge_cls}">'
        f"{_e(badge_zh)}</b></span>",
    ]
    if stats.model:
        facts.append(f'<span class="fact">模型 <b>{_e(stats.model)}</b></span>')
    if stats.started_at:
        facts.append(
            f'<span class="fact">开始 <b>{_e(stats.started_at)}</b></span>'
        )
    facts.append(
        f'<span class="fact">总时长 <b>{_e(_secs(stats.wall_clock_seconds))}'
        "</b></span>"
    )
    if stats.total_tokens is not None:
        facts.append(
            f'<span class="fact">总 Token <b>{_e(_int(stats.total_tokens))}'
            "</b></span>"
        )
    if stats.peak_input_tokens is not None:
        facts.append(
            f'<span class="fact">峰值上下文 <b>'
            f"{_e(_int(stats.peak_input_tokens))}</b> tokens</span>"
        )
    facts.append(
        f'<span class="fact">步骤 <b>{stats.total_steps}</b> · 轮次 '
        f"<b>{stats.live_turns}</b> live"
        + (
            f" + <b>{stats.copied_turns}</b> 记忆基底"
            if stats.copied_turns
            else ""
        )
        + "</span>"
    )
    if stats.stop_reason:
        facts.append(
            f'<span class="fact">停止原因 <b>{_e(stats.stop_reason)}</b></span>'
        )
    return '<div class="facts">' + "".join(facts) + "</div>"


def _tool_matrix_html(stats: TrajectoryStats) -> str | None:
    if not stats.tools:
        return None
    rows = [
        "<table class=\"matrix\"><tr><th>工具</th><th>调用</th><th>占比</th>"
        "<th>平均耗时*</th><th>最长*</th><th>重试</th><th>无观测</th>"
        "<th>成功率</th></tr>"
    ]
    total = stats.tool_calls_total or 1
    for t in stats.tools:
        rows.append(
            f"<tr><td>{_e(t.name)}</td><td>{t.calls}</td>"
            f"<td>{_e(_pct(t.calls, total))}</td>"
            f"<td>{_e(_secs(t.avg_seconds))}</td>"
            f"<td>{_e(_secs(t.longest_seconds))}</td>"
            f"<td>{t.retries}</td><td>{t.missing_obs}</td>"
            f"<td>{_e(_pct(t.observed, t.calls))}</td></tr>"
        )
    rows.append("</table>")
    return "".join(rows)


# --- 逐轮打分（turn 切面）-------------------------------------------------

_SNIPPET = 360  # 面板正文截断长度（完整原文在密封证据里）


def _snippet(text: str, limit: int = _SNIPPET) -> str:
    """显示截断——面板直接展示密封原文（HTML 转义），仅超长截断。

    注意与 ``redact_snippet``（quality.py）的分工：那是给**入库**的
    判分理由字符串做防泄漏脱敏的（压成首4字…末2字）；本面板是渲染
    在密封证据旁边的审阅工件，正文必须可读，所以只截断、不脱敏。
    """
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


def _metric_chips(outcomes: Sequence[MetricOutcome]) -> str:
    """轨迹级维度 chips（同对象 evaluate 的折叠结果）。"""
    chips = []
    for outcome in outcomes:
        if outcome.score is None:
            chips.append(
                f'<span class="mchip skip">{_e(outcome.name)}：跳过</span>'
            )
        else:
            color = {
                "green": "#16a34a", "yellow": "#c08a2d", "red": "#dc2626",
            }[_score_class(outcome.score)]
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


def _turn_section_html(analysis: TurnAnalysis) -> str:
    """逐轮打分节：轨迹级 chips + 每轮一张卡。"""
    parts = ["<h3>逐轮打分（turn 切面）</h3>"]
    if analysis.metric_outcomes:
        parts.append(_metric_chips(analysis.metric_outcomes))
    else:
        parts.append(
            '<p class="note">该套件判分器未声明 turn 指标——仅展示结构'
            "切面（应答/工具闭环/回复长度），无归因分。</p>"
        )
    if not analysis.turns:
        parts.append(_note("轨迹无用户面消息，切不出轮次"))
        return "".join(parts)
    parts.append(
        '<div class="turns">'
        + "".join(
            _turn_card(turn, score)
            for turn, score in zip(analysis.turns, analysis.scores)
        )
        + "</div>"
    )
    return "".join(parts)


def _trial_html(panel: TrialPanel, window: int | None) -> str:
    s = panel.stats
    parts = [
        '<section class="trial">',
        f"<h2>{_e(panel.heading)}</h2>",
        f'<div class="meta">{_e(panel.meta)}</div>',
        _facts_html(s),
    ]
    if s.missing:
        parts.append(
            '<p class="note">部分数据不可用：'
            + _e("；".join(s.missing))
            + "</p>"
        )

    parts.append("<h3>执行图谱</h3>")
    timeline = _timeline_svg(s)
    if timeline is None:
        parts.append(_note("轨迹无步骤"))
    else:
        parts.append(
            '<p class="legend"><span class="sw" style="background:#2563eb">'
            '</span>用户<span class="sw" style="background:#16a34a"></span>'
            '智能体<span class="sw" style="background:#dc2626"></span>'
            '工具无观测（失败）<span class="sw" style="background:#9ca3af">'
            "</span>记忆基底（fork 复制上下文）</p>"
        )
        parts.append(timeline)

    parts.append("<h3>工具调用</h3>")
    if s.tools:
        parts.append(
            f'<p class="summary">{s.tool_calls_total} 次调用 · '
            + "、".join(f"{_e(t.name)} {t.calls}" for t in s.tools)
            + "</p>"
        )
        bars = _bars_svg(
            [(t.name, float(t.calls), f"{t.calls} 次") for t in s.tools],
            "#2563eb",
        )
        if bars:
            parts.append(bars)
    else:
        parts.append(_note("该试次无工具调用"))

    parts.append("<h3>Token 脉冲</h3>")
    pulse = _pulse_svg(s)
    if pulse is None:
        parts.append(_note("步骤无 token 计量"))
    else:
        parts.append(
            '<p class="legend"><span class="sw" style="background:#93c5fd">'
            '</span>缓存输入<span class="sw" style="background:#2563eb">'
            '</span>未缓存输入<span class="sw" style="background:#f59e0b">'
            "</span>输出</p>"
        )
        parts.append(pulse)

    parts.append("<h3>上下文压力</h3>")
    ctx = _context_svg(s, window)
    parts.append(ctx if ctx is not None else _note("步骤无输入 token 计量"))

    parts.append("<h3>工具失败与重试 · 时间消耗</h3>")
    fail_rate = (
        f"（{s.tool_missing_obs_total / s.tool_calls_total:.0%}）"
        if s.tool_calls_total
        else ""
    )
    worst = max(s.tools, key=lambda t: t.missing_obs, default=None)
    summary = (
        f'<p class="summary">{s.tool_missing_obs_total} 次无观测{fail_rate}'
        + (
            f" · 失败最多：{_e(worst.name)}（{worst.missing_obs} 次）"
            if worst is not None and worst.missing_obs
            else ""
        )
        + f" · 重试 {s.tool_retries_total} 次"
        + (
            f" · 最长调用 {_e(s.longest_call[0])} "
            f"{_e(_secs(s.longest_call[1]))}"
            if s.longest_call is not None
            else ""
        )
        + (
            f" · 工具步骤耗时占比 {s.tool_time_share:.0%}"
            if s.tool_time_share is not None
            else ""
        )
        + "</p>"
    )
    parts.append(summary)

    parts.append("<h3>工具结果矩阵</h3>")
    matrix = _tool_matrix_html(s)
    if matrix is None:
        parts.append(_note("该试次无工具调用"))
    else:
        parts.append(matrix)
        parts.append(
            '<p class="legend">* 耗时口径 = 含该调用的相邻步骤时间差'
            "（含模型推理），来自密封时间戳；成功率 = 有观测的调用占比"
            "（密封证据无显式错误码，无观测是唯一可陈述的失败信号）。</p>"
        )

    parts.append("<h3>耗时分布（按工具）</h3>")
    dur_rows = [
        (t.name, t.avg_seconds or 0.0, f"平均 {_secs(t.avg_seconds)}")
        for t in s.tools
        if t.avg_seconds is not None
    ]
    dur = _bars_svg(dur_rows, "#7c3aed") if dur_rows else None
    parts.append(
        dur if dur is not None else _note("无时间戳或无工具调用")
    )

    # 逐轮打分（可选层）：CLI 传入了 turn 切面才渲染。
    if panel.turn_analysis is not None:
        parts.append(_turn_section_html(panel.turn_analysis))

    parts.append("</section>")
    return "".join(parts)


def render_trajectory_html(
    *,
    title: str,
    meta_lines: Sequence[str],
    trials: Sequence[TrialPanel],
    context_window: int | None = None,
) -> str:
    """渲染整页轨迹面板（自包含 HTML，同输入同字节）。"""
    head_meta = "".join(f"<li>{_e(m)}</li>" for m in meta_lines)
    body = "".join(_trial_html(p, context_window) for p in trials)
    return (
        "<!doctype html>"
        '<html lang="zh"><head><meta charset="utf-8">'
        f"<title>{_e(title)}</title><style>{_CSS}{_PANEL_CSS}</style>"
        "</head><body>"
        f'<h1>{_e(title)}</h1><ul class="meta">{head_meta}</ul>'
        f"{body}"
        '<footer><p>aeval 轨迹面板 · 数据全部来自 sha256 校验的密封'
        "轨迹证据；缺失处如实标注不可用，不编造。turn 分 = 该轮命中"
        "探针/锚点的判分均值（与轨迹级指标同一条判分路径）；记忆基"
        "底轮（fork 复制上下文）不进 live 归因；未命中锚点的轮次记"
        "「未评测」。对话正文直接取自密封原文（超长截断至 "
        f"{_SNIPPET} 字，HTML 转义）；入库的判分理由字符串另行走"
        "脱敏（redact_snippet），两者口径不同。</p></footer>"
        "</body></html>"
    )
