"""族 9：小仓库调试（当前最难的一族，打的是真 agentic 能力）。

为什么加这一族：第一轮 PK 里 8 个族被前沿模型全部打穿（10 个模型成功率全在 90~97%，
置信区间两两重叠），说明题目难度不够。这一族刻意叠了四层难度：

  1. **多文件仓库导航**：4 个模块 + 数据文件，得先探索清楚结构
  2. **刻意误导的注释**：文件里写着"这段已评审确认无误"，跟着注释走就会放过 bug
  3. **必须真跑代码**：bug 都在边界/初值/取整上，光读代码很难确信
  4. **必须算出数值**：最终要给"修复后 main 的输出值"，只看出 bug 拿不到分

工具执行时会**把仓库真正落到磁盘**（harness 提供 workdir），所以 run_python 能直接
`from app.transform import run` 跑真代码 —— 这是它比前面几族难的根本原因。
"""
from __future__ import annotations

import ast
import copy
import shutil
import tempfile
from pathlib import Path

from . import GradeResult, TaskInstance, extract_json, register, rng, run_python

PKG = "app"
TESTFILE = "test_all.py"
README = "README.md"

MISLEADING = [
    "# NOTE: this branch is verified correct in production.",
    "# TODO(reviewed): logic below is fine, do not change it.",
    "# 这段逻辑已经过评审，确认无误，请勿修改。",
    "# IMPORTANT: rounding here follows the spec exactly.",
]

TEST_HARNESS = """def check(rows, want):
    got = run(rows)
    assert got == want, 'got=%r want=%r' % (got, want)
    return True


def test_basic():
{cases}

if __name__ == '__main__':
    test_basic()
    print('TESTS PASS')
"""


# ---------------------------------------------------------------- 四个模板


def _t_sliding_mean(r) -> dict:
    size = r.choice([2, 3])
    good = (
        "def run(rows):\n"
        "    out = []\n"
        "    for i in range(len(rows)):\n"
        f"        win = rows[max(0, i - {size} + 1): i + 1]\n"
        "        out.append(round(sum(x.value for x in win) / len(win), 2))\n"
        "    return out\n"
    )
    bad = (
        "def run(rows):\n"
        "    out = []\n"
        "    for i in range(len(rows)):\n"
        f"        win = rows[max(0, i - {size} + 1): i + 1]\n"
        f"        out.append(round(sum(x.value for x in win) / {size}, 2))\n"
        "    return out\n"
    )
    cases = (
        "    rows = [Row('a', 2.0), Row('b', 4.0), Row('c', 6.0)]\n"
        f"    check(rows, [{2.0}, {(2.0 + 4.0) / 2}, {round((4.0 + 6.0) / 2, 2)}])\n"
    )
    return {
        "name": "滑动窗口均值", "good": good, "bad": bad, "cases": cases,
        "bug_kind": "窗口不足时用了固定除数，而不是实际条数",
        "hint": (f"本系统按 {size} 条一组计算移动均值；"
                 f"**窗口不足 {size} 条时按实际条数求均值**。结果保留两位小数。"),
    }


def _t_tiered_fee(r) -> dict:
    rate = round(r.uniform(0.5, 4.5), 2)
    free = r.choice([3, 4, 5])
    good = (
        "def run(rows):\n"
        f"    billable = max(0, len(rows) - {free})\n"
        f"    return round(billable * {rate}, 2)\n"
    )
    bad = (
        "def run(rows):\n"
        f"    billable = len(rows) - {free}\n"
        f"    return round(billable * {rate}, 2)\n"
    )
    cases = (
        "    rows = [Row('a', 1.0), Row('b', 1.0)]\n"
        "    check(rows, 0.0)\n"
    )
    return {
        "name": "阶梯费率计费", "good": good, "bad": bad, "cases": cases,
        "bug_kind": "免费额度大于条目数时算出负计费",
        "hint": (f"计费规则：基础费率 {rate}，**前 {free} 条免费**，其余按费率计费，"
                 f"结果保留两位小数。条目数不足免费额度时计费为 0。"),
    }


