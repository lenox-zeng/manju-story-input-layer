"""OpenAI 兼容（DeepSeek / 豆包·火山方舟 / 任意端点）客户端：JSON 强约束 + 自修复重试。

为什么需要「自修复」这一层
--------------------------
v1 方案只写了 `response_format={"type":"json_object"}` 就算完事。实测中 27B 模型
在长结构（19 个镜头）下仍会出现：字段名拼错、数组长度为 0、枚举值写成中文、
把整段包在 ```json 围栏里。任何一次都会让下游员工直接崩掉、整条流水线重跑。

这里把「解析 → 校验 → 带着报错重问」做成一次可重试的闭环，
并把失败原因记进 trace，这样在 Phoenix 里能直接看到是哪个员工、哪一版提示词在拖后腿。
"""

from __future__ import annotations

import json
import os
import re
import threading
import time
from dataclasses import asdict, dataclass, field, is_dataclass
from pathlib import Path
from typing import Any, TypeVar

from pydantic import BaseModel

from .config import api_key_value, get_config
from .schemas import describe, parse_json, schema_error_text, validate
from .tracing import ospan, set_output
from .tracing import OpenInferenceKind as K

T = TypeVar("T", bound=BaseModel)


def _usage_int(usage: Any, *names: str) -> int:
    """从不同厂商的 usage 对象里取整数指标（DeepSeek/OpenAI 字段名不完全一致）。"""
    if usage is None:
        return 0
    for name in names:
        value = getattr(usage, name, None)
        if value is not None:
            try:
                return int(value)
            except (TypeError, ValueError):
                return 0
    details = getattr(usage, "prompt_tokens_details", None)
    if details is not None:
        for name in names:
            value = getattr(details, name, None)
            if value is not None:
                try:
                    return int(value)
                except (TypeError, ValueError):
                    return 0
    return 0


def _json_default(obj: Any) -> Any:
    """payload 兜底序列化：漏转的 Pydantic 模型/阶段切片不应整条流水线崩掉。"""
    if isinstance(obj, BaseModel):
        return obj.model_dump(mode="json")
    if is_dataclass(obj) and not isinstance(obj, type):
        return asdict(obj)
    if isinstance(obj, (set, tuple)):
        return list(obj)
    if isinstance(obj, Path):
        return str(obj)
    raise TypeError(f"Object of type {type(obj).__name__} is not JSON serializable")


def _drop_nulls(node: Any) -> Any:
    """递归剔除模型输出里的 null 值键/元素，让 pydantic 回落各字段默认值。

    DeepSeek/豆包等模型常把「没有状态变化/空集合」写成 null 而非空 dict/list，
    而 pydantic 对 `dict[str,str]`/`list[str]` 遇到 null 会直接判类型非法
    （Input should be a valid dictionary / valid list），导致整轮重试白烧 token。
    统一在解析后、校验前把 null 剔除：缺失字段由 default_factory 提供空默认。
    """
    if isinstance(node, dict):
        return {k: _drop_nulls(v) for k, v in node.items() if v is not None}
    if isinstance(node, list):
        return [_drop_nulls(v) for v in node if v is not None]
    return node


# 模型偶尔把「亨利叔叔」这类中文人名直接写进只接受 Cid 的字段
# （CutSubject.char = ^C\d+$），pydantic 会在_进入质量门之前_就抛
# ValidationError，导致整场 EDL 白白重跑。这里在解析后、校验前做一次
# **只作用于角色引用字段**的别名归一化：名字 → payload 里的 Cid。
_CHAR_MAP_KEYS = {"where", "facing", "posture", "wardrobe", "injuries",
                  "screen_side"}
_CHAR_LIST_KEYS = {"characters", "character_ids"}
_CHAR_SCALAR_KEYS = {"char", "speaker"}


def _collect_character_aliases(payload: Any) -> dict[str, str]:
    """从提示词 payload 里收集 人名 → Cid（兼容 cast / staging 的嵌套结构）。"""
    aliases: dict[str, str] = {}

    def walk(node: Any) -> None:
        if isinstance(node, BaseModel):
            walk(node.model_dump(mode="json"))
        elif isinstance(node, dict):
            cid, name = node.get("id"), node.get("name")
            if (isinstance(cid, str) and re.fullmatch(r"C\d+", cid)
                    and isinstance(name, str) and name.strip()):
                aliases.setdefault(name.strip(), cid)
            for value in node.values():
                walk(value)
        elif isinstance(node, list):
            for value in node:
                walk(value)

    walk(payload)
    return aliases


def _as_char_id(value: Any, aliases: dict[str, str]) -> Any:
    if not isinstance(value, str):
        return value
    text = value.strip()
    if text in aliases:
        return aliases[text]
    if re.fullmatch(r"C\d+", text):
        return text
    # "亨利叔叔（C1）"/"C1 亨利叔叔"：从明确引用里抽出唯一 Cid
    found = re.search(r"C\d+", text)
    return found.group(0) if found else value


