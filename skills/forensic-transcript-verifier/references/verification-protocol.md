# Court Recording Verification Protocol

## Contents

1. Evidence hierarchy
2. Time and speaker model
3. Two-pass method
4. Deterministic gates
5. Word output requirements
6. Failure rules
7. MAGI autonomous agent contract

## 1. Evidence hierarchy

Use this order:

1. Source recording for whether speech occurred, playback timing, and observable speaker activity.
2. User-designated manually corrected excerpt for exact text inside that excerpt.
3. Existing full transcript as a draft, not presumed truth.
4. Timestamped ASR as a search and omission-detection aid, never as final proof.

For autonomous court-grade work, use two independently generated ASR sources: the original audio and a separately filtered mono 16 kHz extraction. Agreement improves confidence; disagreement creates a review item.

When recording and baseline wording appear inconsistent, preserve the baseline inside its declared scope and record the discrepancy for human review. Do not silently rewrite the baseline.

## 2. Time and speaker model

Represent each turn as:

```text
start, end, speaker, text, source, confidence
```

### Speaker boundaries

- Split at every actual speaker change.
- Merge only consecutive same-speaker turns.
- A short interruption belongs to the interrupter, even when surrounded by a long answer.
- Quoted speech narrated by the current speaker remains in the narrator's turn; do not create a fictitious live speaker.
- When a segment contains inseparable rapid alternation, label both speakers and mark the content unresolved instead of assigning the whole interval to one person.

### Speaker evidence

Use at least two of the following when possible:

- Visible mouth, mask, head, or body movement synchronized with speech.
- Stable voice characteristics.
- Microphone direction or room location.
- Question/answer grammar and legal role.
- Continuity with the preceding and following turn.

Dialogue grammar alone is insufficient when the audio or picture conflicts.

### Time blocks

A document can contain an excerpt whose displayed timestamps use another clock. Treat a backward timestamp jump as a new monotonic block. Check overlaps inside each block, and separately store the excerpt-to-video mapping used for coverage.

## 3. Two-pass method

### Pass 1: evidence-first

Review recording chronology and correct the working transcript. Focus on:

- Missing utterances and silent gaps containing speech.
- Wrong speaker attribution.
- Overlapping speaker ranges.
- Long respondent ranges that contain prosecutor/judge interjections.
- Breaks, procedural talk, and resumption language.
- Names, amounts, dates, and legally significant verbs.

Use ASR to locate candidates, then verify against the recording. Retain filler when it changes interaction, attribution, agreement, denial, timing, or procedure.

### Pass 2: output-first

Discard the working assumption that Pass 1 is correct. Read the generated DOCX back and rerun every deterministic gate. Review flagged timestamps from the recording again. Confirm the actual output, not merely the generator source.

## 4. Deterministic gates

### Baseline gate

- Extract the baseline with tracked changes accepted.
- Parse every `[HH:MM:SS(.ff)] speaker` entry.
- Check entry count, timestamp presence, speaker presence, and exact text presence.
- Permit adjacent same-speaker entries to appear as a merged range only if both original timestamps and both exact text strings remain present.

### Overlap gate

- Split the transcript into monotonic time blocks.
- Flag `next.start < current.end - tolerance`.
- Review any overlap across different speakers; do not dismiss it as semantic merging.

### Coverage gate

- Select nontrivial ASR segments above configured confidence and below no-speech thresholds.
- Mark a segment covered only when its full interval falls inside a transcript interval with small padding.
- Add the mapped source-video interval for a baseline excerpt that uses another clock.
- Group adjacent uncovered segments for manual review.

### Speaker-review gate

Flag question-like ASR segments (`嗎`, `呢`, `誰`, `為什麼`, `然後呢`, question mark) that fall wholly inside answer-only speaker ranges. Review each result; some are genuine clarification questions or narrated quotations.

### Output gate

- Every audit header must exist in the DOCX-extracted text.
- The two automated readback/audit passes must independently re-extract the DOCX and produce identical gate evidence.
- DOCX schema validation must pass.
- Rendered PDF must have text on every expected page.
- Inspect representative pages visually.

## 5. Word output requirements

- Create a new, clearly named court-review DOCX.
- Use A4, readable Traditional Chinese fonts, stable margins, header/footer, and page numbers.
- Explain that video and excerpt clocks differ when applicable.
- Visually distinguish uncertainty markers without hiding them.
- Include an unresolved-items table listing time, speaker, uncertain content, and reason.
- Keep the manually corrected excerpt verbatim.

## 6. Failure rules

- If the recording cannot be decoded, stop and report the media failure.
- If the baseline cannot be extracted, do not claim an exact match.
- If speaker identity cannot be confirmed, mark it unresolved.
- If an ASR engine is unavailable, continue manual verification but state that the automated coverage gate was not run.
- If rendering fails because of fonts, configure a CJK-capable font environment and rerun.
- If validation fails, do not deliver the candidate as final.
- Always require a qualified human to review the final legal work product before filing.

## 7. MAGI autonomous agent contract

- Use local MAGI inference only. A result routed through Codex/OpenAI does not count as autonomous evidence.
- A court-facing run must require the independent filtered-audio ASR. Disabling `require_secondary_asr` is a smoke-test-only relaxation and fails the court-grade contract.
- Sample sequential frames around the utterance, not one isolated still.
- Cover every non-baseline transcript turn; high-risk findings add review points and never replace full-timeline coverage.
- Sweep long turns at eight-second intervals. A whole-turn speaker relabel requires unanimous accepted points across the turn; disagreement becomes an unresolved split/review item.
- Use different temporal radii and different reasoning order for the two visual passes.
- Require both visual passes to agree before changing a speaker label.
- Require two text-review passes and both ASR sources to support an exact same correction before changing substantive text.
- Lock the manually corrected baseline block against model rewriting.
- Keep all rejected or conflicting proposals in machine-readable reports and the Word unresolved-items table.
- Generate a new DOCX; never overwrite the source transcript.
- Rerun baseline, overlap, ASR coverage, speaker-review, OOXML, PDF render, and readback-consistency gates against the generated DOCX.
- Treat a truncated review plan as a failed court-grade run, even if every sampled point passed.
