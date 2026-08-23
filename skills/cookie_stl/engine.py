"""Bounded, offline line-art to cookie cutter/stamp STL conversion.

The engine accepts image bytes and returns an in-memory ZIP.  It does not use
the network, case storage, a database, or a persistent working directory.
"""
from __future__ import annotations

import hashlib
import io
import json
import math
import struct
import time
import zipfile
from dataclasses import dataclass

import numpy as np
from PIL import Image, ImageFilter, ImageOps

try:
    from shapely import constrained_delaunay_triangles, set_precision
    from shapely import affinity
    from shapely.geometry import GeometryCollection, MultiLineString, MultiPolygon, Polygon
    from shapely.geometry.polygon import orient as _orient_polygon
    from shapely.ops import polygonize, unary_union
except Exception:  # pragma: no cover - the deployment preflight checks this.
    Polygon = None  # type: ignore[assignment,misc]


MAX_BYTES = 8 * 1024 * 1024
MAX_PIXELS = 4096 * 4096
MAX_GRID_SIDE = 512
# Keep geometric deviation below a typical 0.4 mm nozzle's half-width.  The
# prior 0.35 mm gate could be visibly faceted even though the mesh was closed.
MAX_CONTOUR_ERROR_MM = 0.15
GEOMETRY_PRECISION_MM = 1e-5
SURFACE_CANONICAL_TOLERANCE_MM = 0.01
MIN_RELIEF_FEATURE_MM = 0.4
MAX_GENERATION_SECONDS = 20.0
MAX_BOUNDARY_SEGMENTS = 200_000
MAX_TRIANGLES_PER_MESH = 150_000
ALLOWED_FORMATS = frozenset({"PNG", "JPEG", "BMP", "TIFF"})


class CookieSTLError(ValueError):
    """A fixed-code, public-safe conversion failure."""


def _check_deadline(deadline: float) -> None:
    if time.monotonic() > deadline:
        raise CookieSTLError("resource_limit_exceeded")


@dataclass(frozen=True, slots=True)
class CookieParameters:
    width_mm: float = 80.0
    blade_height_mm: float = 15.0
    blade_wall_mm: float = 1.2
    rim_mm: float = 3.0
    stamp_base_mm: float = 3.0
    relief_mm: float = 2.0
    clearance_mm: float = 0.3

    def validate(self) -> "CookieParameters":
        bounds = {
            "width_mm": (20.0, 200.0),
            "blade_height_mm": (5.0, 30.0),
            "blade_wall_mm": (0.6, 3.0),
            "rim_mm": (0.0, 15.0),
            "stamp_base_mm": (1.0, 10.0),
            "relief_mm": (0.4, 12.0),
            "clearance_mm": (0.0, 3.0),
        }
        for name, (low, high) in bounds.items():
            value = float(getattr(self, name))
            if not math.isfinite(value) or not low <= value <= high:
                raise CookieSTLError("invalid_dimensions")
        if self.blade_height_mm <= self.stamp_base_mm:
            raise CookieSTLError("invalid_dimensions")
        return self


def _components(mask: np.ndarray) -> list[list[tuple[int, int]]]:
    height, width = mask.shape
    seen = np.zeros_like(mask, dtype=bool)
    result: list[list[tuple[int, int]]] = []
    for raw_y, raw_x in zip(*np.where(mask)):
        y, x = int(raw_y), int(raw_x)
        if seen[y, x]:
            continue
        stack = [(y, x)]
        seen[y, x] = True
        component: list[tuple[int, int]] = []
        while stack:
            current_y, current_x = stack.pop()
            component.append((current_y, current_x))
            for dy, dx in ((1, 0), (-1, 0), (0, 1), (0, -1)):
                next_y, next_x = current_y + dy, current_x + dx
                if (
                    0 <= next_y < height
                    and 0 <= next_x < width
                    and mask[next_y, next_x]
                    and not seen[next_y, next_x]
                ):
                    seen[next_y, next_x] = True
                    stack.append((next_y, next_x))
        result.append(component)
    return result


def _largest_component(mask: np.ndarray, *, minimum: int = 1) -> np.ndarray:
    components = _components(mask)
    if not components:
        return np.zeros_like(mask, dtype=bool)
    component = max(components, key=len)
    if len(component) < minimum:
        return np.zeros_like(mask, dtype=bool)
    result = np.zeros_like(mask, dtype=bool)
    for y, x in component:
        result[y, x] = True
    return result


def _remove_specks(mask: np.ndarray, minimum: int) -> np.ndarray:
    result = np.zeros_like(mask, dtype=bool)
    for component in _components(mask):
        if len(component) >= minimum:
            for y, x in component:
                result[y, x] = True
    return result


def _dilate(mask: np.ndarray, iterations: int) -> np.ndarray:
    result = mask.astype(bool, copy=True)
    for _ in range(max(0, int(iterations))):
        padded = np.pad(result, 1, constant_values=False)
        result = (
            padded[1:-1, 1:-1]
            | padded[:-2, 1:-1]
            | padded[2:, 1:-1]
            | padded[1:-1, :-2]
            | padded[1:-1, 2:]
        )
    return result


def _erode(mask: np.ndarray, iterations: int) -> np.ndarray:
    result = mask.astype(bool, copy=True)
    for _ in range(max(0, int(iterations))):
        padded = np.pad(result, 1, constant_values=False)
        result = (
            padded[1:-1, 1:-1]
            & padded[:-2, 1:-1]
            & padded[2:, 1:-1]
            & padded[1:-1, :-2]
            & padded[1:-1, 2:]
        )
    return result


def _close(mask: np.ndarray) -> np.ndarray:
    return _erode(_dilate(mask, 1), 1)


def _regularize_diagonal_contacts(mask: np.ndarray) -> np.ndarray:
    """Remove point-only voxel contacts that would create non-manifold edges.

    Raster curves can contain a 2x2 checkerboard where two occupied cells meet
    only at one corner. Extruding both cells makes four faces share one
    vertical edge. Filling the two missing cells changes the outline by at
    most one working-grid cell and turns the contact into a printable surface.
    """

    result = mask.astype(bool, copy=True)
    for _ in range(8):
        top_left = result[:-1, :-1]
        top_right = result[:-1, 1:]
        bottom_left = result[1:, :-1]
        bottom_right = result[1:, 1:]
        first = top_left & bottom_right & ~top_right & ~bottom_left
        second = top_right & bottom_left & ~top_left & ~bottom_right
        if not first.any() and not second.any():
            break
        ys, xs = np.where(first)
        result[ys, xs + 1] = True
        result[ys + 1, xs] = True
        ys, xs = np.where(second)
        result[ys, xs] = True
        result[ys + 1, xs + 1] = True
    return result


