from __future__ import annotations

import io
import hashlib
import json
import struct
import zipfile

import numpy as np
import pytest
from PIL import Image, ImageDraw
from shapely import affinity
from shapely.geometry import MultiPolygon, Polygon

from skills.cookie_stl import engine
from skills.cookie_stl.engine import (
    CookieParameters,
    CookieSTLError,
    _binary_stl,
    _heightfield_mesh,
    _layered_mesh,
    _mask_pixel_geometry,
    _minimum_polygon_feature,
    _physical_mask_geometry,
    generate_zip_bytes,
    inspect_line_art_bytes,
    mesh_signed_volume,
    segment_line_art,
    validate_watertight,
)


def _face(*, dark_background: bool = False, open_outline: bool = False) -> bytes:
    background, ink = (0, 255) if dark_background else (255, 0)
    image = Image.new("L", (96, 80), background)
    draw = ImageDraw.Draw(image)
    if open_outline:
        draw.line((10, 10, 85, 10), fill=ink, width=3)
        draw.line((10, 10, 10, 68), fill=ink, width=3)
        draw.line((10, 68, 70, 68), fill=ink, width=3)
    else:
        draw.rounded_rectangle((8, 8, 87, 71), radius=18, outline=ink, width=3)
    draw.ellipse((26, 28, 32, 34), fill=ink)
    draw.ellipse((62, 28, 68, 34), fill=ink)
    draw.arc((31, 32, 64, 57), 10, 170, fill=ink, width=3)
    output = io.BytesIO()
    image.save(output, format="PNG")
    return output.getvalue()


def _rounded_face() -> bytes:
    image = Image.new("L", (120, 100), 255)
    draw = ImageDraw.Draw(image)
    draw.rounded_rectangle((8, 8, 111, 91), radius=20, outline=0, width=4)
    draw.ellipse((34, 35, 42, 43), fill=0)
    draw.ellipse((77, 35, 85, 43), fill=0)
    draw.arc((38, 43, 82, 74), 0, 180, fill=0, width=4)
    output = io.BytesIO()
    image.save(output, format="PNG")
    return output.getvalue()


def _closed_details() -> bytes:
    image = Image.new("L", (100, 90), 255)
    draw = ImageDraw.Draw(image)
    draw.rectangle((6, 6, 93, 83), outline=0, width=3)
    draw.ellipse((24, 28, 36, 40), outline=0, width=3)
    draw.ellipse((64, 28, 76, 40), outline=0, width=3)
    draw.ellipse((42, 52, 58, 68), outline=0, width=3)
    output = io.BytesIO()
    image.save(output, format="PNG")
    return output.getvalue()


def _empty_closed_frame(*, line_width: int = 3) -> bytes:
    image = Image.new("L", (120, 100), 255)
    draw = ImageDraw.Draw(image)
    draw.rounded_rectangle(
        (8, 8, 111, 91),
        radius=22,
        outline=0,
        width=line_width,
    )
    output = io.BytesIO()
    image.save(output, format="PNG")
    return output.getvalue()


def _frame_with_attached_spoke() -> bytes:
    image = Image.new("L", (120, 100), 255)
    draw = ImageDraw.Draw(image)
    draw.rounded_rectangle((8, 8, 111, 91), radius=22, outline=0, width=4)
    draw.line((9, 50, 62, 50), fill=0, width=4)
    output = io.BytesIO()
    image.save(output, format="PNG")
    return output.getvalue()


def _circular_symbol_line_art() -> bytes:
    image = Image.new("L", (180, 180), 255)
    draw = ImageDraw.Draw(image)
    draw.ellipse((14, 14, 166, 166), outline=0, width=4)
    # Thin diagonal/curved symbols exercise anti-aliased binarisation and
    # stamp relief preservation independently of the vector cutter outline.
    draw.line((45, 128, 135, 52), fill=0, width=2)
    draw.arc((55, 55, 125, 125), 195, 342, fill=0, width=2)
    draw.ellipse((82, 82, 98, 98), outline=0, width=2)
    output = io.BytesIO()
    image.save(output, format="PNG")
    return output.getvalue()


