"""故事层契约（小说 → 大纲 → 角色/美术 → 剧本 → 站位 → 分镜）。

这一层**只写戏和画面事实**，不写 ComfyUI 节点、模型名、工作流名。
编译到 `manju.schemas` 的 v10.1 渲染契约后，才由适配层决定怎么出图/出视频。

设计原则：
1. `extra="forbid"`：LLM 多写字段当场失败，交给 `LLMClient.structured` 修复重试。
2. 所有 id 引用必须闭合；编译器不猜。
3. 时长、台词、节拍都按确定性规则计算，不让模型重新估。
"""
from __future__ import annotations

import re
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class StoryModel(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True, validate_assignment=True)


# ==========================================================================
# 通用小结构
# ==========================================================================
class AdaptationItem(StoryModel):
    what: str = Field(description="保留/删除/合并的内容")
    why: str = Field(description="这样处理的原因")


class RiskItem(StoryModel):
    what: str = Field(description="改编风险")
    plan: str = Field(description="应对方案")


class Adaptation(StoryModel):
    core: str = Field(description="一句话核心：这部戏到底是关于什么")
    keep: list[AdaptationItem] = Field(min_length=1)
    cut: list[AdaptationItem] = Field(default_factory=list)
    merge: list[AdaptationItem] = Field(default_factory=list)
    risks: list[RiskItem] = Field(default_factory=list)


class Spine(StoryModel):
    want: str = Field(description="主角想要什么（可执行的目标）")
    obstacle: str = Field(description="最大的阻力")
    choice: str = Field(description="主角必须做的主动选择")
    cost: str = Field(description="选择的代价")
    turn: str = Field(description="本集/本剧的转折")
    hook: str = Field(description="结尾钩子")


# ==========================================================================
# 上游结构化契约（P0 新增）：世界设定 / 视觉风格 / 角色关系
# ==========================================================================
class WorldFaction(StoryModel):
    name: str
    desc: str = ""


class WorldLocation(StoryModel):
    name: str
    desc: str = ""


class WorldTimeline(StoryModel):
    epoch: str = Field(description="时代/纪元名")
    event: str = Field(description="该时代的关键事件")


class WorldRule(StoryModel):
    name: str
    desc: str = ""


class WorldBible(StoryModel):
    """世界设定结构化契约：势力 / 地理 / 时间线 / 规则 四块。"""
    factions: list[WorldFaction] = Field(default_factory=list)
    geography: list[WorldLocation] = Field(default_factory=list)
    timeline: list[WorldTimeline] = Field(default_factory=list)
    rules: list[WorldRule] = Field(default_factory=list)

    def blocks(self) -> dict[str, list]:
        return {"factions": self.factions, "geography": self.geography,
                "timeline": self.timeline, "rules": self.rules}


class VisualBible(StoryModel):
    """画风圣经契约（对齐 Art Bible：风格词 / 主色 / DO-DON'T / 参考风格）。"""
    style_keywords: list[str] = Field(default_factory=list)
    main_colors: list[str] = Field(default_factory=list,
                                   description="5-7 个主色 hex，如 #1a1a2e")
    do_dont: list[str] = Field(default_factory=list, description="DO/DON'T 规则")
    reference_styles: list[str] = Field(default_factory=list,
                                        description="参考风格名，如暗黑写实/赛博霓虹")


class Relationship(StoryModel):
    """角色关系结构化契约。"""
    type: str = Field(description="关系类型：敌对/亲属/师徒/爱慕/同盟…")
    target: str = Field(description="指向角色 id（C1…）或自由名")
    note: str = ""


class StoryParams(StoryModel):
    episodes: int = Field(ge=1, le=200)
    target_seconds: float = Field(gt=0, default=90)
    aspect_ratio: Literal["9:16", "16:9"] = "9:16"


class OutlineCharacter(StoryModel):
    id: str = Field(pattern=r"^C\d+$", description="C1 格式")
    name: str
    role: str = Field(description="主角/反派/配角/功能性角色")
    tier: Literal["lead", "support", "functional"] = "support"
    age: str = ""
    gender: str = ""
    identity: str = Field(default="", description="身份/职业")
    appearance: str = Field(default="", description="可见的外貌与服装要点（中文）")
    personality: list[str] = Field(default_factory=list)
    arc: str = ""
    voice_hint: str = Field(default="", description="音色/说话方式（中文）")


class OutlineScene(StoryModel):
    id: str = Field(pattern=r"^S\d+$")
    name: str
    primary: bool = False
    summary: str = Field(default="", description="这个空间讲什么故事")


class OutlineProp(StoryModel):
    id: str = Field(pattern=r"^P\d+$")
    name: str
    function: str = Field(description="这件道具承载什么剧情")
    scale: Literal["手持级", "桌面级", "家具级"] = "手持级"


class Beat(StoryModel):
    id: str = Field(pattern=r"^B\d+$")
    type: str = Field(description="打脸/揭破/反转/救赎/悬念…")
    weight: Literal["major", "minor"] = "minor"
    episode: int = Field(ge=1)
    setup: str = Field(default="", description="铺垫")
    payoff: str = Field(default="", description="回收")


class DesignEpisode(StoryModel):
    """情节分集设计的一集：按戏剧单元切，不按字数/章节切。

    `atom_ids` 是小说情节原子的 1-based 连续区间；`must_show` 是这一集
    必须让观众看见的完整事件单元（2-6 个），后续剧本/分镜逐条对账，缺一条即失败。
    集长由内容决定（不按字数/章节）：关键短事件哪怕只有 400-500 字也可以独立成集；
    `key_lines` 是必须原样保留的关键台词，允许纯对话集。
    """
    ep: int = Field(ge=1)
    title: str
    atom_ids: list[int] = Field(
        min_length=1,
        description="本集覆盖的情节原子编号（1-based，连续、按顺序、不重叠）")
    synopsis: str = Field(
        description="本集戏剧单元梗概，叙述体；可引用 1-2 句关键台词（带引号），"
                    "但概述主体仍是叙述")
    must_show: list[str] = Field(
        min_length=1, max_length=6,
        description="本集必须拍出来的具体事件（可见动作/决定/物件变化/关键揭露），"
                    "2-6 条，按发生顺序；抽象主题、心理活动不算。"
                    "每条是一个完整事件单元（起因→行动→结果），允许单事件独立成集")
    key_lines: list[str] = Field(
        default_factory=list, max_length=4,
        description="本集必须原样保留的关键台词（小说原文，1-4 句，带引号）；"
                    "剧本阶段逐句一字不改落进 beats，不得改写或删减。"
                    "允许本集以这些关键对话为主体（纯对话集）")
    key_props: list[str] = Field(
        default_factory=list, max_length=3,
        description="本集承担剧情转折的关键道具（小说原名，0-3 件）；"
                    "每件都必须有特写/插入镜并在本集回收")
    dramatic_question: str = Field(
        default="", description="本集驱动观众看下去的问题（一句话）")
    turn: str = Field(default="", description="本集转折：什么改变/被揭露")
    climax: str = Field(default="", description="本集情绪最高点，具体可拍")
    hook: str = Field(default="", description="开场第一秒的视觉钩子")
    cliff: str = Field(description="结尾悬念，必须具体可拍，并接住下一集")
    estimated_seconds: float = Field(
        default=90, gt=0, le=300,
        description="讲完本集 must_show 所需的自然时长（通常 45-240s，由内容决定，"
                    "不按字数/章节均分）；关键短事件独立成集时允许明显小于 90s")


