from __future__ import annotations

import math
import re
import uuid
from copy import deepcopy
from typing import Any


MASTER_PLAN_PROMPT = """你是长篇小说的总导演和总编剧。你的任务是建立足以约束整部长篇的详细故事圣经，不写正文，也不逐章列流水账。
依据作者提供的题材、核心构想、人物、世界规则、历史背景和结局方向，先写有完整因果链的全书详细大纲，再拆成连续分卷。篇幅越长，规划必须越充分；不能用一句口号代替大纲。

返回严格 JSON：
{
  "theme": "全书反复检验的价值命题",
  "reader_promise": "读者持续能获得的核心体验",
  "central_conflict": "贯穿全书且会升级的核心冲突",
  "story_engine": "使剧情能够持续运转的因果机制",
  "ending_state": "终局必须抵达的状态与代价",
  "full_outline": "按故事顺序写成的详细全书大纲，交代开端、升级、关键选择、重大转折、高潮、结局及因果",
  "main_plot": "主线从起点到终局的完整推进链",
  "theme_progression": "主题如何在前中后期被不同事件反复检验并改变答案",
  "pacing_plan": "全书节奏、高潮密度、缓冲段和信息揭示安排",
  "stakes_ladder": ["风险与代价如何逐级升级，4-8条"],
  "major_character_arcs": ["点名人物：起点→关键选择→关系或信念变化→终局，3-8条"],
  "subplots": ["支线名称：作用、推进阶段、与主线交汇点及回收方式，2-8条"],
  "historical_nodes": ["历史架空或时间节点：前置条件、允许改变与不可违背边界，0-10条"],
  "volumes": [{
    "title": "卷名",
    "chapter_count": 6,
    "goal": "本卷结束时必须完成的阶段变化",
    "conflict": "本卷主导矛盾和双方策略",
    "synopsis": "本卷详细剧情梗概，写清起因、连续升级、关键选择、代价和卷末结果",
    "turning_points": ["2-5个改变行动方向的具体转折"],
    "character_arcs": ["本卷涉及人物的状态或关系变化"],
    "subplots": ["本卷推进或回收的支线"],
    "must_keep": ["本卷必须兑现的事实或承诺"],
    "must_avoid": ["本卷不得提前发生或不得违背的事项"],
    "ending_state": "进入下一卷时的人物、资源、局势和悬念状态",
    "bridge_to_next": "本卷结果如何直接触发下一卷"
  }]
}

要求：
1. 分卷总章数应接近指定总章数；每卷建议 4-18 章，短篇可更少。
2. 相邻卷必须有明确因果，后卷由前卷结果触发。
3. 不使用“主角”“反派”“某势力”等占位词；项目已有名字时必须点名。
4. 不把终局秘密提前消耗，不连续堆叠新设定，不靠巧合推动。
5. full_outline必须达到输入指定的目标字数，并与计划总章数匹配；96章长篇不能只写数百字。
6. 每卷synopsis建议180-350字，不能让各卷只更换名词却重复同一目标。
7. 必须恰好生成输入指定的分卷数量，分卷总章数等于计划总章数。
8. 先保证所有JSON完整闭合；只返回 JSON，不要 Markdown。"""


MASTER_CORE_PROMPT = """你是长篇小说的总导演、总编剧和连续性编辑。先建立全书级故事圣经，不写正文，也不拆分卷。
你必须根据计划总章数控制细节密度：长篇不能用几句口号代替大纲。现有总纲是作者意图的重要来源，但你要补齐因果链、人物变化、主题递进、节奏和回收，不得擅自改变硬规则。

只返回严格 JSON：
{
  "theme": "全书反复检验的价值命题",
  "reader_promise": "读者持续获得的核心体验",
  "central_conflict": "贯穿全书且会升级的核心冲突",
  "story_engine": "让剧情可持续运转的因果机制",
  "ending_state": "结局状态、人物选择与不可逆代价",
  "full_outline": "按故事顺序写成的详细全书大纲，必须覆盖开端、阶段升级、关键选择、主要转折、高潮、结局和重要支线回收",
  "main_plot": "主线从起点到终局的完整推进链",
  "theme_progression": "主题在前中后期如何被不同事件检验并改变答案",
  "pacing_plan": "全书节奏、高潮密度、缓冲段和信息揭示安排",
  "stakes_ladder": ["风险与代价逐级升级，4-8条"],
  "major_character_arcs": ["点名人物：起点→关键选择→关系或信念变化→终局，4-8条"],
  "subplots": ["支线名称：作用、阶段、交汇点与回收方式，3-8条"],
  "historical_nodes": ["历史/架空节点：前置条件、可改变范围与边界，3-10条"]
}

要求：
1. full_outline 必须达到输入指定的目标字数，并与计划总章数匹配；96章长篇必须包含清楚的阶段推进，不能只有数百字。
2. 使用作品中已有的人名、地点、组织和事件，不使用“主角”“某势力”等占位词。
3. 每个阶段都写清“前因→行动/博弈→选择→结果→下一阶段的新问题”，避免只列主题口号。
4. 既尊重作者现有大纲，又指出并弥合其中的跳跃；不得把现代知识写成无代价万能答案。
5. 先保证 JSON 完整闭合，只返回 JSON，不要 Markdown。"""


