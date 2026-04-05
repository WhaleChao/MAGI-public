
import os
import fcntl
import py_compile
import shutil
import re
import tempfile
import requests
import logging

TARGET_FILE = os.path.expanduser("~/.openclaw/skills/law-office/legal_crawler.py")
BACKUP_FILE = os.path.expanduser("~/.openclaw/skills/law-office/legal_crawler.py.bak")
_LOCK_FILE = TARGET_FILE + ".lock"

# LLM Config (oMLX)
try:
    from api.routing.service_registry import get_service_url as _get_svc_url
    OMLX_URL = _get_svc_url("omlx_inference") + "/v1/chat/completions"
except Exception:
    OMLX_URL = "http://127.0.0.1:8080/v1/chat/completions"
MODEL = os.environ.get("MAGI_MAIN_MODEL", "")

logger = logging.getLogger("CrawlerArchitect")


class CrawlerArchitect:
    def __init__(self):
        self.logger = logger

    def create_backup(self):
        if os.path.exists(TARGET_FILE):
            shutil.copy2(TARGET_FILE, BACKUP_FILE)
            return True
        return False

    def restore_backup(self):
        if os.path.exists(BACKUP_FILE):
            shutil.copy2(BACKUP_FILE, TARGET_FILE)
            return True
        return False

    def generate_crawler_code(self, requirement):
        prompt = f"""
You are an expert Python Crawler Developer.
Write a Python class to scrape data based on the user's requirement.

Context:
- File `legal_crawler.py` uses `requests`, `bs4`, `mysql.connector`.
- There is a global `DB_CONFIG` and `get_db_connection()` function available.
- All new data MUST be inserted into the `legal_news` table.
- Table Schema: `legal_news (title, source, url, snippet, published_date, crawled_at, keywords)`

Requirements:
1. Class Name: Must be explicitly named based on the source (e.g., `PTTCrawler`).
2. Method: Must have a `run(self)` method that:
    - Returns the number of added items (int).
    - Prints progress using `print()`.
    - Handles exceptions gracefully.
3. Logic: Fetch data, parse with BeautifulSoup, insert into DB using `INSERT IGNORE` or `ON DUPLICATE KEY UPDATE`.
4. Output: ONLY the Python class code. No introduction, no markdown fences.

User Requirement: "{requirement}"
        """
        try:
            response = requests.post(OMLX_URL, json={
                "model": MODEL,
                "prompt": prompt,
                "stream": False,
                "options": {"temperature": 0.2, "stop": ["<|eot_id|>"]}
            }, timeout=60)

            if response.status_code == 200:
                code = response.json().get('response', '')
                code = re.sub(r'```python', '', code)
                code = re.sub(r'```', '', code)
                return code.strip()
            logger.warning("LLM returned status %s", response.status_code)
            return None
        except Exception as e:
            logger.warning("generate_crawler_code failed: %s", e)
            return None

    @staticmethod
    def _validate_syntax(code: str) -> tuple[bool, str]:
        """Validate Python syntax via py_compile before injection."""
        fd, tmp_path = tempfile.mkstemp(suffix=".py", prefix="crawler_validate_")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(code)
            py_compile.compile(tmp_path, doraise=True)
            return True, ""
        except py_compile.PyCompileError as e:
            return False, str(e)
        finally:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass

    def inject_code(self, new_class_code):
        if not new_class_code:
            return False, "No code generated."

        # Validate generated code syntax before touching production file
        syntax_ok, syntax_err = self._validate_syntax(new_class_code)
        if not syntax_ok:
            logger.warning("Generated code failed syntax check: %s", syntax_err)
            return False, f"Syntax error in generated code: {syntax_err[:200]}"

        try:
            with open(TARGET_FILE, 'r') as f:
                original_content = f.read()

            match = re.search(r'class\s+(\w+):', new_class_code)
            if not match:
                return False, "Could not extract class name."
            class_name = match.group(1)

            # Insert Class Definition before `def run_all_crawlers`
            split_marker = "def run_all_crawlers():"
            if split_marker not in original_content:
                return False, "Could not find `run_all_crawlers` function."

            parts = original_content.split(split_marker)
            new_content = parts[0] + "\n" + new_class_code + "\n\n" + split_marker + parts[1]

            # Insert Execution Call inside `run_all_crawlers`
            # Look for the summary block
            summary_marker = "    # 總結"
            exec_injection = f"""
    # [Architect Added] {class_name}
    try:
        crawler = {class_name}()
        added = crawler.run()
        print(f"  {class_name}: 新增 {{added}}")
    except Exception as e:
        print(f"  ❌ {class_name} Error: {{e}}")
"""
            if summary_marker in new_content:
                new_content = new_content.replace(summary_marker, exec_injection + "\n" + summary_marker)
            else:
                return False, "Could not find injection point inside `run_all_crawlers`."

            # Validate full file syntax before writing
            full_ok, full_err = self._validate_syntax(new_content)
            if not full_ok:
                logger.warning("Injected file failed syntax check: %s", full_err)
                return False, f"Injected file syntax error: {full_err[:200]}"

            # Atomic write via temp file
            tmp_path = TARGET_FILE + ".tmp"
            with open(tmp_path, 'w', encoding='utf-8') as f:
                f.write(new_content)
            os.replace(tmp_path, TARGET_FILE)

            return True, class_name
        except Exception as e:
            logger.warning("inject_code failed: %s", e)
            return False, str(e)

    def execute_modification(self, requirement):
        # File lock to prevent concurrent modifications
        os.makedirs(os.path.dirname(_LOCK_FILE) or ".", exist_ok=True)
        lock_fd = open(_LOCK_FILE, "w")
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            lock_fd.close()
            return "❌ 另一個 CrawlerArchitect 正在執行，請稍後再試"

        try:
            if not self.create_backup():
                return "❌ Backup failed."

            code = self.generate_crawler_code(requirement)
            if not code:
                return "❌ Code generation failed."

            success, msg = self.inject_code(code)
            if not success:
                self.restore_backup()
                return f"❌ Injection failed: {msg}"

            return f"✅ 爬蟲修改成功！已新增功能: {requirement} (Class: {msg})"
        finally:
            fcntl.flock(lock_fd, fcntl.LOCK_UN)
            lock_fd.close()

if __name__ == "__main__":
    architect = CrawlerArchitect()
    # print(architect.execute_modification("Crawl PTT Gossiping"))
