"""对战台面板：一个本地小网页，把两个"选手"拖进左右场地，同一批题各跑一遍。

为什么做成一个页面而不是桌面程序：

- 这个项目本来就是纯 Python + 标准库能跑的东西，不想为了界面拖进 Qt/Electron 这类重依赖
  （依赖一重，别人就不愿意 clone 了）；
- 拖拽、左右分栏、毛玻璃这些用 HTML/CSS 写最省事，也最容易改；
- 只监听 127.0.0.1，不联网上传任何东西，密钥照旧只从环境变量/凭据文件读，不落盘。

跟 `aipk run` 的关系：面板不自己造轮子，它就是把 `Runner` 包装成一个后台任务 ——
进度直接从 `runs/<run_id>/runs.jsonl` 里读（Runner 每完成一次就 flush 一行），
所以页面上滚动的日志和落盘的证据是同一份东西。
"""
from __future__ import annotations

import json
import mimetypes
import queue as _queue
import socket
import subprocess
import threading
import time
import urllib.error
import urllib.request
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from . import __version__
from .config import ROSTER, ModelSpec, RunConfig, load_gateways
from .tasks import all_families

WEB_DIR = Path(__file__).resolve().parent / "web"
REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_PORT = 8771

# 快速局：挑 6 个判定完全靠代码、不需要多轮的族，十几秒能打完
QUICK_FAMILIES = ["constraint", "cascade", "toolchain", "datatransform", "compliance", "premise"]

# 离线的"假选手"：不连网关、不花钱，用来演示装置本身通不通
OFFLINE_BOTS: list[tuple[str, str, str]] = [
    ("oracle", "满分机器人", "照着标准答案作答 —— 它应该 100% 通过"),
    ("half", "半桶水机器人", "约一半的族会答对，像个偏科的选手"),
    ("slow", "慢吞吞机器人", "答得都对，但每次都要磨蹭一会儿"),
    ("wrong", "错答机器人", "格式合法、内容全错 —— 它应该 0% 通过"),
]

_RUNS: dict[str, dict[str, Any]] = {}
_LOCK = threading.Lock()
# 排队：一次只跑一局（一起压网关会触发限流，反而把模型打成假 0 分），后面的在队列里等
_JOBQ: "_queue.Queue[str]" = _queue.Queue()
_DISP_STARTED = False


def _ensure_dispatcher() -> None:
    global _DISP_STARTED
    with _LOCK:
        if _DISP_STARTED:
            return
        _DISP_STARTED = True
    threading.Thread(target=_dispatcher, daemon=True, name="pk-dispatch").start()


def _dispatcher() -> None:
    while True:
        rid = _JOBQ.get()
        with _LOCK:
            entry = _RUNS.get(rid)
        if entry is None:
            _JOBQ.task_done()
            continue
        with _LOCK:
            entry["status"] = "running"
        try:
            entry["_work"]()
        finally:
            _JOBQ.task_done()


# ---------------------------------------------------------------- 选手名单


def _spec_from_key(key: str) -> ModelSpec:
    prov, _, model = (key or "").partition("/")
    if prov == "scripted":
        for mode, name, _note in OFFLINE_BOTS:
            if mode == model:
                return ModelSpec("scripted", mode, name, "offline")
        raise KeyError(f"没有这个离线机器人：{model}")
    for p, m, name, tier in ROSTER:
        if p == prov and m == model:
            return ModelSpec(p, m, name, tier)
    raise KeyError(f"名单里没有这个模型：{key}")


def _gateways() -> dict[str, dict]:
    """拿到网关配置；一份都没有也不报错（离线机器人不需要网关）。"""
    try:
        return load_gateways(strict=False)
    except Exception:  # noqa: BLE001  配置写坏了不该让面板起不来
        return {}


def _state() -> dict:
    gws = _gateways()
    offline = [{"key": f"scripted/{mode}", "name": name, "kind": "离线", "note": note,
                "enabled": True} for mode, name, note in OFFLINE_BOTS]
    real: list[dict] = []
    for prov, model, name, tier in ROSTER:
        gw = gws.get(prov) or {}
        ok = bool(gw.get("api_key"))
        if not ok:
            continue          # 没密钥的模型不摆出来，免得点了一路报错
        real.append({"key": f"{prov}/{model}", "name": name,
                     "kind": "旗舰" if tier == "flagship" else "轻量",
                     "note": f"{prov} · 会真的花钱", "enabled": True})
    note = ""
    if not real:
        note = "没检测到可用的网关：先用离线机器人玩一局；想接自己的模型，编辑 aipk.config.yaml（`python -m aipk init` 生成模板）。"
    return {"offline": offline, "models": real, "gateways": sorted(gws),
            "usable": len(real), "note": note}


