"""看某一次 run 的原始记录：谁失败了、为什么、错误长什么样。

排查"某个模型表现异常差"的第一步 —— 本项目 14 个测量缺陷几乎都是这么查出来的。

用法：
    python tools/inspect_run.py runs/<id>/runs.jsonl                 # 概览：每个模型的成功/失败
    python tools/inspect_run.py runs/<id>/runs.jsonl --model kimi    # 只看匹配的模型
    python tools/inspect_run.py runs/<id>/runs.jsonl --errors        # 只列去重后的错误
"""
from __future__ import annotations

import argparse
import collections
import json
import sys
from pathlib import Path


def load(path: Path) -> list[dict]:
    return [json.loads(l) for l in path.open(encoding="utf-8") if l.strip()]


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description="查看一次 run 的原始记录")
    ap.add_argument("runs_jsonl", type=Path)
    ap.add_argument("--model", help="只看模型名里含这个子串的记录")
    ap.add_argument("--errors", action="store_true", help="只列去重后的错误")
    a = ap.parse_args(argv[1:])

    rows = load(a.runs_jsonl)
    if a.model:
        rows = [r for r in rows if a.model in r["model_key"]]
    if not rows:
        print("没有匹配的记录")
        return 1

    if a.errors:
        seen = collections.Counter()
        first: dict[str, dict] = {}
        for r in rows:
            if not r.get("error"):
                continue
            key = str(r["error"])[:160]
            seen[key] += 1
            first.setdefault(key, r)
        if not seen:
            print(f"{len(rows)} 条记录里没有任何 error 字段")
            return 0
        for msg, n in seen.most_common():
            r = first[msg]
            print(f"[{n} 次] {r['model_key']}  infra_failure={r['infra_failure']}")
            print(f"    {msg}")
            print(f"    完整：{str(r['error'])[:600]}")
        return 0

    per = collections.defaultdict(lambda: [0, 0, 0, 0])   # solved, total, infra, degraded
    for r in rows:
        s = per[r["model_key"]]
        s[1] += 1
        s[0] += 1 if r["solved"] else 0
        s[2] += 1 if r["infra_failure"] else 0
        s[3] += 1 if r.get("param_degraded") else 0
    print(f"{len(rows)} 条记录\n")
    print(f"{'模型':<34}{'成功':>10}{'基础设施故障':>14}{'参数降级':>10}")
    for m, (ok, n, infra, deg) in sorted(per.items(), key=lambda kv: -(kv[1][0] / max(1, kv[1][1]))):
        print(f"{m:<34}{f'{ok}/{n}':>10}{infra:>14}{deg:>10}")
    bad = [r for r in rows if not r["solved"] and not r["infra_failure"]]
    if bad:
        print(f"\n未解决（排除基础设施故障后）{len(bad)} 条：")
        for r in bad[:20]:
            reason = (r.get("grade") or {}).get("reason", "")
            print(f"  {r['model_key']:<32} {r['task_key']:<28} rep{r['rep']}  {reason[:90]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
