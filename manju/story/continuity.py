# -*- coding: utf-8 -*-
"""连续性硬校验：state_out 跨镜/跨场/跨集。

确定性规则，不调模型：
* 服装：必须是角色已有 state_id，且与本场剧本声明的服装一致；相邻镜不得无故换装。
* 道具：**本镜展示的道具**持有者必须在画内；转移必须可见；不得凭空消失。
  画外角色继续持有未出镜的道具是合法的世界状态，不能因为人不在画内就判错。
* 伤势：相邻镜不得回退；跨集必须延续。
* 走位：同一角色相邻镜方位跳变 >90° 且不是 space/time/match_cut → 错。
* 180°：双人相对位置在相邻镜翻转且没有新的全景 → 错。
* 目线：双人对白/反应镜里，两人的 facing 必须大致看向对方。
* 景别：相邻镜跳 3 级以上且没有新信息/反应/POV 理由 → 错。

旧分镜没有 state_out 时只记 warning，不阻断（兼容旧产物）。
"""
from __future__ import annotations

import re

from .models import (
    Cast, CutState, ScriptEpisode, ScriptScene, StoryCut, StoryEpisodeBoard,
    StorySegment, face_lock_required,
)

_RING = ["north", "northeast", "east", "southeast",
         "south", "southwest", "west", "northwest"]
_SIZES = ["extreme-wide", "wide", "medium", "close", "extreme-close"]
_CUT_OK = {"space", "time", "match_cut"}
_EYELINE_SKIP = {"pov", "insert", "establish", "transition"}
_WARDROBE_TOKEN = re.compile(r"C\d+-[A-Za-z0-9_-]+")


def script_wardrobe_at(scene: ScriptScene, beat_index: int) -> dict[str, str]:
    """按剧本拍级账本推算第 beat_index 拍结束时的服装态。

    场级 wardrobe 是起点；beat.state_out 快照里的 Cxx-state token 覆盖；
    beat.state_change 里显式写出的 token 也可作为授权目标。分镜层必须
    跟随这个确定性结果，不能因为模型漏写就沿用旧服装。
    """
    running = dict(scene.wardrobe or {})
    for beat in scene.beats[:max(0, beat_index)]:
        for cid, snap in (beat.state_out or {}).items():
            m = _WARDROBE_TOKEN.search(str(snap or ""))
            if m:
                running[cid] = m.group(0)
        for cid, change in (beat.state_change or {}).items():
            m = _WARDROBE_TOKEN.search(str(change or ""))
            if m:
                running[cid] = m.group(0)
    return running


def _ring_index(value: str) -> int | None:
    return _RING.index(value) if value in _RING else None


def _ring_distance(a: str, b: str) -> int:
    ia, ib = _ring_index(a), _ring_index(b)
    if ia is None or ib is None:
        return 0
    d = abs(ia - ib)
    return min(d, 8 - d)


def _relative(a: str, b: str) -> int | None:
    """a 相对 b 的左右位置：正=右侧，负=左侧，范围 [-4, 4]。"""
    ia, ib = _ring_index(a), _ring_index(b)
    if ia is None or ib is None:
        return None
    d = (ia - ib) % 8
    return d if d <= 4 else d - 8


def _direction_to(me: str, other: str) -> str | None:
    ia, ib = _ring_index(me), _ring_index(other)
    if ia is None or ib is None:
        return None
    d = (ib - ia) % 8
    # 正对面（相差 4 格）时 +4/-4 等价，必须按“从 me 出发转半圈”取
    # other 的实际方位；否则 south→north 会被算成 south，导致误报目线。
    if d == 4:
        return _RING[ib]
    return _RING[d]


def _faces_toward(facing: str, me: str, other: str) -> bool:
    want = _direction_to(me, other)
    if want is None or _ring_index(facing) is None:
        return True                       # camera/away/未知方向不判
    return _ring_distance(facing, want) <= 1


def _declared(cut: StoryCut) -> bool:
    return not cut.state_out.empty()


