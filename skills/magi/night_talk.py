import logging
import os
import time
from datetime import datetime

import requests

from skills.brain_manager.action import switch_brain_mode
from skills.magi.council_approval import is_core_change, queue_core_change_for_approval

# Import Bridges
_MAGI_ROOT = os.path.abspath(os.path.dirname(os.path.dirname(os.path.dirname(__file__))))

try:
    from skills.bridge import melchior_client
    from skills.bridge import melchior_bridge
    from skills.bridge.inference_gateway import InferenceGateway
except ImportError:
    melchior_client = None
    melchior_bridge = None
    InferenceGateway = None

try:
    from skills.bridge import watcher_bridge
except ImportError:
    watcher_bridge = None

try:
    from skills.bridge import balthasar_bridge
except ImportError:
    balthasar_bridge = None

# Logging
logger = logging.getLogger("NightTalk")
if not logging.getLogger().handlers:
    logging.basicConfig(level=logging.INFO)

# Configuration
AGENDA_FILE = f"{_MAGI_ROOT}/nightly_council_agenda.md"
MINUTES_FILE = f"{_MAGI_ROOT}/nightly_council_minutes.md"
def wait_for_casper(timeout=60):
    """Check that local inference (oMLX or Ollama) is reachable."""
    start_time = time.time()
    while time.time() - start_time < timeout:
        try:
            # Try oMLX first (port 8080)
            omlx_port = os.environ.get("MAGI_OMLX_PORT", "8080")
            resp = requests.get(f"http://127.0.0.1:{omlx_port}/v1/models", timeout=2)
            if resp.status_code == 200:
                return True
        except Exception:
            logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, 51, exc_info=True)
        try:
            # Fallback: check Ollama
            try:
                from api.routing.service_registry import get_service_url as _gsurl
                _omlx_base = _gsurl("omlx_inference")
            except Exception:
                _omlx_base = "http://127.0.0.1:8080"
            resp = requests.get(f"{_omlx_base}/v1/models", timeout=2)
            if resp.status_code == 200:
                return True
        except Exception:
            logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, 58, exc_info=True)
        time.sleep(2)
    return False


def get_casper_thought(prompt, system_prompt="You are Casper, the Chairman of the Nightly Council."):
    """Ask Casper via InferenceGateway (oMLX → Ollama fallback)."""
    try:
        if InferenceGateway is not None:
            r = InferenceGateway().dispatch(
                prompt=prompt,
                system=system_prompt,
                task_type="night_talk",
                timeout=60,
                temperature=0.3,
                max_tokens=500,
                tc_review=False,
            )
            if r.get("success") and r.get("response"):
                return r["response"]
        # Direct fallback if gateway unavailable
        if melchior_client:
            omlx_chat = getattr(melchior_client, "_chat_omlx", None)
            omlx_avail = getattr(melchior_client, "_omlx_available", None)
            if callable(omlx_chat) and callable(omlx_avail) and omlx_avail():
                r = omlx_chat(prompt=prompt, system_prompt=system_prompt, temperature=0.3, max_tokens=500, timeout=60)
                if r.get("success") and r.get("response"):
                    return r["response"]
        return "(Offline: no inference backend available)"
    except Exception as e:
        return f"(Error: {e})"


def _notify_pending_core_change(item: dict, quorum_rule: str):
    """
    Send core-change pending approval to DC/LINE in plain language.
    Falls back gracefully if webhook is not configured.
    """
    try:
        from skills.ops.red_phone import alert_admin

        approval_id = item.get("id", "?")
        plain = item.get("plain_summary") or ""
        if plain:
            body = plain
        else:
            body = (
                f"問題：{item.get('issue','')[:200]}\n"
                f"提案：{(item.get('proposal') or '')[:400]}"
            )

        msg = (
            "🟡 夜議通過一項核心改動，等待您審批\n"
            "─────────────────\n"
            f"{body}\n"
            "─────────────────\n"
            f"決議規則：{quorum_rule}\n"
            f"審批碼：{approval_id}\n\n"
            "✅ 批准請回覆：批准 " + approval_id + "\n"
            "❌ 拒絕請回覆：拒絕 " + approval_id + " [原因]"
        )
        return alert_admin(msg, severity="warning", topic_key="alert")
    except Exception as e:
        logger.warning(f"Notify pending core change failed: {e}")
        return {"line": False, "discord": False, "error": str(e)}


def _to_yes(v: str) -> bool:
    return (v or "").strip().lower().startswith("yes")


