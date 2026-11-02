"""外部 JSON 的宽容读取。

**为什么需要这一层**：`llm_mode=external` 的输入是**豆包贴出来、人手动另存**的，
而豆包几乎必然会用 markdown 代码围栏包住 JSON（```` ```json … ``` ````）。
让用户每次都手动剥围栏，是"三份文档 × 每次返工"的重复劳动，漏一次就是一次
`io.unreadable` —— 而围栏不是**内容错误**，是**粘贴格式**，没有理由让流水线因此停下。

**宽容的口径故意开得很窄**：只有当整份文本**恰好就是一个**围栏块时才剥。
"先说了几句客套话、再给 JSON" **不匹配** —— 那种情况说明豆包确实没听话，
应该报错让人看见，而不是被这里悄悄兜住。

本模块只做**文本 → 对象**这一件事，不读配置、不打日志，纯函数。
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

# 整份文本就是一个围栏块：开头 ```（可带语言标注）→ 内容 → 结尾 ```
# 用 \A…\Z 锚死，所以"围栏前后还有别的字"一律不匹配（见模块说明的窄口径）。
_FENCE = re.compile(r"\A\s*```[A-Za-z0-9_+.-]*[ \t]*\r?\n(?P<body>.*?)\r?\n?[ \t]*```\s*\Z",
                    re.DOTALL)


def unwrap_fence(text: str) -> str:
    """剥掉"整份文本恰好是一个 markdown 围栏块"的外壳；否则原样返回。

    公开出来是给测试和各读取点共用 —— "什么算围栏"这件事只能有一个实现，
    否则各处判定迟早漂移（同 `gate.classify_path` 的理由）。
    """
    m = _FENCE.match(text)
    # 正则用的是惰性 `.*?` + `\\Z`，遇到**两个**围栏块时会从第一个 ``` 一路吃到
    # 最后一个 ```，把两块当成一块。那种情况不是"一个围栏块"，必须原样返回
    # 让它去报错 —— 豆包给了两块 JSON，人得知道，不能只拿到其中一块。
    if m is None or "```" in m.group("body"):
        return text
    return m.group("body")


def loads(text: str) -> Any:
    """`json.loads` + 围栏容忍。BOM 请由调用方先处理（或用 `read`）。"""
    return json.loads(unwrap_fence(text))


def read(path: str | Path) -> Any:
    """读文件并解析。编码固定 utf-8-sig —— 豆包/记事本导出的文件常带 BOM，
    按 utf-8 读会炸在第一个字符上（这个坑踩过一次，别再踩）。
    """
    return loads(Path(path).read_text(encoding="utf-8-sig"))
