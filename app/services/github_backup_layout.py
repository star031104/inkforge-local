"""Render one novel project into the GitHub backup folder layout.

Pure functions only: project dict in, ``{relative_path: bytes}`` out.  Paths
are relative to the book folder and use Chinese folder names that are valid on
both Windows and GitHub.  Secrets are stripped recursively before anything is
written.
"""
from __future__ import annotations

from copy import deepcopy
import json
import re
from typing import Any

from .publishing import count_words, export_docx, export_epub, export_txt


WINDOWS_RESERVED = {
    "CON", "PRN", "AUX", "NUL",
    *(f"COM{index}" for index in range(1, 10)),
    *(f"LPT{index}" for index in range(1, 10)),
}
_ILLEGAL_PATH = re.compile(r'[\\/:*?"<>|\x00-\x1f\x7f​-‏  ﻿]+')
_SECRET_VALUE = re.compile(
    r"^(?:sk-[A-Za-z0-9_\-]{16,}|gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,}|xox[abpr]-[A-Za-z0-9-]{10,})$"
)
_SECRET_KEY_WORDS = ("token", "password", "secret", "authorization", "cookie")

IMPORTANCE_FOLDERS = {"main": "主要", "major": "主要", "protagonist": "主要"}
THREAD_STATUS = {
    "open": "未回收", "progressing": "推进中", "deferred": "延后",
    "ready": "可回收", "closed": "已回收",
}
AUTHORITY_LABELS = {
    "candidate": "候选稿", "reviewed": "已审阅", "accepted": "已接受", "locked": "已锁定",
}


# --------------------------------------------------------------- sanitizing


def safe_name(value: Any, fallback: str = "未命名", limit: int = 60) -> str:
    """A single path segment safe for Windows Explorer and GitHub."""
    text = _ILLEGAL_PATH.sub("_", str(value or "")).strip()
    text = re.sub(r"\s+", " ", text)
    text = re.sub(r"_+", "_", text)
    text = text[:limit].strip(" ._")
    if not text:
        text = fallback
    if text.split(".")[0].upper() in WINDOWS_RESERVED:
        text = f"_{text}"
    if text.startswith("."):
        text = "_" + text[1:]
    return text


def unique_name(name: str, used: set[str]) -> str:
    candidate = name
    index = 2
    while candidate.casefold() in used:
        candidate = f"{name}-{index}"
        index += 1
    used.add(candidate.casefold())
    return candidate


def _is_secret_key(key: str) -> bool:
    lowered = key.casefold()
    if lowered == "api_key" or lowered.endswith("_key") or lowered.endswith("apikey"):
        return True
    return any(word in lowered for word in _SECRET_KEY_WORDS)


def strip_secrets(value: Any) -> Any:
    """Recursively blank credential-like string fields and secret-looking values.

    Numeric fields such as ``max_tokens`` keep their values: only *string*
    values under secret-like keys are cleared, so restore keeps every setting
    except the credentials themselves.
    """
    if isinstance(value, dict):
        result = {}
        for key, item in value.items():
            if isinstance(item, str) and _is_secret_key(str(key)):
                result[key] = ""
            else:
                result[key] = strip_secrets(item)
        return result
    if isinstance(value, list):
        return [strip_secrets(item) for item in value]
    if isinstance(value, str) and _SECRET_VALUE.match(value.strip()):
        return ""
    return value


# --------------------------------------------------------------- markdown


def _yaml_scalar(value: Any) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    return json.dumps(str(value), ensure_ascii=False)


def frontmatter(fields: dict[str, Any]) -> str:
    lines = ["---"]
    for key, value in fields.items():
        if isinstance(value, (list, tuple)):
            if not value:
                lines.append(f"{key}: []")
            else:
                lines.append(f"{key}:")
                lines.extend(f"  - {_yaml_scalar(item)}" for item in value)
        else:
            lines.append(f"{key}: {_yaml_scalar(value)}")
    lines.append("---")
    return "\n".join(lines) + "\n\n"


def _text(value: Any) -> str:
    return str(value or "").replace("\r\n", "\n").strip()


