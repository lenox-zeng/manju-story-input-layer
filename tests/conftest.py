"""pytest 公共夹具。

**关键：测试绝不碰真实项目目录。**
`MANJU_ROOT` 指向一个临时目录，并把真实的 `workflows/*.json` 与
`state/object_info.json`（ComfyUI 节点定义快照）拷进去。
这样工作流转换这类测试既能用真实数据、又完全可离线复现，
而且不会往项目 `state/` 里写垃圾。
"""

from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path

import pytest

REAL_ROOT = Path(__file__).resolve().parents[1]
if str(REAL_ROOT) not in sys.path:
    sys.path.insert(0, str(REAL_ROOT))
# 部分老测试写的是 `from factories import ...`（依赖 pytest 把 tests/ 塞进
# sys.path）。全局装过同名 `tests` 包时，`from tests import ...` 会解析到
# 别处，所以两边都显式放行。
TESTS_DIR = Path(__file__).resolve().parent
if str(TESTS_DIR) not in sys.path:
    sys.path.insert(1, str(TESTS_DIR))


@pytest.fixture()
def root(tmp_path, monkeypatch) -> Path:
    """隔离的项目根目录。"""
    (tmp_path / "workflows").mkdir()
    (tmp_path / "state").mkdir()
    (tmp_path / "output").mkdir()
    (tmp_path / "logs").mkdir()
    for f in (REAL_ROOT / "workflows").glob("*.json"):
        shutil.copy2(f, tmp_path / "workflows" / f.name)
    snap = REAL_ROOT / "state" / "object_info.json"
    if snap.is_file():
        shutil.copy2(snap, tmp_path / "state" / "object_info.json")

    monkeypatch.setenv("MANJU_ROOT", str(tmp_path))
    monkeypatch.setenv("PHOENIX_ENABLED", "0")  # 测试里不要真去连 OTLP
    monkeypatch.delenv("OTEL_TRACES_ENDPOINT", raising=False)

    from manju import config as config_mod
    config_mod._CACHED = None
    yield tmp_path
    config_mod._CACHED = None


@pytest.fixture()
def cfg(root):
    from manju.config import get_config
    c = get_config(reload=True)
    c.set("project_root", str(root))
    return c


@pytest.fixture()
def object_info(root) -> dict:
    p = root / "state" / "object_info.json"
    if not p.is_file():
        pytest.skip("没有 object_info 快照；先跑一次 `manju workflows` 抓取")
    return json.loads(p.read_text(encoding="utf-8"))


