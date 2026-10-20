"""Import an existing manuscript and reverse-engineer its story bible.

Workflow (all state is resumable and lives in the extension store under
``import_job``):

1. ``split_manuscript`` turns a parsed TXT/MD/DOCX/EPUB file into chapters.
2. ``create_imported_project`` writes the chapters as locked, author-owned
   prose (the author wrote them; nothing here is AI text).
3. ``run_import_analysis`` reads the manuscript in batches with the planning
   model and proposes characters, world entries and foreshadowing candidates,
   then synthesises premise/outline/narrative fields.  Every proposal is a
   *candidate*; nothing reaches the authoritative project until the author
   confirms it with ``confirm_import_review``.
4. ``run_import_settlement`` settles chapter memory one chapter at a time
   through the regular evidence-bound memory path, checkpointing after each
   chapter so the job can pause and resume.
"""

from __future__ import annotations

import asyncio
import re
import uuid
from collections.abc import Awaitable, Callable
from copy import deepcopy
from datetime import datetime, timezone
from typing import Any

from app.editorial_workflow import lock_chapter
from app.evidence_audit import verify_quote
from app.memory_integrity import begin_memory_commit, chapter_content_hash, mark_memory_commit
from app.project_service import ProjectConflictError

JOB_KEY = "import_job"
DEFAULT_TARGET_CHARS = 3000
BATCH_CHARS = 12000
CHAPTER_EXCERPT_CHARS = 6000
DEFAULT_MAX_BATCHES = 16

_CN_NUM = "0-9０-９零〇一二两三四五六七八九十百千万壹贰叁肆伍陆柒捌玖拾佰仟"
# A heading keyword must be followed by a separator or the end of the line, so
# prose such as "第三节课他睡着了" / "第五回合开始" / "第一章节" is not a heading.
_HEAD_END = r"(?=[\s:：、.．\-—·(（【\[]|$)"
CHAPTER_HEADING_RE = re.compile(
    rf"^(?:#{{1,6}}\s*)?(?P<head>(?:第\s*[{_CN_NUM}]+\s*[章回节话]{_HEAD_END}"
    rf"|(?:chapter|CHAPTER|Chapter)\s+[0-9IVXLCivxlc]+{_HEAD_END}"
    rf"|(?:序章|序言|序幕|楔子|引子|尾声|终章|后记){_HEAD_END}"
    rf"|番外(?:篇|之)?[{_CN_NUM}]*{_HEAD_END}))"
    r"(?:[\s:：、.．\-—·]*(?P<title>[^\n]{0,40}))?$"
)
# "第一章雾起": 章/回 glued to a short title.  Accepted only for short lines
# that do not end like a sentence, and never for 章节 / 回合 / 回到 style prose.
COMPACT_CHAPTER_RE = re.compile(
    rf"^(?:#{{1,6}}\s*)?(?P<head>第\s*[{_CN_NUM}]+\s*[章回])(?![节合到来去家首头])(?P<title>[^\s，,。！？!?；;]{{1,20}})$"
)
VOLUME_HEADING_RE = re.compile(
    rf"^(?:#{{1,6}}\s*)?第\s*[{_CN_NUM}]+\s*[卷部集]{_HEAD_END}(?:[\s:：、]*[^\n]{{0,30}})?$"
)
SENTENCE_END = ("。", "！", "？", "!", "?", "…", "”", "」")

StructuredCompletion = Callable[..., Awaitable[tuple[dict[str, Any], list[str]]]]


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


# ---------------------------------------------------------------------------
# Splitting
# ---------------------------------------------------------------------------


def _clean_line(line: str) -> str:
    line = line.replace("　", " ").strip()
    line = re.sub(r"^>\s*", "", line)
    line = line.replace("**", "").replace("__", "")
    return line.strip()


def _paragraphs(lines: list[str]) -> str:
    return "\n\n".join(item for item in (_clean_line(line) for line in lines) if item)


def _is_heading(line: str) -> re.Match[str] | None:
    stripped = _clean_line(line)
    if not stripped or len(stripped) > 48 or stripped.endswith(SENTENCE_END[:5]):
        return None
    return CHAPTER_HEADING_RE.match(stripped) or (
        COMPACT_CHAPTER_RE.match(stripped) if len(stripped) <= 24 else None
    )


_SENTENCE_BREAK_RE = re.compile(r"(?<=[。！？!?…])(?![”」』’）)])|(?<=[。！？!?…][”」』’）)])")


def _hard_split(paragraph: str, limit: int) -> list[str]:
    """Split one over-long paragraph (e.g. a TXT without line breaks) into
    pieces of at most ``limit`` chars, preferring sentence boundaries."""
    if len(paragraph) <= limit:
        return [paragraph]
    pieces: list[str] = []
    current = ""
    for sentence in (part for part in _SENTENCE_BREAK_RE.split(paragraph) if part):
        while len(sentence) > limit:  # no punctuation at all: cut blindly
            if current:
                pieces.append(current)
                current = ""
            pieces.append(sentence[:limit])
            sentence = sentence[limit:]
        if current and len(current) + len(sentence) > limit:
            pieces.append(current)
            current = ""
        current += sentence
    if current:
        pieces.append(current)
    return pieces