def _otsu_threshold(values: np.ndarray) -> int:
    histogram = np.bincount(values.ravel(), minlength=256).astype(np.float64)
    total = float(values.size)
    weighted_total = float(np.dot(np.arange(256), histogram))
    background_weight = 0.0
    background_sum = 0.0
    best_variance = -1.0
    best_threshold = 127
    for threshold in range(256):
        background_weight += histogram[threshold]
        if background_weight <= 0:
            continue
        foreground_weight = total - background_weight
        if foreground_weight <= 0:
            break
        background_sum += threshold * histogram[threshold]
        mean_background = background_sum / background_weight
        mean_foreground = (weighted_total - background_sum) / foreground_weight
        variance = background_weight * foreground_weight * (mean_background - mean_foreground) ** 2
        if variance > best_variance:
            best_variance = variance
            best_threshold = threshold
    return int(best_threshold)


def load_line_art_bytes(content: bytes) -> np.ndarray:
    if not content or len(content) > MAX_BYTES:
        raise CookieSTLError("input_file_invalid_or_too_large")
    try:
        with Image.open(io.BytesIO(content)) as image:
            image_format = str(image.format or "").upper()
            width, height = image.size
            if image_format not in ALLOWED_FORMATS:
                raise CookieSTLError("input_not_supported_image")
            if width <= 0 or height <= 0 or width * height > MAX_PIXELS:
                raise CookieSTLError("input_pixel_limit")
            rgba = image.convert("RGBA")
            white = Image.new("RGBA", rgba.size, (255, 255, 255, 255))
            grayscale = ImageOps.grayscale(Image.alpha_composite(white, rgba))
            longest = max(grayscale.size)
            # Always use the full bounded raster budget.  Previously images
            # between 256 and 512 px were left at their source resolution, so
            # the same 80 mm cutter could preserve visible pixel steps even
            # though the 512-side memory/deadline budget was already allowed.
            # LANCZOS upsampling supplies anti-aliased sub-pixel coverage; the
            # vector error gate still proves the resulting physical contour.
            working_longest = MAX_GRID_SIDE
            if longest != working_longest:
                ratio = working_longest / float(longest)
                working_size = tuple(max(1, int(round(side * ratio))) for side in grayscale.size)
                grayscale = grayscale.resize(working_size, Image.Resampling.LANCZOS)
            # LANCZOS leaves sub-pixel grey coverage around curved ink.  A
            # small blur before Otsu turns that coverage into an anti-aliased
            # binary decision instead of thresholding JPEG stair-steps.
            grayscale = ImageOps.autocontrast(grayscale).filter(ImageFilter.GaussianBlur(radius=0.55))
            pixels = np.asarray(grayscale, dtype=np.uint8)
    except CookieSTLError:
        raise
    except Exception as exc:
        raise CookieSTLError("input_not_supported_image") from exc

    threshold = _otsu_threshold(pixels)
    border = np.concatenate((pixels[0], pixels[-1], pixels[:, 0], pixels[:, -1]))
    dark_background = float(np.median(border)) < threshold
    lines = pixels > threshold if dark_background else pixels <= threshold
    lines = _close(lines)
    lines = _remove_specks(lines, max(4, int(lines.size * 0.0002)))
    if int(lines.sum()) < 12:
        raise CookieSTLError("no_usable_line_art")
    # A safely separable outer contour needs background on every source edge.
    # Besides catching clipped drawings, this rejects pathological very-thick
    # empty frames whose foreground/background polarity can otherwise invert
    # and turn the whole canvas into a false printable silhouette.
    if (
        lines[:2].any()
        or lines[-2:].any()
        or lines[:, :2].any()
        or lines[:, -2:].any()
    ):
        raise CookieSTLError("outer_contour_touches_image_edge")
    return np.pad(lines, 4, constant_values=False)


def _external_background(lines: np.ndarray) -> np.ndarray:
    height, width = lines.shape
    open_space = ~lines
    seen = np.zeros_like(lines, dtype=bool)
    stack: list[tuple[int, int]] = []
    for x in range(width):
        stack.extend(((0, x), (height - 1, x)))
    for y in range(height):
        stack.extend(((y, 0), (y, width - 1)))
    while stack:
        y, x = stack.pop()
        if not (0 <= y < height and 0 <= x < width) or seen[y, x] or not open_space[y, x]:
            continue
        seen[y, x] = True
        stack.extend(((y + 1, x), (y - 1, x), (y, x + 1), (y, x - 1)))
    return seen


def _separated_internal_linework(
    lines: np.ndarray,
    external_background: np.ndarray,
    silhouette: np.ndarray,
) -> np.ndarray:
    """Keep only line components separated from the exterior-facing frame.

    Counting every dark pixel inside an eroded silhouette is unsafe: a thick
    outer frame still has dark pixels on its inner edge and was therefore
    mistaken for stamp detail.  Any connected ink component touching the
    exterior background is part of the cutter outline, in its entirety; only
    components separated from it by white space may become optional relief.
    """
    exterior_neighbors = _dilate(external_background, 1)
    internal = np.zeros_like(lines, dtype=bool)
    outer_component_count = 0
    for component in _components(lines):
        if any(exterior_neighbors[y, x] for y, x in component):
            outer_component_count += 1
            continue
        for y, x in component:
            internal[y, x] = True
    if outer_component_count == 0:
        raise CookieSTLError("open_or_missing_outer_contour")

    internal &= _erode(silhouette, 2)
    internal = _remove_specks(
        internal,
        max(3, int(lines.size * 0.00002)),
    )
    return internal


