# InkForge 0.19 最终实现工程报告

**实现日期：** 2026-08-30  
**工作目录：** `C:\Users\16708\Documents\Codex\2026-07-29\gei-w\outputs\inkforge-local`  
**参考上传项目：** `D:\桌面\inkforge-local-refactored-v0.18.1-cloudqa-r3\inkforge-local-0.18.1`  
**实现版本：** `0.19.0` / API Schema `34`  
**验证结果：** `161 passed`

---

## 1. 最终结果

本轮没有把模型变成“拥有永久记忆的聊天窗口”，而是把 InkForge 实现成模型之外的小说状态系统：

1. 作者接受正文后，正文先可靠保存；
2. 系统为正文建立带哈希的记忆提交；
3. 模型只提出本章状态增量；
4. 本地 Reducer 验证每条证据是否真实出现在接纳正文；
5. 只有核验通过的增量才能进入长期记忆和知识图谱；
6. 下一次生成前，系统按优先级检索并重新注入相关状态；
7. 提示词预览解释哪些内容被发送、裁剪或省略。

因此，“模型不忘记小说”现在由外置状态、证据门、幂等提交和上下文重建共同实现，而不是依赖 Grok、Qwen 或本地模型的会话记忆。

---

## 2. 数据保护与迁移

### 2.1 原库保护

实施前已建立只读可恢复备份：

```text
data/backups/inkforge-before-final-implementation-20260829-195834.db
大小：78,188,544 bytes
SHA-256：AF9CAB8FB28F3E7D9B018ECC2AF6FE61A9636610BBCE5110189724AD7EBEF03D
```

没有用上传项目的数据库覆盖当前数据库，也没有清理用户章节、历史版本或备份。

### 2.2 迁移策略

项目仍保存在 SQLite `projects.payload` JSON 中。0.19 使用读取时惰性迁移：

- `memory.state_version` 至少为 `3`；
- 自动补充 `story_digest_candidate` 和 `commits`；
- 自动补充事实、时间线和关系的证据字段；
- 自动补充章节的 `memory_status`、`memory_commit_id` 和 `accepted_content_hash`；
- 旧字段继续保留，不做破坏性重写。

这种做法让现有数据库可以直接打开，也避免一次性全库迁移失败。

---

## 3. 章节验收事务

### 3.1 状态机

```text
候选草稿
   │ 作者接受
   ▼
settlement_pending      正文和提交记录已经持久化
   │ 模型提取完成
   ▼
settlement_extracted    增量候选已得到，尚未成为权威状态
   │ 本地证据门 + Reducer
   ├──────────────► committed       正文和长期状态一致
   │
   └──────────────► state_degraded  正文安全，记忆可稍后重试
```

### 3.2 哈希与幂等

`app/memory_integrity.py` 对规范化换行后的章节正文计算 SHA-256：

- 同一章节、同一正文再次接受：复用原提交；
- 已提交正文再次接受：直接返回 committed，不重复写事实；
- 正文在提取期间变化：旧提交返回 HTTP 409；
- 降级提交再次接受：恢复为 pending，并增加 attempts；
- 提交记录最多保留 200 条。

### 3.3 并行编辑保护

模型提取可能持续数分钟。应用记忆时，后端不再直接保存浏览器几分钟前的完整项目快照，而是：

1. 先验证浏览器提交与目标正文哈希；
2. 从 SQLite 读取服务器最新项目；
3. 在最新项目上应用本章增量；
4. 再次验证服务器中的目标章哈希；
5. 保存结果。

这样可以保留提取期间对其他章节的编辑，同时阻止目标章节被旧结果覆盖。

### 3.4 API

| API | 责任 |
| --- | --- |
| `POST /api/chapter/accept` | 先持久化接纳正文并建立 pending 提交 |
| `POST /api/chapter/memory` | 提取状态增量；可携带 `commit_id` |
| `POST /api/chapter/memory/apply` | 验证哈希、证据并幂等提交 |
| `POST /api/chapter/memory/degrade` | 提取或应用失败时保存降级状态 |

