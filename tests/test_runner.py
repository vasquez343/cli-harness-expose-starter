import asyncio
from contextlib import aclosing
import os
from pathlib import Path
import sys
from dataclasses import replace

import pytest

from agentd.config import Config
from agentd.runner import HarnessError, child_env, process_events, run


def cfg(tmp_path, **kwargs):
    return Config(token='fake', workspace=tmp_path, allow_unsafe_cli=True, **kwargs)


async def collect(argv, config, prompt='test'):
    return [event async for event in process_events(argv, config=config, prompt=prompt)]


def test_child_environment_is_allowlisted(monkeypatch):
    monkeypatch.setenv('AGENTD_TOKEN','fake-token')
    monkeypatch.setenv('UNRELATED_SECRET','fake-secret')
    monkeypatch.setenv('PROVIDER_TEST_KEY','fake-provider')
    monkeypatch.setenv('HARNESS_ENV_ALLOWLIST','AGENTD_TOKEN,PROVIDER_TEST_KEY')
    env=child_env()
    assert 'AGENTD_TOKEN' not in env and 'UNRELATED_SECRET' not in env
    assert env['PROVIDER_TEST_KEY']=='fake-provider'


@pytest.mark.asyncio
async def test_prompt_is_data_not_shell(tmp_path):
    payload='$(touch SHOULD_NOT_EXIST); `echo nope`\nhello'
    result=await collect([sys.executable,'-c',
        'import json,sys; print(json.dumps({"prompt":sys.stdin.read()}))'],cfg(tmp_path),payload)
    assert result == [{'prompt':payload}]
    assert not (tmp_path/'SHOULD_NOT_EXIST').exists()


@pytest.mark.asyncio
async def test_inactivity_and_total_deadlines(tmp_path):
    with pytest.raises(HarnessError,match='timeout'):
        await collect([sys.executable,'-c','import time; time.sleep(10)'],
                      cfg(tmp_path,idle_timeout=.1,total_timeout=2))
    script='import time\nwhile True:\n print("{}",flush=True)\n time.sleep(.02)'
    with pytest.raises(HarnessError,match='timeout'):
        await collect([sys.executable,'-c',script],cfg(tmp_path,idle_timeout=1,total_timeout=.2))


@pytest.mark.asyncio
async def test_output_limits_and_exit_failure(tmp_path):
    with pytest.raises(HarnessError,match='limit'):
        await collect([sys.executable,'-c','print("x"*10000)'],cfg(tmp_path,max_output_bytes=128))
    with pytest.raises(HarnessError,match='code 7'):
        await collect([sys.executable,'-c','raise SystemExit(7)'],cfg(tmp_path))


@pytest.mark.asyncio
async def test_closing_generator_kills_descendant(tmp_path):
    child=tmp_path/'child.py'
    child.write_text('import time,pathlib\ntime.sleep(.4)\npathlib.Path("escaped").write_text("bad")')
    script=f'import subprocess,sys,time\nsubprocess.Popen([sys.executable,{str(child)!r}])\nprint("{{}}",flush=True)\ntime.sleep(10)'
    async with aclosing(process_events([sys.executable,'-c',script],config=cfg(tmp_path),prompt='')) as events:
        assert await anext(events)=={}
    await asyncio.sleep(.6)
    assert not (tmp_path/'escaped').exists()


@pytest.mark.asyncio
@pytest.mark.parametrize('provider',['codex','claude'])
async def test_adapters_with_fake_cli(tmp_path,monkeypatch,provider):
    binary=tmp_path/provider
    binary.write_text('#!'+sys.executable+'\nimport sys,json\nprompt=sys.stdin.read()\n'
        'if "-o" in sys.argv:\n open(sys.argv[sys.argv.index("-o")+1],"w").write(prompt)\n'
        'print(json.dumps({"type":"result","result":prompt}))\n')
    binary.chmod(0o755)
    monkeypatch.setenv('PATH',str(tmp_path)+os.pathsep+os.environ['PATH'])
    events=[e async for e in run(replace(cfg(tmp_path),provider=provider),'hello')]
    assert events[-1]['ok'] and events[-1]['text']=='hello'


@pytest.mark.asyncio
async def test_closing_adapter_closes_process_group(tmp_path,monkeypatch):
    binary=tmp_path/'claude'
    binary.write_text('#!'+sys.executable+'\nimport time,pathlib\nprint("{}",flush=True)\n'
                      'time.sleep(.4)\npathlib.Path("escaped").write_text("bad")\n')
    binary.chmod(0o755)
    monkeypatch.setenv('PATH',str(tmp_path)+os.pathsep+os.environ['PATH'])
    async with aclosing(run(replace(cfg(tmp_path),provider='claude'),'hello')) as events:
        assert (await anext(events))['type']=='event'
    await asyncio.sleep(.6)
    assert not (tmp_path/'escaped').exists()


@pytest.mark.asyncio
async def test_claude_missing_result_is_not_success(tmp_path,monkeypatch):
    binary=tmp_path/'claude'
    binary.write_text('#!'+sys.executable+'\nprint("{}")\n')
    binary.chmod(0o755)
    monkeypatch.setenv('PATH',str(tmp_path)+os.pathsep+os.environ['PATH'])
    with pytest.raises(HarnessError,match='result event'):
        _=[e async for e in run(replace(cfg(tmp_path),provider='claude'),'hello')]
