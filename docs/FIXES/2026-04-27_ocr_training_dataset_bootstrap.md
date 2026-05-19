# OCR training dataset bootstrap

## Scope

Created a first usable MAGI OCR/field-extraction training dataset from already curated court-document filenames plus macOS Vision evidence. This is silver data, not human gold data: records are accepted only when filename-derived fields are supported by OCR/native text evidence.

## Files

- `scripts/ops/build_ocr_training_dataset.py`
- `tests/test_ocr_training_dataset_builder.py`
- `data/ocr_training/20260427_court_docs_vision40/`

## Output

- `silver_ocr_field_training.jsonl`: 24 records
- `needs_labeling.jsonl`: 11 records
- `rejected.jsonl`: 5 records
- `manifest.json`: run metadata

Each silver record contains:

- PDF path, filename, SHA256 head digest
- parsed target fields: `date`, `court`, `case_number`, `doc_type`, `party`
- OCR/native sources with quality scores
- support evidence and support score
- chat-style `training_messages` suitable for a strict JSON OCR-field extractor fine-tune or distillation run

## Guardrails

- No LLM was used to create labels.
- Broad Synology walks are avoided by using `--candidate-list`.
- Each PDF is processed in a separate subprocess with `--per-file-timeout`, so cloud placeholders or bad PDFs cannot block the batch.
- Chandra/Qwen was not used.
- LAF core modules were not touched.

## Validation

- `./venv/bin/python -m pytest -q tests/test_ocr_training_dataset_builder.py` -> 5 passed
- Smoke dataset without Vision showed native text is insufficient for scanned court PDFs: 0 silver / 13 needs-labeling / 7 errors on 20 files.
- Vision dataset on 20 files: 12 silver / 6 needs-labeling / 2 errors.
- Vision dataset on 40 files: 24 silver / 11 needs-labeling / 5 errors.
- Verified every sampled silver record has parseable `training_messages[-1].content` matching `filename_fields`.

## Notes

The 40-file batch is intentionally small and clean. It is suitable as the first distillation/evaluation seed, but it should not be treated as a final model-training corpus without later human review and broader sampling.
