# Agent Harness — Test Suite

> **194 passed / 0 failed** (unit + integration + E2E)

This directory contains the full test suite for Agent Harness. Tests are organized by module and run via pytest.

---

## Quick Start

```bash
# Run all tests
make test

# Run with coverage
make test-cov

# Run excluding slow E2E tests
pytest tests/ -q --ignore=tests/test_e2e.py

# Run a specific test file
pytest tests/test_coordinator.py -v

# Run a specific test
pytest tests/test_coordinator.py::TestNodeHumanGate::test_parallel_level_all_gates_checked -xvs
```

---

## Test Organization

| File | Module | What It Tests | Approx. Tests |
|------|--------|---------------|:-------------:|
| `test_session_store.py` | SessionStore | JSONL persistence, concurrent append, rewind, fork | 16 |
| `test_session.py` | SessionManager | State machine transitions, gate events, rewinds | 20 |
| `test_cap_token.py` | CapTokenService | JWT generation, validation, permission enforcement | 8 |
| `test_agent_spec.py` | AgentSpecService | YAML loading, schema validation, registry lookup | 6 |
| `test_sandbox.py` | Sandbox backends | Bubblewrap/Docker spawn, execute, lifecycle | 10 |
| `test_agent_runtime.py` | AgentRuntime | `_agent_loop`, tool dispatch, LLM interaction | 12 |
| `test_tools.py` | Tools | read_file, write_file, bash, validate, diff, list_agents | 14 |
| `test_mock_llm.py` | Mock LLM | Context assembly pipeline, httpx transport interception | 5 |
| `test_coordinator.py` | Coordinator | DAG compilation, phase execution, node/phase gates, parallel execution, dynamic spawn | 23 |
| `test_orchestrator.py` | Orchestrator | Job lifecycle, start/cancel/status, gate polling | 6 |
| `test_e2e.py` | End-to-end | Full job run with real sandbox + LLM (slow) | 6 |
| `test_cli_lifecycle.py` | CLI | compile, ps, start, stop, logs, exec, rm | 8 |
| `test_cli_new.py` | CLI (new) | run, status, cancel, gate, session, audit | 12 |
| `test_sdk_client.py` | SDK | HarnessClient initialization, config loading | 6 |
| `test_sdk_config.py` | SDK Config | Pydantic Settings, YAML/TOML, env var expansion | 8 |
| `test_sdk_jobs.py` | SDK Jobs | jobs.submit, vars injection, prompt append | 5 |
| `test_audit_log.py` | AuditLog | Global + per-session dual write, 16 audit points | 12 |

---

## Shared Fixtures (`conftest.py`)

| Fixture | Scope | Description |
|---------|:-----:|-------------|
| `temp_store_path` | function | Temporary directory for session data, auto-cleaned |
| `set_deepseek_env` | session (autouse) | Sets `DEEPSEEK_API_KEY` and `DEEPSEEK_BASE_URL` for all tests |
| `deepseek_api_key` | function | Returns test API key string |
| `sandbox_manager` | function | Creates a `SandboxManager` with isolated `sandbox.yaml` |
| `sample_session_id` | function | Unique UUID per test |
| `sample_event` | function | Sample Event dict |
| `sample_agent_spec_dict` | function | Sample AgentSpec-compatible dict |
| `sample_dag_nodes` | function | Sample DAG node dicts |
| `sample_config_path` | function | Temporary `local.yaml` with minimal config |
| `sandbox_yaml_path` | function | Temporary `sandbox.yaml` with bwrap/docker backends |

---

## Test Design Principles

1. **Behavior over implementation** — Tests verify what the system does, not how.
2. **Public interfaces only** — No testing of private methods.
3. **Mock external dependencies** — LLM API calls and sandbox processes are mocked; real backends used only in E2E.
4. **No fixed sleeps** — All async waits use deadline polling (`for _ in range(N): sleep(); if condition: break`).
5. **Gate tests run in background threads** — `human_gate` blocks, so `execute_phase` is launched via `threading.Thread` and resolved from the test thread.

---

## Adding a New Test

```python
def test_new_behavior(self, coord_fixtures):
    """One-sentence description of the behavior being verified.

    Context: why this test exists, what regression it prevents.
    """
    client = harness_fixtures["client"]
    # ... setup ...
    events = client.sessions.events.list(session_id)
    assert events[-1].event_type == "agent_completed"
```

**Docstring rule**: Explain "why" the test exists (what behavior it protects), not "what" it does (the code already says that).

---

## CI / Regression

```bash
# Full suite (used in CI)
pytest tests/ -q

# Quick smoke (excludes E2E)
pytest tests/ -q --ignore=tests/test_e2e.py
```

---

## Known Constraints

- E2E tests (`test_e2e.py`) require network access to DeepSeek API and Bubblewrap binary.
- `test_tools.py` includes 4 real-LLM tests gated behind `real_api_key` fixture.
- `jwt` library emits `InsecureKeyLengthWarning` (test key is 11 bytes); safe in test context.
