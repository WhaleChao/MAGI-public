# MAGI 法律工作流（公開版）

公開版保留通用法律工作流引擎，但不包含私人 OSC 資料、事務所案件、NAS 路徑、帳號、金鑰或私有實務見解庫。

## 已開放的通用能力

- `api/legal_workflow.py`：判斷法律研究、書狀覆核、法扶回報三類工作流。
- `detect_legal_workflow()`：依文字、案由、文件類型選擇代理與案件設定。
- `workflow_prompt_block()`：產生可放入 AI prompt 的覆核規則。
- `workflow_review()`：檢查無來源引用、待確認欄位等高風險輸出。
- `append_workflow_footer()`：在法律回覆末端標示工作流與人工覆核門檻。

## 公開版隔離原則

公開版的 `api/domains/judgment_flow.py` 仍不提供即時裁判收集或私有實務見解庫。當使用者詢問法律研究時，MAGI 會回覆設定提示與工作流規則，提醒部署者先接上自己的合法資料來源。

若要接入公開資料庫或 MCP，請自行建立 adapter，並維持以下規則：

- 不提交 API key、cookie、帳密、私有資料庫 dump。
- 不把摘要當全文引用。
- 查不到來源時直接回覆查不到。
- 書狀正式引用前，仍需人工核對全文。
