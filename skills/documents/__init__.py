"""
Documents Skills Package

NOTE:
- Skills are executed by a restricted runner that may not have optional deps (e.g. PyMuPDF/fitz).
- Avoid importing heavy/optional modules at import-time; expose lazy wrappers instead.
"""

def extract_text(*args, **kwargs):
    from .pdf_bridge import extract_text as _impl

    return _impl(*args, **kwargs)


def summarize_pdf(*args, **kwargs):
    from .pdf_bridge import summarize_pdf as _impl

    return _impl(*args, **kwargs)


def get_pdf_info(*args, **kwargs):
    from .pdf_bridge import get_pdf_info as _impl

    return _impl(*args, **kwargs)


def extract_chapters(*args, **kwargs):
    from .epub_bridge import extract_chapters as _impl

    return _impl(*args, **kwargs)


def summarize_epub(*args, **kwargs):
    from .epub_bridge import summarize_epub as _impl

    return _impl(*args, **kwargs)


def get_epub_info(*args, **kwargs):
    from .epub_bridge import get_epub_info as _impl

    return _impl(*args, **kwargs)
