"""指标聚合：把"体验"变成可比的数字。

只看正确率会重蹈官方跑分的覆辙，所以这里把成本、稳定性、协作能力一起算。
"""
from __future__ import annotations

import math
from collections import defaultdict
from dataclasses import dataclass, field

from .harness import RunResult


def wilson_ci(k: int, n: int, z: float = 1.96) -> tuple[float, float]:
    """小样本比例的正确置信区间 —— 直接报均值会被质疑。"""
    if n == 0:
        return (0.0, 1.0)
    p = k / n
    denom = 1 + z * z / n
    center = (p + z * z / (2 * n)) / denom
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / denom
    return (max(0.0, center - half), min(1.0, center + half))


@dataclass
class ModelStats:
    model_key: str
    name: str = ""
    tier: str = ""
    n: int = 0
    solved: int = 0
    reward_sum: float = 0.0
    by_family: dict[str, list[float]] = field(default_factory=lambda: defaultdict(list))
    solve_by_family: dict[str, list[int]] = field(default_factory=lambda: defaultdict(list))
    ttfts: list[int] = field(default_factory=list)
    total_ms: list[int] = field(default_factory=list)
    prompt_tokens: int = 0
    completion_tokens: int = 0
    reasoning_tokens: int = 0
    turns_total: int = 0
    tool_calls: int = 0
    wasted_tool_calls: int = 0
    injected_failures: int = 0
    recovered: int = 0
    used_submit: int = 0
    empty_outputs: int = 0
    errors: int = 0
    per_task_solved: dict[str, list[int]] = field(default_factory=lambda: defaultdict(list))
    judge_scores: list[float] = field(default_factory=list)
    excluded_infra: int = 0          # 因网关限流/网络故障被排除的运行数
    usage_estimated: int = 0         # token 数为估算值的运行数（网关不报 usage）
    param_degraded: int = 0          # 参数被网关拒绝、已降级重投的运行数（如 temperature）

    # ---- 派生指标 ----

    @property
    def solve_rate(self) -> float:
        return self.solved / self.n if self.n else 0.0

    @property
    def solve_ci(self) -> tuple[float, float]:
        return wilson_ci(self.solved, self.n)

    @property
    def mean_reward(self) -> float:
        return self.reward_sum / self.n if self.n else 0.0

    @property
    def median_ttft_ms(self) -> int | None:
        return _median(self.ttfts)

    @property
    def p90_total_ms(self) -> int | None:
        if not self.total_ms:
            return None
        s = sorted(self.total_ms)
        return s[min(len(s) - 1, int(0.9 * len(s)))]

    @property
    def reasoning_ratio(self) -> float:
        if not self.completion_tokens:
            return 0.0
        return self.reasoning_tokens / self.completion_tokens

    @property
    def tokens_per_task(self) -> float:
        if not self.n:
            return 0.0
        return (self.prompt_tokens + self.completion_tokens) / self.n

    @property
    def prompt_amplification(self) -> float:
        """上下文重发开销 = 累计 prompt tokens / (轮数 × 单轮基准)。

        多轮 agent 里每轮都要重发全部历史，turn 越多、上下文越肥，钱烧得越快。
        这个值越高说明该模型"话多、绕圈、把上下文撑大"。
        """
        if not self.turns_total:
            return 0.0
        return self.prompt_tokens / self.turns_total / 1000.0  # 单位：千 token/轮

    @property
    def wasted_call_rate(self) -> float:
        return self.wasted_tool_calls / self.tool_calls if self.tool_calls else 0.0

    @property
    def recovery_rate(self) -> float | None:
        if not self.injected_failures:
            return None
        return self.recovered / self.injected_failures

    @property
    def submit_rate(self) -> float:
        return self.used_submit / self.n if self.n else 0.0

    @property
    def pass_k(self) -> float:
        """k 次重复**全部**成功的比例 —— 抽卡感强不强就看这个。"""
        if not self.per_task_solved:
            return 0.0
        full = sum(1 for v in self.per_task_solved.values() if v and all(v))
        return full / len(self.per_task_solved)

    @property
    def consistency(self) -> float:
        """同一任务多次运行的得分标准差均值（越小越稳）。"""
        vals = []
        for v in self.per_task_solved.values():
            if len(v) > 1:
                m = sum(v) / len(v)
                vals.append(math.sqrt(sum((x - m) ** 2 for x in v) / len(v)))
        return sum(vals) / len(vals) if vals else 0.0

    @property
    def judge_mean(self) -> float | None:
        return sum(self.judge_scores) / len(self.judge_scores) if self.judge_scores else None


