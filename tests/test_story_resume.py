# -*- coding: utf-8 -*-
"""resume 指纹与大纲道具裁剪的确定性测试。"""
from __future__ import annotations

import json
import re

from types import SimpleNamespace

from manju.story.models import (
    EpisodePlan, Outline, OutlineCharacter, OutlineProp, OutlineScene,
    CutPrompt, ScriptBeat, ScriptEpisode, ScriptScene, SegmentPrompts,
    StoryCut, StoryParams, StorySegment,
)
from manju.story.stages import (
    PROMPT_POLICY_REV, StoryPipeline, _cliff_action_text, _prune_unused_props,
    _repair_script_output, _repair_segment_output, _script_budget,
    _split_long_segments,
)


class _DummyLLM:
    cfg = {"llm.model": "model-a"}


def _pipe(tmp_path) -> StoryPipeline:
    return StoryPipeline(llm=_DummyLLM(), workdir=tmp_path)


def _outline(prop_ids: list[str]) -> Outline:
    return Outline(
        source="s", title="t", logline="l",
        adaptation={"core": "c", "keep": [{"what": "a", "why": "b"}]},
        spine={"want": "w", "obstacle": "o", "choice": "c",
               "cost": "c", "turn": "t", "hook": "h"},
        params=StoryParams(episodes=1, target_seconds=60),
        characters=[OutlineCharacter(id="C1", name="阿禾", role="主角")],
        beats=[],
        scenes=[OutlineScene(id="S1", name="河岸")],
        props=[
            OutlineProp(id="P1", name="旧箱", function="秘密"),
            OutlineProp(id="P2", name="雨伞", function="未用"),
        ],
        episodes=[EpisodePlan(
            ep=1, title="t", synopsis="s", hook="h", cliff="c",
            scene_ids=["S1"], character_ids=["C1"], prop_ids=prop_ids)],
    )


def test_fingerprint_includes_source_and_model(tmp_path):
    p = _pipe(tmp_path)
    p.set_fingerprint(1, 90, "9:16", source="小说甲", model="m1")
    fp = p._resume_fp

    p.set_fingerprint(1, 90, "9:16", source="小说甲", model="m1")
    assert p._resume_fp == fp

    p.set_fingerprint(1, 90, "9:16", source="小说乙", model="m1")
    assert p._resume_fp != fp

    p.set_fingerprint(1, 90, "9:16", source="小说甲", model="m2")
    assert p._resume_fp != fp


def test_fingerprint_reads_model_from_llm_cfg(tmp_path):
    p = _pipe(tmp_path)
    p.set_fingerprint(1, 90, "9:16", source="x")
    fp_default = p._resume_fp
    p.llm.cfg = {"llm.model": "model-b"}
    p.set_fingerprint(1, 90, "9:16", source="x")
    assert p._resume_fp != fp_default


def test_fp_ok_missing_key_policy(tmp_path):
    p = _pipe(tmp_path)
    p.set_fingerprint(1, 90, "9:16", source="x", model="m")
    (tmp_path / "_resume_fp.json").write_text(
        json.dumps({"outline": p._resume_fp}), encoding="utf-8")

    assert p._fp_ok("outline") is True
    assert p._fp_ok("script_ep01") is False       # 缺键默认视为失效
    assert p._fp_ok("script_ep01", missing_ok=True) is True  # 旧版本产物可放行

    p.set_fingerprint(1, 90, "16:9", source="x", model="m")
    assert p._fp_ok("outline") is False

    # 非 run 上下文（未设置指纹）退回原行为
    p._resume_fp = ""
    assert p._fp_ok("anything") is True


