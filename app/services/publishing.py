"""Web-novel export (TXT / EPUB / DOCX) and serial update-cadence tracking.

Exports are built with the standard library only so the packaged app stays
offline.  The cadence manager stores its schedule and per-chapter publish
status in the extension store, never in the manuscript JSON.
"""

from __future__ import annotations

import html
import io
import re
import uuid
import zipfile
from datetime import date, datetime, timedelta, timezone
from math import ceil
from typing import Any

SCHEDULE_KEY = "publishing"
PUBLISH_STATUSES = ("draft", "ready", "published")
_CN_DIGITS = "零一二三四五六七八九"
# XML 1.0 forbids most C0 controls, lone surrogates and U+FFFE/U+FFFF; a single
# one (common in text pasted from web pages) makes EPUB/DOCX unreadable.
_XML_ILLEGAL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\ud800-\udfff\ufffe\uffff]")


def xml_safe(value: Any) -> str:
    """Strip XML-illegal characters, then escape for element/attribute text."""
    return html.escape(_XML_ILLEGAL.sub("", str(value or "")))


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def chinese_number(value: int) -> str:
    """1 → 一, 12 → 十二, 105 → 一百零五 (up to 9999)."""
    if value <= 0:
        return str(value)
    if value >= 10000:
        return str(value)
    units = [(1000, "千"), (100, "百"), (10, "十")]
    result = ""
    remainder = value
    zero_pending = False
    for size, unit in units:
        digit, remainder = divmod(remainder, size)
        if digit:
            if zero_pending:
                result += "零"
                zero_pending = False
            result += ("" if (size == 10 and digit == 1 and not result) else _CN_DIGITS[digit]) + unit
        elif result:
            zero_pending = True
    if remainder:
        if zero_pending:
            result += "零"
        result += _CN_DIGITS[remainder]
    return result


_MARKDOWN_PATTERNS = (
    (re.compile(r"^\s{0,3}#{1,6}\s*", re.M), ""),
    (re.compile(r"\*\*(.+?)\*\*"), r"\1"),
    (re.compile(r"__(.+?)__"), r"\1"),
    (re.compile(r"(?<!\*)\*(?!\*)([^*\n]+)\*(?!\*)"), r"\1"),
    (re.compile(r"^\s*>\s?", re.M), ""),
    (re.compile(r"^\s*[-*+]\s+", re.M), ""),
    (re.compile(r"^\s*-{3,}\s*$", re.M), ""),
    (re.compile(r"`([^`]*)`"), r"\1"),
)
_HEADING_PREFIX = re.compile(
    r"^\s*(第\s*[0-9０-９零〇一二两三四五六七八九十百千万壹贰叁肆伍陆柒捌玖拾佰仟]+\s*[章回节话]|chapter\s+\d+)[\s:：、.．\-—]*",
    re.I,
)


def strip_markdown(text: str) -> str:
    for pattern, replacement in _MARKDOWN_PATTERNS:
        text = pattern.sub(replacement, text)
    return text


def chapter_paragraphs(content: str) -> list[str]:
    text = strip_markdown(_XML_ILLEGAL.sub("", str(content or "").replace("\r\n", "\n")))
    return [line.replace("　", " ").strip() for line in text.split("\n") if line.strip()]


def count_words(text: str) -> int:
    """Platform-style count: CJK characters + Latin words (punctuation excluded)."""
    cjk = len(re.findall(r"[一-鿿㐀-䶿]", text))
    latin = len(re.findall(r"[A-Za-z0-9]+", text))
    return cjk + latin


def chapter_heading(number: int, title: str, style: str = "cn") -> str:
    clean = _HEADING_PREFIX.sub("", str(title or "")).strip()
    prefix = f"第{chinese_number(number)}章" if style == "cn" else f"第{number}章"
    return f"{prefix} {clean}".strip()


def selected_chapters(project: dict[str, Any], options: dict[str, Any]) -> list[tuple[int, dict[str, Any]]]:
    chapters = list(enumerate(project.get("chapters", []), start=1))
    start = int(options.get("start", 1) or 1)
    end = int(options.get("end", 0) or 0) or len(chapters)
    chosen = [
        (number, chapter)
        for number, chapter in chapters
        if start <= number <= end and str(chapter.get("content", "")).strip()
    ]
    if options.get("only_locked"):
        chosen = [item for item in chosen if item[1].get("authority_state") == "locked"]
    return chosen


