"""Tests for harness/sdk/config.py — TDD vertical slice."""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

import pytest

from harness.sdk.config import HarnessSettings, LLMConfig, resolve_llm_profile


class TestHarnessSettings:
    """HarnessSettings loads configuration from YAML with env override."""

    def test_from_yaml_reads_local_config(self, tmp_path: Path) -> None:
        """HarnessSettings.from_yaml() reads values from a YAML file."""
        config_file = tmp_path / "local.yaml"
        config_file.write_text(
            'default_backend: "docker"\n'
            'hmac_secret: "test-secret-32bytes-long!!"\n'
            'sessions_dir: "./test_sessions/"\n'
            'audit_enabled: false\n',
            encoding="utf-8",
        )

        settings = HarnessSettings.from_yaml(str(config_file))

        assert settings.default_backend == "docker"
        assert settings.hmac_secret == "test-secret-32bytes-long!!"
        assert settings.sessions_dir == "./test_sessions/"
        assert settings.audit_enabled is False
        assert settings.audit_log_dir == "./sessions/"

    def test_env_var_overrides_yaml(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """Environment variables take precedence over YAML values."""
        config_file = tmp_path / "local.yaml"
        config_file.write_text('default_backend: "docker"\n', encoding="utf-8")

        monkeypatch.setenv("HARNESS_DEFAULT_BACKEND", "bubblewrap")

        settings = HarnessSettings.from_yaml(str(config_file))
        assert settings.default_backend == "bubblewrap"

    def test_missing_file_uses_defaults(self, tmp_path: Path) -> None:
        """If YAML file does not exist, use pydantic defaults."""
        missing = tmp_path / "nonexistent.yaml"

        settings = HarnessSettings.from_yaml(str(missing))
        assert settings.default_backend == "bubblewrap"
        # hmac_secret defaults to empty string (must be configured explicitly)
        assert settings.hmac_secret == ""


class TestResolveLLMProfile:
    """resolve_llm_profile() looks up LLM configs by name across tiers."""

    def test_project_level_config(self, tmp_path: Path) -> None:
        """Project-level configs/llm.yaml is checked first."""
        configs_dir = tmp_path / "configs"
        configs_dir.mkdir()
        llm_file = configs_dir / "llm.yaml"
        llm_file.write_text(
            'deepseek-prod:\n'
            '  provider: deepseek\n'
            '  api_key: "sk-proj-key"\n'
            '  model: deepseek-v4-flash\n',
            encoding="utf-8",
        )

        config = resolve_llm_profile("deepseek-prod", project_dir=tmp_path)

        assert config.provider == "deepseek"
        assert config.api_key == "sk-proj-key"
        assert config.model == "deepseek-v4-flash"

    def test_user_level_config(self, tmp_path: Path) -> None:
        """User-level ~/.harness/llm.d/{name}.yaml is checked second."""
        home = tmp_path / "home"
        home.mkdir()
        llm_dir = home / ".harness" / "llm.d"
        llm_dir.mkdir(parents=True)
        llm_file = llm_dir / "openai-gpt4o.yaml"
        llm_file.write_text(
            'provider: openai\n'
            'api_key: "sk-user-key"\n'
            'base_url: "https://api.openai.com/v1"\n'
            'model: gpt-4o\n',
            encoding="utf-8",
        )

        # Patch HOME so we don't touch real ~/.harness
        old_home = os.environ.get("HOME")
        try:
            os.environ["HOME"] = str(home)
            config = resolve_llm_profile("openai-gpt4o", project_dir=tmp_path / "no_project_configs")
        finally:
            if old_home is not None:
                os.environ["HOME"] = old_home
            else:
                os.environ.pop("HOME", None)

        assert config.provider == "openai"
        assert config.api_key == "sk-user-key"
        assert config.model == "gpt-4o"

    def test_env_var_expansion(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """${VAR} in YAML values is expanded from environment."""
        monkeypatch.setenv("MY_SECRET_KEY", "sk-from-env")

        configs_dir = tmp_path / "configs"
        configs_dir.mkdir()
        llm_file = configs_dir / "llm.yaml"
        llm_file.write_text(
            'test-profile:\n'
            '  api_key: "${MY_SECRET_KEY}"\n'
            '  model: test-model\n',
            encoding="utf-8",
        )

        config = resolve_llm_profile("test-profile", project_dir=tmp_path)
        assert config.api_key == "sk-from-env"

    def test_system_level_config(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """System-level /etc/harness/llm.d/{name}.yaml is checked as fallback."""
        # Patch /etc prefix for testing
        monkeypatch.setattr(
            "harness.sdk.config._SYSTEM_CONFIG_DIR",
            tmp_path / "etc" / "harness" / "llm.d",
        )
        system_dir = tmp_path / "etc" / "harness" / "llm.d"
        system_dir.mkdir(parents=True)
        llm_file = system_dir / "anthropic-claude.yaml"
        llm_file.write_text(
            'provider: anthropic\n'
            'api_key: "sk-system-key"\n'
            'base_url: "https://api.anthropic.com/v1"\n'
            'model: claude-3-opus\n',
            encoding="utf-8",
        )

        config = resolve_llm_profile("anthropic-claude", project_dir=tmp_path / "noproj")

        assert config.provider == "anthropic"
        assert config.api_key == "sk-system-key"
        assert config.model == "claude-3-opus"

    def test_missing_profile_raises(self, tmp_path: Path) -> None:
        """If profile not found anywhere, raise RuntimeError."""
        with pytest.raises(RuntimeError) as exc_info:
            resolve_llm_profile("nonexistent-profile", project_dir=tmp_path)

        assert "nonexistent-profile" in str(exc_info.value)
