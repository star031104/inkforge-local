from __future__ import annotations

import argparse
import asyncio
import getpass
import json
import os
import re
import shutil
import subprocess
import sys
import time
import uuid
import zipfile
from copy import deepcopy
from datetime import datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from fastapi.testclient import TestClient

import app.main as main
from app.db import ProjectStore, default_project, ensure_project_defaults
from app.llama_client import chat_once, chat_stream, list_models


MODEL = "Qwen/Qwen3-8B"
BASE_URL = "https://api.siliconflow.cn/v1"

STYLE_SAMPLE = """
雨水沿着瓦当落进石槽。沈砚没有立刻开口，只把三枚算筹摆在木牍旁边，让仓吏先算第二遍。院外有甲士经过，靴底把积水踩得很响。仓吏算到最后一枚，脸色才变，却仍把竹片压在掌下，不肯说账有错。

沈砚也没有争。他把结论缩到最小：三处差额落在同一旬，只能说明值得再查，不能说明谁偷了粮。秦王政听完，先问旧例为何不许外人翻册，再问若木牍不离仓曹、封泥由仓吏亲验，风险还能剩多少。没有人说大道理，厅里只剩灯芯轻响。

第二册开封以后，同一旬末尾又出现一个不大的缺口。沈砚没有抬头看王，只把那一日的木牍移到左侧，让仓吏亲手复算。天光越过门槛时，两个人报出了同一个数。这个数还不能定罪，却足以让第三册必须在明日开封。
""".strip()

CANON_SOURCE = """
《雾港来客》角色资料：洛弦，二十四岁，港务档案员。她习惯先核对记录再相信口头说法，面对陌生人礼貌但保持距离。她说话短，极少主动谈自己的过去。压力增大时会把问题拆成可验证的小步骤，不会因为对方示好就迅速交付信任。她擅长整理航运记录和辨认旧式封签，但不会格斗，也没有超自然能力。她最反感为了效率篡改原始记录。对重要的人产生信任也需要长期共同经历。资料对应故事开篇、她尚未认识男主的时间点。
""".strip()


def compact_len(text: str) -> int:
    return len(re.sub(r"\s+", "", str(text or "")))


def parse_sse(text: str) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    for block in str(text or "").split("\n\n"):
        line = next((x for x in block.splitlines() if x.startswith("data:")), "")
        if not line:
            continue
        try:
            events.append(json.loads(line[5:].strip()))
        except json.JSONDecodeError:
            continue
    return events


def scrub(value: Any, secret: str) -> Any:
    if isinstance(value, str):
        return value.replace(secret, "***REDACTED***") if secret else value
    if isinstance(value, list):
        return [scrub(item, secret) for item in value]
    if isinstance(value, dict):
        clean = {}
        for key, item in value.items():
            if str(key).lower() in {"api_key", "apikey", "authorization"}:
                clean[key] = "***REDACTED***" if item else ""
            else:
                clean[key] = scrub(item, secret)
        return clean
    return value


class Recorder:
    def __init__(self, secret: str) -> None:
        self.secret = secret
        self.steps: list[dict[str, Any]] = []

    def add(self, name: str, status: str, detail: str = "", **data: Any) -> None:
        self.steps.append(
            {
                "name": name,
                "status": status,
                "detail": scrub(str(detail), self.secret),
                "data": scrub(data, self.secret),
            }
        )
        mark = {"PASS": "PASS", "WARN": "WARN", "FAIL": "FAIL"}.get(status, status)
        print(f"[{mark}] {name}: {scrub(str(detail), self.secret)}", flush=True)

    def guard(self, name: str, fn, *, warn_on_false: bool = False):
        started = time.time()
        try:
            value = fn()
            ok = bool(value) if isinstance(value, bool) else True
            status = "PASS" if ok else ("WARN" if warn_on_false else "FAIL")
            self.add(name, status, f"{time.time()-started:.1f}s")
            return value
        except Exception as exc:  # noqa: BLE001 - QA collector must continue
            self.add(name, "FAIL", f"{type(exc).__name__}: {exc}")
            return None


def cloud_settings(base_url: str) -> dict[str, Any]:
    # Intentionally no api_key. app.providers resolves the process-only env var.
    return {
        "provider": "siliconflow",
        "base_url": base_url,
        "api_key": "",
        "model": MODEL,
        "temperature": 0.76,
        "top_p": 0.9,
        "top_k": 40,
        "min_p": 0.05,
        "repeat_penalty": 1.08,
        "enable_thinking": False,
        "thinking_budget": 0,
        "max_tokens": 3500,
        "context_budget": 24000,
        "recent_chars": 12000,
        "target_words": 900,
        "memory_items": 12,
        "lore_budget": 4500,
        "lore_recursion_steps": 2,
    }


