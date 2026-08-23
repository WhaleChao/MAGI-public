from __future__ import annotations

import hashlib
import io
import json
import multiprocessing
import re
import sys
import types
import zipfile

import pytest
from flask import Flask
from PIL import Image, ImageDraw


def _spawn_cookie_child_with_failed_setrlimit(connection, monitor_ready, content, values):
    """Run the production child target with a real spawned setup failure."""
    import resource

    import skills.cookie_stl as engine
    from api.blueprints import cookie_cutter as blueprint

    original_setrlimit = resource.setrlimit
    original_generate = engine.generate_zip_bytes

    def reject_limit(*_args, **_kwargs):
        raise OSError("private platform detail must not cross IPC")

    def reject_generation(*_args, **_kwargs):
        raise AssertionError("STL generation must not start without the CPU limit")

    resource.setrlimit = reject_limit
    engine.generate_zip_bytes = reject_generation
    try:
        blueprint._cookie_generation_child(connection, monitor_ready, content, values)
    finally:
        engine.generate_zip_bytes = original_generate
        resource.setrlimit = original_setrlimit


def _png(width: int = 64, height: int = 64) -> bytes:
    from PIL import Image
    output = io.BytesIO()
    Image.new("L", (width, height), 255).save(output, format="PNG")
    return output.getvalue()


def _face_png() -> bytes:
    image = Image.new("L", (96, 80), 255)
    draw = ImageDraw.Draw(image)
    draw.rounded_rectangle((8, 8, 87, 71), radius=18, outline=0, width=3)
    draw.ellipse((26, 28, 32, 34), fill=0)
    draw.ellipse((62, 28, 68, 34), fill=0)
    draw.arc((31, 32, 64, 57), 10, 170, fill=0, width=3)
    output = io.BytesIO()
    image.save(output, format="PNG")
    return output.getvalue()


def _empty_frame_png(*, line_width: int = 3) -> bytes:
    image = Image.new("L", (96, 80), 255)
    draw = ImageDraw.Draw(image)
    draw.rounded_rectangle((8, 8, 87, 71), radius=18, outline=0, width=line_width)
    output = io.BytesIO()
    image.save(output, format="PNG")
    return output.getvalue()


def _synthetic_bundle(summary: dict[str, object]) -> bytes:
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("cutter.stl", b"cutter")
        if summary.get("stamp_generated") is True:
            archive.writestr("stamp_mirrored.stl", b"stamp")
        archive.writestr("segmentation_preview.png", b"preview")
        archive.writestr("parameters.json", json.dumps(summary, sort_keys=True))
        archive.writestr("README.txt", "readme")
    return output.getvalue()


def _summary_fixture() -> dict[str, object]:
    return {
        "mode": "cutter_and_stamp",
        "stamp_generated": True,
        "watertight": True,
    }


class _MonitorReady:
    def __init__(self):
        self.set_calls = 0

    def set(self):
        self.set_calls += 1

    def wait(self, *, timeout):
        assert timeout > 0
        return self.set_calls > 0


def _app(*, csrf: bool = False) -> Flask:
    app = Flask(__name__, template_folder="../templates")
    app.config.update(TESTING=True, SECRET_KEY="cookie-cutter-test")
    from api.blueprints.cookie_cutter import cookie_cutter_bp

    app.register_blueprint(cookie_cutter_bp)
    if csrf:
        from api.csrf_guard import middleware_apply_csrf

        middleware_apply_csrf(app)
    return app


def test_public_cookie_cutter_page_is_responsive_and_no_store():
    response = _app().test_client().get("/cookie-cutter")
    page = response.get_data(as_text=True)
    assert response.status_code == 200
    assert response.headers["Cache-Control"] == "no-store"
    assert 'name="viewport"' in page
    assert "/api/cookie-cutter/generate" in page
    assert "/lottery" in page and "/exam-tutor" in page
    assert "只有封閉外框時會產生光滑切模" in page
    assert "只有外框也可生成" in page


