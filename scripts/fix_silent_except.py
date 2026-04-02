#!/usr/bin/env python3
"""
Batch-replace silent exception patterns with logged versions.

Scans Python files for patterns like:
  - except Exception:
      pass
  - except Exception as e:
      pass
  - except:
      pass
  - except Exception: pass (same line)

And replaces with logging statements that include line numbers.

Usage:
  python fix_silent_except.py [--apply]

  Without --apply: dry run (shows what would change)
  With --apply: actually writes changes to files
"""

import os
import re
import sys
import logging
from pathlib import Path
from typing import List, Tuple, Set
from dataclasses import dataclass


@dataclass
class Replacement:
    """Represents a single replacement to be made."""
    line_number: int
    original: str
    replacement: str
    start_pos: int
    end_pos: int


@dataclass
class FileResult:
    """Result of processing a single file."""
    filepath: str
    replacements: List[Replacement]
    needs_import: bool
    changed: bool = False


class SilentExceptFixer:
    """Finds and fixes silent exception handlers."""

    # Directories to skip
    SKIP_DIRS = {'archive', 'build', 'dist', 'backups', '__pycache__'}

    # Pattern to detect except blocks with pass on next line
    # Matches: except [Exception|Exception as name]:\n<indentation>pass
    EXCEPT_PASS_MULTILINE = re.compile(
        r'^(\s*)(except\s+(?:Exception(?:\s+as\s+\w+)?)?)\s*:\s*\n(\s+)pass\b',
        re.MULTILINE
    )

    # Pattern to detect except blocks with pass on same line
    # Matches: except [Exception|Exception as name]: <spaces> pass
    EXCEPT_PASS_SAMELINE = re.compile(
        r'^(\s*)(except\s+(?:Exception(?:\s+as\s+\w+)?)?)\s*:\s+pass\b',
        re.MULTILINE
    )

    # Pattern to detect bare except: pass patterns
    EXCEPT_BARE_PASS_MULTILINE = re.compile(
        r'^(\s*)(except)\s*:\s*\n(\s+)pass\b',
        re.MULTILINE
    )

    EXCEPT_BARE_PASS_SAMELINE = re.compile(
        r'^(\s*)(except)\s*:\s+pass\b',
        re.MULTILINE
    )

    def __init__(self, magi_root: str):
        """Initialize with MAGI root directory."""
        self.magi_root = Path(magi_root).resolve()
        self.files_to_scan = self._get_files_to_scan()

    def _get_files_to_scan(self) -> List[Path]:
        """Get all Python files to scan, respecting skip directories."""
        files = []
        scan_dirs = ['api', 'casper_ecosystem', 'skills']

        for scan_dir in scan_dirs:
            dir_path = self.magi_root / scan_dir
            if not dir_path.exists():
                print(f"Warning: Directory not found: {dir_path}", file=sys.stderr)
                continue

            for py_file in dir_path.rglob('*.py'):
                # Check if file is in a skip directory
                if self._is_in_skip_dir(py_file):
                    continue
                files.append(py_file)

        return sorted(files)

    def _is_in_skip_dir(self, filepath: Path) -> bool:
        """Check if filepath is under a skip directory."""
        for part in filepath.parts:
            if part in self.SKIP_DIRS:
                return True
        return False

    def _line_number_at_pos(self, content: str, pos: int) -> int:
        """Get line number (1-indexed) at position in content."""
        return content[:pos].count('\n') + 1

    def _get_indentation(self, content: str, pos: int) -> str:
        """Get indentation string at the start of the line containing pos."""
        # Find start of line
        line_start = content.rfind('\n', 0, pos) + 1
        # Find end of indentation
        indent_end = line_start
        while indent_end < len(content) and content[indent_end] in ' \t':
            indent_end += 1
        return content[line_start:indent_end]

    def _find_replacements(self, content: str, filepath: Path) -> Tuple[List[Replacement], bool]:
        """Find all silent except patterns in content."""
        replacements = []

        # Pattern 1: except Exception: pass (multiline)
        for match in self.EXCEPT_PASS_MULTILINE.finditer(content):
            indent = match.group(1)
            pass_indent = match.group(3)
            line_num = self._line_number_at_pos(content, match.start())

            # Build replacement maintaining indentation
            replacement = (
                f"{indent}except Exception:\n"
                f"{pass_indent}logging.getLogger(__name__).debug("
                f'"silent-catch at %s:%s", __name__, {line_num}, exc_info=True)'
            )
            replacements.append(Replacement(
                line_number=line_num,
                original=match.group(0),
                replacement=replacement,
                start_pos=match.start(),
                end_pos=match.end()
            ))

        # Pattern 2: except Exception: pass (same line)
        for match in self.EXCEPT_PASS_SAMELINE.finditer(content):
            # Skip if already matched by multiline pattern
            start_pos = match.start()
            if any(r.start_pos <= start_pos < r.end_pos for r in replacements):
                continue

            indent = match.group(1)
            line_num = self._line_number_at_pos(content, match.start())

            replacement = (
                f"{indent}except Exception:\n"
                f"{indent}    logging.getLogger(__name__).debug("
                f'"silent-catch at %s:%s", __name__, {line_num}, exc_info=True)'
            )
            replacements.append(Replacement(
                line_number=line_num,
                original=match.group(0),
                replacement=replacement,
                start_pos=match.start(),
                end_pos=match.end()
            ))

        # Pattern 3: bare except: pass (multiline)
        for match in self.EXCEPT_BARE_PASS_MULTILINE.finditer(content):
            # Skip if already matched
            start_pos = match.start()
            if any(r.start_pos <= start_pos < r.end_pos for r in replacements):
                continue

            indent = match.group(1)
            pass_indent = match.group(3)
            line_num = self._line_number_at_pos(content, match.start())

            replacement = (
                f"{indent}except:\n"
                f"{pass_indent}logging.getLogger(__name__).debug("
                f'"silent-catch at %s:%s", __name__, {line_num}, exc_info=True)'
            )
            replacements.append(Replacement(
                line_number=line_num,
                original=match.group(0),
                replacement=replacement,
                start_pos=match.start(),
                end_pos=match.end()
            ))

        # Pattern 4: bare except: pass (same line)
        for match in self.EXCEPT_BARE_PASS_SAMELINE.finditer(content):
            # Skip if already matched
            start_pos = match.start()
            if any(r.start_pos <= start_pos < r.end_pos for r in replacements):
                continue

            indent = match.group(1)
            line_num = self._line_number_at_pos(content, match.start())

            replacement = (
                f"{indent}except:\n"
                f"{indent}    logging.getLogger(__name__).debug("
                f'"silent-catch at %s:%s", __name__, {line_num}, exc_info=True)'
            )
            replacements.append(Replacement(
                line_number=line_num,
                original=match.group(0),
                replacement=replacement,
                start_pos=match.start(),
                end_pos=match.end()
            ))

        # Check if file needs logging import
        needs_import = 'import logging' not in content

        return sorted(replacements, key=lambda r: r.start_pos, reverse=True), needs_import

    def _apply_replacements(self, content: str, replacements: List[Replacement],
                           needs_import: bool) -> str:
        """Apply replacements to content, in reverse order to maintain positions."""
        # Apply replacements in reverse order
        for replacement in replacements:
            content = (
                content[:replacement.start_pos] +
                replacement.replacement +
                content[replacement.end_pos:]
            )

        # Add import if needed
        if needs_import and replacements:
            # Add import at the very top of file
            if content.startswith('#!'):
                # Skip shebang line
                newline_pos = content.find('\n')
                insert_pos = newline_pos + 1
            else:
                insert_pos = 0

            # Check if there are already imports
            # Insert after existing imports or at the top
            lines = content.split('\n')
            insert_line = 0

            # Skip shebang and docstrings
            in_docstring = False
            docstring_char = None
            for i, line in enumerate(lines):
                stripped = line.strip()

                if i == 0 and stripped.startswith('#!'):
                    insert_line = i + 1
                    continue

                # Handle docstrings
                if '"""' in stripped or "'''" in stripped:
                    if not in_docstring:
                        in_docstring = True
                        docstring_char = '"""' if '"""' in stripped else "'''"
                        if stripped.count(docstring_char) == 2:
                            in_docstring = False
                    else:
                        in_docstring = False
                    insert_line = i + 1
                    continue

                if in_docstring:
                    insert_line = i + 1
                    continue

                # Found first non-docstring, non-shebang line
                break

            # Also skip past any `from __future__` imports (must be first)
            while insert_line < len(lines):
                s = lines[insert_line].strip()
                if s.startswith('from __future__'):
                    insert_line += 1
                elif s == '' and insert_line + 1 < len(lines) and lines[insert_line + 1].strip().startswith('from __future__'):
                    insert_line += 1
                else:
                    break

            # Insert import
            lines.insert(insert_line, 'import logging')
            content = '\n'.join(lines)

        return content

    def process_file(self, filepath: Path) -> FileResult:
        """Process a single file, returning what changes would be made."""
        try:
            with open(filepath, 'r', encoding='utf-8') as f:
                content = f.read()
        except (UnicodeDecodeError, IOError) as e:
            print(f"Error reading {filepath}: {e}", file=sys.stderr)
            return FileResult(str(filepath), [], False)

        replacements, needs_import = self._find_replacements(content, filepath)

        return FileResult(
            filepath=str(filepath),
            replacements=replacements,
            needs_import=needs_import,
            changed=len(replacements) > 0
        )

    def process_all_files(self) -> List[FileResult]:
        """Process all files, returning results."""
        results = []
        for filepath in self.files_to_scan:
            result = self.process_file(filepath)
            if result.changed or result.needs_import:
                results.append(result)
        return results

    def apply_changes(self, results: List[FileResult]) -> None:
        """Write changes to disk."""
        for result in results:
            if not result.changed and not result.needs_import:
                continue

            try:
                with open(result.filepath, 'r', encoding='utf-8') as f:
                    content = f.read()
            except (UnicodeDecodeError, IOError) as e:
                print(f"Error reading {result.filepath}: {e}", file=sys.stderr)
                continue

            new_content = self._apply_replacements(content, result.replacements, result.needs_import)

            try:
                with open(result.filepath, 'w', encoding='utf-8') as f:
                    f.write(new_content)
            except IOError as e:
                print(f"Error writing {result.filepath}: {e}", file=sys.stderr)
                continue