def test_prompt_policy_invalidates_prompt_cache_only(tmp_path):
    """提示词策略升级只重建提示词缓存，不应连圣经/大纲/剧本一起重跑。"""
    p = _pipe(tmp_path)
    p.set_fingerprint(1, 90, "9:16", source="x", model="m")
    fp = p._resume_fp
    seg = StorySegment(id="E01-01", scene_index=1, cuts=[StoryCut(
        beats=(1, 1), seconds=2.0, size="close", frame="阿禾停步",
        prompt_en="a woman stops at the pier")])
    p._save("board_ep01_scene01", seg)
    p._save("shots_ep01_scene01", SegmentPrompts(
        prompts=[CutPrompt(prompt_en="a woman stops", motion="she turns")]))
    p._touch_fp("outline")     # 代表圣经/大纲等阶段已缓存

    meta = json.loads((tmp_path / "_resume_fp.json").read_text(encoding="utf-8"))
    # EDL 段缓存不含提示词，不应绑定提示词策略。
    assert not p._prompt_policy_sensitive("board_ep01_scene01")
    assert "board_ep01_scene01::prompt_policy" not in meta
    assert p._prompt_policy_sensitive("shots_ep01_scene01")
    assert meta["shots_ep01_scene01::prompt_policy"] == PROMPT_POLICY_REV
    assert p._prompt_policy_ok("shots_ep01_scene01") is True

    # 旧工作层没有新键（或旧版本）→ 只让提示词缓存失效。
    meta["shots_ep01_scene01::prompt_policy"] = "old"
    (tmp_path / "_resume_fp.json").write_text(
        json.dumps(meta, ensure_ascii=False), encoding="utf-8")
    p._resume_meta = None      # 模拟新进程重新读盘
    assert p._prompt_policy_ok("shots_ep01_scene01") is False
    # 全局指纹不含提示词策略，其他阶段的缓存不会被连带失效。
    assert p._resume_fp == fp
    assert p._fp_ok("outline") is True


def test_prune_unused_props_removes_and_logs(tmp_path):
    o = _outline(["P1"])
    logs: list[str] = []
    removed = _prune_unused_props(o, logs.append)

    assert removed == ["P2"]
    assert [p.id for p in o.props] == ["P1"]
    assert any("P2" in line and "剔除" in line for line in logs)


def test_script_budget_stays_within_hard_limit():
    b = _script_budget(90)
    assert b["hard_max_seconds"] == 103.5
    # 2026-10-30：台词密度放开（约 3.0 字/秒，允许关键对话密集/纯对话集）。
    assert b["target_chars"] == 270
    assert b["suggest_lines"] == 17
    assert b["line_seconds_max"] == 4.0
    # 按目标字数（而不是每句都顶格）写满也应落在硬上限内；
    # 同时一镜一拍的下限不得高于上限，否则提示词自相矛盾。
    avg_line_seconds = (b["target_chars"] / b["suggest_lines"]) / b["chars_per_second"]
    assert (b["max_action_beats"] * b["action_seconds"]
            + b["suggest_lines"] * avg_line_seconds) <= b["hard_max_seconds"]
    assert b["min_total_beats"] == 26               # 90s / 3.5s（2026-10-30 放宽）
    assert b["max_total_beats"] >= b["min_total_beats"]

    short = _script_budget(60)
    assert short["target_chars"] == 180
    assert short["suggest_scenes"] == "1-4"
    assert short["suggest_beats_per_scene"] == "4-12"

    long = _script_budget(120)
    assert long["target_chars"] == 360
    assert long["suggest_lines"] == 22
    assert long["suggest_scenes"] == "1-4"
    assert long["max_total_beats"] > b["max_total_beats"]


def _script_ep(prior_beats: int, cliff: str = "银鞋突然发光，暗影一闪。") -> ScriptEpisode:
    prior = ScriptScene(
        id="S1", characters=["C1"], beat="高潮",
        beats=[ScriptBeat(action="多萝茜向前走。") for _ in range(prior_beats)])
    last = ScriptScene(
        id="S2", characters=["C1"], beat="结尾钩子", props=["P1"],
        beats=[ScriptBeat(action="多萝茜停在路口。")])
    return ScriptEpisode(
        ep=1, title="t", target_seconds=90, hook="h", cliff=cliff,
        hook_beat=(1, 1), scenes=[prior, last])


