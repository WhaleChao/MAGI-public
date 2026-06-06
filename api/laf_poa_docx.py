from __future__ import annotations

import datetime as _dt
import html
import os
import re
import shutil
import tempfile
import zipfile
from pathlib import Path
from typing import Any

from api.laf_branch_profiles import (
    DEFAULT_LAWYER_NAME,
    get_law_firm_profile,
    normalize_branch_label,
    resolve_laf_branch_profile,
)


FALSE_VALUES = {"0", "false", "no", "off", "disabled"}
TEMPLATE_DIR = Path(__file__).resolve().parents[1] / "templates" / "laf_poa"
TEMPLATE_FILENAMES = {
    "general": "general.docx",
    "indigenous_center": "indigenous_center.docx",
}


def laf_poa_docx_enabled() -> bool:
    return os.environ.get("MAGI_LAF_POA_DOCX", "1").strip().lower() not in FALSE_VALUES


def laf_poa_docx_templates_enabled() -> bool:
    return os.environ.get("MAGI_LAF_POA_DOCX_TEMPLATES", "1").strip().lower() not in FALSE_VALUES


def is_laf_power_of_attorney_pdf(path: str | os.PathLike[str]) -> bool:
    p = Path(path)
    return p.suffix.lower() == ".pdf" and "委任狀" in p.name and not p.name.startswith("._")


def laf_poa_docx_path(pdf_path: str | os.PathLike[str]) -> Path:
    p = Path(pdf_path)
    return p.with_name(f"{p.stem}（可填寫版）.docx")


def laf_poa_template_docx_path(pdf_path: str | os.PathLike[str]) -> Path:
    p = Path(pdf_path)
    return p.with_name(f"{p.stem}（範本）.docx")


def laf_poa_case_docx_path(pdf_path: str | os.PathLike[str]) -> Path:
    return laf_poa_docx_path(pdf_path)


def _text(value: Any) -> str:
    return str(value or "").strip()


def _metadata_first(data: dict[str, Any], *keys: str) -> str:
    for key in keys:
        value = _text(data.get(key))
        if value:
            return value
    return ""


def normalize_laf_branch_label(branch: str) -> str:
    return normalize_branch_label(branch)


def _laf_branch_phone(data: dict[str, Any], branch_label: str) -> str:
    explicit = _metadata_first(
        data,
        "branch_phone",
        "laf_branch_phone",
        "office_phone",
        "division_phone",
        "phone",
    )
    if explicit:
        return explicit
    profile = resolve_laf_branch_profile(branch_label)
    return profile.phone if profile and profile.phone else "待確認"


def _default_lawyer_name(case_type: str, case_reason: str) -> str:
    return DEFAULT_LAWYER_NAME


def _normalize_lawyer_name(value: str, case_type: str, case_reason: str) -> str:
    # 法扶委任狀受任人固定為喬政翔律師；不得受 DB 承辦律師或案件種類干擾。
    return DEFAULT_LAWYER_NAME


def _roc_date_parts(data: dict[str, Any]) -> tuple[str, str, str]:
    explicit_roc = (
        _metadata_first(data, "roc_year", "roc_y"),
        _metadata_first(data, "roc_month", "roc_m"),
        _metadata_first(data, "roc_day", "roc_d"),
    )
    if all(explicit_roc):
        return explicit_roc

    raw = _metadata_first(data, "document_date", "poa_date", "date", "download_date")
    parsed: _dt.date | None = None
    if raw:
        for fmt in ("%Y-%m-%d", "%Y/%m/%d", "%Y%m%d"):
            try:
                parsed = _dt.datetime.strptime(raw, fmt).date()
                break
            except ValueError:
                pass
    if parsed is None:
        parsed = _dt.date.today()
    return str(parsed.year - 1911), str(parsed.month), str(parsed.day)


def _roc_date_from_filename(path: Path) -> dict[str, str]:
    # Official LAF files usually end with a ROC date, e.g.
    # 委任狀_1150421-W-004_1150529.pdf.
    matches = list(re.finditer(r"(?:^|_)(1\d{2})(\d{2})(\d{2})(?:\D|$)", path.stem))
    if not matches:
        return {}
    match = matches[-1]
    return {
        "roc_year": str(int(match.group(1))),
        "roc_month": str(int(match.group(2))),
        "roc_day": str(int(match.group(3))),
    }


