"""引擎能力档案：同一套叙事/分镜规则，按渲染引擎给出可执行参数。

分层原则（与 README 架构一致）：
- 叙事层（分集完整性、关键台词保留、场间因果、一镜一信息）**不随引擎变化**；
- 本文件只定义"引擎能力"差异：单镜时长、同框人脸、口型、运镜幅度、提示词语言。
  引擎差异通过 ScriptParams 的**默认值**注入，不产生两套提示词逻辑。

配置方式：config.json 的 ``story.engine`` 取值
``""``（通用默认，现有行为）/ ``"comfyui"`` / ``"seedance"`` / ``"jimeng"``。
用户在 config 里**显式配置**的字段优先；未显式配置的字段取档案推荐值。
"""

from __future__ import annotations

from typing import Any

# camera_motion:
#   full    = 允许推拉摇移跟环绕（Seedance 系云端引擎，运镜指令可靠）；
#   limited = 只允许轻度运镜（Push In/Tilt/Pedestal），禁大幅运动
#             （ComfyUI 系局部模型/AnimateDiff 大幅运动易形变）。
ENGINE_PROFILES: dict[str, dict[str, Any]] = {
    "": {
        "label": "通用默认（现有行为）",
        "params": {},
    },
    "comfyui": {
        "label": "ComfyUI 本地管线（PuLID/Wav2Lip/AnimateDiff 系）",
        "params": {
            "max_shot_seconds": 4.0,       # 本地逐镜渲染成本高，偏好碎镜
            "min_shot_seconds": 1.0,
            "max_segment_seconds": 12.0,
            "max_faces_per_shot": 1,       # PuLID 通常单参考图单脸 → 对话拆正反打
            "max_lines_per_shot": 1,       # 外部口型节点长句易对不准
            "max_line_chars": 20,          # 单句限短，保住口型对齐
            "camera_motion": "limited",
            "max_actions_per_shot": 1,     # 单镜最多 1 个主动作；更多必须拆段/拆镜
            "action_seconds_per_beat": 1.5,  # 每个动作节拍的最低秒数（时长下限基准）
            "pause_seconds_per_line": 0.7,   # 每句台词后的停顿秒数
            "duration_tiers": [2.0, 3.0, 4.0],  # 向上对齐档位（25fps: 49/73/97 帧）
        },
    },
    "seedance": {
        "label": "Seedance 云端文生视频（原生 lipsync / 运镜指令）",
        "params": {
            "max_shot_seconds": 8.0,       # 情绪/关键对话镜可给足时长
            "min_shot_seconds": 1.0,
            "max_segment_seconds": 15.0,
            "max_faces_per_shot": 3,       # 对话镜可双人同框正反打
            "max_lines_per_shot": 3,
            "max_line_chars": 40,          # 关键台词可完整成句，一集全是对话可行
            "camera_motion": "full",       # 必须有运镜（推拉摇移跟环绕）
            "max_actions_per_shot": 2,     # 云端长镜可承载 2 个顺序动作，更多必须拆
            "action_seconds_per_beat": 1.5,
            "pause_seconds_per_line": 0.7,
            "duration_tiers": None,        # 任意整数秒，不设档位表
        },
    },
    "jimeng": {
        "label": "即梦 AI 短剧工作流",
        "params": {
            "max_shot_seconds": 6.0,
            "min_shot_seconds": 1.0,
            "max_segment_seconds": 15.0,
            "max_faces_per_shot": 2,
            "max_lines_per_shot": 2,
            "max_line_chars": 30,
            "camera_motion": "full",
            "max_actions_per_shot": 2,
            "action_seconds_per_beat": 1.5,
            "pause_seconds_per_line": 0.7,
            "duration_tiers": None,
        },
    },
}


def engine_profile(name: str | None) -> dict[str, Any]:
    """按 engine 名返回档案参数；未知名称回退通用默认（不报错，避免配置笔误炸管线）。"""
    key = (name or "").strip().lower()
    entry = ENGINE_PROFILES.get(key, ENGINE_PROFILES[""])
    return dict(entry["params"])
