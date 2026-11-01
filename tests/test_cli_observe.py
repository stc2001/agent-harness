"""CLI observe (agent/human, truncated bool, out-file, holder) — TDD."""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest


def _ev(t, src, payload):
    return SimpleNamespace(event_type=t, source=src, payload=payload)


def _stub(events):
    class Events:
        @staticmethod
        def list(sid):
            return events

    class Sessions:
        events = Events()

        @staticmethod
        def holder(sid):
            return {"holder_instance_id": "inst-1",
                    "holder_status": "idle"}

        @staticmethod
        def show(sid):
            return {"session_id": sid, "event_count": len(events),
                    "events": events, "holder_instance_id": "inst-1",
                    "holder_status": "idle"}

    return SimpleNamespace(sessions=Sessions())


@pytest.fixture
def cli_mod(monkeypatch):
    import harness.cli.main as m
    return m


def test_agent_json_summary_with_truncated(cli_mod, monkeypatch, capsys):
    events = [
        _ev("agent_started", "a", {"tools": ["bash"]}),
        _ev("agent_thought", "a", {"content": "hi"}),
        _ev("tool_called", "a", {"tool": "bash", "args": {"command": "ls"}}),
        _ev("tool_result", "a", {"tool": "bash", "result": "x"}),
        _ev("agent_completed", "a", {"reason": "stop", "output": "done"}),
    ]
    monkeypatch.setattr(cli_mod, "_client", lambda: _stub(events))
    args = SimpleNamespace(session_id="s1", follow=False, user_type="agent",
                           tail=3, out_file="")
    cli_mod.cmd_logs(args)
    captured = capsys.readouterr().out
    first, rest = captured.split("\n", 1)
    j = json.loads(first)
    assert j["session_id"] == "s1"
    assert j["truncated"] is True
    assert j["events"] == 5 and j["steps"] == 1
    assert j["tools"]["bash"] == 2
    assert j["last"] == "agent_completed"
    assert j["holder_instance_id"] == "inst-1"
    assert j["holder_status"] == "idle"
    lines = [l for l in rest.split("\n") if l.strip()]
    assert len(lines) == 3


def test_human_markdown_full(cli_mod, monkeypatch, capsys):
    events = [_ev("agent_started", "a", {"tools": []}),
              _ev("agent_completed", "a", {"output": "ok"})]
    monkeypatch.setattr(cli_mod, "_client", lambda: _stub(events))
    args = SimpleNamespace(session_id="s1", follow=False, user_type="human",
                           tail=80, out_file="")
    cli_mod.cmd_logs(args)
    out = capsys.readouterr().out
    assert out.startswith("## Session s1")
    assert "- events: 2" in out
    assert "- holder: inst-1 (idle)" in out
    assert out.count("[agent_") >= 2


def test_out_file_writes_summary_and_full(cli_mod, monkeypatch, capsys,
                                          tmp_path):
    events = [_ev("agent_thought", "a", {"content": "c"}),
              _ev("agent_completed", "a", {"output": "ok"})]
    monkeypatch.setattr(cli_mod, "_client", lambda: _stub(events))
    fp = tmp_path / "obs.txt"
    args = SimpleNamespace(session_id="s1", follow=False, user_type="agent",
                           tail=80, out_file=str(fp))
    cli_mod.cmd_logs(args)
    text = fp.read_text(encoding="utf-8")
    head = json.loads(text.split("\n", 1)[0])
    assert head["out_file"] == str(fp)
    assert text.count("[agent_") >= 2


def test_session_show_uses_observe(cli_mod, monkeypatch, capsys):
    events = [_ev("agent_completed", "a", {"output": "ok"})]
    monkeypatch.setattr(cli_mod, "_client", lambda: _stub(events))
    args = SimpleNamespace(subcommand="show", session_id="s1",
                           user_type="agent", tail=80, out_file="")
    cli_mod.cmd_session(args)
    first = capsys.readouterr().out.split("\n", 1)[0]
    j = json.loads(first)
    assert j["holder_status"] == "idle"
    assert j["holder_instance_id"] == "inst-1"


def test_mid_truncate_keeps_head_and_tail():
    """CLI 显示层：中间截断（保头保尾），而非硬切前 200。"""
    from harness.cli.main import _mid_truncate
    s = "A" * 1000 + "TAILMARK"
    out = _mid_truncate(s, width=120)
    assert len(out) <= 120
    assert out.startswith("A")
    assert "TAILMARK" in out
    assert "…" in out or "..." in out


def test_observe_long_input_is_bounded(cli_mod, monkeypatch, capsys):
    """超长 agent_started.input 不再原样刷屏（中间截断显示）。"""
    long_input = "甲" * 2000 + "TAILMARK"
    events = [_ev("agent_started", "a", {"input": long_input}),
              _ev("agent_completed", "a", {"output": "ok"})]
    monkeypatch.setattr(cli_mod, "_client", lambda: _stub(events))
    args = SimpleNamespace(session_id="s1", follow=False, user_type="human",
                           tail=80, out_file="")
    cli_mod.cmd_logs(args)
    out = capsys.readouterr().out
    line = [l for l in out.split("\n") if l.startswith("[agent_started]")][0]
    assert len(line) < 600, len(line)
    assert "TAILMARK" in line

