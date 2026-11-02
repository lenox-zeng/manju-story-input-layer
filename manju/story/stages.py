"""小说 → 故事层（大纲/角色/美术/剧本/站位/分镜）的 LLM 流水线。

- 每个阶段都有确定性质量门；不过门就把错误原文回喂重写。
- 提示词构建在 prompts.py、确定性修复在 repairs.py，本文件只保留管线本身。
- 中间产物写到 `state/story/`，可人工查看，也可 `resume=True` 续跑。
"""
from __future__ import annotations

import json
import re
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from pydantic import BaseModel

from ..config import get_config
from ..llm import LLMClient
from .engines import engine_profile
from .episodes import EpisodeSlice
from .ingest import parse_text
from .models import (
    ActionBeatLine, Art, Cast, CutState, EpisodeBatch, EpisodePlan, Outline,
    ScriptEpisode, ScriptParams, ScriptScene, SeriesDesign, Staging,
    StoryBible, StoryCut, StoryEpisodeBoard, StorySegment, StoryStoryboard,
    SegmentPlan, SegmentPrompts, _beat_seconds,
)
from .plot import (
    build_plot_atoms, design_payload, design_slices,
    prefix_cut, repair_design_in_place, validate_series_design,
)
from .summarize import build_codex
from .validate import (
    GateReport, _event_key, _line_chars, coverage_required, event_score,
    validate_art, validate_bible, validate_board, validate_cast,
    validate_episode_batch, validate_outline, validate_script,
    validate_segment_cuts, validate_staging,
)
# ------------------------------------------------------------------
# 提示词层 / 确定性修复层（拆分自本文件的历史实现）：
#   prompts.py —— 阶段规则、系统提示词、输入 payload 与定向摘录；
#   repairs.py —— 质量门不过时的本地修复。
# 本文件保留全部名字的 re-export，外部（含测试）从 stages 导入不变。
# ------------------------------------------------------------------
from .prompts import (
    ACTIONLINE_RULES, BIBLE_RULES, BOARD_EXCERPT, EPISODE_BATCH_CHARS, EPISODE_EXCERPT, EPISODE_RULES, SCRIPT_EXCERPT, SERIES_DESIGN_RULES, STAGING_EXCERPT, STAGING_RULES, _SENT_SPLIT, _art_rules, _bible_payload, _board_rules, _cast_payload, _cast_rules, _cut_brief, _edl_rules, _excerpt, _focus_excerpt, _identity_repetition_warnings, _neighbors, _placeholder_prompt, _prev_end, _prompt_brief, _prompt_lang_label, _prompt_rules, _script_rules, _slice_text, _system, _world_bible,
)  # noqa: F401
from .repairs import (
    _BEAT_FALLBACK, _CLIFF_QUOTES, _LENS_BY_SIZE, _TRANSITION_TO_REASON, _action_beats_by_index, _apply_design_to_plans, _assemble_segment, _beat_summary, _cliff_action_text, _coverage_of_beat, _demote_unused_primary_scenes, _derived_frame, _merge_stagings, _narration_by_beat, _next_prop_id, _normalize_edl_plan, _normalize_plan_units, _plan_characters, _prop_scale, _prune_unused_props, _repair_narration, _repair_script_coverage, _repair_script_output, _repair_script_plot, _repair_segment_output, _repair_staging_output, _repair_unused_characters, _resolve_prop_id, _scene_match_text, _split_long_line, _split_long_segments, _state_after, screen_axis_flip,
)  # noqa: F401


Progress = Callable[[str], None]

# 提示词/契约口径版本：改动**生成口径**（提示词、契约字段、切分方式）时递增，
# 让 resume 不复用旧产物。
#
# 2026-10-30-plot-keys-1：分集新增 key_lines（关键台词原样保留）、synopsis 允许
# 引用台词、集长放宽、剧本层放开台词容量、分镜"必须有运镜"、引擎档案参数化。
# 这些改动改变系列设计/剧本/分镜的生成口径，旧缓存整体判失效。
STORY_RULES_REV = "2026-10-30-plot-keys-1"

# 提示词策略版本：输入层不再关心实现层用哪种人物一致性技术。分镜 prompt_en
# 固定只写动作/场景/表演，身份数据（ref_characters / story_assets 的
# identity_prompt、identity_anchor）由实现层自行注入。
#
# 2026-10-30-camera-1：prompt_en/motion 规则改为"必须有运镜"（不再禁运镜词），
# 并按引擎档案（camera_motion=full/limited）收敛运镜幅度；prompt_en 允许光线/
# 情绪/细节词。只让 shots_* 这类含逐镜提示词的缓存失效。
#
# 这不是全局指纹：只让 shots_* 这类含逐镜提示词的缓存失效，避免为了改提示词
# 策略重跑圣经/大纲/剧本，也避免重建不含提示词的 EDL 段缓存。
PROMPT_POLICY_REV = "2026-10-30-camera-1"

# 引擎单镜上限来自 ScriptParams.max_shot_seconds（见 params_from_cfg）；
# 台词拍若超过该时长，分镜阶段无论怎么切都装不下，必须在剧本层先拆拍。


def _key_line_spans(lines: list[str], body: str, *, max_spans: int = 5) -> bool:
    """key_line 是否原样落在台词序列里：单拍包含，或任意 ≤max_spans 个
    连续拍的拼接包含（拆拍路径；上限防整场误拼）。"""
    n = len(lines)
    for i in range(n):
        acc = ""
        for j in range(i, min(n, i + max_spans)):
            acc += lines[j]
            if body in acc:
                return True
    return False


class StoryGateError(RuntimeError):
    """质量门失败。payload 携带最后一次尝试的产出（供失败包快照与人工修复）。"""

    def __init__(self, stage: str, report: GateReport, *, payload: Any = None):
        self.stage = stage
        self.report = report
        self.payload = payload
        super().__init__(f"{stage} 连续重写仍未过质量门：\n{report.text()}")


# --------------------------------------------------------------------------
# Prompt
# --------------------------------------------------------------------------


# --------------------------------------------------------------------------
# 取源：只给本集切片 + 相邻集梗概，绝不把全文前 4000 字塞给每一集
# --------------------------------------------------------------------------
# 按情节分集后，单集切片可达 1-2 万字；摘录必须尽量整段给，不能再腰斩中段。
# 中段恰恰是因果链最容易断的地方（谁在什么时候因为什么做了什么）。


def _script_budget(target: float, sp: ScriptParams | None = None, *,
                   dialogue_light: bool = False) -> dict[str, Any]:
    """按目标时长推导可执行的节拍/台词预算（供提示词与校验共用）。

    动作拍按配置（默认 2.5s）；台词按单句上限与语速估算。
    这样算出的动作拍上限不会偏大，模型照此写就不会轻易冲破硬上限。

    dialogue_light=True 表示原文对白稀薄，给出较低的台词参考值；台词句数
    不设硬上限，关键对话密集（含纯对话集）由 key_lines 机制保证一字不改落拍。
    场次 = 完整戏剧单元数（1-4 场），由内容决定，不按秒数切。
    """
    sp = sp or ScriptParams()
    max_seconds = target * (1 + sp.tolerance)
    # 台词密度目标随集长缩放：默认约 3.0 字/秒（≈60% 时间在说话），
    # 90s ≈ 270 字。关键对话密集时允许更多，纯对话集可行（key_lines 必须
    # 一字不改落拍）；dialogue_light 只在原文对白稀薄时给低参考值。
    if dialogue_light:
        # 原文对白稀薄：只保留真正冲突/抉择的句子（参考值，不设硬上限）。
        target_chars = max(50, min(160, int(round(target * 1.2 / 10)) * 10))
        suggest_lines = max(2, min(6, round(target_chars / 18)))
    else:
        target_chars = max(
            100, min(int(round(target * 3.0 / 10)) * 10, 1200))
        suggest_lines = max(4, min(40, round(target_chars / 16)))
    line_seconds = sp.max_line_chars / sp.chars_per_second
    # 上限用“平均句长”而不是“最长句长”估算，否则下限（一镜一拍）会超过上限。
    avg_line_seconds = (target_chars / suggest_lines) / sp.chars_per_second
    max_action_beats = max(
        1, int((max_seconds - suggest_lines * avg_line_seconds) / sp.action_seconds))
    # 场次 = 完整戏剧单元数，由内容决定（1-4 场），不再按秒数切 5-8 场。
    # 一个单元演完再切；纯对话集允许整集一场；每场 3-12 拍。
    suggest_scenes, beats_per_scene = "1-4", "4-12"
    # 分镜契约是“一镜一拍”，所以剧本拍数就是镜头数。drama 门要求 90s
    # 至少 35 镜（平均 ≤2.6s/镜）——2026-10-30 起按内容算时长后放宽到
    # 平均 3.5s/镜，避免把每镜压成 2.5s 碎镜；且 reaction/insert/establish/pov
    # 有覆盖率配额；这些必须在剧本层就规划好，否则分镜层再怎么写都补不出来。
    cov = coverage_required(target, dialogue_light=dialogue_light)
    min_total_beats = max(1, round(target / 3.5))
    narration_limits = {
        "exposition": sp.narration_max_exposition,
        "inner": sp.narration_max_inner,
        "max_total_seconds": round(
            target * (0.30 if dialogue_light else 0.18), 1),
    }
    return {
        "target_seconds": target,
        "hard_max_seconds": round(max_seconds, 1),
        "action_seconds": sp.action_seconds,
        "chars_per_second": sp.chars_per_second,
        "line_seconds_max": round(line_seconds, 2),
        "target_chars": target_chars,
        "suggest_scenes": suggest_scenes,
        "suggest_beats_per_scene": beats_per_scene,
        "suggest_lines": suggest_lines,
        "max_action_beats": max_action_beats,
        # 下限与分镜/下游门同源；上限绝不能低于下限，否则提示词自相矛盾。
        "max_total_beats": max(min_total_beats, max_action_beats + suggest_lines),
        "min_total_beats": min_total_beats,
        "coverage_required": cov,
        "dialogue_light": dialogue_light,
        "narration_limits": narration_limits,
    }


_QUOTE_PAIR_RE = re.compile(r"[“\"「『]([^”\"」』]+)[”\"」』]")


def _dialogue_metrics(text: str) -> dict[str, Any]:
    """估算原文对白密度，决定是否启用“少对白强化叙事”模式。

    只看引号内文本：对白句数、对白字数占比、每千字对白句数。
    原文没有引号对白时直接判为 dialogue_light。
    """
    source = (text or "").strip()
    compact_len = max(1, len(re.sub(r"\s+", "", source)))
    quoted = [q.strip() for q in _QUOTE_PAIR_RE.findall(source) if q.strip()]
    if not quoted:
        # 某些文本只用直角/双引号；再兜底一次，避免把整本无引号小说误判成“对白充足”。
        quoted = [q.strip() for q in (
            re.findall(r"「([^」]+)」", source)
            + re.findall(r"『([^』]+)』", source)) if q.strip()]
    quote_chars = sum(_line_chars(q) for q in quoted)
    ratio = quote_chars / compact_len
    lines_per_1000 = len(quoted) / (compact_len / 1000.0)
    return {
        "quoted_lines": len(quoted),
        "quote_chars": quote_chars,
        "quote_char_ratio": round(ratio, 4),
        "lines_per_1000": round(lines_per_1000, 2),
        "dialogue_light": bool(not quoted or ratio < 0.08 or lines_per_1000 < 0.6),
    }


