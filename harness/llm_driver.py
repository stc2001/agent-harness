"""LLM driver layer (ADR-055): vendor rendering, out of the framework.

THE PROBLEM THIS SOLVES
  The framework holds an *intent* ("enable thinking"); the *field name* that
  expresses it is backend-specific.  MEASURED on llama.cpp `b11312-0c1e57098`
  (temperature=0, identical prompt):

      (no switch)                                        reasoning=913
      top-level "enable_thinking": true                  reasoning=863  ignored
      top-level "enable_thinking": false                 reasoning=863  IGNORED
      top-level "thinking": true                         reasoning=863  ignored
      "chat_template_kwargs": {"enable_thinking": false} reasoning=0    works

  That build only reads `chat_template_kwargs` (the top-level toggle is upstream
  PR #22336, newer than our build), and passing the wrong shape is SILENT.

THE SPLIT
  * framework  — SEMANTICS: who wins (ADR-054 P/Q), when to audit
  * driver     — RENDERING: which field name carries the intent
  * app        — may register its own driver, so a new backend needs no framework
                 change (this is the point of the layer, ADR-038)

Drivers are pure: no IO, no credentials, no client.  That keeps them trivially
testable and keeps ADR-041 §4 true (no vendor branches in the runtime).
"""
from __future__ import annotations

from typing import Any, Protocol, runtime_checkable

__all__ = [
    "LLMDriver", "PassthroughDriver", "LlamaCppDriver", "DeepSeekDriver",
    "register_driver", "get_driver", "registered_drivers",
    "default_driver_for", "driver_for", "DEFAULT_DRIVER",
]


@runtime_checkable
class LLMDriver(Protocol):
    """Renders vendor-neutral intent into a backend's request shape."""

    name: str

    def build_extra_body(self, *, spec: Any, cfg: Any,
                         thinking: bool | None) -> dict:
        """Return the `extra_body` this backend expects.

        `thinking` carries the ALREADY RESOLVED intent:
            None  -> no opinion; leave the profile's configuration alone
            True  -> thinking on
            False -> thinking off
        Precedence is decided by the framework (ADR-054), not here.
        """
        ...

    def reasoning_of(self, message: Any) -> str:
        """Extract the reasoning text from a response message ("" if absent)."""
        ...


# ── built-in drivers ────────────────────────────────────────────────────

class PassthroughDriver:
    """Default: vendor-neutral keys, for any OpenAI-compatible backend.

    Byte-identical to the pre-driver behaviour: forwards `thinking` and the
    profile's `chat_template_kwargs` untouched.
    """

    name = "passthrough"

    def build_extra_body(self, *, spec: Any, cfg: Any,
                         thinking: bool | None) -> dict:
        extra: dict = {}
        ctk = getattr(cfg, "chat_template_kwargs", None) if cfg is not None else None
        if ctk:
            extra["chat_template_kwargs"] = dict(ctk)
        if thinking:
            extra["thinking"] = True          # neutral flag, vendor-neutral
        return extra

    def reasoning_of(self, message: Any) -> str:
        return getattr(message, "reasoning_content", "") or ""


class LlamaCppDriver:
    """llama.cpp: reasoning is toggled via `chat_template_kwargs.enable_thinking`.

    Verified on build b11312-0c1e57098 — top-level `thinking` / `enable_thinking`
    are silently ignored there, so the template variable is the only lever.
    """

    name = "llamacpp"
    _KEY = "chat_template_kwargs"
    _FLAG = "enable_thinking"

    def build_extra_body(self, *, spec: Any, cfg: Any,
                         thinking: bool | None) -> dict:
        extra: dict = {}
        tmpl = {}
        ctk = getattr(cfg, "chat_template_kwargs", None) if cfg is not None else None
        if ctk:
            tmpl.update(ctk)                  # merge, never clobber siblings
        if thinking is not None:
            tmpl[self._FLAG] = bool(thinking)  # explicit intent is rendered here
        if tmpl:
            extra[self._KEY] = tmpl
        if thinking:
            # keep the neutral flag too: harmless on llama.cpp and it preserves
            # behaviour if the server is upgraded past PR #22336
            extra["thinking"] = True
        return extra

    def reasoning_of(self, message: Any) -> str:
        return getattr(message, "reasoning_content", "") or ""


