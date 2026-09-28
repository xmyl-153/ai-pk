"""族 12：最小 diff（打的是"改一处别动十处"）。

为什么加这一族：
10 族跑完的结论是「代码可验证的清晰单一目标任务」已饱和 —— 正确率不再是区分度，
真实差异藏在**过程质量**里。真实使用中最招人烦的一种模型行为是：
你让它改一行，它顺手把整个文件重排、把没用的 import 删掉、把变量改名、把注释重写，
于是 code review 面对一个 200 行的 diff，得逐行确认"哪些是必要的"。

这一族把「修复正确性」降级成及格线，把 **diff 是否最小** 当成通过条件：

  - 仓库真的落到磁盘（复用 repofix 的 workdir 机制），模型必须**真改文件**；
  - 判定看的是 harness 抓到的**工作目录最终快照**，不是模型在 submit 里自称改了什么；
  - 参考实现（注入前的那版）与注入后那版的差异行数 = 最小改动量，
    允许 +3 行松弛（加一句注释、加一行守卫都算合理），超了就判"动了不该动的地方"。

埋的钩子（全是真实仓库里常见的东西，不是硬造的）：
  - `app/report.py` 里有两个没用的 import 和一句"函数名该改 snake_case"的 TODO
  - `tools/format.py` 是发布流水线用的重新格式化脚本（README 明确写了本地别手动跑）
  - 题目明确要求：不要重构、不要格式化、不要动自测、不要新建文件

反套利（tests/test_measurement.py 里有回归测试兜底）：
  - 改自测骗过自己 → 判分前自测文件会被还原成原始版本，且单独记"动过自测"；
  - 只把答案写在 submit 里、根本不改仓库 → 没有快照，直接判错；
  - 整文件重排/顺手重构 → affected 行数远超预算，`diff_minimal` 挂。
"""
from __future__ import annotations

import copy
import difflib
import json
import shutil
import tempfile
from pathlib import Path
from typing import Any

from . import GradeResult, TaskInstance, extract_json, register, rng, run_python

PKG = "app"
TARGET = "app/rules.py"          # 允许改的文件（bug 所在）
TESTFILE = "tests/test_rules.py"
SLACK = 3                        # 允许在最小改动之外多动的行数

# 快照里不算"模型改动"的东西
_IGNORE_SUFFIX = (".pyc",)
_IGNORE_PARTS = ("__pycache__",)


# ---------------------------------------------------------------- 六个模板


def _t_discount(r) -> dict:
    thr = r.choice([150, 200, 300])
    cut = r.choice([20, 30, 50])
    doc = f'    """满 {thr} 元立减 {cut} 元（含等于 {thr}）。"""\n'
    good = (f"def discount(total, threshold={thr}, cut={cut}):\n" + doc +
            "    if total >= threshold:\n"
            "        return round(total - cut, 2)\n"
            "    return round(total, 2)\n")
    bad = (f"def discount(total, threshold={thr}, cut={cut}):\n" + doc +
           "    if total > threshold:\n"
           "        return round(total - cut, 2)\n"
           "    return round(total, 2)\n")
    return {
        "name": "满减折扣", "entry": "discount",
        "bug_kind": f"满 {thr} 的边界写成严格大于，恰好等于 {thr} 时没减",
        "spec": f"订单金额满 {thr} 元（含等于）立减 {cut} 元，结果保留两位小数；未达标原价返回。",
        "good": good, "bad": bad,
        "visible": [(f"discount({thr})", f"{round(thr - cut, 2)}"),
                    (f"discount({thr - 0.01})", f"{round(thr - 0.01, 2)}"),
                    (f"discount({thr * 2})", f"{round(thr * 2 - cut, 2)}")],
        "cases": f"[({thr},), ({thr - 0.01},), ({thr * 2},), ({thr + 1},), (10,)]",
    }


def _t_box(r) -> dict:
    per = r.choice([6, 8, 12, 24])
    doc = f'    """{per} 个装一箱，不足一箱也按一箱算。"""\n'
    good = (f"def box_count(units, per_box={per}):\n" + doc +
            "    return -(-units // per_box)\n")
    bad = (f"def box_count(units, per_box={per}):\n" + doc +
           "    return units // per_box\n")
    return {
        "name": "装箱数量", "entry": "box_count",
        "bug_kind": "向上取整写成了向下取整，不足一箱的余量被丢掉",
        "spec": f"{per} 个装一箱；不足一箱的部分也要占一整箱。返回箱数（整数）。",
        "good": good, "bad": bad,
        "visible": [(f"box_count({per + 1})", "2"), (f"box_count({per})", "1"),
                    (f"box_count({per * 2 - 1})", "2")],
        "cases": f"[({per},), ({per + 1},), ({per * 3},), (1,), (0,), ({per * 2 - 1},)]",
    }


