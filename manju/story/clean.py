# -*- coding: utf-8 -*-
"""小说拿到手后的预处理：格式归一化 + 明显广告/网址清理。

设计原则（保守优先）：
* 只删“整行明显是广告/网址/分隔符”的内容，不删正文段落；
* 章节标题、作者名、书名等行永不按广告删除；
* 不尝试自动修复乱码，只检测并报告（乱码修复比乱删更危险）；
* 不改写原文文件，只返回清理后的文本与报告；报告保留被删样例，便于人工复核；
* 清理是确定性的，不调用 LLM，可重复执行（幂等）。

顺序：解码 → clean_text → 章节识别 → 分集 → 压缩/剧本。
"""
from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass, field

# 行首/行尾控制字符、零宽字符
_ZW_RE = re.compile(r"[​-‏⁠﻿]")
_CTRL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")

# HTML 常见标签与实体（网页抓取的文本经常带）
_SCRIPT_RE = re.compile(r"<(script|style)\b[^>]*>.*?</\1>", re.IGNORECASE | re.DOTALL)
_HTML_TAG_RE = re.compile(
    r"</?(?:p|br|div|span|a|font|strong|em|b|i|u|h[1-6]|li|ul|ol|"
    r"table|thead|tbody|tr|td|th|img|blockquote|pre|code)\b[^>]*>",
    re.IGNORECASE)
_ENTITY_RE = re.compile(r"&(nbsp|amp|lt|gt|quot|#\d+|#x[0-9a-f]+);",
                        re.IGNORECASE)
_ENTITY_MAP = {"nbsp": " ", "amp": "&", "lt": "<", "gt": ">", "quot": '"'}

# 网址 / 站点 / 广告关键词
_URL_RE = re.compile(r"(?:https?://|www\.)[^\s<>\"'）)】]+", re.IGNORECASE)
_DOMAIN_RE = re.compile(
    r"(?<![\w.-])(?:[a-z0-9-]+\.)+"
    r"(?:com|net|org|cc|cn|info|xyz|top|vip|me|tv|la|io|co)\b",
    re.IGNORECASE)
# 只列“几乎不会出现在正文里”的短语。不要放“首发/广告/推荐票/打赏”这类
# 单字词：实测“首发小前锋”会被误判成站点广告，整句正文被删。
_AD_WORDS_RE = re.compile(
    r"(笔趣阁|顶点小说|无弹窗|最新章节|全文阅读|请收藏|加入书签|手机阅读|"
    r"手机用户|请记住本站|本站域名|本站地址|下载.{0,6}APP|"
    r"微信公众号|QQ群|书友群|求推荐票|求月票|求打赏|求订阅|本章未完|"
    r"点击下一页|返回目录|章节目录|广告位|广告招租|广告合作)",
    re.IGNORECASE)
# 分隔线：*** / --- / === / …… / ~~~~ 等
_SEPARATOR_RE = re.compile(
    r"^[\s*=\-_—–·.。…~#★☆♦◆◇■□▲△▽▼※]{3,}$")

# 章节标题：用于保护，防止广告规则误删标题行
_HEADING_RE = re.compile(
    r"^\s*(?:第\s*[0-9０-９零〇一二三四五六七八九十百千万两]+\s*[章回节卷部篇集]"
    r"|楔子|序章|序言|序幕|引子|前言|尾声|终章|后记|番外|"
    r"(?:chapter|part|volume)\s+[0-9ivxlcdm]+)",
    re.IGNORECASE)


@dataclass(frozen=True)
class CleanReport:
    """一次清理的可核对结果。字数均为“字符数”，不是 token。"""

    before_chars: int = 0
    after_chars: int = 0
    before_content: int = 0
    after_content: int = 0
    before_cjk: int = 0
    after_cjk: int = 0
    removed_lines: int = 0
    removed_by: dict[str, int] = field(default_factory=dict)
    examples: list[dict[str, str]] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def changed(self) -> bool:
        return (self.before_chars != self.after_chars
                or self.removed_lines > 0
                or self.before_content != self.after_content)

    @property
    def removed_content(self) -> int:
        return max(0, self.before_content - self.after_content)

    @property
    def removed_ratio(self) -> float:
        if self.before_content <= 0:
            return 0.0
        return self.removed_content / self.before_content

    def to_dict(self) -> dict:
        return {
            "before_chars": self.before_chars,
            "after_chars": self.after_chars,
            "before_content": self.before_content,
            "after_content": self.after_content,
            "before_cjk": self.before_cjk,
            "after_cjk": self.after_cjk,
            "removed_lines": self.removed_lines,
            "removed_by": dict(self.removed_by),
            "removed_ratio": round(self.removed_ratio, 4),
            "examples": list(self.examples),
            "warnings": list(self.warnings),
        }


def content_chars(text: str) -> int:
    """去掉所有空白（含换行、CR、空格、全角空格）后的字符数。"""
    return sum(1 for ch in text if not ch.isspace())


def cjk_chars(text: str) -> int:
    """纯汉字数量（不含标点、数字、拉丁字母）。"""
    return sum(1 for ch in text if "一" <= ch <= "鿿")


def _example(category: str, line: str) -> dict[str, str]:
    line = line.strip()
    if len(line) > 80:
        line = line[:77] + "…"
    return {"category": category, "line": line}


