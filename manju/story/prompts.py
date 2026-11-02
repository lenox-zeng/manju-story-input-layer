"""提示词层：阶段规则、系统提示词、输入 payload 与定向摘录。

与 stages.py 的关系：stages 的生成方法调用本模块构建「提示词 + 输入」；
本模块只依赖数据模型与契约 schema，不依赖 stages / repairs。

改动生成口径（规则文案、摘录策略、payload 结构）只动本模块；
涉及指纹递增时见 stages.py 顶部 STORY_RULES_REV / PROMPT_POLICY_REV 说明。
"""
from __future__ import annotations

import re
from typing import Any

from pydantic import BaseModel

from ..schemas import describe
from .episodes import EpisodeSlice
from .models import (
    Art, Cast, Outline, ScriptEpisode, ScriptParams, StoryCut, StorySegment,
)
from .validate import _event_key

def _prompt_lang_label(prompt_language: str) -> str:
    """给提示词字段用的语言标签。默认英文（与历史行为一致），可配中文/任意。"""
    return {
        "en": "英文",
        "zh": "中文",
        "any": "当前引擎接受的语言（中/英均可）",
    }.get(prompt_language, "英文")


def _system(role: str, task: str, model_cls: type[BaseModel], rules: str,
            prompt_language: str = "en") -> str:
    # prompt/negative_prompt/desc 类字段都遵循同一语言配置，不只是 prompt_en。
    lang_rule = {
        "en": "中文内容写中文，所有 prompt/negative_prompt/desc 类字段写英文。",
        "zh": "中文内容写中文，所有 prompt/negative_prompt/desc 类字段写中文"
              "（当前引擎接受中文提示词）。",
        "any": "中文内容写中文，所有 prompt/negative_prompt/desc 类字段的语言"
               "按当前引擎能力自由选择。",
    }.get(prompt_language, "中文内容写中文，所有 prompt/negative_prompt/desc 类字段写英文。")
    return "\n\n".join([
        f"# 你的角色\n{role}",
        f"# 你的任务\n{task}",
        "# 输入\n用户消息是一段 JSON。只依据它工作。不要臆造输入之外的事实。",
        f"# 你必须输出的 JSON 结构\n{describe(model_cls)}",
        f"# 铁律\n{rules}",
        "# 输出要求\n直接输出一个 JSON 对象（第一个字符必须是 `{`，最后一个字符必须是 `}`）。"
        "严禁在 JSON 前后输出任何思考、分析、计划、草稿、复述或说明文字，严禁 markdown 围栏——"
        "这类内容会让 JSON 解析失败并整轮重跑。"
        f"字段名必须与结构完全一致；{lang_rule}",
    ])


BIBLE_RULES = """
1. 只改编输入小说；保留核心冲突与人物关系，删掉无法在竖屏短剧里拍出来的支线。
2. 这是**全剧圣经**，不要输出分集梗概（episodes 由后续按批生成）。
3. 场景表 5-12 个（95% 的戏在 3-5 个主场景内），道具 3-8 件且都必须在剧情中真正使用。
4. 角色 1-6 人；给每个角色一个能在画面上一眼区分的轮廓/发型/服装颜色。
5. 爽点 beats：至少 1 个 major，major 不能只落在最后一集；任意连续 3 集必须至少有 1 个爽点。
   episode 字段按 params.episodes 分配。
6. spine 六字段必须具体、可拍、互相因果；power_rule 写清主角凭什么翻盘、边界和代价。
7. genre 不要写“爽文”“爆款”这类空词；写“末世热血武道·少年成长”这种可执行类型。
8. 场景/道具/角色 id 必须唯一；后续每一集只能引用这里已有的 id。
"""


EPISODE_RULES = """
1. 只输出本批要求的集号，一集一条，不得增删、不得改集号。
2. 每集 synopsis 用叙述体（100-180 字），禁止出现引号台词；
   hook 是第一秒的视觉钩子，cliff 是结尾悬念，两者都必须具体可拍。
3. scene_ids/character_ids/prop_ids 只能引用 bible 中已有的 id；只写本集真正用到的。
4. 每集 5-8 场的戏剧容量：20 秒内主角做出一次不可逆的主动选择；
   中段有一次真实失败/代价；结尾落到 cliff。场次按戏剧单元合并，
   不要把一个完整事件拆成几场“假换景”。
5. 任意连续 3 集至少 1 个爽点；爽点归属以 bible.beats 为准，不要新造。
6. 本集出场 ≥3 人时必须写 crowd_plan（群演怎么摆、是否参与对话）。
7. 集与集必须连续：上一集 cliff 要在本集有回应；本集 cliff 要能钩住下一集。
8. 不要写镜头/构图/提示词，只写剧情事实。
9. must_show 是本集必须拍出来的 2-6 个具体事件，按发生顺序，写可见动作/
   决定/物件变化，不写心理活动或抽象主题；剧本层会逐条对账，缺一即失败。
   synopsis 只围绕这些事件写因果链（A 导致 B，B 付出代价，代价引出 C），
   不要把切片里的事件逐条罗列——那会把 90 秒撑成流水账。
10. key_props 写本集承担转折的道具（小说原名，0-3 件）；props 表里必须有对应
    道具，剧本会给每件安排 insert 特写。
11. 如果输入已经给出 series_design（该集的原子区间/必拍事件），以它为准：
    不得增删 must_show，不得把两集的情节并到一集。集数由情节决定，
    不按字数或章节数。
"""


