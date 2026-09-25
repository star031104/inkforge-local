"""Chapter-route role, language, chronology, and checkpoint validation."""
from __future__ import annotations

import math
import re
from difflib import SequenceMatcher
from typing import Any

from ..narrative_policy import profile as narrative_profile
from ..planning import normalize_route
from ..core.text import bounded_excerpt
from .schema_validation import require_schema
from .planning_validation import (
    _chinese_bigrams,
    _director_chapter_core_forbidden_terms,
    _director_chapter_forbidden_terms,
    _director_chapter_seed,
    _validate_director_stage_boundary,
    historical_asset_conflicts,
    validate_director_chapter_forbidden_terms, validate_director_chapter_seed,
    validate_director_volume_domain, validate_route_batch,
)


def _validation_error_detail(exc: Exception) -> str:
    """Render deterministic route validation failures without service imports."""
    return str(exc).strip() or exc.__class__.__name__

def _director_chapter_job(position: int, count: int) -> str:
    jobs = (
        "问题与行动空间：建立本卷独有问题，让人物获得有限行动空间，不解决核心问题",
        "首次试验：执行一个可检验的小行动，只得到局部结果并暴露一个新变量",
        "反常证据：发现与上一章成功解释相矛盾的事实，改变调查或判断方向",
        "外部阻力：由人物、制度、环境或关系施加具体阻力，迫使行动条件发生变化",
        "修正与代价：人物修改办法但必须损失资源、信誉、时间或一段关系",
        "适应性反制：人物、系统或环境根据此前行动产生回应，使原办法不能照搬",
        "关系重议：让合作者因价值、利益、情感或风险分配改变合作条件",
        "外部后果：让前期决策影响具体人物或场域，并反过来约束下一步",
        "资源或关系损失：即使取得局部成果，也失去关键支持、时间、信任或筹码",
        "终局选择收束：让证据、关系与代价迫使人物面对两个无法兼得的具体行动，本章暂不决定",
        "卷末蓄势：人物支付最后一项局部代价并备齐卷末选择所需条件；必须保留下一卷触发所需的人物、联系和行动通道",
        "卷末兑现：完成本卷最终选择和代价，只由其具体结果触发下一卷",
    )
    bucket = round(position * (len(jobs) - 1) / max(1, count - 1))
    return jobs[min(len(jobs) - 1, max(0, bucket))]


def _director_state_dimension(position: int, count: int) -> str:
    dimensions = (
        "行动空间", "局部资源", "证据与认知", "外部约束关系",
        "已经支付的具体成本与他人信任", "旧办法是否还能继续执行", "合作条件与责任分配",
        "百姓、军队或地方已经承受的可点名后果", "合法性或关键筹码",
        "两道无法同时执行的具体命令及各自代价", "卷末选择所需的最后条件或已经支付的代价",
        "卷末总体状态",
    )
    bucket = round(position * (len(dimensions) - 1) / max(1, count - 1))
    return dimensions[min(len(dimensions) - 1, max(0, bucket))]


def _director_job_prohibition(position: int, count: int) -> str:
    bucket = round(position * 11 / max(1, count - 1))
    prohibitions = (
        "不得在本章解决危机或取得完整信任",
        "只能获得局部试验结果，不得宣布方案全面正确",
        "不得以证明原方案正确、迫使他人承认模型有效或再次成功收尾；反常事实必须真正改变判断方向",
        "不得靠一次展示轻易化解外部阻力，必须改变条件或关系",
        "修正必须支付可见代价，不得无损优化",
        "反制必须让旧办法失效，不得让阻力只负责衬托主角",
        "联盟变化不得用一句误会带过，必须改变合作条件",
        "外部后果不得只做背景描写，必须反过来约束决策",
        "局部成果不得抵消合法性、资源或筹码损失",
        "只能摆出两个具体行动及其不同代价，不得提前替人物决定执行哪一个",
        "必须新增一项当场发生的条件或代价；不得切断下一卷所需联系，不得提前完成最终选择或卷末桥梁",
        "只兑现本卷承诺，不得顺带完成后续卷任务",
    )
    return prohibitions[min(11, max(0, bucket))]


