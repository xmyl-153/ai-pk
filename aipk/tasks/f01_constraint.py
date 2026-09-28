"""族 1：约束满足排班（打的是"真推理"，不是"记题型"）。

生成法：先随机造一个可行解，再从解反推约束 → 必然可解；
用回溯求解器验证**唯一解**，不唯一就换 seed 重生成。
判定：代码验证答案是否满足全部约束（不比对参考解，任何合法解都算对）。
"""
from __future__ import annotations

import itertools

from . import GradeResult, TaskInstance, extract_json, register, rng

TASK_NAMES = ["数据清洗", "特征抽取", "模型评测", "报告撰写", "索引重建",
              "日志归档", "权限审计", "缓存预热", "依赖升级", "回滚演练",
              "压力测试", "容量评估", "告警收敛", "密钥轮换", "流量切换"]
OWNERS = ["阿岩", "小满", "子墨", "青禾", "望舒", "澄澄", "亦舟", "南枝"]
_DAYS = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"]
_HALVES = ["上午", "下午", "晚上"]


def _slot_labels(m: int) -> list[str]:
    """槽位数随任务数变化，标签也要跟着长，避免越界。"""
    out: list[str] = []
    for d in _DAYS:
        for h in _HALVES:
            out.append(d + h)
            if len(out) >= m:
                return out
    return out + [f"槽{i}" for i in range(len(out), m)]


def _solve(n: int, m: int, fixed: dict[int, int], forbid: dict[int, set[int]],
           order: list[tuple[int, int]], limit: int = 3) -> list[tuple[int, ...]]:
    """回溯求解：返回最多 limit 个解（用来判断唯一性）。"""
    sols: list[tuple[int, ...]] = []
    assign: list[int] = [-1] * n

    def ok(i: int, s: int) -> bool:
        if s in forbid.get(i, ()):  # 禁排
            return False
        for a, b in order:  # a 必须早于 b
            if a == i and assign[b] != -1 and not (s < assign[b]):
                return False
            if b == i and assign[a] != -1 and not (assign[a] < s):
                return False
        return True

    def bt(i: int) -> None:
        if len(sols) >= limit:
            return
        if i == n:
            sols.append(tuple(assign))
            return
        if i in fixed:
            s = fixed[i]
            if ok(i, s):
                assign[i] = s
                bt(i + 1)
                assign[i] = -1
            return
        for s in range(m):
            if ok(i, s):
                assign[i] = s
                bt(i + 1)
                assign[i] = -1

    bt(0)
    return sols


