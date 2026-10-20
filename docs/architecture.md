# 系统架构

InkForge Local 采用本地优先的分层架构。项目 JSON 是作品事实源，SQLite 负责事务、版本、任务和可重建检索索引；模型输出只能形成候选内容或带证据的派生状态。

## 依赖方向

```text
static UI
   ↓ HTTP/SSE
app/api ───────→ app/services ───────→ app/domain
   │                    │                   │
   └────────────→ app/infrastructure        │
                        │                   │
                        └──────────→ app/core ←─────────┘
```

`domain` 不依赖 API、服务、数据库或浏览器状态。`services` 可以组合领域规则与外部能力。`api` 只负责输入验证、状态码和响应协议。`infrastructure` 不决定故事规则。`core` 只提供配置和无副作用的基础工具。

进程入口位于 `app/entrypoints`。源码启动、PyInstaller 便携版和自动部署启动器最终都进入同一个 server 入口，共用日志、浏览器健康等待、主机和端口规则。

## 后端目录

| 路径 | 职责 |
| --- | --- |
| `app/core` | 环境读取、版本、运行路径、请求预算与模型完成回执 |
| `app/api/schemas.py` | 稳定的 HTTP 请求契约 |
| `app/api/routers/projects.py` | 项目生命周期、备份校验恢复、搜索、版本与写作技能 |
| `app/api/routers/system.py` | 健康状态、模型接口测试连通（`/api/models/test`）、模型列表与接口摘要 |
| `app/api/routers/research.py` | 参考资料、文风、正典与知识图谱 |
| `app/api/routers/prompts.py` | 提示词预览、上下文快照与确定性回放 |
| `app/api/routers/chapter_sessions.py` | 章节会话、检查点与回滚 |
| `app/api/routers/generation.py` | 正文流式生成、长度续补与生成事件协议 |
| `app/api/routers/planning.py` | 单章、全书、分卷规划与路线应用 |
| `app/api/routers/editorial.py` | 编辑路由组合入口 `create_editorial_routes`（接纳、契约、审校、修复队列与记忆结算） |
| `app/api/routers/editorial_memory.py` | 接纳、契约、工作流重置、修复队列、记忆提取 / 补提取 / 应用 / 降级、全稿体检 |
| `app/api/routers/editorial_audit.py` | 章节审计（分类审校、跨卷回退检查）与本地质量检查 |
| `app/api/routers/editorial_support.py` | 编辑路由共享的代理、结果类型、新实体识别与审校风险信号 |
| `app/api/routers/incubation.py` | 灵感方案、后台孵化与创作方向推荐 |
| `app/api/routers/director.py` | 自动导演任务的启动、暂停、恢复与查询接口 |
| `app/api/routers/evaluation.py` | 作品评测指标、评测历史与两两盲评接口 |
| `app/domain/planning_validation.py` | 规划、分卷、记忆输出的纯验证规则 |
| `app/domain/route_validation.py` | 章节路线、阶段边界和跨章重复规则 |
| `app/domain/prose.py` | 正文长度、截断、清理和确定性安全门禁 |
| `app/storage/schema/project.py` | 项目默认结构与旧作品的兼容迁移 |
| `app/story/continuity_state.py` | 道具与伤势事件日志的纯状态机与确定性冲突检查 |
| `app/llm/json_schemas.py` | 约束解码使用的宽松 JSON Schema |
| `app/story/evidence.py` | 审校证据编号定位、精确 / 模糊匹配与稿件哈希绑定 |
| `app/evaluation_suite.py` | 长篇评测指标、终局窗口、机器味信号与成对盲评 |
| `app/llm/structured_output.py` | JSON 输出解析、验证和一次有界修复 |
| `app/services/prose_recovery.py` | 输入绑定的正文检查点、有界续补与分场回执复用 |
| `app/llm/providers.py` | 单一 OpenAI 兼容接口：URL 规范化、旧设置迁移、写作/规划模型分工、按服务能力门控的扩展参数与 `register_payload_extension` 扩展点 |
| `app/llm/client.py` | HTTP / SSE 客户端、`probe_endpoint` 连通测试与服务类型识别 |
| `app/llm/errors.py` | 模型错误分类及面向作者的错误说明 |
| `app/services/memory_settlement.py` | 证据绑定的章节记忆结算 |
| `app/services/editorial_policy.py` | 审校门禁、候选排序和修复策略 |
| `app/services/director_planning.py` | 从开书种子到逐章路线的分阶段模型规划 |
| `app/services/director_runtime.py` | 自动导演的稳定公开导入面（`create_director_runtime`、`DirectorDependencies` 等再导出）与测试替换点 |
| `app/services/director/` | 自动导演实现，按关注点拆分，见下文“自动导演模块” |
| `app/services/craft_settings.py` | 正文质量开关的默认值、归一化与任务级覆盖（`critic_budget` 等） |
| `app/services/antislop.py` | 生成时去套话：llama.cpp `logit_bias` 与命中句局部改写 |
| `app/services/candidate_selection.py` | 关键章多候选、确定性筛查与双向成对比较选稿 |
| `app/services/dialogue_rehearsal.py` | 按人物知情边界的对白演出与越界台词过滤 |
| `app/services/focused_critics.py` | ConStory 五类分项审校、风险评分与审校预算（合并 / 全量 / 自动升级） |
| `app/services/rolling_replan.py` | 结算后滚动重规划后续章节路线，跳过作者修改与锁定路线，校验变差即还原 |
| `app/story/memory_extraction.py` | 分阶段章节记忆提取、截断减半重试与只补失败阶段 |
| `app/story/hook_ledger.py` | 章末钩子账本：兑现 / 推进 / 未处理判定、钩子债与过期 |
| `app/services/experience_bank.py` | 按模型统计可信审校问题并生成固定模板的纠偏提示 |
| `app/api/routers/extensions.py` | 旧稿导入、关系图与时间线、正文导出 |
| `app/services/manuscript_import.py` | 旧稿切章、分批设定反推（候选）、作者确认与可暂停的逐章记忆结算 |
| `app/story/story_graph.py` | 按章节时间边界投影人物关系图、事件时间线与伏笔弧线 |
| `app/storage/extension_store.py` | 与作品 JSON 分离的附属记录（导入任务），同库备份 |
| `app/storage/search_index.py` | FTS 文档编译、检索词和快照脱敏 |
| `app/storage/secret_store.py` | 与作品、版本历史和备份隔离的项目凭据存储 |
| `app/storage/automatic_backup.py` | 与 HTTP 请求解耦的启动时及周期数据库备份 |
| `app/entrypoints/server.py` | 统一服务进程、日志和浏览器健康检查入口 |
| `app/storage/project_store.py` | SQLite 仓储、表迁移、事务和版本持久化 |
| `app/main.py` | 兼容入口、生命周期、共享依赖与路由组合 |