def _gaze_pair(cut: StoryCut, cast: Cast | None
               ) -> tuple[str, str, str, str, str, str] | None:
    """需要互相凝视的双人镜；返回 (x, y, fx, fy, wx, wy)。

    - 非双人镜 / 反应、插入、POV、建立、转场镜不判；
    - 同向追跑/跟拍（两人 facing 基本一致）不是目线失误；
    - 非人类或无脸角色（狗、稻草人、面具）不参与硬规则。
    """
    if len(cut.characters) != 2 or cut.coverage in _EYELINE_SKIP:
        return None
    x, y = cut.characters
    st = cut.state_out
    fx, fy = st.facing.get(x), st.facing.get(y)
    wx, wy = st.where.get(x), st.where.get(y)
    if not (fx and fy and wx and wy):
        return None
    if _ring_index(fx) is None or _ring_index(fy) is None:
        return None                       # toward_camera/off-screen 等不判
    if _ring_distance(fx, fy) <= 1:
        return None                       # 同向追逐/跟拍
    if cast is not None:
        by_id = {c.id: c for c in cast.characters}
        if not all(cid in by_id and face_lock_required(by_id[cid])
                   for cid in (x, y)):
            return None
    return x, y, fx, fy, wx, wy


def repair_gaze(cut: StoryCut, cast: Cast | None = None) -> bool:
    """确定性修正双人镜的目线：让 facing 指向对方。

    模型反复把 facing 写成同一方向（或写成背对），重掷只会烧钱。
    只要 where 合法且双方都是需要锁脸的人类，就能算出唯一正确的朝向；
    同向追跑与 POV/插入等镜头保持原样。返回是否修改。
    """
    pair = _gaze_pair(cut, cast)
    if pair is None:
        return False
    x, y, _, _, wx, wy = pair
    st = cut.state_out
    changed = False
    for me, other_where in ((x, wy), (y, wx)):
        my_where = st.where[me]
        if not _faces_toward(st.facing[me], my_where, other_where):
            want = _direction_to(my_where, other_where)
            if want is not None:
                st.facing[me] = want
                changed = True
    return changed


def _pairs(items: list) -> list[tuple]:
    return list(zip(items, items[1:]))


# --------------------------------------------------------------------------
# 单镜 / 相邻镜
# --------------------------------------------------------------------------
def validate_cut_state(cut: StoryCut, scene: ScriptScene, cast: Cast) -> list[str]:
    """单镜 state_out 自洽：人物必须齐全，服装/道具/方向必须合法。"""
    errs: list[str] = []
    st = cut.state_out
    chars = {c.id: c for c in cast.characters}
    wardrobe_ok = {cid: {w.state_id for w in c.wardrobe_states}
                   for cid, c in chars.items()}
    for cid in cut.characters:
        if cid not in st.where or cid not in st.facing or cid not in st.wardrobe:
            errs.append(f"{cut.beats} 镜：{cid} 的 state_out 缺 where/facing/wardrobe")
        wid = st.wardrobe.get(cid, "")
        if wid and cid in wardrobe_ok and wid not in wardrobe_ok[cid]:
            errs.append(f"{cut.beats} 镜：{cid} 的服装 {wid} 不在角色状态库里")
        expected = script_wardrobe_at(scene, cut.beats[1]).get(cid)
        if expected and wid and wid != expected:
            errs.append(f"{cut.beats} 镜：{cid} 穿 {wid}，本拍剧本状态应为 {expected}")
        if not st.posture.get(cid):
            errs.append(f"{cut.beats} 镜：{cid} 的 state_out 缺 posture")
    for cid in st.where:
        if cid not in chars:
            errs.append(f"{cut.beats} 镜：state_out.where 出现未知角色 {cid}")
    # state_out 是“完整世界状态”，会继承画外人物持有的道具。只有本镜真正
    # 展示的道具（cut.props）或本镜发生了易主，才要求持有者在画内；否则
    # “C2 特写时 C1 仍在画外拿着鞋”会被误判成“道具在人不在”。
    from_props = cut.state_from.props
    for prop, owner in st.props.items():
        changed = bool(from_props) and from_props.get(prop) != owner
        if prop in cut.props or changed:
            if owner != "scene" and owner not in cut.characters:
                errs.append(f"{cut.beats} 镜：道具 {prop} 在 {owner} 手里，"
                            f"但 {owner} 不在画内")
        if owner != "scene" and owner not in chars:
            errs.append(f"{cut.beats} 镜：道具 {prop} 的持有者 {owner} 不存在")
    return errs


