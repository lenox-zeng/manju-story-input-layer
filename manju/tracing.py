"""可观测性：OpenTelemetry + OpenInference 埋点，**缺依赖时自动降级为空操作**。

相比 v1 的两点改动
------------------
1. **零硬依赖**。v1 在模块顶层 `from phoenix.otel import register`，
   没装 Phoenix 就 import 失败、整条流水线起不来。这里全部 try/except，
   依赖缺失 = 埋点变空操作，业务照跑。`Observability.available` 说明当前状态。
2. **两种导出后端**：直发 Phoenix（默认，无需 Docker），
   或经 OTel Collector 的 OTLP/HTTP（可 fan-out 到 Phoenix + OpenObserve）。

跨进程 trace 透传沿用 W3C traceparent：主编发送 A2A Task 前 `inject_carrier()`，
员工进程收到后 `attach(carrier)` 续上同一条 trace。
"""

from __future__ import annotations

import base64
import contextlib
import json
import os
from enum import Enum
from typing import Any

from .config import get_config


class OpenInferenceKind(str, Enum):
    """OpenInference 的 span 类型（避免硬依赖其 semconv 包）。"""

    CHAIN = "CHAIN"
    AGENT = "AGENT"
    TOOL = "TOOL"
    LLM = "LLM"
    RETRIEVER = "RETRIEVER"
    EMBEDDING = "EMBEDDING"
    RERANKER = "RERANKER"
    GUARDRAIL = "GUARDRAIL"
    EVALUATOR = "EVALUATOR"


SA_KIND = "openinference.span.kind"
SA_INPUT = "input.value"
SA_OUTPUT = "output.value"
SA_MIME = "input.mime_type"

_MAX_ATTR = 8000


class Observability:
    """进程级单例。`enabled` 为 False 时所有方法都是 no-op。"""

    def __init__(self) -> None:
        self.enabled = False
        self.available = False
        self.reason = "未初始化"
        self._tracer: Any = None
        self._propagator: Any = None
        self._setup_done = False

    # -- 初始化 -----------------------------------------------------------
    def setup(self, project: str | None = None, endpoint: str | None = None) -> None:
        if self._setup_done:
            return
        self._setup_done = True

        cfg = get_config()
        if not cfg.get("observability.enabled", False):
            self.reason = "observability.enabled=false（埋点关闭，流水线照常跑）"
            return
        self.enabled = True

        try:
            from opentelemetry import trace
            from opentelemetry.sdk.resources import Resource
            from opentelemetry.sdk.trace import TracerProvider
            from opentelemetry.sdk.trace.export import BatchSpanProcessor
            from opentelemetry.trace.propagation.tracecontext import (
                TraceContextTextMapPropagator,
            )
        except ImportError as e:
            self.enabled = False
            self.reason = f"缺少 opentelemetry-sdk：{e}。pip install opentelemetry-sdk"
            return

        ep = endpoint or cfg.get("observability.otlp_endpoint")
        provider = TracerProvider(
            resource=Resource.create(
                {"service.name": project or cfg.get("observability.project", "manju-pipeline")}
            )
        )
        try:
            from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter

            provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter(endpoint=ep)))
        except ImportError as e:
            self.reason = f"缺少 OTLP 导出器（{e}）；span 只在内存里，不上报"
            ep = "(未上报)"

        trace.set_tracer_provider(provider)
        self._tracer = trace.get_tracer("manju-pipeline")
        self._propagator = TraceContextTextMapPropagator()
        self.available = True
        self.reason = f"已启用，导出到 {ep}"

        # 自动给所有 OpenAI SDK 调用埋点（DeepSeek / 豆包等兼容接口同样生效）
        try:
            from openinference.instrumentation.openai import OpenAIInstrumentor

            OpenAIInstrumentor().instrument(tracer_provider=provider)
        except Exception as e:  # noqa: BLE001 - 自动埋点失败不该拖垮业务
            self.reason += f"（OpenAI 自动埋点不可用：{type(e).__name__}）"

    # -- span -------------------------------------------------------------
    @contextlib.contextmanager
    def span(
        self,
        name: str,
        kind: OpenInferenceKind = OpenInferenceKind.CHAIN,
        inp: Any = None,
        attributes: dict[str, Any] | None = None,
    ):
        if not self.available:
            yield None
            return
        with self._tracer.start_as_current_span(name) as sp:
            sp.set_attribute(SA_KIND, kind.value)
            if inp is not None:
                sp.set_attribute(SA_INPUT, _dump(inp))
                sp.set_attribute(SA_MIME, "application/json")
            for k, v in (attributes or {}).items():
                _set(sp, k, v)
            try:
                yield sp
            except Exception as e:  # noqa: BLE001
                self.record_error(sp, e)
                raise

    @staticmethod
    def set_output(span: Any, out: Any) -> None:
        if span is not None:
            _set(span, SA_OUTPUT, _dump(out))
            _set(span, "output.mime_type", "application/json")

    @staticmethod
    def set_attr(span: Any, key: str, value: Any) -> None:
        if span is not None:
            _set(span, key, value)

    @staticmethod
    def record_error(span: Any, exc: BaseException) -> None:
        if span is None:
            return
        try:
            from opentelemetry.trace import Status, StatusCode

            span.set_status(Status(StatusCode.ERROR, str(exc)))
            span.record_exception(exc)
        except Exception:  # noqa: BLE001
            pass

    @staticmethod
    def add_event(span: Any, name: str, attrs: dict[str, Any] | None = None) -> None:
        if span is None:
            return
        try:
            span.add_event(name, attributes={k: str(v) for k, v in (attrs or {}).items()})
        except Exception:  # noqa: BLE001
            pass

    # -- 跨进程透传 --------------------------------------------------------
    def inject(self) -> dict[str, str]:
        carrier: dict[str, str] = {}
        if self.available:
            try:
                self._propagator.inject(carrier)
            except Exception:  # noqa: BLE001
                pass
        return carrier

    def attach(self, carrier: dict[str, str] | None) -> Any:
        if not self.available or not carrier:
            return None
        try:
            from opentelemetry import context as otel_context

            ctx = self._propagator.extract(carrier)
            return otel_context.attach(ctx)
        except Exception:  # noqa: BLE001
            return None

    def detach(self, token: Any) -> None:
        if token is None or not self.available:
            return
        try:
            from opentelemetry import context as otel_context

            otel_context.detach(token)
        except Exception:  # noqa: BLE001
            pass


