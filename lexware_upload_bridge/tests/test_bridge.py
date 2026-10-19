from __future__ import annotations
import asyncio
import base64
import dataclasses
import io
import json
from pathlib import Path
from archive_import import parse_startup_options
import uuid

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from PIL import Image
import yaml

from bridge import (BridgeError, Settings, Store, Manager, McpClient, ScannerClient,
                    JPG_PIPELINE, atomic_write, archive_subdir, digest,
                    image_info, make_app, prepare_upload, validate_local_url)

FILE_ID = "a7c66ab7-874c-49d5-8f29-f369af0fc52f"
VOUCHER_ID = "a7c66ab7-874c-49d5-8f29-f369af0fc530"


def photo(fmt="JPEG", size=(300, 420)):
    out = io.BytesIO()
    im = Image.new("RGB", size, (243, 241, 234))
    im.save(out, fmt)
    return out.getvalue()


@pytest.fixture
def settings():
    return Settings.from_dict({"scanner_device_id": "hpaio:/net/test?ip=192.168.1.10&queue=false"})


@pytest.fixture
def store(tmp_path, settings):
    obj = Store(tmp_path / "data", tmp_path / "share", settings)
    yield obj
    try:
        obj.close()
    except Exception:
        pass


class FakeScanner:
    def __init__(self, data=None):
        self.data = data or photo()
        self.calls = []
    async def scan(self, resolution):
        self.calls.append(("scan", resolution))
        return "Scan_2026-10-11_210000.jpg"
    async def download(self, filename):
        self.calls.append(("download", filename))
        return self.data


class FakeMCP:
    def __init__(self, settings):
        self.calls = []
    async def __aenter__(self): return self
    async def __aexit__(self, *args): pass
    async def preflight(self): self.calls.append("preflight")
    async def upload(self, data, filename, mime):
        self.calls.append(("upload", data, filename, mime))
        return {"id": FILE_ID, "voucherId": VOUCHER_ID}


def test_defaults_are_dormant(settings):
    assert not settings.scan_enabled
    assert not settings.upload_enabled
    assert not settings.mcp_token
    assert not settings.api_token


@pytest.mark.parametrize("value", ["../Private", "/share/Private", "WE.R/../Private", "Privat/Lexware", "WE.R\\Private", "WE.R"])
def test_archive_path_rejects_wrong_scope(value):
    with pytest.raises(BridgeError): archive_subdir(value)


@pytest.mark.parametrize("url", ["https://example.com/mcp", "http://169.254.169.254/mcp", "file:///etc/passwd", "http://user:password@host/mcp", "http://0.0.0.0/mcp", "http://host/mcp?secret=abc"])
def test_rejects_nonlocal_or_unsafe_urls(url):
    with pytest.raises(BridgeError): validate_local_url(url, mcp=True)


@pytest.mark.parametrize("url", ["http://ab12cd34-lexware-mcp:8080/mcp", "http://core-test.local.hass.io:8080/mcp", "http://172.30.32.1:8080/mcp"])
def test_internal_mcp_addresses(url):
    assert validate_local_url(url, mcp=True) == url


def test_symlink_archive_rejected(tmp_path, settings):
    (tmp_path / "outside").mkdir()
    (tmp_path / "share").mkdir()
    (tmp_path / "share" / "WE.R").symlink_to(tmp_path / "outside", target_is_directory=True)
    with pytest.raises(BridgeError): Store(tmp_path / "data", tmp_path / "share", settings)


def test_atomic_writer_does_not_overwrite(tmp_path):
    path = tmp_path / "existing.jpg"
    path.write_bytes(b"old")
    with pytest.raises(FileExistsError): atomic_write(path, b"new")
    assert path.read_bytes() == b"old"
    assert list(tmp_path.glob('.tmp-*')) == []


def test_small_original_remains_identical():
    source = photo()
    output, meta = prepare_upload(source, 1200, 4_800_000)
    assert output == source
    assert not meta["modified"]
    assert meta["dpi"] == 1200


def test_png_can_stay_lossless():
    source = photo("PNG")
    output, meta = prepare_upload(source, 1200, 4_800_000)
    assert output == source and meta["mime"] == "image/png"


