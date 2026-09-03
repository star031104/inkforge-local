from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass
from typing import Any

from .lore import activate_lore
from .memory import (
    MemoryHit,
    relevance,
    render_memories,
    render_thread_agenda,
    retrieve_memories,
    select_thread_agenda,
)
from .planning import render_planning_context
from .style_engine import render_fingerprint, render_style_context
from .manuscript_quality import prompt_repetition_guard
from .knowledge import render_knowledge_context
from .references import render_reference_context, combined_style_corpus
from .canon import render_canon_context
from .writing_skills import activate_writing_skills, render_writing_skills


SYSTEM_PROMPT = """你是“砚火”，一名严谨的中文小说合著者。
你的任务是创作可直接进入正文的文学文本，同时维护人物、世界设定、时间线和叙事视角的一致性。

优先级从高到低：
1. 作者临时要求与人工核对的同人角色正典锁。
2. 本章计划中的必须保留、必须避免，以及已确认故事事实/人物状态/时间线/世界规则。
3. 近期焦点、长期作者意图和大纲。
4. 抽象文风特征。

若大纲要求角色做出明显违背人工核对正典核心人格的行为，优先调整剧情实现方式，而不是扭曲角色。

硬规则：
1. 不复述任务，不解释创作过程，不输出“以下是正文”等元话语。
2. 不擅自改写既定事实，不用回忆、梦境或“其实”强行修补矛盾。
3. 人物只能依据其亲历、被告知或合理推断的信息行动。
4. 道具、伤势、位置、关系和时间流逝必须承接最近状态。
5. 人物的价值观、恐惧、内在矛盾和语言指纹是稳定约束；状态可以变化，但变化必须由正文事件造成。
6. 人物不得为了推进剧情突然变聪明、变愚蠢、改口吻或遗忘自身目标；如需反常行为，正文必须给出可观察诱因。
7. 稳定外貌是事实库，不是每次登场都要复述的清单；只提取与当前动作、视角和情绪有关的少量细节。
8. 不连续复用同一段环境介绍、身体反应、眼神、比喻、句式起笔或段落模板。
9. 用行动、感官、对话与具体细节呈现，不用总结代替关键场面。
10. 避免模板化网文腔、连续排比、滥用破折号、空泛抒情和同义反复。
11. 抵达本章目标即可，不抢写后续大纲，不提前回收未到时机的伏笔。
12. 不得声称“旧信、日记、过去对话、回忆或档案中早已写过/发生过”上下文未提供的信息。新线索必须在当前场景中可观察，或明确保持为人物猜测。
13. 每个场景遵循“欲望/任务→阻力→选择→代价→新状态”的因果链；转折必须改写人物下一步行动，不能只是再来一条消息。
14. 开头尽快落在正在变化的具体时刻；结尾停在选择、后果或认知已改变之后。普通章节不强造悬崖，终章不再新增未结问题。
15. 输出只包含用户要求的小说正文。"""


STYLE_ANALYSIS_PROMPT = """你是文学编辑。请分析给定样文的可迁移写作特征，不要模仿专有角色、地名、情节或独特句子。
返回严格 JSON：
{
  "name": "不超过12字的风格名",
  "profile": "150-300字，描述叙述视角、句法节奏、用词、意象、对话、情绪距离与场景组织",
  "dos": ["4-8条可执行写作规则"],
  "donts": ["3-6条应避免的偏差"]
}
不要输出 Markdown 代码块。样文如下：
"""


CHAPTER_PLAN_PROMPT = """你是长篇小说的章节规划师。依据权威设定、之前进展和作者要求，为当前章节制定一个克制、可执行的计划。不得直接写正文。
所有字段必须使用输入中真实存在的人名、地点、道具或事件，至少点名一个具体实体。禁止输出“主角”“某人”“某物”“核心特质”“NPC”“根据总纲”等占位词；输入不足时明确写“暂无足够设定”，不得编造通用套路。
返回严格 JSON：
{
  "goal": "本章结束时发生的可验证变化",
  "conflict": "本章主要阻力与人物选择",
  "must_keep": ["必须出现或保持的事实，3-6条"],
  "must_avoid": ["不得发生的跑偏、剧透或矛盾，3-6条"],
  "turning_point": "本章中段或后段的转折",
  "ending_hook": "结尾留下的具体悬念或推动力；终章写收束结果",
  "chapter_type": "setup/escalation/reversal/revelation/payoff/aftermath/transition/climax/resolution之一",
  "pov_character": "本章视角人物；全知视角写omniscient",
  "time_location": "故事内时间与主要地点",
  "opening_beat": "开篇第一拍：谁正做什么、即时阻力是什么；续写时必须承接已有结尾",
  "scene_beats": ["3-6拍因果链；每拍写行动→阻力/发现→选择或后果"],
  "emotional_turn": "哪一件可观察事件使哪名人物的态度发生何种变化",
  "thread_actions": ["线索ID：plant/advance/defer/payoff/hold + 本章具体动作，0-3条"],
  "exit_state": "章末信息、资源、关系、位置或立场的新状态",
  "ending_type": "decision/revelation/reversal/deadline/consequence/image/closure之一"
}
长度限制：goal、conflict 各不超过70字；turning_point、ending_hook 各不超过60字；
must_keep 与 must_avoid 各3-5条，每条不超过35字。先保证 JSON 完整闭合，再考虑措辞丰富。
scene_beats 必须前后相因，不能把同一结果换三种说法。线索议程中标为必须推进的线索必须出现在 thread_actions；未到回收时机的线索不得 payoff。
不要输出 Markdown。"""


CHAPTER_MEMORY_PROMPT = """你是长篇小说的状态观察员。只根据提供的本章正文输出“本章造成的增量”，不得重写整个数据库，不得推测未来，不得把修辞、计划或人物猜测当成客观事实。
优先保证 JSON 完整闭合。summary 与 story_so_far 必须输出；其余没有可靠增量时可以省略该字段或输出空数组。
为了适配小模型，每章最多输出：character_updates 4项、facts 6项、plot_threads 4项、timeline 4项、relationship_updates 4项、description_updates 4项、continuity_notes 4项。不要为了凑数量重复同一事实。
返回严格 JSON：
{
  "summary": "120-250字的因果摘要，包含关键选择、结果和结尾状态",
  "story_so_far": "在已有全书进展基础上更新成300-600字滚动摘要，保留主因果链、关键选择、当前局面和仍未解决的核心问题",
  "character_updates": [{"name":"人物名","state":"本章结束时的新状态","location":"结束位置","knowledge_gain":"兼容字段：本章新增知情","knowledge_gains":[{"text":"此人新知道的原子信息","learned_how":"亲历/被告知/推断","certainty":"confirmed或suspected","evidence":"正文连续原句8-40字"}],"items":"获得、失去或仍持有的重要道具","emotion":"结束时情绪","appearance_state":"仅记录衣着/伤势/伪装等可变外观","evidence":"能证明状态变化的正文连续原句8-40字"}],
  "facts": [{"text":"以后必须保持一致的原子事实","tags":["人物或地点"],"importance":1,"confidence":"confirmed或suspected","visibility":"objective/private/rumor","evidence":"正文连续原句8-40字","supersedes_id":"若替代旧事实则填旧事实ID"}],
  "plot_threads": [{"thread_id":"更新既有线索时必须使用输入中的ID；新线索留空","title":"稳定线索名","type":"mystery/promise/threat/relationship/goal","status":"open/progressing/deferred/ready/closed","latest":"本章推进","expected_payoff":"最终要回答或兑现什么","payoff_condition":"满足什么正文条件才可回收","target_window":"immediate/near/mid/slow/endgame","stakeholders":["相关人物"],"knowledge_holders":["当前真正知情者"],"payoff":"仅closed时写实际回收结果","evidence":"能证明推进或回收的正文连续原句8-40字"}],
  "timeline": [{"time":"故事内时间，没有则写相对时间","event":"可验证事件","participants":["亲历人物"],"location":"地点","causes":["直接前因"],"effects":["已发生后果"],"evidence":"正文连续原句8-40字"}],
  "relationship_updates": [{"left":"人物A","right":"人物B","state":"章末关系事实","tension":"具体张力","trust":"信任变化","knowledge_gap":"双方信息差","evidence":"正文连续原句8-40字"}],
  "scene_settlement": {"goal_achieved":"yes/partial/no","irreversible_changes":["不可逆变化"],"open_questions":["本章真实留下的问题"],"closing_state":"最后场景状态"},
  "description_updates": [{"character":"人物名","aspect":"外貌/动作/情绪/环境意象","phrase":"本章已经使用、后续不宜机械复述的显著描写，8-40字"}],
  "continuity_notes": ["可能需要作者确认的矛盾或含糊点"]
}
importance 为 1-5。evidence 必须是正文中逐字连续出现的短语；没有证据的条目不要输出。既有线索只更新，不得换名复制；只有正文已经兑现 expected_payoff 且满足 payoff_condition 才能 closed。description_updates 只记录具有辨识度、确实出现的短语，最多4条；普通动作不要记录。不要输出 Markdown。"""


