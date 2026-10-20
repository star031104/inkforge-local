"""Genre templates for the new-book wizard (新书向导).

A template is a *skeleton*, never story content: it proposes narrative
defaults, hard-rule lines, editable banned-phrase suggestions, world-book
entries with fill-in prompts and character cards with prompts.  The wizard
applies a template additively — it only fills empty fields and appends items
whose title/role is not present yet, so an existing manuscript is never
overwritten.

Placeholders are kept out of the writing context on purpose: skeleton world
entries are created disabled until the author fills and enables them, and
unnamed character cards are skipped by the prompt builder.
"""
from __future__ import annotations

import uuid
from copy import deepcopy
from typing import Any

PLACEHOLDER_PREFIX = "（待填写）"
VALID_POV = {"auto", "first", "third_limited", "omniscient"}
VALID_TENSE = {"auto", "past", "present"}
VALID_STRUCTURE = {"adaptive", "mystery", "relationship", "adventure", "literary", "custom", "legacy"}
ARCHETYPES = ("主角", "对手", "导师", "盟友")

_BASE_CHARACTER_PROMPTS: dict[str, dict[str, str]] = {
    "主角": {
        "importance": "main",
        "description": "主角是谁：身份、处境、最想要的东西。",
        "hard_limits": "主角绝不会做的事（道德底线、能力上限），写作中不得越过。",
        "knowledge": "开篇时主角知道什么、不知道什么（信息边界）。",
        "secrets": "主角对他人隐瞒的事，以及被揭开的代价。",
        "voice": "说话方式：句子长短、口头禅、对不同人的语气差别。",
    },
    "对手": {
        "importance": "main",
        "description": "对手想要什么，为什么与主角的目标正面冲突。",
        "hard_limits": "对手的行事原则与底线——聪明的对手不降智、不为主角让路。",
        "knowledge": "对手掌握而主角不知道的信息。",
        "secrets": "对手的真实动机或隐藏筹码。",
        "voice": "对手的说话方式与压迫感来源。",
    },
    "导师": {
        "importance": "supporting",
        "description": "导师能教给主角什么，又有什么局限或私心。",
        "hard_limits": "导师不会替主角做的事（避免成为万能解题人）。",
        "knowledge": "导师知道的关键旧事。",
        "secrets": "导师隐瞒的过去。",
        "voice": "导师的说话方式（点到为止 / 絮叨 / 严厉）。",
    },
    "盟友": {
        "importance": "supporting",
        "description": "盟友为什么站在主角一边，自己又想要什么。",
        "hard_limits": "盟友的底线：在什么情况下会离开或背叛。",
        "knowledge": "盟友独有的技能或信息渠道。",
        "secrets": "盟友未说出口的顾虑。",
        "voice": "盟友的说话方式，与主角形成反差。",
    },
}


def _characters(roles: dict[str, str], overrides: dict[str, dict[str, str]] | None = None) -> list[dict[str, Any]]:
    """Build the four archetype skeletons with genre-specific role labels."""
    cards = []
    for archetype in ARCHETYPES:
        card = {"archetype": archetype, "role": roles.get(archetype, archetype)}
        card.update(_BASE_CHARACTER_PROMPTS[archetype])
        card.update((overrides or {}).get(archetype, {}))
        cards.append(card)
    return cards


def _world(title: str, category: str, keys: list[str], prompts: list[str], constant: bool = False) -> dict[str, Any]:
    return {
        "title": title,
        "category": category,
        "keys": keys,
        "constant": constant,
        "content": PLACEHOLDER_PREFIX + "\n" + "\n".join(f"· {line}：" for line in prompts),
    }


def _banned(phrase: str, reason: str, default: bool = True) -> dict[str, Any]:
    return {"phrase": phrase, "reason": reason, "default": default}


