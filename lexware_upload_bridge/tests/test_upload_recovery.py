"""Offline regressions for acceptance, shutdown and local acknowledgement recovery."""
from __future__ import annotations

import asyncio
import io
import json

import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer
from PIL import Image

from bridge import BridgeError, Manager, Settings, Store

FILE_ID = "a7c66ab7-874c-49d5-8f29-f369af0fc52f"
VOUCHER_ID = "a7c66ab7-874c-49d5-8f29-f369af0fc530"


def receipt():
    out = io.BytesIO()
    Image.new("RGB", (30, 40), "white").save(out, "JPEG")
    return out.getvalue()


class FakeScanner:
    async def scan(self, resolution): return "receipt.jpg"
    async def download(self, filename): return receipt()


class FakeMCP:
    def __init__(self): self.uploads, self.preflights = [], 0
    async def __aenter__(self): return self
    async def __aexit__(self, *args): pass
    async def preflight(self): self.preflights += 1
    async def upload(self, data, filename, mime):
        self.uploads.append((data, filename, mime))
        return {"id": FILE_ID, "voucherId": VOUCHER_ID}


@pytest.fixture
def settings(): return Settings(scan_enabled=True, upload_enabled=True)


@pytest.fixture
def store(tmp_path, settings):
    obj = Store(tmp_path / "data", tmp_path / "share", settings)
    yield obj
    try: obj.close()
    except Exception: pass


async def ready_job(store, manager):
    job = store.create(1200, False)
    await manager.archive(job["id"], receipt())
    return store.get(job["id"])


@pytest.mark.asyncio
@pytest.mark.parametrize("scan_and_upload", [False, True])
async def test_shutdown_during_close_preserves_acceptance_and_duplicate_guard(store, settings, scan_and_upload):
    entered_exit, hold_exit = asyncio.Event(), asyncio.Event()
    class ClosingMCP(FakeMCP):
        async def __aexit__(self, *args):
            if len(self.uploads) == 1:
                entered_exit.set()
                await hold_exit.wait()
    mcp = ClosingMCP()
    manager = Manager(settings, store, FakeScanner(), lambda _: mcp)
    if scan_and_upload: job = manager.launch_scan(1200, True)
    else:
        job = await ready_job(store, manager)
        manager.launch_upload(job["id"])
    await asyncio.wait_for(entered_exit.wait(), timeout=5)
    assert store.get(job["id"])["status"] == "uploaded"
    await manager.shutdown()
    await asyncio.sleep(0)
    assert store.get(job["id"])["status"] == "uploaded"
    assert store.get(job["id"])["file_id"] == FILE_ID
    with pytest.raises(BridgeError): manager.launch_upload(job["id"])
    second = manager.launch_scan(1200, True)
    await asyncio.gather(*tuple(manager.tasks))
    assert store.get(second["id"])["status"] == "duplicate"
    assert len(mcp.uploads) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("scan_and_upload", [False, True])
async def test_close_exception_cannot_downgrade_accepted_job(store, settings, scan_and_upload):
    class FailingCloseMCP(FakeMCP):
        async def __aexit__(self, *args): raise OSError("injected session-close failure")
    mcp = FailingCloseMCP()
    manager = Manager(settings, store, FakeScanner(), lambda _: mcp)
    if scan_and_upload:
        job = store.create(1200, True)
        await manager.run_scan(job["id"])
    else:
        job = await ready_job(store, manager)
        await manager.run_upload(job["id"])
    assert store.get(job["id"])["status"] == "uploaded"
    assert store.get(job["id"])["file_id"] == FILE_ID
    assert len(mcp.uploads) == 1


@pytest.mark.asyncio
@pytest.mark.loopback
async def test_shutdown_in_actual_mcp_client_close_keeps_uploaded(tmp_path):
    uploads = []
    async def handle(request):
        body = await request.json()
        method = body["method"]
        if method == "notifications/initialized": return web.Response(status=202)
        if method == "initialize": result = {"protocolVersion": "2025-06-18"}
        elif method == "tools/list":
            properties = {name: {"type": "string"} for name in ["fileBase64", "filename", "mimeType"]}
            properties["type"] = {"type": "string", "enum": ["voucher"]}
            result = {"tools": [{"name": "upload-file", "inputSchema": {
                "type": "object", "properties": properties,
                "required": ["fileBase64", "filename", "mimeType"],
            }}]}
        elif method == "tools/call":
            uploads.append(body)
            result = {"structuredContent": {"id": FILE_ID}}
        else: raise AssertionError(method)
        return web.json_response({"jsonrpc": "2.0", "id": body["id"], "result": result})
    app = web.Application(); app.router.add_post("/mcp", handle)
    server = TestServer(app); await server.start_server()
    try:
        settings = Settings(upload_enabled=True, mcp_url=str(server.make_url("/mcp")), mcp_token="m" * 32)
        store = Store(tmp_path / "data", tmp_path / "share", settings)
        try:
            manager = Manager(settings, store)
            job = await ready_job(store, manager)
            shutdown_complete = asyncio.Event()
            original_update = store.update
            shutdown_scheduled = False
            async def shutdown():
                await manager.shutdown()
                shutdown_complete.set()
            def observe_update(jid, **changes):
                nonlocal shutdown_scheduled
                record = original_update(jid, **changes)
                if changes.get("status") == "uploaded" and not shutdown_scheduled:
                    shutdown_scheduled = True
                    asyncio.get_running_loop().call_soon(lambda: asyncio.create_task(shutdown()))
                return record
            store.update = observe_update
            manager.launch_upload(job["id"])
            await asyncio.wait_for(shutdown_complete.wait(), timeout=5)
            await asyncio.sleep(0)
            assert store.get(job["id"])["status"] == "uploaded"
            assert len(uploads) == 1
            with pytest.raises(BridgeError): manager.launch_upload(job["id"])
        finally: store.close()
    finally: await server.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("persistent", [False, True])
