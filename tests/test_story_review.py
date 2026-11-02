# -*- coding: utf-8 -*-
"""人工审阅节点：大纲审批、身份图审批、失败包。"""
from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from manju.story.models import (
    Adaptation, AdaptationItem, Beat, EpisodePlan, Outline,
    OutlineCharacter, OutlineProp, OutlineScene, PowerRule, RiskItem, Spine,
    StoryParams,
)
from manju.story.review import (
    approve_assets, approve_outline, asset_hashes, assets_gate_status,
    build_outline_review, mark_assets_review_required, marker_path,
    outline_gate_status, request_outline_review, write_failure_packet,
)
from manju.story.stages import StoryPipeline
from manju.story.validate import GateReport


def _outline(episodes: int = 2) -> Outline:
    return Outline(
        source="源文",
        title="渡口测试",
        logline="少年在渡口做出选择",
        adaptation=Adaptation(
            core="一个少年必须离开渡口",
            keep=[AdaptationItem(what="主线", why="核心冲突")],
            cut=[AdaptationItem(what="支线", why="无法在竖屏展开")],
        ),
        spine=Spine(want="离开", obstacle="渡口封了", choice="上船",
                    cost="失去旧身份", turn="船主认出他", hook="船离岸"),
        power_rule=PowerRule(name="看水", sees="读懂水流", cannot="改变天气",
                             cost="每次消耗体力"),
        params=StoryParams(episodes=episodes, target_seconds=90),
        characters=[
            OutlineCharacter(id="C1", name="阿禾", role="主角", tier="lead"),
            OutlineCharacter(id="C2", name="船主", role="反派", tier="support"),
        ],
        scenes=[OutlineScene(id="S1", name="渡口", primary=True,
                             summary="全剧主场景")],
        props=[OutlineProp(id="P1", name="船票", function="离岸的凭证")],
        beats=[Beat(id="B1", type="揭破", weight="major", episode=1),
               Beat(id="B2", type="反转", weight="minor", episode=2)],
        episodes=[
            EpisodePlan(ep=i, title=f"第{i}集", synopsis="叙述体梗概",
                        hook="开场钩子", cliff="结尾悬念", scene_ids=["S1"],
                        character_ids=["C1", "C2"], prop_ids=["P1"])
            for i in range(1, episodes + 1)
        ],
    )


def _save_outline(workdir, outline):
    workdir.mkdir(parents=True, exist_ok=True)
    p = workdir / "outline.json"
    p.write_text(json.dumps(outline.model_dump(mode="json"),
                            ensure_ascii=False, indent=2), encoding="utf-8")
    return p


def test_outline_review_writes_html_and_markdown(cfg, tmp_path):
    workdir = tmp_path / "work"
    outline = _outline()
    _save_outline(workdir, outline)
    review = request_outline_review(cfg, outline, workdir)
    assert review.index.is_file() and review.markdown.is_file()
    html = review.index.read_text(encoding="utf-8")
    assert "渡口测试" in html and "先看这五件事" in html
    assert marker_path(cfg, "outline").is_file()


def test_outline_requires_approval_then_passes(cfg, tmp_path):
    workdir = tmp_path / "work"
    outline = _outline()
    _save_outline(workdir, outline)
    assert outline_gate_status(cfg, workdir, resume=True)[0]
    request_outline_review(cfg, outline, workdir)
    assert not outline_gate_status(cfg, workdir, resume=True)[0]
    ok, _ = approve_outline(cfg, workdir)
    assert ok
    assert not marker_path(cfg, "outline").exists()
    assert outline_gate_status(cfg, workdir, resume=True)[0]


def test_outline_edit_after_approval_requires_rereview(cfg, tmp_path):
    workdir = tmp_path / "work"
    outline = _outline()
    p = _save_outline(workdir, outline)
    request_outline_review(cfg, outline, workdir)
    assert approve_outline(cfg, workdir)[0]
    data = json.loads(p.read_text(encoding="utf-8"))
    data["title"] = "改过的大纲"
    p.write_text(json.dumps(data, ensure_ascii=False, indent=2),
                 encoding="utf-8")
    ok, msg = outline_gate_status(cfg, workdir, resume=True)
    assert not ok and "修改" in msg
    # 没有重新审阅，不能直接批准新版本
    assert not approve_outline(cfg, workdir)[0]
    request_outline_review(cfg, _outline(), workdir)
    assert approve_outline(cfg, workdir)[0]


def test_outline_review_escapes_content(cfg, tmp_path):
    outline = _outline()
    outline.title = '<script>alert(1)</script>'
    review = build_outline_review(outline, tmp_path)
    html = review.index.read_text(encoding="utf-8")
    assert "<script>alert(1)</script>" not in html
    assert "&lt;script&gt;" in html