MASTER_VOLUMES_PROMPT = """你是长篇小说的分卷总导演。依据已经完成的全书故事圣经，生成指定数量的详细分卷蓝图，不写正文，也不逐章列流水账。

只返回严格 JSON：
{
  "volumes": [{
    "title": "能体现本卷独有事件或命题的卷名",
    "chapter_count": 12,
    "goal": "卷末必须完成的可验证阶段变化",
    "conflict": "本卷主导矛盾、双方策略与不可兼得的选择",
    "synopsis": "本卷详细剧情梗概：起因、连续升级、关键选择、代价、卷末结果及下一卷触发点",
    "turning_points": ["2-5个改变行动方向的具体转折"],
    "character_arcs": ["点名人物在本卷的状态、关系或信念变化"],
    "subplots": ["本卷推进或回收的具体支线"],
    "must_keep": ["必须兑现的事实、伏笔或承诺"],
    "must_avoid": ["不得提前发生或不得违背的事项"],
    "ending_state": "卷末人物、资源、局势和悬念状态",
    "bridge_to_next": "本卷哪一个具体结果直接触发下一卷"
  }]
}

要求：
1. 恰好生成输入指定的分卷数，chapter_count 总和必须等于计划总章数。
2. 每卷 synopsis 建议 220-450 字，并覆盖该卷完整因果链；相邻卷不得只是替换名词后重复同一目标。
3. 每卷必须承担不同的主线阶段、人物变化和主题检验；后卷由前卷的具体结果触发。
4. 卷名、目标、转折必须来自具体事件，禁止“初次选择、阻力反制、风险升级、核心选择、关系转向”等抽象模板标题。
5. 使用已有专名，不使用“主角”“反派”“某势力”等占位词。
6. 不提前消耗终局秘密，不依靠巧合推进；已写正文和作者硬规则高于规划。
7. 先保证 JSON 完整闭合，只返回 JSON，不要 Markdown。"""


DIRECTOR_MASTER_BIBLE_PROMPT = """你是无人值守小说生产线的总导演。当前只建立精炼但可执行的全书故事圣经，不写详细大纲、不分卷、不写正文。
返回严格 JSON：
{
  "theme": "全书反复检验的价值命题",
  "reader_promise": "读者持续获得的核心体验",
  "central_conflict": "贯穿全书并不断升级的冲突",
  "story_engine": "危机如何反复制造选择、代价和新局面",
  "ending_state": "结局状态、人物选择与不可逆代价",
  "main_plot": "主线起点、连续升级、核心转折、高潮与终局，180-320字",
  "theme_progression": "主题在前中后期如何被事件检验并改变答案，120-220字",
  "pacing_plan": "高潮密度、缓冲段、信息揭示与收束原则，100-180字",
  "stakes_ladder": ["4-8条逐级升级的具体风险与代价"],
  "major_character_arcs": ["点名人物：起点→关键选择→变化→终局，3-8条"],
  "subplots": ["支线：作用、交汇点与回收方式，3-6条"],
  "historical_nodes": ["历史或架空节点：边界与可改变范围，3-8条"]
}
必须服从作者硬规则并使用已有专名。字段简洁、因果明确；先保证 JSON 完整闭合，不输出 full_outline、volumes、Markdown或解释。"""


