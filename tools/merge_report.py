"""把主运行 + 敏感度实验的三个 run 目录合并成一份总报告。"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from aipk.report import build_report  # noqa: E402
from aipk.runner import load_run  # noqa: E402

RUNS = ["runs/20260923-203751", "runs/20260923-225434", "runs/20260923-230157"]

recs = []
for d in RUNS:
    recs += load_run(Path(d) / "runs.jsonl")
out = Path("runs/COMBINED")
out.mkdir(exist_ok=True)
print(f"合并 {len(recs)} 条记录 -> {out}")
for p in build_report(recs, out):
    print("  ", p)
