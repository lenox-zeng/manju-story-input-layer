# -*- coding: utf-8 -*-
"""审计一次生成产物：must_show 覆盖、场间因果、关键道具特写、关键拍运镜。

只读，不调用任何模型。优先读内部产物 `state/story/<小说>/`（字段最全），
也兼容只编译后的 `input/<小说>/`；旧产物没有 must_show/emphasis 字段时
标记为 SKIP，而不是误报失败。

用法：
    python scripts/audit_run.py --novel my_story
    python scripts/audit_run.py --dir state/story/my_story
    python scripts/audit_run.py --dir input/my_story --json
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path
from typing import Any

DEFAULT_ROOT = Path(__file__).resolve().parents[1]
ROOT = Path(os.environ.get("MANJU_ROOT") or DEFAULT_ROOT).resolve()

CLOSE_SIZES = {"close", "extreme-close"}
EMPHASIS = {"none", "highlight", "key_prop", "climax"}


# --------------------------------------------------------------------------
# 小工具：读取 / 事件匹配（与故事层质量门同口径）
# --------------------------------------------------------------------------
def _load(path: Path) -> dict[str, Any] | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) else None


def _key(text: str) -> str:
    return re.sub(r"[\W_]+", "", str(text or "").lower())


def _event_match(a: str, b: str) -> bool:
    ka, kb = _key(a), _key(b)
    if not ka or not kb:
        return False
    if ka == kb:
        return True
    return ((len(ka) >= 4 and ka in kb)
            or (len(kb) >= 4 and kb in ka))


def _event_score(text: str, event: str) -> float:
    key, hay = _key(event), _key(text)
    if not key or not hay:
        return 0.0
    if key in hay:
        return 1.0
    grams = {key[i:i + 2] for i in range(max(1, len(key) - 1))}
    return sum(1 for g in grams if g in hay) / len(grams) if grams else 0.0


def _same_event(a: str, b: str) -> bool:
    return (_event_match(a, b) or _event_score(a, b) >= 0.6
            or _event_score(b, a) >= 0.6)


def _ep_num(obj: dict[str, Any], default: int) -> int:
    for key in ("ep", "num", "episode"):
        val = obj.get(key)
        if isinstance(val, int) and val > 0:
            return val
    return default


def _scene_text(scene: dict[str, Any]) -> str:
    parts = [str(scene.get(k) or "") for k in
             ("location", "goal", "obstacle", "turn", "cost", "information",
              "mood", "major_event")]
    for beat in scene.get("beats") or []:
        if isinstance(beat, dict):
            parts.extend(str(beat.get(k) or "") for k in
                         ("action", "line", "visual_focus"))
    return " ".join(x for x in parts if x)


def _beat_has_emphasis(beat: dict[str, Any], event: str = "") -> bool:
    emphasis = beat.get("emphasis") or "none"
    if emphasis not in {"highlight", "key_prop", "climax"}:
        return False
    if not event:
        return True
    focus = str(beat.get("visual_focus") or "")
    action = str(beat.get("action") or "")
    return bool(focus and _same_event(focus, event)) or bool(
        action and _same_event(action, event))


# --------------------------------------------------------------------------
# 定位产物
# --------------------------------------------------------------------------
def _layout(directory: Path) -> str:
    if (directory / "outline.json").is_file():
        return "internal"
    if (directory / "external" / "plan.json").is_file():
        return "compiled"
    return "unknown"


def _load_outline(directory: Path, layout: str) -> dict[str, Any] | None:
    if layout == "internal":
        return _load(directory / "outline.json")
    return _load(directory / "external" / "plan.json")


def _script_path(directory: Path, layout: str, ep: int) -> Path:
    if layout == "internal":
        return directory / f"script_ep{ep:02d}.json"
    return directory / "external" / f"ep{ep:02d}" / "script.json"


def _board_path(directory: Path, layout: str, ep: int) -> Path:
    if layout == "internal":
        return directory / f"board_ep{ep:02d}.json"
    return directory / f"ep{ep:02d}" / "storyboard.json"


def _discover_episodes(directory: Path, layout: str) -> list[int]:
    nums: set[int] = set()
    if layout == "internal":
        for p in directory.glob("script_ep*.json"):
            m = re.search(r"script_ep(\d+)\.json$", p.name)
            if m:
                nums.add(int(m.group(1)))
        for p in directory.glob("board_ep*.json"):
            m = re.search(r"board_ep(\d+)\.json$", p.name)
            if m:
                nums.add(int(m.group(1)))
    elif layout == "compiled":
        for p in (directory / "external").glob("ep*/script.json"):
            m = re.search(r"ep(\d+)", p.parent.name)
            if m:
                nums.add(int(m.group(1)))
    return sorted(nums)


def _novel_dirs(root: Path) -> list[str]:
    names: set[str] = set()
    for base in (root / "state" / "story", root / "input"):
        if not base.is_dir():
            continue
        for p in base.iterdir():
            if not p.is_dir() or p.name.startswith("."):
                continue
            if _layout(p) != "unknown":
                names.add(p.name)
    return sorted(names)


def _resolve(root: Path, novel: str | None,
             directory: str | None) -> tuple[str, Path | None, str]:
    if directory:
        d = Path(directory)
        if not d.is_absolute():
            d = root / d
        if not d.is_dir():
            return (d.name, None, "missing")
        return d.name, d, "explicit"

    if not novel:
        return ("", None, "missing")
    for base, kind in ((root / "state" / "story" / novel, "internal"),
                       (root / "state" / "story" / (".work_" + novel),
                        "internal-work"),
                       (root / "input" / novel, "compiled")):
        if base.is_dir() and _layout(base) != "unknown":
            return novel, base, kind
    return novel, None, "missing"


# --------------------------------------------------------------------------
# 审计
# --------------------------------------------------------------------------
def audit(root: Path | str | None = None, novel: str | None = None,
          directory: str | None = None) -> dict[str, Any]:
    """返回结构化审计结果。不抛异常、不调模型。"""
    root = Path(root).resolve() if root else ROOT
    name, base, source = _resolve(root, novel, directory)
    result: dict[str, Any] = {
        "novel": name or novel or "",
        "source": source,
        "path": str(base) if base else "",
        "plot_mode": False,
        "episodes": [],
        "issues": [],
        "warnings": [],
        "verdict": "SKIP",
    }
    if base is None:
        result["issues"].append(f"找不到可审计的产物：{novel or directory}")
        result["verdict"] = "FAIL"
        return result

    layout = _layout(base)
    result["layout"] = layout
    outline = _load_outline(base, layout)
    outline_eps: list[dict[str, Any]] = []
    if outline:
        raw = outline.get("episodes") or []
        outline_eps = [e for e in raw if isinstance(e, dict)]
    result["plot_mode"] = any(e.get("must_show") or e.get("key_props")
                              for e in outline_eps)
    if not result["plot_mode"]:
        result["warnings"].append(
            "大纲没有 must_show/key_props（旧产物或非情节模式）；"
            "情节覆盖与emphasis 审计跳过。建议用 v2.0 情节模式重跑。")

    by_ep: dict[int, dict[str, Any]] = {}
    for idx, ep in enumerate(outline_eps, 1):
        by_ep[_ep_num(ep, idx)] = ep
    for ep in _discover_episodes(base, layout):
        by_ep.setdefault(ep, {"ep": ep})

    if not by_ep:
        result["issues"].append(
            "没有找到 outline.json / script_ep*.json / external/plan.json。")
        result["verdict"] = "FAIL"
        return result

    for ep_num in sorted(by_ep):
        ep = by_ep[ep_num]
        must_show = [str(x) for x in (ep.get("must_show") or []) if str(x).strip()]
        key_props = [str(x) for x in (ep.get("key_props") or []) if str(x).strip()]
        row: dict[str, Any] = {
            "ep": ep_num,
            "title": ep.get("title", ""),
            "must_show_total": len(must_show),
            "must_show_hit": 0,
            "must_show_emphasized": 0,
            "key_props_total": len(key_props),
            "key_props_script_ok": 0,
            "key_props_board_ok": 0,
            "causal_missing": 0,
            "scenes": 0,
            "beats": 0,
            "cuts": 0,
            "static_emphasis": 0,
            "key_prop_not_close": 0,
            "script_legacy": False,
            "board_legacy": False,
            "issues": [],
        }
        ep_issues: list[str] = row["issues"]

        legacy = not result["plot_mode"]
        script = _load(_script_path(base, layout, ep_num))
        if script is None:
            msg = f"EP{ep_num}: 缺少剧本产物"
            (result["warnings"] if legacy else ep_issues).append(msg)
            result["episodes"].append(row)
            continue

        scenes = [s for s in (script.get("scenes") or []) if isinstance(s, dict)]
        row["scenes"] = len(scenes)
        row["beats"] = sum(len(s.get("beats") or []) for s in scenes)
        row["script_legacy"] = not (
            script.get("must_show")
            or any((b.get("emphasis") or "none") != "none" or b.get("visual_focus")
                   for s in scenes for b in (s.get("beats") or [])
                   if isinstance(b, dict))
        )

        check_script = result["plot_mode"] and not row["script_legacy"]

        # ---- must_show：每条都必须被某场的 major_event 认领 ----
        if check_script:
            for event in must_show:
                claimed = [s for s in scenes
                           if _same_event(s.get("major_event") or "", event)]
                if claimed:
                    row["must_show_hit"] += 1
                    if any(_beat_has_emphasis(b) for s in claimed
                           for b in (s.get("beats") or [])
                           if isinstance(b, dict)):
                        row["must_show_emphasized"] += 1
                else:
                    ep_issues.append(
                        f"EP{ep_num}: must_show 未被任何场认领：{event}")

            # ---- 场间因果：第 2 场起必须有 causal_from ----
            for idx, scene in enumerate(scenes[1:], 2):
                if not str(scene.get("causal_from") or "").strip():
                    row["causal_missing"] += 1
                    ep_issues.append(
                        f"EP{ep_num}: S{idx:02d} 缺 causal_from（场间跳接）")

        # ---- 关键道具：剧本 insert 特写 + 分镜 close/非静态 ----
        board = _load(_board_path(base, layout, ep_num))
        cuts: list[dict[str, Any]] = []
        if board:
            for seg in board.get("segments") or []:
                if isinstance(seg, dict):
                    cuts.extend(c for c in (seg.get("cuts") or [])
                                if isinstance(c, dict))
        row["cuts"] = len(cuts)
        row["board_legacy"] = bool(cuts) and not any(
            (c.get("emphasis") or "none") != "none" or c.get("visual_focus")
            for c in cuts)
        check_board = check_script and board is not None and not row["board_legacy"]

        if check_script:
            for prop in key_props:
                script_ok = False
                for scene in scenes:
                    for beat in scene.get("beats") or []:
                        if not isinstance(beat, dict):
                            continue
                        if (beat.get("emphasis") or "none") != "key_prop":
                            continue
                        if (beat.get("coverage") or "") != "insert":
                            continue
                        if _same_event(beat.get("visual_focus") or "", prop) or \
                                _same_event(beat.get("action") or "", prop):
                            script_ok = True
                            break
                    if script_ok:
                        break
                if script_ok:
                    row["key_props_script_ok"] += 1
                else:
                    ep_issues.append(
                        f"EP{ep_num}: 关键道具没有 insert 特写：{prop}")

                if not check_board:
                    continue
                board_ok = False
                for cut in cuts:
                    if (cut.get("emphasis") or "none") != "key_prop":
                        continue
                    if not _same_event(cut.get("visual_focus") or "", prop):
                        continue
                    if cut.get("size") not in CLOSE_SIZES:
                        continue
                    if cut.get("camera") == "Static Shot":
                        continue
                    board_ok = True
                    break
                if board_ok:
                    row["key_props_board_ok"] += 1
                else:
                    ep_issues.append(
                        f"EP{ep_num}: 分镜没有给关键道具 close + 运镜：{prop}")

        # ---- 分镜：emphasis 拍必须非静态；key_prop 必须 close ----
        if board is None:
            if check_script:
                ep_issues.append(
                    f"EP{ep_num}: 缺少分镜产物 board_ep{ep_num:02d}.json")
        else:
            if check_script and row["board_legacy"]:
                ep_issues.append(
                    f"EP{ep_num}: 分镜缺 emphasis/visual_focus（旧产物）")
            for cut in (cuts if check_board else []):
                emphasis = cut.get("emphasis") or "none"
                if emphasis not in EMPHASIS:
                    ep_issues.append(
                        f"EP{ep_num}: 分镜 emphasis 非法：{emphasis!r}")
                    continue
                if (emphasis in {"highlight", "key_prop", "climax"}
                        and cut.get("camera") == "Static Shot"):
                    row["static_emphasis"] += 1
                    ep_issues.append(
                        f"EP{ep_num}: {emphasis} 镜仍是 Static Shot"
                        f"（{cut.get('visual_focus') or cut.get('shot') or ''}）")
                if (emphasis == "key_prop"
                        and cut.get("size") not in CLOSE_SIZES):
                    row["key_prop_not_close"] += 1
                    ep_issues.append(
                        f"EP{ep_num}: key_prop 镜景别不是 close："
                        f"{cut.get('size')}（{cut.get('visual_focus') or ''}）")
            if check_script and not cuts:
                ep_issues.append(f"EP{ep_num}: 分镜没有 cuts")

        if result["plot_mode"] and row["script_legacy"]:
            ep_issues.append(
                f"EP{ep_num}: 剧本缺 must_show/emphasis（旧产物）")

        result["episodes"].append(row)

    all_issues = [x for row in result["episodes"] for x in row["issues"]]
    result["issues"] = all_issues
    if not result["plot_mode"]:
        result["verdict"] = "SKIP"
    elif all_issues:
        result["verdict"] = "FAIL"
    else:
        result["verdict"] = "PASS"
    return result


# --------------------------------------------------------------------------
# 输出
# --------------------------------------------------------------------------
def _fmt(a: int, b: int) -> str:
    return f"{a}/{b}" if b else "-"


def render(result: dict[str, Any]) -> str:
    lines = [
        f"novel : {result.get('novel') or '-'}",
        f"source: {result.get('source')} {result.get('path') or ''}".rstrip(),
        f"layout: {result.get('layout') or '-'}   "
        f"plot_mode: {'yes' if result.get('plot_mode') else 'no'}",
    ]
    for w in result.get("warnings") or []:
        lines.append(f"  [WARN] {w}")
    lines.append("")
    header = ("ep  must_show  emph  key_prop(script/board)  causal  "
              "static  kp_close  scenes  beats  cuts")
    lines.append(header)
    lines.append("-" * len(header))
    for row in result.get("episodes") or []:
        lines.append(
            f"{row['ep']:<3} "
            f"{_fmt(row['must_show_hit'], row['must_show_total']):<10} "
            f"{_fmt(row['must_show_emphasized'], row['must_show_total']):<5} "
            f"{_fmt(row['key_props_script_ok'], row['key_props_total'])}/"
            f"{_fmt(row['key_props_board_ok'], row['key_props_total']):<14} "
            f"{row['causal_missing']:<7} "
            f"{row['static_emphasis']:<7} "
            f"{row['key_prop_not_close']:<9} "
            f"{row['scenes']:<7} {row['beats']:<6} {row['cuts']}"
        )
    lines.append("")
    issues = result.get("issues") or []
    if issues:
        lines.append(f"issues ({len(issues)}):")
        lines.extend(f"  - {x}" for x in issues)
    else:
        lines.append("issues: none")
    lines.append(f"\nverdict: {result.get('verdict')}")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:  # noqa: BLE001
        pass
    p = argparse.ArgumentParser(
        prog="python scripts/audit_run.py",
        description="审计 must_show / 因果 / 关键道具特写 / 关键拍运镜（只读）")
    p.add_argument("--novel", help="小说名，默认读 state/story/<小说>（回退 input/<小说>）")
    p.add_argument("--dir", help="直接指定产物目录（内部 state 目录或编译后 input 目录）")
    p.add_argument("--root", default=None, help="项目根目录；默认 MANJU_ROOT 或脚本上一级")
    p.add_argument("--json", action="store_true", help="输出 JSON")
    a = p.parse_args(argv)

    root = Path(a.root).resolve() if a.root else ROOT
    if not a.novel and not a.dir:
        names = _novel_dirs(root)
        print("用法：python scripts/audit_run.py --novel <小说名> 或 --dir <产物目录>")
        if names:
            print("可用小说：" + "、".join(names))
        return 2

    result = audit(root=root, novel=a.novel, directory=a.dir)
    if a.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        print(render(result))
    return 0 if result["verdict"] in {"PASS", "SKIP"} else 1


if __name__ == "__main__":
    raise SystemExit(main())
