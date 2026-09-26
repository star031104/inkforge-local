"""Persistent automatic-director runtime and HTTP control surface."""
from __future__ import annotations

import asyncio
from contextlib import suppress
import json
import math
import re
import uuid
from copy import deepcopy
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

import httpx

from ..api.schemas import (
    AutoDirectorStartRequest,
    AutoRefineRequest,
    ChapterActionRequest,
    ContinuePlannedWritingRequest,
)
from ..db import ensure_project_defaults, utc_now
from ..domain.planning_validation import (
    _director_volume_specs,
    validate_director_volume_expansion,
    validate_incubator_result,
    validate_master_result,
)
from ..domain.prose import (
    _clean_repair_continuation,
    _dedupe_adjacent_sentence_blocks,
    _dedupe_exact_paragraphs,
    _dedupe_repeated_sentences,
    _director_candidate_gate_failures,
    _effective_prose_settings,
    _ensure_final_route_closure,
    _number_phrases,
    _paragraphize_prose,
    _prose_char_count,
    _prose_looks_truncated,
    _remove_orphan_chinese_quotes,
    _sanitize_generated_prose,
    _scene_constraint_violations,
    _strip_nonfinal_scene_violations,
    _strip_unsupported_recollections,
    _trim_incomplete_prose_tail,
)
from ..domain.route_validation import _audit_route_checkpoint_prefix
from ..editorial_workflow import enqueue_repair, lock_chapter, rebuild_repair_queue
from ..fallbacks import local_memory_result
from ..llama_client import ModelContentFilteredError, chat_once, chat_stream
from ..manuscript_quality import manuscript_health_report
from ..memory import render_memories, retrieve_memories
from ..must_contracts import scan_contracts
from ..planning import apply_volume_routes, normalize_master_plan
from ..prompts import build_prompt, estimate_tokens, render_epistemic_context
from ..providers import settings_for_workload
from .editorial_policy import (
    _audit_issues,
    _director_manuscript_gate_failures,
    _quality_directives,
    _refinement_candidate_passes,
    _refinement_candidate_rank,
    _resolve_project_repairs,
    _systemic_quality_issues,
    _targeted_refinement_findings,
)
from .memory_settlement import _apply_director_memory, _chapter_number
from .model_errors import bounded_excerpt, planning_exception_detail


class _StoreProxy:
    """Resolve the active store on every access so tests and app swaps remain safe."""

    def __init__(self, provider: Callable[[], Any]):
        self._provider = provider

    def __getattr__(self, name: str) -> Any:
        return getattr(self._provider(), name)


class _LiveDependency:
    """Resolve an explicitly typed callback lazily for tests and hot swaps."""

    def __init__(self, provider: Callable[[], Callable[..., Any]]):
        self._provider = provider

    def __call__(self, *args: Any, **kwargs: Any) -> Any:
        return self._provider()(*args, **kwargs)


@dataclass(frozen=True)
class DirectorDependencies:
    attach_indexed_retrieval: Callable[[], Callable[..., Any]]
    persist_context_snapshot: Callable[[], Callable[..., Any]]
    complete_incubator_option: Callable[[], Callable[..., Any]]
    generate_incubator_core: Callable[[], Callable[..., Any]]
    director_full_outline: Callable[[], Callable[..., Any]]
    chapter_audit: Callable[[], Callable[..., Any]]
    chapter_memory: Callable[[], Callable[..., Any]]
    chapter_plan: Callable[[], Callable[..., Any]]
    director_character_card: Callable[[], Callable[..., Any]]
    director_master_bible: Callable[[], Callable[..., Any]]
    director_master_contract: Callable[[], Callable[..., Any]]
    director_master_volume_core: Callable[[], Callable[..., Any]]
    director_master_volume_details: Callable[[], Callable[..., Any]]
    director_plan_chapter_route: Callable[[], Callable[..., Any]]
    director_seed_brief: Callable[[], Callable[..., Any]]
    director_seed_cast: Callable[[], Callable[..., Any]]
    director_seed_world: Callable[[], Callable[..., Any]]
    find_chapter: Callable[[], Callable[..., Any]]
    launch_director: Callable[[], Callable[..., Any]]


@dataclass(frozen=True)
class DirectorRuntime:
    runners: dict[str, asyncio.Task[None]]
    run_auto_director: Callable[[str], Any]
    launch_director: Callable[[str], None]
    director_event: Callable[..., None]
    remove_quality_debt: Callable[..., bool]
    start: Callable[..., Any]
    write_planned: Callable[..., Any]
    refine: Callable[..., Any]
    get_task: Callable[..., Any]
    latest: Callable[..., Any]
    pause: Callable[..., Any]
    resume: Callable[..., Any]


class DirectorControlError(RuntimeError):
    def __init__(self, status_code: int, detail: str):
        super().__init__(detail)
        self.status_code = status_code
        self.detail = detail


