"""ADR-055: the LLM driver layer — vendor rendering out of the framework.

WHY THIS EXISTS
  The framework holds an *intent* ("enable thinking") while the *field name* that
  expresses it is backend-specific.  MEASURED on llama.cpp `b11312-0c1e57098`
  (temperature=0, identical prompt):

      (no switch)                                      reasoning=913
      top-level "enable_thinking": true                reasoning=863   ignored
      top-level "enable_thinking": false               reasoning=863   IGNORED
      top-level "thinking": true                       reasoning=863   ignored
      "chat_template_kwargs": {"enable_thinking": false}  reasoning=0  works

  So that build ONLY understands `chat_template_kwargs`; the top-level toggle is
  upstream PR #22336, newer than our build.  Passing the wrong shape is SILENT —
  the server just uses the template default.

  Putting `if backend == "local"` in the runtime (ADR-054's stopgap) violates
  ADR-041 §4 and turns the framework into a set of vendor branches.  The driver
  layer keeps the SEMANTICS in the framework (ADR-054 P/Q precedence) and pushes
  only the FIELD RENDERING to a replaceable, app-registerable driver.
"""
from __future__ import annotations

import pytest

from harness.llm_driver import (
    LLMDriver,
    LlamaCppDriver,
    PassthroughDriver,
    default_driver_for,
    get_driver,
    register_driver,
    registered_drivers,
)


class _Spec:
    """Minimal spec stand-in carrying only what a driver may read."""
    thinking = None
    backend = "local"

    def __init__(self, thinking=None):
        self.thinking = thinking


class _Cfg:
    def __init__(self, backend="local", **kw):
        self.backend = backend
        for k, v in kw.items():
            setattr(self, k, v)


class _Msg:
    def __init__(self, reasoning_content=None, content="x"):
        if reasoning_content is not None:
            self.reasoning_content = reasoning_content
        self.content = content


# ── contract ────────────────────────────────────────────────────────────

def test_builtin_drivers_satisfy_the_protocol():
    assert isinstance(PassthroughDriver(), LLMDriver)
    assert isinstance(LlamaCppDriver(), LLMDriver)


def test_every_driver_declares_a_name():
    for d in (PassthroughDriver(), LlamaCppDriver()):
        assert d.name and isinstance(d.name, str)


def test_reasoning_of_tolerates_missing_attribute():
    """A driver must not explode when the backend omits reasoning entirely."""
    for d in (PassthroughDriver(), LlamaCppDriver()):
        assert d.reasoning_of(_Msg()) == ""
        assert d.reasoning_of(None) == ""
        assert d.reasoning_of(_Msg(reasoning_content="why")) == "why"


# ── passthrough: the cloud path must stay byte-identical ────────────────

def test_passthrough_sends_the_neutral_flag_only():
    """Default driver: vendor-neutral key, no invented renderer keys."""
    extra = PassthroughDriver().build_extra_body(
        spec=_Spec(thinking=True), cfg=_Cfg(backend="cloud"), thinking=True)
    assert extra.get("thinking") is True
    assert "chat_template_kwargs" not in extra or \
        extra["chat_template_kwargs"] == {}


def test_passthrough_with_no_opinion_sends_nothing():
    extra = PassthroughDriver().build_extra_body(
        spec=_Spec(thinking=None), cfg=_Cfg(backend="cloud"), thinking=None)
    assert "thinking" not in extra


def test_passthrough_preserves_profile_chat_template_kwargs():
    """The profile's escape hatch is passed through untouched (ADR-054 P path)."""
    extra = PassthroughDriver().build_extra_body(
        spec=_Spec(thinking=None),
        cfg=_Cfg(backend="cloud", chat_template_kwargs={"enable_thinking": False}),
        thinking=None)
    assert extra["chat_template_kwargs"] == {"enable_thinking": False}


# ── llamacpp: the measured renderer ─────────────────────────────────────

