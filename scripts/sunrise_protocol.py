import sys
import os

# Add project root to path
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

try:
    from skills.magi.sunrise import execute_sunrise_protocol
    print(execute_sunrise_protocol())
except ImportError as e:
    print(f"❌ Import Error: {e}")
except Exception as e:
    print(f"❌ Execution Error: {e}")