async def direct_provider_checks(cfg: dict[str, Any], recorder: Recorder, out_dir: Path) -> None:
    started = time.time()
    try:
        models = await list_models(cfg)
        recorder.add(
            "SiliconFlow 模型列表",
            "PASS" if MODEL in models else "FAIL",
            f"发现 {len(models)} 个 chat 模型；目标模型={'存在' if MODEL in models else '不存在'}",
        )
    except Exception as exc:  # noqa: BLE001
        recorder.add("SiliconFlow 模型列表", "FAIL", f"{type(exc).__name__}: {exc}")

    try:
        text = await chat_once(
            cfg,
            [{"role": "user", "content": "只回答四个字：连接正常"}],
            max_tokens=32,
            timeout_seconds=90,
        )
        recorder.add("普通 Chat Completion", "PASS" if "连接正常" in text else "FAIL", text[:100])
    except Exception as exc:  # noqa: BLE001
        recorder.add("普通 Chat Completion", "FAIL", f"{type(exc).__name__}: {exc}")

    try:
        raw = await chat_once(
            cfg,
            [{"role": "user", "content": '只输出 JSON：{"ok":true,"mode":"non-thinking","model":"qwen3-8b"}'}],
            max_tokens=120,
            json_mode=True,
            timeout_seconds=120,
        )
        payload = json.loads(raw)
        ok = payload.get("ok") is True
        recorder.add("结构化 JSON", "PASS" if ok else "FAIL", raw[:180])
    except Exception as exc:  # noqa: BLE001
        recorder.add("结构化 JSON", "FAIL", f"{type(exc).__name__}: {exc}")

    try:
        pieces: list[str] = []
        async for part in chat_stream(
            cfg,
            [
                {
                    "role": "user",
                    "content": "写一段120到180字的战国咸阳仓曹场景，只写小说正文，不解释，不输出思考过程。",
                }
            ],
        ):
            pieces.append(part)
        prose = "".join(pieces).strip()
        (out_dir / "00_真实SiliconFlow_流式烟测.txt").write_text(prose, encoding="utf-8")
        ok = compact_len(prose) >= 80 and "<think>" not in prose.lower()
        recorder.add("SSE 流式 + 非思考正文", "PASS" if ok else "FAIL", f"{compact_len(prose)} 字")
    except Exception as exc:  # noqa: BLE001
        recorder.add("SSE 流式 + 非思考正文", "FAIL", f"{type(exc).__name__}: {exc}")
    recorder.add("Provider 直连总耗时", "PASS", f"{time.time()-started:.1f}s")


