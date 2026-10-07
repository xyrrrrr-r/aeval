"""静态可视化面板（markdown 报告之外的第二种渲染面）。

面板与 markdown 报告消费同一份 ``aggregate_run`` 的 ``RunSummary``
（类别聚合也走同一个 ``rollup_categories``）——面板不另算任何数字，
只换呈现；报告里的每个数都能在面板上找到同一个值。

产物是自包含单文件 HTML：内联 CSS/SVG、零外部依赖、零脚本，离线
可开、可归档、可邮寄。输出字节确定（无时间戳）——同输入必得同
字节，与 job 文件 canonical 化的纪律一致。
"""

from __future__ import annotations

from html import escape

from aeval.bundle.attestation import ComparabilityReport
from aeval.metrics.report import (
    CategoryRollup,
    RunSummary,
    _VERDICT_ZH,
    category_pass_pow_k,
    rollup_categories,
)

__all__ = ["render_dashboard_html"]

# 判定色板：契约枚举 → 颜色。顺序即 donut 段序。
_VERDICT_COLORS = {
    "pass": "#2f9e6e",
    "fail": "#e05252",
    "cannot_judge": "#d9a441",
    "infra_invalid": "#c77832",
    "unfinalized": "#9aa0a6",
}

_CSS = """
:root {
  --bg: #f6f7f9; --panel: #ffffff; --ink: #1c2733; --muted: #66707c;
  --line: #e3e6ea; --pass: #2f9e6e; --fail: #e05252; --warn: #b8860b;
  --chipbg: #eef1f4;
}
@media (prefers-color-scheme: dark) {
  :root {
    --bg: #14181d; --panel: #1d232b; --ink: #e8ebef; --muted: #9aa4af;
    --line: #2c343e; --chipbg: #262e38;
  }
}
* { box-sizing: border-box; }
body {
  margin: 0; background: var(--bg); color: var(--ink);
  font: 15px/1.55 -apple-system, "PingFang SC", "Hiragino Sans GB",
        "Microsoft YaHei", "Segoe UI", sans-serif;
}
.wrap { max-width: 1080px; margin: 0 auto; padding: 28px 20px 48px; }
h1 { font-size: 22px; margin: 0 0 4px; }
h2 { font-size: 16px; margin: 34px 0 12px; color: var(--muted);
     font-weight: 600; letter-spacing: .04em; }
.meta { color: var(--muted); font-size: 13px; }
.cards { display: grid; grid-template-columns: repeat(auto-fit,
         minmax(150px, 1fr)); gap: 12px; margin-top: 20px; }
.card { background: var(--panel); border: 1px solid var(--line);
        border-radius: 10px; padding: 14px 16px; }
.card .v { font-size: 24px; font-weight: 700; margin-top: 2px; }
.card .v small { font-size: 13px; font-weight: 400; color: var(--muted); }
.card .l { font-size: 12px; color: var(--muted); }
.banner { border-radius: 10px; padding: 10px 14px; margin-top: 16px;
          font-size: 14px; border: 1px solid; }
.banner.warn { background: color-mix(in srgb, var(--fail) 10%, transparent);
               border-color: var(--fail); color: var(--fail); }
.split { display: grid; grid-template-columns: 300px 1fr; gap: 12px;
         margin-top: 12px; }
@media (max-width: 720px) { .split { grid-template-columns: 1fr; } }
.panel { background: var(--panel); border: 1px solid var(--line);
         border-radius: 10px; padding: 16px; }
.legend { display: grid; gap: 6px; font-size: 13px; margin-top: 10px; }
.legend .row { display: flex; justify-content: space-between; gap: 8px; }
.dot { display: inline-block; width: 10px; height: 10px; border-radius: 3px;
       margin-right: 6px; vertical-align: -1px; }
.catrows { display: grid; gap: 10px; }
.catrow { display: grid; grid-template-columns: 200px 1fr 90px;
          gap: 12px; align-items: center; }
@media (max-width: 720px) { .catrow { grid-template-columns: 1fr; } }
.catrow .name { font-size: 14px; }
.catrow .name small { color: var(--muted); }
.bar { height: 14px; border-radius: 7px; overflow: hidden;
       background: var(--chipbg); display: flex; }
.bar .p { background: var(--pass); height: 100%; }
.bar .f { background: var(--fail); height: 100%; }
.catrow .pk { text-align: right; font-variant-numeric: tabular-nums;
              font-size: 13px; color: var(--muted); }
.chip { background: var(--chipbg); border-radius: 20px; padding: 2px 10px;
        font-size: 12px; color: var(--muted); }
details { background: var(--panel); border: 1px solid var(--line);
          border-radius: 10px; margin-top: 10px; }
summary { cursor: pointer; padding: 12px 16px; font-weight: 600;
          font-size: 14px; list-style: none; }
summary::before { content: "▸ "; color: var(--muted); }
details[open] summary::before { content: "▾ "; }
summary .cnt { color: var(--muted); font-weight: 400; font-size: 13px; }
table { border-collapse: collapse; width: 100%; font-size: 13.5px; }
th, td { padding: 7px 14px; text-align: right; }
th { color: var(--muted); font-weight: 500; font-size: 12.5px;
     border-top: 1px solid var(--line); }
td:first-child, th:first-child { text-align: left; }
tr:last-child td { border-bottom: none; }
td { border-bottom: 1px solid var(--line); }
td.tbar { min-width: 120px; }
.mbar { height: 8px; border-radius: 4px; background: var(--chipbg);
        overflow: hidden; display: flex; }
footer { margin-top: 40px; color: var(--muted); font-size: 12.5px;
         border-top: 1px solid var(--line); padding-top: 12px; }
"""


