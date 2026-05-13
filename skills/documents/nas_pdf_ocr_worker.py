#!/usr/bin/env python3
# -*- coding: utf-8 -*-
import os
import sys
import logging
import sqlite3
import subprocess
import time
import argparse
import fitz  # PyMuPDF
from pathlib import Path
from logging.handlers import RotatingFileHandler

# Logger Setup
LOG_FILE = "/tmp/magi_nas_ocr.log"
logger = logging.getLogger("NasOCRWorker")
logger.setLevel(logging.INFO)
fh = RotatingFileHandler(LOG_FILE, maxBytes=10*1024*1024, backupCount=3)
formatter = logging.Formatter('%(asctime)s [%(levelname)s] %(message)s')
fh.setFormatter(formatter)
ch = logging.StreamHandler()
ch.setFormatter(formatter)
logger.addHandler(fh)
logger.addHandler(ch)

DB_PATH = os.path.expanduser("~/.magi_nas_ocr_queue.db")

_NAS_HOME_USER = (
    os.environ.get("MAGI_NAS_HOME_USER")
    or os.environ.get("MAGI_NAS_USER")
    or "home"
).strip().strip("/\\") or "home"
NAS_ROOT = os.environ.get("MAGI_NAS_CASE_ROOT", f"/Volumes/homes/{_NAS_HOME_USER}/01_案件")
ARCHIVE_SUBDIR = "_Archive_No_OCR"

# OCR Tool Path
OCRMYPDF_BIN = "/opt/homebrew/bin/ocrmypdf"

def init_db():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("""
        CREATE TABLE IF NOT EXISTS ocr_queue (
            file_path TEXT PRIMARY KEY,
            status TEXT DEFAULT 'pending',
            last_attempt TIMESTAMP,
            attempt_count INTEGER DEFAULT 0,
            error_msg TEXT
        )
    """)
    conn.commit()
    conn.close()

def ensure_nas_mount():
    try:
        from api.nas_mount_guard import ensure_nas_mounts
        res = ensure_nas_mounts()
        return any(res.values())
    except Exception as e:
        logger.warning(f"Failed to use nas_mount_guard: {e}. Checking manually.")
        return os.path.exists(NAS_ROOT)

def _is_digital_pdf(pdf_path: str, threshold: int = 150) -> bool:
    """判斷 PDF 是否為原生數位檔 (不需要做 OCR)"""
    try:
        doc = fitz.open(pdf_path)
        sample_pages = len(doc)  # Completely unrestricted page scan
        total_text_len = 0
        for i in range(sample_pages):
            page_text = doc[i].get_text()
            total_text_len += len(page_text.strip())
            
            # 單頁如果超過 threshold 字，通常就是原生數位 PDF
            if len(page_text.strip()) > threshold:
                return True
                
        # 加總平均
        if sample_pages > 0 and (total_text_len / sample_pages) > (threshold * 0.5):
            return True
            
        return False
    except Exception as e:
        logger.error(f"Error checking PDF type for {pdf_path}: {e}")
        return False

def scan_nas_for_pdfs(max_limit=1000, max_depth=5):
    """掃描 NAS 目錄，找出所有的未處理 PDF，放進 DB。
    NAS 友善：深度限制（預設 5）、每 50 目錄 sleep 0.05s 避免打掛 NAS。"""
    if not os.path.exists(NAS_ROOT):
        logger.error(f"NAS root {NAS_ROOT} not accessible.")
        return 0

    logger.info(f"Scanning {NAS_ROOT} for untreated PDFs (max_limit={max_limit}, max_depth={max_depth})...")
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()

    added = 0
    dir_count = 0
    # 用有限深度的 stack-based DFS 取代無限 os.walk
    stack = [(NAS_ROOT, 0)]
    while stack:
        cur_dir, depth = stack.pop()
        dir_count += 1
        if dir_count % 50 == 0:
            time.sleep(0.05)  # NAS I/O 節流
        if dir_count > 5000:
            logger.warning(f"NAS scan safety cap reached ({dir_count} dirs). Stopping.")
            break

        try:
            with os.scandir(cur_dir) as it:
                for entry in it:
                    if entry.is_dir(follow_symlinks=False):
                        if ARCHIVE_SUBDIR in entry.name:
                            continue
                        if depth < max_depth:
                            stack.append((entry.path, depth + 1))
                    elif entry.is_file(follow_symlinks=False):
                        fname = entry.name
                        if not fname.lower().endswith('.pdf'):
                            continue
                        if "_OCR.pdf" in fname:
                            continue
                        full_path = entry.path
                        ocr_counterpart = full_path[:-4] + "_OCR.pdf"
                        if os.path.exists(ocr_counterpart):
                            continue
                        try:
                            c.execute("INSERT INTO ocr_queue (file_path) VALUES (?)", (full_path,))
                            added += 1
                            if max_limit > 0 and added >= max_limit:
                                conn.commit()
                                conn.close()
                                logger.info(f"Scan limit reached. Added {added} items ({dir_count} dirs visited).")
                                return added
                        except sqlite3.IntegrityError:
                            pass
        except Exception as e:
            logger.debug(f"scandir error on {cur_dir}: {e}")
            continue

    conn.commit()
    conn.close()
    logger.info(f"Scan complete. Added {added} new items to queue ({dir_count} dirs visited).")
    return added

