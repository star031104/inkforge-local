"""导出前清理：确定性的书稿检查与修复（不调用模型）。

Four groups of checks run over the selected chapters:

* ``punctuation`` – quote style, dashes, ellipses, half-width punctuation and
  stray spaces between CJK characters, duplicated punctuation, paragraph
  whitespace and runs of blank lines.  Everything except unbalanced quotes is
  auto-fixable; each rule is a separate opt-in fix key.
* ``repetition`` – the same distinctive word repeated inside a short window,
  identical openings of consecutive paragraphs and book-level overused
  phrases.  Report only.
* ``names`` – near-miss spellings of character names/aliases (Hamming
  distance 1 for 2–3 character names; homophones when ``pypinyin`` happens to
  be installed).  Fixed only through mappings the author confirms.
* ``formatting`` – mixed chapter numbering styles (fixable), numbering gaps,
  duplicate titles and empty chapters (report only).

Fixes never silently touch locked chapters: they are skipped unless the caller
explicitly includes them.  The router persists the result with a non-autosave
reason, which makes the project store snapshot every changed chapter into its
version history, so each cleanup can be undone chapter by chapter.
"""

from __future__ import annotations

import re
from collections import Counter, defaultdict
from collections.abc import Callable, Iterable
from copy import deepcopy
from typing import Any

from app.review.manuscript_quality import _fatigued_phrases
from app.review.local_quality import AI_CLICHES
from app.writing.antislop import banned_phrases

try:  # optional: homophone detection only when the author already has it
    from pypinyin import lazy_pinyin as _lazy_pinyin  # type: ignore[import-not-found]
except Exception:  # pragma: no cover - depends on the local environment
    _lazy_pinyin = None

VERSION_REASON = "export-cleanup"
MAX_ISSUES_PER_GROUP = 400

CJK = "\u4e00-\u9fff\u3400-\u4dbf"
CJK_RE = re.compile(f"[{CJK}]")
OPEN_QUOTES = "“‘「『"
CLOSE_QUOTES = "”’」』"
CJK_PUNCT = "，。、；：？！…—“”‘’「」『』（）《》"
_CJK_OR_PUNCT = f"[{CJK}{re.escape(CJK_PUNCT)}]"

CATEGORY_LABELS = {
    "punctuation": "标点规范",
    "repetition": "重复用词",
    "names": "人名一致性",
    "formatting": "章节格式",
}
FIX_LABELS = {
    "quotes": "引号统一",
    "dashes": "破折号统一为——",
    "ellipsis": "省略号统一为……",
    "halfwidth": "中文间半角标点改全角",
    "spaces": "删除中文字符间多余空格",
    "duplicate_punct": "删除重复标点（。。/，，）",
    "whitespace": "删除段首段尾空白",
    "blank_lines": "合并多余空行",
    "title_numbering": "统一章节编号写法",
}
CODE_LABELS = {
    **FIX_LABELS,
    "unbalanced_quote": "引号不成对",
    "window_repeat": "短距离重复用词",
    "same_opening": "相邻段落开头相同",
    "overused_phrase": "全书高频套话",
    "name_variant": "疑似人名错写",
    "numbering_gap": "章节编号不连续",
    "duplicate_title": "章节标题重复",
    "empty_chapter": "空章节",
}
FIX_KEYS = tuple(FIX_LABELS)
PUNCTUATION_FIXES = ("quotes", "dashes", "ellipsis", "halfwidth", "spaces", "duplicate_punct", "whitespace", "blank_lines")