GENRE_TEMPLATES: dict[str, dict[str, Any]] = {
    "history": {
        "name": "历史穿越 / 历史权谋",
        "genre": "历史",
        "description": "朝代考据、官制礼法与权力博弈；适合穿越、架空历史与宫廷权谋。",
        "narrative": {"pov": "third_limited", "tense": "past", "tone": "沉稳克制，细节考据，权谋暗涌", "structure_profile": "adaptive"},
        "settings": {"story_mode": "long", "target_chapters": 120, "target_words": 2500, "volumes": 4},
        "book_rules": [
            "朝代、年号、官制与地名以世界书「编年」「制度」条目为准，不得自行杜撰冲突设定。",
            "古人不降智：同僚与对手有自己的信息、利益和判断，不会为主角让路。",
            "主角的现代知识受时代物质条件限制，不出现超时代工业或一夜改制。",
            "称谓、礼仪、计量与货币遵循「称谓与礼仪」「器物与生活」条目。",
            "历史大事的走向可以被影响，但必须写清代价与连锁反应。",
        ],
        "banned_phrases": [
            _banned("搞定", "现代口语，古代人物对白中不应出现"),
            _banned("靠谱", "现代口语"),
            _banned("内卷", "当代网络词"),
            _banned("格局", "当代流行用法"),
            _banned("OK", "现代外来语"),
            _banned("皇上", "秦以前没有“皇帝”称号；先秦背景请勾选", False),
            _banned("万岁", "先秦至汉初并非专指皇帝；视朝代决定", False),
            _banned("银两", "白银大规模流通始于明代；唐宋以前背景建议勾选", False),
            _banned("太监", "以“太监”泛称宦官始于明代；此前多称宦者、中官、内侍", False),
            _banned("分钟", "计时宜用时辰、刻、盏茶；穿越者内心独白可保留", False),
        ],
        "world_entries": [
            _world("称谓与礼仪", "礼制", ["称谓", "礼仪", "行礼", "自称", "陛下", "大人"], [
                "对君主的称呼与君主自称", "官员之间、上下级之间的称谓", "家族内称谓（父母、兄弟、妻妾、子女）", "常见礼节（揖、拜、跪）及适用场合",
            ], True),
            _world("器物与生活", "生活", ["货币", "铜钱", "计量", "饮食", "服饰", "出行"], [
                "货币与物价（一顿饭、一匹布约值多少）", "计时与计量（时辰、里、斤、石）", "饮食、服饰、照明、出行方式", "本时代没有、必须避免出现的器物",
            ], True),
            _world("编年", "历史", ["年号", "纪年", "那一年", "大事"], [
                "故事开始的年号与对应公元年", "故事期间已发生 / 将发生的重大事件（按时间排列）", "主角穿越或介入的时间点",
            ], True),
            _world("制度", "制度", ["官制", "官职", "科举", "赋税", "兵制", "律法"], [
                "中央与地方官制，主角能接触到的官职层级", "选官方式（察举 / 科举 / 门荫）", "赋税与兵役", "律法要点与刑罚",
            ]),
        ],
        "characters": _characters(
            {"主角": "主角", "对手": "政敌 / 对手", "导师": "引路人 / 座师", "盟友": "盟友 / 同僚"},
            {"主角": {
                "knowledge": "主角带来的现代知识边界，以及对本朝了解到什么程度。",
                "secrets": "主角的穿越者身份或其他秘密，暴露的代价。",
                "voice": "说话方式：文白比例、对上对下的语气差别；内心独白可略带现代口吻。",
            }},
        ),
    },
    "mystery": {
        "name": "悬疑推理",
        "genre": "悬疑",
        "description": "公平线索、嫌疑人矩阵与层层揭示；适合本格、社会派与刑侦。",
        "narrative": {"pov": "third_limited", "tense": "past", "tone": "冷静克制，信息精确，悬念层层递进", "structure_profile": "mystery"},
        "settings": {"story_mode": "long", "target_chapters": 60, "target_words": 2500, "volumes": 3},
        "book_rules": [
            "公平线索：真相的关键证据必须在揭示前以读者可辨识的方式出现过。",
            "侦探不依赖巧合或读者没见过的信息破案。",
            "每名嫌疑人都要有动机、机会与（可被检验的）不在场说明。",
            "揭示必须回指已出现的证据，不临时追加新设定。",
            "不在章末使用全知视角剧透（如“他不知道的是……”）。",
        ],
        "banned_phrases": [
            _banned("殊不知", "全知剧透，破坏悬念"),
            _banned("他不知道的是", "全知剧透，破坏悬念"),
            _banned("细思极恐", "网络用语，替读者下结论"),
            _banned("命运的齿轮", "套路化表达"),
            _banned("真相只有一个", "借用台词，易出戏"),
        ],
        "world_entries": [
            _world("案件档案", "案件", ["案件", "死者", "案发", "现场"], [
                "受害者身份与死因", "案发时间、地点与发现经过", "警方 / 侦探掌握的初始信息",
            ], True),
            _world("线索清单", "线索", ["线索", "证据", "物证", "痕迹"], [
                "每条线索：出现章节、表面含义、真实含义", "误导线索及其合理来源",
            ]),
            _world("嫌疑人与不在场证明", "人物", ["嫌疑人", "不在场", "动机"], [
                "每名嫌疑人的动机、机会与不在场说明", "哪一条说明是假的，破绽在哪里",
            ]),
            _world("真相时间线（作者专用）", "真相", ["真相", "凶手", "作案"], [
                "真实作案过程（按分钟 / 小时）", "凶手为掩盖真相做了什么", "最终揭示的顺序",
            ]),
        ],
        "characters": _characters(
            {"主角": "侦探 / 主角", "对手": "真凶 / 对手", "导师": "前辈 / 顾问", "盟友": "助手 / 搭档"},
            {"对手": {"secrets": "真凶的作案手法与动机，何时、因何暴露。"}},
        ),
    },
    "romance": {
        "name": "言情",
        "genre": "言情",
        "description": "关系的距离、信任与选择；适合都市、古言与校园情感故事。",
        "narrative": {"pov": "third_limited", "tense": "past", "tone": "细腻温暖，情绪真实，张力克制", "structure_profile": "relationship"},
        "settings": {"story_mode": "long", "target_chapters": 80, "target_words": 2200, "volumes": 3},
        "book_rules": [
            "感情推进由具体事件与选择驱动，不靠旁白宣告“他爱上了她”。",
            "两位主角都有独立于恋爱之外的目标与成长。",
            "误会不能靠“一句话就能解开却刻意不说”来拖延。",
            "配角不工具化，各自有动机与生活。",
            "亲密场景尺度：（待填写）",
        ],
        "banned_phrases": [
            _banned("邪魅一笑", "套路化表达"),
            _banned("嘴角勾起一抹", "高频模板句"),
            _banned("心漏跳了一拍", "高频模板句"),
            _banned("小鹿乱撞", "套路化表达"),
            _banned("你成功引起了我的注意", "网络梗，易出戏"),
        ],
        "world_entries": [
            _world("主要关系图", "关系", ["关系", "前任", "家人", "朋友"], [
                "两位主角的初始关系与距离", "前任、家人、朋友对这段关系的态度", "关系中的核心阻力",
            ], True),
            _world("时代与社会背景", "背景", ["时代", "城市", "社会"], [
                "故事发生的年代、城市与阶层环境", "影响恋爱选择的社会规则或家族规矩",
            ], True),
            _world("生活场景", "场景", ["公司", "学校", "住处", "店"], [
                "两人反复相遇的场所", "每个场所的氛围与可发生的事件",
            ]),
            _world("情感节点表", "节奏", ["初遇", "告白", "误会", "重逢"], [
                "初遇、靠近、危机、确认关系等节点大致落在第几章", "每个节点由什么事件触发",
            ]),
        ],
        "characters": _characters(
            {"主角": "主角", "对手": "情敌 / 外部阻力", "导师": "闺蜜 / 长辈", "盟友": "恋爱对象"},
            {"盟友": {
                "importance": "main",
                "description": "恋爱对象是谁，自身的目标与伤口，为什么会被主角吸引。",
                "hard_limits": "恋爱对象的底线：不会为爱放弃什么。",
            }},
        ),
    },
    "xianxia": {
        "name": "玄幻仙侠",
        "genre": "玄幻仙侠",
        "description": "境界体系、宗门势力与因果代价；适合修仙、玄幻与东方奇幻。",
        "narrative": {"pov": "third_limited", "tense": "past", "tone": "恢弘大气，热血有度，因果分明", "structure_profile": "adventure"},
        "settings": {"story_mode": "long", "target_chapters": 200, "target_words": 3000, "volumes": 5},
        "book_rules": [
            "境界体系固定不变，越级战斗必须有明确的条件或代价。",
            "力量来源与上限以世界书「境界与功法」为准。",
            "主角不靠天降机缘连续解决问题，每次突破都要有前置铺垫。",
            "宗门与势力有自身利益，不为主角服务。",
            "已死角色不随意复活；复活须有设定支撑和沉重代价。",
        ],
        "banned_phrases": [
            _banned("恐怖如斯", "网文高频套话"),
            _banned("倒吸一口凉气", "网文高频套话"),
            _banned("此子断不可留", "网文高频套话"),
            _banned("一股磅礴的气息", "空泛描写"),
            _banned("系统", "非系统流作品请勾选", False),
        ],
        "world_entries": [
            _world("境界与功法", "力量体系", ["境界", "修为", "突破", "功法", "灵力"], [
                "境界名称与顺序，每个境界的能力上限", "突破的条件与风险", "主要功法与相克关系",
            ], True),
            _world("世界规则", "世界设定", ["天道", "因果", "寿元", "天劫"], [
                "天道 / 因果如何约束修士", "寿元与天劫规则", "不可违背的禁忌",
            ], True),
            _world("势力与宗门", "势力", ["宗门", "门派", "世家", "皇朝"], [
                "主要势力及其立场、利益", "势力之间的恩怨与平衡",
            ]),
            _world("地理与秘境", "地理", ["大陆", "州", "秘境", "山"], [
                "大陆与区域划分", "秘境的开启条件与危险",
            ]),
            _world("天材地宝与货币", "资源", ["灵石", "丹药", "法宝", "灵草"], [
                "货币（灵石等）与物价", "稀有资源的获取途径与争夺",
            ]),
        ],
        "characters": _characters(
            {"主角": "主角", "对手": "宿敌", "导师": "师父 / 引路人", "盟友": "同门 / 道侣"},
        ),
    },
    "scifi": {
        "name": "科幻",
        "genre": "科幻",
        "description": "核心技术设定与社会后果；适合硬科幻、太空歌剧与近未来。",
        "narrative": {"pov": "third_limited", "tense": "past", "tone": "冷静理性，细节可信，思想先行", "structure_profile": "adaptive"},
        "settings": {"story_mode": "long", "target_chapters": 60, "target_words": 2500, "volumes": 3},
        "book_rules": [
            "核心技术有明确的原理边界与代价，以世界书「核心技术」为准。",
            "不出现与既有设定矛盾的技术跳跃。",
            "技术改变必须带来可见的社会后果。",
            "科学解释服务情节，不写成说明书。",
            "光速、通讯延迟等物理约束全书保持一致。",
        ],
        "banned_phrases": [
            _banned("黑科技", "空泛的网络用语"),
            _banned("科技感十足", "空泛描写"),
            _banned("仿佛来自未来", "空泛描写"),
            _banned("不可思议的科技", "空泛描写"),
        ],
        "world_entries": [
            _world("核心技术", "技术", ["技术", "装置", "引擎", "协议"], [
                "核心技术的原理与能力边界", "使用代价与失效条件", "谁掌握这项技术",
            ], True),
            _world("社会结构", "社会", ["政府", "公司", "阶层", "联盟"], [
                "权力结构与主要组织", "技术带来的阶层变化",
            ], True),
            _world("时间线", "历史", ["纪年", "历史", "战争", "年"], [
                "从现在到故事开始之间的关键事件", "故事开始的年份",
            ]),
            _world("地点与环境", "地理", ["星球", "城市", "飞船", "空间站"], [
                "主要地点的环境与生存条件", "地点之间的交通方式与耗时",
            ]),
            _world("术语表", "术语", ["术语", "缩写", "代号"], [
                "本书专有名词与含义", "人物日常使用的俚语",
            ]),
        ],
        "characters": _characters(
            {"主角": "主角", "对手": "对手 / 体制力量", "导师": "导师 / 前辈科学家", "盟友": "搭档 / 船员"},
        ),
    },
    "realism": {
        "name": "现实文学",
        "genre": "现实",
        "description": "时代处境中的普通人；适合乡土、家族、职场与成长叙事。",
        "narrative": {"pov": "third_limited", "tense": "past", "tone": "朴素真切，克制留白，细节见人", "structure_profile": "literary"},
        "settings": {"story_mode": "long", "target_chapters": 40, "target_words": 3000, "volumes": 2},
        "book_rules": [
            "不靠巧合推动关键情节。",
            "人物行为符合其阶层、职业与时代处境。",
            "不做道德说教，让细节与选择呈现立场。",
            "物价、通讯、政策等时代细节以「时代与地方」条目为准。",
            "结局不强行圆满。",
        ],
        "banned_phrases": [
            _banned("岁月静好", "空泛抒情"),
            _banned("命运的齿轮", "套路化表达"),
            _banned("五味杂陈", "替读者概括情绪"),
            _banned("泪水模糊了双眼", "套路化表达"),
        ],
        "world_entries": [
            _world("时代与地方", "背景", ["年代", "县城", "城市", "方言", "村"], [
                "故事的年代与具体地方", "当地方言与风俗", "那几年影响人物命运的政策或事件",
            ], True),
            _world("职业与行当", "职业", ["工作", "单位", "工厂", "职业"], [
                "主要人物的职业日常", "行业规矩与收入",
            ]),
            _world("家族与人情", "关系", ["家里", "亲戚", "邻居", "老家"], [
                "家族成员与彼此的亏欠", "人情往来的规矩",
            ]),
            _world("物价与生活", "生活", ["工资", "房租", "物价", "钱"], [
                "工资、房租与常见物价", "人物的经济压力从何而来",
            ]),
        ],
        "characters": _characters(
            {"主角": "主角", "对手": "对立者 / 生活压力", "导师": "长辈 / 引路人", "盟友": "亲友 / 同伴"},
        ),
    },
    "general": {
        "name": "通用",
        "genre": "",
        "description": "不预设题材，只提供人物、规则与世界的基础骨架。",
        "narrative": {"pov": "auto", "tense": "auto", "tone": "", "structure_profile": "adaptive"},
        "settings": {"story_mode": "long", "target_chapters": 30, "target_words": 2000, "volumes": 2},
        "book_rules": [
            "已接纳的正文是最高事实，后续计划不得与之冲突。",
            "人物的每次关键行动都要有动机与代价。",
            "不在正文中解释写作意图或总结主题。",
        ],
        "banned_phrases": [
            _banned("眼中闪过一丝", "高频模板句"),
            _banned("嘴角勾起一抹", "高频模板句"),
            _banned("不由得", "高频虚词，宜少用", False),
        ],
        "world_entries": [
            _world("世界基本设定", "世界设定", ["世界", "背景", "时代"], [
                "故事发生的时间、地点与基本面貌", "与现实世界最大的不同",
            ], True),
            _world("核心规则", "世界设定", ["规则", "能力", "禁忌"], [
                "本书独有的规则或能力，以及它的限制",
            ], True),
            _world("主要地点", "地理", ["地点", "城", "家"], [
                "反复出现的场景与氛围",
            ]),
            _world("组织与势力", "势力", ["组织", "势力", "公司", "家族"], [
                "主要组织的目标与彼此关系",
            ]),
        ],
        "characters": _characters({}),
    },
}