DIRECTOR_VOLUME_CONTRACT_PROMPT = """你是长篇小说的结构总监。根据故事圣经，只为输入指定的一卷建立分卷契约；这里只决定本卷为什么存在，不展开详细梗概。
返回严格 JSON：
{
    "contract": {
    "number": 1,
    "title": "来自本卷独有事件或意象的卷名",
    "goal": "卷末可验证的阶段变化",
    "conflict": "双方策略与不可兼得的选择",
    "ending_state": "卷末人物、资源和局势状态",
    "bridge_to_next": "哪个具体结果触发下一卷",
    "theme_test": "本卷用什么事件检验主题",
    "primary_arena": "本卷独占的主要故事场域/制度层级/行动领域",
    "time_span": "本卷覆盖的明确时间跨度",
    "irreversible_change": "本卷结束后再也回不到原状的具体变化",
    "character_choice": "点名核心人物在本卷必须作出的不可兼得选择",
    "new_story_question": "本卷回答旧问题后新产生、交给下一卷的问题"
  }
}
number 必须与输入指定卷号一致。每卷必须更换主要故事场域、关键选择和不可逆变化，不能把同一制度试验、同一政治争论或同一终局状态换一种措辞重演。承接已经完成的前卷契约，不得重复其目标；最后一卷负责终局，其他卷不得提前兑现终局。只返回这一卷的完整 JSON。"""


DIRECTOR_VOLUME_CORE_PROMPT = """你是长篇小说的分卷导演。当前只写指定一卷的剧情梗概核心，不列转折数组，不写逐章路线或正文。
返回严格 JSON：
{
  "volume_core": {
    "title": "必须与指定卷名一致",
    "goal": "卷末可验证变化，不超过100字",
    "conflict": "本卷主导矛盾与双方策略，不超过140字",
    "synopsis": "180-320字详细梗概：起因、升级、选择、代价、结果",
    "ending_state": "卷末人物、资源和局势，不超过120字",
    "bridge_to_next": "触发下一卷的结果；终卷写收束，不超过80字"
  }
}
剧情必须属于指定范围，使用具体人名和事件。严格遵守长度，先闭合 JSON；不输出数组、其他卷、Markdown或解释。"""


DIRECTOR_VOLUME_DETAILS_PROMPT = """你是小说连续性与分卷编辑。剧情梗概核心已经确定；当前只补充这卷的执行清单，不复述梗概，不写正文。
返回严格 JSON：
{
  "volume_details": {
    "turning_points": ["3-5个改变行动方向的具体转折，每条不超过70字"],
    "character_arcs": ["点名人物在本卷的状态、关系或信念变化，2-5条"],
    "subplots": ["本卷推进或回收的支线，1-4条"],
    "must_keep": ["必须兑现的事实、伏笔或承诺，2-5条"],
    "must_avoid": ["不得提前发生或违背的事项，2-5条"]
  }
}
只补充指定一卷；不得复述 synopsis，不得输出其他字段。先保证 JSON 完整闭合，不输出 Markdown 或解释。"""


VOLUME_PLAN_PROMPT = """你是长篇小说的分卷导演。请只拆解输入指定的本卷小批次章节，不写正文。
每一章必须拥有不同的场景任务、阻力、选择、转折和结果，同时承接上一章已经造成的状态。不能把本卷目标原句复制到每一章，不能用“风险升级、关系转向、核心选择”等抽象节拍词代替具体剧情。

返回严格 JSON：
{
  "volume_id": "输入中的分卷ID",
  "chapters": [{
    "number": 1,
    "title": "具体章名",
    "goal": "章末可验证的局面变化",
    "conflict": "本章阻力与人物必须做出的选择",
    "turning_point": "改变理解、关系或行动方向的具体转折",
    "ending_hook": "由本章因果自然产生的下一步推动力",
    "must_keep": ["本章必须兑现或保持的事实，1-4条"],
    "must_avoid": ["本章不得提前发生或不得违背的事项，1-4条"]
  }]
}

要求：
1. 必须恰好生成本批次指定范围内的每一章，不多不少，number 使用全书章号。
2. 章节之间形成“结果→新问题→选择→代价”的因果链，不要写重复功能的章节。
3. 每3-5章至少改变一次目标、关系、资源或认知，但不能每章都靠突发反转。
4. 已写正文和权威记忆高于旧规划；若冲突，做最小适配，不篡改已发生事实。
5. 不使用“主角”“某人”“关键人物”等占位词；输入不足时明确写“待作者确认”。
6. title必须来自本章独有事件；goal写章末可观察结果，不能复述卷目标。
7. goal、conflict建议50-100字；turning_point、ending_hook建议35-80字；
   must_keep与must_avoid各1-4条。内容必须具体，但优先保证JSON完整。
8. 相邻章节的title和goal不得相同或高度近似。
9. 只返回 JSON，不要 Markdown。"""