SERIES_DESIGN_RULES = """
1. 你拿到的是按原文顺序编号的情节原子（atom）。先通读，识别“戏剧单元”：
   一个单元 = 目标 → 阻力 → 转折 → 代价/高潮 → 新问题（cliff）。
   按戏剧单元分集，**不要按字数、章节数或固定 9000 字/2 章切**。
2. 每集 atom_ids 必须是连续区间，按顺序覆盖它所包含的原子；所有集合起来
   必须恰好覆盖 1..atom_count 每个原子一次，不能跳段、重叠、倒序或漏尾。
   如果拿不准边界，宁可把转折原子完整放进前一集或后一集，也不能留空档。
   每个原子只能归属一集，不能重复引用。
3. 每集 must_show 写 2-6 条**必须让观众看见**的完整事件单元（可见动作、决定、
   物件变化、关键揭露），按发生顺序，并构成一条因果链：起因 → 行动 → 阻力 →
   转折 → 代价/结果。**每条都是一个讲完整的事件**（为什么发生、谁做了什么、
   结果怎样），不是互不相干的场面。抽象主题、内心活动、背景交代不算。
   一个完整戏剧单元可以单独成集：**哪怕只有 400-500 字的关键片段，只要它
   讲得完整清楚，就独立成集**（删减自由，完整性优先）。
4. key_props 选 0-3 件本集真正推动剧情的道具（用小说里的原名）；每件都要
   在 must_show 或 synopsis 里实际被使用/看见，不能只是摆设。
5. dramatic_question 一句话写观众这一集在等什么答案；turn 写什么改变/被揭露；
   climax 写情绪最高点的具体画面；cliff 写结尾停在哪一个未解决的画面上。
6. 集与集必须连续：下一集开场要直接接住上一集 cliff 的后果。
7. 不得发明小说里没有的关键事件或人物；改编可以合并/压缩，但 must_show 必须
   能在原文原子中找到依据。
8. 集数由内容决定：一个完整戏剧单元一集；不要为了凑集数硬拆，
   也不要把两个单元塞进一集。删减发生在“这集讲哪些事件”的选择上，
   选中的事件必须演完整。
9. synopsis 用叙述体 60-140 字，**允许引用 1-2 句关键台词（带引号）**；
   每集 title 具体可辨识。
10. estimated_seconds 是这一集讲完 must_show 所需的自然时长（45-240s 由内容
    决定，不按字数均分）：关键短事件独立成集时允许明显小于 90s；若超过 240s，
    说明该集内容过多，应拆分或删减。
11. key_lines 选 1-4 句本集**必须原样保留**的关键台词（推动转折/揭示真相/
    情绪爆点的原文对话，逐字照抄，带引号）。剧本阶段会把它们一字不改落进
    节拍——所以只选真正不能被改写的那几句。允许本集以这些关键对话为主体
    （纯对话集）：关键对话密集时，一集可以主要是对话，只要每句有可见动作/
    反应支撑。
"""


def _cast_rules(sp: ScriptParams) -> str:
    lang = _prompt_lang_label(sp.prompt_language)
    if sp.prompt_language == "en":
        neg = "image.negative_prompt 英文；"
        voice = "英文音色描述"
    else:
        neg = "image.negative_prompt 与提示词同语言（英文负面关键词也可以直接用）；"
        voice = "你的 TTS 引擎接受的音色描述"
    return f"""
1. 只输出大纲 characters 里出现的角色，id 必须一一对应，不能增删。
2. importance：主角 protagonist，主要对手/伙伴 major，其余 supporting/minor。
3. identity_lock 是跨集不变的脸/发型/瞳色/体型/永久标记；服装写进 wardrobe_states，不要写进 identity_lock。
4. wardrobe_states 的 state_id 必须是 `<角色id>-<英文小写标签>`，如 C1-default、C1-injured。
5. image.prompt 是{lang}全身/半身定妆提示词：只写人物外貌与服装，禁止出现角色姓名、场景、背景、镜头词。
6. {neg}去掉 low quality / extra fingers / deformed face / text / watermark。
7. voice：主要角色必须给 timbre 与 prompt（{voice}）；按年龄、身份、气质区分。
8. persona 写中文；relationships 每项用 {{"with":"C2","relation":"..."}}。
9. 【表演一致性·P1】每个主要角色（主角/重要配角）必须给 pose_library（3-5 个
   命名姿态英文短语）与 expression_library（3-5 个命名表情英文短语）：短语要
   **一眼可识别、跨镜逐字复用**（如 "hands clasped at chest" / "brow furrowed,
   lips tight"），禁止空泛（"happy"/"sad"）——分镜层引用角色表演时从这两个
   库里取短语或 id，保证跨镜动作与表情一致。
"""


def _art_rules(sp: ScriptParams) -> str:
    lang = _prompt_lang_label(sp.prompt_language)
    if sp.prompt_language == "en":
        neg = "negative_prompt 必须包含 people, person, figure, human。"
    else:
        neg = ("negative_prompt 必须明确排除人（people/person/figure/human，"
               "或你的模型认得的对应词）。")
    return f"""
1. 场景/道具 id 必须与输入大纲完全一致，不能增删。
2. 每场 3-5 个 anchors（可复用的空间锚点：门、桌、窗、梁、灯），用中文写。
3. 每个场景至少 1 个 lighting 状态；prompt 必须是{lang}。
4. image.prompt 是{lang}**空景**提示词：环境、材质、光线、纵深，明确 no people / empty；
   {neg}
5. 场景 prompt 禁止出现任何角色姓名。
6. 道具 anchors 3-5 个；states 至少 1 个；prompt 必须包含尺度短语
   （手持级=handheld、桌面级=tabletop、家具级=furniture-level）；negative 包含 hand, fingers。
7. 同一空间的不同时间/状态用 variant_of + changes 表达，不要复制成新场景。
"""


