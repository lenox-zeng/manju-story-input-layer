# -*- coding: utf-8 -*-
"""故事层可视化报告：index.html + epNN.html + report.webp。

* 纯本地、零服务：HTML 内联 CSS，图片用 headless Chrome/Edge 截图。
* 长篇按季折叠：`SEASON_SIZE` 集一季，集页仍然独立可点开。
* 报告只读产物，不改任何契约/中间文件。
"""
from __future__ import annotations

import html
import json
import os
import shutil
import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

SEASON_SIZE = 40
_DIALOGUE = "口型：按台词逐字生成"


@dataclass
class ReportResult:
    index: Path
    episodes: list[Path] = field(default_factory=list)
    webp: Path | None = None
    screenshot: bool = False
    notes: list[str] = field(default_factory=list)


def _e(value: Any) -> str:
    return html.escape(str(value if value is not None else ""))


def _duration(ep: Any) -> float:
    return float(getattr(ep, "target_seconds", 0) or 0)


def _cut_total(board: Any) -> int:
    return sum(len(seg.cuts) for seg in getattr(board, "segments", []) or [])


def _lines(scene: Any, cut: Any) -> list[str]:
    a, b = cut.beats
    out = []
    for beat in scene.beats[a - 1:b]:
        if getattr(beat, "line", ""):
            who = getattr(beat, "speaker", "") or "?"
            out.append(f"{who}：{beat.line}")
    return out


def _state_summary(state: Any) -> str:
    bits = []
    for key, label in (("where", "位"), ("facing", "向"),
                       ("wardrobe", "装"), ("posture", "姿"),
                       ("props", "物"), ("injuries", "伤")):
        value = getattr(state, key, {}) or {}
        if value:
            bits.append(label + " " + "，".join(f"{k}={v}" for k, v in value.items()))
    return "；".join(bits) or "—"


def _seasons(episodes: list[Any]) -> list[list[Any]]:
    return [episodes[i:i + SEASON_SIZE] for i in range(0, len(episodes), SEASON_SIZE)]


CSS = """
:root{--bg:#0e1116;--card:#171c24;--line:#2a3342;--fg:#e7edf5;--mut:#93a1b5;
--acc:#f0b45a;--ok:#52c48a;--warn:#e3b341;--bad:#e06c75}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--fg);
font:14px/1.6 "Segoe UI","Microsoft YaHei",system-ui,sans-serif}
a{color:var(--acc);text-decoration:none}a:hover{text-decoration:underline}
.wrap{max-width:1180px;margin:0 auto;padding:28px 20px 60px}
h1{font-size:26px;margin:0 0 4px}h2{font-size:18px;margin:30px 0 12px}
h3{font-size:15px;margin:14px 0 8px}.mut{color:var(--mut)}
.kpis{display:grid;grid-template-columns:repeat(auto-fit,minmax(130px,1fr));gap:10px}
.kpi{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:12px}
.kpi b{display:block;font-size:22px;color:var(--acc)}
.band{display:flex;align-items:flex-end;gap:2px;height:80px;background:var(--card);
border:1px solid var(--line);border-radius:10px;padding:8px;overflow-x:auto}
.bar{flex:1 0 7px;background:var(--acc);border-radius:2px 2px 0 0;opacity:.8;
position:relative;min-height:2px}
.ep{background:var(--card);border:1px solid var(--line);border-radius:10px;
padding:12px;margin:8px 0;display:flex;gap:14px;align-items:flex-start}
.ep .no{font:700 18px/1 monospace;color:var(--acc);min-width:64px}
.ep .t{font-weight:600}.tags{display:flex;flex-wrap:wrap;gap:6px;margin-top:6px}
.tag{font-size:11px;border:1px solid var(--line);border-radius:99px;padding:1px 8px;
color:var(--mut)}.tag.ok{color:var(--ok);border-color:var(--ok)}
.tag.warn{color:var(--warn);border-color:var(--warn)}
.tag.bad{color:var(--bad);border-color:var(--bad)}
details{background:var(--card);border:1px solid var(--line);border-radius:10px;
padding:10px 12px;margin:10px 0}summary{cursor:pointer;font-weight:600}
.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(250px,1fr));gap:10px}
.card{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:12px}
.card .name{font-weight:600}.small{font-size:12px}.mono{font-family:Consolas,monospace}
table{border-collapse:collapse;width:100%;font-size:12px}
th,td{border:1px solid var(--line);padding:4px 6px;text-align:center}
th:first-child,td:first-child{text-align:left}
.shot{background:var(--card);border:1px solid var(--line);border-left:3px solid var(--acc);
border-radius:8px;padding:10px;margin:8px 0}
.shot .row{display:flex;gap:10px;flex-wrap:wrap;color:var(--mut);font-size:12px}
.shot .frame{margin:6px 0}.shot .en{color:var(--mut);font-size:12px;
font-family:Consolas,monospace}
.gate{font-size:12px;margin:2px 0}.gate.bad{color:var(--bad)}
.gate.warn{color:var(--warn)}.back{display:inline-block;margin-bottom:14px}
"""


