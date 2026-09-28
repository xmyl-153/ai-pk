"""族 10：长程状态维护（打的是"多轮不丢失状态"——目前唯一没被跑满的维度）。

为什么加这一族：repofix 这种"多文件 + 真跑代码 + 算数值"的硬任务，GLM-5.3 依然 3/3 打穿。
说明**凡是「代码可验证的清晰单一目标任务」，前沿模型基本饱和了**。
前面 9 族全是单轮或短多轮（2-6 轮），唯一没测过的维度是：
**一次会话里连续维护状态，任何一步错都会传播到最后**。

设计：给一份账本（code → 数值），然后分 K 批下发操作（每批一个新 user turn），
每批要求模型提交**完整的最新账本**。操作含：
  - `*n` 乘系数 / `+n` 加常数（每步四舍五入到两位）
  - `dup k=n` 以 k 为模板新增 k1..kn
  - `swap a,b` 交换两个键的值
  - `del` 删除
  - `merge {..}` 合并，**同名冲突时新值覆盖**
判定：每批都与参考状态精确比对；最终状态必须完全正确。
难度旋钮：批数 K、每批操作数、以及**专门制造的陷阱批**：
  - 批里混入自相矛盾的操作（按"后出现的为准"处理）
  - 批里混入"作废上一批的某条"的指令
"""
from __future__ import annotations

import copy
import json

from . import GradeResult, TaskInstance, extract_json, register, rng

UNITS = ["次", "条", "份", "项", "笔"]


def _apply(state: dict[str, float], op: dict) -> dict[str, float]:
    """参考实现：所有操作都在这里定义，唯一真值来源。"""
    s = dict(state)
    k = op["kind"]
    if k == "mul":
        s[op["key"]] = round(s.get(op["key"], 0.0) * op["n"], 2)
    elif k == "add":
        s[op["key"]] = round(s.get(op["key"], 0.0) + op["n"], 2)
    elif k == "dup":
        base, n = op["key"], op["n"]
        for i in range(1, n + 1):
            s[f"{base}{i}"] = s.get(base, 0.0)
    elif k == "swap":
        a, b = op["a"], op["b"]
        s[a], s[b] = s.get(b, 0.0), s.get(a, 0.0)
    elif k == "del":
        s.pop(op["key"], None)
    elif k == "merge":
        for kk, vv in op["pairs"]:
            s[kk] = round(float(vv), 2)   # 冲突时新值覆盖
    return s


def _describe(op: dict) -> str:
    k = op["kind"]
    if k == "mul":
        return f"把 `{op['key']}` 乘以 {op['n']}（结果保留两位小数）"
    if k == "add":
        return f"给 `{op['key']}` 加上 {op['n']}（结果保留两位小数）"
    if k == "dup":
        return (f"以 `{op['key']}` 当前值为模板，新增 {op['n']} 个键 "
                f"`{op['key']}1` … `{op['key']}{op['n']}`，值都等于 `{op['key']}` 的当前值")
    if k == "swap":
        return f"交换 `{op['a']}` 与 `{op['b']}` 的值"
    if k == "del":
        return f"删除键 `{op['key']}`"
    if k == "merge":
        items = "、".join(f"`{a}`={b}" for a, b in op["pairs"])
        return f"合并以下新记录（**同名时新值覆盖旧值**）：{items}"
    return "?"


def _gen_batch(r, keys: list[str], n_ops: int, trap: str | None) -> list[dict]:
    ops: list[dict] = []
    for _ in range(n_ops):
        pick = r.random()
        if pick < 0.30:
            ops.append({"kind": "mul", "key": r.choice(keys), "n": r.choice([2, 3, 5])})
        elif pick < 0.55:
            ops.append({"kind": "add", "key": r.choice(keys), "n": r.choice([7, 13, 21, -5])})
        elif pick < 0.70:
            ops.append({"kind": "swap", "a": r.choice(keys), "b": r.choice(keys)})
        elif pick < 0.85:
            ops.append({"kind": "dup", "key": r.choice(keys[:2]), "n": r.choice([1, 2])})
        else:
            a, b = r.sample(keys, 2)
            ops.append({"kind": "merge", "pairs": [(a, r.randint(11, 99)), (b, r.randint(11, 99))]})
    # 陷阱：自相矛盾（后出现的为准）
    if trap == "conflict" and ops:
        victim = ops[r.randrange(len(ops))]
        if victim["kind"] in ("mul", "add"):
            ops.append({"kind": "mul" if victim["kind"] == "add" else "add",
                        "key": victim["key"], "n": 2 if victim["kind"] == "add" else 1})
    # 陷阱：作废上一批的某条（这里表现为本批末尾的"撤销"式覆盖）
    if trap == "undo" and ops:
        last = ops[-1]
        if last["kind"] == "mul":
            ops.append({"kind": "swap", "a": last["key"], "b": last["key"]})
    return ops