def _script_rules(sp: ScriptParams, *, dialogue_light: bool = False,
                  suggest_lines: int | None = None) -> str:
    line_limit = min(sp.max_line_chars,
                     max(1, int(sp.max_shot_seconds * sp.chars_per_second)))
    if suggest_lines is None:
        suggest_lines = 4 if dialogue_light else 9
    return f"""
1. 一集 1-4 个完整戏剧单元（场），场数由内容决定：一个完整事件单元演完再切，
   不要为了凑场数把一场戏拆碎；允许整集只有一场戏（纯对话集）。
   每场 3-12 拍，beats 是“动作/台词”交替的可拍节拍；每一拍只能二选一：
   {{"action":"中文动作叙述"}} 或 {{"speaker":"C1","line":"中文台词","delivery":"语气"}}。
2. 动作节拍必须是具体可见的动作链（谁对谁做了什么 + 环境反应），不能写心理活动；
   动作文字里禁止出现引号台词。
3. 单句台词 ≤{sp.max_line_chars} 字（含标点；引擎口型/单镜能力）；为了让分镜能装进
   {sp.max_shot_seconds:g}s 单镜，请按 **≤{line_limit} 字**写，超过 {line_limit} 字
   会被确定性拆成两拍。**台词句数不设硬上限**：本集建议 {suggest_lines} 句只是参考，
   关键对话密集时允许更多（纯对话集没问题）；**payload.episode_plan.key_lines 里的
   台词必须逐字一字不改落进 beats**：不允许改写/删减/转述/润色。整句超过
   {line_limit} 字时，拆成**连续两拍**（第 1 拍前半句、第 2 拍后半句，中间可夹
   动作/反应拍），观众听到的仍是原文完整一句；拆拍后每拍 ≤{line_limit} 字；
   台词是动作的延伸，不是解释。
4. 台词只允许画内说话：speaker 必须在本场 characters 里。
   需要交代画面外的必要前情/世界规则/时间跳跃，或角色关键内心时，
   写进 scene.narration（不占节拍、不做口型、不写成 speaker+line 的 VO 台词）：
   - kind=exposition：说话人固定 NARRATOR，只补画面无法交代的信息；
   - kind=inner：speaker 必须是本场 C# 角色，只在该角色有可见反应/抉择时使用。
   旁白不得复述画面已经看见的动作，不得用来解释台词。
5. 场景 beat 只能取：钩子/激励/抉择/受挫/转折/代价/高潮/收束/结尾钩子。
6. 20 秒内必须有一场 beat=抉择；15-60 秒内必须有一场 beat=受挫或代价（cost 非空）；
   最后一场必须是 结尾钩子。
7. hook_beat 必须是 [场序号, 拍序号]，且落在全集前 3 拍内。
8. 每场必须写 goal/obstacle/turn/cost/information；cost 没有就写“无”。
9. 场景 id/角色 id/道具 id/服装 state_id 必须来自输入，不能新造。
   场景 id 只能取 payload.allowed_scene_ids；同一地点 id 可以在本集多场重复使用
   （场次由数组顺序区分），但绝不能自行往后编号出 S6/S7/S8。
10. 时长由 beats 决定：动作拍按 {sp.action_seconds:g}s、
    台词按 字数/{sp.chars_per_second:g} 秒（标点计入）。
    总时长目标 ±15%，并严格遵守 payload.budget 给出的拍数上下限。
11. 每场至少 1 个动作/反应拍；纯对话集可以以对白为主，但每场必须有可见
    动作支撑（表情、转身、手势、道具交互），不许全程呆站念白。
12. beats_claimed 填本集实际使用的 outline.beats 的 id（可空）。
13. 【镜头预算】分镜契约是“一镜一拍”，本集节拍总数必须落在
    payload.budget.min_total_beats 到 max_total_beats 之间。宁可多加一个
    短动作拍，也不要少写；少于下限会在分镜阶段直接失败。
14. 【覆盖率规划】每个 beat 必须写 coverage，取值为
    establish/action/reaction/insert/pov/dialogue/transition：
    establish 交代空间与人物相对位置；action 推进动作；reaction 写角色对上一拍的
    可见反应（表情/身体/后退/握紧）；insert 写道具或环境细节特写（手、门把手、
    鞋、火光）；pov 写角色主观看到的画面；dialogue 承载台词；transition 交代
    时间/空间转移。全集覆盖数量必须达到 payload.budget.coverage_required
    （reaction/insert/establish/pov 是硬配额），并且这些拍要真的能拍成对应镜头：
    - reaction 拍：动作文字只写“谁对什么产生什么可见反应”，不写新事件；
    - insert 拍：只写一个道具/局部细节，角色可以只在画外；
    - establish 拍：只交代空间、距离、站位，不推进冲突；
    - pov 拍：以某角色的视线方向写他看见的东西。
    同一 coverage 不要连续超过 3 拍；纯台词拍写 coverage="dialogue"。
15. 【情节对账·硬性】payload.episode_plan.must_show 是本集必须让观众看见的
    具体事件。每一条都必须落到至少一场：在该场 major_event 里原样抄写该条
    （一字不改）。major_event 只能取 must_show 里的条目；纯反应/过渡场可留空，
    但不得为了凑场次新造 major_event。
16. 【场间因果·硬性】第 2 场起每场必须写 causal_from：上一场的哪个结果/选择/
    物件变化导致了本场。禁止“上一场还在屋里，下一场无因站在街上”的跳场。
    第 1 场写“承接上集结尾”或“开场建立情境”。
17. 【关键道具·硬性】payload.episode_plan.key_props 里的每件道具，本集必须
    真正出场（写进该场 props），并至少有一拍 coverage="insert"、
    emphasis="key_prop"、visual_focus=道具名（只拍道具/手/局部，角色可在画外）。
18. 【关键镜头·硬性】本集转折/高潮/关键信息拍必须标 emphasis：
    highlight=关键转折或新信息；climax=情绪最高点；key_prop=关键道具特写。
    这些拍必须写 visual_focus（镜头要让观众看清什么），分镜层会强制非静态运镜；
    普通过场标 none。不要给每一拍都标 emphasis。
19. 【节奏分配】must_show 是这一集的骨架，不是一张打卡清单。每条 must_show
    至少给 2 个连续可拍的拍（行动/决定 + 可见反应或后果），转折与 climax 给
    3-4 拍；同一事件的动作、反应、道具细节放在同一场演完，再进入下一事件。
    atom 区间里没进 must_show 的原文内容用一句台词、一个反应或一个 insert 带过，
    不要求逐条覆盖。**禁止每条 must_show 只给 1 拍就跳到下一件事**——观众必须
    看清“为什么发生、谁做了什么、结果怎样”。
20. 【旁白·硬性】scene.narration 是可选数组，每条含 kind/text/beat/speaker。
    - 只叠在动作拍上（beat 必须指向本场一个非台词拍）；旁白不增加时长，也不做口型。
    - exposition 只补画面外的前情/规则/时间跳跃，每集 ≤payload.budget.narration_limits.exposition；
      inner 只补关键抉择/情绪转折，每集 ≤payload.budget.narration_limits.inner。
    - 旁白是盐，不是主食：能用一个动作/反应/道具特写说清的，不要写旁白；
      更不要把旁白写成“他走进来，她看着他”这种看图说话。
    - 对白稀薄的段落，优先用可见动作链 + reaction/insert 拍承载因果，
      再用 1-2 条必要旁白补齐观众必须知道、但画面无法交代的信息。
21. 【场间转场·硬性】第 2 场起每场写 transition_in：type 取
    match_cut/sound_bridge/action_bridge/time_jump/fade/dissolve；
    match_cut/sound_bridge/action_bridge 必须写 anchor（上一场与本场共享的
    物件/动作/声音）；time_jump/fade/dissolve 写时间或空间落点。
    优先用 match_cut/sound_bridge/action_bridge 做有机衔接，不要无动机硬切；
    转场锚点必须在上一场和本场都真实存在，不能凭空发明。
"""


