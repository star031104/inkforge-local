# InkForge Local 0.20.0

发布日期：2026-08-30  
API Schema：36  
Memory State：4

## 新增

- SQLite FTS5 可重建全文索引：索引旧章节摘要、正文片段、事实、伏笔、时间线、关系和人物知情账本。
- 结构化记忆与 FTS 正文片段混合排序，保留类型配额、相关性重排和近重复抑制。
- 数据库层时间边界：生成第 N 章时，正文检索只允许第 N 章之前的材料，并遵守事实有效期。
- 作者/读者/人物三层知情边界；事实新增 `known_by`、`reader_known`、`author_only`。
- 人物 `knowledge_baseline` 与带章节号、证据的 `knowledge_ledger` 分离，不再把动态新知写回开篇知识。
- 上下文快照：冻结实际模型消息、区块裁剪、检索来源、激活世界书、激活 Skill、运行配置和提示词 SHA-256。
- 生成自动创建上下文快照；支持列表、查看和使用新 Provider 设置流式回放冻结提示词。
- 快照入库前递归移除凭据字段，并脱敏疑似 API Key / Bearer Token。
- 写作 Skills：内置只读、个人跨项目、项目随书三种范围；always / auto / manual 三种激活模式。
- Skill 权限白名单只允许 `prompt_instructions`，拒绝命令、工具、文件、网络和可执行能力声明。
- 新增界面：写作 Skills 管理、激活 Skill 徽标、上下文快照创建与历史审计。

## API

- `POST /api/search`
- `GET /api/writing-skills`
- `POST /api/writing-skills/user`
- `DELETE /api/writing-skills/user/{skill_id}`
- `POST /api/projects/{project_id}/writing-skills`
- `DELETE /api/projects/{project_id}/writing-skills/{skill_id}`
- `POST /api/prompt/snapshot`
- `GET /api/projects/{project_id}/context-snapshots`
- `GET /api/prompt/snapshots/{snapshot_id}`
- `POST /api/prompt/snapshots/{snapshot_id}/replay`

`/api/health` 新增 `search_index`；提示词预览与生成元信息新增检索来源、激活 Skills、快照 ID 和提示词哈希。

## 迁移与兼容

- 启动时自动创建 FTS、索引状态、上下文快照和个人 Skills 表。
- 旧项目读取时升级至 Memory State 4；旧人物知识尽可能从已有账本条目中剥离为 `knowledge_baseline`。
- FTS5 不可用时自动回落到原有依赖无关词法检索，正文创作功能不停止。
- FTS 索引是派生数据；保存、导入、恢复和删除项目会同步重建或清理，不改变项目 JSON 事实源。

## 验证

- 自动化：`183 passed`。
- SQLite：`PRAGMA integrity_check = ok`。
- 已验证旧正文召回、当前/未来章隔离、事实有效期、索引替换/删除、旧库重建、知情时间边界、快照脱敏/保留/回放、Skill 只读与能力拒绝。