def list_templates() -> list[dict[str, Any]]:
    """Every template with its full skeleton, ordered for the wizard select."""
    return [{"id": key, **deepcopy(value)} for key, value in GENRE_TEMPLATES.items()]


def get_template(template_id: str) -> dict[str, Any] | None:
    template = GENRE_TEMPLATES.get(str(template_id or ""))
    return {"id": template_id, **deepcopy(template)} if template else None


def project_has_work(project: dict[str, Any]) -> bool:
    """Same notion of "non-empty" as the incubator: any authored text or asset."""
    if not isinstance(project, dict):
        return False
    if any(str(project.get(key) or "").strip() for key in ("premise", "outline", "author_intent", "current_focus", "book_rules")):
        return True
    if any(isinstance(project.get(key), list) and project[key] for key in ("characters", "world_entries", "references")):
        return True
    planning = project.get("planning") if isinstance(project.get("planning"), dict) else {}
    if planning.get("volumes") or any(planning.get("master", {}).values() if isinstance(planning.get("master"), dict) else []):
        return True
    for chapter in project.get("chapters") or []:
        if not isinstance(chapter, dict):
            continue
        if any(str(chapter.get(key) or "").strip() for key in ("content", "summary", "scene_goal", "author_note")):
            return True
        plan = chapter.get("plan") if isinstance(chapter.get("plan"), dict) else {}
        if any(value for value in plan.values()):
            return True
    return False


