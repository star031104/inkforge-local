"""Genre profiles for planning prompts.

Planning prompts used to be tuned on a single book and leaked its nouns and its
investigation-shaped plot into every project.  This module keeps the
deterministic volume structure (opening → escalation → turn → finale batches)
but lets each genre express those phases in its own dramatic vocabulary.

Everything here is plain data plus small pure helpers so prompt builders,
validators and tests can share one source of truth.
"""
from __future__ import annotations

import ipaddress
import math
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlparse

PHASES = ("opening", "escalation", "turn", "finale")
PHASE_NAMES = {
    "opening": "起势",
    "escalation": "升级",
    "turn": "转折",
    "finale": "收束",
}


@dataclass(frozen=True)
class GenreProfile:
    key: str
    label: str
    genre_keywords: tuple[str, ...]
    premise_keywords: tuple[str, ...]
    phase_focus: dict[str, str]
    slot_roles: dict[str, tuple[str, ...]]
    event_palette: tuple[str, ...]
    guidance: str
    batch_pressure: str = "新的压力、关系变化、资格或代价"
    structure_aliases: tuple[str, ...] = field(default_factory=tuple)


# Each phase lists six ordered roles so both 3-chapter (local) and 6-chapter
# (cloud) batches can be sampled evenly without repeating a role.
PROFILES: dict[str, GenreProfile] = {
    "history_transmigration": GenreProfile(
        key="history_transmigration",
        label="穿越/历史",
        genre_keywords=("穿越",),
        premise_keywords=("穿越", "穿回", "回到古代", "魂穿", "身穿"),
        phase_focus={
            "opening": "立场亮相：异乡人带着另一套观念落入时代现场，各方首次表态",
            "escalation": "交锋：观念与时代规则正面相撞，各方以言辞、利益和行动角力",
            "turn": "站队与代价：人物必须选边，盟友与敌手重新排列",
            "finale": "权力格局变化：本卷选择落地，时代局势出现不可逆的新格局",
        },
        slot_roles={
            "opening": (
                "让穿越者在具体时代场景中亮出与众不同的立场或判断",
                "让时代中的关键人物以自己的利益和信念作出回应",
                "让一次看似微小的介入引起旁人警觉或兴趣",
                "让穿越者认清自身知识的边界与身份的限制",
                "让各方第一次公开表态，形成初步阵营",
                "让穿越者作出第一个无法撤回的立场选择",
            ),
            "escalation": (
                "让一场议论、奏对或谈判把观念冲突摆上台面",
                "让对立一方用时代内合理的手段施压",
                "让盟友提出交换条件，合作不再无偿",
                "让穿越者的办法在时代条件下走样或付出意外代价",
                "让旁观的势力开始押注，局面变得更拥挤",
                "让一次交锋的结果改变人物在权力结构中的位置",
            ),
            "turn": (
                "让人物被迫在两方之间公开站队",
                "让一位重要人物改变立场或离开",
                "让此前的选择以具体后果反噬",
                "让穿越者的信念与时代现实正面冲突",
                "让力量对比出现明显倾斜",
                "用新的代价逼出终局前无法回避的抉择",
            ),
            "finale": (
                "把此前的立场、关系和代价汇聚到同一场交锋",
                "让各方亮出底牌，摊开真正的利益",
                "执行最终选择并承受相应代价",
                "让权力格局在具体场景中发生可见的变化",
                "让人物面对自己选择所改变的人与事",
                "只在最后一章兑现卷末状态，同时引出下一卷的新局面",
            ),
        },
        event_palette=(
            "朝堂议事与奏对", "辩难与说服", "结盟与背盟", "站队表态", "联姻与人质",
            "军情与战事", "宗族与门客", "礼法冲突", "时代观念碰撞", "人心向背",
        ),
        guidance=(
            "穿越/历史题材：推动力来自不同立场的人物在时代条件下的交锋与站队。"
            "穿越者的见识只能带来新视角，不能成为无代价的万能答案；"
            "时代中的人物同样聪明、有自己的利益和逻辑。每卷以权力格局或人物处境的变化收束。"
        ),
        batch_pressure="新的立场分化、交换条件、名分或代价",
        structure_aliases=(),
    ),
    "history_power": GenreProfile(
        key="history_power",
        label="历史/权谋",
        genre_keywords=(
            "历史", "权谋", "宫斗", "宅斗", "朝堂", "古代", "战国", "三国",
            "架空历史", "王朝", "帝王",
        ),
        premise_keywords=("朝廷", "皇帝", "王朝", "将军", "宰相", "藩王", "储君", "诸侯"),
        phase_focus={
            "opening": "布局：各方势力亮出诉求，人物在局中找到自己的位置",
            "escalation": "博弈：各方出招与应招，利益交换与暗中较量升级",
            "turn": "倒戈与代价：盟约松动，一方失势或反扑",
            "finale": "格局重排：本卷较量分出阶段胜负，权力结构改变",
        },
        slot_roles={
            "opening": (
                "让人物在具体场合中卷入各方利益的交汇点",
                "让一方势力亮出诉求并试探人物",
                "让人物第一次借势或出招",
                "让对手以时代内合理的方式回应",
                "让人物看清局中真正的利害",
                "让人物作出第一个无法撤回的选择",
            ),
            "escalation": (
                "让一次利益交换改变各方关系",
                "让有明确利益的对手采取具体反制",
                "让人物的出招带来意料之外的连锁反应",
                "让盟友提出更高的要价",
                "让一场公开的较量使局面升温",
                "让计划付出可见代价，并修正下一步方向",
            ),
            "turn": (
                "让盟约出现松动，一方倒向对手",
                "制造一次改变力量对比的重大挫败或有限胜利",
                "让此前埋下的隐患在关键时刻发作",
                "让人物被迫牺牲某人或某种名分",
                "让对手亮出更深一层的意图",
                "用新的代价逼出终局前无法回避的抉择",
            ),
            "finale": (
                "把此前的布局、关系和代价汇入最终较量",
                "让各方摊牌，逼出真实立场",
                "执行最终选择并完成核心对抗",
                "让胜负以具体的人事变动落地",
                "让人物面对胜利或失败的余波",
                "只在最后一章兑现卷末状态，同时制造下一卷的具体问题",
            ),
        },
        event_palette=(
            "朝会与廷议", "密谈与结盟", "联姻与人质", "任免与贬谪", "军务与边事",
            "礼法之争", "家族与派系", "流言与名声", "宴饮与试探", "兵变或政变",
        ),
        guidance=(
            "历史/权谋题材：推动力来自各方利益的布局、交换与反制。"
            "每个人物都按自身利益和时代规则行事，不靠对手降智取胜；"
            "胜负以具体的人事、名分和格局变化体现。"
        ),
        batch_pressure="新的利益交换、名分、筹码或代价",
    ),
    "mystery": GenreProfile(
        key="mystery",
        label="悬疑/推理",
        genre_keywords=("悬疑", "推理", "侦探", "刑侦", "犯罪", "惊悚", "探案", "密室"),
        premise_keywords=("命案", "凶手", "失踪", "案件", "谜团", "尸体", "嫌疑", "真相"),
        phase_focus={
            "opening": "谜面：抛出核心谜团，读者与人物获得第一批线索",
            "escalation": "线索与误导：线索累积，合理的误判把人物引向错误方向",
            "turn": "反转：关键事实推翻此前判断，危险逼近",
            "finale": "揭示：真相回指已见线索，代价落地",
        },
        slot_roles={
            "opening": (
                "以一个具体异常事件抛出本卷谜团",
                "让人物接触第一批线索与相关人物",
                "让一个看似合理的初步解释成形",
                "让一名关系人隐瞒或说谎",
                "让一条不合常理的细节被注意到",
                "让人物决定追查并因此付出第一份代价",
            ),
            "escalation": (
                "让新线索把嫌疑引向某个方向",
                "让一次误导使人物作出错误判断",
                "让对手或真凶采取行动掩盖痕迹",
                "让一名证人或关系人的说法出现矛盾",
                "让危险第一次落到人物或其亲近者身上",
                "让人物修正推理并锁定新的追查方向",
            ),
            "turn": (
                "让一个关键事实推翻此前的判断",
                "让嫌疑转移到意想不到的人身上",
                "让人物的追查付出沉重代价",
                "让隐藏的动机或关系浮出水面",
                "让真凶或对手抢先一步",
                "用新的危险逼出终局前必须作出的抉择",
            ),
            "finale": (
                "把此前的线索、误导和代价汇入最终对质",
                "让人物设局或面对面揭穿谎言",
                "执行最终行动并完成核心对抗",
                "让真相回指读者已见过的线索",
                "让揭示的余波改变人物关系",
                "只在最后一章兑现卷末状态，同时留下下一卷的新谜面",
            ),
        },
        event_palette=(
            "现场勘查", "证词矛盾", "关键线索", "误导与假象", "嫌疑转移",
            "对质与审问", "二次事件", "跟踪与设局", "动机浮现", "真相揭示",
        ),
        guidance=(
            "悬疑/推理题材：跟踪读者已见线索、合理误判与待解问题。"
            "揭示必须回指此前已经出现的线索，不靠巧合或临时设定破案；"
            "不要求每章破案或反转，但每章应改变读者对谜团的理解。"
        ),
        batch_pressure="新的线索、疑点、危险或代价",
        structure_aliases=("mystery",),
    ),
    "romance": GenreProfile(
        key="romance",
        label="言情/关系",
        genre_keywords=(
            "言情", "爱情", "恋爱", "甜宠", "虐恋", "耽美", "百合", "婚恋",
            "情感", "纯爱", "古言", "现言",
        ),
        premise_keywords=("相爱", "恋人", "婚约", "暗恋", "重逢", "相遇", "联姻", "前任"),
        phase_focus={
            "opening": "相遇：两人在具体处境中相遇，各自带着创伤与渴望",
            "escalation": "误解与靠近：靠近与退缩交替，误解来自各自真实的顾虑",
            "turn": "考验：外部压力或内心伤口迫使关系面临断裂",
            "finale": "抉择：人物为关系作出代价清楚的选择",
        },
        slot_roles={
            "opening": (
                "让两人在一个具体处境中相遇或重逢",
                "让彼此留下鲜明却不完整的第一印象",
                "让一次被迫的共处暴露各自的处境",
                "让一方看见另一方不为人知的一面",
                "让外部关系（家人、朋友、对手）介入两人之间",
                "让一方作出第一个靠近或回避的选择",
            ),
            "escalation": (
                "让一个日常细节拉近两人的距离",
                "让一次误解来自各自真实的顾虑",
                "让第三方或外部处境制造阻隔",
                "让一方袒露部分心事，另一方回应或退缩",
                "让关系的边界被一次具体事件试探",
                "让两人对彼此的认识发生可见变化",
            ),
            "turn": (
                "让外部压力迫使两人站到对立位置",
                "让一方的秘密或旧伤被揭开",
                "让一次选择伤害了对方或自己",
                "让关系出现几乎无法挽回的距离",
                "让人物独自面对自己真正想要的东西",
                "用新的代价逼出终局前无法回避的抉择",
            ),
            "finale": (
                "把此前的误解、靠近和伤害汇入一次正面相对",
                "让人物说出一直没说出口的话",
                "让人物为关系作出代价清楚的选择",
                "让选择在具体行动中兑现",
                "让关系进入新的状态并被周围的人看见",
                "只在最后一章兑现卷末状态，同时留下下一卷的情感问题",
            ),
        },
        event_palette=(
            "相遇与重逢", "被迫共处", "误解与解释", "日常细节", "第三者介入",
            "家庭压力", "秘密袒露", "分离与等待", "告白与拒绝", "承诺与抉择",
        ),
        guidance=(
            "言情/关系题材：关注距离、信任、误解和边界的细微变化。"
            "误解必须来自人物真实的顾虑而不是降智；允许日常共处和沉默积累意义，"
            "不强制每章争执，关系变化要有可见的行动承载。"
        ),
        batch_pressure="新的心结、距离变化、承诺或代价",
        structure_aliases=("relationship",),
    ),
    "fantasy": GenreProfile(
        key="fantasy",
        label="奇幻/玄幻/仙侠",
        genre_keywords=(
            "玄幻", "仙侠", "修仙", "修真", "奇幻", "魔幻", "武侠", "异世",
            "异界", "西幻", "神话", "高武",
        ),
        premise_keywords=("修炼", "宗门", "灵根", "魔法", "法术", "妖", "神器", "境界", "秘境"),
        phase_focus={
            "opening": "入局：人物踏入新的世界层级，规则与危险初现",
            "escalation": "历练：试炼、争夺与成长，力量提升伴随代价",
            "turn": "劫难：更强的敌手或世界真相打破既有认知",
            "finale": "破局：以成长与牺牲换取阶段胜利，世界版图扩展",
        },
        slot_roles={
            "opening": (
                "让人物在具体场景中遭遇改变命运的机缘或变故",
                "让世界的一条规则或力量体系以代价的形式显现",
                "让人物结识引路人、同伴或宿敌",
                "让人物第一次运用能力并看清自己的上限",
                "让一股更大的势力注意到人物",
                "让人物作出踏入新天地的选择",
            ),
            "escalation": (
                "让一次试炼或争夺考验人物的能力与心性",
                "让对手以自身规则和资源施压",
                "让同伴之间因利益或理念产生分歧",
                "让力量的提升伴随明确的代价",
                "让一处新地点或秘境展开新的危险",
                "让人物在挫败中找到新的道路",
            ),
            "turn": (
                "让更强的敌手或势力正面压迫",
                "让世界的隐藏真相改变人物的认知",
                "让重要的同伴或依靠遭受损失",
                "让人物的道心或信念受到动摇",
                "让局势逼人物冒险突破",
                "用新的代价逼出终局前无法回避的抉择",
            ),
            "finale": (
                "把此前的成长、羁绊和代价汇入最终一战",
                "让敌我双方亮出真正底牌",
                "执行最终选择并完成核心对抗",
                "让胜负以具体的牺牲或突破落地",
                "让世界格局或人物地位发生可见变化",
                "只在最后一章兑现卷末状态，同时打开下一卷的新天地",
            ),
        },
        event_palette=(
            "机缘与变故", "试炼与比斗", "秘境探索", "宗门或势力纷争", "功法与突破",
            "宝物争夺", "妖魔或异族", "师徒与同伴", "世界规则显现", "强敌压境",
        ),
        guidance=(
            "奇幻/玄幻/仙侠题材：力量体系必须有清晰规则和代价，成长来自选择与付出，"
            "不靠无代价的外挂。世界观随卷逐步展开，每卷带来新的层级、地点或真相。"
        ),
        batch_pressure="新的敌意、境界瓶颈、羁绊或代价",
        structure_aliases=("adventure",),
    ),
    "scifi": GenreProfile(
        key="scifi",
        label="科幻",
        genre_keywords=(
            "科幻", "赛博", "星际", "末世", "机甲", "太空", "废土",
            "人工智能", "时间旅行",
        ),
        premise_keywords=("飞船", "星球", "人工智能", "AI", "克隆", "殖民", "实验室", "外星"),
        phase_focus={
            "opening": "设定落地：一个技术或社会设定改变人物的日常处境",
            "escalation": "推演：设定的连锁后果扩散，各方以不同立场应对",
            "turn": "临界：设定的代价暴露，人物信念与系统冲突",
            "finale": "抉择：人物在设定的极限处作出选择，世界状态改变",
        },
        slot_roles={
            "opening": (
                "让一个具体的技术或社会现象闯入人物生活",
                "让人物所处世界的规则通过一次事件显形",
                "让人物遇到与此设定利害相关的他人",
                "让人物第一次利用或对抗这一设定",
                "让设定的第一个意外后果出现",
                "让人物作出无法撤回的第一个选择",
            ),
            "escalation": (
                "让设定的连锁后果波及更多人",
                "让不同立场的人以各自方式应对",
                "让人物的应对方式产生副作用",
                "让一个机构、集体或对手施加压力",
                "让人物发现设定背后更深的运作方式",
                "让计划付出可见代价，并修正下一步方向",
            ),
            "turn": (
                "让设定的真实代价暴露",
                "让人物的信念与系统正面冲突",
                "让一位同伴作出不同选择",
                "让局势逼近不可逆的临界点",
                "让人物失去关键依靠",
                "用新的代价逼出终局前无法回避的抉择",
            ),
            "finale": (
                "把此前的发现、关系和代价汇入最终行动",
                "让各方在临界点上摊牌",
                "执行最终选择并完成核心对抗",
                "让世界状态发生可见的变化",
                "让人物承受选择带来的后果",
                "只在最后一章兑现卷末状态，同时提出下一卷的新问题",
            ),
        },
        event_palette=(
            "技术异变", "社会规则显形", "远航或迁徙", "实验与事故", "人机关系",
            "资源争夺", "身份与记忆", "集体与个体冲突", "外部威胁", "临界抉择",
        ),
        guidance=(
            "科幻题材：从一个核心设定推演其对个人和社会的连锁后果，"
            "设定内部逻辑自洽，技术有限制与代价；人物的选择而非技术本身决定结局。"
        ),
        batch_pressure="新的后果、限制、立场分化或代价",
    ),
    "realist": GenreProfile(
        key="realist",
        label="现实/文学",
        genre_keywords=(
            "现实", "文学", "都市", "家庭", "成长", "乡土", "职场", "年代",
            "青春", "纯文学", "群像",
        ),
        premise_keywords=("家庭", "父亲", "母亲", "工作", "故乡", "童年", "婚姻", "生活"),
        phase_focus={
            "opening": "处境：人物在日常中显出缺口与渴望",
            "escalation": "侵蚀：生活中的压力与关系一点点改变人物",
            "turn": "裂变：一个事件迫使人物重新看待自己与他人",
            "finale": "余响：人物作出选择，生活以新的样子继续",
        },
        slot_roles={
            "opening": (
                "让人物在一个具体的日常场景中显出处境",
                "让人物的渴望或缺口在细节里浮现",
                "让身边的人以各自的方式与人物发生关联",
                "让一件小事打破日常的平衡",
                "让人物隐约意识到需要改变",
                "让人物作出一个看似微小却有后果的选择",
            ),
            "escalation": (
                "让生活压力以具体事件出现",
                "让一段关系发生细微却可见的变化",
                "让人物的应对方式带来新的困境",
                "让他人的处境映照出人物自身",
                "让一次相处或冲突积累意义",
                "让人物对自己的认识发生偏移",
            ),
            "turn": (
                "让一个事件迫使人物重新审视自己",
                "让一段重要关系发生断裂或转向",
                "让此前回避的问题无法再回避",
                "让人物失去某种曾经确定的东西",
                "让人物独自面对内心矛盾",
                "用新的代价逼出终局前无法回避的抉择",
            ),
            "finale": (
                "把此前的积累汇入一个具体时刻",
                "让人物面对最重要的人或事",
                "让人物作出选择并承担它",
                "让选择以日常中的具体变化体现",
                "让余韵留在意象或细节中",
                "只在最后一章兑现卷末状态，同时留下下一阶段的生活问题",
            ),
        },
        event_palette=(
            "日常场景", "家庭关系", "工作处境", "代际冲突", "离开与归来",
            "友情变化", "身体与疾病", "金钱与生计", "回忆触发", "微小选择",
        ),
        guidance=(
            "现实/文学题材：允许感知、日常、内在矛盾和意象承担推进；"
            "保留含混与留白，不强求悬崖结尾和显性代价，但每章应让人物的处境或认识有所移动。"
        ),
        batch_pressure="新的处境、关系变化、认识或代价",
        structure_aliases=("literary",),
    ),
    "general": GenreProfile(
        key="general",
        label="通用",
        genre_keywords=(),
        premise_keywords=(),
        phase_focus={
            "opening": "起势：建立本卷独有问题、行动入口和第一次受阻",
            "escalation": "升级：让既有行动产生反制、代价和方向变化",
            "turn": "转折：力量对比或人物关系出现明显倾斜",
            "finale": "收束：完成本卷最终选择与代价，并触发下一卷",
        },
        slot_roles={
            "opening": (
                "建立本卷独有问题与人物当下处境",
                "让人物采取第一次具体行动",
                "让行动得到局部结果并暴露新的变量",
                "让相关人物以各自利益作出回应",
                "遭遇第一次阻力，使原计划受挫",
                "作出不可轻易撤回的选择",
            ),
            "escalation": (
                "让人物换一种方式推进，不重复上一批的方法",
                "让有明确利益的对手采取具体反制",
                "让合作关系因利益或理念发生变化",
                "让行动带来意料之外的后果",
                "让局面中加入新的力量或处境",
                "让计划付出可见代价，并据此修正下一阶段方向",
            ),
            "turn": (
                "让支线或人物关系与主线正面碰撞",
                "制造一次改变力量对比的重大挫败或有限胜利",
                "让此前的选择以具体后果反噬",
                "让人物失去关键支持",
                "让人物重新认识局势或自己",
                "用新的代价逼出终局前无法回避的抉择",
            ),
            "finale": (
                "把此前的行动、关系和代价汇入最终抉择",
                "让各方亮出真实立场",
                "执行最终选择并完成核心对抗",
                "让结果以具体的变化落地",
                "让人物面对选择的余波",
                "只在最后一章兑现卷末状态，同时制造下一卷的具体问题",
            ),
        },
        event_palette=(
            "具体行动", "阻力与反制", "合作与分歧", "意外后果", "关系变化",
            "处境转变", "选择与代价", "新力量介入", "失去与获得", "阶段收束",
        ),
        guidance=(
            "通用结构：根据人物欲望、上一场后果和未解决冲突选择下一步；"
            "允许行动、观察、余波或关系积累，由具体情势决定节奏，不按固定模板机械轮换。"
        ),
        batch_pressure="新的压力、关系变化、资格或代价",
    ),
}