def _t_cumprod(r) -> dict:
    good = (
        "def run(rows):\n"
        "    total = 1.0\n"
        "    out = []\n"
        "    for x in rows:\n"
        "        total = round(total * x.value, 3)\n"
        "        out.append(total)\n"
        "    return out\n"
    )
    bad = (
        "def run(rows):\n"
        "    total = 0.0\n"
        "    out = []\n"
        "    for x in rows:\n"
        "        total = round(total * x.value, 3)\n"
        "        out.append(total)\n"
        "    return out\n"
    )
    cases = (
        "    rows = [Row('a', 2.0), Row('b', 3.0)]\n"
        "    check(rows, [2.0, 6.0])\n"
    )
    return {
        "name": "累计乘积", "good": good, "bad": bad, "cases": cases,
        "bug_kind": "累乘初值写成 0，导致结果全是 0",
        "hint": "做累计乘积，从 1 开始逐条累乘，每条保留三位小数（四舍五入）。",
    }


def _t_group_top(r) -> dict:
    good = (
        "def run(rows):\n"
        "    best = {}\n"
        "    order = []\n"
        "    for x in rows:\n"
        "        if x.kind not in best:\n"
        "            best[x.kind] = x.value\n"
        "            order.append(x.kind)\n"
        "        elif x.value > best[x.kind]:\n"
        "            best[x.kind] = x.value\n"
        "    return [best[k] for k in order]\n"
    )
    bad = (
        "def run(rows):\n"
        "    best = {}\n"
        "    order = []\n"
        "    for x in rows:\n"
        "        if x.kind not in best:\n"
        "            best[x.kind] = x.value\n"
        "            order.append(x.kind)\n"
        "        elif x.value >= best[x.kind]:\n"
        "            best[x.kind] = x.value\n"
        "            order.append(x.kind)\n"
        "    return [best[k] for k in order]\n"
    )
    cases = (
        "    rows = [Row('a', 1.0, 'p'), Row('b', 5.0, 'p')]\n"
        "    check(rows, [5.0])\n"
    )
    return {
        "name": "分组取最大", "good": good, "bad": bad, "cases": cases,
        "bug_kind": "并列时重复追加分组键，输出长度错误",
        "hint": "按 kind 分组，取每组 value 的最大值；输出顺序按分组首次出现的顺序。",
    }


TEMPLATES = [_t_sliding_mean, _t_tiered_fee, _t_cumprod, _t_group_top]


# ---------------------------------------------------------------- 仓库组装


def _skeleton(r) -> dict[str, str]:
    return {
        f"{PKG}/__init__.py": "",
        f"{PKG}/core.py": (
            '"""数据装载与基础类型。"""\n'
            "from dataclasses import dataclass\n\n\n"
            "@dataclass\n"
            "class Row:\n"
            "    key: str\n"
            "    value: float\n"
            "    kind: str = 'x'\n\n\n"
            "def load(path):\n"
            "    rows = []\n"
            "    with open(path, 'r', encoding='utf-8') as f:\n"
            "        for line in f:\n"
            "            line = line.strip()\n"
            "            if not line:\n"
            "                continue\n"
            "            parts = line.split(',')\n"
            "            kind = parts[2] if len(parts) > 2 else 'x'\n"
            "            rows.append(Row(parts[0], float(parts[1]), kind))\n"
            "    return rows\n"
        ),
        f"{PKG}/store.py": (
            '"""写回工具（本任务用不到）。"""\n'
            "def save(path, text):\n"
            "    with open(path, 'w', encoding='utf-8') as f:\n"
            "        f.write(text)\n"
            "    return len(text)\n"
        ),
        f"{PKG}/main.py": (
            "import os\n"
            "import sys\n\n"
            "sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))\n\n"
            "from app.core import load\n"
            "from app.transform import run\n\n\n"
            "def main(path='data/input.csv'):\n"
            "    return run(load(path))\n\n\n"
            "if __name__ == '__main__':\n"
            "    print(main())\n"
        ),
        README: (
            "# 计费/统计小工具\n\n"
            "## 目录\n"
            f"- `{PKG}/core.py` 数据装载与 Row 定义\n"
            f"- `{PKG}/transform.py` **核心变换逻辑**\n"
            f"- `{PKG}/store.py` 写回（本任务用不到）\n"
            f"- `{PKG}/main.py` 入口，读 `data/input.csv` 并打印 run() 的结果\n"
            f"- `{TESTFILE}` 自测脚本\n"
            "- `data/input.csv` 输入数据，格式 `key,value,kind`\n\n"
            "## 复现\n"
            "```\n"
            f"python {TESTFILE}     # 自测\n"
            f"python -m {PKG}.main  # 打印结果\n"
            "```\n"
        ),
    }


