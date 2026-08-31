# MAGI V3 技術手冊（公版）

版本：v3-20260820-rc627

MAGI 是一套以本機優先、工具契約、資料證據與可回滾發行為核心的 AI 作業平台。本公版說明架構與開發契約；不包含帳號、案件、runtime 狀態、部署路徑或私密整合資料。

## 1. 架構

- Gateway：互動 HTTP、登入、安全標頭與工具 API。
- Control：管理、健康、版本與操作面板。
- Supervisor：排程、worker、單例 owner、checkpoint 與恢復。
- 專用工作流：檔案、Drive/NAS、文件、入口、日曆與通知均以結構化契約執行。

## 2. 可靠度原則

1. 副作用必須具備身分綁定、冪等、收據、回讀與失敗回滾。
2. 外部等待標示為 deferred，不冒充 completed。
3. 受控 schema 拒絕未知欄位與型別強制轉換。
4. 檔案同步以內容與來源 identity 驗證，不以路徑相似猜測。
5. installed release 不可變；每個候選版重建獨立證據。

## 3. 模型與工具

模型不是唯一核心。MAGI 先路由意圖與資料來源，再由權威工具執行；本機或外部模型輸出仍需來源、忠實度、引用、隱私與資源閘門。外部模型只接受明示、當次授權。

## 4. 排程與健康

排程維持 durable occurrence、retry、checkpoint 與 terminal receipt。健康燈只呈現最新 owner 證據：綠色代表新鮮成功，處理中代表 canonical owner 正常前進，黃色代表可恢復等待，紅色代表安全阻擋或重試耗盡。不得刪除狀態檔來清燈。

## 5. 安全與隱私

- 公開發行不包含秘密、Cookie、token、案件內容、資料庫或 runtime state。
- 私密端點使用登入、CSRF、no-store、noindex 與安全標頭。
- 錯誤輸出採固定 reason code 與去識別計數。
- 支援包與品質收據以 SHA-256 與匿名摘要為主。

## 6. 測試與發行

發行依序經過 focused regression、sealed bundle、privacy audit、host-outer full quality、備份與實際還原、inactive install、hash-bound cutover、post-cutover 與完整健康回讀。測試通過不等同 LIVE 寫入授權。

## 7. rc627 摘要

- 封存來源：2,034 files。
- 正式品質：exact/full passed；2,446 tests collected。
- 隱私稽核：passed，violations=0。
- LIVE 健康：business、function、Doctor、guardian、Funnel 通過。
- Cookie Cutter：三個合成形狀通過 printable、manifold、no-persist、no-external 驗證。

## 8. GitHub 分工

- **MAGI-public**：核心架構、公開測試、範例設定與本公版手冊。
- **MAGI-v3（private）**：由原 MAGI-v2 原地更名，保留完整 V2 歷史並承接 V3 封存來源與私版工程手冊。

兩個倉庫都不得保存真實秘密或案件資料。版本分支不改寫既有歷史。

## 9. 開發與貢獻

提交前執行 repository tests、public release audit、秘密／PII 掃描、symlink 與 private path 檢查。任何外部整合請使用 placeholder 設定與 mock/fixture，禁止提交個人帳號或 LIVE 收據。