class SeriesDesign(StoryModel):
    """先看整本小说，按戏剧单元决定集数与每集必拍事件。"""
    source: str = ""
    logline: str = ""
    episodes: list[DesignEpisode] = Field(min_length=1, max_length=200)

    @model_validator(mode="after")
    def _unique_eps(self) -> "SeriesDesign":
        eps = [e.ep for e in self.episodes]
        if len(eps) != len(set(eps)):
            raise ValueError(f"episode 重复：{eps}")
        return self


class EpisodePlan(StoryModel):
    ep: int = Field(ge=1)
    title: str
    synopsis: str = Field(description="叙述体，禁止引号台词")
    hook: str = Field(description="开场钩子说明")
    cliff: str = Field(description="结尾悬念说明")
    scene_ids: list[str] = Field(min_length=1)
    character_ids: list[str] = Field(default_factory=list)
    prop_ids: list[str] = Field(default_factory=list)
    crowd_plan: str = ""
    warnings: list[str] = Field(default_factory=list)
    emotion_curve: list[str] = Field(
        default_factory=list,
        description="本集情绪走向标签序列，如 [紧张, 悬念, 转折, 爆发, 温情]，"
                    "供 script 阶段按情绪基调写对白/节拍")
    # ---- 情节驱动分集（Plot mode）：由 SeriesDesign 确定性写入 ----
    must_show: list[str] = Field(
        default_factory=list, max_length=6,
        description="本集必须拍出来的具体事件；剧本每一条都要落到至少一场")
    key_lines: list[str] = Field(
        default_factory=list, max_length=4,
        description="本集必须原样保留的关键台词（小说原文，带引号）；"
                    "由 SeriesDesign 确定性写入，剧本阶段逐句一字不改落进 beats")
    key_props: list[str] = Field(
        default_factory=list, max_length=3,
        description="本集关键道具名；每件至少一个 insert 特写并参与本集转折")
    dramatic_question: str = ""
    turn: str = ""
    climax: str = ""
    estimated_seconds: float | None = None


class PowerRule(StoryModel):
    """主角的“金手指/独特优势”。没有超能力的故事也要写清他凭什么能翻盘。"""
    name: str
    sees: str = Field(description="它能看到/做到什么")
    cannot: str = Field(description="它做不到什么（边界）")
    cost: str = Field(description="每次使用的代价")


class StoryBible(StoryModel):
    """全局圣经：不依赖某一集的角色/场景/道具/爽点/参数。"""
    source: str
    title: str
    logline: str
    genre: str = ""
    audience: str = ""
    adaptation: Adaptation
    spine: Spine
    power_rule: PowerRule = Field(default_factory=lambda: PowerRule(
        name="主角的判断力", sees="他比旁人更早看清局势与人心",
        cannot="不能直接改变力量对比", cost="每次判断都要承担后果"))
    params: StoryParams
    visual_bible: dict[str, Any] | None = Field(
        default=None,
        description="全剧画风圣经（业务层）。None 时编译器用默认视觉圣经；"
                    "改这里即可换整剧画风，无需改代码。结构化时建议含 "
                    "style_keywords / main_colors(5-7 个 hex) / do_dont / reference_styles")
    world: WorldBible | None = Field(
        default=None,
        description="世界设定结构化契约：势力/地理/时间线/规则 四块。"
                    "P0 起 bible 应输出 world；None 时视为未做世界设定")
    characters: list[OutlineCharacter] = Field(min_length=1)
    scenes: list[OutlineScene] = Field(min_length=1)
    props: list[OutlineProp] = Field(default_factory=list)
    beats: list[Beat] = Field(default_factory=list)

    @model_validator(mode="after")
    def _unique_ids(self) -> "StoryBible":
        for label, ids in (("character", [c.id for c in self.characters]),
                           ("scene", [s.id for s in self.scenes]),
                           ("prop", [p.id for p in self.props]),
                           ("beat", [b.id for b in self.beats])):
            if len(ids) != len(set(ids)):
                raise ValueError(f"{label} id 重复：{ids}")
        return self


class EpisodeBatch(StoryModel):
    """一次 LLM 调用产出的分集梗概（每批 ≤ story_episode_batch 集）。"""
    episodes: list[EpisodePlan] = Field(min_length=1)

    @model_validator(mode="after")
    def _unique_eps(self) -> "EpisodeBatch":
        eps = [e.ep for e in self.episodes]
        if len(eps) != len(set(eps)):
            raise ValueError(f"episode 重复：{eps}")
        return self


class Outline(StoryBible):
    """全局圣经 + 全部分集梗概；编译器与下游只认这个完整对象。"""
    episodes: list[EpisodePlan] = Field(min_length=1)

    @model_validator(mode="after")
    def _episodes(self) -> "Outline":
        cids = {c.id for c in self.characters}
        sids = {s.id for s in self.scenes}
        pids = {p.id for p in self.props}
        eps = [e.ep for e in self.episodes]
        if len(eps) != len(set(eps)):
            raise ValueError(f"episode 重复：{eps}")
        if len(eps) != self.params.episodes:
            raise ValueError(
                f"episodes 数量 {len(eps)} 与 params.episodes={self.params.episodes} 不一致")
        for ep in self.episodes:
            for sid in ep.scene_ids:
                if sid not in sids:
                    raise ValueError(f"第 {ep.ep} 集引用了不存在的场景 {sid}")
            for cid in ep.character_ids:
                if cid not in cids:
                    raise ValueError(f"第 {ep.ep} 集引用了不存在的角色 {cid}")
            for pid in ep.prop_ids:
                if pid not in pids:
                    raise ValueError(f"第 {ep.ep} 集引用了不存在的道具 {pid}")
        return self


