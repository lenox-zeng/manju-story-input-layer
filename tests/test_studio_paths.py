# -*- coding: utf-8 -*-
"""Studio 路径/续跑/画风合并等纯逻辑测试（不起 HTTP 服务）。"""
from __future__ import annotations

import json

import pytest

import manju.studio as studio


def _isolate(tmp_path, monkeypatch, novel: str = "demo") -> None:
    state = tmp_path / "state" / "story"
    state.mkdir(parents=True)
    monkeypatch.setattr(studio, "STATE", state)
    monkeypatch.setattr(studio, "INPUT", tmp_path / "input")
    monkeypatch.setattr(studio, "CURRENT", state / ".studio_current.json")
    monkeypatch.setattr(studio, "DIRTY", state / ".studio_dirty")
    monkeypatch.setattr(studio, "CONFIG", tmp_path / "config.json")
    monkeypatch.setattr(studio, "_CURRENT_NOVEL", novel)


def _write(p, data) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")


def test_story_dir_prefers_work_then_final(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    state = studio.STATE
    work = state / ".work_demo"
    final = state / "demo"

    # 都没有：返回工作层路径（首次生成位置）
    assert studio._story_dir() == work
    assert studio._current_info()["phase"] == "work"

    # 只有指纹等元数据不算产物，不能据此选工作层
    work.mkdir(parents=True)
    _write(work / "_resume_fp.json", {"outline": "x"})
    assert studio._has_artifacts(work) is False
    assert studio._story_dir() == work
    assert studio._current_info()["phase"] == "work"

    # 最终目录有产物且工作层为空：读成品
    _write(final / "outline.json", {"title": "t"})
    assert studio._story_dir() == final
    assert studio._current_info()["phase"] == "final"

    # 工作层一旦有中间产物，优先读工作层
    _write(work / "outline.json", {"title": "t2"})
    assert studio._story_dir() == work
    assert studio._current_info()["phase"] == "work"


def test_get_board_uses_two_digit_filename(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    work = studio.STATE / ".work_demo"
    # 现行落盘是 StoryEpisodeBoard，不是 StoryStoryboard 包装。
    _write(work / "board_ep01.json", {
        "ep": 1, "segments": [{"id": "E01-01", "scene_index": 1, "cuts": []}],
    })
    # 三位的旧命名不应被读取
    _write(work / "board_ep001.json", {"episodes": [{"ep": 999}]})

    assert studio.list_eps() == [1]
    assert studio.get_board(1)["ep"] == 1
    assert studio._ep_obj(studio.get_board(1))["segments"][0]["scene_index"] == 1


def test_ep_obj_accepts_legacy_storyboard_wrapper():
    """早期产物外面包了 {episodes:[...]}，读旧目录时不能 KeyError。"""
    legacy = {"source": "x", "episodes": [{"ep": 2, "segments": [{}, {}]}]}
    assert studio._ep_obj(legacy)["ep"] == 2
    assert len(studio._ep_obj(legacy)["segments"]) == 2


def test_ep_summary_and_edit_shot_use_episode_board_shape(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    work = studio.STATE / ".work_demo"
    _write(work / "outline.json", {"episodes": [{"ep": 1, "title": "试播"}]})
    _write(work / "board_ep01.json", {
        "ep": 1,
        "segments": [{
            "id": "E01-01", "scene_index": 1,
            "cuts": [
                {"shot": "S1", "prompt_en": "a", "size": "wide",
                 "motion": "walk", "seconds": 2.5, "coverage": "master"},
                {"shot": "S2", "prompt_en": "b", "size": "close",
                 "motion": "turn", "seconds": 1.5, "coverage": "insert"},
            ],
        }],
    })

    rows = studio.ep_summary()
    assert rows == [{"ep": 1, "title": "试播", "scenes": 1,
                     "cuts": 2, "seconds": 4.0}]

    r = studio.edit_shot(1, 1, {"prompt_en": "c", "camera": "Push In"})
    assert r["shot"] == "S2"
    seg = json.loads((work / "board_ep01.json")
                     .read_text(encoding="utf-8"))["segments"][0]
    assert seg["cuts"][1]["prompt_en"] == "c"
    assert seg["cuts"][1]["camera"] == "Push In"


def test_save_visual_merges_structured_fields(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    _write(studio.STATE / ".work_demo" / "outline.json", {
        "title": "t",
        "visual_bible": {"style_keywords": ["ink"], "main_colors": ["#111"],
                         "do_dont": {"do": ["x"]}},
    })

    studio.save_visual({"medium": "film still", "lens": "35mm", "palette": ""})

    vb = json.loads((studio.STATE / ".work_demo" / "outline.json")
                    .read_text(encoding="utf-8"))["visual_bible"]
    assert vb["style_keywords"] == ["ink"]
    assert vb["main_colors"] == ["#111"]
    assert vb["do_dont"] == {"do": ["x"]}
    assert vb["medium"] == "film still"
    assert vb["lens"] == "35mm"
    assert "palette" not in vb  # 空值不写回，避免冲掉已有配置


def test_parse_range_variants():
    assert studio._parse_range("1-4") == (1, 4)
    assert studio._parse_range("2–6") == (2, 6)
    assert studio._parse_range(" 3 — 5 ") == (3, 5)
    assert studio._parse_range([7, 9]) == (7, 9)
    assert studio._parse_range("auto") is None
    assert studio._parse_range(None) is None


def test_build_pipeline_cmd_defaults_to_resume_and_range(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    inp = tmp_path / "input"
    inp.mkdir()
    (inp / "demo.txt").write_text("x", encoding="utf-8")

    cmd = studio._build_pipeline_cmd("demo.txt", {"range": "3-5", "episodes": "auto"})
    assert "--resume" in cmd
    assert "--from-episode" in cmd and cmd[cmd.index("--from-episode") + 1] == "3"
    assert "--to-episode" in cmd and cmd[cmd.index("--to-episode") + 1] == "5"
    assert "--episodes" not in cmd
    assert "--workdir" not in cmd  # 交给 CLI 的临时工作层策略

    fresh = studio._build_pipeline_cmd("demo.txt", {"fresh": True, "episodes": 4})
    assert "--resume" not in fresh
    assert fresh[fresh.index("--episodes") + 1] == "4"

    explicit = studio._build_pipeline_cmd("demo.txt", {
        "range": "1-9", "from": 2, "to": 3, "clean": True,
    })
    assert explicit[explicit.index("--from-episode") + 1] == "2"
    assert explicit[explicit.index("--to-episode") + 1] == "3"
    assert "--clean" in explicit


def test_build_pipeline_cmd_chapter_mode(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    inp = tmp_path / "input"
    inp.mkdir()
    (inp / "demo.txt").write_text("x", encoding="utf-8")

    auto = studio._build_pipeline_cmd("demo.txt", {
        "episodes": "auto", "chapters_per_episode": 3,
    })
    assert auto[auto.index("--chapters-per-episode") + 1] == "3"
    assert "--episodes" not in auto

    # 显式集数走字数分集，不能再叠加章节模式（CLI 会报冲突）
    fixed = studio._build_pipeline_cmd("demo.txt", {
        "episodes": 4, "chapters_per_episode": 3,
    })
    assert "--chapters-per-episode" not in fixed
    assert fixed[fixed.index("--episodes") + 1] == "4"


def test_config_update_persists_whitelisted_settings_only(monkeypatch):
    """配置页提交的字段必须真的写进 config；Key 例外，只留在进程内存。"""
    monkeypatch.setattr(studio, "_SESSION_API_KEY", "")
    cfg = {"llm": {"api_key": "sk-test-legacy-key"}, "story": {}}
    studio.apply_config_update(cfg, {
        "story_max_auto_episodes": 20,
        "story_chars_per_episode": 8000,
        # 温度由各阶段固定，配置页不再持久化。
        "temperature": 1.5,
        # 章节数已改为生成页按次选择，配置页不再持久化。
        "story_chapters_per_episode": 3,
        "story": {"prompt_identity_mode": "full"},
        "studio": {"generation": {"episodes": "auto"}},
        # 新 Key 只进内存，旧版明文 key 在保存时清除。
        "api_key": "sk-test-session-key",
    })
    assert cfg["llm"]["story_max_auto_episodes"] == 20
    assert cfg["llm"]["story_chars_per_episode"] == 8000
    assert "story_chapters_per_episode" not in cfg["llm"]
    assert "temperature" not in cfg["llm"]
    assert "api_key" not in cfg["llm"]
    assert studio._SESSION_API_KEY == "sk-test-session-key"
    # 输入层不再接受形象模式；旧页面/旧配置提交也应被忽略。
    assert "prompt_identity_mode" not in cfg["story"]
    assert cfg["studio"]["generation"]["episodes"] == "auto"
    # 掩码回传不能覆盖会话 Key（长 key / 短 key 两种掩码）。
    studio.apply_config_update(cfg, {"api_key": "sk-••••cdef"})
    studio.apply_config_update(cfg, {"api_key": "••••"})
    assert studio._SESSION_API_KEY == "sk-test-session-key"
    assert "api_key" not in cfg["llm"]
    # 非白名单字段不落盘。
    studio.apply_config_update(cfg, {"story": {"unexpected": "x"}})
    assert "unexpected" not in cfg["story"]


def test_config_save_promotes_legacy_key_to_session(monkeypatch):
    """旧页面只回传掩码时，不能把唯一可用的 Key 静默删掉。"""
    monkeypatch.setattr(studio, "_SESSION_API_KEY", "")
    cfg = {"llm": {"api_key": "sk-test-legacy-key"}}

    studio.apply_config_update(cfg, {"api_key": "sk-••••-key"})

    assert "api_key" not in cfg["llm"]
    assert studio._SESSION_API_KEY == "sk-test-legacy-key"


def test_effective_api_key_priority(monkeypatch):
    """环境变量 > Studio 会话 > 旧 config；任何分支都不写回配置。"""
    monkeypatch.setattr(studio, "_SESSION_API_KEY", "session-key")
    monkeypatch.setenv("DEEPSEEK_API_KEY", "env-key")
    assert studio._effective_api_key({"llm": {"api_key": "cfg-key"}}) == "env-key"
    monkeypatch.delenv("DEEPSEEK_API_KEY")
    assert studio._effective_api_key({"llm": {"api_key": "cfg-key"}}) == "session-key"
    monkeypatch.setattr(studio, "_SESSION_API_KEY", "")
    assert studio._effective_api_key({"llm": {"api_key": "cfg-key"}}) == "cfg-key"


def test_build_pipeline_cmd_explicit_zero_disables_chapter_mode():
    """生成页每次都显式给 0/N：0 必须传给 CLI，才能覆盖 config 的旧章节默认值。"""
    cmd = studio._build_pipeline_cmd("demo.txt", {})
    assert "--chapters-per-episode" not in cmd

    for val in (0, "", "0"):
        cmd = studio._build_pipeline_cmd(
            "demo.txt", {"episodes": "auto", "chapters_per_episode": val})
        assert cmd[cmd.index("--chapters-per-episode") + 1] == "0"

    cmd = studio._build_pipeline_cmd("demo.txt", {"chapters_per_episode": 2})
    assert cmd[cmd.index("--chapters-per-episode") + 1] == "2"


def test_generation_defaults_normalizes_legacy_range():
    cfg_default = studio.generation_defaults({
        "llm": {"story_chapters_per_episode": 2},
        "studio": {"generation": {"episodes": "auto", "range": "3-5",
                                  "ratio": "16:9", "duration": 60}},
    })
    assert cfg_default["mode"] == "auto"
    assert cfg_default["from"] == 3 and cfg_default["to"] == 5
    assert cfg_default["ratio"] == "16:9" and cfg_default["duration"] == 60
    assert cfg_default["default_chapters"] == 2

    fixed = studio.generation_defaults({
        "llm": {},
        "studio": {"generation": {"episodes": 4, "chapters_per_episode": 0}},
    })
    assert fixed["mode"] == "fixed" and fixed["episodes"] == 4

    chapters = studio.generation_defaults({
        "llm": {},
        "studio": {"generation": {"episodes": "auto", "chapters_per_episode": 3}},
    })
    assert chapters["mode"] == "chapters" and chapters["chapters_per_episode"] == 3


def test_save_generation_defaults_is_single_source(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    cfg = {"llm": {}, "studio": {"generation": {"range": "1-9", "episodes": "auto"}}}
    _write(studio.CONFIG, cfg)

    studio.save_generation_defaults({
        "episodes": "auto", "chapters_per_episode": 0,
        "from": 2, "to": 3, "ratio": "16:9", "duration": 75,
    })

    gen = json.loads(studio.CONFIG.read_text(encoding="utf-8"))["studio"]["generation"]
    assert gen["from"] == 2 and gen["to"] == 3
    assert gen["episodes"] == "auto" and gen["chapters_per_episode"] == 0
    assert gen["ratio"] == "16:9" and gen["duration"] == 75
    assert "range" not in gen  # 旧键不再和新字段并存


def test_start_pipeline_rejects_missing_novel(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    ok, msg = studio.start_pipeline("nope.txt", {})
    assert ok is False
    assert "不存在" in msg


def test_pipeline_status_reports_published_paths(tmp_path, monkeypatch):
    """成功提示要能直接给出产物路径，不能只让用户去翻日志。"""
    _isolate(tmp_path, monkeypatch)
    monkeypatch.setattr(studio, "_PIPE", {
        "running": False, "done": True, "ok": True, "error": "",
        "lines": [], "story": "demo.txt", "out": "", "work": "",
    })

    st = studio.pipeline_status()
    assert st["out"] == str(tmp_path / "input" / "demo")
    assert st["work"] == str(studio.STATE / "demo")

    # 还没选小说时不回传半截路径。
    monkeypatch.setitem(studio._PIPE, "story", "")
    st = studio.pipeline_status()
    assert st["out"] == "" and st["work"] == ""


def test_read_llm_trace_reports_model_and_cost(tmp_path, monkeypatch):
    """日志页要能区分 Flash/Pro，并按官方价给出空闲/高峰费用区间。"""
    trace = tmp_path / "llm_trace.jsonl"
    rows = [
        {"stage": "flash", "model": "deepseek-flash", "in_tokens": 1000,
         "out_tokens": 1000, "cache_hit_tokens": 100,
         "cache_miss_tokens": 900},
        {"stage": "pro", "model": "deepseek-v4-pro", "in_tokens": 1000,
         "out_tokens": 1000, "cache_hit_tokens": 100,
         "cache_miss_tokens": 900},
    ]
    trace.write_text(
        "\n".join(json.dumps(r, ensure_ascii=False) for r in rows) + "\n",
        encoding="utf-8")
    monkeypatch.setattr(studio, "TRACE_LOG", trace)

    d = studio.read_llm_trace(default_model="deepseek-flash")

    assert d["total"] == 2
    assert [e["stage"] for e in d["entries"]] == ["pro", "flash"]
    assert d["entries"][0]["model"] == "deepseek-v4-pro"
    assert d["entries"][0]["cost_offpeak_cny"] == pytest.approx(0.017565)
    assert d["entries"][0]["cost_peak_cny"] == pytest.approx(0.03513)
    assert d["entries"][1]["cost_offpeak_cny"] == pytest.approx(0.004902)
    assert d["entries"][1]["cost_peak_cny"] == pytest.approx(0.009804)
    assert d["summary"]["models"] == {
        "deepseek-v4-pro": 1, "deepseek-flash": 1}
    assert d["summary"]["cache_hit_ratio"] == pytest.approx(0.1)
    assert d["summary"]["cost"]["offpeak_cny"] == pytest.approx(0.0225)
    assert d["summary"]["cost"]["peak_cny"] == pytest.approx(0.0449)
    assert d["summary"]["cost"]["price_date"] == "2026-10-27"


def test_read_llm_trace_infers_legacy_model_and_missing_cache(
        tmp_path, monkeypatch):
    """旧记录没有 model/缓存明细：按当前配置推断，输入按未命中保守估算。"""
    trace = tmp_path / "llm_trace.jsonl"
    trace.write_text(json.dumps({
        "stage": "old", "in_tokens": 1000, "out_tokens": 1000,
    }) + "\n", encoding="utf-8")
    monkeypatch.setattr(studio, "TRACE_LOG", trace)

    d = studio.read_llm_trace(default_model="deepseek-flash")

    e = d["entries"][0]
    assert e["model"] == "deepseek-flash"
    assert e["model_inferred"] is True
    assert d["summary"]["model_inferred_calls"] == 1
    assert e["cost_offpeak_cny"] == pytest.approx(0.005)
    assert e["cost_peak_cny"] == pytest.approx(0.01)
    assert d["summary"]["cache_hit_tokens"] == 0
    assert d["summary"]["cache_miss_tokens"] == 1000


def test_read_llm_trace_flags_unknown_model(tmp_path, monkeypatch):
    trace = tmp_path / "llm_trace.jsonl"
    trace.write_text(json.dumps({
        "stage": "custom", "model": "custom-model",
        "in_tokens": 1_000_000, "out_tokens": 0,
    }) + "\n", encoding="utf-8")
    monkeypatch.setattr(studio, "TRACE_LOG", trace)

    d = studio.read_llm_trace(default_model="deepseek-flash")

    assert d["entries"][0]["pricing_model"] == "deepseek-flash"
    assert d["entries"][0]["pricing_unknown"] is True
    assert d["summary"]["cost"]["unknown_models"] == ["custom-model"]
    assert d["summary"]["cost"]["offpeak_cny"] == pytest.approx(1.0)
