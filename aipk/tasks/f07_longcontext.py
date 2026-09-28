"""族 7：长文规则应用（打的是"真读进去了，还是假装读了"）。

不是简单大海捞针——要求把一条规则**应用到长文里的一批记录**上。
长文里埋了四类干扰：相似但不满足条件的记录、已作废记录、口径不同的相邻章节、
以及一条"只适用于特定区域"的限定。答案必须精确。
"""
from __future__ import annotations

from . import GradeResult, TaskInstance, extract_json, register, rng

REGIONS = ["华东", "华南", "华北", "西南", "东北", "西北"]
ITEMS = ["网关", "存储", "调度", "索引", "消息", "鉴权", "计费", "报表", "风控", "埋点"]


@register("longcontext")
def build(seed: int) -> TaskInstance:
    r = rng(seed * 86028121 + 17)
    regions = r.sample(REGIONS, 4)
    target_region = regions[0]
    items = r.sample(ITEMS, 6)
    n_sections = r.choice([7, 8, 9])
    n_records = r.choice([22, 26, 30])

    # 生成记录：字段 = (序号, 区域, 组件, 数值, 状态)
    records: list[tuple[int, str, str, float, str]] = []
    for i in range(n_records):
        reg = r.choice(regions)
        item = r.choice(items)
        val = round(r.uniform(10, 990), 2)
        status = "生效" if r.random() < 0.78 else "作废"
        records.append((i + 1, reg, item, val, status))

    # 正确口径：区域 == target_region 且 状态 == 生效 的记录，数值求和 + 条数
    keep = [rec for rec in records if rec[1] == target_region and rec[4] == "生效"]
    truth_sum = round(sum(rec[3] for rec in keep), 2)
    truth_cnt = len(keep)

    # 把记录打散进若干章节，章节里塞入其他区域的记录当干扰
    buckets: list[list[tuple]] = [[] for _ in range(n_sections)]
    for idx, rec in enumerate(records):
        buckets[idx % n_sections].append(rec)

    doc: list[str] = ["# 运维台账（摘录）", ""]
    doc += ["说明：本台账按批次记录，部分批次已作废，作废批次不参与任何统计。", ""]
    for si, bucket in enumerate(buckets):
        doc.append(f"## 批次 {si + 1:02d}")
        doc.append("")
        doc.append("| 序号 | 区域 | 组件 | 数值 | 状态 |")
        doc.append("| --- | --- | --- | --- | --- |")
        for rec in sorted(bucket):
            doc.append(f"| {rec[0]} | {rec[1]} | {rec[2]} | {rec[3]:.2f} | {rec[4]} |")
        doc.append("")
        if si == n_sections // 2:
            doc.append("> 附注：以下附录为历史归档，**不属于台账正文，不参与统计**。")
            doc.append("")
            doc.append("| 序号 | 区域 | 组件 | 数值 | 状态 |")
            doc.append("| --- | --- | --- | --- | --- |")
            for j in range(4):
                doc.append(f"| A{j + 1} | {target_region} | {r.choice(items)} | {round(r.uniform(10, 990), 2):.2f} | 生效 |")
            doc.append("")
    doc += ["", "（台账结束）"]
    document = "\n".join(doc)

    prompt = f"""下面是一份运维台账摘录。请仔细阅读后回答统计问题。

{document}

---
统计规则：
- 只统计**台账正文表格**里 `区域` 等于「{target_region}」**且** `状态` 等于「生效」的记录。
- 「附注/附录」部分的表格不是台账正文，**一律不计入**。
- 作废记录不计入。

只输出 JSON，不要解释：
{{"count": 记录条数, "total": 数值总和（保留两位小数）, "excluded_wrong_region": 正文中被排除的区域不匹配记录数, "excluded_void": 正文中被排除的作废记录数}}"""

    def grader(answer: str, _sum=truth_sum, _cnt=truth_cnt, _recs=records,
               _region=target_region, **_kw) -> GradeResult:
        obj = extract_json(answer)
        if not isinstance(obj, dict):
            return GradeResult(False, 0.0, {"format": False}, "没给出 JSON 对象", parse_ok=False)

        def near(a, b) -> bool:
            try:
                return abs(float(a) - float(b)) < 0.011
            except Exception:  # noqa: BLE001
                return False

        body = [x for x in _recs]  # 正文记录（附录是另造的，不在其中）
        wrong_region = sum(1 for x in body if x[1] != _region)
        void = sum(1 for x in body if x[1] == _region and x[4] == "作废")
        checks = {
            "count_ok": obj.get("count") == _cnt,
            "total_ok": near(obj.get("total", -1), _sum),
            "excluded_wrong_region_ok": obj.get("excluded_wrong_region") == wrong_region,
            "excluded_void_ok": obj.get("excluded_void") == void,
        }
        n_ok = sum(checks.values())
        solved = checks["count_ok"] and checks["total_ok"]
        bad = [k for k, v in checks.items() if not v]
        return GradeResult(solved, n_ok / len(checks), checks,
                           "统计正确" if solved else f"错误字段：{', '.join(bad)}（应为 count={_cnt}, total={_sum}）")

    return TaskInstance(
        tid=f"l{seed}", family="longcontext", seed=seed,
        messages=[{"role": "user", "content": prompt}],
        answer_spec='{"count":…, "total":…, "excluded_wrong_region":…, "excluded_void":…}',
        grader=grader, tools=[], needs_tools=False,
        meta={"truth": {"count": truth_cnt, "total": truth_sum},
              "target_region": target_region, "doc_chars": len(document)},
    )
