"""把故事层编译成 v10.1 渲染契约。

输出（默认写到 `input/`）：
  input/external/plan.json
  input/external/ep01/script.json
  input/external/ep01/director.json
  input/ep01/storyboard.json
  input/story_assets.json          # 角色定妆 / 场景 plate / 道具的生成清单
  input/prompts/ep01.md            # 人工核对用

编译器只做**确定性映射**，不再调用模型、不再创作。
"""
from __future__ import annotations

import json
import math
import re
from pathlib import Path
from typing import Any

from .. import jsonio
from ..drama import OBJECT_LEXICON
from .continuity import script_wardrobe_at
from .editorial import count_action_verbs
from .models import (
    Art, ArtScene, Cast, CastCharacter, CutState, Outline, ScriptEpisode,
    ScriptParams, ScriptScene, Staging, StagingPlace, StagingScene, StoryCut,
    StoryEpisodeBoard, StoryStoryboard, _line_chars, identity_anchor,
)

# --------------------------------------------------------------------------
# 视觉圣经（默认兜底）
# 权威来源在业务层：outline.visual_bible（StoryBible 字段，可被 LLM/用户覆盖）。
# 这里只保留默认画风，用户改 plan 层 visual_bible 即可换整剧画风，无需改代码。
# --------------------------------------------------------------------------
def _count_actions(text: str) -> int:
    """动作动词数（中英双语，去重）。时长算账用；保守子集宁可少报。"""
    if not text:
        return 0
    _v = re.compile(
        r"(推|拉|抓|跑|跳|摔|走|站起|坐下|转身|抬头|低头|抱|扔|捡|关|开|看|喊|哭|笑|跪|倒|冲|停|扑|滚|爬|躲|敲|撞|挥手|指|"
        r"walk|run|grab|push|pull|turn|look|reach|lean|open|close|lift|drop|step|rush|point|raise|slip|release|"
        r"sink|slide|stiffen|swallow|catch|approach|halt|swing|sweep|bend|kneel|stand|rise|shift|move|enter|leave|flee|"
        r"creep|stretch|crouch|jump|throw|toss|grip|yank|wrench|shove|whirl|dive|lunge|snap)", re.I)
    return len(set(_v.findall(text)))


def _duration_basis_text(cut, params: ScriptParams, shot_size: str,
                         actual_sec: float, cut_lines: list[dict]) -> str:
    """时长算账（中文，供人工复核）：内容下限（动作/台词/景别）vs EDL 实际分配。

    只做记录不做改写：镜和/场和仍以 EDL 分配的 cut.seconds 为准，
    防止 storyboard 单镜放大后打破"镜和=场和"约束。
    """
    n = _count_actions(f"{cut.prompt_en} {cut.motion}")
    floor = params.shot_size_floor.get(shot_size, 1.5)
    parts: list[str] = []
    act_t = 0.0
    if n:
        act_t = n * params.action_seconds_per_beat
        parts.append(f"动作{n}拍×{params.action_seconds_per_beat:g}s={act_t:.1f}s")
    chars = sum(_line_chars(ln.get("text") or "") for ln in cut_lines)
    line_t = 0.0
    if chars:
        line_t = chars / params.chars_per_second + len(cut_lines) * params.pause_seconds_per_line
        parts.append(f"台词{chars}字/{params.chars_per_second:g}s+"
                     f"{len(cut_lines)}停顿×{params.pause_seconds_per_line:g}s={line_t:.1f}s")
    parts.append(f"景别下限{floor:g}s")
    need = max(floor, act_t, line_t)
    verdict = "达标" if actual_sec + 1e-6 >= need else "不足（需拆镜/拆段）"
    return f"内容下限 {'；'.join(parts)} → 取大{need:.1f}s；EDL 分配{actual_sec:.1f}s（{verdict}）"


# drama 门 B4：prompt 不得含运镜词（运镜由 motion/camera_move 承担）。
# LLM 偶尔把运镜词写进 prompt 画面描述，compile 这里做确定性清洗兜底。
_CAMERA_WORD_CLEAN = {
    "tracking shot": "shot",
    "push-in": "shot",
    "push in": "shot",
    "pull-out": "shot",
    "pull out": "shot",
    "pan left": "shot",
    "pan right": "shot",
    "tilt up": "shot",
    "tilt down": "shot",
    "dolly": "shot",
    "camera move": "shot",
    "camera moves": "shot",
}


def _clean_prompt_camera(text: str) -> str:
    """从画面描述里剥离运镜词（大小写不敏感），替换为中性 'shot' 保持句式。"""
    if not text:
        return text
    low = text.lower()
    out = text
    for src, dst in _CAMERA_WORD_CLEAN.items():
        if src in low:
            # 逐段替换保留原大小写风格：仅对实际出现的小写命中区做原位替换
            pass
    # 统一做法：按小写命中做原位替换（保留替换目标为小写 shot，句内无害）
    for src in _CAMERA_WORD_CLEAN:
        pattern = re.compile(re.escape(src), re.I)
        out = pattern.sub("shot", out)
    out = re.sub(r"\bshot\s+shot\b", "shot", out)
    out = re.sub(r"\s{2,}", " ", out).strip()
    return out


def _state_brief(st: Any) -> str:
    """CutState → 段级承接文本（短摘要）。空状态返回空串。"""
    if st is None:
        return ""
    data = getattr(st, "model_dump", lambda: None)()
    if not data:
        return ""
    parts: list[str] = []
    for k in ("where", "facing", "posture", "wardrobe", "props"):
        if data.get(k):
            parts.append(f"{k}:{json.dumps(data[k], ensure_ascii=False)}")
    return "；".join(parts)[:120]


_SEG_ACTION_VERB = re.compile(
    r"(?:ing\b|ed\b|(?:turn|step|walk|run|grab|push|pull|reach|lean|open|close|lift|drop|"
    r"rush|point|raise|swing|bend|kneel|stand|rise|shift|move|enter|leave|flee|creep|"
    r"crouch|jump|throw|grip|yank|shove|whirl|dive|lunge|snap|scoop|clutch|pivot|slip|"
    r"slide|stagger|tumble|spin|slam|stomp|pound|stare|glance|freeze|cut|catch|glint|"
    r"rake|scatter|carry|wave|nod|shake|whisper|duck|dodge|tug|drag|haul)(?:s|es)\b)", re.I)


def _split_action_clauses(prompt_en: str) -> tuple[list[str], list[str], str]:
    """把整镜英文 prompt 切成 [基础定位, 动作从句, 状态从句]。

    基础定位 = 首句（含景别/主体/环境）；动作从句 = 含动作动词的逗号分句；
    状态从句 = 其余（表情/光线/细节）。供 fallback 语义拆段重组用，
    避免把整镜 prompt 开头整段复制进每个段（旧实现 L153 的缺陷）。
    """
    if not prompt_en:
        return [], [], ""
    head = prompt_en.split(",", 1)[0].strip()
    rest = prompt_en[len(head):]
    clauses = [c.strip() for c in rest.split(",") if c.strip()]
    acts: list[str] = []
    states: list[str] = []
    for c in clauses:
        if _SEG_ACTION_VERB.search(c):
            acts.append(c)
        else:
            states.append(c)
    return acts, states, head


def _cap_seg(text: str) -> str:
    """段文本首词大写、去首尾空白、保证句号结尾（小写开头的逗号分句转独立句）。"""
    t = " ".join(text.split())
    if not t:
        return ""
    t = t[0].upper() + t[1:]
    if not t.endswith((".", "!", "?", "…", '"')):
        t += "."
    return t


_RECIPE_LENS_MM = re.compile(r"(\d+(?:\.\d+)?)\s*mm\b")
_RECIPE_DOF = re.compile(
    r"(shallow\s*depth\s*of\s*field|deep\s*focus|deep\s*depth\s*of\s*field|"
    r"bokeh|compressed|wide[- ]angle|telephoto|long\s*lens|macro)", re.I)
_RECIPE_LIGHT_DIR = re.compile(
    r"(screen-left|screen-right|from\s+behind|behind\s+|top\s+light|rim\s*light|"
    r"backlight|key\s*light|window|side\s+light)", re.I)