def export_txt(project: dict[str, Any], options: dict[str, Any] | None = None) -> dict[str, Any]:
    options = options or {}
    indent = "　　" if options.get("indent", True) else ""
    separator = "\n\n" if options.get("blank_line", True) else "\n"
    style = "arabic" if options.get("number_style") == "arabic" else "cn"
    parts: list[str] = []
    stats = []
    if options.get("include_title", True):
        parts.append(str(project.get("title", "")).strip() or "未命名作品")
        if str(project.get("premise", "")).strip() and options.get("include_intro", False):
            parts.append("简介：" + str(project["premise"]).strip())
    for number, chapter in selected_chapters(project, options):
        paragraphs = chapter_paragraphs(chapter.get("content", ""))
        heading = chapter_heading(number, chapter.get("title", ""), style)
        body = separator.join(indent + paragraph for paragraph in paragraphs)
        parts.append(f"{heading}{separator}{body}")
        stats.append({"number": number, "title": heading, "words": count_words("".join(paragraphs))})
    text = ("\n\n\n" if options.get("blank_line", True) else "\n\n").join(parts).strip() + "\n"
    text = _XML_ILLEGAL.sub("", text)
    return {
        "filename": f"{_safe_filename(project.get('title'))}.txt",
        "mime": "text/plain;charset=utf-8",
        "content": text,
        "encoding": "text",
        "chapters": stats,
        "total_words": sum(item["words"] for item in stats),
    }


def _safe_filename(value: Any) -> str:
    name = _XML_ILLEGAL.sub("", str(value or ""))
    name = re.sub(r'[\\/:*?"<>|\x00-\x1f\x7f]+', "_", name).strip(" .")
    return name[:80] or "inkforge"


def _xhtml(title: str, body: str) -> str:
    return (
        '<?xml version="1.0" encoding="utf-8"?>\n<!DOCTYPE html>\n'
        '<html xmlns="http://www.w3.org/1999/xhtml" xmlns:epub="http://www.idpf.org/2007/ops" lang="zh-CN" xml:lang="zh-CN">\n'
        f"<head><meta charset=\"utf-8\"/><title>{xml_safe(title)}</title>"
        '<link rel="stylesheet" type="text/css" href="style.css"/></head>\n'
        f"<body>{body}</body></html>\n"
    )


EPUB_CSS = """body{font-family:serif;line-height:1.8;margin:0 5%;}
h1{font-size:1.4em;text-align:center;margin:2em 0 1.5em;}
p{text-indent:2em;margin:0 0 .6em;}
.cover h1{margin-top:30%;font-size:2em;}
.cover p{text-indent:0;text-align:center;color:#555;}"""