DIRECTOR_CHAPTER_ROUTE_PROMPT = """你是无人值守小说生产线的逐章导演。当前只规划指定的一章，不写正文，也不输出其他章节。
返回严格 JSON：
{
  "route": {
    "number": 1,
    "title": "来自本章独有事件或意象的具体章名",
    "goal": "只写一个章末可验证的主要状态变化，45-90字；不得复述过程",
    "conflict": "具体人物采取什么行动阻止、迫使谁作何选择，45-90字",
    "turning_point": "改变认知、关系、资源或行动方向的具体转折，30-70字",
    "ending_hook": "由本章结果自然产生的下一步压力或问题，30-70字",
    "must_keep": ["必须兑现或保持的事实，1-3条"],
    "must_avoid": ["不得提前发生或违背的事项，1-3条"]
  }
}
必须服从输入给定的本章岗位与唯一状态维度，直接承接上一章结果。不得把上一章已经成功的办法换一个场景再次成功；目标只能改变本章指定的主要状态，其他变化作为代价或钩子。非卷末章不得提前完成卷目标或卷末状态。先保证 JSON 闭合，只输出这一章，不要 Markdown或解释。"""


def empty_planning() -> dict[str, Any]:
    return {
        "version": 1,
        "master": {
            "theme": "",
            "reader_promise": "",
            "central_conflict": "",
            "story_engine": "",
            "ending_state": "",
            "full_outline": "",
            "main_plot": "",
            "theme_progression": "",
            "pacing_plan": "",
            "stakes_ladder": [],
            "major_character_arcs": [],
            "subplots": [],
            "historical_nodes": [],
        },
        "volumes": [],
    }


def ensure_planning_defaults(value: Any) -> dict[str, Any]:
    planning = value if isinstance(value, dict) else {}
    planning.setdefault("version", 1)
    if not isinstance(planning.get("master"), dict):
        planning["master"] = {}
    master = planning["master"]
    for key in (
        "theme",
        "reader_promise",
        "central_conflict",
        "story_engine",
        "ending_state",
        "full_outline",
        "main_plot",
        "theme_progression",
        "pacing_plan",
    ):
        master.setdefault(key, "")
    for key in (
        "stakes_ladder",
        "major_character_arcs",
        "subplots",
        "historical_nodes",
    ):
        if not isinstance(master.get(key), list):
            master[key] = []
    if not isinstance(planning.get("volumes"), list):
        planning["volumes"] = []
    clean_volumes = []
    for index, raw in enumerate(planning["volumes"]):
        if not isinstance(raw, dict):
            continue
        volume = raw
        volume.setdefault("id", str(uuid.uuid4()))
        volume.setdefault("number", index + 1)
        volume.setdefault("title", f"第{index + 1}卷")
        volume.setdefault("chapter_start", 1)
        volume.setdefault("chapter_end", volume["chapter_start"])
        for key in (
            "goal",
            "conflict",
            "synopsis",
            "ending_state",
            "bridge_to_next",
            "theme_test",
            "primary_arena",
            "time_span",
            "irreversible_change",
            "character_choice",
            "new_story_question",
        ):
            volume.setdefault(key, "")
        for key in (
            "turning_points",
            "character_arcs",
            "subplots",
            "must_keep",
            "must_avoid",
            "chapters",
        ):
            if not isinstance(volume.get(key), list):
                volume[key] = []
        volume["chapters"] = [
            normalize_route(item, int(volume["chapter_start"]) + route_index)
            for route_index, item in enumerate(volume["chapters"])
            if isinstance(item, dict)
        ]
        clean_volumes.append(volume)
    planning["volumes"] = clean_volumes
    return planning


def _strings(value: Any, limit: int = 8) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(item).strip() for item in value if str(item).strip()][:limit]


def normalize_route(raw: dict[str, Any], number: int) -> dict[str, Any]:
    return {
        "id": str(raw.get("id") or uuid.uuid4()),
        "number": number,
        "title": str(raw.get("title") or f"第{number}章").strip(),
        "goal": str(raw.get("goal", "")).strip(),
        "conflict": str(raw.get("conflict", "")).strip(),
        "turning_point": str(raw.get("turning_point", "")).strip(),
        "ending_hook": str(raw.get("ending_hook", "")).strip(),
        "must_keep": _strings(raw.get("must_keep"), 6),
        "must_avoid": _strings(raw.get("must_avoid"), 6),
        "quality_warnings": _strings(raw.get("quality_warnings"), 6),
        "status": str(raw.get("status", "planned")),
    }


