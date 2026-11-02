# -*- coding: utf-8 -*-
"""分镜审核检查器（六层标准 L1-L6 可计算项）。

用法:
  python scripts/audit_storyboard.py <storyboard.json> [--script script.json] [--cast cast.json]

输出:
  汇总统计 + 逐镜问题 + 建议人工重点看镜头清单。
  退出码 0（报告性质）。
"""
import argparse
import json
import re
import sys
from collections import Counter, defaultdict

PLACEHOLDER = re.compile(r"(TODO|待补|占位|placeholder|xxx|\bTBD\b|\.\.\.$)", re.I)
NUMERIC = re.compile(
    r"(\d+(\.\d+)?\s*(秒|s|m|米|cm|mm|度|°|英尺|尺|步|paces|feet|meters|%|倍|x))|"
    r"(ten|half|two|three|four|five|six|seven|eight|nine|fifteen|twenty|thirty)\s+(paces|feet|meters|seconds|steps|degrees)",
    re.I)
STATIC_WORDS = re.compile(
    r"(static shot|static frame|still frame|no motion|no movement|fixed camera|定格|静止画面|纹丝不动|画面静止)", re.I)
ACTION_VERBS = re.compile(
    r"(推|拉|抓|跑|跳|摔|走|站起|坐下|转身|抬头|低头|抱|扔|捡|关|开|看|喊|哭|笑|跪|倒|冲|停|扑|滚|爬|躲|敲|撞|挥手|指|"
    r"walk|run|grab|push|pull|turn|look|reach|lean|open|close|lift|drop|step|rush|point|raise|hold|slip|release|"
    r"sink|slide|stiffen|swallow|catch|approach|halt|swing|sweep|bend|kneel|stand|rise|shift|move|enter|leave|flee|"
    r"creep|stretch|crouch|jump|throw|toss|grip|yank|wrench|shove)", re.I)


def norm(s):
    if s is None:
        return ""
    if isinstance(s, (dict, list)):
        return json.dumps(s, ensure_ascii=False)
    return str(s).strip()