def _randomized_closed_line_art(seed: int) -> bytes:
    """Build deterministic varied line art without relying on private images."""
    rng = np.random.default_rng(seed)
    image = Image.new("L", (224, 224), 255)
    draw = ImageDraw.Draw(image)
    width = int(rng.integers(3, 7))
    outer_kind = seed % 4
    if outer_kind == 0:
        draw.ellipse((12, 16, 211, 207), outline=0, width=width)
    elif outer_kind == 1:
        radius = int(rng.integers(20, 52))
        draw.rounded_rectangle(
            (12, 16, 211, 207), radius=radius, outline=0, width=width
        )
    elif outer_kind == 2:
        points = []
        for index in range(12):
            angle = 2.0 * np.pi * index / 12.0
            radius = float(rng.uniform(82.0, 98.0))
            points.append(
                (112.0 + radius * np.cos(angle), 112.0 + radius * np.sin(angle))
            )
        draw.line(points + [points[0]], fill=0, width=width, joint="curve")
    else:
        # A crescent-like closed contour exercises the concave frame supplied
        # by the user while remaining wholly synthetic.
        outer = [
            (25, 74), (45, 39), (78, 20), (103, 54), (112, 86),
            (121, 54), (146, 20), (179, 39), (199, 74), (205, 119),
            (188, 159), (154, 188), (112, 201), (70, 188), (36, 159),
            (19, 119), (25, 74),
        ]
        draw.line(outer, fill=0, width=width, joint="curve")

    feature_count = int(rng.integers(2, 6))
    for feature_index in range(feature_count):
        kind = (seed + feature_index) % 4
        # Keep every synthetic detail safely separated from every outer-frame
        # variant, including the narrow centre of the crescent-like contour.
        cx = int(rng.integers(72, 153))
        cy = int(rng.integers(116, 153))
        size = int(rng.integers(8, 18))
        feature_width = int(rng.integers(2, 5))
        if kind == 0:
            draw.ellipse(
                (cx - size, cy - size, cx + size, cy + size),
                outline=0,
                width=feature_width,
            )
        elif kind == 1:
            draw.arc(
                (cx - size, cy - size, cx + size, cy + size),
                start=int(rng.integers(0, 90)),
                end=int(rng.integers(220, 355)),
                fill=0,
                width=feature_width,
            )
        elif kind == 2:
            draw.line(
                (
                    cx - size,
                    cy + size // 2,
                    cx,
                    cy - size,
                    cx + size,
                    cy + size // 2,
                ),
                fill=0,
                width=feature_width,
                joint="curve",
            )
        else:
            draw.regular_polygon(
                (cx, cy, size),
                n_sides=int(rng.integers(3, 7)),
                rotation=int(rng.integers(0, 90)),
                outline=0,
                width=feature_width,
            )
    output = io.BytesIO()
    image.save(output, format="PNG")
    return output.getvalue()


def _stl_metrics(payload: bytes) -> dict[str, object]:
    count = struct.unpack_from("<I", payload, 80)[0]
    assert len(payload) == 84 + 50 * count
    dtype = np.dtype([("normal", "<f4", (3,)), ("vertices", "<f4", (3, 3)), ("attr", "<u2")])
    records = np.frombuffer(payload, dtype=dtype, offset=84, count=count)
    vertices = records["vertices"].astype(float)
    normals = records["normal"].astype(float)
    edges: dict[tuple[tuple[float, ...], tuple[float, ...]], tuple[int, int]] = {}
    volume = 0.0
    normal_alignment = []
    for triangle in vertices:
        volume += float(np.dot(triangle[0], np.cross(triangle[1], triangle[2]))) / 6.0
        points = [tuple(np.round(point, 7)) for point in triangle]
        for start, end in ((0, 1), (1, 2), (2, 0)):
            key = tuple(sorted((points[start], points[end])))
            count0, direction0 = edges.get(key, (0, 0))
            edges[key] = (count0 + 1, direction0 + (1 if points[start] == key[0] else -1))
    for triangle, stored_normal in zip(vertices, normals):
        calculated = np.cross(triangle[1] - triangle[0], triangle[2] - triangle[0])
        calculated /= np.linalg.norm(calculated)
        normal_alignment.append(float(np.dot(calculated, stored_normal)))
    return {
        "triangles": count,
        "bounds": vertices.reshape(-1, 3).max(axis=0) - vertices.reshape(-1, 3).min(axis=0),
        "volume": volume,
        "edge_bad": sum(count0 != 2 or direction != 0 for count0, direction in edges.values()),
        "normal_min_alignment": min(normal_alignment),
        "vertices": vertices,
    }


def test_small_line_art_uses_full_bounded_raster_budget():
    lines = engine.load_line_art_bytes(_face())

    # load_line_art_bytes adds four safe background pixels on every side.
    assert max(lines.shape) == engine.MAX_GRID_SIDE + 8