def test_llamacpp_renders_thinking_into_chat_template_kwargs():
    """THE core requirement: explicit thinking lands where llama.cpp reads it."""
    d = LlamaCppDriver()
    assert d.build_extra_body(spec=_Spec(thinking=True),
                              cfg=_Cfg(), thinking=True)[
        "chat_template_kwargs"]["enable_thinking"] is True
    assert d.build_extra_body(spec=_Spec(thinking=False),
                              cfg=_Cfg(), thinking=False)[
        "chat_template_kwargs"]["enable_thinking"] is False


def test_llamacpp_no_opinion_defers_to_profile():
    """thinking=None ⇒ the profile's value survives (P path)."""
    d = LlamaCppDriver()
    extra = d.build_extra_body(
        spec=_Spec(thinking=None),
        cfg=_Cfg(chat_template_kwargs={"enable_thinking": False}),
        thinking=None)
    assert extra["chat_template_kwargs"]["enable_thinking"] is False


def test_llamacpp_explicit_overrides_profile_value():
    """thinking=True with a profile saying False ⇒ True (Q path)."""
    d = LlamaCppDriver()
    extra = d.build_extra_body(
        spec=_Spec(thinking=True),
        cfg=_Cfg(chat_template_kwargs={"enable_thinking": False}),
        thinking=True)
    assert extra["chat_template_kwargs"]["enable_thinking"] is True


def test_llamacpp_keeps_other_chat_template_kwargs():
    """Rendering must merge, not clobber, unrelated template kwargs."""
    d = LlamaCppDriver()
    extra = d.build_extra_body(
        spec=_Spec(thinking=True),
        cfg=_Cfg(chat_template_kwargs={"some_other_flag": 1}),
        thinking=True)
    assert extra["chat_template_kwargs"]["some_other_flag"] == 1
    assert extra["chat_template_kwargs"]["enable_thinking"] is True


# ── registry ────────────────────────────────────────────────────────────

def test_registry_lookup_and_default_by_backend():
    assert registered_drivers(), "no drivers registered"
    assert isinstance(get_driver("passthrough"), PassthroughDriver)
    assert isinstance(get_driver("llamacpp"), LlamaCppDriver)
    # unknown name must not silently become a working driver
    with pytest.raises(Exception):
        get_driver("does-not-exist")


def test_default_driver_follows_backend():
    assert default_driver_for("local").name == "llamacpp"
    assert default_driver_for("cloud").name == "passthrough"
    # an unknown/empty backend must fall back safely, not raise
    assert default_driver_for("").name == "passthrough"


def test_app_can_register_its_own_driver():
    """The whole point: an app adds a backend WITHOUT touching the framework."""

    class _MyVllmDriver:
        name = "test-vllm"

        def build_extra_body(self, *, spec, cfg, thinking):
            return {"vllm_thinking": bool(thinking)} if thinking is not None else {}

        def reasoning_of(self, message):
            return getattr(message, "reasoning", "") or ""

    d = _MyVllmDriver()
    register_driver("test-vllm", d)
    assert get_driver("test-vllm") is d
    assert default_driver_for("test-vllm").name == "test-vllm"
    extra = d.build_extra_body(spec=_Spec(thinking=True), cfg=_Cfg(),
                               thinking=True)
    assert extra == {"vllm_thinking": True}


def test_registering_a_bad_object_is_rejected():
    class _NotADriver:
        pass

    with pytest.raises(Exception):
        register_driver("broken", _NotADriver())


# ── SDK exposure: the APP-facing surface (ADR-055 §3) ───────────────────

def test_sdk_exposes_the_driver_registry():
    """`client.llm` is how an app adds a backend without editing the framework."""
    from harness.interfaces import SandboxConfig
    from harness.sdk import HarnessClient

    client = HarnessClient(sandbox_configs={"main": SandboxConfig(
        backend="bubblewrap", firewall={"network": "host"}, shared_folders=[])})
    assert client.llm is not None
    names = client.llm.list_drivers()
    assert "passthrough" in names and "llamacpp" in names

    class _AppDriver:
        name = "sdk-app-driver"

        def build_extra_body(self, *, spec, cfg, thinking):
            return {"app_flag": bool(thinking)} if thinking is not None else {}

        def reasoning_of(self, message):
            return ""

    client.llm.register_driver("sdk-app-driver", _AppDriver())
    assert "sdk-app-driver" in client.llm.list_drivers()


