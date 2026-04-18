#!/usr/bin/env node
/**
 * Parse a user-provided reference translation file (.md or .txt) into items JS format.
 *
 * Usage: node parse_reference.js <reference.md> <output.js> [--prefix "9"]
 *
 * The parser looks for paired English/Chinese sections delimited by section markers
 * like [9.01], [9.02], etc. It extracts type (h1/h2/h3/p/quote) from context clues.
 *
 * This is a starting template — adjust the regex patterns to match the specific
 * reference file's formatting conventions.
 */

const fs = require('fs');

const args = process.argv.slice(2);
if (args.length < 2) {
  console.error('Usage: node parse_reference.js <reference.md> <output.js> [--prefix "9"]');
  process.exit(1);
}

const inputFile = args[0];
const outputFile = args[1];
let prefix = '9';
const prefixIdx = args.indexOf('--prefix');
if (prefixIdx >= 0 && args[prefixIdx + 1]) prefix = args[prefixIdx + 1];

const content = fs.readFileSync(inputFile, 'utf8');

// Split by section markers
const sectionRegex = new RegExp(`\\[${prefix}\\.(\\d+)\\]`, 'g');
const markers = [];
let match;
while ((match = sectionRegex.exec(content)) !== null) {
  markers.push({ num: parseInt(match[1]), index: match.index });
}

console.log('Found', markers.length, 'section markers');

// For each section, try to extract en and zh pairs
// This is highly dependent on the reference file's format.
// The basic heuristic: the reference file often has en text followed by zh text
// with clear language switching patterns.

const items = [];
for (let i = 0; i < markers.length; i++) {
  const start = markers[i].index;
  const end = i + 1 < markers.length ? markers[i + 1].index : content.length;
  const section = content.slice(start, end).trim();

  // Detect type from formatting
  let type = 'p';
  if (section.match(/^#+\s/) || section.match(/\*\*[A-Z][^*]+\*\*/)) type = 'h3';

  // The reference file structure varies; this is a minimal extractor.
  // For complex files, you may need to customize this section.
  items.push({
    type,
    en: section, // Will need manual or AI-assisted separation of en/zh
    zh: '',       // To be filled
    _num: markers[i].num,
  });
}

// Write output
const output = 'module.exports = ' + JSON.stringify(items, null, 2) + ';\n';
fs.writeFileSync(outputFile, output);
console.log('Wrote', items.length, 'items to', outputFile);
console.log('NOTE: This is a rough extraction. Review and edit the en/zh split manually or with AI assistance.');