CHAPTER_MEMORY_COMPACT_PROMPT = """你是长篇小说的状态观察员。上一轮完整记忆结构对当前模型过重。现在执行“最小事实包”恢复，只保留摘要、最重要事实、人物章末状态和场景结算。
只根据“本章正文”取证；旧状态和计划仅用于判断什么是新增，绝不能当作已发生事实。内容宁少勿猜。必须先闭合 JSON，不要解释。
返回严格 JSON：
{
  "summary":"80-160字因果摘要，只写本章关键选择、结果和结尾状态",
  "facts":[{"text":"以后必须保持一致的一条原子事实，不超过45字","tags":["人物或地点，最多3个"],"importance":1,"confidence":"confirmed或suspected","visibility":"objective/private/rumor","evidence":"正文连续原句8-30字"}],
  "character_updates":[{"name":"人物","state":"章末新状态，不超过35字","location":"章末地点，不超过20字","knowledge_gain":"本章新增知情，不超过35字","evidence":"正文连续原句8-30字"}],
  "scene_settlement":{"goal_achieved":"yes/partial/no","irreversible_changes":["最多2条"],"open_questions":["最多2条"],"closing_state":"不超过60字"}
}
facts 最多4项、character_updates 最多3项；没有可靠证据就输出空数组。不要输出 story_so_far、关系、时间线、伏笔、描写账本或连续性备注字段。evidence 必须逐字来自正文。不要输出 Markdown。"""


IDEAS_PROMPT = """你是小说策划编辑。根据作者已经提供的题材、人物、长期意图和当前进度，提出3个明显不同但都能落地的创作方向，供作者选择，而不是替作者做唯一决定。
每个方案必须具体点名现有人物或设定；如果项目仍为空白，可以提出原创实体。短篇方案要集中、可收束，长篇方案要能形成持续代价和人物弧。
每个字段不超过40个中文字符，语言紧凑，不解释推理过程。
返回严格 JSON：
{
  "options": [{
    "title": "简短方案名",
    "theme": "要探讨的主题命题",
    "central_conflict": "具体人物之间或人物内外的冲突",
    "story_promise": "这个方向持续给读者什么体验",
    "turning_point": "可用的转折",
    "ending_direction": "可能的收束方向，不写死细节",
    "why_fit": "为什么适合当前作品",
    "risk": "最容易俗套或跑偏的地方"
  }]
}
三个方案不得只是换名字，至少在价值冲突、代价或叙事结构上不同。不要输出 Markdown。"""


INCUBATOR_PROMPT = """你是长篇小说的开书总导演。作者可能只给出一句灵感、一个人物、一种氛围或一个片段。当前只建立 2 套方向核心；人物与世界资产由下一次独立调用补充。

两套方案必须保留作者明确写出的核心偏好，但在主角困境、故事驱动器或终局代价上明显不同。
把综合完成度、可持续性和作者偏好匹配度更高的一套放在 options[0]，供自动导演无人值守时采用。
不要把作者没要求的热门套路、系统、超能力或恋爱线强行塞入。历史、现实、悬疑题材必须给出可信边界。
每套方案都必须包含可持续的主线、硬边界、前期故事弧和结局方向，但本轮不要生成 characters 或 world_entries。

返回严格 JSON：
{
  "options": [{
    "title": "作品名",
    "genre": "题材与类型标签",
    "positioning": "目标读者和阅读体验，50-100字",
    "premise": "完整核心构想，120-220字",
    "reader_promise": "读者持续能得到什么",
    "central_question": "全书最终回答的价值问题",
    "central_conflict": "可持续升级的贯穿冲突",
    "story_engine": "为什么能持续写很多章，必须具体",
    "outline": "从开篇到结局的因果大纲；短篇180-350字，长篇450-800字，包含前因、阶段升级、高潮与代价",
    "author_intent": "长期作者意图，80-160字",
    "current_focus": "开篇前3章只推进什么、暂不揭示什么",
    "book_rules": ["8-12条人物知识边界、能力上限、时间线、叙事禁令或世界硬规则"],
    "ending_direction": "结局状态、人物选择与不可逆代价",
    "tone": "总体气质和叙事要求",
    "pov": "auto、first、third_limited或omniscient",
    "target_chapters": 30,
    "opening_hook": "第一章的具体入口事件",
    "first_arc": "前期故事弧的目标、阻力、转折与阶段结果，120-220字"
  }]
}

每套方案的 book_rules 提供 6-10 条。target_chapters 必须尊重作者选择的篇幅。两套 outline 不得只是同一大纲换人名。先保证 JSON 完整闭合。
不要输出 Markdown，不要解释推理。"""


INCUBATOR_ASSETS_PROMPT = """你是小说开书资产编辑。作品方向已经确定；当前只为这一套候选方向建立精简但可直接编辑的人物卡和世界书，不重写大纲。
严格遵守输入中的作者禁区、硬规则、时代节点、知识边界和称谓。不得用后世称谓、物件或制度污染历史题材；不得把计划中的未来事件写成开篇既成事实。
返回严格 JSON：
{
  "characters": [{
    "name": "姓名",
    "role": "身份和不可替代的剧情功能",
    "aliases": ["当前时间点确实可用的称谓，最多3个"],
    "description": "不可变身份和背景，40-80字",
    "personality": "可观察的思考、待人和压力反应，35-70字",
    "appearance": "2-4个不违背时代的外形锚点",
    "values": "价值取舍和底线，20-50字",
    "contradictions": "欲望与自我阻力，20-50字",
    "relationships": "开篇关系视角，30-70字",
    "hard_limits": "能力、知识和行为边界，20-50字",
    "goal": "开篇目标，不超过35字",
    "knowledge": "开篇确实知道的信息，不超过50字",
    "voice": "语气、句式和避用词，25-60字"
  }],
  "world_entries": [{
    "title": "设定名",
    "category": "制度/地点/组织/历史/物件等",
    "keys": ["2-5个明确触发词"],
    "content": "可直接进入世界书的权威设定，50-110字",
    "canon": "hard或soft",
    "constant": false
  }]
}
提供 3-5 名主要人物和 3-5 个必要世界条目；第一名必须是核心主角。人物之间必须有可执行差异。只写开篇已经成立的状态，不泄露未来。先保证完整闭合，不输出 Markdown或解释。"""


