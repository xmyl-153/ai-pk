"""盲评硬化：用多个裁判重新盲评**已落盘的答案**，把"裁判噪声"从排名里拆出来。

为什么需要它：标定题里三个候选裁判的位置翻转率都是 0，但在 `writing` 实战里
deepseek-flash 的翻转率是 **24.4%** —— 标定通过 ≠ 实战无偏。
于是关键问题变成：**那份排名里有多少是模型差异、多少是裁判噪声？**

做法（不重跑任务，只重跑裁判 —— 答案已经在 runs.jsonl 里）：
  1. 读出每个 (模型, 题, rep) 的最终答案；
  2. 用 `make(family, seed)` 重建题面，取回 judge_prompt 与 reference；
  3. 每个裁判独立盲评（同一对答案**交换位置评两遍**），已有的判决可复用（`--reuse`）；
  4. 汇总四件事：
     - 单裁判翻转率 / 无效率（实战数据，不是标定数据）
     - 委员会（多数票）的翻转率与判平率
     - **裁判之间的排名一致性**（Kendall τ）—— τ 低说明名次本身是裁判噪声
     - 每个模型的分数区间（min~max）—— 区间跨越 0.5 的都别当结论用

用法：
    python tools/rejudge.py runs/WRITING-JUDGED \
        --judges deepseek-flash,glm-5.3,kimi-k3 --reuse --workers 3
"""
from __future__ import annotations

import argparse
import concurrent.futures as cf
import json
import statistics
import sys
import threading
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from aipk.arena import kendall_tau  # noqa: E402
from aipk.config import ROSTER, ModelSpec, load_gateways  # noqa: E402
from aipk.grade import Judge  # noqa: E402
from aipk.provider import Provider  # noqa: E402
from aipk.runner import load_run  # noqa: E402
from aipk.tasks import make  # noqa: E402


def resolve(key: str) -> ModelSpec:
    if "/" in key:
        prov, model = key.split("/", 1)
        return ModelSpec(prov, model, key)
    for prov, model, name, tier in ROSTER:
        if key in (model, name):
            return ModelSpec(prov, model, name, tier)
    raise SystemExit(f"认不出这个模型：{key}")


def verdict_key(task_key: str, rep: int, model_key: str, judge_key: str) -> tuple:
    """判决的唯一键 —— **必须带模型维度**。

    踩过的坑：一开始键是 (task_key, rep, judge_key)，少了 model_key。
    于是同一个 (题, rep) 下 10 个模型的答案共用一条判决：
    表里每个模型的分数变得一模一样，Kendall τ 假模假样地等于 1.000，
    而且 `--reuse` 会把 A 模型的判决当成 B 模型的（270 次里 261 次"复用"）。
    这类"看起来收敛得很漂亮"的结果，比崩掉更危险。
    """
    return (task_key, rep, model_key, judge_key)