def segment_line_art(content: bytes) -> tuple[np.ndarray, np.ndarray]:
    lines = load_line_art_bytes(content)
    external_background = _external_background(lines)
    enclosed = (~lines) & (~external_background)
    minimum_area = max(36, int(lines.size * 0.02))
    interior = _largest_component(enclosed, minimum=minimum_area)
    if not interior.any():
        raise CookieSTLError("open_or_missing_outer_contour")
    # `interior` proves that a substantial closed outer region exists.  Build
    # the actual silhouette from every enclosed cell plus its ink so closed
    # eyes/text remain internal detail instead of being mistaken for holes.
    silhouette = _largest_component(lines | enclosed, minimum=minimum_area)
    ys, xs = np.where(silhouette)
    if int(xs.max() - xs.min() + 1) < 8 or int(ys.max() - ys.min() + 1) < 8:
        raise CookieSTLError("outer_contour_too_small")
    internal_lines = _separated_internal_linework(
        lines,
        external_background,
        silhouette,
    )
    return _largest_component(silhouette, minimum=minimum_area), internal_lines


def inspect_line_art_bytes(content: bytes) -> dict[str, object]:
    """Run the production segmentation contract without retaining the image."""
    _silhouette, internal_lines = segment_line_art(content)
    return {
        "line_art_validated": True,
        "internal_feature_components": len(_components(internal_lines)),
        "generation_mode": (
            "cutter_and_stamp" if internal_lines.any() else "cutter_only"
        ),
    }


def _crop_masks(*masks: np.ndarray, margin: int = 2) -> tuple[np.ndarray, ...]:
    combined = np.zeros_like(masks[0], dtype=bool)
    for mask in masks:
        combined |= mask.astype(bool)
    ys, xs = np.where(combined)
    if not len(xs):
        raise CookieSTLError("empty_geometry")
    y0 = max(0, int(ys.min()) - margin)
    y1 = min(combined.shape[0], int(ys.max()) + margin + 1)
    x0 = max(0, int(xs.min()) - margin)
    x1 = min(combined.shape[1], int(xs.max()) + margin + 1)
    return tuple(mask[y0:y1, x0:x1] for mask in masks)


def _trace_outer_pixel_boundary(mask: np.ndarray) -> list[tuple[float, float]]:
    """Return the longest closed raster boundary without exposing image data.

    This is deliberately an edge trace rather than a contour of occupied cell
    centres: it represents the silhouette boundary, so subsequent vector
    simplification removes staircase edges instead of merely moving them.
    """
    edges: set[tuple[tuple[int, int], tuple[int, int]]] = set()
    height, width = mask.shape
    for y, x in zip(*np.where(mask)):
        y, x = int(y), int(x)
        for a, b, outside in (
            ((x, y), (x + 1, y), y == 0 or not mask[y - 1, x]),
            ((x + 1, y), (x + 1, y + 1), x == width - 1 or not mask[y, x + 1]),
            ((x + 1, y + 1), (x, y + 1), y == height - 1 or not mask[y + 1, x]),
            ((x, y + 1), (x, y), x == 0 or not mask[y, x - 1]),
        ):
            if outside:
                edges.add(tuple(sorted((a, b))))
    neighbors: dict[tuple[int, int], list[tuple[int, int]]] = {}
    for a, b in edges:
        neighbors.setdefault(a, []).append(b)
        neighbors.setdefault(b, []).append(a)
    if not edges or any(len(items) != 2 for items in neighbors.values()):
        raise CookieSTLError("open_or_missing_outer_contour")
    remaining = set(edges)
    loops: list[list[tuple[int, int]]] = []
    while remaining:
        start_edge = next(iter(remaining))
        start, current = start_edge
        previous = start
        loop = [start]
        while True:
            loop.append(current)
            edge = tuple(sorted((previous, current)))
            remaining.discard(edge)
            choices = [point for point in neighbors[current] if point != previous]
            if not choices:
                raise CookieSTLError("open_or_missing_outer_contour")
            following = choices[0]
            if following == start:
                break
            previous, current = current, following
            if len(loop) > len(edges) + 2:
                raise CookieSTLError("open_or_missing_outer_contour")
        if len(loop) >= 4:
            loops.append(loop)
    if not loops:
        raise CookieSTLError("open_or_missing_outer_contour")
    def _area(loop: list[tuple[int, int]]) -> float:
        return abs(sum(a[0] * b[1] - b[0] * a[1] for a, b in zip(loop, loop[1:] + loop[:1])))
    chosen = max(loops, key=_area)
    return [(float(x), float(y)) for x, y in chosen]


def _perpendicular_distance(point, start, end) -> float:
    dx, dy = end[0] - start[0], end[1] - start[1]
    if dx == 0.0 and dy == 0.0:
        return math.dist(point, start)
    return abs(dx * (start[1] - point[1]) - (start[0] - point[0]) * dy) / math.hypot(dx, dy)


def _rdp(points: list[tuple[float, float]], tolerance: float) -> list[tuple[float, float]]:
    if len(points) <= 2:
        return points
    distance, index = max(
        (_perpendicular_distance(point, points[0], points[-1]), position)
        for position, point in enumerate(points[1:-1], start=1)
    )
    if distance <= tolerance:
        return [points[0], points[-1]]
    return _rdp(points[: index + 1], tolerance)[:-1] + _rdp(points[index:], tolerance)


def _simplify_closed_contour(points: list[tuple[float, float]], tolerance: float) -> list[tuple[float, float]]:
    if len(points) < 8:
        raise CookieSTLError("outer_contour_too_small")
    # Split at an approximately diametric pair to avoid an arbitrary seam
    # turning a smooth circular arc into a long chord.
    anchor = points[0]
    opposite = max(range(1, len(points)), key=lambda index: math.dist(anchor, points[index]))
    first = _rdp(points[: opposite + 1], tolerance)
    second = _rdp(points[opposite:] + [points[0]], tolerance)
    contour = first[:-1] + second[:-1]
    if len(contour) < 4:
        raise CookieSTLError("outer_contour_too_small")
    return contour


def _iter_polygons(geometry):
    if geometry.is_empty:
        return []
    if geometry.geom_type == "Polygon":
        return [geometry]
    if geometry.geom_type == "MultiPolygon":
        return list(geometry.geoms)
    return [item for item in getattr(geometry, "geoms", ()) if item.geom_type == "Polygon"]


