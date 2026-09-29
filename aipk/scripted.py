"""离线演示 provider：不连任何网关，按协议把"标准答案"提交上去。

为什么要有它（开源项目的门面）：

1. **别人 clone 下来不用 key 就能跑通全链路** —— 任务生成 → 冻结 harness →
   多轮/工具/分阶段 → 判定 → 报告，一条都不少。
   同类项目（如 agent-hack/agent-harness）也把"默认不需要 API key 就能跑"当硬要求：
   一个需要付费 API 才能跑的测试，没人会去贡献。
2. **它同时是端到端回归测试**：满分机器人应当 100% 通过。哪天某个族的 grader
   或 harness 改坏了，`python -m aipk demo` 会立刻掉分 ——
   这比只看 `selfcheck`（只测 grader 单点）覆盖面更大。
3. **对战台面板拿它当陪练**：面板要有输有赢才看得出门道，所以除了满分/错答，
   另加"半桶水"（稳定地答错一部分）和"慢吞吞"（答得对但慢）两档。

它不是"模拟模型能力"：这几档机器人只用来验证**测量装置**、演示**面板怎么用**，
不能拿来比较模型（要比模型请配真网关）。
"""
from __future__ import annotations

import hashlib
import json
import threading
import time

from .oracle import truth_answer
from .provider import ChatResult, ToolCall, Usage
from .tasks import TaskInstance

# 每档机器人每次调用"花"多久。真模型当然是真耗时，这里是为了让离线演示的
# 速度对比看起来像回事（不然全是 1ms，倍数会变成天文数字）。
# 面板与文档都会写明"离线机器人的延迟是脚本模拟的"。
MODE_LATENCY = {"oracle": 0.22, "half": 0.30, "slow": 0.95, "wrong": 0.22}