def validate_director_route_role(
    route: dict[str, Any], position: int, count: int
) -> None:
    bucket = round(position * 11 / max(1, count - 1))
    combined = " ".join(
        str(route.get(field, ""))
        for field in ("goal", "turning_point", "ending_hook")
    )
    ending_hook = str(route.get("ending_hook", ""))
    meta_phrases = (
        "可选方案集合", "形成两个不可兼得", "形成不可兼得", "形成两难",
        "主动放弃退路", "外部社会后果", "方案因对手反制失效",
        "关系出现裂痕", "联盟出现裂痕", "信誉受损", "阶段目标",
        "状态维度", "本章岗位",
    )
    leaked = [phrase for phrase in meta_phrases if phrase in combined]
    meta_patterns = (
        r"两个不可兼得", r"不可兼得.{0,6}(选择|方案)",
        r"两难(?:选择|选项)", r"压缩至.{0,8}(?:选择|选项)",
        r"放弃.{0,8}退路", r"(?:信任|关系|联盟).{0,5}裂痕",
        r"方案.{0,8}(?:失效|被反制)",
    )
    leaked.extend(
        match.group(0)
        for pattern in meta_patterns
        if (match := re.search(pattern, combined))
    )
    if leaked:
        raise ValueError(
            "章节路线照抄了导演节拍术语，必须改写成具体事件与可观察结果："
            + "、".join(leaked)
        )
    if bucket == 2 and re.search(
        r"证明.{0,12}(正确|有效)|验证.{0,12}(正确|有效)|迫使.{0,16}(承认|认可)|再次成功|正确性",
        combined,
    ):
        raise ValueError(
            "章节岗位高度重复：反常证据章不得再次以证明方案正确或获得认可作为转折"
        )
    if bucket <= 1 and re.search(
        r"(?:获得|取得).{0,16}(?:直接处置|全面处置|接管|完整授权|正式授权|实权)",
        combined,
    ):
        raise ValueError(
            "章节岗位提前升级：开篇授权或首次试验只能取得有限资格，不得直接获得完整处置权"
        )
    if bucket <= 4 and re.search(
        r"(?:伪造|假造).{0,20}(?:换取|取得|获得|接管|授权|官印)|"
        r"(?:换取|取得|获得|接管).{0,20}(?:伪造|假造)",
        combined,
    ):
        raise ValueError(
            "制度可信度问题：不得用伪造文书或印信直接换取正式官权"
        )
    if re.search(
        r"(?:即将|将在).{0,18}(?:发现|揭开|意识到|发生|得知)", ending_hook
    ):
        raise ValueError(
            "章末变化是预告而非已发生事件：必须落在本章可观察的新事实或压力上"
        )
    if bucket == 10 and (
        re.search(
            r"(?:切断|断绝).{0,18}(?:所有|全部|任何).{0,12}(?:联系|联络|文书|退路|通路)",
            combined,
        )
        or re.search(r"成为.{0,8}(?:独裁者|唯一主宰|绝对统治者)", combined)
    ):
        raise ValueError(
            "卷末部署过度：倒数第二章必须备齐卷末选择的条件，不得切断全部联系或提前成为绝对统治者"
        )


def validate_director_penultimate_bridge(
    route: dict[str, Any], volume: dict[str, Any], position: int, count: int
) -> None:
    """Keep the penultimate route compatible with the promised next-volume bridge."""
    if position != count - 2:
        return
    bridge = " ".join(
        str(volume.get(field, ""))
        for field in ("ending_state", "bridge_to_next")
    )
    route_text = " ".join(
        str(route.get(field, ""))
        for field in ("goal", "turning_point", "ending_hook")
    )
    central_bridge = any(
        marker in bridge
        for marker in ("征召", "入朝", "咸阳", "朝堂", "秦王", "嬴政", "中央")
    )
    total_severance = re.search(
        r"(?:切断|断绝|焚毁).{0,24}(?:咸阳|中央|朝廷).{0,18}(?:所有|全部|任何|联系|联络|通路|文书)|"
        r"(?:所有|全部|任何).{0,12}(?:咸阳|中央|朝廷).{0,12}(?:联系|联络|通路|文书)",
        route_text,
    )
    if central_bridge and total_severance:
        raise ValueError(
            "卷末桥梁冲突：下一卷需要中央征召或入朝，本章不得切断与咸阳、朝廷的全部联系"
        )


