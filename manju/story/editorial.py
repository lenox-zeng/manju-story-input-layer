# -*- coding: utf-8 -*-
"""剪辑纪律校验：跳切 / 越轴迹象 / 动作断点 / 时长下限。

这是"导演级"剪辑规则的**确定性**校验（非 LLM）。它补上现有 state 级连续性
（continuity.py 只查 state_out→state_in 的 where/facing/wardrobe/props 延续）
所不覆盖的"剪辑"层面：画面接不接得上、景别跳不跳、动作因果断不断。

接入 validate_segment_cuts：默认 warning 级暴露断点（不阻断过门，配合
BOARD_RULES 强化让生成时尽量遵守）；`strict=True` 时升级为 error，触发
_generate 的回喂重写。
"""
from __future__ import annotations

import re
from typing import Any

SIZE_ORDER = {"extreme_wide": 0, "wide": 1, "medium": 2,
              "close": 3, "extreme_close": 4}

# ---- 2026-10-30 动作数统计（时长与阶段化校验共用） ----
_ACTION_VERBS = re.compile(
    r"(推|拉|抓|跑|跳|摔|走|站起|坐下|转身|抬头|低头|抱|扔|捡|关|开|看|喊|哭|笑|跪|倒|冲|停|扑|滚|爬|躲|敲|撞|挥手|指|"
    r"walk|run|grab|push|pull|turn|look|reach|lean|open|close|lift|drop|step|rush|point|raise|hold|slip|release|"
    r"sink|slide|stiffen|swallow|catch|approach|halt|swing|sweep|bend|kneel|stand|rise|shift|move|enter|leave|flee|"
    r"creep|stretch|crouch|jump|throw|toss|grip|yank|wrench|shove|whirl|dive|lunge|snap)", re.I)


def count_action_verbs(text: str) -> int:
    """统计一段描述里的动作动词数（中英双语，去重）。

    用于"单镜时长 ≥ 动作数 × 基准"的校验：动作越多，需要的时间越长；
    超过引擎单镜动作上限（max_actions_per_shot）时要求拆镜/拆段。
    动词词表是保守子集（不追求召回），误报宁可少。
    """
    if not text:
        return 0
    return len(set(_ACTION_VERBS.findall(text)))

# 上一镜 action_done 后，下一镜应出现的承接 coverage
_ACTION_REACTION = {"action", "reaction", "establish", "dialogue"}

# 动作镜表达所需的最短时长
MIN_ACTION_SECONDS = 2.2
# 台词镜按“字数/语速”确定性算时长，9 字短句天然只有 2.0s；
# 不能拿动作镜的 2.2s 去判它“动作不完整”，否则短对白永远告警。
MIN_DIALOGUE_SECONDS = 1.2

# 非过渡理由时景别连跳级数上限
MAX_JUMP_LEVELS = 2

_TRANSITION_REASONS = {"new_info", "pov", "reaction", "sound", "time", "match_cut"}


def _size_gap(a: str | None, b: str | None) -> int | None:
    if a not in SIZE_ORDER or b not in SIZE_ORDER:
        return None
    return abs(SIZE_ORDER[a] - SIZE_ORDER[b])


def _val(obj: Any, key: str, default: Any = None) -> Any:
    if isinstance(obj, dict):
        return obj.get(key, default)
    return getattr(obj, key, default)


def validate_editing(cuts: list[Any], scene: Any, *, strict: bool = False
                     ) -> tuple[list[str], list[str]]:
    """逐镜剪辑纪律。返回 (errors, warnings)。

    检查项：
      1) 时长下限：非 reaction/insert/pov 的动作镜 < MIN_ACTION_SECONDS 视为动作
         表达不完整；dialogue 镜按台词时长单独用 MIN_DIALOGUE_SECONDS 判定。
      2) 跳切：相邻镜同景别 + 同 coverage，机位视角过近。
      3) 动作断点：上一镜 cut_reason=action_done，下一镜无 action/reaction/establish 承接。
      4) 景别连跳：相邻镜景别跨度 ≥3 级且无过渡理由。
      5) 机位视角雷同（30°/20% 法则的近似）：同景别连续镜之间画面几乎不变。
    """
    errors: list[str] = []
    warnings: list[str] = []
    prev: Any = None
    for c in cuts:
        secs = _val(c, "seconds")
        cov = _val(c, "coverage", "") or ""
        size = _val(c, "size", "") or ""
        reason = _val(c, "cut_reason", "") or ""
        beats = _val(c, "beats")
        tag = str(beats) if beats else "?"
        # 1) 时长下限
        if secs is not None:
            try:
                secs = float(secs)
            except (TypeError, ValueError):
                secs = None
            if secs is not None and cov == "dialogue":
                if secs < MIN_DIALOGUE_SECONDS:
                    msg = (f"{tag} 镜 dialogue/{size} 仅 {secs:.1f}s，"
                           f"短于台词镜下限 {MIN_DIALOGUE_SECONDS}s，请补足停顿")
                    (errors if strict else warnings).append(msg)
            elif (secs is not None and secs < MIN_ACTION_SECONDS
                  and cov not in ("reaction", "insert", "pov")):
                msg = (f"{tag} 镜 {cov}/{size} 仅 {secs:.1f}s < {MIN_ACTION_SECONDS}s，"
                       f"动作表达不完整，请加长或拆镜")
                (errors if strict else warnings).append(msg)
        if prev is None:
            prev = c
            continue
        psize = _val(prev, "size", "") or ""
        pcov = _val(prev, "coverage", "") or ""
        preason = _val(prev, "cut_reason", "") or ""
        pbeats = _val(prev, "beats")
        ptag = str(pbeats) if pbeats else "?"
        # 2) 跳切：同景别 + 同 coverage
        if size and psize and size == psize and cov and pcov and cov == pcov:
            msg = (f"{ptag}→{tag} 同景别 {size} + 同 coverage {cov}，"
                   f"机位视角过近形成跳切，请改变景别或机位（30°/20% 法则）")
            (errors if strict else warnings).append(msg)
        # 3) 动作断点
        if preason == "action_done" and cov and cov not in _ACTION_REACTION:
            msg = (f"{ptag}(action_done) 后接 {tag}({cov})，动作无可见承接，"
                   f"请补承接镜/反应镜，别让关键动作悬空")
            (errors if strict else warnings).append(msg)
        # 4) 景别连跳
        gap = _size_gap(psize, size)
        if gap is not None and gap >= 3 and reason not in _TRANSITION_REASONS:
            msg = (f"{ptag}→{tag} 景别 {psize}→{size} 连跳 {gap} 级易跳切，"
                   f"需 new_info/pov/reaction/sound/time/match_cut 理由，或用过渡镜")
            (errors if strict else warnings).append(msg)
        prev = c
    return errors, warnings
