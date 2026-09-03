# InkForge 0.17 长篇小说记忆 / 同人正典重构研究报告

> 本文记录 0.17 重构为什么这样设计。目标不是把某个开源项目“搬进来”，而是比较不同项目的有效机制，再用 InkForge 自己的数据结构重新实现。

## 1. 这次要解决的真正问题

对 4～5 万字乃至更长的小说，模型“上下文够不够长”并不是唯一问题。真正让人物和设定跑掉的常见原因是：

1. **事实没有层级**：原作正典、作者硬设定、模型猜测、章节临时状态被混在一起。
2. **状态没有时间性**：角色已经从 A 地移动到 B 地，但 A、B 两条“当前地点”同时被检索。
3. **人物卡只存百科信息**：姓名、身高、能力都在，却没有“怎么思考、怎么说话、什么绝不会做”。
4. **参考小说和原作资料混用**：文风样文负责“怎么写”，原作资料负责“写谁”；若混在同一检索池，容易既 OOC 又复刻原句。
5. **长篇记忆只做摘要**：摘要能保剧情大势，却不适合回答“谁知道什么”“两人当前关系是什么”“某能力的限制是什么”。
6. **模型生成的知识直接成为真相**：一次错误抽取会在后续几十章不断被强化。
7. **全量上下文灌输**：把所有设定、所有章节摘要、所有人物卡每次都塞进去，会让真正关键的约束反而被稀释。

因此本次重构把目标定为：

> **事实源明确 + 检索投影可重建 + 原作角色正典可人工核对 + 章节接受后才更新硬记忆 + 文风学习不等于原句复刻。**

---

## 2. Storydex：最值得借鉴的是“知识治理”，不是图谱画布

研究项目：<https://github.com/TensorHub-ORG/Storydex>

Storydex 当前把小说项目作为普通本地目录，并将角色、世界书、记忆、WIKI、图谱等拆开管理。它最重要的原则是：**项目文件是事实源，WIKI/索引/图谱属于可重建的投影。**

### 2.1 它的知识图谱为什么比“人物同章出现就连线”可靠

Storydex 的关系层明确区分：

- canonical entity / 实体注册表；
- confirmed fact / 已确认事实；
- candidate relation / 模型提议的候选关系；
- relation review ledger / 待审核关系；
- source/evidence / 关系依据；
- planned / observed / inferred 等不同知识状态。

它甚至专门避免把“共现”直接等价为关系。这一点很关键：

> 狂三和花火在同一章出现，只能证明两人出现在同一文本范围，不能证明她们是朋友、敌人、亲密关系或竞争关系。

### 2.2 它不是每次把整张知识图谱塞给模型

Storydex 的上下文装配思路更值得采用：

1. 最近正文片段；
2. 滚动章节摘要；
3. 当前活跃角色结构；
4. 当前相关世界设定；
5. 与活跃实体有关的已确认事实；
6. 只取一跳/有限深度的关系邻域；
7. FTS/BM25 相关文本；
8. WIKI 参考条目；
9. 当前变量状态。

每个区块都有独立预算，而不是无限堆积。

**InkForge 0.17 的对应实现：**

- `app/knowledge.py`：实体、事实、关系、状态替换；
- `app/prompts.py`：活跃实体 → 相关知识 → 关系邻域；
- 原来的故事记忆、世界书、章节摘要仍然保留；
- 图谱只是 UI/检索投影，不取代人物卡、世界书和正文。

### 2.3 为什么没有直接复制 Storydex 代码

Storydex 当前 LICENSE 是 **Apache-2.0 + Commons Clause** 的 source-available 条款。0.17 只借鉴架构思想，知识层代码为 InkForge 独立实现，不复制其源代码。

参考：<https://github.com/TensorHub-ORG/Storydex/blob/main/LICENSE>

---

## 3. InkOS：最值得借鉴的是“真相文件 + 写审改闭环”

研究项目：<https://github.com/logep/inkos>

InkOS 已经不是单纯的提示词工具，而是完整的小说 Agent。它的几个设计非常适合 InkForge：

### 3.1 写 → 审 → 改，而不是一遍生成就入稿

InkOS 将 Writer、Auditor、Revision 形成管线；这和 InkForge 已有的候选草稿、连续性审计、最小修订理念相同。

0.17 继续保持：

- AI 输出先进入候选草稿；
- 正文不会自动覆盖；
- 角色正典可以单独审校；
- 最终接受后才更新长期状态。

### 3.2 “真相文件”比纯摘要更适合长篇

InkOS 续写已有作品时会逆向建立世界状态、人物矩阵、资源账本、伏笔等长期结构。这个方向说明：

