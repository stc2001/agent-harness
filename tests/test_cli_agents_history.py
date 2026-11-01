"""CLI agents history (I-32) — dual-mode output — TDD."""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest


def _cli_mod(monkeypatch, fake_agents):
    import harness.cli.main as m
    client = SimpleNamespace(agents=fake_agents)
    monkeypatch.setattr(m, "_client", lambda: client)
    return m


class _FakeAgents:
    def __init__(self):
        self.calls = []

    def history_sessions(self, name):
        self.calls.append(("sessions", name))
        return [{"session_id": "s1", "sandbox_id": "bx1",
                 "first_ts": 1.0, "last_ts": 2.0}]

    def history_sandboxes(self, name):
        self.calls.append(("sandboxes", name))
        return [{"sandbox_id": "bx1", "session_count": 2,
                 "first_ts": 1.0, "last_ts": 2.0}]

    def history_bindings(self, name, sid):
        self.calls.append(("bindings", name, sid))
        return [{"agent": name, "sandbox_id": "bx2",
                 "first_ts": 3.0, "last_ts": 3.0}]


def test_agents_history_sessions_agent_json(monkeypatch, capsys):
    fa = _FakeAgents()
    m = _cli_mod(monkeypatch, fa)
    args = SimpleNamespace(name="A", history_sessions=True,
                           history_sandboxes=False, history_bindings=False,
                           session="", user_type="agent", out_file="")
    m.cmd_agents(args)
    out = capsys.readouterr().out
    recs = json.loads(out)
    assert recs[0]["session_id"] == "s1"
    assert fa.calls == [("sessions", "A")]


def test_agents_history_bindings_requires_session(monkeypatch, capsys):
    fa = _FakeAgents()
    m = _cli_mod(monkeypatch, fa)
    args = SimpleNamespace(name="A", history_sessions=False,
                           history_sandboxes=False, history_bindings=True,
                           session="", user_type="agent", out_file="")
    rc = m.cmd_agents(args)
    assert rc == 1
    assert "requires --session" in capsys.readouterr().err


def test_agents_history_sandboxes_human(monkeypatch, capsys):
    fa = _FakeAgents()
    m = _cli_mod(monkeypatch, fa)
    args = SimpleNamespace(name="A", history_sessions=False,
                           history_sandboxes=True, history_bindings=False,
                           session="", user_type="human", out_file="")
    m.cmd_agents(args)
    out = capsys.readouterr().out
    assert "- sandbox=bx1 sessions=2" in out
