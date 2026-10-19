"""App-configured single-page scans; no real scanner, HA or Lexware calls."""
from __future__ import annotations

import asyncio
import copy
import dataclasses
import io
import re
import json
from pathlib import Path
from archive_import import parse_startup_options
from unittest.mock import AsyncMock, Mock

import pytest
import yaml
from aiohttp import web
from aiohttp.test_utils import TestServer, make_mocked_request
from PIL import Image

from bridge import (BridgeError, CSRF_KEY, JPG_PIPELINE, Manager, SCAN_CONFIGURATION_FIELDS,
                    ScannerClient, Settings, Store, configured_scan_profile, make_app)

DEVICE = "hpaio:/net/test?ip=192.168.1.10&queue=false"
FILE_ID = "a7c66ab7-874c-49d5-8f29-f369af0fc52f"


def device():
    return {"id": DEVICE, "features": {
        "--resolution": {"options": [75, 100, 200, 300, 600, 1200]},
        "--source": {"options": ["Flatbed", "ADF"]},
        "--mode": {"options": ["Color", "Gray", "Lineart"]},
        "-l": {"limits": [0, 215.9]}, "-t": {"limits": [0, 297]},
        "-x": {"limits": [0, 215.9]}, "-y": {"limits": [0, 297]},
        "--page-width": {"limits": [0, 215.9]}, "--page-height": {"limits": [0, 297]},
        "--brightness": {"limits": [0, 2000]}, "--contrast": {"limits": [0, 2000]},
    }, "settings": {
        "pipeline": {"options": [JPG_PIPELINE, "PNG"]},
        "filters": {"options": ["filter.auto-level"], "default": ["filter.auto-level"]},
        "batchMode": {"options": ["none", "auto"], "default": "auto"},
    }}


def options(**updates):
    return Settings.from_dict({"scanner_device_id": DEVICE, **updates})


@pytest.mark.parametrize("resolution", [75, 100, 200, 300, 600, 1200])
def test_resolution_dropdown_exact_strings_and_internal_integers(resolution):
    assert options(scan_resolution=str(resolution)).scan_resolution == resolution
    assert type(options(scan_resolution=str(resolution)).scan_resolution) is int
    assert options(scan_resolution=resolution).scan_resolution == resolution


@pytest.mark.parametrize("value", ["075", " 75", "75 ", "75.0", "+75", "417", 417, True, False, 75.0, None])
def test_resolution_cannot_accept_an_unadvertised_dropdown_value(value):
    with pytest.raises(BridgeError):
        options(scan_resolution=value)


@pytest.mark.parametrize("name,value", [
    ("scan_mode", "RGB"), ("scan_mode", False), ("scan_mode", []),
    ("scan_format", "PDF"), ("scan_format", "JPEG"), ("scan_paper_size", "Letter"),
    ("scan_paper_size", "full"), ("scan_brightness", -1), ("scan_brightness", 2001),
    ("scan_contrast", 2001), ("scan_contrast", "1000"), ("scan_contrast", True),
])
def test_config_rejects_unsupported_enums_and_lighting(name, value):
    with pytest.raises(BridgeError):
        options(**{name: value})


def test_package_configuration_dropdowns_and_prepared_defaults_match_backend():
    root = Path(__file__).resolve().parents[1]
    config = yaml.safe_load((root / "config.yaml").read_text())
    prepared = json.loads((root / "options.prepared.json").read_text())
    assert re.fullmatch(r"\d+\.\d+\.\d+", config["version"])
    assert config["schema"]["scan_resolution"] == "list(75|100|200|300|600|1200)"
    assert config["schema"]["scan_mode"] == "list(Color|Gray|Lineart)"
    assert config["schema"]["scan_format"] == "list(JPG|PNG)"
    assert config["schema"]["scan_paper_size"] == "list(A4|A5|A6|Full)"
    for raw in (config["options"], prepared):
        settings, _ = parse_startup_options(raw)
        assert raw["scan_resolution"] == "300" and settings.scan_resolution == 300
        assert settings.scan_mode == "Lineart" and settings.scan_format == "JPG"
        assert settings.scan_paper_size == "A4"
        assert settings.scan_brightness == settings.scan_contrast == 1000
        assert not settings.scan_enabled and not settings.upload_enabled
        assert not settings.mcp_token and not settings.api_token