DIRECTOR_SEED_BRIEF_PROMPT = """你是自动小说导演的开书策划。当前是无人值守流程，只生成一套最可靠的故事骨架，不提供备选方案，也不展开逐卷逐章大纲。
必须保留作者明确写出的核心灵感与禁区；不要强塞系统、修仙、超能力、恋爱线或工业外挂。历史和现实题材不得让其他人物降智。
返回严格 JSON：
{
  "title": "作品名",
  "genre": "题材与类型",
  "positioning": "目标读者与阅读体验，60-120字",
  "premise": "核心构想，150-260字",
  "reader_promise": "持续阅读承诺",
  "central_question": "最终回答的价值问题",
  "central_conflict": "能持续升级的贯穿冲突",
  "story_engine": "危机如何反复产生选择、代价和新局面",
  "story_spine": "从开篇、发展、转折、高潮到结局的因果骨架；长篇350-650字，3-5章短篇可压缩为140-300字；详细分卷由下一阶段生成",
  "author_intent": "长期作者意图，100-180字",
  "current_focus": "开篇前三章只推进什么、暂不揭示什么",
  "book_rules": ["6-10条知识边界、能力上限、时间线或叙事禁令"],
  "ending_direction": "结局状态、选择和不可逆代价",
  "tone": "总体气质",
  "pov": "auto、first、third_limited或omniscient",
  "opening_hook": "第一章具体入口事件",
  "first_arc": "前期故事弧的目标、阻力、转折和阶段结果，120-220字"
}
先保证完整闭合和字段齐全。不要输出人物数组、世界书、Markdown或解释。"""


DIRECTOR_CAST_PROMPT = """你是自动小说导演的选角编辑。根据已经确认的故事骨架，只确定开篇真正需要的主要人物名单，不写完整人物卡，不重写大纲。
返回严格 JSON：
{
  "characters": [{
    "name": "姓名",
    "role": "身份和剧情功能",
    "narrative_function": "不可被其他人物替代的叙事作用，不超过50字",
    "relationship_seed": "与主角或核心矛盾的初始关系，不超过50字",
    "core_conflict": "自身欲望、阻力和代价，不超过60字"
  }]
}
提供3-5人，第一名必须是核心主角；姓名不得重复，功能不得互换。每项必须是已经决定采用的具体人物，姓名中不得出现“或、代表、待定”、斜线或括号备选说明。字段必须简短，先保证 JSON 完整闭合。不要输出完整人物卡、世界书、Markdown或解释。"""


DIRECTOR_CHARACTER_CARD_PROMPT = """你是小说人物总监。现在只为指定的一名人物建立可长期检索的权威人物卡，不输出其他人物，不重写剧情。
返回严格 JSON 对象：
{
  "name": "必须与指定姓名完全一致",
  "role": "身份和剧情功能",
  "aliases": ["常用称谓，最多3个"],
  "description": "不可变身份与背景，60-110字",
  "personality": "可观察的思考、待人和压力反应，50-100字",
  "appearance": "2-4个稳定外形锚点，30-60字",
  "values": "价值观、底线及取舍顺序，30-70字",
  "fears": "恐惧、软肋与回避机制，30-70字",
  "contradictions": "想要什么却因何抗拒，30-70字",
  "relationships": "对其他主要人物的初始看法，50-100字",
  "arc": "人物弧起点、压力、可能变化及不可逆选择，50-100字",
  "hard_limits": "不可无依据改变的能力、认知或行为底线，30-70字",
  "goal": "开篇目标，不超过40字",
  "state": "开篇身体、资源或关系状态，不超过50字",
  "knowledge": "开篇确实知道的事，不超过60字",
  "secrets": "本人隐瞒且他人不可自动知道的事，不超过60字",
  "voice": "词汇、句长、语气、潜台词习惯和避用词，40-80字",
  "dialogue_examples": ["一条原创短对白，只展示声音和潜台词"]
}
人物特征必须可观察、可执行，不用“复杂、善良、聪明”等空标签。只输出这个 JSON 对象，先保证完整闭合，不要 Markdown 或解释。"""


DIRECTOR_WORLD_PROMPT = """你是小说世界设定编辑。根据已经确认的故事骨架和人物名单，只建立开篇必须知道、以后必须保持一致的世界书条目，不重写人物卡或大纲。
返回严格 JSON：
{
  "world_entries": [{
    "title": "设定名",
    "category": "制度/地点/组织/历史/物件等",
    "keys": ["2-5个明确触发词"],
    "content": "可直接进入世界书的权威设定，60-120字",
    "canon": "hard或soft",
    "constant": false
  }]
}
提供3-5个必要条目。优先制度边界、时间地点、组织关系、知识与技术上限；不要把剧情摘要伪装成设定。先保证 JSON 完整闭合，不输出 Markdown 或解释。"""


AUDIT_PROMPT = """你是苛刻但克制的长篇小说连续性与场景审计员。对照权威上下文检查候选草稿，不因个人文风偏好报错。
检查：人物知识来源、永久人格核心与说话方式、稳定外貌与可变外观、动机、位置、时间、伤势、道具、关系、世界规则、章节计划、伏笔阶段/回收条件、重复内容、提前剧透和结尾状态。还要检查场景是否形成“任务→阻力→选择→代价→新状态”，转折是否真正改变后续行动，开头是否承接已有结尾，结尾是否完成本章变化而非突然截断。检查是否原样复用了近期已经用过的显著外貌、动作、环境或比喻描写；普通名词和必要事实重复不算问题。
严重度必须遵守：
- high：草稿与权威事实明确矛盾，或泄露被明确禁止的真相。
- medium：有较强证据表明连续性可能断裂，需要作者确认。
- low：轻微重复或表达问题，不影响情节事实。
人物没有表现出你期待的情绪、再次描写既定环境、或采用不同但合理的写法，都不构成连续性错误。每个问题必须引用草稿中的具体证据；没有问题就返回空数组和 pass。
长期规则中要求未来召回的人名、地点或伏笔，只有当前章计划或线索治理议程明确激活时才应出现；不得要求当前章提前暗示后续卷内容，也不得提出违反 must_avoid 的修订建议。
最多返回4个最重要的问题；message不超过60字，suggestion不超过40字，strengths最多2条，revision_brief不超过80字。
返回严格 JSON：
{
  "score": 0,
  "verdict": "pass或revise",
  "issues": [{"severity":"high或medium或low","category":"类别","message":"具体问题","suggestion":"最小修复建议"}],
  "strengths": ["做得好的地方"],
  "revision_brief": "若需修改，给出不超过80字的最小修订说明"
}
score 为 0-100。不要重写全文，不要输出 Markdown。"""


@dataclass
class PromptSection:
    name: str
    content: str
    priority: int
    role: str = "user"
    required: bool = False
    keep_tail: bool = False
    min_chars: int = 0
    original_tokens: int = 0
    status: str = "included"
    reason: str = ""


@dataclass
class PromptBuild:
    messages: list[dict[str, str]]
    activated_lore: list[dict[str, Any]]
    activated_skills: list[dict[str, Any]]
    estimated_tokens: int
    sections: list[dict[str, Any]]
    retrieved_memories: list[MemoryHit]
    budget_warnings: list[str]


def estimate_tokens(text: str) -> int:
    chinese = len(re.findall(r"[\u3400-\u9fff]", text))
    other = max(0, len(text) - chinese)
    # Conservative across Qwen/Gemma/Llama tokenizers. Slight overestimation is
    # preferable to a late llama.cpp context-overflow error.
    return int(chinese / 1.25 + other / 3.6) + 1


