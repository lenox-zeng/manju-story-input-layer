"""契约 §10.1 的**跨对象** block 门。

`schemas.py` 只能校验**单份文档内部**的规则（未知字段、枚举、二元遮挡环……）。
凡是"必须三份文档同时在场才判定得了"的规则，全部落在这里：

  - 跨对象引用：`char_id` / `state_id` / `disguise:<char_id>` 是否真的存在
  - `subjects` / `ref_characters` 越出本镜所属场景的 `chars_on_screen`
  - 时间轴：台词超时、`at_sec` 越界/重叠、镜头↔场景↔单集的时长容差
  - `occludes` 成环（schema 只抓得住二元互遮，三元环漏网）
  - 跨集 `continuity_ledger` 末态 → 下集初态

**为什么非要有这一层**：v9 出的事故清一色是"单看每份文档都合法、合起来才错"。
单文档校验天然给不了这个保证，而这类错误一旦漏到出图阶段，代价是整集返工。

设计约定
  - 纯函数、无副作用：不写文件、不读全局配置、不碰网络。同输入同输出。
    （跨层上下文一律显式传 `_Ctx`，不用模块级全局 —— 全局会让并发跑两集时串味。）
  - 默认**只出报告**（`Report`），不抛异常；`strict=True`（或 CLI `--strict`）才硬失败。
  - 输入既收 pydantic 模型、也收原始 dict —— 上游（LLM 产出、外部剧本）手里
    就是 dict，逼调用方先自己 `model_validate` 只会让这道门被绕过。

CLI::

    python -m manju.gate plan.json ep01.script.json ep01.storyboard.json --strict
"""

from __future__ import annotations

import argparse
import json
import math
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Literal, Sequence

from pydantic import ValidationError

from . import jsonio
from .schemas import Plan, Scene, Script, Storyboard, effective_requirements

# 契约 §7.1：默认语速，与 story.chars_per_second 同值。
DEFAULT_SPEECH_CPS = 4.5
# 契约 §10.1：时长容差。浮点累加与四舍五入不该卡死整集。
DEFAULT_TOLERANCE = 0.2
# 纯浮点零头，只用于"两句台词是否重叠"，不参与契约容差。
_EPS = 1e-6
_NARRATION_CPS = 5.0
# v10.2：口型段长的两条线（帧）。TTS 估长 = len(text)/speech_cps。
# 安全余量 2 帧：LatentSync 与 8n+1 取整都会吃掉 1 帧，留 2 帧才不至于贴边。
_DIALOGUE_FRAME_MARGIN = 2
# 成片帧率默认值；调用方可传 `fps=` 覆盖。
DEFAULT_FPS = 24.0
# 配音阶段允许的最大压缩倍率（渲染侧能力参数）。
# 超过这条线就是"怎么加速都塞不进本镜时长"，属硬错。
DEFAULT_MAX_ATEMPO = 1.5

# 同框多脸判定的"这个朝向不露脸"集合。只有**背影**算不露脸：
# `away` 是一张正脸都贡献不了；`profile_*` 仍是一张可辨识的脸，
# 依赖人脸参考的引擎拿它做参考同样会串。`facing` 缺省（None）按"可能露脸"算。
# 注意：是否限制同框人脸数由 `max_faces_per_shot` 决定，默认不限制。
_FACE_HIDDEN_FACING: frozenset[str] = frozenset({"away"})

FindingLevel = Literal["block", "warn", "info"]
_LEVEL_ORDER = {"block": 0, "warn": 1, "info": 2}

# 契约 §7.4：`continuity_ledger` 内层键必须与 `plan.continuity.mutable` **同名**。
# 所以规范键名从 plan 里**现取**，不在这里硬编码 —— 契约 §5 的示例写
# `wardrobe_states`，而 input/external/plan.json 写的是单数 `wardrobe_state`，
# 两者只要有一个被写死，另一个就会被误判成违规。以文档自身为准才是"同名"的本意。
LEDGER_FALLBACK_KEYS: tuple[str, ...] = ("wardrobe_state", "伤势", "持有道具", "关系变化")
# 口语别名 → 候选规范键（按序取第一个真实存在于 mutable 里的）。
LEDGER_ALIASES: dict[str, tuple[str, ...]] = {
    "换装": ("wardrobe_state", "wardrobe_states"),
    "获得道具": ("持有道具",),
    "失去道具": ("持有道具",),
    "人物关系": ("关系变化", "人物关系"),
    "关系变化": ("关系变化", "人物关系"),
}


def _canon_ledger_key(raw: str, keys: tuple[str, ...]) -> str | None:
    """把账本内层键归一到 mutable 里的规范键；归不了返回 None（→ block）。"""
    if raw in keys:
        return raw
    for cand in LEDGER_ALIASES.get(raw, ()):
        if cand in keys:
            return cand
    # 单复数兜底：wardrobe_state / wardrobe_states 是同一个键的两种拼法，
    # 不该因为一个 s 就把整集账本判违规
    for k in keys:
        if k.rstrip("s") == raw.rstrip("s"):
            return k
    return None


def _is_list_key(key: str) -> bool:
    """值为数组的键（空数组 = 明确"无道具"，与"未记录"不是一回事）。"""
    return "道具" in key


def _is_wardrobe_key(key: str) -> bool:
    """服装态键：值必须是该角色状态库里的 state_id。"""
    return "wardrobe" in key.lower() or "装" in key

# §7.3：这两个标记表示该角色不在当前时间线，状态变化不计入账本
_OFF_TIMELINE_FLAGS = ("flashback", "vision")


# ==========================================================================
# 报告
# ==========================================================================
@dataclass(frozen=True)
class Finding:
    """一条校验结论。`where` 一律写成"能直接拿去 grep"的定位串。"""

    level: FindingLevel
    code: str
    where: str
    message: str
    fix: str = ""

    def line(self) -> str:
        s = f"[{self.level}] {self.code} @ {self.where}：{self.message}"
        return f"{s}\n        修法：{self.fix}" if self.fix else s


@dataclass(frozen=True)
class Report:
    """一次完整校验的结果。"""

    findings: tuple[Finding, ...] = ()

    def of(self, level: FindingLevel) -> tuple[Finding, ...]:
        return tuple(f for f in self.findings if f.level == level)

    @property
    def blocks(self) -> tuple[Finding, ...]:
        return self.of("block")

    @property
    def warns(self) -> tuple[Finding, ...]:
        return self.of("warn")

    @property
    def infos(self) -> tuple[Finding, ...]:
        return self.of("info")

    @property
    def ok(self) -> bool:
        """无 block 即通过（warn / info 不阻断）。"""
        return not self.blocks

    def codes(self, level: FindingLevel = "block") -> list[str]:
        """测试用：只要编码，不要长文案。"""
        return [f.code for f in self.of(level)]

    @classmethod
    def combine(cls, *reports: Report) -> Report:
        items = [f for r in reports for f in r.findings]
        return Report(tuple(sorted(items, key=lambda f: _LEVEL_ORDER[f.level])))

    def raise_if_blocked(self) -> Report:
        if self.blocks:
            raise GateBlocked(self)
        return self

    def to_text(self, *, show_warn: bool = True) -> str:
        head = (f"契约门：block {len(self.blocks)} / warn {len(self.warns)}"
                f" / info {len(self.infos)}")
        lines = [head]
        lines += [f.line() for f in self.blocks]
        if show_warn:
            lines += [f.line() for f in self.warns]
        lines += [f.line() for f in self.infos]
        if not self.findings:
            lines.append("全部通过。")
        return "\n".join(lines)

    def to_json(self) -> list[dict[str, Any]]:
        return [
            {"level": f.level, "code": f.code, "where": f.where,
             "message": f.message, "fix": f.fix}
            for f in self.findings
        ]


class GateBlocked(Exception):
    """`strict=True` 且存在 block 时抛出，异常体里带着完整报告。"""

    def __init__(self, report: Report) -> None:
        self.report = report
        super().__init__(f"契约门未通过：{len(report.blocks)} 条 block\n"
                         + report.to_text(show_warn=False))


class _Findings:
    """收集器：顺手去重，免得一个跑偏的 char_id 刷出几十行同样的告警。"""

    def __init__(self) -> None:
        self._items: list[Finding] = []
        self._seen: set[tuple[str, str, str]] = set()

    def add(self, level: FindingLevel, code: str, where: str, message: str,
            fix: str = "") -> None:
        key = (code, where, message)
        if key in self._seen:
            return
        self._seen.add(key)
        self._items.append(Finding(level, code, where, message, fix))

    def report(self) -> Report:
        return Report(tuple(sorted(self._items, key=lambda f: _LEVEL_ORDER[f.level])))


# ==========================================================================
# 输入归一
# ==========================================================================
def _as_dict(obj: dict[str, Any] | str | Path) -> dict[str, Any]:
    if isinstance(obj, dict):
        return obj
    # BOM 与 markdown 围栏都在 jsonio 里统一处理：豆包导出的文件两样都常带，
    # 而这两样都不是**内容**错误，没理由让这道门因此停下
    data = jsonio.read(obj)
    if not isinstance(data, dict):
        raise ValueError(f"{obj}：顶层不是 JSON 对象")
    return data


def _first_errors(e: ValidationError, limit: int = 5) -> str:
    items = e.errors()
    shown = [
        f"{'/'.join(str(x) for x in it['loc']) or '<root>'}: {it['msg']}"
        for it in items[:limit]
    ]
    if len(items) > limit:
        shown.append(f"…… 另有 {len(items) - limit} 条")
    return "；".join(shown)


def _label_of(raw: Any, kind: str) -> str:
    if isinstance(raw, (str, Path)):
        return Path(raw).name
    ep = raw.get("episode") if isinstance(raw, dict) else getattr(raw, "episode", None)
    return f"ep{ep}" if ep else kind


