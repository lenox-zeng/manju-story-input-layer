"""交接契约：唯一的 Schema 来源 —— **v10.3**（向后兼容 v10.1 / v10.2）。

对应文档：`docs/漫剧文本输入契约_v10.3.md`（定稿）。

同时认 v10.1 / v10.2 / v10.3
-----------------------------
本模块**不再有 v9 兼容层**。v9 的 `mode` / `negative_prompt` / `ref_images` /
`controlnet` / `rife_enabled` / `dubit_enabled` / `ratio` / `edit_instruction` /
`scene.dialogue` / `scene.characters` / `scene.place` / `plan.constraints` /
`plan.style_ref_en`（顶层）一律**删除**，不是改名保留。

三层分离（契约 §1.1 / §1.3）
---------------------------
| 层 | 基类 | extra | 谁写 |
|---|---|---|---|
| A 业务事实 | `_ALayer` | `forbid`（未知字段 = block） | 编剧 / LLM |
| B 能力声明 `requirements` | `_BLayer` | `forbid` + 值域枚举 | 编剧可留空 |
| C 引擎提示 `hints` | `_CLayer` | `allow` | **只由适配层写**，不持久化 |

> `_ALayer` / `_BLayer` 用 `forbid` 是**故意的**：v9 事故的根因就是
> `extra="ignore"` 把契约新增字段静默吞掉，声明与执行不一致却无人报警。
> 现在任何未实现的契约字段都会当场报错。

`_Base`（`extra="ignore"`）只留给**非契约的内部交接**（qa_report / video /
manifest / shots_rendered）。那些是我们自己产出的中间件，不是业务资产，
收紧它们属于另一件事，别混进来。

命名约定：`plan.json / script.json / storyboard.json / shots_rendered.json /
video.json / qa_report.json / manifest.json`。
"""

from __future__ import annotations

import json
import types
from enum import Enum
from typing import Any, Literal, Union, get_args, get_origin

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    ValidationError,
    field_validator,
    model_validator,
)

__all__ = [
    "SCHEMA_VERSION",
    # 入口
    "RunRequest",
    # plan
    "Plan", "VisualBible", "OutputConfig", "Aspects", "CastSeparation",
    "SeparationViolation", "Continuity", "Character", "IdentityLock",
    "WardrobeState", "EpisodeBrief",
    # script
    "Script", "Scene", "SpaceEntry", "DialogueLine", "HookChain",
    # storyboard
    "Storyboard", "Shot", "ShotSegment", "Framing", "FramingSubject",
    "ShotSubject",
    # 能力声明
    "Requirements", "merge_requirements", "effective_requirements",
    # 枚举
    "AspectRatio", "DerivationStrategy", "ResolutionPolicy", "ShotSize",
    "Position", "HeightLevel", "Depth", "Facing", "SceneType",
    "CharacterStatus", "StageOverride", "FilmStock", "Lens",
    "IdentityFidelity", "WardrobeFidelity", "SpatialControl",
    "TemporalStability", "QualityTier", "MultiSubjectStrategy",
    "Interaction", "HandPrecision", "TextRender", "Interpolation", "Dubbing",
    # 内部交接
    "ShotRendered", "ShotsRendered", "VideoInfo", "QAIssue", "QAReport",
    "DeliveryManifest", "RouteTo",
    # 工具
    "HANDOFFS", "validate", "describe", "parse_json", "schema_error_text",
    "capability_defaults",
]

SCHEMA_VERSION = "v10.3"

# 契约 §5：visual_bible.forbidden 的基线。只增不减（校验器强制）。
FORBIDDEN_BASELINE: tuple[str, ...] = (
    "anime", "cartoon", "illustration", "painting", "manga",
    "comic", "3d render", "cgi", "chibi", "cel shading", "line art",
    "watercolor", "sketch", "plastic skin",
    "anime style", "manga style", "anime art", "cartoon style",
    "illustration style", "comic style", "cel-shaded", "toon",
    "动漫", "卡通", "漫画", "插画", "手绘", "赛璐璐", "三渲二",
)

# 契约 §4.1 的内置默认。`None` 表示"该键未声明"，由上一级或这里兜底。
CAPABILITY_DEFAULTS: dict[str, str] = {
    "identity_fidelity": "high",
    "wardrobe_fidelity": "high",
    "spatial_control": "none",
    "temporal_stability": "scene",
    "quality_tier": "standard",
    "multi_subject_strategy": "auto",
    "interaction": "none",
    "hand_precision": "default",
    "text_render": "none",
    "interpolation": "standard",
    "dubbing": "auto",
}


def capability_defaults() -> dict[str, str]:
    """契约 §4.1 的内置默认值（拷贝，防调用方改到全局）。"""
    return dict(CAPABILITY_DEFAULTS)


# ==========================================================================
# 基类
# ==========================================================================
class _Base(BaseModel):
    """**非契约**的内部交接模型：宽松，未知字段忽略。"""

    model_config = ConfigDict(extra="ignore", populate_by_name=True)


class _ContractBase(BaseModel):
    """三个契约层的公共配置。

    `validate_assignment=True` 是**故意的**：流水线里到处 `shot.duration_sec = per`
    这种就地赋值，不校验的话，一个赋值就能把合法对象改成非法对象，
    而所有校验器都已经跑完了 —— 又一条静默降级通道。
    """

    model_config = ConfigDict(
        extra="forbid", populate_by_name=True, validate_assignment=True
    )


class _ALayer(_ContractBase):
    """A 层业务事实。未知字段 = block（契约 §1.3）。"""


class _BLayer(_ContractBase):
    """B 层能力声明。未知能力键 = block（契约 §1.3）。"""


class _CLayer(BaseModel):
    """C 层引擎提示。**唯一**允许 `extra="allow"` 的一层（契约 §1.3）。"""

    model_config = ConfigDict(
        extra="allow", populate_by_name=True, validate_assignment=True
    )


class Hints(_CLayer):
    """C 层容器：引擎专属参数。

    **业务侧永远留空**；只由适配层写入，且**不持久化**（契约 §9.4）——
    换引擎时整体丢弃，不构成版本变更。
    """


# ==========================================================================
# 枚举
# ==========================================================================
class AspectRatio(str, Enum):
    R16_9 = "16:9"
    R9_16 = "9:16"


class DerivationStrategy(str, Enum):
    """契约 §6.3 四种派生策略。"""

    INDEPENDENT = "independent"
    REFRAME_CROP = "reframe_crop"
    REFRAME_RECOMPOSE = "reframe_recompose"
    CROP = "crop"


class ResolutionPolicy(str, Enum):
    ENGINE_DEFAULT = "engine_default"
    EXPLICIT = "explicit"


class ShotSize(str, Enum):
    EXTREME_WIDE = "extreme_wide"
    WIDE = "wide"
    FULL = "full"
    MEDIUM = "medium"
    MEDIUM_CLOSE = "medium_close"
    CLOSE_UP = "close_up"
    EXTREME_CLOSE_UP = "extreme_close_up"


class SubjectType(str, Enum):
    """本画幅的主体类别（v10.2）。

    为什么要它：2026-10-22 实测，声明 `close_up` 但首帧实际是人脸占比 <0.15 的
    中景时，LTX-2.3 会为了满足提示词而**镜内硬切**（SH02 峰值帧差 36）。但纯物件
    镜（SH26 靴子、SH30 手掌、SH01 刀）画面里本来就没有脸，人脸占比判据不适用。
    有了这个字段，首帧验收门就能区分"该有脸却没有"和"本来就不该有脸"。
    `None` = 旧分镜未声明，验收门跳过该镜。
    """

    CHARACTER = "character"
    OBJECT = "object"
    ENVIRONMENT = "environment"


class Position(str, Enum):
    """语义化水平位置 —— 替代 v9 的 0-1 浮点数 `x`（契约 §6.5 D1）。"""

    FAR_LEFT = "far_left"
    LEFT = "left"
    CENTER_LEFT = "center_left"
    CENTER = "center"
    CENTER_RIGHT = "center_right"
    RIGHT = "right"
    FAR_RIGHT = "far_right"


