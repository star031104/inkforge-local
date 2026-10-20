"""Plain manuscript export (TXT / EPUB / DOCX).

Exports are built with the standard library only so the packaged app stays
offline.  The GitHub backup reuses the same exporters.
"""

from __future__ import annotations

import html
import io
import re
import uuid
import zipfile
from datetime import datetime, timezone
from typing import Any

_CN_DIGITS = "零一二三四五六七八九"
# XML 1.0 forbids most C0 controls, lone surrogates and U+FFFE/U+FFFF; a single
# one (common in text pasted from web pages) makes EPUB/DOCX unreadable.
_XML_ILLEGAL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\ud800-\udfff\ufffe\uffff]")


def xml_safe(value: Any) -> str:
    """Strip XML-illegal characters, then escape for element/attribute text."""
    return html.escape(_XML_ILLEGAL.sub("", str(value or "")))


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
    """Word count: CJK characters + Latin words (punctuation excluded)."""
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
