from __future__ import annotations

import hashlib
import ipaddress
import json
import re
import socket
import uuid
from copy import deepcopy
from datetime import datetime, timezone
from html import unescape
from typing import Any
from urllib.parse import quote_plus, urlparse
from xml.etree import ElementTree

import httpx


SOURCE_TIERS = {
    "S": "原作正文、作者或出版社直接材料",
    "A": "官方网站、官方角色介绍、官方访谈与设定集",
    "B": "高质量百科、逐条注明出处的资料整理",
    "C": "普通媒体介绍、社区整理",
    "D": "粉丝讨论、二创、无法追溯的转述",
}
ROLE_FAMILIES = {
    "research": "reasoning",
    "planning": "reasoning",
    "extraction": "reasoning",
    "critic": "reasoning",
    "prose": "prose",
    "revision": "prose",
}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _id(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4()}"


def content_hash(value: Any) -> str:
    raw = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def ensure_professional_defaults(project: dict[str, Any]) -> dict[str, Any]:
    governance = project.setdefault("governance", {})
    governance.setdefault("schema_version", 1)
    governance.setdefault("revisions", {})
    for key in ("story_bible", "characters", "canon", "world", "planning", "style", "research"):
        governance["revisions"].setdefault(key, 1)
    governance.setdefault("assets", [])

    research = project.setdefault("research", {})
    research.setdefault("schema_version", 1)
    research.setdefault("sources", [])
    research.setdefault("claims", [])
    research.setdefault("conflicts", [])
    research.setdefault("dossiers", [])

    state = project.setdefault("narrative_state", {})
    state.setdefault("schema_version", 1)
    state.setdefault("events", [])
    state.setdefault("character_state", {})
    state.setdefault("relationship_state", {})

    voice = project.setdefault("voice_lab", {})
    voice.setdefault("schema_version", 1)
    voice.setdefault("samples", [])
    voice.setdefault("profiles", {})
    voice.setdefault("tests", [])

    editorial = project.setdefault("editorial", {})
    editorial.setdefault("schema_version", 1)
    editorial.setdefault("drafts", [])
    editorial.setdefault("reviews", [])
    editorial.setdefault("revisions", [])
    editorial.setdefault("finalizations", [])
    return project


def revision_snapshot(project: dict[str, Any], kinds: list[str] | None = None) -> dict[str, int]:
    ensure_professional_defaults(project)
    revisions = project["governance"]["revisions"]
    selected = kinds or list(revisions)
    return {key: int(revisions.get(key, 0) or 0) for key in selected}


def bump_authority(project: dict[str, Any], kind: str, reason: str = "") -> dict[str, Any]:
    ensure_professional_defaults(project)
    revisions = project["governance"]["revisions"]
    revisions[kind] = int(revisions.get(kind, 0) or 0) + 1
    stale: list[str] = []
    for asset in project["governance"]["assets"]:
        dependencies = asset.get("dependency_snapshot", {})
        if kind in dependencies and int(dependencies[kind]) != revisions[kind]:
            asset["status"] = "stale"
            asset["stale_reason"] = reason or f"{kind} 已更新"
            stale.append(str(asset.get("id", "")))
    return {"kind": kind, "revision": revisions[kind], "stale_assets": stale}


def register_asset(
    project: dict[str, Any], *, kind: str, content: Any, authority: str = "candidate",
    dependencies: list[str] | None = None, source_ids: list[str] | None = None,
) -> dict[str, Any]:
    ensure_professional_defaults(project)
    asset = {
        "id": _id("asset"), "kind": kind, "version": 1,
        "authority": authority, "status": "current", "content_hash": content_hash(content),
        "dependency_snapshot": revision_snapshot(project, dependencies),
        "source_ids": list(dict.fromkeys(source_ids or [])), "created_at": _now(),
    }
    project["governance"]["assets"].append(asset)
    return asset


def governance_report(project: dict[str, Any]) -> dict[str, Any]:
    ensure_professional_defaults(project)
    assets = project["governance"]["assets"]
    return {
        "revisions": revision_snapshot(project),
        "current": sum(item.get("status") == "current" for item in assets),
        "stale": [item for item in assets if item.get("status") == "stale"],
        "assets": assets,
    }