def _gate(codes: Iterable[str], errors: bool) -> str:
    rows = list(codes)
    if not rows:
        return ""
    cls = "bad" if errors else "warn"
    return "".join(f'<div class="gate {cls}">{"✕" if errors else "!"} {_e(x)}</div>'
                   for x in rows)


def _render_index(outline: Any, cast: Any, art: Any,
                  scripts: list[Any], boards: dict[int, Any],
                  gates: dict[int, tuple[list[str], list[str]]],
                  title: str) -> str:
    eps = outline.episodes
    total_sec = sum(_duration(e) for e in eps)
    total_cuts = sum(_cut_total(boards.get(e.ep)) for e in eps)
    total_lines = sum(len([b for sc in s.scenes for b in sc.beats if b.line])
                      for s in scripts)
    bars = "".join(
        f'<div class="bar" style="height:{(_duration(e) / max(1.0, max(_duration(x) for x in eps))) * 100:.0f}%" '
        f'title="EP{e.ep:02d} {_duration(e):.0f}s"></div>' for e in eps)
    seasons = _seasons(list(eps))
    season_html = []
    for si, group in enumerate(seasons, 1):
        cards = []
        for e in group:
            errs, warns = gates.get(e.ep, ([], []))
            state = "bad" if errs else ("warn" if warns else "ok")
            cards.append(
                f'<div class="ep"><div class="no">EP{e.ep:02d}</div><div>'
                f'<div class="t"><a href="ep{e.ep:02d}.html">{_e(e.title)}</a></div>'
                f'<div class="small mut">{_e(e.synopsis)}</div>'
                f'<div class="tags"><span class="tag {state}">'
                f'{len(errs)} 错 / {len(warns)} 警</span>'
                f'<span class="tag">{_duration(e):.0f}s</span>'
                f'<span class="tag">{_cut_total(boards.get(e.ep))} 镜</span></div>'
                f'</div></div>')
        open_attr = " open" if si == 1 or len(seasons) <= 4 else ""
        season_html.append(
            f'<details{open_attr}><summary>第 {si} 季 · 第 {group[0].ep}–{group[-1].ep} 集'
            f'（{len(group)} 集）</summary>{"".join(cards)}</details>')

    # 角色卡 + 出场矩阵
    appearance: dict[str, set[int]] = {c.id: set() for c in cast.characters}
    for s in scripts:
        for sc in s.scenes:
            for cid in sc.characters:
                if cid in appearance:
                    appearance[cid].add(s.ep)
    chars = []
    for c in cast.characters:
        seen = sorted(appearance.get(c.id, set()))
        chars.append(
            f'<div class="card"><div class="name">{_e(c.name)} '
            f'<span class="small mut">{_e(c.id)} · {_e(c.importance)}</span></div>'
            f'<div class="small mut">{_e(c.one_liner or "")}</div>'
            f'<div class="small">出场：{len(seen)} 集'
            f'{("（" + "、".join(f"EP{x:02d}" for x in seen[:8]) + ("…" if len(seen) > 8 else "") + "）") if seen else ""}</div>'
            f'<div class="small mono mut">{_e((c.image.prompt or "")[:150])}</div></div>')
    matrix_rows = []
    for c in cast.characters:
        cells = "".join("<td>●</td>" if e.ep in appearance.get(c.id, set()) else "<td>·</td>"
                        for e in eps)
        matrix_rows.append(f"<tr><td>{_e(c.name)}</td>{cells}</tr>")
    matrix = ('<table><tr><th>角色</th>'
              + "".join(f"<th>{e.ep}</th>" for e in eps) + "</tr>"
              + "".join(matrix_rows) + "</table>")

    scenes = "".join(
        f'<div class="card"><div class="name">{_e(s.name)} '
        f'<span class="small mut">{_e(s.id)}</span></div>'
        f'<div class="small mono mut">{_e((s.image.prompt or "")[:180])}</div></div>'
        for s in art.scenes)
    props = "".join(
        f'<div class="card"><div class="name">{_e(p.name)} '
        f'<span class="small mut">{_e(p.id)}</span></div>'
        f'<div class="small mut">{_e(getattr(p, "function", "") or getattr(p, "summary", ""))}</div>'
        f'<div class="small mono mut">{_e((getattr(p.image, "prompt", "") or "")[:150])}</div></div>'
        for p in art.props)

    gate_rows = []
    for e in eps:
        errs, warns = gates.get(e.ep, ([], []))
        if errs or warns:
            gate_rows.append(f'<div class="card"><div class="name">EP{e.ep:02d}</div>'
                             f'{_gate(errs, True)}{_gate(warns, False)}</div>')

    return f"""<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{_e(title)} · 漫剧报告</title><style>{CSS}</style></head><body><div class="wrap">
<h1>{_e(title)}</h1><div class="mut">故事层报告 · {len(eps)} 集 ·
{len(cast.characters)} 角色 · {len(art.scenes)} 场景 · {len(art.props)} 道具</div>
<h2>总览</h2><div class="kpis">
<div class="kpi"><b>{len(eps)}</b>集数</div>
<div class="kpi"><b>{total_sec / 60:.1f}</b>总分钟</div>
<div class="kpi"><b>{total_cuts}</b>切镜</div>
<div class="kpi"><b>{total_lines}</b>台词</div>
<div class="kpi"><b>{len(cast.characters)}</b>角色</div>
<div class="kpi"><b>{len(art.scenes)}</b>场景</div>
</div>
<h2>分集节奏</h2><div class="band">{bars}</div>
<div class="small mut">柱高=单集目标时长；集页内是逐场分镜、台词口型与连续性状态。</div>
<h2>分集</h2>{''.join(season_html)}
<h2>角色与出场矩阵</h2><div class="grid">{''.join(chars)}</div>
<div style="overflow:auto;margin-top:12px">{matrix}</div>
<h2>场景</h2><div class="grid">{scenes}</div>
<h2>道具</h2><div class="grid">{props}</div>
<h2>质量门</h2>{''.join(gate_rows) or '<div class="mut">全部门通过。</div>'}
</div></body></html>"""


