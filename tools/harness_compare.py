"""两种 harness 的正面对比：冻结 harness vs 真实 CLI（Phase B）。

用法：
    python tools/harness_compare.py runs/<冻结那次> runs/<外部CLI那次> \
        [--map deepseek/deepseek-flash=jiyuanapi/deepseek-flash] \
        [--label-a "冻结协议" --label-b "Codex CLI"]

只比较**两边都跑过的任务**（task_key 相同），否则"没跑过的题"会被算成失败。
`--map` 用来把外部 CLI 里的模型 id 对到冻结 harness 里的同一个模型
（同一个模型族经不同供给时 id 不同，映射后差异里同时含"壳子"和"供给"两个因素，
报告里会显式提醒，不会假装是纯 harness 效应）。
"""
from __future__ import annotations

import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from aipk.harness import RunResult  # noqa: E402
from aipk.metrics import check_breakdown  # noqa: E402
from aipk.report import FAMILY_LABEL, _check_label  # noqa: E402
from aipk.runner import load_run  # noqa: E402


def _mean(xs: list[int]) -> float:
    return sum(xs) / len(xs) if xs else 0.0


def compare(ra: list[RunResult], rb: list[RunResult], na: str, nb: str,
            mapping: dict[str, str] | None = None) -> str:
    mapping = mapping or {}
    profs = {p for r in ra + rb if not r.infra_failure for p in [r.profile]}
    keys_a = {r.task_key for r in ra if not r.infra_failure}
    keys_b = {r.task_key for r in rb if not r.infra_failure}
    common = sorted(keys_a & keys_b)
    models = sorted({r.model_key for r in ra} & {r.model_key for r in rb})

    L: list[str] = []
    L.append("# Harness 对比：冻结协议 vs 真实 CLI")
    L.append("")
    L.append(f"- A = **{na}**（profile: {sorted({r.profile for r in ra})}）")
    L.append(f"- B = **{nb}**（profile: {sorted({r.profile for r in rb})}）")
    L.append(f"- 共同模型：{', '.join(models) or '（无）'}")
    L.append(f"- 共同任务：{len(common)} 个（A 独有 {len(keys_a - keys_b)}，B 独有 {len(keys_b - keys_a)}）")
    if mapping:
        L.append("- 已做的模型 id 映射：" + "、".join(f"`{k}` → `{v}`" for k, v in mapping.items()))
        L.append("")
        L.append("> ⚠️ 映射意味着两边是**同一模型族的不同供给**（网关/服务方不同）。"
                 "所以下面看到的差异里同时含「壳子」和「供给」两个因素，"
                 "**不能单独归因成 harness 效应**；能确定的是量级，不是精确归因。")
    L.append("")

    if not models or not common:
        L.append("> 没有可比交集 —— 两边要跑同一批模型 × 同一批任务才能算 harness 敏感度。")
        return "\n".join(L)

    # 同题配对：按 (task_key, model, rep) 对齐
    def index(rs: list[RunResult]) -> dict[tuple[str, str, int], RunResult]:
        return {(r.task_key, r.model_key, r.rep): r for r in rs if not r.infra_failure}

    ia, ib = index(ra), index(rb)
    for m in models:
        L.append(f"## {m}")
        L.append("")
        pairs = [(ia[k], ib[k]) for k in ia if k in ib and k[1] == m]
        if not pairs:
            L.append("（没有同题配对）")
            L.append("")
            continue
        a_win = b_win = both = neither = 0
        for x, y in pairs:
            if x.solved and y.solved:
                both += 1
            elif x.solved:
                a_win += 1
            elif y.solved:
                b_win += 1
            else:
                neither += 1
        n = len(pairs)
        L.append(f"同题配对 {n} 组：**A 独赢 {a_win}**、**B 独赢 {b_win}**、都过 {both}、都挂 {neither}")
        L.append("")
        rows = []
        for label, rs in ((na, [x for x, _ in pairs]), (nb, [y for _, y in pairs])):
            n_ok = sum(1 for r in rs if r.solved)
            est = sum(1 for r in rs if r.usage_estimated)
            rows.append((label, n_ok / len(rs) * 100, _mean([r.turns for r in rs]),
                         _mean([r.tool_calls for r in rs]),
                         _mean([r.prompt_tokens + r.completion_tokens for r in rs]),
                         _mean([r.total_ms for r in rs]) / 1000.0, est))
        L.append("| 壳子 | 成功率 | 平均轮数 | 平均内部步骤 | 平均 token | 平均耗时 | 用量为估算的条数 |")
        L.append("|---|---|---|---|---|---|---|")
        for label, rate, turns, steps, toks, secs, est in rows:
            L.append(f"| {label} | {rate:.1f}% | {turns:.1f} | {steps:.1f} "
                     f"| {toks:.0f}{'≈' if est else ''} | {secs:.1f}s | {est} |")
        L.append("")
        L.append("> 「轮数」两边口径不同：冻结 harness 是模型调用次数；"
                 "外部 CLI 自己只报 1 个 turn，它的自主步数要看「内部步骤」。")
        L.append("")
        # 子检查对比（机制信号）
        chk_a = check_breakdown([x for x, _ in pairs])
        chk_b = check_breakdown([y for _, y in pairs])
        names = sorted({n for c in (chk_a, chk_b) for fam in c for n in c[fam]})
        if names:
            L.append("| 任务族 | 子检查 | A | B |")
            L.append("|---|---|---|---|")
            for fam in sorted(set(chk_a) | set(chk_b)):
                for name in sorted(set(chk_a.get(fam, {})) | set(chk_b.get(fam, {}))):
                    da = chk_a.get(fam, {}).get(name)
                    db = chk_b.get(fam, {}).get(name)
                    va = f"{da['rate'] * 100:.0f}%" if da else "—"
                    vb = f"{db['rate'] * 100:.0f}%" if db else "—"
                    L.append(f"| {FAMILY_LABEL.get(fam, fam)} | {_check_label(name)} | {va} | {vb} |")
            L.append("")
        L.append("")

    L.append("## 怎么读")
    L.append("")
    L.append("- 同题配对里 **A 独赢 / B 独赢** 的条数，就是「换壳子改变了结论」的直接计数；")
    L.append("  两边都过（或都挂）的题在这个维度上没有信息，别拿来撑结论。")
    L.append("- 外部 CLI 的 token 常常不报用量，此时按字符数估算并标 `≈`，"
             "跨壳子比成本只能当量级参考。")
    L.append("- 结论强度：**仅限这批任务 + 这次配置**。这是 harness 敏感度，不是模型能力排名。")
    L.append("")
    return "\n".join(L)


