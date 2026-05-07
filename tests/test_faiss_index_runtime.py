from __future__ import annotations

from pathlib import Path
from threading import Lock


def test_save_to_disk_creates_missing_index_dir(monkeypatch, tmp_path):
    import skills.memory.faiss_index as faiss_index

    index_dir = tmp_path / "nested" / "index_cache"
    monkeypatch.setattr(faiss_index, "INDEX_DIR", str(index_dir))

    class _DummyIndex:
        ntotal = 2

    def _fake_write_index(_index, path):
        Path(path).write_text("index", encoding="utf-8")

    monkeypatch.setattr(faiss_index.faiss, "write_index", _fake_write_index)

    index = object.__new__(faiss_index.FAISSMemoryIndex)
    index.dim = faiss_index.DIM
    index._index = _DummyIndex()
    index._id_map = [11, 12]
    index._doc_to_pos = {11: 0, 12: 1}
    index._rw_lock = Lock()
    index._dirty = True
    index._index_type = "flat"

    assert index.save_to_disk() is True
    assert (index_dir / faiss_index.INDEX_FILE).exists()
    assert (index_dir / faiss_index.IDMAP_FILE).exists()
    assert (index_dir / "meta.json").exists()