def test_sdk_resolve_reports_the_effective_driver():
    """Diagnostics: an operator can ask which renderer a profile would use."""
    from harness.interfaces import SandboxConfig
    from harness.sdk import HarnessClient

    client = HarnessClient(sandbox_configs={"main": SandboxConfig(
        backend="bubblewrap", firewall={"network": "host"}, shared_folders=[])})

    class _S:
        thinking = None

    assert client.llm.resolve(_S(), _Cfg(backend="local")) == "llamacpp"
    assert client.llm.resolve(_S(), _Cfg(backend="cloud")) == "passthrough"
    assert client.llm.resolve(_S(), _Cfg(backend="local", driver="passthrough")) \
        == "passthrough"


def test_unregistered_named_driver_raises_not_falls_back():
    """A typo'd driver name must not silently run a different renderer."""
    from harness.llm_driver import driver_for
    with pytest.raises(KeyError):
        driver_for(_Spec(), _Cfg(backend="local", driver="typo-name"))


# ── deepseek: a SECOND real backend with a DIFFERENT shape ──────────────
#
# MEASURED against api.deepseek.com (model deepseek-flash):
#     {"thinking": true}                        -> 422, "invalid type: boolean,
#                                                  expected struct ThinkingOptions"
#     {"thinking": {"type": "enabled"}}         -> 200, reasoning 96 chars
#     {"thinking": {"type": "disabled"}}        -> 200, reasoning  0 chars
#     {"thinking": {"type": "auto"|"none"|""}}  -> 422, "unknown variant"
#     (field omitted)                           -> thinking ON by default
#
# This is exactly why ADR-055 exists: the SAME intent ("turn thinking on") needs
# `chat_template_kwargs.enable_thinking` on llama.cpp and `thinking.{type}` on
# DeepSeek.  The framework must not know either; the driver must.

def test_deepseek_driver_renders_thinking_as_an_object():
    from harness.llm_driver import DeepSeekDriver
    d = DeepSeekDriver()
    assert d.build_extra_body(spec=_Spec(thinking=True), cfg=_Cfg(backend="cloud"),
                              thinking=True)["thinking"] == {"type": "enabled"}
    assert d.build_extra_body(spec=_Spec(thinking=False), cfg=_Cfg(backend="cloud"),
                              thinking=False)["thinking"] == {"type": "disabled"}


def test_deepseek_driver_no_opinion_sends_no_thinking_field():
    """Omitted ⇒ the provider default applies; do not invent a value."""
    from harness.llm_driver import DeepSeekDriver
    extra = DeepSeekDriver().build_extra_body(spec=_Spec(thinking=None),
                                             cfg=_Cfg(backend="cloud"), thinking=None)
    assert "thinking" not in extra


def test_deepseek_driver_never_sends_a_boolean():
    """The regression that produced a 422: a boolean where a struct is required."""
    from harness.llm_driver import DeepSeekDriver
    for th in (True, False, None):
        extra = DeepSeekDriver().build_extra_body(spec=_Spec(), cfg=_Cfg(backend="cloud"),
                                                 thinking=th)
        if "thinking" in extra:
            assert not isinstance(extra["thinking"], bool), extra


def test_deepseek_driver_reads_reasoning_content():
    from harness.llm_driver import DeepSeekDriver
    assert DeepSeekDriver().reasoning_of(_Msg(reasoning_content="why")) == "why"
    assert DeepSeekDriver().reasoning_of(_Msg()) == ""


def test_deepseek_driver_is_registered_and_selectable():
    from harness.llm_driver import DeepSeekDriver, get_driver
    assert isinstance(get_driver("deepseek"), DeepSeekDriver)
    # selectable by backend name too (an app that sets backend: deepseek)
    from harness.llm_driver import default_driver_for
    assert default_driver_for("deepseek").name == "deepseek"