def _section(title: str, body: Any, level: int = 2) -> str:
    text = _text(body)
    return f"{'#' * level} {title}\n\n{text}\n\n" if text else ""


def _bullets(title: str, items: Any, level: int = 2) -> str:
    values = [
        _text(item if not isinstance(item, dict) else _dict_line(item))
        for item in (items if isinstance(items, list) else [])
    ]
    values = [item for item in values if item]
    if not values:
        return ""
    return f"{'#' * level} {title}\n\n" + "\n".join(f"- {item}" for item in values) + "\n\n"


def _dict_line(item: dict[str, Any]) -> str:
    for key in ("text", "title", "message", "issue", "description", "summary", "name"):
        if _text(item.get(key)):
            prefix = _text(item.get("category") or item.get("severity") or "")
            return f"[{prefix}] {_text(item.get(key))}" if prefix else _text(item.get(key))
    return json.dumps(item, ensure_ascii=False)


def _fields(pairs: list[tuple[str, Any]]) -> str:
    rows = [f"- **{label}**：{_text(value)}" for label, value in pairs if _text(value)]
    return "\n".join(rows) + "\n\n" if rows else ""


def _doc(title: str, *parts: str, meta: dict[str, Any] | None = None) -> bytes:
    head = frontmatter(meta) if meta else ""
    body = "".join(part for part in parts if part).strip()
    return (head + f"# {title}\n\n" + (body + "\n" if body else "（暂无内容）\n")).encode("utf-8")


# --------------------------------------------------------------- helpers


def chapter_words(project: dict[str, Any]) -> int:
    return sum(count_words(str(chapter.get("content", ""))) for chapter in project.get("chapters", []))


def written_chapter_count(project: dict[str, Any]) -> int:
    return sum(1 for chapter in project.get("chapters", []) if _text(chapter.get("content")))


def _volumes(project: dict[str, Any]) -> list[dict[str, Any]]:
    planning = project.get("planning") if isinstance(project.get("planning"), dict) else {}
    return [item for item in planning.get("volumes", []) if isinstance(item, dict)]


def _volume_folder(volume: dict[str, Any], index: int) -> str:
    number = int(volume.get("number") or index + 1)
    return safe_name(f"卷{number:02d}-{_text(volume.get('title')) or f'第{number}卷'}")


def chapter_volume_map(project: dict[str, Any]) -> dict[int, str]:
    mapping: dict[int, str] = {}
    for index, volume in enumerate(_volumes(project)):
        try:
            start = int(volume.get("chapter_start") or 0)
            end = int(volume.get("chapter_end") or 0)
        except (TypeError, ValueError):
            continue
        folder = _volume_folder(volume, index)
        for number in range(max(1, start), end + 1):
            mapping.setdefault(number, folder)
    return mapping


# --------------------------------------------------------------- sections


def _settings_files(project: dict[str, Any]) -> dict[str, bytes]:
    narrative = project.get("narrative") if isinstance(project.get("narrative"), dict) else {}
    style = project.get("style") if isinstance(project.get("style"), dict) else {}
    files = {
        "00-设定/核心构想.md": _doc(
            "核心构想",
            _fields([("类型", project.get("genre")), ("篇幅模式", project.get("story_mode")),
                     ("目标章数", narrative.get("target_chapters"))]),
            _section("故事前提", project.get("premise")),
            _section("核心问题", narrative.get("central_question")),
            _section("结局方向", narrative.get("ending_direction")),
            _section("当前弧线", narrative.get("current_arc")),
            _fields([("视角", narrative.get("pov")), ("时态", narrative.get("tense")),
                     ("基调", narrative.get("tone"))]),
        ),
        "00-设定/全书大纲.md": _doc("全书大纲", _text(project.get("outline")) + "\n"),
        "00-设定/作者意图与硬规则.md": _doc(
            "作者意图与硬规则",
            _section("作者意图", project.get("author_intent")),
            _section("硬规则", project.get("book_rules")),
            _section("当前关注", project.get("current_focus")),
            _section("生产规范", project.get("production_spec")),
        ),
        "00-设定/文风卡.md": _doc(
            f"文风卡：{_text(style.get('name')) or '默认文风'}",
            _section("文风画像", style.get("profile")),
            _bullets("要做", style.get("dos")),
            _bullets("避免", style.get("donts")),
            _section("样例", style.get("sample")),
        ),
    }
    return files


