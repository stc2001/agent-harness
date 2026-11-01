#!/usr/bin/env python3
"""Local-model smoke check: profile resolution → raw SDK call → harness run.

WHY THIS IS A SCRIPT AND NOT A pytest TEST
  It needs a live OpenAI-compatible model server.  CI and most dev boxes have
  none, so as a test it would either fail spuriously or be skipped everywhere.
  The deterministic, always-runnable equivalent lives in
  `tests/test_context_round_truncation.py::test_e2e_assembled_prompt_reaches_tool_and_answer`
  (in-process MockTransport, no network).  This script is the *optional*,
  explicitly-gated check against a real small model.

STAGES
  1 config  — the LLM profile resolves and points where expected
  2 raw     — the OpenAI SDK can talk to that server end to end
  3 harness — a full AgentSpec → sandbox → bash → answer round trip

Usage:
    python scripts/harness_smoke.py [config|raw|harness|all]

Preconditions for stage 3: bubblewrap (`bwrap`) present, and the server running.
Stage 3 is skipped (not failed) when either is missing.
"""
from __future__ import annotations

import json
import os
import shutil
import sys
from pathlib import Path

WS = Path(__file__).resolve().parents[1]

# Project dir = repo root: HarnessClient resolves configs/llm.yaml,
# harness/config/*.yaml and ./sessions/ relative to Path.cwd().
os.chdir(WS)
# HOME must be ASSIGNED (not setdefault): the instance registry resolves
# ~/.harness/instances via Path.home(), and HOME is usually already set.
os.environ["HOME"] = str(WS / ".harness_home")
os.environ.setdefault("XDG_CONFIG_HOME", str(WS / ".harness_home" / ".config"))
os.environ.setdefault("HF_HUB_OFFLINE", "1")
os.environ.setdefault("TRANSFORMERS_OFFLINE", "1")
(WS / ".harness_home").mkdir(exist_ok=True)
(WS / "harness_ws").mkdir(exist_ok=True)

STAGE = sys.argv[1] if len(sys.argv) > 1 else "all"


def _base_url() -> str:
    """Resolve the configured endpoint (do not hardcode a port)."""
    try:
        from harness.sdk.config import resolve_llm_profile
        return resolve_llm_profile("default", project_dir=WS).base_url
    except Exception:
        return "http://127.0.0.1:8080/v1"


def server_up() -> bool:
    """llama.cpp serves /health at the ORIGIN, not under /v1."""
    import urllib.request
    base = _base_url().rstrip("/")
    if base.endswith("/v1"):
        base = base[: -len("/v1")]
    try:
        with urllib.request.urlopen(base + "/health", timeout=3) as r:
            return r.status == 200
    except Exception:
        return False


def stage_config() -> bool:
    from harness.sdk.config import resolve_llm_profile
    cfg = resolve_llm_profile("default", project_dir=WS)
    print("[1] profile resolved")
    print(f"    base_url = {cfg.base_url}")
    print(f"    model    = {cfg.model}")
    print("    -> OK")
    return True


def stage_raw() -> bool:
    from openai import OpenAI
    from harness.sdk.config import resolve_llm_profile
    cfg = resolve_llm_profile("default", project_dir=WS)
    client = OpenAI(api_key=cfg.api_key or "sk-local", base_url=cfg.base_url)
    extra = {}
    ctk = getattr(cfg, "chat_template_kwargs", None)
    if ctk:
        extra["extra_body"] = {"chat_template_kwargs": dict(ctk)}
    r = client.chat.completions.create(
        model=cfg.model,
        messages=[{"role": "user",
                   "content": "Reply with exactly the word PONG and nothing else."}],
        temperature=getattr(cfg, "temperature_override", None) or 0.0,
        max_tokens=64, **extra)
    msg = r.choices[0].message
    content = msg.content or ""
    print("[2] raw call via OpenAI SDK")
    print(f"    content = {content!r}")
    print(f"    usage   = in={r.usage.prompt_tokens} out={r.usage.completion_tokens}")
    ok = content.strip() != ""      # empty content ⇒ thinking ate the budget
    print("    ->", "OK" if ok else "FAILED (empty content)")
    return ok


def stage_harness() -> bool:
    from harness.interfaces import AgentSpec, SandboxConfig
    from harness.sdk import HarnessClient

    if shutil.which("bwrap") is None:
        print("[3] SKIPPED: bubblewrap (bwrap) not installed")
        return True

    client = HarnessClient(sandbox_configs={"main": SandboxConfig(
        backend="bubblewrap", firewall={"network": "host"},
        shared_folders=[{"host_path": str(WS / "harness_ws"),
                         "sandbox_path": "/ws"}])})

    from harness.sdk.config import resolve_llm_profile
    model = resolve_llm_profile("default", project_dir=WS).model

    spec = AgentSpec(
        name="smoke", description="smoke test", tools=["bash"],
        model=model,
        sandbox="main", max_iterations=4, timeout_seconds=600,
        system_prompt=("You are a terse assistant in a Linux sandbox with a bash "
                       "tool. Answer in as few words as possible."),
        permissions={"subagent_spawn": False,
                     "file_access": {str(WS / "harness_ws") + "/": "read_write"}},
    )
    inst = client.agents.register(spec)
    sid = client.sessions.start(inst.instance_id).session_id
    print(f"[3] harness run (instance={inst.instance_id} session={sid})")
    res = client.agents.run(inst.instance_id,
                            "Run the bash command: echo HELLO_FROM_HARNESS . "
                            "Then reply with exactly what it printed.",
                            session_id=sid)
    out = (getattr(res, "output", "") or "")
    print(f"    status = {res.status}")
    print(f"    output = {out[:200]!r}")
    ok = "HELLO_FROM_HARNESS" in out
    print("    ->", "OK" if ok else "CHECK MANUALLY")
    return ok


STAGES = {"config": stage_config, "raw": stage_raw, "harness": stage_harness}
order = ["config", "raw", "harness"] if STAGE == "all" else [STAGE]

if not server_up():
    print(f"[!] no model server at {_base_url()} — start it first.\n"
          f"    (the deterministic equivalent needs no server:\n"
          f"     python -m pytest "
          f"tests/test_context_round_truncation.py -k e2e -q)", file=sys.stderr)
    sys.exit(2)

results: dict[str, bool] = {}
for name in order:
    print("=" * 60)
    try:
        results[name] = STAGES[name]()
    except Exception:                                    # noqa: BLE001
        import traceback
        traceback.print_exc()
        results[name] = False
print("=" * 60)
print("RESULTS:", json.dumps(results))
sys.exit(0 if all(results.values()) else 1)