# --------------------------------------------------------------------------
# 交接对象的工厂：测试里造最小可用数据
# --------------------------------------------------------------------------
@pytest.fixture()
def plan() -> dict:
    """一份**最小但完整**的 v10.1 plan.json（系列圣经）。"""
    from manju.schemas import FORBIDDEN_BASELINE

    return {
        "schema_version": "v10.1",
        "plan_version": 1,
        "scope": "series",
        "project": "封印圣躯",
        "genre": "玄幻",
        "logline": "少年为救妹妹，解开自己身上的封印。",
        "target_audience": "18-35",
        "total_episodes": 1,

        "visual_bible": {
            "medium": "photorealistic cinematic film still",
            "film_stock": "Kodak Vision3 500T",
            "lens": "35mm anamorphic",
            "lighting": "low-key motivated practicals, hard rim light",
            "palette": "teal-orange, desaturated midtones, deep blacks",
            "texture": "fine film grain, subtle halation",
            "contrast": "medium-high contrast, protected highlights",
            "skin_tone": "natural warm skin, visible pores",
            "motion_character": "subtle handheld breathing",
            "depth_style": "shallow depth of field",
            "forbidden": list(FORBIDDEN_BASELINE),
            "style_ref_en": None,
        },
        "output": {
            "aspects": {
                "enabled": ["16:9", "9:16"],
                "primary": "16:9",
                "derivation": {"9:16": "reframe_crop"},
                "resolution_policy": "engine_default",
                "min_resolution": {"16:9": [1280, 720], "9:16": [720, 1280]},
            },
            "simultaneous": True,
            "episode_sec": 12,
            "target_platform": "抖音",
        },
        "requirements": {
            "identity_fidelity": "locked",
            "wardrobe_fidelity": "high",
            "temporal_stability": "series",
            "multi_subject_strategy": "auto",
            "interpolation": "none",
            "dubbing": "auto",
        },
        "cast_separation": {
            "co_occurrence_rule": "两个角色在任意一集任意一场的 chars_on_screen 中同时出现 ≥1 次，"
                                  "即构成『存在同框机会的角色对』",
            "rule": "存在同框机会的角色对，必须在至少 3 个维度上明显不同",
            "axes": ["身高(≥8cm差)", "发色", "发长", "体型", "年龄感", "服装主色"],
            "violations": [],
        },
        "characters": [
            {"char_id": "C1", "name": "阿元", "role": "主角", "status": "active",
             "desc_cn": "28 岁东亚男性，瘦高，灰色格纹衬衫",
             "desc_en": "a 28-year-old East Asian man, oval face, narrow eyes, "
                        "shoulder-length black hair tied back, lean build, "
                        "faded grey plaid cotton shirt",
             "identity_lock": {
                 "age_range": "27-30", "face_shape": "oval, high cheekbones",
                 "hair": "shoulder-length black, tied back",
                 "eyes": "single-lidded, narrow, dark brown",
                 "build": "lean, 182cm, narrow shoulders", "height_cm": 182,
                 "silhouette_cue": "lean frame, always left hand gloved",
                 "facial_marks": "thin scar across left eyebrow",
                 "skin": "warm undertone, visible pores",
             },
             "wardrobe_states": [
                 {"state_id": "C1-default",
                  "desc_en": "faded grey plaid cotton shirt with rolled sleeves",
                  "tags": ["日常"], "advisory_episodes": "1-40"},
                 {"state_id": "C1-combat",
                  "desc_en": "grey plaid shirt open over a black undershirt, "
                             "left hand gauntlet exposed",
                  "tags": ["战斗"], "advisory_episodes": "41-70"},
             ],
             "signature_props": ["black short baton"],
             "color_signature": "#4A5A6B", "voice_ref": "低沉、克制的青年男声"},
            {"char_id": "C2", "name": "小满", "role": "配角", "status": "active",
             "desc_cn": "16 岁少女，双马尾，白棉布裙",
             "desc_en": "a 16-year-old girl, round face, large round eyes, "
                        "twin chestnut ponytails, petite build, white cotton dress",
             "identity_lock": {
                 "age_range": "15-17", "face_shape": "round, soft jawline",
                 "hair": "chestnut twin ponytails, waist length",
                 "eyes": "large, round, hazel",
                 "build": "petite, 155cm", "height_cm": 155,
                 "silhouette_cue": "small frame, ribbon on left ponytail",
                 "facial_marks": "", "skin": "fair, light freckles",
             },
             "wardrobe_states": [
                 {"state_id": "C2-default", "desc_en": "white cotton dress",
                  "tags": ["日常"], "advisory_episodes": None},
             ],
             "signature_props": ["brass bell pendant"],
             "color_signature": "#E8DCC8", "voice_ref": "清亮少女声"},
        ],
        "episodes": [
            {"num": 1, "title": "解封", "summary": "阿元解开第一层封印", "hook": "血滴落祭坛"}
        ],
        "continuity": {
            "immutable": ["identity_lock 全部字段", "signature_props", "color_signature"],
            "mutable": ["wardrobe_states", "伤势", "持有道具", "人物关系"],
            "policy": "identity_lock 定稿后全剧不可就地修改；重大设定变更必须升 plan_version",
        },
        "hints": {},
    }


@pytest.fixture()
def script() -> dict:
    """一份最小但完整的 v10.1 script.json（每集）。"""
    return {
        "schema_version": "v10.1",
        "series_ref": "plan.json",
        "episode": 1,
        "title": "解封",
        "aspects_override": None,
        "requirements": {},
        "scenes": [
            {"scene_id": "S01", "location": "古庙", "time": "夜",
             "action": "阿元跪在祭坛前，掌心按在裂纹上",
             "scene_type": ["dialogue", "action"],
             "space_map": {"C1": {"pos": "center", "depth": "midground",
                                  "facing": "toward_camera", "note": "距祭坛约2米"}},
             "character_flags": {},
             "chars_on_screen": ["C1"],
             "wardrobe_state": {"C1": "C1-default"},
             "speech_cps": None,
             "lines": [{"speaker": "C1", "text": "我不能死。", "emotion": "冷硬",
                        "timing_sec": 1.5, "at_sec": 0.5}],
             "mood": "压抑", "duration_sec": 6,
             "beat": "钩子", "function": "把观众钉在祭坛这一下"},
            {"scene_id": "S02", "location": "古庙外", "time": "夜",
             "action": "小满奔来，在门槛外停住",
             "scene_type": ["dialogue"],
             "space_map": {"C2": {"pos": "center_left", "depth": "foreground",
                                  "facing": "toward_camera", "note": "门槛外"}},
             "character_flags": {},
             "chars_on_screen": ["C2"],
             "wardrobe_state": {"C2": "C2-default"},
             "lines": [{"speaker": "C2", "text": "哥！", "emotion": "急",
                        "timing_sec": 0.6, "at_sec": 6.4}],
             "mood": "紧张", "duration_sec": 6,
             "beat": "升级", "function": "把威胁带进画面"},
        ],
        "continuity_ledger": {
            "C1": {"换装": "C1-default", "伤势": "右掌灼伤", "获得道具": []},
        },
        "hook_chain": {"from_ep": 0, "to_ep": 2, "thread": "封印的来源"},
        "hints": {},
    }


