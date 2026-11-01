"""I-32 DRT: graph queries vs in-memory sqlite reference (differential random).

Reference mirrors the same invariant as the JSON store: ONE edge per
(agent, instance, session, sandbox) triple with first/last timestamps.
"""

from __future__ import annotations

import random
import sqlite3
import uuid

import pytest

from harness.session_store.impl import SessionStore
from harness.sdk.client import _AgentsClient


def _store(tmp_path):
    return SessionStore(base_path=str(tmp_path / "s"))


def _add_edge(store, sql, agent, instance, session, sandbox, ts):
    if session not in store.list_sessions():
        store.create_session(session, {"spec_name": agent})
    meta = store.get_session_metadata(session) or {}
    hist = list(meta.get("binding_history") or [])
    hit = next((e for e in hist if e.get("agent") == agent
                and e.get("instance_id") == instance
                and e.get("sandbox_id") == sandbox), None)
    if hit is not None:
        hit["last_ts"] = ts
    else:
        hist.append({"agent": agent, "instance_id": instance,
                     "session_id": session, "sandbox_id": sandbox,
                     "first_ts": ts, "last_ts": ts})
    store.update_session_metadata(session, {"binding_history": hist})
    sql.execute(
        "INSERT INTO edges(agent,instance_id,session_id,sandbox_id,first,last) "
        "VALUES(?,?,?,?,?,?) ON CONFLICT(agent,instance_id,session_id,sandbox_id) "
        "DO UPDATE SET first=MIN(first,excluded.first), "
        "last=MAX(last,excluded.last)",
        (agent, instance, session, sandbox, ts, ts))


def _ag(store):
    client = type("C", (), {})()
    client._store = store
    return _AgentsClient(client)


def _setup_con():
    con = sqlite3.connect(":memory:")
    con.execute("CREATE TABLE edges("
                "agent TEXT, instance_id TEXT, session_id TEXT, "
                "sandbox_id TEXT, first REAL, last REAL, "
                "PRIMARY KEY(agent, instance_id, session_id, sandbox_id))")
    return con


@pytest.mark.parametrize("seed", [1, 7, 42])
def test_drt_matches_sqlite(tmp_path, seed):
    store = _store(tmp_path)
    con = _setup_con()
    rng = random.Random(seed)
    agents = ["A%d" % i for i in range(4)]
    sessions = []
    sandboxes = []
    ts = 0.0
    for _ in range(150):
        ts += rng.uniform(0.5, 3.0)
        agent = rng.choice(agents)
        instance = "i-" + agent
        session = (rng.choice(sessions) if sessions and rng.random() < 0.6
                   else "s%d-%s" % (len(sessions), uuid.uuid4().hex[:6]))
        if session not in sessions:
            sessions.append(session)
        sandbox = (rng.choice(sandboxes) if sandboxes and rng.random() < 0.5
                   else "bx%d-%s" % (len(sandboxes), uuid.uuid4().hex[:6]))
        if sandbox not in sandboxes:
            sandboxes.append(sandbox)
        _add_edge(store, con, agent, instance, session, sandbox, ts)

    ag = _ag(store)
    for agent in agents:
        exp = [(sid, m) for sid, m in con.execute(
                "SELECT session_id, MAX(last) FROM edges WHERE agent=? "
                "GROUP BY session_id ORDER BY MAX(last) DESC",
                (agent,)).fetchall()]
        got = [(r["session_id"], r["last_ts"])
               for r in ag.history_sessions(agent)]
        assert got == exp, (seed, agent, "sessions")

        exp2 = [(sb, mn, mx, cnt) for sb, mn, mx, cnt in con.execute(
                "SELECT sandbox_id, MIN(first), MAX(last), "
                "COUNT(DISTINCT session_id) FROM edges WHERE agent=? "
                "GROUP BY sandbox_id ORDER BY MAX(last) DESC",
                (agent,)).fetchall()]
        got2 = [(r["sandbox_id"], r["first_ts"], r["last_ts"],
                 r["session_count"]) for r in ag.history_sandboxes(agent)]
        assert got2 == exp2, (seed, agent, "sandboxes")

    for sid in sessions[:4]:
        for agent in agents:
            exp3 = [(a, i, sb, last) for (a, i, _s, sb, _f, last) in con.execute(
                    "SELECT agent, instance_id, session_id, sandbox_id, first, last "
                    "FROM edges WHERE session_id=? AND agent=? "
                    "ORDER BY last DESC", (sid, agent)).fetchall()]
            got3 = [(e["agent"], e["instance_id"], e["sandbox_id"],
                     e["last_ts"]) for e in ag.history_bindings(agent, sid)]
            assert got3 == exp3, (seed, agent, sid, "bindings")