`app.main:app` 仍是公开启动入口。拆分过程保留该导入路径，避免破坏启动脚本、测试和第三方调用。

### 自动导演模块

`app/services/director_runtime.py` 只保留公开导入面：`create_director_runtime`、`DirectorDependencies`、`DirectorRuntime`、`DirectorControlError`、`TaskStoppedError`、`run_while_running` 等从这里导入。实现位于 `app/services/director/`：

| 模块 | 职责 |
| --- | --- |
| `common.py` | 常量、`DirectorDependencies` / `DirectorRuntime` 数据类、错误类型与纯函数（`_craft_overrides`、`_known_audit_score` 等） |
| `context.py` | `DirectorContext`：各模块共享的命名空间 |
| `state.py` | 任务保存与续租、事件、规划债 / 质量债及其对账 |
| `support.py` | 章节记忆结算与补提取、经验库、钩子账本、滚动重规划 |
| `prose.py` | 正文生成：分场紧凑提示词、对白演出、去套话、关键章多候选、证据段落局部修补 |
| `refinement.py` | 全书 AI 精修与收敛判断 |
| `incubator.py` | 灵感孵化任务与开书阶段（骨架、人物、世界书） |
| `master.py` | 全书规划与分卷逐章路线阶段 |
| `chapters.py` | 逐章“写作 → 审计 → 修订 → 锁定 → 记忆结算”循环 |
| `runner.py` | 任务执行器：租约心跳、阶段分派、暂停 / 失败处理、启动 |
| `control.py` | HTTP 侧控制：启动、按规划续写、精修、查询、暂停、恢复 |
| `runtime.py` | 按依赖顺序安装上述模块并组装 `DirectorRuntime` |

