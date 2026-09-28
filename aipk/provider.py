"""OpenAI 兼容网关适配层。

三个真实坑（都在本机实测过）：
1. 思考型模型把正文放 content，思考放 reasoning_content —— 但 max_tokens 太小会被
   reasoning 吃光，content 返回 None。所以 max_tokens 必须给足，且要单独统计 reasoning 用量。
2. 工具调用格式各家不一：有的走原生 tool_calls，有的把 JSON 写在正文里。
   统一归一到 ToolCall，才能公平比较（这是 harness 适配性的核心）。
3. 部分网关不支持某些参数（tool_choice / response_format），报 400 时要能降级重试。
"""
from __future__ import annotations

import json
import re
import threading
import time
from dataclasses import dataclass, field
from typing import Any

import httpx

from .config import ModelSpec, load_gateways


class RateLimiter:
    """按网关限速。实测这些网关有很硬的 QPS 限制（如 20 请求/3 秒、1302 账户限流），
    不限速的话模型会被 429 打成 0 分 —— 那是把基础设施故障记成模型失败，必须避免。
    """

    def __init__(self, qps: float = 1.2):
        self.min_interval = 1.0 / max(qps, 0.05)
        self._lock = threading.Lock()
        self._next_ok = 0.0

    def wait(self) -> None:
        with self._lock:
            now = time.monotonic()
            if now < self._next_ok:
                time.sleep(self._next_ok - now)
                now = time.monotonic()
            self._next_ok = now + self.min_interval


_LIMITERS: dict[str, RateLimiter] = {}
_LIMITERS_LOCK = threading.Lock()


def limiter_for(provider_id: str, qps: float) -> RateLimiter:
    with _LIMITERS_LOCK:
        if provider_id not in _LIMITERS:
            _LIMITERS[provider_id] = RateLimiter(qps)
        return _LIMITERS[provider_id]


_RETRY_AFTER_RE = re.compile(r'"retryAfterSeconds"\s*:\s*([0-9.]+)')
_RETRY_AFTER_RE2 = re.compile(r"retry.?after[\"'\s:=]+([0-9.]+)", re.I)


def _retry_after_seconds(text: str, default: float) -> float:
    for rx in (_RETRY_AFTER_RE, _RETRY_AFTER_RE2):
        m = rx.search(text or "")
        if m:
            try:
                return max(1.0, float(m.group(1)))
            except Exception:  # noqa: BLE001
                pass
    return default


@dataclass
class ToolCall:
    id: str
    name: str
    args: dict[str, Any]
    raw_args: str = ""
    source: str = "native"      # native | text —— 用于统计"谁不会用工具"


@dataclass
class Usage:
    prompt_tokens: int = 0
    completion_tokens: int = 0
    reasoning_tokens: int = 0
    cached_tokens: int = 0
    reported: bool = True       # 网关是否真的报了 usage（有的流式网关不报）

    @property
    def total_tokens(self) -> int:
        return self.prompt_tokens + self.completion_tokens

    @property
    def reasoning_ratio(self) -> float:
        if not self.completion_tokens:
            return 0.0
        return self.reasoning_tokens / self.completion_tokens


@dataclass
class ChatResult:
    text: str = ""
    reasoning: str = ""
    tool_calls: list[ToolCall] = field(default_factory=list)
    usage: Usage = field(default_factory=Usage)
    ttft_ms: int | None = None          # 首字延迟（流式才有）
    total_ms: int = 0
    finish_reason: str = ""
    error: str | None = None
    attempts: int = 1
    http_status: int | None = None
    infra_failure: bool = False         # True = 限流/网络/超时/参数不被网关接受，**不是模型的锅**，不进评分
    param_degraded: bool = False        # True = 该模型不吃我们给的某个参数（例如 temperature），已自动降级
    raw_message: dict[str, Any] = field(default_factory=dict)


# 正文里内联工具调用的兜底解析（模型不会用原生 function calling 时）
_FENCE_RE = re.compile(r"```(?:json)?\s*(\{.*?\})\s*```", re.S)
_BARE_RE = re.compile(r'(\{[^{}]*"(?:tool|name|function)"\s*:[^{}]*\})', re.S)