def _allocate_counts(raw_volumes: list[dict[str, Any]], total: int) -> list[int]:
    count = min(max(1, len(raw_volumes)), total)
    raw_volumes = raw_volumes[:count]
    weights = []
    for item in raw_volumes:
        suggested = item.get("chapter_count")
        if not isinstance(suggested, (int, float)) or suggested <= 0:
            start, end = item.get("chapter_start"), item.get("chapter_end")
            suggested = (
                int(end) - int(start) + 1
                if isinstance(start, (int, float))
                and isinstance(end, (int, float))
                and end >= start
                else 1
            )
        weights.append(max(1.0, float(suggested)))
    remaining = total - count
    ideal_extra = [remaining * weight / sum(weights) for weight in weights]
    extras = [math.floor(value) for value in ideal_extra]
    for index in sorted(
        range(count), key=lambda i: ideal_extra[i] - extras[i], reverse=True
    )[: remaining - sum(extras)]:
        extras[index] += 1
    return [1 + value for value in extras]


def normalize_master_plan(raw: dict[str, Any], target_chapters: int) -> dict[str, Any]:
    target = min(2000, max(1, int(target_chapters or 1)))
    raw_volumes = [
        item for item in raw.get("volumes", []) if isinstance(item, dict)
    ][:20]
    if not raw_volumes:
        raise ValueError("模型没有返回可用的分卷规划")
    counts = _allocate_counts(raw_volumes, target)
    cursor = 1
    volumes = []
    for index, (item, chapter_count) in enumerate(zip(raw_volumes, counts)):
        end = cursor + chapter_count - 1
        volumes.append(
            {
                "id": str(uuid.uuid4()),
                "number": index + 1,
                "title": str(item.get("title") or f"第{index + 1}卷").strip(),
                "chapter_start": cursor,
                "chapter_end": end,
                "goal": str(item.get("goal", "")).strip(),
                "conflict": str(item.get("conflict", "")).strip(),
                "synopsis": str(item.get("synopsis", "")).strip(),
                "turning_points": _strings(item.get("turning_points")),
                "character_arcs": _strings(item.get("character_arcs")),
                "subplots": _strings(item.get("subplots")),
                "must_keep": _strings(item.get("must_keep")),
                "must_avoid": _strings(item.get("must_avoid")),
                "ending_state": str(item.get("ending_state", "")).strip(),
                "bridge_to_next": str(item.get("bridge_to_next", "")).strip(),
                "chapters": [],
            }
        )
        cursor = end + 1
    master = {
        key: str(raw.get(key, "")).strip()
        for key in (
            "theme",
            "reader_promise",
            "central_conflict",
            "story_engine",
            "ending_state",
            "full_outline",
            "main_plot",
            "theme_progression",
            "pacing_plan",
        )
    }
    for key in (
        "stakes_ladder",
        "major_character_arcs",
        "subplots",
        "historical_nodes",
    ):
        master[key] = _strings(raw.get(key), 12)
    return {"version": 1, "master": master, "volumes": volumes}


