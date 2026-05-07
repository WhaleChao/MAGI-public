#!/usr/bin/env node
// -*- coding: utf-8 -*-
/**
 * _docx_table_gen.js
 * ==================
 * MAGI 共用 docx 表格產生器。
 * 支援三種模式：bilingual（雙語對照）、transcript（逐字稿）、summary（摘要）。
 *
 * 用法：NODE_PATH=... node _docx_table_gen.js <data.json>
 */

const fs = require("fs");
const {
  Document, Packer, Paragraph, TextRun, Table, TableRow, TableCell,
  Header, Footer, AlignmentType, PageOrientation,
  BorderStyle, WidthType, ShadingType, VerticalAlign, PageNumber,
  HeightRule,
} = require("docx");

// ── Helpers ─────────────────────────────────────────────────────────────────

// Strip XML-illegal control characters (U+0000–U+0008, U+000B, U+000C, U+000E–U+001F)
// Keep TAB (0x09), LF (0x0A), CR (0x0D)
function sanitizeXml(text) {
  if (!text) return "";
  return text.replace(/[\x00-\x08\x0B\x0C\x0E-\x1F]/g, "");
}

// ── Design tokens ───────────────────────────────────────────────────────────

const FONT_CJK = "微軟正黑體";    // Primary CJK font
const FONT_LATIN = "Calibri";     // Latin fallback
const HEADER_FILL = "1F4E79";     // Deep blue header
const ROW_EVEN = "FFFFFF";
const ROW_ODD  = "F0F4F8";        // Soft blue-grey stripe
const ACCENT   = "1F4E79";        // Title & header accent

const BORDER_LIGHT = { style: BorderStyle.SINGLE, size: 1, color: "D0D5DD" };
const BORDERS = { top: BORDER_LIGHT, bottom: BORDER_LIGHT, left: BORDER_LIGHT, right: BORDER_LIGHT };
const BORDER_HEADER = { style: BorderStyle.SINGLE, size: 2, color: "1F4E79" };
const BORDERS_HEADER = { top: BORDER_HEADER, bottom: BORDER_HEADER, left: BORDER_HEADER, right: BORDER_HEADER };

const CELL_MARGINS = { top: 100, bottom: 100, left: 140, right: 140 };

// ── Text rendering ──────────────────────────────────────────────────────────

function makeTextParagraphs(text, fontSize, opts = {}) {
  const lines = sanitizeXml(text || "").split("\n").filter(l => l.trim());
  if (!lines.length) {
    return [new Paragraph({ children: [new TextRun({ text: "", font: FONT_CJK, size: fontSize })] })];
  }
  return lines.map(line => new Paragraph({
    spacing: { after: 80, line: 300 },
    ...opts,
    children: [new TextRun({
      text: line.trim(),
      font: FONT_CJK,
      size: fontSize,
      ...(opts.run || {}),
    })],
  }));
}

function headerCell(text, width) {
  return new TableCell({
    borders: BORDERS_HEADER,
    width: { size: width, type: WidthType.DXA },
    shading: { fill: HEADER_FILL, type: ShadingType.CLEAR },
    verticalAlign: VerticalAlign.CENTER,
    margins: CELL_MARGINS,
    children: [new Paragraph({
      alignment: AlignmentType.CENTER,
      spacing: { before: 40, after: 40 },
      children: [new TextRun({
        text,
        bold: true,
        color: "FFFFFF",
        font: FONT_CJK,
        size: 22,
      })],
    })],
  });
}

function dataCell(text, width, fontSize, fill, vAlign) {
  return new TableCell({
    borders: BORDERS,
    width: { size: width, type: WidthType.DXA },
    shading: { fill, type: ShadingType.CLEAR },
    verticalAlign: vAlign || VerticalAlign.TOP,
    margins: CELL_MARGINS,
    children: makeTextParagraphs(text, fontSize),
  });
}

// ── Bilingual mode ──────────────────────────────────────────────────────────