前端“插入并更新记忆”已经按上述顺序调用。任何模型错误发生时，界面会明确显示“正文已安全保存，记忆待修复”。

---

## 4. 严格证据治理

### 4.1 可成为权威状态的条件

下列模型增量都必须带 `evidence`，且去除常见标点和空白后必须是接纳正文的连续子串：

| 增量 | 无证据/证据不匹配时的行为 |
| --- | --- |
| 人物状态、位置、物品、情绪、临时外观 | 跳过并记录 warning |
| 人物新增知情 | 不进入 knowledge ledger |
| 原子事实 | 不进入 memory facts |
| 伏笔推进或回收 | 不更新；回收还必须有 payoff |
| 时间线事件 | 不写入时间线 |
| 动态关系 | 不更新关系状态 |
| 显著描写 | 只有原句可在正文中找到才进入去重复账本 |

### 4.2 事实层与知识层统一

0.18.1 同时存在 `memory.facts` 和 `knowledge.facts`。0.19 没有立即删除兼容层，而是明确主从关系：

- `memory.facts`：章节记忆兼容源；
- `knowledge.facts`：规范化检索投影；
- 投影保留 `source_memory_id`、章节来源、证据、有效期和可见性；
- 只有 `memory` 中已经 `evidence_verified=true` 的条目才可投影为 confirmed；
- 图谱可以重建，不是唯一真相源。

本轮复核发现并修复了一个关键旁路：模型原先可能用“非空但不匹配正文的 evidence”绕过旧记忆拒绝，直接投影到知识图谱。现在事实和关系必须先在记忆层核验成功，知识层还会再次验证证据原句。

### 4.3 全书摘要治理

模型返回的 `story_so_far` 不再直接覆盖权威滚动进展，而是保存为：

```text
memory.story_digest_candidate
```

真正的 `memory.story_so_far` 由 `derive_story_so_far()` 从有正文、有摘要的已接纳章节确定性汇编。超出预算时保留开头两章和最近章节，中段用明确省略标记替代。单次模型响应不能删除或改写早期全书因果链。

章节摘要仍是模型辅助生成的高层导航信息；发生细节冲突时，带正文证据的原子事实、人物状态、时间线和关系优先级更高。

---

## 5. Context Viewer

`app/prompts.py` 的每个上下文区块现在记录：

- `role`；
- `priority`；
- `required`；
- `status = included / trimmed / omitted`；
- `tokens_before`；
- `tokens_after`；
- `reason`；
- 实际发送内容。

前端提示词预览会显示“已发送 / 已裁剪 / 已省略”和 token 变化。例如：

```text
相关资料证据 · P88 · 已裁剪 · 2140→680 tokens
原因：为模型输出预留上下文预算
```

这使作者可以判断模型为什么漏掉某项设定：是没有命中、优先级低、被裁剪，还是根本没有进入项目状态，而不再只能猜测“模型失忆”。

---

## 6. xAI / Grok 工程接入

### 6.1 已实现

- Provider 名称：`xai`；
- API 基址：`https://api.x.ai/v1`；
- 默认模型：`grok-4.6`；
- API Key：优先项目显式值，其次 `XAI_API_KEY`，再次 `INKFORGE_XAI_API_KEY`；
- 普通 Chat Completions、SSE 流式与 JSON 模式沿用现有 OpenAI-Compatible 客户端；
- 关闭模型思考时发送 `reasoning_effort=low`，开启时发送 `high`；
- 不向 xAI 发送 SiliconFlow/llama.cpp 专用的 `top_k`、`min_p`、`chat_template_kwargs` 或 `reasoning_budget`；
- 前端提供“一键填入 xAI Grok 4.6”预设；
- Provider 自动检测 `api.x.ai`；
- 导出 JSON 自动去掉 API Key。

xAI 官方当前 Quickstart 使用 `https://api.x.ai/v1` 和 `grok-4.6`：

