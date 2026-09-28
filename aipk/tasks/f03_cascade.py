"""族 3：级联调试（打的是"会跑代码验证，还是靠猜"）。

设计：给一段带隐蔽 bug 的函数 + 一段可运行的自测脚本。
要求模型：① 修好函数 ② 指出最小触发输入。
判定：把模型给的代码塞进测试脚本**真跑一遍**（不看它怎么说），再语义比对触发输入。

工程教训（都实际踩过）：
- 用例**必须由函数按参数生成**，不能存成字符串再 replace —— 字符串替换会静默失配
  （实测分页模板的替换就没生效，不同 seed 生成同一道题）。
- 模板必须 deepcopy，否则改一个 seed 会永久改坏模块级模板。
- 触发输入要**语义比对**：模型写 `[1.5,2.25]`、`items=[1.5,2.25]`、`fix([1.5,2.25])`
  都是对的，字符串比对会把它们全判错（实测 24/24 全灭，其实模型全答对了）。
"""
from __future__ import annotations

import ast
import copy
import json
import re

from . import GradeResult, TaskInstance, extract_json, register, rng, run_python


def _builders() -> list[dict]:
    """每个模板：good/bad 实现 + 由参数生成用例的函数 + 触发输入生成函数。"""
    return [
        {
            "name": "滑动窗口最大值",
            "good": "def fix(nums, k):\n    out = []\n    for i in range(len(nums) - k + 1):\n        out.append(max(nums[i:i+k]))\n    return out",
            "bad": "def fix(nums, k):\n    out = []\n    for i in range(len(nums) - k):\n        out.append(max(nums[i:i+k]))\n    return out",
            "cases": lambda d: (
                [((list(range(3 + d, 6 + d)) , 2), [3 + d, 4 + d, 4 + d, 5 + d]),
                 (([1, 2], 2), [2]), (([5], 1), [5]), (([2, 2, 2, 2], 3), [2, 2])],
                f"nums={list(range(3 + d, 6 + d))}, k=2",
            ),
            "bug_kind": "off-by-one 边界",
        },
        {
            "name": "累计求和（保留两位）",
            "good": "def fix(items):\n    total = 0.0\n    out = []\n    for x in items:\n        total += x\n        out.append(round(total, 2))\n    return out",
            "bad": "def fix(items):\n    total = 0.0\n    out = []\n    for x in items:\n        total = x\n        out.append(round(total, 2))\n    return out",
            "cases": lambda d: (
                [(([1.5, 2.25 + d / 100, 0.25],), [1.5, round(3.75 + d / 100, 2), round(4.0 + d / 100, 2)]),
                 (([1, 2, 3],), [1, 3, 6]), (([0.1, 0.2],), [0.1, 0.3])],
                "items=[1.5, " + f"{2.25 + d / 100:.2f}" + ", 0.25]",
            ),
            "bug_kind": "累加写成赋值",
        },
        {
            "name": "去重保序",
            "good": "def fix(items):\n    seen = set()\n    out = []\n    for x in items:\n        if x not in seen:\n            seen.add(x)\n            out.append(x)\n    return out",
            "bad": "def fix(items):\n    out = []\n    for x in items:\n        if x not in out:\n            out.append(x)\n    return out[::-1]",
            "cases": lambda d: (
                [(([d, 1, d, 2, 1],), [d, 1, 2]), (([1, 1, 1],), [1]),
                 ((['a', 'b', 'a'],), ['a', 'b'])],
                "items=" + repr([d, 1, d, 2, 1]),
            ),
            "bug_kind": "顺序被反转",
        },
        {
            "name": "区间合并",
            "good": "def fix(iv):\n    if not iv:\n        return []\n    iv = sorted(iv)\n    out = [list(iv[0])]\n    for a, b in iv[1:]:\n        if a <= out[-1][1]:\n            out[-1][1] = max(out[-1][1], b)\n        else:\n            out.append([a, b])\n    return [tuple(x) for x in out]",
            "bad": "def fix(iv):\n    if not iv:\n        return []\n    iv = sorted(iv)\n    out = [list(iv[0])]\n    for a, b in iv[1:]:\n        if a < out[-1][1]:\n            out[-1][1] = max(out[-1][1], b)\n        else:\n            out.append([a, b])\n    return [tuple(x) for x in out]",
            "cases": lambda d: (
                [(([(1, d), (d, d + 2)],), [(1, d + 2)]),
                 (([(1, 3), (2, 5), (8, 9)],), [(1, 5), (8, 9)]),
                 (([(1, 2)],), [(1, 2)])],
                "iv=" + repr([(1, d), (d, d + 2)]),
            ),
            "bug_kind": "相邻区间未合并（< vs <=）",
        },
        {
            "name": "分页切片",
            "good": "def fix(items, page, size):\n    start = (page - 1) * size\n    return items[start:start + size]",
            "bad": "def fix(items, page, size):\n    start = page * size\n    return items[start:start + size]",
            "cases": lambda d: (
                [((list(range(d, d + 6)), 1, 2), [d, d + 1]),
                 ((list(range(d, d + 6)), 3, 2), [d + 4, d + 5]),
                 (([1, 2, 3], 2, 5), [])],
                f"items={list(range(d, d + 6))}, page=1, size=2",
            ),
            "bug_kind": "页码从 0 起算",
        },
        {
            "name": "安全除法汇总",
            "good": "def fix(pairs):\n    out = []\n    for a, b in pairs:\n        out.append(round(a / b, 3) if b else None)\n    return out",
            "bad": "def fix(pairs):\n    out = []\n    for a, b in pairs:\n        out.append(round(a / b, 3))\n    return out",
            "cases": lambda d: (
                [(([(d * 2, 2), (1, 0)],), [float(d), None]),
                 (([(9, 3)],), [3.0])],
                "pairs=" + repr([(d * 2, 2), (1, 0)]),
            ),
            "bug_kind": "未处理除零",
        },
    ]


