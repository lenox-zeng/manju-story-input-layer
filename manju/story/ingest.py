# -*- coding: utf-8 -*-
"""小说解析层：编码识别 → 章节切分 → 章节表。

只做确定性的事，不调用 LLM：

1. 编码：UTF-8 / UTF-8 BOM / UTF-16（含无 BOM 的启发式）/ GBK(GB18030)。
2. 章节：第X章/回/节/卷/部/篇/集、楔子、序章、尾声、番外、Chapter N。
3. 无章节：按 `chunk_size`（默认 2 万字）+ `overlap`（默认 500 字）切段，
   并尽量在换行处收尾，避免把一句话劈开。

本模块**从不写盘**；CLI 的 `--dry-run` 因此天然成立。
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import dataclass
from pathlib import Path

from .clean import CleanReport, clean_text, cjk_chars, content_chars

DEFAULT_CHUNK = 20_000
DEFAULT_OVERLAP = 500
MAX_HEADING_LEN = 60

_KIND_LABEL = {
    "preamble": "前言",
    "chapter": "章",
    "volume": "卷",
    "prologue": "序",
    "epilogue": "尾声",
    "extra": "番外",
    "chunk": "切段",
}

_CN_NUM = r"[0-9０-９零〇一二三四五六七八九十百千万两]+"

_HEADING_RE = re.compile(
    r"^\s*(?P<head>"
    rf"第\s*{_CN_NUM}\s*[章回节卷部篇集]"
    r"|楔子|序章|序言|序幕|引子|前言|尾声|终章|后记"
    r"|番外(?:篇)?(?:\s*[0-9０-９零〇一二三四五六七八九十百]+)?"
    r"|(?:chapter|part|volume)\s+[0-9ivxlcdm]+"
    r")(?P<rest>.*)$",
    re.IGNORECASE,
)

_SENTENCE_END = re.compile(r"[。！？!?；;]\s*$")
_SEPARATOR = re.compile(r"^[\s:：、.．·—\-]")


class IngestError(ValueError):
    """输入文件无法解析（编码、空文件、二进制）。"""


@dataclass(frozen=True)
class Chapter:
    index: int                 # 1 起
    title: str                 # 标题行原文（strip 后）
    start: int                 # 在全文中的字符偏移
    end: int
    kind: str                  # chapter/volume/prologue/epilogue/extra/preamble/chunk
    text: str

    @property
    def chars(self) -> int:
        return self.end - self.start

    @property
    def label(self) -> str:
        return _KIND_LABEL.get(self.kind, self.kind)


@dataclass(frozen=True)
class Novel:
    path: str
    encoding: str
    text: str
    chapters: tuple[Chapter, ...]
    chaptered: bool            # False = 无章节，按字数切段
    cleanup: CleanReport | None = None

    @property
    def total_chars(self) -> int:
        """原始字符数（含换行/空格），用于切片偏移与阈值判断。"""
        return len(self.text)

    @property
    def content_chars(self) -> int:
        """去空白后的有效字符数，用于对外展示“小说多少字”。"""
        return content_chars(self.text)

    @property
    def cjk_chars(self) -> int:
        """纯汉字数量。"""
        return cjk_chars(self.text)

    def table(self) -> str:
        rows = [f"  #  类型    字数      起止              标题"]
        for c in self.chapters:
            rows.append(f"{c.index:>3}  {c.label:<4} {c.chars:>7}  "
                        f"{c.start:>7}-{c.end:<7}  {c.title}")
        return "\n".join(rows)

    def to_dict(self, *, with_text: bool = False) -> dict:
        chapters = []
        for c in self.chapters:
            item = {"index": c.index, "title": c.title, "kind": c.kind,
                    "start": c.start, "end": c.end, "chars": c.chars}
            if with_text:
                item["text"] = c.text
            chapters.append(item)
        data = {"path": self.path, "encoding": self.encoding,
                "total_chars": self.total_chars,
                "content_chars": self.content_chars,
                "cjk_chars": self.cjk_chars,
                "chaptered": self.chaptered, "chapters": chapters}
        if self.cleanup is not None:
            data["cleanup"] = self.cleanup.to_dict()
        return data


# --------------------------------------------------------------------------
# 编码
# --------------------------------------------------------------------------
def _utf16_hint(data: bytes) -> str | None:
    """无 BOM 的 UTF-16：文本里会出现大量 0x00（ASCII/换行的一半）。"""
    sample = data[:4096]
    if len(sample) < 8:
        return None
    nuls = sample.count(0)
    if nuls < max(4, len(sample) // 200):      # 纯中文 UTF-16 的 NUL 很少，但不会为零
        return None
    even = sum(1 for i in range(0, len(sample), 2) if sample[i] == 0)
    odd = sum(1 for i in range(1, len(sample), 2) if sample[i] == 0)
    if odd > even * 3:
        return "utf-16-le"
    if even > odd * 3:
        return "utf-16-be"
    return None


def decode_bytes(data: bytes) -> tuple[str, str]:
    """返回 (文本, 编码标签)。标签用于日志，不保证是 Python codec 名。"""
    if not data:
        raise IngestError("文件是空的")
    if data.startswith(b"\xef\xbb\xbf"):
        return data.decode("utf-8-sig"), "UTF-8-BOM"
    if data.startswith(b"\xff\xfe"):
        return data.decode("utf-16"), "UTF-16LE"
    if data.startswith(b"\xfe\xff"):
        return data.decode("utf-16"), "UTF-16BE"

    hint = _utf16_hint(data)
    if hint:
        return data.decode(hint), "UTF-16LE" if hint.endswith("le") else "UTF-16BE"

    for encoding, label in (("utf-8", "UTF-8"), ("gb18030", "GBK")):
        try:
            text = data.decode(encoding)
        except UnicodeDecodeError:
            continue
        if "\x00" in text:
            break
        return text, label
    raise IngestError("无法识别编码（已尝试 UTF-8 / UTF-16 / GBK）；"
                      "请另存为 UTF-8 后重试")


def read_text(path: str | Path) -> tuple[str, str]:
    p = Path(path)
    try:
        data = p.read_bytes()
    except OSError as e:
        raise IngestError(f"读不了 {p}：{e}") from e
    return decode_bytes(data)


# --------------------------------------------------------------------------
# 章节
# --------------------------------------------------------------------------
def _classify(head: str) -> str:
    h = head.strip().lower()
    if h.startswith("番外"):
        return "extra"
    if h.startswith(("楔子", "序章", "序言", "序幕", "引子", "前言")):
        return "prologue"
    if h.startswith(("尾声", "终章", "后记")):
        return "epilogue"
    if re.search(r"[卷部篇集]$", h):
        return "volume"
    return "chapter"


def _is_heading(line: str) -> bool:
    line = line.strip()
    if not line or len(line) > MAX_HEADING_LEN or _SENTENCE_END.search(line):
        return False
    m = _HEADING_RE.match(line)
    if not m:
        return False
    rest = m.group("rest").strip()
    if not rest:
        return True
    # “第一章 少年” / “第一章：少年” 这类带分隔符的标题
    if _SEPARATOR.match(m.group("rest")):
        return True
    # 无分隔符时只接受很短的标题，降低把正文行误判成标题的概率
    return len(rest) <= 12 and not re.search(r"[，。！？；]", rest)


def split_chapters(text: str, *, chunk_size: int = DEFAULT_CHUNK,
                   overlap: int = DEFAULT_OVERLAP) -> tuple[list[Chapter], bool]:
    """返回 (章节列表, 是否识别到章节标题)。"""
    if chunk_size <= 0:
        raise IngestError("chunk_size 必须 > 0")
    if overlap < 0 or overlap >= chunk_size:
        raise IngestError("overlap 必须 ≥ 0 且 < chunk_size")

    marks: list[tuple[int, str]] = []          # (行首偏移, 标题行)
    offset = 0
    for raw in text.splitlines(keepends=True):
        line = raw.rstrip("\r\n")
        if _is_heading(line):
            marks.append((offset, line.strip()))
        offset += len(raw)

    if not marks:
        return _chunk(text, chunk_size, overlap), False

    chapters: list[Chapter] = []
    if text[:marks[0][0]].strip():
        pre = text[:marks[0][0]]
        chapters.append(Chapter(1, "首个标题之前", 0, marks[0][0], "preamble", pre))

    for i, (start, title) in enumerate(marks):
        end = marks[i + 1][0] if i + 1 < len(marks) else len(text)
        m = _HEADING_RE.match(title)
        kind = _classify(m.group("head") if m else title)
        chapters.append(Chapter(len(chapters) + 1, title, start, end, kind,
                                text[start:end]))
    return chapters, True


def _chunk(text: str, size: int, overlap: int) -> list[Chapter]:
    out: list[Chapter] = []
    start = 0
    n = len(text)
    while start < n:
        end = min(start + size, n)
        if end < n:
            # 在目标终点前 800 字内找最后一个换行，别把句子劈开
            nl = text.rfind("\n", max(start + size - 800, start), end)
            if nl > start + size // 2:
                end = nl + 1
        text_part = text[start:end]
        out.append(Chapter(len(out) + 1, f"第 {len(out) + 1} 段（无章节）",
                           start, end, "chunk", text_part))
        if end >= n:
            break
        start = max(end - overlap, start + 1)
    return out


def parse_text(text: str, *, source: str = "<memory>", encoding: str = "UTF-8",
               chunk_size: int = DEFAULT_CHUNK,
               overlap: int = DEFAULT_OVERLAP,
               cleanup: bool = True) -> Novel:
    if not (text or "").strip():
        raise IngestError("小说正文为空")
    report: CleanReport | None = None
    if cleanup:
        text, report = clean_text(text)
    chapters, chaptered = split_chapters(text, chunk_size=chunk_size, overlap=overlap)
    return Novel(source, encoding, text, tuple(chapters), chaptered, report)


def parse(path: str | Path, *, chunk_size: int = DEFAULT_CHUNK,
          overlap: int = DEFAULT_OVERLAP, cleanup: bool = True) -> Novel:
    text, encoding = read_text(path)
    return parse_text(text, source=str(path), encoding=encoding,
                      chunk_size=chunk_size, overlap=overlap, cleanup=cleanup)


# --------------------------------------------------------------------------
# CLI（供 novel --list-chapters 复用）
# --------------------------------------------------------------------------
def main(argv: list[str] | None = None) -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:  # noqa: BLE001 - 老终端不支持就算了
        pass
    p = argparse.ArgumentParser(
        prog="python -m manju.story.ingest",
        description="小说编码识别与章节切分（只读，不写盘）")
    p.add_argument("novel", help="小说文本文件")
    p.add_argument("--list-chapters", action="store_true", help="打印章节表")
    p.add_argument("--dry-run", action="store_true",
                   help="只解析并打印计划；本命令从不写盘")
    p.add_argument("--json", action="store_true", help="输出 JSON（不含正文）")
    p.add_argument("--chunk-size", type=int, default=DEFAULT_CHUNK,
                   help=f"无章节时的切段字数（默认 {DEFAULT_CHUNK}）")
    p.add_argument("--overlap", type=int, default=DEFAULT_OVERLAP,
                   help=f"无章节时的重叠字数（默认 {DEFAULT_OVERLAP}）")
    args = p.parse_args(argv)

    try:
        novel = parse(args.novel, chunk_size=args.chunk_size, overlap=args.overlap)
    except IngestError as e:
        print(f"[FAIL] {e}", file=sys.stderr)
        return 2

    if args.json:
        print(json.dumps(novel.to_dict(), ensure_ascii=False, indent=2))
        return 0

    mode = "章节" if novel.chaptered else "无章节，按字数切段"
    print(f"[OK] {novel.path}")
    print(f"  编码：{novel.encoding}   总字数：{novel.total_chars}   "
          f"切分：{mode}   段数：{len(novel.chapters)}")
    if args.list_chapters or args.dry_run:
        print(novel.table())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
