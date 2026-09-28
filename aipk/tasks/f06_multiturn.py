"""族 6：需求变更跟随（打的是"多轮状态跟踪"）。

设计：先给一版规格，模型产出初稿；然后用户中途改需求（含"作废前面那条"），
要求模型按**最新**规格重做。真实体验里这是最常见的翻车点：
模型会拿旧规格里的字段/口径继续算，或者两版混着来。
判定：最终交付物必须满足 v2 规格，且**不能**留下 v1 独有的痕迹。
"""
from __future__ import annotations

from . import GradeResult, TaskInstance, extract_json, register, rng

SUBJECTS = ["订阅计费", "工单超时", "广告投放", "设备心跳", "课程完成率"]


def _agg(rows: list[tuple[str, float]], key: str, mode: str) -> dict:
    out: dict[str, float] = {}
    for k, v in rows:
        out.setdefault(k, 0.0)
        out[k] = out[k] + v if mode == "sum" else max(out[k], v)
    return {k: round(v, 2) for k, v in sorted(out.items())}


@register("multiturn")
def build(seed: int) -> TaskInstance:
    r = rng(seed * 49979687 + 11)
    subject = r.choice(SUBJECTS)
    dims = ["渠道A", "渠道B", "渠道C", "渠道D"]
    r.shuffle(dims)
    dims = dims[:3]
    rows: list[tuple[str, float]] = []
    for i in range(8):
        rows.append((r.choice(dims), round(r.uniform(10, 400), 2)))
    v1_mode = "sum"
    v2_mode = "max"
    v1 = _agg(rows, "维度", v1_mode)
    v2 = _agg(rows, "维度", v2_mode)

    msg1 = (f"统计{subject}数据。原始数据（维度, 数值）：\n"
            + "\n".join(f"  {k}: {v}" for k, v in rows)
            + f"\n\n请按维度聚合，取**求和**，保留两位小数。\n"
              '只输出 JSON：{"result": {"维度": 数值}}')
    msg2 = ("等一下，需求变了，**请推翻上面那条聚合规则**：\n"
            "现在改为按维度取**最大值**（不是求和），保留两位小数。\n"
            "注意：最终只按最新规则给结果，不要把求和的结果混进来。\n"
            '只输出 JSON：{"result": {"维度": 数值}}')

    def grader(answer: str, _v2=v2, _v1=v1, **_kw) -> GradeResult:
        obj = extract_json(answer)
        if not isinstance(obj, dict) or not isinstance(obj.get("result"), dict):
            return GradeResult(False, 0.0, {"format": False}, "没给出 JSON 结构", parse_ok=False)
        got = obj["result"]
        norm: dict[str, float] = {}
        for k, v in got.items():
            try:
                norm[str(k).strip()] = round(float(v), 2)
            except Exception:  # noqa: BLE001
                return GradeResult(False, 0.0, {"format": False}, f"数值无法解析：{k}={v!r}", parse_ok=False)
        checks = {
            "keys_ok": set(norm) == set(_v2),
            "v2_values_ok": all(abs(norm.get(k, -1) - v) < 0.011 for k, v in _v2.items()),
            "no_v1_residue": not all(abs(norm.get(k, -1) - v) < 0.011 for k, v in _v1.items()),
        }
        solved = checks["keys_ok"] and checks["v2_values_ok"]
        n_ok = sum(checks.values())
        if solved:
            reason = "正确跟随了变更后的规则"
        elif not checks["v2_values_ok"]:
            reason = "仍按旧规则（求和）作答 —— 需求变更未跟随"
        else:
            reason = "维度键不匹配"
        return GradeResult(solved, n_ok / len(checks), checks, reason)

    return TaskInstance(
        tid=f"m{seed}", family="multiturn", seed=seed,
        messages=[{"role": "user", "content": msg1},
                  {"role": "assistant", "content": '{"result": ' + str(v1).replace("'", '"') + '}'},
                  {"role": "user", "content": msg2}],
        answer_spec='{"result": {"维度": 数值}}',
        grader=grader, tools=[], needs_tools=False,
        meta={"v1": v1, "v2": v2, "rows": rows},
    )