> 长篇真正需要的是多个专门的状态账本，而不是一个越来越长的“前情提要”。

InkForge 原本已经有：

- facts；
- plot_threads；
- timeline；
- relationships；
- knowledge_ledger；
- description_ledger。

0.17 没有把这些删掉，而是增加一层结构化知识投影，避免破坏已经成熟的记忆体系。

### 3.3 文风学习应该是“统计指纹 + 抽象指南”

InkOS 的文风分析不只是把样文原文塞进 prompt，而会提取风格特征。InkForge 也已有 style fingerprint，因此 0.17 做的是升级：

- 支持多文件样文；
- TXT / MD / DOCX / EPUB / PDF / HTML；
- 多篇样文聚合分析；
- 只把“style”类别送入文风分析；
- 生成后检查与样文之间是否存在过长精确重合。

### 3.4 许可边界

InkOS 当前采用 AGPL-3.0。0.17 同样只参考工作流思想，不复制其具体实现。

参考：<https://github.com/logep/inkos/blob/master/LICENSE>

---

## 4. Ai-Novel Lite：最值得借鉴的是“结构化对象 + 可替换模型层”

代表性比较项目：<https://github.com/inliver233/Ai-Novel>

> “ai-novel”存在多个同名项目，这里以当前检索到的 `inliver233/Ai-Novel` Lite 分支作为代表，不表示所有同名项目都采用相同架构。

它把以下对象独立建模：

- 项目；
- 角色；
- 大纲/细纲；
- 章节；
- 世界观条目；
- Prompt；
- 模型配置；
- 向量检索/结构化记忆。

它更偏“平台化”：FastAPI + React + PostgreSQL + Redis/RQ + pgvector。

对 InkForge 而言，不需要为了个人本地写作直接引入 PostgreSQL/Redis；SQLite 的可靠性和便携性更合适。但它提醒我们两件事：

1. 模型配置应该和写作业务解耦；
2. Prompt/记忆/章节是不同领域对象，不应全部塞进一个字符串。

因此 0.17 新增 `app/providers.py`，把 SiliconFlow、本地 llama.cpp、通用 OpenAI-Compatible 三种 Provider 的参数差异隔离起来。

---

## 5. NovelClaw：最值得借鉴的是“可检查工作区”

研究项目：<https://github.com/iLearn-Lab/NovelClaw>

NovelClaw 强调：小说写作不是一次 prompt，而是一个长期工作区。它暴露 session、run、chapter、storyboard、world、character、style、memory bank 等操作面。

InkForge 原本已经具有非常接近的理念：

- 章节计划可查看；
- 提示词预览可查看；
- 草稿先审后入；
- 长任务有检查点；
- 记忆可手工修改。

0.17 加入的知识图谱、参考资料库、Canon Lock 都继续遵守“**可见、可编辑、可人工确认**”原则，而不是隐藏在 Agent 内部。

---

## 6. Grok 网页版方法如何落进 InkForge

之前讨论的 Grok 长篇方案，本质可以概括为：

```text
参考小说 → 提炼文风档案
原作资料 → 建立角色正典档案
作者要求 → 世界观 / 人物关系 / 大纲
逐章写作 → 每几章状态存档
继续写 → 注入当前相关状态
```

0.17 将它产品化：

| Grok 手工操作 | InkForge 0.17 |
| --- | --- |
| 手动上传几篇参考小说 | 参考资料库，多文件导入 |
| 让模型总结文风 | 综合文风分析 + 统计指纹 |
| 手动整理狂三/花火/绘梨衣人物卡 | Canon Profile + 原作资料分析 |
| 每次提醒“不要 OOC” | 最高优先级 Canon Lock |
| 每几章让 Grok 做状态总结 | 接受章节后自动记忆回灌 |
| 手工回顾人物关系 | 结构化关系邻域 + 图谱 |
| 手工告诉模型哪些事实最重要 | 活跃实体相关事实检索 |
| 担心 AI 抄样文 | 精确长片段重合检测 |

因此 InkForge 不再依赖“同一个聊天窗口一直记着”，而是把记忆显式保存下来。

---

## 7. 0.17 的新数据分层

### 7.1 Authoritative / 权威层

真正能定义后续写作的来源：

1. 作者手工输入；
2. 已人工核对的 Canon Profile；
3. 已接受正文；
4. 已确认的人物卡 / 世界书；
5. 已确认结构化事实与关系。

### 7.2 Candidate / 候选层

不得自动成为真相：

- AI 从资料分析出的未人工核对 Canon Profile；
- candidate fact；
- candidate relation；
- 推测性资料。