def _text(value: Any, limit: int = 4000) -> str:
    return str(value or "").strip()[:limit]


def _int(value: Any, default: int, low: int, high: int) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError):
        return default
    return max(low, min(high, number))


def _selected(items: list[dict[str, Any]], key: str, chosen: Any, default_flag: str | None = None) -> list[dict[str, Any]]:
    """Filter template items by the wizard's checkbox selection (None = defaults)."""
    if chosen is None:
        return [item for item in items if not default_flag or item.get(default_flag, True)]
    wanted = {str(value) for value in chosen} if isinstance(chosen, list) else set()
    return [item for item in items if str(item.get(key)) in wanted]


def _world_entry(item: dict[str, Any], order: int) -> dict[str, Any]:
    constant = bool(item.get("constant"))
    return {
        "id": str(uuid.uuid4()), "title": item["title"], "category": item.get("category") or "世界设定",
        "canon": "hard" if constant else "soft", "keys": list(item.get("keys") or []), "secondary_keys": [],
        "selective_logic": "and_any", "content": item.get("content") or "", "position": "after", "order": order,
        "constant": constant,
        # Skeletons stay out of the writing context until the author fills them in.
        "enabled": False, "wizard_skeleton": True,
        "match": "any", "case_sensitive": False, "character_names": [], "chapter_start": 0, "chapter_end": 0,
        "inclusion_group": "", "non_recursable": False, "prevent_recursion": False, "delay_until_recursion": False,
    }