def test_oversize_image_gets_upload_copy_only():
    im = Image.effect_noise((1300, 1800), 60).convert('RGB')
    out = io.BytesIO(); im.save(out, 'PNG'); source = out.getvalue()
    before = digest(source)
    output, meta = prepare_upload(source, 1200, 100_000)
    assert len(output) <= 100_000
    assert meta["modified"] and meta["mime"] == "image/jpeg"
    assert meta["dpi"] >= 300
    assert digest(source) == before
    assert image_info(output)["extension"] == ".jpg"


@pytest.mark.parametrize("data", [b"", b"<html>error</html>", b"%PDF-1.7\nno photo", photo()[:200]])
def test_bad_images_rejected(data):
    with pytest.raises(BridgeError): image_info(data)


@pytest.mark.asyncio
async def test_default_denial_makes_no_calls(store, settings):
    scanner = FakeScanner(); manager = Manager(settings, store, scanner)
    with pytest.raises(BridgeError): manager.launch_scan(1200, False)
    assert scanner.calls == [] and store.recent() == []


@pytest.mark.asyncio
async def test_local_only_archive_never_calls_mcp(store, settings):
    settings = dataclasses.replace(settings, scan_enabled=True)
    scanner = FakeScanner()
    def forbidden(_): raise AssertionError('MCP must not be touched')
    manager = Manager(settings, store, scanner, forbidden)
    job = store.create(1200, False)
    await manager.run_scan(job['id'])
    saved = store.get(job['id'])
    assert saved['status'] == 'ready'
    assert store.path(saved, 'original').read_bytes() == scanner.data
    assert store.path(saved, 'upload').read_bytes() == scanner.data
    assert len(scanner.calls) == 2
    assert (store.directory(saved)/'status.json').exists()


@pytest.mark.asyncio
async def test_both_local_files_are_durable_before_upload(store, settings):
    settings = dataclasses.replace(settings, scan_enabled=True, upload_enabled=True)
    fake = FakeMCP(settings)
    job = store.create(1200, True)
    original_upload = fake.upload
    async def inspected_upload(data, filename, mime):
        record = store.get(job['id'])
        assert record['status'] == 'uploading'
        assert json.loads((store.directory(record)/'status.json').read_text())['status'] == 'uploading'
        assert store.path(record,'upload').read_bytes() == data
        store.verify(record)
        return await original_upload(data, filename, mime)
    fake.upload = inspected_upload
    manager = Manager(settings, store, FakeScanner(), lambda _: fake)
    await manager.run_scan(job['id'])
    saved = store.get(job['id'])
    assert saved['status'] == 'uploaded' and saved['file_id'] == FILE_ID
    assert saved['voucher_id'] == VOUCHER_ID
    assert json.loads((store.directory(saved)/'lexware-response.json').read_text())['file_id'] == FILE_ID


@pytest.mark.asyncio
async def test_second_identical_file_not_uploaded_twice(store, settings):
    settings = dataclasses.replace(settings, scan_enabled=True, upload_enabled=True)
    fake = FakeMCP(settings); manager = Manager(settings, store, FakeScanner(), lambda _: fake)
    first=store.create(600,True); await manager.run_scan(first['id'])
    second=store.create(600,True); await manager.run_scan(second['id'])
    assert store.get(first['id'])['status']=='uploaded'
    assert store.get(second['id'])['status']=='duplicate'
    assert len([x for x in fake.calls if isinstance(x,tuple) and x[0]=='upload'])==1
    assert store.get(second['id'])['duplicate_of']==first['id']


@pytest.mark.asyncio
async def test_ambiguous_upload_not_retried_even_after_restart(tmp_path, settings):
    settings=dataclasses.replace(settings,scan_enabled=True,upload_enabled=True)
    store=Store(tmp_path/'data',tmp_path/'share',settings)
    fake=FakeMCP(settings); count=0
    async def timeout(*args):
        nonlocal count;count+=1;raise TimeoutError('sensitive-network-details')
    fake.upload=timeout
    manager=Manager(settings,store,FakeScanner(),lambda _:fake)
    job=store.create(1200,True); await manager.run_scan(job['id'])
    assert store.get(job['id'])['status']=='needs_review' and count==1
    assert 'sensitive-network-details' not in json.dumps(store.get(job['id']))
    with pytest.raises(BridgeError): manager.launch_upload(job['id'])
    store.close(); reopened=Store(tmp_path/'data',tmp_path/'share',settings)
    assert reopened.get(job['id'])['status']=='needs_review' and count==1
    reopened.close()