# Roughly the most frequent Chinese characters.  A "variant" spelling of a name
# produced by swapping in one of these is almost always an ordinary word
# (林中 / 林子 next to 林砚), so they are not reported without pinyin evidence.
COMMON_TEXT = (
    "的一是不了在人有我他这个们中来上大为和国地到以说时要就出会可也你对生能而子那得于着下自之年过发后作里用道行所然家种事成方多经么去法学如都同现当没动面起看定天分还进好小部其些主样理心她本前开但因只从想实日军者意无力它与长把机十民第公此已工使情明性知全三又关点正业外将两高间由问很最重并物手应战向头文体政美相见被利什二等产或新己制身果加西斯月话合回特代内信表化老给世位次度门任常先海通教儿原东声提立及比员解水名真论处走义各入几口认条平系气题活尔更别打女变四神总何电数安少报才结反受目太量再感建务做接必场件计管期市直德资命山金指克许统区保至队形社便空决治展马科司五基眼书非则听白却界达光放强即像难且权思王象完设式色路记南品住告类求据程北边死张该交规万取拉格望觉术领共确传师观清今切院让识候带导争运笑飞风步改收根干造言联持组每济车亲极林服快办议往元英士证近失转夫令准布始怎呢存未远叫台单影具罗字爱击流备兵连调深商算质团集百需价花党华城石级整府离况亚请技际约示复病息究线似官火断精满支视消越器容照须九增研写称企八功吗包片史委乎查轻易早曾除农找装广显吧阿李标谈吃图念六引历首医局突专费号尽另周较注语仅考落青随选列武红响虽推势参希古众构房半节土投某案黑维革划敌致陈律足态护七兴派孩验责营星够章音跟志底站严巴例防族供效续施留讲型料终答紧黄绝奇察母京段依批群项故按河米围江织害斗双境客纪采举杀攻父苏密低朝友诉止细愿千值仍男钱破网热助倒育属坐帝限船脸职速刻乐否刚威毛状率甚独球般普怕弹校苦创假久错承印晚兰试股拿脑预谁益阳若哪微尼继送急血惊伤素药适波夜省初喜卫源食险待述陆习置居劳财环排福纳欢雷警获模充负云停木游龙树疑层冷洲冲射略范竟句室异激汉村哈策演简卡罪判担州静退既衣您宗积余痛检差富灵协角占配征修皮挥胜降阶审沉坚善妈刘读啊超免压银买皇养伊怀执副乱抗犯追帮宣佛岁航优怪香著田铁控税左右份穿艺背阵草脚概恶块顿敢守酒岛托央户烈洋哥索胡款靠评版宝座释景顾弟登货互付伯慢欧换闻危忙核暗姐介坏讨丽良序升监临亮露永呼味野架域沙掉括舰鱼杂误湾吉减编楚肯测败屋跑梦散温困剑渐封救贵枪缺楼县尚毫移娘朋画班智亦耳恩短掌恐遗固席松秘谢鲁遇康虑幸均销钟诗藏赶剧票损忽巨炮旧端探湖录叶春乡附吸予礼港雨呀板庭妇归睛饭额含顺输摇招婚脱补谓督毒油疗旅泽材灭逐莫笔亡鲜词圣择寻厂睡博勒烟授诺伦岸奥唐卖俄炸载洛健堂旁宫喝借君禁阴园谋宋避抓荣姑孙逃牙束跳顶玉镇雪午练迫爷篇肉嘴馆遍凡础洞卷坦牛宁纸诸训私庄祖丝翻暴森塔默握戏隐熟骨访弱蒙歌店鬼软典欲萨伙遭盘爸扩盖弄雄稳忘亿刺拥徒姆杨齐赛趋曲刀床迎冰虚玩析窗醒妻透购替塞努休虎扬途侵刑绿兄迅套贸毕唯谷轮库迹尤竞街促延震弃甲伟麻川申缓潜闪售灯针哲络抵朱埃抱鼓植纯夏忍页杰筑折郑贝尊吴秀混臣雅振染盛怒舞圆搞狂措姓残秋培迷诚宽宇猛摆梅毁伸摩盟末乃悲拍丁赵"
    "啦嘛吧呗哦嗯唉哎呃喔么噢咦哟着过们"
)
COMMON_CHARS = set(COMMON_TEXT)
# The few hundred most frequent characters: a two-character repeat made only of
# these is usually a word boundary rather than a distinctive word.
VERY_COMMON_CHARS = set(COMMON_TEXT[:300])
# Function words / generic vocabulary that are never "distinctive" repetition.
STOPWORDS = set(
    "他们 她们 它们 我们 你们 自己 一个 一下 一样 一边 一直 一起 一点 一些 一切 没有 什么 这个 那个 这些 那些 这样 那样 这里 那里 "
    "知道 起来 时候 已经 还是 不是 就是 可以 出来 过来 下来 上来 回来 进来 看着 说道 怎么 为什么 因为 所以 但是 可是 如果 虽然 "
    "只是 只有 还有 不过 而且 然后 于是 突然 似乎 仿佛 好像 也许 或许 可能 应该 需要 必须 能够 不能 不会 不要 不用 现在 刚才 "
    "之后 之前 以后 以前 时间 地方 东西 事情 问题 声音 目光 眼睛 身体 身上 心里 面前 身后 旁边 一眼 一声 一次 两个 几个 那么 这么 "
    "非常 十分 有些 有点 有人 别人 人们 对方 彼此 所有 其中 之间 之中 当中 这种 那种 一种 这时 那时 此时 此刻 然而 不得 不住 "
    "不禁 觉得 感觉 看到 听到 想到 看见 听见 发现 开始 继续 终于 已然 依旧 仍然 还在 正在 正是 便是 却是 也是 都是 又是 只能 "
    "说话 回答 问道 笑道 点头 摇头 转身 抬头 低头 走了 来了 去了 到了 好了 对了 是的 不是 没事 怎样 哪里 谁的 我的 你的 他的 她的".split()
)
EDGE_CHARS = set("的一是在了着也与及或而就都将把被让向从于为有无这那其之并却又更很他她它们我你吗呢吧啊么")
SEPARATOR_LINE = re.compile(r"^[\s\-—–―*＊·•=~～#]+$")

_CN_NUM = "零〇一二两三四五六七八九十百千"
_CN_DIGIT_VALUE = {"零": 0, "〇": 0, "一": 1, "二": 2, "两": 2, "三": 3, "四": 4, "五": 5, "六": 6, "七": 7, "八": 8, "九": 9}
_CN_UNIT_VALUE = {"十": 10, "百": 100, "千": 1000}
TITLE_ARABIC = re.compile(r"^(\s*第\s*)([0-9０-９]+)(\s*[章回节话])")
TITLE_CN = re.compile(rf"^(\s*第\s*)([{_CN_NUM}]+)(\s*[章回节话])")


# ----------------------------------------------------------------- helpers

def chinese_number(value: int) -> str:
    from app.io.export_formats import chinese_number as convert

    return convert(value)


def parse_chinese_number(text: str) -> int | None:
    """一 → 1, 十二 → 12, 一百零五 → 105, 二〇三 → 203 (digit-by-digit form)."""
    text = str(text or "").strip()
    if not text:
        return None
    if all(char in _CN_DIGIT_VALUE for char in text):
        value = 0
        for char in text:
            value = value * 10 + _CN_DIGIT_VALUE[char]
        return value
    total, digit = 0, 0
    for char in text:
        if char in _CN_DIGIT_VALUE:
            digit = _CN_DIGIT_VALUE[char]
        elif char in _CN_UNIT_VALUE:
            total += (digit or 1) * _CN_UNIT_VALUE[char]
            digit = 0
        else:
            return None
    return total + digit


def _arabic_value(text: str) -> int:
    return int(text.translate(str.maketrans("０１２３４５６７８９", "0123456789")))


def title_number(title: str) -> tuple[str, int | None]:
    """Return (style, number) where style is ``arabic``, ``cn`` or ``""``."""
    match = TITLE_ARABIC.match(str(title or ""))
    if match:
        return "arabic", _arabic_value(match.group(2))
    match = TITLE_CN.match(str(title or ""))
    if match:
        return "cn", parse_chinese_number(match.group(2))
    return "", None


