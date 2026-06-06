from __future__ import annotations

import os
import tempfile
from pathlib import Path
from typing import Any


FALSE_VALUES = {"0", "false", "no", "off", "disabled"}


def laf_poa_docx_enabled() -> bool:
    return os.environ.get("MAGI_LAF_POA_DOCX", "1").strip().lower() not in FALSE_VALUES


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


def _normalize_case_metadata(case_metadata: dict[str, Any] | None) -> dict[str, str]:
    data = case_metadata or {}
    return {
        "client_name": str(data.get("client_name") or data.get("name") or "").strip(),
        "laf_case_number": str(
            data.get("laf_case_number")
            or data.get("legal_aid_number")
            or data.get("case_number")
            or ""
        ).strip(),
        "branch": str(data.get("branch") or data.get("laf_branch") or "").strip(),
        "case_reason": str(data.get("case_reason") or data.get("reason") or "").strip(),
        "case_type": str(data.get("case_type") or "").strip(),
    }


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
    normalized_metadata = _normalize_case_metadata(case_metadata)
    result: dict[str, Any] = {
        "ok": False,
        "status": "",
        "pdf_path": str(source),
        "template_docx_path": str(template_target),
        "docx_path": str(target),
        "pages": 0,
        "error": "",
        "filled_fields": {},
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
    if target.exists() and template_target.exists() and not overwrite:
        try:
            if target.stat().st_mtime >= source.stat().st_mtime and target.stat().st_size > 0:
                result.update(ok=True, status="exists")
                return result
        except OSError:
            pass

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
