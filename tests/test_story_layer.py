# -*- coding: utf-8 -*-
"""故事层：确定性质量门 + v10.1 编译 + 下游 gate/drama 审计。

用一部合成短片把“小说之后”的全部结构跑通，不调用 LLM、不碰 GPU。
"""
from __future__ import annotations

import json
import math
import os

import pytest
from pydantic import BaseModel

from manju.story.compile import compile_all, compile_plan, _fallback_segments
from manju.story.continuity import validate_adjacent
from manju.story.models import (
    Anchor, Art, ArtImage, ArtProp, ArtScene, Beat, Cast, CastCharacter,
    CutState, CutSubject, EpisodePlan, IdentityLock, ImageSpec, LightingState,
    Outline, OutlineCharacter, OutlineProp, OutlineScene, PowerRule,
    PropState, ScriptBeat, ScriptEpisode, ScriptScene, ScriptParams,
    Spine, Staging, StagingCast, StagingItem, StagingLocation, StagingPlace,
    StagingScene, StoryCut, StoryEpisodeBoard, StorySegment, StoryStoryboard,
    StoryParams, VoiceSpec, WardrobeState, face_lock_required,
)
from manju.schemas import Plan
from manju.story.stages import (
    StoryPipeline, _art_rules, _board_rules, _cast_rules, _prompt_rules,
    _demote_unused_primary_scenes, _merge_stagings, _repair_script_coverage,
    _repair_segment_output, _repair_staging_output, _script_budget,
    params_from_cfg,
)
from manju.story.validate import (
    GateReport, validate_all, validate_bible, validate_board, validate_outline,
    validate_script, validate_segment_cuts, validate_staging,
)

DIALOGUES = ["我们得走了", "船还等在岸边", "你放下箱子",
             "别回头继续走", "雾里有人在看", "天亮前过河",
             "把东西还他们", "这一次我不逃"]


def _fixture():
    chars = [
        OutlineCharacter(id="C1", name="阿禾", role="主角", tier="lead",
                         identity="摆渡人的女儿", appearance="灰布外套，短发",
                         personality=["克制", "倔强"], voice_hint="低沉克制"),
        OutlineCharacter(id="C2", name="陆远", role="对手", tier="support",
                         identity="巡夜人", appearance="黑色雨衣，高个",
                         personality=["冷静"], voice_hint="冷硬"),
    ]
    scenes = [OutlineScene(id=f"S{i}", name=f"河岸空间{i}", primary=i <= 4,
                           summary="雾中的河岸") for i in range(1, 9)]
    props = [OutlineProp(id="P1", name="旧皮箱", function="装着全剧的秘密",
                         scale="手持级")]
    beats = [
        Beat(id="B1", type="悬念", weight="major", episode=1,
             setup="箱子里是什么", payoff="箱子被打开"),
        Beat(id="B2", type="转折", weight="minor", episode=1,
             setup="陆远拦路", payoff="阿禾说出真相"),
        Beat(id="B3", type="收束", weight="minor", episode=1,
             setup="船离岸", payoff="雾里出现第三种脚步"),
    ]
    outline = Outline(
        source="fixture", title="渡口", logline="雾夜渡口，一个女孩用一只旧皮箱换回全船人的命。",
        genre="悬疑短剧", audience="短剧观众",
        adaptation={"core": "真相与代价", "keep": [{"what": "渡口", "why": "主场景"}],
                    "cut": [], "merge": [], "risks": []},
        spine=Spine(want="带箱子过河", obstacle="巡夜人拦截", choice="交出真相",
                    cost="失去庇护", turn="箱子里的东西不是钱", hook="雾里还有第三个人"),
        power_rule=PowerRule(name="看穿谎言", sees="别人说话的破绽",
                             cannot="不能证明真相", cost="每看穿一次就头痛"),
        params=StoryParams(episodes=1, target_seconds=90, aspect_ratio="9:16"),
        characters=chars, scenes=scenes, props=props, beats=beats,
        episodes=[EpisodePlan(
            ep=1, title="雾夜渡口",
            synopsis="阿禾提着旧皮箱来到渡口，被陆远拦下；她交出箱子，却换来更大的危险。",
            hook="箱子里的东西让巡夜人后退半步", cliff="雾里出现第三个人的脚步",
            scene_ids=[s.id for s in scenes], character_ids=["C1", "C2"],
            prop_ids=["P1"], crowd_plan="背景两名过河客，不参与对话")],
    )

    cast = Cast(source="fixture", summary="两名主要角色", characters=[
        CastCharacter(
            id="C1", name="阿禾", importance="protagonist", one_liner="摆渡人的女儿",
            persona={"gender": "女", "age_range": "18-22", "identity": "摆渡人",
                     "appearance": "灰布外套，短发", "personality": ["克制"]},
            identity_lock=IdentityLock(age_range="18-22", face_shape="窄长脸",
                                       hair="黑色齐耳短发", eyes="深褐色",
                                       build="瘦削", height_cm=165),
            appearance_block="thin 18-22 East Asian woman, narrow elongated face, "
                             "deep brown eyes, black bob hair at ear length, small nose, "
                             "fair skin with a small mole below the left ear",
            wardrobe_states=[
                WardrobeState(state_id="C1-default", desc_en="gray coarse cloth coat, dark trousers"),
                WardrobeState(state_id="C1-wet", desc_en="wet gray coat, soaked sleeves")],
            image=ImageSpec(prompt="young East Asian woman, gray coarse cloth coat, "
                                  "short black bob hair, thin build, neutral background",
                            negative_prompt="blurry, low quality, text, watermark"),
            voice=VoiceSpec(timbre="低沉克制", prompt="low calm female voice, restrained"),
            color_signature="#6B6B6B"),
        CastCharacter(
            id="C2", name="陆远", importance="major", one_liner="巡夜人",
            persona={"gender": "男", "age_range": "30-35", "identity": "巡夜人",
                     "appearance": "黑色雨衣，高个", "personality": ["冷静"]},
            identity_lock=IdentityLock(age_range="30-35", face_shape="方脸",
                                       hair="黑色短寸", eyes="灰色",
                                       build="高大", height_cm=185),
            appearance_block="tall 30-35 East Asian man, square jaw, short cropped black "
                             "hair, cold grey eyes, broad shoulders, weather-worn skin",
            wardrobe_states=[
                WardrobeState(state_id="C2-default", desc_en="black raincoat, tall boots"),
                WardrobeState(state_id="C2-unmasked", desc_en="black raincoat open, no hat")],
            image=ImageSpec(prompt="tall East Asian man, black raincoat, short cropped hair, "
                                  "square jaw, neutral background",
                            negative_prompt="blurry, low quality, text, watermark"),
            voice=VoiceSpec(timbre="冷硬", prompt="cold low male voice, clipped"),
            color_signature="#1C1C1C"),
    ])

    art_scenes = []
    for s in scenes:
        art_scenes.append(ArtScene(
            id=s.id, name=s.name, primary=s.primary, summary="雾中木栈桥与旧渡船",
            location_type="EXT" if s.primary else "EXT",
            time_of_day="DAWN" if s.primary else "DAWN",
            anchors=[Anchor(name="木桩", desc="湿黑的缆桩"),
                     Anchor(name="渡船", desc="旧木船"),
                     Anchor(name="雾", desc="贴水的浓雾")],
            lighting=[LightingState(state="dawn", prompt="cold blue dawn light, wet wood")],
            image=ArtImage(prompt="empty old wooden ferry pier in thick river fog, "
                                  "wet planks, mooring posts, no people, cold blue dawn light",
                           negative_prompt="people, person, figure, human, text, watermark"),
        ))
    art = Art(source="fixture", setting="民国河港", scenes=art_scenes, props=[
        ArtProp(id="P1", name="旧皮箱", scale="手持级", summary="装着真相的旧箱",
                anchors=[Anchor(name="锁扣", desc="生锈的铜锁"),
                         Anchor(name="边角", desc="磨白的皮面"),
                         Anchor(name="提手", desc="缠着布条")],
                states=[PropState(state="closed", prompt="closed worn leather suitcase"),
                        PropState(state="open", prompt="open worn leather suitcase")],
                related_scenes=["S1", "S8"], carried_by="C1",
                image=ArtImage(prompt="handheld worn brown leather suitcase, brass clasp, "
                                      "scratched edges, neutral background",
                               negative_prompt="hand, fingers, people, text, watermark")),
    ])

    coverages = [
        ["establish", "action", "reaction", "insert", "dialogue"],
        ["pov", "reaction", "action", "insert", "dialogue"],
        ["establish", "action", "reaction", "insert", "dialogue"],
        ["reaction", "action", "reaction", "insert", "dialogue"],
        ["establish", "action", "reaction", "insert", "dialogue"],
        ["reaction", "action", "reaction", "insert", "dialogue"],
        ["establish", "action", "reaction", "insert", "dialogue"],
        ["reaction", "action", "reaction", "insert", "dialogue"],
    ]
    labels = ["钩子", "抉择", "激励", "受挫", "转折", "代价", "高潮", "结尾钩子"]
    script_scenes = []
    for i in range(8):
        line = DIALOGUES[i]
        cov = coverages[i]
        beats = [
            ScriptBeat(action="阿禾把旧皮箱换到左手，右手压住箱扣，沿栈桥向前走。",
                       coverage=cov[0],
                       state_from="阿禾在栈桥南段，面朝北", screen_side="center"),
            ScriptBeat(action="陆远从缆桩后跨步挡住去路，抬手示意她停下。",
                       coverage=cov[1],
                       state_from="陆远在栈桥北段，面朝南", screen_side="center"),
            ScriptBeat(speaker="C1" if i % 2 == 0 else "C2", line=line,
                       coverage=cov[2],
                       delivery="压低声音", expression="眼神戒备，嘴角绷紧"),
            ScriptBeat(action="阿禾把皮箱放到两人之间的木板上，锁扣轻响。",
                       coverage=cov[3],
                       state_from="阿禾在栈桥中段，面朝北", screen_side="center"),
            ScriptBeat(action="陆远俯身看箱面，又抬头看向河心，侧身让开。",
                       coverage=cov[4],
                       state_from="陆远在栈桥北段，面朝南", screen_side="center"),
        ]
        script_scenes.append(ScriptScene(
            id=f"S{i+1}", location="雾中渡口", time="黎明前",
            lighting="cold blue dawn", characters=["C1", "C2"], props=["P1"],
            wardrobe={"C1": "C1-default", "C2": "C2-default"},
            goal="带箱子过河", obstacle="巡夜人拦截",
            turn="陆远看到箱面后让开", cost=("失去退路" if i == 3 else
                                              ("受伤" if i == 5 else "无")),
            information="箱子里不是钱", objects=["旧皮箱"], mood="压抑",
            beat=labels[i], function="推进", beats=beats,
        ))
    ep = ScriptEpisode(ep=1, title="雾夜渡口", target_seconds=90,
                       hook="箱子让巡夜人后退", cliff="雾里出现第三个人",
                       hook_beat=(1, 1), beats_claimed=["B1", "B2", "B3"],
                       scenes=script_scenes)

    locations = []
    staging_scenes = []
    for i, sc in enumerate(script_scenes, 1):
        locations.append(StagingLocation(
            scene_id=sc.id, name="雾中渡口",
            items=[StagingItem(name="木桌", plan=[0.2, 0.3, 0.8, 0.6], on=False),
                   StagingItem(name="缆桩", plan=[0.05, 0.1, 0.15, 0.4], on=False)]))
        staging_scenes.append(StagingScene(
            ep=1, scene=i, scene_id=sc.id, lighting="cold blue dawn",
            cast=[StagingCast(id="C1", name="阿禾"), StagingCast(id="C2", name="陆远")],
            places=[
                StagingPlace(id="C1", name="阿禾", item="木桌", side="west",
                             along=0.4, posture="stand", facing="south"),
                StagingPlace(id="C2", name="陆远", item="木桌", side="east",
                             along=0.6, posture="stand", facing="south")],
            extras=0, why="对话双方左右相对，皮箱在中间"))
    staging = Staging(source="fixture", locations=locations, scenes=staging_scenes)

    sizes = ["medium", "wide", "close", "extreme-wide", "medium"]
    cameras = ["Static Shot", "Tracking Shot", "Static Shot", "Push In", "Static Shot"]
    segments = []
    for i, sc in enumerate(script_scenes, 1):
        cuts = []
        for b in range(1, 6):
            is_line = b == 3
            sec = (round(len("".join(sc.beats[b - 1].line.split())) / 4.5, 2)
                   if is_line else 2.5)
            # 每个画幅只放 1 张正脸：下游 gate 的结构限制（PuLID 单参考图）
            who = sc.beats[2].speaker if is_line else ("C1" if b % 2 else "C2")
            cuts.append(StoryCut(
                beats=(b, b), seconds=sec, size=sizes[b - 1],
                camera=cameras[b - 1], characters=[who], props=["P1"],
                frame=f"第{i}场第{b}拍：阿禾与陆远隔着旧皮箱对峙，雾气贴地，冷蓝晨光。",
                prompt_en="a young woman in a gray coat and a tall man in a black raincoat "
                          "face each other across a worn leather suitcase on a wet wooden pier, "
                          "thick river fog, cold blue dawn light",
                shot="两人僵持，皮箱横在中间。",
                motion="fog drifting, coat hem moving slightly",
                coverage=coverages[i - 1][b - 1], cut_reason="action_done",
                purpose="推进对峙", information="箱子里不是钱",
                sfx="water lapping", stability="stable",
                subjects=[CutSubject(
                    char=who,
                    wardrobe_state=f"{who}-default",
                    pose=("standing, hand on suitcase" if who == "C1"
                          else "standing, blocking the way"),
                    expression=("calm, guarded" if who == "C1"
                                else "cold, watchful"))],
            ))
        segments.append(StorySegment(id=f"E01-{i:02d}", scene_index=i, cuts=cuts))
    board = StoryEpisodeBoard(ep=1, segments=segments)
    return outline, cast, art, [ep], staging, StoryStoryboard(
        source="fixture", aspect_ratio="9:16", episodes=[board])