@pytest.mark.parametrize("paper,width,height", [("A4", 210, 297), ("A5", 148, 210), ("A6", 105, 148),
                                                ("Full", 215.9, 297)])
@pytest.mark.parametrize("format,pipeline", [("JPG", JPG_PIPELINE), ("PNG", "PNG")])
def test_profile_is_derived_only_from_app_options_and_device_dimensions(paper, width, height, format, pipeline):
    settings = options(scan_resolution="75", scan_mode="Lineart", scan_format=format,
                       scan_paper_size=paper, scan_brightness=0, scan_contrast=2000)
    advertised = device()
    before = copy.deepcopy(advertised)
    profile = configured_scan_profile(settings, advertised)
    assert advertised == before
    assert profile == {"params": {
        "deviceId": DEVICE, "resolution": 75, "source": "Flatbed", "mode": "Lineart",
        "left": 0, "top": 0, "width": width, "height": height,
        "pageWidth": width, "pageHeight": height, "brightness": 0, "contrast": 2000,
    }, "pipeline": pipeline, "filters": [], "batch": "none", "index": 1}


@pytest.mark.parametrize("inactive", [True, False])
def test_only_active_advertised_optional_features_are_sent(inactive):
    advertised = device()
    optional = ("--mode", "--brightness", "--contrast", "--page-width", "--page-height")
    for feature in optional:
        if inactive:
            advertised["features"][feature]["enabled"] = False
        else:
            del advertised["features"][feature]
    # These capabilities have no App option and must not turn the scan into an ADF job.
    advertised["features"]["--adf-mode"] = {"options": ["Duplex"], "default": "Duplex"}
    advertised["features"]["--ald"] = {"options": ["yes", "no"], "default": "yes"}
    profile = configured_scan_profile(options(), advertised)
    assert set(profile["params"]) == {"deviceId", "resolution", "source", "left", "top", "width", "height"}
    assert profile["params"]["source"] == "Flatbed" and profile["batch"] == "none"


def test_full_geometry_respects_nonzero_device_origin():
    advertised = device()
    advertised["features"]["-l"]["limits"][0] = 5
    advertised["features"]["-t"]["limits"][0] = 10
    profile = configured_scan_profile(options(scan_paper_size="Full"), advertised)
    assert profile["params"]["left"] == 5 and profile["params"]["top"] == 10
    assert profile["params"]["width"] == profile["params"]["pageWidth"] == 210.9
    assert profile["params"]["height"] == profile["params"]["pageHeight"] == 287


@pytest.mark.parametrize("change", [
    "resolution", "resolution_inactive", "source", "source_missing", "source_inactive", "mode", "pipeline",
    "batch", "brightness", "contrast", "paper_too_large", "nonfinite", "negative_origin", "empty_extent",
])
def test_configured_profile_is_rejected_before_scan_if_capabilities_do_not_match(change):
    advertised = device()
    features = advertised["features"]
    if change == "resolution": features["--resolution"]["options"] = [75]
    elif change == "resolution_inactive": features["--resolution"]["enabled"] = False
    elif change == "source": features["--source"]["options"] = ["ADF"]
    elif change == "source_missing": del features["--source"]
    elif change == "source_inactive": features["--source"]["enabled"] = False
    elif change == "mode": features["--mode"]["options"] = ["Gray"]
    elif change == "pipeline": advertised["settings"]["pipeline"]["options"] = ["PNG"]
    elif change == "batch": advertised["settings"]["batchMode"]["options"] = ["auto"]
    elif change in {"brightness", "contrast"}: features["--" + change]["limits"] = [0, 500]
    elif change == "paper_too_large": features["-x"]["limits"] = [0, 100]
    elif change == "nonfinite": features["-x"]["limits"] = [0, float("inf")]
    elif change == "negative_origin": features["-l"]["limits"] = [-1, 215.9]
    elif change == "empty_extent": features["-x"]["limits"] = [0, 0]
    with pytest.raises(BridgeError):
        configured_scan_profile(options(scan_paper_size="Full" if change == "empty_extent" else "A4"), advertised)


