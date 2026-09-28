"""探针：网关到底听不听 `temperature`？

为什么需要这一步：本项目所有历史运行都是 `temperature=0`，所以 `一致性 σ` 恒为 0。
要测"输出方差"，前提是**这个参数真的生效** —— 否则跑出来"方差=0"是网关忽略了参数，
不是模型稳定。这类"测了个空"的坑，本项目已经踩过好几次（口径不对、题目给错答案）。

做法：同一 prompt、同一模型，在 temperature=0 与 temperature=0.9 下各调 N 次，
统计**去重后的答案数**。0 下应高度一致；0.9 下若仍完全一致，就要怀疑参数没生效。
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from aipk.config import ROSTER, ModelSpec, load_gateways  # noqa: E402
from aipk.provider import Provider  # noqa: E402

PROMPT = "给一个 3 个字的男孩小名，只回名字本身，不要标点、不要解释、不要重复我的问题。"
N = 6
MODELS = ["deepseek-flash", "qwen3.8-flash"]


def probe(gateways, model_id: str, temperature: float) -> list[str]:
    spec = None
    for prov, mid, name, tier in ROSTER:
        if mid == model_id:
            spec = ModelSpec(prov, mid, name, tier)
            break
    if spec is None:
        raise SystemExit(f"名单里没有 {model_id}")
    p = Provider(spec, gateways, timeout_s=120, qps=1.2)
    outs: list[str] = []
    try:
        for _ in range(N):
            res = p.chat([{"role": "user", "content": PROMPT}], None,
                         temperature=temperature, max_tokens=256)
            outs.append((res.text or "").strip().replace("\n", " ")[:20])
    finally:
        p.close()
    return outs


def main() -> int:
    gws = load_gateways()
    for model_id in MODELS:
        print(f"=== {model_id} ===")
        for t in (0.0, 0.9):
            outs = probe(gws, model_id, t)
            uniq = sorted(set(outs))
            print(f"  temperature={t}: {N} 次调用 -> {len(uniq)} 种答案")
            print(f"    {outs}")
    print("\n判断：0.9 下答案种类明显多于 0.0 → 参数生效，可以做方差实验；"
          "两者一样 → 先查网关是否忽略 temperature，别急着报「模型很稳」。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
