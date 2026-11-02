# -*- coding: utf-8 -*-
"""B+D：EDL 一镜一拍归一化 + 分镜字段确定性推导。"""
from __future__ import annotations

from manju.story.models import (
    ActionBeat, ActionBeatLine, Cast, CastCharacter, CutPlan, CutPrompt,
    CutState, ImageSpec, ScriptBeat, ScriptParams, ScriptScene, SegmentPlan,
    WardrobeState, _beat_seconds,
)
from manju.story.stages import (
    _assemble_segment, _normalize_edl_plan, _normalize_plan_units,
    _repair_segment_output,
)
from manju.story.validate import validate_segment_cuts


def _scene() -> ScriptScene:
    return ScriptScene(
        id="S1", location="河岸", lighting="cold blue dawn",
        characters=["C1"], props=["P1"], information="银鞋会发光",
        beats=[
            ScriptBeat(action="多萝茜抬头看天。", coverage="establish",
                       state_from="站在门口", screen_side="left"),
            ScriptBeat(action="银鞋在泥地上发出微光。", coverage="insert",
                       state_from="她低头", screen_side="center"),
            ScriptBeat(speaker="C1", line="我们走。", coverage="reaction",
                       expression="坚定", state_from="她抬起脚",
                       screen_side="right"),
        ])


def _line() -> ActionBeatLine:
    return ActionBeatLine(scene_index=1, beats=[
        ActionBeat(beats=(1, 1), goal="交代空间", from_prev="门口",
                   action="多萝茜抬头", to_next="看到天空",
                   who="C1", where="center", facing="north",
                   screen_side="left"),
        ActionBeat(beats=(2, 2), goal="道具亮相", from_prev="她低头",
                   action="银鞋发光", to_next="她抬起脚",
                   who="", where="center", facing="north",
                   screen_side="center"),
        ActionBeat(beats=(3, 3), goal="做出决定", from_prev="她抬起脚",
                   action="多萝茜开口", to_next="向前走",
                   who="C1", where="east", facing="west",
                   screen_side="right"),
    ])


def _plan() -> SegmentPlan:
    return SegmentPlan(scene_index=1, cuts=[
        CutPlan(size="wide", camera="Static Shot"),
        CutPlan(size="close", camera="Push In", props=["P1"]),
        CutPlan(size="medium", camera="Static Shot",
                characters=["C1"], cut_reason="reaction",
                state_out={"wardrobe": {"C1": "C1-default"}}),
    ])


def _cast() -> Cast:
    return Cast(source="fixture", characters=[CastCharacter(
        id="C1", name="多萝西",
        wardrobe_states=[
            WardrobeState(state_id="C1-default",
                          desc_en="gray gingham dress"),
            WardrobeState(state_id="C1-injured",
                          desc_en="torn gray gingham dress"),
        ],
        image=ImageSpec(prompt="a young woman in a gray dress"))])


def _prompts() -> list[CutPrompt]:
    return [
        CutPrompt(prompt_en="a young woman looks up at the cold dawn sky, "
                            "standing at the riverbank",
                  motion="she slowly raises her chin"),
        CutPrompt(prompt_en="silver shoes glowing faintly on wet mud, "
                            "close detail, cold blue dawn light",
                  motion=""),
        CutPrompt(prompt_en="a determined young woman in a gray coat faces west "
                            "by the river, cold blue dawn light",
                  motion="she steps forward"),
    ]


def test_normalize_plan_units_fills_omitted_beats_and_expands_ranges():
    scene = _scene()
    plan = SegmentPlan(scene_index=1, cuts=[
        CutPlan(size="wide"),
        CutPlan(size="close", beats=(2, 2)),
        CutPlan(size="medium", beats=(3, 4)),
    ])
    units = _normalize_plan_units(plan, scene)
    assert [i for i, _ in units] == [1, 2, 3, 4]
    assert [cp.size for _, cp in units] == ["wide", "close", "medium", "medium"]