def _split_by_length(text: str, target_chars: int) -> list[dict[str, Any]]:
    # (piece, continues_previous): pieces cut from one over-long paragraph are
    # re-joined without a paragraph break when they land in the same chapter.
    pieces = [
        (piece, position > 0)
        for item in (_clean_line(p) for p in text.split("\n"))
        if item
        for position, piece in enumerate(_hard_split(item, target_chars))
    ]
    chapters: list[dict[str, Any]] = []
    content = ""
    size = 0
    starts_mid_paragraph = False
    for piece, continues in pieces:
        if continues and size and size + len(piece) > target_chars:
            chapters.append({"title": "", "content": content})
            content, size = "", 0
        if not content:
            starts_mid_paragraph = continues
        content += (("" if continues else "\n\n") if content else "") + piece
        size += len(piece)
        if size >= target_chars:
            chapters.append({"title": "", "content": content})
            content, size = "", 0
    if content:
        if chapters and size < target_chars * 0.3:
            chapters[-1]["content"] += ("" if starts_mid_paragraph else "\n\n") + content
        else:
            chapters.append({"title": "", "content": content})
    for index, item in enumerate(chapters, start=1):
        item["title"] = f"第{index}章"
    return chapters


def split_manuscript(
    text: str,
    *,
    mode: str = "auto",
    target_chars: int = DEFAULT_TARGET_CHARS,
    min_chapter_chars: int = 30,
) -> dict[str, Any]:
    """Split raw manuscript text into chapters.

    ``mode`` is ``auto`` (headings, falling back to length), ``headings`` or
    ``length``.  Empty heading blocks (usually a table of contents) are
    dropped and reported as warnings.
    """
    text = str(text or "").replace("\r\n", "\n").replace("\r", "\n")
    target_chars = max(800, min(20000, int(target_chars or DEFAULT_TARGET_CHARS)))
    warnings: list[str] = []
    lines = text.split("\n")
    headings = [index for index, line in enumerate(lines) if _is_heading(line)]
    method = "length"
    chapters: list[dict[str, Any]] = []
    preamble = ""
    if mode != "length" and len(headings) >= 2 or (mode == "headings" and headings):
        method = "headings"
        volume = ""
        preamble = _paragraphs(
            [line for line in lines[: headings[0]] if not VOLUME_HEADING_RE.match(_clean_line(line))]
        )
        bounds = headings + [len(lines)]
        dropped = 0
        for position, start in enumerate(headings):
            for line in lines[(bounds[position - 1] if position else 0):start]:
                if VOLUME_HEADING_RE.match(_clean_line(line)):
                    volume = _clean_line(line)
            match = _is_heading(lines[start])
            head = re.sub(r"\s+", "", match.group("head")) if match else ""
            title_tail = (match.group("title") or "").strip() if match else ""
            title = f"{head} {title_tail}".strip() if title_tail else head
            body_lines = [
                line
                for line in lines[start + 1 : bounds[position + 1]]
                if not VOLUME_HEADING_RE.match(_clean_line(line))
            ]
            content = _paragraphs(body_lines)
            if len(content) < min_chapter_chars:
                dropped += 1
                continue
            chapters.append({"title": title[:60] or f"第{len(chapters) + 1}章", "content": content, "volume": volume})
        if dropped:
            warnings.append(f"已忽略 {dropped} 个没有正文的章节标题（通常是目录）")
        if len(preamble) >= 200:
            chapters.insert(0, {"title": "序", "content": preamble, "volume": ""})
            warnings.append("首个章节标题之前有较长正文，已单独保存为“序”")
        elif preamble:
            warnings.append(f"首个章节标题之前的 {len(preamble)} 字（可能是书名或简介）未导入正文")
    if method == "length" or not chapters:
        if mode == "headings":
            warnings.append("没有识别到章节标题，已按篇幅切分")
        method = "length"
        chapters = _split_by_length(text, target_chars)
    oversized = [item["title"] for item in chapters if len(item["content"]) > 20000]
    if oversized:
        warnings.append(f"{len(oversized)} 章超过 2 万字，记忆反推时只会读取头尾节选：{'、'.join(oversized[:5])}")
    return {
        "method": method,
        "chapters": chapters,
        "total_chars": sum(len(item["content"]) for item in chapters),
        "warnings": warnings,
        "preamble": preamble[:500],
    }


def preview_split(parsed: dict[str, Any], **options: Any) -> dict[str, Any]:
    split = split_manuscript(parsed.get("text", ""), **options)
    name = str(parsed.get("name", "")).rsplit("/", 1)[-1]
    title_guess = re.sub(r"\.[A-Za-z0-9]+$", "", name).strip() or "导入的旧稿"
    first_line = next((line.strip() for line in split.get("preamble", "").split("\n") if line.strip()), "")
    if 2 <= len(first_line) <= 30 and not re.search(r"[。！？：:]|作者|目录|简介", first_line):
        title_guess = first_line
    return {
        "title_guess": title_guess[:80],
        "method": split["method"],
        "total_chars": split["total_chars"],
        "chapter_count": len(split["chapters"]),
        "warnings": split["warnings"],
        "chapters": [
            {
                "index": index + 1,
                "title": item["title"],
                "volume": item.get("volume", ""),
                "chars": len(item["content"]),
                "excerpt": item["content"][:80],
            }
            for index, item in enumerate(split["chapters"])
        ],
    }


