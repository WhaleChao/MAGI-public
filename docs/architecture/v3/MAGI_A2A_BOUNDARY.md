# MAGI A2A 邊界

RC643 只預留 A2A 1.0 proposal adapter，預設停用。`config/a2a/adapter.json` 強制 `proposal-only`、無 writer 權限、無 federation，且 remote host 採 allowlist。

即使未來顯式啟用，它也只能建立帶 payload digest 的提案收據，不能 dispatch、修改法律資料、成為 production owner 或切換 release。WHALE hostname 與 Tailnet CGNAT 網段會被固定拒絕，不能藉 A2A 恢復已放棄的聯合機制。

任何真正 A2A 執行能力都必須由後續 release 另行設計、核准及 LIVE 驗收；本 adapter 不預先授權該擴張。
