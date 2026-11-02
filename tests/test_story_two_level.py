# -*- coding: utf-8 -*-
"""两级大纲：全局圣经 + 分批分集梗概；剧本只拿本集切片 + 相邻梗概。"""
from __future__ import annotations

from manju.story.episodes import EpisodeSlice
from manju.story.models import (
    Beat, EpisodePlan, Outline, OutlineCharacter, OutlineProp, OutlineScene,
    ScriptParams, Spine, StoryBible, StoryParams,
)
from manju.story.stages import (
    StoryPipeline, _neighbors, _repair_unused_characters, _slice_text,
)


class FakeEpisodesLLM:
    def __init__(self):
        self.payloads: list[dict] = []

    def structured(self, stage, system, payload, *, model_cls, **_kw):
        self.payloads.append(payload)
        plans = []
        for item in payload["episodes"]:
            ep = item["ep"]
            plans.append(EpisodePlan(
                ep=ep, title=f"第{ep}集", synopsis=f"第{ep}集的连续事件",
                hook="开场钩子", cliff="结尾悬念",
                scene_ids=["S1"], character_ids=["C1"], prop_ids=["P1"]))
        return model_cls(episodes=plans)

    def chat(self, *_a, **_kw):                    # 不应被调用
        raise AssertionError("两级大纲不该走 chat()")


def _bible(n: int) -> StoryBible:
    return StoryBible(
        source="fixture", title="渡口", logline="雾夜渡口的一只旧皮箱。",
        adaptation={"core": "真相与代价",
                    "keep": [{"what": "渡口", "why": "主场景"}]},
        spine=Spine(want="过河", obstacle="巡夜人", choice="交出箱子",
                    cost="失去退路", turn="箱子里的不是钱", hook="第三个人"),
        params=StoryParams(episodes=n, target_seconds=90, aspect_ratio="9:16"),
        characters=[OutlineCharacter(id="C1", name="阿禾", role="主角",
                                     tier="lead")],
        scenes=[OutlineScene(id="S1", name="渡口", primary=True)],
        props=[OutlineProp(id="P1", name="旧皮箱", function="装秘密")],
        beats=[Beat(id=f"B{i}", type="悬念",
                    weight="major" if i == 1 else "minor", episode=i)
               for i in range(1, n + 1)],
    )


def _slices(n: int) -> list[EpisodeSlice]:
    out = []
    pos = 0
    for i in range(1, n + 1):
        text = f"EP{i}UNIQUE" + "正文" * 100
        out.append(EpisodeSlice(i, pos, pos + len(text), (i,), text))
        pos += len(text)
    return out


def test_episode_plans_are_batched_by_10_and_use_own_slice(tmp_path):
    n = 25
    pipe = StoryPipeline(llm=FakeEpisodesLLM(), workdir=tmp_path)
    outline = pipe.episode_plans(_bible(n), _slices(n), 90.0)
    llm: FakeEpisodesLLM = pipe.llm

    assert [len(p["episodes"]) for p in llm.payloads] == [10, 10, 5]
    assert [e.ep for e in outline.episodes] == list(range(1, n + 1))
    for payload in llm.payloads:
        for item in payload["episodes"]:
            assert item["source_excerpt"].startswith(f"EP{item['ep']}UNIQUE")
    # 第二批发给模型的只有上一批最后一集的梗概
    assert llm.payloads[1]["previous_synopsis"]["ep"] == 10


def test_episode_plans_resume_from_batch_cache(tmp_path):
    n = 25
    first = StoryPipeline(llm=FakeEpisodesLLM(), workdir=tmp_path, resume=True)
    first.episode_plans(_bible(n), _slices(n), 90.0)

    second_llm = FakeEpisodesLLM()
    second = StoryPipeline(llm=second_llm, workdir=tmp_path, resume=True)
    second.episode_plans(_bible(n), _slices(n), 90.0)
    assert second_llm.payloads == []


def test_slice_text_never_falls_back_to_source_head():
    slices = _slices(3)
    assert _slice_text(slices, 2, "SOURCE" * 2000, 1000).startswith("EP2UNIQUE")


