"""Tests for SDK _PromptsClient — assemble, build_messages, describe_tools."""

from __future__ import annotations

import pytest


class TestPromptsAssemble:
    """prompts.assemble() — system_prompt construction."""

    def test_assemble_base_only(self):
        """assemble() with only base_prompt returns it unchanged."""
        from harness.sdk.client import _PromptsClient
        pc = _PromptsClient(None)
        result = pc.assemble(base_prompt="You are a test agent.")
        assert result == "You are a test agent."

    def test_assemble_with_vars(self):
        """vars are substituted via {key} template."""
        from harness.sdk.client import _PromptsClient
        pc = _PromptsClient(None)
        result = pc.assemble(
            base_prompt="Current time: {time}.",
            vars={"time": "12:00"},
        )
        assert "Current time: 12:00." in result

    def test_assemble_missing_var_keeps_placeholder(self):
        """Missing vars keep {key} in output."""
        from harness.sdk.client import _PromptsClient
        pc = _PromptsClient(None)
        result = pc.assemble(
            base_prompt="Hello {name}.",
            vars={},
        )
        assert "{name}" in result

    def test_assemble_with_append(self):
        """append goes after base_prompt + vars."""
        from harness.sdk.client import _PromptsClient
        pc = _PromptsClient(None)
        result = pc.assemble(
            base_prompt="You are a test agent.",
            append="Additional note.",
        )
        assert "You are a test agent." in result
        assert "Additional note." in result
        assert result.index("test agent") < result.index("Additional note")

    def test_assemble_with_skills(self):
        """skills metadata appended, sorted alphabetically."""
        from harness.skills import SkillMeta
        from harness.sdk.client import _PromptsClient
        pc = _PromptsClient(None)
        skills = [
            SkillMeta(name="z-shell", description="Zsh tips", ),
            SkillMeta(name="bash-expert", description="Bash tips", ),
        ]
        result = pc.assemble(
            base_prompt="You are a test agent.",
            skills=skills,
        )
        # bash-expert should appear before z-shell (alpha sort)
        assert "bash-expert" in result
        assert "z-shell" in result
        b_pos = result.index("bash-expert")
        z_pos = result.index("z-shell")
        assert b_pos < z_pos

    def test_assemble_full_pipeline(self):
        """vars → append → skills in order."""
        from harness.skills import SkillMeta
        from harness.sdk.client import _PromptsClient
        pc = _PromptsClient(None)
        result = pc.assemble(
            base_prompt="Time: {time}.",
            vars={"time": "12:00"},
            skills=[SkillMeta("s", "desc")],
            append="Extra.",
        )
        # vars before append
        assert result.index("12:00") < result.index("Extra")
        # skills last
        assert "Available skills" in result

    def test_assemble_no_skills_section_when_empty(self):
        """No 'Available skills' when skills list is empty or None."""
        from harness.sdk.client import _PromptsClient
        pc = _PromptsClient(None)
        result = pc.assemble(base_prompt="Test.")
        assert "Available skills" not in result


