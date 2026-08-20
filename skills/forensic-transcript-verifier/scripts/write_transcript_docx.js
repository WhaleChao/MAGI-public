#!/usr/bin/env node
"use strict";

const fs = require("fs");
const path = require("path");
const {
  AlignmentType,
  BorderStyle,
  Document,
  Footer,
  Header,
  PageBreak,
  PageNumber,
  Packer,
  Paragraph,
  ShadingType,
  Table,
  TableCell,
  TableRow,
  TextRun,
  WidthType,
} = require("docx");

const FONT = "Noto Sans CJK TC";
const A4_WIDTH = 11906;
const A4_HEIGHT = 16838;
const CONTENT_WIDTH = 9026;
const BORDER = { style: BorderStyle.SINGLE, size: 1, color: "B8C2CC" };
const BORDERS = { top: BORDER, bottom: BORDER, left: BORDER, right: BORDER };

function clean(value) {
  return String(value || "").replace(/[\x00-\x08\x0B\x0C\x0E-\x1F]/g, "");
}

function textRun(text, options = {}) {
  return new TextRun({ text: clean(text), font: FONT, size: 22, ...options });
}

function cell(text, width, header = false) {
  return new TableCell({
    borders: BORDERS,
    width: { size: width, type: WidthType.DXA },
    shading: header ? { fill: "D9EAF7", type: ShadingType.CLEAR } : undefined,
    margins: { top: 90, bottom: 90, left: 120, right: 120 },
    children: [new Paragraph({ children: [textRun(text, { bold: header, size: 19 })] })],
  });
}

function unresolvedTable(rows) {
  const widths = [1500, 1400, 2500, 3626];
  const output = [new TableRow({
    tableHeader: true,
    children: [
      cell("時間", widths[0], true),
      cell("發話者", widths[1], true),
      cell("未決內容", widths[2], true),
      cell("原因／所需確認", widths[3], true),
    ],
  })];
  for (const row of rows || []) {
    output.push(new TableRow({ children: [
      cell(row.time, widths[0]),
      cell(row.speaker, widths[1]),
      cell(row.content, widths[2]),
      cell(row.reason, widths[3]),
    ] }));
  }
  return new Table({ width: { size: CONTENT_WIDTH, type: WidthType.DXA }, columnWidths: widths, rows: output });
}

async function main() {
  const taskPath = process.argv[2];
  if (!taskPath) throw new Error("usage: write_transcript_docx.js task.json");
  const data = JSON.parse(fs.readFileSync(taskPath, "utf8"));
  const outputPath = path.resolve(String(data.output_path || ""));
  if (!outputPath.toLowerCase().endsWith(".docx")) throw new Error("output_path must be .docx");
  fs.mkdirSync(path.dirname(outputPath), { recursive: true });

  const children = [
    new Paragraph({
      alignment: AlignmentType.CENTER,
      spacing: { after: 260 },
      children: [textRun(data.title || "訊問影音完整譯文（重新勘驗校正版）", { bold: true, size: 34 })],
    }),
    new Paragraph({
      spacing: { after: 140 },
      children: [textRun(data.case_info || "", { size: 20, color: "4B5563" })],
    }),
    new Paragraph({
      spacing: { after: 260 },
      children: [textRun(
        "說明：本譯文由 MAGI 依影音、時間戳 ASR、畫面序列及人工校正節文進行兩次獨立勘驗。人工校正節文於其範圍內逐字保留；不確定內容以【】明示。",
        { size: 20 }
      )],
    }),
  ];

  for (const turn of data.turns || []) {
    children.push(new Paragraph({
      keepNext: true,
      spacing: { before: 150, after: 60 },
      children: [
        textRun(`[${clean(turn.display)}] `, { bold: true, color: "1F4E79", size: 20 }),
        textRun(turn.speaker || "【發話者未定】", { bold: true, size: 20 }),
      ],
    }));
    const uncertain = /【[^】]*(?:聽辨|發話者|未定)[^】]*】/.test(clean(turn.text));
    children.push(new Paragraph({
      spacing: { after: 80, line: 360 },
      children: [textRun(`「${clean(turn.text)}」`, { color: uncertain ? "9C0006" : "111827" })],
    }));
  }

  children.push(new Paragraph({ children: [new PageBreak()] }));
  children.push(new Paragraph({
    spacing: { after: 180 },
    children: [textRun("未決事項與人工最終確認清單", { bold: true, size: 28 })],
  }));
  if ((data.unresolved || []).length) {
    children.push(unresolvedTable(data.unresolved));
  } else {
    children.push(new Paragraph({ children: [textRun("本次兩輪技術複核未留下未決項目；法院送件前仍須由承辦人確認。", { size: 20 })] }));
  }

  const doc = new Document({
    styles: {
      default: { document: { run: { font: FONT, size: 22 } } },
    },
    sections: [{
      properties: {
        page: {
          size: { width: A4_WIDTH, height: A4_HEIGHT },
          margin: { top: 1080, right: 1440, bottom: 1080, left: 1440 },
        },
      },
      headers: { default: new Header({ children: [new Paragraph({
        alignment: AlignmentType.RIGHT,
        children: [textRun(data.header || data.title || "法院影音勘驗譯文", { size: 16, color: "6B7280" })],
      })] }) },
      footers: { default: new Footer({ children: [new Paragraph({
        alignment: AlignmentType.CENTER,
        children: [textRun("— ", { size: 16 }), new TextRun({ children: [PageNumber.CURRENT], font: FONT, size: 16 }), textRun(" —", { size: 16 })],
      })] }) },
      children,
    }],
  });

  const buffer = await Packer.toBuffer(doc);
  const temporary = `${outputPath}.${process.pid}.tmp`;
  fs.writeFileSync(temporary, buffer);
  fs.renameSync(temporary, outputPath);
  process.stdout.write(JSON.stringify({ success: true, output: outputPath, turns: (data.turns || []).length }));
}

main().catch((error) => {
  process.stderr.write(String(error && error.stack ? error.stack : error));
  process.exit(1);
});
