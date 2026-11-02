# -*- coding: utf-8 -*-
"""人工审阅节点：大纲审批、角色身份图审批、质量门失败包。

设计原则：
* 审批是**可选阻塞点**，只覆盖影响半径最大的对象；
* 审批记录带内容哈希，上游一改就自动失效；
* 所有产物落在 `state/review/`，不碰模型、不需要 GPU。
"""
from __future__ import annotations

import hashlib
import html
import json
import os
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

OUTLINE = "outline"
ASSETS = "assets"


@dataclass
class ReviewResult:
    index: Path
    markdown: Path


def _now() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def review_root(cfg: Any) -> Path:
    p = Path(cfg.p("state")) / "review"
    p.mkdir(parents=True, exist_ok=True)
    return p


def _sha_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def _canonical(data: Any) -> bytes:
    return json.dumps(data, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":")).encode("utf-8")


def _data_hash(data: Any) -> str:
    return hashlib.sha256(_canonical(data)).hexdigest()


def outline_hash(path: str | Path) -> str:
    """对 outline.json 做规范化哈希；只改缩进/键序不会误判。"""
    p = Path(path)
    return _data_hash(json.loads(p.read_text(encoding="utf-8-sig")))


def _model_hash(obj: Any) -> str:
    if hasattr(obj, "model_dump"):
        return _data_hash(obj.model_dump(mode="json"))
    return _data_hash(vars(obj))


def _load_json(path: Path, default: Any) -> Any:
    if not path.is_file():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8-sig"))
    except Exception:  # noqa: BLE001 - 坏文件按缺失处理，不能阻断审批
        return default


def _save_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n",
                    encoding="utf-8")


def ledger_path(cfg: Any) -> Path:
    return review_root(cfg) / "approvals.json"


def _marker(cfg: Any, stage: str) -> Path:
    return review_root(cfg) / f"{stage}_required.json"


def marker_path(cfg: Any, stage: str) -> Path:
    return _marker(cfg, stage)


def load_ledger(cfg: Any) -> dict[str, Any]:
    return _load_json(ledger_path(cfg), {})


def _same_path(a: Any, b: Any) -> bool:
    if not a or not b:
        return False
    try:
        return Path(a).resolve() == Path(b).resolve()
    except OSError:
        return str(a) == str(b)


def _update_ledger(cfg: Any, stage: str, entry: dict[str, Any]) -> None:
    data = load_ledger(cfg)
    data[stage] = entry
    _save_json(ledger_path(cfg), data)


# --------------------------------------------------------------------------
# 大纲审批
# --------------------------------------------------------------------------
def request_outline_review(cfg: Any, outline: Any,
                           workdir: str | Path) -> ReviewResult:
    workdir = Path(workdir)
    out = review_root(cfg)
    result = build_outline_review(outline, out)
    source = workdir / "outline.json"
    h = outline_hash(source) if source.is_file() else _model_hash(outline)
    _save_json(_marker(cfg, OUTLINE), {
        "stage": OUTLINE,
        "hash": h,
        "workdir": str(workdir),
        "created_at": _now(),
        "review": str(result.index),
    })
    return result


def approve_outline(cfg: Any, workdir: str | Path) -> tuple[bool, str]:
    workdir = Path(workdir)
    source = workdir / "outline.json"
    if not source.is_file():
        return False, f"没有大纲缓存：{source}。先运行 --review-outline。"
    h = outline_hash(source)
    marker = _load_json(_marker(cfg, OUTLINE), {})
    if marker and not _same_path(marker.get("workdir"), workdir):
        marker = {}
    if marker and marker.get("hash") and marker["hash"] != h:
        return False, ("大纲在审阅后又被修改。请重新运行 --review-outline，"
                       "查看最新版本后再批准。")
    entry = load_ledger(cfg).get(OUTLINE) or {}
    if entry and not _same_path(entry.get("workdir"), workdir):
        entry = {}
    if not marker and entry.get("hash") and entry["hash"] != h:
        return False, ("大纲在上次批准后被修改。请重新运行 --review-outline，"
                       "查看最新版本后再批准。")
    _update_ledger(cfg, OUTLINE, {
        "hash": h,
        "workdir": str(workdir),
        "approved_at": _now(),
        "review": str(review_root(cfg) / "outline.html"),
    })
    _marker(cfg, OUTLINE).unlink(missing_ok=True)
    return True, f"大纲已批准：{source}"