def _write_repo(root: Path, files: dict[str, str]) -> None:
    for rel, body in files.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(body, encoding="utf-8")


_RUN_SNIPPET = (
    "import sys\n"
    "sys.path.insert(0, '.')\n"
    "from app.core import load\n"
    "from app.transform import run\n"
    "rows = load('data/input.csv')\n"
    "print('OUT', repr(run(rows)))\n"
)


def _assemble(seed: int) -> dict | None:
    r = rng(seed * 2654435761 + 101)
    tpl = copy.deepcopy(TEMPLATES[r.randrange(len(TEMPLATES))](r))
    files = _skeleton(r)

    # 数据（用 seed 派生 tag 写进 key，避免不同 seed 撞出结构相同的题）
    tag = seed % 97 + 7
    n = r.choice([5, 6, 7])
    kinds = r.sample(["p", "q", "s"], r.choice([1, 2, 2, 3]))
    rows = [(f"{tag}r{i + 1}", round(r.uniform(1.0, 20.0), 2), r.choice(kinds)) for i in range(n)]
    files["data/input.csv"] = "\n".join(f"{k},{v},{kd}" for k, v, kd in rows) + "\n"

    dry = tempfile.mkdtemp(prefix="aipk-dry-")
    try:
        root = Path(dry)
        note = MISLEADING[r.randrange(len(MISLEADING))]
        files[f"{PKG}/transform.py"] = (
            '"""核心变换逻辑。"""\n'
            "from .core import Row  # noqa: F401\n\n\n"
            f"{note}\n"
            f"{tpl['bad']}"
        )
        files[TESTFILE] = (
            "from app.core import Row\n"
            "from app.transform import run\n\n\n"
            + TEST_HARNESS.format(cases=tpl["cases"])
        )
        _write_repo(root, files)

        # 1) 坏实现必须跑不过自测
        bad_ok, _ = run_python(f"exec(open('{TESTFILE}', encoding='utf-8').read())", timeout=25, workdir=dry)

        # 2) 正确实现跑一遍，拿到期望输出（不手算，靠真跑）
        (root / f"{PKG}/transform.py").write_text(
            '"""核心变换逻辑。"""\nfrom .core import Row  # noqa: F401\n\n\n' + tpl["good"],
            encoding="utf-8")
        good_ok, good_out = run_python(_RUN_SNIPPET, timeout=25, workdir=dry)
        test_ok, _ = run_python(f"exec(open('{TESTFILE}', encoding='utf-8').read())", timeout=25, workdir=dry)

        expected = ""
        for line in good_out.splitlines():
            if line.startswith("OUT "):
                expected = line[4:].strip()
        if not (expected and test_ok and not bad_ok):
            return None
    finally:
        shutil.rmtree(dry, ignore_errors=True)

    return {"files": files, "tpl": tpl, "expected_out": expected, "rows": rows,
            "good_transform_py": '"""核心变换逻辑。"""\nfrom .core import Row  # noqa: F401\n\n\n' + tpl["good"]}


