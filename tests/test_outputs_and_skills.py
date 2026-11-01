"""Tests for declare_output_path, install_skill tools, SandboxConfig.outputs."""

from __future__ import annotations

import pytest


class TestSandboxConfigOutputs:
    """SandboxConfig.outputs field."""

    def test_outputs_field_exists(self):
        """SandboxConfig has outputs dict field."""
        from harness.interfaces import SandboxConfig
        cfg = SandboxConfig(backend="bubblewrap")
        assert hasattr(cfg, "outputs")
        assert isinstance(cfg.outputs, dict)

    def test_outputs_default_empty(self):
        """outputs defaults to empty dict."""
        from harness.interfaces import SandboxConfig
        cfg = SandboxConfig(backend="bubblewrap")
        assert cfg.outputs == {}

    def test_outputs_with_values(self):
        """outputs accepts name: sandbox_path mapping."""
        from harness.interfaces import SandboxConfig
        cfg = SandboxConfig(
            backend="bubblewrap",
            outputs={"results": "/workspace/results"},
        )
        assert cfg.outputs["results"] == "/workspace/results"


class TestDeclareOutputPathTool:
    """declare_output_path built-in tool."""

    @pytest.fixture
    def reg_with_tool(self):
        from harness.tool_registry import ToolRegistry
        reg = ToolRegistry()
        from harness.tools.declare_output import register as _register
        _register(reg)
        return reg

    def test_declare_valid_path(self, reg_with_tool):
        """declare_output_path with valid outputs path returns confirmation."""
        from harness.interfaces import SandboxConfig
        cfg = SandboxConfig(
            backend="bubblewrap",
            outputs={"results": "/workspace/results"},
        )
        reg_with_tool.inject({"sandbox_config": cfg})
        handler = reg_with_tool.get_handler("declare_output_path")
        result = handler(path="/workspace/results")
        assert "declared" in result.lower() or "Output" in result

    def test_declare_invalid_path(self, reg_with_tool):
        """declare_output_path with path not in outputs raises PermissionError."""
        from harness.interfaces import SandboxConfig
        cfg = SandboxConfig(
            backend="bubblewrap",
            outputs={"results": "/workspace/results"},
        )
        reg_with_tool.inject({"sandbox_config": cfg})
        handler = reg_with_tool.get_handler("declare_output_path")
        with pytest.raises(PermissionError, match="not a valid output"):
            handler(path="/workspace/random")

    def test_declare_no_outputs_configured(self, reg_with_tool):
        """declare_output_path with empty outputs raises PermissionError."""
        from harness.interfaces import SandboxConfig
        cfg = SandboxConfig(backend="bubblewrap")
        reg_with_tool.inject({"sandbox_config": cfg})
        handler = reg_with_tool.get_handler("declare_output_path")
        with pytest.raises(PermissionError, match="No outputs configured"):
            handler(path="/anything")


class TestInstallSkillTool:
    """install_skill built-in tool — validates skill in sandbox."""

    @pytest.fixture
    def reg_with_tool(self):
        from harness.tool_registry import ToolRegistry
        reg = ToolRegistry()
        from harness.tools.install_skill import register as _register
        _register(reg)
        return reg

    def test_install_skill_success(self, reg_with_tool):
        """install_skill with valid skill path returns DONE."""
        reg_with_tool.inject({"execute": lambda cmd: ("---\nname: bash-expert\ndescription: Bash\n---\n# Body", "", 0)})
        handler = reg_with_tool.get_handler("install_skill")
        result = handler(name="bash-expert")
        assert "DONE" in result or "valid" in result.lower()

    def test_install_skill_not_found(self, reg_with_tool):
        """install_skill with missing file returns error."""
        reg_with_tool.inject({"execute": lambda cmd: ("", "not found", 1)})
        handler = reg_with_tool.get_handler("install_skill")
        result = handler(name="nobody")
        assert "Error" in result or "not found" in result.lower()

    def test_install_skill_bad_frontmatter(self, reg_with_tool):
        """install_skill with invalid SKILL.md returns error."""
        reg_with_tool.inject({"execute": lambda cmd: ("# No frontmatter", "", 0)})
        handler = reg_with_tool.get_handler("install_skill")
        result = handler(name="bad-skill")
        assert "Error" in result