def _stage_marks(data: dict[str, Any]) -> str:
    stage = _metadata_first(data, "stage", "trial_stage", "instance", "審級")
    if "偵" in stage:
        return "□第　　審　■偵 查 中　□"
    if "二" in stage or "2" in stage:
        return "□第 一 審　■第 二 審　□偵 查 中　□"
    if "三" in stage or "3" in stage:
        return "□第 一 審　□第 二 審　■第 三 審　□偵 查 中　□"
    if "一" in stage or "1" in stage:
        return "■第 一 審　□第 二 審　□偵 查 中　□"
    return "□第　　審　□偵 查 中　□"


def _role_marks(data: dict[str, Any], case_type: str) -> str:
    role = _metadata_first(data, "poa_role", "role", "case_role")
    joined = f"{case_type} {role}"
    if "刑" in joined or "辯" in joined:
        return "□代 理 人　□告訴代理人　■辯 護 人　□輔 佐 人"
    if "告訴" in joined:
        return "□代 理 人　■告訴代理人　□辯 護 人　□輔 佐 人"
    return "■代 理 人　□告訴代理人　□辯 護 人　□輔 佐 人"


def _court_line(data: dict[str, Any]) -> str:
    court = _metadata_first(data, "court", "court_name", "法院", "prosecutor_office")
    if not court:
        return "□　　　　　　　法 院　□　　　　　　檢察署　□轉呈　□　　　　　委員會"
    if "檢" in court:
        return f"□ 法 院　■ {court}　□轉呈　□ 委員會"
    if "委員會" in court:
        return f"□ 法 院　□ 檢察署　□轉呈　■ {court}"
    return f"■ {court}　□ 檢察署　□轉呈　□ 委員會"


def _compact_pdf_line(line: str) -> str:
    return re.sub(r"\s+", " ", line).strip()


def _next_meaningful_line(lines: list[str], start: int) -> str:
    for line in lines[start:]:
        value = _compact_pdf_line(line)
        if value:
            return value
    return ""


def _extract_laf_poa_pdf_metadata(path: Path) -> dict[str, str]:
    """Extract official fields from the downloaded LAF POA PDF itself."""

    data: dict[str, str] = {}
    try:
        import fitz  # type: ignore

        with fitz.open(str(path)) as pdf:
            text = "\n".join(page.get_text("text") for page in pdf)
    except Exception:
        return data

    normalized_text = re.sub(r"[ \t\u3000]+", " ", text)
    lines = [_compact_pdf_line(line) for line in text.splitlines()]

    laf_match = re.search(r"本會申請編號[:：]\s*([0-9]{6,7}-[A-Z]-\d{3})", normalized_text)
    if laf_match:
        data["laf_case_number"] = laf_match.group(1)

    branch_match = re.search(r"本事件經本會\s*(.+?)\s*審核准予扶助", normalized_text, re.S)
    if branch_match:
        data["branch"] = _compact_pdf_line(branch_match.group(1))
    phone_match = re.search(r"逕致電分會\(([^)]+)\)", normalized_text)
    if phone_match:
        data["branch_phone"] = _compact_pdf_line(phone_match.group(1))

    if "受原住民族委員會委託辦理原住民法律扶助專用委任狀" in normalized_text:
        data["branch"] = data.get("branch") or "原住民族法律服務中心"

    court_case_match = re.search(r"案號[:：]\s*([^\n\r]+)", normalized_text)
    if court_case_match:
        court_case = _compact_pdf_line(court_case_match.group(1))
        if any(ch.isdigit() for ch in court_case):
            data["court_case_number"] = court_case

    id_match = re.search(r"\b([A-Z][12]\d{8})\b", normalized_text)
    if id_match:
        data["client_id"] = id_match.group(1)

    lawyer_match = re.search(r"([\u4e00-\u9fff]{2,4}律師)", normalized_text)
    if lawyer_match:
        data["lawyer_name"] = lawyer_match.group(1)

    reason_match = re.search(r"為\s*([^\n\r]{1,40}?)\s*事[（(]案[）)]件", normalized_text)
    if reason_match:
        reason = _compact_pdf_line(reason_match.group(1))
        if reason and " " not in reason:
            data["case_reason"] = reason

    for idx, line in enumerate(lines):
        if line == "姓名" and not data.get("client_name"):
            name = _next_meaningful_line(lines, idx + 1)
            if name and "律師" not in name and len(name) <= 20:
                data["client_name"] = name
        elif line == "出生年月日" and not data.get("client_birthday"):
            birthday = _next_meaningful_line(lines, idx + 1)
            if re.search(r"\d+\s*年\s*\d+\s*月\s*\d+\s*日", birthday):
                data["client_birthday"] = birthday.replace(" ", "")

    data.update({k: v for k, v in _roc_date_from_filename(path).items() if v})
    return data


