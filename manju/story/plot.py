# -*- coding: utf-8 -*-
"""情节驱动分集：先把小说切成有序情节原子，再由 LLM 按戏剧单元分集。

设计目标（对应“不是必须按 9000 字或 2 章分集”）：
1. **原子只用于定位**：原子是原文的连续区间，不改变正文；LLM 看到的是
   每个原子的一段摘录，返回的 atom_ids 直接映射回原文切片。
2. **按戏剧单元分集**：每集必须有明确的 dramatic_question / turn / climax /
   cliff，以及 2-6 条 must_show（必须拍出来的具体事件）。
3. **确定性兜底**：模型给的区间有跳段/重叠/漏尾时，修复为“从第一段到最后
   一段、每段恰好一次、顺序不减”的连续切片；宁可调边界，也不丢小说内容。

本模块不调用 LLM（只有校验/切片/修复）。
"""
from __future__ import annotations

import math
import re
from dataclasses import dataclass

from .episodes import EpisodeSlice
from .ingest import Novel
from .models import DesignEpisode, SeriesDesign
from .validate import GateReport

DEFAULT_ATOM_CHARS = 2400
DEFAULT_ATOM_EXCERPT_CHARS = 900
DEFAULT_MAX_ATOMS = 180
# 一次设计请求里所有原子摘录的总预算，避免长篇把上下文撑爆。
DEFAULT_TOTAL_EXCERPT_CHARS = 100_000
MIN_ATOM_CHARS = 400


_PREFIX_BOUNDARY = re.compile(r"[。！？!?…][”’\"']?\s*|\n")


def prefix_cut(text: str, limit: int, window: int = 400,
               look_back: int = 200) -> int:
    """把前缀硬截断挪到离 ``limit`` 最近的句末/换行。

    只生成前 N 集时，设计窗口若正好切在句子中间，最后一个情节单元会从
    半句话开始，模型只能猜。向前/向后各看一小段，选最近的边界；找不到
    就保持原上限，绝不为了找句号无限扩读。
    """
    text = text or ""
    if limit <= 0:
        return 0
    if limit >= len(text):
        return len(text)
    start = max(0, limit - max(0, look_back))
    end = min(len(text), limit + max(0, window))
    ends = [m.end() for m in _PREFIX_BOUNDARY.finditer(text, start, end)]
    if not ends:
        return limit
    # 距离相同优先向后，保证截断点落在边界之后。
    best = min(ends, key=lambda e: (abs(e - limit), 0 if e >= limit else 1))
    return min(len(text), best)


@dataclass(frozen=True)
class PlotAtom:
    """原文的连续区间；`index` 从 1 开始，供 LLM 引用。"""
    index: int
    start: int
    end: int
    chapters: tuple[int, ...]
    text: str

    @property
    def chars(self) -> int:
        return self.end - self.start

    def excerpt(self, limit: int) -> str:
        return _excerpt(self.text, limit)


