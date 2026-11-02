# -*- coding: utf-8 -*-
"""长篇压缩：分卷、短篇跳过、卷数上限、合并超限、缓存续跑。"""
from __future__ import annotations

from manju.story.ingest import parse_text
from manju.story.summarize import VolumeDigest, build_codex, group_volumes


class FakeLLM:
    def __init__(self, *, digest_chars: int = 300, merge_text: str | None = None):
        self.digest_chars = digest_chars
        self.merge_text = merge_text
        self.volume_calls = 0
        self.merge_calls = 0

    def structured(self, stage, system, payload, *, model_cls, **_kw):
        self.volume_calls += 1
        v = payload["volume"]
        return model_cls(events=f"第{v}卷：" + "事件" * (self.digest_chars // 2),
                         new_entities=[f"人物{v}", "渡口"])

    def chat(self, system, user, **_kw):
        self.merge_calls += 1
        return self.merge_text if self.merge_text is not None else "全书法典正文"


def _novel(chapters: int, chars: int = 5000, *, paragraphs: int = 0):
    parts = []
    for i in range(1, chapters + 1):
        head = f"第{i}章 渡口夜话\n"
        if paragraphs:
            body = ("段落内容。" * (chars // paragraphs) + "\n") * paragraphs
            body = body[: chars - len(head)]
        else:
            body = "字" * (chars - len(head) - 1) + "\n"
        parts.append(head + body)
    return parse_text("".join(parts))


def test_short_novel_is_not_summarized():
    novel = _novel(1, 5000)
    llm = FakeLLM()
    codex = build_codex(novel, llm)
    assert codex.skipped
    assert codex.text == novel.text
    assert llm.volume_calls == 0


def test_volumes_are_15_chapters_by_default():
    novel = _novel(30)
    volumes, warnings = group_volumes(novel)
    assert warnings == []
    assert [v.title for v in volumes] == ["第1–15章", "第16–30章"]


def test_codex_collects_events_and_entities():
    novel = _novel(30)
    llm = FakeLLM()
    codex = build_codex(novel, llm)
    assert llm.volume_calls == 2 and llm.merge_calls == 0
    assert "第1–15章" in codex.text and "第16–30章" in codex.text
    assert codex.new_entities == ["人物1", "渡口", "人物2"]
    assert not codex.truncated


def test_volume_cap_is_reported_not_silent():
    novel = _novel(20)
    llm = FakeLLM()
    codex = build_codex(novel, llm, chapters_per_volume=5, max_volumes=2)
    assert len(codex.volumes) == 2
    assert codex.truncated
    assert any("未进入法典" in w and "第11–20章" in w for w in codex.warnings)


def test_merge_over_limit_is_hard_trimmed_with_warning():
    novel = _novel(30)
    llm = FakeLLM(digest_chars=5000, merge_text="法" * 20000)
    codex = build_codex(novel, llm)
    assert llm.merge_calls == 1
    assert len(codex.text) <= 9000
    assert codex.truncated and any("截断" in w for w in codex.warnings)
    assert codex.text.endswith("后文已截断）")


def test_resume_reuses_volume_cache(tmp_path):
    novel = _novel(30)
    first = build_codex(novel, FakeLLM(), workdir=tmp_path, resume=True)
    second_llm = FakeLLM()
    second = build_codex(novel, second_llm, workdir=tmp_path, resume=True)
    assert second_llm.volume_calls == 0
    assert second.text == first.text


def test_volume_digest_schema():
    d = VolumeDigest(events="有人来过。", new_entities=["旧皮箱"])
    assert d.events == "有人来过。"
