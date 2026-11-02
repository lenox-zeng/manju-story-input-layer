# -*- coding: utf-8 -*-
"""情节驱动分集：原子切片 / 设计修复 / must_show 与关键道具门。"""
from __future__ import annotations

from types import SimpleNamespace

from manju.story.episodes import EpisodeSlice
from manju.story.ingest import parse_text
from manju.story.models import (
    ActionBeatLine, ActionBeat, CutPlan, CutState, DesignEpisode, EpisodePlan,
    ScriptBeat, ScriptEpisode, ScriptParams, ScriptScene, SeriesDesign,
    StoryCut, SegmentPlan,
)
from manju.story.plot import (
    build_plot_atoms, design_slices, prefix_cut, repair_design_in_place,
    validate_series_design,
)
from manju.story.stages import (
    StoryPipeline, _apply_design_to_plans, _assemble_segment, _focus_excerpt,
    _range_source, _repair_script_plot, _repair_script_output,
)
from manju.story.validate import validate_script


def _novel():
    chapters = []
    for i in range(1, 7):
        body = f"第{i}章 标题{i}\n" + ("多萝西沿着黄砖路向前走，稻草人跟在她身后。" * 60)
        chapters.append(body)
    return parse_text("\n".join(chapters), cleanup=True)


def test_prefix_cut_moves_to_a_sentence_or_line_boundary():
    assert prefix_cut("甲" * 4998 + "。" + "乙" * 100, 5000) == 4999
    assert prefix_cut("甲" * 5000 + "\n" + "乙" * 100, 5000) == 5001
    # 窗口内找不到边界时保持原上限，不能为了找句号无限扩读。
    assert prefix_cut("甲" * 6000, 5000) == 5000
    assert prefix_cut("短文本", 5000) == 3


def test_atoms_cover_full_text_without_gaps():
    novel = _novel()
    atoms = build_plot_atoms(novel, atom_chars=1000, max_atoms=50,
                             excerpt_chars=200, total_excerpt_chars=5000)
    assert len(atoms) >= 2
    assert atoms[0].start == 0
    assert atoms[-1].end == len(novel.text)
    for a, b in zip(atoms, atoms[1:]):
        assert a.end == b.start          # 不重叠、不跳段
    assert list(range(1, len(atoms) + 1)) == [a.index for a in atoms]


def _design(n: int = 3) -> SeriesDesign:
    must = ["多萝西被龙卷风卷走", "她落在陌生村庄", "银鞋在泥地上发光"]
    eps = []
    for i in range(1, n + 1):
        eps.append(DesignEpisode(
            ep=i, title=f"第{i}集", atom_ids=[i],
            synopsis=f"第{i}段事件发生，主角做出选择并承担后果。",
            must_show=[must[i - 1], must[(i % 3)]],
            key_props=["银鞋"] if i == 1 else [],
            dramatic_question=f"第{i}集她会怎么选？",
            turn="她决定继续走", climax="银鞋发光", hook="龙卷风掠过",
            cliff="前方出现新的阻拦"))
    return SeriesDesign(source="x", logline="回家", episodes=eps)


def test_repair_design_fills_gaps_and_maps_back_to_source():
    novel = _novel()
    atoms = build_plot_atoms(novel, atom_chars=1000, max_atoms=50,
                             excerpt_chars=200, total_excerpt_chars=5000)
    design = _design(3)
    design.episodes[0].atom_ids = [2]
    design.episodes[1].atom_ids = [4]
    design.episodes[2].atom_ids = [len(atoms)]
    design.episodes[0].must_show = [""]      # 空条目 → 从梗概拆事件兜底
    repair_design_in_place(design, len(atoms))

    assert [d.ep for d in design.episodes] == [1, 2, 3]
    flat = [i for d in design.episodes for i in d.atom_ids]
    assert flat == list(range(1, len(atoms) + 1))
    assert design.episodes[0].must_show             # 空事件从梗概兜底

    rep = validate_series_design(design, len(atoms), seconds=90)
    assert rep.ok, rep.text()
    slices = design_slices(novel, atoms, design)
    assert slices[0].start == 0
    assert slices[-1].end == len(novel.text)
    for a, b in zip(slices, slices[1:]):
        assert a.end == b.start