@pytest.mark.parametrize("dark_background", [False, True])
def test_line_art_generates_distinct_watertight_cutter_and_mirrored_stamp(dark_background):
    bundle, summary = generate_zip_bytes(_face(dark_background=dark_background), CookieParameters(width_mm=72))

    assert summary["watertight"] is True
    assert summary["mirrored_stamp"] is True
    geometry = summary["geometry"]
    assert geometry["grip_cells"] > geometry["cutter_ring_cells"] > 0
    assert geometry["stamp_plate_cells"] > geometry["relief_cells"] > 0
    assert geometry["cutter_triangles"] > 0 and geometry["stamp_triangles"] > 0
    with zipfile.ZipFile(io.BytesIO(bundle)) as archive:
        assert set(archive.namelist()) == {
            "cutter.stl",
            "stamp_mirrored.stl",
            "segmentation_preview.png",
            "parameters.json",
            "README.txt",
        }
        payload = json.loads(archive.read("parameters.json"))
        assert payload["offline"] is True
        assert payload["upload_persisted"] is False
        assert archive.read("cutter.stl")[:4] == b"MAGI"


def test_variable_height_corner_mesh_is_strictly_watertight_and_mirrors():
    heights = np.asarray([[2.0, 1.0, 1.0], [1.0, 1.0, 1.0]])
    triangles = _heightfield_mesh(heights, 1.0, mirror=True)
    validate_watertight(triangles)
    assert mesh_signed_volume(triangles) == pytest.approx(float(heights.sum()))
    highest = [vertex for triangle in triangles for vertex in triangle if vertex[2] == 2.0]
    assert highest and min(vertex[0] for vertex in highest) >= 2.0


def test_unit_voxel_has_consistent_outward_normals_and_unit_volume():
    triangles = _heightfield_mesh(np.asarray([[1.0]]), 1.0)

    validate_watertight(triangles)
    assert mesh_signed_volume(triangles) == pytest.approx(1.0)


def test_individually_watertight_but_disconnected_shells_are_rejected():
    first = _heightfield_mesh(np.asarray([[1.0]]), 1.0)
    second = [
        tuple((x + 3.0, y, z) for x, y, z in triangle)
        for triangle in first
    ]
    with pytest.raises(CookieSTLError, match="disconnected_mesh"):
        validate_watertight(first + second)


def test_rounded_diagonal_line_art_is_regularized_to_watertight_meshes():
    bundle, summary = generate_zip_bytes(_rounded_face(), CookieParameters())

    assert summary["watertight"] is True
    with zipfile.ZipFile(io.BytesIO(bundle)) as archive:
        assert archive.read("cutter.stl")
        assert archive.read("stamp_mirrored.stl")


def test_vector_cutter_limits_finished_envelope_and_removes_raster_staircase():
    bundle, summary = generate_zip_bytes(_circular_symbol_line_art(), CookieParameters(width_mm=80))
    with zipfile.ZipFile(io.BytesIO(bundle)) as archive:
        metrics = _stl_metrics(archive.read("cutter.stl"))

    bounds = metrics["bounds"]
    assert 79.0 <= float(max(bounds[:2])) <= 80.05
    assert metrics["edge_bad"] == 0
    assert float(metrics["volume"]) > 1.0
    assert float(metrics["normal_min_alignment"]) > 0.99999
    # A circle was previously a 192-cell heightfield (tens of thousands of
    # facets); a vector wall must be materially simpler without losing volume.
    assert int(metrics["triangles"]) < 5000
    assert summary["geometry"]["cutter_vector_vertices"] >= 12
    assert summary["geometry"]["cutter_contour_hausdorff_mm"] <= 0.15
    vertices = metrics["vertices"].reshape(-1, 3)[:, :2]
    centre = (vertices.min(axis=0) + vertices.max(axis=0)) / 2.0
    radii = np.linalg.norm(vertices - centre, axis=1)
    outer = radii[radii >= radii.max() - 1.0]
    assert float(outer.std()) < 0.30