def load_docs(paths: Iterable[str | Path]) -> tuple[dict[str, Any] | None,
                                                    list[dict[str, Any]],
                                                    list[dict[str, Any]]]:
    """按**顶层键**识别文件类型（文件名不作数），返回 (plan, scripts, boards) 的原始 dict。"""
    plan: dict[str, Any] | None = None
    scripts: list[dict[str, Any]] = []
    boards: list[dict[str, Any]] = []
    for p in paths:
        data = _as_dict(p)
        kind = _kind_of(data)
        if kind == "plan":
            plan = data
        elif kind == "storyboard":
            boards.append(data)
        elif kind == "script":
            scripts.append(data)
        else:
            raise ValueError(
                f"{p}：认不出类型（顶层既无 characters+visual_bible，也无 scenes / shots）")
    return plan, scripts, boards


def _kind_of(data: dict[str, Any]) -> Literal["plan", "script", "storyboard", "unknown"]:
    if "characters" in data and "visual_bible" in data:
        return "plan"
    if "shots" in data:
        return "storyboard"
    if "scenes" in data:
        return "script"
    return "unknown"


def classify_path(path: str | Path) -> tuple[str, dict[str, Any] | None, str]:
    """认一个文件是什么：`(kind, 原始 dict 或 None, 说明)`。

    kind ∈ `plan` / `script` / `storyboard` / `unknown` / `unreadable`。
    **文件名不作数** —— 豆包导出的命名五花八门，只有顶层键可靠。

    公开出来是给 `tools/batch_text.py` 的目录发现用的：它要按类型收集素材，
    但"什么算 plan"这件事只能有一个实现，否则两处判定迟早会漂移。
    """
    try:
        data = _as_dict(path)
    except (OSError, ValueError, json.JSONDecodeError) as e:
        return "unreadable", None, str(e)
    return _kind_of(data), data, ""


def _harvest_schemas(
    fs: _Findings,
    plan_raw: Any | None,
    scripts_raw: Sequence[Any],
    boards_raw: Sequence[Any],
) -> tuple[Plan | None, list[Script], list[Storyboard]]:
    """先各自过一遍 schema，**过不了的文档直接剔除**。

    带着半截对象继续跑跨对象校验，只会产出一堆由第一处错误派生出来的假告警，
    把真正的根因淹掉。schema 已经报过一次，这里不再重复报。
    """
    plan: Plan | None = None
    if plan_raw is not None:
        try:
            plan = plan_raw if isinstance(plan_raw, Plan) else Plan.model_validate(_as_dict(plan_raw))
        except ValidationError as e:
            fs.add("block", "schema.invalid", "plan",
                   f"plan.json 不符合 v10.1：{_first_errors(e)}",
                   "先修 schema（枚举值 / 未知字段 / 必填项），再谈跨对象校验")
    good_scripts: list[Script] = []
    for raw in scripts_raw:
        label = _label_of(raw, "script")
        try:
            good_scripts.append(raw if isinstance(raw, Script) else Script.model_validate(_as_dict(raw)))
        except ValidationError as e:
            fs.add("block", "schema.invalid", label,
                   f"{label} 不符合 v10.1：{_first_errors(e)}", "先修 schema 再谈跨对象校验")
    good_boards: list[Storyboard] = []
    for raw in boards_raw:
        label = _label_of(raw, "storyboard")
        try:
            good_boards.append(raw if isinstance(raw, Storyboard) else Storyboard.model_validate(_as_dict(raw)))
        except ValidationError as e:
            fs.add("block", "schema.invalid", label,
                   f"{label} 不符合 v10.1：{_first_errors(e)}", "先修 schema 再谈跨对象校验")
    return plan, good_scripts, good_boards


# ==========================================================================
# 校验上下文
# ==========================================================================
@dataclass
class _Ctx:
    """跨对象校验需要的一切。显式传递，不用全局。"""

    plan: Plan
    cast_ids: set[str] = field(default_factory=set)
    states: dict[str, set[str]] = field(default_factory=dict)
    colors: dict[str, str] = field(default_factory=dict)
    tolerance: float = DEFAULT_TOLERANCE
    speech_cps: float = DEFAULT_SPEECH_CPS
    episode_sec: float | None = None
    fps: float = DEFAULT_FPS
    max_atempo: float = DEFAULT_MAX_ATEMPO
    # `story.no_face_chars`（旧 `pipeline.pulid.no_face_chars`）：声明为"没有可锁的脸"的角色
    # （面具/头盔/剪影）。它们不参与同框多脸计数 —— 拿一张无脸图喂 InsightFace
    # 要么检不出脸、要么检出个假的。由调用方从 config 传进来（本模块不读配置）。
    no_face_chars: frozenset[str] = frozenset()
    # 渲染引擎能力：每个画幅最多允许几张可锁的人脸。
    # None = 不限制（通用默认）；本地 PuLID 单参考图可配 1。
    max_faces_per_shot: int | None = None

    def __post_init__(self) -> None:
        self.cast_ids = {c.char_id for c in self.plan.characters}
        self.states = {c.char_id: {w.state_id for w in c.wardrobe_states}
                       for c in self.plan.characters}
        self.colors = {c.char_id: (c.color_signature or "").strip().lower()
                       for c in self.plan.characters}
        self.no_face_chars = frozenset(str(c) for c in (self.no_face_chars or ()))
        if self.max_faces_per_shot is not None:
            try:
                v = int(self.max_faces_per_shot)
                self.max_faces_per_shot = v if v >= 1 else None
            except (TypeError, ValueError):
                self.max_faces_per_shot = None

    @property
    def cast_hint(self) -> str:
        return (f"plan.characters 只有 {sorted(self.cast_ids)}；"
                "禁止动态新增角色（§10.1），要加人得先改 plan")

    @property
    def episode_target(self) -> float:
        return self.episode_sec if self.episode_sec is not None else self.plan.output.episode_sec

    @property
    def ledger_keys(self) -> tuple[str, ...]:
        """账本内层键的权威 = 本系列 `plan.continuity.mutable` 的实际取值（§7.4）。"""
        return tuple(self.plan.continuity.mutable) or LEDGER_FALLBACK_KEYS


def _is_off_timeline(scene: Scene, char: str) -> bool:
    """§7.3：闪回 / 幻象场里该角色的状态不计入账本时间线。"""
    return any(f in _OFF_TIMELINE_FLAGS for f in scene.character_flags.get(char, ()))


def _enabled_aspects(plan: Plan, script: Script | None, shot: Any | None) -> list[str]:
    """本镜**实际启用**的画幅（§6.2 就近覆盖）。

    v10.1 明确：判 `framing` 该有哪些构图，基准是 shot 级 `aspect_overrides`，
    不是 plan 全局 —— 否则"某镜只出竖屏"会被误判成缺构图。
    """
    out = [a.value for a in plan.output.aspects.enabled]
    overrides = [getattr(script, "aspects_override", None) if script else None,
                 getattr(shot, "aspect_overrides", None) if shot else None]
    for ov in overrides:
        if isinstance(ov, dict) and ov.get("enabled"):
            out = [str(a) for a in ov["enabled"]]
    return out


def _find_cycle(edges: dict[str, list[str]]) -> list[str] | None:
    """在 `occludes` 有向图里找环，返回环路径（首尾同点）；无环返回 None。

    schema 只抓得住二元互遮（A↔B）。三元环 A→B→C→A 里任意两人都不互遮，
    能完整过掉 schema，却在适配层递归展开遮挡链时直接爆栈。
    """
    WHITE, GREY, BLACK = 0, 1, 2
    color = {n: WHITE for n in edges}
    stack: list[str] = []

    def dfs(n: str) -> list[str] | None:
        color[n] = GREY
        stack.append(n)
        for m in edges.get(n, ()):
            if m not in color:      # 指向本画幅外的角色（schemas 已拦），别在这儿绕
                continue
            if color[m] == GREY:
                return stack[stack.index(m):] + [m]
            if color[m] == WHITE:
                found = dfs(m)
                if found:
                    return found
        color[n] = BLACK
        stack.pop()
        return None

    for n in list(edges):
        if color[n] == WHITE:
            found = dfs(n)
            if found:
                return found
    return None


def _shot_spans(board: Storyboard, scene_id: str) -> list[tuple[float, float, str]]:
    return [(s.start_sec, s.start_sec + s.duration_sec, s.shot_id)
            for s in board.shots
            if s.scene_id == scene_id and s.start_sec is not None]


def _shot_at(board: Storyboard | None, scene_id: str, at: float) -> str:
    """`at_sec` 落在本场哪个镜头里；没有 board 就返回空串。"""
    if board is None:
        return ""
    for lo, hi, sid in _shot_spans(board, scene_id):
        if lo - _EPS <= at < hi - _EPS:
            return sid
    return ""


def _effective_strategy(ctx: _Ctx, script: Script | None, shot: Any) -> str:
    """`multi_subject_strategy` 的键级合并（§4.2）：镜头 → script → plan。"""
    for layer in (getattr(shot, "requirements", None),
                  getattr(script, "requirements", None),
                  ctx.plan.requirements):
        v = getattr(layer, "multi_subject_strategy", None)
        if v is not None:
            return v.value if hasattr(v, "value") else str(v)
    return ""


