#!/bin/bash
set -euo pipefail

MAGI_ROOT="/Users/ai/Desktop/MAGI"
PY="$MAGI_ROOT/venv/bin/python3"
RUN_TS="$(date +%Y%m%d_%H%M%S)"
RUN_BASE="${MAGI_AUTOPILOT_RUNS_DIR:-$MAGI_ROOT/_autopilot_runs}"
RUN_DIR="$RUN_BASE/${RUN_TS}_official_api_night_once"
mkdir -p "$RUN_DIR"

# load env best-effort
if [ -f "$MAGI_ROOT/.env" ]; then
  set +u
  set -a
  source "$MAGI_ROOT/.env" >/dev/null 2>&1 || true
  set +a
  set -u
fi

TASK='official_api_night_pull {"max_jdocs":1200,"max_days":7,"force":false,"notify":true}'
export JUDICIAL_API_ALLOW_INSECURE_SSL="${JUDICIAL_API_ALLOW_INSECURE_SSL:-1}"

set +e
"$PY" "$MAGI_ROOT/skills/judgment-collector/action.py" --task "$TASK" > "$RUN_DIR/result.json" 2> "$RUN_DIR/stderr.log"
RC=$?
set -e

"$PY" - <<'PY' "$RUN_DIR/result.json" "$RUN_DIR/report.txt" "$RC"
import json, sys, datetime
result_path, report_path, rc = sys.argv[1], sys.argv[2], int(sys.argv[3])
now = datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')
try:
    data = json.load(open(result_path, 'r', encoding='utf-8'))
except Exception as e:
    data = {"success": False, "error": f"invalid_json: {e}"}

auth = data.get('auth_success', None)
if auth is True:
    auth_s = '成功'
elif auth is False:
    auth_s = '失敗'
else:
    auth_s = '未知'

fetched = data.get('fetched', 0)
msg = data.get('message') or data.get('error') or ''
lines = [
    '司法院 API 夜間一次性任務報告',
    f'時間: {now}',
    f'執行返回碼: {rc}',
    f'Auth: {auth_s}',
    f'拉取筆數: {fetched}',
    f'結果: {msg}',
]
open(report_path, 'w', encoding='utf-8').write('\n'.join(lines) + '\n')
print(json.dumps({
    'ok': bool(data.get('success')),
    'auth_success': auth,
    'fetched': fetched,
    'message': msg,
    'report': report_path,
}, ensure_ascii=False))
PY

SUMMARY_JSON="$RUN_DIR/summary.json"
"$PY" - <<'PY' "$RUN_DIR/result.json" "$SUMMARY_JSON"
import json, sys
res_path, out_path = sys.argv[1], sys.argv[2]
try:
    d = json.load(open(res_path,'r',encoding='utf-8'))
except Exception:
    d = {'success': False, 'error': 'invalid_result_json'}
msg = {
  'ok': bool(d.get('success')),
  'auth_success': d.get('auth_success', None),
  'fetched': int(d.get('fetched') or 0),
  'message': d.get('message') or d.get('error') or ''
}
open(out_path,'w',encoding='utf-8').write(json.dumps(msg, ensure_ascii=False, indent=2))
PY

# Optional push by red_phone (line/discord) to avoid occupying current work page
"$PY" - <<'PY' "$RUN_DIR/summary.json"
import json, sys
p = sys.argv[1]
try:
    d = json.load(open(p,'r',encoding='utf-8'))
except Exception:
    d = {'ok': False, 'auth_success': None, 'fetched': 0, 'message': 'summary load failed'}
text = (
    '🌙 司法院 API 夜間測試完成\n'
    f"Auth: {'成功' if d.get('auth_success') is True else ('失敗' if d.get('auth_success') is False else '未知')}\n"
    f"拉取筆數: {int(d.get('fetched') or 0)}\n"
    f"結果: {str(d.get('message') or '')[:300]}"
)
try:
    import sys, os
    root='/Users/ai/Desktop/MAGI'
    if root not in sys.path:
        sys.path.insert(0, root)
    from skills.ops.red_phone import notify_all
    notify_all(text, severity='info')
except Exception:
    pass
PY

exit 0
