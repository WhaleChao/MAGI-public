import json
import logging
import os
import zipfile
import xml.etree.ElementTree as ET

logger = logging.getLogger("FileBridge")

from skills.documents.vector_pipeline import ingest_text_to_vector_memory


def _chunk_text(text: str, chunk_chars: int = 2000, overlap: int = 150, max_chunks: int = 200) -> list[str]:
    s = (text or "").strip()
    if not s:
        return []
    chunk_chars = int(max(400, chunk_chars))
    overlap = int(max(0, min(overlap, chunk_chars // 2)))
    out = []
    i = 0
    while i < len(s) and len(out) < int(max_chunks):
        out.append(s[i : i + chunk_chars])
        i += max(1, chunk_chars - overlap)
    return out


def _sample_evenly(chunks: list[str], max_samples: int) -> list[tuple[int, str]]:
    parts = [c for c in (chunks or []) if (c or "").strip()]
    if not parts:
        return []
    n = len(parts)
    k = max(1, min(int(max_samples), n))
    if k >= n:
        return [(i + 1, parts[i]) for i in range(n)]
    idxs = sorted({int(round(i * (n - 1) / (k - 1))) for i in range(k)}) if k > 1 else [0]
    out = []
    for i in idxs:
        i = max(0, min(n - 1, i))
        out.append((i + 1, parts[i]))
    return out


def _read_text(path: str, max_bytes: Optional[int] = 512_000) -> str:
    with open(path, "rb") as f:
        if max_bytes is None or int(max_bytes) <= 0:
            raw = f.read()
        else:
            raw = f.read(int(max_bytes))
    try:
        return raw.decode("utf-8")
    except Exception:
        return raw.decode("utf-8", errors="ignore")


def _extract_docx_text(path: str, max_chars: Optional[int] = 120_000) -> str:
    """
    Minimal .docx text extraction without extra dependencies.
    """
    if not zipfile.is_zipfile(path):
        return ""
    try:
        with zipfile.ZipFile(path, "r") as z:
            xml_bytes = z.read("word/document.xml")
        root = ET.fromstring(xml_bytes)
        texts = []
        for el in root.iter():
            # WordprocessingML text nodes usually end with }t
            if el.tag.endswith("}t") and el.text:
                texts.append(el.text)
        out = "\n".join(t.strip() for t in texts if t and t.strip())
        if max_chars is None or int(max_chars) <= 0:
            return out
        return out[: int(max_chars)]
    except Exception as e:
        logger.warning(f"DOCX extract failed: {e}")
        return ""


def extract_text_from_file(
    path: str,
    filename: str = "",
    *,
    max_bytes: Optional[int] = 512_000,
    max_json_chars: Optional[int] = 200_000,
    max_docx_chars: Optional[int] = 120_000,
) -> dict:
    """
    Returns {"success": bool, "text": str, "type": str, "error": str}
    """
    p = (path or "").strip()
    if not p or not os.path.exists(p):
        return {"success": False, "text": "", "type": "", "error": f"file not found: {p}"}

    name = (filename or os.path.basename(p) or "").lower()
    ext = os.path.splitext(name)[1].lower()

    # Text-like files
    if ext in {".txt", ".md", ".log", ".csv"}:
        return {"success": True, "text": _read_text(p, max_bytes=max_bytes), "type": ext.lstrip("."), "error": ""}

    if ext in {".json"}:
        try:
            data = json.loads(_read_text(p, max_bytes=max_bytes))
            pretty = json.dumps(data, ensure_ascii=False, indent=2)
            if max_json_chars is not None and int(max_json_chars) > 0:
                pretty = pretty[: int(max_json_chars)]
            return {"success": True, "text": pretty, "type": "json", "error": ""}
        except Exception as e:
            return {"success": True, "text": _read_text(p, max_bytes=max_bytes), "type": "json", "error": f"json parse error: {e}"}

    # Office docs
    if ext in {".docx"}:
        text = _extract_docx_text(p, max_chars=max_docx_chars)
        if text:
            return {"success": True, "text": text, "type": "docx", "error": ""}
        return {"success": False, "text": "", "type": "docx", "error": "docx extract returned empty text"}

    return {"success": False, "text": "", "type": ext.lstrip("."), "error": f"unsupported file type: {ext or name}"}


def summarize_extracted_text(title: str, text: str, max_chars: int = 9000, source_primary: str = "") -> str:
    content = (text or "").strip()
    if not content:
        return "⚠️ 文件內容為空或無法解析。"
    doc_key = ""
    try:
        if len(content) > max_chars:
            _chunk_sz = int(os.environ.get("MAGI_FILE_VECTOR_CHUNK_CHARS", "1200") or "1200")
            _cfg_max = int(os.environ.get("MAGI_FILE_VECTOR_MAX_CHUNKS", "0") or "0")
            _hard_max = int(os.environ.get("MAGI_FILE_VECTOR_MAX_CHUNKS_HARD", "99999") or "99999")
            _auto_max = max(20, (len(content) // max(1, _chunk_sz)) + 10)
            _vec_max = min(_hard_max, _cfg_max if _cfg_max > 0 else _auto_max)
            ing = ingest_text_to_vector_memory(
                kind="file",
                primary=(source_primary or title or content),
                title=title,
                text=content,
                chunk_chars=_chunk_sz,
                overlap=int(os.environ.get("MAGI_FILE_VECTOR_OVERLAP", "120")),
                max_chunks_total=_vec_max,
            )
            if ing.get("success"):
                doc_key = ing.get("doc_key", "") or ""
    except Exception as e:
        logger.warning(f"Vector ingest skipped: {e}")

    # If very long, summarize from sampled chunks (better coverage than naive truncation).
    if len(content) > max_chars:
        chunks = _chunk_text(
            content,
            chunk_chars=int(os.environ.get("MAGI_FILE_SUMMARY_CHUNK_CHARS", "2200")),
            overlap=int(os.environ.get("MAGI_FILE_SUMMARY_OVERLAP", "180")),
            max_chunks=600,
        )
        sampled = _sample_evenly(chunks, max_samples=int(os.environ.get("MAGI_FILE_SUMMARY_SAMPLES", "6")))
        payload = []
        for i, part in sampled:
            payload.append(f"[Chunk {i}/{len(chunks)}]\n{part}")
        content_for_llm = "\n\n".join(payload)[:max_chars]
    else:
        content_for_llm = content

    from skills.bridge.grounded_ai import chat_casper

    prompt = (
        "請用繁體中文摘要以下文件內容，條列 3-7 點重點；若是表格/清單，請把規則與關鍵數字整理出來。\n\n"
        f"[文件標題]\n{title}\n\n"
        f"[內容]\n{content_for_llm}\n"
    )
    summary = chat_casper(prompt)
    foot = ""
    if doc_key and os.environ.get("MAGI_VECTOR_NOTE", "0") == "1":
        foot = f"\n\n[向量索引] doc_key={doc_key}"
    return f"📄 **文件摘要**\n\n{summary}{foot}"


def summarize_file(path: str, filename: str = "") -> str:
    info = extract_text_from_file(path, filename=filename)
    if not info.get("success"):
        return f"📄 檔案 `{filename or os.path.basename(path)}` 已接收，但無法摘要：{info.get('error')}"
    title = filename or os.path.basename(path)
    return summarize_extracted_text(title, info.get("text", ""), source_primary=path)
