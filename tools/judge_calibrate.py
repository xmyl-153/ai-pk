"""裁判标定：在信任何 LLM 裁判之前，先用**已知答案**测它准不准。

三种标定（缺一不可）：
  1. **判别力**：好答案 vs 明显烂答案，裁判该稳定选好的
  2. **位置偏置**：同一对答案交换位置评两遍，结论翻转率
  3. **自一致性**：两份**完全相同**的答案，裁判不该系统性地偏向某一侧

只有三项都过，裁判打出来的分才值得写进报告。否则就是"用另一个模型的偏见测这个模型"。

**每个候选裁判单独标定**：早期版本把多个候选塞进同一个 Judge，那测的是"委员会"，
看不出单个裁判行不行。多个候选按同一套已知答案各跑一遍，再按明确规则择优：
三项是否全过 → 无效裁决率 → 重试次数 → 单次判决耗时。

用法：
    python tools/judge_calibrate.py                    # 标定默认候选池
    python tools/judge_calibrate.py glm-5.3 kimi-k3    # 指定候选
"""
from __future__ import annotations

import json
import statistics
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from aipk.config import ROSTER, ModelSpec, load_gateways  # noqa: E402
from aipk.grade import Judge  # noqa: E402
from aipk.provider import Provider  # noqa: E402

# 真实长度的对照：标定题都是短句，但实战里裁判要读两份**长答案**再判。
# 实测教训：glm-5.3 在短标定题上单次判决 23s（看着只比 deepseek-flash 慢 3.7 倍），
# 但在 writing 实战的长提示下慢到几分钟一条 —— 因为它是"想很多"的模型，
# 提示越长思考越久，而**标定的成本排名不会自动迁移到实战**。
# 所以标定必须带一组真实长度的对照，并单独报它的耗时。
REAL_LEN_PAIR = {
    "name": "真实长度·改写对照",
    "good": ("LRU 按最近一次访问的时间排序，淘汰最久没有被访问的页。读和写都会刷新它的新鲜度，"
             "所以判断依据是「最近访问」，而不是「最近写入」。实现上通常用哈希表加双向链表："
             "哈希表负责 O(1) 定位，链表负责维护访问顺序，每次命中都把该节点移到表头，"
             "容量满时从表尾淘汰。这样命中、插入、淘汰都是常数时间，代价是每个条目多两个指针。"
             "需要注意的两点：一是并发场景下链表操作要加锁或做分片，否则会退化成全局瓶颈；"
             "二是扫描型负载（顺序读一大片只用一次的数据）会把热数据冲掉，"
             "这类场景更适合用 LRU-K 或分段 LRU 来抵抗污染。"),
    "bad": ("LRU 淘汰最久没有被写入的页，写入会刷新新鲜度。它一般用哈希表加数组实现，"
            "查找和淘汰都是常数时间。并发的时候不用加锁，因为链表操作本身是原子的；"
            "如果遇到顺序扫描把缓存冲掉，只要把容量调大就可以了，不需要别的策略。"),
}


def resolve_key(key: str) -> tuple[str, str]:
    """接受 "provider/model" 或只写模型名（从名单里查）。"""
    if "/" in key:
        prov, model = key.split("/", 1)
        return prov, model
    for prov, model, name, _tier in ROSTER:
        if key in (model, name):
            return prov, model
    raise SystemExit(f"认不出这个模型：{key}。写 provider/model，或从 `python -m aipk list` 里挑一个。")

RUBRIC = ("评分维度（各 0-5 分）：准确性、清晰度、简洁度、读者适配。"
          "只输出 JSON：{\"score\": 0-20 的整数, \"reason\": \"一句话\"}")

