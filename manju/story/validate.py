"""故事层确定性质量门。

所有门都是纯函数：不调模型、不碰 GPU。LLM 生成后先过这里，失败把错误原文回喂。
"""
from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from typing import Any

from .continuity import validate_board_continuity
from .editorial import count_action_verbs
from .models import (
    Art, Cast, EpisodePlan, Outline, ScriptEpisode, ScriptParams, ScriptScene,
    Staging, StoryBible, StoryEpisodeBoard, StorySegment, StoryStoryboard,
    _beat_seconds, _line_chars, _scene_seconds, face_lock_required,
)


def _face_lock_chars(cast: Cast, ids) -> set[str]:
    """只统计需要锁脸的角色：宠物/稻草人/面具角色不占"正脸"名额。"""
    by_id = {c.id: c for c in cast.characters}
    return {cid for cid in set(ids)
            if cid in by_id and face_lock_required(by_id[cid])}

SHOT_BEATS = {"钩子", "激励", "抉择", "受挫", "转折", "代价", "高潮", "收束", "结尾钩子"}

# 场间转场：bridge 类必须有共享锚点；fade/dissolve 允许无锚点。
TRANSITION_TYPES = {"match_cut", "sound_bridge", "action_bridge",
                    "time_jump", "fade", "dissolve"}
BRIDGE_TRANSITIONS = {"match_cut", "sound_bridge", "action_bridge"}
TRANSITION_CUT_REASON = {
    "match_cut": {"match_cut"},
    "sound_bridge": {"sound", "match_cut"},
    "action_bridge": {"action_done", "match_cut", "space"},
    "time_jump": {"time", "match_cut"},
    "fade": {"time", "space", "new_info"},
    "dissolve": {"time", "space", "new_info"},
}
NARRATION_CPS = 5.0


def coverage_required(target: float, dialogue_light: bool = False) -> dict[str, int]:
    """覆盖率硬配额（剧本层/分镜层/提示词共用同一口径）。"""
    scale = max(target / 90.0, 1e-6)
    reaction, insert = (9, 7) if dialogue_light else (8, 6)
    return {
        "reaction": max(1, round(reaction * scale)),
        "insert": max(1, round(insert * scale)),
        "establish": max(1, round(3 * scale)),
        "pov": max(1, round(1 * scale)),
    }

# P0：情绪曲线合法标签白名单
EMOTION_TAGS = {"紧张", "悬念", "转折", "爆发", "温情", "低落", "舒缓", "高潮", "对峙", "抉择"}
HEX_RE = re.compile(r"^#[0-9a-fA-F]{6}$")


def _validate_visual_bible(vb: dict) -> list[str]:
    """visual_bible 契约门：结构化四键（style_keywords/main_colors/do_dont/reference_styles）。"""
    errs: list[str] = []
    if not isinstance(vb, dict):
        return ["visual_bible 必须是对象（style_keywords/main_colors/do_dont/reference_styles）"]
    keys = vb
    sk = keys.get("style_keywords")
    if not isinstance(sk, list) or not sk:
        errs.append("visual_bible.style_keywords 必须是非空字符串数组（画风关键词，至少一个）")
    colors = keys.get("main_colors")
    if not isinstance(colors, list):
        errs.append("visual_bible.main_colors 必须是 5-7 个 hex 色值的字符串数组")
    elif not 5 <= len(colors) <= 7:
        errs.append(f"visual_bible.main_colors 应为 5-7 个主色，当前 {len(colors)} 个")
    else:
        bad = [c for c in colors if not isinstance(c, str) or not HEX_RE.match(c)]
        if bad:
            errs.append(f"visual_bible.main_colors 含非 hex 色值：{bad}")
    dd = keys.get("do_dont")
    if isinstance(dd, dict):
        # 兼容 {DO:[...], DON'T:[...]} 分组对象：摊平成规则条目再校验
        dd = [x for grp in dd.values() for x in (grp if isinstance(grp, list) else [grp])]
    if not isinstance(dd, list):
        errs.append("visual_bible.do_dont 必须是字符串数组（每条一条 DO/DON'T 规则）"
                    "或 {DO:[...], DON'T:[...]} 对象")
    elif not dd:
        errs.append("visual_bible 缺 do_dont（DO/DON'T 规则）")
    else:
        bad_dd = [x for x in dd if not isinstance(x, str) or not x.strip()]
        if bad_dd:
            errs.append(f"visual_bible.do_dont 含空/非字符串条目：{bad_dd}")
    return errs



STYLE_WORDS = ("cinematic", "film still", "photorealistic", "masterpiece",
               "best quality", "anime", "manga", "teal-orange", "film grain",
               "8k", "4k")
CAMERA_WORDS = ("push in", "push-in", "pull out", "pan left", "pan right",
                "tilt up", "tilt down", "dolly", "tracking shot", "camera move")

QUOTES = ("「", "」", "『", "』", "“", "”", "\"")
ACTION_WORDS = ("坐下", "站起", "转身", "抬手", "走", "跑", "冲", "抓", "推", "拉",
                "劈", "挡", "看", "抬头", "低头", "握", "放", "拿", "扑", "退")


@dataclass
class GateReport:
    layer: str
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.errors

    def add(self, msg: str) -> None:
        self.errors.append(msg)

    def warn(self, msg: str) -> None:
        self.warnings.append(msg)

    def text(self) -> str:
        lines = [f"[{self.layer}] {'通过' if self.ok else '未通过'} "
                 f"({len(self.errors)} 个错误 / {len(self.warnings)} 个警告)"]
        lines += [f"  ✗ {e}" for e in self.errors]
        lines += [f"  ! {w}" for w in self.warnings]
        return "\n".join(lines)

    def to_dict(self) -> dict[str, Any]:
        return {"layer": self.layer, "ok": self.ok,
                "errors": self.errors, "warnings": self.warnings}


def _has_quote(text: str) -> bool:
    return any(q in (text or "") for q in QUOTES)


def _soft(r: GateReport, strict: bool, msg: str) -> None:
    """软门：默认只记 warning；--strict 时恢复为错误。"""
    if strict:
        r.add(msg)
    else:
        r.warn(msg)


def _emotion_check(ep: EpisodePlan, r: GateReport, strict: bool = True) -> None:
    """情绪曲线白名单门：非空时每项都必须在合法标签表里。

    标签是叙事口味，不是渲染契约；默认只提示，``strict`` 才升级。
    """
    for t in ep.emotion_curve:
        if t not in EMOTION_TAGS:
            _soft(r, strict,
                  f"第 {ep.ep} 集情绪曲线标签 {t!r} 不在白名单 {sorted(EMOTION_TAGS)}")


def _delivery_conflict(scenes: list[ScriptScene]) -> list[str]:
    """台词风格一致性门：同一角色 delivery 情绪极性冲突（无转折场却喜怒两极）。"""
    POS = {"喜悦", "温柔", "微笑", "深情", "轻松", "得意", "欣慰", "柔和", "轻声"}
    NEG = {"愤怒", "暴怒", "吼", "哭", "哽咽", "恨", "威胁", "冷笑", "憎恶", "痛", "嘶吼"}
    by_speaker: dict[str, list[tuple[str, str]]] = {}
    for sc in scenes:
        for b in sc.beats:
            if b.line and b.speaker:
                by_speaker.setdefault(b.speaker, []).append((b.delivery, sc.beat))
    out: list[str] = []
    for sp, dels in by_speaker.items():
        if len(dels) < 2:
            continue
        pos = [d for d, _ in dels if any(w in d for w in POS)]
        neg = [d for d, _ in dels if any(w in d for w in NEG)]
        if pos and neg:
            # 只在对峙/转折/高潮场可容忍情绪剧变
            any_pivot = any(beat in {"转折", "高潮", "抉择", "对峙", "爆发"} for _, beat in dels)
            if not any_pivot:
                out.append(f"{sp} 台词 delivery 同时出现正/负极性情绪且无转折场"
                          f"（{pos[0]!r} vs {neg[0]!r}），风格不一致")
    return out


# P0-C4 动作拍可拍性：命中即视为"内心句"，摄像机拍不到。
# 刻意排除单独"想"（"她想了想"可为可见停顿），只收强内心动词，降低真实剧本误报。
PSYCH_VERBS = ("觉得", "以为", "认为", "意识到", "感到", "害怕", "担心",
               "渴望", "幻想", "盘算", "暗自", "琢磨", "心想", "内心", "暗自揣度")


def _psych_verb(text: str) -> str:
    """返回首个命中的心理动词；无则空串。"""
    for w in PSYCH_VERBS:
        if w in (text or ""):
            return w
    return ""


def _same_location_merge(a: ScriptScene, b: ScriptScene) -> bool:
    """P0-C6 同地点连续场合并判据：相邻场同 location+time+lighting 且人物重叠。

    合法连续同地（如追逃、同一对话连续发生）若人物重叠会被误判，
    故同时要求 time 与 lighting 也一致才建议合并，降低误报。
    """
    same = (a.location == b.location and a.time == b.time
            and a.lighting == b.lighting)
    overlap = set(a.characters) & set(b.characters)
    return bool(same and overlap)


# --------------------------------------------------------------------------
# P1-S1 电影化动作句规范：动作句长上限 / 被动句 / 心理词黑名单（扩展 P0-C4）
# --------------------------------------------------------------------------
PSYCH_VERBS = ("觉得", "以为", "认为", "意识到", "感到", "害怕", "担心",
               "渴望", "幻想", "盘算", "暗自", "琢磨", "心想", "内心", "暗自揣度",
               # P1-S1 扩展：强内心活动，摄像机拍不到
               "回忆起", "想起", "明白", "懂了", "发觉", "思绪", "羞赧", "惶恐")

ACTION_MAX_LEN = 45          # 单个动作拍叙述句上限（汉字数），超过=一段塞多动作
PASSIVE_MARKERS = ("被他", "被她", "被对方", "被人", "被风", "被雨", "被门",
                   "为他所", "为她所", "受到", "遭到")


def _psych_verb(text: str) -> str:
    """返回首个命中的心理动词；无则空串。"""
    for w in PSYCH_VERBS:
        if w in (text or ""):
            return w
    return ""


def _passive_voice(text: str) -> str:
    """P1-S1 返回首个命中的被动句标记；可拍动作应主动语态（谁对谁做）。"""
    for w in PASSIVE_MARKERS:
        if w in (text or ""):
            return w
    return ""


# --------------------------------------------------------------------------
# P1-S5 台词文白夹杂：同一角色台词 register 不一致
# --------------------------------------------------------------------------
FORMAL_MARKS = ("阁下", "此乃", "未曾", "甚是", "倘若", "殊不知", "寡人",
                "本宫", "爱卿", "此番", "务必", "足下")
COLLOQ_MARKS = ("咋", "啥", "别", "滚", "喂", "咱", "甭", "啥呀", "别闹")


def _register_mix(scenes: list[ScriptScene]) -> list[str]:
    """同一角色既有书面文言腔又有口语俚语腔 → 风格跳变告警。"""
    by_sp: dict[str, list[str]] = {}
    for sc in scenes:
        for b in sc.beats:
            if b.line and b.speaker:
                by_sp.setdefault(b.speaker, []).append(b.line)
    out: list[str] = []
    for sp, lines in by_sp.items():
        text = "".join(lines)
        formal = [w for w in FORMAL_MARKS if w in text]
        colloq = [w for w in COLLOQ_MARKS if w in text]
        if formal and colloq:
            out.append(f"{sp} 台词文白夹杂：书面词 {formal} 与口语词 {colloq} 同框，"
                       "口癖/语气 profile 不统一")
    return out


