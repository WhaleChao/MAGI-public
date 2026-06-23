from pathlib import Path
import re


FILE_REVIEW_ACTION = Path(__file__).resolve().parents[1] / "skills" / "file-review-orchestrator" / "action.py"


def test_download_sync_dispatches_to_sync_handler():
    """`download_sync` must dispatch to dedicated sync handler, not generic handler."""
    src = FILE_REVIEW_ACTION.read_text(encoding="utf-8")

    assert 'def cmd_download_sync(' in src
    assert '"download_sync"' in src

    m = re.search(
        r'if task\.startswith\("download_sync"\):\n(.*?)\n    if task == "download"',
        src,
        re.S,
    )
    assert m is not None, "download_sync dispatch block is missing"

    block = m.group(1)
    assert '"download_sync"' in block
    assert 'cmd_download_sync' in block
    assert 'cmd_download(case_number=' not in block