_RECIPE_LIGHT_QUAL = re.compile(
    r"(soft|hard|diffused|harsh|cool|warm|grey|golden|dusk|overcast|dim|hazy)", re.I)


def _extract_camera_recipe(prompt_en: str) -> dict:
    """从整镜 prompt_en 确定性提取摄影配方，填 framing.camera_recipe 槽位。

    P0.5 契约 §6.5：实现层消费 lens_mm / depth_of_field / light_key /
    light_quality 结构化配方（ComfyUI 指南按此读配方）。prompt_en 文本是
    唯一配方来源（LLM 输出），compile 在此归一化提取，保持产物自洽。
    """
    d: dict[str, str] = {}
    m = _RECIPE_LENS_MM.search(prompt_en or "")
    if m:
        d["lens_mm"] = m.group(1) + "mm"
    m = _RECIPE_DOF.search(prompt_en or "")
    if m:
        v = m.group(1)
        d["depth_of_field"] = "shallow" if v.startswith("shallow") else (
            "deep" if "deep" in v else v.lower())
    m = _RECIPE_LIGHT_DIR.search(prompt_en or "")
    if m:
        d["light_key"] = m.group(1).lower().strip()
    m = _RECIPE_LIGHT_QUAL.search(prompt_en or "")
    if m:
        d["light_quality"] = m.group(1).lower().strip()
    return d


def _fallback_segments(cut, duration: float, n_actions: int) -> list[dict] | None:
    """确定性拆段兜底：LLM 未写 segments 时，对动作数>1 的镜按动作数拆 2-3 段。

    语义拆段（v2.0 修复）：从整镜 prompt_en 按动作从句重组每段的**独立英文句**
    （基础定位 + 该段动作），不再整段复制 prompt_en 开头（旧实现：每段=前 80
    字符截断，动作/剧情全部丢失，段间 100% 重复）。
    保证段和=整镜时长（按动作数均摊、末段收尾）、time_range 连续不重叠、
    末段 keyframe=True + frame_hint="last"（LTX 末帧注入 / Seedance 尾帧条件）、
    段级 state_out→state_in 承接。beat 用 cut.beat_goal（中文，人工核对）。
    单动作镜（n≤1）不需要拆段，返回 None 保持向后兼容。
    """
    if n_actions <= 1:
        return None
    k = min(3, max(2, n_actions))
    phases = (["start", "mid", "end"] if k == 3
              else ["start", "end"])
    base, rem = divmod(n_actions, k)
    counts = [base + (1 if i < rem else 0) for i in range(k)]
    acts, states, head = _split_action_clauses(cut.prompt_en or "")
    # 动作从句不够 k 段时：补状态从句；仍不够则按顺序循环（每段仍有独立句，不复制头）。
    pool = acts + [s for s in states if _SEG_ACTION_VERB.search("She " + s)]
    if len(pool) < k and cut.beat_goal:
        # 从中文动作要点补充英文动作句（确定性词表兜底，保持每段有动作）。
        zh_verbs = re.findall(r"(?:抱|抓|跑|跳|摔|走|站起|坐下|转身|抬头|低头|扔|捡|关|开|看|喊|哭|笑|跪|倒|冲|停|扑|滚|爬|躲|敲|撞|挥手|指)", cut.beat_goal)
        while len(pool) < k and zh_verbs:
            pool.append(f"she {zh_verbs.pop(0)} as the beat continues")
    subject_hint = ""
    m = re.search(r"\b(a|an|the)\s+[a-z]+\s+(girl|woman|man|farmer|dog|figure|child|group|crowd)", (head or "")[:60])
    if m:
        subject_hint = f"The {m.group(2)}"
    # 未进 pool 的剩余状态从句独立池：每段至多取一个、按序弹出，
    # 用尽后走变体句——禁止相邻段取同一句（v2.0.1 修复：旧实现全部取 states[0]）。
    state_pool = [s for s in states if s not in pool][::-1]
    variants = ["holds still as the moment lands",
                "settles into the final pose",
                "holds the pose"]
    segs: list[dict[str, Any]] = []
    acc = 0.0
    state = _state_brief(cut.state_from)
    idx = 0
    for i, cnt in enumerate(counts):
        d = duration * (cnt / n_actions) if n_actions else duration / k
        if i == k - 1:
            d = duration - acc            # 末段收尾：段和恒=整镜
        start, end = acc, acc + d
        acc = end
        out_state = (_state_brief(cut.state_out) if i == k - 1
                     else f"{cut.beat_goal or '段'}完成后接续下一段")
        # 本段负责的动作用该段分配到的所有从句表达（counts[i] 个），
        # 保证动作从句全部落段、不丢弃（旧实现只写 pool[i] 丢后续动作）。
        seg_acts = pool[idx:idx + cnt]
        idx += cnt
        if i == 0:
            seg_text = (head + (", " + ", ".join(seg_acts) if seg_acts else ""))
        else:
            prefix = subject_hint or "The figure"
            if seg_acts:
                seg_text = f"{prefix}, " + ", ".join(seg_acts)
            elif state_pool:
                seg_text = f"{prefix}, " + state_pool.pop(0)
            else:
                seg_text = f"{prefix}, " + variants[i % len(variants)]
        segs.append({
            "text": _cap_seg(seg_text),
            "duration_sec": round(d, 2),
            "phase": phases[i],
            "beat": cut.beat_goal or cut.shot or "",
            "time_range": [round(start, 2), round(end, 2)],
            "camera_phase": cut.camera or "",
            "state_in": state,
            "state_out": out_state,
            "keyframe": i == k - 1,
            "frame_hint": "last" if i == k - 1 else None,
            "source": "fallback",   # 兜底段=动作组语义拆分（段=独立动作句，非截断复制）
        })
        state = out_state
    return segs


def _compile_visual_bible(vb: dict[str, Any] | None) -> dict[str, Any]:
    """把业务层 visual_bible 翻译成下游 Plan 契约。

    业务层（StoryBible）用的是 style_keywords / main_colors / do_dont /
    reference_styles；下游 v10.2 Plan 要的是 medium / film_stock / lens /
    palette / style_ref_en 等摄影槽位。两者字段不同，不能直接透传，否则
    plan.json 会在 gate 的 schema 校验里整份作废。
    """
    data = dict(_DEFAULT_VISUAL_BIBLE)
    data["forbidden"] = list(_DEFAULT_VISUAL_BIBLE["forbidden"])
    if not vb:
        return data

    def _texts(value: Any) -> list[str]:
        if value is None:
            return []
        if isinstance(value, str):
            return [value.strip()] if value.strip() else []
        if isinstance(value, dict):
            return [str(v).strip() for v in value.values() if str(v).strip()]
        if isinstance(value, (list, tuple, set)):
            return [str(v).strip() for v in value if str(v).strip()]
        return [str(value).strip()]

    # style_keywords + reference_styles + DO 规则都进 style_ref_en 逃生舱；
    # DON'T 追加到 forbidden，保留契约基线，不替换。
    style_bits: list[str] = []
    for key in ("style_keywords", "reference_styles"):
        style_bits.extend(_texts(vb.get(key)))
    do_dont = vb.get("do_dont")
    if isinstance(do_dont, dict):
        style_bits.extend(_texts(do_dont.get("DO")))
        data["forbidden"].extend(_texts(do_dont.get("DON'T")))
    else:
        style_bits.extend(_texts(do_dont))
    if style_bits:
        data["style_ref_en"] = ", ".join(dict.fromkeys(style_bits))

    colors = _texts(vb.get("main_colors"))
    if colors:
        data["palette"] = ", ".join(colors)
    return data