def run_worker(batch_size=20):
    if not ensure_nas_mount():
        logger.error("NAS is not mounted. Exiting worker.")
        return
        
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    # Get pending items
    c.execute("""
        SELECT file_path FROM ocr_queue 
        WHERE status IN ('pending', 'failed') AND attempt_count < 3
        ORDER BY attempt_count ASC 
        LIMIT ?
    """, (batch_size,))
    
    rows = c.fetchall()
    if not rows:
        logger.info("Queue is empty. Nothing to do.")
        conn.close()
        return
        
    logger.info(f"Processing batch of {len(rows)} files...")
    
    for row in rows:
        pdf_path = row[0]
        
        # Check if file still exists
        if not os.path.exists(pdf_path):
            c.execute("UPDATE ocr_queue SET status='missing' WHERE file_path=?", (pdf_path,))
            conn.commit()
            continue
            
        logger.info(f"Processing: {pdf_path}")
        c.execute("UPDATE ocr_queue SET status='processing', attempt_count=attempt_count+1, last_attempt=datetime('now') WHERE file_path=?", (pdf_path,))
        conn.commit()
        
        # Check if native digital
        if _is_digital_pdf(pdf_path):
            logger.info("   -> Skipped (Detected as native digital PDF)")
            c.execute("UPDATE ocr_queue SET status='skipped_digital' WHERE file_path=?", (pdf_path,))
            conn.commit()
            continue
            
        out_path = pdf_path[:-4] + "_OCR.pdf"
        
        try:
            # Execute ocrmypdf
            # Note: optimization level 1 is safe but helps with file size compression
            # jobs=2 to prevent NAS from choking
            cmd = [
                OCRMYPDF_BIN,
                "--force-ocr",
                "-l", "chi_tra+eng",
                "--optimize", "1",
                "--jobs", "2",
                "--oversample", "300",
                "--deskew",
                pdf_path,
                out_path
            ]
            
            logger.info("   -> Running OCR (this may take a while)...")
            start_time = time.time()
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=1200) # 20 mins timeout per file
            elapsed = time.time() - start_time
            
            if result.returncode == 0 and os.path.exists(out_path) and os.path.getsize(out_path) > 1000:
                logger.info(f"   -> Success in {elapsed:.1f}s. Archive old file.")
                
                # Move old file
                parent_dir = os.path.dirname(pdf_path)
                archive_dir = os.path.join(parent_dir, ARCHIVE_SUBDIR)
                os.makedirs(archive_dir, exist_ok=True)
                
                old_filename = os.path.basename(pdf_path)
                archive_path = os.path.join(archive_dir, old_filename)
                
                try:
                    os.rename(pdf_path, archive_path)
                except Exception as e:
                    logger.warning(f"   -> Failed to move old file: {e}")
                    
                c.execute("UPDATE ocr_queue SET status='completed' WHERE file_path=?", (pdf_path,))
            else:
                error_msg = f"Returncode: {result.returncode}, Stderr: {result.stderr[:200]}"
                logger.error(f"   -> Failed: {error_msg}")
                c.execute("UPDATE ocr_queue SET status='failed', error_msg=? WHERE file_path=?", (error_msg, pdf_path,))
                
        except subprocess.TimeoutExpired:
            logger.error("   -> Timeout (>20m)")
            c.execute("UPDATE ocr_queue SET status='failed', error_msg='Timeout > 20m' WHERE file_path=?", (pdf_path,))
        except Exception as e:
            logger.error(f"   -> Exception: {e}")
            c.execute("UPDATE ocr_queue SET status='failed', error_msg=? WHERE file_path=?", (str(e), pdf_path,))

        conn.commit()
    conn.close()

if __name__ == "__main__":
    init_db()
    parser = argparse.ArgumentParser()
    parser.add_argument('command', choices=['scan', 'work', 'status'])
    parser.add_argument('--batch', type=int, default=20)
    args = parser.parse_args()
    
    # 確保 NAS script 能被 import
    sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..')))
    
    if args.command == 'scan':
        scan_nas_for_pdfs(max_limit=1000)
    elif args.command == 'work':
        run_worker(batch_size=args.batch)
    elif args.command == 'status':
        conn = sqlite3.connect(DB_PATH)
        c = conn.cursor()
        c.execute("SELECT status, count(*) FROM ocr_queue GROUP BY status")
        print("\n--- OCR Queue Status ---")
        for row in c.fetchall():
            print(f"{row[0]:<20}: {row[1]}")
        print("------------------------\n")
        conn.close()
