#!/usr/bin/env python3
"""Build customer-facing MAGI installer artifacts.

The macOS artifact is an ad-hoc-signed .app inside a DMG.  Without an Apple
Developer ID it cannot be notarized, so the generated README explains the
Gatekeeper prompt honestly.  The Windows artifact is a payload folder plus a
PowerShell builder that creates MAGI-Setup.exe on a Windows runner.
"""

from __future__ import annotations

import argparse
import json
import os
import platform
import shutil
import subprocess
import sys
import time
import zipfile
from pathlib import Path
from typing import Any


REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_OUTPUT_ROOT = REPO_ROOT / "dist" / "installers"

if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))


def _run(command: list[str], *, cwd: Path | None = None, timeout: int = 1200) -> subprocess.CompletedProcess[str]:
    return subprocess.run(command, cwd=cwd, text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=timeout)


def _copy(src: Path, dst: Path) -> None:
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(src, dst)


def build_release_archive(source_root: Path, output_root: Path, *, bundle_name: str, force: bool) -> Path:
    from bin.release_bundle import build_release_bundle

    result = build_release_bundle(source_root, output_root / "release", bundle_name=bundle_name, force=force)
    return result.archive_path


def _write_macos_launcher(app_root: Path) -> None:
    macos_dir = app_root / "Contents" / "MacOS"
    resources = app_root / "Contents" / "Resources"
    macos_dir.mkdir(parents=True, exist_ok=True)
    resources.mkdir(parents=True, exist_ok=True)
    launcher = macos_dir / "MAGI Installer"
    launcher.write_text(
        """#!/bin/zsh
set -euo pipefail
APP_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
RES="$APP_ROOT/Resources"
CMD="cd \\"$RES\\" && /usr/bin/env python3 \\"$RES/magi_install_launcher.py\\" --archive \\"$RES/MAGI-release.zip\\" --public"
osascript <<OSA
tell application "Terminal"
  activate
  do script "$CMD"
end tell
OSA
""",
        encoding="utf-8",
    )
    launcher.chmod(0o755)


def _write_info_plist(app_root: Path) -> None:
    plist = app_root / "Contents" / "Info.plist"
    plist.write_text(
        """<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>CFBundleDevelopmentRegion</key><string>zh_TW</string>
  <key>CFBundleDisplayName</key><string>MAGI Installer</string>
  <key>CFBundleExecutable</key><string>MAGI Installer</string>
  <key>CFBundleIdentifier</key><string>tw.magi.installer</string>
  <key>CFBundleName</key><string>MAGI Installer</string>
  <key>CFBundlePackageType</key><string>APPL</string>
  <key>CFBundleShortVersionString</key><string>1.0</string>
  <key>CFBundleVersion</key><string>1</string>
  <key>LSMinimumSystemVersion</key><string>13.0</string>
  <key>NSHighResolutionCapable</key><true/>
</dict>
</plist>
""",
        encoding="utf-8",
    )


def _write_macos_readme(path: Path) -> None:
    path.write_text(
        """# MAGI macOS Installer

1. Open `MAGI Installer.app` from this DMG.
2. The installer opens Terminal and asks whether MAGI may install MariaDB, Tailscale, oMLX/Ollama, and the recommended model.
3. If macOS says the developer cannot be verified, Control-click the app and choose Open, or remove quarantine after verifying the source:

   xattr -dr com.apple.quarantine "/Applications/MAGI Installer.app"

This build is ad-hoc signed so it can be packaged reproducibly, but it is not notarized because no Apple Developer ID certificate was provided.
""",
        encoding="utf-8",
    )


def build_macos_app(archive: Path, output_root: Path, *, force: bool, make_dmg: bool) -> dict[str, Any]:
    app_root = output_root / "macos" / "MAGI Installer.app"
    if app_root.exists():
        if not force:
            raise FileExistsError(app_root)
        shutil.rmtree(app_root)
    resources = app_root / "Contents" / "Resources"
    resources.mkdir(parents=True, exist_ok=True)
    _write_macos_launcher(app_root)
    _write_info_plist(app_root)
    _copy(REPO_ROOT / "scripts" / "packaging" / "magi_install_launcher.py", resources / "magi_install_launcher.py")
    _copy(archive, resources / "MAGI-release.zip")
    _write_macos_readme(output_root / "macos" / "README-macOS.txt")

    codesign = shutil.which("codesign")
    sign = {"available": bool(codesign), "ok": False, "output_tail": ""}
    if codesign:
        proc = _run([codesign, "--force", "--deep", "--sign", "-", str(app_root)], timeout=120)
        sign = {"available": True, "ok": proc.returncode == 0, "output_tail": proc.stdout[-2000:]}

    dmg_path = output_root / "MAGI-macOS-Installer.dmg"
    dmg = {"requested": make_dmg, "ok": False, "path": str(dmg_path), "output_tail": ""}
    if make_dmg:
        if dmg_path.exists():
            dmg_path.unlink()
        hdiutil = shutil.which("hdiutil")
        if hdiutil:
            proc = _run(
                [
                    hdiutil,
                    "create",
                    "-volname",
                    "MAGI Installer",
                    "-srcfolder",
                    str(output_root / "macos"),
                    "-ov",
                    "-format",
                    "UDZO",
                    str(dmg_path),
                ],
                timeout=600,
            )
            dmg = {"requested": True, "ok": proc.returncode == 0, "path": str(dmg_path), "output_tail": proc.stdout[-2000:]}
        else:
            dmg = {"requested": True, "ok": False, "path": str(dmg_path), "output_tail": "hdiutil not found"}

    return {"app": str(app_root), "codesign": sign, "dmg": dmg}


