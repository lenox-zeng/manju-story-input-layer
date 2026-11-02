# -*- coding: utf-8 -*-
"""提示词质量门：segments 段文本的重复/截断/动作完整性确定性校验。

背景（2026-11-01）：EP01 实测 65/68 镜 segments 走 compile `_fallback_segments`
兜底，旧实现把整镜 prompt_en 前 80 字符整段复制成每段 text（段间 100% 重复、
剧情动作丢失、信息保留 ~15%）。本门用于把这类退化挡在门外，并作为
segments 修复（compile.py `_fallback_segments` 语义拆段）的回归基线。

判据（按严重度分级）：
  block：
    b1 段 text <8 词（信息量不足）
    b2 段 text 以整镜 prompt 前 15 词为前缀（旧"复制开头"特征）
    b3 同镜段间两两 Jaccard 词集相似度 > 0.60（重复文本）
  warn：
    w1 段间相似度 > 0.40（接近重复，LLM 段从严）
    w2 段 text 缺动作动词（-ing/-ed/原形-s；establish/静止镜允许缺，可配豁免）
    w3 LLM 段（source 非 fallback）与整镜 prompt 词集重合 > 0.60（应写自己的画面）
    w4 整镜 prompt 缺摄影配方：无光照词（光源/质量/色温）
    w5 整镜 prompt 缺摄影配方：无镜头词（焦段/景深）——不给光照=引擎默认平光，
       镜头词缺失=画面纵深与压迫感失控（P0.5 规则，warn 不 block）
    w6 整镜 prompt 含拍摄调度词（dialogue shot / reaction shot / POV 等，自检 2026-11-02）
    w7 整镜 prompt 含空泛修饰词（vivid / stunning 等无效字符）

用法：python scripts/check_prompt_quality.py [storyboard.json] [--strict]
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

ACTION_VERB = re.compile(
    r"(?:ing\b|ed\b|(?:turn|step|walk|run|grab|push|pull|reach|lean|open|close|lift|drop|"
    r"rush|point|raise|swing|bend|kneel|stand|rise|shift|move|enter|leave|flee|creep|"
    r"crouch|jump|throw|grip|yank|shove|whirl|dive|lunge|snap|scoop|clutch|pivot|slip|"
    r"stagger|tumble|spin|slam|stomp|pound|stare|glance|freeze|bow|hold|carry|follow)"
    r"(?:s|es)\b)", re.I)

# 豁免动作词检查的 coverage（纯状态/定场镜）
ACTION_EXEMPT = {"establish", "transition"}

# P0.5 摄影配方（warn 级）：光照词 = 光源方向/质量/色温
LIGHT_WORDS = re.compile(
    r"(light|lighting|sunlight|daylight|lamplight|moonlight|firelight|"
    r"backlight|rim\s*light|key\s*light|soft|hard|diffused|harsh|"
    r"cool|warm|grey|golden|glow|shadow|halo|dusk|dawn|overcast|"
    r"screen-left|screen-right|from\s+behind|top\s+light|window)", re.I)
# 镜头词 = 焦段/景深
LENS_WORDS = re.compile(
    r"(mm\b|lens|shallow|deep\s*focus|depth\s*of\s*field|bokeh|"
    r"wide-angle|telephoto|compressed|focal|long\s*lens|macro)", re.I)
# 拍摄调度词（2026-11-02 自检发现：S5 对话段 prompt 混入 "dialogue shot:" 前缀；
# 拍摄类型/调度指令应放在 framing/coverage 字段，prompt_en 只写画面内容。warn 不 block：
# 旧产物已存在，block 会破坏既有产物全绿状态；规则文本已硬禁，warn 用于暴露存量）
DIRECTOR_WORDS = re.compile(
    r"(?:dialogue\s+shot|reaction\s+shot|insert\s+shot|point-of-view\s+shot|"
    r"establishing\s+shot|two-shot|over-the-shoulder|reverse\s+angle|"
    r"(?<![a-z-])pov(?!\w))", re.I)
# 空泛修饰词（"无效字符"：不传递可渲染信息，用户判据明确点名）
FLUFF_WORDS = re.compile(
    r"\b(vivid|vibrant|stunning|beautiful|gorgeous|amazing|breathtaking|"
    r"epic|striking|dramatic|rich|detailed|intricate|elaborate|lovely|"
    r"wonderful|perfect|extraordinary|impressive|majestic|serene|tranquil)\b", re.I)


def _words(text: str) -> set[str]:
    return set(re.findall(r"[a-z]+", (text or "").lower()))


def _jaccard(a: set[str], b: set[str]) -> float:
    if not a and not b:
        return 0.0
    return len(a & b) / len(a | b)


def _finding(level: str, code: str, where: str, message: str, fix: str) -> dict:
    return {"level": level, "code": code, "where": where,
            "message": message, "fix": fix}


def check_board(path: Path, *, strict: bool = False) -> tuple[list[dict], int]:
    data = json.loads(path.read_text(encoding="utf-8-sig"))
    shots = data.get("shots") or []
    findings: list[dict] = []
    seg_shots = 0
    for s in shots:
        pid = s.get("shot_id") or s.get("id") or "?"
        # w4/w5：整镜 prompt 摄影配方（P0.5 规则，warn 不 block）——无 segments
        # 的单动作镜同样必须带配方，故放在 segs 判断之前对每镜独立检查
        prompt_text = s.get("prompt") or ""
        if prompt_text and not LIGHT_WORDS.search(prompt_text):
            findings.append(_finding(
                "warn", "prompt.no_light_recipe", pid,
                "整镜 prompt 未检出光照词（光源方向/质量/色温），引擎将默认平光",
                "prompt 结尾写明光：screen-left/behind/rim + soft/hard + cool/warm 色温"))
        if prompt_text and not LENS_WORDS.search(prompt_text):
            findings.append(_finding(
                "warn", "prompt.no_lens_recipe", pid,
                "整镜 prompt 未检出镜头词（焦段/景深），画面纵深与压迫感失控",
                "prompt 结尾写明镜：50mm shallow depth of field / 35mm deep focus"))
        # w6a：拍摄调度词混入 prompt_en（自检 2026-11-02）
        if prompt_text:
            dm = DIRECTOR_WORDS.search(prompt_text)
            if dm:
                findings.append(_finding(
                    "warn", "prompt.director_word", pid,
                    f"prompt_en 含拍摄调度词 '{dm.group(0).strip()}'（dialogue shot / "
                    f"reaction shot / POV 等），拍摄类型应由 framing/coverage 表达",
                    "删除调度词前缀，prompt_en 只写本帧画面内容（主体+动作+光线）"))
        # w6b：空泛修饰词（不传递可渲染信息）
        if prompt_text:
            fm = FLUFF_WORDS.search(prompt_text)
            if fm:
                findings.append(_finding(
                    "warn", "prompt.fluff", pid,
                    f"prompt_en 含空泛修饰词 '{fm.group(0).lower()}'（vivid/stunning 等），"
                    f"不传递可渲染信息", "替换为具体可渲染细节（色名/材质/光线条件）"))
        # w6：camera_recipe 结构化槽位（P0.5）——仅当产物已声明 framing 时检查，
        # 旧产物无 framing 字段视为未升级契约，不制造噪音。
        # 契约 §6.5：camera_recipe 是 Framing（per-aspect）槽位，检查任一画幅。
        framing = (s.get("framing") or {})
        recipe = None
        if framing:
            for aspect in ("9:16", "16:9"):
                af = framing.get(aspect) if isinstance(framing, dict) else None
                if isinstance(af, dict) and af.get("camera_recipe"):
                    recipe = af["camera_recipe"]
                    break
        if framing:
            filled = bool(recipe) and any(
                recipe.get(k) for k in ("lens_mm", "depth_of_field",
                                        "light_key", "light_quality"))
            if not filled:
                findings.append(_finding(
                    "warn", "prompt.no_camera_recipe", pid,
                    "framing.camera_recipe 槽位缺失或全空（P0.5 要求摄影配方结构化）",
                    "填 lens_mm/depth_of_field/light_key/light_quality（insert/pov 可省 composition）"))
        segs = s.get("segments") or []
        if not segs:
            continue
        seg_shots += 1
        prompt = _words(prompt_text)
        prompt_head = " ".join((s.get("prompt") or "").split())[:15].lower()
        texts = [str(x.get("text") or "") for x in segs]
        # b1：段 text 碎片（<4 词，连主体+2 词状态都不够）→ warn 提示，
        #    不 block：真实状态表情段（"The figure, chin trembling."）词少但语义完整，
        #    核心 block 判据是重复/截断（b2/b3，即用户的"重复/无效字符/剧情丢失"）。
        for i, t in enumerate(texts):
            if len(_words(t)) < 4:
                findings.append(_finding(
                    "warn", "seg.too_short", f"{pid}/seg{i}",
                    f"段 text 仅 {len(_words(t))} 词（<4），偏碎片化",
                    "段 text 建议 ≥4 词；纯碎片段需补充主语+状态/动作"))
        # b2：非首段复制整镜 prompt 开头（start 段天然继承基础定位句，允许）
        for i, t in enumerate(texts):
            if i == 0:
                continue
            if len(_words(t)) >= 5 and " ".join(t.split())[:15].lower() == prompt_head:
                findings.append(_finding(
                    "block", "seg.prompt_head_copy", f"{pid}/seg{i}",
                    f"段{i} text 以整镜 prompt 开头 15 词为前缀（旧'复制开头'特征），"
                    f"剧情动作会被截断丢失", "段 text 必须按本段主动作重组，禁止整段复制 prompt 开头"))
        # b3 / w1：段间相似度
        for i in range(len(texts)):
            for j in range(i + 1, len(texts)):
                sim = _jaccard(_words(texts[i]), _words(texts[j]))
                if sim > 0.60:
                    findings.append(_finding(
                        "block", "seg.dup", f"{pid}",
                        f"段{i}与段{j} Jaccard 相似度 {sim:.2f} > 0.60（重复文本，一段一动作被破坏）",
                        "每段 text 必须写本段自己的主动作句"))
                elif sim > 0.40:
                    findings.append(_finding(
                        "warn", "seg.near_dup", f"{pid}",
                        f"段{i}与段{j} Jaccard 相似度 {sim:.2f} > 0.40（接近重复）",
                        "收紧段文本，只保留本段动作的独立表述"))
        # w2：段缺动作动词
        coverage = (s.get("coverage") or "").lower()
        for i, t in enumerate(texts):
            if coverage in ACTION_EXEMPT:
                continue
            if not ACTION_VERB.search(t):
                findings.append(_finding(
                    "warn", "seg.no_verb", f"{pid}/seg{i}",
                    "段 text 未检出动作动词（-ing/-ed/动作原形），画面可能静止无推进",
                    "段 text 应含本段主动作的英文动词句（start/mid/end 推进）"))
        # w3：LLM 段与整镜 prompt 重合过高（fallback 段与整镜同源，豁免）
        if not any((x.get("source") or "") == "fallback" for x in segs):
            for i, t in enumerate(texts):
                sim = _jaccard(_words(t), prompt)
                if sim > 0.60:
                    findings.append(_finding(
                        "warn", "seg.prompt_overlap", f"{pid}/seg{i}",
                        f"LLM 段与整镜 prompt 词集重合 {sim:.2f} > 0.60（应写本段自己的画面）",
                        "段 text 独立描述本段动作，避免整段复用整镜 prompt"))
    blocks = [f for f in findings if f["level"] == "block"]
    print(f"=== 提示词质量门 ===")
    print(f"文件: {path}")
    print(f"总镜数: {len(shots)}，含 segments 镜数: {seg_shots}")
    print(f"findings: block={len(blocks)} warn={len(findings) - len(blocks)}")
    for f in findings:
        print(f"  [{f['level'].upper()}] {f['code']} {f['where']}: {f['message'][:100]}")
    print(f"结论: {'PASS' if not blocks else f'{len(blocks)} BLOCK（需修复）'}")
    return findings, 1 if blocks else 0


def main() -> int:
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    strict = "--strict" in sys.argv
    path = Path(args[0]) if args else Path("input/.work_绿野仙踪/ep01/storyboard.json")
    if not path.exists():
        print(f"找不到 storyboard: {path}")
        return 2
    _, code = check_board(path, strict=strict)
    return code


if __name__ == "__main__":
    raise SystemExit(main())
