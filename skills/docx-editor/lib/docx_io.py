"""
docx_io.py — ZIP read/write + document.xml extraction

移植自 Mike docxTrackedChanges.ts 的 ZIP / XML I/O 部分，改用 lxml + Python zipfile。

設計：
- read_docx_to_xml(): 讀 docx bytes → (ZipFile in-memory, lxml root element)
- write_xml_to_docx(): 改後的 lxml root → 寫回 ZIP bytes
- extract_paragraph_text(): 抽段落純文字
- find_max_id(): 掃全文最大 w:id（給新 edit 用）

XML namespace:
  W  = http://schemas.openxmlformats.org/wordprocessingml/2006/main
"""

import io
import zipfile
from copy import deepcopy
from typing import Tuple

from lxml import etree

# Word namespace
W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
W_TAG = f"{{{W}}}"

# Convenience tag builders
def _w(tag: str) -> str:
    return f"{{{W}}}{tag}"


def read_docx_to_xml(docx_bytes: bytes) -> Tuple[zipfile.ZipFile, etree._Element]:
    """
    讀 docx bytes，回傳 (ZipFile, document.xml root element)。

    Raises:
        ValueError: 不是合法 zip 或缺少 document.xml 或 XML malformed
    """
    try:
        zf = zipfile.ZipFile(io.BytesIO(docx_bytes))
    except zipfile.BadZipFile as e:
        raise ValueError(f"not a valid docx (not a zip): {e}") from e

    # Handle both forward-slash and backslash paths (like Mike's getZipEntry)
    doc_path = _find_zip_entry(zf, "word/document.xml")
    if doc_path is None:
        zf.close()
        raise ValueError("not a valid docx (missing document.xml)")

    try:
        xml_bytes = zf.read(doc_path)
    except Exception as e:
        zf.close()
        raise ValueError(f"not a valid docx (cannot read document.xml): {e}") from e

    try:
        root = etree.fromstring(xml_bytes)
    except etree.XMLSyntaxError as e:
        zf.close()
        raise ValueError(f"document.xml is malformed: {e}") from e

    return zf, root


def _find_zip_entry(zf: zipfile.ZipFile, path_slash: str) -> str:
    """
    找 ZIP 中的條目，支援 forward-slash 和 backslash 兩種形式（Mike 的 getZipEntry 邏輯）。
    Returns the actual entry name in the ZIP, or None if not found.
    """
    names = zf.namelist()
    if path_slash in names:
        return path_slash
    # Try backslash variant
    path_back = path_slash.replace("/", "\\")
    if path_back in names:
        return path_back
    return None


def write_xml_to_docx(
    original_zip: zipfile.ZipFile,
    new_document_xml: etree._Element,
) -> bytes:
    """
    把改過的 document.xml 寫回，其他檔案原封不動，回傳新 ZIP bytes。

    注意：
    - 只改 word/document.xml（或其 backslash 變體）
    - 其他條目 byte-for-byte 複製
    - 用 DEFLATE 壓縮（與 Word 預設一致）
    """
    doc_path = _find_zip_entry(original_zip, "word/document.xml")

    # Serialize new document.xml
    new_xml_bytes = etree.tostring(
        new_document_xml,
        xml_declaration=True,
        encoding="UTF-8",
        standalone=True,
    )

    out_buf = io.BytesIO()
    with zipfile.ZipFile(out_buf, "w", compression=zipfile.ZIP_DEFLATED) as out_zf:
        for entry in original_zip.infolist():
            if entry.filename == doc_path:
                # Write new document.xml
                out_zf.writestr(entry, new_xml_bytes)
            else:
                # Copy other entries verbatim
                out_zf.writestr(entry, original_zip.read(entry.filename))

    return out_buf.getvalue()


def extract_paragraph_text(p_element: etree._Element) -> str:
    """
    抽段落純文字（concat 所有 <w:t> 內容，忽略 <w:delText>）。

    等同 Mike 的 flattenParagraph 的文字抽取部分（但不做 run 追蹤）。
    """
    result = []
    for el in p_element.iter():
        tag = el.tag
        if tag == _w("t"):
            result.append(el.text or "")
        # w:delText is intentionally skipped (deleted content not in accepted view)
    return "".join(result)


def find_max_id(document_root: etree._Element) -> int:
    """
    掃全文檔最大 w:id 值（int），給新 edit 用。
    掃 w:ins 和 w:del 的 w:id 屬性。
    """
    max_id = 0
    id_attr = _w("id")
    for el in document_root.iter():
        tag = el.tag
        if tag == _w("ins") or tag == _w("del"):
            raw = el.get(id_attr)
            if raw is not None:
                try:
                    v = int(raw)
                    if v > max_id:
                        max_id = v
                except (ValueError, TypeError):
                    pass
    return max_id


def get_body(document_root: etree._Element):
    """回傳 w:body element，若無則 raise ValueError。"""
    body = document_root.find(_w("body"))
    if body is None:
        raise ValueError("document.xml missing w:body")
    return body


def collect_paragraphs(node: etree._Element) -> list:
    """
    遞迴收集所有段落（w:p），包含表格內的段落。
    不進 w:ins/w:del（tracked changes wrappers）的直接子段落，
    但會進 tbl/tr/tc/sdt/sdtContent。
    """
    result = []
    for child in node:
        tag = child.tag
        if tag == _w("p"):
            result.append(child)
        elif tag in (_w("tbl"), _w("tr"), _w("tc"), _w("sdt"), _w("sdtContent")):
            result.extend(collect_paragraphs(child))
    return result
