# -*- coding: utf-8 -*-
"""思考模式 / 流式输出 / 能力降级（全部用假客户端，不调真实 API）。"""
from __future__ import annotations

from types import SimpleNamespace

from manju.llm import LLMClient


class _Err(Exception):
    def __init__(self, code: int, msg: str):
        super().__init__(msg)
        self.status_code = code


class _Stream:
    def __init__(self, chunks):
        self._chunks = chunks
        self.closed = False

    def __iter__(self):
        return iter(self._chunks)

    def close(self):
        self.closed = True


class _Client:
    def __init__(self, handler):
        self.calls: list[dict] = []
        self._handler = handler
        self.chat = SimpleNamespace(
            completions=SimpleNamespace(create=self._create))

    def _create(self, **kwargs):
        self.calls.append(kwargs)
        return self._handler(kwargs)


def _client(thinking: str = "disabled", *, stream: bool = True,
            by_stage: dict | None = None) -> LLMClient:
    c = LLMClient()
    c.cfg = {
        "llm.model": "mini",
        "llm.thinking": thinking,
        "llm.thinking_by_stage": by_stage or {},
        "llm.stream": stream,
        "llm.stream_idle_timeout_sec": 30,
        "llm.request_timeout_sec": 60,
    }
    return c


def _usage(hit: int = 7, miss: int = 3, prompt: int = 10,
           completion: int = 5) -> SimpleNamespace:
    return SimpleNamespace(
        prompt_tokens=prompt, completion_tokens=completion,
        prompt_cache_hit_tokens=hit, prompt_cache_miss_tokens=miss)


def _stream_chunks() -> list[SimpleNamespace]:
    def chunk(content=None, reasoning=None, finish=None, usage=None):
        return SimpleNamespace(
            usage=usage,
            choices=([SimpleNamespace(
                delta=SimpleNamespace(content=content,
                                      reasoning_content=reasoning),
                finish_reason=finish)] if content or reasoning or finish else []),
        )
    return [
        chunk(content='{"a":'),
        chunk(content="1}"),
        chunk(finish="stop", usage=_usage()),
    ]


def _plain(content: str = '{"a": 1}') -> SimpleNamespace:
    msg = SimpleNamespace(content=content, reasoning_content=None)
    return SimpleNamespace(
        usage=_usage(), choices=[SimpleNamespace(
            message=msg, finish_reason="stop")])


def test_chat_streams_with_thinking_disabled_and_cache_usage():
    c = _client()
    stream = _Stream(_stream_chunks())
    fake = _Client(lambda _kw: stream)
    c._client = fake

    assert c.chat("sys", "user", json_mode=False) == '{"a":1}'
    call = fake.calls[0]
    assert call["stream"] is True
    assert call["stream_options"] == {"include_usage": True}
    assert call["extra_body"] == {"thinking": {"type": "disabled"}}
    assert "timeout" in call

    assert stream.closed is True
    assert c.last_stream is True
    assert c.last_thinking == "disabled"
    assert c.last_finish_reason == "stop"
    assert c.last_cache_hit_tokens == 7
    assert c.last_cache_miss_tokens == 3
    assert c.stats.stream_calls == 1


def test_thinking_param_rejection_degrades_to_plain():
    c = _client(stream=False)

    def handler(kw):
        if "extra_body" in kw or "reasoning_effort" in kw:
            raise _Err(400, "Unsupported parameter: thinking")
        return _plain()

    fake = _Client(handler)
    c._client = fake

    assert c.chat("sys", "user", json_mode=False) == '{"a": 1}'
    assert len(fake.calls) == 2
    assert "extra_body" not in fake.calls[-1]
    assert c.last_thinking == "auto"
    assert "disabled" in c._thinking_unsupported_modes


def test_stream_rejection_falls_back_to_non_stream():
    c = _client(thinking="auto")

    def handler(kw):
        if kw.get("stream"):
            raise _Err(400, "stream is not supported by this endpoint")
        return _plain()

    fake = _Client(handler)
    c._client = fake

    assert c.chat("sys", "user", json_mode=False) == '{"a": 1}'
    assert len(fake.calls) == 2
    assert c.last_stream is False
    assert c._stream_unsupported is True
    # 同进程后续调用不再尝试流式
    c.chat("sys", "user", json_mode=False)
    assert fake.calls[-1].get("stream") is None


def test_stream_options_rejection_retries_without_usage_option():
    c = _client(thinking="auto")

    def handler(kw):
        if kw.get("stream_options"):
            raise _Err(400, "Unsupported parameter: stream_options")
        if kw.get("stream"):
            return _Stream(_stream_chunks())
        return _plain()

    fake = _Client(handler)
    c._client = fake

    assert c.chat("sys", "user", json_mode=False) == '{"a":1}'
    assert fake.calls[0].get("stream_options") is not None
    assert fake.calls[1].get("stream") is True
    assert "stream_options" not in fake.calls[1]
    assert c._stream_options_unsupported is True
    assert c.last_stream is True


def test_thinking_by_stage_uses_longest_prefix():
    c = _client(thinking="low", by_stage={
        "novel-board": "low",
        "novel-board-prompts": "disabled",
        "novel-script": "high",
    })
    assert c._resolve_thinking("novel-board-prompts 第1集第2场", None) == "disabled"
    assert c._resolve_thinking("novel-board-edl 第1集第2场", None) == "low"
    assert c._resolve_thinking("novel-script", None) == "high"
    assert c._resolve_thinking("other", None) == "low"

    kwargs, mode = c._thinking_kwargs("disabled")
    assert mode == "disabled"
    assert kwargs["extra_body"]["thinking"]["type"] == "disabled"
    kwargs, mode = c._thinking_kwargs("high")
    assert mode == "high"
    assert kwargs["reasoning_effort"] == "high"