# ==========================================================================
# 主入口
# ==========================================================================
def check_series(
    plan: Plan | dict[str, Any] | str | Path,
    scripts: Sequence[Script | dict[str, Any] | str | Path] = (),
    boards: Sequence[Storyboard | dict[str, Any] | str | Path] = (),
    *,
    tolerance: float = DEFAULT_TOLERANCE,
    speech_cps: float = DEFAULT_SPEECH_CPS,
    episode_sec: float | None = None,
    no_face_chars: Iterable[str] = (),
    max_faces_per_shot: int | None = None,
    fps: float = DEFAULT_FPS,
    max_atempo: float = DEFAULT_MAX_ATEMPO,
    strict: bool = False,
) -> Report:
    """跑一遍 §10.1 的跨对象 block 门。

    `scripts` / `boards` 按集传入（多集就是多个元素），内部按 `episode` 配对。
    `episode_sec` 不传则取 `plan.output.episode_sec`。
    `no_face_chars` 传 `story.no_face_chars`，只影响同框多脸的计数口径。
    `max_faces_per_shot` 是渲染引擎能力（None=不限制）；不是普适故事规则。
    """
    fs = _Findings()
    plan_m, scripts_m, boards_m = _harvest_schemas(fs, plan, list(scripts), list(boards))

    if plan_m is None:
        # 没 plan 就判不了任何引用完整性。但必须如实报出来，不能静默返回"通过"。
        fs.add("block", "ref.no_plan", "-",
               "没有可用的 plan.json，跨对象引用无法校验",
               "plan 是 char_id / state_id 的唯一权威，必须一起传")
        report = fs.report()
        return report.raise_if_blocked() if strict else report

    ctx = _Ctx(plan=plan_m, tolerance=tolerance, speech_cps=speech_cps,
               episode_sec=episode_sec, fps=fps, max_atempo=max_atempo,
               no_face_chars=frozenset(no_face_chars or ()),
               max_faces_per_shot=max_faces_per_shot)

    # 两边是**不同来源**在改：`no_face_chars` 在 config.json（人写一次就不动了），
    # 而 `char_id` 来自豆包生成的 plan —— 豆包换一套编号只是换几个字母，
    # 没有任何报错，配置里的条目就静默成了空操作。后果分两处：
    #   - 门这边：本该无脸的角色重新计入"同框几张脸"，多报（偏严，还看得见）；
    #   - 出图那边（employees.py 选 PuLID 脸源）：面具/剪影角色重新成为候选
    #     脸源，一旦被选中就是把整镜的脸锁到一张面具上 —— 这条是静默的。
    # 所以这里只 warn 不 block：条目对不上不一定是你写错，也可能是 plan 还没定稿。
    stale = sorted(ctx.no_face_chars - ctx.cast_ids)
    if stale:
        fs.add("warn", "config.no_face_char_unknown", "-",
               f"config 的 story.no_face_chars 写了 {stale}，"
               f"但 plan.characters 只有 {sorted(ctx.cast_ids)}",
               "对不上的条目**不会生效**：要么把 plan 里的 char_id 改回配置认得"
               "的名字，要么把配置里的陈旧条目删掉。若这些角色确实没有可锁的脸"
               "（面具/头盔/剪影），改名后必须同步更新配置")

    script_by_ep = {s.episode: s for s in scripts_m}
    board_by_ep = {b.episode: b for b in boards_m}
    # 同一集传了两份 → 下面的 dict 会静默留下一份，另一份**整集没校验**。
    # 这种"看起来查过了"的漏检比不查更危险，宁可 block。
    for kind, docs in (("script", scripts_m), ("storyboard", boards_m)):
        eps = [d.episode for d in docs]
        dup = sorted({e for e in eps if eps.count(e) > 1})
        if dup:
            fs.add("block", "dup.episode", f"ep{dup[0]}",
                   f"{kind} 里第 {dup} 集不止一份，只有一份会被校验",
                   "一集一份；多份通常是导出事故，先合并再进门")

    for ep in sorted(script_by_ep):
        script = script_by_ep[ep]
        board = board_by_ep.get(ep)
        for scene in script.scenes:
            _check_scene_refs(fs, ctx, script, scene)
            _check_cast_separation(fs, ctx, script, scene)
        _check_episode_total(fs, ctx, script)
        _check_timeline(fs, ctx, script, board)

    for ep in sorted(board_by_ep):
        board = board_by_ep[ep]
        script = script_by_ep.get(ep)
        if script is None:
            fs.add("block", "ref.no_script_for_board", f"ep{ep}",
                   "这一集只有 storyboard 没有 script，镜头挂不到场上",
                   "storyboard 的每一镜都必须属于 script 已定义的场")
            continue
        _check_board(fs, ctx, script, board)

    _check_ledger_chain(fs, ctx, scripts_m)

    report = fs.report()
    return report.raise_if_blocked() if strict else report


# --------------------------------------------------------------------------
# script 层：引用完整性 + 时长总量
# --------------------------------------------------------------------------
def _check_scene_refs(fs: _Findings, ctx: _Ctx, script: Script, scene: Scene) -> None:
    where = f"ep{script.episode}/{scene.scene_id}"

    refs: dict[str, str] = {}
    for c in scene.chars_on_screen:
        refs.setdefault(c, "chars_on_screen")
    for c in scene.wardrobe_state:
        refs.setdefault(c, "wardrobe_state")
    for c in scene.space_map:
        refs.setdefault(c, "space_map")
    for c in scene.character_flags:
        refs.setdefault(c, "character_flags")
    for ln in scene.lines:
        refs.setdefault(ln.speaker, "lines.speaker")
    for c, src in refs.items():
        if c not in ctx.cast_ids:
            fs.add("block", "ref.unknown_char", f"{where}/{src}",
                   f"角色 {c!r} 不在 plan.characters", ctx.cast_hint)

    # 旁白与对白分轨：explanation 固定 NARRATOR；inner 必须是本场角色。
    for i, n in enumerate(scene.narration):
        if n.kind == "inner":
            if n.speaker not in ctx.cast_ids:
                fs.add("block", "ref.unknown_char",
                       f"{where}/narration[{i}]",
                       f"内心独白 speaker {n.speaker!r} 不在 plan.characters",
                       ctx.cast_hint)
            elif scene.chars_on_screen and n.speaker not in scene.chars_on_screen:
                fs.add("block", "ref.narration_speaker_not_on_screen",
                       f"{where}/narration[{i}]",
                       f"内心独白 speaker {n.speaker} 不在本场 "
                       f"chars_on_screen={sorted(scene.chars_on_screen)}",
                       "内心独白要让观众看见说话的人；补进本场或改 exposition")

    # disguise 不带参 schema 已拦；指错人只有这一层拦得住
    for c, flags in scene.character_flags.items():
        for f in flags:
            if not str(f).startswith("disguise:"):
                continue
            target = str(f).split(":", 1)[1].strip()
            if target not in ctx.cast_ids:
                fs.add("block", "ref.disguise_unknown",
                       f"{where}/character_flags/{c}",
                       f"disguise 目标 {target!r} 不在 plan.characters", ctx.cast_hint)

    for c, sid in scene.wardrobe_state.items():
        if c in ctx.cast_ids and sid not in ctx.states[c]:
            fs.add("block", "ref.unknown_state", f"{where}/wardrobe_state/{c}",
                   f"{c} 没有服装态 {sid!r}", f"{c} 现有：{sorted(ctx.states[c])}")

    # 台词行里写的 speaker 也必须在本场 chars_on_screen 里（有人在场没台词要列出，
    # 反过来"有台词却不在场"是漏列，出图会漏人）
    allowed = set(scene.chars_on_screen)
    for i, ln in enumerate(scene.lines):
        if allowed and ln.speaker not in allowed:
            fs.add("block", "ref.speaker_not_on_screen", f"{where}/lines[{i}]",
                   f"说话人 {ln.speaker} 不在本场 chars_on_screen={sorted(allowed)}",
                   "把说话人补进 chars_on_screen（连同 space_map / wardrobe_state）")


def _check_cast_separation(fs: _Findings, ctx: _Ctx, script: Script,
                           scene: Scene) -> None:
    """同框角色的**服装主色**不能撞车（§5 cast_separation 的轴之一）。

    为什么这一条落在这里而不是图像层：`Character.color_signature` 的 schema 说明是
    "角色主色，**如 #1B3A5C**" —— 它是给**同框区分度**用的色号，是文本层的判据。
    2026-10-13 之前它没有任何消费端；期间曾被短暂接到首帧提示词上
    （`"costume color #1B3A5C"`），那是错的：往扩散模型提示词里灌十六进制没有意义，
    已移除。区分度该在的地方是这里 —— 两人穿同色，观众在小屏上就分不清谁是谁，
    而这在出图之前就能查出来。

    只报 `warn` 不报 `block`：主色相近未必是错（同门弟子就该穿一样的），
    但值得写剧本的人看一眼。
    """
    where = f"ep{script.episode}/{scene.scene_id}"
    on_screen = [c for c in scene.chars_on_screen if c in ctx.colors]
    for i, a in enumerate(on_screen):
        for b in on_screen[i + 1:]:
            ca, cb = ctx.colors.get(a, ""), ctx.colors.get(b, "")
            if not ca or not cb:
                continue
            if ca == cb:
                fs.add("warn", "cast.same_color", f"{where}/chars_on_screen",
                       f"{a} 与 {b} 的 color_signature 都是 {ca!r}",
                       "同框两人服装主色相同，小屏上难分辨；"
                       "改色号或在 plan.cast_separation.violations 里说明这是有意的")


def _check_episode_total(fs: _Findings, ctx: _Ctx, script: Script) -> None:
    """场景之和 == 单集目标时长（容差内）。不需要 board 就能判。"""
    total = script.total_duration
    target = ctx.episode_target
    if abs(total - target) > ctx.tolerance + _EPS:
        fs.add("block", "time.episode_sum_mismatch", f"ep{script.episode}",
               f"本集场景时长合计 {total:.3f}s ≠ 单集目标 {target:.3f}s"
               f"（容差 ±{ctx.tolerance}s）",
               "改场景时长，或把 plan.output.episode_sec 调成真实目标时长")


