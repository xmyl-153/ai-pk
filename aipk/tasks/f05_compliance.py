"""族 5：硬格式合规（打的是"听不听话"，而不是"聪不聪明"）。

这是真实体验里最要命的一环：模型很聪明，但让它"别加解释""每行不超过 N 字"
就是做不到。约束是程序化生成的，逐条机械验证。

可解性保证：题面里要求的词全部来自**预先生成的候选句**，候选句按构造满足
「行首两字互不相同」「每句一个句号」等约束，因此一定存在合法解（不是无解题）。
"""
from __future__ import annotations

import re

from . import GradeResult, TaskInstance, register, rng

TOPICS = ["为什么代码评审要限制单次改动量", "为什么日志要结构化",
          "为什么接口要幂等", "为什么缓存要设过期时间", "为什么要区分读写路径"]
REQUIRED_TAIL = ["以上。", "汇报完毕。", "请确认。", "结论如上。"]
FORBIDDEN = ["总之", "值得注意的是", "首先", "其次", "最后", "综上", "换言之", "换句话说"]
MUST_POOL = ["上游", "下游", "回滚", "可观测", "幂等", "灰度", "熔断", "限流", "重放", "采样"]

# 候选句：每个句子的前两字唯一，句尾恰好一个句号
SENTENCE_BANK = [
    "改动量小，评审者才能逐行看懂并发现边界问题。",
    "边界条件往往藏在细节里，改动越大越容易漏看。",
    "日志结构化之后，检索与告警才能按字段精确匹配。",
    "字段缺失时应当显式留空，而不是靠人猜含义。",
    "接口幂等能让重试变安全，网络抖动不再造成重复扣款。",
    "重试策略必须配合唯一请求标识，否则幂等无从谈起。",
    "缓存过期时间要按数据变更频率设定，过短等于没缓存。",
    "过期策略与容量淘汰要一起考虑，避免集中失效。",
    "读写路径分离后，可以各自选择更合适的存储与索引。",
    "只读副本承担查询压力，主库专注写入稳定性。",
    "灰度发布先放小流量，确认无异常再逐步放大。",
    "回滚预案要在上线前演练，否则故障时来不及。",
    "熔断保护的是调用方，避免被下游拖垮。",
    "限流按租户维度设置，可以防止单点打满全局。",
    "可观测不只是看板，而是能回答为什么变慢了。",
    "采样比例过高会掩盖长尾，过低又丢失现场。",
    "上游变更要提前通知，下游才有时间适配。",
    "下游超时设置必须短于上游，否则重试会放大压力。",
    "重放历史请求时，要确认副作用已经隔离。",
    "容量评估需要压测数据支撑，不能凭感觉拍数。",
]


def _pick_sentences(r, max_len: int, banned: list[str], need_words: list[str], n: int) -> list[str]:
    """挑 n 句：满足长度、无禁用词、覆盖必现词、行首两字互不相同。"""
    pool = [s for s in SENTENCE_BANK if len(s) <= max_len and not any(b in s for b in banned)]
    r.shuffle(pool)
    chosen: list[str] = []
    seen_starts: set[str] = set()
    covered: set[str] = set()
    # 先满足必现词
    for w in need_words:
        for s in pool:
            if w in s and s[:2] not in seen_starts and s not in chosen:
                chosen.append(s)
                seen_starts.add(s[:2])
                covered.update(x for x in need_words if x in s)
                break
    for s in pool:
        if len(chosen) >= n:
            break
        if s[:2] in seen_starts or s in chosen:
            continue
        chosen.append(s)
        seen_starts.add(s[:2])
    return chosen[:n]


