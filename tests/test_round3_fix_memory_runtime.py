"""Regressions for review findings #4 (backup dir), #5 (shared reader limit),
#6 (reader polling never stuck) and #7 (cache-busting of changed assets)."""
from __future__ import annotations

import asyncio
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from app.services import reader_simulation as readers

ROOT = Path(__file__).resolve().parents[1]


# ----------------------------------------------------------------- finding #4

def test_relocated_database_keeps_the_default_backup_directory(tmp_path):
    """INKFORGE_DB_PATH alone must not move backups away from data/backups:
    existing installs would lose sight of (and stop rotating) their backups."""
    env = {key: value for key, value in os.environ.items() if key != "INKFORGE_BACKUP_PATH"}
    env["INKFORGE_DB_PATH"] = str(tmp_path / "elsewhere" / "inkforge.db")
    output = subprocess.run(
        [sys.executable, "-c", "from app.core.config import settings; print(settings.backup_path)"],
        cwd=ROOT, env=env, capture_output=True, text=True, encoding="utf-8", check=True,
    ).stdout.strip()
    assert Path(output) == (ROOT / "data" / "backups").resolve()


def test_explicit_backup_path_still_wins(tmp_path):
    env = dict(os.environ)
    env["INKFORGE_DB_PATH"] = str(tmp_path / "db" / "inkforge.db")
    env["INKFORGE_BACKUP_PATH"] = str(tmp_path / "bk")
    output = subprocess.run(
        [sys.executable, "-c", "from app.core.config import settings; print(settings.backup_path)"],
        cwd=ROOT, env=env, capture_output=True, text=True, encoding="utf-8", check=True,
    ).stdout.strip()
    assert Path(output) == (tmp_path / "bk").resolve()


def test_test_session_backups_are_isolated_from_repo_data():
    from app.core.config import settings

    data = (ROOT / "data").resolve()
    for name in ("database_path", "secret_path", "telemetry_path", "backup_path"):
        path = Path(getattr(settings, name)).resolve()
        assert data not in (path, *path.parents), f"{name} -> {path}"


# ----------------------------------------------------------------- finding #5

def _project(chapters: int) -> dict:
    text = "她推开门，雨声一下子涌进来，桌上的信还没有拆开。" * 30
    return {"chapters": [{"id": f"c{i}", "title": f"第{i}章", "content": text} for i in range(chapters)],
            "settings": {}}


def _tracking_completion():
    state = {"now": 0, "peak": 0}

    async def fake(settings, messages, **kwargs):
        state["now"] += 1
        state["peak"] = max(state["peak"], state["now"])
        await asyncio.sleep(0.01)
        state["now"] -= 1
        return {"blocks": []}, []

    return fake, state


def test_default_limit_is_shared_by_concurrent_runs():
    fake, state = _tracking_completion()

    async def main():
        await asyncio.gather(
            readers.simulate_chapters(_project(3), [0, 1, 2], completion=fake, compare_with_plan=False),
            readers.simulate_chapters(_project(3), [0, 1, 2], completion=fake, compare_with_plan=False),
        )

    asyncio.run(main())
    assert 1 < state["peak"] <= readers.DEFAULT_CONCURRENCY


def test_shared_limit_survives_separate_event_loops():
    fake, _state = _tracking_completion()
    for _ in range(2):  # a loop-bound semaphore reused across loops would raise
        asyncio.run(readers.simulate_chapters(_project(4), [0, 1, 2, 3], completion=fake, compare_with_plan=False))


# ----------------------------------------------------------------- finding #6

NODE_HARNESS = r"""
const fs = require("fs"), vm = require("vm");
const src = fs.readFileSync(process.argv[2], "utf8");
const timers = [];
const box = {innerHTML: ""};
const ctx = {
  project: {id: "p", chapters: []},
  $: sel => sel === "#readerStatus" ? box : sel === "#modal" ? {open: true} : null,
  $$: () => [], asArray: v => Array.isArray(v) ? v : [],
  escapeHtml: s => String(s), toast: () => {},
  api: async () => { throw new Error("网络错误"); },
  setTimeout: (fn, ms) => { timers.push(ms); return timers.length; }, clearTimeout: () => {},
  console,
};
vm.createContext(ctx);
vm.runInContext(src, ctx);
(async () => {
  let finished = 0;
  await ctx.pollReaderJob("p", () => { finished += 1; }, 0);
  const retried = timers.length;
  await ctx.pollReaderJob("p", () => { finished += 1; }, 4);
  console.log(JSON.stringify({retried, finished, timers: timers.length, html: box.innerHTML}));
})();
"""


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
def test_reader_polling_retries_then_releases_the_button(tmp_path):
    harness = tmp_path / "harness.cjs"
    harness.write_text(NODE_HARNESS, encoding="utf-8")
    output = subprocess.run(
        ["node", str(harness), str(ROOT / "static" / "js" / "features" / "reader-simulation.js")],
        capture_output=True, text=True, encoding="utf-8", check=True,
    ).stdout.strip()
    result = json.loads(output)
    assert result["retried"] == 1          # first failure schedules a retry
    assert result["finished"] == 1         # the last allowed failure gives up and re-enables
    assert result["timers"] == 1           # ... without scheduling another poll
    assert "已停止自动刷新" in result["html"]


# ----------------------------------------------------------------- finding #7

def test_every_asset_is_cache_busted_with_the_app_version():
    from app.core.config import APP_VERSION

    html = (ROOT / "static" / "index.html").read_text(encoding="utf-8")
    urls = re.findall(r'(?:src|href)="(/assets/[^"]+)"', html)
    assert len(urls) >= 20
    for url in urls:  # a release bump must change every asset URL, planning.js included
        assert url.endswith(f"?v={APP_VERSION}"), f"{url} still served with an old cache key"