# --------------------------------------------------------------------------
# 时间轴（§7.1）
# --------------------------------------------------------------------------
def _check_timeline(fs: _Findings, ctx: _Ctx, script: Script,
                    board: Storyboard | None) -> None:
    for scene in script.scenes:
        where = f"ep{script.episode}/{scene.scene_id}"
        # P0-S2：台词容量校验统一按标准语速（board 生成镜头时长即按标准 cps），
        # 场景 speech_cps 情绪覆盖值只作渲染端语速参考，不参与"装不装得下"判定。
        cps = ctx.speech_cps

        def dur_of(ln: Any) -> float:
            if ln.timing_sec is not None:
                return ln.timing_sec
            return math.ceil(len(ln.text) / cps) if cps > 0 else 0.0

        if cps <= 0:
            # 只可能来自调用方传错参数（schema 已保证 speech_cps > 0）
            fs.add("block", "time.bad_cps", where,
                   f"本场语速 cps={cps} 非法", "语速必须为正数")
            continue
        for i, ln in enumerate(scene.lines):
            at = f"{where}/lines[{i}]"
            need = math.ceil(len(ln.text) / cps)
            if ln.timing_sec is not None and need > ln.timing_sec + _EPS:
                fs.add("block", "time.line_too_long", at,
                       f"{ln.speaker} 的台词 {len(ln.text)} 字，按 {cps} 字/秒需要 "
                       f"{need}s，但 timing_sec={ln.timing_sec}s",
                       f"砍字，或把 timing_sec 加到 ≥ {need}s"
                       "（别靠调大 cps 蒙混——TTS 不会因此读得更快）")
            if ln.at_sec is None:
                continue
            spans = _shot_spans(board, scene.scene_id) if board else []
            if spans and not any(lo - _EPS <= ln.at_sec < hi - _EPS
                                 for lo, hi, _ in spans):
                fs.add("block", "time.at_sec_out_of_shot", at,
                       f"{ln.speaker} 的 at_sec={ln.at_sec}s 不落在本场任何镜头区间内"
                       f"（本场镜头：{[(sid, f'{lo}~{hi}') for lo, hi, sid in spans]}）",
                       "把 at_sec 钉到说话人所在镜头的 start_sec + 0.5~1.0")

        # 同一角色**同一镜内**多句不得重叠；没 board 时退化为按场分组
        groups: dict[tuple[str, str], list[Any]] = {}
        for ln in scene.lines:
            if ln.at_sec is None:
                continue
            shot_id = _shot_at(board, scene.scene_id, ln.at_sec)
            groups.setdefault((ln.speaker, shot_id), []).append(ln)
        for (speaker, shot_id), lines in groups.items():
            lines.sort(key=lambda x: x.at_sec or 0.0)
            for a, b in zip(lines, lines[1:]):
                end = (a.at_sec or 0.0) + dur_of(a)
                if end > (b.at_sec or 0.0) + _EPS:
                    fs.add("block", "time.at_sec_overlap",
                           f"{where}/{shot_id or '本场'}",
                           f"{speaker} 的两句台词在时间轴上重叠："
                           f"{a.at_sec}s(+{dur_of(a)}s) 压住了 {b.at_sec}s",
                           "把后一句的 at_sec 推到前一句结束之后，或砍前一句的字")

        # 旁白是叠轨：不占镜头时长，但必须落在本场镜头内，且不能被台词挤爆。
        spans = _shot_spans(board, scene.scene_id) if board else []
        for i, n in enumerate(scene.narration):
            at = f"{where}/narration[{i}]"
            need = math.ceil(len(n.text) / _NARRATION_CPS)
            if n.timing_sec is not None and need > n.timing_sec + _EPS:
                fs.add("block", "time.narration_too_long", at,
                       f"旁白 {len(n.text)} 字按 {_NARRATION_CPS:g} 字/秒需要 "
                       f"{need}s，但 timing_sec={n.timing_sec}s",
                       f"砍字，或把 timing_sec 加到 ≥ {need}s")
            if n.at_sec is None:
                continue
            if spans and not any(lo - _EPS <= n.at_sec < hi - _EPS
                                 for lo, hi, _ in spans):
                fs.add("block", "time.narration_at_sec_out_of_shot", at,
                       f"旁白 at_sec={n.at_sec}s 不落在本场任何镜头区间内",
                       "把旁白钉到承载它的镜头 start_sec 之后")

        nats = [n for n in scene.narration if n.at_sec is not None]
        nats.sort(key=lambda x: x.at_sec or 0.0)
        for a, b in zip(nats, nats[1:]):
            end = (a.at_sec or 0.0) + (
                a.timing_sec or math.ceil(len(a.text) / _NARRATION_CPS))
            if end > (b.at_sec or 0.0) + _EPS:
                fs.add("warn", "time.narration_overlap", where,
                       f"两条旁白在时间轴上重叠：{a.at_sec}s(+{end - (a.at_sec or 0):.1f}s) "
                       f"压住了 {b.at_sec}s",
                       "同一时间只保留一条人声旁白，否则口播会糊")


# --------------------------------------------------------------------------
# storyboard 层
# --------------------------------------------------------------------------
def _check_shot_narration(fs: _Findings, ctx: _Ctx, script: Script,
                          shot: Any, scene: Any | None, where: str,
                          in_shot: set[str], board: Storyboard) -> None:
    """v10.3 旁白时间契约（storyboard.shots[].narration 是唯一权威）。

    - 说话人：inner 必须在镜内（warn，既有规则）；
    - timing_sec ≥ ceil(汉字数/5)（block）；
    - shot_id 指向真实镜头、at_sec 落在锚镜窗口内（block）；
    - within_shot 必须完整落在锚镜内（留 0.08s，block）；
    - 与同场台词、与其他旁白至少留 0.08s 空隙（block）。
    """
    for i, n in enumerate(shot.narration):
        at = f"{where}/narration[{i}]"
        if n.kind == "inner" and n.speaker not in in_shot:
            fs.add("warn", "narration.speaker_not_in_shot", at,
                   f"内心独白 {n.speaker} 不在本镜画面里",
                   "把该角色放进本镜反应/中景，或改成 exposition")
        need = math.ceil(len(n.text) / _NARRATION_CPS)
        if n.timing_sec is not None and need > n.timing_sec + _EPS:
            fs.add("block", "time.narration_too_long", at,
                   f"旁白 {len(n.text)} 字按 {_NARRATION_CPS:g} 字/秒需要 "
                   f"{need}s，但 timing_sec={n.timing_sec}s",
                   f"砍字，或把 timing_sec 加到 ≥ {need}s")
        if n.at_sec is None:
            continue
        # 锚镜：优先按 shot_id 查找，缺省回退到承载镜本身
        anchor = None
        if n.shot_id:
            anchor = next((s for s in board.shots
                           if s.shot_id == n.shot_id), None)
        else:
            anchor = shot
        if anchor is None:
            fs.add("block", "ref.narration_unknown_shot", at,
                   f"shot_id={n.shot_id!r} 在 storyboard 里不存在",
                   "旁白只能锚定真实存在的镜头")
            continue
        a_start, a_end = anchor.start_sec, anchor.end_sec
        if not (a_start - _EPS <= n.at_sec < a_end - _EPS):
            fs.add("block", "time.narration_at_sec_out_of_shot", at,
                   f"旁白 at_sec={n.at_sec}s 不落在锚镜 "
                   f"{anchor.shot_id} [{a_start:.2f}~{a_end:.2f}) 内",
                   "把 at_sec 钉到承载镜头 start_sec 之后、end_sec 之前")
            continue
        n_end = n.at_sec + (n.timing_sec or need)
        if n.anchor_mode == "within_shot" \
                and n_end > a_end - 0.08 + _EPS:
            fs.add("block", "time.narration_exceeds_shot", at,
                   f"within_shot 旁白到 {n_end:.2f}s，超出锚镜 "
                   f"{anchor.shot_id} 结束 {a_end - 0.08:.2f}s（须留 0.08s）",
                   "缩短/拆分旁白，或改为 voiceover_span 并声明 allow_span_shots")
        # 与同场台词的空隙：旁白 [at, n_end] vs 台词 [at_sec, at_sec+dur]
        if scene is not None:
            for ln in scene.lines:
                if ln.at_sec is None:
                    continue
                l_start = ln.at_sec
                l_end = l_start + (ln.timing_sec or
                                   math.ceil(len(ln.text) / _NARRATION_CPS))
                lo = max(n.at_sec, l_start)
                hi = min(n_end, l_end)
                if hi > lo + _EPS or \
                        (min(n_end, l_start) - max(n.at_sec, l_end) < 0.08
                         and n_end > l_start and n.at_sec < l_end):
                    fs.add("block", "time.narration_overlap_dialogue", at,
                           f"旁白 [{n.at_sec:.2f}~{n_end:.2f}) 与台词 "
                           f"[{l_start:.2f}~{l_end:.2f}) 重叠或间距 <0.08s",
                           "把旁白移到台词空隙，或缩短/拆分旁白")

    # 本镜内相邻旁白空隙
    nats = [n for n in shot.narration if n.at_sec is not None]
    nats.sort(key=lambda x: x.at_sec or 0.0)
    for a, b in zip(nats, nats[1:]):
        a_end = (a.at_sec or 0.0) + (
            a.timing_sec or math.ceil(len(a.text) / _NARRATION_CPS))
        if a_end > (b.at_sec or 0.0) + 0.08 - _EPS:
            fs.add("block", "time.narration_overlap", where,
                   f"同镜两条旁白重叠或间距 <0.08s：{a.at_sec}s "
                   f"(+{a_end - (a.at_sec or 0):.1f}s) 压住 {b.at_sec}s",
                   "同一时间只保留一条人声旁白")


