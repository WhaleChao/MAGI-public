#!/usr/bin/env node
/**
 * Bilingual DOCX builder template.
 *
 * Usage: Adapt this template for each project. The key steps are:
 *   1. Import all item files (chunks, fills, retries, human reference)
 *   2. Deduplicate by section number using qualityScore
 *   3. Render to a landscape A4 two-column Word table
 *
 * Dependencies: npm install -g docx
 *
 * Customization points (marked with TODO):
 *   - File imports (add/remove item files)
 *   - Chapter/section prefix regex
 *   - Title, author, citation info
 *   - Output filename
 */

const fs = require('fs');
const { Document, Packer, Paragraph, TextRun, AlignmentType, PageOrientation,
  Table, TableRow, TableCell, WidthType, BorderStyle, ShadingType, VerticalAlign } = require('docx');

// TODO: Import your item files here
// const chunkA = require('./items_a.js');
// const chunkB = require('./items_b.js');
// const human = require('./items_human.js');
// for (const h of human) h.__human = true;

// TODO: Set the section number regex for your document
// e.g., /^\[9\.(\d+)\]/ for chapter 9, /^\[(\d+)\]/ for simple numbering
function sectionNum(it) {
  const m = (it.en || '').match(/^\[9\.(\d+)\]/);
  return m ? parseInt(m[1]) : null;
}

function isPlaceholder(it) { return (it.zh || '').includes('待翻譯'); }

function qualityScore(it) {
  let s = 0;
  if (!isPlaceholder(it)) s += 100000;
  if (it.__human) s += 10000000;   // user's translation always wins
  if (it.__retry3) s += 5000000;   // CJK audit retranslation
  if (it.__retry) s += 500000;     // first-round retries
  s += (it.zh || '').length;
  s += (it.en || '').length / 10;
  return s;
}

// Deduplicate
const bySection = new Map();
const headings = [];
// TODO: Add your arrays here
const allArrays = [/* chunkA, chunkB, human */];
for (const arr of allArrays) {
  for (const it of arr) {
    const n = sectionNum(it);
    if (n === null) { headings.push(it); continue; }
    const ex = bySection.get(n);
    if (!ex || qualityScore(it) > qualityScore(ex)) bySection.set(n, it);
  }
}

// Build items list
const items = [];

// TODO: Add your document header items
items.push({ type: 'title',
  en: 'Document Title',
  zh: '文件標題' });

// Add sections in order
const sectionNums = [...bySection.keys()].sort((a, b) => a - b);
for (const n of sectionNums) {
  items.push(bySection.get(n));
}

// TODO: Set total expected sections
const TOTAL_SECTIONS = 192;
const missingSections = [];
const placeholderSections = [];
for (let i = 1; i <= TOTAL_SECTIONS; i++) {
  if (!bySection.has(i)) missingSections.push(i);
  else if (isPlaceholder(bySection.get(i))) placeholderSections.push(i);
}
console.log('Sections covered:', bySection.size, '/', TOTAL_SECTIONS);
if (missingSections.length) console.log('Missing:', missingSections.join(','));
if (placeholderSections.length) console.log('Placeholder-only:', placeholderSections.join(','));

// ========== DOCX Rendering ==========

const enFont = 'Times New Roman';
const zhFont = { name: 'Times New Roman', eastAsia: 'PMingLiU' };
const border = { style: BorderStyle.SINGLE, size: 4, color: 'BBBBBB' };
const cellBorders = { top: border, bottom: border, left: border, right: border };
const cellMargins = { top: 100, bottom: 100, left: 140, right: 140 };
const COL = 7600;

function parseInline(text, { baseBold = false, baseItalics = false, size = 22, lang = 'en' } = {}) {
  const font = lang === 'zh' ? zhFont : enFont;
  const runs = [];
  const re = /\*\*([^*]+)\*\*|\*([^*]+)\*/g;
  let last = 0, m;
  while ((m = re.exec(text)) !== null) {
    if (m.index > last) runs.push(new TextRun({ text: text.slice(last, m.index), bold: baseBold, italics: baseItalics, size, font }));
    if (m[1] !== undefined) runs.push(new TextRun({ text: m[1], bold: true, italics: baseItalics, size, font }));
    else runs.push(new TextRun({ text: m[2], bold: baseBold, italics: true, size, font }));
    last = re.lastIndex;
  }
  if (last < text.length) runs.push(new TextRun({ text: text.slice(last), bold: baseBold, italics: baseItalics, size, font }));
  if (runs.length === 0) runs.push(new TextRun({ text: '', bold: baseBold, italics: baseItalics, size, font }));
  return runs;
}

function p(text, { bold = false, italics = false, size = 22, align, indentLeft = 0, lang = 'en', spacingAfter = 80, lineSpacing = 300 } = {}) {
  return new Paragraph({
    alignment: align,
    indent: indentLeft ? { left: indentLeft } : undefined,
    spacing: { after: spacingAfter, line: lineSpacing },
    children: parseInline(text || '', { baseBold: bold, baseItalics: italics, size, lang }),
  });
}