def _normalize_case_metadata(case_metadata: dict[str, Any] | None) -> dict[str, str]:
    data = case_metadata or {}
    case_type = _metadata_first(data, "case_type", "type", "category", "case_category")
    case_reason = _metadata_first(data, "case_reason", "reason", "cause", "案由")
    branch_label = normalize_laf_branch_label(_metadata_first(data, "branch", "laf_branch", "division"))
    branch_phone = _laf_branch_phone(data, branch_label)
    roc_year, roc_month, roc_day = _roc_date_parts(data)
    lawyer_name = _normalize_lawyer_name(
        _metadata_first(data, "lawyer_name", "lawyer", "attorney", "assigned_lawyer"),
        case_type,
        case_reason,
    )
    return {
        "client_name": _metadata_first(data, "client_name", "name", "party_name", "當事人"),
        "client_birthday": _metadata_first(data, "client_birthday", "birthday", "birth_date", "birth"),
        "client_id": _metadata_first(data, "client_id", "id_number", "identity_number", "national_id"),
        "client_address_phone": _metadata_first(data, "client_address_phone", "address_phone", "address"),
        "laf_case_number": _metadata_first(data, "laf_case_number", "legal_aid_number", "applyno", "case_number"),
        "court_case_number": _metadata_first(data, "court_case_number", "court_case_no", "court_no", "court_number"),
        "branch": branch_label,
        "branch_phone": branch_phone,
        "case_reason": case_reason,
        "case_type": case_type,
        "lawyer_name": lawyer_name,
        "court_line": _court_line(data),
        "stage_marks": _stage_marks(data),
        "role_marks": _role_marks(data, case_type),
        "roc_year": roc_year,
        "roc_month": roc_month,
        "roc_day": roc_day,
    }


def select_laf_poa_template(
    case_metadata: dict[str, str] | None = None,
) -> tuple[str, Path] | None:
    if not laf_poa_docx_templates_enabled():
        return None
    data = case_metadata or {}
    key = "indigenous_center" if "原住民族" in _text(data.get("branch")) else "general"
    template_path = TEMPLATE_DIR / TEMPLATE_FILENAMES[key]
    if not template_path.exists():
        return None
    return key, template_path


def _template_values(metadata: dict[str, str]) -> dict[str, str]:
    firm = get_law_firm_profile()
    return {
        "LAF_CASE_NUMBER": metadata["laf_case_number"],
        "COURT_CASE_NUMBER": metadata["court_case_number"],
        "CLIENT_NAME": metadata["client_name"],
        "CLIENT_BIRTHDAY": metadata["client_birthday"],
        "CLIENT_ID": metadata["client_id"],
        "LAWYER_NAME": firm.lawyer_name,
        "LAW_FIRM_OFFICE_NAME": firm.office_name,
        "LAW_FIRM_ADDRESS_LINE": firm.address_line,
        "LAW_FIRM_PHONE": firm.phone,
        "LAW_FIRM_FAX": firm.fax,
        "LAW_FIRM_MOBILE": firm.mobile,
        "CASE_REASON": metadata["case_reason"],
        "COURT_LINE": metadata["court_line"],
        "STAGE_MARKS": metadata["stage_marks"],
        "ROLE_MARKS": metadata["role_marks"],
        "BRANCH_LABEL": metadata["branch"] or "待確認分會",
        "BRANCH_PHONE": metadata["branch_phone"],
        "ROC_YEAR": metadata["roc_year"],
        "ROC_MONTH": metadata["roc_month"],
        "ROC_DAY": metadata["roc_day"],
    }