STAGING_RULES = """
1. 为输入剧本的每一场输出一个 StagingScene，scene 序号、scene_id 必须一一对应。
2. locations 给出每场可用的 2D 平面：items 的 plan=[west,north,east,south]，0-1 归一化；
   物件是桌子/床/门/柜台等可站可坐的东西。
3. places 必须覆盖本场每个 characters 恰好一次；present=false 表示在画外。
4. item/side 二选一：站/坐在物件上写 side="on"；站在物件旁写 north/east/south/west。
5. along 0-1 表示沿物件边的位置；posture 写 sit/stand；facing 写八方向。
6. extras > 0 时必须写 extras_where。
7. 站位要服务构图：主角不要永远居中；对话双方尽量形成左右/前后关系。
"""


def _board_rules(sp: ScriptParams) -> str:
    min_shot = sp.min_shot_seconds
    max_shot = sp.max_shot_seconds
    lang = _prompt_lang_label(sp.prompt_language)
    posture_lang = {
        "en": "英文",
        "zh": "中文",
        "any": "引擎可读的任意语言",
    }.get(sp.prompt_language, "英文")
    if sp.max_faces_per_shot is None:
        face_rule = ("12. 同框可锁正脸数不设引擎上限；仍按构图与叙事需要安排正反打/过肩/"
                     "背影，不要把所有人挤在一个画幅里。")
    else:
        face_rule = (f"12. 当前引擎每个画幅最多 {sp.max_faces_per_shot} 张可锁正脸；"
                     "超出时用单人反打/过肩/背影/局部特写拆开。"
                     "只有全员背身或剪影、确实不需要锁脸时，"
                     "才给该 cut 置 allow_multi_face=true。")
    if sp.camera_motion == "limited":
        camera_rule = ("camera 只能取给定枚举；**每镜必须有运镜**："
                       "Static Shot 不得超过 60%，动态机位限 Push In/Tilt/Pedestal "
                       "等轻度运动，禁止 Orbit/手持/剧烈摇移（引擎防形变）。")
    else:
        camera_rule = ("camera 只能取给定枚举；**每镜必须有运镜**："
                       "Static Shot 不得超过 40%，定场/反应镜用缓慢推近或轻摇代替"
                       "纯静止；推拉摇移跟环绕均可。")
    return f"""
1. 每条 cut 必须认领本场连续的一段 beats：[起, 止]（含两端）。同一场的 cuts 必须
   按顺序、不重不漏地覆盖该场全部节拍。
2. 切镜时长必须在 {min_shot:g}-{max_shot:g}s；非 reaction/insert 的单镜
   尽量 ≥{min(max_shot, 2.2):g}s；单镜只容纳一个动作节拍，
   不得把未完成的关键动作截断跳走。
   **时长按内容算**（三值取大）：秒数 ≥ max(景别下限[特写1.5/中景3/远景4]s,
   本镜动作节拍数×{sp.action_seconds_per_beat:g}s, 台词字数/{sp.chars_per_second:g}s
   + 句数×{sp.pause_seconds_per_line:g}s)；计算过程写进 duration_basis 供复核。
   动作数超过 {('引擎上限 '+str(sp.max_actions_per_shot) if sp.max_actions_per_shot else '不设')}
   时**必须拆镜或拆段（segments）**，不得硬塞。
3. size 只能取 extreme-wide/wide/medium/close/extreme-close；全集至少 2 种景别；
   连续 3 镜不能同景别且同 coverage。
4. {camera_rule}关键拍按第 21 条必须有运镜。
5. coverage 只能取 establish/action/reaction/insert/pov/dialogue/transition；
   **每个 cut 的 coverage 必须采用它认领的第一个拍的 beat_plan.coverage**
   （剧本层已经完成覆盖率规划，分镜层不得改类；认领多拍时取首拍）。
   cut_reason 只能取 action_done/reaction/new_info/pov/sound/space/time/match_cut。
6. prompt_en 是{lang}**首帧画面**：主体 + 位置 + 动作瞬间 + 光线 + 情绪。
   每镜必须包含：动作动词 + 情绪/表情 + 至少一个可辨识细节（道具/手势/空间关系）；
   **不要写运镜词**（tracking shot / push in / pan / tilt / dolly / camera move 等一律禁入
   prompt_en——运镜只写在 motion/camera 字段，下游门会把运镜词当 block 拦下）；
   不要写画风词（cinematic / film still / 8k）。只写这一帧看得见的内容；
   角色用外貌特征指代，不写角色姓名。
7. characters/props 必须是本场剧本里出现过的 id；subjects 必须覆盖 characters，每个角色
   给{lang} pose 和 expression。
8. frame 用中文写同一帧（供人工核对），shot/motion 写这几秒内的动作。
9. 每个切镜的 seconds 必须装得下它认领的台词
   （字数/{sp.chars_per_second:g} 秒）。
10. 声音只写现实音效（sfx）；不要写配乐。旁白由剧本层 scene.narration 提供，
    不要在本层改写或塞进 sfx；但有旁白的拍，画面必须为它提供可见的承载信息。
11. 每次只输出**本场一个 segment**：id 用输入给定的 segment_id，scene_index=本场序号；
    所有 cut 只能属于这一场。
{face_rule}
13. 每个 cut 必须写 state_out：
    where/facing 用输入 cast 里的八方向词表；posture 写{posture_lang}；
    wardrobe 用该角色在 subjects.wardrobe_state 里的 state_id；
    props 写每件道具在谁手里（无主写 "scene"）；injuries 一旦出现，后续 cut 必须延续。
14. 从输入的 state_in / previous_cuts 接着写：不得瞬移、不得无故换装、不得凭空丢道具或伤势。
15. 位置跳变必须有 space/time/match_cut 理由；双人左右关系不得翻转（180° 跳轴）；
    双人对白/反应镜的 facing 必须看向对方；景别不得连跳 3 级，
    除非 cut_reason 是 new_info/pov/reaction/sound。
16. 【动作完整性】每镜必须完整呈现一个可独立辨识的动作节拍（起点→过程→结果），
    用一句话能说清"谁、做了什么、导致什么"；不得把一段未完成的关键动作截断跳走。
17. 【因果衔接】相邻镜必须共享动作因果——上一镜 action_done 后，下一镜必须给出该动作的
    可见承接（对象出现/反应/空间变化）；不得凭空开启与上一镜无关的新动作。需要交代的
    空间移动（A 走向 B 等）必须补过渡镜，不得直接把角色瞬移到新位置。
18. 【承接续写】镜 N 的起始状态 = 镜 N-1 的 state_out（where/facing/posture/wardrobe/props），
    把"从上一镜结束处接着发生什么"写进本镜 prompt_en 起始；prompt_en 不只是孤立首帧，
    要体现与上一镜的画面连续性（动作/位置/光线不跳变）。
19. 【空间与机位纪律】每场先给一个关系镜头（establish/双人中景）交代角色相对位置，再切局部；
    对话用 主镜头→过肩→反打→反应 结构；本场机位保持在 180° 轴线一侧不得越轴；
    角色屏幕方向（左/右）与出画入画方向保持稳定；双人对白/反应的 facing 必须看向对方。
20. 【单一职责】每镜只完成一个动作节拍，不把多个节拍塞进一镜。若一个节拍确实
    需要多机位，不得在本层把一拍拆给多镜（会违反“一镜一拍”），而应在剧本层
    把它拆成多个可独立拍摄的拍；本层仍严格一镜一拍、不重不漏。
21. 【关键镜头·硬性】beat_plan 里 emphasis=key_prop 的拍：景别必须是
    close/extreme-close，camera 不得用 Static Shot（用 Push In/Zoom In/Tilt 等），
    画面主体是道具/手/局部。emphasis=highlight/climax 的拍：camera 不得用
    Static Shot，必须用推/拉/摇/移等有方向运动的机位。代码会再次强制，
    但你的画面描述必须真的对准 visual_focus。
22. 【阶段化子分镜 segments】动作节拍数 >1 的镜**必须**输出 segments 数组
    （1–3 段，一段一个主动作，段秒数和 = 整镜秒数，段区间连续不重叠）：
    - text 写该段**自己的**英文画面句（单一动作/状态，别写跨段多事件）；
      **每段 text 必须包含本段主动作的英文动词句，禁止把整镜 prompt 开头整段
      复制进段 text，禁止同一镜的 2-3 段 text 内容相同**——段与段按动作推进
      区分（start=起始状态+动作起点，mid=动作过程，end=动作完成+结果承接）；
    - beat 写中文动作要点；duration_sec 该段秒数（≥动作数×{sp.action_seconds_per_beat:g}s）；
    - state_out → 下一段 state_in 承接（段级不许动作断裂/瞬移）；
    - camera_phase 继承整镜 camera_move（运镜约束不变）；
    - 台词只影响该段时长，**词一字不改**（key_lines 约束不变）；
    - 末段 keyframe=true + frame_hint="last"（供 Seedance 尾帧串联 /
      LTX 中段注入；单动作镜省略 segments）。
23. 【摄影配方·硬性】每镜 prompt 结尾必须写明摄影配方，两件套缺一不可：
    - **光**：光源方向（screen-left/right/behind/top/rim）+ 质量（hard/soft/diffused）+
      色温冷暖（cool grey daylight / warm lamplight / dusk orange），如
      "light from screen-left window, soft grey daylight, cool";
      **不给光照 = 引擎默认平光**，平光是画面"假/平"的第一根因；
    - **镜**：焦段 + 景深（50mm shallow depth of field / 35mm deep focus /
      85mm compressed），长焦拍面部压迫感、广角拍空间纵深感；
    - 配方只描述本镜摄影事实，**不得**与画风唯一来源（美术圣经 style_keywords/
      do_dont）冲突，也不得写成运镜词（运镜只在 camera_move）。
24. 【锚点 verbatim·硬性】prompt/segments 里出现 bible 角色时，该角色的
    identity_prompt/identity_anchor 中**可一眼识别的锚点短语**（发型/疤/服饰色块/
    道具绑定）必须**逐字复用**，禁止自行改写、缩写或替换同义词——
    跨镜跨集只有逐字复用同一锚点短语，渲染层才能锁定同一个人；
    场景/道具同理复用场景 anchors 与道具 scale 短语（灯光状态可随剧情变化，
    但形状/材质/年代的锚点短语不得改写）。
"""