def _render_episode(ep: Any, script: Any, board: Any,
                    errors: list[str], warnings: list[str]) -> str:
    scenes = {i: sc for i, sc in enumerate(script.scenes, 1)}
    shots = []
    cut_no = 0
    for seg in (getattr(board, "segments", None) or []):
        scene = scenes.get(seg.scene_index)
        if scene is None:
            continue
        for cut in seg.cuts:
            cut_no += 1
            lines = _lines(scene, cut)
            dialogue = "".join(
                f'<div class="gate">▶ {_e(x)} <span class="mut">（{_DIALOGUE}）</span></div>'
                for x in lines) or '<div class="small mut">本镜无台词</div>'
            cont = _state_summary(cut.state_out)
            shots.append(
                f'<div class="shot"><div class="row"><b>#{cut_no:02d}</b>'
                f'<span>{_e(cut.size)}</span><span>{_e(cut.camera)}</span>'
                f'<span>{_e(cut.coverage)}</span><span>{cut.seconds:.1f}s</span>'
                f'<span>beats {cut.beats[0]}-{cut.beats[1]}</span>'
                f'<span>人物 {_e("、".join(cut.characters) or "—")}</span></div>'
                f'<div class="frame">{_e(cut.frame)}</div>'
                f'<div class="en">{_e(cut.prompt_en)}</div>'
                f'{dialogue}'
                f'<div class="small mut">连续性：{_e(cont)}</div></div>')
    scene_cards = "".join(
        f'<div class="card"><div class="name">第 {i} 场 {_e(sc.id)} · {_e(sc.beat)}</div>'
        f'<div class="small mut">目标：{_e(sc.goal)} / 阻碍：{_e(sc.obstacle)} / '
        f'转折：{_e(sc.turn)} / 代价：{_e(sc.cost)}</div></div>'
        for i, sc in enumerate(script.scenes, 1))
    status = "bad" if errors else ("warn" if warnings else "ok")
    return f"""<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>EP{ep.ep:02d} {_e(ep.title)}</title><style>{CSS}</style></head><body><div class="wrap">
<a class="back" href="index.html">← 返回总览</a>
<h1>EP{ep.ep:02d} · {_e(ep.title)}</h1>
<div class="mut">{_duration(ep):.0f}s · {len(script.scenes)} 场 ·
{cut_no} 镜 · <span class="tag {status}">质量门</span></div>
<h2>梗概</h2><div class="card">{_e(ep.synopsis)}</div>
<div class="tags"><span class="tag">钩子：{_e(ep.hook)}</span>
<span class="tag">悬念：{_e(ep.cliff)}</span></div>
<h2>场次</h2><div class="grid">{scene_cards}</div>
<h2>分镜卡</h2>{''.join(shots) or '<div class="mut">本集没有分镜。</div>'}
<h2>连续性 / 质量门</h2>{_gate(errors, True)}
{_gate(warnings, False) or '<div class="mut">无 warning。</div>'}
</div></body></html>"""


