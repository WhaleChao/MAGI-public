from api.osc.client_ids import is_canonical_client_id, next_client_id_from_existing


def test_client_id_sequence_ignores_uuid_webc_and_hex_like_values():
    values = [
        {"id": "C0159"},
        {"id": "C0160"},
        {"id": "C775F05FA"},
        {"id": "C7023687"},
        {"id": "webc-abcdef123456"},
        {"id": "a18fd864-c79b-4d07-ac89-cea6e6025c06"},
    ]

    assert next_client_id_from_existing(values) == "C0161"


def test_client_id_canonical_guard_matches_original_osc_format():
    assert is_canonical_client_id("C0001")
    assert is_canonical_client_id("C0160")
    assert not is_canonical_client_id("C775F05FA")
    assert not is_canonical_client_id("C7023687")
    assert not is_canonical_client_id("webc-abcdef")
