# Changelog

> 纯净包说明：本文件保留完整版本历史；历史条目中的样书路径/小说名用于记录当时的修复，
> 纯净包本身不包含任何小说正文、样书、中间产物或生成结果。

## Unreleased — 渲染对接修复：多人策略 / 场景资产映射 / 台词帧对齐（2026-10-27）

### Fixed

- **同框 ≥2 人不再输出 `multi_subject_strategy: auto`**：内部 `StoryCut` /
  `CutPlan` 新增该字段；编译分镜时按镜头语义写死 `joint`（普通同框）或
  `hybrid`（群像/允许同框多脸）；本地 gate 对生效值为 `auto`/未声明的
  多人镜报 `struct.multi_subject_undeclared`（block）。
- **`story_assets.json` 新增 `scene_refs`**：`script/storyboard.scene_id`
  是每集重新计数的场次 id，`scenes` 的 key 是全剧复用的地点资产 id；
  两者不是同一命名空间。`scene_refs[集号][scene_id]` 给出显式映射，
  修复“S3 场次被错配到资产 S3（另一个地点）”与 S5–S7 缺资产的问题。
- **台词镜头按 8n+1 帧对齐**：`_cut_durations` 的台词下限同时按
  24fps、8n+1 向下取整与 2 帧口型余量计算，避免 2.61s 这类镜头
  “看似够、取整后不够”而稳定触发 `time.dialogue_exceeds_shot`；
  单集总时长仍在 ±0.2s 内。

### Tests

- 新增多人同框策略、gate 对 `auto` 的硬拦、`scene_refs` 映射、
  12 字台词 8n+1 时长四项回归测试（全量 **243 passed / 6 skipped**）。

---

## Unreleased — LLM 日志模型与费用估算（2026-10-27）

### Added

- `state/llm_trace.jsonl` 每条调用记录实际 `model`（配置或 `MANJU_LLM_MODEL`
  解析后的值），排查“到底跑了 Flash 还是 Pro”不再靠推断。
- Studio「LLM 日志」页显示模型、输入/输出/缓存命中 token，并按 DeepSeek 官方价
  （2026-10-27）估算费用区间（空闲–高峰）。旧记录无 `model` 时按当前配置推断并
  标 `*`；未知模型按 Flash 兜底并在页头提示。
- 仅改日志与展示，不改变生成流程、请求参数或模型选择；费用为本地估算，
  含失败重试产生的 token，实际账单以平台为准。

### Tests

- 覆盖 trace 记录模型、Flash/Pro 费用估算、旧记录推断与未知模型兜底
  （全量 **239 passed / 6 skipped**）。

---

## Unreleased — 低对白叙事增强与场间转场（2026-10-27）

### Added

- **结构化旁白**：剧本 `scene.narration` 支持 `exposition`（解释性旁白，speaker 固定为
  `NARRATOR`）与 `inner`（角色内心独白，speaker 为 `C#`）。旁白叠在动作拍上，
  不写口型、不设置 `speaking`，并编译到契约的 `scene.narration` / `shot.narration`。
- **场间转场契约**：`scene.transition_in` 支持 `match_cut` / `sound_bridge` /
  `action_bridge` / `time_jump` / `fade` / `dissolve`；bridge 类必须提供具体
  `anchor`。编译时据此决定场次首镜的 `cut_reason`，避免无动机硬切。
- **低对白自动模式**：按原文引号台词密度与引号字符比例判定“对白稀疏”，
  自动下调台词行数/字数预算，提高 reaction/insert coverage，并提示使用
  `exposition`/`inner` 补足叙事信息。
- 新配置项：`story.narration_mode`、`story.narration_max_exposition`、
  `story.narration_max_inner`、`story.transition_mode`。
- **下游对接指南**：新增 `docs/ComfyUI对接指南.md`，面向渲染实现层说明
  产物读取顺序、A/B/C 层策略、能力映射、字幕/TTS/口型/旁白/转场消费方式
  与最小验收清单。它是接口文档，不包含渲染代码。

### Changed

- 硬门从“禁止一切 VO”调整为“对白只允许画内；必要解释与内心走结构化旁白”。
- 旁白总量受上限约束（每集 exposition ≤2、inner ≤3；对白稀疏集旁白时长 ≤30%，
  普通集 ≤18%），避免把分镜写成有声书。
- `compile_script` / `compile_storyboard`、`gate`、`drama` 均支持旁白与转场字段，
  并校验其时间轴、说话人在场与 anchor 一致性。