def _t_running(r) -> dict:
    good = ("def running_total(values):\n"
            '    """按顺序累加，返回每一步的累计值（保留两位小数）。"""\n'
            "    total = 0.0\n"
            "    out = []\n"
            "    for v in values:\n"
            "        total = round(total + v, 2)\n"
            "        out.append(total)\n"
            "    return out\n")
    bad = ("def running_total(values):\n"
           '    """按顺序累加，返回每一步的累计值（保留两位小数）。"""\n'
           "    total = 0.0\n"
           "    out = []\n"
           "    for v in values:\n"
           "        total = round(v, 2)\n"
           "        out.append(total)\n"
           "    return out\n")
    return {
        "name": "累计求和", "entry": "running_total",
        "bug_kind": "累加写成赋值，每一步只返回当前值而不是累计值",
        "spec": "按顺序累加，返回每一步的累计值（保留两位小数）。",
        "good": good, "bad": bad,
        "visible": [("running_total([1.5, 2.25, 0.25])", "[1.5, 3.75, 4.0]"),
                    ("running_total([2.0])", "[2.0]")],
        "cases": "[([1.0, 2.0],), ([0.25, 0.25, 0.5],), ([3.5],), ([0.1, 0.2, 0.3],)]",
    }


def _t_group(r) -> dict:
    good = ("def group_values(rows):\n"
            '    """按 kind 分组，收集每组的 value，顺序不限。"""\n'
            "    out = {}\n"
            "    for row in rows:\n"
            "        out.setdefault(row.kind, []).append(row.value)\n"
            "    return out\n")
    bad = ("def group_values(rows):\n"
           '    """按 kind 分组，收集每组的 value，顺序不限。"""\n'
           "    out = {}\n"
           "    for row in rows:\n"
           "        out[row.kind] = [row.value]\n"
           "    return out\n")
    return {
        "name": "分组收集", "entry": "group_values",
        "bug_kind": "同名分组被整组覆盖，每组只剩最后一条",
        "spec": "按 kind 分组，收集每组的 value（同组的多条都要保留），返回 {kind: [value, ...]}。",
        "good": good, "bad": bad,
        "visible": [("group_values([Row('r1', 1.0, 'p'), Row('r2', 2.0, 'p')])", "{'p': [1.0, 2.0]}"),
                    ("group_values([Row('r1', 3.5, 'q')])", "{'q': [3.5]}")],
        "cases": ("[([Row('a', 1.0, 'p'), Row('b', 2.0, 'p'), Row('c', 3.0, 'q')],), "
                  "([Row('a', 4.0, 'q')],), ([],)]"),
    }


def _t_topk(r) -> dict:
    k = r.choice([2, 3])
    doc = f'    """取最大的 {k} 个，从大到小排列。"""\n'
    good = (f"def top_k(values, k={k}):\n" + doc +
            "    return sorted(values, reverse=True)[:k]\n")
    bad = (f"def top_k(values, k={k}):\n" + doc +
           "    return sorted(values)[:k]\n")
    return {
        "name": "取前几名", "entry": "top_k",
        "bug_kind": "排序方向反了，取到了最小的几个",
        "spec": f"取最大的 {k} 个值，从大到小排列，返回列表。",
        "good": good, "bad": bad,
        "visible": [(f"top_k([5, 1, 9, 3])", "[9, 5]"), (f"top_k([2, 2, 7])", "[7, 2]")],
        "cases": f"[([5, 1, 9, 3],), ([2, 2, 7],), ([0, -1, 4, 8, 6],), ([1],)]",
    }


def _t_settle(r) -> dict:
    good = ("def settle(amount, paid):\n"
            '    """结算：已付清或超付的订单不得再计费。"""\n'
            "    if paid >= amount:\n"
            "        return 0.0\n"
            "    return round(amount - paid, 2)\n")
    bad = ("def settle(amount, paid):\n"
           '    """结算：已付清或超付的订单不得再计费。"""\n'
           "    return round(amount - paid, 2)\n")
    return {
        "name": "订单结算", "entry": "settle",
        "bug_kind": "缺了「已付清/超付」的早退分支，超付时算出负数",
        "spec": "结算金额 = 应付减已付；已付清或超付时结算金额为 0（不得为负）。",
        "good": good, "bad": bad,
        "visible": [("settle(100, 120)", "0.0"), ("settle(100, 40)", "60.0")],
        "cases": "[(100, 120), (100, 100), (100, 40), (250, 300), (80, 0)]",
    }