def _single_cut(seg, beat_a, beat_b):
    seg = seg.model_copy(deep=True)
    cut = seg.cuts[0].model_copy(deep=True)
    cut.beats = (beat_a, beat_b)
    cut.characters = ["C1", "C2"]
    cut.subjects = [
        CutSubject(char="C1", wardrobe_state="C1-default",
                   pose="standing", expression="calm"),
        CutSubject(char="C2", wardrobe_state="C2-default",
                   pose="standing opposite", expression="cold"),
    ]
    seg.cuts = [cut]
    return seg


def test_script_budget_plans_shot_and_coverage_quotas():
    """一镜一拍 + drama 覆盖率配额必须在剧本预算里算出来。"""
    budget = _script_budget(90)

    assert budget["min_total_beats"] == 26          # 90s / 3.5s（2026-10-30 放宽）
    assert budget["max_total_beats"] >= budget["min_total_beats"]
    assert budget["coverage_required"] == {
        "reaction": 8, "insert": 6, "establish": 3, "pov": 1}
    # 2026-10-30：场次改为"完整戏剧单元"（1-4 场，由内容决定），不再按秒数切 5-8 场。
    assert budget["suggest_scenes"] == "1-4"
    assert budget["suggest_beats_per_scene"] == "4-12"


def test_script_gate_new_narrative_rules():
    """叙事优先口径：戏剧单元 1-4 个、台词句数不设硬上限、key_lines 透传。"""
    outline, cast, art, eps, _, _ = _fixture()
    ep = eps[0].model_copy(deep=True)
    # 8 场在旧口径合法，新口径（1-4 个完整戏剧单元）应提醒。
    rep = validate_script(ep, outline, cast, art)
    assert "建议 1-4 个" in rep.text()

    # 压到 4 个单元 → 不再提醒。
    four = ep.model_copy(deep=True)
    four.scenes = four.scenes[:4]
    rep4 = validate_script(four, outline, cast, art)
    assert "建议 1-4 个" not in rep4.text()

    # 台词句数不设硬上限：24 句也不告警（非 dialogue_light）。
    many = ep.model_copy(deep=True)
    for sc in many.scenes:
        for _ in range(2):
            sc.beats.append(ScriptBeat(
                speaker="C1", line="这句台词用来撑句数，长度合适。",
                coverage="dialogue", delivery="平静"))
    repm = validate_script(many, outline, cast, art)
    assert "句台词，建议" not in repm.text()

    # key_lines 由 SeriesDesign 确定性透传到 EpisodePlan（修复链路断点）。
    from manju.story.models import DesignEpisode
    from manju.story.repairs import _apply_design_to_plans
    d = DesignEpisode(ep=1, title="雾夜渡口", atom_ids=[1],
                      synopsis="过河", must_show=["阿禾交箱"],
                      key_lines=["“箱子里的东西不是钱。”"],
                      key_props=["P1"], cliff="雾里第三个人")
    plan = outline.episodes[0].model_copy(deep=True)
    plan.key_lines = []
    _apply_design_to_plans([plan], {1: d}, 90)
    assert plan.key_lines == ["“箱子里的东西不是钱。”"]


def test_script_gate_blocks_too_few_beats_and_missing_coverage():
    """剧本层必须提前拦住镜数不足与覆盖率缺项。"""
    outline, cast, art, eps, _, _ = _fixture()
    ep = eps[0]

    clean = validate_script(ep, outline, cast, art)
    assert "少于下限" not in clean.text()
    assert "少于配额" not in clean.text()

    short = ep.model_copy(deep=True)
    short.scenes = short.scenes[:4]                 # 20 拍 < 35
    text = validate_script(short, outline, cast, art).text()
    assert "少于下限" in text

    no_reaction = ep.model_copy(deep=True)
    for sc in no_reaction.scenes:
        for b in sc.beats:
            if b.coverage == "reaction":
                b.coverage = None
    text = validate_script(no_reaction, outline, cast, art).text()
    assert "coverage=reaction" in text
    assert "未声明 coverage" in text


def test_script_coverage_repair_fills_quota_without_changing_beats():
    """全标 action 时，确定性重标已有动作拍，不能为配额增删剧情/台词。"""
    outline, cast, art, eps, _, _ = _fixture()
    ep = eps[0].model_copy(deep=True)
    for sc in ep.scenes:
        for b in sc.beats:
            b.coverage = "action"
    assert "coverage=reaction" in validate_script(ep, outline, cast, art).text()

    before = [(len(sc.beats), sc.beats[0].action, sc.beats[2].line)
              for sc in ep.scenes]
    fixed = _repair_script_coverage(ep, ScriptParams(), lambda *_: None)
    after = [(len(sc.beats), sc.beats[0].action, sc.beats[2].line)
             for sc in ep.scenes]

    assert fixed >= 18
    assert before == after                       # 拍数/动作/台词原样
    assert "少于配额" not in validate_script(ep, outline, cast, art).text()


def test_unused_primary_scene_is_demoted_not_fatal():
    """未被任何一集使用的主场景降级为备用，不再阻断整条流水线。"""
    outline, _, _, eps, _, _ = _fixture()
    out = outline.model_copy(deep=True)
    out.scenes = list(out.scenes) + [
        OutlineScene(id="S99", name="备用码头", primary=True, summary="从未使用")]

    assert "主场景 S99 没有被任何一集使用" in validate_outline(out).text()
    changed = _demote_unused_primary_scenes(out, lambda *_: None)

    assert changed == ["S99"]
    scene = next(s for s in out.scenes if s.id == "S99")
    assert scene.primary is False
    rep = validate_outline(out, strict=False)
    assert rep.ok
    assert "非主场景 S99" in rep.text()


def test_staging_item_autoregisters_in_location():
    """人物绑定了地点物件表里没有的物体时补登记，而不是整集重写。"""
    _, _, _, eps, staging, _ = _fixture()
    st = staging.model_copy(deep=True)
    st.scenes[0].places[0].item = "废弃车辆"

    fixed = _repair_staging_output(st, eps[0], lambda *_: None)

    loc = next(loc for loc in st.locations if loc.scene_id == st.scenes[0].scene_id)
    assert fixed == 1
    assert any(item.name == "废弃车辆" for item in loc.items)