# ---------------------------------------------------------------------------
# Project creation
# ---------------------------------------------------------------------------


def _new_chapter(title: str, content: str, volume: str = "") -> dict[str, Any]:
    chapter: dict[str, Any] = {
        "id": str(uuid.uuid4()),
        "title": title,
        "summary": "",
        "content": content,
        "scene_goal": "",
        "author_note": "",
        "imported_volume": volume,
        "source": "manuscript-import",
    }
    if len(content.strip()) >= 100:
        lock_chapter(chapter, actor="manuscript-import", force=True)
    else:
        chapter["authority_state"] = "accepted"
    return chapter


def new_job(source_name: str, chapter_count: int, total_chars: int) -> dict[str, Any]:
    return {
        "status": "imported",
        "phase": "analysis",
        "source_name": source_name,
        "chapter_count": chapter_count,
        "total_chars": total_chars,
        "batches": [],
        "review": None,
        "settle_index": 0,
        "settled_chapter_ids": [],
        "failed_chapters": [],
        "warnings": [],
        "events": [],
        "pause_requested": False,
        "error": "",
        "created_at": utc_now(),
        "updated_at": utc_now(),
    }


def create_imported_project(
    store: Any,
    split: dict[str, Any],
    *,
    title: str,
    genre: str = "",
    source_name: str = "",
    target_project_id: str = "",
    copy_settings_from: str = "",
) -> dict[str, Any]:
    chapters = [
        _new_chapter(item["title"], item["content"], item.get("volume", ""))
        for item in split.get("chapters", [])
    ]
    if not chapters:
        raise ValueError("没有可导入的章节")
    if target_project_id:
        project = store.get(target_project_id)
        if project is None:
            raise KeyError(target_project_id)
        if any(str(item.get("content", "")).strip() for item in project.get("chapters", [])):
            raise ValueError("只能导入到还没有正文的作品；已有正文时请新建作品导入")
    else:
        project = store.create(title or "导入的旧稿")
    if copy_settings_from and copy_settings_from != project.get("id"):
        source = store.get(copy_settings_from)
        if source and isinstance(source.get("settings"), dict):
            project["settings"] = deepcopy(source["settings"])
    project["title"] = title or project.get("title") or "导入的旧稿"
    if genre:
        project["genre"] = genre
    project["chapters"] = chapters
    project.setdefault("narrative", {})["target_chapters"] = max(
        int(project.get("narrative", {}).get("target_chapters", 30) or 30), len(chapters)
    )
    saved = store.save(project["id"], project, reason="manuscript-import")
    return saved


# ---------------------------------------------------------------------------
# Job bookkeeping
# ---------------------------------------------------------------------------


def job_event(job: dict[str, Any], message: str, kind: str = "info") -> None:
    events = job.setdefault("events", [])
    events.append({"at": utc_now(), "kind": kind, "message": message})
    job["events"] = events[-120:]
    job["updated_at"] = utc_now()


def _persist(extensions: Any, project_id: str, job: dict[str, Any]) -> None:
    """Save job progress without losing a pause request made meanwhile."""

    def merge(current: Any) -> dict[str, Any]:
        if isinstance(current, dict) and current.get("pause_requested"):
            job["pause_requested"] = True
        return job

    if hasattr(extensions, "update"):
        extensions.update(project_id, JOB_KEY, merge)
    else:  # minimal stores used by scripts
        merge(extensions.get(project_id, JOB_KEY) or {})
        extensions.put(project_id, JOB_KEY, job)


def request_pause(extensions: Any, project_id: str) -> dict[str, Any] | None:
    """Atomically flag the stored job for pausing; never rewrites progress."""

    def flag(current: Any) -> dict[str, Any] | None:
        if not isinstance(current, dict):
            return None
        if current.get("status") in {"analyzing", "settling"} and not current.get("pause_requested"):
            current["pause_requested"] = True
            job_event(current, "已请求暂停，当前这一步完成后停止", "warning")
        return current

    return extensions.update(project_id, JOB_KEY, flag)


def plan_batches(
    chapters: list[dict[str, Any]], *, max_batches: int = DEFAULT_MAX_BATCHES
) -> list[dict[str, Any]]:
    """Group consecutive chapters into ~BATCH_CHARS windows.

    Very long manuscripts are sampled evenly (always keeping the first and
    last window) so analysis cost stays bounded; skipped windows are still
    covered later by per-chapter memory settlement.
    """
    groups: list[list[int]] = []
    current: list[int] = []
    size = 0
    for index, chapter in enumerate(chapters):
        length = min(CHAPTER_EXCERPT_CHARS, len(str(chapter.get("content", ""))))
        if current and size + length > BATCH_CHARS:
            groups.append(current)
            current, size = [], 0
        current.append(index)
        size += length
    if current:
        groups.append(current)
    selected = set(range(len(groups)))
    max_batches = max(2, int(max_batches or DEFAULT_MAX_BATCHES))
    if len(groups) > max_batches:
        step = (len(groups) - 1) / (max_batches - 1)
        selected = {round(i * step) for i in range(max_batches)}
    return [
        {
            "index": number,
            "chapter_indexes": group,
            "status": "pending" if number in selected else "skipped",
            "result": None,
            "error": "",
        }
        for number, group in enumerate(groups)
    ]


