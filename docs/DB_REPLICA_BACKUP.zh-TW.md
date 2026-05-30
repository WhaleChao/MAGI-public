# MAGI MariaDB 遠端主庫與本機備援同步

更新日期：2026-05-30

## 目的

這份文件說明如何讓本機 MAGI MariaDB 作為遠端 MariaDB master 的 replica，用於備援與災難復原。此流程不取代一般每日 `mysqldump` 備份；replica 是即時或近即時備援，dump 備份則是可回到特定時間點的保險。

## 目前本機狀態

- MAGI 實際 MariaDB 服務：`127.0.0.1:3306`
- 相容入口：`127.0.0.1:3307`
- `3307` 由 `com.magi.db-proxy` 轉接到 `127.0.0.1:3306`
- 本機 `server-id`：`2`
- 本機 `binlog_format`：`ROW`
- 本機 `log_bin`：未啟用。若本機只作為 replica，這是可接受狀態；若未來要讓第三台再複製本機，才需要啟用本機 binlog。

## 遠端主庫需完成的設定

請遠端 MariaDB 管理者在 `100.97.29.92:3306` 執行下列設定，密碼請改成實際強密碼，且兩個來源使用同一組密碼：

```sql
CREATE USER IF NOT EXISTS 'repl'@'whale.tail6738b7.ts.net' IDENTIFIED BY '請設定一組密碼';
CREATE USER IF NOT EXISTS 'repl'@'100.116.54.16' IDENTIFIED BY '請設定同一組密碼';

GRANT REPLICATION SLAVE, REPLICATION CLIENT ON *.* TO 'repl'@'whale.tail6738b7.ts.net';
GRANT REPLICATION SLAVE, REPLICATION CLIENT ON *.* TO 'repl'@'100.116.54.16';

FLUSH PRIVILEGES;
```

遠端也必須確認：

- `server-id` 不可為 `2`
- `log_bin` 已啟用
- `binlog_format=ROW`
- `100.116.54.16` 可連入 `3306`
- 若使用 GTID，遠端 GTID 狀態需健康；若不用 GTID，需提供 `MASTER_LOG_FILE` 與 `MASTER_LOG_POS`

## 本機檢查

先只檢查，不變更資料：

```bash
cd ~/Desktop/MAGI_v2
venv/bin/python scripts/ops/configure_mariadb_replica.py \
  --check-only \
  --local-port 3307 \
  --remote-host 100.97.29.92 \
  --remote-port 3306 \
  --server-id 2
```

檢查結果會寫入：

```text
.runtime/db_replica_setup_latest.json
```

這個 JSON 會遮蔽密碼，可以提交給維運者判讀。

## 正式套用

等遠端管理者提供 `repl` 密碼後，先確認本機有可執行 `CHANGE MASTER` 的管理帳號。不要使用一般 MAGI app 帳號硬套；一般帳號通常只有 `law_firm_data` 與 `magi_brain` 權限，沒有全域 replication 管理權。

```bash
cd ~/Desktop/MAGI_v2
MAGI_DB_REPLICA_PASSWORD='遠端 repl 密碼' \
MAGI_DB_REPLICA_LOCAL_ADMIN_USER='本機管理帳號' \
MAGI_DB_REPLICA_LOCAL_ADMIN_PASSWORD='本機管理密碼' \
venv/bin/python scripts/ops/configure_mariadb_replica.py \
  --apply \
  --yes-i-understand \
  --local-port 3307 \
  --remote-host 100.97.29.92 \
  --remote-port 3306 \
  --server-id 2
```

正式套用前，工具會先備份本機資料庫到：

```text
_db_backups/replica_cutover/
```

除非已另外完成完整備份，不要使用 `--skip-backup`。

## 不使用 GTID 的情形

若遠端未啟用 GTID，請遠端提供目前 master status：

```sql
SHOW MASTER STATUS;
```

再用：

```bash
venv/bin/python scripts/ops/configure_mariadb_replica.py \
  --apply \
  --yes-i-understand \
  --no-gtid \
  --master-log-file 'mariadb-bin.000001' \
  --master-log-pos 12345
```

## 驗證同步

套用後看 `SHOW SLAVE STATUS` 摘要：

- `Slave_IO_Running=Yes`
- `Slave_SQL_Running=Yes`
- `Last_IO_Error` 空白
- `Last_SQL_Error` 空白
- `Seconds_Behind_Master` 可接受

也可以重跑：

```bash
venv/bin/python scripts/ops/configure_mariadb_replica.py --check-only
```

## 安全原則

- 不提交任何 DB 密碼、token 或 Google OAuth token。
- 沒有本機備份，不切 replication。
- 遠端主庫資料若不是 MAGI 目前資料的來源，不可直接切本機為 replica，否則可能導致資料被遠端覆蓋。
- `com.magi.db-proxy` 必須維持 `3307 -> 127.0.0.1:3306`，不得再指向退役 DB。