function cell(paragraphs, { shading } = {}) {
  return new TableCell({
    width: { size: COL, type: WidthType.DXA },
    borders: cellBorders, margins: cellMargins, verticalAlign: VerticalAlign.TOP,
    shading: shading ? { fill: shading, type: ShadingType.CLEAR, color: 'auto' } : undefined,
    children: paragraphs,
  });
}

function row(left, right, opts = {}) {
  return new TableRow({ children: [cell(left, opts), cell(right, opts)] });
}

function fullRow(paragraphs, opts = {}) {
  return new TableRow({
    children: [new TableCell({
      width: { size: COL * 2, type: WidthType.DXA }, columnSpan: 2,
      borders: cellBorders, margins: cellMargins,
      shading: opts.shading ? { fill: opts.shading, type: ShadingType.CLEAR, color: 'auto' } : undefined,
      children: paragraphs,
    })],
  });
}

// Style map for each item type
const styleMap = {
  title: (it) => fullRow([
    p(it.en, { bold: true, size: 28, align: AlignmentType.CENTER, spacingAfter: 60 }),
    p(it.zh, { bold: true, size: 26, align: AlignmentType.CENTER, spacingAfter: 0, lang: 'zh' }),
  ], { shading: 'EAF3FB' }),
  authors: (it) => row(
    [p(it.en, { size: 22, align: AlignmentType.CENTER, spacingAfter: 0 })],
    [p(it.zh, { size: 20, align: AlignmentType.CENTER, spacingAfter: 0, lang: 'zh' })],
  ),
  cite: (it) => row(
    [p(it.en, { italics: true, size: 22, align: AlignmentType.CENTER, spacingAfter: 0 })],
    [p(it.zh, { italics: true, size: 20, align: AlignmentType.CENTER, spacingAfter: 0, lang: 'zh' })],
  ),
  h1: (it) => row(
    [p(it.en, { bold: true, size: 32, spacingAfter: 0 })],
    [p(it.zh, { bold: true, size: 30, spacingAfter: 0, lang: 'zh' })],
    { shading: 'D5E8F0' },
  ),
  h2: (it) => row(
    [p(it.en, { bold: true, size: 28, spacingAfter: 0 })],
    [p(it.zh, { bold: true, size: 26, spacingAfter: 0, lang: 'zh' })],
    { shading: 'EAF3FB' },
  ),
  h3: (it) => row(
    [p(it.en, { bold: true, size: 25, spacingAfter: 0 })],
    [p(it.zh, { bold: true, size: 23, spacingAfter: 0, lang: 'zh' })],
    { shading: 'F4F8FB' },
  ),
  p: (it) => row(
    [p(it.en, { size: 24, spacingAfter: 0, lineSpacing: 320 })],
    [p(it.zh, { size: 22, spacingAfter: 0, lineSpacing: 340, lang: 'zh' })],
  ),
  quote: (it) => row(
    [p(it.en, { italics: true, size: 22, spacingAfter: 0, indentLeft: 360 })],
    [p(it.zh, { italics: true, size: 21, spacingAfter: 0, indentLeft: 360, lang: 'zh' })],
  ),
  note: (it) => it.zh ? fullRow([p(it.zh, { italics: true, size: 20, spacingAfter: 0, lang: 'zh' })], { shading: 'FFF4E0' }) : null,
};

// Build table rows
const rows = [];
rows.push(new TableRow({
  tableHeader: true,
  children: [
    cell([p('English (Original)', { bold: true, size: 22, align: AlignmentType.CENTER, spacingAfter: 0 })], { shading: 'D5E8F0' }),
    cell([p('繁體中文（翻譯）', { bold: true, size: 22, align: AlignmentType.CENTER, spacingAfter: 0, lang: 'zh' })], { shading: 'D5E8F0' }),
  ],
}));

for (const it of items) {
  const renderer = styleMap[it.type];
  if (renderer) {
    const r = renderer(it);
    if (r) rows.push(r);
  }
}

const table = new Table({
  width: { size: COL * 2, type: WidthType.DXA },
  columnWidths: [COL, COL], rows,
});

const doc = new Document({
  styles: { default: { document: { run: { font: 'Times New Roman', size: 22 } } } },
  sections: [{
    properties: {
      page: {
        size: { width: 11906, height: 16838, orientation: PageOrientation.LANDSCAPE },
        margin: { top: 720, right: 720, bottom: 720, left: 720 },
      },
    },
    children: [
      new Paragraph({
        alignment: AlignmentType.CENTER,
        spacing: { after: 120 },
        children: [new TextRun({ text: '中英對照表（左：英文原文｜右：繁體中文翻譯）', bold: true, size: 22, font: zhFont })],
      }),
      table,
    ],
  }],
});

// TODO: Set output path
const OUTPUT = process.env.OUTPUT_PATH || 'Bilingual_中英對照.docx';
Packer.toBuffer(doc).then(buf => {
  fs.writeFileSync(OUTPUT, buf);
  console.log('Wrote', OUTPUT, buf.length, 'bytes');
  console.log('Total items:', items.length);
});