### 7.3 Retrieval Projection / 检索投影

可以删掉后重建：

- entity index；
- knowledge graph；
- 当前相关事实列表；
- 当前关系邻域。

这能避免“索引坏了 = 小说设定丢了”。

---

## 8. 原作同人角色一致性：为什么要单独做 Canon Lock

以时崎狂三、花火、上杉绘梨衣为例，最危险的 OOC 并不是“发色写错”这种表面错误，而是：

- 为了让恋爱推进，突然无条件信任主角；
- 所有人都变成同一种“嘴硬吃醋女主”；
- 语言习惯逐渐同质化；
- 能力代价/限制被忘记；
- 角色知道了自己不可能知道的信息；
- 原作核心执念被当前剧情便利覆盖。

因此 Canon Profile 专门保存：

- source work；
- timeline node；
- identity；
- appearance anchors；
- core/deep personality；
- values/goals/fears；
- abilities/limitations；
- speech style；
- behavior/emotional/relationship patterns；
- must_preserve；
- must_not；
- source_refs；
- user_verified。

并采用以下优先级：

```text
人工核对的原作核心设定
    > 已确认正文事实
    > 当前人物关系和状态
    > 大纲
    > 单章剧情便利
```

如果大纲要求狂三做出明显违背角色核心的行为，正确处理不是“让狂三配合大纲”，而是改造剧情实现。

---

## 9. 章节接受后的知识回写

0.17 不会在模型刚生成正文时写入硬知识。

只有：

```text
候选正文
  ↓
作者接受
  ↓
长期记忆提取
  ↓
证据必须能在已接受正文中找到
  ↓
状态/关系写入结构化知识层
```

当前自动投影包括：

- 人物当前地点；
- 人物当前状态；
- 有证据的人物关系更新。

“当前地点”等可变事实使用 superseded 机制：新值确认后旧值不再作为当前真相检索。

**不会做的事：**

- 不因两人同章出现自动建立关系；
- 不把模型猜测直接设为 confirmed；
- 不把没有正文证据的关系变化写入硬知识。

---

## 10. 为什么暂时不引入 Neo4j / pgvector

对于目前 InkForge 的目标——单作者、单机、4～20 万字量级小说——直接引入图数据库会显著增加部署成本，而不一定提升最终写作质量。

目前优先级更高的是：

1. 数据正确；
2. 知识有证据；
3. 活跃实体选择正确；
4. 可变状态不会互相冲突；
5. Prompt 预算正确；
6. 原作角色不 OOC。

因此 0.17 先采用 SQLite + JSON 项目对象 + 内存相关性检索。如果未来作品达到数百万字、多项目共享资料库或需要大规模语义检索，再接 embedding/pgvector 会更合理。

---

## 11. 模型层重构

### SiliconFlow

默认：

```text
provider = siliconflow
base_url = https://api.siliconflow.cn/v1
model = Qwen/Qwen3-8B
enable_thinking = false
```

Provider 层只发送 SiliconFlow 支持的字段；`min_p` 用于 Qwen3，非思考写作显式使用 `enable_thinking=false`。

### 本地 llama.cpp

以后切本地只改设置：

```text
provider = llama_cpp
base_url = http://127.0.0.1:8080/v1
model = 你的本地模型 ID
enable_thinking = false
```

业务层、Prompt、知识图谱、Canon Lock、样文系统都不用改。

---

## 12. 这次重构刻意没有做的事情

- 没有直接复制 Storydex/InkOS 源码；
- 没有把整个项目迁成 React/Vue；当前原生前端足够轻量；
- 没有引入 Neo4j；
- 没有引入 Redis/RQ；原有导演任务机制可用；
- 没有把模型记忆当原作资料；原作角色事实必须来自用户资料或人工核对；
- 没有让 AI 自动改写已确认正文；作者仍保留最终控制。

---

## 13. 后续最值得继续做的方向

如果继续迭代，建议按这个优先级：

1. **Canon 关系差异矩阵**：同一刺激下，不同原作角色的内心/表面/行动/语言反应模板。
2. **事件实体与因果图**：不仅画人物关系，也记录事件 A → 事件 B 的因果链。
3. **知识候选审核面板**：模型抽取 candidate fact/relation，用户一键确认/驳回。
4. **可选 embedding 检索**：大于几十万字时加入本地 embedding / pgvector / Qdrant。
5. **多模型路由**：Qwen3-8B 做抽取，小型审计模型做检查，大模型只负责正文。
6. **Canon 数据包导入导出**：将已核对的角色正典档案复用于下一本同人小说。


