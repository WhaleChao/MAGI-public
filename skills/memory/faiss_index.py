# -*- coding: utf-8 -*-
"""
FAISS Vector Index Manager for MAGI Memory System
==================================================
Auto-scaling index strategy:
  - < 100K docs  → IndexFlatIP   (exact, ~30MB)
  - 100K–1M docs → IndexIVFFlat  (exact, ~3GB)
  - > 1M docs    → IndexIVFPQ    (compressed, ~2GB for 21M)

Usage:
    idx = FAISSMemoryIndex.get_instance()
    idx.search(query_vec, top_k=5)   # → [(doc_id, score), ...]
    idx.add(doc_id, vec)             # incremental insert
"""

import json
import logging
import os
import threading
import time
import warnings
from pathlib import Path
from typing import Dict, List, Optional, Tuple

with warnings.catch_warnings():
    warnings.filterwarnings(
        "ignore",
        message=r"builtin type SwigPyPacked has no __module__ attribute",
        category=DeprecationWarning,
    )
    warnings.filterwarnings(
        "ignore",
        message=r"builtin type SwigPyObject has no __module__ attribute",
        category=DeprecationWarning,
    )
    warnings.filterwarnings(
        "ignore",
        message=r"builtin type swigvarlink has no __module__ attribute",
        category=DeprecationWarning,
    )
    import faiss
import numpy as np

logger = logging.getLogger("FAISSIndex")

# Defaults
DIM = 768
INDEX_DIR = os.environ.get(
    "FAISS_INDEX_DIR",
    str(Path(__file__).parent / "index_cache"),
)
INDEX_FILE = "mem_index.faiss"
IDMAP_FILE = "mem_idmap.npy"

# Thresholds for auto-scaling
TIER_IVF_THRESHOLD = 100_000
TIER_IVFPQ_THRESHOLD = 1_000_000

# IVF parameters
IVF_NLIST = 256           # clusters for < 1M
IVFPQ_NLIST = 65536       # clusters for 21M scale
PQ_M = 96                 # subquantizers (must divide DIM=768)
PQ_NBITS = 8

# nprobe: how many IVF clusters to search (higher = better recall, slower).
# Default uses sqrt(nlist) which gives ~95%+ recall for typical distributions.
# Override via env var for per-deployment tuning.
NPROBE_OVERRIDE = int(os.environ.get("FAISS_NPROBE", "0")) or 0


