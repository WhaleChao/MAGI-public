#!/usr/bin/env python3
"""Public release safety audit for MAGI.

The audit scans files tracked by git, blocks known private runtime paths, and
flags high-confidence secrets before a branch is pushed to a public remote.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Iterable


REPO_ROOT = Path(__file__).resolve().parents[1]

BLOCKED_TRACKED_PREFIXES = (
    ".claude/",
    ".claire/",
    ".runtime/",
    "runtime/supplement_cache/",
    "docs/deploy/",
)

TEXT_EXT_ALLOW = {
    "",
    ".cfg",
    ".css",
    ".csv",
    ".env",
    ".example",
    ".html",
    ".ini",
    ".js",
    ".json",
    ".jsonl",
    ".md",
    ".plist",
    ".py",
    ".sh",
    ".sql",
    ".toml",
    ".txt",
    ".yaml",
    ".yml",
}


@dataclass(frozen=True)
class Finding:
    path: str
    line: int
    kind: str
    severity: str
    detail: str


SECRET_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("private_key", re.compile(r"BEGIN (?:RSA|OPENSSH|EC|DSA|PRIVATE) KEY")),
    ("github_token", re.compile(r"\b(?:ghp|gho|ghu|ghs|ghr)_[A-Za-z0-9_]{20,}\b")),
    ("github_pat", re.compile(r"\bgithub_pat_[A-Za-z0-9_]{20,}\b")),
    ("huggingface_token", re.compile(r"\bhf_[A-Za-z0-9]{20,}\b")),
    ("slack_token", re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{20,}\b")),
    ("openai_key", re.compile(r"\bsk-[A-Za-z0-9]{32,}\b")),
    ("nvidia_nim_key", re.compile(r"\bnvapi-[A-Za-z0-9_-]{24,}\b")),
    ("bearer_token", re.compile(r"\bBearer\s+[A-Za-z0-9._-]{24,}\b")),
    ("inline_password", re.compile(r"(?i)\bpassword\s*[:=]\s*['\"][^'\"]{8,}['\"]")),
    ("mysql_cli_password", re.compile(r"-p['\"][^,'\"]{8,}['\"]")),
)

PII_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("taiwan_mobile", re.compile(r"\b09\d{8}\b")),
    ("tailnet_ip", re.compile(r"\b100\.(?:6[4-9]|[7-9]\d|1[01]\d|12[0-7])\.\d{1,3}\.\d{1,3}\b")),
)

PUBLIC_ISOLATION_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("lawsnote_private_feature", re.compile(r"lawsnote", re.IGNORECASE)),
    ("private_mailbox_marker", re.compile(r"whalelawyer", re.IGNORECASE)),
    ("private_nas_marker", re.compile(r"lumi63181107", re.IGNORECASE)),
)


def _git_ls_files(repo_root: Path = REPO_ROOT) -> list[str]:
    proc = subprocess.run(
        ["git", "ls-files"],
        cwd=repo_root,
        check=True,
        text=True,
        stdout=subprocess.PIPE,
    )
    return [line for line in proc.stdout.splitlines() if line]


def _is_probably_text(path: Path) -> bool:
    if path.suffix.lower() in TEXT_EXT_ALLOW:
        return True
    return path.name in {".gitignore", ".env.example", "Dockerfile", "Makefile"}


def _is_allowed_secret_example(rel_path: str, line: str) -> bool:
    lower = line.lower()
    if "<<replace_with" in lower or "your-api-key" in lower:
        return True
    if '"password: "' in lower and ("startswith" in lower or "split" in lower):
        return True
    if rel_path.startswith("tests/") and any(marker in lower for marker in ("testkey", "oldkey", "newkey", "abcdefghijklmnopqrstuvwxyz")):
        return True
    return False


def _is_allowed_pii_example(rel_path: str, line: str, kind: str) -> bool:
    """Allow intentional fixture data without muting production files."""

    if not rel_path.startswith("tests/"):
        return False
    lower = line.lower()
    if any(marker in lower for marker in ("fixture", "sample", "dummy", "fake", "mock", "placeholder")):
        return True
    mobile_examples = ("091234" + "5678", "0912-345" + "-678", "098866" + "6555")
    if kind == "taiwan_mobile" and any(value in line for value in mobile_examples):
        return True
    tailnet_example = "100.64" + ".1.2"
    if kind == "tailnet_ip" and tailnet_example in line:
        return True
    return False


def scan_text(rel_path: str, text: str, *, public_isolation: bool = False) -> list[Finding]:
    findings: list[Finding] = []
    for idx, line in enumerate(text.splitlines(), start=1):
        if public_isolation:
            for kind, pattern in PUBLIC_ISOLATION_PATTERNS:
                if rel_path in {".gitignore", "scripts/public_release_audit.py", "scripts/first_run_setup.py"}:
                    continue
                if rel_path.startswith("tests/"):
                    continue
                if pattern.search(line):
                    findings.append(Finding(rel_path, idx, kind, "error", "private integration marker must not be published"))
        for kind, pattern in SECRET_PATTERNS:
            if pattern.search(line) and not _is_allowed_secret_example(rel_path, line):
                findings.append(Finding(rel_path, idx, kind, "error", "high-confidence secret-like value"))
        for kind, pattern in PII_PATTERNS:
            if pattern.search(line) and not _is_allowed_pii_example(rel_path, line, kind):
                severity = "warning"
                if rel_path.startswith(BLOCKED_TRACKED_PREFIXES):
                    severity = "error"
                findings.append(Finding(rel_path, idx, kind, severity, "PII/private-network marker"))
    return findings


def scan_tracked_files(
    paths: Iterable[str] | None = None,
    repo_root: Path = REPO_ROOT,
    *,
    public_isolation: bool = False,
) -> list[Finding]:
    tracked = list(paths) if paths is not None else _git_ls_files(repo_root)
    findings: list[Finding] = []
    for rel_path in tracked:
        if rel_path.startswith(BLOCKED_TRACKED_PREFIXES):
            findings.append(Finding(rel_path, 1, "blocked_path", "error", "private runtime/operator path is tracked"))
        abs_path = repo_root / rel_path
        if not abs_path.exists() or not _is_probably_text(abs_path):
            continue
        try:
            text = abs_path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue
        findings.extend(scan_text(rel_path, text, public_isolation=public_isolation))
    return findings


def summarize(findings: list[Finding]) -> dict[str, object]:
    errors = [f for f in findings if f.severity == "error"]
    warnings = [f for f in findings if f.severity == "warning"]
    return {
        "ok": not errors,
        "errors": len(errors),
        "warnings": len(warnings),
        "findings": [asdict(f) for f in findings],
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Scan tracked files before public release.")
    parser.add_argument("--json", action="store_true", help="print machine-readable JSON")
    parser.add_argument("--strict", action="store_true", help="treat warnings as failures")
    parser.add_argument("--public-isolation", action="store_true", help="also block private integrations such as Lawsnote/private NAS markers")
    args = parser.parse_args(argv)

    findings = scan_tracked_files(public_isolation=args.public_isolation)
    if args.strict:
        findings = [
            Finding(f.path, f.line, f.kind, "error" if f.severity == "warning" else f.severity, f.detail)
            for f in findings
        ]
    result = summarize(findings)

    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        status = "PASS" if result["ok"] else "FAIL"
        print(f"MAGI public release audit: {status} ({result['errors']} errors, {result['warnings']} warnings)")
        for finding in findings[:80]:
            print(f"{finding.severity.upper()} {finding.path}:{finding.line} {finding.kind} - {finding.detail}")
        if len(findings) > 80:
            print(f"... {len(findings) - 80} more findings omitted")

    return 0 if result["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
