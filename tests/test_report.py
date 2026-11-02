# -*- coding: utf-8 -*-
"""故事层报告：index/ep 页、WebP 兜底、长篇按季折叠。"""
from __future__ import annotations

from manju.report import SEASON_SIZE, build_report
from manju.story.models import StoryStoryboard


def _fixture():
    from test_story_layer import _fixture as base
    return base()


def test_report_writes_pages_and_webp(tmp_path):
    outline, cast, art, scripts, _staging, board = _fixture()
    result = build_report(outline, cast, art, scripts, board,
                          outdir=tmp_path, screenshot=False)
    assert result.index.is_file()
    assert result.episodes and result.episodes[0].name == "ep01.html"
    assert result.webp and result.webp.is_file() and result.webp.stat().st_size > 0
    assert not result.screenshot
    assert any("占位图" in n for n in result.notes)

    index = result.index.read_text(encoding="utf-8")
    assert "总览" in index and "角色与出场矩阵" in index
    assert "阿禾" in index and "旧皮箱" in index
    ep = result.episodes[0].read_text(encoding="utf-8")
    assert "分镜卡" in ep and "口型" in ep and "连续性" in ep


def test_long_series_folds_by_season(tmp_path):
    outline, cast, art, scripts, _staging, board = _fixture()
    episodes = [outline.episodes[0].model_copy(update={"ep": i})
                for i in range(1, 113)]
    long_outline = outline.model_copy(update={"episodes": episodes})
    result = build_report(long_outline, cast, art, scripts,
                          StoryStoryboard(source="x", episodes=board.episodes),
                          outdir=tmp_path, screenshot=False)
    index = result.index.read_text(encoding="utf-8")
    seasons = (112 + SEASON_SIZE - 1) // SEASON_SIZE
    assert index.count("<details") == seasons
    assert f"第 {seasons} 季" in index
    # 独立集页仍然只按实际有剧本的集写；其余集在总览里可展开
    assert [p.name for p in result.episodes] == ["ep01.html"]