def test_validate_design_reports_missing_atoms():
    design = _design(3)
    design.episodes[0].atom_ids = [1]
    design.episodes[1].atom_ids = [3]      # 跳过 2
    design.episodes[2].atom_ids = [4]
    rep = validate_series_design(design, 4)
    assert not rep.ok
    assert "缺失" in rep.text()


def _plan() -> EpisodePlan:
    return EpisodePlan(
        ep=1, title="龙卷风", synopsis="多萝西被卷走。",
        hook="风来了", cliff="门被吹开", scene_ids=["S1"],
        character_ids=["C1"], prop_ids=["P1"],
        must_show=["多萝西被龙卷风卷走", "旧皮箱被吹开"],
        key_props=["旧皮箱"],
        dramatic_question="她能活下来吗？", turn="房子飞起来",
        climax="龙卷风撕开屋顶", estimated_seconds=90)


def _script() -> ScriptEpisode:
    scene = ScriptScene(
        id="S1", location="小屋", time="白天", lighting="grey storm light",
        characters=["C1"], props=["P1"], wardrobe={"C1": "C1-default"},
        goal="躲进地窖", obstacle="门锁住", turn="风把门吹开", cost="无",
        information="风暴提前到了", beat="激励",
        beats=[
            ScriptBeat(action="多萝西抓住旧皮箱的提手，向地窖口跑。",
                       coverage="action", state_from="站在窗边",
                       screen_side="center"),
            ScriptBeat(action="旧皮箱的铜锁在风里弹开，箱盖翻起。",
                       coverage="insert", state_from="提着箱子",
                       screen_side="center"),
            ScriptBeat(action="龙卷风把木门连框拔起，碎木扑面而来。",
                       coverage="action", state_from="站在地窖口",
                       screen_side="center"),
        ])
    return ScriptEpisode(
        ep=1, title="龙卷风", target_seconds=90, hook="风来了",
        cliff="门被吹开", hook_beat=(1, 1),
        scenes=[scene])


def _cast():
    return SimpleNamespace(characters=[SimpleNamespace(
        id="C1", wardrobe_states=[SimpleNamespace(state_id="C1-default")])])


def _art():
    return SimpleNamespace(
        scenes=[SimpleNamespace(id="S1")],
        props=[SimpleNamespace(id="P1", name="旧皮箱")])


def _outline(plan: EpisodePlan):
    return SimpleNamespace(
        characters=_cast().characters,
        scenes=[SimpleNamespace(id="S1")],
        beats=[], episodes=[plan])


def test_repair_script_plot_marks_key_prop_and_causal_chain():
    plan, ep = _plan(), _script()
    logs: list[str] = []
    _repair_script_plot(ep, plan, _art(), logs.append)

    assert ep.must_show == plan.must_show
    assert ep.key_props == plan.key_props
    assert ep.scenes[0].causal_from           # 开场承接
    key_beats = [b for b in ep.scenes[0].beats if b.emphasis == "key_prop"]
    assert key_beats and key_beats[0].visual_focus == "旧皮箱"
    assert key_beats[0].coverage == "insert"
    assert any("关键道具" in m for m in logs)


def test_repair_marks_the_beat_that_actually_shows_must_show():
    """强调拍要落在演出关键事件的那一拍，而不是场次第一拍。"""
    plan = _plan()
    plan.must_show = ["铜锁弹开"]
    plan.key_props = []
    ep = _script()
    sc = ep.scenes[0]
    sc.major_event = "铜锁弹开"
    sc.beats = [
        ScriptBeat(action="风从窗缝里灌进来。", coverage="action",
                   state_from="窗边", screen_side="center"),
        ScriptBeat(action="旧皮箱的铜锁弹开，箱盖翻起。", coverage="action",
                   state_from="提着箱子", screen_side="center"),
        ScriptBeat(action="多萝西抬头看向门外。", coverage="reaction",
                   state_from="站在屋里", screen_side="center"),
    ]
    art = SimpleNamespace(scenes=[SimpleNamespace(id="S1")], props=[])
    _repair_script_plot(ep, plan, art, lambda _m: None)

    marked = [b for b in sc.beats if b.emphasis == "highlight"]
    assert marked and "铜锁" in marked[0].action


