#!/usr/bin/env python3
"""Build the rc643 MAGI maintenance encyclopedia and machine-readable source index."""

from __future__ import annotations

import argparse
import ast
import hashlib
import html
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from collections import Counter, defaultdict
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Iterable


MANUAL_VERSION = "rc643"
RELEASE_ID = "v3-20260831-rc643-r75-hotfix7-r1"
SOURCE_COMMIT = "a58db40d7675fe654782d297c3c070407fb89727"
RELEASE_MANIFEST_SHA = "96ab97392dfbdbf46cb895e92c0db725d80ef53202ad5568dad65bdf76cd7829"
FORMAL_CHAIN_SHA = "b6f29aeda6062654317231c2bbb73ee6d382bbaecd2bc88f84e51d3cfb89dbb6"
RELEASE_STATUS = "active_production"
BUILD_DATE = "2026-08-31"

MANUAL_BASENAME = f"MAGI_V3_維修百科全書_{MANUAL_VERSION}"
INDEX_BASENAME = f"MAGI_V3_原始碼索引_{MANUAL_VERSION}.json"
_GENERATED_MANUAL_RE = re.compile(
    r"^(?:docs|magi_v3/manual_assets)/MAGI_V3_(?:維修百科全書|原始碼索引)_rc\d+\.(?:html|md|pdf|json)$"
)
_WORKSTATION_PATH_TEXT_RE = re.compile(
    r"(?i)(?:\b[a-z]:[\\/]|/(?:Users|Volumes)/)[^\s'\"`\]\[(){},;]*"
)


@dataclass(frozen=True)
class Symbol:
    qualname: str
    kind: str
    line: int
    end_line: int
    signature: str
    summary: str


