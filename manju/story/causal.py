# -*- coding: utf-8 -*-
"""P2 R² 两遍生成 + 分层多 Agent 的确定性编排辅助。

- Reader：滑窗扫法典/大纲 → CausalGraph（LLM 产出，本模块只做去环与校验）
- Rewriter：基于因果图产 SceneOutline，再产 screenplay
- Look Back：整集生成后回看场间事件链闭合，断裂则命中回流重写

全部为纯确定性逻辑；LLM 调用在 stages.py 编排。
"""
from __future__ import annotations

from .models import CausalEdge, CausalGraph, SceneOutlineItem, ScriptScene


def break_cycles(graph: CausalGraph) -> tuple[CausalGraph, list[str]]:
    """greedy cycle-breaking：DFS 找环，环上最后一条入边回退，直到无环。

    返回 (无环图, 被回退的边描述列表)。确定性（按节点 id 字典序遍历）。
    """
    adj: dict[str, list[str]] = {}
    for e in graph.edges:
        adj.setdefault(e.from_id, []).append(e.to_id)
    removed: list[str] = []
    kept: list[CausalEdge] = []
    WHITE, GRAY, BLACK = 0, 1, 2
    color: dict[str, int] = {n.id: WHITE for n in graph.nodes}

    def dfs(u: str) -> None:
        color[u] = GRAY
        for v in sorted(adj.get(u, [])):
            if color.get(v, WHITE) == GRAY:
                # 回边：找到环，丢弃（greedy：丢这条）
                removed.append(f"{u}->{v}")
                continue
            if color.get(v, WHITE) == WHITE:
                dfs(v)
                kept.append(CausalEdge(from_id=u, to_id=v))
        color[u] = BLACK

    for nid in sorted(color):
        if color[nid] == WHITE:
            dfs(nid)
    # 重建图（去环边）
    g2 = CausalGraph(nodes=graph.nodes, edges=kept)
    return g2, removed


def validate_scene_chain(outline: list[SceneOutlineItem],
                         graph: CausalGraph) -> list[str]:
    """Rewriter 产出的场景大纲必须与因果图闭合：

    - 每场 beats 引用的事件 id 必须在图中存在；
    - 第 2 场及以后，本场覆盖的事件必须至少有一条因果边来自前序场覆盖的事件
      （否则=与前序无因果承接，凭空开启新事件线）。
    """
    errs: list[str] = []
    g = {e.to_id for e in graph.edges} | {e.from_id for e in graph.edges}
    known = graph.node_ids()
    covered_so_far: set[str] = set()
    for item in sorted(outline, key=lambda x: x.scene):
        for nid in item.beats:
            if nid not in known:
                errs.append(f"第{item.scene}场引用事件 {nid} 不在因果图中")
        mine = set(item.beats)
        if covered_so_far:
            has_incoming = any(
                e.from_id in covered_so_far and e.to_id in mine
                for e in graph.edges)
            # 或者本场事件直接作为前序事件的 react/causes 目标
            if not has_incoming and mine:
                errs.append(
                    f"第{item.scene}场事件 {sorted(mine)} 与前序覆盖事件 "
                    f"{sorted(covered_so_far)} 无因果边承接（凭空开启新事件线）")
        covered_so_far |= mine
    return errs


def look_back_check(scenes: list[ScriptScene]) -> list[str]:
    """P2-3 Look Back 回看：场间事件链闭合确定性检查。

    口径：第 2 场起，开场第一拍必须 state_from 非空（承接上场末状态），
    否则=场间断裂，命中回流重写该场。
    """
    out: list[str] = []
    for prev, cur in zip(scenes, scenes[1:]):
        first = cur.beats[0] if cur.beats else None
        if first is None:
            continue
        if not (first.state_from or "").strip():
            out.append(
                f"{cur.id} 开场拍未承接上场（{prev.id}）状态：缺 state_from，"
                "Look Back 判定场间断裂，回流重写该场")
    return out
