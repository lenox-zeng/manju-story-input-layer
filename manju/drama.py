"""剧本/分镜的**戏剧层**审计（v12）。

`schemas.py` 校验单份文档的形状；`gate.py` 校验三份文档的技术一致性。
两者都不回答一个问题：**这 90 秒到底有没有戏、镜头能不能剪。**

这个模块补的就是那一层，并把 v12 规范落成可执行断言：

  - 剧本：目标/阻力/转折/代价/信息、20 秒内主动选择、中段失败、金手指规则、
    setup/payoff、台词是动作、结尾钩子由本集事件引发
  - 分镜：镜头密度、coverage 分布、景别/机位变化、单镜时长、起止动作、
    9:16 与 16:9 双构图、关键物象是否真的写进 prompt

设计约定与 `gate.py` 一致：纯函数、默认只报告、`strict=True` 才硬失败。
戏剧信息放在 `director.json`（script schema 是 extra="forbid"，不能加字段）。

CLI::

    python -m manju.drama plan.json script.json storyboard.json \
        --director director.json --strict
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Literal

from pydantic import ValidationError

from . import jsonio
from .schemas import Plan, Script, Storyboard

DEFAULT_TARGET_SEC = 90.0
DEFAULT_SPEECH_CPS = 4.5

# 允许的 beat 值。旧稿里的“升级”“爽点”不在内 —— 它们不是因果节拍。
BEATS = {"钩子", "激励", "抉择", "受挫", "转折", "代价", "高潮", "收束", "结尾钩子"}
COVERAGE = {"establish", "action", "reaction", "insert", "pov", "dialogue", "transition"}
CUT_REASONS = {
    "action_done", "reaction", "new_info", "pov", "sound",
    "space", "time", "match_cut",
}
FUNCTION_MARKERS = ("目标：", "阻力：", "转折：", "代价：", "信息：")

# 关键物象中文 → 英文 prompt 词。用于“这一场的故事物象有没有真的被画出来”。
OBJECT_LEXICON: dict[str, tuple[str, ...]] = {
    "武晶": ("crystal", "diamond-shaped crystal", "gem"),
    "刀": ("blade", "knife", "dagger"),
    "主梁": ("beam", "rafter", "support beam"),
    "断梁": ("broken beam", "fallen beam", "collapsed beam"),
    "裂纹": ("crack", "fracture"),
    "玩偶": ("doll", "rag doll"),
    "红布": ("red cloth",),
    "孩子": ("child", "small hand", "boy"),
    "手": ("hand", "hands", "fingers"),
    "兽群": ("beast herd", "beasts", "herd", "pack"),
    "荒兽": ("beast", "spined beast"),
    "狼": ("wolf",),
    "墨牙": ("wolf",),
    "避难棚": ("shelter", "shed", "lean-to"),
    "棚": ("shelter", "shed", "lean-to"),
    "地道": ("tunnel", "passage"),
    "隧道": ("tunnel", "passage"),
    "石墙": ("stone wall", "wall of stone", "barricade"),
    "崖": ("cliff",),
    "山坡": ("slope",),
    "尘雾": ("dust", "dust cloud"),
    "尘土": ("dust",),
    "火光": ("fire", "flame", "brazier", "sparks"),
    "火盆": ("brazier", "fire bowl"),
    "鼻血": ("nosebleed", "blood"),
    "血": ("blood",),
    "单筒": ("monocular", "spyglass", "scope"),
    "观测镜": ("monocular", "spyglass", "scope"),
    "高楼": ("rooftop", "ruined tower"),
}

# 这些词出现在 prompt 里说明写的是“风格/运镜”，不是这一帧的画面内容。
STYLE_WORDS = (
    "cinematic", "film still", "photorealistic", "masterpiece", "best quality",
    "anime", "manga", "teal-orange", "film grain", "8k", "4k",
)
CAMERA_WORDS = (
    "push in", "push-in", "pull out", "pan left", "pan right", "tilt up",
    "tilt down", "dolly", "tracking shot", "camera move",
)
_CJK = re.compile(r"[一-鿿]")


@dataclass(frozen=True)
class Finding:
    level: Literal["block", "warn", "info"]
    code: str
    where: str
    message: str
    fix: str = ""

    def line(self) -> str:
        s = f"[{self.level}] {self.code} @ {self.where}：{self.message}"
        return f"{s}\n        修法：{self.fix}" if self.fix else s


@dataclass
class Report:
    findings: list[Finding] = field(default_factory=list)

    def add(self, level: str, code: str, where: str, message: str, fix: str = "") -> None:
        self.findings.append(Finding(level, code, where, message, fix))  # type: ignore[arg-type]

    def block(self, *a: Any, **kw: Any) -> None:
        self.add("block", *a, **kw)

    def warn(self, *a: Any, **kw: Any) -> None:
        self.add("warn", *a, **kw)

    def info(self, *a: Any, **kw: Any) -> None:
        self.add("info", *a, **kw)

    @property
    def blocks(self) -> list[Finding]:
        return [f for f in self.findings if f.level == "block"]

    @property
    def warns(self) -> list[Finding]:
        return [f for f in self.findings if f.level == "warn"]

    @property
    def infos(self) -> list[Finding]:
        return [f for f in self.findings if f.level == "info"]

    def ok(self) -> bool:
        return not self.blocks

    def summary(self) -> str:
        return (f"drama: {len(self.blocks)} block / {len(self.warns)} warn"
                f" / {len(self.findings) - len(self.blocks) - len(self.warns)} info")

    def to_text(self) -> str:
        order = {"block": 0, "warn": 1, "info": 2}
        rows = sorted(self.findings, key=lambda f: (order[f.level], f.code, f.where))
        head = self.summary()
        body = "\n".join(f.line() for f in rows)
        return f"{head}\n{body}" if body else head

    def to_json(self) -> dict[str, Any]:
        return {
            "summary": {
                "block": len(self.blocks),
                "warn": len(self.warns),
                "info": len(self.findings) - len(self.blocks) - len(self.warns),
                "ok": self.ok(),
            },
            "findings": [
                {"level": f.level, "code": f.code, "where": f.where,
                 "message": f.message, "fix": f.fix}
                for f in self.findings
            ],
        }


# --------------------------------------------------------------------------
# 解析
# --------------------------------------------------------------------------
def _load_docs(files: Iterable[str | Path]) -> tuple[Plan | None, Script | None, Storyboard | None]:
    plan = script = board = None
    for path in files:
        raw = jsonio.read(Path(path))
        if not isinstance(raw, dict):
            raise ValueError(f"{path} 顶层必须是 JSON 对象")
        if "shots" in raw:
            board = Storyboard.model_validate(raw)
        elif "scenes" in raw:
            script = Script.model_validate(raw)
        elif "characters" in raw or "episodes" in raw:
            plan = Plan.model_validate(raw)
    return plan, script, board


def _scene_starts(script: Script) -> dict[str, float]:
    out: dict[str, float] = {}
    t = 0.0
    for s in script.scenes:
        out[s.scene_id] = round(t, 3)
        t += float(s.duration_sec)
    return out


def _han_count(text: str) -> int:
    return len(_CJK.findall(text or ""))


def _function_value(function: str, key: str) -> str:
    m = re.search(rf"{re.escape(key)}：(.*?)(?:；|$)", function or "")
    return (m.group(1).strip() if m else "")


def _shot_start_map(board: Storyboard) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    t = 0.0
    for s in board.shots:
        dur = float(s.duration_sec)
        out[s.shot_id] = {
            "start": round(t, 3), "end": round(t + dur, 3),
            "duration": dur, "shot": s,
        }
        t += dur
    return out


def _director_shot(director: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {
        str(x.get("shot_id")): x
        for x in (director.get("shots") or [])
        if isinstance(x, dict) and x.get("shot_id")
    }


def _coverage_of(ds: dict[str, Any] | None, shot: Any) -> str:
    if ds and ds.get("coverage"):
        return str(ds["coverage"]).strip()
    hints = getattr(shot, "hints", None)
    if hints is not None:
        val = getattr(hints, "coverage", None)
        if val:
            return str(val).strip()
        try:
            val = (hints.model_extra or {}).get("coverage")
        except Exception:  # noqa: BLE001
            val = None
        if val:
            return str(val).strip()
    return ""


def _cut_reason_of(ds: dict[str, Any] | None, shot: Any) -> str:
    if ds and ds.get("cut_reason"):
        return str(ds["cut_reason"]).strip()
    hints = getattr(shot, "hints", None)
    if hints is not None:
        val = getattr(hints, "cut_reason", None)
        if val:
            return str(val).strip()
        try:
            val = (hints.model_extra or {}).get("cut_reason")
        except Exception:  # noqa: BLE001
            val = None
        if val:
            return str(val).strip()
    return ""


def _soft(rep: Report, strict: bool, code: str, where: str,
          message: str, fix: str = "") -> None:
    """软门：默认 warning；--strict 时恢复为 block。"""
    (rep.block if strict else rep.warn)(code, where, message, fix)


def _prompt_has_object(prompts: list[str], obj: str) -> bool:
    toks = OBJECT_LEXICON.get(obj)
    if not toks:
        toks = (obj.lower(),)
    blob = " ".join(prompts).lower()
    return any(tok.lower() in blob for tok in toks)


# --------------------------------------------------------------------------
# 审计
# --------------------------------------------------------------------------
def audit(
    plan: Plan | None,
    script: Script,
    board: Storyboard,
    director: dict[str, Any] | None = None,
    *,
    target_sec: float = DEFAULT_TARGET_SEC,
    speech_cps: float = DEFAULT_SPEECH_CPS,
    strict: bool = False,
    max_shot_sec: float = 3.2,
    max_line_chars: int = 18,
) -> Report:
    rep = Report()
    starts = _scene_starts(script)
    scenes = {s.scene_id: s for s in script.scenes}
    shots_by_id = _shot_start_map(board)
    ds_by_id = _director_shot(director or {})
    total = float(script.total_duration)
    board_total = float(board.total_duration)

    # ---------------- 剧本结构 ----------------
    if abs(total - target_sec) > 2.0:
        rep.block("S1", "script.duration", f"总时长 {total:.2f}s，目标 {target_sec:.0f}s（容差 ±2s）",
                  "按目标集长重排场景 duration_sec")
    if not 6 <= len(script.scenes) <= 10:
        _soft(rep, strict, "S1", "script.scenes", f"{len(script.scenes)} 场",
              "90 秒集用 6–10 场；每场 6–16 秒")
    for s in script.scenes:
        if not 6.0 <= float(s.duration_sec) <= 16.0:
            rep.warn("S1", f"{s.scene_id}.duration",
                     f"单场 {s.duration_sec:.2f}s，建议 6–16s")
        if s.beat not in BEATS:
            rep.block("S2", f"{s.scene_id}.beat",
                      f"beat={s.beat!r} 不在 {sorted(BEATS)}",
                      "改成因果节拍，不要写“升级/爽点”")
        missing = [m for m in FUNCTION_MARKERS if m not in (s.function or "")]
        if missing:
            _soft(rep, strict, "S2", f"{s.scene_id}.function",
                  f"function 缺标记 {missing}",
                  "固定写：目标：…；阻力：…；转折：…；代价：…；信息：…")
        info = _function_value(s.function, "信息")
        if not info:
            rep.warn("S2", f"{s.scene_id}.information", "function 的“信息”为空")
        if not s.action or len(s.action) < 20:
            _soft(rep, strict, "S10", f"{s.scene_id}.action",
                  "action 太短，写不出可拍动作链",
                  "写：谁对谁做了什么 + 环境反应 + 可拍细节")
        if s.space_map and set(s.chars_on_screen) - set(s.space_map):
            rep.warn("S11", f"{s.scene_id}.space_map",
                     f"space_map 未覆盖 {sorted(set(s.chars_on_screen) - set(s.space_map))}")

    # 20 秒内主动选择
    early = [s for s in script.scenes
             if starts.get(s.scene_id, 999) <= 20.0 and s.beat == "抉择"]
    if not early:
        _soft(rep, strict, "S3", "spine.choice",
              "20 秒内没有任何一场 beat=抉择",
              "在 20s 前让主角做出不可逆的主动选择")

    # 中段失败
    fail = [s for s in script.scenes
            if 15.0 <= starts.get(s.scene_id, -1) <= 60.0
            and s.beat in {"受挫", "代价"}
            and _function_value(s.function, "代价") not in ("", "无")]
    if not fail:
        _soft(rep, strict, "S4", "spine.failure",
              "中段没有 beat=受挫/代价 且代价非“无”的场",
              "中段必须让主角真实失败一次并付出代价")

    # 台词
    n_lines = sum(len(s.lines) for s in script.scenes)
    if not 6 <= n_lines <= 12:
        _soft(rep, strict, "S8", "lines.count", f"{n_lines} 句台词，建议 6–12 句")
    for s in script.scenes:
        line_total = 0.0
        for line in s.lines:
            if line.speaker not in s.chars_on_screen:
                rep.block("S9", f"{s.scene_id}/{line.speaker}.speaker",
                          "说话人不在本场 chars_on_screen",
                          "把说话人加入 chars_on_screen，或改台词归属")
            n = _han_count(line.text)
            if n > max_line_chars:
                rep.block("S7", f"{s.scene_id}/{line.speaker}.length",
                          f"{n} 字：{line.text!r}",
                          f"单句 ≤{max_line_chars} 汉字（story.max_line_chars 可配）")
            need = n / max(0.1, float(s.speech_cps or speech_cps))
            if line.timing_sec is not None and float(line.timing_sec) + 1e-6 < need:
                rep.block("S8", f"{s.scene_id}/{line.speaker}.timing",
                          f"{line.timing_sec:.2f}s < {need:.2f}s（{n} 字）",
                          "加时长或删字")
            if line.at_sec is not None:
                if not (starts[s.scene_id] - 1e-6 <= float(line.at_sec)
                        <= starts[s.scene_id] + float(s.duration_sec) + 1e-6):
                    rep.block("S8", f"{s.scene_id}/{line.speaker}.at_sec",
                              f"at_sec={line.at_sec} 越出本场")
            line_total += float(line.timing_sec or need)
        if line_total > float(s.duration_sec) * 0.55 + 1e-6:
            rep.warn("S8", f"{s.scene_id}.line_ratio",
                     f"台词 {line_total:.2f}s / 场 {s.duration_sec:.2f}s 超过 55%")
        # 旁白/内心独白：不做口型，但 speaker/时长/场内坐标必须自洽。
        for ni, n in enumerate(getattr(s, "narration", []) or []):
            if n.kind == "inner" and n.speaker not in s.chars_on_screen:
                rep.block("S9", f"{s.scene_id}/narration[{ni}].speaker",
                          f"内心独白 {n.speaker} 不在本场 chars_on_screen",
                          "让说话人出现在本场，或改成 NARRATOR 解释性旁白")
            need = _han_count(n.text) / 5.0
            if n.timing_sec is not None and float(n.timing_sec) + 1e-6 < need:
                rep.block("S8", f"{s.scene_id}/narration[{ni}].timing",
                          f"{n.timing_sec:.2f}s < {need:.2f}s（{_han_count(n.text)} 字）",
                          "加时长或删字")
            if n.at_sec is not None and not (
                    starts[s.scene_id] - 1e-6 <= float(n.at_sec)
                    <= starts[s.scene_id] + float(s.duration_sec) + 1e-6):
                rep.block("S8", f"{s.scene_id}/narration[{ni}].at_sec",
                          f"at_sec={n.at_sec} 越出本场")

    # ---------------- 导演层 ----------------
    if not director:
        rep.block("D0", "director.missing",
                  "缺少导演层；spine/setup_payoff/coverage/切点无法校验",
                  "生成 director.json，见 docs/剧本规范_v12_导演级.md §5")
    else:
        spine = director.get("spine") or {}
        for k in ("want", "obstacle", "choice", "cost", "turn", "hook"):
            if not str(spine.get(k) or "").strip():
                _soft(rep, strict, "D1", f"director.spine.{k}", "为空")
        power = director.get("power_rule") or {}
        for k in ("name", "sees", "cannot", "cost"):
            if not str(power.get(k) or "").strip():
                _soft(rep, strict, "D2", f"director.power_rule.{k}", "为空")
        sp = director.get("setup_payoff") or []
        if len(sp) < 3:
            _soft(rep, strict, "D3", "setup_payoff.count",
                  f"{len(sp)} 组，至少 3 组")
        order = {sid: i for i, sid in enumerate(shots_by_id)}
        for i, item in enumerate(sp):
            sid = f"SP{i + 1}"
            a = str(item.get("setup_shot") or "")
            b = str(item.get("payoff_shot") or "")
            obj = str(item.get("object") or "")
            if a not in shots_by_id or b not in shots_by_id:
                _soft(rep, strict, "D3", f"setup_payoff.{sid}.shots",
                      f"setup={a!r} payoff={b!r} 有镜头不存在")
                continue
            if order[a] >= order[b]:
                _soft(rep, strict, "D3", f"setup_payoff.{sid}.order",
                      f"payoff {b} 不在 setup {a} 之后")
            if obj:
                setup_prompt = shots_by_id[a]["shot"].prompt
                payoff_prompt = shots_by_id[b]["shot"].prompt
                if not _prompt_has_object([setup_prompt], obj):
                    _soft(rep, strict, "D3",
                          f"setup_payoff.{sid}.object_setup",
                          f"setup 镜头 {a} 的 prompt 没有 {obj}")
                if not _prompt_has_object([payoff_prompt], obj):
                    _soft(rep, strict, "D3",
                          f"setup_payoff.{sid}.object_payoff",
                          f"payoff 镜头 {b} 的 prompt 没有 {obj}")
        dscenes = {str(x.get("scene_id")): x for x in (director.get("scenes") or [])
                   if isinstance(x, dict)}
        for s in script.scenes:
            dx = dscenes.get(s.scene_id)
            if not dx:
                _soft(rep, strict, "D4", f"{s.scene_id}.director",
                      "导演层缺这场")
                continue
            for k in ("goal", "obstacle", "turn", "cost", "information"):
                if not str(dx.get(k) or "").strip():
                    _soft(rep, strict, "D4", f"{s.scene_id}.{k}",
                          "导演层字段为空")
        last = script.scenes[-1]
        if last.beat != "结尾钩子":
            rep.block("S12", f"{last.scene_id}.beat",
                      f"最后一场 beat={last.beat!r}，应为“结尾钩子”")
        hook = str(spine.get("hook") or "")
        if not hook:
            _soft(rep, strict, "S12", "director.spine.hook", "为空")

    # ---------------- 分镜密度 ----------------
    n_shots = len(board.shots)
    if n_shots < 35 or n_shots > 60:
        _soft(rep, strict, "B1", "board.count",
              f"{n_shots} 镜，90 秒集应为 40–55 镜（硬边界 35–60）")
    elif n_shots < 40 or n_shots > 55:
        rep.warn("B1", "board.count", f"{n_shots} 镜，建议 40–55")
    avg = board_total / max(1, n_shots)
    if avg > 2.6:
        _soft(rep, strict, "B1", "board.avg",
              f"平均 {avg:.2f}s/镜，上限 2.6s")
    elif avg > 2.4:
        rep.warn("B1", "board.avg", f"平均 {avg:.2f}s/镜，建议 ≤2.4")
    if abs(board_total - total) > 0.5:
        rep.block("B1", "board.total",
                  f"分镜总长 {board_total:.2f}s ≠ 剧本 {total:.2f}s")
    for s in board.shots:
        if float(s.duration_sec) > max_shot_sec:
            rep.block("B1", f"{s.shot_id}.duration",
                      f"{s.duration_sec:.2f}s > {max_shot_sec:.1f}s")
        elif float(s.duration_sec) > max_shot_sec - 0.2:
            rep.warn("B1", f"{s.shot_id}.duration",
                     f"{s.duration_sec:.2f}s > {max_shot_sec - 0.2:.1f}s")
        if s.scene_id not in scenes:
            rep.block("B2", f"{s.shot_id}.scene",
                      f"scene_id={s.scene_id!r} 不存在")
        if not (s.keyframe or "").strip():
            rep.warn("B2", f"{s.shot_id}.keyframe", "keyframe 为空")
        if not (s.motion or "").strip():
            rep.warn("B2", f"{s.shot_id}.motion", "motion 为空")
        for aspect in ("9:16", "16:9"):
            if aspect not in s.framing:
                rep.block("B3", f"{s.shot_id}.framing", f"缺 {aspect} 构图")
        p = (s.prompt or "").lower()
        if _CJK.search(s.prompt or ""):
            rep.block("B4", f"{s.shot_id}.prompt", "prompt 里出现中文")
        bad_style = [w for w in STYLE_WORDS if w in p]
        if bad_style:
            rep.warn("B4", f"{s.shot_id}.prompt", f"风格词 {bad_style}")
        bad_cam = [w for w in CAMERA_WORDS if w in p]
        if bad_cam:
            rep.block("B4", f"{s.shot_id}.prompt", f"镜头运动词 {bad_cam}")

    by_scene: dict[str, list[Any]] = {sid: [] for sid in scenes}
    for s in board.shots:
        by_scene.setdefault(s.scene_id, []).append(s)
    for sid, xs in by_scene.items():
        if sid not in scenes:
            continue
        if len(xs) < 3:
            _soft(rep, strict, "B5", f"{sid}.shots",
                  f"{len(xs)} 镜，至少 4 镜（硬边界 3）")
        elif len(xs) < 4:
            rep.warn("B5", f"{sid}.shots", f"{len(xs)} 镜，建议 4–8 镜")
        elif len(xs) > 8:
            rep.warn("B5", f"{sid}.shots", f"{len(xs)} 镜，建议 4–8 镜")
        durs = [round(float(x.duration_sec), 3) for x in xs]
        if len(durs) > 2 and len(set(durs)) == 1:
            rep.warn("B5", f"{sid}.durations",
                     f"{len(durs)} 镜全部 {durs[0]}s，旧规范后遗症")
        sum_d = sum(durs)
        if abs(sum_d - float(scenes[sid].duration_sec)) > 0.3:
            rep.block("B5", f"{sid}.duration_sum",
                      f"镜头和 {sum_d:.2f}s ≠ 场景 {scenes[sid].duration_sec:.2f}s")

    # ---------------- coverage / 景别 / 机位 ----------------
    cov: dict[str, int] = {k: 0 for k in COVERAGE}
    sizes: dict[str, int] = {}
    cams: dict[str, int] = {}
    for s in board.shots:
        ds = ds_by_id.get(s.shot_id)
        c = _coverage_of(ds, s)
        if c not in COVERAGE:
            rep.block("B6", f"{s.shot_id}.coverage",
                      f"coverage={c!r} 不在 {sorted(COVERAGE)}",
                      "在 director.json 的 shots 里补 coverage")
        else:
            cov[c] += 1
        cr = _cut_reason_of(ds, s)
        if cr not in CUT_REASONS:
            rep.block("B6", f"{s.shot_id}.cut_reason",
                      f"cut_reason={cr!r} 不在 {sorted(CUT_REASONS)}")
        if ds:
            if not str(ds.get("purpose") or "").strip():
                rep.warn("B6", f"{s.shot_id}.purpose", "purpose 为空")
            if not str(ds.get("information") or "").strip():
                _soft(rep, strict, "B6", f"{s.shot_id}.information",
                      "information 为空")
        for aspect, fr in s.framing.items():
            sizes[fr.shot_size] = sizes.get(fr.shot_size, 0) + 1
        cams[s.camera or "static"] = cams.get(s.camera or "static", 0) + 1

    for k, n in (("reaction", 10), ("insert", 8), ("establish", 4)):
        hard = {"reaction": 8, "insert": 6, "establish": 3}[k]
        if cov[k] < hard:
            _soft(rep, strict, "B7", f"coverage.{k}",
                  f"{cov[k]} 个，至少 {hard}")
        elif cov[k] < n:
            rep.warn("B7", f"coverage.{k}", f"{cov[k]} 个，建议 ≥{n}")
    if cov["pov"] < 1:
        _soft(rep, strict, "B7", "coverage.pov", "没有 pov 镜头")

    # 连续 3 镜同景别 + 同 coverage
    run: list[tuple[str, str, str]] = []
    for s in board.shots:
        ds = ds_by_id.get(s.shot_id)
        c = _coverage_of(ds, s)
        size = next(iter(s.framing.values())).shot_size if s.framing else ""
        key = (size, c)
        if run and run[-1][1:] == key:
            run.append((s.shot_id, size, c))
        else:
            run = [(s.shot_id, size, c)]
        if len(run) >= 3:
            _soft(rep, strict, "B8", f"{run[-1][0]}.repeat",
                  f"连续 3 镜同景别/同 coverage：{size}/{c}",
                  "插入反应、插入或不同景别")
            run = []

    if len(sizes) <= 1:
        _soft(rep, strict, "B9", "board.sizes", "全集只有一种景别")
    static = cams.get("static", 0) / max(1, n_shots)
    # 只限制“几乎全静止”。关键拍已在故事层强制运镜，这里默认只提示；
    # strict 模式恢复为硬门槛。
    if static > 0.85:
        _soft(rep, strict, "B9", "board.static",
              f"static {static:.0%}，接近幻灯片")
    elif static < 0.15:
        rep.warn("B9", "board.static",
                 f"static 仅 {static:.0%}，全片几乎都在动")

    # ---------------- 关键物象是否进 prompt ----------------
    if director:
        prompts_by_scene: dict[str, list[str]] = {sid: [] for sid in scenes}
        for s in board.shots:
            prompts_by_scene.setdefault(s.scene_id, []).append(s.prompt or "")
        for dx in (director.get("scenes") or []):
            sid = str(dx.get("scene_id") or "")
            if sid not in scenes:
                continue
            objects = dx.get("objects") or []
            if not isinstance(objects, list):
                objects = []
            for obj in objects:
                obj = str(obj)
                if not _prompt_has_object(prompts_by_scene.get(sid, []), obj):
                    _soft(rep, strict, "B10", f"{sid}.objects",
                          f"关键物象 {obj!r} 没有出现在本场任何 prompt 里",
                          "把该物象写成画面主体/前景，或拆一个插入镜")

    # ---------------- 时间轴 ----------------
    if abs(board_total - target_sec) > 0.5:
        rep.warn("B11", "board.target",
                 f"{board_total:.2f}s vs 目标 {target_sec:.0f}s")

    return rep


def audit_files(
    files: Iterable[str | Path],
    director_path: str | Path | None = None,
    *,
    target_sec: float = DEFAULT_TARGET_SEC,
    speech_cps: float = DEFAULT_SPEECH_CPS,
    strict: bool = False,
    max_shot_sec: float = 3.2,
    max_line_chars: int = 18,
) -> Report:
    plan, script, board = _load_docs(files)
    if script is None:
        raise ValueError("没有找到 script（含 scenes 的 JSON）")
    if board is None:
        raise ValueError("没有找到 storyboard（含 shots 的 JSON）")
    director = None
    if director_path:
        p = Path(director_path)
        if p.is_file():
            director = jsonio.read(p)
        else:
            raise FileNotFoundError(f"director.json 不存在：{p}")
    return audit(plan, script, board, director,
                 target_sec=target_sec, speech_cps=speech_cps, strict=strict,
                 max_shot_sec=max_shot_sec, max_line_chars=max_line_chars)


def main(argv: list[str] | None = None) -> int:
    """独立入口：python -m manju.drama ...（CLI 也注册了同名子命令）。"""
    import argparse

    ap = argparse.ArgumentParser(prog="manju-drama", description="剧本/分镜戏剧审计 v12")
    ap.add_argument("files", nargs="+")
    ap.add_argument("--director")
    ap.add_argument("--target-sec", type=float, default=DEFAULT_TARGET_SEC)
    ap.add_argument("--cps", type=float, default=None,
                    help="语速基线；默认读 config 的 story.chars_per_second（4.5）")
    ap.add_argument("--max-line-chars", type=int, default=None,
                    help="单句台词字数上限；默认读 config 的 story.max_line_chars（18）")
    ap.add_argument("--max-shot-sec", type=float, default=None,
                    help="单镜时长上限；默认读 config 的 story.max_shot_seconds（3.2）")
    ap.add_argument("--json", action="store_true", dest="as_json")
    ap.add_argument("--strict", action="store_true")
    a = ap.parse_args(argv)

    cps, line_chars, max_shot = a.cps, a.max_line_chars, a.max_shot_sec
    if cps is None or line_chars is None or max_shot is None:
        try:
            from .config import get_config
            from .story.stages import params_from_cfg
            sp = params_from_cfg(get_config())
            cps = sp.chars_per_second if cps is None else cps
            line_chars = sp.max_line_chars if line_chars is None else line_chars
            max_shot = sp.max_shot_seconds if max_shot is None else max_shot
        except Exception:  # noqa: BLE001 - 无 config 时退回通用默认
            cps = DEFAULT_SPEECH_CPS if cps is None else cps
            line_chars = 18 if line_chars is None else line_chars
            max_shot = 3.2 if max_shot is None else max_shot
    try:
        rep = audit_files(a.files, a.director,
                          target_sec=a.target_sec, speech_cps=cps,
                          strict=a.strict, max_line_chars=line_chars,
                          max_shot_sec=max_shot)
    except (OSError, ValueError, ValidationError) as e:  # noqa: BLE001
        print(f"[block] drama 读取失败：{type(e).__name__}: {e}")
        return 1
    if a.as_json:
        print(json.dumps(rep.to_json(), ensure_ascii=False, indent=2))
    else:
        print(rep.to_text())
        if rep.blocks and not a.strict:
            print("（未加 --strict：仅报告，退出码 0）")
    return 1 if (a.strict and rep.blocks) else 0


if __name__ == "__main__":
    raise SystemExit(main())
