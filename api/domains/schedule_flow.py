"""
Schedule / calendar queries extracted from Orchestrator.

All functions accept `orch` (the Orchestrator instance) instead of `self`.
"""
from __future__ import annotations

import logging
import os

logger = logging.getLogger("Orchestrator")


def get_schedule(orch) -> str:
    """Get upcoming meetings from law_firm_data database + Google Calendar."""
    try:
        from skills.law_firm.manage_meetings import list_meetings
        from datetime import datetime, timedelta, timezone

        result = list_meetings()

        db_items = []
        if result.get("success") and result.get("data"):
            meetings = result["data"]
            for m in meetings[:7]:
                dt_str = m.get('datetime', '')
                if dt_str:
                    try:
                        dt = datetime.fromisoformat(dt_str)
                        date_fmt = dt.strftime("%m/%d %H:%M")
                    except Exception:
                        date_fmt = dt_str[:16]
                else:
                    date_fmt = "待定"

                meeting_type = m.get('type', '會議')
                client = m.get('client_name', '')
                location = m.get('location', '')

                line = f"• **{date_fmt}** - {meeting_type}"
                if client:
                    line += f" ({client})"
                if location:
                    line += f" @ {location}"
                db_items.append(line)

        # ── Google Calendar ──
        gcal_items = []
        try:
            import importlib
            import importlib.util
            from api.runtime_paths import get_config_path
            credentials_path = str(get_config_path("credentials.json"))
            token_path = str(get_config_path("google_calendar_token.json"))
            spec = importlib.util.spec_from_file_location(
                "osc_orchestrator_action",
                os.path.join(os.environ.get("MAGI_ROOT_DIR", ""), "skills", "osc-orchestrator", "action.py"),
            )
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)
            svc = mod._build_google_calendar_service(credentials_path, token_path, interactive=False)
            if svc.get("ok") and svc.get("service"):
                service = svc["service"]
                tz = timezone(timedelta(hours=8))
                now = datetime.now(tz)
                today_start = now.replace(hour=0, minute=0, second=0, microsecond=0).isoformat()
                today_end = (now + timedelta(days=7)).replace(hour=23, minute=59, second=59, microsecond=0).isoformat()
                try:
                    _cal_list = service.calendarList().list().execute().get("items", [])
                    _cal_ids = [c["id"] for c in _cal_list if c.get("id")]
                except Exception:
                    _cal_ids = ["primary"]
                if not _cal_ids:
                    _cal_ids = ["primary"]
                _all_events = []
                _seen_ids = set()
                for _cid in _cal_ids:
                    try:
                        _r = service.events().list(
                            calendarId=_cid,
                            timeMin=today_start,
                            timeMax=today_end,
                            singleEvents=True,
                            orderBy='startTime',
                            maxResults=20,
                        ).execute()
                        for _ev in _r.get('items', []):
                            _eid = _ev.get('id', '')
                            if _eid not in _seen_ids:
                                _seen_ids.add(_eid)
                                _all_events.append(_ev)
                    except Exception:
                        pass
                _all_events.sort(key=lambda e: e.get('start', {}).get('dateTime', e.get('start', {}).get('date', '')))
                for ev in _all_events:
                    start_raw = ev['start'].get('dateTime', ev['start'].get('date', ''))
                    summary = ev.get('summary', '(無標題)')
                    ev_location = ev.get('location', '')
                    try:
                        dt_ev = datetime.fromisoformat(start_raw)
                        date_fmt = dt_ev.strftime("%m/%d %H:%M")
                    except Exception:
                        date_fmt = start_raw[:16] if start_raw else "待定"
                    line = f"• **{date_fmt}** - {summary}"
                    if ev_location:
                        line += f" @ {ev_location}"
                    gcal_items.append(line)
        except Exception as e:
            logger.warning(f"Google Calendar query failed: {e}")

        all_items = db_items + gcal_items
        if all_items:
            response = "📅 **近期行程**\n\n"
            response += "\n".join(all_items) + "\n"
            if gcal_items:
                response += f"\n_(含 {len(gcal_items)} 筆 Google 日曆行程)_"
            return response
        else:
            return "📅 目前沒有排定的行程。"

    except Exception as e:
        logger.error(f"❌ Schedule query error: {e}")
        return f"⚠️ 無法讀取行程: {e}"