def format_filepath(filepath: str, root: Path) -> str:
    """Format filepath relative to root if possible."""
    try:
        return str(Path(filepath).relative_to(root))
    except ValueError:
        return filepath


def main():
    """Main entry point."""
    import argparse

    parser = argparse.ArgumentParser(
        description='Batch-replace silent exception patterns with logged versions'
    )
    parser.add_argument(
        '--apply',
        action='store_true',
        help='Actually write changes to files (default: dry run)'
    )

    args = parser.parse_args()

    # Determine MAGI root (parent of scripts/)
    magi_root = Path(__file__).resolve().parent.parent
    if not (magi_root / "api").exists():
        print(f"Error: MAGI root not found at {magi_root}", file=sys.stderr)
        sys.exit(1)

    # Run fixer
    fixer = SilentExceptFixer(str(magi_root))

    if not fixer.files_to_scan:
        print("No Python files found to scan.")
        sys.exit(0)

    print(f"Scanning {len(fixer.files_to_scan)} Python files...")
    results = fixer.process_all_files()

    if not results:
        print("No silent exception patterns found.")
        sys.exit(0)

    # Display results
    mode = "DRY RUN" if not args.apply else "APPLY MODE"
    print(f"\n{'='*70}")
    print(f"{mode}: Changes to be made")
    print(f"{'='*70}\n")

    total_replacements = 0
    for result in results:
        rel_path = format_filepath(result.filepath, magi_root)
        print(f"{rel_path}")

        if result.replacements:
            print(f"  Replacements: {len(result.replacements)}")
            for i, repl in enumerate(result.replacements, 1):
                print(f"    Line {repl.line_number}: except ... pass")
            total_replacements += len(result.replacements)

        if result.needs_import:
            print(f"  Add: import logging")

        print()

    # Summary
    print(f"{'='*70}")
    print(f"Summary:")
    print(f"  Files affected: {len(results)}")
    print(f"  Total replacements: {total_replacements}")
    print(f"{'='*70}\n")

    if args.apply:
        print("Applying changes...")
        fixer.apply_changes(results)
        print("Changes applied successfully!")
    else:
        print("DRY RUN: No changes written.")
        print("Use --apply flag to write changes to disk.")


if __name__ == '__main__':
    main()
