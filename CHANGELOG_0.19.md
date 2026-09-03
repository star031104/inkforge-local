# InkForge 0.19.0 — Evidence-Grounded Memory Transactions

发布日期：2026-08-30

## 核心变化

- 新增章节验收事务：`settlement_pending → settlement_extracted → committed`，失败进入 `state_degraded`。
- 每次验收绑定章节正文 SHA-256；同一正文重复操作幂等，正文改变后旧提交拒绝应用。
- 正文先保存，再调用模型提取记忆；模型超时、断网或结构错误不再威胁已接纳正文。
- 人物状态、人物知情、事实、伏笔、时间线与关系全部要求正文连续证据，并由本地代码核验。
- 封闭知识图谱旁路：只有已在记忆层核验通过的事实和关系才能投影为 confirmed。
- 模型生成的全书摘要降为候选；权威 `story_so_far` 由已接纳章节摘要确定性汇编。
- 提示词预览升级为 Context Viewer，显示区块选择、优先级、裁剪原因和裁剪前后 token。
- 新增 xAI / Grok Provider 预设：`https://api.x.ai/v1`、`grok-4.6`，支持 `XAI_API_KEY` 与 `INKFORGE_XAI_API_KEY`。
- 记忆提交使用服务器最新项目作为合并基础，避免耗时提取覆盖其他章节的并行编辑。
- 前端故事记忆面板新增最近章节验收状态。

## 兼容与数据

- 记忆状态模式升级到 `state_version=3`，旧项目在读取时惰性迁移。
- 不修改 SQLite 表结构；新字段继续存入项目 JSON payload，旧数据库可直接打开。
- 实施前数据库备份：`data/backups/inkforge-before-final-implementation-20260829-195834.db`。
- 导出项目 JSON 继续自动清除 `settings.api_key`。

## 验证

- Python 编译通过。
- 浏览器 JavaScript 语法检查通过。
- `pytest -q`：`161 passed`。
- 当前机器没有 xAI 或 SiliconFlow Key，因此未冒充真实云端 E2E；xAI 已完成载荷、环境变量、检测与默认值的自动测试。
