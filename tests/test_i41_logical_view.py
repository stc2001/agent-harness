"""I-41：读面契约落实 — runtime 必须消费「逻辑视图」（rewind 生效）。

接缝：AgentRuntime.execute_task 的真实调用点（信号 = 模型实际收到的 messages）。

"""

from __future__ import annotations

import time
import uuid
from unittest.mock import MagicMock

import pytest

from harness.gateway.gateway import Gateway
from harness.interfaces import AgentSpec, Event
from harness.session.impl import SessionManager
from harness.session_store.impl import SessionStore


class _Msg:
    def __init__(self, content, tool_calls=None, reasoning_content=None):
        self.content = content
        self.tool_calls = tool_calls
        self.reasoning_content = reasoning_content


def _spec():
    return AgentSpec(name='t41', description='t', tools=[], model='deepseek-v4-flash',
                     sandbox='main', max_iterations=3, llm_profile='default',
                     permissions={'subagent_spawn': False})


@pytest.fixture
def scenario(tmp_path):
    store = SessionStore(base_path=str(tmp_path))
    mgr = SessionManager(store)
    sid = str(uuid.uuid4())
    store.create_session(sid, metadata={'instance_id': 'i1', 'spec_name': 't41',
                                        'llm_profile': 'default'})

    def ev(t, payload, eid=None, parent=None):
        e = Event(event_id=eid or ('ev_' + uuid.uuid4().hex[:6]), session_id=sid,
                  event_type=t, timestamp=time.time(), source='t', payload=payload,
                  parent_event_id=parent)
        store.append_event(e)
        return e

    e1 = ev('agent_started', {'input': 'TASK-A'})
    ev('agent_thought', {'content': 'SECRET-OLD-ROUND'})
    ev('agent_completed', {'output': 'SECRET-OLD-ROUND'})
    mgr.rewind(sid, e1.event_id)

    from harness.agent_runtime.impl import AgentRuntime
    rt = AgentRuntime(spec=_spec(), gateway=Gateway(event_bus=None), store=store,
                      api_key='x', sandbox=MagicMock())
    seen = {}

    def fake_call_llm(messages, tools):
        seen['messages'] = messages
        return _Msg('DONE'), 'stop'

    rt._call_llm = fake_call_llm
    return store, mgr, sid, rt, seen


def test_rewound_round_is_not_sent_to_the_model(scenario):
    """rewind 之后，被回退掉的旧轮不得出现在模型上下文中。"""
    store, mgr, sid, rt, seen = scenario
    rt.execute_task('FOLLOW-UP', sid)
    flat = ' '.join(str(m.get('content')) for m in seen['messages'])
    assert 'SECRET-OLD-ROUND' not in flat, seen['messages']


def test_logical_view_public_api_excludes_rewound_events(tmp_path):
    """SessionManager.logical_view() 是日志层公开读面：rewind 标记重放。"""
    store = SessionStore(base_path=str(tmp_path))
    mgr = SessionManager(store)
    sid = str(uuid.uuid4())
    store.create_session(sid, metadata={'instance_id': 'i1', 'spec_name': 't41',
                                        'llm_profile': 'default'})
    e1 = Event(event_id='e1', session_id=sid, event_type='agent_started',
               timestamp=time.time(), source='t', payload={'input': 'A'})
    store.append_event(e1)
    e2 = Event(event_id='e2', session_id=sid, event_type='agent_thought',
               timestamp=time.time(), source='t', payload={'content': 'OLD'})
    store.append_event(e2)
    mgr.rewind(sid, 'e1')
    view = mgr.logical_view(sid)
    assert [e.event_type for e in view] == ['agent_started', 'session_rewound']


# ── 切片 2：提示词形状层（assemblable）──────────────────────────────


def _ev(t, payload, eid, parent=None):
    return Event(event_id=eid, session_id='s', event_type=t, timestamp=0.0,
                 source='t', payload=payload, parent_event_id=parent)


def test_assemblable_strips_dangling_tool_round():
    """缺 tool_result 的工具轮必须被剥离（否则模型收到非法消息链）。"""
    from harness.prompt_assembly import assemblable
    events = [
        _ev('agent_started', {'input': 'T'}, 'e1'),
        _ev('agent_thought', {'content': 'A', 'tool_calls': [
            {'id': 'c1', 'name': 'bash', 'arguments': '{}'}]}, 'e2'),
    ]
    out = assemblable(events)
    assert [e.event_type for e in out] == ['agent_started']


def test_assemblable_passes_clean_history_through():
    from harness.prompt_assembly import assemblable
    events = [
        _ev('agent_started', {'input': 'T'}, 'e1'),
        _ev('agent_thought', {'content': 'A', 'tool_calls': [
            {'id': 'c1', 'name': 'bash', 'arguments': '{}'}]}, 'e2'),
        _ev('tool_result', {'result': 'R'}, 'e3', parent='c1'),
    ]
    assert assemblable(events) == events


def test_dangling_tool_round_never_reaches_the_model(tmp_path):
    """runtime 组合（logical_view + assemblable）后，非法消息链不得发给模型。"""
    store = SessionStore(base_path=str(tmp_path))
    SessionManager(store)
    sid = str(uuid.uuid4())
    store.create_session(sid, metadata={'instance_id': 'i1', 'spec_name': 't41',
                                        'llm_profile': 'default'})
    for e in [
        Event(event_id='e1', session_id=sid, event_type='agent_started',
              timestamp=0.0, source='t', payload={'input': 'T'}),
        Event(event_id='e2', session_id=sid, event_type='agent_thought',
              timestamp=0.0, source='t',
              payload={'content': 'A', 'tool_calls': [
                  {'id': 'c1', 'name': 'bash', 'arguments': '{}'}]}),
    ]:
        store.append_event(e)

    from harness.agent_runtime.impl import AgentRuntime
    rt = AgentRuntime(spec=_spec(), gateway=Gateway(event_bus=None), store=store,
                      api_key='x', sandbox=MagicMock())
    seen = {}

    def fake_call_llm(messages, tools):
        seen['messages'] = messages
        return _Msg('DONE'), 'stop'

    rt._call_llm = fake_call_llm
    rt.execute_task('FOLLOW-UP', sid)
    msgs = seen['messages']
    assert not any(m.get('tool_calls') for m in msgs), msgs
    assert not any(m['role'] == 'tool' for m in msgs), msgs