def add_research_source(project: dict[str, Any], item: dict[str, Any]) -> dict[str, Any]:
    ensure_professional_defaults(project)
    text = re.sub(r"\s+", " ", str(item.get("text", ""))).strip()
    source = {
        "id": str(item.get("id") or _id("source")),
        "title": str(item.get("title", "")).strip()[:300],
        "url": str(item.get("url", "")).strip(),
        "publisher": str(item.get("publisher", "")).strip()[:200],
        "tier": str(item.get("tier", "C")).upper() if str(item.get("tier", "C")).upper() in SOURCE_TIERS else "C",
        "text": text[:120_000], "retrieved_at": str(item.get("retrieved_at") or _now()),
        "content_hash": content_hash(text), "status": "current",
    }
    project["research"]["sources"].append(source)
    bump_authority(project, "research", f"新增资料：{source['title']}")
    return source


def approve_claim(project: dict[str, Any], item: dict[str, Any]) -> dict[str, Any]:
    ensure_professional_defaults(project)
    source_id = str(item.get("source_id", ""))
    source = next((value for value in project["research"]["sources"] if value.get("id") == source_id), None)
    if not source:
        raise ValueError("考据结论必须绑定已保存的资料来源")
    evidence = str(item.get("evidence", "")).strip()
    if not evidence or evidence not in str(source.get("text", "")):
        raise ValueError("证据必须是资料正文中可逐字定位的短句")
    claim = {
        "id": str(item.get("id") or _id("claim")), "subject": str(item.get("subject", "")).strip(),
        "predicate": str(item.get("predicate", "")).strip(), "value": str(item.get("value", "")).strip(),
        "certainty": str(item.get("certainty", "explicit")), "source_id": source_id,
        "source_tier": source.get("tier", "C"), "evidence": evidence,
        "status": "approved", "approved_at": _now(),
    }
    if not claim["subject"] or not claim["predicate"] or not claim["value"]:
        raise ValueError("结论必须包含 subject、predicate 和 value")
    project["research"]["claims"] = [
        value for value in project["research"]["claims"]
        if str(value.get("id", "")) != claim["id"]
    ]
    for existing in project["research"]["claims"]:
        if existing.get("status") == "approved" and existing.get("subject") == claim["subject"] and existing.get("predicate") == claim["predicate"] and existing.get("value") != claim["value"]:
            project["research"]["conflicts"].append({
                "id": _id("conflict"), "left_claim_id": existing.get("id"),
                "right_claim_id": claim["id"], "status": "open", "created_at": _now(),
            })
    project["research"]["claims"].append(claim)
    bump_authority(project, "canon", f"批准考据结论：{claim['subject']}")
    return claim


def build_character_dossier(project: dict[str, Any], character: str) -> dict[str, Any]:
    ensure_professional_defaults(project)
    claims = [item for item in project["research"]["claims"] if item.get("status") == "approved" and item.get("subject") == character]
    grouped: dict[str, list[dict[str, Any]]] = {}
    for claim in claims:
        grouped.setdefault(str(claim.get("predicate", "未分类")), []).append(claim)
    dossier = {
        "id": _id("dossier"), "character": character, "status": "candidate",
        "sections": grouped, "source_ids": list(dict.fromkeys(str(item.get("source_id")) for item in claims)),
        "decision_cases": [], "created_at": _now(),
    }
    project["research"]["dossiers"].append(dossier)
    register_asset(project, kind="character_dossier", content=dossier, authority="candidate", dependencies=["research", "canon"], source_ids=dossier["source_ids"])
    return dossier