# --------------------------------------------------------------------------
# P1-S6 动作拍涉及角色必须在场（联合 staging space_map 可执行性）
# --------------------------------------------------------------------------
def _beat_characters_on_scene(sc: ScriptScene) -> list[str]:
    """动作拍叙述里点名的 C id 必须在本场人物表（否则=角色不在场却做动作）。"""
    out: list[str] = []
    present = set(sc.characters)
    for bi, b in enumerate(sc.beats, 1):
        if not b.action:
            continue
        for cid in set(re.findall(r"\bC\d+\b", b.action)):
            if cid not in present:
                out.append(f"{sc.id} 第 {bi} 拍动作点名 {cid}，但 {cid} 不在本场人物表"
                           "（角色不在场却做动作，staging space_map 无可站位）")
    return out


_WARDROBE_TOKEN = re.compile(r"C\d+-[A-Za-z0-9_-]+")


def _beat_state_chain_check(sc: ScriptScene, r: GateReport, strict: bool) -> None:
    """P1-2 拍级状态账本：逐拍 state_out 衔接校验。

    跑表口径：场级 wardrobe 快照为起点；拍内 state_change 授权换装；
    拍末 state_out 里的服装态 token 与跑表不一致且本拍无 state_change 授权 → 拦。
    """
    running: dict[str, str] = dict(sc.wardrobe)
    for bi, b in enumerate(sc.beats, 1):
        authorized = {c for c, v in (b.state_change or {}).items()
                      if any(w in v for w in ("换", "脱", "穿", "披", "戴"))}
        for cid, snap in (b.state_out or {}).items():
            m = _WARDROBE_TOKEN.search(snap or "")
            if not m:
                continue
            tok = m.group(0)
            prev = running.get(cid)
            if prev and prev != tok and cid not in authorized:
                _soft(r, strict,
                      f"{sc.id} 第 {bi} 拍 state_out[{cid}]={tok!r}，"
                      f"但上一拍末/场级为 {prev!r} 且本拍无 state_change 授权换装，"
                      "拍级状态账本断裂（S6 式无据换鞋）")
            running[cid] = tok
        # state_change 里登记的换装同步进跑表（无目标态时记 changed 占位）
        for cid, v in (b.state_change or {}).items():
            if any(w in v for w in ("换", "脱", "穿", "披", "戴")):
                running[cid] = running.get(cid) or f"{cid}-changed"


def _global_beats(ep: ScriptEpisode) -> list[tuple[int, int]]:
    out: list[tuple[int, int]] = []
    for si, scene in enumerate(ep.scenes, 1):
        for bi in range(1, len(scene.beats) + 1):
            out.append((si, bi))
    return out


# ==========================================================================
# 大纲（全局圣经 / 分集梗概 / 完整大纲）
# ==========================================================================
def validate_bible(bible: StoryBible,
                   episodes: list[EpisodePlan] | None = None,
                   *, strict: bool = False) -> GateReport:
    """全局圣经的质量门；给了 episodes 时顺带查“失业角色/空转场景”。

    strict=False 时，爽点间隔只记 warning（门禁分级）。
    """
    r = GateReport("bible")
    chars = {c.id: c for c in bible.characters}
    scenes = {s.id: s for s in bible.scenes}
    props = {p.id: p for p in bible.props}

    # P0：世界设定结构化门（world 存在时必须四块非空）
    if bible.world is not None:
        for block, items in bible.world.blocks().items():
            if not items:
                _soft(r, strict, f"世界设定缺 {block} 块（势力/地理/时间线/规则应齐全）")
    # P0：visual_bible 契约门（存在时必须过结构化校验）
    if bible.visual_bible is not None:
        for e in _validate_visual_bible(bible.visual_bible):
            _soft(r, strict, f"visual_bible：{e}")

    if not bible.adaptation.core.strip():
        _soft(r, strict, "adaptation.core 不能为空")
    if not bible.adaptation.keep:
        _soft(r, strict, "adaptation.keep 至少保留一条")
    if bible.adaptation.cut and not any(x.what.strip() for x in bible.adaptation.cut):
        _soft(r, strict, "adaptation.cut 里有空条目")

    leads = [c for c in bible.characters if c.tier == "lead"]
    support = [c for c in bible.characters if c.tier == "support"]
    functional = [c for c in bible.characters if c.tier == "functional"]
    if not 1 <= len(leads) <= 5:
        _soft(r, strict, f"lead 角色必须 1-5 人，当前 {len(leads)}")
    if len(support) > 10:
        _soft(r, strict, f"support 角色上限 10，当前 {len(support)}")
    if len(functional) > 10:
        _soft(r, strict, f"functional 角色上限 10，当前 {len(functional)}")

    # 爽点分布
    count = bible.params.episodes
    by_ep: dict[int, list] = {}
    for b in bible.beats:
        if not 1 <= b.episode <= count:
            r.add(f"爽点 {b.id} 指向不存在的集 {b.episode}")
        by_ep.setdefault(b.episode, []).append(b)
    if count > 1:
        gap = 0
        for ep in range(1, count + 1):
            if by_ep.get(ep):
                gap = 0
            else:
                gap += 1
                if gap > 3:
                    _soft(r, strict, f"第 {ep} 集前连续 3 集没有爽点")
                    break
    majors = [b for b in bible.beats if b.weight == "major"]
    if not majors:
        _soft(r, strict, "至少需要一个 major 爽点")
    elif count > 1 and min(b.episode for b in majors) == count:
        _soft(r, strict, "最早的 major 爽点不能落在最后一集")

    # 主场景上限（随集数动态，与 shuohao 同口径）
    import math
    max_primary = min(15, max(5, 4 + math.ceil(count / 10)))
    n_primary = sum(1 for s in bible.scenes if s.primary)
    if n_primary > max_primary:
        _soft(r, strict, f"主场景 {n_primary} 个，超过上限 {max_primary}")
    if len(bible.props) > 8:
        _soft(r, strict, f"叙事道具 {len(bible.props)} 件，超过上限 8")

    if episodes is None:
        return r

    used_chars = {c for e in episodes for c in e.character_ids}
    used_scenes = {s for e in episodes for s in e.scene_ids}
    used_props = {p for e in episodes for p in e.prop_ids}
    for cid in chars:
        if chars[cid].tier == "functional":
            continue  # 功能性角色允许"仅被提及、不出场"，不判失业
        if cid not in used_chars:
            # 角色未被采用是改编取舍，不是产物契约；默认提示，strict 才拦。
            _soft(r, strict, f"角色 {cid} 没有被任何一集使用（失业角色）")
    for sid in scenes:
        if sid not in used_scenes:
            if scenes[sid].primary:
                _soft(r, strict, f"主场景 {sid} 没有被任何一集使用（空转场景）")
            else:
                _soft(r, strict, f"非主场景 {sid} 没有被任何一集使用（空转场景，备用设定可忽略）")
    for pid in props:
        if pid not in used_props:
            _soft(r, strict, f"道具 {pid} 没有被任何一集使用")
    for e in episodes:
        if not e.hook.strip() or not e.cliff.strip():
            r.add(f"第 {e.ep} 集 hook/cliff 不能为空")
        if _has_quote(e.synopsis):
            _soft(r, strict,
                  f"第 {e.ep} 集 synopsis 必须是叙述体，不能出现引号台词")
        if len(e.character_ids) >= 3 and not e.crowd_plan.strip():
            _soft(r, strict, f"第 {e.ep} 集有 3 人以上出场，必须写 crowd_plan")
        for sid in e.scene_ids:
            if sid not in scenes:
                r.add(f"第 {e.ep} 集引用不存在的场景 {sid}")
        for cid in e.character_ids:
            if cid not in chars:
                r.add(f"第 {e.ep} 集引用不存在的角色 {cid}")
        for pid in e.prop_ids:
            if pid not in props:
                r.add(f"第 {e.ep} 集引用不存在的道具 {pid}")
        _emotion_check(e, r, strict)
    return r


def validate_episode_batch(bible: StoryBible, episodes: list[EpisodePlan],
                           expected: list[int], *, strict: bool = False
                           ) -> GateReport:
    """单批分集梗概：集号必须精确覆盖本批，引用必须落在圣经里。"""
    r = GateReport("episodes")
    eps = [e.ep for e in episodes]
    if sorted(eps) != sorted(expected):
        r.add(f"分集编号 {sorted(eps)} 与本批要求 {sorted(expected)} 不一致")
    chars = {c.id for c in bible.characters}
    scenes = {s.id for s in bible.scenes}
    props = {p.id for p in bible.props}
    for e in episodes:
        if not 1 <= e.ep <= bible.params.episodes:
            r.add(f"第 {e.ep} 集超出 params.episodes={bible.params.episodes}")
        if not e.hook.strip() or not e.cliff.strip():
            r.add(f"第 {e.ep} 集 hook/cliff 不能为空")
        if _has_quote(e.synopsis):
            _soft(r, strict,
                  f"第 {e.ep} 集 synopsis 必须是叙述体，不能出现引号台词")
        if not e.scene_ids:
            r.add(f"第 {e.ep} 集没有 scene_ids")
        if e.must_show:
            # 2–6 条是改编建议；1 条或 7 条仍能拍，只有重复是可确定判定的坏数据。
            if not 2 <= len(e.must_show) <= 6:
                _soft(r, strict,
                      f"第 {e.ep} 集 must_show 必须 2-6 条，当前 {len(e.must_show)}")
            if len({m.strip() for m in e.must_show}) != len(e.must_show):
                r.add(f"第 {e.ep} 集 must_show 有重复事件")
            for m in e.must_show:
                if not 4 <= len(m.strip()) <= 80:
                    _soft(r, strict,
                          f"第 {e.ep} 集 must_show 条目应为 4-80 字可拍事件：{m!r}")
        if len(e.key_props) > 3:
            _soft(r, strict, f"第 {e.ep} 集 key_props 最多 3 件")
        if len(e.character_ids) >= 3 and not e.crowd_plan.strip():
            _soft(r, strict,
                  f"第 {e.ep} 集有 3 人以上出场，必须写 crowd_plan")
        for sid in e.scene_ids:
            if sid not in scenes:
                r.add(f"第 {e.ep} 集引用不存在的场景 {sid}")
        for cid in e.character_ids:
            if cid not in chars:
                r.add(f"第 {e.ep} 集引用不存在的角色 {cid}")
        for pid in e.prop_ids:
            if pid not in props:
                r.add(f"第 {e.ep} 集引用不存在的道具 {pid}")
        _emotion_check(e, r, strict)
    return r


def validate_outline(outline: Outline, *, strict: bool = False) -> GateReport:
    r = validate_bible(outline, outline.episodes, strict=strict)
    r.layer = "outline"
    return r