def _e(value: object) -> str:
    return escape(str(value), quote=True)


def _fmt(value: float | None, digits: int = 4) -> str:
    return "—" if value is None else f"{value:.{digits}f}"


def _pct(value: float) -> str:
    return f"{value:.1%}"


def _donut_svg(counts: dict[str, int]) -> str:
    """判定分布 donut：pathLength=100 的圆段叠加，从 12 点顺时针。"""
    total = sum(counts.values())
    if total == 0:
        return (
            '<svg viewBox="0 0 140 140" width="180" height="180" role="img" '
            'aria-label="无试次"><circle cx="70" cy="70" r="52" fill="none" '
            'stroke="var(--chipbg)" stroke-width="16"/></svg>'
        )
    segments = []
    cumulative = 0.0
    for verdict in ("pass", "fail", "cannot_judge", "infra_invalid", "unfinalized"):
        count = counts.get(verdict, 0)
        if not count:
            continue
        fraction = count / total * 100.0
        segments.append(
            f'<circle cx="70" cy="70" r="52" fill="none" '
            f'stroke="{_VERDICT_COLORS[verdict]}" stroke-width="16" '
            f'pathLength="100" stroke-dasharray="{fraction:.3f} '
            f'{100.0 - fraction:.3f}" stroke-dashoffset="{-cumulative:.3f}" '
            f'transform="rotate(-90 70 70)"/>'
        )
        cumulative += fraction
    return (
        '<svg viewBox="0 0 140 140" width="180" height="180" role="img" '
        'aria-label="判定分布">' + "".join(segments) +
        f'<text x="70" y="66" text-anchor="middle" font-size="26" '
        f'fill="var(--ink)">{total}</text>'
        '<text x="70" y="86" text-anchor="middle" font-size="11" '
        'fill="var(--muted)">试次</text></svg>'
    )


def _bar(passes: int, fails: int, valid: int) -> str:
    """堆叠横条：绿 = 通过占比，红 = 失败占比（占有效试次）。"""
    if valid <= 0:
        return '<div class="bar" title="无有效试次"></div>'
    p = passes / valid * 100.0
    f = fails / valid * 100.0
    return (
        f'<div class="bar"><div class="p" style="width:{p:.2f}%" '
        f'title="通过 {passes}/{valid}"></div>'
        f'<div class="f" style="width:{f:.2f}%" '
        f'title="失败 {fails}/{valid}"></div></div>'
    )