def test_spawned_child_setrlimit_failure_is_safe_terminal_and_reaped():
    import api.blueprints.cookie_cutter as blueprint

    context = multiprocessing.get_context("spawn")
    parent, child = context.Pipe(duplex=False)
    monitor_ready = context.Event()
    worker = context.Process(
        target=_spawn_cookie_child_with_failed_setrlimit,
        args=(
            child,
            monitor_ready,
            b"must-not-be-processed",
            {
                name: float(getattr(blueprint.CookieParameters(), name))
                for name in blueprint.CookieParameters.__dataclass_fields__
            },
        ),
        name="cookie-cutter-setrlimit-regression",
    )
    try:
        worker.start()
        child.close()
        assert parent.poll(10), "spawned child did not return a bounded terminal result"
        payload = parent.recv()
        worker.join(10)

        child_reaped = not worker.is_alive() and worker.exitcode is not None
        child_leaks = sum(
            process.pid == worker.pid for process in multiprocessing.active_children()
        )
        assert payload == (
            "resource_error",
            blueprint._RESOURCE_LIMIT_SETUP_FAILURE,
        )
        assert "private platform detail" not in repr(payload)
        assert not any(isinstance(item, bytes) for item in payload)
        assert worker.exitcode == 0
        assert child_reaped is True
        assert child_leaks == 0
    finally:
        parent.close()
        child.close()
        if worker.is_alive():
            worker.terminate()
            worker.join(2)


def test_child_waits_for_parent_resource_monitor_before_generation(monkeypatch):
    import resource

    import api.blueprints.cookie_cutter as blueprint
    import skills.cookie_stl as engine

    sent = []

    class Connection:
        def send(self, payload):
            sent.append(payload)

        def close(self):
            return None

    class NeverReady:
        def wait(self, *, timeout):
            assert timeout == blueprint.MAX_GENERATION_SECONDS
            return False

    monkeypatch.setattr(resource, "setrlimit", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        engine,
        "generate_zip_bytes",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("engine must wait for the parent RSS monitor")
        ),
    )

    blueprint._cookie_generation_child(
        Connection(),
        NeverReady(),
        b"synthetic",
        {
            name: float(getattr(blueprint.CookieParameters(), name))
            for name in blueprint.CookieParameters.__dataclass_fields__
        },
    )

    assert sent == [("cookie_error", "resource_attestation_unavailable")]


def test_parent_maps_exact_resource_setup_terminal_and_reaps_worker(monkeypatch):
    import api.blueprints.cookie_cutter as blueprint

    class Parent:
        def poll(self, _timeout):
            return True

        def recv(self):
            return "resource_error", blueprint._RESOURCE_LIMIT_SETUP_FAILURE

        def close(self):
            return None

    class Child:
        def close(self):
            return None

    class Worker:
        pid = 4321

        def __init__(self):
            self.running = False
            self.exitcode = None

        def start(self):
            self.running = True

        def is_alive(self):
            return self.running

        def join(self, _timeout):
            self.running = False
            self.exitcode = 0

        def terminate(self):
            raise AssertionError("resource setup failure should exit cleanly")

        def kill(self):
            raise AssertionError("resource setup failure should exit cleanly")

    worker = Worker()

    class Context:
        def Pipe(self, *, duplex):
            assert duplex is False
            return Parent(), Child()

        def Event(self):
            return _MonitorReady()

        def Process(self, **_kwargs):
            return worker

    psutil = types.ModuleType("psutil")
    psutil.Process = lambda _pid: types.SimpleNamespace()
    monkeypatch.setitem(sys.modules, "psutil", psutil)
    monkeypatch.setattr(blueprint.multiprocessing, "get_context", lambda _kind: Context())

    with pytest.raises(
        blueprint.CookieSTLError,
        match=f"^{blueprint._RESOURCE_LIMIT_SETUP_FAILURE}$",
    ):
        blueprint._generate_bounded(b"synthetic", blueprint.CookieParameters())

    assert worker.is_alive() is False
    assert worker.exitcode == 0


def test_cookie_cutter_page_uses_magi_theme_and_responsive_workspace():
    page = _app().test_client().get("/cookie-cutter").get_data(as_text=True)
    assert 'class="site-lottery site-cookie-cutter"' in page
    assert "magi-theme.css" in page and "magi-theme.js" in page
    assert "data-magi-theme-toggle" in page
    assert "cc-workspace" in page and "cc-guide" in page
    assert "@media (max-width:760px)" in page
    assert ':root[data-magi-theme="cyber"] body.site-cookie-cutter' in page
    assert "cc-status is-visible" in page