# ==========================================================================
# 角色
# ==========================================================================
def validate_cast(cast: Cast, outline: Outline, *, strict: bool = False
                  ) -> GateReport:
    r = GateReport("cast")
    by_id = {c.id: c for c in cast.characters}
    # P0：别名全局去重门（同一别名不能归属两个角色）
    alias_map: dict[str, str] = {}
    for c in cast.characters:
        for a in c.aliases:
            if a in alias_map:
                r.add(f"别名 {a!r} 同时归属 {alias_map[a]} 与 {c.id}")
            else:
                alias_map[a] = c.id
    for c in outline.characters:
        if c.id not in by_id:
            r.add(f"大纲角色 {c.id} 在 cast 里缺失")
    for c in cast.characters:
        if not c.wardrobe_states:
            r.add(f"{c.id} 至少需要一个 wardrobe_state")
        ids = [w.state_id for w in c.wardrobe_states]
        if len(ids) != len(set(ids)):
            r.add(f"{c.id} 的 wardrobe_state id 重复")
        if not c.image.prompt.strip():
            r.add(f"{c.id} 缺少 image.prompt")
        low = c.image.prompt.lower()
        if c.name and c.name.lower() in low:
            _soft(r, strict,
                  f"{c.id} 的 image.prompt 含角色名 {c.name!r}（应为通用描述）")
        if not c.identity_lock.hair.strip() or not c.identity_lock.face_shape.strip():
            r.warn(f"{c.id} 的 identity_lock 信息不全（建议补 hair/face_shape）")
        # P0：Veo 式不可变外貌块门
        if c.importance in ("protagonist", "major") and not c.appearance_block.strip():
            _soft(r, strict,
                  f"{c.id} 是主要角色，必须给 appearance_block（Veo 式不可变外貌块）")
        # P0：关系契约门
        for rel in c.persona.relationship_spec:
            if not rel.type.strip() or not rel.target.strip():
                _soft(r, strict,
                      f"{c.id} 的 relationship_spec 有条目缺 type/target")
        if c.importance in ("protagonist", "major") and not c.voice.prompt.strip() and not c.voice.timbre.strip():
            _soft(r, strict, f"{c.id} 是主要角色，必须给 voice 音色信息")
    return r


# ==========================================================================
# 美术
# ==========================================================================
def validate_art(art: Art, outline: Outline, cast: Cast | None = None,
                 *, strict: bool = False) -> GateReport:
    r = GateReport("art")
    scenes = {s.id: s for s in art.scenes}
    for s in outline.scenes:
        if s.id not in scenes:
            r.add(f"大纲场景 {s.id} 在 art 里缺失")
    for s in art.scenes:
        # P0：剧本工业字段门（INT/EXT/昼夜），主场景必填
        if not s.location_type and s.primary:
            _soft(r, strict,
                  f"{s.id} 是主场景，必须给 location_type（INT/EXT）")
        if not s.time_of_day and s.primary:
            _soft(r, strict,
                  f"{s.id} 是主场景，必须给 time_of_day（DAY/NIGHT/DAWN/DUSK）")
        if not 3 <= len(s.anchors) <= 5:
            _soft(r, strict,
                  f"{s.id} 的 anchors 必须 3-5 个，当前 {len(s.anchors)}")
        if not s.lighting:
            r.add(f"{s.id} 至少需要一个光照状态")
        for light in s.lighting:
            if not light.prompt.strip():
                r.add(f"{s.id}/{light.state} 缺少光照提示词"
                      "（语言按 story.prompt_language）")
        if not s.image.prompt.strip():
            r.add(f"{s.id} 缺少 image.prompt")
        negative = (s.image.negative_prompt or "").lower()
        prompt = (s.image.prompt or "").lower()
        if not any(w in negative for w in ("people", "person", "figure", "human")):
            r.warn(f"{s.id} 的 negative_prompt 建议显式禁人（people/person/figure）")
        if "no people" not in prompt and "empty" not in prompt:
            r.warn(f"{s.id} 的 image.prompt 建议写明空景无人")
        if s.variant_of and not s.changes.strip():
            r.add(f"{s.id} 是变体，必须写 changes")
        if s.variant_of and s.variant_of not in scenes:
            r.add(f"{s.id} 的 variant_of 指向不存在的场景 {s.variant_of}")
    for p in art.props:
        # P0：Prop/SetDressing 分类门——陈设不应绑定手持角色
        if p.category == "SetDressing" and p.carried_by.strip():
            r.warn(f"{p.id} 是 SetDressing（陈设），不应有 carried_by={p.carried_by!r}")
        if not 3 <= len(p.anchors) <= 5:
            _soft(r, strict, f"{p.id} 的 anchors 必须 3-5 个")
        if not p.states:
            r.add(f"{p.id} 至少需要一个状态")
        if not p.image.prompt.strip():
            r.add(f"{p.id} 缺少 image.prompt")
        low = p.image.prompt.lower()
        need = {"手持级": "handheld", "桌面级": "tabletop", "家具级": "furniture"}[p.scale]
        if need not in low:
            r.warn(f"{p.id} 的 image.prompt 缺少尺度短语 {need!r}")
        if not any(w in (p.image.negative_prompt or "").lower() for w in ("hand", "finger")):
            r.warn(f"{p.id} 的 negative_prompt 建议禁手")
    if cast:
        names = {c.name for c in cast.characters if c.name}
        for s in art.scenes:
            for n in names:
                if n and n.lower() in (s.image.prompt or "").lower():
                    _soft(r, strict,
                          f"{s.id} 的 image.prompt 含角色名 {n!r}（场景图必须无人）")
    return r


# ==========================================================================
# 剧本
# ==========================================================================
def _event_key(text: str) -> str:
    return re.sub(r"[\W_]+", "", (text or "").lower())


def event_match(a: str, b: str) -> bool:
    """两个事件描述是否指向同一件事（归一化后相等/互相包含）。"""
    ka, kb = _event_key(a), _event_key(b)
    if not ka or not kb:
        return False
    if ka == kb:
        return True
    return ((len(ka) >= 4 and ka in kb)
            or (len(kb) >= 4 and kb in ka))


def event_score(text: str, event: str) -> float:
    """粗粒度匹配分数：事件里有几个二元组出现在场次文本里。"""
    key = _event_key(event)
    hay = _event_key(text)
    if not key or not hay:
        return 0.0
    if key in hay:
        return 1.0
    grams = {key[i:i + 2] for i in range(max(1, len(key) - 1))}
    if not grams:
        return 0.0
    return sum(1 for g in grams if g in hay) / len(grams)


def _scene_text(sc: ScriptScene) -> str:
    parts = [sc.location, sc.goal, sc.obstacle, sc.turn, sc.cost,
             sc.information, sc.mood, sc.major_event]
    for b in sc.beats:
        parts.extend([b.action or "", b.line or "", b.visual_focus or ""])
    return " ".join(x for x in parts if x)


def scene_has_prop(sc: ScriptScene, prop: str, art: Art) -> bool:
    key = _event_key(prop)
    for pid in sc.props:
        art_prop = next((x for x in art.props if x.id == pid), None)
        name = (art_prop.name if art_prop is not None else "")
        if _event_key(pid) == key or (name and _event_key(name) == key):
            return True
    return bool(key and key in _event_key(_scene_text(sc)))


def beat_has_prop(beat, prop: str) -> bool:
    key = _event_key(prop)
    if not key:
        return False
    return key in _event_key(" ".join([
        beat.visual_focus or "", beat.action or "", beat.line or ""]))


def _narration_errors(sc: ScriptScene, si: int, chars: dict,
                      r: GateReport) -> None:
    """场级旁白检查：叠在动作拍、限条数、说话人合法、不复述画面。"""
    seen: set[int] = set()
    for ni, n in enumerate(sc.narration, 1):
        tag = f"{sc.id} 旁白 {ni}"
        if not n.text.strip():
            r.add(f"{tag}：text 不能为空")
            continue
        if n.beat < 1 or n.beat > len(sc.beats):
            r.add(f"{tag}：beat={n.beat} 越界（本场 {len(sc.beats)} 拍）")
            continue
        if n.beat in seen:
            r.warn(f"{tag}：同一拍叠了多条旁白，口播会互相盖住")
        seen.add(n.beat)
        beat = sc.beats[n.beat - 1]
        if beat.line:
            r.add(f"{tag}：不能叠在台词拍上（旁白与对白会抢声道），"
                  "请改叠到相邻动作/反应拍")
        if n.kind == "inner":
            if n.speaker not in sc.characters:
                r.add(f"{tag}：内心独白 speaker {n.speaker} 不在本场人物里")
            elif n.speaker not in chars:
                r.add(f"{tag}：内心独白 speaker {n.speaker} 不是已存在的角色")
        elif n.kind == "exposition" and n.speaker != "NARRATOR":
            r.add(f"{tag}：解释性旁白 speaker 必须是 NARRATOR")
        action_key = re.sub(r"\s+", "", beat.action or "")
        text_key = re.sub(r"\s+", "", n.text)
        if len(text_key) >= 6 and text_key in action_key:
            r.warn(f"{tag}：旁白只是在复述可见动作「{beat.action[:24]}」，"
                   "应补画面外的前情/规则/内心，而非看图说话")


def _transition_errors(sc: ScriptScene, si: int, p: ScriptParams,
                       r: GateReport, *, enforce: bool) -> None:
    """场间转场检查：第 2 场起必须能说出怎么接过来的。"""
    tr = sc.transition_in
    if tr is None:
        if si == 1:
            return
        _soft(r, enforce or p.transition_mode == "on",
              f"{sc.id} 缺 transition_in：第 2 场起要说明如何从上一场接过来，"
              "避免无动机硬切")
        return
    if tr.type not in TRANSITION_TYPES:
        r.add(f"{sc.id} transition_in.type={tr.type!r} 不在 {sorted(TRANSITION_TYPES)}")
        return
    if tr.type in BRIDGE_TRANSITIONS and not tr.anchor.strip():
        _soft(r, enforce or p.transition_mode == "on",
              f"{sc.id} {tr.type} 缺 anchor：必须写共享的物件/动作/声音锚点")
    if tr.type in {"time_jump", "fade", "dissolve"} and not tr.anchor.strip():
        r.warn(f"{sc.id} {tr.type} 未写 anchor（建议补时间/空间落点）")
    if si == 1 and p.transition_mode == "off":
        r.warn(f"{sc.id} 第 1 场仍写了 transition_in（可留空，除非承接上集）")


