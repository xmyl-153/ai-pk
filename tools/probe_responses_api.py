"""一次性探针：本项目网关是否支持 OpenAI Responses API。

Codex CLI 0.155 只认 `wire_api = "responses"`（chat 已被移除）。
如果 tokenrhythm 网关支持 /responses，就能让 Codex 跑**同一个网关上的同一个模型**，
把"换壳子"和"换供给"两个因素彻底分开。
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import httpx  # noqa: E402

from aipk.config import load_gateways  # noqa: E402

gw = load_gateways()["jiyuanapi"]
url = gw["base_url"] + "/responses"
H = {"Authorization": f"Bearer {gw['api_key']}", "Content-Type": "application/json"}


def probe(label: str, body: dict, stream: bool = False) -> None:
    print("=" * 60)
    print(label, "| stream =", stream)
    try:
        if stream:
            with httpx.stream("POST", url, json=body, timeout=60, headers=H) as r:
                print("status:", r.status_code)
                head = ""
                for chunk in r.iter_text():
                    head += chunk
                    if len(head) > 300:
                        break
                print("body head:", head[:300].replace("\n", " "))
        else:
            r = httpx.post(url, json=body, timeout=60, headers=H)
            print("status:", r.status_code)
            print("body:", r.text[:400])
    except Exception as e:  # noqa: BLE001
        print("请求失败：", type(e).__name__, e)


base = {"model": "deepseek-flash", "input": "只回复两个字：收到", "max_output_tokens": 64}

probe("1) 非流式、普通 input", base)
probe("2) 流式", {**base, "stream": True}, stream=True)
probe("3) 带 tools + instructions", {
    **base,
    "instructions": "你是一个被评测的 AI 助手。",
    "tools": [{"type": "function", "name": "list_files", "description": "列出文件",
               "parameters": {"type": "object", "properties": {}},
               "strict": False}],
    "tool_choice": "auto",
    "stream": True,
}, stream=True)
# Codex 的真实请求还会带这些字段；逐个加上看哪一项会让网关说“模型不可用”
probe("4) + reasoning.effort（Codex 一定带）", {
    **base, "stream": True, "store": True,
    "reasoning": {"effort": "low", "summary": "auto"},
}, stream=True)
probe("5) + reasoning + include + parallel_tool_calls", {
    **base, "stream": True, "store": True,
    "reasoning": {"effort": "low", "summary": "auto"},
    "include": ["reasoning.encrypted_content"],
    "parallel_tool_calls": True,
    "prompt_cache_key": "aipk-probe",
}, stream=True)


