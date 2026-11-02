"""确定性修复层：质量门不过时的本地修复（不回喂模型）。

原则：能在代码里判定的错误绝不整段重问模型；修复必须能被质量门再次验证。
本模块依赖 prompts（_placeholder_prompt）与 validate / models，
不依赖 stages 的生成流程。
"""
from __future__ import annotations

import math
import re
from typing import Any, Callable

from .continuity import repair_gaze, script_wardrobe_at
from .editorial import count_action_verbs
from .episodes import EpisodeSlice
from .models import (
    ActionBeatLine, Art, Cast, CutPlan, CutState, CutSubject, DIRECTIONS,
    DesignEpisode, EpisodePlan, NarrationLine, Outline, OutlineProp,
    ScriptBeat, ScriptEpisode, ScriptParams, ScriptScene, SegmentPlan,
    Staging, StagingItem, StagingLocation, StoryBible, StoryCut,
    StorySegment, _beat_seconds, _scene_seconds, face_lock_required,
)
from .prompts import _placeholder_prompt
from .validate import (
    SHOT_BEATS, _event_key, _line_chars, beat_has_prop, coverage_required,
    event_match, event_score, scene_has_prop,
)

Progress = Callable[[str], None]

def _prune_unused_props(outline: Outline, progress: Progress) -> list[str]:
    """剔除未被任何一集引用的道具。

    “道具没被使用”是典型的可确定性修复问题：模型已经生成完所有分集梗概，
    再为它整轮重写既贵又未必改对。这里直接裁剪大纲道具表（连同后续美术阶段），
    只影响从未出现在任何一集里的冗余设定。
    """
    used = {pid for ep in outline.episodes for pid in ep.prop_ids}
    removed = [p for p in outline.props if p.id not in used]
    if not removed:
        return []
    outline.props = [p for p in outline.props if p.id in used]
    for p in removed:
        progress(f"  [WARN] 道具 {p.id}（{p.name}）未被任何一集使用，"
                 f"已从大纲剔除（后续美术不再生成）")
    return [p.id for p in removed]


def _demote_unused_primary_scenes(outline: Outline,
                                  progress: Progress) -> list[str]:
    """未被任何一集使用的主场景降级为备用场景。

    分集梗概已经定稿，一个从未被引用的“主场景”不是错误，只是备用设定；
    硬判失败会让整条流水线为一个可选场景重跑。保留美术设定，仅摘掉
    primary 标记，质量门会按 warning 提示，不再阻断。
    """
    used = {sid for ep in outline.episodes for sid in ep.scene_ids}
    demoted = [s.id for s in outline.scenes
               if s.primary and s.id not in used]
    for scene in outline.scenes:
        if scene.id in demoted:
            scene.primary = False
    if demoted:
        progress(f"  [WARN] 主场景 {demoted} 未被任何一集使用，"
                 "已降级为备用场景（不阻断生成）")
    return demoted


def _repair_unused_characters(outline: Outline, slices: list[EpisodeSlice],
                              progress: Progress
                              ) -> tuple[list[str], list[str]]:
    """分集梗概漏登记角色时的确定性修复。

    - 原文（本集完整切片）里确实出现 → 补登记到提及最多的那一集；
    - 改编范围内完全没出现 → support 从大纲剔除，避免后续白生成定妆；
    - lead 不自动删，交给质量门报错，防止主角被静默裁掉。

    返回 (补登记的角色 id, 剔除的角色 id)。
    """
    used = {cid for ep in outline.episodes for cid in ep.character_ids}
    texts = {ep.ep: (slices[ep.ep - 1].text
                     if 1 <= ep.ep <= len(slices) else "")
             for ep in outline.episodes}
    added: list[str] = []
    removed: list[str] = []
    for c in list(outline.characters):
        if c.id in used or c.tier == "functional":
            continue
        name = (c.name or "").strip()
        counts = {ep: (texts[ep].count(name) if name else 0) for ep in texts}
        best_ep, best = max(counts.items(), key=lambda kv: kv[1],
                            default=(0, 0))
        if best > 0:
            ep = next(e for e in outline.episodes if e.ep == best_ep)
            if c.id not in ep.character_ids:
                ep.character_ids.append(c.id)
            if len(ep.character_ids) >= 3 and not ep.crowd_plan.strip():
                ep.crowd_plan = "群演只做背景走动与围观，不与主要角色对白。"
            progress(f"  [FIX] 角色 {c.id}（{c.name}）在分集梗概中漏登记，"
                     f"已按原文补入第 {best_ep} 集")
            added.append(c.id)
        elif c.tier == "lead":
            progress(f"  [WARN] 主角 {c.id}（{c.name}）未出现在改编范围内的"
                     f"任何一集，保留设定并由质量门报错")
        else:
            outline.characters = [x for x in outline.characters
                                  if x.id != c.id]
            progress(f"  [WARN] 角色 {c.id}（{c.name}）在改编范围内未出现，"
                     f"已从大纲剔除（后续不生成定妆）")
            removed.append(c.id)
    return added, removed


_CLIFF_QUOTES = "“”\"'「」『』‘’"


_BEAT_FALLBACK = {
    "舒缓": "收束", "日常": "收束", "过渡": "收束", "平静": "收束",
    "温情": "收束", "轻松": "收束", "喜悦": "收束", "缓和": "收束",
    "铺垫": "激励", "推进": "激励", "希望": "激励", "闪回": "激励",
    "危机": "转折", "冲突": "转折", "对峙": "转折", "紧张": "转折",
    "压抑": "受挫", "悬疑": "转折", "反转": "转折", "揭示": "转折",
    "悲伤": "受挫", "愤怒": "受挫", "绝望": "受挫", "失败": "受挫",
    "爆发": "高潮", "决战": "高潮", "追逐": "高潮", "打斗": "高潮",
    "战斗": "高潮", "结局": "收束", "结尾": "结尾钩子",
}


def _split_long_line(text: str, limit: int) -> list[str]:
    """把超长台词拆成每段 ≤limit 的若干拍；优先在标点处断开，否则对半拆。"""
    text = (text or "").strip()
    out: list[str] = []
    punct = "，。！？；：、,.!?;:"
    while len(text) > limit:
        cut = 0
        for i, ch in enumerate(text[:limit]):
            if ch in punct and i + 1 >= limit // 2:
                cut = i + 1
        if not cut:
            half = len(text) // 2
            cut = half if half <= limit else limit
        out.append(text[:cut].strip())
        text = text[cut:].strip()
    if text:
        out.append(text)
    return out or [""]


def _cliff_action_text(cliff: str, fallback: str) -> str:
    """把 cliff 文案压成一条可拍的动作句；不安全时退回通用定格动作。"""
    from .validate import (
        ACTION_MAX_LEN, _has_quote, _line_chars, _passive_voice, _psych_verb,
    )
    text = (cliff or "").strip()
    for q in _CLIFF_QUOTES:
        text = text.replace(q, "")
    text = re.sub(r"\s+", "", text)
    # 只拼接完整分句，避免把半句话截断成病句
    parts = [p for p in re.split(r"(?<=[。！？!?；;，,])", text) if p]
    action = ""
    for part in parts:
        if _line_chars(action + part) > ACTION_MAX_LEN:
            break
        action += part
    if (not action or _line_chars(action) > ACTION_MAX_LEN
            or _has_quote(action) or _psych_verb(action) or _passive_voice(action)):
        return fallback
    return action


def _repair_script_coverage(ep: ScriptEpisode, p: ScriptParams,
                            progress: Progress, *,
                            dialogue_light: bool = False) -> int:
    """把全集 coverage 配额补到硬门槛。

    模型常把每个动作拍都写成 action，导致分镜层一镜一拍继承后
    reaction/insert/pov 配额永远不够，3 次重写也只是换个说法。这里在
    不改变台词、动作文本和拍数的前提下，把**已有的动作拍**重新标注为
    缺失的 coverage；镜头语义仍然成立，只是导演分类补齐。
    """
    required = coverage_required(ep.target_seconds,
                                 dialogue_light=dialogue_light)
    entries: list[tuple[int, int, ScriptBeat]] = []
    for si, sc in enumerate(ep.scenes, 1):
        for bi, beat in enumerate(sc.beats, 1):
            if beat.coverage is None:
                beat.coverage = "dialogue" if beat.line else "action"
            entries.append((si, bi, beat))
    counts: dict[str, int] = {}
    for _, _, beat in entries:
        counts[beat.coverage] = counts.get(beat.coverage, 0) + 1
    need = {cov: max(0, n - counts.get(cov, 0))
            for cov, n in required.items()}
    if not any(need.values()):
        return 0

    first_pos = {(si, 1) for si, _ in enumerate(ep.scenes, 1)}
    last_pos = {(si, len(sc.beats))
                for si, sc in enumerate(ep.scenes, 1)}
    final_pos = (len(ep.scenes), len(ep.scenes[-1].beats)) if ep.scenes else None
    positions = [(si, bi) for si, bi, _ in entries]
    by_pos = {(si, bi): beat for si, bi, beat in entries}
    used: set[tuple[int, int]] = set()
    changed = 0

    def pick(pool: list[tuple[int, int]]) -> tuple[int, int] | None:
        for pos in pool:
            if pos in used or pos not in by_pos:
                continue
            if pos == final_pos and len(positions) > 1:
                continue          # 集末镜头留给 cliff 落点
            beat = by_pos[pos]
            if beat.coverage in {"establish", "insert", "reaction", "pov"}:
                continue          # 不拆东墙补西墙
            return pos
        return None

    no_line = [pos for pos in positions
               if not by_pos[pos].line]
    after_open = [pos for pos in positions
                  if pos not in first_pos and pos not in last_pos]
    pools = {
        "establish": ([pos for pos in positions if pos in first_pos and pos not in last_pos]
                      + after_open + positions),
        "insert": (no_line or positions),
        "reaction": ([pos for pos in positions if pos not in first_pos]
                     + positions),
        "pov": ([pos for pos in positions if pos not in first_pos and not by_pos[pos].line]
                + no_line + positions),
    }
    for cov in ("establish", "insert", "reaction", "pov"):
        for _ in range(need[cov]):
            pos = pick(pools[cov])
            if pos is None:
                break
            by_pos[pos].coverage = cov
            used.add(pos)
            changed += 1
    if changed:
        progress(f"  [FIX] 剧本 coverage 配额自动补齐 {changed} 拍"
                 f"（reaction/insert/establish/pov）")
    return changed


def _scene_match_text(sc: ScriptScene) -> str:
    parts = [sc.location, sc.goal, sc.obstacle, sc.turn, sc.cost,
             sc.information, sc.mood, sc.major_event]
    for b in sc.beats:
        parts.extend([b.action or "", b.line or "", b.visual_focus or ""])
    return " ".join(x for x in parts if x)