class FAISSMemoryIndex:
    """Thread-safe singleton FAISS index with auto-scaling."""

    _instance: Optional["FAISSMemoryIndex"] = None
    _lock = threading.Lock()

    @classmethod
    def get_instance(cls, dim: int = DIM) -> "FAISSMemoryIndex":
        if cls._instance is None:
            with cls._lock:
                if cls._instance is None:
                    cls._instance = cls(dim=dim)
        return cls._instance

    def __init__(self, dim: int = DIM):
        self.dim = dim
        self._index: Optional[faiss.Index] = None
        self._id_map: List[int] = []        # position → doc_id
        self._doc_to_pos: Dict[int, int] = {}  # doc_id → position (for dedup)
        self._rw_lock = threading.Lock()
        self._dirty = False
        self._index_type = "none"

        os.makedirs(INDEX_DIR, exist_ok=True)

        # Try loading from disk
        if not self._load_from_disk():
            # Start with empty flat index
            self._index = faiss.IndexFlatIP(self.dim)
            self._index_type = "flat"
            logger.info("Initialized empty FlatIP index (dim=%d)", self.dim)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def search(self, query_vec: list, top_k: int = 5) -> List[Tuple[int, float]]:
        """
        KNN search. Returns [(doc_id, score), ...] sorted by score desc.
        """
        if self._index is None or self._index.ntotal == 0:
            return []

        q = np.array([query_vec], dtype=np.float32)
        faiss.normalize_L2(q)  # normalize for inner product = cosine sim

        with self._rw_lock:
            k = min(top_k, self._index.ntotal)
            if hasattr(self._index, 'nprobe'):
                # Adaptive nprobe: sqrt(nlist) balances recall vs speed.
                # Old formula (ntotal//100) resolved to 32 for 400K vecs with
                # nlist=256, effectively brute-forcing 12.5% of clusters.
                # sqrt(256)=16 gives ~95% recall at ~2x faster search.
                if NPROBE_OVERRIDE > 0:
                    self._index.nprobe = NPROBE_OVERRIDE
                else:
                    _nlist = getattr(self._index, 'nlist', 256)
                    self._index.nprobe = max(1, int(_nlist ** 0.5))
            scores, indices = self._index.search(q, k)

        results = []
        for score, idx in zip(scores[0], indices[0]):
            if idx < 0 or idx >= len(self._id_map):
                continue
            doc_id = self._id_map[idx]
            results.append((doc_id, float(score)))

        return results

    def add(self, doc_id: int, vec: list) -> None:
        """Add a single vector. Skips if doc_id already indexed."""
        if doc_id in self._doc_to_pos:
            return

        v = np.array([vec], dtype=np.float32)
        faiss.normalize_L2(v)

        with self._rw_lock:
            # For IVF indexes, we need to handle the case where the index
            # is trained but we're adding incrementally
            if self._index_type == "flat":
                self._index.add(v)
            else:
                # IVF/IVFPQ - add to index if trained
                if self._index.is_trained:
                    self._index.add(v)
                else:
                    logger.warning("Index not trained, skipping add for doc_id=%d", doc_id)
                    return

            pos = len(self._id_map)
            self._id_map.append(doc_id)
            self._doc_to_pos[doc_id] = pos
            self._dirty = True

    def add_batch(self, doc_ids: List[int], vecs: np.ndarray) -> int:
        """
        Add multiple vectors at once. Returns count of newly added.
        vecs: shape (N, dim), float32
        """
        # Filter out already-indexed
        mask = [i for i, did in enumerate(doc_ids) if did not in self._doc_to_pos]
        if not mask:
            return 0

        new_ids = [doc_ids[i] for i in mask]
        new_vecs = vecs[mask].copy()
        faiss.normalize_L2(new_vecs)

        with self._rw_lock:
            if self._index_type == "flat":
                self._index.add(new_vecs)
            elif self._index.is_trained:
                self._index.add(new_vecs)
            else:
                logger.warning("Index not trained, cannot batch add")
                return 0

            base_pos = len(self._id_map)
            for i, did in enumerate(new_ids):
                self._id_map.append(did)
                self._doc_to_pos[did] = base_pos + i

            self._dirty = True
        return len(new_ids)

    @property
    def total(self) -> int:
        return self._index.ntotal if self._index else 0

    @property
    def index_type(self) -> str:
        return self._index_type

    # ------------------------------------------------------------------
    # Build from DB
    # ------------------------------------------------------------------

    def build_from_db(self, db_config: dict, batch_size: int = 2000) -> int:
        """
        One-shot: load ALL vectors from MariaDB and build index.
        Returns total vectors indexed.
        """
        import mysql.connector

        logger.info("Building FAISS index from MariaDB...")
        t0 = time.time()

        conn = mysql.connector.connect(**db_config, connection_timeout=10)
        cursor = conn.cursor()

        cursor.execute("SELECT COUNT(*) FROM vectors")
        total = cursor.fetchone()[0]
        logger.info("Total vectors in DB: %d", total)

        if total == 0:
            conn.close()
            return 0

        # Stream in batches
        all_ids = []
        all_vecs = []

        cursor.execute("SELECT doc_id, embedding FROM vectors ORDER BY doc_id")
        batch_ids = []
        batch_vecs = []

        for doc_id, vec_json in cursor:
            try:
                vec = json.loads(vec_json)
                if len(vec) != self.dim:
                    continue
                batch_ids.append(doc_id)
                batch_vecs.append(vec)
            except Exception:
                continue

            if len(batch_ids) >= batch_size:
                all_ids.extend(batch_ids)
                all_vecs.extend(batch_vecs)
                batch_ids, batch_vecs = [], []
                if len(all_ids) % 5000 == 0:
                    logger.info("  Loaded %d / %d vectors...", len(all_ids), total)

        # Final batch
        all_ids.extend(batch_ids)
        all_vecs.extend(batch_vecs)
        conn.close()

        if not all_vecs:
            logger.warning("No valid vectors found in DB")
            return 0

        t_load = time.time()
        logger.info("Loaded %d vectors in %.1fs", len(all_ids), t_load - t0)

        # Build numpy array and sanitize — drop rows with NaN/Inf
        vecs_np = np.array(all_vecs, dtype=np.float32)
        del all_vecs  # free memory
        finite_mask = np.isfinite(vecs_np).all(axis=1)
        n_bad = int((~finite_mask).sum())
        if n_bad:
            logger.warning("Dropping %d vectors with NaN/Inf values", n_bad)
            vecs_np = vecs_np[finite_mask]
            all_ids = [aid for aid, ok in zip(all_ids, finite_mask) if ok]
        faiss.normalize_L2(vecs_np)
        # normalize_L2 can produce NaN for zero-norm vectors — drop those too
        finite_mask2 = np.isfinite(vecs_np).all(axis=1)
        n_bad2 = int((~finite_mask2).sum())
        if n_bad2:
            logger.warning("Dropping %d vectors with NaN after normalize", n_bad2)
            vecs_np = vecs_np[finite_mask2]
            all_ids = [aid for aid, ok in zip(all_ids, finite_mask2) if ok]

        # Choose index type based on scale
        n = len(all_ids)
        index = self._create_index_for_scale(n, vecs_np)

        with self._rw_lock:
            self._index = index
            self._id_map = all_ids
            self._doc_to_pos = {did: i for i, did in enumerate(all_ids)}
            self._dirty = True

        t_build = time.time()
        logger.info(
            "✅ FAISS index built: %d vectors, type=%s, %.1fs total",
            n, self._index_type, t_build - t0,
        )

        self.save_to_disk()
        return n

    def _create_index_for_scale(self, n: int, vecs: np.ndarray) -> faiss.Index:
        """Create the right index type based on data size."""
        if n < TIER_IVF_THRESHOLD:
            # Flat: exact search, small memory
            self._index_type = "flat"
            index = faiss.IndexFlatIP(self.dim)
            index.add(vecs)
            logger.info("Using IndexFlatIP (exact) for %d vectors", n)

        elif n < TIER_IVFPQ_THRESHOLD:
            # IVFFlat: clustered exact search
            nlist = min(IVF_NLIST, max(1, n // 40))
            self._index_type = "ivf_flat"
            quantizer = faiss.IndexFlatIP(self.dim)
            index = faiss.IndexIVFFlat(quantizer, self.dim, nlist, faiss.METRIC_INNER_PRODUCT)
            logger.info("Training IVFFlat (nlist=%d) for %d vectors...", nlist, n)
            index.train(vecs)
            index.add(vecs)
            logger.info("Using IndexIVFFlat for %d vectors", n)

        else:
            # IVF+PQ: compressed, for millions of vectors
            nlist = min(IVFPQ_NLIST, max(256, int(n ** 0.5)))
            self._index_type = "ivf_pq"
            quantizer = faiss.IndexFlatIP(self.dim)
            index = faiss.IndexIVFPQ(
                quantizer, self.dim, nlist, PQ_M, PQ_NBITS,
                faiss.METRIC_INNER_PRODUCT,
            )
            # Train on a sample if dataset is huge
            train_size = min(n, 500_000)
            if train_size < n:
                rng = np.random.default_rng(42)
                train_indices = rng.choice(n, train_size, replace=False)
                train_vecs = vecs[train_indices]
            else:
                train_vecs = vecs
            logger.info(
                "Training IVF+PQ (nlist=%d, m=%d) on %d samples...",
                nlist, PQ_M, len(train_vecs),
            )
            index.train(train_vecs)
            index.add(vecs)
            logger.info("Using IndexIVFPQ for %d vectors", n)

        return index

    # ------------------------------------------------------------------
    # Persistence
    # ------------------------------------------------------------------

    def save_to_disk(self) -> bool:
        """Save index + id map to disk (atomic: write tmp then rename)."""
        try:
            os.makedirs(INDEX_DIR, exist_ok=True)
            idx_path = os.path.join(INDEX_DIR, INDEX_FILE)
            map_path = os.path.join(INDEX_DIR, IDMAP_FILE)
            meta_path = os.path.join(INDEX_DIR, "meta.json")

            # Atomic write: tmp file → rename to prevent corruption on SIGKILL
            idx_tmp = idx_path + ".tmp"
            # np.save auto-appends .npy, so use .tmp without .npy extension
            map_tmp_base = os.path.join(INDEX_DIR, "mem_idmap_tmp")
            map_tmp_file = map_tmp_base + ".npy"  # np.save will create this
            meta_tmp = meta_path + ".tmp"

            with self._rw_lock:
                faiss.write_index(self._index, idx_tmp)
                np.save(map_tmp_base, np.array(self._id_map, dtype=np.int64))
                meta = {
                    "index_type": self._index_type,
                    "total": self._index.ntotal,
                    "dim": self.dim,
                    "updated": time.strftime("%Y-%m-%dT%H:%M:%S"),
                }
                with open(meta_tmp, "w") as f:
                    json.dump(meta, f)

                # Atomic rename (all-or-nothing on POSIX)
                os.replace(idx_tmp, idx_path)
                os.replace(map_tmp_file, map_path)
                os.replace(meta_tmp, meta_path)

                self._dirty = False

            size_mb = os.path.getsize(idx_path) / 1e6
            logger.info("Saved FAISS index to %s (%.1f MB)", idx_path, size_mb)
            return True
        except Exception as e:
            logger.error("Failed to save index: %s", e)
            # Clean up tmp files
            for tmp in [idx_tmp, map_tmp_file, meta_tmp]:
                try:
                    os.unlink(tmp)
                except OSError:
                    pass
            return False

    def _load_from_disk(self) -> bool:
        """Load index + id map from disk."""
        idx_path = os.path.join(INDEX_DIR, INDEX_FILE)
        map_path = os.path.join(INDEX_DIR, IDMAP_FILE)
        meta_path = os.path.join(INDEX_DIR, "meta.json")

        if not os.path.exists(idx_path) or not os.path.exists(map_path):
            return False

        try:
            self._index = faiss.read_index(idx_path, faiss.IO_FLAG_MMAP)
            self._id_map = np.load(map_path).tolist()
            self._doc_to_pos = {did: i for i, did in enumerate(self._id_map)}

            # Load metadata
            if os.path.exists(meta_path):
                with open(meta_path) as f:
                    meta = json.load(f)
                self._index_type = meta.get("index_type", "flat")
            else:
                self._index_type = "flat"

            logger.info(
                "Loaded FAISS index from disk: %d vectors, type=%s",
                self._index.ntotal, self._index_type,
            )
            return True
        except Exception as e:
            logger.error("Failed to load index from disk: %s", e)
            return False

    # ------------------------------------------------------------------
    # Sync: pick up new records from DB since last build
    # ------------------------------------------------------------------

    def sync_new_from_db(self, db_config: dict) -> int:
        """
        Incremental sync: fetch vectors with doc_id > max indexed, add them.
        Returns count of newly added vectors.
        """
        if not self._id_map:
            return self.build_from_db(db_config)

        max_id = max(self._id_map)

        import mysql.connector
        conn = mysql.connector.connect(**db_config, connection_timeout=5)
        cursor = conn.cursor()

        cursor.execute(
            "SELECT doc_id, embedding FROM vectors WHERE doc_id > %s ORDER BY doc_id",
            (max_id,),
        )

        new_ids = []
        new_vecs = []
        for doc_id, vec_json in cursor:
            try:
                vec = json.loads(vec_json)
                if len(vec) == self.dim:
                    new_ids.append(doc_id)
                    new_vecs.append(vec)
            except Exception:
                continue
        conn.close()

        if not new_ids:
            return 0

        vecs_np = np.array(new_vecs, dtype=np.float32)
        added = self.add_batch(new_ids, vecs_np)

        if added > 0:
            self.save_to_disk()
            logger.info("Synced %d new vectors from DB", added)

        return added

    def rebuild_if_needed(self, db_config: dict, hours_threshold: float = 24.0) -> bool:
        """
        Check if the index was last built more than `hours_threshold` ago.
        If so, rebuild it completely from MariaDB to purge phantom/deleted memories.
        Returns True if rebuilt.
        """
        meta_path = os.path.join(INDEX_DIR, "meta.json")
        try:
            if os.path.exists(meta_path):
                with open(meta_path) as f:
                    meta = json.load(f)
                updated_str = meta.get("updated", "")
                if updated_str:
                    from datetime import datetime
                    updated_dt = datetime.strptime(updated_str, "%Y-%m-%dT%H:%M:%S")
                    age_hours = (datetime.now() - updated_dt).total_seconds() / 3600.0
                    if age_hours < hours_threshold:
                        logger.debug("FAISS index age %.1fh < %.1fh, no rebuild needed", age_hours, hours_threshold)
                        return False
        except Exception as e:
            logger.warning("Failed to check FAISS index age: %s. Will force rebuild.", e)

        logger.info("FAISS index is older than %.1fh, triggering full rebuild to purge deleted memories...", hours_threshold)

        # Note: build_from_db() internally acquires self._rw_lock when swapping
        # the index (line 246), so reads remain consistent during the rebuild.
        # DB streaming happens without holding the lock to avoid blocking searches.
        self.build_from_db(db_config)
        return True
