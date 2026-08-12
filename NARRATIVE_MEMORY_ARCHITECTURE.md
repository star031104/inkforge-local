# 砚火 v0.16：叙事记忆与小说质量架构

## 1. 结论先行

高质量长篇不是让模型“记住全文”，而是让系统在每次写作前编译一份小而准确的当前工作集，并在写作后只提交有正文证据的状态增量。模型上下文再长，也不能替代以下四件事：

1. 区分永久设定、当前状态、计划和已发生事实。
2. 区分客观事实、人物知情、人物猜测和作者秘密。
3. 让伏笔有埋设、推进、延后、回收条件和到期压力，而不只是一个“未结”标签。
4. 让每章在结构上交付一个新状态，并把交付结果写回权威记忆。

砚火 v0.16 因此采用“作者控制 → 分层规划 → 上下文编译 → 正文 → 审计 → 有证据结算”的闭环，而不是一次性超长提示词。

## 2. 公开方案对照与取舍

### SillyTavern（酒馆）

SillyTavern World Info 的有效设计是：条目以关键词/正则按需激活，支持递归关联、插入顺序、不同上下文来源和独立预算。它解决的是“当前对话应看到哪些设定”，而不是完整的小说时间状态。

砚火保留并强化了这条路线：主词任一/全部、二级 AND/NOT、正则、递归、人物/章节作用域、互斥组、hard/soft canon、before/after/near 三种位置和独立 lore token 预算。小说生成要求可复现，因此没有采用随机触发概率和聊天式 timed effect。

### InkOS

InkOS 的关键不在多 Agent 数量，而在状态治理：结构化 JSON 是权威层，Markdown 是人类投影，SQLite 是检索加速；Planner 产生章节 intent，Composer 编译 context/rule-stack/trace；Observer/Reflector 只输出 JSON delta，由代码不可变应用并校验；伏笔有推进与债务治理。

砚火是单机、单模型、8GB 显存取向，不复制重型多 Agent 运行时，但采用了适合本项目的核心原则：权威结构状态、写后增量、证据验证、章节结算单、伏笔议程和可检查的提示词分区。

### Sudowrite Story Bible

Sudowrite 的公开工作流把 Story Bible 作为作品资料中枢，并形成明确的依赖链：Genre/Style、Synopsis、Characters、Worldbuilding 共同约束 Outline，Outline 再拆成以时间、地点和 POV 为边界的 Scenes，最后由 Draft 结合这些资料写正文。它的重要经验是“上游卡片服务于下游场景”，而不是把整本资料库无差别塞进每次生成。

砚火 v0.16 对应采用了章节场景契约、`scene_beats`、POV/时空边界、相关人物与世界条目选择，并进一步补上公开 Story Bible 工作流较少强调的事实有效期、人物知情来源、关系增量、伏笔债务和正文证据链。

### 长篇生成研究

- Re3 证明“结构计划 + 反复注入当前故事状态 + 修订”显著优于直接长生成。
- DOC 把创意负担前移到层级细纲，并用章节级控制器维持细纲一致性。
- DOME 使用动态层级大纲和时序知识图谱，说明静态大纲必须允许在已发生事实约束下做局部适配。
- StoryWriter 使用事件节点、事件关系和动态历史压缩，强调章节规划不能只有主题口号，必须具有事件与人物关系。
- CONCOCT 说明长篇节奏问题本质上与大纲粒度不均有关：关键事件过粗、过场过细都会破坏阅读体验。

## 3. 权威层级

发生冲突时按以下顺序解释，不允许模型自行平均折中：

1. 已接纳正文及有正文证据的结算结果。
2. 人物永久核心、hard 世界规则、本书硬规则。
3. 当前人物/关系/道具/位置/时间状态。
4. 本章作者注、必须保留、必须避免。
5. 本章执行计划。
6. 当前卷和逐章路线。
7. 全书大纲、近期焦点、长期作者意图。
8. soft 世界资料和抽象文风偏好。

“路线卡”只是未来意图；正文一旦接纳，旧路线与事实冲突时必须最小适配路线，不能篡改正文。

## 4. 人物为什么能够长期稳定

### 4.1 三层人物模型

人物卡不再把所有内容塞进一个 description：

- 永久核心：身份背景、人格压力反应、价值取舍、恐惧、内在矛盾、人物弧、硬边界、语言指纹。
- 稳定外貌：2–5 个识别锚点，只是事实库，不要求每次登场复述。
- 动态状态：目标、位置、身体/关系状态、物品、情绪和 `appearance_state`（衣着、伤势、伪装）。