def test_assets_approval_and_change_invalidation(cfg, tmp_path):
    assets = tmp_path / "assets" / "characters"
    for cid, blob in (("C1", b"one"), ("C2", b"two")):
        (assets / cid).mkdir(parents=True)
        (assets / cid / "identity.png").write_bytes(blob)

    ok, msg = assets_gate_status(cfg, assets, require=True)
    assert not ok and "anchors" in msg
    assert asset_hashes(assets).keys() == {"C1", "C2"}

    review = mark_assets_review_required(cfg, assets)
    assert review.index.is_file()
    assert not assets_gate_status(cfg, assets, require=True)[0]
    assert approve_assets(cfg, assets)[0]
    assert assets_gate_status(cfg, assets, require=True)[0]

    (assets / "C1" / "identity.png").write_bytes(b"changed")
    ok, msg = assets_gate_status(cfg, assets, require=True)
    assert not ok and "C1" in msg
    assert not approve_assets(cfg, assets)[0]  # 必须先重新审阅
    mark_assets_review_required(cfg, assets)
    assert approve_assets(cfg, assets)[0]
    assert assets_gate_status(cfg, assets, require=True)[0]


def test_failure_packet(cfg, tmp_path):
    report = GateReport("novel-script", ["台词超长", "时长超限"], ["软门"])
    latest = write_failure_packet(cfg, tmp_path, "novel-script", report, 3)
    assert latest.is_file()
    assert "novel-script" in latest.read_text(encoding="utf-8")
    raw = sorted(latest.parent.glob("*.json"))[-1]
    data = json.loads(raw.read_text(encoding="utf-8"))
    assert data["errors"] == ["台词超长", "时长超限"]
    assert data["attempts"] == 3


def test_pipeline_until_outline_skips_cast(tmp_path):
    pipe = StoryPipeline(llm=object(), workdir=tmp_path)
    pipe.prepare_source = lambda text: "源文"
    pipe.bible = lambda *a, **k: SimpleNamespace()
    pipe.episode_plans = lambda *a, **k: SimpleNamespace(
        source="源文", episodes=[SimpleNamespace(ep=1)])

    def boom(*_a, **_k):
        raise AssertionError("until=outline 不应生成角色/美术")

    pipe.cast = boom
    pipe.art = boom
    outline, cast, art, scripts, _staging, board = pipe.run(
        "源文", 1, 90, "9:16", until="outline")
    assert outline is not None
    assert cast is None and art is None and board is None
    assert scripts == []


def test_pipeline_resume_uses_edited_outline(tmp_path):
    """人工改过的 outline.json 必须优先于 bible/批次缓存。"""
    outline = _outline(2)
    outline.title = "人工改过的大纲"
    pipe = StoryPipeline(llm=object(), workdir=tmp_path, resume=True)
    pipe._save("outline", outline)
    pipe.prepare_source = lambda text: "源文"

    def boom(*_a, **_k):
        raise AssertionError("有完整 outline 缓存时不应重跑大纲")

    pipe.bible = boom
    pipe.episode_plans = boom
    got, cast, art, scripts, _staging, board = pipe.run(
        "源文", 2, 90, "9:16", until="outline")
    assert got.title == "人工改过的大纲"
    assert cast is None and art is None and board is None


def test_story_cli_approve_outline_without_llm(cfg, root, tmp_path):
    from manju import story_cli

    story = root / "story.txt"
    story.write_text("第一段。\n\n第二段。", encoding="utf-8")
    workdir = tmp_path / "work"
    _save_outline(workdir, _outline())
    rc = story_cli.main([str(story), "--approve", "outline",
                         "--workdir", str(workdir)])
    assert rc == 0
    ledger = json.loads(
        (Path(cfg.p("state")) / "review" / "approvals.json").read_text(
            encoding="utf-8"))
    assert ledger["outline"]["hash"]


@pytest.mark.skip(reason="依赖完整流水线的 manju.cli（--approve assets / novel 子命令）；本输入层包仅提供精简的 manju.story_cli")
def test_full_cli_approve_assets_without_llm(cfg, root):
    from manju import cli

    story = root / "story.txt"
    story.write_text("第一段。\n\n第二段。", encoding="utf-8")
    rc = cli.main(["novel", str(story), "--approve", "assets"])
    assert rc == 2  # 没有 identity.png，不能空批准

    char_dir = Path(cfg.p("assets")) / "characters" / "C1"
    char_dir.mkdir(parents=True)
    (char_dir / "identity.png").write_bytes(b"anchor")
    rc = cli.main(["novel", str(story), "--approve", "assets"])
    assert rc == 0
    ledger = json.loads(
        (Path(cfg.p("state")) / "review" / "approvals.json").read_text(
            encoding="utf-8"))
    assert set(ledger["assets"]["hashes"]) == {"C1"}


@pytest.mark.skip(reason="依赖完整流水线的 manju.cli（--review-assets --run / novel 子命令）；本输入层包仅提供精简的 manju.story_cli")
def test_full_cli_review_assets_full_blocks_without_approval(cfg, root):
    from manju import cli

    story = root / "story.txt"
    story.write_text("第一段。\n\n第二段。", encoding="utf-8")
    rc = cli.main(["novel", str(story), "--review-assets", "--run", "full"])
    assert rc == 2