def outline_gate_status(cfg: Any, workdir: str | Path,
                        *, resume: bool) -> tuple[bool, str]:
    """返回 (是否放行, 原因)。--ignore-review 由调用方决定是否绕过。"""
    workdir = Path(workdir)
    source = workdir / "outline.json"
    if not source.is_file():
        return True, ""
    h = outline_hash(source)
    marker = _load_json(_marker(cfg, OUTLINE), {})
    if marker and not _same_path(marker.get("workdir"), workdir):
        marker = {}
    if marker:
        return False, (
            "大纲人工审阅尚未批准。先看 "
            f"{review_root(cfg) / 'outline.html'}，然后运行 "
            "`--resume --approve outline`。"
        )
    entry = load_ledger(cfg).get(OUTLINE) or {}
    if entry and not _same_path(entry.get("workdir"), workdir):
        entry = {}
    if entry and resume and entry.get("hash") and entry["hash"] != h:
        return False, (
            "大纲在上次批准后被修改。请重新运行 --review-outline，"
            "确认后再 `--resume --approve outline`。"
        )
    return True, ""


# --------------------------------------------------------------------------
# 角色身份图审批
# --------------------------------------------------------------------------
def asset_hashes(assets_dir: str | Path) -> dict[str, str]:
    """每个角色目录的 identity.png 即身份锚点；只审批锚点，不审批派生图。"""
    root = Path(assets_dir)
    if not root.is_dir():
        return {}
    out: dict[str, str] = {}
    for d in sorted(root.iterdir()):
        p = d / "identity.png"
        if d.is_dir() and p.is_file():
            out[d.name] = _sha_file(p)
    return out


def mark_assets_review_required(cfg: Any,
                                assets_dir: str | Path) -> ReviewResult:
    assets_dir = Path(assets_dir)
    hashes = asset_hashes(assets_dir)
    out = review_root(cfg)
    result = build_assets_review(assets_dir, out)
    _save_json(_marker(cfg, ASSETS), {
        "stage": ASSETS,
        "hashes": hashes,
        "assets_dir": str(assets_dir),
        "created_at": _now(),
        "review": str(result.index),
    })
    return result


def approve_assets(cfg: Any, assets_dir: str | Path) -> tuple[bool, str]:
    assets_dir = Path(assets_dir)
    hashes = asset_hashes(assets_dir)
    if not hashes:
        return False, (f"{assets_dir} 下没有 identity.png。"
                       "先运行 `--run anchors` 生成定妆图。")
    marker = _load_json(_marker(cfg, ASSETS), {})
    if marker and not _same_path(marker.get("assets_dir"), assets_dir):
        marker = {}
    entry = load_ledger(cfg).get(ASSETS) or {}
    if entry and not _same_path(entry.get("assets_dir"), assets_dir):
        entry = {}
    if not marker and entry.get("hashes") and entry["hashes"] != hashes:
        return False, ("角色身份图在上次批准后被修改。"
                       "请先重新检查（--run anchors --review-assets），再批准。")
    _update_ledger(cfg, ASSETS, {
        "hashes": hashes,
        "assets_dir": str(assets_dir),
        "approved_at": _now(),
        "review": str(review_root(cfg) / "assets.html"),
    })
    _marker(cfg, ASSETS).unlink(missing_ok=True)
    return True, f"角色身份图已批准：{len(hashes)} 个角色"