# ==========================================================================
# 角色层
# ==========================================================================
class IdentityLock(StoryModel):
    age_range: str = ""
    face_shape: str = ""
    hair: str = ""
    eyes: str = ""
    build: str = ""
    height_cm: int | None = Field(default=None, ge=0, le=260)
    silhouette_cue: str = ""
    facial_marks: str = ""
    skin: str = ""


class WardrobeState(StoryModel):
    state_id: str = Field(pattern=r"^C\d+-[A-Za-z0-9_-]+$")
    desc_en: str = Field(description="英文服装描述，直接进图像条件")
    tags: list[str] = Field(default_factory=list)


class Persona(StoryModel):
    gender: str = ""
    age_range: str = ""
    identity: str = ""
    appearance: str = ""
    personality: list[str] = Field(default_factory=list)
    temperament: str = ""
    motivation: str = ""
    arc: str = ""
    relationships: list[dict[str, str]] = Field(default_factory=list)
    relationship_spec: list[Relationship] = Field(
        default_factory=list,
        description="P0 结构化关系契约（type/target/note），供关系图谱查询")


class ImageSpec(StoryModel):
    prompt: str = Field(description="英文单张人物图提示词，禁止人名/场景词")
    negative_prompt: str = ""
    sheet: str = Field(default="", description="英文设定图版面指令")
    tags: list[str] = Field(default_factory=list)


class VoiceSpec(StoryModel):
    timbre: str = ""
    pitch: str = ""
    pace: str = ""
    accent: str = ""
    emotion: str = ""
    reference_hint: str = ""
    prompt: str = Field(default="", description="英文音色提示词，给 TTS")


class CastCharacter(StoryModel):
    # 说明放在类体里会被 Pydantic 当成字段，故用注释：identity_anchor() 只读
    # identity_lock 里最省 token 的区分特征，不重复完整定妆 prompt。
    id: str = Field(pattern=r"^C\d+$")
    name: str
    aliases: list[str] = Field(default_factory=list)
    importance: Literal["protagonist", "major", "supporting", "minor"] = "supporting"
    one_liner: str = ""
    persona: Persona = Field(default_factory=Persona)
    identity_lock: IdentityLock = Field(default_factory=IdentityLock)
    appearance_block: str = Field(
        default="",
        description="P0 Veo 式不可变外貌块：把体型/五官/印记/肤色/发型写成一段英文，"
                    "跨集逐字复用（可变项如服装单独走 wardrobe_state）")
    wardrobe_states: list[WardrobeState] = Field(min_length=1)
    image: ImageSpec
    voice: VoiceSpec = Field(default_factory=VoiceSpec)
    color_signature: str = ""
    pose_library: list[str] = Field(
        default_factory=list,
        description="P1 姿态库（可选）：3-5 个命名姿态英文短语，跨镜逐字复用，"
                    "如 'hands clasped at chest'。分镜 LLM 的 pose 从库里取，保证表演一致")
    expression_library: list[str] = Field(
        default_factory=list,
        description="P1 表情库（可选）：3-5 个命名表情英文短语，跨镜逐字复用，"
                    "如 'brow furrowed, lips tight'。分镜 LLM 的 expression 从库里取")


_NON_FACE_HINTS_CN = (
    # 只保留不会误伤普通描述的动物/非人词。单字“龙/马/鸟/狮/虎/狼/熊/兔/
    # 猴/鼠/蛇/象/鹰”会把“龙卷风”“骏马般”“鸟瞰”等正常描述误判成非人角色。
    "狗", "犬", "猫", "宠物", "动物", "稻草人", "机器人", "机械人", "玩偶",
    "木偶", "面具", "头盔", "蒙面", "剪影", "乌鸦", "南瓜", "巨龙", "神龙",
    "龙族", "飞龙", "骏马", "战马",
)
# 英文必须按词边界匹配：否则 “petite” 命中 pet、“crown” 命中 crow，
# 会把正常人类角色误判成不需要锁脸。
_NON_FACE_HINTS_EN = (
    "dog", "cat", "puppy", "kitten", "pet", "animal", "scarecrow", "robot",
    "android", "machine", "puppet", "doll", "mask", "helmet", "silhouette",
    "dragon", "horse", "bird", "lion", "tiger", "wolf", "bear", "rabbit",
    "monkey", "mouse", "snake", "elephant", "eagle", "crow", "pumpkin",
)
_NON_FACE_RE = re.compile(
    r"\b(?:" + "|".join(re.escape(h) for h in _NON_FACE_HINTS_EN) + r")\b",
    re.IGNORECASE,
)


def face_lock_required(ch: CastCharacter) -> bool:
    """该角色是否需要 PuLID 锁脸。

    同框多脸门只应统计**需要锁脸的人**：狗/猫/稻草人/机器人/面具角色
    没有可锁的人脸，把宠物算成"第二张正脸"会让导演永远过不了门
    （绿野仙踪 S1：多萝茜 + 托托）。
    """
    blob = " ".join([
        ch.persona.identity, ch.persona.appearance, ch.one_liner,
        ch.appearance_block, *(ch.image.tags or []),
    ])
    if any(hint in blob for hint in _NON_FACE_HINTS_CN):
        return False
    return _NON_FACE_RE.search(blob) is None


class Cast(StoryModel):
    source: str
    summary: str = ""
    characters: list[CastCharacter] = Field(min_length=1)

    @model_validator(mode="after")
    def _unique(self) -> "Cast":
        ids = [c.id for c in self.characters]
        if len(ids) != len(set(ids)):
            raise ValueError(f"角色 id 重复：{ids}")
        return self