def _vote_casper_safety(text: str) -> tuple[str, str]:
    body = (text or "").lower()
    risk_flags = ["delete", "drop table", "rm -rf", "bypass", "disable safety", "危險", "破壞"]
    if any(flag in body for flag in risk_flags):
        return "No", "safety risk detected"
    if len(body.strip()) < 12:
        return "No", "insufficient analysis"
    return "Yes", "safety/compliance acceptable"


def _vote_melchior_engineering(text: str) -> tuple[str, str]:
    body = (text or "").lower()
    good_signals = ["fix", "patch", "test", "validate", "rollback", "fallback", "monitor", "retry", "修復", "驗證", "回滾"]
    if any(sig in body for sig in good_signals) and len(body) > 24:
        return "Yes", "implementation looks actionable"
    return "No", "proposal not actionable enough"


def _vote_balthasar_ux(text: str) -> tuple[str, str]:
    body = (text or "").lower()
    ux_signals = ["user", "service", "latency", "notify", "summary", "fallback", "使用者", "體驗", "通知", "穩定"]
    if any(sig in body for sig in ux_signals):
        return "Yes", "ux/operations impact considered"
    return "No", "ux/operations impact unclear"


def _deliberate(issue: str, c_analysis: str, m_proposal: str) -> tuple[str, str, str]:
    """
    One round of deliberation: Casper reviews Melchior's proposal and may raise
    concerns; if so, Melchior revises. Returns (c_challenge, m_revised, summary).
    """
    challenge_prompt = (
        f"你是 Casper（主席），你剛收到 Melchior 的工程提案。\n"
        f"原始問題：{issue}\n"
        f"你的初步分析：{c_analysis}\n"
        f"Melchior 的提案：{m_proposal}\n\n"
        "請用 1-3 句審查這份提案：是否有遺漏回滾機制？測試計畫？安全疑慮？"
        "如果提案可接受，直接說「提案可接受」；如果有顧慮，清楚指出問題。"
    )
    c_challenge = get_casper_thought(challenge_prompt)

    # If Casper says acceptable or no keywords of concern → no revision needed
    concern_flags = ["遺漏", "缺少", "問題", "疑慮", "建議", "需要", "should", "missing", "concern"]
    needs_revision = any(kw in (c_challenge or "").lower() for kw in concern_flags)
    acceptable_flags = ["可接受", "acceptable", "looks good", "沒有問題", "no issue"]
    if any(kw in (c_challenge or "").lower() for kw in acceptable_flags):
        needs_revision = False

    m_revised = m_proposal
    if needs_revision and InferenceGateway is not None:
        try:
            revision_prompt = (
                f"你是 Melchior（工程師）。Casper 對你的提案提出了以下顧慮：\n{c_challenge}\n\n"
                f"原始提案：{m_proposal}\n\n"
                "請修訂提案以回應 Casper 的顧慮，確保包含：回滾機制、測試計畫、可觀測性。限 300 字。"
            )
            r = InferenceGateway().dispatch(
                prompt=revision_prompt,
                task_type="night_talk",
                timeout=90,
                tc_review=False,
            )
            if r.get("success") and r.get("response"):
                m_revised = r["response"]
        except Exception:
            logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, 194, exc_info=True)  # keep original proposal on failure

    plain_summary = (
        f"問題：{issue[:120]}\n"
        f"提案摘要：{m_revised[:300]}\n"
        f"Casper 審查：{c_challenge[:180]}"
    )
    return c_challenge, m_revised, plain_summary