def _parse_inline_tool_calls(text: str) -> list[ToolCall]:
    """从正文里挖出形如 {"tool": "...", "args": {...}} 的调用。"""
    found: list[ToolCall] = []
    for m in list(_FENCE_RE.finditer(text)) + list(_BARE_RE.finditer(text)):
        blob = m.group(1)
        try:
            obj = json.loads(blob)
        except Exception:  # noqa: BLE001
            continue
        if not isinstance(obj, dict):
            continue
        name = obj.get("tool") or obj.get("name") or obj.get("function")
        args = obj.get("args") or obj.get("arguments") or obj.get("parameters") or {}
        if isinstance(args, str):
            try:
                args = json.loads(args)
            except Exception:  # noqa: BLE001
                args = {"_raw": args}
        if isinstance(name, str) and isinstance(args, dict):
            found.append(
                ToolCall(id=f"inline-{len(found)}", name=name, args=args,
                         raw_args=blob[:400], source="text")
            )
    return found


# 已知能"降级重投"解决的不支持参数（网关明确说某个值不吃）
_DEGRADABLE_PARAMS = ("temperature", "top_p", "tool_choice", "stream")
# 网关表达"这个参数/值我不接受"的常见措辞
_BAD_PARAM_MARKERS = ("not supported", "unsupported", "invalid_parameter", "invalid value",
                      "invalid parameter", "unrecognized", "unknown field",
                      "不支持", "无效", "非法", "未知")


def classify_http_error(code: int, text: str) -> tuple[bool, str | None]:
    """把 HTTP 错误分成「基础设施/配置问题」与「可降级的参数问题」。

    返回 (是否算基础设施故障, 需要降级重投的参数名或 None)。

    为什么必须这么分：实测给 kimi-k3 传 `temperature=0.7` 会被网关直接 400
    （`invalid_parameter_error: Parameter 'temperature'=0.7 is not supported`）。
    这类 400 是**请求参数不被接受**，不是模型答不出来 ——
    早期版本把它算成模型失败，直接让 kimi-k3 在那一轮 pass^k 掉到 0%。
    4xx 里凡是"请求本身的问题"都不该进模型评分（与本项目对 429 的处理同一条原则）。
    """
    low = (text or "").lower()
    bad_marker = any(m in low for m in _BAD_PARAM_MARKERS)
    for p in _DEGRADABLE_PARAMS:
        if bad_marker and p in low:
            return True, p
    if code == 400:
        return True, None          # 请求本身有问题 → 工装问题，不是模型的锅
    return code in (408, 409, 425, 429, 500, 502, 503, 504), None


def _num(d: dict, *keys: str) -> int:
    for k in keys:
        v = d.get(k)
        if isinstance(v, (int, float)):
            return int(v)
    return 0