def test_repair_script_fills_objects_and_cliff_duration():
    ep = _script_ep(prior_beats=10)
    art = SimpleNamespace(props=[SimpleNamespace(id="P1", name="银鞋子")])
    logs: list[str] = []
    _repair_script_output(ep, _outline(["P1"]), art, logs.append)

    assert ep.scenes[-1].objects == ["银鞋子"]
    last = ep.scenes[-1]
    assert len(last.beats) == 2
    assert last.beats[-1].action
    assert len(last.beats[-1].action) <= 45
    assert last.beats[-1].state_from
    assert any("集末场景" in m and "FIX" in m for m in logs)


def test_repair_script_skips_when_total_would_overflow():
    ep = _script_ep(prior_beats=40)  # 40×2.5 + 2.5 = 102.5s，再补一拍就超 103.5s
    art = SimpleNamespace(props=[])
    logs: list[str] = []
    _repair_script_output(ep, _outline([]), art, logs.append)

    assert len(ep.scenes[-1].beats) == 1
    assert any("硬上限" in m for m in logs)


def test_repair_script_maps_illegal_beat_and_forces_final_hook():
    ep = _script_ep(prior_beats=10)
    ep.scenes[0].beat = "舒缓"          # 不在戏剧节拍表
    ep.scenes[-1].beat = "高潮"         # 末场必须是结尾钩子
    art = SimpleNamespace(props=[])
    logs: list[str] = []
    _repair_script_output(ep, _outline([]), art, logs.append)

    assert ep.scenes[0].beat == "收束"
    assert ep.scenes[-1].beat == "结尾钩子"
    assert any("舒缓" in m and "映射" in m for m in logs)


def test_repair_script_splits_overlong_dialogue():
    ep = _script_ep(prior_beats=10)
    long_line = "多萝茜，你必须沿着黄砖路一直向前走别回头"   # 20 字，含标点
    ep.scenes[0].beats = [
        ScriptBeat(speaker="C1", line=long_line, expression="急切",
                   state_out={"C1": "center/forward"}),
    ]
    art = SimpleNamespace(props=[])
    logs: list[str] = []
    _repair_script_output(ep, _outline([]), art, logs.append)

    beats = ep.scenes[0].beats
    assert len(beats) == 2
    assert "".join(b.line for b in beats) == long_line
    assert all(b.line and not b.action for b in beats)
    assert all(len(b.line) <= 18 for b in beats)
    assert all(b.speaker == "C1" and b.expression == "急切" for b in beats)
    # 状态只保留在整句说完的最后一拍
    assert beats[0].state_out == {}
    assert beats[-1].state_out == {"C1": "center/forward"}
    assert any("拆为 2 拍" in m for m in logs)


def test_repair_script_maps_invented_scene_ids_to_allowed_locations():
    """第 2 集接着编 S6/S7/S8 是引用错误，不该靠三轮重写。"""
    scenes = [ScriptScene(
        id=f"S{i}", location="河岸", characters=["C1"],
        beats=[ScriptBeat(action="多萝茜向前走。")]) for i in (6, 7, 8)]
    ep = ScriptEpisode(ep=1, title="t", target_seconds=90, hook="h", cliff="c",
                       hook_beat=(1, 1), scenes=scenes)
    art = SimpleNamespace(props=[], scenes=[
        SimpleNamespace(id="S1", name="河岸")])
    logs: list[str] = []
    _repair_script_output(ep, _outline([]), art, logs.append)

    assert [s.id for s in ep.scenes] == ["S1", "S1", "S1"]
    assert all(s.location == "河岸" for s in ep.scenes)
    assert any("S6" in m and "映射为 S1" in m for m in logs)