def historical_project(cfg: dict[str, Any]) -> dict[str, Any]:
    project = default_project("cloud-historical", "咸阳仓牍", "cloud")
    project["settings"].update(cfg)
    project["genre"] = "战国历史架空"
    project["premise"] = "公元前237年前后，现代公共政策研究者沈砚意外来到秦国，只能用当时可执行的方法协助核验仓曹粮账，并逐步卷入秦廷制度争论。"
    project["author_intent"] = "写一部尊重时代物质条件、人物智力和政治利益的历史小说；现代知识必须被翻译成算筹、木牍、流程和可执行制度。"
    project["current_focus"] = "只写沈砚第一次核账并获得继续检查下一册的有限资格，不提前解决贪腐案，不提前推动统一。"
    project["book_rules"] = (
        "公元前221年统一以前，叙述称嬴政为秦王政，朝臣可称王上，不得称皇帝、始皇帝。"
        "古人不降智；任何制度变化都要受到官僚利益、信息成本、交通和执行能力约束。"
        "沈砚不能制造超越时代工业基础的技术奇迹，也不知道秦廷隐秘。"
    )
    project["narrative"].update(
        {
            "pov": "third_limited",
            "tone": "克制、具体、政治现实主义",
            "central_question": "更可计算的治理是否会同时扩大国家能力与百姓负担？",
            "ending_direction": "制度工具被秦廷吸收，但主角必须面对效率与权力扩张的双重后果。",
            "target_chapters": 12,
        }
    )
    project["style"].update(
        {
            "name": "克制历史现实主义",
            "sample": STYLE_SAMPLE,
            "profile": "短句与中长句交替；政治冲突落实到木牍、算筹、封泥、命令、粮车和可见程序；不靠抽象口号推进。",
            "dos": ["用具体物件和程序承载制度冲突", "人物先争可执行边界再争价值判断"],
            "donts": ["现代职场黑话", "古人集体降智", "提前使用皇帝称谓"],
        }
    )
    project["references"] = [
        {"id": "style-a", "name": "克制历史样文", "kind": "style", "text": STYLE_SAMPLE * 2, "enabled": True}
    ]
    project["characters"] = [
        {
            "id": "shen",
            "name": "沈砚",
            "role": "主角",
            "appearance": "二十余岁，初到秦地",
            "personality": "先核证据再下结论，不轻易把相关性当因果",
            "goal": "先活下来并取得可持续的制度试验空间",
            "knowledge": "知道现代统计与公共治理基本方法；不知道秦廷机密、人物私下计划",
            "hard_limits": "不能读心；不能凭空知道未公开史实；现代方法必须转译为当时工具",
            "voice": "克制、把结论缩到可证明的最小范围",
        },
        {
            "id": "qin",
            "name": "秦王政",
            "role": "秦王",
            "personality": "重结果与控制，也追问制度能否执行以及会带来何种风险",
            "knowledge": "掌握秦廷政务；不知道沈砚的现代来历",
            "hard_limits": "不会因为主角一句现代术语就无条件信服",
            "voice": "问题直接，追问边界与代价",
        },
        {
            "id": "fan",
            "name": "樊吏",
            "role": "仓曹旧吏",
            "personality": "熟悉旧例，首先保护自身职责与流程合法性",
            "knowledge": "知道仓曹操作和当前账册；不知道沈砚的真实来历",
        },
    ]
    project["world_entries"] = [
        {
            "id": "titles",
            "title": "秦统一前称谓",
            "category": "历史硬事实",
            "keys": ["秦王政", "嬴政", "王上", "咸阳"],
            "secondary_keys": [],
            "content": "公元前221年统一以前，不称皇帝、始皇帝；叙述称秦王政，朝臣面君可称王上。",
            "canon": "hard",
            "position": "after",
            "order": 1,
            "constant": True,
            "enabled": True,
            "match": "any",
            "character_names": [],
            "chapter_start": 0,
            "chapter_end": 0,
            "inclusion_group": "",
            "non_recursable": False,
            "prevent_recursion": False,
            "delay_until_recursion": False,
        },
        {
            "id": "material",
            "title": "仓曹物质条件",
            "category": "制度与日常",
            "keys": ["仓曹", "粮账", "木牍", "封泥", "算筹"],
            "secondary_keys": ["粮", "账"],
            "content": "核账使用木牍、竹简、算筹与封泥；信息传递依赖人力与道路。不得出现电子表格、数据库、KPI、二维码等现代工具或术语。",
            "canon": "hard",
            "position": "after",
            "order": 2,
            "constant": False,
            "enabled": True,
            "match": "any",
            "character_names": ["沈砚"],
            "chapter_start": 0,
            "chapter_end": 0,
            "inclusion_group": "",
            "non_recursable": False,
            "prevent_recursion": False,
            "delay_until_recursion": False,
        },
    ]
    ch = project["chapters"][0]
    ch["title"] = "第一章 仓门旧牍"
    ch["scene_goal"] = "沈砚核对两册粮牍，证明同一旬差额值得继续查，并取得次日开第三册的有限资格。"
    ch["author_note"] = "不要直接抓出幕后人物；不要写成现代审计培训；本章只推进一步。"
    return ensure_project_defaults(project)


def new_chapter(title: str) -> dict[str, Any]:
    template = default_project("template", "template", "now")["chapters"][0]
    chapter = deepcopy(template)
    chapter["id"] = str(uuid.uuid4())
    chapter["title"] = title
    return chapter


def generation_call(client: TestClient, project: dict[str, Any], chapter_id: str, mode: str, instruction: str, target: int, selection: str = "") -> tuple[str, dict[str, Any], list[dict[str, Any]]]:
    response = client.post(
        "/api/generate",
        json={
            "project": project,
            "chapter_id": chapter_id,
            "mode": mode,
            "instruction": instruction,
            "selection": selection,
            "target_words": target,
        },
    )
    if response.status_code != 200:
        raise RuntimeError(f"generate HTTP {response.status_code}: {response.text[:600]}")
    events = parse_sse(response.text)
    error = next((e for e in events if e.get("type") == "error"), None)
    if error:
        raise RuntimeError(str(error.get("message")))
    text = "".join(str(e.get("text") or "") for e in events if e.get("type") == "token").strip()
    done = next((e for e in events if e.get("type") == "done"), {})
    return text, done, events


