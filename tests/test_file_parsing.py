import base64
import io
import zipfile

from app.file_parsing import parse_reference_file


def _b64(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")


def test_parse_utf8_text_reference():
    result = parse_reference_file("sample.txt", _b64("第一章\n测试文本。这是一段足够长度的参考内容，用于解析验证。".encode("utf-8")))
    assert result["text"].startswith("第一章")
    assert result["format"] == "text"


def test_parse_minimal_docx_reference():
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("word/document.xml", '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:body><w:p><w:r><w:t>角色资料。这是一段足够长度的原作角色参考资料。</w:t></w:r></w:p></w:body></w:document>')
    result = parse_reference_file("canon.docx", _b64(buf.getvalue()))
    assert "角色资料" in result["text"]
    assert result["format"] == "docx"


def test_parse_html_strips_script_and_keeps_paragraphs():
    html = b"<html><style>.x{color:red}</style><script>bad()</script><p>InkForge HTML reference paragraph one.</p><p>Paragraph two is long enough for parsing.</p></html>"
    result = parse_reference_file("reference.html", _b64(html))
    assert "bad()" not in result["text"]
    assert "paragraph one" in result["text"]
    assert result["format"] == "html"


def test_parse_minimal_epub_reference():
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("mimetype", "application/epub+zip")
        zf.writestr("OEBPS/chapter1.xhtml", "<html><body><p>EPUB reference chapter with enough content to be extracted correctly.</p></body></html>")
        zf.writestr("OEBPS/chapter2.xhtml", "<html><body><p>Second chapter paragraph for ordering and extraction.</p></body></html>")
    result = parse_reference_file("book.epub", _b64(buf.getvalue()))
    assert "EPUB reference chapter" in result["text"]
    assert "Second chapter" in result["text"]
    assert result["format"] == "epub"


def _minimal_pdf_bytes(text: str) -> bytes:
    # Tiny valid PDF with a single Helvetica text line; keeps the test self-contained.
    stream = f"BT /F1 12 Tf 72 720 Td ({text}) Tj ET".encode("latin-1")
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Resources << /Font << /F1 5 0 R >> >> /Contents 4 0 R >>",
        b"<< /Length " + str(len(stream)).encode() + b" >>\nstream\n" + stream + b"\nendstream",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    out = bytearray(b"%PDF-1.4\n")
    offsets = [0]
    for index, obj in enumerate(objects, start=1):
        offsets.append(len(out))
        out.extend(f"{index} 0 obj\n".encode()); out.extend(obj); out.extend(b"\nendobj\n")
    xref = len(out)
    out.extend(f"xref\n0 {len(objects)+1}\n".encode())
    out.extend(b"0000000000 65535 f \n")
    for offset in offsets[1:]: out.extend(f"{offset:010d} 00000 n \n".encode())
    out.extend(f"trailer\n<< /Size {len(objects)+1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode())
    return bytes(out)


def test_parse_pdf_reference():
    data = _minimal_pdf_bytes("InkForge PDF reference text is long enough for extraction testing.")
    result = parse_reference_file("reference.pdf", _b64(data))
    assert "InkForge PDF reference" in result["text"]
    assert result["format"] == "pdf"


def test_parse_gb18030_text_reference():
    text = "第一章\n这是GB18030编码的参考资料，用于验证手机导出的中文文本也能正确读取。"
    result = parse_reference_file("legacy.txt", _b64(text.encode("gb18030")))
    assert "GB18030" in result["text"]