def _metric_cards(summary: RunSummary) -> str:
    cards = [
        (
            "试次",
            f'{summary.total_trials} <small>有效分母 '
            f"{summary.valid_trials}</small>",
        ),
        ("通过 · 失败", f"{summary.passes} · {summary.fails}"),
        (f"pass@{summary.k}" if summary.k is not None else "pass@k",
         _fmt(summary.pass_at_k)),
        (f"pass^{summary.k}" if summary.k is not None else "pass^k",
         _fmt(summary.pass_pow_k_value)),
    ]
    if summary.exclusion is not None:
        cards.append(("排除率", _pct(summary.exclusion.exclusion_rate)))
    cards.append(
        ("单次通过成本",
         "不可用" if summary.cost_per_pass is None
         else f"{summary.cost_per_pass:.1f} tokens")
    )
    # 值侧全是格式化数字/固定文案（无套件派生数据，无标记注入面），
    # 只有标签需要转义；试次卡的内联 <small> 因此保持标记。
    cells = "".join(
        f'<div class="card"><div class="l">{_e(label)}</div>'
        f'<div class="v">{value}</div></div>'
        for label, value in cards
    )
    return f'<section class="cards">{cells}</section>'


def _verdict_panel(summary: RunSummary) -> str:
    counts = summary.verdict_counts
    total = sum(counts.values())
    rows = "".join(
        f'<div class="row"><span><span class="dot" '
        f'style="background:{_VERDICT_COLORS[verdict]}"></span>'
        f"{_e(_VERDICT_ZH.get(verdict, verdict))}({verdict})</span>"
        f"<span>{count}</span></div>"
        for verdict, count in sorted(counts.items())
    )
    return (
        '<div class="panel"><h2 style="margin-top:0">判定分布</h2>'
        f"{_donut_svg(counts)}<div class=\"legend\">{rows}</div></div>"
    )


def _exclusion_panel(summary: RunSummary) -> str:
    if summary.exclusion is None:
        return ""
    ex = summary.exclusion
    items = "".join(
        f'<div class="row"><span>{_e(key)}</span><span>{count}</span></div>'
        for key, count in sorted(ex.excluded.items())
    )
    rows = items or '<div class="row"><span>无排除</span><span>0</span></div>'
    return (
        '<div class="panel"><h2 style="margin-top:0">排除明细</h2>'
        f'<div class="legend">{rows}<div class="row">'
        f"<span>排除率</span><span>{_pct(ex.exclusion_rate)}</span></div>"
        f'<div class="row"><span>有效分母</span>'
        f"<span>{summary.valid_trials} / {summary.total_trials}</span>"
        "</div></div></div>"
    )


def _category_section(summary: RunSummary, rollup: list[CategoryRollup]) -> str:
    rows = []
    for cat in rollup:
        pk = category_pass_pow_k(cat, summary.k)
        rows.append(
            f'<div class="catrow"><div class="name">'
            f"{_e(cat.display)}<small>({_e(cat.key)}) · "
            f"{cat.tasks} 任务</small></div>"
            f"{_bar(cat.passes, cat.fails, cat.valid)}"
            f'<div class="pk">pass^k {_fmt(pk)} · '
            f"{cat.passes}/{cat.valid}</div></div>"
        )
    return (
        "<h2>按类别结果</h2>"
        f'<div class="catrows">{"".join(rows)}</div>'
    )


