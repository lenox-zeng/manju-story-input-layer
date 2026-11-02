# -*- coding: utf-8 -*-
"""长篇压缩：分卷事件摘要 → ≤ 限额的"全书法典"。

规则（确定性优先）：
* 短篇（全文 ≤ 法典限额）**跳过**压缩，原文直接当法典。
* 每 `volume_chapters`（默认 15）章一卷；每卷一次 LLM 调用，
  产出 `events`（事件链）+ `new_entities`（本卷首次出现的人名/地名/道具）。
* 最多 `max_volumes`（默认 60）卷；超限**明确**标记 truncated 并给出被省略的章节范围，
  不静默截断。
* 合并后仍超限时，确定性截断到限额并记 warning。
* 可把每卷摘要缓存在 workdir，`resume=True` 时复用。
"""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from pydantic import BaseModel, ConfigDict, Field

from .ingest import Chapter, Novel

Progress = Callable[[str], None]


class VolumeDigest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    events: str = Field(description="按时间顺序的事件链：谁做了什么、结果是什么")
    new_entities: list[str] = Field(
        default_factory=list, description="本卷首次出现的人名/地名/关键道具")


@dataclass
class Volume:
    index: int
    chapters: list[Chapter]
    digest: VolumeDigest | None = None

    @property
    def start(self) -> int:
        return self.chapters[0].start

    @property
    def end(self) -> int:
        return self.chapters[-1].end

    @property
    def title(self) -> str:
        a, b = self.chapters[0].index, self.chapters[-1].index
        return f"第{a}–{b}章" if a != b else f"第{a}章"


@dataclass
class Codex:
    text: str
    volumes: list[Volume] = field(default_factory=list)
    truncated: bool = False
    skipped: bool = False                 # 短篇：未压缩
    warnings: list[str] = field(default_factory=list)

    @property
    def new_entities(self) -> list[str]:
        seen: list[str] = []
        for v in self.volumes:
            for e in (v.digest.new_entities if v.digest else []):
                if e not in seen:
                    seen.append(e)
        return seen

    def to_dict(self) -> dict[str, Any]:
        return {
            "text": self.text,
            "truncated": self.truncated,
            "skipped": self.skipped,
            "warnings": list(self.warnings),
            "new_entities": self.new_entities,
            "volumes": [
                {"index": v.index, "title": v.title,
                 "start": v.start, "end": v.end,
                 "digest": v.digest.model_dump(mode="json") if v.digest else None}
                for v in self.volumes
            ],
        }


_SUMMARY_SYS = """# 你的角色
你是长篇小说的剧情压缩器，只记录事实。

# 你的任务
把这一卷的章节正文压缩成按时间顺序的事件链：谁做了什么、对谁做、结果是什么；
保留因果、人物关系变化、关键道具的转移、地点变化与本卷结尾的悬念。

# 铁律
1. 只依据输入正文，不补充、不推断、不评论、不写台词。
2. events 写中文叙述，按时间顺序，200-600 字。
3. new_entities 只列本卷首次出现的人名/地名/关键道具；没有就给空数组。
4. 不要写"本卷讲述了"这类空话，直接写事件。

# 输出
只输出一个 JSON 对象：{"events": "...", "new_entities": ["..."]}"""

_MERGE_SYS = """# 你的角色
你是长篇小说的事实法典编纂者。

# 任务
把按卷排列的事件摘要合并成一份连续的《全书法典》，供后续分集改编直接引用：
保留主线因果、人物关系、关键道具、重要地点、力量/规则边界与结局。
不要评论，不要写台词，不要新增事实。

# 输出
只输出法典正文（纯文本），不要 JSON、不要 markdown 围栏。"""


def group_volumes(novel: Novel, *, chapters_per_volume: int = 15,
                  max_volumes: int = 60) -> tuple[list[Volume], list[str]]:
    """按章分卷；返回 (卷列表, warnings)。超限时只保留前 max_volumes 卷。"""
    if chapters_per_volume <= 0 or max_volumes <= 0:
        raise ValueError("chapters_per_volume / max_volumes 必须 > 0")

    chapters = list(novel.chapters)
    limit = chapters_per_volume * max_volumes
    warnings: list[str] = []
    if len(chapters) > limit:
        omitted = chapters[limit:]
        warnings.append(
            f"原文共 {len(chapters)} 章，超过上限 {max_volumes} 卷 × "
            f"{chapters_per_volume} 章；只压缩前 {limit} 章，"
            f"未进入法典：第{omitted[0].index}–{omitted[-1].index}章")
        chapters = chapters[:limit]

    volumes = []
    for i in range(0, len(chapters), chapters_per_volume):
        volumes.append(Volume(len(volumes) + 1, chapters[i:i + chapters_per_volume]))
    return volumes, warnings


def _trim(text: str, limit: int, marker: str) -> str:
    keep = max(0, limit - len(marker))
    cut = text[:keep]
    nl = cut.rfind("\n")
    if nl > keep // 2:
        cut = cut[:nl]
    return cut.rstrip() + marker