def _budget_lore(
    entries: list[dict[str, Any]], token_budget: int
) -> tuple[list[dict[str, Any]], list[str]]:
    ranked = sorted(
        entries,
        key=lambda item: (
            bool(item.get("constant", False)),
            str(item.get("canon", "soft")) == "hard",
            item.get("_activation_reason") == "direct",
            len(item.get("_matched_keys", [])),
            int(item.get("order", 100)),
        ),
        reverse=True,
    )
    selected: list[dict[str, Any]] = []
    used = 0
    omitted: list[str] = []
    for entry in ranked:
        cost = estimate_tokens(str(entry.get("content", ""))) + 12
        hard = str(entry.get("canon", "soft")) == "hard"
        if selected and used + cost > token_budget and not hard:
            omitted.append(str(entry.get("title", "未命名设定")))
            continue
        selected.append(entry)
        used += cost
    selected.sort(key=lambda item: int(item.get("order", 100)))
    warnings = []
    if omitted:
        warnings.append(
            f"世界书独立预算约 {token_budget} tokens，已省略 {len(omitted)} 条低优先级设定："
            + "、".join(omitted[:8])
        )
    if used > token_budget:
        warnings.append(
            f"权威/常驻世界规则约 {used} tokens，超过世界书预算但仍保留"
        )
    return selected, warnings


def _render_plan(current: dict[str, Any]) -> str:
    plan = current.get("plan", {}) if isinstance(current.get("plan"), dict) else {}
    route = current.get("route", {}) if isinstance(current.get("route"), dict) else {}

    def value(key: str, fallback: object = "") -> object:
        planned = plan.get(key)
        if planned not in (None, "", []):
            return planned
        routed = route.get(key)
        return routed if routed not in (None, "", []) else fallback

    values = [
        f"目标：{value('goal', current.get('scene_goal', ''))}",
        f"冲突：{value('conflict')}",
        "必须保留：\n- " + "\n- ".join(value("must_keep", [])),
        "必须避免：\n- " + "\n- ".join(value("must_avoid", [])),
        f"转折：{value('turning_point')}",
        f"结尾钩子：{value('ending_hook')}",
        f"章节功能：{plan.get('chapter_type', '')}",
        f"视角人物：{plan.get('pov_character', '')}",
        f"时间地点：{plan.get('time_location', '')}",
        f"开篇第一拍：{plan.get('opening_beat', '')}",
        "场景因果拍：\n- " + "\n- ".join(plan.get("scene_beats", [])),
        f"情绪转向：{plan.get('emotional_turn', '')}",
        "线索动作：\n- " + "\n- ".join(plan.get("thread_actions", [])),
        f"章末新状态：{plan.get('exit_state', '')}",
        f"结尾类型：{plan.get('ending_type', '')}",
    ]
    return "\n".join(value for value in values if value.split("：", 1)[-1].strip("- \n"))


def _chapter_boundary_contract(project: dict[str, Any], current_index: int) -> str:
    chapters = project.get("chapters", [])
    current = chapters[current_index] if current_index < len(chapters) else {}
    plan = current.get("plan", {}) if isinstance(current.get("plan"), dict) else {}
    target = max(1, int(project.get("narrative", {}).get("target_chapters", 30) or 30))
    number = current_index + 1
    final_chapter = number >= target
    previous_ending = ""
    if current_index > 0:
        previous = chapters[current_index - 1]
        previous_plan = previous.get("plan", {}) if isinstance(previous.get("plan"), dict) else {}
        previous_ending = str(
            previous_plan.get("exit_state")
            or previous_plan.get("ending_hook")
            or previous.get("summary", "")
        )
    opening_rule = (
        "从正在发生的具体动作、感官变化、对话压力或上一章后果切入；"
        "前10%内让读者知道谁在何处想完成什么、眼前阻力是什么。"
        "除非环境本身造成阻力，不以天气、景物全景、人物履历或主题议论热身。"
    )
    if current.get("content", "").strip():
        opening_rule = "这是续写：第一句只承接现有最后动作/感官/对话，不重新设计章节开头。"
    ending_rule = (
        "结尾必须落在本章选择已经造成的新状态上；钩子来自后果、期限、发现或关系变化，"
        "不得凭空闯入陌生人/新消息，也不得用泛泛感叹代替变化。"
    )
    if final_chapter:
        ending_rule = (
            "这是计划终章：回答核心问题，兑现主要因果与人物选择，呈现代价后的稳定状态；"
            "不再新增需要后文解释的秘密、反派、任务或悬崖。结尾类型必须是 closure 或余韵意象。"
        )
    return (
        f"章节位置：第{number}/{target}章{'（计划终章）' if final_chapter else ''}\n"
        f"上一章交付状态：{previous_ending}\n"
        f"计划开篇：{plan.get('opening_beat', '')}\n"
        f"计划章末：{plan.get('exit_state', '') or plan.get('goal', '')}\n"
        f"开头规则：{opening_rule}\n"
        f"中段规则：每一拍必须由上一拍结果触发；至少一次选择要损失时间、资源、关系、权限、安全或自我认同。\n"
        f"结尾规则：{ending_rule}"
    )


def _craft_brief(project: dict[str, Any], current_index: int) -> str:
    current = project.get("chapters", [])[current_index]
    plan = current.get("plan", {}) if isinstance(current.get("plan"), dict) else {}
    chapter_type = str(plan.get("chapter_type", "") or "推进")
    pov = str(plan.get("pov_character", "") or project.get("narrative", {}).get("pov", "auto"))
    emotional_turn = str(plan.get("emotional_turn", ""))
    return (
        f"本章功能：{chapter_type}；视角过滤器：{pov}；情绪转向：{emotional_turn}\n"
        "文笔执行：每段只承担一个主要动作（行动、观察、判断、对话或余波）；"
        "关键选择前放慢并给出可触摸的证据，过场压缩；抽象判断后立刻用动作、物件、空间距离或话语反应落地。\n"
        "对白执行：每个说话者都有当场目的；重要对白至少包含一次回避、试探、误解、交换或拒绝，"
        "不要让人物轮流解释设定。叙述不得替人物总结尚未做出的选择。\n"
        "细节执行：优先选择会被人物使用、误读、损坏或付出代价的细节；"
        "同一段不堆叠视觉、听觉、嗅觉清单，也不为“文艺”制造与人物无关的比喻。"
    )


def _continuation_anchor(text: str) -> str:
    if not text.strip():
        return "当前章节尚无正文，从本章计划中的首个动作直接开场。"
    paragraphs = [item.strip() for item in re.split(r"\n+", text) if item.strip()]
    tail = "\n\n".join(paragraphs[-2:])[-1800:]
    sentences = [
        item.strip()
        for item in re.split(r"(?<=[。！？!?])", text)
        if item.strip()
    ]
    last = sentences[-1] if sentences else text[-200:]
    return (
        f"原文最后一句：{last}\n原文最后两段：\n{tail}\n\n"
        "续写的第一句必须在动作、感官、对话或因果上直接承接此刻，"
        "不得复述最后一句，不得重新介绍场景，不得无提示跳时空或切换视角。"
    )


def _narrative_contract(project: dict[str, Any], current_index: int) -> str:
    narrative = project.get("narrative", {})
    mode = project.get("story_mode", "long")
    target = max(1, int(narrative.get("target_chapters", 30)))
    progress = min(1, (current_index + 1) / target)
    base = (
        f"作品形态：{'短篇/中短篇' if mode == 'short' else '长篇/连载'}\n"
        f"叙事视角：{narrative.get('pov', 'auto')}\n"
        f"时态：{narrative.get('tense', 'auto')}\n"
        f"总体气质：{narrative.get('tone', '')}\n"
        f"核心问题：{narrative.get('central_question', '')}\n"
        f"结局方向：{narrative.get('ending_direction', '')}\n"
        f"当前故事弧：{narrative.get('current_arc', '')}\n"
        f"计划篇幅：约 {target} 章；当前进度约 {progress:.0%}。"
    )
    if mode == "short":
        if progress >= 0.7:
            base += (
                "\n短篇已进入后段：禁止新增需要大量篇幅解释的新支线或核心设定；"
                "让既有选择产生代价，推动线索汇合、高潮与收束。"
            )
        else:
            base += (
                "\n短篇规则：围绕一个核心问题和一条主要冲突线；每个场景同时推进情节与人物，"
                "不铺设无法在计划篇幅内回收的新支线。"
            )
    else:
        base += (
            "\n长篇规则：本章只完成当前故事弧中的一步；维持主线与支线的层级，"
            "不要为了刺激连续升级力量或提前消耗终局秘密。"
        )
    return base


