"""I-43：组装产物预览（单一真源 = runtime 与 SDK 同一函数）。"""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from harness.interfaces import AgentSpec


@pytest.fixture
def tmp_home(monkeypatch, tmp_path):
    home = tmp_path / 'home'
    home.mkdir()
    monkeypatch.setenv('HOME', str(home))
    return str(home)


@pytest.fixture
def client(tmp_home, tmp_path, monkeypatch):
    from harness.sdk import HarnessClient
    from harness.sdk.config import HarnessSettings
    from harness.orchestration.pool import AgentInstancePool
    from harness.sandbox.manager import SandboxEntity

    monkeypatch.setattr('harness.sdk.client.SandboxManager', lambda **kw: MagicMock())
    c = HarnessClient(settings=HarnessSettings(
        hmac_secret='test-secret-0123456789abcdef',
        sessions_dir=str(tmp_path / 'sessions')))
    mgr = MagicMock()
    entity = SandboxEntity(sandbox_id='sbx-1', config_name='main', backend=MagicMock())
    mgr.get_or_create.return_value = entity
    mgr.get_config.return_value = None
    mgr.ensure.return_value = entity
    c._pool = AgentInstancePool(store=c._store, gateway=c._gateway, api_key='sk-test',
                                sandbox_manager=mgr, audit_log=c._audit,
                                model_pool=c._model_pool)
    import harness.agent_runtime.impl as impl_mod

    def fake_openai(**kwargs):
        cl = MagicMock()
        cl.chat.completions.create.return_value = SimpleNamespace(choices=[SimpleNamespace(
            message=SimpleNamespace(role='assistant', content='DONE', tool_calls=None),
            finish_reason='stop')])
        return cl

    monkeypatch.setattr(impl_mod.openai, 'OpenAI', fake_openai)
    return c


def _spec(name='i43-worker'):
    return AgentSpec(name=name, description='t', tools=['echo'],
                     model='deepseek-v4-flash', sandbox='main',
                     max_iterations=1, llm_profile='default',
                     system_prompt='SYS-BASE',
                     permissions={'subagent_spawn': False})


def test_preview_with_task_mode_and_read_only(client):
    """task= 模式：预览“这条 Task 发出去会看到什么”，且不落盘、不调 LLM。"""
    inst = client.agents.register(_spec())
    sid = client.sessions.start(inst.instance_id).session_id
    before = len(client.sessions.audit_events(sid))
    out = client.sessions.preview_llm_messages(sid, instance_id=inst.instance_id,
                                               task='DO-THE-THING')
    assert out['meta']['synthetic_task'] is True
    assert any('DO-THE-THING' in (m.get('content') or '') for m in out['messages'])
    assert out['messages'][0]['role'] == 'system'
    assert out['messages'][0]['content'] == 'SYS-BASE'
    assert len(client.sessions.audit_events(sid)) == before, '预览不得落盘'


def test_preview_event_id_mode_uses_historical_prefix(client):
    """event_id= 模式：以该事件为界的历史上下文（后续轮不得出现）。"""
    inst = client.agents.register(_spec())
    res = client.agents.run(inst.instance_id, 'TASK-ONE')
    sid = res.session_id
    events = client.sessions.audit_events(sid)
    first = events[0].event_id
    out = client.sessions.preview_llm_messages(sid, instance_id=inst.instance_id,
                                               event_id=first)
    assert out['meta']['as_of_event_id'] == first
    flat = ' '.join(str(m.get('content')) for m in out['messages'])
    assert 'DONE' not in flat, flat


def test_preview_matches_what_runtime_actually_sent(client, monkeypatch):
    """单一真源：预览必须与 runtime 实际发送的 messages 完全一致。"""
    import harness.agent_runtime.impl as impl_mod

    sent = {}
    real_openai = impl_mod.openai.OpenAI

    def capturing_openai(**kwargs):
        cl = real_openai(**kwargs)
        orig = cl.chat.completions.create

        def create(**kw):
            sent['messages'] = kw.get('messages')
            return orig(**kw)

        cl.chat.completions.create = create
        return cl

    monkeypatch.setattr(impl_mod.openai, 'OpenAI', capturing_openai)
    inst = client.agents.register(_spec())
    res = client.agents.run(inst.instance_id, 'PARITY-TASK')
    # 该轮调用发生时，日志里只有 agent_started（回复尚未产生）——以此事件
    # 为界的预览必须与 runtime 实际发送的内容**逐字节一致**。
    first = client.sessions.audit_events(res.session_id)[0].event_id
    out = client.sessions.preview_llm_messages(res.session_id,
                                               instance_id=inst.instance_id,
                                               event_id=first)
    assert sent['messages'] == out['messages']


def test_preview_reports_truncation_in_meta(client):
    """meta 必须回答“内容去哪了”：截断时 truncated/dropped_event_count 可见。"""
    inst = client.agents.register(_spec())
    res = client.agents.run(inst.instance_id, 'OLD-TASK' * 40)
    client.agents.run(inst.instance_id, 'NEW-TASK' * 40, session_id=res.session_id)
    sid = res.session_id
    out = client.sessions.preview_llm_messages(
        sid, instance_id=inst.instance_id,
        overrides={'context_window': 1, 'budget_ratio': 0.01})
    assert out['meta']['truncated'] is True
    assert out['meta']['dropped_event_count'] >= 0
    assert out['meta']['context_window'] == 1


def test_preview_unknown_instance_fails_loudly(client):
    inst = client.agents.register(_spec())
    sid = client.sessions.start(inst.instance_id).session_id
    with pytest.raises((KeyError, ValueError)):
        client.sessions.preview_llm_messages(sid, instance_id='does-not-exist')


def test_cli_preview_prints_messages_and_meta(client, monkeypatch, capsys):
    import harness.cli.main as cli_mod

    inst = client.agents.register(_spec())
    sid = client.sessions.start(inst.instance_id).session_id
    monkeypatch.setattr(cli_mod, '_client', lambda: client)
    args = SimpleNamespace(instance_id=inst.instance_id, session=sid,
                           event_id=None, task='CLI-TASK', json=True)
    rc = cli_mod.cmd_preview(args)
    out = capsys.readouterr().out
    assert rc == 0
    payload = json.loads(out)
    assert payload['meta']['synthetic_task'] is True
    assert any('CLI-TASK' in (m.get('content') or '') for m in payload['messages'])