def _normalize_model_refs(node: Any, aliases: dict[str, str],
                          field: str | None = None) -> Any:
    """递归把角色引用字段里的人名换成 Cid；正文/提示词字段原样保留。"""
    if not aliases:
        return node
    if isinstance(node, dict):
        out: dict[str, Any] = {}
        for key, value in node.items():
            new_key = (_as_char_id(key, aliases)
                       if field in _CHAR_MAP_KEYS and isinstance(key, str)
                       else key)
            if field == "props" and isinstance(value, str):
                value = _as_char_id(value, aliases)   # P1: C1 -> C1
            out[new_key] = _normalize_model_refs(value, aliases, new_key)
        return out
    if isinstance(node, list):
        if field in _CHAR_LIST_KEYS:
            return [_as_char_id(v, aliases) if isinstance(v, str)
                    else _normalize_model_refs(v, aliases, field)
                    for v in node]
        return [_normalize_model_refs(v, aliases, field) for v in node]
    if field in _CHAR_SCALAR_KEYS:
        return _as_char_id(node, aliases)
    return node


def _status_code(exc: BaseException) -> int | None:
    """尽力从 SDK 异常里掏出 HTTP 状态码（不强制依赖 openai 包）。"""
    code = getattr(exc, "status_code", None)
    if isinstance(code, int):
        return code
    resp = getattr(exc, "response", None)
    code = getattr(resp, "status_code", None)
    return code if isinstance(code, int) else None


def _is_transient_error(exc: BaseException) -> bool:
    """网络抖动 / 限流 / 5xx 值得重试；鉴权失败、参数错误重试也没用。"""
    code = _status_code(exc)
    if code is not None:
        return code in (408, 409, 425, 429) or code >= 500
    # 没有状态码时按常见 SDK / httpx 异常类名判断
    transient = {
        "APIConnectionError", "APITimeoutError", "RateLimitError",
        "InternalServerError", "ServiceUnavailableError",
        "ConnectError", "ConnectTimeout", "ReadTimeout", "ReadError",
        "RemoteProtocolError", "WriteError", "PoolTimeout", "TimeoutError",
    }
    return type(exc).__name__ in transient


def _is_local_url(url: str) -> bool:
    """判断是否本机地址（用于绕过系统代理）。"""
    from urllib.parse import urlparse

    host = (urlparse(url).hostname or "").lower()
    return host in {"127.0.0.1", "localhost", "::1"}


@dataclass
class LLMStats:
    calls: int = 0
    repairs: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    cache_hit_tokens: int = 0
    cache_miss_tokens: int = 0
    seconds: float = 0.0
    stream_calls: int = 0
    history: list[dict[str, Any]] = field(default_factory=list)

    def add(self, usage: Any, secs: float, note: str) -> None:
        self.calls += 1
        self.seconds += secs
        if note == "stream":
            self.stream_calls += 1
        if usage is not None:
            self.prompt_tokens += _usage_int(usage, "prompt_tokens")
            self.completion_tokens += _usage_int(usage, "completion_tokens")
            self.cache_hit_tokens += _usage_int(
                usage, "prompt_cache_hit_tokens", "cached_tokens")
            self.cache_miss_tokens += _usage_int(
                usage, "prompt_cache_miss_tokens", "uncached_tokens")
        self.history.append({"note": note, "sec": round(secs, 1)})

    def summary(self) -> str:
        cache = ""
        if self.cache_hit_tokens or self.cache_miss_tokens:
            total = self.cache_hit_tokens + self.cache_miss_tokens
            cache = (f" / cache {self.cache_hit_tokens}/{total} "
                     f"({self.cache_hit_tokens / total:.0%})" if total else "")
        stream = f" / stream {self.stream_calls}" if self.stream_calls else ""
        return (
            f"{self.calls} 次调用 / 修复 {self.repairs} 次 / "
            f"in {self.prompt_tokens} tok / out {self.completion_tokens} tok / "
            f"{self.seconds:.0f}s{cache}{stream}"
        )


def _sanitize_no_proxy_env() -> None:
    """清理 NO_PROXY 里 httpx 无法解析的方括号 IPv6 条目。

    某些 Windows/代理环境会把 `[::1]` 写进 NO_PROXY；httpx 解析时会抛
    `Invalid port: ':1]'`，导致连远程 API 的健康检查都直接失败。去掉这些
    条目、保留其余域名，既不牺牲代理，也不影响本机地址（本机请求另走
    trust_env=False）。
    """
    import os

    for name in ("NO_PROXY", "no_proxy"):
        raw = os.environ.get(name)
        if not raw or "[" not in raw:
            continue
        items = [x for x in raw.split(",")
                 if x.strip() and not x.strip().startswith("[")]
        os.environ[name] = ",".join(items)