def load(p):
    if not p:
        return None
    try:
        with open(p, encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:  # noqa: BLE001
        print(f"[ERROR] 读 {p} 失败: {e}")
        return None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("storyboard")
    ap.add_argument("--script", default=None)
    ap.add_argument("--cast", default=None)
    a = ap.parse_args()

    sb = load(a.storyboard)
    if not sb:
        sys.exit(1)
    shots = sb.get("shots") or []
    script = load(a.script)
    cast = load(a.cast)
    cast_chars = cast.get("characters") if cast else []
    valid_ids = {c.get("id") or c.get("char_id") for c in cast_chars if isinstance(c, dict)}
    print(f"== 分镜审计 {a.storyboard}")
    print(f"   镜头总数: {len(shots)} | cast 角色: {sorted(valid_ids)}")
    print()

    issues = defaultdict(list)  # level -> [(shot_id, msg)]
    stats = Counter()
    scene_shots = defaultdict(list)
    for s in shots:
        scene_shots[s.get("scene_id", "?")].append(s)

    # ---------- L4/L6 完整性 ----------
    for s in shots:
        sid = s.get("shot_id", "?")
        for f in ("prompt", "framing", "motion", "camera", "camera_move",
                  "emotion", "act_beat"):
            if not norm(s.get(f)):
                issues["L4"].append((sid, f"缺字段 {f}"))
                stats["missing_field"] += 1
        txt = " ".join(str(s.get(k)) for k in
                       ("prompt", "sfx", "audio", "narration", "effect",
                        "hints", "keyframe"))
        m = PLACEHOLDER.search(txt)
        if m:
            issues["L6"].append((sid, f"疑似占位符/未完成: {m.group(0)}"))
        if s.get("duration_sec") is None:
            issues["L4"].append((sid, "缺 duration_sec"))
        # segments 与时长对账
        segs = s.get("segments")
        if segs is not None and isinstance(segs, list):
            try:
                tsum = sum(float(x.get("duration_sec", 0)) for x in segs)
                if abs(tsum - float(s.get("duration_sec", 0))) > 0.6:
                    issues["L4"].append((sid, f"segments 总时长 {tsum:.1f}s ≠ duration {s.get('duration_sec')}s"))
                # 段区间连续不重叠
                prev_end = 0.0
                for x in segs:
                    tr = x.get("time_range")
                    if isinstance(tr, list) and len(tr) >= 2:
                        try:
                            st, en = float(tr[0]), float(tr[1])
                            if abs(st - prev_end) > 0.31:
                                issues["L4"].append(
                                    (sid, f"segments 区间不连续：{tr} 应从 {prev_end:.1f}s 起"))
                            prev_end = en
                        except (TypeError, ValueError):
                            pass
                # 每段最多 1 个主动作（一段一动作纪律）。
                # 兜底段（source=fallback，compile 动作组拆分）豁免该语义检查：
                # 确定性兜底无法做逐动作语义拆分，只保证结构（段和/连续/末帧）。
                for x in segs:
                    if x.get("source") == "fallback":
                        continue
                    nseg = len(set(ACTION_VERBS.findall(norm(x.get("text")))))
                    if nseg > 1:
                        issues["L4"].append((sid, f"segments 段 '{x.get('text', '')[:40]}' "
                                                  f"含 {nseg} 个动作动词，一段应只容一个主动作"))
            except (TypeError, ValueError):
                pass

    # ---------- L2 引用一致性（C 编号越界 / 内镜一致性） ----------
    for s in shots:
        sid = s.get("shot_id", "?")
        refs = set()
        def collect_refs(obj):
            if isinstance(obj, dict):
                c = obj.get("char")
                if c:
                    refs.add(c)
                for v in obj.values():
                    collect_refs(v)
            elif isinstance(obj, list):
                for v in obj:
                    collect_refs(v)
        collect_refs(s.get("subjects"))
        collect_refs(s.get("framing"))
        collect_refs(s.get("ref_characters"))
        # 人物越界
        bad = refs - valid_ids
        if bad:
            issues["L2"].append((sid, f"引用不在 cast 的角色 {sorted(bad)}（幻觉角色）"))
        # framing 9:16 / 16:9 两套 subjects 是否一致
        fr = s.get("framing")
        if isinstance(fr, dict):
            sub_sets = []
            for ratio, spec in fr.items():
                if isinstance(spec, dict):
                    sub_sets.append({x.get("char") for x in (spec.get("subjects") or []) if x.get("char")})
            if len(sub_sets) > 1 and sub_sets[0] != sub_sets[1]:
                issues["L2"].append((sid, f"9:16 与 16:9 的 subjects 不一致 {sub_sets}"))
        # subjects 与 ref_characters 是否一致
        subj = {x.get("char") for x in (s.get("subjects") or []) if x.get("char")} if isinstance(s.get("subjects"), list) else set()
        rc = set(s.get("ref_characters") or [])
        if subj and rc and subj != rc:
            issues["L2"].append((sid, f"subjects {sorted(subj)} ≠ ref_characters {sorted(rc)}"))

    # ---------- L4 时长 / prompt ----------
    for s in shots:
        sid = s.get("shot_id", "?")
        d = s.get("duration_sec")
        if d is not None:
            d = float(d)
            if d < 1.5:
                issues["L4"].append((sid, f"单镜 {d:.1f}s 过短（<1.5s，信息装不下）"))
            if d > 8:
                issues["L4"].append((sid, f"单镜 {d:.1f}s 过长（>8s，生成器易漂移）"))
        p = norm(s.get("prompt"))
        if p:
            n = len(p)
            if n < 40:
                issues["L4"].append((sid, f"prompt 仅 {n} 字符，信息密度不足"))
            if not NUMERIC.search(p):
                issues["L4"].append((sid, "prompt 无具体数字（距离/秒/角度），生成器难稳定执行"))
            if not ACTION_VERBS.search(p):
                issues["L4"].append((sid, "prompt 无可见动作动词，可能是静态/描述性空镜"))
            # 2026-10-30 时长 vs 动作数：动作多、秒数少 = 放不下（SH03 类超载）
            n_act = len(set(ACTION_VERBS.findall(p + " " + norm(s.get("motion")))))
            if n_act and d is not None:
                need = n_act * 1.5
                if d + 1e-6 < need:
                    issues["L4"].append(
                        (sid, f"{n_act} 个动作动词，时长应 ≥{need:.1f}s"
                              f"（{n_act}×1.5s），实际 {d:.1f}s——内容放不下，"
                              "应加时长或拆段/拆镜"))
            if STATIC_WORDS.search(p) and str(s.get("motion", "")).strip():
                issues["L4"].append((sid, "prompt 含静态词但 motion 非空，疑似冲突"))

    # ---------- L3 镜头语言 ----------
    size_seq = []
    for sc in sorted(scene_shots):
        sizes = []
        for s in scene_shots[sc]:
            fr = s.get("framing")
            size = None
            if isinstance(fr, dict):
                sp = fr.get("9:16") or fr.get("16:9") or {}
                size = sp.get("shot_size") if isinstance(sp, dict) else None
            size = size or "?"
            sizes.append(size)
            size_seq.append(size)
        cnt = Counter(sizes)
        if len(cnt) <= 1:
            issues["L3"].append((f"{sc}", f"全场景别单一 {dict(cnt)}（无变化，视觉枯燥）"))
        # 连续相同景别 run
        run = 1
        for i in range(1, len(sizes)):
            if sizes[i] == sizes[i - 1] and sizes[i] != "?":
                run += 1
            else:
                if run >= 4:
                    issues["L3"].append((f"{sc}", f"连续 {run} 镜同景别 {sizes[i-1]}（建议换景别/切角度）"))
                run = 1
        if run >= 4:
            issues["L3"].append((f"{sc}", f"连续 {run} 镜同景别 {sizes[-1]}（建议换景别/切角度）"))
    cam_moves = Counter(norm(s.get("camera_move")) for s in shots)
    static_moves = [s.get("shot_id") for s in shots
                    if STATIC_WORDS.search(norm(str(s.get("motion"))))]
    if static_moves:
        issues["L3"].append(("ALL", f"静态/静止运镜镜 {len(static_moves)} 个: {static_moves}"))

    # ---------- L1 叙事（可算部分） ----------
    beats = Counter(norm(s.get("act_beat")) for s in shots)
    missing_emotion = [s.get("shot_id") for s in shots if not norm(s.get("emotion"))]
    if missing_emotion:
        issues["L1"].append(("ALL", f"缺 emotion 镜 {len(missing_emotion)} 个: {missing_emotion}"))

    # ---------- L5 音画（可算部分） ----------
    no_sfx = [s.get("shot_id") for s in shots if not norm(s.get("sfx"))]
    if len(no_sfx) > len(shots) * 0.5:
        issues["L5"].append(("ALL", f"{len(no_sfx)}/{len(shots)} 镜无 sfx（超过一半，音效铺陈不足）"))
    # audio 只在“有台词镜”里必须非空（同期声）；无台词镜 audio 空是合理设计
    line_scenes = set()
    if script:
        for sc in (script.get("scenes") or []):
            if sc.get("lines"):
                line_scenes.add(sc.get("scene_id") or sc.get("id"))
    for s in shots:
        if s.get("scene_id") in line_scenes and not norm(s.get("audio")):
            issues["L5"].append((s.get("shot_id"),
                                 f"有台词场 {s.get('scene_id')} 但该镜无 audio 同期声标注"))

    # ---------- 台词时长 vs 镜头时长（需 script） ----------
    if script:
        scenes = script.get("scenes") or []
        line_by_scene = {}
        for sc in scenes:
            lns = []
            for ln in sc.get("lines", []):
                lns.append((ln.get("speaker"), ln.get("text") or "",
                            float(ln.get("timing_sec", 0))))
            line_by_scene[sc.get("scene_id")] = lns
        for sc in sorted(scene_shots):
            ss = scene_shots[sc]
            dur = sum(float(x.get("duration_sec", 0)) for x in ss)
            lns = line_by_scene.get(sc, [])
            speak = sum(ln[2] for ln in lns)
            if lns and speak > dur + 2:
                issues["L5"].append((f"{sc}", f"台词 {speak:.1f}s > 镜头时长 {dur:.1f}s（台词装不下）"))

    # ---------- 输出 ----------
    order = ["L1", "L2", "L3", "L4", "L5", "L6"]
    total = 0
    for lv in order:
        items = issues.get(lv, [])
        if not items:
            continue
        total += len(items)
        print(f"--- {lv} （{len(items)} 项） ---")
        for sid, msg in items:
            print(f"  [{sid}] {msg}")
    print()
    print(f"== 汇总：{total} 项问题 | 景别分布 {dict(Counter(size_seq))} | 运镜分布 {dict(cam_moves)} | 节拍分布 {dict(beats)}")
    print()
    # 建议人工重点看：只列硬性问题（引用/台词同期声/占位符/时长异常），数字类提示不占名额
    hot = set()
    for lv in ("L2", "L5", "L6"):
        for sid, _ in issues.get(lv, []):
            hot.add(sid.split(" ")[0])
    for s in shots:
        d = s.get("duration_sec")
        if d is not None and (float(d) < 1.5 or float(d) > 8):
            hot.add(s.get("shot_id"))
    if hot:
        print("== 建议人工重点核对的镜：", sorted(hot))
    print("== 人工必审（脚本无法判）：叙事连贯性、情绪准确性、运镜表达目的、")
    print("   动作承接语义（上镜末→下镜初）、音画主观匹配、C 编号语义是否真对得上角色")


if __name__ == "__main__":
    main()
