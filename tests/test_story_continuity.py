# -*- coding: utf-8 -*-
"""连续性硬校验：state_out 跨镜/跨场/跨集。"""
from __future__ import annotations

from manju.story.continuity import (
    repair_gaze, validate_adjacent, validate_board_continuity, validate_cut_state,
    validate_handoff, validate_segment,
)
from manju.story.editorial import validate_editing
from manju.story.models import (
    Cast, CastCharacter, CutState, CutSubject, ImageSpec, Persona, ScriptBeat,
    ScriptEpisode, ScriptScene, StoryCut, StoryEpisodeBoard, StorySegment,
    WardrobeState,
)
from manju.story.validate import validate_segment_cuts


def _cast() -> Cast:
    def person(cid: str, name: str) -> CastCharacter:
        return CastCharacter(
            id=cid, name=name,
            wardrobe_states=[
                WardrobeState(state_id=f"{cid}-default", desc_en="grey robe"),
                WardrobeState(state_id=f"{cid}-injured", desc_en="torn robe"),
            ],
            image=ImageSpec(prompt="a young villager"),
        )
    return Cast(source="x", characters=[person("C1", "阿禾"),
                                        person("C2", "陆远")])


def _dog_cast() -> Cast:
    def person(cid: str, name: str, identity: str, tags: list[str]) -> CastCharacter:
        return CastCharacter(
            id=cid, name=name, persona=Persona(identity=identity),
            wardrobe_states=[WardrobeState(state_id=f"{cid}-default",
                                           desc_en="default")],
            image=ImageSpec(prompt="x", tags=tags),
        )
    return Cast(source="x", characters=[
        person("C1", "多萝茜", "堪萨斯农场少女", ["girl"]),
        person("C2", "托托", "宠物狗", ["dog"]),
    ])


def _scene(beats: int = 4) -> ScriptScene:
    return ScriptScene(
        id="S1", characters=["C1", "C2"], props=["P1"],
        wardrobe={"C1": "C1-default", "C2": "C2-default"},
        beats=[ScriptBeat(action="阿禾抬手推开门，雾漫进来。")
               for _ in range(beats)])


def _state(*chars: str, where: dict[str, str] | None = None,
           facing: dict[str, str] | None = None,
           wardrobe: dict[str, str] | None = None,
           props: dict[str, str] | None = None,
           injuries: dict[str, str] | None = None) -> CutState:
    chars = chars or ("C1",)
    return CutState(
        where=where or {c: "north" for c in chars},
        facing=facing or {c: "south" for c in chars},
        posture={c: "stand" for c in chars},
        wardrobe=wardrobe or {c: f"{c}-default" for c in chars},
        props=props or {},
        injuries=injuries or {},
    )


def _cut(*, beats=(1, 1), size="medium", chars=("C1",),
         reason="action_done", coverage="action",
         state: CutState | None = None) -> StoryCut:
    return StoryCut(
        beats=beats, seconds=2.0, size=size, characters=list(chars),
        frame="阿禾站在门口", prompt_en="a young woman stands in the doorway",
        coverage=coverage, cut_reason=reason,
        state_out=state if state is not None else _state(*chars),
    )


# --------------------------------------------------------------------------
# 相邻镜
# --------------------------------------------------------------------------
def test_position_jump_needs_reason():
    a = _cut(state=_state("C1", where={"C1": "north"}))
    b = _cut(beats=(2, 2), state=_state("C1", where={"C1": "south"}))
    assert any("位置从 north 跳到 south" in e for e in validate_adjacent(a, b))
    b.cut_reason = "space"
    assert not validate_adjacent(a, b)


def test_wardrobe_rollback_needs_reason():
    a = _cut(state=_state("C1", wardrobe={"C1": "C1-injured"}))
    b = _cut(beats=(2, 2), state=_state("C1", wardrobe={"C1": "C1-default"}))
    assert any("服装" in e and "换装" in e for e in validate_adjacent(a, b))
    b.cut_reason = "time"
    assert not validate_adjacent(a, b)