def test_each_must_show_gets_its_own_moving_emphasis_beat():
    """同一场承载两条必拍时，两条都要有强调拍，不能只标第一条。"""
    plan = _plan()
    plan.must_show = ["铜锁弹开", "木门被吹开"]
    plan.key_props = []
    ep = _script()
    sc = ep.scenes[0]
    sc.major_event = "铜锁弹开"
    sc.beats = [
        ScriptBeat(action="旧皮箱的铜锁弹开，箱盖翻起。", coverage="insert",
                   state_from="提着箱子", screen_side="center"),
        ScriptBeat(action="木门被吹开，碎木扑面而来。",
                   coverage="action", state_from="站在屋里", screen_side="center"),
        ScriptBeat(action="多萝西抬手挡住碎木。", coverage="reaction",
                   state_from="站在屋里", screen_side="center"),
    ]
    art = SimpleNamespace(scenes=[SimpleNamespace(id="S1")], props=[])
    _repair_script_plot(ep, plan, art, lambda _m: None)

    marked = [b for b in sc.beats if b.emphasis != "none"]
    assert len(marked) >= 2
    focuses = " ".join(b.visual_focus for b in marked)
    assert "铜锁" in focuses and "门" in focuses


def test_missing_key_prop_gets_fallback_insert_instead_of_failing_run():
    """剧本漏写设计指定的关键道具时，确定性补插入特写，而不是硬门反复失败。"""
    plan, ep = _plan(), _script()
    plan.key_props = ["银鞋"]
    art = SimpleNamespace(
        scenes=[SimpleNamespace(id="S1")],
        props=[SimpleNamespace(id="P1", name="旧皮箱"),
               SimpleNamespace(id="P2", name="银鞋")])
    logs: list[str] = []
    _repair_script_plot(ep, plan, art, logs.append)

    sc = ep.scenes[0]
    assert "P2" in sc.props
    marked = [b for b in sc.beats
              if b.emphasis == "key_prop" and b.visual_focus == "银鞋"]
    assert marked and marked[0].coverage == "insert"
    assert any("未在剧本文字中出现" in m for m in logs)


def test_key_emphasis_forces_closeup_and_camera_move():
    scene = _script().scenes[0]
    scene.beats[1].emphasis = "key_prop"
    scene.beats[1].visual_focus = "旧皮箱"
    plan = SegmentPlan(scene_index=1, cuts=[
        CutPlan(beats=(1, 1), size="wide", camera="Static Shot"),
        # EDL 故意漏写道具：装配层要按 visual_focus 自动补回 P1
        CutPlan(beats=(2, 2), size="wide", camera="Static Shot"),
        CutPlan(beats=(3, 3), size="medium", camera="Static Shot"),
    ])
    line = ActionBeatLine(scene_index=1, beats=[
        ActionBeat(beats=(1, 1), goal="跑向地窖", from_prev="窗边",
                   action="多萝西抓住皮箱跑", to_next="箱子锁弹开",
                   who="C1", where="center", facing="north",
                   screen_side="center"),
        ActionBeat(beats=(2, 2), goal="道具特写", from_prev="提着箱子",
                   action="锁扣弹开", to_next="箱盖翻起",
                   who="", where="center", facing="north",
                   screen_side="center"),
        ActionBeat(beats=(3, 3), goal="风暴爆发", from_prev="站在地窖口",
                   action="门被吹开", to_next="碎木扑面",
                   who="C1", where="center", facing="north",
                   screen_side="center"),
    ])
    seg = _assemble_segment(plan, scene, line, CutState(), [], ScriptParams(),
                            seg_id="E01-01", scene_index=1, cast=_cast(),
                            art=_art())
    cut = seg.cuts[1]
    assert cut.emphasis == "key_prop"
    assert cut.visual_focus == "旧皮箱"
    assert cut.size == "close"
    assert cut.camera == "Push In"
    assert cut.props == ["P1"]              # 道具参考图不会丢