# 模型偶尔把情绪词/场次类型写进 drama beat 字段；映射到最近的合法节拍。


def _fallback_slices(text: str, count: int) -> list[EpisodeSlice]:
    """没有外部切片时的兜底：把（已压缩的）文本均分。"""
    if count < 1:
        raise ValueError("集数必须 ≥ 1")
    if count == 1:
        return [EpisodeSlice(1, 0, len(text), (), text)]
    cuts = [0]
    for k in range(1, count):
        target = len(text) * k // count
        nl = text.find("\n", target)
        cuts.append(nl + 1 if 0 <= nl < target + 400 else target)
    cuts.append(len(text))
    return [EpisodeSlice(i + 1, cuts[i], cuts[i + 1], (),
                         text[cuts[i]:cuts[i + 1]]) for i in range(count)]


def _range_source(text: str, slices: list[EpisodeSlice] | None,
                  episode_range: tuple[int, int] | None) -> str:
    """范围生成从第 1 集开始时，把长文压缩限制到本范围覆盖的原文。

    只生成前 N 集却对全书做分卷摘要，是长篇最大的无谓 token 开销；
    从第 M>1 集开始时仍需全书背景，所以只在起点为 1 时截取。
    """
    if not (episode_range and episode_range[0] == 1 and slices
            and 1 <= episode_range[1] <= len(slices)):
        return text
    end = slices[episode_range[1] - 1].end
    return text[:end] if 0 < end < len(text) else text


def _cfg_first(cfg, keys: tuple[str, ...], default=None):
    for key in keys:
        value = cfg.get(key)
        if value is not None:
            return value
    return default


def _cfg_nonneg_int(value, default: int, *, upper: int) -> int:
    try:
        return max(0, min(int(value), upper))
    except (TypeError, ValueError):
        return default


def _cfg_mode(value, default: str, allowed: set[str]) -> str:
    text = str(value if value is not None else default).strip().lower()
    return text if text in allowed else default


def _cfg_optional_int(value) -> int | None:
    """None/0/空/auto → None（不限制）；否则取 >=1 的整数。"""
    if value is None:
        return None
    if isinstance(value, str):
        text = value.strip().lower()
        if text in {"", "none", "null", "auto", "0"}:
            return None
    try:
        return max(1, int(value))
    except (TypeError, ValueError):
        return None


def _cfg_positive_float(value, default: float, *, upper: float) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    if number <= 0:
        return default
    return min(number, upper)


def _cfg_tier_list(value) -> list[float] | None:
    """时长档位表：None/空 → None（不设档位）；列表 → 正数升序去重。"""
    if value is None:
        return None
    if isinstance(value, str):
        text = value.strip().lower()
        if text in {"", "none", "null"}:
            return None
    items: list[float] = []
    for item in value if isinstance(value, (list, tuple)) else [value]:
        try:
            number = float(item)
        except (TypeError, ValueError):
            continue
        if number > 0:
            items.append(number)
    if not items:
        return None
    return sorted(set(items))


def _cfg_floor_map(value) -> dict[str, float]:
    """景别时长下限表：None → 默认表；否则只覆盖给出的键。"""
    field = ScriptParams.model_fields["shot_size_floor"]
    if field.default_factory is not None:
        default = dict(field.default_factory())
    else:
        default = dict(field.default)
    if not value or not isinstance(value, dict):
        return default
    for k, v in value.items():
        try:
            default[str(k)] = float(v)
        except (TypeError, ValueError):
            continue
    return default


def params_from_cfg(cfg) -> ScriptParams:
    """从 config 构建故事层参数。

    这里只放**引擎/项目能力**（单镜时长、同框脸数、一句/多句、提示词语言、
    分段上限、运镜幅度），不是普适剧作规则。取值优先级：
    1. config 里 story.*（或兼容的 pipeline.*）显式配置；
    2. 未显式配置时，取 story.engine 对应档案（engines.py）的推荐值；
    3. 档案也没有时，取 ScriptParams 通用默认。

    * 缺省 engine（""）时用通用默认（人脸/台词句数不限、full 运镜）；
    * 本地单参考图 PuLID + 单人口型可显式配 1，或直接配 story.engine=comfyui。
    """
    # story.* 是唯一权威来源；pipeline.* 只保留给旧配置兼容。
    prof = engine_profile(_cfg_first(cfg, ("story.engine",), ""))
    def _prof(name: str, default):
        """档案优先、其次通用默认；供 _cfg_first 的 default 参数使用。"""
        return prof.get(name, default)

    max_shot = _cfg_positive_float(
        _cfg_first(cfg, ("story.max_shot_seconds", "pipeline.max_shot_seconds"),
                   _prof("max_shot_seconds",
                         ScriptParams.model_fields["max_shot_seconds"].default)),
        6.0, upper=30.0)
    max_segment = _cfg_positive_float(
        _cfg_first(cfg, ("story.max_segment_seconds",
                         "pipeline.max_segment_seconds"),
                   _prof("max_segment_seconds",
                         ScriptParams.model_fields["max_segment_seconds"].default)),
        15.0, upper=600.0)
    min_shot = _cfg_positive_float(
        _cfg_first(cfg, ("story.min_shot_seconds", "pipeline.min_shot_seconds"),
                   _prof("min_shot_seconds",
                         ScriptParams.model_fields["min_shot_seconds"].default)),
        1.0, upper=max_shot)
    language = str(_cfg_first(cfg, ("story.prompt_language",), "en")
                   or "en").strip().lower()
    if language not in {"en", "zh", "any"}:
        language = "en"
    narration_mode = _cfg_mode(
        _cfg_first(cfg, ("story.narration_mode",), "auto"), "auto",
        {"auto", "off", "on"})
    transition_mode = _cfg_mode(
        _cfg_first(cfg, ("story.transition_mode",), "auto"), "auto",
        {"auto", "off", "on"})
    camera_motion = str(_cfg_first(
        cfg, ("story.camera_motion",),
        _prof("camera_motion", ScriptParams.model_fields["camera_motion"].default))
        or "full").strip().lower()
    if camera_motion not in {"full", "limited"}:
        camera_motion = "full"
    return ScriptParams(
        chars_per_second=_cfg_positive_float(
            _cfg_first(cfg, ("story.chars_per_second",), 4.5), 4.5, upper=60.0),
        action_seconds=_cfg_positive_float(
            _cfg_first(cfg, ("story.action_seconds",), 2.5), 2.5, upper=60.0),
        tolerance=_cfg_positive_float(
            _cfg_first(cfg, ("story.tolerance",), 0.15), 0.15, upper=0.95),
        max_line_chars=_cfg_optional_int(
            _cfg_first(cfg, ("story.max_line_chars",),
                       _prof("max_line_chars", 18))) or 18,
        # 单源：story.*；旧版 pipeline.pulid/lipsync 仍兼容。
        max_faces_per_shot=_cfg_optional_int(
            _cfg_first(cfg, ("story.max_faces_per_shot",
                             "pipeline.pulid.max_faces_per_shot"),
                       _prof("max_faces_per_shot", None))),
        max_lines_per_shot=_cfg_optional_int(
            _cfg_first(cfg, ("story.max_lines_per_shot",
                             "pipeline.lipsync.max_lines_per_shot",
                             "lipsync.max_lines_per_shot"),
                       _prof("max_lines_per_shot", None))),
        min_shot_seconds=min_shot,
        max_shot_seconds=max_shot,
        camera_motion=camera_motion,
        max_segment_seconds=max_segment,
        prompt_language=language,
        narration_mode=narration_mode,
        narration_max_exposition=_cfg_nonneg_int(
            _cfg_first(cfg, ("story.narration_max_exposition",), 2), 2,
            upper=8),
        narration_max_inner=_cfg_nonneg_int(
            _cfg_first(cfg, ("story.narration_max_inner",), 3), 3, upper=12),
        transition_mode=transition_mode,
        # ---- 2026-10-30 时长与阶段化 ----
        max_actions_per_shot=_cfg_optional_int(
            _cfg_first(cfg, ("story.max_actions_per_shot",),
                       _prof("max_actions_per_shot", None))),
        action_seconds_per_beat=_cfg_positive_float(
            _cfg_first(cfg, ("story.action_seconds_per_beat",),
                       _prof("action_seconds_per_beat", 1.5)), 1.5, upper=10.0),
        pause_seconds_per_line=_cfg_positive_float(
            _cfg_first(cfg, ("story.pause_seconds_per_line",),
                       _prof("pause_seconds_per_line", 0.7)), 0.7, upper=5.0),
        duration_tiers=_cfg_tier_list(
            _cfg_first(cfg, ("story.duration_tiers",),
                       _prof("duration_tiers", None))),
        round_up_step=_cfg_positive_float(
            _cfg_first(cfg, ("story.round_up_step",), 0.5), 0.5, upper=5.0),
        shot_size_floor=_cfg_floor_map(
            _cfg_first(cfg, ("story.shot_size_floor",), None)),
    )


def max_faces_from_cfg(cfg) -> int | None:
    """兼容旧调用：只读同框人脸上限。"""
    return params_from_cfg(cfg).max_faces_per_shot


# --------------------------------------------------------------------------
# 通用：生成 + 质量门回喂
# --------------------------------------------------------------------------
@dataclass
class _PreparedScene:
    """B：已确定镜位/状态的场次，等待并行的视觉提示词展开。"""
    ep: ScriptEpisode
    index: int
    scene: ScriptScene
    line: ActionBeatLine
    plan: SegmentPlan
    skeleton: StorySegment
    state_in: CutState
    prev_cuts: list[StoryCut]
    art: Art
    cast: Cast
    staging: Staging
    slices: list[EpisodeSlice] | None
    source: str