def assets_gate_status(cfg: Any, assets_dir: str | Path,
                       *, require: bool = False) -> tuple[bool, str]:
    assets_dir = Path(assets_dir)
    marker = _load_json(_marker(cfg, ASSETS), {})
    if marker and not _same_path(marker.get("assets_dir"), assets_dir):
        marker = {}
    if marker:
        return False, (
            "角色身份图尚未批准。先看 "
            f"{review_root(cfg) / 'assets.html'}，然后运行 `--approve assets`。"
        )
    entry = load_ledger(cfg).get(ASSETS) or {}
    if entry and not _same_path(entry.get("assets_dir"), assets_dir):
        entry = {}
    if require and not entry:
        return False, (
            "还没有可审批的身份图。先运行 `--run anchors`，"
            "检查后 `--approve assets`，再 `--run full`。"
        )
    if entry:
        current = asset_hashes(assets_dir)
        old = entry.get("hashes") or {}
        if current != old:
            changed = sorted(set(current) | set(old))
            return False, (
                "角色身份图已变化（新增/替换/删除：" + ", ".join(changed) +
                "）。请重新检查并 `--approve assets`。"
            )
    return True, ""


# --------------------------------------------------------------------------
# 渲染
# --------------------------------------------------------------------------
def _e(value: Any) -> str:
    return html.escape(str(value if value is not None else ""))


def _md(value: Any) -> str:
    return str(value if value is not None else "").replace("|", r"\|").replace(
        "\n", "<br>")


def _items(values: Iterable[Any]) -> list[Any]:
    return list(values or [])


def _adaptation_rows(adaptation: Any) -> list[tuple[str, str, str]]:
    rows: list[tuple[str, str, str]] = []
    for label, attr in (("保留", "keep"), ("删除", "cut"), ("合并", "merge")):
        for item in _items(getattr(adaptation, attr, [])):
            rows.append((label, str(getattr(item, "what", "")),
                         str(getattr(item, "why", ""))))
    for item in _items(getattr(adaptation, "risks", [])):
        rows.append(("风险", str(getattr(item, "what", "")),
                     str(getattr(item, "plan", ""))))
    return rows


