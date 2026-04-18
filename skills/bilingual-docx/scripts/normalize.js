#!/usr/bin/env node
/**
 * Terminology normalization for bilingual translation.
 *
 * Purpose: Parallel translation by multiple subagents produces inconsistent
 * terminology. This script applies a configurable term map to unify all zh text
 * before building the final docx. It also standardizes case name formatting.
 *
 * Usage:
 *   As a CLI tool (preview mode):
 *     node normalize.js --preview items_a.js items_b.js ...
 *
 *   As a module (in build.js):
 *     const { normalizeZh, enrichCaseHeading } = require('./normalize.js');
 *     for (const it of items) {
 *       if (it.zh) it.zh = normalizeZh(it.zh);
 *       enrichCaseHeading(it); // adds original EN case name to zh parenthetical
 *     }
 */

// ========== CONFIGURABLE TERMINOLOGY MAP ==========
// Edit this table for each new project domain.
// Format: [pattern, replacement]
// Patterns are applied in order; put more specific patterns before general ones.

const TERMINOLOGY_MAP = [
  // Committee names
  // Negative lookbehind prevents 人權事務委員會 → 人權事務事務委員會
  [/(?<!事務)人權委員會/g, '人權事務委員會'],

  // degrading treatment — unify all variants
  [/侮辱性的待遇或刑罰/g, '有辱人格之待遇或處罰'],
  [/侮辱之處遇或懲罰/g, '有辱人格之待遇或處罰'],
  [/侮辱性待遇/g, '有辱人格之待遇'],
  [/貶抑性待遇/g, '有辱人格之待遇'],
  [/侮辱性的待遇/g, '有辱人格之待遇'],

  // acquiescence — all variants → 默許
  [/預設同意（acquiescence）/g, '默許（acquiescence）'],
  [/預設同意/g, '默許'],
  [/默認（acquiescence）/g, '默許（acquiescence）'],
  [/默認的形式/g, '默許的形式'],
  [/默認/g, '默許'],

  // State party
  [/國家當事人/g, '締約國'],

  // ill-treatment
  [/不當待遇（ill-treatment）/g, '虐待（ill-treatment）'],
  [/不當待遇/g, '虐待'],

  // solitary confinement
  [/獨立隔離監禁（solitary confinement）/g, '單獨監禁（solitary confinement）'],
  [/獨立隔離監禁/g, '單獨監禁'],
  [/隔離監禁（solitary confinement）/g, '單獨監禁（solitary confinement）'],

  // detention incommunicado
  [/禁止與外界接觸之拘留（detention incommunicado）/g, '與外界隔絕之拘禁（detention incommunicado）'],
  [/禁止與外界接觸之拘留/g, '與外界隔絕之拘禁'],
  [/隔絕羈押（detention incommunicado）/g, '與外界隔絕之拘禁（detention incommunicado）'],

  // lawful sanctions
  [/合法制裁的免責條款/g, '合法制裁但書'],
  [/合法制裁附加條款/g, '合法制裁但書'],
];

// ========== CASE NAME NORMALIZATION ==========

/**
 * Convert ALL-CAPS case names to Title Case in zh text.
 * e.g., "ALZERY v SWEDEN" → "Alzery v Sweden"
 */
function normalizeCaseNames(text) {
  return text.replace(
    /\b([A-Z]{2,}(?:\s+[A-Z]{2,})*)\s+(?:v|V)\s+([A-Z]{2,}(?:\s+[A-Z]{2,})*)\b/g,
    (match, name, country) => {
      const toTitle = s => s.split(/\s+/).map(w =>
        w.charAt(0).toUpperCase() + w.slice(1).toLowerCase()
      ).join(' ');
      return toTitle(name) + ' v ' + toTitle(country);
    }
  );
}

/**
 * Apply all terminology normalizations to a zh text string.
 */
function normalizeZh(text) {
  if (!text) return text;
  let result = text;
  for (const [pattern, replacement] of TERMINOLOGY_MAP) {
    result = result.replace(pattern, replacement);
  }
  result = normalizeCaseNames(result);
  return result;
}

/**
 * Enrich h3 case headings: add original English case name to ZH parenthetical.
 * Transforms: Name 訴 Country案（case#） → Name訴Country案（ORIG v COUNTRY, case#）
 */
function enrichCaseHeading(it) {
  if (it.type !== 'h3' || !it.en || !it.zh) return;
  const enMatch = it.en.match(/^\[[\d]+\.\d+\]\s+(.+?)\s+v\s+(.+?)\s*\((.+?)\)\s*$/i);
  if (!enMatch) return;
  const origName = enMatch[1].trim();
  const origCountry = enMatch[2].trim();
  const caseNum = enMatch[3].trim();
  const origFull = origName + ' v ' + origCountry;
  // Don't duplicate if already present
  if (it.zh.includes(origFull) || it.zh.includes(origName.toUpperCase())) return;
  const zhParen = new RegExp('（' + caseNum.replace(/[.*+?^${}()|[\]\\]/g, '\\$&') + '）');
  if (zhParen.test(it.zh)) {
    it.zh = it.zh.replace(zhParen, '（' + origFull + ', ' + caseNum + '）');
  }
}

// ========== CLI MODE ==========

if (require.main === module) {
  const args = process.argv.slice(2);
  const previewMode = args.includes('--preview');
  const itemFiles = args.filter(a => a !== '--preview');

  if (itemFiles.length === 0) {
    console.error('Usage: node normalize.js [--preview] <items_file1.js> [items_file2.js ...]');
    process.exit(2);
  }

  let totalChanges = 0;

  for (const f of itemFiles) {
    try {
      const arr = require(f.startsWith('/') ? f : process.cwd() + '/' + f);
      for (const it of arr) {
        const origZh = it.zh || '';
        const normalized = normalizeZh(origZh);
        if (normalized !== origZh) {
          totalChanges++;
          if (previewMode) {
            const n = (it.en || '').match(/^\[[\d]+\.(\d+)\]/);
            const secLabel = n ? `[${n[0]}]` : '?';
            // Show first diff
            for (let i = 0; i < Math.min(origZh.length, normalized.length); i++) {
              if (origZh[i] !== normalized[i]) {
                const ctx = 20;
                console.log(`${secLabel} in ${f}:`);
                console.log(`  BEFORE: ...${origZh.slice(Math.max(0, i-ctx), i+ctx+20)}...`);
                console.log(`  AFTER:  ...${normalized.slice(Math.max(0, i-ctx), i+ctx+20)}...`);
                break;
              }
            }
          }
        }
      }
    } catch (e) {
      console.error('Error loading', f, ':', e.message);
    }
  }

  console.log(`\nTotal zh fields that would be modified: ${totalChanges}`);
  if (previewMode) {
    console.log('(Preview mode — no files were modified. Remove --preview to apply in build.)');
  }
}

// ========== MODULE EXPORTS ==========

module.exports = { normalizeZh, enrichCaseHeading, normalizeCaseNames, TERMINOLOGY_MAP };
