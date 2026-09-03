# InkForge 0.17.0 — Knowledge & Canon Refactor

## Added

- SiliconFlow / llama.cpp / generic OpenAI-Compatible provider abstraction.
- Default SiliconFlow preset: `Qwen/Qwen3-8B`, non-thinking mode.
- Multi-file reference library: style / canon / background / research.
- TXT, MD, JSON, CSV, HTML, DOCX, EPUB and PDF text import.
- Multi-reference style analysis and source-overlap guard.
- Fanfiction Canon Profile with human-verification state.
- Canon-aware prompt priority and OOC audit endpoint.
- Optional strict Canon Gate before accepting generated prose.
- Evidence-grounded entity/fact/relation knowledge layer.
- Superseded current-state facts to prevent stale-state conflicts.
- Knowledge graph inspection/editor UI.
- Automatic projection of accepted, evidence-backed location/state/relationship updates.
- API key removal from exported project JSON.

## Changed

- API schema 28 → 31.
- App version 0.16.0 → 0.17.0.
- Model client is no longer llama.cpp-specific.
- Prompt composition separates style references from canon/background references.
- README now documents SiliconFlow-first testing and later local-model switching.

## Verification

- 120 pytest tests pass.
- `node --check static/app.js` passes.
- `python -m compileall app` passes.
- FastAPI root/health/reference/knowledge/provider smoke tests pass.
- SiliconFlow protocol integration tested against a local mock OpenAI-compatible server using model ID `Qwen/Qwen3-8B`.
- Live SiliconFlow cloud generation was **not** run because no user API key was provided.

