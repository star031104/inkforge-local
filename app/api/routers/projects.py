"""Project lifecycle, revisions, search, and project writing skills."""
from __future__ import annotations

from datetime import datetime, timezone
from collections.abc import Callable
from pathlib import Path
import re
from typing import Any

from fastapi import APIRouter, HTTPException

from ...db import ProjectStore
from ...infrastructure.backup_location import BackupLocation
from ...project_service import ProjectConflictError, save_project_versioned
from ...writing_skills import available_writing_skills, normalize_writing_skill
from ..schemas import (
    CreateProject,
    BackupSettingsRequest,
    DatabaseRestoreRequest,
    ProjectSearchRequest,
    SkillMutationRequest,
)


def create_projects_router(
    store_provider: Callable[[], ProjectStore],
    backup_directory: Path | BackupLocation,
    active_tasks_provider: Callable[[], dict[str, Any]] | None = None,
) -> APIRouter:
    router = APIRouter(tags=["projects"])

    def store() -> ProjectStore:
        return store_provider()

    def backup_root() -> Path:
        return (
            backup_directory.path
            if isinstance(backup_directory, BackupLocation)
            else backup_directory
        )

    def backup_path(filename: str) -> Path:
        if not re.fullmatch(r"inkforge-[A-Za-z0-9._-]+\.db", filename):
            raise HTTPException(400, "备份文件名无效")
        root = backup_root().resolve()
        candidate = (root / filename).resolve()
        if candidate.parent != root:
            raise HTTPException(400, "备份路径无效")
        if not candidate.is_file():
            raise HTTPException(404, "备份不存在")
        return candidate

    @router.get("/api/projects")
    async def project_list() -> list[dict[str, Any]]:
        return store().list()

    @router.post("/api/projects")
    async def project_create(body: CreateProject) -> dict[str, Any]:
        return store().create(body.title)

    @router.post("/api/projects/import")
    async def project_import(body: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(body, dict) or not isinstance(body.get("chapters"), list):
            raise HTTPException(422, "导入文件不是有效的砚火项目 JSON")
        return store().import_project(body)

    @router.post("/api/backup")
    async def database_backup() -> dict[str, Any]:
        path = store().backup(backup_root())
        return {"ok": True, "filename": path.name}

    @router.get("/api/backup-settings")
    async def database_backup_settings() -> dict[str, Any]:
        if isinstance(backup_directory, BackupLocation):
            return backup_directory.describe()
        return {
            "path": str(backup_root().resolve()),
            "default_path": str(backup_root().resolve()),
            "writable": True,
            "same_volume_as_database": True,
            "off_device_recommended": True,
            "backup_count": 0,
            "latest_backup_at": "",
        }

    @router.put("/api/backup-settings")
    async def database_backup_settings_update(
        body: BackupSettingsRequest,
    ) -> dict[str, Any]:
        if not isinstance(backup_directory, BackupLocation):
            raise HTTPException(409, "当前运行方式不支持修改备份目录")
        try:
            return backup_directory.set_path(body.path)
        except (OSError, ValueError) as exc:
            raise HTTPException(422, f"无法使用该备份目录：{exc}") from exc

    @router.get("/api/backups")
    async def database_backups() -> dict[str, Any]:
        directory = backup_root()
        directory.mkdir(parents=True, exist_ok=True)
        items = []
        for path in sorted(
            directory.glob("inkforge-*.db"),
            key=lambda item: item.stat().st_mtime,
            reverse=True,
        ):
            stat = path.stat()
            items.append(
                {
                    "filename": path.name,
                    "size_bytes": stat.st_size,
                    "modified_at": datetime.fromtimestamp(
                        stat.st_mtime, timezone.utc
                    ).isoformat(),
                    "safety_backup": path.name.startswith("inkforge-before-restore-"),
                    "automatic": path.name.startswith("inkforge-auto-"),
                }
            )
        return {"items": items, "keep": 12, "safety_keep": 5}

    @router.get("/api/backups/{filename}")
    async def database_backup_preview(filename: str) -> dict[str, Any]:
        return store().inspect_backup(backup_path(filename))

    @router.post("/api/backups/{filename}/restore")
    async def database_backup_restore(
        filename: str, body: DatabaseRestoreRequest
    ) -> dict[str, Any]:
        if body.confirmation.strip() != "恢复数据库":
            raise HTTPException(422, "请输入“恢复数据库”确认操作")
        active_tasks = active_tasks_provider() if active_tasks_provider else {}
        if any(not task.done() for task in active_tasks.values()):
            raise HTTPException(409, "AI 自动任务仍在运行，请先暂停后再恢复数据库")
        try:
            result = store().restore_database_backup(
                backup_path(filename), backup_root()
            )
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc
        result["projects"] = store().list()
        return result

    @router.post("/api/search")
    async def project_search(body: ProjectSearchRequest) -> dict[str, Any]:
        repository = store()
        project = repository.get(body.project_id)
        if not project:
            raise HTTPException(404, "项目不存在")
        if body.current_chapter_number is not None:
            chapter_number = body.current_chapter_number
        elif body.chapter_id:
            chapter_number = next(
                (index + 1 for index, chapter in enumerate(project.get("chapters", []))
                 if str(chapter.get("id", "")) == body.chapter_id),
                len(project.get("chapters", [])) or 1,
            )
        else:
            chapter_number = len(project.get("chapters", [])) or 1
        return {
            "engine": "fts5" if repository.fts_enabled else "lexical_fallback",
            "chapter_number": chapter_number,
            "hits": repository.search_project(body.project_id, body.query, chapter_number, body.limit),
        }

    @router.get("/api/writing-skills")
    async def writing_skills(project_id: str = "") -> dict[str, Any]:
        repository = store()
        project: dict[str, Any] = {"writing_skills": []}
        if project_id:
            project = repository.get(project_id) or {}
            if not project:
                raise HTTPException(404, "项目不存在")
        return {
            "skills": available_writing_skills(project, repository.user_writing_skills()),
            "permissions": {"allowed": ["prompt_instructions"],
                            "denied": ["commands", "tools", "filesystem", "network"]},
        }

    @router.post("/api/writing-skills/user")
    async def writing_skill_user_upsert(body: SkillMutationRequest) -> dict[str, Any]:
        try:
            return {"item": store().upsert_user_writing_skill(body.item)}
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc

    @router.delete("/api/writing-skills/user/{skill_id}")
    async def writing_skill_user_delete(skill_id: str) -> dict[str, Any]:
        if skill_id.startswith("builtin-"):
            raise HTTPException(409, "内置写作 Skill 为只读")
        if not store().delete_user_writing_skill(skill_id):
            raise HTTPException(404, "个人写作 Skill 不存在")
        return {"ok": True}

    @router.post("/api/projects/{project_id}/writing-skills")
    async def writing_skill_project_upsert(
        project_id: str, body: SkillMutationRequest
    ) -> dict[str, Any]:
        repository = store()
        project = repository.get(project_id)
        if not project:
            raise HTTPException(404, "项目不存在")
        task = repository.latest_director_task(project_id)
        if task and task.get("task_type") != "incubation" and task.get("status") in {"queued", "running"}:
            raise HTTPException(409, "自动导演运行期间不能修改项目写作 Skill")
        try:
            item = normalize_writing_skill(body.item, scope="project", readonly=False)
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
        skills = project.setdefault("writing_skills", [])
        for index, existing in enumerate(skills):
            if isinstance(existing, dict) and existing.get("id") == item["id"]:
                skills[index] = item
                break
        else:
            skills.append(item)
        return {"item": item, "project": repository.save(project_id, project, reason="writing-skill-upsert")}

    @router.delete("/api/projects/{project_id}/writing-skills/{skill_id}")
    async def writing_skill_project_delete(project_id: str, skill_id: str) -> dict[str, Any]:
        if skill_id.startswith("builtin-"):
            raise HTTPException(409, "内置写作 Skill 为只读")
        repository = store()
        project = repository.get(project_id)
        if not project:
            raise HTTPException(404, "项目不存在")
        before = len(project.get("writing_skills", []))
        project["writing_skills"] = [item for item in project.get("writing_skills", [])
                                     if not isinstance(item, dict) or str(item.get("id", "")) != skill_id]
        if len(project["writing_skills"]) == before:
            raise HTTPException(404, "项目写作 Skill 不存在")
        return {"ok": True, "project": repository.save(project_id, project, reason="writing-skill-delete")}

    @router.get("/api/projects/{project_id}")
    async def project_get(project_id: str) -> dict[str, Any]:
        project = store().get(project_id)
        if not project:
            raise HTTPException(404, "项目不存在")
        return project

    @router.get("/api/projects/{project_id}/context-snapshots")
    async def project_context_snapshots(project_id: str, limit: int = 30) -> list[dict[str, Any]]:
        repository = store()
        if not repository.get(project_id):
            raise HTTPException(404, "项目不存在")
        return repository.context_snapshots(project_id, limit)

    @router.put("/api/projects/{project_id}")
    async def project_save(project_id: str, body: dict[str, Any]) -> dict[str, Any]:
        repository = store()
        director_task = repository.latest_director_task(project_id)
        if director_task and director_task.get("task_type") != "incubation" and director_task.get("status") in {"queued", "running"}:
            raise HTTPException(409, "自动导演正在写入该作品，请先暂停任务再手动编辑或保存")
        try:
            return save_project_versioned(repository, project_id, body)
        except ProjectConflictError as exc:
            raise HTTPException(409, str(exc)) from exc
        except KeyError:
            raise HTTPException(404, "项目不存在") from None

    @router.delete("/api/projects/{project_id}")
    async def project_delete(project_id: str) -> dict[str, Any]:
        repository = store()
        if not repository.get(project_id):
            raise HTTPException(404, "项目不存在")
        backup = repository.backup(backup_root())
        repository.delete(project_id)
        return {"ok": True, "backup": backup.name}

    @router.get("/api/projects/{project_id}/revisions")
    async def project_revisions(project_id: str) -> list[dict[str, Any]]:
        repository = store()
        if not repository.get(project_id):
            raise HTTPException(404, "项目不存在")
        return repository.revisions(project_id)

    @router.post("/api/projects/{project_id}/revisions/{revision_id}/restore")
    async def project_restore(project_id: str, revision_id: int) -> dict[str, Any]:
        try:
            return store().restore(project_id, revision_id)
        except KeyError:
            raise HTTPException(404, "历史版本不存在") from None

    @router.get("/api/projects/{project_id}/chapters/{chapter_id}/versions")
    async def chapter_versions(project_id: str, chapter_id: str) -> list[dict[str, Any]]:
        repository = store()
        project = repository.get(project_id)
        if not project:
            raise HTTPException(404, "项目不存在")
        if not any(str(item.get("id", "")) == chapter_id for item in project["chapters"]):
            raise HTTPException(404, "章节不存在")
        return repository.chapter_versions(project_id, chapter_id)

    @router.post("/api/projects/{project_id}/chapters/{chapter_id}/versions/{version_id}/restore")
    async def chapter_version_restore(project_id: str, chapter_id: str, version_id: int) -> dict[str, Any]:
        try:
            return store().restore_chapter_version(project_id, chapter_id, version_id)
        except KeyError:
            raise HTTPException(404, "章节历史版本不存在") from None

    return router
