"""
Casper Code Review Skill
========================
A specialized skill for conducting distributed code reviews using Melchior and RAG.

Capabilities:
1.  **RAG-Enhanced Review**: Uses `skills.memory.codebase_rag` to retrieve relevant context.
2.  **Distributed Inference**: Offloads heavy analysis to Melchior (70B).
3.  **Report Generation**: Generates comprehensive markdown reports.

Usage:
    from skills.casper.code_review import review_skill
    review_skill.run_review(target_dir="~/Desktop/code")
"""

import os
import glob
import time
from datetime import datetime

from skills.magi.sunrise import execute_sunrise_protocol
from skills.bridge.inference_gateway import InferenceGateway
from skills.memory.codebase_rag import memory
from skills.ops.task_tracker import tracker

class CodeReviewSkill:
    def __init__(self):
        self.name = "Distributed Code Review"
        self.description = "Orchestrates distributed code review using Melchior and RAG."

    def _get_relevant_context(self, file_path, file_content):
        """
        Retrieves relevant context using RAG.
        """
        # Ingest file into memory
        memory.reset()
        if not memory.ingest_file(file_path):
            return file_content[:8000]  # Fallback to truncation if ingestion fails

        # Queries for code review
        queries = [
            "security vulnerabilities and hardcoded credentials",
            "performance bottlenecks and inefficient algorithms",
            "architectural patterns and code structure",
            "error handling and edge cases"
        ]

        context_chunks = []
        for query in queries:
            results = memory.query(query, n_results=2) # Get top 2 chunks per query
            if results['documents'][0]:
                for doc in results['documents'][0]:
                    if doc not in context_chunks:
                        context_chunks.append(doc)
        
        if not context_chunks:
            return file_content[:8000] # Fallback
            
        return "\n...\n".join(context_chunks)

    def review_file(self, file_path):
        """
        Reviews a single file using Melchior.
        """
        file_name = os.path.basename(file_path)
        print(f"[{file_name}] Reading...")
        
        try:
            with open(file_path, 'r', encoding='utf-8') as f:
                content = f.read()
        except Exception as e:
            return f"❌ Error reading {file_name}: {e}"

        # RAG or Truncate (Increased limit for 30B model context)
        if len(content) > 30000:
            print(f"  🧠 Using RAG for {file_name} ({len(content)} chars)...")
            review_content = self._get_relevant_context(file_path, content)
            print(f"  🧠 Retrieved {len(review_content)} relevant characters.")
        else:
            review_content = content

        if file_name in ["apple_ai.py", "web_research.py"]:
             print(f"⚠️ Skipping blacklisted file: {file_name}")
             return "⚠️ Skipped (Blacklisted due to known hangs)"

        prompt = f"""
You are Qwen3 (30B), a Senior AI Software Engineer at MAGI.
Your task is to REVIEW and IMPROVE the following Python code from `{file_name}`.

CODE CONTEXT:
```python
{review_content}
```

### Instructions:
1.  **Analyze**: Identify bugs, security risks, and performance issues.
2.  **Refactor**: Provide a *complete, improved version* of the code (or specific functions) if significant changes are needed.
3.  **Explain**: Briefly explain *why* you made changes.

Output Format:
- **Summary**: Bullet points of issues found.
- **Improved Code**: A code block with the fix.
- **Reasoning**: Design choices for the improvement.
"""
        try:
            gw = InferenceGateway()
            response = gw.chat(prompt, task_type="coding", timeout=300)
            if response.get('success') and response.get('response'):
                return response['response']
            else:
                return f"❌ Inference Error: {response.get('error', 'unknown')}"
        except Exception as e:
            return f"❌ Communication Error: {e}"

    def _compute_hash(self, file_path):
        """Computes SHA256 hash of a file."""
        import hashlib
        sha256_hash = hashlib.sha256()
        try:
            with open(file_path, "rb") as f:
                for byte_block in iter(lambda: f.read(4096), b""):
                    sha256_hash.update(byte_block)
            return sha256_hash.hexdigest()
        except Exception:
            return None

    def run_review(self, target_dir):
        """
        Executes the full review process on a directory with incremental scanning.
        """
        import json
        target_dir = os.path.expanduser(target_dir)
        report_file = os.path.join(target_dir, f"Code_Review_Report_{datetime.now().strftime('%Y%m%d_%H%M')}.md")
        cache_file = os.path.join(target_dir, "review_cache.json")
        
        # Load Cache
        cache = {}
        if os.path.exists(cache_file):
            try:
                with open(cache_file, "r") as f:
                    cache = json.load(f)
            except Exception:
                cache = {}

        print(f"🚀 Starting Code Review for {target_dir}")
        print(f"📝 Report will be saved to {report_file}")

        # 1. Sunrise Protocol
        print("🌅 Initiating Sunrise Protocol...")
        # 1. Sunrise Protocol
        print("🌅 Initiating Sunrise Protocol...")
        sunrise_report = execute_sunrise_protocol()
        print(sunrise_report)

        if "Failed to switch" in sunrise_report or "Melchior API Offline" in sunrise_report:
             print("❌ Sunrise Protocol Failed. Aborting Code Review.")
             return
        
        # 2. Find Files (Recursive)
        files = glob.glob(os.path.join(target_dir, "**", "*.py"), recursive=True)
        if not files:
            print("❌ No Python files found.")
            return

        # Task ID
        task_id = "code_review"
        tracker.update_task(task_id, "Qwen3 Code Review", 0, "Starting...", type="scan")

        print(f"📂 Found {len(files)} Python files.")

        with open(report_file, "w", encoding="utf-8") as report:
            report.write(f"# Distributed Code Review Report\n")
            report.write(f"**Date:** {datetime.now().strftime('%Y-%m-%d %H:%M')}\n")
            report.write(f"**Target:** `{target_dir}`\n\n")
            report.flush()

            for i, file_path in enumerate(files):
                file_name = os.path.basename(file_path)
                
                # Update Progress
                progress = int(((i) / len(files)) * 100)
                
                # Check Hash
                current_hash = self._compute_hash(file_path)
                if current_hash and current_hash == cache.get(file_path):
                    print(f"⏩ Skipping unchanged file: {file_name}", flush=True)
                    tracker.update_task(task_id, "Qwen3 Code Review", progress, f"Skipped (Unchanged): {file_name}", type="scan")
                    continue
                
                if os.path.getsize(file_path) < 10:
                    print(f"⏭️ Skipping empty/small file: {file_name}", flush=True)
                    tracker.update_task(task_id, "Qwen3 Code Review", progress, f"Skipped (Empty): {file_name}", type="scan")
                    continue

                tracker.update_task(task_id, "Qwen3 Code Review", progress, f"Analyzing {file_name}...", type="scan")
                print(f"[{i+1}/{len(files)}] Reviewing {file_name}...", flush=True)
                
                # Write Header BEFORE analysis (for debugging stalls)
                report.write(f"## 📄 {file_name}\n")
                report.write(f"**Status:** Analysis Started...\n\n")
                report.flush()
                
                start_time = time.time()
                review_result = self.review_file(file_path)
                duration = time.time() - start_time
                
                # Check for success (simple heuristic)
                if "❌" not in review_result:
                    cache[file_path] = current_hash
                    # Save cache incrementally
                    with open(cache_file, "w") as f:
                        json.dump(cache, f, indent=2)
                
                # Append Result
                report.write(f"**Analysis Time:** {duration:.2f}s\n\n")
                report.write(review_result + "\n\n")
                report.write("---\n\n")
                report.flush()
                
                print(f"✅ {file_name} Complete ({duration:.2f}s)")

        tracker.complete_task(task_id)
        print(f"\n✨ Review Complete! Report: {report_file}")

# Export Instance
review_skill = CodeReviewSkill()
