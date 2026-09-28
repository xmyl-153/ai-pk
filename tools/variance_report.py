"""输出方差报告：同一道题重复跑，答案到底漂不漂。

为什么不能只看 `consistency`：它是**对错**的标准差，只反映"有时做对有时做错"。
真实使用里还有一种更烦人的漂移：**每次都做对，但答案不一样**（数字口径变了、改的文件变了）。
所以这里同时报三个量：

  - `正确性翻转`：同一题多次运行里，有的对有的错（= 抽卡）
  - `实质漂移`：把答案里的自由文本（note/summary/reason…）去掉后，多次运行的答案仍不同
  - `原样漂移`：连措辞算上，答案文本完全一致的比例（通常很高，只作对照）

用法：
    python tools/variance_report.py runs/<temp0那次>
    python tools/variance_report.py runs/<temp0那次> runs/<temp0.7那次>
"""
from __future__ import annotations

import json
import re
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from aipk.report import FAMILY_LABEL  # noqa: E402
from aipk.runner import load_run  # noqa: E402
from aipk.tasks import extract_json  # noqa: E402

FREE_TEXT_KEYS = {"note", "summary", "reason", "explanation", "comment",
                  "why_wrong", "why", "description", "detail", "explain"}


def fingerprint(text: str) -> tuple[str, str]:
    """答案指纹：结构化的去掉自由文本字段；非结构化的去空白。"""
    obj = extract_json(text or "")
    if isinstance(obj, dict):
        core = {k: v for k, v in obj.items() if str(k).lower() not in FREE_TEXT_KEYS}
        return ("json", json.dumps(core, sort_keys=True, ensure_ascii=False, default=str))
    if isinstance(obj, list):
        return ("json", json.dumps(obj, sort_keys=True, ensure_ascii=False, default=str))
    return ("raw", re.sub(r"\s+", "", text or ""))


def per_model(rs) -> dict:
    """{模型: {族: 指标}} + 全局汇总。"""
    groups: dict[tuple[str, str], list] = defaultdict(list)
    for r in rs:
        if r.infra_failure:
            continue
        groups[(r.model_key, r.task_key)].append(r)

    agg: dict[str, dict[str, list]] = defaultdict(lambda: defaultdict(list))
    for (model, task), items in groups.items():
        fam = task.split(":")[0]
        items = sorted(items, key=lambda x: x.rep)
        solved = [1 if r.solved else 0 for r in items]
        fps = {fingerprint(r.final_answer)[1] for r in items}
        raws = {re.sub(r"\s+", "", r.final_answer or "") for r in items}
        agg[model][fam].append({
            "n": len(items),
            "all_solved": all(solved),
            "flip": len(set(solved)) > 1,
            "subst_drift": len(fps) > 1,
            "raw_drift": len(raws) > 1,
        })

    out: dict[str, dict] = {}
    for model, fams in agg.items():
        row: dict[str, dict] = {}
        for fam, gs in fams.items():
            row[fam] = {
                "groups": len(gs),
                "runs": sum(g["n"] for g in gs),
                "pass_k": round(sum(1 for g in gs if g["all_solved"]) / len(gs), 3),
                "flip_rate": round(sum(1 for g in gs if g["flip"]) / len(gs), 3),
                "subst_drift": round(sum(1 for g in gs if g["subst_drift"]) / len(gs), 3),
                "raw_drift": round(sum(1 for g in gs if g["raw_drift"]) / len(gs), 3),
            }
        allg = [g for gs in fams.values() for g in gs]
        row["__all__"] = {
            "groups": len(allg),
            "runs": sum(g["n"] for g in allg),
            "pass_k": round(sum(1 for g in allg if g["all_solved"]) / len(allg), 3),
            "flip_rate": round(sum(1 for g in allg if g["flip"]) / len(allg), 3),
            "subst_drift": round(sum(1 for g in allg if g["subst_drift"]) / len(allg), 3),
            "raw_drift": round(sum(1 for g in allg if g["raw_drift"]) / len(allg), 3),
        }
        out[model] = row
    return out


def render(label: str, table: dict) -> list[str]:
    L = [f"## {label}", ""]
    fams = sorted({f for row in table.values() for f in row if f != "__all__"})
    L.append("| 模型 | 重复组 | pass^k | 正确性翻转 | 实质漂移 | 原样漂移 |")
    L.append("|---|---|---|---|---|---|")
    for model in sorted(table, key=lambda m: -table[m]["__all__"]["pass_k"]):
        a = table[model]["__all__"]
        L.append(f"| {model.split('/')[-1]} | {a['groups']} | {a['pass_k'] * 100:.0f}% | "
                 f"{a['flip_rate'] * 100:.0f}% | {a['subst_drift'] * 100:.0f}% | "
                 f"{a['raw_drift'] * 100:.0f}% |")
    L.append("")
    for fam in fams:
        rows = [(m, table[m][fam]) for m in sorted(table) if fam in table[m]]
        if not rows:
            continue
        L.append(f"**{FAMILY_LABEL.get(fam, fam)}**：" + "、".join(
            f"{m.split('/')[-1]} pass^k {d['pass_k'] * 100:.0f}% / 实质漂移 {d['subst_drift'] * 100:.0f}%"
            for m, d in rows))
        L.append("")
    return L


