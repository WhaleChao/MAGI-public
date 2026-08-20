"""Small, dependency-free safety wrapper for Office XML minidom parsing.

The normal runtime uses ``defusedxml``.  V3 validation also runs with
PYTHONSAFEPATH/PYTHONNOUSERSITE, where optional site packages are deliberately
unavailable.  Office Open XML parts never require DTDs or custom entities, so
the fallback rejects both before delegating to the standard-library parser.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any
from xml.dom import minidom as _minidom


MAX_OFFICE_XML_BYTES = 32 * 1024 * 1024
_FORBIDDEN_DECLARATION = re.compile(br"<!\s*(?:DOCTYPE|ENTITY)\b", re.IGNORECASE)


class UnsafeOfficeXML(ValueError):
    """Raised when an XML part uses declarations forbidden by OOXML."""


def _checked_bytes(value: str | bytes | bytearray) -> bytes:
    data = value.encode("utf-8") if isinstance(value, str) else bytes(value)
    if len(data) > MAX_OFFICE_XML_BYTES:
        raise UnsafeOfficeXML("Office XML part exceeds the 32MB safety limit")
    if _FORBIDDEN_DECLARATION.search(data):
        raise UnsafeOfficeXML("DTD and ENTITY declarations are not allowed in Office XML")
    return data


def parseString(value: str | bytes | bytearray, *args: Any, **kwargs: Any):
    return _minidom.parseString(_checked_bytes(value), *args, **kwargs)


def parse(source: Any, *args: Any, **kwargs: Any):
    if hasattr(source, "read"):
        return parseString(source.read(), *args, **kwargs)
    return parseString(Path(source).read_bytes(), *args, **kwargs)
