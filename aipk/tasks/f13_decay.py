"""族 13：长会话一致性（打的是"同一件事问了两次，答案自己就变了"）。

为什么加这一族：
`longstate` 族测的是**状态维护**（多批操作后账本对不对），`longcontext` 测的是**长文里
的数读没读进去**。这两族都不测真实使用里最让人抓狂的一件事：
**上下文一长，模型对同一个事实的表述就前后不一致** —— 你可能在第 2 轮拿到过正确答案，
第 9 轮再问一次，它给了另一个值，而且它自己都没发现。

设计（真多轮，走 harness 的 next_stage 机制，不是把 50 轮一次性塞进 prompt）：
  1. 开场：给出项目 A 的档案（6 个字段），并**立刻问一次**主库地址 → 记为「首答」；
  2. 中间：K-1 个真实小任务（统计/排序/求和/换算/去重），其中**必含一轮**给出
     项目 B 的档案（字段同名、值不同），并要求报出 **B 的主库地址** → 制造"串台"引信；
  3. 结尾：再问一次**同一个问题**，并要求它同时报出"你第一次回答这个问题时给的值"。

判定（全部机械可判，不碰裁判）：
  - `probe1_correct` 首答是否正确（这一族里首答几乎必对，所以它是个基线）；
  - `probe2_correct` 末答是否正确（**衰减信号**：首答对、末答错 = 长会话里被串台/漂移）；
  - `no_drift`      末答与它自己首答是否一致（一致性，不看对错）；
  - `self_report_true` 自报的"首答"是否是它**真实说过**的那个值（防事后美化）；
  - `fillers_ok`    中间那些小任务做没做（诊断用，不参与 solved —— 中间任务全丢说明
                    它后半程已经放弃了，那样的"一致"没有意义）。

注意：一致性**不等于**正确性。这一族报的是"同一个事实被问两次的漂移率"，
和正确率分开看 —— 这正是正确率饱和之后还剩的区分度。
"""
from __future__ import annotations

import json
import re

from . import GradeResult, TaskInstance, extract_json, register, rng

FIELDS = ["主库地址", "备份库地址", "单价（元/千次）", "生效月份", "值班人"]
PROBE_FIELD = "主库地址"

NAMES = ["陈默", "周叙", "林昭", "许砚", "何谦", "宋屿", "钟遥", "沈泊"]
CODENAMES = ["星槎", "白鹭", "青隼", "赤苇", "玄圃", "疏影", "长风", "照野"]
CITIES = ["bj", "sh", "gz", "hz", "cd", "wh"]

TEXT_POOL = [
    "这个季度的扩容评审", "按照最新的容量规划", "把峰值时段的调用量", "折算成等效负载之后",
    "再和上个月的数据对比", "发现主库的写入压力", "主要来自批量导入任务",
    "如果不在导入前做限流", "那么每天的晚高峰", "依然会出现排队等待",
    "需要在下个版本里", "把限流策略下沉到网关", "并且保留手工放行的开关",
]


# ---------------------------------------------------------------- 归一化


def _norm(v) -> str:
    """把答案归一成可比字符串：数字按数值比，文本去空白、统一小写。"""
    if v is None:
        return ""
    if isinstance(v, bool):
        return str(v)
    if isinstance(v, (int, float)):
        f = float(v)
        return str(int(f)) if abs(f - round(f)) < 1e-9 else f"{f:.6f}".rstrip("0").rstrip(".")
    s = str(v).strip()
    try:
        return _norm(float(s))
    except (TypeError, ValueError):
        return re.sub(r"\s+", "", s).lower()


def _val_of(raw: str):
    """从一次提交里挖出 value 字段（兼容裸字符串提交）。"""
    if raw is None:
        return None
    obj = extract_json(raw)
    if isinstance(obj, dict) and "value" in obj:
        return obj.get("value")
    if isinstance(obj, dict) and "answer" in obj:
        return obj.get("answer")
    return raw.strip() if isinstance(raw, str) else None


