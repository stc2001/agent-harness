"""Module 3 — Agent Spec implementation."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import yaml

from harness.interfaces import AgentSpec, AgentSpecNotFoundError, AgentSpecValidationError


class AgentSpecService:
    """Load and validate agent definitions from a YAML registry."""

    def __init__(self, registry_path: str = "./registry/") -> None:
        self.registry_path = Path(registry_path)
        self._specs: dict[str, "AgentSpec"] = {}

    def register(self, spec) -> None:
        """Register an AgentSpec programmatically (no YAML file)."""
        from harness.interfaces import AgentSpec
        errors = self.validate(spec)
        if errors:
            raise ValueError(f"Spec validation failed: {'; '.join(errors)}")
        self._specs[spec.name] = spec

    def _file_path(self, name: str) -> Path:
        return self.registry_path / f"{name}.yaml"

    def load(self, name: str) -> AgentSpec:
        """Load an agent specification from registry/{name}.yaml.

        Checks in-memory registered specs first.

        Args:
            name: Agent name (matches the YAML filename without extension).

        Returns:
            Parsed and validated AgentSpec.

        Raises:
            AgentSpecNotFoundError: If the YAML file does not exist.
            AgentSpecValidationError: If the YAML is malformed or missing required fields.
        """
        if name in self._specs:
            return self._specs[name]

        file_path = self._file_path(name)
        if not file_path.exists():
            raise AgentSpecNotFoundError(f"Agent spec not found: {file_path}")

        try:
            with file_path.open("r", encoding="utf-8") as f:
                raw: dict[str, Any] = yaml.safe_load(f) or {}
        except yaml.YAMLError as exc:
            raise AgentSpecValidationError(f"Invalid YAML in {file_path}: {exc}") from exc
        except OSError as exc:
            raise AgentSpecValidationError(f"Cannot read {file_path}: {exc}") from exc

        if not isinstance(raw, dict):
            raise AgentSpecValidationError(
                f"Expected a YAML mapping in {file_path}, got {type(raw).__name__}"
            )

        required_fields = ("name", "description", "model")
        missing = [field for field in required_fields if field not in raw]
        if missing:
            raise AgentSpecValidationError(
                f"Missing required fields in {file_path}: {', '.join(missing)}"
            )

        try:
            spec = AgentSpec(**raw)
        except TypeError as exc:
            raise AgentSpecValidationError(
                f"Failed to construct AgentSpec from {file_path}: {exc}"
            ) from exc

        errors = self.validate(spec)
        if errors:
            raise AgentSpecValidationError(
                f"Validation failed for {file_path}: " + "; ".join(errors)
            )

        return spec

    def validate(self, spec: AgentSpec) -> list[str]:
        """Validate an AgentSpec and return a list of error messages.

        An empty list means the spec is valid.

        Args:
            spec: The AgentSpec to validate.

        Returns:
            List of human-readable error strings (empty = valid).
        """
        errors: list[str] = []

        if not spec.name or not spec.name.strip():
            errors.append("'name' must be non-empty")

        # ADR-054 §3: an empty `model` is legitimate — it means "the profile
        # supplies it" (registration is compile-only and must not resolve
        # profiles; resolution happens at execute time, where an empty model on
        # BOTH sides fails loudly via resolve_spec_model).  Only guard the shape:
        # a non-empty value must not be whitespace-only.
        if spec.model and not spec.model.strip():
            errors.append("'model', when set, must not be whitespace-only")

        # I-13: backend must be auto|cloud|local
        backend = getattr(spec, "backend", "auto")
        if backend not in ("auto", "cloud", "local"):
            errors.append(
                f"'backend' must be one of auto|cloud|local, got {backend!r}")

        for tool in spec.tools:
            if " " in tool:
                errors.append(f"tool name must not contain spaces: {tool!r}")

        return errors

    def list_all(self) -> list[str]:
        """List all available agent names in the registry.

        Returns:
            Sorted list of agent names (includes both in-memory and YAML specs).
        """
        names = list(self._specs.keys())
        if self.registry_path.exists():
            for p in self.registry_path.iterdir():
                if p.is_file() and p.suffix == ".yaml":
                    names.append(p.stem)
        return sorted(set(names))
