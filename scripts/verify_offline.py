# -*- coding: utf-8 -*-
"""离线验收入口：编译 + 测试 + CLI 冒烟 +（有样书时）情节原子 dry-run。不调用 API。"""
from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _python(root: Path) -> str:
    for rel in (Path(".venv") / "Scripts" / "python.exe",
                Path(".venv") / "bin" / "python"):
        p = root / rel
        if p.is_file():
            return str(p)
    return sys.executable


def main(argv: list[str] | None = None) -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:  # noqa: BLE001
        pass
    py = _python(ROOT)
    env = dict(os.environ)
    env["MANJU_ROOT"] = str(ROOT)
    env["PYTHONUNBUFFERED"] = "1"
    steps = [
        ("语法编译", [py, "-m", "compileall", "-q", "manju", "scripts"]),
        ("离线测试", [py, "-m", "pytest", "tests", "-q"]),
        ("版本入口", [py, "-m", "manju.story_cli", "--version"]),
        ("路径诊断", [py, "-m", "manju.story_cli", "--doctor"]),
    ]
    samples = sorted((ROOT / "input").glob("*.txt"))
    skipped: dict[str, str] = {}
    if samples:
        sample = samples[0]
        steps.append(
            ("情节 dry-run", [py, "-m", "manju.story_cli", str(sample),
                              "--dry-run", "--to-episode", "2", "--yes"]))
    else:
        skipped["情节 dry-run"] = (
            "纯净包未附带样书；把小说放进 input/ 后单独运行")
    print(f"项目根：{ROOT}", flush=True)
    print(f"解释器：{py}", flush=True)
    print("模式：离线（不调用任何模型 API）\n", flush=True)
    failed: list[str] = []
    for label, cmd in steps:
        print("=" * 68, flush=True)
        print(f"[{label}] {' '.join(cmd)}", flush=True)
        started = time.monotonic()
        try:
            code = subprocess.run(cmd, cwd=str(ROOT), env=env).returncode
        except OSError as e:
            print(f"[FAIL] 无法执行：{e}")
            failed.append(label)
            continue
        elapsed = time.monotonic() - started
        if code == 0:
            print(f"[OK] {label}  {elapsed:.1f}s")
        else:
            print(f"[FAIL] {label}  exit={code}  {elapsed:.1f}s")
            failed.append(label)
    for label, why in skipped.items():
        print(f"[SKIP] {label}  {why}")
    print("\n" + "=" * 68)
    if failed:
        print(f"未通过：{'、'.join(failed)}")
        return 1
    if skipped:
        print("离线验收通过（跳过：" + "、".join(skipped) + "）。")
    else:
        print("全部离线验收通过。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
