# MAGI Tests

All test scripts, fixtures, and QA tools are consolidated in this directory.

## Structure

```
tests/
├── test_*.py           # Unit and integration tests (pytest)
├── smoke_*.py          # Smoke tests for quick validation
├── simulation_test.py  # Full system simulation
├── conftest.py         # Shared pytest fixtures
├── fixtures/           # Test media, PDFs, and sample data
│   ├── *.pdf           # Sample PDFs for OCR/processing tests
│   ├── *.wav / *.aiff  # Audio samples for speech pipeline tests
│   └── *.jpg / *.png   # Images for vision tests
└── tmp_qa/             # One-off QA scripts (gitignored)
```

## Running Tests

```bash
# Run all tests
cd /Users/ai/Desktop/MAGI
python -m pytest tests/ -v

# Run a specific test
python -m pytest tests/test_routing.py -v

# Run smoke tests only
python -m pytest tests/smoke_*.py -v
```

## Adding New Tests

Place all new test files in this directory following the naming convention:
- `test_*.py` for pytest-compatible tests
- `smoke_*.py` for quick smoke tests
- Large test fixtures go in `fixtures/`
- Temporary QA scripts go in `tmp_qa/` (gitignored)