def identity_anchor(ch: CastCharacter, *, limit: int = 180) -> str:
    """短身份锚点：只保留同框辨认所需的区分特征。

    分镜已经通过 `ref_characters` 绑定定妆图，提示词再逐字重复整段
    `image.prompt` 既浪费 token，也会让模型把“静态定妆照”的姿势/背景
    带进每个镜头。这里只取发型、面部印记、轮廓这三项最便宜且稳定的
    区分信息；缺锁脸字段时才回退到外貌块的首个短语。

    `limit` 以字符计（中英混排都按字符截断），避免模型把整段外貌塞进来。
    """
    lock = ch.identity_lock
    parts: list[str] = []
    seen: set[str] = set()
    for value in (lock.hair, lock.facial_marks, lock.silhouette_cue):
        text = " ".join((value or "").split()).strip(" ,;，；")
        key = text.casefold()
        if text and key not in seen:
            parts.append(text)
            seen.add(key)
    if not parts:
        source = ch.appearance_block or (ch.image.prompt if ch.image else "") or ""
        text = " ".join(source.split()).strip(" ,;，；")
        first = re.split(r"(?<=[.!?。！？])\s+|[,;，；]", text, maxsplit=1)[0]
        first = first.strip(" ,;，；")
        if first:
            parts.append(first)
    anchor = "; ".join(parts)
    if len(anchor) <= limit:
        return anchor
    cut = anchor[:limit]
    # 英文尽量在词边界截断，避免留下半个单词；中文没有空格则按字符截。
    space = cut.rfind(" ")
    if space >= limit * 3 // 4:
        cut = cut[:space]
    return cut.rstrip(" ,;，；") + "…"


# ==========================================================================
# 美术层
# ==========================================================================
class Anchor(StoryModel):
    name: str
    desc: str


class LightingState(StoryModel):
    state: str
    prompt: str = Field(description="英文光照提示词")


class ArtImage(StoryModel):
    prompt: str = Field(description="英文主视角提示词")
    negative_prompt: str = ""
    sheet: str = ""
    tags: list[str] = Field(default_factory=list)


class ArtScene(StoryModel):
    id: str = Field(pattern=r"^S\d+$")
    name: str
    primary: bool = False
    summary: str = ""
    location_type: Literal["", "INT", "EXT", "INT/EXT"] = Field(
        default="", description="剧本工业字段：内景/外景")
    time_of_day: Literal["", "DAY", "NIGHT", "DAWN", "DUSK"] = Field(
        default="", description="剧本工业字段：昼夜")
    anchors: list[Anchor] = Field(min_length=3, max_length=5)
    lighting: list[LightingState] = Field(min_length=1)
    image: ArtImage
    variant_of: str | None = None
    changes: str = ""


class PropState(StoryModel):
    state: str
    prompt: str = Field(description="英文状态提示词")


class ArtProp(StoryModel):
    id: str = Field(pattern=r"^P\d+$")
    name: str
    scale: Literal["手持级", "桌面级", "家具级"] = "手持级"
    category: Literal["Prop", "SetDressing"] = Field(
        default="Prop",
        description="StudioBinder 规范：Prop=角色交互道具，SetDressing=陈设，两类分开管")
    summary: str = ""
    anchors: list[Anchor] = Field(min_length=3, max_length=5)
    states: list[PropState] = Field(min_length=1)
    related_scenes: list[str] = Field(default_factory=list)
    carried_by: str = ""
    image: ArtImage


class Art(StoryModel):
    source: str
    setting: str = Field(default="", description="年代/地域，禁止‘古代/架空’这类含糊词")
    scenes: list[ArtScene] = Field(min_length=1)
    props: list[ArtProp] = Field(default_factory=list)

    @model_validator(mode="after")
    def _unique(self) -> "Art":
        for label, ids in (("scene", [s.id for s in self.scenes]),
                           ("prop", [p.id for p in self.props])):
            if len(ids) != len(set(ids)):
                raise ValueError(f"{label} id 重复：{ids}")
        return self


# ==========================================================================
# 剧本层
# ==========================================================================
ShotCoverage = Literal["establish", "action", "reaction", "insert",
                       "pov", "dialogue", "transition"]

NarrationKind = Literal["exposition", "inner"]
TransitionKind = Literal["match_cut", "sound_bridge", "action_bridge",
                         "time_jump", "fade", "dissolve"]


class NarrationLine(StoryModel):
    """旁白：叠在已有画面上，不占节拍、不做口型。

    - exposition：NARRATOR 解释性旁白，交代必要前情/世界规则/时间跳跃；
    - inner：角色内心独白，只在该角色有可见反应/关键抉择时使用。
    """
    kind: NarrationKind = Field(description="exposition=解释性旁白；inner=角色内心独白")
    text: str = Field(min_length=1, max_length=60, description="旁白文本（中文，含标点）")
    beat: int = Field(ge=1, description="叠在本场第几拍上（1 起）")
    speaker: str = Field(default="NARRATOR",
                         description="exposition 固定 NARRATOR；inner 必须是 C# 角色 id")
    delivery: str = Field(default="", description="语气/语速提示")

    @model_validator(mode="after")
    def _speaker_ok(self) -> "NarrationLine":
        if self.kind == "exposition" and self.speaker != "NARRATOR":
            raise ValueError("解释性旁白的 speaker 必须是 NARRATOR")
        if self.kind == "inner" and not re.fullmatch(r"C\d+", self.speaker):
            raise ValueError("内心独白的 speaker 必须是角色 id（C1 格式）")
        return self


class SceneTransition(StoryModel):
    """场间转场：下一场的第一个镜头如何接住上一场。

    anchor 必须是一个具体的共享元素：同一物件/动作/形状、连续音效、
    或明确的时间跳跃落点。空 anchor 只允许 fade/dissolve。
    """
    type: TransitionKind
    anchor: str = Field(default="", description="共享锚点：物件/动作/声音/时间落点")
    note: str = Field(default="", description="转场说明（可选）")