class HeightLevel(str, Enum):
    LOW = "low"
    MID = "mid"
    HIGH = "high"


class Depth(str, Enum):
    FOREGROUND = "foreground"
    MIDGROUND = "midground"
    BACKGROUND = "background"


class Facing(str, Enum):
    TOWARD_CAMERA = "toward_camera"
    AWAY = "away"
    PROFILE_LEFT = "profile_left"
    PROFILE_RIGHT = "profile_right"
    TOWARD_OTHER = "toward_other"


class SceneType(str, Enum):
    DIALOGUE = "dialogue"
    ACTION = "action"
    TRANSITION = "transition"
    MONTAGE = "montage"
    GROUP_DIALOGUE = "group_dialogue"


class CharacterStatus(str, Enum):
    ACTIVE = "active"
    RETIRED = "retired"
    DEAD = "dead"


class StageOverride(str, Enum):
    """契约 §8.1：值域收紧的封闭枚举。**禁止**引擎名/节点名/工作流名。"""

    DRAFT = "draft"
    STANDARD = "standard"
    HERO = "hero"
    SKIP = "skip"


class FilmStock(str, Enum):
    VISION3_500T = "Kodak Vision3 500T"
    PORTRA_400 = "Kodak Portra 400"
    ETERNA_500T = "Fuji Eterna 500T"
    CINESTILL_800T = "CineStill 800T"
    HP5_BW = "Ilford HP5 B&W"


class Lens(str, Enum):
    W24 = "24mm wide"
    MM35 = "35mm"
    MM50 = "50mm"
    MM85 = "85mm portrait"
    ANAMORPHIC35 = "35mm anamorphic"