def validate_adjacent(a: StoryCut, b: StoryCut,
                      cast: Cast | None = None,
                      scene: ScriptScene | None = None,
                      *, scene_start: bool = False) -> list[str]:
    """相邻两镜：走位跳变、服装回退、道具转移、目线、180°、景别跳变。

    给了 scene 时，剧本拍级账本（state_change / state_out 快照）授权的
    换装不再要求 space/time；新场开场按场级 wardrobe 快照换装也放行。
    scene_start=True 表示 b 是下一场的首镜：新场重新建立空间，允许从
    上一场特写直接切到全景，不要求模型额外编造 new_info 理由。
    """
    errs: list[str] = []
    sa, sb = a.state_out, b.state_out
    if sa.empty() and sb.empty():
        return errs
    both = set(a.characters) & set(b.characters)

    for cid in both:
        wa, wb = sa.where.get(cid), sb.where.get(cid)
        if wa and wb and wa != wb and _ring_distance(wa, wb) > 2:
            if b.cut_reason not in _CUT_OK:
                errs.append(f"{a.beats}→{b.beats}：{cid} 位置从 {wa} 跳到 {wb}"
                            "（需 space/time/match_cut）")
        da, db = sa.wardrobe.get(cid), sb.wardrobe.get(cid)
        if da and db and da != db and b.cut_reason not in {"time", "space"}:
            authorized = False
            if scene is not None:
                before = script_wardrobe_at(scene, b.beats[0] - 1).get(cid)
                after = script_wardrobe_at(scene, b.beats[1]).get(cid)
                authorized = before == da and after == db
                if not authorized and b.beats[0] == 1 \
                        and scene.wardrobe.get(cid) == db:
                    authorized = True          # 新场开场按场级服装快照换装
            if not authorized:
                errs.append(f"{a.beats}→{b.beats}：{cid} 服装 {da} → {db}，"
                            "没有换装理由")
        ia, ib = sa.injuries.get(cid, ""), sb.injuries.get(cid, "")
        if ia and not ib:
            errs.append(f"{a.beats}→{b.beats}：{cid} 的伤势 {ia!r} 凭空消失")

    for prop, owner in sb.props.items():
        prev = sa.props.get(prop)
        if prev is None:
            continue
        if prev != owner:
            if owner != "scene" and owner not in b.characters:
                errs.append(f"{a.beats}→{b.beats}：道具 {prop} 的新持有者 {owner} 不在画内")
            elif prev == "scene":
                # 从环境（地面/桌面）拿起：上一镜可以只是道具特写，不能要求
                # "scene" 出现在 characters 里。只要本镜同时展示道具与新持有
                # 者，并且本镜确实是动作完成，交接就算在本镜内可见。
                if prop not in b.props or b.cut_reason != "action_done":
                    errs.append(f"{a.beats}→{b.beats}：道具 {prop} 从 scene 转到 {owner}，"
                                "交接不可见")
            elif prev not in b.characters and owner not in a.characters:
                errs.append(f"{a.beats}→{b.beats}：道具 {prop} 从 {prev} 转到 {owner}，"
                            "交接不可见")
        elif b.cut_reason in {"space", "time"}:
            continue
    for prop, owner in sa.props.items():
        if prop not in sb.props and owner != "scene" and b.cut_reason not in {"time"}:
            errs.append(f"{a.beats}→{b.beats}：道具 {prop} 从 {owner} 手里消失")

    # 180°：双人相对位置翻转
    if len(both) >= 2:
        pair = sorted(both)[:2]
        x, y = pair
        wx1, wy1 = sa.where.get(x), sa.where.get(y)
        wx2, wy2 = sb.where.get(x), sb.where.get(y)
        if all(v in _RING for v in (wx1, wy1, wx2, wy2)):
            order1 = _relative(wx1, wy1)
            order2 = _relative(wx2, wy2)
            flip = (order1 is not None and order2 is not None
                    and abs(order1) >= 3 and abs(order2) >= 3
                    and order1 * order2 < 0)
            if flip and a.size not in {"wide", "extreme-wide"} \
                    and b.size not in {"wide", "extreme-wide"} \
                    and b.cut_reason not in _CUT_OK:
                errs.append(f"{a.beats}→{b.beats}：{x}/{y} 左右关系翻转（180° 跳轴），"
                            "需插入全景或 space/match_cut")

    # 目线：同一镜内双人对白/反应（同向追跑不判；无脸角色不判）
    pair = _gaze_pair(b, cast)
    if pair is not None:
        x, y, fx, fy, wx, wy = pair
        if not _faces_toward(fx, wx, wy):
            errs.append(f"{b.beats} 镜：{x} 的目线 {fx} 没有看向 {y}（{wy}）")
        if not _faces_toward(fy, wy, wx):
            errs.append(f"{b.beats} 镜：{y} 的目线 {fy} 没有看向 {x}（{wx}）")

    # 景别跳变。新场首镜是合法的空间重建，不按场内的跳切规则约束。
    if not scene_start and a.size in _SIZES and b.size in _SIZES:
        jump = abs(_SIZES.index(a.size) - _SIZES.index(b.size))
        if jump >= 3 and b.cut_reason not in {"new_info", "pov", "reaction", "sound"}:
            errs.append(f"{a.beats}→{b.beats}：景别从 {a.size} 跳到 {b.size}，"
                        "缺少 new_info/pov/reaction/sound 理由")
    return errs