def test_contour_error_is_measured_in_mm_against_unsimplified_boundary():
    content = _circular_symbol_line_art()
    silhouette, _details = segment_line_art(content)
    ys, xs = np.where(silhouette)
    params = CookieParameters(width_mm=80)
    scale_mm = (
        params.width_mm - params.blade_wall_mm - 2.0 * params.rim_mm
    ) / max(int(xs.max() - xs.min() + 1), int(ys.max() - ys.min() + 1))
    raw = affinity.scale(
        _mask_pixel_geometry(silhouette),
        xfact=scale_mm,
        yfact=scale_mm,
        origin=(0.0, 0.0),
    )
    vector, quality = _physical_mask_geometry(silhouette, scale_mm)
    measured = float(raw.boundary.hausdorff_distance(vector.boundary))

    assert measured == pytest.approx(
        quality["hausdorff_mm"], abs=engine.GEOMETRY_PRECISION_MM
    )
    assert measured <= 0.15


def test_rim_parameter_changes_real_grip_geometry_without_exceeding_envelope():
    content = _circular_symbol_line_art()
    outputs = []
    for rim_mm in (0.0, 3.0):
        bundle, summary = generate_zip_bytes(
            content, CookieParameters(width_mm=80, rim_mm=rim_mm)
        )
        with zipfile.ZipFile(io.BytesIO(bundle)) as archive:
            cutter = archive.read("cutter.stl")
        outputs.append((hashlib.sha256(cutter).hexdigest(), summary["geometry"]))

    assert outputs[0][0] != outputs[1][0]
    assert outputs[1][1]["cutter_grip_area_mm2"] > outputs[0][1]["cutter_grip_area_mm2"]
    assert outputs[0][1]["cutter_finished_envelope_mm"] <= 80.05
    assert outputs[1][1]["cutter_finished_envelope_mm"] <= 80.05


def test_stamp_keeps_thin_diagonal_and_curved_symbol_relief():
    bundle, summary = generate_zip_bytes(_circular_symbol_line_art(), CookieParameters())
    # The synthetic symbol contains more than an outer contour; a zero/small
    # relief would reveal that vector cutter cleanup leaked into stamp detail.
    assert summary["geometry"]["relief_cells"] >= 80
    assert summary["geometry"]["stamp_contour_hausdorff_mm"] <= 0.15
    assert summary["geometry"]["stamp_relief_min_feature_mm"] >= 0.4
    assert summary["geometry"]["stamp_triangles"] < 50_000
    with zipfile.ZipFile(io.BytesIO(bundle)) as archive:
        metrics = _stl_metrics(archive.read("stamp_mirrored.stl"))
    assert metrics["edge_bad"] == 0
    assert metrics["normal_min_alignment"] > 0.99999
    vertices = metrics["vertices"].reshape(-1, 3)[:, :2]
    silhouette, _details = segment_line_art(_circular_symbol_line_art())
    ys, xs = np.where(silhouette)
    scale_mm = (80.0 - 1.2 - 2.0 * 3.0) / max(
        int(xs.max() - xs.min() + 1), int(ys.max() - ys.min() + 1)
    )
    grid_residual = np.abs(vertices / scale_mm - np.round(vertices / scale_mm))
    assert float((grid_residual < 1e-5).all(axis=1).mean()) < 0.95


def test_layered_arrangement_handles_holes_and_multiple_raised_components():
    base = Polygon(
        [(0, 0), (30, 0), (30, 30), (0, 30)],
        holes=[[(12, 12), (18, 12), (18, 18), (12, 18)]],
    )
    raised = MultiPolygon(
        [
            Polygon(
                [(2, 2), (11, 2), (11, 11), (2, 11)],
                holes=[[(5, 5), (8, 5), (8, 8), (5, 8)]],
            ),
            Polygon([(20, 20), (27, 20), (27, 27), (20, 27)]),
        ]
    )
    triangles = _layered_mesh(base, raised, 3.0, 5.0)
    metrics = _stl_metrics(_binary_stl(triangles, "arrangement regression"))

    assert metrics["edge_bad"] == 0
    assert metrics["volume"] > 0
    assert metrics["normal_min_alignment"] > 0.99999


def test_minimum_feature_and_resource_guards_fail_closed(monkeypatch):
    assert _minimum_polygon_feature(Polygon([(0, 0), (.39, 0), (.39, 2), (0, 2)])) == 0
    assert _minimum_polygon_feature(Polygon([(0, 0), (.41, 0), (.41, 2), (0, 2)])) == .4

    monkeypatch.setattr(engine, "MAX_BOUNDARY_SEGMENTS", 10)
    with pytest.raises(CookieSTLError, match="resource_limit_exceeded"):
        _mask_pixel_geometry(np.ones((4, 4), dtype=bool))

    ticks = iter((0.0, 21.0))
    monkeypatch.setattr(engine.time, "monotonic", lambda: next(ticks))
    with pytest.raises(CookieSTLError, match="resource_limit_exceeded"):
        generate_zip_bytes(_face())