def validate_director_route_structure(
    result: dict[str, Any], chapter_number: int
) -> dict[str, Any]:
    require_schema("route")(result)
    route = result["route"]
    if not isinstance(route, dict):
        raise ValueError("章节路线 route 不是对象")
    for field in (
        "title", "goal", "conflict", "turning_point", "ending_hook",
        "must_keep", "must_avoid",
    ):
        if field not in route:
            raise ValueError(f"章节路线缺少 {field}")
    for field in ("title", "goal", "conflict", "turning_point", "ending_hook"):
        if not str(route.get(field, "")).strip():
            raise ValueError(f"章节路线 {field} 为空")
    if not isinstance(route.get("must_keep"), list) or not isinstance(
        route.get("must_avoid"), list
    ):
        raise ValueError("章节路线 must_keep 和 must_avoid 必须是数组")
    if len(route["must_keep"]) > 3:
        raise ValueError("章节路线 must_keep 最多保留与本章直接相关的 3 条事实")
    if len(route["must_avoid"]) > 3:
        raise ValueError("章节路线 must_avoid 最多列出本章最相关的 3 条禁令")
    route["number"] = chapter_number
    normalized = normalize_route(route, chapter_number)
    # A model may add an unsolicited quality_warnings field containing its own
    # self-assessment.  Only deterministic validators may create production
    # debt; otherwise praise such as "no modern terms used" blocks release.
    normalized["quality_warnings"] = []
    return normalized


def validate_director_route_language(
    project: dict[str, Any], route: dict[str, Any]
) -> None:
    genre = str(project.get("genre", ""))
    if not any(marker in genre for marker in ("历史", "古代", "战国", "架空")):
        return
    text = " ".join(
        str(route.get(field, ""))
        for field in ("title", "goal", "conflict", "turning_point", "ending_hook")
    )
    # must_avoid is control metadata and commonly repeats the exact forbidden
    # phrase supplied by the production specification.  Judging it as story
    # language makes a compliant route impossible to save.  must_keep remains
    # checked because it is positive story context that can flow into prose.
    text += " " + " ".join(str(item) for item in route.get("must_keep", []))
    forbidden = [
        marker for marker in (
            "毫秒", "秒级", "黑盒化", "算法", "APP", "互联网", "数据库",
            "物理", "数字化", "全省", "数据", "误差率", "行政授权",
            "外交官", "标准化", "系统性", "信任度", "机构",
            "模型", "系统", "自动标记", "高危", "管控", "背锅", "背书",
            "机器逻辑", "帝国机器", "复核官署", "现代", "量化", "逻辑",
            "防御体系", "政治清洗", "集体造假机制", "恐怖机器", "合法授权",
            "签字", "签名", "技术性", "人事任命", "行政壁垒", "产出",
            "特派巡查使", "实数", "虚数",
            "考核体系", "行政体系", "政治体系", "机制", "通讯",
            "独裁者", "绝对服从", "技术工具", "效率机器", "军机处",
            "行政特区", "行政脐带", "行政合法性", "信息黑洞",
        )
        if marker.lower() in text.lower()
    ]
    if forbidden:
        raise ValueError(
            f"时代语言质量问题：章节路线含有不应直接出现的现代技术词 {', '.join(forbidden)}"
        )
    authority_text = " ".join(
        str(project.get(field, ""))
        for field in ("genre", "premise", "production_spec", "book_rules")
    )
    timeline_conflicts = historical_asset_conflicts(route, authority_text)
    if timeline_conflicts:
        raise ValueError(
            "时代语言质量问题：章节路线违反秦统一前时间边界，含有后世称谓或物件 "
            + "、".join(timeline_conflicts)
        )
    if re.search(
        r"(?:县令|县长|县丞|郡守|仓吏|令史)赵高|赵高.{0,6}(?:县令|县长|县丞|郡守|仓吏|令史)",
        text,
    ):
        raise ValueError(
            "时代身份质量问题：不得把赵高随意改写为地方县令、郡守或仓吏"
        )
    configured_names = {
        str(item.get("name", "")).strip()
        for item in project.get("characters", [])
        if isinstance(item, dict) and str(item.get("name", "")).strip()
    }
    known_historical_names = {
        "赵高", "蒙恬", "蒙毅", "王翦", "王贲", "扶苏", "胡亥",
        "吕不韦", "嫪毐", "昌平君", "尉缭", "姚贾", "顿弱",
    }
    unauthorized = sorted(
        name
        for name in known_historical_names
        if name in text and name not in configured_names and name not in authority_text
    )
    if unauthorized:
        raise ValueError(
            "时代身份质量问题：章节擅自启用尚未进入人物表或作者设定的历史人物 "
            + "、".join(unauthorized)
        )
    if re.search(r"自制.{0,6}私印.{0,18}(?:行文|公文|调令|粮册)", text):
        raise ValueError(
            "制度可信度问题：私人自制印信不能直接取得官文、调令或官仓簿籍效力"
        )
    if re.search(
        r"\d+(?:\.\d+)?\s*%|(?:信任|粮食|资源|效率|风险|关系)维度\s*[-+]\s*\d+|A\s*/\s*B区",
        text,
        re.IGNORECASE,
    ):
        raise ValueError(
            "时代语言质量问题：古代题材路线不得使用百分比、数值维度或 A/B 分区表达"
        )


