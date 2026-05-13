=============================================================================
fix_silent_except.py - Silent Exception Pattern Fixer
=============================================================================

OVERVIEW
--------
This script batch-replaces silent exception handlers with logged versions 
across the MAGI codebase. It finds patterns like:
  - except Exception:
      pass
  - except Exception as e:
      pass
  - except:
      pass
  - except Exception: pass (on same line)

And replaces them with logging calls that include line numbers and exc_info.

USAGE
-----
1. DRY RUN (preview changes):
   python3 fix_silent_except.py

2. APPLY CHANGES (write to disk):
   python3 fix_silent_except.py --apply

DIRECTORIES SCANNED
-------------------
- api/
- casper_ecosystem/
- skills/

DIRECTORIES SKIPPED
-------------------
- archive/
- build/
- dist/
- backups/
- __pycache__/

REPLACEMENT PATTERN
-------------------
Original:
  except Exception:
      pass

Becomes:
  except Exception:
      logging.getLogger(__name__).debug(
          "silent-catch at %s:%s", __name__, <LINE_NUMBER>, exc_info=True)

FEATURES
--------
✓ Handles variable indentation (spaces and tabs)
✓ Matches both multiline and same-line patterns
✓ Preserves original indentation
✓ Auto-adds "import logging" where needed
✓ Includes line numbers in log messages
✓ Processes ~7000+ Python files
✓ Reports detailed summary (files affected, replacements per file)

EXAMPLE OUTPUT (DRY RUN)
-----------------------
Scanning 7178 Python files...

======================================================================
DRY RUN: Changes to be made
======================================================================

api/admin_allowlist.py
  Replacements: 2
    Line 91: except ... pass
    Line 26: except ... pass
  Add: import logging

...

======================================================================
Summary:
  Files affected: 6698
  Total replacements: 1397
======================================================================

SAFETY
------
- Default mode is DRY RUN (no changes written)
- Always review dry run output before using --apply
- Original files have proper encoding detection (UTF-8)
- Skips files with read errors (reported to stderr)