def _character(item: dict[str, Any], answers: dict[str, str]) -> dict[str, Any]:
    archetype = item.get("archetype") or ""
    name = answers["protagonist_name"] if archetype == "主角" else ""
    card: dict[str, Any] = {
        "id": str(uuid.uuid4()), "name": name, "role": item.get("role") or archetype, "aliases": [],
        "importance": item.get("importance") or "supporting", "active": True, "knowledge_ledger": [],
        "dialogue_examples": [], "wizard_skeleton": True, "wizard_archetype": archetype,
    }
    for key in ("personality", "appearance", "appearance_state", "values", "fears", "contradictions", "mannerisms",
                "relationships", "arc", "goal", "state", "location", "items", "emotion"):
        card[key] = ""
    prompt_fields = ("description", "hard_limits", "knowledge", "secrets", "voice")
    prompts = {key: str(item.get(key) or "") for key in prompt_fields}
    card["wizard_prompts"] = prompts
    if name:
        # A named card enters the writing context, so it carries only what the
        # author actually wrote; the prompts stay in wizard_prompts.
        for key in prompt_fields:
            card[key] = ""
    else:
        for key in prompt_fields:
            card[key] = f"{PLACEHOLDER_PREFIX}{prompts[key]}" if prompts[key] else ""
    if archetype == "主角":
        if answers["protagonist"]:
            card["description"] = answers["protagonist"]
        if answers["want"]:
            card["goal"] = answers["want"]
        if answers["obstacle"]:
            card["fears"] = f"最大阻碍：{answers['obstacle']}"
    elif archetype == "对手" and answers["obstacle"]:
        card["description"] = f"{card['description']}\n与主角的冲突：{answers['obstacle']}".strip()
    return card