# -- B 层能力值域（契约 §4.1）---------------------------------------------
class IdentityFidelity(str, Enum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    LOCKED = "locked"


class WardrobeFidelity(str, Enum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


class SpatialControl(str, Enum):
    NONE = "none"
    LOOSE = "loose"
    STRICT = "strict"


class TemporalStability(str, Enum):
    NONE = "none"
    SHOT = "shot"
    SCENE = "scene"
    SERIES = "series"


class QualityTier(str, Enum):
    DRAFT = "draft"
    STANDARD = "standard"
    HERO = "hero"


class MultiSubjectStrategy(str, Enum):
    AUTO = "auto"
    JOINT = "joint"
    PER_SUBJECT = "per_subject"
    HYBRID = "hybrid"


class Interaction(str, Enum):
    NONE = "none"
    CONTACT = "contact"
    ENTANGLED = "entangled"


class HandPrecision(str, Enum):
    DEFAULT = "default"
    HERO = "hero"


class TextRender(str, Enum):
    NONE = "none"
    SIMPLE = "simple"
    DENSE = "dense"


class Interpolation(str, Enum):
    NONE = "none"
    STANDARD = "standard"
    HIGH = "high"


class Dubbing(str, Enum):
    NONE = "none"
    AUTO = "auto"
    LIPSYNC = "lipsync"


# 非契约的内部枚举（保持原样）
class RouteTo(str, Enum):
    """质检问题的回退目标 —— 决定显存闸门往哪个模式切。"""

    PLANNER = "planner"
    WRITER = "writer"
    STORYBOARD = "storyboard"
    RENDERER = "renderer"
    AV = "av"


# ==========================================================================
# 0) 入口需求
# ==========================================================================
class RunRequest(_Base):
    """主编收到的原始需求。

    字段刻意做宽：用户在 `input.example.json` 里已经写了一版更丰富的需求单
    （`must_have` / `avoid` / 自带角色设定 / `limits`），那些是有价值的创作约束，
    不能因为"不在我定义的 Schema 里"就被丢掉。多余的键由 `extra="ignore"` 兜住。

    **本模型不是契约资产（v10.1 / v10.2 均不含它）** —— 它是人手写的入口单，
    跑起来之后才会被编译成 `plan.json`。所以这里仍然允许 `ratio` 这类口语字段。
    """

    title: str = Field(default="", description="作品名（中文）；留空则由创意里取名")
    idea: str = Field(description="一句话到一段话的创意/题材描述")
    genre: str = Field(default="玄幻", description="类型：玄幻/都市/悬疑/喜剧…")
    episodes: int = Field(default=1, ge=1, le=500, description="要生产的集数")
    target_duration_sec: int = Field(default=60, ge=5, le=600, description="单集目标时长（秒）")
    ratio: str = Field(default="16:9", description="画幅，如 16:9 / 9:16")
    style_ref: str = Field(default="电影感写实，柔和自然光，浅景深", description="视觉风格（中文）")
    audience: str = Field(default="18-35 岁短视频观众", description="目标受众")
    notes: str = Field(default="", description="额外要求（可留空）")

    # -- 用户可选的额外约束（有就会被认真对待）----------------------------
    characters: list[dict[str, Any]] = Field(
        default_factory=list,
        description='用户指定的角色，形如 [{"name","role","appearance","consistency_note"}]',
    )
    must_have: list[str] = Field(default_factory=list, description="必须出现的桥段/元素")
    avoid: list[str] = Field(default_factory=list, description="必须避免的内容")
    limits: dict[str, Any] = Field(default_factory=dict, description="制作限制（显存/轮次/工作流名）")
    assets_dir: str = Field(default="", description="素材目录（可选）")
    output_dir: str = Field(default="", description="输出目录（可选，默认 output/<run_id>）")

    def resolved_title(self) -> str:
        t = (self.title or "").strip()
        if t:
            return t
        return (self.idea or "未命名")[:16]

    def constraints_for_planner(self) -> dict[str, Any]:
        """把用户约束整理成给模型看的紧凑对象。"""
        out: dict[str, Any] = {
            "画幅": self.ratio,
            "单集目标时长秒": self.target_duration_sec,
            "集数": self.episodes,
            "受众": self.audience,
            "风格": self.style_ref,
        }
        if self.must_have:
            out["必须包含"] = self.must_have
        if self.avoid:
            out["必须避免"] = self.avoid
        if self.characters:
            out["用户已指定角色（务必沿用其姓名与设定）"] = self.characters
        if self.notes:
            out["补充说明"] = self.notes
        return out


# ==========================================================================
# 1) 通用能力声明（契约 §4）
# ==========================================================================
class Requirements(_BLayer):
    """B 层能力声明 —— 只写"要多像、要多稳"，**不含任何实现方式**。

    可出现在 plan 级 / script 级 / storyboard 镜头级。合并语义见
    `merge_requirements()`：**键级合并，缺省继承最近上级，禁止整体替换**（§4.2）。

    全部字段可省略。`None` 与"未声明"等价（都会被上一级覆盖）。
    """

    identity_fidelity: IdentityFidelity | None = Field(
        default=None, description="与角色参考的相似度；locked=必须与参考完全一致"
    )
    wardrobe_fidelity: WardrobeFidelity | None = Field(
        default=None, description="服装与 wardrobe_state 描述的一致度"
    )
    spatial_control: SpatialControl | None = Field(
        default=None, description="是否严格按 framing.subjects 摆放人物"
    )
    temporal_stability: TemporalStability | None = Field(
        default=None, description="稳定度范围：镜内 / 镜间 / 全集 / 全剧"
    )
    quality_tier: QualityTier | None = Field(
        default=None, description="质量档位，直接影响成本"
    )
    multi_subject_strategy: MultiSubjectStrategy | None = Field(
        default=None,
        description="多人同框策略：joint=整体一次 / per_subject=逐人再合成 / hybrid=混合。"
                    "仅 plan 级允许 auto；同框 ≥2 人的镜头**必须显式填写**",
    )
    interaction: Interaction | None = Field(
        default=None, description="人物间肢体接触强度；entangled=缠斗/拥抱，难度最高"
    )
    hand_precision: HandPrecision | None = Field(
        default=None, description="手部是否为画面重点（手部特写、持物特写）"
    )
    text_render: TextRender | None = Field(
        default=None, description="画面内是否需渲染文字（招牌、屏幕、书信）"
    )
    interpolation: Interpolation | None = Field(
        default=None, description="是否需要补帧及其档位"
    )
    dubbing: Dubbing | None = Field(
        default=None, description="是否需要配音处理；lipsync=还要求口型对齐"
    )


def _declared(req: Requirements | None) -> dict[str, str]:
    """取出**显式声明**的能力键（值为 None 视同未声明）。"""
    if req is None:
        return {}
    out: dict[str, str] = {}
    for key in CAPABILITY_DEFAULTS:
        val = getattr(req, key, None)
        if val is None:
            continue
        out[key] = val.value if isinstance(val, Enum) else str(val)
    return out


def merge_requirements(
    *, shot: Requirements | None = None,
    script: Requirements | None = None,
    plan: Requirements | None = None,
) -> dict[str, str]:
    """键级合并（契约 §4.2）。

    按 `镜头级 → script 级 → plan 级 → 内置默认` **逐键**取第一个出现的值。

    **禁止整体替换** —— 镜头级只写 `quality_tier` 时，plan 级的
    `identity_fidelity: locked` 必须继续生效。这是 v9 静默降级事故的机制性解药。

    刻意用**关键字参数**：位置参数在这里等于把三层顺序交给调用点去记，
    写反了不会报错、只会静默降级 —— 正是本契约要消灭的东西。
    """
    merged = capability_defaults()
    for layer in (plan, script, shot):      # 从最低优先级往上盖
        merged.update(_declared(layer))
    return merged


def effective_requirements(
    *, shot: Requirements | None = None,
    script: Requirements | None = None,
    plan: Requirements | None = None,
) -> dict[str, tuple[str, str]]:
    """生效链（契约 §4.3）：每个能力键的**最终值 + 来源层级**。

    返回 `{键: (值, 来源)}`，来源取值 `shot` / `script` / `plan` / `default`。
    适配层应把这份记录写进**运行时报告**（不写入资产文件，§9.4），
    用于机械拦截"声明 `locked` 却按 `high` 执行"。
    """
    chain: dict[str, tuple[str, str]] = {}
    for src, layer in (("shot", shot), ("script", script), ("plan", plan)):
        for key, val in _declared(layer).items():
            # setdefault 而不是赋值：本函数按 **优先级从高到低** 遍历
            # （镜头级 → script 级 → plan 级），必须先到先得。写成赋值就是
            # plan 覆盖 shot，与 merge_requirements 的语义正好相反 ——
            # 而 `shot_caps` 直接拿这条链当实际取值喂渲染器，等于把镜头级
            # 的 `dubbing: lipsync` / `multi_subject_strategy: per_subject`
            # 静默降级成 plan 级的值（§9.3.3 反模式）。
            chain.setdefault(key, (val, src))
    for key, val in capability_defaults().items():
        chain.setdefault(key, (val, "default"))
    return chain


# ==========================================================================
# 2) plan.json v10.2（系列圣经）
# ==========================================================================
class VisualBible(_ALayer):
    """全片画风的**唯一来源**（契约 §3 铁律 1 / §5.2）。

    其他文件（尤其 `storyboard.style_suffix`）的任何画风描述均不生效。
    这些槽位是**摄影事实**，不是渲染参数，因此不违反命名禁令（§1.2 例外段）。
    """

    medium: Literal["photorealistic cinematic film still"] = Field(
        description="媒介，锁死为此值，禁止改"
    )
    film_stock: FilmStock = Field(description="胶片型号（受限枚举）")
    lens: Lens = Field(description="镜头（受限枚举）")
    lighting: str = Field(default="", description="光线，摄影术语")
    palette: str = Field(default="", description="色彩，摄影/调色术语")
    texture: str = Field(default="", description="材质与颗粒")
    contrast: str = Field(default="", description="对比度")
    skin_tone: str = Field(default="", description="肤色与皮肤质感")
    motion_character: str = Field(default="", description="运动的质感（手持抖动等）")
    depth_style: str = Field(default="", description="景深风格")
    forbidden: list[str] = Field(
        default_factory=lambda: list(FORBIDDEN_BASELINE),
        description="禁止出现的画风词（中英文都列）。**只能在此基础上追加，不能删减**。"
                    "适配层据此为支持负面条件的引擎编译 negative 条件",
    )
    style_ref_en: str | None = Field(
        default=None,
        description="可选逃生舱：文本式画风串。不填=由适配层从槽位编译。"
                    "填了仍须过 forbidden 扫描",
    )

    @field_validator("forbidden")
    @classmethod
    def _no_weakening(cls, v: list[str]) -> list[str]:
        missing = [w for w in FORBIDDEN_BASELINE if w not in v]
        if missing:
            raise ValueError(
                f"forbidden 只能追加不能删减，缺少契约基线词：{missing[:8]}"
                f"{' …' if len(missing) > 8 else ''}"
            )
        return v


class Aspects(_ALayer):
    """画幅配置（契约 §6.2）。"""

    enabled: list[AspectRatio] = Field(
        default_factory=lambda: [AspectRatio.R16_9],
        description="启用哪些画幅，如 [\"16:9\",\"9:16\"]",
    )
    primary: AspectRatio = Field(default=AspectRatio.R16_9, description="主画幅")
    derivation: dict[str, DerivationStrategy] = Field(
        default_factory=dict,
        description="非主画幅的派生策略：{\"9:16\":\"reframe_crop\"}。"
                    "independent=独立生成 / reframe_crop=扩边重排（不换景别机位、不增减角色）/ "
                    "reframe_recompose=允许局部重绘换机位 / crop=直接裁切",
    )
    resolution_policy: ResolutionPolicy = Field(
        default=ResolutionPolicy.ENGINE_DEFAULT, description="分辨率策略"
    )
    min_resolution: dict[str, tuple[int, int]] | None = Field(
        default=None,
        description="各画幅的**业务可接受下限**（像素），如 {\"16:9\":[1280,720]}。"
                    "这是验收阈值不是渲染参数，是 A 层唯一允许出现像素值的地方（§1.2 例外）",
    )

    @model_validator(mode="after")
    def _consistent(self) -> Aspects:
        if not self.enabled:
            raise ValueError("aspects.enabled 不能为空")
        if self.primary not in self.enabled:
            raise ValueError(f"primary={self.primary.value!r} 不在 enabled 里")
        valid = {a.value for a in AspectRatio}
        for k in self.derivation:
            if k not in valid:
                raise ValueError(f"derivation 的键 {k!r} 不是合法画幅，可用 {sorted(valid)}")
            if k not in {a.value for a in self.enabled}:
                raise ValueError(f"derivation 的键 {k!r} 不在 enabled 里")
        if self.primary.value in self.derivation:
            raise ValueError(
                f"derivation 不应包含主画幅 {self.primary.value!r}（主画幅是源，不派生）"
            )
        for k in (self.min_resolution or {}):
            if k not in {a.value for a in self.enabled}:
                raise ValueError(f"min_resolution 的键 {k!r} 不在 enabled 里")
        return self


class OutputConfig(_ALayer):
    aspects: Aspects = Field(description="画幅配置")
    simultaneous: bool = Field(default=True, description="一次批跑是否同时产出全部启用画幅")
    episode_sec: float = Field(default=90, gt=0, description="单集目标时长（秒）")
    target_platform: str = Field(default="", description="目标平台，如 抖音/B站")


class SeparationViolation(_ALayer):
    pair: list[str] = Field(description="角色对，如 [\"C1\",\"C2\"]")
    missing: list[str] = Field(default_factory=list, description="缺失的区分维度")
    severity: Literal["block", "warn"] = Field(
        default="warn", description="引用完整性问题用 block，审美类一律 warn"
    )


class CastSeparation(_ALayer):
    """防串脸第一道防线：角色区分度（契约 §5 / §10.2）。"""

    co_occurrence_rule: str = Field(
        description="『存在同框机会的角色对』的可执行定义。校验器扫描全部 script.json 的 "
                    "chars_on_screen（**非** lines.speaker）构建同框图，仅对图中的边校验"
    )
    rule: str = Field(description="存在同框机会的角色对必须在至少 3 个维度上明显不同")
    axes: list[str] = Field(default_factory=list, description="区分维度清单")
    violations: list[SeparationViolation] = Field(default_factory=list)


class Continuity(_ALayer):
    immutable: list[str] = Field(default_factory=list, description="全剧不可变的字段")
    mutable: list[str] = Field(default_factory=list, description="可变的字段")
    policy: str = Field(default="", description="变更政策")


class IdentityLock(_ALayer):
    """不可变特征（契约 §3 铁律 3）。

    **只写身体固有特征与永久佩戴物**（义肢、永久手套）。
    可更换的服装一律禁止写入本字段 —— 服装归 `wardrobe_states`。
    """

    age_range: str = Field(default="", description="年龄区间，如 23-25")
    face_shape: str = Field(default="", description="脸型与骨相")
    hair: str = Field(default="", description="发型发色")
    eyes: str = Field(default="", description="眼型瞳色")
    build: str = Field(default="", description="体型")
    height_cm: int | None = Field(default=None, ge=0, description="身高（厘米）")
    silhouette_cue: str = Field(
        default="", description="剪影识别特征。**禁写服装**（永久佩戴物除外）"
    )
    facial_marks: str = Field(default="", description="面部永久标记（疤、痣）")
    skin: str = Field(default="", description="肤色与皮肤质感")


class WardrobeState(_ALayer):
    state_id: str = Field(description="状态 id，如 C1-default / C1-combat")
    desc_en: str = Field(description="英文服装描述，直接进图像条件")
    tags: list[str] = Field(
        default_factory=list,
        description="语义标签（如 日常/战斗/第2阶段）。**tags 是权威**",
    )
    advisory_episodes: str | None = Field(
        default=None,
        description="建议适用集数，如 \"41-70\"。**仅 info 级提示**——闪回/剧集重排下"
                    "不匹配属正常，不作告警",
    )


class EpisodeBrief(_ALayer):
    num: int = Field(ge=1, description="集号")
    title: str = Field(description="集名")
    summary: str = Field(description="本集梗概")
    hook: str = Field(default="", description="开篇钩子")


class Character(_ALayer):
    """主要角色。

    `desc_en` 是**跨镜头一致性的锚点**：出图前先用它生成一张「角色定妆图」，
    之后每个镜头都以该定妆图作参考图，人物不会每镜换脸。
    """

    char_id: str = Field(description="短 id，如 C1，后续镜头用它引用")
    name: str = Field(description="角色名")
    role: str = Field(default="主角", description="主角/配角/反派…")
    status: CharacterStatus = Field(
        default=CharacterStatus.ACTIVE,
        description="active / retired / dead。退场角色保留供闪回复用，不删除",
    )
    desc_cn: str = Field(description="中文外貌与服装描述，供人核对")
    desc_en: str = Field(
        description="英文外貌与服装描述，直接进图像提示词，越具体越好。"
                    "**禁止出现任何场景/背景词**（alley/street/室内/雨…），场景归 storyboard.prompt"
    )
    identity_lock: IdentityLock = Field(
        description="不可变特征（脸/发型/瞳色/体型/永久标记）。服装不写在这里"
    )
    wardrobe_states: list[WardrobeState] = Field(
        default_factory=list, description="服装状态库，按 state_id 被 script 引用"
    )
    signature_props: list[str] = Field(default_factory=list, description="标志性道具")
    color_signature: str = Field(default="", description="角色主色，如 #1B3A5C")
    voice_ref: str = Field(default="", description="音色参考描述（中文）")
    pose_library: list[str] = Field(
        default_factory=list,
        description="P1 姿态库（可选）：3-5 个命名姿态英文短语，跨镜逐字复用，"
                    "如 'hands clasped at chest'。分镜 LLM 的 pose 从库里取，保证表演一致",
    )
    expression_library: list[str] = Field(
        default_factory=list,
        description="P1 表情库（可选）：3-5 个命名表情英文短语，跨镜逐字复用，"
                    "如 'brow furrowed, lips tight'。分镜 LLM 的 expression 从库里取",
    )

    @model_validator(mode="after")
    def _wardrobe_ids_unique(self) -> Character:
        ids = [w.state_id for w in self.wardrobe_states]
        dup = {i for i in ids if ids.count(i) > 1}
        if dup:
            raise ValueError(f"{self.char_id} 的 wardrobe_states.state_id 重复：{sorted(dup)}")
        return self


class Plan(_ALayer):
    """① 策划 → ② 编剧。全剧唯一一份（系列圣经）。"""

    schema_version: Literal["v10.1", "v10.2", "v10.3"] = Field(
        description="契约版本。v10.3 只新增可选字段，v10.1 / v10.2 继续可用"
    )
    plan_version: int = Field(default=1, ge=1, description="设定版本；identity_lock 变更必须 +1")
    scope: Literal["series"] = Field(default="series", description="固定 series（全剧唯一一份）")

    project: str = Field(description="作品名")
    genre: str = Field(description="类型")
    logline: str = Field(description="一句话故事")
    target_audience: str = Field(description="受众")
    total_episodes: int = Field(default=1, ge=1, description="总集数")

    visual_bible: VisualBible = Field(description="画风圣经 —— 全片画风唯一来源")
    output: OutputConfig = Field(description="画幅与成片配置")
    requirements: Requirements = Field(
        default_factory=Requirements, description="全剧能力声明默认值（可被 script / 镜头覆盖）"
    )
    cast_separation: CastSeparation = Field(description="角色区分度规则（防串脸第一道防线）")

    characters: list[Character] = Field(min_length=1, description="主要角色表（1-6 个）")
    episodes: list[EpisodeBrief] = Field(min_length=1, description="分集梗概")
    continuity: Continuity = Field(default_factory=Continuity, description="连续性与变更政策")

    hints: Hints = Field(default_factory=Hints, description="C 层引擎提示，业务侧留空")

    @field_validator("characters")
    @classmethod
    def _ids_unique(cls, v: list[Character]) -> list[Character]:
        ids = [c.char_id for c in v]
        if len(ids) != len(set(ids)):
            raise ValueError("char_id 必须唯一")
        return v


# ==========================================================================
# 3) script.json v10.2（每集）
# ==========================================================================
class SpaceEntry(_ALayer):
    """本场基准站位（同场跨镜沿用；单镜走位变化写 storyboard.framing）。"""

    pos: Position = Field(description="语义化水平位置")
    depth: Depth = Field(default=Depth.MIDGROUND, description="前后景")
    facing: Facing | None = Field(default=None, description="朝向")
    note: str = Field(default="", description="备注，如 距巷口约2米")


class NarrationLine(_ALayer):
    """人声旁白/内心独白：叠在画面上，不做口型。

    speaker=NARRATOR 表示解释性旁白；C# 表示该角色的内心独白。
    与 lines 分开，避免 TTS/字幕/口型把旁白当对白处理。
    """

    kind: Literal["exposition", "inner"] = Field(
        description="exposition=解释性旁白（NARRATOR）；inner=角色内心独白（C#）")
    text: str = Field(description="旁白文本")
    speaker: str = Field(default="NARRATOR",
                         description="NARRATOR 或 char_id，如 C1")
    delivery: str = Field(default="", description="语气/语速提示，给 TTS 用")
    timing_sec: float | None = Field(
        default=None, gt=0, description="该条旁白预期时长（秒），必须能装下文本"
                                        "（≥ceil(汉字数/5)）")
    at_sec: float | None = Field(
        default=None, ge=0, description="该条旁白在成片时间轴上的绝对起点（秒）")
    # ---- v10.3 锚点字段（旁白时间契约）：旁白唯一权威在 storyboard.shots[].narration ----
    anchor_mode: Literal["within_shot", "voiceover_span"] | None = Field(
        default=None,
        description="旁白必须完整落在本镜内，还是允许跨后续镜头（v10.3 必填语义）")
    shot_id: str | None = Field(
        default=None,
        description="旁白锚定的镜头 id；必须是真实存在的 shot（v10.3 必填语义）")
    allow_span_shots: int | None = Field(
        default=None, ge=1,
        description="仅 voiceover_span 使用：最多跨几个镜头（含锚镜，默认 1）")
    allow_cross_scene: bool | None = Field(
        default=None,
        description="是否允许跨场景；默认禁止（v10.3）")
    max_shift_sec: float | None = Field(
        default=None, ge=0,
        description="允许实现层前后微调的最大秒数（默认 3.0）")

    @model_validator(mode="after")
    def _speaker_ok(self) -> NarrationLine:
        if self.kind == "exposition" and self.speaker != "NARRATOR":
            raise ValueError("解释性旁白的 speaker 必须是 NARRATOR")
        if self.kind == "inner" and not self.speaker.startswith("C"):
            raise ValueError("内心独白的 speaker 必须是角色 id（C1 格式）")
        return self


class SceneTransition(_ALayer):
    """场间转场：下一场首镜如何接住上一场。"""

    type: Literal["match_cut", "sound_bridge", "action_bridge",
                  "time_jump", "fade", "dissolve"]
    anchor: str = Field(default="", description="共享物件/动作/声音/时间落点")
    note: str = Field(default="", description="转场说明")


class DialogueLine(_ALayer):
    """**一句**台词（句级，而不是整场一坨）。

    为什么必须句级：① 字幕——整场一坨会烧成一条跨 22s 的巨幕；② 配音——TTS 要逐句
    生成并按句排时间轴；③ 口型校验要拿"句"的能量包络去对帧。
    """

    speaker: str = Field(description="说话人：char_id，如 C1")
    text: str = Field(description="台词文本")
    emotion: str = Field(default="", description="语气提示，给 TTS 用，如 颤声/冷笑/咬牙")
    timing_sec: float | None = Field(
        default=None, gt=0,
        description="该句预期时长（秒）。不给则按本场语速估算（默认 4.5 字/秒）",
    )
    at_sec: float | None = Field(
        default=None, ge=0,
        description="该句在**成片时间轴**上的绝对起点（秒，全片坐标）。默认逐句顺排；"
                    "对白镜需要钉到特定镜头时填 说话人所在镜头的 start_sec + 0.5~1.0",
    )


def _valid_flags(v: list[str]) -> list[str]:
    """`character_flags` 取值校验（契约 §7）。"""
    for item in v:
        if item in ("flashback", "resurrected", "vision"):
            continue
        if item.startswith("disguise:"):
            target = item.split(":", 1)[1].strip()
            if not target:
                raise ValueError(
                    "disguise 必须带参：写成 \"disguise:C2\"（易容成 C2）。"
                    "不带参则适配层不知该用哪份参考图"
                )
            continue
        raise ValueError(
            f"character_flags 取值 {item!r} 非法。可用：flashback / resurrected / vision "
            "/ disguise:<char_id>"
        )
    return v


class Scene(_ALayer):
    scene_id: str = Field(description="场景 id，如 S01")
    location: str = Field(default="", description="地点")
    time: str = Field(default="", description="日/夜/黄昏")
    action: str = Field(description="动作与画面描写（中文）：看得见的画面，不写心理活动")
    scene_type: list[SceneType] = Field(
        default_factory=list,
        description="数组标签：dialogue / action / transition / montage / group_dialogue。"
                    "含 action 或 group_dialogue → 默认质量档位提升",
    )
    space_map: dict[str, SpaceEntry] = Field(
        default_factory=dict, description="本场基准站位：char_id → 位置/景深/朝向"
    )
    character_flags: dict[str, list[str]] = Field(
        default_factory=dict,
        description="角色特殊出场语义：{\"C1\":[\"flashback\"],\"C2\":[\"disguise:C1\"]}。"
                    "取值 flashback / resurrected / vision / disguise:<char_id>。"
                    "闪回或幻象场不计入 continuity_ledger 时间线",
    )
    chars_on_screen: list[str] = Field(
        default_factory=list,
        description="本场实际出场角色（**含无台词者**）。有人在场没台词也必须列出，"
                    "否则出图会漏人。这也是 cast_separation 同框判定的**唯一依据**",
    )
    wardrobe_state: dict[str, str] = Field(
        default_factory=dict,
        description="本场各角色穿的服装态：{\"C1\":\"C1-default\"}。"
                    "state_id 必须存在于 plan 对应角色的状态库",
    )
    speech_cps: float | None = Field(
        default=None, gt=0, description="本场语速覆盖（字/秒）。不填=默认 4.5"
    )
    lines: list[DialogueLine] = Field(
        default_factory=list,
        description="句级台词。字幕 / TTS / 口型三处共用这份时间轴，是唯一事实来源",
    )
    narration: list[NarrationLine] = Field(
        default_factory=list,
        description="本场旁白/内心独白。与 lines 分开：不做口型，可叠在任意镜头画面上。"
                    "旧 v10.2 产物没有该字段时为空。",
    )
    transition_in: SceneTransition | None = Field(
        default=None,
        description="上一场如何接进本场；第 1 场可空。用于避免无动机硬切。",
    )
    mood: str = Field(default="", description="氛围（同时用于 BGM 选曲的情绪标签）")
    duration_sec: float = Field(gt=0, description="本场时长（秒）")
    beat: str = Field(default="", description="本场节拍：钩子/升级/期待/爽点/结尾钩子 等")
    function: str = Field(default="", description="本场功能说明（这十几秒观众情绪该被推到哪）")

    @field_validator("character_flags")
    @classmethod
    def _flags_ok(cls, v: dict[str, list[str]]) -> dict[str, list[str]]:
        for _, items in v.items():
            _valid_flags(list(items))
        return v

    @model_validator(mode="after")
    def _wardrobe_targets_on_screen(self) -> Scene:
        stray = [c for c in self.wardrobe_state if c not in self.chars_on_screen]
        if stray and self.chars_on_screen:
            raise ValueError(
                f"{self.scene_id} 的 wardrobe_state 给了不在 chars_on_screen 里的角色：{stray}"
            )
        return self


class HookChain(_ALayer):
    from_ep: int = Field(default=0, ge=0, description="钩子来自第几集（0=开场）")
    to_ep: int = Field(default=0, ge=0, description="指向第几集")
    thread: str = Field(default="", description="贯穿线索")


class Script(_ALayer):
    """② 编剧 → ③ 分镜。每集一份。"""

    schema_version: Literal["v10.1", "v10.2", "v10.3"] = Field(
        description="契约版本。v10.3 只新增可选字段，v10.1 / v10.2 继续可用"
    )
    series_ref: str = Field(default="plan.json", description="指向系列圣经")
    episode: int = Field(ge=1)
    title: str = Field(default="", description="本集标题")

    aspects_override: dict[str, Any] | None = Field(
        default=None, description="按集覆盖画幅配置（结构与 plan.output.aspects 同形）。null=不覆盖"
    )
    requirements: Requirements = Field(
        default_factory=Requirements, description="本集能力声明（键级覆盖 plan 级）"
    )

    scenes: list[Scene] = Field(min_length=1)
    continuity_ledger: dict[str, dict[str, Any]] = Field(
        default_factory=dict,
        description="本集末态账本：char_id → {伤势/换装/获得道具/关系变化…}。"
                    "跨集校验要求 ep_N 末态 == ep_{N+1} 初态；闪回/幻象场不计入",
    )
    hook_chain: HookChain | None = Field(default=None, description="钩子链")
    hints: Hints = Field(default_factory=Hints, description="C 层引擎提示，业务侧留空")

    @property
    def total_duration(self) -> float:
        return sum(s.duration_sec for s in self.scenes)


# ==========================================================================
# 4) storyboard.json v10.2（每集）
# ==========================================================================
class ShotSegment(_ALayer):
    """长镜头的一个分段。

    每段要**自己的画面描述** —— 否则一段 prompt 复制 n 份，每段都试图演完全部事件，
    内容打架、接缝必闪帧。与 `prompt` 冲突时以 `prompt` 为准（§8.2）。

    v10.2 阶段化扩展（2026-10-30，全部可选，旧文件继续可用）：
    一段一个主动作；段区间连续不重叠、段秒数和 = 整镜 duration_sec；
    段间用 state_out → 下一段 state_in 承接。引擎映射走配置：
    LTX → frame_hint 中段关键帧注入（LTXVAddGuide）；Seedance → 首段拼 prompt、
    末段 keyframe=true 做尾帧（return_last_frame）；万相/可灵 → 时间戳多镜头。
    """

    text: str = Field(description="该段画面描述（英文，单一动作/状态，别写跨段的多事件）")
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
    source: str | None = Field(
        default=None,
        description="段来源：'fallback'=compile 确定性动作组拆分（非逐动作语义拆分，"
                    "审计豁免一段一动作语义检查）；缺省/None=分镜层 LLM 生成")


class FramingSubject(_ALayer):
    """某画幅内一个角色的**摄影事实**（站位/景深/朝向/遮挡）。

    全部是"人类场记本能理解"的事实，不是引擎参数 —— 这是 `pos` 取代 v9 浮点 `x`
    的全部理由（§6.5 D1）。
    """

    char: str = Field(description="char_id")
    pos: Position = Field(description="语义化水平位置（替代 v9 的 0-1 浮点数）")
    height_level: HeightLevel = Field(
        default=HeightLevel.MID, description="垂直层：表达阶梯站位、高低落座、俯拍分布"
    )
    depth: Depth = Field(default=Depth.MIDGROUND, description="前后景")
    facing: Facing | None = Field(default=None, description="朝向")
    occludes: list[str] = Field(
        default_factory=list,
        description="**只填直接被遮挡者**。适配层递归展开完整遮挡链；禁止循环",
    )
    offscreen: bool = Field(
        default=False, description="画外角色（有声音/被提及但不在画面）"
    )
    mirrored: bool = Field(default=False, description="镜面/倒影中的虚像")


class CameraRecipe(_ALayer):
    """P0.5 摄影配方（可选，v10.4 新槽位，旧产物可读）。

    只写本镜**摄影事实**，不写画风（画风唯一来源是 VisualBible）。
    ``light_key`` 一句话给全"光源方向+质量+色温"，引擎不给光照就默认平光。
    """

    lens_mm: str = Field(
        default="", description="焦段枚举：24/35/50/85。对话镜 35-50、特写 50-85、环境 24-35"
    )
    depth_of_field: str = Field(
        default="", description="景深：shallow / moderate / deep"
    )
    light_key: str = Field(
        default="",
        description="光源方向+质量+色温一句话，如 'soft warm key from camera left, "
                    "gentle cool fill, subtle rim on hair'",
    )
    light_quality: str = Field(
        default="", description="光照质感：diffused / hard / atmospheric"
    )
    composition: str = Field(
        default="",
        description="构图槽位（可选）：headroom/三分法/居中，写则必须可辨识",
    )


class Framing(_ALayer):
    """**某一个画幅**的独立构图。

    `framing` 是 `{画幅: Framing}` 的字典 —— 16:9 与 9:16 各有一套，
    因为竖屏不是横屏切一刀（§6.1）。
    """

    shot_size: ShotSize = Field(description="景别")
    subject_type: SubjectType | None = Field(
        default=None,
        description="本画幅主体类别（v10.2，可选）：character / object / environment。"
                    "None=旧分镜未声明。物件镜与环境镜不做人脸占比校验，"
                    "避免把 SH26（靴子）/SH30（手掌）这种正确镜头当成首帧偏差",
    )
    composition: str = Field(description="人类可读的构图描述（中文）")
    overlap_order: list[str] = Field(
        default_factory=list,
        description="完整前后遮挡顺序（前 → 后）。交叠 ≥3 人时建议填写；"
                    "2 人时 occludes 已可唯一确定，也可填写，但必须与之一致",
    )
    subjects: list[FramingSubject] = Field(
        default_factory=list, description="本画幅内每个角色的站位"
    )
    camera_recipe: CameraRecipe | None = Field(
        default=None,
        description="P0.5 摄影配方槽位（可选）。None=旧产物未声明；"
                    "填写时 lens_mm/depth_of_field/light_key 至少其一非空",
    )

    @model_validator(mode="after")
    def _occlusion_consistent(self) -> Framing:
        chars = [s.char for s in self.subjects]
        if len(chars) != len(set(chars)):
            raise ValueError(f"subjects 里 char 重复：{chars}")

        # occludes 只能指向本画幅内的角色
        for s in self.subjects:
            stray = [c for c in s.occludes if c not in chars]
            if stray:
                raise ValueError(f"{s.char} 的 occludes 指向不在本画幅的角色：{stray}")

        # 遮挡环（A 遮 B、B 遮 A）
        edges = {s.char: set(s.occludes) for s in self.subjects}
        for a, targets in edges.items():
            for b in targets:
                if a in edges.get(b, ()):
                    raise ValueError(f"occludes 出现循环：{a} ↔ {b}")

        # overlap_order 与 occludes 必须一致（§6.5 优先级规则）
        if self.overlap_order:
            if len(self.overlap_order) != len(set(self.overlap_order)):
                raise ValueError(f"overlap_order 有重复：{self.overlap_order}")
            stray = [c for c in self.overlap_order if c not in chars]
            if stray:
                raise ValueError(f"overlap_order 里的角色不在 subjects 里：{stray}")
            idx = {c: i for i, c in enumerate(self.overlap_order)}
            for s in self.subjects:
                if s.char not in idx:
                    continue
                for t in s.occludes:
                    if idx[t] < idx[s.char]:
                        raise ValueError(
                            f"overlap_order 与 occludes 矛盾：{s.char} 遮挡 {t}，"
                            f"但 overlap_order 里 {t} 排在 {s.char} 前面"
                        )
        return self


class ShotSubject(_ALayer):
    """逐角色的姿态与表情。必须覆盖 `ref_characters` 全部角色。"""

    char: str = Field(description="char_id")
    wardrobe_state: str = Field(
        default="", description="本镜该角色穿的服装态 id，须与 script 同场 wardrobe_state 一致"
    )
    pose: str = Field(default="", description="姿态（英文），如 right hand raised, palm out")
    expression: str = Field(default="", description="表情（英文），如 cold calm")
    speaking: bool | None = Field(
        default=None,
        description="本镜该角色是否开口说话（v10.2，可选）。"
                    "对白镜应至少有一个 True，且 prompt/motion 要带开口动作——"
                    "LatentSync 只能放大已有的嘴部动作，不能凭空造（2.3 原片嘴部"
                    "信号 std 0.020 vs 2.5 0.067）。None=旧分镜未声明，门不检查",
    )


class Continuity(_ALayer):
    """镜间连续性（A 层业务事实）。

    为什么单独一层：分镜此前只有"这一镜画什么"，没有"上一镜留下了什么"。
    于是人物位置、道具在哪只手、光色、屏幕方向每镜各写各的，成片就是
    "每帧都能动、但接不成戏"。这些字段**不参与引擎参数**，只用于：
      1. 编译进提示词（站位/光色）；
      2. 校验器检查相邻镜是否自相矛盾；
      3. 返工时知道这一镜的起止状态。
    """

    relation: str = Field(
        default="", description="与上一镜的关系：continuation / reverse / cutaway / "
                                "insert / new_scene")
    screen_direction: str = Field(
        default="", description="人物/运动在画面里的方向：left_to_right / "
                                "right_to_left / toward_camera / away")
    prev_state: str = Field(default="", description="承接上一镜时的人物/道具状态")
    next_state: str = Field(default="", description="本镜结束时留给下一镜的状态")
    prop_state: str = Field(default="", description="关键道具状态，如『武心在右手刀柄』")
    light_state: str = Field(default="", description="光色/光源方向，如 dusk, warm key from left")
    eyeline: str = Field(default="", description="视线匹配对象（shot_id 或角色）")


class Shot(_ALayer):
    """③ 分镜 → ④ 出图。一个镜头 = 一张首帧图（后续由此图生视频）。"""

    shot_id: str = Field(description="镜头 id，如 SH01")
    scene_id: str = Field(description="所属场景 id")
    act_beat: str = Field(
        default="", description="结构段标签：hook/setup/escalate/climax/resolve（节奏校验用）"
    )

    prompt: str = Field(
        description="英文正向提示词，**本镜画面描述的权威字段**。只写画面，不写运镜。"
                    "权威仅限文本描述范畴：与 framing / subjects / motion / ref_characters "
                    "冲突时一律以**结构化字段**为准",
    )
    framing: dict[str, Framing] = Field(
        default_factory=dict,
        description="每个启用画幅一套独立构图：{\"16:9\":{...},\"9:16\":{...}}。"
                    "缺少任一「本镜实际启用画幅」的构图即 block",
    )
    subjects: list[ShotSubject] = Field(
        default_factory=list, description="逐角色姿态表情，必须覆盖 ref_characters 全部角色"
    )
    ref_characters: list[str] = Field(
        default_factory=list, description="本镜需要的人物 char_id，用于挂角色定妆图作参考"
    )
    requirements: Requirements = Field(
        default_factory=Requirements,
        description="本镜能力声明。**键级合并**：只写要覆盖的键，其余继承 script / plan",
    )
    aspect_overrides: dict[str, Any] | None = Field(
        default=None,
        description="按镜覆盖画幅：{\"9:16\":\"reframe_recompose\"} 或 {\"enabled\":[\"16:9\"]}。"
                    "null=不覆盖。若使某画幅一致性等级低于 plan 默认则告警",
    )

    motion: str = Field(
        default="",
        description="**画面内的被摄主体**在做什么（英文进行时），如 a young man sprinting。"
                    "只写本镜唯一持续动作，别写跨段的多事件",
    )
    camera: str = Field(
        default="static",
        description="摄影机**处于什么形态**：static / handheld / tripod / steadicam",
    )
    camera_move: str | None = Field(
        default=None,
        description="摄影机**是否运动、怎么动**：push_in / pull_out / pan_left / tilt_up / track。"
                    "null=无运动。与 camera、motion 互不替代",
    )
    audio: str = Field(default="", description="本镜音效/BGM/环境声描述")
    narration: list[NarrationLine] = Field(
        default_factory=list,
        description="叠加在本镜画面上的人声旁白/内心独白；不做口型，不算 speaking。",
    )
    transition_in: SceneTransition | None = Field(
        default=None, description="本镜是本场首镜时，从上一场接过来的转场设计。")
    sfx: str = Field(default="", description="音效（后期用，不进画面提示词）")
    effect: str = Field(default="", description="特效光/粒子/残影等（英文）")
    emotion: str = Field(default="", description="本镜情绪标签，如 怒/惊/屈辱/悬念")
    keyframe: str = Field(default="", description="关键帧画面要点（中文，供人工核对）")

    track: str | None = Field(
        default=None,
        description="Motion Track 轨迹 JSON 路径（运动/打斗镜用）。null 不启用。"
                    "轨迹按**整镜**帧号生成，多段镜头由流水线按帧区间自动切给各段",
    )
    stage_override: StageOverride | None = Field(
        default=None,
        description="本镜用哪套**制作阶段**：draft / standard / hero / skip。"
                    "禁止填写引擎名 / 节点名 / 工作流名",
    )

    start_sec: float | None = Field(default=None, ge=0, description="本镜在全片时间轴上的起点（秒）")
    duration_sec: float = Field(gt=0, le=30, description="本镜时长（秒）")
    duration_basis: str | None = Field(
        default=None,
        description="时长算账过程（中文，供人工复核与调参）：如"
                    "'动作3拍×1.5s=4.5s；台词0s；景别下限1.5s；取大4.5s→对齐5s'。"
                    "无内容下限（纯反应/单动作且未超载）时可省略")
    end_sec: float | None = Field(default=None, ge=0, description="本镜在全片时间轴上的终点（秒）")
    verify_point: str | None = Field(
        default=None, description="技术验证点标签，如 V1/V2/V3…；量产片为 null"
    )
    segments: list[ShotSegment] | None = Field(
        default=None,
        description="可选分段细化（长镜头用）。非空时与 prompt 语义一致，冲突以 prompt 为准",
    )
    continuity: Continuity = Field(
        default_factory=Continuity,
        description="镜间连续性：承接状态、屏幕方向、道具/光色。旧分镜留空时校验器只告警")
    hints: Hints = Field(default_factory=Hints, description="C 层引擎提示，业务侧留空")

    @model_validator(mode="after")
    def _subjects_cover_refs(self) -> Shot:
        if not self.ref_characters:
            return self
        covered = {s.char for s in self.subjects}
        missing = [c for c in self.ref_characters if c not in covered]
        if missing:
            raise ValueError(
                f"{self.shot_id} 的 subjects 没有覆盖这些出场角色：{missing}。"
                "本镜有人的话，每个出场角色都要给 pose/expression"
            )
        return self

    @property
    def cam(self) -> str:
        """运镜指令：`camera_move` 给定时优先，否则回退 `camera`。"""
        return (self.camera_move or "").strip() or (self.camera or "").strip()


class Storyboard(_ALayer):
    """③ 分镜 → ④ 出图。每集一份。"""

    schema_version: Literal["v10.1", "v10.2", "v10.3"] = Field(
        description="契约版本。v10.3 只新增可选字段，v10.1 / v10.2 继续可用"
    )
    episode: int = Field(ge=1)
    style_suffix: str = Field(
        default="",
        description="**必须为空**。画风只在 plan.visual_bible，分镜层任何画风描述均不生效",
    )
    shots: list[Shot] = Field(min_length=1)
    scene_anchors: dict[str, str] = Field(
        default_factory=dict,
        description="场景环境锚点：scene_id → 英文环境描述。同场景镜头逐字复用防漂移",
    )
    hints: Hints = Field(default_factory=Hints, description="C 层引擎提示，业务侧留空")

    @field_validator("style_suffix")
    @classmethod
    def _style_suffix_must_be_empty(cls, v: str) -> str:
        if v.strip():
            raise ValueError(
                f"style_suffix 必须为空（当前 {v!r}）。画风是 plan.visual_bible 的专属权威，"
                "分镜里写画风会造成源头不唯一"
            )
        return v

    @property
    def total_duration(self) -> float:
        return sum(s.duration_sec for s in self.shots)

    def shot(self, shot_id: str) -> Shot | None:
        return next((s for s in self.shots if s.shot_id == shot_id), None)


# ==========================================================================
# 5) 内部交接（非契约资产，保持宽松）
# ==========================================================================
class ShotRendered(_Base):
    shot_id: str
    image_path: str = Field(description="落盘到本项目 output 目录的绝对路径")
    workflow: str = Field(description="实际使用的工作流名")
    seed: int = Field(description="随机种子，便于复现")
    attempts: int = Field(default=1, ge=1, description="第几次尝试才成功（回退重出会累加）")
    comfy_prompt_id: str = Field(default="", description="ComfyUI 任务 id，便于对照日志")
    duration_sec: float = Field(default=0.0)


class ShotsRendered(_Base):
    """④ 出图 → ⑤ 音视频"""

    episode: int
    anchor_image: str = Field(default="", description="角色定妆图路径（一致性锚点）")
    # 允许为空：`--stage anchors` 只出定妆图 + 4 视角，产物就是"没有镜头"，
    # 这是合法中间态（下游 av 环节在该模式下直接被跳过）。
    shots: list[ShotRendered] = Field(default_factory=list)


class VideoInfo(_Base):
    """⑤ 音视频 → ⑥ 质检"""

    episode: int
    video_path: str = Field(description="成片绝对路径")
    timeline_json: str = Field(default="", description="时间线/分段中间件路径")
    duration_sec: float = Field(ge=0)
    resolution: str = Field(description="如 1280x720")
    fps: float = Field(default=24.0)
    has_audio: bool = Field(default=True)
    segments: list[str] = Field(default_factory=list, description="各镜头片段路径")
    seed: int = Field(default=0)


class QAIssue(_Base):
    shot_id: str = Field(default="", description="相关镜头 id；整体问题留空")
    level: Literal["hard", "soft"] = Field(description="hard=必须重做；soft=可接受但建议改进")
    problem: str = Field(description="问题描述（中文）")
    route_to: RouteTo = Field(description="回退给哪个员工重做")
    suggestion: str = Field(default="", description="具体修改建议，会拼进下一轮的任务里")


class QAReport(_Base):
    """⑥ 质检 → ⑦ 成片 / 回退"""

    result: Literal["pass", "fail"] = Field(description="只允许 pass 或 fail")
    issues: list[QAIssue] = Field(default_factory=list)
    overall: str = Field(default="", description="一句话总评（中文）")
    checked_frames: int = Field(default=0, description="实际看了多少帧")

    def blocking_routes(self) -> list[RouteTo]:
        """返回需要回退的目标（去重、按流水线顺序）。"""
        order = [RouteTo.PLANNER, RouteTo.WRITER, RouteTo.STORYBOARD, RouteTo.RENDERER, RouteTo.AV]
        hit = {i.route_to for i in self.issues if i.level == "hard"}
        return [r for r in order if r in hit]


class DeliveryManifest(_Base):
    """⑦ 成片 → 用户"""

    episode: int
    archive_dir: str = Field(description="归档目录绝对路径")
    video_path: str = Field(description="归档后的成片路径")
    manifest_path: str = Field(description="本清单自身的路径")
    artifacts: list[str] = Field(default_factory=list, description="归档的全部文件路径")
    qa_rounds: int = Field(default=0)
    total_sec: float = Field(default=0.0)


# ==========================================================================
# 注册表 + 工具函数
# ==========================================================================
HANDOFFS: dict[str, type[BaseModel]] = {
    "request": RunRequest,
    "plan": Plan,
    "script": Script,
    "storyboard": Storyboard,
    "shots_rendered": ShotsRendered,
    "video": VideoInfo,
    "qa_report": QAReport,
    "manifest": DeliveryManifest,
}

_PY_TYPES = {
    str: "字符串", int: "整数", float: "数字", bool: "布尔", dict: "对象", list: "数组",
}


def describe(model: type[BaseModel], indent: int = 0) -> str:
    """把模型渲染成给大模型看的紧凑结构说明（比 JSON Schema 省一半 token）。

    C 层容器（`hints`）没有声明字段 —— 渲染成一句说明，而不是空的 `{}`，
    否则模型会以为那里什么都没有、进而自己发明键。
    """
    pad = "  " * indent
    if not model.model_fields:
        return "{}  // 引擎专属参数。业务侧永远留空，不要往这里写任何东西"
    lines: list[str] = ["{"]
    for name, f in model.model_fields.items():
        ann = f.annotation
        desc = (f.description or "").split("\n")[0]
        optional = not f.is_required()

        tname, sub = _type_name(ann)
        val = tname
        if sub is not None and not sub.model_fields:
            val = describe(sub)             # C 层容器：内联说明，不写 <对象>
        elif sub is not None:
            val += "\n" + _describe_nested(sub, indent + 2)
        hint = f"  // {desc}" if desc else ""
        opt = "（可省略）" if optional else ""
        lines.append(f'{pad}  "{name}": {val}{opt}{hint}')
    lines.append(pad + "}")
    return "\n".join(lines)


def _describe_nested(model: type[BaseModel], indent: int) -> str:
    pad = "  " * indent
    return f"{pad}[\n{describe(model, indent)}\n{pad}]"


def _type_name(ann: Any) -> tuple[str, type[BaseModel] | None]:
    """把类型注解渲染成给模型看的中文说明。

    **必须用 `typing.get_origin` / `get_args`，不能用 `ann.__origin__`** ——
    `X | None`（PEP 604）没有 `__origin__` 属性，用属性访问会让所有可选字段
    静默退化成"任意"，模型就再也看不到枚举值域了（本文件 2026-10-13 修）。
    """
    origin = get_origin(ann)
    args = get_args(ann)

    if origin in (list, set, tuple) and args:
        inner_t, sub = _type_name(args[0])
        return f"[{inner_t}, ...]", sub
    if origin is dict and len(args) == 2:
        inner_t, sub = _type_name(args[1])
        return f'{{"<键>": {inner_t}, ...}}', sub
    if origin is Union or origin is types.UnionType:    # Optional[X] / X | None
        rest = [a for a in args if a is not type(None)]
        if len(rest) == 1:
            return _type_name(rest[0])
        return " | ".join(_type_name(a)[0] for a in rest), None
    if origin is Literal:
        return " | ".join(f'"{a}"' for a in args), None
    if isinstance(ann, type) and issubclass(ann, BaseModel):
        return "<对象>", ann
    if isinstance(ann, type) and issubclass(ann, Enum):
        return " | ".join(f'"{e.value}"' for e in ann), None
    if ann is str:
        return "字符串", None
    if ann in (int, float):
        return "数字", None
    if ann is bool:
        return "布尔", None
    if ann is dict:
        return "对象", None
    if ann is list:
        return "数组", None
    return _PY_TYPES.get(ann, "任意"), None


def parse_json(text: str) -> dict[str, Any]:
    """从模型回复里抠出 JSON 对象 —— 容忍 ```json 围栏与前后闲话。"""
    if not text or not text.strip():
        raise ValueError("模型返回为空")
    s = text.strip()
    if s.startswith("```"):
        s = s.split("```")[1] if "```" in s[3:] else s[3:]
        if s.lstrip().lower().startswith("json"):
            s = s.lstrip()[4:]
    s = s.strip()
    start = s.find("{")
    if start < 0:
        raise ValueError(f"返回里找不到 JSON 对象：{text[:200]}")
    decoder = json.JSONDecoder()
    data = None
    idx = start
    while idx >= 0:
        try:
            cand, end = decoder.raw_decode(s[idx:])
            tail = s[idx + end:].strip().strip("`").strip()
            if tail == "":
                # 该 JSON 一直延续到末尾，即模型最终输出
                data = cand
                break
            # 思考里可能先给了一个完整示例 JSON，真正的输出在后面，先记着继续找
            data = cand if data is None else data
        except json.JSONDecodeError:
            pass  # 思考/示例里的半截 {，跳到下一个候选
        idx = s.find("{", idx + 1)
    if data is None:
        raise ValueError(
            "JSON 解析失败：输出可能被截断（内容超过输出上限）或混入了思考文字，"
            "请增大该阶段 max_tokens 并只输出 JSON")
    if not isinstance(data, dict):
        raise ValueError(f"顶层必须是 JSON 对象，实际是 {type(data).__name__}")
    return data


def validate(stage: str, data: Any) -> BaseModel:
    """按阶段名校验。失败时把 pydantic 的报错原样抛出，供 llm 层回灌重试。"""
    model = HANDOFFS.get(stage)
    if model is None:
        raise KeyError(f"未知阶段 {stage!r}，可选：{sorted(HANDOFFS)}")
    if isinstance(data, model):
        return data
    return model.model_validate(data)


def schema_error_text(exc: Exception) -> str:
    """把校验异常整理成给模型看的一小段文本。"""
    if isinstance(exc, ValidationError):
        parts = []
        for e in exc.errors()[:12]:
            loc = ".".join(str(x) for x in e.get("loc", ()))
            parts.append(f"- 字段 {loc or '<根>'}：{e.get('msg')}")
        return "\n".join(parts)
    return str(exc)
