@echo off
REM ============================================================
REM MAGI Startup Script for Windows
REM ============================================================
title MAGI - Multi-Agent Governance Infrastructure

cd /d "%~dp0"

REM Check Python venv
if exist "venv\Scripts\python.exe" (
    set PYTHON=venv\Scripts\python.exe
) else if exist ".venv\Scripts\python.exe" (
    set PYTHON=.venv\Scripts\python.exe
) else (
    echo [ERROR] Python venv not found. Please run:
    echo   python -m venv venv
    echo   venv\Scripts\pip install -r requirements.txt
    pause
    exit /b 1
)

REM Check if .env exists
if not exist ".env" (
    echo [INFO] First-time setup detected. Launching Setup Wizard...
    %PYTHON% setup_wizard.py
)

REM Start MAGI daemon
echo [INFO] Starting MAGI...
%PYTHON% daemon.py