def _outline_html(outline: Any, title: str) -> str:
    params = getattr(outline, "params", None)
    adaptation = getattr(outline, "adaptation", None)
    spine = getattr(outline, "spine", None)
    power = getattr(outline, "power_rule", None)
    episodes = _items(getattr(outline, "episodes", []))
    characters = _items(getattr(outline, "characters", []))
    scenes = _items(getattr(outline, "scenes", []))
    props = _items(getattr(outline, "props", []))
    beats = _items(getattr(outline, "beats", []))
    episodes = sorted(episodes, key=lambda e: getattr(e, "ep", 0))
    beats = sorted(beats, key=lambda b: (getattr(b, "episode", 0),
                                         getattr(b, "id", "")))

    adaptation_table = "".join(
        f"<tr><td>{_e(kind)}</td><td>{_e(what)}</td><td>{_e(why)}</td></tr>"
        for kind, what, why in _adaptation_rows(adaptation)) or (
        '<tr><td colspan="3" class="mut">无</td></tr>')

    char_rows = "".join(
        f"<tr><td>{_e(getattr(c, 'id', ''))}</td>"
        f"<td>{_e(getattr(c, 'name', ''))}</td>"
        f"<td>{_e(getattr(c, 'role', ''))} / {_e(getattr(c, 'tier', ''))}</td>"
        f"<td>{_e(getattr(c, 'age', ''))} {_e(getattr(c, 'gender', ''))}</td>"
        f"<td>{_e(getattr(c, 'identity', ''))}</td>"
        f"<td>{_e(getattr(c, 'appearance', ''))}</td></tr>"
        for c in characters) or '<tr><td colspan="6" class="mut">无</td></tr>'

    scene_rows = "".join(
        f"<tr><td>{_e(getattr(s, 'id', ''))}</td>"
        f"<td>{_e(getattr(s, 'name', ''))}</td>"
        f"<td>{'是' if getattr(s, 'primary', False) else ''}</td>"
        f"<td>{_e(getattr(s, 'summary', ''))}</td></tr>"
        for s in scenes) or '<tr><td colspan="4" class="mut">无</td></tr>'

    prop_rows = "".join(
        f"<tr><td>{_e(getattr(p, 'id', ''))}</td>"
        f"<td>{_e(getattr(p, 'name', ''))}</td>"
        f"<td>{_e(getattr(p, 'function', ''))}</td>"
        f"<td>{_e(getattr(p, 'scale', ''))}</td></tr>"
        for p in props) or '<tr><td colspan="4" class="mut">无</td></tr>'

    beat_rows = "".join(
        f"<tr><td>{_e(getattr(b, 'id', ''))}</td>"
        f"<td>{_e(getattr(b, 'episode', ''))}</td>"
        f"<td>{_e(getattr(b, 'type', ''))}</td>"
        f"<td>{_e(getattr(b, 'weight', ''))}</td>"
        f"<td>{_e(getattr(b, 'setup', ''))}</td>"
        f"<td>{_e(getattr(b, 'payoff', ''))}</td></tr>"
        for b in beats) or '<tr><td colspan="6" class="mut">无</td></tr>'

    episode_rows = "".join(
        f"<tr><td>{_e(getattr(e, 'ep', ''))}</td>"
        f"<td>{_e(getattr(e, 'title', ''))}</td>"
        f"<td>{_e(getattr(e, 'synopsis', ''))}</td>"
        f"<td>{_e(getattr(e, 'hook', ''))}</td>"
        f"<td>{_e(getattr(e, 'cliff', ''))}</td>"
        f"<td>{_e(', '.join(getattr(e, 'scene_ids', [])))}</td></tr>"
        for e in episodes) or '<tr><td colspan="6" class="mut">无</td></tr>'

    spine_rows = ""
    if spine is not None:
        spine_rows = "".join(
            f"<tr><th>{_e(label)}</th><td>{_e(getattr(spine, attr, ''))}</td></tr>"
            for label, attr in (("想要", "want"), ("阻力", "obstacle"),
                                ("选择", "choice"), ("代价", "cost"),
                                ("转折", "turn"), ("钩子", "hook")))

    power_html = ""
    if power is not None:
        power_html = (
            '<div class="grid3">'
            f'<div><b>能做什么</b><p>{_e(getattr(power, "sees", ""))}</p></div>'
            f'<div><b>边界</b><p>{_e(getattr(power, "cannot", ""))}</p></div>'
            f'<div><b>代价</b><p>{_e(getattr(power, "cost", ""))}</p></div>'
            '</div>')

    params_text = ""
    if params is not None:
        params_text = (f"{_e(getattr(params, 'episodes', ''))} 集 · "
                       f"每集 {_e(getattr(params, 'target_seconds', ''))} 秒 · "
                       f"{_e(getattr(params, 'aspect_ratio', ''))}")

    return f"""<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{_e(title)} · 大纲审阅</title>
<style>
:root{{--bg:#14110f;--card:#1e1a17;--ink:#efe7dc;--mut:#a99e91;--line:#3a322c;--acc:#e2a35a;--ok:#7fbf8a}}
*{{box-sizing:border-box}}body{{margin:0;background:var(--bg);color:var(--ink);font:15px/1.65 "Segoe UI","Microsoft YaHei",sans-serif}}
main{{max-width:1180px;margin:0 auto;padding:40px 24px 80px}}
h1{{margin:0 0 4px;font-size:30px}}h2{{margin:36px 0 12px;font-size:20px;color:var(--acc)}}
.mut{{color:var(--mut)}}.kpis{{display:flex;gap:12px;flex-wrap:wrap;margin:22px 0}}
.kpi{{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:12px 16px;min-width:120px}}
.kpi b{{display:block;font-size:22px;color:var(--acc)}}
.card{{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:18px 20px;margin:12px 0}}
table{{width:100%;border-collapse:collapse;background:var(--card);border:1px solid var(--line);border-radius:10px;overflow:hidden}}
th,td{{padding:9px 10px;border-bottom:1px solid var(--line);vertical-align:top;text-align:left}}
th{{background:#261f1b;color:var(--acc);font-size:13px;white-space:nowrap}}
tr:last-child td{{border-bottom:0}}
.grid3{{display:grid;grid-template-columns:repeat(3,1fr);gap:12px}}
.grid3>div{{background:#261f1b;border-radius:8px;padding:12px}}
.grid3 p{{margin:6px 0 0;color:var(--mut)}}
.checklist{{border-left:3px solid var(--acc);padding-left:16px}}
.checklist li{{margin:6px 0}}
code{{background:#261f1b;padding:2px 6px;border-radius:4px;color:#f0c690}}
@media(max-width:760px){{.grid3{{grid-template-columns:1fr}}}}
</style></head><body><main>
<h1>{_e(title)}</h1>
<div class="mut">全剧大纲 · 人工审阅页 · {_e(params_text)}</div>
<div class="kpis">
<div class="kpi"><b>{len(episodes)}</b>集数</div>
<div class="kpi"><b>{len(characters)}</b>角色</div>
<div class="kpi"><b>{len(scenes)}</b>场景</div>
<div class="kpi"><b>{len(props)}</b>道具</div>
<div class="kpi"><b>{len(beats)}</b>爽点</div>
</div>

<div class="card checklist"><b>先看这五件事</b>
<ol>
<li>砍掉的支线是否伤到主线；合并的角色是否还立得住。</li>
<li>大爆点落在第几集；连续三集是否有爽点真空。</li>
<li>角色档位是否过多；功能性角色是否真的有事做。</li>
<li>主场景是否过于分散；一次性场景是否有规避方案。</li>
<li>每集的开场钩子和结尾悬念是否具体可拍。</li>
</ol></div>

<h2>改编决策</h2>
<table><tr><th>类型</th><th>内容</th><th>原因</th></tr>{adaptation_table}</table>
<div class="card"><b>一句话核心</b><p>{_e(getattr(adaptation, 'core', ''))}</p></div>

<h2>故事脊柱</h2>
<table>{spine_rows or '<tr><td class="mut">无</td></tr>'}</table>

<h2>主角优势与代价</h2>{power_html or '<div class="card mut">无</div>'}

<h2>角色</h2>
<table><tr><th>ID</th><th>姓名</th><th>定位</th><th>年龄/性别</th><th>身份</th><th>可见特征</th></tr>{char_rows}</table>

<h2>场景</h2>
<table><tr><th>ID</th><th>名称</th><th>主场景</th><th>说明</th></tr>{scene_rows}</table>

<h2>道具</h2>
<table><tr><th>ID</th><th>名称</th><th>剧情功能</th><th>尺度</th></tr>{prop_rows}</table>

<h2>爽点分布</h2>
<table><tr><th>ID</th><th>集</th><th>类型</th><th>权重</th><th>铺垫</th><th>回收</th></tr>{beat_rows}</table>

<h2>分集梗概</h2>
<table><tr><th>集</th><th>标题</th><th>梗概</th><th>开场钩子</th><th>结尾悬念</th><th>场景</th></tr>{episode_rows}</table>

<h2>批准后继续</h2>
<div class="card">检查或修改 <code>state/story/outline.json</code> 后：
<p><code>python -m manju.story_cli &lt;小说&gt; --resume --approve outline</code></p>
<p class="mut">修改大纲会改变哈希，必须重新审阅并批准。要跳过审批，显式加 <code>--ignore-review</code>。</p>
</div>
</main></body></html>"""