def _excerpt(text: str, start: int = 0, end: int | None = None, radius: int = 18) -> str:
    end = start if end is None else end
    left = max(0, start - radius)
    right = min(len(text), end + radius)
    return ("…" if left else "") + text[left:right].replace("\n", " ") + ("…" if right < len(text) else "")


def _has_cjk(text: str) -> bool:
    return bool(CJK_RE.search(text))


def _chapter_number_map(project: dict[str, Any]) -> list[tuple[int, dict[str, Any]]]:
    return [
        (number, chapter)
        for number, chapter in enumerate(project.get("chapters", []) or [], start=1)
        if isinstance(chapter, dict)
    ]


def select_chapters(project: dict[str, Any], options: dict[str, Any] | None = None) -> list[tuple[int, dict[str, Any]]]:
    options = options or {}
    chapters = _chapter_number_map(project)
    ids = {str(item) for item in options.get("chapter_ids") or [] if str(item)}
    if ids:
        return [(number, chapter) for number, chapter in chapters if str(chapter.get("id", "")) in ids]
    start = int(options.get("start", 1) or 1)
    end = int(options.get("end", 0) or 0) or len(chapters)
    return [(number, chapter) for number, chapter in chapters if start <= number <= end]


def is_locked(chapter: dict[str, Any]) -> bool:
    return str(chapter.get("authority_state", "")) == "locked"


def _paragraph_index(lines: list[str]) -> list[int]:
    """Map each line to its index among non-empty paragraphs (-1 for blank lines)."""
    result, counter = [], 0
    for line in lines:
        if line.strip():
            result.append(counter)
            counter += 1
        else:
            result.append(-1)
    return result


def _offset_to_paragraph(content: str) -> Callable[[int], int]:
    starts: list[tuple[int, int]] = []
    offset, counter = 0, 0
    for line in content.split("\n"):
        if line.strip():
            starts.append((offset, counter))
            counter += 1
        offset += len(line) + 1

    def locate(position: int) -> int:
        found = 0
        for start, index in starts:
            if start > position:
                break
            found = index
        return found

    return locate


# ------------------------------------------------------------ punctuation

def dominant_quote_style(project: dict[str, Any]) -> str:
    curly = corner = 0
    for _, chapter in _chapter_number_map(project):
        text = str(chapter.get("content", ""))
        curly += sum(text.count(char) for char in "“”")
        corner += sum(text.count(char) for char in "「」")
    return "corner" if corner > curly else "curly"


def _quote_marks(style: str) -> tuple[str, str, str, str]:
    return ("「", "」", "『", "』") if style == "corner" else ("“", "”", "‘", "’")


def _alternate(text: str, targets: str, opener: str, closer: str, *, skip: Callable[[str, int], bool] | None = None) -> tuple[str, int]:
    chars = list(text)
    changed = 0
    is_open = True
    for index, char in enumerate(chars):
        if char not in targets or (skip and skip(text, index)):
            continue
        replacement = opener if is_open else closer
        if replacement != char:
            chars[index] = replacement
            changed += 1
        is_open = not is_open
    return "".join(chars), changed


def _is_apostrophe(text: str, index: int) -> bool:
    before = text[index - 1] if index > 0 else ""
    after = text[index + 1] if index + 1 < len(text) else ""
    return before.isascii() and before.isalnum() and after.isascii() and after.isalnum()


def fix_quotes(line: str, style: str) -> tuple[str, int]:
    if not _has_cjk(line):
        return line, 0
    open_d, close_d, open_s, close_s = _quote_marks(style)
    if style == "corner":
        mapping = {"“": open_d, "”": close_d, "‘": open_s, "’": close_s}
    else:
        mapping = {"「": open_d, "」": close_d, "『": open_s, "』": close_s}
    changed = sum(line.count(char) for char in mapping)
    text = "".join(mapping.get(char, char) for char in line)
    text, ascii_double = _alternate(text, '"＂', open_d, close_d)
    text, ascii_single = _alternate(text, "'＇", open_s, close_s, skip=_is_apostrophe)
    changed += ascii_double + ascii_single
    # Same-direction pairs such as “你好“ – only re-sequence when the count is
    # even, because an odd count may be the multi-paragraph speech convention.
    for opener, closer in ((open_d, close_d), (open_s, close_s)):
        marks = [char for char in text if char in (opener, closer)]
        expected = [opener if index % 2 == 0 else closer for index in range(len(marks))]
        if marks and len(marks) % 2 == 0 and marks != expected:
            text, count = _alternate(text, opener + closer, opener, closer)
            changed += count
    return text, changed


_DASH_RE = re.compile(r"-{2,}|[—―─－]+|–{2,}")
_ELLIPSIS_RE = re.compile(r"\.{3,}|。{3,}|…+|·{3,}|．{3,}|、{3,}")
_HALFWIDTH = {",": "，", "?": "？", "!": "！", ":": "：", ";": "；", ".": "。"}
_HALFWIDTH_RE = re.compile(
    rf"(?<=[{CJK}”’」』）》])([,?!:;.])(?=[{CJK}“”‘’「」『』（《]|\s|$)"
)
_SPACES_RE = re.compile(rf"(?<={_CJK_OR_PUNCT})[ \t\u3000\u00a0]+(?={_CJK_OR_PUNCT})")
_DUPLICATE_RE = re.compile(r"([，。、；：])\1+")


def fix_dashes(line: str) -> tuple[str, int]:
    if SEPARATOR_LINE.match(line):
        return line, 0
    count = 0

    def replace(match: re.Match[str]) -> str:
        nonlocal count
        value = match.group()
        if value == "——":
            return value
        if value in {"—", "－", "─", "―"}:
            before = line[match.start() - 1] if match.start() else ""
            after = line[match.end()] if match.end() < len(line) else ""
            # A lone dash between ASCII letters/digits is a hyphen or a range.
            if (before.isascii() and before.isalnum()) and (after.isascii() and after.isalnum()):
                return value
        count += 1
        return "——"

    text = _DASH_RE.sub(replace, line)
    return text, count


