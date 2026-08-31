# MAGI V3 可觀測性契約

MAGI 使用 W3C `traceparent` 串接 Agent Gateway、模型、工具、worker 與高風險 receipt。程式只接受 allowlist 的類別與數值欄位；prompt、模型內容、工具參數、案件識別、檔案路徑、browser profile 與 secret 不得進入 span。

production 預設先寫入 mode-0600 的本機 JSONL spool。設為 `MAGI_OTEL_MODE=otlp` 時，仍保留本機 spool，並只允許將 OTLP/HTTP JSON 傳至 loopback Collector。Collector 使用 `config/observability/otel-collector.yaml`，Phoenix 必須自架且不得對外公開。

高風險 job 的 canonical receipt 由 ledger 自動補入 32 位 hex `trace_id`；外部 worker 不能藉由省略 trace 讓 receipt 脫離追蹤。行為評測同時驗證必須／禁止的 span 與工具、重試上限、receipt 及終態，而不檢查或保存案件內容。
