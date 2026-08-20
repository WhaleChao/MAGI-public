---
name: forensic-transcript-verifier
description: Autonomously operate and verify Taiwanese court or prosecutor hearing video/audio against a full DOCX transcript and a manually corrected excerpt; use local visual models, dual ASR, two independent observation passes, speaker attribution, omission detection, semantic same-speaker merging, exact baseline locking, uncertainty markers, and court-ready Word generation. Use for 勘驗影像、訊問筆錄、完整譯文、逐字稿校正、發話者辨識、人工校正版節文、法院提交前複核、MP4/MP3/WAV plus DOCX transcript comparison, or requests requiring MAGI to watch/listen/correct without Codex assistance.
---

# Forensic Transcript Verifier

Treat the recording as primary evidence and a user-identified manually corrected excerpt as the controlling text baseline for that excerpt only. Produce a new artifact; never overwrite source evidence or the user's original DOCX files.

For court-facing work, read [references/verification-protocol.md](references/verification-protocol.md) completely before editing.

## Required inputs

Obtain or identify:

1. The source video or audio.
2. The full transcript DOCX to be corrected.
3. Any manually corrected excerpt DOCX and its relationship to the recording.
4. The desired output path and whether semantic merging is required.

For autonomous execution, use only MAGI's direct local oMLX/Gemma vision/text routes and local ASR routes. Do not call a general chat fallback chain. Reject any Codex/OpenAI cloud model route. Generate a filtered second ASR pass when a second ASR file is not supplied.

`require_secondary_asr=false` is permitted only for development smoke tests. Never use that relaxation for court-facing verification or describe its result as court-grade.

If the user requests semantic merging, merge only consecutive speech by the same speaker. Start a new paragraph whenever the speaker changes, including short interjections such as「然後呢」、「為什麼」、「蛤」or a one-word answer.

## Mandatory workflow

### 1. Preserve and inventory

- Record absolute paths, hashes, media duration, and file sizes.
- Work in a separate audit directory.
- Extract DOCX text with accepted tracked changes for comparison, while preserving the source document.
- Generate or reuse the highest-quality available timestamped ASR. ASR is an index and omission detector, not ground truth.

Run:

```bash
python3 action.py --task '{"operation":"inspect","video":"/abs/hearing.mp4","transcript":"/abs/full.docx","baseline":"/abs/excerpt.docx","output_dir":"/abs/audit"}'
```

### 2. Establish the controlling baseline

- Accept the user's manually corrected excerpt exactly, including nonstandard pronouns, punctuation, ellipses, and name glyphs.
- Determine the excerpt's speaker labels from the corrected document and recording context.
- Do not silently normalize「他／她」, names, punctuation, or variant characters inside the baseline.
- Document any difference between excerpt timecodes and video playback time.

### 3. First independent verification: evidence and speaker pass

Review the recording in chronological order and verify:

- Every intelligible utterance is represented.
- Each speaker change creates a new turn.
- Consecutive same-speaker turns are semantically merged without deleting substantive words.
- Time ranges do not overlap across speakers.
- Long answer paragraphs do not hide prosecutor/judge questions.
- Breaks, water requests, guard coordination, resumption of questioning, and low-volume utterances are not omitted.
- Names and low-audibility words are not guessed.

Use visual position, mouth/head movement, microphone direction, voice, and dialogue context together. If the speaker or words remain uncertain, write an explicit marker such as:

- `【聽辨不清：…】`
- `【姓名聽辨未定：音似「…」】`
- `【發話者未定：…】`

MAGI autonomous mode must:

- Put every non-baseline transcript turn on the review timeline, then add denser points for speaker boundaries, short interjections, ASR question findings, and uncertainty markers.
- Sweep long turns at eight-second intervals for hidden speaker changes; never relabel a whole turn unless every planned point for that turn forms the same two-pass visual consensus.
- Extract five-frame chronological contact sheets around every selected utterance.
- Run visual Pass 1 and Pass 2 with different time windows and opposite reasoning order.
- Accept a speaker correction only when both passes independently agree above the confidence threshold.
- Run two text/ASR review prompts for material corrections and apply only byte-identical, high-confidence proposals.
- Preserve every non-agreed result in the unresolved-items table instead of guessing.
- Fail the court-grade gate if a configured review limit truncates any planned timeline point.