def test_repair_script_splits_line_that_cannot_fit_one_shot():
    """28 字 = 6.2s > 单镜上限 6.0s；不拆就只有"永远过不了"的分镜门。"""
    ep = _script_ep(prior_beats=10)
    line = "托托，别跑，龙卷风就要来了呀，我们得赶快躲进地窖里才对！"  # 28 字，6.2s
    ep.scenes[0].beats = [
        ScriptBeat(speaker="C1", line=line, expression="焦急",
                   state_out={"C1": "center/forward"}),
    ]
    logs: list[str] = []
    _repair_script_output(ep, _outline([]), SimpleNamespace(props=[]), logs.append)

    beats = ep.scenes[0].beats
    assert len(beats) == 2
    assert "".join(b.line for b in beats) == line
    # 每段都能装进一个 6.0s 切镜（≤27 字）
    assert all(len("".join(b.line.split())) <= 27 for b in beats)
    assert any("超过单镜" in m and "拆为 2 拍" in m for m in logs)


def test_repair_segment_splits_grouped_cuts_and_normalizes_durations():
    """8.70s≠8.33s、3 镜<4 都是算得出来的错，不该靠回喂重写。"""
    scene = ScriptScene(
        id="S1", characters=["C1"], props=[],
        beats=[
            ScriptBeat(action="多萝茜望向云层。"),          # 2.5s
            ScriptBeat(action="托托跳到地面。"),            # 2.5s
            ScriptBeat(speaker="C1", line="托托，别跑，龙"),   # 7 字
            ScriptBeat(speaker="C1", line="卷风就要来了呀！"),  # 8 字
        ])
    cuts = [
        StoryCut(beats=(1, 1), seconds=3.0, size="medium",
                 frame="x", prompt_en="x"),
        StoryCut(beats=(2, 3), seconds=3.2, size="medium",
                 frame="x", prompt_en="x"),
        StoryCut(beats=(4, 4), seconds=2.5, size="medium",
                 frame="x", prompt_en="x"),
    ]
    seg = StorySegment(id="E01-01", scene_index=1, cuts=cuts)
    logs: list[str] = []
    _repair_segment_output(seg, scene, logs.append)

    assert [c.beats for c in seg.cuts] == [(1, 1), (2, 2), (3, 3), (4, 4)]
    assert all(1.0 <= c.seconds <= 3.2 for c in seg.cuts)
    # 动作拍 2.5 + 2.5 + 7/4.5 + 8/4.5 = 8.334
    assert abs(sum(c.seconds for c in seg.cuts) - 8.334) < 0.02
    assert any("拆分" in m for m in logs)
    assert any("时长归一" in m for m in logs)


def test_split_long_segments_keeps_scene_and_beats_intact():
    """20s 长场不可能压进 15s；按镜拆段后场次/节拍/总时长都不变。"""
    cuts = [StoryCut(beats=(i, i), seconds=2.5, size="medium",
                     frame="x", prompt_en="x") for i in range(1, 9)]
    seg = StorySegment(id="E01-01", scene_index=1, cuts=cuts)
    logs: list[str] = []
    out = _split_long_segments([seg], 1, logs.append)

    assert len(out) >= 2
    assert all(s.scene_index == 1 for s in out)
    assert [c.beats for s in out for c in s.cuts] == [(i, i) for i in range(1, 9)]
    assert all(sum(c.seconds for c in s.cuts) <= 15.0 + 1e-6 for s in out)
    assert abs(sum(c.seconds for s in out for c in s.cuts) - 20.0) < 1e-6
    assert len({s.id for s in out}) == len(out)
    assert all(re.fullmatch(r"E01-\d{2}", s.id) for s in out)
    assert any("长" in m and "拆" in m for m in logs)