def _outline_md(outline: Any, title: str) -> str:
    adaptation = getattr(outline, "adaptation", None)
    spine = getattr(outline, "spine", None)
    power = getattr(outline, "power_rule", None)
    params = getattr(outline, "params", None)
    episodes = sorted(_items(getattr(outline, "episodes", [])),
                      key=lambda e: getattr(e, "ep", 0))
    lines = [f"# {title} · 大纲审阅", ""]
    if params is not None:
        lines += [f"{getattr(params, 'episodes', '')} 集 · "
                  f"每集 {getattr(params, 'target_seconds', '')} 秒 · "
                  f"{getattr(params, 'aspect_ratio', '')}", ""]
    lines += ["## 改编决策", "",
              f"**核心**：{getattr(adaptation, 'core', '')}", "",
              "| 类型 | 内容 | 原因 |", "|---|---|---|"]
    lines += [f"| {_md(k)} | {_md(w)} | {_md(y)} |"
              for k, w, y in _adaptation_rows(adaptation)]
    lines += ["", "## 故事脊柱", "", "| 字段 | 内容 |", "|---|---|"]
    if spine is not None:
        for label, attr in (("想要", "want"), ("阻力", "obstacle"),
                            ("选择", "choice"), ("代价", "cost"),
                            ("转折", "turn"), ("钩子", "hook")):
            lines.append(f"| {label} | {_md(getattr(spine, attr, ''))} |")
    if power is not None:
        lines += ["", "## 主角优势", "",
                  f"- 能做什么：{getattr(power, 'sees', '')}",
                  f"- 边界：{getattr(power, 'cannot', '')}",
                  f"- 代价：{getattr(power, 'cost', '')}"]
    lines += ["", "## 角色", "",
              "| ID | 姓名 | 定位 | 年龄/性别 | 身份 | 可见特征 |",
              "|---|---|---|---|---|---|"]
    for c in _items(getattr(outline, "characters", [])):
        lines.append(
            f"| {_md(getattr(c, 'id', ''))} | {_md(getattr(c, 'name', ''))} | "
            f"{_md(getattr(c, 'role', ''))} / {_md(getattr(c, 'tier', ''))} | "
            f"{_md(getattr(c, 'age', ''))} {_md(getattr(c, 'gender', ''))} | "
            f"{_md(getattr(c, 'identity', ''))} | "
            f"{_md(getattr(c, 'appearance', ''))} |")
    lines += ["", "## 场景", "", "| ID | 名称 | 主场景 | 说明 |", "|---|---|---|---|"]
    for s in _items(getattr(outline, "scenes", [])):
        lines.append(
            f"| {_md(getattr(s, 'id', ''))} | {_md(getattr(s, 'name', ''))} | "
            f"{'是' if getattr(s, 'primary', False) else ''} | "
            f"{_md(getattr(s, 'summary', ''))} |")
    lines += ["", "## 道具", "", "| ID | 名称 | 功能 | 尺度 |", "|---|---|---|---|"]
    for p in _items(getattr(outline, "props", [])):
        lines.append(
            f"| {_md(getattr(p, 'id', ''))} | {_md(getattr(p, 'name', ''))} | "
            f"{_md(getattr(p, 'function', ''))} | "
            f"{_md(getattr(p, 'scale', ''))} |")
    lines += ["", "## 爽点", "", "| ID | 集 | 类型 | 权重 | 铺垫 | 回收 |",
              "|---|---|---|---|---|---|"]
    for b in sorted(_items(getattr(outline, "beats", [])),
                    key=lambda b: (getattr(b, "episode", 0),
                                   getattr(b, "id", ""))):
        lines.append(
            f"| {_md(getattr(b, 'id', ''))} | {_md(getattr(b, 'episode', ''))} | "
            f"{_md(getattr(b, 'type', ''))} | {_md(getattr(b, 'weight', ''))} | "
            f"{_md(getattr(b, 'setup', ''))} | {_md(getattr(b, 'payoff', ''))} |")
    lines += ["", "## 分集梗概", "",
              "| 集 | 标题 | 梗概 | 开场钩子 | 结尾悬念 |",
              "|---|---|---|---|---|"]
    for e in episodes:
        lines.append(
            f"| {_md(getattr(e, 'ep', ''))} | {_md(getattr(e, 'title', ''))} | "
            f"{_md(getattr(e, 'synopsis', ''))} | {_md(getattr(e, 'hook', ''))} | "
            f"{_md(getattr(e, 'cliff', ''))} |")
    lines += ["", "## 批准", "",
              "检查或修改 `state/story/outline.json` 后运行：", "",
              "```powershell",
              "python -m manju.story_cli <小说> --resume --approve outline",
              "```", "",
              "修改大纲会改变哈希，必须重新审阅并批准。",
              "要跳过审批，显式加 `--ignore-review`。", ""]
    return "\n".join(lines)


