# MAGI — Third Party Bill of Materials (BOM)

**Last Updated:** March 19, 2026

This document lists all direct and indirect dependencies of the MAGI project, their versions, licenses, and licensing notes.

---

## Core Dependencies

### Web Framework & HTTP
| Package | Version | License | Notes |
|---------|---------|---------|-------|
| Flask | ≥3.0 | BSD-3-Clause | Web framework for REST API and UI |
| Flask-Login | ≥0.6 | MIT | User session management |
| Werkzeug | ≥3.0 | BSD-3-Clause | WSGI utilities (Flask dependency) |
| flask-cors | ≥4.0 | MIT | Cross-Origin Resource Sharing support |
| requests | ≥2.31 | Apache-2.0 | HTTP client library |
| aiohttp | ≥3.9 | Apache-2.0 | Async HTTP client/server |

### Configuration & Environment
| Package | Version | License | Notes |
|---------|---------|---------|-------|
| python-dotenv | ≥1.0 | BSD-3-Clause | Environment variable loading |

### Database
| Package | Version | License | Notes |
|---------|---------|---------|-------|
| mysql-connector-python | ≥8.2 | GPL-2.0 with FOSS Exception | MySQL database connector; **Commercial use requires acknowledgment** |

### System & Process Management
| Package | Version | License | Notes |
|---------|---------|---------|-------|
| psutil | ≥5.9 | BSD-3-Clause | System and process utilities |

---

## Channel Integration Dependencies

### Discord Integration
| Package | Version | License | Notes |
|---------|---------|---------|-------|
| discord.py | ≥2.3 | MIT | Discord bot framework |

### LINE Messaging API
| Package | Version | License | Notes |
|---------|---------|---------|-------|
| line-bot-sdk | ≥3.5 | Apache-2.0 | LINE Messaging API SDK |

### Telegram Bot
| Package | Version | License | Notes |
|---------|---------|---------|-------|
| python-telegram-bot | ≥20.0 | LGPL-3.0 | Telegram Bot API wrapper |

---

## PDF Processing Dependencies

### PDF Libraries
| Package | Version | License | Notes |
|---------|---------|---------|-------|
| PyMuPDF | ≥1.23 | AGPL-3.0 | PDF document processing; **Commercial use requires license** |
| pypdf | ≥3.17 | BSD-3-Clause | PDF reader and writer |
| pdfplumber | ≥0.10 | MIT | PDF data extraction |
| reportlab | ≥4.0 | BSD-3-Clause | PDF generation |
| pdf2image | ≥1.16 | Apache-2.0 | PDF to image conversion |
| Pillow | ≥10.0 | HPND | Image processing library |

---

## Document Processing Dependencies

### Office Documents
| Package | Version | License | Notes |
|---------|---------|---------|-------|
| python-docx | ≥1.0 | MIT | Read/write Microsoft Word files |
| python-pptx | ≥0.6 | MIT | Read/write PowerPoint files |
| openpyxl | ≥3.1 | MIT | Read/write Excel files |

### XML & Document Parsing
| Package | Version | License | Notes |
|---------|---------|---------|-------|
| lxml | ≥4.9 | BSD-3-Clause | XML and HTML processing |
| defusedxml | ≥0.7 | PSF | Prevent XML vulnerabilities |
| EbookLib | ≥0.18 | LGPL-3.0 | EPUB e-book library |

---

## OCR Dependencies

| Package | Version | License | Notes |
|---------|---------|---------|-------|
| rapidocr-onnxruntime | ≥1.3 | MIT/Apache-2.0 | Optical Character Recognition using ONNX Runtime |
| opendataloader-pdf | ≥2.4.3 | Apache-2.0 | Optional layout-aware PDF conversion/OCR provider; requires Java 11+ |

---

## Web Scraping & Browser Automation

| Package | Version | License | Notes |
|---------|---------|---------|-------|
| beautifulsoup4 | ≥4.12 | MIT | HTML/XML parsing |
| playwright | ≥1.40 | Apache-2.0 | Browser automation framework |
| selenium | ≥4.15 | Apache-2.0 | WebDriver browser automation |
| webdriver-manager | ≥4.0 | MIT | WebDriver binary management |

---

## AI & Machine Learning Dependencies