def _director_historicalize_context(
    project: dict[str, Any], text: str
) -> str:
    genre = str(project.get("genre", ""))
    if not any(marker in genre for marker in ("历史", "古代", "战国", "架空")):
        return text
    replacements = (
        ("行政调动令", "调任文书"), ("行政管理工具", "治吏手段"),
        ("行政合法性", "官署名分"), ("行政特区", "自外于王法之地"),
        ("行政脐带", "官文往来"), ("行政体系", "官署法度"),
        ("政治体系", "朝廷法度"), ("考核体系", "考课法"),
        ("信息黑洞", "无从核验之地"), ("军机处", "议兵官署"),
        ("政治清洗", "借法黜逐异己"), ("集体造假机制", "上下相蒙之法"),
        ("技术工具", "办事手段"), ("效率机器", "唯求速效的官府"),
        ("独裁者", "专断之主"), ("绝对服从", "唯命是从"),
        ("合法授权", "合乎秦律的授命"), ("技术性", "只论办法的"),
        ("人事任命", "官吏任免"), ("行政壁垒", "官署阻隔"),
        ("量化考核", "据数考课"), ("现代", "后世"), ("逻辑", "理路"),
        ("系统性", "成片"), ("标准化", "统一尺度"), ("数字化", "改用统一簿籍"),
        ("数据包", "原始簿册"), ("数据库", "簿册库"), ("计算模型", "推算之法"),
        ("物理阻抗", "实物阻碍"), ("行政授权", "官署授命"), ("审查机构", "御史属官"),
        ("操作员", "经手吏员"), ("编码", "记号"), ("流程", "次序"),
        ("签字画押", "署押"), ("签字", "署名"), ("签名", "署名"),
        ("审计", "复核"), ("数据", "簿籍数目"), ("模型", "推算之法"),
        ("网络", "文书通路"), ("信息", "文书消息"), ("权限", "职权"),
        ("机制", "成法"), ("技术", "手段"), ("维度", "一项状态"),
        ("指标", "数目"), ("透明性", "可查验程度"),
    )
    result = text
    for source, target in replacements:
        result = result.replace(source, target)
    return result


