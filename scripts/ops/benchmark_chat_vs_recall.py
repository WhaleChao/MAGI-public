#!/usr/bin/env python3
"""Benchmark: SIMPLE chat vs legal recall latency comparison."""
import sys, os, time, json
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..'))

def main():
    results = {}

    # Test 1: SIMPLE chat (should skip Graph-RAG)
    from skills.bridge.grounded_ai import is_small_talk_intent
    t0 = time.time()
    for q in ["你好", "今天天氣如何", "綠茶好喝嗎", "早安", "謝謝你"]:
        is_small_talk_intent(q, "SIMPLE")
    results["small_talk_classify_ms"] = round((time.time() - t0) * 1000, 1)

    # Test 2: Graph-RAG context (should be fast due to caching)
    try:
        from skills.engine.knowledge_graph.graph_rag import graph_context
        t0 = time.time()
        ctx = graph_context("侵權行為", top_k=3)
        results["graph_rag_first_ms"] = round((time.time() - t0) * 1000, 1)

        t0 = time.time()
        ctx2 = graph_context("侵權行為", top_k=3)
        results["graph_rag_cached_ms"] = round((time.time() - t0) * 1000, 1)
        results["graph_rag_hit"] = len(ctx) > 0
    except Exception as e:
        results["graph_rag_error"] = str(e)

    # Test 3: recall latency
    try:
        from skills.memory.mem_bridge import recall
        t0 = time.time()
        r = recall("侵權行為", top_k=3)
        results["recall_legal_ms"] = round((time.time() - t0) * 1000, 1)
        results["recall_legal_count"] = len(r) if isinstance(r, list) else 0
    except Exception as e:
        results["recall_error"] = str(e)

    results["success"] = True
    print(json.dumps(results, ensure_ascii=False, indent=2))
    return 0

if __name__ == "__main__":
    sys.exit(main())
