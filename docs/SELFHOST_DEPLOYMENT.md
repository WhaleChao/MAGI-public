# MAGI V3 通用自架部署手冊

這份手冊適用於要在自己的 macOS 或 Windows 電腦部署單一組織 MAGI 的使用者。安裝器不會沿用開發者的絕對路徑、密鑰、案件資料或排程執行狀態。

## 1. 已驗收的平台邊界

| 功能 | macOS | Windows |
|---|---|---|
| OSC 網頁、對話與工具 API | 支援 | 支援 |
| 使用者層級背景服務 | LaunchAgent | Task Scheduler（異常退出限次重啟） |
| MySQL／MariaDB | 支援 | 支援 |
| NVIDIA／OpenAI 相容 API | 支援 | 支援 |
| Apple MLX、Apple Translation、狀態列應用程式 | 選用 | 不適用，自動停用 |
| PDF、OCR、Playwright | `--full` 後可啟用 | `-Full` 後可啟用 |

Windows 版的桌面狀態入口是網頁，不會嘗試載入 AppKit 或 Apple 專用模型。

## 2. 安裝前準備

1. 安裝 Python 3.12。Windows 安裝時要勾選「Add Python to PATH」。
2. 準備 MySQL 8 或 MariaDB 10.6 以上版本，以及具有建立資料庫與資料表權限的專用帳號。
3. 先不要啟用法扶、法院、Google、通知或股票等選用功能；等應用程式本體驗收後再逐項開啟。

發布者應使用 `python scripts/magi_selfhost.py package --apply` 建立交付檔，不要直接壓縮開發目錄。交付檔會排除 `.git`、`.env`、虛擬環境、快取、執行狀態與未列入的本機檔案，並內附 SHA-256 清單。

## 3. Windows 安裝

在解壓後的 MAGI 目錄中開啟 PowerShell：

```powershell
.\install-magi.ps1
```

這只會顯示安裝計畫。確認路徑與使用者層級排程後，才執行：

```powershell
.\install-magi.ps1 -Apply
```

需要 PDF、OCR 與瀏覽器自動化套件時：

```powershell
.\install-magi.ps1 -Apply -Full
```

安裝位置是 `%LOCALAPPDATA%\MAGI\selfhost`，不需要系統管理員權限。

若此時尚未填寫資料庫密碼，安裝器會完成程式與不可變版本準備，但將背景服務留在「等待設定」；這是正常安全狀態，不是安裝失敗。

## 4. macOS 安裝

```zsh
./install-magi.command
./install-magi.command --apply
```

安裝位置是 `~/Library/Application Support/MAGI/selfhost`。完整選用套件使用 `--full`。

## 5. 首次設定與資料庫

安裝器會產生 `config/selfhost.json` 與 `secrets/magi.env`。`FLASK_SECRET_KEY` 與 `MAGI_API_KEY` 會在本機產生；請自行填入下列資料：

```text
DB_HOST=127.0.0.1
DB_PORT=3306
DB_USER=magi
DB_PASSWORD=<private-password>
DB_NAME=magi
MAGI_BRAIN_DB_NAME=magi_brain
```

不要將 `magi.env` 傳給其他人或放入 Git。填寫後執行：

```text
python scripts/magi_selfhost.py configure --interactive
python scripts/magi_selfhost.py configure --interactive --apply
python scripts/magi_selfhost.py database
python scripts/magi_selfhost.py database --apply
python scripts/magi_selfhost.py doctor
python scripts/magi_selfhost.py doctor --strict
```

互動設定器會使用隱藏輸入讀取密碼，不提供 `--db-password` 參數，因此密碼不會出現在 shell 歷史、處理程序清單或 JSON 輸出。

`database --apply` 只會執行冪等的 `CREATE DATABASE/TABLE IF NOT EXISTS`，不會刪除現有資料。第一位在 `/register` 建立的使用者會成為管理員；之後公開註冊預設關閉。

資料庫驗收通過後，才安裝並啟動背景服務：

```text
python scripts/magi_selfhost.py service install --apply
python scripts/magi_selfhost.py service start --apply
python scripts/magi_selfhost.py doctor --live
```

## 6. 功能群與它們的影響

`config/selfhost.json` 中的 `features` 決定服務、憑證檢查與排程是否同時生效。關閉功能不是故障，對應排程會在建置 release 時停用。

- `case_management`：案件資料夾、待辦與生命週期，預設開啟。
- `legal_aid`、`court_portal`：需要真實業務帳號與對應憑證。
- `google_calendar`、`google_drive`：需要 Google 憑證檔，啟用後 `doctor` 會檢查。
- `messaging`：至少設定一組 Discord、Telegram 或 LINE token。
- `judgment_library`、`knowledge`、`documents`、`market`、`research`：各自控制資料處理管線，不會因其中一項沒設好而影響核心 OSC。
- `local_models`：僅 macOS 可用；Windows 會強制停用 Apple 專用排程。
- `development`：回歸測試、自動編碼與候選版驗證；一般自架機不應啟用。

修改功能後要建立新的不可變 release，因為排程清單是候選版的一部分。

## 7. 驗收門檻

正式使用前必須全部通過：

```text
python scripts/magi_selfhost.py doctor
python scripts/magi_selfhost.py doctor --live
python scripts/magi_selfhost.py doctor --live --strict
python scripts/ops/selfhost_portability_audit.py
python scripts/ops/selfhost_release_smoke.py
```

`doctor --strict` 會把所有警告視為尚未完成佈建，只有 `ready=true`
才會回傳成功。`doctor --live --strict` 另會驗證網頁 `/readyz` 與工具 API
`/health`。可選功能只有在啟用後才會成為驗收項目；法扶、法院入口、
Google、通知與 NVIDIA 重型模型的必要憑證不完整時，正式驗收不會放行。

`selfhost_release_smoke.py` 只在臨時目錄內驗證交付 ZIP 的解壓、不可變版本、
雜湊綁定、原子升級、回滾、竄改偵測與當前平台原生服務計畫；不會讀取憑證、
連線客戶資料庫或安裝系統服務。正式交付時，macOS 與 Windows CI 都必須產生
`native-release-smoke.json` 證據。

## 8. 升級、回滾與移除

升級會先建立新的不可變 release，再停止舊版、原子切換、啟動新版，並實測網頁與工具 API。若新版未通過 LIVE 驗收，安裝器會自動切回前一版並重新驗收；回滾與升級都不會改寫使用者資料。如果加上 `--no-restart`，只會封存候選版，不會切換有效版本。

```text
python scripts/magi_selfhost.py --source <new-source> upgrade
python scripts/magi_selfhost.py --source <new-source> upgrade --apply
python scripts/magi_selfhost.py rollback --apply
python scripts/magi_selfhost.py uninstall --apply
```

移除預設保留設定、案件與備份。只有同時提供 `--remove-data --confirm-remove-data` 才會刪除資料。

## 9. 故障處理原則

1. 先執行 `doctor`，不要盲目重裝。
2. 某項業務尚未設定時，關閉對應 feature，不要使用假 token 繞過檢查。
3. 升級的 LIVE 驗收失敗時會自動恢復前一版；仍可用 `rollback --apply` 主動切回，不要複製舊 release 覆蓋新 release。
4. 備份 `data/`、`config/` 與 `secrets/`，但不要把 `secrets/` 放到雲端公開分享。