def normalize_answers(raw: dict[str, Any] | None, template: dict[str, Any]) -> dict[str, Any]:
    raw = raw if isinstance(raw, dict) else {}
    defaults = template.get("settings", {})
    mode = raw.get("story_mode") if raw.get("story_mode") in {"long", "short"} else defaults.get("story_mode", "long")
    return {
        "title": _text(raw.get("title"), 120),
        "genre": _text(raw.get("genre"), 60) or template.get("genre", ""),
        "story_mode": mode,
        "target_chapters": _int(raw.get("target_chapters"), defaults.get("target_chapters", 30), 1, 2000),
        "target_words": _int(raw.get("target_words"), defaults.get("target_words", 1200), 300, 10000),
        "volumes": _int(raw.get("volumes"), defaults.get("volumes", 1), 1, 24),
        "premise": _text(raw.get("premise"), 2000),
        "protagonist": _text(raw.get("protagonist"), 1000),
        "protagonist_name": _text(raw.get("protagonist_name"), 40),
        "want": _text(raw.get("want"), 1000),
        "obstacle": _text(raw.get("obstacle"), 1000),
        "ending": _text(raw.get("ending"), 2000),
        "tone": _text(raw.get("tone"), 200),
    }


def apply_template(
    project: dict[str, Any], template_id: str, answers: dict[str, Any] | None = None,
    selection: dict[str, Any] | None = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Fill ``project`` from a template without overwriting authored content.

    ``selection`` holds the wizard checkboxes: lists of rule texts
    (``book_rules``), phrases (``banned_phrases``), world titles
    (``world_entries``) and archetypes (``characters``).  A missing key means
    "template defaults".  Returns the updated copy and a summary of what was
    added or skipped.
    """
    template = get_template(template_id)
    if not template:
        raise KeyError(template_id)
    result = deepcopy(project) if isinstance(project, dict) else {}
    selection = selection if isinstance(selection, dict) else {}
    values = normalize_answers(answers, template)
    fresh = not project_has_work(result)
    summary: dict[str, Any] = {"template": template_id, "fresh_project": fresh, "added": {}, "skipped": []}

    def fill(target: dict[str, Any], key: str, value: Any, empty: tuple[Any, ...] = ("",)) -> None:
        if value in ("", None):
            return
        current = target.get(key)
        if current is None or (isinstance(current, str) and current.strip() in empty) or current in empty:
            target[key] = value
        elif current != value:
            summary["skipped"].append(key)

    if values["title"]:
        fill(result, "title", values["title"], ("", "未命名故事", "我的新故事"))
    fill(result, "genre", values["genre"], ("", "幻想") if fresh else ("",))
    if fresh:
        result["story_mode"] = values["story_mode"]
    premise_lines = [values["premise"]]
    for label, key in (("主角", "protagonist"), ("主角想要", "want"), ("最大阻碍", "obstacle")):
        if values[key]:
            premise_lines.append(f"{label}：{values[key]}")
    fill(result, "premise", "\n".join(line for line in premise_lines if line))

    narrative = result.setdefault("narrative", {})
    if not isinstance(narrative, dict):
        narrative = result["narrative"] = {}
    template_narrative = template.get("narrative", {})
    pov = template_narrative.get("pov", "auto")
    tense = template_narrative.get("tense", "auto")
    fill(narrative, "pov", pov if pov in VALID_POV else "auto", ("", "auto"))
    fill(narrative, "tense", tense if tense in VALID_TENSE else "auto", ("", "auto"))
    fill(narrative, "tone", values["tone"] or template_narrative.get("tone", ""))
    structure = template_narrative.get("structure_profile", "adaptive")
    fill(narrative, "structure_profile", structure if structure in VALID_STRUCTURE else "adaptive", ("", "adaptive"))
    fill(narrative, "ending_direction", values["ending"])
    if fresh or not narrative.get("target_chapters"):
        narrative["target_chapters"] = values["target_chapters"]
    if fresh or not narrative.get("preferred_volume_count"):
        narrative["preferred_volume_count"] = values["volumes"]

    settings = result.setdefault("settings", {})
    if isinstance(settings, dict) and (fresh or not settings.get("target_words")):
        settings["target_words"] = values["target_words"]

    rules = _selected([{"text": line} for line in template.get("book_rules", [])], "text", selection.get("book_rules"))
    existing_rules = [line.strip() for line in str(result.get("book_rules") or "").splitlines() if line.strip()]
    new_rules = [item["text"] for item in rules if item["text"] not in existing_rules]
    extra_rules = [str(line).strip() for line in selection.get("extra_book_rules") or [] if str(line).strip()]
    new_rules += [line for line in extra_rules if line not in existing_rules and line not in new_rules]
    if new_rules:
        result["book_rules"] = "\n".join(existing_rules + new_rules)
    summary["added"]["book_rules"] = len(new_rules)

    governance = result.setdefault("governance", {})
    if not isinstance(governance, dict):
        governance = result["governance"] = {}
    current_banned = governance.get("banned_phrases") or []
    if isinstance(current_banned, str):
        current_banned = [item.strip() for item in current_banned.replace("，", ",").replace("\n", ",").split(",")]
    current_banned = [str(item).strip() for item in current_banned if str(item).strip()]
    chosen_banned = [item["phrase"] for item in _selected(template.get("banned_phrases", []), "phrase", selection.get("banned_phrases"), "default")]
    chosen_banned += [str(item).strip() for item in selection.get("extra_banned_phrases") or [] if len(str(item).strip()) >= 2]
    added_banned = [phrase for phrase in dict.fromkeys(chosen_banned) if phrase not in current_banned and len(phrase) >= 2]
    governance["banned_phrases"] = current_banned + added_banned
    summary["added"]["banned_phrases"] = len(added_banned)

    world = result.get("world_entries") if isinstance(result.get("world_entries"), list) else []
    titles = {str(entry.get("title", "")).strip() for entry in world if isinstance(entry, dict)}
    added_world = 0
    for item in _selected(template.get("world_entries", []), "title", selection.get("world_entries")):
        if item["title"] in titles:
            summary["skipped"].append(f"world:{item['title']}")
            continue
        world.append(_world_entry(item, 20 + 10 * len(world)))
        titles.add(item["title"])
        added_world += 1
    result["world_entries"] = world
    summary["added"]["world_entries"] = added_world

    cast = result.get("characters") if isinstance(result.get("characters"), list) else []
    taken = {str(c.get("wizard_archetype") or c.get("role") or "").strip() for c in cast if isinstance(c, dict)}
    taken |= {str(c.get("name") or "").strip() for c in cast if isinstance(c, dict)}
    added_cast = 0
    for item in _selected(template.get("characters", []), "archetype", selection.get("characters")):
        if item["archetype"] in taken or item.get("role") in taken or (
            item["archetype"] == "主角" and values["protagonist_name"] and values["protagonist_name"] in taken
        ):
            summary["skipped"].append(f"character:{item['archetype']}")
            continue
        cast.append(_character(item, values))
        taken.add(item["archetype"])
        added_cast += 1
    result["characters"] = cast
    summary["added"]["characters"] = added_cast
    return result, summary


def volume_instruction(answers: dict[str, Any] | None, template_id: str) -> str:
    """A planning hint the wizard pre-fills for the first master-plan run."""
    template = get_template(template_id) or get_template("general")
    values = normalize_answers(answers, template or {})
    parts = [f"全书计划 {values['target_chapters']} 章，分为 {values['volumes']} 卷，每章约 {values['target_words']} 字。"]
    if template and template.get("name") and template_id != "general":
        parts.append(f"题材：{template['name']}。")
    if values["tone"]:
        parts.append(f"文风气质：{values['tone']}。")
    return "".join(parts)