class Provider:
    """一个 (网关, 模型) 的客户端。线程内复用 httpx.Client。"""

    def __init__(self, spec: ModelSpec, gateways: dict[str, dict] | None = None, timeout_s: float = 240.0,
                 qps: float = 1.2):
        gws = gateways or load_gateways()
        if spec.provider_id not in gws:
            raise KeyError(f"未知网关 {spec.provider_id}；可用：{list(gws)}")
        gw = gws[spec.provider_id]
        if not gw.get("api_key"):
            raise KeyError(f"网关 {spec.provider_id} 缺少 api key")
        self.spec = spec
        self.base_url = gw["base_url"]
        self.api_key = gw["api_key"]
        self.display_name = gw["display_name"]
        self._client = httpx.Client(timeout=timeout_s)
        self._limiter = limiter_for(spec.provider_id, qps)
        self._no_tool_choice = False
        self._no_stream = False
        self._no_temperature = False
        self.param_degraded = False      # 本模型是否吃过"参数降级"（报告里要标出来）

    # ---------- 主调用 ----------

    def chat(
        self,
        messages: list[dict],
        tools: list[dict] | None = None,
        *,
        temperature: float = 0.0,
        max_tokens: int = 8192,
        stream: bool = True,
        max_retries: int = 3,
    ) -> ChatResult:
        body: dict[str, Any] = {
            "model": self.spec.model_id,
            "messages": messages,
            "max_tokens": max_tokens,
        }
        if not self._no_temperature:
            body["temperature"] = temperature
        if tools:
            body["tools"] = tools
            if not self._no_tool_choice:
                body["tool_choice"] = "auto"

        last: ChatResult | None = None
        # 用于 usage 缺失时的估算
        prompt_chars = sum(len(str(m.get("content") or "")) for m in messages)
        for attempt in range(1, max_retries + 1):
            use_stream = stream and not self._no_stream
            self._limiter.wait()
            t0 = time.perf_counter()
            try:
                if use_stream:
                    res = self._stream_call(body, t0, prompt_chars)
                else:
                    res = self._plain_call(body, t0, prompt_chars)
            except httpx.HTTPStatusError as e:
                code = e.response.status_code
                txt = e.response.text or ""
                infra, degrade = classify_http_error(code, txt)
                last = ChatResult(error=f"HTTP {code}: {txt[:300]}",
                                  http_status=code, attempts=attempt,
                                  infra_failure=infra,
                                  total_ms=int((time.perf_counter() - t0) * 1000))
                # 参数不支持 → 降级后立刻重试
                if code == 400 and "tool_choice" in txt and not self._no_tool_choice:
                    self._no_tool_choice = True
                    continue
                if code == 400 and "stream" in txt.lower() and not self._no_stream:
                    self._no_stream = True
                    continue
                if degrade == "temperature" and not self._no_temperature:
                    # 实测：kimi-k3（阿里云 MaaS）只接受特定 temperature，
                    # 给 0.7 直接 400 invalid_parameter_error。
                    # 这种"参数不被接受"不是模型能力问题，降级重投并把标记带进报告。
                    self._no_temperature = True
                    self.param_degraded = True
                    continue
                if code in (408, 409, 425, 429, 500, 502, 503, 504):
                    # 尊重网关给的 retryAfterSeconds（实测有些网关会明确返回）
                    wait = _retry_after_seconds(txt, min(2 ** attempt, 30)) + 0.5
                    time.sleep(wait)
                    continue
                return last
            except Exception as e:  # noqa: BLE001  网络/超时
                last = ChatResult(error=f"{type(e).__name__}: {e}"[:300], attempts=attempt,
                                  infra_failure=True,
                                  total_ms=int((time.perf_counter() - t0) * 1000))
                if attempt < max_retries:
                    time.sleep(min(2 ** attempt, 20))
                    continue
                return last

            res.attempts = attempt
            if res.error and attempt < max_retries:
                res.infra_failure = True
                time.sleep(min(2 ** attempt, 20))
                last = res
                continue
            # 把"本模型吃过参数降级"带回去 —— 报告要据此标注，
            # 否则读者会以为它和别人跑在同一采样参数下（实测漏过一次）。
            res.param_degraded = self.param_degraded
            return res
        return last or ChatResult(error="unknown failure", infra_failure=True)

    # ---------- 两种传输 ----------

    def _plain_call(self, body: dict, t0: float, prompt_chars: int = 0) -> ChatResult:
        r = self._client.post(f"{self.base_url}/chat/completions",
                              headers={"Authorization": f"Bearer {self.api_key}"}, json=body)
        if r.status_code != 200:
            raise httpx.HTTPStatusError("bad status", request=r.request, response=r)
        data = r.json()
        msg = ((data.get("choices") or [{}])[0].get("message")) or {}
        finish = ((data.get("choices") or [{}])[0].get("finish_reason")) or ""
        return self._build(msg, finish, data.get("usage") or {},
                           int((time.perf_counter() - t0) * 1000), None, prompt_chars)

    def _stream_call(self, body: dict, t0: float, prompt_chars: int = 0) -> ChatResult:
        body = dict(body, stream=True)
        text_parts: list[str] = []
        reason_parts: list[str] = []
        tc_acc: dict[int, dict] = {}
        usage: dict = {}
        finish = ""
        ttft: int | None = None
        with self._client.stream("POST", f"{self.base_url}/chat/completions",
                                 headers={"Authorization": f"Bearer {self.api_key}"},
                                 json=body) as resp:
            if resp.status_code != 200:
                resp.read()
                raise httpx.HTTPStatusError("bad status", request=resp.request, response=resp)
            for line in resp.iter_lines():
                if not line or not line.startswith("data:"):
                    continue
                payload = line[5:].strip()
                if payload == "[DONE]":
                    break
                try:
                    chunk = json.loads(payload)
                except Exception:  # noqa: BLE001
                    continue
                if chunk.get("usage"):
                    usage = chunk["usage"]
                for ch in chunk.get("choices") or []:
                    if ch.get("finish_reason"):
                        finish = ch["finish_reason"]
                    delta = ch.get("delta") or {}
                    c = delta.get("content")
                    if c:
                        if ttft is None:
                            ttft = int((time.perf_counter() - t0) * 1000)
                        text_parts.append(c)
                    rc = delta.get("reasoning_content") or delta.get("reasoning")
                    if rc:
                        if ttft is None:
                            ttft = int((time.perf_counter() - t0) * 1000)
                        reason_parts.append(rc)
                    for tc in delta.get("tool_calls") or []:
                        idx = tc.get("index", 0)
                        slot = tc_acc.setdefault(idx, {"id": "", "name": "", "args": ""})
                        if tc.get("id"):
                            slot["id"] = tc["id"]
                        fn = tc.get("function") or {}
                        if fn.get("name"):
                            slot["name"] = fn["name"]
                        if fn.get("arguments"):
                            slot["args"] += fn["arguments"]
        msg: dict[str, Any] = {"content": "".join(text_parts), "reasoning_content": "".join(reason_parts)}
        if tc_acc:
            msg["tool_calls"] = [
                {"id": v["id"] or f"call_{i}", "type": "function",
                 "function": {"name": v["name"], "arguments": v["args"]}}
                for i, v in sorted(tc_acc.items())
            ]
        return self._build(msg, finish, usage, int((time.perf_counter() - t0) * 1000), ttft, prompt_chars)

    # ---------- 归一化 ----------

    @staticmethod
    def _build(msg: dict, finish: str, usage: dict, total_ms: int, ttft: int | None,
               prompt_chars: int = 0) -> ChatResult:
        text = msg.get("content") or ""
        if isinstance(text, list):  # 少数网关返回 content 数组
            text = "".join(p.get("text", "") for p in text if isinstance(p, dict))
        reasoning = msg.get("reasoning_content") or msg.get("reasoning") or ""

        calls: list[ToolCall] = []
        for tc in msg.get("tool_calls") or []:
            fn = tc.get("function") or {}
            raw = fn.get("arguments") or "{}"
            try:
                args = json.loads(raw) if isinstance(raw, str) else (raw or {})
            except Exception:  # noqa: BLE001
                args = {"_raw": raw}
            calls.append(ToolCall(id=tc.get("id") or f"call_{len(calls)}",
                                  name=fn.get("name") or "", args=args if isinstance(args, dict) else {"_raw": args},
                                  raw_args=raw if isinstance(raw, str) else json.dumps(raw, ensure_ascii=False),
                                  source="native"))
        if not calls and text:
            calls = _parse_inline_tool_calls(text)

        pt = usage.get("prompt_tokens_details") or {}
        ct = usage.get("completion_tokens_details") or {}
        reported = bool(usage) and (_num(usage, "prompt_tokens", "input_tokens") > 0
                                    or _num(usage, "completion_tokens", "output_tokens") > 0)
        u = Usage(
            prompt_tokens=_num(usage, "prompt_tokens", "input_tokens"),
            completion_tokens=_num(usage, "completion_tokens", "output_tokens"),
            reasoning_tokens=_num(ct, "reasoning_tokens") or _num(usage, "reasoning_tokens"),
            cached_tokens=_num(pt, "cached_tokens"),
            reported=reported,
        )
        if not reported:
            # 有的网关流式响应不报 usage。此时**不能当 0** ——
            # 那会让"token 经济性"指标把该模型算成零成本，排名就歪了。
            # 用字符数估算并标记为估算值，报告里会区分。
            u.completion_tokens = max(1, int((len(text) + len(reasoning)) / 1.7))
            u.reasoning_tokens = int(len(reasoning) / 1.7)
            u.prompt_tokens = max(1, int(prompt_chars / 1.7))
            u.reported = False
        err = None
        if not text.strip() and not calls and finish != "stop":
            err = f"empty output (finish_reason={finish or 'unknown'})"
        return ChatResult(text=text, reasoning=reasoning, tool_calls=calls, usage=u,
                          ttft_ms=ttft, total_ms=total_ms, finish_reason=finish, error=err,
                          raw_message=msg)

    def close(self) -> None:
        self._client.close()
