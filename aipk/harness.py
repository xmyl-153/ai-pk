"""冻结 harness：所有模型跑同一套提示词 + 同一套工具协议 + 同一轮数上限。

Phase A 的核心公平性保证都在这里。同时把 harness 做成**可变的 profile**：
同一模型在不同 profile 下跑，差值就是 harness 敏感度（真实体验差异的主要来源）。

Phase B 预留：`ExternalHarness` 接口，把真实 CLI（Claude Code / Codex / Cursor）
包成同样的 `run(task) -> RunResult`，复用同一任务集与 oracle。
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .provider import ChatResult, Provider, ToolCall
from .tasks import GradeResult, TaskInstance, run_python


# ---------------------------------------------------------------- 工作目录快照

# harness 自己写进工作区的协议文件：**不算模型的改动**。
# 踩过的坑：外部 CLI harness 一开始把 TASK.md 和 last-message 文件写在任务工作区里，
# 结果 mindiff 族把这两份协议文件算成"顺手改了 32 行、3 个文件"，
# 把三个正确答案全判成"diff 不最小" —— 典型的"看着像模型不行，其实是测量代码的锅"。
PROTOCOL_ARTIFACTS = {"TASK.md", "_last_message.txt", "ANSWER.json", "answer.json"}


def snapshot_dir(workdir: str | None, limit: int = 200_000) -> dict[str, str]:
    """把工作目录里的文本文件读成 {相对路径: 内容}。

    用途：像 mindiff 这种"判你改了几行"的任务，证据必须是模型**真正落盘的结果**，
    不能是它在 submit 里自称的 diff。构建脚本产生的缓存（__pycache__ / *.pyc）
    和 harness 自己写的临时片段不算模型改动，一律剔除。
    """
    out: dict[str, str] = {}
    if not workdir:
        return out
    root = Path(workdir)
    if not root.exists():
        return out
    for p in sorted(root.rglob("*")):
        if not p.is_file():
            continue
        rel = p.relative_to(root).as_posix()
        if "__pycache__" in rel or rel.endswith(".pyc") or rel == "_aipk_snippet.py":
            continue
        try:
            body = p.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        out[rel] = body[:limit]
    return out

# ---------------------------------------------------------------- 冻结工具协议

TOOL_SCHEMAS: dict[str, dict] = {
    "read_file": {
        "type": "function",
        "function": {
            "name": "read_file",
            "description": "读取虚拟文件系统中的文件内容。",
            "parameters": {
                "type": "object",
                "properties": {"path": {"type": "string", "description": "文件名"}},
                "required": ["path"],
            },
        },
    },
    "list_files": {
        "type": "function",
        "function": {
            "name": "list_files",
            "description": "列出虚拟文件系统中的所有文件名。",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    "run_python": {
        "type": "function",
        "function": {
            "name": "run_python",
            "description": "在沙箱中执行一段 Python 代码并返回 stdout/stderr。",
            "parameters": {
                "type": "object",
                "properties": {"code": {"type": "string", "description": "要执行的 Python 源码"}},
                "required": ["code"],
            },
        },
    },
    "submit": {
        "type": "function",
        "function": {
            "name": "submit",
            "description": "提交最终答案，结束任务。",
            "parameters": {
                "type": "object",
                "properties": {"answer": {"type": "string", "description": "最终答案（按题目要求的格式）"}},
                "required": ["answer"],
            },
        },
    },
}

BASE_SYSTEM = """你是一个被评测的 AI 助手。请独立完成任务。

