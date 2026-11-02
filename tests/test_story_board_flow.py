# -*- coding: utf-8 -*-
"""分镜编排：EDL 顺序 + 提示词并行 + 整场缓存复用（假模型，不联网）。"""
from __future__ import annotations

from types import SimpleNamespace

from manju.story.models import (
    ActionBeat, ActionBeatLine, Cast, CastCharacter, CutPlan, CutPrompt,
    ImageSpec, ScriptBeat, ScriptEpisode, ScriptScene, SegmentPlan,
    SegmentPrompts, WardrobeState,
)
from manju.story.stages import StoryPipeline


class _FakeLLM:
    def __init__(self):
        self.calls: list[str] = []
        self.payloads: list[tuple[str, dict]] = []

    def structured(self, stage, system, payload, *, model_cls, **_kw):
        self.calls.append(stage)
        self.payloads.append((stage, payload))
        if model_cls is ActionBeatLine:
            n = len(payload["scene"]["beats"])
            return ActionBeatLine(scene_index=payload["scene_index"], beats=[
                ActionBeat(
                    beats=(i, i), goal=f"完成第{i}拍",
                    from_prev="延续上一拍", action=f"动作{i}",
                    to_next="进入下一拍", who="C1", where="center",
                    facing="north", suggested_shot="中景动作",
                    state_from="站在河岸", screen_side="center")
                for i in range(1, n + 1)])
        if model_cls is SegmentPlan:
            n = len(payload["beat_plan"])
            sizes = ["wide", "medium", "close", "wide"]
            cams = ["Static Shot", "Push In", "Static Shot", "Pull Out"]
            return SegmentPlan(scene_index=payload["scene_index"], cuts=[
                CutPlan(size=sizes[(i - 1) % 4],
                        camera=cams[(i - 1) % 4])
                for i in range(1, n + 1)])
        if model_cls is SegmentPrompts:
            n = len(payload["cut_plan"])
            return SegmentPrompts(prompts=[
                CutPrompt(
                    prompt_en="a young woman stands by the river in a gray "
                              "dress, cold dawn light, mist on the water",
                    motion="she slowly raises her hand")
                for _ in range(n)])
        raise AssertionError(f"unexpected model_cls={model_cls}")


def _cast() -> Cast:
    return Cast(source="fixture", characters=[CastCharacter(
        id="C1", name="多萝西",
        wardrobe_states=[WardrobeState(
            state_id="C1-default", desc_en="gray gingham dress")],
        image=ImageSpec(prompt="a young woman in a gray dress"))])


def _episode() -> ScriptEpisode:
    coverages = ["establish", "action", "insert", "reaction",
                 "pov", "action", "reaction", "action"]
    scenes = []
    k = 0
    for si in (1, 2):
        beats = []
        for _ in range(4):
            beats.append(ScriptBeat(
                action="多萝茜在河岸向前走。", coverage=coverages[k],
                state_from="站在河岸", screen_side="center"))
            k += 1
        scenes.append(ScriptScene(
            id="S1", location="河岸", characters=["C1"],
            lighting="cold blue dawn", information="她决定出发",
            beats=beats))
    return ScriptEpisode(ep=1, title="出发", target_seconds=20.0,
                         hook="她抬头", cliff="雾里出现人影",
                         hook_beat=(2, 4), scenes=scenes)


def test_board_runs_edl_then_parallel_prompts_and_reuses_cache(tmp_path):
    fake = _FakeLLM()
    pipe = StoryPipeline(llm=fake, workdir=tmp_path, resume=True)
    ep = _episode()
    art = SimpleNamespace(scenes=[], props=[])
    staging = SimpleNamespace(scenes=[])

    board = pipe.board(ep, art, _cast(), staging)

    assert len(board.segments) == 2
    assert [[c.beats for c in s.cuts] for s in board.segments] == [
        [(i, i) for i in range(1, 5)], [(i, i) for i in range(1, 5)]]
    # 顺序：每场 actionline + EDL，随后每场提示词展开
    assert len(fake.calls) == 6
    assert sum("novel-board-edl" in s for s in fake.calls) == 2
    assert sum("novel-board-prompts" in s for s in fake.calls) == 2
    # 提示词确实来自第二级，不是占位符
    assert all(c.prompt_en.startswith("a young woman stands")
               for s in board.segments for c in s.cuts)
    # 跨场状态继承
    assert (board.segments[1].cuts[0].state_from
            == board.segments[0].cuts[-1].state_out)

    # 第二次：整场缓存全部命中，不再调用模型
    before = len(fake.calls)
    again = pipe.board(ep, art, _cast(), staging)
    assert len(fake.calls) == before
    assert [[c.beats for c in s.cuts] for s in again.segments] == [
        [(i, i) for i in range(1, 5)], [(i, i) for i in range(1, 5)]]


def test_key_emphasis_reaches_the_prompt_stage_and_forces_a_moving_closeup(
        tmp_path):
    """关键道具的 emphasis/visual_focus 必须传到提示词层，不能只停在剧本。"""
    fake = _FakeLLM()
    pipe = StoryPipeline(llm=fake, workdir=tmp_path)
    ep = _episode()
    ep.scenes[0].beats[0].emphasis = "key_prop"
    ep.scenes[0].beats[0].visual_focus = "旧皮箱"
    art = SimpleNamespace(scenes=[], props=[])
    staging = SimpleNamespace(scenes=[])

    board = pipe.board(ep, art, _cast(), staging)

    prompts_payload = next(p for s, p in fake.payloads
                           if "novel-board-prompts" in s)
    first = prompts_payload["cut_plan"][0]
    assert first["emphasis"] == "key_prop"
    assert first["visual_focus"] == "旧皮箱"
    cut = board.segments[0].cuts[0]
    assert cut.emphasis == "key_prop"
    assert cut.visual_focus == "旧皮箱"
    assert cut.size in {"close", "extreme-close"}
    assert cut.camera != "Static Shot"