def _entity_sub(m: re.Match[str]) -> str:
    key = m.group(1).lower()
    if key in _ENTITY_MAP:
        return _ENTITY_MAP[key]
    try:
        if key.startswith("#x"):
            return chr(int(key[2:], 16))
        if key.startswith("#"):
            return chr(int(key[1:]))
    except (ValueError, OverflowError):
        pass
    return m.group(0)


def _normalize_spaces(line: str, *, heading: bool = False) -> str:
    """统一空格；标题行保留“第一章 开始”这类作者有意留的分隔。"""
    line = re.sub(r"[ \t]+", " ", line).strip()
    line = re.sub(r"\s+([，。！？；：、）】》”’…—])", r"\1", line)
    line = re.sub(r"([（【《“‘])\s+", r"\1", line)
    line = re.sub(r"([，。！？；：、])\s+", r"\1", line)
    if not heading:
        # 网页/OCR 常见的“逐字空格”：两个汉字之间的单空格是排版噪音。
        # 标题不处理，避免把“第一章 开始”改成“第一章开始”。
        line = re.sub(r"(?<=[一-鿿])\s+(?=[一-鿿])",
                      "", line)
    return line


def _normalize_heading(line: str) -> str:
    """把“第 1 章”这类标题的空格收掉，便于统一识别与显示。"""
    if not _HEADING_RE.match(line):
        return line
    return re.sub(
        r"^(第)\s*([0-9０-９零〇一二三四五六七八九十百千万两]+)\s*"
        r"([章回节卷部篇集])",
        r"\1\2\3", line)


def _ad_category(line: str) -> str | None:
    """返回广告类别；None 表示保留。只对短行生效，避免误删正文。"""
    if _HEADING_RE.match(line):
        return None
    if len(line) > 120:
        return None
    if _URL_RE.search(line):
        # 整行以网址为主才删；正文里夹一个网址只做行内清理
        rest = _URL_RE.sub("", line).strip(" \t:：-—|")
        if len(rest) <= 8:
            return "url"
    if _DOMAIN_RE.search(line):
        rest = _DOMAIN_RE.sub("", line).strip(" \t:：-—|")
        if len(rest) <= 8:
            return "url"
    if _AD_WORDS_RE.search(line):
        return "ad"
    return None


def clean_text(text: str) -> tuple[str, CleanReport]:
    """清理文本并返回 (cleaned, report)。

    不做“自动改错”：疑似乱码只记 warning，保持原文可见。
    """
    raw_text = text or ""
    before_chars = len(raw_text)             # 真·原始字符（含 CRLF/BOM）
    normalized = raw_text.replace("\r\n", "\n").replace("\r", "\n")
    normalized = normalized.replace("﻿", "")
    before_content = content_chars(normalized)
    before_cjk = cjk_chars(normalized)

    warnings: list[str] = []
    replacement = normalized.count("�")
    mojibake_hits = len(re.findall(r"(?:Ã.|Â.|â€|ï¿½)", normalized))
    if replacement or mojibake_hits:
        warnings.append(
            f"检测到疑似乱码 {replacement + mojibake_hits} 处，"
            "未自动修复；请人工核对（自动改字风险高于保留）")

    removed: Counter[str] = Counter()
    examples: list[dict[str, str]] = []

    # 先去掉网页脚本/样式与常见标签、实体
    normalized, n_script = _SCRIPT_RE.subn("", normalized)
    if n_script:
        removed["html"] += n_script
    normalized = _HTML_TAG_RE.sub("", normalized)
    normalized = _ENTITY_RE.sub(_entity_sub, normalized)

    out: list[str] = []
    for raw in normalized.split("\n"):
        line = _ZW_RE.sub("", raw)
        line = _CTRL_RE.sub("", line)
        line = line.replace(" ", " ").replace("　", " ")
        is_heading = bool(_HEADING_RE.match(line))
        line = _normalize_spaces(line, heading=is_heading)

        if not line:
            out.append("")
            continue

        # 分隔线统一成空行，不单独算“删正文”
        if _SEPARATOR_RE.fullmatch(line):
            removed["separator"] += 1
            if len(examples) < 20:
                examples.append(_example("separator", line))
            out.append("")
            continue

        if _HEADING_RE.match(line):
            out.append(_normalize_heading(line))
            continue

        category = _ad_category(line)
        if category:
            removed[category] += 1
            if len(examples) < 20:
                examples.append(_example(category, line))
            continue

        # 正文里夹带的网址：去掉网址，保留其余内容
        line2 = _URL_RE.sub("", line).strip()
        if line2 != line:
            removed["inline_url"] += 1
            if len(examples) < 20:
                examples.append(_example("inline_url", line))
            line = line2
            if not line:
                continue

        out.append(_normalize_spaces(line))

    cleaned = "\n".join(out)
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned).strip()
    # 保留“原文是否有结尾换行”的形状：清理不应凭空改变文本长度口径
    if cleaned and normalized.endswith("\n"):
        cleaned += "\n"

    after_chars = len(cleaned)
    after_content = content_chars(cleaned)
    after_cjk = cjk_chars(cleaned)
    report = CleanReport(
        before_chars=before_chars,
        after_chars=after_chars,
        before_content=before_content,
        after_content=after_content,
        before_cjk=before_cjk,
        after_cjk=after_cjk,
        removed_lines=sum(removed.values()),
        removed_by=dict(removed),
        examples=examples,
        warnings=warnings,
    )
    if report.removed_ratio > 0.10:
        report.warnings.append(
            f"清理删除了 {report.removed_ratio:.1%} 的有效字符，"
            "请检查是否误删正文（样例见 examples）")
    return cleaned, report
