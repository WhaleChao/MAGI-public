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
from magi_v3 import fcntl_compat as fcntl
import hashlib
import shutil
import threading
import time
import warnings
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

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
_SHARED_STATE_DIR = (os.environ.get("MAGI_SHARED_STATE_DIR") or "").strip()
INDEX_DIR = os.environ.get(
    "FAISS_INDEX_DIR",
    str(Path(_SHARED_STATE_DIR).expanduser() / "memory" / "index_cache")
    if _SHARED_STATE_DIR
    else str(Path(__file__).parent / "index_cache"),
)
INDEX_FILE = "mem_index.faiss"
IDMAP_FILE = "mem_idmap.npy"
ACTIVE_MANIFEST_FILE = "active_generation.json"
GENERATION_MANIFEST_FILE = "generation_manifest.json"
GENERATIONS_DIR = "generations"

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


def active_generation_paths(index_dir: str | Path = INDEX_DIR) -> dict[str, Path]:
    """Return committed artifact paths without consulting staging generations."""

    root = Path(index_dir)
    active = json.loads((root / ACTIVE_MANIFEST_FILE).read_text(encoding="utf-8"))
    generation = str(active.get("active_generation") or "")
    if (
        active.get("schema_version") != 1
        or not generation
        or "/" in generation
        or generation.startswith(".")
    ):
        raise ValueError("active FAISS generation manifest is invalid")
    directory = root / GENERATIONS_DIR / generation
    return {
        name: directory / name
        for name in (INDEX_FILE, IDMAP_FILE, "meta.json", GENERATION_MANIFEST_FILE)
    }


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

    def __init__(self, dim: int = DIM, *, load_existing: bool = True):
        self.dim = dim
        self._index: Optional[faiss.Index] = None
        self._id_map: List[int] = []        # position → doc_id
        self._doc_to_pos: Dict[int, int] = {}  # doc_id → position (for dedup)
        self._rw_lock = threading.Lock()
        self._dirty = False
        self._index_type = "none"
        self._publish_fault_hook: Optional[Callable[[str], None]] = None

        os.makedirs(INDEX_DIR, exist_ok=True)

        # Try loading from disk
        if not load_existing or not self._load_from_disk():
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

    def build_from_db_streaming(
        self,
        db_config: dict,
        *,
        batch_size: int = 512,
        training_sample_size: int = 20_000,
        low_memory_ivfpq_threshold: int = 250_000,
        publish: bool = True,
    ) -> dict[str, int | str]:
        """Build a fresh index without retaining every MariaDB vector in Python.

        The legacy builder first accumulated a list of all JSON vectors and then
        copied it into a NumPy array.  At production scale that overlapped the
        old on-disk index, PyMuPDF and the ingest worker.  This builder trains on
        one bounded sample, streams fixed-size batches into a fresh index, and
        is intended for a dedicated post-ingest maintenance process.
        """

        import mysql.connector

        batch_size = max(32, min(int(batch_size), 4096))
        training_sample_size = max(2_000, min(int(training_sample_size), 50_000))
        low_memory_ivfpq_threshold = max(
            TIER_IVF_THRESHOLD, int(low_memory_ivfpq_threshold)
        )
        conn = mysql.connector.connect(**db_config, connection_timeout=10)
        count_cursor = conn.cursor()
        try:
            count_cursor.execute("SELECT COUNT(*) FROM vectors")
            row = count_cursor.fetchone()
            declared_total = int(row[0] if row else 0)
        finally:
            count_cursor.close()
        if declared_total <= 0:
            conn.close()
            self._index = faiss.IndexFlatIP(self.dim)
            self._index_type = "flat"
            self._id_map = []
            self._doc_to_pos = {}
            self._dirty = True
            if publish and not self.save_to_disk():
                raise RuntimeError("empty streamed FAISS index publish failed")
            return {
                "declared_rows": 0,
                "indexed_rows": 0,
                "invalid_rows": 0,
                "training_rows": 0,
                "max_batch_rows": 0,
                "index_type": "flat",
            }

        def valid_vectors(rows: list[tuple]) -> tuple[list[int], np.ndarray, int]:
            ids: list[int] = []
            vectors: list[list[float]] = []
            invalid = 0
            for row in rows:
                try:
                    doc_id = int(row[0]) if len(row) > 1 else 0
                    raw = row[1] if len(row) > 1 else row[0]
                    vector = json.loads(raw) if isinstance(raw, str) else raw
                    array = np.asarray(vector, dtype=np.float32)
                    if array.shape != (self.dim,) or not np.isfinite(array).all():
                        raise ValueError("invalid vector")
                    ids.append(doc_id)
                    vectors.append(array.tolist())
                except (TypeError, ValueError, json.JSONDecodeError):
                    invalid += 1
            if not vectors:
                return ids, np.empty((0, self.dim), dtype=np.float32), invalid
            matrix = np.asarray(vectors, dtype=np.float32)
            faiss.normalize_L2(matrix)
            finite = np.isfinite(matrix).all(axis=1)
            if not finite.all():
                invalid += int((~finite).sum())
                ids = [doc_id for doc_id, keep in zip(ids, finite) if keep]
                matrix = matrix[finite]
            return ids, matrix, invalid

        training_rows = 0
        if declared_total < TIER_IVF_THRESHOLD:
            index: faiss.Index = faiss.IndexFlatIP(self.dim)
            index_type = "flat"
        else:
            training_cursor = conn.cursor()
            try:
                training_cursor.execute(
                    "SELECT doc_id, embedding FROM vectors ORDER BY doc_id LIMIT %s",
                    (min(declared_total, training_sample_size),),
                )
                training_raw = training_cursor.fetchall()
            finally:
                training_cursor.close()
            _training_ids, training, _training_invalid = valid_vectors(training_raw)
            training_rows = len(training)
            if training_rows < 256:
                conn.close()
                raise RuntimeError("insufficient valid training vectors for bounded FAISS rebuild")
            if declared_total < low_memory_ivfpq_threshold:
                nlist = min(IVF_NLIST, max(1, training_rows // 40))
                index_type = "ivf_flat"
                quantizer = faiss.IndexFlatIP(self.dim)
                index = faiss.IndexIVFFlat(
                    quantizer, self.dim, nlist, faiss.METRIC_INNER_PRODUCT
                )
            else:
                nlist = min(
                    IVFPQ_NLIST,
                    max(256, min(int(declared_total**0.5), training_rows // 40)),
                )
                index_type = "ivf_pq"
                quantizer = faiss.IndexFlatIP(self.dim)
                index = faiss.IndexIVFPQ(
                    quantizer,
                    self.dim,
                    nlist,
                    PQ_M,
                    PQ_NBITS,
                    faiss.METRIC_INNER_PRODUCT,
                )
            index.train(training)
            del training

        stream_cursor = conn.cursor(buffered=False)
        indexed_ids: list[int] = []
        invalid_rows = 0
        max_batch_rows = 0
        try:
            stream_cursor.execute("SELECT doc_id, embedding FROM vectors ORDER BY doc_id")
            while True:
                rows = stream_cursor.fetchmany(batch_size)
                if not rows:
                    break
                max_batch_rows = max(max_batch_rows, len(rows))
                ids, vectors, invalid = valid_vectors(rows)
                invalid_rows += invalid
                if not ids:
                    continue
                index.add(vectors)
                indexed_ids.extend(ids)
        finally:
            stream_cursor.close()
            conn.close()

        if index.ntotal != len(indexed_ids) or len(indexed_ids) != len(set(indexed_ids)):
            raise RuntimeError("streamed FAISS index/id-map consistency check failed")
        with self._rw_lock:
            self._index = index
            self._id_map = indexed_ids
            # The dedicated builder exits after publishing. Avoid a second large
            # Python dictionary here; normal readers reconstruct it on load.
            self._doc_to_pos = {}
            self._index_type = index_type
            self._dirty = True
        if publish and not self.save_to_disk():
            raise RuntimeError("streamed FAISS index publish failed")
        return {
            "declared_rows": declared_total,
            "indexed_rows": len(indexed_ids),
            "invalid_rows": invalid_rows,
            "training_rows": training_rows,
            "max_batch_rows": max_batch_rows,
            "index_type": index_type,
        }

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

    @staticmethod
    def _sha256_path(path: str) -> str:
        digest = hashlib.sha256()
        with open(path, "rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
        return digest.hexdigest()

    @staticmethod
    def _fsync_directory(path: str) -> None:
        descriptor = os.open(path, os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)

    def _publish_fault(self, point: str) -> None:
        hook = getattr(self, "_publish_fault_hook", None)
        if hook is not None:
            hook(point)

    def _generation_manifest(self, generation_dir: str, generation: str) -> dict[str, Any]:
        files: dict[str, dict[str, Any]] = {}
        for name in (INDEX_FILE, IDMAP_FILE, "meta.json"):
            path = os.path.join(generation_dir, name)
            files[name] = {
                "sha256": self._sha256_path(path),
                "size": os.path.getsize(path),
            }
        return {
            "schema_version": 1,
            "generation": generation,
            "index_type": self._index_type,
            "total": int(self._index.ntotal),
            "dim": int(self.dim),
            "files": files,
        }

    def _load_generation(
        self,
        generation: str,
        *,
        expected_manifest_sha256: str = "",
    ) -> tuple[Any, list[int], str]:
        if not generation or "/" in generation or generation.startswith("."):
            raise ValueError("invalid FAISS generation name")
        directory = os.path.join(INDEX_DIR, GENERATIONS_DIR, generation)
        manifest_path = os.path.join(directory, GENERATION_MANIFEST_FILE)
        if expected_manifest_sha256 and self._sha256_path(manifest_path) != expected_manifest_sha256:
            raise ValueError("FAISS generation manifest hash mismatch")
        with open(manifest_path, encoding="utf-8") as handle:
            manifest = json.load(handle)
        if (
            not isinstance(manifest, dict)
            or manifest.get("schema_version") != 1
            or manifest.get("generation") != generation
            or manifest.get("dim") != self.dim
            or not isinstance(manifest.get("total"), int)
            or not isinstance(manifest.get("files"), dict)
        ):
            raise ValueError("FAISS generation manifest identity is invalid")
        for name in (INDEX_FILE, IDMAP_FILE, "meta.json"):
            row = manifest["files"].get(name)
            path = os.path.join(directory, name)
            if (
                not isinstance(row, dict)
                or row.get("size") != os.path.getsize(path)
                or row.get("sha256") != self._sha256_path(path)
            ):
                raise ValueError(f"FAISS generation artifact mismatch: {name}")
        index = faiss.read_index(os.path.join(directory, INDEX_FILE))
        id_map = np.load(os.path.join(directory, IDMAP_FILE)).tolist()
        with open(os.path.join(directory, "meta.json"), encoding="utf-8") as handle:
            meta = json.load(handle)
        total = int(manifest["total"])
        if (
            index.d != self.dim
            or index.ntotal != total
            or len(id_map) != total
            or len(id_map) != len(set(id_map))
            or meta.get("total") != total
            or meta.get("dim") != self.dim
            or meta.get("index_type") != manifest.get("index_type")
        ):
            raise ValueError("FAISS generation index/id-map/meta consistency failed")
        return index, [int(value) for value in id_map], str(manifest.get("index_type") or "flat")

    def save_to_disk(
        self, *, precommit_validator: Callable[[], bool] | None = None
    ) -> bool:
        """Publish one complete generation through a single manifest commit point."""
        staging_dir = generation_dir = ""
        active_committed = False
        try:
            os.makedirs(INDEX_DIR, exist_ok=True)
            generations_root = os.path.join(INDEX_DIR, GENERATIONS_DIR)
            os.makedirs(generations_root, exist_ok=True)
            lock_path = os.path.join(INDEX_DIR, ".faiss_index.write.lock")
            generation = f"g-{time.time_ns()}-{os.getpid()}-{threading.get_ident()}"
            staging_dir = os.path.join(generations_root, f".{generation}.tmp")
            generation_dir = os.path.join(generations_root, generation)
            with self._rw_lock:
                with open(lock_path, "a") as lock_fh:
                    fcntl.flock(lock_fh, fcntl.LOCK_EX)
                    try:
                        os.mkdir(staging_dir)
                        index_path = os.path.join(staging_dir, INDEX_FILE)
                        idmap_path = os.path.join(staging_dir, IDMAP_FILE)
                        meta_path = os.path.join(staging_dir, "meta.json")
                        faiss.write_index(self._index, index_path)
                        with open(index_path, "rb") as handle:
                            os.fsync(handle.fileno())
                        self._publish_fault("index_written")
                        with open(idmap_path, "wb") as handle:
                            np.save(handle, np.array(self._id_map, dtype=np.int64))
                            handle.flush()
                            os.fsync(handle.fileno())
                        self._publish_fault("idmap_written")
                        meta = {
                            "index_type": self._index_type,
                            "total": int(self._index.ntotal),
                            "dim": int(self.dim),
                            "updated": time.strftime("%Y-%m-%dT%H:%M:%S"),
                            "generation": generation,
                        }
                        with open(meta_path, "x", encoding="utf-8") as f:
                            json.dump(meta, f)
                            f.flush()
                            os.fsync(f.fileno())
                        self._publish_fault("meta_written")
                        manifest = self._generation_manifest(staging_dir, generation)
                        manifest_path = os.path.join(staging_dir, GENERATION_MANIFEST_FILE)
                        with open(manifest_path, "x", encoding="utf-8") as handle:
                            json.dump(manifest, handle, sort_keys=True, separators=(",", ":"))
                            handle.flush()
                            os.fsync(handle.fileno())
                        self._publish_fault("generation_manifest_written")
                        self._fsync_directory(staging_dir)
                        os.replace(staging_dir, generation_dir)
                        staging_dir = ""
                        self._fsync_directory(generations_root)
                        self._publish_fault("generation_renamed")
                        if (
                            precommit_validator is not None
                            and precommit_validator() is not True
                        ):
                            raise RuntimeError(
                                "FAISS generation failed its precommit validator"
                            )
                        active_path = os.path.join(INDEX_DIR, ACTIVE_MANIFEST_FILE)
                        previous = ""
                        previous_manifest_sha256 = ""
                        if os.path.exists(active_path):
                            try:
                                with open(active_path, encoding="utf-8") as handle:
                                    previous_active = json.load(handle)
                                    previous = str(
                                        previous_active.get("active_generation") or ""
                                    )
                                    previous_manifest_sha256 = str(
                                        previous_active.get("generation_manifest_sha256")
                                        or ""
                                    )
                            except Exception:
                                previous = ""
                                previous_manifest_sha256 = ""
                        active = {
                            "schema_version": 1,
                            "active_generation": generation,
                            "previous_generation": previous,
                            "previous_generation_manifest_sha256": previous_manifest_sha256,
                            "generation_manifest_sha256": self._sha256_path(
                                os.path.join(generation_dir, GENERATION_MANIFEST_FILE)
                            ),
                            "total": int(self._index.ntotal),
                            "dim": int(self.dim),
                        }
                        active_tmp = os.path.join(
                            INDEX_DIR,
                            f".{ACTIVE_MANIFEST_FILE}.{generation}.tmp",
                        )
                        with open(active_tmp, "x", encoding="utf-8") as handle:
                            json.dump(active, handle, sort_keys=True, separators=(",", ":"))
                            handle.flush()
                            os.fsync(handle.fileno())
                        os.replace(active_tmp, active_path)
                        active_committed = True
                        self._publish_fault("active_manifest_replaced")
                        self._fsync_directory(INDEX_DIR)
                        self._publish_fault("directory_fsynced")
                        keep = {generation, previous} - {""}
                        for name in os.listdir(generations_root):
                            if name.startswith("g-") and name not in keep:
                                shutil.rmtree(
                                    os.path.join(generations_root, name),
                                    ignore_errors=True,
                                )
                        self._fsync_directory(generations_root)
                    finally:
                        fcntl.flock(lock_fh, fcntl.LOCK_UN)
                self._dirty = False
            size_mb = os.path.getsize(os.path.join(generation_dir, INDEX_FILE)) / 1e6
            logger.info("Published FAISS generation %s (%.1f MB)", generation, size_mb)
            return True
        except Exception as e:
            logger.error("Failed to save index: %s", e)
            if staging_dir:
                shutil.rmtree(staging_dir, ignore_errors=True)
            if generation_dir and not active_committed:
                shutil.rmtree(generation_dir, ignore_errors=True)
            return False

    def _load_from_disk(self) -> bool:
        """Load one verified generation; fall back without mixing artifacts."""
        idx_path = os.path.join(INDEX_DIR, INDEX_FILE)
        map_path = os.path.join(INDEX_DIR, IDMAP_FILE)
        meta_path = os.path.join(INDEX_DIR, "meta.json")
        active_path = os.path.join(INDEX_DIR, ACTIVE_MANIFEST_FILE)
        lock_path = os.path.join(INDEX_DIR, ".faiss_index.write.lock")
        try:
            os.makedirs(INDEX_DIR, exist_ok=True)
            with open(lock_path, "a") as lock_fh:
                fcntl.flock(lock_fh, fcntl.LOCK_SH)
                try:
                    loaded = None
                    if os.path.exists(active_path):
                        try:
                            with open(active_path, encoding="utf-8") as handle:
                                active = json.load(handle)
                            if (
                                not isinstance(active, dict)
                                or active.get("schema_version") != 1
                                or active.get("dim") != self.dim
                            ):
                                raise ValueError("active FAISS manifest is invalid")
                            candidates = [
                                (
                                    str(active.get("active_generation") or ""),
                                    str(active.get("generation_manifest_sha256") or ""),
                                ),
                                (
                                    str(active.get("previous_generation") or ""),
                                    str(
                                        active.get(
                                            "previous_generation_manifest_sha256"
                                        )
                                        or ""
                                    ),
                                ),
                            ]
                        except Exception:
                            generation_root = os.path.join(INDEX_DIR, GENERATIONS_DIR)
                            candidates = [
                                (name, "")
                                for name in sorted(os.listdir(generation_root), reverse=True)
                                if name.startswith("g-")
                            ] if os.path.isdir(generation_root) else []
                        for generation, manifest_sha in candidates:
                            if not generation:
                                continue
                            try:
                                loaded = self._load_generation(
                                    generation,
                                    expected_manifest_sha256=manifest_sha,
                                )
                                break
                            except Exception as exc:
                                logger.error(
                                    "Rejected FAISS generation %s: %s", generation, exc
                                )
                    elif os.path.exists(idx_path) and os.path.exists(map_path):
                        index = faiss.read_index(idx_path)
                        id_map = np.load(map_path).tolist()
                        meta = {}
                        if os.path.exists(meta_path):
                            with open(meta_path, encoding="utf-8") as handle:
                                meta = json.load(handle)
                        if index.d != self.dim or index.ntotal != len(id_map):
                            raise ValueError("legacy FAISS index/id-map consistency failed")
                        loaded = (index, id_map, str(meta.get("index_type") or "flat"))
                    if loaded is None:
                        return False
                    self._index, self._id_map, self._index_type = loaded
                    self._doc_to_pos = {
                        did: i for i, did in enumerate(self._id_map)
                    }
                finally:
                    fcntl.flock(lock_fh, fcntl.LOCK_UN)
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
        try:
            meta_path = str(active_generation_paths(INDEX_DIR)["meta.json"])
        except Exception:
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
