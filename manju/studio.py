# -*- coding: utf-8 -*-
"""Manju Studio —— 输入层图形管理台（本地 Web）。

浏览器打开 http://127.0.0.1:8092 即可，界面按工作流组织：
- 生成：选小说、分集方式、本次生成范围（起止集）与画面参数
- 审阅：分集/分镜、角色/场景/道具、画风/世界观、人物关系
- 发布：改动标记 + 底部「重新编译契约」，把中间产物固化为 input/ 契约
- 系统：长期配置（文本模型/改编策略）与 LLM 调用日志

只读/写 输入层（config.json / state/story / input），不碰渲染/成片。
前端单页在 `studio_ui.html`，与本文件同目录。
用法：
    python -m manju.studio                 # 起服务，默认 127.0.0.1:8092
    python -m manju.studio --port 8092 --host 127.0.0.1
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

from .config import api_key_source, env_api_key

ROOT = Path(__file__).resolve().parent.parent
STATE = ROOT / "state" / "story"
INPUT = ROOT / "input"
DIRTY = STATE / ".studio_dirty"
CURRENT = STATE / ".studio_current.json"
CONFIG = ROOT / "config.json"
TRACE_LOG = ROOT / "state" / "llm_trace.jsonl"


def _novel_name(name: str) -> str:
    return Path(str(name)).stem


def _novel_work_dir(name: str) -> Path:
    return STATE / (".work_" + name)


def _novel_final_dir(name: str) -> Path:
    return STATE / name


def _has_artifacts(p: Path) -> bool:
    """目录里是否有真正的中间产物（排除 resume 指纹等元数据）。"""
    if not p.is_dir():
        return False
    return any(f.is_file() and f.suffix == ".json" and f.name != "_resume_fp.json"
               for f in p.iterdir())


def _story_dir() -> Path:
    """当前小说目录。

    优先 ``state/story/.work_<小说名>``（生成中/上次失败留下的工作层），
    其次 ``state/story/<小说名>``（已成功固化的成品）。空选择回退平铺目录。
    """
    if not _CURRENT_NOVEL:
        return STATE
    work = _novel_work_dir(_CURRENT_NOVEL)
    final = _novel_final_dir(_CURRENT_NOVEL)
    if _has_artifacts(work):
        return work
    if final.is_dir():
        return final
    return work


def _current_info() -> dict:
    d = _story_dir()
    if not _CURRENT_NOVEL:
        phase = "flat"
    elif d.name.startswith(".work_"):
        phase = "work"
    else:
        phase = "final"
    return {"novel": _CURRENT_NOVEL, "dir": str(d), "phase": phase}


def _load_current() -> str:
    try:
        return _novel_name(json.load(open(CURRENT, encoding="utf-8")).get("novel") or "")
    except Exception:  # noqa: BLE001 - 缺失/损坏都按未选择处理
        return ""


def _save_current(name: str) -> None:
    try:
        CURRENT.parent.mkdir(parents=True, exist_ok=True)
        CURRENT.write_text(json.dumps({"novel": name}, ensure_ascii=False),
                           encoding="utf-8")
    except OSError:
        pass


def _auto_current() -> str:
    """启动时恢复上次选择；没有记录则选最近活跃的小说目录。"""
    saved = _load_current()
    if saved and (_novel_work_dir(saved).is_dir() or _novel_final_dir(saved).is_dir()):
        return saved
    best: tuple[float, str] | None = None
    if STATE.is_dir():
        for d in STATE.iterdir():
            if not d.is_dir():
                continue
            name = d.name[6:] if d.name.startswith(".work_") else d.name
            if name.startswith(".") or not _has_artifacts(d):
                continue
            mtime = max((f.stat().st_mtime for f in d.glob("*.json")),
                        default=d.stat().st_mtime)
            if best is None or mtime > best[0]:
                best = (mtime, name)
    return best[1] if best else ""


# 当前小说（state/story/<小说名> 或 .work_<小说名>）
_CURRENT_NOVEL = _auto_current()


MASK_KEEP = 4          # api_key 掩码保留尾部位数

# Studio 配置页输入的 Key 只保留在本进程内存里，随生成子进程的环境变量下发；
# 不写入 config.json，避免明文落盘。要跨重启使用，请设置 DEEPSEEK_API_KEY。
_SESSION_API_KEY = ""


def load_config() -> dict:
    return json.load(open(CONFIG, encoding="utf-8"))


def save_config(cfg: dict) -> None:
    CONFIG.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")


# DeepSeek 官方价（CNY / 1M tokens，2026-10-27）。0=空闲时段，1=高峰时段。
# 只用于 Studio 日志估算：不参与生成，也不改变任何请求参数。
_TRACE_PRICES = {
    "deepseek-flash": {
        "hit": (0.02, 0.04), "miss": (1.0, 2.0), "out": (4.0, 8.0),
    },
    "deepseek-v4-pro": {
        "hit": (0.15, 0.30), "miss": (4.5, 9.0), "out": (13.5, 27.0),
    },
}
_TRACE_MODEL_ALIASES = {
    "deepseek-v4-flash": "deepseek-flash",
    "deepseek-v4.1-flash": "deepseek-flash",
    "deepseek-v4-flash-vision-exp": "deepseek-flash",
    "deepseek-pro": "deepseek-v4-pro",
    "deepseek-v4.1-pro": "deepseek-v4-pro",
}
_TRACE_DEFAULT_MODEL = "deepseek-flash"
_TRACE_PRICE_DATE = "2026-10-27"


def _trace_int(value) -> int:
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError):
        return 0


def _trace_pricing_key(model: str) -> tuple[str, bool]:
    """模型名 → 价目表键；第二个返回值表示是否按 Flash 兜底估算。"""
    key = (model or "").strip().lower()
    key = _TRACE_MODEL_ALIASES.get(key, key)
    if key in _TRACE_PRICES:
        return key, False
    # 带日期/版本后缀的同一模型（如 deepseek-flash-20261001）。
    for known in _TRACE_PRICES:
        if key.startswith(known + "-") or key.startswith(known + "."):
            return known, False
    return _TRACE_DEFAULT_MODEL, True


def _trace_usage(row: dict) -> tuple[int, int, int]:
    """返回 (缓存命中输入, 未命中输入, 输出)；缺缓存明细时全部按未命中计。"""
    in_tokens = _trace_int(row.get("in_tokens"))
    hit = min(_trace_int(row.get("cache_hit_tokens")), in_tokens)
    miss = min(_trace_int(row.get("cache_miss_tokens")), in_tokens - hit)
    if hit + miss < in_tokens:
        miss = in_tokens - hit
    return hit, miss, _trace_int(row.get("out_tokens"))


def _trace_cost(pricing_key: str, hit: int, miss: int, out_tokens: int,
                *, peak: bool) -> float:
    price = _TRACE_PRICES[pricing_key]
    idx = 1 if peak else 0
    return (hit * price["hit"][idx]
            + miss * price["miss"][idx]
            + out_tokens * price["out"][idx]) / 1_000_000


def _decorate_trace_rows(rows: list[dict], default_model: str) -> list[dict]:
    default = (default_model or "").strip()
    out: list[dict] = []
    for row in rows:
        rec = dict(row)
        raw_model = str(row.get("model") or "").strip()
        model = raw_model or default or _TRACE_DEFAULT_MODEL
        pricing_key, unknown = _trace_pricing_key(model)
        hit, miss, out_tokens = _trace_usage(row)
        rec["model"] = model
        rec["model_inferred"] = not bool(raw_model)
        rec["pricing_model"] = pricing_key
        rec["cost_offpeak_cny"] = round(
            _trace_cost(pricing_key, hit, miss, out_tokens, peak=False), 6)
        rec["cost_peak_cny"] = round(
            _trace_cost(pricing_key, hit, miss, out_tokens, peak=True), 6)
        if unknown:
            rec["pricing_unknown"] = True
        out.append(rec)
    return out


def _summarize_trace(rows: list[dict]) -> dict:
    models: dict[str, int] = {}
    unknown_models: list[str] = []
    inferred_calls = 0
    in_tokens = out_tokens = hit_total = miss_total = 0
    offpeak = peak = 0.0
    for row in rows:
        model = str(row.get("model") or _TRACE_DEFAULT_MODEL)
        models[model] = models.get(model, 0) + 1
        if row.get("model_inferred"):
            inferred_calls += 1
        if row.get("pricing_unknown") and model not in unknown_models:
            unknown_models.append(model)
        hit, miss, out = _trace_usage(row)
        in_tokens += _trace_int(row.get("in_tokens"))
        out_tokens += out
        hit_total += hit
        miss_total += miss
        pricing_key = (row.get("pricing_model")
                       or _trace_pricing_key(model)[0])
        offpeak += _trace_cost(pricing_key, hit, miss, out, peak=False)
        peak += _trace_cost(pricing_key, hit, miss, out, peak=True)
    cache_total = hit_total + miss_total
    return {
        "calls": len(rows),
        "in_tokens": in_tokens,
        "out_tokens": out_tokens,
        "cache_hit_tokens": hit_total,
        "cache_miss_tokens": miss_total,
        "cache_hit_ratio": (round(hit_total / cache_total, 4)
                            if cache_total else None),
        "models": models,
        "model_inferred_calls": inferred_calls,
        "cost": {
            "currency": "CNY",
            "price_date": _TRACE_PRICE_DATE,
            "offpeak_cny": round(offpeak, 4),
            "peak_cny": round(peak, 4),
            "unknown_models": unknown_models,
        },
    }


def _default_trace_model() -> str:
    try:
        cfg = load_config()
    except Exception:  # noqa: BLE001 - 配置缺失/损坏时仍要能看日志
        return ""
    llm = cfg.get("llm") if isinstance(cfg, dict) else None
    return str((llm or {}).get("model") or "")


def read_llm_trace(limit: int = 300,
                   default_model: str | None = None) -> dict:
    """读 state/llm_trace.jsonl（每次 LLM 调用一行，由 manju/llm.py 追加）。

    返回最新在前的条目，并对全部记录汇总 token 与按官方价的费用区间。
    旧记录没有 model 字段时，用当前配置模型推断并标记 model_inferred。
    """
    default_model = (_default_trace_model()
                     if default_model is None else default_model)
    if not TRACE_LOG.exists():
        return {"entries": [], "total": 0, "path": str(TRACE_LOG),
                "mtime": None, "summary": _summarize_trace([])}
    rows: list[dict] = []
    try:
        with open(TRACE_LOG, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    rows.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
        mtime = time.strftime("%Y-%m-%d %H:%M:%S",
                              time.localtime(TRACE_LOG.stat().st_mtime))
    except Exception as e:  # noqa: BLE001
        return {"entries": [], "total": 0, "path": str(TRACE_LOG),
                "mtime": None, "error": str(e),
                "summary": _summarize_trace([])}
    rows.reverse()
    decorated = _decorate_trace_rows(rows, default_model)
    return {"entries": decorated[:limit], "total": len(decorated),
            "path": str(TRACE_LOG), "mtime": mtime,
            "summary": _summarize_trace(decorated)}


def mask_key(key: str) -> str:
    if not key:
        return ""
    if len(key) <= MASK_KEEP + 4:
        return "••••"
    return "sk-••••" + key[-MASK_KEEP:]


def _effective_api_key(cfg: dict | None = None) -> str:
    """环境变量 > 本 Studio 会话输入 > 旧 config.json（兼容迁移）。"""
    env_key = env_api_key()
    if env_key:
        return env_key
    if _SESSION_API_KEY:
        return _SESSION_API_KEY
    if cfg:
        return str((cfg.get("llm") or {}).get("api_key") or "")
    return ""


def _effective_api_key_source(cfg: dict | None = None) -> str:
    if env_api_key():
        return api_key_source()
    if _SESSION_API_KEY:
        return "studio-session（重启后失效）"
    if cfg and str((cfg.get("llm") or {}).get("api_key") or ""):
        return "config.json（建议迁移到环境变量）"
    return "missing"


# Studio 配置页可写的字段白名单。新增页面字段时三处必须同步：
# 表单渲染、saveCfg 组装 body、这里持久化；漏一处就会“界面能改、保存不生效”。
_CONFIG_LLM_KEYS = (
    "provider", "base_url", "model", "parallel",
    "story_chars_per_episode", "story_max_auto_episodes",
)
# 温度不对外开放：各生成阶段已在代码里固定 0.0（卷摘要 0.2），
# config 里的 llm.temperature 只作为未来未显式传值调用的内部兜底。


def apply_config_update(cfg: dict, body: dict) -> dict:
    """把 Studio 配置页提交的字段合并进 config；只写白名单，避免误改其他键。

    API Key 是例外：它只进入本进程内存并随生成子进程下发，不写回 config.json。
    这样 Studio 仍能即开即用，又不会把明文密钥落盘。
    """
    global _SESSION_API_KEY
    llm = cfg.setdefault("llm", {})
    key = body.get("api_key")
    if key and "•" not in key:
        _SESSION_API_KEY = str(key)
    elif "api_key" in body and not _SESSION_API_KEY:
        # 旧版页面只回传掩码时，把磁盘上的旧 Key 提升到本次会话，
        # 再从配置里删除；用户本次仍能生成，重启前自行改设环境变量。
        legacy = str(llm.get("api_key") or "")
        if legacy:
            _SESSION_API_KEY = legacy
    # 只要配置页提交过 api_key（包括掩码回传），就顺手清掉旧版明文 key。
    if "api_key" in body:
        llm.pop("api_key", None)
    for key_name in _CONFIG_LLM_KEYS:
        if key_name in body:
            llm[key_name] = body[key_name]
    if isinstance(body.get("studio"), dict):
        cfg["studio"] = body["studio"]
    return cfg


def mark_dirty(msg: str = "") -> None:
    DIRTY.write_text(json.dumps({"at": time.time(), "msg": msg}, ensure_ascii=False),
                     encoding="utf-8")


def clear_dirty() -> None:
    if DIRTY.exists():
        DIRTY.unlink()


def dirty_info() -> dict:
    if not DIRTY.exists():
        return {"dirty": False}
    d = json.load(open(DIRTY, encoding="utf-8"))
    return {"dirty": True, "msg": d.get("msg", ""), "at": d.get("at")}


def _read_json(p: Path):
    return json.load(open(p, encoding="utf-8-sig"))


def _write_json(p: Path, data) -> None:
    p.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def _in(p: Path, base: Path) -> bool:
    try:
        p.resolve().relative_to(base.resolve())
        return True
    except ValueError:
        return False


# ---------------------------------------------------------------------------
# 数据读写
# ---------------------------------------------------------------------------
def list_eps() -> list[int]:
    import re
    # 只认两位集号（与 get_board/compile 的 :02d 完全一致）；
    # 混入 board_ep001.json 这类旧命名会导致同一集被列两次、读的却还是另一位。
    return sorted({int(m.group(1)) for f in _story_dir().glob("board_ep*.json")
                   if (m := re.match(r"board_ep(\d{2})\.json$", f.name))})


def get_board(ep: int) -> dict:
    return _read_json(_story_dir() / f"board_ep{ep:02d}.json")


def _ep_obj(board: dict) -> dict:
    """取出单集 board 本体。

    现行落盘是 `StoryEpisodeBoard`（`{ep, state_in, segments}`）；早期版本
    外面还包了一层 `{episodes:[...]}`。这里两种都兼容，避免界面对旧产物
    直接报 KeyError。
    """
    if isinstance(board, dict):
        eps = board.get("episodes")
        if isinstance(eps, list) and eps and isinstance(eps[0], dict):
            return eps[0]
    return board if isinstance(board, dict) else {}


def _outline_eps() -> dict[int, dict]:
    try:
        o = _read_json(_story_dir() / "outline.json")
    except Exception:  # noqa: BLE001 - 缺大纲时只显示分镜统计
        return {}
    out: dict[int, dict] = {}
    for e in o.get("episodes", []) or []:
        try:
            out[int(e.get("ep"))] = e
        except (TypeError, ValueError):
            continue
    return out


def ep_summary() -> list[dict]:
    """分集页卡片用的轻量统计：标题、场数、镜数、总时长。"""
    meta = _outline_eps()
    rows: list[dict] = []
    for ep in list_eps():
        title = (meta.get(ep) or {}).get("title") or ""
        try:
            obj = _ep_obj(get_board(ep))
            segs = obj.get("segments") or []
            cuts = [c for s in segs for c in (s.get("cuts") or [])]
            secs = 0.0
            for c in cuts:
                try:
                    secs += float(c.get("seconds") or 0)
                except (TypeError, ValueError):
                    pass
            rows.append({"ep": ep, "title": title, "scenes": len(segs),
                         "cuts": len(cuts), "seconds": round(secs, 1)})
        except Exception as e:  # noqa: BLE001
            rows.append({"ep": ep, "title": title, "scenes": 0, "cuts": 0,
                         "seconds": 0, "error": str(e)})
    return rows


def get_assets() -> dict:
    return {
        "cast": _read_json(_story_dir() / "cast.json"),
        "art": _read_json(_story_dir() / "art.json"),
    }


def get_relations() -> dict:
    """P1：人物关系图谱数据——角色节点 + 结构化关系边（relationship_spec）+ 别名。"""
    cast = _read_json(_story_dir() / "cast.json") or {}
    chars = []
    for c in cast.get("characters", []):
        persona = c.get("persona", {}) or {}
        rels = persona.get("relationship_spec") or persona.get("relationships") or []
        chars.append({
            "id": c.get("id"), "name": c.get("name"),
            "aliases": c.get("aliases", []),
            "relationships": [{"type": r.get("type"), "target": r.get("target"),
                               "note": r.get("note", "")} for r in rels],
        })
    return {"characters": chars}


# ---------------------------------------------------------------------------
# 上游执行（小说 → 契约）：子进程跑 story_cli，捕获进度流
# ---------------------------------------------------------------------------
_PIPE = {"running": False, "done": False, "ok": None, "error": "",
         "lines": [], "story": "", "out": "", "work": ""}


def list_novels() -> list[dict]:
    out = []
    for f in sorted(INPUT.glob("*.txt")):
        try:
            chars = len(f.read_text(encoding="utf-8-sig"))
        except Exception:  # noqa: BLE001
            chars = 0
        out.append({"name": f.name, "chars": chars})
    return out


def pipeline_status() -> dict:
    st = {k: _PIPE[k] for k in
          ("running", "done", "ok", "error", "lines", "story", "out", "work")}
    # 成功后 story_cli 会把临时层固化到 input/<stem>/ 与 state/story/<stem>/；
    # 这两个路径由小说名确定，直接回传，界面不必从日志里猜。
    stem = _novel_name(_PIPE["story"]) if _PIPE["story"] else ""
    st["out"] = str(INPUT / stem) if stem else ""
    st["work"] = str(STATE / stem) if stem else ""
    return st


def publish_info() -> dict:
    """发布页所需的路径、集数统计与已编译状态。"""
    cur = _current_info()
    stem = cur.get("novel") or ""
    out = (INPUT / stem) if stem else INPUT
    work = _story_dir()
    compiled = bool((out / "external" / "plan.json").is_file()) if stem else False
    return {
        "novel": stem,
        "work": str(work) if stem else "",
        "out": str(out) if stem else "",
        "compiled": compiled,
        "dirty": dirty_info(),
        "episodes": ep_summary(),
    }


def _parse_range(value) -> tuple[int, int] | None:
    """解析 "1-4" / "1–4" / [1,4] 形式的集数范围。"""
    if isinstance(value, (list, tuple)) and len(value) == 2:
        try:
            return int(value[0]), int(value[1])
        except (TypeError, ValueError):
            return None
    import re
    m = re.match(r"^\s*(\d+)\s*[-–—~]\s*(\d+)\s*$", str(value or ""))
    if not m:
        return None
    return int(m.group(1)), int(m.group(2))


def generation_defaults(cfg: dict | None = None) -> dict:
    """生成页的「上次使用」参数（唯一入口，配置页不再重复展示）。

    `studio.generation` 只保存上一次运行的选择；旧配置里的 `range` 仍可读，
    新运行会把它改写成明确的 from/to，避免同一语义存在两份数据。
    """
    cfg = cfg if cfg is not None else load_config()
    gen = ((cfg.get("studio") or {}).get("generation") or {})
    frm, to = gen.get("from"), gen.get("to")
    if frm in (None, "", 0) and to in (None, "", 0):
        rng = _parse_range(gen.get("range"))
        if rng:
            frm, to = rng
    eps_raw = gen.get("episodes", "auto")
    try:
        eps: int | None = int(eps_raw)
    except (TypeError, ValueError):
        eps = None
    try:
        chapters = int(gen.get("chapters_per_episode") or 0)
    except (TypeError, ValueError):
        chapters = 0
    if eps is not None:
        mode = "fixed"
    elif chapters > 0:
        mode = "chapters"
    else:
        mode = "auto"
    try:
        duration = float(gen.get("duration") or 90)
    except (TypeError, ValueError):
        duration = 90.0
    try:
        default_chapters = int((cfg.get("llm") or {})
                               .get("story_chapters_per_episode") or 0)
    except (TypeError, ValueError):
        default_chapters = 0
    return {
        "mode": mode,
        "episodes": eps,
        "chapters_per_episode": chapters,
        "default_chapters": default_chapters,
        "from": frm or "",
        "to": to or "",
        "ratio": gen.get("ratio") or "9:16",
        "duration": duration,
    }


def save_generation_defaults(opts: dict) -> None:
    """运行开始时记住本次参数，下次打开生成页直接预填。"""
    cfg = load_config()
    studio = cfg.setdefault("studio", {})
    gen = studio.setdefault("generation", {})
    eps = opts.get("episodes", "auto")
    try:
        gen["episodes"] = int(eps) if str(eps).strip().lower() != "auto" else "auto"
    except (TypeError, ValueError):
        gen["episodes"] = "auto"
    try:
        gen["chapters_per_episode"] = max(0, int(opts.get("chapters_per_episode") or 0))
    except (TypeError, ValueError):
        gen["chapters_per_episode"] = 0
    gen["from"] = opts.get("from") or ""
    gen["to"] = opts.get("to") or ""
    gen["ratio"] = str(opts.get("ratio") or "9:16")
    try:
        gen["duration"] = float(opts.get("duration") or 90)
    except (TypeError, ValueError):
        gen["duration"] = 90.0
    # 旧键清除：之后 from/to 是唯一事实来源。
    gen.pop("range", None)
    save_config(cfg)


def _build_pipeline_cmd(story_name: str, opts: dict) -> list[str]:
    """组装 story_cli 命令；Studio 默认续跑（--resume），避免失败后整部重烧。"""
    story = INPUT / story_name
    cmd = [sys.executable, "-m", "manju.story_cli", str(story), "--yes", "--force",
           "--ratio", str(opts.get("ratio", "9:16")),
           "--duration", str(opts.get("duration", 90))]
    # 不传 --workdir：story_cli 走临时工作层（写 .work_<小说名>，成功后固化到
    # state/story/<小说名> 与 input/<小说名>），--clean 只清临时、成品永不被删
    if not opts.get("fresh"):
        cmd += ["--resume"]
    ep = opts.get("episodes")
    if ep and str(ep) != "auto":
        cmd += ["--episodes", str(ep)]
    elif "chapters_per_episode" in opts:
        # 生成页每次都会显式给出 0 或 N：0 用来覆盖 config 里的旧章节默认值，
        # 让「按剧情自动分集」真正生效，而不是被 config 悄悄切回章节模式。
        try:
            cpe_i = max(0, int(opts.get("chapters_per_episode") or 0))
        except (TypeError, ValueError):
            cpe_i = 0
        cmd += ["--chapters-per-episode", str(cpe_i)]
    rng = _parse_range(opts.get("range"))
    frm = opts.get("from") if opts.get("from") not in (None, "") else (rng[0] if rng else None)
    to = opts.get("to") if opts.get("to") not in (None, "") else (rng[1] if rng else None)
    if frm:
        cmd += ["--from-episode", str(frm)]
    if to:
        cmd += ["--to-episode", str(to)]
    if opts.get("clean"):
        cmd += ["--clean"]
    return cmd


def _pipeline_worker(story_name: str, opts: dict) -> None:
    global _CURRENT_NOVEL
    _CURRENT_NOVEL = _novel_name(story_name)  # 中间/编译产物按小说名分目录
    _save_current(_CURRENT_NOVEL)
    _PIPE.update(running=True, done=False, ok=None, error="", lines=[], story=story_name)
    try:
        cmd = _build_pipeline_cmd(story_name, opts)
        env = os.environ.copy()
        key = _effective_api_key(load_config())
        if key:
            # 子进程只从环境变量拿 Key，不写日志/配置。
            env["MANJU_LLM_API_KEY"] = key
        proc = subprocess.Popen(cmd, cwd=ROOT, stdout=subprocess.PIPE,
                                stderr=subprocess.STDOUT, text=True,
                                encoding="utf-8", errors="replace", env=env)
        for line in proc.stdout:
            _PIPE["lines"].append(line.rstrip("\n"))
        rc = proc.wait()
        _PIPE["ok"] = (rc == 0)
        if rc != 0:
            _PIPE["error"] = f"story_cli 退出码 {rc}（详情见日志，常见为质量门未过/模型不可用）"
    except Exception as e:  # noqa: BLE001
        _PIPE["error"] = str(e)
        _PIPE["ok"] = False
    finally:
        _PIPE["running"] = False
        _PIPE["done"] = True


def start_pipeline(story_name: str, opts: dict) -> tuple[bool, str]:
    if _PIPE["running"]:
        return False, "已有上游任务在运行，请先等它结束"
    if not (INPUT / story_name).is_file():
        return False, f"小说不存在：{story_name}"
    try:
        save_generation_defaults(opts)
    except OSError:
        pass  # 配置只读时不影响本次运行
    threading.Thread(target=_pipeline_worker, args=(story_name, opts),
                     daemon=True).start()
    return True, f"已启动上游：{story_name}"


def get_setup() -> dict:
    """上游设定：结构化画风 visual_bible + 世界设定 WorldBible 四块。"""
    o = _read_json(_story_dir() / "outline.json")
    return {"visual_bible": o.get("visual_bible") or {},
            "world": o.get("world") or None}


def save_setup(vb: dict | None, world: dict | None) -> dict:
    o = _read_json(_story_dir() / "outline.json")
    if vb is not None:
        o["visual_bible"] = vb
    if world is not None:
        o["world"] = world
    _write_json(_story_dir() / "outline.json", o)
    mark_dirty("上游设定（画风/世界）已修改，建议重编译")
    return {"saved": True}


def get_visual() -> dict | None:
    o = _read_json(_story_dir() / "outline.json")
    return o.get("visual_bible")


def save_visual(vb: dict) -> None:
    """保存旧式画风字段：合并进现有 visual_bible，不覆盖结构化四键。

    Studio 的「配置」页仍保留 medium/lens 等旧字段；如果直接整体替换，
    会把上游设定页编辑的 style_keywords/main_colors/do_dont 全部冲掉。
    """
    o = _read_json(_story_dir() / "outline.json")
    cur = o.get("visual_bible")
    merged = dict(cur) if isinstance(cur, dict) else {}
    merged.update({k: v for k, v in vb.items() if v not in (None, "")})
    o["visual_bible"] = merged
    _write_json(_story_dir() / "outline.json", o)
    mark_dirty("画风 visual_bible 已修改")


def edit_shot(ep: int, idx: int, updates: dict) -> dict:
    b = get_board(ep)
    segs = _ep_obj(b).get("segments") or []
    cuts = [c for s in segs for c in (s.get("cuts") or [])]
    if not (0 <= idx < len(cuts)):
        raise ValueError(f"镜头索引 {idx} 越界（共 {len(cuts)}）")
    cut = cuts[idx]
    allowed = {"prompt_en", "size", "motion", "seconds", "coverage",
               "cut_reason", "shot", "camera"}
    # cut_reason / camera 都是契约字段，允许在这里微调；不再额外放宽结构字段。
    for k, v in updates.items():
        if k not in allowed:
            raise ValueError(f"不允许改字段 {k}")
        if k == "seconds":
            try:
                v = float(v)
            except (TypeError, ValueError):
                raise ValueError("seconds 必须是数字")
        cut[k] = v
    _write_json(_story_dir() / f"board_ep{ep:02d}.json", b)
    mark_dirty(f"EP{ep:02d} 镜头 {cut.get('shot') or idx + 1} 已修改")
    return {"ok": True, "idx": idx, "shot": cut.get("shot"), "fields": list(updates)}


def edit_asset(kind: str, aid: str, updates: dict) -> dict:
    """kind: cast|scene|prop；aid: 角色/场景/道具 id。"""
    a = get_assets()
    if kind == "cast":
        coll = a["cast"]["characters"]
    elif kind == "scene":
        coll = a["art"]["scenes"]
    elif kind == "prop":
        coll = a["art"]["props"]
    else:
        raise ValueError(f"未知资产类型 {kind}")
    obj = next((x for x in coll if str(x.get("id")) == str(aid)), None)
    if obj is None:
        raise ValueError(f"找不到 {kind} {aid}")
    allowed = {"name", "summary", "color_signature", "one_liner",
               "scale", "carried_by", "variant_of"}
    img_keys = {"prompt", "negative_prompt", "sheet", "tags"}
    for k, v in updates.items():
        if k in allowed:
            obj[k] = v
        elif k in img_keys:
            obj.setdefault("image", {})[k] = v
        else:
            raise ValueError(f"不允许改字段 {k}")
    if kind == "cast":
        _write_json(_story_dir() / "cast.json", a["cast"])
    else:
        _write_json(_story_dir() / "art.json", a["art"])
    mark_dirty(f"{kind} {aid} 已修改")
    return {"ok": True, "kind": kind, "id": aid, "fields": list(updates)}


def do_compile() -> dict:
    """把当前小说中间产物编译为契约；编译前先过质量门，编译后再跑 gate/drama。

    手工改过镜头/资产后直接编译很容易写出不合规契约，所以这里不再静默通过：
    任何硬门或下游 block 都会以可读错误返回，且不会清除「待重编译」标记。
    """
    from manju.config import get_config  # noqa: PLC0415
    from manju.story.models import (  # noqa: PLC0415
        Art, Cast, Outline, ScriptEpisode, Staging, StoryEpisodeBoard,
        StoryStoryboard, face_lock_required,
    )
    from manju.story.compile import compile_all  # noqa: PLC0415
    from manju.story.stages import (  # noqa: PLC0415
        _merge_stagings, _repair_staging_output, params_from_cfg,
    )
    from manju.story.validate import validate_all  # noqa: PLC0415

    d = _story_dir()
    eps = list_eps()
    if not eps:
        raise ValueError(f"{d} 下没有 board_epNN.json，请先完成上游生成")
    required = [d / "outline.json", d / "cast.json", d / "art.json"]
    for ep in eps:
        required += [d / f"script_ep{ep:02d}.json",
                     d / f"staging_ep{ep:02d}.json",
                     d / f"board_ep{ep:02d}.json"]
    missing = [p.name for p in required if not p.is_file()]
    if missing:
        raise ValueError("缺少中间产物：" + "、".join(missing)
                         + "。请先续跑上游生成（Studio 已默认 --resume）。")

    outline = Outline(**_read_json(d / "outline.json"))
    cast = Cast(**_read_json(d / "cast.json"))
    art = Art(**_read_json(d / "art.json"))
    scripts: list[ScriptEpisode] = []
    boards: list[StoryEpisodeBoard] = []
    stagings: list[Staging] = []
    staging_fixes: list[str] = []
    for ep in eps:
        script = ScriptEpisode(**_read_json(d / f"script_ep{ep:02d}.json"))
        scripts.append(script)
        st = Staging(**_read_json(d / f"staging_ep{ep:02d}.json"))
        # 旧缓存可能早于 staging 修复：人物站位引用了地点物件表里没有的名字。
        # 这是纯记账问题，编译前按与生成阶段相同的规则补登记，避免整包被硬门挡住。
        _repair_staging_output(st, script, staging_fixes.append)
        stagings.append(st)
        boards.append(StoryEpisodeBoard(
            **_ep_obj(_read_json(d / f"board_ep{ep:02d}.json"))))
    # 同一 scene_id 可能被多集复用，必须按名字求物件表并集；直接拼接会让
    # 后一集的窄表覆盖前一集，误报“item 不在地点物件表里”。
    staging = _merge_stagings(stagings, outline.source or "")
    storyboard = StoryStoryboard(source=outline.source or "", episodes=boards)

    # 单镜时长/人脸数/台词句数/分段上限都是引擎能力，不是故事层规则：
    # 缺省不限制人脸与句数，只有 config 显式配置才收紧。
    sp = params_from_cfg(get_config())
    pre = validate_all(outline, cast, art, scripts, staging, storyboard,
                       strict=False, params=sp)
    bad = [r for r in pre if not r.ok]
    if bad:
        raise ValueError("质量门未通过，未编译：\n"
                         + "\n".join(r.text() for r in bad))

    out_dir = (INPUT / _CURRENT_NOVEL) if _CURRENT_NOVEL else INPUT
    written = compile_all(outline, cast, art, scripts, staging, storyboard,
                          out_dir, params=sp)

    # 编译后下游确定性审计（与 story_cli 口径一致）
    from manju.drama import audit_files  # noqa: PLC0415
    from manju.gate import check_files  # noqa: PLC0415

    all_paths = [written["plan"]]
    for ep in scripts:
        all_paths += [written[f"script_ep{ep.ep:02d}"],
                      written[f"storyboard_ep{ep.ep:02d}"]]
    # 与 CLI 同一口径：宠物/稻草人/面具不占正脸名额；上限同样取引擎能力配置。
    no_face = {c.id for c in cast.characters if not face_lock_required(c)}
    gate_rep = check_files(all_paths, tolerance=0.2, no_face_chars=no_face,
                           speech_cps=sp.chars_per_second,
                           max_faces_per_shot=sp.max_faces_per_shot)
    if gate_rep.blocks:
        raise ValueError("契约 gate 审计未通过（文件已写出，请修正后重编译）：\n"
                         + gate_rep.to_text())
    drama_bad: list[str] = []
    for ep in scripts:
        dr = audit_files([written["plan"], written[f"script_ep{ep.ep:02d}"],
                          written[f"storyboard_ep{ep.ep:02d}"]],
                         written[f"director_ep{ep.ep:02d}"],
                         target_sec=ep.target_seconds,
                         max_shot_sec=sp.max_shot_seconds,
                         max_line_chars=sp.max_line_chars)
        if not dr.ok():
            drama_bad.append(dr.to_text())
    if drama_bad:
        raise ValueError("drama 审计未通过（文件已写出，请修正后重编译）：\n"
                         + "\n".join(drama_bad))

    clear_dirty()
    return {"ok": True, "written": len(written), "eps": eps, "out": str(out_dir),
            "warnings": sum(len(r.warnings) for r in pre),
            "repairs": staging_fixes}


# ---------------------------------------------------------------------------
# HTTP handler
# ---------------------------------------------------------------------------
class Handler(BaseHTTPRequestHandler):
    server_version = "ManjuStudio/0.1"

    def _send(self, code: int, body: str, ctype: str = "application/json; charset=utf-8") -> None:
        data = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def _json(self, code: int, obj) -> None:
        self._send(code, json.dumps(obj, ensure_ascii=False))

    def _body(self) -> dict:
        n = int(self.headers.get("Content-Length") or 0)
        if not n:
            return {}
        try:
            return json.loads(self.rfile.read(n).decode("utf-8"))
        except Exception:  # noqa: BLE001
            return {}

    # -- GET --
    def do_GET(self):  # noqa: N802
        u = urlparse(self.path)
        path = u.path
        if path in ("/", "/index.html", "/studio"):
            self._send(200, INDEX_HTML, "text/html; charset=utf-8")
            return
        try:
            if path == "/api/overview":
                cfg = load_config()
                llm = cfg.get("llm", {})
                api_key = _effective_api_key(cfg)
                self._json(200, {
                    "eps": list_eps(),
                    "ep_stats": ep_summary(),
                    "dirty": dirty_info(),
                    "llm": {**llm,
                            "api_key": mask_key(api_key),
                            "api_key_source": _effective_api_key_source(cfg)},
                    "story": cfg.get("story", {}),
                    "studio": cfg.get("studio", {}),
                    "generation": generation_defaults(cfg),
                    "publish": publish_info(),
                    "current": _current_info(),
                })
            elif path == "/api/publish":
                self._json(200, publish_info())
            elif path == "/api/visual":
                self._json(200, {"visual_bible": get_visual() or {}})
            elif path == "/api/assets":
                a = get_assets()
                self._json(200, {"cast": a["cast"], "art": a["art"]})
            elif path == "/api/cast/relations":
                self._json(200, get_relations())
            elif path == "/api/pipeline/novels":
                self._json(200, {"novels": list_novels()})
            elif path == "/api/novel/current":
                self._json(200, _current_info())
            elif path == "/api/pipeline/status":
                self._json(200, pipeline_status())
            elif path == "/api/setup":
                self._json(200, get_setup())
            elif path == "/api/trace":
                self._json(200, read_llm_trace())
            elif path.startswith("/api/ep/"):
                ep = int(path.rsplit("/", 1)[-1])
                obj = _ep_obj(get_board(ep))
                segs = obj.get("segments") or []
                meta = _outline_eps().get(ep) or {}
                cuts = []
                for s in segs:
                    for c in (s.get("cuts") or []):
                        cuts.append({
                            "idx": len(cuts),
                            "shot": c.get("shot") or "",
                            "beats": c.get("beats"),
                            "seconds": c.get("seconds"),
                            "size": c.get("size"),
                            "camera": c.get("camera"),
                            "motion": c.get("motion"),
                            "prompt": c.get("prompt_en", ""),
                            "coverage": c.get("coverage", ""),
                            "cut_reason": c.get("cut_reason", ""),
                            "scene": s.get("scene_index"),
                            "props": c.get("props", []),
                            "characters": c.get("characters", []),
                        })
                secs = 0.0
                for c in cuts:
                    try:
                        secs += float(c.get("seconds") or 0)
                    except (TypeError, ValueError):
                        pass
                self._json(200, {
                    "ep": ep,
                    "title": meta.get("title") or "",
                    "synopsis": meta.get("synopsis") or "",
                    "scene_count": len(segs),
                    "cuts": cuts,
                    "seconds": round(secs, 1),
                })
            else:
                self._json(404, {"error": "not found", "path": path})
        except Exception as e:  # noqa: BLE001
            self._json(500, {"error": str(e)})

    # -- PUT / POST --
    def do_PUT(self):  # noqa: N802
        u = urlparse(self.path)
        b = self._body()
        try:
            if u.path == "/api/config":
                save_config(apply_config_update(load_config(), b))
                self._json(200, {"ok": True})
            elif u.path == "/api/visual":
                if "visual_bible" not in b:
                    raise ValueError("缺 visual_bible")
                save_visual(b["visual_bible"])
                self._json(200, {"ok": True, "dirty": True})
            elif u.path == "/api/setup":
                save_setup(b.get("visual_bible"), b.get("world"))
                self._json(200, {"ok": True, "dirty": True})
            elif u.path.startswith("/api/ep/") and "/shot/" in u.path:
                parts = u.path.strip("/").split("/")
                # /api/ep/{n}/shot/{idx}
                ep = int(parts[2]); idx = int(parts[4])
                if "updates" not in b:
                    raise ValueError("缺 updates")
                r = edit_shot(ep, idx, b["updates"])
                self._json(200, {**r, "dirty": dirty_info()})
            elif u.path == "/api/novel/current":
                name = _novel_name((b.get("novel") or "").strip()) if b.get("novel") else ""
                if name and any(ch in name for ch in ("/", "\\", "..", ":")):
                    raise ValueError("非法小说名")
                global _CURRENT_NOVEL
                _CURRENT_NOVEL = name
                _save_current(name)
                self._json(200, {"ok": True, **_current_info()})
            elif u.path == "/api/assets":
                if not all(k in b for k in ("kind", "id", "updates")):
                    raise ValueError("缺 kind/id/updates")
                r = edit_asset(b["kind"], b["id"], b["updates"])
                self._json(200, {**r, "dirty": dirty_info()})
            else:
                self._json(404, {"error": "not found", "path": u.path})
        except Exception as e:  # noqa: BLE001
            self._json(400, {"error": str(e)})

    def do_POST(self):  # noqa: N802
        u = urlparse(self.path)
        if u.path == "/api/compile":
            try:
                r = do_compile()
                self._json(200, {**r, "dirty": dirty_info()})
            except Exception as e:  # noqa: BLE001
                self._json(500, {"error": str(e), "dirty": dirty_info()})
        elif u.path == "/api/pipeline/run":
            try:
                b = self._body()
                ok, msg = start_pipeline(str(b.get("story", "")), b.get("opts", {}))
                code = 200 if ok else 409
                self._json(code, {"ok": ok, "msg": msg})
            except Exception as e:  # noqa: BLE001
                self._json(500, {"error": str(e)})
        else:
            self._json(404, {"error": "not found"})

    def log_message(self, *a) -> None:  # 静默
        pass


# ---------------------------------------------------------------------------
# 前端单页
# ---------------------------------------------------------------------------
INDEX_HTML = (Path(__file__).with_name("studio_ui.html")
               .read_text(encoding="utf-8"))



def main(argv: list[str] | None = None) -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:  # noqa: BLE001
        pass
    p = argparse.ArgumentParser(prog="python -m manju.studio",
                                description="Manju Studio 输入层图形管理台")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=8092)
    a = p.parse_args(argv)
    if not _story_dir().exists():
        _story_dir().mkdir(parents=True, exist_ok=True)
    srv = ThreadingHTTPServer((a.host, a.port), Handler)
    print(f"[OK] Manju Studio 已启动: http://{a.host}:{a.port}")
    print("  浏览器打开即可配置 / 看改分镜 / 编辑资产 / 重编译契约。Ctrl+C 停止。")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