def _edl_rules(sp: ScriptParams) -> str:
    lang = _prompt_lang_label(sp.prompt_language)
    face_rule = (
        "8. 同框可锁正脸数不设引擎上限；仍按叙事需要安排正反打/过肩/背影。"
        if sp.max_faces_per_shot is None else
        f"8. 每个画幅最多 {sp.max_faces_per_shot} 张可锁正脸；超出时用单人反打/"
        "过肩/背影/局部特写拆开。只有全员背身或剪影、确实不需要锁脸时，"
        "才给该 cut 置 allow_multi_face=true。")
    if sp.camera_motion == "limited":
        camera_rule = ("camera 只能取给定枚举；**每镜必须有运镜**："
                       "Static Shot 不得超过 60%，动态机位限 Push In/Tilt/Pedestal "
                       "等轻度运动，禁止 Orbit/手持/剧烈摇移（引擎防形变）。")
    else:
        camera_rule = ("camera 只能取给定枚举；**每镜必须有运镜**："
                       "Static Shot 不得超过 40%，定场/反应镜用缓慢推近或轻摇代替"
                       "纯静止；推拉摇移跟环绕均可。")
    return f"""
1. cuts 数组必须按顺序、一镜一拍覆盖本场全部剧作拍（数组第 N 项就是第 N 拍），
   不要合并、不要拆分、不要漏拍。
2. 每项只输出导演决策字段：size / camera / characters / props / cut_reason /
   sfx / state_out / allow_multi_face。**不要输出** seconds、coverage、frame、
   prompt_en、motion、purpose、information、beat_goal、subjects、lens 等
   —— 这些由代码按剧本拍确定性地推导。
3. size 只能取 extreme-wide/wide/medium/close/extreme-close；全集至少 2 种景别。
4. {camera_rule}关键拍按第 12 条必须有运镜。
5. characters 只能取本场人物 id；props 只能取本场道具 id。insert/establish
   可省略 characters（代码会按动作线主体/场次主角补全）；characters 决定同框人脸数。
6. cut_reason 只能取 action_done/reaction/new_info/pov/sound/space/time/match_cut；
   同一 coverage 不要连续 3 镜且同景别。
7. 每镜 state_out 只写本镜结束时的**变化**：where/facing 用受控方向词表，
   posture 写{lang}，wardrobe 用服装 state_id，props 写道具持有者（无主写 scene），
   injuries 一旦出现后续必须延续。无变化可省略（代码会沿用上一镜）。
   道具只有在本镜 props 里出现时才要求持有者在画内；未展示的既知道具可以
   继续由画外角色持有，这是合法的世界状态，不要为了“让它在画内”硬塞人物。
   {face_rule}
9. 从 state_in / previous_cuts 接着写：不瞬移、不无故换装、不凭空丢道具/伤势；
   双人左右关系不得翻转（180° 轴线）。双人对白/反应镜的 facing 必须指向
   对方（同向追跑/跟拍除外）。
10. 优先采用 action_line 每拍的 suggested_shot / who / where / facing；
    本层只补景别、机位、道具与状态，不重新设计动作。
11. 每镜完整呈现一个动作节拍（起点→过程→结果），不截断关键动作。
12. 【关键镜头·硬性】beat_plan 的 emphasis=key_prop 拍必须是 close/extreme-close，
    且 camera 非 Static Shot；emphasis=highlight/climax 拍不得用 Static Shot。
    代码会再次强制，但你的机位选择要与 visual_focus 一致。
13. 【旁白承载】beat_plan 某拍的 narration 非空时，本镜仍只拍这一拍的动作；
    画面要给旁白留出可承载的视觉信息（表情/物件/空间），不要改成口型镜，
    也不要把旁白文字写进 sfx。旁白由剧本层提供，本层不改写。
14. 【场间转场】payload.transition_in 非空时，本场首镜的 cut_reason 必须能接住：
    match_cut→match_cut、sound_bridge→sound、action_bridge→action_done/space、
    time_jump→time、fade/dissolve→time/space/new_info。代码会兜底，但画面要真的
    共享 anchor 里的物件/动作/声音，而不是硬切到新场。
"""