def _stage_progression_guard(project: dict[str, Any], current_index: int) -> str:
    """Make the current volume's unique job explicit instead of relying on a huge outline."""
    volumes = project.get("planning", {}).get("volumes", [])
    if not isinstance(volumes, list) or not volumes:
        return "本章必须造成至少一个可验证的新变化，不能只换地点或措辞重演上一章的问题。"
    chapter_number = current_index + 1
    active_index = 0
    for index, volume in enumerate(volumes):
        if not isinstance(volume, dict):
            continue
        start = int(volume.get("chapter_start", 1) or 1)
        end = int(volume.get("chapter_end", start + int(volume.get("chapter_count", 1) or 1) - 1) or start)
        if start <= chapter_number <= end:
            active_index = index
            break
    volume = volumes[active_index] if isinstance(volumes[active_index], dict) else {}
    previous = volumes[active_index - 1] if active_index > 0 and isinstance(volumes[active_index - 1], dict) else {}
    following = volumes[active_index + 1] if active_index + 1 < len(volumes) and isinstance(volumes[active_index + 1], dict) else {}
    return (
        f"当前卷：第{active_index + 1}卷《{volume.get('title', '')}》\n"
        f"本卷唯一主战场：{volume.get('primary_arena', volume.get('conflict', ''))}\n"
        f"本卷必须完成：{volume.get('goal', '')}\n"
        f"本卷不可逆变化：{volume.get('irreversible_change', volume.get('ending_state', ''))}\n"
        f"上一卷已经完成、不得重做：{previous.get('ending_state', '')}\n"
        f"下一卷才处理、不得抢写：{following.get('goal', '')}\n"
        "本章验收：必须改变信息、资源、关系、权限、风险或人物立场中的至少一项；"
        "若删去本章而后续毫无变化，说明本章无效，必须重新设计。"
    )


def _active_character_names(project: dict[str, Any], query: str) -> set[str]:
    active: set[str] = set()
    folded = query.casefold()
    for character in project.get("characters", []):
        name = str(character.get("name", "")).strip()
        aliases = character.get("aliases", [])
        if isinstance(aliases, str):
            aliases = re.split(r"[,，\n]", aliases)
        names = [name] + [str(item).strip() for item in aliases if str(item).strip()]
        if name and any(candidate.casefold() in folded for candidate in names):
            active.add(name)
    return active


def _character_core(
    character: dict[str, Any], current_chapter_number: int | None = None
) -> str:
    name = str(character.get("name", "")).strip()
    ledger = [
        item
        for item in character.get("knowledge_ledger", [])
        if isinstance(item, dict)
        and item.get("active", True)
        and str(item.get("text", "")).strip()
        and (
            current_chapter_number is None
            or int(item.get("chapter_number", 0) or 0) == 0
            or int(item.get("chapter_number", 0) or 0) < current_chapter_number
        )
    ][-16:]
    ledger_text = "；".join(
        f"{item.get('text', '')}（{item.get('learned_how', '来源未标注')}，"
        f"{item.get('certainty', 'confirmed')}）"
        for item in ledger
    )
    return (
        f"【{name}】｜{character.get('importance', 'supporting')}\n"
        f"身份与不可变背景：{character.get('role', '')}；{character.get('description', '')}\n"
        f"人格内核：{character.get('personality', '')}\n"
        f"价值观与底线：{character.get('values', '')}\n"
        f"恐惧/软肋：{character.get('fears', '')}\n"
        f"内在矛盾：{character.get('contradictions', '')}\n"
        f"稳定外貌锚点：{character.get('appearance', '')}\n"
        f"当前可变外观（衣着/伤势/伪装）：{character.get('appearance_state', '')}\n"
        f"习惯动作：{character.get('mannerisms', '')}\n"
        f"绝不可写偏：{character.get('hard_limits', '')}\n"
        f"关系视角：{character.get('relationships', '')}\n"
        f"当前目标：{character.get('goal', '')}\n"
        f"当前状态：{character.get('state', '')}\n"
        f"所在位置：{character.get('location', '')}\n"
        f"开篇/人工维护的知情边界：{character.get('knowledge_baseline', character.get('knowledge', ''))}\n"
        f"有来源的新增知情：{ledger_text}\n"
        f"携带物品：{character.get('items', '')}\n"
        f"当前情绪：{character.get('emotion', '')}\n"
        f"语言指纹：{character.get('voice', '')}"
    )


def _character_context(
    project: dict[str, Any], query: str, limit: int = 10,
    current_chapter_number: int | None = None,
) -> str:
    active_names = _active_character_names(project, query)
    importance_scores = {"main": 8.0, "supporting": 3.0, "minor": 0.0}
    rendered: list[tuple[int, float, str]] = []
    for index, char in enumerate(project.get("characters", [])):
        name = str(char.get("name", "")).strip()
        if not name or not char.get("active", True):
            continue
        text = _character_core(char, current_chapter_number)
        boost = importance_scores.get(str(char.get("importance", "supporting")), 2.0)
        if name in active_names:
            boost += 20.0
        rendered.append((index, relevance(query, text) + boost, text[:4200]))
    if not rendered:
        return ""
    # Always retain one protagonist as the behavioural anchor, then add only
    # characters explicitly activated by this chapter. This prevents future
    # cast cards from leaking later arcs while keeping large-cast prompts safe.
    anchor = next((item for item in rendered if "】｜main" in item[2]), rendered[0])
    chosen = {anchor[0]}
    for item in sorted(rendered, key=lambda row: (row[1], -row[0]), reverse=True):
        character_name = str(
            project.get("characters", [])[item[0]].get("name", "")
        ).strip()
        if character_name in active_names:
            chosen.add(item[0])
        if len(chosen) >= limit:
            break
    return "\n\n".join(text for index, _, text in rendered if index in chosen)