@register("repofix")
def build(seed: int) -> TaskInstance:
    a = None
    for attempt in range(20):
        a = _assemble(seed + attempt * 7919)
        if a:
            break
    if not a:
        raise RuntimeError(f"repofix seed={seed} 生成不出有效实例（坏实现没挂或真值算不出）")

    files, tpl = a["files"], a["tpl"]
    listing = "\n".join(f"  {p}" for p in sorted(files))
    prompt = f"""一个小项目（工单编号 W{seed % 100000:05d}）里有 bug，请你修好它。项目结构：

{listing}

功能背景：{tpl['hint']}

现象：自测脚本 `{TESTFILE}` 当前跑不过。

请完成三件事：
1. 定位 bug（**注意：文件里的注释不一定可信**）。
2. 给出修复后 `{PKG}/transform.py` 的**完整文件内容**。
3. 报告修复后 `run(rows)` 的返回值（`rows` 是 `data/input.csv` 装载出来的），写成 python 字面量。

工具：read_file 读文件；run_python 执行代码，**工作目录就是项目根目录**。
建议先 `run_python` 跑一次自测看看实际报错，再决定怎么改。

只输出 JSON，不要任何解释：
{{"bug_function": "出问题的函数名", "bug_reason": "一句话说明", "fixed_transform_py": "修复后的完整文件内容", "main_output": "修复后 run(rows) 的返回值（python 字面量）"}}"""

    def grader(answer: str, _a=a, **_kw) -> GradeResult:
        obj = extract_json(answer)
        if not isinstance(obj, dict):
            return GradeResult(False, 0.0, {"format": False}, "没给出 JSON 对象", parse_ok=False)
        checks: dict[str, bool] = {}
        checks["named_function"] = str(obj.get("bug_function") or "").strip().endswith("run")
        fixed = str(obj.get("fixed_transform_py") or "")
        checks["gave_code"] = "def run" in fixed
        checks["bug_reason_given"] = len(str(obj.get("bug_reason") or "")) >= 6

        passed = False
        runtime_out = ""
        detail = ""
        if checks["gave_code"]:
            dry = tempfile.mkdtemp(prefix="aipk-g-")
            try:
                root = Path(dry)
                _write_repo(root, _a["files"])
                (root / f"{PKG}/transform.py").write_text(fixed, encoding="utf-8")
                ok_test, out_test = run_python(
                    "try:\n"
                    f"    exec(open('{TESTFILE}', encoding='utf-8').read())\n"
                    "    print('TESTS PASS')\n"
                    "except Exception as e:\n"
                    "    print('TESTS FAIL', type(e).__name__, e)\n", timeout=30, workdir=dry)
                passed = "TESTS PASS" in out_test
                if not passed:
                    detail = out_test.strip().splitlines()[-1][:140] if out_test.strip() else "无输出"
                else:
                    ok_run, out_run = run_python(_RUN_SNIPPET, timeout=30, workdir=dry)
                    for line in out_run.splitlines():
                        if line.startswith("OUT "):
                            runtime_out = line[4:].strip()
            finally:
                shutil.rmtree(dry, ignore_errors=True)

        def norm(s: str) -> str:
            s = (s or "").strip().replace(" ", "").replace("'", '"')
            try:
                return repr(ast.literal_eval(s))
            except Exception:  # noqa: BLE001
                return s

        checks["tests_pass"] = passed
        submitted = str(obj.get("main_output") or "")
        checks["output_matches_runtime"] = bool(runtime_out) and norm(submitted) == norm(runtime_out)
        checks["output_correct"] = norm(submitted) == norm(_a["expected_out"]) and bool(submitted.strip())

        solved = checks["tests_pass"] and checks["output_correct"]
        reward = sum(checks.values()) / len(checks)
        if solved:
            reason = "定位正确、修复通过自测、输出值正确"
        elif not checks["gave_code"]:
            reason = "没给出修复后的文件内容"
        elif not checks["tests_pass"]:
            reason = f"修复后自测仍失败：{detail}"
        else:
            reason = f"代码修好了但输出值不对：给 {submitted!r}，实际 {runtime_out!r}"
        return GradeResult(solved, reward, checks, reason)

    return TaskInstance(
        tid=f"r{seed}", family="repofix", seed=seed,
        messages=[{"role": "user", "content": prompt}],
        answer_spec='{"bug_function":…, "bug_reason":…, "fixed_transform_py":…, "main_output":…}',
        grader=grader, tools=["read_file", "list_files", "run_python"], needs_tools=True,
        meta={"files": files, "expected_out": a["expected_out"],
              "good_transform_py": a["good_transform_py"],
              "bug_kind": tpl["bug_kind"], "template": tpl["name"]},
    )