def _replace_docx_placeholders(template_path: Path, output_path: Path, values: dict[str, str]) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = output_path.with_suffix(output_path.suffix + ".tmp")
    with zipfile.ZipFile(template_path, "r") as src, zipfile.ZipFile(tmp_path, "w", zipfile.ZIP_DEFLATED) as dst:
        for item in src.infolist():
            payload = src.read(item.filename)
            if item.filename.endswith(".xml"):
                xml = payload.decode("utf-8")
                for key, value in values.items():
                    xml = xml.replace(f"{{{{{key}}}}}", html.escape(_text(value)))
                payload = xml.encode("utf-8")
            dst.writestr(item, payload)
    try:
        from docx import Document  # type: ignore

        Document(str(tmp_path))
    except Exception:
        tmp_path.unlink(missing_ok=True)
        raise
    os.replace(tmp_path, output_path)


def _create_from_laf_poa_template(
    template_key: str,
    template_path: Path,
    template_target: Path,
    target: Path,
    metadata: dict[str, str],
) -> dict[str, str]:
    tmp_template = template_target.with_suffix(template_target.suffix + ".tmp")
    template_target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(template_path, tmp_template)
    try:
        from docx import Document  # type: ignore

        Document(str(tmp_template))
    except Exception:
        tmp_template.unlink(missing_ok=True)
        raise
    os.replace(tmp_template, template_target)

    values = _template_values(metadata)
    _replace_docx_placeholders(template_path, target, values)
    values["template_key"] = template_key
    return values


