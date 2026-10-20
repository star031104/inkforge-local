"""Start InkForge in a throw-away data directory, install the demo, verify it.

Usage: python scripts/smoke_demo.py [--root DIR]

``--root`` is the directory that contains ``app/`` and ``static/`` (defaults to
this repository); the build scripts point it at the deployment payload so the
check also proves ``app/demo`` was packaged.  Only the standard library is
used for HTTP; the app itself runs with the current interpreter.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import time
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parents[1]


def available_port() -> int:
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        return int(listener.getsockname()[1])


class Client:
    def __init__(self, base: str):
        self.base = base

    def call(self, method: str, path: str, body: Any = None) -> tuple[int, Any]:
        data = None if body is None else json.dumps(body).encode("utf-8")
        request = Request(self.base + path, data=data, method=method,
                          headers={"Content-Type": "application/json"})
        try:
            with urlopen(request, timeout=30) as response:
                raw = response.read().decode("utf-8")
                status = response.status
        except HTTPError as error:
            raw = error.read().decode("utf-8", errors="replace")
            status = error.code
        try:
            return status, json.loads(raw)
        except ValueError:
            return status, raw


def check(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)
    print(f"  ok  {message}")


def wait_for_health(client: Client, process: subprocess.Popen, timeout: float = 45.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if process.poll() is not None:
            output = process.stdout.read().decode("utf-8", errors="replace") if process.stdout else ""
            raise RuntimeError(f"InkForge exited early:\n{output}")
        try:
            status, payload = client.call("GET", "/api/health")
            if status == 200 and isinstance(payload, dict) and payload.get("status") == "ok":
                return
        except (OSError, URLError):
            pass
        time.sleep(0.3)
    raise RuntimeError("InkForge health check timed out")


def verify(client: Client) -> None:
    status, demo = client.call("GET", "/api/demo")
    check(status == 200 and demo.get("available") is True, "演示作品文件已随程序打包")
    check(demo.get("installed") is False, "全新数据目录中尚未安装演示作品")

    status, installed = client.call("POST", "/api/demo/install", {})
    check(status == 200 and installed.get("project_id"), "POST /api/demo/install 安装成功")
    project_id = installed["project_id"]

    status, _ = client.call("POST", "/api/demo/install", {})
    check(status == 409, "重复安装被拒绝（409）")

    status, project = client.call("GET", f"/api/projects/{project_id}")
    check(status == 200 and project.get("title") == demo.get("title"), "演示作品可以载入")
    check(project.get("demo", {}).get("id") == "dukoudeng", "演示标记保留")

    chapters = project.get("chapters", [])
    check(len(chapters) == 3, "章节列表含 3 章")
    for chapter in chapters:
        length = len("".join(str(chapter.get("content", "")).split()))
        check(chapter.get("title") and chapter.get("summary") and 1000 <= length <= 2200,
              f"《{chapter.get('title')}》正文 {length} 字，含摘要")
        check(isinstance(chapter.get("story_year"), int) and chapter.get("story_date"), "章节带故事内时间")
        check(chapter.get("route_id") and chapter.get("volume_id"), "章节已绑定路线")
    check(isinstance(chapters[0].get("execution", {}).get("audit_score"), int), "第一章带审计评分")

    volumes = project.get("planning", {}).get("volumes", [])
    check(len(volumes) == 2, "全书规划含 2 卷")
    routes = volumes[0].get("chapters", [])
    check([route.get("number") for route in routes] == list(range(1, 9)), "第一卷 8 条章节路线")
    check(sum(1 for route in routes if route.get("number", 0) > len(chapters)) == 5, "第 4–8 章为未写路线")
    check(any(route.get("debate", {}).get("opponent_strongest_arguments") for route in routes), "含交锋场景路线")

    characters = project.get("characters", [])
    check(len(characters) == 4, "4 张人物卡")
    for card in characters:
        check(all(card.get(key) for key in ("knowledge", "secrets", "hard_limits", "voice", "dialogue_examples")),
              f"人物卡「{card.get('name')}」字段完整")
    world = project.get("world_entries", [])
    check(len(world) == 6 and sum(bool(item.get("constant")) for item in world) == 2, "6 条世界书（2 条常驻）")
    check(any(item.get("status") == "approved" for item in project.get("author_preferences", [])), "含已批准作者偏好")

    status, listing = client.call("GET", "/api/projects")
    row = next((item for item in listing if item.get("id") == project_id), None) if status == 200 else None
    check(bool(row) and row.get("chapter_count") == 3, "作品列表显示演示作品与章节数")

    status, demo_after = client.call("GET", "/api/demo")
    check(demo_after.get("installed") is True and demo_after.get("project_id") == project_id, "安装状态可查询")

    status, forced = client.call("POST", "/api/demo/install", {"force": True})
    check(status == 200 and forced.get("project_id") != project_id, "force 可再装一份副本")
    old_ids = {chapter["id"] for chapter in chapters}
    new_ids = {chapter["id"] for chapter in forced["project"]["chapters"]}
    check(not (old_ids & new_ids), "副本的章节 ID 全部重新生成")

    status, html = client.call("GET", "/")
    check(status == 200 and "features/onboarding.js" in str(html), "首页加载上手脚本")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, default=ROOT)
    args = parser.parse_args()
    root = args.root.resolve()
    if not (root / "app" / "demo" / "dukoudeng.json").is_file():
        print(f"FAIL: {root} 中缺少 app/demo/dukoudeng.json")
        return 1
    port = available_port()
    with tempfile.TemporaryDirectory(prefix="inkforge-demo-smoke-") as directory:
        data = Path(directory)
        environment = {
            **os.environ,
            "PYTHONUTF8": "1",
            "INKFORGE_DB_PATH": str(data / "inkforge.db"),
            "INKFORGE_SECRET_PATH": str(data / "inkforge-secrets.db"),
            "INKFORGE_BACKUP_PATH": str(data / "backups"),
            "INKFORGE_LOG_PATH": str(data / "logs" / "inkforge.log"),
        }
        process = subprocess.Popen(
            [sys.executable, "-m", "app", "--host", "127.0.0.1", "--port", str(port), "--no-browser"],
            cwd=str(root), env=environment, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        )
        try:
            client = Client(f"http://127.0.0.1:{port}")
            wait_for_health(client, process)
            verify(client)
        except Exception as error:  # noqa: BLE001 - report every failure the same way
            print(f"FAIL: {error}")
            return 1
        finally:
            process.terminate()
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
    print("Demo smoke test passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