class ScriptBeat(StoryModel):
    action: str | None = Field(default=None, description="动作节拍（叙述体，禁止引号台词）")
    speaker: str | None = Field(default=None, description="C1 或 VO")
    line: str | None = Field(default=None, description="台词本体")
    delivery: str = Field(default="", description="语气/表演提示")
    coverage: ShotCoverage | None = Field(
        default=None,
        description="本拍在分镜里承担的 coverage 类型：establish 建立 / action 动作 / "
                    "reaction 反应 / insert 道具环境细节 / pov 主观视点 / dialogue 对白 / "
                    "transition 转场。剧本层用它规划全集覆盖率配额；分镜一镜一拍沿用此值。")
    state_change: dict[str, str] = Field(
        default_factory=dict,
        description="P0-C2 本拍内角色状态变更：换装/道具易主/伤势，key=角色Cid，"
                    "value=变更（如 换鞋/脱下外套/接过剑/手臂受伤）。"
                    "供校验场级 wardrobe 快照与拍内变更一致。")
    expression: str = Field(
        default="",
        description="P1-S2 微表演/表情提示（中文，如：咬紧牙关/眼神闪躲/嘴角抽了一下）。"
                    "关键拍（有对白或情绪转折拍）必填，分镜层据此定 subjects.expression，不临场发明。")
    state_from: str = Field(
        default="",
        description="P1 剧本层承接：本拍起始承接什么（谁在哪/朝向/在做什么），"
                    "与上一拍 state_out 对账，不允许瞬移/无由换装。")
    screen_side: str = Field(
        default="",
        description="P1 建议机位侧：left/right/center，守 180° 轴线；"
                    "连续拍同一主体屏幕侧翻转需有过渡镜/切理由。")
    state_out: dict[str, str] = Field(
        default_factory=dict,
        description="P1 拍级状态账本：本拍结束后关键状态快照，key=角色Cid，"
                    "value=「方位/朝向/服装态/伤势」斜杠串（如 center/away_from_camera/C1-silver/无伤）。"
                    "逐拍衔接校验：拍末状态必须与下一拍 state_from 可对账。")
    # ---- 情节驱动：镜头为什么必须拍这一拍 ----
    visual_focus: str = Field(
        default="",
        description="本拍画面重点（如：银鞋、门缝里的影子、主角的手）。"
                    "emphasis≠none 时必填，分镜层据此强制景别/运镜。")
    emphasis: Literal["none", "highlight", "key_prop", "climax"] = Field(
        default="none",
        description="本拍叙事权重：key_prop=关键道具特写（分镜强制 close/推镜）；"
                    "highlight=关键转折/信息（强制非静态机位）；"
                    "climax=情绪最高点（强制非静态机位）。")

    @model_validator(mode="after")
    def _exactly_one(self) -> "ScriptBeat":
        has_action = bool((self.action or "").strip())
        has_line = bool((self.line or "").strip())
        if has_action == has_line:
            raise ValueError("节拍必须二选一：action 或 speaker+line")
        if has_line and not (self.speaker or "").strip():
            raise ValueError("台词节拍必须给 speaker")
        return self


class ScriptScene(StoryModel):
    id: str = Field(pattern=r"^S\d+$")
    location: str = ""
    time: str = ""
    lighting: str = ""
    characters: list[str] = Field(default_factory=list)
    props: list[str] = Field(default_factory=list)
    wardrobe: dict[str, str] = Field(default_factory=dict)
    goal: str = ""
    obstacle: str = ""
    turn: str = ""
    cost: str = ""
    information: str = ""
    objects: list[str] = Field(default_factory=list)
    mood: str = ""
    beat: str = ""
    function: str = ""
    callbacks: list[str] = Field(
        default_factory=list,
        description="P0 剧情逻辑门：本场台词回指的既往事件/前情线索"
                    "（如“上周在铁匠铺的约定”），供校验是否真实存在于前文")
    restructure: dict[str, Any] = Field(
        default_factory=dict,
        description="P2-2 场景重构决策记录：{from:[S..], action: merge/split, "
                    "to:[S..], reason: 为何合并/拆分}；有重构必有理由，可复核。")
    # ---- 情节驱动：场与场的因果链 ----
    major_event: str = Field(
        default="",
        description="本场承载的 must_show 事件（必须逐字取自本集 must_show）；"
                    "纯反应/过渡场可留空，但 causal_from 必须写清上一场如何导致本场。")
    causal_from: str = Field(
        default="",
        description="本场由什么导致：上一场的结果/上集 cliff；第 1 场写“开场/承接上集”。"
                    "禁止无因果跳场。")
    narration: list[NarrationLine] = Field(
        default_factory=list,
        description="叠在本场画面上的人声旁白/内心独白；不占节拍、不做口型。"
                    "只补画面无法交代的必要信息或角色内心，禁止复述可见动作。")
    transition_in: SceneTransition | None = Field(
        default=None,
        description="本场如何从上一场接过来；第 1 场可空。第 2 场起应给，"
                    "避免无动机硬切。")
    beats: list[ScriptBeat] = Field(min_length=1)


class ScriptEpisode(StoryModel):
    ep: int = Field(ge=1)
    title: str = ""
    target_seconds: float = Field(gt=0, default=90)
    hook: str
    cliff: str
    hook_beat: tuple[int, int] = Field(description="[场, 拍]，钩子具象的认领位置")
    beats_claimed: list[str] = Field(default_factory=list)
    # 由 EpisodePlan 确定性写入，剧本层不得自行增删
    must_show: list[str] = Field(default_factory=list)
    key_props: list[str] = Field(default_factory=list)
    scenes: list[ScriptScene] = Field(min_length=1)

    @property
    def duration(self) -> float:
        return sum(_scene_seconds(s) for s in self.scenes)

    def duration_with(self, params: ScriptParams) -> float:
        return sum(_scene_seconds(s, params) for s in self.scenes)


