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
| `app/core` | 环境读取、版本和运行路径 |
| `app/api/schemas.py` | 稳定的 HTTP 请求契约 |
| `app/api/routers/projects.py` | 项目生命周期、备份校验恢复、搜索、版本与写作技能 |
| `app/api/routers/system.py` | 健康状态、模型连接与提供商摘要 |
| `app/api/routers/research.py` | 参考资料、文风、正典与知识图谱 |
| `app/api/routers/prompts.py` | 提示词预览、上下文快照与确定性回放 |
| `app/api/routers/chapter_sessions.py` | 章节会话、检查点与回滚 |
| `app/api/routers/generation.py` | 正文流式生成、长度续补与生成事件协议 |
| `app/api/routers/director.py` | 自动导演任务的启动、暂停、恢复与查询接口 |
| `app/domain/planning_validation.py` | 规划、分卷、记忆输出的纯验证规则 |
| `app/domain/route_validation.py` | 章节路线、阶段边界和跨章重复规则 |
| `app/domain/prose.py` | 正文长度、截断、清理和确定性安全门禁 |
| `app/domain/project_schema.py` | 项目默认结构与旧作品的兼容迁移 |
| `app/services/structured_output.py` | JSON 输出解析、验证和一次有界修复 |
| `app/services/model_errors.py` | 模型错误分类及面向作者的错误说明 |
| `app/services/memory_settlement.py` | 证据绑定的章节记忆结算 |
| `app/services/editorial_policy.py` | 审校门禁、候选排序和修复策略 |
| `app/services/director_runtime.py` | 自动导演后台任务、检查点、暂停恢复与运行时依赖 |
| `app/infrastructure/search_index.py` | FTS 文档编译、检索词和快照脱敏 |
| `app/infrastructure/secret_store.py` | 与作品、版本历史和备份隔离的项目凭据存储 |
| `app/infrastructure/automatic_backup.py` | 与 HTTP 请求解耦的启动时及周期数据库备份 |
| `app/entrypoints/server.py` | 统一服务进程、日志和浏览器健康检查入口 |
| `app/db.py` | SQLite 仓储、表迁移、事务和版本持久化 |
| `app/main.py` | 兼容入口、章节/规划用例与应用依赖组合 |

`app.main:app` 仍是公开启动入口。拆分过程保留该导入路径，避免破坏启动脚本、测试和第三方调用。

## 前端目录

| 路径 | 职责 |
| --- | --- |
| `static/app.js` | 全局状态、兼容检查和共享 UI 基础 |
| `static/js/core/project.js` | 作品加载、收集、保存与章节渲染 |
| `static/js/core/generation.js` | 模型检查、提示词、正文生成和候选稿 |
| `static/js/core/dialogs.js` | 设置和危险操作确认窗口 |
| `static/js/features/planning.js` | 全书、分卷与单章规划 |
| `static/js/features/resources.js` | 资料、正典、知识图谱、人物与世界书 |
| `static/js/features/editorial.js` | 审校、修订、质量检查和记忆接纳 |
| `static/js/features/governance.js` | 契约、权威、版本、导出和项目体检 |
| `static/js/features/director.js` | 自动导演、自动精修和灵感孵化 |
| `static/workspace.js` | 场景、作者偏好、调用统计和候选评测 |
| `static/js/bootstrap.js` | DOM 事件绑定和应用启动 |

脚本使用经典浏览器全局作用域以保持无构建工具运行。加载顺序由 `static/index.html` 明确规定；功能文件只声明函数，最后由 `bootstrap.js` 绑定事件并启动。

## 数据与一致性

1. 浏览器编辑基于 `updated_at` 做乐观并发控制。
2. SQLite 在同一事务内比较版本、保存项目、章节版本和检索投影。
3. 正文接纳后按正文哈希创建记忆提交。
4. 修改已结算章节会使受影响后缀失效；重建必须按章顺序执行。
5. FTS 是可删除重建的投影，不是权威数据源。
6. 模型调用统计存入独立数据库，不保存正文、提示词、地址或密钥。
7. 模型凭据只进入独立 secrets 数据库；作品、历史版本、上下文快照和自动备份均不持有 Key。
8. 自动导演依赖通过 `DirectorDependencies` 显式声明，并保留可测试的延迟绑定，不再按字符串从 `main.py` 查找函数。
9. 数据库恢复只接受备份目录中的合法文件；恢复前创建安全快照，恢复后再次校验，任一步失败都会用安全快照回滚。
10. 定时备份由应用生命周期管理，不依赖作者是否触发某个保存接口；自动备份和手动备份分别轮换。

## 发布形态

- `dist/InkForge` 是带齐运行环境的便携版，适合完全离线分发。
- `dist/InkForge-Deploy` 是体积更小的自动部署版，首次启动按需安装 Python 和锁定依赖。
- 两种形态都把作品、凭据、日志和备份放入用户数据目录，不写入只读安装目录。
- 自动部署启动器对官方 Python 安装包校验固定 SHA-256，依赖环境由应用版本和 `requirements.txt` 指纹隔离。

## 扩展规则

- 新 HTTP 能力先定义请求模型，再放入对应路由；不要继续向 `main.py` 添加简单 CRUD。
- 新故事约束应实现为无 I/O 的领域函数，并为边界条件增加测试。
- 新模型工作流放入 `services`，显式注入仓储或模型调用函数。
- 新前端能力放入最接近的 `core` 或 `features` 文件；启动绑定只放在 `bootstrap.js`。
- 数据库表或字段迁移必须幂等，并兼容旧项目 JSON。