def _wardrobe_scene() -> ScriptScene:
    return ScriptScene(
        id="S1", characters=["C1"], wardrobe={"C1": "C1-default"},
        beats=[
            ScriptBeat(action="阿禾推开门。"),
            ScriptBeat(
                action="阿禾换上伤者的外套。",
                state_change={"C1": "换装，状态转 C1-injured"},
                state_out={"C1": "center/north/C1-injured/无伤"}),
        ])


def test_script_authorized_wardrobe_change_is_allowed():
    """剧本 state_change/state_out 已授权的换装，不再要求 space/time。"""
    scene = _wardrobe_scene()
    a = _cut(beats=(1, 1), state=_state("C1", wardrobe={"C1": "C1-default"}))
    b = _cut(beats=(2, 2), state=_state("C1", wardrobe={"C1": "C1-injured"}))
    assert not any("换装" in e
                   for e in validate_adjacent(a, b, scene=scene))


def test_unscripted_wardrobe_change_is_still_blocked():
    scene = _wardrobe_scene()
    scene.beats[1].state_change = {}
    scene.beats[1].state_out = {}
    a = _cut(beats=(1, 1), state=_state("C1", wardrobe={"C1": "C1-default"}))
    b = _cut(beats=(2, 2), state=_state("C1", wardrobe={"C1": "C1-injured"}))
    assert any("换装" in e for e in validate_adjacent(a, b, scene=scene))


def test_scene_start_wardrobe_can_use_scene_snapshot():
    """新场按场级 wardrobe 快照换装，是剧本声明，不是无据跳变。"""
    scene = ScriptScene(
        id="S2", characters=["C1"], wardrobe={"C1": "C1-injured"},
        beats=[ScriptBeat(action="阿禾走进雨里。")])
    a = _cut(state=_state("C1", wardrobe={"C1": "C1-default"}))
    b = _cut(beats=(1, 1), state=_state("C1", wardrobe={"C1": "C1-injured"}))
    assert not any("换装" in e
                   for e in validate_adjacent(a, b, scene=scene))


def test_cut_wardrobe_must_match_beat_ledger():
    scene = _wardrobe_scene()
    early = _cut(beats=(1, 1), state=_state("C1", wardrobe={"C1": "C1-injured"}))
    assert any("剧本状态应为 C1-default" in e
               for e in validate_cut_state(early, scene, _cast()))
    after = _cut(beats=(2, 2), state=_state("C1", wardrobe={"C1": "C1-injured"}))
    assert not validate_cut_state(after, scene, _cast())


def test_prop_transfer_must_be_visible():
    a = _cut(chars=("C1",), state=_state("C1", props={"P1": "C1"}))
    b = _cut(beats=(2, 2), chars=("C2",),
             state=_state("C2", props={"P1": "C2"}))
    assert any("交接不可见" in e for e in validate_adjacent(a, b))


def test_prop_cannot_vanish():
    a = _cut(chars=("C1",), state=_state("C1", props={"P1": "C1"}))
    b = _cut(beats=(2, 2), chars=("C1",), state=_state("C1"))
    assert any("消失" in e for e in validate_adjacent(a, b))


def test_injury_cannot_rollback():
    a = _cut(state=_state("C1", injuries={"C1": "left arm cut"}))
    b = _cut(beats=(2, 2), state=_state("C1"))
    assert any("凭空消失" in e for e in validate_adjacent(a, b))


def test_180_degree_flip():
    a = _cut(chars=("C1", "C2"),
             state=_state("C1", "C2", where={"C1": "north", "C2": "southeast"}))
    b = _cut(beats=(2, 2), chars=("C1", "C2"),
             state=_state("C1", "C2", where={"C1": "southeast", "C2": "north"}))
    assert any("180°" in e for e in validate_adjacent(a, b))


def test_eyeline_must_face_partner():
    a = _cut(chars=("C1", "C2"),
             state=_state("C1", "C2", where={"C1": "north", "C2": "south"}))
    b = _cut(beats=(2, 2), chars=("C1", "C2"), coverage="dialogue",
             state=_state("C1", "C2", where={"C1": "north", "C2": "south"},
                          facing={"C1": "north", "C2": "south"}))
    assert any("目线" in e for e in validate_adjacent(a, b))