def validate_script(ep: ScriptEpisode, outline: Outline, cast: Cast,
                    art: Art, params: ScriptParams | None = None,
                    *, strict: bool = False, dialogue_light: bool = False,
                    enforce_transitions: bool = False) -> GateReport:
    """剧本门。hard=结构/引用/时长/台词规则；soft=场数/句数/抉择/受挫。"""
    r = GateReport(f"script.ep{ep.ep}")
    p = params or ScriptParams()
    if not 1 <= len(ep.scenes) <= 4:
        _soft(r, strict, f"本集 {len(ep.scenes)} 个戏剧单元，建议 1-4 个"
                         "（每个单元必须讲完整清楚；更多单元会每单元只剩一个跳切）")
    chars = {c.id: c for c in cast.characters}
    scenes = {s.id: s for s in art.scenes}
    outline_scenes = {s.id for s in outline.scenes}
    outline_beats = {b.id for b in outline.beats}
    plan = next((e for e in outline.episodes if e.ep == ep.ep), None)
    used_scenes = set()

    total = ep.duration_with(p)
    target = ep.target_seconds
    if abs(total - target) > target * p.tolerance:
        r.add(f"本集估算 {total:.1f}s，目标 {target:.1f}s，超出 ±{p.tolerance:.0%}")

    # P0-S7 场景 id 必须唯一：LLM 可能给两场戏贴同名 id（S3 出现两次），
    # 会让 EDL 按剧本实际场数迭代时场错位、人物校验撞车。
    ids = [s.id for s in ep.scenes]
    dup = sorted({i for i in ids if ids.count(i) > 1})
    if dup:
        r.add(f"场景 id 重复：{dup}（两场不同戏贴了同一个 id，请改为唯一编号）")

    narration = [n for sc in ep.scenes for n in sc.narration]
    n_exp = sum(1 for n in narration if n.kind == "exposition")
    n_inner = sum(1 for n in narration if n.kind == "inner")
    if p.narration_mode == "off" and narration:
        r.add("narration_mode=off，但剧本写入了旁白；请删掉 scene.narration")
    if n_exp > p.narration_max_exposition:
        r.add(f"本集解释性旁白 {n_exp} 条，超过上限 "
              f"{p.narration_max_exposition}（否则会变成有声书）")
    if n_inner > p.narration_max_inner:
        r.add(f"本集内心独白 {n_inner} 条，超过上限 "
              f"{p.narration_max_inner}（只保留关键抉择/情绪转折）")
    if p.narration_mode == "on" and not narration:
        r.add("narration_mode=on，但本集没有任何 scene.narration")
    if dialogue_light and p.narration_mode == "auto" and not narration:
        _soft(r, strict, "源文本对白稀薄：建议至少 1 条解释性旁白或内心独白，"
                          "补画面无法交代的前因/规则/内心；仍以可见动作为主")
    narration_secs = sum(_line_chars(n.text) for n in narration) / NARRATION_CPS
    max_narration_secs = ep.target_seconds * (0.30 if dialogue_light else 0.18)
    if narration_secs > max_narration_secs + 1e-6:
        _soft(r, strict,
              f"旁白合计约 {narration_secs:.1f}s，超过 {max_narration_secs:.1f}s"
              "（画面叙事为主，旁白只补关键信息）")

    starts: list[float] = []
    clock = 0.0
    prev_sc: ScriptScene | None = None
    for si, sc in enumerate(ep.scenes, 1):
        starts.append(clock)
        clock += _scene_seconds(sc, p)
        used_scenes.add(sc.id)
        # P0-C6 同地点连续场合并校验（治 S6/S7 式假场次）
        if prev_sc is not None and _same_location_merge(prev_sc, sc):
            r.warn(f"{prev_sc.id}/{sc.id} 同地点连续场（{sc.location} {sc.time} {sc.lighting}）"
                   "人物重叠，疑似假场次，建议合并或改用转场区分")
        prev_sc = sc
        if sc.beat not in SHOT_BEATS:
            r.add(f"{sc.id} 的 beat={sc.beat!r} 不在戏剧节拍表里 {sorted(SHOT_BEATS)}")
        if sc.id not in scenes:
            r.add(f"{sc.id} 不在 art 里")
        if sc.id not in outline_scenes:
            r.add(f"{sc.id} 不在大纲里")
        for k, label in ((sc.goal, "目标"), (sc.obstacle, "阻力"),
                         (sc.turn, "转折"), (sc.cost, "代价"), (sc.information, "信息")):
            if not k.strip():
                _soft(r, strict, f"{sc.id} 缺少戏剧字段 {label}")
        if not any(b.action for b in sc.beats):
            r.add(f"{sc.id} 没有动作节拍（纯对白无法出画面）")
        # P0: 剧情逻辑连续性门 —— 台词回指的对象必须真实存在于前文
        for cb in sc.callbacks:
            cited = re.findall(r"\b(C\d+|S\d+|B\d+)\b", cb)
            if cited:
                for mid in cited:
                    if not (mid in chars or mid in scenes or mid in outline_beats):
                        r.add(f"{sc.id} 台词回指 {mid!r}，但前文不存在该角色/场景/爽点")
            else:
                r.warn(f"{sc.id} 台词回指「{cb}」为纯文字线索，无法核验前情是否存在")
        _narration_errors(sc, si, chars, r)
        _transition_errors(sc, si, p, r, enforce=enforce_transitions)
        # P0-S6 场景人物引用必须真实：越界（幻觉 C6）与漏登记（戏里有但 chars 没写）
        # 都会在 EDL 阶段变成"越出本场人物"死循环，必须在剧本门拦住。
        extra_chars = set(sc.characters) - set(chars)
        if extra_chars:
            r.add(f"{sc.id} 人物 {sorted(extra_chars)} 不在 cast 里")
        speakers = {b.speaker for b in sc.beats if b.speaker}
        if speakers - set(sc.characters):
            r.add(f"{sc.id} 台词人物 {sorted(speakers - set(sc.characters))} "
                  f"未登记进本场 characters {sorted(sc.characters)}")
        for bi, b in enumerate(sc.beats, 1):
            if b.action and _has_quote(b.action):
                _soft(r, strict,
                      f"{sc.id} 第 {bi} 拍：动作叙述体里不能出现引号台词")
            if b.action:
                _pv = _psych_verb(b.action)
                if _pv:
                    _soft(r, strict,
                          f"{sc.id} 第 {bi} 拍：动作拍含心理动词「{_pv}」不可拍"
                          "（摄像机拍不到内心；请转成可见动作/台词subtext/物件反应）")
                # P1-S1 电影化动作句：句长上限（一段一动作）+ 被动句检测
                if _line_chars(b.action) > ACTION_MAX_LEN:
                    _soft(r, strict,
                          f"{sc.id} 第 {bi} 拍动作句 {_line_chars(b.action)} 字超过 {ACTION_MAX_LEN}，"
                          "一段塞了多个动作，请拆成多拍或精简为「谁+对谁+做什么」")
                _pv2 = _passive_voice(b.action)
                if _pv2:
                    _soft(r, strict,
                          f"{sc.id} 第 {bi} 拍动作句含被动标记「{_pv2}」，"
                          "请改主动语态（谁对谁做了什么，摄影机跟主体走）")
                # P1 剧本层承接字段：动作拍必须声明起始承接与建议机位侧
                if not b.state_from.strip():
                    _soft(r, strict, f"{sc.id} 第 {bi} 拍缺 state_from"
                                     "（本拍承接上一拍末：谁在哪/朝向）")
                if not b.screen_side.strip():
                    _soft(r, strict, f"{sc.id} 第 {bi} 拍缺 screen_side"
                                     "（建议机位侧 left/right/center）")
            # P1-S2 微表演：有对白的关键拍必须给 expression（不临场发明）
            if b.line and not (b.expression or "").strip():
                _soft(r, strict, f"{sc.id} 第 {bi} 拍有对白但缺 expression"
                                  "（微表演提示，如：咬紧牙关/眼神闪躲）")
            if b.line:
                if b.speaker == "VO":
                    r.add(f"{sc.id} 第 {bi} 拍：不支持画外音；让说话人出现在本场")
                elif b.speaker not in sc.characters:
                    r.add(f"{sc.id} 第 {bi} 拍：speaker {b.speaker} 不在本场人物里")
                if _line_chars(b.line) > p.max_line_chars:
                    r.add(f"{sc.id} 第 {bi} 拍：台词 {_line_chars(b.line)} 字，"
                          f"超过 {p.max_line_chars} 字（一口气说不完）")
        for cid in sc.characters:
            if cid not in chars:
                r.add(f"{sc.id} 引用了不存在的角色 {cid}")
        for pid in sc.props:
            if not any(x.id == pid for x in art.props):
                r.add(f"{sc.id} 引用了不存在的道具 {pid}")
        for cid, wid in sc.wardrobe.items():
            c = chars.get(cid)
            if c and wid not in {w.state_id for w in c.wardrobe_states}:
                r.add(f"{sc.id} 的服装态 {wid} 不在 {cid} 的状态库里")
        # P0-C2 拍级状态变更：换装/道具易主/伤势须登记真实角色，且与场级 wardrobe 快照不矛盾
        for bi, b in enumerate(sc.beats, 1):
            for cid, chg in (b.state_change or {}).items():
                if cid not in chars:
                    r.add(f"{sc.id} 第 {bi} 拍 state_change 引用不存在的角色 {cid}")
                elif cid not in sc.characters:
                    r.warn(f"{sc.id} 第 {bi} 拍 state_change 角色 {cid} 不在本场人物表，状态变更无主")
                if any(w in chg for w in ("换", "脱", "穿", "披", "戴")):
                    if cid not in sc.wardrobe:
                        r.warn(f"{sc.id} 第 {bi} 拍 {cid} 发生换装但场级 wardrobe 未登记该角色，"
                               "请在场级快照补 {cid}:最终服装态")
        # P1-8 动作拍点名角色必须在场（联合 staging space_map 可执行性）
        for msg in _beat_characters_on_scene(sc):
            r.add(msg)
        # P1-2 拍级状态账本前移：拍末 state_out 逐拍衔接。
        # 下游 script_wardrobe_at 会跟随这里声明的快照，所以默认只提示；
        # strict 模式仍把它升级为硬错误。
        _beat_state_chain_check(sc, r, strict)
        # P2-2 场景重构决策记录：有重构必有理由、必有 from/to
        rs = sc.restructure or {}
        if rs:
            if rs.get("action") not in {"merge", "split"}:
                r.add(f"{sc.id} 声明了场景重构但 action 非法"
                      f"（{rs.get('action')!r}，应为 merge/split）")
            if not (rs.get("reason") or "").strip():
                r.add(f"{sc.id} 声明了{rs.get('action')}但无理由"
                      "（场景重构决策必须可复核：from/to/理由齐全）")
            if not rs.get("from") or not rs.get("to"):
                r.add(f"{sc.id} 场景重构缺 from/to 场景清单")
        # P0-S3 props/objects 对账：props(交互道具 P id) 应登记在场陈设 objects 中
        for pid in sc.props:
            _pn = next((x.name for x in art.props if x.id == pid), None)
            if _pn and _pn not in sc.objects:
                r.warn(f"{sc.id} 道具 {pid}({_pn}) 未登记进 objects（场陈设），跨镜道具引用无源")
        line_sec = sum(_beat_seconds(b, p) for b in sc.beats if b.line)
        if line_sec > _scene_seconds(sc, p) * 0.55:
            r.warn(f"{sc.id} 台词占本场 {line_sec / _scene_seconds(sc, p):.1%}，"
                   "超过 55%，画面会被说话拖死")

    # ------------------------------------------------------------------
    # 情节驱动门：必须事件 / 场间因果 / 关键道具 / 关键镜头权重
    # ------------------------------------------------------------------
    must_show = [m.strip() for m in (plan.must_show if plan else []) if m.strip()]
    key_props = [p.strip() for p in (plan.key_props if plan else []) if p.strip()
                 if not re.fullmatch(r"P\d+", p.strip())]
    if must_show:
        ep.must_show = list(must_show)
    if key_props:
        ep.key_props = list(key_props)

    # 1) 每一条 must_show 必须落到至少一场；major_event 不得新造关键情节
    for m in must_show:
        hits = [sc for sc in ep.scenes
                if event_match(sc.major_event, m)
                or _event_key(m) in _event_key(_scene_text(sc))
                or event_score(_scene_text(sc), m) >= 0.6]
        if not hits:
            r.add(f"必备情节没有落到任何一场：{m}"
                  "（必须在某场 major_event 原样认领，并真正演出该事件）")
    for sc in ep.scenes:
        ev = (sc.major_event or "").strip()
        if (ev and must_show
                and not any(event_match(ev, m) or event_score(ev, m) >= 0.6
                            for m in must_show)):
            r.add(f"{sc.id} 的 major_event「{ev}」不在本集 must_show 里"
                  "（不得新造关键情节/跳过既定情节）")

    # 2) 场间因果链：第 2 场起必须写“本场由上一场什么结果导致”。
    # 情节模式（有 must_show）下是硬门；旧大纲没有 must_show 时只提醒，
    # 避免历史工程/人工大纲因新增字段直接失效。
    causal_hard = bool(must_show)
    for si, sc in enumerate(ep.scenes, 1):
        if si == 1:
            if not (sc.causal_from or "").strip():
                _soft(r, strict, f"{sc.id} 缺 causal_from（开场如何承接上集/建立情境）")
            continue
        causal = (sc.causal_from or "").strip()
        if not causal:
            msg = (f"{sc.id} 缺 causal_from：每一场都必须由上一场的结果导致，"
                   "禁止无因果跳场")
            if causal_hard:
                r.add(msg)
            else:
                _soft(r, strict, msg)
            continue
        prev = ep.scenes[si - 2]
        prev_key = _event_key(" ".join([prev.major_event or "", prev.turn or "",
                                        prev.cost or ""]))
        if prev_key and _event_key(causal) and _event_key(causal) not in prev_key:
            # 不要求逐字，但必须能看出接的是上一场；完全无关才提示。
            overlap = event_score(causal, prev.major_event or prev.turn or "")
            if overlap <= 0:
                _soft(r, strict, f"{sc.id} 的 causal_from「{causal}」"
                                 f"与上一场 {prev.id} 的结果对不上，疑似跳场")

    # 3) 关键道具：必须在场次 props 里出现，并至少有一个 insert/特写拍
    for prop in key_props:
        prop_scenes = [sc for sc in ep.scenes if scene_has_prop(sc, prop, art)]
        if not prop_scenes:
            r.add(f"关键道具「{prop}」没有在任何一场出场"
                  "（key_props 必须真正参与本集剧情）")
            continue
        emph = [b for sc in prop_scenes for b in sc.beats
                if b.emphasis == "key_prop" and beat_has_prop(b, prop)]
        if not emph:
            r.add(f"关键道具「{prop}」没有任何 key_prop 特写拍"
                  "（至少一个 insert 拍，emphasis=key_prop，visual_focus 写明道具）")
            continue
        if not any(b.coverage == "insert" for b in emph):
            _soft(r, strict, f"关键道具「{prop}」的 emphasis 拍没有标 coverage=insert"
                             "（分镜仍会强制特写，但剧本层应明确它是插入镜）")

    # 4) 关键镜头权重：emphasis 字段必须配 visual_focus
    for sc in ep.scenes:
        for bi, b in enumerate(sc.beats, 1):
            if b.emphasis != "none" and not (b.visual_focus or "").strip():
                r.add(f"{sc.id} 第 {bi} 拍 emphasis={b.emphasis} 但缺 visual_focus"
                      "（分镜不知道这一拍该让观众看什么）")
            if b.emphasis == "key_prop" and not (b.coverage == "insert"):
                _soft(r, strict, f"{sc.id} 第 {bi} 拍是 key_prop 但不是 insert"
                                 "（关键道具应单独给插入特写）")

    # 与 drama 门对齐的三条硬规则
    early_choice = [s for s, t in zip(ep.scenes, starts)
                    if t <= 20.0 and s.beat == "抉择"]
    if not early_choice:
        _soft(r, strict, "20 秒内没有任何一场 beat=抉择"
                          "（主角必须尽早做不可逆选择）")
    mid_fail = [s for s, t in zip(ep.scenes, starts)
                if 15.0 <= t <= 60.0 and s.beat in {"受挫", "代价"}
                and s.cost.strip() not in ("", "无")]
    if not mid_fail:
        _soft(r, strict, "中段（15-60s）没有 beat=受挫/代价 且 cost 非空的场")
    if ep.scenes[-1].beat != "结尾钩子":
        r.add(f"最后一场 beat={ep.scenes[-1].beat!r}，必须是“结尾钩子”")
    n_lines = sum(1 for s in ep.scenes for b in s.beats if b.line)
    # 台词句数不设硬上限（用户要求：关键对话密集/纯对话集都可以）：
    # 只在源文本对白稀薄且本集异常多台词时给一个参考提示。
    if dialogue_light and n_lines > 8:
        _soft(r, strict, f"源文本对白稀薄，本集却写了 {n_lines} 句台词；"
                          "建议以可见动作/旁白承载信息（台词句数不设硬上限，仅为参考）")
    # 对白密度：下限随集长按 3.0 字/秒缩放（90s≈270 字），
    # 上限=全集时长可承载的台词量（纯对话集合法，只是会撑满时长）。
    n_chars = sum(_line_chars(b.line) for s in ep.scenes for b in s.beats if b.line)
    min_chars = max(60, int(round(ep.target_seconds * 3.0 / 10)) * 10)
    max_chars = int(ep.target_seconds * p.chars_per_second)
    if n_chars > max_chars:
        _soft(r, strict,
              f"本集台词 {n_chars} 字，超出全集承载上限 {max_chars}"
              "（台词+动作时间放不下，会撑破时长）")
    elif 0 < n_chars < min_chars:
        r.warn(f"本集台词仅 {n_chars} 字，建议 {min_chars}-{max_chars} 字"
               "（过稀，对白推进不足；关键对话集不受此限）")

    # 一镜一拍：剧本拍数就是分镜镜头数。分镜层无法凭空补拍，所以在这里
    # 提前执行 drama 的镜头数下限，避免剧本合格、成片阶段才发现镜数不够。
    # 2026-10-30：单镜时长按内容计算（2-6s），平均基准从 2.6s 放宽到 3.5s，
    # 避免把每镜压成 2.5s 碎镜（这正是 SH03 放不下的根因之一）。
    total_beats = sum(len(s.beats) for s in ep.scenes)
    min_total_beats = max(1, round(ep.target_seconds / 3.5))
    if total_beats < min_total_beats:
        _soft(r, strict,
              f"本集只有 {total_beats} 拍，少于下限 {min_total_beats}"
              f"（一镜一拍；{ep.target_seconds:g}s 集按平均 ≤3.5s/镜至少需要 "
              f"{min_total_beats} 镜）。请补可独立拍摄的动作/反应/插入拍")

    # coverage 配额：与 manju.drama 的覆盖率硬门同源，必须在剧本层就规划好。
    required_cov = coverage_required(ep.target_seconds,
                                     dialogue_light=dialogue_light)
    coverage_actual: dict[str, int] = {}
    missing_cov: list[str] = []
    for si, s in enumerate(ep.scenes, 1):
        for bi, b in enumerate(s.beats, 1):
            if b.coverage is None:
                missing_cov.append(f"第{si}场第{bi}拍")
                cov = "dialogue" if b.line else "action"
            else:
                cov = b.coverage
            coverage_actual[cov] = coverage_actual.get(cov, 0) + 1
    if missing_cov:
        shown = "、".join(missing_cov[:12]) + ("…" if len(missing_cov) > 12 else "")
        _soft(r, strict,
              f"有 {len(missing_cov)} 拍未声明 coverage（{shown}）；"
              "每拍必须标注 establish/action/reaction/insert/pov/dialogue/transition")
    for cov, need in required_cov.items():
        got = coverage_actual.get(cov, 0)
        if got < need:
            _soft(r, strict,
                  f"coverage={cov} 仅 {got} 拍，少于配额 {need}"
                  f"（{ep.target_seconds:g}s 集要求 establish/action/reaction/"
                  "insert/pov/dialogue/transition 覆盖达标；请在剧本层补拍并标注 coverage）")

    # 钩子具象：必须在全集前 3 拍内
    gb = _global_beats(ep)
    if ep.hook_beat not in gb:
        r.add(f"hook_beat {ep.hook_beat} 不是本集有效节拍")
    elif gb.index(ep.hook_beat) >= 3:
        r.add(f"hook_beat {ep.hook_beat} 不在前 3 拍内（冷开场规则），实际第 "
              f"{gb.index(ep.hook_beat) + 1} 拍")

    for t in ep.beats_claimed:
        if t not in outline_beats:
            r.add(f"beats_claimed 里的 {t!r} 不在大纲爽点表里")
    # P0: 台词风格一致性门
    for m in _delivery_conflict(ep.scenes):
        r.warn(m)
    # P1-S5 台词 register 一致性（文白夹杂）
    for m in _register_mix(ep.scenes):
        _soft(r, strict, m)
    # P1-S4 竖屏范式：开场 3s 钩子 / 30-45s 卡点 / 集末 cliff 5-10s
    for m in _vertical_pacing(ep, p, strict):
        if m.startswith("[warn]"):
            r.warn(m)
        else:
            _soft(r, strict, m.removeprefix("[err] "))
    # P1-S3 改编忠实度：关键事件回指覆盖率
    align = narrative_alignment(ep, outline)
    if align["missing"]:
        _soft(r, strict,
              f"改编忠实度：关键事件 {align['missing']} 未在本集回指"
              f"（覆盖率 {align['coverage']:.0%}，目标 ≥50%）")
    return r