def test_shared_scene_id_unions_location_items_across_episodes():
    """两集复用同一 scene_id 时，合并后的地点表必须取物件并集。

    真实故障：ep1 的 S2 写“黄砖路起点”，ep2 的 S2 写“黄砖路/石块”；
    若合并时按 scene_id 只留先出现的地点，最终 validate_all 会在 ep2 上
    报“item 不在地点物件表里”，而此时分镜早已全部生成完毕。
    """
    _, _, art, eps, staging, _ = _fixture()
    ep2 = eps[0].model_copy(deep=True)
    ep2.ep = 2
    ep2.scenes = [eps[0].scenes[0].model_copy(deep=True)]      # 复用 S1
    stg2 = Staging(
        source="fixture",
        locations=[StagingLocation(
            scene_id="S1", name="雾中渡口",
            items=[StagingItem(name="黄砖路", plan=[0.1, 0.2, 0.9, 0.8], on=True)])],
        scenes=[StagingScene(
            ep=2, scene=1, scene_id="S1",
            cast=[StagingCast(id="C1", name="阿禾"),
                  StagingCast(id="C2", name="陆远")],
            places=[StagingPlace(id="C1", name="阿禾", item="黄砖路",
                                 side="west", along=0.4, posture="stand",
                                 facing="south"),
                    StagingPlace(id="C2", name="陆远", present=False)])])
    merged = _merge_stagings([staging, stg2], "fixture")

    rep = validate_staging(merged, ep2, art)

    assert rep.ok, rep.text()
    s1 = next(loc for loc in merged.locations if loc.scene_id == "S1")
    names = {item.name for item in s1.items}
    assert {"木桌", "缆桩", "黄砖路"} <= names


def test_offscreen_prop_is_removed_from_visible_props_only():
    """画外角色持有的道具被误塞进 props 时，只从本镜可见道具移除。"""
    _, cast, _, eps, _, board = _fixture()
    scene = eps[0].scenes[0]
    seg = board.episodes[0].segments[0].model_copy(deep=True)
    cut = seg.cuts[0].model_copy(deep=True)
    cut.characters = ["C1"]
    cut.subjects = [CutSubject(char="C1")]
    cut.props = ["P1"]
    cut.state_out = cut.state_out.model_copy(
        update={"props": {"P1": "C2"}})
    cut.state_from = cut.state_out.model_copy(deep=True)
    seg.cuts = [cut]

    _repair_segment_output(seg, scene, lambda *_: None, cast=cast)

    assert seg.cuts[0].props == []
    assert seg.cuts[0].state_out.props == {"P1": "C2"}   # 世界状态不能丢


def test_segment_repair_normalizes_character_names_to_ids():
    """分镜修复要把 characters/state_out 里的人名映射回 Cid，而不是崩掉。"""
    _, cast, _, eps, _, board = _fixture()
    scene = eps[0].scenes[0]
    seg = board.episodes[0].segments[0].model_copy(deep=True)
    cut = seg.cuts[0]
    cut.characters = ["阿禾"]
    cut.state_out = cut.state_out.model_copy(update={
        "where": {"阿禾": "center"},
        "props": {"P1": "阿禾"},
    })

    _repair_segment_output(seg, scene, lambda *_: None, cast=cast)

    assert cut.characters == ["C1"]
    assert cut.state_out.where.get("C1") == "center"
    assert "阿禾" not in cut.state_out.where
    assert cut.state_out.props["P1"] == "C1"


def test_face_limit_repair_converges_to_single_face():
    """超过引擎锁脸上限的双人正打，确定性收敛为单人镜而不是反复重写。"""
    _, cast, _, eps, _, board = _fixture()
    scene = eps[0].scenes[0]
    seg = board.episodes[0].segments[0].model_copy(deep=True)
    for cut in seg.cuts:
        cut.characters = ["C1", "C2"]
        cut.subjects = [CutSubject(char="C1"), CutSubject(char="C2")]
        cut.allow_multi_face = False

    _repair_segment_output(
        seg, scene, lambda *_: None,
        params=ScriptParams(max_faces_per_shot=1), cast=cast)

    assert all(len(set(c.characters) & {"C1", "C2"}) == 1 for c in seg.cuts)
    line_cut = seg.cuts[2]                 # 第 3 拍是台词拍
    assert scene.beats[2].speaker in line_cut.characters


def test_generate_feeds_check_crash_back_instead_of_aborting(tmp_path):
    """质量门里的确定性修复崩溃时，应重试而不是让整条流水线直接退出。"""
    class _Out(BaseModel):
        answer: int

    class _FakeLLM:
        def __init__(self):
            self.calls = 0

        def structured(self, *_a, **_k):
            self.calls += 1
            return _Out(answer=self.calls)

    pipe = StoryPipeline(llm=_FakeLLM(), workdir=tmp_path)
    pipe.attempts = 2
    seen: list[int] = []

    def check(obj):
        seen.append(obj.answer)
        if obj.answer == 1:
            raise ValueError("boom from repair")
        return GateReport("test")

    out = pipe._generate("stage", _Out, "sys", {}, check,
                         name="out", max_tokens=16)

    assert out.answer == 2
    assert seen == [1, 2]


def test_board_coverage_inherits_script_plan():
    """分镜 coverage 不得自创，必须跟随剧本拍规划（认领的首拍）。"""
    _, _, _, eps, _, board = _fixture()
    scene = eps[0].scenes[0]
    seg = board.episodes[0].segments[0].model_copy(deep=True)
    seg.cuts[0].coverage = "action"                # 剧本首拍是 establish
    seg.cuts[4].coverage = "establish"             # 剧本第五拍是 dialogue

    _repair_segment_output(seg, scene, lambda *_: None)

    assert seg.cuts[0].coverage == "establish"
    assert seg.cuts[4].coverage == "dialogue"


def test_board_rules_require_script_coverage():
    rules = _board_rules(ScriptParams())

    assert "beat_plan.coverage" in rules
    assert "不得改类" in rules
    # 不再诱导模型把一拍拆给多镜（违反一镜一拍）
    assert "一个节拍需要多机位时拆成多镜" not in rules


def test_prompt_rules_include_recipe_and_anchor_verbatim():
    """分镜 prompt 实际注入的规则（_prompt_rules）必须带 P0.5 配方与 P1 锚点 verbatim。

    回归 2026-11-02：新标准规则此前只写在 _board_rules（从未被 stages.py 调用），
    分镜生成 prompt 实际只有禁调度词/对话镜规则，导致产物 36/37 镜缺镜头配方。
    """
    rules = _prompt_rules(ScriptParams())
    assert "摄影配方" in rules
    assert "screen-left" in rules
    assert "shallow depth of field" in rules
    assert "identity_anchor" in rules
    assert "逐字复用" in rules
    # 与 P1 矛盾的历史约束（“不要写任何外貌”）已移除
    assert "不要写任何姓名、脸部、身材、发型" not in rules


def test_extract_camera_recipe_from_prompt():
    """framing.camera_recipe 槽位由 compile 从 prompt_en 确定性提取（契约 §6.5）。"""
    from manju.story.compile import _extract_camera_recipe

    r = _extract_camera_recipe(
        "A girl in a red collar hat, light from screen-left window, soft grey "
        "daylight, 50mm shallow depth of field")
    assert r["lens_mm"] == "50mm"
    assert r["depth_of_field"] == "shallow"
    assert r["light_key"] == "screen-left"
    assert r["light_quality"] == "soft"

    r2 = _extract_camera_recipe("Extreme close-up of paws, 85mm compressed, warm lamplight")
    assert r2["lens_mm"] == "85mm"
    assert r2["depth_of_field"] == "compressed"
    assert "warm" in (r2.get("light_quality") or "")


def test_fallback_segments_no_adjacent_dup_when_action_clauses_few():
    """拆段兜底：动作从句不足段数时，相邻段文本不得重复（回归 SH08/SH20 seg.dup）。

    旧实现全部取 states[0]，导致第 2/3 段 100% 相同（Jaccard=1.00 触发质量门 block）。
    """
    import re as _re
    prompt_en = (
        "Extreme close-up of small black curly paws scrambling on a sharply "
        "tilted wooden floor, claws skidding, one paw sliding sideways with "
        "dust and straw scattering; a thin worn red cord collar has slipped up "
        "high around the base of a black furry ear; warm lamplight rakes the "
        "fur, a cold blue flicker cuts across the boards.")
    cut = StoryCut.model_construct(
        scene_index=1, beats=(1, 1), seconds=2.27, size="close",
        camera="Static Shot", characters=["C1"], props=[], frame="中文",
        prompt_en=prompt_en, shot="", motion="",
        coverage="action", cut_reason="action_done",
        state_from=CutState(), state_out=CutState(),
        beat_goal="托托的黑爪在倾斜的木板上打滑，红绳项圈滑到耳根。")
    segs = _fallback_segments(cut, 2.27, 4)
    assert segs is not None and len(segs) >= 2
    for a, b in zip(segs, segs[1:]):
        wa = set(_re.findall(r"[a-z]+", a["text"].lower()))
        wb = set(_re.findall(r"[a-z]+", b["text"].lower()))
        j = (len(wa & wb) / len(wa | wb)) if (wa or wb) else 0.0
        assert j < 0.6, (a["text"], b["text"], j)
    # 段和恒=整镜（±round 精度 0.01）、末段 keyframe
    assert abs(sum(s["duration_sec"] for s in segs) - 2.27) < 0.011
    assert segs[-1]["keyframe"] is True


def test_board_static_ratio_only_guards_against_near_all_static():
    """关键拍强制运镜后，静态占比偏低不应被判失败；只拦“几乎全静止”。"""
    _outline, cast, _art, scripts, _staging, board = _fixture()
    ep = scripts[0]
    bep = board.episodes[0]

    # 运镜充分：每 5 镜 1 个 Static（20%），应通过且不告警。
    for seg in bep.segments:
        for cut in seg.cuts:
            cut.camera = ("Static Shot" if cut.beats[0] % 5 == 0
                          else "Push In")
    rep = validate_board(bep, ep, cast)
    static_msgs = [m for m in rep.errors + rep.warnings
                   if "Static Shot" in m and "占比" in m]
    assert not static_msgs, static_msgs

    # 几乎全静止：必须有运镜（full 静态 ≤40%），默认提示、--strict 升级为硬错误。
    for seg in bep.segments:
        for cut in seg.cuts:
            cut.camera = "Static Shot"
    rep = validate_board(bep, ep, cast)
    assert any("不得超过 40%" in m for m in rep.warnings), rep.warnings
    strict = validate_board(bep, ep, cast, strict=True)
    assert any("不得超过 40%" in m for m in strict.errors), strict.errors


