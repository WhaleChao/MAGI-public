from __future__ import annotations

import hashlib
import json
import os
from datetime import datetime, timezone
from pathlib import Path

import pytest

from scripts.v3_laf_dedup_compat import (
    LAFDedupBlocked,
    _connect_from_environment,
    create_manifest,
    import_verified_manifest,
    load_verified_manifest,
    verify_imported_manifest,
)


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _source(path: Path, value) -> Path:
    path.write_text(json.dumps(value), encoding="utf-8")
    return path.resolve()


class FakeStore:
    def __init__(self, *, laf=(), dedup=(), duplicate_laf=(), fail_insert_at=None, lock=True):
        self.laf = set(laf)
        self.dedup = set(dedup)
        self.duplicate_laf = set(duplicate_laf)
        self.fail_insert_at = fail_insert_at
        self.lock_available = lock
        self.snapshot = None
        self.schema_validated = False
        self.locked = False
        self.commits = 0
        self.rollbacks = 0
        self.inserts = 0

    def validate_schema(self):
        self.schema_validated = True

    def acquire_lock(self, _timeout):
        self.locked = self.lock_available
        return self.lock_available

    def release_lock(self):
        self.locked = False

    def begin(self):
        self.snapshot = (set(self.laf), set(self.dedup))

    def commit(self):
        self.commits += 1
        self.snapshot = None

    def rollback(self):
        self.rollbacks += 1
        if self.snapshot is not None:
            self.laf, self.dedup = self.snapshot
            self.snapshot = None

    def laf_record_count(self, message_id):
        return 2 if message_id in self.duplicate_laf else int(message_id in self.laf)

    def dedup_record_count(self, message_id):
        return int(message_id in self.dedup)

    def ensure_dedup_record(self, message_id):
        self.inserts += 1
        if self.fail_insert_at == self.inserts:
            raise RuntimeError("injected insert failure")
        self.dedup.add(message_id)

    def insert_laf_record(self, message_id):
        self.inserts += 1
        if self.fail_insert_at == self.inserts:
            raise RuntimeError("injected insert failure")
        self.laf.add(message_id)


def test_snapshot_unions_legacy_list_and_mapping_without_business_payload(tmp_path):
    first = _source(tmp_path / "first.json", ["a" * 16, "b" * 16])
    second = _source(tmp_path / "second.json", {"b" * 16: True, "c" * 16: {"ignored": "value"}})
    output = (tmp_path / "compat.json").resolve()

    report = create_manifest(
        [second, first],
        output,
        created_at=datetime(2026, 7, 15, tzinfo=timezone.utc),
    )

    assert report["record_count"] == 3
    assert report["contains_business_payload"] is False
    assert os.stat(output).st_mode & 0o777 == 0o600
    manifest = json.loads(output.read_text(encoding="utf-8"))
    assert manifest["records"] == ["a" * 16, "b" * 16, "c" * 16]
    assert [row["path"] for row in manifest["sources"]] == sorted([str(first), str(second)])
    assert all(set(row) == {"path", "sha256", "size", "format", "record_count"} for row in manifest["sources"])


def test_manifest_verification_is_hash_bound_and_rejects_source_drift(tmp_path):
    source = _source(tmp_path / "processed.json", ["a" * 16])
    output = (tmp_path / "compat.json").resolve()
    create_manifest([source], output)
    digest = _sha(output)

    assert load_verified_manifest(output, digest)["record_count"] == 1
    source.write_text(json.dumps(["a" * 16, "b" * 16]), encoding="utf-8")
    with pytest.raises(LAFDedupBlocked, match="changed after the snapshot"):
        load_verified_manifest(output, digest)
    with pytest.raises(LAFDedupBlocked, match="SHA-256"):
        load_verified_manifest(output, "0" * 64, revalidate_sources=False)


@pytest.mark.parametrize(
    "payload",
    [
        [" valid-but-spaced"],
        ["a" * 16, "a" * 16],
        [123],
        "not-a-list",
    ],
)
def test_snapshot_fails_closed_on_ambiguous_source(payload, tmp_path):
    source = _source(tmp_path / "processed.json", payload)
    with pytest.raises(LAFDedupBlocked):
        create_manifest([source], (tmp_path / "compat.json").resolve())


def _manifest(records):
    return {"records": list(records), "records_sha256": "f" * 64}