def test_prepare_accepts_memory_only_safe_png():
    response = _app().test_client().post(
        "/api/cookie-cutter/prepare",
        data={"image": (io.BytesIO(_face_png()), "../../line.png")},
        content_type="multipart/form-data",
    )
    body = response.get_json()
    assert response.status_code == 200
    assert body["ok"] is True
    assert body["engine_contract"] == "magi.cookie-cutter-stl/v1"
    assert body["upload_persisted"] is False
    assert body["width"] == 96 and body["height"] == 80
    assert body["line_art_validated"] is True
    assert body["internal_feature_components"] == 3
    assert body["generation_mode"] == "cutter_and_stamp"
    assert "切模與鏡像壓模" in body["next_step"]


@pytest.mark.parametrize("line_width", [2, 8, 24])
def test_prepare_accepts_closed_empty_frame_as_cutter_only(line_width):
    response = _app().test_client().post(
        "/api/cookie-cutter/prepare",
        data={
            "image": (
                io.BytesIO(_empty_frame_png(line_width=line_width)),
                "empty-frame.png",
            )
        },
        content_type="multipart/form-data",
    )

    assert response.status_code == 200
    body = response.get_json()
    assert body["ok"] is True
    assert body["generation_mode"] == "cutter_only"
    assert body["internal_feature_components"] == 0
    assert "只產生光滑切模" in body["next_step"]


def test_prepare_rejects_non_image_and_oversized_pixels():
    client = _app().test_client()
    bad_type = client.post(
        "/api/cookie-cutter/prepare",
        data={"image": (io.BytesIO(b"not image"), "line.svg")},
        content_type="multipart/form-data",
    )
    assert bad_type.status_code == 400
    assert bad_type.get_json()["message"] == "只支援 PNG、JPG、BMP 或 TIF 圖片"
    huge = client.post(
        "/api/cookie-cutter/prepare",
        data={"image": (io.BytesIO(_png(4097, 4097)), "line.png")},
        content_type="multipart/form-data",
    )
    assert huge.status_code == 400
    assert "4096 × 4096" in huge.get_json()["message"]


def test_declared_oversize_is_rejected_before_request_files_or_engine(monkeypatch):
    import api.blueprints.cookie_cutter as blueprint

    monkeypatch.setattr(blueprint, "_within_rate_limit", lambda: True)
    monkeypatch.setattr(
        blueprint, "_read_upload_bounded",
        lambda _upload: (_ for _ in ()).throw(AssertionError("multipart must not parse")),
    )
    response = _app().test_client().open(
        "/api/cookie-cutter/prepare",
        method="POST",
        data=b"ignored",
        content_type="multipart/form-data; boundary=preflight",
        environ_overrides={
            "CONTENT_LENGTH": str(blueprint.MAX_UPLOAD_BYTES + blueprint.MAX_MULTIPART_OVERHEAD_BYTES + 1)
        },
    )
    assert response.status_code == 413
    assert response.headers["Cache-Control"] == "no-store"
    assert "8MB" in response.get_json()["message"]


def test_allowed_multipart_body_reaches_bounded_reader(monkeypatch):
    import api.blueprints.cookie_cutter as blueprint

    monkeypatch.setattr(blueprint, "_within_rate_limit", lambda: True)
    observed = {"called": False}
    original = blueprint._read_upload_bounded
    def wrapped(upload):
        observed["called"] = True
        return original(upload)
    monkeypatch.setattr(blueprint, "_read_upload_bounded", wrapped)
    response = _app().test_client().post(
        "/api/cookie-cutter/prepare",
        data={"image": (io.BytesIO(_face_png()), "line.png")},
        content_type="multipart/form-data",
    )
    assert response.status_code == 200
    assert observed["called"] is True