def test_repair_script_cleans_dangling_references():
    ep = _script_ep(prior_beats=10)
    sc = ep.scenes[0]
    sc.characters = ["C9"]
    sc.props = ["P9"]
    sc.wardrobe = {"C1": "C1-bad"}
    sc.callbacks = ["回指 C9 的旧事", "回指 C1 的旧事"]
    sc.restructure = {"action": "merge"}          # 缺 reason/from/to
    sc.beats = [
        ScriptBeat(action="C2 推门进来。",
                   state_change={"C9": "接过剑"}),
        ScriptBeat(speaker="C1", line="我来了。"),
    ]
    ep.beats_claimed = ["B9"]

    cast = SimpleNamespace(characters=[SimpleNamespace(
        id="C1", wardrobe_states=[SimpleNamespace(state_id="C1-default")])])
    logs: list[str] = []
    _repair_script_output(ep, _outline([]), SimpleNamespace(props=[]),
                          logs.append, cast)

    assert sc.characters == ["C1"]                # C9 删除；C1 因说话/动作补登记
    assert sc.props == []
    assert sc.wardrobe == {}
    assert sc.callbacks == ["回指 C1 的旧事"]
    assert sc.restructure == {}
    assert sc.beats[0].state_change == {}
    assert ep.beats_claimed == []
    assert any("悬空引用清理完成" in m for m in logs)


def test_repair_script_normalizes_invalid_hook_beat():
    ep = _script_ep(prior_beats=10)
    ep.hook_beat = (9, 9)
    logs: list[str] = []
    _repair_script_output(ep, _outline([]), SimpleNamespace(props=[]), logs.append)

    assert ep.hook_beat == (1, 1)
    assert any("hook_beat" in m and "FIX" in m for m in logs)

    # 合法位置但超过前 3 拍 → 同样归一
    ep2 = _script_ep(prior_beats=10)
    ep2.hook_beat = (1, 4)
    _repair_script_output(ep2, _outline([]), SimpleNamespace(props=[]),
                          lambda _m: None)
    assert ep2.hook_beat == (1, 1)


def test_cliff_action_text_falls_back_for_unfilmable_cliff():
    assert _cliff_action_text("银鞋突然自行发光，黄砖路尽头暗影一闪。", "F") \
        .startswith("银鞋突然自行发光")
    assert _cliff_action_text("她心里害怕极了。", "FALLBACK") == "FALLBACK"
    long_cliff = "多萝茜" * 30
    assert _cliff_action_text(long_cliff, "FALLBACK") == "FALLBACK"


def test_clean_workdir_removes_whole_temp_out(tmp_path):
    from manju.story_cli import clean_workdir

    work = tmp_path / ".work_demo"
    work.mkdir()
    (work / "outline.json").write_text("{}", encoding="utf-8")
    (work / "notes.txt").write_text("x", encoding="utf-8")

    out = tmp_path / ".work_demo_out"
    (out / "external").mkdir(parents=True)
    (out / "external" / "plan.json").write_text("{}", encoding="utf-8")
    (out / "storyboard_ep01.json").write_text("{}", encoding="utf-8")

    final = tmp_path / "demo"
    final.mkdir()
    (final / "outline.json").write_text("{}", encoding="utf-8")

    logs: list[str] = []
    assert clean_workdir(work, out, yes=True, _out=logs.append) is True
    # 临时工作层被清空，但目录本身保留（re-run 还会写它）
    assert work.is_dir() and list(work.iterdir()) == []
    # 整个临时输出目录被删（旧 epNN 不会混进固化目录）
    assert not out.exists()
    # 成品目录永不被 --clean 触碰
    assert (final / "outline.json").is_file()


def test_clean_workdir_non_temp_out_only_removes_external(tmp_path):
    from manju.story_cli import clean_workdir

    work = tmp_path / "work"
    work.mkdir()
    (work / "outline.json").write_text("{}", encoding="utf-8")
    out = tmp_path / "demo"
    (out / "external").mkdir(parents=True)
    (out / "keep.json").write_text("{}", encoding="utf-8")

    assert clean_workdir(work, out, yes=True, _out=lambda _m: None) is True
    assert not (out / "external").exists()
    assert (out / "keep.json").is_file()  # 自定义 out 时只清 external


def test_prune_unused_props_noop_when_all_used(tmp_path):
    o = _outline(["P1", "P2"])
    logs: list[str] = []
    assert _prune_unused_props(o, logs.append) == []
    assert [p.id for p in o.props] == ["P1", "P2"]
    assert logs == []