def build_outline_review(outline: Any, outdir: str | Path) -> ReviewResult:
    out = Path(outdir)
    out.mkdir(parents=True, exist_ok=True)
    title = str(getattr(outline, "title", "") or "漫剧故事层")
    index = out / "outline.html"
    markdown = out / "outline.md"
    index.write_text(_outline_html(outline, title), encoding="utf-8")
    markdown.write_text(_outline_md(outline, title), encoding="utf-8")
    return ReviewResult(index=index, markdown=markdown)


# --------------------------------------------------------------------------
# 角色身份图审阅页
# --------------------------------------------------------------------------
def _assets_rows(assets_dir: Path, outdir: Path) -> list[dict[str, Any]]:
    root = assets_dir
    rows: list[dict[str, Any]] = []
    if not root.is_dir():
        return rows
    for d in sorted(root.iterdir()):
        if not d.is_dir():
            continue
        p = d / "identity.png"
        exists = p.is_file()
        rel = ""
        if exists:
            try:
                rel = os.path.relpath(p, outdir).replace(os.sep, "/")
            except ValueError:
                rel = ""
        rows.append({
            "id": d.name, "path": str(p), "rel": rel,
            "exists": exists,
            "sha256": _sha_file(p) if exists else "",
            "size": p.stat().st_size if exists else 0,
        })
    return rows