def _world_files(project: dict[str, Any]) -> dict[str, bytes]:
    files: dict[str, bytes] = {}
    used: dict[str, set[str]] = {}
    for entry in project.get("world_entries", []):
        category = safe_name(entry.get("category") or "世界设定", "世界设定")
        name = unique_name(safe_name(entry.get("title"), "未命名条目"), used.setdefault(category, set()))
        meta = {
            "id": entry.get("id", ""),
            "title": entry.get("title", ""),
            "category": entry.get("category", ""),
            "keys": [str(item) for item in entry.get("keys", []) if str(item).strip()],
            "secondary_keys": [str(item) for item in entry.get("secondary_keys", []) if str(item).strip()],
            "constant": bool(entry.get("constant")),
            "enabled": bool(entry.get("enabled", True)),
            "canon": entry.get("canon", ""),
            "chapter_start": entry.get("chapter_start", 0),
            "chapter_end": entry.get("chapter_end", 0),
        }
        files[f"01-世界书/{category}/{name}.md"] = _doc(
            _text(entry.get("title")) or "未命名条目",
            _text(entry.get("content")) + "\n\n",
            _bullets("关联人物", entry.get("character_names")),
            meta=meta,
        )
    return files


def _character_files(project: dict[str, Any]) -> dict[str, bytes]:
    files: dict[str, bytes] = {}
    used: dict[str, set[str]] = {}
    for character in project.get("characters", []):
        if not character.get("active", True):
            group = "未启用"
        else:
            group = IMPORTANCE_FOLDERS.get(str(character.get("importance", "")), "配角")
        name = unique_name(safe_name(character.get("name"), "未命名人物"), used.setdefault(group, set()))
        aliases = character.get("aliases") if isinstance(character.get("aliases"), list) else []
        meta = {
            "id": character.get("id", ""),
            "name": character.get("name", ""),
            "aliases": [str(item) for item in aliases if str(item).strip()],
            "importance": character.get("importance", ""),
            "active": bool(character.get("active", True)),
        }
        files[f"02-人物卡/{group}/{name}.md"] = _doc(
            _text(character.get("name")) or "未命名人物",
            _fields([("身份", character.get("role")), ("当前位置", character.get("location")),
                     ("当前状态", character.get("state")), ("情绪", character.get("emotion")),
                     ("持有物", character.get("items"))]),
            _section("性格", character.get("personality") or character.get("description")),
            _section("外貌", character.get("appearance")),
            _section("目标", character.get("goal")),
            _section("价值观", character.get("values")),
            _section("恐惧", character.get("fears")),
            _section("矛盾", character.get("contradictions")),
            _section("说话方式", character.get("voice")),
            _section("习惯动作", character.get("mannerisms")),
            _section("人物关系", character.get("relationships")),
            _section("人物弧线", character.get("arc")),
            _section("不可违背", character.get("hard_limits")),
            _section("已知信息", character.get("knowledge_baseline") or character.get("knowledge")),
            _section("秘密", character.get("secrets")),
            _bullets("对白示例", character.get("dialogue_examples")),
            meta=meta,
        )
    return files


def _route_lines(volume: dict[str, Any]) -> str:
    routes = [item for item in volume.get("chapters", []) if isinstance(item, dict)]
    if not routes:
        return ""
    lines = ["## 章节路线\n"]
    for route in routes:
        lines.append(f"### 第{route.get('number', '?')}章 {_text(route.get('title'))}\n")
        lines.append(_fields([
            ("目标", route.get("goal")), ("冲突", route.get("conflict")),
            ("转折", route.get("turning_point")), ("章末钩子", route.get("ending_hook")),
            ("故事时间", route.get("story_date")), ("状态", route.get("status")),
        ]))
        lines.append(_bullets("必须保留", route.get("must_keep"), 4))
        lines.append(_bullets("必须避免", route.get("must_avoid"), 4))
    return "\n".join(part for part in lines if part)