def narrative_alignment(ep: ScriptEpisode, outline: Outline) -> dict:
    """P1-S3 改编忠实度评估器（确定性，不上 LLM）。

    口径：本集 beats_claimed + callbacks 里回指的 B id 视为"已覆盖"；
    大纲里 episode==本集 的关键事件为"应回指"集合，未覆盖者进 missing。
    """
    covered: set[str] = set(ep.beats_claimed)
    for sc in ep.scenes:
        for cb in sc.callbacks:
            covered |= set(re.findall(r"\bB\d+\b", cb))
    expected = [b.id for b in outline.beats if b.episode == ep.ep]
    covered &= set(expected) | covered  # 只统计本集相关事件
    missing = [b for b in expected if b not in covered]
    total = len(expected) or 1
    return {"covered": sorted(covered & set(expected)), "missing": missing,
            "coverage": len(covered & set(expected)) / total,
            "expected": expected, "total": total}


def _vertical_pacing(ep: ScriptEpisode, p: ScriptParams, strict: bool) -> list[str]:
    """P1-S4 竖屏范式检查（纯拍级时间戳确定性计算）。"""
    out: list[str] = []
    clock = 0.0
    hook_at: float | None = None
    last_pivot_at: float | None = None
    for sc in ep.scenes:
        for b in sc.beats:
            dur = _beat_seconds(b, p)
            if hook_at is None and (sc.beat in {"钩子", "激励"} or clock <= 3.0):
                if sc.beat in {"钩子", "激励"}:
                    hook_at = clock
            if sc.beat in {"转折", "高潮", "受挫", "代价", "抉择"}:
                if last_pivot_at is not None and clock - last_pivot_at > 45.0:
                    out.append(f"[warn] 距上一小高潮/转折已 {clock - last_pivot_at:.0f}s"
                               "（>45s 无情绪波动点，竖屏节奏拖沓）")
                last_pivot_at = clock
            clock += dur
    # 开场 3 秒内必须引爆问题：hook_beat 落在的绝对时间
    gb = _global_beats(ep)
    if ep.hook_beat in gb:
        t = 0.0
        si, bi = 1, 0
        found = False
        for s_i, sc in enumerate(ep.scenes, 1):
            for b_i, b in enumerate(sc.beats, 1):
                if (s_i, b_i) == tuple(ep.hook_beat):
                    found = True
                    break
                t += _beat_seconds(b, p)
            if found:
                break
        if found and t > 3.0:
            out.append(f"[err] 开场 {t:.1f}s 才有 hook_beat（冷开场要求 ≤3s 引爆问题）")
    # 集末 cliff：最后一场时长须 5-15s（太短撑不住悬念，太长泄气）
    last_dur = _scene_seconds(ep.scenes[-1], p)
    if last_dur < 5.0:
        out.append(f"[err] 集末最后一场仅 {last_dur:.1f}s，cliff 撑不住（需 5-10s 悬念落点）")
    return out


