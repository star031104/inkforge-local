"""Run an isolated, paid-model release acceptance against the real HTTP app.

The runner never opens the author's normal database. It launches a disposable
server, creates a new work, kills the server after the first completed chapter,
restarts it, resumes the persisted task, and validates the resulting manuscript.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import time
from typing import Any
from uuid import uuid4

import httpx


ROOT = Path(__file__).resolve().parents[1]
OUTPUT_ROOT = ROOT / "output" / "real-model-acceptance"
TERMINAL_STATUSES = {"completed", "failed", "paused"}
SECRET_NAMES = {
    "api_key",
    "reasoning_api_key",
    "prose_api_key",
    "brave_api_key",
    "authorization",
}


class AcceptancePrerequisiteError(RuntimeError):
    """The requested real provider is not configured or reachable."""


def redact(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            key: "[redacted]" if key.lower() in SECRET_NAMES and item else redact(item)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [redact(item) for item in value]
    return value


def available_port() -> int:
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        return int(listener.getsockname()[1])


def default_provider() -> str:
    explicit = os.environ.get("INKFORGE_ACCEPT_PRIMARY_PROVIDER", "").strip()
    if explicit:
        return explicit
    if os.environ.get("ZHIPU_API_KEY") or os.environ.get("INKFORGE_ZHIPU_API_KEY"):
        return "zhipu"
    if os.environ.get("MODELSCOPE_API_KEY") or os.environ.get(
        "INKFORGE_MODELSCOPE_API_KEY"
    ):
        return "modelscope"
    return "llama_cpp"


def provider_defaults(provider: str, *, secondary: bool = False) -> tuple[str, str]:
    if provider == "zhipu":
        return "https://open.bigmodel.cn/api/paas/v4", "glm-4.7-flash"
    if provider == "modelscope":
        return "https://api-inference.modelscope.cn/v1", "ZhipuAI/GLM-5.2"
    if provider == "llama_cpp":
        return "http://127.0.0.1:8080/v1", "local-model"
    prefix = "SECONDARY" if secondary else "PRIMARY"
    return (
        os.environ.get(f"INKFORGE_ACCEPT_{prefix}_BASE_URL", "http://127.0.0.1:8080/v1"),
        os.environ.get(f"INKFORGE_ACCEPT_{prefix}_MODEL", "local-model"),
    )


def acceptance_settings(routing: str) -> dict[str, Any]:
    primary_provider = default_provider()
    secondary_provider = os.environ.get(
        "INKFORGE_ACCEPT_SECONDARY_PROVIDER", "modelscope"
    ).strip()
    primary_base, primary_model = provider_defaults(primary_provider)
    secondary_base, secondary_model = provider_defaults(
        secondary_provider, secondary=True
    )
    primary_base = os.environ.get(
        "INKFORGE_ACCEPT_PRIMARY_BASE_URL", primary_base
    ).strip()
    primary_model = os.environ.get(
        "INKFORGE_ACCEPT_PRIMARY_MODEL", primary_model
    ).strip()
    secondary_base = os.environ.get(
        "INKFORGE_ACCEPT_SECONDARY_BASE_URL", secondary_base
    ).strip()
    secondary_model = os.environ.get(
        "INKFORGE_ACCEPT_SECONDARY_MODEL", secondary_model
    ).strip()
    return {
        "model_routing": routing,
        "provider": primary_provider,
        "base_url": primary_base,
        "model": primary_model,
        "api_key": os.environ.get("INKFORGE_ACCEPT_PRIMARY_KEY", "").strip(),
        "reasoning_provider": secondary_provider,
        "reasoning_base_url": secondary_base,
        "reasoning_model": secondary_model,
        "reasoning_api_key": os.environ.get(
            "INKFORGE_ACCEPT_SECONDARY_KEY", ""
        ).strip(),
        "temperature": 0.72,
        "top_p": 0.9,
        "max_tokens": 3500,
        "context_budget": 24000,
        "target_words": 600,
        "creative_freedom": "balanced",
        "enable_thinking": False,
    }


class ManagedServer:
    def __init__(self, run_directory: Path, port: int):
        self.run_directory = run_directory
        self.port = port
        self.process: subprocess.Popen[bytes] | None = None
        self.log_stream = None
        self.environment = os.environ.copy()
        self.environment.update(
            {
                "PYTHONUTF8": "1",
                "INKFORGE_DB_PATH": str(run_directory / "acceptance.db"),
                "INKFORGE_SECRET_PATH": str(run_directory / "acceptance-secrets.db"),
                "INKFORGE_BACKUP_PATH": str(run_directory / "backups"),
            }
        )

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def start(self) -> None:
        self.log_stream = (self.run_directory / "server.log").open("ab")
        self.process = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "uvicorn",
                "app.main:app",
                "--host",
                "127.0.0.1",
                "--port",
                str(self.port),
            ],
            cwd=ROOT,
            env=self.environment,
            stdout=self.log_stream,
            stderr=subprocess.STDOUT,
        )
        deadline = time.monotonic() + 35
        while time.monotonic() < deadline:
            if self.process.poll() is not None:
                raise RuntimeError("验收服务启动失败，请查看 server.log")
            try:
                response = httpx.get(f"{self.url}/api/health", timeout=2)
                if response.status_code == 200:
                    return
            except httpx.HTTPError:
                pass
            time.sleep(0.35)
        raise RuntimeError("验收服务启动超时")

    def kill(self) -> None:
        if self.process and self.process.poll() is None:
            self.process.kill()
            self.process.wait(timeout=15)
        if self.log_stream:
            self.log_stream.close()
        self.process = None
        self.log_stream = None

    def close(self) -> None:
        if self.process and self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=15)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait(timeout=10)
        if self.log_stream:
            self.log_stream.close()
        self.process = None
        self.log_stream = None


def api(client: httpx.Client, method: str, path: str, **kwargs: Any) -> Any:
    response = client.request(method, path, **kwargs)
    if response.is_error:
        try:
            detail = response.json().get("detail", response.text)
        except (ValueError, AttributeError):
            detail = response.text
        raise RuntimeError(f"{method} {path} -> {response.status_code}: {detail}")
    return response.json()


def preflight(client: httpx.Client, settings: dict[str, Any], routing: str) -> dict[str, Any]:
    try:
        models = api(client, "POST", "/api/models", json=settings)
    except (httpx.HTTPError, RuntimeError) as exc:
        raise AcceptancePrerequisiteError(f"真实模型预检失败：{exc}") from exc
    summary = api(client, "POST", "/api/provider/summary", json=settings)
    if routing == "dual" and summary.get("effective_routing") != "dual":
        raise AcceptancePrerequisiteError(
            "双模型验收要求两个模型槽都可用；当前实际仍是单模型"
        )
    return {"models": models, "summary": summary}


def create_source_project(
    client: httpx.Client, settings: dict[str, Any], routing: str
) -> dict[str, Any]:
    created = api(
        client,
        "POST",
        "/api/projects",
        json={"title": f"真实模型验收源-{routing}"},
    )
    project = api(client, "GET", f"/api/projects/{created['id']}")
    project["settings"].update(settings)
    project["genre"] = "近未来悬疑"
    project["style"]["profile"] = "克制、具体、以动作和证据推进，不使用空泛总结"
    return api(client, "PUT", f"/api/projects/{project['id']}", json=project)


def manuscript_checks(project: dict[str, Any], expected_chapters: int) -> dict[str, Any]:
    chapters = [item for item in project.get("chapters", []) if isinstance(item, dict)]
    completed = [item for item in chapters if len(str(item.get("content", "")).strip()) >= 100]
    hashes = [
        hashlib.sha256(str(item.get("content", "")).strip().encode("utf-8")).hexdigest()
        for item in completed
    ]
    locked = [item for item in completed if item.get("authority_state") == "locked"]
    summarized = [item for item in completed if str(item.get("summary", "")).strip()]
    commits = project.get("memory", {}).get("commits", [])
    checks = {
        "chapter_count": len(chapters),
        "completed_chapters": len(completed),
        "locked_chapters": len(locked),
        "summarized_chapters": len(summarized),
        "unique_completed_chapters": len(set(hashes)),
        "memory_commits": len(commits) if isinstance(commits, list) else 0,
        "all_expected_chapters_written": len(completed) >= expected_chapters,
        "no_duplicate_full_chapters": len(set(hashes)) == len(hashes),
        "all_completed_chapters_locked": len(locked) == len(completed),
        "memory_settled_for_expected_chapters": (
            isinstance(commits, list) and len(commits) >= expected_chapters
        ),
        "all_expected_chapters_summarized": len(summarized) >= expected_chapters,
    }
    checks["passed"] = all(
        checks[key]
        for key in (
            "all_expected_chapters_written",
            "no_duplicate_full_chapters",
            "all_completed_chapters_locked",
            "memory_settled_for_expected_chapters",
            "all_expected_chapters_summarized",
        )
    )
    return checks


def run_case(
    server: ManagedServer,
    routing: str,
    chapters: int,
    target_words: int,
    timeout_minutes: int,
    restart: bool,
) -> dict[str, Any]:
    settings = acceptance_settings(routing)
    settings["target_words"] = target_words
    with httpx.Client(base_url=server.url, timeout=90) as client:
        preflight_result = preflight(client, settings, routing)
        source = create_source_project(client, settings, routing)
        started = api(
            client,
            "POST",
            "/api/director/start",
            json={
                "source_project": source,
                "seed": (
                    "一座沿海城市开始出现只在旧照片中存在的人。负责整理公共档案的"
                    "女主发现，每确认一次失踪者身份，现实就会抹去她自己的一段记忆。"
                ),
                "preferences": "因果清晰、证据可追溯、人物选择必须付出代价。",
                "story_mode": "short" if chapters <= 12 else "long",
                "target_chapters": chapters,
                "target_words": target_words,
                "quality_threshold": 70,
                "max_revision_attempts": 1,
                "continue_on_quality_debt": True,
            },
        )
        task_id = str(started["task"]["id"])
        project_id = str(started["project"]["id"])

    deadline = time.monotonic() + timeout_minutes * 60
    restarted = False
    last_progress = None
    final_task: dict[str, Any] = {}
    while time.monotonic() < deadline:
        with httpx.Client(base_url=server.url, timeout=30) as client:
            task = api(client, "GET", f"/api/director/tasks/{task_id}")
        progress = (
            task.get("phase"),
            int(task.get("completed_chapters", 0) or 0),
            task.get("message"),
        )
        if progress != last_progress:
            print(
                f"[{routing}] {task.get('status')} · {progress[0]} · "
                f"{progress[1]}/{chapters} · {progress[2]}",
                flush=True,
            )
            last_progress = progress
        if (
            restart
            and not restarted
            and int(task.get("completed_chapters", 0) or 0) >= 1
            and task.get("status") in {"queued", "running"}
        ):
            server.kill()
            time.sleep(0.5)
            server.start()
            with httpx.Client(base_url=server.url, timeout=30) as client:
                orphan = api(
                    client, "GET", f"/api/director/projects/{project_id}/latest"
                )
                if orphan.get("status") != "paused":
                    raise RuntimeError(
                        f"服务重启后任务应转为 paused，实际为 {orphan.get('status')}"
                    )
                api(client, "POST", f"/api/director/tasks/{task_id}/resume")
            restarted = True
            time.sleep(1)
            continue
        if task.get("status") in TERMINAL_STATUSES:
            final_task = task
            break
        time.sleep(3)
    else:
        raise TimeoutError(f"{routing} 验收超过 {timeout_minutes} 分钟")

    with httpx.Client(base_url=server.url, timeout=90) as client:
        project = api(client, "GET", f"/api/projects/{project_id}")
        health = api(
            client,
            "POST",
            "/api/project/manuscript-health",
            json={"project": project},
        )
    checks = manuscript_checks(project, chapters)
    checks["restart_recovery_verified"] = restarted if restart else True
    checks["task_completed"] = final_task.get("status") == "completed"
    checks["passed"] = bool(
        checks["passed"]
        and checks["restart_recovery_verified"]
        and checks["task_completed"]
    )
    return {
        "routing": routing,
        "project_id": project_id,
        "task_id": task_id,
        "provider": redact(preflight_result["summary"]),
        "checks": checks,
        "task": {
            "status": final_task.get("status"),
            "phase": final_task.get("phase"),
            "quality_debts": len(final_task.get("quality_debts", [])),
            "planning_debts": len(final_task.get("planning_debts", [])),
            "message": final_task.get("message", ""),
            "error": final_task.get("error", ""),
        },
        "manuscript_health": health,
        "passed": checks["passed"],
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run isolated real-model single/dual routing and restart recovery acceptance."
    )
    parser.add_argument("--routing", choices=("single", "dual", "both"), default="single")
    parser.add_argument("--chapters", type=int, default=10)
    parser.add_argument("--target-words", type=int, default=600)
    parser.add_argument("--timeout-minutes", type=int, default=180)
    parser.add_argument("--no-restart", action="store_true")
    parser.add_argument("--preflight-only", action="store_true")
    parser.add_argument("--output", type=Path)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if not 3 <= args.chapters <= 30:
        raise SystemExit("--chapters 必须在 3 到 30 之间")
    if not 300 <= args.target_words <= 5000:
        raise SystemExit("--target-words 必须在 300 到 5000 之间")
    run_id = time.strftime("%Y%m%d-%H%M%S") + "-" + uuid4().hex[:6]
    run_directory = OUTPUT_ROOT / run_id
    run_directory.mkdir(parents=True, exist_ok=False)
    report_path = (args.output or run_directory / "report.json").resolve()
    server = ManagedServer(run_directory, available_port())
    routing_modes = ["single", "dual"] if args.routing == "both" else [args.routing]
    report: dict[str, Any] = {
        "schema_version": 1,
        "run_id": run_id,
        "started_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "database": str(run_directory / "acceptance.db"),
        "routing_requested": routing_modes,
        "cases": [],
        "passed": False,
    }
    exit_code = 1
    try:
        server.start()
        if args.preflight_only:
            with httpx.Client(base_url=server.url, timeout=90) as client:
                report["preflight"] = {
                    mode: redact(
                        preflight(client, acceptance_settings(mode), mode)
                    )
                    for mode in routing_modes
                }
            report["passed"] = True
            exit_code = 0
        else:
            for index, mode in enumerate(routing_modes):
                case = run_case(
                    server,
                    mode,
                    args.chapters,
                    args.target_words,
                    args.timeout_minutes,
                    restart=not args.no_restart and index == 0,
                )
                report["cases"].append(case)
            report["passed"] = all(case["passed"] for case in report["cases"])
            exit_code = 0 if report["passed"] else 1
    except AcceptancePrerequisiteError as exc:
        report["prerequisite_error"] = str(exc)
        exit_code = 2
    except Exception as exc:
        report["error"] = f"{type(exc).__name__}: {exc}"
        exit_code = 1
    finally:
        server.close()
        report["finished_at"] = time.strftime("%Y-%m-%dT%H:%M:%S%z")
        report_path.parent.mkdir(parents=True, exist_ok=True)
        report_path.write_text(
            json.dumps(redact(report), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        print(f"验收报告：{report_path}")
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