### Tests

- 新增低对白预算、旁白叠加、内心独白在场、编译/门禁贯通、配置回退等离线测试。

### Docs

- 全面校对 v2.0 文档：修正 `frame` → `framing`、测试徽章与数量（230 → 236）、
  `transition_mode=auto` 的硬门口径，补齐 `story.min_shot_seconds` /
  `action_seconds` / `tolerance` / `max_line_chars` / `no_face_chars` 配置说明。
- 补全 Studio 的旁白/转场编辑边界、图文版与 README 的 `docs/ComfyUI对接指南.md`
  入口，清理契约文档 §12 / §14 的 v10.1 版本残留与 live 基线的 `--clean` 表述。
- 修正契约与下游指南中的旁白字段形态：编译后的 `scene.narration` 不保留
  `beat`，改为按 `timing_sec` / `at_sec` 消费；`beat` 仅存在于生成阶段的
  内部剧本模型。
- 区分 `narration_mode="off"`（禁止写旁白，写入即 block）与
  `transition_mode="off"`（关闭强制，缺失降为 warning）。
- 补齐 README / 中英教程的 `script.json`、`storyboard.json` 产物说明
  （场级旁白、场间转场、镜头旁白与首镜转场）；修正 Studio 内部剧本文件名
  为 `script_epNN.json`，并修正报告 `epNN.html` 不含口型字段的旧描述。
- 教程的一键验收改为全量 `pytest tests` / `verify_offline.py`，移除已过时的
  10 个测试模块子集命令。
- 修正完成标记位置：`_completed.json` 实际写在 `state/story/<小说名>/`，
  不在 `input/<小说名>/`；下游交接说明改为按产物完整性或随包清单判断。
- 本地链接检查：0 个失效链接、0 个失效锚点、0 个失效图片。
- **收拢下游交接边界**：契约新增 §0.10“下游接入约定（所有平台通用）”，
  §14 拆为“交给下游的三件套 / 输入层内部交付物”；ComfyUI 指南收敛为
  平台适配层（读取顺序、字段映射、降级、验收），不再重复契约规则；
  README / 教程 / 图文版统一说明“契约 + 平台指南 + 完整样例”，
  明确不交付 `manju/schemas.py` / `gate.py` / `drama.py`。

---

## Unreleased — 配置安全与单一来源（2026-10-26）

### Security

- **不再把 API Key 写进 `config.json`**：新增 `MANJU_LLM_API_KEY` 优先级，仍支持
  `DEEPSEEK_API_KEY` / `OPENAI_API_KEY`；旧配置里的 `llm.api_key` 仅作兼容，
  `--doctor` 会显示来源并提示迁移。
- **Studio 输入的 Key 只保存在进程内存**，随生成子进程通过环境变量下发，
  保存配置时清除旧版明文 Key。Studio 重启后需重新输入；长期使用请设环境变量。
- 移除实际配置中的明文 Key、`llm.temperature`、渲染侧遗留块与 53 个说明键。

### Changed

- 配置收敛为输入层真正消费的字段：`project_root / llm / story / paths / studio`。
  渲染/ComfyUI/TTS/显存等键不再混在输入层配置里。
- 引擎能力项统一到 `story.*`：`max_shot_seconds`、`max_segment_seconds`、
  `min_shot_seconds`、`max_faces_per_shot`、`max_lines_per_shot`。
  旧 `pipeline.*` 键仍兼容，但 `story.*` 优先。
- 未显式传温度时内部兜底从 `0.7` 改为 `0.0`，避免旧配置或新调用引入随机漂移。
- 说明性配置键迁入 `docs/decision-log.md`（该文档为历史存档，不随纯净包交付）；`config.example.json` 只保留可执行字段。

### Tests

- 离线测试 **230 passed / 6 skipped**；新增环境变量 Key 检测、旧配置 Key 兜底、
  缺少 Key 时 live 模式提前退出、以及 token/修复次数解析测试。
- `scripts/pass_rate.py` 的 Key 检测改为与运行时同一套优先级
  （环境变量优先，旧 `llm.api_key` 兜底）；live 报告新增每轮与合计的
  LLM 调用/质量门修复/输入输出 token。

### Known gap

- **live 端到端通过率基线尚未执行**：需要新 Key（旧 Key 应按已泄露处理并轮换）
  且会产生 API 费用。执行步骤、指标口径与记录模板见 `docs/live-baseline.md`。