def test_assemble_segment_derives_every_non_edl_field():
    scene, line, plan = _scene(), _line(), _plan()
    seg = _assemble_segment(plan, scene, line, CutState(), _prompts(),
                            ScriptParams(), seg_id="E01-01", scene_index=1,
                            cast=_cast())

    assert [c.beats for c in seg.cuts] == [(1, 1), (2, 2), (3, 3)]
    assert [c.coverage for c in seg.cuts] == ["establish", "insert", "reaction"]
    assert [round(c.seconds, 3) for c in seg.cuts] == [
        round(_beat_seconds(b, ScriptParams()), 3) for b in scene.beats]
    assert seg.cuts[0].frame.startswith("空间关系")
    assert seg.cuts[1].frame.startswith("细节")

    # 省略 characters 时由动作线主体/场次人物推导，EDL 不必重复输出
    assert seg.cuts[0].characters == ["C1"]
    assert seg.cuts[1].characters == ["C1"]
    assert seg.cuts[1].props == ["P1"]

    for c in seg.cuts:
        assert c.purpose and c.information and c.beat_goal
        assert c.lens and c.camera_position and c.stability
        assert c.state_out.where.get("C1")
        assert c.state_out.facing.get("C1")
        assert c.state_out.posture.get("C1")
        assert c.state_out.wardrobe.get("C1") == "C1-default"
        assert {s.char for s in c.subjects} >= set(c.characters)

    # 空 motion 由代码兜底；state_from 严格等于上一镜 state_out
    assert seg.cuts[1].motion == "continuous natural action"
    assert seg.cuts[1].state_from == seg.cuts[0].state_out
    assert seg.cuts[2].state_from == seg.cuts[1].state_out
    assert seg.cuts[1].state_out.props.get("P1") == "scene"


def test_person_name_in_characters_falls_back_instead_of_crashing():
    """EDL 把中文人名写进 characters 时不能抛 ValidationError。

    真实故障：CutPlan.characters 不校验 ^C\\d+$，人名原样传给
    CutSubject(char=...) 才炸，且发生在质量门之外，整场直接失败。
    """
    scene, line, plan = _scene(), _line(), _plan()
    plan.cuts[1].characters = ["多萝西"]          # 不是 Cid
    plan.cuts[2].characters = ["C1", "多萝西"]     # 混合：保留合法 Cid

    seg = _assemble_segment(plan, scene, line, CutState(), _prompts(),
                            ScriptParams(), seg_id="E01-01", scene_index=1,
                            cast=_cast())

    assert seg.cuts[1].characters == ["C1"]        # 退回动作线/场次主体
    assert seg.cuts[2].characters == ["C1"]
    assert all(c.char.startswith("C") for c in seg.cuts[1].subjects)


def test_assemble_segment_enforces_script_wardrobe_change():
    """剧本拍级账本写了换装，EDL 漏写或写回旧服装时必须被覆盖。

    真实故障（绿野仙踪 S3→S4）：剧本第 5 拍已声明 C1-arrival → C1-default，
    EDL 仍沿用 C1-arrival，下一场开场按剧本写 C1-default，跨场校验就报
    “没有换装理由”。这里必须由装配层按 state_change/state_out 快照落地。
    """
    scene = _scene()
    scene.wardrobe = {"C1": "C1-default"}
    scene.beats[1].state_change = {"C1": "换装，状态转 C1-injured"}
    scene.beats[1].state_out = {"C1": "center/north/C1-injured/无伤"}
    scene.beats[2].state_out = {"C1": "east/west/C1-injured/无伤"}

    plan = _plan()
    plan.cuts[1].state_out = {}                                  # EDL 漏写
    plan.cuts[2].state_out = {"wardrobe": {"C1": "C1-default"}}  # EDL 写回旧装

    seg = _assemble_segment(plan, scene, _line(), CutState(), _prompts(),
                            ScriptParams(), seg_id="E01-01", scene_index=1,
                            cast=_cast())

    assert seg.cuts[1].state_out.wardrobe["C1"] == "C1-injured"
    assert seg.cuts[2].state_out.wardrobe["C1"] == "C1-injured"

    rep = validate_segment_cuts(seg, scene, _cast(), state_in=CutState(),
                                prev_cuts=[])
    assert not any("服装" in e for e in rep.errors), rep.errors


def test_repair_cached_segment_normalizes_invalid_wardrobe_and_unknown_state():
    """缓存里的旧分镜可能带非法服装态/未知角色键；装配后修复必须清掉。"""
    scene, line, plan = _scene(), _line(), _plan()
    seg = _assemble_segment(plan, scene, line, CutState(), _prompts(),
                            ScriptParams(), seg_id="E01-01", scene_index=1,
                            cast=_cast())
    seg.cuts[0].state_out.wardrobe["C1"] = "C1-nope"
    seg.cuts[0].state_out.where["C99"] = "north"

    _repair_segment_output(seg, scene, lambda _msg: None,
                           params=ScriptParams(), cast=_cast())

    assert seg.cuts[0].state_out.wardrobe["C1"] == "C1-default"
    assert "C99" not in seg.cuts[0].state_out.where
    rep = validate_segment_cuts(seg, scene, _cast(), state_in=CutState(),
                                prev_cuts=[])
    assert not any("不在角色状态库" in e or "未知角色" in e
                   for e in rep.errors), rep.errors