def add_narrative_event(project: dict[str, Any], item: dict[str, Any]) -> dict[str, Any]:
    ensure_professional_defaults(project)
    kind = str(item.get("kind", "character"))
    if kind not in {"character", "relationship"}:
        raise ValueError("事件 kind 只能是 character 或 relationship")
    evidence_type = str(item.get("evidence_type") or ("chapter" if item.get("evidence") else "author"))
    event = {
        "id": str(item.get("id") or _id("event")), "kind": kind,
        "chapter_id": str(item.get("chapter_id", "")), "chapter_number": int(item.get("chapter_number", 0) or 0),
        "actors": [str(value).strip() for value in item.get("actors", []) if str(value).strip()],
        "summary": str(item.get("summary", "")).strip(), "evidence": str(item.get("evidence", "")).strip(),
        "evidence_type": evidence_type,
        "deltas": deepcopy(item.get("deltas", {})), "status": str(item.get("status", "confirmed")),
        "created_at": _now(),
    }
    if not event["actors"] or not event["summary"]:
        raise ValueError("事件必须包含 actors 和 summary")
    if evidence_type == "chapter":
        chapter = next((value for value in project.get("chapters", []) if str(value.get("id")) == event["chapter_id"]), None)
        if not chapter or not event["evidence"] or event["evidence"] not in str(chapter.get("content", "")):
            raise ValueError("正文事件的证据必须能在对应章节中逐字定位；人工确定的状态请使用 evidence_type=author")
    project["narrative_state"]["events"].append(event)
    rebuild_narrative_state(project)
    return event


def rebuild_narrative_state(project: dict[str, Any]) -> dict[str, Any]:
    ensure_professional_defaults(project)
    characters: dict[str, dict[str, Any]] = {}
    relationships: dict[str, dict[str, Any]] = {}
    ordered = sorted(project["narrative_state"]["events"], key=lambda value: (int(value.get("chapter_number", 0)), str(value.get("created_at", ""))))
    for event in ordered:
        if event.get("status") != "confirmed":
            continue
        if event.get("kind") == "character":
            for actor in event.get("actors", []):
                state = characters.setdefault(actor, {"events": [], "traits": {}, "last_chapter": 0})
                state["events"].append(event["id"])
                state["traits"].update(event.get("deltas", {}))
                state["last_chapter"] = max(state["last_chapter"], int(event.get("chapter_number", 0)))
        elif len(event.get("actors", [])) >= 2:
            pair = " ↔ ".join(sorted(event["actors"][:2]))
            state = relationships.setdefault(pair, {"events": [], "dimensions": {}, "last_chapter": 0})
            state["events"].append(event["id"])
            state["dimensions"].update(event.get("deltas", {}))
            state["last_chapter"] = max(state["last_chapter"], int(event.get("chapter_number", 0)))
    project["narrative_state"]["character_state"] = characters
    project["narrative_state"]["relationship_state"] = relationships
    return {"character_state": characters, "relationship_state": relationships}


def _voice_metrics(text: str) -> dict[str, Any]:
    sentences = [value.strip() for value in re.split(r"[。！？!?]+", text) if value.strip()]
    dialogue = re.findall(r"[“\"]([^”\"]+)[”\"]", text)
    chars = max(1, len(re.sub(r"\s+", "", text)))
    return {
        "characters": chars, "sentence_count": len(sentences),
        "mean_sentence_chars": round(sum(map(len, sentences)) / max(1, len(sentences)), 2),
        "dialogue_ratio": round(sum(map(len, dialogue)) / chars, 3),
        "question_rate": round((text.count("？") + text.count("?")) / max(1, len(sentences)), 3),
        "ellipsis_rate": round((text.count("……") + text.count("…")) / max(1, len(sentences)), 3),
    }


def add_voice_sample(project: dict[str, Any], item: dict[str, Any]) -> dict[str, Any]:
    ensure_professional_defaults(project)
    text = str(item.get("text", "")).strip()
    character = str(item.get("character", "")).strip()
    if not character or len(text) < 10:
        raise ValueError("声纹样本需要人物名和至少 10 字原文")
    sample = {
        "id": _id("voice"), "character": character, "text": text[:20_000],
        "polarity": str(item.get("polarity", "positive")), "source_id": str(item.get("source_id", "")),
        "context": str(item.get("context", "")), "metrics": _voice_metrics(text), "created_at": _now(),
    }
    project["voice_lab"]["samples"].append(sample)
    positive = [value for value in project["voice_lab"]["samples"] if value.get("character") == character and value.get("polarity") == "positive"]
    profile = {
        "character": character, "sample_ids": [value["id"] for value in positive],
        "sample_count": len(positive), "updated_at": _now(),
        "mean_sentence_chars": round(sum(value["metrics"]["mean_sentence_chars"] for value in positive) / max(1, len(positive)), 2),
        "dialogue_ratio": round(sum(value["metrics"]["dialogue_ratio"] for value in positive) / max(1, len(positive)), 3),
    }
    project["voice_lab"]["profiles"][character] = profile
    return sample