def _prompt_rules(sp: ScriptParams) -> str:
    lang = _prompt_lang_label(sp.prompt_language)
    subject_rule = ("角色用位置/动作/代词指代（如 the woman on the left），不写角色姓名。")
    identity_rule = (
        "角色身份由 ref_characters 参考图与 story_assets 的 identity_prompt /\n"
        "   identity_anchor 提供。prompt_en **逐字复用**（verbatim）该角色 identity_anchor\n"
        "   中可一眼识别的锚点短语（发型 / 疤 / 服饰色块 / 帽 / 项圈 / 道具，如 高尖蓝帽、\n"
        "   红绳项圈、银星白袍），同一角色跨镜锚点短语不得改写，保证渲染层锁定同一张脸；\n"
        "   **不写姓名**、不展开脸型身材。只写动作瞬间、姿态、表情、视线、位置、场景与光线；\n"
        "   同框外形相近时用位置/朝向/景别区分；本镜服装若改变，只写改变后的衣物。")
    if sp.camera_motion == "limited":
        motion_scope = ("引擎不支持大幅运动：运镜限 Push In / Tilt / Pedestal 等轻度运动，"
                        "**禁止** Orbit / 手持 / 剧烈摇移。")
        static_note = "Static Shot 不得超过 60%，动态机位用轻度运镜（Push In/Tilt/Pedestal）"
    else:
        motion_scope = ("运镜幅度按 cut_plan.camera：Push In / Orbit / Tracking 等写清方向与节奏；"
                        "情绪/关键镜用推近或环绕。")
        static_note = "Static Shot 不得超过 40%，定场/反应镜用缓慢推近或轻摇代替纯静止"
    return f"""
1. prompts 数组必须与输入的 cut_plan.cuts **一一对应、数量相等、顺序一致**；
   第 N 项就是第 N 个切镜。
2. 每项只输出 prompt_en 与 motion 两个字段，不要输出其他字段。
3. prompt_en 是{lang}**首帧画面**：主体 + 位置 + 动作瞬间 + 光线 + 情绪。
   每镜必须包含：动作动词 + 情绪/表情 + 至少一个可辨识细节（道具/手势/空间关系），
   信息量要具体到能直接画出这一帧，不要写空泛的“两人对视”。{subject_rule}
   **prompt_en 禁写运镜词与拍摄调度词**（tracking shot / push in / pan / tilt / dolly /
   camera move 等运镜词，以及 dialogue shot / reaction shot / insert shot /
   point-of-view shot / establishing shot / two-shot / over-the-shoulder / POV 等
   拍摄类型与调度词一律禁入——景别、镜头类型、机位、视角由 framing/coverage 字段
   表达，prompt_en 只写这一帧看得见的画面内容；下游门对 prompt 里的运镜词直接 block，
   对调度词 warn）。
4. motion 是{lang}画面内动作（进行时）：写这几秒内看得见的动作 + **镜头运动（运镜）**。
   **每镜必须有运镜**，不得写纯静态机位；{motion_scope}
   motion 与本镜 prompt_en 的首帧衔接，不塞入多个事件。motion 不得为空。
5. {identity_rule}
6. 一个切镜只写一个动作瞬间；不要新增 cut_plan 之外的人物、道具或事件。
7. 不要复制定妆照的站姿、背景或打光；本镜的站位、动作、场景与光线以 cut_plan /
   staging 为准。
8. cut_plan 若给 emphasis=key_prop / visual_focus，prompt_en 必须把该道具/局部
   写成画面主体（可只拍手、鞋、门缝、信纸），motion 只写道具或手部变化；
   emphasis=highlight/climax 时 motion 要写出推动情绪的动作，不得只写静态表情。
9. 【对话镜·硬性】cut_plan 的 coverage=dialogue 或该镜是台词拍时，prompt_en
   必须给出说话人的视线/表情/手势（“说话”本身是可拍动作），motion 写说话时
   的手势与对方的可见反应；不得把对话镜写成两人呆站。
10. 【摄影配方·硬性】prompt_en 结尾必须写明摄影配方两件套，缺一不可：
   - 光：光源方向（screen-left / screen-right / behind / top / rim）+ 质量
     （hard / soft / diffused）+ 色温冷暖（cool grey daylight / warm lamplight /
     dusk orange），如 light from screen-left window, soft grey daylight, cool；
     不给光照 = 引擎默认平光，平光是画面假、平的第一根因；
   - 镜：焦段 + 景深（50mm shallow depth of field / 35mm deep focus /
     85mm compressed），长焦拍面部压迫感、广角拍空间纵深感；
   配方只描述本镜摄影事实，不冲突美术圣经 style_keywords / do_dont，也不写成
   运镜词（运镜只在 motion / camera_move）。
11. 【分段结构·硬性】本镜若含多个动作节拍（动作 >1），prompt_en 的动作从句
   按节拍顺序用逗号分句组织，每一节拍一个独立从句（skidding … / slipping … /
   cutting …），不得把多个节拍揉成一个长句；该从句结构供下游确定性拆段
   （一段一动作）直接消费，保证段与段动作递进、不重复、段和=整镜时长。
"""