每个模块提供 `install(ctx)`，在 `DirectorContext` 上定义并注册自己的闭包；后安装的模块从 `ctx` 读取先安装模块的函数。外部依赖仍通过 `DirectorDependencies` 延迟绑定。`chat_stream`、`chat_once`、`run_structured_completion`、`_director_candidate_gate_failures` 和 `repair_incomplete_memory` 每次调用时都从 `director_runtime` 模块属性解析，因此测试对该模块的 `monkeypatch` 仍然生效。编辑路由用同样方式处理 `editorial.repair_incomplete_memory`。

用量归属：任务执行器启动时把调用绑定到作品（`bind_scope`，包括恢复的任务）；逐章循环和全书精修把每一章的写作、审计、修订与记忆结算包在 `usage_scope(project_id, chapter_id)` 中，按章统计费用。

分场紧凑提示词与主正文提示词使用同一组渲染函数：本章 `debate` 交锋结构（`render_debate_for_prompt`），以及有 `story_date` 时的故事内时间（`history_tools.render_story_date`）。两者中的明确数量计入本段允许使用的数字。普通章节两者都为空，提示词不变。

## 前端目录

| 路径 | 职责 |
| --- | --- |
| `static/js/core/namespace.js` | 最先加载：定义 `window.Inkforge` 与 `Inkforge.register(name, api)` |
| `static/app.js` | 全局状态、兼容检查和共享 UI 基础 |
| `static/js/core/project.js` | 作品加载、收集、保存与章节渲染 |
| `static/js/core/generation.js` | 模型检查、提示词、正文生成和候选稿 |
| `static/js/core/stream.js` | SSE 解码与完成回执校验；提前 EOF 不标记成功 |
| `static/js/core/dialogs.js` | 设置和危险操作确认窗口 |
| `static/js/features/planning.js` | 全书、分卷与单章规划 |
| `static/js/features/resources.js` | 资料、正典、知识图谱、人物与世界书 |
| `static/js/features/editorial.js` | 审校、修订、质量检查和记忆接纳 |
| `static/js/features/governance.js` | 契约、权威、版本、导出和项目体检 |
| `static/js/features/director.js` | 自动导演（含单次审校预算）、自动精修和灵感孵化 |
| `static/js/core/usage.js` | 用量与费用查询（`window.inkforgeUsage`，同时注册为 `Inkforge.usage`） |
| `static/js/features/wizard.js` | 新书向导与空状态入口 |
| `static/js/features/history-debate.js` | 交锋结构工作台、时代称谓表与修订复核提示 |
| `static/js/features/evaluation.js` | 质量评测面板：指标卡片、历史与盲评 |
| `static/js/features/manuscript-import.js` | 旧稿导入向导、设定候选确认与任务进度 |
| `static/js/features/visualize.js` | SVG 力导向关系图与伏笔时间线（无外部库） |
| `static/workspace.js` | 场景、作者偏好、调用统计和候选评测 |
| `static/js/bootstrap.js` | DOM 事件绑定和应用启动 |