async def configured_fake_scan(settings, advertised, snapshot=None):
    calls = []

    async def get_context(request):
        calls.append("context")
        return web.json_response({"devices": [advertised]})

    async def post_scan(request):
        calls.append(await request.json())
        return web.json_response({"file": {"name": "precise_returned_file.jpg", "fullname": "/ignored/latest.jpg"}})

    app = web.Application()
    app.router.add_get("/api/v1/context", get_context)
    app.router.add_post("/api/v1/scan", post_scan)
    server = TestServer(app)
    await server.start_server()
    settings = dataclasses.replace(settings, scanserv_url=str(server.make_url("/")).rstrip("/"))
    scanner = ScannerClient(settings)
    try:
        result = await scanner.scan(snapshot)
        return result, calls, scanner.last_scan_profile
    except BridgeError as exc:
        return exc, calls, scanner.last_scan_profile
    finally:
        await server.close()


@pytest.mark.asyncio
@pytest.mark.loopback
async def test_scanner_posts_one_derived_profile_and_returns_exact_job_filename():
    settings = options(scan_resolution="75", scan_mode="Lineart", scan_paper_size="Full")
    result, calls, recorded = await configured_fake_scan(settings, device())
    assert result == "precise_returned_file.jpg"
    assert calls == ["context", configured_scan_profile(settings, device())]
    assert recorded == calls[1]


@pytest.mark.asyncio
@pytest.mark.loopback
async def test_scanner_uses_the_frozen_job_configuration():
    original = options(scan_resolution="75", scan_mode="Lineart", scan_paper_size="Full")
    snapshot = {name: getattr(original, name) for name in SCAN_CONFIGURATION_FIELDS}
    later = dataclasses.replace(original, scan_resolution=600, scan_mode="Color", scan_paper_size="A4")
    result, calls, _ = await configured_fake_scan(later, device(), snapshot)
    assert result == "precise_returned_file.jpg"
    assert calls[1] == configured_scan_profile(original, device())


@pytest.mark.asyncio
@pytest.mark.loopback
async def test_context_mismatch_has_no_post_or_automatic_retry():
    advertised = device()
    advertised["features"]["--resolution"]["options"] = [75]
    result, calls, recorded = await configured_fake_scan(options(), advertised)
    assert isinstance(result, BridgeError) and result.code == "resolution_unavailable"
    assert calls == ["context"] and recorded is None


async def dispatch(app, method, path, body=None, headers=None, peer="172.30.32.2"):
    transport = Mock()
    transport.get_extra_info.return_value = (peer, 12345)
    request = make_mocked_request(method, path, headers=headers or {}, app=app, transport=transport)
    request.json = AsyncMock(return_value=body)
    match = await app.router.resolve(request)
    match.add_app(app)
    request._match_info = match
    return await app.middlewares[0](request, match.handler)


@pytest.mark.asyncio
@pytest.mark.parametrize("scan_enabled,upload_enabled,code", [(False, True, "scan_disabled"),
                                                              (True, False, "upload_disabled")])
async def test_public_scan_requires_both_gates_even_with_an_empty_request(tmp_path, scan_enabled, upload_enabled, code):
    settings = dataclasses.replace(options(), scan_enabled=scan_enabled, upload_enabled=upload_enabled)
    store = Store(tmp_path / "data", tmp_path / "share", settings)
    scanner = Mock()
    manager = Manager(settings, store, scanner)
    app = make_app(manager)
    try:
        response = await dispatch(app, "POST", "/api/scan", {},
                                  {"Content-Type": "application/json", "X-CSRF-Token": app[CSRF_KEY]})
        assert response.status == 409 and json.loads(response.body)["code"] == code
        assert not manager.tasks and not scanner.mock_calls and not store.recent()
    finally:
        store.close()


