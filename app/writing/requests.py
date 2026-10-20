"""Parameter object shared by chapter actions (plan / audit / memory).

Used by the HTTP routes and by the automatic director, which calls the same
chapter workflows without going through HTTP."""
from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


class ChapterActionRequest(BaseModel):
    project: dict[str, Any]
    chapter_id: str
    instruction: str = ""
    draft: str = ""
    commit_id: str = ""
    # Optional per-run craft overrides (e.g. a director task's ``critic_budget``).
    config: dict[str, Any] = Field(default_factory=dict)