脚本使用经典浏览器全局作用域以保持无构建工具运行。加载顺序由 `static/index.html` 明确规定；功能文件只声明函数，最后由 `bootstrap.js` 绑定事件并启动。

### 前端命名空间约定

- 不改为 ES 模块，也不引入打包工具。`static/js/core/namespace.js` 是第一个加载的脚本，定义 `window.Inkforge` 和 `Inkforge.register(name, api)`。`register` 会合并到同名条目，并返回该条目。
- 功能文件在末尾注册自己的公开函数：`Inkforge.director`、`Inkforge.planning`、`Inkforge.wizard`、`Inkforge.historyDebate`、`Inkforge.usage`。原有全局函数和 `window.inkforgeUsage` 继续保留，以兼容现有调用。
- 新代码跨文件调用时，优先使用 `Inkforge.<feature>.<fn>()`。只在文件内部使用的辅助函数不注册。
- 新增脚本时在 `index.html` 中带 `?v=<APP_VERSION>` 引用，并放在 `namespace.js` 之后。

## 数据与一致性

1. 浏览器编辑基于 `updated_at` 做乐观并发控制。
2. SQLite 在同一事务内比较版本、保存项目、章节版本和检索投影。
3. 正文接纳后按正文哈希创建记忆提交。
4. 修改已结算章节会使受影响后缀失效；重建必须按章顺序执行。
5. FTS 是可删除重建的投影，不是权威数据源。
6. 模型调用统计存入独立数据库，不保存正文、提示词、地址或密钥。
7. 模型只有一个 OpenAI 兼容接口（`settings.base_url`），`settings.model` 用于写作类任务，`settings.planning_model` 用于规划、审校、提取与研究（空值表示同一模型）；扩展参数由连通测试写入的 `settings.capabilities` 决定是否发送。模型凭据只进入独立 secrets 数据库；作品、历史版本、上下文快照和自动备份均不持有 Key。
8. 自动导演依赖通过 `DirectorDependencies` 显式声明，并保留可测试的延迟绑定，不再按字符串从 `main.py` 查找函数。
9. 数据库恢复只接受备份目录中的合法文件；恢复前创建安全快照，恢复后再次校验，任一步失败都会用安全快照回滚。
10. 定时备份由应用生命周期管理，不依赖作者是否触发某个保存接口；自动备份和手动备份分别轮换。
11. 完整模型请求按输入、输出预留、消息开销和安全余量统一检查。估算明确标记，续补与 JSON 修复会重新计算剩余输出空间；预算不足时保留原始材料并停止。
12. `finish_reason=length/max_tokens` 独立于标点判断。自动导演最多执行 1–5 次接收尝试，默认 3 次；已收到正文进入任务检查点，完成确认后仍须经过原有审校、接纳与记忆结算。
13. 正文流检查点按完整消息与模型生成设置的哈希绑定，同章最多保留 32 个场景回执；首个片段立即保存，后续随片段到达约每 2 秒保存一次，暂停、断线与完成时立即保存。进程突然退出可能损失最近一次保存后收到的片段。章完成后清理；输入变化时不复用旧流回执。
14. 硬约束不按字符截断；Skill 的方法步骤和正反例作为完整单元取舍。上下文快照记录实际注入标记和完整请求预算。
15. 导入任务、读者报告、发布节奏写入 `project_extensions` 附属表，不进入作品 JSON，因此后台任务不会与浏览器的乐观并发保存冲突；删除作品时一并清理。
16. 审校草稿按 `[P段-S句]` 编号送审，模型返回 `evidence_ids`，本地按编号取回原文；引文依次尝试精确匹配、模糊匹配（相似度 ≥ 0.9）。保存的证据始终是正文原句，并绑定被审稿件哈希，不能拿 A 稿的问题去验证 B 稿。审校分数小于等于 10 时按十分制换算，空分数按问题严重度推算。
17. 章节记忆按“摘要与结算 / 原子事实 / 人物状态 / 伏笔与时间线 / 关系”分阶段提取（`app/story/memory_extraction.py`），章节正文作为相同前缀共享。输出未闭合时，按减半条目上限重新提取，不修补残缺 JSON；可选阶段失败只记警告。遥测按任务类型统计 `length_rate`，超过 20% 时告警。
18. 时代语汇门禁读取 `governance.term_allowlist` 与 `governance.pov_modern_allowed`。后者为空时，根据设定中的穿越、重生、现代人等词推断；开启后，叙述中的现代术语按 1/3 计入，对白中的按原值计入。本书高频短语由已接纳正文自动学习，写入 `memory.learned_fatigue_phrases`（可重建），不再写死在源码中。
19. 全书精修在全稿门禁失败时，优先精修贡献度最高的章节。连续三轮定稿分提升不到 1 分，或未通过项重复出现时，停止重跑，标记 `release_status=needs_author` 并列出原因；作品被人工修改后才能再次启动，也可以传 `force` 强制继续。
20. 正文质量开关集中在 `app/services/craft_settings.py`，保存在 `project.settings`，自动导演任务的 `config` 可逐项覆盖：`antislop_enabled`、`antislop_max_rewrites`、`antislop_logit_bias`、`candidate_count`（1–3）、`candidate_scope`（key/all）、`dialogue_rehearsal`（auto/always/off）、`critic_mode`（focused/single）、`rolling_replan`、`rolling_replan_window`。作者禁用短语保存在 `governance.banned_phrases`。
21. 去套话（`app/services/antislop.py`）：禁用词由三部分组成——作者禁用短语（一次也不允许）、通用 AI 套话和本书自动学习的高频短语（各允许出现一次）。llama.cpp 上，先经 `/tokenize` 解析出多字词元，再施加负向 `logit_bias`，单个常用字不受影响。所有服务在生成后只把超额出现的句子连同前后各一句，一次性送去局部改写，再拼回原文；改写仍含套话、长度异常或含换行时保留原句，从不因套话重写整章。手动生成通过 SSE `replace` 事件替换候选稿，候选稿语义不变。
22. 关键章多候选（`app/services/candidate_selection.py`）：开篇章、末章，以及路线或计划中带“高潮 / 反转 / 卷末”等标记或 `key_scene` 的章节，按 `candidate_count` 生成多份候选。每份候选带不同的变体提示，保证流检查点不会互相复用。先做确定性筛查（篇幅、套话、重复句、截断），再由规划模型两两比较（因果、人物能动性、对白辨识度、新鲜度、计划交付）；呈现顺序按内容哈希交换，以分散位置偏差。全部候选和比较理由写入任务的 `chapter_candidates`。修订时优先按证据局部修补问题段落，只有找不到可定位的证据段落才整章重写。
23. 对白演出（`app/services/dialogue_rehearsal.py`）：路线中点名的主要人物达到两个或以上时，每人单独调用一次，只能看到自己的档案、声纹样本和截至本章的知情范围。其他人物的 secrets、author_only / private 事实一律不提供。产出的意图、台词与动作作为“对白演出素材”注入正文提示词；与不可知事实共享两个以上四字片段的台词会被拦截。
24. 分类审校（`app/services/focused_critics.py`）：按 ConStory 的五类错误分别审校——人物记忆与能力、事实细节、视角与语气、时间线 / 因果 / 弃坑、世界规则与地理。每个审校器只拿到本类所需的上下文，按块限长；系统提示与编号草稿完全相同，构成可缓存前缀。各类结果合并为原有审计格式，`critic_mode=single` 保留旧的单次审校。
25. 稳定前缀：正文提示词的 system 消息只包含核心协议，同一作品内逐字节不变；按关键词激活的世界书移到 user 消息开头。llama.cpp 请求默认带 `cache_prompt`。
26. 约束解码：`structured_completion(..., schema=...)` 在服务支持时启用 JSON Schema 约束——llama.cpp 用 `json_schema`，vLLM 等用 `response_format.json_schema`——否则仍用 `json_object` 加有界修复。宽松 Schema 集中在 `app/llm/json_schemas.py`，覆盖审校、分类审校、章节计划、分阶段记忆、去套话、候选比较、对白演出和滚动重规划。
27. 道具与伤势状态机（`app/story/continuity_state.py`）：记忆提取新增“道具与伤势”阶段，事件按章节号写入 `memory.item_events` / `memory.injury_events`，必须带可核验证据，重新结算同一章时会先替换旧事件。当前状态由早于本章的事件折叠得出，自动遵守章节时间边界，并注入提示词。审校时做确定性检查：已遗失或已毁坏的道具在没有找回 / 修复描写的情况下被使用，或新伤在最短恢复章数内被写成痊愈，都会报出问题。
28. 滚动重规划（`app/services/rolling_replan.py`）：每章通过并结算后，根据实际摘要与场景结算，微调后续最多 `rolling_replan_window` 章未写、未锁定路线的 goal / conflict / turning_point / ending_hook。不跨卷，不改卷契约。调整后重新跑路线校验，结果变差则整体还原。字段级差异写入任务的 `replan_history` 和事件日志。
29. 钩子欠账（`app/story/hook_ledger.py`）：章节接纳时把计划中的 `ending_hook` 记入 `memory.hook_ledger`，记录按章节 ID 保存，位置由当前章节顺序重新推导；后续章节的评价同样以章节 ID 为键，因此插入或删除章节不会错位。旧版以章号为键的评价在首次读写时，按记录缓存的 `origin_chapter` 与当前位置之差平移后迁移为章节 ID，超出全书范围的丢弃。超过 `hook_window` 仍未处理的钩子成为钩子债注入下一章，超过若干窗口自动过期。
30. 模型经验库（`app/services/experience_bank.py`）：只统计证据已核验或本地检测出的问题，`count` 为出现过的不同章节数（`chapter_ids`），同章反复审校不累计；权重按新章节衰减。缺少 `chapter_ids` 的旧记录按原 `count` 补入 `#legacy1…N` 占位 ID（不超过窗口），服务端与浏览器端合并结果一致。提示词只使用固定模板，模型原文只在界面展示。
32. 审校预算：`critic_budget` 为 `auto` 时按风险信号评分，低风险合并为一次审校、高风险运行五个分项审校器，合并审校发现需升级的问题时自动改为全量；`full` / `light` 固定预算。一键创作启动请求可带可选的 `critic_budget`，写入任务 `config` 后仅对本次运行生效。