def _mask_pixel_geometry(mask: np.ndarray):
    """Polygonize occupied raster cells without constructing one box per cell."""
    if Polygon is None or not mask.any():
        raise CookieSTLError("empty_geometry")
    segments: list[tuple[tuple[float, float], tuple[float, float]]] = []
    height, width = mask.shape
    for raw_y, raw_x in zip(*np.where(mask)):
        y, x = int(raw_y), int(raw_x)
        for start, end, outside in (
            ((x, y), (x + 1, y), y == 0 or not mask[y - 1, x]),
            ((x + 1, y), (x + 1, y + 1), x == width - 1 or not mask[y, x + 1]),
            ((x + 1, y + 1), (x, y + 1), y == height - 1 or not mask[y + 1, x]),
            ((x, y + 1), (x, y), x == 0 or not mask[y, x - 1]),
        ):
            if outside:
                segments.append((start, end))
    if not segments:
        raise CookieSTLError("empty_geometry")
    if len(segments) > MAX_BOUNDARY_SEGMENTS:
        raise CookieSTLError("resource_limit_exceeded")
    accepted = []
    for face in polygonize(MultiLineString(segments)):
        point = face.representative_point()
        x = min(width - 1, max(0, int(math.floor(point.x))))
        y = min(height - 1, max(0, int(math.floor(point.y))))
        if mask[y, x]:
            accepted.append(face)
    geometry = unary_union(accepted) if accepted else Polygon()
    if geometry.is_empty or not geometry.is_valid or geometry.area <= 1e-8:
        raise CookieSTLError("line_art_geometry_incomplete")
    return geometry


def _topology_signature(geometry) -> tuple[int, int]:
    polygons = _iter_polygons(geometry)
    return len(polygons), sum(len(polygon.interiors) for polygon in polygons)


def _physical_mask_geometry(
    mask: np.ndarray,
    scale_mm: float,
    *,
    mirror: bool = False,
    max_error_mm: float = MAX_CONTOUR_ERROR_MM,
):
    """Return a rounded sub-pixel vector contour with a measured error gate."""
    raw = _mask_pixel_geometry(mask)
    raw = affinity.scale(raw, xfact=scale_mm, yfact=scale_mm, origin=(0.0, 0.0))
    if mirror:
        raw = affinity.scale(
            raw,
            xfact=-1.0,
            yfact=1.0,
            origin=(mask.shape[1] * scale_mm / 2.0, 0.0),
        )
    raw = set_precision(raw, GEOMETRY_PRECISION_MM)
    expected_topology = _topology_signature(raw)
    # A small round-trip buffer introduces genuine sub-pixel arcs.  Douglas-
    # Peucker then removes collinear pixel teeth.  Both are reduced until the
    # symmetric physical error is proven, never replaced by a one-pixel floor.
    smoothing_mm = min(0.10, max(0.025, scale_mm * 0.35))
    simplify_mm = min(0.30, max(0.08, scale_mm * 0.90))
    for _ in range(8):
        parts = []
        for polygon in _iter_polygons(raw):
            candidate = polygon.simplify(simplify_mm, preserve_topology=True)
            candidate = candidate.buffer(smoothing_mm, quad_segs=4, join_style=1)
            candidate = candidate.buffer(-smoothing_mm, quad_segs=4, join_style=1)
            candidate = candidate.simplify(simplify_mm, preserve_topology=True)
            if not candidate.is_empty:
                parts.append(candidate)
        candidate = unary_union(parts) if parts else Polygon()
        candidate = set_precision(candidate, GEOMETRY_PRECISION_MM)
        if (
            not candidate.is_empty
            and candidate.is_valid
            and _topology_signature(candidate) == expected_topology
            and candidate.area > 1e-8
        ):
            error_mm = float(raw.boundary.hausdorff_distance(candidate.boundary))
            if math.isfinite(error_mm) and error_mm <= max_error_mm + 1e-9:
                return candidate, {
                    "hausdorff_mm": error_mm,
                    "raw_vertices": sum(
                        len(p.exterior.coords) + sum(len(r.coords) for r in p.interiors)
                        for p in _iter_polygons(raw)
                    ),
                    "vector_vertices": sum(
                        len(p.exterior.coords) + sum(len(r.coords) for r in p.interiors)
                        for p in _iter_polygons(candidate)
                    ),
                }
        smoothing_mm *= 0.5
        simplify_mm *= 0.5
    raise CookieSTLError("contour_quality_failed")


def _surface_triangles(geometry, z: float, *, upward: bool):
    triangles: list[tuple[tuple[float, float, float], ...]] = []
    for polygon in _iter_polygons(geometry):
        polygon = _orient_polygon(polygon, sign=1.0)
        faces = constrained_delaunay_triangles(polygon)
        for face in getattr(faces, "geoms", ()):
            if face.geom_type != "Polygon" or not (
                face.covered_by(polygon)
                or face.covered_by(polygon.buffer(GEOMETRY_PRECISION_MM * 2.0))
            ):
                continue
            coords = list(face.exterior.coords)[:-1]
            if len(coords) != 3:
                continue
            points = [(float(x), float(y), float(z)) for x, y in coords]
            cross_z = (
                (points[1][0] - points[0][0]) * (points[2][1] - points[0][1])
                - (points[1][1] - points[0][1]) * (points[2][0] - points[0][0])
            )
            if (cross_z > 0) != upward:
                points[1], points[2] = points[2], points[1]
            triangles.append(tuple(points))
    return triangles


def _side_triangles(geometry, lower_z: float, upper_z: float):
    triangles: list[tuple[tuple[float, float, float], ...]] = []
    for raw_polygon in _iter_polygons(geometry):
        polygon = _orient_polygon(raw_polygon, sign=1.0)
        for ring in (polygon.exterior, *polygon.interiors):
            coords = [(float(x), float(y)) for x, y in list(ring.coords)[:-1]]
            for start, end in zip(coords, coords[1:] + coords[:1]):
                a0 = (start[0], start[1], float(lower_z))
                b0 = (end[0], end[1], float(lower_z))
                a1 = (start[0], start[1], float(upper_z))
                b1 = (end[0], end[1], float(upper_z))
                triangles.extend(((a0, b0, b1), (a0, b1, a1)))
    return triangles


