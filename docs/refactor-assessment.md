# v0.37 重构评估：可维护性问题与目标结构

本文记录 v0.36.0（删除连载发布与读者模拟之后）代码结构的主要问题，以及 v0.37.0 采用的目标布局和迁移原则。重构只改变代码位置和组织方式，不改变功能行为（除已删除的功能外）。

## 一、现状问题

### 后端

1. **`app/` 根目录堆了约 40 个松散模块**。领域规则（`memory.py`、`knowledge.py`、`canon.py`）、模型接入（`providers.py`、`llama_client.py`、`model_telemetry.py`）、HTTP 处理（`workspace_api.py`、`professional_api.py`）、持久化（`db.py`、`project_service.py`）和提示词（`prompts.py`）平铺在一起，无法从目录看出依赖方向，新人很难判断某个功能该放在哪里。
2. **同类东西位置不一致**。已经存在 `core / domain / services / infrastructure / api` 五层，但只有部分模块迁入：
   - 路由一部分在 `app/api/routers/`，另一部分在根目录的 `workspace_api.py`、`professional_api.py`；
   - 模型相关代码分散在根目录（`providers.py`、`llama_client.py`、`model_telemetry.py`）、`core/`（`model_budget.py`、`model_completion.py`）、`services/`（`structured_output.py`、`model_errors.py`）和 `domain/`（`json_schemas.py`）；
   - 审校相关代码分散在根目录（`quality.py`、`manuscript_quality.py`、`evidence_audit.py`、`calibration.py`、`editorial_workflow.py`、`must_contracts.py`）和 `services/`（`focused_critics.py`、`volume_review.py`、`editorial_policy.py`、`experience_bank.py`）；
   - `services/` 实际是“所有剩余模块”的收纳箱，混合了写作、审校、记忆、规划、导入导出、GitHub 备份和自动导演。
3. **`prompts.py`（约 1.5k 行）混合所有领域**：正文上下文编排（`build_prompt`）、章节规划提示词、灵感/孵化提示词、自动导演开书提示词、审计提示词、记忆提示词、文风分析提示词，以及人物知情边界渲染。任何一个领域改提示词都要打开这个大文件；其中 `CHAPTER_MEMORY_PROMPT` 已无引用。
4. **`planning.py` 同时是提示词常量文件和规划逻辑**：前 230 行是 9 个大段提示词，后面才是规划归一化、降级规划和路线应用。
5. **`main.py`（约 600 行）既是组合根又承载业务辅助函数**：单例创建、`structured_completion` 兼容门面、章节查找、记忆来源锁定校验、受管项目持久化、检索附加、上下文快照、正文构建预处理、本地质检上下文、导演运行时接线全部写在一个文件，测试只能通过 `app.main` 的模块全局变量打补丁。
6. **`db.py`（约 1.3k 行）一个类包办全部持久化**：建表与迁移、项目 CRUD 与版本、导演任务租约、上下文快照与章节会话、用户写作 Skills、全文检索、数据库备份校验与恢复。
7. **依赖方向事实上是网状的**：
   - `domain/project_schema.py` 为了补默认值，反向依赖规划、校准、历史工具、写作 Skills 等上层模块；
   - 自动导演（`services/director/*`）直接引用 `app.api.schemas` 中的请求模型，业务层依赖 HTTP 层；
   - `memory_settlement`、`experience_bank` 和导演模块只为一个 `utc_now()` 就引用整个 `db.py`；
   - `fallbacks.py` 把文风、记忆、审计、灵感四个领域的本地降级结果放在一起。
8. **架构测试只覆盖 `domain` 和 `services`**，没有约束新的模块落点，`app/` 根目录可以继续无限增长。

### 前端