class ScriptParams(StoryModel):
    chars_per_second: float = Field(default=4.5, gt=0)
    action_seconds: float = Field(default=2.5, gt=0)
    tolerance: float = Field(default=0.15, gt=0, lt=1)
    max_line_chars: int = Field(default=18, ge=1,
                                description="单句字数上限；下游 drama 审计读取同一值")
    # ---- 以下是渲染/配音引擎能力，不是故事层普适规则 ----
    # 默认都不做限制；具体项目按自己的引擎在 config.json 的 story / pipeline 段覆盖。
    max_faces_per_shot: int | None = Field(
        default=None, ge=1,
        description="每个画幅最多允许多少张可锁的人脸；None=不限制（默认）")
    max_lines_per_shot: int | None = Field(
        default=None, ge=1,
        description="同一镜最多允许几句台词；None=不限制（默认）。"
                    "单参考图口型引擎通常设为 1")
    min_shot_seconds: float = Field(
        default=1.0, gt=0,
        description="单镜最小时长；由渲染/剪辑引擎能力决定")
    max_shot_seconds: float = Field(
        default=6.0, gt=0, le=30,
        description="单镜最大时长；由渲染引擎单段能力与节奏共同决定。"
                    "2026-10-30 起默认 6.0s（调研：无引擎用统一档位，"
                    "主流单镜 2-5s + 尾帧余量）；引擎档案覆盖："
                    "comfyui=4.0（LTX 档位）、seedance/jimeng=8.0")
    camera_motion: Literal["full", "limited"] = Field(
        default="full",
        description="运镜能力：full=允许推拉摇移跟环绕（Seedance 系云端引擎）；"
                    "limited=只允许轻度运镜（Push In/Tilt/Pedestal），"
                    "禁大幅运动（ComfyUI 系局部模型防形变）。"
                    "由 story.engine 档案或 story.camera_motion 显式配置决定")
    max_segment_seconds: float = Field(
        default=15.0, gt=0, le=600,
        description="一个分镜段的最大时长；由一次生成任务的时长上限决定")
    # ---- 2026-10-30 时长与阶段化：单镜时长按内容计算，不再默认档位 ----
    max_actions_per_shot: int | None = Field(
        default=None, ge=1,
        description="单镜动作数上限；超出必须拆镜/拆段（分镜层 hard error）。"
                    "None=不限制（默认）。seedance=2，comfyui=1")
    action_seconds_per_beat: float = Field(
        default=1.5, gt=0,
        description="分镜校验基准：单镜时长必须 ≥ 动作节拍数 × 该值。"
                    "（推断参数，可调；行业无权威每秒动作数公式）")
    pause_seconds_per_line: float = Field(
        default=0.7, gt=0,
        description="每句台词后的停顿秒数，计入台词时长下限")
    duration_tiers: list[float] | None = Field(
        default=None,
        description="单镜时长向上对齐的档位表（秒）。None=不设档位，向上取整到 "
                    "round_up_step。comfyui=[2,3,4]（25fps 49/73/97 帧），"
                    "seedance=None（任意整数秒）")
    round_up_step: float = Field(
        default=0.5, gt=0,
        description="无 duration_tiers 时，时长向上取整的步长（秒）")
    shot_size_floor: dict[str, float] = Field(
        default_factory=lambda: {
            "extreme_close": 1.5, "close": 1.5, "medium": 3.0,
            "wide": 4.0, "extreme_wide": 4.0,
        },
        description="景别时长下限表（秒）：特写/近景 1.5s，中景 3s，远景 4s。"
                    "单镜时长下限 = max(景别下限, 动作数×基准, 台词秒)")
    prompt_language: Literal["en", "zh", "any"] = Field(
        default="en",
        description="首帧提示词语言：en=只接受英文（多数文生图模型）；"
                    "zh=只接受中文；any=不限制")
    # ---- 旁白/转场：叙事增强，不是引擎能力 ----
    narration_mode: Literal["auto", "off", "on"] = Field(
        default="auto",
        description="旁白策略：auto=对白稀薄/纯动作段落自动补；off=关闭；on=要求本集有旁白")
    narration_max_exposition: int = Field(
        default=2, ge=0, le=8,
        description="每集解释性旁白条数上限（防有声书化）")
    narration_max_inner: int = Field(
        default=3, ge=0, le=12,
        description="每集内心独白条数上限（防过度抒情）")
    transition_mode: Literal["auto", "off", "on"] = Field(
        default="auto",
        description="场间转场策略：auto=默认要求场间有动机；off=不检查；"
                    "on=强制第 2 场起写 transition_in")


class StoryScript(StoryModel):
    source: str
    params: ScriptParams = Field(default_factory=ScriptParams)
    episodes: list[ScriptEpisode] = Field(min_length=1)


def _line_chars(text: str) -> int:
    return len("".join((text or "").split()))


def _beat_seconds(beat: ScriptBeat, params: ScriptParams) -> float:
    if beat.action:
        return max(params.min_shot_seconds, params.action_seconds)
    # 极短台词（如“走！”）按语速算可能 <1s，但引擎没有短于 min_shot 的镜头。
    # 在这里就按单镜下限计时，剧本/分镜/审计才用同一套时长口径。
    return max(params.min_shot_seconds,
               _line_chars(beat.line or "") / params.chars_per_second)


def _scene_seconds(scene: ScriptScene, params: ScriptParams | None = None) -> float:
    p = params or ScriptParams()
    return sum(_beat_seconds(b, p) for b in scene.beats)


# ==========================================================================
# 站位层
# ==========================================================================
class StagingItem(StoryModel):
    name: str
    plan: list[float] = Field(min_length=4, max_length=4, description="[西,北,东,南]，0-1")
    on: bool = True
    round: bool = False


class StagingLocation(StoryModel):
    scene_id: str = Field(pattern=r"^S\d+$")
    name: str
    items: list[StagingItem] = Field(min_length=1)


class StagingCast(StoryModel):
    id: str = Field(pattern=r"^C\d+$")
    name: str


class StagingPlace(StoryModel):
    id: str = Field(pattern=r"^C\d+$")
    name: str
    present: bool = True
    item: str | None = None
    side: Literal["north", "east", "south", "west", "on"] | None = None
    along: float | None = Field(default=None, ge=0, le=1)
    posture: Literal["sit", "stand"] | None = None
    facing: Literal["north", "northeast", "east", "southeast",
                    "south", "southwest", "west", "northwest"] | None = None


class StagingScene(StoryModel):
    ep: int = Field(ge=1)
    scene: int = Field(ge=1, description="这一集的第几场")
    scene_id: str = Field(pattern=r"^S\d+$")
    lighting: str = ""
    cast: list[StagingCast] = Field(default_factory=list)
    places: list[StagingPlace] = Field(default_factory=list)
    extras: int = Field(default=0, ge=0)
    extras_where: str | None = None
    why: str = ""


class Staging(StoryModel):
    source: str
    locations: list[StagingLocation] = Field(default_factory=list)
    scenes: list[StagingScene] = Field(default_factory=list)


# ==========================================================================
# 分镜层
# ==========================================================================
CutSize = Literal["extreme-wide", "wide", "medium", "close", "extreme-close"]
CutCamera = Literal[
    "Static Shot", "Push In", "Pull Out", "Zoom In", "Zoom Out",
    "Pan Left", "Pan Right", "Truck Left", "Truck Right",
    "Tilt Up", "Tilt Down", "Pedestal Up", "Pedestal Down",
    "Arc Shot", "Tracking Shot", "Shake Slightly", "Shake Strongly",
    "POV", "Roll Clockwise", "Roll Counterclockwise",
]


class CutSubject(StoryModel):
    char: str = Field(pattern=r"^C\d+$")
    wardrobe_state: str = ""
    pose: str = Field(default="", description="英文姿态")
    expression: str = Field(default="", description="英文表情")


DIRECTIONS = {"center", "north", "northeast", "east", "southeast",
              "south", "southwest", "west", "northwest",
              "off-screen", "toward_camera", "away_from_camera"}