function buildBilingual(data) {
  // Landscape A4: content width ≈ 16838 - 2×1134 = 14570 DXA
  const tableWidth = 14400;
  const col1W = 660;    // 頁碼 (narrow)
  const col2W = 6870;   // 原文
  const col3W = 6870;   // 翻譯

  const labels = data.col_labels || {};
  const rows = [
    new TableRow({
      tableHeader: true,
      height: { value: 480, rule: HeightRule.AT_LEAST },
      children: [
        headerCell(labels.col1 || "頁碼", col1W),
        headerCell(labels.col2 || "原文", col2W),
        headerCell(labels.col3 || "翻譯", col3W),
      ],
    }),
  ];

  (data.pages || []).forEach((pg, i) => {
    const fill = i % 2 === 0 ? ROW_EVEN : ROW_ODD;
    rows.push(new TableRow({
      children: [
        new TableCell({
          borders: BORDERS,
          width: { size: col1W, type: WidthType.DXA },
          shading: { fill, type: ShadingType.CLEAR },
          verticalAlign: VerticalAlign.TOP,
          margins: CELL_MARGINS,
          children: [new Paragraph({
            alignment: AlignmentType.CENTER,
            spacing: { before: 40 },
            children: [new TextRun({
              text: `${pg.page || i + 1}`,
              bold: true,
              font: FONT_CJK,
              size: 20,
              color: "555555",
            })],
          })],
        }),
        dataCell(pg.source || "", col2W, 20, fill),
        dataCell(pg.target || "", col3W, 20, fill),
      ],
    }));
  });

  const titleChildren = [];
  if (data.title) {
    titleChildren.push(
      new Paragraph({
        alignment: AlignmentType.CENTER,
        spacing: { after: 100 },
        children: [new TextRun({
          text: data.title,
          bold: true,
          font: FONT_CJK,
          size: 32,
          color: ACCENT,
        })],
      })
    );
  }
  if (data.subtitle) {
    titleChildren.push(
      new Paragraph({
        alignment: AlignmentType.CENTER,
        spacing: { after: 300 },
        children: [new TextRun({
          text: data.subtitle,
          font: FONT_CJK,
          size: 22,
          color: "666666",
          italics: true,
        })],
      })
    );
  }

  return buildDoc({
    landscape: true,
    headerText: data.header_text || data.title || "",
    children: [
      ...titleChildren,
      new Table({
        width: { size: tableWidth, type: WidthType.DXA },
        columnWidths: [col1W, col2W, col3W],
        rows,
      }),
    ],
  });
}

// ── Transcript mode ─────────────────────────────────────────────────────────

function buildTranscript(data) {
  // Portrait A4: content width ≈ 11906 - 2×1440 = 9026
  const tableWidth = 9026;
  const col1W = 1200;  // speaker
  const col2W = 1000;  // time
  const col3W = 6826;  // content

  const speakerColors = {
    "法官": "DBEAFE", "審判長": "DBEAFE",
    "被告": "FEF3C7", "辯護人": "EDE9FE",
    "檢察官": "FCE7F3", "證人": "D1FAE5",
    "告訴人": "FEF9C3", "告訴代理人": "FEF9C3",
  };

  const rows = [
    new TableRow({
      tableHeader: true,
      height: { value: 480, rule: HeightRule.AT_LEAST },
      children: [
        headerCell("發言人", col1W),
        headerCell("時間", col2W),
        headerCell("內容", col3W),
      ],
    }),
  ];

  (data.segments || []).forEach((seg, i) => {
    const baseFill = speakerColors[seg.speaker] || (i % 2 === 0 ? ROW_EVEN : ROW_ODD);
    rows.push(new TableRow({
      children: [
        new TableCell({
          borders: BORDERS,
          width: { size: col1W, type: WidthType.DXA },
          shading: { fill: baseFill, type: ShadingType.CLEAR },
          verticalAlign: VerticalAlign.TOP,
          margins: CELL_MARGINS,
          children: [new Paragraph({
            children: [new TextRun({
              text: seg.speaker || "",
              bold: true,
              font: FONT_CJK,
              size: 20,
            })],
          })],
        }),
        new TableCell({
          borders: BORDERS,
          width: { size: col2W, type: WidthType.DXA },
          shading: { fill: baseFill, type: ShadingType.CLEAR },
          verticalAlign: VerticalAlign.TOP,
          margins: CELL_MARGINS,
          children: [new Paragraph({
            children: [new TextRun({
              text: seg.time || "",
              font: FONT_CJK,
              size: 18,
              color: "888888",
            })],
          })],
        }),
        dataCell(seg.content || "", col3W, 20, baseFill),
      ],
    }));
  });

  const titleChildren = [];
  if (data.title) {
    titleChildren.push(
      new Paragraph({
        alignment: AlignmentType.CENTER,
        spacing: { after: 200 },
        children: [new TextRun({
          text: data.title,
          bold: true,
          font: FONT_CJK,
          size: 28,
          color: ACCENT,
        })],
      })
    );
  }

  return buildDoc({
    landscape: false,
    headerText: data.case_info || data.title || "",
    children: [
      ...titleChildren,
      new Table({
        width: { size: tableWidth, type: WidthType.DXA },
        columnWidths: [col1W, col2W, col3W],
        rows,
      }),
    ],
  });
}

// ── Summary mode ────────────────────────────────────────────────────────────