def _chapter_excerpt(content: str, limit: int = CHAPTER_EXCERPT_CHARS) -> str:
    content = str(content or "")
    if len(content) <= limit:
        return content
    half = limit // 2
    return content[:half] + "\n……（中间省略）……\n" + content[-half:]


BATCH_PROMPT = """你是中文长篇小说的资深责任编辑，正在为作者已经写好的旧稿反推设定，方便作者继续创作。
只依据给出的正文归纳，绝不编造正文没有出现的人物、设定或伏笔；不确定的写“未知”或留空。
每个条目的 evidence 必须是正文中逐字连续出现的 8—40 字原句，用来让作者核对。
只返回严格 JSON，不要 Markdown：
{
  "summary": "本段剧情概要，不超过 200 字",
  "characters": [{"name":"正文中的称呼","aliases":["其他称呼"],"role":"身份/定位","importance":"main/supporting/minor","personality":"性格与价值观","appearance":"稳定外貌特征","goal":"当前目标","voice":"说话方式","relationships":"与其他人物的关系","evidence":"正文原句"}],
  "world": [{"title":"设定名","keys":["触发关键词"],"content":"设定说明","evidence":"正文原句"}],
  "foreshadow": [{"title":"悬念/伏笔名","setup":"正文如何埋下","expected_payoff":"可能的回收方向（推测要标明）","evidence":"正文原句"}]
}
characters 最多 12 项，world 最多 8 项，foreshadow 最多 6 项。"""

SYNTHESIS_PROMPT = """你是中文长篇小说的资深责任编辑。下面是作者旧稿的分段概要和人物名单。
请反推这部作品的整体设定，供作者确认后继续写作。只依据材料归纳，不确定处留空。
只返回严格 JSON，不要 Markdown：
{
  "genre": "题材类型",
  "premise": "一句话梗概与核心冲突",
  "outline": "已写部分的主线梳理（按阶段，300 字以内）",
  "author_intent": "推测作者想写成什么样的书（标注为推测）",
  "book_rules": "正文中已经确立、后文不应违背的硬规则（每行一条）",
  "story_so_far": "截至最后一章的全书进展摘要（400 字以内）",
  "narrative": {"pov":"first/third_limited/omniscient","tense":"past/present","tone":"总体气质","central_question":"故事核心问题","current_arc":"当前故事弧","ending_direction":"可能的收束方向（推测）"}
}"""


def _validate_batch(result: dict[str, Any]) -> None:
    if not isinstance(result, dict):
        raise ValueError("反推结果不是 JSON 对象")
    for key in ("characters", "world", "foreshadow"):
        if key in result and not isinstance(result[key], list):
            raise ValueError(f"{key} 必须是数组")


def _validate_synthesis(result: dict[str, Any]) -> None:
    if not isinstance(result, dict) or not str(result.get("premise", "")).strip():
        raise ValueError("缺少 premise")


def _name_key(value: Any) -> str:
    return re.sub(r"\s+", "", str(value or "")).casefold()


def _as_list(value: Any) -> list[str]:
    if isinstance(value, str):
        value = re.split(r"[,，、\n]", value)
    if not isinstance(value, list):
        return []
    return [str(item).strip() for item in value if str(item).strip()][:12]


IMPORTANCE_RANK = {"main": 3, "supporting": 2, "minor": 1}