---

## Unreleased — P0 质量门分级与确定性修复（2026-10-26）

### Changed

- **质量门分两级**：契约/渲染不变量（字段缺失、未知引用、时长算术、
  说话人、镜头数与拍数、EDL 越界等）保持硬失败；故事品味类启发式
  （使用率、覆盖率、节奏配额、字数/镜头数下限、静态占比、连续重复景别等）
  默认降为警告。`--strict` 恢复旧的全硬失败行为，便于质量审计。
  覆盖 `validate.py` 的 bible/outline/cast/art/script/staging/segment/board
  以及 `drama.py` 的 S/D/B 类检查。非严格模式下生成可以完整跑完并产出
  可审计警告，不再因为单条口味规则中断整条流水线。
- 所有结构化生成阶段默认 `temperature=0.0`（系列设计、bible、分集、cast、
  art、script、staging、action line、EDL、提示词扩写），减少同一本小说
  两次运行之间的随机漂移。
- **移除 `story.prompt_identity_mode`**：人物一致性属于实现层能力，输入层
  不再提供 anchor/none/full 开关，也不再在 `prompt_en` 里写身份描述。
  分镜提示词固定只写动作/场景/表演；角色身份继续以结构化数据输出
  （`ref_characters`、`subjects[].expression`、`wardrobe_state`、
  `story_assets.json` 的 `identity_prompt` / `identity_anchor`），由实现层
  按参考图、锁脸或纯文本方案自行注入。Studio 配置页同步移除该选项，旧配置
  键会被忽略。

### Fixed

- 新增 `_normalize_edl_plan`：在质量门之前确定性补齐漏拍、展开跨拍切镜、
  去掉重复拍；把中文人名映射为 Cid；剔除场外角色/道具；清理未知状态键、
  非法服装态、非法道具持有者；继承的场外道具保持原归属。漏拍/多拍/人名/
  越界道具不再触发整场重写。
- 道具归属按镜头顺序推演：只有本镜真的展示道具时才允许易主；画外易主
  （如遗留状态 `P2: scene` 被下一场顺着写成 `scene→C4`）回退到上一归属。
  同镜可见的“环境→角色”交接自动标记 `cut_reason=action_done`。修复
  《绿野仙踪》EP2 S5“道具 P2 从 scene 转到 C4，交接不可见”的整场重写。
- 缓存分镜在 resume 时同样执行 `_repair_segment_output`：剔除未知角色键、
  按剧本服装账本归一服装态、修正可见道具归属，旧缓存无需 `--clean` 也能
  通过新规则。
- 编译期场级 `wardrobe_state` 与拍级换装对齐：一个角色在本场账面换装时，
  不再把起始服装写成全场唯一值（否则 gate 会用它与前后镜头做全等比较，
  产生 `ref.wardrobe_mismatch`）。该角色从场级 map 省略，逐镜
  `subjects[].wardrobe_state` 继续携带正确的 state_id；gate 仍校验其存在性。
  修复《绿野仙踪》EP1 S3 / EP2 S1 共 7 个 wardrobe block。
- 编译期台词 `timing_sec` 同时满足**标准语速**（gate 的容量判定）与
  `scene.speech_cps`（drama S8 的情绪语速），取两者较长值。修复激动场次
  `speech_cps > 4.5` 时 timing 被缩短、gate 报 `time.line_too_long` 的
  block；低沉/悲伤场次仍不会因降速不足被 drama S8 判超时。
- 不递增 `STORY_RULES_REV`：本次只放宽严重度并修复缓存，不改变字段契约或
  提示词；保留指纹以复用已生成的大纲/剧本，避免用户为同一次任务重复付费。
- 新增 `PROMPT_POLICY_REV`：只绑定 `shots_ep*` 这类含逐镜提示词的缓存。
  提示词口径升级时重建这些缓存，但不会连带重跑圣经/大纲/剧本，也不重建
  不含提示词的 EDL 段缓存。

### Studio

- 界面按工作流重排：**生成 → 分集/分镜 → 资产 → 画风/世界观 → 发布**，
  配置与 LLM 日志归入「系统」。前端拆到 `manju/studio_ui.html`，服务端
  仍单文件启动。
- **删除重复的「起止集」**：配置页不再出现集数/起止集/画幅/时长；这些
  参数只在生成页按次设置，并在运行开始时写回 `studio.generation` 作为
  “上次使用”预填。起止集与总集数在界面上分开解释：前者只决定本次跑哪
  几集，后者决定改编成几集。