_DEFAULT_VISUAL_BIBLE = {
    "medium": "photorealistic cinematic film still",
    "film_stock": "Kodak Vision3 500T",
    "lens": "35mm",
    "lighting": "soft cinematic key light with subtle warm rim light on hair and shoulders, "
               "gentle controlled shadow falloff, clean highlight-to-shadow transition",
    "palette": "restrained cinematic palette derived from the story setting, desaturated "
               "highlights, no pure black in costume",
    "texture": "fine 35mm film grain, subtle halation in highlights, natural cloth and "
               "leather texture, real skin with visible pores",
    "contrast": "moderate cinematic contrast, soft defined highlights, deep but open shadows, "
                "preserved black levels without crushed blacks",
    "skin_tone": "natural healthy skin with visible pores and natural sheen, no plastic "
                 "airbrush, no over-smoothing",
    "motion_character": "stable locked-off frame with minimal handheld breathing, "
                        "barely perceptible micro-motion",
    "depth_style": "moderate depth of field, sharp focus on the intended subject with "
                   "softly blurred clean background",
    "style_ref_en": None,
    "forbidden": [
        "anime", "cartoon", "illustration", "painting", "manga", "comic",
        "3d render", "cgi", "chibi", "cel shading", "line art", "watercolor",
        "sketch", "plastic skin", "anime style", "manga style", "anime art",
        "cartoon style", "illustration style", "comic style", "cel-shaded",
        "toon", "动漫", "卡通", "漫画", "插画", "手绘", "赛璐璐", "三渲二",
    ],
}

NARRATION_CPS = 5.0

SIZE_MAP = {
    "extreme-wide": "extreme_wide",
    "wide": "wide",
    "medium": "medium",
    "close": "close_up",
    "extreme-close": "extreme_close_up",
}

CAMERA_MAP = {
    "Static Shot": None,
    "Push In": "push_in",
    "Pull Out": "pull_out",
    "Zoom In": "zoom_in",
    "Zoom Out": "zoom_out",
    "Pan Left": "pan_left",
    "Pan Right": "pan_right",
    "Truck Left": "truck_left",
    "Truck Right": "truck_right",
    "Tilt Up": "tilt_up",
    "Tilt Down": "tilt_down",
    "Pedestal Up": "pedestal_up",
    "Pedestal Down": "pedestal_down",
    "Arc Shot": "arc_shot",
    "Tracking Shot": "tracking_shot",
    "Shake Slightly": "shake_slightly",
    "Shake Strongly": "shake_strongly",
    "POV": "pov",
    "Roll Clockwise": "roll_clockwise",
    "Roll Counterclockwise": "roll_counterclockwise",
}

FACING_MAP = {
    "north": "away",
    "south": "toward_camera",
    "west": "profile_left",
    "east": "profile_right",
    "northeast": "away",
    "northwest": "away",
    "southeast": "toward_other",
    "southwest": "toward_other",
}

POSITIONS = ["far_left", "left", "center_left", "center", "center_right", "right", "far_right"]


# --------------------------------------------------------------------------
# 小工具
# --------------------------------------------------------------------------
def _dump(model: Any) -> dict[str, Any]:
    return model.model_dump(mode="json", exclude_none=False)


def _write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def _bounds(item_plan: list[float]) -> tuple[float, float, float, float]:
    x0, y0, x1, y1 = item_plan
    return min(x0, x1), min(y0, y1), max(x0, x1), max(y0, y1)


def _place_xy(place: StagingPlace, st: StagingScene,
              loc_items: dict[str, Any]) -> tuple[float, float]:
    if place.item is None or place.item not in loc_items:
        return 0.5, 0.5
    x0, y0, x1, y1 = _bounds(loc_items[place.item].plan)
    x, y = (x0 + x1) / 2, (y0 + y1) / 2
    side = place.side or "on"
    if side == "west":
        x = x0 - 0.06
    elif side == "east":
        x = x1 + 0.06
    elif side == "north":
        y = y0 - 0.06
    elif side == "south":
        y = y1 + 0.06
    return max(0.0, min(1.0, x)), max(0.0, min(1.0, y))


def _positions(st: StagingScene, location: Any, chars: list[str]) -> dict[str, dict[str, Any]]:
    items = {x.name: x for x in (location.items if location else [])}
    places = {p.id: p for p in st.places}
    xy: dict[str, tuple[float, float]] = {}
    for cid in chars:
        p = places.get(cid)
        xy[cid] = _place_xy(p, st, items) if p and p.present else (0.5, 0.5)
    order = sorted(chars, key=lambda c: xy[c][0])
    out: dict[str, dict[str, Any]] = {}
    n = len(order)
    for i, cid in enumerate(order):
        if n == 1:
            pos = "center"
        elif n == 2:
            pos = "center_left" if i == 0 else "center_right"
        elif n == 3:
            pos = ["left", "center", "right"][i]
        elif n == 4:
            pos = ["left", "center_left", "center_right", "right"][i]
        else:
            idx = round(i * (len(POSITIONS) - 1) / max(1, n - 1))
            pos = POSITIONS[idx]
        p = places.get(cid)
        y = xy[cid][1]
        depth = "foreground" if y > 0.66 else ("background" if y < 0.34 else "midground")
        facing = FACING_MAP.get(p.facing or "", "toward_other") if p else "toward_other"
        out[cid] = {"pos": pos, "depth": depth, "facing": facing,
                    "note": (p.posture if p and p.posture else "")}
    return out


_EMOTION_EN = {
    "不甘": "defiant, unwilling",
    "恐惧": "fearful",
    "害怕": "fearful",
    "咬牙": "determined, gritted teeth",
    "下定": "determined",
    "震撼": "shocked, trembling",
    "颤声": "trembling voice, shaken",
    "压住恐惧": "controlled fear, urgent",
    "快速判断": "alert, thinking fast",
    "嘶吼": "shouting, fierce",
    "命令": "commanding, authoritative",
    "催促": "urgent, impatient",
    "冷沉": "cold, composed",
    "笃定": "confident, certain",
    "压低": "low voice, tense",
    "疲惫": "exhausted",
    "警觉": "alert",
    "痛苦": "in pain",
    "愤怒": "angry",
    "悲伤": "sad",
    "坚定": "resolute",
    "震惊": "shocked",
    "温柔": "gentle",
    "焦虑": "anxious",
    "紧张": "tense",
    "疑惑": "confused",
    "冷": "cold, aloof",
    "笑": "smiling",
}


def _expression_en(text: str) -> str:
    """把中文表演提示翻成图像模型能吃的英文表情短语（确定性映射，不调模型）。"""
    raw = str(text or "").strip()
    if not raw:
        return ""
    hits = [en for zh, en in _EMOTION_EN.items() if zh in raw]
    # 去重但保持顺序
    out: list[str] = []
    for h in hits:
        if h not in out:
            out.append(h)
    return ", ".join(out) if out else "natural, emotionally restrained"


def _scene_beats(scene: ScriptScene) -> tuple[list[str], list[dict[str, Any]]]:
    actions = [(i, b.action) for i, b in enumerate(scene.beats, 1) if b.action]
    lines: list[dict[str, Any]] = []
    t = 0.0
    for i, b in enumerate(scene.beats, 1):
        if b.action:
            t += 0.0
        if b.line:
            lines.append({"i": i, "speaker": b.speaker, "text": b.line,
                          "delivery": b.delivery, "at": t})
    return [a for _, a in actions], lines


def _function(sc: ScriptScene) -> str:
    return (f"目标：{sc.goal or '推进本场目标'}；阻力：{sc.obstacle or '阻力'}；"
            f"转折：{sc.turn or '转折'}；代价：{sc.cost or '无'}；"
            f"信息：{sc.information or sc.mood or '推进剧情'}")


def _scene_speech_cps(sc: ScriptScene, base: float = 4.5) -> float:
    """P0-S2 情绪→语速：按本场台词 delivery/场 mood 情绪词推 speech_cps。

    倍数表对齐中文短剧经验（cps=每秒汉字），以 `base`（story.chars_per_second）
    为 1.0 基线，换语种/换模型时只改基线即可。
    """
    text = "".join((b.delivery or "") for b in sc.beats if b.line) + (sc.mood or "")
    if any(w in text for w in ("吼", "暴怒", "怒", "咆哮", "爆发", "嘶吼", "歇斯底里")):
        return base * 6.2 / 4.5
    if any(w in text for w in ("亢奋", "兴奋", "激动", "狂热", "高昂")):
        return base * 5.4 / 4.5
    if any(w in text for w in ("急", "焦虑", "紧张", "催促", "恐慌", "加速", "惊叫")):
        return base * 5.2 / 4.5
    if any(w in text for w in ("哽咽", "哭", "低声", "沉重", "哀", "悲", "苍白", "虚弱", "颤抖")):
        return base * 3.2 / 4.5
    return base