def _layered_mesh(base, raised, base_height: float, raised_height: float):
    """Build one manifold solid; the shared interface is never double-capped."""
    base = set_precision(
        base.simplify(SURFACE_CANONICAL_TOLERANCE_MM, preserve_topology=True),
        GEOMETRY_PRECISION_MM,
    )
    raised = set_precision(
        raised.intersection(base).simplify(
            SURFACE_CANONICAL_TOLERANCE_MM, preserve_topology=True
        ),
        GEOMETRY_PRECISION_MM,
    )
    if base.is_empty or raised.is_empty or not base.is_valid or not raised.is_valid:
        raise CookieSTLError("line_art_geometry_incomplete")
    # Polygonize and triangulate the complete arrangement once.  Reusing this
    # one planar triangulation for the bottom, both top levels, and both side
    # bands guarantees identical edge subdivision at every shared interface.
    # Triangulating ``base.difference(raised)`` separately is not sufficient:
    # GEOS may remove a collinear node on one face but retain it on the other.
    arrangement = unary_union((base.boundary, raised.boundary))
    arrangement_faces = []
    for face in polygonize(arrangement):
        point = face.representative_point()
        if point.covered_by(base):
            arrangement_faces.append(set_precision(face, GEOMETRY_PRECISION_MM))
    if not arrangement_faces:
        raise CookieSTLError("line_art_geometry_incomplete")
    planar = constrained_delaunay_triangles(GeometryCollection(arrangement_faces))
    planar_triangles: list[tuple[tuple[float, float], ...]] = []
    raised_flags: list[bool] = []
    for face in getattr(planar, "geoms", ()):
        if face.geom_type != "Polygon":
            continue
        point = face.representative_point()
        if not point.covered_by(base.buffer(GEOMETRY_PRECISION_MM * 2.0)):
            continue
        coords = [(float(x), float(y)) for x, y in list(face.exterior.coords)[:-1]]
        if len(coords) != 3:
            continue
        cross_z = (
            (coords[1][0] - coords[0][0]) * (coords[2][1] - coords[0][1])
            - (coords[1][1] - coords[0][1]) * (coords[2][0] - coords[0][0])
        )
        if cross_z < 0:
            coords[1], coords[2] = coords[2], coords[1]
        is_raised = point.covered_by(raised)
        overlap = float(face.intersection(raised).area)
        crossing_area = min(overlap, abs(float(face.area) - overlap))
        if crossing_area > max(1e-5, float(face.area) * 1e-4):
            raise CookieSTLError("planar_arrangement_crossing")
        planar_triangles.append(tuple(coords))
        raised_flags.append(is_raised)
    if not planar_triangles or not any(raised_flags):
        raise CookieSTLError("line_art_geometry_incomplete")

    all_edges: dict[
        tuple[tuple[float, float], tuple[float, float]],
        list[tuple[tuple[float, float], tuple[float, float]]],
    ] = {}
    raised_edges: dict[
        tuple[tuple[float, float], tuple[float, float]],
        list[tuple[tuple[float, float], tuple[float, float]]],
    ] = {}
    triangles: list[tuple[tuple[float, float, float], ...]] = []
    for points, is_raised in zip(planar_triangles, raised_flags):
        bottom = tuple((x, y, 0.0) for x, y in reversed(points))
        top_z = raised_height if is_raised else base_height
        top = tuple((x, y, float(top_z)) for x, y in points)
        triangles.extend((bottom, top))
        for start, end in (
            (points[0], points[1]),
            (points[1], points[2]),
            (points[2], points[0]),
        ):
            canonical_start = tuple(round(value, 7) for value in start)
            canonical_end = tuple(round(value, 7) for value in end)
            key = tuple(sorted((canonical_start, canonical_end)))
            all_edges.setdefault(key, []).append((start, end))
            if is_raised:
                raised_edges.setdefault(key, []).append((start, end))

    def add_band(edge_map, lower_z: float, upper_z: float) -> None:
        for occurrences in edge_map.values():
            if len(occurrences) == 2:
                continue
            if len(occurrences) != 1:
                raise CookieSTLError("non_manifold_planar_arrangement")
            start, end = occurrences[0]
            a0 = (start[0], start[1], float(lower_z))
            b0 = (end[0], end[1], float(lower_z))
            a1 = (start[0], start[1], float(upper_z))
            b1 = (end[0], end[1], float(upper_z))
            triangles.extend(((a0, b0, b1), (a0, b1, a1)))

    add_band(all_edges, 0.0, base_height)
    add_band(raised_edges, base_height, raised_height)
    if not triangles:
        raise CookieSTLError("empty_mesh")
    if len(triangles) > MAX_TRIANGLES_PER_MESH:
        raise CookieSTLError("resource_limit_exceeded")
    if mesh_signed_volume(triangles) < 0:
        triangles = [(a, c, b) for a, b, c in triangles]
    validate_watertight(triangles)
    return triangles


def _minimum_polygon_feature(geometry) -> float:
    # A single erosion is a conservative physical gate: every disconnected
    # relief component must retain printable area after removing half the
    # minimum feature from every boundary.  Binary-searching an exact width is
    # prohibitively expensive for hundreds of glyph/line components and adds
    # no safety beyond this pass/fail contract.
    for polygon in _iter_polygons(geometry):
        if polygon.buffer(-MIN_RELIEF_FEATURE_MM / 2.0).is_empty:
            return 0.0
    return MIN_RELIEF_FEATURE_MM if _iter_polygons(geometry) else 0.0


def _vector_cutter_mesh(silhouette: np.ndarray, scale_mm: float, params: CookieParameters):
    """Extrude a blade above a lower grip flange as one manifold solid."""
    if Polygon is None:
        raise CookieSTLError("vector_geometry_unavailable")
    outline, contour = _physical_mask_geometry(silhouette, scale_mm)
    half_wall = params.blade_wall_mm / 2.0
    inner = outline.buffer(-half_wall, quad_segs=3, join_style=1).simplify(
        0.08, preserve_topology=True
    )
    blade_outer = outline.buffer(half_wall, quad_segs=3, join_style=1).simplify(
        0.08, preserve_topology=True
    )
    grip_outer = outline.buffer(
        half_wall + params.rim_mm, quad_segs=3, join_style=1
    ).simplify(0.08, preserve_topology=True)
    blade = blade_outer.difference(inner)
    grip = grip_outer.difference(inner)
    blade = set_precision(blade, GEOMETRY_PRECISION_MM)
    grip = set_precision(grip, GEOMETRY_PRECISION_MM)
    blade_polygons = _iter_polygons(blade)
    if (
        blade.is_empty
        or grip.is_empty
        or not blade.is_valid
        or not grip.is_valid
        or not grip.buffer(GEOMETRY_PRECISION_MM).covers(blade)
        # A printable cutter edge is one annular wall: one connected outer
        # shell and one connected inner shell.  A merely watertight union can
        # still contain several individually closed wall fragments, which a
        # slicer will correctly show as breaks in the intended cutting edge.
        or len(blade_polygons) != 1
        or len(blade_polygons[0].interiors) != 1
        or not blade_polygons[0].exterior.is_ring
        or not blade_polygons[0].interiors[0].is_ring
    ):
        raise CookieSTLError("cutter_wall_not_continuous")
    grip_height = min(3.2, max(2.0, params.blade_height_mm * 0.24))
    triangles = _layered_mesh(grip, blade, grip_height, params.blade_height_mm)
    bounds = grip.bounds
    envelope = max(bounds[2] - bounds[0], bounds[3] - bounds[1])
    if envelope > params.width_mm + 0.05:
        raise CookieSTLError("finished_envelope_exceeded")
    return triangles, outline, {
        "vertices": int(contour["vector_vertices"]),
        "contour_hausdorff_mm": float(contour["hausdorff_mm"]),
        "blade_area_mm2": float(blade.area),
        "grip_area_mm2": float(grip.area),
        "finished_envelope_mm": float(envelope),
        "grip_height_mm": float(grip_height),
    }


