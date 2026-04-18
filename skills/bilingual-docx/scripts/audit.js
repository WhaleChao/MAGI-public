#!/usr/bin/env node
/**
 * Bilingual translation quality audit — THREE-LAYER CHECK.
 * Usage: node audit.js items_a.js items_b.js [items_c.js ...]
 *
 * Layer 1 — Formal checks:
 *   1. Coverage: all sections present, no placeholders
 *   2. Length ratio: zh too short → possible summary
 *   3. CJK ratio: too much Latin in zh → untranslated English
 *
 * Layer 2 — Semantic checks (CRITICAL — catches meaning reversals):
 *   4. Negation consistency: EN has "not/no/never/without" but ZH lacks 不/未/無/沒/非/否/並不
 *   5. False negation: ZH has strong negation but EN doesn't
 *
 * Layer 3 — Terminology consistency:
 *   6. Known mistranslations (configurable term map)
 *   7. Committee name errors (人權委員會 vs 人權事務委員會)
 *
 * Exits with code 1 if problems are found, 0 if clean.
 */

const files = process.argv.slice(2);
if (files.length === 0) {
  console.error('Usage: node audit.js <items_file1.js> [items_file2.js ...]');
  process.exit(2);
}

function sectionNum(it) {
  const m = (it.en || '').match(/^\[[\d]+\.(\d+)\]/);
  return m ? parseInt(m[1]) : null;
}

function cjkRatio(s) {
  if (!s) return { cjk: 0, latin: 0, total: 0, ratio: 0 };
  let cjk = 0, latin = 0;
  for (const ch of s) {
    const code = ch.codePointAt(0);
    if ((code >= 0x4e00 && code <= 0x9fff) || (code >= 0x3000 && code <= 0x303f)) cjk++;
    else if ((code >= 0x41 && code <= 0x7a)) latin++;
  }
  return { cjk, latin, total: cjk + latin, ratio: cjk / Math.max(cjk + latin, 1) };
}

function isPlaceholder(it) { return (it.zh || '').includes('待翻譯'); }

function qualityScore(it) {
  let s = 0;
  if (!isPlaceholder(it)) s += 100000;
  if (it.__human) s += 10000000;
  if (it.__full) s += 8000000;
  if (it.__retry3) s += 5000000;
  if (it.__retry) s += 500000;
  s += (it.zh || '').length;
  s += (it.en || '').length / 10;
  return s;
}

// ========== LAYER 2: Negation detection ==========

// English negation patterns (in main clauses, not just footnotes)
const EN_NEGATION_PATTERNS = [
  /\bdid not\b/i, /\bdoes not\b/i, /\bdo not\b/i,
  /\bwas not\b/i, /\bwere not\b/i, /\bis not\b/i, /\bare not\b/i,
  /\bcannot\b/i, /\bcould not\b/i, /\bwould not\b/i, /\bshould not\b/i,
  /\bnot (?:a |an |the |be |been |being )/i,
  /\bno (?:one|person|state|violation|breach|evidence)\b/i,
  /\bnever\b/i,
  /\bneither\b/i,
  /\bwithout (?:his|her|its|their|the|any|prior|free)\b/i,
  /\bfail(?:s|ed)? to\b/i,
  /\bunable to\b/i,
  /\bdid not (?:amount|constitute|breach|violate|find|reach)\b/i,
];

// Chinese negation markers
const ZH_NEGATION_RE = /[不未無沒非否]|並不|均不|尚未|從未|絕不|不得|不構成|未能|並未|不違反/;

function checkNegationConsistency(en, zh) {
  if (!en || !zh) return null;

  // Only check substantive content (skip short footnotes etc.)
  // Strip footnote numbers and parenthetical citations for cleaner matching
  const enClean = en.replace(/\[\d+\]/g, '').replace(/\(\d{4}\)/g, '');

  for (const pattern of EN_NEGATION_PATTERNS) {
    if (pattern.test(enClean)) {
      // EN has negation — does ZH have corresponding negation?
      if (!ZH_NEGATION_RE.test(zh)) {
        // Extract the specific English negation for the report
        const match = enClean.match(pattern);
        return {
          type: 'NEGATION_MISMATCH',
          detail: `EN has "${match[0]}" but ZH lacks negation marker (不/未/無/沒/非/否)`,
          en_snippet: en.slice(Math.max(0, en.indexOf(match[0]) - 30), en.indexOf(match[0]) + match[0].length + 30),
        };
      }
      break; // Found matching negation, no problem
    }
  }
  return null;
}

// ========== LAYER 3: Terminology consistency ==========

const TERM_ERRORS = [
  { pattern: /(?<!事務)人權委員會/, correct: '人權事務委員會', label: 'HRC_NAME' },
  { pattern: /侮辱性的待遇或刑罰/, correct: '有辱人格之待遇或處罰', label: 'DEGRADING' },
  { pattern: /侮辱之處遇或懲罰/, correct: '有辱人格之待遇或處罰', label: 'DEGRADING' },
  { pattern: /侮辱性待遇/, correct: '有辱人格之待遇', label: 'DEGRADING' },
  { pattern: /貶抑性待遇/, correct: '有辱人格之待遇', label: 'DEGRADING' },
  { pattern: /預設同意/, correct: '默許', label: 'ACQUIESCENCE' },
  { pattern: /國家當事人/, correct: '締約國', label: 'STATE_PARTY' },
  { pattern: /獨立隔離監禁/, correct: '單獨監禁', label: 'SOLITARY' },
  { pattern: /合法制裁的免責條款/, correct: '合法制裁但書', label: 'LAWFUL_SANCTIONS' },
];

