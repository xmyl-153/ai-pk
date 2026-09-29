"""报告：HTML（可分享）+ Markdown（可提交）。

原则：**把不确定性写进报告**，而不是只报一个名次。
- 每个成功率都带 Wilson 95% 区间
- 排名带 bootstrap 区间；区间重叠就明说"分不出高下"
- 附权重敏感性：换权重后排名是否翻转
- 附裁判审计：位置翻转率
- 附原始证据路径：任何人可复跑复算
"""
from __future__ import annotations

import html
import json
from datetime import datetime
from pathlib import Path

from .arena import bt_ratings, harness_sensitivity, head_to_head, win_matrix
from .harness import RunResult
from .metrics import (ALT_WEIGHTS, WEIGHTS, aggregate, check_breakdown, composite,
                      family_matrix, judge_audit_from_results, rank_stability,
                      saturation_report)

FAMILY_LABEL = {
    "constraint": "约束排班",
    "toolchain": "工具链跟随",
    "cascade": "级联调试",
    "datatransform": "脏数据转换",
    "compliance": "硬格式合规",
    "multiturn": "需求变更跟随",
    "longcontext": "长文规则应用",
    "writing": "找错改写",
    "repofix": "小仓库调试",
    "longstate": "长程状态维护",
    "premise": "前提辨识",
    "mindiff": "最小 diff",
    "decay": "长会话一致性",
}

# 子检查项的中文标签：这些是"折进 solved 之前"的机制信号，值得单独看
CHECK_LABEL = {
    "format": "格式合规",
    "has_workdir_evidence": "真改了工作目录",
    "tests_pass": "自测通过",
    "held_out_ok": "隐藏用例通过",
    "no_test_tamper": "未动自测文件",
    "diff_minimal": "diff 最小",
    "single_file_scope": "只动一个文件",
    "summary_given": "给了改动说明",
    "claimed_files": "报了改动文件",
    "premise_flag": "前提判定对",
    "answer": "费用算对",
    "not_blind_follow": "没照抄错值",
    "note": "说明有效",
    "probe1_correct": "首答正确",
    "probe2_correct": "末答正确",
    "no_drift": "首末答一致",
    "self_report_true": "自报历史属实",
    "fillers_ok": "中间任务全对",
    "note_given": "给了说明",
    "gave_code": "给了代码",
    "bug_reason_given": "说明了原因",
    "named_function": "点名了函数",
    "output_correct": "输出值正确",
    "output_matches_runtime": "与实跑一致",
}


def _check_label(name: str) -> str:
    return CHECK_LABEL.get(name, name)


def _pct(x: float | None) -> str:
    return "—" if x is None else f"{x * 100:.1f}%"


def _ms(x: int | None) -> str:
    if x is None:
        return "—"
    return f"{x / 1000:.1f}s" if x >= 1000 else f"{x}ms"


def build_report(results: list[RunResult], run_dir: Path, alt_run: Path | None = None) -> list[Path]:
    names = {}
    for r in results:
        names.setdefault(r.model_key, (r.model_key.split("/")[-1], ""))
    stats = aggregate(results, names)
    outcomes = head_to_head(results)
    ratings = bt_ratings(outcomes, seed=7)
    h2h = win_matrix(outcomes)
    fam = family_matrix(stats)
    comp = composite(stats)
    stability = rank_stability(stats)
    sat = saturation_report(stats, fam)
    sens = harness_sensitivity(results)
    chk = check_breakdown(results)
    audit = judge_audit_from_results(results)

    if alt_run and (alt_run / "runs.jsonl").exists():
        from .runner import load_run
        sens = harness_sensitivity(results + load_run(alt_run / "runs.jsonl"))

    order = sorted(stats, key=lambda k: -comp[k])
    fams = sorted({f for row in fam.values() for f in row})
    generated = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    md = _markdown(order, stats, ratings, comp, stability, sens, fam, fams, run_dir, generated,
                   results, h2h, outcomes, sat, chk, audit)
    md_path = run_dir / "REPORT.md"
    md_path.write_text(md, encoding="utf-8")
    html_path = run_dir / "report.html"
    html_path.write_text(_html(order, stats, ratings, comp, stability, sens, fam, fams,
                               run_dir, generated, h2h, outcomes, results, sat, chk, audit),
                         encoding="utf-8")
    (run_dir / "summary.json").write_text(json.dumps({
        "generated": generated,
        "composite": comp,
        "weights": WEIGHTS,
        "alt_weights": ALT_WEIGHTS,
        "ratings": ratings,
        "solve_rate": {k: {"value": st.solve_rate, "ci": st.solve_ci, "n": st.n}
                       for k, st in stats.items()},
        "family_matrix": fam,
        "check_breakdown": chk,
        "judge_audit": audit,
        "harness_sensitivity": sens,
        "rank_stability": stability,
        "saturation": sat,
    }, ensure_ascii=False, indent=2), encoding="utf-8")
    return [md_path, html_path, run_dir / "summary.json"]


