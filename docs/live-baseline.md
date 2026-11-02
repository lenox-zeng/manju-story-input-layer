# Live 端到端通过率基线

> **当前状态：未执行。** 本文只定义测量口径与执行步骤，不包含任何推测的通过率、
> token 数或耗时。没有真实运行报告，就不宣称基线已达成。
>
> **纯净包说明**：包内不附样书；下文 `input\my_story.txt` 请替换为你自己的小说路径。

## 为什么还没跑

- 旧 API Key 已按“可能泄露”处理，从 `config.json` 移除；需要先在服务商控制台
  吊销并生成新 Key。
- 真实生成会产生 API 费用，且必须在获得明确授权后才能执行。
- 本机当前环境未设置 `DEEPSEEK_API_KEY` / `MANJU_LLM_API_KEY`；
  `--doctor` 显示 `api_key: missing`，`pass_rate.py --live` 会在调用前直接退出。

## 安全前置

1. 在服务商控制台吊销旧 Key，生成新 Key。
2. 只通过环境变量提供，不写进 `config.json`，不贴到聊天/日志：

   ```powershell
   $env:DEEPSEEK_API_KEY = "<新 Key>"
   ```

3. 确认来源（不会打印 Key 内容）：

   ```powershell
   .\scripts\run.ps1 --doctor
   ```

   期望看到 `api_key: env:DEEPSEEK_API_KEY`，不再是 `missing`。

## 首轮小样

先跑 1 轮、前 2 集，确认链路和费用量级：

```powershell
python scripts\pass_rate.py --live --runs 1 --input input\my_story.txt --to-episode 2
```

`pass_rate.py` 本身没有 `--clean` 参数，也不需要在运行前手工清理：每轮都会新建
独立的 `state/pass_rate/run_<时间戳>_NN/`（内含 `work/`、`out/`、`run.log`），
不会覆盖 `input/<小说名>/` 的成品。

## 正式基线

小样链路正常后，再跑 3 轮。`--runs 3` 会自动为每轮创建新的 `run_<时间戳>_NN/`
工作目录（不共享缓存），用于观察稳定性，而不是只挑一次成功结果：

```powershell
python scripts\pass_rate.py --live --runs 3 --input input\my_story.txt --to-episode 2
```

首轮与正式基线之间**不改代码、不改提示词、不改配置**；否则测到的是新版本，
不是同一基线。

## 报告字段

`state/pass_rate/report_<时间戳>.json` 会记录：

| 字段 | 含义 |
|---|---|
| `passed` / `runs` / `pass_rate` | 通过轮次与通过率 |
| `met` | 是否达到脚本内目标 `TARGET=0.90` |
| `results[].exit_code` | `story_cli` 退出码 |
| `results[].audit_verdict` | `audit_run.py` 的 `PASS` / `FAIL` / `SKIP` |
| `results[].issues` / `warnings` | 产物审计发现的问题 |
| `results[].elapsed_sec` | 单轮墙钟耗时 |
| `results[].llm` | CLI 汇总的调用次数、质量门修复次数、输入/输出 token、LLM 秒数 |
| `results[].log` / `workdir` / `outdir` | 原始日志与隔离产物，便于定位失败阶段 |
| `totals` | 所有轮次的调用/修复/token/LLM 秒数合计 |

“通过”的判定是：`story_cli` 退出码为 0，且 `audit_run.py` 判定 `PASS`。
不是“模型返回了内容”就算通过。

## 结果记录模板

执行后把真实数字填进下表；没有执行就保持“未执行”。

| 日期 | 输入 | 集数范围 | 轮次 | 通过 | 通过率 | 调用/修复 | 输入 token | 输出 token | 墙钟耗时 |
|---|---|---|---|---|---|---|---|---|---|
| 未执行 | `input/<小说>.txt` | 1–2 | — | — | — | — | — | — | — |

失败轮次按 `results[].log` 与 `issues` 归因到具体阶段（解析/分集/圣经/剧本/
站位/分镜/编译/审计），不要只写“模型不稳定”。

## 完成标准

1. 至少 3 轮真实运行，报告文件保留在 `state/pass_rate/`。
2. 上表填入真实数字，并注明 Key 来源为环境变量（不记录 Key 本身）。
3. 如果未达到 0.90，不修改 `TARGET`；按失败阶段修复后重新测，并保留新旧两份报告。
4. 只有当报告存在且数字可复现时，才在 README / CHANGELOG 中写“已达基线”。
