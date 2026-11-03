# -*- coding: utf-8 -*-
"""自动分集：集数公式、章节边界、超长章拆分、覆盖不重叠、人工覆盖。"""
from __future__ import annotations

import json

import pytest

from manju.story.episodes import (
    EpisodeError, auto_episode_count, load_episode_map,
    needs_auto_confirmation, split_episodes, write_slices,
)
from manju.story.ingest import parse_text


def _novel(chapters: int, chars: int = 5000):
    parts = []
    for i in range(1, chapters + 1):
        head = f"第{i}章 标题\n"
        parts.append(head + "字" * (chars - len(head) - 1) + "\n")
    return parse_text("".join(parts))


def _assert_partition(novel, slices):
    assert slices[0].start == 0
    assert slices[-1].end == novel.total_chars
    for a, b in zip(slices, slices[1:]):
        assert a.end == b.start                      # 首尾相接、不重叠
    for s in slices:
        assert s.text == novel.text[s.start:s.end]


def test_episode_count_formula():
    assert auto_episode_count(300_000, 9000) == 34
    assert auto_episode_count(9000, 9000) == 1
    assert auto_episode_count(9001, 9000) == 2


def test_auto_limit():
    assert not needs_auto_confirmation(12)          # 12 本身不超限
    assert needs_auto_confirmation(13)
    assert not needs_auto_confirmation(8)


def test_60_chapters_300k_splits_into_34_without_leaking():
    novel = _novel(60, 5000)
    assert novel.total_chars == 300_000
    slices = split_episodes(novel)
    assert len(slices) == 34
    _assert_partition(novel, slices)

    seen: list[int] = []
    for s in slices:
        assert s.chapter_indexes
        assert s.chars > 0
        seen.extend(s.chapter_indexes)
    assert seen == list(range(1, 61))               # 每章只落在一集


def test_oversized_chapter_is_split_by_paragraphs():
    novel = parse_text("第一章 长夜\n" + ("段落内容。" * 50 + "\n") * 100)
    assert novel.chapters[0].chars > 2 * 9000
    slices = split_episodes(novel)
    assert len(slices) == auto_episode_count(novel.total_chars)
    _assert_partition(novel, slices)
    assert all(s.chapter_indexes == (1,) for s in slices)
    assert len({s.index for s in slices}) == len(slices)


def test_explicit_episode_count_overrides_auto():
    novel = _novel(60, 5000)
    slices = split_episodes(novel, episodes=5)
    assert len(slices) == 5
    _assert_partition(novel, slices)


def test_tiny_last_chapter_merged_in_auto_mode():
    # 超大第一章 + 极小尾章：按章节边界会切出 200 字的“独立一集”
    novel = parse_text("第一章 开端\n" + "字" * 9800 + "\n第二章 尾声\n" + "尾" * 200)
    slices = split_episodes(novel)
    assert len(slices) == 1
    assert slices[0].chars == novel.total_chars
    _assert_partition(novel, slices)

    # 显式指定集数时不自动合并（尊重用户）
    forced = split_episodes(novel, episodes=2)
    assert len(forced) == 2
    _assert_partition(novel, forced)


def test_oversized_chapter_yields_balanced_episodes():
    novel = parse_text("第一章 长夜\n" + "字" * 17000 + "\n第二章 尾声\n" + "尾" * 800)
    slices = split_episodes(novel)
    assert len(slices) == 2
    # 不会出现 17k + 0.8k 的极端不均
    assert all(s.chars >= 8000 for s in slices)
    _assert_partition(novel, slices)


def test_short_novel_is_one_episode():
    novel = _novel(1, 5000)
    slices = split_episodes(novel)
    assert len(slices) == 1
    assert slices[0].text == novel.text


# --------------------------------------------------------------------------
# 章节分集：章节是主依据，字数是安全阀
# --------------------------------------------------------------------------
def test_chapters_per_episode_groups_author_chapters():
    novel = _novel(6, 2000)
    slices = split_episodes(novel, chapters_per_episode=2)
    assert [s.chapter_indexes for s in slices] == [(1, 2), (3, 4), (5, 6)]
    assert len(slices) == 3
    _assert_partition(novel, slices)


def test_chapters_per_episode_merges_tiny_tail():
    # K=3 -> (1-3)(4-6)(7)；第 7 章只有 2000 字，低于安全下限，向前合并
    novel = _novel(7, 2000)
    slices = split_episodes(novel, chapters_per_episode=3)
    assert [s.chapter_indexes for s in slices] == [(1, 2, 3), (4, 5, 6, 7)]
    _assert_partition(novel, slices)


def test_chapters_per_episode_splits_oversized_group():
    # 两章共 24k，超过 1.5×9000 上限，必须按段落拆开
    novel = _novel(2, 12000)
    slices = split_episodes(novel, chapters_per_episode=2)
    assert len(slices) >= 2
    assert all(s.chars <= 13500 for s in slices)
    _assert_partition(novel, slices)


