# -*- coding: utf-8 -*-
"""小说解析层：编码、章节正则、无章节切段、章节表/CLI。

纯确定性，不调用 LLM。
"""
from __future__ import annotations

import json

import pytest

from manju.story import ingest

BODY = "阿禾提着旧皮箱走过湿木栈桥，雾从河面漫上来。\n"


def _write(tmp_path, text: str, encoding: str):
    p = tmp_path / f"novel-{encoding}.txt"
    p.write_bytes(text.encode(encoding))
    return p


# --------------------------------------------------------------------------
# 编码
# --------------------------------------------------------------------------
@pytest.mark.parametrize("encoding,label", [
    ("utf-8", "UTF-8"),
    ("utf-8-sig", "UTF-8-BOM"),
    ("gbk", "GBK"),
    ("utf-16", "UTF-16LE"),
    ("utf-16-le", "UTF-16LE"),     # 无 BOM：靠 NUL 分布判断
])
def test_encoding_detection(tmp_path, encoding, label):
    text = "第一章 渡口\n" + BODY * 40
    novel = ingest.parse(_write(tmp_path, text, encoding))
    assert novel.encoding == label
    assert novel.text.startswith("第一章 渡口")
    assert len(novel.chapters) == 1


def test_empty_file_is_rejected(tmp_path):
    p = tmp_path / "empty.txt"
    p.write_bytes(b"")
    with pytest.raises(ingest.IngestError):
        ingest.parse(p)


def test_binary_junk_is_rejected(tmp_path):
    p = tmp_path / "junk.txt"
    p.write_bytes(bytes(range(256)) * 20)
    with pytest.raises(ingest.IngestError):
        ingest.parse(p)


# --------------------------------------------------------------------------
# 章节正则
# --------------------------------------------------------------------------
def test_all_heading_forms_are_recognised():
    text = """第一章 少年
正文一。
第二回 夜行
正文二。
第三节：归来
正文三。
第一卷 风起
正文四。
楔子
正文五。
序章 旧事
正文六。
尾声
正文七。
番外一
正文八。
Chapter 3 The End
正文九。
"""
    novel = ingest.parse_text(text)
    assert novel.chaptered
    assert len(novel.chapters) == 9
    kinds = [c.kind for c in novel.chapters]
    assert kinds == ["chapter", "chapter", "chapter", "volume",
                     "prologue", "prologue", "epilogue", "extra", "chapter"]
    assert novel.chapters[-1].title.startswith("Chapter 3")


def test_offsets_are_exact():
    text = "第一章 甲\n" + BODY + "第二章 乙\n" + BODY
    novel = ingest.parse_text(text)
    for c in novel.chapters:
        assert novel.text[c.start:c.end] == c.text
    assert novel.chapters[0].end == novel.chapters[1].start


def test_preamble_before_first_heading_is_kept():
    text = "书名：渡口\n作者：某人\n\n第一章 开始\n正文。\n"
    novel = ingest.parse_text(text)
    assert novel.chapters[0].kind == "preamble"
    assert "书名：渡口" in novel.chapters[0].text
    assert novel.chapters[1].kind == "chapter"
    assert len(novel.chapters) == 2


def test_prose_line_starting_with_number_is_not_a_heading():
    text = ("第一章 开始\n" + BODY
            + "第三章的内容不能删。\n" + BODY)
    novel = ingest.parse_text(text)
    assert len(novel.chapters) == 1


def test_heading_mid_line_is_not_a_heading():
    text = "第一章 开始\n他说第三章的内容不能删。\n"
    novel = ingest.parse_text(text)
    assert len(novel.chapters) == 1


# --------------------------------------------------------------------------
# 无章节：2 万字 + 500 字重叠
# --------------------------------------------------------------------------
def test_fallback_chunking_with_overlap():
    text = "甲" * 45_000
    novel = ingest.parse_text(text)
    assert not novel.chaptered
    assert len(novel.chapters) == 3
    assert novel.chapters[0].start == 0
    assert novel.chapters[-1].end == len(text)
    assert novel.chapters[1].start == novel.chapters[0].end - 500
    assert novel.chapters[2].start == novel.chapters[1].end - 500
    for a, b in zip(novel.chapters, novel.chapters[1:]):
        assert b.start < a.end                     # 确实重叠
    for c in novel.chapters:
        assert c.chars <= 20_000


def test_chunk_boundary_prefers_newline():
    text = ("甲乙丙丁" * 4_975) + "\n" + ("戊己庚辛" * 10_000)
    novel = ingest.parse_text(text)
    first = novel.chapters[0]
    assert first.text.endswith("\n")
    assert first.end < 20_000


def test_60_chapters_300k_counts_60():
    chapters = []
    for i in range(1, 61):
        head = f"第{i}章 渡口夜话\n"
        chapters.append(head + "字" * (5_000 - len(head) - 1) + "\n")
    text = "".join(chapters)
    assert len(text) == 300_000
    novel = ingest.parse_text(text)
    assert novel.chaptered
    assert len(novel.chapters) == 60
    assert all(c.kind == "chapter" for c in novel.chapters)
    assert novel.chapters[-1].end == len(text)


def test_table_lists_every_chapter():
    novel = ingest.parse_text("第一章 甲\n" + BODY + "第二章 乙\n" + BODY)
    table = novel.table()
    assert "第一章 甲" in table and "第二章 乙" in table
    assert "章" in table


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------
def test_cli_list_chapters_and_dry_run(tmp_path, capsys):
    p = _write(tmp_path, "第一章 甲\n" + BODY + "第二章 乙\n" + BODY, "utf-8")
    assert ingest.main([str(p), "--list-chapters", "--dry-run"]) == 0
    out = capsys.readouterr().out
    assert "UTF-8" in out and "第二章 乙" in out


def test_cli_json_has_no_body_text(tmp_path, capsys):
    p = _write(tmp_path, "第一章 甲\n" + BODY, "utf-8")
    assert ingest.main([str(p), "--json"]) == 0
    data = json.loads(capsys.readouterr().out)
    assert data["chapters"][0]["title"] == "第一章 甲"
    assert "text" not in data["chapters"][0]


@pytest.mark.skip(reason="依赖完整流水线的 manju.cli（novel 子命令）；本输入层包仅提供精简的 manju.story_cli（--list-chapters/--dry-run 等价行为在 story_cli 下已覆盖）")
def test_novel_list_chapters_exits_before_llm(root, capsys):
    """--list-chapters / --dry-run 必须在连模型之前返回。"""
    from manju.cli import main

    p = root / "story.txt"
    p.write_bytes(("第一章 甲\n" + BODY).encode("gbk"))
    assert main(["novel", str(p), "--list-chapters"]) == 0
    out = capsys.readouterr().out
    assert "第一章 甲" in out and "GBK" in out

    assert main(["novel", str(p), "--dry-run"]) == 0
    assert "DRY-RUN" in capsys.readouterr().out
