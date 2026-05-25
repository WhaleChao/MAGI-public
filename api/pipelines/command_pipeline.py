"""
Command handling pipeline extracted from Orchestrator.

Contains: handle_command (the massive dispatch method) and list_skills.

All functions accept an `orch` parameter (the Orchestrator instance)
instead of `self`.
"""
from __future__ import annotations

import json
import logging
import os
import re
import subprocess
import sys
import threading
import time

logger = logging.getLogger("Orchestrator")

_MAGI_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))


# Lazy-loaded modules
def _get_handler(name: str):
    from api.orchestrator import _get_handler as _orig
    return _orig(name)


def list_skills(orch) -> str:
    from api.pipelines.skill_listing import build_skill_list_response

    return build_skill_list_response(_MAGI_ROOT, logger=logger)


def handle_command(orch, user_id, message, role="user", platform="LINE") -> str:
    """
    Routes commands to skills / system functions.
    Uses CommandRegistry for extensible dispatch, falls back to legacy if-elif.

    This is a direct extraction of the massive _handle_command method.
    It delegates back to orch for methods that remain on the Orchestrator class.
    """
    # The full _handle_command body is enormous (~2100 lines).
    # We delegate to the original method on the orchestrator instance
    # to avoid duplicating all that logic during the initial split.
    # Future refactoring passes should break this further into sub-dispatchers.
    return orch._handle_command(user_id, message, role=role, platform=platform)