def _scene_type(sc: ScriptScene) -> list[str]:
    n_lines = sum(1 for b in sc.beats if b.line)
    kinds: list[str] = []
    if n_lines:
        kinds.append("dialogue")
    if any(b.action for b in sc.beats):
        kinds.append("action")
    if len(sc.characters) >= 3 and n_lines:
        kinds.append("group_dialogue")
    if not kinds:
        kinds = ["transition"]
    return kinds


# --------------------------------------------------------------------------
# plan.json
# --------------------------------------------------------------------------
def compile_plan(outline: Outline, cast: Cast, ratio: str,
                 art: Art | None = None) -> dict[str, Any]:
    role_of = {c.id: c for c in outline.characters}
    characters = []
    for c in cast.characters:
        oc = role_of.get(c.id)
        props = [p.name for p in (art.props if art else []) if p.carried_by == c.id]
        desc_cn = "；".join(x for x in [
            (oc.identity if oc else ""), (oc.appearance if oc else ""),
            "；".join(f"{w.state_id}:{w.tags}" for w in c.wardrobe_states),
        ] if x) or c.one_liner or c.name
        characters.append({
            "char_id": c.id,
            "name": c.name,
            "role": (oc.role if oc and oc.role else
                     ("主角" if c.importance == "protagonist" else "配角")),
            "status": "active",
            "desc_cn": desc_cn,
            "desc_en": c.image.prompt,
            "identity_lock": _dump(c.identity_lock),
            "wardrobe_states": [{"state_id": w.state_id, "desc_en": w.desc_en,
                                 "tags": w.tags} for w in c.wardrobe_states],
            "signature_props": props,
            "color_signature": c.color_signature,
            "voice_ref": c.voice.timbre or c.voice.prompt or (oc.voice_hint if oc else ""),
        })
    w, h = (576, 1024) if ratio == "9:16" else (1024, 576)
    # 视觉圣经优先取业务层（outline.visual_bible），未提供则用编译器默认画风。
    # 业务层字段名与下游 Plan 契约不同，必须翻译，不能直接透传。
    visual_bible = _compile_visual_bible(outline.visual_bible)
    return {
        "schema_version": "v10.3",
        "plan_version": 1,
        "scope": "series",
        "project": outline.title,
        "genre": outline.genre or "剧情",
        "logline": outline.logline,
        "target_audience": outline.audience or "短剧观众",
        "total_episodes": outline.params.episodes,
        "visual_bible": visual_bible,
        "output": {
            "aspects": {
                "enabled": [ratio],
                "primary": ratio,
                "derivation": {},
                "resolution_policy": "engine_default",
                "min_resolution": {ratio: [w, h]},
            },
            "simultaneous": False,
            "episode_sec": outline.params.target_seconds,
            "target_platform": "短剧",
        },
        "requirements": {
            "identity_fidelity": "locked",
            "wardrobe_fidelity": "high",
            "temporal_stability": "series",
            "multi_subject_strategy": "auto",
            "interpolation": "standard",
            "dubbing": "auto",
        },
        "cast_separation": {
            "co_occurrence_rule": "同框角色必须在身高、发型、服装颜色、面部特征上至少 3 维可区分；"
                                  "同框超过 3 人时主角必须位于可辨识位置",
            "rule": "同框角色两两之间至少 3 个维度不同",
            "axes": ["身高(差8cm以上)", "发型", "脸型", "年龄感", "服装颜色",
                     "身形轮廓", "标志物", "面部特征"],
            "violations": [],
        },
        "characters": characters,
        "episodes": [{"num": e.ep, "title": e.title, "summary": e.synopsis,
                      "hook": e.hook} for e in outline.episodes],
        "continuity": {
            "immutable": ["identity_lock 全字段", "signature_props", "color_signature"],
            "mutable": ["wardrobe_state", "伤势", "随身道具", "关系变化"],
            "policy": "identity_lock 锁定后全剧不改；变更必须提升 plan_version 并重出定妆图",
        },
        "hints": {},
    }


# --------------------------------------------------------------------------
# script.json
# --------------------------------------------------------------------------
def _continuity_ledger(state: CutState) -> dict[str, dict[str, Any]]:
    """把本集终态 CutState 归一成契约账本（§7.4：char_id → 规范键）。

    键名与 plan.continuity.mutable 同名：wardrobe_state / 伤势 / 随身道具。
    跨集校验要求「本集末态 == 下集初态」，没变也要原样抄一遍；
    空就用 "" 或 []（明确「无」≠「未记录」）。
    """
    props_by_char: dict[str, list[str]] = {}
    for slug, holder in sorted(state.props.items()):
        if holder and holder != "scene":
            props_by_char.setdefault(holder, []).append(slug)
    chars = set(state.wardrobe) | set(state.injuries) | set(props_by_char)
    return {
        c: {
            "wardrobe_state": state.wardrobe.get(c, ""),
            "伤势": state.injuries.get(c, ""),
            "随身道具": sorted(props_by_char.get(c, [])),
        }
        for c in sorted(chars)
    }


# 下游 LTX/LatentSync 的帧数口径：24fps、8n+1 向下取整、台词另留 2 帧余量。
# 这里必须在**分配镜头时长时**就按同一口径算下限，否则 2.61s 这类“看起来
# 够、取整后不够”的镜头会稳定在 gate 里报 time.dialogue_exceeds_shot。
_COMPILE_FPS = 24.0
_DIALOGUE_FRAME_MARGIN = 2


