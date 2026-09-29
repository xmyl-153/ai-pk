"""判定层：代码 oracle 为主，盲评为辅。

盲评的三条去偏措施（缺一条这个榜就没法看）：
1. 裁判不知道哪段是谁写的，标签只写"答案1/答案2"
2. **交换位置评两遍**：只有两次结论一致才算数，不一致记为"位置不稳定"
3. 报裁判一致率：位置翻转率 + 多裁判 Cohen's κ
"""
from __future__ import annotations

import itertools
import json
import random
from dataclasses import dataclass, field

from .provider import Provider
from .tasks import extract_json

JUDGE_TMPL = """你在做一次**盲评**。你不知道两段答案分别由谁生成。

{rubric}

待评内容：

【答案1】
{a}

【答案2】
{b}

请独立评分，不要考虑篇幅长短（长不等于好）。
只输出 JSON，不要任何其他文字：
{{"winner": "1" 或 "2" 或 "tie", "score1": 0-20 整数, "score2": 0-20 整数, "reason": "一句话理由"}}"""


@dataclass
class JudgeVerdict:
    winner: str = "tie"          # 1 | 2 | tie
    score_a: float = 0.0
    score_b: float = 0.0
    flipped: bool = False        # 交换位置后结论变了 → 位置偏置
    invalid: bool = False        # 裁判一次都没吐出可解析的 JSON —— 这不是"平局"
    raw: list[dict] = field(default_factory=list)


class Judge:
    """盲评裁判。

    实测踩过的坑：用思考型模型当裁判时，max_tokens 给 2048 会被 reasoning 吃光，
    正文为空 → extract_json 拿到 None → 整批判决作废（上一轮 writing 族大量 null）。
    所以这里给足配额、失败重试一次，并把"无效裁决数"记下来以便报告。
    """

    def __init__(self, providers: list[Provider], max_tokens: int = 6144, retries: int = 2):
        if not providers:
            raise ValueError("至少需要一个裁判模型")
        self.providers = providers
        self.max_tokens = max_tokens
        self.retries = max(1, retries)
        self.invalid = 0      # 无法解析的裁决次数
        self.calls = 0        # 总调用次数

    def _ask(self, provider: Provider, a: str, b: str, rubric: str) -> dict | None:
        prompt = JUDGE_TMPL.format(rubric=rubric, a=a[:6000], b=b[:6000])
        for attempt in range(self.retries):
            self.calls += 1
            out = provider.chat([{"role": "user", "content": prompt}], None,
                                temperature=0.0, max_tokens=self.max_tokens, stream=True)
            obj = extract_json(out.text) if out.text else None
            if isinstance(obj, dict) and "winner" in obj:
                return obj
        self.invalid += 1
        return None

    @property
    def invalid_rate(self) -> float:
        return self.invalid / self.calls if self.calls else 0.0

    def compare(self, a: str, b: str, rubric: str) -> JudgeVerdict:
        """a/b 是两份答案。返回以 a 为基准的裁决。"""
        v = JudgeVerdict()
        votes: list[str] = []
        scores_a: list[float] = []
        scores_b: list[float] = []
        for provider in self.providers:
            fwd = self._ask(provider, a, b, rubric)
            rev = self._ask(provider, b, a, rubric)
            v.raw.append({"judge": provider.spec.key, "forward": fwd, "reverse": rev})
            if fwd and rev:
                w_f = str(fwd.get("winner", "tie"))
                w_r = str(rev.get("winner", "tie"))
                # 反向时 winner 语义反转
                w_r_as_a = {"1": "2", "2": "1", "tie": "tie"}.get(w_r, "tie")
                if w_f != w_r_as_a:
                    v.flipped = True
                    votes.append("tie")           # 位置不稳定 → 记平局，不奖励偏置
                else:
                    votes.append(w_f)
                try:
                    scores_a.append(float(fwd.get("score1", 0)))
                    scores_b.append(float(fwd.get("score2", 0)))
                except Exception:  # noqa: BLE001
                    pass
            elif fwd:
                votes.append(str(fwd.get("winner", "tie")))
                try:
                    scores_a.append(float(fwd.get("score1", 0)))
                    scores_b.append(float(fwd.get("score2", 0)))
                except Exception:  # noqa: BLE001
                    pass
        if not votes:
            # 一次有效裁决都没有：这不是"平局"，是"没判出来"。
            # 早期版本把它当 tie（0.5 分），等于白送模型半分 —— 必须显式标成无效。
            v.invalid = True
            return v
        tally = {"1": votes.count("1"), "2": votes.count("2"), "tie": votes.count("tie")}
        v.winner = max(tally, key=lambda k: tally[k])
        if tally["1"] == tally["2"]:
            v.winner = "tie"
        v.score_a = sum(scores_a) / len(scores_a) if scores_a else 0.0
        v.score_b = sum(scores_b) / len(scores_b) if scores_b else 0.0
        return v


# ---------------------------------------------------------------- 裁判自审


def verdict_score(v: JudgeVerdict, strict: bool = False) -> float | None:
    """把一次盲评裁决折算成"对参考解"的相对分；判不出来返回 None。

    a = 模型答案，b = 参考解：赢=1.0 平=0.5 输=0.0。

    `strict=True`（严格模式）下，**位置翻转的裁决直接丢弃**（返回 None），而不是记成平局。
    为什么需要这个开关：实测裁判实战位置翻转率 26%，
    "翻转记平局"会把噪声往 0.5 拉、连真实差距一起抹平；
    严格模式宁愿样本少，也不让拿不准的裁决参与打分。
    两种口径都能算（`tools/rejudge.py` 出对照表），
    由使用者按"要样本量还是要硬度"选。
    """
    if v.invalid:
        return None
    if v.flipped and strict:
        return None
    return {"1": 1.0, "2": 0.0, "tie": 0.5}.get(v.winner, 0.5)


def judge_audit(verdicts: list[JudgeVerdict]) -> dict:
    """裁判自身的可靠性：位置翻转率 + 有效裁决数。"""
    n = len(verdicts)
    if not n:
        return {"n": 0, "position_flip_rate": None, "decided_rate": None}
    flips = sum(1 for v in verdicts if v.flipped)
    decided = sum(1 for v in verdicts if v.winner in ("1", "2"))
    return {
        "n": n,
        "position_flip_rate": round(flips / n, 3),
        "decided_rate": round(decided / n, 3),
    }


def cohen_kappa(labels_a: list[str], labels_b: list[str]) -> float | None:
    """两个裁判的一致性（用于多裁判场景）。"""
    if len(labels_a) != len(labels_b) or not labels_a:
        return None
    cats = sorted(set(labels_a) | set(labels_b))
    n = len(labels_a)
    agree = sum(1 for x, y in zip(labels_a, labels_b) if x == y) / n
    exp = 0.0
    for c in cats:
        exp += (labels_a.count(c) / n) * (labels_b.count(c) / n)
    if abs(1 - exp) < 1e-9:
        return None
    return round((agree - exp) / (1 - exp), 3)


# ---------------------------------------------------------------- 配对赛程


def pair_schedule(model_keys: list[str], max_pairs: int | None = None, seed: int = 0) -> list[tuple[str, str]]:
    """全循环配对（数量小时最稳），超出上限则按 seed 抽样。"""
    pairs = list(itertools.combinations(sorted(model_keys), 2))
    if max_pairs and len(pairs) > max_pairs:
        r = random.Random(seed)
        pairs = r.sample(pairs, max_pairs)
    return pairs