TEMPLATES = _builders()

TEST_RUNNER = ("ok = 0\n"
               "for args, want in cases:\n"
               "    got = fix(*args)\n"
               "    assert got == want, (args, got, want)\n"
               "    ok += 1\n"
               "print('ALLPASS', ok)\n")


def _probe(code: str, cases: list) -> tuple[bool, str]:
    """把一段实现 + 用例跑起来，返回 (是否全部通过, 输出)。"""
    src = code + "\n\ncases = " + repr(cases) + "\n" + TEST_RUNNER
    return run_python(src, timeout=25)


def _norm_expr(src: str):
    """把一个 python 表达式归一成可比较的元组；无法解析返回 None。

    要覆盖模型实际会用到的**全部**合法写法（每一种都实测出现过）：
      `items=[1.5, 2.25]`   → 关键字写法（不是合法表达式，要包一层调用才能 parse）
      `[1.5, 2.25]`         → 裸字面量
      `fix([1.5, 2.25])`    → 带函数名
      `[7,1,8,1,9], 2`      → 多参数（parse 成 Tuple）
      `([7,1,8,1,9], 2)`    → 多参数加圆括号，**会多包一层**，必须摊平（实测漏了这条）
      `([(1,0)],)`          → 单元素元组包着元组，也要摊平
    差一层就会把全对的回答判错 —— 这是测量错误，不是模型错误。
    """
    src = (src or "").strip()
    if not src:
        return None
    # 关键字写法：包一层调用再解析
    if re.match(r"^[A-Za-z_][A-Za-z0-9_]*\s*=", src):
        src = f"_f({src})"
    try:
        node = ast.parse(src, mode="eval").body
    except SyntaxError:
        return None

    def flat(v):
        """摊平嵌套元组；列表不摊（列表本身是数据）。"""
        if isinstance(v, tuple):
            out: list = []
            for x in v:
                out.extend(flat(x) if isinstance(x, tuple) else [x])
            return tuple(out)
        return (v,)

    if isinstance(node, ast.Call):
        # 关键字写法会落在 node.keywords 里，不是 node.args —— 这里必须都收
        parts: list[ast.expr] = list(node.args) + [kw.value for kw in node.keywords if kw.value is not None]
        if not parts:
            return None
        try:
            vals = tuple(ast.literal_eval(a) for a in parts)
        except Exception:  # noqa: BLE001
            return None
        return flat(vals) if len(vals) > 1 else flat(vals[0])
    try:
        v = ast.literal_eval(node)
    except Exception:  # noqa: BLE001
        return None
    return flat(v)


