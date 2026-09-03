from pathlib import Path


APP_JS = Path(__file__).parents[1] / "static" / "app.js"
INDEX_HTML = Path(__file__).parents[1] / "static" / "index.html"


def test_draft_and_async_results_are_bound_to_originating_chapter():
    source = APP_JS.read_text(encoding="utf-8")
    assert "let draftTarget = null;" in source
    assert "draftTarget.chapterId!==activeChapterId" in source
    assert 'api("/api/chapter/memory/apply"' in source
    assert "activeChapterId=targetChapterId" in source
    assert "chapterById(targetChapterId)" in source


def test_generated_draft_returns_to_its_beginning():
    source = APP_JS.read_text(encoding="utf-8")
    assert '$("#draft").scrollTop=0;' in source


def test_audit_issues_support_selective_reversible_revision():
    source = APP_JS.read_text(encoding="utf-8")
    assert 'class="audit-select"' in source
    assert "reviseSelectedAuditIssues" in source
    assert "AI 修订所选问题" in source
    assert "撤销本次修订" in source
    assert 'mode:"rewrite"' in source
    assert "lastAuditDraftSignature" in source
    assert "重新审计的是当前修订稿" in source
    assert "审计对象：当前右侧草稿" in source


def test_inspiration_incubator_and_one_click_chapter_flow_are_bound():
    source = APP_JS.read_text(encoding="utf-8")
    assert 'api("/api/incubator"' in source
    assert "createProjectFromIncubator" in source
    assert "autoPlanAndWriteChapter" in source
    assert 'api("/api/chapter/plan"' in source
    assert "AI 正在写本章" in source
    assert 'activeMode=target.content.trim()?"continue":"instruction"' in source


def test_full_book_director_has_persistent_progress_controls():
    source = APP_JS.read_text(encoding="utf-8")
    html = INDEX_HTML.read_text(encoding="utf-8")
    assert "一键创作全文 · 从灵感到完稿" in html
    assert 'id="directorStatusBtn"' in html
    assert 'api("/api/director/start"' in source
    assert "fullBookDirectorModal" in source
    assert "renderDirectorProgress" in source
    assert "从检查点继续" in source
    assert "continue_on_quality_debt" in source
    assert "director-running" in source
    assert "恢复时从下一张人物卡继续" in source


def test_long_running_operations_are_bound_to_originating_project():
    source = APP_JS.read_text(encoding="utf-8")
    assert "requestProject=JSON.parse(JSON.stringify(project))" in source
    assert "if(project.id!==targetProjectId)return toast" in source
    assert "let loadRequestId = 0;" in source
    assert "if(requestId!==loadRequestId)return;" in source


def test_project_health_check_requires_explicit_duplicate_cleanup():
    source = APP_JS.read_text(encoding="utf-8")
    assert "projectHealthModal" in source
    assert 'class="duplicate-clean"' in source
    assert "confirm(`将清空" in source
    assert 'save("project-health-remove-duplicate-content")' in source
    assert "未回收线索" in source
    assert "待确认备注" in source


def test_detected_model_replaces_stale_saved_model():
    source = APP_JS.read_text(encoding="utf-8")
    assert "!models.includes(configured)" in source
    assert 'save("sync-detected-model")' in source
    assert "已匹配当前模型" in source


def test_detailed_master_outline_and_volume_blueprint_are_editable():
    source = APP_JS.read_text(encoding="utf-8")
    assert 'data-key="full_outline"' in source
    assert 'data-key="main_plot"' in source
    assert 'data-key="major_character_arcs"' in source
    assert 'data-key="synopsis"' in source
    assert 'data-key="bridge_to_next"' in source
    assert "syncDetailedOutline" in source
    assert 'save("sync-ai-detailed-outline")' in source


def test_incomplete_volume_result_cannot_replace_existing_routes():
    source = APP_JS.read_text(encoding="utf-8")
    incomplete_guard = source.index("if(!result.complete)")
    replacement = source.index("currentVolume.chapters=asArray(result.chapters)")
    assert incomplete_guard < replacement
    assert "旧路线未被替换" in source


def test_planning_timer_is_inserted_before_first_generation_finishes():
    source = APP_JS.read_text(encoding="utf-8")
    assert 'note.insertAdjacentHTML("afterend",planningStatusHtml())' in source
    assert "本地9B模型可能需要 2–12 分钟" in source
    assert "停止当前规划任务" in source
    assert "planningAborter?.abort()" in source


def test_unsaved_work_warns_and_memory_acceptance_uses_reliable_transaction():
    source = APP_JS.read_text(encoding="utf-8")
    assert 'window.addEventListener("beforeunload"' in source
    assert "editVersion===savedVersion" in source
    assert 'api("/api/chapter/accept"' in source
    assert 'api("/api/chapter/memory"' in source
    assert 'api("/api/chapter/memory/apply"' in source
    assert 'api("/api/chapter/memory/degrade"' in source
    assert "commit_id:commitId" in source
    assert "正文已安全保存，记忆状态待修复" in source
    assert "待确认连续性备注" in source
    assert "r.continuity_notes" in source


def test_prompt_preview_uses_current_chapter_and_formats_validation_errors():
    source = APP_JS.read_text(encoding="utf-8")
    assert "chapter_id:chapterId||activeChapterId" in source
    assert "Array.isArray(detail)" in source


def test_professional_character_and_world_controls_are_bound():
    source = APP_JS.read_text(encoding="utf-8")
    for marker in (
        "c-personality",
        "c-values",
        "c-contradictions",
        "c-examples",
        "w-secondary",
        "w-logic",
        "w-group",
        "w-nonrecursive",
        "mLoreBudget",
        "mLoreSteps",
        "description_ledger",
    ):
        assert marker in source


def test_reference_knowledge_canon_and_provider_controls_are_bound():
    source = APP_JS.read_text(encoding="utf-8")
    html = INDEX_HTML.read_text(encoding="utf-8")
    for marker in (
        'id="fanficBtn"',
        'id="referencesBtn"',
        'id="knowledgeBtn"',
        'id="styleFilesBtn"',
        'id="canonAuditBtn"',
    ):
        assert marker in html
    for marker in (
        'api("/api/reference/parse"',
        'api("/api/knowledge/graph"',
        'api("/api/canon/audit"',
        "canonPreflightBeforeAccept",
        "audit_before_accept",
        'exported.settings.api_key=""',
        'value="siliconflow"',
        'value="xai"',
        'value="llama_cpp"',
    ):
        assert marker in source


def test_context_viewer_shows_selection_and_budget_trace():
    source = APP_JS.read_text(encoding="utf-8")
    assert 's.status==="omitted"' in source
    assert "tokens_before" in source
    assert "tokens_after" in source
    assert "本区块未发送给模型" in source


def test_short_generation_repair_is_visible_and_windows_has_bypass_launcher():
    source = APP_JS.read_text(encoding="utf-8")
    root = APP_JS.parents[1]
    assert 'event.type==="repair"' in source
    assert "正在自动补足" in source
    assert "length_repaired" in source
    assert (root / "run.bat").exists()
    assert "ExecutionPolicy Bypass" in (root / "run.bat").read_text(encoding="utf-8")
