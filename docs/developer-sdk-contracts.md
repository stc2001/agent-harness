# Developer Guide: SDK APIs & Contracts (python-harness)

> Audience: developers onboarding to the python-harness execution engine.
> Source of truth: code in `harness/`; ADRs in `docs/adr/`; issue list in
`issues/python-harness-issues.md`. This doc captures cross-cutting CONTRACTS a new
developer must know before touching code.

## 1. SDK layout (HarnessClient sub-clients)

| client | role | key methods |
|--------|------|-------------|
| `agents` | register/run/status/stop/list | `register(spec, sandbox_id=…)`, `run(spec\|inst_id, task, session_id=…)`, `status`, `stop`, `delete` |
| `sessions` | session lifecycle/events | `start(instance_id)`, `stop`, `show`, `events.list/stream`, `rewind` |
| `program` | I-18 deterministic zero-token runner | `run(script, session_id=…, sandbox_name=…, sandbox_id=…, timeout, retries)` |
| `policy` | I-27 rule middleware | `enable(name, agent)`, `add(name, agent, veto, observe)`, `disable`, `list` |
| `memory` | context compression helpers | `estimate_tokens`, `tail`, `compress`, `inject_summary` |
| `models` | I-14 model service pool | `register/unregister/status` |
| `sandboxes` | I-26 sandbox entities | `list()`, `destroy(sandbox_id)`, `set_ttl` |
| `gates` / `gateway` | human gates / safe authorize | gate resolve; authorize subset |

Env: `DEEPSEEK_API_KEY`/`DEEPSEEK_BASE_URL` (or llm.d configs). HOME =
`~/.harness` (registry/sessions/audit) — tests/scenarios isolate it.

## 2. Execution & identity contracts (I-23/25, ADR-015/046)

- `register` creates a **registered instance** (`inst_<hex>`). `run(inst_id)`
  lazily creates that instance OWN runtime, keyed `(spec_name, inst_id)`;
  same template may run as many instances concurrently (distinct uuids).
- `run(spec, …)` auto-registers a NEW instance each call (no name-collapse).
- Busy is per-instance: run on a RUNNING instance raises; follow-up goes via
  `events.send` → wake mailbox (I-11). `stop/status` locate by instance id.
- Session binds the registered instance id; `events.send` wakes that exact
  instance (re-materialising a never-run runtime).

## 3. Hooks contract (ADR-042/048)

- `HookRegistry` is PROCESS-global; every trigger passes `agent=spec.name`
  plus point-specific fields. Business isolation is done by filtering on
  `ctx.agent`/`ctx.spec.name` INSIDE hooks (not by separate registries).
- Hook points: `authorize.veto/observe`, `tool.execute` (post-use, payload
  tool_name/args/result/session_id/ok), `tool.result`, `llm.call`,
  `agent.pre_step` (fires BEFORE messages are built each iteration),
  `agent.lifecycle`, `agent.stop.framework|app`.
- Around hooks receive `ctx` built BEFORE `final` runs; read `payload`/attrs
  from the returned `TriggerResult` or `current_context()` AFTER `next_fn()`.
- **Token metering (I-15)**: read authoritative server usage after the call
  via `current_context()["tokens_in"/"tokens_out"]` — never `ctx.tokens_*`
  (they are None pre-final). usage-less backends leave them unset.
- Policy (I-27) rides authorize.veto/observe + tool.execute; ALL rules
  default OFF; every enable/add names an agent or "*". Deny-wins union.

## 4. Context & compression contracts (ADR-034 Rev2)

- `prompt_assembly.build_messages` builds messages from session events with
  token budgeting: budget = `context_window × budget_ratio` (default .8).
  Fills NEWEST-first; the LATEST `agent_started` is an anchor and is never
  truncated; oversized tool results are clipped at ~8K with a marker.
- **Compression is a fold, not a delete.** A `memory_summary` event carrying
  `payload.after_event_id` makes `build_messages` IGNORE events before that
  boundary and insert the summary right after the task anchor (not counted
  against budget; at most one summary per session honoured). Events remain
  physically appended (ADR-037 append-only); the fold is in the logical view.
- App-side recipe (trigger policy belongs to the APP): estimate → tail →
  compress → inject_summary. Example 80%-of-window policy: in an
  `agent.pre_step` observe hook, when `estimate_tokens(sid)` exceeds
  `window*ratio`, run `tail(sid, keep_ratio)` and fold the old span.

## 5. Sandbox contracts (ADR-040/047)

- Sandbox identity is `sandbox_id` (uuid) everywhere above backends;
  bwrap PID / docker container ids never leave the backend (private map).
- `SandboxManager` API: `get_or_create(name, create_new=False)`, `create`,
  `ensure(name, sandbox_id)` (re-materialises pinned ids), `get_entity`,
  `destroy(sandbox_id)`, `list()`. uuid is stable across respawn.
- A sandbox entity persists independently of any agent (agent stop !=
  destroy); explicit `destroy` is the only teardown.
- **Host-mapped folders need permission**: tools gate file paths through
  `spec.permissions.file_access` (host prefixes). A shared folder mounted at
  sandbox `/ws` from host `H` is only usable if `file_access` includes `H/`.
- Network: default bwrap is `network: host` (SHARES the host netns) so
  127.0.0.1 inside the sandbox IS the host loopback; `network: none` adds
  `--unshare-net`.

## 6. Session event vocabulary

`agent_started` (task anchor), `agent_thought` (assistant round, may carry
`reasoning_content` + `tool_calls`), `tool_called` (allowed flag),
`tool_result`, `hook_result` (non-None tool.execute hook return),
`agent_completed`, `agent_failed`, `memory_summary`, `program_started/
program_completed` (I-18), `human_gate_*`. Replay/rewind operate over this
stream (ADR-039 resume pairs tool_called→tool_result by parent_event_id).

## 7. CLI observation (agent/human dual-mode)

`harness logs <sid>` and `harness session show <sid>` now render via one
contract: default **agent mode** prints ONE JSON summary line first
(`truncated` bool, step/event counts, type & tool histograms, last event,
latest output, error) then the tail detail window (`--tail N`, default 80).
`--user_type human` prints a markdown summary then FULL details.
`--out-file PATH` writes the summary (json/md) plus full details to a file
(path echoed in the summary). `--follow` streams.

## 8. Development conventions

- TDD: red→green vertical slices; mock sandbox managers return real
  `SandboxEntity`-shaped mocks; fake LLM via swapping `impl.openai.OpenAI`
  (message needs `tool_calls=None`); registry/session isolation via HOME
  redirection; policy tests clear the global HookRegistry per test.
- Run: `.venv/bin/python -m pytest tests/…` (network flake deselect:
  `test_sandbox.py::TestFirewallPropagation::test_host_network_allows_connections`).
- Real-LLM scenarios are local-only (not shipped in this repo); use your own workspace/fixtures.