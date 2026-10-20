# 系统架构

InkForge Local 采用本地优先的分层架构。项目 JSON 是作品事实源，SQLite 负责事务、版本、任务和可重建检索索引；模型输出只能形成候选内容或带证据的派生状态。

## 依赖方向

```text
static UI ──HTTP/SSE──→ app/main.py ──→ app/api（routers + container）
                                            │
            ┌───────────────────────────────┼───────────────────────────┐
            ↓                               ↓                           ↓
      app/director                       app/io                  app/evaluation
            │                               │                           │
            └──────────────→ app/planning ⇄ app/writing ⇄ app/review ←─┘
                                            │
                                            ↓
                                      app/storage
                                            │
                                            ↓
                                       app/story
                                            │
                                            ↓
                                        app/llm
                                            │
                                            ↓
                                        app/core
```

规则由 `tests/test_architecture.py` 强制：

- `core` 不导入任何其他 app 包；`llm` 只导入 `core`；`story` 只导入 `core`、`llm`；`storage` 只导入 `core`、`llm`、`story`。
- `planning`、`writing`、`review` 是同层功能包，可以互相引用，可以使用 `core / llm / story / storage`，但不得导入 `director`、`io`、`evaluation`、`api`。
- `director`、`io`、`evaluation` 使用功能包和基础包，彼此不引用，也不导入 `api`。
- 只有 `app/api` 与 `app/main.py` 可以导入 `app.api`；FastAPI / Starlette 只出现在这两处。
- `app/` 根目录只保留 `__init__.py`、`__main__.py`、`main.py`；每个包的 `__init__.py` 用文档字符串说明职责；禁止相对导入。

进程入口位于 `app/entrypoints`。源码启动（`python -m app`、`run.bat` / `run.ps1`）、PyInstaller 便携版和自动部署启动器最终都进入同一个 server 入口，以 `app.main:app` 启动。

## 后端包

| 包 | 职责 | 主要模块 |
| --- | --- | --- |
| `app/core` | 配置、版本与运行路径、日志、文本与类型转换、时钟、通用 JSON Schema 校验 | `config` `logging` `text` `coercion` `clock` `schema_validation` |
| `app/llm` | 单一 OpenAI 兼容接口与能力门控、HTTP/SSE 客户端、结构化输出与有界修复、调用记录与用量、模型错误分类、请求预算、完成回执、约束解码 Schema | `providers` `client` `structured_output` `telemetry` `errors` `budget` `completion` `json_schemas` |
| `app/storage` | SQLite 仓储（`ProjectStore` 及其混入：结构迁移、导演任务、快照与会话、全文检索、数据库备份）、项目文档 Schema 与兼容迁移、凭据、检索文档编译、附属记录、自动备份、备份位置、实例锁 | `project_store` `sqlite_schema` `director_tasks` `snapshots` `project_search` `database_backup` `schema/` `secret_store` `search_index` `extension_store` `automatic_backup` `backup_location` `instance_lock` `project_service` |
| `app/story` | 故事事实层：长期记忆与完整性、分阶段记忆提取、知识层、世界书、同人 Canon、参考资料、章节时间边界、道具伤势状态机、人物知情边界、权威版本与考据、钩子账本、关系图投影、交锋结构、历史时间与称谓、审校证据定位 | `memory` `memory_integrity` `memory_extraction` `knowledge` `lore` `canon` `references` `temporal_context` `continuity_state` `epistemics` `story_systems` `hook_ledger` `story_graph` `debate` `history` `evidence` |
| `app/planning` | 规划提示词、规划模型与本地降级规划、题材画像与新书模板、叙事策略、规划与路线校验、滚动重规划、导演开书规划、本地灵感方向 | `prompts` `model` `genre_profiles` `genre_templates` `narrative_policy` `validation` `route_validation` `rolling_replan` `director_planning` `ideas` |
| `app/writing` | 正文提示词与分层上下文编排、任务提示策略、章节会话、正文规则与续补恢复、去套话、对白演出、多候选选稿、分支试写、文风引擎、写作 Skills、场景卡与作者偏好、写作开关、章节动作参数 | `prompts` `context_builder` `prompt_policy` `chapter_session` `prose` `prose_recovery` `antislop` `dialogue_rehearsal` `candidate_selection` `branch_trials` `style_engine` `skills` `workspace` `craft_settings` `requests` |
| `app/review` | 审计提示词、本地质检、全稿质量分析、分项审校、评分校准、整卷回顾、审校门禁与精修策略、编辑工作流（接纳、修复队列、锁定）、硬契约、模型经验库、证据绑定的章节记忆结算 | `prompts` `local_quality` `manuscript_quality` `focused_critics` `calibration` `volume_review` `editorial_policy` `editorial_workflow` `contracts` `experience_bank` `memory_settlement` |
| `app/evaluation` | 候选稿机械评测与提示词回归、长篇评测指标、终局窗口、机器味信号与成对盲评 | `candidates` `longform` |
| `app/director` | 自动导演运行时，见下文“自动导演模块” | `service` `runtime` `chapters` … |
| `app/io` | 旧稿导入、参考与旧稿文件解析、TXT / EPUB / DOCX 正文导出、导出前清理、GitHub 私人仓库备份 | `manuscript_import` `file_parsing` `export_formats` `export_cleanup` `github_backup` `github_backup_layout` `github_client` |
| `app/api` | `container`（进程单例、`structured_completion` 门面、章节查找 / 受管保存 / 检索附加 / 上下文快照等跨路由回调、导演接线）、`schemas`（HTTP 请求契约）、`routers/*`（每个产品区域一个 `create_*_router` 工厂） | `container` `schemas` `routers/` |
| `app/main.py` | 应用工厂：生命周期（实例锁、导演租约清理、定时备份）、静态资源挂载与按固定顺序注册路由 | — |
| `app/entrypoints` | 统一服务进程、日志和浏览器健康检查入口 | `server` |
| `app/demo` | 首启演示作品《渡口灯》 | — |

