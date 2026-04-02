import os
import json
import logging
import numpy as np

logger = logging.getLogger("pdf-namer-rag")

try:
    from sentence_transformers import SentenceTransformer
    RAG_AVAILABLE = True
except ImportError:
    RAG_AVAILABLE = False
    logger.warning("SentenceTransformers not found. RAG disabled.")

SKILL_DIR = os.path.dirname(os.path.abspath(__file__))
TRAINING_DATA_PATH = os.path.join(SKILL_DIR, "training_data.json")

class FeedbackRAG:
    """
    Loads historical user corrections (training_data.json) into a lightweight Numpy Vector DB.
    Provides semantic search over document text to auto-correct future routing.
    """
    def __init__(self):
        self.model = None
        self.texts = []
        self.metadatas = []
        self.embeddings = None
        self._load()

    def _load(self):
        if not RAG_AVAILABLE:
            return
        logger.info("Loading RAG Model for PDF Namer Feedback Loop...")
        try:
            self.model = SentenceTransformer("all-MiniLM-L6-v2")
            if os.path.exists(TRAINING_DATA_PATH):
                with open(TRAINING_DATA_PATH, "r", encoding="utf-8") as f:
                    data = json.load(f)
                
                # We only embed the text_preview
                for item in data:
                    text_preview = item.get("text_preview", "").strip()
                    if len(text_preview) > 20: 
                        self.texts.append(text_preview)
                        self.metadatas.append(item)
                
                if self.texts:
                    self.embeddings = self.model.encode(self.texts)
                    logger.info(f"✅ Loaded {len(self.texts)} feedback examples into RAG.")
        except Exception as e:
            logger.error(f"RAG init error: {e}")

    def query(self, text: str, n_results: int = 3) -> list:
        if not RAG_AVAILABLE or self.embeddings is None or not self.texts:
            return []
        try:
            query_emb = self.model.encode([text])
            scores = np.dot(self.embeddings, query_emb.T).flatten()
            top_k = np.argsort(scores)[::-1][:n_results]
            
            results = []
            for i in top_k:
                # Threshold for similarity
                if scores[i] > 0.45:
                    results.append((float(scores[i]), self.metadatas[i]))
            return results
        except Exception as e:
            logger.error(f"RAG query error: {e}")
            return []

    def log_feedback(self, text_preview: str, case_folder: str, category: str, doc_name: str) -> None:
        """Dynamically add new feedback to training data."""
        new_entry = {
            "text_preview": text_preview,
            "relative_path": f"{case_folder}/{category}/{doc_name}",
            "filename": doc_name,
            "category": category,
        }
        
        # Add to runtime memory if ready
        if RAG_AVAILABLE and self.model:
            self.texts.append(text_preview)
            self.metadatas.append(new_entry)
            new_emb = self.model.encode([text_preview])
            if self.embeddings is None:
                self.embeddings = new_emb
            else:
                self.embeddings = np.vstack([self.embeddings, new_emb])
        
        # Append to JSON
        data = []
        if os.path.exists(TRAINING_DATA_PATH):
            try:
                with open(TRAINING_DATA_PATH, "r", encoding="utf-8") as f:
                    data = json.load(f)
            except Exception:
                logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, 96, exc_info=True)
        
        data.append(new_entry)
        with open(TRAINING_DATA_PATH, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)

# Singleton
rag_engine = FeedbackRAG()
