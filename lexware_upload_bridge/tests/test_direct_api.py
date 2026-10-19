"""Direct API receipt uploads against temporary localhost services only."""
from __future__ import annotations

import dataclasses
import io
import json
from pathlib import Path

import aiohttp
from aiohttp import web
from aiohttp.test_utils import TestServer
from PIL import Image
import pytest
import yaml

import bridge
from bridge import BridgeError, LexwareApiClient, Manager, McpClient, Settings, Store

FILE_ID = "a7c66ab7-874c-49d5-8f29-f369af0fc52f"
VOUCHER_ID = "a7c66ab7-874c-49d5-8f29-f369af0fc530"
KEY = "synthetic-private-api-key-123456789"


def settings(**updates):
    return Settings.from_dict({"upload_backend": "api", "lexware_api_key": KEY,
                               "upload_enabled": True, "scan_enabled": True,
                               "scanner_device_id": "hpaio:/net/test?ip=192.168.1.10&queue=false", **updates})


def image():
    out = io.BytesIO()
    Image.new("RGB", (24, 32), "white").save(out, "JPEG")
    return out.getvalue()


def test_backend_options_and_password_schema_are_explicit():
    root = Path(__file__).resolve().parents[1]
    config = yaml.safe_load((root / "config.yaml").read_text())
    assert config["schema"]["upload_backend"] == "list(mcp|api)"
    assert config["schema"]["lexware_api_key"] == "password"
    assert config["options"]["upload_backend"] == "api"
    assert config["options"]["lexware_api_key"] == ""
    assert Settings.from_dict({}).upload_backend == "mcp"
    assert not Settings.from_dict({}).upload_configured
    assert settings(mcp_url="", mcp_token="").upload_configured
    assert not settings(lexware_api_key="", mcp_url="http://old-mcp:8080/mcp", mcp_token="m" * 32).upload_configured
    assert bridge.LEXWARE_UPLOAD_URL == "https://api.lexware.io/v1/files"


@pytest.mark.parametrize("updates", [
    {"upload_backend": "auto"}, {"upload_backend": None}, {"upload_backend": True},
    {"lexware_api_key": 12}, {"lexware_api_key": "short"},
    {"lexware_api_key": "a" * 20 + "\n"}, {"lexware_api_key": "a" * 20 + "ü"},
    {"lexware_api_key": " " + KEY}, {"lexware_api_key": KEY + " "},
    {"lexware_api_key": "a" * 4097}, {"lexware_api_url": "https://example.com"},
])
def test_bad_config_cannot_select_arbitrary_transport_or_secret(updates):
    with pytest.raises(BridgeError):
        settings(**updates)


