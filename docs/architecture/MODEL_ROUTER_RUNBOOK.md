# MAGI 智慧模型路由與 OOM 防線

更新日期：2026-05-24

## 目的

MAGI 不以「能把大模型啟動」作為成功標準，而是以「在法律文件、工具調用、OCR、翻譯、摘要與背景任務同時存在時不 OOM」作為標準。智慧模型路由會在每次推論前判斷任務難度、目前上線模型與資源狀態，再決定是否升級到更聰明的模型。

## 模型分工

| 層級 | 模型 | 用途 | 商用預設 |
| --- | --- | --- | --- |
| stable_local | `gemma-4-e4b-it-4bit` | 白天一般對話、工具調用、摘要、翻譯、書狀輔助 | 開啟 |
| heavy_local_moe | `gemma-4-26b-a4b-it-4bit` | 法律分析、長翻譯、深度摘要、實務見解 | 僅在安全閘門通過且模型已上線時使用 |
| experimental_dense_local | `gemma-4-31b-experimental` | 實驗與離線 benchmark | 關閉 |
| embedding_local | `modernbert-embed-4bit` | 向量檢索 | 保留 |
| verify_sidecar | `Phi-4-mini-instruct-4bit` | 交叉檢查與備援 | 可逐步降級 |
| crosscheck_sidecar | `SmolLM3-3B-4bit` | 交叉檢查與備援 | 可逐步降級 |
| cloud_heavy | NVIDIA NIM 非中國模型 allowlist | `@heavy` 明確要求的高品質任務 | 私有版依設定開啟 |

## 26B-A4B 安全閘門

26B-A4B 只有在下列條件同時成立時才會被選用：

- `gemma-4-26b-a4b-it-4bit` 已經在目前 oMLX profile 上線。
- resource governor 為 `normal`。
- 可用磁碟至少 `MAGI_ROUTER_26B_MIN_DISK_GB`，預設 70GB。
- free + inactive memory 至少 `MAGI_ROUTER_26B_MIN_FREE_GB`，預設 8GB。
- swap 不超過 `MAGI_ROUTER_26B_MAX_SWAP_GB`，預設 20GB。
- 單併發，且 prompt 長度未超過 `MAGI_ROUTER_26B_MAX_PROMPT_CHARS`。

任一條件不成立時，MAGI 會退回 E4B，並在 `model_route_decision.blocked_reasons` 留下原因，例如 `disk_free<70GB` 或 `26b_not_live`。

## 環境變數

| 變數 | 預設 | 說明 |
| --- | --- | --- |
| `MAGI_SMART_MODEL_ROUTER` | `1` | 啟用智慧模型路由 |
| `MAGI_ROUTER_26B_MIN_DISK_GB` | `70` | 26B 最低可用磁碟 |
| `MAGI_ROUTER_26B_MIN_FREE_GB` | `8` | 26B 最低 free + inactive memory |
| `MAGI_ROUTER_26B_MAX_SWAP_GB` | `20` | 26B 最高 swap |
| `MAGI_ROUTER_QUALITY_PROMPT_CHARS` | `6000` | 超過此長度視為高品質任務 |
| `MAGI_ROUTER_26B_MAX_PROMPT_CHARS` | `60000` | 超過此長度不啟用 26B，避免 KV cache 爆量 |

## Live 驗證

```bash
python3 scripts/ops/smart_model_router_live.py --chat-probe --json
python3 scripts/ops/model_live_gate.py --expect auto --json
python3 scripts/ops/resource_governor.py status --json
```

判讀方式：

- `smart_model_router_live ok=true` 代表路由與本機聊天探針通過。
- `26b_blocked_reasons` 有內容時不是故障，而是安全閘門正在保護主機。
- 若 `active_models` 包含 26B 且 `26b_blocked_reasons` 為空，法律分析、翻譯、摘要等高品質任務會升級 26B。

## 小模型退場原則

小模型不應一次全部移除。退場順序如下：

1. 保留 `modernbert-embed-4bit`，因為它是向量檢索模型，不是聊天模型。
2. 確認 26B-A4B 可在白天安全通過 live gate。
3. 讓 Phi-4 / SmolLM3 從常駐改成按需或備援。
4. 跑工具調用回歸：行程、氣象、法扶、閱卷、筆錄、書狀、實務見解不得誤調用。
5. 連續觀察後再移除 launch agent。

## 紅線

- 不使用中國模型或中國供應商模型。
- 不在 `disk_free<70GB` 時下載或啟動新大模型。
- 不在 swap 快速上升時啟動 26B / 31B。
- 不讓 31B Dense 進入商用預設，除非完整 legal workload benchmark 無 OOM 並且品質優於 26B-A4B。