def fix_ellipsis(line: str) -> tuple[str, int]:
    count = 0

    def replace(match: re.Match[str]) -> str:
        nonlocal count
        if match.group() != "……":
            count += 1
        return "……"

    return _ELLIPSIS_RE.sub(replace, line), count


def fix_halfwidth(line: str) -> tuple[str, int]:
    count = 0

    def replace(match: re.Match[str]) -> str:
        nonlocal count
        count += 1
        return _HALFWIDTH[match.group(1)]

    return _HALFWIDTH_RE.sub(replace, line), count


def fix_spaces(line: str) -> tuple[str, int]:
    leading = re.match(r"^[ \t\u3000\u00a0]*", line).group()
    body = line[len(leading):]
    text, count = _SPACES_RE.subn("", body)
    return leading + text, count


def fix_duplicate_punct(line: str) -> tuple[str, int]:
    return _DUPLICATE_RE.subn(r"\1", line)


def fix_whitespace(line: str) -> tuple[str, int]:
    stripped = line.strip(" \t\u3000\u00a0\r")
    return stripped, int(stripped != line)


LINE_FIXERS: dict[str, Callable[..., tuple[str, int]]] = {
    "ellipsis": fix_ellipsis,
    "dashes": fix_dashes,
    "halfwidth": fix_halfwidth,
    "duplicate_punct": fix_duplicate_punct,
    "spaces": fix_spaces,
    "whitespace": fix_whitespace,
}
# Order matters: 。。。 must become …… before duplicate punctuation collapses it.
LINE_FIX_ORDER = ("quotes", "ellipsis", "dashes", "halfwidth", "duplicate_punct", "spaces", "whitespace")


def fix_blank_lines(content: str) -> tuple[str, int]:
    text = content.replace("\r\n", "\n").replace("\r", "\n")
    count = 0

    def replace(match: re.Match[str]) -> str:
        nonlocal count
        count += 1
        return "\n\n"

    text = re.sub(r"\n[ \t\u3000]*\n(?:[ \t\u3000]*\n)+", replace, text)
    trimmed = text.strip("\n")
    if trimmed != text:
        count += 1
    return trimmed, count


def diff_excerpt(before: str, after: str, radius: int = 12) -> tuple[str, str]:
    """Short before/after snippets centred on the changed span."""
    if before == after:
        return before.strip()[:60], after.strip()[:60]
    prefix = 0
    while prefix < min(len(before), len(after)) and before[prefix] == after[prefix]:
        prefix += 1
    suffix = 0
    while (
        suffix < min(len(before), len(after)) - prefix
        and before[len(before) - 1 - suffix] == after[len(after) - 1 - suffix]
    ):
        suffix += 1
    left = max(0, prefix - radius)

    def cut(text: str) -> str:
        right = min(len(text), len(text) - suffix + radius)
        snippet = text[left:right]
        return ("…" if left else "") + snippet + ("…" if right < len(text) else "")

    return cut(before), cut(after)


def apply_punctuation(content: str, fixes: Iterable[str], style: str) -> tuple[str, Counter[str], list[dict[str, Any]]]:
    """Run the selected punctuation fixes; return text, counts and change records."""
    selected = set(fixes)
    counts: Counter[str] = Counter()
    records: list[dict[str, Any]] = []
    text = str(content or "").replace("\r\n", "\n").replace("\r", "\n")
    lines = text.split("\n")
    paragraphs = _paragraph_index(lines)
    for index, line in enumerate(lines):
        for key in LINE_FIX_ORDER:
            if key not in selected:
                continue
            previous = line
            if key == "quotes":
                line, count = fix_quotes(line, style)
            else:
                line, count = LINE_FIXERS[key](line)
            if count:
                counts[key] += count
                before, after = diff_excerpt(previous, line)
                records.append({
                    "code": key,
                    "paragraph": paragraphs[index],
                    "count": count,
                    "before": before,
                    "after": after,
                })
        lines[index] = line
    text = "\n".join(lines)
    if "blank_lines" in selected:
        text, count = fix_blank_lines(text)
        if count:
            counts["blank_lines"] += count
            records.append({"code": "blank_lines", "paragraph": 0, "count": count, "before": "", "after": ""})
    return text, counts, records


def unbalanced_quotes(content: str, style: str) -> list[dict[str, Any]]:
    open_d, close_d, open_s, close_s = _quote_marks(style)
    lines = [line for line in str(content or "").split("\n") if line.strip()]
    issues = []
    for index, raw in enumerate(lines):
        line, _ = fix_quotes(raw, style)
        opens, closes = line.count(open_d), line.count(close_d)
        single_opens, single_closes = line.count(open_s), line.count(close_s)
        if opens == closes and single_opens == single_closes:
            continue
        if opens == closes + 1 and single_opens == single_closes and index + 1 < len(lines):
            following, _ = fix_quotes(lines[index + 1].lstrip(" \u3000"), style)
            if following.startswith(open_d):
                continue  # multi-paragraph speech: each paragraph re-opens the quote
        issues.append({
            "code": "unbalanced_quote",
            "paragraph": index,
            "excerpt": line.strip()[:80],
            "message": f"本段前引号 {opens + single_opens} 个、后引号 {closes + single_closes} 个",
        })
    return issues


# -------------------------------------------------------------- repetition