def _heightfield_mesh(heights: np.ndarray, scale_mm: float, *, mirror: bool = False):
    field = np.fliplr(heights) if mirror else heights
    height, width = field.shape
    levels = [0.0] + sorted({float(value) for value in field.ravel() if value > 0})
    triangles: list[tuple[tuple[float, float, float], ...]] = []
    for raw_y, raw_x in zip(*np.where(field > 0)):
        y, x = int(raw_y), int(raw_x)
        top = float(field[y, x])
        x0, x1 = x * scale_mm, (x + 1) * scale_mm
        y0, y1 = (height - y - 1) * scale_mm, (height - y) * scale_mm
        bottom = ((x0, y0, 0.0), (x1, y0, 0.0), (x1, y1, 0.0), (x0, y1, 0.0))
        upper = ((x0, y0, top), (x1, y0, top), (x1, y1, top), (x0, y1, top))
        triangles.extend(((bottom[0], bottom[2], bottom[1]), (bottom[0], bottom[3], bottom[2])))
        triangles.extend(((upper[0], upper[1], upper[2]), (upper[0], upper[2], upper[3])))
        side_specs = (
            (0, -1, (x0, y1), (x0, y0)),
            (0, 1, (x1, y0), (x1, y1)),
            (1, 0, (x0, y0), (x1, y0)),
            (-1, 0, (x1, y1), (x0, y1)),
        )
        for low, high in zip(levels, levels[1:]):
            if top < high:
                continue
            for dy, dx, start, end in side_specs:
                neighbor_y, neighbor_x = y + dy, x + dx
                neighbor = (
                    float(field[neighbor_y, neighbor_x])
                    if 0 <= neighbor_y < height and 0 <= neighbor_x < width
                    else 0.0
                )
                if neighbor >= high:
                    continue
                a = (start[0], start[1], low)
                b = (end[0], end[1], low)
                c = (end[0], end[1], high)
                d = (start[0], start[1], high)
                triangles.extend(((a, b, c), (a, c, d)))
    if not triangles:
        raise CookieSTLError("empty_mesh")
    return triangles


def _canonical_vertex(vertex: tuple[float, float, float]) -> tuple[float, float, float]:
    return tuple(round(float(value), 7) for value in vertex)


def validate_watertight(triangles) -> None:
    edges: dict[tuple[tuple[float, float, float], tuple[float, float, float]], int] = {}
    edge_orientation: dict[
        tuple[tuple[float, float, float], tuple[float, float, float]], int
    ] = {}
    signed_volume = 0.0
    triangle_edges: list[
        tuple[
            tuple[tuple[float, float, float], tuple[float, float, float]],
            ...,
        ]
    ] = []
    edge_triangles: dict[
        tuple[tuple[float, float, float], tuple[float, float, float]],
        list[int],
    ] = {}
    for triangle_index, triangle in enumerate(triangles):
        a, b, c = (np.asarray(vertex, dtype=np.float64) for vertex in triangle)
        if float(np.linalg.norm(np.cross(b - a, c - a))) <= 1e-10:
            raise CookieSTLError("degenerate_mesh")
        signed_volume += float(np.dot(a, np.cross(b, c))) / 6.0
        vertices = tuple(_canonical_vertex(vertex) for vertex in triangle)
        current_edges = []
        for start, end in ((vertices[0], vertices[1]), (vertices[1], vertices[2]), (vertices[2], vertices[0])):
            edge = tuple(sorted((start, end)))
            current_edges.append(edge)
            edges[edge] = edges.get(edge, 0) + 1
            direction = 1 if start == edge[0] else -1
            edge_orientation[edge] = edge_orientation.get(edge, 0) + direction
            edge_triangles.setdefault(edge, []).append(triangle_index)
        triangle_edges.append(tuple(current_edges))
    if (
        not edges
        or any(count != 2 for count in edges.values())
        or any(direction != 0 for direction in edge_orientation.values())
    ):
        raise CookieSTLError("non_watertight_mesh")
    # Two independently closed shells also satisfy the classic two-faces-per-
    # edge test.  Cookie cutters and their stamp plates must instead be one
    # connected printable solid, so prove triangle connectivity explicitly.
    pending = [0] if triangle_edges else []
    visited: set[int] = set()
    while pending:
        triangle_index = pending.pop()
        if triangle_index in visited:
            continue
        visited.add(triangle_index)
        for edge in triangle_edges[triangle_index]:
            pending.extend(edge_triangles[edge])
    if len(visited) != len(triangle_edges):
        raise CookieSTLError("disconnected_mesh")
    if signed_volume <= 1e-8:
        raise CookieSTLError("inverted_or_empty_mesh")


