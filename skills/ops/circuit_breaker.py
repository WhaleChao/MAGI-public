"""
CIRCUIT BREAKER MODULE (共識熔斷器)
===================================
Implements the consensus circuit breaker from MAGI Constitution.
After 3 consecutive failed consensus attempts, system enters 4-hour cooldown.
"""

import os
_MAGI_ROOT = os.path.abspath(os.path.dirname(os.path.dirname(os.path.dirname(__file__))))
import json
import time
from datetime import datetime, timedelta

# State file location
STATE_FILE = "/Users/ai/.magi/circuit_breaker_state.json"

# Configuration
MAX_FAILURES = 3
COOLDOWN_HOURS = 4

def _load_state() -> dict:
    """Load circuit breaker state from file."""
    try:
        with open(STATE_FILE, 'r') as f:
            return json.load(f)
    except Exception:
        return {"failure_count": 0, "cooldown_until": None}

def _save_state(state: dict):
    """Save circuit breaker state to file."""
    os.makedirs(os.path.dirname(STATE_FILE), exist_ok=True)
    with open(STATE_FILE, 'w') as f:
        json.dump(state, f, indent=2, default=str)

def is_tripped() -> tuple[bool, str]:
    """
    Check if circuit breaker is currently tripped.
    
    Returns:
        (True, reason) if tripped (system in cooldown)
        (False, "") if system is operational
    """
    state = _load_state()
    
    if state.get("cooldown_until"):
        cooldown_until = datetime.fromisoformat(state["cooldown_until"])
        if datetime.now() < cooldown_until:
            remaining = cooldown_until - datetime.now()
            return True, f"System in cooldown mode. Remaining: {remaining}"
        else:
            # Cooldown expired, reset
            state["failure_count"] = 0
            state["cooldown_until"] = None
            _save_state(state)
    
    return False, ""

def record_failure(reason: str = "") -> dict:
    """
    Record a consensus failure.
    
    Returns:
        State dict with current status
    """
    state = _load_state()
    state["failure_count"] = state.get("failure_count", 0) + 1
    state["last_failure"] = datetime.now().isoformat()
    state["last_failure_reason"] = reason
    
    if state["failure_count"] >= MAX_FAILURES:
        # Trip the circuit breaker
        state["cooldown_until"] = (datetime.now() + timedelta(hours=COOLDOWN_HOURS)).isoformat()
        print(f"[CIRCUIT BREAKER] 🔴 TRIPPED! {MAX_FAILURES} failures reached. Cooldown: {COOLDOWN_HOURS}h")
        
        # Alert admin via Red Phone
        try:
            import sys
            sys.path.insert(0, f'{_MAGI_ROOT}/skills/ops')
            import red_phone
            red_phone.alert_admin(
                f"🔴 Circuit Breaker TRIPPED!\n"
                f"Consecutive Failures: {state['failure_count']}\n"
                f"Cooldown Until: {state['cooldown_until']}\n"
                f"Last Reason: {reason}",
                severity="critical"
            )
        except Exception as e:
            print(f"[CIRCUIT BREAKER] Alert failed: {e}")
    
    _save_state(state)
    return state

def record_success():
    """Record a successful consensus, resetting failure count."""
    state = _load_state()
    state["failure_count"] = 0
    state["last_success"] = datetime.now().isoformat()
    _save_state(state)
    print("[CIRCUIT BREAKER] ✅ Success recorded, failure count reset.")

def manual_reset():
    """Manually reset the circuit breaker (Admin only)."""
    state = {"failure_count": 0, "cooldown_until": None, "manual_reset": datetime.now().isoformat()}
    _save_state(state)
    print("[CIRCUIT BREAKER] 🔧 Manual reset by Admin.")
    return state

def get_status() -> dict:
    """Get current circuit breaker status."""
    state = _load_state()
    tripped, reason = is_tripped()
    return {
        "tripped": tripped,
        "reason": reason,
        "failure_count": state.get("failure_count", 0),
        "cooldown_until": state.get("cooldown_until"),
        "max_failures": MAX_FAILURES,
        "cooldown_hours": COOLDOWN_HOURS
    }

# =============================================================================
# Test
# =============================================================================
if __name__ == "__main__":
    print("🔌 CIRCUIT BREAKER TEST")
    print("=" * 50)
    
    # Reset for clean test
    manual_reset()
    
    print("\nRecording 3 failures...")
    for i in range(3):
        state = record_failure(f"Test failure {i+1}")
        print(f"  Failure {i+1}: count={state['failure_count']}")
    
    print("\nChecking if tripped...")
    tripped, reason = is_tripped()
    print(f"  Tripped: {tripped}")
    print(f"  Reason: {reason}")
    
    print("\nFull status:")
    print(json.dumps(get_status(), indent=2))
    
    # Cleanup
    manual_reset()
    print("\nReset complete.")