def test_neighbors_are_only_prev_cur_next(tmp_path):
    n = 5
    pipe = StoryPipeline(llm=FakeEpisodesLLM(), workdir=tmp_path)
    outline = pipe.episode_plans(_bible(n), _slices(n), 90.0)
    assert [x["ep"] for x in _neighbors(outline, 3)] == [2, 3, 4]
    assert [x["ep"] for x in _neighbors(outline, 1)] == [1, 2]


def _outline_with_support() -> Outline:
    return Outline(
        source="fixture", title="渡口", logline="雾夜渡口的一只旧皮箱。",
        adaptation={"core": "真相与代价",
                    "keep": [{"what": "渡口", "why": "主场景"}]},
        spine=Spine(want="过河", obstacle="巡夜人", choice="交出箱子",
                    cost="失去退路", turn="箱子里的不是钱", hook="第三个人"),
        params=StoryParams(episodes=2, target_seconds=90, aspect_ratio="9:16"),
        characters=[
            OutlineCharacter(id="C1", name="陈野", role="主角", tier="lead"),
            OutlineCharacter(id="C6", name="赵若曦", role="配角",
                            tier="support"),
            OutlineCharacter(id="C7", name="未出场者", role="配角",
                            tier="support"),
        ],
        scenes=[OutlineScene(id="S1", name="车厢", primary=True)],
        beats=[],
        props=[OutlineProp(id="P1", name="手机", function="联络")],
        episodes=[
            EpisodePlan(ep=1, title="一", synopsis="s", hook="h", cliff="c",
                        scene_ids=["S1"], character_ids=["C1"],
                        prop_ids=["P1"]),
            EpisodePlan(ep=2, title="二", synopsis="s", hook="h", cliff="c",
                        scene_ids=["S1"], character_ids=["C1"],
                        prop_ids=["P1"]),
        ],
    )


def _slice(ep: int, text: str) -> EpisodeSlice:
    return EpisodeSlice(ep, 0, len(text), (ep,), text)


def test_repair_unused_character_uses_source_evidence():
    o = _outline_with_support()
    logs: list[str] = []
    added, removed = _repair_unused_characters(
        o, [_slice(1, "陈野和赵若曦在车厢"),
            _slice(2, "赵若曦 赵若曦 站台")], logs.append)

    assert added == ["C6"]
    assert removed == ["C7"]
    assert "C6" in o.episodes[1].character_ids
    assert "C7" not in [c.id for c in o.characters]
    assert any("补入第 2 集" in line for line in logs)


def test_repair_unused_lead_is_not_silently_dropped():
    o = _outline_with_support()
    for ep in o.episodes:
        ep.character_ids = []
    logs: list[str] = []
    _repair_unused_characters(o, [_slice(1, "无人出场")], logs.append)

    ids = [c.id for c in o.characters]
    assert "C1" in ids          # lead 保留，交给质量门报错
    assert "C6" not in ids      # support 未出现，剔除
    assert "C7" not in ids
    assert any("主角 C1" in line for line in logs)


def test_episode_plans_feed_full_slice_and_repair_missing_character(tmp_path):
    n = 2
    bible = _bible(n)
    bible.characters.append(
        OutlineCharacter(id="C6", name="赵若曦", role="配角", tier="support"))
    slices = [
        _slice(1, "开头" + "甲" * 3000 + "赵若曦出场" + "乙" * 3000),
        _slice(2, "开头" + "丙" * 3000 + "赵若曦出场" + "丁" * 3000),
    ]
    pipe = StoryPipeline(llm=FakeEpisodesLLM(), workdir=tmp_path)

    outline = pipe.episode_plans(bible, slices, 90.0)

    payload = pipe.llm.payloads[0]
    assert payload["episodes"][0]["source_excerpt"] == slices[0].text
    assert all("赵若曦" in item["source_excerpt"]
               for item in payload["episodes"])
    assert any("C6" in ep.character_ids for ep in outline.episodes)


def test_script_params_still_default_to_18_chars():
    assert ScriptParams().max_line_chars == 18
