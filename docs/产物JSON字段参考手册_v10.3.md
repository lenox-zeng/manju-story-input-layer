# 产物 JSON 字段参考手册 v10.3

> 配套契约：`docs/漫剧文本输入契约_v10.3.md`（规范与约束的权威来源；本文档只做**逐字段字典**，冲突时以契约为准）。
> 样例数据来源：`绿野仙踪 EP01`（90s）真实产物，字段结构与契约一致。
> 适用产物：`plan.json` / `script_ep01.json` / `storyboard_ep01.json` / `director_ep01.json` / `story_assets.json`。

---

## 0. 六个产物文件一览

| 文件 | 内容 | 消费方 |
|---|---|---|
| `plan.json` | 全集分集计划：项目设定、角色表、每集摘要/hook、连续性约束 | 生成侧、后续集续写 |
| `script_ep01.json` | 单集剧本：场、台词行、旁白（本层旁白**留空**，权威在分镜）、节拍 | 分镜与配音层 |
| `storyboard_ep01.json` | **单集分镜**（成片唯一权威：画面+动作+声音+时间轴） | **实现层主消费** |
| `director_ep01.json` | 导演层：转场、音效、机位声明、情绪曲线 | 剪辑/动效层 |
| `story_assets.json` | 角色/场景/道具资产：定妆提示词、身份锚点、多服装态、音色 | 渲染层（锁脸/定妆） |
| `prompts/story.md` | 本次生成全链提示词 | 溯源审计 |

---

## 1. storyboard_ep01.json（重点）

### 1.1 顶层

| 字段 | 类型 | 含义 |
|---|---|---|
| `schema_version` | str | 契约版本号（`storyboard-v1` 系） |
| `episode` | int | 集号 |
| `style_suffix` | str | 画风后缀（当前为空：美术风格收敛在 assets） |
| `shots` | array[shot] | 37 个镜头，**按成片顺序** |
| `scene_anchors` | dict | 每场"空场景"定场提示词（无人物），渲染层场景基底图用 |
| `hints` | dict | 顶层提示（当前为空 `{}`） |

### 1.2 单镜字段（37 镜字段结构完全一致，共 27 个）

#### ① 标识与时间轴

| 字段 | 类型 | 含义 | 示例（SH01） |
|---|---|---|---|
| `shot_id` | str | 镜头编号（成片顺序） | `SH01` |
| `scene_id` | str | 所属场 | `S1` |
| `act_beat` | str | 本镜承担的剧作节拍 | `抉择` |
| `start_sec` | float | 全片时间轴起点（秒） | `0.0` |
| `end_sec` | float | 全片时间轴终点（秒） | `2.24` |
| `duration_sec` | float | 本镜时长（全片镜头连加 = 集长，90.0s） | `2.24` |
| `duration_basis` | str | **时长算账过程**（人工复核为什么是这个时长） | `动作4拍×1.5s=6.0s；景别下限3s → 取大6.0s；EDL 分配2.2s（不足）` |

#### ② 画面内容（给生成模型）

| 字段 | 类型 | 含义 | 示例（节选） |
|---|---|---|---|
| `prompt` | str | **英文首帧提示词**：主体+动作瞬间+光线+情绪，结尾含配方两件套（光+镜） | `A girl in a pale blue and white gingham pinafore dress ... 50mm shallow depth of field.` |
| `motion` | str | 画面内动作（英文进行时）+ 运镜，视频模型动态描述 | `Slow push in toward the girl and the dog as the door slams...` |
| `keyframe` | str | 关键帧画面要点（中文，人工核对） | `空间关系·medium：灰蒙蒙的草原上...` |
| `emotion` | str | 情绪标签（可空） | `""` |
| `effect` | str | 特效光/粒子/残影（英文，可空） | `""` |

#### ③ 构图结构 `framing`（每个启用画幅一套）

`framing` 内 `9:16` 与 `16:9` 各一套独立构图（竖屏不是横屏切一刀）。每套含：

| 字段 | 类型 | 含义 | 示例 |
|---|---|---|---|
| `shot_size` | str | 景别（medium / close / extreme close-up ...） | `medium` |
| `subject_type` | str | 主体类别：`character` / `object` / `environment` | `character` |
| `composition` | str | 人类可读构图描述（中文） | `9:16：空间关系·medium：...；阴云压顶的灰色日光...` |
| `subjects[]` | array | 画幅内每个角色的站位，见下 | — |
| `overlap_order` | array | 完整前后遮挡顺序（前→后） | `[]` |
| `camera_recipe` | obj | **摄影配方结构化槽位**（契约 §6.5），见下 | — |

`framing.*.subjects[]`（站位）：