class DeepSeekDriver:
    """DeepSeek: reasoning is toggled by an OPTIONS STRUCT, not a boolean.

    MEASURED against api.deepseek.com (deepseek-flash):
        {"thinking": true}                       -> 422 "invalid type: boolean
                                                    `true`, expected struct
                                                    ThinkingOptions"
        {"thinking": {"type": "enabled"}}        -> 200, reasoning present
        {"thinking": {"type": "disabled"}}       -> 200, reasoning absent
        {"thinking": {"type": "auto"|"none"|""}} -> 422 "unknown variant"
        (field omitted)                          -> thinking ON (provider default)

    Only `enabled` / `disabled` are accepted, and omitting the field leaves the
    provider default in place — hence `thinking is None` sends nothing.
    """

    name = "deepseek"
    _KEY = "thinking"

    def build_extra_body(self, *, spec: Any, cfg: Any,
                         thinking: bool | None) -> dict:
        extra: dict = {}
        if thinking is not None:
            extra[self._KEY] = {"type": "enabled" if thinking else "disabled"}
        return extra

    def reasoning_of(self, message: Any) -> str:
        return getattr(message, "reasoning_content", "") or ""


# ── registry ────────────────────────────────────────────────────────────

_REGISTRY: dict[str, LLMDriver] = {}
# Which driver a backend gets when the profile does not name one.
_BACKEND_DEFAULTS = {"local": "llamacpp", "passthrough": "passthrough",
                     "cloud": "passthrough", "deepseek": "deepseek"}
DEFAULT_DRIVER = "passthrough"


def register_driver(name: str, driver: LLMDriver) -> None:
    """Register (or replace) a driver. Apps use this to add a backend (ADR-055).

    Validated eagerly so a malformed driver fails at registration, not mid-run.
    """
    if not name or not isinstance(name, str):
        raise ValueError("driver name must be a non-empty string")
    if not isinstance(driver, LLMDriver):
        raise TypeError(
            "driver %r does not satisfy the LLMDriver protocol "
            "(needs `name`, `build_extra_body(spec, cfg, thinking)`, "
            "`reasoning_of(message)`)" % (name,))
    if getattr(driver, "name", "") != name:
        raise ValueError(
            "driver registered as %r but declares name=%r; they must match so "
            "audit/log lines are unambiguous" % (name, getattr(driver, "name", "")))
    _REGISTRY[name] = driver


def get_driver(name: str) -> LLMDriver:
    """Look up a driver by name; unknown names raise (never silently default)."""
    try:
        return _REGISTRY[name]
    except KeyError:
        raise KeyError(
            "unknown LLM driver %r; registered: %s. Register it with "
            "client.llm.register_driver(name, driver) (ADR-055)."
            % (name, ", ".join(sorted(_REGISTRY)) or "(none)")) from None


def registered_drivers() -> list[str]:
    return sorted(_REGISTRY)


def default_driver_for(backend: str) -> LLMDriver:
    """The driver a backend gets when the profile names none.

    Order:
      1. a driver REGISTERED UNDER the backend's name — so an app that does
         `register_driver("my-vllm", ...)` gets it selected just by running
         against `backend: my-vllm`, with no framework or profile change;
      2. the built-in mapping (`local` -> llamacpp, `cloud` -> passthrough);
      3. `passthrough`, so an unrecognised backend keeps the pre-driver
         behaviour instead of breaking a run.
    """
    key = (backend or "").strip().lower()
    if key and key in _REGISTRY:
        return _REGISTRY[key]
    name = _BACKEND_DEFAULTS.get(key, DEFAULT_DRIVER)
    return _REGISTRY.get(name) or _REGISTRY[DEFAULT_DRIVER]


def driver_for(spec: Any, cfg: Any) -> LLMDriver:
    """Resolve the driver for one call.

    Order: explicit `LLMConfig.driver` (profile) -> backend default.
    A named-but-unregistered driver RAISES: silently using another renderer would
    reproduce exactly the invisible-misconfiguration bug this layer exists for.
    """
    named = (getattr(cfg, "driver", "") or "").strip() if cfg is not None else ""
    if named:
        return get_driver(named)              # loud on typos
    backend = getattr(cfg, "backend", "") if cfg is not None else ""
    return default_driver_for(backend)


register_driver(PassthroughDriver.name, PassthroughDriver())
register_driver(LlamaCppDriver.name, LlamaCppDriver())
register_driver(DeepSeekDriver.name, DeepSeekDriver())