@register("constraint")
def build(seed: int) -> TaskInstance:
    for attempt in range(200):
        r = rng(seed * 1000 + attempt)
        n = r.choice([5, 6, 6, 7])
        m = n  # 槽数=任务数：每槽恰好一项（题目仍写"最多一项"，但解必须是双射）
        tasks = r.sample(TASK_NAMES, n)
        owner = r.sample(OWNERS, min(n, len(OWNERS)))
        owner = [owner[i % len(owner)] for i in range(n)]

        # 先造一个可行解：n 个任务放进 m 个槽（每槽最多一个）
        truth = [i % m for i in range(n)]
        r.shuffle(truth)

        fixed: dict[int, int] = {}
        forbid: dict[int, set[int]] = {}
        order: list[tuple[int, int]] = []

        # 固定若干任务
        for i in r.sample(range(n), k=r.choice([1, 1, 2])):
            fixed[i] = truth[i]
        # 禁排：从非真值槽里挑
        for i in range(n):
            others = [s for s in range(m) if s != truth[i]]
            for s in r.sample(others, k=r.choice([0, 1, 1, 2])):
                forbid.setdefault(i, set()).add(s)

        # 关键：**增量加顺序约束直到解唯一**（否则约束太松，题目有多解、无法判定）
        cands: list[tuple[int, int]] = []
        for a in range(n):
            for b in range(n):
                if a != b and truth[a] < truth[b]:
                    cands.append((a, b))
        r.shuffle(cands)
        if not cands:
            continue
        for a, b in cands[: max(1, n)]:
            order.append((a, b))
        order = sorted(set(order))
        if len(_solve(n, m, fixed, forbid, order, limit=2)) != 1:
            for a, b in cands:
                order = sorted(set(order + [(a, b)]))
                if len(_solve(n, m, fixed, forbid, order, limit=2)) == 1:
                    break
            else:
                continue
        if len(_solve(n, m, fixed, forbid, order, limit=2)) != 1:
            continue

        lines = [f"团队有 {n} 项任务需要在 {m} 个时间槽内排完。每个时间槽**最多安排一项任务**，每项任务恰好占一个槽。",
                 "", "任务清单："]
        for i, t in enumerate(tasks):
            lines.append(f"  {i + 1}. {t}（负责人：{owner[i]}）")
        lines += ["", "时间槽编号（用编号作答）："]
        labels = _slot_labels(m)
        for s in range(m):
            lines.append(f"  槽 {s} = {labels[s]}")
        lines += ["", "硬约束："]
        for i, s in sorted(fixed.items()):
            lines.append(f"  - 「{tasks[i]}」必须排在槽 {s}")
        for i, ss in sorted(forbid.items()):
            if ss:
                lines.append(f"  - 「{tasks[i]}」不能排在槽 " + "、".join(str(x) for x in sorted(ss)))
        for a, b in order:
            lines.append(f"  - 「{tasks[a]}」必须严格早于「{tasks[b]}」")
        lines += ["", "要求：给出**唯一**满足全部约束的排班方案，用槽编号表示。",
                  "不要输出推理过程，只输出 JSON。",
                  '格式：{"schedule": {"任务名": 槽编号, ...}}']

        prompt = "\n".join(lines)
        truth_map = {tasks[i]: truth[i] for i in range(n)}
        ref = {"schedule": truth_map}

        def grader(answer: str, _truth=truth_map, _n=n, _m=m, _tasks=tasks, _fixed=fixed,
                   _forbid=forbid, _order=order, **_kw) -> GradeResult:
            obj = extract_json(answer)
            if not isinstance(obj, dict) or not isinstance(obj.get("schedule"), dict):
                return GradeResult(False, 0.0, {"format": False}, "没给出合法 JSON 结构", parse_ok=False)
            got = obj["schedule"]
            got_norm: dict[str, int] = {}
            for k, v in got.items():
                try:
                    got_norm[str(k).strip()] = int(v)
                except Exception:  # noqa: BLE001
                    return GradeResult(False, 0.0, {"format": False}, f"槽编号不是整数：{k}={v!r}", parse_ok=False)
            checks: dict[str, bool] = {}
            checks["covered_all"] = set(got_norm) == set(_tasks)
            checks["distinct_slots"] = len(set(got_norm.values())) == len(got_norm)
            checks["slot_range"] = all(0 <= s < _m for s in got_norm.values())
            checks["fixed_ok"] = all(got_norm.get(_tasks[i]) == s for i, s in _fixed.items())
            checks["forbid_ok"] = all(got_norm.get(_tasks[i]) not in ss for i, ss in _forbid.items())
            checks["order_ok"] = all(
                (got_norm.get(_tasks[a], 99) < got_norm.get(_tasks[b], -1)) for a, b in _order
            )
            hard = [k for k in ("covered_all", "distinct_slots", "slot_range", "fixed_ok", "forbid_ok", "order_ok")
                    if not checks[k]]
            solved = not hard
            n_ok = sum(1 for v in checks.values() if v)
            reward = n_ok / len(checks)
            reason = "满足全部约束" if solved else f"违反：{', '.join(hard)}"
            return GradeResult(solved, reward, checks, reason)

        return TaskInstance(
            tid=f"c{seed}", family="constraint", seed=seed,
            messages=[{"role": "user", "content": prompt}],
            answer_spec='{"schedule": {"任务名": 槽编号}}',
            grader=grader, tools=[], needs_tools=False,
            meta={"n": n, "unique_solution": truth_map},
        )
    raise RuntimeError(f"constraint seed={seed} 生成不出唯一解实例")


def _unused():  # 保留 itertools 引用，避免 linter 噪声
    return itertools
