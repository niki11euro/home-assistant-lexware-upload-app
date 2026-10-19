"""Focused auth and MCP regression tests; only temporary localhost services."""
from __future__ import annotations

import copy
import dataclasses
import json
from types import SimpleNamespace

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from bridge import BridgeError, Manager, McpClient, Settings, Store, make_app

FILE_ID = "a7c66ab7-874c-49d5-8f29-f369af0fc52f"
GOOD_SCHEMA = {
    "type": "object",
    "properties": {
        "fileBase64": {"type": "string"}, "filename": {"type": "string"},
        "mimeType": {"type": "string", "enum": ["image/jpeg", "image/png"]},
        "type": {"type": "string", "enum": ["voucher"]},
    },
    "required": ["fileBase64", "filename", "mimeType"],
    "additionalProperties": False,
}


class NoNetwork:
    def __getattr__(self, name):
        raise AssertionError("Unexpected scanner access: " + name)


@pytest.mark.asyncio
@pytest.mark.loopback
async def test_non_ascii_bearer_rejected_without_unhandled_error(tmp_path, caplog):
    root = tmp_path.resolve()
    settings = dataclasses.replace(Settings(), api_token="a" * 40)
    store = Store(root / "data", root / "share", settings)
    client = TestClient(TestServer(make_app(Manager(settings, store, NoNetwork()))))
    await client.start_server()
    try:
        for token in ("wrong", "ä"):
            response = await client.get("/api/status", headers={"Authorization": "Bearer " + token})
            assert response.status == 401
        assert (await client.get("/api/status", headers={"Authorization": "Bearer " + settings.api_token})).status == 200
        assert store.recent() == []
        assert "Error handling request" not in caplog.text
        assert settings.api_token not in caplog.text
    finally:
        await client.close()


@pytest.mark.asyncio
@pytest.mark.loopback
async def test_non_ascii_csrf_rejected_without_unhandled_error(tmp_path, caplog):
    root = tmp_path.resolve()
    store = Store(root / "data", root / "share", Settings())
    client = TestClient(TestServer(make_app(Manager(Settings(), store, NoNetwork()),
                                          trusted_peers=frozenset({"127.0.0.1"}))))
    await client.start_server()
    try:
        for token in ("wrong", "ä"):
            response = await client.post("/api/scan", headers={"X-CSRF-Token": token},
                                         json={"confirm_business": True})
            assert response.status == 403
        assert (await client.get("/api/status")).status == 200
        assert store.recent() == []
        assert "Error handling request" not in caplog.text
    finally:
        await client.close()


class FakeContent:
    def __init__(self, chunks):
        self.chunks = chunks

    async def iter_chunked(self, size):
        for chunk in self.chunks:
            yield chunk


@pytest.mark.asyncio
@pytest.mark.parametrize("newline", ["\n", "\r", "\r\n"])
@pytest.mark.parametrize("bom", [False, True])
async def test_sse_framing_bom_and_chunk_boundaries(newline, bom):
    packet = {"jsonrpc": "2.0", "id": 1, "result": {"text": "Größe"}}
    fields = [": heartbeat", "event: message", "id: ignored"]
    fields += ["data: " + line for line in json.dumps(packet, ensure_ascii=False, indent=2).splitlines()]
    payload = (("\ufeff" if bom else "") + newline.join(fields) + newline * 2).encode()
    response = SimpleNamespace(content=FakeContent([bytes([byte]) for byte in payload]))
    result = await McpClient(Settings())._sse(response, 1)
    assert result == packet


@pytest.mark.asyncio
async def test_sse_incomplete_event_is_not_accepted():
    packet = {"jsonrpc": "2.0", "id": 1, "result": {}}
    response = SimpleNamespace(content=FakeContent([b"data: " + json.dumps(packet).encode() + b"\n"]))
    with pytest.raises(BridgeError) as error:
        await McpClient(Settings())._sse(response, 1)
    assert error.value.code == "mcp_stream_incomplete"


