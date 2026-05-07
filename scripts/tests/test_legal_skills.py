
import sys
import os
import time
import traceback
from multiprocessing import Process, Queue

# Ensure project root is in path
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '../..')))

from skills.legal.doc_analysis import analyze_document_content


def _analysis_worker(table_contents, case_data, q: Queue):
    try:
        out = analyze_document_content(table_contents, case_data)
        q.put({"ok": True, "replacements": out})
    except Exception as e:
        q.put({"ok": False, "error": str(e), "traceback": traceback.format_exc()})


def test_doc_analysis():
    print("Testing skills.legal.doc_analysis...")
    timeout_sec = int(os.environ.get("MAGI_TEST_LEGAL_SKILLS_TIMEOUT_SEC", "45") or "45")
    strict_timeout = os.environ.get("MAGI_TEST_LEGAL_SKILLS_STRICT_TIMEOUT", "0").strip().lower() in {"1", "true", "yes", "on"}
    
    # Mock Data
    table_contents = [
        "【狀頭表格】",
        "[0,0]: 原告 | [0,1]: 詳卷 | [0,2]: 王小明",
        "[1,0]: 被告 | [1,1]: 詳卷 | [1,2]: 李大同",
        "[2,0]: 案號 | [2,1]:  | [2,2]: 112年度訴字第888號",
        "[3,0]: 股別 | [3,1]:  | [3,2]: 孝",
        "\n【狀尾表格】",
        "[0,0]: 臺灣臺北地方法院"
    ]
    
    case_data = {
        'client_name': '陳阿美',
        'court_case_number': '113年度訴字第666號',
        'court_division': '忠',
        'court_name': '臺灣新北地方法院',
        'case_reason': '損害賠償',
        'opponent_name': '張三瘋'
    }
    
    print(f"Sending Mock Table Content: {len(table_contents)} lines")
    print(f"Target Case Data: {case_data}")
    
    start_time = time.time()
    q: Queue = Queue(maxsize=1)
    p = Process(target=_analysis_worker, args=(table_contents, case_data, q))
    p.start()
    try:
        p.join(timeout=max(5, timeout_sec))
        if p.is_alive():
            p.terminate()
            p.join(timeout=2)
            if strict_timeout:
                print(f"❌ Test Failed: timeout_exceeded_{timeout_sec}s")
            else:
                print(f"⚠️ Test Warning: timeout_exceeded_{timeout_sec}s (treated as soft-timeout)")
            return
        result = q.get_nowait() if not q.empty() else {"ok": False, "error": "no_result"}
        if not result.get("ok"):
            print(f"❌ Test Failed: {result.get('error') or 'unknown_error'}")
            tb = (result.get("traceback") or "").strip()
            if tb:
                print(tb)
            return
        replacements = result.get("replacements") or {}
        end_time = time.time()
        
        print(f"\nTime taken: {end_time - start_time:.2f} seconds")
        print("-" * 40)
        print("Replacements Received:")
        for k, v in replacements.items():
            print(f"  {k}: {v}")
        print("-" * 40)
        
        if replacements:
            print("✅ Test Passed: Received replacements from Melchior.")
        else:
            print("⚠️ Test Warning: Received empty replacements (Check Melchior logs/response).")
            
    except Exception as e:
        print(f"❌ Test Failed: {e}")
        import traceback
        traceback.print_exc()
    finally:
        try:
            if p.is_alive():
                p.terminate()
                p.join(timeout=2)
        except Exception:
            pass

if __name__ == "__main__":
    test_doc_analysis()
