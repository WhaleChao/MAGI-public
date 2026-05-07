# -*- coding: utf-8 -*-
"""
06_F.py — 消債蘿蔔特 補件書狀產生器（單機版）

視覺風格仿 05_E.py。
執行：python3 06_F.py
依賴：PyQt5、MAGI_v2 src/supplement_core
"""

import os
import sys
import subprocess

# ── 🔑 路徑注入：讓 supplement_core 可以被 import ───────────────────────────
_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT_CANDIDATES = [
    os.path.abspath(os.path.join(_HERE, "..", "..")),
    os.path.abspath(os.path.join(_HERE, "..")),
    "/Users/ai/Desktop/MAGI_v2",
]
_DEFAULT_MAGI_V2 = next(
    (
        root for root in _ROOT_CANDIDATES
        if os.path.isdir(os.path.join(root, "src", "supplement_core"))
    ),
    "/Users/ai/Desktop/MAGI_v2",
)
MAGI_V2 = os.environ.get("MAGI_V2_ROOT", _DEFAULT_MAGI_V2)
sys.path.insert(0, os.path.join(MAGI_V2, "src"))
sys.path.insert(0, MAGI_V2)
os.environ.setdefault("MAGI_ROOT", MAGI_V2)

# ── 📁 案件根目錄 ─────────────────────────────────────────────────────────────
_DEFAULT_CASE_ROOT = (
    "/Users/ai/Library/CloudStorage/SynologyDrive-homes"
    "/01_案件/法扶案件/消費者債務清理"
)
OSC_CASE_ROOT = os.environ.get("OSC_CASE_ROOT", _DEFAULT_CASE_ROOT)

from PyQt5.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QListWidget, QListWidgetItem, QLabel, QLineEdit, QPushButton,
    QComboBox, QProgressBar, QTableWidget, QTableWidgetItem,
    QHeaderView, QMessageBox, QSplitter, QGroupBox, QSizePolicy,
    QAbstractItemView, QTextEdit,
)
from PyQt5.QtCore import Qt, QThread, pyqtSignal, QObject
from PyQt5.QtGui import QFont


# ─────────────────────────────────────────────────────────────────────────────
# 🧵 背景工作執行緒（Step 1）
# ─────────────────────────────────────────────────────────────────────────────

class ExtractWorker(QObject):
    """在 QThread 中執行 load_text + extract + find_candidates，避免 UI 凍結。"""

    finished = pyqtSignal(dict)   # 成功：{"extracted": dict, "matched": list}
    error    = pyqtSignal(str)    # 失敗：錯誤訊息

    def __init__(self, ruling_pdf_path: str, case_meta: dict):
        super().__init__()
        self.ruling_pdf_path = ruling_pdf_path
        self.case_meta       = case_meta

    def run(self):
        try:
            from supplement_core import load_text, extract, find_candidates
        except ImportError as e:
            self.error.emit(
                f"❌ 無法 import supplement_core\n{e}\n\n"
                f"請確認 MAGI_v2 已部署，並設定 env MAGI_V2_ROOT"
            )
            return
        try:
            text_result = load_text(self.ruling_pdf_path)
            ruling_text = text_result.get("text", "") if isinstance(text_result, dict) else str(text_result or "")
            extracted   = extract(ruling_text)
            matched     = find_candidates(self.case_meta, extracted.get("items", []))
            self.finished.emit({"extracted": extracted, "matched": matched})
        except Exception as e:
            self.error.emit(f"⚠️ 抽取補件項目時發生錯誤：\n{e}")


# ─────────────────────────────────────────────────────────────────────────────
# 🖥  主視窗
# ─────────────────────────────────────────────────────────────────────────────

