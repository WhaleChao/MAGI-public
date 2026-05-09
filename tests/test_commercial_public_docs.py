from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


REQUIRED_DOCS = [
    "SECURITY.md",
    "SUPPORT.md",
    "docs/COMMERCIAL_READINESS.md",
    "docs/TERMS_OF_SERVICE.md",
    "docs/PRIVACY_POLICY.md",
    "docs/DATA_RETENTION_POLICY.md",
    "docs/THIRD_PARTY_BOM.md",
]


def test_commercial_public_docs_exist_and_are_linked_from_readme():
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    readme_zh = (ROOT / "README.zh-TW.md").read_text(encoding="utf-8")
    combined = readme + "\n" + readme_zh

    for rel_path in REQUIRED_DOCS:
        path = ROOT / rel_path
        assert path.exists(), rel_path
        assert path.stat().st_size > 500, rel_path
        assert rel_path in combined, rel_path


def test_public_readme_points_to_public_repository():
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    readme_zh = (ROOT / "README.zh-TW.md").read_text(encoding="utf-8")

    assert "git clone https://github.com/WhaleChao/MAGI-public.git" in readme
    assert "git clone https://github.com/WhaleChao/MAGI-public.git" in readme_zh
