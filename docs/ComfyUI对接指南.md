# ComfyUI 对接指南（契约 v10.3）

> **平台**：ComfyUI / 渲染层  
> **目标契约**：v10.3  
> **指南版本**：1.1（2026-11-01，v10.3 旁白时间契约同步）
>
> 本文只写 **ComfyUI 侧怎么消费契约**：读取顺序、字段映射、降级策略与验收。
> 字段语义以 [`docs/漫剧文本输入契约_v10.3.md`](漫剧文本输入契约_v10.3.md) 为准；
> 本文**不定义新字段**，只保留 ComfyUI 必须知道的结论，规则细节以契约为准。
>
> **这份文档是接口说明，不是渲染代码。**
> 本包（v2.0）只负责把小说编译成 v10.3 契约；ComfyUI 客户端、能力注册表、
> 适配器、工作流、TTS、口型与显存编排都在**下游实现层**。
> 下游可以按本文档实现或改造自己的适配层，不需要改故事层、提示词或契约字段。

---

## 0. 边界与责任

```
小说
  → manju-story-input v2.0（解析 / 分集 / 故事层 / 编译 / 门禁）
  → v10.3 契约：plan.json / script.json / storyboard.json / director.json
  → 下游实现层（ComfyUI 或任意生成引擎）
      ├─ 资产：定妆图 / 场景 plate / 道具设定
      ├─ 画面：首帧 → 图生视频
      ├─ 声音：台词 TTS / 旁白 TTS / 字幕 / 混音
      ├─ 口型：仅画内台词
      └─ 剪辑：时间轴 / 双画幅 / 场间转场
```

**交接三件套**：本契约 + 本文 + 一份完整样例（如 `input/<小说名>/`；纯净包不含生成结果，样例需从实际运行产出中单独提供）。
`manju/schemas.py` / `gate.py` / `drama.py` 是输入层内部实现，不随接口交付。

输入层承诺：

- 所有业务事实都在契约里，下游不需要读 LLM 中间产物；
- A 层字段稳定、可机械校验，不会出现“引擎参数伪装成业务字段”；
- 所有新增字段都是可选的，旧产物可继续读取；
- 下游遇到不支持的能力必须**显式报错**，不能静默降级。

输入层不承诺：

- 不包含 ComfyUI 连接、节点图、模型名、工作流 JSON；
- 不包含 TTS、口型、混音、显存管理；
- 不保证某个引擎一定能实现某个能力——这由下游能力注册表声明。

---

## 1. 最小对接流程

```powershell
# 1) 生成契约（只调文本模型，不出图）
python -m manju.story_cli input\my_story.txt --to-episode 2

# 2) 机械审计（不启动 ComfyUI）
python -m manju.gate input\my_story\external\plan.json `
  input\my_story\external\ep01\script.json `
  input\my_story\ep01\storyboard.json --strict

python -m manju.drama input\my_story\external\plan.json `
  input\my_story\external\ep01\script.json `
  input\my_story\ep01\storyboard.json `
  --director input\my_story\external\ep01\director.json --strict
