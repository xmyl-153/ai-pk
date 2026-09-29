"""AI PK —— 命令行入口。

用法：
  python -m aipk list                         列出可用模型与网关
  python -m aipk selfcheck                    任务族自检（生成器+oracle 是否自洽）
  python -m aipk preview [--family F]         预览一个任务实例（看题目长什么样）
  python -m aipk smoke [--model KEY]          单模型单任务冒烟
  python -m aipk run [--seed N] [--reps K]    正式 PK
  python -m aipk report [--run DIR]           生成 HTML/MD 报告
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .config import ROSTER, KNOWN_UNAVAILABLE, ModelSpec, RunConfig, load_gateways


def cmd_list(_args) -> int:
    gws = load_gateways()
    print("网关：")
    for pid, gw in gws.items():
        state = "有密钥" if gw["api_key"] else "缺密钥"
        print(f"  {pid:<16s} {gw['display_name']:<12s} {state:<6s} {gw['base_url']}")
    print("\n首战名单：")
    for prov, model, name, tier in ROSTER:
        print(f"  {tier:<9s} {name:<22s} {prov}/{model}")
    if KNOWN_UNAVAILABLE:
        print("\n已知不可用（避免重复踩坑）：")
        for k, why in KNOWN_UNAVAILABLE.items():
            print(f"  {k:<34s} {why}")
    return 0


def cmd_selfcheck(_args) -> int:
    from .tasks import all_families, make
    print("任务族自检（每个族生成 3 个实例，用参考解自测 oracle）：")
    bad = 0
    for fam in all_families():
        seen: dict[str, int] = {}
        for tno, seed in enumerate((11, 22, 33)):
            try:
                inst = make(fam, seed)
            except Exception as e:  # noqa: BLE001
                print(f"  [FAIL] {fam} seed={seed} 生成失败：{type(e).__name__}: {e}")
                bad += 1
                continue
            # 用 meta 里的真值构造"标准答案"喂给 oracle，必须判对
            truth_answer = _truth_answer(fam, inst)
            if truth_answer is None:
                print(f"  [skip] {fam} seed={seed} 无法自动构造真值答案（需人工确认）")
                continue
            g = inst.grader(truth_answer)
            flag = "OK  " if g.solved else "FAIL"
            if not g.solved:
                bad += 1
            print(f"  [{flag}] {fam:<14s} seed={seed:<4d} 真值应判对 → {g.reason[:70]}")
            # 额外检查：同族不同 seed 必须生成不同实例（否则 reps 是假重复）
            sig = json.dumps(inst.messages, ensure_ascii=False)[:400]
            if sig in seen:
                print(f"  [FAIL] {fam} seed={seed} 与 seed={seen[sig]} 生成了完全相同的题目")
                bad += 1
            seen[sig] = seed
    print("\n结论：" + ("全部自洽" if bad == 0 else f"{bad} 处不自洽，必须修"))
    return 0 if bad == 0 else 1


def _truth_answer(fam: str, inst) -> str | None:
    """标准答案构造已挪到 aipk/oracle.py（自检与离线演示共用同一套口径）。"""
    from .oracle import truth_answer
    return truth_answer(fam, inst)


def cmd_preview(args) -> int:
    from .tasks import all_families, make
    fams = [args.family] if args.family else all_families()
    for fam in fams:
        inst = make(fam, args.seed)
        print("=" * 78)
        print(f"任务族 {fam}  |  id={inst.tid}  |  seed={inst.seed}  |  需要工具={inst.needs_tools}")
        print(f"作答格式：{inst.answer_spec}")
        print("-" * 78)
        for m in inst.messages:
            content = m["content"]
            print(f"[{m['role']}] {content[:1500]}{'…' if len(content) > 1500 else ''}")
            print()
        print(f"[meta] {json.dumps({k: v for k, v in inst.meta.items() if k != 'files'}, ensure_ascii=False, default=str)[:600]}")
    return 0


def cmd_smoke(args) -> int:
    from .runner import Runner
    from .tasks import make
    cfg = RunConfig(seed=args.seed, reps=1, families=[args.family], max_turns=args.max_turns)
    spec = ModelSpec(*ROSTER[0])
    if args.model:
        for r in ROSTER:
            if args.model in (r[1], f"{r[0]}/{r[1]}"):
                spec = ModelSpec(*r)
                break
    runner = Runner(cfg, profile_name=args.profile)
    print(f"冒烟：{spec.key} × {args.family}")
    results, out = runner.run(models=[spec], families=[args.family], judge_models=[])
    for r in results:
        print(f"\n最终答案：{r.final_answer[:800]}")
        print(f"判定：solved={r.solved} reward={r.reward} 理由={r.grade.reason if r.grade else None}")
        print(f"指标：turns={r.turns} ttft={r.ttft_ms}ms total={r.total_ms}ms "
              f"tok={r.prompt_tokens}+{r.completion_tokens}(reasoning {r.reasoning_tokens})")
        print(f"证据：{out}")
    return 0


def cmd_run(args) -> int:
    from .runner import Runner
    cfg = RunConfig(seed=args.seed, reps=args.reps, max_turns=args.max_turns,
                    temperature=args.temperature, max_tokens=args.max_tokens,
                    tasks_per_family=args.tasks_per_family,
                    qps_per_gateway=args.qps, max_workers=args.workers,
                    infra_retries=args.infra_retries, judge_strict=args.judge_strict)
    models = [ModelSpec(*r) for r in ROSTER]
    if args.models:
        want = {x.strip() for x in args.models.split(",")}
        models = [m for m in models if m.model_id in want or m.key in want or m.name in want]
    families = args.families.split(",") if args.families else None
    profiles = args.profiles.split(",") if args.profiles else ["frozen-v1"]
    judge_models = [] if args.no_judge else (args.judge.split(",") if args.judge else None)

    all_results = []
    for prof in profiles:
        runner = Runner(cfg, profile_name=prof)
        results, out = runner.run(models=models, families=families, judge_models=judge_models)
        all_results.append((prof, out, results))
    print("\n运行目录：")
    for prof, out, _ in all_results:
        print(f"  {prof}: {out}")
    return 0


def cmd_external(args) -> int:
    """Phase B：把真实 agent CLI 当成"壳子"跑同一批任务，出 harness 敏感度。"""
    from .harness import ExternalCLI, ExternalHarness
    from .runner import Runner

    cfg = RunConfig(seed=args.seed, reps=args.reps, tasks_per_family=args.tasks_per_family,
                    max_turns=args.max_turns, qps_per_gateway=args.qps, max_workers=args.workers)
    families = args.families.split(",") if args.families else ["premise"]
    if "/" in args.model_key:
        prov, mid = args.model_key.split("/", 1)
    else:
        prov, mid = "external", args.model_key
    spec = ModelSpec(prov, mid, args.model_key, "external")
    cli = ExternalCLI(name=args.name, model=args.model, model_key=spec.key,
                      timeout_s=args.timeout)
    if args.cmd:
        cli.cmd = args.cmd.split()
    if args.provider:
        # 把外部 CLI 指到本项目**同一个网关**上：这样壳子变了、模型没变，
        # 差异才能归因给 harness（密钥只进子进程环境变量，不落盘、不进命令行）。
        gws = load_gateways()
        gw = gws.get(args.provider)
        if not gw or not gw.get("api_key"):
            print(f"网关 {args.provider} 不存在或没有密钥，可用：{sorted(gws)}")
            return 2
        envname = "AIPK_EXT_GATEWAY_KEY"
        cli.env[envname] = gw["api_key"]
        # Codex 0.155 只认 responses（chat 已被移除）—— 本机网关实测支持 /responses，
        # 所以能把外部 CLI 指到同一个网关上的同一个模型：壳子变了、模型和供给都没变。
        cli.config_overrides = [
            'model_providers.aipk.name="aipk-%s"' % args.provider,
            'model_providers.aipk.base_url="%s"' % gw["base_url"],
            'model_providers.aipk.env_key="%s"' % envname,
            'model_providers.aipk.wire_api="responses"',
            'model_provider="aipk"',
            'model_reasoning_effort="%s"' % args.reasoning_effort,
        ]
        print(f"网关对齐：{args.provider} → {gw['base_url']}（密钥走子进程环境变量）")
        if not args.model:
            print("提示：加了 --provider 却没给 --model，CLI 可能仍在用自己配置里的模型 id")
    if args.env_cred:
        # 把 DSH 里的一条凭据注入外部 CLI 的子进程环境（例如 Codex 的 DEEPSEEK_API_KEY）
        from .config import load_credential
        for name in args.env_cred.split(","):
            name = name.strip()
            if not name:
                continue
            try:
                cli.env[name] = load_credential(name)
                print(f"环境变量注入：{name}（长度 {len(cli.env[name])}，不落盘）")
            except KeyError as e:
                print(str(e))
                return 2
    harness = ExternalHarness(cli)
    print(f"外部 harness：{cli.name}　命令={' '.join(cli.cmd)}　模型={cli.model or '(CLI 默认)'}")
    print(f"榜单 id={spec.key}　任务族={families}　实例/族={args.tasks_per_family}　reps={args.reps}")
    print(f"超时={cli.timeout_s:.0f}s　并发={args.workers}")
    runner = Runner(cfg, profile_name=cli.name, harness=harness)
    results, out = runner.run(models=[spec], families=families, judge_models=[])
    print(f"\n运行目录：{out}")
    n_solved = sum(1 for r in results if r.solved)
    n_infra = sum(1 for r in results if r.infra_failure)
    print(f"完成 {len(results)} 次：成功 {n_solved}，基础设施故障 {n_infra}")
    return 0


def cmd_demo(args) -> int:
    """离线跑通全链路：不需要任何 API key、不花一分钱。

    满分机器人（scripted-oracle）应当 100% 通过；故意答错的机器人（scripted-wrong）
    应当 0% 通过。两者都对，才说明"测量装置"是好的 —— 这是本项目的端到端自检。
    """
    from .config import ModelSpec
    from .report import build_report
    from .runner import Runner

    cfg = RunConfig(seed=args.seed, reps=args.reps, tasks_per_family=args.tasks_per_family,
                    max_turns=args.max_turns, qps_per_gateway=args.qps, max_workers=args.workers)
    families = args.families.split(",") if args.families else None
    models = [ModelSpec("scripted", "oracle", "scripted-oracle", "offline")]
    if not args.oracle_only:
        models.append(ModelSpec("scripted", "wrong", "scripted-wrong", "offline"))
    print("离线演示：不连网关、不需要 key。")
    print("  scripted-oracle 应当 100% 通过；scripted-wrong 应当 0% 通过。")
    # gateways={} = 这台机器一个网关都不需要：demo 跑的是 scripted 模型，
    # 没有 aipk.config.yaml / ~/.dsh 也照样得能跑通（缺陷 #20）。
    runner = Runner(cfg, profile_name=args.profile, gateways={})
    results, out = runner.run(models=models, families=families, judge_models=[])
    per: dict[str, list[int]] = {}
    for r in results:
        if r.infra_failure:
            continue
        per.setdefault(r.model_key, []).append(1 if r.solved else 0)
    print("\n结果：")
    ok = True
    for k, v in sorted(per.items()):
        rate = sum(v) / len(v)
        want = 1.0 if k.endswith("oracle") else 0.0
        flag = "OK  " if abs(rate - want) < 1e-9 else "FAIL"
        if flag == "FAIL":
            ok = False
        print(f"  [{flag}] {k:<22s} 通过率 {rate * 100:.1f}%（期望 {want * 100:.0f}%）")
    build_report(results, out)
    print(f"\n报告：{out / 'REPORT.md'}（HTML 同目录）")
    print("结论：" + ("测量装置自洽（满分全过、错答全挂）" if ok else
                     "⚠ 有偏差 —— 某个族的 grader 或 harness 可能坏了"))
    return 0 if ok else 1


def cmd_init(args) -> int:
    """生成一份配置模板，方便别人接自己的网关。"""
    from .config import USER_CONFIG_NAMES, example_config_text
    target = Path(args.path or USER_CONFIG_NAMES[0])
    if target.exists() and not args.force:
        print(f"{target} 已存在（要覆盖加 --force）")
        return 1
    target.write_text(example_config_text(), encoding="utf-8")
    print(f"已写入 {target}")
    print("接下来：填好 providers（base_url + api_key_env）和 roster，然后：")
    print("  python -m aipk list        # 确认网关与名单读到了")
    print("  python -m aipk demo        # 不需要 key 也能跑通全链路")
    print("  python -m aipk smoke --model <你的模型名> --family premise")
    return 0


def cmd_report(args) -> int:
    from .report import build_report
    from .runner import load_run
    runs_root = Path(args.run) if args.run else _latest_run()
    if runs_root is None:
        print("找不到运行目录，先跑 `python -m aipk run`")
        return 1
    results = load_run(runs_root / "runs.jsonl")
    print(f"读入 {len(results)} 条运行记录：{runs_root}")
    paths = build_report(results, runs_root, alt_run=Path(args.alt) if args.alt else None)
    for p in paths:
        print(f"  生成 {p}")
    return 0


def _latest_run() -> Path | None:
    root = Path(__file__).resolve().parent.parent / "runs"
    if not root.exists():
        return None
    dirs = sorted([d for d in root.iterdir() if d.is_dir() and (d / "runs.jsonl").exists()])
    return dirs[-1] if dirs else None


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="aipk", description="AI PK —— 用固定综合任务测模型真实质量")
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("list", help="列出网关与首战名单").set_defaults(func=cmd_list)
    sub.add_parser("selfcheck", help="任务族自检").set_defaults(func=cmd_selfcheck)

    dm = sub.add_parser("demo", help="离线跑通全链路（不需要 API key）")
    dm.add_argument("--families", help="逗号分隔的任务族；不传=全部")
    dm.add_argument("--seed", type=int, default=2026)
    dm.add_argument("--reps", type=int, default=1)
    dm.add_argument("--tasks-per-family", type=int, default=1)
    dm.add_argument("--max-turns", type=int, default=12)
    dm.add_argument("--profile", default="frozen-v1")
    dm.add_argument("--oracle-only", action="store_true", help="只跑满分机器人")
    dm.add_argument("--qps", type=float, default=1.2)
    dm.add_argument("--workers", type=int, default=4)
    dm.set_defaults(func=cmd_demo)

    ini = sub.add_parser("init", help="生成配置模板（接自己的网关）")
    ini.add_argument("--path", help="写到指定路径（默认 aipk.config.yaml）")
    ini.add_argument("--force", action="store_true")
    ini.set_defaults(func=cmd_init)

    pv = sub.add_parser("preview", help="预览任务实例")
    pv.add_argument("--family")
    pv.add_argument("--seed", type=int, default=2026)
    pv.set_defaults(func=cmd_preview)

    sm = sub.add_parser("smoke", help="单模型单任务冒烟")
    sm.add_argument("--model")
    sm.add_argument("--family", default="constraint")
    sm.add_argument("--seed", type=int, default=2026)
    sm.add_argument("--profile", default="frozen-v1")
    sm.add_argument("--max-turns", type=int, default=12,
                    help="轮数上限；分阶段任务（decay）要放宽，否则会话会被截断")
    sm.set_defaults(func=cmd_smoke)

    rn = sub.add_parser("run", help="正式 PK")
    rn.add_argument("--seed", type=int, default=2026)
    rn.add_argument("--reps", type=int, default=3)
    rn.add_argument("--tasks-per-family", type=int, default=3,
                    help="每族实例数；1 个实例会让 pass^k 退化成成功率")
    rn.add_argument("--max-turns", type=int, default=12)
    rn.add_argument("--temperature", type=float, default=0.0)
    rn.add_argument("--max-tokens", type=int, default=8192)
    rn.add_argument("--models", help="逗号分隔的模型 id")
    rn.add_argument("--families", help="逗号分隔的任务族")
    rn.add_argument("--profiles", help="逗号分隔的 harness profile")
    rn.add_argument("--judge", help="逗号分隔的裁判模型；不传则用默认裁判")
    rn.add_argument("--no-judge", action="store_true", help="关闭盲评（省时间省钱）")
    rn.add_argument("--judge-strict", action="store_true",
                    help="严格盲评：位置翻转的裁决直接丢弃，不记平局（实测翻转率约 26%）")
    rn.add_argument("--qps", type=float, default=1.2, help="每网关限速（请求/秒），防被 429 打成假 0 分")
    rn.add_argument("--workers", type=int, default=6, help="并发上限")
    rn.add_argument("--infra-retries", type=int, default=2, help="基础设施故障重投次数")
    rn.set_defaults(func=cmd_run)

    rp = sub.add_parser("report", help="生成报告")
    rp.add_argument("--run")
    rp.add_argument("--alt", help="另一个 run 目录（用于 harness 敏感度对比）")
    rp.set_defaults(func=cmd_report)

    ex = sub.add_parser("external", help="Phase B：用真实 agent CLI 跑同一批任务")
    ex.add_argument("--families", default="premise", help="逗号分隔的任务族")
    ex.add_argument("--seed", type=int, default=2026)
    ex.add_argument("--reps", type=int, default=1)
    ex.add_argument("--tasks-per-family", type=int, default=3)
    ex.add_argument("--max-turns", type=int, default=20)
    ex.add_argument("--timeout", type=float, default=900.0, help="单次 CLI 调用超时（秒）")
    ex.add_argument("--name", default="codex-cli", help="profile 名（报告里按它分组）")
    ex.add_argument("--model", default=None, help="传给 CLI 的模型 id；不传用 CLI 自己的默认")
    ex.add_argument("--model-key", default="external/codex",
                    help="榜单里的模型 id；想和冻结 harness 的同一模型对齐就写同一个 key")
    ex.add_argument("--cmd", default=None, help="覆盖 CLI 命令（空格分隔）")
    ex.add_argument("--provider", default=None,
                    help="把外部 CLI 指到本项目同一个网关（provider id），实现同模型换壳子对比")
    ex.add_argument("--env-cred", default=None,
                    help="把 DSH 凭据里的某个 key 注入子进程环境（逗号分隔，如 DEEPSEEK_API_KEY）")
    ex.add_argument("--reasoning-effort", default="low", help="网关覆盖时给的 reasoning effort")
    ex.add_argument("--workers", type=int, default=2, help="外部 CLI 并发数（别开太大）")
    ex.add_argument("--qps", type=float, default=1.2)
    ex.set_defaults(func=cmd_external)

    args = p.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