# ==========================================================================
# 站位
# ==========================================================================
def validate_staging(staging: Staging, ep: ScriptEpisode, art: Art,
                     *, strict: bool = False) -> GateReport:
    r = GateReport(f"staging.ep{ep.ep}")
    locs = {l.scene_id: l for l in staging.locations}
    eps = [s for s in staging.scenes if s.ep == ep.ep]
    if len(eps) != len(ep.scenes):
        r.add(f"站位场次数 {len(eps)} 与剧本场次数 {len(ep.scenes)} 不一致")
    for i, (sc, st) in enumerate(zip(ep.scenes, eps), 1):
        if st.scene != i:
            r.add(f"站位第 {i} 场的 scene 编号是 {st.scene}")
        if st.scene_id != sc.id:
            r.add(f"站位第 {i} 场 scene_id={st.scene_id}，剧本是 {sc.id}")
        loc = locs.get(sc.id)
        if loc is None:
            r.add(f"站位缺少地点 {sc.id}")
            continue
        items = {x.name: x for x in loc.items}
        cast_ids = {c.id for c in st.cast}
        if cast_ids != set(sc.characters):
            r.add(f"第 {i} 场 cast {sorted(cast_ids)} 与剧本人物 {sorted(sc.characters)} 不一致")
        place_ids = {p.id for p in st.places}
        if place_ids != cast_ids:
            r.add(f"第 {i} 场 places 必须覆盖每个出场人物恰好一次")
        for p in st.places:
            if not p.present:
                continue
            if p.item and p.item not in items:
                r.add(f"第 {i} 场 {p.id} 的 item {p.item!r} 不在地点物件表里")
                continue   # 缺物件时跳过后续 items 访问，避免 items[p.item] KeyError
            if p.item is None:
                r.warn(f"第 {i} 场 {p.id} 没有绑定物件（走位会退化为平面中心）")
            if p.item and p.side == "on" and not items[p.item].on:
                r.add(f"第 {i} 场 {p.id} 站在 {p.item} 上，但该物件 on=false")
        if st.extras > 0 and not st.extras_where:
            _soft(r, strict,
                  f"第 {i} 场有 {st.extras} 个群演，必须写 extras_where")
    return r


# ==========================================================================
# 分镜
# ==========================================================================
def _prompt_lang_error(text: str, p: ScriptParams) -> str | None:
    """首帧提示词语言检查。语言是引擎能力，默认 en；zh/any 不拦。"""
    if p.prompt_language != "en":
        return None
    if re.search(r"[一-鿿]", text or ""):
        return "prompt_en 含中文（当前配置要求英文提示词；" \
               "story.prompt_language=any/zh 可关闭）"
    return None


def _cut_line_count(scene: ScriptScene, a: int, b: int) -> int:
    return sum(1 for i in range(a, b + 1)
               if 1 <= i <= len(scene.beats) and scene.beats[i - 1].line)


def _emphasis_errors(cut) -> list[str]:
    """情节权重镜头硬规则：关键道具必须特写，关键转折/高潮必须有运镜。"""
    errs: list[str] = []
    if cut.emphasis == "key_prop":
        if cut.size not in {"close", "extreme-close"}:
            errs.append(f"切镜 {cut.beats} 是 key_prop，景别必须 close/extreme-close，"
                        f"当前 {cut.size}")
        if cut.camera == "Static Shot":
            errs.append(f"切镜 {cut.beats} 是 key_prop，机位不能是 Static Shot"
                        "（必须推/拉/移，让观众注意到道具）")
        if not (cut.visual_focus or "").strip():
            errs.append(f"切镜 {cut.beats} 是 key_prop，但缺 visual_focus")
    elif cut.emphasis in {"highlight", "climax"}:
        if cut.camera == "Static Shot":
            errs.append(f"切镜 {cut.beats} 是 {cut.emphasis}，机位不能是 Static Shot"
                        "（关键转折/高潮必须有运镜）")
        if not (cut.visual_focus or "").strip():
            errs.append(f"切镜 {cut.beats} 是 {cut.emphasis}，但缺 visual_focus")
    return errs


def validate_segment_cuts(segment: StorySegment, scene: ScriptScene, cast: Cast,
                          *, state_in=None,
                          prev_cuts: list | None = None,
                          params: ScriptParams | None = None,
                          strict: bool = False) -> GateReport:
    """单场分镜门：逐场顺序生成时用，避免把整集门槛套到一场上。"""
    from .continuity import validate_segment as _validate_state

    r = GateReport(f"board.scene{segment.scene_index}")
    p = params or ScriptParams()
    cuts = segment.cuts
    tr = segment.transition_in
    if tr is not None and cuts:
        allowed = TRANSITION_CUT_REASON.get(tr.type, set())
        if tr.type in BRIDGE_TRANSITIONS and not tr.anchor.strip():
            _soft(r, strict, f"{scene.id} {tr.type} 缺 anchor：场间没有共享锚点")
        if allowed and cuts[0].cut_reason not in allowed:
            r.warn(f"{scene.id} 用 {tr.type} 转场，但首镜 cut_reason="
                   f"{cuts[0].cut_reason}；建议用 {sorted(allowed)} 之一承接")
    elif tr is None and segment.scene_index > 1 and p.transition_mode == "on":
        _soft(r, strict, f"{scene.id} 缺 transition_in（transition_mode=on）")
    # 契约自洽：切镜按节拍认领，且一拍不能被两个切镜重复认领，
    # 所以切镜数天然 ≤ 本场拍数。"至少 4 镜"只能要求 min(4, 拍数)；
    # 否则 3 拍的场次无论怎么重写都不可能通过（绿野仙踪 S1 即此情况）。
    min_cuts = min(4, len(scene.beats))
    if len(cuts) < min_cuts:
        _soft(r, strict,
              f"本场只有 {len(cuts)} 个切镜，至少 {min_cuts} 个"
              f"（本场 {len(scene.beats)} 拍）")
    elif len(cuts) < 4:
        r.warn(f"本场只有 {len(scene.beats)} 拍 / {len(cuts)} 镜；"
               "建议剧本层补 1 拍以获得 4 镜覆盖")
    total = sum(c.seconds for c in cuts)
    want = _scene_seconds(scene, p)
    if abs(total - want) > 0.3:
        r.add(f"本场切镜合计 {total:.2f}s ≠ 剧本 {want:.2f}s"
              f"（差 {abs(total - want):.2f}s，允许 0.3s）")
    claimed: list[int] = []
    for c in cuts:
        # 纯空镜（无人物、无道具）没有可继承状态是合法的；有人物却没状态
        # 才会导致下一镜瞬移/换装无从对账。
        if c.state_out.empty() and c.characters:
            r.add(f"本场 {c.beats} 镜没有声明 state_out"
                  "（where/facing/posture/wardrobe/props/injuries）")
        if not p.min_shot_seconds <= c.seconds <= p.max_shot_seconds:
            r.add(f"本场有 {c.seconds:.1f}s 的切镜，超出 "
                  f"{p.min_shot_seconds:g}-{p.max_shot_seconds:g}s")
        r.errors.extend(_emphasis_errors(c))
        if not c.prompt_en.strip():
            r.add("切镜缺少 prompt_en")
        else:
            lang_err = _prompt_lang_error(c.prompt_en, p)
            if lang_err:
                r.add(lang_err)
        low = c.prompt_en.lower()
        bad_cam = [w for w in CAMERA_WORDS if w in low]
        # 2026-10-30：运镜不再禁用——prompt_en 允许写运镜起点（如 the camera
        # begins pushing in），运镜主体由 motion/机位承担。此处仅保留画风词提醒。
        if not c.frame.strip():
            r.add("切镜缺少 frame")
        if not set(c.characters) <= set(scene.characters):
            r.add(f"切镜人物 {c.characters} 越出本场人物 {scene.characters}")
        faces = _face_lock_chars(cast, c.characters)
        if (p.max_faces_per_shot is not None
                and len(faces) > p.max_faces_per_shot
                and not c.allow_multi_face):
            r.add(f"同框 {sorted(faces)} 张正脸；当前配置的上限是 "
                  f"{p.max_faces_per_shot} 张/画幅"
                  "（story.max_faces_per_shot；不限制请设为 null）")
        if not set(c.props) <= set(scene.props):
            r.add(f"切镜道具 {c.props} 越出本场道具 {scene.props}")
        if not set(c.characters) <= {s.char for s in c.subjects}:
            r.add("subjects 没覆盖 characters")
        a, b = c.beats
        if a > b or b > len(scene.beats):
            r.add(f"切镜节拍 {c.beats} 越界（本场 {len(scene.beats)} 拍）")
            continue
        claimed.extend(range(a, b + 1))
        n_lines = _cut_line_count(scene, a, b)
        if p.max_lines_per_shot is not None and n_lines > p.max_lines_per_shot:
            r.add(f"切镜 {c.beats} 含 {n_lines} 句台词；当前引擎上限 "
                  f"{p.max_lines_per_shot} 句/镜"
                  "（story.max_lines_per_shot；不限制请设为 null）")
        line_sec = sum(_beat_seconds(scene.beats[i - 1], p) for i in range(a, b + 1))
        if line_sec > c.seconds + 0.05:
            r.add(f"切镜 {c.beats} 装不下台词：需要 {line_sec:.1f}s，只给了 {c.seconds:.1f}s")
        # ---- 2026-10-30 时长与阶段化（EDL 级同口径） ----
        # 注意：EDL 层 seconds 由剧本拍确定性推导（_beat_seconds），LLM 无法在此
        # 层改时长；超载只能靠分镜层拆段（segments）或人工拆镜。因此这里用 warn
        # 提示（不阻断），hard error 留给 segments 自洽与台词装不下。
        n_actions = count_action_verbs(f"{c.prompt_en} {c.motion}")
        if n_actions:
            need_sec = n_actions * p.action_seconds_per_beat
            if c.seconds + 1e-6 < need_sec:
                r.warn(f"切镜 {c.beats} 有 {n_actions} 个动作动词，建议时长 ≥{need_sec:.1f}s"
                       f"（{n_actions}×{p.action_seconds_per_beat:g}s），当前 {c.seconds:.1f}s"
                       "——成片易放不下，请在分镜层拆段（segments）或人工拆镜")
            if (p.max_actions_per_shot is not None
                    and n_actions > p.max_actions_per_shot):
                r.warn(f"切镜 {c.beats} 有 {n_actions} 个动作，超过引擎建议上限 "
                       f"{p.max_actions_per_shot} 个/镜"
                       "（story.max_actions_per_shot）——请拆镜或拆段（segments）")
    expected = list(range(1, len(scene.beats) + 1))
    if claimed != expected:
        r.add(f"节拍覆盖不连续/有漏：{claimed}，应为 {expected}")
    r.errors.extend(_validate_state(segment, scene, cast,
                                    state_in=state_in, prev_cuts=prev_cuts))
    # P0 剪辑纪律：跳切/越轴迹象/动作断点/时长下限（默认 warning，strict 升级）
    from .editorial import validate_editing
    edit_errs, edit_warns = validate_editing(cuts, scene, strict=strict)
    r.errors.extend(edit_errs)
    r.warnings.extend(edit_warns)
    return r