# Higher priority wins when the genre string names several genres: the more
# specific structural driver (a mystery, a romance, a magic system) shapes the
# volume more than the historical setting it happens in.
_PRIORITY = (
    "mystery", "romance", "fantasy", "scifi",
    "history_transmigration", "history_power", "realist",
)
_FANTASY_MARKERS = PROFILES["fantasy"].genre_keywords


def _hits(text: str, keywords: tuple[str, ...]) -> int:
    return sum(1 for keyword in keywords if keyword and keyword in text)


def detect_genre_key(project: dict[str, Any]) -> str:
    """Return the profile key for a project from its genre label and premise."""
    narrative = project.get("narrative") if isinstance(project.get("narrative"), dict) else {}
    explicit = str(narrative.get("genre_profile", "") or "").strip()
    if explicit in PROFILES:
        return explicit
    genre = str(project.get("genre", "") or "")
    premise = str(project.get("premise", "") or "")
    if genre.strip():
        if "穿越" in genre and not any(marker in genre for marker in _FANTASY_MARKERS):
            # "历史穿越·战国", "穿越" with any era, or a bare 穿越 label.
            if not any(
                _hits(genre, PROFILES[key].genre_keywords)
                for key in ("mystery", "romance", "scifi")
            ):
                return "history_transmigration"
        for key in _PRIORITY:
            if _hits(genre, PROFILES[key].genre_keywords):
                return key
    scores = {
        key: _hits(premise, PROFILES[key].premise_keywords)
        + _hits(premise, PROFILES[key].genre_keywords)
        for key in _PRIORITY
    }
    if scores["history_transmigration"] and not scores["fantasy"]:
        return "history_transmigration"
    best = max(_PRIORITY, key=lambda key: (scores[key], -_PRIORITY.index(key)))
    if scores[best]:
        return best
    structure = str(narrative.get("structure_profile", "") or "")
    for key, profile in PROFILES.items():
        if structure and structure in profile.structure_aliases:
            return key
    return "general"