function buildSummary(data) {
  // Landscape A4
  const tableWidth = 14400;
  const col1W = 550;   // #
  const col2W = 2000;  // heading
  const col3W = 5925;  // summary
  const col4W = 5925;  // excerpt

  const rows = [
    new TableRow({
      tableHeader: true,
      height: { value: 480, rule: HeightRule.AT_LEAST },
      children: [
        headerCell("#", col1W),
        headerCell("段落", col2W),
        headerCell("摘要", col3W),
        headerCell("原文節錄", col4W),
      ],
    }),
  ];

  (data.sections || []).forEach((sec, i) => {
    const fill = i % 2 === 0 ? ROW_EVEN : ROW_ODD;
    rows.push(new TableRow({
      children: [
        new TableCell({
          borders: BORDERS,
          width: { size: col1W, type: WidthType.DXA },
          shading: { fill, type: ShadingType.CLEAR },
          verticalAlign: VerticalAlign.TOP,
          margins: CELL_MARGINS,
          children: [new Paragraph({
            alignment: AlignmentType.CENTER,
            children: [new TextRun({
              text: `${i + 1}`,
              bold: true,
              font: FONT_CJK,
              size: 20,
            })],
          })],
        }),
        new TableCell({
          borders: BORDERS,
          width: { size: col2W, type: WidthType.DXA },
          shading: { fill, type: ShadingType.CLEAR },
          verticalAlign: VerticalAlign.TOP,
          margins: CELL_MARGINS,
          children: [new Paragraph({
            children: [new TextRun({
              text: sec.heading || "",
              bold: true,
              font: FONT_CJK,
              size: 20,
            })],
          })],
        }),
        dataCell(sec.summary || "", col3W, 20, fill),
        dataCell(sec.excerpt || "", col4W, 18, fill),
      ],
    }));
  });

  const titleChildren = [];
  if (data.title) {
    titleChildren.push(
      new Paragraph({
        alignment: AlignmentType.CENTER,
        spacing: { after: 200 },
        children: [new TextRun({
          text: data.title,
          bold: true,
          font: FONT_CJK,
          size: 32,
          color: ACCENT,
        })],
      })
    );
  }

  return buildDoc({
    landscape: true,
    headerText: data.title || "",
    children: [
      ...titleChildren,
      new Table({
        width: { size: tableWidth, type: WidthType.DXA },
        columnWidths: [col1W, col2W, col3W, col4W],
        rows,
      }),
    ],
  });
}

// ── Shared doc builder ──────────────────────────────────────────────────────

function buildDoc({ landscape, headerText, children }) {
  // A4 DXA: w=11906 h=16838; docx library swaps w/h internally for landscape
  const pageSize = landscape
    ? { width: 11906, height: 16838, orientation: PageOrientation.LANDSCAPE }
    : { width: 11906, height: 16838 };

  return new Document({
    styles: {
      default: {
        document: {
          run: { font: FONT_CJK, size: 20 },
        },
      },
    },
    sections: [{
      properties: {
        page: {
          size: pageSize,
          margin: landscape
            ? { top: 720, right: 1134, bottom: 720, left: 1134 }
            : { top: 1080, right: 1440, bottom: 1080, left: 1440 },
        },
      },
      headers: {
        default: new Header({
          children: [new Paragraph({
            alignment: AlignmentType.RIGHT,
            children: [new TextRun({
              text: headerText || "MAGI 文件",
              font: FONT_CJK,
              size: 16,
              color: "999999",
              italics: true,
            })],
          })],
        }),
      },
      footers: {
        default: new Footer({
          children: [new Paragraph({
            alignment: AlignmentType.CENTER,
            children: [
              new TextRun({ text: "— ", font: FONT_LATIN, size: 16, color: "999999" }),
              new TextRun({ children: [PageNumber.CURRENT], font: FONT_LATIN, size: 16, color: "999999" }),
              new TextRun({ text: " —", font: FONT_LATIN, size: 16, color: "999999" }),
            ],
          })],
        }),
      },
      children,
    }],
  });
}

// ── Main ────────────────────────────────────────────────────────────────────

async function main() {
  const jsonPath = process.argv[2];
  if (!jsonPath) {
    console.error("Usage: node _docx_table_gen.js <data.json>");
    process.exit(1);
  }

  const raw = fs.readFileSync(jsonPath, "utf-8");
  const data = JSON.parse(raw);

  // Sanitize all string fields recursively to remove XML-illegal chars
  function sanitizeObj(obj) {
    if (typeof obj === "string") return sanitizeXml(obj);
    if (Array.isArray(obj)) return obj.map(sanitizeObj);
    if (obj && typeof obj === "object") {
      for (const k of Object.keys(obj)) obj[k] = sanitizeObj(obj[k]);
    }
    return obj;
  }
  sanitizeObj(data);

  let doc;
  switch (data.mode) {
    case "bilingual":
      doc = buildBilingual(data);
      break;
    case "transcript":
      doc = buildTranscript(data);
      break;
    case "summary":
      doc = buildSummary(data);
      break;
    default:
      console.error(`Unknown mode: ${data.mode}`);
      process.exit(1);
  }

  const buffer = await Packer.toBuffer(doc);
  fs.writeFileSync(data.out_path, buffer);
  console.log(`OK: ${data.out_path}`);
}

main().catch(err => {
  console.error(err);
  process.exit(1);
});
