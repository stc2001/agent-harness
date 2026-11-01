"""I-42：读面二分 —— 语义读面（逻辑视图）vs 审计读面（物理日志）。"""

from __future__ import annotations

import pathlib
import re
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


def _spec(name='i42-worker'):
    return AgentSpec(name=name, description='t', tools=['echo'],
                     model='deepseek-v4-flash', sandbox='main',
                     max_iterations=1, llm_profile='default',
                     permissions={'subagent_spawn': False})


def _ran_session(client):
    inst = client.agents.register(_spec())
    res = client.agents.run(inst.instance_id, 'task')
    return res.session_id


def test_semantic_events_list_excludes_rewound_events(client):
    """语义读面：rewind 之后 list() 不得再返回被回退的事件。"""
    sid = _ran_session(client)
    physical = client._store.read_events(sid)
    target = physical[0].event_id
    client.sessions.rewind(sid, target)
    logical = client.sessions.events.list(sid)
    assert all(e.event_type != 'agent_completed' for e in logical), logical
    assert client.sessions.events.list(sid)[-1].event_type == 'session_rewound'


def test_audit_events_exposes_physical_log(client):
    """审计读面：物理日志（含被回退内容与 rewind 标记）必须可读。"""
    sid = _ran_session(client)
    client.sessions.rewind(sid, client._store.read_events(sid)[0].event_id)
    audit = client.sessions.audit_events(sid)
    types = [e.event_type for e in audit]
    assert 'agent_completed' in types, types
    assert 'session_rewound' in types, types


def test_audit_path_points_at_session_file(client):
    sid = _ran_session(client)
    p = pathlib.Path(client.sessions.audit_path(sid))
    assert p.exists() and p.name == 'events.jsonl'


def test_memory_tail_uses_semantic_view(client):
    """memory 读面属语义侧：rewind 后不得包含被回退的轮次。"""
    sid = _ran_session(client)
    client.sessions.rewind(sid, client._store.read_events(sid)[0].event_id)
    tail = client.memory.tail(sid, keep_ratio=1.0)
    text = str(tail)
    assert 'DONE' not in text, text


ALLOWED_PHYSICAL_READERS = {
    'harness/session/impl.py': None,          # logical_view 实现（任意函数）
    'harness/sdk/client.py': {'audit_events'},  # 仅审计读面
}


def test_physical_reads_are_confined_to_the_audit_face():
    """fitness：`store.read_events(` 只允许出现在日志层实现与审计读面内。"""
    root = pathlib.Path(__file__).resolve().parent.parent / 'harness'
    offenders = []
    for path in root.rglob('*.py'):
        rel = path.relative_to(root.parent).as_posix()
        lines = path.read_text(encoding='utf-8').splitlines()
        for i, line in enumerate(lines, 1):
            if 'read_events(' not in line or 'def read_events' in line:
                continue
            owner = None
            for j in range(i - 1, 0, -1):
                m = re.match(r'\s*def\s+(\w+)', lines[j - 1])
                if m:
                    owner = m.group(1)
                    break
            allowed = ALLOWED_PHYSICAL_READERS.get(rel, set())
            if rel in ALLOWED_PHYSICAL_READERS:
                if allowed is None or owner in allowed:
                    continue
            offenders.append(f'{rel}:{i} in {owner}()')
    assert not offenders, '物理日志读点越界: ' + '; '.join(offenders)


def test_cli_logs_default_semantic_audit_flag_physical(client, monkeypatch, capsys):
    """CLI：logs 默认语义视图；--audit 显示物理日志（对照调试）。"""
    from types import SimpleNamespace
    import harness.cli.main as cli_mod

    sid = _ran_session(client)
    client.sessions.rewind(sid, client._store.read_events(sid)[0].event_id)
    monkeypatch.setattr(cli_mod, '_client', lambda: client)

    def _run(audit):
        args = SimpleNamespace(session_id=sid, follow=False, user_type='human',
                               tail=200, out_file='', audit=audit)
        cli_mod.cmd_logs(args)
        return capsys.readouterr().out

    semantic_out = _run(False)
    audit_out = _run(True)
    assert 'DONE' not in semantic_out, semantic_out[:200]
    assert 'DONE' in audit_out, audit_out[:200]