def known_names(project: dict[str, Any]) -> list[dict[str, Any]]:
    result = []
    for character in project.get("characters", []) or []:
        if not isinstance(character, dict):
            continue
        name = str(character.get("name", "")).strip()
        aliases = character.get("aliases", [])
        if isinstance(aliases, str):
            aliases = re.split(r"[,，、/\s]+", aliases)
        forms = [name, *[str(alias).strip() for alias in aliases or []]]
        forms = [form for form in dict.fromkeys(forms) if form and _has_cjk(form)]
        if forms:
            result.append({"name": name or forms[0], "forms": forms})
    return result


def _mask_names(text: str, forms: Iterable[str]) -> str:
    for form in sorted(set(forms), key=len, reverse=True):
        if len(form) >= 2:
            text = text.replace(form, "\x00" * len(form))
    return text


def _cjk_ngrams(text: str, size: int) -> dict[str, list[int]]:
    positions: dict[str, list[int]] = defaultdict(list)
    for run in re.finditer(f"[{CJK}]+", text):
        value, base = run.group(), run.start()
        for offset in range(len(value) - size + 1):
            positions[value[offset:offset + size]].append(base + offset)
    return positions


def _distinctive(gram: str) -> bool:
    if gram in STOPWORDS or len(set(gram)) == 1:
        return False
    if gram[0] in EDGE_CHARS or gram[-1] in EDGE_CHARS:
        return False
    # Two common characters are usually a word boundary ("走到", "门口"); a
    # two-character hit needs at least one rarer character to be distinctive.
    return len(gram) >= 3 or any(char not in VERY_COMMON_CHARS for char in gram)


def window_repeats(content: str, name_forms: Iterable[str], window: int = 200, minimum: int = 3) -> list[dict[str, Any]]:
    """Distinctive 2–4 character words used ``minimum`` times within ``window`` chars.

    Longer grams are tried first; a shorter or overlapping gram whose hits sit
    on characters already covered by a reported repeat is folded into it, so a
    repeated passage is reported once rather than once per n-gram.
    """
    content = str(content or "")
    masked = _mask_names(content, name_forms)
    locate = _offset_to_paragraph(content)
    flagged: list[dict[str, Any]] = []
    covered: set[int] = set()
    for size in (4, 3, 2):
        for gram, positions in _cjk_ngrams(masked, size).items():
            if len(positions) < minimum or not _distinctive(gram):
                continue
            spaced: list[int] = []
            for position in positions:
                if not spaced or position >= spaced[-1] + size:
                    spaced.append(position)
            hit = next(
                (index for index in range(len(spaced) - minimum + 1)
                 if spaced[index + minimum - 1] - spaced[index] <= window),
                None,
            )
            if hit is None:
                continue
            hits = [position for position in spaced if spaced[hit] <= position < spaced[hit] + window]
            spans = {position + offset for position in hits for offset in range(size)}
            overlapping = sum(
                1 for position in hits if any(position + offset in covered for offset in range(size))
            )
            covered |= spans
            if overlapping * 2 >= len(hits):
                continue
            start, end = hits[0], hits[-1] + size
            flagged.append({
                "code": "window_repeat",
                "paragraph": locate(start),
                "phrase": gram,
                "excerpt": _excerpt(content, start, min(end, start + 70), 6)[:110],
                "message": f"“{gram}”在约 {end - start} 字内出现 {len(hits)} 次",
            })
    flagged.sort(key=lambda item: item["paragraph"])
    return flagged


_LEADING_MARKS = re.compile(r"^[\s\u3000“”‘’「」『』\"'（(《【]+")


def same_openings(content: str) -> list[dict[str, Any]]:
    paragraphs = [line.strip() for line in str(content or "").split("\n") if line.strip()]
    issues = []

    def opening(text: str) -> str:
        match = re.match(f"[{CJK}]+", _LEADING_MARKS.sub("", text))
        return match.group() if match else ""

    heads = [opening(text) for text in paragraphs]
    for index in range(1, len(paragraphs)):
        previous, current = heads[index - 1], heads[index]
        common = 0
        while common < min(len(previous), len(current)) and previous[common] == current[common]:
            common += 1
        if common >= 3:
            issues.append({
                "code": "same_opening",
                "paragraph": index,
                "phrase": current[:common],
                "excerpt": paragraphs[index][:60],
                "message": f"与上一段同样以“{current[:common]}”开头",
            })
    # Three or more consecutive paragraphs that open with the same two characters.
    run_start = 0
    for index in range(1, len(paragraphs) + 1):
        lead = heads[run_start][:2]
        if index < len(paragraphs) and len(lead) == 2 and heads[index][:2] == lead:
            continue
        if index - run_start >= 3:
            covered = any(
                run_start < item["paragraph"] < index and item["phrase"].startswith(lead) for item in issues
            )
            if not covered:
                issues.append({
                    "code": "same_opening",
                    "paragraph": run_start,
                    "phrase": lead,
                    "excerpt": paragraphs[run_start][:60],
                    "message": f"连续 {index - run_start} 段都以“{lead}”开头",
                })
        run_start = index
    issues.sort(key=lambda item: item["paragraph"])
    return issues