class Summarizer:
    def __init__(self, llm: Any, workdir: str | Path | None = None, *,
                 limit: int = 9000, chapters_per_volume: int = 15,
                 max_volumes: int = 60, progress: Progress | None = None,
                 resume: bool = False):
        self.llm = llm
        self.workdir = Path(workdir) if workdir else None
        self.limit = limit
        self.chapters_per_volume = chapters_per_volume
        self.max_volumes = max_volumes
        self.progress = progress or (lambda _m: None)
        self.resume = resume

    # -- 缓存 ------------------------------------------------------------
    def _volume_path(self, index: int) -> Path | None:
        if self.workdir is None:
            return None
        return self.workdir / f"volume_{index:03d}.json"

    def _load_digest(self, vol: Volume, source_hash: str) -> VolumeDigest | None:
        p = self._volume_path(vol.index)
        if not self.resume or p is None or not p.is_file():
            return None
        try:
            data = json.loads(p.read_text(encoding="utf-8-sig"))
            if (data.get("source_hash") != source_hash
                    or data.get("start") != vol.start or data.get("end") != vol.end):
                return None
            return VolumeDigest.model_validate(data["digest"])
        except Exception:  # noqa: BLE001 - 缓存坏了就重生成
            return None

    def _save_digest(self, vol: Volume, source_hash: str) -> None:
        p = self._volume_path(vol.index)
        if p is None or vol.digest is None:
            return
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps({
            "source_hash": source_hash, "index": vol.index,
            "start": vol.start, "end": vol.end,
            "digest": vol.digest.model_dump(mode="json"),
        }, ensure_ascii=False, indent=2), encoding="utf-8")

    # -- 生成 ------------------------------------------------------------
    def _summarize(self, vol: Volume, source_hash: str) -> VolumeDigest:
        cached = self._load_digest(vol, source_hash)
        if cached is not None:
            self.progress(f"  复用卷摘要 {vol.index}/{vol.title}")
            vol.digest = cached
            return cached
        self.progress(f"  压缩卷 {vol.index}/{self.max_volumes}（{vol.title}）…")
        payload = {
            "volume": vol.index,
            "chapter_range": [vol.chapters[0].index, vol.chapters[-1].index],
            "chapters": [{"title": c.title, "text": c.text} for c in vol.chapters],
        }
        vol.digest = self.llm.structured(
            "novel-volume", _SUMMARY_SYS, payload,
            model_cls=VolumeDigest, max_tokens=65536, temperature=0.2)
        self._save_digest(vol, source_hash)
        return vol.digest

    def _merge(self, volumes: list[Volume]) -> tuple[str, bool, list[str]]:
        warnings: list[str] = []
        parts = []
        for v in volumes:
            assert v.digest is not None
            parts.append(f"【{v.title}】\n{v.digest.events.strip()}")
            if v.digest.new_entities:
                parts.append("新增：" + "、".join(v.digest.new_entities))
        joined = "\n\n".join(parts)
        if len(joined) <= self.limit:
            return joined, False, warnings

        self.progress(f"  卷摘要合计 {len(joined)} 字，合并压缩到 {self.limit} 字以内…")
        merged = self.llm.chat(_MERGE_SYS, joined, json_mode=False,
                               max_tokens=65536, temperature=0.2).strip()
        if len(merged) <= self.limit:
            return merged, False, warnings

        marker = "\n……（合并后仍超长，后文已截断）"
        warnings.append(f"合并后 {len(merged)} 字仍超过 {self.limit} 字，已确定性截断")
        return _trim(merged, self.limit, marker), True, warnings

    def build(self, novel: Novel) -> Codex:
        text = novel.text
        if len(text) <= self.limit:
            return Codex(text=text, skipped=True)

        volumes, warnings = group_volumes(
            novel, chapters_per_volume=self.chapters_per_volume,
            max_volumes=self.max_volumes)
        truncated_by_volume = bool(warnings)
        for w in warnings:
            self.progress(f"  [WARN] {w}")

        source_hash = hashlib.sha1(text.encode("utf-8")).hexdigest()[:16]
        for v in volumes:
            self._summarize(v, source_hash)

        merged, trimmed, merge_warnings = self._merge(volumes)
        warnings.extend(merge_warnings)
        codex = Codex(text=merged, volumes=volumes,
                      truncated=truncated_by_volume or trimmed,
                      warnings=warnings)
        # P0：法典质量门——暴露悬空实体/事件链断裂
        codex_rep = validate_codex(codex)
        for w in codex_rep.warnings:
            codex.warnings.append(f"[codex] {w}")
        for e in codex_rep.errors:
            codex.warnings.append(f"[codex-err] {e}")
        return codex


def build_codex(novel: Novel, llm: Any, *, limit: int = 9000,
                chapters_per_volume: int = 15, max_volumes: int = 60,
                workdir: str | Path | None = None, resume: bool = False,
                progress: Progress | None = None) -> Codex:
    return Summarizer(llm, workdir, limit=limit,
                      chapters_per_volume=chapters_per_volume,
                      max_volumes=max_volumes, progress=progress,
                      resume=resume).build(novel)


_CODEX_ENT_MARK = re.compile(r"[「《]([^」》]{1,20})[」》]")


def validate_codex(codex: Codex) -> GateReport:
    """法典质量门（P0）：事件链完整 + 实体表有效 + 无悬空实体。

    - skipped（短篇）不参与校验；
    - 每卷事件链必须非空（事件链无断裂）；
    - 全局实体表（new_entities）必须登记了实体；
    - 卷事件里以「《》」标注的专名必须落在全局实体表内，否则为悬空实体。
    """
    from .validate import GateReport
    r = GateReport("codex")
    if codex.skipped or not codex.volumes:
        return r
    ents = codex.new_entities
    for v in codex.volumes:
        if v.digest and not (v.digest.events or "").strip():
            r.add(f"第 {v.index} 卷事件链为空（压缩断裂）")
    if not ents:
        r.add("法典未登记任何实体（new_entities 全空），压缩疑似失效或实体对齐缺失")
    if ents and len(ents) < 3:
        r.warn(f"法典仅登记 {len(ents)} 个实体，请复核是否遗漏人物/地点/道具")
    for v in codex.volumes:
        if not v.digest:
            continue
        for m in _CODEX_ENT_MARK.findall(v.digest.events):
            if m not in ents:
                r.warn(f"第 {v.index} 卷事件引用《{m}》未登记进法典实体表（悬空实体）")
    return r