ACTIONLINE_RULES = """
1. 只认领本场剧本的节拍，beats=[起,止] 按顺序连续、不重不漏覆盖全部剧作拍。
2. 每拍只写一个动作（who 对谁做什么→结果），一句话可表达；不塞多动作、不截断关键动作。
3. 因果衔接：相邻拍必须共享动作因果（上一拍 to_next → 本拍 from_prev），不凭空开启与上拍无关的新动作。
4. 空间纪律：同一主体跨拍方位/朝向/屏幕侧要能对上，不瞬移、不越轴（180°）；
   确需换位必须在 who/where/action 里写出横移过程。
5. who/where/facing 用受控词表（center/north/south/east/west/off-screen/toward_camera/away_from_camera），
   不要漏、不要用叙述句代替。
6. suggested_shot 给足（景别/镜头类型/承接），供下一级成镜直接采用。
7. state_from 直接抄剧本拍 state_from（谁在哪/朝向/在做什么），是承接意图不是新设计。
8. screen_side 建议机位侧 left/right/center，守 180° 轴线；同一主体不得无理由左右翻转。
9. 每次只输出本场一个 ActionBeatLine。
"""


SCRIPT_EXCERPT = 20_000


STAGING_EXCERPT = 8_000


BOARD_EXCERPT = 12_000


EPISODE_EXCERPT = 20_000


EPISODE_BATCH_CHARS = 60_000


def _excerpt(text: str, limit: int) -> str:
    text = (text or "").strip()
    if len(text) <= limit:
        return text
    head = limit * 3 // 5
    return text[:head] + "\n……（本集切片中段省略）……\n" + text[-(limit - head):]


_SENT_SPLIT = re.compile(r"(?<=[。！？!?；;\n])")


