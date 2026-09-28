"""族 8：找错并改写（打的是"表达能力 + 判断力"，这条走盲评）。

设计：程序化生成一段"看起来很像但对某个关键点说错了"的技术解释，
要求模型 ① 指出错在哪 ② 重写成正确版本。
- ① 用代码判定（必须命中被注入的那个错误点）
- ② 走**盲评**：裁判不知道哪段是谁写的，且交换 A/B 位置评两遍去偏
这样既有确定性的锚点，又能测主观质量 —— 而且能反过来审计裁判本身。
"""
from __future__ import annotations

import re

from . import GradeResult, TaskInstance, register, rng

# (正确陈述, 错误陈述, 错误点关键词, 展开后的参考改写)
FLAWS = [
    ("LRU 淘汰的是最久未被访问的页。", "LRU 淘汰的是最近最少**写入**的页。",
     ["访问", "读取", "读"], "LRU 按最近一次访问的时间排序，淘汰最久没被访问的页；读写都会刷新它的新鲜度，所以不是看谁最少被写入。"),
    ("缓存命中率高意味着平均访存时间下降。", "缓存命中率高意味着单次访存延迟上升。",
     ["下降", "降低", "减少", "更快"], "命中率越高，越多请求在快层拿到数据，平均访存时间随之下降；变慢的是未命中的那部分请求，不是整体。"),
    ("进程切换比线程切换开销大，因为要换页表。", "线程切换比进程切换开销大，因为线程要换页表。",
     ["进程", "页表"], "线程共享同一地址空间，切换时不用换页表；进程切换要换页表并刷新 TLB，所以开销更大。"),
    ("幂等意味着同一请求重复执行的结果与执行一次相同。", "幂等意味着同一请求只会被执行一次。",
     ["多次", "重复", "结果相同", "相同结果"], "幂等不阻止重复执行，它保证重复执行后的最终状态与只执行一次相同，因此重试是安全的。"),
    ("TCP 慢启动阶段拥塞窗口按指数增长。", "TCP 慢启动阶段拥塞窗口按线性增长。",
     ["指数", "翻倍"], "慢启动阶段每收到一个确认就把拥塞窗口翻倍，增长是指数的；线性增长属于拥塞避免阶段。"),
    ("写时复制让 fork 后的父子进程先共享物理页。", "写时复制让 fork 时立刻复制全部物理页。",
     ["共享", "不复制", "延迟复制", "lazy"], "fork 时不复制物理页，父子进程先共享同一份只读映射，等某一方真正写入时才复制那一页。"),
    ("哈希表负载因子升高会加剧冲突，退化查询性能。", "哈希表负载因子升高会减少冲突，提升查询性能。",
     ["冲突", "退化", "变慢", "性能下降"], "负载因子升高意味着桶更拥挤，冲突变多、链更长，查询从接近常数退化为接近线性。"),
    ("算术强度低的任务更容易受内存带宽限制。", "算术强度低的任务更容易受算力限制。",
     ["带宽", "访存", "内存"], "算术强度低说明每搬一个字节做的运算少，瓶颈在内存带宽而不是算力，这类任务属于访存受限。"),
]

CONTEXTS = [
    "给一位刚学完 C 语言的大二学生讲清楚",
    "写进团队内部 wiki，读者是有三年经验的工程师",
    "给一位转行做后端的运营同事解释",
    "作为技术分享的开场三句话",
]


@register("writing")
def build(seed: int) -> TaskInstance:
    r = rng(seed * 999983 + 23)
    good, bad, keywords, ref_rewrite = r.choice(FLAWS)
    ctx = r.choice(CONTEXTS)
    n_filler = r.choice([3, 4])
    fillers = r.sample([f for f in FLAWS if f[0] != good], n_filler)

    para: list[str] = [f"关于这个问题，我的理解是这样的。"]
    for i, f in enumerate(fillers):
        para.append(f"第 {i + 1} 点，{f[0]}")
    # 把错误点插在中间
    insert_at = r.randrange(1, len(para) + 1)
    para.insert(insert_at, f"第 {insert_at} 点，{bad}")
    para.append("以上就是我的理解，欢迎指正。")
    draft = "".join(para)

    prompt = f"""下面这段技术说明里，**有且只有一处**关键错误。请找出来并改写。

原文：
{draft}

要求：
1. 明确指出错在哪一句、错在什么地方（一句话）。
2. 针对{ctx}，重写一段**正确**的说明，覆盖原文全部要点，控制在 120 字以内。
3. 不要提"原文""上面""错误"这类元话语，直接给出正确的说明。

只输出 JSON：
{{"wrong_sentence": "出错的原文句子", "why_wrong": "错在哪", "corrected": "重写后的正确说明"}}"""

    def grader(answer: str, _kw=keywords, _bad=bad, _good=good, **_kw2) -> GradeResult:
        from . import extract_json
        obj = extract_json(answer)
        if not isinstance(obj, dict):
            return GradeResult(False, 0.0, {"format": False}, "没给出 JSON 对象", parse_ok=False)
        ws = str(obj.get("wrong_sentence") or "")
        why = str(obj.get("why_wrong") or "")
        corr = str(obj.get("corrected") or "")
        bad_plain = _bad.replace("**", "")
        ws_plain = ws.replace("**", "").replace(" ", "")
        # 命中判定：复述了错误句的关键部分，或者用语义关键词点出了错在哪
        core = bad_plain[:10].replace(" ", "")
        hit_sentence = (core and core in ws_plain) or (ws_plain and ws_plain in bad_plain.replace(" ", ""))
        hit_keyword = any(k in (why + corr + ws) for k in _kw)
        checks = {
            "located_right_sentence": bool(hit_sentence or hit_keyword),
            "explains_flaw": bool(hit_keyword or (corr and _good.replace("**", "")[:8] in corr)),
            "rewrote": len(corr) >= 25,
            "length_ok": len(corr) <= 160,
        }
        n_ok = sum(checks.values())
        solved = checks["located_right_sentence"] and checks["rewrote"]
        return GradeResult(solved, n_ok / len(checks), checks,
                           "找到并改对了" if solved else "未命中错误点或未重写")

    judge_prompt = (
        "你要比较两段针对同一问题的技术说明改写。读者背景与要求如下：\n"
        f"「{ctx}」，覆盖原文全部要点，120 字以内，不提元话语。\n\n"
        "评分维度（各 0-5 分）：准确性、清晰度、简洁度、读者适配。\n"
        "只输出 JSON：{\"score\": 0-20 的整数, \"reason\": \"一句话\"}"
    )

    return TaskInstance(
        tid=f"w{seed}", family="writing", seed=seed,
        messages=[{"role": "user", "content": prompt}],
        answer_spec='{"wrong_sentence":…, "why_wrong":…, "corrected":…}',
        grader=grader, judge_prompt=judge_prompt,
        reference=(f'{{"wrong_sentence": "{bad}", "why_wrong": "与正确表述相反", '
                   f'"corrected": "{ref_rewrite}"}}'),
        tools=[], needs_tools=False,
        meta={"good": good, "bad": bad, "keywords": keywords,
              "reference_rewrite": ref_rewrite, "draft": draft},
    )


def _strip(s: str) -> str:
    return re.sub(r"\s+", "", s)
