# 开发与验证

## 本地环境

项目支持 Python 3.10 及以上版本。开发依赖通过 `requirements-dev.txt` 安装：

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements-dev.txt
```

运行服务：

```powershell
.\run.ps1
```

如需与作者作品完全隔离，可先设置独立数据库：

```powershell
$env:INKFORGE_DB_PATH = "$env:TEMP\inkforge-development.db"
.\run.ps1
```

## 提交前检查

```powershell
.\.venv\Scripts\python.exe -m compileall -q app
.\.venv\Scripts\python.exe -m ruff check app tests scripts
.\.venv\Scripts\python.exe -m pytest -q
.\.venv\Scripts\python.exe scripts\evaluate.py
.\.venv\Scripts\python.exe scripts\check_static.py
```

测试启动前会把 `INKFORGE_DB_PATH` 指向临时目录，不会打开正式作品库。浏览器验证也应使用独立数据库。

## 真实模型发布验收

`scripts/acceptance_real_model.py` 会自行启动一套隔离服务，不读取正式作品数据库。先执行连接预检，再进行长流程：

```powershell
$env:ZHIPU_API_KEY = "你的 Key"
.\.venv\Scripts\python.exe scripts\acceptance_real_model.py --routing single --preflight-only
.\.venv\Scripts\python.exe scripts\acceptance_real_model.py --routing single --chapters 10
```

双模型使用 `--routing dual`，并通过 `INKFORGE_ACCEPT_PRIMARY_*` 与 `INKFORGE_ACCEPT_SECONDARY_*` 提供两个槽位。长流程会在第一章后强制停止服务、重启、核对孤儿任务状态并从检查点继续。退出码 `0` 表示通过，`1` 表示流程或质量门禁失败，`2` 表示模型配置或连接条件不足。

正式发布应在同一次报告中覆盖两种模式，并执行发布门禁：

```powershell
.\.venv\Scripts\python.exe scripts\acceptance_real_model.py --routing both --chapters 10
.\.venv\Scripts\python.exe scripts\check_release_acceptance.py output\real-model-acceptance\<run-id>\report.json
```

Windows 便携版和安装包通过 `scripts/build_windows.ps1` 构建。依赖或数据库结构代码改变时，`run.ps1` 会先生成经过完整性校验的升级前副本；SQLite 结构迁移本身也会保留独立的迁移前副本。

自动部署启动器和可分发 ZIP 通过 `scripts/build_launcher.ps1` 构建。构建会先检查所有版本号和必需发布文件，再对编译后的 EXE 执行无副作用冒烟测试。`scripts/check_project.py` 也是 CI 的发布契约门禁。

完成测试或发布后，可运行 `scripts/cleanup_workspace.ps1` 清除构建中间文件、评测输出、缓存和旧版发布 ZIP。脚本默认保留当前版本的自动部署包、便携版、开发环境、作品数据库和全部备份；只有显式传入 `-RemoveCurrentReleases` 才会删除当前发布产物。

## 变更边界

- HTTP 路径或响应字段变化时，评估并更新 `API_SCHEMA_VERSION`。
- 只改变内部目录、实现或静态资源版本时，更新应用版本，不必提升 API schema。
- 调整记忆、锁定或失效算法时，必须覆盖旧章修改、章节调序和重复结算。
- 调整模型提示词时，必须确认未来章节内容不会进入当前章上下文。
- 调整正文提示词时必须提升或确认 `PROSE_PROMPT_VERSION`，并更新 `prompt_cases.json`；上下文快照会记录实际提示词版本。
- 调整候选稿流程时，正文只能在作者明确插入或接纳后改变。

## CI

GitHub Actions 在 Windows 和 Linux 上运行 Python 3.10、3.12 的测试与离线规则评测。真实模型验收不进入 CI，因为它需要作者凭据、较长时间并可能产生费用。