def test_public_generate_returns_real_stl_bundle_without_login(monkeypatch):
    import api.blueprints.cookie_cutter as blueprint

    monkeypatch.setattr(blueprint, "_within_rate_limit", lambda: True)
    response = _app().test_client().post(
        "/api/cookie-cutter/generate",
        data={
            "image": (io.BytesIO(_face_png()), "face.png"),
            "width_mm": "72",
            "blade_height_mm": "15",
            "blade_wall_mm": "1.2",
            "rim_mm": "3",
            "stamp_base_mm": "3",
            "relief_mm": "2",
            "clearance_mm": ".3",
        },
        content_type="multipart/form-data",
    )

    assert response.status_code == 200
    assert response.headers["Cache-Control"] == "no-store"
    assert response.headers["X-MAGI-Mesh-Status"] == "watertight"
    assert response.mimetype == "application/zip"
    with zipfile.ZipFile(io.BytesIO(response.data)) as archive:
        assert "cutter.stl" in archive.namelist()
        assert "stamp_mirrored.stl" in archive.namelist()
        receipt = json.loads(archive.read("parameters.json"))
    attestation = {
        key: receipt[key]
        for key in (
            "generation_seconds",
            "peak_rss_bytes",
            "child_reaped",
            "child_leaks",
        )
    }
    assert isinstance(attestation["generation_seconds"], (int, float))
    assert attestation["generation_seconds"] >= 0
    assert isinstance(attestation["peak_rss_bytes"], int)
    assert attestation["peak_rss_bytes"] > 0
    assert attestation["child_reaped"] is True
    assert attestation["child_leaks"] == 0
    assert not ({"path", "image", "filename"} & set(attestation))


def test_public_generate_turns_closed_empty_frame_into_cutter_only_bundle(monkeypatch):
    import api.blueprints.cookie_cutter as blueprint

    monkeypatch.setattr(blueprint, "_within_rate_limit", lambda: True)
    response = _app().test_client().post(
        "/api/cookie-cutter/generate",
        data={
            "image": (io.BytesIO(_empty_frame_png(line_width=8)), "empty-frame.png"),
            "width_mm": "72",
            "blade_height_mm": "15",
            "blade_wall_mm": "1.2",
            "rim_mm": "3",
            "stamp_base_mm": "3",
            "relief_mm": "2",
            "clearance_mm": ".3",
        },
        content_type="multipart/form-data",
    )

    assert response.status_code == 200
    assert response.headers["X-MAGI-Mesh-Status"] == "watertight"
    with zipfile.ZipFile(io.BytesIO(response.data)) as archive:
        assert set(archive.namelist()) == blueprint._COOKIE_ARCHIVE_COMMON_MEMBERS
        receipt = json.loads(archive.read("parameters.json"))
        assert receipt["mode"] == "cutter_only"
        assert receipt["stamp_generated"] is False
        assert receipt["watertight"] is True
        assert receipt["geometry"]["cutter_triangles"] > 0
        assert receipt["geometry"]["stamp_triangles"] == 0


@pytest.mark.parametrize("malicious_peak", [-1, 10**12])
def test_archive_attestation_overwrites_untrusted_claims_and_rejects_bad_schema(
    malicious_peak,
):
    import api.blueprints.cookie_cutter as blueprint

    child_summary = {
        "mode": "cutter_and_stamp",
        "stamp_generated": True,
        "watertight": True,
        "generation_seconds": -999,
        "peak_rss_bytes": malicious_peak,
        "child_reaped": False,
        "child_leaks": 99,
    }
    trusted = {
        "generation_seconds": 1.25,
        "peak_rss_bytes": 4096,
        "child_reaped": True,
        "child_leaks": 0,
    }
    original_bundle = _synthetic_bundle(child_summary)
    preserved_members = {
        "cutter.stl",
        "stamp_mirrored.stl",
        "segmentation_preview.png",
        "README.txt",
    }
    with zipfile.ZipFile(io.BytesIO(original_bundle)) as archive:
        before = {name: archive.read(name) for name in preserved_members}
        before_sha256 = {
            name: hashlib.sha256(payload).hexdigest()
            for name, payload in before.items()
        }

    bundle, receipt = blueprint._attest_generated_bundle(
        original_bundle,
        child_summary,
        trusted,
    )

    assert {key: receipt[key] for key in trusted} == trusted
    with zipfile.ZipFile(io.BytesIO(bundle)) as archive:
        assert set(archive.namelist()) == blueprint._COOKIE_ARCHIVE_STAMP_MEMBERS
        assert json.loads(archive.read("parameters.json")) == receipt
        after = {name: archive.read(name) for name in preserved_members}
        assert after == before
        assert {
            name: hashlib.sha256(payload).hexdigest()
            for name, payload in after.items()
        } == before_sha256

    malformed = io.BytesIO()
    with zipfile.ZipFile(malformed, "w") as archive:
        archive.writestr("parameters.json", json.dumps(child_summary))
    with pytest.raises(
        blueprint.CookieSTLError,
        match="generation_archive_schema_failed",
    ):
        blueprint._attest_generated_bundle(
            malformed.getvalue(),
            child_summary,
            trusted,
        )