def fallback_master_plan(
    project: dict[str, Any], target_chapters: int, reason: str
) -> dict[str, Any]:
    """Build an editable plan from the author's outline when a local model times out."""
    outline = str(project.get("outline", "")).strip()
    narrative = project.get("narrative", {})
    heading_pattern = re.compile(
        r"(?m)^\s*#{0,6}\s*第[一二三四五六七八九十百\d]+卷"
        r"[：:\s]+([^\n（(]+)"
        r"(?:[（(][^\n]*?第?\s*(\d+)\s*[—–-]\s*(\d+)\s*章)?"
    )
    matches = list(heading_pattern.finditer(outline))
    raw_volumes: list[dict[str, Any]] = []
    for index, match in enumerate(matches[:20]):
        body_end = matches[index + 1].start() if index + 1 < len(matches) else len(outline)
        body = outline[match.end() : body_end]
        body = re.sub(r"[#>*`|]+", " ", body)
        lines = [
            re.sub(r"^\s*[-\d.、]+\s*", "", line).strip()
            for line in body.splitlines()
            if line.strip()
        ]
        summary = "；".join(lines[:3])
        start = int(match.group(2)) if match.group(2) else 0
        end = int(match.group(3)) if match.group(3) else 0
        raw_volumes.append(
            {
                "title": match.group(1).strip(),
                "chapter_count": end - start + 1 if end >= start > 0 else 1,
                "goal": summary[:90] or "依据作者总纲完成本卷阶段变化",
                "conflict": (summary[90:170] or project.get("premise", ""))[:80],
                "synopsis": "；".join(lines)[:1200] or summary,
                "ending_state": (lines[-1] if lines else "进入下一阶段")[:80],
                "bridge_to_next": (
                    "由本卷结果触发下一阶段，具体因果待AI详细规划"
                ),
            }
        )
    if not raw_volumes:
        count = 1 if target_chapters <= 6 else min(12, max(2, math.ceil(target_chapters / 12)))
        chunks = [
            item.strip()
            for item in re.split(r"\n\s*\n|(?<=[。！？])", outline)
            if item.strip()
        ]
        for index in range(count):
            chunk = chunks[index] if index < len(chunks) else ""
            raw_volumes.append(
                {
                    "title": f"第{index + 1}阶段",
                    "chapter_count": max(1, round(target_chapters / count)),
                    "goal": chunk[:90] or "待作者审核阶段目标",
                    "conflict": str(project.get("premise", ""))[:80],
                    "synopsis": chunk[:1200],
                    "ending_state": "推动故事进入下一阶段",
                    "bridge_to_next": "本阶段结果触发下一阶段",
                }
            )
    raw = {
        "theme": str(narrative.get("central_question", ""))[:80],
        "reader_promise": str(project.get("author_intent", ""))[:80],
        "central_conflict": str(project.get("premise", ""))[:80],
        "story_engine": "依据作者既有总纲逐卷推进选择、行动与代价",
        "ending_state": str(narrative.get("ending_direction", ""))[:80],
        "full_outline": outline,
        "main_plot": str(project.get("premise", ""))[:600],
        "theme_progression": str(narrative.get("central_question", ""))[:500],
        "pacing_plan": "依据分卷章数逐级升级冲突，具体节奏待AI完成",
        "stakes_ladder": [],
        "major_character_arcs": [],
        "subplots": [],
        "historical_nodes": [],
        "volumes": raw_volumes,
    }
    result = normalize_master_plan(raw, target_chapters)
    result["fallback"] = True
    result["warnings"] = [
        f"本地模型未在时限内完成，已从现有总纲生成可编辑备用规划。原因：{reason}"
    ]
    return result


def normalize_volume_routes(
    raw: dict[str, Any], volume: dict[str, Any]
) -> tuple[list[dict[str, Any]], list[str]]:
    start = int(volume.get("chapter_start", 1))
    end = int(volume.get("chapter_end", start))
    expected = end - start + 1
    source = [item for item in raw.get("chapters", []) if isinstance(item, dict)]
    routes = [
        normalize_route(item, start + index) for index, item in enumerate(source[:expected])
    ]
    warnings = []
    if len(routes) != expected:
        warnings.append(
            f"模型应返回 {expected} 章，实际返回 {len(routes)} 章；"
            "未自动填充空洞，请重新拆解本卷。"
        )
    return routes, warnings


def _outline_segment_for_volume(outline: str, volume: dict[str, Any]) -> str:
    title = str(volume.get("title", "")).strip()
    if not title or title not in outline:
        return str(volume.get("goal", ""))
    start = outline.find(title)
    next_heading = re.search(
        r"(?m)^\s*#{0,6}\s*第[一二三四五六七八九十百\d]+卷[：:\s]+",
        outline[start + len(title) :],
    )
    end = (
        start + len(title) + next_heading.start()
        if next_heading
        else len(outline)
    )
    return outline[start:end].strip()


