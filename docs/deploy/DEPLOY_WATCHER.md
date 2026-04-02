# WATCHER 部署計畫 (Deployment Plan)

## 節點資訊
- **角色**: Watcher (審計員 / The Silent Notary)
- **硬體**: MacBook Air M1 (8GB RAM)
- **位置**: 常駐備援裝置

## 重要說明
> ⚠️ **Watcher 不參與投票**。它的唯一職責是：
> 1. 獨立記錄所有投票 Log
> 2. 在 03:05 AM 驗證 Log 簽名
> 3. 偵測竄改行為並觸發 Emergency Stop

## 部署步驟

### 1. 最小化安裝 (無 LLM)
Watcher 不需要 Ollama 或 AI 模型，只需要：
- Python 3.9+
- SSH 存取權限
- Tailscale VPN

```bash
# 確認 Python
python3 --version

# 安裝依賴
pip3 install requests mysql-connector-python
```

### 2. 複製審計腳本
從 Casper 複製 Watcher 專用腳本：
```bash
scp -r casper@100.97.29.92:/Users/ai/Desktop/MAGI_v2/skills/watcher ~/Desktop/watcher
```

或建立新目錄：
```bash
mkdir -p ~/Desktop/MAGI_v2/watcher
```

### 3. 建立審計腳本 (audit_notary.py)
```python
#!/usr/bin/env python3
"""
WATCHER NOTARY SCRIPT
Runs at 03:05 AM to verify voting logs.
"""
import json
import hashlib
from datetime import datetime

LOG_FILE = "/Users/watcher/.magi/voting_log.json"
KEEPER_HOST = "100.121.61.74"

def verify_logs():
    # Check log file exists
    try:
        with open(LOG_FILE, 'r') as f:
            logs = json.load(f)
    except FileNotFoundError:
        print("[WATCHER] No voting logs found. All clear.")
        return True
    
    # Verify each log signature
    for log in logs:
        expected_hash = log.get("signature")
        content = json.dumps({k: v for k, v in log.items() if k != "signature"}, sort_keys=True)
        actual_hash = hashlib.sha256(content.encode()).hexdigest()
        
        if expected_hash != actual_hash:
            print(f"[WATCHER] ⚠️ TAMPERING DETECTED: {log}")
            trigger_emergency_stop()
            return False
    
    print(f"[WATCHER] ✅ All {len(logs)} logs verified.")
    return True

def trigger_emergency_stop():
    """Federation dissolution - critical alert."""
    import red_phone
    red_phone.alert_admin(
        "🚨 EMERGENCY STOP - FEDERATION DISSOLUTION\n"
        "Watcher detected log tampering!\n"
        "All MAGI operations suspended.",
        severity="critical"
    )

if __name__ == "__main__":
    print(f"[WATCHER] Notary running at {datetime.now()}")
    verify_logs()
```

### 4. 設定每日排程 (03:05 AM)
```bash
# 編輯 crontab
crontab -e

# 加入
5 3 * * * /usr/bin/python3 ~/Desktop/MAGI_v2/watcher/audit_notary.py >> ~/Desktop/MAGI_v2/watcher/notary.log 2>&1
```

### 5. 驗證 Keeper 連線
```bash
mysql -h 100.121.61.74 -u magi_watcher -p'Watcher_ReadOnly_2026!' -e "SELECT 1;"
```

## 安全設定

### 資料庫使用者 (Keeper 上建立)
```sql
-- Watcher 只需要讀取權限
CREATE USER 'magi_watcher'@'%' IDENTIFIED BY 'Watcher_ReadOnly_2026!';
GRANT SELECT ON magi_brain.* TO 'magi_watcher'@'%';
FLUSH PRIVILEGES;
```

---
*MAGI Federation Deployment Guide - Watcher Node*