def test_archive_attestation_accepts_exact_cutter_only_members_and_rejects_stamp_drift():
    import api.blueprints.cookie_cutter as blueprint

    summary = {
        "mode": "cutter_only",
        "stamp_generated": False,
        "watertight": True,
    }
    attestation = {
        "generation_seconds": 0.25,
        "peak_rss_bytes": 4096,
        "child_reaped": True,
        "child_leaks": 0,
    }
    bundle, receipt = blueprint._attest_generated_bundle(
        _synthetic_bundle(summary), summary, attestation
    )
    assert receipt["mode"] == "cutter_only"
    with zipfile.ZipFile(io.BytesIO(bundle)) as archive:
        assert set(archive.namelist()) == blueprint._COOKIE_ARCHIVE_COMMON_MEMBERS

    drifted = io.BytesIO()
    with zipfile.ZipFile(io.BytesIO(_synthetic_bundle(summary))) as source:
        with zipfile.ZipFile(drifted, "w", zipfile.ZIP_DEFLATED) as target:
            for info in source.infolist():
                target.writestr(info, source.read(info.filename))
            target.writestr("stamp_mirrored.stl", b"unexpected")
    with pytest.raises(
        blueprint.CookieSTLError,
        match="generation_archive_schema_failed",
    ):
        blueprint._attest_generated_bundle(
            drifted.getvalue(), summary, attestation
        )


def test_generate_uses_double_submit_csrf_and_rate_limit(monkeypatch):
    import api.blueprints.cookie_cutter as blueprint

    monkeypatch.setattr(blueprint, "_within_rate_limit", lambda: True)
    client = _app(csrf=True).test_client()
    page = client.get("/cookie-cutter")
    cookie_header = "\n".join(page.headers.getlist("Set-Cookie"))
    token_match = re.search(r"X-CSRF-Token=([^;]+)", cookie_header)
    assert token_match
    payload = {
        "image": (io.BytesIO(_face_png()), "face.png"),
        "width_mm": "72", "blade_height_mm": "15", "blade_wall_mm": "1.2",
        "rim_mm": "3", "stamp_base_mm": "3", "relief_mm": "2", "clearance_mm": ".3",
    }
    rejected = client.post("/api/cookie-cutter/generate", data=payload, content_type="multipart/form-data")
    assert rejected.status_code == 403

    payload["image"] = (io.BytesIO(_face_png()), "face.png")
    accepted = client.post(
        "/api/cookie-cutter/generate",
        data=payload,
        content_type="multipart/form-data",
        headers={"X-CSRF-Token": token_match.group(1)},
    )
    assert accepted.status_code == 200

    monkeypatch.setattr(blueprint, "_within_rate_limit", lambda: False)
    limited = client.post(
        "/api/cookie-cutter/prepare",
        data={"image": (io.BytesIO(_face_png()), "face.png")},
        content_type="multipart/form-data",
        headers={"X-CSRF-Token": token_match.group(1)},
    )
    assert limited.status_code == 429


@pytest.mark.parametrize(
    ("code", "message_fragment"),
    [
        ("contour_quality_failed", "0.15 mm"),
        ("cutter_wall_not_continuous", "單一連續封閉環"),
        ("feature_too_small", "最小寬度"),
        ("resource_limit_exceeded", "處理逾時"),
        ("finished_envelope_exceeded", "成品尺寸"),
        ("generation_resource_limit_setup_failed", "資源限制無法安全啟用"),
        ("outer_contour_touches_image_edge", "四周保留白邊"),
    ],
)
def test_geometry_quality_failures_have_fixed_actionable_messages(
    monkeypatch, code, message_fragment
):
    import api.blueprints.cookie_cutter as blueprint

    monkeypatch.setattr(blueprint, "_within_rate_limit", lambda: True)
    monkeypatch.setattr(
        blueprint,
        "_generate_bounded",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(blueprint.CookieSTLError(code)),
    )
    response = _app().test_client().post(
        "/api/cookie-cutter/generate",
        data={
            "image": (io.BytesIO(_face_png()), "face.png"),
            "width_mm": "80",
            "blade_height_mm": "15",
            "blade_wall_mm": "1.2",
            "rim_mm": "3",
            "stamp_base_mm": "3",
            "relief_mm": "2",
            "clearance_mm": ".3",
        },
        content_type="multipart/form-data",
    )

    assert response.status_code == 400
    assert response.headers["Cache-Control"] == "no-store"
    assert message_fragment in response.get_json()["message"]


