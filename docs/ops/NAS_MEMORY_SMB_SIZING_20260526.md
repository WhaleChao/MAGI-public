# NAS 記憶體與 SMB 穩定性評估（2026-05-26）

## 結論

如果目前 NAS 是 Synology DS220j，無法靠加記憶體解決 SMB 斷線問題。DS220j 官方資料列示記憶體為 512 MB DDR4，規格中沒有可擴充記憶體欄位；Synology 的升級判斷邏輯是：產品規格有標示「可擴充至」才代表支援記憶體擴充。

MAGI 目前的案件資料量、Synology Drive、SMB 掛載、Google Drive 對照、PDF/OCR/閱卷/法扶批次工作，已經超出 DS220j 這類入門機的穩定承載範圍。

## 建議門檻

| 目標 | 建議記憶體 | 判斷 |
| --- | ---: | --- |
| 只想短期改善、小型兩人事務所、資料量不再快速增加 | 6 GB | DS224+ 官方上限是 6 GB；可用，但不是長期保守解。 |
| 希望明顯降低 SMB/Drive 卡死、支援 MAGI 夜間批次、卷宗搬移、索引與備份 | 16 GB | 建議最低採購目標。不要再買只能停在 6 GB 的機型作為長期主 NAS。 |
| 希望長期穩定、案件資料持續成長、保留快照/Drive/備份/索引餘裕 | 32 GB | 建議主 NAS 目標，搭配可擴充的 Plus 系列與 ECC 記憶體。 |

## 機型方向

1. **不建議繼續以 DS220j 作為 MAGI 主 NAS**
   - 512 MB RAM 太小。
   - 無可支援的記憶體擴充路徑。
   - DSM、Synology Drive、SMB、多客戶端同步與大量小檔掃描會互相搶資源。

2. **最低可接受：DS224+ 加到 6 GB**
   - 官方規格為 2 GB DDR4 non-ECC，最大 6 GB（2 GB + 4 GB）。
   - 適合作為低成本改善，但對 MAGI 長期商用主 NAS 仍偏緊。

3. **建議：可上 16 GB / 32 GB 的 Plus 系列**
   - DS723+ / DS923+ 類型支援最高 32 GB（16 GB x 2）。
   - 若案件資料持續成長，建議從 16 GB 起跳，預留 32 GB 擴充空間。
   - 優先選 Synology 官方相容 ECC 記憶體，避免 DSM 警告、開機不穩或保固爭議。

## 現場觀察

本機於 2026-05-26 檢查時：

- NAS IP `192.168.1.3` 可以 ping。
- SMB `445` 可以連通。
- DSM HTTP/HTTPS 查詢逾時。
- macOS `NetAuthSysAgent` 出現 `U` 狀態，且 `/Volumes/homes`、`/Volumes/lumi`、`/Volumes/bakup` 均未掛載。

這表示不是單純 IP 跳掉，而是 NAS/DSM/SMB 服務在負載下沒有健康回應，macOS 掛載端也被拖住。增加 NAS 記憶體能減少 swap 與服務卡死，但 DS220j 無法加 RAM，因此若確認為 DS220j，硬體升級應改為更換 NAS。

## MAGI 配套調整

硬體升級前，MAGI 仍應維持以下保護：

- 避免無限制 `os.walk` 掃 NAS。
- 大型結案搬移改離峰、限速、可續傳。
- NAS 未掛載時優先使用 Synology Drive 本機副本，但不得為已結案案件重建空殼資料夾。
- `NetAuthSysAgent` 卡在 `U` 時不得連續重試 SMB 掛載，避免造成更多殭屍程序。
- SMB 掛回後才執行法扶附件、閱卷、筆錄、PDF 待辦批次。

## 採購建議

如果目標是「長期避免 SMB 斷線干擾 MAGI 與事務所作業」，建議不要只追求最低可開機規格：

- **最低建議：16 GB RAM 等級 NAS。**
- **保守商用建議：可擴充至 32 GB 的 NAS，先裝 16 GB，必要時上 32 GB。**
- **DS224+ 的 6 GB 只能算過渡方案，不建議作為最終主 NAS。**

資料量已接近主 NAS 壓力邊界時，記憶體之外也應同步評估：

- 改 4-bay 或以上，避免容量與重建壓力太高。
- 支援 Btrfs、快照與較強 CPU。
- 若常搬大型卷宗，考慮 2.5GbE / 10GbE 升級空間。
- 將 BACKUP share 與主案件 share 的清理/備份策略分開。

## 參考來源

- Synology DS220j data sheet：512 MB DDR4，未列記憶體擴充欄位。
- Synology DS224+ product comparison：2 GB DDR4 non-ECC，最高 6 GB（2 GB + 4 GB）。
- Synology DS723+ / DS923+ product specification：最高 32 GB（16 GB x 2）。
- Synology 社群升級說明：產品規格有列「Memory Expandable up to」才代表支援記憶體擴充；非相容記憶體可能導致不穩或保固支援問題。