class CutState(StoryModel):
    """一个切镜结束时的可继承状态；跨镜/跨集硬校验的输入。"""
    where: dict[str, str] = Field(default_factory=dict,
                                  description="角色 -> 场景内方位（受控词表）")
    facing: dict[str, str] = Field(default_factory=dict,
                                   description="角色 -> 面朝方向（受控词表）")
    posture: dict[str, str] = Field(default_factory=dict,
                                    description="角色 -> 姿态，英文")
    wardrobe: dict[str, str] = Field(default_factory=dict,
                                     description="角色 -> 服装 state_id")
    props: dict[str, str] = Field(default_factory=dict,
                                  description="道具 -> 持有者 C1 / scene")
    injuries: dict[str, str] = Field(default_factory=dict,
                                     description="角色 -> 伤势/异常状态")

    @model_validator(mode="after")
    def _directions(self) -> "CutState":
        for field_name in ("where", "facing"):
            for who, value in getattr(self, field_name).items():
                if value not in DIRECTIONS:
                    raise ValueError(
                        f"{field_name}[{who}]={value!r} 不在受控方向词表 {sorted(DIRECTIONS)}")
        return self

    def empty(self) -> bool:
        return not any((self.where, self.facing, self.posture, self.wardrobe,
                        self.props, self.injuries))


class CutSegment(StoryModel):
    """单镜的阶段化子分镜（一段一个主动作）。

    2026-10-30 时长与阶段化落地：动作数 >1 的镜必须拆段；一段只容纳
    一个主动作（起点→过程→结果），段区间连续不重叠、段秒数和 = 整镜时长。
    单动作镜可省略（segments=None）。
    """

    text: str = Field(description="该段画面描述（英文，单一动作/状态，别写跨段多事件）")
    duration_sec: float = Field(gt=0, description="该段时长（秒）")
    phase: Literal["start", "mid", "end"] = Field(
        default="mid", description="段位：start=起 / mid=中 / end=终")
    beat: str = Field(default="", description="该段的中文动作要点（供人工核对）")
    time_range: list[float] | None = Field(
        default=None, description="[起秒, 止秒]；可选，代码按累计自动补全")
    camera_phase: str = Field(
        default="", description="该段内的运镜阶段（继承整镜 camera_move，不改变运镜约束）")
    state_in: str = Field(default="", description="段起始承接：从上一段结束处接什么")
    state_out: str = Field(default="", description="段结束状态；作为下一段 state_in 的起点")
    keyframe: bool = Field(
        default=False, description="本段是否作为条件注入帧（LTX 中段注入 / Seedance 尾帧）")
    frame_hint: str | None = Field(
        default=None, description="目标帧提示：'first'/'last'/具体帧号（LTX 8n+1）")


class StoryCut(StoryModel):
    beats: tuple[int, int] = Field(description="认领本场第几拍到第几拍（含两端）")
    # 上限交给 ScriptParams.max_shot_seconds / max_segment_seconds 校验；
    # schema 这里只挡住明显异常值（同一参数可被不同引擎配置成更长单镜）。
    seconds: float = Field(gt=0, le=600)
    size: CutSize
    camera: CutCamera = "Static Shot"
    characters: list[str] = Field(default_factory=list)
    props: list[str] = Field(default_factory=list)
    frame: str = Field(description="中文分镜图描述：主体、位置、动作瞬间、光线")
    prompt_en: str = Field(description="英文首帧提示词：直接给图像模型")
    shot: str = Field(default="", description="中文镜头正文：这几秒发生什么")
    motion: str = Field(default="", description="英文画面内动作（进行时）")
    lens: str = ""
    camera_position: str = ""
    composition: str = ""
    eyeline: str = ""
    focus: str = ""
    stability: Literal["stable", "slight-shake", "handheld"] = "stable"
    sfx: str = ""
    lighting: str = ""
    subjects: list[CutSubject] = Field(default_factory=list)
    coverage: ShotCoverage = "action"
    cut_reason: Literal["action_done", "reaction", "new_info", "pov", "sound",
                        "space", "time", "match_cut"] = "action_done"
    purpose: str = ""
    information: str = ""
    state_out: CutState = Field(default_factory=CutState,
                                description="本镜结束时人物/道具/伤势状态")
    # ---- P1 两级生成新增：动作承接 / 越轴校验 / 叙事职责 ----
    state_from: CutState = Field(default_factory=CutState,
                                 description="本镜起始状态 = 上一镜 state_out（承接起点）")
    screen_side: dict[str, str] = Field(
        default_factory=dict,
        description="角色 -> 屏幕侧 left/right/center，供越轴（180°）校验")
    beat_goal: str = Field(default="",
                           description="本镜叙事目标：它为本节拍/本段完成什么")
    emphasis: Literal["none", "highlight", "key_prop", "climax"] = Field(
        default="none",
        description="本镜叙事权重；key_prop 必须 close/extreme-close 且非静态机位，"
                    "highlight/climax 必须非静态机位")
    visual_focus: str = Field(
        default="", description="本镜必须让观众看清的对象/动作重点")
    allow_multi_face: bool = Field(
        default=False,
        description="仅当画幅内多人全部背身/剪影、无需锁脸时显式置 true；"
                    "否则同框 ≥2 张正脸下游无法用 PuLID 保脸")
    multi_subject_strategy: Literal["auto", "joint", "per_subject", "hybrid"] | None = Field(
        default=None,
        description="同框 ≥2 人时的合成策略；None/auto 会在编译期按镜头语义"
                    "补成 joint 或 hybrid（§4.4 禁止 auto 兜底）")
    # ---- 2026-10-30 时长与阶段化 ----
    segments: list[CutSegment] | None = Field(
        default=None,
        description="阶段化子分镜（动作数 >1 时必填）：一段一主动作，段和=整镜时长。"
                    "单动作镜省略。LLM 输出，compile 透传到 storyboard.shots[].segments")
    duration_basis: str | None = Field(
        default=None,
        description="时长算账过程（中文，供人工复核与调参）：如"
                    "'动作3拍×1.5s=4.5s；台词0s；景别下限1.5s；取大4.5s→对齐5s'")

    @model_validator(mode="after")
    def _beat_range(self) -> "StoryCut":
        a, b = self.beats
        if a < 1 or b < a:
            raise ValueError(f"beats 区间非法：{self.beats}")
        return self


