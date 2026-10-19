"""Scanner browser-profile validation using only disposable local fake services."""
from __future__ import annotations

import copy
import dataclasses

import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer

from bridge import BridgeError, ScannerClient, Settings, sanitize_scan_profile

DEVICE = "hpaio:/net/test?ip=192.168.1.10&queue=false"


def profile():
    return {
        "version": "3.0.3",
        "params": {
            "deviceId": DEVICE, "resolution": "600", "source": "Flatbed", "mode": "Gray",
            "left": "10.2", "top": "2.5", "width": "120.0", "height": "180.5",
            "brightness": "1050", "contrast": "950",
        },
        "pipeline": "PNG", "filters": ["filter.auto-level"], "batch": "none", "index": 1,
    }


def context():
    return {"devices": [{
        "id": DEVICE,
        "features": {
            "--resolution": {"options": [75, 150, 300, 600, 1200]},
            "--source": {"options": ["Flatbed", "ADF"]},
            "--mode": {"options": ["Color", "Gray", "Lineart"]},
            "-l": {"limits": [0, 210]}, "-t": {"limits": [0, 297]},
            "-x": {"limits": [0, 210]}, "-y": {"limits": [0, 297]},
            "--brightness": {"limits": [1, 2000]}, "--contrast": {"limits": [1, 2000]},
        },
        "settings": {
            "pipeline": {"options": ["PNG", "JPG | @:pipeline.high-quality"], "default": "PNG"},
            "filters": {"options": ["filter.auto-level"], "default": []},
            "batchMode": {"options": ["none", "auto"], "default": "none"},
        },
    }]}


def test_browser_profile_numbers_are_copied_without_mutating_storage():
    raw = profile()
    saved = copy.deepcopy(raw)
    clean = sanitize_scan_profile(raw, DEVICE)
    assert raw == saved
    assert "version" not in clean
    assert clean["params"]["resolution"] == 600
    assert type(clean["params"]["resolution"]) is int
    assert clean["params"]["left"] == 10.2
    assert clean["params"]["width"] == 120
    assert clean["params"]["brightness"] == 1050
    assert clean["params"]["mode"] == "Gray"
    assert clean["pipeline"] == "PNG"
    clean["filters"].clear()
    assert raw["filters"] == ["filter.auto-level"]


@pytest.mark.parametrize("key,value", [
    ("resolution", True), ("resolution", 74), ("resolution", 1201), ("resolution", 300.5),
    ("width", 0), ("left", -1), ("width", "NaN"), ("width", float("inf")),
    ("contrast", {}), ("brightness", "1e3"), ("mode", True),
    ("dynamicLineart", "false"), ("source", "ADF"), ("deviceId", "another-scanner"),
    ("isPreview", True), ("serverUrl", "http://example.com"),
])
def test_invalid_or_unscoped_parameters_rejected(key, value):
    raw = profile()
    raw["params"][key] = value
    with pytest.raises(BridgeError):
        sanitize_scan_profile(raw, DEVICE)


@pytest.mark.parametrize("change", [
    {"pipeline": "PDF (JPG | @:pipeline.high-quality)"},
    {"pipeline": "Custom image command"}, {"pipeline": []},
    {"filters": "filter.auto-level"}, {"filters": ["filter.auto-level"] * 2},
    {"filters": [{}]}, {"batch": "auto"}, {"index": -1}, {"index": True},
    {"filename": "private.jpg"}, {"url": "http://another-service"},
])
def test_unsupported_outputs_and_request_fields_rejected(change):
    raw = profile()
    raw.update(change)
    with pytest.raises(BridgeError):
        sanitize_scan_profile(raw, DEVICE)


@pytest.mark.parametrize("missing", ["params", "pipeline", "filters"])
def test_missing_browser_selection_has_no_implicit_profile(missing):
    raw = profile()
    del raw[missing]
    with pytest.raises(BridgeError):
        sanitize_scan_profile(raw, DEVICE)