临时衣着和伤势不会覆盖稳定外貌。提示词明确要求每次出场只选与动作、视角或情绪相关的 1–2 个锚点。

### 4.2 人物知情不是全局事实

`knowledge_ledger` 的每一项保存：

- 原子信息；
- 如何得知（亲历、被告知、推断）；
- confirmed 或 suspected；
- 来源章节、章节号和正文证据。

这样“读者知道”“作者知道”“A 知道”与“B 知道”不会混在一起。人物只能依据自身 ledger、开篇知情边界或当前场景中新获得的信息行动。

### 4.3 动态关系独立建模

初始关系视角保留在人物卡；正文造成的当前关系保存在 `memory.relationships`，记录双方、关系事实、具体张力、信任变化、信息差和来源。关系更新包含未知人物或伪造证据时不会写入。

## 5. 世界书为什么不会挤爆上下文

世界书条目必须短、独立、可直接理解。标题和触发词不会自动成为模型上下文，因此 content 自身要说明“这是什么、规则是什么、违反后果是什么”。

激活顺序：

1. 扫描本章要求、路线、相关记忆和最近正文。
2. 激活常驻条目与直接命中条目。
3. 用已激活条目内容做有限递归扫描。
4. 先解决互斥组，再按 canon、常驻、直接命中数和 order 分配预算。
5. hard 条目超预算仍保留；soft 条目可以省略并在提示词预览报告。

制度边界、能力上限、时间地点等稳定规则进入世界书；会随剧情变化的持有人、伤势、关系、权限不能写成永久 world entry，应进入状态记忆。

## 6. 事实、事件和时间

### 6.1 原子事实

每条事实支持：`source_chapter_id`、`evidence`、`valid_from_chapter`、`valid_until_chapter`、`confidence`、`visibility` 和 `supersedes_id`。

新事实替代旧事实时不直接删除旧值，而是停用旧值并关闭有效区间。检索只选择当前章节有效的事实。这样“钥匙在沈砚手中”到“钥匙被仓吏夺走”不会同时作为当前真相注入。

### 6.2 轻量事件图

时间线事件除 time/event 外，还记录 participants、location、causes 和 effects。它不是重型图数据库，但已经足以回答：谁亲历、在哪里发生、直接前因是什么、已经造成什么后果。

### 6.3 章节结算单

每章 `settlement` 保存：目标是否完成、不可逆变化、开放问题、结尾状态、人物更新、事实 ID、线索 ID、关系 ID 和回写警告。滚动摘要用于压缩全局；结算单用于追责和恢复，两者不互相替代。

## 7. 伏笔、线索和暗线

`plot_threads` 采用五阶段：

- `open`：已埋设，尚未形成连续推进；
- `progressing`：本章给出新证据、压力或关系变化；
- `deferred`：作者有意延后，不作为当前债务；
- `ready`：正文条件已基本满足，可以回收；
- `closed`：预期兑现已在正文完成。

每条线索还记录 type、setup、latest、expected_payoff、payoff_condition、target_window、stakeholders、knowledge_holders、创建章和最近推进章。

目标窗口分 immediate、near、mid、slow、endgame。系统按线索年龄、静默章数、全书进度和状态计算：

- 本章必须推进/合理延后；
- 已具备回收条件；
- 可以保持或小幅推进。

关闭线索必须同时具有实际 payoff 和可在正文逐字核验的 evidence，否则自动降为 progressing 并留下警告。项目体检会报告静默债务和“已关闭但没有回收结果”的坏状态。

## 8. 一章应该怎样开始、推进和结束

单章计划现在不仅有 goal/conflict，还包含：chapter_type、pov_character、time_location、opening_beat、scene_beats、emotional_turn、thread_actions、exit_state 和 ending_type。

### 开始

- 新章节从正在变化的动作、感官、对话压力或上一章后果切入。
- 前 10% 内建立“谁在何处想完成什么、眼前阻力是什么”。
- 环境没有因果作用时，不以天气、全景、履历或主题议论热身。
- 续写不是重新开章，第一句必须承接已有最后一句/两段。

### 中段

使用“任务/欲望 → 阻力 → 选择 → 代价 → 新状态”。每一拍由上一拍的结果触发。转折必须改变下一步行动、关系、资源、权限或认知，不能只是再来一条信息。

