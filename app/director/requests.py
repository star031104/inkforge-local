"""Request contracts of the automatic director (start / refine / continue)."""
from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field


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
    # Per-run audit budget; ``None`` follows the project's ``critic_budget`` setting.
    critic_budget: Literal["auto", "full", "light"] | None = None


class AutoRefineRequest(BaseModel):
    scope: str = "repairs"
    chapter_ids: list[str] = Field(default_factory=list, max_length=300)
    instruction: str = Field(default="", max_length=12_000)
    quality_threshold: int = Field(default=82, ge=50, le=100)
    max_revision_attempts: int = Field(default=3, ge=1, le=5)
    force: bool = False


class ContinuePlannedWritingRequest(BaseModel):
    quality_threshold: int = Field(default=82, ge=50, le=100)
    max_revision_attempts: int = Field(default=2, ge=0, le=5)
    continue_on_quality_debt: bool = True
