from __future__ import annotations

import pytest

from api.laf_case_classifier import normalize_laf_case_fields, normalize_laf_case_type
from casper_ecosystem.law_firm_orchestrators.laf_folder_builder import LAFFolderBuilder


@pytest.mark.parametrize(
    ("raw_stage", "expected_stage"),
    [
        ("刑事一審辯護", "一審"),
        ("刑事第二審辯護", "二審"),
        ("刑事三審辯護案件", "三審"),
        ("刑事更審辯護", "更審"),
    ],
)
def test_criminal_service_labels_are_split_into_type_and_stage(
    raw_stage: str,
    expected_stage: str,
) -> None:
    assert normalize_laf_case_type("", raw_stage, "妨害秩序等") == (
        "刑事",
        expected_stage,
    )


def test_normalized_fields_preserve_substantive_reason() -> None:
    assert normalize_laf_case_fields(
        "刑事",
        "刑事一審辯護",
        "妨害秩序等",
    ) == ("刑事", "一審", "妨害秩序等")


def test_folder_builder_never_leaks_laf_criminal_service_label() -> None:
    builder = LAFFolderBuilder.__new__(LAFFolderBuilder)

    folder_name = builder._build_folder_name(
        {
            "case_number": "2026-0089",
            "client_name": "楊聖恩",
            "case_type": "刑事",
            "case_stage": "刑事一審辯護",
            "case_reason": "妨害秩序等",
        }
    )

    assert folder_name == "2026-0089-楊聖恩-一審-妨害秩序等"