def export_epub(project: dict[str, Any], options: dict[str, Any] | None = None) -> dict[str, Any]:
    options = options or {}
    style = "arabic" if options.get("number_style") == "arabic" else "cn"
    title = str(project.get("title", "")).strip() or "未命名作品"
    author = str(options.get("author", "")).strip() or "佚名"
    book_id = f"urn:uuid:{project.get('id') or uuid.uuid4()}"
    modified = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    chapters = selected_chapters(project, options)
    if not chapters:
        raise ValueError("没有可导出的正文章节")
    buffer = io.BytesIO()
    stats = []
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr(zipfile.ZipInfo("mimetype"), "application/epub+zip", compress_type=zipfile.ZIP_STORED)

        def write(name: str, data: str) -> None:
            archive.writestr(name, data, compress_type=zipfile.ZIP_DEFLATED)

        write(
            "META-INF/container.xml",
            '<?xml version="1.0" encoding="utf-8"?>\n<container version="1.0" xmlns="urn:oasis:names:tc:opendocument:xmlns:container">'
            '<rootfiles><rootfile full-path="OEBPS/content.opf" media-type="application/oebps-package+xml"/></rootfiles></container>',
        )
        write("OEBPS/style.css", EPUB_CSS)
        intro = str(project.get("premise", "")).strip()
        write(
            "OEBPS/cover.xhtml",
            _xhtml(title, f'<section class="cover"><h1>{xml_safe(title)}</h1><p>{xml_safe(author)}</p>'
                   + (f"<p>{xml_safe(intro)}</p>" if intro else "") + "</section>"),
        )
        manifest = ['<item id="css" href="style.css" media-type="text/css"/>',
                    '<item id="nav" href="nav.xhtml" media-type="application/xhtml+xml" properties="nav"/>',
                    '<item id="cover" href="cover.xhtml" media-type="application/xhtml+xml"/>']
        spine = ['<itemref idref="cover"/>']
        nav_items = []
        for position, (number, chapter) in enumerate(chapters, start=1):
            heading = chapter_heading(number, chapter.get("title", ""), style)
            paragraphs = chapter_paragraphs(chapter.get("content", ""))
            body = f"<h1>{xml_safe(heading)}</h1>" + "".join(
                f"<p>{xml_safe(paragraph)}</p>" for paragraph in paragraphs
            )
            name = f"chapter-{position:04d}.xhtml"
            write(f"OEBPS/{name}", _xhtml(heading, body))
            manifest.append(f'<item id="c{position}" href="{name}" media-type="application/xhtml+xml"/>')
            spine.append(f'<itemref idref="c{position}"/>')
            nav_items.append(f'<li><a href="{name}">{xml_safe(heading)}</a></li>')
            stats.append({"number": number, "title": heading, "words": count_words("".join(paragraphs))})
        write(
            "OEBPS/nav.xhtml",
            _xhtml("目录", f'<nav epub:type="toc" id="toc"><h1>目录</h1><ol>{"".join(nav_items)}</ol></nav>'),
        )
        write(
            "OEBPS/content.opf",
            '<?xml version="1.0" encoding="utf-8"?>\n'
            '<package xmlns="http://www.idpf.org/2007/opf" version="3.0" unique-identifier="bookid" xml:lang="zh-CN">'
            '<metadata xmlns:dc="http://purl.org/dc/elements/1.1/">'
            f'<dc:identifier id="bookid">{xml_safe(book_id)}</dc:identifier>'
            f"<dc:title>{xml_safe(title)}</dc:title><dc:creator>{xml_safe(author)}</dc:creator>"
            f'<dc:language>zh-CN</dc:language><meta property="dcterms:modified">{modified}</meta></metadata>'
            f"<manifest>{''.join(manifest)}</manifest><spine>{''.join(spine)}</spine></package>",
        )
    return {
        "filename": f"{_safe_filename(title)}.epub",
        "mime": "application/epub+zip",
        "content": buffer.getvalue(),
        "encoding": "binary",
        "chapters": stats,
        "total_words": sum(item["words"] for item in stats),
    }