class SupplementGenerator(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("消債蘿蔔特 - 補件書狀產生器")
        self.setGeometry(150, 80, 1200, 800)

        # 狀態
        self._case_meta  : dict | None = None   # 目前選中案件 meta
        self._matched    : list        = []      # find_candidates 回傳
        self._extracted  : dict | None = None   # extract 回傳

        self._thread : QThread | None  = None
        self._worker : ExtractWorker | None = None

        self._init_ui()
        self._refresh_case_list()

    # ── 🏗  UI 初始化 ────────────────────────────────────────────────────────

    def _init_ui(self):
        """建立左右分欄佈局。"""
        central = QWidget()
        self.setCentralWidget(central)
        root_layout = QHBoxLayout(central)
        root_layout.setContentsMargins(8, 8, 8, 8)
        root_layout.setSpacing(8)

        # 建立左右 splitter
        splitter = QSplitter(Qt.Horizontal)
        root_layout.addWidget(splitter)

        # 左欄
        splitter.addWidget(self._build_left_panel())
        # 右欄
        splitter.addWidget(self._build_right_panel())
        splitter.setStretchFactor(0, 1)
        splitter.setStretchFactor(1, 3)

    def _build_left_panel(self) -> QWidget:
        """左欄：案件清單 + 重整按鈕。"""
        panel = QWidget()
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(0, 0, 0, 0)

        # 標題
        title = QLabel("選擇案件")
        title.setFont(QFont("", 11, QFont.Bold))
        layout.addWidget(title)

        # 案件清單
        self.case_list = QListWidget()
        self.case_list.setSelectionMode(QAbstractItemView.SingleSelection)
        self.case_list.itemClicked.connect(self._on_case_selected)
        layout.addWidget(self.case_list)

        # 重整按鈕
        btn_refresh = QPushButton("🔄 重整清單")
        btn_refresh.clicked.connect(self._refresh_case_list)
        layout.addWidget(btn_refresh)

        return panel

    def _build_right_panel(self) -> QWidget:
        """右欄：案件資訊 + 程序選擇 + 書狀號 + 裁定下拉 + Step 按鈕 + 補件表格。"""
        panel = QWidget()
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(4, 4, 4, 4)
        layout.setSpacing(8)

        # ① 案件資訊（唯讀）
        grp_info = QGroupBox("① 案件資訊")
        grp_info_layout = QVBoxLayout(grp_info)
        self.lbl_case_no   = QLabel("案號：—")
        self.lbl_party     = QLabel("當事人：—")
        self.lbl_court     = QLabel("法院：—")
        self.lbl_case_dir  = QLabel("路徑：—")
        self.lbl_case_dir.setWordWrap(True)
        for lbl in (self.lbl_case_no, self.lbl_party, self.lbl_court, self.lbl_case_dir):
            grp_info_layout.addWidget(lbl)
        layout.addWidget(grp_info)

        # ② 程序選擇
        grp_proc = QGroupBox("② 程序選擇")
        grp_proc_layout = QHBoxLayout(grp_proc)
        self.combo_procedure = QComboBox()
        self.combo_procedure.addItems(["更生", "清算"])
        grp_proc_layout.addWidget(self.combo_procedure)
        grp_proc_layout.addStretch()
        layout.addWidget(grp_proc)

        # ③ 書狀號
        grp_brief = QGroupBox("③ 書狀號（N，可修改）")
        grp_brief_layout = QHBoxLayout(grp_brief)
        self.spin_brief_no = QLineEdit("1")
        self.spin_brief_no.setMaximumWidth(80)
        grp_brief_layout.addWidget(QLabel("第"))
        grp_brief_layout.addWidget(self.spin_brief_no)
        grp_brief_layout.addWidget(QLabel("份書狀"))
        grp_brief_layout.addStretch()
        layout.addWidget(grp_brief)

        # ④ 裁定 PDF 下拉
        grp_pdf = QGroupBox("④ 裁定 PDF 選擇")
        grp_pdf_layout = QHBoxLayout(grp_pdf)
        self.combo_pdf = QComboBox()
        self.combo_pdf.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)
        grp_pdf_layout.addWidget(self.combo_pdf)
        layout.addWidget(grp_pdf)

        # Step 1 按鈕 + 進度條
        self.btn_step1 = QPushButton("📄 Step 1：抽取補件項目")
        self.btn_step1.setEnabled(False)
        self.btn_step1.clicked.connect(self._on_step1)
        layout.addWidget(self.btn_step1)

        self.progress = QProgressBar()
        self.progress.setRange(0, 0)  # 不定長進度
        self.progress.setVisible(False)
        layout.addWidget(self.progress)

        # ⑤ 補件項目表格（可編輯）
        grp_items = QGroupBox("⑤ 補件項目（可編輯，✓ 勾選後產生書狀）")
        grp_items_layout = QVBoxLayout(grp_items)

        self.table = QTableWidget(0, 5)
        self.table.setHorizontalHeaderLabels(
            ["✓ 勾選", "分類 (category)", "期間 (period)", "必要", "附件選擇"]
        )
        hh = self.table.horizontalHeader()
        hh.setSectionResizeMode(0, QHeaderView.ResizeToContents)
        hh.setSectionResizeMode(1, QHeaderView.Stretch)
        hh.setSectionResizeMode(2, QHeaderView.Stretch)
        hh.setSectionResizeMode(3, QHeaderView.ResizeToContents)
        hh.setSectionResizeMode(4, QHeaderView.Stretch)
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        grp_items_layout.addWidget(self.table)
        layout.addWidget(grp_items, stretch=1)

        # Step 2 按鈕
        self.btn_step2 = QPushButton("💾 Step 2：產生 docx 並開啟")
        self.btn_step2.setEnabled(False)
        self.btn_step2.clicked.connect(self._on_step2)
        layout.addWidget(self.btn_step2)

        return panel

    # ── 📋 案件清單 ──────────────────────────────────────────────────────────

    def _refresh_case_list(self):
        """掃描 OSC_CASE_ROOT，列出所有子目錄（案件）。"""
        self.case_list.clear()
        if not os.path.isdir(OSC_CASE_ROOT):
            self.case_list.addItem(f"⚠️ 找不到目錄：{OSC_CASE_ROOT}")
            return
        entries = sorted(
            e for e in os.listdir(OSC_CASE_ROOT)
            if os.path.isdir(os.path.join(OSC_CASE_ROOT, e))
            and not e.startswith(".")
        )
        for name in entries:
            item = QListWidgetItem(name)
            item.setData(Qt.UserRole, os.path.join(OSC_CASE_ROOT, name))
            self.case_list.addItem(item)

    # ── 🖱 選案件 ────────────────────────────────────────────────────────────

    def _on_case_selected(self, list_item: QListWidgetItem):
        """使用者點選案件 → parse_case_meta → 填入資訊欄位。"""
        case_dir = list_item.data(Qt.UserRole)
        try:
            from supplement_core import parse_case_meta
        except ImportError as e:
            QMessageBox.critical(
                self, "Import 失敗",
                f"無法 import supplement_core\n{e}\n\n"
                f"請確認 MAGI_v2 已部署，並設定 env MAGI_V2_ROOT"
            )
            return

        try:
            meta = parse_case_meta(case_dir)
        except Exception as e:
            QMessageBox.warning(self, "案件解析失敗", str(e))
            return

        self._case_meta = meta
        self._matched   = []
        self._extracted = None

        # 填入唯讀資訊
        parties_str = "、".join(meta.get("parties", [])) or "—"
        self.lbl_case_no.setText(f"案號：{meta.get('case_no') or meta.get('sample_id') or '—'}")
        self.lbl_party.setText(f"當事人：{parties_str}")
        self.lbl_court.setText(f"法院：{meta.get('court') or '—'}")
        self.lbl_case_dir.setText(f"路徑：{case_dir}")

        # 預設程序
        proc_default = meta.get("procedure_default", "更生")
        idx = self.combo_procedure.findText(proc_default)
        if idx >= 0:
            self.combo_procedure.setCurrentIndex(idx)

        # 預設書狀號（系統建議：brief_seq_next）
        self.spin_brief_no.setText(str(meta.get("brief_seq_next", 1)))

        # 填入裁定 PDF 下拉
        self._load_court_notices(meta)

        # 清空補件表格
        self.table.setRowCount(0)
        self.btn_step1.setEnabled(True)
        self.btn_step2.setEnabled(False)

    def _load_court_notices(self, meta: dict):
        """列出 09_法院通知或程序裁定 內 PDF，填入 combo_pdf。"""
        self.combo_pdf.clear()
        try:
            from supplement_core import list_court_notices
            notices = list_court_notices(meta)
        except Exception as e:
            self.combo_pdf.addItem(f"⚠️ 無法讀取：{e}")
            return

        if not notices:
            self.combo_pdf.addItem("（無 PDF）")
            return

        for n in notices:
            self.combo_pdf.addItem(n["filename"], userData=n["path"])

    # ── 📝 Step 1 ────────────────────────────────────────────────────────────

    def _on_step1(self):
        """啟動背景執行緒：load_text → extract → find_candidates。

        Step 1 開始前先執行 M10：更新 DB 案號並在 UI 顯示結果。
        """
        if not self._case_meta:
            return

        pdf_path = self.combo_pdf.currentData()
        if not pdf_path:
            QMessageBox.warning(self, "未選裁定 PDF", "請先選擇裁定 PDF 檔案。")
            return

        # ── M10：案號更新（update_case_no_from_notices）─────────────────────
        try:
            from supplement_core import list_court_notices, update_case_no_from_notices
            notices = list_court_notices(self._case_meta)
            upd = update_case_no_from_notices(self._case_meta, notices, dry_run=False)
            if upd.get("new_case_no") and upd["new_case_no"] != upd.get("current_case_no"):
                old_no = upd.get("current_case_no") or "（無）"
                new_no = upd["new_case_no"]
                # 更新本地 case_meta
                self._case_meta["court_case_number"] = new_no
                if upd.get("new_court"):
                    self._case_meta["court_name"] = upd["new_court"]
                # 更新 UI 案號欄
                self.lbl_case_no.setText(f"案號：{new_no}（已更新）")
                if upd.get("updated"):
                    QMessageBox.information(
                        self, "DB 案號已更新",
                        f"舊案號：{old_no}\n新案號：{new_no}\n\n來源：{upd.get('source_pdf', '')}"
                    )
            elif upd.get("new_case_no") and upd.get("errors") == ["already_current"]:
                # 已是最新，靜默
                pass
        except Exception as _upd_err:
            # 案號更新失敗不阻斷主流程
            import logging
            logging.getLogger("06_F").warning("update_case_no_from_notices: %s", _upd_err)

        # 停用按鈕，顯示進度
        self.btn_step1.setEnabled(False)
        self.btn_step2.setEnabled(False)
        self.progress.setVisible(True)
        self.table.setRowCount(0)

        # 建立執行緒
        self._thread = QThread()
        self._worker = ExtractWorker(pdf_path, self._case_meta)
        self._worker.moveToThread(self._thread)

        self._thread.started.connect(self._worker.run)
        self._worker.finished.connect(self._on_extract_done)
        self._worker.error.connect(self._on_extract_error)
        self._worker.finished.connect(self._thread.quit)
        self._worker.error.connect(self._thread.quit)
        self._thread.finished.connect(self._thread.deleteLater)

        self._thread.start()

    def _on_extract_done(self, result: dict):
        """Step 1 成功，填入補件項目表格。"""
        self._extracted = result["extracted"]
        self._matched   = result["matched"]

        self.progress.setVisible(False)
        self.btn_step1.setEnabled(True)

        # 確保 parties 顯示以 case_meta（資料夾名）為準，不受 LLM 抽取結果影響
        if self._case_meta:
            parties_str = "、".join(self._case_meta.get("parties", [])) or "—"
            self.lbl_party.setText(f"當事人：{parties_str}")

        items   = self._extracted.get("items", [])
        matched = self._matched  # list[dict]，與 items 等長

        self.table.setRowCount(len(items))

        for row, item in enumerate(items):
            # 對應 matched 資訊（若有）
            m = matched[row] if row < len(matched) else {}
            candidates: list[dict] = m.get("candidates", [])

            # col 0：勾選 checkbox
            chk = QTableWidgetItem()
            chk.setFlags(Qt.ItemIsUserCheckable | Qt.ItemIsEnabled)
            chk.setCheckState(Qt.Checked if item.get("mandatory", True) else Qt.Unchecked)
            self.table.setItem(row, 0, chk)

            # col 1：category（可編輯）
            cat_item = QTableWidgetItem(item.get("category", ""))
            self.table.setItem(row, 1, cat_item)

            # col 2：period（可編輯）
            period_item = QTableWidgetItem(item.get("period", ""))
            self.table.setItem(row, 2, period_item)

            # col 3：必要
            mandatory_item = QTableWidgetItem("✓" if item.get("mandatory") else "")
            mandatory_item.setFlags(Qt.ItemIsEnabled)
            mandatory_item.setTextAlignment(Qt.AlignCenter)
            self.table.setItem(row, 3, mandatory_item)

            # col 4：附件下拉（QComboBox）
            combo = QComboBox()
            combo.addItem("（不附）", userData=None)
            for c in candidates:
                label = c.get("filename", c.get("path", "?"))
                combo.addItem(label, userData=c.get("path"))
            # 預設選 selected
            selected = m.get("selected")
            if selected:
                for i in range(combo.count()):
                    if combo.itemData(i) == selected:
                        combo.setCurrentIndex(i)
                        break
            self.table.setCellWidget(row, 4, combo)

        self.table.resizeRowsToContents()
        self.btn_step2.setEnabled(True)

    def _on_extract_error(self, msg: str):
        """Step 1 失敗。"""
        self.progress.setVisible(False)
        self.btn_step1.setEnabled(True)
        QMessageBox.critical(self, "Step 1 失敗", msg)

    # ── 📦 Step 2 ────────────────────────────────────────────────────────────

    def _on_step2(self):
        """讀取表格內容 → write_brief_folder → open docx。"""
        if not self._case_meta or not self._extracted:
            return

        try:
            from supplement_core import write_brief_folder
        except ImportError as e:
            QMessageBox.critical(self, "Import 失敗", str(e))
            return

        # 收集勾選的 items 與使用者選擇的附件
        items_all = self._extracted.get("items", [])
        matched_out: list[dict] = []
        checked_items: list[dict] = []

        for row in range(self.table.rowCount()):
            chk = self.table.item(row, 0)
            if not chk or chk.checkState() != Qt.Checked:
                continue
            item = dict(items_all[row]) if row < len(items_all) else {}
            # 覆寫使用者編輯的 category / period
            cat_cell = self.table.item(row, 1)
            period_cell = self.table.item(row, 2)
            if cat_cell:
                item["category"] = cat_cell.text()
            if period_cell:
                item["period"] = period_cell.text()
            checked_items.append(item)

            # 附件選擇
            combo = self.table.cellWidget(row, 4)
            selected_path = combo.currentData() if combo else None
            m_row = self._matched[row] if row < len(self._matched) else {}
            matched_entry = dict(m_row)
            matched_entry["selected"] = selected_path
            matched_out.append(matched_entry)

        if not checked_items:
            QMessageBox.warning(self, "未選補件項", "請至少勾選一個補件項目。")
            return

        procedure = self.combo_procedure.currentText()
        try:
            brief_seq = int(self.spin_brief_no.text())
        except ValueError:
            brief_seq = self._case_meta.get("brief_seq_next", 1)

        # 臨時組合 extracted dict（含勾選後的 items）
        extracted_copy = dict(self._extracted)
        extracted_copy["items"] = checked_items

        try:
            result = write_brief_folder(
                self._case_meta,
                extracted_copy,
                matched_out,
                procedure=procedure,
                brief_seq=brief_seq,
            )
        except Exception as e:
            QMessageBox.critical(
                self, "Step 2 失敗",
                f"產生書狀時發生錯誤：\n{e}\n\n"
                f"目標路徑：{self._case_meta.get('case_dir', '—')}"
            )
            return

        docx_path = result.get("docx_path", "")
        folder_path = result.get("folder_path", "")

        # 開啟 docx
        if docx_path and os.path.exists(docx_path):
            subprocess.run(["open", docx_path])
        else:
            subprocess.run(["open", folder_path])

        QMessageBox.information(
            self, "✅ 完成",
            f"書狀資料夾已建立：\n{folder_path}\n\n文件：{os.path.basename(docx_path)}"
        )


# ─────────────────────────────────────────────────────────────────────────────
# 🚀 Entry Point
# ─────────────────────────────────────────────────────────────────────────────

def main():
    app = QApplication(sys.argv)
    win = SupplementGenerator()
    win.show()
    sys.exit(app.exec_())


if __name__ == "__main__":
    main()
