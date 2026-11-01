"""ToolRegistry — lightweight in-process tool registration with DI.

Modeled after Claude Code's in-process MCP SDK pattern:
register Python functions as LLM-callable tools with OpenAI function
calling schemas, and inject runtime dependencies (sandbox, permissions,
execute, etc.) at call time via explicit dependency declaration.

No external dependencies. No network server. No async.
"""

from __future__ import annotations

from typing import Any, Callable


class ToolRegistry:
    """Register Python functions as LLM-callable tools.

    Usage::

        reg = ToolRegistry()

        @reg.register("echo", "Echo a message", {"message": str})
        def echo(message: str) -> str:
            return message

        @reg.register("read", "Read file", {"path": str}, deps=["sandbox", "perms"])
        def read(path, sandbox=None, perms=None) -> str: ...

        reg.inject({"sandbox": my_sandbox, "perms": my_perms})
        defs = reg.get_definitions()  # → OpenAI function-calling format
        handler = reg.get_handler("read")
        result = handler("/workspace/x.txt")
    """

    def __init__(self) -> None:
        self._tools: dict[str, dict[str, Any]] = {}

    # ── Registration ────────────────────────────────────────

    def register(
        self,
        name: str,
        description: str,
        parameters: dict[str, type],
        deps: list[str] | None = None,
        param_descriptions: dict[str, str] | None = None,
        examples: list[dict[str, Any]] | None = None,
    ) -> Callable[[Callable], Callable]:
        """Decorator: register a tool function.

        Args:
            name: Unique tool name (OpenAI function name).
            description: Human-readable description shown to the LLM.
            parameters: Mapping of parameter names to Python types
                        (str, int, float, bool, list, dict).
            deps: Names of runtime dependencies to inject at call time,
                  e.g. ``["sandbox", "permissions"]``.
            param_descriptions: Human-readable descriptions for each
                                parameter. Keys must match parameters dict.
                                If omitted, parameter name is used as
                                description (backward-compatible).
            examples: Few-shot examples included in the tool description
                      to help the LLM generate correct JSON arguments.

        Returns:
            Decorator that registers the function and returns it unchanged.

        Raises:
            ValueError: If a tool with the same name is already registered.
        """
        if name in self._tools:
            raise ValueError(f"Tool '{name}' is already registered")

        deps = deps or []

        def decorator(fn: Callable) -> Callable:
            self._tools[name] = {
                "fn": fn,
                "_original": fn,
                "description": description,
                "parameters": parameters,
                "deps": deps,
                "param_descriptions": param_descriptions or {},
                "examples": examples or [],
            }
            return fn

        return decorator

    # ── Queries ─────────────────────────────────────────────

    def list_tools(self) -> list[str]:
        """Return registered tool names in registration order."""
        return list(self._tools.keys())

    def get_handler(self, name: str) -> Callable:
        """Return the callable handler for a registered tool.

        After ``inject()``, the returned handler has its declared
        dependencies pre-bound as keyword arguments.

        Raises:
            KeyError: If the tool name is not registered.
        """
        entry = self._tools.get(name)
        if entry is None:
            raise KeyError(f"Tool '{name}' is not registered")
        return entry["fn"]

    def get_definitions(self) -> list[dict[str, Any]]:
        """Return OpenAI function-calling compatible tool definitions."""
        import json
        definitions: list[dict[str, Any]] = []
        for name, entry in self._tools.items():
            properties: dict[str, Any] = {}
            required: list[str] = []
            param_descs = entry.get("param_descriptions", {})
            for param_name, param_type in entry["parameters"].items():
                properties[param_name] = {
                    "type": _python_type_to_json(param_type),
                    "description": param_descs.get(param_name, param_name),
                }
                required.append(param_name)

            # Build description with examples appended
            full_desc = entry["description"]
            examples = entry.get("examples", [])
            if examples:
                full_desc += "\n\nExamples:"
                for ex in examples:
                    full_desc += f"\n{json.dumps(ex, ensure_ascii=False)}"

            definitions.append({
                "type": "function",
                "function": {
                    "name": name,
                    "description": full_desc,
                    "parameters": {
                        "type": "object",
                        "properties": properties,
                        "required": required,
                    },
                },
            })
        return definitions

    # ── Dependency injection ────────────────────────────────

    def inject(self, deps: dict[str, Any]) -> None:
        """Bind runtime dependencies to registered tools.

        For each tool that declared dependencies, wraps its handler
        with a partial application of the matching deps from the
        provided dictionary.  Repeated calls are idempotent — each
        inject always operates on the *original* function, not on
        previously-wrapped handlers.

        Raises:
            ValueError: If any declared dependency is missing from
                        the provided deps dictionary.
        """
        for name, entry in self._tools.items():
            declared = entry.get("deps", [])
            if not declared:
                continue

            missing = [d for d in declared if d not in deps]
            if missing:
                raise ValueError(
                    f"Tool '{name}' requires dependencies {missing} "
                    f"but inject() did not provide them"
                )

            # Always wrap the ORIGINAL function to keep inject idempotent
            original = entry["_original"]
            entry["fn"] = _make_bound(
                original,
                {d: deps[d] for d in declared},
                declared,
            )

    def copy(self) -> "ToolRegistry":
        """Return an independent copy with dependencies UNBOUND.

        ADR-015 (2026-10-19): a client-level registry is shared by many runtimes,
        but `inject()` binds dependencies (sandbox execute, permissions, …) PER
        runtime.  Handing every runtime a copy keeps those bindings isolated while
        still letting an app register tools once on the client.
        """
        cloned = ToolRegistry()
        cloned._tools = {
            name: {**entry, "fn": entry["_original"]}
            for name, entry in self._tools.items()
        }
        return cloned


# ── Helpers ──────────────────────────────────────────────────


def _make_bound(fn: Callable, bound: dict[str, Any],
                order: list[str]) -> Callable:
    """Create a handler with deps pre-bound as keyword arguments."""
    def handler(*args: Any, **kwargs: Any) -> Any:
        return fn(*args, **{d: bound[d] for d in order}, **kwargs)
    return handler


def _python_type_to_json(ptype: type) -> str:
    """Map Python type to JSON Schema type string."""
    mapping: dict[type, str] = {
        str: "string",
        int: "integer",
        float: "number",
        bool: "boolean",
        list: "array",
        dict: "object",
    }
    return mapping.get(ptype, "string")