def _next_8n1(frames: int) -> int:
    """>= frames 的最小 8n+1（帧）。"""
    frames = max(1, int(frames))
    return ((frames - 1 + 7) // 8) * 8 + 1


def _dialogue_safe_seconds(text: str, cps: float) -> float:
    """一句台词在 8n+1 取整后仍装得下的最短镜头时长。"""
    chars = len("".join(text.split()))
    if not chars:
        return 0.0
    need = math.ceil(chars / max(cps, 0.1) * _COMPILE_FPS) + _DIALOGUE_FRAME_MARGIN
    return _next_8n1(need) / _COMPILE_FPS


def _cut_durations(board: StoryEpisodeBoard, ep: ScriptEpisode,
                   params: ScriptParams) -> dict[int, float]:
    """把切镜时长规范化到单集目标秒数，返回 `{id(cut): seconds}`。

    为什么必须在编译期做：故事层生成时只能把总时长控制在 ±15%，而下游
    `drama` 要求场景合计 == 单集目标（±0.2s）。LLM 无法精确凑出 90.00s，
    所以在**不改剧情节拍**的前提下，把差值均摊到各切镜，并保证：

    - 每镜仍在 params.min_shot_seconds–max_shot_seconds；
    - 有台词的镜头不小于该镜头认领台词的估算时长；
    - `compile_script` 与 `compile_storyboard` 共用同一套时长。
    """
    entries: list[tuple[int, StoryCut]] = [
        (seg.scene_index, cut) for seg in board.segments for cut in seg.cuts
    ]
    if not entries:
        return {}
    lower: list[float] = []
    cps = max(0.1, params.chars_per_second)
    for si, cut in entries:
        scene = ep.scenes[si - 1]
        # 只有台词是硬下限；动作拍估算的 2.5s 可以在编译期压缩到 1s，
        # 否则 LLM 写多了动作拍就永远凑不到 90s。
        # 下限同时满足两个口径：
        #   1) 连续台词的正常语速总时长；
        #   2) 单句在 24fps、8n+1 向下取整、再留 2 帧余量后仍装得下。
        # 只算 1) 会出现“2.61s 看似够、取整后只剩 57 帧”的 gate warn。
        total = 0.0
        per_line: list[float] = []
        for bi in range(cut.beats[0], cut.beats[1] + 1):
            b = scene.beats[bi - 1]
            if b.line:
                total += len("".join(b.line.split())) / cps
                per_line.append(_dialogue_safe_seconds(b.line, cps))
        need = max([total, *per_line]) if per_line else 0.0
        lower.append(max(params.min_shot_seconds,
                         min(params.max_shot_seconds, need)))

    durs = [max(lo, min(params.max_shot_seconds, round(c.seconds, 2)))
            for lo, (_, c) in zip(lower, entries)]
    target = float(ep.target_seconds)

    # 连续均摊：每轮只把差值分给还有上下限空间的镜头
    for _ in range(200):
        diff = target - sum(durs)
        if abs(diff) < 0.005:
            break
        free = [i for i, d in enumerate(durs)
                if (diff > 0 and d < params.max_shot_seconds - 1e-6)
                or (diff < 0 and d > lower[i] + 1e-6)]
        if not free:
            break
        share = diff / len(free)
        for i in free:
            durs[i] = max(lower[i],
                          min(params.max_shot_seconds, durs[i] + share))

    # 2 位小数 + 残差修正（下游全部按 2 位小数计时）
    durs = [max(math.ceil(lo * 100 - 1e-6) / 100,
                min(params.max_shot_seconds, round(d, 2)))
            for lo, d in zip(lower, durs)]
    residual = round(target - sum(durs), 2)
    for i, d in enumerate(durs):
        if abs(residual) < 0.005:
            break
        room = ((params.max_shot_seconds - d) if residual > 0
                else (d - lower[i]))
        if room <= 1e-6:
            continue
        step = math.copysign(min(abs(residual), room), residual)
        durs[i] = round(d + step, 2)
        residual = round(residual - step, 2)

    if abs(target - sum(durs)) > 0.2:
        raise ValueError(
            f"第 {ep.ep} 集无法把分镜时长规范到 {target:.1f}s："
            f"当前 {sum(durs):.2f}s，切镜数 {len(durs)}。"
            f"检查单镜时长是否大量顶到 {params.min_shot_seconds:g}s/"
            f"{params.max_shot_seconds:g}s 边界。")
    return {id(c): d for d, (_, c) in zip(durs, entries)}


def _cut_clock_map(board: StoryEpisodeBoard,
                   durs: dict[int, float] | None = None,
                   ) -> dict[tuple[int, int], tuple[float, float]]:
    """(场序, 拍序) -> 该拍所在镜头的 (绝对 start_sec, duration_sec)。"""
    clock = 0.0
    out: dict[tuple[int, int], tuple[float, float]] = {}
    for seg in board.segments:
        for cut in seg.cuts:
            dur = (durs or {}).get(id(cut)) or round(cut.seconds, 2)
            for bi in range(cut.beats[0], cut.beats[1] + 1):
                out.setdefault((seg.scene_index, bi), (round(clock, 2), dur))
            clock += dur
    return out


def _scene_uid(index: int) -> str:
    """编译产物的 scene_id 必须全集唯一。

    剧本层的 `ScriptScene.id` 是**地点 id**（来自 outline/art），同一地点
    可以在多场复用；而 v10.2 的 `scene_id` 是**场次 id**，gate/drama 都用
    字典按它索引。这里按场次序号重新编号，地点信息仍用 `sc.id` 查 art。
    """
    return f"S{index}"


def compile_script(ep: ScriptEpisode, outline: Outline, cast: Cast,
                   art: Art, staging: Staging, params: ScriptParams,
                   ratio: str, board: StoryEpisodeBoard | None = None) -> dict[str, Any]:
    cast_by = {c.id: c for c in cast.characters}
    # staging.scene 是场次序号，天然唯一；staging.scene_id 是地点 id，可能重复。
    st_by_scene = {s.scene: s for s in staging.scenes if s.ep == ep.ep}
    loc_by_scene = {l.scene_id: l for l in staging.locations}
    durs = _cut_durations(board, ep, params) if board else {}
    shot_at_beat = _cut_clock_map(board, durs) if board else {}
    scene_sec: dict[int, float] = {}
    if board:
        # 一个长场可能被拆成多段（同 scene_index），这里必须累加；
        # 直接赋值会只剩最后一段的时长，gate 的 scene_sum 立刻对不上。
        for seg in board.segments:
            scene_sec[seg.scene_index] = round(
                scene_sec.get(seg.scene_index, 0.0)
                + sum(durs.get(id(c), round(c.seconds, 2)) for c in seg.cuts), 2)
    scenes = []
    for si, sc in enumerate(ep.scenes, 1):
        st = st_by_scene.get(si) or StagingScene(
            ep=ep.ep, scene=si, scene_id=sc.id, cast=[], places=[])
        positions = _positions(st, loc_by_scene.get(sc.id), sc.characters)
        actions, _ = _scene_beats(sc)
        action = "；".join(actions)
        if len(action) < 20:
            action = (action + f"；{sc.location}{sc.time}，{sc.mood}，"
                      f"人物保持可拍的连续动作。").strip("；")
        # scene.speech_cps 是给渲染端/TTS 的情绪参考值（悲伤降速、激动提速），
        # 不参与“镜头装不装得下”的容量判定。gate 的 time.line_too_long 明确
        # 按标准语速（story.chars_per_second）算，分镜镜头时长也按标准语速
        # 生成；timing_sec 必须同口径，否则激动场次（cps>标准）会被判台词超长。
        base_cps = max(0.1, params.chars_per_second if params else 4.5)
        scene_cps = _scene_speech_cps(sc, base_cps)
        beat_duration = round(sum(
            (params.action_seconds if b.action else
             len("".join(b.line.split())) / scene_cps)
            for b in sc.beats), 2)
        # 编译期已把分镜总时长规范到目标值；剧本场景时长必须与分镜同源，
        # 否则 gate 的 scene_sum / episode_sum 会对不上。
        duration = scene_sec.get(si, beat_duration)
        clock = 0.0
        beat_starts: dict[int, float] = {}
        lines = []
        for bi, b in enumerate(sc.beats, 1):
            beat_starts[bi] = round(clock, 2)
            if b.line:
                n = len("".join(b.line.split()))
                # 容量按标准语速；drama S8 用 scene.speech_cps 只判“是否偏短”，
                # 这里取两者更长的一端即可同时满足两个审计。
                dur = max(math.ceil(n / base_cps), math.ceil(n / scene_cps), 0)
                start, shot_dur = shot_at_beat.get(
                    (si, bi), (round(clock, 2), params.action_seconds))
                # at_sec 是**全集绝对时间**（与 board.start_sec 同一坐标系），
                # 钉在所说镜头 start_sec + 0.5，且保证落在镜头窗口内
                at = start + min(0.5, max(0.0, shot_dur - 0.3))
                lines.append({
                    "speaker": b.speaker,
                    "text": b.line,
                    "emotion": b.delivery,
                    "timing_sec": dur,
                    "at_sec": round(at, 2),
                })
                clock += n / base_cps
            elif b.action:
                clock += params.action_seconds
        # v10.3：旁白只保留一份 —— 唯一权威在 storyboard.shots[].narration。
        # script.scenes[].narration 必须留空，避免与 shot 级旁白时间不一致
        #（历史产物曾出现同文本 script 0.40s / shot 0.30s 的 0.1s 漂移）。
        narration = []
        # v10.2 的 scene.wardrobe_state 是「场级单态」，gate 要求同场所有
        # subjects 与它完全一致。若角色在本场拍级账本里换装（入水/换衣/
        # 穿鞋），一个场级值必然与前后镜头冲突。此时**省略该角色**：
        # 逐镜 subjects 已携带正确的 state_id，gate 仍会校验它在角色库里；
        # 场级 map 只保留本场不变的角色，避免用一个错误值卡住整集。
        wardrobe: dict[str, str] = {}
        for cid, wid in sc.wardrobe.items():
            if cid not in cast_by:
                continue
            states = {wid}
            for bi in range(1, len(sc.beats) + 1):
                got = script_wardrobe_at(sc, bi).get(cid)
                if got:
                    states.add(got)
            if len(states) == 1:
                wardrobe[cid] = wid
        scenes.append({
            "scene_id": _scene_uid(si),
            "location": sc.location or (art.scenes[0].name if art.scenes else ""),
            "time": sc.time,
            "action": action,
            "scene_type": _scene_type(sc),
            "space_map": positions,
            "character_flags": {},
            "chars_on_screen": sc.characters,
            "wardrobe_state": wardrobe,
            "speech_cps": scene_cps,
            "lines": lines,
            "narration": narration,
            "transition_in": (sc.transition_in.model_dump(mode="json")
                              if sc.transition_in is not None else None),
            "mood": sc.mood,
            "duration_sec": duration,
            "beat": sc.beat,
            "function": _function(sc),
        })
    return {
        "schema_version": "v10.3",
        "series_ref": "plan.json",
        "episode": ep.ep,
        "title": ep.title,
        "aspects_override": None,
        "requirements": {},
        "scenes": scenes,
        "continuity_ledger": _continuity_ledger(board.state_out) if board else {},
        "hook_chain": {"from_ep": max(0, ep.ep - 1), "to_ep": ep.ep,
                       "thread": ep.hook},
        "hints": {},
    }


# --------------------------------------------------------------------------
# storyboard.json / director.json
# --------------------------------------------------------------------------
def _director_setup_payoff(board: StoryEpisodeBoard, art: Art,
                           shots: list[dict[str, Any]]) -> list[dict[str, Any]]:
    cuts = [c for seg in board.segments for c in seg.cuts]
    shot_of_cut = list(zip(cuts, shots))
    out: list[dict[str, Any]] = []
    used: set[str] = set()
    for prop in art.props:
        hits = [(sh["shot_id"], c) for c, sh in shot_of_cut
                if prop.id in c.props]
        if len(hits) < 2:
            continue
        a, b = hits[0][0], hits[-1][0]
        tokens = OBJECT_LEXICON.get(prop.name, ())
        obj = prop.name if tokens and all(
            any(t in (sh["prompt"] or "").lower() for t in tokens)
            for sh in shots if sh["shot_id"] in (a, b)) else ""
        out.append({
            "id": f"SP{len(out) + 1}",
            "object": obj,
            "setup": f"{prop.name}第一次出现",
            "setup_shot": a,
            "payoff": f"{prop.name}最后一次出现并完成叙事功能",
            "payoff_shot": b,
        })
        used.update((a, b))
    # 道具不够 3 件时，用“开场/收束”镜头补足结构位
    if len(out) < 3 and len(shots) >= 4:
        pairs = [(shots[0]["shot_id"], shots[-1]["shot_id"]),
                 (shots[1]["shot_id"], shots[-2]["shot_id"]),
                 (shots[len(shots) // 2]["shot_id"], shots[-3]["shot_id"])]
        for a, b in pairs:
            if len(out) >= 3:
                break
            if a == b or any(x["setup_shot"] == a and x["payoff_shot"] == b
                             for x in out):
                continue
            out.append({"id": f"SP{len(out) + 1}", "object": "",
                        "setup": "开场状态", "setup_shot": a,
                        "payoff": "收束状态", "payoff_shot": b})
    return out


def _multi_subject_requirements(cut: StoryCut) -> dict[str, str]:
    """同框 ≥2 人时必须在镜头级写死策略（契约 §4.4）。

    剧本层没有“多人策略”这个创作概念，所以这里做确定性兜底：
    - 显式给了非 auto 值 → 原样采用；
    - 允许同框多张正脸（群像/定场，`allow_multi_face=True`）→ hybrid：
      主角色逐人保脸、背景人群整体生成；
    - 其余双人/多人同框 → joint：一个画幅一次成像。
    `auto` 不再向下游输出，避免适配层静默降级。
    """
    if len(set(cut.characters)) < 2:
        return {}
    strategy = (cut.multi_subject_strategy or "").strip()
    if strategy and strategy != "auto":
        return {"multi_subject_strategy": strategy}
    return {"multi_subject_strategy": "hybrid" if cut.allow_multi_face else "joint"}


def _shot_requirements(cut: StoryCut, on_screen_lines: list[dict[str, str]]) -> dict[str, str]:
    req: dict[str, str] = {}
    if on_screen_lines:
        req["dubbing"] = "lipsync"
    req.update(_multi_subject_requirements(cut))
    return req


def compile_storyboard(ep: ScriptEpisode, board: StoryEpisodeBoard, art: Art,
                       cast: Cast, staging: Staging, outline: Outline,
                       params: ScriptParams, ratio: str) -> tuple[dict[str, Any], dict[str, Any]]:
    scene_meta = {s.id: s for s in art.scenes}
    st_by_scene = {s.scene: s for s in staging.scenes if s.ep == ep.ep}
    loc_by_scene = {l.scene_id: l for l in staging.locations}
    shots: list[dict[str, Any]] = []
    shot_by_cut: dict[int, dict[str, Any]] = {}
    durs = _cut_durations(board, ep, params)
    # v10.3 旁白锚点：预扫描每镜 [start_sec, end_sec]（与下方主循环同一
    # 顺序/口径），供 within_shot / voiceover_span 判定与跨镜跨度计算。
    timeline: dict[int, tuple[float, float]] = {}
    _tl = 0.0
    for _seg in board.segments:
        for _cut in _seg.cuts:
            _d = durs.get(id(_cut), round(_cut.seconds, 2))
            timeline[id(_cut)] = (_tl, _tl + _d)
            _tl += _d
    clock = 0.0
    n = 0
    for seg in board.segments:
        si = seg.scene_index
        sc = ep.scenes[si - 1]
        st = st_by_scene.get(si) or StagingScene(
            ep=ep.ep, scene=si, scene_id=sc.id, cast=[], places=[])
        positions = _positions(st, loc_by_scene.get(sc.id), sc.characters)
        for cut in seg.cuts:
            n += 1
            shot_id = f"SH{n:02d}"
            size = SIZE_MAP[cut.size]
            move = CAMERA_MAP[cut.camera]
            # 本 cut 认领的拍里有哪些台词。分镜的 beats 是**唯一**的归属依据：
            # 不按时间猜，避免"这句到底属于上一镜还是下一镜"的漂移。
            cut_lines: list[dict[str, str]] = []
            cut_narration: list[dict[str, Any]] = []
            for bi in range(cut.beats[0], cut.beats[1] + 1):
                if 1 <= bi <= len(sc.beats):
                    b = sc.beats[bi - 1]
                    if b.line:
                        cut_lines.append({
                            "speaker": b.speaker,
                            "text": b.line,
                            "delivery": b.delivery or sc.mood,
                        })
                    for nl in sc.narration:
                        if nl.beat != bi:
                            continue
                        n_chars = len("".join(nl.text.split()))
                        cut_narration.append({
                            "kind": nl.kind,
                            "text": nl.text,
                            "speaker": nl.speaker,
                            "delivery": nl.delivery,
                            "timing_sec": max(1.0, math.ceil(n_chars / NARRATION_CPS)),
                        })
            # 台词句数上限由引擎能力决定：None=不限制；单人口型引擎可配 1。
            if (params.max_lines_per_shot is not None
                    and len(cut_lines) > params.max_lines_per_shot):
                raise ValueError(
                    f"{sc.id} 第 {cut.beats[0]}-{cut.beats[1]} 拍所在切镜有 "
                    f"{len(cut_lines)} 句台词（{', '.join(x['speaker'] for x in cut_lines)}），"
                    f"当前引擎上限 {params.max_lines_per_shot} 句/镜；"
                    "请把这几拍拆成独立切镜")
            delivery_by_char = {ln["speaker"]: ln["delivery"] for ln in cut_lines}
            on_screen_lines = [ln for ln in cut_lines if ln["speaker"] in cut.characters]
            speakers = {ln["speaker"] for ln in on_screen_lines}
            # v10.2：无人物镜区分物件/环境。物件镜（靴子、手掌）和远景空镜
            # 本来就没有人脸，不该被"人脸占比"口径判成首帧偏差；也便于分镜阶段
            # 明确"这一镜的主体到底是什么"。
            if cut.characters:
                subject_type = "character"
            elif size in ("extreme_wide", "wide"):
                subject_type = "environment"
            else:
                subject_type = "object"
            subjects = []
            for cid in cut.characters:
                sub = next((x for x in cut.subjects if x.char == cid), None)
                wardrobe = (sub.wardrobe_state if sub else "") or sc.wardrobe.get(cid, "")
                expr = (sub.expression if sub else "") or _expression_en(
                    delivery_by_char.get(cid) or sc.mood)
                subjects.append({
                    "char": cid,
                    "wardrobe_state": wardrobe,
                    "pose": sub.pose if sub else "",
                    "expression": expr,
                    # v10.2：开口动作是 LatentSync 的前提，它只能放大已有的嘴部动作。
                    "speaking": cid in speakers,
                })
            frame = positions
            framing = {}
            # 双画幅必须都写：drama_gate B3 要求 framing 同时含 9:16 与 16:9
            #（适配层用双画幅做横版封面/多端适配），保持接入不变不可只写单画幅。
            for aspect in ("9:16", "16:9"):
                framing[aspect] = {
                    "shot_size": size,
                    "subject_type": subject_type,
                    "composition": (f"{aspect}：{cut.frame}"
                                    + ("，左右留出空间" if aspect == "16:9" else "")),
                    "overlap_order": [],
                    "subjects": [{
                        "char": cid,
                        "pos": frame.get(cid, {}).get("pos", "center"),
                        "height_level": "mid",
                        "depth": frame.get(cid, {}).get("depth", "midground"),
                        "facing": frame.get(cid, {}).get("facing", "toward_other"),
                        "occludes": [],
                    } for cid in cut.characters],
                    # P0.5 契约 §6.5：camera_recipe 是 Framing（per-aspect）槽位，
                    # 由 compile 从 prompt_en 确定性提取（lens_mm/depth_of_field/
                    # light_key/light_quality），实现层按此读配方。
                    "camera_recipe": _extract_camera_recipe(cut.prompt_en),
                }
            duration = durs.get(id(cut), round(cut.seconds, 2))
            # v10.3 旁白锚点：每条旁白都带 shot_id / anchor_mode /
            # allow_span_shots / allow_cross_scene / max_shift_sec。
            # within_shot：at_sec+timing_sec 完整落在锚镜内（留 0.08s 空隙）；
            # 装不下则 voiceover_span，只在同一场内向后累加镜头直到覆盖。
            for item in cut_narration:
                item["shot_id"] = shot_id
                at = clock + min(0.3, max(0.0, duration - 0.2))
                item["at_sec"] = round(at, 2)
                t_start, t_end = timeline[id(cut)]
                need_end = at + item["timing_sec"]
                if need_end <= t_end - 0.08:
                    item["anchor_mode"] = "within_shot"
                    item["allow_span_shots"] = 1
                    item["allow_cross_scene"] = False
                    item["max_shift_sec"] = 0.5
                else:
                    span, acc = 1, t_end
                    for cut2 in seg.cuts:
                        s0, e0 = timeline[id(cut2)]
                        if e0 <= t_end:
                            continue
                        span += 1
                        acc = e0
                        if acc >= need_end:
                            break
                    item["anchor_mode"] = "voiceover_span"
                    item["allow_span_shots"] = span
                    item["allow_cross_scene"] = False
                    item["max_shift_sec"] = 1.0
            shot = {
                "shot_id": shot_id,
                "scene_id": _scene_uid(si),
                "act_beat": sc.beat,
                "prompt": _clean_prompt_camera(cut.prompt_en),
                "framing": framing,
                "subjects": subjects,
                "ref_characters": cut.characters,
                # 画内台词 = 口型诉求；画外台词只做声音，不做口型。
                # 同框 ≥2 人时同时写显式 multi_subject_strategy（§4.4）。
                "requirements": _shot_requirements(cut, on_screen_lines),
                "aspect_overrides": None,
                "motion": cut.motion,
                "camera": "static" if move is None else "tripod",
                "camera_move": move,
                "audio": "；".join(
                    f"{'同期' if ln['speaker'] in cut.characters else '画外'}"
                    f"（{ln['speaker']}，"
                    f"{'画内' if ln['speaker'] in cut.characters else '无口型'}）："
                    f"{ln['text']}"
                    for ln in cut_lines
                ),
                "narration": cut_narration,
                "transition_in": (
                    seg.transition_in.model_dump(mode="json")
                    if cut is seg.cuts[0] and seg.transition_in is not None
                    else None),
                "sfx": cut.sfx,
                "effect": "",
                "emotion": (cut_lines[0]["delivery"] if cut_lines else
                            (subjects[0]["expression"] if subjects else sc.mood)),
                "keyframe": cut.frame,
                "track": None,
                "stage_override": None,
                "start_sec": round(clock, 2),
                "duration_sec": duration,
                "duration_basis": _duration_basis_text(cut, params, size, duration, cut_lines),
                "end_sec": round(clock + duration, 2),
                "verify_point": None,
                "segments": (
                    [s.model_dump(mode="json") for s in cut.segments]
                    if cut.segments
                    else _fallback_segments(
                        cut, duration,
                        _count_actions(f"{cut.prompt_en} {cut.motion}"))
                ),
                # A5：hints 定位为 C 层引擎参数；coverage/cut_reason/purpose/information
                # 本是导演业务信息，权威副本在 director.json（shots 同键），此处保留仅为
                # 兼容既有适配层读取——适配层应忽略或用 director 覆盖，勿当作引擎推导输入。
                "hints": {
                    "coverage": cut.coverage,
                    "cut_reason": cut.cut_reason,
                    "purpose": cut.purpose or f"完成{sc.id}的这一拍",
                    "information": cut.information or sc.information or sc.mood,
                    "scene_shot": si,
                    **({"allow_multi_face": True} if cut.allow_multi_face else {}),
                },
            }
            shots.append(shot)
            shot_by_cut[id(cut)] = shot
            clock += duration

    # 下游按 shot.scene_id 查环境锚点。scene_id 已按场次重新编号，
    # 因此这里必须用同一套编号；同一地点的多场复用同一个英文锚点。
    scene_anchors: dict[str, str] = {}
    for si, sc in enumerate(ep.scenes, 1):
        a = scene_meta.get(sc.id)
        scene_anchors[_scene_uid(si)] = (
            a.image.prompt if a is not None else sc.location)
    story_json = {
        "schema_version": "v10.3",
        "episode": ep.ep,
        "style_suffix": "",
        "shots": shots,
        "scene_anchors": scene_anchors,
        "hints": {},
    }

    d_scenes = []
    for si, sc in enumerate(ep.scenes, 1):
        ids = [shot_by_cut[id(c)]["shot_id"]
               for seg in board.segments if seg.scene_index == si
               for c in seg.cuts if id(c) in shot_by_cut]
        prompts = [s["prompt"].lower() for s in shots if s["shot_id"] in ids]
        objects = []
        for prop in art.props:
            if prop.id not in sc.props:
                continue
            tokens = OBJECT_LEXICON.get(prop.name)
            if tokens and any(any(t in p for t in tokens) for p in prompts):
                objects.append(prop.name)
        d_scenes.append({
            "scene_id": _scene_uid(si),
            "goal": sc.goal, "obstacle": sc.obstacle, "turn": sc.turn,
            "cost": sc.cost, "information": sc.information,
            "objects": objects,
            "shots": ids,
            "transition_in": (sc.transition_in.model_dump(mode="json")
                              if sc.transition_in is not None else None),
            "narration": [],
            # v10.3：旁白唯一权威在 storyboard.shots[].narration；
            # director 场景级不再携带旁白副本，避免多份副本漂移。
        })
    # A3：从场次节拍定位爽点/代价镜头，填 power_rule 的 used_at / limited_at。
    # 爽点（主角用金手指翻盘）落在「高潮/翻盘/转折」节拍的场；代价/受挫（能力受限）
    # 落在「代价/受挫」节拍或 cost 非空的场。
    used_at: list[str] = []
    limited_at: list[str] = []
    for _si, _sc in enumerate(ep.scenes, 1):
        _ids = d_scenes[_si - 1]["shots"] if _si - 1 < len(d_scenes) else []
        if not _ids:
            continue
        _beat = (_sc.beat or "").lower()
        if "高潮" in _beat or "翻盘" in _beat or _beat == "转折":
            used_at.extend(_ids)
        if "代价" in _beat or "受挫" in _beat or (_sc.cost and _sc.cost != "无"):
            limited_at.extend(_ids)
    director = {
        "schema_version": "director-v1",
        "episode": ep.ep,
        "title": ep.title,
        "logline": outline.logline,
        "spine": _dump(outline.spine),
        "power_rule": {
            "name": outline.power_rule.name,
            "sees": outline.power_rule.sees,
            "cannot": outline.power_rule.cannot,
            "cost": outline.power_rule.cost,
            "used_at": used_at,
            "limited_at": limited_at,
        },
        "setup_payoff": _director_setup_payoff(board, art, shots),
        "scenes": d_scenes,
        "shots": [{
            "shot_id": s["shot_id"],
            "coverage": s["hints"]["coverage"],
            "cut_reason": s["hints"]["cut_reason"],
            "purpose": s["hints"]["purpose"],
            "information": s["hints"]["information"],
            "narration": s.get("narration", []),
            "transition_in": s.get("transition_in"),
        } for s in shots],
    }
    return story_json, director


# --------------------------------------------------------------------------
# 资产清单 & 提示词文档
# --------------------------------------------------------------------------
def compile_assets(outline: Outline, cast: Cast, art: Art,
                   scripts: list[ScriptEpisode] | None = None) -> dict[str, Any]:
    characters = {}
    for c in cast.characters:
        characters[c.id] = {
            "name": c.name,
            "identity_prompt": c.image.prompt,
            "identity_anchor": identity_anchor(c),
            "negative_prompt": c.image.negative_prompt,
            "sheet": c.image.sheet,
            "wardrobe_states": {w.state_id: w.desc_en for w in c.wardrobe_states},
            "voice": _dump(c.voice),
            "color_signature": c.color_signature,
            "pose_library": list(c.pose_library),
            "expression_library": list(c.expression_library),
            "views": ["identity", "side"],
        }
    scenes = {}
    for s in art.scenes:
        scenes[s.id] = {
            "name": s.name,
            "plate_prompt": "，".join([s.image.prompt] + [l.prompt for l in s.lighting[:1]]),
            "negative_prompt": s.image.negative_prompt,
            "anchors": [a.desc for a in s.anchors],
            "lighting": {l.state: l.prompt for l in s.lighting},
            "variant_of": s.variant_of,
        }
    props = {}
    for p in art.props:
        props[p.id] = {
            "name": p.name,
            "prompt": p.image.prompt,
            "negative_prompt": p.image.negative_prompt,
            "states": {x.state: x.prompt for x in p.states},
            "scale": p.scale,
            "carried_by": p.carried_by,
        }
    # 编译后的 scene_id 是**场次号**（S1..Sn，每集重新计数），
    # story_assets.scenes 的 key 是**地点资产 id**（art scene id，全剧复用）。
    # 两者不是同一个命名空间：同一地点可在多场出现，不同地点也可能让
    # 场次号与资产 id 撞名（例如本集 S3=主车厢，而资产 S3=铁门）。
    # 所以必须显式给下游一张映射表，不能靠同名 key 猜。
    scene_refs: dict[str, dict[str, str]] = {}
    for ep in scripts or []:
        scene_refs[str(ep.ep)] = {
            _scene_uid(si): sc.id for si, sc in enumerate(ep.scenes, 1)
        }
    return {
        "schema_version": "story-assets-v1",
        "source": outline.source,
        "characters": characters,
        "scenes": scenes,
        # 场次 → 地点资产。下游按 `scene_refs[集号][script.scene_id]`
        # 取 `scenes[资产 id]`，再用其 plate_prompt / variant_of 生成或复用 plate。
        "scene_refs": scene_refs,
        "props": props,
    }


def _markdown(outline: Outline, cast: Cast, art: Art,
              scripts: list[ScriptEpisode], boards: StoryStoryboard,
              params: ScriptParams | None = None) -> str:
    sp = params or ScriptParams()
    by_id = {c.id: c for c in cast.characters}
    lines = [f"# {outline.title}", "", outline.logline, "",
             f"- 类型：{outline.genre}", f"- 集数：{outline.params.episodes}",
             f"- 单集：{outline.params.target_seconds:.0f}s",
             f"- 画幅：{outline.params.aspect_ratio}", "",
             "## 角色", ""]
    for c in cast.characters:
        lines.append(f"- **{c.id} {c.name}**：{c.one_liner or c.image.prompt}")
    lines += ["", "## 场景", ""]
    for s in art.scenes:
        lines.append(f"- **{s.id} {s.name}**：{s.image.prompt}")
    if art.props:
        lines += ["", "## 道具", ""]
        for p in art.props:
            lines.append(f"- **{p.id} {p.name}**（{p.scale}）：{p.image.prompt}")
    for ep in scripts:
        board = next((b for b in boards.episodes if b.ep == ep.ep), None)
        n_cut = sum(len(sg.cuts) for sg in board.segments) if board else 0
        lines += ["", f"## 第 {ep.ep} 集 {ep.title}", "",
                  f"目标 {ep.target_seconds:.0f}s / 预估 {ep.duration_with(sp):.1f}s / "
                  f"{len(ep.scenes)} 场 / {n_cut} 镜", ""]
        if not board:
            continue
        k = 0
        for seg in board.segments:
            sc = ep.scenes[seg.scene_index - 1]
            lines.append(f"### 第 {seg.scene_index} 场 {sc.id} · {sc.location}")
            for cut in seg.cuts:
                k += 1
                names = "、".join(by_id[x].name if x in by_id else x
                                 for x in cut.characters) or "空镜"
                lines.append(f"- `SH{k:02d}` {cut.seconds:.1f}s {cut.size} "
                             f"[{cut.coverage}] {names}：{cut.frame}")
                lines.append(f"  - EN: {cut.prompt_en}")
    return "\n".join(lines) + "\n"


# --------------------------------------------------------------------------
# 总入口
# --------------------------------------------------------------------------
def compile_all(outline: Outline, cast: Cast, art: Art,
                scripts: list[ScriptEpisode], staging: Staging,
                boards: StoryStoryboard, out_dir: str | Path,
                params: ScriptParams | None = None) -> dict[str, Path]:
    p = params or ScriptParams()
    out = Path(out_dir)
    ratio = outline.params.aspect_ratio
    written: dict[str, Path] = {}

    plan = compile_plan(outline, cast, ratio, art)
    plan_path = out / "external" / "plan.json"
    _write_json(plan_path, plan)
    written["plan"] = plan_path

    for ep in scripts:
        board = next((b for b in boards.episodes if b.ep == ep.ep), None)
        if board is None:
            raise ValueError(f"分镜缺少第 {ep.ep} 集")
        script = compile_script(ep, outline, cast, art, staging, p, ratio, board)
        script_path = out / "external" / f"ep{ep.ep:02d}" / "script.json"
        _write_json(script_path, script)
        written[f"script_ep{ep.ep:02d}"] = script_path

        story, director = compile_storyboard(ep, board, art, cast, staging,
                                             outline, p, ratio)
        story_path = out / f"ep{ep.ep:02d}" / "storyboard.json"
        director_path = out / "external" / f"ep{ep.ep:02d}" / "director.json"
        _write_json(story_path, story)
        _write_json(director_path, director)
        written[f"storyboard_ep{ep.ep:02d}"] = story_path
        written[f"director_ep{ep.ep:02d}"] = director_path

    assets_path = out / "story_assets.json"
    _write_json(assets_path, compile_assets(outline, cast, art, scripts))
    written["assets"] = assets_path

    md_path = out / "prompts" / "story.md"
    md_path.parent.mkdir(parents=True, exist_ok=True)
    md_path.write_text(_markdown(outline, cast, art, scripts, boards, p), encoding="utf-8")
    written["prompts"] = md_path
    return written