def run_historical_pipeline(client: TestClient, cfg: dict[str, Any], recorder: Recorder, out_dir: Path) -> dict[str, Any]:
    project = historical_project(cfg)
    chapter = project["chapters"][0]

    style = client.post("/api/style/analyze-references", json={"project": project})
    if style.status_code == 200:
        data = style.json()
        recorder.add("真实模型·多样本文风分析", "PASS" if not data.get("fallback") else "WARN", data.get("name", ""), fallback=data.get("fallback"))
        project["style"].update({k: data.get(k, project["style"].get(k)) for k in ("name", "profile", "dos", "donts", "source_ids")})
    else:
        recorder.add("真实模型·多样本文风分析", "FAIL", style.text[:500])

    plan = client.post(
        "/api/chapter/plan",
        json={"project": project, "chapter_id": chapter["id"], "instruction": "只规划第一章：两册粮牍出现同旬小额差值，争取明日开第三册。"},
    )
    if plan.status_code == 200:
        pdata = plan.json()
        recorder.add("真实模型·章节细纲", "PASS" if not pdata.get("fallback") else "WARN", pdata.get("goal", ""), fallback=pdata.get("fallback"))
        for key in chapter["plan"]:
            if key in pdata:
                chapter["plan"][key] = pdata[key]
    else:
        recorder.add("真实模型·章节细纲", "FAIL", plan.text[:500])

    preview = client.post(
        "/api/prompt/preview",
        json={"project": project, "chapter_id": chapter["id"], "mode": "instruction", "instruction": "严格按计划写仓曹木牍粮账核验场景。", "selection": "", "target_words": 900},
    )
    if preview.status_code == 200:
        lore_titles = [item.get("title") for item in preview.json().get("activated_lore", [])]
        ok = "秦统一前称谓" in lore_titles and "仓曹物质条件" in lore_titles
        recorder.add("提示词/世界书激活", "PASS" if ok else "FAIL", "、".join(str(x) for x in lore_titles))
    else:
        recorder.add("提示词/世界书激活", "FAIL", preview.text[:500])

    draft, done, events = generation_call(
        client,
        project,
        chapter["id"],
        "instruction",
        "写第一章。场景必须落在仓曹木牍核账：沈砚不指控任何人，只证明第二册也有同旬缺口，秦王政最终允许次日开第三册。只写小说正文。",
        900,
    )
    chapter["content"] = draft
    (out_dir / "01_咸阳仓牍_第一章_真实SiliconFlow.md").write_text("# 第一章 仓门旧牍\n\n" + draft + "\n", encoding="utf-8")
    forbidden = [term for term in ("皇帝", "始皇帝", "KPI", "数据库", "二维码", "电子表格", "作为AI", "以下是") if term in draft]
    gen_ok = compact_len(draft) >= 675 and not forbidden and "<think>" not in draft.lower()
    recorder.add(
        "真实模型·历史正文第一章",
        "PASS" if gen_ok else "FAIL",
        f"{compact_len(draft)} 字；repair={done.get('length_repaired')}; forbidden={forbidden}",
        done=done,
    )

    quality = client.post("/api/chapter/quality", json={"project": project, "chapter_id": chapter["id"], "draft": draft})
    qdata = quality.json() if quality.status_code == 200 else {}
    recorder.add("本地质量门禁·第一章", "PASS" if quality.status_code == 200 and qdata.get("verdict") == "pass" else "WARN", f"score={qdata.get('score')} verdict={qdata.get('verdict')}", issues=qdata.get("issues", []))

    audit = client.post("/api/chapter/audit", json={"project": project, "chapter_id": chapter["id"], "draft": draft, "instruction": "重点检查称谓、时代工具、人物知情边界和是否过早定罪。"})
    adata = audit.json() if audit.status_code == 200 else {}
    if audit.status_code == 200:
        recorder.add("真实模型·连续性审计", "PASS", f"score={adata.get('score')} verdict={adata.get('verdict')}", issues=adata.get("issues", []), fallback=adata.get("fallback"))
    else:
        recorder.add("真实模型·连续性审计", "FAIL", audit.text[:500])

    # If either gate asks for revision, exercise whole-selection rewrite once.
    if qdata.get("verdict") == "revise" or adata.get("verdict") == "revise":
        issues = json.dumps((qdata.get("issues", []) + adata.get("issues", []))[:8], ensure_ascii=False)
        revised, rdone, _ = generation_call(
            client,
            project,
            chapter["id"],
            "rewrite",
            "根据以下审计问题修订整章。保留已经发生的事件，不增加新往事，不改变历史时间点，只修正长度、称谓、时代工具、知识泄漏和明显AI套话。问题：" + issues,
            max(300, compact_len(draft)),
            selection=draft,
        )
        if compact_len(revised) >= 300:
            draft = revised
            chapter["content"] = draft
            (out_dir / "01_咸阳仓牍_第一章_真实SiliconFlow_审计修订版.md").write_text("# 第一章 仓门旧牍（审计修订版）\n\n" + draft + "\n", encoding="utf-8")
            recorder.add("真实模型·整章审计修订", "PASS", f"{compact_len(draft)} 字；repair={rdone.get('length_repaired')}")
        else:
            recorder.add("真实模型·整章审计修订", "FAIL", f"修订稿仅 {compact_len(revised)} 字")

    memory = client.post("/api/chapter/memory", json={"project": project, "chapter_id": chapter["id"], "draft": draft, "instruction": ""})
    if memory.status_code == 200:
        mdata = memory.json()
        memory_facts = mdata.get("facts", []) if isinstance(mdata.get("facts"), list) else []
        memory_status = "PASS" if not mdata.get("fallback") and memory_facts else "WARN"
        recorder.add(
            "真实模型·章节记忆提取",
            memory_status,
            mdata.get("summary", "")[:180],
            fallback=mdata.get("fallback"),
            memory_mode=mdata.get("memory_mode", "full"),
            fact_count=len(memory_facts),
            warnings=mdata.get("warnings", []),
        )
        applied = client.post("/api/chapter/memory/apply", json={"project": project, "chapter_id": chapter["id"], "result": mdata})
        if applied.status_code == 200:
            project = applied.json()["project"]
            fact_count = len(project.get("memory", {}).get("facts", []))
            recorder.add(
                "记忆回灌/证据投影",
                "PASS" if fact_count else "WARN",
                f"facts={fact_count}",
                warnings=applied.json().get("warnings", []),
            )
        else:
            recorder.add("记忆回灌/证据投影", "FAIL", applied.text[:500])
    else:
        recorder.add("真实模型·章节记忆提取", "FAIL", memory.text[:500])

    graph = client.post("/api/knowledge/graph", json={"project": project})
    if graph.status_code == 200:
        g = graph.json()
        recorder.add("知识图谱投影", "PASS" if g.get("nodes") else "FAIL", f"nodes={len(g.get('nodes', []))}, edges={len(g.get('edges', []))}")
    else:
        recorder.add("知识图谱投影", "FAIL", graph.text[:500])

    # Second chapter proves accepted memory is actually retrieved and used.
    ch2 = new_chapter("第二章 第三册")
    ch2["scene_goal"] = "承接第一章：第三册开封前有人试图用程序理由拖延，沈砚必须守住昨夜封存形成的证据链。"
    project["chapters"].append(ch2)
    p2 = client.post("/api/chapter/plan", json={"project": project, "chapter_id": ch2["id"], "instruction": "承接第一章，不重复核对第二册；让第三册是否开封成为本章即时冲突。"})
    if p2.status_code == 200:
        pdata = p2.json()
        for key in ch2["plan"]:
            if key in pdata:
                ch2["plan"][key] = pdata[key]
        recorder.add("真实模型·第二章细纲", "PASS" if not pdata.get("fallback") else "WARN", pdata.get("goal", ""))
    else:
        recorder.add("真实模型·第二章细纲", "FAIL", p2.text[:400])

    preview2 = client.post("/api/prompt/preview", json={"project": project, "chapter_id": ch2["id"], "mode": "continue", "instruction": "承接第一章结果，写第三册开封前的程序冲突。", "selection": "", "target_words": 750})
    retrieved = preview2.json().get("retrieved_memories", []) if preview2.status_code == 200 else []
    recorder.add("跨章相关记忆召回", "PASS" if retrieved else "WARN", f"retrieved={len(retrieved)}")

    draft2, done2, _ = generation_call(client, project, ch2["id"], "continue", "承接第一章已经确认的结果。第三册还没有开，不要重复第一章核第二册的过程；推进到新的程序冲突并留下下一步钩子。", 750)
    ch2["content"] = draft2
    (out_dir / "02_咸阳仓牍_第二章_真实SiliconFlow.md").write_text("# 第二章 第三册\n\n" + draft2 + "\n", encoding="utf-8")
    recorder.add("真实模型·历史正文第二章/续写", "PASS" if compact_len(draft2) >= 560 else "FAIL", f"{compact_len(draft2)} 字；repair={done2.get('length_repaired')}")

    # Exercise selected rewrite and selected expansion with real cloud model.
    selection = draft2[: min(len(draft2), 220)]
    rewritten, _, _ = generation_call(client, project, ch2["id"], "rewrite", "只改写选中段：删掉抽象判断，用动作和物件表现压力，不改变事实。", max(100, compact_len(selection)), selection=selection)
    recorder.add("真实模型·重写选中", "PASS" if 60 <= compact_len(rewritten) <= max(500, compact_len(selection) * 3) else "FAIL", f"{compact_len(rewritten)} 字")
    (out_dir / "03_重写选中_真实SiliconFlow.txt").write_text(rewritten, encoding="utf-8")

    seed = "沈砚检查第三册封泥，没有立刻拆开。"
    expanded, _, _ = generation_call(client, project, ch2["id"], "expand", "扩写选中段为约300字现场动作，包含封泥、绳结、仓吏在场核验；不能增加幕后真相。", 300, selection=seed)
    recorder.add("真实模型·扩写选中", "PASS" if compact_len(expanded) >= 180 else "FAIL", f"{compact_len(expanded)} 字")
    (out_dir / "04_扩写选中_真实SiliconFlow.txt").write_text(expanded, encoding="utf-8")

    combined = "# 《咸阳仓牍》真实 SiliconFlow E2E 测试稿\n\n## 第一章 仓门旧牍\n\n" + draft + "\n\n## 第二章 第三册\n\n" + draft2 + "\n"
    (out_dir / "咸阳仓牍_真实SiliconFlow_E2E两章.md").write_text(combined, encoding="utf-8")

    health = client.post("/api/project/manuscript-health", json={"project": project})
    if health.status_code == 200:
        h = health.json()
        recorder.add("全稿体检", "PASS", f"score={h.get('score', h.get('health_score', 'n/a'))}", report=h)
    else:
        recorder.add("全稿体检", "FAIL", health.text[:400])

    return project