# --------------------------------------------------------------------------
# 场 / 集
# --------------------------------------------------------------------------
def validate_segment(segment: StorySegment, scene: ScriptScene, cast: Cast, *,
                     state_in: CutState | None = None,
                     prev_cuts: list[StoryCut] | None = None) -> list[str]:
    errs: list[str] = []
    if not segment.cuts:
        return ["分镜段没有 cut"]
    if all(not _declared(c) for c in segment.cuts):
        return []                       # 旧产物：跳过，validate_board 会给 warning
    for cut in segment.cuts:
        errs.extend(validate_cut_state(cut, scene, cast))
    for a, b in _pairs(segment.cuts):
        errs.extend(validate_adjacent(a, b, cast=cast, scene=scene))

    first = segment.cuts[0]
    if state_in is not None and not state_in.empty() and _declared(first):
        for cid, injury in state_in.injuries.items():
            if injury and cid in first.characters and not first.state_out.injuries.get(cid):
                errs.append(f"本场第一个镜 {first.beats}：{cid} 的伤势 {injury!r} 没有延续")
        for cid, wid in state_in.wardrobe.items():
            got = first.state_out.wardrobe.get(cid)
            if wid and got and wid != got and scene.wardrobe.get(cid) in (None, wid):
                errs.append(f"本场第一个镜 {first.beats}：{cid} 服装从 {wid} 回退到 {got}")
        for prop, owner in state_in.props.items():
            got = first.state_out.props.get(prop)
            if owner and got and owner != got and owner not in first.characters \
                    and got not in first.characters:
                errs.append(f"本场第一个镜 {first.beats}：道具 {prop} 转移不可见")

    if prev_cuts:
        last = prev_cuts[-1]
        # 跨场边界：允许新场用全景重新建立空间（特写→全景是正常转场，
        # 不是场内的无理由景别跳切）。
        errs.extend(validate_adjacent(last, first, cast=cast, scene=scene,
                                      scene_start=True))
    return errs


def validate_board_continuity(board: StoryEpisodeBoard, ep: ScriptEpisode,
                              cast: Cast) -> tuple[list[str], list[str]]:
    errs: list[str] = []
    warns: list[str] = []
    cuts = [c for seg in board.segments for c in seg.cuts]
    if not cuts:
        return ["分镜为空"], warns
    if all(not _declared(c) for c in cuts):
        warns.append("全部切镜未声明 state_out，连续性校验已跳过（旧分镜）")
        return errs, warns

    scene_by_index = {i: sc for i, sc in enumerate(ep.scenes, 1)}
    state_in = board.state_in
    prev_cuts: list[StoryCut] = []
    for seg in board.segments:
        scene = scene_by_index.get(seg.scene_index)
        if scene is None:
            errs.append(f"段 {seg.id} 的 scene_index={seg.scene_index} 越界")
            continue
        errs.extend(validate_segment(seg, scene, cast,
                                     state_in=state_in, prev_cuts=prev_cuts))
        prev_cuts.extend(seg.cuts)
        state_in = seg.cuts[-1].state_out
    return errs, warns


def validate_handoff(prev: CutState, nxt: CutState) -> list[str]:
    """跨集：上一集 state_out 必须延续到下一集 state_in。"""
    errs: list[str] = []
    for cid, injury in prev.injuries.items():
        if injury and not nxt.injuries.get(cid):
            errs.append(f"跨集：{cid} 的伤势 {injury!r} 在下一集消失")
    for prop, owner in prev.props.items():
        got = nxt.props.get(prop)
        if owner and got is None:
            errs.append(f"跨集：道具 {prop}（{owner} 持有）在下一集消失")
    return errs