OBS = Observability()


def setup_tracing(project: str | None = None, endpoint: str | None = None) -> Observability:
    OBS.setup(project=project, endpoint=endpoint)
    return OBS


def ospan(name, kind=OpenInferenceKind.CHAIN, inp=None, attributes=None):
    return OBS.span(name, kind, inp, attributes)


def set_output(span, out) -> None:
    OBS.set_output(span, out)


def inject_carrier() -> dict[str, str]:
    return OBS.inject()


def attach(carrier):
    return OBS.attach(carrier)


def detach(token) -> None:
    OBS.detach(token)


def tracing_status() -> str:
    return f"{'开' if OBS.available else '关'}：{OBS.reason}"


# --------------------------------------------------------------------------
def _dump(value: Any, limit: int = _MAX_ATTR) -> str:
    try:
        s = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, default=str)
    except Exception:  # noqa: BLE001
        s = repr(value)
    return s if len(s) <= limit else s[:limit] + f"…(共 {len(s)} 字)"


def _set(span: Any, key: str, value: Any) -> None:
    try:
        if value is None:
            return
        if isinstance(value, (bool, int, float, str)):
            span.set_attribute(key, value if not isinstance(value, str) else _dump(value))
        else:
            span.set_attribute(key, _dump(value))
    except Exception:  # noqa: BLE001
        pass


# --------------------------------------------------------------------------
def otlp_endpoint_hint() -> str:
    """给启动脚本/文档用的提示：当前端点 + 是否经 Collector。"""
    cfg = get_config()
    ep = cfg.get("observability.otlp_endpoint", "")
    if ":4318" in ep:
        return f"{ep}（经 OTel Collector，可同时 fan-out 到 Phoenix 与 OpenObserve）"
    if ":6006" in ep:
        return f"{ep}（直发 Phoenix，不需要 Docker）"
    return ep


def basic_auth_header(email: str, password: str) -> str:
    """OpenObserve 用 HTTP Basic；v1 文档让人手算 base64，这里给个函数。"""
    return "Basic " + base64.b64encode(f"{email}:{password}".encode()).decode()


def env_flags() -> dict[str, str]:
    return {
        "PHOENIX_ENABLED": os.environ.get("PHOENIX_ENABLED", "(未设，默认开)"),
        "OTEL_TRACES_ENDPOINT": os.environ.get("OTEL_TRACES_ENDPOINT", "(未设，用 config)"),
    }