def test_story_taste_rules_are_warnings_by_default_and_strict_restores():
    """小说里没被采用的配角/台词覆盖率等是改编取舍，不能默认卡死生成。

    真实故障（SSS）：整本小说里的 C6 没被分集采用，直接以“失业角色”
    硬失败。P0 起这类叙事口味默认只告警，``--strict`` 才恢复为错误。
    """
    outline, cast, art, scripts, staging, board = _fixture()
    outline.characters.append(OutlineCharacter(
        id="C9", name="过场人", role="路人", tier="support",
        identity="只在原文出现", appearance="斗笠", personality=["沉默"],
        voice_hint="低"))
    rep = validate_bible(outline, outline.episodes)
    assert any("C9" in m and "没有被任何一集使用" in m for m in rep.warnings)
    assert not any("C9" in m for m in rep.errors)
    strict = validate_bible(outline, outline.episodes, strict=True)
    assert any("C9" in m and "没有被任何一集使用" in m for m in strict.errors)

    # coverage 配额同理：默认提示，strict 才拦。
    ep = scripts[0]
    bep = board.episodes[0]
    for seg in bep.segments:
        for cut in seg.cuts:
            cut.coverage = "action"
    rep = validate_board(bep, ep, cast)
    assert any("coverage.reaction" in m and "至少" in m
               for m in rep.warnings), rep.warnings
    strict = validate_board(bep, ep, cast, strict=True)
    assert any("coverage.reaction" in m and "至少" in m
               for m in strict.errors), strict.errors


def test_prop_pickup_from_scene_is_visible_in_the_current_shot():
    """回归：道具从地面被拿起时，上一镜可以只是道具特写。

    真实故障（绿野仙踪 S3）：第 4 拍银鞋还在地上（P1=scene），第 5 拍
    女巫弯腰把鞋穿到多萝茜脚上（P1=C1），镜头里 C1/C3 与 P1 都在。旧规则
    要求 C1 出现在上一镜，或把 "scene" 当人物写进 characters，导致正确
    分镜被判“交接不可见”。从环境拿起只要求本镜展示道具+新持有者，并且
    本镜是 action_done。
    """
    def cut(beats, chars, props, owner, reason):
        return StoryCut(
            beats=beats, seconds=2.0, size="close", camera="Push In",
            characters=chars, props=props,
            frame="银鞋与脚踝特写",
            prompt_en="close-up of silver shoes and ankles",
            cut_reason=reason,
            state_out=CutState(props={"P1": owner}),
        )

    ground = cut((4, 4), ["C5"], ["P1"], "scene", "match_cut")
    pickup = cut((5, 5), ["C1", "C3"], ["P1"], "C1", "action_done")
    assert not any("交接不可见" in e for e in validate_adjacent(ground, pickup))

    # 本镜没有真正展示道具：拿起动作不可见，仍要拦。
    hidden = cut((5, 5), ["C1", "C3"], [], "C1", "action_done")
    assert any("交接不可见" in e for e in validate_adjacent(ground, hidden))

    # 只是反应镜：没有动作完成，不能算可见交接。
    reaction = cut((5, 5), ["C1", "C3"], ["P1"], "C1", "reaction")
    assert any("交接不可见" in e for e in validate_adjacent(ground, reaction))

    # 新持有者本身不在画内，仍是硬错误。
    offscreen = cut((5, 5), ["C3"], ["P1"], "C1", "action_done")
    assert any("新持有者 C1 不在画内"
               for e in validate_adjacent(ground, offscreen))


def test_face_limit_is_engine_capability_not_default():
    """默认不限制同框人脸；只有配置了 max_faces_per_shot 才拦。"""
    _, cast, _, eps, _, board = _fixture()
    seg = _single_cut(board.episodes[0].segments[0], 1, 1)
    scene = eps[0].scenes[0]

    assert "张/画幅" not in validate_segment_cuts(seg, scene, cast).text()

    limited = validate_segment_cuts(
        seg, scene, cast, params=ScriptParams(max_faces_per_shot=1))
    assert "当前配置的上限是 1 张/画幅" in limited.text()


def test_line_limit_is_engine_capability_not_default():
    """默认不限制单镜台词句数；配置 1 时才报多句。"""
    _, cast, _, eps, _, board = _fixture()
    scene = eps[0].scenes[0].model_copy(deep=True)
    scene.beats = list(scene.beats)
    scene.beats[1] = ScriptBeat(speaker="C2", line="你放下箱子",
                                delivery="冷硬", expression="盯着她")
    seg = _single_cut(board.episodes[0].segments[0], 2, 3)
    seg.cuts[0].seconds = 3.0

    assert "句/镜" not in validate_segment_cuts(seg, scene, cast).text()

    limited = validate_segment_cuts(
        seg, scene, cast,
        params=ScriptParams(max_lines_per_shot=1, max_shot_seconds=3.2))
    assert "当前引擎上限 1 句/镜" in limited.text()


def test_prompt_language_any_disables_english_only_check():
    """提示词语言是引擎能力：默认 en 校验，any/zh 不拦。"""
    _, cast, _, eps, _, board = _fixture()
    seg = board.episodes[0].segments[0].model_copy(deep=True)
    seg.cuts = [seg.cuts[0].model_copy(deep=True)]
    seg.cuts[0].prompt_en = "雾中渡口，两个人隔着一只旧皮箱对峙"
    scene = eps[0].scenes[0]

    assert "prompt_en 含中文" in validate_segment_cuts(seg, scene, cast).text()
    loose = validate_segment_cuts(
        seg, scene, cast, params=ScriptParams(prompt_language="any"))
    assert "prompt_en 含中文" not in loose.text()


def test_prompt_language_flows_into_cast_art_and_board_rules():
    """语言是引擎能力：cast/art/board 的提示词规则跟着 story.prompt_language 走。"""
    en = ScriptParams()
    zh = ScriptParams(prompt_language="zh")
    assert "image.prompt 是英文" in _cast_rules(en)
    assert "image.prompt 是中文" in _cast_rules(zh)
    assert "prompt 必须是英文" in _art_rules(en)
    assert "prompt 必须是中文" in _art_rules(zh)
    assert "prompt_en 是英文" in _board_rules(en)
    assert "prompt_en 是中文" in _board_rules(zh)
    # 默认行为（英文）不变。
    assert "image.negative_prompt 英文" in _cast_rules(en)


def test_drama_line_cap_is_configurable(tmp_path):
    """drama 审计的单句字数上限必须跟 story.max_line_chars 一致。"""
    from manju.drama import audit_files

    outline, cast, art, scripts, staging, board = _fixture()
    files = compile_all(outline, cast, art, scripts, staging, board, tmp_path)
    data = json.loads(files["script_ep01"].read_text(encoding="utf-8"))
    data["scenes"][0]["lines"][0]["text"] = "这" * 20
    long_script = tmp_path / "script_long.json"
    long_script.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    paths = [files["plan"], long_script, files["storyboard_ep01"]]

    strict_18 = audit_files(paths, files["director_ep01"], target_sec=90)
    assert any(f.code == "S7" for f in strict_18.blocks)
    loose = audit_files(paths, files["director_ep01"], target_sec=90,
                        max_line_chars=30)
    assert not any(f.code == "S7" for f in loose.blocks)


def test_no_proxy_ipv6_entries_are_sanitized(monkeypatch):
    """NO_PROXY 里的 [::1] 会让 httpx 抛 Invalid port，必须清理掉。"""
    from manju.llm import _sanitize_no_proxy_env

    monkeypatch.setenv("NO_PROXY", "localhost,[::1],127.0.0.1")
    monkeypatch.setenv("no_proxy", "localhost,[::1],127.0.0.1")
    _sanitize_no_proxy_env()
    assert os.environ["NO_PROXY"] == "localhost,127.0.0.1"
    assert os.environ["no_proxy"] == "localhost,127.0.0.1"


def test_params_from_cfg_generic_defaults_and_local_override():
    """config 不写能力项 = 通用不限制；写了才收紧。"""
    default = params_from_cfg({})
    assert default.max_faces_per_shot is None
    assert default.max_lines_per_shot is None
    assert default.max_shot_seconds == 6.0
    assert default.max_segment_seconds == 15.0

    local = params_from_cfg({
        "pipeline.pulid.max_faces_per_shot": 1,
        "lipsync.max_lines_per_shot": 1,
        "story.prompt_language": "zh",
        "pipeline.max_shot_seconds": 4.0,
    })
    assert local.max_faces_per_shot == 1
    assert local.max_lines_per_shot == 1
    assert local.prompt_language == "zh"
    assert local.max_shot_seconds == 4.0

    # story.* 是唯一权威来源；pipeline.* 仅作旧配置兼容。
    canonical = params_from_cfg({
        "story.max_shot_seconds": 2.8,
        "pipeline.max_shot_seconds": 9.0,
        "story.max_segment_seconds": 12.0,
        "pipeline.max_segment_seconds": 30.0,
        "story.max_faces_per_shot": 2,
        "pipeline.pulid.max_faces_per_shot": 1,
        "story.max_lines_per_shot": 3,
        "pipeline.lipsync.max_lines_per_shot": 1,
    })
    assert canonical.max_shot_seconds == 2.8
    assert canonical.max_segment_seconds == 12.0
    assert canonical.max_faces_per_shot == 2
    assert canonical.max_lines_per_shot == 3

    narration = params_from_cfg({
        "story.narration_mode": "off",
        "story.narration_max_exposition": 1,
        "story.narration_max_inner": 2,
        "story.transition_mode": "on",
    })
    assert narration.narration_mode == "off"
    assert narration.narration_max_exposition == 1
    assert narration.narration_max_inner == 2
    assert narration.transition_mode == "on"

    invalid = params_from_cfg({
        "story.narration_mode": "sometimes",
        "story.transition_mode": "maybe",
    })
    assert invalid.narration_mode == "auto"
    assert invalid.transition_mode == "auto"


def test_world_bible_is_json_serializable():
    """world_bible 直接塞进 LLM payload，任何 Pydantic 对象都会让 json.dumps 崩。"""
    from manju.story.stages import _world_bible

    _, cast, art, *_ = _fixture()
    data = _world_bible(cast, art)
    json.dumps(data, ensure_ascii=False)          # 不应抛 TypeError
    assert data["characters"]["C1"]["wardrobe_states"][0]["state_id"] == "C1-default"


def test_prompt_identity_mode_is_not_a_pipeline_parameter():
    """输入层不决定实现层的人物一致性技术，旧配置键也不应再生效。"""
    assert "prompt_identity_mode" not in ScriptParams.model_fields
    p = params_from_cfg({
        "story.prompt_identity_mode": "full",
        "llm.story_prompt_identity_mode": "none",
    })
    assert "prompt_identity_mode" not in p.model_dump()


def test_identity_anchor_is_short_and_uses_distinguishing_features():
    from manju.story.models import IdentityLock, identity_anchor

    _, cast, *_ = _fixture()
    c1 = cast.characters[0]
    anchor = identity_anchor(c1)
    assert "黑色齐耳短发" in anchor
    assert len(anchor) <= 180
    # 不包含完整定妆 prompt 里的服装/站姿。
    assert "gray coarse cloth coat" not in anchor
    assert "neutral background" not in anchor

    # 锁脸字段缺失时才回退到外貌块首句，且仍受长度上限约束。
    fallback = c1.model_copy(deep=True)
    fallback.identity_lock = IdentityLock()
    text = identity_anchor(fallback)
    assert text.startswith("thin 18-22 East Asian woman")
    assert len(text) <= 181