def _planning_files(project: dict[str, Any]) -> dict[str, bytes]:
    planning = project.get("planning") if isinstance(project.get("planning"), dict) else {}
    master = planning.get("master") if isinstance(planning.get("master"), dict) else {}
    files = {
        "03-规划/全书规划.md": _doc(
            "全书规划",
            _section("主题", master.get("theme")),
            _section("读者承诺", master.get("reader_promise")),
            _section("核心冲突", master.get("central_conflict")),
            _section("故事引擎", master.get("story_engine")),
            _section("终局状态", master.get("ending_state")),
            _section("详细大纲", master.get("full_outline")),
            _section("主线", master.get("main_plot")),
            _section("主题递进", master.get("theme_progression")),
            _section("节奏安排", master.get("pacing_plan")),
            _bullets("风险阶梯", master.get("stakes_ladder")),
            _bullets("主要人物弧线", master.get("major_character_arcs")),
            _bullets("支线", master.get("subplots")),
            _bullets("历史与世界节点", master.get("historical_nodes")),
        )
    }
    for index, volume in enumerate(_volumes(project)):
        folder = _volume_folder(volume, index)
        files[f"03-规划/{folder}.md"] = _doc(
            _text(volume.get("title")) or f"第{index + 1}卷",
            _fields([("章节范围", f"第{volume.get('chapter_start')}–{volume.get('chapter_end')}章"),
                     ("时间跨度", volume.get("time_span")), ("主要舞台", volume.get("primary_arena"))]),
            _section("本卷目标", volume.get("goal")),
            _section("主导矛盾", volume.get("conflict")),
            _section("剧情梗概", volume.get("synopsis")),
            _bullets("转折点", volume.get("turning_points")),
            _bullets("人物变化", volume.get("character_arcs")),
            _bullets("支线", volume.get("subplots")),
            _bullets("必须兑现", volume.get("must_keep")),
            _bullets("不得违背", volume.get("must_avoid")),
            _section("卷末状态", volume.get("ending_state")),
            _section("通往下一卷", volume.get("bridge_to_next")),
            _route_lines(volume),
            meta={"id": volume.get("id", ""), "number": volume.get("number", index + 1),
                  "chapter_start": volume.get("chapter_start", 0),
                  "chapter_end": volume.get("chapter_end", 0)},
        )
    return files


def _chapter_files(project: dict[str, Any]) -> dict[str, bytes]:
    files: dict[str, bytes] = {}
    volume_map = chapter_volume_map(project)
    for number, chapter in enumerate(project.get("chapters", []), start=1):
        folder = volume_map.get(number, "未分卷")
        title = _text(chapter.get("title")) or f"第{number}章"
        clean_title = re.sub(r"^第\s*[0-9零〇一二两三四五六七八九十百千]+\s*章[\s:：、.-]*", "", title)
        name = safe_name(f"第{number:03d}章-{clean_title or '未命名'}", f"第{number:03d}章")
        execution = chapter.get("execution") if isinstance(chapter.get("execution"), dict) else {}
        meta = {
            "id": chapter.get("id", ""),
            "number": number,
            "title": title,
            "volume": folder,
            "words": count_words(str(chapter.get("content", ""))),
            "status": AUTHORITY_LABELS.get(str(chapter.get("authority_state", "")), chapter.get("authority_state", "")),
            "audit_score": execution.get("audit_score"),
        }
        content = _text(chapter.get("content"))
        files[f"04-正文/{folder}/{name}.md"] = _doc(
            f"第{number}章 {clean_title}".strip(),
            (content + "\n\n") if content else "（尚未写作）\n\n",
            _section("本章摘要", chapter.get("summary"), 2) if _text(chapter.get("summary")) else "",
            meta=meta,
        )
    return files