@pytest.mark.asyncio
async def test_identical_uncertain_file_is_blocked(store, settings):
    settings=dataclasses.replace(settings,scan_enabled=True,upload_enabled=True)
    fake=FakeMCP(settings)
    async def timeout(*args): raise TimeoutError()
    fake.upload=timeout
    manager=Manager(settings,store,FakeScanner(),lambda _:fake)
    first=store.create(1200,True);await manager.run_scan(first['id'])
    second=store.create(1200,True);await manager.run_scan(second['id'])
    assert store.get(first['id'])['status']=='needs_review'
    assert store.get(second['id'])['error']['code']=='duplicate_uncertain'
    assert fake.calls==['preflight']


@pytest.mark.asyncio
async def test_archive_tampering_blocks_before_network(store, settings):
    settings=dataclasses.replace(settings,scan_enabled=True,upload_enabled=True)
    fake=FakeMCP(settings);manager=Manager(settings,store,FakeScanner(),lambda _:fake)
    job=store.create(1200,False);await manager.run_scan(job['id'])
    store.path(store.get(job['id']),'original').write_bytes(b'tampered')
    await manager.run_upload(job['id'])
    assert not fake.calls
    assert store.get(job['id'])['error']['code']=='archive_changed'


@pytest.mark.asyncio
async def test_missing_rights_does_not_enable_anything(store, settings):
    settings=dataclasses.replace(settings,scan_enabled=True,upload_enabled=True)
    fake=FakeMCP(settings)
    async def missing(): raise BridgeError('upload_not_permitted','Keine Uploadrechte.')
    fake.preflight=missing
    manager=Manager(settings,store,FakeScanner(),lambda _:fake)
    job=store.create(1200,True);await manager.run_scan(job['id'])
    saved=store.get(job['id'])
    assert saved['status']=='ready' and saved['error']['code']=='upload_not_permitted'
    assert not fake.calls


@pytest.mark.asyncio
async def test_storage_failure_prevents_upload(store, settings, monkeypatch):
    settings=dataclasses.replace(settings,scan_enabled=True,upload_enabled=True)
    fake=FakeMCP(settings);manager=Manager(settings,store,FakeScanner(),lambda _:fake)
    job=store.create(1200,True)
    def fail(*args): raise BridgeError('disk_full','Speicher voll.')
    monkeypatch.setattr(store,'check_space',fail)
    await manager.run_scan(job['id'])
    assert store.get(job['id'])['status']=='error'
    assert not fake.calls


@pytest.mark.asyncio
async def test_startup_recovers_saved_ack_without_network(tmp_path, settings):
    store=Store(tmp_path/'data',tmp_path/'share',settings)
    manager=Manager(settings,store,FakeScanner())
    job=store.create(1200,False);await manager.run_scan(job['id'])
    job=store.update(job['id'],status='uploading')
    store.save_ack(job,{'id':FILE_ID,'voucherId':VOUCHER_ID})
    store.close()
    reopened=Store(tmp_path/'data',tmp_path/'share',settings)
    assert reopened.get(job['id'])['status']=='uploaded'
    assert reopened.get(job['id'])['file_id']==FILE_ID
    reopened.close()


@pytest.mark.asyncio
async def test_startup_without_ack_flags_review(tmp_path, settings):
    store=Store(tmp_path/'data',tmp_path/'share',settings)
    manager=Manager(settings,store,FakeScanner())
    job=store.create(1200,False);await manager.run_scan(job['id'])
    store.update(job['id'],status='uploading');store.close()
    reopened=Store(tmp_path/'data',tmp_path/'share',settings)
    assert reopened.get(job['id'])['status']=='needs_review'
    reopened.close()


@pytest.mark.asyncio
@pytest.mark.loopback
async def test_no_scan_on_startup_or_status(store, settings):
    fake=FakeScanner();manager=Manager(settings,store,fake)
    client=TestClient(TestServer(make_app(manager,trusted_peers=frozenset({'127.0.0.1'}))))
    await client.start_server()
    try:
        response=await client.get('/api/status');data=await response.json()
        assert response.status==200 and not data['scan_enabled'] and not data['upload_enabled']
        assert fake.calls==[] and not manager.tasks
    finally: await client.close()