def test_world_bible_carries_no_identity_text():
    """world_bible 只给连续性事实；完整外貌与身份锚点留给实现层。"""
    from manju.story.stages import _world_bible

    _, cast, art, *_ = _fixture()
    data = _world_bible(cast, art)
    ch = data["characters"]["C1"]

    assert "appearance" not in ch
    assert "identity_anchor" not in ch
    # 服装/颜色是连续性数据，必须保留。
    assert ch["wardrobe_states"][0]["state_id"] == "C1-default"
    assert ch["color_signature"] == "#6B6B6B"


def test_cast_payload_compacts_identity_for_prompt_stage():
    """提示词阶段只拿表演/连续性所需字段，不拿完整身份描述。"""
    from manju.story.stages import _cast_payload

    _, cast, *_ = _fixture()
    data = _cast_payload(cast, {"C1"})
    ch = data["characters"][0]

    assert "image" not in ch
    assert "appearance_block" not in ch
    assert "identity_lock" not in ch
    assert "appearance" not in ch["persona"]
    assert "identity_anchor" not in ch
    assert ch["wardrobe_states"][0]["state_id"] == "C1-default"
    assert len(data["characters"]) == 1     # 本场之外的角色仍被裁掉


def test_prompt_rules_keep_identity_out_of_prompt_text():
    from manju.story.stages import _prompt_rules

    rules = _prompt_rules(ScriptParams())
    assert "ref_characters" in rules and "story_assets" in rules
    # P1 锚点 verbatim：身份交给 ref_characters / identity_anchor，prompt_en
    # 逐字复用可一眼识别的锚点短语，但不写姓名、不展开脸型身材（2026-11-02 修正）
    assert "identity_anchor" in rules
    assert "逐字复用" in rules
    assert "不写姓名" in rules
    assert "本镜服装若改变" in rules


def test_compile_assets_keeps_identity_for_render_layer():
    """提示词不写身份，但完整身份数据必须仍交给实现层。"""
    from manju.story.compile import compile_assets

    outline, cast, art, *_ = _fixture()
    assets = compile_assets(outline, cast, art)
    c1 = assets["characters"]["C1"]
    assert c1["identity_prompt"]
    assert c1["identity_anchor"]
    assert c1["identity_prompt"] != c1["identity_anchor"]


def test_identity_repetition_warning():
    from manju.story.stages import _identity_repetition_warnings

    _, cast, *_ = _fixture()
    clause = "fair skin with a small mole below the left ear"
    cut = StoryCut(beats=(1, 1), seconds=2.5, size="close",
                   characters=["C1"], frame="阿禾停步",
                   prompt_en=f"a woman pauses at the pier, {clause}, cold dawn light")
    seg = StorySegment(id="E01-01", scene_index=1, cuts=[cut])

    warns = _identity_repetition_warnings(seg, cast)
    assert warns and "C1" in warns[0]


def test_compile_renumbers_repeated_location_ids(tmp_path):
    """同地点多场复用 sc.id；编译后的 scene_id 必须唯一，否则 gate 字典会吞场。"""
    outline, cast, art, scripts, staging, board = _fixture()
    ep = scripts[0]
    ep.scenes[1].id = ep.scenes[0].id          # 两场发生在同一地点

    files = compile_all(outline, cast, art, scripts, staging, board, tmp_path)
    script = json.loads(files["script_ep01"].read_text(encoding="utf-8"))
    story = json.loads(files["storyboard_ep01"].read_text(encoding="utf-8"))
    director = json.loads(files["director_ep01"].read_text(encoding="utf-8"))

    want = [f"S{i}" for i in range(1, len(ep.scenes) + 1)]
    assert [s["scene_id"] for s in script["scenes"]] == want
    assert [s["scene_id"] for s in director["scenes"]] == want
    assert {sh["scene_id"] for sh in story["shots"]} == set(want)
    assert set(story["scene_anchors"]) == set(want)

    # 同一地点的两场复用同一个环境锚点。
    assert story["scene_anchors"]["S1"] == story["scene_anchors"]["S2"]


def test_story_layer_gates_and_compile(tmp_path):
    outline, cast, art, scripts, staging, board = _fixture()
    reports = validate_all(outline, cast, art, scripts, staging, board)
    errors = [r.text() for r in reports if not r.ok]
    assert not errors, "\n".join(errors)

    files = compile_all(outline, cast, art, scripts, staging, board, tmp_path)
    for key in ("plan", "script_ep01", "storyboard_ep01", "director_ep01",
                "assets", "prompts"):
        assert key in files and files[key].is_file(), key

    from manju.drama import audit_files
    from manju.gate import check_files
    paths = [files["plan"], files["script_ep01"], files["storyboard_ep01"]]

    gate_rep = check_files(paths)
    assert not gate_rep.blocks, gate_rep.to_text()

    drama_rep = audit_files(paths, files["director_ep01"], target_sec=90)
    assert drama_rep.ok(), drama_rep.to_text()

    plan = json.loads(files["plan"].read_text(encoding="utf-8"))
    story = json.loads(files["storyboard_ep01"].read_text(encoding="utf-8"))
    assert plan["total_episodes"] == 1
    assert len(story["shots"]) == 40
    assert {"9:16", "16:9"} <= set(story["shots"][0]["framing"])

    # v10.3：schema 版本与可选字段，编译器必须真的写出来（否则只是一纸契约）。
    assert story["schema_version"] == "v10.3"
    assert story["shots"][0]["framing"]["9:16"]["subject_type"] == "character"
    talk = [s for s in story["shots"] if "画内" in (s.get("audio") or "")]
    assert talk, "fixture 里应有画内台词镜"
    for s in talk:
        assert s["requirements"].get("dubbing") == "lipsync"
        assert any(x.get("speaking") is True for x in s["subjects"]), s["shot_id"]


def _face_char(name: str, identity: str, appearance: str, block: str,
               tags: list[str]) -> CastCharacter:
    return CastCharacter(
        id="C1", name=name, one_liner=identity,
        persona={"identity": identity, "appearance": appearance},
        appearance_block=block,
        wardrobe_states=[WardrobeState(state_id="C1-default", desc_en="coat")],
        image=ImageSpec(prompt=block, tags=tags),
    )


def test_face_lock_ignores_english_substring_false_positives():
    # “petite” 不应命中 pet，“crown” 不应命中 crow。
    girl = _face_char(
        "多萝茜", "农场女孩", "金发，穿上银鞋",
        "A petite 10-year-old girl with golden braids and a silver crown.",
        ["girl", "silver shoes"],
    )
    assert face_lock_required(girl)


def test_face_lock_ignores_chinese_single_char_false_positives():
    # “龙卷风”不应命中“龙”；真正的狗/稻草人仍应排除。
    girl = _face_char(
        "多萝茜", "农场女孩", "龙卷风把她卷到奥兹国",
        "A young girl carried away by a tornado.", ["girl"],
    )
    assert face_lock_required(girl)
    dog = _face_char("托托", "多萝茜的小狗", "灰黑色小犬", "A small dog.", ["dog"])
    assert not face_lock_required(dog)


def test_compile_visual_bible_maps_story_fields_to_plan_contract():
    outline, cast, art, *_ = _fixture()
    outline.visual_bible = {
        "style_keywords": ["storybook realism", "sepia-to-vibrant"],
        "main_colors": ["#8B7355", "#2E8B57"],
        "do_dont": {"DO": "灰色调表现堪萨斯", "DON'T": "不要血腥"},
        "reference_styles": ["The Wizard of Oz 1939"],
    }
    plan = compile_plan(outline, cast, "9:16", art)
    # 下游 Plan 契约必须能直接校验；之前业务层字段透传会整份 schema 失败。
    Plan.model_validate(plan)
    vb = plan["visual_bible"]
    assert vb["medium"] == "photorealistic cinematic film still"
    assert vb["palette"] == "#8B7355, #2E8B57"
    assert "storybook realism" in vb["style_ref_en"]
    assert "The Wizard of Oz 1939" in vb["style_ref_en"]
    assert "不要血腥" in vb["forbidden"]


def test_compile_omits_in_scene_changing_wardrobe_from_scene_map(tmp_path):
    """场内有授权换装时，不能用一个场级 state_id 去卡所有镜头。

    真实故障（绿野仙踪 EP1 S3 / EP2 S1）：剧本拍级账本在第 2/5 拍换装，
    分镜前半段是 arrival/kansas、后半段是 default；compile 却把场级
    wardrobe_state 固定成起始态，gate 对每镜做全等比较 → block。
    """
    from manju.gate import check_files

    outline, cast, art, scripts, staging, board = _fixture()
    sc = scripts[0].scenes[2]
    sc.wardrobe = {"C1": "C1-wet", "C2": "C2-default"}
    sc.beats[2].state_change = {"C1": "换回干衣，服装态转 C1-default"}
    sc.beats[2].state_out = {"C1": "center/north/C1-default/无伤"}

    seg = board.episodes[0].segments[2]
    for cut in seg.cuts:
        for sub in cut.subjects:
            if sub.char == "C1":
                sub.wardrobe_state = ("C1-wet" if cut.beats[0] <= 2
                                      else "C1-default")

    files = compile_all(outline, cast, art, scripts, staging, board, tmp_path)
    data = json.loads(files["script_ep01"].read_text(encoding="utf-8"))
    scene = data["scenes"][2]

    assert "C1" not in scene["wardrobe_state"]
    assert scene["wardrobe_state"]["C2"] == "C2-default"

    paths = [files["plan"], files["script_ep01"], files["storyboard_ep01"]]
    no_face = {c.id for c in cast.characters if not face_lock_required(c)}
    rep = check_files(paths, no_face_chars=no_face)
    assert not [f for f in rep.blocks if f.code == "ref.wardrobe_mismatch"]