# ---------------------------------------------------------------- 打完怎么算分


def _summarize(results: list, spec: ModelSpec) -> dict:
    mine = sorted([r for r in results if r.model_key == spec.key], key=lambda r: r.task_key)
    graded = [r for r in mine if not r.infra_failure]
    total = len(graded)
    solved = sum(1 for r in graded if r.solved)
    tokens = sum(r.prompt_tokens + r.completion_tokens for r in graded)
    reports_tokens = any((r.prompt_tokens or r.completion_tokens) for r in graded)
    rea = sum(r.reasoning_tokens for r in graded)
    rea_reported = any(r.reasoning_tokens for r in graded)
    return {
        "key": spec.key,
        "name": spec.name,
        "solved": solved,
        "total": total,
        "rate": (solved / total) if total else 0.0,
        "avg_ms": (sum(r.total_ms for r in graded) / total) if total else 0.0,
        "avg_turns": (sum(r.turns for r in graded) / total) if total else 0.0,
        "avg_tok": (tokens / total) if (total and reports_tokens) else None,
        "tokens": tokens if reports_tokens else None,
        # 推理 token 占比：v4pro 那类"thinking 挤爆输出预算"的体感，就体现在这里
        "rea_ratio": (rea / tokens) if (reports_tokens and rea_reported) else None,
        "infra": sum(1 for r in mine if r.infra_failure),
        "cells": ["inf" if r.infra_failure else ("ok" if r.solved else "no") for r in mine],
        "per_task": {r.task_key: (None if r.infra_failure else r.solved) for r in mine},
    }


def _verdict(L: dict, R: dict, fam_name: dict[str, str]) -> dict:
    """谁赢了 —— 通过率先说话；分数拉不开，就让速度和成本说话。"""
    bullets: list[str] = []
    if L["total"] == 0 or R["total"] == 0:
        return {"verdict": "tie", "headline": "这局没打成（没有有效样本）", "bullets": ["检查一下日志。"]}

    lr, rr = L["rate"], R["rate"]
    if abs(lr - rr) > 1e-9:
        win = "left" if lr > rr else "right"
        w, l = (L, R) if win == "left" else (R, L)
        diff = w["solved"] - l["solved"]
        tight = "就差一道题，险胜" if diff == 1 else f"多拿下 {diff} 道题"
        headline = f"{w['name']} 赢下这一局 —— {w['solved']}:{l['solved']}，{tight}"
    else:
        win = "tie"
        headline = f"{L['solved']}:{R['solved']} 打平 —— 分数一样，那就看谁快、谁省"

    bullets.append(
        f"<b>通过率</b>：{L['name']} {L['rate'] * 100:.0f}%（{L['solved']}/{L['total']}），"
        f"{R['name']} {R['rate'] * 100:.0f}%（{R['solved']}/{R['total']}）。")
    if L["infra"] or R["infra"]:
        bullets.append(
            f"<b>基础设施故障</b>：{L['name']} {L['infra']} 次、{R['name']} {R['infra']} 次 —— "
            "这类失败（限流/网络）不算模型答错，已经在分母里剔掉了。")

    # 谁在哪类题上丢分：同一批题逐题比，才有资格说"差距在哪"
    only_l = [k for k, v in L["per_task"].items() if v is True and R["per_task"].get(k) is False]
    only_r = [k for k, v in R["per_task"].items() if v is True and L["per_task"].get(k) is False]
    fam_of = lambda ks: "、".join(sorted({fam_name.get(k.split(":")[0], k.split(":")[0]) for k in ks}))
    if only_l:
        bullets.append(f"<b>{L['name']} 独赢的题</b>（对方在这几道上栽了）：{fam_of(only_l)}。")
    if only_r:
        bullets.append(f"<b>{R['name']} 独赢的题</b>（对方在这几道上栽了）：{fam_of(only_r)}。")
    if not only_l and not only_r:
        bullets.append("两边在每一道题上的结果都一样 —— 这一批题没把它们分开。")

    # 速度与成本：正确率饱和之后，真正拉开差距的地方
    if L["avg_ms"] and R["avg_ms"]:
        fast, slow_ = (L, R) if L["avg_ms"] < R["avg_ms"] else (R, L)
        times = slow_["avg_ms"] / max(fast["avg_ms"], 1.0)
        if times >= 1.25:
            bullets.append(
                f"<b>速度</b>：{fast['name']} 平均 {fast['avg_ms'] / 1000:.2f}s/题，"
                f"{slow_['name']} 平均 {slow_['avg_ms'] / 1000:.2f}s/题 —— 差 {times:.1f} 倍"
                + ("。分数打平的时候，这个差距就是全部的区别。" if win == "tie" else "。"))
        else:
            bullets.append(
                f"<b>速度</b>：{L['name']} {L['avg_ms'] / 1000:.2f}s/题，"
                f"{R['name']} {R['avg_ms'] / 1000:.2f}s/题，基本一样快。")
    if L["tokens"] is not None and R["tokens"] is not None:
        bullets.append(f"<b>账单</b>：{L['name']} {L['tokens']} token，{R['name']} {R['tokens']} token。")
    else:
        bullets.append("<b>账单</b>：离线机器人不上报 token —— 接上真模型才会出现这一行。")

    if L["key"].startswith("scripted/") and R["key"].startswith("scripted/"):
        bullets.append("两边都是离线机器人：它们只在验证「这把尺子准不准」，不代表任何模型的真实水平。")

    fam_name_only = None
    return {"verdict": win, "headline": headline, "bullets": bullets,
            "fam_name_only": fam_name_only}