class _DesignLLM:
    """返回一个故意跳原子的设计；修复层必须把全文补成连续切片。"""

    def __init__(self):
        self.calls = 0
        self.last_payload: dict = {}

    def structured(self, stage, system, payload, *, model_cls, **_kw):
        self.calls += 1
        self.last_payload = payload
        n = len(payload["atoms"])
        return SeriesDesign(source="x", logline="回家", episodes=[
            DesignEpisode(
                ep=1, title="第一单元", atom_ids=[1],
                synopsis="多萝西沿着黄砖路向前走，稻草人跟在她身后。",
                must_show=["多萝西沿着黄砖路向前走",
                          "稻草人跟在她身后"],
                key_props=[], turn="她决定继续走", climax="黄砖路伸向远方",
                hook="龙卷风掠过", cliff="前方出现新的阻拦",
                estimated_seconds=90),
            DesignEpisode(
                ep=2, title="第二单元", atom_ids=[n],
                synopsis="多萝西沿着黄砖路向前走，稻草人跟在她身后。",
                must_show=["多萝西沿着黄砖路向前走",
                          "稻草人跟在她身后"],
                key_props=[], turn="她决定继续走", climax="黄砖路伸向远方",
                hook="继续赶路", cliff="前方出现新的阻拦",
                estimated_seconds=90),
        ])


def test_design_series_pipeline_repairs_and_partitions(tmp_path):
    log: list[str] = []
    pipe = StoryPipeline(llm=_DesignLLM(), workdir=tmp_path,
                         progress=log.append)
    design, slices = pipe.design_series(_novel().text, 90)

    assert [d.ep for d in design.episodes] == [1, 2]
    flat = [i for d in design.episodes for i in d.atom_ids]
    assert flat == list(range(1, max(flat) + 1))     # 不重叠、不跳段
    assert slices[0].start == 0
    assert slices[-1].end == len(_novel().text)
    for a, b in zip(slices, slices[1:]):
        assert a.end == b.start
    assert pipe.design is design


def test_design_series_prefix_only_reads_what_it_will_generate(tmp_path):
    """--to-episode N 只设计前 N 集：长篇不为后文付 token，也不稀释前几集。"""
    text = _novel().text
    llm = _DesignLLM()
    pipe = StoryPipeline(llm=llm, workdir=tmp_path, progress=lambda _m: None)
    design, slices = pipe.design_series(text, 90, max_episodes=1)

    assert len(design.episodes) == 1
    assert len(slices) == 1
    assert slices[0].end < len(text)          # 前缀而非全书
    scope = llm.last_payload["scope"]
    assert scope["partial_source"] is True
    assert scope["max_episodes"] == 1
    # 硬截断会挪到最近的句末/换行（前 200 / 后 400 字内），避免从半句话开始。
    assert 4800 <= scope["source_chars"] <= 5400


def test_range_source_reads_only_the_prefix_when_starting_at_ep1():
    text = "A" * 100 + "B" * 100
    slices = [EpisodeSlice(1, 0, 100, (), text[:100]),
              EpisodeSlice(2, 100, 200, (), text[100:])]
    # 只做前 1 集：压缩阶段不必读后 100 字
    assert _range_source(text, slices, (1, 1)) == text[:100]
    # 从第 2 集开始：需要全书背景，不能截
    assert _range_source(text, slices, (2, 2)) == text
    assert _range_source(text, slices, None) == text


def test_focus_excerpt_keeps_must_show_passage_in_middle():
    text = ("开头铺垫。" * 300
            + "多萝西在河边捡起银鞋，鞋面映出绿光。"
            + "结尾收束。" * 300)
    out = _focus_excerpt(text, ["多萝西捡起银鞋"], 300)
    assert "银鞋" in out                    # 中段关键事件没有被头尾摘录挤掉
    assert len(out) < 600


def test_design_key_prop_is_linked_to_bible_id_or_created():
    plan = _plan()
    plan.key_props = []
    plan.prop_ids = []
    bible = SimpleNamespace(props=[
        SimpleNamespace(id="P1", name="旧皮箱"),
        SimpleNamespace(id="P2", name="黄砖路"),
    ])
    d = DesignEpisode(
        ep=1, title="龙卷风", atom_ids=[1], synopsis="多萝西被卷走。",
        must_show=["多萝西被龙卷风卷走", "旧皮箱被吹开"],
        key_props=["旧皮箱", "银鞋"], cliff="门被吹开")

    _apply_design_to_plans([plan], {1: d}, 90, bible=bible)

    # 已有道具走 id 映射；小说原名对不上时补建，绝不能让它被“未使用”裁掉
    assert "P1" in plan.prop_ids
    assert any(p.name == "银鞋" for p in bible.props)
    silver = next(p for p in bible.props if p.name == "银鞋")
    assert silver.id in plan.prop_ids