def overused_phrases(project: dict[str, Any], chapters: list[tuple[int, dict[str, Any]]]) -> list[dict[str, Any]]:
    rows = [chapter for _, chapter in chapters if str(chapter.get("content", "")).strip()]
    total_chars = sum(len(re.findall(f"[{CJK}]", str(chapter.get("content", "")))) for chapter in rows)
    if not rows or not total_chars:
        return []
    banned = banned_phrases(project)
    author = {item.phrase for item in banned if item.source == "author"}
    extra = [item.phrase for item in banned] + list(AI_CLICHES)
    candidates: dict[str, int] = {}
    # Known tics (author bans, generic clichés, this book's learned phrases) are
    # counted directly; repeated whole clauses come from the fatigue detector.
    for phrase in dict.fromkeys(extra):
        count = sum(str(chapter.get("content", "")).count(phrase) for chapter in rows)
        if count:
            candidates[phrase] = count
    for item in _fatigued_phrases(rows, limit=40, extra_phrases=extra):
        if not any(known in item["phrase"] or item["phrase"] in known for known in candidates):
            candidates[item["phrase"]] = item["count"]
    issues = []
    for phrase, count in candidates.items():
        rate = count * 10000 / total_chars
        if phrase not in author and (count < 3 or rate < 0.3):
            continue
        locations = []
        for number, chapter in chapters:
            content = str(chapter.get("content", ""))
            hits = content.count(phrase)
            if hits:
                position = content.find(phrase)
                locations.append({
                    "chapter_id": str(chapter.get("id", "")),
                    "chapter_number": number,
                    "chapter_title": str(chapter.get("title", "")),
                    "paragraph": _offset_to_paragraph(content)(position),
                    "count": hits,
                    "excerpt": _excerpt(content, position, position + len(phrase)),
                })
        issues.append({
            "code": "overused_phrase",
            "phrase": phrase,
            "count": count,
            "per_10k": round(rate, 2),
            "source": "author" if phrase in author else "book",
            "message": f"“{phrase}”全书出现 {count} 次（每万字 {rate:.2f} 次）" + ("，属于作者禁用词" if phrase in author else ""),
            "locations": locations,
        })
    issues.sort(key=lambda item: (-item["per_10k"], item["phrase"]))
    return issues


# ------------------------------------------------------------------- names

def _pinyin(text: str) -> list[str] | None:
    if _lazy_pinyin is None:
        return None
    try:
        return list(_lazy_pinyin(text))
    except Exception:  # pragma: no cover
        return None


def name_variants(project: dict[str, Any], chapters: list[tuple[int, dict[str, Any]]]) -> list[dict[str, Any]]:
    people = known_names(project)
    all_forms = {form for person in people for form in person["forms"]}
    if not all_forms:
        return []
    texts = [(number, chapter, str(chapter.get("content", ""))) for number, chapter in chapters]
    joined = "\n".join(text for _, _, text in texts)
    masked_texts = [(number, chapter, text, _mask_names(text, all_forms)) for number, chapter, text in texts]
    index: dict[tuple[int, int, str], list[tuple[str, str]]] = defaultdict(list)
    for person in people:
        for form in person["forms"]:
            if 2 <= len(form) <= 3 and joined.count(form) >= 2:
                for position in range(len(form)):
                    key = (len(form), position, form[:position] + form[position + 1:])
                    index[key].append((form, person["name"]))
    if not index:
        return []
    totals: Counter[str] = Counter()
    for _, _, _, masked in masked_texts:
        for run in re.finditer(f"[{CJK}]+", masked):
            value = run.group()
            for size in (2, 3):
                totals.update(value[offset:offset + size] for offset in range(len(value) - size + 1))
    form_counts = {form: joined.count(form) for form in all_forms}
    variants: dict[str, dict[str, Any]] = {}
    for gram, count in totals.items():
        matches = [
            (position, match)
            for position in range(len(gram))
            for match in index.get((len(gram), position, gram[:position] + gram[position + 1:]), ())
        ]
        if not matches or gram in all_forms or gram in STOPWORDS:
            continue
        if any(gram in form for form in all_forms if len(form) > len(gram)):
            continue
        for position, (form, person_name) in matches:
            swapped = gram[position]
            form_count = form_counts[form]
            if count >= form_count:
                continue
            homophone = None
            left, right = _pinyin(gram), _pinyin(form)
            if left is not None and right is not None:
                homophone = left == right
            if len(gram) == 2 and homophone is False:
                continue
            if swapped in COMMON_CHARS and not homophone:
                continue
            # Two-character forms whose first character differs (老篙→竹篙,
            # 沈二→斤二) are almost always ordinary words, not typos; a real
            # slip keeps the surname/prefix and changes the given-name char.
            if len(gram) == 2 and position == 0 and not homophone:
                continue
            existing = variants.get(gram)
            if existing and existing["name_count"] >= form_count:
                continue
            variants[gram] = {
                "code": "name_variant",
                "variant": gram,
                "name": form,
                "character": person_name,
                "count": count,
                "name_count": form_count,
                "homophone": homophone,
                "message": f"“{gram}”疑似“{form}”的错写（{gram} {count} 次 / {form} {form_count} 次）",
            }
    results = []
    for gram, item in variants.items():
        locations = []
        for number, chapter, text, masked in masked_texts:
            hits = [match.start() for match in re.finditer(re.escape(gram), masked)]
            if hits:
                locate = _offset_to_paragraph(text)
                locations.append({
                    "chapter_id": str(chapter.get("id", "")),
                    "chapter_number": number,
                    "chapter_title": str(chapter.get("title", "")),
                    "paragraph": locate(hits[0]),
                    "count": len(hits),
                    "excerpt": _excerpt(text, hits[0], hits[0] + len(gram)),
                })
        item["locations"] = locations
        results.append(item)
    results.sort(key=lambda item: (-item["name_count"], item["variant"]))
    return results


def normalize_mappings(mappings: Iterable[Any], project: dict[str, Any] | None = None) -> list[tuple[str, str]]:
    clean: dict[str, str] = {}
    for item in mappings or []:
        if isinstance(item, dict):
            source, target = str(item.get("from", "")).strip(), str(item.get("to", "")).strip()
        elif isinstance(item, (list, tuple)) and len(item) == 2:
            source, target = str(item[0]).strip(), str(item[1]).strip()
        else:
            continue
        if len(source) < 2 or not target or source == target:
            raise ValueError(f"人名映射无效：{source or '空'} → {target or '空'}")
        clean[source] = target
    return sorted(clean.items(), key=lambda pair: len(pair[0]), reverse=True)


