"""族 2：工具链跟随（打的是"会不会用工具 + 会不会走神"）。

设计：一棵虚拟文件依赖树。根文件列出若干子文件；子文件各自带一个数值 + 更多引用。
正确答案 = 从根出发**可达**文件的数值之和。
埋两个坑：
  A. 引用了不存在的文件 → 跳过（很多模型会瞎编一个数）
  B. 存在从根不可达的文件 → 只能读到根可达的部分（"把目录里所有文件都读了"会算错）
判定：数值精确比对。
"""
from __future__ import annotations

from . import GradeResult, TaskInstance, extract_json, register, rng

THEMES = [
    ("账单", "账单项"), ("工单", "工单条目"), ("库存", "库存记录"),
    ("实验", "实验批次"), ("凭证", "凭证条目"),
]


@register("toolchain")
def build(seed: int) -> TaskInstance:
    r = rng(seed * 7919 + 13)
    theme, item = r.choice(THEMES)
    # 用 seed 派生一个短编号，写进文件名：否则不同 seed 可能生成结构完全相同的题
    tag = seed % 89 + 10
    n_nodes = r.choice([7, 8, 9, 10])
    ids = [f"{theme}{tag}_{i:02d}.txt" for i in range(1, n_nodes + 1)]
    values = {i: r.randint(11, 97) for i in ids}

    # 造树：节点 i 引用若干更小编号的节点
    children: dict[str, list[str]] = {ids[0]: []}
    for k in range(1, n_nodes):
        pool = ids[:k]
        n_ref = min(len(pool), r.choice([1, 1, 2, 2, 3]))
        children[ids[k]] = sorted(r.sample(pool, n_ref))
    # 根指向若干节点
    children[ids[0]] = sorted(r.sample(ids[1:], min(n_nodes - 1, r.choice([2, 3, 3]))))

    # 坑 A：加一个不存在的引用
    ghost = f"{theme}{tag}_99.txt"
    ghost_host = r.choice(ids[1:])
    children[ghost_host] = sorted(set(children[ghost_host] + [ghost]))

    # 坑 B：造一个从根不可达的节点
    reachable = set()
    stack = [ids[0]]
    while stack:
        cur = stack.pop()
        if cur in reachable or cur == ghost:
            continue
        reachable.add(cur)
        stack.extend(children.get(cur, []))
    unreachable = [i for i in ids if i not in reachable]

    files: dict[str, str] = {}
    for i in ids:
        body = [f"# {item} {i}", f"金额={values[i]}"]
        if children.get(i):
            body.append("关联文件：" + ", ".join(children[i]))
        else:
            body.append("关联文件：无")
        files[i] = "\n".join(body)
    if not unreachable:
        # 强制造一个孤儿
        orphan = ids[-1]
        files[orphan] = f"# {item} {orphan}\n金额={values[orphan]}\n关联文件：无"
        for k in list(children):
            children[k] = [c for c in children[k] if c != orphan]
        unreachable = [orphan]
        reachable = {i for i in ids if i not in unreachable}

    expected = sum(values[i] for i in reachable)

    prompt = f"""你正在核对一批{theme}。文件都在虚拟文件系统里，用 read_file 读取。

起点：`{ids[0]}`

规则（必须严格遵守）：
1. 从起点文件开始，**沿着「关联文件」字段递归**把所有能到达的文件都读一遍。
2. 只统计**从起点可达**的文件的「金额」。
3. 如果某个「关联文件」在文件系统里不存在，直接跳过，不要臆造金额。
4. 不要统计没有从起点链接到的文件。

最后给出从起点可达的所有文件金额**总和**。
只输出 JSON，不要解释。
格式：{{"total": 整数, "files_read": ["读过的文件名", ...]}}"""

    def grader(answer: str, _expected=expected, _reach=reachable, _ghost=ghost, **_kw) -> GradeResult:
        obj = extract_json(answer)
        if not isinstance(obj, dict):
            return GradeResult(False, 0.0, {"format": False}, "没给出 JSON 对象", parse_ok=False)
        if "total" not in obj:
            return GradeResult(False, 0.0, {"format": False}, "缺少 total 字段", parse_ok=False)
        try:
            total = int(obj["total"])
        except Exception:  # noqa: BLE001
            return GradeResult(False, 0.0, {"format": False}, f"total 不是整数：{obj['total']!r}", parse_ok=False)
        checks = {
            "total_correct": total == _expected,
            "no_ghost": _ghost not in [str(x) for x in (obj.get("files_read") or [])],
            "listed_files": bool(obj.get("files_read")),
        }
        solved = checks["total_correct"]
        reason = "总和正确" if solved else f"总和错误：期望 {_expected}，得到 {total}（差 {total - _expected:+d}）"
        return GradeResult(solved, 1.0 if solved else (0.5 if checks["no_ghost"] else 0.0), checks, reason)

    return TaskInstance(
        tid=f"t{seed}", family="toolchain", seed=seed,
        messages=[{"role": "user", "content": prompt}],
        answer_spec='{"total": 整数, "files_read": [...]}',
        grader=grader, tools=["read_file"], needs_tools=True,
        meta={"expected": expected, "reachable": sorted(reachable),
              "unreachable": sorted(unreachable), "ghost": ghost, "files": files},
    )
