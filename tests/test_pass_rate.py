# -*- coding: utf-8 -*-
"""端到端通过率脚本的 Key 检测：环境变量优先，旧配置仅兜底。"""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _load_script():
    path = ROOT / "scripts" / "pass_rate.py"
    spec = importlib.util.spec_from_file_location("manju_pass_rate_script", path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def _clear_key_env(monkeypatch) -> None:
    for name in ("MANJU_LLM_API_KEY", "DEEPSEEK_API_KEY", "OPENAI_API_KEY"):
        monkeypatch.delenv(name, raising=False)


def test_pass_rate_detects_env_key(tmp_path, monkeypatch):
    _clear_key_env(monkeypatch)
    monkeypatch.setenv("MANJU_ROOT", str(tmp_path))
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-test-placeholder")
    (tmp_path / "config.json").write_text("{}", encoding="utf-8")

    assert _load_script()._has_api_key(tmp_path) is True


def test_pass_rate_keeps_legacy_config_key_as_fallback(tmp_path, monkeypatch):
    _clear_key_env(monkeypatch)
    monkeypatch.setenv("MANJU_ROOT", str(tmp_path))
    (tmp_path / "config.json").write_text(
        json.dumps({"llm": {"api_key": "sk-test-placeholder"}}), encoding="utf-8")

    assert _load_script()._has_api_key(tmp_path) is True


def test_pass_rate_reports_missing_key(tmp_path, monkeypatch):
    _clear_key_env(monkeypatch)
    monkeypatch.setenv("MANJU_ROOT", str(tmp_path))
    (tmp_path / "config.json").write_text("{}", encoding="utf-8")

    assert _load_script()._has_api_key(tmp_path) is False


def test_pass_rate_parses_token_and_repair_totals():
    script = _load_script()
    log = ("...\n"
           "  LLM: 42 次调用 / 修复 7 次 / in 120000 tok / out 34000 tok / 615s\n"
           "[OK] done\n")
    assert script._parse_llm_stats(log) == {
        "calls": 42,
        "repairs": 7,
        "prompt_tokens": 120000,
        "completion_tokens": 34000,
        "llm_seconds": 615,
    }
    assert script._parse_llm_stats("no stats here") == {}