def test_chapters_per_episode_requires_chapters():
    novel = parse_text("没有章节标题的正文。" * 100)
    with pytest.raises(EpisodeError, match="章节"):
        split_episodes(novel, chapters_per_episode=2)


def test_chapters_per_episode_conflicts_with_explicit_episodes():
    novel = _novel(4, 1000)
    with pytest.raises(EpisodeError, match="不能同时"):
        split_episodes(novel, episodes=2, chapters_per_episode=2)


def test_episode_map_by_chapters(tmp_path):
    novel = _novel(10, 1000)
    p = tmp_path / "map.json"
    p.write_text(json.dumps({"episodes": [
        {"ep": 1, "chapters": [1, 3]},
        {"ep": 2, "chapters": [4, 6]},
        {"ep": 3, "chapters": [7, 10]},
    ]}), encoding="utf-8")
    slices = split_episodes(novel, episode_map=p)
    assert [s.index for s in slices] == [1, 2, 3]
    assert [s.chapter_indexes for s in slices] == [(1, 2, 3), (4, 5, 6),
                                                   (7, 8, 9, 10)]
    _assert_partition(novel, slices)


def test_episode_map_by_offsets(tmp_path):
    novel = _novel(4, 1000)
    p = tmp_path / "map.json"
    p.write_text(json.dumps({
        "1": {"start": 0, "end": 2000},
        "2": {"start": 2000, "end": novel.total_chars},
    }), encoding="utf-8")
    slices = load_episode_map(p, novel)
    assert [s.index for s in slices] == [1, 2]
    _assert_partition(novel, slices)


def test_episode_map_overlap_is_rejected(tmp_path):
    novel = _novel(4, 1000)
    p = tmp_path / "map.json"
    p.write_text(json.dumps({"episodes": [
        {"ep": 1, "start": 0, "end": 2500},
        {"ep": 2, "start": 2000, "end": 4000},
    ]}), encoding="utf-8")
    with pytest.raises(EpisodeError):
        load_episode_map(p, novel)


def test_write_slices_creates_ep_src_files(tmp_path):
    novel = _novel(3, 5000)
    slices = split_episodes(novel, episodes=3)
    paths = write_slices(slices, tmp_path)
    assert [p.name for p in paths] == ["ep_src_001.txt", "ep_src_002.txt",
                                       "ep_src_003.txt"]
    assert paths[1].read_text(encoding="utf-8").strip() == slices[1].text.strip()


# --------------------------------------------------------------------------
# CLI：auto 分集 / 超限确认
# --------------------------------------------------------------------------
def _long_text(path_root):
    parts = []
    for i in range(1, 61):
        head = f"第{i}章 标题\n"
        parts.append(head + "字" * (5000 - len(head) - 1) + "\n")
    p = path_root / "long.txt"
    p.write_text("".join(parts), encoding="utf-8")
    return p


@pytest.mark.skip(reason="依赖完整流水线的 manju.cli（novel 子命令）；本输入层包仅提供精简的 manju.story_cli，等价行为见 test_story_cli_dry_run_*")
def test_novel_dry_run_reports_34_episodes(root, capsys):
    from manju.cli import main

    p = _long_text(root)
    assert main(["novel", str(p), "--dry-run", "--yes"]) == 0
    out = capsys.readouterr().out
    assert "34 集" in out and "EP034" in out
    assert not (root / "state" / "story").exists()      # dry-run 不写盘


@pytest.mark.skip(reason="依赖完整流水线的 manju.cli（novel 子命令）；本输入层包仅提供精简的 manju.story_cli，等价行为见 test_story_cli_auto_limit_needs_yes")
def test_auto_episodes_over_limit_needs_confirmation(root, capsys):
    from manju.cli import main

    p = _long_text(root)
    assert main(["novel", str(p), "--dry-run"]) == 2
    assert "非交互环境" in capsys.readouterr().out


@pytest.mark.skip(reason="依赖完整流水线的 manju.cli（novel 子命令）；本输入层包仅提供精简的 manju.story_cli")
def test_novel_dry_run_rejects_bad_episode_range(root, capsys):
    from manju.cli import main

    p = _long_text(root)
    assert main(["novel", str(p), "--dry-run", "--yes",
                 "--from-episode", "5", "--to-episode", "2"]) == 2
    assert "集数范围非法" in capsys.readouterr().out


