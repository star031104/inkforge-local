from __future__ import annotations

from copy import deepcopy
from typing import Any


class ProjectConflictError(RuntimeError):
    """Raised when a client attempts to save from an outdated project version."""


def save_project_versioned(
    store: Any, project_id: str, payload: dict[str, Any]
) -> dict[str, Any]:
    current = store.get(project_id)
    if not current:
        raise KeyError(project_id)
    clean = deepcopy(payload)
    reason = str(clean.pop("_save_reason", "autosave"))
    expected_updated_at = str(clean.pop("_expected_updated_at", "") or "")
    if not expected_updated_at or expected_updated_at != str(current.get("updated_at", "")):
        raise ProjectConflictError(
            "作品已在另一个窗口或后台任务中更新。当前编辑尚未覆盖服务器版本；"
            "请导出本地副本或重新载入后合并。"
        )
    return store.save(project_id, clean, reason=reason, expected_updated_at=expected_updated_at)
