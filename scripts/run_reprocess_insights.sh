#!/bin/bash
# 凌晨自動重處理 legal_insights（一次性）
# 1. 開啟 Codex summary feature
# 2. 執行 reprocess_insights.py（含 API 抓全文）
# 3. 關閉 Codex summary feature
# 4. 自我刪除 LaunchAgent（一次性任務）

set -e
MAGI_ROOT="${MAGI_ROOT:-$(cd "$(dirname "$0")/.." && pwd)}"
cd "$MAGI_ROOT"
source venv/bin/activate

LOG="/tmp/reprocess_insights_overnight.log"
exec > "$LOG" 2>&1
echo "=== $(date) Starting overnight reprocess ==="

# 開啟 summary feature
python3 -c "
import json
p = json.load(open('.agent/codex_distributed_policy.json'))
p['features']['summary'] = True
json.dump(p, open('.agent/codex_distributed_policy.json', 'w'), indent=2)
print('summary feature enabled')
"

# 執行重處理
python3 scripts/reprocess_insights.py --delay 10 || true

# 關閉 summary feature
python3 -c "
import json
p = json.load(open('.agent/codex_distributed_policy.json'))
p['features']['summary'] = False
json.dump(p, open('.agent/codex_distributed_policy.json', 'w'), indent=2)
print('summary feature disabled')
"

echo "=== $(date) Overnight reprocess complete ==="

# 移除一次性 LaunchAgent
launchctl bootout gui/$(id -u) /Users/ai/Library/LaunchAgents/com.magi.reprocess-insights.plist 2>/dev/null || true
rm -f /Users/ai/Library/LaunchAgents/com.magi.reprocess-insights.plist
