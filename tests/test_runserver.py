import asyncio
import json
import logging
from unittest import mock

import aiohttp
import pytest

from aiohttp_devtools.runserver import runserver
from aiohttp_devtools.runserver.serve import create_auxiliary_app, create_main_app, serve_main_app
from aiohttp_devtools.runserver.watch import PyCodeEventHandler

from .conftest import mktree

SIMPLE_APP = {
    'app.py': """\
from aiohttp import web

async def hello(request):
    return web.Response(text='hello world')

def create_app(loop):
    app = web.Application(loop=loop)
    app.router.add_get('/', hello)
    return app"""
}


async def test_start_runserver(loop, tmpworkdir):
    mktree(tmpworkdir, SIMPLE_APP)
    aux_app, observer, aux_port = runserver(app_path='app.py', loop=loop)
    assert isinstance(aux_app, aiohttp.web.Application)
    assert aux_port == 8001

    # this has started the app running in a separate process, check it's working. ugly but comprehensive check
    app_running = False
    async with aiohttp.ClientSession(loop=loop) as session:
        for i in range(20):
            try:
                async with session.get('http://localhost:8000/') as r:
                    assert r.status == 200
                    assert (await r.text()) == 'hello world'
            except (AssertionError, OSError):
                await asyncio.sleep(0.1, loop=loop)
            else:
                app_running = True
                break
    assert app_running

    assert len(observer._handlers) == 1
    event_handlers = list(observer._handlers.values())[0]
    assert len(event_handlers) == 2
    code_event_handler = next(eh for eh in event_handlers if isinstance(eh, PyCodeEventHandler))
    code_event_handler._process.terminate()


async def test_run_app(loop, tmpworkdir, test_client):
    mktree(tmpworkdir, SIMPLE_APP)
    app = create_main_app(app_path='app.py', loop=loop)
    assert isinstance(app, aiohttp.web.Application)
    cli = await test_client(app)
    r = await cli.get('/')
    assert r.status == 200
    text = await r.text()
    assert text == 'hello world'


async def test_aux_app(loop, tmpworkdir, test_client):
    mktree(tmpworkdir, {
        'test.txt': 'test value',
    })
    app = create_auxiliary_app(static_path='.', port=8000, loop=loop)
    cli = await test_client(app)
    r = await cli.get('/test.txt')
    assert r.status == 200
    text = await r.text()
    assert text == 'test value'


def test_run_app_http(tmpworkdir, loop, mocker):
    mktree(tmpworkdir, SIMPLE_APP)
    mocker.spy(loop, 'create_server')
    mock_modify_main_app = mocker.patch('aiohttp_devtools.runserver.serve.modify_main_app')
    # for some reason calling setup_logging breaks subsequent tests
    mock_setup_logging = mocker.patch('aiohttp_devtools.runserver.serve.setup_logging')
    loop.call_later(0.05, loop.stop)

    serve_main_app(app_path='app.py', loop=loop)

    assert loop.is_closed()
    loop.create_server.assert_called_with(mock.ANY, '0.0.0.0', 8000)
    mock_modify_main_app.assert_called_with(mock.ANY, '/static/', True, True, 8001)
    mock_setup_logging.assert_called_with(False)


@pytest.fixture
def aux_cli(test_client, loop):
    app = create_auxiliary_app(static_path='.', port=8000, loop=loop)
    return loop.run_until_complete(test_client(app))


async def test_websocket_hello(aux_cli, caplog):
    async with aux_cli.session.ws_connect(aux_cli.make_url('/livereload')) as ws:
        ws.send_json({'command': 'hello', 'protocols': ['http://livereload.com/protocols/official-7']})
        async for msg in ws:
            assert msg.tp == aiohttp.MsgType.text
            data = json.loads(msg.data)
            assert data == {
                'serverName': 'livereload-aiohttp',
                'command': 'hello',
                'protocols': ['http://livereload.com/protocols/official-7']
            }
            break  # noqa
    assert 'adev.server.aux: browser disconnected, appears no websocket connection was made' in caplog.log


async def test_websocket_info(aux_cli, caplog):
    caplog.set_level(logging.DEBUG)
    async with aux_cli.session.ws_connect(aux_cli.make_url('/livereload')) as ws:
        ws.send_json({'command': 'info', 'url': 'foobar', 'plugins': 'bang'})
    assert 'adev.server.aux: browser connected:' in caplog


async def test_websocket_bad(aux_cli, caplog):
    async with aux_cli.session.ws_connect(aux_cli.make_url('/livereload')) as ws:
        ws.send_str('not json')
        ws.send_json({'command': 'hello', 'protocols': ['not official-7']})
        ws.send_json({'command': 'boom', 'url': 'foobar', 'plugins': 'bang'})
        ws.send_bytes(b'this is bytes')
    assert 'adev.server.aux: live reload protocol 7 not supported' in caplog.log
    assert 'adev.server.aux: JSON decode error' in caplog.log
    assert 'adev.server.aux: Unknown ws message' in caplog.log
    assert "adev.server.aux: unknown websocket message type binary, data: b'this is bytes'" in caplog.log


async def test_websocket_reload(aux_cli, caplog):
    caplog.set_level(logging.DEBUG)
    app = aux_cli._server.app
    assert app.src_reload('foobar') == 0
    async with aux_cli.session.ws_connect(aux_cli.make_url('/livereload')) as ws:
        ws.send_json({
            'command': 'info',
            'url': 'foobar',
            'plugins': 'bang',
        })
        await asyncio.sleep(0.05, loop=app.loop)
        assert 'adev.server.aux: browser connected:' in caplog
        assert app.src_reload('foobar') == 1