@pytest.mark.asyncio
async def test_status_and_startup_make_no_provider_calls_or_leak_keys(tmp_path, monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("No network/session is opened by status or initialization")
    monkeypatch.setattr(aiohttp, "ClientSession", forbidden)
    configured = settings()
    store = Store(tmp_path / "data", tmp_path / "share", configured)
    try:
        manager = Manager(configured, store)
        assert manager.mcp_factory is LexwareApiClient
        status = manager.public_status()
        assert status["upload_backend"] == "api" and status["upload_configured"]
        assert not status["mcp_configured"] and status["device_state"]["state"] == "idle"
        assert KEY not in json.dumps(status) and KEY not in repr(configured)
        legacy = Manager(dataclasses.replace(configured, upload_backend="mcp"), store)
        assert legacy.mcp_factory is McpClient
    finally:
        store.close()


@pytest.mark.asyncio
async def test_missing_key_remains_ready_and_never_uses_legacy_mcp(tmp_path, monkeypatch):
    configured = settings(lexware_api_key="", mcp_url="http://old-mcp:8080/mcp", mcp_token="m" * 32)
    def forbidden(*args, **kwargs):
        raise AssertionError("Must neither create an API session nor fallback to MCP")
    monkeypatch.setattr(aiohttp, "ClientSession", forbidden)
    store = Store(tmp_path / "data", tmp_path / "share", configured)
    try:
        manager = Manager(configured, store)
        assert manager.public_status()["device_state"]["state"] == "not_configured"
        job = store.create(300, True)
        await manager.archive(job["id"], image())
        await manager.run_upload(job["id"])
        saved = store.get(job["id"])
        assert saved["status"] == "ready" and saved["error"]["code"] == "lexware_not_configured"
    finally:
        store.close()


@pytest.mark.asyncio
async def test_api_upload_gate_prevents_all_transport_creation(tmp_path, monkeypatch):
    configured = settings(upload_enabled=False)
    def forbidden(*args, **kwargs):
        raise AssertionError("A disabled upload must not create a session")
    monkeypatch.setattr(aiohttp, "ClientSession", forbidden)
    store = Store(tmp_path / "data", tmp_path / "share", configured)
    try:
        manager = Manager(configured, store)
        job = store.create(300, True)
        await manager.archive(job["id"], image())
        await manager.run_upload(job["id"])
        assert store.get(job["id"])["status"] == "ready"
        assert store.get(job["id"])["error"]["code"] == "upload_disabled"
    finally:
        store.close()


@pytest.mark.loopback
@pytest.mark.asyncio
async def test_exact_multipart_upload_and_202_ack_without_extra_requests(tmp_path, monkeypatch, caplog):
    calls = []
    content = image()
    async def handler(request):
        assert request.method == "POST" and request.path == "/v1/files"
        assert request.headers["Authorization"] == "Bearer " + KEY
        assert request.headers["Accept"] == "application/json"
        parts = {}
        reader = await request.multipart()
        while (part := await reader.next()) is not None:
            parts[part.name] = (part.filename, part.headers.get("Content-Type"), bytes(await part.read()))
        assert set(parts) == {"file", "type"}
        assert parts["file"][1:] == ("image/jpeg", content)
        assert parts["file"][0].startswith("WER_") and parts["file"][0].endswith(".jpg")
        assert parts["type"][2] == b"voucher"
        calls.append(request.path)
        return web.json_response({"id": FILE_ID, "voucherId": VOUCHER_ID, "ignored": KEY}, status=202)
    app = web.Application(); app.router.add_post("/v1/files", handler)
    async with TestServer(app) as server:
        monkeypatch.setattr(bridge, "LEXWARE_UPLOAD_URL", str(server.make_url("/v1/files")))
        store = Store(tmp_path / "data", tmp_path / "share", settings())
        try:
            manager = Manager(settings(), store)
            job = store.create(300, True)
            await manager.archive(job["id"], content)
            await manager.run_upload(job["id"])
            saved = store.get(job["id"])
            assert saved["status"] == "uploaded"
            assert saved["file_id"] == FILE_ID and saved["voucher_id"] == VOUCHER_ID
            ack = (store.directory(saved) / "lexware-response.json").read_text()
            assert KEY not in ack and KEY not in caplog.text
            await manager.run_upload(job["id"])
            assert store.get(job["id"])["status"] == "uploaded"
            assert calls == ["/v1/files"]
        finally:
            store.close()


@pytest.mark.loopback
@pytest.mark.asyncio
@pytest.mark.parametrize("status,body", [
    (200, {"id": FILE_ID}), (201, {"id": FILE_ID}), (401, {"secret": KEY}),
    (403, {}), (429, {}), (500, {}), (202, {"id": "not-a-uuid"}),
    (202, {"id": FILE_ID, "voucherId": "bad"}), (202, []), (202, {"id": 123}),
    (202, "not-json"), (202, "x" * 65_000),
])
async def test_unconfirmed_upload_is_reviewable_and_never_retried(tmp_path, monkeypatch, caplog, status, body):
    calls = []
    async def handler(request):
        calls.append(request.path)
        await request.read()
        return (web.json_response(body, status=status) if not isinstance(body, str)
                else web.Response(text=body, status=status))
    app = web.Application(); app.router.add_post("/v1/files", handler)
    async with TestServer(app) as server:
        monkeypatch.setattr(bridge, "LEXWARE_UPLOAD_URL", str(server.make_url("/v1/files")))
        store = Store(tmp_path / "data", tmp_path / "share", settings())
        try:
            manager = Manager(settings(), store)
            job = store.create(300, True)
            await manager.archive(job["id"], image())
            await manager.run_upload(job["id"])
            assert store.get(job["id"])["status"] == "needs_review"
            await manager.run_upload(job["id"])
            assert calls == ["/v1/files"]
            assert KEY not in json.dumps(store.get(job["id"])) and KEY not in caplog.text
            assert not (store.directory(store.get(job["id"])) / "lexware-response.json").exists()
            store.close()
            store = Store(tmp_path / "data", tmp_path / "share", settings())
            assert store.get(job["id"])["status"] == "needs_review"
            assert not Manager(settings(), store).tasks
        finally:
            store.close()


@pytest.mark.loopback
@pytest.mark.asyncio
async def test_provider_redirect_cannot_forward_key_or_upload(monkeypatch):
    calls = []
    async def upload(request):
        calls.append("upload")
        return web.Response(status=307, headers={"Location": "/unexpected"})
    async def redirected(request):
        calls.append("redirected")
        return web.json_response({"id": FILE_ID}, status=202)
    app = web.Application(); app.router.add_post("/v1/files", upload); app.router.add_post("/unexpected", redirected)
    async with TestServer(app) as server:
        monkeypatch.setattr(bridge, "LEXWARE_UPLOAD_URL", str(server.make_url("/v1/files")))
        async with LexwareApiClient(settings()) as client:
            assert not client.session.trust_env
            assert client.session.timeout.total == 180
            await client.preflight()
            assert not calls
            with pytest.raises(BridgeError, match="HTTP 307"):
                await client.upload(image(), "receipt.jpg", "image/jpeg")
        assert calls == ["upload"]


@pytest.mark.asyncio
async def test_connection_failure_is_single_attempt_with_redacted_error(monkeypatch):
    calls = []
    class BrokenSession:
        closed = False
        def post(self, *args, **kwargs):
            calls.append(kwargs)
            raise aiohttp.ClientConnectionError(KEY)
        async def close(self): self.closed = True
    monkeypatch.setattr(aiohttp, "ClientSession", lambda **_: BrokenSession())
    async with LexwareApiClient(settings()) as client:
        await client.preflight()
        with pytest.raises(BridgeError) as error:
            await client.upload(image(), "receipt.jpg", "image/jpeg")
        assert error.value.code == "lexware_request" and KEY not in str(error.value)
    assert len(calls) == 1 and calls[0]["allow_redirects"] is False


@pytest.mark.asyncio
@pytest.mark.parametrize("data,name,mime", [(b"", "a.jpg", "image/jpeg"),
    (b"x" * 100_001, "a.jpg", "image/jpeg"), (b"x", "../a.jpg", "image/jpeg"),
    (b"x", "a.pdf", "application/pdf"), (b"x", "a.jpg", "text/plain")])
async def test_bad_upload_is_rejected_before_any_request(data, name, mime):
    async with LexwareApiClient(settings(max_upload_bytes=100_000)) as client:
        await client.preflight()
        with pytest.raises(BridgeError):
            client.validate_upload(data, name, mime)

