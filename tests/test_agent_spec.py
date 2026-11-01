"""Tests for Module 3 — Agent Spec."""

from __future__ import annotations

import pytest

from harness.agent_spec.impl import AgentSpecService
from harness.interfaces import (
    AgentSpec,
    AgentSpecNotFoundError,
    AgentSpecValidationError,
)


@pytest.fixture
def service(tmp_path):
    """Return an AgentSpecService backed by a temporary registry."""
    return AgentSpecService(registry_path=str(tmp_path))


@pytest.fixture
def valid_yaml():
    return """
name: test-agent
description: "A test agent"
sandbox: main
capabilities: [toolcall]
tools: [echo, sum, sandbox_execute]
model: deepseek-v4-flash
system_prompt: "You are a test agent."
max_iterations: 3
permissions:
  subagent_spawn: false
  file_access:
    /workspace: read_write
"""


class TestLoad:
    def test_load_valid_spec(self, service, tmp_path, valid_yaml):
        (tmp_path / "test-agent.yaml").write_text(valid_yaml, encoding="utf-8")
        spec = service.load("test-agent")
        assert isinstance(spec, AgentSpec)
        assert spec.name == "test-agent"
        assert spec.description == "A test agent"
        assert spec.model == "deepseek-v4-flash"
        assert spec.tools == ["echo", "sum", "sandbox_execute"]

    def test_load_not_found(self, service):
        with pytest.raises(AgentSpecNotFoundError):
            service.load("nonexistent")

    def test_load_malformed_yaml(self, service, tmp_path):
        (tmp_path / "bad.yaml").write_text("{ not yaml: [", encoding="utf-8")
        with pytest.raises(AgentSpecValidationError):
            service.load("bad")

    def test_load_missing_required_field_name(self, service, tmp_path):
        content = """
description: "missing name"
model: deepseek-v4-flash
"""
        (tmp_path / "no-name.yaml").write_text(content, encoding="utf-8")
        with pytest.raises(AgentSpecValidationError) as exc_info:
            service.load("no-name")
        assert "name" in str(exc_info.value).lower()

    def test_load_missing_required_field_description(self, service, tmp_path):
        content = """
name: no-desc
model: deepseek-v4-flash
"""
        (tmp_path / "no-desc.yaml").write_text(content, encoding="utf-8")
        with pytest.raises(AgentSpecValidationError) as exc_info:
            service.load("no-desc")
        assert "description" in str(exc_info.value).lower()

    def test_load_missing_required_field_model(self, service, tmp_path):
        content = """
name: no-model
description: "missing model"
"""
        (tmp_path / "no-model.yaml").write_text(content, encoding="utf-8")
        with pytest.raises(AgentSpecValidationError) as exc_info:
            service.load("no-model")
        assert "model" in str(exc_info.value).lower()

    def test_load_scalar_instead_of_mapping(self, service, tmp_path):
        (tmp_path / "scalar.yaml").write_text("just a string", encoding="utf-8")
        with pytest.raises(AgentSpecValidationError):
            service.load("scalar")


class TestValidate:
    def test_validate_passes(self, service, tmp_path, valid_yaml):
        (tmp_path / "test-agent.yaml").write_text(valid_yaml, encoding="utf-8")
        spec = service.load("test-agent")
        assert service.validate(spec) == []

    def test_validate_empty_name(self, service):
        spec = AgentSpec(name="", description="x", model="m")
        errors = service.validate(spec)
        assert any("name" in e.lower() for e in errors)

    def test_validate_empty_model_is_allowed(self, service):
        """ADR-054 §3: model="" means "the profile supplies it" — NOT an error.

        Registration is compile-only and must not resolve profiles, so the
        "neither side has a model" case is caught at execute time by
        resolve_spec_model, not here.
        """
        spec = AgentSpec(name="x", description="x", model="")
        errors = service.validate(spec)
        assert not any("model" in e.lower() for e in errors), errors

    def test_validate_whitespace_only_model_still_rejected(self, service):
        spec = AgentSpec(name="x", description="x", model="   ")
        errors = service.validate(spec)
        assert any("model" in e.lower() for e in errors)

    def test_validate_tool_with_space(self, service):
        spec = AgentSpec(name="x", description="x", model="m", tools=["bad tool"])
        errors = service.validate(spec)
        assert any("space" in e.lower() for e in errors)

    def test_validate_multiple_errors(self, service):
        # model="" is no longer an error (ADR-054 §3), so 2 remain: name, tool.
        # A whitespace-only model WOULD still be one.
        spec = AgentSpec(name="", description="x", model="   ", tools=["bad tool"])
        errors = service.validate(spec)
        assert len(errors) == 3


class TestListAll:
    def test_list_all_sorted(self, service, tmp_path):
        (tmp_path / "charlie.yaml").write_text("name: c\ndescription: c\nmodel: m\n")
        (tmp_path / "alpha.yaml").write_text("name: a\ndescription: a\nmodel: m\n")
        (tmp_path / "bravo.yaml").write_text("name: b\ndescription: b\nmodel: m\n")
        # non-yaml file should be ignored
        (tmp_path / "readme.md").write_text("hello")

        names = service.list_all()
        assert names == ["alpha", "bravo", "charlie"]

    def test_list_all_empty_registry(self, tmp_path):
        empty_service = AgentSpecService(registry_path=str(tmp_path))
        assert empty_service.list_all() == []

    def test_list_all_missing_registry(self, tmp_path):
        missing_service = AgentSpecService(registry_path=str(tmp_path / "nope"))
        assert missing_service.list_all() == []


class TestIntegrationWithRealRegistry:
    """Tests that exercise the committed sample registry files."""

    def test_load_sample_test_worker(self):
        service = AgentSpecService(registry_path="./registry/")
        spec = service.load("test-worker")
        assert spec.name == "test-worker"
        assert spec.model == "deepseek-v4-flash"

    def test_load_sample_test_validator(self):
        service = AgentSpecService(registry_path="./registry/")
        spec = service.load("test-validator")
        assert spec.name == "test-validator"

    def test_list_all_real_registry(self):
        service = AgentSpecService(registry_path="./registry/")
        names = service.list_all()
        assert "test-worker" in names
        assert "test-validator" in names