@pytest.mark.asyncio
async def test_local_status_and_busy_cover_all_phases_through_upload_session_cleanup(tmp_path):
    settings = dataclasses.replace(options(), scan_enabled=True, upload_enabled=True,
                                   mcp_url="http://lexware-mcp:8080/mcp", mcp_token="t" * 32)
    store = Store(tmp_path / "data", tmp_path / "share", settings)
    scan_started, finish_scan, download_started, finish_download = (asyncio.Event() for _ in range(4))
    upload_started, finish_upload, cleanup_started, finish_cleanup = (asyncio.Event() for _ in range(4))
    calls = []
    buffer = io.BytesIO()
    Image.new("RGB", (40, 60), "white").save(buffer, "JPEG")
    source = buffer.getvalue()

    class Scanner:
        async def scan(self, selected):
            calls.append("scan")
            scan_started.set()
            await finish_scan.wait()
            return "reported_this_job.jpg"
        async def download(self, name):
            assert name == "reported_this_job.jpg"
            calls.append("download")
            download_started.set()
            await finish_download.wait()
            return source

    class MCP:
        async def __aenter__(self): return self
        async def __aexit__(self, *args):
            cleanup_started.set()
            await finish_cleanup.wait()
        async def preflight(self): calls.append("preflight")
        async def upload(self, data, filename, mime):
            assert data == source and mime == "image/jpeg"
            calls.append("upload")
            upload_started.set()
            await finish_upload.wait()
            return {"id": FILE_ID}

    manager = Manager(settings, store, Scanner(), lambda _: MCP())
    app = make_app(manager)
    headers = {"Content-Type": "application/json", "X-CSRF-Token": app[CSRF_KEY]}
    try:
        initial = manager.public_status()
        assert calls == [] and initial["device_state"]["state"] == "idle"
        assert initial["scanner_settings_source"] == "ha_app_configuration"
        assert initial["scan_profile"] == {"resolution": 1200, "mode": "Color", "format": "JPG",
                                            "paper_size": "A4", "brightness": 1000, "contrast": 1000,
                                            "source": "Flatbed"}
        response = await dispatch(app, "POST", "/api/scan", {}, headers)
        assert response.status == 202
        job_id = json.loads(response.body)["job"]["id"]
        await asyncio.wait_for(scan_started.wait(), 2)
        for expected_phase, stage, steps in [("scanning", "scan", 0), ("receiving", "archive", 1),
                                              ("uploading", "upload", 2), ("uploaded", "complete", 3)]:
            if expected_phase == "receiving":
                finish_scan.set()
                await asyncio.wait_for(download_started.wait(), 2)
            elif expected_phase == "uploading":
                finish_download.set()
                await asyncio.wait_for(upload_started.wait(), 2)
            elif expected_phase == "uploaded":
                finish_upload.set()
                await asyncio.wait_for(cleanup_started.wait(), 2)
            status_response = await dispatch(app, "GET", "/api/status")
            state = json.loads(status_response.body)
            assert state["busy"] and state["active_job_id"] == job_id
            assert state["active_job"]["status"] == expected_phase
            assert state["device_state"]["state"] == "busy"
            assert state["progress"]["stage"] == stage and state["progress"]["completed_steps"] == steps
            assert state["progress"]["total_steps"] == 3 and state["progress"]["source"] == "local_journal"
            assert "percent" not in state["progress"]
            response = await dispatch(app, "POST", "/api/scan", {}, headers)
            assert response.status == 409 and json.loads(response.body)["code"] == "busy"
            assert calls.count("scan") == 1
        finish_cleanup.set()
        await asyncio.gather(*tuple(manager.tasks))
        await asyncio.sleep(0)
        final = manager.public_status()
        assert not final["busy"] and final["active_job"] is None and final["active_job_id"] is None
        assert final["device_state"]["state"] == "idle" and final["progress"]["completed_steps"] == 3
        assert calls == ["scan", "download", "preflight", "upload"]
    finally:
        finish_scan.set(); finish_download.set(); finish_upload.set(); finish_cleanup.set()
        await manager.shutdown()
        store.close()