def validate_board(board: StoryEpisodeBoard, ep: ScriptEpisode,
                   cast: Cast, params: ScriptParams | None = None,
                   *, strict: bool = False) -> GateReport:
    r = GateReport(f"storyboard.ep{ep.ep}")
    p = params or ScriptParams()
    n_scenes = len(ep.scenes)
    by_scene: dict[int, list] = {i: [] for i in range(1, n_scenes + 1)}
    seg_ids = []
    total = 0.0
    for seg in board.segments:
        seg_ids.append(seg.id)
        if not 1 <= seg.scene_index <= n_scenes:
            r.add(f"段 {seg.id} 的 scene_index={seg.scene_index} 越界")
            continue
        seg_total = sum(c.seconds for c in seg.cuts)
        total += seg_total
        if seg_total > p.max_segment_seconds + 1e-6:
            r.add(f"段 {seg.id} 总时长 {seg_total:.1f}s，超过 "
                  f"{p.max_segment_seconds:g}s（story.max_segment_seconds）")
        for c in seg.cuts:
            for msg in _emphasis_errors(c):
                r.add(f"段 {seg.id}：{msg}")
            if not p.min_shot_seconds <= c.seconds <= p.max_shot_seconds:
                r.add(f"段 {seg.id} 有 {c.seconds:.1f}s 的切镜，超出 "
                      f"{p.min_shot_seconds:g}-{p.max_shot_seconds:g}s")
            # ---- 2026-10-30 时长与阶段化：动作数 × 基准 = 时长下限（warn） ----
            n_actions = count_action_verbs(f"{c.prompt_en} {c.motion}")
            if n_actions:
                need_sec = n_actions * p.action_seconds_per_beat
                if c.seconds + 1e-6 < need_sec:
                    r.warn(f"段 {seg.id} 切镜 {c.beats} 有 {n_actions} 个动作动词，"
                          f"建议时长 ≥{need_sec:.1f}s（{n_actions}×"
                          f"{p.action_seconds_per_beat:g}s），当前 {c.seconds:.1f}s"
                          "——成片易放不下，请在分镜层拆段（segments）或人工拆镜")
                if (p.max_actions_per_shot is not None
                        and n_actions > p.max_actions_per_shot):
                    r.warn(f"段 {seg.id} 切镜 {c.beats} 有 {n_actions} 个动作，"
                          f"超过引擎建议上限 {p.max_actions_per_shot} 个/镜"
                          "（story.max_actions_per_shot）——请拆镜或拆段（segments）")
            # segments 自洽：段秒数和=整镜、区间连续不重叠
            if c.segments:
                seg_sum = sum(s.duration_sec for s in c.segments)
                if abs(seg_sum - c.seconds) > 0.11:
                    r.add(f"段 {seg.id} 切镜 {c.beats} 的 segments 秒数和 "
                          f"{seg_sum:.2f}s ≠ 整镜 {c.seconds:.2f}s")
                prev_end = 0.0
                for s in c.segments:
                    if s.time_range:
                        st, en = s.time_range
                        if abs(st - prev_end) > 0.11:
                            r.add(f"段 {seg.id} 切镜 {c.beats} segments 区间不连续："
                                  f"{s.time_range} 应从 {prev_end:.2f}s 起")
                        prev_end = en
            if not c.prompt_en.strip():
                r.add(f"段 {seg.id} 有切镜缺少 prompt_en")
            else:
                lang_err = _prompt_lang_error(c.prompt_en, p)
                if lang_err:
                    r.add(f"段 {seg.id} 的 {lang_err}")
            low = c.prompt_en.lower()
            bad_style = [w for w in STYLE_WORDS if w in low]
            if bad_style:
                r.warn(f"段 {seg.id} 的 prompt_en 含画风词 {bad_style}，应交给 visual_bible")
            # 2026-10-30：运镜不再禁用——prompt_en 允许运镜起点，运镜主体由
            # motion/机位承担；删除旧的“镜头运动词”报错。
            # 单镜信息密度：过短（<40 字符，且非占位符兜底）说明画面描述贫瘠，
            # 生成结果会"讲不清"——每镜应含动作动词+情绪/表情+可辨识细节。
            pe = c.prompt_en.strip()
            if (pe and len(pe) < 40
                    and "人物与环境清晰可见" not in pe
                    and "subject and environment visible" not in pe):
                r.warn(f"段 {seg.id} 切镜 {c.beats} 提示词过短（{len(pe)} 字符），"
                       "画面信息可能不足：应含动作动词+情绪/表情+至少一个可辨识细节")
            if not c.frame.strip():
                r.add(f"段 {seg.id} 有切镜缺少 frame")
            sc = ep.scenes[seg.scene_index - 1]
            if not set(c.characters) <= set(sc.characters):
                r.add(f"段 {seg.id} 切镜人物 {c.characters} 越出剧本本场人物")
            faces = _face_lock_chars(cast, c.characters)
            if (p.max_faces_per_shot is not None
                    and len(faces) > p.max_faces_per_shot
                    and not c.allow_multi_face):
                r.add(f"段 {seg.id} 切镜同框 {sorted(faces)} 张正脸；"
                      f"当前配置上限 {p.max_faces_per_shot} 张/画幅"
                      "（story.max_faces_per_shot）。"
                      "改成单人反打/过肩/背影/局部；若全员确实背身或剪影，"
                      "置 allow_multi_face=true")
            if not set(c.props) <= set(sc.props):
                r.add(f"段 {seg.id} 切镜道具 {c.props} 越出剧本本场道具")
            sub_ids = {s.char for s in c.subjects}
            if not set(c.characters) <= sub_ids:
                r.add(f"段 {seg.id} 切镜 subjects 没覆盖 characters")
            by_scene[seg.scene_index].append(c)
    if len(seg_ids) != len(set(seg_ids)):
        r.add(f"段号重复：{seg_ids}")

    # 节拍覆盖：每场恰好一次、按顺序、连续
    for si, sc in enumerate(ep.scenes, 1):
        cuts = by_scene.get(si, [])
        if not cuts:
            r.add(f"第 {si} 场没有任何切镜")
            continue
        min_cuts = min(4, len(sc.beats))
        if len(cuts) < min_cuts:
            _soft(r, strict,
                  f"第 {si} 场只有 {len(cuts)} 个切镜，至少 {min_cuts} 个"
                  f"（本场 {len(sc.beats)} 拍）")
        elif len(cuts) < 4:
            r.warn(f"第 {si} 场 {len(sc.beats)} 拍 / {len(cuts)} 镜；"
                   "建议剧本层补拍以获得 4 镜覆盖")
        scene_sec = sum(c.seconds for c in cuts)
        want = _scene_seconds(sc, p)
        if abs(scene_sec - want) > 0.3:
            r.add(f"第 {si} 场切镜合计 {scene_sec:.2f}s ≠ 剧本 {want:.2f}s"
                  f"（差 {abs(scene_sec - want):.2f}s，允许 0.3s）")
        claimed: list[int] = []
        for c in cuts:
            a, b = c.beats
            if a > b or b > len(sc.beats):
                r.add(f"第 {si} 场切镜节拍 {c.beats} 越界（本场 {len(sc.beats)} 拍）")
                continue
            claimed.extend(range(a, b + 1))
            n_lines = _cut_line_count(sc, a, b)
            if p.max_lines_per_shot is not None and n_lines > p.max_lines_per_shot:
                r.add(f"第 {si} 场切镜 {c.beats} 含 {n_lines} 句台词；"
                      f"当前引擎上限 {p.max_lines_per_shot} 句/镜"
                      "（story.max_lines_per_shot；不限制请设为 null）")
            line_sec = sum(_beat_seconds(sc.beats[i - 1], p)
                           for i in range(a, b + 1))
            if line_sec > c.seconds + 0.05:
                r.add(f"第 {si} 场切镜 {c.beats} 装不下台词：需要 {line_sec:.1f}s，"
                      f"只给了 {c.seconds:.1f}s")
        expected = list(range(1, len(sc.beats) + 1))
        if claimed != expected:
            r.add(f"第 {si} 场节拍覆盖不连续/有漏：{claimed}，应为 {expected}")

    target = ep.target_seconds
    if abs(total - target) > target * p.tolerance:
        r.add(f"分镜总时长 {total:.1f}s，目标 {target:.1f}s，超出 ±{p.tolerance:.0%}")
    all_cuts = [c for seg in board.segments for c in seg.cuts]
    n = len(all_cuts)
    # 2026-10-30：按内容算时长后平均基准放宽（3.5s/镜），上限 1.5s/镜不变
    lo, hi = max(1, round(target / 3.5)), max(1, round(target / 1.5))
    if not lo <= n <= hi:
        _soft(r, strict,
              f"切镜 {n} 个，{target:.0f}s 集应在 {lo}-{hi} 个"
              "（平均 1.5-3.5s/镜）")
    if n:
        static = sum(1 for c in all_cuts if c.camera == "Static Shot") / n
        # 按引擎档案收敛运镜强度：full=必须有运镜（静态 ≤40%），
        # limited=轻度运镜（静态 ≤60%）。关键拍已有独立硬规则
        # （key_prop/highlight/climax 必须非 Static）。
        static_cap = 0.6 if getattr(p, "camera_motion", "full") == "limited" else 0.4
        if static > static_cap:
            _soft(r, strict,
                  f"Static Shot 占 {static:.0%}，不得超过 {static_cap:.0%}"
                  "（每镜必须有运镜；定场/反应镜用缓慢推近或轻摇代替纯静止）")
        elif static < 0.10:
            r.warn(f"Static Shot 仅占 {static:.0%}，建议保留少量"
                   "定场/反应镜做节奏呼吸")
        sizes = {c.size for c in all_cuts}
        if len(sizes) < 2:
            _soft(r, strict, "全集只有一种景别")
        # drama 的覆盖率门槛按目标时长等比缩放。覆盖率是叙事口味，
        # 默认只提示；关键拍（key_prop/highlight/climax）仍由硬规则强制。
        scale = target / 90.0
        for key, need in (("reaction", 8), ("insert", 6),
                          ("establish", 3), ("pov", 1)):
            got = sum(1 for c in all_cuts if c.coverage == key)
            need_n = max(1, round(need * scale))
            if got < need_n:
                _soft(r, strict,
                      f"coverage.{key} 只有 {got} 个，"
                      f"{target:.0f}s 集至少 {need_n} 个")
        run: list[tuple[str, str]] = []
        for c in all_cuts:
            key = (c.size, c.coverage)
            if run and run[-1] == key:
                run.append(key)
                if len(run) >= 3:
                    _soft(r, strict,
                          f"连续 3 镜同景别/同 coverage：{c.size}/{c.coverage}")
                    break
            else:
                run = [key]

    # 连续性（state_out 跨镜/跨场；旧分镜没有 state_out 时只给 warning）
    cont_errs, cont_warns = validate_board_continuity(board, ep, cast)
    r.errors.extend(cont_errs)
    r.warnings.extend(cont_warns)
    return r