class CutPlan(StoryModel):
    """分镜 EDL 的单镜规划：只保留必须由模型决定的导演字段。

    时长/覆盖率/画面中文描述/表情/叙事职责/机位元数据全部由代码从剧本拍
    与场次信息确定性推导，不再让模型重复输出（B+D 优化）。
    """
    beats: tuple[int, int] | None = Field(
        default=None,
        description="可省略；cuts 数组顺序就是拍序。给值时必须是单拍 [i,i]，"
                    "多拍会被代码拆开。")
    size: CutSize
    camera: CutCamera = "Static Shot"
    characters: list[str] = Field(
        default_factory=list,
        description="本镜出场角色 Cid；可省略，代码会按动作线主体/场次人物推导")
    props: list[str] = Field(default_factory=list)
    cut_reason: Literal["action_done", "reaction", "new_info", "pov", "sound",
                        "space", "time", "match_cut"] = "action_done"
    sfx: str = Field(default="", description="现实音效关键词（中文）")
    state_out: dict[str, Any] = Field(
        default_factory=dict,
        description="本镜结束状态（可省略）：{where:{C1:center}, facing:{C1:north}, "
                    "posture:{C1:standing}, wardrobe:{C1:C1-default}, "
                    "props:{P1:C1}, injuries:{C1:无伤}}；无变化省略，代码沿用上一镜。"
                    "这里用普通对象接收，方向词表由代码清洗，避免一处笔误整轮重写")
    allow_multi_face: bool = False
    multi_subject_strategy: Literal["auto", "joint", "per_subject", "hybrid"] | None = Field(
        default=None,
        description="可省略；同框 ≥2 人时由编译期按 allow_multi_face/景别兜底，"
                    "显式给非 auto 值时优先采用")


class SegmentPlan(StoryModel):
    """一场分镜的 EDL：cuts 按数组顺序一镜一拍覆盖本场全部剧作拍。"""
    scene_index: int = Field(default=1, ge=1)
    cuts: list[CutPlan] = Field(min_length=1)
    blocking: str = ""
    soundscape: str = ""
    music: str = ""


class CutPrompt(StoryModel):
    """给单镜的视觉展开结果：只含首帧提示词与画面内动作。"""
    prompt_en: str = Field(min_length=1,
                           description="英文首帧提示词：主体+位置+动作瞬间+光线")
    motion: str = Field(default="", description="英文画面内动作（进行时）")


class SegmentPrompts(StoryModel):
    prompts: list[CutPrompt] = Field(min_length=1)


class ActionBeat(StoryModel):
    """动作线里的一个语义连续的动作节拍（两级生成的第一级输出）。"""
    beats: tuple[int, int] = Field(description="认领本场第几拍到第几拍（含两端）")
    goal: str = Field(description="本拍为叙事完成什么（一句话）")
    from_prev: str = Field(description="从上一拍结束处承接什么（谁/在哪/在做什么）")
    action: str = Field(description="本拍单一动作（起点→过程→结果，一句话可表达）")
    to_next: str = Field(description="本拍结束把谁送到哪 / 下一拍承接什么")
    who: str = Field(default="",
                     description="P0-C1 本拍动作主体（C id；空则沿用 from_prev 主体）")
    where: str = Field(default="",
                       description="P0-C1 动作发生方位（受控词表：center/north/…/"
                                   "off-screen/toward_camera/away_from_camera）")
    facing: str = Field(default="",
                        description="P0-C1 主体朝向（同上受控词表）")
    suggested_shot: str = Field(
        default="",
        description="P0-C3 本拍建议镜头类型/景别/承接（如 过肩反打、中景动作、"
                    "特写反应、推镜），分镜第二级据此切镜而非全临场决定")
    # ---- P1 承接意图下到剧本层：第一级（动作线）就定死，第二级只执行不设计 ----
    state_from: str = Field(
        default="",
        description="P1 本拍起始承接：上一拍末谁在哪/朝向/在做什么（结构化承接意图，"
                    "直接抄剧本拍 state_from；不得临场新造空间关系）")
    screen_side: str = Field(
        default="",
        description="P1 建议机位侧 left/right/center：同一主体跨拍不得无理由左右翻转（越轴）。")


class ActionBeatLine(StoryModel):
    """一场的动作节拍序列：按顺序、不重不漏覆盖本场全部剧作拍。"""
    scene_index: int = Field(ge=1)
    beats: list[ActionBeat] = Field(min_length=1)

    def claimed(self) -> list[int]:
        out: list[int] = []
        for b in self.beats:
            out.extend(range(b.beats[0], b.beats[1] + 1))
        return out


class StorySegment(StoryModel):
    id: str = Field(pattern=r"^E\d{2}-\d{2}$")
    scene_index: int = Field(ge=1)
    cuts: list[StoryCut] = Field(min_length=1)
    blocking: str = ""
    soundscape: str = ""
    music: str = ""
    transition_in: SceneTransition | None = Field(
        default=None,
        description="本段（通常一场）从上一段接过来的转场设计；拆分长段时首段保留")


class StoryEpisodeBoard(StoryModel):
    ep: int = Field(ge=1)
    state_in: CutState = Field(default_factory=CutState,
                               description="本集开场状态（= 上一集终态）")
    segments: list[StorySegment] = Field(min_length=1)

    @property
    def state_out(self) -> CutState:
        return self.segments[-1].cuts[-1].state_out


class StoryStoryboard(StoryModel):
    source: str
    aspect_ratio: Literal["9:16", "16:9"] = "9:16"
    episodes: list[StoryEpisodeBoard] = Field(min_length=1)


# ==========================================================================
# P2-1 R² 两遍生成：Reader 因果情节图 → Rewriter 场景大纲 → screenplay
# ==========================================================================
class CausalNode(StoryModel):
    """Reader 第一遍产出的事件节点：一个因果链上的最小事件单元。"""
    id: str = Field(description="事件节点 id，如 E1/E2…")
    what: str = Field(description="发生了什么（一句话事实）")
    why: str = Field(default="", description="为什么发生（动机/前因）")
    stake: str = Field(default="", description="代价/赌注")


class CausalEdge(StoryModel):
    """因果边：from 事件导致 to 事件。"""
    from_id: str
    to_id: str
    kind: Literal["causes", "enables", "reacts_to", "turns"] = "causes"


class CausalGraph(StoryModel):
    """Reader 滑窗扫法典/大纲后产出的结构化因果情节图。"""
    nodes: list[CausalNode] = Field(min_length=1)
    edges: list[CausalEdge] = Field(default_factory=list)

    def node_ids(self) -> set[str]:
        return {n.id for n in self.nodes}


class SceneOutlineItem(StoryModel):
    """Rewriter 第二遍：基于因果图的场景大纲（先于 screenplay）。"""
    scene: int = Field(ge=1)
    location: str
    beats: list[str] = Field(description="本场覆盖的因果节点 id 列表")
    goal: str = ""
    cliff: str = ""