def test_size_jump_needs_reason():
    a = _cut(size="extreme-wide", state=_state("C1"))
    b = _cut(beats=(2, 2), size="close", state=_state("C1"))
    assert any("景别" in e for e in validate_adjacent(a, b))
    b.cut_reason = "new_info"
    assert not validate_adjacent(a, b)


def test_scene_opening_can_reset_shot_size():
    """新场从上一场特写直接切全景是空间重建，不是场内跳切。"""
    scene = _scene()
    last = _cut(beats=(4, 4), size="extreme-close", state=_state("C1"))
    opening = _cut(beats=(1, 1), size="wide", reason="space",
                   state=_state("C1"))
    # 场内仍按原规则判错；只有标记为跨场首镜时才放行。
    assert any("景别" in e for e in validate_adjacent(last, opening))
    seg = StorySegment(id="E01-02", scene_index=1, cuts=[opening])
    errs = validate_segment(seg, scene, _cast(), prev_cuts=[last])
    assert not any("景别" in e for e in errs)


# --------------------------------------------------------------------------
# 场 / 集
# --------------------------------------------------------------------------
def test_segment_continues_state_in():
    scene = _scene()
    cut = _cut(state=_state("C1"))
    seg = StorySegment(id="E01-01", scene_index=1, cuts=[cut])
    errs = validate_segment(seg, scene, _cast(),
                            state_in=_state("C1", injuries={"C1": "cut"}))
    assert any("没有延续" in e for e in errs)


def test_segment_requires_state_out():
    scene = _scene()
    cuts = [_cut(beats=(i, i), state=CutState()) for i in range(1, 5)]
    seg = StorySegment(id="E01-01", scene_index=1, cuts=cuts)
    rep = validate_segment_cuts(seg, scene, _cast())
    assert any("没有声明 state_out" in e for e in rep.errors)


def test_legacy_board_only_warns():
    ep = ScriptEpisode(ep=1, hook="箱子落地", cliff="雾里的脚步",
                       hook_beat=(1, 1), target_seconds=10,
                       scenes=[_scene()])
    seg = StorySegment(id="E01-01", scene_index=1, cuts=[
        _cut(beats=(i, i), state=CutState()) for i in range(1, 5)])
    board = StoryEpisodeBoard(ep=1, segments=[seg])
    errs, warns = validate_board_continuity(board, ep, _cast())
    assert not errs and any("旧分镜" in w for w in warns)


def test_cross_episode_handoff():
    prev = _state("C1", props={"P1": "C1"}, injuries={"C1": "cut"})
    nxt = _state("C1")
    errs = validate_handoff(prev, nxt)
    assert any("伤势" in e for e in errs)
    assert any("道具 P1" in e for e in errs)
    assert not validate_handoff(prev, _state(
        "C1", props={"P1": "C1"}, injuries={"C1": "cut"}))


# --------------------------------------------------------------------------
# 契约可行性：不再要求"做不到的事"
# --------------------------------------------------------------------------
def _four_cut_segment(chars=("C1",), **cut_kwargs) -> StorySegment:
    cuts = []
    for i in range(1, 5):
        c = _cut(beats=(i, i), chars=chars, **cut_kwargs)
        c.seconds = 2.5
        c.subjects = [CutSubject(char=cid) for cid in chars]
        cuts.append(c)
    return StorySegment(id="E01-01", scene_index=1, cuts=cuts)


def test_face_gate_ignores_non_human_partner():
    """多萝茜 + 托托（狗）同框只有一张需要锁的人脸。"""
    scene = _scene(4)
    seg = _four_cut_segment(chars=("C1", "C2"), coverage="action")
    rep = validate_segment_cuts(seg, scene, _dog_cast())
    assert not any("正脸" in e for e in rep.errors)
    assert not any("目线" in e for e in rep.errors)


