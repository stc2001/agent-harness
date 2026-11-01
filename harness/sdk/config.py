"""SDK configuration layer — Pydantic Settings + LLM profile resolution.

Supports three-tier config lookup:
    1. Environment variables (highest priority)
    2. Project-level configs/llm.yaml
    3. User-level ~/.harness/llm.d/*.yaml
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any

import yaml
from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class LLMConfig(BaseSettings):
    """Runtime LLM configuration (credentials + parameters)."""

    model_config = SettingsConfigDict(
        env_prefix="HARNESS_LLM_",
        extra="allow",
    )

    # ADR-055: which LLM driver renders vendor-neutral intent into this backend's
    # request shape.  Empty = infer from `backend` (local -> llamacpp,
    # cloud -> passthrough) or from a driver registered under the backend name.
    # Naming a driver that is not registered RAISES (never silently falls back):
    # an invisible renderer mismatch is exactly the bug the driver layer fixes.
    driver: str = ""
    # I-49: selects `tokenizer_dir/<provider>` as the FALLBACK tokenizer, used
    # only when the model server's own `/tokenize` endpoint is unreachable.
    # It must match the model being served: the DeepSeek vocab contains no CJK
    # at all, so pairing it with a Chinese model counts Chinese as 0 tokens
    # (that vocab has 0 CJK entries; a served Spark X2.5 has 33,986).
    # Set this in `configs/llm.yaml` for a self-hosted model.  Default stays
    # "deepseek" so behaviour is unchanged unless configured.
    provider: str = "deepseek"
    api_key: str = Field(default="", description="API key; empty → read from env var")
    base_url: str = "https://api.deepseek.com/v1"
    model: str = "deepseek-v4-flash"
    # I-13: backend kind (cloud|local), optional LoRA adapter, endpoint alias.
    backend: str = "cloud"
    endpoint: str = ""
    adapter: str = ""
    # I-14: model-service pool key (local backends route through the pool).
    model_service: str = ""
    timeout: int = 30
    temperature: float = 0.7
    max_tokens: int = 4096
    context_window: int = 128000
    # Local-backend escape hatch: extra kwargs handed to the server's chat
    # template. llama.cpp accepts "chat_template_kwargs", which is how a
    # template's default thinking mode is turned OFF:
    #     chat_template_kwargs: {enable_thinking: false}
    # Without this, a reasoning model spends max_tokens on its reasoning block
    # and /v1/chat/completions can return EMPTY content.
    chat_template_kwargs: dict = Field(default_factory=dict)
    # Override the hardcoded temperature=0.0 used in _call_llm_once.
    # None = keep upstream behaviour (greedy).
    temperature_override: float | None = None


class HarnessSettings(BaseSettings):
    """Global Harness settings loaded from harness/config/local.yaml."""

    model_config = SettingsConfigDict(
        env_prefix="HARNESS_",
        env_nested_delimiter="__",
        extra="ignore",
        populate_by_name=True,
    )

    deepseek_api_key: str = Field(default="", validation_alias="DEEPSEEK_API_KEY")
    base_url: str = "https://api.deepseek.com/v1"
    hmac_secret: str = ""
    default_backend: str = "bubblewrap"
    sessions_dir: str = "./sessions/"
    audit_enabled: bool = True
    audit_log_dir: str = "./sessions/"

    @classmethod
    def from_yaml(cls, path: str = "harness/config/local.yaml") -> "HarnessSettings":
        """Load settings from YAML/TOML file, with environment variable override.

        Priority: environment variables > config file > pydantic defaults.
        Auto-detects format by trying YAML first, then TOML.
        """
        file_data: dict[str, Any] = {}
        p = Path(path)
        if p.exists():
            content = p.read_text(encoding="utf-8")
            raw = _try_parse_yaml(content) or _try_parse_toml(content) or {}
            if isinstance(raw, dict):
                file_data = _flatten_dict(raw)

        # Map flattened keys back to pydantic field names.
        # TOML nesting like [security] hmac_secret = "x" becomes security_hmac_secret.
        # We strip known section prefixes so the field matches.
        known_prefixes = ("security_", "sandbox_", "orchestrator_", "session_")
        mapped: dict[str, Any] = {}
        for key, value in file_data.items():
            mapped_key = key
            for prefix in known_prefixes:
                if key.startswith(prefix):
                    mapped_key = key[len(prefix):]
                    break
            mapped[mapped_key] = value

        # Environment variables override file values
        env_prefix = cls.model_config.get("env_prefix", "HARNESS_")
        for key, value in os.environ.items():
            if key.startswith(env_prefix):
                field_name = key[len(env_prefix):].lower()
                mapped[field_name] = value

        return cls(**mapped)


def _flatten_dict(d: dict[str, Any], prefix: str = "") -> dict[str, Any]:
    """Flatten nested dict to flat keys (e.g. {'sandbox': {'backend': 'x'}} → {'sandbox_backend': 'x'}).

    Used to map nested YAML structure to flat pydantic fields.
    """
    result: dict[str, Any] = {}
    for key, value in d.items():
        new_key = f"{prefix}{key}" if not prefix else f"{prefix}_{key}"
        if isinstance(value, dict):
            result.update(_flatten_dict(value, new_key))
        else:
            result[new_key] = value
    return result


def _try_parse_yaml(content: str) -> dict[str, Any] | None:
    """Try to parse content as YAML; return None on failure."""
    import yaml
    try:
        result = yaml.safe_load(content)
        return result if isinstance(result, dict) else None
    except yaml.YAMLError:
        return None


def _try_parse_toml(content: str) -> dict[str, Any] | None:
    """Try to parse content as TOML; return None on failure."""
    try:
        import tomllib
    except ImportError:
        try:
            import tomli as tomllib
        except ImportError:
            return None
    try:
        return tomllib.loads(content)
    except Exception:
        return None


def _expand_env_vars(value: str) -> str:
    """Replace ${VAR} or $VAR with environment variable values."""
    from string import Template
    return Template(value).safe_substitute(os.environ)


def _load_yaml_with_env_expansion(path: Path) -> dict[str, Any]:
    """Load YAML and expand ${VAR} / $VAR in string values recursively."""
    with path.open("r", encoding="utf-8") as f:
        raw = yaml.safe_load(f) or {}

    def expand(obj: Any) -> Any:
        if isinstance(obj, str):
            return _expand_env_vars(obj)
        if isinstance(obj, dict):
            return {k: expand(v) for k, v in obj.items()}
        if isinstance(obj, list):
            return [expand(v) for v in obj]
        return obj

    return expand(raw)


# System-level config directory (overridable for testing)
_SYSTEM_CONFIG_DIR = Path("/etc/harness/llm.d")


def resolve_llm_profile(profile_name: str, project_dir: Path | None = None) -> LLMConfig:
    """Resolve an LLM profile by name across three tiers.

    Lookup order (later overrides earlier):
        1. System-level: /etc/harness/llm.d/{profile_name}.yaml
        2. User-level:   ~/.harness/llm.d/{profile_name}.yaml
        3. Project-level: {project_dir}/configs/llm.yaml  →  profile matching name

    Project-level wins because it is most specific (per-repo overrides).
    """
    # 3. Project-level lookup (highest priority — most specific)
    if project_dir is not None:
        project_llm = project_dir / "configs" / "llm.yaml"
        if project_llm.exists():
            data = _load_yaml_with_env_expansion(project_llm)
            profiles = data if isinstance(data, dict) else {}
            if profile_name in profiles:
                return LLMConfig(**profiles[profile_name])

    # 2. User-level lookup
    user_dir = Path.home() / ".harness" / "llm.d"
    user_file = user_dir / f"{profile_name}.yaml"
    if user_file.exists():
        data = _load_yaml_with_env_expansion(user_file)
        return LLMConfig(**data)

    # 1. System-level lookup (lowest priority — system-wide defaults)
    system_file = _SYSTEM_CONFIG_DIR / f"{profile_name}.yaml"
    if system_file.exists():
        data = _load_yaml_with_env_expansion(system_file)
        return LLMConfig(**data)

    # 4. Environment variable fallback for well-known profiles
    env_key = os.environ.get("DEEPSEEK_API_KEY", "")
    if env_key and profile_name in ("default", "deepseek"):
        return LLMConfig(api_key=env_key)

    raise RuntimeError(
        f"LLM profile '{profile_name}' not found. "
        f"Searched: project configs/llm.yaml, ~/.harness/llm.d/{profile_name}.yaml, "
        f"/etc/harness/llm.d/{profile_name}.yaml"
    )


def available_llm_profiles(project_dir: Path | None = None) -> list[str]:
    """Names of LLM profiles visible to a deployment (sorted, de-duplicated).

    Mirrors `resolve_llm_profile`'s lookup tiers: project `configs/llm.yaml`
    keys, then `~/.harness/llm.d/*.yaml` and `/etc/harness/llm.d/*.yaml` stems.
    Used by the SDK's `client.config.profiles()` surface; profile VALUES are
    still resolved (and env-expanded) by `resolve_llm_profile`.
    """
    names: set[str] = set()
    if project_dir is not None:
        project_llm = project_dir / "configs" / "llm.yaml"
        if project_llm.exists():
            try:
                data = yaml.safe_load(project_llm.read_text(encoding="utf-8")) or {}
            except yaml.YAMLError:
                data = {}
            if isinstance(data, dict):
                names.update(str(k) for k in data)
    for directory in (Path.home() / ".harness" / "llm.d", _SYSTEM_CONFIG_DIR):
        if directory.is_dir():
            names.update(p.stem for p in directory.glob("*.yaml"))
    return sorted(names)