def _repair_script_plot(ep: ScriptEpisode, plan: EpisodePlan, art: Art,
                        progress: Progress) -> int:
    """把 must_show / causal_from / key_props / emphasis 落到剧本结构上。

    只做**确定性对账**，不改台词、不替模型发明剧情：
    * 空 major_event 按文本相似度认领最接近的 must_show；
    * 每场补 causal_from（承接上一场的可见结果）；
    * 关键道具找到已有动作/插入拍，标 key_prop + visual_focus；
    * 转折/高潮/集末钩子各标一个 highlight/climax，保证分镜有非静态运镜。
    """
    must = [m.strip() for m in plan.must_show if (m or "").strip()]
    key_props = [p.strip() for p in plan.key_props if (p or "").strip()]
    ep.must_show = list(must)
    ep.key_props = list(key_props)
    fixed = 0
    scenes = ep.scenes

    # 1) major_event 认领：先清掉新造的关键事件，再为空场补最接近的一条。
    for sc in scenes:
        ev = (sc.major_event or "").strip()
        if ev and must and not any(event_match(ev, m) for m in must):
            best = max(must, key=lambda m: event_score(ev, m))
            if event_score(ev, best) >= 0.5:
                sc.major_event = best
                fixed += 1
            else:
                sc.major_event = ""
                fixed += 1
    matched = {m for m in must
               for sc in scenes
               if event_match(sc.major_event, m)
               or _event_key(m) in _event_key(_scene_match_text(sc))
               or event_score(_scene_match_text(sc), m) >= 0.6}
    for sc in scenes:
        if (sc.major_event or "").strip() or not must:
            continue
        ranked = sorted(must, key=lambda m: event_score(_scene_match_text(sc), m),
                        reverse=True)
        if ranked and event_score(_scene_match_text(sc), ranked[0]) >= 0.3:
            sc.major_event = ranked[0]
            matched.add(ranked[0])
            fixed += 1
    for m in must:
        if m in matched:
            continue
        ranked = sorted(
            (sc for sc in scenes if not (sc.major_event or "").strip()),
            key=lambda sc: event_score(_scene_match_text(sc), m),
            reverse=True)
        if ranked and event_score(_scene_match_text(ranked[0]), m) > 0.2:
            ranked[0].major_event = m
            matched.add(m)
            fixed += 1

    # 2) 场间因果：空 causal_from 用上一场的可见结果兜底，不留无因果跳场。
    for i, sc in enumerate(scenes):
        if (sc.causal_from or "").strip():
            continue
        if i == 0:
            sc.causal_from = ("开场：承接上一集结尾的后果" if ep.ep > 1
                              else "开场：建立本集情境")
        else:
            prev = scenes[i - 1]
            result = (prev.major_event or prev.turn or prev.cost
                      or prev.information or "上一场的结果")
            sc.causal_from = f"承接{prev.id}：{result}"
        fixed += 1

    # 2.5) 每条 must_show 至少一个强调拍：关键事件不能是静止过场。
    # 同一场可能承载多条必拍事件，所以按“事件”而不是按“场”标记；
    # 已有强调拍的场次仍可给另一条未覆盖的事件补拍（require_clean=False）。
    def _mark(scene: ScriptScene, emphasis: str, focus: str,
              *, require_clean: bool = True) -> bool:
        if scene is None or not scene.beats:
            return False
        if require_clean and any(b.emphasis != "none" for b in scene.beats):
            return False
        cands = [b for b in scene.beats if b.emphasis == "none"]
        if not cands:
            return False
        cands = [b for b in cands if not b.line] or cands
        # 强调拍必须落在真正演出该事件的那一拍上，不能随便选场次第一拍：
        # 否则运镜对着一句无关过场，观众仍会觉得关键情节被跳过。
        beat = max(cands, key=lambda b: event_score(
            " ".join([b.action or "", b.visual_focus or "", b.line or ""]),
            focus or ""))
        beat.emphasis = emphasis
        beat.visual_focus = (focus or _beat_summary(beat) or scene.turn)[:60]
        return True

    for m in must:
        scenes_for_m = [
            sc for sc in scenes
            if event_match(sc.major_event, m)
            or _event_key(m) in _event_key(_scene_match_text(sc))
            or event_score(_scene_match_text(sc), m) >= 0.6]
        if not scenes_for_m:
            continue
        sc = max(scenes_for_m,
                 key=lambda s: event_score(_scene_match_text(s), m))
        already = any(
            b.emphasis != "none"
            and (event_match(b.visual_focus, m)
                 or event_score(" ".join([b.action or "", b.visual_focus or ""]),
                                m) >= 0.85)
            for b in sc.beats)
        if already:
            continue
        if _mark(sc, "highlight", m, require_clean=False):
            fixed += 1

    # 3) 关键道具：在已有拍里找最合适的一拍标插入特写。
    for prop in key_props:
        # 先保证道具真的挂进承载它的场次，否则分镜拿不到 P# 参考图。
        pid = _resolve_prop_id(prop, art.props)
        if pid:
            pname = next((p.name for p in art.props if p.id == pid), "")
            for sc in scenes:
                if pid not in sc.props and scene_has_prop(sc, prop, art):
                    sc.props.append(pid)
                    fixed += 1
                if pid in sc.props and pname and pname not in sc.objects:
                    sc.objects.append(pname)
                    fixed += 1
        already = [b for sc in scenes for b in sc.beats
                   if b.emphasis == "key_prop" and beat_has_prop(b, prop)]
        if already:
            for b in already:
                if not (b.visual_focus or "").strip():
                    b.visual_focus = prop
                    fixed += 1
            continue
        candidates: list[tuple[int, ScriptBeat]] = []
        for rank, sc in enumerate(scenes):
            if not scene_has_prop(sc, prop, art):
                continue
            for b in sc.beats:
                if b.line:
                    continue
                # 一个拍只能承载一个强调重点：别把另一件道具的特写覆盖掉。
                if b.emphasis == "key_prop":
                    continue
                score = (2 if beat_has_prop(b, prop) else 0)
                score += (2 if b.coverage == "insert" else 0)
                score += (1.5 if b.emphasis == "none" else 0)
                candidates.append((score - rank * 0.01, b))
        if not candidates:
            # 模型完全没让设计指定的关键道具出场：退而求其次，在本集最相关的
            # 场次挂一个无台词插入特写。不新造动作、不改台词，只保证道具
            # 有参考图和特写，避免整集因一条硬门反复失败。
            if pid is None:
                continue
            fallback = sorted(
                (sc for sc in scenes
                 if any(not b.line and b.emphasis != "key_prop"
                        for b in sc.beats)),
                key=lambda sc: (pid in sc.props,
                                event_score(_scene_match_text(sc), prop)),
                reverse=True)
            if not fallback:
                continue
            sc = fallback[0]
            if pid not in sc.props:
                sc.props.append(pid)
            cand_beats = [b for b in sc.beats
                          if not b.line and b.emphasis != "key_prop"]
            beat = max(cand_beats,
                       key=lambda b: (b.coverage == "insert",
                                      b.emphasis == "none"))
            beat.coverage = "insert"
            beat.emphasis = "key_prop"
            beat.visual_focus = prop
            fixed += 1
            progress(f"  [WARN] 关键道具「{prop}」未在剧本文字中出现，"
                     f"已在 {sc.id} 绑定插入特写（请检查改编是否漏掉它）")
            continue
        beat = max(candidates, key=lambda x: x[0])[1]
        if not beat.line:
            beat.coverage = "insert"
        beat.emphasis = "key_prop"
        beat.visual_focus = prop
        fixed += 1
        progress(f"  [FIX] 关键道具「{prop}」绑定插入特写拍："
                 f"{_beat_summary(beat)[:36]}")

    # 4) 关键镜头权重：转折/高潮/集末钩子至少各有一个非静态运镜的拍。
    # 高潮：优先标 beat=高潮；没有就用靠后的代价/受挫场；再退到末场。
    climax_sc = next((sc for sc in scenes if sc.beat == "高潮"), None)
    if climax_sc is None:
        late = [sc for sc in scenes if sc.beat in {"代价", "受挫"}]
        climax_sc = late[-1] if late else (scenes[-1] if scenes else None)
    if climax_sc is not None and _mark(
            climax_sc, "climax", climax_sc.turn or ep.cliff):
        fixed += 1
    # 转折：优先 beat=转折；其次有 turn 的非末场；再退到中段场。
    turn_sc = next((sc for sc in scenes if sc.beat == "转折"), None) \
        or next((sc for sc in scenes if (sc.turn or "").strip()
                 and sc is not climax_sc and sc.beat != "结尾钩子"), None)
    if turn_sc is None and scenes:
        turn_sc = scenes[len(scenes) // 2]
    if turn_sc is not None and _mark(turn_sc, "highlight", turn_sc.turn):
        fixed += 1
    if scenes and _mark(scenes[-1], "highlight", ep.cliff):
        fixed += 1
    # 开场钩子也要有一次明确运镜，避免第一场只有站桩交代。
    if scenes and _mark(scenes[0], "highlight", ep.hook):
        fixed += 1

    if fixed:
        progress(f"  [FIX] 情节对账：{len(must)} 条必拍 / {len(key_props)} 件关键道具，"
                 f"已确定性补齐 {fixed} 处")
    return fixed


def _repair_narration(ep: ScriptEpisode, p: ScriptParams,
                      progress: Progress,
                      cast: Cast | None = None) -> None:
    """旁白/转场的确定性清理：不编造内容，只删非法或超限条目。"""
    if p.narration_mode == "off":
        dropped = sum(len(sc.narration) for sc in ep.scenes)
        for sc in ep.scenes:
            sc.narration = []
            sc.transition_in = None
        if dropped:
            progress(f"  [FIX] narration_mode=off，移除 {dropped} 条旁白")
        return

    valid_chars = {c.id for c in (cast.characters if cast else [])}
    kept_exp = kept_inner = 0
    dropped = 0
    for sc in ep.scenes:
        out: list[NarrationLine] = []
        for n in sc.narration:
            if n.beat < 1 or n.beat > len(sc.beats):
                dropped += 1
                continue
            beat = sc.beats[n.beat - 1]
            if beat.line:
                # 旁白不能盖台词：顺延到最近的非台词拍
                candidates = [i for i, b in enumerate(sc.beats, 1) if not b.line]
                if not candidates:
                    dropped += 1
                    continue
                n.beat = min(candidates, key=lambda i: abs(i - n.beat))
                beat = sc.beats[n.beat - 1]
            if n.kind == "inner":
                if n.speaker not in sc.characters:
                    if n.speaker in valid_chars:
                        sc.characters.append(n.speaker)
                    else:
                        dropped += 1
                        continue
                if kept_inner >= p.narration_max_inner:
                    dropped += 1
                    continue
                kept_inner += 1
            else:
                n.speaker = "NARRATOR"
                if kept_exp >= p.narration_max_exposition:
                    dropped += 1
                    continue
                kept_exp += 1
            out.append(n)
        sc.narration = out

    for i, sc in enumerate(ep.scenes):
        if i == 0:
            continue
        tr = sc.transition_in
        if tr is None:
            continue
        if tr.type in {"match_cut", "sound_bridge", "action_bridge"} \
                and not tr.anchor.strip():
            # 不用因果文本冒充视觉锚点；留给门/重写决定。
            progress(f"  [WARN] {sc.id} {tr.type} 缺 anchor，交回质量门")
    if dropped:
        progress(f"  [FIX] 旁白清理：移除 {dropped} 条非法/超限条目")


def _longest_common_substr(a: str, b: str) -> int:
    """a、b 的最长公共连续子串长度（按字符）。"""
    if not a or not b:
        return 0
    n, m = len(a), len(b)
    dp = [0] * (m + 1)
    best = 0
    for i in range(n):
        prev = 0
        for j in range(m):
            tmp = dp[j + 1]
            if a[i] == b[j]:
                dp[j + 1] = prev + 1
                if dp[j + 1] > best:
                    best = dp[j + 1]
            else:
                dp[j + 1] = 0
            prev = tmp
    return best


def _repair_key_lines(ep: ScriptEpisode, plan: Any, progress: Progress) -> int:
    """key_lines 落拍兜底：把 LLM 改写/转述的关键台词确定性还原为原文。

    仅当存在明显变体拍（与原文最长公共连续子串 ≥6 字、重合比例 ≥45%）
    才把该拍 line 替换为原文一字不改，保留原说话人；找不到变体拍则不动
    （留给质量门回喂，绝不新造拍——说话人无法确定）。
    调用时机在拆拍之前：还原后的长句会走既有的确定性拆拍（拆出的拍
    前后相邻，拼接后仍等于原文完整一句），满足 key_lines 校验允许的
    "拆拍拼接"路径。
    """
    keys = [k.strip().strip("\u201c\u201d\"'“”").strip()
            for k in (getattr(plan, "key_lines", None) or [])]
    keys = [k for k in keys if k]
    if not keys:
        return 0
    rows = [(si, bi, b) for si, sc in enumerate(ep.scenes)
            for bi, b in enumerate(sc.beats)
            if getattr(b, "line", None)]
    lines = [b.line for _, _, b in rows]
    fixed = 0
    for key in keys:
        if any(key in ln for ln in lines):
            continue
        if any(key in lines[i] + lines[i + 1] for i in range(len(lines) - 1)):
            continue
        best = None
        best_score = 0.0
        for si, bi, b in rows:
            ln = b.line
            if not ln:
                continue
            common = _longest_common_substr(key, ln)
            # 变体判定：① 与原文有 ≥8 字连续子串；② 原文字符出现在变体里的
            # 比例 ≥80%（"改几个字/换说法"的改写命中，无关台词几乎不可能）。
            if common < 8:
                continue
            hit = sum(1 for ch in key if ch in ln) / max(len(key), 1)
            if hit < 0.8:
                continue
            if hit > best_score:
                best, best_score = (si, bi, b), hit
        if best is None:
            continue
        si, bi, b = best            # 解包命中拍，勿用循环遗留变量
        b.line = key
        fixed += 1
        progress(f"  [FIX] 关键台词变体还原为原文"
                 f"（{key[:14]}…，第 {si + 1} 场第 {bi + 1} 拍）")
    return fixed


def _split_action_text(text: str, max_actions: int) -> list[str]:
    """把多动作拍文本按动作连词边界切成分组，每组动作数 ≤ max_actions。

    中文切分点：逗号/顿号/分号/句号 + 常见动作连词（接着/然后/随即/转身/
    一把/又/再/猛地/顺势/赶紧/立刻/突然）；英文：逗号/分号/句号 + 连词。
    先用边界切成子句，再贪心合并成组；单子句动作数已超限时按动词位置硬切。
    """
    if not text:
        return [text] if text else []
    n = count_action_verbs(text)
    if n <= max_actions:
        return [text]
    # 1) 按边界切成子句（保留原词序，去掉空段）
    pieces = [p.strip() for p in re.split(
        r"(?<=[，。；、；,.])|(?<=接着)|(?<=然后)|(?<=随即)|(?<=转身)|"
        r"(?<=一把)|(?<=猛地)|(?<=顺势)|(?<=赶紧)|(?<=立刻)|(?<=突然)|"
        r"(?<=又)|(?<=再)|"
        r"(?<= and )|(?<= then )|(?<= before )|(?<= as )|(?<= while )",
        text) if p and p.strip()]
    if not pieces:
        pieces = [text]
    # 2) 目标组数：每组至多 max_actions 个动作
    groups: list[str] = []
    buf: list[str] = []
    buf_actions = 0
    for p in pieces:
        pa = count_action_verbs(p)
        if pa > max_actions:
            # 单个子句动作超限：先落盘已积累内容，再按动词位置硬切该子句
            if buf:
                groups.append("".join(buf)); buf, buf_actions = [], 0
            groups.extend(_hard_split_clause(p, max_actions))
            continue
        if buf_actions + pa > max_actions and buf:
            groups.append("".join(buf)); buf, buf_actions = [], 0
        buf.append(p); buf_actions += pa
    if buf:
        groups.append("".join(buf))
    # 3) 兜底：极端情况仍超限时，直接按动词计数均分
    out: list[str] = []
    for g in groups:
        if count_action_verbs(g) > max_actions:
            out.extend(_hard_split_clause(g, max_actions))
        else:
            out.append(g)
    return out


def _hard_split_clause(text: str, max_actions: int) -> list[str]:
    """单个子句动作数超限时，在第 max_actions+1 个动词词首硬切。

    中文无单词边界问题，可在任意字符间切；兜底场景极少用到英文长子句，
    即使切在英文单词中间也只是提示词层面的轻微瑕疵（计数仍准确）。
    """
    n = count_action_verbs(text)
    if n <= max_actions or n == 0:
        return [text] if text else []
    verbs = sorted(m.start() for m in re.finditer(
        r"(推|拉|抓|跑|跳|摔|走|站起|坐下|转身|抬头|低头|抱|扔|捡|关|开|看|喊|哭|笑|跪|倒|冲|停|扑|滚|爬|躲|敲|撞|挥手|指|"
        r"walk|run|grab|push|pull|turn|look|reach|lean|open|close|lift|drop|step|rush|point|raise|slip|release|"
        r"sink|slide|stiffen|swallow|catch|approach|halt|swing|sweep|bend|kneel|stand|rise|shift|move|enter|leave|flee|"
        r"creep|stretch|crouch|jump|throw|toss|grip|yank|wrench|shove|whirl|dive|lunge|snap)",
        text, re.I))
    if not verbs:
        return [text]
    parts: list[str] = []
    start = 0
    for i in range(max_actions, n, max_actions):
        cut = verbs[i]
        # 组间留一个空白/标点作为缓冲，避免切出过短残片
        if cut - start < 2 and i + 1 < len(verbs):
            cut = verbs[i + 1]
        if cut <= start or cut >= len(text):
            continue
        parts.append(text[start:cut].strip())
        start = cut
    parts.append(text[start:].strip())
    return [p for p in parts if p]


def _split_overloaded_action_beats(ep: ScriptEpisode, p: ScriptParams,
                                   progress: Progress) -> None:
    """动作超载拍确定性拆拍（EDL 拆镜的前置：一镜一拍 + 一拍至多
    max_actions_per_shot 个动作）。

    用户拍板：拆镜/拆段优先级高于加长单镜。剧本动作拍若含动作数超过引擎
    max_actions_per_shot（comfyui=1 / seedance=2 / jimeng=2），在剧本层就按
    动作连词边界拆成多拍——分镜层一镜一拍即天然获得"一拍一动作"的可渲染镜。
    状态变更/拍末快照/视觉重点/叙事权重归最后一拍（与超长台词拆拍同规则）。
    集长预算保护：拆拍会增加总时长（每拆一拍 +action_seconds），拆到
    target×(1+tolerance) 硬上限为止，剩余交给质量门回喂。
    """
    max_actions = p.max_actions_per_shot or 2
    if max_actions < 1:
        return
    # 预算：拆拍总新增秒数不能把集长推出硬上限
    total_now = sum(_scene_seconds(s, p) for s in ep.scenes)
    max_total = ep.target_seconds * (1 + p.tolerance)
    for sc in ep.scenes:
        beats: list[ScriptBeat] = []
        split_count = 0
        for b in sc.beats:
            if not b.action or count_action_verbs(b.action) <= max_actions:
                beats.append(b)
                continue
            chunks = _split_action_text(b.action, max_actions)
            added = (len(chunks) - 1) * p.action_seconds
            if total_now + added > max_total + 1e-6:
                progress(f"  [WARN] {sc.id} 动作拍含 "
                         f"{count_action_verbs(b.action)} 个动作（上限 {max_actions}），"
                         f"但拆拍会超出集长 {max_total:.1f}s 硬上限，"
                         f"留给质量门回喂")
                beats.append(b)
                continue
            for k, chunk in enumerate(chunks):
                nb = b.model_copy(deep=True)
                nb.action = chunk
                nb.line = None
                if k < len(chunks) - 1:
                    # 状态变更/拍末快照/视觉重点/叙事权重只属于整组动作做完后的最后一拍
                    nb.state_change = {}
                    nb.state_out = {}
                    nb.visual_focus = ""
                    nb.emphasis = "none"
                    nb.expression = ""
                beats.append(nb)
            split_count += 1
            total_now += added
            progress(f"  [FIX] {sc.id} 动作拍含 {count_action_verbs(b.action)} 个动作"
                     f"（引擎上限 {max_actions}），拆为 {len(chunks)} 拍")
        if split_count:
            sc.beats = beats


def _repair_script_output(ep: ScriptEpisode, outline: Outline, art: Art,
                          progress: Progress, cast: Cast | None = None,
                          params: ScriptParams | None = None,
                          *, dialogue_light: bool = False) -> None:
    """对剧本做**确定性**修补，避免为机器可修的问题整轮重问模型。

    1. `props` 引用了美术道具，但场次 `objects` 没登记同名陈设 → 自动补名；
    2. 悬空引用/元数据清理：删掉不存在的角色/道具/爽点 id、非法服装态、
       无主 state_change、不完整 restructure；说话人/动作点名角色若真实存在
       但未登记在场 → 补登记（只删不存在的 id，不改剧情文本）；
    3. 场次 `beat` 不在戏剧节拍表 → 映射到最近的合法节拍；末场强制“结尾钩子”；
    4. 单句台词超过单镜可容纳的字数 → 按标点拆成多拍（总字数/总时长不变，
       状态变更只留在整句说完的最后一拍）；
    5. `hook_beat` 失效或不在前 3 拍 → 归一到本集第 1 拍（冷开场指针）；
    6. 集末最后一场不足 5s（cliff 撑不住）→ 补 1-2 个 cliff 落点动作拍。
       补拍前检查总时长硬上限，宁可交给质量门回喂也不写出超长剧本。
    """
    p = params or ScriptParams()

    # 0) 场次 id 归一：本集只能引用 episode_plan.scene_ids 里的地点。
    # 模型常见的错误是"第 2 集接着 S1–S5 往后编 S6/S7/S8"，而大纲里根本没有
    # 这些地点——这是可确定映射的引用错误，不该烧掉整轮重写。
    plan = next((e for e in outline.episodes if e.ep == ep.ep), None)
    valid_global = [s.id for s in outline.scenes]
    # 真正非法的只有"大纲里完全不存在的 id"；跨集复用别集地点虽然不理想，
    # 但仍在大纲里，不能强行改写（否则会与已缓存站位/美术对不上）。
    preferred = [sid for sid in (plan.scene_ids if plan else [])
                 if sid in valid_global]
    allowed = preferred or valid_global
    art_scenes = {a.id: a for a in getattr(art, "scenes", [])}
    if allowed:
        for i, sc in enumerate(ep.scenes):
            if sc.id in valid_global:
                continue
            old = sc.id
            loc = (sc.location or "").strip().lower()
            target = None
            # 先按地点文本匹配美术场景；匹配不到再轮转，至少保证引用合法。
            if loc:
                for sid in allowed:
                    a = art_scenes.get(sid)
                    name = (getattr(a, "name", "") or "").strip().lower()
                    if name and (name in loc or loc in name):
                        target = sid
                        break
            if target is None:
                target = allowed[i % len(allowed)]
            sc.id = target
            a = art_scenes.get(target)
            if a is not None and getattr(a, "name", ""):
                sc.location = a.name
            progress(f"  [FIX] 场景 {i + 1} 的 id {old} 不在本集可用地点 "
                     f"{allowed}，映射为 {target}"
                     f"（{getattr(a, 'name', '') or '同地点复用'}）")

    # 1) 道具 → 场陈设 objects
    prop_names = {x.id: x.name for x in art.props}
    fixed = 0
    for sc in ep.scenes:
        for pid in sc.props:
            name = prop_names.get(pid)
            if name and name not in sc.objects:
                sc.objects.append(name)
                fixed += 1
    if fixed:
        progress(f"  [FIX] 已补登记 {fixed} 个道具名到场次 objects")

    # 2) 悬空引用清理：只删不存在的 id / 非法元数据，不改剧情文本
    valid_chars = {c.id for c in (cast.characters if cast else outline.characters)}
    valid_props = {p.id for p in art.props}
    valid_beats = {b.id for b in outline.beats}
    valid_scene_ids = {s.id for s in outline.scenes}
    wardrobe_by_char = ({c.id: {w.state_id for w in c.wardrobe_states}
                         for c in cast.characters} if cast else {})
    cleaned = 0
    for sc in ep.scenes:
        bad = [c for c in sc.characters if c not in valid_chars]
        if bad:
            sc.characters = [c for c in sc.characters if c in valid_chars]
            progress(f"  [FIX] {sc.id} 移除不存在的角色 {bad}")
            cleaned += 1
        bad = [p for p in sc.props if p not in valid_props]
        if bad:
            sc.props = [p for p in sc.props if p in valid_props]
            progress(f"  [FIX] {sc.id} 移除不存在的道具 {bad}")
            cleaned += 1
        bad_w = sorted(cid for cid, wid in sc.wardrobe.items()
                       if cid in wardrobe_by_char and wid not in wardrobe_by_char[cid])
        if bad_w:
            for cid in bad_w:
                sc.wardrobe.pop(cid, None)
            progress(f"  [FIX] {sc.id} 移除不在服装库里的状态 {bad_w}")
            cleaned += 1
        dangling = []
        for cb in sc.callbacks:
            cited = re.findall(r"\b(C\d+|S\d+|B\d+)\b", cb)
            if any(mid not in valid_chars and mid not in valid_scene_ids
                   and mid not in valid_beats for mid in cited):
                dangling.append(cb)
        if dangling:
            sc.callbacks = [cb for cb in sc.callbacks if cb not in dangling]
            progress(f"  [FIX] {sc.id} 移除悬空回指 {dangling}")
            cleaned += 1
        if sc.restructure:
            rs = sc.restructure
            if not (rs.get("action") in {"merge", "split"}
                    and (rs.get("reason") or "").strip()
                    and rs.get("from") and rs.get("to")):
                sc.restructure = {}
                progress(f"  [FIX] {sc.id} 移除不完整的场景重构记录")
                cleaned += 1
        for b in sc.beats:
            bad_sc = [cid for cid in (b.state_change or {}) if cid not in valid_chars]
            if bad_sc:
                b.state_change = {cid: v for cid, v in b.state_change.items()
                                  if cid in valid_chars}
                progress(f"  [FIX] {sc.id} 移除无主 state_change {bad_sc}")
                cleaned += 1
            # 真实存在的说话人/动作点名角色 → 补登记为“在场”，否则空间表排不出站位
            if (b.speaker and b.speaker != "VO" and b.speaker in valid_chars
                    and b.speaker not in sc.characters):
                sc.characters.append(b.speaker)
                progress(f"  [FIX] {sc.id} 补登记在场说话人 {b.speaker}")
                cleaned += 1
            for cid in sorted(set(re.findall(r"\bC\d+\b", b.action or ""))):
                if cid in valid_chars and cid not in sc.characters:
                    sc.characters.append(cid)
                    progress(f"  [FIX] {sc.id} 补登记动作点名角色 {cid}")
                    cleaned += 1
    bad_b = [t for t in ep.beats_claimed if t not in valid_beats]
    if bad_b:
        ep.beats_claimed = [t for t in ep.beats_claimed if t in valid_beats]
        progress(f"  [FIX] 移除不存在的爽点认领 {bad_b}")
        cleaned += 1
    if cleaned:
        progress(f"  [FIX] 悬空引用清理完成（{cleaned} 处）")

    # 2.5) key_lines 落拍兜底：改写/转述的关键台词确定性还原为原文。
    # 放在拆拍（4）之前：还原后的长句仍会走既有拆拍（前后相邻，拼接=原文）。
    if plan is not None:
        _repair_key_lines(ep, plan, progress)

    # 3) 场次 beat 口径：非法值映射到合法节拍；末场强制“结尾钩子”
    last_idx = len(ep.scenes) - 1
    for i, sc in enumerate(ep.scenes):
        if sc.beat not in SHOT_BEATS:
            mapped = "结尾钩子" if i == last_idx else _BEAT_FALLBACK.get(sc.beat, "收束")
            progress(f"  [FIX] {sc.id} 的 beat={sc.beat!r} 不在节拍表，"
                     f"映射为 {mapped!r}")
            sc.beat = mapped
        elif i == last_idx and sc.beat != "结尾钩子":
            progress(f"  [FIX] 末场 beat={sc.beat!r} → '结尾钩子'")
            sc.beat = "结尾钩子"

    # 4) 超长台词：拆成多拍（总字数/时长不变）
    # 句长上限来自剧作质量口径；但单镜有引擎时长上限，超过
    # max_shot_seconds * chars_per_second 的台词拍在分镜层**永远装不进一个切镜**，
    # 且一拍不能被两个切镜重复认领。所以按两者更小值拆拍。
    shot_line_limit = max(1, int(p.max_shot_seconds * p.chars_per_second))
    line_limit = min(p.max_line_chars, shot_line_limit)
    for sc in ep.scenes:
        beats: list[ScriptBeat] = []
        split_count = 0
        for b in sc.beats:
            if not b.line or _line_chars(b.line) <= line_limit:
                beats.append(b)
                continue
            chunks = _split_long_line(b.line, line_limit)
            for k, chunk in enumerate(chunks):
                nb = b.model_copy(deep=True)
                nb.line = chunk
                if k < len(chunks) - 1:
                    # 状态变更/拍末快照只属于整句说完后的最后一拍
                    nb.state_change = {}
                    nb.state_out = {}
                beats.append(nb)
            split_count += 1
            secs = _line_chars(b.line) / p.chars_per_second
            progress(f"  [FIX] {sc.id} 台词 {_line_chars(b.line)} 字 "
                     f"({secs:.1f}s) 超过单镜 {p.max_shot_seconds:g}s，"
                     f"已拆为 {len(chunks)} 拍")
        if split_count:
            sc.beats = beats

    # 4.3) 动作超载拍：引擎单镜动作上限内的拍才能一镜一拍渲染。
    # 拆镜/拆段优先级高于加长单镜——在剧本层就把多动作拍拆成"一拍一动作"，
    # 分镜层天然得到可渲染的镜（状态/重点归末拍，集长硬上限保护）。
    _split_overloaded_action_beats(ep, p, progress)

    # 4.4) 旁白/转场：先按拍号/角色/条数做确定性清理，避免一个越界 beat 整轮重写。
    _repair_narration(ep, p, progress, cast=cast)

    # 4.5) coverage 配额：只重标已有动作拍，不增删剧情/台词。
    _repair_script_coverage(ep, p, progress, dialogue_light=dialogue_light)

    # 4.6) 情节对账：must_show / 场间因果 / 关键道具 / 关键镜头权重。
    if plan is not None:
        _repair_script_plot(ep, plan, art, progress)

    # 5) hook_beat 指针归一：失效或不在前 3 拍 → 本集第 1 拍
    positions = [(si, bi) for si, sc in enumerate(ep.scenes, 1)
                 for bi in range(1, len(sc.beats) + 1)]
    hb = tuple(ep.hook_beat)
    if hb not in positions or positions.index(hb) >= 3:
        ep.hook_beat = positions[0]
        progress(f"  [FIX] hook_beat={hb} → {positions[0]}（冷开场需在前 3 拍）")

    # 6) 集末 cliff 时长
    last = ep.scenes[-1]
    before = _scene_seconds(last, p)
    if before >= 5.0:
        return
    max_total = ep.target_seconds * (1 + p.tolerance)
    char_names = {c.id: c.name for c in outline.characters}
    fallback_names = [char_names.get(cid) for cid in last.characters]
    fallback_name = next((n for n in fallback_names if n), "")
    fallback = (f"{fallback_name}僵在原地，画面定格。"
                if fallback_name else "画面定格，最后一瞬停住。")
    actions = [_cliff_action_text(ep.cliff, fallback), fallback]
    i = 0
    while _scene_seconds(last, p) < 5.0:
        total = sum(_scene_seconds(s, p) for s in ep.scenes)
        if total + p.action_seconds > max_total + 1e-6:
            progress(f"  [WARN] 集末场景仅 {before:.1f}s，但补拍会超出 "
                     f"{max_total:.1f}s 硬上限，交给质量门回喂")
            return
        prev = last.beats[-1]
        if prev.state_out:
            state_from = "；".join(f"{cid} {snap}" for cid, snap in prev.state_out.items())
        else:
            state_from = prev.state_from or "承接上一拍终态"
        last.beats.append(ScriptBeat(
            action=actions[min(i, len(actions) - 1)],
            coverage="action",
            state_from=state_from,
            screen_side=prev.screen_side or "center",
        ))
        i += 1
    progress(f"  [FIX] 集末场景 {before:.1f}s → {_scene_seconds(last, p):.1f}s"
             "（补齐 cliff 落点拍）")


def _split_long_segments(segments: list[StorySegment], ep: int,
                         progress: Progress,
                         params: ScriptParams | None = None) -> list[StorySegment]:
    """把超过 max_segment_seconds 的分镜段按切镜边界拆开。

    契约要求「一场一段、每段 ≤ 引擎单次生成上限」，但剧本场时长由拍数决定；
    长场无论怎么重写都不可能压进一段。这里在镜与镜之间拆段：场次、节拍、
    总时长全部不变，只让每段落回上限内，下游按 scene_index 聚合仍然成立。
    """
    limit = (params or ScriptParams()).max_segment_seconds
    out: list[StorySegment] = []
    split = 0
    for seg in segments:
        chunks: list[list[StoryCut]] = []
        cur: list[StoryCut] = []
        total = 0.0
        for c in seg.cuts:
            if cur and total + c.seconds > limit + 1e-6:
                chunks.append(cur)
                cur, total = [], 0.0
            cur.append(c)
            total += c.seconds
        if cur:
            chunks.append(cur)
        if len(chunks) == 1:
            out.append(seg)
            continue
        split += 1
        totals = [round(sum(c.seconds for c in ch), 2) for ch in chunks]
        for k, ch in enumerate(chunks):
            part = seg if k == 0 else seg.model_copy(deep=True)
            part.cuts = ch
            if k > 0:
                part.transition_in = None   # 转场只属于本场第一段
            out.append(part)
        progress(f"  [FIX] 分镜段 {seg.id} 共 {sum(totals):.2f}s，"
                 f"按镜拆为 {len(chunks)} 段 {totals}（每段 ≤{limit:g}s）")
    # 段号必须全集唯一：拆分后按顺序重新编号。
    for n, seg in enumerate(out, 1):
        seg.id = f"E{ep:02d}-{n:02d}"
    if split:
        progress(f"  [FIX] 长段拆分完成：{len(segments)} 段 → {len(out)} 段")
    return out


def _repair_segment_output(seg: StorySegment, scene: ScriptScene,
                           progress: Progress,
                           params: ScriptParams | None = None,
                           cast: Cast | None = None) -> None:
    """分镜确定性修复：先做算术，再交给质量门。

    LLM 很容易把 8.70s 写成 8.33s、把 4 拍并成 3 镜。这些是**可计算**的错误，
    重掷骰子既费钱又漂移，所以在这里直接按剧本拍长归一：

    - 一个切镜覆盖多拍且（超过引擎单镜上限 / 台词句数超限 / 镜数不足）→ 按拍拆开；
    - 每镜 seconds = 它认领的拍长之和（动作拍与语速取配置）；
    - 残差在 [min_shot_seconds, max_shot_seconds] 内均摊，保证合计精确；
    - 画内可见道具的持有者在画外且本镜未易主 → 从本镜 props 移除（世界状态保留）；
    - 双人对白/反应镜 facing 未指向对方 → 按 where 自动校正；
    - 同框锁脸数超过引擎上限 → 保留说话人/道具持有者，收敛为单人镜。
    """
    p = params or ScriptParams()
    max_shot = p.max_shot_seconds
    min_shot = p.min_shot_seconds
    if not seg.cuts:
        return
    cuts = sorted(seg.cuts, key=lambda c: (c.beats[0], c.beats[1]))
    min_cuts = min(4, len(scene.beats))

    def _span(a: int, b: int) -> float:
        return sum(_beat_seconds(scene.beats[i - 1], p) for i in range(a, b + 1))

    expanded: list[StoryCut] = []
    split_count = 0
    for c in cuts:
        a, b = c.beats
        n_lines = sum(1 for i in range(a, b + 1) if scene.beats[i - 1].line)
        too_many_lines = (p.max_lines_per_shot is not None
                          and n_lines > p.max_lines_per_shot)
        if b > a and (_span(a, b) > max_shot + 1e-6
                      or too_many_lines or len(cuts) < min_cuts):
            for i in range(a, b + 1):
                nb = c.model_copy(deep=True)
                nb.beats = (i, i)
                expanded.append(nb)
            split_count += 1
        else:
            expanded.append(c)
    if split_count:
        line_rule = (f"每镜 ≤{p.max_lines_per_shot} 句台词"
                     if p.max_lines_per_shot is not None else "台词句数不限")
        progress(f"  [FIX] 分镜拆分 {split_count} 个多拍切镜"
                 f"（本场至少 {min_cuts} 镜、单镜 ≤{max_shot:g}s、{line_rule}）")

    # 覆盖率继承：分镜的 coverage 必须跟随剧本层规划（认领的首拍），
    # 否则 reaction/insert/establish/pov 配额会在最后一层漂移，drama 门必挂。
    coverage_fixed = 0
    for c in expanded:
        a = max(1, min(len(scene.beats), c.beats[0]))
        beat = scene.beats[a - 1]
        want = beat.coverage or ("dialogue" if beat.line else "action")
        if c.coverage != want:
            c.coverage = want
            coverage_fixed += 1
    if coverage_fixed:
        progress(f"  [FIX] 分镜 coverage 按剧本拍规划校正 {coverage_fixed} 镜")

    # 运镜兜底：LLM 若仍写出过量静态机位，按引擎档案确定性补运镜。
    # full=静态 ≤40%；limited=静态 ≤60%（轻度运镜）。改写对象优先取
    # 非定场/反应的中近景镜，保住"每镜必须有运镜"；camera 会被实现层
    # 翻译成运镜指令（compile.py 的 cut_motion 映射）。
    motion_cap = 0.6 if getattr(p, "camera_motion", "full") == "limited" else 0.4
    if expanded:
        n_shots = len(expanded)
        static_idx = [i for i, c in enumerate(expanded)
                      if c.camera == "Static Shot"]
        over = len(static_idx) - int(n_shots * motion_cap)
        if over > 0:
            candidates = [i for i in static_idx
                          if expanded[i].coverage not in {"establish", "reaction"}
                          and expanded[i].size in {"medium", "close",
                                                   "extreme-close"}]
            if not candidates:
                candidates = list(static_idx)
            limited = getattr(p, "camera_motion", "full") == "limited"
            dyn = (["Push In", "Tilt Up", "Tilt Down", "Pull Out"] if limited
                   else ["Push In", "Tracking Shot", "Pan Left", "Pan Right",
                         "Arc Shot"])
            fixed_motion = 0
            for i in candidates[:over]:
                c = expanded[i]
                old = c.camera
                c.camera = dyn[i % len(dyn)]
                # 兜底短语里没有运镜时，补一句镜头运动，让视频引擎真动起来。
                if not c.motion or "camera" not in c.motion.lower():
                    c.motion = (c.motion or "").strip()
                    suffix = "镜头缓慢推近" if limited else \
                        "镜头向主体推近并缓缓环绕"
                    if c.motion:
                        c.motion = f"{c.motion}；{suffix}"
                    else:
                        c.motion = suffix
                    fixed_motion += 1
                progress(f"  [FIX] 切镜 {c.beats} 机位 {old} → {c.camera}"
                         "（每镜必须有运镜）")
            if fixed_motion:
                progress(f"  [FIX] {fixed_motion} 镜 motion 补运镜描述")

    # D：能从剧本拍/场次算出来的字段，不再依赖模型输出。这里只补空值或
    # 需要与剧本对齐的字段，模型已经写好的创作内容（frame/prompt/motion）不覆盖。
    derived = 0
    gaze_fixed = 0
    face_fixed = 0
    prop_fixed = 0
    prop_scope_fixed = 0
    ref_fixed = 0
    state_fixed = 0
    wardrobe_fixed = 0
    by_id = ({ch.id: ch for ch in cast.characters} if cast is not None else {})
    aliases = ({ch.name.strip(): ch.id for ch in cast.characters
                if ch.name.strip()} if cast is not None else {})

    def _strict_cid(value: Any) -> str | None:
        """合法 Cid 或已知人名 → Cid；其余返回 None（不猜）。"""
        if not isinstance(value, str):
            return None
        text = value.strip()
        if text in by_id or re.fullmatch(r"C\d+", text):
            return text
        return aliases.get(text)

    def _loose_cid(value: Any) -> str | None:
        cid = _strict_cid(value)
        if cid:
            return cid
        if isinstance(value, str):
            found = re.search(r"C\d+", value)
            return found.group(0) if found else None
        return None

    # 首镜的 state_from 就是本场 state_in；后续每镜必须以上一镜修复后的
    # state_out 为起点，不能沿用装配时那份（可能含越界道具）的快照。
    prev_state = (expanded[0].state_from.model_copy(deep=True)
                  if expanded and not expanded[0].state_from.empty()
                  else CutState())
    prev_screen: dict[str, str] = {}
    for pos, c in enumerate(expanded):
        # 角色引用归一：EDL 允许 characters 写人名（schema 不拦），但装配
        # CutSubject 时只接受 Cid；缓存里也可能残留旧产物的人名。先把已知
        # 人名映射成 Cid，未知人名剔除，避免在这里抛 ValidationError。
        mapped: list[str] = []
        for value in c.characters:
            cid = _strict_cid(value)
            if cid and cid not in mapped:
                mapped.append(cid)
        if mapped != c.characters:
            c.characters = mapped
            ref_fixed += 1
        for subj in c.subjects:
            cid = _loose_cid(subj.char)
            if cid and cid != subj.char:
                subj.char = cid
                ref_fixed += 1
        kept = [s for s in c.subjects if _strict_cid(s.char)]
        if len(kept) != len(c.subjects):
            c.subjects = kept
            ref_fixed += 1
        for st in (c.state_out, c.state_from):
            for field_name in ("where", "facing", "posture", "wardrobe",
                               "injuries"):
                values = getattr(st, field_name)
                if not values:
                    continue
                remapped = {(_strict_cid(k) or k): v
                            for k, v in values.items()}
                if remapped != values:
                    setattr(st, field_name, remapped)
                    ref_fixed += 1
            if st.props:
                remapped = {k: (_strict_cid(v) or v)
                            for k, v in st.props.items()}
                if remapped != st.props:
                    st.props = remapped
                    ref_fixed += 1

        # 场次道具表是最终权限：本镜只能展示本场声明的道具。画外继承的
        # 道具保留在完整世界状态里，但本场看不到交接，就不能改归属；模型
        # 凭空新增的场外道具则直接从世界状态里丢弃。先修 state_from 链，
        # 再修 state_out，后续镜头才不会继续继承错误归属。
        if pos > 0:
            c.state_from = prev_state.model_copy(deep=True)

        # 状态引用清洗（缓存产物也走这里）：
        # - where/facing/posture/wardrobe/injuries 不允许出现不存在的角色；
        # - 服装态必须是角色状态库里的 state_id，非法值回退到上一镜/场级；
        # - state_out 必须跟随剧本拍级服装账本，缓存旧产物也要纠正。
        if by_id:
            for st in (c.state_out, c.state_from):
                for field_name in ("where", "facing", "posture", "wardrobe",
                                   "injuries"):
                    values = getattr(st, field_name)
                    if not values:
                        continue
                    clean = {k: v for k, v in values.items() if k in by_id}
                    if clean != values:
                        setattr(st, field_name, clean)
                        state_fixed += 1
                for cid, wid in list(st.wardrobe.items()):
                    ch = by_id.get(cid)
                    if ch is None:
                        continue
                    ok = {w.state_id for w in ch.wardrobe_states}
                    if wid in ok:
                        continue
                    fallback = (prev_state.wardrobe.get(cid)
                                or scene.wardrobe.get(cid)
                                or (ch.wardrobe_states[0].state_id
                                    if ch.wardrobe_states else ""))
                    if fallback:
                        st.wardrobe[cid] = fallback
                    else:
                        del st.wardrobe[cid]
                    wardrobe_fixed += 1
        end_beat = max(1, min(len(scene.beats), c.beats[1]))
        for cid, wid in script_wardrobe_at(scene, end_beat).items():
            if wid and c.state_out.wardrobe.get(cid) != wid:
                c.state_out.wardrobe[cid] = wid
                wardrobe_fixed += 1

        allowed_props = set(scene.props)
        visible = [pid for pid in c.props if pid in allowed_props]
        if visible != c.props:
            prop_scope_fixed += len(c.props) - len(visible)
            c.props = visible
        for pid, owner in list(c.state_out.props.items()):
            if pid in allowed_props:
                # 本场可见道具的持有者必须是真实角色或 scene；模型写出
                # 不存在的人名/Cid 时，归到本镜第一个在场角色。
                if owner not in ("scene",) and owner not in by_id:
                    fallback = next((cid for cid in c.characters
                                     if cid in by_id), "")
                    c.state_out.props[pid] = fallback or "scene"
                    prop_scope_fixed += 1
                continue
            inherited = c.state_from.props.get(pid)
            if inherited is None:
                del c.state_out.props[pid]
                prop_scope_fixed += 1
            elif owner != inherited:
                c.state_out.props[pid] = inherited
                prop_scope_fixed += 1

        a = max(1, min(len(scene.beats), c.beats[0]))
        beat = scene.beats[a - 1]
        base = _beat_summary(beat)
        cov = c.coverage
        if not c.frame.strip():
            c.frame = _derived_frame(beat, cov, c.size, scene)
            derived += 1
        if not c.shot.strip():
            c.shot = base
            derived += 1
        if not c.beat_goal.strip():
            c.beat_goal = f"{cov}：{base[:48]}"
            derived += 1
        if not c.purpose.strip():
            c.purpose = f"{scene.id} {cov} 拍：{base[:48]}"
            derived += 1
        if not c.information.strip():
            c.information = scene.information or scene.mood or "推进剧情"
            derived += 1
        if not c.lighting.strip():
            c.lighting = scene.lighting
            derived += 1
        if not c.lens.strip():
            c.lens = _LENS_BY_SIZE.get(c.size, "")
            derived += 1
        if not c.camera_position.strip():
            c.camera_position = "eye-level"
            derived += 1
        if not c.focus.strip() and c.size in {"close", "extreme-close"}:
            c.focus = "shallow depth of field"
            derived += 1
        if not c.motion.strip():
            c.motion = ("持续的自然动作" if p.prompt_language == "zh"
                        else "continuous natural action")
            derived += 1
        if not c.state_from.empty():
            pass
        else:
            c.state_from = prev_state.model_copy(deep=True)
        if not c.screen_side:
            c.screen_side = {
                cid: prev_screen.get(cid, "center") for cid in c.characters}
            derived += 1
        else:
            for cid in c.characters:
                c.screen_side.setdefault(cid, prev_screen.get(cid, "center"))
        have = {s.char for s in c.subjects}
        missing = [cid for cid in c.characters if cid not in have]
        if missing:
            c.subjects.extend(
                CutSubject(
                    char=cid,
                    wardrobe_state=(c.state_out.wardrobe.get(cid, "")
                                    or scene.wardrobe.get(cid, "")),
                    pose=c.state_out.posture.get(cid, ""),
                    expression="",
                )
                for cid in missing)
            derived += 1
        # 道具在本镜 props 里、持有者却不在画内：如果本镜没有发生易主，
        # 说明它只是被继承的世界状态误塞进了画框。从本镜可见道具里移除，
        # 世界状态仍保留在 state_out，下一镜继续有效。
        for pid in list(c.props):
            owner = (c.state_out.props.get(pid)
                     or c.state_from.props.get(pid))
            if owner in (None, "scene") or owner in c.characters:
                continue
            if c.state_from.props.get(pid) in (None, owner):
                c.props.remove(pid)
                prop_fixed += 1
        # 画幅锁脸数超引擎上限时，确定性收敛为单人镜。模型常忽略
        # “每画幅最多 N 张正脸”，重写三次仍会写出双人正打；这里保留
        # 台词说话人/可见道具持有者，其余锁脸角色转为画外。
        faces = [cid for cid in c.characters
                 if cid in by_id and face_lock_required(by_id[cid])]
        if (p.max_faces_per_shot is not None
                and len(faces) > p.max_faces_per_shot
                and not c.allow_multi_face):
            priority: list[str] = []
            for owner in (c.state_out.props.get(pid) for pid in c.props):
                if owner in faces and owner not in priority:
                    priority.append(owner)
            if beat.speaker in faces and beat.speaker not in priority:
                priority.append(beat.speaker)
            for cid in faces:
                if cid not in priority:
                    priority.append(cid)
            keep = set(priority[:p.max_faces_per_shot])
            drop = set(faces) - keep
            if drop:
                c.characters = [cid for cid in c.characters if cid not in drop]
                c.subjects = [s for s in c.subjects if s.char not in drop]
                face_fixed += 1
        # 双人对白/反应镜的 facing 是可由 where 唯一算出的：模型写错时
        # 直接校正，而不是整场重写（“两人都在北边、都朝北”是重试高发项）。
        if repair_gaze(c, cast):
            gaze_fixed += 1
        prev_state = c.state_out
        prev_screen = dict(c.screen_side)
    if derived:
        progress(f"  [FIX] 分镜派生字段补齐 {derived} 项"
                 "（frame/purpose/information/state_from/subjects 等）")
    if gaze_fixed:
        progress(f"  [FIX] 双人镜目线自动校正 {gaze_fixed} 镜（facing 指向同框对象）")
    if face_fixed:
        progress(f"  [FIX] 超锁脸上限的切镜收敛为单人镜 {face_fixed} 镜"
                 f"（上限 {p.max_faces_per_shot} 张/画幅）")
    if prop_fixed:
        progress(f"  [FIX] 移除画内无主的可见道具 {prop_fixed} 处"
                 "（持有者在画外，世界状态继续保留）")
    if prop_scope_fixed:
        progress(f"  [FIX] 按场次道具表归一 {prop_scope_fixed} 处"
                 "（场外道具不展示；已继承的归属保持不变）")
    if ref_fixed:
        progress(f"  [FIX] 角色引用归一 {ref_fixed} 处"
                 "（人名 → Cid / 剔除未知人物）")
    if state_fixed:
        progress(f"  [FIX] 状态表剔除未知角色键 {state_fixed} 处")
    if wardrobe_fixed:
        progress(f"  [FIX] 服装态按剧本账本/状态库归一 {wardrobe_fixed} 处")

    total_want = _scene_seconds(scene, p)
    lower: list[float] = []
    durs: list[float] = []
    for c in expanded:
        a, b = c.beats
        span = _span(a, b)
        lower.append(max(min_shot, span))
        durs.append(max(lower[-1], min(max_shot, span)))

    # 均摊残差：只在 [lower, max_shot] 内调整，保证合计精确
    for _ in range(200):
        diff = round(total_want - sum(durs), 4)
        if abs(diff) < 0.005:
            break
        free = [i for i, d in enumerate(durs)
                if (diff > 0 and d < max_shot - 1e-6)
                or (diff < 0 and d > lower[i] + 1e-6)]
        if not free:
            break
        share = diff / len(free)
        for i in free:
            durs[i] = max(lower[i], min(max_shot, durs[i] + share))
    durs = [round(d, 2) for d in durs]
    residual = round(total_want - sum(durs), 2)
    for i, d in enumerate(durs):
        if abs(residual) < 0.005:
            break
        room = ((max_shot - d) if residual > 0
                else (d - lower[i]))
        if room <= 1e-6:
            continue
        step = math.copysign(min(abs(residual), room), residual)
        durs[i] = round(d + step, 2)
        residual = round(residual - step, 2)

    old_total = sum(c.seconds for c in expanded)
    changed = len(expanded) != len(cuts) or any(
        abs(c.seconds - d) > 0.005 for c, d in zip(expanded, durs))
    for c, d in zip(expanded, durs):
        c.seconds = d
    seg.cuts = expanded
    if changed:
        progress(f"  [FIX] 切镜时长归一：{old_total:.2f}s → "
                 f"{sum(durs):.2f}s（剧本场 {total_want:.2f}s，"
                 f"{len(expanded)} 镜）")
    if len(expanded) < min_cuts:
        progress(f"  [WARN] 本场只有 {len(expanded)} 个切镜，至少 {min_cuts} 个；"
                 "节拍认领有缺口，交给质量门")


def _repair_staging_output(staging: Staging, ep: ScriptEpisode,
                           progress: Progress) -> int:
    """把人物站位引用、但地点物件表漏登记的对象补进去。

    这是纯记账修复：模型已经把人物绑到“废弃车辆/卷帘门”上，重写整集站位
    不会让地点物件表更正确，只会在 3 次重试后仍然失败。补一个居中的可站物件
    即可让平面调度成立，后续仍可在 Studio 里微调坐标。
    """
    locs = {loc.scene_id: loc for loc in staging.locations}
    scenes = [s for s in staging.scenes if s.ep == ep.ep]
    fixed = 0
    for sc, st in zip(ep.scenes, scenes):
        loc = locs.get(sc.id)
        if loc is None:
            continue
        names = {item.name for item in loc.items}
        for place in st.places:
            if not place.present or not place.item or place.item in names:
                continue
            loc.items.append(StagingItem(
                name=place.item, plan=[0.5, 0.5, 0.5, 0.5], on=True))
            names.add(place.item)
            fixed += 1
            progress(f"  [FIX] {sc.id} 地点物件表补登记 {place.item!r}"
                     "（人物站位已引用，坐标先置中）")
    return fixed


def _merge_stagings(stagings: list[Staging], source: str) -> Staging:
    """合并各集站位；同一 scene_id 的地点物件表取并集。

    多集可能复用同一个 scene_id（例如 S2 黄砖路），但每集的物件表是按
    本集剧本重新列的：ep1 写“黄砖路起点”，ep2 写“黄砖路/石块”。若按
    ``setdefault`` 只保留先出现的地点，合并后的地点表就装不下后一集人物
    站位的引用，最终 ``validate_all`` 会在 ep2 上报
    “item 不在地点物件表里”。这里按名字求并集，保留首次出现的坐标。
    """
    locations: dict[str, StagingLocation] = {}
    for st in stagings:
        for loc in st.locations:
            cur = locations.get(loc.scene_id)
            if cur is None:
                locations[loc.scene_id] = loc.model_copy(deep=True)
                continue
            names = {item.name for item in cur.items}
            for item in loc.items:
                if item.name not in names:
                    cur.items.append(item.model_copy(deep=True))
                    names.add(item.name)
    return Staging(source=source, locations=list(locations.values()),
                   scenes=[s for st in stagings for s in st.scenes])


def _resolve_prop_id(name: str, props: list[OutlineProp]) -> str | None:
    """把设计里的道具原名对到 bible 的 P#；允许“银鞋/银色的鞋”这类包含关系。"""
    key = _event_key(name)
    if not key:
        return None
    for p in props:
        if _event_key(p.name) == key:
            return p.id
    for p in props:
        pn = _event_key(p.name)
        if pn and min(len(pn), len(key)) >= 2 and (pn in key or key in pn):
            return p.id
    return None


def _prop_scale(name: str) -> str:
    if any(k in name for k in ("桌", "床", "门", "柜", "车", "船", "椅",
                               "架", "墙", "屋", "桥", "台")):
        return "家具级"
    if any(k in name for k in ("信", "纸", "钥匙", "戒指", "鞋", "包", "箱",
                               "杯", "瓶", "书", "刀", "剑", "灯", "镜", "伞")):
        return "手持级"
    return "桌面级"


def _next_prop_id(props: list[OutlineProp]) -> str:
    nums = [int(p.id[1:]) for p in props
            if p.id.startswith("P") and p.id[1:].isdigit()]
    return f"P{max(nums, default=0) + 1}"


def _apply_design_to_plans(plans: list[EpisodePlan],
                           design_eps: dict[int, DesignEpisode],
                           seconds: float,
                           bible: StoryBible | None = None,
                           progress: Progress | None = None) -> None:
    """把情节设计的硬字段写进分集梗概（模型不得改集数/必拍事件）。

    关键道具还要落成真实的 P# 并写进 prop_ids：否则大纲裁道具时会把
    “本集关键道具”当冗余删掉，剧本阶段就再也拍不到它的特写。
    """
    created: list[str] = []
    for plan in plans:
        d = design_eps.get(plan.ep)
        if d is None:
            continue
        plan.title = d.title or plan.title
        plan.must_show = list(d.must_show)
        plan.key_lines = list(d.key_lines)
        plan.key_props = list(d.key_props)
        plan.dramatic_question = d.dramatic_question
        plan.turn = d.turn
        plan.climax = d.climax
        plan.cliff = d.cliff
        plan.estimated_seconds = seconds
        if not (plan.hook or "").strip():
            plan.hook = d.hook or d.cliff
        if not (plan.synopsis or "").strip():
            plan.synopsis = d.synopsis
        if bible is None:
            continue
        for name in plan.key_props:
            pid = _resolve_prop_id(name, bible.props)
            if pid is None:
                pid = _next_prop_id(bible.props)
                bible.props.append(OutlineProp(
                    id=pid, name=name,
                    function=f"第 {plan.ep} 集关键道具：{name}",
                    scale=_prop_scale(name)))
                created.append(f"{pid}（{name}）")
            if pid not in plan.prop_ids:
                plan.prop_ids.append(pid)
    if created and progress is not None:
        progress("  [FIX] 关键道具不在 bible 中，已补建："
                 + "、".join(created))


def _beat_summary(beat: ScriptBeat) -> str:
    if beat.action:
        return beat.action.strip()
    if beat.line:
        return f"{beat.speaker or ''}：{beat.line}".strip("：")
    return ""


def _coverage_of_beat(beat: ScriptBeat) -> str:
    return beat.coverage or ("dialogue" if beat.line else "action")


def _action_beats_by_index(line: ActionBeatLine) -> dict[int, Any]:
    out: dict[int, Any] = {}
    for ab in line.beats:
        a, b = ab.beats
        for i in range(a, b + 1):
            out.setdefault(i, ab)
    return out


def _normalize_edl_plan(plan: SegmentPlan, scene: ScriptScene,
                        cast: Cast | None, art: Art | None,
                        state_in: CutState | None = None,
                        progress: Progress | None = None) -> None:
    """在装配前把 EDL 归一成「一拍一镜 + 合法引用 + 合法状态」。

    这些错误全部可由代码判定，不该整段重问模型：

    * 多拍切镜展开成一拍一镜；重复拍保留第一条；漏拍用中性单拍补齐；
    * characters 只保留本场 Cid（人名先映射）；
    * props 只保留本场 P#（道具名先映射）；
    * state_out 只保留合法角色键；非法服装态删除（交给装配继承）；
    * 场外道具若已从上一镜继承则锁定原归属，否则从世界状态里删除；
    * 可见道具的持有者非法时，归到本镜第一个在场角色（或 scene）。
    """
    say = progress or (lambda _m: None)
    n = len(scene.beats)
    if n <= 0:
        return

    cast_chars = ({c.id: c for c in cast.characters} if cast is not None else {})
    name_to_cid = ({c.name.strip(): c.id for c in cast.characters
                    if c.name and c.name.strip()} if cast is not None else {})
    valid_cids = set(cast_chars) or set(scene.characters)
    scene_chars = list(scene.characters)
    scene_charset = set(scene_chars)
    prop_by_id = ({p.id: p for p in art.props} if art is not None else {})
    scene_props = set(scene.props)
    inherited_props = dict(state_in.props) if state_in is not None else {}

    def _cid(value: Any) -> str | None:
        if not isinstance(value, str):
            return None
        text = value.strip()
        if text in valid_cids:
            return text
        mapped = name_to_cid.get(text)
        return mapped if mapped in valid_cids else None

    def _pid(value: Any, *, any_prop: bool = False) -> str | None:
        if not isinstance(value, str):
            return None
        text = value.strip()
        if text in scene_props:
            return text
        if art is not None:
            resolved = _resolve_prop_id(text, art.props)
            if resolved and (any_prop or resolved in scene_props):
                return resolved
        if any_prop and (text in prop_by_id
                         or re.fullmatch(r"P\d+", text)):
            return text
        return None

    # 1) 展开/补齐：cuts 必须严格覆盖 1..n，每拍恰好一镜。
    expanded: list[CutPlan] = []
    next_i = 1
    for cp in plan.cuts:
        if cp.beats is None:
            a = b = next_i
        else:
            a, b = cp.beats
        if a < 1 or b < a or a > n:
            continue
        b = min(b, n)
        for i in range(a, b + 1):
            nb = cp.model_copy(deep=True)
            nb.beats = (i, i)
            expanded.append(nb)
        next_i = max(next_i, b + 1)
    expanded.sort(key=lambda c: c.beats[0])
    seen: set[int] = set()
    kept: list[CutPlan] = []
    duplicate = 0
    for cp in expanded:
        i = cp.beats[0]
        if i in seen:
            duplicate += 1
            continue
        seen.add(i)
        kept.append(cp)
    missing = [i for i in range(1, n + 1) if i not in seen]
    for i in missing:
        kept.append(CutPlan(beats=(i, i), size="medium",
                            camera="Static Shot"))
    kept.sort(key=lambda c: c.beats[0])
    plan.cuts = kept

    # 2) 引用与状态清洗。
    ref_fixed = 0
    wardrobe_fixed = 0
    prop_fixed = 0
    # 道具归属按镜头顺序推演：只有在本镜**看得见**时才允许易主。
    # 真实故障（绿野仙踪 EP2 S5）：上一场在画外把 P2 归到 scene，下一场
    # 模型顺着这个错误状态写 scene→C4，但本镜既没带 P2、cut_reason 也不是
    # action_done，于是被判“交接不可见”后整场重写。把画外易主抹平即可。
    running_props: dict[str, str] = dict(inherited_props)
    prev_chars: set[str] = set()
    for cp in plan.cuts:
        chars: list[str] = []
        for value in cp.characters:
            cid = _cid(value)
            if cid and cid in scene_charset and cid not in chars:
                chars.append(cid)
        if chars != cp.characters:
            cp.characters = chars
            ref_fixed += 1

        props: list[str] = []
        for value in cp.props:
            pid = _pid(value)
            if pid and pid not in props:
                props.append(pid)
        if props != cp.props:
            cp.props = props
            ref_fixed += 1

        raw_state = cp.state_out if isinstance(cp.state_out, dict) else {}
        state: dict[str, Any] = {}
        for field in ("where", "facing", "posture", "wardrobe", "injuries"):
            values = raw_state.get(field)
            if not isinstance(values, dict):
                continue
            clean: dict[str, str] = {}
            for key, value in values.items():
                cid = _cid(key)
                if not cid or not isinstance(value, str) or not value.strip():
                    ref_fixed += 1
                    continue
                if field in {"where", "facing"} and value not in DIRECTIONS:
                    ref_fixed += 1
                    continue
                if field == "wardrobe" and cid in cast_chars:
                    ok = {w.state_id for w in cast_chars[cid].wardrobe_states}
                    if value not in ok:
                        wardrobe_fixed += 1
                        continue
                clean[cid] = value.strip()
            if clean:
                state[field] = clean
        # 本镜可见道具即使没在 state_out 里声明，装配时也会默认归 scene；
        # 先把它们纳入推演，后续镜头才有正确的上一归属。
        for pid in props:
            running_props.setdefault(pid, "scene")
        raw_props = raw_state.get("props")
        if isinstance(raw_props, dict):
            clean_props: dict[str, str] = {}
            fallback_owner = (next((cid for cid in cp.characters
                                    if cid in valid_cids), "")
                              or next((cid for cid in scene_chars
                                       if cid in valid_cids), "")
                              or "scene")
            for key, value in raw_props.items():
                pid = _pid(key, any_prop=True)
                if not pid:
                    prop_fixed += 1
                    continue
                if pid not in scene_props:
                    # 场外道具：只能继承，不能在本场视线外凭空易主。
                    if pid in inherited_props:
                        clean_props[pid] = inherited_props[pid]
                    else:
                        prop_fixed += 1
                    continue
                owner = _cid(value)
                if owner is None:
                    owner = "scene" if value == "scene" else fallback_owner
                    prop_fixed += 1
                if owner not in valid_cids and owner != "scene":
                    owner = fallback_owner
                    prop_fixed += 1

                prev_owner = running_props.get(pid, "scene")
                if owner != prev_owner:
                    visible = pid in props
                    if visible and owner == "scene":
                        pass                      # 放在画面里的地面/桌面
                    elif visible and owner in chars:
                        if prev_owner == "scene":
                            # 同一镜里看得见道具和新持有者 = 交接完成。
                            # continuity 用 cut_reason 判定是否算动作完成。
                            if cp.cut_reason != "action_done":
                                cp.cut_reason = "action_done"
                                prop_fixed += 1
                        elif prev_owner not in chars \
                                and owner not in prev_chars:
                            # 旧持有者不在本镜、新持有者上一镜也不在画内，
                            # 观众看不到交接：保持原归属。
                            owner = prev_owner
                            prop_fixed += 1
                    else:
                        # 本镜根本没展示道具，不可能看见易主。
                        owner = prev_owner
                        prop_fixed += 1
                clean_props[pid] = owner
                running_props[pid] = owner
            if clean_props:
                state["props"] = clean_props
        cp.state_out = state
        prev_chars = set(chars)

    if missing or duplicate or ref_fixed or wardrobe_fixed or prop_fixed:
        bits: list[str] = []
        if missing:
            bits.append(f"补漏拍 {missing}")
        if duplicate:
            bits.append(f"合并重复拍 {duplicate}")
        if ref_fixed:
            bits.append(f"清洗引用 {ref_fixed}")
        if wardrobe_fixed:
            bits.append(f"剔除非法服装态 {wardrobe_fixed}")
        if prop_fixed:
            bits.append(f"归一道具状态 {prop_fixed}")
        say("  [FIX] EDL 预归一：" + "；".join(bits))


def _normalize_plan_units(plan: SegmentPlan,
                          scene: ScriptScene) -> list[tuple[int, CutPlan]]:
    """把 EDL 的 cuts 展平成「一拍一镜」并排序。

    允许模型省略 beats（数组顺序即拍序），也允许写 [a,b]；跨多拍的项会复制成
    每拍一项。重复/缺拍不在这里发明内容，交给质量门回喂。
    """
    units: list[tuple[int, CutPlan]] = []
    next_i = 1
    for cp in plan.cuts:
        if cp.beats is None:
            a = b = next_i
        else:
            a, b = cp.beats
        if a < 1 or b < a:
            continue
        for i in range(a, b + 1):
            units.append((i, cp))
        next_i = max(next_i, b + 1)
    units.sort(key=lambda x: x[0])
    return units


def _plan_characters(cp: CutPlan, scene: ScriptScene, beat: ScriptBeat,
                     action_beat: Any, coverage: str) -> list[str]:
    """本镜人物：只接受 Cid。

    模型偶尔把“亨利叔叔”这类人名写进 characters；CutPlan 不校验该字段，
    若原样传给 CutSubject(char=...) 会在装配阶段抛 ValidationError，整场
    EDL 直接失败。这里只保留合法 Cid（包括本场外的 Cid，交给质量门报错），
    全是人名时退回剧本拍/场次兜底。
    """
    def _ids(values: Any) -> list[str]:
        return [v.strip() for v in values
                if isinstance(v, str) and re.fullmatch(r"C\d+", v.strip())]

    ids = _ids(cp.characters)
    if ids:
        return ids
    if beat.speaker and _ids([beat.speaker]):
        return _ids([beat.speaker])
    who = getattr(action_beat, "who", "") if action_beat is not None else ""
    if who and _ids([who]):
        return _ids([who])
    # insert 常是手/脚/道具特写：有动作主体就跟着主体，否则至少给场次主角，
    # 避免 state_out 为空或道具持有者「不在画内」。
    if coverage in {"establish", "transition"}:
        return list(scene.characters)
    return list(scene.characters[:1])


def _state_after(prev: CutState, cp: CutPlan, scene: ScriptScene,
                 beat: ScriptBeat, action_beat: Any,
                 characters: list[str], cast: Cast | None = None) -> CutState:
    """上一镜状态 + EDL 声明的变化 + 可推导的兜底，输出合法 CutState。"""
    data = prev.model_dump(mode="json")
    extra = cp.state_out if isinstance(cp.state_out, dict) else {}
    for field in ("where", "facing", "posture", "wardrobe", "props", "injuries"):
        values = extra.get(field)
        if not isinstance(values, dict):
            continue
        for key, value in values.items():
            if not isinstance(value, str) or not value.strip():
                continue
            if field in {"where", "facing"} and value not in DIRECTIONS:
                continue
            data[field][str(key)] = value
    ab_where = getattr(action_beat, "where", "") if action_beat is not None else ""
    ab_facing = getattr(action_beat, "facing", "") if action_beat is not None else ""
    cast_chars = ({c.id: c for c in cast.characters}
                  if cast is not None else {})
    for cid in characters:
        if not data["where"].get(cid):
            data["where"][cid] = ab_where if ab_where in DIRECTIONS else "center"
        if not data["facing"].get(cid):
            data["facing"][cid] = (ab_facing if ab_facing in DIRECTIONS
                                   else "toward_camera")
        if not data["posture"].get(cid):
            data["posture"][cid] = "standing"
        if not data["wardrobe"].get(cid):
            ch = cast_chars.get(cid)
            if ch is not None and ch.wardrobe_states:
                data["wardrobe"][cid] = ch.wardrobe_states[0].state_id
            elif scene.wardrobe.get(cid):
                data["wardrobe"][cid] = scene.wardrobe[cid]
    for pid in cp.props:
        data["props"].setdefault(pid, "scene")
    return CutState.model_validate(data)


_LENS_BY_SIZE = {
    "extreme-wide": "14mm wide lens",
    "wide": "24mm wide lens",
    "medium": "35mm lens",
    "close": "85mm lens",
    "extreme-close": "100mm macro lens",
}


def _derived_frame(beat: ScriptBeat, coverage: str, size: str,
                   scene: ScriptScene) -> str:
    text = _beat_summary(beat)
    if beat.visual_focus:
        text = f"{text}（画面重点：{beat.visual_focus}）"
    label = {
        "establish": "空间关系", "reaction": "反应", "insert": "细节",
        "pov": "主观视点", "dialogue": "对白", "transition": "转场",
    }.get(coverage, "动作")
    light = f"；{scene.lighting}" if scene.lighting else ""
    return f"{label}·{size}：{text}{light}"[:160]


def _narration_by_beat(scene: ScriptScene) -> dict[int, list[dict[str, str]]]:
    out: dict[int, list[dict[str, str]]] = {}
    for n in scene.narration:
        out.setdefault(n.beat, []).append({
            "kind": n.kind, "speaker": n.speaker,
            "text": n.text, "delivery": n.delivery,
        })
    return out


_TRANSITION_TO_REASON = {
    "match_cut": "match_cut",
    "sound_bridge": "sound",
    "action_bridge": "action_done",
    "time_jump": "time",
    "fade": "time",
    "dissolve": "time",
}


def _assemble_segment(plan: SegmentPlan, scene: ScriptScene,
                      action_line: ActionBeatLine, state_in: CutState,
                      prompts: list[Any] | None, params: ScriptParams,
                      *, seg_id: str = "E00-00",
                      scene_index: int = 1,
                      cast: Cast | None = None,
                      art: Art | None = None) -> StorySegment:
    """由 EDL + 视觉提示词确定性拼出完整 StorySegment。

    这里只读取模型给的单镜决策（景别/机位/人物/道具/切理由/状态），其余字段
    全部由剧本拍算出来：seconds、coverage、frame、subjects、purpose、
    information、beat_goal、state_from、机位元数据。
    """
    units = _normalize_plan_units(plan, scene)
    by_index = _action_beats_by_index(action_line)
    prev_state = state_in.model_copy(deep=True)
    prev_screen: dict[str, str] = {}
    cuts: list[StoryCut] = []
    sfx_all: list[str] = []
    for index, cp in units:
        if index < 1 or index > len(scene.beats):
            continue
        beat = scene.beats[index - 1]
        ab = by_index.get(index)
        coverage = _coverage_of_beat(beat)
        size, camera = cp.size, cp.camera
        cut_reason = cp.cut_reason
        if index == 1 and scene.transition_in is not None and scene_index > 1:
            mapped = _TRANSITION_TO_REASON.get(scene.transition_in.type)
            if mapped:
                cut_reason = mapped
        props = list(cp.props)
        # 情节权重确定性覆盖导演字段：关键道具必须特写、关键转折/高潮必须有运镜。
        # 模型即使全写 Static Shot/wide，也不会让关键环节变成“站桩念台词”。
        if beat.emphasis == "key_prop":
            if size not in {"close", "extreme-close"}:
                size = "close"
            if camera == "Static Shot":
                camera = "Push In"
            # 关键道具镜必须真的带上该道具的 P#，否则参考图/提示词都没有道具。
            if art is not None and beat.visual_focus:
                pid = _resolve_prop_id(beat.visual_focus, art.props)
                if pid and pid in scene.props and pid not in props:
                    props.append(pid)
        elif beat.emphasis in {"highlight", "climax"} and camera == "Static Shot":
            camera = "Push In"
        characters = _plan_characters(cp, scene, beat, ab, coverage)
        # 内心独白必须让观众看见说话的人：把该角色纳入本镜主体。
        for n in scene.narration:
            if (n.beat == index and n.kind == "inner"
                    and n.speaker not in characters):
                characters.append(n.speaker)
        state_out = _state_after(prev_state, cp, scene, beat, ab, characters,
                                 cast=cast)
        # 剧本拍级账本已授权的换装必须落实到分镜，不能沿用旧服装；
        # 后续镜头通过 prev_state 继承，模型再写回旧 state_id 也会被覆盖。
        for cid, wid in script_wardrobe_at(scene, index).items():
            if wid:
                state_out.wardrobe[cid] = wid
        side = (getattr(ab, "screen_side", "") if ab is not None else "") or ""
        screen_side: dict[str, str] = {}
        for cid in characters:
            screen_side[cid] = (side if side in {"left", "right", "center"}
                                else prev_screen.get(cid, "center"))
        prev_screen.update(screen_side)
        prompt = prompts[index - 1] if prompts and index - 1 < len(prompts) else None
        prompt_en = (getattr(prompt, "prompt_en", "") or "").strip() \
            or _placeholder_prompt(params, size)
        motion = (getattr(prompt, "motion", "") or "").strip()
        if not motion:
            motion = ("持续的自然动作" if params.prompt_language == "zh"
                      else "continuous natural action")
        base = _beat_summary(beat)
        subjects = [
            CutSubject(
                char=cid,
                wardrobe_state=state_out.wardrobe.get(cid, "")
                or scene.wardrobe.get(cid, ""),
                pose=state_out.posture.get(cid, ""),
                expression="",
            )
            for cid in characters
        ]
        cut = StoryCut(
            beats=(index, index),
            seconds=round(_beat_seconds(beat, params), 4),
            size=size,
            camera=camera,
            characters=characters,
            props=props,
            frame=_derived_frame(beat, coverage, size, scene),
            prompt_en=prompt_en,
            shot=base,
            motion=motion,
            lens=_LENS_BY_SIZE.get(size, ""),
            camera_position="eye-level",
            composition="",
            eyeline="",
            focus=("shallow depth of field"
                   if size in {"close", "extreme-close"} else ""),
            stability=("handheld" if "Shake" in (camera or "") else "stable"),
            sfx=cp.sfx,
            lighting=scene.lighting,
            subjects=subjects,
            coverage=coverage,
            cut_reason=cut_reason,
            purpose=f"{scene.id} {coverage} 拍：{base[:48]}",
            information=scene.information or scene.mood or "推进剧情",
            state_out=state_out,
            state_from=prev_state.model_copy(deep=True),
            screen_side=screen_side,
            beat_goal=f"{coverage}：{base[:48]}",
            emphasis=beat.emphasis,
            visual_focus=beat.visual_focus,
            allow_multi_face=cp.allow_multi_face,
            multi_subject_strategy=cp.multi_subject_strategy,
        )
        cuts.append(cut)
        if cp.sfx:
            sfx_all.append(cp.sfx)
        prev_state = state_out
    return StorySegment(
        id=seg_id, scene_index=scene_index, cuts=cuts,
        blocking=plan.blocking,
        soundscape=plan.soundscape or "；".join(sfx_all),
        music=plan.music or scene.mood,
        transition_in=scene.transition_in,
    )


def screen_axis_flip(beats: list) -> str | None:
    """P1-1 越轴（180°）确定性检查：同主体相邻拍 screen_side 左↔右翻转需有横移过渡。

    beats 为 ActionBeat 列表；返回越轴描述（有错）或 None。
    合法换位：上一拍 where 在 west/east 且本拍 where 改变（角色真的横移了）。
    """
    prev: dict[str, tuple[str, str]] = {}
    for b in beats:
        who = (b.who or "").strip()
        side = (b.screen_side or "").strip()
        if not who or side not in {"left", "right"}:
            continue
        if who in prev:
            p_side, p_where = prev[who]
            if p_side != side:
                moved = p_where in {"west", "east"} and (b.where or "") != p_where
                if not moved:
                    return (f"{who} 屏幕侧由 {p_side} 翻到 {side} 且无横移过渡，"
                            "180° 越轴（下一级不得直接反打）")
        prev[who] = (side, b.where or "")
    return None