def _write_windows_cmd(folder: Path) -> None:
    (folder / "Start MAGI Installer.cmd").write_text(
        """@echo off
setlocal
cd /d "%~dp0"
where py >nul 2>nul
if %ERRORLEVEL% EQU 0 (
  py -3 magi_install_launcher.py --archive MAGI-release.zip --public
) else (
  python magi_install_launcher.py --archive MAGI-release.zip --public
)
pause
""",
        encoding="utf-8",
    )


def _write_windows_builder(folder: Path) -> None:
    (folder / "build_windows_exe.ps1").write_text(
        r"""$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot
python -m pip install --upgrade pip pyinstaller
python -m PyInstaller --onefile --console --name MAGI-Setup --add-data "MAGI-release.zip;." magi_install_launcher.py
Write-Host "Built: $PSScriptRoot\dist\MAGI-Setup.exe"
""",
        encoding="utf-8",
    )


def _zip_folder(folder: Path, archive: Path) -> None:
    if archive.exists():
        archive.unlink()
    with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as zf:
        for path in sorted(folder.rglob("*")):
            zf.write(path, path.relative_to(folder.parent).as_posix())


def build_windows_payload(archive: Path, output_root: Path, *, force: bool, build_exe: bool) -> dict[str, Any]:
    folder = output_root / "windows"
    if folder.exists():
        if not force:
            raise FileExistsError(folder)
        shutil.rmtree(folder)
    folder.mkdir(parents=True, exist_ok=True)
    _copy(REPO_ROOT / "scripts" / "packaging" / "magi_install_launcher.py", folder / "magi_install_launcher.py")
    _copy(archive, folder / "MAGI-release.zip")
    _write_windows_cmd(folder)
    _write_windows_builder(folder)
    (folder / "README-Windows.txt").write_text(
        """MAGI Windows Installer

Preferred customer artifact: MAGI-Setup.exe built on Windows with build_windows_exe.ps1.
The setup program detects and can help install MariaDB, Tailscale, Ollama, and the recommended model when the customer allows system package installation.
Unsigned EXE files may show Microsoft Defender SmartScreen warnings until a publisher/hash reputation exists.
If no EXE is available, run "Start MAGI Installer.cmd" from this folder.
""",
        encoding="utf-8",
    )
    zip_path = output_root / "MAGI-Windows-Installer-Payload.zip"
    _zip_folder(folder, zip_path)

    exe_result = {"requested": build_exe, "ok": False, "path": "", "output_tail": ""}
    if build_exe:
        if platform.system() != "Windows":
            exe_result["output_tail"] = "Windows EXE build skipped: run build_windows_exe.ps1 on a Windows runner."
        else:
            proc = _run(["powershell", "-ExecutionPolicy", "Bypass", "-File", str(folder / "build_windows_exe.ps1")], cwd=folder, timeout=1800)
            exe_path = folder / "dist" / "MAGI-Setup.exe"
            exe_result = {"requested": True, "ok": proc.returncode == 0 and exe_path.exists(), "path": str(exe_path), "output_tail": proc.stdout[-4000:]}

    return {"folder": str(folder), "zip": str(zip_path), "exe": exe_result}


def build_installers(args: argparse.Namespace) -> dict[str, Any]:
    output_root = args.output_root.resolve()
    output_root.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d")
    bundle_name = args.bundle_name or f"MAGI-customer-{stamp}"
    archive = Path(args.archive).resolve() if args.archive else build_release_archive(args.source_root.resolve(), output_root, bundle_name=bundle_name, force=args.force)
    payload = {
        "ok": True,
        "generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "archive": str(archive),
        "output_root": str(output_root),
        "macos": build_macos_app(archive, output_root, force=args.force, make_dmg=not args.no_dmg),
        "windows": build_windows_payload(archive, output_root, force=args.force, build_exe=args.windows_exe),
    }
    payload["ok"] = bool(payload["macos"]["codesign"]["ok"] or platform.system() != "Darwin")
    if not args.no_dmg:
        payload["ok"] = payload["ok"] and bool(payload["macos"]["dmg"]["ok"])
    manifest = output_root / "installer_manifest.json"
    manifest.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    payload["manifest"] = str(manifest)
    return payload


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Build MAGI customer installer artifacts.")
    parser.add_argument("--source-root", type=Path, default=REPO_ROOT)
    parser.add_argument("--output-root", type=Path, default=DEFAULT_OUTPUT_ROOT)
    parser.add_argument("--bundle-name", default="")
    parser.add_argument("--archive", type=Path, default=None, help="Use an existing MAGI-release zip instead of building one")
    parser.add_argument("--force", action="store_true", help="overwrite existing artifacts")
    parser.add_argument("--no-dmg", action="store_true", help="skip DMG creation")
    parser.add_argument("--windows-exe", action="store_true", help="build MAGI-Setup.exe when running on Windows")
    parser.add_argument("--json", action="store_true")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    payload = build_installers(args)
    if args.json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        print(f"MAGI installers: {'OK' if payload['ok'] else 'CHECK NEEDED'}")
        print(f"Manifest: {payload['manifest']}")
        print(f"macOS app: {payload['macos']['app']}")
        print(f"macOS DMG: {payload['macos']['dmg']['path']}")
        print(f"Windows payload: {payload['windows']['zip']}")
        if payload["windows"]["exe"]["path"]:
            print(f"Windows EXE: {payload['windows']['exe']['path']}")
    return 0 if payload["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
