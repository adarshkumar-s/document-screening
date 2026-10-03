# OCR benchmark: compare extraction quality with evidence

This benchmark makes OCR/extraction comparisons measurable rather than relying on feature lists or README claims.

## Input format

Create a UTF-8 JSONL file with one reviewed document per line. Use synthetic or properly de-identified examples; do not commit landowner personal data.

```json
{"document_id":"synthetic-001","expected":{"survey_number":"12/3","village":"Rampur","area":"2.5 hectare"},"predicted":{"survey_number":"12/3","village":"Rampur","area":"2.5 hectares"}}
{"document_id":"synthetic-002","expected":{"survey_number":"44","village":"रामपुर"},"predicted":{"survey_number":"","village":"रामपुर"}}
```

- `expected` contains values checked by a human reviewer.
- `predicted` contains the output from the OCR/extraction system being evaluated.
- Field names must be consistent across systems.
- Keep the same held-out document set for every system.

## Run

```bash
python tools/ocr_benchmark.py /path/to/reviewed-results.jsonl
python tools/ocr_benchmark.py /path/to/reviewed-results.jsonl --no-mismatches
python -m unittest tests.test_ocr_benchmark -v
```

## How to interpret the report

- **Field coverage**: fraction of non-empty expected fields for which the system returned a non-empty value.
- **Exact-match accuracy**: fraction of expected field values that match after Unicode compatibility normalization, case folding and whitespace normalization. Missing values count as incorrect.
- **Mean character similarity**: normalized Levenshtein similarity, useful for spotting near-misses but not a substitute for exact correctness.
- **Per-field metrics**: exposes weak fields such as survey number, owner, village or area that an aggregate can hide.
- **Mismatches**: shows the expected and predicted values for manual error analysis. Use `--no-mismatches` when sharing a summary that should not expose record text.

This is a benchmark of supplied extraction outputs, not an OCR engine and not a legal validation. It deliberately does not normalize away punctuation, units or digits that can change land-record meaning. Build a held-out set covering clear scans, low-resolution/skewed scans, multi-page PDFs and relevant languages/scripts. Report sample size and metrics by document type and language before comparing systems.
