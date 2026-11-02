# -*- coding: utf-8 -*-
"""输入层验收：集范围过滤、短篇回归、逐场缓存续跑。"""
from __future__ import annotations

from types import SimpleNamespace

from manju.story.models import (CutState, StoryCut, StoryEpisodeBoard,
                                StorySegment)
from manju.story.stages import StoryPipeline


def _fake_pipeline(tmp_path, episodes=4):
    """桩掉所有生成阶段，只观察 run() 的调度与状态传递。"""
    pipe = StoryPipeline(llm=object(), workdir=tmp_path)
    plans = [SimpleNamespace(ep=i) for i in range(1, episodes + 1)]
    pipe.prepare_source = lambda text: "源文"
    pipe.bible = lambda *a, **k: SimpleNamespace()
    pipe.episode_plans = lambda *a, **k: SimpleNamespace(
        source="源文", episodes=plans)
    pipe.cast = lambda *a, **k: SimpleNamespace()
    pipe.art = lambda *a, **k: SimpleNamespace()

    calls = {"script": [], "staging": [], "board": [], "state_in": []}

    def script(ep, *_a, **_k):
        calls["script"].append(ep)
        return SimpleNamespace(ep=ep)

    def staging(ep, *_a, **_k):
        calls["staging"].append(ep.ep)
        return SimpleNamespace(locations=[], scenes=[])

    def board(ep, *_a, **k):
        calls["board"].append(ep.ep)
        calls["state_in"].append(k["state_in"].posture.get("C1", ""))
        cut = StoryCut(beats=(1, 1), seconds=2.0, size="medium",
                       frame="中景", prompt_en="medium shot",
                       state_out=CutState(posture={"C1": f"pose{ep.ep}"}))
        seg = StorySegment(id=f"E{ep.ep:02d}-01", scene_index=1, cuts=[cut])
        return StoryEpisodeBoard(ep=ep.ep, segments=[seg])

    pipe.script = script
    pipe.staging = staging
    pipe.board = board
    return pipe, calls


def _slices(n):
    return [SimpleNamespace(index=i, text="x") for i in range(n)]


def _board(ep, posture):
    cut = StoryCut(beats=(1, 1), seconds=2.0, size="medium",
                   frame="中景", prompt_en="medium shot",
                   state_out=CutState(posture={"C1": posture}))
    return StoryEpisodeBoard(
        ep=ep, segments=[StorySegment(id=f"E{ep:02d}-01", scene_index=1,
                                      cuts=[cut])])


def test_episode_range_inherits_previous_cached_state(tmp_path):
    pipe, calls = _fake_pipeline(tmp_path)
    pipe._save("board_ep01", _board(1, "pose1"))
    _outline, _cast, _art, scripts, _staging, board = pipe.run(
        "源文", 4, 90, "9:16", slices=_slices(4), episode_range=(2, 3))
    assert calls["script"] == [2, 3]
    assert calls["staging"] == [2, 3]
    assert calls["board"] == [2, 3]
    assert [s.ep for s in scripts] == [2, 3]
    # 范围外集不重跑，但第 2 集的 state_in 来自第 1 集缓存终态
    assert calls["state_in"][0] == "pose1"
    assert calls["state_in"][1] == "pose2"
    assert [e.ep for e in board.episodes] == [2, 3]


def test_episode_range_without_previous_cache_starts_empty(tmp_path):
    pipe, calls = _fake_pipeline(tmp_path)
    msgs = []
    pipe.progress = msgs.append
    pipe.run("源文", 4, 90, "9:16", slices=_slices(4), episode_range=(2, 2))
    assert calls["state_in"] == [""]
    assert any("没有第 1 集分镜缓存" in m for m in msgs)


def test_no_range_runs_every_episode_in_order(tmp_path):
    pipe, calls = _fake_pipeline(tmp_path)
    pipe.run("源文", 4, 90, "9:16", slices=_slices(4))
    assert calls["script"] == [1, 2, 3, 4]
    assert calls["state_in"] == ["", "pose1", "pose2", "pose3"]


def test_cache_keeps_segment_when_state_inputs_match(tmp_path, monkeypatch):
    """逐场缓存：同样的 state_in / 上一镜，命中后不再调用模型。"""
    pipe = StoryPipeline(llm=object(), workdir=tmp_path, resume=True)
    cut = StoryCut(beats=(1, 1), seconds=2.0, size="medium",
                   frame="中景，人物站在门口", prompt_en="medium shot, woman at door")
    seg = StorySegment(id="E01-01", scene_index=1, cuts=[cut])
    pipe._save("board_ep01_scene01", seg)

    def _boom(*_a, **_k):
        raise AssertionError("缓存命中时不应再生成")

    monkeypatch.setattr(pipe, "_generate", _boom)
    got = pipe._load("board_ep01_scene01", StorySegment)
    assert got is not None and got.id == seg.id