def start_night_talk():
    """
    Initiates the Nightly Council with resilient quorum protocol.
    Decision rules:
    - Normal: 3/3 unanimous (Casper + Melchior + Balthasar)
    - Fallback: if Balthasar laptop offline, 2/2 unanimous (Casper + Melchior)
    - Any core change must be sent to DC and marked pending approval.
    """
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M")
    minutes_text = f"# 🌙 Nightly Council Minutes ({timestamp})\n\n"

    logger.info("🌙 Initiating Night Talk Protocol...")
    switch_brain_mode("local", force=True)

    # --- PHASE 1: ATTENDANCE CHECK (三哲人) ---
    logger.info("1. Attendance Check...")

    # Watcher 已從夜議必要條件剔除，僅作可選記錄
    watcher_online = False
    if watcher_bridge:
        try:
            is_online, _ = watcher_bridge.check_health()
            watcher_online = bool(is_online)
        except Exception:
            logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, 228, exc_info=True)

    if not wait_for_casper():
        return "❌ Casper (Chairman) Failed to Start."

    melchior_online = False
    balthasar_online = False
    try:
        if melchior_client and melchior_client.check_health().get("online"):
            melchior_online = True
    except Exception:
        logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, 239, exc_info=True)

    try:
        if balthasar_bridge:
            # Night council: Balthasar may join if laptop is reachable (force remote check).
            is_online, _ = balthasar_bridge.check_health(force_remote=True)
            if is_online:
                balthasar_online = True
    except Exception:
        logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, 248, exc_info=True)

    # Melchior is mandatory for council resolution.
    if not melchior_online:
        minutes_text += (
            "**Roll Call**: Casper (Present), Melchior (Absent), Balthasar "
            f"({'Present' if balthasar_online else 'Absent'}).\n"
            f"**Watcher**: {'Present (Recording)' if watcher_online else 'Offline (非必要)'}.\n"
            "❌ **會議流會 (Adjourned)**\n原因：Melchior 缺席，無法形成有效決議。\n"
        )
        return minutes_text

    fallback_mode = not balthasar_online
    quorum_rule = "2/2 fallback (Casper+Melchior)" if fallback_mode else "3/3 unanimous"
    quorum_display = "2/2" if fallback_mode else "3/3"

    minutes_text += (
        f"**Roll Call**: Casper (Present), Melchior (Present), "
        f"Balthasar ({'Present' if balthasar_online else 'Absent'}).\n"
    )
    minutes_text += f"**Watcher**: {'Present (Recording)' if watcher_online else 'Offline (非必要)'}.\n"
    minutes_text += f"**Quorum Rule**: {quorum_rule} ({quorum_display})\n\n"

    # --- PHASE 1.5: KNOWLEDGE TRANSFER (Skill Sync) ---
    logger.info("1.5 Knowledge Transfer (Skill Sync)...")
    minutes_text += "**Knowledge Transfer**: Initiated.\n"

    try:
        import shutil
        import tempfile

        skills_dir = f"{_MAGI_ROOT}/skills"
        temp_dir = tempfile.gettempdir()
        zip_path = os.path.join(temp_dir, "magi_skills_sync")
        shutil.make_archive(zip_path, "zip", skills_dir)
        final_zip = zip_path + ".zip"

        minutes_text += f"- **Package**: Created ({os.path.getsize(final_zip) / 1024:.1f} KB)\n"

        minutes_text += "- **Melchior**: Sending... "
        res = melchior_bridge.sync_skills(final_zip) if melchior_bridge else {"success": False, "error": "bridge missing"}
        if res.get("success"):
            minutes_text += "✅ Synced.\n"
        else:
            minutes_text += f"❌ Failed ({res.get('error')}).\n"

        if balthasar_online and balthasar_bridge:
            minutes_text += "- **Balthasar**: Sending... "
            b_res = balthasar_bridge.sync_skills(final_zip, force_remote=True)
            if b_res.get("success"):
                minutes_text += "✅ Synced.\n"
            else:
                minutes_text += f"⚠️ Failed ({b_res.get('error')}).\n"
        else:
            minutes_text += "- **Balthasar**: Skipped (Offline, fallback mode).\n"

        minutes_text += "\n"
    except Exception as e:
        logger.error(f"❌ Knowledge Transfer Failed: {e}")
        minutes_text += f"❌ **Knowledge Transfer Error**: {e}\n\n"

    # --- PHASE 2: AGENDA & ANOMALIES ---
    issues = []
    try:
        anomalies = watcher_bridge.get_anomalies(unresolved_only=True) if watcher_bridge else {}
        if isinstance(anomalies, dict) and anomalies.get("anomalies"):
            for a in anomalies["anomalies"]:
                issues.append(f"⚠️ [Watcher] {a}")
    except Exception:
        logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, 317, exc_info=True)

    if os.path.exists(AGENDA_FILE):
        with open(AGENDA_FILE, "r", encoding="utf-8") as f:
            for line in f.readlines():
                if "Issue" in line or "Action" in line:
                    issues.append(line.strip())

    # --- 每日 1% 改善：自動從系統健康和日誌中提取改善議題 ---
    try:
        _kaizen_prompt = (
            "你是 MAGI 系統的首席架構師。今天的目標是找出一個可以立即改善的具體項目，"
            "實現「每天進步 1%」的持續改善精神。\n\n"
            "請基於以下系統現況，提出 1-2 個具體可執行的改善提案：\n"
        )
        # Gather system context for kaizen analysis
        _kaizen_ctx = []
        try:
            import requests as _req
            _h = _req.get("http://127.0.0.1:5002/health", timeout=5).json()
            _sys = _h.get("system", {})
            _kaizen_ctx.append(f"RAM: {_sys.get('memory_percent', '?')}%, CPU: {_sys.get('cpu_percent', '?')}%, Disk: {_sys.get('disk_free_gb', '?')}GB free")
            _kaizen_ctx.append(f"FAISS vectors: {_h.get('faiss', {}).get('vectors', '?')}")
        except Exception:
            logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, 341, exc_info=True)
        # Recent errors from daemon.log (last 200 lines)
        try:
            _daemon_log = f"{_MAGI_ROOT}/daemon.log"
            if os.path.exists(_daemon_log):
                with open(_daemon_log, "r", encoding="utf-8", errors="ignore") as _f:
                    _lines = _f.readlines()[-200:]
                _errs = [l.strip() for l in _lines if "ERROR" in l or "CRITICAL" in l]
                if _errs:
                    _kaizen_ctx.append(f"最近錯誤 ({len(_errs)} 筆): " + "; ".join(_errs[-3:])[:500])
                else:
                    _kaizen_ctx.append("最近無錯誤")
        except Exception:
            logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, 354, exc_info=True)
        # Latest night patrol report
        try:
            _report_dir = f"{_MAGI_ROOT}/reports"
            _reports = sorted(
                [f for f in os.listdir(_report_dir) if f.startswith("night_patrol_")],
                reverse=True,
            )
            if _reports:
                with open(os.path.join(_report_dir, _reports[0]), "r", encoding="utf-8") as _f:
                    _kaizen_ctx.append(f"最新巡邏報告摘要: {_f.read()[:600]}")
        except Exception:
            logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, 366, exc_info=True)

        _kaizen_prompt += "\n".join(_kaizen_ctx)
        _kaizen_prompt += (
            "\n\n請用以下格式回覆（每個提案一行）：\n"
            "Issue: [問題描述] | Action: [具體行動]"
        )
        _kaizen_response = get_casper_thought(_kaizen_prompt, system_prompt=(
            "你是 MAGI 系統改善顧問。專注於：效能優化、錯誤預防、自動化改進、"
            "使用者體驗提升。每個提案必須具體可執行，不要空泛建議。用繁體中文回答。"
        ))
        if _kaizen_response and "Issue" in _kaizen_response:
            for _kline in _kaizen_response.split("\n"):
                _kline = _kline.strip()
                if _kline and ("Issue" in _kline or "Action" in _kline):
                    issues.append(f"📈 [每日1%改善] {_kline}")
        elif _kaizen_response and len(_kaizen_response) > 20:
            issues.append(f"📈 [每日1%改善] {_kaizen_response[:200]}")
        logger.info(f"📈 Kaizen analysis generated {len([i for i in issues if '每日1%' in i])} improvement items")
    except Exception as _ke:
        logger.warning(f"Kaizen analysis failed: {_ke}")

    if not issues:
        issues = ["System Health Optimizations (General Review)"]

    minutes_text += f"**Agenda**: {len(issues)} Items.\n\n"

    # --- PHASE 3: COUNCIL VOTING ---
    pending_core_count = 0
    for i, issue in enumerate(issues, 1):
        minutes_text += f"### Item {i}: {issue[:70]}...\n"

        # 1. CASPER (Analyze + vote)
        c_res = get_casper_thought(
            f"Analyze issue with safety-first governance lens (Casper persona): {issue}"
        )
        c_vote, c_reason = _vote_casper_safety(c_res)
        minutes_text += f"**👻 Casper**: {c_res}\n"

        # 2. MELCHIOR (Propose + vote)
        m_vote = "No"
        m_reason = "offline"
        proposal = "None"
        try:
            prompt = (
                f"Casper analysis: {c_res}\nIssue: {issue}\n"
                "Provide engineering proposal with tests, rollback, and observability."
            )
            if InferenceGateway is not None:
                m_res = InferenceGateway().dispatch(
                    prompt=prompt,
                    task_type="night_talk",
                    timeout=120,
                    tc_review=False,
                )
            else:
                m_res = {
                    "success": False,
                    "response": "None",
                    "error": "inference_gateway_unavailable",
                    "degraded": True,
                    "route": "failed_all",
                }
            proposal = m_res.get("response", "None")
            m_vote, m_reason = _vote_melchior_engineering(proposal)
        except Exception:
            m_vote, m_reason = "No", "offline"
        minutes_text += f"**🤖 Melchior**: {proposal}\n"

        # 2.5. DELIBERATION ROUND: Casper challenges proposal, Melchior may revise
        c_challenge, proposal, plain_summary = _deliberate(issue, c_res, proposal)
        minutes_text += f"**💬 Casper 審查**: {c_challenge}\n"
        if proposal != m_res.get("response", "None"):
            minutes_text += f"**🔧 Melchior 修訂**: {proposal[:300]}...\n"
            # Re-evaluate votes against revised proposal
            m_vote, m_reason = _vote_melchior_engineering(proposal)

        # 3. BALTHASAR (Ratify vote or fallback)
        if balthasar_online:
            try:
                b_vote, b_reason = _vote_balthasar_ux(proposal)
                minutes_text += f"**🍏 Balthasar**: {b_reason}.\n"
            except Exception:
                b_vote = "No (Evaluation Error)"
        else:
            b_vote = "N/A (Offline Fallback)"
            minutes_text += "**🍏 Balthasar**: Offline -> fallback 2/2 rule enabled.\n"

        minutes_text += (
            f"**Votes**: Casper({c_vote}:{c_reason}) | "
            f"Melchior({m_vote}:{m_reason}) | Balthasar({b_vote})\n"
        )

        # 4. Pass/Veto based on rule
        if balthasar_online:
            passed = _to_yes(c_vote) and _to_yes(m_vote) and _to_yes(b_vote)
            pass_rule = "3/3"
        else:
            passed = _to_yes(c_vote) and _to_yes(m_vote)
            pass_rule = "2/2 fallback"

        if not passed:
            minutes_text += f"🚫 **VETOED** ({pass_rule} not met).\n---\n"
            continue

        # 5. Core change requires explicit user approval via LINE/DC
        if is_core_change(issue=issue, proposal=proposal):
            queue = queue_core_change_for_approval(
                issue=issue,
                proposal=proposal,
                votes={"casper": c_vote, "melchior": m_vote, "balthasar": b_vote},
                quorum_rule=pass_rule,
                source="nightly_council",
            )
            if queue.get("success"):
                pending_core_count += 1
                item = queue.get("item", {})
                item["plain_summary"] = plain_summary  # attach human-readable summary
                notify = _notify_pending_core_change(item, pass_rule)
                minutes_text += (
                    f"🟡 **PASSED ({pass_rule}) BUT CORE CHANGE IS PENDING APPROVAL**.\n"
                    f"- Approval ID: `{item.get('id')}`\n"
                    f"- DC Notify: line={notify.get('line')} discord={notify.get('discord')}\n"
                    "⚠️ 未獲核准前不得執行此核心改動。\n"
                )
            else:
                minutes_text += "❌ 核心改動待審建立失敗，視同未通過執行門檻。\n"
        else:
            # Non-core change passed → auto-execute immediately
            minutes_text += f"✅ **PASSED ({pass_rule})**."
            try:
                from skills.magi.council_executor import execute_approved_change
                _exec_item = {"id": f"auto-{i}-{datetime.now().strftime('%Y%m%d%H%M%S')}",
                              "issue": issue, "proposal": proposal}
                _exec_r = execute_approved_change(_exec_item)
                if _exec_r.get("success"):
                    _files = ", ".join(_exec_r.get("patches_applied", []))
                    minutes_text += f" Auto-executed: {_files}\n"
                else:
                    minutes_text += f" Auto-exec skipped: {_exec_r.get('error', '?')[:120]}\n"
            except Exception as _exec_e:
                minutes_text += f" Auto-exec error: {_exec_e}\n"
        minutes_text += "---\n"

    if pending_core_count:
        minutes_text += f"\n**Pending Core Changes**: {pending_core_count}\n"

    # --- PHASE 4: ARCHIVE ---
    try:
        with open(MINUTES_FILE, "a", encoding="utf-8") as f:
            f.write(minutes_text + "\n")

        with open(AGENDA_FILE, "w", encoding="utf-8") as f:
            f.write("# 🌙 Nightly Council Agenda\n\n(Waiting for new issues...)\n")

        logger.info("✅ Night Talk Concluded & Recorded.")
    except Exception as e:
        logger.error(f"❌ Failed to archive: {e}")

    # --- PHASE 5: RECOVERY ---
    logger.info("🌅 Sunrise Protocol Initiated. Restoring Distributed Brain...")
    time.sleep(5)
    res = switch_brain_mode("distributed", force=True)
    logger.info(f"☀️ System Restoration: {res}")

    minutes_text += f"\n---\n**System Restoration**: {res}\n"
    return minutes_text


if __name__ == "__main__":
    print(start_night_talk())