class _RetryTransport(Exception):
    """请求参数不被当前端点接受时，降级到下一档传输方式（不是网络错误）。"""

    def __init__(self, kind: str):
        self.kind = kind
        super().__init__(kind)


class LLMClient:
    """薄封装。真正重要的是 `structured()` 里的重试闭环。"""

    def __init__(self, base_url: str | None = None, stats: LLMStats | None = None):
        cfg = get_config()
        self.base_url = base_url or (cfg.get("llm.base_url")
                                     or "https://api.deepseek.com/v1")
        self.cfg = cfg
        self.stats = stats or LLMStats()
        self._client = None
        # 最近一次调用的 usage / finish_reason（结构化重试时读它记 trace）
        self.last_usage = None
        self.last_model: str = ""
        self.last_finish_reason: str | None = None
        self.last_thinking = "auto"
        self.last_stream = False
        self.last_first_token_secs: float | None = None
        self.last_cache_hit_tokens = 0
        self.last_cache_miss_tokens = 0
        # 并行分镜提示词展开时，last_* 是共享状态会串台；每次调用额外写线程本地副本
        self._local = threading.local()
        self._stats_lock = threading.Lock()
        # 端点能力探测：被拒一次后，本轮进程不再重复发同类参数
        self._stream_unsupported = False
        self._stream_options_unsupported = False
        self._thinking_unsupported_modes: set[str] = set()

    def _api_key(self) -> str:
        """密钥优先级：环境变量 > config.json 的 llm.api_key。"""
        return api_key_value(self.cfg)

    # -- 惰性构造，避免没装 openai 时 import 就炸 --------------------------
    @property
    def client(self):
        if self._client is None:
            from openai import OpenAI

            _sanitize_no_proxy_env()
            kwargs: dict[str, Any] = {}
            if _is_local_url(self.base_url):
                # 本机地址不走系统代理。某些环境把 `[::1]` 写进
                # NO_PROXY（httpx 解析会抛 InvalidURL），或把 127.0.0.1 交给
                # 转发器（502）。ComfyUI 客户端已用同样策略。
                import httpx

                kwargs["http_client"] = httpx.Client(trust_env=False)
            api_key = self._api_key()
            self._client = OpenAI(
                base_url=self.base_url,
                # 本地兼容服务可留空；远程（DeepSeek / 豆包）必须给 key
                api_key=api_key or "not-needed",
                timeout=float(os.environ.get("MANJU_LLM_TIMEOUT_SEC")
                              or self.cfg.get("llm.request_timeout_sec", 2400)),
                max_retries=1,
                **kwargs,
            )
        return self._client

    # -- 健康检查 ---------------------------------------------------------
    def health(self, timeout: float = 10.0, attempts: int = 2) -> dict[str, Any]:
        """连通性检查：调 OpenAI 兼容的 `/v1/models`。

        DeepSeek / 豆包(火山方舟) / 本地兼容端点都支持这个端点，
        不再依赖 llama-server 特有的 `/health`。

        健康检查只是“提前探路”，不能让 5 秒抖动阻断整次生成：
        - 429/5xx 与网络错误重试一次；
        - 返回真实 status / error，调用方据此区分 Key/余额问题与临时网络问题。
        """
        import httpx

        _sanitize_no_proxy_env()
        base = self.base_url.rstrip("/")
        if not base.endswith("/v1"):
            base += "/v1"
        api_key = api_key_value(self.cfg)
        headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
        last: dict[str, Any] = {"ok": False, "status": None,
                                "error": "health check not attempted"}
        for attempt in range(1, max(1, attempts) + 1):
            try:
                r = httpx.get(f"{base}/models", timeout=timeout,
                              headers=headers,
                              trust_env=not _is_local_url(base))
                last = {"ok": r.status_code == 200,
                        "status": r.status_code,
                        "body": _safe_json(r),
                        "attempt": attempt}
                transient = (r.status_code == 429
                             or 500 <= r.status_code < 600)
                if not transient:
                    return last
            except Exception as e:  # noqa: BLE001 - 健康检查不该抛
                last = {"ok": False, "status": None,
                        "error": f"{type(e).__name__}: {e}",
                        "attempt": attempt}
            if attempt < attempts:
                time.sleep(0.5 * attempt)
        return last

    def list_models(self) -> list[str]:
        try:
            return [m.id for m in self.client.models.list().data]
        except Exception:  # noqa: BLE001
            return []

    # -- 思考模式 / 流式传输 ----------------------------------------------
    def _resolve_thinking(self, stage: str | None,
                          override: str | None) -> str:
        mode = override
        if mode is None:
            mode = self.cfg.get("llm.thinking", "auto")
            by_stage = self.cfg.get("llm.thinking_by_stage") or {}
            if stage and isinstance(by_stage, dict):
                # 前缀匹配：stage 常带“第 N 集第 M 场”后缀
                for key in sorted(by_stage, key=lambda k: len(str(k)), reverse=True):
                    if stage == str(key) or stage.startswith(str(key)):
                        mode = by_stage[key]
                        break
        return str(mode or "auto").strip().lower()

    def _thinking_kwargs(self, mode: str) -> tuple[dict[str, Any], str]:
        if mode in self._thinking_unsupported_modes:
            return {}, "auto"
        if mode in {"off", "none", "disabled", "disable", "false", "0", "no"}:
            return ({"extra_body": {"thinking": {"type": "disabled"}}},
                    "disabled")
        if mode in {"low", "medium", "high", "max", "minimal"}:
            effort = {"max": "high", "minimal": "low"}.get(mode, mode)
            return {"reasoning_effort": effort}, mode
        return {}, "auto"

    def _stream_enabled(self) -> bool:
        if self._stream_unsupported:
            return False
        value = self.cfg.get("llm.stream", True)
        if isinstance(value, str):
            return value.strip().lower() not in {
                "false", "0", "off", "no", "disabled", "none"}
        return bool(value)

    def _stream_variants(self, *, allow_stream: bool, allow_thinking: bool,
                         allow_options: bool) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        if allow_stream:
            if allow_thinking:
                out.append({"key": "stream+thinking+options", "stream": True,
                            "thinking": True, "options": True})
                out.append({"key": "stream+thinking", "stream": True,
                            "thinking": True, "options": False})
            out.append({"key": "stream" + ("+options" if allow_options else ""),
                        "stream": True, "thinking": False,
                        "options": allow_options})
            if not allow_thinking or not allow_options:
                # 已经因能力降级过，补一个最保守的纯流式档
                out.append({"key": "stream-min", "stream": True,
                            "thinking": False, "options": False})
        if allow_thinking:
            out.append({"key": "plain+thinking", "stream": False,
                        "thinking": True, "options": False})
        out.append({"key": "plain", "stream": False, "thinking": False,
                    "options": False})
        return out

    def _thread_last(self, name: str, default=None):
        return getattr(self._local, name, default)

    def _current_model(self) -> str:
        """本次请求实际使用的模型；测试替换 chat() 时退回配置，trace 仍可归因。"""
        return str(self._thread_last("model")
                   or self.last_model
                   or os.environ.get("MANJU_LLM_MODEL")
                   or self.cfg.get("llm.model")
                   or "deepseek-flash")

    def _record_result(self, *, usage, finish_reason: str | None,
                       thinking: str, streamed: bool,
                       first_token: float | None) -> None:
        self.last_usage = usage
        self.last_finish_reason = finish_reason
        self.last_thinking = thinking
        self.last_stream = streamed
        self.last_first_token_secs = first_token
        self.last_cache_hit_tokens = _usage_int(
            usage, "prompt_cache_hit_tokens", "cached_tokens")
        self.last_cache_miss_tokens = _usage_int(
            usage, "prompt_cache_miss_tokens", "uncached_tokens")
        # 线程本地副本：并行展开时每个线程读自己的，不读共享 last_*
        self._local.usage = usage
        self._local.finish_reason = finish_reason
        self._local.thinking = thinking
        self._local.stream = streamed
        self._local.first_token = first_token
        self._local.cache_hit = self.last_cache_hit_tokens
        self._local.cache_miss = self.last_cache_miss_tokens

    @staticmethod
    def _strip_thinking(kwargs: dict[str, Any]) -> None:
        kwargs.pop("extra_body", None)
        kwargs.pop("reasoning_effort", None)

    @staticmethod
    def _is_param_rejection(exc: BaseException, *terms: str) -> bool:
        if _status_code(exc) not in (400, 404, 405, 422):
            return False
        msg = f"{type(exc).__name__}: {exc}".lower()
        return any(t in msg for t in terms)

    def _request_once(self, kwargs: dict[str, Any], *, stream: bool,
                      stream_options: bool,
                      span=None) -> tuple[str, Any, str | None, float | None]:
        """发一次请求。参数不被支持时抛 `_RetryTransport`，由 chat() 降级重试。"""
        import httpx

        kw = dict(kwargs)
        if stream:
            kw["stream"] = True
            if stream_options:
                kw["stream_options"] = {"include_usage": True}
            idle = float(self.cfg.get("llm.stream_idle_timeout_sec", 180) or 180)
            # read=空闲超时：只要 180s 内没有任何 chunk 就断开，避免 40 分钟挂死。
            kw["timeout"] = httpx.Timeout(connect=30.0, read=max(10.0, idle),
                                          write=60.0, pool=30.0)
        total_timeout = float(self.cfg.get("llm.request_timeout_sec", 2400) or 2400)
        t0 = time.time()
        first_token: float | None = None
        try:
            resp = self.client.chat.completions.create(**kw)
        except Exception as e:  # noqa: BLE001 - 统一按“端点能力/网络”分流
            self._raise_transport_fallback(e, stream=stream,
                                           stream_options=stream_options,
                                           has_thinking=("extra_body" in kw
                                                         or "reasoning_effort" in kw))
            raise

        usage = None
        finish_reason: str | None = None
        text_parts: list[str] = []
        reasoning_parts: list[str] = []
        got_any = False
        streamed = False

        # 有些兼容端点忽略 stream=True 直接回完整对象
        if stream and not hasattr(resp, "choices"):
            streamed = True
            try:
                for chunk in resp:
                    if time.time() - t0 > total_timeout:
                        raise TimeoutError(
                            f"流式调用超过总超时 {total_timeout:g}s")
                    u = getattr(chunk, "usage", None)
                    if u is not None:
                        usage = u
                    choices = getattr(chunk, "choices", None) or []
                    if not choices:
                        continue
                    choice = choices[0]
                    delta = getattr(choice, "delta", None)
                    content = getattr(delta, "content", None) if delta else None
                    reasoning = (getattr(delta, "reasoning_content", None)
                                 if delta else None)
                    if content or reasoning:
                        got_any = True
                        if first_token is None:
                            first_token = time.time() - t0
                        if content:
                            text_parts.append(content)
                        if reasoning:
                            reasoning_parts.append(reasoning)
                    fr = getattr(choice, "finish_reason", None)
                    if fr:
                        finish_reason = fr
            except Exception as e:  # noqa: BLE001
                self._raise_transport_fallback(e, stream=True,
                                               stream_options=stream_options,
                                               has_thinking=("extra_body" in kw
                                                             or "reasoning_effort" in kw),
                                               got_any=got_any)
                raise
            finally:
                close = getattr(resp, "close", None)
                if callable(close):
                    try:
                        close()
                    except Exception:  # noqa: BLE001
                        pass
        else:
            try:
                if not stream:
                    usage = getattr(resp, "usage", None)
                    choice = resp.choices[0]
                    finish_reason = getattr(choice, "finish_reason", None)
                    msg = choice.message
                    text_parts.append(getattr(msg, "content", None) or "")
                    reasoning_parts.append(
                        getattr(msg, "reasoning_content", None) or "")
                    got_any = bool("".join(text_parts).strip()
                                   or "".join(reasoning_parts).strip())
                else:
                    # 端点在 stream=True 下回了完整对象：按非流处理
                    usage = getattr(resp, "usage", None)
                    choice = resp.choices[0]
                    finish_reason = getattr(choice, "finish_reason", None)
                    msg = choice.message
                    text_parts.append(getattr(msg, "content", None) or "")
                    reasoning_parts.append(
                        getattr(msg, "reasoning_content", None) or "")
                    got_any = True
            except Exception as e:  # noqa: BLE001
                self._raise_transport_fallback(
                    e, stream=stream, stream_options=stream_options,
                    has_thinking=("extra_body" in kw or "reasoning_effort" in kw),
                    got_any=got_any)
                raise

        text = "".join(text_parts).strip()
        if not text:
            # 推理型模型把正文挤空时退到 reasoning_content（保持旧行为）
            text = "".join(reasoning_parts).strip()
            if text and span is not None:
                span.set_attribute("llm.fallback_reasoning", True)
        return text, usage, finish_reason, first_token

    def _raise_transport_fallback(self, exc: BaseException, *, stream: bool,
                                  stream_options: bool, has_thinking: bool,
                                  got_any: bool = False) -> None:
        if got_any:
            raise exc
        if has_thinking and self._is_param_rejection(
                exc, "thinking", "reasoning_effort", "extra_body"):
            raise _RetryTransport("thinking") from exc
        if stream and stream_options and self._is_param_rejection(
                exc, "stream_options", "include_usage"):
            raise _RetryTransport("stream_options") from exc
        if stream and self._is_param_rejection(
                exc, "stream", "sse", "not supported", "unsupported",
                "does not support"):
            raise _RetryTransport("stream") from exc
        raise exc

    # -- 原始对话 ---------------------------------------------------------
    def chat(
        self,
        system: str,
        user: str,
        *,
        json_mode: bool = True,
        temperature: float | None = None,
        max_tokens: int | None = None,
        images: list[str] | None = None,
        span=None,
        stage: str | None = None,
        thinking: str | None = None,
    ) -> str:
        messages: list[dict[str, Any]] = [{"role": "system", "content": system}]
        if images:
            content: list[dict[str, Any]] = [{"type": "text", "text": user}]
            for img in images:
                content.append(
                    {"type": "image_url", "image_url": {"url": _to_data_url(img)}}
                )
            messages.append({"role": "user", "content": content})
        else:
            messages.append({"role": "user", "content": user})

        # 环境变量只用于临时压测/换模型，不写入 config。
        model = str(os.environ.get("MANJU_LLM_MODEL")
                    or self.cfg.get("llm.model") or "deepseek-flash")
        # 请求前先落线程本地副本：并行展开时 trace 各自归属自己的模型。
        self.last_model = model
        self._local.model = model
        kwargs: dict[str, Any] = {
            "model": model,
            "messages": messages,
            "temperature": (
                temperature if temperature is not None else self.cfg.get("llm.temperature", 0.0)
            ),
            "max_tokens": max_tokens or self.cfg.get("llm.max_tokens", 6000),
        }
        if json_mode:
            kwargs["response_format"] = {"type": "json_object"}

        requested_mode = self._resolve_thinking(stage, thinking)
        thinking_kwargs, effective_mode = self._thinking_kwargs(requested_mode)
        base_kwargs = dict(kwargs)
        base_kwargs.update(thinking_kwargs)

        allow_stream = self._stream_enabled()
        allow_thinking = bool(thinking_kwargs)
        allow_options = not self._stream_options_unsupported
        tried: set[str] = set()
        last_transport: _RetryTransport | None = None
        t0 = time.time()
        result: tuple[str, Any, str | None, float | None] | None = None
        used_mode = effective_mode
        used_stream = False

        while True:
            variants = self._stream_variants(
                allow_stream=allow_stream,
                allow_thinking=allow_thinking,
                allow_options=allow_options)
            candidate = next((v for v in variants if v["key"] not in tried), None)
            if candidate is None:
                break
            tried.add(candidate["key"])
            kw = dict(base_kwargs)
            if not candidate["thinking"]:
                self._strip_thinking(kw)
            if not candidate["stream"]:
                kw.pop("stream", None)
                kw.pop("stream_options", None)
                kw.pop("timeout", None)
            try:
                result = self._request_once(
                    kw,
                    stream=candidate["stream"],
                    stream_options=candidate["options"],
                    span=span,
                )
                used_mode = effective_mode if candidate["thinking"] else "auto"
                used_stream = candidate["stream"]
                break
            except _RetryTransport as e:
                last_transport = e
                if e.kind == "thinking":
                    allow_thinking = False
                    self._thinking_unsupported_modes.add(effective_mode)
                    used_mode = "auto"
                elif e.kind == "stream_options":
                    allow_options = False
                    self._stream_options_unsupported = True
                elif e.kind == "stream":
                    allow_stream = False
                    self._stream_unsupported = True
                continue

        if result is None:
            assert last_transport is not None
            raise last_transport

        text, usage, finish_reason, first_token = result
        secs = time.time() - t0
        self._record_result(usage=usage, finish_reason=finish_reason,
                            thinking=used_mode if used_mode else requested_mode,
                            streamed=used_stream, first_token=first_token)
        with self._stats_lock:
            self.stats.add(usage, secs, "stream" if used_stream else "chat")
        if span is not None:
            span.set_attribute("llm.seconds", round(secs, 2))
            span.set_attribute("llm.thinking", self.last_thinking)
            span.set_attribute("llm.stream", used_stream)
            if first_token is not None:
                span.set_attribute("llm.first_token_seconds", round(first_token, 2))
            u = usage
            if u is not None:
                span.set_attribute("llm.token_count.prompt",
                                   _usage_int(u, "prompt_tokens"))
                span.set_attribute("llm.token_count.completion",
                                   _usage_int(u, "completion_tokens"))
                if self.last_cache_hit_tokens or self.last_cache_miss_tokens:
                    span.set_attribute("llm.token_count.cache_hit",
                                       self.last_cache_hit_tokens)
        return text

    def _chat_with_api_retry(
        self,
        stage: str,
        system: str,
        user: str,
        *,
        temperature: float | None,
        max_tokens: int | None,
        images: list[str] | None,
        span=None,
    ) -> str:
        """网络层重试：只对超时/限流/5xx 退避重试，4xx（鉴权/参数）直接抛。

        每次失败都记一行 trace（api_error=True），否则一次断网只会留下
        “某个阶段没产出”的假象，排查时看不到真正原因。
        """
        retries = max(0, int(self.cfg.get("llm.api_retries", 2)))
        backoff = max(0.0, float(self.cfg.get("llm.api_retry_backoff_sec", 2.0)))
        last: Exception | None = None
        for i in range(retries + 1):
            try:
                return self.chat(
                    system, user, temperature=temperature, max_tokens=max_tokens,
                    images=images, span=span, stage=stage,
                )
            except Exception as e:  # noqa: BLE001 - 统一判定是否值得重试
                last = e
                transient = _is_transient_error(e)
                _append_llm_trace({
                    "t": time.strftime("%H:%M:%S"),
                    "stage": stage,
                    "model": self._current_model(),
                    "attempt": -1,
                    "api_retry": i,
                    "ok": False,
                    "api_error": True,
                    "transient": transient,
                    "thinking": self._thread_last("thinking"),
                    "stream": self._thread_last("stream"),
                    "error": f"{type(e).__name__}: {e}"[:300],
                    "system": system[:400],
                    "user": user[:600],
                    "output": "",
                })
                if not transient or i >= retries:
                    raise
                delay = min(backoff * (2 ** i), 30.0)
                if span is not None:
                    span.set_attribute(f"llm.api_retry.{i}", round(delay, 1))
                time.sleep(delay)
        raise last  # pragma: no cover - 循环内必然 return/raise

    # -- 结构化输出（核心） ------------------------------------------------
    def structured(
        self,
        stage: str,
        system: str,
        payload: Any,
        *,
        model_cls: type[T] | None = None,
        temperature: float | None = None,
        max_tokens: int | None = None,
        images: list[str] | None = None,
        extra_user: str = "",
    ) -> T:
        """调用 → 解析 → 校验，失败则把报错回灌重问（最多 json_repairs 次）。"""
        from .schemas import HANDOFFS

        cls = model_cls or HANDOFFS[stage]

        # 离线假模型：不碰显卡就能把整条流水线跑通（见 fake_llm.py）
        # 纯输入层包不附带 fake_llm.py，缺省视为关闭。
        try:
            from . import fake_llm
        except ImportError:
            fake_llm = None

        if fake_llm is not None and fake_llm.enabled():
            with ospan(f"llm.{stage}(fake)", kind=K.LLM) as sp:
                data = _normalize_model_refs(
                    fake_llm.synthesize(stage, payload),
                    _collect_character_aliases(payload))
                obj = cls.model_validate(data)
                set_output(sp, obj.model_dump(mode="json"))
            return obj

        user = (json.dumps(payload, ensure_ascii=False, indent=2, default=_json_default)
                if not isinstance(payload, str) else payload)
        if extra_user:
            user = f"{user}\n\n{extra_user}"

        repairs = int(self.cfg.get("llm.json_repairs", 2))
        last_err: Exception | None = None
        last_truncated = False
        raw = ""

        for attempt in range(repairs + 1):
            with ospan(
                f"llm.{stage}" + (f"@repair{attempt}" if attempt else ""),
                kind=K.LLM,
                inp={"system_chars": len(system), "payload_chars": len(user)},
            ) as sp:
                t0 = time.time()
                try:
                    raw = self._chat_with_api_retry(
                        stage,
                        system,
                        user,
                        temperature=temperature,
                        max_tokens=max_tokens,
                        images=images,
                        span=sp,
                    )
                except Exception as e:  # noqa: BLE001 - API 失败不进入 JSON 修复循环
                    if attempt:
                        with self._stats_lock:
                            self.stats.repairs += 1
                    raise LLMCallError(
                        f"员工 {stage} 调用模型失败（已按配置做网络重试）："
                        f"{type(e).__name__}: {e}"
                    ) from e
                secs = time.time() - t0
                # 真实调用写在 thread-local；测试/旧代码直接替换 chat 时只写
                # 实例属性，这里回退到共享字段保持兼容。
                u = self._thread_last("usage", self.last_usage)
                finish = self._thread_last("finish_reason",
                                           self.last_finish_reason)
                thinking = self._thread_last("thinking", self.last_thinking)
                streamed = self._thread_last("stream", self.last_stream)
                first_token = self._thread_last(
                    "first_token", self.last_first_token_secs)
                truncated = finish == "length"
                try:
                    # 被 max_tokens 截断的回复即便侥幸能解析，内容也残缺，必须重问
                    if truncated:
                        raise ValueError(
                            "模型输出被 max_tokens 截断（finish_reason=length），"
                            "JSON 不完整或字段缺失"
                        )
                    data = parse_json(raw)
                    data = _drop_nulls(data)  # 模型爱写 null：剔除后回落默认，避免类型判非法
                    data = _normalize_model_refs(
                        data, _collect_character_aliases(payload))
                    obj = cls.model_validate(data)
                    set_output(sp, obj.model_dump(mode="json"))
                    _append_llm_trace({
                        "t": time.strftime("%H:%M:%S"),
                        "stage": stage,
                        "model": self._current_model(),
                        "attempt": attempt,
                        "ok": True,
                        "secs": round(secs, 1),
                        "in_tokens": _usage_int(u, "prompt_tokens"),
                        "out_tokens": _usage_int(u, "completion_tokens"),
                        "cache_hit_tokens": _usage_int(
                            u, "prompt_cache_hit_tokens", "cached_tokens"),
                        "cache_miss_tokens": _usage_int(
                            u, "prompt_cache_miss_tokens", "uncached_tokens"),
                        "thinking": thinking,
                        "stream": streamed,
                        "first_token_secs": (round(first_token, 2)
                                             if first_token is not None else None),
                        "finish_reason": finish,
                        "truncated": False,
                        "system": system[:400],
                        "user": user[:600],
                        "output": raw[:6000],
                    })
                    return obj
                except Exception as e:  # noqa: BLE001 - 三类失败统一处理
                    last_err = e
                    last_truncated = truncated
                    _append_llm_trace({
                        "t": time.strftime("%H:%M:%S"),
                        "stage": stage,
                        "model": self._current_model(),
                        "attempt": attempt,
                        "ok": False,
                        "secs": round(secs, 1),
                        "in_tokens": _usage_int(u, "prompt_tokens"),
                        "out_tokens": _usage_int(u, "completion_tokens"),
                        "cache_hit_tokens": _usage_int(
                            u, "prompt_cache_hit_tokens", "cached_tokens"),
                        "cache_miss_tokens": _usage_int(
                            u, "prompt_cache_miss_tokens", "uncached_tokens"),
                        "thinking": thinking,
                        "stream": streamed,
                        "finish_reason": finish,
                        "truncated": truncated,
                        "error": schema_error_text(e)[:300],
                        "system": system[:400],
                        "user": user[:600],
                        "output": raw[:6000],
                    })
                    if sp is not None:
                        sp.set_attribute("llm.attempt", attempt + 1)
                        if truncated:
                            sp.set_attribute("llm.truncated", True)
                    if attempt:
                        with self._stats_lock:
                            self.stats.repairs += 1
            if attempt >= repairs:
                break
            # 回灌：把「你上次错在哪」说清楚，比重新问一遍有效得多
            hint = (
                "⚠️ 上一次输出因 max_tokens 被截断：请删减冗余描写、压缩到硬性上限内，"
                "并保证 JSON 完整闭合。\n"
                if last_truncated else ""
            )
            user = (
                f"{user}\n\n"
                f"⚠️ 你上一次的输出不合法，必须修正后重新只输出一个 JSON 对象。\n"
                f"{hint}"
                f"错误详情：\n{schema_error_text(last_err)}\n"
                f"你上次的原始输出（前 1500 字）：\n{raw[:1500]}\n"
                f"请严格按给定结构重新输出完整 JSON，不要解释、不要 markdown 围栏。"
            )

        raise LLMFormatError(
            f"员工 {stage} 连续 {repairs + 1} 次输出都不符合 Schema："
            f"{schema_error_text(last_err) if last_err else '未知错误'}"
        ) from last_err

    def system_prompt(self, role: str, task: str, stage: str, rules: str = "") -> str:
        """统一的 system prompt 组装：角色 / 输入 / **输出结构** / 铁律。"""
        from .schemas import HANDOFFS

        cls = HANDOFFS[stage]
        blocks = [
            f"# 你的角色\n{role}",
            f"# 你的任务\n{task}",
            "# 输入\n用户消息是一段 JSON。只依据它工作，不要臆造不存在的信息。",
            f"# 你必须输出的 JSON 结构\n{describe(cls)}",
        ]
        if rules:
            blocks.append(f"# 铁律\n{rules}")
        blocks.append(
            "# 输出要求\n"
            "只输出**一个 JSON 对象**，不要 markdown 代码围栏、不要任何解释文字。\n"
            "所有字段名必须与上面结构完全一致（英文键名）。中文内容照写中文。"
        )
        return "\n\n".join(blocks)


