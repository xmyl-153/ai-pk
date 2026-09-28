"""族 4：脏数据转换（打的是"细节注意力"）。

设计：生成一段 CSV，埋 4 类真实脏数据：
  1. 引号内逗号（"北京, 朝阳"）—— 天真 split(",") 必错
  2. 空值与空白值（"" / "   "）—— 必须当缺失
  3. 重复订单号（只保留后出现的那条）—— 去重规则要读清楚
  4. 字段前后空格 —— 必须 strip 后再聚合
判定：参考实现算出精确 JSON，逐字段比对。
"""
from __future__ import annotations

from . import GradeResult, TaskInstance, extract_json, register, rng

CITIES = ['北京, 朝阳', '上海, 浦东', '广州, 天河', '深圳, 南山', '成都, 武侯', '杭州, 西湖']
CHANNELS = ["线上", "线下", "代理"]


def _ref_transform(rows: list[dict]) -> dict:
    """参考实现：dedup 取最后一条，strip，空值跳过。"""
    merged: dict[str, dict] = {}
    for row in rows:
        oid = str(row.get("order_id", "")).strip()
        if not oid:
            continue
        merged[oid] = row  # 后出现覆盖先出现
    totals: dict[str, float] = {}
    counts: dict[str, int] = {}
    missing = 0
    for oid, row in merged.items():
        city = str(row.get("city", "")).strip()
        amt_raw = str(row.get("amount", "")).strip()
        if amt_raw == "" or city == "":
            missing += 1
            continue
        amt = float(amt_raw)
        totals[city] = round(totals.get(city, 0.0) + amt, 2)
        counts[city] = counts.get(city, 0) + 1
    return {
        "rows_kept": len(merged),
        "missing_skipped": missing,
        "total_amount": round(sum(totals.values()), 2),
        "by_city": {k: totals[k] for k in sorted(totals)},
        "count_by_city": {k: counts[k] for k in sorted(counts)},
    }


@register("datatransform")
def build(seed: int) -> TaskInstance:
    r = rng(seed * 15485863 + 29)
    n = r.choice([9, 10, 11, 12])
    rows: list[dict] = []
    used: set[str] = set()
    for i in range(n):
        oid = f"O{1000 + i}"
        city = r.choice(CITIES)
        amount = round(r.uniform(10, 900), 2)
        if r.random() < 0.14:
            amount_s = ""                      # 空金额
        elif r.random() < 0.10:
            amount_s = "   "                   # 空白金额
        else:
            amount_s = f"{amount:.2f}"
        pad = " " if r.random() < 0.35 else ""
        rows.append({"order_id": oid, "city": f"{pad}{city}{pad}", "amount": amount_s})
        used.add(oid)
    # 制造重复订单号（后出现覆盖）
    for _ in range(r.choice([1, 2])):
        src = r.choice(rows)
        dup = {"order_id": src["order_id"],
               "city": r.choice(CITIES),
               "amount": f"{round(r.uniform(50, 500), 2):.2f}"}
        rows.insert(r.randrange(len(rows) + 1), dup)

    def csv_escape(v: str) -> str:
        return f'"{v}"' if ("," in v or '"' in v or v != v.strip()) else v

    csv_lines = ["order_id,city,amount"]
    for row in rows:
        csv_lines.append(",".join(csv_escape(str(row[k])) for k in ("order_id", "city", "amount")))
    csv_text = "\n".join(csv_lines)
    truth = _ref_transform(rows)

    prompt = f"""下面是一段订单 CSV（含引号包裹、空白、重复订单号等脏数据）：

```csv
{csv_text}
```

请按规则清洗并汇总：
1. `order_id` 前后空格去掉后作为唯一键；**同一个 order_id 出现多次时，只保留最后出现的那一条**。
2. `city` 去掉前后空格后作为分组键（注意 city 里本身含有逗号，不要被拆开）。
3. `amount` 去掉前后空格；若为空字符串或纯空白，视为缺失，该行**不计入金额与计数**，但要统计进 missing_skipped。
4. 金额保留两位小数。

只输出 JSON，不要解释：
{{"rows_kept": 去重后行数, "missing_skipped": 缺失行数, "total_amount": 总金额, "by_city": {{"城市": 金额}}, "count_by_city": {{"城市": 行数}}}}"""

    def grader(answer: str, _truth=truth, **_kw) -> GradeResult:
        obj = extract_json(answer)
        if not isinstance(obj, dict):
            return GradeResult(False, 0.0, {"format": False}, "没给出 JSON 对象", parse_ok=False)
        checks: dict[str, bool] = {}

        def near(a, b) -> bool:
            try:
                return abs(float(a) - float(b)) < 0.011
            except Exception:  # noqa: BLE001
                return False

        checks["rows_kept"] = obj.get("rows_kept") == _truth["rows_kept"]
        checks["missing_skipped"] = obj.get("missing_skipped") == _truth["missing_skipped"]
        checks["total_amount"] = near(obj.get("total_amount", -1), _truth["total_amount"])
        got_city = obj.get("by_city") or {}
        checks["by_city"] = (isinstance(got_city, dict)
                             and set(got_city) == set(_truth["by_city"])
                             and all(near(got_city[k], v) for k, v in _truth["by_city"].items()))
        got_cnt = obj.get("count_by_city") or {}
        checks["count_by_city"] = isinstance(got_cnt, dict) and got_cnt == _truth["count_by_city"]
        n_ok = sum(checks.values())
        solved = n_ok == len(checks)
        reward = n_ok / len(checks)
        bad = [k for k, v in checks.items() if not v]
        return GradeResult(solved, reward, checks,
                           "全部字段正确" if solved else f"错字段：{', '.join(bad)}")

    return TaskInstance(
        tid=f"x{seed}", family="datatransform", seed=seed,
        messages=[{"role": "user", "content": prompt}],
        answer_spec='{"rows_kept":…, "missing_skipped":…, "total_amount":…, "by_city":{…}, "count_by_city":{…}}',
        grader=grader, tools=[], needs_tools=False,
        meta={"truth": truth, "n_csv_rows": len(rows)},
    )