def _top_surface_continuity(triangles, *, cutter_ring: bool) -> dict[str, int]:
    """Prove that every maximum-height feature has closed boundary loops."""
    if not triangles:
        raise CookieSTLError("empty_mesh")
    maximum_z = max(float(vertex[2]) for triangle in triangles for vertex in triangle)
    top_triangles = []
    for triangle in triangles:
        if all(abs(float(vertex[2]) - maximum_z) <= GEOMETRY_PRECISION_MM for vertex in triangle):
            top_triangles.append(
                tuple(
                    (round(float(vertex[0]), 7), round(float(vertex[1]), 7))
                    for vertex in triangle
                )
            )
    if not top_triangles:
        raise CookieSTLError("top_surface_missing")
    edge_faces: dict[
        tuple[tuple[float, float], tuple[float, float]], list[int]
    ] = {}
    for index, triangle in enumerate(top_triangles):
        for start, end in (
            (triangle[0], triangle[1]),
            (triangle[1], triangle[2]),
            (triangle[2], triangle[0]),
        ):
            if start == end:
                raise CookieSTLError("degenerate_mesh")
            edge_faces.setdefault(tuple(sorted((start, end))), []).append(index)
    if any(len(faces) not in {1, 2} for faces in edge_faces.values()):
        raise CookieSTLError("top_surface_non_manifold")

    face_neighbors: dict[int, set[int]] = {
        index: set() for index in range(len(top_triangles))
    }
    boundary_neighbors: dict[tuple[float, float], set[tuple[float, float]]] = {}
    for edge, faces in edge_faces.items():
        if len(faces) == 2:
            face_neighbors[faces[0]].add(faces[1])
            face_neighbors[faces[1]].add(faces[0])
        else:
            start, end = edge
            boundary_neighbors.setdefault(start, set()).add(end)
            boundary_neighbors.setdefault(end, set()).add(start)
    if not boundary_neighbors or any(len(items) != 2 for items in boundary_neighbors.values()):
        raise CookieSTLError("top_surface_boundary_open")

    def component_count(neighbors) -> int:
        unseen = set(neighbors)
        count = 0
        while unseen:
            count += 1
            stack = [unseen.pop()]
            while stack:
                current = stack.pop()
                for neighbor in neighbors[current]:
                    if neighbor in unseen:
                        unseen.remove(neighbor)
                        stack.append(neighbor)
        return count

    surface_components = component_count(face_neighbors)
    boundary_loops = component_count(boundary_neighbors)
    if cutter_ring and (surface_components != 1 or boundary_loops != 2):
        raise CookieSTLError("cutter_wall_not_continuous")
    return {
        "top_surface_components": surface_components,
        "top_boundary_loops": boundary_loops,
    }


def mesh_signed_volume(triangles) -> float:
    """Return the signed volume used by independent geometry regressions."""

    return sum(
        float(
            np.dot(
                np.asarray(triangle[0], dtype=np.float64),
                np.cross(
                    np.asarray(triangle[1], dtype=np.float64),
                    np.asarray(triangle[2], dtype=np.float64),
                ),
            )
        )
        / 6.0
        for triangle in triangles
    )


def _binary_stl(triangles, label: str) -> bytes:
    validate_watertight(triangles)
    # STL vertices are float32.  Revalidate those exact serialized positions;
    # otherwise two close float64 vertices can collapse during encoding and
    # create a real slicer-visible crack after the pre-serialization check.
    serialized_triangles = [
        tuple(
            tuple(float(np.float32(value)) for value in vertex)
            for vertex in triangle
        )
        for triangle in triangles
    ]
    validate_watertight(serialized_triangles)
    header = (f"MAGI {label} watertight STL").encode("ascii")[:80].ljust(80, b"\0")
    output = io.BytesIO()
    output.write(header)
    output.write(struct.pack("<I", len(triangles)))
    for triangle in serialized_triangles:
        # STL stores float32 vertices.  Compute the normal from those exact
        # serialized coordinates, not the higher-precision construction
        # points, so small valid constrained-Delaunay faces cannot carry a
        # stale normal after float32 rounding.
        vertices = [
            np.asarray(vertex, dtype=np.float32).astype(np.float64)
            for vertex in triangle
        ]
        normal = np.cross(vertices[1] - vertices[0], vertices[2] - vertices[0])
        length = float(np.linalg.norm(normal))
        if length <= 1e-10:
            raise CookieSTLError("degenerate_mesh")
        normal = normal / length
        output.write(struct.pack("<12fH", *normal, *vertices[0], *vertices[1], *vertices[2], 0))
    return output.getvalue()


def _preview_png(grip: np.ndarray, ring: np.ndarray, plate: np.ndarray, relief: np.ndarray) -> bytes:
    height, width = grip.shape
    preview = np.full((height, width, 3), 250, dtype=np.uint8)
    preview[plate] = (219, 234, 254)
    preview[grip] = (251, 191, 36)
    preview[ring] = (31, 41, 55)
    preview[relief] = (37, 99, 235)
    image = Image.fromarray(preview, mode="RGB")
    image = image.resize((min(768, width * 4), min(768, height * 4)), Image.Resampling.NEAREST)
    output = io.BytesIO()
    image.save(output, format="PNG", optimize=True)
    return output.getvalue()


