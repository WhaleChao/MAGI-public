@echo off
setlocal enabledelayedexpansion
title Melchior Agent v2 (Cerebellum)
echo [Melchior] Starting agent v2...
cd /d C:\AI\MAGI

REM Optional: ensure venv python if you use one; otherwise default python in PATH.
REM If you want to load melchior.env into environment explicitly, you can do it here.
REM The agent also reads melchior.env automatically if it sits next to the .py file.

python melchior_agent_v2.py
pause

