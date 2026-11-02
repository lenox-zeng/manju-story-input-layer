# -*- coding: utf-8 -*-
"""跨集连续性动线图 + World Bible 自动导出（P2）。

目的：
  1. 跨集动线图：扫描全部集分镜的 state_out，为每个角色/道具/伤势建立"逐镜对齐"的
     动线轨迹（在哪、朝哪、伤势变化、道具在谁手里），供下游（实现层/审查）读入；
  2. World Bible 自动导出：把 cast/art 的角色/服装/场景/道具连续性块写成独立 JSON，
     逐字复用到任意镜头，跨集保持一致。

渲染/成片一律不做——本模块只产出**输入侧**的连续性数据文件。
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from .models import Art, Cast, StoryStoryboard, identity_anchor


def export_world_bible(cast: Cast, art: Art) -> dict[str, Any]:
    """World Bible：角色/服装/场景/道具连续性块（与 stages._world_bible 同口径）。"""
    characters = {}
    for c in cast.characters:
        wb = c.wardrobe_states
        if isinstance(wb, (list, tuple)):
            wardrobe = [s.model_dump(mode="json") if hasattr(s, "model_dump")
                        else s for s in wb]
        else:
            wardrobe = []
        characters[c.id] = {
            "name": c.name,
            "appearance": (c.image.prompt if c.image else "") or "",
            "identity_anchor": identity_anchor(c),
            "wardrobe_states": wardrobe,
            "color_signature": c.color_signature or "",
        }
    scenes = {}
    for s in art.scenes:
        scenes[s.id] = {
            "name": s.name,
            "plate": (s.image.prompt if s.image else "") or "",
            "lighting": [(l.state, l.prompt) for l in s.lighting] if s.lighting else [],
        }
    props = {}
    for p in art.props:
        props[p.id] = {
            "name": p.name,
            "prompt": (p.image.prompt if p.image else "") or "",
            "scale": p.scale or "",
        }
    return {"characters": characters, "scenes": scenes, "props": props}


def build_continuity_map(storyboard: StoryStoryboard) -> dict[str, Any]:
    """跨集逐镜扫描 state_out，建立角色/道具/伤势动线图。

    返回：
      characters: {角色id: [{ep, scene, cut, where, facing, posture, injuries, screen_side}, ...]}
      props:      {道具id: [{ep, scene, cut, holder}, ...]}
      injuries:   {角色id: [{ep, scene, cut, state}, ...]}（只记出现伤势的镜头，便于追变化点）
      meta:       本集列表
    """
    characters: dict[str, list[dict]] = {}
    props: dict[str, list[dict]] = {}
    injuries: dict[str, list[dict]] = {}
    for board in storyboard.episodes:
        ep = board.ep
        for si, seg in enumerate(board.segments, 1):
            for ci, cut in enumerate(seg.cuts, 1):
                so = cut.state_out
                ref = {"ep": ep, "scene": si, "cut": ci}
                ss = cut.screen_side if isinstance(cut.screen_side, dict) else {}
                for who in so.where:
                    characters.setdefault(who, []).append({
                        **ref,
                        "where": so.where.get(who),
                        "facing": so.facing.get(who),
                        "posture": so.posture.get(who),
                        "injuries": so.injuries.get(who),
                        "screen_side": ss.get(who),
                    })
                    inj = so.injuries.get(who)
                    if inj:
                        injuries.setdefault(who, []).append({**ref, "state": inj})
                for prop, holder in so.props.items():
                    props.setdefault(prop, []).append({**ref, "holder": holder})
    return {
        "characters": characters,
        "props": props,
        "injuries": injuries,
        "meta": {"episodes": [b.ep for b in storyboard.episodes]},
    }


def dump_continuity(storyboard: StoryStoryboard, cast: Cast, art: Art,
                    workdir: str | Path) -> tuple[Path, Path]:
    """同时导出 World Bible 与跨集动线图到 workdir/state，返回 (world_bible, continuity_map) 路径。"""
    wd = Path(workdir)
    state_dir = wd / "state"
    state_dir.mkdir(parents=True, exist_ok=True)
    wb = state_dir / "world_bible.json"
    wb.write_text(
        __import__("json").dumps(export_world_bible(cast, art),
                                 ensure_ascii=False, indent=2),
        encoding="utf-8")
    cm = state_dir / "continuity_map.json"
    cm.write_text(
        __import__("json").dumps(build_continuity_map(storyboard),
                                 ensure_ascii=False, indent=2),
        encoding="utf-8")
    return wb, cm