def generate_zip_bytes(content: bytes, parameters: CookieParameters | None = None) -> tuple[bytes, dict[str, object]]:
    deadline = time.monotonic() + MAX_GENERATION_SECONDS
    params = (parameters or CookieParameters()).validate()
    silhouette, internal_lines = segment_line_art(content)
    _check_deadline(deadline)
    ys, xs = np.where(silhouette)
    preliminary_scale = params.width_mm / max(int(xs.max() - xs.min() + 1), int(ys.max() - ys.min() + 1))
    wall_pixels = max(1, int(round(params.blade_wall_mm / preliminary_scale)))
    rim_pixels = max(0, int(round(params.rim_mm / preliminary_scale)))
    clearance_pixels = max(0, int(round(params.clearance_mm / preliminary_scale)))
    relief_pixels = max(1, int(round(0.55 / preliminary_scale)))

    ring = silhouette & (~_erode(silhouette, wall_pixels))
    ring = _regularize_diagonal_contacts(_largest_component(ring, minimum=12))
    grip = _regularize_diagonal_contacts(_dilate(ring, rim_pixels))
    stamp_generated = bool(internal_lines.any())
    if stamp_generated:
        plate = _regularize_diagonal_contacts(
            _largest_component(_erode(silhouette, clearance_pixels), minimum=24)
        )
        relief = _regularize_diagonal_contacts(
            _dilate(internal_lines, relief_pixels) & plate
        )
        # A one-cell contour repair must remain supported by the stamp body.
        plate |= relief
    else:
        plate = np.zeros_like(silhouette, dtype=bool)
        relief = np.zeros_like(silhouette, dtype=bool)
    if not ring.any() or not grip.any() or (stamp_generated and (not plate.any() or not relief.any())):
        raise CookieSTLError("line_art_geometry_incomplete")

    grip, ring, plate, relief, silhouette = _crop_masks(
        grip, ring, plate, relief, silhouette, margin=2
    )
    ys, xs = np.where(silhouette)
    silhouette_span = max(
        int(xs.max() - xs.min() + 1),
        int(ys.max() - ys.min() + 1),
    )
    usable_outline_mm = params.width_mm - params.blade_wall_mm - 2.0 * params.rim_mm
    if usable_outline_mm < max(4.0, params.blade_wall_mm * 4.0):
        raise CookieSTLError("invalid_dimensions")
    cutter_scale = usable_outline_mm / silhouette_span
    cutter_triangles, outline, cutter_vector = _vector_cutter_mesh(
        silhouette, cutter_scale, params
    )
    cutter_surface = _top_surface_continuity(cutter_triangles, cutter_ring=True)
    _check_deadline(deadline)

    stamp_triangles = []
    relief_feature_mm = 0.0
    relief_vector = {"hausdorff_mm": 0.0, "vector_vertices": 0}
    if stamp_generated:
        # The stamp is a two-level boundary arrangement, not a grid of voxels.
        # Plate and relief share the exact GEOS boundary at z=stamp_base_mm;
        # the interface is emitted once so no coincident caps are hidden.
        plate_geometry = outline.buffer(-params.clearance_mm, quad_segs=8, join_style=1)
        relief_geometry, relief_vector = _physical_mask_geometry(
            relief, cutter_scale, max_error_mm=MAX_CONTOUR_ERROR_MM
        )
        _check_deadline(deadline)
        relief_area_before_clip = float(relief_geometry.area)
        relief_geometry = set_precision(
            relief_geometry.intersection(plate_geometry), GEOMETRY_PRECISION_MM
        )
        if (
            plate_geometry.is_empty
            or relief_geometry.is_empty
            or not plate_geometry.is_valid
            or not relief_geometry.is_valid
            or relief_area_before_clip <= 0
            or relief_geometry.area / relief_area_before_clip < 0.95
        ):
            raise CookieSTLError("line_art_geometry_incomplete")
        relief_feature_mm = _minimum_polygon_feature(relief_geometry)
        if relief_feature_mm + 1e-6 < MIN_RELIEF_FEATURE_MM:
            raise CookieSTLError("feature_too_small")
        mirror_origin_x = silhouette.shape[1] * cutter_scale / 2.0
        plate_geometry = set_precision(
            affinity.scale(
                plate_geometry,
                xfact=-1.0,
                yfact=1.0,
                origin=(mirror_origin_x, 0.0),
            ),
            GEOMETRY_PRECISION_MM,
        )
        relief_geometry = set_precision(
            affinity.scale(
                relief_geometry,
                xfact=-1.0,
                yfact=1.0,
                origin=(mirror_origin_x, 0.0),
            ),
            GEOMETRY_PRECISION_MM,
        )
        stamp_triangles = _layered_mesh(
            plate_geometry,
            relief_geometry,
            params.stamp_base_mm,
            params.stamp_base_mm + params.relief_mm,
        )
        stamp_surface = _top_surface_continuity(
            stamp_triangles, cutter_ring=False
        )
        _check_deadline(deadline)
    else:
        stamp_surface = {"top_surface_components": 0, "top_boundary_loops": 0}
    cutter_stl = _binary_stl(cutter_triangles, "cutter")
    stamp_stl = (
        _binary_stl(stamp_triangles, "mirrored stamp")
        if stamp_generated
        else None
    )
    preview = _preview_png(grip, ring, plate, relief)
    summary: dict[str, object] = {
        "schema": "magi.cookie-cutter-stl/v1",
        "mode": "cutter_and_stamp" if stamp_generated else "cutter_only",
        "offline": True,
        "upload_persisted": False,
        "stamp_generated": stamp_generated,
        "mirrored_stamp": stamp_generated,
        "watertight": True,
        "input_sha256": hashlib.sha256(content).hexdigest(),
        "grid": {"width": int(grip.shape[1]), "height": int(grip.shape[0])},
        "parameters": {name: float(getattr(params, name)) for name in params.__dataclass_fields__},
        "geometry": {
            "cutter_ring_cells": int(ring.sum()),
            "grip_cells": int(grip.sum()),
            "stamp_plate_cells": int(plate.sum()) if stamp_generated else 0,
            "relief_cells": int(relief.sum()),
            "cutter_triangles": len(cutter_triangles),
            "stamp_triangles": len(stamp_triangles),
            "cutter_vector_vertices": int(cutter_vector["vertices"]),
            "cutter_wall_area_mm2": round(float(cutter_vector["blade_area_mm2"]), 4),
            "cutter_grip_area_mm2": round(float(cutter_vector["grip_area_mm2"]), 4),
            "cutter_finished_envelope_mm": round(
                float(cutter_vector["finished_envelope_mm"]), 6
            ),
            "cutter_contour_hausdorff_mm": round(
                float(cutter_vector["contour_hausdorff_mm"]), 6
            ),
            "cutter_top_surface_components": int(
                cutter_surface["top_surface_components"]
            ),
            "cutter_top_boundary_loops": int(
                cutter_surface["top_boundary_loops"]
            ),
            "stamp_contour_hausdorff_mm": round(
                float(relief_vector["hausdorff_mm"]), 6
            ),
            "stamp_relief_min_feature_mm": (
                round(float(relief_feature_mm), 6) if stamp_generated else None
            ),
            "stamp_vector_vertices": int(relief_vector["vector_vertices"]),
            "stamp_top_surface_components": int(
                stamp_surface["top_surface_components"]
            ),
            "stamp_top_boundary_loops": int(
                stamp_surface["top_boundary_loops"]
            ),
        },
    }
    readme = "MAGI 餅乾切模\ncutter.stl：光滑封閉外框切模，含握邊。\n"
    if stamp_generated:
        readme += "stamp_mirrored.stl：壓模底板與內部凸紋，已鏡像。\n"
    else:
        readme += "原圖沒有獨立內部圖案，因此本次只產生切模。\n"
    readme += "列印前請在切片軟體確認尺寸、封閉網格與器材安全。\n"
    bundle = io.BytesIO()
    with zipfile.ZipFile(bundle, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("cutter.stl", cutter_stl)
        if stamp_stl is not None:
            archive.writestr("stamp_mirrored.stl", stamp_stl)
        archive.writestr("segmentation_preview.png", preview)
        archive.writestr("parameters.json", json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True))
        archive.writestr("README.txt", readme)
    return bundle.getvalue(), summary