@register("longstate")
def build(seed: int) -> TaskInstance:
    r = rng(seed * 40503 + 61)
    unit = r.choice(UNITS)
    n_keys = r.choice([3, 4])
    keys = [f"k{i + 1}" for i in range(n_keys)]
    state = {k: float(r.randint(11, 97)) for k in keys}
    init = copy.deepcopy(state)

    n_batches = r.choice([4, 5, 6])
    batches: list[list[dict]] = []
    for b in range(n_batches):
        trap = None
        if b == r.randrange(1, n_batches):
            trap = r.choice(["conflict", "undo"])
        batches.append(_gen_batch(r, keys, r.choice([2, 3, 3]), trap))

    # 参考执行：逐批算出每一批的期望状态
    expected_states: list[dict[str, float]] = []
    cur = dict(init)
    for ops in batches:
        for op in ops:
            cur = _apply(cur, op)
        expected_states.append(dict(cur))
    final = expected_states[-1]
    init_lines = "\n".join(f"  {k} = {v:.2f}" for k, v in init.items())

    def _stage_text(i: int) -> str:
        ops = batches[i]
        lines = "\n".join(f"  {j + 1}. {_describe(op)}" for j, op in enumerate(ops))
        last = i == n_batches - 1
        tail = ("\n\n完成后调用 submit 提交**最终完整账本**。"
                if last else
                f"\n\n完成后调用 submit 提交**当前完整账本**（键和值都要），"
                f"然后等我下发第 {i + 2} 批。不要提前做后面的操作。")
        return f"第 {i + 1} 批操作：\n{lines}{tail}"

    msg1 = (f"我们来维护一份{unit}账本，初始状态：\n\n{init_lines}\n\n"
            f"我会**分批**下发操作，每批做完请提交**完整的最新账本**（不要只给变化的部分），"
            f"然后我会下发下一批。\n\n{_stage_text(0)}")
    messages: list[dict] = [{"role": "user", "content": msg1}]

    def _next_stage(submitted: str, stage_idx: int):
        """模型提交完第 stage_idx 批后，下发下一批。"""
        nxt = stage_idx + 1
        if nxt >= n_batches:
            return None
        return _stage_text(nxt), '{"ledger": {"键": 数值, ...}}（提交当前完整账本）'

    def grader(answer: str, _exp=expected_states[-1], _init=init, **_kw) -> GradeResult:
        obj = extract_json(answer)
        if not isinstance(obj, dict):
            return GradeResult(False, 0.0, {"format": False}, "没给出 JSON 对象", parse_ok=False)
        ledger = obj.get("ledger") if isinstance(obj.get("ledger"), dict) else obj
        got: dict[str, float] = {}
        for k, v in (ledger or {}).items():
            try:
                got[str(k).strip()] = round(float(v), 2)
            except Exception:  # noqa: BLE001
                return GradeResult(False, 0.0, {"format": False}, f"值无法解析：{k}={v!r}", parse_ok=False)
        checks = {
            "same_keys": set(got) == set(_exp),
            "all_values_correct": all(abs(got.get(k, -1e9) - v) < 0.011 for k, v in _exp.items()),
            "not_initial_state": got != _init,
        }
        n_ok = sum(checks.values())
        solved = checks["same_keys"] and checks["all_values_correct"]
        wrong = [k for k, v in _exp.items() if abs(got.get(k, -1e9) - v) >= 0.011]
        if solved:
            reason = "多轮状态维护完全正确"
        elif not checks["same_keys"]:
            missing = sorted(set(_exp) - set(got))[:4]
            extra = sorted(set(got) - set(_exp))[:4]
            reason = f"键集合不对（缺 {missing}，多 {extra}）"
        else:
            detail = "，".join(f"{k}: 期望{v}得{got.get(k)}" for k in wrong[:4])
            reason = f"{len(wrong)} 个键的值错误 —— {detail}"
        return GradeResult(solved, n_ok / len(checks), checks, reason)

    return TaskInstance(
        tid=f"s{seed}", family="longstate", seed=seed,
        messages=messages,
        answer_spec='{"ledger": {"键": 数值, ...}}（每批都要提交完整账本）',
        grader=grader, tools=[], needs_tools=False, next_stage=_next_stage,
        meta={"init": init, "expected_states": expected_states, "final": final,
              "n_batches": n_batches, "n_ops": sum(len(b) for b in batches),
              "unit": unit},
    )