def run_canon_pipeline(client: TestClient, cfg: dict[str, Any], recorder: Recorder, out_dir: Path) -> None:
    project = default_project("canon-cloud", "雾港来客·同人机制验收", "cloud")
    project["settings"].update(cfg)
    project["fanfic"]["enabled"] = True
    project["fanfic"]["mode"] = "canon"
    project["references"] = [
        {"id": "canon-luoxian", "name": "洛弦原作资料", "kind": "canon", "source_work": "雾港来客", "text": CANON_SOURCE * 5, "enabled": True}
    ]
    project["characters"] = [{"id": "luoxian", "name": "洛弦", "role": "主要角色"}]
    response = client.post("/api/canon/analyze-character", json={"project": project, "character_id": "luoxian", "reference_ids": ["canon-luoxian"]})
    if response.status_code != 200:
        recorder.add("真实模型·Canon Profile", "FAIL", response.text[:600])
        return
    data = response.json()
    profile = data.get("profile", {})
    explicit = data.get("explicit_facts", [])
    ok = bool(profile.get("core_personality")) and bool(profile.get("must_not")) and bool(explicit)
    recorder.add("真实模型·Canon Profile", "PASS" if ok else "FAIL", f"must_not={len(profile.get('must_not', []))}, explicit={len(explicit)}")
    profile["user_verified"] = True  # synthetic source above is the test authority and has been authored for this QA.
    profile["enabled"] = True
    project["characters"][0]["canon_profile"] = profile
    chapter = project["chapters"][0]
    chapter["scene_goal"] = "洛弦第一次与陌生调查员见面，只交换可核验记录，不迅速交付信任。"
    draft, done, _ = generation_call(client, project, chapter["id"], "instruction", "写洛弦第一次接触陌生调查员。她可以合作核验一张航运单，但不能突然信任、透露私密过去或表现出格斗/超能力。只写正文。", 500)
    (out_dir / "05_CanonLock_真实SiliconFlow测试稿.md").write_text("# Canon Lock 真实 SiliconFlow 测试稿\n\n" + draft + "\n", encoding="utf-8")
    recorder.add("真实模型·Canon Lock 生成", "PASS" if compact_len(draft) >= 350 else "FAIL", f"{compact_len(draft)} 字；repair={done.get('length_repaired')}")
    audit = client.post("/api/canon/audit", json={"project": project, "chapter_id": chapter["id"], "draft": draft})
    if audit.status_code == 200:
        adata = audit.json()
        score = int(adata.get("score", 0) or 0)
        recorder.add("真实模型·OOC/Canon 审校", "PASS" if score >= 75 and adata.get("verdict") != "block" else "WARN", f"score={score} verdict={adata.get('verdict')}", issues=adata.get("issues", []))
    else:
        recorder.add("真实模型·OOC/Canon 审校", "FAIL", audit.text[:500])


