from __future__ import annotations

from flask import Blueprint, jsonify, render_template, request
from flask_login import current_user, login_required

from api.sentencing_trends import (
    search_public_judgment_candidates,
    search_sentencing_trends,
)


sentencing_trends_bp = Blueprint("sentencing_trends", __name__)


def _search_public_judgment_candidates(query: str, **kwargs):
    """Compatibility wrapper for existing callers and tests."""

    return search_public_judgment_candidates(query, **kwargs)


@sentencing_trends_bp.get("/sentencing-trends")
@login_required
def page():
    return render_template("sentencing_trends.html", user=current_user)


@sentencing_trends_bp.get("/api/sentencing-trends/search")
@login_required
def search_api():
    try:
        result = search_sentencing_trends(
            court=request.args.get("court", ""),
            judge=request.args.get("judge", ""),
            offense=request.args.get("offense", ""),
            date_from=request.args.get("date_from", ""),
            date_to=request.args.get("date_to", ""),
            judge_scope=request.args.get("judge_scope", "last_listed"),
            include_mcp=str(request.args.get("include_mcp", "1")).lower() not in {"0", "false", "no", "off"},
            limit=request.args.get("limit", 100),
            mcp_search=_search_public_judgment_candidates,
        )
        response = jsonify(result)
        response.headers["Cache-Control"] = "private, no-store"
        return response
    except ValueError as exc:
        return jsonify({"ok": False, "message": str(exc)}), 400
    except Exception:
        return jsonify({"ok": False, "message": "量刑裁判查詢暫時無法完成，請稍後再試。"}), 503