def render_professional_context(project: dict[str, Any], active_names: set[str] | None = None) -> str:
    ensure_professional_defaults(project)
    names = active_names or set()
    lines: list[str] = []
    for name, state in project["narrative_state"]["character_state"].items():
        if not names or name in names:
            lines.append(f"人物状态｜{name}｜{json.dumps(state.get('traits', {}), ensure_ascii=False)}")
    for pair, state in project["narrative_state"]["relationship_state"].items():
        if not names or any(name in pair for name in names):
            lines.append(f"关系状态｜{pair}｜{json.dumps(state.get('dimensions', {}), ensure_ascii=False)}")
    for name, profile in project["voice_lab"]["profiles"].items():
        if not names or name in names:
            lines.append(f"对白声纹｜{name}｜均句长 {profile.get('mean_sentence_chars')}，对白占比 {profile.get('dialogue_ratio')}；仅作节奏约束，不复刻原句。")
    return "\n".join(lines)


def create_editorial_draft(project: dict[str, Any], chapter_id: str, content: str, source: str = "generation") -> dict[str, Any]:
    ensure_professional_defaults(project)
    if not content.strip():
        raise ValueError("候选稿不能为空")
    draft = {
        "id": _id("draft"), "chapter_id": chapter_id, "version": 1, "status": "draft",
        "content": content, "content_hash": content_hash(content), "source": source,
        "authority_snapshot": revision_snapshot(project), "created_at": _now(), "updated_at": _now(),
    }
    project["editorial"]["drafts"].append(draft)
    return draft


def add_editorial_review(project: dict[str, Any], draft_id: str, report: dict[str, Any]) -> dict[str, Any]:
    ensure_professional_defaults(project)
    draft = next((value for value in project["editorial"]["drafts"] if value.get("id") == draft_id), None)
    if not draft:
        raise ValueError("候选稿不存在")
    review = {"id": _id("review"), "draft_id": draft_id, "draft_hash": draft["content_hash"], "report": deepcopy(report), "created_at": _now()}
    project["editorial"]["reviews"].append(review)
    draft["status"] = "reviewed"
    return review


def create_revision_proposal(project: dict[str, Any], draft_id: str, content: str, review_id: str = "") -> dict[str, Any]:
    ensure_professional_defaults(project)
    draft = next((value for value in project["editorial"]["drafts"] if value.get("id") == draft_id), None)
    if not draft:
        raise ValueError("原候选稿不存在")
    proposal = {
        "id": _id("revision"), "draft_id": draft_id, "review_id": review_id,
        "base_hash": draft["content_hash"], "content": content, "content_hash": content_hash(content),
        "status": "proposed", "created_at": _now(),
    }
    project["editorial"]["revisions"].append(proposal)
    return proposal


def finalize_editorial(project: dict[str, Any], draft_id: str, revision_id: str = "") -> dict[str, Any]:
    ensure_professional_defaults(project)
    draft = next((value for value in project["editorial"]["drafts"] if value.get("id") == draft_id), None)
    if not draft:
        raise ValueError("候选稿不存在")
    content = draft["content"]
    if revision_id:
        revision = next((value for value in project["editorial"]["revisions"] if value.get("id") == revision_id and value.get("draft_id") == draft_id), None)
        if not revision:
            raise ValueError("修订提案不存在或不属于该候选稿")
        content = revision["content"]
        revision["status"] = "accepted"
    chapter = next((value for value in project.get("chapters", []) if str(value.get("id")) == str(draft["chapter_id"])), None)
    if not chapter:
        raise ValueError("目标章节不存在")
    if any(value.get("draft_id") == draft_id for value in project["editorial"]["finalizations"]):
        raise ValueError("该候选稿已经定稿；定稿记录不可覆盖")
    finalization = {
        "id": _id("final"), "draft_id": draft_id, "revision_id": revision_id,
        "chapter_id": draft["chapter_id"], "content": content, "content_hash": content_hash(content),
        "authority_snapshot": revision_snapshot(project), "finalized_at": _now(),
    }
    project["editorial"]["finalizations"].append(finalization)
    chapter["content"] = content
    draft["status"] = "finalized"
    return finalization