# ---------------------------------------------------------------- 跑一局


def _start_models(keys: list[str], scope: str) -> dict:
    """跑一局。keys 是 2 个 → 比分模式（带胜方判定）；3 个及以上 → 跑分模式（排行榜）。"""
    from .runner import Runner

    keys = list(dict.fromkeys(k for k in keys if k))[:8]      # 去重、封顶 8 个，免得跑太久
    if len(keys) < 1:
        raise ValueError("至少选一个选手")
    specs = [_spec_from_key(k) for k in keys]
    mode = "pk" if len(specs) == 2 else "score"
    fams = QUICK_FAMILIES if scope != "full" else all_families()
    cfg = RunConfig(reps=1, tasks_per_family=1, max_turns=40,  # 分阶段族要够用的轮数
                    max_workers=4, judge_models=[])            # 面板不做盲评：省时间也省钱
    runner = Runner(cfg, profile_name="frozen-v1", verbose=False)

    run_id = runner.run_id
    total = len(fams) * len(specs)
    fam_name = {f: f for f in fams}

    def work() -> None:
        try:
            # 真模型但没配好网关时，这里会抛出人话错误（延迟加载的好处：离线不受影响）
            results, _out = runner.run(models=specs, families=fams, judge_models=[])
            summaries = [_summarize(results, s) for s in specs]
            res: dict[str, Any] = {"models": summaries, "mode": mode,
                                   "out": str(runner.out_dir.relative_to(REPO_ROOT))}
            if mode == "pk":
                L, R = summaries
                res.update({"left": L, "right": R, **_verdict(L, R, fam_name)})
            with _LOCK:
                _RUNS[run_id].update({"status": "done", "done": total, "result": res})
        except Exception as e:  # noqa: BLE001  面板里任何异常都要能在页面上看见
            with _LOCK:
                _RUNS[run_id].update({"status": "error",
                                      "error": f"{type(e).__name__}: {str(e)[:300]}"})

    with _LOCK:
        _RUNS[run_id] = {"status": "queued", "done": 0, "total": total, "result": None,
                         "error": None, "mode": mode, "_work": work,
                         "out": str(runner.out_dir.relative_to(REPO_ROOT)),
                         "started": time.time()}
    _JOBQ.put(run_id)                 # 排队：一次只跑一局，后面的自动等
    _ensure_dispatcher()
    return {"run_id": run_id, "total": total, "mode": mode, "queue": _JOBQ.qsize()}


def _start(left_key: str, right_key: str, scope: str) -> dict:
    return _start_models([left_key, right_key], scope)


