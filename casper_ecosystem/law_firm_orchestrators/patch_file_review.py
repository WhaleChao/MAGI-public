"""Legacy no-op patch hook for the file-review orchestrator.

The payment notifier is now implemented directly in file_review_automation.py
with a PDF-delivery gate.  Keeping this file as a no-op prevents older
maintenance jobs from re-injecting an unsafe text-only payment notice.
"""

print("patch_file_review.py: no-op; payment notification guard is built in.")