def create_director_runtime(
    *,
    store_provider: Callable[[], Any],
    backup_directory: Path | Callable[[], Path],
    dependencies: DirectorDependencies,
) -> DirectorRuntime:
    """Build one director runtime with explicit access to application dependencies."""
    director_runners: dict[str, asyncio.Task[None]] = {}
    store = _StoreProxy(store_provider)
    BACKUP_DIRECTORY = backup_directory
    runtime_instance_id = str(store.instance_id)

    def _backup_directory() -> Path:
        return BACKUP_DIRECTORY() if callable(BACKUP_DIRECTORY) else BACKUP_DIRECTORY

    _attach_indexed_retrieval = _LiveDependency(dependencies.attach_indexed_retrieval)
    _persist_context_snapshot = _LiveDependency(dependencies.persist_context_snapshot)
    _complete_incubator_option = _LiveDependency(dependencies.complete_incubator_option)
    _generate_incubator_core = _LiveDependency(dependencies.generate_incubator_core)
    _director_full_outline = _LiveDependency(dependencies.director_full_outline)
    chapter_audit = _LiveDependency(dependencies.chapter_audit)
    chapter_memory = _LiveDependency(dependencies.chapter_memory)
    chapter_plan = _LiveDependency(dependencies.chapter_plan)
    director_character_card = _LiveDependency(dependencies.director_character_card)
    director_master_bible = _LiveDependency(dependencies.director_master_bible)
    director_master_contract = _LiveDependency(dependencies.director_master_contract)
    director_master_volume_core = _LiveDependency(dependencies.director_master_volume_core)
    director_master_volume_details = _LiveDependency(dependencies.director_master_volume_details)
    director_plan_chapter_route = _LiveDependency(dependencies.director_plan_chapter_route)
    director_seed_brief = _LiveDependency(dependencies.director_seed_brief)
    director_seed_cast = _LiveDependency(dependencies.director_seed_cast)
    director_seed_world = _LiveDependency(dependencies.director_seed_world)
    find_chapter = _LiveDependency(dependencies.find_chapter)
    _launch_director = _LiveDependency(dependencies.launch_director)

    def _director_event(task: dict[str, Any], message: str, kind: str = "info") -> None:
        task.setdefault("events", []).append(
            {"time": utc_now(), "kind": kind, "message": message}
        )
        task["events"] = task["events"][-100:]
        task["message"] = message
    
    
    def _save_director_task(
        task: dict[str, Any], message: str | None = None, kind: str = "info"
    ) -> dict[str, Any]:
        if message:
            _director_event(task, message, kind)
        saved = store.save_director_task(task["id"], task)
        if saved.get("status") in {"queued", "running", "stopping"}:
            store.renew_director_task_lease(task["id"], runtime_instance_id)
        return saved
    
    
    def _record_planning_debt(
        task: dict[str, Any], debt: dict[str, Any]
    ) -> None:
        debts = task.setdefault("planning_debts", [])
        if not isinstance(debts, list):
            debts = []
            task["planning_debts"] = debts
        identity = (
            str(debt.get("phase", "")),
            str(debt.get("volume_id", "")),
            int(debt.get("chapter", 0) or 0),
        )
        debts[:] = [
            item for item in debts
            if (
                str(item.get("phase", "")),
                str(item.get("volume_id", "")),
                int(item.get("chapter", 0) or 0),
            ) != identity
        ]
        debts.append({"time": utc_now(), **debt})
        task["planning_debts"] = debts[-120:]
    
    
    def _quality_debt_index(task: dict[str, Any], chapter_id: str) -> int:
        debts = task.get("quality_debts", [])
        if not isinstance(debts, list):
            return -1
        return next(
            (
                index
                for index, item in enumerate(debts)
                if isinstance(item, dict)
                and str(item.get("chapter_id", "")) == str(chapter_id)
            ),
            -1,
        )
    
    
    def _remove_quality_debt(task: dict[str, Any], chapter_id: str) -> bool:
        debts = task.get("quality_debts", [])
        if not isinstance(debts, list):
            task["quality_debts"] = []
            return False
        kept = [
            item
            for item in debts
            if not isinstance(item, dict)
            or str(item.get("chapter_id", "")) != str(chapter_id)
        ]
        changed = len(kept) != len(debts)
        task["quality_debts"] = kept
        return changed
    
    
    def _reconcile_director_quality_debts(task: dict[str, Any]) -> dict[str, Any]:
        """Keep persisted director debt scores aligned with the latest chapter run."""
        project = store.get(str(task.get("project_id", "")))
        debts = task.get("quality_debts", [])
        if not project or not isinstance(debts, list) or not debts:
            return task
        chapters = {
            str(item.get("id", "")): item
            for item in project.get("chapters", [])
            if isinstance(item, dict)
        }
        reconciled: list[dict[str, Any]] = []
        changed = False
        seen: set[str] = set()
        for item in debts:
            if not isinstance(item, dict):
                changed = True
                continue
            chapter_id = str(item.get("chapter_id", ""))
            if chapter_id in seen:
                changed = True
                continue
            seen.add(chapter_id)
            chapter = chapters.get(chapter_id, {})
            execution = chapter.get("execution", {}) if isinstance(chapter, dict) else {}
            if not isinstance(execution, dict):
                execution = {}
            if str(execution.get("status", "")) == "accepted":
                changed = True
                continue
            current = dict(item)
            if "audit_score" in execution:
                score = max(0, min(100, int(execution.get("audit_score", 0) or 0)))
                if int(current.get("score", 0) or 0) != score:
                    current["score"] = score
                    changed = True
            reconciled.append(current)
        if changed:
            task["quality_debts"] = reconciled
            # Polling a live task must remain read-only: saving a just-read payload
            # here could race the runner and overwrite a newer checkpoint.
            if task.get("status") not in {"queued", "running", "stopping"}:
                task = store.save_director_task(task["id"], task)
        return task
    
    
    def _director_project_from_option(
        project: dict[str, Any], option: dict[str, Any], config: dict[str, Any]
    ) -> dict[str, Any]:
        project = ensure_project_defaults(project)
        project["title"] = str(option.get("title") or "AI 自动创作小说")
        project["genre"] = str(option.get("genre", ""))
        project["story_mode"] = config.get("story_mode", "long")
        project["premise"] = str(option.get("premise", ""))
        project["outline"] = str(option.get("outline", ""))
        project["author_intent"] = str(option.get("author_intent", ""))
        project["current_focus"] = str(
            option.get("current_focus") or option.get("first_arc", "")
        )
        project["book_rules"] = "\n".join(
            str(item) for item in option.get("book_rules", []) if str(item).strip()
        )
        project["production_spec"] = str(config.get("seed", "")).strip()
        narrative = project["narrative"]
        narrative.update(
            {
                "target_chapters": int(config.get("target_chapters", 30)),
                "central_question": str(option.get("central_question", "")),
                "ending_direction": str(option.get("ending_direction", "")),
                "current_arc": str(option.get("first_arc", "")),
                "pov": (
                    option.get("pov")
                    if option.get("pov")
                    in {"auto", "first", "third_limited", "omniscient"}
                    else "auto"
                ),
                "tone": str(option.get("tone", "")),
            }
        )
        project["settings"]["target_words"] = int(config.get("target_words", 1200))
        project["characters"] = []
        for character_index, item in enumerate(option.get("characters", [])):
            if not isinstance(item, dict):
                continue
            project["characters"].append(
                {
                    "id": str(uuid.uuid4()),
                    "name": str(item.get("name", "")),
                    "role": str(item.get("role", "")),
                    "aliases": item.get("aliases", []) if isinstance(item.get("aliases"), list) else [],
                    "importance": "main" if character_index == 0 else "supporting",
                    "active": True,
                    "description": str(item.get("description", "")),
                    "personality": str(item.get("personality", item.get("description", ""))),
                    "appearance": str(item.get("appearance", "")),
                    "values": str(item.get("values", "")),
                    "fears": str(item.get("fears", "")),
                    "contradictions": str(item.get("contradictions", "")),
                    "mannerisms": str(item.get("mannerisms", "")),
                    "relationships": str(item.get("relationships", "")),
                    "arc": str(item.get("arc", "")),
                    "hard_limits": str(item.get("hard_limits", "")),
                    "goal": str(item.get("goal", "")),
                    "state": str(item.get("state", "")),
                    "knowledge": str(item.get("knowledge", "")),
                    "secrets": str(item.get("secrets", "")),
                    "voice": str(item.get("voice", "")),
                    "dialogue_examples": item.get("dialogue_examples", []) if isinstance(item.get("dialogue_examples"), list) else [],
                    "location": "",
                    "items": "",
                    "emotion": "",
                }
            )
        project["world_entries"] = []
        for index, item in enumerate(option.get("world_entries", [])):
            if not isinstance(item, dict):
                continue
            project["world_entries"].append(
                {
                    "id": str(uuid.uuid4()),
                    "title": str(item.get("title", "")),
                    "category": str(item.get("category", "世界设定")),
                    "keys": [str(key) for key in item.get("keys", [])],
                    "secondary_keys": [],
                    "selective_logic": "and_any",
                    "content": str(item.get("content", "")),
                    "canon": str(item.get("canon", "hard" if item.get("constant") else "soft")),
                    "position": "after",
                    "order": 20 + index * 10,
                    "constant": bool(item.get("constant", False)),
                    "enabled": True,
                    "match": "any",
                    "character_names": [],
                    "chapter_start": 0,
                    "chapter_end": 0,
                    "inclusion_group": "",
                    "non_recursable": False,
                    "prevent_recursion": False,
                    "delay_until_recursion": False,
                }
            )
        project["chapters"] = [
            {
                "id": str(uuid.uuid4()),
                "title": "第一章",
                "summary": "",
                "content": "",
                "scene_goal": str(option.get("opening_hook", "")),
                "author_note": "",
                "plan": {
                    "goal": str(option.get("opening_hook", "")),
                    "conflict": str(option.get("central_conflict", "")),
                    "must_keep": [],
                    "must_avoid": [],
                    "turning_point": "",
                    "ending_hook": "",
                },
            }
        ]
        project["planning"] = {"version": 1, "master": {}, "volumes": []}
        project["memory"] = {
            "state_version": 4,
            "epistemic_schema_version": 1,
            "story_so_far": "",
            "story_digest_candidate": {},
            "facts": [],
            "plot_threads": [],
            "timeline": [],
            "relationships": [],
            "continuity_notes": [],
            "description_ledger": [],
            "commits": [],
        }
        return ensure_project_defaults(project)
    
    
    async def _director_generate_prose(
        task: dict[str, Any], project: dict[str, Any], chapter: dict[str, Any],
        *, mode: str = "instruction", instruction: str = "", selection: str = ""
    ) -> str:
        config = task["config"]
        # Generation may need a much larger output window than the saved everyday
        # model setting, especially when a 4-5k character chapter is rewritten in
        # full. Work on a copy so that the temporary token ceiling is not persisted.
        project = deepcopy(project)
        target_chars = int(config["target_words"])
        project["settings"] = _effective_prose_settings(
            project.get("settings", {}),
            target_chars,
            "revision" if mode == "rewrite" else "prose",
        )
        project["settings"]["target_words"] = target_chars
        safe_directive_markers = (
            "长度", "重复", "句式", "时代", "元话语", "疑似新增往事",
            "待确认新设定", "视角", "知识来源", "人物状态", "时间线",
            "目标偏离", "段落节奏",
        )
        adaptive = [
            str(item)
            for item in task.get("quality_directives", [])
            if str(item).strip()
            and any(marker in str(item) for marker in safe_directive_markers)
        ][-6:]
        if adaptive:
            instruction += (
                "\n【自动导演根据前章质量债生成的临时硬约束】\n- "
                + "\n- ".join(adaptive)
            )

        def compact_messages() -> list[dict[str, str]]:
            """Keep the chapter route while removing unrelated long-history context."""
            chapters = project.get("chapters", [])
            chapter_index = next(
                (i for i, item in enumerate(chapters) if item.get("id") == chapter.get("id")),
                0,
            )
            previous = chapters[max(0, chapter_index - 2):chapter_index]
            context = "\n".join(
                f"第 {chapter_index - len(previous) + i + 1} 章《{item.get('title', '')}》："
                f"{str(item.get('summary') or item.get('content', '')[-550:])[:650]}"
                for i, item in enumerate(previous)
            )
            route = chapter.get("route") or {}
            plan = chapter.get("plan") or {}
            fields = ("goal", "conflict", "turning_point", "ending_hook")
            beats = "\n".join(
                f"{name}：{str(plan.get(name) or route.get(name) or '')[:550]}"
                for name in fields
            )
            required = "；".join(str(x) for x in route.get("must_keep", [])[:6])
            forbidden = "；".join(str(x) for x in route.get("must_avoid", [])[:6])
            return [
                {
                    "role": "system",
                    "content": (
                        "你是历史小说作者。只输出完整的小说正文。"
                        "以人物行动、对话和文书细节呈现冲突；涉及伤亡时简洁交代后果，"
                        "避免直观的暴力或血腥描写。不得编造与既有情节冲突的事实。"
                    ),
                },
                {
                    "role": "user",
                    "content": (
                        f"作品《{project.get('title', '')}》，本章《{chapter.get('title', '')}》。"
                        f"目标约 {target_chars} 字。\n前情：\n{context}\n本章路线：\n{beats}"
                        f"\n必须保持：{required}\n不得发生：{forbidden}"
                        "\n请完成起因、冲突、转折和收束，直接从具体场景起笔。"
                    ),
                },
            ]
    
        async def receive(settings: dict[str, Any], messages: list[dict[str, str]]) -> str:
            """Buffer director prose and continue it after transient stream drops."""
            try:
                recovery_attempts = int(
                    settings.get("director_stream_recovery_attempts", 3) or 3
                )
            except (TypeError, ValueError):
                recovery_attempts = 3
            recovery_attempts = max(1, min(recovery_attempts, 5))
            original_messages = [dict(item) for item in messages]
            active_messages = original_messages
            combined = ""
            for recovery_index in range(recovery_attempts):
                pieces: list[str] = []
                try:
                    async for piece in chat_stream(
                        settings_for_workload(settings, "prose"), active_messages
                    ):
                        pieces.append(piece)
                        current = store.get_director_task(task["id"])
                        if not current or current.get("status") != "running":
                            raise asyncio.CancelledError()
                    candidate = "".join(pieces).strip()
                    continuation = (
                        _clean_repair_continuation(combined, candidate)
                        if combined
                        else candidate
                    )
                    if continuation:
                        combined = (
                            combined.rstrip() + "\n\n" + continuation.lstrip()
                        ).strip()
                    if recovery_index:
                        latest = store.get_director_task(task["id"])
                        if latest and latest.get("status") == "running":
                            _director_event(
                                latest,
                                f"模型连接已自动恢复，保留并续接了约 {_prose_char_count(combined)} 字正文",
                                "success",
                            )
                            saved = store.save_director_task(task["id"], latest)
                            task.clear()
                            task.update(saved)
                    return combined
                except (httpx.TransportError, httpx.TimeoutException) as exc:
                    candidate = "".join(pieces).strip()
                    continuation = (
                        _clean_repair_continuation(combined, candidate)
                        if combined
                        else candidate
                    )
                    if continuation:
                        combined = (
                            combined.rstrip() + "\n\n" + continuation.lstrip()
                        ).strip()
                    if recovery_index + 1 >= recovery_attempts:
                        raise
                    latest = store.get_director_task(task["id"])
                    if not latest or latest.get("status") != "running":
                        raise asyncio.CancelledError() from exc
                    _director_event(
                        latest,
                        "模型连接短暂中断；"
                        + (
                            f"已保留约 {_prose_char_count(combined)} 字，正在自动续接"
                            if combined
                            else "尚未收到正文，正在自动重连"
                        )
                        + f"（{recovery_index + 1}/{recovery_attempts - 1}）",
                        "warning",
                    )
                    saved = store.save_director_task(task["id"], latest)
                    task.clear()
                    task.update(saved)
                    await asyncio.sleep(min(8, 2 ** recovery_index))
                    if combined:
                        anchor = re.sub(r"\s+", " ", combined[-220:]).strip()
                        active_messages = original_messages + [
                            {"role": "assistant", "content": combined},
                            {
                                "role": "user",
                                "content": (
                                    "刚才传输中断。上面的 assistant 正文已经完整保留；"
                                    "只从最后一句之后续写，绝不重写、复述或解释。"
                                    f"接续锚点：【{anchor}】。保持原计划、人物、时间、"
                                    "地点和视角不变，完成剩余正文并自然收束。"
                                ),
                            },
                        ]
                    else:
                        active_messages = original_messages
            return combined
    
        if project.get("settings", {}).get("director_scene_generation", False):
            plan = chapter.get("plan", {}) if isinstance(chapter.get("plan"), dict) else {}
            route = chapter.get("route", {}) if isinstance(chapter.get("route"), dict) else {}
            raw_contracts = route.get("scene_contracts", [])
            if isinstance(raw_contracts, list) and 3 <= len(raw_contracts) <= 8 and all(
                isinstance(item, dict) and str(item.get("boundary", "")).strip()
                for item in raw_contracts
            ):
                phases = [
                    (
                        str(item.get("job", "完成当前场景合同")).strip(),
                        str(item.get("boundary", "")).strip(),
                        [str(beat).strip() for beat in item.get("beats", []) if str(beat).strip()][:8],
                    )
                    for item in raw_contracts
                ]
            else:
                phases = [
                    (
                        "建立本章进入状态，只让人物开始当前任务",
                        str(route.get("goal") or plan.get("goal", "")).strip(),
                        [],
                    ),
                    (
                        "让主要阻力在现场具体发生，但不解决",
                        str(route.get("conflict") or plan.get("conflict", "")).strip(),
                        [],
                    ),
                    (
                        "让核验、对抗或选择推进到本章转折",
                        str(route.get("turning_point") or plan.get("turning_point", "")).strip(),
                        [],
                    ),
                    (
                        "只完成本章规定的退出状态并收束",
                        str(route.get("ending_hook") or plan.get("ending_hook", "")).strip(),
                        [],
                    ),
                ]
            ending_text = str(route.get("ending_hook") or plan.get("ending_hook", ""))
            delayed_actions = [
                action
                for action in (
                    "开仓", "署名", "下令", "交出", "烧毁", "放弃", "离开",
                    "抵达", "处死", "杀死", "获得官印", "交还官印",
                )
                if action in ending_text
            ]
            partial = ""
            scene_target = max(
                320,
                min(
                    900,
                    int(project.get("settings", {}).get("director_scene_target_chars", 850) or 850),
                ),
            )
            for scene_index, (scene_job, scene_boundary, scene_beats) in enumerate(phases, start=1):
                scene_project = deepcopy(project)
                scene_chapter = next(
                    item for item in scene_project.get("chapters", [])
                    if item.get("id") == chapter.get("id")
                )
                scene_chapter["content"] = partial
                scene_instruction = (
                    f"整章分场写作：当前是第 {scene_index}/{len(phases)} 段。{scene_job}。\n"
                    f"本段唯一交付边界：【{scene_boundary}】。\n"
                    f"本段写约 {scene_target} 个中文字符；只输出从现有末句之后发生的新正文，"
                    "不得复述前文、不得重新介绍人物任务、不得新增往事或回忆。"
                )
                if scene_index < len(phases):
                    scene_instruction += (
                        "当前不得越过本段边界，也不得猜测、提及或执行尚未提供的后段内容。"
                        "本段结束在能自然接续下一段的动作或压力上。"
                    )
                    if delayed_actions:
                        scene_instruction += (
                            "存在尚未提供的章末延迟动作；当前段不得猜测、命名或开始其物理步骤。"
                        )
                else:
                    scene_instruction += (
                        "这是末段：准确交付章末状态后立即停止，不新增陌生人、新消息、"
                        "新线索或下一章事件。"
                    )
                    if delayed_actions:
                        scene_instruction += (
                            "此前延迟的章末动作现在必须在场完成："
                            + "、".join(delayed_actions)
                            + "。"
                        )
                # Whole-chapter revision feedback can name a required ending event.
                # Broadcasting it into every isolated scene defeats the phase wall
                # and makes small models execute the ending in scene one. The route
                # remains authoritative; audit feedback is enforced by rebuilding
                # all four route phases and re-auditing the finished chapter.
                scene_request = {
                    "project": scene_project,
                    "chapter_id": chapter["id"],
                    "mode": "continue" if partial else "instruction",
                    "instruction": scene_instruction,
                    "selection": "",
                    "target_words": scene_target,
                }
                _attach_indexed_retrieval(scene_project, scene_request)
                scene_build = build_prompt(scene_project, scene_request)
                scene_authority = "\n".join([scene_boundary, *scene_beats])
                compact_query = "\n".join(
                    [scene_authority, partial[-1800:], scene_instruction]
                )
                scene_memories = retrieve_memories(
                    scene_project,
                    compact_query,
                    _chapter_number(scene_project, scene_chapter) - 1,
                    8,
                    indexed_hits=scene_request.get("_indexed_memory_hits", []),
                )
                active_names = {
                    str(item.get("name", "")).strip()
                    for item in scene_project.get("characters", [])
                    if str(item.get("name", "")).strip()
                    and str(item.get("name", "")).strip() in compact_query
                }
                main_name = next(
                    (
                        str(item.get("name", "")).strip()
                        for item in scene_project.get("characters", [])
                        if item.get("active", True)
                        and str(item.get("importance", "supporting")) == "main"
                        and str(item.get("name", "")).strip()
                    ),
                    "",
                )
                if main_name:
                    active_names.add(main_name)
                character_lines = []
                for item in scene_project.get("characters", []):
                    name = str(item.get("name", "")).strip()
                    if not name or name not in active_names or not item.get("active", True):
                        continue
                    character_lines.append(
                        f"【{name}】身份：{item.get('role', '')}；"
                        f"人格：{item.get('personality', '')}；"
                        f"不可写偏：{item.get('hard_limits', '')}；"
                        f"当前状态：{item.get('state', '')}；"
                        f"当前知情：{item.get('knowledge_baseline', item.get('knowledge', ''))}；"
                        f"语言：{item.get('voice', '')}"
                    )
                lore_lines = []
                folded_query = compact_query.casefold()
                for entry in scene_project.get("world_entries", []):
                    if not isinstance(entry, dict) or not entry.get("enabled", True):
                        continue
                    keys = entry.get("keys", [])
                    if isinstance(keys, str):
                        keys = re.split(r"[,，\n]", keys)
                    if entry.get("constant", False) or any(
                        str(key).strip().casefold() in folded_query
                        for key in keys if str(key).strip()
                    ):
                        lore_lines.append(
                            f"【{entry.get('title', '')}】{entry.get('content', '')}"
                        )
                    if len(lore_lines) >= 4:
                        break
                completed_boundaries = [
                    f"第{number}段已经完成" for number in range(1, scene_index)
                ]
                visible_route_lines = [f"当前边界：{scene_boundary}"]
                if scene_beats:
                    visible_route_lines.append(
                        "必须依次写出的动作：" + "；".join(scene_beats)
                    )
                compact_system = (
                    "你是严谨的中文历史小说作者，只续写当前分场正文。禁止解释任务、输出标题或规划标签。"
                    "只写眼前可观察的动作、物件、对话和判断；禁止用‘想起、记得、曾经、上次、"
                    "昨日见过’凭空制造往事。禁止现代技术/管理术语、全知剧透和无来源新人物。"
                    "不得复述已经完成的动作、证据、问答或结论。路线中的所有数字必须逐字保持，"
                    "不得缩写、换算、改成近似数或另造数字。禁用目光如炬、指节发白、空气凝固、"
                    "命运齿轮、无人知道、更大的风暴。第三人称限知必须服从人物知情边界。"
                    "不得新增能改变判断的证物、证词、书信、印鉴、血迹、布料、暗号或目击者；"
                    "当前分场的有效证据只有权威路线明确写出的内容。"
                )
                compact_user = f"""【当前分场可见的权威路线】
    {chr(10).join(visible_route_lines)}
    整章级 must_keep 与 must_avoid 已由导演校验，本段不重复展示，避免诱发复述或提前写出后段事件。
    本段允许使用的明确数量只有：{'、'.join(sorted(_number_phrases(scene_authority))) or '无'}；不得另造日期、时刻、车数、物数，也不得提前引用其他分场的数量。
    
    【当前人物】
    {chr(10).join(character_lines) or '只使用路线中已经点名的人物。'}
    本段可出现的姓名只有：{'、'.join(sorted(active_names)) or '无'}；其他现场人物只能用已有职称，不得创造姓名。
    
    【人物知情边界】
    {render_epistemic_context(scene_project, _chapter_number(scene_project, scene_chapter) - 1, compact_query, active_names)}
    
    【相关已发生记忆】
    {bounded_excerpt(render_memories(scene_memories), 2400) or '尚无已接受章节记忆。'}
    
    【当前触发世界规则】
    {chr(10).join(lore_lines) or '无额外条目；不得自行补造制度与前史。'}
    
    【前文最后接续锚点】
    {partial[-360:] if partial else '尚未起笔。'}
    
    【已完成、严禁换词重写】
    {'；'.join(completed_boundaries) or '无。'}
    
    【当前分场指令】
    {scene_instruction}"""
                scene_build.messages = [
                    {"role": "system", "content": compact_system},
                    {"role": "user", "content": compact_user},
                ]
                scene_build.estimated_tokens = estimate_tokens(
                    compact_system + "\n" + compact_user
                )
                scene_build.sections = [
                    {
                        "name": "导演分场紧凑上下文",
                        "content": compact_user,
                        "priority": 100,
                        "tokens": estimate_tokens(compact_user),
                        "tokens_before": estimate_tokens(compact_user),
                        "tokens_after": estimate_tokens(compact_user),
                        "status": "included",
                        "selected": True,
                        "reason": "",
                        "role": "user",
                        "required": True,
                    }
                ]
                _persist_context_snapshot(
                    scene_project,
                    scene_request,
                    scene_build,
                    reason=f"director_generation_scene_{scene_index}",
                )
                segment = await asyncio.wait_for(
                    receive(scene_project["settings"], scene_build.messages),
                    timeout=900,
                )
                violations = _scene_constraint_violations(
                    segment, route, scene_index, len(phases), delayed_actions, scene_authority
                )
                if (
                    _prose_char_count(segment) < 280
                    or _prose_looks_truncated(segment)
                    or violations
                ):
                    retry_messages = [dict(item) for item in scene_build.messages]
                    retry_messages.append(
                        {
                            "role": "user",
                            "content": (
                                "上一回应未通过当前分场的硬校验，请完全重写本分场。"
                                f"校验原因：{'；'.join(violations) or '篇幅不足或句子未结束'}。"
                                "至少写到约320个中文字符；仍只执行当前分场边界，不复述前文、"
                                "不抢写后续、不解释。不得新增任何人名、数字、时刻、制度或道具。"
                            ),
                        }
                    )
                    segment = await asyncio.wait_for(
                        receive(scene_project["settings"], retry_messages),
                        timeout=900,
                    )
                segment = _strip_nonfinal_scene_violations(
                    segment, route, scene_index, len(phases), delayed_actions, scene_authority
                )
                if _prose_char_count(segment) < 320:
                    top_up_messages = [dict(item) for item in scene_build.messages]
                    top_up_messages.extend(
                        [
                            {"role": "assistant", "content": segment},
                            {
                                "role": "user",
                                "content": (
                                    "只从上句之后续写当前分场约150至220个中文字符，"
                                    "补足现场动作、物件反应或简短问答。不得复述，"
                                    "不得新增姓名、数字、时刻、制度、道具或后段事件；"
                                    "写完一个完整句子立即停止。"
                                ),
                            },
                        ]
                    )
                    top_up = await asyncio.wait_for(
                        receive(scene_project["settings"], top_up_messages),
                        timeout=900,
                    )
                    top_up = _clean_repair_continuation(segment, top_up)
                    top_up = _strip_nonfinal_scene_violations(
                        top_up, route, scene_index, len(phases), delayed_actions, scene_authority
                    )
                    segment = (segment.rstrip() + "\n\n" + top_up.lstrip()).strip()
                if scene_index == len(phases):
                    segment = _ensure_final_route_closure(
                        segment, route, delayed_actions
                    )
                violations = _scene_constraint_violations(
                    segment, route, scene_index, len(phases), delayed_actions, scene_authority
                )
                if violations:
                    raise ValueError(
                        f"模型第 {scene_index} 段违反路线硬约束：{'；'.join(violations)}"
                    )
                segment = _clean_repair_continuation(partial, segment)
                segment_authority = "\n".join(
                    [
                        json.dumps(route, ensure_ascii=False),
                        json.dumps(plan, ensure_ascii=False),
                        "\n".join(character_lines),
                        render_memories(scene_memories),
                        str(scene_chapter.get("summary", "")),
                    ]
                )
                segment = _strip_unsupported_recollections(
                    _dedupe_adjacent_sentence_blocks(segment), segment_authority
                )
                # A scene is a semantic boundary, not an independent chapter.  Treat
                # this only as a non-empty-output guard: aggregate chapter length and
                # the later local/AI audit own development and pacing quality.  Safety
                # stripping may legitimately reduce a concise bridge scene heavily.
                minimum_scene_chars = 20
                if _prose_char_count(segment) < minimum_scene_chars:
                    raise ValueError(
                        f"模型重试并补写后第 {scene_index} 段正文仍不足 {minimum_scene_chars} 字"
                    )
                partial = (partial.rstrip() + "\n\n" + segment.lstrip()).strip()
            if len(partial) < 300:
                raise ValueError("模型返回的分场正文不足 300 字")
            return _dedupe_exact_paragraphs(
                _paragraphize_prose(
                    _remove_orphan_chinese_quotes(
                        _dedupe_repeated_sentences(
                            _dedupe_adjacent_sentence_blocks(partial)
                        )
                    )
                )
            )
    
        length_multiplier = max(
            1.0,
            min(
                1.6,
                float(project.get("settings", {}).get("director_length_prompt_multiplier", 1.0) or 1.0),
            ),
        )
        prompt_target = int(math.ceil(int(config["target_words"]) * length_multiplier))
        request = {
            "project": project, "chapter_id": chapter["id"], "mode": mode,
            "instruction": instruction, "selection": selection,
            "target_words": prompt_target,
        }
        if mode == "rewrite" and selection.strip():
            # Director revisions replace an entire chapter. Manual rewrite mode is
            # often used for a selected sentence/paragraph and intentionally keeps
            # the selection's scale; these are different operations.
            request["_full_chapter_rewrite"] = True
        _attach_indexed_retrieval(project, request)
        build = build_prompt(project, request)
        _persist_context_snapshot(project, request, build, reason="director_generation")
        try:
            text = await asyncio.wait_for(
                receive(project["settings"], build.messages), timeout=900
            )
        except ModelContentFilteredError:
            latest = store.get_director_task(task["id"])
            if not latest or latest.get("status") != "running":
                raise
            _director_event(
                latest,
                "正文请求被模型服务中止；正在用精简上下文和非直观暴力的写法重试一次",
                "warning",
            )
            saved = store.save_director_task(task["id"], latest)
            task.clear()
            task.update(saved)
            text = await asyncio.wait_for(
                receive(project["settings"], compact_messages()), timeout=900
            )
        text, cleanup_notes = _sanitize_generated_prose(text)
        text, tail_repair_note = _trim_incomplete_prose_tail(text)
        if tail_repair_note:
            cleanup_notes.append(tail_repair_note)
        if cleanup_notes:
            latest = store.get_director_task(task["id"])
            if latest and latest.get("status") == "running":
                _director_event(
                    latest,
                    "；".join(cleanup_notes) + "，仅保留可审计的小说正文",
                    "warning",
                )
                saved = store.save_director_task(task["id"], latest)
                task.clear()
                task.update(saved)
        if _prose_char_count(text) < 100:
            raise ValueError("模型返回的正文不足 100 字")
        return text

    async def _director_chapter_memory(
        project: dict[str, Any], chapter: dict[str, Any], draft: str
    ) -> dict[str, Any]:
        try:
            return await asyncio.wait_for(
                chapter_memory(
                    ChapterActionRequest(
                        project=project, chapter_id=chapter["id"], draft=draft,
                        instruction="自动导演状态回灌",
                    )
                ),
                timeout=180,
            )
        except asyncio.TimeoutError:
            return local_memory_result(
                project, chapter, draft,
                "完整记忆提取超过 180 秒；已保留正文并使用本地基础摘要",
            )
    
    
    async def _director_patch_refinement_excerpts(
        project: dict[str, Any], chapter: dict[str, Any], draft: str,
        audit: dict[str, Any], target_chars: int,
    ) -> str:
        """Repair one to three evidenced paragraphs without destabilizing the chapter."""
        findings = _targeted_refinement_findings(audit)
        if not findings:
            return draft
        patched = draft
        settings = _effective_prose_settings(
            project.get("settings", {}), min(1200, max(300, target_chars)), "revision"
        )
        for finding in findings:
            quote = finding["quote"]
            start = patched.find(quote)
            if start < 0:
                continue
            before = patched[max(0, start - 260):start]
            after = patched[start + len(quote):start + len(quote) + 260]
            messages = [
                {
                    "role": "system",
                    "content": (
                        "你是中文小说局部精修编辑。只重写给定问题段落，不改事件事实、人物、"
                        "时间顺序和叙事视角。输出一段可直接替换的小说正文；不要标题、说明、"
                        "引号包裹或修改计划。"
                    ),
                },
                {
                    "role": "user",
                    "content": (
                        f"【章节】{chapter.get('title', '')}\n"
                        f"【问题】[{finding['category']}] {finding['message']}\n"
                        f"【修复要求】{finding['suggestion'] or '换用新的具体动作、观察与人物反应表达。'}\n"
                        f"【前文】{before}\n【必须替换的段落】{quote}\n【后文】{after}\n"
                        "只输出替换段落，保持与前后文自然衔接。"
                    ),
                },
            ]
            try:
                replacement = await chat_once(
                    settings,
                    messages,
                    temperature=0.35,
                    max_tokens=min(1200, max(320, len(quote) * 3)),
                    timeout_seconds=180,
                )
                replacement, _notes = _sanitize_generated_prose(replacement)
                replacement = replacement.strip().strip("`\n")
            except Exception:
                return draft
            replacement_chars = _prose_char_count(replacement)
            if (
                replacement_chars < max(18, int(_prose_char_count(quote) * 0.45))
                or replacement_chars > max(500, int(_prose_char_count(quote) * 2.2))
                or quote in replacement
                or _director_candidate_gate_failures(replacement, max(300, len(quote)))
            ):
                return draft
            patched = patched[:start] + replacement + patched[start + len(quote):]
        return patched
    
    
    async def _run_auto_refinement(
        task: dict[str, Any], project: dict[str, Any]
    ) -> None:
        """Audit and revise existing chapters without replacing failed candidates."""
        config = task["config"]
        targets = [str(item) for item in task.get("refinement_targets", []) if str(item)]
        task["total_chapters"] = len(targets)
        start = max(0, int(task.get("refine_index", 0) or 0))
        for position in range(start, len(targets)):
            latest = store.get_director_task(task["id"])
            if not latest or latest.get("status") != "running":
                raise asyncio.CancelledError()
            task.clear()
            task.update(latest)
            project = store.get(task["project_id"])
            if not project:
                raise ValueError("精修作品已被删除")
            chapter_id = targets[position]
            chapter_index, chapter = find_chapter(project, chapter_id)
            original = str(chapter.get("content", "")).strip()
            task["current_chapter"] = chapter_index + 1
            task["current_refine_position"] = position + 1
            task = _save_director_task(
                task,
                f"AI 精修 {position + 1}/{len(targets)}：正在审校《{chapter.get('title', '')}》",
            )
            if len(original) < 100:
                issue = {
                    "severity": "high", "category": "正文缺失",
                    "message": "章节正文不足 100 字，无法在保留事实的前提下精修。",
                    "suggestion": "先让 AI 按章节计划生成完整正文。",
                }
                enqueue_repair(project, chapter, issue, source="auto-refine")
                task.setdefault("quality_debts", []).append(
                    {"chapter": chapter_index + 1, "chapter_id": chapter_id,
                     "title": chapter.get("title", ""), "score": 0, "issues": [issue]}
                )
                project = store.save(project["id"], project, reason=f"refine-chapter-{chapter_index + 1}-skipped")
                task["refine_index"] = position + 1
                task["completed_chapters"] = position + 1
                task = _save_director_task(task, f"第 {chapter_index + 1} 章正文不足，已保留并加入待办", "warning")
                continue
    
            draft = original
            audit: dict[str, Any] = {}
            contract_result: dict[str, Any] = {}
            passed = False
            used_attempts = 0
            clean_original = _dedupe_exact_paragraphs(
                _dedupe_adjacent_sentence_blocks(original)
            )
            original_chars = _prose_char_count(original)
            clean_chars = _prose_char_count(clean_original)
            if clean_chars < int(original_chars * 0.75):
                # A loop-inflated chapter must not be asked to regenerate all of the
                # duplicated length. Preserve the substantive first copy and allow a
                # modest expansion for transitions and missing scene texture.
                target_chars = max(300, min(5000, int(clean_chars * 1.25)))
            else:
                target_chars = max(300, min(5000, original_chars))
            task["config"]["target_words"] = target_chars
            best_draft = original
            best_audit: dict[str, Any] = {}
            best_contract: dict[str, Any] = {}
            best_rank = (-1, -10_000, -1)
            original_score = 0
            for attempt in range(int(config["max_revision_attempts"]) + 1):
                audit_project = deepcopy(project)
                audit_project.setdefault("settings", {})["target_words"] = target_chars
                audit = await chapter_audit(
                    ChapterActionRequest(
                        project=audit_project, chapter_id=chapter_id, draft=draft,
                        instruction="AI 全自动精修：核对连续性、章节目标、人物状态、语言质量和跨章重复。",
                    )
                )
                contract_result = scan_contracts(project, chapter, draft, chapter_index + 1)
                if not contract_result.get("passed", False):
                    violations = [
                        {
                            "severity": str(item.get("severity", "high")),
                            "category": "硬契约",
                            "message": str(item.get("message", "硬契约未满足")),
                            "suggestion": "严格满足硬契约，同时保留原有情节事实。",
                            "evidence": item.get("evidence", []),
                            "evidence_verified": True,
                        }
                        for item in contract_result.get("violations", [])
                        if isinstance(item, dict)
                    ]
                    audit.setdefault("issues", []).extend(violations)
                    audit["score"] = min(int(audit.get("score", 0) or 0), 74)
                    audit["verdict"] = "revise"
                score = int(audit.get("score", 0) or 0)
                if attempt == 0:
                    original_score = score
                candidate_rank = _refinement_candidate_rank(
                    draft, audit, contract_result,
                    int(config["quality_threshold"]), target_chars,
                )
                if candidate_rank > best_rank:
                    best_rank = candidate_rank
                    best_draft = draft
                    best_audit = deepcopy(audit)
                    best_contract = deepcopy(contract_result)
                task["current_audit"] = {
                    "chapter": chapter_index + 1, "chapter_id": chapter_id,
                    "score": score, "verdict": str(audit.get("verdict", "review")),
                    "attempt": attempt + 1, "updated_at": utc_now(),
                }
                task = _save_director_task(
                    task,
                    f"《{chapter.get('title', '')}》第 {attempt + 1} 次审校完成：{score} 分",
                    "success" if audit.get("verdict") == "pass" else "warning",
                )
                passed = _refinement_candidate_passes(
                    audit, contract_result, int(config["quality_threshold"])
                )
                if passed or attempt >= int(config["max_revision_attempts"]):
                    used_attempts = attempt
                    break
                issues = _audit_issues(audit)
                requirements = "\n".join(
                    f"{number}. [{item.get('category', '问题')}] {item.get('message', '')}；建议：{item.get('suggestion', '')}"
                    for number, item in enumerate(issues, start=1)
                ) or str(audit.get("revision_brief", "提高本章连续性与语言质量"))
                extra = str(config.get("instruction", "")).strip()
                task = _save_director_task(
                    task, f"《{chapter.get('title', '')}》未达 {config['quality_threshold']} 分，正在自动修订"
                )
                inherited = task.get("seed_candidates", {}).get(chapter_id, {})
                inherited_draft = (
                    str(inherited.get("draft", "")).strip()
                    if isinstance(inherited, dict)
                    else ""
                )
                if attempt == 0 and len(inherited_draft) >= 100 and inherited_draft != original:
                    draft = inherited_draft
                    task["refinement_checkpoint"] = {
                        "chapter_id": chapter_id,
                        "position": position + 1,
                        "draft": draft[:30000],
                        "attempt": attempt + 1,
                        "saved_at": utc_now(),
                        "inherited": True,
                    }
                    used_attempts = attempt + 1
                    task = _save_director_task(
                        task,
                        f"《{chapter.get('title', '')}》已继承上次最佳候选（{inherited.get('score', 0)} 分），继续局部精修",
                        "success",
                    )
                    continue
                revision_seed = _dedupe_exact_paragraphs(
                    _dedupe_adjacent_sentence_blocks(best_draft)
                )
                if revision_seed != best_draft:
                    task = _save_director_task(
                        task,
                        f"《{chapter.get('title', '')}》已先清理确定性重复，再交给 AI 补写精修",
                        "success",
                    )
                draft = await _director_patch_refinement_excerpts(
                    project, chapter, revision_seed, best_audit or audit, target_chars
                )
                if draft != revision_seed:
                    task = _save_director_task(
                        task,
                        f"《{chapter.get('title', '')}》仅剩局部问题，已做定点修补并保持其余正文不变",
                        "success",
                    )
                else:
                    draft = await _director_generate_prose(
                        task, project, chapter, mode="rewrite", selection=revision_seed,
                        instruction=(
                            "对整章做精修，只修复列出的问题。保留原文全部既定事实、人物关系、"
                            "事件顺序、叙事视角、伏笔与章末功能；禁止新增背景设定，禁止解释修改过程。\n"
                            "如果输入中有重复段落或循环句，删除重复副本后用新的现场动作、感官细节和人物反应补足篇幅；"
                            "不得换词复述已经发生的同一动作。\n"
                            + (f"作者额外要求：{extra}\n" if extra else "")
                            + requirements
                            + "\n只输出完整修订稿。"
                        ),
                    )
                draft = _dedupe_exact_paragraphs(
                    _dedupe_adjacent_sentence_blocks(draft)
                )
                task["refinement_checkpoint"] = {
                    "chapter_id": chapter_id, "position": position + 1,
                    "draft": draft[:30000], "attempt": attempt + 1,
                    "saved_at": utc_now(),
                }
                used_attempts = attempt + 1
                task = _save_director_task(task, f"《{chapter.get('title', '')}》第 {attempt + 1} 版修订稿已保存", "success")
    
            draft = best_draft
            audit = best_audit or audit
            contract_result = best_contract or contract_result
            passed = _refinement_candidate_passes(
                audit, contract_result, int(config["quality_threshold"])
            )
            gate_failures = _director_candidate_gate_failures(draft, target_chars)
            if passed and not gate_failures:
                if str(audit.get("verdict", "")) != "pass":
                    audit = deepcopy(audit)
                    audit["model_gate_verdict"] = str(audit.get("verdict", "revise"))
                    audit["verdict"] = "pass"
                    audit.setdefault("score_components", {})["threshold"] = int(
                        config["quality_threshold"]
                    )
                changed = draft.strip() != original
                chapter["content"] = draft.strip()
                chapter["authority_state"] = "accepted"
                lock_chapter(chapter, actor="auto-refine", audit=audit, contract_scan=contract_result)
                _resolve_project_repairs(project, chapter_id)
                memory_warnings: list[str] = []
                if changed or str(chapter.get("memory_status", "")) != "committed":
                    try:
                        memory = await chapter_memory(
                            ChapterActionRequest(
                                project=project, chapter_id=chapter_id,
                                draft=draft, instruction="AI 精修通过后的状态与长期记忆回灌",
                            )
                        )
                        memory_warnings = list(memory.get("warnings", [])) if isinstance(memory, dict) else []
                        _apply_director_memory(project, chapter, memory)
                    except Exception as exc:
                        chapter["memory_status"] = "pending_rebuild"
                        memory_warnings = [f"精修正文已锁定，但记忆回灌待重试：{planning_exception_detail(exc)}"]
                        enqueue_repair(
                            project, chapter,
                            {"category": "记忆回灌", "severity": "medium", "message": memory_warnings[0]},
                            source="auto-refine",
                        )
                _remove_quality_debt(task, chapter_id)
                run_record = {
                    "status": "accepted", "last_run_at": utc_now(),
                    "model": str(project.get("settings", {}).get("model", "")),
                    "target_words": target_chars, "audit_score": int(audit.get("score", 0)),
                    "audit_verdict": str(audit.get("verdict", "pass")),
                    "revision_attempts": used_attempts, "issues": _audit_issues(audit),
                    "warnings": memory_warnings, "authority_state": chapter.get("authority_state", "locked"),
                    "contract_scan": contract_result, "refined_by_ai": True,
                }
                chapter["execution"] = run_record
                chapter.setdefault("run_history", []).append(deepcopy(run_record))
                chapter["run_history"] = chapter["run_history"][-10:]
                project = store.save(project["id"], project, reason=f"auto-refine-chapter-{chapter_index + 1}")
                task = _save_director_task(
                    task,
                    f"《{chapter.get('title', '')}》精修通过并已锁定（{audit.get('score', 0)} 分）",
                    "success",
                )
            else:
                issues = _audit_issues(audit)
                if gate_failures:
                    issues.extend(
                        {"severity": "high", "category": "正文安全门", "message": item,
                         "suggestion": "重新生成完整修订稿。"}
                        for item in gate_failures
                    )
                debt = {
                    "chapter": chapter_index + 1, "chapter_id": chapter_id,
                    "title": chapter.get("title", ""), "score": int(audit.get("score", 0) or 0),
                    "issues": issues,
                }
                debt_index = _quality_debt_index(task, chapter_id)
                if debt_index >= 0:
                    task["quality_debts"][debt_index] = debt
                else:
                    task.setdefault("quality_debts", []).append(debt)
                for issue in issues:
                    enqueue_repair(project, chapter, issue, source="auto-refine")
                task["last_rejected_candidate"] = {
                    **debt, "draft": draft[:30000], "rejected_at": utc_now(),
                }
                task.setdefault("refinement_candidates", {})[chapter_id] = {
                    **debt, "draft": draft[:30000], "saved_at": utc_now(),
                }
                project = store.save(project["id"], project, reason=f"auto-refine-chapter-{chapter_index + 1}-review")
                task = _save_director_task(
                    task,
                    f"《{chapter.get('title', '')}》自动精修后仍未达标，原正文保持不变",
                    "warning",
                )
            task.setdefault("refinement_results", []).append(
                {
                    "chapter": chapter_index + 1,
                    "chapter_id": chapter_id,
                    "title": chapter.get("title", ""),
                    "status": "accepted" if passed and not gate_failures else "review",
                    "original_score": original_score,
                    "best_score": int(audit.get("score", 0) or 0),
                    "revision_attempts": used_attempts,
                    "updated_at": utc_now(),
                }
            )
            task["refinement_results"] = task["refinement_results"][-300:]
            task.pop("refinement_checkpoint", None)
            task.pop("current_audit", None)
            task["refine_index"] = position + 1
            task["completed_chapters"] = position + 1
            task = store.save_director_task(task["id"], task)
    
        project = store.get(task["project_id"])
        final_health = manuscript_health_report(project)
        release_failures = _director_manuscript_gate_failures(
            final_health, final=True, genre=str(project.get("genre", ""))
        )
        task["latest_manuscript_health"] = final_health
        task["release_score"] = int(final_health.get("score", 0))
        task["release_failures"] = release_failures
        task["release_ready"] = not release_failures and not task.get("quality_debts")
        task["release_status"] = "ready" if task["release_ready"] else "needs_revision"
        task["phase"] = "completed"
        task["status"] = "completed"
        _save_director_task(
            task,
            (
                f"AI 全书精修完成，{len(targets)} 章已通过复核，可以导出"
                if task["release_ready"]
                else f"AI 全书精修完成；仍有 {len(task.get('quality_debts', []))} 章需要复核"
            ),
            "success" if task["release_ready"] else "warning",
        )
    
    
    async def _run_incubation_task(
        task: dict[str, Any], project: dict[str, Any]
    ) -> None:
        config = task.get("config", {})
        seed = str(config.get("seed", "")).strip()
        preferences = str(config.get("preferences", "")).strip()
        story_mode = "short" if config.get("story_mode") == "short" else "long"
        target = max(3, min(300, int(config.get("target_chapters", 30) or 30)))
    
        core_options = task.get("incubation_core_options")
        if not isinstance(core_options, list) or len(core_options) != 2:
            task["incubation_step"] = "directions"
            task = _save_director_task(
                task, "灵感孵化 1/3：正在构思两套不同的作品方向"
            )
            core_options, warnings = await _generate_incubator_core(
                project, seed, preferences, story_mode, target
            )
            task["incubation_core_options"] = core_options
            task.setdefault("warnings", []).extend(warnings)
            task["incubation_step"] = "option_1"
            task = _save_director_task(
                task, "两套故事方向已保存；开始完善第一套人物和世界设定", "success"
            )
    
        options = task.get("incubation_options")
        if not isinstance(options, list):
            options = []
        if len(options) > len(core_options):
            options = options[: len(core_options)]
        for index in range(len(options), len(core_options)):
            task["incubation_step"] = f"option_{index + 1}"
            task = _save_director_task(
                task,
                f"灵感孵化 {index + 2}/3：正在完善第 {index + 1} 套方案的人物与世界",
            )
            option, warnings = await _complete_incubator_option(
                project,
                core_options[index],
                index=index + 1,
                seed=seed,
                preferences=preferences,
                target=target,
            )
            options.append(option)
            task["incubation_options"] = options
            task.setdefault("warnings", []).extend(warnings)
            task = _save_director_task(
                task, f"第 {index + 1} 套完整方案已保存", "success"
            )
    
        result = {"options": options}
        validate_incubator_result(
            result, target_chapters=target, story_mode=story_mode
        )
        task["incubation_options"] = options
        task["result"] = {
            "options": options,
            "warnings": task.get("warnings", []),
            "generation_mode": "resumable_background_task",
            "fallback": False,
        }
        task["incubation_step"] = "completed"
        task["phase"] = "completed"
        task["status"] = "completed"
        _save_director_task(
            task, "两套完整作品方案已生成，可以选择创建新作品", "success"
        )
    
    
    async def _run_auto_director(task_id: str) -> None:
        task = store.get_director_task(task_id)
        if not task:
            return
        if not store.claim_director_task(task_id, runtime_instance_id):
            return

        async def keep_lease_alive() -> None:
            while True:
                await asyncio.sleep(15)
                if not store.renew_director_task_lease(
                    task_id, runtime_instance_id
                ):
                    return

        lease_heartbeat = asyncio.create_task(keep_lease_alive())
        try:
            task["status"] = "running"
            task = _save_director_task(task, "自动导演已启动")
            config = task["config"]
            project = store.get(task["project_id"])
            if not project:
                raise ValueError("自动导演作品已被删除")
    
            if task.get("task_type") == "incubation":
                await _run_incubation_task(task, project)
                return
    
            if task.get("phase") == "refinement":
                await _run_auto_refinement(task, project)
                return
    
            if task.get("phase") == "incubator":
                brief = task.get("seed_brief")
                if not isinstance(brief, dict):
                    task["incubator_step"] = "brief"
                    task = _save_director_task(
                        task, "灵感开书 1/4：正在生成单套故事骨架"
                    )
                    brief, brief_warnings = await director_seed_brief(
                        project, config
                    )
                    task["seed_brief"] = brief
                    task["incubator_step"] = "cast"
                    for warning in brief_warnings:
                        _director_event(task, warning, "warning")
                    task = _save_director_task(
                        task,
                        "故事骨架已保存；下一步生成精简人物名单",
                        "success",
                    )
                cast_result = task.get("seed_cast")
                if not isinstance(cast_result, dict):
                    task["incubator_step"] = "cast"
                    task = _save_director_task(
                        task, "灵感开书 2/4：正在确定 3-5 名主要人物"
                    )
                    cast_result, cast_warnings = await director_seed_cast(
                        project, config, brief
                    )
                    task["seed_cast"] = cast_result
                    task["incubator_step"] = "characters"
                    for warning in cast_warnings:
                        _director_event(task, warning, "warning")
                    task = _save_director_task(
                        task, "主要人物名单已保存；开始逐人建立人物卡", "success"
                    )
                cast = cast_result.get("characters", [])
    
                cards = task.get("seed_character_cards")
                if not isinstance(cards, list):
                    cards = []
                if len(cards) > len(cast):
                    cards = cards[: len(cast)]
                for index in range(len(cards), len(cast)):
                    target = cast[index]
                    task["incubator_step"] = "characters"
                    task = _save_director_task(
                        task,
                        f"灵感开书 3/4：正在生成人物卡 {index + 1}/{len(cast)}《{target.get('name', '')}》",
                    )
                    card, card_warnings = await director_character_card(
                        project, config, brief, cast, target
                    )
                    cards.append(card)
                    task["seed_character_cards"] = cards
                    for warning in card_warnings:
                        _director_event(task, warning, "warning")
                    task = _save_director_task(
                        task,
                        f"人物卡《{card.get('name', '')}》已保存（{len(cards)}/{len(cast)}）",
                        "success",
                    )
    
                world_result = task.get("seed_world")
                if not isinstance(world_result, dict):
                    task["incubator_step"] = "world"
                    task = _save_director_task(
                        task, "灵感开书 4/4：正在生成开篇必要世界书"
                    )
                    world_result, world_warnings = await director_seed_world(
                        project, config, brief, cast
                    )
                    task["seed_world"] = world_result
                    for warning in world_warnings:
                        _director_event(task, warning, "warning")
                    task = _save_director_task(task, "世界书已保存", "success")
    
                assets = {
                    "characters": cards,
                    "world_entries": world_result.get("world_entries", []),
                }
                task["seed_assets"] = assets
                task = _save_director_task(task)
                option = {
                    **brief,
                    "outline": str(brief.get("story_spine", "")),
                    "target_chapters": int(config.get("target_chapters", 30)),
                    "characters": assets.get("characters", []),
                    "world_entries": assets.get("world_entries", []),
                }
                project = _director_project_from_option(project, option, config)
                project = store.save(project["id"], project, reason="director-incubator")
                task["phase"] = "master"
                task["incubator_step"] = "completed"
                task["selected_direction"] = {key: option.get(key, "") for key in ("title", "genre", "positioning", "premise")}
                task = _save_director_task(task, f"已采用推荐方向《{project['title']}》，开始全书规划", "success")
    
            if task.get("phase") == "master":
                project = store.get(task["project_id"])
                target = int(project.get("narrative", {}).get("target_chapters", config.get("target_chapters", 30)))
                configured_volumes = config.get("preferred_volume_count")
                requested_volumes = (
                    min(target, int(configured_volumes))
                    if configured_volumes
                    else 1 if target <= 6 else min(12, max(2, math.ceil(target / 12)))
                )
                specs = _director_volume_specs(target, requested_volumes)
    
                bible = task.get("master_bible")
                if not isinstance(bible, dict):
                    task["master_step"] = "bible"
                    task = _save_director_task(task, "全书规划 1/3：正在建立故事圣经")
                    bible, bible_warnings = await director_master_bible(
                        project, "自动导演模式：建立可直接分卷执行的故事圣经。"
                    )
                    task["master_bible"] = bible
                    task["master_step"] = "contracts"
                    for warning in bible_warnings:
                        _director_event(task, warning, "warning")
                    task = _save_director_task(task, "全书故事圣经已保存", "success")
    
                contracts = task.get("master_contracts")
                if not isinstance(contracts, list):
                    contracts = []
                if len(contracts) > len(specs):
                    contracts = contracts[: len(specs)]
                for index in range(len(contracts), len(specs)):
                    task["master_step"] = "contracts"
                    task["current_volume"] = index + 1
                    task["total_volumes"] = len(specs)
                    task = _save_director_task(
                        task,
                        f"全书规划 2/3：正在建立第 {index + 1}/{len(specs)} 卷因果契约",
                    )
                    contract, contract_warnings = await director_master_contract(
                        project, bible, specs, index, contracts
                    )
                    contracts.append(contract)
                    task["master_contracts"] = contracts
                    for warning in contract_warnings:
                        _director_event(task, warning, "warning")
                    task = _save_director_task(
                        task,
                        f"第 {index + 1} 卷因果契约已保存（{len(contracts)}/{len(specs)}）",
                        "success",
                    )
                task["master_step"] = "volumes"
                task = _save_director_task(task, "全部分卷契约已保存；开始逐卷扩写", "success")
    
                expanded = task.get("master_volumes")
                if not isinstance(expanded, list):
                    expanded = []
                if len(expanded) > len(specs):
                    expanded = expanded[: len(specs)]
                for index in range(len(expanded), len(specs)):
                    task["master_step"] = "volumes"
                    task["current_volume"] = index + 1
                    task["total_volumes"] = len(specs)
                    saved_core = task.get("master_volume_core")
                    if not isinstance(saved_core, dict) or int(saved_core.get("index", -1)) != index:
                        task.pop("master_volume_core", None)
                        task = _save_director_task(
                            task,
                            f"全书规划 3/3：正在生成第 {index + 1}/{len(specs)} 卷剧情梗概《{contracts[index].get('title', '')}》",
                        )
                        core, core_warnings = await director_master_volume_core(
                            project, bible, contracts, specs, index, expanded
                        )
                        task["master_volume_core"] = {"index": index, "data": core}
                        for warning in core_warnings:
                            _director_event(task, warning, "warning")
                        task = _save_director_task(
                            task,
                            f"第 {index + 1} 卷剧情梗概核心已保存；继续生成执行清单",
                            "success",
                        )
                    else:
                        core = saved_core.get("data", {})
                    task = _save_director_task(
                        task,
                        f"全书规划 3/3：正在生成第 {index + 1}/{len(specs)} 卷转折与人物弧",
                    )
                    details, detail_warnings = await director_master_volume_details(
                        project, bible, contracts, specs, index, core
                    )
                    contract_fields = {
                        key: contracts[index].get(key, "")
                        for key in (
                            "theme_test", "primary_arena", "time_span",
                            "irreversible_change", "character_choice", "new_story_question",
                        )
                    }
                    volume = {
                        **core,
                        **details,
                        **contract_fields,
                        "chapter_count": specs[index]["chapter_count"],
                    }
                    validate_director_volume_expansion({"volume": volume})
                    expanded.append(volume)
                    task["master_volumes"] = expanded
                    task.pop("master_volume_core", None)
                    for warning in detail_warnings:
                        _director_event(task, warning, "warning")
                    task = _save_director_task(
                        task,
                        f"第 {index + 1} 卷详细蓝图已保存（{len(expanded)}/{len(specs)}）",
                        "success",
                    )
    
                raw_planning = {
                    **bible,
                    "full_outline": _director_full_outline(expanded),
                    "volumes": expanded,
                }
                outline_target = 700 if target <= 12 else 1200 if target <= 30 else 1800 if target <= 60 else 2600 if target <= 120 else 3200
                validate_master_result(
                    raw_planning, requested_volumes, max(500, int(outline_target * 0.7)), target
                )
                planning = normalize_master_plan(raw_planning, target)
                planning["fallback"] = False
                planning["warnings"] = []
                project["planning"] = planning
                if planning.get("master", {}).get("full_outline"):
                    project["outline"] = planning["master"]["full_outline"]
                project = store.save(project["id"], project, reason="director-master-plan")
                task["phase"] = "volumes"
                task["volume_index"] = 0
                task["total_volumes"] = len(planning.get("volumes", []))
                task["master_step"] = "completed"
                task = _save_director_task(task, f"全书规划完成，共 {task['total_volumes']} 卷", "success")
    
            if task.get("phase") == "volumes":
                project = store.get(task["project_id"])
                volumes = project.get("planning", {}).get("volumes", [])
                for index in range(int(task.get("volume_index", 0)), len(volumes)):
                    volume = volumes[index]
                    task["current_volume"] = index + 1
                    expected = int(volume["chapter_end"]) - int(volume["chapter_start"]) + 1
                    checkpoints = task.get("route_checkpoints")
                    if not isinstance(checkpoints, dict):
                        checkpoints = {}
                    existing = volume.get("chapters", [])
                    if isinstance(existing, list) and len(existing) == expected:
                        valid_count, checkpoint_issue = _audit_route_checkpoint_prefix(
                            project, volume, existing
                        )
                        if valid_count == expected:
                            project = ensure_project_defaults(
                                apply_volume_routes(project, volume["id"])
                            )
                            project = store.save(
                                project["id"],
                                project,
                                reason=f"director-volume-{index + 1}-reapplied",
                            )
                            task["volume_index"] = index + 1
                            task = _save_director_task(
                                task,
                                f"检测到第 {index + 1} 卷路线完整且通过复核，已重新同步章节后继续",
                                "warning",
                            )
                            continue
                        checkpoints[volume["id"]] = existing[:valid_count]
                        project["planning"]["volumes"][index]["chapters"] = []
                        task["route_checkpoints"] = checkpoints
                        task.setdefault("route_rejected_history", []).append(
                            {
                                "time": utc_now(), "volume_id": volume["id"],
                                "from_chapter": int(volume["chapter_start"]) + valid_count,
                                "count": expected - valid_count, "reason": checkpoint_issue,
                            }
                        )
                        task["route_rejected_history"] = task["route_rejected_history"][-30:]
                        task = _save_director_task(
                            task,
                            f"第 {index + 1} 卷旧路线复核发现问题：{checkpoint_issue}；"
                            f"已保留前 {valid_count} 条并从下一条自动重建",
                            "warning",
                        )
                    routes = checkpoints.get(volume["id"])
                    if not isinstance(routes, list):
                        routes = []
                    if len(routes) > expected:
                        routes = routes[:expected]
                    valid_count, checkpoint_issue = _audit_route_checkpoint_prefix(
                        project, volume, routes
                    )
                    if valid_count < len(routes):
                        rejected_count = len(routes) - valid_count
                        task.setdefault("route_rejected_history", []).append(
                            {
                                "time": utc_now(), "volume_id": volume["id"],
                                "from_chapter": int(volume["chapter_start"]) + valid_count,
                                "count": rejected_count, "reason": checkpoint_issue,
                            }
                        )
                        task["route_rejected_history"] = task["route_rejected_history"][-30:]
                        routes = routes[:valid_count]
                        checkpoints[volume["id"]] = routes
                        task["route_checkpoints"] = checkpoints
                        task = _save_director_task(
                            task,
                            f"恢复前复核发现旧检查点问题：{checkpoint_issue}；已保留前 "
                            f"{valid_count} 条，从第 {valid_count + 1} 条自动重建",
                            "warning",
                        )
                    for chapter_number in range(
                        int(volume["chapter_start"]) + len(routes),
                        int(volume["chapter_end"]) + 1,
                    ):
                        task["route_volume_index"] = index + 1
                        task["route_chapter_number"] = chapter_number
                        task["route_completed"] = len(routes)
                        task["route_total"] = expected
                        task = _save_director_task(
                            task,
                            f"正在拆解第 {index + 1}/{len(volumes)} 卷《{volume.get('title', '')}》：第 {len(routes) + 1}/{expected} 条章节路线",
                        )
                        route, route_warnings = await director_plan_chapter_route(
                            project, volume, chapter_number, routes
                        )
                        routes.append(route)
                        checkpoints[volume["id"]] = routes
                        task["route_checkpoints"] = checkpoints
                        task["route_completed"] = len(routes)
                        route_issues = [
                            str(item) for item in route.get("quality_warnings", [])
                            if str(item).strip()
                        ]
                        if route_issues:
                            _record_planning_debt(
                                task,
                                {
                                    "phase": "route", "volume_id": volume["id"],
                                    "volume": index + 1, "chapter": chapter_number,
                                    "title": route.get("title", ""), "issues": route_issues,
                                },
                            )
                        for warning in route_warnings:
                            _director_event(task, warning, "warning")
                        task = _save_director_task(
                            task,
                            f"第 {chapter_number} 章路线《{route.get('title', '')}》已保存（{len(routes)}/{expected}）",
                            "success",
                        )
                    project["planning"]["volumes"][index]["chapters"] = routes
                    project = ensure_project_defaults(apply_volume_routes(project, volume["id"]))
                    project = store.save(project["id"], project, reason=f"director-volume-{index + 1}")
                    checkpoints.pop(volume["id"], None)
                    task["route_checkpoints"] = checkpoints
                    task["volume_index"] = index + 1
                    task = _save_director_task(task, f"第 {index + 1} 卷已拆解并建立章节", "success")
                task["phase"] = "chapters"
                task["chapter_index"] = int(task.get("chapter_index", 0))
                task["total_chapters"] = len(project.get("chapters", []))
                task = _save_director_task(task, f"分卷与逐章路线完成，开始创作 {task['total_chapters']} 章正文", "success")
    
            if task.get("phase") == "chapters":
                project = store.get(task["project_id"])
                chapters = project.get("chapters", [])
                for index in range(int(task.get("chapter_index", 0)), len(chapters)):
                    chapter = project["chapters"][index]
                    task["current_chapter"] = index + 1
                    task = _save_director_task(task, f"第 {index + 1}/{len(chapters)} 章：正在细化《{chapter.get('title', '')}》")
                    if chapter.get("content", "").strip():
                        if chapter.get("execution", {}).get("status") == "memory_pending":
                            draft = str(chapter["content"])
                            task = _save_director_task(
                                task, f"第 {index + 1} 章正文已保存，正在补全剧情记忆"
                            )
                            memory = await _director_chapter_memory(project, chapter, draft)
                            _apply_director_memory(project, chapter, memory)
                            chapter["memory_status"] = "committed"
                            chapter["execution"]["status"] = "accepted"
                            chapter["execution"]["warnings"] = list(memory.get("warnings", []))
                            project = store.save(
                                project["id"], project,
                                reason=f"director-chapter-{index + 1}-memory-recovered",
                            )
                            task.pop("chapter_draft_checkpoint", None)
                            task["completed_chapters"] = index + 1
                            task = _save_director_task(
                                task, f"第 {index + 1} 章剧情记忆已补全", "success"
                            )
                        if (
                            str(chapter.get("execution", {}).get("status", ""))
                            == "accepted"
                            and _remove_quality_debt(task, str(chapter.get("id", "")))
                        ):
                            task = _save_director_task(
                                task,
                                f"第 {index + 1} 章已通过复审，旧质量债务已清除",
                                "success",
                            )
                        task["chapter_index"] = index + 1
                        task = _save_director_task(task, "检测到已有正文，本章保持不变并跳过", "warning")
                        continue
                    plan = await chapter_plan(
                        ChapterActionRequest(project=project, chapter_id=chapter["id"], instruction="自动导演模式：服从上层路线和已发生事实，只规划本章。")
                    )
                    if plan.get("fallback"):
                        issues = [str(item) for item in plan.get("warnings", []) if str(item).strip()]
                        _record_planning_debt(
                            task,
                            {
                                "phase": "chapter_plan", "chapter": index + 1,
                                "chapter_id": chapter.get("id", ""),
                                "title": chapter.get("title", ""),
                                "issues": issues or ["AI 单章细化失败，已使用上层章节路线继续"],
                            },
                        )
                        _director_event(
                            task,
                            f"第 {index + 1} 章 AI 细化未完成，已使用上层路线继续并记录规划债务",
                            "warning",
                        )
                    chapter["plan"] = plan
                    chapter["scene_goal"] = plan.get("goal", chapter.get("scene_goal", ""))
                    project = store.save(project["id"], project, reason=f"director-chapter-{index + 1}-plan")
                    chapter = project["chapters"][index]
                    draft_checkpoint = task.get("chapter_draft_checkpoint", {})
                    if (
                        isinstance(draft_checkpoint, dict)
                        and str(draft_checkpoint.get("chapter_id", ""))
                        == str(chapter.get("id", ""))
                        and len(str(draft_checkpoint.get("draft", "")).strip()) >= 100
                    ):
                        draft = str(draft_checkpoint["draft"]).strip()
                        draft, checkpoint_repair_note = _trim_incomplete_prose_tail(draft)
                        if checkpoint_repair_note:
                            task["chapter_draft_checkpoint"] = {
                                **draft_checkpoint,
                                "draft": draft[:30000],
                                "repaired_at": utc_now(),
                                "repair_note": checkpoint_repair_note,
                            }
                            task["last_rejected_candidate"] = {}
                            task = _save_director_task(
                                task,
                                f"第 {index + 1} 章检查点已自动修复：{checkpoint_repair_note}；现在重新审计",
                                "success",
                            )
                        if _prose_looks_truncated(draft):
                            task.pop("chapter_draft_checkpoint", None)
                            task["last_rejected_candidate"] = {}
                            task = _save_director_task(
                                task,
                                f"第 {index + 1} 章旧检查点无法安全补齐，已自动作废并重新生成正文",
                                "warning",
                            )
                            draft = await _director_generate_prose(
                                task,
                                project,
                                chapter,
                                instruction=(
                                    "旧候选稿曾在半句中截断，已经作废。请从零生成完整本章，"
                                    "严格执行本章计划并以完整句子自然收束，只输出小说正文。"
                                ),
                            )
                            task["chapter_draft_checkpoint"] = {
                                "chapter": index + 1,
                                "chapter_id": chapter.get("id", ""),
                                "draft": draft[:30000],
                                "revision_attempt": 0,
                                "saved_at": utc_now(),
                                "regenerated_from_invalid_checkpoint": True,
                            }
                            task = _save_director_task(
                                task,
                                f"第 {index + 1} 章重新生成的正文已保存到检查点，开始审计",
                                "success",
                            )
                        else:
                            task = _save_director_task(
                                task,
                                f"第 {index + 1} 章：已从正文检查点恢复，直接继续审计",
                                "success",
                            )
                    else:
                        task = _save_director_task(task, f"第 {index + 1} 章：正在生成正文")
                        draft = await _director_generate_prose(
                            task, project, chapter,
                            instruction="严格执行本章计划，从具体场景起笔，完成本章目标、冲突、转折和结尾推动力。只输出小说正文。",
                        )
                        task["chapter_draft_checkpoint"] = {
                            "chapter": index + 1,
                            "chapter_id": chapter.get("id", ""),
                            "draft": draft[:30000],
                            "revision_attempt": 0,
                            "saved_at": utc_now(),
                        }
                        task = _save_director_task(
                            task,
                            f"第 {index + 1} 章正文已保存到检查点，开始审计",
                            "success",
                        )
                    audit: dict[str, Any] = {}
                    revision_attempt_limit = min(
                        int(config["max_revision_attempts"]),
                        max(
                            0,
                            int(
                                project.get("settings", {}).get(
                                    "director_revision_attempt_limit",
                                    config["max_revision_attempts"],
                                )
                            ),
                        ),
                    )
                    for attempt in range(revision_attempt_limit + 1):
                        task = _save_director_task(task, f"第 {index + 1} 章：正在进行连续性与质量审计（第 {attempt + 1} 次）")
                        audit = await chapter_audit(
                            ChapterActionRequest(project=project, chapter_id=chapter["id"], draft=draft, instruction="自动导演整章审计")
                        )
                        audit_score = max(0, min(100, int(audit.get("score", 0) or 0)))
                        existing_debt = _quality_debt_index(task, str(chapter.get("id", "")))
                        if existing_debt >= 0:
                            task["quality_debts"][existing_debt] = {
                                "chapter": index + 1,
                                "chapter_id": chapter.get("id", ""),
                                "title": chapter.get("title", ""),
                                "score": audit_score,
                                "issues": _audit_issues(audit),
                            }
                        task["current_audit"] = {
                            "chapter": index + 1,
                            "chapter_id": chapter.get("id", ""),
                            "score": audit_score,
                            "verdict": str(audit.get("verdict", "review")),
                            "attempt": attempt + 1,
                            "updated_at": utc_now(),
                        }
                        task = _save_director_task(
                            task,
                            f"第 {index + 1} 章第 {attempt + 1} 次审计完成：{audit_score} 分",
                            "success" if audit.get("verdict") == "pass" else "warning",
                        )
                        if _refinement_candidate_passes(
                            audit, {}, int(config["quality_threshold"])
                        ):
                            _remove_quality_debt(task, str(chapter.get("id", "")))
                            break
                        if attempt >= revision_attempt_limit:
                            break
                        issues = _audit_issues(audit)
                        task["quality_directives"] = _quality_directives(audit)
                        requirements = "\n".join(
                            f"{number}. [{item.get('category', '问题')}] {item.get('message', '')}；建议：{item.get('suggestion', '')}"
                            for number, item in enumerate(issues, start=1)
                        ) or str(audit.get("revision_brief", "修复审计发现的问题"))
                        task = _save_director_task(task, f"第 {index + 1} 章：审计未通过，正在自动修订")
                        systemic = _systemic_quality_issues(audit)
                        if systemic:
                            draft = await _director_generate_prose(
                                task,
                                project,
                                chapter,
                                mode="instruction",
                                instruction=(
                                    "上一候选存在结构性重复或阶段偏离，必须废弃原候选，从本章路线重新设计不同的场景动作链。"
                                    "保留上层规定的章末新状态，但不得沿用原候选的段落、句式、对话问答或意象。\n"
                                    + requirements
                                ),
                            )
                        else:
                            draft = await _director_generate_prose(
                                task, project, chapter, mode="rewrite", selection=draft,
                                instruction=f"完整修订候选正文并解决以下问题：\n{requirements}\n保留未被指出的问题、人物关系、事件顺序和结尾功能，只输出完整修订稿。",
                            )
                        task["chapter_draft_checkpoint"] = {
                            "chapter": index + 1,
                            "chapter_id": chapter.get("id", ""),
                            "draft": draft[:30000],
                            "revision_attempt": attempt + 1,
                            "saved_at": utc_now(),
                        }
                        task = _save_director_task(
                            task,
                            f"第 {index + 1} 章第 {attempt + 1} 版修订稿已保存到检查点",
                            "success",
                        )
                    contract_result = scan_contracts(project, chapter, draft, index + 1)
                    if not contract_result.get("passed", False):
                        contract_issues = [
                            {
                                "severity": str(item.get("severity", "high")),
                                "category": "硬契约",
                                "message": str(item.get("message", "硬契约未满足")),
                                "suggestion": "按契约要求修订本章后重新扫描。",
                                "evidence": item.get("evidence", []),
                                "evidence_verified": True,
                            }
                            for item in contract_result.get("violations", [])
                            if isinstance(item, dict)
                        ]
                        audit.setdefault("issues", []).extend(contract_issues)
                        audit["score"] = min(int(audit.get("score", 0)), 74)
                        audit["verdict"] = "revise"
                    passed = _refinement_candidate_passes(
                        audit, contract_result, int(config["quality_threshold"])
                    )
                    if not passed:
                        debt = {"chapter": index + 1, "chapter_id": chapter.get("id", ""), "title": chapter.get("title", ""), "score": int(audit.get("score", 0)), "issues": _audit_issues(audit)}
                        debts = task.setdefault("quality_debts", [])
                        debt_index = _quality_debt_index(task, str(chapter.get("id", "")))
                        same_chapter_retry = debt_index >= 0
                        if same_chapter_retry:
                            debts[debt_index] = debt
                        else:
                            debts.append(debt)
                        systemic = _systemic_quality_issues(audit)
                        if same_chapter_retry:
                            task["consecutive_quality_debts"] = 1
                            task["consecutive_systemic_debts"] = 1 if systemic else 0
                        else:
                            task["consecutive_quality_debts"] = int(task.get("consecutive_quality_debts", 0)) + 1
                            task["consecutive_systemic_debts"] = (
                                int(task.get("consecutive_systemic_debts", 0)) + 1 if systemic else 0
                            )
                        task["quality_directives"] = _quality_directives(audit)
                        task["last_rejected_candidate"] = {
                            "chapter": index + 1,
                            "chapter_id": chapter.get("id", ""),
                            "title": chapter.get("title", ""),
                            "draft": draft[:30000],
                            "audit": audit,
                            "rejected_at": utc_now(),
                        }
                        if int(task.get("consecutive_systemic_debts", 0)) >= 3 or int(task.get("consecutive_quality_debts", 0)) >= 5:
                            task["status"] = "paused"
                            task["checkpoint_message"] = (
                                f"已连续 {task.get('consecutive_quality_debts', 0)} 章未达标；"
                                "系统已触发质量熔断，当前低质量候选未写入正文。"
                            )
                            _save_director_task(
                                task,
                                "连续质量债表明规划或提示词存在系统性问题，已在写入本章前暂停；请先查看全稿体检后从检查点继续",
                                "warning",
                            )
                            return
                        if not config.get("continue_on_quality_debt", True):
                            task["status"] = "paused"
                            _save_director_task(task, f"第 {index + 1} 章连续修订后仍未达标，已暂停等待人工接管", "warning")
                            return
                    else:
                        _remove_quality_debt(task, str(chapter.get("id", "")))
                        task["consecutive_quality_debts"] = 0
                        task["consecutive_systemic_debts"] = 0
                        task["quality_directives"] = []
                    candidate_failures = _director_candidate_gate_failures(
                        draft, int(config["target_words"])
                    )
                    if candidate_failures:
                        task["status"] = "paused"
                        task["last_rejected_candidate"] = {
                            "chapter": index + 1,
                            "chapter_id": chapter.get("id", ""),
                            "title": chapter.get("title", ""),
                            "draft": draft[:30000],
                            "audit": audit,
                            "gate_failures": candidate_failures,
                            "rejected_at": utc_now(),
                        }
                        task["checkpoint_message"] = (
                            f"第 {index + 1} 章候选稿未写入正文："
                            + "；".join(candidate_failures)
                        )
                        _save_director_task(
                            task,
                            task["checkpoint_message"]
                            + "。这是正文安全门禁，需从本章检查点重新生成。",
                            "error",
                        )
                        return
                    chapter["content"] = draft
                    chapter["authority_state"] = "reviewed" if not passed else "accepted"
                    memory: dict[str, Any] = {}
                    memory_warnings: list[str] = []
                    if passed:
                        if str(audit.get("verdict", "")) != "pass":
                            audit = deepcopy(audit)
                            audit["model_gate_verdict"] = str(
                                audit.get("verdict", "revise")
                            )
                            audit["verdict"] = "pass"
                            audit.setdefault("score_components", {})["threshold"] = int(
                                config["quality_threshold"]
                            )
                        lock_chapter(
                            chapter,
                            actor="auto-director",
                            audit=audit,
                            contract_scan=contract_result,
                        )
                        chapter["summary"] = local_memory_result(
                            project, chapter, draft, "等待完整记忆提取"
                        )["summary"]
                        chapter["memory_status"] = "pending"
                        chapter["execution"] = {
                            "status": "memory_pending",
                            "audit_score": int(audit.get("score", 0)),
                            "audit_verdict": str(audit.get("verdict", "pass")),
                            "contract_scan": contract_result,
                        }
                        project = store.save(
                            project["id"], project,
                            reason=f"director-chapter-{index + 1}-text-saved",
                        )
                        chapter = project["chapters"][index]
                        task = _save_director_task(
                            task, f"第 {index + 1} 章正文已写入作品，正在提取剧情记忆", "success"
                        )
                        memory = await _director_chapter_memory(project, chapter, draft)
                        memory_warnings = list(memory.get("warnings", [])) if isinstance(memory, dict) else []
                        _apply_director_memory(project, chapter, memory)
                        chapter["memory_status"] = "committed"
                    else:
                        chapter["memory_status"] = "quarantined"
                        memory_warnings = ["未通过审校的候选稿已隔离，未回灌正式记忆"]
                        for issue in _audit_issues(audit):
                            enqueue_repair(project, chapter, issue, source="director")
                    if isinstance(memory, dict) and memory.get("fallback"):
                        _record_planning_debt(
                            task,
                            {
                                "phase": "memory", "chapter": index + 1,
                                "chapter_id": chapter.get("id", ""),
                                "title": chapter.get("title", ""),
                                "issues": [str(item) for item in memory.get("warnings", []) if str(item).strip()]
                                or ["AI 记忆提取失败，已使用本地记忆回灌"],
                            },
                        )
                        _director_event(
                            task,
                            f"第 {index + 1} 章使用本地记忆回灌，正文流水线继续运行",
                            "warning",
                        )
                    run_record = {
                        "status": "accepted" if passed else "quality_debt",
                        "last_run_at": utc_now(),
                        "model": str(project.get("settings", {}).get("model", "")),
                        "target_words": int(config["target_words"]),
                        "audit_score": int(audit.get("score", 0)),
                        "audit_verdict": str(audit.get("verdict", "review")),
                        "revision_attempts": int(attempt),
                        "issues": _audit_issues(audit),
                        "warnings": memory_warnings,
                        "authority_state": chapter.get("authority_state", "candidate"),
                        "contract_scan": contract_result,
                    }
                    chapter["execution"] = run_record
                    chapter.setdefault("run_history", []).append(deepcopy(run_record))
                    chapter["run_history"] = chapter["run_history"][-10:]
                    project = store.save(project["id"], project, reason=f"director-chapter-{index + 1}-accepted")
                    task.pop("chapter_draft_checkpoint", None)
                    task.pop("current_audit", None)
                    task["chapter_index"] = index + 1
                    task["completed_chapters"] = index + 1
                    task = _save_director_task(
                        task,
                        (
                            f"第 {index + 1} 章已锁定并完成记忆回灌"
                            if passed
                            else f"第 {index + 1} 章已进入精修队列，未污染正式记忆"
                        ),
                        "success" if passed else "warning",
                    )
                    volume_ends = {
                        int(item.get("chapter_end", 0) or 0)
                        for item in project.get("planning", {}).get("volumes", [])
                        if isinstance(item, dict)
                    }
                    if len(chapters) >= 10 and (
                        index + 1 in volume_ends or (index + 1) % 10 == 0
                    ):
                        health = manuscript_health_report(project)
                        task.setdefault("manuscript_health_history", []).append(
                            {
                                "chapter": index + 1,
                                "score": int(health.get("score", 0)),
                                "severity": str(health.get("severity", "high")),
                                "duplicate_passage_count": int(
                                    health.get("duplicate_passage_count", 0)
                                ),
                                "similar_chapter_count": len(
                                    health.get("similar_chapters", [])
                                ),
                                "volume_progression_issue_count": len(
                                    health.get("volume_progression_issues", [])
                                ),
                                "memory_integrity_issue_count": len(
                                    health.get("memory_integrity_issues", [])
                                ),
                            }
                        )
                        task["manuscript_health_history"] = task[
                            "manuscript_health_history"
                        ][-30:]
                        hard_failures = _director_manuscript_gate_failures(
                            health,
                            final=index + 1 == len(chapters),
                            genre=str(project.get("genre", "")),
                        )
                        if hard_failures:
                            task["latest_manuscript_health"] = health
                            manuscript_debts = task.setdefault(
                                "manuscript_quality_debts", []
                            )
                            manuscript_debts.append(
                                {
                                    "chapter": index + 1,
                                    "score": int(health.get("score", 0)),
                                    "issues": list(hard_failures),
                                    "recorded_at": utc_now(),
                                }
                            )
                            task["manuscript_quality_debts"] = manuscript_debts[-30:]
                            if not config.get("continue_on_quality_debt", True):
                                task["status"] = "paused"
                                task["checkpoint_message"] = (
                                    f"第 {index + 1} 章全稿门禁未通过："
                                    + "；".join(hard_failures)
                                    + "。本章与记忆已保留，请修订后从检查点继续。"
                                )
                                _save_director_task(
                                    task,
                                    task["checkpoint_message"],
                                    "warning",
                                )
                                return
                            task = _save_director_task(
                                task,
                                f"截至第 {index + 1} 章的全稿检查发现："
                                + "；".join(hard_failures)
                                + (
                                    "。初稿生产已完成，已进入精修队列。"
                                    if index + 1 == len(chapters)
                                    else "。已记录全稿质量债务，并按无人值守设置继续下一章。"
                                ),
                                "warning",
                            )
                            continue
                        task["latest_manuscript_health"] = health
                        task = _save_director_task(
                            task,
                            f"截至第 {index + 1} 章的全稿门禁通过（健康分 {health.get('score', 0)}）",
                            "success",
                        )
                final_health = manuscript_health_report(project)
                release_failures = _director_manuscript_gate_failures(
                    final_health, final=True, genre=str(project.get("genre", ""))
                )
                task["latest_manuscript_health"] = final_health
                task["release_score"] = int(final_health.get("score", 0))
                task["release_failures"] = release_failures
                task["release_ready"] = not release_failures and not task.get(
                    "quality_debts"
                )
                task["release_status"] = (
                    "ready" if task["release_ready"] else "needs_revision"
                )
                task["phase"] = "completed"
                task["status"] = "completed"
                if task["release_ready"]:
                    message = (
                        f"《{project.get('title', '')}》初稿与发布门禁均已完成，"
                        f"共 {len(chapters)} 章"
                    )
                    kind = "success"
                else:
                    message = (
                        f"《{project.get('title', '')}》初稿生产完成，共 {len(chapters)} 章；"
                        f"当前规则检查分 {task['release_score']}，已进入精修队列"
                    )
                    kind = "warning"
                _save_director_task(task, message, kind)
        except asyncio.CancelledError:
            latest = store.get_director_task(task_id)
            if latest and latest.get("status") not in {"completed", "failed"}:
                latest["status"] = "paused"
                _save_director_task(latest, "自动导演已暂停，可从当前检查点继续", "warning")
            raise
        except Exception as exc:
            latest = store.get_director_task(task_id) or task
            latest["status"] = "paused"
            latest["failure_kind"] = (
                "content_filtered" if isinstance(exc, ModelContentFilteredError) else "hard"
            )
            if isinstance(exc, ModelContentFilteredError):
                current_project = store.get(latest["project_id"])
                latest["failure_project_updated_at"] = (
                    current_project.get("updated_at", "") if current_project else ""
                )
            latest["error"] = planning_exception_detail(exc)
            _save_director_task(
                latest,
                f"自动导演已暂停：{latest['error']}",
                "error",
            )
        finally:
            lease_heartbeat.cancel()
            with suppress(asyncio.CancelledError):
                await lease_heartbeat
            store.release_director_task_lease(task_id, runtime_instance_id)
            director_runners.pop(task_id, None)
    
    
    def _runtime_launch_director(task_id: str) -> None:
        running = director_runners.get(task_id)
        if running and not running.done():
            return
        director_runners[task_id] = asyncio.create_task(_run_auto_director(task_id))
    
    
    def _reconcile_orphaned_director_task(task: dict[str, Any]) -> dict[str, Any]:
        """Turn a restart-left task into a resumable checkpoint.
    
        This must happen in the serving process, not in ``ProjectStore.__init__``.
        Database readers and test processes are allowed to open the same file while
        a director is running and must never pause that live job as a side effect.
        """
        if task.get("status") not in {"queued", "running", "stopping"}:
            return task
        runner = director_runners.get(str(task.get("id", "")))
        if runner is not None and not runner.done():
            return task
        if store.director_task_lease_active(str(task.get("id", ""))):
            return task
        task["status"] = "paused"
        return _save_director_task(
            task,
            "检测到服务重启遗留任务，已安全转为暂停；可从最后检查点继续",
            "warning",
        )
    
    
    async def director_start(body: AutoDirectorStartRequest) -> dict[str, Any]:
        source = ensure_project_defaults(body.source_project)
        store.backup(_backup_directory())
        created = store.create("AI 自动导演新作")
        created["settings"] = json.loads(json.dumps(source.get("settings", {}), ensure_ascii=False))
        created["style"] = json.loads(json.dumps(source.get("style", {}), ensure_ascii=False))
        created = store.save(created["id"], created, reason="director-created")
        config = {
            "seed": body.seed.strip(), "preferences": body.preferences.strip(),
            "story_mode": "short" if body.story_mode == "short" else "long",
            "target_chapters": int(body.target_chapters), "target_words": int(body.target_words),
            "preferred_volume_count": (
                int(body.preferred_volume_count) if body.preferred_volume_count else None
            ),
            "quality_threshold": int(body.quality_threshold),
            "max_revision_attempts": int(body.max_revision_attempts),
            "continue_on_quality_debt": bool(body.continue_on_quality_debt),
        }
        task = store.create_director_task(
            created["id"],
            {"phase": "incubator", "message": "等待自动导演启动", "config": config, "events": [], "completed_chapters": 0, "quality_debts": [], "planning_debts": []},
        )
        _launch_director(task["id"])
        return {"task": task, "project": created}
    
    
    async def director_write_planned(
        project_id: str, body: ContinuePlannedWritingRequest
    ) -> dict[str, Any]:
        project = store.get(project_id)
        if not project:
            raise DirectorControlError(404, "作品不存在")
        latest = store.latest_director_task(project_id)
        if latest and latest.get("status") in {"queued", "running", "stopping"}:
            raise DirectorControlError(409, "当前已有 AI 任务运行，请先等待完成或暂停")
        project = ensure_project_defaults(project)
        volumes = [
            item
            for item in project.get("planning", {}).get("volumes", [])
            if isinstance(item, dict)
        ]
        if not volumes:
            raise DirectorControlError(400, "还没有分卷规划，请先生成全书大规划")
        incomplete: list[str] = []
        for index, volume in enumerate(volumes, start=1):
            expected = max(
                0,
                int(volume.get("chapter_end", 0) or 0)
                - int(volume.get("chapter_start", 1) or 1)
                + 1,
            )
            routes = volume.get("chapters", [])
            if not isinstance(routes, list) or len(routes) != expected or expected <= 0:
                incomplete.append(str(volume.get("title") or f"第{index}卷"))
        if incomplete:
            raise DirectorControlError(
                400,
                "这些分卷还没有完整章节路线：" + "、".join(incomplete[:8]),
            )
    
        store.backup(_backup_directory())
        for volume in volumes:
            project = ensure_project_defaults(
                apply_volume_routes(project, str(volume.get("id", "")))
            )
        project = store.save(project_id, project, reason="planned-writing-start")
        chapters = [
            item for item in project.get("chapters", []) if isinstance(item, dict)
        ]
        first_unwritten = next(
            (
                index
                for index, chapter in enumerate(chapters)
                if not str(chapter.get("content", "")).strip()
            ),
            len(chapters),
        )
        if first_unwritten >= len(chapters):
            raise DirectorControlError(400, "规划中的章节已经全部有正文，可使用 AI 自动精修进行总检")
    
        config = {
            "seed": "",
            "preferences": "使用已经审核的全书规划和逐章路线继续写作",
            "story_mode": project.get("story_mode", "long"),
            "target_chapters": len(chapters),
            "target_words": int(
                project.get("settings", {}).get("target_words", 1200) or 1200
            ),
            "quality_threshold": int(body.quality_threshold),
            "max_revision_attempts": int(body.max_revision_attempts),
            "continue_on_quality_debt": bool(body.continue_on_quality_debt),
        }
        task = store.create_director_task(
            project_id,
            {
                "task_type": "planned_production",
                "phase": "chapters",
                "message": "规划和章节已保存，等待 AI 从第一章未完成正文继续创作",
                "config": config,
                "events": [],
                "chapter_index": first_unwritten,
                "completed_chapters": first_unwritten,
                "total_chapters": len(chapters),
                "quality_debts": [],
                "planning_debts": [],
            },
        )
        _launch_director(task["id"])
        return {"task": task, "project": project}
    
    
    async def director_refine(project_id: str, body: AutoRefineRequest) -> dict[str, Any]:
        project = store.get(project_id)
        if not project:
            raise DirectorControlError(404, "作品不存在")
        latest = store.latest_director_task(project_id)
        if latest and latest.get("status") in {"queued", "running"}:
            raise DirectorControlError(409, "当前已有 AI 自动任务运行，请等待完成或先暂停")
        project = ensure_project_defaults(project)
        rebuild_repair_queue(project)
        chapters = [item for item in project.get("chapters", []) if isinstance(item, dict)]
        existing_ids = {str(item.get("id", "")) for item in chapters}
        requested = [str(item) for item in body.chapter_ids if str(item) in existing_ids]
        if requested:
            targets = requested
        elif body.scope == "all":
            targets = [str(item.get("id", "")) for item in chapters if len(str(item.get("content", "")).strip()) >= 100]
        else:
            active_repairs = {
                str(item.get("chapter_id", ""))
                for item in project.get("repair_queue", [])
                if isinstance(item, dict)
                and str(item.get("status", "queued")) in {"queued", "in_progress"}
            }
            targets = [str(item.get("id", "")) for item in chapters if str(item.get("id", "")) in active_repairs]
        targets = list(dict.fromkeys(targets))
        if not targets:
            raise DirectorControlError(400, "没有可自动精修的章节；可选择检查全部已有正文")
        store.backup(_backup_directory())
        project = store.save(project_id, project, reason="auto-refine-start")
        queue_by_chapter: dict[str, list[dict[str, Any]]] = {}
        for item in project.get("repair_queue", []):
            if isinstance(item, dict) and str(item.get("status", "queued")) in {"queued", "in_progress"}:
                queue_by_chapter.setdefault(str(item.get("chapter_id", "")), []).append(item)
        chapter_map = {str(item.get("id", "")): (index + 1, item) for index, item in enumerate(chapters)}
        debts = []
        for chapter_id in targets:
            number, chapter = chapter_map[chapter_id]
            issues = [
                {
                    "severity": str(item.get("severity", "medium")),
                    "category": str(item.get("category", "质量问题")),
                    "message": str(item.get("message", "待精修")),
                    "suggestion": str(item.get("suggestion", "")),
                    "source": str(item.get("source", "repair_queue")),
                }
                for item in queue_by_chapter.get(chapter_id, [])
            ]
            debts.append(
                {"chapter": number, "chapter_id": chapter_id, "title": chapter.get("title", ""),
                 "score": int(chapter.get("execution", {}).get("audit_score", 0) or 0), "issues": issues}
            )
        config = {
            "quality_threshold": int(body.quality_threshold),
            "max_revision_attempts": int(body.max_revision_attempts),
            "continue_on_quality_debt": True,
            "target_words": int(project.get("settings", {}).get("target_words", 1200) or 1200),
            "instruction": body.instruction.strip(),
            "scope": "all" if body.scope == "all" else "repairs",
        }
        task = store.create_director_task(
            project_id,
            {
                "task_type": "refinement", "phase": "refinement",
                "message": "等待 AI 全书精修启动", "config": config,
                "events": [], "completed_chapters": 0, "total_chapters": len(targets),
                "refine_index": 0, "refinement_targets": targets,
                "quality_debts": debts, "planning_debts": [],
                "seed_candidates": {
                    chapter_id: candidate
                    for chapter_id, candidate in {
                        **(
                            latest.get("seed_candidates", {})
                            if isinstance(latest, dict)
                            and isinstance(latest.get("seed_candidates"), dict)
                            else {}
                        ),
                        **(
                            latest.get("refinement_candidates", {})
                            if isinstance(latest, dict)
                            and isinstance(latest.get("refinement_candidates"), dict)
                            else {}
                        ),
                        **(
                            {
                                str(latest.get("last_rejected_candidate", {}).get("chapter_id", "")):
                                latest.get("last_rejected_candidate", {})
                            }
                            if isinstance(latest, dict)
                            and isinstance(latest.get("last_rejected_candidate"), dict)
                            and str(latest.get("last_rejected_candidate", {}).get("chapter_id", ""))
                            else {}
                        ),
                    }.items()
                    if chapter_id in targets
                    and isinstance(candidate, dict)
                    and len(str(candidate.get("draft", "")).strip()) >= 100
                },
            },
        )
        _launch_director(task["id"])
        return {"task": task, "project": project}
    
    
    async def director_task(task_id: str) -> dict[str, Any]:
        task = store.get_director_task(task_id)
        if not task:
            raise DirectorControlError(404, "自动导演任务不存在")
        task = _reconcile_orphaned_director_task(task)
        return _reconcile_director_quality_debts(task)
    
    
    async def director_latest(project_id: str) -> dict[str, Any]:
        task = store.latest_director_task(project_id)
        return (
            _reconcile_director_quality_debts(
                _reconcile_orphaned_director_task(task)
            )
            if task
            else {"status": "none", "project_id": project_id}
        )
    
    
    async def director_pause(task_id: str) -> dict[str, Any]:
        task = store.get_director_task(task_id)
        if not task:
            raise DirectorControlError(404, "自动导演任务不存在")
        if task.get("status") in {"completed", "failed"}:
            return task
        task["status"] = "paused"
        task = _save_director_task(task, "正在暂停自动导演…", "warning")
        runner = director_runners.get(task_id)
        if runner and not runner.done():
            runner.cancel()
        return task
    
    
    async def director_resume(task_id: str) -> dict[str, Any]:
        task = store.get_director_task(task_id)
        if not task:
            raise DirectorControlError(404, "自动导演任务不存在")
        if task.get("status") == "completed":
            return task
        if task.get("failure_kind") == "content_filtered":
            current_project = store.get(task["project_id"])
            if current_project and current_project.get("updated_at") == task.get(
                "failure_project_updated_at"
            ):
                raise DirectorControlError(
                    409,
                    "模型服务已拦截本章正文。请先调整本章路线或更换正文模型并保存，再从检查点继续。",
                )
        task["status"] = "queued"
        task.pop("error", None)
        task.pop("failure_kind", None)
        task.pop("failure_project_updated_at", None)
        task["checkpoint_message"] = ""
        task = _save_director_task(task, "任务已进入恢复队列")
        _launch_director(task_id)
        return task

    return DirectorRuntime(
        runners=director_runners,
        run_auto_director=_run_auto_director,
        launch_director=_runtime_launch_director,
        director_event=_director_event,
        remove_quality_debt=_remove_quality_debt,
        start=director_start,
        write_planned=director_write_planned,
        refine=director_refine,
        get_task=director_task,
        latest=director_latest,
        pause=director_pause,
        resume=director_resume,
    )