def fallback_volume_routes(
    project: dict[str, Any], volume: dict[str, Any], reason: str
) -> dict[str, Any]:
    start = int(volume.get("chapter_start", 1))
    end = int(volume.get("chapter_end", start))
    count = end - start + 1
    segment = _outline_segment_for_volume(str(project.get("outline", "")), volume)
    sentences = [
        re.sub(r"^[#>*\-\d.、\s]+", "", item).strip()
        for item in re.split(r"\n+|(?<=[。！？；])", segment)
        if re.sub(r"^[#>*\-\d.、\s]+", "", item).strip()
    ]
    protagonist = next(
        (
            str(item.get("name", "")).strip()
            for item in project.get("characters", [])
            if str(item.get("name", "")).strip()
        ),
        str(project.get("title", "本书")),
    )
    phase_labels = (
        "承接余波",
        "线索浮现",
        "初次选择",
        "阻力反制",
        "理念交锋",
        "代价显现",
        "关系转向",
        "方案受挫",
        "风险升级",
        "决断前夜",
        "核心选择",
        "卷末余波",
    )
    existing = project.get("chapters", [])
    routes = []
    for offset in range(count):
        number = start + offset
        phase_index = min(
            len(phase_labels) - 1,
            round(offset * (len(phase_labels) - 1) / max(1, count - 1)),
        )
        source_index = min(
            len(sentences) - 1,
            round(offset * (len(sentences) - 1) / max(1, count - 1)),
        ) if sentences else 0
        source = sentences[source_index][:100] if sentences else ""
        old = existing[number - 1] if number - 1 < len(existing) else {}
        old_title = str(old.get("title", "")).strip()
        generic = bool(
            re.fullmatch(r"第[一二三四五六七八九十百千万\d]+章", old_title)
        )
        title = (
            old_title
            if old_title and not generic
            else f"{phase_labels[phase_index]}"
        )
        goal = (
            f"{protagonist}在“{source[:55]}”这一局面中完成{phase_labels[phase_index]}，"
            "并造成可验证的新变化。"
            if source
            else f"{protagonist}完成{phase_labels[phase_index]}，推动本卷目标向前一步。"
        )
        routes.append(
            normalize_route(
                {
                    "title": title,
                    "goal": goal,
                    "conflict": str(volume.get("conflict", ""))
                    or str(
                        project.get("planning", {})
                        .get("master", {})
                        .get("central_conflict", "")
                    ),
                    "turning_point": (
                        f"本章行动暴露新的代价，使下一步从“{phase_labels[phase_index]}”"
                        "转向更具体的选择。"
                    ),
                    "ending_hook": (
                        "本章结果直接触发下一章的阻力或选择。"
                        if number < end
                        else str(volume.get("ending_state", "进入下一卷的新局面"))
                    ),
                    "must_keep": list(volume.get("must_keep", []))[:3],
                    "must_avoid": list(volume.get("must_avoid", []))[:3]
                    + ["不得篡改已经写入正文和故事记忆的事实"],
                    "status": "planned",
                },
                number,
            )
        )
    return {
        "volume_id": volume.get("id", ""),
        "chapters": routes,
        "warnings": [
            f"AI未完成分卷拆解，已依据本卷目标、现有总纲和章节节奏建立"
            f"{count}条可编辑备用路线。原因：{reason}"
        ],
        "complete": True,
        "fallback": True,
    }


def fallback_chapter_plan(
    project: dict[str, Any], chapter_index: int, chapter: dict[str, Any], reason: str
) -> dict[str, Any]:
    _, planned_route = find_planning_for_chapter(project, chapter_index)
    route = chapter.get("route") if isinstance(chapter.get("route"), dict) else planned_route
    route = route or {}
    recent_facts = [
        str(item.get("text", ""))
        for item in project.get("memory", {}).get("facts", [])
        if isinstance(item, dict) and item.get("active", True) and item.get("text")
    ][-3:]
    must_keep = list(route.get("must_keep", [])) + recent_facts
    must_avoid = list(route.get("must_avoid", []))
    if not must_avoid:
        must_avoid = ["不得篡改已写正文", "不得提前完成后续章节目标", "不得新增无依据往事"]
    return {
        "goal": str(route.get("goal") or chapter.get("scene_goal", "")),
        "conflict": str(route.get("conflict", "")),
        "must_keep": list(dict.fromkeys(must_keep))[:8],
        "must_avoid": list(dict.fromkeys(must_avoid))[:8],
        "turning_point": str(route.get("turning_point", "")),
        "ending_hook": str(route.get("ending_hook", "")),
        "chapter_type": "escalation",
        "pov_character": "",
        "time_location": "承接上一章",
        "opening_beat": "直接承接上一章结尾的动作、感官或对话压力",
        "scene_beats": [
            value
            for value in (
                str(route.get("conflict", "")),
                str(route.get("turning_point", "")),
                str(route.get("goal", "")),
            )
            if value
        ],
        "emotional_turn": "",
        "thread_actions": [],
        "exit_state": str(route.get("goal", "")),
        "ending_type": "consequence",
        "fallback": True,
        "warnings": [
            f"AI未完成单章细化，已将上层章节路线转换为执行计划。原因：{reason}"
        ],
    }


def find_volume(
    project: dict[str, Any], volume_id: str
) -> tuple[int, dict[str, Any]]:
    planning = ensure_planning_defaults(project.get("planning"))
    project["planning"] = planning
    for index, volume in enumerate(planning["volumes"]):
        if volume.get("id") == volume_id:
            return index, volume
    raise KeyError(volume_id)