def _task_table(summary: RunSummary, task_ids) -> str:
    head = (
        "<tr><th>任务</th><th>试次</th><th>有效</th><th>通过</th>"
        "<th>失败</th><th>pass^k</th><th></th></tr>"
    )
    body = []
    for task_id in task_ids:
        group = summary.task_groups[task_id]
        label = (
            f"{summary.task_titles[task_id]}({task_id})"
            if task_id in summary.task_titles
            else task_id
        )
        pk = _fmt(group.pass_pow_k_value)
        body.append(
            f"<tr><td>{_e(label)}</td><td>{group.total_trials}</td>"
            f"<td>{group.valid_trials}</td>"
            f'<td style="color:var(--pass)">{group.passes}</td>'
            f'<td style="color:var(--fail)">{group.fails}</td>'
            f"<td>{pk}</td>"
            f'<td class="tbar"><div class="mbar">'
            f'<div class="p" style="width:'
            f"{(group.passes / group.valid_trials * 100.0) if group.valid_trials else 0:.2f}%\"></div>"
            f'<div class="f" style="width:'
            f"{(group.fails / group.valid_trials * 100.0) if group.valid_trials else 0:.2f}%\"></div>"
            "</div></td></tr>"
        )
    return f"<table>{head}{''.join(body)}</table>"


def _task_section(summary: RunSummary, rollup: list[CategoryRollup] | None) -> str:
    # 大套件默认折叠（123 任务的明细不该一屏铺开），小套件默认展开。
    open_attr = " open" if len(summary.task_groups) <= 30 else ""
    if rollup is not None:
        blocks = []
        for cat in rollup:
            blocks.append(
                f"<details{open_attr}><summary>{_e(cat.display)}"
                f'<span class="cnt">({_e(cat.key)}) · {cat.tasks} 任务 · '
                f"通过 {cat.passes}/{cat.valid} · pass^k "
                f"{_fmt(category_pass_pow_k(cat, summary.k))}</span></summary>"
                f"{_task_table(summary, cat.task_ids)}</details>"
            )
    else:
        blocks = [
            f"<details{open_attr}><summary>全部任务"
            f'<span class="cnt">· {len(summary.task_groups)} 个</span>'
            f"</summary>{_task_table(summary, sorted(summary.task_groups))}"
            "</details>"
        ]
    return f"<h2>按任务结果</h2>{''.join(blocks)}"


def render_dashboard_html(
    summary: RunSummary,
    comparison: ComparabilityReport | None = None,
) -> str:
    comparison = comparison or summary.comparability
    meta_bits = [f"运行 {_e(', '.join(summary.run_ids))}"]
    if summary.suite_labels:
        meta_bits.append(f"套件 {_e('、'.join(summary.suite_labels))}")
    if summary.k is not None:
        meta_bits.append(f"k = {summary.k}")

    banners = ""
    if summary.exclusion_rate_flagged:
        banners += (
            '<div class="banner warn">排除率超标：分数不可单独作为可信'
            "信号（详见排除明细）。</div>"
        )
    if comparison is not None and not comparison.comparable:
        banners += (
            '<div class="banner warn">run 之间不可比：'
            f"{_e(comparison.first_difference())}</div>"
        )

    rollup = (
        rollup_categories(summary)
        if summary.task_groups and len(summary.task_groups) > 1
        else None
    )
    category_html = (
        _category_section(summary, rollup) if rollup is not None else ""
    )

    return (
        "<!doctype html>\n<html lang=\"zh-CN\">\n<head>\n<meta charset="
        '"utf-8">\n<meta name="viewport" content="width=device-width, '
        'initial-scale=1">\n<title>aeval 运行面板 · '
        f"{_e(', '.join(summary.run_ids))}</title>\n<style>{_CSS}</style>\n"
        "</head>\n<body>\n<div class=\"wrap\">\n"
        "<header><h1>aeval 运行面板</h1>"
        f'<div class="meta">{" · ".join(meta_bits)}</div></header>\n'
        f"{banners}\n{_metric_cards(summary)}\n"
        f'<div class="split">{_verdict_panel(summary)}'
        f"{_exclusion_panel(summary)}</div>\n"
        f"{category_html}\n{_task_section(summary, rollup)}\n"
        "<footer>每个数字与 markdown 报告同源（同一 RunSummary、同一类别聚"
        "合）；可追溯到密封证据、判分器版本、有效分母与运行时锁，详见 "
        "run_manifest.json。本页自包含、无脚本、离线可开。</footer>\n"
        "</div>\n</body>\n</html>\n"
    )