| 字段 | 类型 | 含义 | 示例 |
|---|---|---|---|
| `char` | str | 角色 ID | `C1` |
| `pos` | str | 画面位置 | `center_left` |
| `height_level` | str | 高度层（top/mid/bottom） | `mid` |
| `depth` | str | 景深层次（foreground/midground/background） | `midground` |
| `facing` | str | 朝向 | `toward_camera` |
| `occludes` | array | 遮挡哪些角色 | `[]` |

`framing.*.camera_recipe`（摄影配方，实现层调节点）：

| 字段 | 类型 | 含义 | 示例 |
|---|---|---|---|
| `lens_mm` | str | 焦段（24/35/50/85...） | `50mm` |
| `depth_of_field` | str | 景深（shallow/moderate/deep） | `shallow` |
| `light_key` | str | 光源方向 | `behind` |
| `light_quality` | str | 光质量（diffused/hard/atmospheric/grey...） | `grey` |

> 注意：`camera_recipe` 只写在每个画幅**内部**（per-aspect）；顶层不再冗余（v10.3 产物已清理）。

#### ④ 角色

| 字段 | 类型 | 含义 | 示例 |
|---|---|---|---|
| `subjects[]`（镜级） | array | 角色状态，见下 | — |
| `ref_characters` | array[str] | **本镜画面内实际出现**的角色 ID 清单（取定妆图参考；画外角色不入，画外对象用 `offscreen=true`） | `["C1","C2"]` |

`subjects[]`（镜级）项：

| 字段 | 类型 | 含义 | 示例 |
|---|---|---|---|
| `char` | str | 角色 ID | `C1` |
| `wardrobe_state` | str | 本镜服装态（对应 assets `wardrobe_states` 键） | `C1-default` |
| `pose` | str | 姿势 | `standing` |
| `expression` | str | 表情（可空） | `""` |
| `speaking` | bool | **是否开口说话**（口型诉求） | `false` |

#### ⑤ 机位与调度

| 字段 | 类型 | 含义 | 示例 |
|---|---|---|---|
| `camera` | str | 机位形态：static / handheld / tripod / steadicam | `tripod` |
| `camera_move` | str | 运镜：push_in / pull_out / pan_left / tilt_up / track... | `push_in` |

#### ⑥ 声音

| 字段 | 类型 | 含义 | 示例 |
|---|---|---|---|
| `audio` | str | 台词（画内/画外标记 + 说话人 + 文本；无台词为空） | `""` |
| `narration[]` | array | **旁白（唯一权威）**，见下 | — |
| `sfx` | str | 音效（后期用，不进画面提示词） | `狂风呼啸，木门砰砰撞击门框...` |

`narration[]` 项（旁白锚点）：

| 字段 | 类型 | 含义 | 示例 |
|---|---|---|---|
| `kind` | str | 旁白类型：exposition / transition / emotion ... | `exposition` |
| `text` | str | 旁白文案（中文） | `堪萨斯草原常年干旱，龙卷风地洞是唯一避难处。` |
| `speaker` | str | 说话人（画外音 = `NARRATOR`） | `NARRATOR` |
| `delivery` | str | 语气/演播要求 | `低沉平稳` |
| `timing_sec` | float | 朗读时长（秒） | `5` |
| `shot_id` | str | **锚定镜** | `SH01` |
| `at_sec` | float | 起播秒（相对锚镜起点） | `0.3` |
| `anchor_mode` | str | 锚定模式：`within_shot`（锚镜内）/ `voiceover_span`（跨镜） | `voiceover_span` |
| `allow_span_shots` | int | 最多跨几镜 | `3` |
| `allow_cross_scene` | bool | 是否允许跨场 | `false` |
| `max_shift_sec` | float | 允许漂移上限（秒） | `1.0` |

#### ⑦ 结构化提示 `hints`（给下游 gate / 剪辑的逻辑字段）

| 字段 | 类型 | 含义 | 示例 |
|---|---|---|---|
| `coverage` | str | 镜头类型：establish / action / dialogue / insert ... | `establish` |
| `cut_reason` | str | 切镜原因：action_done / reaction / pov ... | `action_done` |
| `purpose` | str | 本镜叙事目标 | `S1 establish 拍：...` |
| `information` | str | 关键信息 | `龙卷风中心正把小房子抬起。` |
| `scene_shot` | int | 场内容第几镜 | `1` |
| `allow_multi_face` | bool | 是否允许多正脸同框（false 时下游保脸策略规避） | `true` |

#### ⑧ 分段 `segments`（动作节拍 >1 的镜拆 2-3 段，一段一动作；单动作镜不分段）

