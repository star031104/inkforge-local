# InkForge 0.18.0 — E2E Hardening

## 核心修复

- **目标字数自适应输出预算**：正文生成会根据目标字数临时提高本轮 `max_tokens`，不再被旧的 1800 token 上限过早截断；项目默认改为 3500。
- **短稿自动补足**：首轮明显短于目标或疑似半句截断时，从原文末尾连续补写，必要时最多补两次，不重写、不重复，SSE 增加 `repair` 与最终长度元数据。
- **提示词预览与真实生成预算一致**：长章节会按实际输出预留上下文，不再出现预览看似充足、真正生成时预算不同的问题。
- **选区重写尺度修复**：`rewrite` 以选中文字长度为目标，不再把百字选区错误扩成整章目标长度。
- **审计修订短选区兼容**：80–99 字选区不会再因修订接口最小目标字数约束触发 422。
- **Provider 错误提示去 llama.cpp 偏置**：云端和本地模型统一显示可理解的连接/HTTP错误。
- **SiliconFlow 配置迁移**：项目显式选择 SiliconFlow 但配置不完整时，会补齐官方兼容 Base URL、`Qwen/Qwen3-8B` 和空 Key，不再错误继承本地 `no-key`。

## Windows 使用体验

- 新增 `run.bat`：推荐 Windows 直接双击/运行，内部仅对本次进程使用 `ExecutionPolicy Bypass`。
- 新增 `run-tests.bat`：一键运行全部 Pytest。
- 新增 `cloud-smoke-test.bat/.ps1` + `scripts/cloud_smoke_test.py`：在用户电脑上对真实 SiliconFlow 做模型列表、普通对话、JSON、流式正文四项烟测；Key 只进入当前进程环境变量，不落盘。

## QA

- 自动测试从 120 项扩展为 **149 项**；并以 `ResourceWarning` 作为错误再跑一遍，仍为 149/149 通过。
- 新增真实本地 HTTP OpenAI-Compatible 仿真服务 E2E，覆盖 `/models`、Bearer、普通 JSON、SSE、`reasoning_content` 丢弃、SiliconFlow 采样参数隔离及正文短稿自动补足。
- 新增项目 CRUD / 版本 / 导入导出 / 备份、参考资料解析、文风分析、Canon、知识事实与关系、全稿体检、导演暂停恢复等 API 表面测试。
- 新增 TXT/GB18030、HTML、DOCX、EPUB、PDF 解析测试。
- 新增历史小说完整管线测试：世界书触发 → 历史正文 → 连续性审计 → 记忆提取 → 证据投影 → 知识图谱。

## 环境说明

本次 ChatGPT 沙盒禁止访问公网 DNS，因此无法从沙盒真正连接 SiliconFlow。所有本地 E2E 均已通过；真实云端调用必须在有公网的电脑运行 `cloud-full-test.bat`（完整验收）或 `cloud-smoke-test.bat`（快速烟测）完成。该限制不会被写成“云端实测通过”。

## 0.18 二轮硬化

- **审计误报收敛**：不再把单独的“原来/本该”当成新增往事；区分个人往事、未确认假设和从铭牌/账册/简牍等当前证据读取的历史信息。
- **权威上下文参与本地审计**：故事总纲、人物卡、世界书、已接受事实/时间线和本章计划可证明某个过去事实并抑制假阳性。
- **最多两次连续补写**：首轮和第一次补写仍明显偏短时允许第二次从原文尾部续补；生成门槛与本地 75% 长度门禁对齐。
- **环境变量 API Key**：SiliconFlow 可通过 `INKFORGE_SILICONFLOW_API_KEY` 注入密钥，避免真实 QA 时把测试 Key 写进项目或 SQLite。
- **Cloud Full E2E**：新增 `cloud-full-test.bat/.ps1` 与 `scripts/cloud_full_e2e.py`，可在联网 Windows 上完成真实 Qwen3-8B 全链路测试并打包 QA 报告与真实测试小说。

## Cloud QA launcher hotfix

- `cloud-full-test.bat` now calls `scripts/cloud_full_e2e.py` directly instead of going through Windows PowerShell.
- `cloud-smoke-test.bat` also calls Python directly.
- The optional PowerShell wrappers are ASCII-only and CRLF-normalized for Windows PowerShell 5.1 compatibility.
- `scripts/cloud_smoke_test.py` can prompt for a hidden temporary SiliconFlow API key itself.
- Added launcher regression tests so future packages fail CI if the main cloud QA path depends on PowerShell encoding again.

## 0.18.1 — Real SiliconFlow QA fixes

基于 2026-08-23 用户联网 Windows 环境真实运行 `Qwen/Qwen3-8B` 得到的 Cloud Full E2E 报告继续修复：

- **灵感孵化短篇/试验卷阈值自适应**：不再用 30+ 章长篇的 450 字全书大纲门槛卡死 3-12 章项目；12 章试验卷使用更合理的结构完整度下限，正式全书规划仍保留更严格验证。
- **自动导演短篇故事骨架阈值自适应**：3-5 章短篇允许 140-300 字压缩 story spine，但仍要求明确覆盖开篇、发展、关键转折/高潮和结局；解决真实 QA 中 3 章导演在 incubator 阶段被 220 字长篇门槛暂停的问题。
- **章节记忆精简 AI 恢复**：完整结构化记忆两次失败后，不再立即退回“只摘正文首尾句”的本地摘要；新增第二级精简结构化 AI 提取，优先保住摘要、人物状态、关键事实、关系变化和证据，再考虑纯本地 fallback。
- **小模型记忆 JSON 容错**：只有 `summary` 与 `story_so_far` 强制存在；空的时间线/线索数组允许省略并在后处理补空，避免 Qwen3-8B 因漏一个空字段导致整份记忆报废。
- **章节记忆输出限额**：限制每类增量条目数量，优先保证 JSON 闭合与证据准确，减少 8B 模型在复杂 schema 下截断。
- **续补重复结尾清洗**：短稿自动补写先缓冲本次 continuation，剥离与既有正文末尾完全相同的回声，并清理足够长的相邻重复句块；真实 QA 中第一章末段连续重复三次的问题得到针对性回归测试。
- **Cloud QA 记忆验收加强**：真实云端报告现在记录 `memory_mode`、记忆 warnings 和事实数量；预设历史测试章若没有提取出任何证据事实会标记 WARN，而不再用 `facts=0` 作为表面 PASS。
- 自动测试增至 **157/157 passed**，并以 `ResourceWarning` 作为错误重复运行同样通过。