class StoryPipeline:
    def __init__(self, llm: LLMClient | None = None,
                 workdir: str | Path | None = None,
                 progress: Progress | None = None,
                 resume: bool = False,
                 strict: bool = False):
        self.llm = llm or LLMClient()
        self.cfg = get_config()
        self.workdir = Path(workdir or (Path(self.cfg.p("state")) / "story"))
        self.workdir.mkdir(parents=True, exist_ok=True)
        self.resume = resume
        # 软门默认只记 warning；--strict 恢复为错误
        self.strict = strict
        self.attempts = int(self.cfg.get("llm.story_gate_attempts", 3))
        # 引擎能力参数：全部可配置；人脸/台词句数缺省不限制。
        self.params = params_from_cfg(self.cfg)
        self.max_faces_per_shot = self.params.max_faces_per_shot
        # B2：⑥⑦ 跨集并行的并发数（远程 API 天然可并发）；progress 包锁，多线程不交错
        self.parallel = max(1, int(self.cfg.get("llm.parallel", 3)))
        # B：分镜提示词展开是纯读操作，可跨场并行；默认不小于 staging 并发
        self.prompt_parallel = max(
            1, int(self.cfg.get("llm.prompt_parallel", self.parallel) or self.parallel))
        self._lock = threading.Lock()
        self._fp_lock = threading.RLock()
        _base = progress or (lambda _m: None)

        def _safe_progress(m: str) -> None:
            with self._lock:
                _base(m)

        self.progress = _safe_progress
        # P0-③ resume 参数指纹：记录本次 run 的关键参数，防止改 --duration/--ratio
        # 后静默复用旧产物。指纹一致才允许复用缓存；不一致则对应产物重新生成。
        self._resume_fp: str = ""
        self._resume_meta: dict[str, str] | None = None

    def set_fingerprint(self, episodes: int, seconds: float, ratio: str,
                        *, source: str = "", model: str = "",
                        split: str = "") -> None:
        """用关键参数计算本次运行指纹，写入后 _load 复用前会校验。

        除集数/秒数/画幅外，还要包含**源文本哈希与模型名**：换了一部同名小说
        或换了模型但参数没变时，不能静默复用旧大纲/旧分集。
        章节模式还要把“每几章一集”纳入指纹，否则同集数但分组不同会复用错大纲。
        """
        import hashlib
        src_hash = hashlib.sha1((source or "").encode("utf-8")).hexdigest()[:12]
        if not model:
            cfg_obj = getattr(self.llm, "cfg", None)
            model = str(cfg_obj.get("llm.model", "")) if cfg_obj is not None else ""
        raw = (f"ep={episodes}|sec={seconds:.4f}|ratio={ratio}"
               f"|model={model}|src={src_hash}")
        if split:
            raw += f"|split={split}"
        self._resume_fp = hashlib.sha1(raw.encode("utf-8")).hexdigest()[:12]

    def _fp_meta(self) -> dict[str, str]:
        with self._fp_lock:
            if self._resume_meta is None:
                mp = self.workdir / "_resume_fp.json"
                self._resume_meta = (json.loads(mp.read_text(encoding="utf-8-sig"))
                                     if mp.is_file() else {})
            return self._resume_meta

    def _fp_ok(self, name: str, *, missing_ok: bool = False) -> bool:
        if not self._resume_fp:
            return True  # 未设指纹（非 run 上下文），退回原行为
        with self._fp_lock:
            meta = dict(self._fp_meta())
        if name not in meta:
            return missing_ok  # 旧版本产物没有该键：可按迁移策略放行
        return meta.get(name) == self._resume_fp

    def _touch_fp(self, name: str) -> None:
        with self._fp_lock:
            meta = self._fp_meta()
            meta[name] = self._resume_fp
            (self.workdir / "_resume_fp.json").write_text(
                json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")

    @staticmethod
    def _prompt_policy_sensitive(name: str) -> bool:
        """只有逐镜首帧提示词缓存绑定提示词策略；EDL 段缓存不含提示词，不重建。"""
        return name.startswith("shots_ep")

    def _prompt_policy_key(self, name: str) -> str:
        return f"{name}::prompt_policy"

    def _prompt_policy_ok(self, name: str) -> bool:
        """提示词缓存绑定 PROMPT_POLICY_REV，策略变化时只重建提示词缓存。

        不能把版本塞进全局 `_resume_fp`：那会让改提示词口径也重跑
        圣经/大纲/剧本，代价太大。旧工作层没有这个键时按失效处理，
        确保升级后的精简提示词真正生效。
        """
        if not self._resume_fp:
            return True          # 非 run 上下文：与 _fp_ok 的旧行为保持一致
        with self._fp_lock:
            meta = dict(self._fp_meta())
        return meta.get(self._prompt_policy_key(name)) == PROMPT_POLICY_REV

    def _touch_prompt_policy(self, name: str) -> None:
        with self._fp_lock:
            meta = self._fp_meta()
            meta[self._prompt_policy_key(name)] = PROMPT_POLICY_REV
            (self.workdir / "_resume_fp.json").write_text(
                json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")

    # -- 中间产物 ---------------------------------------------------------
    def _path(self, name: str) -> Path:
        return self.workdir / f"{name}.json"

    def _save(self, name: str, obj: BaseModel) -> None:
        p = self._path(name)
        p.write_text(json.dumps(obj.model_dump(mode="json"),
                                ensure_ascii=False, indent=2), encoding="utf-8")
        if self._resume_fp:
            self._touch_fp(name)
            if self._prompt_policy_sensitive(name):
                self._touch_prompt_policy(name)

    def _load(self, name: str, cls: type[BaseModel]):
        p = self._path(name)
        if self.resume and p.is_file() and self._fp_ok(name):
            try:
                obj = cls.model_validate(json.loads(p.read_text(encoding="utf-8-sig")))
                self.progress(f"  复用 {p.name}")
                return obj
            except Exception:  # noqa: BLE001 - 中间文件坏了就重生成
                pass
        return None

    def _generate(self, stage: str, model_cls: type[BaseModel], system: str,
                  payload: dict[str, Any], check: Callable[[Any], GateReport],
                  *, name: str, max_tokens: int, temperature: float | None = None):
        cached = self._load(name, model_cls)
        if (cached is not None and self._prompt_policy_sensitive(name)
                and not self._prompt_policy_ok(name)):
            self.progress(
                f"  [WARN] 提示词策略已变化（当前 {PROMPT_POLICY_REV}），"
                f"重建 {name}.json")
            cached = None
        if cached is not None:
            try:
                rep = check(cached)
            except Exception as e:  # noqa: BLE001 - 旧缓存可能触发修复代码的边界
                self.progress(f"  缓存校验崩溃（{type(e).__name__}: {e}），"
                              "重建该阶段")
                rep = None
            if rep is not None and rep.ok:
                return cached
            if rep is not None:
                self.progress(f"  缓存未过门，重生成：{rep.errors[0]}")
        extra = ""
        report: GateReport | None = None
        for attempt in range(1, self.attempts + 1):
            self.progress(f"  {stage} 第 {attempt}/{self.attempts} 次生成…")
            obj = self.llm.structured(
                stage, system, payload, model_cls=model_cls,
                max_tokens=max_tokens, temperature=temperature,
                extra_user=extra,
            )
            try:
                report = check(obj)
            except Exception as e:  # noqa: BLE001
                # 质量门里的确定性装配/修复如果崩了（例如模型把中文人名写进
                # CutSubject.char），不能把整轮生成直接炸掉：把它当成一条
                # 结构化错误回喂，模型下一次仍有机会改正。
                msg = f"{type(e).__name__}: {e}"
                report = GateReport(stage)
                report.add(f"确定性校验/修复崩溃：{msg}")
                self.progress(f"  校验崩溃，回喂重写：{msg}")
            if report.ok:
                self._save(name, obj)
                self.progress(f"  {stage} 通过质量门"
                              + (f"（{len(report.warnings)} warning）"
                                 if report.warnings else ""))
                return obj
            self.progress(f"  未过门：{len(report.errors)} 个错误，回喂重写")
            # 上次输出整段回喂，模型才能按错误清单逐条定位；
            # 12k 字符会腰斩整集剧本（约 25k 字符），截在 JSON 中间反而更容易改错。
            prev = obj.model_dump_json()
            prev_cap = 40000
            if len(prev) > prev_cap:
                prev = prev[:prev_cap] + "\n…（上次输出过长，此处截断；请以错误清单为准）"
            extra = (
                "\n\n⚠️ 你上一次输出的结构合法，但没有通过质量门。必须逐条修正：\n"
                f"{report.text()}\n"
                f"上一次的输出（供你定位）：\n"
                f"{prev}"
            )
        assert report is not None
        raise StoryGateError(stage, report, payload=payload)

    # -- 0. 情节驱动分集 --------------------------------------------------
    def design_series(self, text: str, seconds: float,
                      *, atom_chars: int | None = None,
                      excerpt_chars: int | None = None,
                      max_atoms: int | None = None,
                      total_excerpt_chars: int | None = None,
                      max_episodes: int | None = None,
                      ) -> tuple[SeriesDesign, list[EpisodeSlice]]:
        """先看整本小说，按戏剧单元决定集数与每集必拍事件。

        与 split_episodes 的区别：不按固定字数/章节分组，而是让 LLM 依据
        因果链/转折点划分；返回的 EpisodeSlice 仍精确映射回原文，正文不丢。

        ``max_episodes`` 用于“只生成前 N 集”：只把足够覆盖 N 集的前缀文本
        交给设计模型，并在设计完成后截取前 N 个戏剧单元。这样长篇不会为了
        还没生成的后文付 token，也不会让前 2 集被 90 万字稀释。
        """
        novel = parse_text(text)
        full_chars = len(novel.text)
        prefix_chars = 0
        if max_episodes and max_episodes > 0:
            per = max(2000, int(self.cfg.get(
                "llm.story_plot_prefix_chars_per_episode", 5000)))
            prefix_chars = min(full_chars, int(max_episodes) * per)
            if prefix_chars < full_chars:
                prefix_chars = prefix_cut(novel.text, prefix_chars)
                novel = parse_text(novel.text[:prefix_chars])
                self.progress(
                    f"  只生成前 {max_episodes} 集：设计输入取前 "
                    f"{prefix_chars} 字（全文 {full_chars} 字），"
                    "不替未生成的后文做分集")
        atoms = build_plot_atoms(
            novel,
            atom_chars=int(atom_chars or self.cfg.get(
                "llm.story_plot_atom_chars", 2400)),
            excerpt_chars=int(excerpt_chars or self.cfg.get(
                "llm.story_plot_atom_excerpt", 900)),
            max_atoms=int(max_atoms or self.cfg.get(
                "llm.story_plot_max_atoms", 180)),
            total_excerpt_chars=int(total_excerpt_chars or self.cfg.get(
                "llm.story_plot_total_chars", 100_000)),
        )
        if not atoms:
            raise ValueError("小说正文为空，无法做情节分集")
        per = design_payload(
            atoms, seconds=seconds,
            excerpt_chars=int(self.cfg.get("llm.story_plot_atom_excerpt", 900)),
            total_excerpt_chars=int(
                self.cfg.get("llm.story_plot_total_chars", 100_000)),
        )
        payload = {
            "params": {"target_seconds": seconds,
                       "aspect_ratio": getattr(self.params, "aspect_ratio", "9:16")},
            "atom_count": len(atoms),
            "atom_chars": atoms[0].chars,
            "atoms": per,
        }
        rules = SERIES_DESIGN_RULES
        if prefix_chars and prefix_chars < full_chars:
            payload["scope"] = {
                "partial_source": True,
                "source_chars": prefix_chars,
                "full_chars": full_chars,
                "max_episodes": int(max_episodes or 0),
            }
            rules += (f"\n11. 本次只需要前 {int(max_episodes or 0)} 集：你看到的是"
                      "全文前段（不是全书）。仍按戏剧单元分集，每个单元的 cliff "
                      "都要能接住下一单元；不要为了“收尾”提前解决主线，"
                      "也不要为了凑集数硬拆。")
        sys = _system(
            "你是短剧总编剧兼剧本策划，最擅长从长篇小说里辨认真正的戏剧单元。",
            "通读全部情节原子，按“目标→阻力→转折→代价→新问题”的戏剧单元分集，"
            "不要按字数或章节数分集。为每集写：atom_ids 连续区间、synopsis、"
            "2-6 条 must_show（必须讲完整的可见事件）、1-4 句 key_lines（必须原样"
            "保留的关键台词，允许纯对话集）、0-3 件 key_props、dramatic_question、"
            "turn、climax、hook、cliff、estimated_seconds。"
            "一个完整事件单元哪怕只有 400-500 字也可以独立成集；"
            "所有原子必须按顺序恰好用一次。",
            SeriesDesign, rules)

        def check(d: SeriesDesign) -> GateReport:
            fixes = repair_design_in_place(d, len(atoms))
            for msg in fixes[:8]:
                self.progress(f"  [FIX] {msg}")
            if len(fixes) > 8:
                self.progress(f"  [FIX] …另有 {len(fixes) - 8} 处分集边界修复")
            rep = validate_series_design(d, len(atoms), seconds=seconds)
            # 软性“原文依据”检查：改编可以合并/压缩，但不能凭空发明关键事件。
            for ep in d.episodes:
                src = "".join(atoms[i - 1].text for i in ep.atom_ids
                              if 1 <= i <= len(atoms))
                for m in ep.must_show:
                    if event_score(src, m) < 0.25:
                        rep.warn(f"第 {ep.ep} 集必拍事件原文依据较弱：{m}"
                                 "（请确认它确实发生在该集原子区间内）")
                for prop in ep.key_props:
                    if _event_key(prop) not in _event_key(src):
                        rep.warn(f"第 {ep.ep} 集关键道具「{prop}」未在原文区间出现"
                                 "（可能记错名称）")
                for kl in ep.key_lines or []:
                    if event_score(src, kl) < 0.3:
                        rep.warn(f"第 {ep.ep} 集关键台词原文依据较弱：{kl}"
                                 "（剧本阶段会一字不改落拍，必须确认它在原文区间内）")
            return rep

        design = self._generate(
            "novel-series-design", SeriesDesign, sys, payload, check,
            name="series_design", max_tokens=65536, temperature=0.0)
        if max_episodes and len(design.episodes) > int(max_episodes):
            dropped = len(design.episodes) - int(max_episodes)
            design.episodes = design.episodes[:int(max_episodes)]
            self.progress(f"  设计要求只生成前 {int(max_episodes)} 集："
                          f"后 {dropped} 个戏剧单元本次不做")
        slices = design_slices(novel, atoms, design)
        self.design = design
        self.plot_atoms = atoms
        self.progress(f"  情节分集：{len(atoms)} 个原子 → {len(slices)} 集"
                      f"（按戏剧单元，不按字数/章节）")
        for d in design.episodes:
            self.progress(f"    EP{d.ep:02d} 原子 {d.atom_ids[0]}-{d.atom_ids[-1]}"
                          f"  {d.title}｜必拍 {len(d.must_show)} 条｜"
                          f"关键道具 {('、'.join(d.key_props) or '无')}")
        return design, slices

    # -- 0. 长文压缩 ------------------------------------------------------
    def prepare_source(self, text: str) -> str:
        text = (text or "").strip()
        if not text:
            raise ValueError("小说正文为空")
        novel = parse_text(text)
        codex = build_codex(
            novel, self.llm,
            limit=int(self.cfg.get("llm.story_source_chars", 9000)),
            chapters_per_volume=int(self.cfg.get("llm.story_volume_chapters", 15)),
            max_volumes=int(self.cfg.get("llm.story_max_volumes", 60)),
            workdir=self.workdir, resume=self.resume, progress=self.progress)
        self.codex = codex
        if codex.skipped:
            self.progress(f"  短篇 {len(text)} 字，跳过压缩")
        else:
            self.progress(f"  全书法典：{len(codex.text)} 字"
                          + ("（已截断）" if codex.truncated else ""))
        return codex.text

    # ------------------------------------------------------------------
    def bible(self, source: str, episodes: int, seconds: float,
              ratio: str) -> StoryBible:
        payload = {
            "source": source,
            "params": {"episodes": episodes, "target_seconds": seconds,
                       "aspect_ratio": ratio},
        }
        design = getattr(self, "design", None)
        if design is not None:
            # 圣经先看到“按情节分的集”，角色/场景/道具表才能服务真正的剧情，
            # 而不是从压缩后的 9000 字里凭空补人。
            payload["series_design"] = {
                "logline": design.logline,
                "episodes": [
                    {"ep": d.ep, "title": d.title,
                     "synopsis": d.synopsis,
                     "must_show": d.must_show,
                     "key_props": d.key_props,
                     "turn": d.turn, "climax": d.climax,
                     "cliff": d.cliff}
                    for d in design.episodes
                ],
            }
        sys = _system("你是短剧总策划兼改编编剧，擅长把小说变成可拍的竖屏短剧圣经。",
                      "产出全剧圣经：改编决策、故事脊柱、角色表、场景表、道具表、爽点。"
                      "不要写分集梗概。"
                      "必须输出 world 世界设定契约，四块都要给：factions(势力)/geography(地理)/"
                      "timeline(时间线)/rules(世界规则，含金手指边界)。"
                      "可选：若剧本确有明确的画风/胶片/镜头语言（如暗黑写实、赛博霓虹、"
                      "古装水墨等），可一并输出 visual_bible 字段覆盖默认画风；"
                      "visual_bible 用结构化四键：style_keywords(画风关键词)/main_colors(5-7个hex主色)"
                      "/do_dont(DO与DON'T规则)/reference_styles(参考风格名)；"
                      "不提供则自动用默认视觉圣经。"
                      "若输入含 series_design（按情节分的集与 must_show/key_props），"
                      "必须优先服务这些剧情：角色/场景/道具表要覆盖每集必拍事件，"
                      "不要按字数重新想象故事。",
                      StoryBible, BIBLE_RULES)

        def check(o: StoryBible) -> GateReport:
            if o.params.episodes != episodes:
                return GateReport("bible", [
                    f"params.episodes={o.params.episodes}，必须等于 {episodes}"])
            if abs(o.params.target_seconds - seconds) > 1e-6:
                return GateReport("bible", [
                    f"params.target_seconds={o.params.target_seconds}，必须等于 {seconds}"])
            if o.params.aspect_ratio != ratio:
                return GateReport("bible", [
                    f"params.aspect_ratio={o.params.aspect_ratio}，必须等于 {ratio}"])
            return validate_bible(o, strict=self.strict)

        return self._generate("novel-bible", StoryBible, sys, payload, check,
                              name="bible", max_tokens=65536, temperature=0.0)

    def episode_plans(self, bible: StoryBible,
                      slices: list[EpisodeSlice], seconds: float) -> Outline:
        """分集梗概按批生成（每批 ≤ story_episode_batch 集），逐批缓存。"""
        n = bible.params.episodes
        if len(slices) != n:
            raise ValueError(f"切片 {len(slices)} 集与 params.episodes={n} 不一致")
        batch_size = max(1, int(self.cfg.get("llm.story_episode_batch", 10)))
        design = getattr(self, "design", None)
        design_by_ep = ({d.ep: d for d in design.episodes}
                        if design is not None else {})
        if design_by_ep:
            # 情节模式的每集原文更长：批小一点，避免中段被摘录裁掉。
            batch_size = min(batch_size, max(1, int(self.cfg.get(
                "llm.story_plot_episode_batch", 3))))
        sys = _system("你是短剧分集策划，负责把全剧圣经拆成连续的分集梗概。",
                      "只为本批要求的集号写分集梗概；引用只能落在圣经已有 id 上。"
                      "每集必须给 emotion_curve（情绪走向标签序列，从"
                      "紧张/悬念/转折/爆发/温情/低落/舒缓/高潮/对峙/抉择 中选，"
                      "按本集情绪先后顺序排列）。"
                      "若输入含 series_design，它是按情节单元写定的硬约束："
                      "must_show/key_props/turn/climax/cliff 必须逐条落实，"
                      "不得合并、删除或新造关键情节；synopsis 要写清这些事件"
                      "如何因果相连，而不是罗列。",
                      EpisodeBatch, EPISODE_RULES)

        plans: list[EpisodePlan] = []
        for start in range(1, n + 1, batch_size):
            batch = list(range(start, min(start + batch_size, n + 1)))
            # 先按单集上限、再按本批总预算均摊。集数少且切片放得下时整段给，
            # 不再固定只喂开头/结尾（中段出场的角色会丢）。
            per = min(EPISODE_EXCERPT,
                      max(800, EPISODE_BATCH_CHARS // len(batch)))
            excerpts = [
                _focus_excerpt(
                    slices[ep - 1].text,
                    ([*design_by_ep[ep].must_show, *design_by_ep[ep].key_props]
                     if ep in design_by_ep else []),
                    per)
                for ep in batch]
            payload = {
                "bible": bible.model_dump(mode="json"),
                "batch": {"from": batch[0], "to": batch[-1]},
                "episodes": [
                    {"ep": ep, "target_seconds": seconds,
                     "source_excerpt": excerpts[i]}
                    for i, ep in enumerate(batch)
                ],
                "series_design": [
                    {"ep": ep,
                     **design_by_ep[ep].model_dump(mode="json")}
                    for ep in batch if ep in design_by_ep
                ] or None,
                "previous_synopsis": (
                    plans[-1].model_dump(mode="json") if plans else None),
            }

            def check(batch_obj: EpisodeBatch,
                      want: list[int] = batch) -> GateReport:
                # 先写入情节设计的硬字段，再校验：模型漏写/改写 must_show
                # 时按权威设计补齐，且补齐后的结果本身也要过门。
                if design_by_ep:
                    _apply_design_to_plans(
                        batch_obj.episodes, design_by_ep, seconds,
                        bible=bible, progress=self.progress)
                return validate_episode_batch(
                    bible, batch_obj.episodes, want, strict=self.strict)

            got = self._generate(
                "novel-episodes", EpisodeBatch, sys, payload, check,
                name=f"episodes_batch_{start:03d}", max_tokens=65536,
                temperature=0.0)
            plans.extend(got.episodes)
            self.progress(f"  分集梗概 {batch[0]}–{batch[-1]} 集通过质量门")

        outline = Outline(**bible.model_dump(), episodes=plans)
        _prune_unused_props(outline, self.progress)
        _repair_unused_characters(outline, slices, self.progress)
        _demote_unused_primary_scenes(outline, self.progress)
        rep = validate_outline(outline, strict=self.strict)
        if not rep.ok:
            raise StoryGateError("outline", rep)
        return outline

    def outline(self, source: str, episodes: int, seconds: float, ratio: str,
                slices: list[EpisodeSlice] | None = None) -> Outline:
        """兼容入口：圣经 + 分集梗概。"""
        bible = self.bible(source, episodes, seconds, ratio)
        return self.episode_plans(
            bible, slices or _fallback_slices(source, episodes), seconds)

    def cast(self, outline: Outline) -> Cast:
        payload = {"outline": outline.model_dump(mode="json")}
        lang = _prompt_lang_label(self.params.prompt_language)
        sys = _system("你是短剧角色设计总监。",
                      "为大纲里的每个角色产出可跨集复用的身份锁、服装状态、"
                      f"{lang}定妆提示词与音色。"
                      "每个主要角色（protagonist/major）必须给 appearance_block："
                      f"一段{lang}不可变外貌块，把体型/五官/印记/肤色/发型写死，"
                      "跨集逐字复用（可变项如服装单独走 wardrobe_state）。"
                      "并给 persona.relationship_spec：结构化关系列表，每条 type/target/note。",
                      Cast, _cast_rules(self.params),
                      prompt_language=self.params.prompt_language)
        return self._generate(
            "novel-cast", Cast, sys, payload,
            lambda c: validate_cast(c, outline, strict=self.strict),
            name="cast", max_tokens=65536, temperature=0.0)

    def art(self, outline: Outline, cast: Cast) -> Art:
        payload = {"outline": outline.model_dump(mode="json"),
                   "cast": cast.model_dump(mode="json")}
        sys = _system("你是短剧美术指导，负责场景概念图与道具设定图的提示词。",
                      "产出每个场景与道具的空景提示词、空间锚点、光照状态和道具状态。"
                      "每个场景必须给 location_type(INT/EXT/INT-EXT) 和 time_of_day(DAY/NIGHT/DAWN/DUSK)"
                      "（剧本工业字段）；每个道具给 category(Prop=角色交互道具 / SetDressing=陈设)。",
                      Art, _art_rules(self.params),
                      prompt_language=self.params.prompt_language)
        return self._generate(
            "novel-art", Art, sys, payload,
            lambda a: validate_art(a, outline, cast, strict=self.strict),
            name="art", max_tokens=65536, temperature=0.0)

    def script(self, ep: int, outline: Outline, cast: Cast, art: Art,
               *, slices: list[EpisodeSlice] | None = None,
               source: str = "",
               prev_episode: ScriptEpisode | None = None) -> ScriptEpisode:
        plan = next(e for e in outline.episodes if e.ep == ep)
        sp = self.params
        source_text = _slice_text(
            slices, ep, source, SCRIPT_EXCERPT,
            anchors=[*plan.must_show, *plan.key_props])
        dialogue_metrics = _dialogue_metrics(source_text)
        dialogue_light = bool(dialogue_metrics["dialogue_light"])
        # 时长预算：动作拍与台词语速取配置。模型最容易在这里“拍脑袋”写超，
        # 所以把可执行的节拍上限直接算出来钉进提示词与 payload。
        target = outline.params.target_seconds
        budget = _script_budget(target, sp, dialogue_light=dialogue_light)
        line_limit = min(
            sp.max_line_chars,
            max(1, int(sp.max_shot_seconds * sp.chars_per_second)))
        max_seconds = budget["hard_max_seconds"]
        max_beats = budget["max_total_beats"]
        suggest_lines = budget["suggest_lines"]
        # C：跨集不变的字段放最前（参数/预算/世界观/角色/美术），
        # 让第 2 集能命中第 1 集建立的前缀缓存。
        payload = {
            "params": sp.model_dump(mode="json"),
            "budget": budget,
            "bible": _bible_payload(outline),
            "cast": cast.model_dump(mode="json"),
            "art": art.model_dump(mode="json"),
            "episode": ep,
            "episode_plan": plan.model_dump(mode="json"),
            "plot_contract": {
                "must_show": list(plan.must_show),
                "key_lines": list(getattr(plan, "key_lines", []) or []),
                "key_props": list(plan.key_props),
                "dramatic_question": plan.dramatic_question,
                "turn": plan.turn,
                "climax": plan.climax,
                "previous_cliff": prev_episode.cliff if prev_episode else "",
            },
            # 从 payload 第一层就能看到本集合法地点，避免模型只盯着 bible 的
            # S1–S5 自行往后编号。
            "allowed_scene_ids": list(plan.scene_ids),
            "neighbors": _neighbors(outline, ep),
            "prev_episode_end": _prev_end(prev_episode, ep),
            "source_excerpt": source_text,
            "dialogue_metrics": dialogue_metrics,
        }
        sys = _system("你是短剧编剧，写的是能被镜头拍出来的戏，不是小说。",
                      f"写第 {ep} 集完整剧本（动作/台词节拍流、场次戏剧字段、钩子）。"
                      "每个场景给 callbacks：本场台词回指的既往事件/前情线索列表。"
                      "**每一项都必须包含至少一个已存在 id（如“B1 龙卷风逼近”"
                      "“S1 房屋落地”）；无法映射到 C#/S#/B# 的前情不要写进 callbacks**"
                      "（纯文字线索会被判为无法核验）。"
                      "台词要贴角色口癖与语气，同一角色不要前后风格跳变。",
                      ScriptEpisode,
                      _script_rules(sp, dialogue_light=dialogue_light,
                                    suggest_lines=suggest_lines),
                      prompt_language=sp.prompt_language)
        # 输出结构硬性：钉死顶层=剧集、scenes=场景数组的层级，杜绝模型把单个场景当顶层输出
        sys += ("\n\n【输出结构·硬性】你要输出的【顶层对象】是整个剧集的剧本对象，"
                "必须且只能含剧集级键：ep / title / hook / cliff / hook_beat / scenes 等。"
                "其中 scenes 是一个【数组】，剧里每一场戏是数组里的一个元素，"
                "只有数组元素（一场戏）才含 id / location / time / lighting / characters / "
                "props / wardrobe / beats 这些场次字段。"
                "**场景字段（id、location、time、lighting、characters、props、wardrobe 等）"
                "绝对不要出现在顶层**——顶层只有剧集级键。本集所有场次都必须放进 scenes "
                "数组，一场不漏，不要只输出一场。")
        if ep > 1:
            sys += ("\n\n【跨集承接】本集是第 " + str(ep) + " 集，"
                    "开场必须显式承接上一集末尾状态（见 prev_episode_end）："
                    "谁在哪里、什么事件余波未了。开场第一拍不得无中生有重启，"
                    "要让观众自然接续上一集结尾。")
        sys += ("\n\n【可拍性】动作拍必须只写镜头拍得到的事：可见动作、可听见的声音、"
                "可见的表情与物件。禁止把角色内心想法直接写成动作拍"
                "（如「她觉得完了」）；心理活动请转化为 visible action / 台词 subtext / 物件反应。"
                "\n【电影化动作句】用文字导演：现在时、主动语态、只写可见可听、"
                "一个动作拍只写一个动作（「谁+对谁+做什么+结果」≤45 字），"
                "不要一段塞多个动作，不要被动句（「被他推到」→「她一把把他推开」）。"
                "\n【拍内状态】若某一拍内发生换装/道具易主/伤势等状态变化，"
                "在该拍 state_change 里登记（key=角色Cid）；并在该拍 state_out "
                "写本拍结束后的快照（方位/朝向/服装态/伤势），供下一拍对账。"
                "state_out/state_change 若本拍无变化就写 {} 或直接省略，"
                "**严禁写成 null**（null 会导致整段 JSON 解析失败）。"
                "\n【剧本层承接】每个动作拍必须写 state_from（承接上一拍末谁在哪/朝向）"
                "与 screen_side（left/right/center，守 180° 轴线不翻转）。"
                "\n【微表演】有对白的拍必须写 expression（中文表情/微表演，"
                "如：咬紧牙关/眼神闪躲），分镜层直接采用不临场发明。"
                f"\n【对白密度】全集合计台词约 {budget['target_chars']} 字"
                f"（约 {suggest_lines} 句，每句 ≤{line_limit} 字；"
                f"超过 {line_limit} 字会被拆拍），"
                "避免台词过密拖死画面或过稀没有对白推进。")
        if dialogue_light:
            sys += ("\n\n【少对白·叙事强化】payload.dialogue_metrics 显示原文对白稀薄。"
                    "本集不要用角色互相解释来补信息（会变成有声书/解说剧）：\n"
                    "- 把因果写成可见动作链：目标→动作→阻力→反应→代价，"
                    "每个关键事件至少给 action + reaction/insert 两拍；\n"
                    "- 用物件、空间、声音、身体反应承载信息（谁握紧箱子、谁后退半步、"
                    "什么声音让两人同时停住）；\n"
                    "- 只在画面确实无法交代时用 scene.narration："
                    "exposition 补前情/规则/时间跳跃，inner 补关键抉择的内心；\n"
                    "- 旁白必须叠在动作拍上，不能复述画面，也不能替代冲突。\n"
                    "- 对白压到 2-6 句，只保留推动冲突/暴露关系的句子。")
        else:
            sys += ("\n\n【旁白纪律】scene.narration 只在画面无法交代关键前情/规则/"
                    "时间跳跃，或角色关键抉择的内心时使用；能拍出来就不要旁白。")
        if sp.transition_mode != "off":
            sys += ("\n【场间转场】第 2 场起写 transition_in：优先 match_cut/"
                    "sound_bridge/action_bridge，并写共享 anchor；"
                    "只有真实时间/空间大跳才用 time_jump/fade/dissolve。"
                    "禁止上一场还在原地、下一场无动机硬切到新空间。")
        sys += (f"\n\n【时长预算·硬性】本集目标 {target:g}s，硬上限 {max_seconds:.1f}s；"
                f"动作拍固定 {sp.action_seconds:g}s、台词拍 = 字数/{sp.chars_per_second:g}s"
                "（标点也算字数）。"
                f"全集合计拍数（动作+台词）必须落在 {budget['min_total_beats']}–"
                f"{max_beats} 个之间（分镜一镜一拍，少于下限会在成片审计直接失败）；"
                f"建议 {budget['suggest_scenes']} 场、每场 {budget['suggest_beats_per_scene']} 拍、"
                f"全集合计约 {suggest_lines} 句台词。"
                "写完逐场累加时长；超预算时先删/合并动作拍，不要靠缩句硬塞。")
        cov = budget["coverage_required"]
        sys += (f"\n\n【覆盖率·硬性】每个 beat 必须写 coverage。全集硬配额："
                f"reaction ≥{cov['reaction']}、insert ≥{cov['insert']}、"
                f"establish ≥{cov['establish']}、pov ≥{cov['pov']}；"
                "其余拍用 action/dialogue/transition。分镜层会原样继承这些值，"
                "不会替剧本补拍；缺额直接判本集失败。")
        if plan.must_show:
            items = "\n".join(f"  {i}. {m}" for i, m in enumerate(plan.must_show, 1))
            props = "、".join(plan.key_props) if plan.key_props else "无"
            sys += ("\n\n【本集必拍情节·硬性】以下事件必须全部出现在戏里，"
                    "一条都不能少；在承载它的场景 major_event 里原样抄写该条：\n"
                    f"{items}\n"
                    f"关键道具：{props}。每件关键道具都必须真正出场，并给一个 "
                    "coverage=insert、emphasis=key_prop、visual_focus=道具名 的特写拍。"
                    "source_excerpt 只是素材范围，不是逐条拍摄清单：除 must_show 外的"
                    "事件只作背景/一句带过/留白，决不要为了“覆盖原文”把 90 秒塞成流水账。"
                    "把同一条 must_show 的动作、反应、物件细节放进同一场，"
                    "靠 causal_from 串起来。")
        if plan.dramatic_question.strip():
            sys += (f"\n本集戏剧问题：{plan.dramatic_question}；"
                    f"转折：{plan.turn}；高潮：{plan.climax}。"
                    "这些关键拍必须标 emphasis=highlight/climax 并写 visual_focus，"
                    "分镜会强制非静态运镜。")
        sys += ("\n\n【集末钩子·硬性】最后一场的 beat 必须是“结尾钩子”，且**至少 2 拍"
                "（动作拍合计 ≥5s，建议 5-10s）**：先用一个可见动作把 cliff 演出来，"
                "再给一个反应/定格收尾。最后一场禁止只写 1 拍。")
        sys += (f"\n\n【句长口径】单句 {sp.max_line_chars} 字是剧作硬上限，"
                f"但当前引擎单镜最长 {sp.max_shot_seconds:g}s；"
                f"请按 ≤{line_limit} 字（含标点）写，"
                f"超过 {line_limit} 字的台词会被自动拆成两拍。写完逐句数一遍。")
        allowed_ids = "、".join(plan.scene_ids)
        sys += (f"\n\n【场次编号·硬性】本集**只能**使用 allowed_scene_ids = "
                f"[{allowed_ids}] 里的地点 id，一个都不能新增、不能把编号往后排"
                "（写成 S6/S7/S8 会直接判为不存在）。场次不够就**重复使用**这些 id，"
                "同一地点可以在多场复用；场次按数组顺序推进即可。"
                "同地点/同时间/同人物的连续内容合并为一场，不要为凑场数拆成"
                "两个同名同景的假场次；需要更多场次时，用已有地点之间的切换来推进。")

        def check(s: ScriptEpisode) -> GateReport:
            # 技术字段由编译器/总策划决定，不允许模型改
            s.ep = ep
            s.title = s.title or plan.title
            s.target_seconds = outline.params.target_seconds
            s.hook = s.hook or plan.hook
            s.cliff = s.cliff or plan.cliff
            s.must_show = list(plan.must_show)
            s.key_props = list(plan.key_props)
            # 机器可修的确定性问题先修掉，再跑门；避免为一条可计算的规则烧掉整轮重写
            _repair_script_output(s, outline, art, self.progress, cast,
                                  params=sp, dialogue_light=dialogue_light)
            rep = validate_script(
                s, outline, cast, art, sp, strict=self.strict,
                dialogue_light=dialogue_light,
                enforce_transitions=sp.transition_mode != "off")
            # 关键台词落拍硬门：key_lines 必须一字不改出现在 beats 台词里，
            # 不允许改写/删减/转述（用户最高优先级的叙事要求）。
            # 允许拆拍：整句超长时拆成连续拍（前后拼接仍是原文完整一句；
            # 上限 5 拍，防止把整场台词误拼成一句）。中间可夹动作/反应拍，
            # 与剧本提示词"拆成连续拍、中间可夹动作/反应拍"的口径一致。
            beat_lines = [b.line or "" for sc_ in s.scenes
                          for b in (sc_.beats or [])]
            for kl in (plan.key_lines or []):
                body = kl.strip().strip("\u201c\u201d\"' “” '").strip()
                if not body:
                    continue
                hit = _key_line_spans(beat_lines, body)
                if not hit:
                    rep.add(f"关键台词未原样落拍（必须一字不改，不得改写/删减/转述）：{kl}")
            if rep.warnings:
                for w in rep.warnings:
                    self.progress(f"  [WARN] {w}")
            return rep
        return self._generate(
            "novel-script", ScriptEpisode, sys, payload, check,
            name=f"script_ep{ep:02d}", max_tokens=131072, temperature=0.0)

    def staging(self, ep: ScriptEpisode, art: Art, *,
                slices: list[EpisodeSlice] | None = None,
                source: str = "") -> Staging:
        payload = {
            # C：参数/场次表在前，逐集变化的内容在后
            "params": self.params.model_dump(mode="json"),
            "scenes": [a.model_dump(mode="json") for a in art.scenes
                       if a.id in {s.id for s in ep.scenes}],
            "episode": ep.model_dump(mode="json"),
            "source_excerpt": _slice_text(slices, ep.ep, source, STAGING_EXCERPT),
        }
        sys = _system("你是短剧现场调度/分镜师，负责 2D 平面站位。",
                      "为本集每一场写地点物件表与人物站位；集号见 payload.episode。",
                      Staging, STAGING_RULES)

        def check(s: Staging) -> GateReport:
            _repair_staging_output(s, ep, self.progress)
            return validate_staging(s, ep, art, strict=self.strict)

        return self._generate(
            "novel-staging", Staging, sys, payload, check,
            name=f"staging_ep{ep.ep:02d}", max_tokens=65536, temperature=0.0)

    def action_line(self, ep: ScriptEpisode, scene_index: int, scene,
                    art: Art, cast: Cast, *, state_in: CutState,
                    prev_cuts: list[StoryCut],
                    slices: list[EpisodeSlice] | None = None,
                    source: str = "") -> ActionBeatLine:
        """第一级（Sequence logic）：先把一场切成语义连续的动作节拍线。

        每拍声明 from_prev（从哪来）/ action（做什么）/ to_next（到哪去），
        先锁定"切几镜、每镜做哪个动作节拍"，再由 scene_board 逐拍成镜。
        """
        # C：静态前缀放最前（world_bible / 本集原文 / 集信息 / 参数），
        # 同一集各场能命中 DeepSeek 前缀缓存；场次相关内容放后面。
        payload = {
            "world_bible": _world_bible(cast, art),
            "source_excerpt": _slice_text(slices, ep.ep, source, BOARD_EXCERPT),
            "episode": {"ep": ep.ep, "title": ep.title,
                        "target_seconds": ep.target_seconds,
                        "hook": ep.hook, "cliff": ep.cliff},
            "params": self.params.model_dump(mode="json"),
            "cast": _cast_payload(cast, set(scene.characters)),
            "scene_index": scene_index,
            "scene": scene.model_dump(mode="json"),
            "state_in": state_in.model_dump(mode="json"),
            "previous_cuts": [_cut_brief(c) for c in prev_cuts[-8:]],
        }
        sys = _system("你是短剧导演，先把这场戏切成语义连续的动作节拍线。",
                      "按本场剧作节拍切分动作节拍；集号与场号见 payload.episode / "
                      "scene_index，不要自己改场次。"
                      "每拍必须声明：\n"
                      "- beats：认领本场第几拍到第几拍（不重不漏覆盖全部剧作拍、按顺序连续）；\n"
                      "- goal：本拍为叙事完成什么（一句话）；\n"
                      "- from_prev：从上一拍结束处承接什么（谁/在哪/在做什么）；\n"
                      "- action：本拍单一动作，起点→过程→结果，一句话可表达；\n"
                      "- to_next：本拍结束把谁送到哪、下一拍承接什么；\n"
                      "- who / where / facing：本拍动作主体（Cid）、发生方位、主体朝向"
                      "（方位/朝向往受控词表靠：center/north/south/east/west/…/"
                      "off-screen/toward_camera/away_from_camera），不要漏、不要用叙述句代替；\n"
                      "- suggested_shot：本拍建议镜头类型/景别/承接"
                      "（如 过肩反打、中景动作、特写反应、推镜），供下一级成镜直接采用。\n"
                      "- state_from：本拍起始承接（直接抄剧本拍 state_from：谁在哪/朝向/在做什么），"
                      "是承接意图不是新设计；\n"
                      "- screen_side：建议机位侧 left/right/center——同一主体跨拍不得无理由左右翻转"
                      "（越轴）；确需换位必须在 where/动作里写出横移过程。\n"
                      "要求动作完整不截断、相邻拍因果衔接、空间移动不瞬移，"
                      "同一主体前后拍方位/朝向/屏幕侧要能对上（不越轴、不瞬移）。",
                      ActionBeatLine, ACTIONLINE_RULES)

        # P1-1 越轴（180°）确定性检查：同主体相邻拍 screen_side 左右翻转需有过渡动作
        def check(line: ActionBeatLine) -> GateReport:
            r = GateReport("novel-actionline")
            expected = list(range(1, len(scene.beats) + 1))
            if line.claimed() != expected:
                r.add(f"动作节拍认领不连续/有漏：{line.claimed()}，应为 {expected}")
            for b in line.beats:
                if not b.from_prev.strip():
                    r.add(f"{b.beats} 拍缺少 from_prev（承接起点）")
                if not b.action.strip():
                    r.add(f"{b.beats} 拍缺少 action（单一动作）")
                if not b.to_next.strip():
                    r.add(f"{b.beats} 拍缺少 to_next（去向）")
                # P0-C1 方位约束：空间细节不得留给分镜临场发明
                if not b.who.strip():
                    r.warn(f"{b.beats} 拍缺 who（沿用 from_prev 主体，建议显式给出）")
                if not b.where.strip():
                    r.add(f"{b.beats} 拍缺少 where（发生方位）")
                if not b.facing.strip():
                    r.add(f"{b.beats} 拍缺少 facing（主体朝向）")
                # P0-C3 镜头建议：缺建议的分镜仍需临场决定节奏
                if not b.suggested_shot.strip():
                    r.warn(f"{b.beats} 拍缺 suggested_shot（建议镜头），分镜将临场决定")
                # P1-1 剧本层承接字段：第一级就把承接意图定死，第二级只执行
                if not b.state_from.strip():
                    r.add(f"{b.beats} 拍缺 state_from（承接上一拍末：谁在哪/朝向）")
                if (b.screen_side or "").strip() not in {"left", "right", "center"}:
                    r.add(f"{b.beats} 拍 screen_side={b.screen_side!r} 非法"
                          "（受控 left/right/center）")
            flip = screen_axis_flip(line.beats)
            if flip:
                r.add(flip)
            return r

        return self._generate(
            f"novel-actionline 第{ep.ep}集第{scene_index}场", ActionBeatLine,
            sys, payload, check,
            name=f"actionline_ep{ep.ep:02d}_scene{scene_index:02d}",
            max_tokens=65536, temperature=0.0)

    def _scene_edl(self, ep: ScriptEpisode, scene_index: int, scene,
                   art: Art, cast: Cast, staging: Staging, *,
                   line: ActionBeatLine, state_in: CutState,
                   prev_cuts: list[StoryCut],
                   slices: list[EpisodeSlice] | None = None,
                   source: str = "") -> SegmentPlan:
        """B 第一级：只排 EDL（景别/机位/人物/道具/切理由/状态），不写提示词。"""
        sp = self.params
        art_scene = next((a for a in art.scenes if a.id == scene.id), None)
        staging_scene = next((s for s in staging.scenes
                              if s.ep == ep.ep and s.scene == scene_index), None)
        narration_by_beat = _narration_by_beat(scene)
        beat_plan = [
            {"beat": i, "seconds": round(_beat_seconds(b, sp), 2),
             "kind": "dialogue" if b.line else "action",
             "coverage": _coverage_of_beat(b),
             "emphasis": b.emphasis,
             "visual_focus": b.visual_focus,
             "narration": narration_by_beat.get(i, []),
             "summary": _beat_summary(b)}
            for i, b in enumerate(scene.beats, 1)
        ]
        payload = {
            # 静态前缀：同集各场逐字相同，命中前缀缓存
            "world_bible": _world_bible(cast, art),
            "source_excerpt": _slice_text(slices, ep.ep, source, BOARD_EXCERPT),
            "episode": {"ep": ep.ep, "title": ep.title,
                        "target_seconds": ep.target_seconds,
                        "hook": ep.hook, "cliff": ep.cliff},
            "params": sp.model_dump(mode="json"),
            "cast": _cast_payload(cast, set(scene.characters)),
            "scene_index": scene_index,
            "scene": scene.model_dump(mode="json"),
            "beat_plan": beat_plan,
            "transition_in": (scene.transition_in.model_dump(mode="json")
                              if scene.transition_in is not None else None),
            "action_line": line.model_dump(mode="json"),
            "art_scene": (art_scene.model_dump(mode="json")
                          if art_scene is not None else None),
            "staging_scene": (staging_scene.model_dump(mode="json")
                              if staging_scene is not None else None),
            "state_in": state_in.model_dump(mode="json"),
            "previous_cuts": [_prompt_brief(c) for c in prev_cuts[-8:]],
            "contract": {
                "cut_count": len(scene.beats),
                "min_cuts": min(4, len(scene.beats)),
                "max_shot_seconds": sp.max_shot_seconds,
                "note": "cuts 数组第 N 项就是第 N 拍；不要输出 seconds/coverage/"
                        "frame/prompt_en/motion/purpose/information/beat_goal/subjects",
            },
        }
        sys = _system(
            "你是导演级短剧分镜师，只负责排 EDL（切点、景别、机位、人物、道具、状态）。",
            "把 action_line 的每个动作节拍落成一个切镜：一镜一拍、按顺序不重不漏。"
            "集号/场号见 payload.episode / scene_index。\n"
            "**不要写视觉提示词**：prompt_en / motion / frame / seconds / coverage / "
            "purpose / information / beat_goal / subjects 都由代码从剧本拍确定性推导。"
            "你只需给 size / camera / characters / props / cut_reason / sfx / "
            "state_out / allow_multi_face。",
            SegmentPlan, _edl_rules(sp))

        def check(plan: SegmentPlan) -> GateReport:
            plan.scene_index = scene_index
            r = GateReport("novel-board-edl")
            # 先做确定性归一，再装配/过门：漏拍、多拍、非法引用、非法服装态
            # 都在这里修掉，不把可计算的错误变成整段重写。
            _normalize_edl_plan(plan, scene, cast, art, state_in=state_in,
                                progress=self.progress)
            expected = list(range(1, len(scene.beats) + 1))
            got = [i for i, _ in _normalize_plan_units(plan, scene)]
            if got != expected:      # 归一后仍不齐，说明代码/模型数据异常
                r.add(f"cuts 必须按顺序一镜一拍覆盖 {expected}，当前为 {got}")
            seg = _assemble_segment(
                plan, scene, line, state_in, None, sp,
                seg_id=f"E{ep.ep:02d}-{scene_index:02d}",
                scene_index=scene_index, cast=cast, art=art)
            _repair_segment_output(seg, scene, self.progress, params=sp,
                                   cast=cast)
            rep = validate_segment_cuts(
                seg, scene, cast, state_in=state_in,
                prev_cuts=prev_cuts, params=sp, strict=self.strict)
            r.errors.extend(rep.errors)
            r.warnings.extend(rep.warnings)
            if r.warnings:
                for w in r.warnings:
                    self.progress(f"  [WARN] {w}")
            return r

        return self._generate(
            f"novel-board-edl 第{ep.ep}集第{scene_index}场", SegmentPlan,
            sys, payload, check,
            name=f"edl_ep{ep.ep:02d}_scene{scene_index:02d}",
            max_tokens=16384, temperature=0.0)

    def _render_scene(self, prepared: _PreparedScene) -> StorySegment:
        """B 第二级：并行展开每镜的英文首帧提示词 + 画面内动作。"""
        p = prepared
        sp = self.params
        line = p.line
        scene = p.scene
        plan = p.plan
        skeleton = p.skeleton
        by_index = _action_beats_by_index(line)
        narration_by_beat = _narration_by_beat(scene)
        cut_plan: list[dict[str, Any]] = []
        for k, cut in enumerate(skeleton.cuts, 1):
            beat = scene.beats[k - 1]
            ab = by_index.get(k)
            cut_plan.append({
                "index": k,
                "beats": list(cut.beats),
                "size": cut.size,
                "camera": cut.camera,
                "coverage": cut.coverage,
                "emphasis": cut.emphasis,
                "visual_focus": cut.visual_focus,
                "characters": cut.characters,
                "props": cut.props,
                "cut_reason": cut.cut_reason,
                "action": cut.shot,
                "expression": beat.expression,
                "who": getattr(ab, "who", "") if ab is not None else "",
                "where": getattr(ab, "where", "") if ab is not None else "",
                "facing": getattr(ab, "facing", "") if ab is not None else "",
                "screen_side": getattr(ab, "screen_side", "") if ab is not None else "",
                "frame": cut.frame,
                "narration": narration_by_beat.get(k, []),
                "state_from": cut.state_from.model_dump(mode="json"),
                "state_out": cut.state_out.model_dump(mode="json"),
            })
        payload = {
            "world_bible": _world_bible(p.cast, p.art),
            "source_excerpt": _slice_text(p.slices, p.ep.ep, p.source,
                                          BOARD_EXCERPT),
            "episode": {"ep": p.ep.ep, "title": p.ep.title,
                        "target_seconds": p.ep.target_seconds,
                        "hook": p.ep.hook, "cliff": p.ep.cliff},
            "params": sp.model_dump(mode="json"),
            "scene_index": p.index,
            "scene": scene.model_dump(mode="json"),
            "cut_plan": cut_plan,
            "transition_in": (scene.transition_in.model_dump(mode="json")
                              if scene.transition_in is not None else None),
            "cast": _cast_payload(p.cast, set(scene.characters)),
            "staging_scene": next(
                (s.model_dump(mode="json") for s in p.staging.scenes
                 if s.ep == p.ep.ep and s.scene == p.index), None),
            "state_in": p.state_in.model_dump(mode="json"),
            "previous_cuts": [_prompt_brief(c) for c in p.prev_cuts[-8:]],
            "contract": {
                "prompt_count": len(skeleton.cuts),
                "note": "prompts 第 N 项对应 cut_plan.cuts 第 N 项；"
                        "只输出 prompt_en 和 motion",
            },
        }
        sys = _system(
            "你是电影摄影指导，把已经定好的切镜写成可直接喂图像模型的首帧提示词。",
            "按 cut_plan 逐镜写英文首帧画面与画面内动作；集号/场号见 payload。\n"
            "**只输出 prompts 数组**，每项只有 prompt_en 与 motion。"
            "不要重排镜头、不要增删镜头、不要输出其他字段。",
            SegmentPrompts, _prompt_rules(sp),
            prompt_language=sp.prompt_language)

        def check(batch: SegmentPrompts) -> GateReport:
            r = GateReport("novel-board-prompts")
            if len(batch.prompts) != len(skeleton.cuts):
                r.add(f"prompts 必须与 cut_plan 一一对应：需要 "
                      f"{len(skeleton.cuts)} 项，实际 {len(batch.prompts)} 项")
            seg = _assemble_segment(
                plan, scene, line, p.state_in, batch.prompts, sp,
                seg_id=f"E{p.ep.ep:02d}-{p.index:02d}",
                scene_index=p.index, cast=p.cast, art=p.art)
            _repair_segment_output(seg, scene, self.progress, params=sp,
                                   cast=p.cast)
            rep = validate_segment_cuts(
                seg, scene, p.cast, state_in=p.state_in,
                prev_cuts=p.prev_cuts, params=sp, strict=self.strict)
            r.errors.extend(rep.errors)
            r.warnings.extend(rep.warnings)
            for i, item in enumerate(batch.prompts, 1):
                if not item.motion.strip():
                    r.warn(f"第 {i} 镜 motion 为空（建议补一句画面内动作）")
            for msg in _identity_repetition_warnings(seg, p.cast):
                r.warn(msg)
            if r.warnings:
                for w in r.warnings:
                    self.progress(f"  [WARN] {w}")
            return r

        batch = self._generate(
            f"novel-board-prompts 第{p.ep.ep}集第{p.index}场", SegmentPrompts,
            sys, payload, check,
            name=f"shots_ep{p.ep.ep:02d}_scene{p.index:02d}",
            max_tokens=16384, temperature=0.0)
        seg = _assemble_segment(
            plan, scene, line, p.state_in, batch.prompts, sp,
            seg_id=f"E{p.ep.ep:02d}-{p.index:02d}", scene_index=p.index,
            cast=p.cast, art=p.art)
        _repair_segment_output(seg, scene, self.progress, params=sp,
                               cast=p.cast)
        rep = validate_segment_cuts(
            seg, scene, p.cast, state_in=p.state_in,
            prev_cuts=p.prev_cuts, params=sp, strict=self.strict)
        if not rep.ok:
            raise StoryGateError(f"novel-board-prompts 第{p.index}场", rep)
        return seg

    def _prepare_scene(self, ep: ScriptEpisode, scene_index: int, scene,
                       art: Art, cast: Cast, staging: Staging, *,
                       state_in: CutState, prev_cuts: list[StoryCut],
                       slices: list[EpisodeSlice] | None,
                       source: str) -> _PreparedScene:
        line = self.action_line(ep, scene_index, scene, art, cast,
                                state_in=state_in, prev_cuts=prev_cuts,
                                slices=slices, source=source)
        plan = self._scene_edl(ep, scene_index, scene, art, cast, staging,
                               line=line, state_in=state_in,
                               prev_cuts=prev_cuts, slices=slices,
                               source=source)
        skeleton = _assemble_segment(
            plan, scene, line, state_in, None, self.params,
            seg_id=f"E{ep.ep:02d}-{scene_index:02d}",
            scene_index=scene_index, cast=cast, art=art)
        _repair_segment_output(skeleton, scene, self.progress,
                               params=self.params, cast=cast)
        return _PreparedScene(
            ep=ep, index=scene_index, scene=scene, line=line, plan=plan,
            skeleton=skeleton, state_in=state_in,
            prev_cuts=list(prev_cuts), art=art, cast=cast, staging=staging,
            slices=slices, source=source)

    def scene_board(self, ep: ScriptEpisode, scene_index: int, scene,
                    art: Art, cast: Cast, staging: Staging, *,
                    state_in: CutState,
                    prev_cuts: list[StoryCut],
                    slices: list[EpisodeSlice] | None = None,
                    source: str = "") -> StorySegment:
        """单场兼容入口：EDL + 视觉展开；board() 会用跨场并行版本。"""
        prepared = self._prepare_scene(
            ep, scene_index, scene, art, cast, staging, state_in=state_in,
            prev_cuts=prev_cuts, slices=slices, source=source)
        seg = self._render_scene(prepared)
        self._save(f"board_ep{ep.ep:02d}_scene{scene_index:02d}", seg)
        return seg

    def board(self, ep: ScriptEpisode, art: Art, cast: Cast,
              staging: Staging, *, slices: list[EpisodeSlice] | None = None,
              source: str = "", state_in: CutState | None = None
              ) -> StoryEpisodeBoard:
        state = state_in.model_copy(deep=True) if state_in else CutState()
        segments: list[StorySegment | None] = [None] * len(ep.scenes)
        prev_cuts: list[StoryCut] = []
        prepared: list[_PreparedScene] = []

        # 第一遍顺序：动作线 + EDL。只有 state_out 需要跨场继承，提示词不需要。
        for i, scene in enumerate(ep.scenes, 1):
            self.progress(f"  第 {ep.ep} 集第 {i}/{len(ep.scenes)} 场 EDL")
            name = f"board_ep{ep.ep:02d}_scene{i:02d}"
            cached = self._load(name, StorySegment)
            if (cached is not None and self._prompt_policy_sensitive(name)
                    and not self._prompt_policy_ok(name)):
                self.progress(
                    f"  [WARN] 提示词策略已变化（当前 {PROMPT_POLICY_REV}），"
                    f"重建 {name}.json")
                cached = None
            if cached is not None:
                cached.id = f"E{ep.ep:02d}-{i:02d}"
                cached.scene_index = i
                _repair_segment_output(cached, scene, self.progress,
                                       params=self.params, cast=cast)
                rep = validate_segment_cuts(
                    cached, scene, cast, state_in=state,
                    prev_cuts=prev_cuts, params=self.params,
                    strict=self.strict)
                if rep.ok:
                    self.progress(f"  复用 {name}.json（{len(cached.cuts)} 镜）")
                    segments[i - 1] = cached
                    prev_cuts.extend(cached.cuts)
                    state = cached.cuts[-1].state_out
                    continue
                self.progress(f"  缓存未过门，重生成："
                              + (rep.errors[0] if rep.errors else "未知错误"))
            p = self._prepare_scene(
                ep, i, scene, art, cast, staging, state_in=state,
                prev_cuts=prev_cuts, slices=slices, source=source)
            prepared.append(p)
            prev_cuts.extend(p.skeleton.cuts)
            state = p.skeleton.cuts[-1].state_out

        # 第二遍并行：视觉提示词互不依赖；各场 EDL/state 已固定。
        if prepared:
            workers = min(self.prompt_parallel, len(prepared))
            self.progress(f"  并行展开 {len(prepared)} 场分镜提示词"
                          f"（并发 {workers}）…")
            with ThreadPoolExecutor(max_workers=workers) as ex:
                rendered = list(ex.map(self._render_scene, prepared))
            for p, seg in zip(prepared, rendered):
                segments[p.index - 1] = seg
                self._save(f"board_ep{ep.ep:02d}_scene{p.index:02d}", seg)

        final_segments = [s for s in segments if s is not None]
        if len(final_segments) != len(ep.scenes):
            missing = [i for i, s in enumerate(segments, 1) if s is None]
            raise StoryGateError(
                "novel-storyboard",
                GateReport("storyboard", [f"第 {missing} 场没有生成分镜段"]))
        final_segments = _split_long_segments(
            final_segments, ep.ep, self.progress, params=self.params)
        board = StoryEpisodeBoard(ep=ep.ep,
                                  state_in=(state_in.model_copy(deep=True)
                                            if state_in else CutState()),
                                  segments=final_segments)
        rep = validate_board(board, ep, cast, self.params,
                             strict=self.strict)
        if not rep.ok:
            raise StoryGateError("novel-storyboard", rep)
        self._save(f"board_ep{ep.ep:02d}", board)
        return board

    # ------------------------------------------------------------------
    def run(self, text: str, episodes: int, seconds: float, ratio: str,
            until: str = "all",
            slices: list[EpisodeSlice] | None = None,
            episode_range: tuple[int, int] | None = None,
            split_sig: str = ""
            ) -> tuple[Outline, Cast | None, Art | None,
                       list[ScriptEpisode], Staging, StoryStoryboard | None]:
        # P0-③ 先锁定参数指纹：改集数/秒数/画幅/源文本/模型后 resume 不再静默复用旧产物
        self.set_fingerprint(episodes, seconds, ratio, source=text,
                             split=f"{split_sig}|rules={STORY_RULES_REV}")
        self.progress("① 读小说 / 压缩成长法典")
        # 从第 1 集开始、只生成前 N 集时，长篇压缩也只看前 N 集覆盖的原文：
        # 不为未生成的后文做分卷摘要（90 万字只做 2 集时尤其关键）。
        source_text = _range_source(text, slices, episode_range)
        if len(source_text) < len(text):
            self.progress(
                f"  只生成前 {episode_range[1]} 集：全文压缩只读前 "
                f"{len(source_text)} 字（全文 {len(text)} 字）")
        source = self.prepare_source(source_text)
        if slices is None:
            slices = _fallback_slices(source, episodes)
        if len(slices) != episodes:
            raise ValueError(f"切片 {len(slices)} 集与集数 {episodes} 不一致")

        # resume 时优先复用完整 outline.json：人工在大纲审阅页后修改的内容
        # 必须真正生效，不能被 bible/分集批次缓存覆盖回去。
        outline: Outline | None = None
        if self.resume:
            # outline 是"人工审阅可编辑"产物：除参数校验外还要核对输入指纹。
            # 指纹只覆盖 episodes/seconds/ratio/源文本/模型，不包含 outline.json
            # 的内容哈希，所以人工改完大纲后续跑仍会复用（不会把修改覆盖回去）。
            cached_outline: Outline | None = None
            op = self._path("outline")
            if op.is_file():
                try:
                    cached_outline = Outline.model_validate(
                        json.loads(op.read_text(encoding="utf-8-sig")))
                except Exception:  # noqa: BLE001 - 坏了就重生成
                    cached_outline = None
            if cached_outline is not None:
                if (cached_outline.params.episodes != episodes
                        or abs(cached_outline.params.target_seconds - seconds) > 1e-6
                        or cached_outline.params.aspect_ratio != ratio):
                    self.progress("  [WARN] outline 缓存参数与本次不一致，重新生成")
                elif not self._fp_ok("outline", missing_ok=True):
                    self.progress("  [WARN] 源文本/模型已变化，outline 缓存失效，重新生成")
                else:
                    # 人工改过大纲也没关系：指纹记录的是“本次 run 的输入参数”，
                    # 不是 outline.json 的文件哈希，所以编辑内容仍会被复用。
                    rep = validate_outline(cached_outline, strict=self.strict)
                    if rep.ok:
                        outline = cached_outline
                        if self._resume_fp:
                            # 旧工作层没有 outline 指纹键：这次复用成功即补写，
                            # 下一次换源/换模型才能真正判失效。
                            self._touch_fp("outline")
                    else:
                        self.progress("  [WARN] outline 缓存未过门，重新生成："
                                      + (rep.errors[0] if rep.errors else ""))

        if outline is None:
            self.progress("② 全局圣经（改编决策 / 角色 / 场景 / 道具 / 爽点）")
            bible = self.bible(source, episodes, seconds, ratio)
            self.progress("③ 分集梗概（按批生成）")
            outline = self.episode_plans(bible, slices, seconds)
            if isinstance(outline, BaseModel):
                self._save("outline", outline)

        if until == "outline":
            return outline, None, None, [], Staging(source=source), None

        self.progress("④ 角色身份锁")
        cast = self.cast(outline)
        self.progress("⑤ 场景与道具美术")
        art = self.art(outline, cast)

        if until == "art":
            return outline, cast, art, [], Staging(source=source), None

        scripts: list[ScriptEpisode] = []
        stagings: list[Staging] = []
        boards: list[StoryEpisodeBoard] = []
        last_state = CutState()          # 上一集终态 → 本集 state_in
        if episode_range and episode_range[0] > 1:
            # 范围生成也要接上前一集终态；上一集没生成过才从头开始（显式告警）
            prev_ep = episode_range[0] - 1
            prev_path = self._path(f"board_ep{prev_ep:02d}")
            if prev_path.is_file():
                try:
                    prev_board = StoryEpisodeBoard.model_validate(
                        json.loads(prev_path.read_text(encoding="utf-8-sig")))
                    last_state = prev_board.state_out
                    self.progress(f"  继承第 {prev_ep} 集终态 → "
                                  f"第 {episode_range[0]} 集 state_in")
                except Exception:  # noqa: BLE001 - 坏缓存不阻断，但必须可见
                    self.progress(f"  [WARN] 第 {prev_ep} 集分镜缓存损坏，"
                                  f"第 {episode_range[0]} 集 state_in 从头开始")
            else:
                self.progress(f"  [WARN] 没有第 {prev_ep} 集分镜缓存，"
                              f"第 {episode_range[0]} 集 state_in 从头开始")
        # B2：script/staging 只依赖 outline/cast/art/本集切片，跨集独立 → 线程池并行；
        # 仅 board 依赖上一集 state_out → 必须跨集顺序。故先并行 ⑥⑦，再顺序 ⑧。
        plan_eps = [p for p in outline.episodes
                    if not (episode_range and not (episode_range[0] <= p.ep <= episode_range[1]))]

        def _run_staging(item: tuple[EpisodePlan, ScriptEpisode]) -> tuple[int, Staging]:
            plan, ep = item
            return plan.ep, self.staging(ep, art, slices=slices, source=source)

        if plan_eps:
            self.progress(f"⑥⑦ 顺序生成 {len(plan_eps)} 集剧本 + 并行站位（并发 {self.parallel}）…")
            # P0-C5 剧本顺序生成：第 N 集写戏时可见上一集末态（跨集承接需前序），
            # 站位不依赖跨集，仍可并行。
            _scripts: list[tuple[int, ScriptEpisode]] = []
            prev: ScriptEpisode | None = None
            if episode_range and episode_range[0] > 1:
                # 范围生成也要接上前一集剧本末态：与分镜侧(继承 board_epN 终态)对齐，
                # 避免"从第 5 集开始"时写戏看不到第 4 集结尾状态、跨集承接断裂。
                prev_ep = episode_range[0] - 1
                prev_path = self._path(f"script_ep{prev_ep:02d}")
                if prev_path.is_file():
                    try:
                        prev = ScriptEpisode.model_validate(
                            json.loads(prev_path.read_text(encoding="utf-8-sig")))
                        self.progress(f"  继承第 {prev_ep} 集剧本末态 → "
                                      f"第 {episode_range[0]} 集剧本 state_in")
                    except Exception:  # noqa: BLE001 - 坏缓存不阻断，但必须可见
                        self.progress(f"  [WARN] 第 {prev_ep} 集剧本缓存损坏，"
                                      f"第 {episode_range[0]} 集剧本承接从头开始")
                else:
                    self.progress(f"  [WARN] 没有第 {prev_ep} 集剧本缓存，"
                                  f"第 {episode_range[0]} 集剧本承接从头开始")
            for p in plan_eps:
                ep_script = self.script(p.ep, outline, cast, art,
                                        slices=slices, source=source,
                                        prev_episode=prev)
                _scripts.append((p.ep, ep_script))
                prev = ep_script
            scripts = [s for _, s in sorted(_scripts)]
            with ThreadPoolExecutor(max_workers=self.parallel) as ex:
                stagings = [s for _, s in sorted(
                    ex.map(_run_staging, zip(plan_eps, scripts)))]
            self.progress(f"⑥⑦ {len(plan_eps)} 集剧本顺序完成、站位并行完成")

        for p, ep, st in zip(plan_eps, scripts, stagings):
            self.progress(f"⑧ 第 {p.ep} 集分镜（逐场顺序生成）")
            got = self.board(ep, art, cast, st, slices=slices, source=source,
                             state_in=last_state)
            boards.append(got)
            last_state = got.state_out

        staging = _merge_stagings(stagings, outline.source)
        board = StoryStoryboard(source=outline.source, aspect_ratio=ratio,
                                episodes=boards)
        return outline, cast, art, scripts, staging, board