def validate_director_assigned_turn(
    route: dict[str, Any], assigned_turn: str
) -> None:
    """Ensure a scheduled volume turning point is actually dramatized.

    Small local models sometimes acknowledge the assigned turn in reasoning but
    return a structurally valid route from an earlier volume.  Requiring two
    concrete Chinese bigram anchors keeps paraphrase freedom while preventing a
    completely unrelated event from occupying the scheduled chapter.
    """
    if not assigned_turn.strip():
        return
    common = {
        "秦策", "发现", "制度", "问题", "引发", "开始", "必须", "选择",
        "地方", "形成", "进行", "通过", "成为", "关键", "首次", "最终",
        "要求", "导致", "不得", "需要", "出现", "实现", "完成", "同时",
        "统一", "标准", "规则", "隐秘", "维持", "无法", "完全", "覆盖",
        "推行",
    }
    anchors = _chinese_bigrams(assigned_turn) - common
    combined = " ".join(
        str(route.get(field, ""))
        for field in ("title", "goal", "conflict", "turning_point", "ending_hook")
    )
    overlap = anchors & _chinese_bigrams(combined)
    required = min(8, max(2, (len(anchors) + 4) // 5))
    common_chars = set(
        "的了在与和及并而后中从向将为以于其这那一上下"
        "完全进行通过形成发现意识认知要求导致成为"
    )
    assigned_chars = {
        char for char in assigned_turn
        if "\u3400" <= char <= "\u9fff" and char not in common_chars
    }
    route_chars = {
        char for char in combined
        if "\u3400" <= char <= "\u9fff" and char not in common_chars
    }
    char_required = min(12, max(6, math.ceil(len(assigned_chars) * 0.25)))
    paraphrase_match = len(assigned_chars & route_chars) >= char_required
    if len(overlap) < required and not paraphrase_match:
        raise ValueError(
            "章节未承载指定卷级转折，必须围绕这件事重新设计："
            + bounded_excerpt(assigned_turn, 180)
        )


def validate_director_future_turns(
    route: dict[str, Any], future_turns: list[tuple[int, str]]
) -> None:
    """Prevent an early chapter from consuming a later scheduled turn."""
    common = {
        "秦策", "发现", "制度", "问题", "引发", "开始", "必须", "选择",
        "地方", "形成", "进行", "通过", "成为", "关键", "首次", "最终",
        "要求", "导致", "不得", "需要", "出现", "实现", "完成", "同时",
        "统一", "标准", "规则", "隐秘", "维持", "无法", "完全", "覆盖",
        "推行",
    }
    combined = " ".join(
        str(route.get(field, ""))
        for field in ("title", "goal", "conflict", "turning_point", "ending_hook")
    )
    route_bigrams = _chinese_bigrams(combined)
    for chapter_number, future_turn in future_turns:
        anchors = _chinese_bigrams(future_turn) - common
        # A few anchors can describe the shared institution or arena without
        # consuming the later action. Require both a meaningful absolute count
        # and roughly one third of the reserved turn before blocking the route.
        required = min(10, max(4, math.ceil(len(anchors) * 0.34)))
        if len(anchors & route_bigrams) >= required:
            raise ValueError(
                f"章节提前占用第 {chapter_number} 章指定转折，当前章不得实现："
                + bounded_excerpt(future_turn, 160)
            )


def _route_similarity_score(
    route: dict[str, Any], previous_routes: list[dict[str, Any]]
) -> float:
    if not previous_routes:
        return 0.0
    fields = ("title", "goal", "conflict", "turning_point", "ending_hook")
    maximum = 0.0
    for previous in previous_routes:
        per_field = []
        for field in fields:
            left = re.sub(r"[\W_]+", "", str(route.get(field, "")))
            right = re.sub(r"[\W_]+", "", str(previous.get(field, "")))
            if left and right:
                per_field.append(SequenceMatcher(None, left, right).ratio())
        if per_field:
            maximum = max(maximum, max(per_field))
    return maximum


def _director_routes_before_volume(
    project: dict[str, Any], volume: dict[str, Any]
) -> list[dict[str, Any]]:
    current_start = int(volume.get("chapter_start", 1) or 1)
    routes: list[dict[str, Any]] = []
    for item in project.get("planning", {}).get("volumes", []):
        if not isinstance(item, dict):
            continue
        if int(item.get("chapter_end", 0) or 0) >= current_start:
            continue
        routes.extend(
            route
            for route in item.get("chapters", [])
            if isinstance(route, dict)
        )
    return routes


def _director_assigned_turn(
    volume: dict[str, Any], position: int, count: int
) -> str:
    turning_points = [str(item) for item in volume.get("turning_points", [])]
    if not turning_points:
        return ""
    turn_positions = [
        round((turn_index + 1) * (count - 1) / len(turning_points))
        for turn_index in range(len(turning_points))
    ]
    if position not in turn_positions:
        return ""
    return turning_points[turn_positions.index(position)]


def _director_outcome_reserved_by_turn(outcome: str, assigned_turn: str) -> bool:
    """Allow an explicit chapter turn to override a synonymous end-state guard."""
    if not outcome.strip() or not assigned_turn.strip():
        return False
    outcome_terms = _chinese_bigrams(outcome)
    turn_terms = _chinese_bigrams(assigned_turn)
    if not outcome_terms or not turn_terms:
        return False
    overlap = len(outcome_terms & turn_terms)
    required = min(6, max(2, math.ceil(min(len(outcome_terms), len(turn_terms)) * 0.18)))
    shared_phrase = any(
        outcome[offset : offset + 4] in assigned_turn
        and not any(
            marker in outcome[offset : offset + 4]
            for marker in ("沈衡", "嬴政", "李斯", "发现", "意识", "决定", "要求")
        )
        for offset in range(max(0, len(outcome) - 3))
    )
    return overlap >= required or shared_phrase


def _route_safe_volume_synopsis(
    synopsis: str,
    future_turns: list[tuple[int, str]],
) -> str:
    """Hide later scheduled turns from a single-chapter planning prompt.

    A route model only needs the causal lane available now. Exposing every
    later reveal makes repair prompts counterproductive because the model keeps
    selecting the most concrete event it can see.
    """
    text = str(synopsis or "").strip()
    if not text or not future_turns:
        return text
    reserved = [
        _chinese_bigrams(str(turn))
        for _, turn in future_turns
        if str(turn).strip()
    ]
    clauses = [
        clause.strip()
        for clause in re.split(r"(?<=[。！？；])", text)
        if clause.strip()
    ]
    safe: list[str] = []
    for clause in clauses:
        terms = _chinese_bigrams(clause)
        leaks_future = any(
            len(terms & future_terms) >= max(
                3, min(8, math.ceil(len(future_terms) * 0.18))
            )
            for future_terms in reserved
            if future_terms
        )
        if not leaks_future:
            safe.append(clause)
    return "".join(safe).strip() or (
        "本卷危机将按逐章因果链升级；当前只处理本章岗位规定的局部变化，"
        "后续保留转折尚未发生。"
    )


def _audit_route_checkpoint_prefix(
    project: dict[str, Any],
    volume: dict[str, Any],
    routes: list[dict[str, Any]],
) -> tuple[int, str]:
    start, end = int(volume["chapter_start"]), int(volume["chapter_end"])
    count = end - start + 1
    accepted: list[dict[str, Any]] = []
    earlier_routes = _director_routes_before_volume(project, volume)
    for index, route in enumerate(routes):
        number = start + index
        try:
            normalized = validate_director_route_structure(
                {"route": dict(route)}, number
            )
            forbidden = [] if number == end else [
                str(volume.get("goal", "")), str(volume.get("ending_state", "")),
                str(volume.get("irreversible_change", "")),
                str(volume.get("bridge_to_next", "")),
            ]
            validate_route_batch(
                {"chapters": [normalized]},
                [number],
                earlier_routes + accepted,
                forbidden,
            )
            seed = _director_chapter_seed(
                project,
                int(volume.get("number", 0) or 0),
                number,
            )
            if not seed:
                if narrative_profile(project) == "legacy":
                    validate_director_route_role(normalized, index, count)
                validate_director_penultimate_bridge(
                    normalized, volume, index, count
                )
            validate_director_route_language(project, normalized)
            if not seed:
                validate_director_assigned_turn(
                    normalized,
                    _director_assigned_turn(volume, index, count),
                )
                validate_director_future_turns(
                    normalized,
                    [
                        (start + later, turn)
                        for later in range(index + 1, count)
                        if (turn := _director_assigned_turn(volume, later, count))
                    ],
                )
            if not seed:
                validate_director_volume_domain(
                    project,
                    int(volume.get("number", 0) or 0),
                    normalized,
                )
            validate_director_chapter_seed(
                normalized,
                seed,
            )
            validate_director_chapter_forbidden_terms(
                normalized,
                _director_chapter_forbidden_terms(
                    project, int(volume.get("number", 0) or 0), number
                ),
            )
            validate_director_chapter_forbidden_terms(
                normalized,
                _director_chapter_core_forbidden_terms(
                    project, int(volume.get("number", 0) or 0), number
                ),
                include_hook=False,
            )
            _validate_director_stage_boundary(
                project,
                int(volume.get("number", 0) or 0),
                " ".join(
                    str(normalized.get(field, ""))
                    for field in (
                        ("title", "goal", "conflict", "turning_point")
                        if number == end
                        else ("title", "goal", "conflict", "turning_point", "ending_hook")
                    )
                ),
            )
            accepted.append(normalized)
        except Exception as exc:
            return index, _validation_error_detail(exc)
    return len(routes), ""