async def test_ack_survives_failed_accepted_status_commit(tmp_path, settings, persistent):
    store = Store(tmp_path / "data", tmp_path / "share", settings)
    mcp = FakeMCP(); manager = Manager(settings, store, mcp_factory=lambda _: mcp)
    job = await ready_job(store, manager)
    original_save, failed = store.save, False
    def fail_accepted(record):
        nonlocal failed
        if record["status"] == "uploaded" and (persistent or not failed):
            failed = True
            raise OSError("injected local commit failure")
        original_save(record)
    store.save = fail_accepted
    await manager.run_upload(job["id"])
    saved = store.get(job["id"])
    assert saved["status"] == ("uploading" if persistent else "uploaded")
    assert json.loads((store.directory(saved) / "lexware-response.json").read_text())["file_id"] == FILE_ID
    assert len(mcp.uploads) == 1
    store.close()
    reopened = Store(tmp_path / "data", tmp_path / "share", settings)
    try:
        assert reopened.get(job["id"])["status"] == "uploaded"
        assert reopened.get(job["id"])["file_id"] == FILE_ID
        assert len(mcp.uploads) == 1
    finally: reopened.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["uploading", "needs_review", "interrupted", "error", "ready", "preflight"])
async def test_restart_recovers_valid_ack_from_uncertain_or_legacy_state(tmp_path, settings, status):
    store = Store(tmp_path / "data", tmp_path / "share", settings)
    job = await ready_job(store, Manager(settings, store))
    store.save_ack(job, {"id": FILE_ID, "voucherId": VOUCHER_ID})
    store.update(job["id"], status=status); store.close()
    reopened = Store(tmp_path / "data", tmp_path / "share", settings)
    try:
        recovered = reopened.get(job["id"])
        assert recovered["status"] == "uploaded"
        assert recovered["file_id"] == FILE_ID and recovered["voucher_id"] == VOUCHER_ID
        assert recovered["error"] is None
    finally: reopened.close()


@pytest.mark.asyncio
async def test_existing_valid_ack_is_reconciled_without_mcp_call(store, settings):
    def forbidden(_): raise AssertionError("MCP must not be opened")
    manager = Manager(settings, store, mcp_factory=forbidden)
    job = await ready_job(store, manager)
    store.save_ack(job, {"id": FILE_ID})
    await manager.run_upload(job["id"])
    assert store.get(job["id"])["status"] == "uploaded"
    assert store.get(job["id"])["file_id"] == FILE_ID


@pytest.mark.asyncio
@pytest.mark.parametrize("mutation", [
    {"accepted_at": None}, {"accepted_at": "2026-10-11T12:00:00"}, {"voucher_id": "invalid"},
    {"file_id": "invalid"}, {"job_id": FILE_ID}, {"upload_sha256": "0" * 64},
])
async def test_invalid_existing_ack_blocks_upload_and_restart(tmp_path, settings, mutation):
    def forbidden(_): raise AssertionError("MCP must not be opened")
    store = Store(tmp_path / "data", tmp_path / "share", settings)
    manager = Manager(settings, store, mcp_factory=forbidden)
    job = await ready_job(store, manager)
    store.save_ack(job, {"id": FILE_ID})
    path = store.directory(job) / "lexware-response.json"
    ack = json.loads(path.read_text()); ack.update(mutation); path.write_text(json.dumps(ack))
    await manager.run_upload(job["id"])
    assert store.get(job["id"])["status"] == "needs_review"
    store.close()
    reopened = Store(tmp_path / "data", tmp_path / "share", settings)
    try: assert reopened.get(job["id"])["status"] == "needs_review"
    finally: reopened.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["original", "upload"])
async def test_ack_with_changed_archived_file_is_not_accepted(tmp_path, settings, kind):
    store = Store(tmp_path / "data", tmp_path / "share", settings)
    job = await ready_job(store, Manager(settings, store))
    store.save_ack(job, {"id": FILE_ID})
    store.path(job, kind).write_bytes(b"changed")
    store.update(job["id"], status="interrupted"); store.close()
    reopened = Store(tmp_path / "data", tmp_path / "share", settings)
    try:
        assert reopened.get(job["id"])["status"] == "needs_review"
        assert reopened.get(job["id"])["file_id"] is None
    finally: reopened.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("evidence", [{"file_id": FILE_ID}, {"accepted_at": "2026-10-11T12:00:00+00:00"}])
async def test_ready_with_previous_acceptance_evidence_is_not_resent(store, settings, evidence):
    def forbidden(_): raise AssertionError("MCP must not be opened")
    manager = Manager(settings, store, mcp_factory=forbidden)
    job = await ready_job(store, manager); store.update(job["id"], **evidence)
    await manager.run_upload(job["id"])
    assert store.get(job["id"])["status"] == "needs_review"


@pytest.mark.asyncio
async def test_validator_failure_stays_ready_without_upload_intent(store, settings):
    class RejectingMCP(FakeMCP):
        def validate_upload(self, data, filename, mime):
            raise BridgeError("mcp_schema_changed", "Uploadschema geaendert.")
    mcp = RejectingMCP(); manager = Manager(settings, store, mcp_factory=lambda _: mcp)
    job = await ready_job(store, manager)
    await manager.run_upload(job["id"])
    saved = store.get(job["id"])
    assert saved["status"] == "ready" and saved["error"]["code"] == "mcp_schema_changed"
    assert mcp.preflights == 1 and mcp.uploads == []
