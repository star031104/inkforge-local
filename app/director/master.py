"""Director master-plan and volume-route phases."""
from __future__ import annotations

from typing import Any

from app.storage.schema.project import ensure_project_defaults
from app.core.clock import utc_now
from app.planning.validation import (
    _director_volume_specs,
    validate_director_volume_expansion,
    validate_master_result,
)
from app.planning.route_validation import _audit_route_checkpoint_prefix
from app.planning.model import apply_volume_routes, normalize_master_plan, planned_volume_count
from app.llm.providers import is_local_endpoint
from app.director.context import DirectorContext


def install(ctx: DirectorContext) -> None:
    """Define the master closures on ``ctx``."""
    store = ctx.store
    _director_full_outline = ctx._director_full_outline
    director_master_bible = ctx.director_master_bible
    director_master_contract = ctx.director_master_contract
    director_master_volume_core = ctx.director_master_volume_core
    director_master_volume_details = ctx.director_master_volume_details
    director_plan_chapter_route = ctx.director_plan_chapter_route
    _director_event = ctx._director_event
    _save_director_task = ctx._save_director_task
    _record_planning_debt = ctx._record_planning_debt

    async def _run_master_phase(task: dict[str, Any], config: dict[str, Any]) -> dict[str, Any]:
        """Story bible → per-volume contracts → expanded volume blueprints."""
        project = store.get(task["project_id"])
        target = int(project.get("narrative", {}).get("target_chapters", config.get("target_chapters", 30)))
        configured_volumes = config.get("preferred_volume_count")
        requested_volumes = planned_volume_count(
            project, target, str(config.get("preferences", "")),
            configured=configured_volumes,
            local=is_local_endpoint(project.get("settings", {})),
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
        return task

    async def _run_volumes_phase(task: dict[str, Any], config: dict[str, Any]) -> dict[str, Any]:
        """Break every volume into checkpointed, re-audited chapter routes."""
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
        return task

    ctx.register(
        _run_master_phase,
        _run_volumes_phase,
    )