def _over_common(table: dict, fams: set[str], key: str) -> dict[str, float]:
    """在**共同任务族**上做加权汇总（两次运行跑的族不一样时不能直接比总行）。"""
    out: dict[str, float] = {}
    for model, row in table.items():
        g = tot = 0
        acc = 0.0
        for fam, d in row.items():
            if fam == "__all__" or fam not in fams:
                continue
            acc += d[key] * d["groups"]
            g += d["groups"]
            tot += d["runs"]
        out[model] = round(acc / g, 3) if g else float("nan")
    return out


def _safe_print(text: str) -> None:
    """Windows 控制台是 GBK，个别符号（⚠ 之类）会直接把脚本打崩 —— 一律降级替换。"""
    enc = getattr(sys.stdout, "encoding", None) or "utf-8"
    try:
        sys.stdout.write(text.encode(enc, "replace").decode(enc, "replace") + "\n")
    except Exception:  # noqa: BLE001
        sys.stdout.write(text.encode("ascii", "replace").decode("ascii") + "\n")


def main(argv: list[str]) -> int:
    args = argv[1:]
    out_path = Path("runs/VARIANCE.md")
    if "--out" in args:
        i = args.index("--out")
        out_path = Path(args[i + 1])
        args = args[:i] + args[i + 2:]
    if not args:
        print(__doc__)
        return 2
    dirs = [Path(a) for a in args]
    tables = []
    L = ["# 输出方差报告（同题重复跑的漂移）", ""]
    for d in dirs:
        rs = load_run(d / "runs.jsonl")
        t = per_model(rs)
        tables.append((d, t))
        L += render(f"{d.name}（{len(rs)} 次运行）", t)
    if len(tables) == 2:
        (da, ta), (db, tb) = tables
        common_models = sorted(set(ta) & set(tb))
        fams_a = {f for m in ta for f in ta[m] if f != "__all__"}
        fams_b = {f for m in tb for f in tb[m] if f != "__all__"}
        common_fams = fams_a & fams_b
        if common_models and common_fams:
            L.append("## 两次运行对照（只算共同任务族：" 
                     + "、".join(FAMILY_LABEL.get(f, f) for f in sorted(common_fams)) + "）")
            L.append("")
            pk_a = _over_common(ta, common_fams, "pass_k")
            pk_b = _over_common(tb, common_fams, "pass_k")
            sd_a = _over_common(ta, common_fams, "subst_drift")
            sd_b = _over_common(tb, common_fams, "subst_drift")
            fl_a = _over_common(ta, common_fams, "flip_rate")
            fl_b = _over_common(tb, common_fams, "flip_rate")
            L.append(f"| 模型 | A pass^k | B pass^k | A 正确性翻转 | B 正确性翻转 "
                     f"| A 实质漂移 | B 实质漂移 |")
            L.append("|---|---|---|---|---|---|---|")
            for m in common_models:
                L.append(f"| {m.split('/')[-1]} | {pk_a[m] * 100:.0f}% | {pk_b[m] * 100:.0f}% "
                         f"| {fl_a[m] * 100:.0f}% | {fl_b[m] * 100:.0f}% "
                         f"| {sd_a[m] * 100:.0f}% | {sd_b[m] * 100:.0f}% |")
            L.append("")
            L.append(f"- A = `{da}`　B = `{db}`")
            L.append("")
    L.append("## 怎么读")
    L.append("")
    L.append("- `pass^k`：同一题 k 次**全部**成功的比例 —— 抽卡感的核心指标。")
    L.append("- `实质漂移`：去掉自由文本字段后答案仍不同 —— 例如同一题两次给出不同的费用数字。")
    L.append("- `原样漂移`：连措辞都不同（通常远高于实质漂移，说明模型只是话不一样，口径没变）。")
    L.append("- 注意：本机网关在 `temperature=0` 下**也不完全确定**"
             "（实测 qwen3.8-flash 6 次调用出 3 种答案），所以两次运行的可比性建立在同一配置上，"
             "不要拿单次结果当「模型稳定」的证据。")
    md = "\n".join(L)
    out_path.write_text(md, encoding="utf-8")
    _safe_print(md)
    _safe_print(f"\n已写入 {out_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