def test_compile_line_timing_covers_standard_and_scene_cps(tmp_path):
    """台词 timing 必须同时满足标准语速（gate）与场景语速（drama S8）。"""
    from manju.drama import audit_files

    outline, cast, art, scripts, staging, board = _fixture()
    sc = scripts[0].scenes[0]
    sc.mood = "悲伤低沉"
    sc.beats[2].line = "这一次我不逃，必须马上走"

    files = compile_all(outline, cast, art, scripts, staging, board, tmp_path)
    data = json.loads(files["script_ep01"].read_text(encoding="utf-8"))
    scene = data["scenes"][0]

    assert scene["speech_cps"] < 4.5
    line = scene["lines"][0]
    assert line["timing_sec"] >= len(line["text"]) / scene["speech_cps"]

    rep = audit_files(
        [files["plan"], files["script_ep01"], files["storyboard_ep01"]],
        files["director_ep01"], target_sec=90)
    assert not [f for f in rep.blocks if f.code == "S8"]


def test_compile_line_timing_not_shortened_by_fast_scene_cps(tmp_path):
    """激动场次 speech_cps>标准语速时，timing 仍按标准给足，否则 gate block。"""
    from manju.gate import check_files

    outline, cast, art, scripts, staging, board = _fixture()
    sc = scripts[0].scenes[0]
    sc.mood = "亢奋激动"
    line = "真有面板！快抽天赋！"      # 10 字：标准语速需 3s，5.4cps 只需 2s
    sc.beats[2].line = line
    sc.beats[2].delivery = "亢奋地喊"
    board.episodes[0].segments[0].cuts[2].seconds = 3.0

    files = compile_all(outline, cast, art, scripts, staging, board, tmp_path)
    data = json.loads(files["script_ep01"].read_text(encoding="utf-8"))
    scene = data["scenes"][0]
    assert scene["speech_cps"] > 4.5
    ln = next(x for x in scene["lines"] if x["text"] == line)
    assert ln["timing_sec"] >= math.ceil(len(line) / 4.5)
    assert ln["timing_sec"] >= math.ceil(len(line) / scene["speech_cps"])

    rep = check_files([files["plan"], files["script_ep01"],
                       files["storyboard_ep01"]])
    assert not [f for f in rep.blocks if f.code == "time.line_too_long"]


# --------------------------------------------------------------------------
# 旁白 / 内心独白 / 场间转场
# --------------------------------------------------------------------------
def _with_narration_and_transitions():
    from manju.story.models import NarrationLine, SceneTransition

    outline, cast, art, scripts, staging, board = _fixture()
    ep = scripts[0].model_copy(deep=True)
    ep.scenes[0].narration = [NarrationLine(
        kind="exposition", text="三年前，渡口还没有这场雾。", beat=1)]
    ep.scenes[1].narration = [NarrationLine(
        kind="inner", text="她不能把箱子交出去。", beat=1, speaker="C1")]
    for sc in ep.scenes[1:]:
        sc.transition_in = SceneTransition(type="match_cut", anchor="旧皮箱")
    return outline, cast, art, ep, staging, board


def test_dialogue_metrics_detects_sparse_dialogue():
    from manju.story.stages import _dialogue_metrics

    sparse = "风从河面吹来，她提着箱子往前走。" * 30
    assert _dialogue_metrics(sparse)["dialogue_light"] is True

    rich = "“我们必须走了。”" * 40
    assert _dialogue_metrics(rich)["dialogue_light"] is False


def test_script_budget_low_dialogue_shifts_to_action_and_narration():
    normal = _script_budget(90)
    light = _script_budget(90, dialogue_light=True)

    assert light["suggest_lines"] < normal["suggest_lines"]
    assert light["target_chars"] < normal["target_chars"]
    assert light["coverage_required"]["reaction"] > normal["coverage_required"]["reaction"]
    assert light["narration_limits"]["exposition"] == 2
    assert light["narration_limits"]["inner"] == 3


def test_script_gate_accepts_overlay_narration_and_scene_transitions():
    outline, cast, art, ep, _, _ = _with_narration_and_transitions()

    rep = validate_script(ep, outline, cast, art, enforce_transitions=True)
    assert rep.ok, rep.text()
    assert "缺 transition_in" not in rep.text()

    variants = ep.model_copy(deep=True)
    for sc in variants.scenes:
        sc.transition_in = None
    text = validate_script(variants, outline, cast, art,
                           enforce_transitions=True).text()
    assert "缺 transition_in" in text


def test_narration_must_overlay_action_and_inner_speaker_must_be_on_scene():
    from manju.story.models import NarrationLine

    outline, cast, art, ep, _, _ = _with_narration_and_transitions()

    bad = ep.model_copy(deep=True)
    bad.scenes[0].narration = [NarrationLine(
        kind="exposition", text="旁白不能盖台词。", beat=3)]
    text = validate_script(bad, outline, cast, art).text()
    assert "不能叠在台词拍上" in text

    bad2 = ep.model_copy(deep=True)
    bad2.scenes[0].narration = [NarrationLine(
        kind="inner", text="她心里一沉。", beat=1, speaker="C9")]
    text2 = validate_script(bad2, outline, cast, art).text()
    assert "内心独白 speaker C9" in text2


def test_compile_emits_narration_and_transition():
    from manju.schemas import Script as ContractScript, Storyboard
    from manju.story.compile import compile_script, compile_storyboard

    outline, cast, art, ep, staging, storyboard = _with_narration_and_transitions()
    board = storyboard.episodes[0].model_copy(deep=True)
    for seg in board.segments:
        if seg.scene_index > 1:
            seg.transition_in = ep.scenes[seg.scene_index - 1].transition_in
    params = ScriptParams()

    script = compile_script(ep, outline, cast, art, staging, params,
                            "9:16", board)
    ContractScript.model_validate(script)
    # v10.3：script 层旁白恒空（唯一权威在 storyboard.shots[].narration）
    assert script["scenes"][0]["narration"] == []
    assert script["scenes"][1]["transition_in"]["type"] == "match_cut"

    story, director = compile_storyboard(
        ep, board, art, cast, staging, outline, params, "9:16")
    Storyboard.model_validate(story)
    narr = story["shots"][0]["narration"][0]
    assert narr["kind"] == "exposition"
    # v10.3 锚点字段
    assert narr["shot_id"] == story["shots"][0]["shot_id"]
    assert narr["anchor_mode"] in ("within_shot", "voiceover_span")
    assert narr["at_sec"] is not None
    assert narr["timing_sec"] is not None
    assert narr["allow_span_shots"] >= 1
    assert narr["allow_cross_scene"] is False
    scene2 = [s for s in story["shots"] if s["scene_id"] == "S2"]
    assert scene2[0]["transition_in"]["type"] == "match_cut"
    # v10.3：director 场景级旁白同样留空（唯一权威在 shot 层）
    assert director["scenes"][0]["narration"] == []


def test_gate_and_drama_accept_compiled_narration(tmp_path):
    from manju.drama import audit_files
    from manju.gate import check_files
    from manju.story.compile import compile_all
    from manju.story.models import StoryStoryboard

    outline, cast, art, ep, staging, storyboard = _with_narration_and_transitions()
    board = storyboard.episodes[0].model_copy(deep=True)
    for seg in board.segments:
        if seg.scene_index > 1:
            seg.transition_in = ep.scenes[seg.scene_index - 1].transition_in
    storyboard = StoryStoryboard(
        source=storyboard.source, aspect_ratio="9:16", episodes=[board])

    files = compile_all(outline, cast, art, [ep], staging, storyboard, tmp_path)
    paths = [files["plan"], files["script_ep01"], files["storyboard_ep01"]]
    gate_rep = check_files(paths)
    assert not gate_rep.blocks, gate_rep.to_text()

    drama_rep = audit_files(paths, files["director_ep01"], target_sec=90)
    assert drama_rep.ok(), drama_rep.to_text()


def _group_first_cut(board, allow_multi_face=False):
    """把第一镜改成 C1/C2 同框，用于验证多人策略编译。"""
    board = board.model_copy(deep=True)
    cut = board.episodes[0].segments[0].cuts[0]
    cut.characters = ["C1", "C2"]
    cut.subjects = [
        CutSubject(char="C1", wardrobe_state="C1-default",
                   pose="standing", expression="calm"),
        CutSubject(char="C2", wardrobe_state="C2-default",
                   pose="standing opposite", expression="cold"),
    ]
    cut.allow_multi_face = allow_multi_face
    return board


def test_compile_declares_non_auto_multi_subject_strategy():
    """同框 ≥2 人必须写死策略；群像允许同框多脸时用 hybrid。"""
    from manju.story.compile import compile_storyboard

    outline, cast, art, scripts, staging, board = _fixture()
    params = ScriptParams()

    normal = _group_first_cut(board)
    story, _ = compile_storyboard(scripts[0], normal.episodes[0], art, cast,
                                  staging, outline, params, "9:16")
    assert story["shots"][0]["requirements"]["multi_subject_strategy"] == "joint"

    crowd = _group_first_cut(board, allow_multi_face=True)
    story, _ = compile_storyboard(scripts[0], crowd.episodes[0], art, cast,
                                  staging, outline, params, "9:16")
    assert story["shots"][0]["requirements"]["multi_subject_strategy"] == "hybrid"


def test_gate_blocks_auto_strategy_for_group_shot(tmp_path):
    """plan 级 auto 不能再给同框镜头兜底。"""
    from manju.gate import check_files
    from manju.story.compile import compile_all

    outline, cast, art, scripts, staging, board = _fixture()
    board = _group_first_cut(board)
    files = compile_all(outline, cast, art, scripts, staging, board, tmp_path)

    report = check_files([files["plan"], files["script_ep01"],
                          files["storyboard_ep01"]])
    assert "struct.multi_subject_undeclared" not in report.codes("block")

    story = json.loads(files["storyboard_ep01"].read_text(encoding="utf-8"))
    story["shots"][0]["requirements"] = {"multi_subject_strategy": "auto"}
    bad = tmp_path / "storyboard_auto.json"
    bad.write_text(json.dumps(story, ensure_ascii=False), encoding="utf-8")

    report = check_files([files["plan"], files["script_ep01"], bad])
    assert "struct.multi_subject_undeclared" in report.codes("block")


def test_compile_assets_scene_refs_follow_compiled_scene_ids(tmp_path):
    """场次 id（S1..Sn）与地点资产 id 是两套命名空间，必须显式映射。"""
    from manju.story.compile import compile_all

    outline, cast, art, scripts, staging, board = _fixture()
    ep = scripts[0]
    ep.scenes[1].id = ep.scenes[0].id       # 两场复用同一地点

    files = compile_all(outline, cast, art, scripts, staging, board, tmp_path)
    assets = json.loads(files["assets"].read_text(encoding="utf-8"))
    refs = assets["scene_refs"]["1"]

    assert refs["S1"] == "S1"
    assert refs["S2"] == "S1"               # 复用地点，不是资产 S2
    assert set(refs) == {f"S{i}" for i in range(1, len(ep.scenes) + 1)}
    assert set(refs.values()) <= set(assets["scenes"])