def render_epistemic_context(
    project: dict[str, Any],
    current_index: int,
    query: str = "",
    active_character_names: set[str] | None = None,
) -> str:
    """Render explicit author/reader/character knowledge boundaries for one chapter."""
    chapters = project.get("chapters", [])
    current_number = current_index + 1
    current = chapters[current_index] if 0 <= current_index < len(chapters) else {}
    planned_pov = str(current.get("plan", {}).get("pov_character", "")).strip()
    active_names = set(active_character_names or _active_character_names(project, query))
    if planned_pov and planned_pov.casefold() not in {"omniscient", "全知", "auto"}:
        active_names.add(planned_pov)
    if not active_names:
        main_names = [
            str(item.get("name", "")).strip()
            for item in project.get("characters", [])
            if item.get("active", True)
            and str(item.get("importance", "supporting")) == "main"
            and str(item.get("name", "")).strip()
        ]
        active_names.update(main_names[:2])

    chapter_numbers = {
        str(chapter.get("id", "")): index + 1
        for index, chapter in enumerate(chapters)
        if isinstance(chapter, dict)
    }

    def fact_available(fact: dict[str, Any]) -> bool:
        if not fact.get("active", True):
            return False
        source_number = chapter_numbers.get(
            str(fact.get("source_chapter_id") or fact.get("chapter_id") or ""), 0
        )
        if source_number and source_number >= current_number:
            return False
        valid_from = int(fact.get("valid_from_chapter", 0) or 0)
        valid_until = int(fact.get("valid_until_chapter", 0) or 0)
        return not (
            (valid_from and current_number < valid_from)
            or (valid_until and current_number > valid_until)
        )

    facts = [
        item
        for item in project.get("memory", {}).get("facts", [])
        if isinstance(item, dict) and fact_available(item)
    ]
    ranked_reader = sorted(
        (
            item
            for item in facts
            if item.get("reader_known", False) and not item.get("author_only", False)
        ),
        key=lambda item: (
            relevance(query, str(item.get("text", ""))),
            int(item.get("importance", 3) or 3),
        ),
        reverse=True,
    )[:8]
    reader_lines = [str(item.get("text", "")).strip() for item in ranked_reader]

    character_blocks: list[str] = []
    for character in project.get("characters", []):
        name = str(character.get("name", "")).strip()
        if not name or name not in active_names or not character.get("active", True):
            continue
        confirmed: list[str] = []
        suspected: list[str] = []
        baseline = str(
            character.get("knowledge_baseline", character.get("knowledge", ""))
        ).strip()
        if baseline:
            confirmed.append(baseline)
        for item in character.get("knowledge_ledger", []):
            if not isinstance(item, dict) or not item.get("active", True):
                continue
            learned_number = int(item.get("chapter_number", 0) or 0)
            if learned_number and learned_number >= current_number:
                continue
            text = str(item.get("text", "")).strip()
            if not text:
                continue
            if str(item.get("certainty", "confirmed")) == "suspected":
                suspected.append(text)
            else:
                confirmed.append(text)
        for fact in facts:
            if name in [str(value) for value in fact.get("known_by", [])]:
                text = str(fact.get("text", "")).strip()
                if text and text not in confirmed:
                    confirmed.append(text)
        forbidden = [
            str(item.get("text", "")).strip()
            for item in facts
            if (
                item.get("author_only", False)
                or str(item.get("visibility", "objective")) == "private"
            )
            and name not in [str(value) for value in item.get("known_by", [])]
            and (
                relevance(query, str(item.get("text", ""))) > 0
                or int(item.get("importance", 3) or 3) >= 5
            )
        ][:6]
        lines = [f"【{name}】"]
        lines.append("可作为行动依据：" + ("；".join(confirmed[-12:]) or "暂无明确记录"))
        if suspected:
            lines.append("仅可当作猜测：" + "；".join(suspected[-8:]))
        if forbidden:
            lines.append("明确不可知/不可据此行动：" + "；".join(forbidden))
        character_blocks.append("\n".join(lines))

    pov_label = planned_pov or str(project.get("narrative", {}).get("pov", "auto"))
    return (
        f"本章视角标记：{pov_label}\n"
        "分层规则：权威事实属于作者/系统层，不自动等于人物知情；"
        "读者已经看到的信息可以形成戏剧反讽，但人物仍不得无来源使用；"
        "人物只有开篇人工知识、早于本章且有证据的知情账本、或 known_by 明示内容可作为行动依据；"
        "suspected 只能写成猜测。其他人物的 secrets 字段默认不可知。\n"
        + (
            "读者截至本章前已知的相关事实：\n- " + "\n- ".join(reader_lines) + "\n"
            if reader_lines
            else "读者截至本章前已知的相关事实：暂无结构化记录。\n"
        )
        + ("\n\n".join(character_blocks) if character_blocks else "当前未锁定具体视角人物，仍须逐人物遵守知情来源。")
    )


def _character_voice_examples(project: dict[str, Any], query: str) -> str:
    if not project.get("settings", {}).get("include_dialogue_examples", True):
        return ""
    active_names = _active_character_names(project, query)
    blocks: list[str] = []
    for character in project.get("characters", []):
        name = str(character.get("name", "")).strip()
        if not name or name not in active_names:
            continue
        examples = character.get("dialogue_examples", [])
        if isinstance(examples, str):
            examples = [item.strip() for item in examples.splitlines() if item.strip()]
        examples = [str(item).strip() for item in examples if str(item).strip()][:3]
        if examples:
            blocks.append(
                f"【{name}对白范例｜只学习措辞、节奏和潜台词，不复制内容】\n- "
                + "\n- ".join(examples)
            )
    return "\n\n".join(blocks)


def _repetition_guard(
    project: dict[str, Any], current_index: int, active_names: set[str]
) -> str:
    chapters = project.get("chapters", [])
    source = "\n".join(
        str(item.get("content", ""))[-7000:]
        for item in chapters[max(0, current_index - 2) : current_index + 1]
    )
    sentence_starts: Counter[str] = Counter()
    paragraph_starts: Counter[str] = Counter()
    for sentence in re.split(r"[。！？!?]+", source):
        compact = re.sub(r"\s+", "", sentence)
        if len(compact) >= 8:
            sentence_starts[compact[:8]] += 1
    for paragraph in re.split(r"\n+", source):
        compact = re.sub(r"\s+", "", paragraph)
        if len(compact) >= 12:
            paragraph_starts[compact[:12]] += 1
    tired = [key for key, count in sentence_starts.items() if count >= 2][:8]
    tired += [key for key, count in paragraph_starts.items() if count >= 2][:6]
    ledger = [
        item
        for item in project.get("memory", {}).get("description_ledger", [])[-40:]
        if isinstance(item, dict)
        and (
            not active_names
            or str(item.get("character", "")) in active_names
        )
    ][-12:]
    ledger_lines = [
        f"- {item.get('character', '人物')}｜{item.get('aspect', '描写')}：{item.get('phrase', '')}"
        for item in ledger
        if str(item.get("phrase", "")).strip()
    ]
    global_guard = prompt_repetition_guard(
        project,
        str(project.get("chapters", [])[current_index].get("id", ""))
        if project.get("chapters") and current_index < len(project.get("chapters", []))
        else "",
    )
    return (
        "人物出场时只选择与当前动作、视角或情绪有关的1—2个外貌锚点；"
        "除非外貌发生变化，不得每次登场都复述整套衣着、眼神、伤痕或招牌动作。\n"
        "同一情绪优先通过新的选择、动作、语气和环境互动表现，不连续复用同一种身体反应或比喻。\n"
        + ("近期高频句段起笔（本次换一种句法）：" + "、".join(dict.fromkeys(tired)) + "\n" if tired else "")
        + ("最近已经使用过的显著描写（没有变化时不要原样再写）：\n" + "\n".join(ledger_lines) + "\n" if ledger_lines else "")
        + global_guard
    )