def _trigger_matches(model_expr: str, want: str) -> bool:
    """语义比对触发输入，而不是字符串比对。

    实测模型会用各种合法写法回答同一个触发输入：
      `items=[1.5, 2.25, 0.25]` / `[1.5, 2.25, 0.25]` / `fix([1.5, 2.25, 0.25])`
    字符串比对会把这三种都判错 —— 那是测量错误，不是模型错误。
    """
    got, exp = _norm_expr(model_expr), _norm_expr(want)
    return got is not None and got == exp


@register("cascade")
def build(seed: int) -> TaskInstance:
    for attempt in range(40):
        r = rng(seed * 104729 + 7 + attempt)
        tpl = copy.deepcopy(TEMPLATES[r.randrange(len(TEMPLATES))])
        delta = r.randint(2, 17)
        cases, trigger = tpl["cases"](delta)
        # 必须重新验证：正确实现能过、坏实现必挂（改参数可能让 bug 不再触发）
        good_ok, _ = _probe(tpl["good"], cases)
        bad_ok, bad_out = _probe(tpl["bad"], cases)
        if good_ok and not bad_ok:
            break
    else:
        raise RuntimeError(f"cascade seed={seed} 生成不出有效实例")

    cases_src = repr(cases)
    header = (
        "下面这个函数有 bug。\n\n"
        f"```python\n{tpl['bad']}\n```\n\n"
        "正确行为由下面的自测脚本给出（该脚本在修复后必须全部通过）：\n\n"
        f"```python\ncases = {cases_src}\n{TEST_RUNNER}```\n\n"
        "要求：\n"
        "1. 修好它（保持函数名 `fix`，签名不变）。\n"
        "2. 指出**最小的**触发输入（即上面 cases 里第一个失败的用例，写成 python 表达式）。\n"
        "3. 一句话说明 bug 性质。\n\n"
        "只输出 JSON，不要输出别的解释：\n"
        '{"fixed_code": "完整的 python 函数定义", "trigger_input": "python 表达式", "bug": "一句话"}'
    )

    def grader(answer: str, _tpl=tpl, _cases=cases, _trigger=trigger, **_kw) -> GradeResult:
        obj = extract_json(answer)
        if not isinstance(obj, dict) or not obj.get("fixed_code"):
            return GradeResult(False, 0.0, {"format": False}, "没给出 fixed_code", parse_ok=False)
        code = str(obj["fixed_code"])
        passed, out = _probe(code, _cases)
        checks = {"runs_and_passes": passed and "ALLPASS" in out}
        got_trigger = str(obj.get("trigger_input") or "")
        checks["trigger_correct"] = _trigger_matches(got_trigger, _trigger)
        checks["bug_explained"] = len(str(obj.get("bug") or "")) >= 4
        solved = checks["runs_and_passes"] and checks["trigger_correct"]
        reward = sum(checks.values()) / len(checks)
        if solved:
            reason = "修复通过全部用例，触发输入正确"
        elif not checks["runs_and_passes"]:
            last = out.strip().splitlines()[-1][:160] if out.strip() else "无输出"
            reason = f"修复后测试未通过：{last}"
        else:
            reason = f"代码对了，但触发输入不对：给的是 {got_trigger!r}"
        return GradeResult(solved, reward, checks, reason)

    return TaskInstance(
        tid=f"d{seed}", family="cascade", seed=seed,
        messages=[{"role": "user", "content": header}],
        answer_spec='{"fixed_code": "...", "trigger_input": "...", "bug": "..."}',
        grader=grader, tools=[], needs_tools=False,
        meta={"template": tpl["name"], "bug_kind": tpl["bug_kind"], "bad_code": tpl["bad"]},
    )


def _selfcheck() -> str:
    """开发自检：确认每个模板的 bad 实现真的会挂、good 真的会过。"""
    lines = []
    for tpl in TEMPLATES:
        cases, _ = tpl["cases"](5)
        bad_ok, bad_out = _probe(tpl["bad"], cases)
        good_ok, _ = _probe(tpl["good"], cases)
        last = bad_out.strip().splitlines()[-1][:70] if bad_out.strip() else ""
        lines.append(f"{tpl['name']:16s} bad_fails={not bad_ok!s:<5s} good_passes={good_ok!s:<5s} {last}")
    return "\n".join(lines)


if __name__ == "__main__":
    print(json.dumps({"templates": len(TEMPLATES)}, ensure_ascii=False))
    print(_selfcheck())