@dataclass(frozen=True)
class FileRecord:
    path: str
    category: str
    extension: str
    bytes: int
    lines: int
    sha256: str
    symbols: tuple[Symbol, ...]
    imports: tuple[str, ...]
    parse_error: str


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for block in iter(lambda: fh.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def redact_workstation_paths(value: str) -> str:
    """Keep documentation useful without copying private absolute paths."""

    return _WORKSTATION_PATH_TEXT_RE.sub("<workstation-path>", value)


def first_sentence(value: str | None, limit: int = 180) -> str:
    text = re.sub(r"\s+", " ", value or "").strip()
    if not text:
        return ""
    text = re.split(r"(?<=[。.!?])\s+", text, maxsplit=1)[0]
    return redact_workstation_paths(text)[:limit]


def signature_for(node: ast.AST) -> str:
    if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
        return ""
    try:
        args = ast.unparse(node.args)
    except Exception:
        args = "…"
    prefix = "async " if isinstance(node, ast.AsyncFunctionDef) else ""
    return redact_workstation_paths(f"{prefix}{node.name}({args})")[:300]


def python_details(path: Path) -> tuple[tuple[Symbol, ...], tuple[str, ...], str]:
    try:
        text = path.read_text(encoding="utf-8")
        tree = ast.parse(text)
    except Exception as exc:
        return (), (), type(exc).__name__
    symbols: list[Symbol] = []
    imports: set[str] = set()

    class Visitor(ast.NodeVisitor):
        def __init__(self) -> None:
            self.stack: list[str] = []

        def _symbol(self, node: ast.AST, kind: str, name: str) -> None:
            qualname = ".".join([*self.stack, name])
            symbols.append(
                Symbol(
                    qualname=qualname,
                    kind=kind,
                    line=int(getattr(node, "lineno", 0)),
                    end_line=int(getattr(node, "end_lineno", getattr(node, "lineno", 0))),
                    signature=signature_for(node),
                    summary=first_sentence(ast.get_docstring(node)),
                )
            )

        def visit_ClassDef(self, node: ast.ClassDef) -> None:
            self._symbol(node, "class", node.name)
            self.stack.append(node.name)
            self.generic_visit(node)
            self.stack.pop()

        def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
            self._symbol(node, "method" if self.stack else "function", node.name)
            self.stack.append(node.name)
            self.generic_visit(node)
            self.stack.pop()

        def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
            self.visit_FunctionDef(node)  # type: ignore[arg-type]

        def visit_Import(self, node: ast.Import) -> None:
            for item in node.names:
                imports.add(item.name)

        def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
            if node.module:
                imports.add(node.module)

    Visitor().visit(tree)
    return tuple(symbols), tuple(sorted(imports)), ""


def category_for(rel: str) -> str:
    if rel.startswith("tests/"):
        return "tests"
    if rel.startswith("magi_v3/"):
        return "v3_kernel"
    if rel.startswith("api/"):
        return "api"
    if rel.startswith("skills/"):
        return "skills"
    if rel.startswith("scripts/"):
        return "operations"
    if rel.startswith("config/"):
        return "configuration"
    if rel.startswith("gui/"):
        return "gui"
    if rel.startswith("templates/") or rel.startswith("static/"):
        return "web_ui"
    if rel.startswith("casper_ecosystem/"):
        return "legal_aid_legacy_adapter"
    if rel.startswith("docs/"):
        return "documentation"
    return rel.split("/", 1)[0] if "/" in rel else "root"


def tracked_files(root: Path) -> list[str]:
    output = subprocess.check_output(["git", "ls-files"], cwd=root, text=True)
    paths = {line.strip() for line in output.splitlines() if line.strip()}
    generator = "scripts/docs/build_magi_encyclopedia.py"
    if (root / generator).is_file():
        paths.add(generator)
    return sorted(path for path in paths if not _GENERATED_MANUAL_RE.fullmatch(path))


def build_inventory(root: Path) -> list[FileRecord]:
    records: list[FileRecord] = []
    for rel in tracked_files(root):
        path = root / rel
        if not path.is_file() or path.is_symlink():
            continue
        data = path.read_bytes()
        lines = data.count(b"\n") + (1 if data and not data.endswith(b"\n") else 0)
        symbols: tuple[Symbol, ...] = ()
        imports: tuple[str, ...] = ()
        parse_error = ""
        if path.suffix == ".py":
            symbols, imports, parse_error = python_details(path)
        records.append(
            FileRecord(
                path=rel,
                category=category_for(rel),
                extension=path.suffix.lower() or "[none]",
                bytes=len(data),
                lines=lines,
                sha256=hashlib.sha256(data).hexdigest(),
                symbols=symbols,
                imports=imports,
                parse_error=parse_error,
            )
        )
    return records


def route_map(root: Path, records: Iterable[FileRecord]) -> list[dict[str, Any]]:
    routes: list[dict[str, Any]] = []
    for record in records:
        if record.extension != ".py" or record.category not in {"api", "v3_kernel", "skills"}:
            continue
        path = root / record.path
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except Exception:
            continue
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            for decorator in node.decorator_list:
                if not isinstance(decorator, ast.Call):
                    continue
                name = ""
                if isinstance(decorator.func, ast.Attribute):
                    name = decorator.func.attr
                elif isinstance(decorator.func, ast.Name):
                    name = decorator.func.id
                if name != "route" or not decorator.args:
                    continue
                try:
                    route = ast.literal_eval(decorator.args[0])
                except Exception:
                    continue
                methods = ["GET"]
                for kw in decorator.keywords:
                    if kw.arg == "methods":
                        try:
                            methods = list(ast.literal_eval(kw.value))
                        except Exception:
                            methods = ["dynamic"]
                routes.append(
                    {
                        "route": str(route),
                        "methods": [str(x) for x in methods],
                        "path": record.path,
                        "line": node.lineno,
                        "handler": node.name,
                    }
                )
    return sorted(routes, key=lambda x: (x["route"], x["path"], x["line"]))


def source_url(repo: str, path: str, line: int | None = None) -> str:
    url = f"https://github.com/WhaleChao/{repo}/blob/{SOURCE_COMMIT}/{path}"
    return f"{url}#L{line}" if line else url


def md_escape(value: object) -> str:
    return str(value).replace("|", "\\|").replace("\n", " ")


def table(headers: list[str], rows: Iterable[Iterable[object]]) -> str:
    data = ["| " + " | ".join(md_escape(x) for x in headers) + " |", "| " + " | ".join("---" for _ in headers) + " |"]
    data.extend("| " + " | ".join(md_escape(x) for x in row) + " |" for row in rows)
    return "\n".join(data) + "\n"


def fenced(code: str, language: str = "text") -> str:
    return f"```{language}\n{code.rstrip()}\n```\n"


def read_json(root: Path, rel: str) -> dict[str, Any]:
    return json.loads((root / rel).read_text(encoding="utf-8"))


def excerpt(root: Path, rel: str, start: int, end: int) -> str:
    lines = (root / rel).read_text(encoding="utf-8").splitlines()
    selected = []
    for idx in range(max(1, start), min(end, len(lines)) + 1):
        content = lines[idx - 1].rstrip()
        selected.append(f"{idx:>5}  {content}" if content else f"{idx:>5}")
    return "\n".join(selected)


CHAPTERS = [
    ("ch01", "1. 閱讀方法、權威順序與安全界線"),
    ("ch02", "2. 整體架構與功能連動總圖"),
    ("ch03", "3. 程序角色、服務、埠與啟動順序"),
    ("ch04", "4. 原始碼目錄、責任邊界與讀碼方法"),
    ("ch05", "5. 請求路由、身分、授權與工具執行"),
    ("ch06", "6. 排程、重試、checkpoint 與自然終局"),
    ("ch07", "7. 法扶派案、附件、開辦與報結"),
    ("ch08", "8. 閱卷、繳費憑證、下載與簽章對帳"),
    ("ch09", "9. 案件、NAS、Google Drive 與雙邊映射"),
    ("ch10", "10. OSC、日曆、待辦、帳務與債務文件"),
    ("ch11", "11. PDF、OCR、筆錄、翻譯與知識庫"),
    ("ch12", "12. 公開創作工具：Cookie Cutter 與影片工作室"),
    ("ch13", "13. 本機模型、資源閘門與降級策略"),
    ("ch14", "14. 通知、外網入口、TG/Discord 與安全邊界"),
    ("ch15", "15. Menubar、NERV、Doctor、Guardian 與紅燈語意"),
    ("ch16", "16. 狀態、鎖、owner、收據與證據鏈"),
    ("ch17", "17. 不可變發行、LIVE 切換與自動回滾"),
    ("ch18", "18. 備份、還原、災難復原與 GitHub 保存"),
    ("ch19", "19. 測試、品質閘門與驗證器加速"),
    ("ch20", "20. 故障排查總則與決策樹"),
    ("ch21", "21. 分功能排查與排除手冊"),
    ("ch22", "22. 已知故障、根因、修復與防回歸"),
    ("ch23", "23. 日常維修、升級與自主演進守則"),
    ("appA", "附錄 A. 核心原始碼節錄與解讀"),
    ("appB", "附錄 B. 全部 production 原始碼索引"),
    ("appC", "附錄 C. 全部測試原始碼索引"),
    ("appD", "附錄 D. 設定、Schema、前端與腳本索引"),
    ("appE", "附錄 E. API 路由索引"),
    ("appF", "附錄 F. 維修命令速查與名詞表"),
]


LINKAGES = [
    ("Web／Mobile／TG／Discord", "api/server.py、api/webhooks/*、api/discord_bot.py", "api/pipelines/* → api/orchestrator.py", "專用 skill / api/osc/*", "durable notification / receipt / health"),
    ("法扶 Gmail", "scripts/ops/laf_gmail_dispatch_scan.py", "laf_automation_v2.py 郵件分類", "laf_portal_new_files_scan.py／laf-orchestrator", "附件歸檔＋業務健康"),
    ("閱卷通知／繳費", "skills/file-review-orchestrator/action.py", "file_review_receipts.py", "法院入口下載／上傳佇列", "signature receipt＋Menubar"),
    ("Drive all-files", "cron_service.py", "scripts/drive_case_sync_worker.py", "api/osc/drive_case_sync.py", "checkpoint＋terminal outcome"),
    ("案件與 OSC", "api/blueprints/osc_*.py", "api/osc/*", "NAS／MariaDB／calendar", "read-back result＋business snapshot"),
    ("PDF／OCR／筆錄", "skills/pdf-*、skills/documents/*", "OCR queue／namer／bookmarker", "NAS 文件與知識索引", "品質收據＋自測"),
    ("Cookie Cutter", "api/blueprints/cookie_cutter.py", "skills/cookie_stl/*", "隔離子程序", "ZIP/STL attestation，零持久化"),
    ("影片工作室", "api/blueprints/video_studio.py", "magi_v3/video_autopilot_adapter.py", "固定上游 portrait normalizer＋本機 ffmpeg＋使用者素材", "指令理解回顯、素材／plan hash、MP4 畫質 attestation，零外送與零持久化"),
    ("判決趨勢", "api/blueprints/sentencing_trends.py", "api/sentencing_trends.py", "本機裁判庫＋臺灣法律 MCP", "逐筆納入／排除理由、官方全文核對、量刑統計與穩定裁判頁連結"),
    ("實務見解", "api/blueprints/legal_research.py", "api/domains/judgment_flow.py", "本機裁判庫＋官方全文 MCP 補量", "來源綁定摘要、官方 JID／網址／全文驗證與排除理由"),
    ("健康與自修", "business_module_live_check.py", "function_health_index.py／magi_doctor.py", "magi_self_repair_guardian.py", "固定語意紅燈＋安全修復"),
]


FAULTS = [
    ("F-001", "Codex 內 formal test 立刻 rc71", "Codex 已在 Seatbelt 內，再建立第二層 macOS Seatbelt 失敗；測試入口甚至未執行。", "改由 host-outer hash-bound runner 執行；不得把 rc71 當產品測試失敗，也不得弱化 Seatbelt gate。", "runner receipt 必須 exact/full、marker 存在、source/manifest SHA 相符。", "scripts/v3_release_gate.py；formal runner evidence"),
    ("F-002", "Gateway 回應約 6 秒，shell 正常", "三角色一律綁 launchd Background QoS，使 HTTP gateway 被降優先。", "gateway=Interactive，control/supervisor=Background；cookie 子程序資源上限不變。", "rendered plist 三角色 ProcessType 精確，gateway LIVE 延遲與資源界線均通。", "scripts/v3_deploy_prepare.py"),
    ("F-003", "Drive checkpoint phase=scan_plan 且 hash cache=0", "duplicate/same-content 兩條路徑直接 local_file_md5，繞過 DriveFileCheckpoint，deadline 又被 generic except 吞掉。", "全部共用 checkpointed MD5；DriveCaseSyncDeadline 先行 re-raise；fingerprint 變更才重算。", "二次規劃零新增 MD5、cache>0、deadline 不前進 cursor。", "api/osc/drive_case_sync.py；magi_v3/drive_file_checkpoint.py"),
    ("F-004", "大量 semantic collision 但沒有內容證據", "NAS alias 沒有 MD5，僅因 NFKC/casefold 同桶便被當成真衝突。", "對 local bucket 使用 bounded checkpointed MD5；同內容選 deterministic representative，異內容才保留人工確認。", "同內容解衝且 cache>0；異內容、native distinct-ID 仍 fail closed。", "api/osc/drive_case_sync.py"),
    ("F-005", "local hash/storage 失敗後仍可能規劃寫入", "duplicate lookup 回傳 local_hash_failed，但後續 download/upload planning 未被一併抑制。", "集中 suppress_case_write_actions，清空下載/上傳 action 並固定 cursor；retryable 與 hard failure 分流。", "timeout/storage/hard error 均零 transfer；partial failure cursor 不前進。", "api/osc/drive_case_sync.py"),
    ("F-006", "Drive outcome gate 對假資料放行或對真 terminal 誤拒", "validator 使用 subset、int(bool)、虛構 aggregate failed，且未綁 canonical status/worker/cron。", "exact schema、strict non-bool int、真 chunk/cycle cursor、canonical path、worker/snapshot/command 三重綁定。", "unknown/raw/bool/jump/early wrap/old release 全拒；真 terminal 通過。", "evidence drive_outcome_gate；scripts/drive_case_sync_worker.py"),
    ("F-007", "每兩分鐘 Drive running，LIVE preflight 永遠等不到 idle", "preflight 在 supervisor 停止前要求 Drive idle，與高頻排程形成活鎖。", "切成 pre-quiesce 身分驗證與 supervisor unload 後 post-quiesce gate；由既有 rollback envelope 保護。", "original cleanup→post gate→install；失敗 restore/start old；不直接 kill child。", "scripts/v3_cutover/*；release live wrapper"),
    ("F-008", "安全的 Drive owner 被誤判 foreign", "ps command 字串會破壞含空白 argv；lsof 有多個 ftxt；wrapper exec 後三層 wrapper 不再出現在 argv。", "KERN_PROCARGS2 結構化 argv＋嚴格 lsof first ftxt；由 sealed cron 最後 -- 推導 exact worker argv。", "實況 owner argc/worker/release 通過；missing/reorder/extra/foreign 全拒。", "release live wrapper；skills/ops/cron_scheduler.py"),
    ("F-009", "排程 handoff 因固定 occurrence/reason tuple 被拒", "evidence wrapper 寫死某次 occurrence，並把 storage rc0 誤綁 process_interrupted/143。", "動態封存當下 exact occurrence digest；依 sealed business recovery labels 驗證 reason，支援合法 storage_recovered 派生票。", "ID 輪替、raw extra、錯 label、bool rc 拒；current storage tuple 通。", "magi_v3/business_recovery.py；skills/ops/cron_scheduler.py"),
    ("F-010", "閱卷入口 7 件、驗證 0 件", "raw result 與 public result_text 選取順序不同；renderer 文案差異造成簽章不一致。", "canonical content marker 改為 result_text→result→row_text；雙側 canonical signature 對帳。", "result 同義欄相等、真正 revision 變更必換簽章。", "magi_v3/file_review_receipts.py"),
    ("F-011", "閱卷 invalid/duplicate/uppercase hash 仍顯示成功", "normalize 後才驗，靜默丟棄非法元素；handled declared list 未被精確驗證。", "四個 raw list 必須等於 normalized；handled=processed∪existing；count strict int。", "invalid extra、duplicate、uppercase、non-list、bool/negative count 全拒。", "skills/file-review-orchestrator/action.py"),
    ("F-012", "沒有待下載檔案卻顯示上輪失敗", "健康層把 0/0 reconciliation 或待確認狀態解讀成失敗，Menubar 優先級不正確。", "attention 優先，合法 zero receipt 顯示正常；未知 reason 固定安全文案。", "ok+0/0 正常；不完整 receipt 維持紅燈。", "scripts/ops/business_readiness_snapshot.py；gui/magi_menubar.py"),
    ("F-013", "法扶『已轉入／審查結果』被誤認已有附件", "只看主旨，未區分審查結果通知與入口實際可下載清單。", "審查結果主旨本身 needs_download=False；須由內文明確指示或官網 listing 證明。", "空 listing 與 listing timeout 分離；無附件不得盲重試/建案。", "laf_automation_v2.py；laf_portal_new_files_scan.py"),
    ("F-014", "法扶附件重試超上限仍盲跑／紅燈無解", "業務狀態把 portal 尚未上架、登入失效、資料錯誤混為同一 retry。", "期限內 bounded retry；達上限轉人工確認；登入/入口/案件資料分開呈現。", "每案 next action 明確；人工確認案不繼續盲試。", "scripts/ops/laf_portal_new_files_scan.py；business_module_live_check.py"),
    ("F-015", "繳費憑證顯示已上傳過但其實是不同閱卷", "去重鍵曾過度依賴案件或舊 receipt，未綁精確 payment event。", "只接受 exact v2 payment event＋檔案 SHA 的 canonical 私密 registry；輸出只留 opaque digest。", "不同事件不得互相跳過；同一事件重送才 idempotent。", "file-review payment registry / evidence payment gate"),
    ("F-016", "Cookie Cutter 表面粗糙、外壁斷點，或只有封閉外框時被拒絕", "舊契約只驗每條邊恰有兩個面；數個各自封閉的碎片仍可能假扮 watertight。float64 建模通過後轉成 STL float32 也可能讓近鄰頂點合併而產生切片裂縫；0.35 mm 輪廓門檻亦可能肉眼可見稜角。", "封閉外框建立單一 annular cutter；外壁必須是單一連通實體，最高層恰一個連續面與兩條封閉邊界。STL float32 座標再驗一次 manifold／方向／連通性；輪廓誤差收緊至 0.15 mm。只有與外框分離的內部圖案才建立鏡像 stamp，且同樣驗封閉頂面。", "附圖凹形框實測 0.112995 mm、1 個頂面／2 條邊界；12 組隨機外框與內圖案、斷裂雙殼反例、薄線與空框全部通過。正式 runtime 的切模／端點／資源／路由聚焦回歸 73/73。", "api/blueprints/cookie_cutter.py；skills/cookie_stl/*；tests/test_cookie_stl.py"),
    ("F-017", "Cookie 子程序資源限制失敗仍繼續生成", "setrlimit ImportError/OSError/ValueError 曾 silent pass。", "child 回固定 resource_error，engine 不執行；parent 精確 IPC schema；finally reap。", "engine_calls=0、無產品 bytes、child_reaped、leaks=0。", "api/blueprints/cookie_cutter.py"),
    ("F-018", "文字推理顯示 E4B（預期 26B）", "不是單一故障；可能是磁碟、free+inactive、swap、resource level 或 26B 未 live 的安全降級。", "依 model registry gate 逐項檢查；清理經核准工程垃圾後重新 probe，不強迫載入導致 OOM。", "decision_summary 明列 gate reason；26B 僅在全條件安全時啟用。", "api/model_router.py；config/model_registry.json"),
    ("F-019", "健康紅燈只因 legacy lock dir 不存在而漏查 owner", "owner validator early return，未繼續掃 legacy PID。", "lock glob 可為空，但 legacy paths 必查；persistent owner/calendar owner 使用 exact schema/argv allowlist。", "missing dir＋live foreign PID 必紅；合法 persistent owner 綠。", "refresh_live_health evidence；scripts/ops/audit_operational_hardening.py"),
    ("F-020", "健康 audit 被 inherited PYTHONPATH 指到 source/evidence", "subprocess 繼承 hostile environment，可能用錯版本。", "執行前綁 installed root、manifest、cron snapshot SHA，覆寫 cwd/PYTHONPATH/MAGI_ROOT。", "hostile env 測試仍只載 installed rc release。", "release refresh_live_health wrapper"),
    ("F-021", "formal bundle privacy gate 因測試 fixture 絕對路徑失敗", "scanner 將測試中的 workstation-style literal 視為私有路徑。", "fixture 以 runtime concatenation 建構，仍測 absolute path 但不把私有 literal 放入 bundle。", "fresh privacy gate violations=0。", "tests/v3/test_change_scope.py；test_validation_router.py"),
    ("F-022", "測試工作樹產生 casper.log，focused gate 失敗", "api.orchestrator import-time handler 在 MAGI_AGENT_DIR 未隔離時落 root。", "測試明確綁 tmp agent dir；fixture 覆寫 hostile inherited env；source root 不產 log。", "tracked/index clean、forbidden untracked=0。", "api/orchestrator.py；tests/test_admin_runtime_blueprint.py"),
    ("F-023", "post-cutover JPG 驗證無法執行", "wrapper 只綁 SHA，原始輸入 bytes 已不存在；視覺相似或重編碼不能替代。", "恢復 byte-exact 原檔後先驗 SHA，再僅送 localhost；缺 bytes 時維持 fail closed。", "input SHA 精確、regular non-symlink、no persistence/no external。", "post_cutover evidence；cookie endpoint"),
    ("F-024", "GitHub 發布混入案件／手機格式資料", "即使私有 repo，也不應保存可逆個資或 runtime dataset。", "公私版都跑 strict audit；個資資料集排除；只保存不可逆 release hash 與文件。", "public/private 0 errors、0 warnings；branch SHA 遠端核對。", "scripts/public_release_audit.py；PUBLIC_RELEASE.json；PRIVATE_RELEASE.json"),
    ("F-025", "Menubar 顯示紅燈但底層功能其實正常", "presentation 把 stale receipt、waiting、degraded、failed 混為同一文字。", "健康 state 與 next_action 分開；unknown reason 固定文案；attention 先於 ready。", "同一 payload 在 CLI/JSON/Menubar 語意一致。", "magi_v3/health_presentation.py；gui/magi_menubar.py"),
    ("F-026", "Golem 顯示孤兒 1／背景工作 2，但 Menubar 顯示殭屍 0", "兩端各自掃 ps 且定義不同；Golem 用 command substring＋PPID=1，把含 worker 路徑的 zsh -lc launcher 誤算成 worker，Menubar 只看持續 Z state。", "新增單一 magi_v3.process_monitor：只認 actual Python worker，沿 ancestry 判斷有無 canonical MAGI owner，孤兒／殭屍／重複採同一 schema；兩端只作呈現。", "rc600 zsh＋Python 父子形狀只算 worker1/orphan1；managed ancestor 為 orphan0；五秒殭屍、duplicate、read failure 與兩端 summary equality 均有回歸。", "magi_v3/process_monitor.py；api/blueprints/web_runtime.py；gui/magi_menubar.py；tests/test_process_monitor_unification.py"),
    ("F-027", "判決趨勢 MCP 候選未納入，但畫面沒有說明理由", "舊流程在結構或篩選條件不符時直接 continue；本機缺本文又會把外部候選一律擋掉，使用者無法分辨是法官、法院、案由、日期、簽署區、主文、附表或來源驗證哪一關失敗。", "每個 MCP 候選都保留公開安全的 exclusion_codes／exclusion_reasons；法官不符會列 requested 與簽署區實際法官。MCP 只有在官方 JID、judgment.judicial.gov.tw 網址、網址內 JID 與完整全文全部交叉驗證後，才可補足本機本文並參與統計。有效 JID 一律連穩定裁判頁；日期可用民國年／月／日選單並由後端正規化重驗。", "指定 PCDM,114,侵訴,59,20260812,1 與隨機法官名冊＋案由 LIVE 抽查：納入者須有官方全文 proof；未納入者須逐筆列出原因；錯 JID／網址、短本文、未簽署法官、缺主文或日期不符均 fail closed。", "api/domains/judgment_official_source.py；api/sentencing_trends.py；api/osc/taiwan_legal_mcp.py；templates/sentencing_trends.html；tests/test_sentencing_trends.py"),
    ("F-028", "HTML 維修手冊的表格在手機上像是把右側文字吃掉", "表格以 display:block 加每欄最小 120px 呈現，實際內容寬度最高 601px，但 390px 視窗只有 317px 可視寬度。", "改為 fixed-layout 整表；儲存格、連結與 code 允許 anywhere/break-word 安全換行，取消欄位最小寬度，窄螢幕再縮小字體與 padding。", "以實際瀏覽器分別量測 1440px 與 390px：24 張表格的 body overflow、table overflow、clipped cell 均為 0；PDF 268 頁目錄與代表性表格頁可讀。", "scripts/docs/build_magi_encyclopedia.py；tests/test_web_information_architecture.py"),
    ("F-029", "Drive owner 實際從 evidence/release-staging 執行，LIVE preflight 拒絕", "production deploy 的 plist 已綁 canonical installed root，但 cron snapshot renderer 仍使用呼叫端 staging root，導致排程啟動驗證暫存版本。", "production 模式的 cron snapshot 一律以 canonical binding_root 渲染；候選 release-staging 只供封裝與驗證，不得成為正式 process command。owner validator 維持 fail closed，禁止為了切版放寬。", "部署測試逐一解析 cron command，要求所有程式路徑位於 canonical installed release、不得包含 candidate/evidence 路徑；LIVE 前以 KERN_PROCARGS2／process argv 回讀 worker root。", "scripts/v3_deploy_prepare.py；tests/v3/test_deploy_prepare.py"),
    ("F-030", "PDF 命名 benchmark 把摘要內的「第4條」誤當當事人", "舊驗證器使用無嵌套括號 regex。檔名外層是「當事人；摘要」，摘要內又有「（第4條）」時，regex 只取到內層法條。", "改用 balanced outer-bracket parser，保留最外層內容與 group span；嵌套摘要不再取代當事人。", "真實 47 份 benchmark 的唯一失敗樣本重驗為有效；PDF-namer 相關 35 測試通過；99% 門檻未降低。", "skills/pdf-namer/naming_validator.py；tests/test_content_quality_hardening_rc390.py"),
    ("F-031", "實務見解每天蒐集量不足，MCP 候選又因本機無本文被排除", "舊流程把本機 mirror 當成唯一全文信任根，外部候選即使帶官方 JID、司法法院網址與全文，也只能當提示；每天排程只處理本機既有欠量，不能補官方全文來源。", "MCP 候選必須通過官方 JID／網址／全文交叉驗證，丟棄 provider 摘要，從全文重新產生 extractive source-bound summary，再沿既有實務可用性 gate 決定是否納入。既有每日裁判蒐集排程加入 bounded MCP gap fill，固定時間與每案由上限，輸出只留 aggregate。", "錯 JID／URL、短本文、provider summary 污染、非官方來源、PII query 全拒；合法官方全文即使本機沒有 mirror 仍可形成 verified_external_official_fulltext。", "api/domains/judgment_flow.py；api/legal_research_quality.py；skills/judgment-collector/action.py；tests/v3/test_legal_research_quality.py"),
    ("F-032", "夜間顯示 backlog=0，實際官方來源仍有大量未下載", "舊健康只算已落本機的 pending wrapper，且把 NVIDIA 96 輪 scheduler selection 誤當成每日 provider 額度，造成『本機沒有待做』與『來源已完整』兩種不同狀態被混為綠燈。失敗 wrapper 也曾被視為永久完成。", "夜間 pull 分別記 source_listed/completed/remaining；失敗 wrapper 可重試；若本機 backlog 為零但 source_remaining>0，健康顯示 SOURCE_PULL_CATCHING_UP。摘要回填分開呈現 scheduler selection capacity、provider daily limit/used/remaining，估算以真實每日額度。夜間 TLR-smart 只在固定窗口提高至 1200 筆、5 天、4800 秒，仍受權威時段與資源 gate 約束。", "來源尚欠量不得假綠；重跑失敗 wrapper 必須成功寫入後才增加完成數；provider 24/day 不得顯示 3072/day；外部 API 非服務時間保留 durable debt，不偽造成功。", "skills/judgment-collector/action.py；scripts/ops/check_judicial_api_pipeline.py；scripts/ops/judgment_summary_staged_backfill.py；api/domains/judicial_api_policy.py"),
    ("F-033", "指定判決取得官方全文後仍因『主文無法辨識』未納入", "司法院將『事 實』展平後可直接連全形匿名代碼，例如『事 實Ａ０７…』；舊主文邊界只允許少數漢字編號，把合法官方全文誤判為 main_section_unrecognized。同時本機 MCP 可選 runtime 實際未安裝，遠端搜尋又遭 WAF 阻擋。", "主文 parser 改以結構性段落標題分界，接受全形匿名代碼但不放寬句內的一般『事實／理由』文字。本機 MCP 部署上游 immutable commit，保留法院篩選，並以 Playwright Chromium 只取司法院 WAF cookie；cookie 0600，瀏覽器不常駐。", "PCDM,114,侵訴,59,20260812,1 實際全文驗證後，梁世樺為末位列名法官，主文刑度可統計且排除碼為空。官方 MCP 79/79；閱卷／筆錄／法扶安裝後 LIVE 20/20；Playwright cleanup 無殘留程序。", "api/sentencing_trends.py；api/osc/taiwan_legal_mcp.py；tests/test_sentencing_trends.py；lawchat-oss/mcp-taiwan-legal-db"),
    ("F-034", "NVIDIA 背景額度耗盡被誤報為排程失敗與健康紅燈", "新的 provider 授權層會回傳 background_heavy_authorization_budget_exhausted；舊週末彙整與 cron 結果政策只辨識 nim_daily_budget_exceeded／nim_background_budget_reserved，因此把可預期的每日額度耗盡重試到上限。", "將三種額度標記統一分類為 terminal schedule deferral；scheduler 啟動時以官方 reconcile 將既有同類失敗改列 deferred，不刪除證據、不假造成功，也不繞過每日額度。只有公開裁判全文可依使用者授權原文送 NVIDIA；案件、當事人檔案及閱卷／筆錄／法扶資料仍禁止外送。", "三種 marker 都產生 deferred、零 false failure；真正 provider error 仍失敗。額度恢復後由正式排程續跑，健康層顯示等待額度而非功能故障。", "scripts/weekend_resummary.py；skills/ops/cron_result_policy.py；tests/test_weekend_resummary_budget_semantics.py"),
    ("F-035", "影片範例單調，且顯示理解命令卻沒有真正套用", "第一版只用 synthetic 測試畫面；後續雖能產文字分鏡，但純文字路徑曾忽略已確認的倒序、轉場、運鏡與音訊設定。上游字幕範例又依賴本機 ffmpeg 未提供的 libass filter。", "改用 Pillow 產生繁中 overlay，允許 1～5 份 JPG／PNG／WebP／MP4／MOV 本機素材；中文指令先解析成 exact edit plan 並回顯，成片端點重算並比對 plan SHA。素材與純文字路徑都套用相同順序、淡化或直接切換、平滑或固定畫面、配樂或靜音。停用 updater、remote fetch、CapCut 與 publish；保留 CSRF、大小／速率／並行／deadline／RSS／process-group cleanup。", "真實圖片、圖片＋MP4、純文字四控制與公開 endpoint 全數重編碼為 1080×1920 H.264/AAC；重新 ffprobe、逐幕畫質抽樣、素材集合／storyboard／plan／output SHA、暫存清除與 child absence 均通。", "api/blueprints/video_studio.py；magi_v3/video_autopilot_adapter.py；templates/video_studio.html；tests/test_video_studio_blueprint.py"),
    ("F-036", "筆錄同步最後回傳 success=true、return code 0，排程仍顯示執行失敗", "同步器會把個別案件已捕捉且可重試的 traceback 寫入 stderr，舊 cron 結果政策只要看到 traceback 就覆蓋最後的結構化成功 receipt，將 partial_retry_pending 誤判為整體失敗。", "結果政策先驗證程序 return code 與最後一筆完整 JSON receipt；return code 0 且 receipt success=true 時，保留 partial／retry_pending 業務狀態但排程終態為 success。非零 return code、未捕捉 traceback、缺 receipt 或 success=false 仍 fail closed。", "production job_transcript_sync 於 active RC643/R75 完成：last_success=true、last_status=success、return code 0、error 空；真正失敗反例與 65 個 focused policy tests 仍通過。", "skills/ops/cron_result_policy.py；tests/test_cron_result_policy.py；cron_state.json receipt"),
    ("F-037", "active release 已是新版本，但 commercial readiness／release gate 仍讀 candidate 路徑或舊 release 證據", "production deploy renderer 已將 LaunchAgent 綁定 canonical installed release；舊 evidence compiler 卻仍要求 candidate root，造成同一個合法封裝同時被部署器接受、證據閘門拒絕。latest 相容 JSON 若未綁 active release，也會讓歷史失敗污染現行紅燈。", "Evidence compiler 在 production mode 驗 canonical installed root、release marker、manifest 與 candidate-equivalent immutable 0555 identity；release gate 重新計算 installed marker／manifest 並要求 deployment_mode=production。active-release pointer 才是 latest 真相，舊 JSON 僅為歷史投影。", "canonical tamper、錯 release、錯 manifest、candidate/production 混綁全部拒絕；RC643/R75 最終 gate 14/14 GO，missing／failed／invalid 全空，原子切換後 active marker 與三角色 executable 均指向同一 installed release。", "scripts/v3_evidence_compiler.py；scripts/v3_release_gate.py；tests/v3/test_evidence_compiler.py；release-gate-final-r3.json"),
    ("F-038", "網頁維修百科仍顯示舊 release 狀態，且過期驗證資料持續占用磁碟", "RC643 維修百科是在 R75 hotfix7 不可變封裝切換後才更新；線上 `/manual` 只能讀 active package，因此仍沿用該封裝內較早的維修百科 metadata。另有已被 R75 取代的舊 campaign／candidate 報告及逾期臨時驗證資料未依生命週期退役。", "建立 R75 docs-only 不可變維修封裝，將 `/manual` 四個固定 allowlist 資產更新為 rc643；不直接修改 installed release。清理器先解析 active marker 與 rollback 保護清單，只刪除明確版本／前綴、由本機帳號擁有且未被程序使用的 superseded 驗證樹，遇到 symlink、owner、scope 或 active identity 異常即 fail closed。", "清理 receipt 證明永久移除 153 個 superseded workspace 項目、14,235,019,434 bytes；r59 回滾版、active hotfix7、正式 campaign 證據與新文件封裝均保留。公開來源 focused regression 44 passed／1 skipped；PDF 168 頁已抽查封面與末頁。production `/manual` 與全站健康則必須在原子切換後另以 LIVE 證據確認。", "scripts/docs/build_magi_encyclopedia.py；api/blueprints/dashboard_pages.py；magi_v3/manual_assets/*rc643*；cleanup receipt；post-cutover manual probe"),
]


def add_heading(parts: list[str], level: int, title: str, anchor: str | None = None) -> None:
    if anchor:
        parts.append(f'<a id="{anchor}"></a>')
    parts.append(f"{'#' * level} {title}\n")


def add_code_reference(parts: list[str], repo: str, root: Path, rel: str, start: int, end: int, explanation: str) -> None:
    parts.append(f"**位置：** [{rel}:{start}]({source_url(repo, rel, start)})<br>\n")
    parts.append(explanation + "\n")
    parts.append(fenced(excerpt(root, rel, start, end), "python"))


def build_markdown(root: Path, repo: str, records: list[FileRecord], routes: list[dict[str, Any]], page_map: dict[str, int]) -> str:
    service = read_json(root, "config/v3_service_manifest.json")
    recovery = read_json(root, "config/business_recovery_contracts.json")
    model_registry = read_json(root, "config/model_registry.json")
    category_counts = Counter(record.category for record in records)
    extension_counts = Counter(record.extension for record in records)
    python_records = [record for record in records if record.extension == ".py"]
    production_python = [record for record in python_records if record.category != "tests"]
    test_python = [record for record in python_records if record.category == "tests"]
    symbol_count = sum(len(record.symbols) for record in python_records)

    parts: list[str] = []
    parts += [
        "# MAGI V3 維修百科全書\n",
        f"**基準版本：** `{RELEASE_ID}`<br>\n**來源 commit：** `{SOURCE_COMMIT}`<br>\n**文件狀態：** active production；release manifest `{RELEASE_MANIFEST_SHA}`，formal gate `{FORMAL_CHAIN_SHA}`<br>\n**文件日期：** {BUILD_DATE}<br>\n**GitHub：** `WhaleChao/{repo}` / immutable commit `{SOURCE_COMMIT}`\n",
        "> 本書的目標不是讓你背程式，而是讓你能從「現象」追到「入口 → owner → state → 外部邊界 → receipt → health」，並知道什麼可以安全修、什麼必須停手。全文不含密碼、Cookie、token、案件內容或可逆個資。\n",
        "## 文件使用方式\n",
        "1. 先查第 20 章的總決策樹，確認是功能故障、等待、降級、資料不一致或驗證器問題。\n2. 到對應功能章找連動表與權威狀態。\n3. 只讀蒐證後，再依第 21 章修復；不要先 kill、刪 lock、改 cron JSON 或清 checkpoint。\n4. 任何原始碼修改都建立新 commit、新 release、新證據鏈；installed release 不就地修改。\n5. 附錄的來源索引列出 SHA、行數與符號；完整內容以不可變 source commit 為準。\n",
        "## 目錄\n",
    ]
    parts.append(table(["章節", "頁"], [(f"[{title}](#{anchor})", page_map.get(anchor, "—")) for anchor, title in CHAPTERS]))
    parts.append("\n---\n")

    add_heading(parts, 1, CHAPTERS[0][1], "ch01")
    parts.append("MAGI 的維修核心是『權威與證據』，不是畫面看起來像成功。讀取順序如下：\n")
    parts.append(table(["順位", "權威來源", "用途", "不可替代原因"], [
        (1, "active release marker", "目前真正生效的 release、root、transaction", "工作區 branch 或 cron 舊 SHA 不代表實際程序"),
        (2, "installed immutable manifest", "逐檔 hash/size、來源 commit", "防止就地修改與半套部署"),
        (3, "formal chain / cutover receipts", "品質、備份、static、install、prepare、rollback", "每一步都有 hash-bound 前提"),
        (4, "owner/lock metadata＋PID 實況", "誰正在執行、是否為 canonical worker", "lock 檔存在不等於 owner 活著；反之亦然"),
        (5, "checkpoint / terminal status", "進度、cache、cursor、風險計數", "舊 terminal 或 copied JSON 不能證明本輪成功"),
        (6, "business/function/Doctor/guardian/Funnel", "面向使用者的整體健康", "必須與同一 active transaction 綁定"),
    ]))
    parts.append("**安全停手線：** owner 活著且 executable、argv、release root 都屬 canonical release 時，禁止 kill、清鎖或改 state；先讓其自然 terminal。只有精確證明 foreign/stale owner，且已有 rollback/重啟契約時，才可終止程序。\n")

    add_heading(parts, 1, CHAPTERS[1][1], "ch02")
    parts.append(f"本版共索引 **{len(records):,}** 個檔案、**{len(python_records):,}** 個 Python 檔、**{symbol_count:,}** 個類別／函式／方法與 **{len(routes):,}** 條靜態 Flask route。\n")
    parts.append(fenced("""使用者 / cron / webhook
        │
        ▼
Gateway（5002/5003）── 身分、CSRF、節流、意圖與相容層
        │
        ▼
Control（8088）────── 工具契約、管理面、協調與健康入口
        │
        ▼
Supervisor ────────── 服務單例、cron、worker、重試與 quiesce
        │
        ├── 業務：法扶／閱卷／OSC／Drive／日曆／帳務
        ├── 文件：PDF／OCR／筆錄／翻譯／知識庫
        ├── 模型：oMLX／NIM／embedding／quality gate
        └── 維運：Doctor／Guardian／NERV／Menubar
        │
        ▼
read-back receipt → business/function health → 使用者通知"""))
    parts.append(table(["觸發", "入口", "協調", "執行", "完成證據"], LINKAGES))
    parts.append("**資料面與控制面分離：** API 回應不是完成證據；真正的外部寫入必須由專用 worker 執行，完成後回讀遠端或目標檔案，再產生去識別 receipt。\n")

    add_heading(parts, 1, CHAPTERS[2][1], "ch03")
    parts.append(table(["服務", "角色", "型態", "埠／命令", "責任"], [
        (item["id"], item["role"], item["kind"], item.get("port") or " ".join(item.get("argv", [])), "required" if item.get("required") else "optional")
        for item in service["services"]
    ]))
    parts.append("啟動與切換順序不是任意的：gateway 直接回應使用者，使用 Interactive QoS；control/supervisor 為 Background。切換時先由 audited engine 停 supervisor，等待 child tree/owner 釋放，再停 gateway/control、安裝新版本、啟動並驗證。回滾使用切換前封存 plist、static receipt 與 active marker。\n")
    parts.append("Host singleton（MariaDB、NAS mount、RPC、oMLX text/embedding、input/memory/oMLX watchdog）不隨每個 release 重複啟動；錯把 singleton 當 release child 會造成雙實例與資料損壞。\n")

    add_heading(parts, 1, CHAPTERS[3][1], "ch04")
    parts.append(table(["區域", "檔案數", "責任"], [
        (name, count, {
            "v3_kernel": "不可變核心、ledger、cron、supervisor、health、release ownership",
            "api": "HTTP、業務 domain、OSC、auth、routing、session、工具契約",
            "skills": "可執行能力與業務 worker",
            "operations": "部署、稽核、備份、維運與驗證",
            "tests": "單元、合約、故障注入、LIVE adapter synthetic",
            "configuration": "能力、模型、service、schedule、resource 與 schema",
            "gui": "Menubar 與人類可讀健康",
            "web_ui": "templates/static 前端",
            "legal_aid_legacy_adapter": "法扶 portal 相容與既有流程",
        }.get(name, "其他版本化內容")) for name, count in sorted(category_counts.items())
    ]))
    parts.append("讀碼順序：先找觸發入口，再找 domain/orchestrator，再找真正外部 I/O，最後找 receipt 與 health evaluator。不要只修畫面文案；若底層 receipt/schema 不完整，Menubar 綠燈反而是危險的假綠。\n")
    parts.append(f"完整 machine-readable 索引：[`docs/{INDEX_BASENAME}`]({source_url(repo, f'docs/{INDEX_BASENAME}')})。\n")

    add_heading(parts, 1, CHAPTERS[4][1], "ch05")
    parts.append("HTTP 請求依序經過 server/app factory、auth/CSRF/request guards、route/domain、tool registry、專用 worker。會改變外部狀態的動作必須具備明確授權、idempotency key、bounded timeout、read-back proof。\n")
    parts.append(table(["層", "主要原始碼", "故障時先看"], [
        ("App / WSGI", "api/app_factory.py、api/wsgi_server.py、magi_v3/gateway.py", "port/listener、factory import、release ownership"),
        ("Auth / CSRF", "api/server_auth.py、api/authz.py、api/csrf_guard.py", "401/403、session、origin、CSRF token"),
        ("Routing", "api/routing/*、api/pipelines/*", "intent、clarification、tool choice、no-guess gate"),
        ("Tools", "api/tools/*、api/tools_api.py", "contract、timeout、async job、output envelope"),
        ("Durability", "api/durable_notifications.py、api/durable_rate_limit.py", "outbox、retry、dedup、delivery receipt"),
    ]))
    parts.append("**排查 500/502：** 先 localhost 健康 → listener PID → installed root → gateway log 安全尾端 → factory import。不要先重啟所有服務；若只是工具 worker 失敗，整體 gateway 重啟會中斷無關請求。\n")

    add_heading(parts, 1, CHAPTERS[5][1], "ch06")
    parts.append("CronService 不是單純 crontab：它有 lane、共享容量、same-job coalescing、pending occurrence、retry、business recovery、owner lock 與結果語意。`command_sha` 是排程定義身分；真正執行版本仍須看 owner PID 的 executable/argv/release root。\n")
    parts.append(table(["物件", "語意", "維修重點"], [
        ("v3_pending_occurrence", "尚未完成或被 controlled shutdown 保留的 occurrence", "不可手動刪；新 supervisor 會用新 definition 重建"),
        ("v3_retry", "結構化失敗後 bounded retry", "reason/label/attempt/timestamp/occurrence 必須 exact"),
        ("owner metadata", "目前執行者與 lock 的公開身分", "PID 活性＋argv＋release root 一起驗"),
        ("checkpoint", "seq、last_progress、hash cache、partial staging", "只讀安全欄；禁止輸出 case/path/token"),
        ("terminal", "chunk_completed 或 cycle_completed", "fresh、cursor 正確、risk counters 全零"),
    ]))
    parts.append("自然終局不是『整輪 221 案全部完成』才算：all-files 採公平單案 chunk；`before→after=before+1` 即可成為 fresh terminal，最後一案才 `total-1→0`。但必須同時 `case_complete=true`、checkpoint seq/hash cache>0、pending/partial/storage/collision/errors 全零。\n")
    parts.append("**rc641 大型上傳修復：** NAS→Drive 的單檔與單輪上傳安全上限提高為 3 GB，仍使用 8 MiB resumable chunks、30 GB 磁碟門檻與 no-overwrite/no-delete 契約。執行器會先判斷單檔上限，再套用本輪 byte budget；因此超限第一檔會留下 `deferred_large_file` 的精確原因，不會再形成 `0 attempted + stopped_by_bytes` 的無效重試。排查時比較 `large_upload_deferred`、`stopped_by_bytes`、attempted/bytes 與 checkpoint cursor；不要手動清除 pending/retry。\n")

    add_heading(parts, 1, CHAPTERS[6][1], "ch07")
    parts.append("法扶流程分為 Gmail 事件分類、案件生命週期、portal 可下載清單、附件下載／歸檔、開辦、進度、報結。『審查結果／已轉入』只證明業務狀態，不證明附件已上架。\n")
    parts.append(table(["訊號", "允許動作", "禁止誤判"], [
        ("正式派案通知", "解析案號/當事人/類型，進入建案與附件流程", "接案意願或補充資料不可當派案"),
        ("審查結果／已轉入", "更新狀態；等待內文明示或 portal listing", "不得直接標 needs_download"),
        ("portal table 有該案", "下載、驗證檔案、歸檔並回讀", "row parse 失敗不可當空清單"),
        ("portal empty", "健康空清單，沒有待下載", "與 timeout/relogin_failed 分開"),
        ("達 retry 上限", "轉人工確認並停止盲重試", "不得只清 queue 讓紅燈消失"),
    ]))
    parts.append("排查附件：先看 Gmail classification receipt → portal login/session → listing diagnostic → row count/parsed count → download receipt → NAS archive → business health。不要代使用者申請案件，也不要把『已轉入』信件當附件通知。\n")

    add_heading(parts, 1, CHAPTERS[7][1], "ch08")
    parts.append("閱卷採雙側簽章對帳：入口 expected signatures 與本輪 processed＋verified-existing handled signatures 必須同一 canonical schema。只看數量 7/7 不夠，因為可能是不同 7 件。\n")
    parts.append(table(["欄位", "規則"], [
        ("portal_downloadable_count", "type 必須是非 bool int 且 >=0"),
        ("expected/processed/verified/handled lists", "lowercase 64-hex、排序、唯一；raw 必須等於 normalize 後結果"),
        ("declared handled", "精確等於 processed ∪ verified-existing"),
        ("accounted", "只在雙側 contract 有效時算 expected ∩ handled"),
        ("success", "expected ⊆ handled、非 deferred、底層 success=true"),
    ]))
    parts.append("繳費憑證去重需綁『案件＋閱卷事件＋檔案 SHA』的私密 registry；同案不同閱卷時間不可互相跳過。若上傳佇列卡住，先查 portal lock owner 是否仍活著；合法 owner 就等待，foreign/stale 才依程序處理。\n")

    add_heading(parts, 1, CHAPTERS[8][1], "ch09")
    parts.append("Drive 同步的安全原則：case identity 先解析、規劃與執行分離、任何本機內容比較都走 fingerprint-bound checkpointed hash、寫入前再驗來源、絕不以路徑相似取代內容證據。\n")
    parts.append(table(["階段", "主要程式", "證據／停止條件"], [
        ("列舉", "scripts/drive_case_sync_worker.py", "worker_kind=all_files、exact command"),
        ("案件解析", "api/osc/drive_case_sync.py", "alias/exclusion/identity guard"),
        ("規劃", "build_file_sync_plan", "zero-write on pending/collision/storage"),
        ("內容比較", "_checkpointed_local_md5＋DriveFileCheckpoint", "fingerprint/cache/deadline"),
        ("執行", "download/upload no-overwrite", "before/after stat、partial sidecar"),
        ("終局", "worker status＋outcome gate", "fresh cursor、cache>0、risk=0"),
    ]))
    parts.append("**映射錯誤處理：** 先區分同一來源 ID、相同 checksum/size、可 hash 的 NAS alias、不可 hash 的 Drive native file。只有證據足夠才能自動合併；distinct native IDs 無內容 proof 時維持 collision，不能猜。\n")

    add_heading(parts, 1, CHAPTERS[9][1], "ch10")
    parts.append("OSC 是案件管理與業務資料的整合面，並非單一檔案。案件、檔案、日曆、待辦、帳務、債務文件各有 domain 與 blueprint；跨域動作要先確認 canonical case identity。\n")
    parts.append(table(["功能", "入口／domain", "外部邊界", "完成證據"], [
        ("案件", "api/blueprints/osc_cases.py、api/osc/case_intelligence.py", "MariaDB/NAS", "read-back case/card"),
        ("檔案", "osc_files.py、document_reuse.py", "NAS/Drive", "hash＋case identity"),
        ("日曆待辦", "osc_gcal.py、calendar_event_time.py、calendar_sources.py", "Google Calendar", "event id＋semantic audit"),
        ("帳務", "osc_accounting.py、accounting_sheet_import.py", "sheet/DB", "import summary＋monthly bonus"),
        ("債務文件", "osc_debt.py、debt_document_generator.py", "DOCX/PDF templates", "required checklist＋download proof"),
    ]))
    parts.append("日曆故障優先檢查 token health、source mapping、timezone/期限解析、duplicate semantic key。不要直接刪 Google event；先由 audit 證明重複並使用專用 reconciliation。\n")

    add_heading(parts, 1, CHAPTERS[10][1], "ch11")
    parts.append("文件鏈通常是：取得來源 → identity/lock → OCR/文字層 → 命名/分類 → 書籤/版面 → 寫入新檔 → reopen/read-back → receipt → index。任何一步失敗都不得覆蓋原檔。\n")
    parts.append(table(["能力", "原始碼", "常見故障"], [
        ("PDF 命名", "skills/pdf-namer/*", "OCR 空、case mapping、state path、watcher 重複"),
        ("PDF 書籤", "skills/pdf-bookmarker/*", "邊界、label、large volume"),
        ("OCR", "skills/engine/ocr/*、nas_pdf_ocr_worker.py", "backend unavailable、queue lock、低品質"),
        ("筆錄", "transcript-downloader/indexer、forensic verifier", "partial retry、portal empty、filename identity"),
        ("翻譯", "skills/translator/*、heavy_translation_quality_live.py", "模型 provenance、術語、長文降級"),
        ("知識庫", "skills/memory/*、obsidian/*、judicial cache", "重複、stale index、來源缺證"),
    ]))
    parts.append("排查順序固定為 source bytes → parser/OCR → canonical identity → target lock → write temp → verify output → atomic replace。若只有畫面預覽成功而無 reopen/receipt，不能視為完成。\n")

    add_heading(parts, 1, CHAPTERS[11][1], "ch12")
    parts.append("Cookie Cutter 僅接受 bounded image upload，於隔離子程序內建立 STL/ZIP。端點有尺寸、速率、timeout、RSS、child reap、ZIP parent 與幾何 attestation。LIVE 驗證只能用 synthetic 或使用者明確授權且 SHA 精確的本機圖片；不得持久化、不得外送。\n")
    parts.append(table(["步驟", "檢查"], [
        ("輸入", "格式、像素、multipart byte 上限、SHA"),
        ("輪廓", "去噪、平滑、封閉、最小特徵厚度"),
        ("幾何", "wall/base/height 尺寸、mesh manifold、non-empty"),
        ("封裝", "ZIP 唯一預期 member、parent/resource attestation"),
        ("資源", "20 秒、384 MiB、2 slots；setrlimit 失敗即拒"),
        ("隱私", "no persistence、no external、固定安全錯誤"),
    ]))
    parts.append("粗糙成品先判斷是輸入鋸齒、輪廓 simplify 過強、平滑不足、列印切片參數或 STL 非 manifold。應用多個自製圖做一致性測試，而非只對單一圖片調參。\n")
    parts.append("影片工作室位於同一個免登入的公開創作分類，但公開只表示不要求 MAGI 帳號，不代表解除安全邊界。它可接受 1～5 份本機圖片或短影片，也可只用文字分鏡。使用者的中文剪輯命令必須先被解析、回顯並確認；生成端會重新計算 edit-plan SHA，避免畫面說已理解、實際卻套用別的設定。不接受外部 URL，也不啟用上游更新器、CapCut 或發布平台。\n")
    parts.append(table(["影片階段", "契約"], [
        ("來源", "video-autopilot-kit v0.21.1、固定 commit、MIT；只封存 portrait normalizer"),
        ("素材", "JPG／PNG／WebP／MP4／MOV；1～5 份、單檔 24 MiB、multipart 合計 64 MiB；magic bytes 後再以 Pillow／ffprobe 解碼驗證"),
        ("命令", "先回顯順序、轉場、運鏡、音訊四欄；未知、衝突、plan SHA 漂移一律拒絕，素材與文字模式使用同一計畫"),
        ("字幕", "Pillow 產生繁中透明 PNG，避開本機 ffmpeg 缺少 libass 的能力差異"),
        ("資源", "JSON 16 KiB、multipart 64 MiB、2/min、單一並行、70 秒、每程序 1.5 GiB、CPU/NOFILE 限制"),
        ("程序", "spawn child＋獨立 process group；逾時 SIGTERM→SIGKILL→wait→absence proof"),
        ("輸出", "唯一 H.264 1080×1920 video＋AAC audio、6/9/12 秒、30 fps、逐幕畫質取樣與 bytes/SHA/ffprobe read-back"),
        ("隱私", "0600、O_EXCL/O_NOFOLLOW 暫存；工作目錄只活在單次 request，回傳或失敗後刪除，不外送、不自動發布"),
    ]))
    parts.append("若影片端點失敗，先讀固定 error code：輸入錯誤回 400/413；節流或忙碌回 429；engine、timeout、資源或 cleanup 失敗回 503。不可把公開端點的 engine 失敗誤當成外網／Gateway 全線故障。\n")

    add_heading(parts, 1, CHAPTERS[12][1], "ch13")
    parts.append(table(["模型", "角色", "啟用條件"], [
        (m["id"], m.get("tier", ""), "; ".join(f"{k}={v}" for k, v in m.get("gates", {}).items()) or "registry allowlist")
        for m in model_registry["models"]
    ]))
    parts.append("E4B 是穩定降級，不等於必然故障。26B/12B 需要 model live、磁碟、free+inactive、swap、resource level、overlay/tool gate 同時安全。強迫大型模型在 24GB unified memory 啟動可能讓整機 swap/OOM，反而使所有功能紅燈。\n")
    parts.append("排查：`model registry → active model probe → resource view → choose_model_for_request → decision_summary`。只有先清理經核准的 cache/evidence 垃圾並恢復資源後，才重新評估升級；不得刪 immutable release、最新 rollback 或唯一 evidence。\n")

    add_heading(parts, 1, CHAPTERS[13][1], "ch14")
    parts.append("通知與外網是最後一公里，功能本體成功不等於訊息已送達。durable outbox、provider response、delivery receipt、Funnel/route health 必須分開。\n")
    parts.append(table(["通道", "程式", "排查"], [
        ("Telegram", "api/webhooks/telegram.py、durable_notifications.py", "token health、topic/channel、outbox、provider error"),
        ("Discord", "api/discord_bot.py", "supervisor child、gateway intent、rate limit"),
        ("LINE compatibility", "api/line_compat.py", "legacy route、auth、delivery response"),
        ("Funnel/Tailscale", "tailscale_funnel_healthcheck.py", "public URL、local upstream、no-store、TLS"),
        ("Gmail/Drive/Calendar", "各專用 OAuth client", "token refresh、scope、canonical credential path"),
    ]))
    parts.append("外網斷線時不要把所有業務功能判成失敗；應呈現 upstream_unavailable/waiting，保留 durable work，恢復後 bounded retry。驗證時不可將 token、URL query secret 或 provider raw body寫入公開 receipt。\n")

    add_heading(parts, 1, CHAPTERS[14][1], "ch15")
    parts.append(table(["層", "回答問題", "原始碼"], [
        ("Business health", "業務結果是否真的完成？下一步是什麼？", "business_module_live_check.py、business_readiness_snapshot.py"),
        ("Function health", "route/skill/contract 是否可用？", "function_health_index.py"),
        ("Doctor", "程序、埠、依賴、磁碟、模型、launchd 是否正常？", "scripts/magi_doctor.py"),
        ("Guardian", "能否做安全的自動修復？", "magi_self_repair_guardian.py"),
        ("NERV/Menubar", "如何向人類呈現 ok/waiting/degraded/attention/failed？", "health_presentation.py、magi_menubar.py"),
        ("Process monitor", "核心、真實 worker、孤兒 ancestry、持續殭屍與重複群組是否一致？", "magi_v3/process_monitor.py → web_runtime.py／magi_menubar.py"),
        ("Funnel", "外部入口是否到達正確 release？", "tailscale_funnel_healthcheck.py"),
    ]))
    parts.append("紅燈不是『請重啟』；先讀 reason_code/next_action/evidence age。waiting 表示系統有安全續作路徑；attention 表示需人類資料或入口處理；failed 才是本輪終局失敗。不得為消紅燈而刪 state。\n")
    parts.append("### Golem／MENUBAR 共用的程序語意\n")
    parts.append(table(["欄位", "唯一正式定義", "常見誤解"], [
        ("core_count", "非 shell -c 包裝、命中 daemon REAPER_NEVER_KILL 的實際程序", "命令文字提到 server.py 不代表真核心"),
        ("worker_count", "argv0 是 Python/PyPy 且命中專用 worker script 的非 Z 程序", "zsh -lc 內文含 action.py 不算 worker"),
        ("orphan_count", "真 worker 的 ancestry 在遇到 canonical supervisor/cron/core 前先抵達 PID 1 或斷鏈", "只看 worker 直接 PPID 會漏掉 orphan shell 下的 child"),
        ("zombie_count", "MAGI 程序樹內同一 PID/PPID 的 Z state 持續至少五秒", "瞬間 exit→wait 不亮紅燈"),
        ("duplicate_groups", "真 worker 的完整 command 完全相同且同時超過一個", "launcher 與 child 命令不同，不是假重複"),
        ("anomaly_count", "孤兒 PID 與殭屍 PID 的聯集，再加 duplicate group 數", "這是異常項目，不是所有程序總數"),
    ]))
    parts.append("兩個 UI 都必須呼叫 `magi_v3.process_monitor.collect_process_monitor()`；任何一端自行重新解讀 `ps` 都視為回歸。讀取失敗必須顯示 attention，不能以零項假綠。\n")

    add_heading(parts, 1, CHAPTERS[15][1], "ch16")
    parts.append("MAGI 的狀態檔分三類：mutable runtime state、owner/lock metadata、immutable evidence。前兩者會變，最後一類只能新增。任何 validator 都要防 symlink、非 regular file、未知欄位、bool 冒充 int、舊 receipt、錯 transaction、copied JSON。\n")
    parts.append(table(["資料", "允許操作", "禁止"], [
        ("owner/lock", "只讀核 PID/exe/argv/root；owner 退場後由官方 cleanup", "手動 rm、看到 PID 就 kill"),
        ("checkpoint", "讀 safe counters；由 worker atomic write", "改 seq/cursor/cache 造成功"),
        ("cron state", "官方 scheduler/marker API", "直接編 JSON、清 retry/pending"),
        ("receipt", "exclusive/atomic、hash-bound、append-only", "覆寫舊成功、混用舊 release"),
        ("active marker", "ActivationTransaction", "手動改 symlink/JSON"),
    ]))

    add_heading(parts, 1, CHAPTERS[16][1], "ch17")
    parts.append("正式發布鏈：clean source → changed-module source gate → sealed bundle/privacy → host-outer full quality（同一 immutable inputs 僅一次）→ backup/actual restore/independent restore → static stage/restore → install inactive → render/audit → private prepare/formal-chain → wrapper review → cutover → core post。business/function/Doctor/guardian/Funnel/Drive/portal/MCP/benchmark 改為獨立背景 receipt，不得串成單一全域同步 gate。\n")
    parts.append("切換必須在 rollback envelope 內：驗 old release 與 durable work eligibility；停 supervisor 並 quiesce；保存 old bytes；安裝/啟動新；active marker atomic commit；post/health 失敗就 cleanup candidate、restore old、start old。不得再次執行已成功的 live_upgrade。\n")
    parts.append(table(["工件", "保證"], [
        ("release-manifest / COMPLETE", "sealed source 的逐檔身份"),
        ("formal-chain", "32 個品質/備份/static/install/prepare/rollback artifacts"),
        ("deploy manifest", "角色、plist、installed root、external inputs"),
        ("active marker", "唯一 active release＋transaction"),
        ("post receipt", "Web/Funnel/STL/legacy absence"),
        ("health receipt", "business/function/Doctor/guardian/Funnel"),
        ("Drive outcome", "fresh terminal/cursor/hashcache/zero risk"),
    ]))

    add_heading(parts, 1, CHAPTERS[17][1], "ch18")
    parts.append("備份不是只有 tar 成功：要做 actual restore drill 與 independent restore，並確認 DB、static receipt、active marker、plist、mutable state 的 byte/schema。回滾材料必須在切換前封存，不能在失敗後臨時從 candidate 猜。\n")
    parts.append("GitHub 是版本保存與協作，不是 LIVE runtime 備份。公私庫都不存 token、Cookie、案件內容或 runtime DB；私庫原 MAGI-v2 已原地更名 MAGI-v3，歷史保留。發布分支保存 privacy-filtered source、手冊與不可逆雜湊。\n")
    parts.append("災難復原順序：停止寫入 → 封存現況 evidence → 驗備份 SHA → isolated restore → schema/integrity check → 啟動單一 release → health → 恢復排程；不得直接把舊 DB 複製到正在寫入的 runtime。\n")

    add_heading(parts, 1, CHAPTERS[18][1], "ch19")
    parts.append("測試分層：unit → contract → adversarial/fail-closed → offline integration → isolated LIVE → maintenance cutover → post-cutover。執行邊界再分成「同步核心 gate」、「變更模組 gate」與「獨立背景健康」。驗證慢的主因是同一 nodes 在 focused/formal 重跑，或把外部網路、portal lock、Drive 長任務與 daily benchmark 錯串成整版 gate。\n")
    parts.append(table(["變更風險", "最低驗證"], [
        ("純文件", "render/links/privacy/audit"),
        ("純 presentation", "formatter tests＋payload adversarial＋business snapshot"),
        ("業務 parser/receipt", "focused full file＋schema negatives＋PII scan"),
        ("外部寫入", "no-write synthetic＋idempotency＋read-back＋rollback"),
        ("scheduler/Drive", "owner/occurrence/retry/checkpoint/cursor/terminal adversarial"),
        ("deploy/cutover", "fresh full chain＋independent review＋LIVE post/health"),
    ]))
    parts.append("加速原則：測試選擇 manifest 化、nodeid 精確；promotion precheck 不得重跑 formal manifest 已覆蓋的 node。formal PASS 只有在 source commit、release/suite manifests、runtime、nodeids、test-source hashes、resource policy 與 runner SHA 全部 byte-identical 時才能沿用；任一變動即重驗。Seatbelt child rc71 改由 host-outer runner，不能跳過 sandbox。\n")

    add_heading(parts, 1, CHAPTERS[19][1], "ch20")
    parts.append(fenced("""看到紅燈／失敗
  ├─ active release/transaction 不符？ → 停止，先修 deployment binding
  ├─ owner 正在跑？
  │    ├─ canonical owner → 讀 checkpoint，等待自然 terminal
  │    └─ foreign/stale → 精確核 PID/PGID/argv，再走受控終止/rollback
  ├─ receipt stale/缺失？ → 查 owner/job terminal，不可複製舊 receipt
  ├─ reason=waiting/degraded？ → 查 next_action 與資源/入口，非立即故障
  ├─ external unavailable？ → 保留 durable state，修 token/network/portal
  ├─ risk/pending/collision>0？ → 查 identity/hash/storage，不可強行清零
  └─ source exception？ → 建新 commit＋focused/adversarial＋fresh release"""))
    parts.append("每次事件建立一張維修紀錄：時間、active release、觸發、使用者症狀、owner、safe counters、第一個可信錯誤、採取動作、read-back、receipt SHA。禁止記錄案件內容、路徑、token。\n")

    add_heading(parts, 1, CHAPTERS[20][1], "ch21")
    runbooks = [
        ("服務無法開啟", ["讀 active marker/transaction", "lsof 查 5002/5003/8088 owner", "launchctl print 三角色", "核 executable/working directory", "localhost health", "只重啟故障角色；若 binding 錯走 rollback"]),
        ("Drive 長時間 running", ["核四 owner metadata", "確認 canonical worker argv/root", "讀 seq/last_progress/hash cache/staging bytes", "有進度就等待", "無進度查 storage/token/deadline", "owner 退場後才跑 outcome gate"]),
        ("法扶附件紅燈", ["確認郵件類型不是單純已轉入", "查 portal session/listing status", "table/empty/timeout 分流", "核 retry 上限與 deadline", "下載後驗 archive receipt", "人工確認案修資料或登入，不清 queue"]),
        ("閱卷 7/0", ["讀 expected receipt", "讀 handled receipt", "比較 signature set hash/fingerprint", "找 result/result_text/row revision", "重跑 canonical probe", "雙側 exact 才標綠"]),
        ("Cookie STL 粗糙或斷壁", ["核原圖解析度/鋸齒", "跑 seeded synthetic 外框與內圖案", "驗 float32 serialized mesh", "驗單一連通殼與頂層2條封閉環", "確認輪廓誤差<=0.15 mm及最小特徵", "實際 slicer 預覽", "不以單張特例降低安全 gate"]),
        ("判決趨勢候選未納入／官方連結不可用", ["確認第一階段 query 以案由發現候選", "逐筆讀 exclusion_codes／exclusion_reasons，不可用靜默 continue", "核官方 JID、FJUD URL 與 URL 內 JID 是否一致", "核全文長度、主文、簽署區、附表與判決日期", "以簽署區比對 requested/actual 法官並明列差異", "用民國年月日選單重現日期篩選；通過者才納入統計"]),
        ("實務見解夜間數量不足", ["先區分 source_remaining 與 local backlog", "若 source remaining>0 且本機 pending=0，應為 SOURCE_PULL_CATCHING_UP 而非全綠", "核既有 daily crawl 的 bounded MCP gap fill 是否啟用", "逐筆只接受官方 JID／URL／全文交叉驗證", "丟棄 provider summary，從官方全文重建 source-bound summary", "核 provider daily remaining 與預估完成天數，不把 scheduler selection 當 provider 額度"]),
        ("E4B 降級", ["查 active models", "查 disk/free+inactive/swap/resource level", "確認 12B/26B gates", "只清可刪 cache/evidence", "重新 model probe", "仍不足就保留降級，勿硬啟動"]),
        ("TG/外網故障", ["localhost 功能先驗", "token health", "outbox pending", "provider DNS/TLS/response", "Funnel upstream/no-store", "恢復後 official retry，不直接重送未知副作用"]),
        ("磁碟不足", ["先列分類與最後使用時間", "保留 active/rollback/latest evidence", "刪已核准 A/B/C cache、舊 worktree、render temp", "重算 free space", "跑 model/resource/health", "留下清理 receipt"]),
        ("Golem／MENUBAR 程序數不一致", ["確認兩端 active release/transaction 相同", "讀 shared summary 的 orphan/zombie/duplicate/anomaly", "以 ps 核 PID、PPID、PGID、stat、actual worker argv", "沿 parent chain 找 canonical supervisor/cron/core", "shell -lc launcher不可當 worker，Z 必須持續五秒", "修 shared classifier 並跑兩端 summary equality，禁止只改 UI 文案"]),
        ("HTML 手冊表格文字被遮住", ["用實際瀏覽器在 1440px 與 390px 重現", "量測 document/table/cell 的 clientWidth 與 scrollWidth", "確認 table-layout、min-width、overflow-wrap、word-break", "修 generator 而非直改 generated HTML", "重建 HTML/PDF/manual_assets", "要求所有表格 overflow=0 且 clipped cell=0"]),
    ]
    for title, steps in runbooks:
        parts.append(f"### {title}\n")
        parts.extend(f"{idx}. {step}\n" for idx, step in enumerate(steps, 1))

    add_heading(parts, 1, CHAPTERS[21][1], "ch22")
    parts.append("以下登錄表是維修時最重要的防回歸知識。『修復』不代表可以刪掉 guard；相反地，對應 negative tests 必須永久保留。\n")
    for fault_id, symptom, cause, fix, verify, source in FAULTS:
        parts.append(f"### {fault_id}｜{symptom}\n")
        parts.append(f"- **根因：** {cause}\n- **修復：** {fix}\n- **驗證：** {verify}\n- **原始碼／證據：** `{source}`\n")

    add_heading(parts, 1, CHAPTERS[22][1], "ch23")
    parts.append("安全維修流程：fork/branch → 重現 → 最小根修 → focused → adversarial → review → fresh release → backup/restore → prepare → authorized cutover → post/health/outcome → Git 保存。installed source 絕不熱修。\n")
    parts.append(table(["可以自主做", "必須停手／需要額外授權"], [
        ("只讀診斷、local tests、source-only 修補、文件、建立未執行 wrapper", "LIVE cutover、外部上傳/刪除、通知他人、清鎖、kill、改 cron/state"),
        ("安全 cache/worktree 清理（已核准範圍）", "刪 active release、rollback、唯一 backup、案件資料"),
        ("健康重算與 PII-free report", "用舊 receipt 假綠或手改 health JSON"),
    ]))
    parts.append("自主演進只能提出可審查 proposal 或在既定 allowlist 內修復，例如清除自己建立且過期的 tmp；不能自行改法律業務判斷、合併案件、上傳文件、刪除遠端檔案或放寬驗證器。\n")

    add_heading(parts, 1, CHAPTERS[23][1], "appA")
    parts.append(f"以下節錄是維修最常需要閱讀的核心邏輯；行號以 source commit `{SOURCE_COMMIT}` 為準。完整檔案請沿不可變連結開啟。\n")
    refs = [
        ("magi_v3/gateway.py", 205, 273, "release ownership 驗證：把 gateway 綁到 exact installed release/manifest。"),
        ("magi_v3/cron_service.py", 80, 126, "occurrence 與 timeout 的 deterministic 身分。"),
        ("magi_v3/drive_file_checkpoint.py", 72, 147, "case/item token、fingerprint、proof 與原子 JSON。"),
        ("magi_v3/file_review_receipts.py", 16, 145, "閱卷 canonical signature 與 snapshot receipt。"),
        ("api/model_router.py", 273, 360, "模型 gate 與 request routing 的安全判斷。"),
        ("api/blueprints/cookie_cutter.py", 45, 140, "cookie 子程序資源錯誤、bounded upload 與 parent cleanup。"),
        ("api/sentencing_trends.py", 220, 360, "穩定官方裁判頁、簽署區法官核對與量刑解析。"),
        ("api/domains/judgment_official_source.py", 1, 190, "MCP 判決的官方 JID、司法法院網址、完整全文與日期正規化信任邊界。"),
        ("api/domains/judgment_flow.py", 305, 470, "實務見解的本機 mirror 與 verified external official fulltext 雙路來源綁定。"),
        ("skills/judgment-collector/action.py", 650, 850, "夜間官方來源欠量、失敗 wrapper 重試與 bounded MCP gap fill。"),
        ("scripts/v3_release_bundle.py", 1158, 1251, "不可變 release bundle 建立與封存。"),
        ("scripts/v3_cutover/activation.py", 187, 275, "active marker 與 activation transaction。"),
        ("gui/magi_menubar.py", 925, 1058, "business/health payload 到人類狀態的轉換。"),
        ("magi_v3/process_monitor.py", 118, 197, "Golem 與 Menubar 共用的實際 worker、owner ancestry 與持續殭屍分類契約。"),
    ]
    for rel, start, end, explanation in refs:
        parts.append(f"### {rel}\n")
        add_code_reference(parts, repo, root, rel, start, end, explanation)

    add_heading(parts, 1, CHAPTERS[24][1], "appB")
    parts.append(f"Production Python 共 **{len(production_python):,}** 檔。每列顯示 path、行數、SHA 前 12 碼與前幾個符號；所有符號與完整 SHA 在 JSON 索引。\n")
    grouped: dict[str, list[FileRecord]] = defaultdict(list)
    for record in production_python:
        grouped[record.path.split("/", 1)[0]].append(record)
    for group in sorted(grouped):
        parts.append(f"### {group}/（{len(grouped[group])} 檔）\n")
        for record in grouped[group]:
            symbols = ", ".join(s.qualname for s in record.symbols[:10]) or "—"
            parts.append(f"- [`{record.path}`]({source_url(repo, record.path)})｜{record.lines} 行｜`{record.sha256[:12]}`｜{symbols}\n")

    add_heading(parts, 1, CHAPTERS[25][1], "appC")
    parts.append(f"測試 Python 共 **{len(test_python):,}** 檔；測試本身是安全契約的一部分，特別是 fail-closed negative cases，不可因『現在會過』就刪除。\n")
    for record in test_python:
        symbols = ", ".join(s.qualname for s in record.symbols[:8]) or "—"
        parts.append(f"- [`{record.path}`]({source_url(repo, record.path)})｜{record.lines} 行｜`{record.sha256[:12]}`｜{symbols}\n")

    add_heading(parts, 1, CHAPTERS[26][1], "appD")
    other = [record for record in records if record.extension != ".py" and record.category not in {"documentation"}]
    parts.append(table(["副檔名", "檔案數"], sorted(extension_counts.items(), key=lambda item: (-item[1], item[0]))))
    for record in other:
        parts.append(f"- [`{record.path}`]({source_url(repo, record.path)})｜{record.bytes:,} bytes｜`{record.sha256[:12]}`\n")

    add_heading(parts, 1, CHAPTERS[27][1], "appE")
    parts.append(f"靜態掃描取得 **{len(routes):,}** 條 decorator routes；動態 add_url_rule 或 runtime-generated route 請另以 `function_health_index.discover_api_routes()` 的 LIVE report 為準。\n")
    parts.append(table(["Methods", "Route", "Handler", "Source"], [
        (",".join(item["methods"]), item["route"], item["handler"], f"[{item['path']}:{item['line']}]({source_url(repo, item['path'], item['line'])})")
        for item in routes
    ]))

    add_heading(parts, 1, CHAPTERS[28][1], "appF")
    parts.append("### 只讀維修命令\n")
    parts.append(fenced("""# Git / source
git status --short
git log -1 --format='%H %s'
shasum -a 256 PATH

# Process / ports (read-only)
ps -p PID -o pid=,ppid=,pgid=,etime=,command=
lsof -nP -iTCP:5002 -sTCP:LISTEN
launchctl print gui/$UID/com.magi.v3.gateway

# Local health
curl -fsS http://127.0.0.1:5002/health
python3 scripts/magi_doctor.py --json
python3 scripts/ops/business_module_live_check.py --json
python3 scripts/ops/function_health_index.py --json

# Source/privacy tests
python3 scripts/public_release_audit.py --strict --json
python3 -m pytest -q TEST_PATH""", "bash"))
    parts.append("### 禁止直接執行的捷徑\n- `rm lock/state/checkpoint`：可能造成雙 owner 或遺失 durable work。\n- 直接編輯 cron JSON：破壞 occurrence/command/retry 證據。\n- 對 canonical owner `kill -9`：跳過 terminal/reap/rollback。\n- 在 installed release 直接改 `.py`：破壞 immutable manifest。\n- 複製舊 success receipt：造成假綠。\n")
    parts.append("### 名詞表\n")
    parts.append(table(["名詞", "定義"], [
        ("active marker", "唯一生效 release/transaction 的原子紀錄"),
        ("canonical", "由正式 runtime root、manifest、schema 與 realpath 共同認定"),
        ("checkpoint", "可續跑的最小進度與內容 hash cache"),
        ("cursor", "公平輪轉 all-files 案件的位置"),
        ("fail closed", "證據不足就拒絕，不猜成功"),
        ("formal chain", "正式發布各 gate 的 hash-bound 工件集合"),
        ("owner", "持有某 domain lock 且身分可由 PID/exe/argv/root 證明的程序"),
        ("receipt", "動作輸入、輸出、read-back 與 identity 的不可逆證據"),
        ("staging", "尚未 atomic commit 的暫存資料；必須可清理且不得被當完成"),
        ("terminal", "worker 已合法完成 chunk/cycle 或結構化失敗並釋放 owner"),
    ]))
    parts.append(f"\n---\n**文件完整性：** 本 PDF/Markdown 是維修導航；完整原始碼仍以 Git branch 中的檔案與 `{INDEX_BASENAME}` SHA 為準。任何後續版本都應重建索引與本書，不可只改頁面文字。\n")
    return "\n".join(parts)


def write_index(path: Path, root: Path, records: list[FileRecord], routes: list[dict[str, Any]]) -> None:
    payload = {
        "schema": "magi.source-index.v1",
        "release_id": RELEASE_ID,
        "source_commit": SOURCE_COMMIT,
        "release_manifest_sha256": RELEASE_MANIFEST_SHA,
        "formal_chain_sha256": FORMAL_CHAIN_SHA,
        "release_status": RELEASE_STATUS,
        "generated_at": BUILD_DATE,
        "root_is_repository_relative": True,
        "summary": {
            "files": len(records),
            "python_files": sum(r.extension == ".py" for r in records),
            "symbols": sum(len(r.symbols) for r in records),
            "routes": len(routes),
            "categories": dict(sorted(Counter(r.category for r in records).items())),
        },
        "files": [
            {
                **{key: value for key, value in asdict(record).items() if key != "symbols"},
                "symbols": [asdict(symbol) for symbol in record.symbols],
            }
            for record in records
        ],
        "routes": routes,
    }
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def heading_pages(pdf_path: Path) -> dict[str, int]:
    from pypdf import PdfReader

    reader = PdfReader(str(pdf_path))
    page_lines: list[set[str]] = []
    for page_no, page in enumerate(reader.pages, 1):
        lines = {
            re.sub(r"\s+", " ", line).strip()
            for line in (page.extract_text() or "").splitlines()
            if line.strip()
        }
        page_lines.append(lines)

    # Prefer the renderer-owned marker exclusively. Falling back chapter by
    # chapter can accidentally bind a missing chapter to its title in the TOC.
    found: dict[str, int] = {}
    for page_no, lines in enumerate(page_lines, 1):
        for anchor, title in CHAPTERS:
            marker = f"MAGI_CHAPTER_MARKER_{anchor}"
            if anchor not in found and marker in lines:
                found[anchor] = page_no
    if found:
        return found

    # Backward-compatible fallback for PDFs produced before the marker was
    # introduced. This branch is all-or-nothing so TOC rows cannot fill gaps.
    for page_no, lines in enumerate(page_lines, 1):
        for anchor, title in CHAPTERS:
            needle = re.sub(r"\s+", " ", title).strip()
            if anchor not in found and needle in lines:
                found[anchor] = page_no
    return found


def add_pdf_outlines(source: Path, target: Path, page_map: dict[str, int]) -> None:
    from pypdf import PdfReader, PdfWriter

    reader = PdfReader(str(source))
    writer = PdfWriter()
    # Copy pages only, then add exactly one machine-derived chapter map.
    writer.append_pages_from_reader(reader)
    for anchor, title in CHAPTERS:
        page = page_map.get(anchor)
        if page:
            writer.add_outline_item(title, page - 1)
    writer.add_metadata(
        {
            "/Title": f"MAGI V3 維修百科全書 {MANUAL_VERSION}",
            "/Author": "MAGI Engineering",
            "/Subject": "MAGI V3 architecture, source map, troubleshooting and recovery",
            "/Keywords": f"MAGI,V3,{MANUAL_VERSION},maintenance,troubleshooting,source code",
        }
    )
    with target.open("wb") as fh:
        writer.write(fh)


def render_pdf(markdown: Path, output: Path, reference_doc: Path | None) -> dict[str, Any]:
    # LibreOffice has repeatedly aborted under nested macOS sandboxing before
    # conversion begins, while browser printing is inefficient for the huge
    # generated source index. Use a pure-Python ReportLab document instead.
    # Pandoc only parses local Markdown to an HTML fragment; no office process,
    # browser, remote page, or mutable profile is involved. ``reference_doc``
    # remains accepted for CLI compatibility but is intentionally unused.
    del reference_doc
    pandoc = shutil.which("pandoc") or "/opt/homebrew/bin/pandoc"
    if not Path(pandoc).is_file():
        raise RuntimeError("pandoc is unavailable")
    with tempfile.TemporaryDirectory(prefix="magi-encyclopedia-") as tmp_text:
        tmp = Path(tmp_text)
        raw_pdf = tmp / "encyclopedia.pdf"
        fragment = subprocess.run(
            [pandoc, "--from=gfm+raw_html", "--to=html5", str(markdown)],
            check=True,
            capture_output=True,
            text=True,
            timeout=45,
        ).stdout

        from bs4 import BeautifulSoup, NavigableString, Tag
        from reportlab.lib import colors
        from reportlab.lib.pagesizes import letter
        from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
        from reportlab.lib.units import inch
        from reportlab.pdfbase import pdfmetrics
        from reportlab.pdfbase.ttfonts import TTFont
        from reportlab.platypus import (
            HRFlowable,
            PageBreak,
            Paragraph,
            Preformatted,
            SimpleDocTemplate,
            Spacer,
            Table,
            TableStyle,
        )

        medium_font = Path("/System/Library/Fonts/STHeiti Medium.ttc")
        light_font = Path("/System/Library/Fonts/STHeiti Light.ttc")
        if not medium_font.is_file() or not light_font.is_file():
            raise RuntimeError("fixed Traditional Chinese PDF fonts are unavailable")
        if "MAGIHeitiMedium" not in pdfmetrics.getRegisteredFontNames():
            pdfmetrics.registerFont(TTFont("MAGIHeitiMedium", str(medium_font), subfontIndex=0))
        if "MAGIHeitiLight" not in pdfmetrics.getRegisteredFontNames():
            pdfmetrics.registerFont(TTFont("MAGIHeitiLight", str(light_font), subfontIndex=0))

        base = getSampleStyleSheet()
        body = ParagraphStyle(
            "MAGIBody", parent=base["BodyText"], fontName="MAGIHeitiLight",
            fontSize=8.7, leading=13.2, textColor=colors.HexColor("#17212b"),
            spaceAfter=6, splitLongWords=True, wordWrap="CJK",
        )
        styles = {
            "h1": ParagraphStyle("MAGIH1", parent=body, fontName="MAGIHeitiMedium", fontSize=18, leading=23, textColor=colors.HexColor("#0f526f"), spaceBefore=8, spaceAfter=12),
            "h2": ParagraphStyle("MAGIH2", parent=body, fontName="MAGIHeitiMedium", fontSize=14, leading=18, textColor=colors.HexColor("#176b86"), spaceBefore=10, spaceAfter=8),
            "h3": ParagraphStyle("MAGIH3", parent=body, fontName="MAGIHeitiMedium", fontSize=11.5, leading=15, textColor=colors.HexColor("#7d4b22"), spaceBefore=8, spaceAfter=6),
            "quote": ParagraphStyle("MAGIQuote", parent=body, leftIndent=14, rightIndent=8, borderColor=colors.HexColor("#5ca6b8"), borderWidth=1, borderPadding=7, backColor=colors.HexColor("#eef8fa")),
            "code": ParagraphStyle("MAGICode", parent=body, fontName="MAGIHeitiLight", fontSize=6.6, leading=8.3, leftIndent=7, rightIndent=7, borderColor=colors.HexColor("#ccd9df"), borderWidth=.5, borderPadding=6, backColor=colors.HexColor("#f5f8fa"), splitLongWords=True),
            "cell": ParagraphStyle("MAGICell", parent=body, fontSize=7.1, leading=10, spaceAfter=0),
            "cell_head": ParagraphStyle("MAGICellHead", parent=body, fontName="MAGIHeitiMedium", fontSize=7.2, leading=10, textColor=colors.HexColor("#0f526f"), spaceAfter=0),
            "marker": ParagraphStyle("MAGIMarker", parent=body, fontSize=.1, leading=.1, textColor=colors.white, spaceBefore=0, spaceAfter=0),
        }

        def inline_markup(node: Any) -> str:
            if isinstance(node, NavigableString):
                return html.escape(str(node), quote=False)
            if not isinstance(node, Tag):
                return ""
            inner = "".join(inline_markup(child) for child in node.children)
            if node.name in {"strong", "b"}:
                return f"<b>{inner}</b>"
            if node.name in {"em", "i"}:
                return f"<i>{inner}</i>"
            if node.name == "code":
                return f'<font name="MAGIHeitiMedium" color="#355b6a">{inner}</font>'
            if node.name == "br":
                return "<br/>"
            if node.name == "a":
                href = str(node.get("href") or "").strip()
                if href.startswith(("https://", "http://")):
                    return f'<link href="{html.escape(href, quote=True)}" color="#176b86">{inner}</link>'
            return inner

        def paragraph_for(node: Tag, style: str = "body") -> Paragraph:
            value = "".join(inline_markup(child) for child in node.children).strip()
            return Paragraph(value or "&#160;", styles.get(style, body))

        soup = BeautifulSoup(fragment, "html.parser")
        story: list[Any] = []
        chapter_titles = {title: anchor for anchor, title in CHAPTERS}
        first_h1 = True

        def emit(node: Any) -> None:
            nonlocal first_h1
            if isinstance(node, NavigableString):
                if str(node).strip():
                    story.append(Paragraph(html.escape(str(node).strip()), body))
                return
            if not isinstance(node, Tag):
                return
            if node.name in {"h1", "h2", "h3", "h4", "h5", "h6"}:
                text_value = re.sub(r"\s+", " ", node.get_text(" ", strip=True))
                if node.name == "h1":
                    if not first_h1:
                        story.append(PageBreak())
                    first_h1 = False
                    anchor = chapter_titles.get(text_value)
                    if anchor:
                        story.append(Paragraph(f"MAGI_CHAPTER_MARKER_{anchor}", styles["marker"]))
                style = "h1" if node.name == "h1" else "h2" if node.name == "h2" else "h3"
                story.append(paragraph_for(node, style))
            elif node.name == "p":
                if node.get_text(" ", strip=True):
                    story.append(paragraph_for(node))
            elif node.name == "blockquote":
                story.append(paragraph_for(node, "quote"))
            elif node.name == "pre":
                story.append(Preformatted(node.get_text(), styles["code"], maxLineLength=96))
                story.append(Spacer(1, 5))
            elif node.name in {"ul", "ol"}:
                ordered = node.name == "ol"
                for index, item in enumerate(node.find_all("li", recursive=False), 1):
                    prefix = f"{index}." if ordered else "•"
                    list_style = ParagraphStyle(
                        f"MAGIList{len(story)}", parent=body, leftIndent=16,
                        firstLineIndent=-11, bulletIndent=4,
                    )
                    value = "".join(inline_markup(child) for child in item.children).strip()
                    story.append(Paragraph(value or "&#160;", list_style, bulletText=prefix))
            elif node.name == "table":
                rows: list[list[Any]] = []
                for tr in node.find_all("tr"):
                    cells = tr.find_all(["th", "td"], recursive=False)
                    if cells:
                        rows.append([
                            Paragraph(
                                "".join(inline_markup(child) for child in cell.children) or "&#160;",
                                styles["cell_head" if cell.name == "th" else "cell"],
                            )
                            for cell in cells
                        ])
                if rows:
                    width = 7.5 * inch
                    columns = max(len(row) for row in rows)
                    for row in rows:
                        row.extend(Paragraph("&#160;", styles["cell"]) for _ in range(columns - len(row)))
                    table = Table(rows, colWidths=[width / columns] * columns, repeatRows=1, hAlign="LEFT")
                    table.setStyle(TableStyle([
                        ("GRID", (0, 0), (-1, -1), .35, colors.HexColor("#9fb4bd")),
                        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#e9f2f5")),
                        ("VALIGN", (0, 0), (-1, -1), "TOP"),
                        ("LEFTPADDING", (0, 0), (-1, -1), 4),
                        ("RIGHTPADDING", (0, 0), (-1, -1), 4),
                        ("TOPPADDING", (0, 0), (-1, -1), 4),
                        ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
                    ]))
                    story.extend([table, Spacer(1, 7)])
            elif node.name == "hr":
                story.append(HRFlowable(width="100%", thickness=.5, color=colors.HexColor("#9fb4bd"), spaceBefore=7, spaceAfter=7))
            else:
                # Pandoc's HTML5 writer groups headings and content in nested
                # section/div containers. Walk those containers recursively;
                # table/list/pre descendants remain owned by their handlers.
                for child in node.children:
                    emit(child)

        for node in soup.children:
            emit(node)

        class ManualDocument(SimpleDocTemplate):
            def afterInit(self) -> None:
                self.title = f"MAGI V3 維修百科全書 {MANUAL_VERSION}"

        def draw_page(canvas: Any, document: Any) -> None:
            canvas.saveState()
            canvas.setTitle(f"MAGI V3 維修百科全書 {MANUAL_VERSION}")
            canvas.setAuthor("MAGI Engineering")
            canvas.setFont("MAGIHeitiLight", 7)
            canvas.setFillColor(colors.HexColor("#607782"))
            canvas.drawCentredString(letter[0] / 2, .28 * inch, f"MAGI V3 {MANUAL_VERSION}  ·  {document.page}")
            canvas.restoreState()

        document = ManualDocument(
            str(raw_pdf), pagesize=letter,
            leftMargin=.5 * inch, rightMargin=.5 * inch,
            topMargin=.48 * inch, bottomMargin=.52 * inch,
            title=f"MAGI V3 維修百科全書 {MANUAL_VERSION}",
            author="MAGI Engineering",
        )
        document.build(story, onFirstPage=draw_page, onLaterPages=draw_page)
        if not raw_pdf.is_file() or raw_pdf.is_symlink():
            raise RuntimeError("ReportLab did not produce the maintenance PDF")
        pages = heading_pages(raw_pdf)
        add_pdf_outlines(raw_pdf, output, pages)
        return {
            "heading_pages": pages,
            "pandoc_fragment_bytes": len(fragment.encode("utf-8")),
            "renderer": "reportlab",
        }


def render_html(markdown: Path, output: Path) -> None:
    """Build a self-contained MAGI-themed HTML manual for web and file:// use."""
    pandoc = shutil.which("pandoc")
    if not pandoc:
        raise RuntimeError("pandoc is required to render HTML")
    fragment = subprocess.run(
        [pandoc, "--from=gfm+raw_html", "--to=html5", str(markdown)],
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    template = r'''<!doctype html>
<html lang="zh-Hant" data-magi-theme="cyber">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <meta name="color-scheme" content="dark light">
  <title>MAGI V3 維修百科全書</title>
  <script>
    (() => {
      let value = "cyber";
      try { value = localStorage.getItem("magi.ui.theme.v1") || "cyber"; } catch (_) {}
      document.documentElement.dataset.magiTheme = ["forest", "light"].includes(value) ? "forest" : "cyber";
    })();
  </script>
  <style>
    :root {
      --radius: 14px; --ease: 160ms ease; --ok: #22c55e; --warn: #f59e0b;
      --bad: #ef4444; --header-h: 66px; --sidebar-w: 292px;
      font-family: "PingFang TC", "Noto Sans TC", Inter, system-ui, sans-serif;
    }
    :root[data-magi-theme="cyber"] {
      color-scheme: dark; --bg: #02060d; --bg-alt: #06111f;
      --surface: rgba(5, 20, 34, .94); --surface-strong: #081d2b;
      --surface-raised: #0b2636; --text: #edfeff; --muted: #9bcbd0;
      --line: rgba(0,238,255,.38); --accent: #00efff; --accent-2: #ff3bd4;
      --accent-soft: rgba(0,239,255,.13); --code: #01040a;
      --shadow: 0 22px 60px rgba(0,0,0,.56), 0 0 34px rgba(0,239,255,.08);
      --grid: radial-gradient(circle at 14% 16%, rgba(255,59,212,.12), transparent 28%),
        radial-gradient(circle at 84% 72%, rgba(0,239,255,.11), transparent 34%),
        linear-gradient(rgba(0,239,255,.035) 1px, transparent 1px),
        linear-gradient(90deg, rgba(0,239,255,.035) 1px, transparent 1px);
    }
    :root[data-magi-theme="forest"] {
      color-scheme: light; --bg: #f3f5ed; --bg-alt: #e9eee2;
      --surface: rgba(255,255,250,.96); --surface-strong: #fffefa;
      --surface-raised: #edf2e9; --text: #1f3028; --muted: #66766d;
      --line: rgba(58,91,71,.24); --accent: #236844; --accent-2: #a6652a;
      --accent-soft: rgba(35,104,68,.10); --code: #f7f8f2;
      --shadow: 0 16px 40px rgba(45,66,51,.11);
      --grid: radial-gradient(circle at 8% 8%, rgba(86,126,91,.10), transparent 34%);
    }
    * { box-sizing: border-box; }
    html { scroll-behavior: smooth; background: var(--bg); }
    body { margin: 0; min-height: 100vh; color: var(--text); background: var(--bg); background-image: var(--grid); background-size: 28px 28px; }
    a { color: var(--accent); text-underline-offset: 3px; }
    button, input { font: inherit; }
    .skip-link { position: fixed; left: 16px; top: 8px; z-index: 100; transform: translateY(-160%); padding: 8px 12px; border-radius: 8px; background: var(--accent); color: var(--bg); }
    .skip-link:focus { transform: translateY(0); }
    .topbar { position: fixed; z-index: 30; inset: 0 0 auto 0; min-height: var(--header-h); display: flex; align-items: center; justify-content: space-between; gap: 16px; padding: 10px clamp(14px,3vw,34px); background: color-mix(in srgb,var(--surface) 92%,transparent); border-bottom: 1px solid var(--line); backdrop-filter: blur(18px); }
    .brand { display: flex; align-items: baseline; gap: 12px; min-width: 0; }
    .brand strong { color: var(--accent); letter-spacing: .12em; font-size: 1.03rem; }
    .brand span { color: var(--muted); font-size: .78rem; white-space: nowrap; }
    .topnav { display: flex; align-items: center; justify-content: flex-end; gap: 8px; flex-wrap: wrap; }
    .topnav a, .topnav button { min-height: 38px; display: inline-flex; align-items: center; gap: 7px; padding: 7px 11px; border: 1px solid var(--line); border-radius: 9px; background: var(--surface-strong); color: var(--text); text-decoration: none; cursor: pointer; }
    .topnav a:hover, .topnav button:hover { background: var(--accent-soft); border-color: var(--accent); }
    .layout { display: grid; grid-template-columns: var(--sidebar-w) minmax(0,1fr); min-height: 100vh; padding-top: var(--header-h); }
    .sidebar { position: fixed; top: var(--header-h); bottom: 0; width: var(--sidebar-w); overflow: auto; padding: 18px 14px 28px; background: color-mix(in srgb,var(--surface) 96%,transparent); border-right: 1px solid var(--line); }
    .search-label { display: grid; gap: 7px; color: var(--muted); font-size: .78rem; }
    .search-label input { width: 100%; min-height: 40px; padding: 9px 11px; color: var(--text); background: var(--surface-strong); border: 1px solid var(--line); border-radius: 9px; outline: none; }
    .search-label input:focus { border-color: var(--accent); box-shadow: 0 0 0 3px var(--accent-soft); }
    .search-status { min-height: 20px; margin: 7px 0; color: var(--muted); font-size: .75rem; }
    .search-results { display: none; gap: 7px; margin: 0 0 14px; }
    .search-results.active { display: grid; }
    .search-result { display: grid; gap: 3px; padding: 8px; border: 1px solid var(--line); border-radius: 8px; text-decoration: none; background: var(--surface-strong); }
    .search-result strong { color: var(--accent); font-size: .78rem; }
    .search-result span { color: var(--muted); font-size: .72rem; line-height: 1.35; }
    .toc { display: grid; gap: 3px; margin-top: 10px; }
    .toc a { display: block; padding: 7px 9px; border-left: 2px solid transparent; border-radius: 6px; color: var(--muted); text-decoration: none; font-size: .78rem; line-height: 1.35; }
    .toc a:hover, .toc a.active { color: var(--text); background: var(--accent-soft); border-left-color: var(--accent); }
    .main { min-width: 0; grid-column: 2; padding: clamp(18px,3vw,42px); }
    .manual-shell { max-width: 1120px; margin: 0 auto; padding: clamp(22px,4vw,54px); background: var(--surface); border: 1px solid var(--line); border-radius: 20px; box-shadow: var(--shadow); }
    .manual-content { line-height: 1.78; overflow-wrap: anywhere; }
    .manual-content > h1:first-of-type { color: var(--accent); margin-top: 0; font-size: clamp(1.75rem,4vw,3rem); }
    h1, h2, h3, h4 { scroll-margin-top: calc(var(--header-h) + 18px); line-height: 1.3; }
    h1 { margin: 2.4em 0 .75em; padding-bottom: .35em; color: var(--accent); border-bottom: 1px solid var(--line); font-size: clamp(1.55rem,2.6vw,2.2rem); }
    h2 { margin: 2em 0 .65em; color: color-mix(in srgb,var(--accent) 78%,var(--text)); font-size: 1.45rem; }
    h3 { margin: 1.7em 0 .55em; color: var(--accent-2); font-size: 1.17rem; }
    p, li { max-width: 88ch; }
    blockquote { margin: 1.2em 0; padding: 12px 16px; border-left: 4px solid var(--accent); background: var(--accent-soft); border-radius: 0 10px 10px 0; }
    hr { margin: 2.5rem 0; border: 0; border-top: 1px solid var(--line); }
    code { padding: .12em .35em; color: color-mix(in srgb,var(--accent) 82%,var(--text)); background: var(--accent-soft); border-radius: 5px; font-family: ui-monospace,SFMono-Regular,Menlo,monospace; font-size: .88em; }
    pre { max-width: 100%; overflow: auto; padding: 16px; color: var(--text); background: var(--code); border: 1px solid var(--line); border-radius: 12px; line-height: 1.5; box-shadow: inset 0 0 24px color-mix(in srgb,var(--accent) 4%,transparent); }
    pre code { padding: 0; color: inherit; background: transparent; }
    table { display: table; width: 100%; max-width: 100%; table-layout: fixed; overflow: visible; border-collapse: collapse; margin: 1.2rem 0; font-size: .88rem; }
    th, td { min-width: 0; max-width: 100%; padding: 9px 11px; vertical-align: top; text-align: left; border: 1px solid var(--line); overflow-wrap: anywhere; word-break: break-word; white-space: normal; }
    th code, td code, th a, td a { overflow-wrap: anywhere; word-break: break-word; }
    th { position: sticky; top: var(--header-h); z-index: 2; color: var(--accent); background: var(--surface-strong); }
    tr:nth-child(even) td { background: color-mix(in srgb,var(--surface-raised) 55%,transparent); }
    mark { padding: 0 .08em; color: #111827; background: #ffe36e; border-radius: 3px; }
    .back-top { position: fixed; right: 18px; bottom: 18px; z-index: 20; width: 44px; height: 44px; border: 1px solid var(--line); border-radius: 50%; color: var(--text); background: var(--surface-strong); cursor: pointer; box-shadow: var(--shadow); }
    @media (max-width: 900px) {
      :root { --header-h: 112px; }
      .topbar { align-items: flex-start; flex-direction: column; }
      .topnav { width: 100%; flex-wrap: nowrap; overflow-x: auto; justify-content: flex-start; padding-bottom: 2px; }
      .layout { display: block; }
      .sidebar { position: static; width: auto; max-height: none; border-right: 0; border-bottom: 1px solid var(--line); }
      .toc { grid-template-columns: repeat(2,minmax(0,1fr)); }
      .main { padding: 12px; }
      .manual-shell { padding: 20px 16px; border-radius: 14px; }
    }
    @media (max-width: 520px) {
      .brand span { display: none; }
      .toc { grid-template-columns: 1fr; }
      .topnav a, .topnav button { min-height: 36px; padding: 6px 9px; font-size: .8rem; }
      table { font-size: .76rem; }
      th, td { padding: 6px 5px; }
    }
    @media print {
      @page { size: Letter; margin: .55in .55in .65in; }
      .topbar, .sidebar, .back-top { display: none !important; }
      .layout { display: block; padding: 0; }
      .main { padding: 0; }
      .manual-shell { max-width: none; padding: 0; border: 0; box-shadow: none; }
      .manual-content > h1 { break-before: page; }
      .manual-content > h1:first-of-type { break-before: auto; }
      tr, pre, blockquote { break-inside: avoid; }
    }
  </style>
</head>
<body>
  <a class="skip-link" href="#manual-content">跳至百科內容</a>
  <header class="topbar">
    <div class="brand"><strong>MAGI</strong><span>V3 維修百科全書 · __MANUAL_VERSION__</span></div>
    <nav class="topnav" aria-label="百科操作">
      <a href="/golem" data-local-href="#manual-content">MAGI 首頁</a>
      <a href="/manual/pdf" data-local-href="./__MANUAL_BASENAME__.pdf" target="_blank" rel="noopener">PDF</a>
      <a href="/manual/markdown" data-local-href="./__MANUAL_BASENAME__.md">Markdown</a>
      <a href="/manual/source-index.json" data-local-href="./__INDEX_BASENAME__" target="_blank" rel="noopener">原始碼索引</a>
      <button type="button" id="themeToggleBtn" aria-label="切換日夜模式">☾ 夜</button>
    </nav>
  </header>
  <div class="layout">
    <aside class="sidebar" aria-label="百科導覽">
      <label class="search-label" for="manual-search">搜尋百科<input id="manual-search" type="search" placeholder="功能、錯誤、檔名、函式…" autocomplete="off"></label>
      <div class="search-status" id="search-status">輸入兩個字開始搜尋</div>
      <div class="search-results" id="search-results"></div>
      <nav class="toc" id="manual-toc" aria-label="章節目錄"></nav>
    </aside>
    <main class="main">
      <article class="manual-shell manual-content" id="manual-content">__MANUAL_BODY__</article>
    </main>
  </div>
  <button type="button" class="back-top" id="back-top" aria-label="回到頁首">↑</button>
  <script>
    (() => {
      const root = document.documentElement;
      const themeButton = document.getElementById("themeToggleBtn");
      const normalize = value => ["forest", "light"].includes(value) ? "forest" : "cyber";
      const applyTheme = (value, persist = true) => {
        const theme = normalize(value);
        root.dataset.magiTheme = theme;
        document.body.classList.toggle("theme-dark", theme === "cyber");
        themeButton.textContent = theme === "cyber" ? "☾ 夜" : "☀ 日";
        themeButton.title = theme === "cyber" ? "切換為日" : "切換為夜";
        themeButton.setAttribute("aria-label", themeButton.title);
        if (persist) try { localStorage.setItem("magi.ui.theme.v1", theme); } catch (_) {}
      };
      applyTheme(root.dataset.magiTheme, false);
      themeButton.addEventListener("click", () => applyTheme(root.dataset.magiTheme === "cyber" ? "forest" : "cyber"));

      if (location.protocol === "file:") {
        document.querySelectorAll("[data-local-href]").forEach(link => link.setAttribute("href", link.dataset.localHref));
      }
      document.querySelectorAll("#manual-content a[href^='http']").forEach(link => {
        link.target = "_blank"; link.rel = "noopener noreferrer";
      });

      const headings = [...document.querySelectorAll("#manual-content h1")];
      const toc = document.getElementById("manual-toc");
      headings.forEach((heading, index) => {
        if (!heading.id) heading.id = `manual-section-${index + 1}`;
        const link = document.createElement("a");
        link.href = `#${heading.id}`;
        link.textContent = heading.textContent;
        toc.appendChild(link);
      });

      const searchable = [...document.querySelectorAll("#manual-content h1, #manual-content h2, #manual-content h3, #manual-content p, #manual-content li, #manual-content tr, #manual-content pre")]
        .map((node, index) => {
          if (!node.id) node.id = `manual-hit-${index + 1}`;
          return { node, text: (node.textContent || "").replace(/\s+/g, " ").trim() };
        }).filter(item => item.text);
      const input = document.getElementById("manual-search");
      const status = document.getElementById("search-status");
      const results = document.getElementById("search-results");
      const renderSearch = () => {
        const query = input.value.trim().toLocaleLowerCase("zh-Hant");
        results.replaceChildren();
        if (query.length < 2) {
          results.classList.remove("active"); status.textContent = "輸入兩個字開始搜尋"; return;
        }
        const matches = searchable.filter(item => item.text.toLocaleLowerCase("zh-Hant").includes(query)).slice(0, 60);
        status.textContent = matches.length ? `顯示前 ${matches.length} 筆結果` : "找不到相符內容";
        results.classList.toggle("active", matches.length > 0);
        matches.forEach(item => {
          const link = document.createElement("a"); link.className = "search-result"; link.href = `#${item.node.id}`;
          const title = document.createElement("strong"); title.textContent = item.node.tagName.toLowerCase();
          const snippet = document.createElement("span"); snippet.textContent = item.text.slice(0, 150);
          link.append(title, snippet); results.appendChild(link);
        });
      };
      input.addEventListener("input", renderSearch);
      input.addEventListener("keydown", event => {
        if (event.key === "Escape") { input.value = ""; renderSearch(); input.blur(); }
      });

      const tocLinks = [...toc.querySelectorAll("a")];
      const observer = new IntersectionObserver(entries => {
        entries.filter(entry => entry.isIntersecting).forEach(entry => {
          tocLinks.forEach(link => link.classList.toggle("active", link.hash === `#${entry.target.id}`));
        });
      }, { rootMargin: "-20% 0px -72% 0px" });
      headings.forEach(heading => observer.observe(heading));
      document.getElementById("back-top").addEventListener("click", () => scrollTo({ top: 0, behavior: "smooth" }));
    })();
  </script>
</body>
</html>
'''
    output.write_text(
        template.replace("__MANUAL_BODY__", fragment)
        .replace("__MANUAL_VERSION__", MANUAL_VERSION)
        .replace("__MANUAL_BASENAME__", MANUAL_BASENAME)
        .replace("__INDEX_BASENAME__", INDEX_BASENAME),
        encoding="utf-8",
    )


def main() -> int:
    global SOURCE_COMMIT
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--repo", default="MAGI-v3")
    parser.add_argument("--source-commit", default=SOURCE_COMMIT)
    parser.add_argument("--reference-doc", type=Path)
    parser.add_argument("--page-map", type=Path)
    parser.add_argument("--html", action="store_true")
    parser.add_argument("--render", action="store_true")
    args = parser.parse_args()
    if not re.fullmatch(r"[0-9a-f]{40}", args.source_commit):
        parser.error("--source-commit must be an exact lowercase 40-character Git SHA")
    SOURCE_COMMIT = args.source_commit
    root = args.root.resolve()
    docs = root / "docs"
    docs.mkdir(exist_ok=True)
    index_path = docs / INDEX_BASENAME
    html_path = docs / f"{MANUAL_BASENAME}.html"
    md_path = docs / f"{MANUAL_BASENAME}.md"
    pdf_path = docs / f"{MANUAL_BASENAME}.pdf"

    records = build_inventory(root)
    routes = route_map(root, records)
    write_index(index_path, root, records, routes)
    page_map: dict[str, int] = {}
    if args.page_map and args.page_map.is_file():
        page_map = {
            str(key): int(value)
            for key, value in json.loads(args.page_map.read_text(encoding="utf-8")).items()
        }
    md_path.write_text(build_markdown(root, args.repo, records, routes, page_map), encoding="utf-8")
    render_info: dict[str, Any] = {}
    if args.render:
        expected_headings = {anchor for anchor, _title in CHAPTERS}
        for _ in range(12):
            md_path.write_text(build_markdown(root, args.repo, records, routes, page_map), encoding="utf-8")
            render_info = render_pdf(md_path, pdf_path, args.reference_doc)
            fresh = render_info["heading_pages"]
            if set(fresh) != expected_headings:
                missing = sorted(expected_headings - set(fresh))
                raise RuntimeError(f"PDF heading page map incomplete: {missing}")
            if fresh == page_map:
                break
            page_map = fresh
        else:
            raise RuntimeError("PDF table-of-contents page map did not converge")
    if args.html or args.render:
        render_html(md_path, html_path)

    manual_assets = root / "magi_v3" / "manual_assets"
    manual_assets.mkdir(parents=True, exist_ok=True)
    for generated in (index_path, md_path, html_path, pdf_path):
        if generated.is_file():
            shutil.copy2(generated, manual_assets / generated.name)

    result = {
        "ok": True,
        "markdown": str(md_path),
        "index": str(index_path),
        "html": str(html_path) if html_path.exists() else "",
        "pdf": str(pdf_path) if pdf_path.exists() else "",
        "records": len(records),
        "python_files": sum(record.extension == ".py" for record in records),
        "symbols": sum(len(record.symbols) for record in records),
        "routes": len(routes),
        "markdown_sha256": sha256_file(md_path),
        "index_sha256": sha256_file(index_path),
        "html_sha256": sha256_file(html_path) if html_path.exists() else "",
        "pdf_sha256": sha256_file(pdf_path) if pdf_path.exists() else "",
        "render": render_info,
    }
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