### 结束

结尾类型在 decision、revelation、reversal、deadline、consequence、image、closure 中选择，避免每章都以陌生人闯入或一句“他不知道……”制造同质悬崖。普通章节停在状态已改变之后；计划终章回答核心问题、兑现主要因果、呈现代价后的稳定状态，不新增需要后文解释的任务。

## 9. 文笔质量如何控制

文笔不能靠“写得优美”四个字。砚火同时使用：

- LLM 提取的可执行风格卡；
- 按当前对话比例、句长和相关性选择的样例；
- 句长均值与波动、段长波动、对白单轮长度、短句比例、感官/抽象词密度等统计指纹；
- 当前章节功能对应的文笔执行简报；
- 全书疲劳词、跨章长句/段落重复和显著描写账本。

执行原则：关键选择前放慢，过场压缩；抽象判断后用动作、物件、空间距离或话语反应落地；对白有当场目的和潜台词，不轮流解释设定；细节必须被使用、误读、损坏或产生代价。统计指纹描述“分布”，不是要求模型机械凑数。

## 10. 上下文编译与 token 预算

必保层包括核心协议、hard 世界规则、本书规则、当前卷/章计划、相关人物状态、章节场景契约、伏笔治理议程、续写锚点和本次任务。可压缩层包括全书大纲、soft lore、滚动摘要、普通检索记忆、风格样例。

检索同时考虑相关度、重要度、近期性、类型配额和内容相似度抑制。旧而无关的线索不会挤掉当前事实，但已经过期的线索债务会被强制带回规划工作集。提示词预览展示每一区块 token 和裁剪警告。

## 11. 推荐工作流

1. 先写长期作者意图、核心问题、结局方向和 6–12 条真正的硬规则。
2. 人物卡优先填价值取舍、压力反应、硬边界和语言指纹；外貌只填少量稳定锚点。
3. 世界书拆成一条一个规则/实体，不把剧情摘要伪装成设定。
4. 生成并审核全书圣经、分卷契约、逐章路线。
5. 每章先生成执行计划，重点检查 opening_beat、scene_beats、exit_state 和 thread_actions。
6. 正文进入草稿区后先审计；确认再“插入并更新记忆”。
7. 查看状态回写警告，尤其是未知人物、证据缺失和线索错误关闭。
8. 每卷末运行项目体检，清理线索债务、记忆来源和结构重复，再进入下一卷。

## 12. 有意保留的边界

- 当前使用中文词项混合检索，没有引入嵌入模型或云向量库，以保证单机速度、可解释性和无额外模型占用。
- 正文证据验证能阻止明显幻觉回写，但不能理解所有同义改写；无 evidence 的旧版/人工数据继续兼容。
- 人物知情和关系依赖状态观察模型抽取，重要转折仍建议人工查看章节结算单。
- 多候选采样、独立角色扮演 Agent 和全稿出版级人工评审不适合默认 8GB 单模型流水线；高风险章节应手动生成多个候选再选择。

## 13. 主要公开参考

- SillyTavern World Info：<https://docs.sillytavern.app/usage/core-concepts/worldinfo/>
- SillyTavern Character Cards / Character Note：<https://docs.sillytavern.app/usage/characters/>
- InkOS：<https://github.com/Narcooo/inkos>
- Sudowrite Story Bible：<https://docs.sudowrite.com/using-sudowrite/1ow1qkGqof9rtcyGnrWUBS/what-is-story-bible/jmWepHcQdJetNrE991fjJC>
- Sudowrite Characters：<https://docs.sudowrite.com/using-sudowrite/1ow1qkGqof9rtcyGnrWUBS/characters/a7tdE1ZB8KvAwMD3Mopwpd>
- Sudowrite Scenes & Draft：<https://docs.sudowrite.com/using-sudowrite/1ow1qkGqof9rtcyGnrWUBS/scenes--chapter-prose/49p5MTVxTKkVFEC5rVUzpY>
- Re3（EMNLP 2022）：<https://aclanthology.org/2022.emnlp-main.296/>
- DOC（ACL 2023）：<https://aclanthology.org/2023.acl-long.190/>
- CONCOCT（EMNLP 2023 Findings）：<https://aclanthology.org/2023.findings-emnlp.723/>
- DOME（NAACL 2025）：<https://aclanthology.org/2025.naacl-long.63/>
- StoryWriter：<https://arxiv.org/abs/2506.16445>