def _timeline_line(item: dict[str, Any]) -> str:
    when = _text(item.get("time"))
    if not when:
        number = item.get("chapter_number")
        when = f"第{number}章" if number else "未定时间"
    location = _text(item.get("location"))
    suffix = f"（{location}）" if location else ""
    return f"- **{when}**：{_text(item.get('event'))}{suffix}"


def _memory_files(project: dict[str, Any]) -> dict[str, bytes]:
    memory = project.get("memory") if isinstance(project.get("memory"), dict) else {}
    facts = [item for item in memory.get("facts", []) if isinstance(item, dict) and item.get("active", True)]
    threads = [item for item in memory.get("plot_threads", []) if isinstance(item, dict)]
    thread_parts = []
    for status, label in THREAD_STATUS.items():
        group = [item for item in threads if item.get("status") == status]
        if not group:
            continue
        thread_parts.append(f"## {label}（{len(group)}）\n\n")
        for item in group:
            thread_parts.append(f"### {_text(item.get('title')) or '未命名线索'}\n\n")
            thread_parts.append(_fields([
                ("类型", item.get("type")), ("埋设", item.get("setup")), ("最新进展", item.get("latest")),
                ("预期回收", item.get("expected_payoff")), ("回收条件", item.get("payoff_condition")),
                ("计划窗口", item.get("target_window")),
            ]))
    timeline = [item for item in memory.get("timeline", []) if isinstance(item, dict)]
    relationships = [item for item in memory.get("relationships", []) if isinstance(item, dict)]
    return {
        "05-记忆与线索/故事记忆.md": _doc(
            "故事记忆",
            _section("前情提要", memory.get("story_so_far")),
            _bullets("已确认事实", [item.get("text", "") for item in facts]),
            _bullets("连续性备注", [
                item.get("text", "") for item in memory.get("continuity_notes", [])
                if isinstance(item, dict) and not item.get("resolved")
            ]),
        ),
        "05-记忆与线索/伏笔与线索.md": _doc("伏笔与线索", "".join(thread_parts)),
        "05-记忆与线索/时间线.md": _doc(
            "时间线",
            "\n".join(_timeline_line(item) for item in timeline if _text(item.get("event"))) + "\n",
        ),
        "05-记忆与线索/人物关系.md": _doc(
            "人物关系",
            "".join(
                f"## {_text(item.get('left'))} ↔ {_text(item.get('right'))}\n\n"
                + _fields([("关系状态", item.get("state")), ("张力", item.get("tension")),
                           ("信任", item.get("trust")), ("信息差", item.get("knowledge_gap"))])
                for item in relationships if item.get("active", True)
            ),
        ),
    }


def _quality_file(project: dict[str, Any]) -> dict[str, bytes]:
    parts = []
    rows = []
    for number, chapter in enumerate(project.get("chapters", []), start=1):
        execution = chapter.get("execution") if isinstance(chapter.get("execution"), dict) else {}
        score = execution.get("audit_score")
        verdict = _text(execution.get("audit_verdict"))
        issues = execution.get("issues") if isinstance(execution.get("issues"), list) else []
        warnings = execution.get("warnings") if isinstance(execution.get("warnings"), list) else []
        if score is None and not verdict and not issues and not warnings:
            continue
        title = _text(chapter.get("title"))
        rows.append(f"| {number} | {title.replace('|', '/')} | {score if score is not None else '—'} | {verdict or '—'} | {len(issues)} |")
        detail = _bullets("问题", issues, 4) + _bullets("警告", warnings, 4)
        if detail:
            parts.append(f"### 第{number}章 {title}\n\n{detail}")
    table = (
        "## 各章最新审校\n\n| 章 | 标题 | 分数 | 结论 | 问题数 |\n|---|---|---|---|---|\n" + "\n".join(rows) + "\n\n"
        if rows else ""
    )
    reviews = []
    raw_reviews = project.get("volume_reviews")
    review_items = list(raw_reviews.values()) if isinstance(raw_reviews, dict) else (raw_reviews if isinstance(raw_reviews, list) else [])
    for volume in _volumes(project):
        if isinstance(volume.get("review"), dict):
            review_items.append({"volume": volume.get("title", ""), **volume["review"]})
    for item in review_items:
        if not isinstance(item, dict):
            continue
        name = _text(item.get("volume") or item.get("volume_title") or item.get("title")) or "分卷"
        reviews.append(
            f"### {name}\n\n"
            + _fields([("分数", item.get("score")), ("结论", item.get("verdict")), ("总结", item.get("summary"))])
            + _bullets("问题", item.get("issues"), 4)
        )
    review_section = ("## 分卷审阅\n\n" + "".join(reviews)) if reviews else ""
    return {
        "06-审校与精修/质量报告.md": _doc(
            "质量报告", table, ("## 问题明细\n\n" + "".join(parts)) if parts else "", review_section
        )
    }


