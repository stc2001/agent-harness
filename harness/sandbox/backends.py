"""Sandbox backend registry — multi-backend support.

Agent Harness supports multiple sandbox backends (Bubblewrap, Docker, etc.)
through a common SandboxInterface. The registry allows runtime selection
of the backend without hardcoding imports in upper-layer modules.

Usage:
    from harness.sandbox.backends import get_sandbox

    sandbox = get_sandbox("bubblewrap")  # or "docker"
    sandbox.spawn(config, token)
"""

from __future__ import annotations

from harness.interfaces import SandboxInterface, SandboxConfig, SandboxSpawnError

_registry: dict[str, type[SandboxInterface]] = {}


def register_backend(name: str, backend_cls: type[SandboxInterface]) -> None:
    """Register a sandbox backend class under a given name.

    Args:
        name: Backend identifier (e.g. "bubblewrap", "docker").
        backend_cls: A class implementing SandboxInterface.
    """
    _registry[name] = backend_cls


def get_sandbox(backend: str = "bubblewrap") -> SandboxInterface:
    """Factory: instantiate a sandbox backend by name.

    Args:
        backend: Backend name (e.g. "bubblewrap", "docker").

    Returns:
        A new sandbox instance implementing SandboxInterface.

    Raises:
        ValueError: If the backend name is not registered.
    """
    cls = _registry.get(backend)
    if cls is None:
        available = list(_registry.keys()) if _registry else ["(none registered)"]
        raise ValueError(
            f"Unknown sandbox backend: '{backend}'. "
            f"Available: {available}. "
            f"Did you import the backend module?"
        )
    return cls()


def list_backends() -> list[str]:
    """Return sorted list of registered backend names."""
    return sorted(_registry.keys())


# ─────────────────────────────────────────────────────────────
# Auto-register built-in backends on import
# ─────────────────────────────────────────────────────────────

def _auto_register() -> None:
    """Register built-in backends. Import errors are non-fatal
    (the backend simply won't be available)."""
    try:
        from harness.sandbox.impl import BubblewrapSandbox
        register_backend("bubblewrap", BubblewrapSandbox)
    except ImportError:
        pass

    try:
        from harness.sandbox.docker_impl import DockerSandbox
        register_backend("docker", DockerSandbox)
    except ImportError:
        pass


_auto_register()