def test_assemble_segment_output_passes_scene_gate():
    scene, line, plan = _scene(), _line(), _plan()
    seg = _assemble_segment(plan, scene, line, CutState(), _prompts(),
                            ScriptParams(), seg_id="E01-01", scene_index=1,
                            cast=_cast())
    rep = validate_segment_cuts(seg, scene, _cast(), state_in=CutState(),
                                prev_cuts=[])
    assert rep.ok, rep.text()


def test_repair_drops_out_of_scene_prop_not_in_inherited_state():
    """真实故障（绿野仙踪 EP2 S4）：本场只有 P5，EDL 却在 C1 反应镜里
    塞入 P2 并写成 scene→C1。场次道具表是权限边界，必须剔除。
    """
    scene, line, plan = _scene(), _line(), _plan()
    plan.cuts[1].props = ["P2"]
    plan.cuts[1].state_out = {"props": {"P2": "C1"}}

    seg = _assemble_segment(plan, scene, line, CutState(), _prompts(),
                            ScriptParams(), seg_id="E01-01", scene_index=1,
                            cast=_cast())
    _repair_segment_output(seg, scene, lambda _msg: None,
                           params=ScriptParams(), cast=_cast())

    assert "P2" not in seg.cuts[1].props
    assert "P2" not in seg.cuts[1].state_out.props
    rep = validate_segment_cuts(seg, scene, _cast(), state_in=CutState(),
                                prev_cuts=[])
    assert not any("越出本场道具" in e for e in rep.errors), rep.errors
    assert not any("交接不可见" in e for e in rep.errors), rep.errors


def test_normalize_edl_plan_fills_missing_beats_and_expands_ranges():
    """EDL 漏拍/多拍是确定性错误，先在装配前补齐，不整段重写。"""
    scene = _scene()
    plan = SegmentPlan(scene_index=1, cuts=[
        CutPlan(size="wide"),                       # 顺序隐式 = 第 1 拍
        CutPlan(size="extreme-close", beats=(2, 3)),
    ])
    _normalize_edl_plan(plan, scene, _cast(), None)

    assert [c.beats for c in plan.cuts] == [(1, 1), (2, 2), (3, 3)]
    assert [c.size for c in plan.cuts] == ["wide", "extreme-close",
                                           "extreme-close"]


def test_normalize_edl_plan_filters_unknown_refs_and_invalid_state():
    scene = _scene()
    plan = SegmentPlan(scene_index=1, cuts=[CutPlan(
        beats=(1, 1), size="medium",
        characters=["多萝西", "C9"],
        props=["P1", "P9"],
        state_out={
            "where": {"C1": "north", "C9": "south"},
            "wardrobe": {"C1": "C1-nope", "C9": "C9-default"},
            "props": {"P1": "C9", "P9": "C1"},
        },
    )])
    state_in = CutState(props={"P2": "C1"})
    _normalize_edl_plan(plan, scene, _cast(), None,
                        state_in=state_in)

    cut = plan.cuts[0]
    assert cut.characters == ["C1"]                 # 人名映射 + 未知剔除
    assert cut.props == ["P1"]                      # 场外道具不展示
    assert cut.state_out["where"] == {"C1": "north"}  # 未知角色键剔除
    assert cut.state_out.get("wardrobe", {}) == {}   # 非法服装态删除
    assert cut.state_out.get("props", {}) == {"P1": "C1"}  # 非法持有者归到在场角色


def test_normalize_edl_plan_locks_inherited_offscreen_prop():
    """已继承的场外道具不能在本场视线外易主，也不能被丢掉。"""
    scene = _scene()
    plan = SegmentPlan(scene_index=1, cuts=[CutPlan(
        beats=(1, 1), size="medium",
        state_out={"props": {"P2": "C1"}},
    )])
    state_in = CutState(props={"P2": "C1"})
    _normalize_edl_plan(plan, scene, _cast(), None, state_in=state_in)

    assert plan.cuts[0].state_out.get("props", {}) == {"P2": "C1"}


