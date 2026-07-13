# MAGI MariaDB 主 DB 與遠端備份庫同步

更新日期：2026-05-31

## 目的

私有版 MAGI 的資料庫方向是：

```text
本機 MAGI MariaDB 主 DB -> 遠端 MariaDB 備份庫 replica
```

也就是這台 MAGI 電腦保留主資料庫，遠端資料庫只做備援追蹤。不得把本機切成追遠端的 replica，否則可能讓遠端舊資料覆蓋本機正式資料。

## 目前本機狀態

- MAGI 實際 MariaDB：`127.0.0.1:3306`
- 相容入口：`127.0.0.1:3307`
- `3307` 由 `com.magi.db-proxy` 轉接到 `127.0.0.1:3306`
- Tailscale master：`<本機 MAGI Tailscale IP>:3306`
- MagicDNS：`<本機 MAGI MagicDNS>:3306`
- 本機 `server-id`：`2`
- 本機 `log_bin`：已啟用
- 本機 `binlog_format`：`ROW`
- binlog 保留：7 天

## 本機 master 檢查

```bash
cd ~/Desktop/MAGI_v2
venv/bin/python scripts/ops/configure_mariadb_master_backup.py --check-only
```

檢查結果會寫入：

```text
.runtime/db_master_backup_latest.json
```

密碼保存在本機：

```text
.runtime/db_replication_credentials.local.json
```

這個檔案權限應為 `600`，不得提交到 git。

## 本機 master 套用

需要重新產生或補齊 replication 帳號時：

```bash
cd ~/Desktop/MAGI_v2
venv/bin/python scripts/ops/configure_mariadb_master_backup.py --apply
```

如果修改了 `/opt/homebrew/etc/my.cnf.d/magi.cnf`，再加上：

```bash
venv/bin/python scripts/ops/configure_mariadb_master_backup.py --apply --restart-mariadb
```

工具會：

- 確認本機 master 狀態。
- 寫入或更新 binlog 設定。
- 建立 `repl` 帳號。
- 允許你指定的遠端備份庫 Tailscale IP / MagicDNS 連入同步。
- 輸出遠端 replica 需要執行的 `CHANGE MASTER` 指令摘要，密碼會遮蔽。

## 初始化備份

遠端備份庫第一次建立 replica 時，需先匯入一份帶 master 座標的 seed dump：

```bash
mkdir -p _db_backups/master_seed
TS=$(date +%Y%m%d_%H%M%S)
mysqldump --protocol=socket -uai --single-transaction --quick --routines --events --triggers --master-data=2 --default-character-set=utf8mb4 law_firm_data | gzip -6 > _db_backups/master_seed/law_firm_data_master_seed_${TS}.sql.gz
mysqldump --protocol=socket -uai --single-transaction --quick --routines --events --triggers --master-data=2 --default-character-set=utf8mb4 magi_brain | gzip -6 > _db_backups/master_seed/magi_brain_master_seed_${TS}.sql.gz
```

目前已產生的 seed dump 位置：

```text
_db_backups/master_seed/
```

## 遠端備份庫要做的事

遠端備份庫需先匯入 seed dump，然後以 `.runtime/db_replication_credentials.local.json` 內的 `repl` 密碼接本機 master。遠端 SQL 範例：

```sql
STOP SLAVE;
RESET SLAVE ALL;
CHANGE MASTER TO
  MASTER_HOST='<本機 MAGI Tailscale IP 或 MagicDNS>',
  MASTER_PORT=3306,
  MASTER_USER='repl',
  MASTER_PASSWORD='請填入本機 .runtime 內的 repl 密碼',
  MASTER_LOG_FILE='magi-bin.000001',
  MASTER_LOG_POS=實際座標;
START SLAVE;
```

實際 `MASTER_LOG_FILE` 與 `MASTER_LOG_POS` 以 seed dump 內的 `CHANGE MASTER TO` 註解或 `SHOW MASTER STATUS` 為準。

## 驗證同步

遠端備份庫執行：

```sql
SHOW SLAVE STATUS\G
```

應確認：

- `Slave_IO_Running: Yes`
- `Slave_SQL_Running: Yes`
- `Last_IO_Error` 空白
- `Last_SQL_Error` 空白
- `Seconds_Behind_Master` 在合理範圍

本機端可檢查：

```sql
SHOW PROCESSLIST;
SHOW MASTER STATUS;
```

## 安全原則

- 本機是主 DB，不可執行追遠端的 `CHANGE MASTER`。
- 遠端備份庫只能做 replica；不要讓遠端自動回寫本機。
- 不提交任何 DB 密碼、token、OAuth token 或 `.runtime/db_replication_credentials.local.json`。
- `com.magi.db-proxy` 必須維持 `3307 -> 127.0.0.1:3306`，不得再指向退役 DB。
- `log-bin` 會占用硬碟；目前設定 binlog 保留 7 天，避免硬碟無限制成長。