def merge_batch_results(
    batches: list[dict[str, Any]], chapters: list[dict[str, Any]]
) -> dict[str, Any]:
    """Merge per-batch proposals into one candidate package (pure function)."""
    characters: list[dict[str, Any]] = []
    by_key: dict[str, dict[str, Any]] = {}
    world: dict[str, dict[str, Any]] = {}
    foreshadow: list[dict[str, Any]] = []
    summaries: list[dict[str, Any]] = []
    full_text = "\n".join(str(item.get("content", "")) for item in chapters)
    for batch in batches:
        result = batch.get("result")
        if batch.get("status") != "done" or not isinstance(result, dict):
            continue
        indexes = batch.get("chapter_indexes", [])
        first = (indexes[0] + 1) if indexes else 0
        last = (indexes[-1] + 1) if indexes else 0
        batch_text = "\n".join(
            str(chapters[i].get("content", "")) for i in indexes if 0 <= i < len(chapters)
        )
        summaries.append({"chapters": [first, last], "summary": str(result.get("summary", ""))[:600]})
        for raw in result.get("characters", []) or []:
            if not isinstance(raw, dict) or not str(raw.get("name", "")).strip():
                continue
            name = str(raw.get("name", "")).strip()[:40]
            aliases = [alias for alias in _as_list(raw.get("aliases")) if _name_key(alias) != _name_key(name)]
            # Merge only when a name matches: this name is a known name/alias,
            # or one of its aliases is someone's name.  Two people who merely
            # share a generic alias ("少爷", "先生") must stay separate.
            existing = by_key.get(_name_key(name)) or next(
                (
                    by_key[_name_key(alias)]
                    for alias in aliases
                    if _name_key(alias) in by_key
                    and _name_key(by_key[_name_key(alias)]["name"]) == _name_key(alias)
                ),
                None,
            )
            evidence = verify_quote(batch_text, raw.get("evidence"))
            if existing is None:
                existing = {
                    "id": f"cand-char-{uuid.uuid4().hex[:10]}",
                    "name": name,
                    "aliases": [],
                    "role": "",
                    "importance": "minor",
                    "personality": "",
                    "appearance": "",
                    "goal": "",
                    "voice": "",
                    "relationships": "",
                    "evidence": "",
                    "evidence_verified": False,
                    "first_chapter": first,
                    "batch_mentions": 0,
                }
                characters.append(existing)
            existing["batch_mentions"] += 1
            for alias in aliases + ([name] if _name_key(name) != _name_key(existing["name"]) else []):
                if alias not in existing["aliases"]:
                    existing["aliases"].append(alias)
            for field in ("role", "personality", "appearance", "voice"):
                if not existing[field] and str(raw.get(field, "")).strip():
                    existing[field] = str(raw.get(field, "")).strip()[:400]
            # Goals and relationships move with the story: the latest batch wins.
            for field in ("goal", "relationships"):
                if str(raw.get(field, "")).strip():
                    existing[field] = str(raw.get(field, "")).strip()[:400]
            importance = str(raw.get("importance", "")).strip().lower()
            if IMPORTANCE_RANK.get(importance, 0) > IMPORTANCE_RANK.get(existing["importance"], 0):
                existing["importance"] = importance
            if evidence["verified"] and not existing["evidence_verified"]:
                existing["evidence"] = evidence["quote"]
                existing["evidence_verified"] = True
            elif not existing["evidence"]:
                existing["evidence"] = str(raw.get("evidence", ""))[:240]
            by_key[_name_key(existing["name"])] = existing
            for alias_key in {_name_key(a) for a in existing["aliases"]}:
                # Never let an alias shadow another character's own name.
                holder = by_key.get(alias_key)
                if holder is None or _name_key(holder["name"]) != alias_key:
                    by_key[alias_key] = existing
        for raw in result.get("world", []) or []:
            if not isinstance(raw, dict) or not str(raw.get("title", "")).strip():
                continue
            key = _name_key(raw.get("title"))
            evidence = verify_quote(batch_text, raw.get("evidence"))
            entry = world.get(key)
            if entry is None:
                entry = world[key] = {
                    "id": f"cand-world-{uuid.uuid4().hex[:10]}",
                    "title": str(raw.get("title", "")).strip()[:60],
                    "keys": [],
                    "content": "",
                    "evidence": "",
                    "evidence_verified": False,
                    "first_chapter": first,
                }
            for item in _as_list(raw.get("keys")) or [entry["title"]]:
                if item not in entry["keys"]:
                    entry["keys"].append(item)
            content = str(raw.get("content", "")).strip()
            if content and content not in entry["content"]:
                entry["content"] = (entry["content"] + "\n" + content).strip()[:1200]
            if evidence["verified"] and not entry["evidence_verified"]:
                entry["evidence"], entry["evidence_verified"] = evidence["quote"], True
        for raw in result.get("foreshadow", []) or []:
            if not isinstance(raw, dict) or not str(raw.get("title", "")).strip():
                continue
            evidence = verify_quote(batch_text, raw.get("evidence"))
            if any(_name_key(item["title"]) == _name_key(raw.get("title")) for item in foreshadow):
                continue
            foreshadow.append({
                "id": f"cand-thread-{uuid.uuid4().hex[:10]}",
                "title": str(raw.get("title", "")).strip()[:80],
                "setup": str(raw.get("setup", "")).strip()[:500],
                "expected_payoff": str(raw.get("expected_payoff", "")).strip()[:500],
                "evidence": evidence["quote"],
                "evidence_verified": bool(evidence["verified"]),
                "chapter_number": first,
                "chapter_id": str(chapters[indexes[0]].get("id", "")) if indexes else "",
            })
    for item in characters:
        names = [item["name"], *item["aliases"]]
        item["occurrences"] = sum(full_text.count(name) for name in names if len(name) >= 2)
        item["verified_in_text"] = item["occurrences"] > 0
        item["suggested"] = item["verified_in_text"] and (
            item["importance"] != "minor" or item["occurrences"] >= 5
        )
    characters.sort(
        key=lambda item: (-IMPORTANCE_RANK.get(item["importance"], 0), -item["occurrences"])
    )
    for entry in world.values():
        entry["suggested"] = bool(entry["evidence_verified"])
    for thread in foreshadow:
        thread["suggested"] = bool(thread["evidence_verified"])
    return {
        "characters": characters[:60],
        "world_entries": list(world.values())[:60],
        "foreshadow": foreshadow[:40],
        "batch_summaries": summaries,
    }


