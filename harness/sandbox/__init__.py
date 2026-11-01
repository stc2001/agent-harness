"""Module 4 — Sandbox isolation layer.

Dependency: interfaces.py → impl.py | docker_impl.py → backends.py → manager.py
All upper layers call manager, never impl/backends directly.

  ┌─────────────────────────────┐
  │        interfaces.py         │  ← 契约 (SandboxInterface, SandboxConfig)
  └──────────┬──────────────────┘
             │
     ┌───────┴────────┐
     │                │
┌────▼─────┐  ┌───────▼────────┐
│ impl.py  │  │ docker_impl.py  │  ← 实现 (spawn/execute/kill/is_alive)
│ bwrap    │  │ Docker          │
└────┬─────┘  └───────┬────────┘
     │                │
     └───────┬────────┘
             │
    ┌────────▼────────┐
    │  backends.py     │  ← 工厂注册表 (get_sandbox → cls())
    │  _registry={}    │
    └────────┬────────┘
             │
    ┌────────▼────────┐
    │  manager.py      │  ← API 层 (get_or_create/destroy/get_config/set_ttl)
    │ TTL reaper       │
    └────────┬────────┘
             │
    ┌────────▼────────┐
    │  app / SDK       │  ← 永远通过 manager 调用
    └─────────────────┘

Rule: always call manager, never impl directly for lifecycle.
Manager tracks instances, detects config changes, and runs
TTL reaper.  Calling impl.kill() bypasses manager tracking
and causes stale state.

How the layers compose:

- ``impl.py`` imports ``SandboxConfig`` from interfaces to read sandbox
  settings (firewall, shared_folders, env_vars) and execute commands
  against the host OS. It provides the concrete ``spawn``, ``execute``,
  ``kill``, and ``is_alive`` primitives.

- ``backends.py`` imports ``SandboxInterface`` from interfaces as the
  type contract it exposes to manager. It instantiates the backend
  class (``BubblewrapSandbox`` or ``DockerSandbox``) via a thin
  ``_registry[name]()`` factory call.

- ``manager.py`` queries ``backends.get_sandbox(name)`` to obtain a
  backend instance. The API surface it exposes (``get_or_create``,
  ``destroy``, ``get_config``, ``set_ttl``) flows from interfaces
  through backends into manager.

- At call time, ``manager.get_or_create("main")`` returns a backend
  reference that is actually ``BubblewrapSandbox``. Calling
  ``backend.spawn(config)`` executes the implementation in ``impl.py``,
  not manager or backends.

Usage:
    from harness.sandbox.manager import SandboxManager
"""

from harness.sandbox.backends import get_sandbox, list_backends, register_backend

__all__ = ["get_sandbox", "list_backends", "register_backend"]
