# -*- coding: utf-8 -*-
"""契约一致性检查：`manju/schemas.py` 字段 与 `docs/漫剧文本输入契约_v10.3.md`。

为什么需要它
------------
契约文档是**唯一契约来源**（README / 交接边界明确：不交付 schemas.py，
下游只认文档）。因此 schema 字段与文档字段必须双向对齐，否则：
- schema 新增了字段而文档没写 → 下游实现层不知道有它，契约悄悄漂移；
- 文档写了字段而 schema 没有 → 要么是历史遗留，要么是文档超前于实现。

本脚本只做**报告**，不设硬门：文档里反引号包住的标识符包含模型名、
枚举值、文件名等大量非字段词，误报不可避免，因此默认退出 0，
用 `--strict` 才在“schema 有、文档无”时返回 1（适合接入 CI）。

用法
----
    python scripts/check_contract_sync.py            # 报告模式
    python scripts/check_contract_sync.py --strict   # 未覆盖字段时退出 1
"""
from __future__ import annotations

import argparse
import ast
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCHEMA_PATH = ROOT / "manju" / "schemas.py"
DOC_PATH = ROOT / "docs" / "漫剧文本输入契约_v10.3.md"

_FIELD_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")


def schema_fields() -> dict[str, tuple[list[str], bool]]:
    """返回 {字段名: (所在模型类名列表, 是否契约层)}。

    契约层 = 继承 _ALayer / _BLayer / _CLayer（含其子类），即业务契约资产；
    其余 BaseModel 子类为内部交接（qa_report / video / manifest / 入口单等），
    契约文档预期不覆盖它们。
    """
    src = SCHEMA_PATH.read_text(encoding="utf-8")
    tree = ast.parse(src)

    classes: dict[str, ast.ClassDef] = {
        n.name: n for n in ast.walk(tree) if isinstance(n, ast.ClassDef)
    }
    CONTRACT_BASES = {"_ALayer", "_BLayer", "_CLayer"}

    def inherits(name: str, targets: set[str], seen: set[str] | None = None) -> bool:
        """name 是否沿继承链命中 targets 中的任一基类。"""
        seen = seen or set()
        node = classes.get(name)
        if node is None or name in seen:
            return False
        for b in node.bases:
            if not isinstance(b, ast.Name):
                continue
            if b.id in targets:
                return True
            if b.id not in seen and inherits(b.id, targets, seen | {name}):
                return True
        return False

    def is_pydantic_model(name: str, seen: set[str] | None = None) -> bool:
        seen = seen or set()
        node = classes.get(name)
        if node is None or name in seen:
            return False
        for b in node.bases:
            if not isinstance(b, ast.Name):
                continue
            if b.id == "BaseModel":
                return True
            if b.id == "Enum":
                return False
            if b.id not in seen and is_pydantic_model(b.id, seen | {name}):
                return True
        return False

    out: dict[str, tuple[list[str], bool]] = {}
    for name, node in classes.items():
        if not is_pydantic_model(name):
            continue
        contract = inherits(name, CONTRACT_BASES)
        for stmt in node.body:
            if isinstance(stmt, ast.AnnAssign) and isinstance(stmt.target, ast.Name):
                out.setdefault(stmt.target.id, ([], contract))[0].append(name)
    return out


_FILE_LIKE = {"json", "md", "txt", "py", "png", "jpg", "jpeg", "webp", "mp4",
              "wav", "mp3", "log", "csv", "yml", "yaml", "html", "js", "css",
              "jpg", "sqlite", "db"}


def doc_mentions() -> set[str]:
    """收集文档中反引号包住的标识符。

    `a.b` 形式（如 `shot.shot_id`）按首段计入，若末段是字段样词
    （非文件扩展名），也计入——文档常以 `父.字段` 形式写字段。
    """
    text = DOC_PATH.read_text(encoding="utf-8")
    found: set[str] = set()
    for m in re.finditer(r"`([A-Za-z_][A-Za-z0-9_.]*)`", text):
        token = m.group(1)
        parts = token.split(".")
        if _FIELD_RE.fullmatch(parts[0]):
            found.add(parts[0])
        if len(parts) > 1 and _FIELD_RE.fullmatch(parts[-1]) \
                and parts[-1].lower() not in _FILE_LIKE:
            found.add(parts[-1])
    return found


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description="检查 schemas.py 与契约文档的字段一致性（报告模式）")
    ap.add_argument("--strict", action="store_true",
                    help="schema 有而文档无的字段存在时返回退出码 1（CI 用）")
    args = ap.parse_args(argv)

    fields = schema_fields()
    mentions = doc_mentions()
    missing_contract: dict[str, list[str]] = {}
    missing_internal: dict[str, list[str]] = {}
    for f, (cls_list, is_contract) in fields.items():
        if f in mentions or f.startswith("_"):
            continue
        target = missing_contract if is_contract else missing_internal
        for c in cls_list:
            target.setdefault(c, []).append(f)

    print("=== 契约一致性检查 ===")
    print(f"  schema : {SCHEMA_PATH}")
    print(f"  文档   : {DOC_PATH}")
    print(f"  schema 字段总数 : {len(fields)}（契约层 + 内部交接）")
    print(f"  文档已覆盖      : {len(fields) - len(missing_contract) - len(missing_internal)} / {len(fields)}")

    if missing_contract:
        print("\n[契约层字段 · schema 有、文档未提及]（应补文档或改文档引用，按类分组）:")
        for cls in sorted(missing_contract):
            print(f"  {cls}: {', '.join(sorted(missing_contract[cls]))}")
    else:
        print("\n所有契约层字段在文档中均有提及。")

    if missing_internal:
        print("\n[内部交接字段 · 预期不覆盖，无需处理]（qa_report / video / manifest / 入口单等）:")
        for cls in sorted(missing_internal):
            print(f"  {cls}: {', '.join(sorted(missing_internal[cls]))}")

    print("\n[提示] 反向漂移（文档提到、schema 无）不在此检查范围：")
    print("  文档中还有模型名 / 枚举值 / 文件名等非字段词，需人工核对；")
    print("  此检查为报告性质，默认退出 0，--strict 时契约层未覆盖字段返回 1。")
    return 1 if (args.strict and missing_contract) else 0


if __name__ == "__main__":
    sys.exit(main())