async def fake_scan(raw, ctx):
    calls = []

    async def get_context(request):
        calls.append("context")
        return web.json_response(ctx)

    async def post_scan(request):
        calls.append(await request.json())
        return web.json_response({"file": {"name": "this_job.png", "fullname": "/untrusted/path/this_job.png"}})

    app = web.Application()
    app.router.add_get("/api/v1/context", get_context)
    app.router.add_post("/api/v1/scan", post_scan)
    server = TestServer(app)
    await server.start_server()
    settings = Settings.from_dict({"scanner_device_id": DEVICE})
    settings = dataclasses.replace(settings, scanserv_url=str(server.make_url("/")).rstrip("/"))
    try:
        client = ScannerClient(settings)
        return await client.scan(raw), calls
    except BridgeError as exc:
        return exc, calls
    finally:
        await server.close()


@pytest.mark.asyncio
@pytest.mark.loopback
async def test_selected_profile_is_sent_exactly_once_with_reported_filename():
    raw = profile()
    name, calls = await fake_scan(raw, context())
    assert name == "this_job.png"
    assert calls == ["context", sanitize_scan_profile(raw, DEVICE)]
    assert calls[1]["params"]["mode"] == "Gray"
    assert calls[1]["params"]["resolution"] == 600
    assert calls[1]["params"]["height"] == 180.5
    assert calls[1]["pipeline"] == "PNG"


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", [
    "resolution", "mode", "source", "pipeline", "filter", "brightness", "crop",
    "missing_width", "missing_mode", "missing_brightness", "unsupported_page_width",
    "nonfinite_limits", "malformed_context", "missing_batch_none", "duplicate_device",
])
@pytest.mark.loopback
async def test_capability_mismatch_never_sends_a_scan(kind):
    raw, ctx = profile(), context()
    device = ctx["devices"][0]
    if kind == "resolution":
        raw["params"]["resolution"] = 900
    elif kind == "mode":
        raw["params"]["mode"] = "Alien"
    elif kind == "source":
        device["features"]["--source"]["options"] = ["ADF"]
    elif kind == "pipeline":
        device["settings"]["pipeline"]["options"] = []
    elif kind == "filter":
        raw["filters"] = ["unavailable"]
    elif kind == "brightness":
        raw["params"]["brightness"] = 2001
    elif kind == "crop":
        raw["params"]["width"] = 205
    elif kind.startswith("missing_") and kind != "missing_batch_none":
        del raw["params"][kind.removeprefix("missing_")]
    elif kind == "unsupported_page_width":
        raw["params"]["pageWidth"] = 210
    elif kind == "nonfinite_limits":
        device["features"]["-x"]["limits"] = [0, float("nan")]
    elif kind == "malformed_context":
        ctx = {"devices": "not-a-list"}
    elif kind == "missing_batch_none":
        device["settings"]["batchMode"]["options"] = ["auto"]
    elif kind == "duplicate_device":
        ctx["devices"].append(copy.deepcopy(device))
    result, calls = await fake_scan(raw, ctx)
    assert isinstance(result, BridgeError)
    assert calls == ["context"]


@pytest.mark.asyncio
@pytest.mark.loopback
async def test_invalid_profile_never_contacts_even_context():
    raw = profile()
    raw["params"]["source"] = "ADF"
    result, calls = await fake_scan(raw, context())
    assert isinstance(result, BridgeError)
    assert calls == []


@pytest.mark.asyncio
@pytest.mark.loopback
async def test_supported_optional_params_preserved():
    raw, ctx = profile(), context()
    raw["params"].update({"pageWidth": "150", "pageHeight": "200", "adfMode": "Simplex",
                          "dynamicLineart": False, "ald": "no"})
    ctx["devices"][0]["features"].update({
        "--page-width": {"limits": [0, 210]}, "--page-height": {"limits": [0, 297]},
        "--adf-mode": {"options": ["Simplex"]}, "--disable-dynamic-lineart": {},
        "--ald": {"options": ["yes", "no"]},
    })
    name, calls = await fake_scan(raw, ctx)
    assert name == "this_job.png"
    assert calls[1]["params"]["pageWidth"] == 150
    assert calls[1]["params"]["dynamicLineart"] is False
    assert calls[1]["params"]["ald"] == "no"