def test_compile_dialogue_duration_survives_8n1_snapping(tmp_path):
    """12 字台词在 24fps/8n+1 下需要 73 帧，不能只按 2.67s 算。"""
    from manju.gate import check_files
    from manju.story.compile import _dialogue_safe_seconds, _next_8n1, compile_all

    line = "你到底是谁别在这里装傻了"
    assert len(line) == 12
    assert _next_8n1(66) == 73
    assert _dialogue_safe_seconds(line, 4.5) == pytest.approx(73 / 24)

    outline, cast, art, scripts, staging, board = _fixture()
    ep = scripts[0]
    ep.scenes[0].beats[2] = ep.scenes[0].beats[2].model_copy(
        update={"line": line})
    # 第一场第 3 拍对应的镜头：3.2s 上限内必须被抬到 >=73 帧。
    files = compile_all(outline, cast, art, scripts, staging, board, tmp_path)
    story = json.loads(files["storyboard_ep01"].read_text(encoding="utf-8"))
    shot = next(s for s in story["shots"]
                if s["scene_id"] == "S1" and "SH03" == s["shot_id"])
    assert shot["duration_sec"] >= 73 / 24

    report = check_files([files["plan"], files["script_ep01"],
                          files["storyboard_ep01"]])
    assert not [c for c in report.codes("warn")
                if c == "time.dialogue_exceeds_shot"], report.to_text()


def _run_repair_script(ep, outline, art):
    """跑剧本确定性修补，返回修复日志与 plan。"""
    from manju.story.repairs import _repair_script_output
    logs: list[str] = []
    plan = outline.episodes[0]
    _repair_script_output(ep, outline, art, logs.append, params=None)
    return logs, plan


def test_repair_key_line_variant_restored_to_verbatim():
    """key_lines 变体拍被确定性还原为原文（一字不改，保留说话人）。"""
    outline, cast, art, scripts, staging, board = _fixture()
    ep = scripts[0]
    plan = outline.episodes[0]
    key = ("我不知道堪萨斯州在哪里，从未听说过那个地方。"
           "但是请你告诉我，那儿是个开化的地方吗？")
    plan.key_lines = [key]
    # 模型把关键台词改写成了变体（只改个别字，保留绝大多数原文）
    variant = ("我不知道堪萨斯州在哪里，从未听说过那个地方。"
               "但是请你告诉我，那儿是开化的地方吗？")
    ep.scenes[0].beats[2] = ep.scenes[0].beats[2].model_copy(
        update={"line": variant})
    logs, _ = _run_repair_script(ep, outline, art)
    # 还原后 40 字超过 18 字/拍，会被确定性拆成连续拍；拼接后必须=原文
    joined = "".join(b.line for b in ep.scenes[0].beats if b.line)
    assert key in joined, joined
    assert any("关键台词变体还原为原文" in x for x in logs)


def test_repair_key_line_untouched_when_no_variant():
    """完全没有变体拍时兜底不动，质量门照常拦截（不瞎插拍）。"""
    outline, cast, art, scripts, staging, board = _fixture()
    ep = scripts[0]
    plan = outline.episodes[0]
    key = ("我不知道堪萨斯州在哪里，从未听说过那个地方。"
           "但是请你告诉我，那儿是个开化的地方吗？")
    plan.key_lines = [key]
    ep.scenes[0].beats[2] = ep.scenes[0].beats[2].model_copy(
        update={"line": "外面下雨了，赶紧把船划走，天亮前别回头"})
    logs, _ = _run_repair_script(ep, outline, art)
    joined = "".join(b.line for b in ep.scenes[0].beats if b.line)
    assert key not in joined
    assert not any("关键台词变体还原为原文" in x for x in logs)


def test_repair_key_line_long_line_splits_adjacent_chunks():
    """还原后的超长关键台词走确定性拆拍：连续拍拼接仍等于原文。"""
    outline, cast, art, scripts, staging, board = _fixture()
    ep = scripts[0]
    plan = outline.episodes[0]
    key = ("我不知道堪萨斯州在哪里，从未听说过那个地方。"
           "但是请你告诉我，那儿是个开化的地方吗？")
    plan.key_lines = [key]
    ep.scenes[0].beats[2] = ep.scenes[0].beats[2].model_copy(
        update={"line": "我不知道堪萨斯州在哪里，从没听过那个地方。"
                        "但是请你告诉我，那儿是个开化的地方吗？"})
    logs, _ = _run_repair_script(ep, outline, art)
    joined = "".join(b.line for b in ep.scenes[0].beats if b.line)
    assert key in joined, joined
    assert any("关键台词变体还原为原文" in x for x in logs)


def test_compile_strips_camera_words_from_prompt():
    """drama 门 B4 要求 prompt 不含运镜词；compile 必须确定性剥离。"""
    from manju.story.compile import _clean_prompt_camera

    src = ("Medium tracking shot inside the small bare farmhouse, "
           "a girl kneeling low beside an iron bedstead, "
           "the camera pushes in toward her face as she turns.")
    out = _clean_prompt_camera(src)
    low = out.lower()
    for w in ("tracking shot", "push in", "pan left", "pan right",
              "tilt up", "tilt down", "dolly", "camera move"):
        assert w not in low, (w, out)
    assert "shot inside the small bare farmhouse" in low
    # 句式保持：主题/地点/动作不被误删
    assert "girl kneeling low beside an iron bedstead" in low
    assert "she turns" in low
    # 空输入安全
    assert _clean_prompt_camera("") == ""
    assert _clean_prompt_camera(None) is None
    # 大小写变体（Tracking Shot）同样剥离
    assert "tracking" not in _clean_prompt_camera(
        "Tracking Shot of the door opening").lower()


def test_split_action_text_groups_verbs_within_engine_cap():
    """多动作拍按连词边界切成组，每组动作数 ≤ 上限。"""
    from manju.story.editorial import count_action_verbs
    from manju.story.repairs import _split_action_text

    text = "她抓碗扔进水盆，又转身扑向地板，再一把抱起小狗"
    assert count_action_verbs(text) >= 4  # 抓/扔/转身/扑/抱 均在保守词表
    for cap in (1, 2):
        chunks = _split_action_text(text, cap)
        assert len(chunks) >= 2
        for c in chunks:
            assert c.strip()
            assert count_action_verbs(c) <= cap, (cap, c)
        joined = "".join(chunks)
        for piece in ("抓碗扔进水盆", "转身扑向地板", "抱起小狗"):
            assert piece in joined
    # 单子句超限：硬切兜底不丢字
    hard = _split_action_text("她抓碗砸盆又捞狗再转身", 2)
    for c in hard:
        assert c.strip()
    assert "".join(hard).replace("，", "").replace(" ", "") == \
        "她抓碗砸盆又捞狗再转身"


def test_split_overloaded_action_beats_respects_budget_and_state():
    """拆拍降动作数到引擎上限内；状态/重点归末拍；集长硬上限保护。"""
    from manju.story.models import ScriptBeat, ScriptScene, ScriptEpisode
    from manju.story.repairs import _split_overloaded_action_beats

    p = ScriptParams(max_actions_per_shot=1, action_seconds=2.5,
                     tolerance=0.15)
    beats = [
        ScriptBeat(action="她抓碗扔进水盆，又转身扑向地板，再一把抱起小狗",
                   state_change={"C1": "接过狗"}, state_out={"C1": "center/抱狗"},
                   visual_focus="小狗", emphasis="climax", expression="急切"),
        ScriptBeat(speaker="C1", line="快走"),
        ScriptBeat(action="她冲向门口", state_out={"C1": "门口"}),
    ]
    scene = ScriptScene(id="S1", location="农舍", characters=["C1"], beats=beats)
    ep = ScriptEpisode(ep=1, target_seconds=30.0, title="t", hook="钩子",
                       cliff="结尾钩子", hook_beat=(1, 1), scenes=[scene])
    logs: list[str] = []
    _split_overloaded_action_beats(ep, p, logs.append)
    # 多动作拍拆成多拍（上限 1）
    assert len(scene.beats) > 3
    first = scene.beats[0]
    # 状态/重点/权重/表情只留在最后一拍
    assert first.state_change == {} and first.visual_focus == "" \
        and first.emphasis == "none" and first.expression == ""
    last = [b for b in scene.beats if b.action and b.emphasis == "climax"][0]
    assert last.state_change == {"C1": "接过狗"} and last.visual_focus == "小狗"
    assert any("拆为" in x for x in logs)
    # 预算保护：target 30s×1.15=34.5s，拆拍总时长不得超上限
    from manju.story.models import _scene_seconds
    total = _scene_seconds(scene, p)
    assert total <= 30.0 * 1.15 + 1e-6, total


def _seg_words(t: str) -> set[str]:
    import re as _re
    return set(_re.findall(r"[a-z]+", t.lower()))


def _jaccard(a: set[str], b: set[str]) -> float:
    if not a and not b:
        return 0.0
    return len(a & b) / len(a | b)


def _make_cut(prompt_en: str, beat_goal: str = "抱狗转身扑向地板") -> object:
    from types import SimpleNamespace
    return SimpleNamespace(
        prompt_en=prompt_en,
        beat_goal=beat_goal,
        shot="SH02",
        camera="medium shot",
        state_from="C1 站在屋门旁抱狗",
        state_out="C1 转身背对门口，小狗在怀中",
    )


def test_fallback_segments_semantic_rewrite_no_head_copy():
    """fallback 拆段=语义重组：段间独立动作句，禁止整段复制 prompt 开头。"""
    from manju.story.compile import _fallback_segments

    prompt = ("Medium shot inside the small farmhouse, a girl in a braid "
              "turning around with the dog in her arms, she lunges toward "
              "the door, she stumbles onto the floor, her face shows shock.")
    cut = _make_cut(prompt, "抱狗转身扑向地板摔倒")
    segs = _fallback_segments(cut, 9.0, 3)
    assert segs is not None and len(segs) == 3
    texts = [s["text"] for s in segs]
    # 1) 段间相似度必须显著低于旧实现（旧：Jaccard=1.00 三份全同）
    for i in range(3):
        for j in range(i + 1, 3):
            assert _jaccard(_seg_words(texts[i]), _seg_words(texts[j])) <= 0.40, \
                (i, j, texts[i], texts[j])
    # 2) 非首段 text 不得以整镜 prompt 前 15 词为前缀（旧实现截断复制特征；
    #    start 段天然继承基础定位句，允许）
    head15 = " ".join(prompt.split())[:15].lower()
    for t in texts[1:]:
        assert " ".join(t.split())[:15].lower() != head15, t
    # 3) 每段保留本段动作（独立动词句推进）
    from manju.story.compile import _SEG_ACTION_VERB
    for t in texts:
        assert _SEG_ACTION_VERB.search(t), t
    # 4) 段和=整镜、时间连续、末段 keyframe
    assert sum(s["duration_sec"] for s in segs) == 9.0
    assert segs[0]["time_range"][1] == segs[1]["time_range"][0]
    assert segs[1]["time_range"][1] == segs[2]["time_range"][0]
    assert segs[-1]["keyframe"] is True and segs[-1]["frame_hint"] == "last"
    # 5) 段间 text 不得相同
    assert len({t for t in texts}) == 3
    # 单动作镜：不拆段（向后兼容）
    assert _fallback_segments(_make_cut("a dog sitting by the door"), 5.0, 1) is None