新增行为与 DeepWrite 的对照、验证范围和后续优先级见 [长篇稳定性与创作质量优化报告](deepwrite-optimization.md)。

## 发布形态

- `dist/InkForge` 是带齐运行环境的便携版，适合完全离线分发。
- `dist/InkForge-Deploy` 是体积更小的自动部署版，首次启动按需安装 Python 和锁定依赖。
- 两种形态都把作品、凭据、日志和备份放入用户数据目录，不写入只读安装目录。
- 自动部署启动器对官方 Python 安装包校验固定 SHA-256，依赖环境由应用版本和 `requirements.txt` 指纹隔离。

## 扩展规则

- 新 HTTP 能力先定义请求模型，再放入对应路由；不要继续向 `main.py` 添加简单 CRUD。
- 新故事约束应实现为无 I/O 的领域函数，并为边界条件增加测试。
- 新模型工作流放入 `services`，显式注入仓储或模型调用函数。
- 新前端能力放入最接近的 `core` 或 `features` 文件，公开函数用 `Inkforge.register` 注册；启动绑定只放在 `bootstrap.js`。
- 数据库表或字段迁移必须幂等，并兼容旧项目 JSON。

长篇评测指标、基准集与运行器见 [长篇评测体系](evaluation.md)；实现位于 `app/evaluation_suite.py`。