# ---------------------------------------------------------------- Markdown


def _markdown(order, stats, ratings, comp, stability, sens, fam, fams, run_dir, generated,
              results, h2h, outcomes, sat, chk=None, audit=None) -> str:
    L: list[str] = []
    n_models = len(stats)
    n_runs = len(results)
    L.append("# AI PK 实测报告")
    L.append("")
    L.append(f"生成时间：{generated}　|　运行目录：`{run_dir}`")
    L.append(f"样本：**{n_models} 个模型 × {n_runs // max(n_models, 1)} 次运行**　|　"
             f"原始证据：`{run_dir / 'runs.jsonl'}`")
    L.append("")
    excl = {k: st.excluded_infra for k, st in stats.items() if st.excluded_infra}
    if excl:
        total_excl = sum(excl.values())
        L.append(f"**已排除 {total_excl} 次基础设施故障运行**（网关限流/网络超时，不是模型的锅）："
                 + "、".join(f"{stats[k].name} {v} 次" for k, v in sorted(excl.items(), key=lambda x: -x[1])))
        L.append("")
        L.append("> 这一条很重要：不限速时网关 429 会把模型打成假 0 分。"
                 "把基础设施故障算成模型答错，就是另一份唬人榜单。")
        L.append("")
    L.append("> **怎么读这份报告**：所有成功率都带 95% 置信区间；"
             "区间重叠的模型**不能**说谁更强。换权重后排名会变的模型，"
             "说明它和邻居其实差不多。这是刻意设计，不是含糊。")
    L.append("")

    # 饱和诊断放在最前面：如果题太简单，正确率排名就是噪声，必须先说清楚
    if sat.get("saturated"):
        L.append("## ⚠ 0. 饱和诊断（先看这条）")
        L.append("")
        L.append(f"**本套任务对当前模型池已经饱和**：成功率极差仅 "
                 f"{sat['solve_rate_spread'] * 100:.1f} 个百分点，"
                 f"所有模型的 95% 置信区间"
                 f"{'两两全部重叠' if sat['all_ci_overlap'] else '高度重叠'} —— "
                 f"**正确率层面分不出高下**。")
        L.append("")
        if sat.get("saturated_families"):
            L.append(f"- 已饱和（所有模型 ≥95%）：{'、'.join(FAMILY_LABEL.get(f, f) for f in sat['saturated_families'])}")
        if sat.get("discriminating_families"):
            L.append(f"- 仍有区分度：{'、'.join(FAMILY_LABEL.get(f, f) for f in sat['discriminating_families'])}")
        L.append("")
        L.append(f"{sat['note']}")
        L.append("")
        L.append("**升级方向**（下一轮该做的）：提高每族实例数与步数上限、"
                 "加长上下文规模、把多族组合成单条长任务链、引入多文件真实仓库操作。")
        L.append("")

    L.append("## 1. 总榜")
    L.append("")
    L.append("| # | 模型 | 综合分 | 任务成功率 (95% CI) | BT 分 (95% CI) | pass^k | 中位首字 | 单任务 token | 排除 |")
    L.append("|---|---|---|---|---|---|---|---|---|")
    for i, k in enumerate(order, 1):
        st = stats[k]
        r = ratings.get(k, {})
        lo, hi = st.solve_ci
        bt = f"{r.get('rating', 0):.0f} ({r.get('lo', 0):.0f}–{r.get('hi', 0):.0f})" if r else "—"
        L.append(f"| {i} | {st.name} | {comp[k]:.3f} | {_pct(st.solve_rate)} "
                 f"({_pct(lo)}–{_pct(hi)}) | {bt} | {_pct(st.pass_k)} | "
                 f"{_ms(st.median_ttft_ms)} | {st.tokens_per_task:.0f}"
                 f"{'≈' if st.usage_estimated else ''} | "
                 f"{st.excluded_infra or '—'} |")
    L.append("")

    unstable = stability["unstable_models"]
    if unstable:
        L.append(f"**⚠ 排名不稳定**：换权重方案后，{len(unstable)} 个模型名次变动 ≥2 位 —— "
                 f"{'、'.join(stats[k].name for k in unstable)}。"
                 f"这几位的相对强弱**不要**当结论用。")
        L.append("")
    L.append("权重方案（可改）：默认 " + json.dumps(WEIGHTS, ensure_ascii=False))
    L.append("")

    L.append("## 2. 分任务族看：谁在哪塌方")
    L.append("")
    L.append("| 模型 | " + " | ".join(FAMILY_LABEL.get(f, f) for f in fams) + " |")
    L.append("|" + "---|" * (len(fams) + 1))
    for k in order:
        cells = []
        for f in fams:
            v = fam[k].get(f)
            cells.append("—" if v != v else f"{v * 100:.0f}%")
        L.append(f"| {stats[k].name} | " + " | ".join(cells) + " |")
    L.append("")

    if chk:
        L.append("### 2.1 族内子检查（机制信号：折进「对/错」之前丢掉的那部分）")
        L.append("")
        L.append("成功率把每道题的内部检查折成了一个布尔值。下面这些是**没有**被折掉的分项 ——"
                 "它们才是正确率饱和之后的信息来源（例如长会话「首答对、末答错」的衰减）。")
        L.append("")
        for f in sorted(chk):
            items = chk[f]
            parts = []
            for name, d in items.items():
                mark = "**" if d["rate"] < 0.9 else ""
                parts.append(f"{mark}{_check_label(name)} {d['rate'] * 100:.0f}%"
                             f"（{d['pass']}/{d['n']}）{mark}")
            L.append(f"- **{FAMILY_LABEL.get(f, f)}**：" + "、".join(parts))
        L.append("")

    L.append("## 3. 体验指标（这些才是「跑分看不出来的东西」）")
    L.append("")
    # 实测最容易说服人的一条：正确率打平，成本差好几倍
    tied = {}
    for k in order:
        tied.setdefault(round(stats[k].solve_rate, 3), []).append(k)
    tie_blocks = [v for v in tied.values() if len(v) >= 2]
    if tie_blocks:
        L.append("### 3.1 正确率打平，成本差几倍 —— 这就是「跑分相同、用起来天差地别」")
        L.append("")
        for blk in tie_blocks:
            by_lat = sorted(blk, key=lambda k: stats[k].p90_total_ms or 0)
            fast, slow = by_lat[0], by_lat[-1]
            p90f, p90s = stats[fast].p90_total_ms or 0, stats[slow].p90_total_ms or 0
            L.append(f"**成功率并列 {stats[fast].solve_rate * 100:.1f}% 的有 {len(blk)} 个**："
                     + "、".join(stats[k].name for k in blk))
            L.append("")
            L.append("| 模型 | P90 总耗时 | 单任务 token | reasoning 占比 | 中位首字 |")
            L.append("|---|---|---|---|---|")
            for k in by_lat:
                s = stats[k]
                L.append(f"| {s.name} | {_ms(s.p90_total_ms)} | {s.tokens_per_task:.0f} "
                         f"{'≈' if s.usage_estimated else ''} | {_pct(s.reasoning_ratio)} | "
                         f"{_ms(s.median_ttft_ms)} |")
            L.append("")
            if p90f and p90s and p90s / p90f >= 1.5:
                L.append(f"- 耗时：最快 {stats[fast].name} {_ms(p90f)} vs 最慢 "
                         f"{stats[slow].name} {_ms(p90s)} —— **差 {p90s / p90f:.1f} 倍**")
            tok_lo = min(blk, key=lambda k: stats[k].tokens_per_task)
            tok_hi = max(blk, key=lambda k: stats[k].tokens_per_task)
            tl, th = stats[tok_lo].tokens_per_task, stats[tok_hi].tokens_per_task
            if tl and th / tl >= 1.5:
                L.append(f"- token：最省 {stats[tok_lo].name} {tl:.0f} vs 最费 "
                         f"{stats[tok_hi].name} {th:.0f} —— **差 {th / tl:.1f} 倍**")
            L.append("")
        L.append("> 只看正确率的榜单会把这一组判成「并列第一」；"
                 "而实际用起来，最快的几秒出结果，最慢的要等几分钟、烧几倍的钱。")
        L.append("")
    L.append("| 模型 | 中位首字 | P90 总耗时 | reasoning 占比 | 工具调用 | 无效调用率 | 故障恢复 | submit 合规 | 一致性(σ↓) |")
    L.append("|---|---|---|---|---|---|---|---|---|")
    for k in order:
        st = stats[k]
        rec = "—" if st.recovery_rate is None else _pct(st.recovery_rate)
        L.append(f"| {st.name} | {_ms(st.median_ttft_ms)} | {_ms(st.p90_total_ms)} | "
                 f"{_pct(st.reasoning_ratio)} | {st.tool_calls} | {_pct(st.wasted_call_rate)} | "
                 f"{rec} | {_pct(st.submit_rate)} | {st.consistency:.2f} |")
    L.append("")
    L.append("> reasoning 占比高 = 简单任务也要想很久，等得烦、烧钱多。"
             "故障恢复 = 工具报错后仍能完成任务的比例。")
    est = {k: st.usage_estimated for k, st in stats.items() if st.usage_estimated}
    if est:
        L.append(">")
        L.append("> `≈` 表示该模型的网关不返回 usage，token 数为字符数估算值（"
                 + "、".join(f"{stats[k].name} {v} 次" for k, v in sorted(est.items(), key=lambda x: -x[1]))
                 + "）。成本类指标对它们只能作参考。")
    deg = {k: st.param_degraded for k, st in stats.items() if st.param_degraded}
    if deg:
        L.append(">")
        L.append("> ⚠ **参数降级**：" + "、".join(
            f"{stats[k].name} {v} 次" for k, v in sorted(deg.items(), key=lambda x: -x[1]))
            + " —— 这些模型的网关拒绝了我们给的采样参数（例如 `temperature` 只接受特定值），"
              "已自动降级重投。**它们的「稳定性/方差」指标不能与本轮其他模型直接比**："
              "它们实际跑在网关默认温度下。")
    L.append("")

    if sens.get("mean_tau") is not None:
        L.append("## 4. Harness 敏感度（换个壳子就变笨吗）")
        L.append("")
        L.append(f"profiles：{', '.join(sens['profiles'])}　|　平均排名一致性 τ = {sens['mean_tau']}")
        L.append("")
        L.append("| 模型 | 排名漂移 | 成功率跨 profile 极差 |")
        L.append("|---|---|---|")
        for k in order:
            d = sens["per_model_drift"].get(k, 0)
            s = sens["per_model_rate_spread"].get(k, 0)
            L.append(f"| {stats[k].name} | {d} 位 | {_pct(s)} |")
        L.append("")

    if audit and audit.get("n"):
        L.append("## 5. 裁判审计（盲评分数能不能信）")
        L.append("")
        L.append(f"裁判判决 **{audit['n']}** 次（无效 {audit.get('invalid', 0)} 次）："
                 f"位置翻转率 **{_pct(audit.get('position_flip_rate'))}**、"
                 f"有效裁决率 {_pct(audit.get('decided_rate'))}、"
                 f"判平率 {_pct(audit.get('tie_rate'))}、"
                 f"无效率 {_pct(audit.get('invalid_rate'))}")
        L.append("")
        L.append("> 位置翻转率 = 同一对答案交换左右位置后结论改变的比例。"
                 "翻转率非 0 说明裁判有位置偏置，它的分只能当参考；"
                 "无效裁决 = 裁判没吐出可解析的 JSON（例如 reasoning 把配额吃光）。")
        L.append("")
        pj = audit.get("per_judge") or {}
        if len(pj) > 1:
            L.append("| 裁判 | 判决数 | 位置翻转率 | 判平率 | **宽严度**（判模型赢） |")
            L.append("|---|---|---|---|---|")
            for jk, d in pj.items():
                tie_r = d.get("tie", 0) / d["n"] if d.get("n") else 0
                L.append(f"| {jk} | {d.get('n', 0)} | {_pct(d.get('flip_rate'))} "
                         f"| {_pct(tie_r)} | {_pct(d.get('model_win_rate'))} |")
            L.append("")
            L.append("> 多裁判跑的时候先看这一栏：**谁在翻**（翻转率）+ **谁宽谁严**（宽严度）。"
                     "实测同一批答案上三个裁判的宽严度能差 33% vs 92% —— "
                     "**换裁判就能换名次**，所以名次只能当弱证据。")
            sp = audit.get("severity_spread")
            if sp is not None and sp >= 0.3:
                L.append("")
                L.append(f"**⚠ 本轮裁判宽严度极差 {sp:.0%}（≥30%）："
                         f"换一个裁判名次就会变，不要写进结论。**")
            L.append("")
        jm = {k: st.judge_mean for k, st in stats.items() if st.judge_mean is not None}
        if jm:
            L.append("| 模型 | 盲评相对分（对参考解，0~1） | 判决数 |")
            L.append("|---|---|---|")
            for k in order:
                if k in jm:
                    pm = (audit.get("per_model") or {}).get(k, {})
                    L.append(f"| {stats[k].name} | {jm[k]:.3f} | {pm.get('n', '—')} |")
            L.append("")
        if audit.get("position_flip_rate"):
            L.append("**⚠ 本轮裁判存在位置偏置，排名里若含盲评权重请谨慎解读。**")
            L.append("")

    L.append("## 6. 配对胜负（谁克谁）")
    L.append("")
    L.append("| 模型 | " + " | ".join(stats[k].name for k in order) + " |")
    L.append("|" + "---|" * (len(order) + 1))
    for a in order:
        row = []
        for b in order:
            if a == b:
                row.append("—")
                continue
            rec = h2h.get(a, {}).get(b, {})
            row.append(f"{rec.get('a', 0)}:{rec.get('b', 0)}" if rec else "—")
        L.append(f"| {stats[a].name} | " + " | ".join(row) + " |")
    L.append("")
    L.append(f"配对总场次：{len(outcomes)}（平局含「两边都失败但连续分相同」）")
    L.append("")

    L.append("## 7. 权威性边界（必读）")
    L.append("")
    L.append("- 本报告只支持这样的结论：**「在 seed/配置/时间如下的这次运行中，系统 Z 的表现是……」**")
    L.append("- 任务分布是人选的，选择即偏见；**不支持**「模型 X 整体比 Y 强」这类说法")
    L.append("- 每个格子样本量有限，请优先看置信区间与 pass^k，而不是名次")
    L.append("- 原始 transcript、工具调用、用量、错误全部落盘，任何人可 `python -m aipk report --run <dir>` 复算")
    L.append("- 裁判（若启用）与选手分离，且**交换位置评两遍**；位置翻转率见 summary.json")
    L.append("")
    return "\n".join(L)


