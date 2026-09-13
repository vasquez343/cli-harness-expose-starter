import asyncio
from dataclasses import replace

import httpx
import pytest
from fastapi.testclient import TestClient

from agentd.app import create_app
from agentd.config import Config
from agentd.runner import HarnessError


def config(tmp_path):
    return Config(token="test-only-token", workspace=tmp_path, allow_unsafe_cli=True)


async def echo(config, prompt):
    yield {"type": "event", "data": {"text": prompt}}
    yield {"type": "result", "ok": True, "text": prompt}


def test_auth_fail_closed_and_explicit_opt_in(tmp_path):
    cfg = config(tmp_path)
    client = TestClient(create_app(cfg, echo))
    assert client.get('/health').status_code == 200
    assert client.post('/run', json={'prompt': 'hello'}).status_code == 401
    assert TestClient(create_app(replace(cfg, token=''), echo)).post(
        '/run', json={'prompt':'hello'}).status_code == 503
    assert TestClient(create_app(replace(cfg, allow_unsafe_cli=False), echo)).post(
        '/run', json={'prompt':'hello'}, headers={'Authorization':'Bearer '+cfg.token}).status_code == 503


def test_result_stream_and_prompt_limit(tmp_path):
    cfg = config(tmp_path)
    client = TestClient(create_app(cfg, echo))
    headers = {'Authorization': 'Bearer '+cfg.token}
    assert client.post('/run', json={'prompt':'hello'}, headers=headers).json()['text'] == 'hello'
    stream = client.post('/stream', json={'prompt':'hello'}, headers=headers)
    assert stream.status_code == 200 and 'text/event-stream' in stream.headers['content-type']
    assert stream.text.count('data: ') == 2
    assert client.post('/run', json={'prompt':'x'*16001}, headers=headers).status_code == 422


@pytest.mark.asyncio
async def test_concurrency_rejected_and_failure_releases_slot(tmp_path):
    entered, release = asyncio.Event(), asyncio.Event()

    async def slow(config, prompt):
        entered.set()
        await release.wait()
        raise HarnessError('fake failure')
        yield

    app = create_app(config(tmp_path), slow)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url='http://local',
                                headers={'Authorization':'Bearer test-only-token'}) as c:
        first = asyncio.create_task(c.post('/run', json={'prompt':'hello'}))
        await entered.wait()
        assert (await c.post('/run', json={'prompt':'hello'})).status_code == 409
        release.set()
        assert (await first).status_code == 502
        assert not app.state.busy


def test_stream_failure_is_terminal_error_and_releases_slot(tmp_path):
    async def broken(config, prompt):
        raise HarnessError('fake failure')
        yield
    app = create_app(config(tmp_path), broken)
    response = TestClient(app).post('/stream', json={'prompt':'hello'},
                                   headers={'Authorization':'Bearer test-only-token'})
    assert response.status_code == 200
    assert '"type": "error"' in response.text and not app.state.busy