def genre_profile(project: dict[str, Any]) -> GenreProfile:
    return PROFILES[detect_genre_key(project)]


PERIOD_MARKERS = ("历史", "古代", "战国", "架空")


def is_period_setting(project: dict[str, Any]) -> bool:
    """True for pre-modern settings whose routes must avoid modern vocabulary.

    "架空" alone is also used for invented fantasy/sci-fi worlds, where words
    such as 系统 or 数据 can be legitimate; those genres are excluded.
    """
    genre = str(project.get("genre", "") or "")
    if not any(marker in genre for marker in PERIOD_MARKERS):
        return False
    return detect_genre_key(project) not in {"fantasy", "scifi"}


def custom_structure(project: dict[str, Any]) -> str:
    """Author-written structure text when the project opted into it."""
    narrative = project.get("narrative") if isinstance(project.get("narrative"), dict) else {}
    if narrative.get("structure_profile") != "custom":
        return ""
    return str(narrative.get("custom_structure", "") or "").strip()[:3000]


def _sample(roles: tuple[str, ...] | list[str], count: int) -> list[str]:
    """Pick ``count`` ordered roles; the last role is always kept."""
    items = list(roles)
    if count <= 0 or not items:
        return []
    if count == 1:
        return [items[-1]]
    if count <= len(items):
        return [
            items[math.floor(index * (len(items) - 1) / (count - 1) + 0.5)]
            for index in range(count)
        ]
    # More chapters than roles: stretch the leading roles over consecutive
    # chapters with an explicit step marker so slots stay distinguishable,
    # while the final chapter keeps the phase's culminating role.
    head = items[:-1] or items
    result: list[str] = []
    seen: dict[int, int] = {}
    for index in range(count - 1):
        source = index * len(head) // (count - 1)
        seen[source] = seen.get(source, 0) + 1
        role = head[source]
        step = seen[source]
        result.append(role if step == 1 else f"{role}（第{step}步，换一种具体行动推进）")
    result.append(items[-1])
    return result