# ---------------------------------------------------------------- HTML


def _html(order, stats, ratings, comp, stability, sens, fam, fams, run_dir, generated,
          h2h, outcomes, results, sat, chk=None, audit=None) -> str:
    def esc(s) -> str:
        return html.escape(str(s))

    def bar(v: float) -> str:
        pct = max(0.0, min(1.0, v)) * 100
        return (f'<div class="bar"><div class="fill" style="width:{pct:.1f}%"></div>'
                f'<span>{pct:.0f}%</span></div>')

    rows = []
    for i, k in enumerate(order, 1):
        st = stats[k]
        lo, hi = st.solve_ci
        r = ratings.get(k, {})
        rows.append(f"""<tr>
<td class="rank">{i}</td>
<td class="name">{esc(st.name)}<div class="key">{esc(k)}</div></td>
<td>{comp[k]:.3f}</td>
<td>{bar(st.solve_rate)}<div class="ci">95% CI {lo * 100:.1f}% – {hi * 100:.1f}%（n={st.n}）</div></td>
<td>{r.get('rating', 0):.0f}<div class="ci">{r.get('lo', 0):.0f} – {r.get('hi', 0):.0f}</div></td>
<td>{st.pass_k * 100:.0f}%</td>
<td>{_ms(st.median_ttft_ms)}</td>
<td>{st.tokens_per_task:.0f}</td>
<td>{st.reasoning_ratio * 100:.0f}%</td>
<td>{'—' if st.recovery_rate is None else f'{st.recovery_rate * 100:.0f}%'}</td>
</tr>""")

    fam_head = "".join(f"<th>{esc(FAMILY_LABEL.get(f, f))}</th>" for f in fams)
    fam_rows = []
    for k in order:
        cells = []
        for f in fams:
            v = fam[k].get(f)
            if v != v:
                cells.append("<td class='na'>—</td>")
            else:
                cls = "good" if v >= 0.8 else ("mid" if v >= 0.4 else "bad")
                cells.append(f"<td class='{cls}'>{v * 100:.0f}%</td>")
        fam_rows.append(f"<tr><td class='name'>{esc(stats[k].name)}</td>{''.join(cells)}</tr>")

    h2h_head = "".join(f"<th>{esc(stats[k].name)}</th>" for k in order)
    h2h_rows = []
    for a in order:
        cells = []
        for b in order:
            if a == b:
                cells.append("<td class='na'>—</td>")
                continue
            rec = h2h.get(a, {}).get(b, {})
            cells.append(f"<td>{rec.get('a', 0)}:{rec.get('b', 0)}</td>" if rec else "<td class='na'>—</td>")
        h2h_rows.append(f"<tr><td class='name'>{esc(stats[a].name)}</td>{''.join(cells)}</tr>")

    unstable = stability["unstable_models"]
    warn = ""
    if unstable:
        warn = (f"<div class='warn'>⚠ 排名不稳定：换权重方案后 "
                f"{esc('、'.join(stats[k].name for k in unstable))} 名次变动 ≥2 位，"
                f"这几位的相对强弱不要当结论用。</div>")
    sat_block = ""
    if sat.get("saturated"):
        sat_block = (f"<div class='warn' style='background:#3a1c1c;border-color:#7a3030;color:#ffb3a7'>"
                     f"<b>⚠ 饱和诊断</b>：本套任务对当前模型池已饱和，成功率极差仅 "
                     f"{sat['solve_rate_spread'] * 100:.1f} 个百分点，"
                     f"{'所有模型 95% 置信区间两两全部重叠' if sat['all_ci_overlap'] else '置信区间高度重叠'} —— "
                     f"<b>正确率层面分不出高下，名次差异落在噪声内</b>。"
                     f"已饱和的族：{esc('、'.join(FAMILY_LABEL.get(f, f) for f in sat.get('saturated_families', [])))}；"
                     f"仍有区分度：{esc('、'.join(FAMILY_LABEL.get(f, f) for f in sat.get('discriminating_families', [])))}。"
                     f"<br>此时该看体验指标（延迟/成本/一致性），并把任务升级后再排名。</div>")

    chk_block = ""
    if chk:
        rows_chk = []
        for f in sorted(chk):
            items = "".join(
                f"<span class='chip{' bad' if d['rate'] < 0.9 else ''}'>"
                f"{esc(_check_label(name))} {d['rate'] * 100:.0f}%"
                f"<i>{d['pass']}/{d['n']}</i></span>"
                for name, d in chk[f].items())
            rows_chk.append(f"<tr><td class='name'>{esc(FAMILY_LABEL.get(f, f))}</td>"
                            f"<td class='chips'>{items}</td></tr>")
        chk_block = f"""
<h2>族内子检查 <span class="sub">折进「对/错」之前丢掉的那部分</span></h2>
<p class="note">成功率把每道题的内部检查折成了一个布尔值。下面是没被折掉的分项 ——
正确率饱和之后，区分度主要来自这里（例如长会话族的「首答正确」与「末答正确」之差就是衰减）。</p>
<table><thead><tr><th>任务族</th><th>子检查通过率</th></tr></thead>
<tbody>{''.join(rows_chk)}</tbody></table>"""

    audit_block = ""
    if audit and audit.get("n"):
        jm = {k: st.judge_mean for k, st in stats.items() if st.judge_mean is not None}
        rows_j = "".join(
            f"<tr><td class='name'>{esc(stats[k].name)}</td><td>{jm[k]:.3f}</td>"
            f"<td>{(audit.get('per_model') or {}).get(k, {}).get('n', '—')}</td></tr>"
            for k in order if k in jm)
        flip = audit.get("position_flip_rate") or 0
        warn_style = " background:#3a1c1c;border-color:#7a3030;color:#ffb3a7" if flip else ""
        audit_block = f"""
<h2>裁判审计 <span class="sub">盲评分数能不能信</span></h2>
<div class="warn" style="margin-top:0{warn_style}">
判决 {audit['n']} 次（无效 {audit.get('invalid', 0)} 次）　·　
<b>位置翻转率 {_pct(audit.get('position_flip_rate'))}</b>　·　
有效裁决率 {_pct(audit.get('decided_rate'))}　·　
判平率 {_pct(audit.get('tie_rate'))}　·　
无效率 {_pct(audit.get('invalid_rate'))}
{'<br><b>⚠ 存在位置偏置：同一对答案换个左右顺序结论就变，盲评分只能当参考。</b>' if flip else ''}
</div>
{'<table><thead><tr><th>模型</th><th>盲评相对分（对参考解，0~1）</th><th>判决数</th></tr></thead><tbody>' + rows_j + '</tbody></table>' if rows_j else ''}"""

    sens_block = ""
    if sens.get("mean_tau") is not None:
        items = "".join(
            f"<tr><td class='name'>{esc(stats[k].name)}</td><td>{sens['per_model_drift'].get(k, 0)} 位</td>"
            f"<td>{sens['per_model_rate_spread'].get(k, 0) * 100:.1f}%</td></tr>" for k in order)
        sens_block = f"""
<h2>Harness 敏感度 <span class="sub">换个壳子就变笨吗</span></h2>
<p class="note">profiles：{esc(', '.join(sens['profiles']))}　平均排名一致性 τ = <b>{sens['mean_tau']}</b>
（1 = 完全一致，越低说明越依赖 harness）</p>
<table><thead><tr><th>模型</th><th>排名漂移</th><th>成功率跨 profile 极差</th></tr></thead>
<tbody>{items}</tbody></table>"""

    return f"""<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>AI PK 实测报告 · {esc(generated)}</title>
<style>
:root{{--bg:#0f1420;--card:#171e2e;--line:#26304a;--fg:#e6ebf5;--dim:#8d9ab5;--acc:#5b8cff;
--good:#2ecc8f;--mid:#e6b84c;--bad:#e2604f}}
*{{box-sizing:border-box}}
body{{margin:0;padding:32px;background:var(--bg);color:var(--fg);
font:14px/1.6 "Segoe UI",system-ui,-apple-system,"Microsoft YaHei",sans-serif}}
h1{{font-size:26px;margin:0 0 6px}} h2{{font-size:18px;margin:34px 0 10px;
padding-bottom:8px;border-bottom:1px solid var(--line)}}
.sub{{font-size:12px;color:var(--dim);font-weight:400;margin-left:8px}}
.meta{{color:var(--dim);font-size:13px;margin-bottom:18px}}
.note{{color:var(--dim);font-size:13px}}
.warn{{background:#3a2a12;border:1px solid #6b4d17;color:#ffd88a;padding:10px 14px;
border-radius:8px;margin:14px 0;font-size:13px}}
table{{width:100%;border-collapse:collapse;background:var(--card);border-radius:10px;
overflow:hidden;margin:12px 0;font-size:13px}}
th,td{{padding:9px 11px;text-align:left;border-bottom:1px solid var(--line);vertical-align:middle}}
th{{background:#1d2637;color:var(--dim);font-weight:600;font-size:12px;
text-transform:uppercase;letter-spacing:.04em}}
tr:last-child td{{border-bottom:none}}
.rank{{font-weight:700;color:var(--acc);width:38px}}
.name{{font-weight:600}} .key{{font-size:11px;color:var(--dim);font-weight:400}}
.ci{{font-size:11px;color:var(--dim);margin-top:3px}}
.bar{{position:relative;background:#222c42;border-radius:5px;height:20px;min-width:110px}}
.fill{{position:absolute;inset:0 auto 0 0;background:linear-gradient(90deg,#3b6fe0,#5b8cff);
border-radius:5px}}
.bar span{{position:relative;font-size:11px;line-height:20px;padding-left:7px;font-weight:600}}
.good{{color:var(--good);font-weight:600}} .mid{{color:var(--mid)}}
.bad{{color:var(--bad)}} .na{{color:#4a5570}}
.chips{{line-height:2}}
.chip{{display:inline-block;background:#222c42;border:1px solid var(--line);border-radius:20px;
padding:2px 10px;margin:2px 4px 2px 0;font-size:12px;color:var(--fg)}}
.chip.bad{{border-color:#7a3030;color:#ffb3a7}}
.chip i{{font-style:normal;color:var(--dim);margin-left:6px;font-size:11px}}
.foot{{margin-top:30px;padding-top:16px;border-top:1px solid var(--line);
color:var(--dim);font-size:12px}}
code{{background:#222c42;padding:1px 6px;border-radius:4px;font-size:12px}}
</style></head><body>
<h1>AI PK 实测报告</h1>
<div class="meta">生成时间 {esc(generated)}　·　运行目录 <code>{esc(run_dir)}</code>　·　
样本 {len(stats)} 个模型 × {len(results) // max(len(stats), 1)} 次运行　·　
配对 {len(outcomes)} 场</div>

<div class="warn">怎么读：所有成功率都带 95% 置信区间，<b>区间重叠的模型不能说谁更强</b>。
这份报告刻意不给出"绝对智力分"——它只说明"在这套任务分布和这个 harness 下，这次跑出来是这样"。</div>
{sat_block}
{warn}

<h2>总榜</h2>
<table><thead><tr><th>#</th><th>模型</th><th>综合分</th><th>任务成功率</th><th>BT 分</th>
<th>pass^k</th><th>中位首字</th><th>单任务 token</th><th>reasoning 占比</th><th>故障恢复</th>
</tr></thead><tbody>{''.join(rows)}</tbody></table>

<h2>分任务族 <span class="sub">谁在哪塌方</span></h2>
<table><thead><tr><th>模型</th>{fam_head}</tr></thead><tbody>{''.join(fam_rows)}</tbody></table>
{chk_block}

<h2>体验指标 <span class="sub">跑分看不出来的东西</span></h2>
<p class="note">reasoning 占比高 = 简单任务也要想很久，等得烦、烧钱多；
pass^k = 同一任务 k 次重复全部成功的比例，反映"抽卡感"。</p>
<table><thead><tr><th>模型</th><th>中位首字</th><th>P90 总耗时</th><th>工具调用</th>
<th>无效调用率</th><th>submit 合规</th><th>一致性 σ↓</th></tr></thead><tbody>
{''.join(f"<tr><td class='name'>{esc(stats[k].name)}</td><td>{_ms(stats[k].median_ttft_ms)}</td>"
         f"<td>{_ms(stats[k].p90_total_ms)}</td><td>{stats[k].tool_calls}</td>"
         f"<td>{stats[k].wasted_call_rate * 100:.0f}%</td><td>{stats[k].submit_rate * 100:.0f}%</td>"
         f"<td>{stats[k].consistency:.2f}</td></tr>" for k in order)}
</tbody></table>
{sens_block}

<h2>配对胜负 <span class="sub">行 vs 列：行胜:列胜</span></h2>
<table><thead><tr><th>模型</th>{h2h_head}</tr></thead><tbody>{''.join(h2h_rows)}</tbody></table>
{audit_block}

<div class="foot">
原始证据：<code>{esc(run_dir / 'runs.jsonl')}</code>（含每轮 transcript、工具调用、用量、错误）<br>
复算：<code>python -m aipk report --run {esc(run_dir)}</code><br>
权威性边界：只支持"在这次配置与任务分布下的相对表现"，不支持"模型 X 整体更强"。
</div>
</body></html>"""
