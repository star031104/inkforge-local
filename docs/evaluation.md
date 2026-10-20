# 长篇评测体系

评测的目的是回答一个具体问题：在相同作者意图与事实材料下，换了提示词版本或模型设置后，章节是否更连贯、更少重复、成本是否可接受。确定性指标不是文学评分；文学偏好由两两盲评单独给出。

代码：`app/evaluation/longform.py`（指标、探针、盲评、历史），`app/api/routers/evaluation.py`（接口），`scripts/evaluate_longform.py`（运行器），`static/js/features/evaluation/evaluation.js`（审校页 →“质量评测”）。

## 固定基准集

`tests/fixtures/longform_cases/` 内三本原创基准书，每本 10 章路线：

| 用例 | 题材 | 重点考察 |
| --- | --- | --- |
| `historical_transmigration` 盐井账 | 历史穿越 | 现代术语只许在主角内心、死者复活、已毁官印、伤势期间能力、配角知情边界 |
| `mystery` 雾港旧案 | 悬疑 | 证物被毁后不能再播放、证人死亡、真凶第 9 章前不得点破、年代用语 |
| `relationship` 晚灯 | 情感/关系 | 已故外婆不能开口、失聪秘密的揭示时机、钢琴修好的时间点、告白时机 |

每个用例包含作者意图、硬规则、人物（含知情边界）、事实、伏笔（`planted_chapter` / `payoff_by` / `payoff_terms`）、10 章路线以及**探针**。

探针是逐句匹配的正则：`{"type":"forbidden","category":"<ConStory 子类>","pattern":…,"from_chapter":n,"until_chapter":m,"exclude_context":…}`。句子同时命中 `exclude_context`（回忆、梦、遗体等）时不计。

## 指标

| 指标 | 计算 | 方向 |
| --- | --- | --- |
| 冲突率 | 确定矛盾数 / 千字。来源：基准探针；由作品自身记忆派生的探针（结算中已死亡的人物之后开口或行动、知识层记为毁坏/遗失的物品之后被持有）；权威状态内部冲突（同一人物同一可变槽位有多个已确认值、事实失效章节早于生效章节） | 越低越好，是下限 |
| LLM 冲突率 | 可选的模型检查结果，单独存放于 `llm_conflicts`，不与确定矛盾相加 | 越低越好 |
| 套话率 | 通用模板套话 + 本书学习到的疲劳短语命中 / 千字，附高频短语 | 越低越好 |
| 弃坑率 | 已到期却未推进的线索 / 已有结论的线索。来源：记忆中的 `plot_threads`（按线索生命周期判断静默过久，或全书完结仍未回收）与基准伏笔 | 越低越好 |
| 提取截断率 | 记忆提取调用中 `finish_reason=length` 的比例 | 越低越好 |
| 审校/写作 token 比 | (critic+audit) / (prose+revision) 的输入输出 token | 越低越省 |
| 平均输入 tokens、每章 tokens、每章成本 | 来自调用统计；单价需显式提供，否则不推算费用 | — |
| 全稿健康分、重复段落、相似章节 | 复用 `manuscript_health_report` | 健康分越高越好 |
| 对白占比、句长均值与离散度、章节字数离散度 | `style_stats` 与逐句统计 | 参考值 |
| ConStory 19 类 | 章节最新审校问题按下表映射计数；机械问题记为“非一致性” | 越低越好 |

| 一致性问题密度 | ConStory 一致性问题数 / 千字（`constory.density`） | 越低越好 |
| 按长度归一（`normalized`） | 确定矛盾、一致性问题、机械问题、套话命中按千字；重复段落、弃坑线索按万字 | 越低越好 |

### 终局窗口（`metrics.terminal`）

参照 NstAgent 的做法，长篇真正的难点是写到后段还能不能自洽，所以另报一组只看结尾的指标：

- 窗口为全书**最后约 10,000 个非空白字**（`terminal_chars`，接口参数 `?terminal_chars=`，运行器 `--terminal-chars`，界面“终局窗口”可改，范围 1,000–200,000）。全书不足窗口时按全文计算，并标记 `whole_text: true`。
- **检测仍然对照全书前文**：探针由全书已结算的状态派生，每一章都会扫描；终局窗口只按问题的*位置*决定是否计入。例如第 3 章已死的人物在最后一章开口，会计入终局窗口。
- 位置：探针命中精确到句子（`offset`/`end`，以非空白字计）；审校问题优先用其引文（`evidence`/`quote`/`excerpt`）在正文中的位置，找不到引文时用整章范围，章节与窗口有重叠即计入；没有章节位置的权威状态冲突记为 `unlocated_conflicts`，只计入全文。
- 输出：`conflicts`、`conflict_rate`（/千字，分母是窗口字数）、`continuity_issues`、`constory_density`、`from_chapter`、`window_chars`。摘要卡片、Markdown 报告、运行器控制台和界面都会并列给出全文与终局两组数字。