项目文档中规划、评分校准样本和写作 Skills 三部分的**数据形状**（默认值与归一化）放在 `app/storage/schema/`，功能模块从那里导入，从而保证存储层不依赖功能层。归一化只补默认值、清理已知字段，旧版本或新版本写入的未知键原样保留。

### 请求流程：写一章（手动）

1. **章节计划**：`features/planning/planning.js` → `POST /api/chapter/plan`（`api/routers/planning.py`）→ `planning.prompts.CHAPTER_PLAN_PROMPT` + `story.epistemics` / `story.temporal_context` 渲染当前章之前的事实 → `container.structured_completion` → `llm.structured_output`（JSON Schema 约束、有界修复）→ `planning.validation` 校验。
2. **生成正文**：`features/writing/generation.js` → `POST /api/generate`（`api/routers/generation.py`）→ `container._attach_indexed_retrieval`（`storage.project_search` 检索 + 写作 Skills）→ `writing.context_builder.build_prompt` 分层编排（世界书、Canon、知识层、记忆、钩子债、经验提示、Skills、预算裁剪）→ `container._prepare_prose_build`（稳定前缀、`llm.client.validate_context_budget`）→ `llm.client.chat_stream` SSE 流；`writing.prose_recovery` 负责截断续补，`writing.antislop` 局部改写套话 → 候选稿回到浏览器，不直接覆盖正文。
3. **审校**：`features/review/editorial.js` → `POST /api/chapter/audit`（`api/routers/editorial_audit.py`）→ `review.local_quality` 本地检查 + `review.focused_critics` 分项审校（或 `review.prompts.AUDIT_PROMPT` 单次审校）→ `story.evidence` 按编号取回证据原句 → `review.editorial_policy` 门禁；可选 `POST /api/generate` 修订。
4. **接纳与记忆**：`POST /api/chapter/accept`（锁定正文、`review.editorial_workflow`）→ `POST /api/chapter/memory`（`story.memory_extraction` 分阶段提取）→ `POST /api/chapter/memory/apply`（`review.memory_settlement` 证据绑定结算、`story.hook_ledger` 钩子账本、`review.experience_bank`）→ `container._persist_managed_project` → `storage.project_store.save`（乐观并发、章节版本、检索投影）。

### 请求流程：自动导演

