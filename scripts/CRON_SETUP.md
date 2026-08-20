# Cron Job for Nightly Council
# Edit crontab with: crontab -e
# 0 3 * * * "$MAGI_PYTHON_EXECUTABLE" "$MAGI_ROOT/scripts/nightly_council.py" >> "$MAGI_LOG_DIR/nightly.log" 2>&1

# Manual Run:
# "$MAGI_PYTHON_EXECUTABLE" "$MAGI_ROOT/scripts/nightly_council.py"
