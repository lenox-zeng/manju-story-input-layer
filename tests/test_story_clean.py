# -*- coding: utf-8 -*-
"""小说预处理：广告/网址清理、格式归一化、字数口径、乱码告警。"""
from __future__ import annotations

from manju.story.clean import cjk_chars, clean_text, content_chars
from manju.story.ingest import parse_text


def test_cleanup_removes_ads_urls_and_normalizes_format():
    text = (
        "书名：测试\r\n\r\n"
        "第一章 开始\r\n"
        "他投进了首发小前锋的位置。\r\n"
        "请收藏本站 www.example.com\r\n"
        "http://ads.example.com\r\n"
        "请记住本站，无弹窗全文阅读\r\n"
        "正文  有   多余空格。\r\n"
        "他 说 道 ： “ 好 ” 。\r\n"
        "......\r\n"
        "第二章 继续\r\n"
        "正文二。\r\n"
    )
    cleaned, report = clean_text(text)

    assert "首发小前锋" in cleaned              # 正文里的“首发”不能被误删
    assert "www.example.com" not in cleaned
    assert "http://ads.example.com" not in cleaned
    assert "请收藏本站" not in cleaned
    assert "第一章 开始" in cleaned
    assert "第二章 继续" in cleaned
    assert "\r" not in cleaned
    assert "正文有多余空格。" in cleaned
    assert "他说道：“好”。" in cleaned
    assert report.removed_lines >= 3
    assert report.removed_by.get("url", 0) >= 1
    assert report.removed_by.get("ad", 0) >= 1
    assert report.removed_by.get("separator", 0) >= 1


def test_cleanup_keeps_author_notes_and_plot_words():
    text = (
        "第一章 开始\n"
        "作者的话：这一章陈野还没有觉醒。\n"
        "他看了一眼楼下的广告牌。\n"
        "正文。\n"
    )
    cleaned, _ = clean_text(text)
    assert "作者的话" in cleaned
    assert "广告牌" in cleaned
    assert "正文。" in cleaned


def test_cleanup_reports_mojibake_without_rewriting():
    text = "第一章 开始\n正文�出现乱码â€™。\n"
    cleaned, report = clean_text(text)
    assert "�" in cleaned                       # 只报告，不擅自改字
    assert any("乱码" in w for w in report.warnings)


def test_cleanup_is_idempotent():
    text = "第一章 开始\r\n\r\n请收藏本站 www.a.com\r\n正文  一。\r\n"
    once, _ = clean_text(text)
    twice, _ = clean_text(once)
    assert once == twice


def test_parse_uses_cleaned_text_and_content_counts():
    novel = parse_text(
        "第一章 开始\n请收藏本站 www.a.com\n正文。\n", cleanup=True)
    assert "www.a.com" not in novel.text
    assert novel.cleanup is not None
    assert novel.content_chars == content_chars(novel.text)
    assert novel.cjk_chars == cjk_chars(novel.text)
    assert novel.content_chars < novel.total_chars


def test_cleanup_can_be_disabled():
    novel = parse_text("第一章 开始\n请收藏本站 www.a.com\n正文。\n",
                       cleanup=False)
    assert "www.a.com" in novel.text
    assert novel.cleanup is None
