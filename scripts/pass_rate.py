# -*- coding: utf-8 -*-
"""端到端通过率测量。

默认**离线**：只读最近一次报告，绝不调用 API。
只有显式加 `--live` 才会真实调用文本模型；每次运行使用独立的
`state/pass_rate/run_*/` 工作目录，不覆盖 `input/<小说>/` 的成品。

用法：
    # 离线：看最近一次真实通过率
    python scripts/pass_rate.py

    # 离线：审计已有产物
    python scripts/pass_rate.py --novel my_story --audit-existing

    # 真实调用（会产生 API 费用；由你显式授权）
    python scripts/pass_rate.py --live --runs 3 --input input/my_story.txt --to-episode 2
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

from audit_run import audit, render  # noqa: E402

DEFAULT_ROOT = Path(__file__).resolve().parents[1]
ROOT = Path(os.environ.get("MANJU_ROOT") or DEFAULT_ROOT).resolve()
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
TARGET = 0.90
_LLM_STATS_RE = re.compile(
    r"LLM:\s*(\d+)\s*次调用\s*/\s*修复\s*(\d+)\s*次\s*/\s*"
    r"in\s*(\d+)\s*tok\s*/\s*out\s*(\d+)\s*tok\s*/\s*(\d+)s"
)


def _parse_llm_stats(log: str) -> dict[str, int]:
    """从 CLI 结束时的 `LLM: ...` 行提取调用/修复/token，供 live 基线汇总。"""
    match = _LLM_STATS_RE.search(log or "")
    if not match:
        return {}
    calls, repairs, prompt, completion, secs = (int(x) for x in match.groups())
    return {
        "calls": calls,
        "repairs": repairs,
        "prompt_tokens": prompt,
        "completion_tokens": completion,
        "llm_seconds": secs,
    }


def _python(root: Path) -> str:
    venv = root / ".venv" / "Scripts" / "python.exe"
    if venv.is_file():
        return str(venv)
    venv = root / ".venv" / "bin" / "python"
    if venv.is_file():
        return str(venv)
    return sys.executable


def _pick_input(root: Path, given: str | None) -> Path | None:
    if given:
        p = Path(given)
        return p if p.is_absolute() else root / p
    files = sorted((root / "input").glob("*.txt"))
    return files[0] if files else None


def _reports(root: Path) -> list[Path]:
    d = root / "state" / "pass_rate"
    return sorted(d.glob("report_*.json")) if d.is_dir() else []


def _print_offline(root: Path, novel: str | None, audit_existing: bool) -> int:
    print("离线模式：不会调用任何 API。")
    reports = _reports(root)
    if reports:
        latest = reports[-1]
        try:
            data = json.loads(latest.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            data = {}
        rate = data.get("pass_rate")
        print(f"最近报告：{latest}")
        if rate is not None:
            print(f"  通过率：{data.get('passed')}/{data.get('runs')} = {rate:.1%}"
                  f"（目标 ≥{TARGET:.0%}）")
        for row in data.get("results") or []:
            ok = "PASS" if row.get("passed") else "FAIL"
            print(f"  run {row.get('run')}: {ok}  exit={row.get('exit_code')}  "
                  f"audit={row.get('audit_verdict')}  {row.get('elapsed_sec', 0):.0f}s")
    else:
        print("还没有真实通过率报告。")

    if audit_existing:
        if not novel:
            print("--audit-existing 需要同时给 --novel <小说名>")
            return 2
        result = audit(root=root, novel=novel)
        print()
        print(render(result))
        return 0 if result["verdict"] in {"PASS", "SKIP"} else 1

    print("\n要测真实通过率（会产生 API 费用）：")
    print(f"  python scripts/{Path(__file__).name} --live --runs 3 "
          f"--input input/my_story.txt --to-episode 2")
    print("  每次运行写独立的 state/pass_rate/run_*/，不覆盖 input/<小说>/ 成品。")
    return 0


def _has_api_key(root: Path) -> bool:
    if str(root) not in sys.path:
        sys.path.insert(0, str(root))
    try:
        from manju.config import Config, api_key_value
        cfg = Config.load()
    except Exception:  # noqa: BLE001 - 配置坏了也要给出可读结论
        return False
    return bool(api_key_value(cfg).strip())


def _run_once(root: Path, py: str, input_path: Path, run_dir: Path,
              lo: int, hi: int, duration: float, ratio: str,
              timeout_sec: float) -> dict[str, Any]:
    workdir = run_dir / "work"
    outdir = run_dir / "out"
    workdir.mkdir(parents=True, exist_ok=True)
    outdir.mkdir(parents=True, exist_ok=True)
    log_path = run_dir / "run.log"
    cmd = [
        py, "-m", "manju.story_cli", str(input_path),
        "--yes", "--force", "--no-report",
        "--workdir", str(workdir), "--out", str(outdir),
        "--from-episode", str(lo), "--to-episode", str(hi),
        "--duration", str(duration), "--ratio", ratio,
    ]
    env = dict(os.environ)
    env["MANJU_ROOT"] = str(root)
    env["PYTHONUNBUFFERED"] = "1"

    started = time.monotonic()
    timed_out = False
    try:
        proc = subprocess.run(cmd, cwd=str(root), env=env, text=True,
                              encoding="utf-8", errors="replace",
                              capture_output=True, timeout=timeout_sec)
        code = proc.returncode
        log = (proc.stdout or "") + (proc.stderr or "")
    except subprocess.TimeoutExpired as e:
        timed_out = True
        code = -9
        log = ((e.stdout or "") if isinstance(e.stdout, str) else "") + \
              ((e.stderr or "") if isinstance(e.stderr, str) else "")
        log += f"\n[TIMEOUT] > {timeout_sec:.0f}s\n"
    elapsed = time.monotonic() - started
    log_path.write_text(log, encoding="utf-8", errors="replace")

    result = audit(root=root, directory=str(workdir))
    passed = (code == 0 and result["verdict"] == "PASS")
    tail = "\n".join(log.strip().splitlines()[-25:])
    return {
        "passed": passed,
        "exit_code": code,
        "timed_out": timed_out,
        "elapsed_sec": elapsed,
        "audit_verdict": result["verdict"],
        "issues": result["issues"][:20],
        "warnings": result["warnings"][:10],
        "episodes_audited": len(result.get("episodes") or []),
        "llm": _parse_llm_stats(log),
        "workdir": str(workdir),
        "outdir": str(outdir),
        "log": str(log_path),
        "log_tail": tail,
    }


def _print_run(row: dict[str, Any]) -> None:
    ok = "PASS" if row["passed"] else "FAIL"
    print(f"\n[run {row['run']}] {ok}  exit={row['exit_code']}  "
          f"audit={row['audit_verdict']}  {row['elapsed_sec']:.0f}s  "
          f"episodes={row.get('episodes_audited', 0)}")
    if row.get("timed_out"):
        print("  [WARN] 运行超时")
    llm = row.get("llm") or {}
    if llm:
        print(f"  LLM: {llm['calls']} 次调用 / 修复 {llm['repairs']} 次 / "
              f"in {llm['prompt_tokens']} tok / out {llm['completion_tokens']} tok")
    for issue in row.get("issues") or []:
        print(f"  - {issue}")
    if not row["passed"] and row.get("log_tail"):
        print("  --- 日志尾部 ---")
        print("\n".join("  " + x for x in row["log_tail"].splitlines()))


def main(argv: list[str] | None = None) -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:  # noqa: BLE001
        pass
    p = argparse.ArgumentParser(
        prog="python scripts/pass_rate.py",
        description="端到端通过率（默认离线；--live 才调用真实 API）")
    p.add_argument("--root", default=None, help="项目根目录；默认 MANJU_ROOT 或脚本上一级")
    p.add_argument("--input", default=None, help="小说 txt；默认 input/ 下第一本 .txt")
    p.add_argument("--novel", default=None, help="离线审计已有产物时的小说名")
    p.add_argument("--audit-existing", action="store_true",
                   help="离线审计 state/story/<小说> 或 input/<小说>")
    p.add_argument("--live", action="store_true",
                   help="显式允许真实调用文本模型（会产生费用）")
    p.add_argument("--runs", type=int, default=3, help="运行次数（1-20，默认 3）")
    p.add_argument("--from-episode", type=int, default=1)
    p.add_argument("--to-episode", type=int, default=2)
    p.add_argument("--duration", type=float, default=90)
    p.add_argument("--ratio", choices=["9:16", "16:9"], default="9:16")
    p.add_argument("--timeout", type=float, default=7200,
                   help="单次运行超时秒数（默认 7200）")
    p.add_argument("--json", action="store_true", help="把最终报告以 JSON 打到 stdout")
    a = p.parse_args(argv)

    root = Path(a.root).resolve() if a.root else ROOT
    os.environ["MANJU_ROOT"] = str(root)
    if str(root) not in sys.path:
        sys.path.insert(0, str(root))
    if not 1 <= a.runs <= 20:
        print("[FAIL] --runs 必须在 1-20 之间")
        return 2
    if not 1 <= a.from_episode <= a.to_episode:
        print("[FAIL] 集数范围非法")
        return 2

    if not a.live:
        return _print_offline(root, a.novel, a.audit_existing)

    input_path = _pick_input(root, a.input)
    if input_path is None or not input_path.is_file():
        print(f"[FAIL] 找不到小说文件：{input_path or '<input/*.txt>'}")
        return 2
    if not _has_api_key(root):
        print("[FAIL] 未找到 API Key；真实运行会失败。"
              "请设置环境变量 DEEPSEEK_API_KEY（或 MANJU_LLM_API_KEY），"
              "再运行 python -m manju.story_cli --doctor 检查。")
        return 2

    minutes = a.runs * (a.to_episode - a.from_episode + 1) * 15
    print("=" * 64)
    print("LIVE 模式：真实调用文本模型，会产生 API 费用。")
    print(f"  输入：{input_path}")
    print(f"  轮次：{a.runs}  集数：{a.from_episode}-{a.to_episode}  "
          f"时长：{a.duration:g}s  画幅：{a.ratio}")
    print(f"  粗略预计：≥{minutes} 分钟（按 15 分钟/集估；实际以日志为准）")
    print("  每轮独立写到 state/pass_rate/run_*/，不覆盖 input/<小说>/ 成品。")
    print("=" * 64)

    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    base = root / "state" / "pass_rate"
    base.mkdir(parents=True, exist_ok=True)
    py = _python(root)
    results: list[dict[str, Any]] = []
    for i in range(1, a.runs + 1):
        run_dir = base / f"run_{stamp}_{i:02d}"
        print(f"\n>>> run {i}/{a.runs}: {run_dir}")
        row = _run_once(root, py, input_path.resolve(), run_dir,
                        a.from_episode, a.to_episode, a.duration, a.ratio,
                        a.timeout)
        row["run"] = i
        results.append(row)
        _print_run(row)

    passed = sum(1 for r in results if r["passed"])
    rate = passed / len(results)
    totals = {k: sum((r.get("llm") or {}).get(k, 0) for r in results)
              for k in ("calls", "repairs", "prompt_tokens",
                        "completion_tokens", "llm_seconds")}
    report = {
        "timestamp": stamp,
        "input": str(input_path),
        "from_episode": a.from_episode,
        "to_episode": a.to_episode,
        "runs": len(results),
        "passed": passed,
        "pass_rate": rate,
        "target": TARGET,
        "met": rate >= TARGET,
        "totals": totals,
        "results": results,
    }
    report_path = base / f"report_{stamp}.json"
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2),
                           encoding="utf-8")

    print("\n" + "=" * 64)
    print(f"通过率：{passed}/{len(results)} = {rate:.1%}  "
          f"（目标 ≥{TARGET:.0%}：{'达标' if report['met'] else '未达标'}）")
    print(f"LLM 汇总：{totals['calls']} 次调用 / 修复 {totals['repairs']} 次 / "
          f"in {totals['prompt_tokens']} tok / out {totals['completion_tokens']} tok")
    print(f"报告：{report_path}")
    print("=" * 64)
    if a.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["met"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