- https://docs.x.ai/developers/quickstart
- https://docs.x.ai/developers/grok-4-6
- https://docs.x.ai/developers/rest-api-reference/inference/chat

### 6.2 没有伪造的验收结论

当前机器没有 `XAI_API_KEY`、`INKFORGE_XAI_API_KEY` 或 SiliconFlow 测试 Key，因此本轮只完成了自动化载荷、默认值、环境变量和 Provider 检测测试，没有声称真实 xAI 云端 E2E 已通过。

真实发布前仍应使用自己的 xAI 账户验证：模型列表、普通响应、SSE、结构化记忆、401、429、5xx、断流和长上下文。

### 6.3 Grok 网页/手机与本项目的关系

Grok 网页或手机 App 适合随身生成和讨论；InkForge 负责小说的权威记忆。两者的正确关系是：

```text
InkForge 状态库 → 选择相关上下文 → Grok 生成候选 → 作者接受
      ↑                                             │
      └──── 正文证据核验 ← 状态增量候选 ←──────────┘
```

Grok 会话、Files 或 Collections 可以辅助携带资料，但不取代人物知情、事实有效期、伏笔状态和章节事务。

---

## 7. 上传版 0.18.1 的迁入范围

以下能力已从上传版的独立实现迁入并保留当前数据库：

- 多文件资料解析：TXT、MD、DOCX、EPUB、PDF、HTML；
- 文风资料与正典资料职责隔离；
- Canon Profile、接受前 OOC Gate；
- 结构化实体、事实、关系和图谱投影；
- SiliconFlow / llama.cpp Provider 参数隔离；
- 结构化记忆的精简 AI 恢复；
- 历史小说完整管线测试、Provider HTTP 测试和 Windows 云测试启动器；
- 生成长度修复、审计误报收紧、灵感孵化与自动导演恢复改进。

迁入不是覆盖：所有源文件通过差异补丁落地，当前 `data/inkforge.db` 没有替换为上传包数据库。

---

## 8. 逐文件实现索引

### 后端核心

| 文件 | 当前责任 | 最值得学习/修改的位置 |
| --- | --- | --- |
| `app/memory_integrity.py` | 哈希、提交状态、幂等、降级、确定性摘要 | 新增重放队列、提交事件日志、摘要检查点 |
| `app/main.py` | API、提取、严格 Reducer、可靠保存 | 拆分 `memory_service.py`、为冲突候选建立人工处理 API |
| `app/db.py` | SQLite、版本、惰性迁移 | 将章节/事实/提交拆成独立表并加外键、乐观版本号 |
| `app/knowledge.py` | 实体、事实、关系、图谱投影 | 增加位置/物品等单值谓词注册表、冲突检测与 FTS5 |
| `app/prompts.py` | 上下文编排、预算与追踪 | 增加每条记忆的入选/落选原因和检索得分 |
| `app/providers.py` | Provider 能力隔离 | 拆成 capability registry，增加 Responses API 和用量字段 |
| `app/llama_client.py` | Chat Completions 与 SSE | 增加标准错误分类、退避重试、xAI conversation cache header |
| `app/canon.py` | 同人 Canon Lock | 增加 AU 覆盖层和 Canon 版本差异 |
| `app/references.py` | 参考资料检索与防复刻 | 增加来源版本、分块索引与删除传播 |
| `app/file_parsing.py` | 多格式文本解析 | 增加 OCR、编码诊断和超大文件后台解析 |

### 前端

| 文件 | 当前责任 | 最值得学习/修改的位置 |
| --- | --- | --- |
| `static/app.js` | 验收事务、Context Viewer、资料/Canon/图谱 UI | 拆分为模块、增加 state-degraded 一键重试与冲突收件箱 |
| `static/index.html` | 单页工作台结构 | 后续 PWA manifest、移动端底部导航、登录状态 |
| `static/style.css` | 桌面/移动响应式样式 | 360px 真机排版、软键盘高度与安全区域 |

### 测试

