"""Configured API/archive/upload integration without TCP or real services."""
import asyncio
import copy
import dataclasses
import io
import json
from unittest.mock import AsyncMock, Mock

import pytest
from aiohttp.test_utils import make_mocked_request
from PIL import Image

from bridge import CSRF_KEY, Manager, Settings, Store, make_app

DEVICE = 'hpaio:/net/test?ip=192.168.1.10&queue=false'
FILE_ID = 'a7c66ab7-874c-49d5-8f29-f369af0fc52f'


def profile():
    return {'params': {'deviceId': DEVICE, 'resolution': '600', 'mode': 'Gray',
                       'source': 'Flatbed', 'left': '5.5', 'top': 0,
                       'width': '120', 'height': '60', 'brightness': '950', 'contrast': 1100},
            'pipeline': 'PNG', 'filters': [], 'batch': 'none', 'index': 1}


async def dispatch(app, method, path, body=None, headers=None, peer='172.30.32.2'):
    transport = Mock()
    transport.get_extra_info.return_value = (peer, 12345)
    request = make_mocked_request(method, path, headers=headers or {}, app=app, transport=transport)
    request.json = AsyncMock(return_value=body)
    match = await app.router.resolve(request)
    match.add_app(app)
    request._match_info = match
    return await app.middlewares[0](request, match.handler)


@pytest.fixture
def rig(tmp_path):
    settings = Settings(scan_enabled=True, upload_enabled=True, scanner_device_id=DEVICE,
                        scan_resolution=600, scan_mode='Gray', scan_format='PNG', scan_paper_size='A6',
                        scan_brightness=950, scan_contrast=1100)
    store = Store(tmp_path / 'data', tmp_path / 'share', settings)
    stream = io.BytesIO()
    Image.new('RGB', (60, 80), 'white').save(stream, 'PNG')
    source = stream.getvalue()
    seen = []

    class Scanner:
        async def scan(self, selected):
            seen.append(('scan', copy.deepcopy(selected)))
            return 'the_job_returned_file.png'
        async def download(self, name):
            seen.append(('download', name))
            return source

    class MCP:
        async def __aenter__(self): return self
        async def __aexit__(self, *args): pass
        async def preflight(self): seen.append(('preflight',))
        def validate_upload(self, data, filename, mime):
            assert data == source and mime == 'image/png'
        async def upload(self, data, filename, mime):
            job = store.recent()[0]
            assert job['status'] == 'uploading'
            assert store.path(job, 'original').read_bytes() == source
            assert store.path(job, 'upload').read_bytes() == source
            seen.append(('upload', data, filename, mime))
            return {'id': FILE_ID}

    manager = Manager(settings, store, Scanner(), lambda _: MCP())
    yield manager, make_app(manager), seen, source
    store.close()


@pytest.mark.asyncio
async def test_configured_scan_api_archives_before_one_file_upload(rig):
    manager, app, seen, source = rig
    response = await dispatch(app, 'POST', '/api/scan',
                              {},
                              {'Content-Type': 'application/json', 'X-CSRF-Token': app[CSRF_KEY]})
    assert response.status == 202
    manager.settings = dataclasses.replace(manager.settings, scan_resolution=300)
    await asyncio.gather(*tuple(manager.tasks))
    saved = manager.store.recent()[0]
    assert saved['status'] == 'uploaded' and saved['file_id'] == FILE_ID
    assert saved['resolution'] == 600
    assert saved['scan_configuration']['scan_resolution'] == 600
    assert seen[0][1]['scan_paper_size'] == 'A6' and seen[0][1]['scan_format'] == 'PNG'
    assert saved['want_upload'] is True
    assert seen[1] == ('download', 'the_job_returned_file.png')
    assert sum(item[0] == 'upload' for item in seen) == 1
    assert saved['original']['sha256'] == saved['upload']['sha256']
    assert manager.store.path(saved, 'original').read_bytes() == source
    assert (manager.store.directory(saved) / 'lexware-response.json').is_file()


@pytest.mark.asyncio
@pytest.mark.parametrize('body,code', [
    ({'confirm_business': True}, 'invalid_parameters'),
    ({'confirm_business': True, 'resolution': 1200}, 'invalid_parameters'),
    ({'confirm_business': False, 'scanner_settings': profile()}, 'invalid_parameters'),
    ({'confirm_business': True, 'scanner_settings': {**profile(), 'pipeline': 'PDF'}}, 'invalid_parameters'),
    ({'upload': False}, 'invalid_parameters'),
    ({'scanner_settings': profile()}, 'invalid_parameters'),
    ({'url': 'http://other-service'}, 'invalid_parameters'),
])
async def test_legacy_or_free_scan_parameters_do_not_start_work(rig, body, code):
    manager, app, seen, _ = rig
    response = await dispatch(app, 'POST', '/api/scan', body,
                              {'Content-Type': 'application/json', 'X-CSRF-Token': app[CSRF_KEY]})
    assert response.status in {400, 409}
    assert json.loads(response.body)['code'] == code
    assert seen == [] and manager.store.recent() == []


@pytest.mark.asyncio
async def test_start_status_and_non_ascii_headers_need_no_service(rig):
    manager, app, seen, _ = rig
    response = await dispatch(app, 'GET', '/api/status')
    assert response.status == 200 and seen == []
    response = await dispatch(app, 'POST', '/api/scan', {'confirm_business': True},
                              {'Content-Type': 'application/json', 'X-CSRF-Token': 'ä'})
    assert response.status == 403
    manager.settings = dataclasses.replace(manager.settings, api_token='a' * 40)
    response = await dispatch(app, 'GET', '/api/status', headers={'Authorization': 'Bearer ä'}, peer='127.0.0.1')
    assert response.status == 401 and manager.store.recent() == [] and seen == []
