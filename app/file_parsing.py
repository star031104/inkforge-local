from __future__ import annotations

import base64
import html
import io
import re
import zipfile
from pathlib import Path
from typing import Any
from xml.etree import ElementTree


def _clean_html(text: str) -> str:
    text = re.sub(r"(?is)<(script|style).*?>.*?</\1>", " ", text)
    text = re.sub(r"(?i)<br\s*/?>", "\n", text)
    text = re.sub(r"(?i)</p\s*>", "\n\n", text)
    text = re.sub(r"(?s)<[^>]+>", " ", text)
    text = html.unescape(text)
    return re.sub(r"[ \t]+", " ", text).strip()


def _decode_text(data: bytes) -> str:
    for encoding in ("utf-8-sig", "utf-8", "gb18030", "big5"):
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", errors="replace")


def _docx_text(data: bytes) -> str:
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        xml_bytes = archive.read("word/document.xml")
    root = ElementTree.fromstring(xml_bytes)
    texts: list[str] = []
    for node in root.iter():
        tag = node.tag.rsplit("}", 1)[-1]
        if tag == "t" and node.text:
            texts.append(node.text)
        elif tag in {"p", "br"}:
            texts.append("\n")
    return re.sub(r"\n{3,}", "\n\n", "".join(texts)).strip()


def _epub_text(data: bytes) -> str:
    parts: list[str] = []
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        for name in sorted(archive.namelist()):
            suffix = Path(name).suffix.lower()
            if suffix not in {".xhtml", ".html", ".htm"}:
                continue
            try:
                parts.append(_clean_html(_decode_text(archive.read(name))))
            except Exception:
                continue
    return "\n\n".join(part for part in parts if part).strip()


def _pdf_text(data: bytes) -> str:
    try:
        from pypdf import PdfReader  # type: ignore
    except ImportError as exc:  # pragma: no cover - optional dependency until installed
        raise ValueError("解析 PDF 需要先安装 requirements.txt 中的 pypdf") from exc
    reader = PdfReader(io.BytesIO(data))
    return "\n\n".join((page.extract_text() or "").strip() for page in reader.pages if (page.extract_text() or "").strip())


def parse_reference_file(name: str, content_base64: str, *, max_bytes: int = 20 * 1024 * 1024) -> dict[str, Any]:
    try:
        data = base64.b64decode(content_base64, validate=True)
    except Exception as exc:
        raise ValueError("文件内容不是合法 base64") from exc
    if not data:
        raise ValueError("文件为空")
    if len(data) > max_bytes:
        raise ValueError("单个参考文件不能超过 20MB")
    suffix = Path(name or "reference.txt").suffix.lower()
    if suffix in {".txt", ".md", ".markdown", ".json", ".csv"}:
        text = _decode_text(data)
    elif suffix in {".html", ".htm"}:
        text = _clean_html(_decode_text(data))
    elif suffix == ".docx":
        text = _docx_text(data)
    elif suffix == ".epub":
        text = _epub_text(data)
    elif suffix == ".pdf":
        text = _pdf_text(data)
    else:
        # Many web-novel exports have non-standard extensions but are still text.
        text = _decode_text(data)
        replacement_ratio = text.count("�") / max(1, len(text))
        if replacement_ratio > 0.02:
            raise ValueError(f"暂不支持 {suffix or '该'} 文件格式")
    text = text.replace("\r\n", "\n").replace("\r", "\n").strip()
    if len(text) < 20:
        raise ValueError("没有解析出足够的正文文字")
    logical_format = (suffix or ".txt").lstrip(".").lower()
    if logical_format in {"txt", "md", "markdown", "json", "csv"}:
        logical_format = "text"
    elif logical_format in {"html", "htm"}:
        logical_format = "html"
    return {
        "name": name,
        "text": text,
        "chars": len(text),
        "suffix": suffix or ".txt",
        "format": logical_format,
    }