| Package | Version | License | Notes |
|---------|---------|---------|-------|
| numpy | ≥1.24 | BSD-3-Clause | Numerical computing library |
| faiss-cpu | ≥1.7 | MIT | Facebook AI similarity search |

---

## Google APIs Integration

| Package | Version | License | Notes |
|---------|---------|---------|-------|
| google-api-python-client | ≥2.100 | Apache-2.0 | Google API client library |
| google-auth | ≥2.23 | Apache-2.0 | Google authentication |
| google-auth-oauthlib | ≥1.1 | Apache-2.0 | OAuth flow for Google APIs |

---

## Utility Dependencies

| Package | Version | License | Notes |
|---------|---------|---------|-------|
| python-dateutil | ≥2.8 | BSD-3-Clause | Date/time utilities |
| holidays | ≥0.40 | MIT | Holiday calendar library |
| pyperclip | ≥1.8 | BSD-3-Clause | Clipboard operations |
| feedparser | ≥6.0 | BSD-2-Clause | RSS/Atom feed parsing |
| watchdog | ≥3.0 | Apache-2.0 | File system event monitoring |

---

## GUI Dependencies

| Package | Version | License | Notes |
|---------|---------|---------|-------|
| tkcalendar | ≥1.6 | MIT | Tkinter calendar widget |

---

## Development & Testing Dependencies

| Package | Version | License | Notes |
|---------|---------|---------|-------|
| pytest | ≥7.0 | MIT | Testing framework |
| pytest-cov | ≥4.0 | MIT | Code coverage plugin |
| ruff | ≥0.3 | MIT | Python linter and formatter |

---

## License Summary

| License | Count | Restriction Level |
|---------|-------|-------------------|
| MIT | 24+ | ⭐⭐ Low (Permissive) |
| Apache-2.0 | 12+ | ⭐⭐ Low (Permissive) |
| BSD-3-Clause | 10+ | ⭐⭐ Low (Permissive) |
| LGPL-3.0 | 2 | ⭐⭐⭐ Medium (Copyleft) |
| GPL-2.0 (with exception) | 1 | ⭐⭐⭐ Medium (Copyleft) |
| AGPL-3.0 | 1 | ⭐⭐⭐⭐ High (Network Copyleft) |
| HPND | 1 | ⭐⭐ Low (Permissive) |
| PSF | 1 | ⭐⭐ Low (Permissive) |

---

## License Compliance Notes

### Critical: Copyleft Licenses

1. **PyMuPDF (AGPL-3.0)** — Network copyleft; any modifications to source or networked use triggers GPL disclosure requirements
   - Status: Used for PDF processing
   - Recommendation: Review if commercial distribution is planned

2. **mysql-connector-python (GPL-2.0 with FOSS Exception)** — Standard GPL with exception for free/open-source software
   - Status: Database connector
   - Recommendation: Current use is compliant

3. **python-telegram-bot & EbookLib (LGPL-3.0)** — Weak copyleft; allows proprietary use if library is dynamically linked
   - Status: Optional dependencies
   - Recommendation: Review linking strategy if used

### Permissive Licenses

Most core and optional dependencies use MIT, Apache-2.0, or BSD-3-Clause licenses, which are business-friendly and permissive.

---

## Binary Dependencies & Transitive Dependencies

This BOM covers **direct dependencies only**. Transitive dependencies are managed by `pip` and may introduce additional licenses.

To generate a complete dependency tree:
```bash
pip freeze > frozen-requirements.txt
pipdeptree > dependency-tree.txt
```

---

## Dependency Updates & Security

Run periodic security scans:
```bash
pip install safety bandit
safety check
bandit -r api/ bin/ -ll
```

Monitor for vulnerabilities:
- GitHub Dependabot
- Snyk
- PyPI Security Advisories

---

## Version Pinning Policy

- **Core dependencies**: Pin major version (e.g., `flask>=3.0`)
- **Optional dependencies**: Allow minor updates (e.g., `pytest>=7.0`)
- **Development tools**: Allow flexibility unless security issue discovered

---

## Contact & License Updates

For questions about third-party licenses or commercial licensing concerns, contact:
- **Legal**: [INSERT LEGAL CONTACT EMAIL]
- **Technical**: [INSERT TECH CONTACT EMAIL]

---

**Generated:** 2026-03-19 | **Schema Version:** 1.0
