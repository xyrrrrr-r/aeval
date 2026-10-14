"""静态可视化面板（markdown 报告之外的第二种渲染面）。

面板与 markdown 报告消费同一份 ``aggregate_run`` 的 ``RunSummary``
（族聚合走同一个 ``rollup_families``）——面板不另算任何数字，
只换呈现；报告里的每个数都能在面板上找到同一个值。

产物是自包含单文件 HTML：内联 CSS/SVG、零外部依赖、零脚本，离线
可开、可归档、可邮寄。输出字节确定（无时间戳）——同输入必得同
字节，与 job 文件 canonical 化的纪律一致。
"""

from __future__ import annotations

from html import escape

from aeval.bundle.attestation import ComparabilityReport
from aeval.metrics.report import (
    RunSummary,
    _VERDICT_ZH,
    baseline_rows_to_render,
    family_pass_pow_k,
    guard_metric_rows,
    guard_role,
    metric_detail_absence_note,
    metric_display,
    task_metric_split,
)
from aeval.metrics.watermark import Watermark, score_watermark

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
.src-reason { font-size: 12px; color: var(--fail); margin: 2px 0;
              line-height: 1.4; max-width: 380px; }
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
table.guard-metrics { margin-top: 6px; }
table.guard-metrics td.mid, table.guard-metrics th.mid,
table.guard-metrics td.mnote, table.guard-metrics th.mnote { text-align: left; }
table.guard-metrics td.gname { vertical-align: top; white-space: nowrap; }
table.guard-metrics td.mnote { color: var(--muted); font-size: 12.5px; }
table.guard-metrics small { display: block; color: var(--muted);
                            font-weight: 400; }
td { border-bottom: 1px solid var(--line); }
td.tbar { min-width: 120px; }
.mbar { height: 8px; border-radius: 4px; background: var(--chipbg);
        overflow: hidden; display: flex; }
/* —— 能力水位 —— */
.health { display: flex; flex-wrap: wrap; gap: 28px; align-items: center;
          background: var(--panel); border: 1px solid var(--line);
          border-radius: 10px; padding: 18px 22px; margin-top: 16px; }