- **重编译按钮移出顶栏**：新增「发布契约」视图；有未发布修改时页面底部
  出现固定发布条，顶部只保留状态胶囊与小说选择。画风保存也回到对应页面。
- 生成页把「集数 / 每几章一集」合并为「分集方式」三选一；高级选项里的
  「先清临时再重跑」重新可见，不再要求改源码。
- **修复 Studio 读取真实中间产物的模型错位**：`board_epNN.json` 实际是
  `StoryEpisodeBoard`、`script_epNN.json` 实际是 `ScriptEpisode`，原先
  按 `StoryStoryboard` / `StoryScript` 包装解析，导致分镜页 KeyError、
  重编译 ValidationError。现在两种旧/新落盘形态都兼容。
- **修复重编译时的站位合并**：多集复用同一 `scene_id` 时，Studio 原先直接
  拼接地点表，后一集的窄表会覆盖前一集，误报“item 不在地点物件表里”。
  现在复用生成阶段的按名并集逻辑；旧缓存缺登记的站位物件也会在编译前
  按同一规则补齐，并在发布结果里列出自动修复项。
- 生成页显式传 `--chapters-per-episode 0`，确保「按剧情自动分集」不会被
  `config.json` 的旧章节默认值覆盖。
- **配置页移除 temperature**：各阶段温度已在代码中固定（机械阶段 `0.0`、
  卷摘要 `0.2`），全局温度输入框对正常生成没有作用，只会误导。配置页只
  保留并行数；`llm.temperature` 继续作为未显式传值时的内部兜底，旧页面
  提交的同名字段会被忽略。
- 显示分集标题、场/镜/时长统计；镜头按场景分组、可折叠；资产改为标签页。

### Docs / 仓库整理（上传 GitHub 前）

- **README 重构为 GitHub 项目首页**：增加徽章、快速导航、CLI 与 Studio 两种用法的
  对照表、样书试跑命令、文档索引与贡献指南；目录树与路径同步修正。
- **教程补齐两种入口**：命令行教程与 Studio 教程互相链接，并明确 Studio 的
  「生成」后台调用同一个 `story_cli`、「发布契约」复用同一套编译与 gate/drama
  审计；补充 Studio 快速开始与 CLI 参数对照表。
- 统一文档里的入口与路径：`stories\my_story.txt` → `input\my_story.txt`，
  `manju.cli novel` → `python -m manju.story_cli`，契约输出统一为
  `input/<小说名>/...`；测试数量统一为 **221 passed / 6 skipped**。
- 归档过时文档：根目录 `使用说明.md`（旧 Studio 导航与配置字段）与
  `docs/ComfyUI对接指南.md`（原版描述完整联调包的适配器/工作流，本包不含相关代码）
  已移出仓库；渲染侧字段以 `docs/漫剧文本输入契约_v10.2.md` 为准。
  **2026-10-27 更正**：下游实现层仍需一份接口说明，已重写为 v2.0 专用版本
  （只讲契约消费，不含实现），见上方 Unreleased。
- 修正 `故事输入层介绍_图文版.md` 的 v10.1 残留、CLI 命令与产物路径；
  `--plates` / `--run` / 身份图审批等完整联调包能力已明确标注不在本包内。
- `.gitignore` 改为只跟踪两本样书：`input/SSS.txt`、`input/绿野仙踪.txt`；
  其他用户小说默认忽略，避免私有稿件被误传。
- 删除未使用的 `stories/` 目录；清理 `.pytest_cache`、`__pycache__`、
  `state/llm_trace.jsonl`、`state/report`、`state/review`、`state/verify_compile`
  等过程文件。`state/story/<小说名>/` 的已固化中间产物保留，便于续跑。

### Tests

- 离线测试 **221 passed / 6 skipped**（新增 EDL 归一化、道具画外交接、
  编译期换装/语速一致性、Studio 产物路径提示、提示词策略缓存边界、
  缓存修复、严格/非严格严重度回归；新增激动场次 timing 不被缩短、
  Studio 真实 board 形态、集数统计、生成参数单源与旧 range 兼容测试）。

## 2.0.0 — 2026-10-26

### Added