def ensure_laf_poa_docx_companion(
    pdf_path: str | os.PathLike[str],
    *,
    overwrite: bool = False,
    case_metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Create a Word companion for LAF power-of-attorney PDFs.

    The output preserves the official PDF's visual layout by rendering each page
    into Word at the same page size.  The original PDF remains untouched.
    """

    source = Path(pdf_path)
    template_target = laf_poa_template_docx_path(source)
    target = laf_poa_case_docx_path(source)
    result: dict[str, Any] = {
        "ok": False,
        "status": "",
        "pdf_path": str(source),
        "template_docx_path": str(template_target),
        "docx_path": str(target),
        "pages": 0,
        "error": "",
        "filled_fields": {},
        "template_key": "",
        "warnings": [],
    }

    if not laf_poa_docx_enabled():
        result.update(ok=True, status="disabled")
        return result
    if not is_laf_power_of_attorney_pdf(source):
        result.update(ok=True, status="not_poa_pdf")
        return result
    if not source.exists():
        result.update(status="missing_pdf", error="pdf_not_found")
        return result

    pdf_metadata = _extract_laf_poa_pdf_metadata(source)
    combined_metadata: dict[str, Any] = dict(case_metadata or {})
    # The official POA PDF is authoritative for its own case number, party, and
    # branch footer. DB/email metadata only fills fields the PDF leaves blank.
    combined_metadata.update({k: v for k, v in pdf_metadata.items() if v})
    normalized_metadata = _normalize_case_metadata(combined_metadata)
    result["pdf_extracted_fields"] = pdf_metadata

    if target.exists() and template_target.exists() and not overwrite:
        try:
            if target.stat().st_mtime >= source.stat().st_mtime and target.stat().st_size > 0:
                result.update(ok=True, status="exists")
                return result
        except OSError:
            pass

    selected_template = select_laf_poa_template(normalized_metadata)
    if selected_template is not None:
        template_key, template_path = selected_template
        try:
            filled_fields = _create_from_laf_poa_template(
                template_key,
                template_path,
                template_target,
                target,
                normalized_metadata,
            )
            result.update(
                ok=True,
                status="created",
                pages=1,
                filled_fields={k: v for k, v in normalized_metadata.items() if v},
                template_key=template_key,
                branch_label=normalized_metadata.get("branch", ""),
                branch_phone=normalized_metadata.get("branch_phone", ""),
                template_values={k: v for k, v in filled_fields.items() if v},
            )
            if normalized_metadata.get("branch_phone") == "待確認":
                result["warnings"].append("missing_branch_phone")
            return result
        except Exception as exc:
            result["warnings"].append(f"template_failed:{exc}")

    try:
        import fitz  # type: ignore
        from docx import Document  # type: ignore
        from docx.enum.table import WD_CELL_VERTICAL_ALIGNMENT, WD_TABLE_ALIGNMENT  # type: ignore
        from docx.enum.section import WD_SECTION_START  # type: ignore
        from docx.enum.text import WD_ALIGN_PARAGRAPH  # type: ignore
        from docx.enum.table import WD_ROW_HEIGHT_RULE  # type: ignore
        from docx.oxml import OxmlElement  # type: ignore
        from docx.oxml.ns import qn  # type: ignore
        from docx.shared import Mm, Pt  # type: ignore

        def emu(points: float) -> int:
            return int(round(points * 12700))

        def clear_paragraph(paragraph) -> None:
            paragraph.alignment = WD_ALIGN_PARAGRAPH.LEFT
            paragraph.paragraph_format.space_before = Pt(0)
            paragraph.paragraph_format.space_after = Pt(0)
            paragraph.paragraph_format.line_spacing = 1

        def set_table_borders_none(table) -> None:
            tbl = table._tbl
            tbl_pr = tbl.tblPr
            borders = tbl_pr.find(qn("w:tblBorders"))
            if borders is None:
                borders = OxmlElement("w:tblBorders")
                tbl_pr.append(borders)
            for name in ("top", "left", "bottom", "right", "insideH", "insideV"):
                edge = borders.find(qn(f"w:{name}"))
                if edge is None:
                    edge = OxmlElement(f"w:{name}")
                    borders.append(edge)
                edge.set(qn("w:val"), "nil")

        def set_cell_margins_zero(cell) -> None:
            tc_pr = cell._tc.get_or_add_tcPr()
            margins = tc_pr.first_child_found_in("w:tcMar")
            if margins is None:
                margins = OxmlElement("w:tcMar")
                tc_pr.append(margins)
            for name in ("top", "start", "left", "bottom", "end", "right"):
                margin = margins.find(qn(f"w:{name}"))
                if margin is None:
                    margin = OxmlElement(f"w:{name}")
                    margins.append(margin)
                margin.set(qn("w:w"), "0")
                margin.set(qn("w:type"), "dxa")

        def inline_to_page_background(inline, width_pt: float, height_pt: float) -> None:
            inline.tag = qn("wp:anchor")
            for key, value in {
                "distT": "0",
                "distB": "0",
                "distL": "0",
                "distR": "0",
                "simplePos": "0",
                "relativeHeight": "251659264",
                "behindDoc": "1",
                "locked": "0",
                "layoutInCell": "1",
                "allowOverlap": "1",
            }.items():
                inline.set(key, value)

            extent = inline.find(qn("wp:extent"))
            if extent is not None:
                extent.set("cx", str(emu(width_pt)))
                extent.set("cy", str(emu(height_pt)))

            simple_pos = OxmlElement("wp:simplePos")
            simple_pos.set("x", "0")
            simple_pos.set("y", "0")
            pos_h = OxmlElement("wp:positionH")
            pos_h.set("relativeFrom", "page")
            h_offset = OxmlElement("wp:posOffset")
            h_offset.text = "0"
            pos_h.append(h_offset)
            pos_v = OxmlElement("wp:positionV")
            pos_v.set("relativeFrom", "page")
            v_offset = OxmlElement("wp:posOffset")
            v_offset.text = "0"
            pos_v.append(v_offset)

            inline.insert(0, pos_v)
            inline.insert(0, pos_h)
            inline.insert(0, simple_pos)

            extent = inline.find(qn("wp:extent"))
            extent_index = list(inline).index(extent) if extent is not None else 2
            effect = OxmlElement("wp:effectExtent")
            for key in ("l", "t", "r", "b"):
                effect.set(key, "0")
            wrap = OxmlElement("wp:wrapNone")
            inline.insert(extent_index + 1, effect)
            inline.insert(extent_index + 2, wrap)

        def add_typing_page(doc, section, image_path: Path, width_pt: float, height_pt: float) -> None:
            section.page_width = Pt(width_pt)
            section.page_height = Pt(height_pt)
            section.top_margin = Mm(0)
            section.bottom_margin = Mm(0)
            section.left_margin = Mm(0)
            section.right_margin = Mm(0)
            section.header_distance = Mm(0)
            section.footer_distance = Mm(0)

            rows, cols = 28, 8
            table = doc.add_table(rows=rows, cols=cols)
            table.alignment = WD_TABLE_ALIGNMENT.LEFT
            table.autofit = False
            set_table_borders_none(table)

            col_width = width_pt / cols
            row_height = max(1.0, (height_pt - 2.0) / rows)
            for row in table.rows:
                row.height = Pt(row_height)
                row.height_rule = WD_ROW_HEIGHT_RULE.EXACTLY
                for cell in row.cells:
                    cell.width = Pt(col_width)
                    cell.vertical_alignment = WD_CELL_VERTICAL_ALIGNMENT.TOP
                    set_cell_margins_zero(cell)
                    for paragraph in cell.paragraphs:
                        clear_paragraph(paragraph)

            paragraph = table.cell(0, 0).paragraphs[0]
            inline_shape = paragraph.add_run().add_picture(
                str(image_path),
                width=Pt(width_pt),
                height=Pt(height_pt),
            )
            inline_to_page_background(inline_shape._inline, width_pt, height_pt)

        def build_template_document() -> tuple[Any, int]:
            doc = Document()
            first_section = doc.sections[0]
            with fitz.open(str(source)) as pdf, tempfile.TemporaryDirectory(prefix="magi_laf_poa_") as tmpdir:
                if len(pdf) == 0:
                    return doc, 0

                page_count = 0
                for idx, page in enumerate(pdf):
                    section = first_section if idx == 0 else doc.add_section(WD_SECTION_START.NEW_PAGE)
                    rect = page.rect

                    pix = page.get_pixmap(matrix=fitz.Matrix(2, 2), alpha=False)
                    img_path = Path(tmpdir) / f"page_{idx + 1}.png"
                    pix.save(str(img_path))
                    add_typing_page(doc, section, img_path, float(rect.width), float(rect.height))
                    page_count += 1
            return doc, page_count

        def add_case_metadata_page(doc) -> None:
            if not any(normalized_metadata.values()):
                return
            section = doc.add_section(WD_SECTION_START.NEW_PAGE)
            section.top_margin = Mm(18)
            section.bottom_margin = Mm(18)
            section.left_margin = Mm(20)
            section.right_margin = Mm(20)
            title = doc.add_paragraph()
            title_run = title.add_run("MAGI 填寫資料")
            title_run.bold = True
            title_run.font.size = Pt(14)
            info = [
                ("分會", normalized_metadata["branch"]),
                ("法扶案號", normalized_metadata["laf_case_number"]),
                ("當事人", normalized_metadata["client_name"]),
                ("案件類型", normalized_metadata["case_type"]),
                ("案由", normalized_metadata["case_reason"]),
            ]
            table = doc.add_table(rows=0, cols=2)
            table.autofit = True
            for label, value in info:
                if not value:
                    continue
                cells = table.add_row().cells
                cells[0].text = label
                cells[1].text = value

        target.parent.mkdir(parents=True, exist_ok=True)
        template_doc, page_count = build_template_document()
        if page_count == 0:
            result.update(status="empty_pdf", error="pdf_has_no_pages")
            return result

        tmp_template = template_target.with_suffix(template_target.suffix + ".tmp")
        template_doc.save(str(tmp_template))
        from docx import Document as _Document  # type: ignore
        _Document(str(tmp_template))
        os.replace(tmp_template, template_target)

        case_doc, _ = build_template_document()
        add_case_metadata_page(case_doc)
        tmp_docx = target.with_suffix(target.suffix + ".tmp")
        case_doc.save(str(tmp_docx))
        _Document(str(tmp_docx))
        os.replace(tmp_docx, target)

        result.update(
            ok=True,
            status="created",
            pages=page_count,
            filled_fields={k: v for k, v in normalized_metadata.items() if v},
        )
        return result
    except Exception as exc:
        result.update(status="failed", error=str(exc))
        return result
