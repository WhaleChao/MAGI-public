# MAGI V3 漸進式模組邊界

RC643 採 strangler adapter 漸進拆分，不對 `osc_cases.py`、閱卷或法扶做大爆炸重寫。既有 route、OSC path、排程 ID、通知與法律業務 receipt 都是穩定外部契約；新能力先落在 `magi_v3` 或明確 domain／connector 模組，再由既有 facade 呼叫。

機器可讀規範位於 `config/v3_module_boundaries.json`，固定四個邊界：Agent Kernel、法律領域、外部 connector、release/ops。三個巨型既有模組被列為 legacy facade，只允許相容轉接、缺陷修復和逐段抽離，不再承接新的跨領域核心邏輯。

本次已先抽離 Evidence Ledger、OTel trace、行為評測、skill sandbox、MCP、外部 canary、memory lifecycle 與供應鏈 gate。這些模組預設不直接寫入案件業務狀態；真正的法律業務 writer 仍由既有 single-owner 流程負責。

任何後續抽離都必須先有固定回歸 scenario，再保持 facade 簽章不變地替換內部實作。若 route、receipt 或 writer ownership 改變，release validation 必須 fail closed。