def _check_board(fs: _Findings, ctx: _Ctx, script: Script, board: Storyboard) -> None:
    ep = board.episode
    by_scene = {sc.scene_id: sc for sc in script.scenes}
    scene_shots: dict[str, list[Any]] = {}
    for shot in board.shots:
        scene_shots.setdefault(shot.scene_id, []).append(shot)

    for shot in board.shots:
        scene = by_scene.get(shot.scene_id)
        where = (f"ep{ep}/{scene.scene_id}/{shot.shot_id}" if scene
                 else f"ep{ep}/{shot.shot_id}")
        if scene is None:
            fs.add("block", "ref.unknown_scene", where,
                   f"shot.scene_id={shot.scene_id!r} 在该集 script 里没有对应场景",
                   "分镜只能挂到 script 已定义的场；要加场得先改 script")
        _check_shot_refs(fs, ctx, scene, shot, where)
        _check_shot_wardrobe(fs, ctx, scene, shot, where)
        # P0-S2：台词容量校验统一按标准语速（同 _check_timeline），情绪 cps 仅渲染参考。
        cps = ctx.speech_cps
        _check_shot_dialogue(fs, ctx, shot, where, cps)
        in_shot = ({str(c) for c in (shot.ref_characters or [])}
                   | {str(s.char) for s in (shot.subjects or [])})
        _check_shot_narration(fs, ctx, script, shot, scene, where, in_shot,
                              board)
        _check_framing(fs, ctx, script, shot, where)
        _check_prompt_shot_size(fs, shot, where)

    _shot_size_mix(fs, board)

    for scene_id, shots in scene_shots.items():
        scene = by_scene.get(scene_id)
        if scene is None:
            continue
        # 本场一个镜头都没有 → 交给 _normalize_shots 兜底造一个，不算 block
        # （契约 §9.2 明确允许这种兜底；在这儿拦会与流水线既有行为打架）
        total = sum(s.duration_sec for s in shots)
        if abs(total - scene.duration_sec) > ctx.tolerance + _EPS:
            fs.add("block", "time.scene_sum_mismatch", f"ep{ep}/{scene_id}",
                   f"该场镜头时长合计 {total:.3f}s ≠ 场景 duration_sec "
                   f"{scene.duration_sec:.3f}s（容差 ±{ctx.tolerance}s）",
                   "改镜头时长或改场景时长，让两者对齐")
        # 场间转场：script 声明了 transition_in，分镜首镜必须带走。
        if scene.transition_in is not None:
            first_tr = next((s.transition_in for s in shots if s.transition_in), None)
            if first_tr is None:
                fs.add("warn", "transition.missing_in_board", f"ep{ep}/{scene_id}",
                       f"script 声明了 {scene.transition_in.type} 转场，"
                       "但本场分镜没有 transition_in",
                       "把转场锚点落到本场首镜的 transition_in")
            elif first_tr.type in {"match_cut", "sound_bridge", "action_bridge"} \
                    and not first_tr.anchor.strip():
                fs.add("warn", "transition.missing_anchor", f"ep{ep}/{scene_id}",
                       f"{first_tr.type} 缺 anchor（共享物件/动作/声音）",
                       "补一个上一场与本场都真实存在的锚点")


# README 第 3 段规则 2 要求"景别以特写/大特写/近景为主，中景辅助，少用全景，不用远景"。
# 这条规则此前**没有任何机器校验** —— 于是出现过分镜里 wide/full 占 5/13、
# 特写只有 1 个，却一路绿灯到出图的情况（2026-10-14 实测 input/storyboard.json）。
# 竖屏小屏上远景里的人物只有几十个像素高，脸锁不住、表情看不见，
# 等于把"保脸"这条主线目标让掉 —— 而这是**文本层就能查出来**的事。
_SHOT_SIZE_WIDE: frozenset[str] = frozenset({"wide", "extreme_wide"})  # README：不用
_SHOT_SIZE_BROAD: frozenset[str] = _SHOT_SIZE_WIDE | {"full"}          # 再加全景：少用
# 远景+全景占比超过这个比例就报。1/4 而不是 1/3：README 的口径是"不用远景、
# 少用全景"，1/3 太松 —— 实测那份违规分镜正好是 38%，会被 1/3 放过。
_BROAD_SHOT_RATIO_LIMIT = 0.25


def _representative_size(vals: list[str]) -> str:
    """一个镜有多个画幅时，取哪个景别代表这一镜。

    取**最宽**的那个：宽景才是风险（脸小到锁不住），窄的那版不构成风险。
    两个画幅都不是宽景时取第一个 —— 此时取谁都不影响"算不算宽景"的结论。
    """
    for v in vals:
        if v in _SHOT_SIZE_BROAD:
            return v
    return vals[0]


def _shot_size_mix(fs: _Findings, board: Storyboard) -> None:
    """整集景别分布体检 —— 只 warn，不 block。

    为什么只 warn：远景/全景本身不是错（开场交代环境、结尾定格都要用），
    错的是**占比失控**。真要 block，就得先有一条经得起争辩的量化契约，
    现在没有，硬拦只会逼人写豁免。

    **按镜计，不按画幅计**：一个镜在 9:16 和 16:9 各有一份 framing，按画幅算
    同一镜会被数两遍，占比也就被画幅数稀释了（多一个派生画幅 = 分母凭空变大）。
    "这一镜是不是远景"是**镜头设计**的事实，跟出几个画幅无关。
    同一镜两个画幅景别不一致时，只要**任一**画幅是宽景就算 —— 宽的那版才是风险。
    """
    if not board.shots:
        return
    per_shot: list[str] = []
    for s in board.shots:
        vals = [fr.shot_size.value for fr in s.framing.values()
                if getattr(fr, "shot_size", None) is not None]
        if not vals:
            continue
        per_shot.append(_representative_size(vals))
    if not per_shot:
        return
    wide = [s for s in per_shot if s in _SHOT_SIZE_WIDE]
    broad = [s for s in per_shot if s in _SHOT_SIZE_BROAD]
    if len(broad) / len(per_shot) <= _BROAD_SHOT_RATIO_LIMIT:
        return
    from collections import Counter
    dist = "、".join(f"{k}×{v}" for k, v in Counter(per_shot).most_common())
    fs.add("warn", "style.wide_shot_discouraged", f"ep{board.episode}",
           f"全景+远景 {len(broad)}/{len(per_shot)} 镜（{len(broad)/len(per_shot):.0%}，"
           f"其中远景 {len(wide)} 镜）。景别分布：{dist}",
           "竖屏小屏上远景里的人物只有几十像素高，脸锁不住也看不见表情 —— "
           "特写/大特写/近景应是主力（README 第 3 段规则 2：不用远景、少用全景）。"
           "把部分镜头改近；确实需要交代环境时，用「局部特写 + 声音」代替大全景")


def _check_shot_refs(fs: _Findings, ctx: _Ctx, scene: Scene | None, shot: Any,
                     where: str) -> None:
    for c in shot.ref_characters:
        if c not in ctx.cast_ids:
            fs.add("block", "ref.unknown_char", f"{where}/ref_characters",
                   f"角色 {c!r} 不在 plan.characters", ctx.cast_hint)
    for c in {s.char for s in shot.subjects}:
        if c not in ctx.cast_ids:
            fs.add("block", "ref.unknown_char", f"{where}/subjects",
                   f"角色 {c!r} 不在 plan.characters", ctx.cast_hint)
    for aspect, fr in shot.framing.items():
        for sub in fr.subjects:
            if sub.char not in ctx.cast_ids:
                fs.add("block", "ref.unknown_char",
                       f"{where}/framing/{aspect}/subjects",
                       f"角色 {sub.char!r} 不在 plan.characters", ctx.cast_hint)

    if scene is None:
        return
    # subjects / ref_characters 越出本场 chars_on_screen。
    # 后果比"出图漏人"重：cast_separation 的同框图正是按 chars_on_screen 建的，
    # 分镜越界加人 = 让这一对角色逃过区分度校验，等于绕过防串脸第一道防线。
    #
    # 例外：POV 镜的场景属于**观察者**（如 SH44 玄烈在楼顶看谷底），画面里是
    # 被观察地点的角色。要求他们出现在观察者场景的 chars_on_screen 里既不合
    # 事实，又会把 C1/C3 塞进 S08 导致其余楼顶镜被自动回填。POV 的同框校验
    # 仍按本镜 subjects 走，只是不要求与观察者同场。
    allowed = set(scene.chars_on_screen)
    is_pov = str(getattr(getattr(shot, "hints", None), "coverage", "") or "") == "pov"
    stray = sorted((set(shot.ref_characters) | {s.char for s in shot.subjects}) - allowed)
    if stray and is_pov:
        fs.add("info", "ref.pov_subjects_external_scene", where,
               f"POV 镜引用本场外角色 {stray}（观察者场景 {scene.scene_id}）",
               "无需处理；POV 允许被观察角色不在观察者场景的 chars_on_screen 里")
    elif stray:
        fs.add("block", "ref.subjects_out_of_scene", where,
               f"{stray} 出现在本镜，但不在 {scene.scene_id} 的 "
               f"chars_on_screen={sorted(allowed)} 里",
               "要么把他们补进该场 chars_on_screen（并补 space_map/wardrobe_state），"
               "要么从本镜删掉")
    # framing 里的额外角色没被 §10.1 明文列出，但走的是同一个绕过口子 —— 先只告警，
    # 等契约明确后再决定是否升 block
    fstray = sorted({s.char for fr in shot.framing.values() for s in fr.subjects
                     if not s.offscreen} - allowed)
    if fstray and not is_pov:
        fs.add("warn", "ref.framing_subject_out_of_scene", where,
               f"framing 里多出 {fstray}，不在 {scene.scene_id} 的 chars_on_screen",
               "确认是本人出镜（该补进 chars_on_screen）还是画外角色（该标 offscreen）")