def export_docx(project: dict[str, Any], options: dict[str, Any] | None = None) -> dict[str, Any]:
    options = options or {}
    style = "arabic" if options.get("number_style") == "arabic" else "cn"
    title = str(project.get("title", "")).strip() or "未命名作品"
    chapters = selected_chapters(project, options)
    if not chapters:
        raise ValueError("没有可导出的正文章节")

    def run(text: str) -> str:
        return f'<w:r><w:t xml:space="preserve">{xml_safe(text)}</w:t></w:r>'

    body = [f'<w:p><w:pPr><w:pStyle w:val="Title"/></w:pPr>{run(title)}</w:p>']
    stats = []
    for index, (number, chapter) in enumerate(chapters):
        heading = chapter_heading(number, chapter.get("title", ""), style)
        page_break = '<w:pageBreakBefore/>' if index else ""
        body.append(f'<w:p><w:pPr><w:pStyle w:val="Heading1"/>{page_break}</w:pPr>{run(heading)}</w:p>')
        paragraphs = chapter_paragraphs(chapter.get("content", ""))
        for paragraph in paragraphs:
            body.append(f'<w:p><w:pPr><w:ind w:firstLineChars="200"/></w:pPr>{run(paragraph)}</w:p>')
        stats.append({"number": number, "title": heading, "words": count_words("".join(paragraphs))})
    ns = 'xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"'
    document = (
        f'<?xml version="1.0" encoding="UTF-8" standalone="yes"?><w:document {ns}><w:body>'
        + "".join(body)
        + '<w:sectPr><w:pgSz w:w="11906" w:h="16838"/><w:pgMar w:top="1440" w:right="1440" w:bottom="1440" w:left="1440"/></w:sectPr>'
        "</w:body></w:document>"
    )
    styles = (
        f'<?xml version="1.0" encoding="UTF-8" standalone="yes"?><w:styles {ns}>'
        '<w:docDefaults><w:rPrDefault><w:rPr><w:rFonts w:ascii="Times New Roman" w:eastAsia="宋体"/><w:sz w:val="24"/></w:rPr></w:rPrDefault>'
        '<w:pPrDefault><w:pPr><w:spacing w:after="120" w:line="360" w:lineRule="auto"/></w:pPr></w:pPrDefault></w:docDefaults>'
        '<w:style w:type="paragraph" w:styleId="Title"><w:name w:val="Title"/><w:pPr><w:jc w:val="center"/></w:pPr><w:rPr><w:b/><w:sz w:val="44"/></w:rPr></w:style>'
        '<w:style w:type="paragraph" w:styleId="Heading1"><w:name w:val="heading 1"/><w:pPr><w:jc w:val="center"/><w:spacing w:before="480" w:after="360"/><w:outlineLvl w:val="0"/></w:pPr><w:rPr><w:b/><w:sz w:val="32"/></w:rPr></w:style>'
        "</w:styles>"
    )
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(
            "[Content_Types].xml",
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?><Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
            '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
            '<Default Extension="xml" ContentType="application/xml"/>'
            '<Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>'
            '<Override PartName="/word/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.styles+xml"/></Types>',
        )
        archive.writestr(
            "_rels/.rels",
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?><Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="word/document.xml"/></Relationships>',
        )
        archive.writestr(
            "word/_rels/document.xml.rels",
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?><Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/styles" Target="styles.xml"/></Relationships>',
        )
        archive.writestr("word/document.xml", document)
        archive.writestr("word/styles.xml", styles)
    return {
        "filename": f"{_safe_filename(title)}.docx",
        "mime": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        "content": buffer.getvalue(),
        "encoding": "binary",
        "chapters": stats,
        "total_words": sum(item["words"] for item in stats),
    }


EXPORTERS = {"txt": export_txt, "epub": export_epub, "docx": export_docx}


# ---------------------------------------------------------------------------
# Update cadence
# ---------------------------------------------------------------------------


def default_schedule() -> dict[str, Any]:
    return {
        "chapters_per_day": 1,
        "target_words": 2000,
        "publish_time": "20:00",
        "rest_weekdays": [],
        "volume_end_chapter": 0,
        "start_date": "",
        "chapter_status": {},
        "updated_at": "",
    }


def normalize_schedule(raw: dict[str, Any] | None) -> dict[str, Any]:
    schedule = default_schedule()
    raw = raw if isinstance(raw, dict) else {}
    schedule.update({key: raw[key] for key in schedule if key in raw})
    try:
        schedule["chapters_per_day"] = max(0.1, min(20.0, float(schedule["chapters_per_day"] or 1)))
        schedule["target_words"] = max(200, min(30000, int(float(schedule["target_words"] or 2000))))
        schedule["volume_end_chapter"] = max(0, int(float(schedule["volume_end_chapter"] or 0)))
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError("更新计划中的数字格式不正确") from exc
    if not re.fullmatch(r"\d{1,2}:\d{2}", str(schedule["publish_time"] or "")):
        schedule["publish_time"] = "20:00"
    schedule["rest_weekdays"] = sorted({
        int(day) for day in schedule.get("rest_weekdays") or [] if str(day).isdigit() and 0 <= int(day) <= 6
    })
    statuses = schedule.get("chapter_status") if isinstance(schedule.get("chapter_status"), dict) else {}
    schedule["chapter_status"] = {
        str(key): {
            "status": value.get("status") if value.get("status") in PUBLISH_STATUSES else "draft",
            "published_at": str(value.get("published_at", ""))[:10],
        }
        for key, value in statuses.items()
        if isinstance(value, dict)
    }
    return schedule