def _excerpt(text: str, limit: int) -> str:
    text = (text or "").strip()
    if len(text) <= limit:
        return text
    head = max(1, limit * 3 // 5)
    tail = max(1, limit - head)
    return (text[:head].rstrip()
            + "\n……（原子中段省略）……\n"
            + text[-tail:].lstrip())


def _split_span(text: str, offset: int, budget: int) -> list[tuple[int, int]]:
    """把一段正文按 budget 切分，尽量在换行处收尾。"""
    budget = max(MIN_ATOM_CHARS, budget)
    spans: list[tuple[int, int]] = []
    pos, n = 0, len(text)
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


def _chapter_spans(novel: Novel, budget: int) -> list[tuple[int, int, tuple[int, ...]]]:
    spans: list[tuple[int, int, tuple[int, ...]]] = []
    for ch in novel.chapters:
        if ch.chars <= budget:
            spans.append((ch.start, ch.end, (ch.index,)))
            continue
        for s, e in _split_span(ch.text, ch.start, budget):
            spans.append((s, e, (ch.index,)))
    return spans


def _plain_spans(novel: Novel, budget: int) -> list[tuple[int, int, tuple[int, ...]]]:
    """无章节文本：按段落/换行累积到预算，避免一句话一个原子。"""
    return [(s, e, ()) for s, e in _split_span(novel.text, 0, budget)]


def _merge_adjacent(raw: list[tuple[int, int, tuple[int, ...]]],
                    group: int) -> list[tuple[int, int, tuple[int, ...]]]:
    if group <= 1:
        return raw
    out: list[tuple[int, int, tuple[int, ...]]] = []
    for i in range(0, len(raw), group):
        chunk = raw[i:i + group]
        chapters = tuple(sorted({c for _, _, chs in chunk for c in chs}))
        out.append((chunk[0][0], chunk[-1][1], chapters))
    return out


def build_plot_atoms(novel: Novel, *, atom_chars: int = DEFAULT_ATOM_CHARS,
                     excerpt_chars: int = DEFAULT_ATOM_EXCERPT_CHARS,
                     max_atoms: int = DEFAULT_MAX_ATOMS,
                     total_excerpt_chars: int = DEFAULT_TOTAL_EXCERPT_CHARS
                     ) -> list[PlotAtom]:
    """把整本小说切成有序原子（保持原文偏移，不重不漏）。"""
    budget = max(MIN_ATOM_CHARS, int(atom_chars))
    spans = (_chapter_spans(novel, budget) if novel.chaptered
             else _plain_spans(novel, budget))
    if not spans:
        spans = [(0, len(novel.text), ())]

    # 章节再短，也不无限切原子：按相邻组合并，直到低于上限。
    guard = 0
    while len(spans) > max(1, max_atoms) and guard < 20:
        guard += 1
        group = max(2, math.ceil(len(spans) / max(1, max_atoms)))
        spans = _merge_adjacent(spans, group)

    atoms: list[PlotAtom] = []
    for i, (s, e, chapters) in enumerate(spans, 1):
        atoms.append(PlotAtom(i, s, e, chapters, novel.text[s:e]))
    return atoms


def atom_excerpt_limit(atom_count: int, excerpt_chars: int,
                       total_chars: int) -> int:
    """给每个原子分配的摘录上限；长篇按总预算均摊。"""
    if atom_count <= 0:
        return max(200, excerpt_chars)
    per = min(int(excerpt_chars), max(240, int(total_chars) // atom_count))
    return max(240, per)


def design_payload(atoms: list[PlotAtom], *, seconds: float,
                   excerpt_chars: int,
                   total_excerpt_chars: int) -> list[dict]:
    per = atom_excerpt_limit(len(atoms), excerpt_chars, total_excerpt_chars)
    return [
        {
            "atom_id": a.index,
            "chapters": list(a.chapters),
            "chars": a.chars,
            "text": a.excerpt(per),
        }
        for a in atoms
    ]


# --------------------------------------------------------------------------
# LLM 输出的确定性修复
# --------------------------------------------------------------------------
def _event_sentences(text: str, limit: int = 4) -> list[str]:
    parts = re.split(r"(?<=[。！？!?；;])", (text or "").strip())
    out: list[str] = []
    for p in parts:
        p = p.strip(" \t\r\n。！？!?；;，,、")
        if len(p) >= 4 and p not in out:
            out.append(p)
        if len(out) >= limit:
            break
    return out


def _clean_events(values: list[str], *, fallback: list[str]) -> list[str]:
    out: list[str] = []
    for value in values or []:
        v = (value or "").strip()
        if not v or v in out:
            continue
        out.append(v[:80])
        if len(out) >= 6:
            break
    if not out:
        out = [v for v in fallback if v][:4]
    return out


def repair_design_in_place(design: SeriesDesign, atom_count: int
                           ) -> list[str]:
    """修复区间跳段/重叠/漏尾与空字段；返回人类可读的修复记录。"""
    fixes: list[str] = []
    eps = sorted(design.episodes, key=lambda e: e.ep)
    if not eps:
        return ["分集设计为空"]

    n = max(1, atom_count)
    ranges: list[list[int]] = []       # 0-based [start, end]
    prev_end = -1
    for i, d in enumerate(eps):
        ids = [i for i in (d.atom_ids or []) if 1 <= i <= n]
        declared_start = min(ids) - 1 if ids else prev_end + 1
        declared_end = max(ids) - 1 if ids else declared_start
        # 每集紧接上一集：模型跳段/重叠时，把空隙并给当前集，绝不丢原文。
        start = 0 if i == 0 else prev_end + 1
        if declared_start != start:
            fixes.append(f"第 {d.ep} 集起点 {declared_start + 1} → "
                         f"{start + 1}（补齐与上一集之间的原子）")
        end = max(declared_end, start)
        if end > n - 1:
            end = n - 1
        ranges.append([start, end])
        prev_end = end

    # 首尾兜底：任何小说内容都不能因为模型漏写而消失。
    if ranges and ranges[-1][1] != n - 1:
        fixes.append(f"最后一集终点 {ranges[-1][1] + 1} → {n}（补回结尾原子）")
        ranges[-1][1] = n - 1

    kept: list[tuple[DesignEpisode, list[int]]] = []
    for d, (s, e) in zip(eps, ranges):
        if e < s:
            fixes.append(f"第 {d.ep} 集没有可用原子，已删除")
            continue
        if d.ep != len(kept) + 1:
            fixes.append(f"集号 {d.ep} → {len(kept) + 1}（重排为连续集号）")
        kept.append((d, [s, e]))
    if not kept:
        kept = [(eps[0], [0, n - 1])]

    fixed_eps: list[DesignEpisode] = []
    for ep, (d, (s, e)) in enumerate(kept, 1):
        d = d.model_copy(deep=True)
        if d.ep != ep:
            d.ep = ep
        d.atom_ids = list(range(s + 1, e + 2))
        if not (d.title or "").strip():
            d.title = f"第{ep}集"
            fixes.append(f"第 {ep} 集标题为空，已补默认标题")
        fallback = _event_sentences(d.synopsis) or [
            (d.turn or "").strip(), (d.climax or "").strip(),
            (d.cliff or "").strip()]
        must = _clean_events(d.must_show, fallback=fallback)
        if must != d.must_show:
            fixes.append(f"第 {ep} 集 must_show 归一为 {len(must)} 条"
                         "（去重/去空/不足时从梗概拆事件）")
        d.must_show = must
        props: list[str] = []
        for p in d.key_props or []:
            p = (p or "").strip()
            if p and p not in props:
                props.append(p)
            if len(props) >= 3:
                break
        d.key_props = props
        lines: list[str] = []
        seen: set[str] = set()
        for kl in d.key_lines or []:
            kl = (kl or "").strip()
            text = kl.strip('“”"\'')
            if not text or text in seen:
                continue
            seen.add(text)
            lines.append(f"“{text}”")
            if len(lines) >= 4:
                break
        if lines != d.key_lines:
            fixes.append(f"第 {ep} 集 key_lines 归一为 {len(lines)} 句"
                         "（去重/去空/统一引号）")
        d.key_lines = lines
        if not (d.synopsis or "").strip():
            d.synopsis = "；".join(must)
            fixes.append(f"第 {ep} 集 synopsis 为空，已用 must_show 拼叙述")
        if not (d.dramatic_question or "").strip():
            d.dramatic_question = f"{d.title}：这一步会带来什么后果？"
        if not (d.turn or "").strip():
            d.turn = must[-1]
        if not (d.climax or "").strip():
            d.climax = must[-1]
        if not (d.hook or "").strip():
            d.hook = must[0]
        if not (d.cliff or "").strip():
            d.cliff = must[-1]
            fixes.append(f"第 {ep} 集 cliff 为空，已用最后一条 must_show 兜底")
        fixed_eps.append(d)

    design.episodes = fixed_eps
    return fixes


# --------------------------------------------------------------------------
# 校验 / 切片
# --------------------------------------------------------------------------
def validate_series_design(design: SeriesDesign, atom_count: int,
                           *, seconds: float = 90.0) -> GateReport:
    r = GateReport("series-design")
    eps = sorted(design.episodes, key=lambda e: e.ep)
    if [e.ep for e in eps] != list(range(1, len(eps) + 1)):
        r.add(f"集号必须从 1 连续：{[e.ep for e in eps]}")
    if not eps:
        r.add("至少需要 1 集")
        return r

    flattened: list[int] = []
    for d in eps:
        ids = list(d.atom_ids)
        if not ids:
            r.add(f"第 {d.ep} 集 atom_ids 为空")
            continue
        if ids != list(range(ids[0], ids[-1] + 1)):
            r.add(f"第 {d.ep} 集 atom_ids 必须是连续区间：{ids[:8]}…")
        if min(ids) < 1 or max(ids) > atom_count:
            r.add(f"第 {d.ep} 集 atom_ids 越界（共 {atom_count} 个原子）")
        flattened.extend(ids)
        limit = max(2, int(round(6 * max(seconds, 1) / 90)))
        if len(d.must_show) > limit:
            r.add(f"第 {d.ep} 集 must_show 有 {len(d.must_show)} 条，"
                  f"{seconds:g}s 最多装 {limit} 条；请拆集或删非关键情节")
        if len(d.must_show) < 2:
            r.warn(f"第 {d.ep} 集只有 {len(d.must_show)} 条 must_show，情节可能单薄")
        target_min = max(2, int(round(4 * max(seconds, 1) / 90)))
        if len(d.must_show) < target_min:
            r.warn(f"第 {d.ep} 集只有 {len(d.must_show)} 条 must_show；"
                   f"{seconds:g}s 建议 4-6 条因果相连的事件，否则叙事会发空")
        if len({m.strip() for m in d.must_show}) != len(d.must_show):
            r.add(f"第 {d.ep} 集 must_show 有重复事件")
        for m in d.must_show:
            if not 4 <= len(m.strip()) <= 80:
                r.add(f"第 {d.ep} 集 must_show 条目应为 4-80 字的可拍事件：{m!r}")
        if len(d.key_props) > 3:
            r.add(f"第 {d.ep} 集 key_props 最多 3 件")
        for kl in d.key_lines or []:
            text = kl.strip().strip('“”"\'')
            if not text:
                r.add(f"第 {d.ep} 集 key_lines 有空条目")
            elif len(text) > 60:
                r.add(f"第 {d.ep} 集 key_lines 台词过长（>60 字）：{text[:20]}…")
        if len(d.key_lines) > 4:
            r.add(f"第 {d.ep} 集 key_lines 最多 4 句")
        for field_name in ("title", "synopsis", "turn", "climax", "cliff"):
            if not getattr(d, field_name).strip():
                r.add(f"第 {d.ep} 集缺少 {field_name}")
        if not d.estimated_seconds or d.estimated_seconds <= 0:
            r.add(f"第 {d.ep} 集 estimated_seconds 必须 > 0")
        if d.estimated_seconds > 300:
            r.add(f"第 {d.ep} 集 estimated_seconds 超过 300s，短剧单集装不下")

    expect = list(range(1, atom_count + 1))
    if flattened != expect:
        if sorted(flattened) == expect:
            r.add("atom_ids 顺序错乱：必须按小说时间顺序覆盖，不能倒序/重排")
        else:
            missing = sorted(set(expect) - set(flattened))
            dup = sorted({i for i in flattened if flattened.count(i) > 1})
            r.add(f"atom_ids 必须恰好覆盖全部 {atom_count} 个原子各一次；"
                  f"缺失 {missing[:12]}，重复 {dup[:12]}")
    return r


def design_slices(novel: Novel, atoms: list[PlotAtom],
                  design: SeriesDesign) -> list[EpisodeSlice]:
    """把设计里的 atom 区间映射回原文切片（1-based atom → character span）。"""
    if not atoms:
        raise ValueError("没有情节原子")
    by_id = {a.index: a for a in atoms}
    slices: list[EpisodeSlice] = []
    for i, d in enumerate(sorted(design.episodes, key=lambda x: x.ep), 1):
        ids = [x for x in d.atom_ids if x in by_id]
        if not ids:
            raise ValueError(f"第 {d.ep} 集没有有效 atom_ids")
        first, last = by_id[min(ids)], by_id[max(ids)]
        chapters = tuple(sorted({c for x in ids for c in by_id[x].chapters}))
        slices.append(EpisodeSlice(
            index=i, start=first.start, end=last.end,
            chapter_indexes=chapters,
            text=novel.text[first.start:last.end]))
    return slices


def design_signature(design: SeriesDesign) -> str:
    """短指纹：集数 + 每集 atom 边界，供 resume 判断分集是否变化。"""
    import hashlib
    raw = "|".join(
        f"{d.ep}:{d.atom_ids[0] if d.atom_ids else 0}-"
        f"{d.atom_ids[-1] if d.atom_ids else 0}"
        for d in sorted(design.episodes, key=lambda x: x.ep))
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:12]