### 4. Run deterministic audit checks

After producing a candidate transcript, run:

```bash
python3 action.py --task '{"operation":"audit","transcript":"/abs/candidate.docx","baseline":"/abs/excerpt.docx","asr_json":"/abs/asr.json","baseline_video_start":"01:23:49","baseline_video_end":"01:29:20","output_dir":"/abs/audit"}'
```

Treat these as mandatory review gates:

- Baseline entry count and exact-text match.
- No cross-speaker timeline overlaps within each monotonic time block.
- No high-confidence ASR speech left outside transcript coverage, after accounting for excerpt/video time mapping.
- Review every question-like ASR segment found wholly inside an answer-only speaker range.
- Preserve a list of unresolved hearing/name/speaker items.

Fix every real issue and rerun the audit. Do not suppress a finding merely to make the report pass.

### 5. Second independent verification: output-first pass

Restart from the generated DOCX rather than the working notes:

1. Re-extract the DOCX text.
2. Recompare every baseline entry.
3. Recompute overlaps and coverage.
4. Recheck speaker attribution at all flagged question-like segments.
5. Confirm every audit-text time/speaker header exists in the DOCX.
6. Validate and render the Word document.

This second pass must not reuse the first pass's conclusions without rechecking the output artifact.

In autonomous mode, Pass 2 must use separately sampled frames and an independently generated filtered-audio ASR. Reusing the first pass's frame sheet or the same ASR file does not satisfy the court-grade gate.

### 6. Validate the court-ready DOCX

Run:

```bash
python3 action.py --task '{"operation":"validate-docx","transcript":"/abs/final.docx","output_dir":"/abs/audit/render"}'
```

Then visually inspect at least:

- The title/instructions page.
- A page containing newly split speaker turns.
- The manually corrected excerpt section.
- A late transcript page.
- The unresolved-items table and final page.

Require a valid DOCX, A4 rendering, no blank/cutoff pages, readable CJK text, stable page numbering, and no missing time/speaker headers.

## Completion standard

Deliver only after both passes are complete. Report:

- The final output path.
- What was corrected, especially speaker-attribution and omitted-speech fixes.
- Whether the baseline matched exactly.
- Remaining explicitly marked uncertainties.
- DOCX validation and rendered page count.

Never claim that an unclear word is certain. Never state court-ready completion if validation or the second pass was skipped.

MAGI can perform all technical work without Codex assistance. A qualified human remains responsible only for the final legal filing decision; human confirmation is not a substitute for a failed technical gate.

## MAGI execution contract

- `inspect`: inventory evidence and extract accepted DOCX text.
- `audit`: run baseline, overlap, ASR coverage, and speaker-review checks.
- `validate-docx`: validate OOXML and render to PDF for QA.
- `full-check`: run inspect, two fresh extraction/audit passes, cross-pass consistency comparison, and DOCX validation on an existing candidate.
- `autonomous-plan`: build the bounded video review timeline without calling models or changing documents.
- `autonomous`: watch sampled video sequences, run dual ASR/text review, correct agreed speaker/text findings, semantically merge same-speaker turns, generate a new DOCX, and run two output readbacks.
- `live-start`: preflight and start one durable court-grade autonomous job from the protected manifest.
- `live-status`: return durable progress and completion evidence for the active court-grade job.
- `help`: print the JSON invocation schema.

Discord exposes the exact admin-only single-word command `勘驗`. Its first invocation starts the current protected manifest; later invocations return progress or the terminal court-grade result without duplicating the same job.

MAGI V2 calls this skill through `skills/run`. MAGI V3 schedules it under the shared `audio_transcription_translation` capability with a `transcription` worker; its successful completion evidence must include the final DOCX and audit report artifacts.