class TestPromptsBuildMessages:
    """prompts.build_messages() — events → OpenAI messages."""

    def test_build_messages_basic(self):
        """system_prompt + agent_started → system + user message."""
        from harness.sdk.client import _PromptsClient
        pc = _PromptsClient(None)
        from harness.interfaces import Event
        events = [
            Event(event_id="e1", session_id="s", event_type="agent_started",
                  timestamp=0, source="test",
                  payload={"input": "Hello"}),
        ]
        messages = pc.build_messages(
            system_prompt="You are helpful.",
            events=events,
            context_window=128000,
        )
        assert len(messages) >= 2
        assert messages[0]["role"] == "system"
        assert messages[0]["content"] == "You are helpful."

    def test_build_messages_token_truncation(self):
        """Large events beyond 50% context_window are truncated."""
        from harness.sdk.client import _PromptsClient
        pc = _PromptsClient(None)
        from harness.interfaces import Event
        events = [
            Event(event_id="e1", session_id="s", event_type="agent_started",
                  timestamp=0, source="test",
                  payload={"input": "x" * 5000}),  # ~1250 tokens
            Event(event_id="e2", session_id="s", event_type="agent_started",
                  timestamp=1, source="test",
                  payload={"input": "y" * 5000}),
        ]
        messages = pc.build_messages(
            system_prompt="Short.",
            events=events,
            context_window=4000,  # budget = 2000 tokens
        )
        # Only the most recent event (e2) fits after system_prompt
        assert len(messages) <= 3  # system + at most 1 user

    def test_build_messages_includes_agent_failed(self):
        """prompt_assembly.build_messages converts agent_failed events."""
        from harness.sdk.client import _PromptsClient
        pc = _PromptsClient(None)
        from harness.interfaces import Event
        events = [
            Event(event_id="e1", session_id="s", event_type="agent_started",
                  timestamp=0, source="test", payload={"input": "do it"}),
            Event(event_id="e2", session_id="s", event_type="agent_failed",
                  timestamp=1, source="test",
                  payload={"error": "Tool error: missing arg"}),
        ]
        messages = pc.build_messages("sys", events, 128000)
        error_msgs = [m for m in messages
                      if "Tool error" in str(m.get("content", ""))]
        assert len(error_msgs) >= 1

    def test_build_messages_surfaces_retry_error(self):
        """prompt_assembly.build_messages appends retry context to task."""
        from harness.sdk.client import _PromptsClient
        pc = _PromptsClient(None)
        from harness.interfaces import Event
        events = [
            Event(event_id="e1", session_id="s", event_type="agent_started",
                  timestamp=0, source="test",
                  payload={"input": "Execute",
                           "_retry_error": "checksum_mismatch"}),
        ]
        messages = pc.build_messages("sys", events, 128000)
        task_msgs = [m for m in messages
                     if "checksum_mismatch" in str(m.get("content", ""))]
        assert len(task_msgs) >= 1

    def test_build_messages_renders_a_memory_slot(self):
        """prompt_assembly renders a memory slot with the harness placeholder.

        The harness owns `<mem>…</mem>` (it identifies its own slots so a
        replacement pass cannot confuse one with the Task anchor) and the slot
        POSITION; the app owns the text.
        """
        from harness.sdk.client import _PromptsClient
        pc = _PromptsClient(None)
        from harness.interfaces import Event
        events = [
            Event(event_id="e1", session_id="s", event_type="memory_summary",
                  timestamp=0, source="test",
                  payload={"content": "Project: go2 RL. Key decisions: ...",
                           "slot": "before_task"}),
        ]
        messages = pc.build_messages("sys", events, 128000)
        mem_msgs = [m for m in messages
                    if "<mem>" in str(m.get("content", ""))]
        assert len(mem_msgs) == 1, messages
        assert mem_msgs[0]["content"] == "<mem>Project: go2 RL. Key decisions: ...</mem>"
        assert mem_msgs[0]["role"] == "user"

    def test_build_messages_respects_budget_ratio(self):
        """Lower budget_ratio causes earlier truncation.

        The budgets are derived from the measured cost of the fixture instead of
        hardcoded ratios: token counting is now exact (server /tokenize, see
        prompt_assembly._count_tokens), and previously-fixed ratios stopped
        forcing truncation — 10 events x ~1006 tokens = ~10060 tokens fits
        inside 128000*0.1 = 12800, so both ratios kept every event and the
        assertion became vacuous. Scaling to the measured total keeps the
        intent (smaller ratio => fewer messages) independent of payload size
        and of which tokenizer is in use.
        """
        from harness.sdk.client import _PromptsClient
        from harness.interfaces import Event
        from harness.prompt_assembly import _count_tokens
        pc = _PromptsClient(None)
        events = [
            Event(event_id=f"e{i}", session_id="s", event_type="agent_started",
                  timestamp=i, source="test",
                  payload={"input": "Hello world " * 500})
            for i in range(10)
        ]
        ctx = 128000
        total = sum(_count_tokens(str(e.payload), "deepseek") for e in events)

        # a budget below the total must drop events; a budget above it keeps all
        ratio_narrow = (total * 0.4) / ctx
        ratio_wide = (total * 3.0) / ctx
        messages = pc.build_messages("sys", events, ctx, budget_ratio=ratio_narrow)
        messages_wide = pc.build_messages("sys", events, ctx, budget_ratio=ratio_wide)
        assert len(messages) < len(messages_wide)
        # and the wide budget really did keep everything
        assert len(messages_wide) == len(events) + 1  # +1 system message


class TestPromptsDescribeTools:
    """prompts.describe_tools() — tool definition extraction."""

    def test_describe_all_tools(self):
        """describe_tools without filter returns all."""
        from harness.sdk.client import _PromptsClient
        from harness.tool_registry import ToolRegistry
        reg = ToolRegistry()
        reg.register("echo", "Echo", {"msg": str})(lambda msg: msg)
        reg.register("sum", "Sum", {"a": float, "b": float})(lambda a, b: str(a + b))

        pc = _PromptsClient(None)
        tools = pc.describe_tools(reg)
        assert len(tools) == 2
        names = {t["function"]["name"] for t in tools}
        assert names == {"echo", "sum"}

    def test_describe_with_filter(self):
        """describe_tools with filter_names returns only matching."""
        from harness.sdk.client import _PromptsClient
        from harness.tool_registry import ToolRegistry
        reg = ToolRegistry()
        reg.register("echo", "Echo", {"msg": str})(lambda msg: msg)
        reg.register("sum", "Sum", {"a": float, "b": float})(lambda a, b: str(a + b))

        pc = _PromptsClient(None)
        tools = pc.describe_tools(reg, filter_names=["echo"])
        assert len(tools) == 1
        assert tools[0]["function"]["name"] == "echo"


class TestAssembleWithAmend:
    """assemble_prompt(amend=...) — amend input injection."""

    def test_amend_appends_instruction(self):
        """amend={"input": ...} is appended as a modification instruction."""
        from harness.prompt_assembly import assemble_prompt

        result = assemble_prompt(
            base_prompt="You are a test agent.",
            amend={"input": "Use a learning rate of 0.001 instead."},
        )
        assert "[修改指令]" in result
        assert "Use a learning rate of 0.001 instead." in result

    def test_amend_no_amend_is_noop(self):
        """amend=None leaves the prompt unchanged."""
        from harness.prompt_assembly import assemble_prompt

        result = assemble_prompt(
            base_prompt="You are a test agent.",
            amend=None,
        )
        assert result == "You are a test agent."
        assert "[修改指令]" not in result

    def test_amend_with_vars_overrides(self):
        """amend vars override existing vars."""
        from harness.prompt_assembly import assemble_prompt

        result = assemble_prompt(
            base_prompt="Priority: {priority}.",
            vars={"priority": "low"},
            amend={"vars": {"priority": "high"}},
        )
        assert "Priority: high." in result