1. `features/director/director.js` → `POST /api/director/start`（`api/routers/director.py`，请求契约 `director.requests.AutoDirectorStartRequest`）→ `director.control` 创建任务（`storage.director_tasks`）→ `container._launch_director` 在后台启动 `director.runner`。
2. `director.runner` 持有任务租约，按阶段分派：`incubator`（`planning.director_planning` 开书骨架、人物卡、世界书）→ `master`（全书圣经、分卷契约、分卷蓝图）→ `volumes`（逐章路线，`planning.route_validation` 校验）→ `chapters`。
3. `director.chapters` 逐章循环：`container.chapter_plan` → `director.prose`（分场提示词、`writing.dialogue_rehearsal`、`writing.candidate_selection`、`writing.antislop`，模型调用经 `director.service.chat_stream` 延迟解析）→ `container.chapter_audit` → 修订 → 锁定 → `container.chapter_memory` + `review.memory_settlement` → `planning.rolling_replan` 微调后续路线；每步写检查点，可暂停 / 恢复。
4. 全书完成后 `director.refinement` 按定稿门禁（`review.editorial_policy`、`review.manuscript_quality`）逐章精修，收敛停滞时以 `needs_author` 停下。前端轮询 `GET /api/director/tasks/{id}` 显示进度。

### 自动导演模块

`app/director/service.py`（原 `app.services.director_runtime`）只保留公开导入面：`create_director_runtime`、`DirectorDependencies`、`DirectorRuntime`、`DirectorControlError`、`TaskStoppedError`、`run_while_running` 等从这里导入。实现位于 `app/director/`：

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
| `requests.py` | 启动、精修、按规划续写的请求契约（路由与导演共用，导演不导入 `app.api`） |
| `service.py` | 公开导入面与测试替换点 |

每个模块提供 `install(ctx)`，在 `DirectorContext` 上定义并注册自己的闭包；后安装的模块从 `ctx` 读取先安装模块的函数。外部依赖仍通过 `DirectorDependencies` 延迟绑定。`chat_stream`、`chat_once`、`run_structured_completion`、`_director_candidate_gate_failures` 和 `repair_incomplete_memory` 每次调用时都从 `app.director.service` 模块属性解析，因此测试对该模块的 `monkeypatch` 仍然生效。编辑路由用同样方式处理 `editorial.repair_incomplete_memory`。

用量归属：任务执行器启动时把调用绑定到作品（`bind_scope`，包括恢复的任务）；逐章循环和全书精修把每一章的写作、审计、修订与记忆结算包在 `usage_scope(project_id, chapter_id)` 中，按章统计费用。

分场紧凑提示词与主正文提示词使用同一组渲染函数：本章 `debate` 交锋结构（`render_debate_for_prompt`），以及有 `story_date` 时的故事内时间（`app.story.history.render_story_date`）。两者中的明确数量计入本段允许使用的数字。普通章节两者都为空，提示词不变。

## 前端目录

| 路径 | 职责 |
| --- | --- |
| `static/js/core/namespace.js` | 最先加载：定义 `window.Inkforge` 与 `Inkforge.register(name, api)` |
| `static/js/core/theme.js` | 主题偏好，在 `<head>` 中同步加载 |
| `static/js/core/state.js` | 全局状态、`api()` / `toast()`、项目归一化、后端兼容检查（`REQUIRED_API_SCHEMA`）和共享 UI 基础 |
| `static/js/core/project.js` | 作品加载、收集、保存与章节渲染 |
| `static/js/core/stream.js` | SSE 解码与完成回执校验；提前 EOF 不标记成功 |
| `static/js/core/dialogs.js` | 新建 / 重命名作品与删除确认窗口 |
| `static/js/features/settings/` | `model-settings.js` 模型与服务设置、`usage.js` 用量与费用（`window.inkforgeUsage` / `Inkforge.usage`） |
| `static/js/features/planning/` | `planning.js` 全书 / 分卷 / 单章规划、`wizard.js` 新书向导 |
| `static/js/features/writing/` | `generation.js` 提示词预览与正文生成、`workspace.js` 场景与作者偏好、调用统计、候选评测、`branches.js` 分支试写 |
| `static/js/features/story/` | `resources.js` 资料 / Canon / 知识图谱 / 人物卡 / 世界书、`memory.js` 故事记忆、`story-graph.js` 关系图与时间线、`history-debate.js` 交锋与时代称谓 |
| `static/js/features/review/` | `editorial.js` 审校 / 修订 / 接纳、`governance.js` 契约 / 权威 / 专业工作台 / 项目体检、`calibration.js` 评分校准、`volume-review.js` 整卷回顾 |
| `static/js/features/director/` | `director.js` 自动导演、自动精修与灵感孵化 |
| `static/js/features/io/` | `export-panel.js` 导出面板、`manuscript-export.js` 正文导出、`export-cleanup.js` 导出前检查、`manuscript-import.js` 旧稿导入、`github-backup.js` GitHub 备份 |
| `static/js/features/storage/` | `versions-backups.js` 版本历史与备份恢复中心 |
| `static/js/features/evaluation/` | `evaluation.js` 质量评测面板 |
| `static/js/features/onboarding/` | `onboarding.js` 首启欢迎页与上手清单 |
| `static/js/bootstrap.js` | DOM 事件绑定和应用启动（最后加载） |
| `static/css/base/` | `tokens.css` 颜色令牌（唯一允许出现颜色值的文件）、`layout.css` 重置 / 控件 / 应用外壳 / 对话框 / 通用卡片、`components.css` 统一视觉层 |
| `static/css/features/` | 每个功能一份样式 |