@pytest.fixture()
def storyboard() -> dict:
    """一份最小但完整的 v10.1 storyboard.json（每集）。"""
    return {
        "schema_version": "v10.1",
        "episode": 1,
        "style_suffix": "",
        "shots": [
            {"shot_id": "SH01", "scene_id": "S01", "act_beat": "hook",
             "prompt": "a young man kneeling before a cracked stone altar, candlelight",
             "framing": {
                 "16:9": {"shot_size": "medium",
                          "composition": "阿元居中偏下，祭坛占画面上三分之一",
                          "subjects": [{"char": "C1", "pos": "center",
                                        "height_level": "low", "depth": "midground",
                                        "facing": "toward_camera"}]},
                 "9:16": {"shot_size": "medium_close",
                          "composition": "竖幅收窄，保留阿元与祭坛裂纹",
                          "subjects": [{"char": "C1", "pos": "center",
                                        "height_level": "low", "depth": "midground",
                                        "facing": "toward_camera"}]},
             },
             "subjects": [{"char": "C1", "wardrobe_state": "C1-default",
                           "pose": "kneeling, right palm pressed on the crack",
                           "expression": "jaw set, cold calm"}],
             "ref_characters": ["C1"],
             "requirements": {"quality_tier": "hero"},
             "aspect_overrides": None,
             "motion": "C1 pressing his right palm slowly onto the crack",
             "camera": "static", "camera_move": None,
             "audio": "低沉紧张 BGM", "sfx": "", "effect": "", "emotion": "压抑",
             "keyframe": "跪祭坛", "track": None, "stage_override": None,
             "start_sec": 0.0, "duration_sec": 6.0, "end_sec": 6.0,
             "verify_point": None, "segments": None, "hints": {}},
            {"shot_id": "SH02", "scene_id": "S02", "act_beat": "escalate",
             "prompt": "a girl running through a temple gate at night",
             "framing": {
                 "16:9": {"shot_size": "full",
                          "composition": "小满从画面右侧闯入，庙门在她身后",
                          "subjects": [{"char": "C2", "pos": "right",
                                        "depth": "foreground",
                                        "facing": "toward_camera"}]},
                 "9:16": {"shot_size": "full",
                          "composition": "竖幅收窄，小满居中偏右",
                          "subjects": [{"char": "C2", "pos": "center_right",
                                        "depth": "foreground",
                                        "facing": "toward_camera"}]},
             },
             "subjects": [{"char": "C2", "wardrobe_state": "C2-default",
                           "pose": "sprinting, skidding to a stop",
                           "expression": "wide-eyed alarm"}],
             "ref_characters": ["C2"],
             "requirements": {},
             "aspect_overrides": None,
             "motion": "C2 sprinting toward the altar",
             "camera": "handheld", "camera_move": "track",
             "audio": "急促脚步声", "sfx": "脚步声", "effect": "", "emotion": "紧张",
             "keyframe": "奔来", "track": None, "stage_override": None,
             "start_sec": 6.0, "duration_sec": 6.0, "end_sec": 12.0,
             "verify_point": None, "segments": None, "hints": {}},
        ],
        "scene_anchors": {"S01": "a dim temple interior with a cracked stone altar",
                          "S02": "a temple gate at night"},
        "hints": {},
    }


@pytest.fixture()
def video(tmp_path) -> dict:
    v = tmp_path / "ep01.mp4"
    v.write_bytes(b"\x00" * 2048)
    return {
        "episode": 1, "video_path": str(v), "timeline_json": "",
        "duration_sec": 12.0, "resolution": "832x1472", "fps": 24.0,
        "has_audio": True, "segments": [], "seed": 7,
    }
