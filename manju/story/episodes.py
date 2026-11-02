# -*- coding: utf-8 -*-
"""分集：优先按章节分组，字数只作为兜底与安全阀。

规则：
* **章节模式**（`chapters_per_episode=2/3/...`）：每 N 章一集；
  章节是作者按内容切的，通常比纯字数更贴戏。无章节文本自动回退字数模式。
* **字数模式**（未指定每几章一集）：集数 = ceil(总字数 / chars_per_episode)，
  优先落在章节边界；保留给无章节文本或显式 `episodes=N`。
* 章节模式也保留安全阀：一集超过 `chars_per_episode * 1.5` 时按段落拆开，
  过短（< 预算的 1/3，且不少于 1000 字）时与相邻集合并，避免 200 字一集。
* `episode_map` 可覆盖自动结果；显式 `episodes=N` 与章节模式互斥。
* 切片写到 `state/story/ep_src_NNN.txt`，各集文本首尾相接、不重叠。

本模块不调用 LLM。
"""
from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass
from pathlib import Path

from .ingest import Novel

DEFAULT_CHARS_PER_EPISODE = 9000
DEFAULT_MAX_AUTO_EPISODES = 12


class EpisodeError(ValueError):
    """分集参数或 ep map 非法。"""


@dataclass(frozen=True)
class EpisodeSlice:
    index: int
    start: int
    end: int
    chapter_indexes: tuple[int, ...]
    text: str

    @property
    def chars(self) -> int:
        return self.end - self.start


@dataclass
class _Atom:
    start: int
    end: int
    chapters: tuple[int, ...]

    @property
    def chars(self) -> int:
        return self.end - self.start


def auto_episode_count(total_chars: int,
                       chars_per_episode: int = DEFAULT_CHARS_PER_EPISODE) -> int:
    if chars_per_episode <= 0:
        raise EpisodeError("chars_per_episode 必须 > 0")
    if total_chars <= 0:
        raise EpisodeError("正文为空")
    return max(1, math.ceil(total_chars / chars_per_episode))


def needs_auto_confirmation(count: int,
                            max_auto: int = DEFAULT_MAX_AUTO_EPISODES) -> bool:
    return count > max_auto