1. **脚本平铺且职责混杂**：`static/app.js`（全局状态、API、项目归一化、导演状态标签）、`static/workspace.js`（场景工作台）和 `static/js/bootstrap.js` 位于不同层级；`js/core/` 里同时放了基础设施（`stream.js`、`namespace.js`）和具体功能（`generation.js` 正文生成、`usage.js` 用量、`dialogs.js` 中的模型设置弹窗）。
2. **`js/features/` 是 20 个平铺文件**，文件名与后端功能没有对应关系（如 `governance.js` 同时负责体检、版本历史、导出面板和备份中心）。
3. **CSS 没有约定**：`style.css`（基础布局 + 导演样式）、`css/theme.css`（色彩令牌）、`css/study.css`（统一视觉层）、`css/workspace.css`（基础控件覆盖 + 规划/审校样式）以及按功能命名的若干文件混在一起，`extensions.css` 是多个功能的杂烩，还残留已删除功能的样式。

## 二、目标结构

### 后端包与依赖方向

```text
app/api ──→ app/director ──→ app/planning ┐
   │           app/io        app/writing  ├──→ app/story ──→ app/llm ──→ app/core
   │           app/evaluation app/review  ┘        ↑            ↑
   └──────────────────────────────────────→ app/storage ────────┘
```

| 包 | 职责 |
| --- | --- |
| `app/core` | 配置、日志、文本与类型转换工具、时钟、通用 JSON Schema 校验；不依赖任何其他 app 包 |
| `app/llm` | 模型接口设置与能力门控、HTTP/SSE 客户端、结构化输出与修复、调用记录与用量、模型错误分类、上下文/输出预算、完成回执、约束解码 Schema |
| `app/storage` | SQLite 仓储（拆为结构迁移、项目、导演任务、快照会话、检索、备份几部分）、项目文档 Schema 与兼容迁移、凭据、检索索引、扩展记录、自动备份、备份位置、实例锁 |
| `app/story` | 故事事实层：长期记忆与完整性、记忆提取、知识层、世界书、同人 Canon、参考资料、章节时间边界、道具伤势状态、人物知情边界、权威版本与考据、钩子账本、关系图投影、交锋结构、历史时间与称谓、审校证据定位 |
| `app/planning` | 规划提示词、全书/分卷/章节规划模型、题材画像与向导模板、叙事策略、规划与路线校验、滚动重规划、导演开书规划 |
| `app/writing` | 正文提示词与上下文编排、任务提示策略、章节会话、正文规则与续补恢复、去套话、对白演出、多候选选稿、分支试写、文风引擎、写作 Skills、场景工作台、写作开关 |
| `app/review` | 审计提示词、本地质检、全稿质量、分项审校、评分校准、整卷回顾、审校门禁与精修策略、编辑工作流、硬契约、经验账本、章节记忆结算 |
| `app/evaluation` | 候选稿机械评测与提示词回归、长篇评测指标与盲评 |
| `app/director` | 自动导演运行时（任务、正文、精修、孵化、分卷、逐章循环、控制面）及其公开导入面 |
| `app/io` | 旧稿导入、TXT/EPUB/DOCX 导出、导出前清理、文件解析、GitHub 备份 |
| `app/api` | 依赖容器（单例与跨路由回调）、HTTP 请求契约和全部路由 |
| `app/main.py` | 应用工厂、生命周期、路由注册 |

依赖规则（由 `tests/test_architecture.py` 强制）：

- `core` 不导入任何其他 app 包；`llm` 只导入 `core`；
- `story` 只导入 `core`、`llm`；`storage` 只导入 `core`、`llm`、`story`；
- `planning`、`writing`、`review` 可以互相引用，但不得导入 `director`、`io`、`evaluation`、`api`；
- `director`、`io`、`evaluation` 不得导入 `api`，也不互相导入；
- 除 `app/api` 与 `app/main.py` 外，任何模块都不得导入 `app.api`；
- `app/` 根目录只允许 `__init__.py`、`__main__.py`、`main.py`。

为满足“存储层不依赖功能层”，项目文档中规划、评分校准、历史资料和写作 Skills 各部分的**数据形状**（默认值与归一化）迁入 `app/storage/schema/`，功能模块从那里导入；自动导演和章节工作流共用的请求模型从 `app.api.schemas` 移到 `app/writing/requests.py` 与 `app/director/requests.py`。

