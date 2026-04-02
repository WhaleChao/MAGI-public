"""
Codebase Memory (RAG) Skill - Unified Embedding Version
========================================================
Provides specialized memory capabilities for analyzing large codebases.
Uses NumPy for lightweight vector storage and Ollama nomic-embed-text (768d)
for embedding generation — the same model used by the main MAGI memory system.

Features:
1. Ingest: Reads code files, chunks them, and stores embeddings in memory.
2. Query: Retrieves relevant code chunks based on a query using Cosine Similarity.
3. Reset: Clears memory for a fresh start.
"""

import logging
import os

import numpy as np

logger = logging.getLogger("CodebaseRAG")

# Embedding via the same Ollama model used by mem_bridge (nomic-embed-text, 768d).
# Lazy-imported to avoid circular imports at module load time.
_get_embedding_fn = None


def _ensure_embedding_fn():
    """Lazy-load the embedding function from mem_bridge."""
    global _get_embedding_fn
    if _get_embedding_fn is not None:
        return True
    try:
        from skills.memory.mem_bridge import get_embedding
        _get_embedding_fn = get_embedding
        return True
    except Exception as e:
        logger.warning("Cannot load mem_bridge.get_embedding: %s. RAG disabled.", e)
        return False


def _embed(text: str):
    """Get embedding for a text string. Returns list[float] or None."""
    if not _ensure_embedding_fn():
        return None
    try:
        vec = _get_embedding_fn(text)
        # mem_bridge returns [0.0]*768 on failure — detect that
        if vec and any(v != 0.0 for v in vec[:10]):
            return vec
        return None
    except Exception as e:
        logger.warning("Embedding error: %s", e)
        return None


# Configuration
CHUNK_SIZE = 50   # Lines per chunk
OVERLAP = 10      # Overlap lines to maintain context


class CodebaseMemory:
    def __init__(self):
        self.documents = []
        self.metadatas = []
        self.embeddings = None  # np.ndarray (N, dim) or None

    def ingest_file(self, file_path):
        """
        Reads a file, chunks it, generates embeddings, and stores in memory.
        """
        if not _ensure_embedding_fn():
            logger.warning("Embedding function unavailable, cannot ingest.")
            return False

        if not os.path.exists(file_path):
            logger.warning("File not found: %s", file_path)
            return False

        file_name = os.path.basename(file_path)
        logger.info("🧠 Ingesting %s into memory...", file_name)

        try:
            with open(file_path, "r", encoding="utf-8") as f:
                lines = f.readlines()
        except Exception as e:
            logger.error("Error reading %s: %s", file_name, e)
            return False

        total_lines = len(lines)
        if total_lines == 0:
            return False

        # Chunking Logic (Sliding Window)
        new_chunks = []
        new_metadatas = []

        for i in range(0, total_lines, CHUNK_SIZE - OVERLAP):
            end = min(i + CHUNK_SIZE, total_lines)
            chunk_content = "".join(lines[i:end])
            new_chunks.append(chunk_content)
            new_metadatas.append({"source": file_name, "start_line": i, "end_line": end})

        # Generate Embeddings
        if not new_chunks:
            logger.warning("No content to chunk for %s", file_name)
            return False

        try:
            vecs = []
            for chunk in new_chunks:
                vec = _embed(chunk)
                if vec is None:
                    logger.warning("Embedding failed for a chunk in %s, skipping file.", file_name)
                    return False
                vecs.append(vec)

            new_embeddings = np.array(vecs, dtype=np.float32)

            if self.embeddings is None:
                self.documents = new_chunks
                self.metadatas = new_metadatas
                self.embeddings = new_embeddings
            else:
                self.documents.extend(new_chunks)
                self.metadatas.extend(new_metadatas)
                self.embeddings = np.vstack([self.embeddings, new_embeddings])

            logger.info("✅ Stored %d chunks for %s", len(new_chunks), file_name)
            return True
        except Exception as e:
            logger.error("Encoding error: %s", e)
            return False

    def query(self, query_text, n_results=3):
        """
        Retrieves top-k relevant chunks for a given query using Cosine Similarity.
        """
        if self.embeddings is None or len(self.documents) == 0:
            return {'documents': [[]], 'metadatas': [[]]}

        try:
            query_vec = _embed(query_text)
            if query_vec is None:
                return {'documents': [[]], 'metadatas': [[]]}

            query_embedding = np.array([query_vec], dtype=np.float32)

            # Cosine similarity via dot product on normalized vectors
            norms_e = np.linalg.norm(self.embeddings, axis=1, keepdims=True)
            norms_e = np.where(norms_e < 1e-12, 1.0, norms_e)
            norm_q = np.linalg.norm(query_embedding)
            if norm_q < 1e-12:
                return {'documents': [[]], 'metadatas': [[]]}

            scores = np.dot(self.embeddings / norms_e, (query_embedding / norm_q).T).flatten()

            # Get top-k indices
            top_k_indices = np.argsort(scores)[::-1][:n_results]

            results = {
                'documents': [[self.documents[i] for i in top_k_indices]],
                'metadatas': [[self.metadatas[i] for i in top_k_indices]],
                'scores': [[float(scores[i]) for i in top_k_indices]]
            }

            return results
        except Exception as e:
            logger.error("Query error: %s", e)
            return {'documents': [[]], 'metadatas': [[]]}

    def reset(self):
        """
        Clears the entire memory.
        """
        self.documents = []
        self.metadatas = []
        self.embeddings = None
        logger.info("🧹 Memory Reset.")


# Singleton Instance for easy import
memory = CodebaseMemory()

if __name__ == "__main__":
    # Test
    test_file = "scripts/ops/distributed_code_review.py"
    if os.path.exists(test_file):
        memory.reset()
        if memory.ingest_file(test_file):
            results = memory.query("security vulnerabilities")
            print("\n🔍 Query Result:")
            if results['documents'][0]:
                for doc in results['documents'][0]:
                    print("-" * 20)
                    print(doc[:200] + "...")

