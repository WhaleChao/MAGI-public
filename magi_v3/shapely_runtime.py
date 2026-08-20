"""Fail-closed Shapely provenance check for sealed cookie-cutter releases."""
from __future__ import annotations
from pathlib import Path

def verify_shapely_runtime(runtime: Path, *, allowed_root: Path | None = None) -> tuple[bool, str]:
    try:
        import shapely
        from shapely import constrained_delaunay_triangles
    except Exception:
        return False, "shapely unavailable"
    try:
        version = tuple(int(part) for part in str(shapely.__version__).split(".")[:2])
        origin = Path(str(getattr(shapely, "__file__", ""))).resolve()
        root = (allowed_root or runtime.parent.parent).resolve()
    except (AttributeError, TypeError, ValueError):
        return False, "shapely runtime metadata invalid"
    if not ((2, 0) <= version < (3, 0)):
        return False, "shapely version must be >=2,<3"
    if not callable(constrained_delaunay_triangles):
        return False, "constrained_delaunay_triangles unavailable"
    if "site-packages" not in origin.parts or root not in origin.parents:
        return False, "shapely origin outside bound runtime"
    return True, f"shapely {shapely.__version__} bound"