### 前端

- `static/js/core/`：命名空间、主题、全局状态与 API、流式读取、项目加载保存、通用弹窗（新建/重命名/删除）；
- `static/js/features/<功能>/`：与后端包对应的子目录（`settings`、`planning`、`writing`、`story`、`review`、`director`、`io`、`storage`、`evaluation`、`onboarding`）；
- `static/js/bootstrap.js` 只负责事件绑定与启动；
- `static/css/base/`：`tokens.css`（色彩令牌）、`layout.css`（基础布局与控件）、`components.css`（统一视觉层）；`static/css/features/`：每个功能一份样式；
- `index.html` 按“核心 → 功能 → 启动”的顺序加载，所有资源保留 `?v=<APP_VERSION>`。

## 三、迁移原则

1. **只搬家，不改行为**：每次迁移一个包，跑全量测试后提交；提示词文本逐字节保持不变。
2. **不留兼容垫片**：所有导入（应用、脚本、测试）直接改到新位置；测试打补丁的符号在新家打补丁，被测代码也在新家查找。
3. **旧作品必须能打开**：项目 JSON 与扩展记录中的未知键原样保留，迁移只做加法。
4. **自动导演公开面保留**：`app/director/service.py` 是原 `app.services.director_runtime` 的新家，继续作为测试替换点（`chat_stream`、`run_structured_completion` 等在调用时查找）。
5. **组合根唯一**：`app/api/container.py` 持有进程单例与跨路由回调，测试统一在这里替换 `store`、`chapter_audit` 等依赖。

## 四、执行结果与说明

重构已按上述布局完成（v0.37.0），与最初设想相比有以下取舍：

- **交锋结构、历史工具和证据定位放在 `app/story`**（`debate`、`history`、`evidence`）。它们是不含 I/O 的故事文本规则，同时被规划、写作、审校和项目 Schema 使用；放在审校包会让存储层反向依赖审校。
- **评测独立成 `app/evaluation`**：评测同时依赖写作（提示词回归）和审校（全稿质量），独立成包可避免功能包之间多出一条依赖。
- **项目 Schema 的“数据形状”迁入 `app/storage/schema/`**：规划（`empty_planning`、`ensure_planning_defaults`、`normalize_route`）、评分校准样本、写作 Skills 的默认值与归一化从功能模块移出，功能模块反向导入。
- **`db.py` 按职责拆为 `ProjectStore` 及 5 个混入模块**（结构迁移、导演任务、快照与会话、全文检索、数据库备份），对外仍是一个 `ProjectStore` 类。
- **`fallbacks.py` 拆散**：本地文风卡归 `writing.style_engine`，本地记忆摘要归 `story.memory_extraction`，本地审计结果归 `review.local_quality`，本地灵感方向归 `planning.ideas`。
- **`prompts.py` 按领域拆分**：正文编排 → `writing/context_builder.py`，正文与文风提示词 → `writing/prompts.py`，规划类提示词（含原 `planning.py` 中的 9 段）→ `planning/prompts.py`，审计提示词 → `review/prompts.py`，记忆精简提示词 → `story/memory_extraction.py`，知情边界渲染 → `story/epistemics.py`。所有仍在使用的提示词逐字节一致（脚本比对）；无引用的 `CHAPTER_MEMORY_PROMPT` 已删除。
- **CSS 拆分保持原有层叠顺序**：加载顺序为 `base/tokens → base/layout → 工作区功能样式 → base/components → 较新的功能样式`。重构前后对主界面、三个侧栏标签和七个主要弹窗（浅色、深色）逐元素比对计算样式，结果完全一致。
- **保留不动**：`app/demo`（演示作品数据）与 `app/entrypoints`（进程入口）原样保留；`api/routers` 中较大的 `editorial_audit.py`、`editorial_memory.py`、`planning.py` 仍承载章节工作流本身（被自动导演通过容器回调复用），本轮只搬家不改写，以免改变行为。