def main(argv: list[str]) -> int:
    args = argv[1:]
    mapping: dict[str, str] = {}
    labels: dict[str, str] = {}
    pos: list[str] = []
    i = 0
    while i < len(args):
        a = args[i]
        if a == "--map" and i + 1 < len(args):
            src, _, dst = args[i + 1].partition("=")
            mapping[src.strip()] = dst.strip()
            i += 2
            continue
        if a in ("--label-a", "--label-b") and i + 1 < len(args):
            labels[a] = args[i + 1]
            i += 2
            continue
        pos.append(a)
        i += 1
    if len(pos) < 2:
        print(__doc__)
        return 2
    da, db = Path(pos[0]), Path(pos[1])
    ra, rb = load_run(da / "runs.jsonl"), load_run(db / "runs.jsonl")
    if mapping:
        for r in rb:
            r.model_key = mapping.get(r.model_key, r.model_key)
    print(f"读入 A={len(ra)} 条（{da}）、B={len(rb)} 条（{db}）"
          + (f"，映射 {mapping}" if mapping else ""))
    md = compare(ra, rb, labels.get("--label-a", da.name), labels.get("--label-b", db.name),
                 mapping)
    out = Path("runs/HARNESS_COMPARE.md")
    out.write_text(md, encoding="utf-8")
    # Windows 控制台默认 GBK，直接 print 全文会因为个别符号炸掉 —— 只打印要点
    try:
        sys.stdout.reconfigure(errors="replace")  # type: ignore[attr-defined]
        print(md)
    except Exception:  # noqa: BLE001
        for line in md.splitlines():
            try:
                print(line)
            except Exception:  # noqa: BLE001
                print(line.encode("ascii", "replace").decode("ascii"))
    print(f"\n已写入 {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