- 独立 v2.0 项目区：独立配置、运行数据、Studio 端口与 git 仓库。
- `--version` 与 `--doctor`：打印包版本、`MANJU_ROOT`、配置/数据路径、
  Key 是否存在（**只显示 configured/MISSING，绝不打印内容**）、情节模式与模型。
- `scripts/verify_offline.py`：编译 + 测试 + CLI 冒烟 + 情节 dry-run，全程不调 API。
- `scripts/audit_run.py`：审计 `must_show` 覆盖、场间 `causal_from`、关键道具
  `insert` 特写、`highlight/key_prop/climax` 非静态运镜；旧产物自动标 `SKIP`。
- `scripts/pass_rate.py`：默认离线；只有显式 `--live` 才真实调用 API。
  每轮写独立的 `state/pass_rate/run_*/`，不覆盖已固化的 `input/<小说>/`。
- `scripts/run.ps1` / `scripts/studio.ps1`：把 `MANJU_ROOT` 固定到 v2.0。
- `.gitignore`、`VERSION`、`MAINTENANCE.md`、v2.0 改动/迁移文档。
- 新增 `--version` / `--doctor` / 产物审计测试；离线测试 204 passed / 6 skipped。

### Fixed

- 分镜静态占比旧门槛（30%–85%）与“关键拍必须运镜”冲突，导致关键拍很多的
  集（如 SSS 第 2 集，静态 23%）被误判失败。现在只硬限制 **>85%**；低于
  15% 改为警告，关键拍仍强制非静态。已用现有前 2 集产物离线重编译验证：
  storyboard / gate / drama 全部通过。
- 模型健康检查不再用 5 秒超时直接阻断生成：改为 10 秒、临时错误重试一次，
  并打印真实 HTTP 状态/原因；只有 401/403（Key）与 402（余额）才提前失败，
  超时/代理/限流/端点不支持 `/models` 时继续走实际生成。
- 道具从环境（`scene`）被角色拿起时，不再要求新持有者出现在上一镜，也不要求
  把 `"scene"` 写进 `characters`。只要本镜同时展示道具与新持有者，且
  `cut_reason=action_done`，就视为镜头内可见交接；新持有者不在画内仍硬失败。
  修复《绿野仙踪》S3“银鞋从地上被穿到多萝茜脚上”被误判“交接不可见”。
- 分镜装配按剧本拍级 `state_change` / `state_out` 服装快照强制落地换装，
  后续镜头自动继承；跨场时剧本已授权的换装不再要求 `space/time`，
  新场开场按场级 `wardrobe` 快照换装也放行。修复《绿野仙踪》S3→S4
  “C1 服装 C1-arrival → C1-default，没有换装理由”。
- 分镜装配以场次 `props` 为最终权限：本场未声明的道具不再出现在切镜里；
  已继承的场外道具保留原归属，不允许在本场视线外易主；模型凭空新增的
  场外道具直接丢弃，并同步修复后续镜头的 `state_from` 链。修复《绿野仙踪》
  EP2 S4“P2 越出本场道具 / P2 从 scene 转到 C1，交接不可见”。
- 新场首镜允许从上一场特写直接切全景重建空间，不再强制模型编造
  `new_info` 理由；场内景别跳切规则不变。修复《绿野仙踪》EP2 S4 前两次
  “extreme-close → wide 缺少 new_info/pov/reaction/sound 理由”的无效重试。

### Inherited from v1.2

- 情节原子与按戏剧单元分集（不按 9000 字/2 章硬切）。
- 每集 2–6 条 `must_show`、0–3 件 `key_props`，剧本逐条认领。
- 场间 `causal_from`、关键道具 `insert` 特写、关键拍非静态运镜。
- `cut_plan` 把 `emphasis` / `visual_focus` 透传到视觉提示词。
- `--to-episode N` 只读前 N×5000 字设计，截断点对齐句末/换行。
- 剧本节奏规则 19、开场钩子运镜、`major_event` 模糊匹配。

### Changed

- Studio 默认端口 8091 → **8092**，避免与 v1.2 同时运行时看错数据目录。
- 真实 `config.json` 补上情节模式与原子/前缀/回退/卷/批/门重试默认键；
  Key 原样保留，未被打印或写入日志。

### Not changed

- 外部契约：**v10.2**。
- 渲染/ComfyUI 执行层：仍不在本包内。

### Known gaps

- **尚未做 live 端到端测量**，因此不宣称 ≥90% 通过率。
- 90 秒成片的最终质量仍取决于源文本与文本模型。