def _progress(run_id: str) -> dict:
    with _LOCK:
        run = _RUNS.get(run_id)
        if run is None:
            return {"status": "error", "error": "没有这一局（面板重启过？再打一次就好）"}
        snap = {k: v for k, v in run.items() if not k.startswith("_")}   # _work 不可 JSON 序列化
    if snap["status"] == "queued":
        lines = [f"排队中：队列里还有 {_JOBQ.qsize()} 局，轮到就自动开始"
                 "（一次只跑一局，避免一起压网关触发限流）。"]
    else:
        lines = ["开跑：同一批题，各跑一遍。"]
    log_path = REPO_ROOT / snap["out"] / "runs.jsonl"
    if log_path.exists():
        rows = []
        for line in log_path.read_text(encoding="utf-8", errors="replace").splitlines():
            try:
                rows.append(json.loads(line))
            except Exception:  # noqa: BLE001
                continue
        for d in rows[-60:]:
            mark = "INF" if d.get("infra_failure") else ("OK " if d.get("solved") else "X  ")
            lines.append(f"[{mark}] {d.get('model_key', ''):<22} {d.get('task_key', ''):<30} "
                         f"轮数={d.get('turns')} 耗时={d.get('total_ms')}ms")
        snap["done"] = len(rows)
    if snap["status"] == "done":
        lines.append(f"打完：证据落在 {snap['out']}/（报告、原始记录都在里面）")
    if snap["status"] == "error":
        lines.append(f"出错了：{snap['error']}")
    snap["log"] = lines
    return snap


# ---------------------------------------------------------------- 关于 / 更新


def _about() -> dict:
    return {
        "name": "AI PK · 模型对战台",
        "version": __version__,
        "author": "xmyl-153（星梦幽灵）",
        "homepage": "https://github.com/xmyl-153/ai-pk",
        "issues": "https://github.com/xmyl-153/ai-pk/issues",
        "contact": "提 Issue / PR 是最快的联系方式；面板只在你本机跑，不会上传任何东西。",
    }


def _gh_json(path: str) -> Any:
    req = urllib.request.Request(
        "https://api.github.com" + path,
        headers={"Accept": "application/vnd.github+json", "User-Agent": "aipk-panel"})
    with urllib.request.urlopen(req, timeout=6) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _local_head() -> str | None:
    """当前 checkout 的提交 sha 前 7 位；不在 git 环境里就返回 None。"""
    try:
        out = subprocess.run(["git", "rev-parse", "HEAD"],
                             cwd=str(Path(__file__).resolve().parent.parent),
                             capture_output=True, timeout=5, text=True)
        sha = (out.stdout or "").strip()
        return sha[:7] if out.returncode == 0 and len(sha) >= 7 else None
    except Exception:  # noqa: BLE001  没有 git 也算正常，只是比不了提交
        return None


def _update() -> dict:
    """尽力联网查一下有没有新版本；三级回退：release 标签 → 任意 tag → 默认分支最新提交。

    仓库没发过 release 时 `releases/latest` 会 404（旧版把这当成"联网失败"），
    所以 404 要落到下一级；只有真连不上 GitHub 才报 ok=False。
    """
    cur = __version__
    base = {"current": cur, "latest": None, "has_update": False}
    try:
        tag = ""
        try:
            tag = str(_gh_json("/repos/xmyl-153/ai-pk/releases/latest").get("tag_name") or "").lstrip("v")
        except urllib.error.HTTPError as e:
            if e.code != 404:  # 404 = 没发过 release，不是故障
                raise
        if not tag:
            tags = _gh_json("/repos/xmyl-153/ai-pk/tags?per_page=1")
            tag = str((tags[0].get("name") if tags else "") or "").lstrip("v")
        commits = _gh_json("/repos/xmyl-153/ai-pk/commits?per_page=1")
        first = commits[0] if commits else {}
        remote_head = str(first.get("sha") or "")[:7]
        remote_date = str(((first.get("commit") or {}).get("committer") or {}).get("date") or "")[:10]
    except urllib.error.HTTPError as e:
        return {**base, "ok": False,
                "note": f"GitHub 接口返回 {e.code}（限流或地址变动）；要更新请手动 `git pull`。"}
    except Exception as e:  # noqa: BLE001  联网失败不该影响面板
        return {**base, "ok": False,
                "note": f"连不上 GitHub（{type(e).__name__}，可能离线或被网络拦截）；要更新请手动 `git pull`。"}

    if tag:
        has = tag != cur
        note = (f"发现新版本 v{tag}（当前 v{cur}），`git pull` 更新。" if has
                else f"当前 v{cur} 与最新标签 v{tag} 一致，已是最新。")
        return {**base, "ok": True, "latest": tag, "has_update": has, "note": note}
    local_head = _local_head()
    if remote_head and local_head and remote_head != local_head:
        return {**base, "ok": True, "latest": cur, "has_update": True,
                "note": f"远端还没打版本标签，但最新提交 {remote_head}（{remote_date}）与本地 {local_head} 不同；`git pull` 更新。"}
    if remote_head and local_head:
        return {**base, "ok": True, "latest": cur,
                "note": f"本地提交与远端最新提交 {remote_head}（{remote_date}）一致，已是最新。"}
    where = f"远端最新提交 {remote_head or '未知'}" + (f"（{remote_date}）" if remote_date else "")
    return {**base, "ok": True, "latest": cur,
            "note": f"远端还没发版本标签；{where}" + (f"，本地 {local_head}" if local_head else "")
                    + "。要更新请 `git pull` 后对比。"}