def test_normalize_edl_plan_does_not_move_prop_the_cut_does_not_show():
    """真实故障（绿野仙踪 EP2 S5）：画外道具不能在本镜易主。

    上一场把 P2 错误地留在 scene，下一场模型顺着写 scene→C4；但本镜既没
    带 P2，cut_reason 也不是 action_done，质量门判“交接不可见”后整场重写。
    """
    scene = _scene()
    scene.props = ["P1", "P2"]
    scene.characters = ["C1", "C4"]
    cast = _cast()
    cast.characters.append(CastCharacter(
        id="C4", name="稻草人",
        wardrobe_states=[WardrobeState(state_id="C4-default",
                                       desc_en="straw man")],
        image=ImageSpec(prompt="a straw man")))

    plan = SegmentPlan(scene_index=1, cuts=[
        CutPlan(beats=(1, 1), size="wide", characters=["C1"],
                props=["P1", "P2"], cut_reason="action_done",
                state_out={"props": {"P2": "C1"}}),
        CutPlan(beats=(2, 2), size="close", characters=["C4"],
                props=[], cut_reason="reaction",
                state_out={"props": {"P2": "C4"}}),   # 本镜看不见 P2
    ])
    _normalize_edl_plan(plan, scene, cast, None)

    assert plan.cuts[0].state_out["props"]["P2"] == "C1"
    assert plan.cuts[1].state_out["props"]["P2"] == "C1"   # 抹平画外易主


def test_normalize_edl_plan_marks_visible_pickup_as_action_done():
    """同一镜里同时出现道具和新持有者时，交接必须被认作动作完成。"""
    scene = _scene()
    scene.props = ["P1", "P2"]
    scene.characters = ["C1"]
    plan = SegmentPlan(scene_index=1, cuts=[CutPlan(
        beats=(1, 1), size="close", characters=["C1"], props=["P2"],
        cut_reason="reaction", state_out={"props": {"P2": "C1"}})])

    _normalize_edl_plan(plan, scene, _cast(), None)

    assert plan.cuts[0].state_out["props"]["P2"] == "C1"
    assert plan.cuts[0].cut_reason == "action_done"


def test_invisible_prop_handoff_does_not_fail_segment_gate():
    """归一后整场可过门：不再因画外 scene→C4 触发“交接不可见”。"""
    scene = _scene()
    scene.props = ["P1", "P2"]
    scene.characters = ["C1", "C4"]
    scene.beats[1] = ScriptBeat(
        speaker="C4", line="把篮子给我。", coverage="dialogue",
        state_from="稻草人伸出手", screen_side="center")
    cast = _cast()
    cast.characters.append(CastCharacter(
        id="C4", name="稻草人",
        wardrobe_states=[WardrobeState(state_id="C4-default",
                                       desc_en="straw man")],
        image=ImageSpec(prompt="a straw man")))

    plan = SegmentPlan(scene_index=1, cuts=[
        CutPlan(beats=(1, 1), size="wide", characters=["C1"],
                props=["P1", "P2"], state_out={"props": {"P2": "C1"}}),
        CutPlan(beats=(2, 2), size="close", characters=["C4"],
                props=[], cut_reason="reaction",
                state_out={"props": {"P2": "C4"}}),
        CutPlan(beats=(3, 3), size="medium", characters=["C4"],
                props=[], cut_reason="reaction"),
    ])
    _normalize_edl_plan(plan, scene, cast, None)
    seg = _assemble_segment(plan, scene, _line(), CutState(), _prompts(),
                            ScriptParams(), seg_id="E01-01", scene_index=1,
                            cast=cast)
    _repair_segment_output(seg, scene, lambda _msg: None,
                           params=ScriptParams(), cast=cast)

    rep = validate_segment_cuts(seg, scene, cast, state_in=CutState(),
                                prev_cuts=[])
    assert not any("交接不可见" in e for e in rep.errors), rep.errors
    assert all(c.state_out.props.get("P2") == "C1" for c in seg.cuts)


def test_repair_keeps_inherited_offscreen_prop_owner():
    """场外但已继承的道具不能凭空消失；本场看不到交接，就保持原归属。"""
    scene, line, plan = _scene(), _line(), _plan()
    plan.cuts[0].props = ["P2"]
    plan.cuts[0].state_out = {"props": {"P2": "scene"}}
    state_in = CutState(props={"P2": "C1"})

    seg = _assemble_segment(plan, scene, line, state_in, _prompts(),
                            ScriptParams(), seg_id="E01-01", scene_index=1,
                            cast=_cast())
    _repair_segment_output(seg, scene, lambda _msg: None,
                           params=ScriptParams(), cast=_cast())

    assert "P2" not in seg.cuts[0].props
    assert seg.cuts[0].state_out.props["P2"] == "C1"
    assert seg.cuts[-1].state_out.props["P2"] == "C1"
    rep = validate_segment_cuts(seg, scene, _cast(), state_in=state_in,
                                prev_cuts=[])
    assert not any("P2" in e for e in rep.errors), rep.errors