def run_creative_planning(client: TestClient, project: dict[str, Any], recorder: Recorder) -> None:
    ideas = client.post("/api/ideas", json={"project": project, "kind": "next", "instruction": "给出第一卷后半段三个不同的政治冲突方向，不提前灭六国。"})
    if ideas.status_code == 200:
        data = ideas.json()
        recorder.add("真实模型·灵感推荐", "PASS" if not data.get("fallback") and len(data.get("options", [])) >= 3 else "WARN", f"options={len(data.get('options', []))} fallback={data.get('fallback')}")
    else:
        recorder.add("真实模型·灵感推荐", "FAIL", ideas.text[:500])

    incubator = client.post(
        "/api/incubator",
        json={
            "project": project,
            "seed": "战国末期，一名只懂现代公共治理方法、没有工业外挂的穿越者进入秦国，从一册粮账开始参与制度变革。",
            "preferences": "历史人物不降智；现代知识必须转译为当时工具；重点写制度收益和代价；不要爽文碾压。",
            "story_mode": "long",
            "target_chapters": 12,
        },
    )
    if incubator.status_code == 200:
        data = incubator.json()
        recorder.add("真实模型·灵感孵化", "PASS" if not data.get("fallback") and len(data.get("options", [])) >= 2 else "WARN", f"options={len(data.get('options', []))}")
    else:
        recorder.add("真实模型·灵感孵化", "FAIL", incubator.text[:500])

    master = client.post("/api/planning/master", json={"project": project, "instruction": "规划12章试验卷，第一卷只处理逐客风波前后的入秦与有限制度试点。"})
    if master.status_code == 200:
        data = master.json()
        recorder.add("真实模型·全书分层规划", "PASS" if not data.get("fallback") and data.get("volumes") else "WARN", f"volumes={len(data.get('volumes', []))} fallback={data.get('fallback')}")
    else:
        recorder.add("真实模型·全书分层规划", "FAIL", master.text[:500])