def _export_files(project: dict[str, Any], include_binary: bool) -> dict[str, bytes]:
    if not written_chapter_count(project):
        return {}
    title = safe_name(project.get("title"), "未命名作品")
    files: dict[str, bytes] = {}
    files[f"07-成品/{title}.txt"] = export_txt(project, {"include_intro": True})["content"].encode("utf-8")
    if include_binary:
        for extension, exporter in (("epub", export_epub), ("docx", export_docx)):
            try:
                files[f"07-成品/{title}.{extension}"] = exporter(project, {})["content"]
            except ValueError:
                continue
    return files


def book_readme(project: dict[str, Any], last_sync: str) -> bytes:
    narrative = project.get("narrative") if isinstance(project.get("narrative"), dict) else {}
    chapters = project.get("chapters", [])
    written = written_chapter_count(project)
    locked = sum(1 for chapter in chapters if chapter.get("authority_state") == "locked")
    return _doc(
        _text(project.get("title")) or "未命名作品",
        _fields([
            ("类型", project.get("genre")),
            ("进度", f"已写 {written} 章 / 共 {len(chapters)} 章（目标 {narrative.get('target_chapters', '—')} 章），已锁定 {locked} 章"),
            ("总字数", f"{chapter_words(project):,}"),
            ("最近同步", last_sync),
        ]),
        _section("故事前提", project.get("premise")),
        "## 目录说明\n\n"
        "- `00-设定`：核心构想、全书大纲、作者意图与硬规则、文风卡\n"
        "- `01-世界书`：按分类整理的世界设定条目\n"
        "- `02-人物卡`：主要 / 配角 / 未启用人物\n"
        "- `03-规划`：全书规划与分卷章节路线\n"
        "- `04-正文`：按卷整理的章节正文\n"
        "- `05-记忆与线索`：故事记忆、伏笔、时间线、人物关系\n"
        "- `06-审校与精修`：最新质量报告\n"
        "- `07-成品`：可直接上传的成品文件\n"
        "- `.inkforge/project.json`：用于在砚火中完整恢复的原始数据（已移除密钥）\n",
        meta={"project_id": project.get("id", ""), "title": project.get("title", ""),
              "words": chapter_words(project), "chapters": len(chapters), "last_sync": last_sync},
    )


def render_book(
    project: dict[str, Any], *, include_binary_exports: bool = False
) -> tuple[dict[str, bytes], dict[str, bytes]]:
    """Return ``(files, volatile)`` relative to the book folder.

    ``volatile`` files (README with the sync time, binary exports whose bytes
    embed timestamps) are compared by fingerprint rather than raw bytes; the
    caller decides whether they need to be uploaded.
    """
    clean = strip_secrets(deepcopy(project))
    files: dict[str, bytes] = {}
    files.update(_settings_files(clean))
    files.update(_world_files(clean))
    files.update(_character_files(clean))
    files.update(_planning_files(clean))
    files.update(_chapter_files(clean))
    files.update(_memory_files(clean))
    files.update(_quality_file(clean))
    exports = _export_files(clean, include_binary_exports)
    for path, data in exports.items():
        if path.endswith(".txt"):
            files[path] = data
    volatile = {path: data for path, data in exports.items() if not path.endswith(".txt")}
    files[".inkforge/project.json"] = (
        json.dumps(clean, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    ).encode("utf-8")
    return files, volatile