def _check_shot_dialogue(fs: _Findings, ctx: _Ctx, shot: Any, where: str,
                         cps: float | None = None) -> None:
    """画内台词必须：说话人在本镜、且声明了口型。

    旧数据里 SH21 写着"同期（C1，画内）"却没声明 `dubbing=lipsync`，
    下游就真的不做口型 —— 观众看到的是"说话不张嘴"。这条检查把
    "分镜文本"和"能力声明"绑死，避免再靠人记得同步两处。
    """
    audio = str(getattr(shot, "audio", "") or "")
    if not audio:
        return
    on_screen = set(re.findall(r"[（(](C\d+)[，,]画内", audio))
    on_screen |= set(re.findall(r"同期[（(](C\d+)[）)]", audio))
    in_shot = {str(getattr(c, "char", "")) for c in (getattr(shot, "subjects", None) or [])}
    in_shot |= {str(c) for c in (getattr(shot, "ref_characters", None) or [])}
    missing = sorted(on_screen - in_shot)
    if missing:
        fs.add("block", "dialogue.speaker_not_in_shot", where,
               f"画内台词说话人 {missing} 不在本镜 ref_characters/subjects={sorted(in_shot)}",
               "把说话人放进本镜（近景/中景），或改成画外音并配听者反应镜")
    eff = effective_requirements(
        shot=getattr(shot, "requirements", None),
        plan=ctx.plan.requirements,
    )
    dubbing = str(eff.get("dubbing", ("none", ""))[0])
    if on_screen and dubbing != "lipsync":
        fs.add("block", "dialogue.lipsync_not_declared", where,
               f"本镜有画内台词（{sorted(on_screen)}），但生效 dubbing={dubbing!r}",
               "给该镜 requirements.dubbing 写 lipsync；否则嘴不会动")
    if on_screen and len(on_screen) > 1:
        fs.add("warn", "dialogue.multiple_speakers", where,
               f"同一镜有多个画内说话人 {sorted(on_screen)}",
               "按你的口型引擎能力决定是否拆成过肩/正反打"
               "（单参考图引擎通常只能锁一张脸；多说话人引擎可忽略）")
    if not on_screen and dubbing == "lipsync":
        fs.add("warn", "dialogue.lipsync_without_line", where,
               "声明了 lipsync，但本镜 audio 里没有画内台词",
               "确认是否需要口型；画外音不要开 lipsync")

    # v10.2①：画内台词镜必须声明开口动作。LatentSync 只能放大已有的嘴部动作，
    # 不能凭空造 —— 2.3 原片嘴部信号 std 0.020（几乎闭嘴）时，LS 后相关只有
    # 0.464–0.493；带开口动作的 SH02 能到 0.659。旧分镜没有 `speaking` 字段
    # （None），跳过不报，避免历史数据刷屏。
    declared = [c for c in (getattr(shot, "subjects", None) or [])
                if getattr(c, "speaking", None) is not None]
    if on_screen and declared:
        talkers = {str(c.char) for c in declared if c.speaking}
        if not (talkers & on_screen):
            fs.add("block", "dialogue.no_speaking_subject", where,
                   f"画内台词说话人 {sorted(on_screen)}，但 subjects 里没有 "
                   f"speaking=true（当前：{sorted(talkers) or '无'}）",
                   "给说话人 subjects[].speaking=true，并在 prompt/motion 里写开口动作"
                   "（如 lips parting as he speaks）；否则口型只能靠 LS 硬补")

    # v10.2②：台词段长必须装得下 TTS。`snap_frames(8n+1)` 会把时长向下取整，
    # 2026-10-22 实测 SH45：41 帧片段对 43 帧音频，相关 0.354；补到 43 帧 0.464。
    # 判定分两级：连 atempo=max_atempo 都塞不下 → block；正常语速塞不下 → warn
    # （配音阶段会加速，能过但不是设计意图）。
    cps = cps or ctx.speech_cps
    for _char, text in _onscreen_texts(audio):
        est = len(text) / max(cps, 0.1)
        avail = _snap_frames_8n1((getattr(shot, "duration_sec", 0.0) or 0.0) * ctx.fps)
        need = math.ceil(est * ctx.fps) + _DIALOGUE_FRAME_MARGIN
        hard = math.ceil(est / max(ctx.max_atempo, 1.0) * ctx.fps) + _DIALOGUE_FRAME_MARGIN
        if need <= avail:
            continue
        level = "block" if hard > avail else "warn"
        fs.add(level, "time.dialogue_exceeds_shot", where,
               f"台词「{text[:14]}…」按 {cps} 字/秒需 {need} 帧，"
               f"本镜 {getattr(shot, 'duration_sec', 0)}s 取整后只有 {avail} 帧"
               f"（8n+1；atempo≤{ctx.max_atempo} 时最低 {hard} 帧）",
               "把本镜加长到能装下台词，或拆句/精简台词；不要指望 LatentSync 补时长")


_UTTERANCE_RE = re.compile(
    r"(?:同期|画外)[（(](C\d+)[，,](画内|无口型)[）)][：:](.*?)"
    r"(?=(?:[；;])?(?:同期|画外)[（(]|$)"
)


def _onscreen_texts(audio: str) -> list[tuple[str, str]]:
    """从 `shot.audio` 里取出画内台词 (char, text)。

    规范写法：`同期（C1，画内）：台词；画外（C2，无口型）：台词`。
    按"下一句的发话标签"切分，而不是按 `；` 切 —— 台词正文自己带分号时
    也不会把一句话切成两半。
    """
    out: list[tuple[str, str]] = []
    for m in _UTTERANCE_RE.finditer(audio or ""):
        if m.group(2) != "画内":
            continue
        text = m.group(3).strip().rstrip("；;")
        if text:
            out.append((m.group(1), text))
    return out


def _snap_frames_8n1(n: float) -> int:
    """LTX 的帧数取整：8n+1，且向下取整（不能超本镜时长）。"""
    i = int(n)
    if i < 1:
        return 0
    return (i - 1) // 8 * 8 + 1


# v10.2③：prompt 里的景别词必须与 framing.shot_size 一致。
# 2026-10-22 实测：首帧是中景、prompt 写 "close-up …"，2.3 的 condition_only
# 不肯推近，直接镜内切（SH02 峰值帧差 36）；只删 "close-up" 无效（35.7），
# 整段改写成首帧景别才消（12.5）。这条是文本层就能查的，不该留到出片后。
# 匹配顺序必须先长后短，否则 "medium close-up" 会先命中 "medium"。
_SIZE_TOKENS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("extreme_close_up", re.compile(r"extreme[-\s]?close[-\s]?up")),
    ("medium_close", re.compile(r"medium[-\s]?close(?:[-\s]?up)?")),
    ("close_up", re.compile(r"close[-\s]?up")),
    ("extreme_wide", re.compile(r"extreme[-\s]?wide(?:[-\s]?shot)?")),
    # 只认 "wide shot"：prompt 里 "wide-angle lens" 是镜头参数，不是景别。
    ("wide", re.compile(r"wide[-\s]?shot")),
    ("full", re.compile(r"full[-\s]?(?:shot|body|figure)")),
    ("medium", re.compile(r"medium[-\s]?shot")),
)


def _sizes_in_prompt(prompt: str) -> set[str]:
    """抽出 prompt 里声明的景别。长词先吃，避免 "medium close-up" 同时命中 medium。"""
    p = (prompt or "").lower()
    found: set[str] = set()
    for name, rx in _SIZE_TOKENS:
        if rx.search(p):
            found.add(name)
            p = rx.sub(" ", p)      # 吃掉已匹配片段，短词不再重复命中
    return found


# 景别阶梯：用来量"隔了几档"。隔 1 档是同一机位的自然松紧（中景↔中近景），
# 隔 2 档以上才是 2.3 会硬切的量级（SH02：medium 首帧 + close-up 提示词）。
_SIZE_LADDER = ("extreme_wide", "wide", "full", "medium",
                "medium_close", "close_up", "extreme_close_up")


def _size_gap(a: str, b: str) -> int:
    try:
        return abs(_SIZE_LADDER.index(a) - _SIZE_LADDER.index(b))
    except ValueError:
        return 99


def _check_prompt_shot_size(fs: _Findings, shot: Any, where: str) -> None:
    """prompt 的景别语义 vs framing.shot_size。

    * 相隔 ≥2 档 → block（2.3 必切；SH02 实测）
    * 相隔 1 档 → warn（中景/中近景这种同机位松紧，通常能忍）
    * 命中最少两种景别 → warn（提示词在描述两套景别，等于要求镜内切）
    * 物件镜/环境镜同样检查景别，但不涉及人脸占比口径
    """
    sizes = _sizes_in_prompt(getattr(shot, "prompt", "") or "")
    if not sizes:
        return
    declared = {fr.shot_size.value for fr in (getattr(shot, "framing", None) or {}).values()
                if getattr(fr, "shot_size", None) is not None}
    if not declared:
        return
    if sizes & declared:
        if len(sizes) > 1:
            fs.add("warn", "style.prompt_multi_shot_size", where,
                   f"prompt 同时写了 {sorted(sizes)}，本镜声明 {sorted(declared)}",
                   "一镜只演一个景别；要换景别请拆镜或拆 segments")
        return
    if len(sizes) > 1:
        # 已经命中多景别，上面那条 warning 就够了，不重复升级成 block
        fs.add("warn", "style.prompt_multi_shot_size", where,
               f"prompt 同时写了 {sorted(sizes)}，本镜声明 {sorted(declared)}",
               "一镜只演一个景别；要换景别请拆镜或拆 segments")
        return
    gap = min(_size_gap(a, b) for a in sizes for b in declared)
    if gap <= 1:
        fs.add("warn", "style.prompt_size_near_miss", where,
               f"prompt 写 {sorted(sizes)}、framing 是 {sorted(declared)}（差 {gap} 档）",
               "尽量让提示词与首帧完全同景别，避免 2.3 小幅推拉")
        return
    fs.add("block", "style.prompt_size_mismatch", where,
           f"prompt 的景别语义是 {sorted(sizes)}，但 framing 声明 {sorted(declared)}"
           f"（差 {gap} 档）",
           "2.3 的 condition_only 不会推近/拉远，只会镜内硬切。"
           "改 prompt 的景别描述使其与首帧一致，或重出首帧为声明的景别")


