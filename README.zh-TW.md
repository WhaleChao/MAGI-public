# MAGI V3（公版）

MAGI 是一套本機優先的 AI 作業平台，以明確工具契約、持久化 checkpoint、隱私邊界與可回滾發行為核心。現行公版基準為 **v3-20260820-rc627**。

## 核心角色

- **Gateway**：互動請求、登入、安全標頭與工具 API。
- **Control**：管理、版本與健康狀態。
- **Supervisor**：排程、worker、單例 owner、重試與恢復。
- **專用工作流**：檔案、Drive/NAS、文件、日曆、通知與領域 adapter。

## 可靠度原則

1. 有副作用工作必須具備身分綁定、冪等、收據、回讀與安全失敗路徑。
2. 外部等待標示為 `deferred`，不得冒充完成。
3. 檔案同步以內容與來源 identity 驗證，不以路徑相似猜測。
4. installed release 不可變；每個候選版本都重建獨立證據。
5. 公版不得包含 runtime、密碼、Cookie、token、案件內容或私密整合資料。

## rc627 摘要

- 封存來源 2,034 個檔案。
- 正式品質 exact/full passed；收集 2,446 項測試。
- 隱私稽核通過，違規 0。
- LIVE business、function、Doctor、guardian、Funnel 全綠。
- Cookie Cutter 三個合成案例通過 printable、manifold、no-persist、no-external 驗證。

完整公開架構、排程、健康、安全與發行說明請見[公版技術手冊](docs/MAGI_V3_技術手冊_rc627_公版.md)。

發布前請執行：

```bash
python3 scripts/public_release_audit.py --public-isolation --strict
```

原私有 MAGI-v2 倉庫已原地更名為 `MAGI-v3`，完整保留 V2 歷史；本公版倉庫仍維持 `MAGI-public`。