def existing_verdicts(rs) -> dict[tuple, dict]:
    """从已有 transcript 里回收判决：{(task_key, rep, model_key, 裁判key): {...}}。"""
    out: dict[tuple, dict] = {}
    for r in rs:
        for t in (r.transcript or []):
            for raw in (t.get("judge") or []):
                key = raw.get("judge")
                if key:
                    out[verdict_key(r.task_key, r.rep, r.model_key, key)] = {
                        "flipped": bool(t.get("flipped")),
                        "winner": t.get("winner"),
                        "reused": True,
                    }
    return out


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description="多裁判重评已落盘的答案")
    ap.add_argument("run_dir", type=Path)
    ap.add_argument("--judges", default="deepseek-flash,glm-5.3,kimi-k3")
    ap.add_argument("--reuse", action="store_true", help="复用 run 里已有的判决，不重复花钱")
    ap.add_argument("--cache", type=Path, default=None,
                    help="判决落盘位置（默认 <out>.cache.json）—— 判决很贵，要当证据存下来")
    ap.add_argument("--workers", type=int, default=3)
    ap.add_argument("--reps", default=None,
                    help="只重评这些 rep（逗号分隔，如 0）；不传=全部。用来把代价压到可接受")
    ap.add_argument("--cache-only", action="store_true",
                    help="只用缓存里的判决做汇总，一条新判决都不请求（重分析不再花钱）")
    ap.add_argument("--out", type=Path, default=Path("runs/REJUDGE.md"))
    a = ap.parse_args(argv[1:])

    cache_path = a.cache or a.out.with_suffix(".cache.json")
    rs = load_run(a.run_dir / "runs.jsonl")
    keep_reps = None
    if a.reps is not None:
        keep_reps = {int(x) for x in a.reps.split(",") if x.strip() != ""}
    todo = [r for r in rs if not r.infra_failure and r.final_answer.strip()
            and (keep_reps is None or r.rep in keep_reps)]
    print(f"读入 {len(rs)} 条运行，其中可重评 {len(todo)} 条（{a.run_dir}）"
          + (f"，只取 rep={sorted(keep_reps)}" if keep_reps else ""))

    specs = [resolve(k.strip()) for k in a.judges.split(",") if k.strip()]
    have: dict[tuple, dict] = {}
    if a.reuse:
        have.update(existing_verdicts(rs))
    if cache_path.exists():
        cached = json.loads(cache_path.read_text(encoding="utf-8"))
        # 缓存里的键是 JSON 数组，转回 tuple
        for k, v in cached.items():
            have[tuple(json.loads(k))] = v
        print(f"从缓存读入 {len(cached)} 条已有判决：{cache_path}")
    gws = load_gateways()

    # 题面重建：judge_prompt / reference 不在 runs.jsonl 里，用 seed 现场重建（生成是确定性的）
    insts: dict[tuple[str, int], object] = {}
    for r in todo:
        fam = r.task_key.split(":")[0]
        insts[(r.task_key, r.rep)] = make(fam, r.seed)
    missing = [k for k, v in insts.items() if not getattr(v, "judge_prompt", None)]
    if missing:
        print(f"[WARN] {len(missing)} 条没有 judge_prompt，跳过")

    verdicts: dict[tuple, dict] = dict(have)
    calls = {"new": 0, "reused": 0}
    lock = threading.Lock()

    def save_cache() -> None:
        """增量落盘：判决很贵，中途挂掉不能全丢（实测一次 90 份 × 3 裁判要一个多小时）。"""
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = cache_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(
            {json.dumps(list(k), ensure_ascii=False): v for k, v in verdicts.items()},
            ensure_ascii=False), encoding="utf-8")
        tmp.replace(cache_path)

    def judge_one(spec: ModelSpec) -> None:
        prov = Provider(spec, gws, timeout_s=180, qps=1.2)
        judge = Judge([prov])
        try:
            for r in todo:
                inst = insts[(r.task_key, r.rep)]
                if not getattr(inst, "judge_prompt", None):
                    continue
                key = verdict_key(r.task_key, r.rep, r.model_key, spec.key)
                if key in verdicts:
                    calls["reused"] += 1
                    continue
                v = judge.compare(r.final_answer, inst.reference, inst.judge_prompt)
                with lock:
                    verdicts[key] = {"flipped": v.flipped, "winner": v.winner,
                                     "invalid": v.invalid, "reused": False}
                    calls["new"] += 1
                    if calls["new"] % 10 == 0:
                        save_cache()
                        # 注意：spec 本身就是 ModelSpec（早先写成 spec.spec.model_id，
                        # 在 worker 线程里抛 AttributeError 把整轮跑挂掉 —— 幸好缓存是增量落盘的）
                        print(f"  [{spec.model_id}] 新判决 {calls['new']} 条"
                              f"（缓存已增量落盘）", flush=True)
        finally:
            prov.close()

    with cf.ThreadPoolExecutor(max_workers=max(1, a.workers)) as ex:
        if not a.cache_only:
            list(ex.map(judge_one, specs))
    if a.cache_only:
        print("--cache-only：跳过所有裁判调用，只用缓存汇总")

    # 判决落盘 —— 判决很贵，且是"这个结论怎么来的"的原始证据
    save_cache()
    print(f"判决已落盘：{cache_path}（{len(verdicts)} 条）")

    # 自评排除：裁判不能给自己的答案打分（自偏好偏差）。
    # 离线重评时容易漏掉这条 —— 在线跑的时候 runner 会排除，这里必须自己排。
    self_judged = {k for k in verdicts
                   if k[2].split("/", 1)[-1] == k[3].split("/", 1)[-1]}
    if self_judged:
        print(f"排除自评判决 {len(self_judged)} 条（裁判评了自己）")

    # ---------- 汇总 ----------
    n_slots = len({(r.task_key, r.rep) for r in todo})
    n_ans = len(todo)
    L = ["# 盲评硬化报告（多裁判重评已落盘答案）", ""]
    L.append(f"- 来源：`{a.run_dir}`：**{n_ans} 份答案**（{n_slots} 个题×重复格 × 多模型）")
    L.append(f"- 裁判：{'、'.join(s.key for s in specs)}")
    L.append(f"- 新判决 {calls['new']} 次，复用 {calls['reused']} 次"
             f"（复用只在 --reuse 时发生，用来省重复花费；键含模型维度，不会张冠李戴）")
    L.append("")

    # 只统计本次**在范围内**的答案（--reps 过滤后）。
    # 踩过的坑：过滤只作用在"要判哪些"，聚合却still 把缓存里所有判决都算进去 ——
    # 于是 `--reps 0` 的报告里出现 90 格判决，看着像跑了全量。
    valid_slots = {(r.task_key, r.rep, r.model_key) for r in todo}

    score_of = {"1": 1.0, "2": 0.0, "tie": 0.5}   # a = 模型答案, b = 参考解
    L.append("## 1. 每个裁判在**实战数据**上的可靠性")
    L.append("")
    L.append("| 裁判 | 判决数 | 位置翻转率 | 判平率 | 判模型赢 | 判参考解赢 |")
    L.append("|---|---|---|---|---|---|")
    per_judge_scores: dict[str, dict[str, list[float]]] = defaultdict(lambda: defaultdict(list))
    judge_rank: dict[str, list[str]] = {}
    for spec in specs:
        vals = [v for (tk, rep, mk, k), v in verdicts.items()
                if k == spec.key and (tk, rep, mk) in valid_slots
                and (tk, rep, mk, k) not in self_judged]
        if not vals:
            continue
        flips = sum(1 for v in vals if v["flipped"])
        ties = sum(1 for v in vals if v["winner"] == "tie")
        wins = sum(1 for v in vals if v["winner"] == "1")
        loses = sum(1 for v in vals if v["winner"] == "2")
        for r in todo:
            key = verdict_key(r.task_key, r.rep, r.model_key, spec.key)
            if key in self_judged:
                continue
            v = verdicts.get(key)
            if v:
                per_judge_scores[spec.key][r.model_key].append(score_of.get(v["winner"], 0.5))
        means = {m: statistics.fmean(v) for m, v in per_judge_scores[spec.key].items()}
        judge_rank[spec.key] = sorted(means, key=lambda m: -means[m])
        L.append(f"| {spec.key} | {len(vals)} | {flips / len(vals) * 100:.1f}% "
                 f"| {ties / len(vals) * 100:.1f}% | {wins / len(vals) * 100:.1f}% "
                 f"| {loses / len(vals) * 100:.1f}% |")
    L.append("")

    # 委员会（多数票，翻转的裁判记为 tie）—— 按 (题, rep, 模型) 聚合
    committee: dict[tuple[str, int, str], list[str]] = defaultdict(list)
    any_flip: dict[tuple[str, int, str], bool] = defaultdict(bool)
    for (tk, rep, mk, k), v in verdicts.items():
        if (tk, rep, mk, k) in self_judged or (tk, rep, mk) not in valid_slots:
            continue
        key = (tk, rep, mk)
        if v.get("flipped"):
            any_flip[key] = True
            committee[key].append("tie")
        else:
            committee[key].append(v["winner"])
    comm_score: dict[str, list[float]] = defaultdict(list)
    comm_tie = 0
    for r in todo:
        votes = committee.get((r.task_key, r.rep, r.model_key)) or []
        if not votes:
            continue
        tally = {w: votes.count(w) for w in ("1", "2", "tie")}
        best = max(tally, key=lambda w: tally[w])
        if tally[best] * 2 <= len(votes):
            best = "tie"
        if best == "tie":
            comm_tie += 1
        comm_score[r.model_key].append(score_of[best])
    n_groups = len(committee)
    n_strict = sum(1 for k in committee if not any_flip[k])
    L.append("## 2. 委员会（多数票）")
    L.append("")
    L.append(f"- 投票 {sum(len(v) for v in committee.values())} 次，覆盖 {n_groups} 个"
             f"（题, rep, 模型）格；委员会判平 **{comm_tie / max(1, n_groups) * 100:.1f}%**")
    L.append("- 机制：单个裁判的翻转**不再直接决定胜负**，而是降级成平局，再由多数票定夺。")
    L.append("")
    L.append("### 两种投票策略（同一批判决，两种用法）")
    L.append("")
    L.append("| 策略 | 可用格数 | 说明 |")
    L.append("|---|---|---|")
    L.append(f"| 宽松：翻转记平局 | {n_groups} | 所有人都有分，但翻转多的格会被和稀泥成平局 |")
    L.append(f"| **严格：任一人翻转就丢弃该格** | {n_strict} "
             f"（{n_strict / max(1, n_groups) * 100:.0f}%）"
             f" | 只保留「所有裁判都拿得准」的格，分数更硬但样本变少 |")
    L.append("")

    # 裁判之间的排名一致性
    keys = list(judge_rank)
    if len(keys) >= 2:
        L.append("## 3. 裁判之间的一致性（这份排名有多少是噪声）")
        L.append("")
        L.append("| 裁判 A | 裁判 B | Kendall τ |")
        L.append("|---|---|---|")
        taus = []
        for i in range(len(keys)):
            for j in range(i + 1, len(keys)):
                t = kendall_tau(judge_rank[keys[i]], judge_rank[keys[j]])
                taus.append(t)
                L.append(f"| {keys[i]} | {keys[j]} | {t:.3f} |")
        L.append("")
        L.append(f"- 平均 τ = **{statistics.fmean(taus):.3f}**"
                 f"（1 = 排名完全一致，0 = 无关）")
        L.append("")

    L.append("## 4. 每个模型的分数区间（跨裁判）")
    L.append("")
    L.append("| 模型 | " + " | ".join(s.key for s in specs if s.key in per_judge_scores)
             + " | 委员会 | 区间宽度 |")
    L.append("|---" * (len([s for s in specs if s.key in per_judge_scores]) + 3) + "|")
    rows = []
    for m in sorted({r.model_key for r in todo}):
        per = [statistics.fmean(per_judge_scores[k][m]) for k in per_judge_scores if per_judge_scores[k].get(m)]
        c = statistics.fmean(comm_score[m]) if comm_score.get(m) else float("nan")
        width = (max(per) - min(per)) if per else float("nan")
        rows.append((m, per, c, width))
    for m, per, c, width in sorted(rows, key=lambda x: -x[2]):
        L.append(f"| {m.split('/')[-1]} | " + " | ".join(f"{p:.3f}" for p in per)
                 + f" | {c:.3f} | {width:.3f} |")
    L.append("")
    L.append("> 区间宽度 = 不同裁判给出的分数极差。宽度 ≥0.5 的模型，"
             "**换一个裁判就能改变结论**，不该写进结论里。")
    L.append("")
    # 5. 代码 oracle vs 盲评：同一批答案，两种尺子分别看见多少差异
    oracle: dict[str, list[float]] = defaultdict(list)
    for r in todo:
        oracle[r.model_key].append(r.reward)
    o_means = [statistics.fmean(v) for v in oracle.values() if v]
    c_means = [statistics.fmean(comm_score[m]) for m in oracle if comm_score.get(m)]
    L.append("## 5. 代码 oracle vs 盲评：两把尺子分别看见多少差异")
    L.append("")
    L.append("| 模型 | oracle 分（均值/σ） | 委员会盲评分（均值/σ） |")
    L.append("|---|---|---|")
    for m in sorted(oracle, key=lambda x: -(statistics.fmean(comm_score[x]) if comm_score.get(x) else 0)):
        o = oracle[m]
        c = comm_score.get(m) or []
        L.append(f"| {m.split('/')[-1]} | {statistics.fmean(o):.3f} / "
                 f"{statistics.pstdev(o):.3f} | "
                 + (f"{statistics.fmean(c):.3f} / {statistics.pstdev(c):.3f} |" if c else "— |"))
    L.append("")
    if o_means and c_means:
        L.append(f"- **oracle 分极差 {max(o_means) - min(o_means):.3f}**，"
                 f"**盲评分极差 {max(c_means) - min(c_means):.3f}** —— "
                 f"同一批答案，代码 oracle 看不见差异、盲评看得见。")
        L.append("- 这就是「正确率饱和」的实证：凡是能用代码判定的地方，"
                 "前沿模型都过了；剩下的差异只能靠人来判（而人判又带噪声）。")
        L.append("")

    L.append("## 怎么读")
    L.append("")
    L.append("- 单裁判翻转率高 → 那份排名只能当弱证据；委员会能摊平翻转，但会增加成本。")
    L.append("- 裁判间 τ 低 → **名次本身**就不可信，此时该做的是加大判决数或换更好的 rubric，"
             "而不是把名次写进结论。")
    L.append("- 分数区间宽 → 该模型的排名依赖裁判选择，同样不能当结论。")

    md = "\n".join(L)
    a.out.write_text(md, encoding="utf-8")
    enc = getattr(sys.stdout, "encoding", None) or "utf-8"
    sys.stdout.write(md.encode(enc, "replace").decode(enc, "replace") + "\n")
    print(f"\n已写入 {a.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
