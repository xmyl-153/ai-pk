"""网关 / 模型名单 / 运行预算配置。

密钥不落盘：运行时从环境变量或 DSH 的 credentials 读。

**两种配置来源**（开源版的关键：别人不该被迫用你的凭据系统）：
  1. 用户配置文件 `aipk.config.yaml`（当前目录，或 `AIPK_CONFIG` 指向的位置）
     —— 写自己的 providers（base_url + api_key_env）和 roster，跨平台通用；
  2. 没配就回退到 DSH 的 `~/.dsh/settings.yaml` + `.credentials.yaml`
     —— 作者本机的用法，方便但不可移植。

还有第三种"模型"来源：`scripted`（离线演示用，不连任何网关、不需要 key），
见 `aipk/scripted.py` —— `python -m aipk demo` 走的就是它。
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

import yaml

DSH_HOME = Path(os.environ.get("DSH_HOME") or (Path.home() / ".dsh"))
CRED_FILE = DSH_HOME / ".credentials.yaml"
SETTINGS_FILE = DSH_HOME / "settings.yaml"

# 用户配置文件搜索顺序（第一个存在的生效）
USER_CONFIG_NAMES = ("aipk.config.yaml", "aipk.config.yml")

# 作者默认名单：8 个跨厂商旗舰 + 2 个轻量对照
# (provider_id, model_id, 展示名, tier)
DEFAULT_ROSTER: list[tuple[str, str, str, str]] = [
    ("alibailian", "deepseek-v4-pro", "DeepSeek V4 Pro", "flagship"),
    ("jiyuanapi", "qwen3.8-max", "Qwen3.8 Max", "flagship"),
    ("jiyuanapi", "glm-5.3", "GLM-5.3", "flagship"),
    ("alibailian", "kimi-k3", "Kimi K3", "flagship"),
    ("jiyuanapi", "seed-2.1-pro", "Seed 2.1 Pro", "flagship"),
    ("jiyuanapi", "longcat-2.0", "LongCat 2.0", "flagship"),
    ("zcode-api-key", "glm-4.7", "GLM-4.7", "flagship"),
    ("jiyuanapi", "deepseek-flash", "DeepSeek V4.1 (flash)", "flagship"),
    # 轻量对照
    ("jiyuanapi", "glm-5.3-flash", "GLM-5.3 Flash", "light"),
    ("alibailian", "qwen3.8-flash", "Qwen3.8 Flash", "light"),
]

# 已知本机不可用（余额/未开通），保留记录避免重复踩坑
KNOWN_UNAVAILABLE = {
    "zcode-api-key/glm-4.6": "余额不足或无可用资源包",
    "zcode-api-key/glm-5.3": "余额不足或无可用资源包",
    "zcode-api-key/glm-5.3-flash": "余额不足或无可用资源包",
    "alibailian/ZHIPU/GLM-5.3": "产品未开通",
}


def _load_yaml(p: Path) -> dict:
    if not p.exists():
        raise FileNotFoundError(f"缺少配置文件：{p}")
    return yaml.safe_load(p.read_text(encoding="utf-8")) or {}


def user_config_path() -> Path | None:
    """找到用户配置文件（`AIPK_CONFIG` 优先，其次当前目录的两个约定名）。"""
    env = os.environ.get("AIPK_CONFIG")
    if env:
        p = Path(env)
        return p if p.exists() else None
    for name in USER_CONFIG_NAMES:
        p = Path.cwd() / name
        if p.exists():
            return p
    return None


def load_user_config() -> dict:
    p = user_config_path()
    return (yaml.safe_load(p.read_text(encoding="utf-8")) or {}) if p else {}


def example_config_text() -> str:
    return '''# AI PK 配置：接你自己的 OpenAI 兼容网关
#
#   1. 另存为 aipk.config.yaml（或跑 `python -m aipk init`）
#   2. 把密钥放进环境变量（不要把密钥写进文件后提交）
#   3. `python -m aipk list` 确认读到了
#
# 没有这个文件时，程序回退到 DSH 的 ~/.dsh/settings.yaml + .credentials.yaml。
# 只想先跑跑看：`python -m aipk demo`（离线、不需要任何 key）。

providers:
  mygateway:
    base_url: https://api.example.com/v1
    api_key_env: MY_GATEWAY_API_KEY     # 从环境变量读（推荐）
    display_name: 我的网关
    # api_key: sk-xxx                   # 也可以直接写死，但别提交到仓库

roster:
  # [provider_id, model_id, 展示名, tier(flagship|light)]
  - [mygateway, some-strong-model, "某旗舰模型", flagship]
  - [mygateway, some-fast-model,   "某轻量模型", light]

run:                # 可选：覆盖默认运行参数
  qps_per_gateway: 1.2   # 每网关限速，**不限速会被 429 打成假 0 分**
  workers: 6
  max_turns: 20
'''


def load_gateways(strict: bool = True) -> dict[str, dict]:
    """返回 {provider_id: {base_url, api_key, display_name}}。

    strict=False 时，一份配置都找不到就返回 {} 而不是抛错 —— 离线 demo 跑的是
    scripted 模型，一个网关都不需要，不该因为"这台机器上没配网关"而跑不起来
    （见缺陷 #20：CI 上第一次跑 demo 就是被这个卡住的）。
    """
    cfg = load_user_config()
    provs_user = cfg.get("providers") or {}
    if provs_user:
        out: dict[str, dict] = {}
        for pid, c in provs_user.items():
            env_name = c.get("api_key_env")
            key = (os.environ.get(env_name, "") if env_name else "") or c.get("api_key", "")
            out[pid] = {"base_url": str(c.get("base_url", "")).rstrip("/"),
                        "api_key": key,
                        "display_name": c.get("display_name") or pid}
        missing = [p for p, v in out.items() if not v["api_key"]]
        if missing:
            print(f"[warn] 这些网关没读到密钥（检查 api_key_env 对应的环境变量）：{missing}")
        return out

    # 回退：DSH 的凭据 + settings（作者本机用法）
    if not (CRED_FILE.exists() and SETTINGS_FILE.exists()):
        if not strict:
            return {}
        raise FileNotFoundError(
            "没有找到任何网关配置。三选一：\n"
            f"  1) 在项目目录放一个 {USER_CONFIG_NAMES[0]}（可先跑 `python -m aipk init` 生成模板）；\n"
            f"  2) 或者准备 DSH 的 {SETTINGS_FILE} 与 {CRED_FILE}；\n"
            "  3) 只想看效果：`python -m aipk demo`（离线、不需要 key）。")
    creds = dict((_load_yaml(CRED_FILE).get("refs")) or {})
    provs = (_load_yaml(SETTINGS_FILE).get("llm-pi-ai") or {}).get("providers") or {}
    out = {}
    for pid, c in provs.items():
        env_name = c.get("apiKeyEnv")
        key = creds.get(env_name) or os.environ.get(env_name or "", "")
        out[pid] = {
            "base_url": c.get("baseURL", "").rstrip("/"),
            "api_key": key,
            "display_name": c.get("displayName") or pid,
        }
    return out


def load_credential(name: str) -> str:
    """按名字取一条凭据（优先环境变量；用于把密钥注入外部 CLI 的子进程，不落盘）。"""
    val = os.environ.get(name, "")
    if not val:
        creds = dict((_load_yaml(CRED_FILE).get("refs")) or {}) if CRED_FILE.exists() else {}
        val = creds.get(name, "")
    if not val:
        raise KeyError(f"没有这个凭据：{name}（可设同名环境变量，或用 DSH 的 .credentials.yaml）")
    return str(val)


def _resolve_roster() -> list[tuple[str, str, str, str]]:
    """名单优先取用户配置；没有就用作者默认名单。"""
    rows = load_user_config().get("roster") or []
    out: list[tuple[str, str, str, str]] = []
    for row in rows:
        if not isinstance(row, (list, tuple)) or len(row) < 3:
            continue
        out.append((str(row[0]), str(row[1]), str(row[2]),
                    str(row[3]) if len(row) > 3 else "flagship"))
    return out or list(DEFAULT_ROSTER)


# 名单是模块级常量（很多模块直接 import 它），在导入时解析一次
ROSTER: list[tuple[str, str, str, str]] = _resolve_roster()


@dataclass
class ModelSpec:
    provider_id: str
    model_id: str
    name: str
    tier: str = "flagship"

    @property
    def key(self) -> str:
        """榜单里的唯一 id：provider 前缀避免同名模型混淆。"""
        return f"{self.provider_id}/{self.model_id}"

    @property
    def short(self) -> str:
        return self.name


@dataclass
class RunConfig:
    seed: int = 2026
    reps: int = 3                 # 每格重复次数
    tasks_per_family: int = 3     # 每族实例数；1 个实例会让 pass^k 退化成成功率，失去意义
    max_turns: int = 12           # 冻结协议的轮数上限
    temperature: float = 0.0      # 默认贪心，降低抽卡噪声
    max_tokens: int = 8192        # 够思考型模型出正文（实测 <256 会被 reasoning 吃光）
    timeout_s: float = 240.0
    qps_per_gateway: float = 1.2  # 每网关限速；实测不限速会被 429 打成假 0 分
    max_workers: int = 6          # 并发上限（配合限速，别把网关打爆）
    infra_retries: int = 2        # 基础设施故障重投次数（不计入模型能力）
    judge_models: list[str] = field(default_factory=lambda: ["jiyuanapi/glm-5.3"])
    judge_strict: bool = False     # True = 位置翻转的裁决直接丢弃（不记平局）
    families: list[str] | None = None   # None = 全部
    roster: list[ModelSpec] = field(default_factory=list)
    out_dir: Path = field(default_factory=lambda: Path(__file__).resolve().parent.parent / "runs")

    def models(self) -> list[ModelSpec]:
        return self.roster or [ModelSpec(*r) for r in ROSTER]


def default_config() -> RunConfig:
    return RunConfig()
