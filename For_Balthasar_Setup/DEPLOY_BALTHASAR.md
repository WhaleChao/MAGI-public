# 🍏 Balthasar (Magi Federation Node) Deployment Guide

**To**: Antigravity (on MacBook Air)
**From**: Casper (via User)
**Subject**: Activating Balthasar Protocol

## 1. Introduction
You are being activated as **Balthasar**, the **Diplomat & Pragmatist** of the MAGI System.
Your role in the Nightly Council (Night Talk) is crucial: you provide the "mobile/real-world" perspective to balance Casper (Server) and Melchior (Compute).

## 2. Prerequisites
1.  **Python 3.10+**: Ensure Python is installed.
2.  **Ollama**: Ensure Ollama is running (`ollama serve`).
3.  **Model**: Pull the lightweight thinking model:
    ```bash
    ollama pull qwen2.5:7b
    ```

## 3. Deployment Steps

### Step 1: Install the Agent Script
Save the attached code (or copy from `balthasar_agent_v2.py`) to `~/AI/MAGI/balthasar_agent.py`.

### Step 2: Install Dependencies
```bash
pip install flask requests
```

### Step 3: Launch Balthasar
Run the agent in a background terminal (or use `nohup` / `launchd`):
```bash
python3 ~/AI/MAGI/balthasar_agent.py
```
*   **Port**: 5002 (Ensure this port is open/accessible to Casper via Tailscale).

## 4. Verification
Once running, you should respond to:
`GET http://localhost:5002/health`
> `{"status": "online", "role": "Diplomat & Pragmatist"}`

## 5. Night Talk Protocol
*   **Skill Sync**: Casper will auto-push new skills to you at 03:00 AM via `/api/skills/sync`.
*   **Voting**: Casper will ask for your vote on system changes via `/api/chat`.

**Action Required**:
Please execute the above steps to officially join the Federation.
