# MAGI V3 實作與驗證狀態

> 本文件由 `scripts/docs/generate_implementation_status.py` 從 active-release marker、release manifest 與 source manifests 產生；不得手動維護版本與數量。

- 產生時間：2026-08-29T12:01:35.027000+00:00
- active production release：v3-20260829-rc643-r59（V3）
- source 與 active release：不同；目前 source 是候選變更
- production generation：V3；V2 已退出 active validation matrix。

## 自動盤點

- API routes：357（14 個 domain）
- skills：52（結構問題 0）
- schedule body adapters：88
- release-quality test files：54（宣告引用 85）
- legacy V2 validation：disabled

## 發布與證據契約

- 發布順序固定為 focused tests → sealed candidate → 一次完整 campaign → single-active cutover → bounded LIVE observation。
- rollback floor：`v3-20260829-rc643-r59`；候選封裝、測試與切換不得修改或覆寫它。
- V3→V3 rotation drill 必須使用隔離 marker 連跑三次；production marker 全程唯讀。
- sealed candidate 完成前禁止 production mutation；切換前後均須驗證 r59 rollback artifact 與 manifest hash。
- 共享外部資料的 payload receipt 不隨新 release 改寫；候選版本以 deployment-local receipt 另行綁定 release 身分。
- `EvidenceEnvelope v2` 綁定 release、source commit、producer/validator、失效時間、狀態類別、reason code、trace 與 receipt。
- 發布驗收、LIVE 健康、業務 backlog、人工待辦是四種不同狀態；舊 release 證據只可作歷史查詢。
- 現有 web routes、OSC 路徑及法律業務外部契約維持相容；source 測試不等同 LIVE 認證。

## 判讀限制

本文件只證明 manifest 與 active marker 的目前觀測，不宣稱尚未執行的 campaign 或 LIVE 驗收已通過。正式結果必須寫入 release-bound Evidence Ledger。
