from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


REQUIRED_DOCS = [
    "SECURITY.md",
    "SUPPORT.md",
    "docs/COMMERCIAL_READINESS.md",
    "docs/PUBLIC_SELF_INSTALL.md",
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


def test_public_readme_uses_public_repository_clone_target():
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    readme_zh = (ROOT / "README.zh-TW.md").read_text(encoding="utf-8")

    assert "git clone https://github.com/WhaleChao/MAGI-public.git" in readme
    assert "git clone https://github.com/WhaleChao/MAGI-public.git" in readme_zh
    assert "git clone https://github.com/WhaleChao/MAGI-v2.git" not in readme
    assert "git clone https://github.com/WhaleChao/MAGI-v2.git" not in readme_zh


def test_current_readmes_and_manual_cover_factory_agent_workflows():
    paths = [
        ROOT / "README.md",
        ROOT / "README.zh-TW.md",
        ROOT / "docs" / "USER_GUIDE.md",
        ROOT / "docs" / "guides" / "MAGI_操作手冊.md",
    ]
    for path in paths:
        text = path.read_text(encoding="utf-8")
        assert "37" in text, path
        assert "Agent" in text, path
        assert ("MENUBAR" in text or "menubar" in text), path
        assert ("calendar" in text.lower() or "行事曆" in text), path
        assert ("confirm" in text.lower() or "確認" in text), path
        assert "/Users/" not in text, path

    docx = ROOT / "docs" / "guides" / "MAGI_操作手冊.docx"
    pdf = ROOT / "docs" / "guides" / "MAGI_操作手冊.pdf"
    assert docx.exists() and docx.stat().st_size > 100_000
    assert pdf.exists() and pdf.stat().st_size > 100_000
