"""从真实证据里聚合每个模型的四维指标 + 不同加权下的排名。

用来回答"实测和大家印象为什么对不上"：把成本/速度/推理占比当一等维度加权后，
排名会怎么变（以 DeepSeek V4 Pro vs GLM 5.3 Flash 为例）。

用法：python tools/perception_gap.py [runs.jsonl 路径]
"""
import json
import sys
from collections import defaultdict

path = sys.argv[1] if len(sys.argv) > 1 else "runs/20260925-132828/runs.jsonl"
rows = [json.loads(l) for l in open(path, encoding="utf-8") if l.strip()]

per = defaultdict(lambda: {"n":0,"solved":0,"ms":0.0,"tok":0,"rea":0,"ttft":0.0,"inf":0,"turns":0.0})
for r in rows:
    m = per[r["model_key"]]
    if r.get("infra_failure"):
        m["inf"] += 1
        continue
    m["n"] += 1
    m["solved"] += 1 if r.get("solved") else 0
    m["ms"] += r.get("total_ms",0)
    m["tok"] += (r.get("prompt_tokens",0)+r.get("completion_tokens",0))
    m["rea"] += r.get("reasoning_tokens",0)
    m["ttft"] += r.get("ttft_ms") or 0
    m["turns"] += r.get("turns",0)

stats = {}
for k,m in per.items():
    if m["n"]==0: continue
    stats[k] = {
        "n": m["n"],
        "solve": m["solved"]/m["n"],
        "avg_ms": m["ms"]/m["n"],
        "avg_tok": m["tok"]/m["n"],
        "rea_ratio": (m["rea"]/(m["tok"]) if m["tok"] else 0.0),
        "avg_ttft_ms": m["ttft"]/m["n"],
        "avg_turns": m["turns"]/m["n"],
        "inf": m["inf"],
    }

def norm(vals, invert=False):
    lo,hi=min(vals),max(vals)
    if hi-lo<1e-9: return [0.5]*len(vals)
    out=[ (v-lo)/(hi-lo) for v in vals ]
    return [1-x for x in out] if invert else out

keys=list(stats)
Q=norm([stats[k]["solve"] for k in keys])
S=norm([stats[k]["avg_ms"] for k in keys], invert=True)
C=norm([stats[k]["avg_tok"] for k in keys], invert=True)
R=norm([stats[k]["rea_ratio"] for k in keys], invert=True)

presets={
 "性能优先": (0.65,0.15,0.10,0.10),
 "便宜优先": (0.30,0.15,0.45,0.10),
 "速度优先": (0.35,0.45,0.10,0.10),
 "均衡":     (0.40,0.20,0.20,0.20),
}
print(f"{'模型':<34}{'n':>4}{'正确率':>8}{'耗时ms':>9}{'token':>9}{'推理占比':>9}")
for k in sorted(keys,key=lambda x:-stats[x]['solve']):
    st=stats[k]
    print(f"{k:<34}{st['n']:>4}{st['solve']*100:>7.1f}%{st['avg_ms']:>9.0f}{st['avg_tok']:>9.0f}{st['rea_ratio']*100:>8.1f}%")

for name,(wq,ws,wc,wr) in presets.items():
    scores=[ wq*Q[i]+ws*S[i]+wc*C[i]+wr*R[i] for i in range(len(keys)) ]
    order=sorted(range(len(keys)),key=lambda i:-scores[i])
    print(f"\n=== {name} (质量{wq}/速度{ws}/成本{wc}/克制{wr}) ===")
    for rank,i in enumerate(order,1):
        tag="   <-- v4pro" if "v4-pro" in keys[i] else ("   <-- glm5.3flash" if keys[i]=="jiyuanapi/glm-5.3-flash" else "")
        print(f"  {rank}. {keys[i]:<34} {scores[i]*100:5.1f}{tag}")