def test_fallback_segments_no_action_loss_when_more_actions_than_segments():
    """动作数 > 段数（4 动作拆 3 段）时，动作从句不得被丢弃（剧情表达完整）。"""
    from manju.story.compile import _fallback_segments, _SEG_ACTION_VERB

    prompt = ("Medium shot in the yard, she grabs the bowl and throws it into "
              "the basin, she turns and lunges toward the floor, she scoops up "
              "the puppy, she rushes to the door, dust swirls around her.")
    cut = _make_cut(prompt, "抓碗扔盆转身扑地抱狗冲向门口")
    segs = _fallback_segments(cut, 12.0, 4)
    assert segs is not None and len(segs) == 3
    joined = " ".join(s["text"] for s in segs).lower()
    for verb in ("grabs", "throws", "turns", "lunges", "scoops", "rushes"):
        assert verb in joined, (verb, joined)
    # 每段都有动作（无空段/占位段）
    for s in segs:
        assert _SEG_ACTION_VERB.search(s["text"]), s["text"]
    # 段和仍=整镜
    assert sum(s["duration_sec"] for s in segs) == 12.0


def test_prompt_quality_gate_blocks_copy_paste_segments(tmp_path):
    """质量门：旧实现（段=复制整镜 prompt 开头）必须被 block。"""
    from scripts.check_prompt_quality import check_board

    prompt = ("Medium shot inside the small farmhouse, a girl in a braid "
              "turning around with the dog in her arms, she lunges toward "
              "the door, she stumbles onto the floor.")
    board = {"shots": [{
        "shot_id": "SH02", "prompt": prompt,
        "segments": [
            {"text": prompt[:80]},          # 旧实现：截断复制
            {"text": prompt[:80]},
            {"text": prompt[:80]},
        ],
    }]}
    path = tmp_path / "bad.json"
    path.write_text(json.dumps(board, ensure_ascii=False), encoding="utf-8")
    findings, code = check_board(path)
    assert code == 1  # 有 block
    codes = {f["code"] for f in findings}
    assert "seg.prompt_head_copy" in codes
    assert "seg.dup" in codes


def test_prompt_quality_gate_passes_semantic_fallback(tmp_path):
    """质量门正例：新 fallback 语义拆段产物应 PASS（0 block）。"""
    from manju.story.compile import _fallback_segments
    from scripts.check_prompt_quality import check_board

    prompt = ("Medium shot inside the small farmhouse, a girl in a braid "
              "turning around with the dog in her arms, she lunges toward "
              "the door, she stumbles onto the floor, her face shows shock.")
    segs = _fallback_segments(_make_cut(prompt, "抱狗转身扑向地板摔倒"), 9.0, 3)
    board = {"shots": [{
        "shot_id": "SH02", "prompt": prompt, "segments": segs,
    }]}
    path = tmp_path / "good.json"
    path.write_text(json.dumps(board, ensure_ascii=False), encoding="utf-8")
    findings, code = check_board(path)
    assert code == 0, [f for f in findings if f["level"] == "block"]


def test_board_rules_photography_and_verbatim():
    """P0.5/P1：_board_rules 必须包含摄影配方与锚点 verbatim 硬性规则。"""
    rules = _board_rules(ScriptParams())
    assert "【摄影配方·硬性】" in rules
    assert "不给光照 = 引擎默认平光" in rules or "不给光照=引擎默认平光" in rules
    assert "焦段" in rules and "景深" in rules
    assert "【锚点 verbatim·硬性】" in rules
    assert "逐字复用" in rules
    assert "禁止自行改写" in rules


def test_prompt_quality_gate_recipe_warns(tmp_path):
    """质量门 P0.5：缺摄影配方的整镜 prompt 报 warn（不 block），
    含光照/镜头词的 prompt 不报配方 warn。"""
    from scripts.check_prompt_quality import check_board

    board = {"shots": [
        {"shot_id": "SH01", "prompt": (
            "Wide shot of the Kansas prairie, the cyclone looms, "
            "a girl stands before the farmhouse.")},
        {"shot_id": "SH02", "prompt": (
            "Medium shot, a girl in a braid turning with the dog, "
            "light from screen-left window, soft grey daylight, cool, "
            "50mm shallow depth of field.")},
    ]}
    path = tmp_path / "recipes.json"
    path.write_text(json.dumps(board, ensure_ascii=False), encoding="utf-8")
    findings, code = check_board(path)
    assert code == 0  # 均为 warn
    wcodes = {f["code"] for f in findings if f["level"] == "warn"}
    assert "prompt.no_light_recipe" in wcodes
    assert "prompt.no_lens_recipe" in wcodes
    # SH01 两个配方 warn，SH02 无配方 warn
    light_warns = [f for f in findings if f["code"] == "prompt.no_light_recipe"]
    lens_warns = [f for f in findings if f["code"] == "prompt.no_lens_recipe"]
    assert len(light_warns) == 1 and light_warns[0]["where"] == "SH01"
    assert len(lens_warns) == 1 and lens_warns[0]["where"] == "SH01"


def test_prompt_quality_gate_camera_recipe_slot_warns(tmp_path):
    """质量门 P0.5：产物声明 framing 但 camera_recipe 缺失/全空 → warn；
    填了 lens_mm/light_key 的不报该 warn；旧产物无 framing 不报。"""
    from scripts.check_prompt_quality import check_board

    board = {"shots": [
        {"shot_id": "SH01", "prompt": "x", "framing": {
            "9:16": {"shot_size": "medium"}, "16:9": {"shot_size": "medium"}}},
        {"shot_id": "SH02", "prompt": "x", "framing": {
            "9:16": {"shot_size": "close",
                     "camera_recipe": {"lens_mm": "50mm", "light_key": "soft warm key"}},
            "16:9": {"shot_size": "close",
                     "camera_recipe": {"lens_mm": "50mm", "light_key": "soft warm key"}}}},
        {"shot_id": "SH03", "prompt": "x"},  # 旧产物，无 framing
    ]}
    path = tmp_path / "slot.json"
    path.write_text(json.dumps(board, ensure_ascii=False), encoding="utf-8")
    findings, code = check_board(path)
    assert code == 0
    warns = [f for f in findings if f["code"] == "prompt.no_camera_recipe"]
    assert len(warns) == 1 and warns[0]["where"] == "SH01"


def test_prompt_quality_gate_director_word_and_fluff_warns(tmp_path):
    """质量门 2026-11-02 自检：prompt_en 混入拍摄调度词（dialogue shot / POV）
    与空泛修饰词（vivid）→ warn（不 block）；景别短语开头（Medium shot /
    Extreme close / Close insert）不算调度词。"""
    from scripts.check_prompt_quality import check_board

    board = {"shots": [
        {"shot_id": "SH55", "prompt": (
            "Medium dialogue shot: the elderly woman at frame left lowers "
            "her gaze, silver-starred robe bright, hard noon light, "
            "50mm shallow depth of field.")},
        {"shot_id": "SH17", "prompt": (
            "POV: the cellar wall of packed earth, one pale root hair, "
            "grey light from above, 35mm deep focus.")},
        {"shot_id": "SH41", "prompt": (
            "Extreme wide point-of-view shot through the doorway onto a "
            "vivid meadow, saturated green, 24mm deep focus.")},
        {"shot_id": "SH05", "prompt": (
            "Close insert of heavy mud-crusted boots slamming down, "
            "hard grey light, 50mm macro.")},
    ]}
    path = tmp_path / "director.json"
    path.write_text(json.dumps(board, ensure_ascii=False), encoding="utf-8")
    findings, code = check_board(path)
    assert code == 0  # 全部 warn，不 block
    dw = [f for f in findings if f["code"] == "prompt.director_word"]
    fw = [f for f in findings if f["code"] == "prompt.fluff"]
    # SH55 dialogue shot、SH17 POV、SH41 point-of-view shot 命中；
    # SH05 "Close insert" 是景别复合开头，不算调度词
    assert {f["where"] for f in dw} == {"SH55", "SH17", "SH41"}
    # SH41 vivid 命中；SH55/17/05 无空泛词
    assert {f["where"] for f in fw} == {"SH41"}


def test_camera_recipe_and_perf_libraries_are_optional_slots():
    """P0.5/P1：CameraRecipe（framing.camera_recipe）与 Character 的
    pose_library / expression_library 均为可选字段（旧产物可读）。"""
    from manju.schemas import CameraRecipe, Character, Framing
    from manju.story.models import Art, Cast, Outline  # noqa: F401（保证导入链）

    f = Framing.model_validate(
        {"shot_size": "close_up", "composition": "居中"})
    assert f.camera_recipe is None
    f2 = Framing.model_validate(
        {"shot_size": "close_up", "composition": "居中",
         "camera_recipe": {"lens_mm": "85mm", "depth_of_field": "shallow"}})
    assert f2.camera_recipe.lens_mm == "85mm"

    c = Character.model_validate({
        "char_id": "C9", "name": "测试", "desc_cn": "x", "desc_en": "x",
        "identity_lock": {"face_shape": "f", "hair": "h", "eyes": "e",
                          "build": "b", "facial_marks": ""}})
    assert c.pose_library == [] and c.expression_library == []
    c2 = Character.model_validate({
        "char_id": "C9", "name": "测试", "desc_cn": "x", "desc_en": "x",
        "identity_lock": {"face_shape": "f", "hair": "h", "eyes": "e",
                          "build": "b", "facial_marks": ""},
        "pose_library": ["hands clasped at chest"],
        "expression_library": ["brow furrowed, lips tight"]})
    assert c2.pose_library[0] == "hands clasped at chest"
