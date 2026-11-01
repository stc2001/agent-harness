"""Tests for ToolRegistry — tool registration, DI, definition generation, and edge cases.

TDD Slice 1/9 of the ToolRegistry + built-in tools refactoring.
"""

from __future__ import annotations

import pytest


class TestRegistration:
    """@ToolRegistry.register decorator and basic queries."""

    def test_register_pure_function(self) -> None:
        from harness.tool_registry import ToolRegistry
        reg = ToolRegistry()

        @reg.register("echo", "Echo a message", {"message": str})
        def echo(message: str) -> str:
            return message

        assert reg.list_tools() == ["echo"]
        handler = reg.get_handler("echo")
        assert handler("hello") == "hello"

    def test_get_definitions_openai_format(self) -> None:
        from harness.tool_registry import ToolRegistry
        reg = ToolRegistry()

        @reg.register("add", "Add two numbers", {"a": float, "b": float})
        def add(a, b): return str(a + b)

        defs = reg.get_definitions()
        assert len(defs) == 1
        fn = defs[0]["function"]
        assert fn["name"] == "add"
        assert fn["description"] == "Add two numbers"
        assert defs[0]["type"] == "function"
        params = fn["parameters"]
        assert params["type"] == "object"
        assert set(params["required"]) == {"a", "b"}
        assert params["properties"]["a"]["type"] == "number"
        assert params["properties"]["b"]["type"] == "number"

    def test_get_definitions_json_types(self) -> None:
        """Python types map to correct JSON schema types."""
        from harness.tool_registry import ToolRegistry
        reg = ToolRegistry()

        @reg.register("typing", "desc", {
            "s": str, "i": int, "f": float, "b": bool,
        })
        def typing_(s, i, f, b): pass

        props = reg.get_definitions()[0]["function"]["parameters"]["properties"]
        assert props["s"]["type"] == "string"
        assert props["i"]["type"] == "integer"
        assert props["f"]["type"] == "number"
        assert props["b"]["type"] == "boolean"

    def test_duplicate_name_raises(self) -> None:
        from harness.tool_registry import ToolRegistry
        reg = ToolRegistry()

        reg.register("dup", "First", {"x": str})(lambda x: x)
        with pytest.raises(ValueError, match="dup"):
            reg.register("dup", "Second", {"x": str})(lambda x: x)

    def test_get_handler_unknown_raises(self) -> None:
        from harness.tool_registry import ToolRegistry
        reg = ToolRegistry()
        with pytest.raises(KeyError):
            reg.get_handler("nobody")

    def test_empty_registry(self) -> None:
        from harness.tool_registry import ToolRegistry
        reg = ToolRegistry()
        assert reg.get_definitions() == []
        assert reg.list_tools() == []

    def test_multiple_tools_order_preserved(self) -> None:
        from harness.tool_registry import ToolRegistry
        reg = ToolRegistry()

        @reg.register("z", "...", {"x": str})
        def z(x): return x

        @reg.register("a", "...", {"x": str})
        def a(x): return x

        assert reg.list_tools() == ["z", "a"]


class TestDI:
    """Dependency injection — declared deps get bound at inject() time."""

    def test_declared_deps_bound_as_kwargs(self) -> None:
        from harness.tool_registry import ToolRegistry
        reg = ToolRegistry()

        @reg.register("rd", "Read", {"path": str}, deps=["sandbox", "perms"])
        def rd(path, sandbox=None, perms=None):
            return f"{sandbox}:{perms}:{path}"

        reg.inject({"sandbox": "sbx1", "perms": "rw"})
        result = reg.get_handler("rd")("/tmp/x")
        assert result == "sbx1:rw:/tmp/x"

    def test_no_deps_unaffected_by_inject(self) -> None:
        from harness.tool_registry import ToolRegistry
        reg = ToolRegistry()

        @reg.register("echo", "Echo", {"message": str})
        def echo(message): return message

        reg.inject({"sandbox": "fake"})
        assert reg.get_handler("echo")("hi") == "hi"

    def test_missing_dep_raises(self) -> None:
        from harness.tool_registry import ToolRegistry
        reg = ToolRegistry()

        @reg.register("needy", "...", {"x": str}, deps=["sandbox"])
        def needy(x, sandbox=None): return x

        with pytest.raises(ValueError, match="sandbox"):
            reg.inject({})

    def test_original_args_preserved(self) -> None:
        """Tool positional args still work; deps are kwargs-only."""
        from harness.tool_registry import ToolRegistry
        reg = ToolRegistry()

        @reg.register("math", "Do math", {"a": int, "b": int}, deps=["log"])
        def math(a, b, log=None):
            log.append(f"{a}+{b}")
            return a + b

        audit = []
        reg.inject({"log": audit})
        result = reg.get_handler("math")(3, 4)
        assert result == 7
        assert audit == ["3+4"]

    def test_double_inject_is_idempotent(self) -> None:
        from harness.tool_registry import ToolRegistry
        reg = ToolRegistry()

        @reg.register("rd", "Read", {"path": str}, deps=["sandbox"])
        def rd(path, sandbox=None): return sandbox + path

        reg.inject({"sandbox": "s1"})
        reg.inject({"sandbox": "s2"})  # second inject overwrites
        assert reg.get_handler("rd")("/x") == "s2/x"


class TestSpawnSubagentPattern:
    """spawn_subagent needs per-call deps injection (spawner varies)."""

    def test_tool_without_deps_can_still_accept_kwargs(self) -> None:
        """A tool that declares no deps can be called with extra kwargs."""
        from harness.tool_registry import ToolRegistry
        reg = ToolRegistry()

        @reg.register("echo", "Echo", {"msg": str})
        def echo(msg, **kwargs):
            return msg

        result = reg.get_handler("echo")("hi", spawner="fake")
        assert result == "hi"

    def test_list_agents_no_deps(self) -> None:
        from harness.tool_registry import ToolRegistry
        reg = ToolRegistry()

        @reg.register("list_agents", "List", {})
        def list_agents():
            return "agent1, agent2"

        assert reg.get_handler("list_agents")() == "agent1, agent2"