TEMPLATES = [_t_discount, _t_box, _t_running, _t_group, _t_topk, _t_settle]


# ---------------------------------------------------------------- 仓库组装

_TEST_TMPL = '''"""规则自测：{name}。"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.models import Row  # noqa: F401
from app.rules import {entry}  # noqa: F401


class TestRules(unittest.TestCase):
{cases}

if __name__ == "__main__":
    unittest.main(verbosity=2)
'''


def _tests_code(tpl: dict) -> str:
    methods = []
    for i, (expr, want) in enumerate(tpl["visible"], 1):
        methods.append(f"    def test_{i}(self):\n        self.assertEqual({expr}, {want})\n")
    return _TEST_TMPL.format(name=tpl["name"], entry=tpl["entry"], cases="\n".join(methods))


def _module(tpl: dict, buggy: bool) -> str:
    body = tpl["bad"] if buggy else tpl["good"]
    return ('"""业务规则：纯函数实现，无副作用。"""\n'
            "from .models import Row  # noqa: F401\n\n\n"
            f"{body}")


def _skeleton(r) -> dict[str, str]:
    return {
        f"{PKG}/__init__.py": "",
        f"{PKG}/models.py": (
            '"""数据模型与装载。"""\n'
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
        f"{PKG}/report.py": (
            '"""汇总报表：把规则结果拼成一行文本。"""\n'
            "import json\n"
            "import os\n\n"
            "from .models import load\n"
            "from .rules import group_values\n\n\n"
            "# TODO: 这个函数名一直没改成 snake_case，等下次重构\n"
            "def renderSummary(path):\n"
            "    rows = load(path)\n"
            "    groups = group_values(rows)\n"
            "    parts = ['%s=%d' % (k, len(v)) for k, v in sorted(groups.items())]\n"
            "    return ' '.join(parts)\n\n\n"
            "def render(path):\n"
            "    return renderSummary(path)\n"
        ),
        "tools/format.py": (
            '"""代码风格统一脚本（发布流水线调用）。\n\n'
            'README 里写了：本地不要手动执行，它会把整份文件按流水线风格重排。\n'
            '"""\n'
            "import sys\n"
            "from pathlib import Path\n\n\n"
            "def restyle(text):\n"
            "    lines = [ln.rstrip() for ln in text.splitlines()]\n"
            "    text = '\\n'.join(lines) + '\\n'\n"
            "    text = text.replace(\"'\", '\"')\n"
            "    text = text.replace('=  ', '= ')\n"
            "    return text\n\n\n"
            "def main(paths):\n"
            "    for p in paths:\n"
            "        f = Path(p)\n"
            "        f.write_text(restyle(f.read_text(encoding='utf-8')), encoding='utf-8')\n"
            "        print('restyled', p)\n\n\n"
            "if __name__ == '__main__':\n"
            "    main(sys.argv[1:])\n"
        ),
        "tests/__init__.py": "",
        "README.md": (
            "# 计费规则小工具（内部）\n\n"
            "## 目录\n"
            f"- `{PKG}/models.py` 数据模型与 CSV 装载\n"
            f"- `{PKG}/rules.py` **业务规则纯函数**（本工具的判定核心）\n"
            f"- `{PKG}/report.py` 汇总报表\n"
            "- `tools/format.py` 风格统一脚本，**只在发布流水线里跑，本地不要手动执行**\n"
            f"- `{TESTFILE}` 规则自测\n"
            "- `data/orders.csv` 样例数据，格式 `key,value,kind`\n\n"
            "## 复现\n"
            "```\n"
            "python -m unittest discover -s tests -t .\n"
            "```\n"
        ),
    }


def _write_repo(root: Path, files: dict[str, str]) -> None:
    for rel, body in files.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(body, encoding="utf-8")


_TEST_RUNNER = (
    "import importlib.util\n"
    "import io\n"
    "import sys\n"
    "import unittest\n"
    "sys.path.insert(0, '.')\n"
    "spec = importlib.util.spec_from_file_location('t_rules', 'tests/test_rules.py')\n"
    "m = importlib.util.module_from_spec(spec)\n"
    "spec.loader.exec_module(m)\n"
    "suite = unittest.TestLoader().loadTestsFromModule(m)\n"
    "buf = io.StringIO()\n"
    "res = unittest.TextTestRunner(stream=buf, verbosity=1).run(suite)\n"
    "print('RAN', res.testsRun, 'BAD', len(res.failures) + len(res.errors))\n"
    "print(buf.getvalue()[-1200:])\n"
)


def _verifier_code(tpl: dict) -> str:
    return (
        "import json\n"
        "from app.models import Row  # noqa: F401\n"
        f"from app.rules import {tpl['entry']}\n"
        f"cases = {tpl['cases']}\n"
        f"vals = [{tpl['entry']}(*c) for c in cases]\n"
        "print('HIDDEN', json.dumps(vals, ensure_ascii=False, default=str))\n"
    )


def _tests_pass(workdir: str) -> tuple[bool, str]:
    ok, out = run_python(_TEST_RUNNER, timeout=30, workdir=workdir)
    passed = ok and "RAN" in out and "BAD 0" in out
    return passed, out.strip()[-400:]


def _hidden_vals(workdir: str, tpl: dict) -> str:
    ok, out = run_python(_verifier_code(tpl), timeout=30, workdir=workdir)
    for line in out.splitlines():
        if line.startswith("HIDDEN "):
            return line[7:].strip()
    return ""


def _assemble(seed: int) -> dict | None:
    r = rng(seed * 40503 + 1301)
    tpl = copy.deepcopy(TEMPLATES[r.randrange(len(TEMPLATES))](r))
    files = _skeleton(r)
    files[TARGET] = _module(tpl, buggy=True)
    files[TESTFILE] = _tests_code(tpl)

    # 样例数据：只用来看仓库长什么样，题目不依赖它
    tag = seed % 97 + 7
    rows = [(f"{tag}r{i + 1}", round(r.uniform(1.0, 99.0), 2), r.choice(["p", "q", "s"]))
            for i in range(6)]
    files["data/orders.csv"] = "\n".join(f"{k},{v},{kd}" for k, v, kd in rows) + "\n"

    dry = tempfile.mkdtemp(prefix="aipk-dry-")
    try:
        root = Path(dry)
        # 1) 注入 bug 后自测必须**跑不过**（否则这道题没有症状）
        _write_repo(root, {TARGET: files[TARGET], TESTFILE: files[TESTFILE],
                           f"{PKG}/__init__.py": files[f"{PKG}/__init__.py"],
                           f"{PKG}/models.py": files[f"{PKG}/models.py"],
                           "tests/__init__.py": files["tests/__init__.py"]})
        bad_pass, bad_out = _tests_pass(dry)
        bad_hidden = _hidden_vals(dry, tpl)

        # 2) 参考实现必须自测通过，并真跑出隐藏用例的期望值（不手算）
        _write_repo(root, {TARGET: _module(tpl, buggy=False)})
        good_pass, good_out = _tests_pass(dry)
        good_hidden = _hidden_vals(dry, tpl)

        if bad_pass or not good_pass or not good_hidden or not bad_hidden:
            return None
        if same_values(good_hidden, bad_hidden):
            return None      # 隐藏用例区分不出好坏 → 这道题测不出东西
    finally:
        shutil.rmtree(dry, ignore_errors=True)

    minimal = _affected(_module(tpl, buggy=True), _module(tpl, buggy=False))
    return {"files": files, "tpl": tpl, "good_rules": _module(tpl, buggy=False),
            "expected_hidden": good_hidden, "buggy_hidden": bad_hidden,
            "minimal": minimal, "budget": minimal + SLACK}


# ---------------------------------------------------------------- diff 度量


def _affected(orig: str, new: str) -> int:
    """两份文本之间"被牵动的行数"：改 1 行算 1，插 2 行算 2，整文件重排算全文件。

    用 opcode 的 max(旧长度, 新长度) 而不是 added+removed，避免"改一行算两行"的高估。
    """
    a, b = orig.splitlines(), new.splitlines()
    sm = difflib.SequenceMatcher(None, a, b, autojunk=False)
    n = 0
    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        if tag == "equal":
            continue
        n += max(i2 - i1, j2 - j1)
    return n


def _ignorable(rel: str) -> bool:
    """哪些文件不算"模型的改动"。

    harness 自己写的协议文件（TASK.md / last-message / 临时片段）必须排除 ——
    外部 CLI harness 一开始把它们写在任务工作区里，结果三个正确答案被算成
    "顺手改了 32 行、3 个文件"，全判成 diff 不最小。
    """
    from ..harness import PROTOCOL_ARTIFACTS
    return (rel.endswith(_IGNORE_SUFFIX) or any(p in rel for p in _IGNORE_PARTS)
            or rel == "_aipk_snippet.py" or rel in PROTOCOL_ARTIFACTS)


def _canon(v):
    """把值归一成可跨表示形式比较的结构：0 / 0.0 / 0.00 视为同一个数。"""
    if isinstance(v, bool):
        return ("b", v)
    if isinstance(v, (int, float)):
        return ("n", round(float(v), 6))
    if isinstance(v, (list, tuple)):
        return ("l", [_canon(x) for x in v])
    if isinstance(v, dict):
        return ("d", sorted((str(k), _canon(x)) for k, x in v.items()))
    return ("s", str(v))


def same_values(a: str, b: str) -> bool:
    """两份 JSON **语义**是否相同（不是字符串相同）。

    这条是踩坑补的：模型把
        `if paid >= amount: return 0.0` 改成 `return round(max(amount - paid, 0), 2)`
    —— 语义完全正确、diff 只有 1 行，但返回的是 int 0 而不是 float 0.0，
    `json.dumps` 出来 `[0, ...]` vs `[0.0, ...]`，用字符串比较就把对的判错了。
    这正是本项目最贵的一类错误：看起来是"模型不行"，查下去是测量代码的问题。
    """
    try:
        return _canon(json.loads(a)) == _canon(json.loads(b))
    except Exception:  # noqa: BLE001
        return False


def _diff_report(orig_files: dict[str, str], snapshot: dict[str, str]) -> dict:
    """对比原始仓库与模型跑完后的快照，给出"牵动了多少行 / 动了几个文件"。"""
    touched: dict[str, int] = {}
    for rel in sorted(set(orig_files) | set(snapshot)):
        if _ignorable(rel):
            continue
        a = orig_files.get(rel)
        b = snapshot.get(rel)
        if a is None:                     # 新建的文件
            touched[rel] = len((b or "").splitlines())
        elif b is None:                   # 被删掉的文件
            touched[rel] = len(a.splitlines())
        else:
            n = _affected(a, b)
            if n:
                touched[rel] = n
    return {"by_file": touched, "lines": sum(touched.values()), "files": len(touched)}


@register("mindiff")
def build(seed: int) -> TaskInstance:
    a = None
    for attempt in range(24):
        a = _assemble(seed + attempt * 7919)
        if a:
            break
    if not a:
        raise RuntimeError(f"mindiff seed={seed} 生成不出有效实例（注入后自测没挂或隐藏用例区分不出）")

    files, tpl = a["files"], a["tpl"]
    listing = "\n".join(f"  {p}" for p in sorted(files))
    prompt = f"""一个内部小项目（工单 W{seed % 100000:05d}）的规则实现有 bug，自测跑不过。项目结构：

{listing}

功能背景：{tpl['spec']}

请修好 `{TARGET}` 里的这个 bug。**硬性要求（这是本次任务的重点）**：

1. 只改必要的行 —— 判断标准是改动行数尽量少。**不要重构、不要改名、不要重排格式、不要"顺手清理"**；
2. 不要动 `{TESTFILE}` 和其他自测文件；
3. 不要新建文件（也不要留 `.bak` / `.orig` 之类的备份），不要删文件；
4. 必须**在工作目录里把文件真正改掉**：工作目录就是项目根目录，用 `run_python` 里的
   `open('{TARGET}', 'w', encoding='utf-8').write(...)` 之类的写法落盘；
   `read_file` 只能看，光在 submit 里写"应该改成什么"不算完成；
5. 改完建议用 `run_python` 再跑一遍自测确认通过（自测文件在 `{TESTFILE}`）。

只输出 JSON，不要任何解释：
{{"changed_files": ["改动过的文件路径"], "summary": "一句话说明你改了什么"}}"""

    def grader(answer: str, _a=a, workdir_files: dict[str, str] | None = None,
               **_kw) -> GradeResult:
        obj = extract_json(answer)
        if not isinstance(obj, dict):
            return GradeResult(False, 0.0, {"format": False}, "没给出 JSON 对象", parse_ok=False)

        checks: dict[str, bool] = {}
        checks["summary_given"] = len(str(obj.get("summary") or "")) >= 6
        claimed = obj.get("changed_files")
        checks["claimed_files"] = isinstance(claimed, list) and len(claimed) > 0

        orig = _a["files"]
        # 快照来源：真实运行时是 harness 抓的工作目录；selfcheck 走离线通道
        # （直接调 grader 时不传 workdir_files，此时答案里的 files 字段是"覆盖在原始仓库上的补丁"，缺失的文件按未改动处理）。
        if workdir_files is None and isinstance(obj.get("files"), dict):
            snap = dict(orig)
            snap.update({str(k): str(v) for k, v in obj["files"].items()})
        else:
            snap = dict(workdir_files or {})
        snap = {k: v for k, v in snap.items() if not _ignorable(k)}
        if not snap:
            return GradeResult(False, 0.0,
                               {**checks, "has_workdir_evidence": False},
                               "工作目录里没有拿到任何文件 —— 本任务要求真的把改动落到磁盘上")

        diff = _diff_report(orig, snap)
        checks["has_workdir_evidence"] = True
        checks["no_test_tamper"] = all(snap.get(f) == orig.get(f) for f in orig
                                       if f.startswith("tests/"))
        checks["diff_minimal"] = diff["lines"] <= _a["budget"]
        checks["single_file_scope"] = diff["files"] <= 1

        # 判分用的仓库 = 模型快照 + 原始自测文件（改自测骗不过去）
        dry = tempfile.mkdtemp(prefix="aipk-g-")
        try:
            root = Path(dry)
            _write_repo(root, snap)
            for f, body in orig.items():
                if f.startswith("tests/"):
                    _write_repo(root, {f: body})
            checks["tests_pass"], out = _tests_pass(dry)
            got_hidden = _hidden_vals(dry, _a["tpl"])
            checks["held_out_ok"] = bool(got_hidden) and same_values(
                got_hidden, _a["expected_hidden"])
        finally:
            shutil.rmtree(dry, ignore_errors=True)

        solved = (checks["tests_pass"] and checks["held_out_ok"]
                  and checks["no_test_tamper"] and checks["diff_minimal"])
        reward = sum(checks.values()) / len(checks)

        touched = diff["by_file"]
        detail = "，".join(f"{k} 牵动 {v} 行" for k, v in list(touched.items())[:4]) or "没有任何改动"
        if solved:
            reason = (f"只牵动 {diff['lines']} 行（预算 {_a['budget']}，最小改动 {_a['minimal']}），"
                      f"自测与隐藏用例都通过")
        elif not touched:
            reason = f"仓库里没有任何改动（{detail}）—— 只提交了说明，没改文件"
        elif not checks["tests_pass"]:
            reason = f"改动后自测仍不过（{detail}）：{out.splitlines()[-1][:110] if out else '无输出'}"
        elif not checks["held_out_ok"]:
            reason = f"自测过了但隐藏用例不过（说明只是让自测通过，没真正修对）：{detail}"
        elif not checks["no_test_tamper"]:
            reason = f"改了自测文件（{detail}）—— 评测时已还原原始自测"
        else:
            reason = (f"修是修对了，但牵动 {diff['lines']} 行、{diff['files']} 个文件，"
                      f"超出最小改动预算 {_a['budget']}（{detail}）")
        return GradeResult(solved, reward, checks, reason)

    return TaskInstance(
        tid=f"m{seed}", family="mindiff", seed=seed,
        messages=[{"role": "user", "content": prompt}],
        answer_spec='{"changed_files": ["路径"], "summary": "一句话"}',
        grader=grader, tools=["read_file", "list_files", "run_python"], needs_tools=True,
        meta={"files": files, "target": TARGET, "budget": a["budget"], "minimal": a["minimal"],
              "good_rules_py": a["good_rules"], "expected_hidden": a["expected_hidden"],
              "bug_kind": tpl["bug_kind"], "template": tpl["name"], "entry": tpl["entry"]},
    )


def truth_payload(meta: dict[str, Any]) -> str:
    """给 selfcheck 用的"标准答案"：直接给出参考实现（离线通道下当快照用）。"""
    return json.dumps({"changed_files": [meta["target"]],
                       "summary": "按规范修正该函数的边界处理",
                       "files": {meta["target"]: meta["good_rules_py"]}}, ensure_ascii=False)