def batch_phase(batch_number: int, total_batches: int) -> str:
    if total_batches <= 1:
        return "single"
    if batch_number >= total_batches:
        return "finale"
    if batch_number == 1:
        return "opening"
    if batch_number == total_batches - 1:
        return "turn"
    return "escalation"


def batch_slot_roles(
    profile: GenreProfile, batch_number: int, total_batches: int, size: int
) -> list[str]:
    """Return exactly ``size`` slot roles for one volume-splitting batch."""
    phase = batch_phase(batch_number, total_batches)
    if phase == "single":
        pool: list[str] = []
        for name in PHASES:
            pool.extend(profile.slot_roles[name])
        return _sample(pool, size)
    return _sample(profile.slot_roles[phase], size)


def phase_description(profile: GenreProfile, batch_number: int, total_batches: int) -> str:
    phase = batch_phase(batch_number, total_batches)
    if phase == "single":
        return "；".join(profile.phase_focus[name] for name in PHASES)
    return profile.phase_focus[phase]


def palette_text(profile: GenreProfile) -> str:
    return "、".join(profile.event_palette)


def planning_guidance(project: dict[str, Any], *, include_custom: bool = True) -> str:
    """One paragraph of genre guidance; author custom structure takes priority."""
    profile = genre_profile(project)
    custom = custom_structure(project) if include_custom else ""
    text = (
        f"题材结构：{profile.label}。{profile.guidance}"
        f"可选事件类型（只作灵感，不必逐项使用）：{palette_text(profile)}。"
    )
    if custom:
        text = (
            f"作者自定义结构（优先于下列题材默认）：{custom}\n" + text
        )
    return text