def test_min_cuts_cannot_exceed_scene_beats():
    """3 拍场次最多 3 个切镜；"至少 4 镜"必须按拍数封顶，否则永远过不了。"""
    scene = ScriptScene(
        id="S1", characters=["C1"], props=[],
        beats=[ScriptBeat(action="阿禾向前走。") for _ in range(3)])
    cuts = []
    for i in range(1, 4):
        c = _cut(beats=(i, i), chars=("C1",))
        c.seconds = 2.5
        c.subjects = [CutSubject(char="C1")]
        cuts.append(c)
    seg = StorySegment(id="E01-01", scene_index=1, cuts=cuts)
    rep = validate_segment_cuts(seg, scene, _cast())
    assert not any("至少 4" in e or "至少 4 个" in e for e in rep.errors)
    assert any("补 1 拍" in w for w in rep.warnings)


def test_offscreen_owner_is_valid_for_inherited_prop():
    """C2 特写时，C1 在画外继续拿着鞋不应被判“道具在人不在”。

    这是绿野仙踪第 2 集第 1 场的真实报错：state_out 是完整世界状态，
    继承来的道具持有者本来就可以不在当前画幅内。
    """
    scene = _scene(2)
    c1 = _cut(beats=(1, 1), chars=("C1",),
              state=_state("C1", props={"P1": "C1"}))
    c2 = _cut(beats=(2, 2), chars=("C2",),
              state=_state("C2", props={"P1": "C1"}))
    c2.state_from = c1.state_out.model_copy(deep=True)
    seg = StorySegment(id="E01-01", scene_index=1, cuts=[c1, c2])
    errs = validate_segment(seg, scene, _cast())
    assert not any("P1" in e and "不在画内" in e for e in errs)


def test_visible_prop_still_requires_owner_on_screen():
    """本镜 props 里展示的道具，持有者必须真的在画内。"""
    scene = _scene(2)
    c1 = _cut(beats=(1, 1), chars=("C1",),
              state=_state("C1", props={"P1": "C1"}))
    c2 = _cut(beats=(2, 2), chars=("C2",),
              state=_state("C2", props={"P1": "C1"}))
    c2.props = ["P1"]
    c2.state_from = c1.state_out.model_copy(deep=True)
    seg = StorySegment(id="E01-01", scene_index=1, cuts=[c1, c2])
    errs = validate_segment(seg, scene, _cast())
    assert any("P1" in e and "不在画内" in e for e in errs)


def test_gaze_repair_points_facing_at_partner():
    c = _cut(beats=(1, 1), chars=("C1", "C2"), coverage="dialogue",
             state=_state("C1", "C2",
                          where={"C1": "north", "C2": "south"},
                          facing={"C1": "north", "C2": "south"}))
    assert repair_gaze(c, _cast()) is True
    assert c.state_out.facing == {"C1": "south", "C2": "north"}
    assert not validate_adjacent(_cut(state=CutState()), c, cast=_cast())


def test_dialogue_uses_its_own_duration_floor():
    """9 字台词确定性算出 2.0s，不应被 2.2s 的动作下限误判。"""
    cut = _cut(coverage="dialogue")
    cut.seconds = 2.0
    errs, warns = validate_editing([cut], None)
    assert not errs and not any("动作表达不完整" in w for w in warns)

    action = _cut(coverage="action")
    action.seconds = 2.0
    _, warns = validate_editing([action], None)
    assert any("动作表达不完整" in w for w in warns)


def test_eyeline_allows_co_directional_chase():
    """两人同向追跑不是目线失误；双人对话仍须对视（见上一个用例）。"""
    a = _cut(chars=("C1", "C2"),
             state=_state("C1", "C2", facing={"C1": "west", "C2": "west"}))
    b = _cut(beats=(2, 2), chars=("C1", "C2"), coverage="dialogue",
             state=_state("C1", "C2", facing={"C1": "west", "C2": "west"}))
    assert not any("目线" in e
                   for e in validate_adjacent(a, b, cast=_cast()))