def test_generation_rss_overage_terminates_spawned_worker(monkeypatch):
    import api.blueprints.cookie_cutter as blueprint

    class Connection:
        def __init__(self):
            self.closed = False

        def poll(self, _timeout):
            return False

        def close(self):
            self.closed = True

    class Worker:
        pid = 4321

        def __init__(self):
            self.running = False
            self.terminated = False
            self.exitcode = None

        def start(self):
            self.running = True

        def is_alive(self):
            return self.running

        def join(self, _timeout):
            return None

        def terminate(self):
            self.terminated = True
            self.running = False
            self.exitcode = -15

        def kill(self):
            raise AssertionError("terminate should be sufficient")

    parent, child, worker = Connection(), Connection(), Worker()

    class Context:
        def Pipe(self, *, duplex):
            assert duplex is False
            return parent, child

        def Event(self):
            return _MonitorReady()

        def Process(self, **_kwargs):
            return worker

    psutil = types.ModuleType("psutil")
    psutil.Process = lambda pid: types.SimpleNamespace(
        memory_info=lambda: types.SimpleNamespace(
            rss=blueprint.MAX_GENERATION_RSS_BYTES + 1
        )
    )
    monkeypatch.setitem(sys.modules, "psutil", psutil)
    monkeypatch.setattr(blueprint.multiprocessing, "get_context", lambda _kind: Context())

    with pytest.raises(blueprint.CookieSTLError, match="generation_resource_limit"):
        blueprint._generate_bounded(b"synthetic", blueprint.CookieParameters())

    assert worker.terminated is True
    assert parent.closed is True and child.closed is True


def test_generation_fast_exit_without_parent_rss_sample_fails_closed(monkeypatch):
    import api.blueprints.cookie_cutter as blueprint

    class Parent:
        def __init__(self):
            self.polls = 0

        def poll(self, _timeout):
            self.polls += 1
            return self.polls >= 2

        def recv(self):
            return "ok", b"zip", {"watertight": True}

        def close(self):
            return None

    class Child:
        def close(self):
            return None

    class Worker:
        pid = 4321

        def __init__(self):
            self.running = False
            self.exitcode = None

        def start(self):
            self.running = True

        def is_alive(self):
            return self.running

        def join(self, _timeout):
            self.running = False
            self.exitcode = 0

        def terminate(self):
            return None

        def kill(self):
            return None

    summary_fixture = _summary_fixture()
    bundle_fixture = _synthetic_bundle(summary_fixture)
    parent, child, worker = Parent(), Child(), Worker()
    parent.recv = lambda: ("ok", bundle_fixture, summary_fixture)

    class Context:
        def Pipe(self, *, duplex):
            assert duplex is False
            return parent, child

        def Event(self):
            return _MonitorReady()

        def Process(self, **_kwargs):
            return worker

    psutil = types.ModuleType("psutil")
    psutil.Process = lambda _pid: types.SimpleNamespace(
        memory_info=lambda: (_ for _ in ()).throw(RuntimeError("process exited"))
    )
    monkeypatch.setitem(sys.modules, "psutil", psutil)
    monkeypatch.setattr(blueprint.multiprocessing, "get_context", lambda _kind: Context())

    with pytest.raises(
        blueprint.CookieSTLError,
        match="resource_attestation_unavailable",
    ):
        blueprint._generate_bounded(b"synthetic", blueprint.CookieParameters())

    assert worker.is_alive() is False
    assert worker.exitcode == 0