@pytest.mark.asyncio
@pytest.mark.loopback
async def test_sse_server_requests_with_same_id_are_not_responses():
    received = []

    async def handle(request):
        body = await request.json()
        received.append(body)
        if "method" not in body:
            return web.Response(status=202)
        rid = body["id"]
        packets = [
            {"jsonrpc": "2.0", "id": rid, "method": "ping"},
            {"jsonrpc": "2.0", "method": "notifications/progress", "params": {}},
            {"jsonrpc": "2.0", "id": "unsupported", "method": "sampling/createMessage", "params": {}},
            {"jsonrpc": "2.0", "id": rid, "result": {"protocolVersion": "2025-06-18"}},
        ]
        return web.Response(body="".join("data: " + json.dumps(p) + "\r\r" for p in packets).encode(),
                            content_type="text/event-stream")

    app = web.Application()
    app.router.add_post("/mcp", handle)
    server = TestServer(app)
    await server.start_server()
    try:
        settings = dataclasses.replace(Settings(), mcp_url=str(server.make_url("/mcp")), mcp_token="m" * 32)
        async with McpClient(settings) as client:
            assert await client.rpc("initialize", {}) == {"protocolVersion": "2025-06-18"}
        assert received == [
            {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
            {"jsonrpc": "2.0", "id": 1, "result": {}},
            {"jsonrpc": "2.0", "id": "unsupported", "error": {"code": -32601, "message": "Method not supported"}},
        ]
    finally:
        await server.close()


@pytest.mark.asyncio
@pytest.mark.loopback
async def test_boolean_response_id_does_not_match_integer_request():
    async def handle(request):
        return web.json_response({"jsonrpc": "2.0", "id": True, "result": {}})

    app = web.Application()
    app.router.add_post("/mcp", handle)
    server = TestServer(app)
    await server.start_server()
    try:
        settings = dataclasses.replace(Settings(), mcp_url=str(server.make_url("/mcp")), mcp_token="m" * 32)
        async with McpClient(settings) as client:
            with pytest.raises(BridgeError) as error:
                await client.rpc("initialize", {})
            assert error.value.code == "mcp_response"
    finally:
        await server.close()


def schema_case(case):
    schema = copy.deepcopy(GOOD_SCHEMA)
    if case == "required":
        schema["required"].append("unexpected")
    elif case == "datatype":
        schema["properties"]["filename"]["type"] = "object"
    elif case == "enum":
        schema["properties"]["type"]["enum"] = ["invoice"]
    elif case == "extra":
        del schema["properties"]["type"]
    elif case == "minLength":
        schema["properties"]["filename"]["minLength"] = 100
    elif case == "maxLength":
        schema["properties"]["fileBase64"]["maxLength"] = 1
    elif case == "pattern":
        schema["properties"]["filename"]["pattern"] = "^allowed.jpg$"
    elif case == "const":
        schema["properties"]["mimeType"]["const"] = "image/png"
    elif case == "anyOf":
        schema["properties"]["type"]["anyOf"] = [{"const": "invoice"}, {"const": "other"}]
    elif case == "oneOf":
        schema["properties"]["type"]["oneOf"] = [{"const": "voucher"}, {"type": "string"}]
    return schema


@pytest.mark.asyncio
@pytest.mark.parametrize("case", ["required", "datatype", "enum", "extra", "minLength", "maxLength", "pattern", "const", "anyOf", "oneOf"])
@pytest.mark.loopback
async def test_announced_schema_blocks_incompatible_arguments_before_tools_call(case):
    received = []

    async def handle(request):
        body = await request.json()
        received.append(body)
        if body["method"] == "notifications/initialized":
            return web.Response(status=202)
        if body["method"] == "initialize":
            result = {"protocolVersion": "2025-06-18"}
        elif body["method"] == "tools/list":
            result = {"tools": [{"name": "upload-file", "inputSchema": schema_case(case)}]}
        else:
            raise AssertionError("Unexpected write request")
        return web.json_response({"jsonrpc": "2.0", "id": body["id"], "result": result})

    app = web.Application()
    app.router.add_post("/mcp", handle)
    server = TestServer(app)
    await server.start_server()
    try:
        settings = dataclasses.replace(Settings(), mcp_url=str(server.make_url("/mcp")), mcp_token="m" * 32)
        async with McpClient(settings) as client:
            await client.preflight()
            with pytest.raises(BridgeError) as error:
                await client.upload(b"test-image", "receipt.jpg", "image/jpeg")
            assert error.value.code == "mcp_schema_changed"
        assert [body["method"] for body in received] == ["initialize", "notifications/initialized", "tools/list"]
    finally:
        await server.close()


@pytest.mark.parametrize("unsupported", [
    {"$ref": "http://127.0.0.1:9/schema"}, {"$ref": "#/properties/filename"},
    {"format": "email"}, {"minimum": 0}, {"type": "integer"},
    {"pattern": "(a+)+$"}, {"pattern": "^a*a*$"}, {"pattern": None}, {"minLength": True},
])
def test_unsupported_or_unsafe_schema_rules_fail_closed(unsupported):
    schema = copy.deepcopy(GOOD_SCHEMA)
    schema["properties"]["filename"].update(unsupported)
    with pytest.raises(BridgeError) as error:
        McpClient._schema_validator(schema)
    assert error.value.code == "mcp_schema_changed"


def test_compatible_schema_and_supported_constraints():
    schema = copy.deepcopy(GOOD_SCHEMA)
    schema["description"] = "Fake upload tool"
    schema["properties"]["filename"].update({"pattern": r"^receipt\.jpg$", "minLength": 1, "maxLength": 200})
    schema["allOf"] = [{"type": "object"}, {"required": ["type"]}]
    schema["properties"]["type"]["anyOf"] = [{"const": "voucher"}, {"const": "invoice"}]
    schema["properties"]["mimeType"]["oneOf"] = [{"const": "image/jpeg"}, {"const": "image/png"}]
    client = McpClient(Settings())
    client._upload_validator = McpClient._schema_validator(schema)
    arguments = client.validate_upload(b"image", "receipt.jpg", "image/jpeg")
    assert arguments == {"fileBase64": "aW1hZ2U=", "filename": "receipt.jpg", "mimeType": "image/jpeg", "type": "voucher"}


def test_upload_requires_schema_preflight():
    with pytest.raises(BridgeError) as error:
        McpClient(Settings()).validate_upload(b"image", "receipt.jpg", "image/jpeg")
    assert error.value.code == "mcp_preflight_required"