### 机器味信号（`metrics.machine`）

全部是确定性统计，只适合比较同一套流程的不同版本，**不能**用来判断文本是否 AI 所写：

| 信号 | 计算 | 方向 |
| --- | --- | --- |
| 字 TTR | 去掉标点空白后，每 1,000 字窗口内不同字 / 总字，取各窗口均值（MATTR 式，避免长书吃亏） | 越高用字越丰富 |
| 双字 TTR | 同上，按相邻两字组合 | 越高搭配越丰富 |
| 3/4/5 字串重复率 | 每 5,000 字窗口内，重复出现的 n 字串占全部 n 字串的比例，取均值；n 字串不跨标点 | 越高越模板化 |
| 句长标准差、变异系数 | 按句末标点切句后的字数统计 | 过低说明句式单调 |

调用统计不含作品 id（设计上不存正文与提示词），因此作品的调用按“作品创建时间之后”的时间窗口近似归属；离线运行器使用创建到最后更新的窗口。

### ConStory 分类映射

参照 ConStory-Bench（arXiv 2603.05890）的五大类，改写为适合中文长篇的 19 个子类：

| 大类 | 子类 |
| --- | --- |
| 人物 | 记忆矛盾、知情越界、能力漂移、技能遗忘、性格动机突变 |
| 事实细节 | 外貌矛盾、命名不一致、数量数值矛盾、物品状态矛盾 |
| 叙事风格 | 视角漂移、语气文风突变、时代语汇失真 |
| 时间线与情节 | 时间矛盾、因果违背、弃坑、情节重复 |
| 世界观 | 规则违背、地理位置矛盾、未确认新设定 |

映射规则见 `CONSTORY_RULES`：先排除机械类（长度、句式、套话、重复段落等），再按关键词顺序先查问题类别、后查问题描述。例如“人物知识/逻辑”→知情越界，“疑似新增往事”→未确认新设定，“情节逻辑”→因果违背。

## 两两盲评

规划审校模型作为评委，对左右两稿按固定维度（因果推进、人物可信、对白辨识度、信息揭示、节奏、新鲜度）各判胜负或平局。每对稿件以 A/B 与 B/A 两种顺序各评一次，只有两次一致的维度才计胜负，不一致记平局；同时报告“位置一致率”，越低说明评委越受位置影响。胜率 = (胜 + 0.5 × 平) / 维度数。

## 运行器

```powershell
# 生成三本基准书并评测（每章用真实的 build_prompt，未运行审校、修订和记忆结算）
python scripts/evaluate_longform.py --case all --base-url http://127.0.0.1:8080/v1 --model local-model --prompt-version prose-2.1.0

# 只生成前 4 章、指定单价、并与上次运行对比
python scripts/evaluate_longform.py --case mystery --chapters 4 --base-url ... --model ... --input-price 1 --output-price 4 --baseline output/eval/<上次运行目录>

# 不调用模型，评测本机数据库里的已有作品
python scripts/evaluate_longform.py --offline --project-id all

# 终局窗口改为最后 2 万字
python scripts/evaluate_longform.py --offline --project-id all --terminal-chars 20000

# 对比两次运行；加 --pairwise 时对同一用例的正文逐章盲评
python scripts/evaluate_longform.py --compare output/eval/<A> output/eval/<B> --pairwise --base-url ... --model ...
```

API Key 可用 `--api-key` 或环境变量 `INKFORGE_API_KEY`；`--model-config` 可传入一个模型设置 JSON。生成模式的调用统计写入本次运行目录旁的独立数据库，不进入作者的调用记录。

输出位于 `output/eval/<时间>-<标签>/`：`run.json`、每本书的 `<用例>.json` 与 `<用例>.md`、汇总 `report.md`；对比输出 `compare.json` 与 `compare.md`。

## 接口

- `GET /api/projects/{id}/evaluation?record=true&input_price=&output_price=&terminal_chars=`：返回全部指标与摘要卡片（含全文与终局窗口两组）；`record=true` 时写入历史。
- `GET /api/projects/{id}/evaluation/history?limit=&kind=metrics|pairwise`
- `POST /api/projects/{id}/evaluation/pairwise`：`{left_text, right_text, left_label, right_label, context}`。

历史以 JSONL 保存在数据库目录下的 `evaluations/<作品 id>.jsonl`，不写入作品 JSON，因此记录评测不会产生新的作品版本。

## 局限

- 确定矛盾只覆盖能用规则判断的情形，是下限；探针为保守匹配，会漏报，也可能在少见句式下误报，报告附带证据句以便复核。
- 生成模式走轻量直连路径，用于隔离提示词和模型本身的表现；完整导演流程（审校、修订、记忆结算）的评测请在应用内运行后用 `--offline` 评分。
- 盲评仍是模型判断，适合比较同一章的两个版本，不应作为唯一的取舍依据。