| 文件 | 验证重点 |
| --- | --- |
| `tests/test_memory_integrity.py` | 持久化、幂等、正文哈希、降级、并行编辑保护 |
| `tests/test_narrative_state.py` | 证据拒绝、状态 v3、知情、关系和伏笔 |
| `tests/test_knowledge_layer.py` | confirmed/candidate、可变状态替代、投影证据门 |
| `tests/test_prompts.py` | 上下文预算和可解释裁剪轨迹 |
| `tests/test_providers.py` | xAI/SiliconFlow/llama.cpp 参数隔离与环境变量 |
| `tests/test_provider_http_e2e.py` | OpenAI-Compatible HTTP/SSE 行为 |
| `tests/test_historical_story_pipeline_e2e.py` | 历史题材生成—审计—记忆—图谱链路 |

---

## 9. 自动化验收

执行命令：

```powershell
.\.venv\Scripts\python.exe -m pytest -q
node --check static\app.js
```

结果：

```text
161 passed
Python compile: PASS
JavaScript syntax: PASS
git diff --check: PASS（仅有 Windows 行尾提示，无空白错误）
```

新增测试覆盖：

- 同一正文接纳幂等；
- 提交持久化；
- 目标正文哈希冲突；
- 记忆失败正文不丢；
- 并行编辑另一章节不被旧快照覆盖；
- 模型全书摘要不能直接覆盖权威摘要；
- 无效证据不能从知识图谱旁路写入；
- Context Viewer 有裁剪状态和原因；
- xAI 默认地址、模型、Key 来源和参数隔离。

---

## 10. 当前明确边界

以下项目没有在没有凭据或没有安全基础设施的情况下冒进实现：

1. **真实 xAI/SiliconFlow 云端 E2E**：缺少用户 Key，不能伪造通过结论；
2. **xAI Responses、Files、Collections**：基础 Chat Completions 先稳定，远端资料仍不能成为唯一真相源；
3. **公网手机入口**：当前服务没有登录、TLS、CSRF 和速率限制，不能只把监听地址改为 `0.0.0.0` 后暴露公网；
4. **FTS5/向量检索**：当前仍以实体、关键词、相关性和去重为主，下一阶段应先建立可量化检索评测；
5. **独立关系数据库模式**：为兼容现有项目，本轮采用 JSON v3 和投影式统一；
6. **完整冲突收件箱/一键重放 UI**：当前 warning、continuity notes 和 state-degraded 已可见，但还没有独立工作台；
7. **多 Agent 写作委员会**：先保证数据写入可靠，再引入会放大吞吐的 Agent。

这些是后续工程项，不影响本轮已经落地的“正文不丢、证据不旁路、提交可重放、上下文可解释、Grok 可切换”主链路。

---

## 11. 推荐下一阶段顺序

1. 为 `state_degraded` 增加“一键重新提取”与后台重试队列；
2. 建立冲突收件箱，将未验证增量保存为 candidate 而不是只有 warning；
3. 对事实、人物知情、位置和物品建立 30/50/100 章回放集；
4. 增加 SQLite FTS5、实体过滤和 MMR，并报告 Recall@10/MRR；
5. 用真实 xAI 账户完成 Provider E2E 和错误矩阵；
6. 将 API Key 迁到操作系统凭据存储，项目只保留 `secret_ref`；
7. 增加认证、TLS 和受控同步后，再做手机 PWA；
8. 最后引入写作 Skills 和 Agent 工具循环。

---

## 12. 工程结论

0.19 的核心不是“又加了一个提示词”，而是建立了可验证的数据边界：

- 草稿不是事实；
- 只有作者接受的正文才有资格结算；
- 模型只提出增量；
- 本地代码决定证据是否成立；
- 提交必须与正文哈希一致；
- 失败时优先保护正文；
- 知识图谱不能绕过记忆证据门；
- Provider 可以替换，小说真相不能跟着模型会话漂移。

这是当前项目从“AI 小说界面”升级为“本地优先的长篇小说状态操作系统”的关键实现基础。
