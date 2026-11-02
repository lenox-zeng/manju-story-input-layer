# -*- coding: utf-8 -*-
"""LLM 网络重试、截断识别与 trace 记录（不调用真实 API）。"""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

import pytest
from pydantic import BaseModel

import manju.llm as llm_mod
from manju.llm import LLMClient, LLMFormatError


class _Mini(BaseModel):
    a: int


def _client(monkeypatch, **cfg) -> LLMClient:
    c = LLMClient()
    c.cfg = {
        "llm.model": "mini",
        "llm.json_repairs": 0,
        "llm.api_retries": 2,
        "llm.api_retry_backoff_sec": 0,
        **cfg,
    }
    monkeypatch.setattr(llm_mod.time, "sleep", lambda _s: None)
    return c


def test_is_transient_error_by_status_and_name():
    class E(Exception):
        def __init__(self, code=None):
            self.status_code = code

    assert llm_mod._is_transient_error(E(429)) is True
    assert llm_mod._is_transient_error(E(500)) is True
    assert llm_mod._is_transient_error(E(401)) is False
    assert llm_mod._is_transient_error(E(400)) is False

    class APIConnectionError(Exception):
        pass

    assert llm_mod._is_transient_error(APIConnectionError("x")) is True

    class ValueError2(ValueError):
        pass

    assert llm_mod._is_transient_error(ValueError2("bad json")) is False


def test_api_retry_retries_transient_then_succeeds(monkeypatch):
    c = _client(monkeypatch)
    traces: list[dict] = []
    monkeypatch.setattr(llm_mod, "_append_llm_trace", traces.append)

    class Flaky(Exception):
        status_code = 503

    calls = []

    def fake_chat(*_a, **_k):
        calls.append(1)
        if len(calls) < 3:
            raise Flaky("upstream unavailable")
        return "ok"

    monkeypatch.setattr(c, "chat", fake_chat)
    out = c._chat_with_api_retry(
        "script", "sys", "user", temperature=None, max_tokens=10, images=None)
    assert out == "ok"
    assert len(calls) == 3
    assert len(traces) == 2
    assert all(t["api_error"] and t["transient"] for t in traces)
    assert "Flaky" in traces[0]["error"]


def test_api_retry_does_not_retry_auth_error(monkeypatch):
    c = _client(monkeypatch)
    monkeypatch.setattr(llm_mod, "_append_llm_trace", lambda _r: None)

    class Denied(Exception):
        status_code = 401

    calls = []

    def fake_chat(*_a, **_k):
        calls.append(1)
        raise Denied("bad key")

    monkeypatch.setattr(c, "chat", fake_chat)
    with pytest.raises(Denied):
        c._chat_with_api_retry(
            "script", "sys", "user", temperature=None, max_tokens=10, images=None)
    assert len(calls) == 1


def test_structured_raises_llm_call_error_after_api_failure(monkeypatch):
    c = _client(monkeypatch)
    traces: list[dict] = []
    monkeypatch.setattr(llm_mod, "_append_llm_trace", traces.append)

    def fake_chat(*_a, **_k):
        raise RuntimeError("connection reset")

    monkeypatch.setattr(c, "chat", fake_chat)
    with pytest.raises(llm_mod.LLMCallError, match="script"):
        c.structured("script", "sys", {"x": 1}, model_cls=_Mini)
    assert traces and traces[-1]["api_error"] is True
    assert all(t["model"] == "mini" for t in traces)


def test_structured_rejects_truncated_output(monkeypatch):
    c = _client(monkeypatch, **{"llm.json_repairs": 1})
    traces: list[dict] = []
    monkeypatch.setattr(llm_mod, "_append_llm_trace", traces.append)

    def fake_chat(*_a, **_k):
        # 即便截断内容碰巧能解析，也必须判失败
        c.last_finish_reason = "length"
        c.last_usage = None
        return '{"a": 1}'

    monkeypatch.setattr(c, "chat", fake_chat)
    with pytest.raises(LLMFormatError):
        c.structured("script", "sys", {"x": 1}, model_cls=_Mini)
    assert len(traces) == 2  # 首次 + 1 次修复
    assert all(t["truncated"] is True and t["ok"] is False for t in traces)
    assert all(t.get("finish_reason") == "length" for t in traces)
    assert all(t["model"] == "mini" for t in traces)


def test_json_default_handles_pydantic_dataclass_set_and_path():
    @dataclass
    class _DC:
        n: int

    payload = {"m": _Mini(a=1), "d": _DC(2), "p": Path("x/y"), "s": {1}}
    data = json.loads(json.dumps(payload, default=llm_mod._json_default))
    assert data["m"] == {"a": 1}
    assert data["d"] == {"n": 2}
    assert data["p"] == str(Path("x/y"))
    assert data["s"] == [1]