@register("compliance")
def build(seed: int) -> TaskInstance:
    for attempt in range(60):
        r = rng(seed * 32452843 + 3 + attempt)
        topic = r.choice(TOPICS)
        max_len = r.choice([34, 38, 42])
        min_lines = r.choice([5, 6, 7])
        max_lines = min_lines + r.choice([0, 1, 2])
        tail = r.choice(REQUIRED_TAIL)
        banned = r.sample(FORBIDDEN, r.choice([2, 3, 3, 4]))
        must_words = r.sample(MUST_POOL, r.choice([2, 3]))
        n_lines = r.randint(min_lines, max_lines)
        solvable = _pick_sentences(r, max_len, banned, must_words, n_lines)
        if len(solvable) < min_lines or not all(w in "".join(solvable) for w in must_words):
            continue

        rules = [
            f"1. 正文**每一行**的字符数不得超过 {max_len} 个字符（按 python `len(line)` 计，不含换行符）。",
            f"2. 正文行数必须在 {min_lines} 到 {max_lines} 行之间（含两端）。",
            "3. 每一行**恰好一个句子**，行尾必须是中文句号「。」。",
            "4. 每一行**不得以相同的两个字开头**（任意两行的前两个字不能相同）。",
            f"5. 全文中**禁止出现**这些词：{'、'.join(banned)}。",
            f"6. 必须出现这些词：{'、'.join(must_words)}（每个至少一次）。",
            f"7. 全文**最后一行必须以「{tail}」结尾**。",
            "8. 不要使用任何 Markdown 标记（不要 #、*、-、`、>、数字序号）。",
            "9. 不要输出任何解释、前言、结语或标题，只输出正文本身。",
        ]
        prompt = (f"写一段关于「{topic}」的短评，严格遵守下列全部约束：\n\n"
                  + "\n".join(rules) + "\n\n直接输出正文，不要输出任何其他内容。")

        def grader(answer: str, _max=max_len, _minl=min_lines, _maxl=max_lines,
                   _tail=tail, _banned=banned, _must=must_words, **_kw) -> GradeResult:
            raw = (answer or "").strip()
            raw = re.sub(r"^```[a-zA-Z]*\n?", "", raw)
            raw = re.sub(r"\n?```$", "", raw).strip()
            lines = [ln for ln in raw.split("\n") if ln.strip() != ""]
            checks: dict[str, bool] = {}
            checks["line_count"] = _minl <= len(lines) <= _maxl
            checks["line_length"] = all(len(ln) <= _max for ln in lines) if lines else False
            checks["one_sentence_per_line"] = all(
                ln.rstrip().endswith("。") and ln.count("。") == 1 for ln in lines
            ) if lines else False
            starts = [ln.strip()[:2] for ln in lines]
            checks["distinct_line_starts"] = len(set(starts)) == len(starts) if lines else False
            checks["no_banned_words"] = not any(b in raw for b in _banned)
            checks["has_required_words"] = all(w in raw for w in _must)
            checks["tail_ok"] = bool(lines) and lines[-1].rstrip().endswith(_tail)
            checks["no_markdown"] = not re.search(r"[#*`>\-]|\d+[.、]", raw)
            n_ok = sum(checks.values())
            solved = n_ok == len(checks)
            bad = [k for k, v in checks.items() if not v]
            detail = ""
            if lines and not checks["line_length"]:
                worst = max(lines, key=len)
                detail = f"最长行 {len(worst)} 字（上限 {_max}）：{worst[:30]}…"
            elif not checks["line_count"]:
                detail = f"行数 {len(lines)}，要求 {_minl}-{_maxl}"
            elif bad:
                detail = "违反：" + ", ".join(bad)
            return GradeResult(solved, n_ok / len(checks), checks,
                               "全部约束满足" if solved else (detail or "违反：" + ", ".join(bad)))

        return TaskInstance(
            tid=f"f{seed}", family="compliance", seed=seed,
            messages=[{"role": "user", "content": prompt}],
            answer_spec="纯文本正文，无 Markdown",
            grader=grader, tools=[], needs_tools=False,
            meta={"max_len": max_len, "min_lines": min_lines, "max_lines": max_lines,
                  "banned": banned, "must_words": must_words, "tail": tail,
                  "known_solution": solvable},
        )
    raise RuntimeError(f"compliance seed={seed} 生成不出可解实例")
