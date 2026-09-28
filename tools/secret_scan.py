"""开源前的自检：确认不会把密钥/隐私写进仓库。

规则很朴素，但必须机器跑一遍 —— "我记得没写密钥"是最不可靠的保证。
"""
from __future__ import annotations

import re
from pathlib import Path

# 要检查的文本类型
EXTS = {".py", ".md", ".yaml", ".yml", ".toml", ".txt", ".json", ".jsonl", ".cfg", ".ini", ".sh"}
SKIP_DIRS = {".git", "runs", "__pycache__", ".venv", "venv", "node_modules", "build", "dist"}

# 常见密钥形态（命中就人工确认）
PATTERNS = [
    ("OpenAI 风格 key", re.compile(r"sk-[A-Za-z0-9_\-]{16,}")),
    ("Bearer 字面量", re.compile(r"Bearer\s+[A-Za-z0-9_\-\.]{20,}")),
    ("长十六进制串（可能是 key/hash）", re.compile(r"\b[a-f0-9]{32,}\b", re.I)),
    ("疑似 api_key 赋值", re.compile(r"api_?key\s*[:=]\s*[\"'][^\"'\s]{16,}[\"']", re.I)),
    ("本机绝对用户路径", re.compile(r"[A-Za-z]:\\Users\\[^\\\s]+", re.I)),
    ("邮箱", re.compile(r"[\w\.\-]+@[\w\-]+\.[a-z]{2,}", re.I)),
]

# 允许出现的例外（文档里的示例、公开信息）
ALLOW = {
    "sk-xxx",                       # 配置模板里的占位符
    "C:\\Users\\20684\\.dsh",       # 文档里说明凭据位置（不含密钥本身）
    "C:/Users/20684/.dsh",
    "users/20684/.codex",           # 同理
    "git@github.com",               # SSH 远程地址，不是密钥（邮箱正则的误报）
    "noreply.github.com",           # 提交用的公开邮箱
    "xmyl-153@",                    # （若有）作者署名
}

hits: list[tuple[str, int, str, str]] = []
for p in sorted(Path(".").rglob("*")):
    if not p.is_file() or p.suffix.lower() not in EXTS:
        continue
    if any(part in SKIP_DIRS for part in p.parts):
        continue
    try:
        text = p.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        continue
    for i, line in enumerate(text.splitlines(), 1):
        if any(a.lower() in line.lower() for a in ALLOW):
            continue
        for name, rx in PATTERNS:
            m = rx.search(line)
            if m:
                hits.append((str(p), i, name, m.group(0)[:60]))

print(f"扫描完成：命中 {len(hits)} 处")
for path, line, name, sample in hits:
    print(f"  [{name}] {path}:{line}  → {sample}")
if not hits:
    print("没有发现疑似密钥/隐私。")