# --------------------------------------------------------------------------
# 切分原子
# --------------------------------------------------------------------------
def _split_text(text: str, offset: int, budget: int) -> list[tuple[int, int]]:
    """把 [offset, offset+len(text)) 切成约 budget 长的段，尽量在换行处收尾。"""
    budget = max(1, budget)
    spans: list[tuple[int, int]] = []
    pos = 0
    n = len(text)
    while pos < n:
        target = min(pos + budget, n)
        if target >= n:
            spans.append((offset + pos, offset + n))
            break
        nl = text.rfind("\n", pos + budget // 2, target)
        end = nl + 1 if nl > pos else target
        spans.append((offset + pos, offset + end))
        pos = end
    return spans


def _atoms_from_novel(novel: Novel, budget: int) -> list[_Atom]:
    if novel.chaptered:
        atoms: list[_Atom] = []
        for c in novel.chapters:
            if c.chars > budget:
                for s, e in _split_text(c.text, c.start, budget):
                    atoms.append(_Atom(s, e, (c.index,)))
            else:
                atoms.append(_Atom(c.start, c.end, (c.index,)))
        return atoms

    # 无章节：直接按段落/换行切，避免 ingest 的 500 字重叠被带进分集
    atoms = []
    pos = 0
    for m in re.finditer(r"\n\s*\n|\n", novel.text):
        if m.end() > pos:
            atoms.append(_Atom(pos, m.end(), ()))
        pos = m.end()
    if pos < len(novel.text):
        atoms.append(_Atom(pos, len(novel.text), ()))
    return atoms or [_Atom(0, len(novel.text), ())]


def _ensure_atom_count(text: str, atoms: list[_Atom], count: int) -> list[_Atom]:
    """原子数不够时，从最大的原子开始对半拆，直到 ≥ count。"""
    atoms = list(atoms)
    guard = 0
    while len(atoms) < count:
        guard += 1
        if guard > 100_000:
            raise EpisodeError("无法切出足够的片段")
        idx = max(range(len(atoms)), key=lambda i: atoms[i].chars)
        a = atoms[idx]
        if a.chars < 2:
            raise EpisodeError(f"正文太短，无法切成 {count} 集")
        spans = _split_text(text[a.start:a.end], a.start, max(1, a.chars // 2))
        if len(spans) < 2:                     # 理论兜底：硬切
            mid = a.start + a.chars // 2
            spans = [(a.start, mid), (mid, a.end)]
        atoms[idx:idx + 1] = [_Atom(s, e, a.chapters) for s, e in spans]
    return atoms


def _boundary_atoms(atoms: list[_Atom], count: int) -> list[int]:
    """返回每集最后一个原子的下标（共 count 个）。"""
    m = len(atoms)
    if m < count:
        raise EpisodeError(f"原子数 {m} 少于集数 {count}")
    total_start = atoms[0].start
    total = atoms[-1].end - total_start
    bounds: list[int] = []
    lo = 0
    for k in range(1, count):
        target = total_start + total * k / count
        hi = m - (count - k)                   # 后面每集至少留一个原子
        if lo >= hi:
            raise EpisodeError("分集边界不足")
        j = min(range(lo, hi), key=lambda i: abs(atoms[i].end - target))
        bounds.append(j)
        lo = j + 1
    bounds.append(m - 1)
    return bounds


# --------------------------------------------------------------------------
# 分集
# --------------------------------------------------------------------------
def _merge_short_tail(slices: list[EpisodeSlice], novel: Novel,
                      min_chars: int) -> list[EpisodeSlice]:
    """末尾不足 min_chars 时并入上一集。

    章节边界是很粗的原子：若最后一章很短（或前面是一整章超大章），
    按边界切会得到几百字的“独立一集”，却仍要按 90 秒写剧本，
    只能靠模型硬编。这里直接并回上一集，保正文首尾相接。
    """
    out = list(slices)
    while len(out) > 1 and out[-1].chars < min_chars:
        prev, tail = out[-2], out[-1]
        merged = EpisodeSlice(
            index=prev.index, start=prev.start, end=tail.end,
            chapter_indexes=tuple(sorted(set(prev.chapter_indexes)
                                         | set(tail.chapter_indexes))),
            text=novel.text[prev.start:tail.end])
        out[-2:] = [merged]
    return out


def _split_by_chapters(novel: Novel, *, chapters_per_episode: int,
                       chars_per_episode: int) -> list[EpisodeSlice]:
    """每 N 章一集；超长组按段落拆、过短组与相邻集合并。"""
    if chapters_per_episode < 1:
        raise EpisodeError("chapters_per_episode 必须 ≥ 1")
    if not novel.chaptered:
        raise EpisodeError("未识别到章节，无法按章节分集；请改用字数分集或 episode_map")

    max_chars = max(1, int(chars_per_episode * 1.5))
    min_chars = max(1000, chars_per_episode // 3)
    groups = [list(novel.chapters[i:i + chapters_per_episode])
              for i in range(0, len(novel.chapters), chapters_per_episode)]

    raw: list[EpisodeSlice] = []
    for group in groups:
        start, end = group[0].start, group[-1].end
        text = novel.text[start:end]
        if len(text) <= max_chars:
            raw.append(EpisodeSlice(
                0, start, end, tuple(c.index for c in group), text))
            continue
        # 一章或几章加起来超预算：按段落拆，仍保留章节归属；
        # 同一章可能跨两集（chapter_indexes 会重复），这是允许的。
        for s, e in _split_text(text, start, max_chars):
            chs = tuple(c.index for c in group
                        if c.end > s and c.start < e)
            raw.append(EpisodeSlice(0, s, e, chs, novel.text[s:e]))

    # 过短组与相邻合并：先向后并，尾部再向前并；合并后不得超过 max_chars。
    merged: list[EpisodeSlice] = []
    for sl in raw:
        if (merged and merged[-1].chars < min_chars
                and merged[-1].chars + sl.chars <= max_chars):
            prev = merged[-1]
            merged[-1] = EpisodeSlice(
                0, prev.start, sl.end,
                tuple(sorted(set(prev.chapter_indexes) | set(sl.chapter_indexes))),
                novel.text[prev.start:sl.end])
        else:
            merged.append(sl)
    while len(merged) > 1 and merged[-1].chars < min_chars:
        prev, tail = merged[-2], merged[-1]
        merged[-2:] = [EpisodeSlice(
            0, prev.start, tail.end,
            tuple(sorted(set(prev.chapter_indexes) | set(tail.chapter_indexes))),
            novel.text[prev.start:tail.end])]

    return [EpisodeSlice(i + 1, s.start, s.end, s.chapter_indexes, s.text)
            for i, s in enumerate(merged)]


def split_episodes(novel: Novel, *, chars_per_episode: int = DEFAULT_CHARS_PER_EPISODE,
                   episodes: int | None = None,
                   episode_map: str | Path | None = None,
                   chapters_per_episode: int | None = None) -> list[EpisodeSlice]:
    if episode_map:
        return load_episode_map(episode_map, novel)
    if chapters_per_episode:
        if episodes is not None:
            raise EpisodeError(
                "episodes 与 chapters_per_episode 不能同时指定："
                "按章分集时集数由章节数决定；要固定总集数请用 --episodes")
        return _split_by_chapters(
            novel, chapters_per_episode=chapters_per_episode,
            chars_per_episode=chars_per_episode)

    count = (episodes if episodes is not None
             else auto_episode_count(novel.total_chars, chars_per_episode))
    if count < 1:
        raise EpisodeError("集数必须 ≥ 1")
    if count > novel.total_chars:
        raise EpisodeError(f"集数 {count} 大于总字数 {novel.total_chars}")

    if count == 1:
        return [EpisodeSlice(1, 0, novel.total_chars,
                             tuple(c.index for c in novel.chapters), novel.text)]

    atoms = _ensure_atom_count(
        novel.text, _atoms_from_novel(novel, chars_per_episode), count)
    bounds = _boundary_atoms(atoms, count)

    slices: list[EpisodeSlice] = []
    prev = 0
    for ep, last in enumerate(bounds, 1):
        group = atoms[prev:last + 1]
        start, end = group[0].start, group[-1].end
        chapters = tuple(sorted({c for a in group for c in a.chapters}))
        slices.append(EpisodeSlice(ep, start, end, chapters, novel.text[start:end]))
        prev = last + 1
    if episodes is None:  # 仅自动分集：显式集数由用户说了算
        slices = _merge_short_tail(
            slices, novel, max(1000, chars_per_episode // 5))
    return slices


# --------------------------------------------------------------------------
# 人工覆盖
# --------------------------------------------------------------------------
def _map_items(data) -> list[dict]:
    if isinstance(data, dict) and "episodes" in data:
        data = data["episodes"]
    if isinstance(data, dict):
        items = []
        for key, value in data.items():
            item = dict(value) if isinstance(value, dict) else {"range": value}
            item.setdefault("ep", key)
            items.append(item)
        return items
    if isinstance(data, list):
        return [dict(x) for x in data]
    raise EpisodeError("episode_map 必须是对象或数组")


def load_episode_map(path: str | Path, novel: Novel) -> list[EpisodeSlice]:
    p = Path(path)
    try:
        data = json.loads(p.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError) as e:
        raise EpisodeError(f"读不了 episode_map {p}：{e}") from e

    slices: list[EpisodeSlice] = []
    try:
        for item in _map_items(data):
            ep = int(item["ep"])
            if "chapters" in item:
                a, b = item["chapters"]
                first, last = novel.chapters[int(a) - 1], novel.chapters[int(b) - 1]
                start, end = first.start, last.end
            elif "chapter_range" in item:
                a, b = item["chapter_range"]
                first, last = novel.chapters[int(a) - 1], novel.chapters[int(b) - 1]
                start, end = first.start, last.end
            elif "range" in item:
                start, end = item["range"]
            else:
                start, end = item["start"], item["end"]
            start, end = int(start), int(end)
            if ep < 1 or start < 0 or end <= start or end > novel.total_chars:
                raise EpisodeError(f"episode_map 第 {ep} 集范围非法：{start}-{end}")
            chapters = tuple(c.index for c in novel.chapters
                             if c.end > start and c.start < end)
            slices.append(EpisodeSlice(ep, start, end, chapters,
                                       novel.text[start:end]))
    except EpisodeError:
        raise
    except (KeyError, TypeError, ValueError, IndexError) as e:
        raise EpisodeError(f"episode_map 格式错误：{e}") from e

    if not slices:
        raise EpisodeError("episode_map 为空")
    slices.sort(key=lambda s: (s.start, s.index))
    for a, b in zip(slices, slices[1:]):
        if b.start < a.end:
            raise EpisodeError(f"episode_map 第 {a.index}/{b.index} 集文本重叠")
    if len({s.index for s in slices}) != len(slices):
        raise EpisodeError("episode_map 集号重复")
    return slices


def write_slices(slices: list[EpisodeSlice], workdir: str | Path,
                 prefix: str = "ep_src") -> list[Path]:
    d = Path(workdir)
    d.mkdir(parents=True, exist_ok=True)
    paths = []
    for s in slices:
        p = d / f"{prefix}_{s.index:03d}.txt"
        p.write_text(s.text.rstrip() + "\n", encoding="utf-8", newline="\n")
        paths.append(p)
    return paths