def build_assets_review(assets_dir: str | Path,
                        outdir: str | Path) -> ReviewResult:
    out = Path(outdir)
    out.mkdir(parents=True, exist_ok=True)
    assets_dir = Path(assets_dir)
    rows = _assets_rows(assets_dir, out)
    ok = sum(1 for r in rows if r["exists"])
    cells: list[str] = []
    for r in rows:
        if r["exists"]:
            image = '<img src="' + _e(r["rel"]) + '" alt="">'
        else:
            image = '<span class="mut">缺失</span>'
        cells.append(
            f"<tr><td>{_e(r['id'])}</td><td>{image}</td>"
            f"<td class=mono>{_e(r['sha256'][:16])}</td>"
            f"<td>{_e(r['size'])}</td></tr>")
    html_rows = "".join(cells) or (
        '<tr><td colspan="4" class="mut">没有角色身份图</td></tr>')
    index = out / "assets.html"
    index.write_text(f"""<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>角色身份图审阅</title>
<style>
:root{{--bg:#14110f;--card:#1e1a17;--ink:#efe7dc;--mut:#a99e91;--line:#3a322c;--acc:#e2a35a}}
*{{box-sizing:border-box}}body{{margin:0;background:var(--bg);color:var(--ink);font:15px/1.65 "Segoe UI","Microsoft YaHei",sans-serif}}
main{{max-width:1080px;margin:0 auto;padding:40px 24px 80px}}
h1{{margin:0 0 4px;font-size:30px}}h2{{margin:30px 0 12px;font-size:20px;color:var(--acc)}}
.mut{{color:var(--mut)}}.kpi{{display:inline-block;background:var(--card);border:1px solid var(--line);border-radius:10px;padding:12px 18px;margin:18px 0}}
.kpi b{{font-size:24px;color:var(--acc)}}table{{width:100%;border-collapse:collapse;background:var(--card);border:1px solid var(--line)}}
th,td{{padding:10px;border-bottom:1px solid var(--line);text-align:left;vertical-align:middle}}
th{{background:#261f1b;color:var(--acc)}}img{{max-width:150px;max-height:200px;border-radius:6px;border:1px solid var(--line)}}
.mono{{font-family:Consolas,monospace;font-size:12px;color:var(--mut)}}
code{{background:#261f1b;padding:2px 6px;border-radius:4px;color:#f0c690}}
</style></head><body><main>
<h1>角色身份图审阅</h1>
<div class="mut">只审 <code>assets/characters/&lt;角色&gt;/identity.png</code>；它是全剧唯一的脸源。</div>
<div class="kpi"><b>{ok}</b> / {len(rows)} 个角色有身份图</div>
<h2>逐角色检查</h2>
<table><tr><th>角色</th><th>身份图</th><th>SHA256</th><th>字节</th></tr>{html_rows}</table>
<h2>检查点</h2>
<ul>
<li>脸、发型、瞳色、体型是否与角色设定一致；</li>
<li>是否一眼能与其他角色区分；</li>
<li>服装是否是默认状态，且没有混入剧情专属伤口/雨湿状态；</li>
<li>图片必须是正面、单人、无遮挡、可被 PuLID 使用。</li>
</ul>
<h2>批准后继续</h2>
<div>确认后由完整联调包运行 <code>--approve assets --run full</code>。
<p class="mut">新增角色或替换 identity.png 会使审批失效，必须重新批准。</p></div>
</main></body></html>""", encoding="utf-8")

    md = ["# 角色身份图审阅", "",
          f"共 {ok} / {len(rows)} 个角色有身份图。", "",
          "| 角色 | 路径 | SHA256 | 字节 |", "|---|---|---|---|"]
    for r in rows:
        md.append(f"| {_md(r['id'])} | {_md(r['path'])} | "
                  f"{_md(r['sha256'][:16])} | {_md(r['size'])} |")
    md += ["", "确认后运行：", "",
           "```powershell",
           "# 完整联调包：--approve assets --run full",
           "```", ""]
    markdown = out / "assets.md"
    markdown.write_text("\n".join(md), encoding="utf-8")
    return ReviewResult(index=index, markdown=markdown)