def _fit_sections(
    sections: list[PromptSection], budget: int
) -> tuple[list[PromptSection], list[str]]:
    warnings: list[str] = []
    hard_caps = {
        "长期作者意图": 6000,
        "近期焦点": 4000,
        "本书不可违背规则": 8000,
        "章节场景契约": 5000,
        "伏笔与暗线治理议程": 7000,
        "文笔执行简报": 4000,
        "前置权威世界规则": 8000,
        "权威场景世界规则": 8000,
        "近端权威世界规则": 6000,
        "作品总纲": 12000,
        "分层导演规划": 10000,
        "人物权威状态": 14000,
        "人物与读者知情边界": 9000,
        "激活写作 Skills": 9000,
        "本场人物对白范例": 5000,
        "描写去重复约束": 5000,
        "全书滚动进展": 6000,
        "待确认连续性备注": 4000,
        "本章执行计划": 6000,
        "当前章节与最近正文": 18000,
        "续写边界锚点": 2400,
        "作者临时注": 4000,
    }
    for section in sections:
        section.original_tokens = estimate_tokens(section.content)
        section.status = "included"
        section.reason = ""
        cap = hard_caps.get(section.name)
        if cap and len(section.content) > cap:
            marker = "\n[…该区块过长，已保留关键部分…]\n"
            section.content = (
                marker + section.content[-cap:]
                if section.keep_tail
                else section.content[:cap] + marker
            )
            section.status = "trimmed"
            section.reason = "超过该类上下文的安全长度上限"
            warnings.append(f"已限制超长区块：{section.name}")
    total = sum(estimate_tokens(section.content) for section in sections)
    if total <= budget:
        return sections, warnings

    for section in sorted(sections, key=lambda item: item.priority):
        if total <= budget:
            break
        if section.required or not section.content:
            continue
        original = section.content
        removable_tokens = max(
            0, estimate_tokens(section.content) - estimate_tokens(section.content[: section.min_chars])
        )
        need = total - budget
        cut_ratio = min(0.85, max(0.2, need / max(1, removable_tokens)))
        keep_chars = max(section.min_chars, int(len(section.content) * (1 - cut_ratio)))
        if keep_chars >= len(section.content):
            continue
        marker = "\n[…已按上下文预算裁剪…]\n"
        section.content = (
            marker + original[-keep_chars:]
            if section.keep_tail
            else original[:keep_chars] + marker
        )
        section.status = "trimmed"
        section.reason = "为模型输出预留上下文预算"
        total = sum(estimate_tokens(item.content) for item in sections)
        warnings.append(f"已裁剪：{section.name}")

    # The first pass respects each section's preferred minimum. In a genuinely
    # tight context window, make a second emergency pass over optional material
    # so the reserved generation space remains real rather than advisory.
    if total > budget:
        for section in sorted(sections, key=lambda item: item.priority):
            if total <= budget:
                break
            if section.required or not section.content:
                continue
            current_tokens = estimate_tokens(section.content)
            need = total - budget
            keep_tokens = max(0, current_tokens - need - 8)
            if keep_tokens < 30:
                section.content = ""
                section.status = "omitted"
                section.reason = "上下文预算不足，低优先级可选区块已省略"
            else:
                keep_chars = max(
                    60,
                    int(len(section.content) * keep_tokens / max(1, current_tokens)),
                )
                original = section.content
                marker = "\n[…为生成正文预留上下文，已进一步裁剪…]\n"
                section.content = (
                    marker + original[-keep_chars:]
                    if section.keep_tail
                    else original[:keep_chars] + marker
                )
                section.status = "trimmed"
                section.reason = "上下文预算紧张，已进行第二轮裁剪"
            total = sum(estimate_tokens(item.content) for item in sections)
            warnings.append(f"已为输出空间进一步裁剪：{section.name}")

    if total > budget:
        warnings.append(f"固定上下文约 {total} tokens，超过预算 {budget}")
    return sections, warnings


def build_retrieval_query(project: dict[str, Any], request: dict[str, Any]) -> str:
    """Compile the same retrieval intent for prompt and persistent search layers."""
    instruction = str(request.get("instruction", "")).strip()
    selection = str(request.get("selection", "")).strip()
    chapter_id = request.get("chapter_id")
    chapters = project.get("chapters", [])
    current_index = next(
        (index for index, item in enumerate(chapters) if item.get("id") == chapter_id),
        max(0, len(chapters) - 1),
    )
    current = chapters[current_index] if chapters else {}
    settings = project.get("settings", {})
    recent_chars = int(settings.get("recent_chars", 12000))
    current_content = current.get("content", "")
    recent_text = current_content[-recent_chars:]
    return "\n".join(
        [
            instruction,
            selection,
            current.get("scene_goal", ""),
            _render_plan(current),
            recent_text[-3000:],
            project.get("current_focus", ""),
        ]
    )