@pytest.mark.asyncio
@pytest.mark.loopback
async def test_external_peer_or_spoofed_header_rejected(store, settings):
    manager=Manager(settings,store,FakeScanner())
    client=TestClient(TestServer(make_app(manager)))
    await client.start_server()
    try:
        response=await client.get('/api/status',headers={'X-Forwarded-For':'172.30.32.2','X-Ingress-Path':'/fake'})
        assert response.status==401
    finally: await client.close()


@pytest.mark.asyncio
@pytest.mark.loopback
async def test_ingress_csrf_and_configured_scan_gates(store, settings):
    manager=Manager(settings,store,FakeScanner())
    client=TestClient(TestServer(make_app(manager,trusted_peers=frozenset({'127.0.0.1'}))))
    await client.start_server()
    try:
        response=await client.post('/api/scan',json={'confirm_business':True})
        assert response.status==403
        s=await (await client.get('/api/status')).json();headers={'X-CSRF-Token':s['csrf']}
        response=await client.post('/api/scan',headers=headers,json={'confirm_business':False})
        assert response.status==400
        response=await client.post('/api/scan',headers=headers,json={})
        assert response.status==409
        assert store.recent()==[]
    finally: await client.close()


@pytest.mark.asyncio
@pytest.mark.loopback
async def test_bearer_access_requires_exact_strong_token(store, settings):
    settings=dataclasses.replace(settings,api_token='a'*40)
    manager=Manager(settings,store,FakeScanner())
    client=TestClient(TestServer(make_app(manager)))
    await client.start_server()
    try:
        assert (await client.get('/api/status',headers={'Authorization':'Bearer wrong'})).status==401
        response=await client.get('/api/status',headers={'Authorization':'Bearer '+'a'*40})
        assert response.status==200
        data=await response.json()
        assert 'api_token' not in data and 'mcp_token' not in data
    finally: await client.close()


@pytest.mark.asyncio
async def test_a_second_click_does_not_create_another_job(store, settings):
    settings=dataclasses.replace(settings,scan_enabled=True)
    manager=Manager(settings,store,FakeScanner())
    job=manager.launch_scan(1200,False)
    with pytest.raises(BridgeError): manager.launch_scan(1200,False)
    await asyncio.gather(*manager.tasks)
    assert len(store.recent())==1 and store.get(job['id'])['status']=='ready'


@pytest.mark.asyncio
@pytest.mark.parametrize('sse',[False,True])
@pytest.mark.loopback
async def test_mcp_transport_and_exact_tool_schema(settings,sse):
    received=[]
    async def handle(request):
        assert request.headers['Authorization']=='Bearer '+'m'*32
        body=await request.json();received.append(body)
        method=body['method']
        if method=='notifications/initialized': return web.Response(status=202)
        if method=='initialize': result={'protocolVersion':'2025-06-18','capabilities':{},'serverInfo':{'name':'fake','version':'1'}}
        elif method=='tools/list':
            assert request.headers['Mcp-Session-Id']=='test-session'
            assert request.headers['MCP-Protocol-Version']=='2025-06-18'
            result={'tools':[{'name':'upload-file','inputSchema':{'type':'object','properties':{x:{'type':'string'} for x in ['fileBase64','filename','mimeType']}}}]}
        elif method=='tools/call':
            assert body['params']['name']=='upload-file'
            args=body['params']['arguments'];assert args['type']=='voucher'
            assert base64.b64decode(args['fileBase64'])==photo()
            result={'structuredContent':{'id':FILE_ID,'voucherId':VOUCHER_ID},'content':[]}
        else: raise AssertionError(method)
        packet={'jsonrpc':'2.0','id':body['id'],'result':result}
        if sse:
            return web.Response(text='event: message\ndata: '+json.dumps(packet)+'\n\n',headers={'Content-Type':'text/event-stream','Mcp-Session-Id':'test-session'})
        return web.json_response(packet,headers={'Mcp-Session-Id':'test-session'})
    app=web.Application();app.router.add_post('/mcp',handle)
    server=TestServer(app);await server.start_server()
    try:
        s=dataclasses.replace(settings,mcp_url=str(server.make_url('/mcp')),mcp_token='m'*32)
        async with McpClient(s) as client:
            await client.preflight();result=await client.upload(photo(),'receipt.jpg','image/jpeg')
        assert result['id']==FILE_ID
        assert [x['method'] for x in received]==['initialize','notifications/initialized','tools/list','tools/call']
    finally: await server.close()