# --------------------------------------------------------------------------
# 质量门失败包
# --------------------------------------------------------------------------
def write_failure_packet(cfg: Any, workdir: str | Path, stage: str,
                         report: Any, attempts: int,
                         payload: Any = None) -> Path:
    root = review_root(cfg) / "failures"
    root.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    slug = re.sub(r"[^\w.-]+", "_", stage or "gate").strip("_")[:60] or "gate"
    stem = f"{stamp}_{slug}"
    data = {
        "stage": stage,
        "created_at": _now(),
        "attempts": attempts,
        "workdir": str(workdir),
        "errors": list(getattr(report, "errors", [])),
        "warnings": list(getattr(report, "warnings", [])),
        "report": getattr(report, "text", lambda: "")(),
        "cache_files": sorted(p.name for p in Path(workdir).glob("*.json")),
    }
    if payload is not None:
        # 最后一次尝试的产出快照：失败包既用于诊断，也用于人工修复
        # （剧本未过门不固化，没有快照人工无物可改）。
        data["payload"] = payload
    raw = root / f"{stem}.json"
    raw.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n",
                   encoding="utf-8")
    lines = [
        f"# 质量门失败包：{stage}", "",
        f"- 时间：{data['created_at']}",
        f"- 尝试次数：{attempts}",
        f"- 中间产物：`{workdir}`", "",
        "## 错误", "",
    ]
    lines += [f"- {x}" for x in data["errors"]] or ["- 无"]
    if data["warnings"]:
        lines += ["", "## 警告", ""] + [f"- {x}" for x in data["warnings"]]
    lines += [
        "", "## 完整报告", "", "```text", data["report"], "```", "",
        "## 下一步", "",
        "1. 按错误修改 `state/story/*.json`，或调整小说/参数后重跑；",
        "2. 继续复用已通过门的缓存：加 `--resume`；",
        "3. 需要重新审阅大纲时：`--review-outline`。",
        "", f"原始 JSON：`{raw}`", "",
    ]
    latest = root / "latest.md"
    latest.write_text("\n".join(lines), encoding="utf-8")
    (root / f"{stem}.md").write_text("\n".join(lines), encoding="utf-8")
    return latest
