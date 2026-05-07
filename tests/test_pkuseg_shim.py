from __future__ import annotations


def test_pkuseg_import_shim_returns_segmenter():
    import pkuseg

    seg = pkuseg.pkuseg()
    tokens = seg.cut("消費者債務清理條例之更生方案")

    assert isinstance(tokens, list)
    assert tokens
    assert any("更生" in token for token in tokens)
