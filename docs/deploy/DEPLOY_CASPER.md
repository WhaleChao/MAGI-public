# 🍎 DEPLOY_CASPER.md (The Governor's Setup)

> **Target Machine**: Mac Mini M4
> **Role**: Governance, Intent Recognition, Business Monitoring
> **Network IP**: Follow `CONNECTION_SECRETS.md`

## 1. Environment Check (Existing OpenClaw)
Since OpenClaw is already running on Casper:

1.  **Check Health**:
    Run `openclaw doctor` in your terminal to ensure the existing installation is healthy.
    
2.  **Update (Optional but Recommended)**:
    ```bash
    npm install -g openclaw@latest
    openclaw update
    ```

3.  **Install Database Drivers**:
    OpenClaw needs to talk to our Keeper (MariaDB).
    ```bash
    # Install MySQL/MariaDB client for the active agent
    npm install mysql2
    ```

## 2. Federation Configuration
We need to tell the existing OpenClaw about the Federation.

**Edit your OpenClaw configuration** (usually `~/.openclaw/config.json` or `.env` depending on your setup). Add/Update these keys:

```json
{
  "agent": {
    "name": "Casper",
    "role": "Governor"
  },
  "database": {
    "host": "100.121.61.74", 
    "user": "magi_agent",
    "password": "Magi_IronDome_2026!",
    "database": "magi_brain"
  },
  "federation": {
    "keeper_ip": "100.121.61.74",
    "legacy_db": "law_firm_data"
  }
}
```

> **Note**: If you run OpenClaw via `.env`, add the corresponding `DB_HOST`, `DB_USER`, etc. variables there.

## 2. AI Engine (Ollama)
Casper needs the "Thinking" and "Embedding" models.

```bash
# 1. Install Ollama from ollama.com, then pull models:
ollama pull qwen2.5:14b      # Main Reasoning
ollama pull nomic-embed-text # Memory/RAG
ollama pull llama-3-taiwan-8b-instruct # Local Culture/Language
```

## 3. Configuration (.env)
Create `~/Desktop/MAGI/MAGI/.env` (This configures the **NanoClaw Engine**):

```env
# --- NanoClaw Kernel Settings ---
AGENT_NAME=Casper
ROLE=Governor
SYSTEM_CORE=NanoClaw_v2.6

# Database (Keeper)
DB_HOST=100.121.61.74  # Tailscale IP from Keeper
DB_PORT=3306
DB_USER=magi_agent
DB_PASSWORD=Magi_IronDome_2026!
DB_NAME=magi_brain
TARGET_DB=law_firm_data

# LINE Messaging API
LINE_CHANNEL_ACCESS_TOKEN=...
LINE_CHANNEL_SECRET=...
```

## 4. The Heartbeat (Cron)
Casper is the "Clock Tower". He must wake up every 15 minutes.

1.  Type `crontab -e`
2.  Add this line:
    ```bash
    */15 * * * * /Users/<USER>/Desktop/MAGI/venv/bin/python /Users/<USER>/Desktop/MAGI/core/casper_heartbeat.py >> /tmp/magi_heartbeat.log 2>&1
    ```

## 5. First Contact (Admin Binding)
Once everything is running:
1.  Run `python3 generate_binding_token.py`
2.  Send the `MAGI-XXXX` code to the LINE Bot.
3.  Casper will recognize you as "The One".

## 6. Apple Intelligence & Siri Shortcuts (The Voice)
Start the "No Wake Word" experience with Siri:

1.  Open **Shortcuts App** > **New Shortcut**.
2.  Name it: **"Hey Casper"** (or just "Casper").
3.  Add Action: **Get Contents of URL**.
    *   **URL**: `http://localhost:5001/chat` (Mac Port 5001)
    *   **Method**: `POST`
    *   **Headers**: `Content-Type: application/json`
    *   **Request Body**:
        ```json
        { "message": "Provided Input" }
        ```
4.  **Usage**: *"Siri, Casper... Check the audit logs."* -> Casper analyzes intent and replies.