def test_structured_payload_serializes_nested_pydantic_models(monkeypatch):
    """回归：payload 里漏转的 Pydantic 对象曾让整条流水线 TypeError。"""
    c = _client(monkeypatch)
    monkeypatch.setattr(llm_mod, "_append_llm_trace", lambda _r: None)
    captured: dict[str, str] = {}

    def fake_chat(*args, **_kw):
        captured["user"] = args[1]
        c.last_finish_reason = "stop"
        c.last_usage = None
        return '{"a": 7}'

    monkeypatch.setattr(c, "chat", fake_chat)
    obj = c.structured("script", "sys", {"nested": _Mini(a=7)}, model_cls=_Mini)
    assert obj.a == 7
    assert '"a": 7' in captured["user"]


def test_structured_resolves_character_names_to_ids(monkeypatch):
    """模型把中文人名写进 Cid 字段时，应在 pydantic 校验前归一化。

    真实故障：CutSubject.char 收到“亨利叔叔”，pattern ^C\\d+$ 直接抛
    ValidationError，整场 EDL 还没进质量门就判失败。
    """
    from pydantic import Field

    class _Subject(BaseModel):
        char: str = Field(pattern=r"^C\d+$")

    class _Cut(BaseModel):
        characters: list[str]
        speaker: str | None = None
        subjects: list[_Subject]
        state_out: dict[str, dict[str, str]]

    c = _client(monkeypatch)
    monkeypatch.setattr(llm_mod, "_append_llm_trace", lambda _r: None)
    payload = {"cast": {"characters": [
        {"id": "C1", "name": "亨利叔叔"},
        {"id": "C2", "name": "多萝西"},
    ]}}

    def fake_chat(*_a, **_k):
        c.last_finish_reason = "stop"
        c.last_usage = None
        return json.dumps({
            "characters": ["亨利叔叔"],
            "speaker": "多萝西（C2）",
            "subjects": [{"char": "亨利叔叔（C1）"}],
            "state_out": {
                "where": {"亨利叔叔": "center"},
                "props": {"小篮子": "多萝西"},
            },
        }, ensure_ascii=False)

    monkeypatch.setattr(c, "chat", fake_chat)
    obj = c.structured("novel-board-edl", "sys", payload, model_cls=_Cut)

    assert obj.characters == ["C1"]
    assert obj.speaker == "C2"
    assert obj.subjects[0].char == "C1"
    assert obj.state_out["where"] == {"C1": "center"}
    assert obj.state_out["props"] == {"小篮子": "C2"}


def test_health_retries_transient_and_returns_status(monkeypatch):
    c = _client(monkeypatch)
    calls: list[str] = []

    class Resp:
        status_code = 200
        text = ""

        def json(self):
            return {"data": []}

    def fake_get(url, **_kw):
        calls.append(url)
        if len(calls) == 1:
            raise RuntimeError("temporary timeout")
        return Resp()

    monkeypatch.setattr("httpx.get", fake_get)
    health = c.health(timeout=0.01, attempts=2)
    assert health["ok"] is True
    assert health["status"] == 200
    assert health["attempt"] == 2
    assert len(calls) == 2


def test_health_reports_auth_error_without_retry(monkeypatch):
    c = _client(monkeypatch)
    calls: list[str] = []

    class Resp:
        status_code = 401
        text = ""

        def json(self):
            return {"error": {"message": "invalid api key"}}

    monkeypatch.setattr("httpx.get", lambda url, **_kw: (
        calls.append(url), Resp())[1])
    health = c.health(timeout=0.01, attempts=3)
    assert health["ok"] is False
    assert health["status"] == 401
    assert health["attempt"] == 1
    assert len(calls) == 1


def test_structured_marks_normal_success(monkeypatch):
    c = _client(monkeypatch)
    traces: list[dict] = []
    monkeypatch.setattr(llm_mod, "_append_llm_trace", traces.append)

    def fake_chat(*_a, **_k):
        c.last_finish_reason = "stop"
        c.last_usage = None
        return '{"a": 3}'

    monkeypatch.setattr(c, "chat", fake_chat)
    obj = c.structured("script", "sys", {"x": 1}, model_cls=_Mini)
    assert obj.a == 3
    assert traces[0]["ok"] is True
    assert traces[0]["finish_reason"] == "stop"
    assert traces[0]["truncated"] is False
    assert traces[0]["model"] == "mini"