# ---------------------------------------------------------------- 中间小任务


def _filler_stats(r) -> dict:
    n = r.randint(14, 22)
    text = "，".join(r.choice(TEXT_POOL) for _ in range(n)) + "。"
    cnt = text.count("的")
    return {"text": f"下面这段文字里一共出现了多少个「的」字？\n\n{text}",
            "spec": '{"answer": 出现次数（整数）}', "truth": str(cnt),
            "tag": "字数统计"}


def _filler_sort(r) -> dict:
    ids = [f"W-{r.randint(1000, 9999)}" for _ in range(7)]
    want = ",".join(sorted(ids))
    return {"text": "把下面这些工单号按字典序从小到大排序，用英文逗号连接，不要空格：\n\n"
                    + "、".join(ids),
            "spec": '{"answer": "排序后的字符串"}', "truth": want, "tag": "排序"}


def _filler_sum(r) -> dict:
    nums = [r.randint(3, 120) for _ in range(5)]
    seg = ["第 %d 号工位" % nums[0], "%d 箱物料" % nums[1], "%d 托盘" % nums[2],
           "%d 件成品" % nums[3], "%d 张单据" % nums[4]]
    r.shuffle(seg)
    text = "本批到货：" + "，".join(seg) + "。请把这句话里出现的所有整数相加。"
    return {"text": text, "spec": '{"answer": 合计值（整数）}',
            "truth": str(sum(nums)), "tag": "提取求和"}


def _filler_convert(r) -> dict:
    vals = [r.randint(700, 9000) for _ in range(3)]
    want = ",".join(f"{v / 1024:.2f}" for v in vals)
    return {"text": "把下面三个值从 KB 换算成 MB（按 1024 进制），各保留两位小数，"
                    "用英文逗号连接：\n\n" + "、".join(f"{v} KB" for v in vals),
            "spec": '{"answer": "换算结果字符串"}', "truth": want, "tag": "单位换算"}


def _filler_unique(r) -> dict:
    pool = [f"{r.choice(CITIES)}{r.randint(1, 9)}.internal" for _ in range(5)]
    items = [r.choice(pool) for _ in range(9)]
    want = str(len(set(items)))
    return {"text": "下面这个列表里有几个**不重复**的域名？\n\n" + "、".join(items),
            "spec": '{"answer": 不重复个数（整数）}', "truth": want, "tag": "去重计数"}


def _filler_checksum(r) -> dict:
    nums = [r.randint(100, 999) for _ in range(4)]
    want = str(sum(nums) % 97)
    return {"text": "把下面四个数相加，然后对 97 取余，给出余数：\n\n"
                    + "、".join(str(x) for x in nums),
            "spec": '{"answer": 余数（整数）}', "truth": want, "tag": "校验和"}


FILLERS = [_filler_stats, _filler_sort, _filler_sum,
           _filler_convert, _filler_unique, _filler_checksum]


def _mk_project(r, codename: str, name: str) -> dict:
    return {
        "name": name,
        "codename": codename,
        "主库地址": f"db-{r.choice(CITIES)}{r.randint(1, 9)}.{codename}.internal:{r.choice([5432, 5433, 6432])}",
        "备份库地址": f"bak-{r.choice(CITIES)}{r.randint(1, 9)}.{codename}.internal:{r.choice([5432, 5433])}",
        "单价（元/千次）": round(r.uniform(4.0, 19.0), 1),
        "生效月份": f"2026-{r.randint(2, 9):02d}",
        "值班人": r.choice(NAMES),
    }


def _sheet(p: dict) -> str:
    lines = [f"  {k} = {p[k]}" for k in FIELDS]
    return f"【{p['name']}项目档案】（代号 {p['codename']}）\n" + "\n".join(lines)