def find_planning_for_chapter(
    project: dict[str, Any], chapter_index: int
) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    number = chapter_index + 1
    planning = ensure_planning_defaults(project.get("planning"))
    for volume in planning["volumes"]:
        if int(volume.get("chapter_start", 1)) <= number <= int(
            volume.get("chapter_end", 0)
        ):
            route = next(
                (
                    item
                    for item in volume.get("chapters", [])
                    if int(item.get("number", 0)) == number
                ),
                None,
            )
            return volume, route
    return None, None


def render_planning_context(project: dict[str, Any], chapter_index: int) -> str:
    planning = ensure_planning_defaults(project.get("planning"))
    master = planning["master"]
    volume, route = find_planning_for_chapter(project, chapter_index)
    parts = [
        "【全书总导演板】",
        f"主题命题：{master.get('theme', '')}",
        f"读者承诺：{master.get('reader_promise', '')}",
        f"核心冲突：{master.get('central_conflict', '')}",
        f"故事驱动器：{master.get('story_engine', '')}",
        f"终局状态：{master.get('ending_state', '')}",
        f"主线推进：{master.get('main_plot', '')}",
        f"主题递进：{master.get('theme_progression', '')}",
        f"节奏规划：{master.get('pacing_plan', '')}",
        f"代价阶梯：{'；'.join(master.get('stakes_ladder', []))}",
        f"全书人物弧：{'；'.join(master.get('major_character_arcs', []))}",
        f"全书支线：{'；'.join(master.get('subplots', []))}",
        f"历史节点：{'；'.join(master.get('historical_nodes', []))}",
        f"AI详细全书大纲：{master.get('full_outline', '')}",
    ]
    if volume:
        parts.extend(
            [
                "【当前分卷战略】",
                f"卷名：{volume.get('title', '')}（第{volume.get('chapter_start')}"
                f"-{volume.get('chapter_end')}章）",
                f"阶段目标：{volume.get('goal', '')}",
                f"主导冲突：{volume.get('conflict', '')}",
                f"本卷梗概：{volume.get('synopsis', '')}",
                f"关键转折：{'；'.join(volume.get('turning_points', []))}",
                f"人物弧：{'；'.join(volume.get('character_arcs', []))}",
                f"本卷支线：{'；'.join(volume.get('subplots', []))}",
                f"卷末状态：{volume.get('ending_state', '')}",
                f"承接下一卷：{volume.get('bridge_to_next', '')}",
                f"本卷必须保持：{'；'.join(volume.get('must_keep', []))}",
                f"本卷禁止跑偏：{'；'.join(volume.get('must_avoid', []))}",
            ]
        )
    if route:
        parts.extend(
            [
                "【当前章路线卡（战略意图）】",
                f"章名：{route.get('title', '')}",
                f"阶段任务：{route.get('goal', '')}",
                f"预定冲突：{route.get('conflict', '')}",
                f"预定转折：{route.get('turning_point', '')}",
                f"预定钩子：{route.get('ending_hook', '')}",
                f"必须保持：{'；'.join(route.get('must_keep', []))}",
                f"必须避免：{'；'.join(route.get('must_avoid', []))}",
            ]
        )
    parts.append(
        "治理规则：路线卡是尚未写作的战略意图；已接受正文、故事记忆、人物状态和"
        "时间线是权威事实。若二者冲突，保留已发生事实，并对本章路线做最小适配。"
    )
    return "\n".join(parts)


def apply_volume_routes(project: dict[str, Any], volume_id: str) -> dict[str, Any]:
    result = deepcopy(project)
    _, volume = find_volume(result, volume_id)
    routes = volume.get("chapters", [])
    expected = int(volume["chapter_end"]) - int(volume["chapter_start"]) + 1
    if len(routes) != expected:
        raise ValueError("本卷路线尚不完整，请重新执行 AI 拆解后再批量建章")
    chapters = result.setdefault("chapters", [])
    while len(chapters) < int(volume["chapter_end"]):
        number = len(chapters) + 1
        chapters.append(
            {
                "id": str(uuid.uuid4()),
                "title": f"第{number}章",
                "summary": "",
                "content": "",
                "scene_goal": "",
                "plan": {},
            }
        )
    for route in routes:
        number = int(route["number"])
        chapter = chapters[number - 1]
        chapter["volume_id"] = volume["id"]
        chapter["route_id"] = route["id"]
        chapter["route"] = deepcopy(route)
        generic_title = re.fullmatch(r"第[一二三四五六七八九十百千万\d]+章", str(chapter.get("title", "")))
        if not chapter.get("content") and (not chapter.get("title") or generic_title):
            chapter["title"] = route["title"]
        if not chapter.get("scene_goal"):
            chapter["scene_goal"] = route["goal"]
    return result
