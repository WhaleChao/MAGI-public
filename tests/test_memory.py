
import sys
import os
import time

# Add root to path
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from skills.memory.mem_bridge import remember, recall

def test_memory():
    print("🧠 MEMORY SYSTEM AUDIT")
    print("=" * 50)
    
    unique_fact = f"Test Fact {int(time.time())}: The user prefers dark mode IDE themes."
    
    # 1. Test Remember
    print("\n[1/2] Testing 'remember'...")
    try:
        remember(unique_fact, source="audit_script")
        print("✅ Memory saved successfully")
    except Exception as e:
        print(f"❌ Remember Failed: {e}")
        return

    # Wait for indexing (if async)
    time.sleep(1)
    
    # 2. Test Recall
    print("\n[2/2] Testing 'recall'...")
    try:
        results = recall("What IDE theme does the user like?", top_k=3)
        found = False
        for r in results:
            print(f"   - Found: {r['content'][:50]}... (Score: {r.get('score', 'N/A')})")
            if "dark mode" in r['content']:
                found = True
        
        if found:
            print("✅ Recall Success: Retrieved relevant fact")
        else:
            print("❌ Recall Failed: Fact not found in top results")
            
    except Exception as e:
        print(f"❌ Recall Failed: {e}")

if __name__ == "__main__":
    test_memory()
