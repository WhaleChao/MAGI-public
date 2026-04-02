# Melchior Iron Dome Sync 部署指南

> **目標**: 讓 Melchior 加入 Iron Dome 分散式同步網路  
> **預計時間**: 5 分鐘

---

## 📦 需要部署的檔案

### 1. iron_dome_sync.py (新增)

**來源:** Casper `/Users/ai/Desktop/MAGI/skills/ops/iron_dome_sync.py`  
**目標:** Melchior `D:\MAGI\skills\ops\iron_dome_sync.py`

複製整個檔案過去即可。

---

### 2. 修改 server.py

在 Melchior 的 `server.py` 中，找到初始化 Flask app 的地方，加入以下程式碼：

```python
# Register Iron Dome Sync Routes
try:
    from skills.ops.iron_dome_sync import register_iron_dome_routes
    register_iron_dome_routes(app)
    print("🛡️ Iron Dome Sync routes registered")
except Exception as e:
    print(f"⚠️ Iron Dome Sync routes not registered: {e}")
```

---

### 3. 設定環境變數 (可選)

在 Melchior 的 `.env` 或環境變數中加入：

```
MAGI_NODE=melchior
```

這讓系統知道自己是哪個節點。如果不設定，預設是 `casper`。

---

## ✅ 驗證部署

部署完成並重啟服務後，在 Melchior 上執行：

```bash
curl http://localhost:5002/api/iron_dome/hash
```

應該回傳類似：
```json
{"hash": "807f61347fa16ea5fa4339754c60ba35", "node": "melchior"}
```

---

## 🔄 測試同步

從 Casper 測試廣播：

```bash
curl -X POST http://localhost:5002/api/iron_dome/broadcast
```

Melchior 應該會收到通知並回應。

---

## 📡 API 端點一覽

| 端點 | 方法 | 功能 |
|------|------|------|
| `/api/iron_dome/hash` | GET | 取得本機規則 hash |
| `/api/iron_dome/patterns` | GET | 匯出完整規則 JSON |
| `/api/iron_dome/status` | GET | 查看所有節點同步狀態 |
| `/api/iron_dome/notify` | POST | 接收其他節點的更新通知 |
| `/api/iron_dome/broadcast` | POST | 廣播更新到所有節點 |

---

## 🔧 如果遇到問題

1. **ModuleNotFoundError**: 確認 `skills/ops/` 目錄存在
2. **Port 衝突**: 確認 Flask 跑在 5002 port
3. **路徑問題**: 修改 `iron_dome_sync.py` 中的 `IRON_DOME_FILE` 路徑

---

> 部署完成後，三位哲人即可自動同步 Iron Dome 規則！ 🛡️