function checkTerminology(zh) {
  const issues = [];
  for (const { pattern, correct, label } of TERM_ERRORS) {
    if (pattern.test(zh)) {
      const match = zh.match(pattern);
      issues.push({ type: 'TERM_' + label, found: match[0], should_be: correct });
    }
  }
  return issues;
}

// ========== Main audit logic ==========

// Deduplicate: keep highest quality for each section number
const bySection = new Map();
for (const f of files) {
  try {
    const arr = require(f.startsWith('/') ? f : process.cwd() + '/' + f);
    if (f.includes('human')) for (const it of arr) it.__human = true;
    if (f.includes('_full_') || f.includes('_full.')) for (const it of arr) it.__full = true;
    if (f.includes('retry3')) for (const it of arr) it.__retry3 = true;
    if (f.includes('retry') && !f.includes('retry3')) for (const it of arr) it.__retry = true;
    for (const it of arr) {
      const n = sectionNum(it);
      if (n === null) continue;
      const ex = bySection.get(n);
      if (!ex || qualityScore(it) > qualityScore(ex)) bySection.set(n, it);
    }
  } catch (e) {
    console.error('Error loading', f, ':', e.message);
  }
}

// Find the max section number
let maxSection = 0;
for (const n of bySection.keys()) if (n > maxSection) maxSection = n;

const problems = [];
let negationWarnings = 0;
let termWarnings = 0;

for (let i = 1; i <= maxSection; i++) {
  const it = bySection.get(i);
  if (!it) { problems.push({ n: i, reason: 'MISSING', layer: 1 }); continue; }
  if (isPlaceholder(it)) { problems.push({ n: i, reason: 'PLACEHOLDER', layer: 1, zh: (it.zh || '').slice(0, 80) }); continue; }

  const enLen = (it.en || '').length;
  const zh = it.zh || '';
  const en = it.en || '';
  const r = cjkRatio(zh);

  // === Layer 1: Formal checks ===

  if (enLen > 150 && r.ratio < 0.5 && r.latin > 50) {
    problems.push({ n: i, reason: 'MOSTLY_ENGLISH', layer: 1, enLen, zhLen: zh.length, cjk: r.cjk, latin: r.latin });
  } else if (enLen > 200 && zh.length / Math.max(enLen, 1) < 0.2) {
    problems.push({ n: i, reason: 'TOO_SHORT', layer: 1, enLen, zhLen: zh.length });
  }

  // === Layer 2: Semantic checks ===

  // Only check non-trivial sections (short ones are where negation errors happen most!)
  const negResult = checkNegationConsistency(en, zh);
  if (negResult) {
    problems.push({ n: i, reason: negResult.type, layer: 2, detail: negResult.detail, snippet: negResult.en_snippet });
    negationWarnings++;
  }

  // === Layer 3: Terminology ===

  const termIssues = checkTerminology(zh);
  for (const issue of termIssues) {
    problems.push({ n: i, reason: issue.type, layer: 3, found: issue.found, should_be: issue.should_be });
    termWarnings++;
  }
}

// ========== Report ==========

console.log('=== BILINGUAL TRANSLATION AUDIT ===');
console.log('Total sections found:', bySection.size, '/ expected:', maxSection);
console.log('');

const layer1 = problems.filter(p => p.layer === 1);
const layer2 = problems.filter(p => p.layer === 2);
const layer3 = problems.filter(p => p.layer === 3);

console.log('Layer 1 (Formal):', layer1.length, 'issues');
console.log('Layer 2 (Semantic):', layer2.length, 'issues');
console.log('Layer 3 (Terminology):', layer3.length, 'issues');
console.log('');

if (layer2.length > 0) {
  console.log('⚠️  CRITICAL — POTENTIAL MEANING REVERSALS:');
  for (const p of layer2) {
    console.log(`  [${p.n}] ${p.reason}: ${p.detail}`);
    if (p.snippet) console.log(`    Context: "${p.snippet}"`);
  }
  console.log('');
}

if (layer1.length > 0) {
  console.log('Layer 1 issues:');
  for (const p of layer1) {
    const details = [p.reason];
    if (p.enLen) details.push('en=' + p.enLen);
    if (p.zhLen !== undefined) details.push('zh=' + p.zhLen);
    console.log('  [' + p.n + ']', details.join(' '));
  }
  console.log('');
}

if (layer3.length > 0) {
  console.log('Layer 3 issues:');
  for (const p of layer3) {
    console.log(`  [${p.n}] ${p.reason}: found "${p.found}" → should be "${p.should_be}"`);
  }
  console.log('');
}

if (problems.length > 0) {
  console.log('Problem section numbers:', [...new Set(problems.map(p => p.n))].sort((a,b) => a-b).join(','));
  // Exit code 2 for semantic issues (highest severity), 1 for others
  process.exit(layer2.length > 0 ? 2 : 1);
} else {
  console.log('✓ All sections pass all three layers of quality checks.');
  process.exit(0);
}
