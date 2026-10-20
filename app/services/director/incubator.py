"""Incubation tasks and the director's incubator phase (seed brief, cast, world)."""
from __future__ import annotations

import uuid
from typing import Any

from app.storage.schema.project import ensure_project_defaults
from app.planning.validation import validate_incubator_result
from app.services.director.context import DirectorContext


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


def install(ctx: DirectorContext) -> None:
    """Define the incubator closures on ``ctx``."""
    store = ctx.store
    _complete_incubator_option = ctx._complete_incubator_option
    _generate_incubator_core = ctx._generate_incubator_core
    director_character_card = ctx.director_character_card
    director_seed_brief = ctx.director_seed_brief
    director_seed_cast = ctx.director_seed_cast
    director_seed_world = ctx.director_seed_world
    _director_event = ctx._director_event
    _save_director_task = ctx._save_director_task

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

    async def _run_incubator_phase(
        task: dict[str, Any], project: dict[str, Any], config: dict[str, Any]
    ) -> dict[str, Any]:
        """Seed brief → cast → character cards → world → project skeleton."""
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
        return task

    ctx.register(
        _run_incubation_task,
        _run_incubator_phase,
    )