# 三组"明显好坏"对照（技术说明改写场景）
PAIRS = [
    {
        "name": "LRU 解释",
        "good": ("LRU 按最近一次访问的时间排序，淘汰最久没被访问的页；"
                 "读写都会刷新它的新鲜度，所以不是看谁最少被写入。"),
        "bad": "LRU 就是随便淘汰一个页，反正缓存命中率差不多。",
    },
    {
        "name": "幂等解释",
        "good": ("幂等不阻止重复执行，它保证重复执行后的最终状态与只执行一次相同，"
                 "因此重试是安全的。"),
        "bad": "幂等就是让接口只执行一次，加个锁就行了。",
    },
    {
        "name": "写时复制",
        "good": ("fork 时不复制物理页，父子进程先共享同一份只读映射，"
                 "等某一方真正写入时才复制那一页。"),
        "bad": "写时复制就是 fork 的时候先把内存都复制一遍，这样比较安全。",
    },
    # 细微对：两份都像模像样，只在关键点上有一处错。
    # 这才是真正检验裁判"配不配进报告"的题 —— 明显好坏连字符串匹配都能判。
    {
        "name": "细微·LRU 方向",
        "good": "LRU 淘汰最久未被访问的页，访问会刷新新鲜度。",
        "bad": "LRU 淘汰最久未被写入的页，写入会刷新新鲜度。",
    },
    {
        "name": "细微·慢启动",
        "good": "TCP 慢启动阶段拥塞窗口按指数增长，收到确认就翻倍。",
        "bad": "TCP 慢启动阶段拥塞窗口按线性增长，每次加一。",
    },
    {
        "name": "细微·算术强度",
        "good": "算术强度低的任务访存占比高，更容易受内存带宽限制。",
        "bad": "算术强度低的任务更容易受算力限制。",
    },
]


# 默认候选池：一个轻量（便宜）、一个主力、一个跨厂商旗舰
DEFAULT_CANDIDATES = ["deepseek-flash", "glm-5.3", "kimi-k3"]


def calibrate_one(key: str, gws: dict) -> dict:
    """标定**单个**裁判（不搞委员会，否则看不出个体差异）。"""
    prov, model = resolve_key(key)
    provider = Provider(ModelSpec(prov, model, key), gws, timeout_s=180)
    judge = Judge([provider])

    disk = {"n": 0, "correct": 0}
    pos = {"n": 0, "flipped": 0, "left_wins": 0}
    ident = {"n": 0, "left_wins": 0, "right_wins": 0, "ties": 0}
    lat: list[float] = []

    print(f"--- {key} ---")
    try:
        for p in PAIRS:
            t0 = time.perf_counter()
            v1 = judge.compare(p["good"], p["bad"], RUBRIC)   # 正序：好答案在左
            lat.append(time.perf_counter() - t0)
            v2 = judge.compare(p["bad"], p["good"], RUBRIC)   # 反序：好答案在右
            v3 = judge.compare(p["good"], p["good"], RUBRIC)  # 相同答案：应判平
            disk["n"] += 2
            disk["correct"] += (1 if v1.winner == "1" else 0) + (1 if v2.winner == "2" else 0)
            pos["n"] += 1
            if (v1.winner == "1") != (v2.winner == "2"):
                pos["flipped"] += 1
            pos["left_wins"] += 1 if v1.winner == "1" else 0
            ident["n"] += 1
            ident["left_wins"] += 1 if v3.winner == "1" else 0
            ident["right_wins"] += 1 if v3.winner == "2" else 0
            ident["ties"] += 1 if v3.winner == "tie" else 0
            print(f"  [{p['name']}] 正序={v1.winner} 反序={v2.winner}（期望 1/2）"
                  f"　相同答案={v3.winner}（期望 tie）")

        # 真实长度的成本对照（只测耗时与可用性，不并入判别力统计）
        rp = REAL_LEN_PAIR
        t0 = time.perf_counter()
        rv = judge.compare(rp["good"], rp["bad"], RUBRIC)
        real_s = time.perf_counter() - t0
        real_ok = (not rv.invalid) and rv.winner in ("1", "2", "tie")
        print(f"  [{rp['name']}] winner={rv.winner} 耗时={real_s:.1f}s "
              f"（这才是实战量级）")
    finally:
        provider.close()

    return {
        "key": key,
        "discrimination": {
            "n": disk["n"], "correct": disk["correct"],
            "accuracy": round(disk["correct"] / disk["n"], 3) if disk["n"] else None,
            "pass": disk["correct"] == disk["n"],
        },
        "position_bias": {
            "n": pos["n"], "flipped": pos["flipped"],
            "flip_rate": round(pos["flipped"] / pos["n"], 3) if pos["n"] else None,
            "left_win_rate": round(pos["left_wins"] / pos["n"], 3) if pos["n"] else None,
            "pass": pos["flipped"] == 0,
        },
        "self_consistency": {
            "n": ident["n"], "left_wins": ident["left_wins"],
            "right_wins": ident["right_wins"], "ties": ident["ties"],
            "pass": ident["ties"] == ident["n"],
        },
        "judge_invalid_rate": round(judge.invalid_rate, 3),
        "judge_calls": judge.calls,
        "median_verdict_s": round(statistics.median(lat), 2) if lat else None,
        "real_len_verdict_s": round(real_s, 1),
        "real_len_usable": bool(real_ok),
    }


