
import sys
import os
import time

# Add root to path
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from skills.research.web_research import search_web, fetch_url_content

def test_web_research():
    print("🌐 WEB RESEARCH AUDIT")
    print("=" * 50)
    
    # 1. Test Search
    print("\n[1/2] Testing DuckDuckGo Search...")
    query = "OpenAI GPT-5 release date rumors 2026"
    print(f"   Query: {query}")
    start = time.time()
    results = search_web(query, num_results=3)
    duration = time.time() - start
    
    if results["success"] and results["results"]:
        print(f"✅ Search Success ({duration:.2f}s)")
        for i, r in enumerate(results["results"][:3]):
            print(f"   {i+1}. {r['title']} ({r['url']})")
    else:
        print(f"❌ Search Failed: {results.get('error')}")
        return

    # 2. Test Fetch
    print("\n[2/2] Testing Content Fetch...")
    test_url = results["results"][0]["url"]
    print(f"   Target: {test_url}")
    
    start = time.time()
    content = fetch_url_content(test_url)
    duration = time.time() - start
    
    if content["success"] and len(content["content"]) > 100:
        print(f"✅ Fetch Success ({duration:.2f}s)")
        print(f"   Title: {content['title']}")
        preview = content['content'][:200].replace('\n', ' ')
        print(f"   Preview: {preview}...")
    else:
        print(f"❌ Fetch Failed: {content.get('error')}")

if __name__ == "__main__":
    test_web_research()