def build_prompt(project: dict[str, Any], request: dict[str, Any]) -> PromptBuild:
    mode = request.get("mode", "continue")
    instruction = request.get("instruction", "").strip()
    selection = request.get("selection", "").strip()
    chapter_id = request.get("chapter_id")
    chapters = project.get("chapters", [])
    current_index = next(
        (index for index, item in enumerate(chapters) if item.get("id") == chapter_id),
        max(0, len(chapters) - 1),
    )
    current = chapters[current_index] if chapters else {}
    settings = project.get("settings", {})
    recent_chars = int(settings.get("recent_chars", 12000))
    current_content = current.get("content", "")
    recent_text = current_content[-recent_chars:]
    query = build_retrieval_query(project, request)
    thread_agenda_entries = select_thread_agenda(
        project,
        query,
        current_index,
        max(4, min(10, int(settings.get("memory_items", 12)) // 2)),
    )
    thread_agenda = render_thread_agenda(thread_agenda_entries)
    if thread_agenda:
        query += "\n" + thread_agenda
    active_character_names = _active_character_names(project, query)
    governed_names = {
        str(name).strip()
        for thread in thread_agenda_entries
        for key in ("stakeholders", "knowledge_holders")
        for name in thread.get(key, [])
        if str(name).strip()
    }
    active_character_names.update(governed_names)
    knowledge_context, knowledge_active_names = render_knowledge_context(
        project, query, sorted(active_character_names)
    )
    active_character_names.update(knowledge_active_names)
    canon_context = render_canon_context(project, query, sorted(active_character_names))
    reference_context = render_reference_context(project, query)
    lore = activate_lore(
        project,
        query,
        current_index,
        active_character_names,
        int(settings.get("lore_recursion_steps", 2)),
    )
    lore, lore_warnings = _budget_lore(
        lore, max(500, int(settings.get("lore_budget", 4500)))
    )
    memories = retrieve_memories(
        project,
        query,
        current_index,
        int(settings.get("memory_items", 12)),
        indexed_hits=request.get("_indexed_memory_hits", []),
    )
    activated_skills = activate_writing_skills(
        project,
        query,
        str(mode),
        explicit_ids=(
            request.get("skill_ids", [])
            or project.get("writing_skill_preferences", {}).get("manual_ids", [])
        ),
        user_skills=request.get("_user_writing_skills", []),
    )
    skills_context = render_writing_skills(activated_skills)

    lore_groups: dict[str, list[str]] = {
        "before": [], "after": [], "near": [],
        "before_hard": [], "after_hard": [], "near_hard": [],
    }
    for entry in lore:
        position = entry.get("position", "after")
        if position not in {"before", "after", "near"}:
            position = "after"
        group = position + ("_hard" if entry.get("canon") == "hard" else "")
        lore_groups[group].append(
            f"【{entry.get('title', '设定')}｜{entry.get('category', '世界设定')}】\n"
            f"{entry.get('content', '')}"
        )

    characters = _character_context(
        project, query, current_chapter_number=current_index + 1
    )
    epistemic_context = render_epistemic_context(
        project, current_index, query, active_character_names
    )
    character_examples = _character_voice_examples(project, query)
    repetition_guard = _repetition_guard(
        project, current_index, active_character_names
    )
    recent_summaries = "\n".join(
        f"- {chapter.get('title', '章节')}：{chapter.get('summary', '').strip()}"
        for chapter in chapters[max(0, current_index - 5) : current_index]
        if chapter.get("summary", "").strip()
    )
    continuity_notes = "\n".join(
        f"- {str(item.get('chapter_title', '')).strip() + '：' if str(item.get('chapter_title', '')).strip() else ''}"
        f"{item.get('text', '')}"
        for item in project.get("memory", {}).get("continuity_notes", [])
        if isinstance(item, dict)
        and not item.get("resolved", False)
        and str(item.get("text", "")).strip()
    )
    target_words = int(request.get("target_words") or settings.get("target_words", 1200))
    # Rewrite should preserve the selected passage's scale. The global chapter
    # target (often 1200-3000 chars) must not accidentally turn a 120-char
    # sentence repair into a new full chapter. "expand" intentionally keeps
    # using the explicit/global target.
    if (
        mode == "rewrite"
        and selection.strip()
        and not request.get("_full_chapter_rewrite", False)
    ):
        selected_chars = len(re.sub(r"\s+", "", selection))
        if selected_chars:
            target_words = max(100, selected_chars)
    style_payload = dict(project.get("style", {}))
    reference_style_corpus = combined_style_corpus(project, max_chars=30000)
    if reference_style_corpus:
        style_payload["sample"] = reference_style_corpus
    outline_text = str(project.get("outline", ""))
    master_outline = str(
        project.get("planning", {}).get("master", {}).get("full_outline", "")
    )
    if (
        outline_text.strip()
        and re.sub(r"\s+", "", outline_text)
        == re.sub(r"\s+", "", master_outline)
    ):
        outline_text = "已与AI详细全书大纲同步，详见“分层导演规划”区块。"
    mode_rules = {
        "continue": "从现有正文最后一句自然续写，不重复已有内容，不突然跳时空。",
        "instruction": "按照创作要求写新的正文，并与现有上下文无缝衔接。",
        "rewrite": "只重写选中文本，保留事实、人物意图和剧情功能。",
        "expand": "扩写选中文本，把概述变成完整场景，不改变既定结果。",
    }
    if mode == "rewrite" and not request.get("_full_chapter_rewrite", False):
        length_rule = (
            f"目标长度：约 {target_words} 个中文字符，尽量保持与选中文本同一量级；"
            "不要为了凑字数新增剧情。"
        )
    else:
        minimum_chars = max(80, int(target_words * 0.8))
        maximum_chars = max(minimum_chars + 40, int(target_words * 1.2))
        length_rule = (
            f"目标长度：约 {target_words} 个中文字符，建议落在 {minimum_chars}-{maximum_chars} 字。"
            f"在至少写到约 {minimum_chars} 字以前不要主动总结或提前收束；"
            "若场景尚未完成，应继续用动作、对话、环境变化和可验证的新信息推进，而不是重复描述。"
        )
    task = f"{mode_rules.get(mode, mode_rules['continue'])}\n{length_rule}"
    if instruction:
        task += f"\n作者本次要求：{instruction}"
    if selection:
        task += f"\n待处理文本：\n{selection}"
    if mode in {"continue", "instruction"}:
        task += (
            "\n事实封闭原则：只有上方权威上下文明确写出的过去信息，才可以当作既有事实。"
            "不得新添人物或物品的来历、旧经历、旧约定、习惯、回忆、信件内容或过去时间点；"
            "若上下文没有答案，就保持未知。允许新增的事实只能是本场景此刻直接发生、"
            "被感官观察到或由角色明确说出的内容；人物猜测必须写成未证实的猜测。"
        )

    sections = [
        PromptSection("核心协议", SYSTEM_PROMPT, 100, "system", True),
        PromptSection("前置权威世界规则", "\n\n".join(lore_groups["before_hard"]), 99, "system", True),
        PromptSection("前置世界规则", "\n\n".join(lore_groups["before"]), 96, "system"),
        PromptSection("长期作者意图", project.get("author_intent", ""), 94, required=True),
        PromptSection("近期焦点", project.get("current_focus", ""), 98, required=True),
        PromptSection("本书不可违背规则", project.get("book_rules", ""), 99, required=True),
        PromptSection("叙事与篇幅契约", _narrative_contract(project, current_index), 98, required=True),
        PromptSection("阶段推进硬门", _stage_progression_guard(project, current_index), 100, required=True),
        PromptSection(
            "章节场景契约",
            _chapter_boundary_contract(project, current_index),
            100,
            required=True,
        ),
        PromptSection(
            "作品总纲",
            f"书名：{project.get('title', '')}\n类型：{project.get('genre', '')}\n"
            f"核心构想：{project.get('premise', '')}\n全书大纲：{outline_text}",
            70,
            min_chars=600,
        ),
        PromptSection(
            "分层导演规划",
            render_planning_context(project, current_index),
            99,
            required=True,
        ),
        PromptSection(
            "同人角色原作正典锁", canon_context, 100, required=bool(canon_context), min_chars=900
        ),
        PromptSection(
            "结构化知识约束", knowledge_context, 100, required=bool(knowledge_context), min_chars=700
        ),
        PromptSection(
            "人物与读者知情边界",
            epistemic_context,
            100,
            required=True,
        ),
        PromptSection(
            "激活写作 Skills",
            skills_context,
            91,
            required=False,
            min_chars=500,
        ),
        PromptSection("人物权威状态", characters, 99, required=bool(characters), min_chars=500),
        PromptSection("权威场景世界规则", "\n\n".join(lore_groups["after_hard"]), 99, required=True),
        PromptSection(
            "相关资料证据", reference_context, 88, required=False, min_chars=600
        ),
        PromptSection("激活的世界书", "\n\n".join(lore_groups["after"]), 89),
        PromptSection(
            "全书滚动进展",
            project.get("memory", {}).get("story_so_far", ""),
            93,
            min_chars=500,
        ),
        PromptSection(
            "待确认连续性备注",
            continuity_notes,
            98,
            required=bool(continuity_notes),
        ),
        PromptSection("相关长期记忆", render_memories(memories), 92, min_chars=500),
        PromptSection(
            "伏笔与暗线治理议程",
            thread_agenda,
            99,
            required=bool(thread_agenda),
        ),
        PromptSection("最近章节摘要", recent_summaries, 90, keep_tail=True, min_chars=500),
        PromptSection(
            "样本文风约束",
            render_style_context(style_payload, query, recent_text),
            73,
            min_chars=500,
        ),
        PromptSection(
            "原文局部文风指纹",
            render_fingerprint(recent_text[-5000:]),
            96,
            required=bool(recent_text),
        ),
        PromptSection(
            "文笔执行简报",
            _craft_brief(project, current_index),
            97,
            required=True,
        ),
        PromptSection(
            "本场人物对白范例",
            character_examples,
            94,
            min_chars=300,
        ),
        PromptSection(
            "描写去重复约束",
            repetition_guard,
            98,
            required=True,
        ),
        PromptSection("本章执行计划", _render_plan(current), 99, required=True),
        PromptSection(
            "当前章节与最近正文",
            f"章节：{current.get('title', '')}\n本章摘要：{current.get('summary', '')}\n"
            f"最近正文：\n{recent_text}",
            97,
            required=True,
            keep_tail=True,
        ),
        PromptSection(
            "续写边界锚点",
            _continuation_anchor(current_content)
            if mode in {"continue", "instruction"}
            else "",
            100,
            required=True,
            keep_tail=True,
        ),
        PromptSection("近端权威世界规则", "\n\n".join(lore_groups["near_hard"]), 100, required=True),
        PromptSection("近端世界书", "\n\n".join(lore_groups["near"]), 95),
        PromptSection(
            "本章作者临时注",
            current.get("author_note", ""),
            100,
            required=True,
        ),
        PromptSection("本次任务", task, 100, required=True),
    ]
    sections = [section for section in sections if section.content.strip()]
    context_window = max(1200, int(settings.get("context_budget", 24000)))
    requested_output = max(256, int(settings.get("max_tokens", 3500)))
    safety_margin = min(900, max(300, context_window // 24))
    # llama.cpp counts prompt and generated tokens in the same context window.
    # Reserving output space here avoids late HTTP 400 errors or truncated prose.
    budget = max(800, context_window - requested_output - safety_margin)
    heading_overhead = sum(
        estimate_tokens(section.name) + 6 for section in sections
    ) + 24
    sections, warnings = _fit_sections(
        sections, max(400, budget - heading_overhead)
    )
    warnings = lore_warnings + warnings
    messages = []
    for role in ("system", "user"):
        content = "\n\n".join(
            f"## {section.name}\n{section.content.strip()}"
            for section in sections
            if section.role == role and section.content.strip()
        )
        if content:
            messages.append({"role": role, "content": content})
    tokens = estimate_tokens("\n".join(message["content"] for message in messages))
    return PromptBuild(
        messages,
        lore,
        activated_skills,
        tokens,
        [
            {
                "name": section.name,
                "content": section.content,
                "priority": section.priority,
                "tokens": estimate_tokens(section.content),
                "tokens_before": section.original_tokens,
                "tokens_after": estimate_tokens(section.content),
                "status": section.status,
                "selected": bool(section.content.strip()),
                "reason": section.reason,
                "role": section.role,
                "required": section.required,
            }
            for section in sections
        ],
        memories,
        warnings,
    )