脚本使用经典浏览器全局作用域以保持无构建工具运行，加载顺序由 `static/index.html` 明确规定。样式顺序为 `base/tokens → base/layout → 工作区功能样式 → base/components → 较新的功能样式`，与重构前的层叠顺序一致；新增功能样式放在 `base/components.css` 之后。所有 `/assets/...` 地址都带 `?v=<APP_VERSION>`。

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
8. 自动导演依赖通过 `DirectorDependencies` 显式声明，由 `app/api/container.py` 以延迟绑定（`lambda: container.chapter_audit` 等）注入，测试替换容器属性即可生效。
9. 数据库恢复只接受备份目录中的合法文件；恢复前创建安全快照，恢复后再次校验，任一步失败都会用安全快照回滚。
10. 定时备份由应用生命周期管理，不依赖作者是否触发某个保存接口；自动备份和手动备份分别轮换。
11. 完整模型请求按输入、输出预留、消息开销和安全余量统一检查。估算明确标记，续补与 JSON 修复会重新计算剩余输出空间；预算不足时保留原始材料并停止。
12. `finish_reason=length/max_tokens` 独立于标点判断。自动导演最多执行 1–5 次接收尝试，默认 3 次；已收到正文进入任务检查点，完成确认后仍须经过原有审校、接纳与记忆结算。
13. 正文流检查点按完整消息与模型生成设置的哈希绑定，同章最多保留 32 个场景回执；首个片段立即保存，后续随片段到达约每 2 秒保存一次，暂停、断线与完成时立即保存。进程突然退出可能损失最近一次保存后收到的片段。章完成后清理；输入变化时不复用旧流回执。
14. 硬约束不按字符截断；Skill 的方法步骤和正反例作为完整单元取舍。上下文快照记录实际注入标记和完整请求预算。
15. 导入任务写入 `project_extensions` 附属表（v0.37 删除的读者报告、发布节奏等旧记录原样保留，不读取也不删除），不进入作品 JSON，因此后台任务不会与浏览器的乐观并发保存冲突；删除作品时一并清理。
16. 审校草稿按 `[P段-S句]` 编号送审，模型返回 `evidence_ids`，本地按编号取回原文；引文依次尝试精确匹配、模糊匹配（相似度 ≥ 0.9）。保存的证据始终是正文原句，并绑定被审稿件哈希，不能拿 A 稿的问题去验证 B 稿。审校分数小于等于 10 时按十分制换算，空分数按问题严重度推算。
17. 章节记忆按“摘要与结算 / 原子事实 / 人物状态 / 伏笔与时间线 / 关系”分阶段提取（`app/story/memory_extraction.py`），章节正文作为相同前缀共享。输出未闭合时，按减半条目上限重新提取，不修补残缺 JSON；可选阶段失败只记警告。遥测按任务类型统计 `length_rate`，超过 20% 时告警。
18. 时代语汇门禁读取 `governance.term_allowlist` 与 `governance.pov_modern_allowed`。后者为空时，根据设定中的穿越、重生、现代人等词推断；开启后，叙述中的现代术语按 1/3 计入，对白中的按原值计入。本书高频短语由已接纳正文自动学习，写入 `memory.learned_fatigue_phrases`（可重建），不再写死在源码中。
19. 全书精修在全稿门禁失败时，优先精修贡献度最高的章节。连续三轮定稿分提升不到 1 分，或未通过项重复出现时，停止重跑，标记 `release_status=needs_author` 并列出原因；作品被人工修改后才能再次启动，也可以传 `force` 强制继续。
20. 正文质量开关集中在 `app/writing/craft_settings.py`，保存在 `project.settings`，自动导演任务的 `config` 可逐项覆盖：`antislop_enabled`、`antislop_max_rewrites`、`antislop_logit_bias`、`candidate_count`（1–3）、`candidate_scope`（key/all）、`dialogue_rehearsal`（auto/always/off）、`critic_mode`（focused/single）、`rolling_replan`、`rolling_replan_window`。作者禁用短语保存在 `governance.banned_phrases`。
21. 去套话（`app/writing/antislop.py`）：禁用词由三部分组成——作者禁用短语（一次也不允许）、通用 AI 套话和本书自动学习的高频短语（各允许出现一次）。llama.cpp 上，先经 `/tokenize` 解析出多字词元，再施加负向 `logit_bias`，单个常用字不受影响。所有服务在生成后只把超额出现的句子连同前后各一句，一次性送去局部改写，再拼回原文；改写仍含套话、长度异常或含换行时保留原句，从不因套话重写整章。手动生成通过 SSE `replace` 事件替换候选稿，候选稿语义不变。
22. 关键章多候选（`app/writing/candidate_selection.py`）：开篇章、末章，以及路线或计划中带“高潮 / 反转 / 卷末”等标记或 `key_scene` 的章节，按 `candidate_count` 生成多份候选。每份候选带不同的变体提示，保证流检查点不会互相复用。先做确定性筛查（篇幅、套话、重复句、截断），再由规划模型两两比较（因果、人物能动性、对白辨识度、新鲜度、计划交付）；呈现顺序按内容哈希交换，以分散位置偏差。全部候选和比较理由写入任务的 `chapter_candidates`。修订时优先按证据局部修补问题段落，只有找不到可定位的证据段落才整章重写。
23. 对白演出（`app/writing/dialogue_rehearsal.py`）：路线中点名的主要人物达到两个或以上时，每人单独调用一次，只能看到自己的档案、声纹样本和截至本章的知情范围。其他人物的 secrets、author_only / private 事实一律不提供。产出的意图、台词与动作作为“对白演出素材”注入正文提示词；与不可知事实共享两个以上四字片段的台词会被拦截。
24. 分类审校（`app/review/focused_critics.py`）：按 ConStory 的五类错误分别审校——人物记忆与能力、事实细节、视角与语气、时间线 / 因果 / 弃坑、世界规则与地理。每个审校器只拿到本类所需的上下文，按块限长；系统提示与编号草稿完全相同，构成可缓存前缀。各类结果合并为原有审计格式，`critic_mode=single` 保留旧的单次审校。
25. 稳定前缀：正文提示词的 system 消息只包含核心协议，同一作品内逐字节不变；按关键词激活的世界书移到 user 消息开头。llama.cpp 请求默认带 `cache_prompt`。
26. 约束解码：`structured_completion(..., schema=...)` 在服务支持时启用 JSON Schema 约束——llama.cpp 用 `json_schema`，vLLM 等用 `response_format.json_schema`——否则仍用 `json_object` 加有界修复。宽松 Schema 集中在 `app/llm/json_schemas.py`，覆盖审校、分类审校、章节计划、分阶段记忆、去套话、候选比较、对白演出和滚动重规划。
27. 道具与伤势状态机（`app/story/continuity_state.py`）：记忆提取新增“道具与伤势”阶段，事件按章节号写入 `memory.item_events` / `memory.injury_events`，必须带可核验证据，重新结算同一章时会先替换旧事件。当前状态由早于本章的事件折叠得出，自动遵守章节时间边界，并注入提示词。审校时做确定性检查：已遗失或已毁坏的道具在没有找回 / 修复描写的情况下被使用，或新伤在最短恢复章数内被写成痊愈，都会报出问题。
28. 滚动重规划（`app/planning/rolling_replan.py`）：每章通过并结算后，根据实际摘要与场景结算，微调后续最多 `rolling_replan_window` 章未写、未锁定路线的 goal / conflict / turning_point / ending_hook。不跨卷，不改卷契约。调整后重新跑路线校验，结果变差则整体还原。字段级差异写入任务的 `replan_history` 和事件日志。
29. 钩子欠账（`app/story/hook_ledger.py`）：章节接纳时把计划中的 `ending_hook` 记入 `memory.hook_ledger`，记录按章节 ID 保存，位置由当前章节顺序重新推导；后续章节的评价同样以章节 ID 为键，因此插入或删除章节不会错位。旧版以章号为键的评价在首次读写时，按记录缓存的 `origin_chapter` 与当前位置之差平移后迁移为章节 ID，超出全书范围的丢弃。超过 `hook_window` 仍未处理的钩子成为钩子债注入下一章，超过若干窗口自动过期。
30. 模型经验库（`app/review/experience_bank.py`）：只统计证据已核验或本地检测出的问题，`count` 为出现过的不同章节数（`chapter_ids`），同章反复审校不累计；权重按新章节衰减。缺少 `chapter_ids` 的旧记录按原 `count` 补入 `#legacy1…N` 占位 ID（不超过窗口），服务端与浏览器端合并结果一致。提示词只使用固定模板，模型原文只在界面展示。
31. 审校预算：`critic_budget` 为 `auto` 时按风险信号评分，低风险合并为一次审校、高风险运行五个分项审校器，合并审校发现需升级的问题时自动改为全量；`full` / `light` 固定预算。一键创作启动请求可带可选的 `critic_budget`，写入任务 `config` 后仅对本次运行生效。