规则：
1. 需要读取文件时调用 read_file；需要计算或验证时调用 run_python。
2. 任务完成后，必须调用 submit 提交最终答案，answer 字段里放题目要求的格式。
3. 不要编造你没有读到的内容。
4. 不要在最终答案里加入题目没要求的解释。"""


@dataclass
class HarnessProfile:
    """harness 变量。改这些就是在改"壳子"。"""
    name: str = "frozen-v1"
    tool_mode: str = "full"          # none | basic(read_file) | full
    inject_failures: bool = False    # 在第 2 次工具调用上注入故障
    system_style: str = "terse"      # terse | verbose —— 提示词风格变量
    require_submit: bool = True

    def tools(self) -> list[dict]:
        if self.tool_mode == "none":
            return []
        if self.tool_mode == "basic":
            return [TOOL_SCHEMAS["read_file"]]
        return [TOOL_SCHEMAS[k] for k in ("read_file", "list_files", "run_python", "submit")]

    def system_prompt(self) -> str:
        if self.system_style == "verbose":
            return BASE_SYSTEM + "\n5. 在调用工具前，先用一句话说明你打算做什么。"
        return BASE_SYSTEM


PROFILES: dict[str, HarnessProfile] = {
    "frozen-v1": HarnessProfile(),
    "tools-off": HarnessProfile(name="tools-off", tool_mode="none", require_submit=False),    "flaky-tools": HarnessProfile(name="flaky-tools", inject_failures=True),
    "verbose-style": HarnessProfile(name="verbose-style", system_style="verbose"),
}


# ---------------------------------------------------------------- 运行结果

@dataclass
class RunResult:
    model_key: str
    task_key: str
    profile: str
    seed: int
    rep: int
    final_answer: str = ""
    grade: GradeResult | None = None
    turns: int = 0
    tool_calls: int = 0
    wasted_tool_calls: int = 0        # 调了不存在的工具 / 参数缺失
    injected_failures: int = 0
    recovered_from_failure: bool = False
    used_submit: bool = False
    ttft_ms: int | None = None
    total_ms: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    reasoning_tokens: int = 0
    error: str | None = None
    infra_failure: bool = False       # True = 网关限流/网络故障，**不计入模型能力评分**
    usage_estimated: bool = False     # True = 该网关不报 usage，token 数是估算值
    param_degraded: bool = False      # True = 该模型不吃我们给的某个参数（如 temperature），已自动降级
    stages: int = 0                   # 分阶段任务实际完成的阶段数
    empty_outputs: int = 0
    # 工作目录在任务结束时的最终快照（相对路径 → 文件内容）。
    # mindiff 族要判"改了几行"，必须看**模型真正改完的仓库**，而不是它在答案里自称改了什么。
    workdir_files: dict[str, str] = field(default_factory=dict)
    transcript: list[dict] = field(default_factory=list)
    judge_scores: dict[str, float] = field(default_factory=dict)

    @property
    def solved(self) -> bool:
        return bool(self.grade and self.grade.solved)

    @property
    def reward(self) -> float:
        return self.grade.reward if self.grade else 0.0


# ---------------------------------------------------------------- 执行器


class FrozenHarness:
    def __init__(self, profile: HarnessProfile | None = None, max_turns: int = 12,
                 temperature: float = 0.0, max_tokens: int = 8192):
        self.profile = profile or PROFILES["frozen-v1"]
        self.max_turns = max_turns
        self.temperature = temperature
        self.max_tokens = max_tokens

    def run(self, provider: Provider, task: TaskInstance, *, seed: int = 0, rep: int = 0) -> RunResult:
        res = RunResult(model_key=provider.spec.key, task_key=task.key, profile=self.profile.name,
                        seed=seed, rep=rep)
        messages: list[dict] = []
        if self.profile.tools():
            messages.append({"role": "system", "content": self.profile.system_prompt()})
        messages += [dict(m) for m in task.messages]
        messages.append({"role": "user", "content": f"作答格式要求：{task.answer_spec}\n"
                                                    f"完成后请调用 submit 提交。" if self.profile.require_submit
                                                    else f"作答格式要求：{task.answer_spec}"})

        vfs: dict[str, str] = dict(task.meta.get("files") or {})
        stage = 0
        # 任务带仓库时把文件真正落到磁盘，run_python 才能 import 并跑它 ——
        # 这是 repofix 族"必须真跑代码"成立的前提。
        workdir: str | None = None
        if vfs and task.needs_tools and "run_python" in {t["function"]["name"] for t in self.profile.tools()}:
            workdir = tempfile.mkdtemp(prefix="aipk-wd-")
            for rel, body in vfs.items():
                p = Path(workdir) / rel
                p.parent.mkdir(parents=True, exist_ok=True)
                p.write_text(body, encoding="utf-8")
        tools = self.profile.tools()
        tool_names = {t["function"]["name"] for t in tools}
        # 离线演示 provider 需要知道"当前是哪道题"（真 Provider 没有这个钩子）
        if hasattr(provider, "set_task"):
            provider.set_task(task)
        last_text = ""
        t_start = time.perf_counter()
        fail_injected_at = 0

        for turn in range(1, self.max_turns + 1):
            res.turns = turn
            out: ChatResult = provider.chat(messages, tools or None,
                                            temperature=self.temperature, max_tokens=self.max_tokens)
            res.prompt_tokens += out.usage.prompt_tokens
            res.completion_tokens += out.usage.completion_tokens
            res.reasoning_tokens += out.usage.reasoning_tokens
            if not out.usage.reported:
                res.usage_estimated = True
            if out.param_degraded:
                res.param_degraded = True
            if out.ttft_ms is not None and (res.ttft_ms is None or out.ttft_ms < res.ttft_ms):
                res.ttft_ms = out.ttft_ms
            if out.error:
                if "empty output" in out.error:
                    res.empty_outputs += 1
                res.error = out.error
                if out.infra_failure:
                    res.infra_failure = True
            res.transcript.append({
                "turn": turn, "text": out.text[:4000], "reasoning_chars": len(out.reasoning),
                "tool_calls": [{"name": c.name, "args": c.args, "source": c.source} for c in out.tool_calls],
                "usage": {"prompt": out.usage.prompt_tokens, "completion": out.usage.completion_tokens,
                          "reasoning": out.usage.reasoning_tokens},
                "ttft_ms": out.ttft_ms, "total_ms": out.total_ms, "finish_reason": out.finish_reason,
                "error": out.error,
            })
            if out.text.strip():
                last_text = out.text

            if not out.tool_calls:
                # 没有工具调用 → 正文即最终答案
                res.final_answer = out.text or last_text
                break

            messages.append({
                "role": "assistant",
                "content": out.text or "",
                "tool_calls": [{"id": c.id, "type": "function",
                                "function": {"name": c.name,
                                             "arguments": json.dumps(c.args, ensure_ascii=False)}}
                               for c in out.tool_calls],
            })

            submitted = False
            for call in out.tool_calls:
                res.tool_calls += 1
                if call.name not in tool_names:
                    res.wasted_tool_calls += 1
                    payload = f"错误：不存在名为 {call.name} 的工具。可用工具：{sorted(tool_names)}"
                else:
                    payload, injected = self._execute(call, vfs, task, res, fail_injected_at, workdir)
                    if injected:
                        fail_injected_at = res.tool_calls
                messages.append({"role": "tool", "tool_call_id": call.id, "content": payload})
                if call.name == "submit" and call.name in tool_names:
                    res.used_submit = True
                    res.final_answer = str(call.args.get("answer") or "")
                    submitted = True
            if submitted:
                # 分阶段任务：把下一批下发下去，继续同一会话。
                # 模型必须看到自己真实的上一批答案，所以不能预先塞占位 assistant 消息。
                if task.next_stage is not None:
                    nxt = task.next_stage(res.final_answer, stage)
                    if nxt is not None:
                        stage += 1
                        if hasattr(provider, "note_stage"):
                            provider.note_stage(stage)
                        user_msg, spec = nxt
                        messages.append({"role": "user", "content": user_msg})
                        if spec:
                            messages.append({"role": "user", "content": f"作答格式要求：{spec}"})
                        res.stages = stage
                        continue
                break

        res.total_ms = int((time.perf_counter() - t_start) * 1000)
        if workdir:
            res.workdir_files = snapshot_dir(workdir)
            shutil.rmtree(workdir, ignore_errors=True)
        if not res.final_answer:
            res.final_answer = last_text
        if res.infra_failure and not res.final_answer.strip():
            # 网关故障导致的空结果：判为"基础设施失败"，不算模型答错
            res.grade = GradeResult(False, 0.0, {"infra": False},
                                    f"基础设施故障，未计入：{res.error}")
        else:
            # workdir_files 只有"落盘类"任务才有内容；其余族的 grader 用 **_kw 忽略它
            res.grade = task.grader(res.final_answer, workdir_files=res.workdir_files)
        return res

    # ---------- 工具执行 ----------

    def _execute(self, call: ToolCall, vfs: dict[str, str], task: TaskInstance,
                 res: RunResult, fail_at: int, workdir: str | None = None) -> tuple[str, bool]:
        name, args = call.name, call.args or {}
        # 故障注入：第 2 次工具调用返回一个"基础设施错误"，看模型能否恢复
        if self.profile.inject_failures and fail_at == 0 and res.tool_calls >= 2 and name != "submit":
            res.injected_failures += 1
            return ("错误：工具服务暂时不可用（HTTP 503）。请稍后重试或改用其他方式完成。", True)

        if name == "read_file":
            path = str(args.get("path") or "").strip()
            if not path:
                res.wasted_tool_calls += 1
                return "错误：缺少 path 参数。", False
            if path in vfs:
                return vfs[path], False
            near = [p for p in vfs if path.lower() in p.lower() or p.lower() in path.lower()]
            hint = f"你是不是想读：{near[:3]}" if near else f"可用文件示例：{sorted(vfs)[:4]}"
            return f"错误：文件不存在 {path}。{hint}", False

        if name == "list_files":
            return json.dumps(sorted(vfs), ensure_ascii=False), False

        if name == "run_python":
            code = str(args.get("code") or "")
            if not code.strip():
                res.wasted_tool_calls += 1
                return "错误：code 为空。", False
            ok, out = run_python(code, timeout=25.0, workdir=workdir)
            tail = out if len(out) <= 4000 else out[:2000] + "\n...[截断]...\n" + out[-1500:]
            head = f"[工作目录：{workdir or '(临时)'}]\n" if workdir else ""
            return (f"{head}[{'执行成功' if ok else '执行失败'}]\n{tail or '(无输出)'}"), False

        if name == "submit":
            return "已收到提交。", False

        res.wasted_tool_calls += 1
        return f"错误：未知工具 {name}", False


# ---------------------------------------------------------------- Phase B：真实 CLI harness


def default_codex_cmd() -> list[str]:
    """找到本机 Codex CLI 的调用方式。

    走 `node <npm>/node_modules/@openai/codex/bin/codex.js` 而不是 .cmd/.ps1 包装器：
    Windows 上包一层 shell 会把带引号的参数交给 cmd 再解析一遍，路径/JSON 一夹就坏。
    """
    npm = Path(os.environ.get("APPDATA") or (Path.home() / "AppData" / "Roaming")) / "npm"
    js = npm / "node_modules" / "@openai" / "codex" / "bin" / "codex.js"
    if js.exists():
        return [shutil.which("node") or "node", str(js)]
    return [shutil.which("codex") or shutil.which("codex.cmd") or "codex"]


@dataclass
class ExternalCLI:
    """真实 agent CLI 的调用参数（Phase B 的被测对象）。"""
    name: str = "codex-cli"
    cmd: list[str] = field(default_factory=default_codex_cmd)
    model: str | None = None            # None = 用该 CLI 自己的默认模型
    model_key: str = "external/codex"   # 榜单里的 id；要与冻结 harness 里的模型对齐就写同一个 key
    timeout_s: float = 900.0
    env: dict[str, str] = field(default_factory=dict)            # 额外环境变量（网关 key 等，不落盘）
    config_overrides: list[str] = field(default_factory=list)    # 额外 -c key=value
    extra_args: list[str] = field(default_factory=lambda: [
        "--skip-git-repo-check",        # 任务工作区不是 git 仓库
        "--sandbox", "workspace-write",  # 只让它写工作区
        "-c", "approval_policy=never",   # 非交互：不要停下来等人点确认
        "--json",                        # 事件流，便于回收轮数/用量
        "--color", "never",
    ])


def _parse_cli_events(stdout: str) -> dict:
    """尽力从 CLI 的 JSONL 事件流里回收轮数与用量（各家字段不一，回收不到就留空）。"""
    info = {"turns": 0, "items": 0, "usage": {}}
    for line in (stdout or "").splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            ev = json.loads(line)
        except Exception:  # noqa: BLE001
            continue
        t = ev.get("type") or (ev.get("msg") or {}).get("type")
        if t in ("turn.completed", "turn_completed"):
            info["turns"] += 1
            u = ev.get("usage") or (ev.get("msg") or {}).get("usage") or {}
            if u:
                info["usage"] = u
        elif t in ("item.completed", "item_completed"):
            info["items"] += 1
    return info


class ExternalHarness:
    """把真实 agent CLI（Codex / Claude Code / …）包成同一个 run(task) -> RunResult 接口。

    三件 Phase B 必须显式定义的事，都在这里定死：

      1. **workspace 铺设协议**：每个任务一个临时目录；`task.meta["files"]` 里的虚拟文件
         全部真写到磁盘（mindiff / repofix 靠它成立）；同一份题目文本另写一份 `TASK.md`，
         让 CLI 自己能读。
      2. **执行与超时**：非交互参数（`approval_policy=never` + `--sandbox workspace-write`），
         prompt 走 stdin（避开 Windows 命令行转义），到点就杀，stdout/stderr 全留档。
      3. **产物读取协议**：最终答案优先取 CLI 写的 last-message 文件（`-o`），
         取不到再退回工作区里的 `ANSWER.json`；**工作区文件快照**一并交给同一个 oracle
         —— 所以"真改没改文件"这类判定在外部 harness 下同样成立。

    与冻结 harness 的唯一区别就是"壳子"：模型、工具、提示词、自主步数全由这个 CLI 决定。
    这正是要被测量的东西（PawBench 实测换壳子能差 11.5 分）。
    """

    def __init__(self, cli: ExternalCLI | None = None):
        self.cli = cli or ExternalCLI()

    @property
    def name(self) -> str:
        return self.cli.name

    @staticmethod
    def build_prompt(task: TaskInstance) -> str:
        parts = [str(m.get("content") or "") for m in task.messages
                 if str(m.get("role")) == "user"]
        body = "\n\n".join(p for p in parts if p.strip())
        return (body
                + f"\n\n作答格式要求：{task.answer_spec}\n"
                + "工作目录（就是你的当前目录）里已经放好了本任务需要的全部文件，"
                  "可以直接读写。\n"
                  "完成后，把你最终的答案（严格按上面的格式，只放答案本身）"
                  "作为你最后一条回复返回。")

    def run(self, provider: Provider | None, task: TaskInstance, *,
            seed: int = 0, rep: int = 0) -> RunResult:
        res = RunResult(model_key=self.cli.model_key, task_key=task.key,
                        profile=self.name, seed=seed, rep=rep)
        ws = tempfile.mkdtemp(prefix="aipk-ext-")
        try:
            files = dict(task.meta.get("files") or {})
            for rel, body in files.items():
                p = Path(ws) / rel
                p.parent.mkdir(parents=True, exist_ok=True)
                p.write_text(body, encoding="utf-8")
            prompt = self.build_prompt(task)
            # 协议文件写在**工作区之外**：工作区必须保持"只有任务本身的文件"，
            # 否则"改了几行"这类判定会把 harness 自己的文件算进去。
            proto = Path(ws).parent / f"_aipk_proto_{Path(ws).name}"
            proto.mkdir(parents=True, exist_ok=True)
            (proto / "TASK.md").write_text(prompt, encoding="utf-8")

            out_file = proto / "_last_message.txt"
            overrides: list[str] = []
            for kv in self.cli.config_overrides:
                overrides += ["-c", kv]
            cmd = [*self.cli.cmd, "exec", "--cd", ws, *self.cli.extra_args,
                   *overrides, "-o", str(out_file)]
            if self.cli.model:
                cmd += ["-m", self.cli.model]
            cmd.append("-")          # prompt 从 stdin 读

            t0 = time.perf_counter()
            rc, out, err, timed_out = 0, "", "", False
            env = {**os.environ, **self.cli.env} if self.cli.env else None
            try:
                p = subprocess.run(cmd, cwd=ws, input=prompt, capture_output=True,
                                   text=True, encoding="utf-8", errors="replace",
                                   timeout=self.cli.timeout_s, env=env)
                rc, out, err = p.returncode, p.stdout or "", p.stderr or ""
            except subprocess.TimeoutExpired as e:
                timed_out = True
                rc = -9
                out = e.stdout if isinstance(e.stdout, str) else ""
                err = f"TIMEOUT after {self.cli.timeout_s}s"
            except (OSError, ValueError) as e:
                res.error = f"外部 CLI 起不来：{type(e).__name__}: {e}"[:300]
                res.infra_failure = True
            res.total_ms = int((time.perf_counter() - t0) * 1000)

            answer = ""
            if out_file.exists():
                answer = out_file.read_text(encoding="utf-8", errors="replace").strip()
            if not answer:
                for cand in ("ANSWER.json", "answer.json", "ANSWER.md", "answer.txt"):
                    f = Path(ws) / cand
                    if f.exists():
                        answer = f.read_text(encoding="utf-8", errors="replace").strip()
                        break
            info = _parse_cli_events(out)
            u = info["usage"] or {}
            res.prompt_tokens = int(u.get("input_tokens") or 0)
            res.completion_tokens = int(u.get("output_tokens") or 0)
            res.reasoning_tokens = int(u.get("reasoning_output_tokens") or 0)
            if not u:
                # 没有用量字段就用字符数估算（并在报告里标 ≈），不能当成零成本
                res.usage_estimated = True
                res.prompt_tokens = len(prompt) // 3
                res.completion_tokens = len(out) // 4
            res.turns = info["turns"] or max(1, info["items"])
            res.tool_calls = info["items"]
            res.used_submit = bool(answer)
            res.final_answer = answer
            res.workdir_files = snapshot_dir(ws)
            res.transcript = [{
                "harness": self.name, "cmd": cmd, "returncode": rc, "timed_out": timed_out,
                "stdout_tail": out[-8000:], "stderr_tail": err[-3000:],
                "events": {"turns": info["turns"], "items": info["items"], "usage": u},
                "workspace_files": sorted(res.workdir_files),
            }]
            if timed_out:
                res.error = f"超时（{self.cli.timeout_s}s）"
            elif rc != 0 and not answer:
                res.error = f"CLI 退出码 {rc}：{err.strip()[-200:]}"

            if timed_out and not answer.strip():
                res.infra_failure = True     # 超时且没产物：算基础设施问题，不判成模型答错
                res.grade = GradeResult(False, 0.0, {"infra": False}, "外部 CLI 超时，未计入")
            else:
                res.grade = task.grader(res.final_answer, workdir_files=res.workdir_files)
            return res
        finally:
            shutil.rmtree(ws, ignore_errors=True)
            shutil.rmtree(Path(ws).parent / f"_aipk_proto_{Path(ws).name}", ignore_errors=True)