def _median(xs: list[int]) -> int | None:
    if not xs:
        return None
    s = sorted(xs)
    return s[len(s) // 2]


def aggregate(results: list[RunResult], names: dict[str, tuple[str, str]] | None = None) -> dict[str, ModelStats]:
    names = names or {}
    out: dict[str, ModelStats] = {}
    for r in results:
        st = out.setdefault(r.model_key, ModelStats(model_key=r.model_key))
        if r.model_key in names:
            st.name, st.tier = names[r.model_key]
        if r.infra_failure:
            # 基础设施故障不是模型的锅：单独计数，不污染能力指标
            st.excluded_infra += 1
            continue
        st.n += 1
        st.solved += 1 if r.solved else 0
        st.reward_sum += r.reward
        st.by_family[r.task_key.split(":")[0]].append(r.reward)
        st.solve_by_family[r.task_key.split(":")[0]].append(1 if r.solved else 0)
        st.per_task_solved[r.task_key].append(1 if r.solved else 0)
        if r.ttft_ms is not None:
            st.ttfts.append(r.ttft_ms)
        st.total_ms.append(r.total_ms)
        st.prompt_tokens += r.prompt_tokens
        st.completion_tokens += r.completion_tokens
        st.reasoning_tokens += r.reasoning_tokens
        st.turns_total += r.turns
        st.tool_calls += r.tool_calls
        st.wasted_tool_calls += r.wasted_tool_calls
        st.injected_failures += r.injected_failures
        st.recovered += 1 if (r.injected_failures and r.solved) else 0
        st.used_submit += 1 if r.used_submit else 0
        st.empty_outputs += r.empty_outputs
        st.usage_estimated += 1 if r.usage_estimated else 0
        st.param_degraded += 1 if r.param_degraded else 0
        st.errors += 1 if r.error else 0
        if r.judge_scores:
            st.judge_scores.append(sum(r.judge_scores.values()) / len(r.judge_scores))
    return out


def family_matrix(stats: dict[str, ModelStats]) -> dict[str, dict[str, float]]:
    """{模型: {任务族: 成功率}}，用于找"哪个模型在哪类任务上塌方"。"""
    fams = sorted({f for st in stats.values() for f in st.solve_by_family})
    out: dict[str, dict[str, float]] = {}
    for key, st in stats.items():
        row: dict[str, float] = {}
        for f in fams:
            v = st.solve_by_family.get(f) or []
            row[f] = (sum(v) / len(v)) if v else float("nan")
        out[key] = row
    return out


def check_breakdown(results: list[RunResult]) -> dict[str, dict[str, dict]]:
    """{族: {子检查项: {pass, n, rate}}} —— 把族内部的机制信号摊开。

    为什么需要它：正确率饱和之后，真正有信息量的是族内部的分项。
    例如 premise 族的"前提判定"对而"费用"错，说明模型识破了错前提但算错了账；
    decay 族的"首答正确率"与"末答正确率"之差就是**长会话衰减**；
    mindiff 族的"自测通过"对而"diff 最小"错，说明它修对了但顺手重构了。
    这些全在 grade.checks 里，以前只被折成一个 solved，信号被丢掉了。
    """
    acc: dict[str, dict[str, list[int]]] = defaultdict(lambda: defaultdict(list))
    for r in results:
        if r.infra_failure or not r.grade or not r.grade.checks:
            continue
        fam = r.task_key.split(":")[0]
        for name, ok in r.grade.checks.items():
            acc[fam][name].append(1 if ok else 0)
    return {fam: {name: {"pass": sum(v), "n": len(v), "rate": round(sum(v) / len(v), 3)}
                  for name, v in items.items()}
            for fam, items in sorted(acc.items())}


def judge_audit_from_results(results: list[RunResult]) -> dict:
    """从原始 transcript 里把**裁判自己的可靠性**汇总出来。

    盲评只有在能自证"裁判不偏"的时候才有意义，所以要报三件事：
      - 位置翻转率：同一对答案换个位置结论就变 → 位置偏置
      - 无效率：裁判没吐出可解析的 JSON（reasoning 吃光配额那类）
      - 判平率 / 有效裁决数：全是平局的裁判等于没判

    以前 `grade.judge_audit()` 写好了却从没被报告调用过 —— 盲评分数进了 summary，
    裁判的可靠性却没人看。这里把它接上。
    """
    n = flips = decided = ties = invalid = 0
    per_model: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    per_judge: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    for r in results:
        for t in (r.transcript or []):
            if "judge" in t:
                raw = t.get("judge") or []
                if not raw:
                    invalid += 1
                    per_model[r.model_key]["invalid"] += 1
                    continue
                n += 1
                per_model[r.model_key]["n"] += 1
                flipped = bool(t.get("flipped"))
                w = t.get("winner")
                if flipped:
                    flips += 1
                    per_model[r.model_key]["flipped"] += 1
                if w in ("1", "2"):
                    decided += 1
                    per_model[r.model_key]["decided"] += 1
                elif w == "tie":
                    ties += 1
                    per_model[r.model_key]["tie"] += 1
                # 每个裁判单独计数：多裁判（委员会）跑的时候，要能看出是哪个裁判在翻
                for entry in raw:
                    jk = str(entry.get("judge") or "?")
                    per_judge[jk]["n"] += 1
                    per_judge[jk]["flipped"] += 1 if flipped else 0
                    if w in ("1", "2"):
                        per_judge[jk]["decided"] += 1
                    elif w == "tie":
                        per_judge[jk]["tie"] += 1
            elif "judge_error" in t or "judge_invalid" in t:
                invalid += 1
                per_model[r.model_key]["invalid"] += 1
    if not n and not invalid:
        return {"n": 0, "note": "本轮没有启用盲评（--no-judge 或任务族没有 judge_prompt）"}
    return {
        "n": n,
        "position_flip_rate": round(flips / n, 3) if n else None,
        "decided_rate": round(decided / n, 3) if n else None,
        "tie_rate": round(ties / n, 3) if n else None,
        "invalid": invalid,
        "invalid_rate": round(invalid / (n + invalid), 3) if (n + invalid) else None,
        "per_model": {k: dict(v) for k, v in sorted(per_model.items())},
        "per_judge": {
            k: {**dict(v), "flip_rate": round(v["flipped"] / v["n"], 3) if v["n"] else None}
            for k, v in sorted(per_judge.items())
        },
    }


# ---------------------------------------------------------------- 综合分


WEIGHTS = {
    "quality": 0.50,     # 成功率（带 CI）
    "reliability": 0.20, # pass^k + 一致性
    "cost": 0.20,        # 延迟 + token 经济
    "collab": 0.10,      # 工具效率 + 故障恢复
}

ALT_WEIGHTS = {
    "quality_first": {"quality": 0.80, "reliability": 0.10, "cost": 0.05, "collab": 0.05},
    "speed_first": {"quality": 0.35, "reliability": 0.15, "cost": 0.45, "collab": 0.05},
    "no_cost": {"quality": 0.60, "reliability": 0.25, "cost": 0.00, "collab": 0.15},
}


def _norm(values: dict[str, float], higher_better: bool = True) -> dict[str, float]:
    """min-max 归一到 0..1；全部相同则给 1.0（不制造虚假差异）。"""
    if not values:
        return {}
    lo, hi = min(values.values()), max(values.values())
    if abs(hi - lo) < 1e-12:
        return {k: 1.0 for k in values}
    if higher_better:
        return {k: (v - lo) / (hi - lo) for k, v in values.items()}
    return {k: (hi - v) / (hi - lo) for k, v in values.items()}


def composite(stats: dict[str, ModelStats], weights: dict[str, float] | None = None) -> dict[str, float]:
    w = weights or WEIGHTS
    q = {k: st.solve_rate for k, st in stats.items()}
    rel = {k: 0.6 * st.pass_k + 0.4 * (1 - min(1.0, st.consistency)) for k, st in stats.items()}
    lat = {k: (st.median_ttft_ms or 0) for k, st in stats.items()}
    tok = {k: st.tokens_per_task for k, st in stats.items()}
    lat_n = _norm(lat, higher_better=False)
    tok_n = _norm(tok, higher_better=False)
    cost = {k: 0.5 * lat_n.get(k, 1.0) + 0.5 * tok_n.get(k, 1.0) for k in stats}
    rec = {k: (st.recovery_rate if st.recovery_rate is not None else 0.5) for k, st in stats.items()}
    rec_n = _norm(rec, higher_better=True)
    collab = {k: 0.5 * rec_n.get(k, 1.0) + 0.5 * (1 - min(1.0, st.wasted_call_rate))
              for k, st in stats.items()}
    out: dict[str, float] = {}
    for k in stats:
        out[k] = (w["quality"] * q.get(k, 0.0)
                  + w["reliability"] * rel.get(k, 0.0)
                  + w["cost"] * cost.get(k, 0.0)
                  + w["collab"] * collab.get(k, 0.0))
    return out


def rank_stability(stats: dict[str, ModelStats]) -> dict:
    """换权重方案后排名是否翻转 —— 翻转了就说明"这俩其实差不多"。"""
    schemes = {"default": WEIGHTS, **ALT_WEIGHTS}
    ranks: dict[str, dict[str, int]] = {}
    for name, w in schemes.items():
        sc = composite(stats, w)
        order = sorted(sc, key=lambda k: -sc[k])
        ranks[name] = {k: i + 1 for i, k in enumerate(order)}
    keys = list(stats)
    max_drift = {k: max(ranks[n][k] for n in ranks) - min(ranks[n][k] for n in ranks) for k in keys}
    return {"ranks": ranks, "max_drift": max_drift,
            "unstable_models": sorted([k for k, d in max_drift.items() if d >= 2])}


def saturation_report(stats: dict[str, ModelStats], fam: dict[str, dict[str, float]],
                      min_families: int = 3) -> dict:
    """饱和诊断：这套题还分不分得开模型？

    诚实地说，如果所有模型成功率都挤在窄带里，**正确率排名就是噪声**。
    这时候该说的是"本套题已饱和"，而不是硬排名次。
    """
    rates = {k: st.solve_rate for k, st in stats.items() if st.n}
    if not rates:
        return {"saturated": False, "note": "无数据"}
    spread = max(rates.values()) - min(rates.values())
    # 所有成功率 95% CI 是否两两重叠（重叠=统计上分不出）
    cis = {k: st.solve_ci for k, st in stats.items() if st.n}
    overlapping_all = True
    keys = list(cis)
    for i in range(len(keys)):
        for j in range(i + 1, len(keys)):
            lo1, hi1 = cis[keys[i]]
            lo2, hi2 = cis[keys[j]]
            if hi1 < lo2 or hi2 < lo1:
                overlapping_all = False
                break
        if not overlapping_all:
            break
    # 任务族饱和：成功率 >= 0.95 的族
    sat_fams = [f for f in sorted({x for row in fam.values() for x in row})
                if all((fam[k].get(f) or 0) >= 0.95 for k in stats if fam[k].get(f) == fam[k].get(f))]
    discriminating = [f for f in sorted({x for row in fam.values() for x in row}) if f not in sat_fams]
    # 判"饱和"的两条理由（任一成立即可），并**说明是哪一条**：
    #   ① 成功率极差太小（<15pp）：模型挤在一条窄带里
    #   ② 没有任何族能把模型分开（每一族对所有模型都 ≥95%）
    # 早期写法是 `len(sat_fams) >= max(1, len(sat_fams)+len(discriminating)-min_families)`，
    # 这个式子在"5 族里 2 族饱和、3 族仍有区分度"时会误判成整体饱和 —— 而报告标题级
    # 结论就是"已饱和，正确率分不出高下"，误判会直接把结论写歪。
    reasons: list[str] = []
    if spread < 0.15:
        reasons.append(f"成功率极差仅 {spread * 100:.1f} 个百分点")
    if not discriminating:
        reasons.append("没有任何任务族能把模型分开（各族对所有模型都 ≥95%）")
    saturated = bool(reasons)
    return {
        "saturated": saturated,
        "saturated_reason": "；".join(reasons) if reasons else "",
        "solve_rate_spread": round(spread, 3),
        "all_ci_overlap": bool(overlapping_all),
        "saturated_families": sat_fams,
        "discriminating_families": discriminating,
        "note": ("本套任务对当前模型池已饱和（" + "；".join(reasons) + "）："
                 "正确率无法区分模型，排名差异落在噪声内。此时应看体验指标"
                 "（延迟/成本/一致性/族内子检查），并把任务升级（提高步数、加长上下文、"
                 "组合多族）后再排名。"
                 if saturated else
                 "本套任务仍有区分度（还有族能把模型分开，且成功率极差 ≥15pp），可参考正确率排名。"),
    }
