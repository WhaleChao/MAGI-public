# 🪟 DEPLOY_MELCHIOR.md (Engineer Setup Guide)

**Target Machine**: Windows PC (Melchior)
**Hardware**: NVIDIA RTX 3060 (12GB)
**Role**: Distributed Compute Node & Image Generator

---

## 🔗 Architecture Overview

Melchior has two "Brain Modes" controlled by Casper:

1.  **Distributed Mode (Default)**:
    -   Runs `rpc-server.exe`.
    -   Contributes GPU VRAM to Casper to run **GLM-4 70B**.
    -   Used for: Chatting, Coding, Logic.

2.  **Engineer Mode (On Demand)**:
    -   Runs local LLM inference (Ollama or MLX).
    -   Used for: **Image Generation** or specific local tasks.
    -   Casper will auto-switch Melchior to this mode when you ask to "Draw".

---

## 🛠️ Step 1: Install Dependencies

1.  **Python 3.11+**: Install from python.org (Check "Add to PATH").
2.  **Git**: Install from git-scm.com.
3.  **Llama.cpp (RPC Backend)**:
    -   Download `llama-b3472-bin-win-cuda-cu12.2.0-x64.zip` (or latest) from [llama.cpp releases](https://github.com/ggerganov/llama.cpp/releases).
    -   Extract to `C:\AI\llama.cpp`.
    -   **Verify**: You should see `rpc-server.exe` in that folder.
4.  **Ollama (Engineer Backend, optional)**:
    -   Install from ollama.com (will be replaced by MLX in future).

---

## 🤖 Step 2: Install The Cerebellum (Agent Script)

This script allows Casper to remotely switch Melchior's mode.

1.  Create a folder `C:\AI\MAGI`.
2.  Copy the `melchior_agent.py` file (provided by Casper) into this folder.
3.  Install Python libs:
    ```powershell
    pip install flask requests
    ```

---

## ⚙️ Step 3: Configure The Agent

Open `C:\AI\MAGI\melchior_agent.py` and edit the configuration at the top:

```python
# CONFIGURATION
RPC_BINARY_PATH = r"C:\AI\llama.cpp\bin\rpc-server.exe"  # Make sure this path is correct!
RPC_PORT = "50052"
```

---

## 🚀 Step 4: Launch!

Create a startup script `start_melchior.bat` on your Desktop:

```batch
@echo off
title Melchior Agent (Cerebellum)
echo 🤖 Awakening Melchior...
cd /d C:\AI\MAGI
python melchior_agent.py
pause
```

**Run this script.**
- You should see: `🤖 Melchior Agent Listening on Port 5002...`
- Keep this window open.

---

## 🔌 Step 5: Connect to Casper

1.  Ensure Melchior and Casper are on the same Tailscale network.
2.  Melchior's IP should be configured in Casper's `brain_manager/action.py` (Default: `100.116.54.16`).
3.  **Test it**: Ask Casper "System status". It should eventually see Melchior online.
