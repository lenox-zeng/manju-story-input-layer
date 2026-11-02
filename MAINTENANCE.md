# 维护边界

> 纯净包说明：本包只含 v2.0 输入层；v1.2 目录、样书、旧产物与 v1.2 指纹清单不在包内。

## 活跃版本：v2.0

- 项目区：`manju-story-input-clean_v2.0/`
- 配置、`state/`、`input/`、`output/`、`logs/`、虚拟环境都在本目录内。
- Studio 默认端口：**8092**。
- 契约版本：**v10.3**（2026-11-03；旁白时间契约、camera_recipe 配方槽位、ref_characters 画面内口径）。
- 新的连贯性迭代、质量工具、性能优化都在这里做。

## 冻结版本：v1.2

- 项目区：`manju-story-input-clean_v1.2/`
- **只读基线**。只有明确处理 v1.2 自身缺陷时才修改；修复单独验证，不回灌 v2.0。
- v2.0 的新功能也不回灌 v1.2，避免两条线再次混在一起。

## 硬隔离

1. 不共享 `config.json`、`state/`、`input/`、`output/`、`logs/`、`.venv/`。
2. 启动 CLI/Studio 时把 `MANJU_ROOT` 指向本项目；`scripts/run.ps1` 与
   `scripts/studio.ps1` 已处理。直接 `python -m ...` 时，先 `cd` 到本项目，
   或显式设置 `MANJU_ROOT`。
3. 两个目录各自一个 git 仓库；不交叉 `git add` / `git commit`。
4. API Key 只放环境变量（`DEEPSEEK_API_KEY` / `MANJU_LLM_API_KEY` / `OPENAI_API_KEY`），
   **不写进 `config.json`、不提交、不打印、不贴到聊天或日志**。Studio 页面输入的 Key
   仅在进程内存中保留。`config.json` 仍整体忽略；示例用 `config.example.json`。
5. v1.2 源文件指纹清单只在原工作区维护；纯净包不包含 v1.2 清单、样书或旧产物。

## 常用命令

```powershell
# 只读诊断（不读小说、不调模型）
.\scripts\run.ps1 --doctor

# 离线验收（编译 + 测试 + CLI 冒烟 + 情节 dry-run）
python scripts\verify_offline.py

# 审计一次产物
python scripts\audit_run.py --novel my_story

# 离线看通过率 / 审计已有成品
python scripts\pass_rate.py
python scripts\pass_rate.py --novel my_story --audit-existing

# 真实通过率（显式 --live 才调用 API）
python scripts\pass_rate.py --live --runs 3 --input input\my_story.txt --to-episode 2
```