def _focus_excerpt(text: str, anchors: list[str], limit: int) -> str:
    """按 must_show / key_props 定向摘录：关键事件所在的段落一定进上下文。

    旧做法只留切片头尾，中段发生的转折/道具会被整段丢掉，剧本只能靠梗概
    猜细节——这正是“情节跳、关键道具没特写”的来源之一。
    """
    text = (text or "").strip()
    if len(text) <= limit:
        return text
    keys: list[str] = []
    for a in anchors or []:
        k = _event_key(a)
        if len(k) >= 2 and k not in keys:
            keys.append(k)
    if not keys:
        return _excerpt(text, limit)
    parts = [p.strip() for p in _SENT_SPLIT.split(text) if p.strip()]
    if len(parts) <= 2:
        return _excerpt(text, limit)
    part_keys = [_event_key(p) for p in parts]
    priority: set[int] = set()      # 每条必拍至少保一句，预算不够也不丢
    for k in keys:
        cands = [i for i, pk in enumerate(part_keys)
                 if k in pk or (len(pk) >= 4 and pk in k)]
        if not cands:
            # 逐字没命中时，用二元组覆盖找最接近的一句。
            grams = {k[j:j + 2] for j in range(len(k) - 1)}
            scored = [(sum(1 for g in grams if g in pk), i)
                      for i, pk in enumerate(part_keys)]
            scored = [(s, i) for s, i in scored if s]
            cands = [i for _, i in sorted(scored, reverse=True)[:2]]
        if cands:
            # 取靠中间的命中句：既避开开头的泛泛提及，也保留实际发生处。
            priority.add(cands[len(cands) // 2])
    if not priority:
        return _excerpt(text, limit)
    keep: set[int] = set(priority)
    for i in priority:
        keep.update(range(max(0, i - 1), min(len(parts), i + 2)))

    head_n, tail_n = max(1, limit // 5), max(1, limit // 5)
    head_parts: list[str] = []
    i = 0
    used = 0
    while i < len(parts) and used + len(parts[i]) <= head_n:
        head_parts.append(parts[i])
        used += len(parts[i])
        i += 1
    tail_parts: list[str] = []
    j = len(parts) - 1
    used = 0
    while j >= i and used + len(parts[j]) <= tail_n:
        tail_parts.insert(0, parts[j])
        used += len(parts[j])
        j -= 1

    mid_budget = max(0, limit - sum(map(len, head_parts))
                     - sum(map(len, tail_parts)))
    ordered = sorted(priority) + sorted(keep - priority)
    mid: list[str] = []
    used = 0
    for idx in ordered:
        if idx < i or idx > j:
            continue
        p = parts[idx]
        # 必拍句即使略超预算也保留，否则关键事件又会被裁掉。
        if used + len(p) > mid_budget and idx not in priority:
            continue
        mid.append(p)
        used += len(p)
    head = "".join(head_parts)
    tail = "".join(tail_parts)
    body = "".join(mid)
    if not body:
        return _excerpt(text, limit)
    return f"{head}\n……（按必拍情节定向摘录）……\n{body}\n……\n{tail}"


def _bible_payload(outline: Outline) -> dict[str, Any]:
    return {k: v for k, v in outline.model_dump(mode="json").items()
            if k != "episodes"}


def _neighbors(outline: Outline, ep: int) -> list[dict[str, Any]]:
    return [{"ep": e.ep, "title": e.title, "synopsis": e.synopsis,
             "hook": e.hook, "cliff": e.cliff}
            for e in outline.episodes if abs(e.ep - ep) <= 1]


def _prev_end(prev: ScriptEpisode | None, ep: int) -> dict[str, Any]:
    """P0-C5 上集末状态事实块：剧本写戏时看到上一集结束时的状态，替代仅邻居梗概。

    取上一集最后一个场景的收尾状态（目标、情绪、末拍动作/台词），
    供本集开场第一拍显式承接，避免跨集衔接"无中生有重启"。
    """
    if prev is None or ep <= 1:
        return {"note": "本集是全剧开场（EP01），无前情承接，允许自由开场"}
    last_scene = prev.scenes[-1]
    last_beats = last_scene.beats[-2:]
    tail = []
    for b in last_beats:
        if b.action:
            tail.append(f"动作：{b.action}")
        elif b.line:
            tail.append(f"台词（{b.speaker}）：{b.line}")
    return {
        "ep": prev.ep,
        "title": prev.title,
        "cliff": prev.cliff,
        "last_scene": {
            "id": last_scene.id,
            "location": last_scene.location,
            "mood": last_scene.mood,
            "goal": last_scene.goal,
        },
        "last_tail": tail,
    }


def _slice_text(slices: list[EpisodeSlice] | None, ep: int,
                source: str, limit: int,
                anchors: list[str] | None = None) -> str:
    text = (slices[ep - 1].text
            if slices and 1 <= ep <= len(slices) else source)
    return _focus_excerpt(text, anchors or [], limit)


def _cast_payload(cast: Cast, ids: set[str]) -> dict[str, Any]:
    """只给本场角色，减少 payload；id 不在本场的不进提示词。

    完整定妆 prompt / appearance_block / identity_lock / persona.appearance
    属于实现层的参考图与身份注入数据，不进入分镜提示词生成上下文。
    """
    data = cast.model_dump(mode="json")
    chars: list[dict[str, Any]] = []
    for raw in data["characters"]:
        if raw.get("id") not in ids:
            continue
        raw.pop("image", None)
        raw.pop("appearance_block", None)
        raw.pop("identity_lock", None)
        persona = raw.get("persona")
        if isinstance(persona, dict):
            persona.pop("appearance", None)
        chars.append(raw)
    data["characters"] = chars
    return data


def _cut_brief(cut: StoryCut) -> dict[str, Any]:
    # P0：补 prompt_en（画面内容），跨场承接不能再只看 state_out 骨架
    return {
        "beats": list(cut.beats), "size": cut.size,
        "coverage": cut.coverage, "characters": cut.characters,
        "cut_reason": cut.cut_reason,
        "prompt_en": cut.prompt_en,
        "motion": cut.motion,
        "state_out": cut.state_out.model_dump(mode="json"),
    }


def _identity_repetition_warnings(seg: StorySegment, cast: Cast) -> list[str]:
    """检测提示词是否又抄了整段定妆外貌。

    输入层不再提供形象模式：prompt_en 应只写表演/场景。模型偶尔会从源文
    或前镜里复述长外貌句；逐字比较足够长的外貌子句，命中就提示人工留意。
    这是软提示，不阻断生成。
    """
    probes: dict[str, list[str]] = {}
    for ch in cast.characters:
        source = ch.appearance_block or (ch.image.prompt if ch.image else "") or ""
        items: list[str] = []
        for clause in re.split(r"[,;，；。！？]", source):
            clause = " ".join(clause.split()).strip()
            if len(clause) < 40:
                continue
            key = clause.casefold()
            if key in items:
                continue
            items.append(key)
        if items:
            probes[ch.id] = items
    warnings: list[str] = []
    for i, cut in enumerate(seg.cuts, 1):
        prompt = (cut.prompt_en or "").casefold()
        for cid in cut.characters:
            for clause in probes.get(cid, []):
                if clause in prompt:
                    warnings.append(
                        f"第 {i} 镜重复了 {cid} 的完整外貌句"
                        f"（{clause[:32]}…）；该镜已由参考图锁定形象，"
                        "建议只保留动作/场景/光线与短身份锚点")
                    break
        if len(warnings) >= 3:
            break
    return warnings


def _placeholder_prompt(sp: ScriptParams, size: str = "medium") -> str:
    if sp.prompt_language == "zh":
        return f"{size} 首帧，人物与环境清晰可见，自然光线"
    return f"clear {size} first frame, subject and environment visible, natural lighting"


def _prompt_brief(cut: StoryCut) -> dict[str, Any]:
    """给并行的视觉展开调用：不含 prompt_en，避免把占位符或旧词喂回去。"""
    return {
        "beats": list(cut.beats), "size": cut.size, "coverage": cut.coverage,
        "characters": cut.characters, "props": cut.props,
        "cut_reason": cut.cut_reason, "frame": cut.frame,
        "emphasis": cut.emphasis, "visual_focus": cut.visual_focus,
        "state_out": cut.state_out.model_dump(mode="json"),
    }


def _world_bible(cast: Cast, art: Art) -> dict[str, Any]:
    """从 cast/art 自动导出"连续性块"（World Bible）。

    把角色/服装/道具/场景写成数据库条目，供分镜阶段查连续性。这里**不写**
    完整定妆 prompt / identity_anchor：身份由 ref_characters 与
    story_assets.json 提供，是否注入提示词由实现层决定。
    """
    ch = {}
    for c in cast.characters:
        wb = c.wardrobe_states
        # wardrobe_states 是 Pydantic 对象，必须转 dict，否则 json.dumps 直接 TypeError
        wardrobe = ([w.model_dump(mode="json") for w in wb]
                    if isinstance(wb, (list, tuple)) else [])
        entry: dict[str, Any] = {
            "name": c.name,
            "wardrobe_states": wardrobe,
            "color_signature": c.color_signature or "",
        }
        ch[c.id] = entry
    scenes = {}
    for s in art.scenes:
        scenes[s.id] = {
            "name": s.name,
            "plate": (s.image.prompt if s.image else "") or "",
            "lighting": [(l.state, l.prompt) for l in s.lighting] if s.lighting else [],
        }
    props = {}
    for p in art.props:
        props[p.id] = {
            "name": p.name,
            "prompt": (p.image.prompt if p.image else "") or "",
            "scale": p.scale or "",
        }
    return {"characters": ch, "scenes": scenes, "props": props}
