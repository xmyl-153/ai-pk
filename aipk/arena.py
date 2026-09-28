"""PK 引擎：Bradley-Terry 评分 + bootstrap 置信区间 + harness 敏感度。

不报"谁第一"这种孤点结论，报的是：
- 相对实力的点估计 + 95% 区间（区间重叠就是"分不出高下"）
- 配对胜负矩阵（谁克谁）
- 换 harness 壳子后排名漂移多少（真实体验差异）
"""
from __future__ import annotations

import math
import random
from collections import defaultdict
from dataclasses import dataclass, field

from .harness import RunResult


@dataclass
class PairOutcome:
    a: str
    b: str
    task_key: str
    seed: int
    rep: int
    result: str          # "a" | "b" | "tie"


def head_to_head(results: list[RunResult]) -> list[PairOutcome]:
    """同一任务 + 同一 seed + 同一 rep 的两次运行配对比较。

    含基础设施故障的运行**不参与**配对 —— 否则"谁被限流谁就输"，
    排名测的是网关而不是模型。
    """
    bucket: dict[tuple[str, int, int], list[RunResult]] = defaultdict(list)
    for r in results:
        if r.infra_failure:
            continue
        bucket[(r.task_key, r.seed, r.rep)].append(r)
    out: list[PairOutcome] = []
    for (task_key, seed, rep), rs in bucket.items():
        for i in range(len(rs)):
            for j in range(i + 1, len(rs)):
                a, b = rs[i], rs[j]
                if a.model_key == b.model_key:
                    continue
                if a.solved and not b.solved:
                    res = "a"
                elif b.solved and not a.solved:
                    res = "b"
                elif a.solved and b.solved:
                    res = "tie"
                else:
                    # 都失败：用连续分区分，仍无差别则平局
                    if a.reward - b.reward > 1e-9:
                        res = "a"
                    elif b.reward - a.reward > 1e-9:
                        res = "b"
                    else:
                        res = "tie"
                out.append(PairOutcome(a.model_key, b.model_key, task_key, seed, rep, res))
    return out


# ---------------------------------------------------------------- Bradley-Terry


def fit_bt(wins: dict[str, float], iters: int = 500) -> dict[str, float]:
    """MM 算法拟合 Bradley-Terry 强度。wins[(i,j)] 是 i 对 j 的等效胜场（平局各半）。"""
    players = sorted({p for k in wins for p in k})
    if not players:
        return {}
    s = {p: 1.0 for p in players}
    for _ in range(iters):
        new: dict[str, float] = {}
        for i in players:
            num = 0.0
            for (x, y), w in wins.items():
                if x == i:
                    num += w
            den = 0.0
            for j in players:
                if j == i:
                    continue
                n_ij = 0.0
                for (x, y), w in wins.items():
                    if (x == i and y == j) or (x == j and y == i):
                        n_ij += w
                if n_ij > 0:
                    den += n_ij / (s[i] + s[j])
            new[i] = (num / den) if den > 0 else s[i]
        # 归一到几何均值 1，避免漂移
        gm = math.exp(sum(math.log(max(v, 1e-12)) for v in new.values()) / len(new))
        s = {k: max(v / gm, 1e-9) for k, v in new.items()}
    return s


def _to_wins(outcomes: list[PairOutcome]) -> dict[tuple[str, str], float]:
    wins: dict[tuple[str, str], float] = defaultdict(float)
    for o in outcomes:
        if o.result == "a":
            wins[(o.a, o.b)] += 1.0
        elif o.result == "b":
            wins[(o.b, o.a)] += 1.0
        else:
            wins[(o.a, o.b)] += 0.5
            wins[(o.b, o.a)] += 0.5
    return wins


def bt_ratings(outcomes: list[PairOutcome], scale: float = 400.0, n_boot: int = 400,
               seed: int = 0) -> dict[str, dict[str, float]]:
    """返回 {模型: {rating, lo, hi, n_games, win_rate}}。rating 以 1000 为基准。"""
    if not outcomes:
        return {}
    wins = _to_wins(outcomes)
    s = fit_bt(wins)
    if not s:
        return {}
    gm = math.exp(sum(math.log(max(v, 1e-12)) for v in s.values()) / len(s))

    def to_rating(x: float) -> float:
        return 1000.0 + scale * math.log(max(x, 1e-12) / gm, 10) / 0.4  # 近似 Elo 刻度

    base = {p: to_rating(v) for p, v in s.items()}

    # 统计每人的胜负场
    games: dict[str, int] = defaultdict(int)
    won: dict[str, float] = defaultdict(float)
    for o in outcomes:
        games[o.a] += 1
        games[o.b] += 1
        if o.result == "a":
            won[o.a] += 1
        elif o.result == "b":
            won[o.b] += 1
        else:
            won[o.a] += 0.5
            won[o.b] += 0.5

    # bootstrap：重采样对局
    r = random.Random(seed)
    boots: dict[str, list[float]] = defaultdict(list)
    for _ in range(n_boot):
        sample = [outcomes[r.randrange(len(outcomes))] for _ in range(len(outcomes))]
        ss = fit_bt(_to_wins(sample), iters=200)
        if not ss:
            continue
        g2 = math.exp(sum(math.log(max(v, 1e-12)) for v in ss.values()) / len(ss))
        for p, v in ss.items():
            boots[p].append(1000.0 + scale * math.log(max(v, 1e-12) / g2, 10) / 0.4)

    out: dict[str, dict[str, float]] = {}
    for p in base:
        vals = sorted(boots.get(p) or [base[p]])
        lo = vals[int(0.025 * (len(vals) - 1))]
        hi = vals[int(0.975 * (len(vals) - 1))]
        out[p] = {
            "rating": round(base[p], 1),
            "lo": round(lo, 1),
            "hi": round(hi, 1),
            "n_games": games[p],
            "win_rate": round(won[p] / games[p], 3) if games[p] else 0.0,
        }
    return out


