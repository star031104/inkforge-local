<div align="center">

# 砚火 · InkForge Local

### 为中文长篇小说而生的本地 AI 写作工作台

从一句灵感，到故事圣经、分卷规划、逐章创作、连续性审计与长期记忆——<br>
让本地模型真正参与一部长篇作品的完整生产，而不只是续写下一段文字。

[![Version](https://img.shields.io/badge/version-0.20.0-c2410c?style=for-the-badge)](https://github.com/star031104/inkforge-local)
[![Python](https://img.shields.io/badge/Python-3.10%2B-3776AB?style=for-the-badge&logo=python&logoColor=white)](https://www.python.org/)
[![FastAPI](https://img.shields.io/badge/FastAPI-0.116-009688?style=for-the-badge&logo=fastapi&logoColor=white)](https://fastapi.tiangolo.com/)
[![Provider](https://img.shields.io/badge/Provider-SiliconFlow%20%7C%20Grok%20%7C%20llama.cpp-111827?style=for-the-badge)](https://github.com/ggml-org/llama.cpp)

**中文长篇 · 同人 Canon Lock · 证据知识图谱 · 可恢复工作流 · 作者最终控制**

[快速开始](#快速开始) · [核心能力](#核心能力) · [工作流](#推荐创作工作流) · [技术架构](#技术架构)

</div>

---

## 项目简介

**砚火**是一套面向中文长篇与同人创作的 AI 写作工作台。0.20 在 0.19.1 的可靠生产链上加入 SQLite FTS5 旧正文片段检索、作者/读者/人物三层知情边界、自动脱敏的上下文快照与安全写作 Skills。模型通信采用 OpenAI-Compatible Provider：可连接 SiliconFlow、xAI，也可以切回本地 llama.cpp / GGUF；写作、记忆、知识图谱、快照与 Canon Lock 不依赖具体模型。

它不会把整本小说粗暴地塞进一次提示词，也不会让模型悄悄覆盖手稿。作品方向、人物状态、世界规则、伏笔、章节计划和历史版本都以结构化数据留在本机；生成内容先进入候选草稿，只有作者确认后才写入正文并更新长期记忆。

> 砚火不是“替你按一下按钮写完一本书”，而是让 AI 在长篇创作中拥有可靠的规划、记忆、审计与恢复能力，同时始终保留作者的决定权。

## 为什么选择砚火

| 常见问题 | 砚火的解决方式 |
| --- | --- |
| 长篇写到后面人物失忆、设定漂移 | 可追溯事实、人物知情、关系、伏笔与事件时间线共同约束 |
| 大纲很宏大，拆到章节却反复做同一件事 | 分卷因果契约、12 类剧情岗位、状态维度轮换与跨章重复检查 |
| 本地小模型容易超时或输出残缺 JSON | 分段事务、流式闭合早停、检查点恢复与可编辑降级结果 |
| AI 草稿出现套话、复述、截断或现代术语 | 本地确定性检查 + AI 连续性审计 + 全稿级质量体检 |
| 自动生成中断后必须从头再来 | SQLite 持久化任务状态，可暂停、恢复并从首个问题点重建 |
| 需要先用 Grok/云模型、以后切本地 | Provider 层隔离 xAI / SiliconFlow / llama.cpp，只改模型设置不改业务代码 |
| 同人角色越写越 OOC | 原作资料库 + 人工核对 Canon Profile + 接受前 Canon Gate |
| 参考小说越学越像“复制” | 抽象风格指纹 + 多样文分析 + 长原句重合检测 |

## 核心能力

### 全书级规划

- **灵感孵化**：把一句人物、冲突或片段扩展成两套可比较的开书方案。
- **故事圣经**：建立主题命题、读者承诺、故事发动机、贯穿冲突与终局代价。
- **分卷蓝图**：按卷固定主要场域、时间跨度、人物选择、不可逆变化和新故事问题。
- **逐章因果路线**：以小批次拆章，检查目标、冲突、转折、章末变化及卷内推进职责。
- **一键创作全文**：从灵感开始，自动完成规划、拆章、正文、审计、修订与记忆回灌。

### 长期叙事记忆

- 章节正文先持久化为 `settlement_pending`，再提取、核验和提交；失败只会进入 `state_degraded`，不会丢正文。
- 每次验收绑定正文 SHA-256，同一正文重复点击不会制造重复事实，正文改变后旧记忆提交会被拒绝。
- 永久人物核心与动态场景状态分离，临时衣着不会覆盖稳定外貌。
- 原子事实、人物状态、知情、关系、时间线和伏笔更新必须附正文连续证据，并在本地逐字核验后才能升级为权威状态。
- 模型返回的全书摘要只保存为候选；实际滚动进展由已接纳章节摘要确定性汇编，不能被单次模型响应整体覆盖。
- 独立维护人物知情账本、动态关系网、事件时间线与伏笔生命周期。
- 世界书支持关键词或正则触发、AND/NOT 条件、递归关联、作用域和 token 预算。
- 相关性检索把结构化状态与 SQLite FTS5 旧正文片段混合排序，并使用类型配额和相似内容抑制，避免同类旧信息挤占上下文。
- 检索查询在数据库层排除当前章与未来章；FTS 索引属于可重建投影，项目 JSON 始终是唯一事实源。
- 作者层真相、读者已见信息和人物有证据知情分别编译；重写早期章节时不会注入本章或后期知情账本。

### 写作与文风控制

- 支持续写、按要求创作、重写选中内容、扩写选中内容。
- 续写时锚定原文末句和场景边界，降低复述、重开场景与凭空补史。
- 文风学习采用“风格卡 + 合法样文节选”的软学习方式，不修改模型权重。
- 根据当前场景的对话比例、句长、节奏和相关性动态选择文风示例。
- 逐章作者注只约束当前章节，长期硬规则与临时要求互不污染。
- 写作 Skills 分为内置只读、个人跨项目和项目随书三层，支持始终、关键词自动和手动激活。
- Skill 只能提供受长度限制的提示词方法，不能执行命令、调用工具、访问文件或联网，也不能覆盖事实与知情边界。

### 原作同人角色与证据知识图谱

- **Canon Lock**：为时崎狂三、花火、上杉绘梨衣等原作角色保存原作、时间节点、人格、价值观、能力限制、语言/行为/情绪模式和禁止 OOC 清单。
- AI 可根据上传的原作资料生成“待核对档案”，**只有人工确认后才成为最高级角色硬约束**。
- 正文接受前可强制运行 Canon Gate；明显 OOC、无铺垫关系突变或高风险样文复刻会阻止入稿。
- **知识层不是把共现当关系**：结构化事实和关系保留来源/证据，候选推断不会自动升级为真相。
- 可变状态支持 superseded：角色移动后，旧“当前地点”不会继续和新地点同时注入。
- 知识图谱是可重建的检索投影，人物卡、世界书和已接受正文仍是事实源。

### 多文件参考资料库

- 支持 TXT / MD / DOCX / EPUB / PDF / HTML 等文本资料。
- 资料分类为：**文风样文 / 原作正典 / 世界背景 / 研究资料**。
- 文风分析只消费“文风样文”，人物正典分析只消费“原作正典”，避免职责混淆。
- 支持多篇样文聚合分析，并对候选正文做长片段精确重合检查。

### 审计与质量系统

- 检查长度、元话语、Markdown、重复原文、循环措辞、AI 套话与疑似截断。
- 审计人物人格、声音、知识来源、关系、道具、伤势、位置、时间与世界规则。
- 识别跨章原句或段落复用、疲劳词、章节相似度和分卷阶段雷同。
- 历史题材可检查现代术语密度，降低语言时代错位。
- 审计建议可逐项选择并执行最小修订，不直接覆盖已确认正文。

### 本地可靠性与数据安全

- SQLite 使用 WAL 与写入等待保护，自动维护数据库备份。
- 章节验收采用可审计提交状态；耗时提取期间会从服务器最新项目合并，避免覆盖其他章节的并行编辑。
- 每章在保存、接纳草稿和自动导演写入前保留独立快照。
- 完整项目可导出并重新导入为独立副本。
- 长任务绑定发起时的作品和章节，切换页面不会把结果串写到其他章节。
- 自动导演支持暂停、恢复、严格模式和无人值守质量债务模式。
- 每次正文生成自动冻结实际模型消息、检索来源、Skill、预算裁剪和提示词 SHA-256；可在界面审计并用新的模型配置回放。
- 快照不会保存 Provider 凭据，凭据字段和疑似 API Key 在入库前统一脱敏。

## 长篇防跑偏机制

砚火通过十六层上下文与状态控制维持长篇一致性：

```mermaid
flowchart TD
    A["作者意图与故事圣经"] --> B["分卷战略与因果契约"]
    B --> C["逐章路线与本章执行计划"]
    C --> D["人物核心、世界书与硬规则"]
    D --> E["事实、知情、关系、伏笔与时间线"]
    E --> F["相关性检索与上下文预算"]
    F --> G["续写锚点、文风指纹与描写账本"]
    G --> H["候选草稿"]
    H --> I["本地检查与 AI 连续性审计"]
    I -->|作者确认| J["写入正文并更新叙事状态"]
    J --> E
    I -->|需要调整| K["最小修订或人工编辑"]
    K --> H
```

提示词预览会展示每个上下文区块的优先级、估算 token、FTS/词法记忆来源、激活的写作 Skills 及预算裁剪轨迹，并可冻结为可审计快照。

## 快速开始

### 环境要求

- Windows 10 / 11
- Python 3.10 或更高版本
- SiliconFlow/xAI API Key（云端方案）或已经启动的 OpenAI-Compatible / llama.cpp 服务

### 1. 启动砚火

```powershell
git clone https://github.com/star031104/inkforge-local.git
cd inkforge-local
.\run.bat
```

访问 **http://127.0.0.1:7860**。首次启动会自动创建虚拟环境并安装依赖。`run.bat` 只对本次子进程使用 PowerShell `ExecutionPolicy Bypass`，不会永久修改系统执行策略。

### 2. 使用 SiliconFlow Qwen3-8B

右上角 **模型设置** 选择 `SiliconFlow`：

```text
API URL: https://api.siliconflow.cn/v1
Model:   Qwen/Qwen3-8B
API Key: 你的 SiliconFlow API Key
Thinking: 关闭
```

应用会使用 OpenAI-Compatible `/chat/completions`，并对 Qwen3 显式发送 `enable_thinking=false`。

> 推荐通过环境变量提供 API Key，使密钥不进入项目数据库；如果在界面中填写，密钥只保存在本机数据库，导出项目 JSON 时会自动移除。

### 3. 使用 xAI / Grok 4.6

在模型设置中点击“填入 xAI Grok 4.6”，或配置：

```text
API URL: https://api.x.ai/v1
Model:   grok-4.6
API Key: 你的 xAI API Key
```

更安全的做法是在启动砚火前设置 `XAI_API_KEY` 或 `INKFORGE_XAI_API_KEY`，界面里的 Key 留空。关闭“模型思考”时，xAI 请求使用 `reasoning_effort=low`；开启时使用 `high`。Grok 在这里是可替换的生成 Provider，小说长期记忆仍由本机 InkForge 状态库管理。

### 4. 后续切换到本地 llama.cpp

先启动本地模型，例如：

```powershell
llama-server.exe `
  -m D:\Models\your-model.gguf `
  --host 127.0.0.1 `
  --port 8080 `
  -c 32768 `
  -ngl 99
```

然后在模型设置选择 `本地 llama.cpp`：

```text
API URL: http://127.0.0.1:8080/v1
Model:   local-model（或服务实际模型 ID）
API Key: 可留空
Thinking: 关闭
```

无需重新建立项目，原有角色、世界观、样文、知识图谱和章节记忆全部继续使用。

## 推荐创作工作流

### 专业分层创作

1. 在“灵感孵化”输入一句想法，从两套方案中选择更有潜力的方向。
2. 生成并审核故事圣经、人物卡、世界书、分卷契约与详细蓝图。
3. 逐卷生成章节因果路线，再批量建立或同步章节。
4. 为当前章节生成执行计划，明确开篇第一拍、场景因果拍和章末状态。
5. 生成候选草稿，执行质量检查和连续性审计。
6. 选择问题进行最小修订，确认后插入正文并更新长期记忆。
7. 定期运行项目体检，处理重复内容、线索债务和记忆来源问题。

### 全文自动导演

如果希望从一个灵感直接开始整本生产，可使用“一键创作全文”。系统会串行执行：

```text
灵感 → 故事骨架 → 人物卡与世界书 → 故事圣经
     → 分卷契约 → 逐卷蓝图 → 逐章路线
     → 本章计划 → 正文 → 审计 → 修订 → 记忆回灌
```

自动导演针对本地 8–12B 模型采用细粒度检查点。模型断开、持续超时或结构化输出无法闭合时会安全暂停；软质量问题则保留最佳完整候选、记录质量债务并按所选模式继续。重启应用后可从最近检查点恢复。

## 技术架构

```text
inkforge-local/
├─ app/
│  ├─ main.py                 FastAPI、业务接口与流式任务
│  ├─ db.py                   SQLite 存储、快照与恢复
│  ├─ llama_client.py         OpenAI-Compatible HTTP / SSE 客户端
│  ├─ providers.py            xAI / SiliconFlow / llama.cpp / 通用 Provider 参数隔离
│  ├─ prompts.py              世界书、Canon、知识层与分层提示词编排
│  ├─ knowledge.py            实体 / 事实 / 关系 / 可变状态知识层
│  ├─ canon.py                同人原作 Canon Profile 与 OOC 审校规则
│  ├─ references.py           多文件参考资料检索与样文防复刻
│  ├─ file_parsing.py         TXT/MD/DOCX/EPUB/PDF/HTML 文本解析
│  ├─ planning.py             全书、分卷与章节规划
│  ├─ memory.py               长期记忆提取与相关性检索
│  ├─ memory_integrity.py     正文哈希、验收提交、降级与确定性滚动摘要
│  ├─ writing_skills.py       只读/个人/项目写作 Skills 与安全激活
│  ├─ lore.py                 世界书条件与递归关联
│  ├─ style_engine.py         文风统计与动态示例选择
│  ├─ quality.py              单章确定性质量检查
│  ├─ manuscript_quality.py   跨章与全稿级质量分析
│  └─ fallbacks.py            本地安全降级策略
├─ static/                    原生 Web 写作界面
├─ data/                      本地数据库与自动备份（不入库）
├─ requirements.txt
├─ run.bat                    Windows 推荐启动入口（自动绕过当前进程执行策略）
└─ run.ps1                    PowerShell 启动脚本
```

### 技术栈

| 层 | 技术 |
| --- | --- |
| Web API | FastAPI + Uvicorn |
| 数据模型 | Pydantic |
| 模型通信 | HTTPX + OpenAI 兼容流式接口 |
| 数据持久化 | SQLite（WAL） |
| 前端 | 原生 HTML / CSS / JavaScript |
| 推理后端 | xAI Grok / SiliconFlow / llama.cpp / 任意 OpenAI-Compatible 服务 |

## 模型建议

砚火不绑定特定模型，Provider 层允许同一个项目在云端 API 与本地推理之间切换：

- **SiliconFlow `Qwen/Qwen3-8B`**：成本、中文能力和结构化输出速度较均衡，适合快速开始完整创作工作流。
- **xAI `grok-4.6`**：适合更强的长文本生成、规划和审校；建议通过环境变量提供 Key。
- **本地 7B / 8B GGUF**：适合轻量续写、灵感讨论和短场景，对显存要求较低。
- **本地 12B / 14B GGUF**：规划、人物一致性和中文表达的综合平衡更好。
- **本地 32B 及以上 GGUF**：复杂人物关系、长场景与文风控制通常更稳定，但资源要求更高。

实际效果取决于模型能力、上下文长度、采样设置，以及本地量化、显存卸载参数。无论切换哪种 Provider，项目中的人物、世界观、Canon、知识图谱和章节记忆均保持不变。

## 数据存储与隐私

- 项目数据默认存储在本机 `data/inkforge.db`。
- 自动备份位于 `data/backups/`，默认轮换保留 12 份。
- 数据库、备份、日志和 SQLite 临时文件已通过 `.gitignore` 排除。
- 默认模型预设是 SiliconFlow `Qwen/Qwen3-8B`；也可一键使用 xAI `grok-4.6`，或切换为完全离线的本地 llama.cpp。
- 导出完整项目 JSON 时会自动清除 `settings.api_key`，避免把云端密钥带入备份文件。
- 导入样文前请确认你拥有相应使用权；文风学习用于抽取一般写作特征，不应复制原句或专有设定。

## 发布说明

GitHub 仓库只保留运行项目所需的源代码、静态资源、依赖清单、启动脚本和本 README。开发期测试、QA 产物、基准结果与内部设计文档均保留在本地，不随项目发布。

## 当前边界

- 文风学习属于 RAG / 提示词层的软学习，不是 LoRA 微调。
- token 数为本地估算，不同模型的实际分词结果会有差异。
- 连续性审计能够降低事实错误，但不能代替作者对关键设定的确认。
- 本地规则擅长发现可确定的机械问题，不能判断主题深度或文学价值。
- 本地小模型的规划质量高度依赖模型能力、量化方式、上下文长度和采样设置。

## 设计参考

项目在架构思路上重点研究了：

- [Storydex](https://github.com/TensorHub-ORG/Storydex) 的事实源 / WIKI / 知识图谱分层、证据关系和活跃实体上下文；
- [InkOS](https://github.com/logep/inkos) 的写—审—改闭环、真相文件和风格指纹；
- [Ai-Novel](https://github.com/inliver233/Ai-Novel) 的结构化对象与模型配置解耦；
- [NovelClaw](https://github.com/iLearn-Lab/NovelClaw) 的长期写作工作区和可检查记忆面板；
- [SillyTavern World Info](https://docs.sillytavern.app/usage/core-concepts/worldinfo/) 的关键词世界信息机制。

0.18 的知识层、Canon Lock 与 Provider 层均为 InkForge 独立实现，没有复制上述项目的源代码；Storydex 和 InkOS 的许可边界详见 `REFACTOR_RESEARCH.md`。

---

<div align="center">

**让模型记住故事，让作者掌握方向。**

如果砚火对你的创作有所帮助，欢迎为项目点亮一个 ⭐。

</div>