.health .big { font-size: 44px; font-weight: 800; line-height: 1; }
.health .sub { font-size: 12px; color: var(--muted); margin-top: 4px; }
.health .stat { font-size: 22px; font-weight: 700; }
.badge { display: inline-block; border-radius: 20px; padding: 3px 12px;
         font-size: 13px; font-weight: 600; color: #fff; }
.badge.ok { background: var(--pass); }
.badge.alarm { background: var(--fail); }
.wm2 { display: grid; grid-template-columns: 300px 1fr; gap: 12px;
       margin-top: 12px; }
@media (max-width: 720px) { .wm2 { grid-template-columns: 1fr; } }
.heat { display: grid; gap: 8px; margin-top: 4px; }
.heat .row { display: flex; align-items: stretch; gap: 8px; }
.heat .blk { min-width: 110px; font-size: 13px; color: var(--muted);
             display: flex; align-items: center; }
.heat .cell { flex: 1; min-width: 118px; border-radius: 8px; padding: 8px 10px;
              color: #fff; }
.heat .cell .n { font-size: 13px; font-weight: 600; }
.heat .cell .s { font-size: 11.5px; opacity: .92; }
.heat .cell.green { background: #2f9e6e; }
.heat .cell.yellow { background: #c08a2d; }
.heat .cell.red { background: #d05252; }
.heat .cell.gray { background: #7d8792; }
.bottom { display: grid; gap: 8px; margin-top: 4px; }
.bottom .item { display: grid; grid-template-columns: 26px 1fr auto;
                gap: 10px; align-items: center; background: var(--panel);
                border: 1px solid var(--line); border-radius: 8px;
                padding: 8px 12px; }
.bottom .rank { font-size: 15px; font-weight: 700; color: var(--muted); }
.bottom .gap { font-size: 12.5px; color: var(--muted); }
.svgtxt { font-size: 11px; fill: var(--muted); }
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


def _family_section(summary: RunSummary, families) -> str:
    """族行（D7）：族自身 GradeResult 判定聚合；排除列带原因。"""
    rows = []
    for family in families:
        pk = family_pass_pow_k(family, summary.k)
        excluded = ""
        if family.cannot_judge:
            reasons = "；".join(
                reason for reason, _count in (
                    r.split(" ×", 1) for r in family.cannot_judge_reasons
                )
            ) if family.cannot_judge_reasons else "无法判定"
            excluded = (
                f'<div class="pk" style="color:var(--warn, #b8860b)">'
                f"排除 {family.cannot_judge}（{_e(reasons)}）</div>"
            )
        engines = "、".join(family.engines)
        division = ""
        if family.deciding_fails or family.foreign_fails:
            division = (
                f'<div class="pk">判定分工：本族 {family.deciding_fails} · '
                f"他层 {family.foreign_fails}</div>"
            )
        rows.append(
            f'<div class="catrow"><div class="name">'
            f"{_e(family.display)}<small>({_e(family.key)}) · "
            f"{family.tasks} 任务 · 引擎 {_e(engines)}</small></div>"
            f"{_bar(family.passes, family.fails, family.valid)}"
            f'<div class="pk">pass^k {_fmt(pk)} · '
            f"{family.passes}/{family.valid}</div>{division}{excluded}</div>"
        )
    return (
        "<h2>按类别结果（指标族）</h2>"
        f'<div class="catrows">{"".join(rows)}</div>'
        f"{_verdict_source_html(summary)}"
    )


def _verdict_source_html(summary: RunSummary) -> str:
    """终判来源分解：每个失败试次由哪一层判失败（基线/族阈值/守卫 veto）。

    没有这一节，"族行数字与基线相近"就只能被读成"族是基线的复述"。
    """
    sources = summary.verdict_sources
    if not sources:
        return ""
    rows = []
    for source in sources:
        task = f"{source.task_id} #{source.trial_index}"
        title = summary.task_titles.get(source.task_id)
        if title:
            task = f"{title}({source.task_id}) #{source.trial_index}"
        reasons = getattr(source, "reasons", ()) or ()
        reason_html = "".join(
            f'<div class="src-reason">{_e(reason)}</div>' for reason in reasons
        ) or "—"
        rows.append(
            "<tr>"
            f"<td><code>{_e(source.trial_id)}</code></td>"
            f"<td>{_e(task)}</td>"
            f"<td>{_e('、'.join(source.deciders))}</td>"
            f"<td>{_e('、'.join(source.roles))}</td>"
            f"<td>{_e(source.mechanism)}</td>"
            f"<td>{_e(source.note or '—')}</td>"
            f"<td>{reason_html}</td>"
            "</tr>"
        )
    baseline_n = sum(1 for s in sources if "官方基线" in s.roles)
    veto_n = sum(1 for s in sources if s.mechanism == "veto")
    family_n = sum(
        1
        for s in sources
        if s.mechanism != "veto" and any(r.startswith("族 ") for r in s.roles)
    )
    guard_n = sum(
        1 for s in sources if s.mechanism != "veto" and "守卫" in s.roles
    )
    parts = [f"官方基线 {baseline_n}"]
    if family_n:
        parts.append(f"族 {family_n}")
    if guard_n:
        parts.append(f"守卫 {guard_n}")
    if veto_n:
        parts.append(f"veto {veto_n}")
    return (
        "<h2>终判来源分解</h2>"
        '<p class="meta">折叠序：任一 veto fail → fail；否则任一 fail → fail；'
        "否则任一 pass → pass（其余层的 cannot_judge 不影响试次）；"
        "只有没有任何层判出 pass/fail 才是 cannot_judge。"
        "「失败理由」列引用判分器写在证据里的理由，理由文本逐字保留，前缀是"
        "「判分器@版本」(或「判分器@版本 · 指标名」) 来源标签；含指标里报告失败的"
        "理由。判分器没写理由就显示「—」，不推断。</p>"
        '<table><thead><tr><th>失败试次</th><th>任务</th>'
        "<th>决定性判分器</th><th>角色</th><th>机制</th><th>对照</th>"
        "<th>失败理由（逐字）</th>"
        f"</tr></thead><tbody>{''.join(rows)}</tbody></table>"
        f'<p class="meta">失败 {len(sources)} = {" · ".join(parts)}'
        "（层计数：一个试次可被多层判 fail，veto 层优先）</p>"
    )


def _metric_detail_html(detail) -> str:
    """逐项指标：族 → 任务 → 指标（v5 Step 3：用例退场）；基线与守卫明细单列。"""
    if detail is None:
        return ""
    blocks = []
    for block in detail.families:
        if not block.declared:
            # 归组族（metrics: []）没有自己的指标——规则由基线/守卫给。
            # 空表不提（12 个归组族各来一张空表，只会把同一批基线明细
            # 重复十二遍）。
            continue
        engines = "、".join(block.engine_labels) or "—"
        metric_rows = []
        totals: dict[str, list[int]] = {}
        for case in block.tasks:
            for item in case.metrics:
                bucket = totals.setdefault(item.metric_id, [0, 0, 0, 0])
                bucket[0] += item.counts.ok
                bucket[1] += item.counts.degraded
                bucket[2] += item.counts.violated
                bucket[3] += item.counts.skipped
        declared_order = list(block.declared)
        extra = [m for m in sorted(totals) if m not in set(declared_order)]
        for metric_id in declared_order + extra:
            ok, degraded, violated, skipped = totals.get(metric_id, [0, 0, 0, 0])
            skipped_cell = str(skipped) if skipped else "0"
            if skipped:
                skipped_cell += " ⚠"
            if metric_id not in totals:
                # 声明了却没有判定落点：如实显示 0，不静默消失
                row = (
                    f'<tr><td>{_e(metric_display(metric_id))} '
                    f'<small>声明项·本 run 未产出'
                    f"</small></td><td>0</td><td>0</td><td>0</td>"
                    f"<td>0</td></tr>"
                )
            elif metric_id in extra:
                row = (
                    f'<tr><td>{_e(metric_display(metric_id))} '
                    f'<small>未在声明里</small></td>'
                    f"<td>{ok}</td><td>{degraded}</td><td>{violated}</td>"
                    f"<td>{skipped_cell}</td></tr>"
                )
            else:
                row = (
                    f"<tr><td>{_e(metric_display(metric_id))}</td><td>{ok}</td>"
                    f"<td>{degraded}</td><td>{violated}</td>"
                    f"<td>{skipped_cell}</td></tr>"
                )
            metric_rows.append(row)
        case_rows = []
        for case in block.tasks:
            title = f"「{case.title}」" if case.title else ""
            judged, skipped_items = task_metric_split(case, block.declared)
            cells = []
            for item in judged:
                bits = []
                if item.counts.ok:
                    bits.append(
                        f'<span style="color:var(--pass)">ok {item.counts.ok}</span>'
                    )
                if item.counts.degraded:
                    bits.append(
                        f'<span style="color:var(--warn, #b8860b)">'
                        f"降级 {item.counts.degraded}</span>"
                    )
                if item.counts.violated:
                    bits.append(
                        f'<span style="color:var(--fail)">违规 {item.counts.violated}</span>'
                    )
                cells.append(f"{_e(metric_display(item.metric_id))}（{' · '.join(bits)}）")
            judged_text = (
                " · ".join(cells)
                if cells
                else '<span class="meta">无有效判定（全部跳过）</span>'
            )
            skip_cell = (
                f'<span style="color:var(--warn, #b8860b)">{skipped_items}</span>'
                if skipped_items
                else "0"
            )
            case_rows.append(
                f"<tr><td>{_e(case.task_id)} [{_e(case.role)}] "
                f"{_e(title)}</td><td>{case.trials}</td>"
                f"<td>{judged_text}</td><td>{skip_cell}</td></tr>"
            )
        # 声明清单不在这里复列：下面那张表就按声明顺序列同样的 id。
        blocks.append(
            f"<details><summary>{_e(block.display)}"
            f'<span class="cnt">({_e(block.key)}) · 引擎 {_e(engines)} · '
            f"适用任务 {len(block.tasks)} · 声明指标 {len(block.declared)} 项"
            f"</span></summary>"
            '<table><tr><th>指标（按声明顺序）</th><th>判定 ok</th><th>降级</th>'
            "<th>违规</th><th>跳过</th></tr>"
            f"{''.join(metric_rows)}</table>"
            '<table><tr><th>任务</th><th>试次</th><th>本任务判的指标</th>'
            "<th>跳过（项）</th></tr>"
            f"{''.join(case_rows)}</table></details>"
        )
    baseline_guards = detail.baseline_guards
    guard_metrics_present = any(
        guard.metrics for guard in (baseline_guards.guards if baseline_guards else ())
    )
    # 基线表只列与「终判结果」不同值的行；这一节别的什么都没有 ⇒ 整块省掉
    # （同一份事实写两遍，读者只会怀疑哪张才准）。面板的「终判结果」总会渲染，
    # 所以这里的 final_table_rendered 恒为真。
    baseline_rows = (
        baseline_rows_to_render(baseline_guards, final_table_rendered=True)
        if baseline_guards is not None
        else ()
    )
    if (
        baseline_guards is not None
        and not (not baseline_rows and not guard_metrics_present and not blocks)
    ):
        labels = "、".join(baseline_guards.baseline_labels) or "—"
        rows = []
        for row in baseline_rows:
            label = (
                f"{row.title}({row.task_id})" if row.title else row.task_id
            )
            rows.append(
                f"<tr><td>{_e(label)}</td><td>{row.trials}</td>"
                f'<td style="color:var(--pass)">{row.passes}</td>'
                f'<td style="color:var(--fail)">{row.fails}</td>'
                f"<td>{row.cannot_judge}</td></tr>"
            )
        if not rows:
            baseline_note = (
                '<p class="meta">逐任务计数与「终判结果」逐行相同（同任务、同试次、'
                "同通过/失败），故不复列；无法判定 = 有效 − 通过 − 失败。</p>"
                if baseline_guards.tasks
                else ""
            )
        else:
            same = len(baseline_guards.tasks) - len(rows)
            scope = (
                f"只列<b>基线与「终判结果」不同值</b>的 {len(rows)} 行"
                + (f"（其余 {same} 行同值，不复列）" if same else "（全部）")
            )
            baseline_note = (
                f'<p class="meta">下表{scope}：这些行就是被守卫 veto / 族阈值改判的'
                "行（归因见「终判来源分解」）；无法判定 = 有效 − 通过 − 失败。</p>"
            )
        baseline_table = (
            "<table><tr><th>任务</th><th>试次</th><th>通过</th>"
            "<th>失败</th><th>无法判定</th></tr>"
            f"{''.join(rows)}</table>"
            if rows
            else ""
        )
        guard_rows = []
        for guard in baseline_guards.guards:
            role = guard_role(guard, baseline_guards.legacy)
            declared_note = (
                f" · 声明清单 {len(guard.declared)} 项 · 读密封 ATIF 轨迹"
                if guard.declared
                else ""
            )
            label = (
                f"{_e(role)} {_e(guard.grader_label)}"
                f"<small>（{guard.trials} 试{_e(declared_note)}）</small>"
            )
            rows_for_guard = guard_metric_rows(guard)
            if not rows_for_guard:
                guard_rows.append(
                    f'<tr><td class="gname">{label}</td>'
                    f'<td colspan="5" class="meta">无指标输出</td></tr>'
                )
                continue
            for offset, (metric_id, ok, degraded, violated, skipped, note) in enumerate(
                rows_for_guard
            ):
                # 同一判分器只在第一行写名字（rowspan），后面留空——一张表
                # 读完一个判分器的整份清单，而不是几行合在一句里。
                name_cell = (
                    f'<td class="gname" rowspan="{len(rows_for_guard)}">{label}</td>'
                    if offset == 0
                    else ""
                )
                violated_cell = (
                    f'<td style="color:var(--fail)">{violated}</td>'
                    if violated
                    else f"<td>{violated}</td>"
                )
                skipped_cell = (
                    f'<td style="color:var(--warn, #b8860b)">{skipped}</td>'
                    if skipped
                    else f"<td>{skipped}</td>"
                )
                degraded_cell = (
                    f'<td style="color:var(--warn, #b8860b)">{degraded}</td>'
                    if degraded
                    else f"<td>{degraded}</td>"
                )
                guard_rows.append(
                    f"<tr>{name_cell}<td class=\"mid\">{_e(metric_id)}</td>"
                    f"<td>{ok}</td>"
                    f"{degraded_cell}{violated_cell}{skipped_cell}"
                    f'<td class="mnote">{_e(note)}</td></tr>'
                )
        if baseline_guards.legacy:
            title = "基线与轨迹层判分器"
            cnt = "v4 封存模型：声明未记录族/守卫归属"
            note = (
                '<p class="meta">本 run 封存于 v5 指标模型之前'
                "（dimension_model 只有 categories/blocks/redline_tasks，没有 "
                "metric_catalog）。下面按判分层列出 store 里实际产出判定的判分器："
                "轨迹层判分器在今天的模型里对应 classification: gate，但归属以"
                "当时的声明为准，本页不代它断言。</p>"
            )
        else:
            title = "基线与守卫"
            cnt = "官方评分 + 有效性门，无类别行"
            note = ""
        guard_table = (
            '<table class="guard-metrics"><tr><th class="gname">判分器</th>'
            '<th class="mid">指标</th>'
            "<th>判定 ok</th><th>降级</th><th>违规</th><th>跳过</th>"
            '<th class="mnote">跳过原因 / 备注</th></tr>'
            f"{''.join(guard_rows)}</table>"
            if guard_rows
            else ""
        )
        blocks.append(
            f"<details><summary>{title}"
            f'<span class="cnt">{cnt}</span>'
            f"</summary>{note}<p class=\"meta\">官方基线：{_e(labels)}</p>"
            f"{baseline_note}{baseline_table}{guard_table}</details>"
        )
    if not blocks:
        # 没有指标级判定也要把话说清楚：这一节曾经静默消失，读者的第一反应
        # 就是"指标去哪了"（sbench 型 outcome-only 契约）。
        absence = metric_detail_absence_note(detail, final_table_rendered=True)
        if not absence:
            return ""
        body = "".join(f'<p class="meta">{_e(line)}</p>' for line in absence)
        return f"<h2>逐项指标（本 run 无指标级判定）</h2>{body}"
    if detail.baseline_guards is not None and detail.baseline_guards.legacy:
        return f"<h2>逐项指标（按判分层）</h2>{''.join(blocks)}"
    return f"<h2>逐项指标（按族 / 任务）</h2>{''.join(blocks)}"


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


def _task_section(
    summary: RunSummary,
    families=None,
) -> str:
    # 大套件默认折叠（123 任务的明细不该一屏铺开），小套件默认展开。
    open_attr = " open" if len(summary.task_groups) <= 30 else ""
    if families:
        # 族模式：分组键 = 声明该 task 的族集合（多值归并列）。
        blocks = []
        grouped: dict[tuple[str, ...], list[str]] = {}
        for task_id in sorted(summary.task_groups):
            grouped.setdefault(
                summary.task_families.get(task_id) or (), []
            ).append(task_id)
        for keys in sorted(grouped, key=lambda ks: (len(ks), ks)):
            if keys:
                header = "、".join(
                    f"{summary.families[k]['name']}({k})" for k in keys
                )
                ids = "、".join(keys)
            else:
                header = "未入族（基线与守卫）"
                ids = "uncategorized"
            blocks.append(
                f"<details{open_attr}><summary>{_e(header)}"
                f'<span class="cnt">({_e(ids)}) · '
                f"{len(grouped[keys])} 任务</span></summary>"
                f"{_task_table(summary, grouped[keys])}</details>"
            )
    else:
        blocks = [
            f"<details{open_attr}><summary>全部任务"
            f'<span class="cnt">· {len(summary.task_groups)} 个</span>'
            f"</summary>{_task_table(summary, sorted(summary.task_groups))}"
            "</details>"
        ]
    return f"<h2>终判结果</h2>{''.join(blocks)}"


def _health_html(watermark: Watermark) -> str:
    """层 1 整体健康度：综合评分 | 整体通过率 | 红线状态。"""
    if watermark.redline_tripped:
        shown = "、".join(watermark.redline_offenders[:3])
        extra = (
            f" 等 {len(watermark.redline_offenders)} 项"
            if len(watermark.redline_offenders) > 3
            else ""
        )
        redline = (
            f'<span class="badge alarm">红线告警：{shown}{extra}</span>'
        )
    else:
        redline = '<span class="badge ok">红线全通过</span>'
    return (
        '<section class="health">'
        f'<div><div class="big">{_fmt(watermark.composite, 2)}</div>'
        '<div class="sub">综合评分（维度加权均分）</div></div>'
        f'<div><div class="stat">{_fmt(watermark.overall_pass_rate, 4)}</div>'
        '<div class="sub">整体通过率</div></div>'
        f'<div>{redline}<div class="sub">红线状态</div></div>'
        "</section>"
    )


def _radar_svg(watermark: Watermark) -> str:
    """层 2 能力雷达：轴 = 大块，值 = 大块加权均分（0-1）。"""
    blocks = [b for b in watermark.blocks if b.score is not None]
    n = len(blocks)
    cx = cy = 110.0
    r = 78.0
    if n < 3:
        # 轴数不足 3 画不了雷达：退化为大块得分条形列表。
        rows = "".join(
            f'<div class="catrow"><div class="name">{_e(b.display)}'
            f"<small>({_e(b.key)})</small></div>"
            f"{_bar(int(round((b.score or 0) * 100)), int(round((1 - (b.score or 0)) * 100)), 100)}"
            f'<div class="pk">{_fmt(b.score, 2)}</div></div>'
            for b in watermark.blocks
        )
        return f'<div class="catrows">{rows}</div>'
    import math

    def point(fraction: float, i: int) -> tuple[float, float]:
        angle = -math.pi / 2 + i * 2 * math.pi / n
        return cx + r * fraction * math.cos(angle), cy + r * fraction * math.sin(angle)

    def polygon(fraction: float) -> str:
        pts = [point(fraction, i) for i in range(n)]
        return " ".join(f"{x:.1f},{y:.1f}" for x, y in pts)

    grid = "".join(
        f'<polygon points="{polygon(f)}" fill="none" '
        f'stroke="var(--line)" stroke-width="1"/>'
        for f in (0.25, 0.5, 0.75, 1.0)
    )
    axes = "".join(
        f'<line x1="{cx}" y1="{cy}" x2="{point(1.0, i)[0]:.1f}" '
        f'y2="{point(1.0, i)[1]:.1f}" stroke="var(--line)"/>'
        for i in range(n)
    )
    data = " ".join(
        f"{point(min(b.score or 0.0, 1.0), i)[0]:.1f},"
        f"{point(min(b.score or 0.0, 1.0), i)[1]:.1f}"
        for i, b in enumerate(blocks)
    )
    labels = []
    for i, b in enumerate(blocks):
        lx, ly = point(1.18, i)
        cos, sin = lx - cx, ly - cy
        anchor = "middle"
        if cos > 18:
            anchor = "start"
        elif cos < -18:
            anchor = "end"
        dy = 4 if abs(sin) < 18 else (10 if sin > 0 else -2)
        labels.append(
            f'<text x="{lx:.1f}" y="{ly + dy:.1f}" text-anchor="{anchor}" '
            f'class="svgtxt">{_e(b.display)} {_fmt(b.score, 2)}</text>'
        )
    return (
        '<svg viewBox="0 0 220 220" width="260" height="260" role="img" '
        'aria-label="能力雷达（按大块加权均分）">'
        f"{grid}{axes}"
        f'<polygon points="{data}" fill="rgba(47,158,110,.25)" '
        f'stroke="var(--pass)" stroke-width="2"/>'
        f'<circle cx="{cx}" cy="{cy}" r="2.5" fill="var(--pass)"/>'
        f"{''.join(labels)}</svg>"
    )


def _quadrant_svg(watermark: Watermark) -> str:
    """层 2 四象限矩阵：横轴达标度（>1 达标），纵轴权重。"""
    dims = [d for d in watermark.dimensions if d.achievement is not None]
    if not dims:
        return '<div class="meta">无有效维度数据</div>'
    x0, x1, y0, y1 = 54.0, 462.0, 18.0, 252.0
    max_w = max(d.weight for d in dims)
    ach_span = 1.5
    w_span = max_w * 1.2

    def px(ach: float) -> float:
        return x0 + min(ach, ach_span) / ach_span * (x1 - x0)

    def py(w: float) -> float:
        return y1 - w / w_span * (y1 - y0)

    div_x = px(1.0)
    div_y = py(max_w / 2.0)
    quadrant_colors = {
        (True, True): "#2f9e6e",    # 达标 + 高权重：核心优势
        (False, True): "#e05252",   # 未达标 + 高权重：急需关注
        (False, False): "#d9a441",  # 未达标 + 低权重：待改进
        (True, False): "#9aa0a6",   # 达标 + 低权重：健康
    }
    points = []
    for d in dims:
        high = d.weight >= max_w / 2.0
        color = quadrant_colors[(d.achievement >= 1.0, high)]
        failing = (
            f" · 失败任务 {len(d.failing_tasks)} 个"
            if d.failing_tasks
            else ""
        )
        points.append(
            f'<circle cx="{px(d.achievement):.1f}" cy="{py(d.weight):.1f}" '
            f'r="6" fill="{color}" opacity=".85">'
            f"<title>{_e(d.display)}({_e(d.key)}) · 大块 "
            f"{_e(d.block_display)} · 达标度 {d.achievement:.2f} · 权重 "
            f"{d.weight:g} · 通过率 {d.value:.1%}{failing}</title></circle>"
        )
    return (
        '<svg viewBox="0 0 480 280" width="100%" role="img" '
        'aria-label="四象限矩阵（达标度 × 权重）">'
        f'<rect x="{x0}" y="{y0}" width="{x1 - x0}" height="{y1 - y0}" '
        f'fill="none" stroke="var(--line)"/>'
        f'<line x1="{div_x}" y1="{y0}" x2="{div_x}" y2="{y1}" '
        f'stroke="var(--muted)" stroke-dasharray="5 4"/>'
        f'<line x1="{x0}" y1="{div_y}" x2="{x1}" y2="{div_y}" '
        f'stroke="var(--line)" stroke-dasharray="3 4"/>'
        f'<text x="{div_x + 4}" y="{y0 + 12}" class="svgtxt">达标线 1.0</text>'
        f'<text x="{x0}" y="{y1 + 16}" class="svgtxt">达标度 0</text>'
        f'<text x="{x1}" y="{y1 + 16}" text-anchor="end" class="svgtxt">'
        f"达标度 ≥ 1</text>"
        f'<text x="{x0 - 6}" y="{y0 + 8}" text-anchor="end" class="svgtxt">'
        f"权重 {max_w:g}</text>"
        f'<text x="{x1}" y="{y0 + 12}" text-anchor="end" class="svgtxt" '
        f'fill="#2f9e6e">核心优势</text>'
        f'<text x="{x0}" y="{y0 + 12}" class="svgtxt" fill="#e05252">'
        f"急需关注</text>"
        f'<text x="{x0}" y="{y1 - 8}" class="svgtxt" fill="#d9a441">待改进</text>'
        f'<text x="{x1}" y="{y1 - 8}" text-anchor="end" class="svgtxt">'
        f"健康</text>"
        f"{''.join(points)}</svg>"
    )


def _heatmap_html(watermark: Watermark) -> str:
    """层 3 维度热力图：绿 ≥ 阈值、黄 ≥ 阈值×90%、红 <、灰 = 未评测。"""
    rows = []
    for block in watermark.blocks:
        cells = []
        for dim in watermark.dimensions:
            if dim.block != block.key:
                continue
            mark = " · 红线" if dim.redline else ""
            cells.append(
                f'<div class="cell {dim.band}" title="{_e(dim.display)}'
                f"({_e(dim.key)}) · 达标度 "
                f'{_fmt(dim.achievement, 2)}{mark}">'
                f'<div class="n">{_e(dim.display)}{mark}</div>'
                f'<div class="s">通过率 {_fmt(dim.value)} · 阈值 '
                f"{dim.threshold:.0%} · 达标度 {_fmt(dim.achievement, 2)}"
                "</div></div>"
            )
        rows.append(
            f'<div class="row"><div class="blk">{_e(block.display)}'
            f"<small><br>{len(cells)} 维度</small></div>"
            f"{''.join(cells)}</div>"
        )
    return f'<div class="heat">{"".join(rows)}</div>'


def _bottom_html(watermark: Watermark, summary: RunSummary) -> str:
    """层 4 短板摘要：达标度最低的前 K 个维度（无数据不计）。"""

    def task_label(task_id: str) -> str:
        title = summary.task_titles.get(task_id)
        return _e(f"{title}({task_id})") if title else _e(task_id)

    items = []
    for rank, dim in enumerate(watermark.bottom, start=1):
        failing = ""
        if dim.failing_tasks:
            shown = "、".join(task_label(t) for t in dim.failing_tasks[:3])
            extra = (
                f" 等 {len(dim.failing_tasks)} 个"
                if len(dim.failing_tasks) > 3
                else ""
            )
            failing = f"<br>失败任务：{shown}{extra}"
        mark = " · 红线" if dim.redline else ""
        items.append(
            f'<div class="item"><div class="rank">{rank}</div><div>'
            f'<div>{_e(dim.display)}({_e(dim.key)}) · '
            f"{_e(dim.block_display)}{mark}{failing}</div>"
            f'<div class="gap">通过率 {_fmt(dim.value)} / 阈值 '
            f"{dim.threshold:.0%} · 差距 "
            f"{(dim.threshold - (dim.value or 0.0)):.1%}"
            f"</div></div>"
            f'<div class="stat">{_fmt(dim.achievement, 2)}</div></div>'
        )
    return f'<div class="bottom">{"".join(items)}</div>'


def _trajectory_html(traj) -> str:
    """轨迹采集带：密封轨迹事实的紧凑汇总（与 markdown 报告同数字）。"""
    facts = [
        ("证据覆盖", f"{traj.trials_with_evidence}/{traj.trials_total} 试次"),
    ]
    if traj.total_tokens is not None:
        facts.append(("总 Token", f"{traj.total_tokens:,}"))
    if traj.mean_wall_seconds is not None:
        facts.append(("平均时长", f"{traj.mean_wall_seconds:.1f}s"))
    facts.append(("工具调用", str(traj.tool_calls)))
    facts.append(
        (
            "无观测",
            f"{traj.tool_missing_obs}"
            + (
                f"（{traj.tool_missing_obs / traj.tool_calls:.0%}）"
                if traj.tool_calls
                else ""
            ),
        )
    )
    facts.append(("重试", str(traj.tool_retries)))
    if traj.peak_input_tokens is not None:
        facts.append(("峰值上下文", f"{traj.peak_input_tokens:,} tokens"))
    if traj.longest_call is not None:
        facts.append(
            ("最长调用", f"{traj.longest_call[0]} {traj.longest_call[1]:.1f}s")
        )
    if traj.total_prompt_tokens and traj.total_cached_tokens is not None:
        facts.append(
            (
                "缓存占输入",
                f"{traj.total_cached_tokens / traj.total_prompt_tokens:.0%}"
                f"（{traj.total_cached_tokens:,}/{traj.total_prompt_tokens:,}）",
            )
        )
    if traj.first_token_ms_min is not None:
        facts.append(
            (
                "首 token 时延",
                f"最快 {traj.first_token_ms_min:,}ms · 中位 "
                f"{traj.first_token_ms_median:,}ms · 最慢 {traj.first_token_ms_max:,}ms",
            )
        )
    else:
        facts.append(("首 token 时延", "不可用（账本未记录）"))
    chips = "".join(
        f'<div class="card"><div class="l">{_e(label)}</div>'
        f'<div class="v">{_e(value)}</div></div>'
        for label, value in facts
    )
    note = ""
    if traj.unavailable:
        note = (
            '<p class="meta">不可用：'
            + _e("、".join(traj.unavailable))
            + "</p>"
        )
    return (
        '<h2>轨迹采集</h2>\n'
        f'<div class="cards">{chips}</div>\n{note}'
    )


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
    if summary.aggregation is not None and not summary.aggregation.comparable:
        agg = summary.aggregation
        detail = "；".join(
            f"{label} ← {'、'.join(runs)}"
            for label, runs in zip(agg.labels, agg.groups)
        )
        banners += (
            f'<div class="banner warn">聚合 {len(agg.run_ids)} run / '
            f"{len(agg.groups)} 组条件（同套件 {agg.suite_id}）："
            "组内可比才合并同键族，跨组各自成行"
            f"（{_e(detail)}）</div>"
        )
    if comparison is not None and not comparison.comparable:
        banners += (
            '<div class="banner warn">run 之间不可比：'
            f"{_e(comparison.first_difference())}</div>"
        )

    families = summary.family_rollups
    category_html = _family_section(summary, families) if families else ""

    # 能力水位（首屏核心视图）：未声明维度模型的
    # 套件不加这四层（向后兼容）。
    watermark = score_watermark(summary)
    if watermark is not None:
        watermark_html = (
            f"{_health_html(watermark)}\n"
            '<div class="wm2"><div class="panel">'
            '<h2 style="margin-top:0">能力雷达</h2>'
            f"{_radar_svg(watermark)}</div>"
            '<div class="panel">'
            '<h2 style="margin-top:0">四象限矩阵（达标度 × 权重）</h2>'
            '<p class="meta">横轴达标度（&gt;1 达标），纵轴权重；'
            f"悬停查看维度明细。</p>{_quadrant_svg(watermark)}</div></div>\n"
            "<h2>维度热力图</h2>\n"
            f"{_heatmap_html(watermark)}\n"
            f"<h2>短板摘要（达标度最低 {len(watermark.bottom)} 项）</h2>\n"
            f"{_bottom_html(watermark, summary)}\n"
        )
    else:
        watermark_html = ""

    # 轨迹采集带：CLI 采集了密封轨迹数据才渲染（与 markdown 报告的
    # 「轨迹采集」节同源同数字）。
    trajectory_html = (
        _trajectory_html(summary.trajectory)
        if summary.trajectory is not None
        else ""
    )

    # 逐项指标层（族模式）：跑不了的指标带原因可见。
    metric_detail_html = _metric_detail_html(summary.metric_detail)

    return (
        "<!doctype html>\n<html lang=\"zh-CN\">\n<head>\n<meta charset="
        '"utf-8">\n<meta name="viewport" content="width=device-width, '
        'initial-scale=1">\n<title>aeval 运行面板 · '
        f"{_e(', '.join(summary.run_ids))}</title>\n<style>{_CSS}</style>\n"
        "</head>\n<body>\n<div class=\"wrap\">\n"
        "<header><h1>aeval 运行面板</h1>"
        f'<div class="meta">{" · ".join(meta_bits)}</div></header>\n'
        f"{banners}\n{_metric_cards(summary)}\n{trajectory_html}"
        f"{watermark_html}"
        f'<div class="split">{_verdict_panel(summary)}'
        f"{_exclusion_panel(summary)}</div>\n"
        f"{category_html}\n{_task_section(summary, families)}\n"
        f"{metric_detail_html}\n"
        "<footer>每个数字与 markdown 报告同源（同一 RunSummary、同一类别聚"
        "合）；可追溯到密封证据、判分器版本、有效分母与运行时锁，详见 "
        "run_manifest.json。本页自包含、无脚本、离线可开。</footer>\n"
        "</div>\n</body>\n</html>\n"
    )