def test_generation_without_parent_rss_monitor_fails_closed(monkeypatch):
    import api.blueprints.cookie_cutter as blueprint

    class Connection:
        def close(self):
            return None

    class Worker:
        pid = 4321

        def __init__(self):
            self.running = False
            self.exitcode = None

        def start(self):
            self.running = True

        def is_alive(self):
            return self.running

        def join(self, _timeout):
            self.running = False
            self.exitcode = 0

        def terminate(self):
            raise AssertionError("join should reap the synthetic child")

        def kill(self):
            raise AssertionError("join should reap the synthetic child")

    parent, child, worker = Connection(), Connection(), Worker()

    class Context:
        def Pipe(self, *, duplex):
            assert duplex is False
            return parent, child

        def Event(self):
            return _MonitorReady()

        def Process(self, **_kwargs):
            return worker

    psutil = types.ModuleType("psutil")

    def unavailable(_pid):
        raise RuntimeError("process monitor unavailable")

    psutil.Process = unavailable
    monkeypatch.setitem(sys.modules, "psutil", psutil)
    monkeypatch.setattr(blueprint.multiprocessing, "get_context", lambda _kind: Context())

    with pytest.raises(
        blueprint.CookieSTLError,
        match="resource_attestation_unavailable",
    ):
        blueprint._generate_bounded(b"synthetic", blueprint.CookieParameters())

    assert worker.is_alive() is False
    assert worker.exitcode == 0


def test_generation_uses_only_parent_sampled_rss(monkeypatch):
    import api.blueprints.cookie_cutter as blueprint

    summary_fixture = _summary_fixture()
    bundle_fixture = _synthetic_bundle(summary_fixture)

    class Parent:
        def __init__(self):
            self.polls = 0

        def poll(self, _timeout):
            self.polls += 1
            return self.polls >= 2

        def recv(self):
            return "ok", bundle_fixture, summary_fixture

        def close(self):
            return None

    class Child:
        def close(self):
            return None

    class Worker:
        pid = 4321

        def __init__(self):
            self.running = False
            self.exitcode = None

        def start(self):
            self.running = True

        def is_alive(self):
            return self.running

        def join(self, _timeout):
            self.running = False
            self.exitcode = 0

        def terminate(self):
            raise AssertionError("clean child must not be terminated")

        def kill(self):
            raise AssertionError("clean child must not be killed")

    parent, child, worker = Parent(), Child(), Worker()
    monitor_ready = _MonitorReady()

    class Context:
        def Pipe(self, *, duplex):
            assert duplex is False
            return parent, child

        def Event(self):
            return monitor_ready

        def Process(self, **_kwargs):
            return worker

    psutil = types.ModuleType("psutil")
    psutil.Process = lambda _pid: types.SimpleNamespace(
        memory_info=lambda: types.SimpleNamespace(rss=4096)
    )
    monkeypatch.setitem(sys.modules, "psutil", psutil)
    monkeypatch.setattr(blueprint.multiprocessing, "get_context", lambda _kind: Context())

    bundle, summary = blueprint._generate_bounded(
        b"synthetic", blueprint.CookieParameters()
    )

    assert summary["peak_rss_bytes"] == 4096
    assert monitor_ready.set_calls == 1
    with zipfile.ZipFile(io.BytesIO(bundle)) as archive:
        assert json.loads(archive.read("parameters.json")) == summary


@pytest.mark.parametrize("malicious_peak", [-1, 10**12])
def test_generation_rejects_child_supplied_rss_attestation(monkeypatch, malicious_peak):
    import api.blueprints.cookie_cutter as blueprint

    summary_fixture = _summary_fixture()
    bundle_fixture = _synthetic_bundle(summary_fixture)

    class Parent:
        def __init__(self):
            self.polls = 0

        def poll(self, _timeout):
            self.polls += 1
            return self.polls >= 2

        def recv(self):
            return (
                "ok",
                bundle_fixture,
                summary_fixture,
                {"peak_rss_bytes": malicious_peak},
            )

        def close(self):
            return None

    class Child:
        def close(self):
            return None

    class Worker:
        pid = 4321

        def __init__(self):
            self.running = False
            self.exitcode = None

        def start(self):
            self.running = True

        def is_alive(self):
            return self.running

        def join(self, _timeout):
            self.running = False
            self.exitcode = 0

        def terminate(self):
            raise AssertionError("clean child must not be terminated")

        def kill(self):
            raise AssertionError("clean child must not be killed")

    parent, child, worker = Parent(), Child(), Worker()

    class Context:
        def Pipe(self, *, duplex):
            assert duplex is False
            return parent, child

        def Event(self):
            return _MonitorReady()

        def Process(self, **_kwargs):
            return worker

    psutil = types.ModuleType("psutil")
    psutil.Process = lambda _pid: types.SimpleNamespace(
        memory_info=lambda: types.SimpleNamespace(rss=4096)
    )
    monkeypatch.setitem(sys.modules, "psutil", psutil)
    monkeypatch.setattr(blueprint.multiprocessing, "get_context", lambda _kind: Context())

    with pytest.raises(blueprint.CookieSTLError, match="generation_failed"):
        blueprint._generate_bounded(b"synthetic", blueprint.CookieParameters())