def apply_name_mappings(content: str, mappings: list[tuple[str, str]], protected: Iterable[str] = ()) -> tuple[str, Counter[str]]:
    """Replace confirmed variants, never inside a longer known name."""
    counts: Counter[str] = Counter()
    if not mappings:
        return content, counts
    guards = sorted({form for form in protected if form}, key=len, reverse=True)
    placeholders: list[str] = []

    def protect(match: re.Match[str]) -> str:
        placeholders.append(match.group())
        return f"\x01{len(placeholders) - 1}\x02"

    text = content
    for source, _ in mappings:
        longer = [form for form in guards if source in form and form != source]
        if longer:
            text = re.sub("|".join(map(re.escape, longer)), protect, text)
    for source, target in mappings:
        occurrences = text.count(source)
        if occurrences:
            counts[f"{source}→{target}"] += occurrences
            text = text.replace(source, target)
    return re.sub(r"\x01(\d+)\x02", lambda match: placeholders[int(match.group(1))], text), counts


# -------------------------------------------------------------- formatting

def formatting_issues(project: dict[str, Any], chapters: list[tuple[int, dict[str, Any]]]) -> tuple[list[dict[str, Any]], str]:
    issues: list[dict[str, Any]] = []
    styles = Counter()
    numbered: list[tuple[int, dict[str, Any], str, int | None]] = []
    for number, chapter in _chapter_number_map(project):
        style, value = title_number(str(chapter.get("title", "")))
        if style:
            styles[style] += 1
        numbered.append((number, chapter, style, value))
    dominant = "cn" if styles["cn"] >= styles["arabic"] else "arabic"
    selected_ids = {id(chapter) for _, chapter in chapters}
    if styles["cn"] and styles["arabic"]:
        for number, chapter, style, _ in numbered:
            if style and style != dominant and id(chapter) in selected_ids:
                issues.append(_chapter_issue("title_numbering", number, chapter,
                    f"标题编号写法与全书主流（{'第一章' if dominant == 'cn' else '第1章'}）不一致", fixable=True,
                    after=renumber_title(str(chapter.get("title", "")), dominant)))
    previous = None
    for number, chapter, style, value in numbered:
        if value is None:
            continue
        if previous is not None and value != previous + 1 and id(chapter) in selected_ids:
            issues.append(_chapter_issue("numbering_gap", number, chapter, f"标题编号 {value} 紧跟在编号 {previous} 之后"))
        previous = value
    title_seen: dict[str, int] = {}
    subtitle_seen: dict[str, int] = {}
    for number, chapter in _chapter_number_map(project):
        title = re.sub(r"\s+", "", str(chapter.get("title", "")))
        subtitle = re.sub(r"\s+", "", TITLE_CN.sub("", TITLE_ARABIC.sub("", str(chapter.get("title", "")))))
        subtitle = re.sub(r"^[:：、.．\-—]+", "", subtitle)
        duplicate_of = title_seen.get(title) if title else None
        if duplicate_of is None and subtitle and len(subtitle) >= 2:
            duplicate_of = subtitle_seen.get(subtitle)
        if duplicate_of is not None and id(chapter) in selected_ids:
            issues.append(_chapter_issue("duplicate_title", number, chapter, f"与第 {duplicate_of} 章标题重复"))
        if title:
            title_seen.setdefault(title, number)
        if subtitle and len(subtitle) >= 2:
            subtitle_seen.setdefault(subtitle, number)
    for number, chapter in chapters:
        if not str(chapter.get("content", "")).strip():
            issues.append(_chapter_issue("empty_chapter", number, chapter, "本章没有正文，导出时会被跳过"))
    return issues, dominant


def renumber_title(title: str, style: str) -> str:
    current, value = title_number(title)
    if not current or current == style or value is None:
        return title
    pattern = TITLE_ARABIC if current == "arabic" else TITLE_CN
    rendered = chinese_number(value) if style == "cn" else str(value)
    return pattern.sub(lambda match: f"{match.group(1)}{rendered}{match.group(3)}", title, count=1)


def _chapter_issue(code: str, number: int, chapter: dict[str, Any], message: str, *, fixable: bool = False, after: str = "") -> dict[str, Any]:
    issue = {
        "code": code,
        "chapter_id": str(chapter.get("id", "")),
        "chapter_number": number,
        "chapter_title": str(chapter.get("title", "")),
        "paragraph": None,
        "excerpt": str(chapter.get("title", "")),
        "message": message,
        "fixable": fixable,
    }
    if after:
        issue["after"] = after
    return issue


# -------------------------------------------------------------------- scan

def _with_chapter(issue: dict[str, Any], number: int, chapter: dict[str, Any]) -> dict[str, Any]:
    return {
        "chapter_id": str(chapter.get("id", "")),
        "chapter_number": number,
        "chapter_title": str(chapter.get("title", "")),
        "locked": is_locked(chapter),
        **issue,
    }