def _parse_day(value: Any) -> date | None:
    try:
        return date.fromisoformat(str(value)[:10])
    except ValueError:
        return None


def _add_working_days(start: date, days_needed: int, rest: list[int]) -> date:
    current = start
    remaining = days_needed
    if len(rest) >= 7:
        return start
    while remaining > 0:
        current += timedelta(days=1)
        if current.weekday() not in rest:
            remaining -= 1
    return current


def cadence_report(project: dict[str, Any], schedule: dict[str, Any], today: date | None = None) -> dict[str, Any]:
    """Derive buffer, streak and projected completion (pure function)."""
    today = today or datetime.now().date()
    schedule = normalize_schedule(schedule)
    chapters = project.get("chapters", [])
    rows = []
    published_days: set[date] = set()
    buffer = 0
    published = 0
    for number, chapter in enumerate(chapters, start=1):
        chapter_id = str(chapter.get("id", ""))
        words = count_words(str(chapter.get("content", "")))
        state = schedule["chapter_status"].get(chapter_id, {})
        status = state.get("status") or "draft"
        if status == "draft" and words >= schedule["target_words"] * 0.6 and chapter.get("authority_state") == "locked":
            suggested = "ready"
        else:
            suggested = status
        if status == "published":
            published += 1
            day = _parse_day(state.get("published_at"))
            if day:
                published_days.add(day)
        elif status == "ready":
            buffer += 1
        rows.append({
            "chapter_id": chapter_id,
            "number": number,
            "title": str(chapter.get("title", ""))[:60],
            "words": words,
            "status": status,
            "suggested_status": suggested,
            "published_at": state.get("published_at", ""),
            "under_target": 0 < words < schedule["target_words"] * 0.8,
        })
    streak = 0
    cursor = today if today in published_days else today - timedelta(days=1)
    earliest = min(published_days) if published_days else today
    while cursor >= earliest:
        if cursor in published_days:
            streak += 1
        elif cursor.weekday() not in schedule["rest_weekdays"]:
            break
        cursor -= timedelta(days=1)
    per_day = schedule["chapters_per_day"]
    buffer_days = round(buffer / per_day, 1) if per_day else 0
    volume_end = schedule["volume_end_chapter"] or int(project.get("narrative", {}).get("target_chapters", 0) or 0)
    remaining = max(0, volume_end - published) if volume_end else 0
    projected = None
    if remaining:
        projected = _add_working_days(today, ceil(remaining / per_day), schedule["rest_weekdays"]).isoformat()
    unwritten = max(0, volume_end - len([row for row in rows if row["words"] > 0])) if volume_end else 0
    warnings = []
    if per_day and buffer < per_day * 3:
        warnings.append(f"存稿只够 {buffer_days} 天，建议至少保持 3 天存稿")
    if any(row["under_target"] and row["status"] != "published" for row in rows):
        short = [str(row["number"]) for row in rows if row["under_target"] and row["status"] != "published"]
        warnings.append(f"第 {'、'.join(short[:8])} 章字数低于目标的 80%")
    today_published = sum(1 for day in published_days if day == today)
    return {
        "schedule": schedule,
        "chapters": rows,
        "stats": {
            "published": published,
            "buffer": buffer,
            "buffer_days": buffer_days,
            "streak_days": streak,
            "today_published": today_published,
            "today_target": per_day,
            "volume_end_chapter": volume_end,
            "remaining_to_publish": remaining,
            "unwritten_chapters": unwritten,
            "projected_volume_end": projected,
            "next_publish": f"{today.isoformat()} {schedule['publish_time']}",
            "total_words": sum(row["words"] for row in rows),
        },
        "warnings": warnings,
    }


def set_chapter_status(schedule: dict[str, Any], chapter_id: str, status: str, published_at: str = "") -> dict[str, Any]:
    if status not in PUBLISH_STATUSES:
        raise ValueError("发布状态只能是 draft / ready / published")
    schedule = normalize_schedule(schedule)
    if status == "published" and not _parse_day(published_at):
        published_at = datetime.now().date().isoformat()
    schedule["chapter_status"][str(chapter_id)] = {
        "status": status,
        "published_at": published_at[:10] if status == "published" else "",
    }
    schedule["updated_at"] = utc_now()
    return schedule
