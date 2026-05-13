
import json
import subprocess
import os
import glob
import sys
import logging
from flask import Flask, request, jsonify
_MAGI_ROOT = os.path.abspath(os.path.dirname(os.path.dirname(os.path.dirname(__file__))))

app = Flask(__name__)
logger = logging.getLogger("CasperSiriBridge")

# Configuration
SESSIONS_DIR = "/Users/ai/.openclaw/agents/main/sessions"

def get_latest_session_id():
    """Find the most recently updated session ID from sessions.json."""
    try:
        sessions_file = os.path.join(SESSIONS_DIR, "sessions.json")
        if not os.path.exists(sessions_file):
            return None

        with open(sessions_file, 'r') as f:
            data = json.load(f)

        # Find session with latest updatedAt
        latest_session = None
        latest_ts = 0

        for key, session in data.items():
            if session.get("updatedAt", 0) > latest_ts:
                latest_ts = session["updatedAt"]
                latest_session = session.get("sessionId")

        return latest_session
    except Exception as e:
        logger.error("Error finding session: %s", e)
        return None

sys.path.insert(0, _MAGI_ROOT)
try:
    from skills.memory import mem_bridge  # MAGI RAG Integration
except ImportError:
    # Fallback: create a dummy mem_bridge so the bridge can still run without memory
    import types
    mem_bridge = types.ModuleType("mem_bridge")
    mem_bridge.recall = lambda *a, **kw: []
    mem_bridge.remember = lambda *a, **kw: None
    logger.warning("mem_bridge unavailable, memory features disabled")

@app.route('/chat', methods=['POST'])
def chat():
    """Handle HTTP POST from Siri."""
    # Use force=True to parse JSON even if Content-Type is wrong (e.g. application\json)
    data = request.get_json(force=True, silent=True)

    if not data:
        logger.warning("No JSON data received")
        return jsonify({"error": "Invalid JSON or missing Content-Type application/json"}), 400

    logger.debug("Received Data: %s", data)
    user_message = data.get("message", "")

    if not user_message:
        logger.warning("'message' key is missing or empty")
        return jsonify({"error": "No message provided. Did you map the 'message' field to 'Provided Input' in Shortcuts?"}), 400

    logger.info("Siri sent: %s", user_message[:120])

    # --- IRON DOME SECURITY CHECK ---
    try:
        # Ensure MAGI root is in path
        if _MAGI_ROOT not in sys.path:
            sys.path.append(_MAGI_ROOT)

        from skills.iron_dome import core as iron_dome

        safe, violation_msg = iron_dome.is_safe(user_message)
        if not safe:
            logger.warning("IRON DOME BLOCKED: %s", violation_msg)

            # --- RED PHONE ALERT ---
            try:
                sys.path.insert(0, f'{_MAGI_ROOT}/skills/ops')
                import red_phone
                red_phone.alert_iron_dome_violation(
                    violation_type="Security Violation",
                    matched_pattern=violation_msg,
                    user_input=user_message
                )
            except Exception as alert_err:
                logger.error("Red Phone alert failed: %s", alert_err)
            # ----------------------

            return jsonify({
                "error": "Your request was blocked by MAGI security protocols.",
                "reason": violation_msg
            }), 403
    except ImportError as e:
        logger.warning("Iron Dome module not found (%s), proceeding without security check.", e)
    except Exception as e:
        logger.warning("Iron Dome check error: %s", e)
    # --------------------------------



    # --- MAGI MEMORY RECALL (RAG) ---
    try:
        memories = mem_bridge.recall(user_message, top_k=3)
        logger.info("RAG Found %d memories.", len(memories))

        context_str = ""
        if memories:
            context_str += "Here is relevant information from your Memory Database (Keeper):\n"
            for m in memories:
                if m['score'] > 0.5: # Threshold
                    context_str += f"- {m['content']} (Source: {m['source']})\n"

            # If we found valid memories, prepend to message
            if context_str:
                logger.debug("Injecting Context: %s...", context_str[:100])
                user_message = f"Context:\n{context_str}\n\nUser Question:\n{user_message}"
    except Exception as e:
        logger.warning("RAG Error (Non-blocking): %s", e)
    # --------------------------------

    session_id = get_latest_session_id()
    if not session_id:
        return jsonify({"error": "No active OpenClaw session found. Please chat with Casper once in the UI first."}), 503

    try:
        # Increase timeout to 120s for model loading
        # Call openclaw CLI with --deliver to ensure it goes to the agent
        logger.info("Calling OpenClaw CLI for session %s...", session_id)
        result = subprocess.run(
            ["openclaw", "agent", "--session-id", session_id, "--message", user_message],
            capture_output=True,
            text=True,
            timeout=120
        )

        if result.returncode != 0:
            logger.error("CLI Error: %s", result.stderr)
            return jsonify({"error": f"OpenClaw CLI failed: {result.stderr}"}), 500

        output = result.stdout
        logger.info("CLI Output Length: %d", len(output))
        logger.debug("CLI Output Snippet: %s", output[-200:])

        # Return the whole output for now, filtering can be added if needed
        response_text = output.strip() if output.strip() else "Message sent to Casper."

        # Auto-memorize the Siri interaction
        try:
            import threading
            from datetime import datetime
            def _siri_remember():
                ts = datetime.now().strftime("%Y%m%d_%H%M")
                q = data.get("message", "")[:200]
                a = response_text[:800]
                content = f"[Q] {q}\n[A] {a}"
                mem_bridge.remember(content, source=f"chatlog|mode=siri|ts={ts}")
            threading.Thread(target=_siri_remember, daemon=True).start()
        except Exception:
            logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, 165, exc_info=True)

        return jsonify({"response": response_text})

    except subprocess.TimeoutExpired:
        logger.error("Timeout expired after 120s")
        return jsonify({"error": "Casper timed out."}), 504
    except Exception as e:
        logger.error("Unexpected error: %s", e)
        return jsonify({"error": str(e)}), 500

if __name__ == '__main__':
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
    logger.info("Casper-Siri Bridge running on port 5001")
    app.run(host='127.0.0.1', port=5001)
