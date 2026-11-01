"""Module 9 — CLI: command-line user interface for Agent Harness.

All commands route through harness.sdk.HarnessClient — no direct module access.
"""

from __future__ import annotations

import argparse
import json
import sys
import uuid
from pathlib import Path

from harness.sdk import HarnessClient
from harness.sdk.models import AgentStatus


# ─────────────────────────────────────────────────────────────
# Helper
# ─────────────────────────────────────────────────────────────


def _client() -> HarnessClient:
    """Create a fresh HarnessClient.

    I-24: sandbox/skills source assembly (sandbox.yaml read, skill-mount
    scan, builtin "main" fallback) moved into HarnessClient.__init__
    (client._load_sandbox_configs) — the CLI is now pure command routing.
    """
    return HarnessClient()


# ─────────────────────────────────────────────────────────────
# Agent lifecycle commands
# ─────────────────────────────────────────────────────────────


def cmd_compile(args: argparse.Namespace) -> int:
    """harness compile <spec.yaml> [--name NAME]"""
    import yaml
    from harness.interfaces import AgentSpec
    from pathlib import Path

    client = _client()
    try:
        path = Path(args.spec_path)
        if not path.exists():
            raise FileNotFoundError(f"Agent spec not found: {args.spec_path}")
        with path.open("r", encoding="utf-8") as f:
            raw = yaml.safe_load(f) or {}
        spec = AgentSpec(**raw)
        instance = client.agents.register(spec, name=args.name)
    except (FileNotFoundError, ValueError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1

    print(instance.instance_id)
    return 0


def cmd_ps(args: argparse.Namespace) -> int:
    """harness ps — list compiled agent instances."""
    client = _client()
    agents = client.agents.list()

    if not agents:
        print("No instances found.")
        return 0

    print(f"{'INSTANCE ID':<20} {'SPEC':<20} {'STATUS':<12} {'NAME'}")
    for inst in agents:
        name = inst.spec_name
        print(f"{inst.instance_id:<20} {inst.spec_name:<20} {inst.status.value:<12} {name}")
    return 0


def cmd_start(args: argparse.Namespace) -> int:
    """harness start <instance_id> [--prompt TEXT] [--var K=V] [--append TEXT]"""
    client = _client()

    cli_vars: dict[str, str] = {}
    if getattr(args, "var", None):
        for item in args.var:
            if "=" not in item:
                print(f"Error: invalid --var format: {item}", file=sys.stderr)
                return 1
            key, _, value = item.partition("=")
            cli_vars[key.strip()] = value.strip()

    cli_append = getattr(args, "append", None)

    try:
        session = client.sessions.start(args.instance_id)
        if args.prompt or cli_vars or cli_append:
            client.sessions.events.send(session.session_id, args.prompt or "")
    except (RuntimeError, KeyError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1

    print(f"Session started: {session.session_id}")
    return 0


def cmd_stop(args: argparse.Namespace) -> int:
    """harness stop <session_id>"""
    client = _client()
    client.sessions.stop(args.session_id)
    print(f"Session stopped: {args.session_id}")
    return 0


def _mid_truncate(text: str, width: int = 400) -> str:
    """显示层截断：保头保尾（I-39）——不做硬切。

    事件/输出本身存全文（session 是事实源），仅在 CLI 渲染时收敛长度。
    """
    if text is None:
        return ""
    if width <= 8 or len(text) <= width:
        return text
    head = (width * 3) // 5
    tail = width - head - 1
    return text[:head] + "\u2026" + text[-tail:]


def _observe_text(events, session_id, status, user_type, tail, out_file,
                  holder=None):
    """Shared agent/human observation renderer.

    agent:  first line JSON summary (truncated bool) then tail detail lines.
    human:  markdown summary then FULL detail lines.
    out_file: writes summary (per user_type) + full details to a file.
    holder: optional dict {holder_instance_id, holder_status} (I-31).
    """
    lines = []
    for ev in events:
        payload = _mid_truncate(
            json.dumps(getattr(ev, "payload", {}) or {}, ensure_ascii=False), 400)
        lines.append("[%s] %s: %s" % (ev.event_type, getattr(ev, "source", ""), payload))

    types: dict[str, int] = {}
    tools: dict[str, int] = {}
    steps = 0
    latest_output = ""
    error = None
    for ev in events:
        types[ev.event_type] = types.get(ev.event_type, 0) + 1
        if ev.event_type == "agent_thought":
            steps += 1
            if not latest_output:
                content = (getattr(ev, "payload", {}) or {}).get("content", "")
                latest_output = _mid_truncate(content or "", 200)
        if ev.event_type in ("tool_called", "tool_result"):
            tname = (getattr(ev, "payload", {}) or {}).get("tool") or \
                (getattr(ev, "payload", {}) or {}).get("tool_name") or "?"
            tools[tname] = tools.get(tname, 0) + 1
        if ev.event_type == "agent_failed" and error is None:
            error = _mid_truncate(
                (getattr(ev, "payload", {}) or {}).get("error") or "", 200)
    total = len(events)
    window = tail
    truncated = total > window
    last_ev = events[-1] if events else None
    summary = {
        "session_id": session_id,
        "status": status or "",
        "steps": steps,
        "events": total,
        "truncated": truncated,
        "window": window,
        "types": types,
        "tools": tools,
        "last": (last_ev.event_type if last_ev else None),
        "latest_output": latest_output,
        "error": error,
    }
    if holder:
        summary["holder_instance_id"] = holder.get("holder_instance_id")
        summary["holder_status"] = holder.get("holder_status")
    body = lines if user_type == "human" else lines[-window:]
    if user_type == "human":
        head = ["## Session %s" % session_id,
                "",
                "- status: %s" % (status or "?"),
                "- events: %d, steps: %d" % (total, steps),
                "- types: %s" % json.dumps(types, ensure_ascii=False),
                "- tools: %s" % json.dumps(tools, ensure_ascii=False),
                "- error: %s" % (error or "-"),
                "- holder: %s (%s)" % ((holder.get("holder_instance_id") if holder else "-"),
                                        (holder.get("holder_status") if holder else "-")),
                "", "### Details", ""]
    else:
        head = [json.dumps(summary, ensure_ascii=False), ""]
    if out_file:
        summary["out_file"] = out_file
        if user_type == "agent":
            head[0] = json.dumps(summary, ensure_ascii=False)
        with open(out_file, "w", encoding="utf-8") as fh:
            fh.write("\n".join(head + lines) + "\n")
    print("\n".join(head + body))
    return 0


def cmd_logs(args: argparse.Namespace) -> int:
    """harness logs <session_id> [--user_type] [--tail N] [--out-file PATH]"""
    client = _client()

    if args.follow:
        print(f"Streaming logs for session {args.session_id} (Ctrl+C to stop)...")
        try:
            for event in client.sessions.events.stream(args.session_id):
                payload = _mid_truncate(
                    json.dumps(event.payload, ensure_ascii=False), 200)
                print(f"[{event.event_type}] {event.source}: {payload}")
        except KeyboardInterrupt:
            print("\nStopped.")
        return 0

    # I-42: 语义读面（默认）vs 审计读面（--audit，物理日志）
    if getattr(args, "audit", False):
        events = client.sessions.audit_events(args.session_id)
    else:
        events = client.sessions.events.list(args.session_id)
    if not events:
        print("No events found.")
        return 0
    holder = client.sessions.holder(args.session_id)
    return _observe_text(events, args.session_id, None,
                         args.user_type, args.tail, args.out_file,
                         holder=holder)



def cmd_preview(args: argparse.Namespace) -> int:
    """harness preview <instance_id> --session <sid> [--event-id E | --task T] [--json]

    I-43 组装产物预览（只读）：与 runtime 同一函数，因此输出 == 实际发送给
    LLM 的上下文；用于回答「模型到底看到了什么 / 内容去哪了」。
    """
    client = _client()
    try:
        out = client.sessions.preview_llm_messages(
            args.session,
            instance_id=args.instance_id,
            event_id=getattr(args, "event_id", None),
            task=getattr(args, "task", None),
        )
    except Exception as exc:  # noqa: BLE001 — CLI 边界：转成可读错误
        print(f"Error: {exc}", file=sys.stderr)
        return 1

    if getattr(args, "json", False):
        print(json.dumps(out, ensure_ascii=False, indent=2))
        return 0

    meta = out["meta"]
    print(f"[preview] model={out.get('model', '')} "
          f"window={meta['context_window']} ratio={meta['budget_ratio']} "
          f"system={meta['system_prompt_source']}")
    print(f"[preview] messages={len(out['messages'])} "
          f"truncated={meta['truncated']} dropped={meta['dropped_event_count']} "
          f"dangling_stripped={meta['stripped_dangling']} "
          f"folded={meta['folded_summary']} "
          f"as_of={meta['as_of_event_id'] or '-'} "
          f"synthetic_task={meta['synthetic_task']}")
    for m in out["messages"]:
        content = _mid_truncate(str(m.get("content") or ""), 200)
        print(f"  {m['role']}: {content}")
    return 0


def cmd_exec(args: argparse.Namespace) -> int:
    """harness exec <session_id> <message> [--append TEXT]"""
    client = _client()

    client.sessions.events.send(
        args.session_id,
        args.message,
        append=getattr(args, "append", None),
    )
    print(f"Message sent to session {args.session_id}")
    return 0


def cmd_rm(args: argparse.Namespace) -> int:
    """harness rm <instance_id>"""
    client = _client()
    try:
        client.agents.delete(args.instance_id)
    except (KeyError, RuntimeError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1

    print(f"Instance deleted: {args.instance_id}")
    return 0


# ─────────────────────────────────────────────────────────────
# Human Gate commands (via SDK)
# # ─────────────────────────────────────────────────────────────


def cmd_gate(args: argparse.Namespace) -> int:
    """harness gate approve|reject|modify <event_id> [--amend-input JSON]"""
    client = _client()
    decision = args.subcommand

    amend_input = None
    if decision == "modify" and args.amend_input:
        try:
            amend_input = json.loads(args.amend_input)
        except json.JSONDecodeError:
            amend_input = {"input": args.amend_input}

    try:
        if decision == "approve":
            client.gates.approve(args.event_id)
        elif decision == "reject":
            client.gates.reject(args.event_id, reason=args.amend_input)
        else:
            client.gates.modify(args.event_id, amend_input=amend_input or {})
    except Exception as exc:
        print(f"Error resolving gate: {exc}", file=sys.stderr)
        return 1

    print(f"Gate {args.event_id} resolved: {decision}")
    if amend_input:
        print(f"Amend input: {amend_input}")
    return 0


# ─────────────────────────────────────────────────────────────
# Session management commands (via SDK)
# ─────────────────────────────────────────────────────────────



def cmd_agents(args: argparse.Namespace) -> int:
    """harness agents --name <agent> --history_sessions|--history_sandboxes|--history_bindings [--session SID]"""
    import json as _json
    client = _client()
    records = []
    if args.history_sessions:
        records = client.agents.history_sessions(args.name)
    elif args.history_sandboxes:
        records = client.agents.history_sandboxes(args.name)
    elif args.history_bindings:
        if not args.session:
            print("--history_bindings requires --session <sid>",
                  file=sys.stderr)
            return 1
        records = client.agents.history_bindings(args.name, args.session)

    text = _json.dumps(records, ensure_ascii=False, indent=2) if \
        args.user_type == "agent" else _render_history_md(records)
    if args.out_file:
        with open(args.out_file, "w", encoding="utf-8") as f:
            f.write(text + "\n")
    print(text)
    return 0


def _render_history_md(records) -> str:
    if not records:
        return "_(no records)_"
    lines = []
    for r in records:
        if "session_id" in r and "sandbox_id" in r:
            lines.append("- session=%s sandbox=%s first=%.2f last=%.2f" % (
                r["session_id"], r["sandbox_id"],
                r.get("first_ts", 0), r.get("last_ts", 0)))
        elif "session_count" in r:
            lines.append("- sandbox=%s sessions=%d first=%.2f last=%.2f" % (
                r["sandbox_id"], r["session_count"],
                r.get("first_ts", 0), r.get("last_ts", 0)))
        else:
            lines.append("- sandbox=%s first=%.2f last=%.2f" % (
                r.get("sandbox_id", "?"), r.get("first_ts", 0),
                r.get("last_ts", 0)))
    return "\n".join(lines)


def cmd_audit(args: argparse.Namespace) -> int:
    """harness audit [--tail N] [--follow]"""
    client = _client()
    audit_path = Path(client._settings.audit_log_dir) / "audit_log.jsonl"

    if not audit_path.exists():
        print("No audit log found.")
        return 0

    if args.follow:
        print(f"Streaming audit log (Ctrl+C to stop)...")
        import time
        last_size = audit_path.stat().st_size
        try:
            while True:
                time.sleep(0.5)
                current_size = audit_path.stat().st_size
                if current_size > last_size:
                    with audit_path.open("r", encoding="utf-8") as f:
                        f.seek(last_size)
                        for line in f:
                            line = line.strip()
                            if line:
                                _print_audit_line(line)
                    last_size = current_size
        except KeyboardInterrupt:
            print("\nStopped.")
        return 0

    # Read all lines
    lines = audit_path.read_text().strip().split("\n")
    if not lines or lines == [""]:
        print("No audit events.")
        return 0

    tail = getattr(args, "tail", None)
    if tail:
        lines = lines[-tail:]

    for line in lines:
        line = line.strip()
        if line:
            _print_audit_line(line)
    return 0


def _print_audit_line(line: str) -> None:
    """Pretty-print a single audit JSON line."""
    try:
        entry = json.loads(line)
        ts = entry.get("ts", 0)
        level = entry.get("level", "info")
        category = entry.get("category", "?")
        event = entry.get("event", "?")
        job_id = entry.get("job_id")
        session_id = entry.get("session_id")
        detail = entry.get("detail", {})

        prefix = f"[{level.upper():5}] [{category:12}] {event:20}"
        ids = []
        if job_id:
            ids.append(f"job={job_id}")
        if session_id:
            ids.append(f"sess={session_id}")
        id_str = " ".join(ids)
        if id_str:
            prefix += f" | {id_str}"
        if detail:
            detail_str = json.dumps(detail, ensure_ascii=False)
            if len(detail_str) > 60:
                detail_str = detail_str[:57] + "..."
            prefix += f" | {detail_str}"
        print(prefix)
    except json.JSONDecodeError:
        print(f"[malformed] {line[:80]}")


def cmd_session(args: argparse.Namespace) -> int:
    """harness session list|show <id>|rewind <id> <event_id>"""
    client = _client()

    if args.subcommand == "list":
        sessions = client.sessions.list()
        if not sessions:
            print("No sessions found.")
            return 0
        print("Sessions:")
        for s in sessions:
            print(f"  {s.session_id}")
        return 0

    if args.subcommand == "show":
        try:
            info = client.sessions.show(args.session_id)
        except KeyError as exc:
            print(f"Error: {exc}", file=sys.stderr)
            return 1

        return _observe_text(
            info["events"], info["session_id"], None,
            args.user_type, args.tail, args.out_file,
            holder=info)

    if args.subcommand == "rewind":
        try:
            client.sessions.rewind(args.session_id, args.event_id)
        except KeyError as exc:
            print(f"Error: {exc}", file=sys.stderr)
            return 1
        except Exception as exc:
            print(f"Error rewinding: {exc}", file=sys.stderr)
            return 1

        print(f"Session {args.session_id} rewound to event {args.event_id}")
        return 0

    return 1


# ─────────────────────────────────────────────────────────────
# Argument parser
# ─────────────────────────────────────────────────────────────


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="harness",
        description="Agent Harness — single-machine local Agent infrastructure",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    # ─── Agent lifecycle ───

    p_compile = sub.add_parser("compile", help="Compile an agent spec into a runnable instance")
    p_compile.add_argument("spec_path", help="Path to agent spec YAML file")
    p_compile.add_argument("--name", help="Display name for the instance")
    p_compile.set_defaults(func=cmd_compile)

    p_ps = sub.add_parser("ps", help="List compiled agent instances")
    p_ps.set_defaults(func=cmd_ps)

    p_start = sub.add_parser("start", help="Start a session for a compiled agent instance")
    p_start.add_argument("instance_id", help="Instance ID to start")
    p_start.add_argument("--prompt", help="Initial prompt to send to the agent")
    p_start.add_argument("--var", action="append", metavar="KEY=VALUE",
                         help="Template variable override (repeatable)")
    p_start.add_argument("--append", metavar="TEXT", help="Append to system prompt")
    p_start.set_defaults(func=cmd_start)

    p_stop = sub.add_parser("stop", help="Stop a running session")
    p_stop.add_argument("session_id", help="Session ID to stop")
    p_stop.set_defaults(func=cmd_stop)

    p_prev = sub.add_parser(
        "preview", help="Preview the assembled LLM context (read-only, I-43)")
    p_prev.add_argument("instance_id", help="Agent instance id (spec source)")
    p_prev.add_argument("--session", required=True, help="Session id")
    p_prev.add_argument("--event-id", default=None, dest="event_id",
                        help="preview the historical context as of this event")
    p_prev.add_argument("--task", default=None,
                        help="preview the context with this Task appended (not persisted)")
    p_prev.add_argument("--json", action="store_true", help="machine-readable output")
    p_prev.set_defaults(func=cmd_preview)

    p_logs = sub.add_parser("logs", help="View session event logs")
    p_logs.add_argument("session_id", help="Session ID")
    p_logs.add_argument("--follow", "-f", action="store_true", help="Stream new events in real-time")
    p_logs.add_argument("--user_type", choices=["agent", "human"], default="agent",
                        help="agent = JSON summary head + tail details; human = markdown summary + full")
    p_logs.add_argument("--tail", type=int, default=80,
                        help="detail window length (agent mode)")
    p_logs.add_argument("--out-file", default="", help="write summary + full details to file")
    p_logs.add_argument("--audit", action="store_true",
                        help="read the physical log (audit face, incl. rewound content) instead of the semantic view")
    p_logs.set_defaults(func=cmd_logs)

    p_exec = sub.add_parser("exec", help="Send a message to a running session")
    p_exec.add_argument("session_id", help="Session ID")
    p_exec.add_argument("message", help="Message to send")
    p_exec.add_argument("--append", metavar="TEXT", help="Append to system prompt")
    p_exec.set_defaults(func=cmd_exec)

    p_rm = sub.add_parser("rm", help="Delete a compiled agent instance")
    p_rm.add_argument("instance_id", help="Instance ID to delete")
    p_rm.set_defaults(func=cmd_rm)

    # ─── Human Gate ───

    p_gate = sub.add_parser("gate", help="Resolve a pending human gate")
    p_gate.add_argument("subcommand", choices=["approve", "reject", "modify"])
    p_gate.add_argument("event_id", help="Gate event ID to resolve")
    p_gate.add_argument("--amend-input", help="Amended input for modify")
    p_gate.set_defaults(func=cmd_gate)

    # ─── Session management ───

    p_session = sub.add_parser("session", help="Session management")
    p_session.add_argument("subcommand", choices=["list", "show", "rewind"])
    p_session.add_argument("session_id", nargs="?", help="Session ID")
    p_session.add_argument("event_id", nargs="?", help="Event ID for rewind")
    p_session.add_argument("--user_type", choices=["agent", "human"], default="agent",
                          help="agent = JSON summary head; human = markdown summary + full")
    p_session.add_argument("--tail", type=int, default=80,
                          help="detail window length (agent mode)")
    p_session.add_argument("--out-file", default="", help="write summary + full details to file")
    p_session.set_defaults(func=cmd_session)

    # ─── Audit log ───

    p_audit = sub.add_parser("audit", help="View security audit log")
    p_audit.add_argument("--tail", "-n", type=int, metavar="N",
                         help="Show last N audit events")
    p_audit.add_argument("--follow", "-f", action="store_true",
                         help="Stream new audit events in real-time")
    p_audit.set_defaults(func=cmd_audit)

    # ─── Skills ───


    p_agents = sub.add_parser("agents", help="Agent history bindings (I-32)")
    p_agents.add_argument("--name", required=True, help="agent spec name")
    grp = p_agents.add_mutually_exclusive_group(required=True)
    grp.add_argument("--history_sessions", action="store_true",
                     help="sessions this agent bound (newest first)")
    grp.add_argument("--history_sandboxes", action="store_true",
                     help="sandboxes this agent bound (newest first)")
    grp.add_argument("--history_bindings", action="store_true",
                     help="sandboxes this agent bound within --session")
    p_agents.add_argument("--session", default="",
                          help="session id (with --history_bindings)")
    p_agents.add_argument("--user_type", choices=["agent", "human"],
                          default="agent")
    p_agents.add_argument("--out-file", default="")
    p_agents.set_defaults(func=cmd_agents)

    p_skills = sub.add_parser("skills", help="Skill management")
    p_skills_sub = p_skills.add_subparsers(dest="skills_subcommand")
    # Default: list host skills when no subcommand given
    p_skills.set_defaults(func=cmd_skills_default)

    p_skills_install = p_skills_sub.add_parser("install", help="Install a skill")
    p_skills_install.add_argument("--from", dest="from_path", required=True,
                                   help="Path to skill directory or git repo")
    p_skills_install.set_defaults(func=cmd_skills_install)

    p_skills_list = p_skills_sub.add_parser("list", help="List installed skills")
    p_skills_list.add_argument("--host", action="store_true",
                                help="List skills on host filesystem")
    p_skills_list.add_argument("--sandbox-id", metavar="ID",
                                help="List skills in a sandbox instance")
    p_skills_list.set_defaults(func=cmd_skills_list)

    return parser


# ─────────────────────────────────────────────────────────────
# Skills commands
# ─────────────────────────────────────────────────────────────


def cmd_skills_default(args: argparse.Namespace) -> int:
    """harness skills — default: list host skills."""
    from harness.skills import list_host_skill_metas
    metas = list_host_skill_metas()
    if metas:
        for m in metas:
            print(f"{m.name}: {m.description}")
    else:
        print("(no skills found)")
    return 0


def cmd_skills_install(args: argparse.Namespace) -> int:
    """harness skills install --from <path>"""
    import shutil
    from pathlib import Path

    src = Path(args.from_path)
    if not src.exists():
        print(f"Error: source path not found: {args.from_path}", file=sys.stderr)
        return 1

    skill_name = src.name
    dst = Path("skills") / skill_name
    dst.mkdir(parents=True, exist_ok=True)

    skill_file = src / "SKILL.md"
    if skill_file.is_file():
        shutil.copy2(skill_file, dst / "SKILL.md")
        print(f"Skill installed: {skill_name}")
        return 0
    else:
        print(f"Error: no SKILL.md found in {args.from_path}", file=sys.stderr)
        return 1


def cmd_skills_list(args: argparse.Namespace) -> int:
    """harness skills list [--host | --sandbox-id ID]"""
    import json

    if args.sandbox_id:
        client = _client()
        mgr = client._sandbox_mgr
        entity = mgr.get_or_create(args.sandbox_id)  # I-26: canonical entity
        from harness.sdk.client import _SkillsClient
        sc = _SkillsClient(client)
        metas = sc.scan(entity.backend, entity.sandbox_id)
        for m in metas:
            print(f"{m.name}: {m.description}")
    else:
        # Default: --host
        from harness.skills import list_host_skill_metas
        metas = list_host_skill_metas()
        for m in metas:
            print(f"{m.name}: {m.description}")
    return 0


# ─────────────────────────────────────────────────────────────
# Entry point
# ─────────────────────────────────────────────────────────────


def main(argv: list[str] | None = None) -> int:
    """CLI entry point. Called by pyproject.toml console_scripts."""
    parser = _build_parser()

    if argv is None:
        argv = sys.argv[1:]

    if not argv:
        parser.print_help()
        return 1

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