class ScriptedProvider:
    """假装成一个"照标准答案作答"的模型。

    `mode`:
      - `oracle`：走完整协议（需要改文件的族会先调 run_python 落盘，再 submit）—— 应当 100% 通过
      - `wrong` ：提交一个格式合法但内容错的答案 —— 应当 0% 通过
                  用来验证"判错"这条路径也通，避免出现"永远满分"的假象
      - `half`  ：按族名稳定地答对约一半（同一批题每次跑结果一致，方便对照）
      - `slow`  ：答得都对，但每次调用都磨蹭一会儿 —— 用来演示"分数打平、速度差几倍"

    **线程安全**：Runner 对每个 model_key 只建一个 provider 实例，而 run 是多线程跑的，
    所以"当前是哪道题、走到第几步、第几阶段"必须放在 **thread-local** 里。
    踩过的坑（缺陷 #19）：一开始用实例属性存这些状态，并发时 A 题的答案会被 B 题取走 ——
    表现为"满分机器人只过 92.3%"，而且**单线程/低并发时完全看不出来**
    （是一次 13 题的跑刚好撞上才暴露的）。
    """

    def __init__(self, spec=None, mode: str = "oracle", latency_s: float | None = None):
        from .config import ModelSpec
        self.spec = spec or ModelSpec("scripted", mode, f"scripted-{mode}", "offline")
        self.mode = mode
        self.latency_s = MODE_LATENCY.get(mode, 0.22) if latency_s is None else latency_s
        self.calls = 0
        self._local = threading.local()

    # ---- 与 Provider 对齐的接口 ----
    def close(self) -> None:
        pass

    def set_task(self, inst: TaskInstance) -> None:
        self._local.inst = inst
        self._local.correct = self._decide_correct(inst)
        self._local.plan = self._build_plan(inst)
        self._local.step = 0
        self._local.stage = 0

    def _decide_correct(self, inst: TaskInstance) -> bool:
        """这一题它会不会答对（只对有输有赢的档位有意义）。

        `half` 按**族名**决定，不是按运行时随机：同一批题每次跑结果一致，
        面板上"它在哪几类题上栽了"才说得清。
        """
        if self.mode == "wrong":
            return False
        if self.mode == "half":
            h = hashlib.blake2b(inst.family.encode("utf-8"), digest_size=2).digest()
            return int.from_bytes(h, "big") % 100 < 55
        return True

    def note_stage(self, stage: int) -> None:
        self._local.stage = stage

    def _state(self) -> tuple[TaskInstance | None, list[ToolCall], int]:
        return (getattr(self._local, "inst", None),
                getattr(self._local, "plan", []),
                getattr(self._local, "step", 0))

    @property
    def _stage(self) -> int:
        return getattr(self._local, "stage", 0)

    # ---- 计划：先做该做的事，最后 submit ----
    def _build_plan(self, inst: TaskInstance) -> list[ToolCall]:
        plan: list[ToolCall] = []
        if getattr(self._local, "correct", True) and inst.family == "mindiff":
            # 最小 diff 族必须**真的改工作目录里的文件**（判定看的是工作区快照）
            good = inst.meta.get("good_rules_py") or ""
            target = inst.meta.get("target") or "app/rules.py"
            code = ("from pathlib import Path\n"
                    f"Path({target!r}).write_text({good!r}, encoding='utf-8')\n"
                    f"print('wrote', {target!r})\n")
            plan.append(ToolCall(id="scripted-write", name="run_python",
                                 args={"code": code}, raw_args=code, source="native"))
        return plan

    def _answer_for(self, inst: TaskInstance) -> str:
        if inst is None:
            return "{}"
        if not getattr(self._local, "correct", True):
            # 答错也要"答得像话"：格式合法、内容不对 —— 这才是判错路径该测的东西
            return json.dumps({"answer": None, "note": f"scripted-{self.mode}"})
        fam = inst.family
        if inst.next_stage is not None:
            return self._staged_answer(inst)
        truth = truth_answer(fam, inst)
        return truth if truth is not None else "{}"

    def _staged_answer(self, inst: TaskInstance) -> str:
        """分阶段族：按当前阶段号给出该阶段的答案。"""
        m = inst.meta
        if inst.family == "longstate":
            states = m["expected_states"]
            idx = min(self._stage, len(states) - 1)
            return json.dumps({"ledger": states[idx]}, ensure_ascii=False)
        # decay：首答与末答都给真值
        return json.dumps({"value": m["truth"], "first_value": m["truth"],
                           "note": "该值在整个会话中未变更"}, ensure_ascii=False)

    def chat(self, messages, tools=None, *, temperature: float = 0.0,
             max_tokens: int = 8192, stream: bool = True, max_retries: int = 3) -> ChatResult:
        self.calls += 1
        # 磨蹭一下：真模型这里是真实耗时，离线机器人是脚本演的（面板会写明）。
        # 每次调用的总耗时由 harness 自己按墙钟计，不需要这里造假。
        if self.latency_s > 0:
            time.sleep(self.latency_s)
        ttft = max(1, int(self.latency_s * 1000 * 0.4))
        spent = max(1, int(self.latency_s * 1000))
        inst, plan, step = self._state()      # 全部取自 thread-local，避免并发串题
        names = {t["function"]["name"] for t in (tools or [])}
        usage = Usage(prompt_tokens=0, completion_tokens=0, reasoning_tokens=0, reported=True)
        # 该族要求的准备工作还没做完 → 先做
        if step < len(plan) and "run_python" in names:
            call = plan[step]
            self._local.step = step + 1
            return ChatResult(text="", tool_calls=[call], finish_reason="tool_calls",
                              usage=usage, ttft_ms=ttft, total_ms=spent)
        answer = self._answer_for(inst)
        if "submit" in names:
            return ChatResult(text="",
                              tool_calls=[ToolCall(id=f"scripted-submit-{self.calls}", name="submit",
                                                   args={"answer": answer}, raw_args=answer,
                                                   source="native")],
                              finish_reason="tool_calls", usage=usage, ttft_ms=ttft, total_ms=spent)
        # 没有工具协议（tools-off profile）：直接把答案当正文
        return ChatResult(text=answer, tool_calls=[], finish_reason="stop",
                          usage=usage, ttft_ms=ttft, total_ms=spent)
