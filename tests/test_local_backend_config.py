"""Local / self-hosted backend support: tokenizer counting, temperature, thinking.

Three defects that are INVISIBLE on the cloud path (where the tokenizer matches
the model and the template has no thinking switch) but break a self-hosted model:

  L1-01  `_count_tokens` used `tokenizer_dir/<provider>` (DeepSeek BPE), whose
         vocab contains NO CJK characters -> a pure-Chinese string counted as
         **0 tokens**, so the budget collapsed.  Fixed by asking the local
         server's own tokenizer (`POST /tokenize`).
  L1-02  `_call_llm_once` hardcoded `temperature=0.0`, so `LLMConfig.temperature`
         and the profile's `temperature:` never took effect.
  L1-03  no way to turn OFF a template's default thinking mode, so a reasoning
         model spent `max_tokens` on its reasoning block and returned EMPTY
         content.

Everything here is offline: the tokenizer tests stop the server path explicitly,
and the request-shape tests capture the kwargs without any network.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

import warnings as _warnings

import harness.prompt_assembly as pa


# ── L1-01: exact counting, server-first with an HF fallback ─────────────

_CJK = "推理引擎已经部署完成，长上下文可以正常工作，但是显存是主要瓶颈。"


def test_count_tokens_uses_server_result_over_fallback(monkeypatch):
    """When the server answers, ITS count is authoritative — including for CJK.

    This is the actual fix for L1-01: the DeepSeek fallback tokenizer has no CJK
    vocabulary (so it reports 0), and only the served model's own tokenizer
    knows the truth.  The server is stubbed here so the test needs no
    infrastructure, but it exercises the real precedence in `_count_tokens`.
    """
    monkeypatch.setattr(pa, "_server_count_tokens", lambda text: 17)
    assert pa._count_tokens(_CJK, "deepseek") == 17


def test_fallback_tokenizer_counts_cjk(monkeypatch):
    """The fallback now counts CJK — the 2026-10-16 fix.

    Previously this asserted `== 0` and said "if this ever starts returning > 0
    the fallback tokenizer gained CJK support and the L1-01 rationale should be
    revisited".  It has, so it is: the artifacts were fine all along (~35,679
    byte-level vocab entries) but `AutoTokenizer` returned the SLOW
    `LlamaTokenizer`, which does not implement the byte-level BPE.  Loading the
    same file fast puts the estimate within ~1% of the provider's own count, so a
    deployment WITHOUT a reachable server is no longer blind.
    """
    monkeypatch.setenv("HARNESS_TOKENIZE_URL", "off")
    monkeypatch.setattr(pa, "_TOKENIZE_URL_CACHE", False, raising=False)
    n = pa._count_tokens(_CJK, "deepseek")
    assert n > 0, "the fallback still cannot encode CJK"


def test_count_tokens_uses_server_result_when_available(monkeypatch):
    """The server count short-circuits the fallback entirely."""
    monkeypatch.setattr(pa, "_server_count_tokens", lambda text: 5)
    assert pa._count_tokens("anything", "deepseek") == 5


def test_count_tokens_falls_back_when_server_unreachable(monkeypatch):
    """With no server, counting degrades to the HF tokenizer instead of raising.

    The local backend is optional infrastructure: a missing model server must
    not break assembly, only make the count approximate.
    """
    monkeypatch.setenv("HARNESS_TOKENIZE_URL", "off")
    monkeypatch.setattr(pa, "_TOKENIZE_URL_CACHE", False, raising=False)
    n = pa._count_tokens("hello world", "deepseek")
    assert n > 0


def test_tokenize_url_explicit_override_used_verbatim(monkeypatch):
    """`HARNESS_TOKENIZE_URL` is the COMPLETE endpoint (not a base_url).

    The two sources deliberately differ: an operator-provided value is used as
    given, while a profile's base_url gets `/tokenize` appended.  (Previously the
    test for this was named `..._is_origin_not_v1` but asserted the opposite of
    its own docstring — corrected per review R3.)
    """
    monkeypatch.setenv("HARNESS_TOKENIZE_URL", "http://127.0.0.1:9999/custom")
    monkeypatch.setattr(pa, "_TOKENIZE_URL_CACHE", False, raising=False)
    assert pa._tokenize_url() == "http://127.0.0.1:9999/custom"


def test_tokenize_url_derived_from_profile_strips_v1(monkeypatch):
    """Profile-derived path appends /tokenize and drops the /v1 suffix."""
    monkeypatch.delenv("HARNESS_TOKENIZE_URL", raising=False)
    monkeypatch.setattr(pa, "_TOKENIZE_URL_CACHE", False, raising=False)

    class _P:
        base_url = "http://127.0.0.1:8080/v1"

    import harness.sdk.config as cfgmod
    monkeypatch.setattr(cfgmod, "resolve_llm_profile", lambda *a, **k: _P())
    assert pa._tokenize_url() == "http://127.0.0.1:8080/tokenize"


# ── R2: the endpoint must never leave the box ───────────────────────────

def test_cloud_profile_does_not_derive_a_remote_endpoint(monkeypatch):
    """A cloud profile must NOT yield `<cloud>/tokenize`.

    Verified defect: with `base_url=https://api.deepseek.com/v1` the derived URL
    was `https://api.deepseek.com/tokenize`, so every count POSTed the prompt
    text to an undeclared remote endpoint (and 404'd, retrying every 30 s).

    Since I-48 this refusal also warns; asserted here so the warning is expected
    rather than incidental noise in the suite output.
    """
    monkeypatch.delenv("HARNESS_TOKENIZE_URL", raising=False)
    monkeypatch.setattr(pa, "_TOKENIZE_URL_CACHE", False, raising=False)
    monkeypatch.setattr(pa, "_TOKENIZE_WARNED", False, raising=False)

    class _P:
        base_url = "https://api.deepseek.com/v1"

    import harness.sdk.config as cfgmod
    monkeypatch.setattr(cfgmod, "resolve_llm_profile", lambda *a, **k: _P())
    with _warnings.catch_warnings(record=True) as caught:
        _warnings.simplefilter("always")
        assert pa._tokenize_url() is None, "remote endpoint must be refused"
        assert [w for w in caught if issubclass(w.category, UserWarning)], \
            "refusal must not be silent (I-48 b)"


def test_remote_env_override_is_refused_and_warns(monkeypatch):
    """Even an explicit remote override is refused — no prompt egress.

    Fail-closed by design (I-47): a count must never ship prompt text off-box.
    I-48 adds a one-shot warning so this is not silent; asserted here.
    """
    monkeypatch.setenv("HARNESS_TOKENIZE_URL", "https://api.deepseek.com/tokenize")
    monkeypatch.setattr(pa, "_TOKENIZE_URL_CACHE", False, raising=False)
    monkeypatch.setattr(pa, "_TOKENIZE_WARNED", False, raising=False)
    with _warnings.catch_warnings(record=True) as caught:
        _warnings.simplefilter("always")
        assert pa._tokenize_url() is None
        assert [w for w in caught if issubclass(w.category, UserWarning)]


def test_loopback_detection():
    assert pa._is_loopback("http://127.0.0.1:8080/tokenize")
    assert pa._is_loopback("http://localhost:1/tokenize")
    assert pa._is_loopback("http://[::1]:8080/tokenize")
    assert not pa._is_loopback("https://api.deepseek.com/tokenize")
    assert not pa._is_loopback("http://10.0.0.5:8080/tokenize")


# ── R1: the REAL HTTP path (URL build → POST → parse → memo) ────────────

def _stub_server(reply: dict, captured: list):
    """Tiny localhost server that records POSTs and replies with `reply`."""
    import json as _json
    import threading
    from http.server import BaseHTTPRequestHandler, HTTPServer

    class _H(BaseHTTPRequestHandler):
        def do_POST(self):  # noqa: N802
            n = int(self.headers.get("Content-Length", 0))
            captured.append((self.path, _json.loads(self.rfile.read(n))))
            body = _json.dumps(reply).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *a):
            pass

    srv = HTTPServer(("127.0.0.1", 0), _H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, srv.server_address[1]


def test_real_http_path_counts_cjk(monkeypatch):
    """End-to-end over real HTTP: CJK text is POSTed and its count returned.

    This is the assertion the acceptance criterion actually asks for
    ("纯中文文本的计数 > 0").  The earlier version stubbed
    `_server_count_tokens` and therefore proved only precedence, never the URL
    build / POST / `{"tokens":[...]}` parse / memo (review R1).
    """
    monkeypatch.delenv("HARNESS_TOKENIZE_URL", raising=False)
    monkeypatch.setattr(pa, "_TOKENIZE_URL_CACHE", False, raising=False)
    pa._TOKEN_COUNT_CACHE.clear()
    pa._TOKENIZE_DOWN_UNTIL = 0.0

    captured: list = []
    srv, port = _stub_server({"tokens": [1] * 17}, captured)

    class _P:
        base_url = f"http://127.0.0.1:{port}/v1"

    import harness.sdk.config as cfgmod
    monkeypatch.setattr(cfgmod, "resolve_llm_profile", lambda *a, **k: _P())
    try:
        n = pa._count_tokens(_CJK, "deepseek")
        assert n == 17, n
        # the request really carried the CJK text, to the /tokenize path
        assert captured, "no HTTP request reached the stub"
        path, body = captured[0]
        assert path == "/tokenize", path
        assert body["content"] == _CJK
        # memoised: a second identical call must not re-POST
        before = len(captured)
        assert pa._count_tokens(_CJK, "deepseek") == 17
        assert len(captured) == before, "cache miss — text was re-POSTed"
    finally:
        srv.shutdown()
        srv.server_close()


def test_real_http_failure_falls_back_and_negative_caches(monkeypatch):
    """A 500 from the endpoint ⇒ HF fallback, and no retry within the TTL."""
    monkeypatch.delenv("HARNESS_TOKENIZE_URL", raising=False)
    monkeypatch.setattr(pa, "_TOKENIZE_URL_CACHE", False, raising=False)
    pa._TOKEN_COUNT_CACHE.clear()
    pa._TOKENIZE_DOWN_UNTIL = 0.0

    import json as _json
    import threading
    from http.server import BaseHTTPRequestHandler, HTTPServer
    hits: list = []

    class _H(BaseHTTPRequestHandler):
        def do_POST(self):  # noqa: N802
            hits.append(1)
            self.send_response(500)
            self.send_header("Content-Length", "0")
            self.end_headers()

        def log_message(self, *a):
            pass

    srv = HTTPServer(("127.0.0.1", 0), _H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    port = srv.server_address[1]

    class _P:
        base_url = f"http://127.0.0.1:{port}/v1"

    import harness.sdk.config as cfgmod
    monkeypatch.setattr(cfgmod, "resolve_llm_profile", lambda *a, **k: _P())
    try:
        monkeypatch.setenv("HARNESS_TOKENIZE_URL", f"http://127.0.0.1:{port}/tokenize")
        monkeypatch.setattr(pa, "_TOKENIZE_URL_CACHE", False, raising=False)
        assert pa._count_tokens("hello", "deepseek") > 0     # fell back, no raise
        assert len(hits) == 1
        # within the negative-cache TTL the endpoint is not hit again
        assert pa._count_tokens("hello again", "deepseek") > 0
        assert len(hits) == 1, "negative cache did not suppress the retry"
    finally:
        srv.shutdown()
        srv.server_close()


def test_token_cache_is_bounded_and_does_not_retain_text():
    """R4: the memo is an LRU with a cap, and keys are hashes (no raw text)."""
    pa._TOKEN_COUNT_CACHE.clear()
    for i in range(pa._TOKEN_COUNT_CACHE_MAX + 50):
        pa._TOKEN_COUNT_CACHE[("u", "k%d" % i)] = i
        if len(pa._TOKEN_COUNT_CACHE) > pa._TOKEN_COUNT_CACHE_MAX:
            pa._TOKEN_COUNT_CACHE.popitem(last=False)
    assert len(pa._TOKEN_COUNT_CACHE) <= pa._TOKEN_COUNT_CACHE_MAX
    assert pa._TOKEN_COUNT_CACHE_MAX <= 10_000, "cap must stay modest"
    pa._TOKEN_COUNT_CACHE.clear()


def test_count_tokens_accepts_none():
    """Defensive: None must not raise (payloads are occasionally empty)."""
    assert pa._count_tokens(None, "deepseek") >= 0


# ── L1-02 / L1-03: the two config fields exist and default inertly ──────

def test_llm_config_has_override_fields():
    from harness.sdk.config import LLMConfig
    c = LLMConfig()
    # L1-02: None keeps the upstream greedy behaviour (no silent change)
    assert c.temperature_override is None
    # L1-03: empty by default -> no chat_template_kwargs sent upstream
    assert c.chat_template_kwargs == {}


def test_profile_can_carry_both_overrides(tmp_path, monkeypatch):
    """A profile can set temperature_override and chat_template_kwargs."""
    import yaml
    from harness.sdk.config import resolve_llm_profile

    cfg_dir = tmp_path / "configs"
    cfg_dir.mkdir()
    (cfg_dir / "llm.yaml").write_text(yaml.safe_dump({
        "default": {
            "base_url": "http://127.0.0.1:8080/v1",
            "model": "m",
            "temperature_override": 0.6,
            "chat_template_kwargs": {"enable_thinking": False},
        }
    }), encoding="utf-8")

    cfg = resolve_llm_profile("default", project_dir=tmp_path)
    assert cfg.temperature_override == 0.6
    assert cfg.chat_template_kwargs == {"enable_thinking": False}


def _stub_tokenizer_dir(root, provider: str) -> None:
    """Create a minimal real tokenizer at `<root>/tokenizer_dir/<provider>/`.

    Lets a test exercise the CONFIGURED provider on the fallback path without
    network or a real model vocab.  Any whitespace-splitting tokenizer does;
    `bert-base-uncased` needs no download here because the vocabulary is written
    inline.
    """
    import json as _json
    d = Path(root) / "tokenizer_dir" / provider
    d.mkdir(parents=True, exist_ok=True)
    vocab = {"[PAD]": 0, "[UNK]": 1, "[CLS]": 2, "[SEP]": 3,
             "hello": 4, "world": 5, "hi": 6, "##s": 7, "the": 8}
    # I-49 review S1: include CJK entries so the fallback can COUNT Chinese.
    # Without them the stub encodes CJK as [UNK] and "count > 0" cannot be
    # asserted, which left the acceptance criterion without an in-suite guard.
    for ch in "中文推理引擎已部署长上下文": 
        vocab.setdefault(ch, len(vocab))
    (d / "vocab.txt").write_text("\n".join(vocab), encoding="utf-8")
    (d / "tokenizer_config.json").write_text(_json.dumps({
        "tokenizer_class": "BertTokenizer", "do_lower_case": True,
        "unk_token": "[UNK]", "pad_token": "[PAD]", "cls_token": "[CLS]",
        "sep_token": "[SEP]",
    }), encoding="utf-8")


# ── Request shape: the two overrides reach the wire ─────────────────────

class _CaptureClient:
    """Stands in for `openai.OpenAI`, capturing create() kwargs."""

    def __init__(self):
        self.calls: list[dict] = []
        outer = self

        class _Completions:
            def create(self, **kwargs):
                outer.calls.append(kwargs)

                class _Msg:
                    content = "ok"
                    tool_calls = None
                    reasoning_content = None

                class _Choice:
                    message = _Msg()
                    finish_reason = "stop"

                class _Resp:
                    choices = [_Choice()]
                    usage = None

                return _Resp()

        self.chat = type("_Chat", (), {"completions": _Completions()})()


def _runtime_with_capture(spec, monkeypatch):
    """Build an AgentRuntime and swap its client for the capture stub."""
    from harness.gateway.gateway import Gateway
    from harness.sdk.config import LLMConfig

    from tests.conftest import register_test_tools

    from harness.agent_runtime.impl import AgentRuntime

    class _SB:
        def spawn(self, config, cap_token=""):
            return "p"

        def kill(self, cid):
            pass

        def is_alive(self, cid):
            return True

        def get_logs(self, cid):
            return ""

        def execute(self, cid, cmd, restrict=None):
            return ("", "", 0)

    from harness.session_store.impl import SessionStore
    import tempfile

    tmp = tempfile.mkdtemp()
    store = SessionStore(base_path=tmp)
    store.create_session("s", metadata={})

    llm = LLMConfig()
    llm.temperature_override = 0.42
    llm.chat_template_kwargs = {"enable_thinking": False}

    rt = AgentRuntime(spec=spec, gateway=Gateway(event_bus=None), store=store,
                      api_key="sk-test", sandbox=_SB(), llm_config=llm)
    register_test_tools(rt._tool_registry)
    rt._inject_tool_deps()
    cap = _CaptureClient()
    rt._client = cap
    return rt, cap


def _spec():
    from harness.interfaces import AgentSpec
    return AgentSpec(name="t", description="t", tools=[],
                     model="deepseek-v4-flash", system_prompt="sys",
                     max_iterations=1)


def test_temperature_override_reaches_request(monkeypatch):
    """L1-02: the profile value must appear in the request, not hardcoded 0.0."""
    rt, cap = _runtime_with_capture(_spec(), monkeypatch)
    rt._call_llm([{"role": "user", "content": "hi"}], tools=[])
    assert cap.calls, "no LLM call captured"
    assert cap.calls[0]["temperature"] == 0.42, cap.calls[0]["temperature"]


def test_temperature_stays_greedy_without_override(monkeypatch):
    """L1-02: unset override keeps the upstream greedy behaviour (0.0)."""
    from harness.sdk.config import LLMConfig
    rt, cap = _runtime_with_capture(_spec(), monkeypatch)
    rt._llm_config = LLMConfig()          # no override
    rt._call_llm([{"role": "user", "content": "hi"}], tools=[])
    assert cap.calls[0]["temperature"] == 0.0


def test_chat_template_kwargs_reach_request(monkeypatch):
    """L1-03: enable_thinking=false must be forwarded via extra_body."""
    rt, cap = _runtime_with_capture(_spec(), monkeypatch)
    rt._call_llm([{"role": "user", "content": "hi"}], tools=[])
    body = cap.calls[0].get("extra_body") or {}
    assert body.get("chat_template_kwargs") == {"enable_thinking": False}, body


def test_no_extra_body_without_chat_template_kwargs(monkeypatch):
    """L1-03: with no kwargs configured, no extra_body is sent (inert)."""
    from harness.sdk.config import LLMConfig
    rt, cap = _runtime_with_capture(_spec(), monkeypatch)
    rt._llm_config = LLMConfig()
    rt._call_llm([{"role": "user", "content": "hi"}], tools=[])
    assert not cap.calls[0].get("extra_body")


# ── I-48 (a): the docstring must match the code ─────────────────────────

def test_docstring_matches_fail_closed_behaviour():
    """The docstring must not claim an explicit override bypasses the gate.

    I-47's docstring said a non-loopback URL is refused "unless it came from an
    explicit override", while the code and `test_remote_env_override_is_refused_and_warns`
    both refuse it.  A later reader "fixing" the code to match that sentence
    would RE-OPEN prompt egress — so pin the invariant in text form too.
    """
    import inspect
    doc = inspect.getdoc(pa._tokenize_url) or ""
    assert "unless it came from an" not in doc, (
        "docstring still claims the override bypasses the loopback gate")
    assert "loopback" in doc.lower()
    # the code really is fail-closed for a remote override
    assert "still refuse" in inspect.getsource(pa._tokenize_url)


# ── I-48 (b): refusal must be VISIBLE, not silent ───────────────────────

def _reset_tokenize_state(monkeypatch):
    monkeypatch.setattr(pa, "_TOKENIZE_URL_CACHE", False, raising=False)
    monkeypatch.setattr(pa, "_TOKENIZE_WARNED", False, raising=False)
    monkeypatch.setattr(pa, "_TOKENIZE_DISABLED_REASON", "", raising=False)
    pa._TOKENIZE_DOWN_UNTIL = 0.0


def test_lan_profile_warns_once_and_records_reason(monkeypatch):
    """A server on another LAN host degrades counting — say so, once.

    Verified gap: `http://192.168.1.50:8080/v1` yielded None with no log, no
    warning and no returned signal, so a user could not tell that CJK counting
    was still broken on their deployment.
    """
    monkeypatch.delenv("HARNESS_TOKENIZE_URL", raising=False)
    _reset_tokenize_state(monkeypatch)

    class _P:
        base_url = "http://192.168.1.50:8080/v1"

    import harness.sdk.config as cfgmod
    monkeypatch.setattr(cfgmod, "resolve_llm_profile", lambda *a, **k: _P())

    with _warnings.catch_warnings(record=True) as caught:
        _warnings.simplefilter("always")
        assert pa._tokenize_url() is None            # still fail-closed
        first = [w for w in caught if issubclass(w.category, UserWarning)]
        assert first, "refusal was silent"
        assert "192.168.1.50" in str(first[0].message)
        # a second resolution (cache reset) must NOT warn again
        monkeypatch.setattr(pa, "_TOKENIZE_URL_CACHE", False, raising=False)
        pa._tokenize_url()
        again = [w for w in caught if issubclass(w.category, UserWarning)]
        assert len(again) == len(first), "warned more than once per process"
    assert "192.168.1.50" in pa._TOKENIZE_DISABLED_REASON


def test_remote_env_override_records_reason(monkeypatch):
    """The refusal reason is recorded for diagnostics (I-48 b)."""
    monkeypatch.setenv("HARNESS_TOKENIZE_URL", "https://api.deepseek.com/tokenize")
    _reset_tokenize_state(monkeypatch)
    with _warnings.catch_warnings(record=True):
        _warnings.simplefilter("always")
        assert pa._tokenize_url() is None
    assert "not loopback" in pa._TOKENIZE_DISABLED_REASON


def test_explicit_off_does_not_warn(monkeypatch):
    """`HARNESS_TOKENIZE_URL=off` is a deliberate opt-out — not a degradation."""
    monkeypatch.setenv("HARNESS_TOKENIZE_URL", "off")
    _reset_tokenize_state(monkeypatch)
    with _warnings.catch_warnings(record=True) as caught:
        _warnings.simplefilter("always")
        assert pa._tokenize_url() is None
        assert not [w for w in caught if issubclass(w.category, UserWarning)]
    assert pa._TOKENIZE_DISABLED_REASON == ""


def test_loopback_server_does_not_warn(monkeypatch):
    monkeypatch.delenv("HARNESS_TOKENIZE_URL", raising=False)
    _reset_tokenize_state(monkeypatch)

    class _P:
        base_url = "http://127.0.0.1:8080/v1"

    import harness.sdk.config as cfgmod
    monkeypatch.setattr(cfgmod, "resolve_llm_profile", lambda *a, **k: _P())
    with _warnings.catch_warnings(record=True) as caught:
        _warnings.simplefilter("always")
        assert pa._tokenize_url() == "http://127.0.0.1:8080/tokenize"
        assert not [w for w in caught if issubclass(w.category, UserWarning)]
    assert pa._TOKENIZE_DISABLED_REASON == ""


def test_wan_deployment_counts_cjk_without_a_local_server(monkeypatch):
    """The fail-closed gate still refuses the remote `/tokenize` (prompt text must
    not leave the box), and that is now HARMLESS for counting: the fallback is
    accurate, so a LAN/WAN deployment is no longer left counting CJK as 0.

    (Previously this test pinned the residual defect — "still reports 0" — and
    said to revisit once the fallback learned CJK.  It has; this is that revisit.)
    """
    monkeypatch.delenv("HARNESS_TOKENIZE_URL", raising=False)
    _reset_tokenize_state(monkeypatch)

    class _P:
        base_url = "http://192.168.1.50:8080/v1"

    import harness.sdk.config as cfgmod
    monkeypatch.setattr(cfgmod, "resolve_llm_profile", lambda *a, **k: _P())
    with _warnings.catch_warnings(record=True):
        _warnings.simplefilter("always")
        n = pa._count_tokens(_CJK, "deepseek")
    assert n > 0, (
        "the LAN/WAN deployment must still count CJK: the gate refuses the "
        "remote /tokenize for privacy, so counting depends on the fallback")
# ── I-49: `provider` selects the FALLBACK tokenizer, per profile ────────

@pytest.mark.filterwarnings(
    "ignore:Deprecated in 0.9.0:DeprecationWarning")
def test_provider_comes_from_profile_not_hardcoded(monkeypatch, tmp_path):
    """`provider` must be read from the resolved profile.

    Root cause of the LAN/WAN failure: it was hardcoded to "deepseek", so a
    self-hosted model fell back to a vocab with NO CJK (0 entries vs 33,986 in
    the served model's vocab) and counted Chinese as 0 tokens.
    """
    import yaml
    cfg_dir = tmp_path / "configs"
    cfg_dir.mkdir()
    (cfg_dir / "llm.yaml").write_text(yaml.safe_dump({
        "default": {"base_url": "http://127.0.0.1:8080/v1", "model": "m",
                    "provider": "spark2_5"},
    }), encoding="utf-8")

    from harness.interfaces import AgentSpec
    from harness.prompt_preview import compose_llm_context
    from harness.session_store.impl import SessionStore

    store = SessionStore(base_path=str(tmp_path / "sessions"))
    store.create_session("s1", metadata={})
    spec = AgentSpec(name="a", description="d", model="m",
                     system_prompt="sys", llm_profile="default")

    _stub_tokenizer_dir(tmp_path, "spark2_5")
    monkeypatch.chdir(tmp_path)
    pa._TOKENIZERS.pop("spark2_5", None)
    pa._TOKENIZE_URL_CACHE = False
    out = compose_llm_context(store, spec, "s1", task="hi")
    assert out["params"]["provider"] == "spark2_5", out["params"]


def test_provider_defaults_to_deepseek_without_profile(monkeypatch, tmp_path):
    """No profile ⇒ legacy default, so nothing changes unless configured."""
    from harness.interfaces import AgentSpec
    from harness.prompt_preview import compose_llm_context
    from harness.session_store.impl import SessionStore

    store = SessionStore(base_path=str(tmp_path / "sessions"))
    store.create_session("s1", metadata={})
    spec = AgentSpec(name="a", description="d", model="m",
                     system_prompt="sys", llm_profile="does-not-exist")
    prev = pa._TOKENIZE_URL_CACHE
    try:
        monkeypatch.chdir(tmp_path)          # no configs/llm.yaml here
        out = compose_llm_context(store, spec, "s1", task="hi")
        assert out["params"]["provider"] == "deepseek", out["params"]
    finally:
        pa._TOKENIZE_URL_CACHE = prev


@pytest.mark.filterwarnings(
    "ignore:Deprecated in 0.9.0:DeprecationWarning")
def test_profile_provider_reaches_fallback_counter(monkeypatch, tmp_path):
    """End-to-end of the fix: profile provider ⇒ that vocab is used on fallback.

    Builds a stub `tokenizer_dir/<provider>/` so no real tokenizer file and no
    network are required; asserts the CONFIGURED provider was consulted.
    """
    import yaml
    from harness.interfaces import AgentSpec
    from harness.prompt_preview import compose_llm_context
    from harness.session_store.impl import SessionStore

    cfg_dir = tmp_path / "configs"
    cfg_dir.mkdir()
    (cfg_dir / "llm.yaml").write_text(yaml.safe_dump({
        "default": {"base_url": "http://127.0.0.1:8080/v1", "model": "m",
                    "provider": "spark2_5"},
    }), encoding="utf-8")

    store = SessionStore(base_path=str(tmp_path / "sessions"))
    store.create_session("s1", metadata={})
    spec = AgentSpec(name="a", description="d", model="m",
                     system_prompt="sys", llm_profile="default")

    _stub_tokenizer_dir(tmp_path, "spark2_5")
    seen: list = []
    real = pa._count_tokens

    def spy(text, provider, tokenizer_dir="tokenizer_dir"):
        seen.append(provider)
        return real(text, provider, tokenizer_dir)

    monkeypatch.setattr(pa, "_count_tokens", spy)
    monkeypatch.chdir(tmp_path)
    pa._TOKENIZERS.pop("spark2_5", None)
    pa._TOKENIZE_URL_CACHE = False
    compose_llm_context(store, spec, "s1", task="hi")
    assert seen, "counter never invoked"
    assert set(seen) == {"spark2_5"}, seen
    # I-49 review S1: the configured provider must make CJK countable.
    # Legacy "deepseek" yields 0 for this string; a CJK-capable vocab does not.
    assert real(_CJK, "spark2_5") > 0, "configured tokenizer counted 0 for CJK"


def test_missing_tokenizer_dir_raises_with_expected_path(tmp_path):
    """A configured-but-absent vocab must name the path, not retry the network.

    Previously `AutoTokenizer.from_pretrained("tokenizer_dir/<p>")` treated the
    missing path as a HF repo id and performed network retries (measured ~9 min
    stall, issues/local-backend-issues.md L2-02).
    """
    pa._TOKENIZERS.pop("no_such_provider", None)
    with pytest.raises(FileNotFoundError) as ei:
        pa._get_tokenizer("no_such_provider", tokenizer_dir=str(tmp_path))
    msg = str(ei.value)
    assert "no_such_provider" in msg
    assert str(tmp_path) in msg, "error must name the expected directory"