def scan(project: dict[str, Any], options: dict[str, Any] | None = None) -> dict[str, Any]:
    options = options or {}
    chapters = select_chapters(project, options)
    style = dominant_quote_style(project)
    people = known_names(project)
    name_forms = [form for person in people for form in person["forms"]]
    groups: dict[str, list[dict[str, Any]]] = {key: [] for key in CATEGORY_LABELS}
    code_counts: dict[str, Counter[str]] = {key: Counter() for key in CATEGORY_LABELS}
    fix_counts: Counter[str] = Counter()
    fix_chapters: dict[str, set[str]] = defaultdict(set)

    def add(category: str, issue: dict[str, Any], weight: int = 1) -> None:
        code_counts[category][issue["code"]] += weight
        if len(groups[category]) < MAX_ISSUES_PER_GROUP:
            groups[category].append(issue)

    for number, chapter in chapters:
        content = str(chapter.get("content", ""))
        if not content.strip():
            continue
        _, counts, records = apply_punctuation(content, PUNCTUATION_FIXES, style)
        for key, count in counts.items():
            fix_counts[key] += count
            fix_chapters[key].add(str(chapter.get("id", "")))
        for record in records:
            add("punctuation", _with_chapter({
                "code": record["code"],
                "paragraph": record["paragraph"],
                "excerpt": record["before"],
                "after": record["after"],
                "message": f"{FIX_LABELS[record['code']]}：{record['count']} 处",
                "fixable": True,
            }, number, chapter), record["count"])
        for issue in unbalanced_quotes(content, style):
            add("punctuation", _with_chapter({**issue, "fixable": False}, number, chapter))
        for issue in window_repeats(content, name_forms)[:30]:
            add("repetition", _with_chapter({**issue, "fixable": False}, number, chapter))
        for issue in same_openings(content)[:30]:
            add("repetition", _with_chapter({**issue, "fixable": False}, number, chapter))

    overused = overused_phrases(project, chapters)
    for issue in overused:
        add("repetition", {**issue, "fixable": False})
    variants = name_variants(project, chapters)
    for issue in variants:
        add("names", {**issue, "fixable": False, "needs_mapping": True}, issue["count"])
    format_issues, number_style = formatting_issues(project, chapters)
    for issue in format_issues:
        add("formatting", {**issue, "locked": any(
            str(chapter.get("id", "")) == issue["chapter_id"] and is_locked(chapter) for _, chapter in chapters)})
        if issue["code"] == "title_numbering":
            fix_counts["title_numbering"] += 1
            fix_chapters["title_numbering"].add(issue["chapter_id"])

    summary = {}
    for category, label in CATEGORY_LABELS.items():
        codes = code_counts[category]
        summary[category] = {
            "label": label,
            "count": sum(codes.values()),
            "codes": {code: {"label": CODE_LABELS.get(code, code), "count": count} for code, count in codes.items()},
            "truncated": len(groups[category]) >= MAX_ISSUES_PER_GROUP,
        }
    locked = [
        {"chapter_id": str(chapter.get("id", "")), "chapter_number": number, "chapter_title": str(chapter.get("title", ""))}
        for number, chapter in chapters if is_locked(chapter)
    ]
    settled = sum(1 for _, chapter in chapters if chapter.get("settlement") and not is_locked(chapter))
    return {
        "chapters_scanned": len(chapters),
        "quote_style": style,
        "number_style": number_style,
        "pinyin_available": _lazy_pinyin is not None,
        "summary": summary,
        "issues": groups,
        "fixes": [
            {"key": key, "label": FIX_LABELS[key], "count": fix_counts[key], "chapters": len(fix_chapters[key])}
            for key in FIX_KEYS
        ],
        "name_variants": [
            {key: item[key] for key in ("variant", "name", "character", "count", "name_count", "homophone")}
            for item in variants
        ],
        "locked_chapters": locked,
        "settled_chapters": settled,
        "total_issues": sum(item["count"] for item in summary.values()),
    }


# ------------------------------------------------------------------- apply

def apply(
    project: dict[str, Any],
    fixes: Iterable[str],
    *,
    name_mappings: Iterable[Any] = (),
    options: dict[str, Any] | None = None,
    include_locked: bool = False,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Return (updated project copy, diff summary).  The input is not modified."""
    selected = [key for key in dict.fromkeys(str(item) for item in fixes or []) if key in FIX_KEYS]
    unknown = [str(item) for item in fixes or [] if str(item) not in FIX_KEYS]
    if unknown:
        raise ValueError(f"未知的修复类型：{'、'.join(unknown)}")
    mappings = normalize_mappings(name_mappings, project)
    if not selected and not mappings:
        raise ValueError("请至少选择一类修复或确认一条人名映射")
    updated = deepcopy(project)
    style = dominant_quote_style(project)
    _, number_style = formatting_issues(project, [])
    protected_names = [form for person in known_names(project) for form in person["forms"]]
    punctuation = [key for key in selected if key in PUNCTUATION_FIXES]
    totals: Counter[str] = Counter()
    changed: list[dict[str, Any]] = []
    skipped_locked: list[dict[str, Any]] = []
    for number, chapter in select_chapters(updated, options):
        ref = {"chapter_id": str(chapter.get("id", "")), "chapter_number": number, "chapter_title": str(chapter.get("title", ""))}
        if is_locked(chapter) and not include_locked:
            skipped_locked.append(ref)
            continue
        before = str(chapter.get("content", ""))
        content = before
        counts: Counter[str] = Counter()
        samples: list[dict[str, str]] = []
        if punctuation and content.strip():
            content, punct_counts, records = apply_punctuation(content, punctuation, style)
            counts.update(punct_counts)
            samples.extend({"before": record["before"], "after": record["after"]} for record in records if record["before"])
        if mappings:
            content, name_counts = apply_name_mappings(content, mappings, protected_names)
            counts.update(name_counts)
        title_before = str(chapter.get("title", ""))
        title_after = title_before
        if "title_numbering" in selected:
            title_after = renumber_title(title_before, number_style)
            if title_after != title_before:
                counts["title_numbering"] += 1
        if mappings:
            title_after, title_counts = apply_name_mappings(title_after, mappings, protected_names)
            counts.update(title_counts)
        chapter["title"] = title_after
        if content == before and title_after == title_before:
            continue
        chapter["content"] = content
        totals.update(counts)
        changed.append({
            **ref,
            "title_after": title_after,
            "chars_before": len(before),
            "chars_after": len(content),
            "counts": dict(counts),
            "samples": samples[:3],
            "was_locked": is_locked(chapter),
        })
    summary = {
        "fixes": selected,
        "name_mappings": [{"from": source, "to": target} for source, target in mappings],
        "changed_chapters": changed,
        "changed_count": len(changed),
        "totals": dict(totals),
        "skipped_locked": skipped_locked,
        "version_reason": VERSION_REASON,
    }
    return updated, summary