def _find_browser() -> str | None:
    candidates = [
        os.environ.get("MANJU_BROWSER", ""),
        os.environ.get("CHROME_PATH", ""),
        str(Path(os.environ.get("PROGRAMFILES", "")) /
            "Google/Chrome/Application/chrome.exe"),
        str(Path(os.environ.get("PROGRAMFILES(X86)", "")) /
            "Microsoft/Edge/Application/msedge.exe"),
        str(Path(os.environ.get("PROGRAMFILES", "")) /
            "Microsoft/Edge/Application/msedge.exe"),
        str(Path(os.environ.get("LOCALAPPDATA", "")) /
            "Google/Chrome/Application/chrome.exe"),
    ]
    names = ("chrome", "chrome.exe", "msedge", "msedge.exe", "chromium",
             "chromium-browser", "google-chrome")
    for name in names:
        hit = shutil.which(name)
        if hit:
            candidates.append(hit)
    for c in candidates:
        if c and Path(c).is_file():
            return c
    return None


def _screenshot(index: Path, webp: Path) -> tuple[bool, str]:
    browser = _find_browser()
    if not browser:
        return False, "未找到 Chrome/Edge；report.webp 用 Pillow 占位图"
    fd, tmp = tempfile.mkstemp(suffix=".png")
    os.close(fd)
    png = Path(tmp)
    cmd = [browser, "--headless=new", "--disable-gpu", "--hide-scrollbars",
           "--allow-file-access-from-files", "--window-size=1440,3200",
           f"--screenshot={png}", index.resolve().as_uri()]
    try:
        subprocess.run(cmd, check=True, timeout=90,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        from PIL import Image
        with Image.open(png) as im:
            im.convert("RGB").save(webp, "WEBP", quality=82, method=6)
        return True, ""
    except Exception as e:  # noqa: BLE001 - 截图失败不阻断报告
        try:
            webp.unlink()
        except OSError:
            pass
        return False, f"截图失败：{type(e).__name__}: {e}"
    finally:
        try:
            png.unlink()
        except OSError:
            pass


def _fallback_webp(webp: Path, kpis: list[tuple[str, str]]) -> None:
    from PIL import Image, ImageDraw
    img = Image.new("RGB", (1200, 760), (14, 17, 22))
    d = ImageDraw.Draw(img)
    d.rectangle((0, 0, 1200, 6), fill=(240, 180, 90))
    d.text((50, 40), "MANJU STORY REPORT", fill=(240, 180, 90))
    y = 110
    for k, v in kpis:
        d.text((50, y), f"{k}: {v}", fill=(231, 237, 245))
        y += 40
    d.text((50, y + 30), "(headless browser unavailable; text fallback)",
           fill=(147, 161, 181))
    img.save(webp, "WEBP", quality=82, method=6)


def build_report(outline: Any, cast: Any, art: Any,
                 scripts: list[Any], board: Any, *,
                 outdir: str | Path, title: str = "",
                 gates: dict[int, tuple[list[str], list[str]]] | None = None,
                 screenshot: bool = True) -> ReportResult:
    """写 index.html / epNN.html / report.webp。"""
    if not title:
        title = str(getattr(outline, "title", "") or "漫剧故事层")
    out = Path(outdir)
    out.mkdir(parents=True, exist_ok=True)
    boards = {b.ep: b for b in (getattr(board, "episodes", []) or [])}
    scripts_by_ep = {s.ep: s for s in scripts}
    gates = gates or {}

    index = out / "index.html"
    index.write_text(_render_index(outline, cast, art, scripts, boards,
                                   gates, title), encoding="utf-8")
    pages: list[Path] = []
    for ep in outline.episodes:
        script = scripts_by_ep.get(ep.ep)
        if script is None:
            continue
        errs, warns = gates.get(ep.ep, ([], []))
        page = out / f"ep{ep.ep:02d}.html"
        page.write_text(_render_episode(ep, script, boards.get(ep.ep),
                                        errs, warns), encoding="utf-8")
        pages.append(page)

    webp = out / "report.webp"
    ok = False
    notes: list[str] = []
    if screenshot:
        ok, note = _screenshot(index, webp)
        if note:
            notes.append(note)
    if not webp.is_file() or webp.stat().st_size == 0:
        webp.unlink(missing_ok=True)
        total_cuts = sum(_cut_total(boards.get(e.ep)) for e in outline.episodes)
        total_lines = sum(len([b for sc in s.scenes for b in sc.beats if b.line])
                          for s in scripts)
        _fallback_webp(webp, [
            ("episodes", str(len(outline.episodes))),
            ("shots", str(total_cuts)),
            ("lines", str(total_lines)),
            ("characters", str(len(cast.characters))),
            ("scenes", str(len(art.scenes))),
        ])
        notes.append("report.webp 为 Pillow 占位图，不是页面截图")
    return ReportResult(index=index, episodes=pages, webp=webp,
                        screenshot=ok, notes=notes)


# --------------------------------------------------------------------------
# 从中间产物目录重建
# --------------------------------------------------------------------------
def _load(path: Path, cls: Any) -> Any:
    return cls.model_validate(json.loads(path.read_text(encoding="utf-8-sig")))


def build_report_from_workdir(workdir: str | Path, outdir: str | Path,
                              *, screenshot: bool = True) -> ReportResult:
    from .story.models import (
        Art, Cast, EpisodeBatch, Outline, ScriptEpisode, StoryBible,
        StoryEpisodeBoard, StoryStoryboard,
    )

    wd = Path(workdir)
    bible = _load(wd / "bible.json", StoryBible)
    plans = []
    for path in sorted(wd.glob("episodes_batch_*.json")):
        plans.extend(_load(path, EpisodeBatch).episodes)
    if not plans:
        raise FileNotFoundError(f"{wd} 下没有 episodes_batch_*.json")
    outline = Outline(**bible.model_dump(), episodes=plans)
    cast = _load(wd / "cast.json", Cast)
    art = _load(wd / "art.json", Art)
    scripts = [_load(p, ScriptEpisode) for p in sorted(wd.glob("script_ep*.json"))]
    eps = [_load(p, StoryEpisodeBoard) for p in sorted(wd.glob("board_ep??.json"))]
    board = StoryStoryboard(source=outline.source, episodes=eps)

    from .story.validate import validate_board, validate_script
    gates: dict[int, tuple[list[str], list[str]]] = {}
    for s in scripts:
        reps = [validate_script(s, outline, cast, art, strict=False)]
        b = next((x for x in eps if x.ep == s.ep), None)
        if b is not None:
            reps.append(validate_board(b, s, cast))
        gates[s.ep] = ([x for r in reps for x in r.errors],
                       [x for r in reps for x in r.warnings])
    return build_report(outline, cast, art, scripts, board, outdir=outdir,
                        gates=gates, screenshot=screenshot)


def cli(argv: list[str] | None = None) -> int:
    import argparse

    ap = argparse.ArgumentParser(prog="python -m manju.report",
                                 description="从 state/story 中间产物生成 HTML/WebP 报告")
    ap.add_argument("--workdir", default="state/story")
    ap.add_argument("--out", default="state/report")
    ap.add_argument("--no-screenshot", action="store_true")
    args = ap.parse_args(argv)
    result = build_report_from_workdir(args.workdir, args.out,
                                       screenshot=not args.no_screenshot)
    print(f"[OK] {result.index}")
    for p in result.episodes:
        print(f"     {p}")
    print(f"     {result.webp}  screenshot={result.screenshot}")
    for note in result.notes:
        print(f"[WARN] {note}")
    return 0


if __name__ == "__main__":
    raise SystemExit(cli())