@register("decay")
def build(seed: int) -> TaskInstance:
    r = rng(seed * 2246822519 + 313)
    names = r.sample(CODENAMES, 2)
    a = _mk_project(r, names[0], "甲")
    b = _mk_project(r, names[1], "乙")
    # 引信保险：两个项目在探测字段上必须真的不同，否则串台也看不出来
    if a[PROBE_FIELD] == b[PROBE_FIELD]:
        b[PROBE_FIELD] = b[PROBE_FIELD].replace(":5432", ":5433")
    if a["值班人"] == b["值班人"]:
        b["值班人"] = r.choice([n for n in NAMES if n != a["值班人"]])

    n_filler = r.choice([6, 7, 8])
    pool = [f for f in FILLERS]
    r.shuffle(pool)
    fillers = [f(r) for f in pool[:max(0, n_filler - 1)]]

    # 必含的"串台引信"：报出 B 的主库地址与值班人（值不同，字段同名）
    bait = {"text": "换一份档案 —— 这是我们另一个项目的：\n\n"
                    + _sheet(b)
                    + f"\n\n请把 **乙项目（代号 {b['codename']}）** 的「{PROBE_FIELD}」和「值班人」"
                      f"报给我，两项都要。",
            "spec": '{"answer": ["主库地址", "值班人"]}', "tag": "串台引信",
            "truth": json.dumps([b[PROBE_FIELD], b["值班人"]], ensure_ascii=False)}
    pos = r.randint(1, max(1, len(fillers)))
    fillers.insert(pos, bait)

    state: dict = {"answers": {}}
    truth = str(a[PROBE_FIELD])
    probe_q = f"再确认一次：**甲项目（代号 {a['codename']}）** 的「{PROBE_FIELD}」是什么？"

    stage0 = (_sheet(a)
              + "\n\n我们先对齐一下信息。\n\n"
              + f"问题：甲项目（代号 {a['codename']}）的「{PROBE_FIELD}」是什么？")
    spec0 = '{"value": "主库地址原文"}'
    final_stage = (probe_q + "\n\n另外，请你同时报出你**第一次**回答这个问题时给出的那个值"
                             "（不是你现在的推测，而是你前面真实提交过的）。")
    spec_final = ('{"value": "这次的主库地址", '
                  '"first_value": "你第一次回答这个问题时给出的值", "note": "一句话"}')

    def _stage_text(i: int) -> tuple[str, str]:
        """第 i 个中间任务（i 从 1 开始）。"""
        f = fillers[i - 1]
        return f["text"], f["spec"]

    def _next_stage(submitted: str, stage_idx: int):
        state["answers"][stage_idx] = submitted
        state["max_stage"] = max(state.get("max_stage", -1), stage_idx)
        if stage_idx == 0:
            return _stage_text(1)
        if stage_idx < len(fillers):
            return _stage_text(stage_idx + 1)
        if stage_idx == len(fillers):
            return final_stage, spec_final
        return None

    def grader(answer: str, _truth=truth, **_kw) -> GradeResult:
        n_stages = len(fillers) + 2
        has_session = bool(state["answers"])
        max_stage = state.get("max_stage", -1)
        reached = (not has_session) or max_stage >= len(fillers)
        obj = extract_json(answer)
        if not isinstance(obj, dict):
            # 这里要分清两种"没给出 JSON"：
            #   ① 会话根本没走到末次探测（模型在长会话中途停止调 submit / 回复为空）——
            #      这是**长会话可靠性**问题，不是格式问题；
            #   ② 走到了、但答案不是 JSON。
            # 实测踩过：seed-2.1-pro 在某个实例上 3/3 都在第 2~4 阶段停住、最后一条回复为空，
            # 旧代码只回一句"没给出 JSON 对象"，看起来像格式问题，把真实信号埋掉了。
            if has_session and not reached:
                return GradeResult(
                    False, 0.0,
                    {"format": False, "reached_final_stage": False},
                    f"会话在第 {max_stage + 2}/{n_stages} 阶段就停了，"
                    f"最后一条回复没有内容也没有 submit —— 长会话里协议没走完，"
                    f"不是答案格式问题（也不是漂移）", parse_ok=False)
            return GradeResult(False, 0.0, {"format": False, "reached_final_stage": True},
                               "走到了末次探测，但答案不是 JSON 对象", parse_ok=False)
        checks: dict[str, bool] = {}
        got = _norm(obj.get("value"))
        fv = _norm(obj.get("first_value"))
        checks["probe2_correct"] = got == _norm(_truth)
        checks["note_given"] = len(str(obj.get("note") or "")) >= 4

        first_raw = state["answers"].get(0)
        if first_raw is None:
            # selfcheck 的离线通道（没有真实会话）—— 只校验答案自洽
            checks["probe1_correct"] = fv == _norm(_truth)
            checks["no_drift"] = checks["probe2_correct"]
            checks["self_report_true"] = fv == _norm(_truth)
        else:
            first_val = _norm(_val_of(first_raw))
            checks["probe1_correct"] = first_val == _norm(_truth)
            checks["no_drift"] = bool(first_val) and got == first_val
            checks["self_report_true"] = bool(first_val) and fv == first_val

        # 中间任务做没做（诊断项）
        ok_fillers = 0
        for i, f in enumerate(fillers, 1):
            sub = state["answers"].get(i)
            if sub is None:
                continue
            got_f = extract_json(sub)
            raw_f = got_f.get("answer") if isinstance(got_f, dict) else got_f
            if _norm(raw_f) == _norm(f["truth"]) or _norm(json.dumps(raw_f, ensure_ascii=False)) == _norm(f["truth"]):
                ok_fillers += 1
        checks["fillers_ok"] = ok_fillers == len(fillers)
        # 会话是否真走到了末次探测：被轮数上限截断的会话不能算"模型漂移"
        checks["reached_final_stage"] = reached

        solved = (checks["probe1_correct"] and checks["probe2_correct"]
                  and checks["no_drift"] and checks["self_report_true"]
                  and checks["reached_final_stage"])
        reward = sum(checks.values()) / len(checks)

        if not checks["reached_final_stage"]:
            reason = (f"会话被轮数上限截断（只走到第 {state.get('max_stage', -1) + 1} 阶段，"
                      f"共需 {len(fillers) + 2} 阶段）—— 这不是模型答错，请放宽 --max-turns 重跑")
        elif solved:
            reason = f"首答与末答一致且正确（{_truth}），自报历史属实"
        elif not checks["probe2_correct"] and checks["probe1_correct"]:
            reason = (f"**长会话漂移**：首答正确（{_truth}），末答变成 {obj.get('value')!r}")
        elif not checks["probe1_correct"] and checks["probe2_correct"]:
            reason = f"末答正确，但首答就给错了（首答 {_val_of(state['answers'].get(0))!r}）"
        elif not checks["probe2_correct"]:
            reason = f"两次都没答对：应为 {_truth}，末答 {obj.get('value')!r}"
        elif not checks["no_drift"]:
            reason = f"首答与末答不一致（{_val_of(state['answers'].get(0))!r} vs {obj.get('value')!r}）"
        else:
            reason = (f"值都对，但自报的首答不实：它真实说过 "
                      f"{_val_of(state['answers'].get(0))!r}，却报成 {obj.get('first_value')!r}")
        if not checks["fillers_ok"]:
            reason += f"；中间 {len(fillers)} 个任务只做对 {ok_fillers} 个"
        return GradeResult(solved, reward, checks, reason)

    return TaskInstance(
        tid=f"d{seed}", family="decay", seed=seed,
        messages=[{"role": "user", "content": stage0}],
        answer_spec=spec0,
        grader=grader, tools=[], needs_tools=False, next_stage=_next_stage,
        meta={"truth": truth, "probe_field": PROBE_FIELD, "project_a": a, "project_b": b,
              "n_stages": 1 + len(fillers) + 1, "fillers": fillers,
              "filler_truths": [f["truth"] for f in fillers],
              "a_address": a[PROBE_FIELD], "b_address": b[PROBE_FIELD]},
    )