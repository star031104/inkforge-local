# InkForge 0.19.0 Cloud Full E2E QA

- 开始：2026-08-30T12:58:27.720258+08:00
- 结束：2026-08-30T12:58:39.270675+08:00
- 模型：Qwen/Qwen3-8B
- Base URL：https://api.siliconflow.cn/v1
- 结果：**FAIL**
- API Key：未写入报告、项目 JSON 或持久测试数据；仅由进程环境读取。

## 步骤

| 状态 | 测试 | 详情 |
| --- | --- | --- |
| FAIL | SiliconFlow 模型列表 | HTTPStatusError: Client error '401 Unauthorized' for url 'https://api.siliconflow.cn/v1/models?sub_type=chat' For more information check: https://developer.mozilla.org/en-US/docs/Web/HTTP/Status/401 |
| FAIL | 普通 Chat Completion | HTTPStatusError: Client error '401 Unauthorized' for url 'https://api.siliconflow.cn/v1/chat/completions' For more information check: https://developer.mozilla.org/en-US/docs/Web/HTTP/Status/401 |
| FAIL | 结构化 JSON | HTTPStatusError: Client error '401 Unauthorized' for url 'https://api.siliconflow.cn/v1/chat/completions' For more information check: https://developer.mozilla.org/en-US/docs/Web/HTTP/Status/401 |
| FAIL | SSE 流式 + 非思考正文 | HTTPStatusError: Client error '401 Unauthorized' for url 'https://api.siliconflow.cn/v1/chat/completions' For more information check: https://developer.mozilla.org/en-US/docs/Web/HTTP/Status/401 |
| PASS | Provider 直连总耗时 | 4.0s |
| PASS | FastAPI 健康检查 | {"status":"ok","app_version":"0.19.0","api_schema_version":34,"database":"ready","active_director_tasks":0} |
| PASS | 环境变量 Key 注入/不落项目 | {"provider": "siliconflow", "base_url": "https://api.siliconflow.cn/v1", "model": "Qwen/Qwen3-8B", "has_api_key": true, "api_key_source": "environment", "non_thinking": true} |
| WARN | 真实模型·多样本文风分析 | 本地统计文风 |
| WARN | 真实模型·章节细纲 | 沈砚核对两册粮牍，证明同一旬差额值得继续查，并取得次日开第三册的有限资格。 |
| PASS | 提示词/世界书激活 | 秦统一前称谓、仓曹物质条件 |
| FAIL | 历史小说完整管线 | RuntimeError: 模型服务返回 HTTP 401 |
| FAIL | 真实模型·Canon Profile | {"detail":"角色正典档案分析失败：模型服务返回 HTTP 401"} |
| WARN | 真实模型·灵感推荐 | options=3 fallback=True |
| FAIL | 真实模型·灵感孵化 | {"detail":"灵感孵化没有得到完整方案：模型服务返回 HTTP 401。原始灵感未被修改，请重试。"} |
| WARN | 真实模型·全书分层规划 | volumes=2 fallback=True |
| PASS | 真实模型·自动导演启动 | task=093e48e6 project=aca87ac3 |
| FAIL | 真实模型·自动导演3章完成 | status=paused error=模型服务返回 HTTP 401 |

## 说明

FAIL 表示功能链路或硬约束未通过；WARN 表示功能可用但模型输出触发了回退、质量门禁或需要作者复核。