| 字段 | 类型 | 含义 | 示例 |
|---|---|---|---|
| `text` | str | 段英文提示词（独立动作句） | `A girl in a pale blue and white gingham ... her braids lifted sideways by wind.` |
| `duration_sec` | float | 段时长（**段和 = 整镜**） | `1.12` |
| `phase` | str | 段相位：start / mid / end | `start` |
| `beat` | str | 段的中文动作要点 | `establish：灰蒙蒙的草原上...` |
| `time_range` | array[float,float] | 段区间 [起,止]（连续不重叠） | `[0.0, 1.12]` |
| `camera_phase` | str | 段运镜 | `Push In` |
| `state_in` | str | 段起始状态（承接上一段） | `""` |
| `state_out` | str | 段结束状态（交给下一段；末段含完整 `where/facing/posture/...`） | `where:{"C1": "center", ...}` |
| `keyframe` | bool | 是否为关键帧（**末段 = true**） | `false` / 末段 `true` |
| `frame_hint` | str \| null | 尾帧条件：`last`（末段） | `last` |
| `source` | str | 来源：`fallback`（确定性拆段兜底）/ `llm` | `fallback` |

#### ⑨ 可选生产字段

| 字段 | 类型 | 含义 | 备注 |
|---|---|---|---|
| `aspect_overrides` | null/obj | 画幅覆盖 | 常规为 null |
| `requirements` | obj | 能力声明：`multi_subject_strategy`（auto/joint/hybrid...） | 同框≥2 人必填 |
| `transition_in` | null/obj | 转场设计 | 常规为 null |
| `track` | null/str | 运动轨迹 JSON 路径 | 量产为 null |
| `stage_override` | null/str | 制作阶段：draft / standard / hero / skip | 量产为 null |
| `verify_point` | null/str | 验证点标签 | 量产为 null |

### 1.3 消费速记（实现层）

| 需求 | 读哪里 |
|---|---|
| 首帧画面 + 摄影 | `prompt`（首帧+配方两件套） |
| 画面内动作 / 运镜 | `motion` + `camera` + `camera_move` |
| 阶段化拆段（LTX） | `segments[]`（逐段注入） |
| 尾帧条件 | `segments` 末段 `keyframe:true` + `frame_hint:"last"` |
| 锁脸/定妆参考 | `ref_characters` + `subjects[].wardrobe_state` + assets `identity_anchor` |
| 口型 | `subjects[].speaking` + `audio` |
| 旁白对齐 | `narration[]`（shot_id / at_sec / anchor_mode / allow_span_shots / max_shift_sec） |
| 配光配镜 | `framing.*.camera_recipe` |

---

## 2. plan.json 顶层

`schema_version` / `plan_version` / `scope` / `project` / `genre` / `logline` / `target_audience` / `total_episodes` / `visual_bible`（画风圣经，唯一画风来源）/ `output` / `requirements`（受限槽位枚举+能力声明）/ `cast_separation` / `characters`（角色表）/ `episodes[]` / `continuity` / `hints`。

`episodes[]` 项：`num`（集号）/ `title` / `summary`（本集摘要）/ `hook`（结尾钩子）。

---

## 3. script_ep01.json

顶层：`schema_version` / `series_ref` / `episode` / `title` / `aspects_override` / `requirements` / `scenes[]` / `continuity_ledger`（状态账本，机械校验）/ `hook_chain` / `hints`。

`scenes[]` 项：`scene_id` / `location` / `time` / `action` / `scene_type`（→ 质量档位）/ `space_map`（空间关系基准站位）/ `character_flags` / `chars_on_screen` / `wardrobe_state` / `speech_cps`（语速）/ `lines[]`（台词）/ `narration`（**留空**，权威在分镜）/ `transition_in` / `mood` / `duration_sec` / `beat` / `function`。

---

## 4. director_ep01.json

顶层：`schema_version` / `episode` / `title` / `logline` / `spine` / `power_rule` / `setup_payoff` / `scenes[]` / `shots[]`（转场、音效、机位声明、情绪曲线）。

---

## 5. story_assets.json（story-assets-v1）

顶层：`schema_version` / `source`（原著来源）/ `characters`（C1-C5）/ `scenes`（S1-S4）/ `scene_refs` / `props`（P1-P5）。

`characters.<id>` 项：

| 字段 | 含义 |
|---|---|
| `name` | 角色名（中文） |
| `identity_prompt` | 英文全身定妆提示词（渲染层锁脸源） |
| `identity_anchor` | 中文可一眼识别锚点（prompt 中逐字复用的短语） |
| `negative_prompt` | 排除项 |
| `sheet` | 角色三视图提示词 |
| `wardrobe_states` | 多服装态（如 C1-default / C1-windswept / C1-oz-arrival） |
| `voice` | 音色（timbre/pitch/pace） |
| `color_signature` | 色彩签名 |
| `pose_library` / `expression_library` / `views` | 姿势/表情/视角库 |

---

## 6. 字段三级分类（契约 §1.3）

- **A 层（必填、block 校验）**：时间轴、结构化站位、ref_characters、旁白锚点等成片正确性关键字段。
- **B 层（可选但校验）**：camera_recipe、requirements 等能力/质量声明。
- **C 层（可选自由字段）**：未知字段允许存在，不得报错或静默丢弃（见契约 §1.3）。