@pytest.mark.asyncio
@pytest.mark.loopback
async def test_mcp_read_only_server_denies_before_upload(settings):
    received=[]
    async def handle(request):
        body=await request.json();received.append(body['method'])
        if body['method']=='notifications/initialized':return web.Response(status=202)
        result={'protocolVersion':'2025-06-18'} if body['method']=='initialize' else {'tools':[]}
        return web.json_response({'jsonrpc':'2.0','id':body['id'],'result':result})
    app=web.Application();app.router.add_post('/mcp',handle)
    server=TestServer(app);await server.start_server()
    try:
        s=dataclasses.replace(settings,mcp_url=str(server.make_url('/mcp')),mcp_token='m'*32)
        async with McpClient(s) as client:
            with pytest.raises(BridgeError,match='upload-file'): await client.preflight()
        assert 'tools/call' not in received
    finally: await server.close()


@pytest.mark.asyncio
@pytest.mark.loopback
async def test_scanner_uses_exact_result_not_latest_file(settings):
    received=[];source=photo()
    async def context(request):
        return web.json_response({'devices':[{'id':settings.scanner_device_id,
            'features':{'--resolution':{'options':[300,600,1200]}},
            'settings':{'pipeline':{'options':[JPG_PIPELINE]}}}]})
    async def scan(request):
        data=await request.json();received.append(data)
        return web.json_response({'file':{'name':'specific_scan.jpg','fullname':'/some/untrusted/directory/specific_scan.jpg'}})
    async def file(request):
        assert request.match_info['name']=='specific_scan.jpg'
        return web.Response(body=source,content_type='image/jpeg')
    app=web.Application();app.router.add_get('/api/v1/context',context)
    app.router.add_post('/api/v1/scan',scan);app.router.add_get('/api/v1/files/{name}',file)
    server=TestServer(app);await server.start_server()
    try:
        s=dataclasses.replace(settings,scanserv_url=str(server.make_url('/')).rstrip('/'))
        client=ScannerClient(s);name=await client.scan(1200);data=await client.download(name)
        assert data==source and len(received)==1
        params=received[0]['params']
        assert params['mode']=='Color' and params['resolution']==1200 and params['width']==210
        assert params['brightness']==1000 and params['contrast']==1000
        assert received[0]['index']==1 and received[0]['filters']==[]
    finally: await server.close()


def test_app_package_has_safe_defaults():
    root=Path(__file__).resolve().parents[1]
    config=yaml.safe_load((root/'config.yaml').read_text())
    assert config['boot']=='manual' and config['stage']=='experimental'
    assert not config['options']['scan_enabled'] and not config['options']['upload_enabled']
    assert not config.get('ports') and not config['host_network']
    assert not config['homeassistant_api'] and not config['hassio_api'] and not config['full_access']
    assert config['map']==[{'type':'share','read_only':False}]
    parse_startup_options(config['options'])
    parse_startup_options(json.loads((root/'options.prepared.json').read_text()))


def test_tokens_not_in_settings_repr(settings):
    s=dataclasses.replace(settings,mcp_token='a-secret-mcp-value',api_token='a-secret-api-value')
    assert 'a-secret' not in repr(s)

@pytest.mark.asyncio
@pytest.mark.loopback
async def test_real_client_transport_error_does_not_repeat_tools_call(settings):
    received=[]
    async def handle(request):
        body=await request.json();received.append(body['method'])
        if body['method']=='notifications/initialized': return web.Response(status=202)
        if body['method']=='initialize': result={'protocolVersion':'2025-06-18'}
        elif body['method']=='tools/list':
            result={'tools':[{'name':'upload-file','inputSchema':{'properties':{x:{} for x in ['fileBase64','filename','mimeType']}}}]}
        else: return web.Response(status=503)
        return web.json_response({'jsonrpc':'2.0','id':body['id'],'result':result})
    app=web.Application();app.router.add_post('/mcp',handle)
    server=TestServer(app);await server.start_server()
    try:
        s=dataclasses.replace(settings,mcp_url=str(server.make_url('/mcp')),mcp_token='m'*32)
        async with McpClient(s) as client:
            await client.preflight()
            with pytest.raises(BridgeError): await client.upload(photo(),'receipt.jpg','image/jpeg')
        assert received.count('tools/call')==1
    finally: await server.close()