新增行为与 DeepWrite 的对照、验证范围和后续优先级见 [长篇稳定性与创作质量优化报告](deepwrite-optimization.md)。

## 发布形态

- `dist/InkForge` 是带齐运行环境的便携版，适合完全离线分发。
- `dist/InkForge-Deploy` 是体积更小的自动部署版，首次启动按需安装 Python 和锁定依赖。
- 两种形态都把作品、凭据、日志和备份放入用户数据目录，不写入只读安装目录。
- 自动部署启动器对官方 Python 安装包校验固定 SHA-256，依赖环境由应用版本和 `requirements.txt` 指纹隔离。

## 如何新增 / 删除一个功能

新增：

1. **后端包**：确定归属包（规划 → `app/planning`，写作 → `app/writing`，审校 → `app/review`，故事事实 → `app/story`，导入导出 → `app/io` …）。纯规则写成无 I/O 的函数；需要调用模型时接收 `completion` 回调或使用 `app.llm`，不要导入 `app.api`。项目 JSON 新字段在 `app/storage/schema/project.py::ensure_project_defaults` 里只做加法默认值。
2. **路由**：在 `app/api/routers/<功能>.py` 写 `create_<功能>_router(...)`，依赖通过参数传入（`lambda: container.store` 等）；请求模型放在路由文件或 `app/api/schemas.py`。
3. **注册**：在 `app/main.py::register_routers` 中 `include_router`；需要跨路由共享的回调放进 `app/api/container.py`。
4. **前端脚本**：新建 `static/js/features/<区域>/<功能>.js`，末尾用 `Inkforge.register("<功能>", {...})` 注册公开函数；按钮的事件绑定写在 `static/js/bootstrap.js`。
5. **样式**：新建 `static/css/features/<功能>.css`，颜色只用 `base/tokens.css` 的令牌。
6. **index.html**：按顺序加入 `<script>` / `<link>`，地址带 `?v=<APP_VERSION>`；新增按钮或面板入口。
7. **测试**：在 `tests/` 增加单元测试与 API 测试；运行 `python -m pytest -q`、`ruff check .`、`python scripts/check_static.py`、`python scripts/check_project.py`。`tests/test_architecture.py` 会检查依赖方向和静态资源引用。
8. 修改 HTTP 契约时同时提升 `app/core/config.py::API_SCHEMA_VERSION` 与 `static/js/core/state.js::REQUIRED_API_SCHEMA`。

删除：

1. 删除后端模块、路由文件及其在 `app/main.py` / `app/api/container.py` 中的注册和回调；`grep` 功能名确认没有其他导入。
2. 删除前端脚本、样式、`index.html` 中的引用、按钮与 `bootstrap.js` 中的绑定；`tests/test_architecture.py` 会发现未被引用的静态文件。
3. **不要删除作品数据**：项目 JSON 与 `project_extensions` 中该功能写过的键保持原样，确保旧作品照常打开；如需清理，另写幂等迁移。
4. 删除只覆盖该功能的测试，保留共用测试；更新 README 功能列表、发布说明和本文档。

长篇评测指标、基准集与运行器见 [长篇评测体系](evaluation.md)；实现位于 `app/evaluation/longform.py`。v0.37 的结构调整背景见 [重构评估](refactor-assessment.md)。