def _check_shot_wardrobe(fs: _Findings, ctx: _Ctx, scene: Scene | None, shot: Any,
                         where: str) -> None:
    for sub in shot.subjects:
        sid = sub.wardrobe_state
        if not sid:
            continue
        if sub.char in ctx.cast_ids and sid not in ctx.states[sub.char]:
            fs.add("block", "ref.unknown_state", f"{where}/subjects/{sub.char}",
                   f"{sub.char} 没有服装态 {sid!r}",
                   f"现有：{sorted(ctx.states[sub.char])}")
        if scene is None:
            continue
        want = scene.wardrobe_state.get(sub.char)
        if want and want != sid:
            fs.add("block", "ref.wardrobe_mismatch", f"{where}/subjects/{sub.char}",
                   f"本镜写 {sid!r}，但 {scene.scene_id} 的 script 写 {want!r}",
                   "以 script 同场 wardrobe_state 为准：换装是场级事实，不是镜级")


def _check_framing(fs: _Findings, ctx: _Ctx, script: Script, shot: Any,
                   where: str) -> None:
    enabled = _enabled_aspects(ctx.plan, script, shot)
    missing = [a for a in enabled if a not in shot.framing]
    if missing:
        fs.add("block", "struct.framing_missing_aspect", where,
               f"本镜启用画幅 {enabled}，但 framing 缺 {missing}",
               "每个启用画幅都要一套独立构图 —— 竖屏不是横屏切一刀")
    strategy = _effective_strategy(ctx, script, shot)
    if len({s.char for s in shot.subjects}) >= 2 and strategy in {"", "auto"}:
        fs.add("block", "struct.multi_subject_undeclared", where,
               "同框 ≥2 人，但 multi_subject_strategy 生效值是 "
               f"{strategy or '未声明'!r}（§4.4 禁止 auto/推导兜底）",
               "在 shot 级显式写 joint / per_subject / hybrid"
               "（plan 级 auto 只作为单人镜的默认值）")
    _check_multi_face(fs, ctx, shot, enabled, where)
    for aspect, fr in shot.framing.items():
        _check_occlusion(fs, fr, f"{where}/framing/{aspect}")


def _hint_truthy(obj: Any, key: str) -> bool:
    """读 C 层 `hints` 里的逃生舱开关（hints 不持久化，`extra="allow"`）。"""
    hints = getattr(obj, "hints", None)
    if hints is None:
        return False
    raw = hints.model_dump() if hasattr(hints, "model_dump") else dict(hints or {})
    return bool(raw.get(key))


def _check_multi_face(fs: _Findings, ctx: _Ctx, shot: Any,
                      enabled: list[str], where: str) -> None:
    """同框人脸数超过**当前配置的引擎能力**时 block。

    这是能力门，不是普适故事规则：
    * `max_faces_per_shot = None`（默认）→ 完全不检查，通用引擎/未来多人引擎都能用；
    * 设为 1 → 只允许单张可锁脸（本地 PuLID 单参考图）；
    * 设为 N → 允许 N 张，超过才报。

    需要挡在文本层的理由：单参考图引擎对第二个角色没有任何锁脸机制，只剩
    IPAdapter 的服装参考 + `desc_en` 文字描述 ——
    出来的是一张"看着像但不是同一人"的脸。体检把 M1 保脸判为不合格，一半原因在这。

    为什么不再校验"有没有声明 `multi_subject_strategy`"：那个四值枚举（auto /
    joint / per_subject / hybrid）**渲染侧零消费** —— 声明了也无人执行。上面那条
    `struct.multi_subject_undeclared` 校验的是"你声明了没有"，本条校验的是
    "这个构图能不能做"。两者不重复，但只有本条能真正保住脸。

    逃生舱：`shot.hints.allow_multi_face = true`（C 层，不持久化）。留给
    "两人全是背影/剪影，压根不需要锁脸"这类确实安全的构图，以及将来引擎支持
    分区条件之后的自证豁免。
    """
    if ctx.max_faces_per_shot is None:
        return                       # 通用默认：不限制，交给下游引擎自己决定
    if _hint_truthy(shot, "allow_multi_face"):
        return
    limit = ctx.max_faces_per_shot
    for aspect in enabled:
        fr = shot.framing.get(aspect)
        if fr is None:            # 缺画幅由 struct.framing_missing_aspect 报，这里不重复
            continue
        faces = sorted({s.char for s in fr.subjects
                        if not s.offscreen
                        and (s.facing is None or s.facing.value not in _FACE_HIDDEN_FACING)
                        and s.char not in ctx.no_face_chars})
        if len(faces) > limit:
            fs.add("block", "cast.multi_face_unsupported", f"{where}/framing/{aspect}",
                   f"{aspect} 画幅内同框 {len(faces)} 张正脸 {faces}，"
                   f"超过当前配置上限 {limit} 张/画幅 "
                   "（story.max_faces_per_shot）",
                   "改构图，让每个画幅的人脸数不超过该上限：单人反打 / 过肩 / 背影 / "
                   "远景剪影 / 局部特写 / 前景遮挡；"
                   "确实安全（全员背影或剪影）时在该镜 hints 写 "
                   '{"allow_multi_face": true} 显式豁免；'
                   "若你的渲染引擎支持多张参考脸，把 max_faces_per_shot 调高或设为 null")
            return


def _check_occlusion(fs: _Findings, fr: Any, tag: str) -> None:
    """`occludes` 成环（§10.1）。

    **只做 schema 做不到的那一半**。`subjects` 重复、`occludes` 指到画幅外、
    `overlap_order` 与 `occludes` 矛盾，这些 `Framing._occlusion_consistent`
    已经在单文档内拦住了 —— 而本模块的输入无论如何都要先过一遍 pydantic，
    所以在这里再写一遍只会是永远跑不到的死代码（看着像有覆盖，其实没有）。
    schema 抓不住的是**三元及以上的环**：A→B→C→A 里任意两人都不互遮，
    单文档校验一路放行，却在适配层递归展开遮挡链时爆栈。
    """
    known = {s.char for s in fr.subjects}
    edges = {s.char: [c for c in s.occludes if c in known] for s in fr.subjects}
    cycle = _find_cycle(edges)
    if cycle:
        fs.add("block", "occ.occludes_cycle", tag,
               f"occludes 成环：{' → '.join(cycle)}",
               "遮挡是偏序关系，环会让适配层递归展开遮挡链时爆栈；"
               "删掉对构图影响最小的那条边")


# --------------------------------------------------------------------------
# 跨集 continuity_ledger（§10.1 + §7.3 + §7.4）
# --------------------------------------------------------------------------
def _norm_ledger(fs: _Findings, ctx: _Ctx, script: Script) -> dict[str, dict[str, Any]]:
    """把一集的账本归一成规范键名，顺手把非法项报掉（§7.4）。"""
    out: dict[str, dict[str, Any]] = {}
    ep = script.episode
    keys = ctx.ledger_keys
    for c, entry in script.continuity_ledger.items():
        if c not in ctx.cast_ids:
            fs.add("block", "ledger.unknown_char", f"ep{ep}/ledger",
                   f"账本里的角色 {c!r} 不在 plan.characters", ctx.cast_hint)
        clean: dict[str, Any] = {}
        for k, v in (entry or {}).items():
            key = _canon_ledger_key(k, keys)
            if key is None:
                fs.add("block", "ledger.unknown_key", f"ep{ep}/ledger/{c}",
                       f"内层键 {k!r} 不在 plan.continuity.mutable {list(keys)} 里",
                       "账本内层键必须与 continuity.mutable 同名（§7.4）；"
                       "口语别名（换装/获得道具/关系变化）由适配层归一后落库")
                continue
            if v is None:
                fs.add("block", "ledger.value_null", f"ep{ep}/ledger/{c}/{key}",
                       f"{key} 是 null，无法与「未记录」区分",
                       '确实没有就写 ""（字符串键）或 []（持有道具）')
                continue
            if _is_list_key(key) and not isinstance(v, list):
                fs.add("block", "ledger.bad_shape", f"ep{ep}/ledger/{c}/{key}",
                       f"{key} 应当是字符串数组（空数组=明确无道具），"
                       f"实际是 {type(v).__name__}",
                       '写成 ["青铜钥匙"]；没有就写 []')
                continue
            if _is_wardrobe_key(key) and c in ctx.cast_ids and v not in ctx.states[c]:
                fs.add("block", "ledger.unknown_state", f"ep{ep}/ledger/{c}/{key}",
                       f"{c} 没有服装态 {v!r}", f"现有：{sorted(ctx.states[c])}")
            clean[key] = v
        out[c] = clean
    return out


def _check_ledger_chain(fs: _Findings, ctx: _Ctx, scripts: list[Script]) -> None:
    """跨集："本集末态 == 下集初态"（§10.1 只看时间线内变化，§7.3）。"""
    normalized = {s.episode: _norm_ledger(fs, ctx, s) for s in scripts}
    eps = sorted(normalized)
    for a, b in zip(eps, eps[1:]):
        if b != a + 1:
            continue        # 中间那集没给，逐键比较会全是假告警
        la, lb = normalized[a], normalized[b]
        for c in sorted(set(la) | set(lb)):
            ea, eb = la.get(c, {}), lb.get(c, {})
            for key in ctx.ledger_keys:
                if key in ea and key not in eb:
                    fs.add("block", "ledger.carry_forward_missing",
                           f"ep{a}→ep{b}/{c}/{key}",
                           f"ep{a} 记了 {c} 的 {key}={ea[key]!r}，ep{b} 整条没记",
                           "账本是**末态快照**不是 diff：没变也要原样抄一遍；"
                           "确实没了就写 []（明确「无」≠「未记录」）")
    for s in scripts:
        _check_flashback_pollution(fs, s, normalized.get(s.episode, {}))