def test_script_gate_requires_must_show_and_key_prop_closeup():
    plan, ep = _plan(), _script()
    # 清掉必拍认领与关键道具特写：门应同时报两类错
    ep.scenes[0].major_event = ""
    for b in ep.scenes[0].beats:
        b.emphasis = "none"
        b.visual_focus = ""
    ep.scenes[0].beats[0].action = "多萝西向地窖口跑。"
    ep.scenes[0].beats[1].action = "门锁发出轻响。"
    text = validate_script(ep, _outline(plan), _cast(), _art()).text()
    assert "必备情节没有落到任何一场" in text
    assert "没有任何 key_prop 特写拍" in text


# --------------------------------------------------------------------------
# v2.0 质量审计脚本（只读 JSON，不依赖 LLM）
# --------------------------------------------------------------------------
def _write_audit_run(tmp_path, *, broken: bool = False) -> None:
    import json

    outline = {
        "source": "样书",
        "episodes": [{
            "ep": 1, "title": "龙卷风",
            "must_show": ["多萝西被龙卷风卷走", "银鞋出现"],
            "key_props": ["银鞋"],
            "turn": "房子被卷起", "climax": "银鞋滚出",
        }],
    }
    script = {
        "ep": 1,
        "must_show": ["多萝西被龙卷风卷走", "银鞋出现"],
        "key_props": ["银鞋"],
        "scenes": [
            {
                "id": "S1", "major_event": "多萝西被龙卷风卷走",
                "causal_from": "开场：龙卷风逼近",
                "beats": [
                    {"action": "多萝西抓住门框", "emphasis": "highlight",
                     "visual_focus": "龙卷风", "coverage": "action"},
                    {"action": "房子离地", "emphasis": "none",
                     "visual_focus": "", "coverage": "action"},
                    {"action": "银鞋从箱子里滚出",
                     "emphasis": "key_prop",
                     "visual_focus": "银鞋",
                     "coverage": "action" if broken else "insert"},
                ],
            },
            {
                "id": "S2",
                "major_event": "" if broken else "银鞋出现",
                "causal_from": "" if broken else "房子被卷起，旧皮箱撞开",
                "beats": [{"action": "银鞋停在地板上", "emphasis": "none"}],
            },
        ],
    }
    board = {
        "episode": 1,
        "segments": [{
            "scene_index": 1,
            "cuts": [
                {"beats": [1, 1], "size": "wide",
                 "camera": "Static Shot" if broken else "Push In",
                 "emphasis": "highlight", "visual_focus": "龙卷风",
                 "coverage": "action"},
                {"beats": [3, 3], "size": "medium" if broken else "close",
                 "camera": "Static Shot" if broken else "Dolly In",
                 "emphasis": "key_prop", "visual_focus": "银鞋",
                 "coverage": "insert"},
            ],
        }],
    }
    for name, obj in (("outline.json", outline),
                      ("script_ep01.json", script),
                      ("board_ep01.json", board)):
        (tmp_path / name).write_text(
            json.dumps(obj, ensure_ascii=False), encoding="utf-8")


def _audit(tmp_path):
    import sys
    from pathlib import Path

    scripts = Path(__file__).resolve().parents[1] / "scripts"
    if str(scripts) not in sys.path:
        sys.path.insert(0, str(scripts))
    from audit_run import audit

    return audit(root=tmp_path, directory=str(tmp_path))


def test_audit_run_passes_plot_contract(tmp_path):
    _write_audit_run(tmp_path)
    result = _audit(tmp_path)
    assert result["verdict"] == "PASS", result["issues"]
    row = result["episodes"][0]
    assert row["must_show_hit"] == 2
    assert row["must_show_emphasized"] == 1
    assert row["key_props_script_ok"] == 1
    assert row["key_props_board_ok"] == 1
    assert row["causal_missing"] == 0
    assert row["static_emphasis"] == 0


def test_audit_run_flags_causal_prop_and_static_camera(tmp_path):
    _write_audit_run(tmp_path, broken=True)
    result = _audit(tmp_path)
    assert result["verdict"] == "FAIL"
    text = "\n".join(result["issues"])
    assert "must_show 未被任何场认领" in text
    assert "causal_from" in text
    assert "insert 特写" in text
    assert "Static Shot" in text
    assert "景别不是 close" in text