def run_director(client: TestClient, cfg: dict[str, Any], recorder: Recorder, out_dir: Path, timeout_minutes: int) -> None:
    source = historical_project(cfg)
    source["narrative"]["target_chapters"] = 3
    start = client.post(
        "/api/director/start",
        json={
            "source_project": source,
            "seed": "战国末期，沈砚从一册粮账开始进入秦国制度现场。三章测试只写入秦、核账、获得有限试点资格，不解决统一大业。",
            "preferences": "历史人物不降智；称谓准确；现代知识只能用当时可执行形式表达；三章各有不同事件功能。",
            "story_mode": "short",
            "target_chapters": 3,
            "target_words": 500,
            "quality_threshold": 70,
            "max_revision_attempts": 1,
            "continue_on_quality_debt": True,
        },
    )
    if start.status_code != 200:
        recorder.add("真实模型·自动导演启动", "FAIL", start.text[:600])
        return
    payload = start.json()
    task_id = payload["task"]["id"]
    project_id = payload["project"]["id"]
    recorder.add("真实模型·自动导演启动", "PASS", f"task={task_id[:8]} project={project_id[:8]}")
    deadline = time.time() + max(5, timeout_minutes) * 60
    last_status = ""
    while time.time() < deadline:
        state = client.get(f"/api/director/tasks/{task_id}")
        if state.status_code != 200:
            recorder.add("真实模型·自动导演轮询", "FAIL", state.text[:500])
            return
        task = state.json()
        status = str(task.get("status", ""))
        if status != last_status:
            print(f"[director] status={status} phase={task.get('phase')} completed={task.get('completed_chapters', 0)}", flush=True)
            last_status = status
        if status == "completed":
            final_project = main.store.get(project_id)
            chapters = final_project.get("chapters", []) if final_project else []
            manuscript = [f"# 《{final_project.get('title', '自动导演测试作')}》真实 SiliconFlow 自动导演三章\n"] if final_project else ["# 自动导演测试作\n"]
            for chapter in chapters:
                manuscript.extend([f"\n## {chapter.get('title', '')}\n", str(chapter.get("content", "")), "\n"])
            (out_dir / "06_自动导演_真实SiliconFlow三章.md").write_text("\n".join(manuscript), encoding="utf-8")
            ok = len(chapters) >= 3 and all(compact_len(ch.get("content", "")) >= 250 for ch in chapters[:3])
            recorder.add("真实模型·自动导演3章完成", "PASS" if ok else "FAIL", f"chapters={len(chapters)} quality_debts={len(task.get('quality_debts', []))}", task=task)
            return
        if status in {"paused", "failed"} and task.get("error"):
            recorder.add("真实模型·自动导演3章完成", "FAIL", f"status={status} error={task.get('error')}", task=task)
            return
        time.sleep(2.0)
    recorder.add("真实模型·自动导演3章完成", "FAIL", f"超过 {timeout_minutes} 分钟仍未完成")


def markdown_report(report: dict[str, Any]) -> str:
    lines = [
        f"# InkForge {report.get('app_version', main.APP_VERSION)} Cloud Full E2E QA",
        "",
        f"- 开始：{report.get('started_at', '')}",
        f"- 结束：{report.get('finished_at', '')}",
        f"- 模型：{report.get('model', '')}",
        f"- Base URL：{report.get('base_url', '')}",
        f"- 结果：**{report.get('overall', '')}**",
        "- API Key：未写入报告、项目 JSON 或持久测试数据；仅由进程环境读取。",
        "",
        "## 步骤",
        "",
        "| 状态 | 测试 | 详情 |",
        "| --- | --- | --- |",
    ]
    for step in report.get("steps", []):
        detail = str(step.get("detail", "")).replace("|", "\\|").replace("\n", " ")[:400]
        lines.append(f"| {step.get('status','')} | {step.get('name','')} | {detail} |")
    lines.extend(["", "## 说明", "", "FAIL 表示功能链路或硬约束未通过；WARN 表示功能可用但模型输出触发了回退、质量门禁或需要作者复核。"])
    return "\n".join(lines) + "\n"


