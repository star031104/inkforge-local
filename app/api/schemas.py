"""Public HTTP request contracts.

Keeping these models free of service imports prevents routing code from becoming
the owner of domain behavior and gives tests a stable import surface.
"""
from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


class CreateProject(BaseModel):
    title: str = Field(default="未命名故事", max_length=120)


class DatabaseRestoreRequest(BaseModel):
    confirmation: str = Field(min_length=1, max_length=40)


class BackupSettingsRequest(BaseModel):
    path: str = Field(default="", max_length=1000)


class GenerateRequest(BaseModel):
    scene_id: str = ""
    project: dict[str, Any]
    chapter_id: str
    mode: str = "continue"
    instruction: str = ""
    selection: str = ""
    target_words: int | None = Field(default=None, ge=100, le=10000)
    skill_ids: list[str] = Field(default_factory=list, max_length=20)


class StyleRequest(BaseModel):
    settings: dict[str, Any]
    sample: str = Field(min_length=100, max_length=100_000)


class ChapterActionRequest(BaseModel):
    project: dict[str, Any]
    chapter_id: str
    instruction: str = ""
    draft: str = ""
    commit_id: str = ""


class ApplyMemoryRequest(BaseModel):
    project: dict[str, Any]
    chapter_id: str
    result: dict[str, Any]
    commit_id: str = ""


class AcceptChapterRequest(BaseModel):
    project: dict[str, Any]
    chapter_id: str
    lock: bool = True
    force: bool = False
    audit: dict[str, Any] = Field(default_factory=dict)
    contract_scan: dict[str, Any] = Field(default_factory=dict)


class WorkflowStageRequest(BaseModel):
    project: dict[str, Any]
    chapter_id: str
    stage: str


class ContractScanRequest(BaseModel):
    project: dict[str, Any]
    chapter_id: str
    draft: str = ""


class RepairStatusRequest(BaseModel):
    project: dict[str, Any]
    task_id: str
    status: str


class SessionRequest(BaseModel):
    project: dict[str, Any]
    chapter_id: str
    checkpoint_id: str = ""


class MemoryCommitStatusRequest(BaseModel):
    project: dict[str, Any]
    chapter_id: str
    commit_id: str
    error: str = ""


class IdeasRequest(BaseModel):
    project: dict[str, Any]
    kind: str = "next"
    instruction: str = ""


class IncubatorRequest(BaseModel):
    project: dict[str, Any]
    seed: str
    preferences: str = ""
    story_mode: str = "long"
    target_chapters: int = 30


class AutoDirectorStartRequest(BaseModel):
    source_project: dict[str, Any]
    seed: str = Field(min_length=8, max_length=20_000)
    preferences: str = Field(default="", max_length=12_000)
    story_mode: str = "long"
    target_chapters: int = Field(default=30, ge=3, le=300)
    preferred_volume_count: int | None = Field(default=None, ge=1, le=24)
    target_words: int = Field(default=1200, ge=300, le=5000)
    quality_threshold: int = Field(default=78, ge=50, le=100)
    max_revision_attempts: int = Field(default=2, ge=0, le=3)
    continue_on_quality_debt: bool = True


class AutoRefineRequest(BaseModel):
    scope: str = "repairs"
    chapter_ids: list[str] = Field(default_factory=list, max_length=300)
    instruction: str = Field(default="", max_length=12_000)
    quality_threshold: int = Field(default=82, ge=50, le=100)
    max_revision_attempts: int = Field(default=3, ge=1, le=5)


class ContinuePlannedWritingRequest(BaseModel):
    quality_threshold: int = Field(default=82, ge=50, le=100)
    max_revision_attempts: int = Field(default=2, ge=0, le=5)
    continue_on_quality_debt: bool = True


class PlanningRequest(BaseModel):
    project: dict[str, Any]
    instruction: str = ""
    volume_id: str = ""


class ApplyVolumeRequest(BaseModel):
    project: dict[str, Any]
    volume_id: str


class ManuscriptHealthRequest(BaseModel):
    project: dict[str, Any]


class ReferenceParseRequest(BaseModel):
    name: str = Field(min_length=1, max_length=255)
    content_base64: str = Field(min_length=8, max_length=30_000_000)


class ProjectRequest(BaseModel):
    project: dict[str, Any]


class ProjectSearchRequest(BaseModel):
    project_id: str
    query: str = Field(min_length=1, max_length=20_000)
    chapter_id: str = ""
    current_chapter_number: int | None = Field(default=None, ge=1, le=100_000)
    limit: int = Field(default=24, ge=1, le=80)


class SnapshotReplayRequest(BaseModel):
    settings: dict[str, Any]


class SkillMutationRequest(BaseModel):
    item: dict[str, Any]


class CanonCharacterRequest(BaseModel):
    project: dict[str, Any]
    character_id: str
    reference_ids: list[str] = Field(default_factory=list)


class CanonAuditRequest(BaseModel):
    project: dict[str, Any]
    chapter_id: str = ""
    draft: str = Field(min_length=20, max_length=80_000)


class KnowledgeMutationRequest(BaseModel):
    project: dict[str, Any]
    item: dict[str, Any]
