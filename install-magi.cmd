@echo off
setlocal
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0install-magi.ps1" %*
exit /b %ERRORLEVEL%