# ==========================================================================
# 总入口
# ==========================================================================
# ==========================================================================
# 外部 storyboard（input/epNN/storyboard.json）的对白硬规则
# ==========================================================================
_LINE_FMT = re.compile(
    r"^(?P<kind>同期|画外)\s*[（(]\s*(?P<char>C\d+)\s*[，,]\s*"
    r"(?P<lip>画内|无口型)\s*[）)]\s*[：:]\s*(?P<text>.+)$")


def validate_external_board(raw: Any, *, strict: bool = True,
                            max_lines_per_shot: int | None = None) -> GateReport:
    """校验编译器产出的 storyboard 台词约定。

    1. 单镜台词数：由 `max_lines_per_shot` 决定；None=不限制（通用默认），
       配置 1 时即「一镜最多一句」。
    2. 说话人必须在场：标 `画内` 的说话人必须出现在该镜 subjects/ref_characters。
    3. 口型标记与画内一致：`画内` ⇔ `requirements.dubbing == "lipsync"`。
    4. v10.3 旁白锚点（§8.4）：`shot.narration[]` 必须带锚点字段且落在锚镜内。

    `画外` 台词允许说话人不在画面，但会提示应补听者反应镜。
    """
    from ..schemas import Storyboard  # 延迟导入，避免 story 包与 schemas 循环

    board = raw if isinstance(raw, Storyboard) else Storyboard.model_validate(raw)
    r = GateReport(f"storyboard.external.ep{board.episode}")
    shots_by_id = {s.shot_id: s for s in board.shots}
    for shot in board.shots:
        sid = shot.shot_id
        audio = str(shot.audio or "").strip()
        if not audio:
            continue
        parts = [p.strip() for p in re.split(r"[；;]", audio) if p.strip()]
        if (max_lines_per_shot is not None
                and len(parts) > max_lines_per_shot):
            r.add(f"{sid} 有 {len(parts)} 句台词；当前引擎上限 "
                  f"{max_lines_per_shot} 句/镜"
                  "（story.max_lines_per_shot；不限制请设为 null）")
        for line in parts:
            m = _LINE_FMT.match(line)
            if not m:
                r.add(f"{sid} 台词格式不合规（应为“同期（C1，画内）：…”）：{line[:40]}")
                continue
            kind, cid, lip = m.group("kind"), m.group("char"), m.group("lip")
            on_frame = (cid in (shot.ref_characters or [])
                        or any(str(s.char) == cid for s in (shot.subjects or [])))
            _dub = getattr(shot.requirements, "dubbing", None)
            dubbed = str(getattr(_dub, "value", _dub) or "")
            if kind == "同期" and lip != "画内":
                r.add(f"{sid} 同期台词却标“{lip}”，应为“画内”")
            if kind == "画外" and lip == "画内":
                r.add(f"{sid} 画外台词却标“画内”，应为“无口型”")
            if lip == "画内":
                if not on_frame:
                    r.add(f"{sid} 画内台词说话人 {cid} 不在画面里")
                if dubbed != "lipsync":
                    r.add(f"{sid} 画内台词要求 dubbing=lipsync，当前为 {dubbed or '空'}")
            else:
                if dubbed == "lipsync":
                    r.add(f"{sid} 画外台词不应要求 lipsync（会把背身/画外脸做变形）")
                # 画外音语境：hints.vo_context 显式声明后不再重复告警。
                # - back_turned：说话人在画内但背身/过肩，做口型会拧后脑
                # - action_vo / listener_reaction / pov：动作中的旁白、听者反应、主观镜
                ctx = str(getattr(shot.hints, "vo_context", "") or "")
                if kind == "画外" and on_frame and ctx not in ("back_turned", "action_vo"):
                    facing = ""
                    for fr in (shot.framing or {}).values():
                        for fs in getattr(fr, "subjects", []) or []:
                            if str(fs.char) == cid and fs.facing is not None:
                                facing = str(getattr(fs.facing, "value", fs.facing))
                    if facing != "away":
                        r.warn(f"{sid} 画外台词说话人 {cid} 在画面里；"
                               "若是背身/过肩可保留，否则应改画内并做口型")
                if (kind == "画外" and not on_frame
                        and ctx not in ("action_vo", "listener_reaction", "pov")):
                    r.warn(f"{sid} 画外音：确认配了听者反应镜，否则观众不知道谁在说")

        # v10.3 旁白锚点（§8.4）：有旁白必须带锚点字段且 at_sec 落在锚镜内。
        for i, n in enumerate(shot.narration or []):
            np = f"{sid}/narration[{i}]"
            if n.anchor_mode is None or n.shot_id is None \
                    or n.at_sec is None or n.timing_sec is None:
                r.add(f"{np} v10.3 要求旁白带完整锚点字段"
                      "（anchor_mode/shot_id/at_sec/timing_sec/allow_span_shots/"
                      "allow_cross_scene/max_shift_sec）")
                continue
            anchor = shots_by_id.get(n.shot_id)
            if anchor is None:
                r.add(f"{np} shot_id={n.shot_id!r} 不存在")
                continue
            if not (anchor.start_sec <= n.at_sec < anchor.end_sec):
                r.add(f"{np} at_sec={n.at_sec}s 不落在锚镜 {n.shot_id} "
                      f"[{anchor.start_sec:.2f}~{anchor.end_sec:.2f}) 内")
            need = max(1.0, math.ceil(len(n.text) / 5.0))
            if n.timing_sec < need - 1e-6:
                r.add(f"{np} timing_sec={n.timing_sec}s 小于按 5 字/秒所需的 {need}s")
            if n.anchor_mode == "within_shot" \
                    and n.at_sec + n.timing_sec > anchor.end_sec - 0.08 + 1e-6:
                r.add(f"{np} within_shot 旁白到 {n.at_sec + n.timing_sec:.2f}s，"
                      f"超出锚镜 {n.shot_id} 结束 {anchor.end_sec - 0.08:.2f}s（留 0.08s）")
    return r


def validate_external_continuity(raw: Any, *, strict: bool = False) -> GateReport:
    """外部分镜的镜间连续性（软校验）。

    规则刻意只覆盖**可判定**的矛盾，不猜创作意图：
      1. 同场相邻镜必须至少声明 `relation` 或 `prev_state`（否则等于没接）；
      2. 左右屏幕方向翻转必须标 `reverse`，否则是跳轴；
      3. 关键道具状态变了必须标 `new_scene` 或在画面里交代（这里只告警）。
    全片一个字段都没写时只出一条总告警，避免 35 镜刷屏。
    """
    from ..schemas import Storyboard  # 延迟导入，避免循环

    board = raw if isinstance(raw, Storyboard) else Storyboard.model_validate(raw)
    r = GateReport(f"storyboard.continuity.ep{board.episode}")
    by_scene: dict[str, list[Any]] = {}
    for shot in board.shots:
        by_scene.setdefault(str(shot.scene_id or "?"), []).append(shot)
    declared = sum(1 for s in board.shots if any((
        s.continuity.relation, s.continuity.screen_direction,
        s.continuity.prev_state, s.continuity.next_state,
        s.continuity.prop_state, s.continuity.light_state, s.continuity.eyeline)))
    if declared == 0:
        r.warn("全片分镜未声明 continuity（镜间承接状态）—— "
               "同场镜头只能靠 prompt 文本碰运气")
        return r
    for scene, shots in by_scene.items():
        prev = None
        for shot in shots:
            c = shot.continuity
            # continuation 必须写清"从哪来、到哪去"：这是渲染提示词的实际输入，
            # 缺了就等于又回到"每镜各写各的"。strict 下缺 prev_state 记 error，
            # 非 strict 保留为 warning（历史外部剧本仍可跑）。
            if c.relation == "continuation" and not c.prev_state:
                msg = f"{shot.shot_id} 标 continuation 但 prev_state 为空（无法承接上一镜）"
                if strict:
                    r.add(msg)
                else:
                    r.warn(msg)
            if c.relation == "continuation" and not c.next_state:
                r.warn(f"{shot.shot_id} 标 continuation 但 next_state 为空"
                       f"（下一镜接不到本镜的结束状态）")
            if prev is not None:
                pc = prev.continuity
                if not (c.relation or c.prev_state) and not pc.next_state:
                    r.warn(f"{prev.shot_id}→{shot.shot_id} 未声明承接关系"
                           f"（relation/prev_state 为空）")
                a, b = pc.screen_direction, c.screen_direction
                if (a and b and a != b and {a, b} == {"left_to_right", "right_to_left"}
                        and c.relation != "reverse"):
                    r.warn(f"{prev.shot_id}→{shot.shot_id} 屏幕方向翻转"
                           f"（{a}→{b}）未标 reverse —— 跳轴")
                # 只查"上一镜有道具状态、本镜没写"这种确定缺失。
                # 不比较两个字符串是否相等：每次状态推进本来就会换措辞，
                # 按字面不等号会 35 镜全刷警告（实测）。
                if (pc.prop_state and not c.prop_state
                        and c.relation not in ("new_scene", "insert", "cutaway")):
                    r.warn(f"{prev.shot_id}→{shot.shot_id} 未声明道具状态"
                           f"（上一镜：{pc.prop_state}）")
            prev = shot
    return r


def validate_all(outline: Outline, cast: Cast, art: Art,
                 scripts: list[ScriptEpisode], staging: Staging,
                 board: StoryStoryboard, *, strict: bool = False,
                 params: ScriptParams | None = None) -> list[GateReport]:
    """跑全部门。软门默认只记 warning，与 CLI 的 ``--strict`` 语义保持一致。

    `params` 携带调用方的渲染能力配置（如 max_faces_per_shot）；
    不传则使用通用默认（不限制同框人脸数）。
    """
    p = params or ScriptParams()
    reps = [validate_outline(outline, strict=strict),
            validate_cast(cast, outline, strict=strict),
            validate_art(art, outline, cast, strict=strict)]
    for ep in scripts:
        reps.append(validate_script(ep, outline, cast, art, p, strict=strict))
        reps.append(validate_staging(staging, ep, art, strict=strict))
        bep = next((b for b in board.episodes if b.ep == ep.ep), None)
        if bep is None:
            reps.append(GateReport(f"storyboard.ep{ep.ep}"))
            reps[-1].add("分镜缺少这一集")
        else:
            reps.append(validate_board(bep, ep, cast, p, strict=strict))
    return reps