async def run_import_analysis(
    *,
    store: Any,
    extensions: Any,
    project_id: str,
    completion: StructuredCompletion,
    max_batches: int = DEFAULT_MAX_BATCHES,
) -> dict[str, Any]:
    """Run (or resume) batch analysis + synthesis.  Returns the final job."""
    project = store.get(project_id)
    if project is None:
        raise KeyError(project_id)
    job = extensions.get(project_id, JOB_KEY) or new_job("", len(project["chapters"]), 0)
    chapters = project.get("chapters", [])
    if not job.get("batches"):
        job["batches"] = plan_batches(chapters, max_batches=max_batches)
        skipped = sum(1 for item in job["batches"] if item["status"] == "skipped")
        if skipped:
            job["warnings"].append(
                f"旧稿较长，设定反推抽样阅读 {len(job['batches']) - skipped}/{len(job['batches'])} 段；其余章节会在逐章记忆结算时补读"
            )
    job.update({"status": "analyzing", "phase": "analysis", "pause_requested": False, "error": ""})
    job_event(job, "开始反推人物、世界设定与伏笔")
    extensions.put(project_id, JOB_KEY, job)
    settings = project.get("settings", {})
    for batch in job["batches"]:
        if batch["status"] in {"done", "skipped"}:
            continue
        current = extensions.get(project_id, JOB_KEY) or job
        if current.get("pause_requested") or job.get("pause_requested"):
            job.update({"status": "paused", "pause_requested": False})
            job_event(job, "已暂停设定反推，可随时继续", "warning")
            extensions.put(project_id, JOB_KEY, job)
            return job
        indexes = batch["chapter_indexes"]
        text = "\n\n".join(
            f"【{chapters[i].get('title', '')}】\n{_chapter_excerpt(chapters[i].get('content', ''))}"
            for i in indexes
            if 0 <= i < len(chapters)
        )
        try:
            result, _warnings = await completion(
                settings,
                [
                    {"role": "system", "content": BATCH_PROMPT},
                    {"role": "user", "content": f"第 {indexes[0] + 1}—{indexes[-1] + 1} 章正文：\n{text}"},
                ],
                max_tokens=2600,
                timeout_seconds=240,
                temperature=0.2,
                validate=_validate_batch,
                workload="planning",
            )
            batch.update({"status": "done", "result": result, "error": ""})
            job_event(job, f"已读完第 {indexes[0] + 1}—{indexes[-1] + 1} 章", "success")
        except Exception as exc:  # model errors are recorded, not fatal
            batch.update({"status": "failed", "error": str(exc)[:500]})
            job_event(job, f"第 {indexes[0] + 1}—{indexes[-1] + 1} 章反推失败：{str(exc)[:200]}", "error")
        _persist(extensions, project_id, job)
    if not any(item["status"] == "done" for item in job["batches"]):
        job.update({"status": "failed", "error": "所有分段反推都失败了，请检查模型设置后继续"})
        job_event(job, job["error"], "error")
        extensions.put(project_id, JOB_KEY, job)
        return job
    review = merge_batch_results(job["batches"], chapters)
    synthesis: dict[str, Any] = {}
    try:
        cast = "\n".join(
            f"- {item['name']}（{item['role'] or '身份未知'}；出现 {item['occurrences']} 次）"
            for item in review["characters"][:20]
        )
        outline = "\n".join(
            f"第{item['chapters'][0]}—{item['chapters'][1]}章：{item['summary']}"
            for item in review["batch_summaries"]
        )
        synthesis, _ = await completion(
            settings,
            [
                {"role": "system", "content": SYNTHESIS_PROMPT},
                {
                    "role": "user",
                    "content": f"【分段概要】\n{outline}\n\n【人物】\n{cast}\n\n【开篇节选】\n{_chapter_excerpt(chapters[0].get('content', ''), 3000)}",
                },
            ],
            max_tokens=1800,
            timeout_seconds=240,
            temperature=0.2,
            validate=_validate_synthesis,
            workload="planning",
        )
    except Exception as exc:
        job["warnings"].append(f"整体设定归纳失败，只保留人物与设定候选：{str(exc)[:200]}")
    narrative = synthesis.get("narrative") if isinstance(synthesis.get("narrative"), dict) else {}
    review["fields"] = {
        key: str(synthesis.get(key, "")).strip()
        for key in ("genre", "premise", "outline", "author_intent", "book_rules", "story_so_far")
    }
    review["narrative"] = {key: str(value).strip() for key, value in narrative.items()}
    review["status"] = "pending"
    review["created_at"] = utc_now()
    job.update({"review": review, "status": "awaiting_review", "phase": "review"})
    job_event(job, "设定反推完成：请核对人物、世界设定和伏笔后确认", "success")
    extensions.put(project_id, JOB_KEY, job)
    return job