def _public_url(url: str) -> None:
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ValueError("只允许公开的 http/https 资料地址")
    if parsed.username or parsed.password:
        raise ValueError("资料地址不能包含账号或密码")
    if parsed.hostname.lower() in {"localhost", "localhost.localdomain"}:
        raise ValueError("不允许访问本机地址")
    try:
        addresses = socket.getaddrinfo(parsed.hostname, parsed.port or (443 if parsed.scheme == "https" else 80))
        for address in addresses:
            ip = ipaddress.ip_address(address[4][0])
            if not ip.is_global:
                raise ValueError("不允许访问内网或保留地址")
    except socket.gaierror as exc:
        raise ValueError("资料地址无法解析") from exc


def html_text(raw: str) -> str:
    value = re.sub(r"(?is)<(script|style|noscript).*?>.*?</\1>", " ", raw)
    value = re.sub(r"(?s)<[^>]+>", " ", value)
    return re.sub(r"\s+", " ", unescape(value)).strip()


async def search_sources(settings: dict[str, Any], query: str, limit: int = 8) -> list[dict[str, Any]]:
    research = settings.get("research", {}) if isinstance(settings.get("research"), dict) else {}
    provider = str(research.get("provider", "bing_rss")).lower()
    timeout = httpx.Timeout(20.0)
    async with httpx.AsyncClient(
        timeout=timeout,
        follow_redirects=True,
        headers={"User-Agent": "InkForge-Research/0.23 (+local novel research workspace)"},
    ) as client:
        if provider == "searxng":
            endpoint = str(research.get("searxng_url", "")).rstrip("/")
            _public_url(endpoint)
            response = await client.get(f"{endpoint}/search", params={"q": query, "format": "json"})
            response.raise_for_status()
            rows = response.json().get("results", [])
            return [{"title": row.get("title", ""), "url": row.get("url", ""), "snippet": row.get("content", ""), "provider": provider} for row in rows[:limit]]
        if provider == "brave":
            token = str(research.get("brave_api_key", ""))
            if not token:
                raise ValueError("尚未配置 Brave Search API Key")
            response = await client.get("https://api.search.brave.com/res/v1/web/search", params={"q": query, "count": limit}, headers={"X-Subscription-Token": token})
            response.raise_for_status()
            rows = response.json().get("web", {}).get("results", [])
            return [{"title": row.get("title", ""), "url": row.get("url", ""), "snippet": row.get("description", ""), "provider": provider} for row in rows[:limit]]
        if provider == "wikipedia":
            response = await client.get("https://zh.wikipedia.org/w/api.php", params={"action": "query", "list": "search", "srsearch": query, "format": "json", "utf8": 1, "srlimit": limit})
            if response.status_code < 400:
                rows = response.json().get("query", {}).get("search", [])
                return [{"title": row.get("title", ""), "url": f"https://zh.wikipedia.org/wiki/{quote_plus(str(row.get('title', '')))}", "snippet": html_text(str(row.get("snippet", ""))), "provider": "wikipedia"} for row in rows[:limit]]
        # Key-free fallback. RSS is intentionally parsed as data rather than
        # rendered HTML, and final pages still pass the public-URL guard.
        response = await client.get("https://www.bing.com/search", params={"q": query, "format": "rss"})
        response.raise_for_status()
        root = ElementTree.fromstring(response.text)
        rows = root.findall(".//item")
        return [{
            "title": str(row.findtext("title") or ""),
            "url": str(row.findtext("link") or ""),
            "snippet": html_text(str(row.findtext("description") or "")),
            "provider": "bing_rss",
        } for row in rows[:limit]]


async def fetch_public_source(url: str) -> dict[str, str]:
    _public_url(url)
    async with httpx.AsyncClient(timeout=20.0, follow_redirects=True, headers={"User-Agent": "InkForge-Research/1.0"}) as client:
        response = await client.get(url)
        response.raise_for_status()
        for hop in [*response.history, response]:
            _public_url(str(hop.url))
        if len(response.content) > 3_000_000:
            raise ValueError("资料页面过大，拒绝导入")
        content_type = str(response.headers.get("content-type", "")).lower()
        if not any(value in content_type for value in ("text/", "html", "xml", "json")):
            raise ValueError("该地址不是可提取的文本资料")
        return {"url": str(response.url), "text": html_text(response.text)[:120_000]}