# ---------------------------------------------------------------- HTTP


class _Handler(BaseHTTPRequestHandler):
    server_version = "aipk-panel"

    def log_message(self, *args) -> None:  # noqa: D102  静音：面板自己的日志够看了
        pass

    # ---- 小工具 ----
    def _json(self, obj: Any, code: int = 200) -> None:
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _file(self, path: Path) -> None:
        if not path.exists() or not path.is_file():
            self._json({"error": "没有这个文件"}, 404)
            return
        data = path.read_bytes()
        ctype = mimetypes.guess_type(str(path))[0] or "application/octet-stream"
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")   # 改完页面刷新就能看到
        self.end_headers()
        self.wfile.write(data)

    # ---- 路由 ----
    def do_GET(self) -> None:  # noqa: N802
        path = self.path.split("?", 1)[0]
        if path in ("/", "/index.html"):
            return self._file(WEB_DIR / "index.html")
        if path.startswith("/static/"):
            rel = path[len("/static/"):]
            target = (WEB_DIR / rel).resolve()
            if WEB_DIR.resolve() not in target.parents and target != WEB_DIR.resolve():
                return self._json({"error": "越界了"}, 403)
            return self._file(target)
        if path == "/api/state":
            return self._json(_state())
        if path == "/api/about":
            return self._json(_about())
        if path == "/api/update":
            return self._json(_update())
        if path.startswith("/api/pk/"):
            return self._json(_progress(path[len("/api/pk/"):]))
        return self._json({"error": "没有这个页面"}, 404)

    def do_POST(self) -> None:  # noqa: N802
        n = int(self.headers.get("Content-Length") or 0)
        try:
            payload = json.loads(self.rfile.read(n) or b"{}")
        except Exception:  # noqa: BLE001
            return self._json({"error": "请求体不是 JSON"}, 400)
        try:
            if self.path == "/api/pk":
                out = _start(str(payload.get("left") or ""), str(payload.get("right") or ""),
                             str(payload.get("scope") or "quick"))
            elif self.path == "/api/score":
                models = payload.get("models") or []
                if isinstance(models, str):
                    models = [models]
                out = _start_models([str(m) for m in models], str(payload.get("scope") or "quick"))
            else:
                return self._json({"error": "没有这个接口"}, 404)
            return self._json(out)
        except Exception as e:  # noqa: BLE001
            return self._json({"error": f"{type(e).__name__}: {str(e)[:200]}"}, 400)


def _free_port(start: int, tries: int = 20) -> int:
    for p in range(start, start + tries):
        with socket.socket() as s:
            if s.connect_ex(("127.0.0.1", p)) != 0:
                return p
    return start


def serve(port: int = DEFAULT_PORT, open_browser: bool = True) -> None:
    port = _free_port(port)
    httpd = ThreadingHTTPServer(("127.0.0.1", port), _Handler)
    url = f"http://127.0.0.1:{port}/"
    print("AI PK · 对战台")
    print(f"  面板地址：{url}（只在本机可访问，关掉这个窗口就停了）")
    print("  玩法：把左边和右边的选手拖好，点「开打」——同一批题各跑一遍，谁更能打当场见分晓。")
    print("  没配网关也能玩：选手池里的「离线机器人」不连任何网关、不花钱。")
    if open_browser:
        threading.Timer(0.6, lambda: webbrowser.open(url)).start()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n收到 Ctrl+C，收工。")
    finally:
        httpd.server_close()


if __name__ == "__main__":  # pragma: no cover
    serve()
