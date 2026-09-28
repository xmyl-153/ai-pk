"""运行器：编排 (模型 × 任务族 × replicate × harness profile)，原始证据全落盘。"""
from __future__ import annotations

import concurrent.futures as cf
import json
import random
import threading
import time
from dataclasses import asdict
from pathlib import Path

from .config import ROSTER, ModelSpec, RunConfig, load_gateways
from .grade import Judge
from .harness import PROFILES, FrozenHarness, RunResult
from .provider import Provider
from .tasks import all_families, derive_seed, make


def _result_to_dict(r: RunResult) -> dict:
    d = asdict(r)
    d["grade"] = asdict(r.grade) if r.grade else None
    d["solved"] = r.solved
    d["reward"] = r.reward
    return d


class Runner:
    def __init__(self, cfg: RunConfig, profile_name: str = "frozen-v1", verbose: bool = True,
                 harness=None):
        """harness 不为 None 时用它替代冻结 harness（Phase B：真实 CLI 当壳子）。"""
        self.cfg = cfg
        self.profile_name = profile_name
        self.profile = PROFILES.get(profile_name)
        self.ext_harness = harness
        self.gateways = load_gateways()
        self.verbose = verbose
        self._lock = threading.Lock()
        self._providers: dict[str, Provider] = {}
        self.run_id = time.strftime("%Y%m%d-%H%M%S")
        self.out_dir = cfg.out_dir / self.run_id
        self.out_dir.mkdir(parents=True, exist_ok=True)
        self._log_path = self.out_dir / "runs.jsonl"
        self._log = self._log_path.open("a", encoding="utf-8")

    def provider(self, spec: ModelSpec) -> Provider:
        with self._lock:
            if spec.key not in self._providers:
                if spec.provider_id == "scripted":
                    # 离线演示：不连网关、不需要 key（见 aipk/scripted.py）
                    from .scripted import ScriptedProvider
                    self._providers[spec.key] = ScriptedProvider(spec, spec.model_id)  # type: ignore[assignment]
                else:
                    self._providers[spec.key] = Provider(spec, self.gateways,
                                                        timeout_s=self.cfg.timeout_s,
                                                        qps=self.cfg.qps_per_gateway)
            return self._providers[spec.key]

    def _say(self, msg: str) -> None:
        if self.verbose:
            print(msg, flush=True)

    def run(self, models: list[ModelSpec] | None = None,
            families: list[str] | None = None,
            judge_models: list[str] | None = None) -> tuple[list[RunResult], Path]:
        models = models or self.cfg.models()
        families = families or self.cfg.families or all_families()
        # 实例集固定为 (族 × 每族实例数)；reps 是**对同一实例重复跑**。
        # 之前把 rep 也塞进 seed，导致每个实例只跑一次 —— pass^k 退化成成功率，
        # "一致性"恒等于 0，等于没测稳定性。
        instances = []
        for fam in families:
            for tno in range(self.cfg.tasks_per_family):
                instances.append(make(fam, derive_seed(self.cfg.seed, 0, tno, fam)))
        tasks = [(inst, rep) for rep in range(self.cfg.reps) for inst in instances]

        harness = self.ext_harness or FrozenHarness(self.profile, max_turns=self.cfg.max_turns,
                                                    temperature=self.cfg.temperature,
                                                    max_tokens=self.cfg.max_tokens)
        total = len(models) * len(tasks)
        self._say(f"[run {self.run_id}] profile={self.profile_name} models={len(models)} "
                  f"tasks={len(tasks)} 组合={total} seed={self.cfg.seed} reps={self.cfg.reps}")

        judge = None
        judge_keys: set[str] = set()      # 归一化后的裁判 key，用于"裁判不评自己"
        jmodels = judge_models if judge_models is not None else self.cfg.judge_models
        if jmodels:
            try:
                specs = [ModelSpec(*self._split_key(k)) for k in jmodels]
                # 关键：排除判据必须用**归一化后的模型 key**。
                # 踩过的坑：CLI 传的是裸模型名 `deepseek-flash`，而运行记录的 key 是
                # `jiyuanapi/deepseek-flash`，两者不相等 → 自偏好排除静默失效，
                # 裁判给自己的答案打了分（这一轮 writing 的 deepseek-flash 格就废了）。
                judge_keys = {s.key for s in specs}
                jprovs = [self.provider(s) for s in specs]
                judge = Judge(jprovs)
                self._say(f"  [judge] 盲评已启用：{', '.join(sorted(judge_keys))}"
                          f"（这几个模型的答案按设计排除，避免自偏好）")
            except Exception as e:  # noqa: BLE001
                # 不能静默降级：整轮盲评会凭空消失，报告里却看不出为什么
                self._say(f"  [WARN] 盲评**未启用**（裁判不可用）：{type(e).__name__}: {e}")
                self._say(f"  [WARN] 本轮不会有盲评分数 —— 要盲评请修好裁判模型名后重跑")

        results: list[RunResult] = []
        done = 0
        t0 = time.perf_counter()

        def one(spec: ModelSpec, inst, rep_idx: int) -> RunResult:
            # 外部 CLI harness 自己跟模型说话，不需要这里的 Provider
            prov = None if self.ext_harness else self.provider(spec)
            last: RunResult | None = None
            # 基础设施故障（限流/网络）自动重投，最多重试 infra_retries 次
            for _ in range(1 + self.cfg.infra_retries):
                try:
                    res = harness.run(prov, inst, seed=inst.seed, rep=rep_idx)
                except Exception as e:  # noqa: BLE001
                    res = RunResult(model_key=spec.key, task_key=inst.key, profile=self.profile_name,
                                    seed=inst.seed, rep=rep_idx, infra_failure=True,
                                    error=f"harness crash: {type(e).__name__}: {e}"[:300])
                last = res
                if not res.infra_failure:
                    break
                time.sleep(3.0)
            res = last  # type: ignore[assignment]
            if judge and inst.judge_prompt and spec.key not in judge_keys and not res.infra_failure:
                # 裁判不评自己（自偏好偏差），自己那格记为空
                try:
                    v = judge.compare(res.final_answer or "", inst.reference, inst.judge_prompt)
                    if v.invalid:
                        # 没判出来 ≠ 平局：不写分数，只记无效（否则等于白送 0.5 分）
                        res.transcript.append({"judge_invalid": v.raw})
                    else:
                        # 以"相对参考解"归一：赢=1.0 平=0.5 输=0
                        res.judge_scores["vs_reference"] = {"1": 1.0, "2": 0.0, "tie": 0.5}[v.winner]
                        res.transcript.append({"judge": v.raw, "flipped": v.flipped,
                                               "winner": v.winner})
                except Exception as e:  # noqa: BLE001
                    res.transcript.append({"judge_error": f"{type(e).__name__}: {e}"[:200]})
            return res

        # 打乱执行顺序：按模型顺序依次提交会让"排在后面的模型"在限流时吃亏，
        # 那是把队列位置记成模型能力。
        jobs = [(spec, inst, rep) for spec in models for inst, rep in tasks]
        random.Random(self.cfg.seed).shuffle(jobs)

        with cf.ThreadPoolExecutor(max_workers=max(1, self.cfg.max_workers)) as ex:
            futures = {ex.submit(one, spec, inst, rep): (spec, inst, rep) for spec, inst, rep in jobs}
            for fut in cf.as_completed(futures):
                spec, inst, rep = futures[fut]
                try:
                    res = fut.result()
                except Exception as e:  # noqa: BLE001
                    self._say(f"  [err] {spec.key} {inst.key}: {e}")
                    continue
                results.append(res)
                with self._lock:
                    self._log.write(json.dumps(_result_to_dict(res), ensure_ascii=False) + "\n")
                    self._log.flush()
                    done += 1
                    if res.infra_failure:
                        mark = "INF"
                    else:
                        mark = "OK " if res.solved else ("ERR" if res.error else "X  ")
                    self._say(f"  [{done:>4}/{total}] {mark} {spec.short:<22s} {inst.key:<28s} "
                              f"turns={res.turns} ttft={res.ttft_ms} "
                              f"tok={res.prompt_tokens + res.completion_tokens} "
                              f"{(res.grade.reason if res.grade else '')[:60]}")

        self._log.close()
        for p in self._providers.values():
            p.close()
        dt = time.perf_counter() - t0
        n_infra = sum(1 for r in results if r.infra_failure)
        self._say(f"[run {self.run_id}] 完成 {len(results)} 次运行（其中基础设施故障 {n_infra} 次，已排除），"
                  f"耗时 {dt:.0f}s -> {self.out_dir}")
        return results, self.out_dir

    @staticmethod
    def _split_key(key: str) -> tuple[str, str, str, str]:
        """接受 `provider/model` 或只写模型名（从名单里查）。

        只写模型名时早期代码直接抛 ValueError，而调用处的 try/except 把它变成
        "裁判不可用，跳过盲评" —— 整轮盲评静默消失。这种静默失败必须堵住。
        """
        if "/" in key:
            prov, model = key.split("/", 1)
            return prov, model, key, "judge"
        for prov, model, name, _tier in ROSTER:
            if key in (model, name):
                return prov, model, f"{prov}/{model}", "judge"
        raise KeyError(f"认不出这个裁判：{key}（写 provider/model，或从 `python -m aipk list` 里挑）")


def load_run(path: Path) -> list[RunResult]:
    """从 runs.jsonl 读回运行结果（用于离线重算报告）。"""
    out: list[RunResult] = []
    with path.open(encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            d = json.loads(line)
            g = d.pop("grade", None)
            d.pop("solved", None)
            d.pop("reward", None)
            res = RunResult(**{k: v for k, v in d.items() if k in RunResult.__dataclass_fields__})
            if g:
                from .tasks import GradeResult
                res.grade = GradeResult(**{k: v for k, v in g.items() if k in GradeResult.__dataclass_fields__})
            out.append(res)
    return out