# --------------------------------------------------------------------------
# Batch sizing for the volume splitter
# --------------------------------------------------------------------------

LOCAL_BATCH_SIZE = 3
CLOUD_BATCH_SIZE = 6


def _fallback_is_local(settings: dict[str, Any]) -> bool:
    from .providers import guess_server, normalize_base_url

    base = normalize_base_url((settings or {}).get("base_url", ""))
    if not base:
        return True
    host = (urlparse(base).hostname or "").strip("[]").lower()
    try:
        address = ipaddress.ip_address(host)
        if address.is_loopback or address.is_private or address.is_link_local:
            return True
    except ValueError:
        if host == "localhost" or host.endswith((".local", ".localhost", ".lan")):
            return True
    return guess_server(base) in {"llama_cpp", "ollama"}


def is_local_model(settings: dict[str, Any]) -> bool:
    from . import providers

    checker = getattr(providers, "is_local_endpoint", None)
    if callable(checker):
        try:
            return bool(checker(settings or {}))
        except Exception:
            pass
    return _fallback_is_local(settings or {})


def volume_batch_size(settings: dict[str, Any]) -> int:
    """Three chapters per call for local 8–12B models, six for cloud APIs."""
    return LOCAL_BATCH_SIZE if is_local_model(settings) else CLOUD_BATCH_SIZE


def volume_batches(start: int, end: int, batch_size: int) -> list[tuple[int, int]]:
    """Split ``start..end`` into ceil(n/size) contiguous, near-equal batches.

    Balancing avoids a one-chapter final batch (e.g. 7 chapters at six per
    call become 4+3 instead of 6+1) so the volume ending is never squeezed
    into an isolated call.
    """
    count = max(0, end - start + 1)
    if count == 0:
        return []
    size = max(1, int(batch_size))
    batches = math.ceil(count / size)
    base, extra = divmod(count, batches)
    result: list[tuple[int, int]] = []
    cursor = start
    for index in range(batches):
        length = base + (1 if index < extra else 0)
        result.append((cursor, cursor + length - 1))
        cursor += length
    return result