# ---------------------------------------------------------------------------
# Review confirmation
# ---------------------------------------------------------------------------


NARRATIVE_ALLOWED = {
    "pov": {"first", "third_limited", "omniscient", "auto"},
    "tense": {"past", "present", "auto"},
}


def apply_review(project: dict[str, Any], review: dict[str, Any], accept: dict[str, Any]) -> list[str]:
    """Copy the author-approved candidates into the project (pure, in place)."""
    applied: list[str] = []
    overwrite = bool(accept.get("overwrite"))
    fields = review.get("fields", {}) if isinstance(review.get("fields"), dict) else {}
    for field in accept.get("fields", []) or []:
        if field == "narrative":
            narrative = project.setdefault("narrative", {})
            for key, value in (review.get("narrative") or {}).items():
                if not value:
                    continue
                if key in NARRATIVE_ALLOWED and value not in NARRATIVE_ALLOWED[key]:
                    continue
                if overwrite or not str(narrative.get(key, "") or "").strip() or narrative.get(key) == "auto":
                    narrative[key] = value
            applied.append("叙事设定")
        elif field == "story_so_far":
            if fields.get("story_so_far"):
                project.setdefault("memory", {})["story_digest_candidate"] = {
                    "summary": fields["story_so_far"],
                    "source": "manuscript-import",
                    "created_at": utc_now(),
                }
                applied.append("全书进展候选摘要")
        elif field in {"genre", "premise", "outline", "author_intent", "book_rules"}:
            value = fields.get(field, "")
            if value and (overwrite or not str(project.get(field, "") or "").strip()):
                project[field] = value
                applied.append(field)
    existing_names = {
        _name_key(name)
        for character in project.get("characters", [])
        for name in [character.get("name", ""), *(character.get("aliases") or [])]
    }
    wanted = set(accept.get("characters", []) or [])
    for item in review.get("characters", []):
        if item.get("id") not in wanted or _name_key(item.get("name")) in existing_names:
            continue
        project.setdefault("characters", []).append({
            "id": str(uuid.uuid4()),
            "name": item["name"],
            "aliases": list(item.get("aliases", [])),
            "role": item.get("role", ""),
            "importance": item.get("importance") if item.get("importance") in IMPORTANCE_RANK else "supporting",
            "description": item.get("personality", ""),
            "personality": item.get("personality", ""),
            "appearance": item.get("appearance", ""),
            "goal": item.get("goal", ""),
            "voice": item.get("voice", ""),
            "relationships": item.get("relationships", ""),
            "source": "manuscript-import",
            "import_evidence": item.get("evidence", ""),
        })
        existing_names.add(_name_key(item["name"]))
        applied.append(f"人物：{item['name']}")
    wanted = set(accept.get("world_entries", []) or [])
    existing_titles = {_name_key(entry.get("title")) for entry in project.get("world_entries", [])}
    for item in review.get("world_entries", []):
        if item.get("id") not in wanted or _name_key(item.get("title")) in existing_titles:
            continue
        project.setdefault("world_entries", []).append({
            "id": str(uuid.uuid4()),
            "title": item["title"],
            "keys": list(item.get("keys", [])) or [item["title"]],
            "content": item.get("content", ""),
            "enabled": True,
            "source": "manuscript-import",
        })
        applied.append(f"世界书：{item['title']}")
    wanted = set(accept.get("foreshadow", []) or [])
    threads = project.setdefault("memory", {}).setdefault("plot_threads", [])
    known_threads = {_name_key(item.get("title")) for item in threads if isinstance(item, dict)}
    for item in review.get("foreshadow", []):
        if item.get("id") not in wanted or _name_key(item.get("title")) in known_threads:
            continue
        threads.append({
            "id": str(uuid.uuid4()),
            "title": item["title"],
            "status": "open",
            "type": "mystery",
            "setup": item.get("setup", ""),
            "latest": item.get("setup", ""),
            "expected_payoff": item.get("expected_payoff", ""),
            "created_chapter_number": int(item.get("chapter_number", 0) or 0),
            "last_advanced_chapter": int(item.get("chapter_number", 0) or 0),
            "chapter_id": item.get("chapter_id", ""),
            "last_chapter_id": item.get("chapter_id", ""),
            "evidence": item.get("evidence", ""),
            "evidence_verified": bool(item.get("evidence_verified")),
            "source_type": "import_candidate",
        })
        applied.append(f"伏笔：{item['title']}")
    return applied


# ---------------------------------------------------------------------------
# Settlement
# ---------------------------------------------------------------------------