def test_story_cli_dry_run_auto_and_list_chapters(root, capsys):
    from manju.story_cli import main

    p = _long_text(root)
    assert main([str(p), "--list-chapters"]) == 0
    assert "第1章" in capsys.readouterr().out

    # 显式关闭情节模式与章节模式，验证原来的字数分集公式仍可用
    assert main([str(p), "--dry-run", "--yes", "--no-plot-mode",
                 "--chapters-per-episode", "0"]) == 0
    out = capsys.readouterr().out
    assert "34 集" in out and "EP034" in out

    # 显式集数下允许附带 "0=关闭章节模式"，不应误报冲突
    assert main([str(p), "--dry-run", "--yes", "--episodes", "2",
                 "--chapters-per-episode", "0"]) == 0
    assert "分集：2 集" in capsys.readouterr().out


def test_story_cli_dry_run_chapter_mode(root, capsys):
    from manju.story_cli import main

    p = _long_text(root)
    assert main([str(p), "--dry-run", "--yes",
                 "--chapters-per-episode", "2"]) == 0
    out = capsys.readouterr().out
    assert "按章节：每 2 章一集" in out
    assert "分集：30 集" in out
    assert "章 1–2" in out
    assert "章 59–60" in out


def test_story_cli_auto_limit_needs_yes(root, capsys):
    from manju.story_cli import main

    p = _long_text(root)
    # 剧情模式不按字数预估集数（集数由 LLM 通读后决定）；这里测的是
    # 旧字数/章节模式的自动集数上限保护。
    assert main([str(p), "--dry-run", "--no-plot-mode"]) == 2
    assert "非交互环境" in capsys.readouterr().out


def test_story_cli_version_and_doctor(root, capsys):
    from manju.story_cli import main

    with pytest.raises(SystemExit) as exc:
        main(["--version"])
    assert exc.value.code == 0
    assert "2.0" in capsys.readouterr().out

    assert main(["--doctor"]) == 0
    out = capsys.readouterr().out
    assert "manju-story-input 2.0.0" in out
    assert "package:" in out
    assert "project_root:" in out
    assert "api_key:" in out
    assert "value never printed" in out


def test_config_example_has_no_notes_or_duplicate_capability_keys():
    """示例配置只保留可执行字段，能力项以 story.* 为唯一来源。

    llm.api_key 允许以空字符串占位（模板不含真实密钥，密钥优先环境变量）；
    temperature 保持不出现（各阶段温度固定 0.0/0.2）。
    """
    from pathlib import Path

    p = Path(__file__).resolve().parents[1] / "config.example.json"
    data = json.loads(p.read_text(encoding="utf-8"))

    def note_keys(obj, prefix=""):
        found = []
        if isinstance(obj, dict):
            for k, v in obj.items():
                path = f"{prefix}.{k}" if prefix else k
                if str(k).startswith("_"):
                    found.append(path)
                found.extend(note_keys(v, path))
        return found

    assert note_keys(data) == []
    assert "pipeline" not in data
    assert "llm_mode" not in data.get("pipeline", {})
    assert data["story"]["max_shot_seconds"] == 3.2
    assert data["story"]["max_segment_seconds"] == 15.0
    assert data["llm"]["api_key"] == ""
    assert "temperature" not in data["llm"]


def test_doctor_warns_about_plaintext_key_and_legacy_config(root, capsys,
                                                           monkeypatch):
    from manju.story_cli import main

    for key in ("MANJU_LLM_API_KEY", "DEEPSEEK_API_KEY", "OPENAI_API_KEY"):
        monkeypatch.delenv(key, raising=False)
    (root / "config.json").write_text(json.dumps({
        "llm": {
            "api_key": "sk-test-not-a-real-key",
            "temperature": 0.7,
            "_说明": "legacy note",
        },
        "pipeline": {
            "llm_mode": "external",
            "contract_gate": "report",
            "drama_gate": "strict",
            "max_shot_seconds": 3.2,
            "pulid": {"max_faces_per_shot": 1, "no_face_chars": ["masked"]},
        },
    }, ensure_ascii=False), encoding="utf-8")

    assert main(["--doctor"]) == 0
    out = capsys.readouterr().out
    assert "config.json（建议迁移到环境变量）" in out
    assert "仍有明文 llm.api_key" in out
    assert "说明/注释键" in out
    assert "pipeline.llm_mode" in out
    assert "pipeline.contract_gate" in out
    assert "pipeline.max_shot_seconds" in out
    assert "pipeline.pulid.no_face_chars" in out
    assert "llm.temperature" in out
    assert "sk-test-not-a-real-key" not in out


def test_doctor_reports_env_key_source(root, capsys, monkeypatch):
    from manju.story_cli import main

    monkeypatch.setenv("MANJU_LLM_API_KEY", "env-test-key")
    assert main(["--doctor"]) == 0
    out = capsys.readouterr().out
    assert "api_key: env:MANJU_LLM_API_KEY" in out
    assert "env-test-key" not in out


def test_story_cli_missing_story_is_usage_error(root, capsys):
    from manju.story_cli import main

    assert main([]) == 2
    assert "缺少小说文件" in capsys.readouterr().out