def test_import_is_transactional_idempotent_and_populates_both_stores():
    store = FakeStore(laf={"a"}, dedup={"b"})
    manifest = _manifest(["a", "b", "c"])

    first = import_verified_manifest(manifest, store, apply=True)
    second = import_verified_manifest(manifest, store, apply=True)

    assert store.laf == {"a", "b", "c"}
    assert store.dedup == {"a", "b", "c"}
    assert first["inserted_laf_email_records"] == 2
    assert first["inserted_dedup_registry"] == 2
    assert first["missing_laf_email_records_before_import"] == 2
    assert first["missing_dedup_registry_before_import"] == 2
    assert second["inserted_laf_email_records"] == 0
    assert second["inserted_dedup_registry"] == 0
    assert store.commits == 2
    assert store.locked is False


def test_import_rolls_back_everything_on_partial_failure():
    store = FakeStore(fail_insert_at=3)
    with pytest.raises(RuntimeError, match="injected"):
        import_verified_manifest(_manifest(["a", "b"]), store, apply=True)

    assert store.laf == set()
    assert store.dedup == set()
    assert store.commits == 0
    assert store.rollbacks >= 1
    assert store.locked is False


def test_dry_run_never_mutates_and_preexisting_duplicates_block():
    store = FakeStore(laf={"a"})
    report = import_verified_manifest(_manifest(["a", "b"]), store, apply=False)
    assert report["status"] == "dry_run"
    assert report["mutation_performed"] is False
    assert store.laf == {"a"}
    assert store.dedup == set()
    assert store.commits == 0

    duplicate = FakeStore(duplicate_laf={"a"})
    with pytest.raises(LAFDedupBlocked, match="ambiguous duplicate"):
        import_verified_manifest(_manifest(["a"]), duplicate, apply=True)
    assert duplicate.commits == 0
    assert duplicate.locked is False


def test_unavailable_advisory_lock_blocks_before_transaction():
    store = FakeStore(lock=False)
    with pytest.raises(LAFDedupBlocked, match="owns the database lock"):
        import_verified_manifest(_manifest(["a"]), store, apply=True)
    assert store.snapshot is None
    assert store.commits == 0
    assert store.rollbacks == 0


def test_post_commit_verifier_reads_both_tables_in_a_fresh_read_only_transaction():
    store = FakeStore(laf={"a", "b"}, dedup={"a", "b"})

    report = verify_imported_manifest(_manifest(["a", "b"]), store)

    assert report["status"] == "dual_store_verified"
    assert report["laf_email_records_verified"] == 2
    assert report["dedup_registry_verified"] == 2
    assert report["mutation_performed"] is False
    assert store.commits == 0
    assert store.rollbacks == 1
    assert store.locked is False


@pytest.mark.parametrize(("laf", "dedup"), (({"a"}, set()), (set(), {"a"})))
def test_post_commit_verifier_fails_when_either_table_is_missing(laf, dedup):
    store = FakeStore(laf=laf, dedup=dedup)

    with pytest.raises(LAFDedupBlocked, match="dual-table verification"):
        verify_imported_manifest(_manifest(["a"]), store)

    assert store.commits == 0
    assert store.rollbacks == 1
    assert store.locked is False


@pytest.mark.parametrize("swap", ["symlink", "hardlink", "sha"])
def test_db_env_consumption_rejects_identity_or_hash_swap_before_connection(tmp_path, swap):
    original = tmp_path / "db.env"
    original.write_text("OSC_DB_PASSWORD=never-connect\n", encoding="utf-8")
    original.chmod(0o600)
    expected = _sha(original)
    if swap == "symlink":
        replacement = tmp_path / "replacement.env"
        replacement.write_bytes(original.read_bytes())
        replacement.chmod(0o600)
        original.unlink()
        original.symlink_to(replacement)
    elif swap == "hardlink":
        os.link(original, tmp_path / "second-link.env")
    else:
        original.write_text("OSC_DB_PASSWORD=changed\n", encoding="utf-8")
        original.chmod(0o600)

    with pytest.raises(LAFDedupBlocked):
        _connect_from_environment(original, expected_sha256=expected)


def test_manifest_creation_rejects_symlinked_output_parent(tmp_path):
    source = _source(tmp_path / "processed.json", ["a" * 16])
    real_parent = tmp_path / "real-parent"
    real_parent.mkdir()
    alias = tmp_path / "alias-parent"
    alias.symlink_to(real_parent, target_is_directory=True)

    with pytest.raises(LAFDedupBlocked, match="new absolute file"):
        create_manifest([source], alias / "compat.json")