def main_cli() -> int:
    parser = argparse.ArgumentParser(description="InkForge real SiliconFlow full E2E QA")
    parser.add_argument("--base-url", default=BASE_URL)
    parser.add_argument("--skip-pytest", action="store_true")
    parser.add_argument("--skip-director", action="store_true")
    parser.add_argument("--director-timeout-minutes", type=int, default=30)
    args = parser.parse_args()

    key = os.environ.get("INKFORGE_SILICONFLOW_API_KEY", "").strip()
    if not key:
        key = getpass.getpass("Temporary SiliconFlow API Key (hidden): ").strip()
    if not key:
        print("API Key is empty.")
        return 2
    os.environ["INKFORGE_SILICONFLOW_API_KEY"] = key

    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    run_dir = ROOT / "qa" / "cloud_runs" / stamp
    out_dir = run_dir / "outputs"
    out_dir.mkdir(parents=True, exist_ok=True)
    recorder = Recorder(key)
    started = datetime.now().astimezone().isoformat()
    cfg = cloud_settings(args.base_url)

    try:
        if not args.skip_pytest:
            env = dict(os.environ)
            # Local tests must not accidentally use the real key even if a test creates SiliconFlow settings.
            env.pop("INKFORGE_SILICONFLOW_API_KEY", None)
            proc = subprocess.run(
                [sys.executable, "-m", "pytest", "-q"],
                cwd=ROOT,
                env=env,
                capture_output=True,
                text=True,
                timeout=180,
            )
            tail = (proc.stdout + "\n" + proc.stderr).strip()[-1200:]
            recorder.add("本地自动测试全集", "PASS" if proc.returncode == 0 else "FAIL", tail)

        asyncio.run(direct_provider_checks(cfg, recorder, out_dir))

        # Isolated persistent store. The project settings contain no api_key; provider auth comes from env.
        original_store, original_root = main.store, main.ROOT
        main.store = ProjectStore(run_dir / "runtime" / "qa.db")
        main.ROOT = run_dir / "runtime"
        main.ROOT.mkdir(parents=True, exist_ok=True)
        try:
            with TestClient(main.app) as client:
                health = client.get("/api/health")
                recorder.add("FastAPI 健康检查", "PASS" if health.status_code == 200 else "FAIL", health.text[:300])
                provider = client.post("/api/provider/summary", json=cfg)
                pdata = provider.json() if provider.status_code == 200 else {}
                recorder.add("环境变量 Key 注入/不落项目", "PASS" if pdata.get("has_api_key") and pdata.get("api_key_source") == "environment" and not cfg.get("api_key") else "FAIL", json.dumps(pdata, ensure_ascii=False))

                try:
                    project = run_historical_pipeline(client, cfg, recorder, out_dir)
                except Exception as exc:  # noqa: BLE001 - keep report alive after a failed cloud stage
                    recorder.add("历史小说完整管线", "FAIL", f"{type(exc).__name__}: {exc}")
                    project = historical_project(cfg)
                try:
                    run_canon_pipeline(client, cfg, recorder, out_dir)
                except Exception as exc:  # noqa: BLE001
                    recorder.add("Canon 完整管线", "FAIL", f"{type(exc).__name__}: {exc}")
                try:
                    run_creative_planning(client, project, recorder)
                except Exception as exc:  # noqa: BLE001
                    recorder.add("灵感/规划完整管线", "FAIL", f"{type(exc).__name__}: {exc}")
                if not args.skip_director:
                    try:
                        run_director(client, cfg, recorder, out_dir, args.director_timeout_minutes)
                    except Exception as exc:  # noqa: BLE001
                        recorder.add("自动导演完整管线", "FAIL", f"{type(exc).__name__}: {exc}")
        finally:
            main.store, main.ROOT = original_store, original_root

        statuses = [step.get("status") for step in recorder.steps]
        overall = "FAIL" if "FAIL" in statuses else "WARN" if "WARN" in statuses else "PASS"
        report = {
            "schema": 1,
            "app_version": main.APP_VERSION,
            "started_at": started,
            "finished_at": datetime.now().astimezone().isoformat(),
            "model": MODEL,
            "base_url": args.base_url,
            "overall": overall,
            "steps": scrub(recorder.steps, key),
        }
        (run_dir / "qa-report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        (run_dir / "QA_REPORT.md").write_text(markdown_report(report), encoding="utf-8")

        # Runtime SQLite may transiently hold generated project data, but never the key.
        # It is excluded from the shareable artifact to keep the report compact.
        runtime_dir = run_dir / "runtime"
        if runtime_dir.exists():
            shutil.rmtree(runtime_dir, ignore_errors=True)

        zip_path = run_dir.parent / f"InkForge_Cloud_QA_{stamp}.zip"
        with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
            for path in sorted(run_dir.rglob("*")):
                if path.is_file():
                    zf.write(path, path.relative_to(run_dir))
        print("\n=== Cloud Full E2E finished ===")
        print(f"Overall: {overall}")
        print(f"Report: {run_dir / 'QA_REPORT.md'}")
        print(f"Shareable ZIP: {zip_path}")
        print("The API key is not included in the report ZIP.")
        return 0 if overall != "FAIL" else 1
    finally:
        os.environ.pop("INKFORGE_SILICONFLOW_API_KEY", None)
        key = ""


if __name__ == "__main__":
    raise SystemExit(main_cli())

