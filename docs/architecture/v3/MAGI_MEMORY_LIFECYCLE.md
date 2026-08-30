# MAGI MemoryRecord v2 生命週期

## 邊界

`MemoryRecord v2` 管理 Agent 的可變記憶，不管理正式卷證。正式案件檔、法院文件、法扶附件、錄音原檔、法定保存資料及 legal hold 均無法透過此介面刪除。

sidecar 只保存 canonical memory ID、內容 SHA-256、來源、case scope、信心、保留政策、legal hold、狀態與索引同步收據，不保存記憶正文。查看、修正、封存及 tombstone 都要求精確 `mem-…` ID；禁止 fuzzy delete。

## 刪除流程

1. 將可刪除 Agent 記憶標為 tombstoned；此時 recall 立即過濾該 ID，Keeper 的離線資料也不能把它復活。
2. 以完全相符的來源和 content hash 處理 primary、Keeper、MariaDB；FAISS 以剩餘資料完整重建。
3. 知識圖譜只移除帶相同 memory ID 的衍生節點；Obsidian 只更新 Agent index tombstone，不刪 vault note 或來源檔。
4. primary、Keeper、MariaDB、FAISS、knowledge graph、Obsidian 與 backup index 全部回報 `not_present`、`tombstoned` 或 `not_applicable`，才產生完成證明。

CLI 的實體 propagation 必須同時提供 `--apply` 與完全相同的 `--confirm-memory-id`。正式法律資料即使被誤標也 fail-closed，不會被 Agent memory deletion 波及。
