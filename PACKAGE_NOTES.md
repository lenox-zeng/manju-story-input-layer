# 纯净包说明

- 包名：`manju-story-input-v2.0.0-clean`
- 版本：v2.0.0
- 契约：v10.3
- 日期：2026-11-03
- 范围：小说 → 剧本 → 分镜 → v10.3 渲染契约的**输入层**代码、测试、脚本与文档。

## 包含

- `manju/`：输入层全部源码（故事层、编译、gate/drama、CLI、Studio、LLM 客户端）。
- `tests/`：18 个测试模块 + `conftest.py`。
- `scripts/`：离线验收、产物审计、通过率、PowerShell 启动。
- `docs/`：契约 v10.3、产物 JSON 字段参考手册、ComfyUI 对接指南、CLI/Studio 教程、图文介绍、七维度自检报告、改动清单、live 基线与自检报告。
- `docs/assets/story-input/`：架构/流水线/质量门等示意图。
- `config.example.json`：可移植配置模板（不含任何 Key）。
- `README.md` / `README.en.md` / `CHANGELOG.md` / `MAINTENANCE.md` / `VERSION` / `requirements.txt` / `.gitignore`。
- 空的 `input/`、`state/`、`logs/`、`output/` 目录（仅 `.gitkeep` 占位）。

## 明确剔除

| 内容 | 原因 |
|---|---|
| `config.json` | 含真实 API Key，**绝不随包交付** |
| `input/*.txt`、`input/3S绷不住了/` | 小说正文与生成结果 |
| `state/` 全部内容 | 中间产物、LLM trace、报告、备份 |
| `output/`、`logs/` 内容 | 生成结果与运行日志 |
| `.git/`、`__pycache__/`、`.pytest_cache/`、`.venv/` | 版本库与本地缓存 |
| `docs/v1.2_source_manifest.sha256` | 仅属于原工作区的 v1.2 只读基线 |
| `docs/v2.0迁移说明.md` | 原工作区从 v1.2 迁移的历史说明，不属于可移植输入层 |
| `docs/decision-log.md` | 已明确标注为历史存档，主体是下游 ComfyUI/显存决策，不属于当前输入层 |

> 文档中的样书命令已替换为 `input/my_story.txt` 或 `input/<小说>.txt`。
> `CHANGELOG.md` 保留历史条目中的样书名/路径，仅用于记录当时的修复，不包含正文。

## 首次使用

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
Copy-Item config.example.json config.json
$env:DEEPSEEK_API_KEY = "<你的 Key>"
.\.venv\Scripts\python.exe -m manju.story_cli --doctor
# 把自己的小说放到 input/my_story.txt
.\.venv\Scripts\python.exe -m manju.story_cli input\my_story.txt --to-episode 2 --yes
```

Linux/macOS 把 `.venv\Scripts\python.exe` 换成 `.venv/bin/python`。

## 自检 1：静态一致性与敏感内容扫描

<!-- CHECK1_START -->
- 禁止项：`config.json` / `.env` / `.git` / 缓存 / `state`、`logs`、`output` 内容 / 小说 `.txt`：**0**。
- 密钥模式扫描（OpenAI 风格 Key、Bearer、私钥块、长 `api_key` 赋值、已知泄漏后缀）：**0 处真实 Key**；测试占位符 `sk-test-*` 已排除。
- 个人绝对路径 / 本机用户名：**0 处**。
- 面向用户的文档与脚本中指向包外样书的陈旧引用：**0 处**。
- 本地链接/图片 **55** 个：0 失效；页内锚点：0 失效。
- 全部 Python 文件 `ast.parse`、全部 JSON 解析：**通过**。
- 版本一致性：`VERSION` = 2.0.0，契约 = v10.2，测试数 = 243。
- 结果：**PASS**。
<!-- CHECK1_END -->

## 自检 2：干净目录回归与压缩包完整性

<!-- CHECK2_START -->
- 解压后 `MANIFEST.sha256` 校验：**79 个文件全部一致**。
- 干净目录 `pytest tests -q -p no:cacheprovider`：**243 passed, 6 skipped**。
- `scripts/verify_offline.py`：exit=0；语法编译、测试、`--version`、`--doctor` 通过；无样书时情节 dry-run 明确 `SKIP`。
- CLI 冒烟：`--version`=0（manju-story-input 2.0.0），无参数=2（预期 2），`gate --help`=0，`drama --help`=0，核心模块 import=0。
- 压缩包 `testzip`：无损坏；条目均位于 `manju-story-input-v2.0.0-clean/` 下；无 Key、小说、中间产物、生成结果或缓存。
- 结果：**PASS**。
<!-- CHECK2_END -->

## 交付物校验

压缩包 SHA256 见包外同名 `.sha256` 文件；包内 `MANIFEST.sha256` 覆盖除自身外的全部文件。