@pytest.mark.asyncio
@pytest.mark.loopback
async def test_mcp_redirect_not_followed(settings):
    target_calls=[]
    async def redirected(request): target_calls.append(True);return web.json_response({})
    async def handler(request): raise web.HTTPFound('/other')
    app=web.Application();app.router.add_post('/mcp',handler);app.router.add_get('/other',redirected)
    server=TestServer(app);await server.start_server()
    try:
        s=dataclasses.replace(settings,mcp_url=str(server.make_url('/mcp')),mcp_token='m'*32)
        async with McpClient(s) as client:
            with pytest.raises(BridgeError): await client.preflight()
        assert not target_calls
    finally: await server.close()


@pytest.mark.asyncio
@pytest.mark.loopback
async def test_mcp_wrong_response_id_rejected(settings):
    async def handler(request):
        return web.json_response({'jsonrpc':'2.0','id':'wrong','result':{'protocolVersion':'2025-06-18'}})
    app=web.Application();app.router.add_post('/mcp',handler)
    server=TestServer(app);await server.start_server()
    try:
        s=dataclasses.replace(settings,mcp_url=str(server.make_url('/mcp')),mcp_token='m'*32)
        async with McpClient(s) as client:
            with pytest.raises(BridgeError): await client.preflight()
    finally: await server.close()


@pytest.mark.asyncio
@pytest.mark.loopback
async def test_mcp_paginated_tool_list(settings):
    calls=[]
    async def handler(request):
        body=await request.json();calls.append(body)
        if body['method']=='notifications/initialized':return web.Response(status=202)
        if body['method']=='initialize':result={'protocolVersion':'2025-06-18'}
        elif not body['params'].get('cursor'):result={'tools':[],'nextCursor':'second-page'}
        else:result={'tools':[{'name':'upload-file','inputSchema':{'properties':{x:{} for x in ['fileBase64','filename','mimeType']}}}]}
        return web.json_response({'jsonrpc':'2.0','id':body['id'],'result':result})
    app=web.Application();app.router.add_post('/mcp',handler)
    server=TestServer(app);await server.start_server()
    try:
        s=dataclasses.replace(settings,mcp_url=str(server.make_url('/mcp')),mcp_token='m'*32)
        async with McpClient(s) as client: await client.preflight()
        assert calls[-1]['params']=={'cursor':'second-page'}
        assert not any(c['method']=='tools/call' for c in calls)
    finally: await server.close()


@pytest.mark.asyncio
async def test_scanner_timeout_does_not_retry(store, settings):
    class FailingScanner(FakeScanner):
        async def scan(self,resolution):
            self.calls.append('scan');raise TimeoutError()
    settings=dataclasses.replace(settings,scan_enabled=True,upload_enabled=True)
    scanner=FailingScanner();fake=FakeMCP(settings)
    manager=Manager(settings,store,scanner,lambda _:fake)
    job=store.create(1200,True);await manager.run_scan(job['id'])
    assert scanner.calls==['scan']
    assert store.get(job['id'])['status']=='error' and not fake.calls


@pytest.mark.asyncio
@pytest.mark.loopback
async def test_output_file_download_is_authenticated_and_checksummed(store, settings):
    settings=dataclasses.replace(settings,api_token='a'*40)
    manager=Manager(settings,store,FakeScanner())
    job=store.create(1200,False);await manager.run_scan(job['id'])
    client=TestClient(TestServer(make_app(manager)));await client.start_server()
    try:
        url='/api/jobs/'+job['id']+'/files/original'
        assert (await client.get(url)).status==401
        response=await client.get(url,headers={'Authorization':'Bearer '+'a'*40})
        assert response.status==200 and await response.read()==photo()
        store.path(store.get(job['id']),'original').write_bytes(b'changed')
        assert (await client.get(url,headers={'Authorization':'Bearer '+'a'*40})).status==409
    finally:await client.close()


@pytest.mark.parametrize('raw',[{'scan_enabled':'false'},{'upload_enabled':1},{'api_token':'short'},{'mcp_token':'with\nnewline'},{'scan_resolution':2400},{'max_upload_bytes':6_000_000}])
def test_invalid_or_unsafe_options_rejected(raw):
    with pytest.raises(BridgeError):Settings.from_dict(raw)
