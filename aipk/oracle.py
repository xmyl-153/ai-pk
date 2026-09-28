"""参考答案（oracle）构造：从任务实例的 meta 里取真值，拼成"标准答案"。

两处用它：
  1. `aipk selfcheck` —— 把标准答案喂给 grader，验证"对的不会被判错"
  2. **离线演示 / 无 key 模式** —— `ScriptedProvider` 假装成一个"满分模型"，
     把标准答案按协议 submit 上去，让整条链路（生成→harness→判定→报告）
     在没有 API key、不花一分钱的情况下跑通

为什么值得单独成模块：任务族的真值口径散在各个 fNN_*.py 的 meta 里，
"怎么把 meta 拼成一份合法作答"是**每个族各自的约定**，
自检和演示必须用同一套口径 —— 写两遍必然漂移（本项目已经因为"两边口径不一致"
栽过一次：见 `docs/MEASUREMENT_DEFECTS.md` 第 7 条）。
"""
from __future__ import annotations

import json


def truth_answer(fam: str, inst) -> str | None:
    """从 meta 里取真值，拼成一份"标准答案"。拿不到就返回 None（调用方应跳过）。"""
    m = inst.meta
    if fam == "constraint":
        return json.dumps({"schedule": m["unique_solution"]}, ensure_ascii=False)
    if fam == "toolchain":
        return json.dumps({"total": m["expected"], "files_read": m["reachable"]}, ensure_ascii=False)
    if fam == "datatransform":
        return json.dumps(m["truth"], ensure_ascii=False)
    if fam == "multiturn":
        return json.dumps({"result": m["v2"]}, ensure_ascii=False)
    if fam == "longcontext":
        t = m["truth"]
        return json.dumps({"count": t["count"], "total": t["total"],
                           "excluded_wrong_region": 0, "excluded_void": 0}, ensure_ascii=False)
    if fam == "cascade":
        # 模板现在是"参数→用例"的函数，真值要用同一参数重新生成
        from .tasks.f03_cascade import TEMPLATES
        for tpl in TEMPLATES:
            if tpl["bad"] == m["bad_code"]:
                for d in range(2, 18):
                    cases, trigger = tpl["cases"](d)
                    if repr(cases) in json.dumps(inst.messages, ensure_ascii=False):
                        return json.dumps({"fixed_code": tpl["good"], "trigger_input": trigger,
                                           "bug": tpl["bug_kind"]}, ensure_ascii=False)
        return None
    if fam == "compliance":
        lines = list(m.get("known_solution") or [])
        if not lines:
            return None
        # 让最后一行以要求的尾缀结尾，同时保持"行首两字唯一"和"一个句号"
        tail = m["tail"]
        if not lines[-1].endswith(tail):
            starts = {ln[:2] for ln in lines[:-1]}
            prefix = next((c for c in "规划推进落地执行复盘验收" if c not in starts), "验收")
            lines[-1] = prefix + tail
        return "\n".join(lines)
    if fam == "writing":
        return json.dumps({"wrong_sentence": m["bad"], "why_wrong": "与正确表述相反",
                           "corrected": m["reference_rewrite"]}, ensure_ascii=False)
    if fam == "repofix":
        # 真值答案 = 仓库里那版"正确实现" + 真跑出来的输出值
        return json.dumps({"bug_function": "run", "bug_reason": "边界或初值处理错误",
                           "fixed_transform_py": m["good_transform_py"],
                           "main_output": m["expected_out"]}, ensure_ascii=False)
    if fam == "longstate":
        return json.dumps({"ledger": m["final"]}, ensure_ascii=False)
    if fam == "premise":
        # 真值答案 = 前提判定 + 规范口径算出的费用（undefined 类必须给 null）
        return json.dumps({"premise_ok": m["expect_flag"], "answer": m["expect_answer"],
                           "note": m["expect_note"]}, ensure_ascii=False)
    if fam == "mindiff":
        # 真值答案 = 参考实现（离线通道下当工作目录快照用，real run 走 harness 快照）
        from .tasks.f12_mindiff import truth_payload
        return truth_payload(m)
    if fam == "decay":
        # 真值答案 = 末次探测值 + 自报的首次答案（离线通道下没有真实首答，两者都填真值）
        return json.dumps({"value": m["truth"], "first_value": m["truth"],
                           "note": "该值在整个会话中未变更"}, ensure_ascii=False)
    return None
