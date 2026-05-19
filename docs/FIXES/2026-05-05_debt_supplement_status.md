# 2026-05-05 — 消債補件模組桌面計劃狀態收斂

來源桌面計劃：`消債補件模組_實作計畫.md`（已移至 `/Users/ai/Desktop/desktop_md_archive_20260505/`）。

## 結論

桌面計劃停在 M0/M0.5 的狀態已過期。MAGI repo 內已存在可用實作與測試覆蓋，本項不再列為桌面 MD 未完成項。

## 已落地範圍

- `src/supplement_core/`：案件 metadata、法院通知挑選、裁定文字載入、補件項目抽取、附件候選匹配、docx 產生、書狀資料夾寫入、案號更新。
- `integrations/debt_robot/06_F.py`：消債蘿蔔特補件書狀單機版入口。
- `api/debt_document_generator.py`：網頁版文件產生，含 `generate_supplement()`。
- `api/blueprints/osc_debt.py`：OSC 消債 API。
- `templates/osc_debt.html`：OSC 消債羅伯特頁面，含補件書狀 panel。
- `data/templates/D_supplement.docx`：補件模板資產。

## 本輪驗證

執行：

```bash
./venv/bin/python -m pytest -q \
  tests/test_debt_robot_source_modules.py \
  tests/test_osc_web_smoke.py::test_debt_forms_list \
  tests/test_osc_web_smoke.py::test_debt_schema_returns_fields \
  tests/test_osc_web_smoke.py::test_debt_source_status_uses_bundled_source
```

結果：`5 passed in 0.53s`。

覆蓋內容：

- 六個消債蘿蔔特模組來源完整性，包含 `06_F.py` 與 `src/supplement_core/__init__.py`。
- 聲請狀、財產說明書、債權人清冊、陳報狀、補件書狀 docx 產出 smoke。
- PDF 合併 smoke。
- OSC 消債 forms/schema/source-status endpoint smoke。

## 殘留邊界

這不是程式未完成，而是產品品質抽測：

- 尚未在本輪新跑 5 件真實消債案件的人工品質評分。
- 附件配對命中率仍應以真實案件抽樣追蹤，而不是桌面 MD 待辦。

後續若要提升品質，請另開「消債補件品質抽測」任務，輸出抽測表與錯誤樣態，不再沿用桌面舊計劃。