```

下游适配层的最小输入：

| 文件 | 用途 |
|---|---|
| `input/<小说>/external/plan.json` | 全剧圣经：角色、身份锁、服装态、视觉圣经、画幅、能力默认值 |
| `input/<小说>/external/epNN/script.json` | 本集场次、台词绝对时间轴、旁白、场间转场 |
| `input/<小说>/epNN/storyboard.json` | 逐镜首帧提示词、景别、构图、人物姿态、运镜、时长、双画幅 |
| `input/<小说>/external/epNN/director.json` | 导演层：spine、power_rule、setup/payoff、coverage、切点 |
| `input/<小说>/story_assets.json` | 定妆 / 场景 / 道具的提示词与资产清单 |
| `input/<小说>/prompts/story.md` | 人工核对用，不参与渲染 |

> 只有完整成功（质量门 + 编译 + 审计全过）的产物才会固化到 `input/<小说>/`。
> 完成标记 `_completed.json` 写在中间产物目录 `state/story/<小说>/`，
> **不在 `input/<小说>/` 里**；下游如果只收到 `input/` 包，请用“必需文件是否齐全”
> 判断，或由交付方随包提供完成标记/清单，避免消费半成品。

---

## 2. ComfyUI 侧读取顺序与契约引用

> 本节只保留 ComfyUI 必须知道的结论；规则细节以契约 §1.3 / §1.4 / §4.2 / §4.3 / §6 为准。

### 2.1 读取顺序

`plan.json` → `script.json` → `storyboard.json` → `director.json` / `story_assets.json`。
`director.json` 是导演业务信息，`story_assets.json` 是资产清单；两者不改变前三份文件的契约地位。

### 2.2 版本与三层

- 产出统一写 `"schema_version": "v10.3"`；读取器接受 `v10` / `v10.1` / `v10.2` / `v10.3`
  （同一大版本）。版本规则见契约 §1.4。
- A 层业务事实、B 层能力声明遇到未知字段必须报错；C 层 `hints` 可忽略且不持久化。
  细节见契约 §1.3。
- **下游自带 schema 时必须补齐 v10.3 相关字段**：`scene.narration`（v10.3 起恒为空）、
  `scene.transition_in`、`shot.narration`（含锚点字段 `anchor_mode` / `shot_id` /
  `at_sec` / `timing_sec` / `allow_span_shots` / `allow_cross_scene` /
  `max_shift_sec`，见契约 §8.4）、`shot.transition_in`。
  否则严格解析会报未知字段；更危险的是 `extra="ignore"` 会静默吞掉旁白/转场，
  成片缺内容却无人报警。

### 2.3 `requirements` 与双画幅

- `requirements` 是**键级合并**，优先级：镜头级 → script 级 → plan 级 → 内置默认；
  下游应输出生效链。细节见契约 §4.2 / §4.3。
- 编译器会为每个镜头同时写 `9:16` 与 `16:9` 两套 `framing`；
  `plan.output.aspects.enabled` 表示本次目标画幅（当前编译器写单个）。
  下游按 `enabled` 决定实际出片画幅，但**读取时保留另一套构图**，
  不要因为“未启用”就丢弃或判为非法。

---

## 3. 业务字段 → 下游动作

### 3.1 `plan.json`（全剧唯一）

| 字段 | 下游用途 |
|---|---|
| `project / genre / logline` | 片名、类型、简介；可用于片头/平台元数据 |
| `visual_bible` | 全片画风唯一来源；生成任何图片都必须注入 |
| `visual_bible.forbidden` | 负面条件基线；必须覆盖全部词条 |
| `output.aspects` | 启用画幅、主画幅、派生策略、最低分辨率 |
| `output.episode_sec` | 单集目标时长 |
| `requirements` | 全剧能力默认值，被 script / shot 逐键覆盖 |
| `characters[]` | 角色 id、身份锁、服装态、标志道具、音色参考 |
| `episodes[]` | 分集标题、梗概、结尾钩子 |
| `continuity` | 不可变/可变字段与变更政策 |

### 3.2 `script.json`（每集）

场次级关键字段：

| 字段 | 下游用途 |
|---|---|
| `scene_id / location / time` | 场景定位、场景 plate 选择 |
| `action` | 本场可见动作（中文，供人工核对） |
| `scene_type` | `dialogue / action / transition / montage / group_dialogue` |
| `space_map` | 本场基准站位；单镜走位以 storyboard.framing 为准 |
| `chars_on_screen` | 本场实际出场（含无台词者）；出图不能漏人 |
| `wardrobe_state` | 场级服装态；逐镜以 shot.subjects 为准 |
| `speech_cps` | 本场语速覆盖（字/秒） |
| `lines[]` | 句级台词；字幕 / TTS / 口型共用这份时间轴 |
| `narration[]` | **v10.3 起恒为空**：旁白唯一权威在 shot 级；读到非空 = v10.2 遗留，以 shot 级为准并告警 |
| `transition_in` | **v2.0 新增**：本场从上一场如何接进来 |
| `mood / duration_sec / beat / function` | 情绪、时长、节拍、场功能 |

台词时间轴：

```json
{
  "speaker": "C1",
  "text": "你到底是谁",
  "emotion": "冷硬",
  "timing_sec": 1.5,
  "at_sec": 3.2
}
```

- `at_sec` 是**全集绝对时间**（秒），与 shot 的 `start_sec` 同一坐标系；
- `timing_sec` 是预期时长；TTS 实际时长以生成结果为准；
- 字幕、TTS、口型三处共用这份时间轴，不要另算一套。

### 3.3 `storyboard.json`（每集）

镜头级关键字段：

| 字段 | 下游用途 |
|---|---|
| `shot_id / scene_id / act_beat` | 镜头与场次归属、节奏标签 |
| `prompt` | 首帧画面的权威文本描述（只写画面，不写运镜） |
| `framing` | 每个启用画幅的景别、主体类型、构图、人物位置 |
| `subjects[]` | 逐角色姿态、表情、服装态、是否开口 |
| `ref_characters[]` | 需要挂角色定妆图的角色 id |
| `requirements` | 本镜能力声明（口型、插帧、多人策略等） |
| `requirements.multi_subject_strategy` | **同框 ≥2 人时必填** `joint` / `per_subject` / `hybrid`；plan 级 `auto` 不能兜底 |
| `motion` | 画面内被摄主体的动作（英文进行时） |
| `camera / camera_move` | 机位形态与运动 |
| `audio` | **只放台词**（同期 / 画外），不放旁白 |
| `narration[]` | **v10.3 唯一权威**：叠在本镜上的旁白/内心独白，每条带 §8.4 锚点字段 |
| `transition_in` | **v2.0 新增**：仅场次第一镜声明 |
| `sfx / effect / emotion / keyframe` | 音效、特效、情绪、人工核对要点 |
| `start_sec / duration_sec / end_sec` | 成片时间轴 |
| `duration_basis` | **v2.0 新增**：中文时长算账过程（`动作3拍×1.5s=4.5s；…取大4.5s→对齐5s`），供人工复核，非引擎参数 |
| `segments` | **v2.0 阶段化扩展**：长镜头分段（可选）。一段一个主动作；`duration_sec` 段秒数、段区间连续不重叠且段和=整镜时长；`state_out→下一段 state_in` 承接；`phase`=start/mid/end；`beat` 中文动作要点；`camera_phase` 继承整镜运镜；`keyframe`/`frame_hint` 用于条件注入；`source="fallback"` 段为 compile 语义动作组拆分（**2026-11-02 修复**：每段=基础定位/主体指代 + 本段分配的动作从句，段间独立、动作不丢失；取代旧实现"整镜 prompt 开头整段复制进每段"的退化，见 `scripts/check_prompt_quality.py` 质量门）；缺省段为 LLM 精写（见下文"LTX 消费方式"） |
| `hints` | C 层引擎提示，业务侧留空；下游可忽略。导演业务信息（`coverage` / `cut_reason` / `purpose` / `information`）的权威副本在 `director.json`，`shot.hints` 中的同名字段仅为兼容旧适配层 |

人物镜的 `framing.subjects` 示例：

```json
{
  "char": "C1",
  "pos": "center_left",
  "height_level": "mid",
  "depth": "midground",
  "facing": "toward_other",
  "occludes": []
}
```

`subjects[].speaking` 只表示**画内开口**。旁白与内心独白永远不设置
`speaking=true`，也不写 `requirements.dubbing="lipsync"`。

### 3.4 `director.json`

导演层是**业务信息**，不是引擎参数：

- `spine`：全剧脊柱；
- `power_rule`：金手指的可见/不可见、代价、使用点、受限点；
- `setup_payoff`：铺垫与回收；
- `scenes[]`：场目标、阻力、转折、代价、信息、道具、镜头列表、
  `transition_in`、`narration`；
- `shots[]`：`coverage`、`cut_reason`、`purpose`、`information`、
  `narration`、`transition_in`。

下游可用于剪辑决策、预告片选镜、质量报告；不要求渲染器理解全部字段。

### 3.5 `story_assets.json`

```json
{
  "schema_version": "story-assets-v1",
  "source": "my_story.txt",
  "characters": {
    "C1": {
      "name": "沈砚",
      "identity_prompt": "英文定妆提示词",
      "identity_anchor": "身份锚点描述",
      "negative_prompt": "负面提示词",
      "sheet": "定妆图说明",
      "wardrobe_states": {"C1-default": "英文服装态描述"},
      "voice": {"timbre": "...", "prompt": "..."},
      "color_signature": "...",
      "views": ["identity", "side"]
    }
  },
  "scenes": {
    "S01": {
      "name": "雨夜窄巷",
      "plate_prompt": "英文空镜提示词",
      "negative_prompt": "...",
      "anchors": ["环境锚点"],
      "lighting": {"夜": "英文光线描述"},
      "variant_of": null
    }
  },
  "scene_refs": {
    "1": {"S01": "S01", "S02": "S02", "S03": "S01"}
  },
  "props": {
    "P1": {
      "name": "断伞",
      "prompt": "英文道具提示词",
      "negative_prompt": "...",
      "states": {"default": "..."},
      "scale": "handheld",
      "carried_by": "C1"
    }
  }
}
```

建议资产路径（下游可自行调整，但同一集内必须稳定）：

```text
assets/characters/{char_id}/identity.png
assets/characters/{char_id}/side.png
assets/scenes/global/{scene_id}/plate.png
assets/scenes/ep{NN}/{scene_id}/plate.png
assets/props/{prop_id}/sheet.png
```

场景 plate 按全局 `scene_id` 生成并跨集复用，不要每集重复生成。

> **两套 id 不能混用**：`script/storyboard.scene_id` 是**场次 id**（每集
> S1..Sn 重新计数）；`scenes` 的 key 是**地点资产 id**（全剧复用）。
> 同一地点可以在多场出现，场次号也可能与某个无关地点的资产 id 撞名。
> 必须按 `scene_refs[集号][scene_id]` 取资产，再读
> `scenes[资产 id].plate_prompt` / `variant_of`；**不要用同名 key 直接猜**。
> 例如某集 `S3` 是主车厢，`scene_refs["1"]["S3"] == "S1"`，而资产 `S3`
> 可能是另一个地点（铁门）。

---

## 4. 旁白与内心独白（v2.0 新增；v10.3 收口到 shot 级）

### 4.1 数据形态（v10.3）

**旁白只有一份权威：`storyboard.shots[].narration`**。`script.json` 的
`scenes[].narration` 必须留空（v10.3 起输入层不再写；读到非空 = v10.2 遗留，
以 shot 级为准并告警）。

`storyboard.json` 的镜头旁白（每条带 §8.4 锚点字段）：

```json
{
  "shot_id": "SH07",
  "start_sec": 18.0,
  "duration_sec": 2.4,
  "narration": [
    {
      "kind": "inner",
      "speaker": "C1",
      "text": "他认识这把伞。",
      "delivery": "压低声线",
      "timing_sec": 2,
      "at_sec": 18.1,
      "shot_id": "SH07",
      "anchor_mode": "within_shot",
      "allow_span_shots": 1,
      "allow_cross_scene": false,
      "max_shift_sec": 0.5
    }
  ]
}
```

跨镜旁白（开场/蒙太奇，`anchor_mode="voiceover_span"`）示例：

```json
{
  "kind": "exposition",
  "speaker": "NARRATOR",
  "text": "多萝茜和托托住在堪萨斯草原的小木屋里。",
  "delivery": "平缓、克制",
  "timing_sec": 6,
  "at_sec": 0.0,
  "shot_id": "SH01",
  "anchor_mode": "voiceover_span",
  "allow_span_shots": 3,
  "allow_cross_scene": false,
  "max_shift_sec": 1.0
}
```

规则：

- `kind=exposition`：解释性旁白，`speaker` 固定 `NARRATOR`；
- `kind=inner`：角色内心独白，`speaker` 是角色 id（`C1` 格式），且该角色
  必须在本镜 `subjects` / `ref_characters` 中；
- **锚点**：`within_shot` → `at_sec ≥ start_sec` 且 `at_sec+timing_sec ≤ end_sec−0.08`；
  `voiceover_span` → 跨度 ≤ `allow_span_shots` 镜、`allow_cross_scene=false` 时不跨场；
- **空隙**：旁白与台词、旁白与旁白之间留 ≥0.08s；实现层只允许在
  `max_shift_sec` 内微调，找不到空位就回传冲突信息（哪个镜/哪句台词/缺口几秒），
  **禁止"只向后顺延"的贪心**（0.1s 级联成十几秒）；
- 旁白不做口型、不设置 `speaking=true`、不写 `requirements.dubbing="lipsync"`；
- 旁白不得复述画面里已经看得见的动作；
- 每集上限：解释性旁白 ≤2 条、内心独白 ≤3 条（由输入层配置控制，
  下游按实际字段消费即可）。
- `timing_sec ≥ ceil(汉字数/5)`，建议留 10–20% 余量。

> 编译后的契约不保留 `beat`，只保留已算好的 `at_sec` / `timing_sec` 与锚点。
> 下游按时间轴消费即可，不需要自己推断拍号。

### 4.2 下游必须做的四件事

1. **字幕**
   - 读取 `shot.narration[]`，按 `at_sec` 排入全集字幕轨；
   - `timing_sec` 作为初始显示时长，实际可按 TTS 结果微调；
   - 建议 `exposition` 与 `inner` 使用不同样式（如斜体/颜色），
     但都不要和台词字幕混在同一行。

2. **TTS**
   - `exposition` 使用叙述者音色；
   - `inner` 使用该角色音色（可比正常台词更近、更干、更低）；
   - 旁白 TTS **不进入口型链路**；
   - 如果下游只支持一个 TTS 音色，至少要把旁白单独生成，不能直接跳过。

3. **混音**
   - 旁白是叠轨，按 `at_sec` 放入，不占镜头时长；
   - 与台词/原声重叠时做 ducking（建议压低 4–8 dB）；
   - 同一时刻只保留一条人声旁白；输入层门禁会在重叠时告警。

4. **一致性校验（v10.3）**
   - 旁白文本不得为空；
   - `kind=inner` 的 speaker 必须在画面里；
   - `timing_sec` 不得小于 `ceil(字符数 ÷ 5 字/秒)`（编译已按此口径计算，最小 1 秒）；
   - `at_sec` 必须落在锚定镜头区间内（按 `shot_id` 找锚镜，缺省按承载镜）；
   - `within_shot`：`at_sec+timing_sec ≤ 锚镜 end_sec − 0.08`；
   - `voiceover_span`：跨度 ≤ `allow_span_shots` 镜，`allow_cross_scene=false` 时不跨场；
   - 旁白与台词、旁白与旁白间距 ≥0.08s，无重叠；
   - 找不到空位：输出精确冲突信息（镜头 / 台词 / 缺口秒数），**不静默后移**。

> **不要把旁白拼进 `shot.audio`。** `audio` 的格式是
> `同期（C1，画内）：…；画外（C2，无口型）：…`，外部 storyboard 的解析器
> 会把它当台词，进而错误地要求口型或错误计算句数。

---

## 5. 场间转场（v2.0 新增）

### 5.1 数据形态

```json
{
  "shot_id": "SH12",
  "scene_id": "S03",
  "transition_in": {
    "type": "sound_bridge",
    "anchor": "远处的警笛",
    "note": "上一场结尾先听到警笛，本场从声音接进来"
  }
}
```

- 只出现在场次第一镜；
- `script.json` 的场次级 `transition_in` 与之一致；
- `director.json` 的场景与镜头也会带同一份信息。

### 5.2 类型与下游动作

| `type` | 含义 | 下游建议 |
|---|---|---|
| `match_cut` | 共享形状/构图/动作的匹配剪辑 | 不加视觉特效；按首镜构图直接切 |
| `action_bridge` | 上一场动作在本场继续 | 不加视觉特效；保证动作方向/速度连续 |
| `sound_bridge` | 声音先入或延续 | 不加视觉特效；把 `anchor` 的声音提前/延后搭桥 |
| `time_jump` | 时间跳跃 | 可加短黑场/字幕时间提示；不加也不违规 |
| `fade` | 淡入淡出 | 可映射为场景边界 fade |
| `dissolve` | 叠化 | 可映射为 xfade |

约束：

- `match_cut` / `action_bridge` / `sound_bridge` 是**剪辑理由**，不是视觉特效；
  下游不要擅自给它们加淡入淡出；
- `fade` / `dissolve` / `time_jump` 才适合映射为 xfade / dip-to-black；
  引擎不支持时保持硬切并记录“未实现转场特效”，不要静默改语义；
- `transition_in` 只读取场次第一镜，不要逐镜重复应用；
- bridge 类的 `anchor` 为空说明输入层没有给出可执行的桥接点，下游应告警，
  而不是自己猜一个。

---

## 6. 能力声明与工作流映射

`requirements` 的合法键与值：

| 键 | 值 | 下游含义 |
|---|---|---|
| `identity_fidelity` | `low / medium / high / locked` | 与角色参考的相似度；`locked` 必须与定妆图一致 |
| `wardrobe_fidelity` | `low / medium / high` | 服装与 `wardrobe_state` 的一致度 |
| `spatial_control` | `none / loose / strict` | 是否严格按 `framing.subjects` 摆放人物 |
| `temporal_stability` | `none / shot / scene / series` | 稳定范围 |
| `quality_tier` | `draft / standard / hero` | 质量档位，直接影响成本 |
| `multi_subject_strategy` | `auto / joint / per_subject / hybrid` | 多人同框策略；同框 ≥2 人必须显式声明 |
| `interaction` | `none / contact / entangled` | 肢体接触强度 |
| `hand_precision` | `default / hero` | 手部是否为重点 |
| `text_render` | `none / simple / dense` | 画面内文字渲染 |
| `interpolation` | `none / standard / high` | 补帧档位 |
| `dubbing` | `none / auto / lipsync` | 配音；`lipsync` 还要求口型对齐 |

下游适配层应维护一份“逻辑能力 → 引擎工作流/节点图”的注册表。输入层只声明
**要多像、要多稳、要不要口型**，不声明模型名、节点名或工作流名。

替换/升级 ComfyUI 工作流时，只需要改下游注册表和工作流文件：

1. 在 ComfyUI 里搭好新工作流，导出 UI 格式；
2. 更新注册表里的工作流/构建器/模型映射；
3. 用引擎的节点信息接口做静态校验；
4. 不需要改故事层、提示词或 v10.2 契约。

**不支持的能力必须报错**（例如引擎不支持 `interpolation: high`），
不能悄悄按 `standard` 执行；否则就是契约明令禁止的静默降级。

---

## 7. 时间轴与剪辑

下游应把以下字段当作唯一时间事实来源：

| 字段 | 坐标系 | 用途 |
|---|---|---|
| `script.lines[].at_sec` | 全集绝对秒 | 台词 TTS / 字幕 / 口型 |
| `script.lines[].timing_sec` | 秒 | 预期时长 |
| `script.narration[].at_sec` | 全集绝对秒 | 旁白 TTS / 字幕 |
| `shot.start_sec` | 全集绝对秒 | 镜头起点 |
| `shot.duration_sec` | 秒 | 镜头时长 |
| `shot.end_sec` | 全集绝对秒 | 镜头终点 |
| `shot.narration[].at_sec` | 全集绝对秒 | 本镜旁白起点 |

剪辑规则：

- 每场切镜合计 = 剧本场时长（编译后 `gate` 容差 ±0.2 秒）；
- 全集合计 = 单集目标（编译后 `gate` 容差 ±0.2 秒；生成阶段另有 ±15% 的估算容差，编译会先归一）；
- 镜头之间必须连续覆盖剧本节拍，不能跳拍；
- 旁白是叠轨，不改变镜头时长；
- 口型只处理画内台词；画外台词不要求口型；旁白永远不要求口型。

---

## 8. 下游最小验收清单

### 8.1 只拿到产物时（ComfyUI 侧必须做）

- [ ] 严格解析 `plan / script / storyboard / director / story_assets`；
      未知 A 层字段必须报错；
- [ ] 确认 `script.scene.narration` 与 `storyboard.shot.narration` 都能被读到；
- [ ] 确认旁白没有出现在 `shot.audio`、没有 `speaking=true`、
      没有 `dubbing="lipsync"`；
- [ ] 确认每个场次第一镜的 `transition_in` 与 script 一致；
- [ ] 确认 `story_assets.json` 覆盖所有 `ref_characters`，并按
      `scene_refs[集号][scene_id]` 解析到真实地点资产（不要按同名 key 猜）；
- [ ] 确认每个启用画幅都有 `framing`，且镜头/场次/单集时长在契约容差内。

### 8.2 如果同时拿到本包源码（可选基线）

- [ ] 跑 `python -m manju.gate ... --strict` 与
      `python -m manju.drama ... --strict`，全部通过。

> 只拿契约产物的下游**不需要**安装本包，也不要求运行这两个命令；
> 它们用于输入层内部回归，不是接口的一部分。

### 8.3 启动 ComfyUI 后

- [ ] 选中一集，按 `plan.characters[].identity_lock` 生成定妆图；
- [ ] 按 `scene_refs` 找到地点资产后，用 `story_assets.scenes[资产].plate_prompt`
      （或 `scene_anchors`）生成/复用场景 plate；
- [ ] 按 `shot.prompt + framing + subjects + ref_characters` 生成首帧；
- [ ] 按 `motion + camera_move + duration_sec` 生成视频；
- [ ] 按 `audio` 生成台词 TTS，按 `narration` 生成旁白 TTS；
- [ ] 只对画内台词做口型；
- [ ] 字幕轨同时包含台词与旁白，且时间正确；
- [ ] 场间转场按 `transition_in.type` 处理，bridge 类没有多余特效；
- [ ] 抽查一条 `exposition` 和一条 `inner`，确认能听到、能看到字幕、
      且嘴型没有被驱动。

### 8.4 LTX 阶段化消费（v2.0 新增：单镜时长与 segments）

- **时长**：按 `shot.duration_sec` 换算帧数并**向上对齐 LTX 档位**
  `8n+1`：可用帧数 = `((int(duration_sec × fps) − 1) // 8) × 8 + 1`
  （25fps 下 2s→49 帧、3s→73 帧、4s→97 帧）。**不要用统一 3s 渲染**；
  时长不足或动作过多的镜（`duration_basis` 标注"不足（需拆镜/拆段）"）
  应先拆段/拆镜再渲染。
- **阶段化**：`segments` 非空时，每段经 **LTXVAddGuide** 按
  `frame_hint` 做中段关键帧注入（LTX 是唯一支持任意中间帧条件注入的
  本地管线）；`keyframe=true` 的段作为 guide 帧，`frame_hint`
  填目标帧号（`8n+1` 对齐）。无 `segments` 的镜按单段首帧+尾帧处理。
- **运镜**：`camera_phase` 继承整镜 `camera_move`，段级细分不改变
  "每镜必须有运镜"约束；LTX-2.3 `condition_only` 模式对推近/拉远
  响应弱，避免提示词与 `framing.shot_size` 景别冲突（见契约 §8.2 v10.2 例外）。
- **摄影配方（v10.4 / P0.5）**：`prompt` 结尾自带"光+镜"两件套（光源方向/
  质量/色温 + 焦段/景深）；`framing.camera_recipe` 可选结构化同款。
  渲染时若无明确光照词，**默认补平光是不被允许的**——缺光照词的镜
  应按 `prompt` 语境推导补光（或退回上游要求补写），不要让引擎出平光；
  焦段词与 `framing.shot_size` 冲突时以 `shot_size` 为准。

---

## 9. 常见问题

| 现象 | 排查 |
|---|---|
| 解析时提示未知字段 | 下游 schema 没有同步 v10.2 新字段；补齐后重跑 |
| 成片完全没有旁白 | 下游只读了 `audio`，没有读 `shot.narration` |
| 旁白被做了口型 | 误把 `narration` 当台词；旁白不设置 `speaking` / `lipsync` |
| 字幕时间对不上 | 混用了镜头相对时间和 `at_sec`；`at_sec` 是全集绝对时间 |
| 场与场之间仍然是硬切 | 没有实现 `transition_in`；bridge 类不需要特效，但需要读取 |
| 多人镜头串脸 | 检查 `requirements.multi_subject_strategy` 与 `max_faces_per_shot` |
| 首帧构图与分镜不符 | 优先信 `framing.subjects`，不要把 `prompt` 当唯一依据 |
| 单镜内容放不下（动作多、时间紧） | 检查 `duration_basis` 是否"不足"；拆段（segments）或拆镜后再渲染，不要硬塞 |
| 换工作流后能力静默降级 | 注册表没有声明"不支持"；必须显式报错 |

---

## 10. 版本与兼容

- **本文指南版本独立于契约版本**：ComfyUI 工作流、节点或模型升级，只更新本文，
  不改 `docs/漫剧文本输入契约_v10.2.md`。
- 新增的旁白/转场字段都是**可选字段**，旧产物没有它们时按空列表 / `null` 读取；
- `schema_version` 仍为 `v10.2`，不需要迁移脚本，也不需要升大版本；
- 新产物交给旧适配层时，可能出现两种失败：
  1. 严格 schema：未知字段直接报错（推荐，能立刻发现）；
  2. 宽松 schema：字段被静默丢弃（危险，必须避免）。
- 建议下游在加载器里显式检查 A 层未知字段，并在启动自检中打印
  “旁白/转场是否已启用”，避免静默漏实现。

---

## 11. 不在本包范围

本包不提供、也不打算提供：

- ComfyUI 客户端、`capabilities.json`、适配器、工作流 JSON；
- TTS / 口型 / 混音 / 补帧 / 显存管理；
- 具体模型名、节点名、分辨率桶、采样参数；
- `--plates` / `--run` 等渲染侧命令。

这些都属于下游实现层。本文档只规定**下游必须如何读取和消费契约**，
以及**哪些行为算静默降级**。