def pick_best(results: dict[str, dict]) -> tuple[str | None, str]:
    """择优规则（明写出来，避免"我觉得这个裁判更好"）。

    1. 三项标定必须全过（判别力 100%、位置翻转 0、相同答案全判平）
    2. 无效裁决率低者优先（判不出 JSON = 浪费一轮）
    3. **真实长度判决耗时低者优先** —— 不是短标定题的耗时：
       实测 glm-5.3 短题 23s、看着只慢 3.7 倍，实战长提示下慢到几分钟一条，
       标定的成本排名不会自动迁移到实战。
    4. 重试次数少者优先
    """
    passed = [k for k, r in results.items()
              if r["discrimination"]["pass"] and r["position_bias"]["pass"]
              and r["self_consistency"]["pass"]]
    if not passed:
        return None, "没有任何候选三项全过 —— 盲评分数不可信，报告里必须标注"

    def rank(k: str):
        r = results[k]
        real = r.get("real_len_verdict_s")
        return (r["judge_invalid_rate"],
                real if real is not None else r.get("median_verdict_s") or 9e9,
                r["judge_calls"])
    best = min(passed, key=rank)
    others = [k for k in passed if k != best]
    detail = "、".join(
        f"{k} 真实长度 {results[k].get('real_len_verdict_s', '?')}s" for k in passed)
    reason = (f"{len(passed)} 个候选三项全过；按「无效裁决率 → **真实长度判决耗时** → 重试次数」"
              f"择优（{detail}）" + (f"；其余通过者：{'、'.join(others)}" if others else ""))
    return best, reason


def main(argv: list[str]) -> int:
    keys = argv[1:] or list(DEFAULT_CANDIDATES)
    print(f"裁判标定：{len(keys)} 个候选（每个单独标定）\n")
    gws = load_gateways()
    results: dict[str, dict] = {}
    for k in keys:
        try:
            results[k] = calibrate_one(k, gws)
        except Exception as e:  # noqa: BLE001
            print(f"--- {k} --- 标定失败：{type(e).__name__}: {e}")
            results[k] = {"key": k, "error": f"{type(e).__name__}: {e}"[:200],
                          "discrimination": {"pass": False}, "position_bias": {"pass": False},
                          "self_consistency": {"pass": False}, "judge_invalid_rate": None,
                          "judge_calls": 0, "median_verdict_s": None}
        print()

    selected, reason = pick_best({k: r for k, r in results.items() if "error" not in r})
    out = {
        "candidates": keys,
        "selected": selected,
        "selection_reason": reason,
        "criteria": ["三项标定全过", "无效裁决率低", "重试次数少", "判决耗时短"],
        "results": results,
    }
    print(json.dumps({k: {kk: vv for kk, vv in v.items() if kk != "key"}
                      for k, v in results.items()}, ensure_ascii=False, indent=2))
    print("\n择优：" + (f"**{selected}** —— {reason}" if selected else f"无可用裁判 —— {reason}"))
    path = Path("runs/judge_calibration.json")
    path.parent.mkdir(exist_ok=True)
    path.write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"已写入 {path}")
    return 0 if selected else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