class LLMFormatError(RuntimeError):
    """模型反复输出不合规 JSON。"""


class LLMCallError(RuntimeError):
    """模型 API 调用本身失败（网络/鉴权/限流重试耗尽）。"""


_TRACE_LOCK = threading.Lock()


def _append_llm_trace(rec: dict) -> None:
    """本地 LLM 调用记录（看板 /api/trace 数据源）。

    与 observability 开关无关，无条件追加一行 JSON 到 state/llm_trace.jsonl，
    供 dashboard 展示输入/输出；体积超过 6 MB 时轮转为仅保留尾部 200 行。
    加锁保证跨集并行生成（B2）时多线程写同一文件不交错。
    """
    try:
        p = Path(__file__).resolve().parents[1] / "state" / "llm_trace.jsonl"
        if not p.parent.is_dir():
            return
        with _TRACE_LOCK:
            with p.open("a", encoding="utf-8") as f:
                f.write(json.dumps(rec, ensure_ascii=False) + "\n")
            if p.stat().st_size > 6 * 1024 * 1024:
                rows = p.read_text(encoding="utf-8", errors="replace").splitlines()[-200:]
                p.write_text("\n".join(rows) + "\n", encoding="utf-8")
    except OSError:
        pass


# --------------------------------------------------------------------------
def _to_data_url(path: str) -> str:
    """本地图片 → data URL，给支持多模态的 OpenAI 兼容接口（豆包视觉模型等）用。"""
    import base64
    import mimetypes
    from pathlib import Path

    p = Path(path)
    mime = mimetypes.guess_type(p.name)[0] or "image/png"
    return f"data:{mime};base64,{base64.b64encode(p.read_bytes()).decode()}"


def _safe_json(resp) -> Any:
    try:
        return resp.json()
    except Exception:  # noqa: BLE001
        return resp.text[:500]
