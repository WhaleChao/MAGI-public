#!/usr/bin/env python3
"""Add the atomic case-document idempotency invariant used by OSC writers."""

from __future__ import annotations

from api.osc.utils import _osc_exec


def migrate() -> dict[str, int | bool]:
    duplicate, _ = _osc_exec(
        """
        SELECT COUNT(*) AS n FROM (
            SELECT case_id, file_path
              FROM case_documents
             GROUP BY case_id, file_path
            HAVING COUNT(*) > 1
        ) duplicate_paths
        """,
        fetch="one",
    )
    duplicate_count = int((duplicate or {}).get("n") or 0)
    if duplicate_count:
        raise RuntimeError(f"case_documents_duplicate_paths:{duplicate_count}")

    _osc_exec(
        "ALTER TABLE case_documents ADD COLUMN IF NOT EXISTS idempotency_key CHAR(64) NULL",
        fetch="none",
    )
    updated, _ = _osc_exec(
        """
        UPDATE case_documents
           SET idempotency_key=SHA2(CONCAT(COALESCE(tenant_id, 'default'), CHAR(0), case_id, CHAR(0), file_path), 256)
         WHERE idempotency_key IS NULL OR idempotency_key=''
        """,
        fetch="none",
    )
    index, _ = _osc_exec(
        """
        SELECT COUNT(*) AS n
          FROM information_schema.statistics
         WHERE table_schema=DATABASE()
           AND table_name='case_documents'
           AND index_name='uq_case_documents_idempotency'
        """,
        fetch="one",
    )
    created = not bool(int((index or {}).get("n") or 0))
    if created:
        _osc_exec(
            "CREATE UNIQUE INDEX uq_case_documents_idempotency ON case_documents (idempotency_key)",
            fetch="none",
        )
    return {
        "ok": True,
        "duplicate_paths": duplicate_count,
        "backfilled": int((updated or {}).get("rowcount") or 0),
        "index_created": created,
    }


if __name__ == "__main__":
    print(migrate())
