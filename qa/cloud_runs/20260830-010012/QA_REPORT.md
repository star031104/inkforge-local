# InkForge 0.19.0 Cloud Full E2E QA

- 开始：2026-08-30T01:00:12.906324+08:00
- 结束：2026-08-30T01:23:48.632905+08:00
- 模型：Qwen/Qwen3-8B
- Base URL：https://api.siliconflow.cn/v1
- 结果：**FAIL**
- API Key：未写入报告、项目 JSON 或持久测试数据；仅由进程环境读取。

## 步骤

| 状态 | 测试 | 详情 |
| --- | --- | --- |
| PASS | 本地自动测试全集 | ........................................................................ [ 44%] ........................................................................ [ 89%] .................                                                        [100%] 161 passed in 7.27s |
| PASS | SiliconFlow 模型列表 | 发现 64 个 chat 模型；目标模型=存在 |
| PASS | 普通 Chat Completion | 连接正常 |
| PASS | 结构化 JSON | {"ok":true,"mode":"non-thinking","model":"qwen3-8b"} |
| PASS | SSE 流式 + 非思考正文 | 177 字 |
| PASS | Provider 直连总耗时 | 11.0s |
| PASS | FastAPI 健康检查 | {"status":"ok","app_version":"0.19.0","api_schema_version":34,"database":"ready","active_director_tasks":0} |
| PASS | 环境变量 Key 注入/不落项目 | {"provider": "siliconflow", "base_url": "https://api.siliconflow.cn/v1", "model": "Qwen/Qwen3-8B", "has_api_key": true, "api_key_source": "environment", "non_thinking": true} |
| PASS | 真实模型·多样本文风分析 | 冷峻克制风格 |
| PASS | 真实模型·章节细纲 | 沈砚通过核对两册粮牍，确认同旬差额存在，并获得次日继续检查第三册的有限资格。 |
| PASS | 提示词/世界书激活 | 秦统一前称谓、仓曹物质条件 |
| PASS | 真实模型·历史正文第一章 | 966 字；repair=True; forbidden=[] |
| WARN | 本地质量门禁·第一章 | score=86 verdict=revise |
| PASS | 真实模型·连续性审计 | score=86 verdict=revise |
| PASS | 真实模型·整章审计修订 | 764 字；repair=False |
| WARN | 真实模型·章节记忆提取 | 雨水沿着瓦当落进石槽，沈砚的指尖沾着墨迹，正将算筹逐一枚排开。他照着第一册木牍的数字，重新在另一块木牍上记下，动作沉稳，如同在打磨一块玉。他将两册木牍收入案几，动作利落，却似藏着未言的不安。秦王政未动，只是静静看着沈砚，仿佛在衡量他是否值得信任。沈砚将目光移回木牍，指尖轻轻摩挲算筹的棱角，像是在确认它们是否曾被他人动过。 |
| WARN | 记忆回灌/证据投影 | facts=0 |
| PASS | 知识图谱投影 | nodes=5, edges=0 |
| PASS | 真实模型·第二章细纲 | 沈砚成功说服秦王政继续检查第三册账册，获得有限的继续权限。 |
| PASS | 跨章相关记忆召回 | retrieved=1 |
| PASS | 真实模型·历史正文第二章/续写 | 710 字；repair=False |
| PASS | 真实模型·重写选中 | 317 字 |
| PASS | 真实模型·扩写选中 | 342 字 |
| PASS | 全稿体检 | score=100 |
| PASS | 真实模型·Canon Profile | must_not=6, explicit=15 |
| PASS | 真实模型·Canon Lock 生成 | 607 字；repair=True |
| PASS | 真实模型·OOC/Canon 审校 | score=95 verdict=pass |
| PASS | 真实模型·灵感推荐 | options=3 fallback=False |
| FAIL | 真实模型·灵感孵化 | {"detail":"灵感孵化没有得到完整方案：模型返回的 JSON 未闭合，通常是输出被截断，请重试。原始灵感未被修改，请重试。"} |
| PASS | 真实模型·全书分层规划 | volumes=2 fallback=False |
| PASS | 真实模型·自动导演启动 | task=a4eef7fb project=c14aab5d |
| FAIL | 真实模型·自动导演3章完成 | status=paused error=分卷详细剧情梗概至少需要 180 字 |

## 说明

FAIL 表示功能链路或硬约束未通过；WARN 表示功能可用但模型输出触发了回退、质量门禁或需要作者复核。