def test_closed_internal_shapes_are_preserved_as_stamp_relief():
    bundle, summary = generate_zip_bytes(_closed_details(), CookieParameters())

    assert summary["geometry"]["relief_cells"] > 0
    assert summary["watertight"] is True
    assert zipfile.is_zipfile(io.BytesIO(bundle))


@pytest.mark.parametrize("seed", range(12))
def test_randomized_outer_and_inner_lines_are_closed_continuous_and_smooth(seed):
    bundle, summary = generate_zip_bytes(
        _randomized_closed_line_art(seed),
        CookieParameters(width_mm=80),
    )
    geometry = summary["geometry"]
    assert summary["watertight"] is True
    assert geometry["cutter_top_surface_components"] == 1
    assert geometry["cutter_top_boundary_loops"] == 2
    assert geometry["cutter_contour_hausdorff_mm"] <= 0.15
    assert geometry["stamp_top_surface_components"] >= 1
    assert geometry["stamp_top_boundary_loops"] >= 1
    assert geometry["stamp_contour_hausdorff_mm"] <= 0.15
    assert geometry["stamp_relief_min_feature_mm"] >= 0.4
    with zipfile.ZipFile(io.BytesIO(bundle)) as archive:
        cutter = _stl_metrics(archive.read("cutter.stl"))
        stamp = _stl_metrics(archive.read("stamp_mirrored.stl"))
    assert cutter["edge_bad"] == 0
    assert stamp["edge_bad"] == 0
    assert cutter["volume"] > 0
    assert stamp["volume"] > 0


@pytest.mark.parametrize("line_width", [2, 4, 8, 16, 28])
def test_closed_empty_frame_generates_smooth_cutter_only(line_width):
    bundle, summary = generate_zip_bytes(
        _empty_closed_frame(line_width=line_width),
        CookieParameters(),
    )

    assert summary["mode"] == "cutter_only"
    assert summary["stamp_generated"] is False
    assert summary["mirrored_stamp"] is False
    assert summary["geometry"]["relief_cells"] == 0
    assert summary["geometry"]["stamp_triangles"] == 0
    assert summary["geometry"]["cutter_contour_hausdorff_mm"] <= 0.15
    with zipfile.ZipFile(io.BytesIO(bundle)) as archive:
        assert set(archive.namelist()) == {
            "cutter.stl",
            "segmentation_preview.png",
            "parameters.json",
            "README.txt",
        }
        metrics = _stl_metrics(archive.read("cutter.stl"))
        assert metrics["edge_bad"] == 0
        assert metrics["normal_min_alignment"] > 0.99999
        assert metrics["triangles"] < 5000


def test_pattern_connected_to_outer_frame_is_not_independent_linework():
    bundle, summary = generate_zip_bytes(
        _frame_with_attached_spoke(), CookieParameters()
    )

    assert summary["mode"] == "cutter_only"
    assert summary["stamp_generated"] is False
    with zipfile.ZipFile(io.BytesIO(bundle)) as archive:
        assert "cutter.stl" in archive.namelist()
        assert "stamp_mirrored.stl" not in archive.namelist()


def test_line_art_inspection_reports_only_safe_topology_counts():
    inspection = inspect_line_art_bytes(_face())

    assert inspection == {
        "line_art_validated": True,
        "internal_feature_components": 3,
        "generation_mode": "cutter_and_stamp",
    }


def test_line_art_inspection_marks_empty_frame_as_cutter_only():
    assert inspect_line_art_bytes(_empty_closed_frame()) == {
        "line_art_validated": True,
        "internal_feature_components": 0,
        "generation_mode": "cutter_only",
    }


def test_open_outline_blank_and_nonimage_fail_closed():
    with pytest.raises(CookieSTLError, match="open_or_missing_outer_contour"):
        generate_zip_bytes(_face(open_outline=True))
    blank = io.BytesIO()
    Image.new("L", (32, 32), 255).save(blank, format="PNG")
    with pytest.raises(CookieSTLError, match="no_usable_line_art"):
        generate_zip_bytes(blank.getvalue())
    with pytest.raises(CookieSTLError, match="input_not_supported_image"):
        generate_zip_bytes(b"not-an-image")