def win_matrix(outcomes: list[PairOutcome]) -> dict[str, dict[str, dict[str, int]]]:
    m: dict[str, dict[str, dict[str, int]]] = defaultdict(lambda: defaultdict(lambda: defaultdict(int)))
    for o in outcomes:
        m[o.a][o.b][o.result] += 1
        inv = {"a": "b", "b": "a", "tie": "tie"}[o.result]
        m[o.b][o.a][inv] += 1
    return {k: {k2: dict(v2) for k2, v2 in v.items()} for k, v in m.items()}


# ---------------------------------------------------------------- harness 敏感度


def kendall_tau(order_a: list[str], order_b: list[str]) -> float:
    """两个排名的一致性，1=完全一致，-1=完全相反。"""
    common = [x for x in order_a if x in order_b]
    if len(common) < 2:
        return 1.0
    pos_b = {k: i for i, k in enumerate(order_b)}
    seq = [pos_b[k] for k in common]
    conc = disc = 0
    for i in range(len(seq)):
        for j in range(i + 1, len(seq)):
            if seq[i] < seq[j]:
                conc += 1
            elif seq[i] > seq[j]:
                disc += 1
    tot = conc + disc
    return (conc - disc) / tot if tot else 1.0


def harness_sensitivity(results: list[RunResult]) -> dict:
    """同一模型在不同 harness profile 下的排名漂移。"""
    by_profile: dict[str, list[RunResult]] = defaultdict(list)
    for r in results:
        if r.infra_failure:
            continue
        by_profile[r.profile].append(r)
    if len(by_profile) < 2:
        return {"profiles": sorted(by_profile), "note": "只有一个 profile，无法算敏感度",
                "per_model_drift": {}, "mean_tau": None, "unstable_models": []}

    ranks: dict[str, dict[str, int]] = {}
    solve_rates: dict[str, dict[str, float]] = {}
    for prof, rs in by_profile.items():
        agg: dict[str, list[int]] = defaultdict(list)
        for r in rs:
            agg[r.model_key].append(1 if r.solved else 0)
        rate = {k: sum(v) / len(v) for k, v in agg.items()}
        solve_rates[prof] = rate
        order = sorted(rate, key=lambda k: -rate[k])
        ranks[prof] = {k: i + 1 for i, k in enumerate(order)}

    # 只比较**在全部 profile 里都跑过**的模型。
    # 否则没跑过的模型会被当成"排名掉到末尾"，算出上百位的假漂移（实测踩过：drift=97）。
    profs = sorted(by_profile)
    common = [m for m in solve_rates[profs[0]] if all(m in solve_rates[p] for p in profs)]
    if not common:
        return {"profiles": profs, "note": "没有跨全部 profile 都跑过的模型，无法比较",
                "per_model_drift": {}, "mean_tau": None, "unstable_models": []}

    models = sorted(common)
    drift = {m: (max(ranks[p][m] for p in profs) - min(ranks[p][m] for p in profs)) for m in models}
    rate_spread = {m: round(max(solve_rates[p].get(m, 0) for p in profs)
                            - min(solve_rates[p].get(m, 0) for p in profs), 3) for m in models}
    taus = []
    for i in range(len(profs)):
        for j in range(i + 1, len(profs)):
            oa = [k for k, _ in sorted(ranks[profs[i]].items(), key=lambda kv: kv[1]) if k in common]
            ob = [k for k, _ in sorted(ranks[profs[j]].items(), key=lambda kv: kv[1]) if k in common]
            taus.append(kendall_tau(oa, ob))
    return {
        "profiles": profs,
        "compared_models": models,
        "per_model_drift": drift,
        "per_model_rate_spread": rate_spread,
        "mean_tau": round(sum(taus) / len(taus), 3) if taus else None,
        "unstable_models": sorted([m for m, d in drift.items() if d >= 2]),
        "solve_rates": {p: {k: round(v, 3) for k, v in r.items() if k in common}
                        for p, r in solve_rates.items()},
    }
