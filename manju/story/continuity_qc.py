# -*- coding: utf-8 -*-
"""P2-4 衔接 QC 面板：跨场/跨集实体漂移 diff 报告（f7-studios carry-forward diff 思路）。

输入 script + board JSON，沿分镜 state_out 链逐 cut 走，对每个实体
（角色位置/朝向/服装/道具归属/伤势）做前后 diff，未被剧本 state_change
授权的漂移标记为断点。纯确定性，不上 LLM。
"""
from __future__ import annotations

from dataclasses import dataclass, field

from .models import ScriptBeat, ScriptEpisode, StoryEpisodeBoard

_WARDROBE_TOKEN = __import__("re").compile(r"C\d+-[A-Za-z0-9_-]+")


@dataclass
class Drift:
    at: str           # 发生位置：E01-02/cut3
    entity: str       # C1 / P1
    field: str        # where/facing/wardrobe/props/injuries
    before: str
    after: str
    authorized: bool  # 是否被剧本 state_change 授权


def _authorized_changes(script: ScriptEpisode) -> set[tuple[str, str]]:
    """剧本里授权过的 (cid, 变更描述) 集合——QC 据此判断漂移是否有据。"""
    out: set[tuple[str, str]] = set()
    for sc in script.scenes:
        for b in sc.beats:
            for cid, chg in (b.state_change or {}).items():
                out.add((cid, chg))
    return out


def diff_episode(script: ScriptEpisode, board: StoryEpisodeBoard) -> list[Drift]:
    """对单集 board 的 state_out 链做实体 diff。"""
    authed = _authorized_changes(script)
    out: list[Drift] = []
    prev = board.state_in
    for seg in board.segments:
        for ci, cut in enumerate(seg.cuts, 1):
            cur = cut.state_out
            at = f"{seg.id}/cut{ci}"
            for fld in ("where", "facing", "posture", "wardrobe", "props", "injuries"):
                pv = getattr(prev, fld)
                cv = getattr(cur, fld)
                for key in set(pv) | set(cv):
                    a, b = pv.get(key), cv.get(key)
                    if a == b:
                        continue
                    ok = False
                    if fld == "wardrobe":
                        ok = any(cid == key for cid, _ in authed)
                    elif fld == "injuries":
                        ok = any(cid == key for cid, _ in authed)
                    out.append(Drift(at=at, entity=key, field=fld,
                                     before=str(a), after=str(b), authorized=ok))
            prev = cur
    return out


def qc_report(script: ScriptEpisode, board: StoryEpisodeBoard) -> dict:
    """实体漂移 diff 报告：断点清单（未授权）+ 全部漂移计数。"""
    drifts = diff_episode(script, board)
    breaks = [d for d in drifts if not d.authorized]
    return {
        "ep": script.ep,
        "total_drifts": len(drifts),
        "unauthorized_breaks": [
            {"at": d.at, "entity": d.entity, "field": d.field,
             "before": d.before, "after": d.after} for d in breaks],
        "ok": not breaks,
    }
