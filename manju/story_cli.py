# -*- coding: utf-8 -*-
"""纯输入层命令行入口：小说 → v10.2 契约。

与 `manju.cli novel` 的区别：
* 不依赖渲染流水线、ComfyUI、工作流文件；
* 只做生成、质量门、编译、gate/drama 审计；
* 可单独复制 `manju/` 的精简模块运行。

用法：
    python -m manju.story_cli input/my_story.txt
    python -m manju.story_cli input/my_story.txt --review
    python -m manju.story_cli input/my_story.txt --review --resume
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from . import __version__
from .config import api_key_source, get_config
from .llm import LLMClient
from .story.compile import compile_all
from .story.episodes import (
    EpisodeError, needs_auto_confirmation, split_episodes, write_slices,
)
from .story.ingest import IngestError, parse as parse_novel, parse_text
from .story.models import face_lock_required
from .story.plot import build_plot_atoms, design_signature, prefix_cut
from .story.review import (
    approve_outline, outline_gate_status, request_outline_review,
    write_failure_packet,
)
from .story.stages import (
    STORY_RULES_REV, StoryGateError, StoryPipeline, params_from_cfg,
)
from .story.validate import validate_all

OK = "[OK]"
FAIL = "[FAIL]"
WARN = "[WARN]"


def _episode_duration(value: str) -> float:
    """一集按剧情定时长，但总时长必须落在 80-90s 之间（硬区间）。"""
    try:
        sec = float(value)
    except (TypeError, ValueError):
        raise argparse.ArgumentTypeError(f"单集时长必须是数字：{value!r}")
    if not 80.0 <= sec <= 90.0:
        raise argparse.ArgumentTypeError(
            f"单集时长 {sec:g}s 超出范围：一集按剧情定时长，"
            f"但必须落在 80-90s 之间（本次 --duration 180 为历史遗留，已不允许）")
    return sec


def _out(text: str = "") -> None:
    print(text, flush=True)


def _episodes_arg(value: str):
    if str(value).lower() == "auto":
        return "auto"
    try:
        n = int(value)
    except ValueError:
        raise argparse.ArgumentTypeError("--episodes 只能是 auto 或正整数")
    if n < 1:
        raise argparse.ArgumentTypeError("--episodes 必须 ≥ 1")
    return n


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="python -m manju.story_cli",
        description="小说 / 短故事 → 可渲染的 v10.2 漫剧契约（不依赖 ComfyUI）",
    )
    p.add_argument("story", nargs="?", help="UTF-8 小说或短故事文本")
    p.add_argument("--version", action="version",
                   version=f"manju-story-input {__version__}")
    p.add_argument("--doctor", action="store_true",
                   help="只打印版本、项目路径、配置与 Key 是否存在，不读小说、不调模型")
    p.add_argument("--episodes", type=_episodes_arg, default="auto",
                   help="auto=情节模式（LLM 按戏剧单元分集）；"
                        "给正整数=强制按字数切成 N 集")
    p.add_argument("--plot-mode", action=argparse.BooleanOptionalAction,
                   default=None,
                   help="auto 时先读全文、按情节单元分集（默认开，可用 --no-plot-mode 关闭）；"
                        "显式 --episodes N / --episode-map / --chapters-per-episode 会覆盖它")
    p.add_argument("--chars-per-episode", type=int, default=None,
                   help="关闭情节模式后的每集预算字数（默认取 config "
                        "llm.story_chars_per_episode=9000）")
    p.add_argument("--chapters-per-episode", type=int, default=None,
                   help="每几章一集（如 2/3）；0=不按章节。指定后自动关闭情节模式。"
                        "默认取 config llm.story_chapters_per_episode=0")
    p.add_argument("--episode-map", default=None,
                   help="人工分集 JSON：{ep: {chapters:[a,b]}} 或 {ep: {start,end}}")
    p.add_argument("--cleanup", action=argparse.BooleanOptionalAction, default=True,
                   help="小说预处理：清理广告/网址、统一换行与多余空格（默认开）")
    p.add_argument("--yes", action="store_true",
                   help="自动分集超过上限（默认 12 集）时不再询问")
    p.add_argument("--list-chapters", action="store_true",
                   help="只识别编码与章节、打印章节表后退出")
    p.add_argument("--dry-run", action="store_true",
                   help="只解析与分集，不调用 LLM、不写盘")
    p.add_argument("--from-episode", type=int, default=None,
                   help="只生成从这一集起（含）")
    p.add_argument("--to-episode", type=int, default=None,
                   help="只生成到这一集止（含）")
    p.add_argument("--report", action=argparse.BooleanOptionalAction,
                   default=True,
                   help="生成 index.html/epNN.html/report.webp（默认开）")
    p.add_argument("--duration", type=_episode_duration, default=90.0,
                   help="单集目标秒数：一集按剧情定时长，但必须落在 80-90s（默认 90）")
    p.add_argument("--ratio", choices=["9:16", "16:9"], default="9:16")
    p.add_argument("--out", default=None, help="输出目录（默认 input/；显式指定时不走临时层）")
    p.add_argument("--workdir", default=None,
                   help="中间产物目录（默认 state/story）")
    p.add_argument("--review", action="store_true",
                   help="只生成大纲/角色/美术并停下，等人工确认")
    p.add_argument("--review-outline", action="store_true",
                   help="只生成大纲就停下，写审阅页；批准后才继续")
    p.add_argument("--approve", choices=["outline"], default=None,
                   help="批准大纲；与 --resume 同用则继续生成")
    p.add_argument("--ignore-review", action="store_true",
                   help="显式跳过未批准的大纲阻塞（危险）")
    p.add_argument("--on-gate-fail", choices=["stop", "ask"], default="stop",
                   help="质量门失败：stop=退出（默认）；ask=交互等待修复后重试")
    p.add_argument("--resume", action="store_true",
                   help="复用已通过质量门的中间产物")
    p.add_argument("--force", action="store_true", help="覆盖已有编译产物")
    p.add_argument("--clean", action="store_true",
                   help="只清除当前小说临时工作目录（state/story/.work_<小说名>）后从头生成，已成功固化的成品不受影响")
    p.add_argument("--strict", action="store_true",
                   help="软门（场数/句数/抉择/受挫/爽点间隔）恢复为错误")
    p.add_argument("--llm-url", default=None,
                   help="OpenAI 兼容服务地址，如 https://api.deepseek.com/v1")
    p.add_argument("--model", default=None,
                   help="临时覆盖 llm.model（如 deepseek-flash）；不写回 config")
    p.add_argument("--llm-timeout", type=float, default=None,
                   help="单次 LLM 请求超时秒数；默认取 llm.request_timeout_sec")
    return p


def clean_workdir(workdir: Path, out: Path, yes: bool, _out) -> bool:
    """清除中间产物与编译产物，保证从零开始重新生成。

    - 只清除当前小说的"临时工作目录"：state/story/.work_<小说名> 下的
      *.json / *.txt 中间产物，以及 <out>/.work_<小说名>/external 编译产物；
    - 不动已成功固化到 state/story/<小说名> 与 <out>/<小说名> 的成品。
    保留 input/ 下的小说 .txt。无 --yes 时逐项确认。
    """
    import shutil
    targets: list[Path] = []
    if workdir.is_dir():
        for p in workdir.iterdir():
            if (p.is_file() and p.suffix in (".json", ".txt")
                    and p.name not in (".gitkeep",)):
                targets.append(p)
    if out.name.startswith(".work_"):
        # 临时输出目录整体删除：只删 external/ 会把旧的 epNN/storyboard 留在原地，
        # 下次编译后可能被一起固化进最终目录。
        if out.is_dir():
            targets.append(out)
    else:
        ext = out / "external"
        if ext.is_dir():
            targets.append(ext)
    if not targets:
        _out("[CLEAN] 没有可清除的中间/编译产物，跳过。")
        return True
    _out(f"[CLEAN] 将清除 {len(targets)} 项：")
    for t in targets[:20]:
        _out(f"  - {t}")
    if len(targets) > 20:
        _out(f"  ... 共 {len(targets)} 项")
    if not yes:
        ans = input("确认清除以上中间/编译产物？[y/N] ").strip().lower()
        if ans not in ("y", "yes"):
            _out("[CLEAN] 已取消。")
            return False
    for t in targets:
        if t.is_dir():
            shutil.rmtree(t)
        else:
            t.unlink(missing_ok=True)
    _out(f"[CLEAN] 已清除 {len(targets)} 项，将从零开始生成。")
    return True


def _health_detail(health: dict) -> str:
    """把健康检查结果压成一行可读原因；绝不包含 API Key。"""
    status = health.get("status")
    if status is not None:
        body = health.get("body")
        message = ""
        if isinstance(body, dict):
            err = body.get("error")
            if isinstance(err, dict):
                message = str(err.get("message") or err.get("type") or "")
            elif err:
                message = str(err)
            elif body.get("message"):
                message = str(body.get("message"))
        elif isinstance(body, str):
            message = body
        text = f"HTTP {status}"
        if message:
            text += f" - {message}"
        return text[:300]
    return str(health.get("error") or "未知错误")[:300]


def _walk_note_keys(obj, prefix: str = "") -> list[str]:
    """递归找出 config 里的 `_说明`/`_note` 注释键（仅用于诊断）。"""
    found: list[str] = []
    if isinstance(obj, dict):
        for key, value in obj.items():
            path = f"{prefix}.{key}" if prefix else key
            if str(key).startswith("_"):
                found.append(path)
            found.extend(_walk_note_keys(value, path))
    elif isinstance(obj, list):
        for i, value in enumerate(obj):
            found.extend(_walk_note_keys(value, f"{prefix}[{i}]"))
    return found


def _has_dotted(obj: dict, dotted: str) -> bool:
    cur = obj
    for part in dotted.split("."):
        if not isinstance(cur, dict) or part not in cur:
            return False
        cur = cur[part]
    return True


def _doctor_config_warnings(cfg_path: Path) -> None:
    """配置卫生检查：明文 Key、遗留注释键、输入层不读取的旧键。"""
    if not cfg_path.is_file():
        return
    try:
        raw = json.loads(cfg_path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError) as e:
        _out(f"  {WARN} 无法读取配置文件做卫生检查：{e}")
        return
    if not isinstance(raw, dict):
        return

    raw_llm = raw.get("llm")
    raw_key = str(raw_llm.get("api_key") or "") if isinstance(raw_llm, dict) else ""
    if raw_key:
        _out(f"  {WARN} config.json 仍有明文 llm.api_key；"
             "建议改用环境变量 MANJU_LLM_API_KEY / DEEPSEEK_API_KEY / OPENAI_API_KEY。")

    notes = _walk_note_keys(raw)
    if notes:
        _out(f"  {WARN} config.json 含 {len(notes)} 个说明/注释键；"
             "建议移到 docs/decision-log.md，配置只保留可执行字段。")

    dead = (
        "pipeline.llm_mode",
        "pipeline.contract_gate",
        "pipeline.drama_gate",
    )
    for key in dead:
        if _has_dotted(raw, key):
            _out(f"  {WARN} config.json 含 {key}；输入层不读取该键，建议删除。")

    legacy = (
        "pipeline.max_shot_seconds",
        "pipeline.max_segment_seconds",
        "pipeline.min_shot_seconds",
        "pipeline.pulid.max_faces_per_shot",
        "pipeline.pulid.no_face_chars",
        "pipeline.lipsync.max_lines_per_shot",
    )
    for key in legacy:
        if _has_dotted(raw, key):
            _out(f"  {WARN} {key} 是旧键；请迁移到 story.*（仍兼容，但不再是唯一来源）。")

    if _has_dotted(raw, "llm.temperature"):
        _out(f"  {WARN} config.json 含 llm.temperature；各阶段已显式固定温度，"
             "该键只会误导，建议删除。")


def _doctor() -> int:
    """只读诊断：路径、配置、API Key 是否存在。**绝不打印 Key 内容。**"""
    import platform

    _out(f"manju-story-input {__version__}")
    pkg_root = Path(__file__).resolve().parent.parent
    _out(f"  package: {pkg_root}")
    _out(f"  cwd: {Path.cwd()}")
    env_root = os.environ.get("MANJU_ROOT")
    _out(f"  MANJU_ROOT: {env_root or '<unset>'}")
    try:
        cfg = get_config(reload=True)
    except SystemExit as e:
        _out(f"  config: ERROR {e}")
        return 2

    cfg_path = Path(cfg.source) if cfg.source else Path(cfg.root) / "config.json"
    _out(f"  config: {cfg_path} (exists={cfg_path.is_file()})")
    _out(f"  project_root: {cfg.root}")
    for label, key in (("state", "paths.state"), ("output", "paths.output"),
                       ("logs", "paths.logs")):
        raw = Path(str(cfg.get(key, label)))
        path = raw if raw.is_absolute() else cfg.root / raw
        _out(f"  {label}: {path}")
    _out(f"  input: {cfg.root / 'input'}")

    source = api_key_source(cfg)
    _out(f"  api_key: {source} (value never printed)")
    _out(f"  plot_mode: {cfg.get('llm.story_plot_mode', True)}")
    _out(f"  model: {cfg.get('llm.model')}  base_url: {cfg.get('llm.base_url')}")
    _out(f"  python: {platform.python_version()}")
    _doctor_config_warnings(cfg_path)

    try:
        same_root = cfg.root.resolve() == pkg_root.resolve()
    except OSError:
        same_root = False
    if not same_root:
        _out(f"  {WARN} project_root 与包目录不一致：{cfg.root} != {pkg_root}")
        _out("       请在本项目目录运行，或设置 MANJU_ROOT 指向本项目；"
             "否则会读写到另一个版本的数据。")
    if source == "missing":
        _out(f"  {WARN} 未配置 API Key，真实生成会失败；离线测试不受影响。")
        _out("       请设置环境变量 DEEPSEEK_API_KEY（或 MANJU_LLM_API_KEY）。")
    return 0


def main(argv: list[str] | None = None) -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:  # noqa: BLE001 - 老终端不支持就算了
        pass
    args = build_parser().parse_args(argv)

    if args.doctor:
        return _doctor()
    if not args.story:
        _out(f"{FAIL} 缺少小说文件。用法：python -m manju.story_cli <小说.txt> [选项]")
        return 2

    # 临时覆盖只影响本次进程，不改 config.json。
    if args.model:
        os.environ["MANJU_LLM_MODEL"] = str(args.model)
    if args.llm_timeout:
        os.environ["MANJU_LLM_TIMEOUT_SEC"] = str(args.llm_timeout)

    cfg = get_config()
    src = Path(args.story)
    if not src.is_absolute():
        src = Path(cfg.root) / src
    if not src.is_file():
        _out(f"{FAIL} 小说文件不存在：{src}")
        return 2
    try:
        novel = parse_novel(src, cleanup=args.cleanup)
    except IngestError as e:
        _out(f"{FAIL} 读不了 {src}：{e}")
        return 2
    text = novel.text

    def _cleanup_line() -> str:
        r = novel.cleanup
        if r is None:
            return "  清理：未启用"
        extra = ""
        if r.removed_lines:
            cats = "、".join(f"{k} {v}" for k, v in sorted(r.removed_by.items()))
            extra = f"；删除 {r.removed_lines} 行（{cats}）"
        warn = f"；{len(r.warnings)} 条告警" if r.warnings else ""
        return (f"  清理：原始 {r.before_chars} → 当前 {r.after_chars} 字符"
                f"{extra}{warn}")

    if args.list_chapters:
        _out(f"{OK} {src}")
        _out(f"  编码：{novel.encoding}   当前字符：{novel.total_chars}   "
             f"有效字符：{novel.content_chars}   汉字：{novel.cjk_chars}")
        _out(f"  {'识别到章节' if novel.chaptered else '无章节，按字数切段'}   "
             f"段数：{len(novel.chapters)}")
        _out(_cleanup_line())
        if novel.cleanup:
            for w in novel.cleanup.warnings:
                _out(f"  [WARN] {w}")
        _out(novel.table())
        return 0

    chars_per_ep = int(args.chars_per_episode
                       or cfg.get("llm.story_chars_per_episode", 9000))
    episode_map = args.episode_map
    if episode_map and not Path(episode_map).is_absolute():
        episode_map = str(Path(cfg.root) / episode_map)

    # 显式集数（--episodes N）与显式每几章一集互斥；auto 时才启用章节模式。
    if args.episodes != "auto" and args.chapters_per_episode not in (None, 0):
        _out(f"{FAIL} --episodes 与 --chapters-per-episode 不能同时指定"
             "（--chapters-per-episode 0 表示关闭章节模式，可以并存）")
        return 2
    if args.episodes != "auto":
        chapters_per_ep = 0
    elif args.chapters_per_episode is not None:
        chapters_per_ep = args.chapters_per_episode
    else:
        try:
            chapters_per_ep = int(cfg.get("llm.story_chapters_per_episode", 0) or 0)
        except (TypeError, ValueError):
            chapters_per_ep = 0
    if chapters_per_ep < 0:
        _out(f"{FAIL} --chapters-per-episode 必须 ≥ 0")
        return 2
    use_chapter = bool(chapters_per_ep and not episode_map)
    if use_chapter and not novel.chaptered:
        _out(f"{WARN} 未识别到章节，回退按字数分集"
             f"（提示：每 2 万字切段，当前 {len(novel.chapters)} 段）")
        use_chapter = False

    # 情节模式：auto + 未显式指定集数/章节/人工分集时，先通读全文由 LLM
    # 按戏剧单元分集，而不是硬按 9000 字或 2 章切。
    plot_enabled = (args.plot_mode if args.plot_mode is not None
                    else bool(cfg.get("llm.story_plot_mode", True)))
    plot_mode = (plot_enabled and args.episodes == "auto"
                 and not episode_map and args.chapters_per_episode in (None, 0)
                 and chapters_per_ep == 0)
    if args.episodes != "auto" or episode_map or args.chapters_per_episode:
        plot_mode = False

    max_auto = int(cfg.get("llm.story_max_auto_episodes", 12))
    slices = []
    if plot_mode:
        if args.dry_run:
            try:
                atom_novel = novel
                prefix_chars = 0
                if args.to_episode:
                    per = max(2000, int(cfg.get(
                        "llm.story_plot_prefix_chars_per_episode", 5000)))
                    prefix_chars = min(len(novel.text),
                                       int(args.to_episode) * per)
                    if prefix_chars < len(novel.text):
                        prefix_chars = prefix_cut(novel.text, prefix_chars)
                        atom_novel = parse_text(novel.text[:prefix_chars],
                                                cleanup=False)
                atoms = build_plot_atoms(
                    atom_novel,
                    atom_chars=int(cfg.get("llm.story_plot_atom_chars", 2400)),
                    excerpt_chars=int(cfg.get("llm.story_plot_atom_excerpt", 900)),
                    max_atoms=int(cfg.get("llm.story_plot_max_atoms", 180)),
                    total_excerpt_chars=int(
                        cfg.get("llm.story_plot_total_chars", 100000)))
            except Exception as e:  # noqa: BLE001
                _out(f"{FAIL} 情节原子切分失败：{type(e).__name__}: {e}")
                return 2
            _out(f"{OK} {src}")
            _out(f"  编码：{novel.encoding}   当前字符：{novel.total_chars}   "
                 f"有效字符：{novel.content_chars}   汉字：{novel.cjk_chars}")
            _out(_cleanup_line())
            if novel.cleanup:
                for w in novel.cleanup.warnings:
                    _out(f"  [WARN] {w}")
            _out(f"  模式：情节分集（按戏剧单元，不按字数/章节）；"
                 f"共 {len(atoms)} 个情节原子")
            if prefix_chars and prefix_chars < len(novel.text):
                _out(f"  只生成前 {args.to_episode} 集：设计阶段只读前 "
                     f"{prefix_chars} 字（全文 {len(novel.text)} 字），"
                     "不替未生成的后文设计分集")
            _out("  提示：实际集数由 LLM 通读后按 must_show 容量决定；"
                 + (f"本次只生成前 {args.to_episode} 集（不超过该上限）。"
                    if args.to_episode else "确认后直接运行即可。"))
            for a in atoms:
                chap = (f"章 {a.chapters[0]}–{a.chapters[-1]}"
                        if a.chapters else "无章节")
                _out(f"    ATOM{a.index:03d}  {a.chars:>6} 字  "
                     f"{a.start:>7}-{a.end:<7}  {chap}")
            _out("\n[DRY-RUN] 只预览情节原子，不调用 LLM、不写盘。")
            return 0
    else:
        try:
            slices = split_episodes(
                novel, chars_per_episode=chars_per_ep,
                episodes=None if args.episodes == "auto" else args.episodes,
                episode_map=episode_map,
                chapters_per_episode=chapters_per_ep if use_chapter else None)
        except EpisodeError as e:
            _out(f"{FAIL} 分集失败：{e}")
            return 2

        auto_mode = args.episodes == "auto" and not episode_map
        if (auto_mode and needs_auto_confirmation(len(slices), max_auto)
                and not args.yes):
            _out(f"{WARN} 自动分集得到 {len(slices)} 集，超过 {max_auto} 集上限。")
            if not sys.stdin.isatty():
                _out(f"{FAIL} 非交互环境请加 --yes 确认，或用 --episodes N 指定集数。")
                return 2
            answer = input(f"  继续按 {len(slices)} 集生成？[y/N] ").strip().lower()
            if answer not in ("y", "yes"):
                _out("已取消。")
                return 2

        if args.dry_run:
            _out(f"{OK} {src}")
            _out(f"  编码：{novel.encoding}   当前字符：{novel.total_chars}   "
                 f"有效字符：{novel.content_chars}   汉字：{novel.cjk_chars}")
            _out(_cleanup_line())
            if novel.cleanup:
                for w in novel.cleanup.warnings:
                    _out(f"  [WARN] {w}")
            mode = (f"按章节：每 {chapters_per_ep} 章一集"
                    if use_chapter else f"按字数：每集约 {chars_per_ep} 字")
            _out(f"  {'识别到章节' if novel.chaptered else '无章节，按字数切段'}；"
                 f"{mode}")
            _out(f"  分集：{len(slices)} 集")
            for s in slices:
                chap = (f"章 {s.chapter_indexes[0]}–{s.chapter_indexes[-1]}"
                        if s.chapter_indexes else "无章节")
                _out(f"    EP{s.index:03d}  {s.chars:>6} 字  "
                     f"{s.start:>7}-{s.end:<7}  {chap}")
            _out("\n[DRY-RUN] 只解析与分集，不调用 LLM、不写盘。")
            return 0

    ep_range = None

    novel_stem = src.stem  # 每部小说的产物各一个目录，互不干扰
    # 生成先写"临时工作目录"(.work_)，完整成功后才固化到最终目录并清空临时；
    # 这样 --clean 只清临时，永不触碰已成功的成品。
    use_tmp = not args.workdir and not args.out
    state_story = Path(cfg.p("state")) / "story"
    if use_tmp:
        workdir = state_story / (".work_" + novel_stem)
        final_workdir = state_story / novel_stem
    else:
        workdir = (Path(args.workdir) if args.workdir
                   else state_story / novel_stem)
        final_workdir = None
    if not workdir.is_absolute():
        workdir = Path(cfg.root) / workdir

    if args.review_outline and args.approve:
        _out(f"{FAIL} --review-outline 与 --approve 不能同时使用。")
        return 2
    if args.approve == "outline":
        ok, msg = approve_outline(cfg, workdir)
        _out(f"{OK if ok else FAIL} {msg}")
        if not ok:
            return 2
        if not args.resume:
            _out("  继续生成：加 --resume 再运行一次，或用 --resume --approve outline 一步完成。")
            return 0

    if not args.review_outline:
        ok, msg = outline_gate_status(cfg, workdir, resume=args.resume)
        if not ok:
            if args.ignore_review:
                _out(f"{WARN} {msg}（已按 --ignore-review 跳过）")
            else:
                _out(f"{FAIL} {msg}")
                return 2

    out = Path(args.out or "input")
    if not out.is_absolute():
        out = Path(cfg.root) / out
    if use_tmp:
        final_out = out / novel_stem
        out = out / (".work_" + novel_stem)  # 编译先写临时目录
    else:
        out = out / novel_stem
        final_out = None
    if args.clean:
        if not clean_workdir(workdir, out, args.yes, _out):
            return 2
    plan_path = out / "external" / "plan.json"
    if not args.review_outline and plan_path.exists() and not args.force:
        _out(f"{FAIL} {plan_path} 已存在。加 --force 覆盖（会替换现有外部输入）")
        return 2

    llm = LLMClient(base_url=args.llm_url)
    health = llm.health()
    if not health.get("ok"):
        detail = _health_detail(health)
        status = health.get("status")
        _out(f"{WARN} 文本模型健康检查未通过：{llm.base_url}")
        _out(f"  {detail}")
        if status in (401, 403):
            _out(f"{FAIL} API Key 无效或没有权限。请检查环境变量 "
                 "DEEPSEEK_API_KEY（或 MANJU_LLM_API_KEY）；旧配置里的 "
                 "llm.api_key 仅作兼容。")
            return 2
        if status == 402:
            _out(f"{FAIL} 账户余额不足。请充值后重试，"
                 "或更换可用的 API Key / 端点。")
            return 2
        # 超时/代理/限流/端点不支持 /models 等：不提前判死刑，
        # 让实际生成调用给出更准确的错误（那里有重试与完整异常）。
        _out(f"{WARN} 可能是网络/代理抖动、限流，或该端点不实现 /models；"
             "继续尝试实际生成。")
        _out("  若随后仍失败，日志会输出首次模型调用的真实 HTTP 错误。")

    pipe = StoryPipeline(llm=llm, workdir=str(workdir),
                         progress=lambda m: _out(m), resume=args.resume,
                         strict=args.strict)

    if plot_mode:
        _out("\n① 情节分集设计（通读全文 → 按戏剧单元分集）")
        # 设计阶段也纳入 resume 指纹：换源/换模型/换时长/换原子切法时
        # 都不能静默复用旧分集（否则集边界会套到另一套原子上）。
        atom_sig = (f"atom={cfg.get('llm.story_plot_atom_chars', 2400)}:"
                    f"excerpt={cfg.get('llm.story_plot_atom_excerpt', 900)}:"
                    f"max={cfg.get('llm.story_plot_max_atoms', 180)}:"
                    f"total={cfg.get('llm.story_plot_total_chars', 100000)}:"
                    f"prefix={cfg.get('llm.story_plot_prefix_chars_per_episode', 5000)}:"
                    f"eps={args.to_episode or 0}:rules={STORY_RULES_REV}")
        pipe.set_fingerprint(0, args.duration, args.ratio,
                             source=text, split=f"design:{atom_sig}")
        try:
            design, slices = pipe.design_series(
                text, args.duration, max_episodes=args.to_episode)
        except StoryGateError as e:
            latest = write_failure_packet(cfg, pipe.workdir, e.stage,
                                          e.report, pipe.attempts,
                                          payload=getattr(e, "payload", None))
            _out(f"\n{FAIL} {e.stage} 未通过质量门。失败包：{latest}")
            _out(e.report.text())
            return 1
        except Exception as e:  # noqa: BLE001
            _out(f"{FAIL} 情节分集失败：{type(e).__name__}: {e}")
            return 1
        if (needs_auto_confirmation(len(slices), max_auto) and not args.yes
                and not args.to_episode):
            _out(f"{WARN} 情节分集得到 {len(slices)} 集，超过 {max_auto} 集上限。")
            if not sys.stdin.isatty():
                _out(f"{FAIL} 非交互环境请加 --yes 确认，或用 --to-episode N "
                     "只生成前 N 集。")
                return 2
            answer = input(f"  继续按 {len(slices)} 集生成？[y/N] ").strip().lower()
            if answer not in ("y", "yes"):
                _out("已取消。")
                return 2
        split_sig = f"plot:{design_signature(design)}"
    else:
        split_sig = f"chapter:{chapters_per_ep}" if use_chapter else ""

    if args.from_episode or args.to_episode:
        lo = int(args.from_episode or 1)
        hi = int(args.to_episode or len(slices))
        if hi > len(slices):
            # 情节分集按内容决定集数：要求的 to 超过实际设计集数时收敛，
            # 不能因为“少设计了一集”把整次运行判为非法。
            _out(f"{WARN} 情节设计只得到 {len(slices)} 集，"
                 f"--to-episode {hi} 收敛为 {len(slices)}")
            hi = len(slices)
        if not 1 <= lo <= hi <= len(slices):
            _out(f"{FAIL} 集数范围非法：{lo}–{hi}（共 {len(slices)} 集）")
            return 2
        ep_range = (lo, hi)

    write_slices(slices, pipe.workdir)
    if novel.cleanup is not None and novel.cleanup.changed:
        (pipe.workdir / "cleaned.txt").write_text(novel.text, encoding="utf-8")
        (pipe.workdir / "cleanup_report.json").write_text(
            json.dumps(novel.cleanup.to_dict(), ensure_ascii=False, indent=2),
            encoding="utf-8")
        _out(f"  [CLEAN] 清理报告：{pipe.workdir / 'cleanup_report.json'}")
    if args.review_outline:
        until = "outline"
    elif args.review and not args.resume:
        until = "art"
    else:
        until = "all"

    while True:
        try:
            outline, cast, art, scripts, staging, board = pipe.run(
                text, len(slices), args.duration, args.ratio, until=until,
                slices=slices, episode_range=ep_range, split_sig=split_sig)
            break
        except StoryGateError as e:
            latest = write_failure_packet(cfg, pipe.workdir, e.stage,
                                          e.report, pipe.attempts,
                                          payload=getattr(e, "payload", None))
            _out(f"\n{FAIL} {e.stage} 未通过质量门。失败包：{latest}")
            _out(e.report.text())
            if args.on_gate_fail != "ask" or not sys.stdin.isatty():
                _out("  修改中间产物后加 --resume 继续。")
                return 1
            answer = input("  修复后输入 r 重试，输入 q 退出：").strip().lower()
            if answer not in ("r", "retry", "重试"):
                return 1
            pipe.resume = True
            continue
        except Exception as e:  # noqa: BLE001 - 需要看到模型/网络错误原文
            _out(f"{FAIL} 生成失败：{type(e).__name__}: {e}")
            return 1

    if args.review_outline:
        review = request_outline_review(cfg, outline, pipe.workdir)
        _out(f"\n{OK} 大纲已生成，等待人工审阅：")
        _out(f"  审阅页：{review.index}")
        _out(f"  文本版：{review.markdown}")
        _out(f"  修改 {pipe.workdir}/outline.json 后："
             "加 --resume --approve outline 继续。")
        return 0

    if board is None:
        _out(f"\n{OK} 大纲 / 角色 / 美术已落盘：{pipe.workdir}")
        _out("  先审角色定妆提示词与场景提示词；确认后加 --resume 继续。")
        return 0

    # 单镜时长/人脸数/台词句数/分段上限都是引擎能力，从 config 读取；
    # 缺省不限制人脸与句数，只有显式配置才收紧。
    sp = params_from_cfg(get_config())
    reports = validate_all(outline, cast, art, scripts, staging, board,
                           strict=args.strict, params=sp)
    bad = [r for r in reports if not r.ok]
    for r in bad:
        _out(r.text())
    if bad:
        _out(f"{FAIL} 故事层仍有 {len(bad)} 个门未通过，未编译")
        return 1

    files = compile_all(outline, cast, art, scripts, staging, board, out,
                        params=sp)

    # 下游确定性审计：gate 跨文档规则 + drama 结构规则
    from .drama import audit_files
    from .gate import check_files

    all_paths = [files["plan"]]
    for ep in scripts:
        all_paths += [files[f"script_ep{ep.ep:02d}"],
                      files[f"storyboard_ep{ep.ep:02d}"]]
    # 故事层已按角色设定判定"哪些角色需要锁脸"（狗/稻草人/面具不占正脸名额），
    # 编译后审计必须用同一口径，否则分镜门过了、gate 又会因宠物同框而 block。
    no_face = {c.id for c in cast.characters if not face_lock_required(c)}
    gate_rep = check_files(all_paths, tolerance=0.2, no_face_chars=no_face,
                           speech_cps=sp.chars_per_second,
                           max_faces_per_shot=sp.max_faces_per_shot)
    gate_blocks = len(gate_rep.blocks)

    drama_bad = 0
    for ep in scripts:
        dr = audit_files(
            [files["plan"], files[f"script_ep{ep.ep:02d}"],
             files[f"storyboard_ep{ep.ep:02d}"]],
            files[f"director_ep{ep.ep:02d}"],
            target_sec=ep.target_seconds, max_shot_sec=sp.max_shot_seconds,
            max_line_chars=sp.max_line_chars,
            strict=args.strict,
        )
        if not dr.ok():
            drama_bad += 1
            _out(dr.to_text())

    if gate_blocks:
        _out(gate_rep.to_text())
    if gate_blocks or drama_bad:
        _out(f"\n{FAIL} 编译产物未通过下游严格门"
             f"（gate block={gate_blocks}，drama 失败集={drama_bad}）。"
             "产物已写出，但先别拿去渲染。")
        return 1

    printed_dir = out
    printed_files: dict[str, Path] = files
    if use_tmp and final_workdir is not None and final_out is not None:
        from shutil import copytree, rmtree
        for dst, src in ((final_workdir, workdir), (final_out, out)):
            if src.is_dir():
                if dst.exists():
                    rmtree(dst)
                dst.parent.mkdir(parents=True, exist_ok=True)
                copytree(src, dst)
        (final_workdir / "_completed.json").write_text(
            '{"status":"completed"}', encoding="utf-8")
        # 临时目录即将删除，下面打印/返回的路径必须映射到最终成品目录，
        # 否则用户看到的是不存在的 .work_* 路径。
        printed_dir = final_out
        printed_files = {}
        for name, p in files.items():
            try:
                printed_files[name] = final_out / Path(p).relative_to(out)
            except ValueError:
                printed_files[name] = p
        rmtree(workdir, ignore_errors=True)   # 临时清空，最终成品已固化
        rmtree(out, ignore_errors=True)
        _out(f"\n{OK} 已固化到最终目录（--clean 不再触碰）：\n"
             f"    中间 {final_workdir}\n    编译 {final_out}")

    _out(f"\n{OK} 已编译到 {printed_dir}（gate + drama 严格门通过）")
    for name, path in printed_files.items():
        _out(f"  {name:18s} {path}")

    if args.report:
        import re as _re
        from .report import build_report
        gates: dict[int, tuple[list[str], list[str]]] = {s.ep: ([], []) for s in scripts}
        for r in reports:
            m = _re.search(r"ep(\d+)$", getattr(r, "layer", ""))
            if m and int(m.group(1)) in gates:
                errs, warns = gates[int(m.group(1))]
                gates[int(m.group(1))] = (errs + r.errors, warns + r.warnings)
        report_dir = Path(cfg.p("state")) / "report"
        try:
            rep = build_report(outline, cast, art, scripts, board,
                               outdir=report_dir, gates=gates)
            _out(f"\n{OK} 报告：{rep.index}")
            for note in rep.notes:
                _out(f"  {WARN} {note}")
        except Exception as e:  # noqa: BLE001 - 报告失败不影响编译产物
            _out(f"{WARN} 报告生成失败：{type(e).__name__}: {e}")
    if getattr(llm, "stats", None) is not None:
        _out(f"  LLM: {llm.stats.summary()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