def test_generation_timeout_reaps_child_and_returns_no_unattested_bundle(monkeypatch):
    import api.blueprints.cookie_cutter as blueprint

    class Connection:
        def __init__(self):
            self.closed = False

        def poll(self, _timeout):
            return False

        def close(self):
            self.closed = True

    class Worker:
        pid = 4321

        def __init__(self):
            self.running = False
            self.exitcode = None
            self.terminated = False

        def start(self):
            self.running = True

        def is_alive(self):
            return self.running

        def join(self, _timeout):
            return None

        def terminate(self):
            self.terminated = True
            self.running = False
            self.exitcode = -15

        def kill(self):
            raise AssertionError("terminate should reap the synthetic child")

    parent, child, worker = Connection(), Connection(), Worker()

    class Context:
        def Pipe(self, *, duplex):
            assert duplex is False
            return parent, child

        def Event(self):
            return _MonitorReady()

        def Process(self, **_kwargs):
            return worker

    psutil = types.ModuleType("psutil")
    psutil.Process = lambda _pid: types.SimpleNamespace(
        memory_info=lambda: types.SimpleNamespace(rss=1024)
    )
    monkeypatch.setitem(sys.modules, "psutil", psutil)
    monkeypatch.setattr(blueprint.multiprocessing, "get_context", lambda _kind: Context())
    monkeypatch.setattr(blueprint, "MAX_GENERATION_SECONDS", 0)

    with pytest.raises(blueprint.CookieSTLError, match="generation_resource_limit"):
        blueprint._generate_bounded(b"synthetic", blueprint.CookieParameters())

    assert worker.terminated is True
    assert worker.exitcode == -15
    assert parent.closed is True and child.closed is True


def test_generation_fails_closed_when_child_cannot_be_reaped(monkeypatch):
    import api.blueprints.cookie_cutter as blueprint

    summary_fixture = _summary_fixture()
    bundle_fixture = _synthetic_bundle(summary_fixture)

    class Parent:
        def __init__(self):
            self.closed = False

        def poll(self, _timeout):
            return True

        def recv(self):
            return "ok", bundle_fixture, summary_fixture

        def close(self):
            self.closed = True

    class Child:
        def __init__(self):
            self.closed = False

        def close(self):
            self.closed = True

    class Worker:
        pid = 4321
        exitcode = None

        def start(self):
            return None

        def is_alive(self):
            return True

        def join(self, _timeout):
            return None

        def terminate(self):
            return None

        def kill(self):
            return None

    parent, child, worker = Parent(), Child(), Worker()

    class Context:
        def Pipe(self, *, duplex):
            assert duplex is False
            return parent, child

        def Event(self):
            return _MonitorReady()

        def Process(self, **_kwargs):
            return worker

    psutil = types.ModuleType("psutil")
    psutil.Process = lambda _pid: types.SimpleNamespace(
        memory_info=lambda: types.SimpleNamespace(rss=1024)
    )
    monkeypatch.setitem(sys.modules, "psutil", psutil)
    monkeypatch.setattr(blueprint.multiprocessing, "get_context", lambda _kind: Context())

    with pytest.raises(
        blueprint.CookieSTLError,
        match="generation_resource_cleanup_failed",
    ):
        blueprint._generate_bounded(b"synthetic", blueprint.CookieParameters())

    assert parent.closed is True and child.closed is True
