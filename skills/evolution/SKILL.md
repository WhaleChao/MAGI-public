---
name: evolution
description: Skill generation and self-evolution capabilities. Use when creating new automation skills, generating code for new tools, or when the system needs to extend its own capabilities.
license: MIT
compatibility: Requires Melchior GPU for code generation
metadata:
  author: MAGI-Federation
  version: "1.0"
  sage: melchior
---

# Evolution Skill

Self-evolution and skill generation capabilities for MAGI.

## Capabilities

- **Create Skill**: Generate new Python skills from natural language descriptions
- **Validate Skill**: Test generated skills before deployment
- **Deploy Skill**: Add new skills to the registry

## Usage

```python
from skills.evolution.skill_factory import create_skill

# Generate a new skill
result = create_skill(
    name="pdf_parser",
    description="Extract text from PDF files",
    instructions="Use PyMuPDF to extract text from pages"
)
```

## Files

- `skill_factory.py` - Skill generation via LLM
