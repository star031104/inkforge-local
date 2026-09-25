"""Measure synthetic long-manuscript saves without accessing user data or models."""
import json
from pathlib import Path
import sys
import tempfile
from time import perf_counter

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.db import ProjectStore

results = []
with tempfile.TemporaryDirectory(prefix="inkforge-benchmark-") as directory:
    for size in (100_000, 500_000, 1_000_000):
        store = ProjectStore(Path(directory) / f"{size}.db")
        p = store.create(f"合成 {size} 字")
        p["chapters"] = [{"id": f"c{i}", "title": f"第{i+1}章", "content": ("雨水落在门前，她收好信件，朝旧桥走去。" * 200)[:2000]}
                         for i in range(size // 2000)]
        started = perf_counter()
        p = store.save(p["id"], p)
        first = perf_counter() - started
        p["chapters"][0]["content"] += "她回头看了一眼。"
        started = perf_counter()
        p = store.save(p["id"], p)
        edit = perf_counter() - started
        started = perf_counter()
        store.save(p["id"], p)
        noop = perf_counter() - started
        results.append({"characters": size, "initial_seconds": round(first, 3), "edit_seconds": round(edit, 3), "noop_seconds": round(noop, 3)})
output = Path(__file__).resolve().parents[1] / "output/benchmark.json"
output.parent.mkdir(parents=True, exist_ok=True)
output.write_text(json.dumps(results, indent=2), encoding="utf-8")
print(json.dumps(results, indent=2))