async def run_import_settlement(
    *,
    store: Any,
    extensions: Any,
    project_id: str,
    chapter_memory: Callable[..., Awaitable[dict[str, Any]]],
    apply_memory: Callable[[dict[str, Any], dict[str, Any], dict[str, Any]], Any],
    request_factory: Callable[..., Any],
    max_chapter_attempts: int = 2,
    per_chapter_timeout: float = 300,
) -> dict[str, Any]:
    """Settle chapter memory sequentially; checkpoint after every chapter."""
    job = extensions.get(project_id, JOB_KEY)
    if not job:
        raise ValueError("这部作品没有旧稿导入任务")
    job.update({"status": "settling", "phase": "settlement", "pause_requested": False, "error": ""})
    job_event(job, f"开始逐章结算记忆（从第 {int(job.get('settle_index', 0)) + 1} 章开始）")
    extensions.put(project_id, JOB_KEY, job)
    while True:
        project = store.get(project_id)
        if project is None:
            raise KeyError(project_id)
        chapters = project.get("chapters", [])
        index = int(job.get("settle_index", 0))
        if index >= len(chapters):
            break
        current = extensions.get(project_id, JOB_KEY) or job
        if current.get("pause_requested") or job.get("pause_requested"):
            job.update({"status": "paused", "pause_requested": False})
            job_event(job, f"已在第 {index + 1} 章前暂停记忆结算", "warning")
            extensions.put(project_id, JOB_KEY, job)
            return job
        chapter = chapters[index]
        content = str(chapter.get("content", "")).strip()
        already = (
            str(chapter.get("memory_status", "")) == "committed"
            and str(chapter.get("accepted_content_hash", "")) == chapter_content_hash(content)
            and chapter.get("id") in job.get("settled_chapter_ids", [])
        )
        if len(content) < 100 or chapter.get("authority_state") != "locked":
            job["warnings"].append(f"第 {index + 1} 章正文不足 100 字或未锁定，已跳过记忆结算")
            job["settle_index"] = index + 1
            _persist(extensions, project_id, job)
            continue
        if already:
            job["settle_index"] = index + 1
            _persist(extensions, project_id, job)
            continue
        last_error = ""
        chapter_id = str(chapter.get("id", ""))
        vanished = False
        for attempt in range(1, max_chapter_attempts + 1):
            # Re-read on every attempt and locate the chapter by id: the author
            # may edit, insert or delete chapters while settlement runs, and the
            # memory must be derived from exactly the text being committed.
            project = store.get(project_id)
            if project is None:
                raise KeyError(project_id)
            chapter = next(
                (item for item in project.get("chapters", []) if str(item.get("id", "")) == chapter_id),
                None,
            )
            if chapter is None:
                vanished = True
                last_error = ""
                break
            content = str(chapter.get("content", "")).strip()
            expected = project["updated_at"]
            try:
                commit, _reused = begin_memory_commit(project, chapter)
                memory = await asyncio.wait_for(
                    chapter_memory(
                        request_factory(
                            project=project,
                            chapter_id=chapter["id"],
                            draft=content,
                            instruction="旧稿导入：从作者原稿反推本章记忆",
                        )
                    ),
                    timeout=per_chapter_timeout,
                )
                apply_memory(project, chapter, memory)
                warnings = list(memory.get("warnings", [])) if isinstance(memory, dict) else []
                mark_memory_commit(project, chapter, commit["id"], "committed", warnings=warnings)
                store.save(
                    project_id,
                    project,
                    reason=f"manuscript-import-settle-{index + 1}",
                    expected_updated_at=expected,
                )
                job.setdefault("settled_chapter_ids", []).append(chapter["id"])
                job["settle_index"] = index + 1
                job_event(job, f"第 {index + 1} 章《{chapter.get('title', '')}》记忆已结算", "success")
                last_error = ""
                break
            except ProjectConflictError:
                last_error = "作品在结算期间被修改，正在重新载入"
                continue
            except Exception as exc:
                last_error = str(exc)[:400] or exc.__class__.__name__
        if vanished:
            job_event(job, f"第 {index + 1} 章在结算期间被删除，改为结算该位置的下一章", "warning")
            _persist(extensions, project_id, job)
            continue
        if last_error:
            job.setdefault("failed_chapters", []).append(
                {"index": index + 1, "chapter_id": chapter.get("id", ""), "error": last_error}
            )
            job.update({"status": "paused", "error": f"第 {index + 1} 章记忆结算失败：{last_error}"})
            job_event(job, job["error"] + "；修复模型设置后可继续", "error")
            extensions.put(project_id, JOB_KEY, job)
            return job
        _persist(extensions, project_id, job)
    job.update({"status": "completed", "phase": "done", "error": ""})
    job_event(job, "旧稿导入完成：可以从最后一章继续写作", "success")
    extensions.put(project_id, JOB_KEY, job)
    return job


def public_job(job: dict[str, Any] | None, running: bool = False) -> dict[str, Any]:
    if not job:
        return {"exists": False}
    view = deepcopy(job)
    for batch in view.get("batches", []):
        batch.pop("result", None)
    view["exists"] = True
    view["running"] = running
    total = max(1, int(view.get("chapter_count", 0) or 0))
    done_batches = sum(1 for item in view.get("batches", []) if item.get("status") in {"done", "failed", "skipped"})
    view["progress"] = {
        "analysis": round(done_batches / max(1, len(view.get("batches", [])) or 1), 3),
        "settlement": round(int(view.get("settle_index", 0)) / total, 3),
    }
    if view.get("status") in {"analyzing", "settling"} and not running:
        view["status"] = "interrupted"
    return view