def _check_flashback_pollution(fs: _Findings, script: Script,
                               ledger: dict[str, dict[str, Any]]) -> None:
    """§7.3：闪回里的换装**不得**被记成状态变化。

    窄口径，只抓契约明文禁止的那一种：某角色最后一次**时间线内**出场穿 A，
    之后又在一个闪回场里穿了 B，而账本记的正是 B —— 这正是会让跨集校验误报、
    并连锁污染后续所有集的写法。
    """
    last_timeline: dict[str, int] = {}
    for i, scene in enumerate(script.scenes):
        for c in scene.chars_on_screen:
            if not _is_off_timeline(scene, c):
                last_timeline[c] = i
    for c, i in last_timeline.items():
        timeline_state = script.scenes[i].wardrobe_state.get(c)
        recorded = next((v for k, v in ledger.get(c, {}).items()
                         if _is_wardrobe_key(k)), None)
        if not timeline_state or not recorded or recorded == timeline_state:
            continue
        flash_states = {
            sc.wardrobe_state.get(c)
            for sc in script.scenes[i + 1:]
            if c in sc.chars_on_screen and _is_off_timeline(sc, c)
        }
        if recorded in flash_states:
            fs.add("block", "ledger.flashback_pollution",
                   f"ep{script.episode}/ledger/{c}",
                   f"账本把闪回里的服装 {recorded!r} 记成了本集末态"
                   f"（时间线内实际是 {timeline_state!r}）",
                   "闪回/幻象场的变化不计入账本时间线（§7.3）："
                   "末态要取闪回之前最近的时间线内状态")


# ==========================================================================
# 文件入口 + CLI
# ==========================================================================
_EP_DIR_RE = re.compile(r"^ep(\d+)$")


def _check_file_episode(fs: _Findings, path: str | Path, data: Any) -> None:
    """目录名 `ep<NN>` 与文档里的 `episode` 必须一致（2026-10-15 新增）。

    这是"**存错集**"的唯一防线。把第 3 集的 `script.json` 存进 `ep02/`：
    schema 合规、跨文档规则全过、`manju run --episode 2` 照跑不误 ——
    成片能出，只是**整集内容是错的**。规模化到几十集后，这类错事后极难定位，
    而它此刻只是一次"目录名与字段没对上"。

    只在**路径里能看出集号**时才判（`.../ep03/script.json`）：
    根目录那份没有集号可比，那是单集时代的正常姿势，交给 coordinator 的
    `_warn_if_root_fallback` 在开跑时提示。plan.json 没有 `episode` 字段，自然跳过。
    """
    m = _EP_DIR_RE.match(Path(path).parent.name)
    if m is None:
        return
    want = int(m.group(1))
    got = data.get("episode") if isinstance(data, dict) else None
    if not isinstance(got, int) or got == want:
        return
    fs.add("block", "file.episode_mismatch", Path(path).name,
           f"目录 {Path(path).parent.name} 是第 {want} 集，文档里 episode 写的是 {got}",
           f"两者必须一致：流水线按**目录**取文件，现在这一份会被当成第 {want} 集"
           f"跑完整集。改目录名或改 episode 字段，让两边同为 {want} 或同为 {got}")


def check_files(paths: Iterable[str | Path], *, strict: bool = False,
                tolerance: float = DEFAULT_TOLERANCE,
                speech_cps: float = DEFAULT_SPEECH_CPS,
                episode_sec: float | None = None,
                no_face_chars: Iterable[str] = (),
                max_faces_per_shot: int | None = None) -> Report:
    """从文件路径跑门。**读不出来 / schema 不过都算 report，不抛 ValidationError**。"""
    fs = _Findings()
    plan: dict[str, Any] | None = None
    scripts: list[dict[str, Any]] = []
    boards: list[dict[str, Any]] = []
    for p in paths:
        kind, data, why = classify_path(p)
        if kind == "unreadable":
            fs.add("block", "io.unreadable", Path(p).name, f"读不出来：{why}",
                   "确认路径、编码（utf-8 / utf-8-sig）与 JSON 合法性")
            continue
        if kind == "plan":
            plan = data
        elif kind == "storyboard":
            boards.append(data)
        elif kind == "script":
            scripts.append(data)
        else:
            fs.add("block", "io.unknown_kind", Path(p).name,
                   "认不出类型（顶层既无 characters+visual_bible，也无 scenes / shots）",
                   "按顶层键判定类型，文件名不作数")
        # 文件类型由顶层键决定（见上），但**集号**只能从路径看出来 —— 两者交叉验一次
        _check_file_episode(fs, p, data)

    if plan is None:
        # 没传 plan：直接说"缺 plan"，不要合成一个空 plan 去撞 schema ——
        # 那样用户会看到"plan.json 不符合 v10.1：project: Field required"，
        # 而他压根没传 plan.json，这种报错会把人带沟里
        fs.add("block", "ref.no_plan", "-", "这一批文件里没有 plan.json",
               "plan 是 char_id / state_id 的唯一权威，必须一起传")
        out = fs.report()
        return out.raise_if_blocked() if strict else out

    report = check_series(plan, scripts, boards, tolerance=tolerance,
                          speech_cps=speech_cps, episode_sec=episode_sec,
                          no_face_chars=no_face_chars,
                          max_faces_per_shot=max_faces_per_shot)
    out = Report.combine(fs.report(), report)
    return out.raise_if_blocked() if strict else out


def main(argv: Sequence[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        prog="python -m manju.gate",
        description="契约 §10.1 跨对象 block 门（plan + script + storyboard）")
    ap.add_argument("files", nargs="+",
                    help="plan.json / *.script.json / *.storyboard.json（按顶层键识别）")
    ap.add_argument("--strict", action="store_true",
                    help="存在 block 时退出码 1（默认只出报告，退出码 0）")
    ap.add_argument("--json", action="store_true", dest="as_json", help="以 JSON 输出结论")
    ap.add_argument("--tolerance", type=float, default=DEFAULT_TOLERANCE,
                    help=f"时长容差秒数（默认 {DEFAULT_TOLERANCE}）")
    ap.add_argument("--cps", type=float, default=None,
                    help=f"默认语速 字/秒；默认读 config 的 story.chars_per_second"
                         f"（{DEFAULT_SPEECH_CPS}）")
    ap.add_argument("--episode-sec", type=float, default=None,
                    help="覆盖单集目标时长（默认取 plan.output.episode_sec）")
    ap.add_argument("--no-face-chars", default="",
                    help="逗号分隔；声明为无脸的角色（面具/头盔/剪影），"
                         "不参与同框多脸计数。默认读 config 的 "
                         "story.no_face_chars")
    ap.add_argument("--max-faces", type=int, default=None,
                    help="每个画幅最多允许多少张可锁人脸；None/0=不限制（通用默认）。"
                         "默认读 config 的 story.max_faces_per_shot；"
                         "本地单参考图引擎可设 1")
    ap.add_argument("--quiet", action="store_true", help="只输出汇总行")
    args = ap.parse_args(argv)
    # Windows 控制台默认 GBK，中文结论会糊成乱码 —— 走项目自己的流初始化
    try:
        from .console import init as _init_console
        _init_console()
    except Exception:  # noqa: BLE001 - 独立调用（非包内）时降级即可
        pass

    # `no_face_chars` 的权威在 config（`story.no_face_chars`，旧 pipeline 兼容）。
    # 显式给了命令行参数就以命令行为准；读不到配置（独立调用）也不影响其余校验。
    no_face = [c.strip() for c in str(args.no_face_chars or "").split(",") if c.strip()]
    if not no_face:
        try:
            from .config import get_config
            cfg = get_config()
            raw = cfg.get("story.no_face_chars")
            if not raw:
                raw = cfg.get("pipeline.pulid.no_face_chars")
            no_face = [str(c) for c in (raw or [])]
        except Exception:  # noqa: BLE001 - 独立调用时降级为"没有无脸角色"
            no_face = []

    max_faces = args.max_faces
    if max_faces is None:
        try:
            from .config import get_config
            from .story.stages import params_from_cfg
            max_faces = params_from_cfg(get_config()).max_faces_per_shot
        except Exception:  # noqa: BLE001 - 独立调用时默认不限制
            max_faces = None

    cps = args.cps
    if cps is None:
        try:
            from .config import get_config
            from .story.stages import params_from_cfg
            cps = params_from_cfg(get_config()).chars_per_second
        except Exception:  # noqa: BLE001 - 独立调用时退回通用默认
            cps = DEFAULT_SPEECH_CPS

    report = check_files(args.files, tolerance=args.tolerance, speech_cps=cps,
                         episode_sec=args.episode_sec, no_face_chars=no_face,
                         max_faces_per_shot=max_faces)
    if args.as_json:
        print(json.dumps(report.to_json(), ensure_ascii=False, indent=2))
    elif args.quiet:
        print(f"block {len(report.blocks)} / warn {len(report.warns)}"
              f" / info {len(report.infos)}")
    else:
        print(report.to_text())
        if report.blocks and not args.strict:
            print("（未加 --strict：仅报告，退出码 0）")
    return 1 if (args.strict and report.blocks) else 0


if __name__ == "__main__":      # pragma: no cover
    sys.exit(main())
