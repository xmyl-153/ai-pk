"""任务框架：生成器 + 参考实现 + oracle。

三条铁律：
1. 每个实例由 seed 程序化生成，答案由参考实现现场算出 —— 没有可背的答案表。
2. 判定优先用代码，不用裁判。
3. 答案必须能机器解析；格式不对要能区分"错"和"不会说话"。
"""
from __future__ import annotations

import hashlib
import json
import random
import re
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

# ---------------------------------------------------------------- 通用工具


def rng(seed: int) -> random.Random:
    return random.Random(seed)


def derive_seed(base: int, rep: int, tno: int, family: str) -> int:
    """从 (基准 seed, 重复号, 实例号, 族名) 派生实例 seed。

    用哈希混合而不是线性相加：线性相加会让 (rep=0,tno=1) 和 (rep=1,tno=0) 撞同一个 seed，
    于是 reps 变成假重复。
    """
    h = hashlib.blake2b(f"{base}|{rep}|{tno}|{family}".encode("utf-8"), digest_size=4).digest()
    return int.from_bytes(h, "big") % (2 ** 31 - 1)


def run_python(code: str, timeout: float = 20.0, workdir: str | None = None) -> tuple[bool, str]:
    """在子进程里跑一段 python，返回 (是否成功, stdout+stderr)。

    workdir 不为空时脚本以该目录为工作目录运行 —— 任务族可以把一个"小仓库"落到
    磁盘，让模型真正 import 它、跑它的测试（这是 repofix 族成立的前提）。
    """
    if workdir:
        f = Path(workdir) / "_aipk_snippet.py"
        try:
            f.write_text(code, encoding="utf-8")
            p = subprocess.run([sys.executable, f.name], capture_output=True, text=True,
                               timeout=timeout, encoding="utf-8", errors="replace", cwd=workdir)
            return p.returncode == 0, (p.stdout or "") + (p.stderr or "")
        except subprocess.TimeoutExpired:
            return False, f"TIMEOUT after {timeout}s"
        finally:
            try:
                f.unlink()
            except OSError:
                pass
    with tempfile.TemporaryDirectory() as td:
        f = Path(td) / "snippet.py"
        f.write_text(code, encoding="utf-8")
        try:
            p = subprocess.run([sys.executable, str(f)], capture_output=True, text=True,
                               timeout=timeout, encoding="utf-8", errors="replace")
            out = (p.stdout or "") + (p.stderr or "")
            return p.returncode == 0, out
        except subprocess.TimeoutExpired:
            return False, f"TIMEOUT after {timeout}s"


_JSON_BLOCK = re.compile(r"```(?:json)?\s*(.*?)\s*```", re.S)


def extract_json(text: str) -> Any | None:
    """从模型输出里尽力挖出 JSON 对象。兼容裸 JSON / 代码块 / 前后有废话。"""
    if not text:
        return None
    cands: list[str] = []
    for m in _JSON_BLOCK.finditer(text):
        cands.append(m.group(1))
    cands.append(text.strip())
    # 最外层大括号切片
    i, j = text.find("{"), text.rfind("}")
    if i != -1 and j > i:
        cands.append(text[i:j + 1])
    i, j = text.find("["), text.rfind("]")
    if i != -1 and j > i:
        cands.append(text[i:j + 1])
    for c in cands:
        c = c.strip()
        if not c:
            continue
        try:
            return json.loads(c)
        except Exception:  # noqa: BLE001
            continue
        try:  # 容忍尾逗号
            return json.loads(re.sub(r",\s*([}\]])", r"\1", c))
        except Exception:  # noqa: BLE001
            continue
    return None


# ---------------------------------------------------------------- 数据结构


@dataclass
class GradeResult:
    solved: bool = False
    reward: float = 0.0          # 0..1 连续分，便于做敏感性分析
    checks: dict[str, bool] = field(default_factory=dict)
    reason: str = ""
    parse_ok: bool = True        # False = 输出压根没按格式来


@dataclass
class TaskInstance:
    """一个具体任务实例。answer_spec 是给模型看的作答格式说明。"""
    tid: str
    family: str
    seed: int
    messages: list[dict]
    answer_spec: str
    grader: Callable[..., GradeResult]
    judge_prompt: str | None = None      # 非空则额外走盲评
    reference: str = ""                  # 给裁判做 A/B 的参考解（仅 judge 类任务用）
    tools: list[str] = field(default_factory=list)
    needs_tools: bool = False
    meta: dict[str, Any] = field(default_factory=dict)
    # 分阶段任务：模型每次 submit 后调用 next_stage(submitted, stage_idx)
    # 返回 (下一阶段的 user 消息, 新的 answer_spec) —— 返回 None 表示任务结束。
    # 长程状态族靠它把"多批操作"逐轮下发，模型必须看到自己真实的上一批答案。
    next_stage: Callable[[str, int], tuple[str, str] | None] | None = None

    @property
    def key(self) -> str:
        return f"{self.family}:{self.tid}"


# ---------------------------------------------------------------- 任务族注册

FAMILIES: dict[str, Callable[[int], TaskInstance]] = {}


def register(name: str):
    def deco(fn):
        FAMILIES[name] = fn
        return fn
    return deco


def make(family: str, seed: int) -> TaskInstance:
    if family not in FAMILIES:
        raise KeyError(f"未知任务族 {family}；可用：{sorted(FAMILIES)}")
    inst = FAMILIES[family](seed)
    inst.family = family
    inst.seed = seed
    return inst


def all_families() -> list[str]:
    return sorted(FAMILIES)


def _autoload() -> None:
    """导入同目录下所有 fNN_*.py，触发 @register 注册。"""
    import importlib
    import pkgutil
    from pathlib import Path

    pkg_dir = Path(__file__).resolve().parent
    for mod in sorted(pkgutil.iter_modules([str(pkg_dir)])):
        if mod.name.startswith("f") and mod.name[1:3].isdigit():
            importlib.import_module(f"{__name__}.{mod.name}")


_autoload()