@pytest.mark.asyncio
async def test_failed_scan_remains_visible_as_local_attention_without_status_network_calls(tmp_path):
    settings = dataclasses.replace(options(), scan_enabled=True, upload_enabled=True)
    store = Store(tmp_path / "data", tmp_path / "share", settings)
    scanner = Mock()
    scanner.scan = AsyncMock(side_effect=BridgeError("scanner_unavailable", "Scanner-Vorpruefung fehlgeschlagen.", 502))
    manager = Manager(settings, store, scanner)
    try:
        manager.launch_scan()
        await asyncio.gather(*tuple(manager.tasks))
        await asyncio.sleep(0)
        for _ in range(3):
            state = manager.public_status()
            assert state["device_state"]["state"] == "attention"
            assert state["progress"]["stage"] == "error" and state["progress"]["completed_steps"] == 0
            assert state["jobs"][0]["error"]["code"] == "scanner_unavailable"
            assert not state["busy"]
        assert scanner.scan.await_count == 1 and scanner.mock_calls == [scanner.mock_calls[0]]
    finally:
        await manager.shutdown()
        store.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("value", ["", "not-a-uuid", "A7C66AB7-874C-49D5-8F29-F369AF0FC52F",
                                   FILE_ID + "\n", "../" + FILE_ID, "{\"id\":\"" + FILE_ID + "\"}"])
async def test_invalid_request_correlation_header_cannot_start_scan(tmp_path, value):
    settings = dataclasses.replace(options(), scan_enabled=True, upload_enabled=True)
    store = Store(tmp_path / "data", tmp_path / "share", settings)
    scanner = Mock()
    manager = Manager(settings, store, scanner)
    app = make_app(manager)
    try:
        response = await dispatch(app, "POST", "/api/scan", {},
                                  {"Content-Type": "application/json", "X-CSRF-Token": app[CSRF_KEY],
                                   "X-Scan-Request-ID": value})
        assert response.status == 400 and json.loads(response.body)["code"] == "invalid_scan_request_id"
        assert not manager.tasks and not scanner.mock_calls and not store.recent()
    finally:
        store.close()


@pytest.mark.asyncio
async def test_request_correlation_is_persisted_and_reuse_never_starts_a_second_scan(tmp_path):
    settings = dataclasses.replace(options(), scan_enabled=True, upload_enabled=True)
    store = Store(tmp_path / "data", tmp_path / "share", settings)
    scanner = Mock()
    scanner.scan = AsyncMock(side_effect=BridgeError("scanner_unavailable", "Lokaler Testfehler.", 502))
    manager = Manager(settings, store, scanner)
    app = make_app(manager)
    request_id = "e5509277-7602-4a77-a9e6-9bb46c86d912"
    headers = {"Content-Type": "application/json", "X-CSRF-Token": app[CSRF_KEY],
               "X-Scan-Request-ID": request_id}
    try:
        response = await dispatch(app, "POST", "/api/scan", {}, headers)
        assert response.status == 202
        job = json.loads(response.body)["job"]
        assert job["scan_request_id"] == request_id and job["want_upload"] is True
        await asyncio.gather(*tuple(manager.tasks))
        await asyncio.sleep(0)
        assert manager.public_status()["jobs"][0]["scan_request_id"] == request_id
        assert json.loads((store.directory(job) / "status.json").read_text())["scan_request_id"] == request_id
        response = await dispatch(app, "POST", "/api/scan", {}, headers)
        assert response.status == 409 and json.loads(response.body)["code"] == "scan_request_already_used"
        assert scanner.scan.await_count == 1 and len(store.recent()) == 1
    finally:
        await manager.shutdown()
        store.close()
    reopened = Store(tmp_path / "data", tmp_path / "share", settings)
    restarted = Manager(settings, reopened, scanner)
    try:
        assert restarted.public_status()["jobs"][0]["scan_request_id"] == request_id
        with pytest.raises(BridgeError, match="bereits gestartet"):
            restarted.launch_scan(scan_request_id=request_id)
        assert scanner.scan.await_count == 1
    finally:
        reopened.close()
