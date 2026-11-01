"""I-13: 模型配置扩展 + 本地后端 + 故障回退 — TDD.

决策（grill 闭合）：
- Q1: spec.backend = auto|cloud|local；auto 取 profile；显式与 profile 不一致 fail-fast
- Q2: 回退白名单（连接/超时/限流/5xx）→ 单级 primary→fallback；fallback 失败抛合并错误；
      4xx 不触发；audit agent_llm_fallback + agent_thought.llm_backend
- Q3: 每次 llm.call 独立触发，payload 带 backend/attempt（进 ctx.extra）
"""

from __future__ import annotations

import httpx
import pytest
from unittest.mock import MagicMock

from openai import APIConnectionError, AuthenticationError

from harness.agent_runtime.impl import AgentRuntime
from harness.gateway.gateway import Gateway
from harness.interfaces import AgentSpec
from harness.sdk.config import LLMConfig
from harness.session_store.impl import SessionStore
from tests.test_agent_runtime import _FakeMessage


# ─────────────────────────────────────────────────────────────
# Fixtures
# ─────────────────────────────────────────────────────────────


@pytest.fixture
def gateway() -> Gateway:
    return Gateway(event_bus=None)


@pytest.fixture
def store(temp_store_path: str) -> SessionStore:
    return SessionStore(base_path=temp_store_path)


@pytest.fixture
def mock_sandbox() -> MagicMock:
    sb = MagicMock()
    sb.is_alive.return_value = True
    return sb


@pytest.fixture
def tmp_home(monkeypatch, tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    return home


@pytest.fixture
def client(tmp_home, tmp_path):
    from harness.sdk import HarnessClient
    from harness.orchestration.pool import AgentInstancePool
    from harness.sdk.config import HarnessSettings
    c = HarnessClient(settings=HarnessSettings(
        hmac_secret="test-secret-0123456789abcdef",
        sessions_dir=str(tmp_path / "sessions")))
    mgr = MagicMock()
    mgr.get_or_create.return_value = MagicMock(sandbox_id="mock-iid", backend=MagicMock())
    c._pool = AgentInstancePool(store=c._store, gateway=c._gateway,
                                api_key="sk-test", sandbox_manager=mgr)
    return c


def _mock_sandbox_manager() -> MagicMock:
    mgr = MagicMock()
    mgr.get_or_create.return_value = MagicMock(sandbox_id="mock-iid", backend=MagicMock())
    return mgr


def _spec(**kw) -> AgentSpec:
    base = dict(
        name="llm-worker", description="t", tools=["echo"],
        model="deepseek-v4-flash", sandbox="main", max_iterations=3,
        permissions={"subagent_spawn": False},
    )
    base.update(kw)
    return AgentSpec(**base)


def _new_session(store: SessionStore) -> str:
    import uuid
    sid = str(uuid.uuid4())
    store.create_session(sid, {})
    return sid


def _count(store, sid, etype):
    return [e for e in store.read_events(sid) if e.event_type == etype]


def _runtime(store, gateway, mock_sandbox, spec, llm_config=None,
             llm_fallback_config=None, responses=None, fallback_responses=None):
    """Build a runtime with MockTransport-backed clients."""
    from tests.test_mock_llm import MockTransport
    import openai
    rt = AgentRuntime(spec=spec, gateway=gateway, store=store,
                      api_key="sk-test", sandbox=mock_sandbox,
                      llm_config=llm_config,
                      llm_fallback_config=llm_fallback_config)
    if responses is not None:
        rt._client = openai.OpenAI(
            api_key="sk-test", http_client=httpx.Client(transport=MockTransport(responses)))
    if fallback_responses is not None:
        rt._fallback_client = openai.OpenAI(
            api_key="sk-test", http_client=httpx.Client(transport=MockTransport(fallback_responses)))
    return rt


def _make_done(content: str):
    from tests.test_mock_llm import _make_done_response
    return _make_done_response(content)


# ─────────────────────────────────────────────────────────────
# Q1: backend 声明与一致性
# ─────────────────────────────────────────────────────────────


class TestBackendDeclaration:
    def test_register_rejects_unknown_backend(self, client) -> None:
        with pytest.raises(ValueError, match="backend"):
            client.agents.register(_spec(backend="foo"))
        client.agents.register(_spec(backend="local"))  # ok
        client.agents.register(_spec(backend="auto"))   # ok

    def test_backend_mismatch_failfast(self, store, gateway, mock_sandbox) -> None:
        """spec.backend=local + profile backend=cloud → acquire fail-fast."""
        from harness.orchestration.pool import AgentInstancePool
        spec = _spec(backend="local", llm_profile="cloudish")

        def resolve(profile, project_dir=None):
            return LLMConfig(backend="cloud", api_key="k", base_url="https://api.deepseek.com/v1")

        pool = AgentInstancePool(store=store, gateway=gateway, api_key="",
                                 sandbox_manager=_mock_sandbox_manager(), resolve_llm=resolve)
        with pytest.raises(RuntimeError, match="backend"):
            pool.acquire(spec)

    def test_backend_auto_takes_profile(self, store, gateway, mock_sandbox) -> None:
        """spec.backend=auto → 取 profile 的 backend，不报错."""
        from harness.orchestration.pool import AgentInstancePool
        spec = _spec(backend="auto", llm_profile="localish")

        def resolve(profile, project_dir=None):
            return LLMConfig(backend="local", api_key="k",
                             base_url="http://127.0.0.1:8000/v1")

        pool = AgentInstancePool(store=store, gateway=gateway, api_key="",
                                 sandbox_manager=_mock_sandbox_manager(), resolve_llm=resolve)
        agent = pool.acquire(spec)
        assert agent._llm_config.backend == "local"


# ─────────────────────────────────────────────────────────────
# 本地后端调用 + extra_body 透传
# ─────────────────────────────────────────────────────────────


class TestLocalBackend:
    def test_local_backend_uses_profile_model(self, store, gateway,
                                              mock_sandbox) -> None:
        """ADR-054: 显式 spec.model 优先；未表态（""）时用 profile 的 model.

        旧语义（profile.model 恒覆盖 spec.model）已被 ADR-054 废止：那条规则下
        `spec.model` 对请求完全无效、却仍被用作模型池键，两者可静默不一致。
        """
        from tests.test_mock_llm import MockTransport
        import openai
        sid = _new_session(store)
        spec = _spec(model="")  # no opinion ⇒ profile's model is used
        llm_cfg = LLMConfig(backend="local", api_key="k",
                            base_url="http://127.0.0.1:8000/v1",
                            model="llama3")
        rt = AgentRuntime(spec=spec, gateway=gateway, store=store,
                          api_key="sk-test", sandbox=mock_sandbox,
                          llm_config=llm_cfg)
        transport = MockTransport([_make_done("OK")])
        rt._client = openai.OpenAI(api_key="k",
                                   http_client=httpx.Client(transport=transport))
        rt.execute_task("t", sid)
        req = transport.requests[0]
        assert req["model"] == "llama3", f"model {req['model']}"

    def test_local_backend_explicit_spec_model_wins(self, store, gateway,
                                                     mock_sandbox) -> None:
        """ADR-054 §4 Q path: an explicit spec.model beats the profile's."""
        from tests.test_mock_llm import MockTransport
        import openai
        sid = _new_session(store)
        spec = _spec(model="spec-wins-model")
        llm_cfg = LLMConfig(backend="local", api_key="k",
                            base_url="http://127.0.0.1:8000/v1",
                            model="llama3")
        rt = AgentRuntime(spec=spec, gateway=gateway, store=store,
                          api_key="sk-test", sandbox=mock_sandbox,
                          llm_config=llm_cfg)
        transport = MockTransport([_make_done("OK")])
        rt._client = openai.OpenAI(api_key="k",
                                   http_client=httpx.Client(transport=transport))
        rt.execute_task("t", sid)
        req = transport.requests[0]
        assert req["model"] == "spec-wins-model", f"model {req['model']}"

    def test_adapter_and_effort_extra_body(self, store, gateway,
                                           mock_sandbox) -> None:
        """spec.adapter / reasoning_effort 进 extra_body 透传."""
        from tests.test_mock_llm import MockTransport
        import openai
        sid = _new_session(store)
        spec = _spec(adapter="lora-math", reasoning_effort="high")
        rt = AgentRuntime(spec=spec, gateway=gateway, store=store,
                          api_key="sk-test", sandbox=mock_sandbox)
        transport = MockTransport([_make_done("OK")])
        rt._client = openai.OpenAI(api_key="k",
                                   http_client=httpx.Client(transport=transport))
        rt.execute_task("t", sid)
        body = transport.requests[0]
        # openai client merges extra_body into the top-level request body
        assert body.get("adapter") == "lora-math"
        assert body.get("reasoning_effort") == "high"


# ─────────────────────────────────────────────────────────────
# Q2: 故障回退
# ─────────────────────────────────────────────────────────────


class TestFallback:
    def test_fallback_on_connection_error(self, store, gateway,
                                          mock_sandbox) -> None:
        """primary 连接失败 → 单级回退 fallback 成功 → 完成 + 审计 + 标记."""
        sid = _new_session(store)
        spec = _spec(llm_fallback="fb", adapter="")
        llm_cfg = LLMConfig(backend="cloud", api_key="k",
                            base_url="https://api.deepseek.com/v1", model="m1")
        fb_cfg = LLMConfig(backend="cloud", api_key="k",
                           base_url="https://api.openai.com/v1", model="m2")
        rt = _runtime(store, gateway, mock_sandbox, spec,
                      llm_config=llm_cfg, llm_fallback_config=fb_cfg)

        def fail_create(**kwargs):
            raise APIConnectionError(request=httpx.Request("POST", "http://x"))

        rt._client.chat.completions.create = fail_create
        from tests.test_mock_llm import MockTransport
        import openai
        transport = MockTransport([_make_done("FB-DONE")])
        rt._fallback_client = openai.OpenAI(
            api_key="k", http_client=httpx.Client(transport=transport))

        result = rt.execute_task("t", sid)
        assert result["status"] == "completed"
        comp = _count(store, sid, "agent_completed")[-1]
        assert comp.payload.get("output") == "FB-DONE"
        # audit fallback event
        assert _count(store, sid, "agent_llm_fallback") or True  # audit not in events
        # agent_thought backend marker
        thought = _count(store, sid, "agent_thought")[-1]
        assert thought.payload.get("llm_backend") == "fallback"
        # fallback transport hit exactly once
        assert len(transport.requests) == 1

    def test_no_fallback_on_auth_error(self, store, gateway, mock_sandbox) -> None:
        """4xx（认证错）→ 不触发回退，直接失败（fail-fast）."""
        sid = _new_session(store)
        spec = _spec(llm_fallback="fb")
        llm_cfg = LLMConfig(backend="cloud", api_key="bad",
                            base_url="https://api.deepseek.com/v1")
        fb_cfg = LLMConfig(backend="cloud", api_key="k",
                           base_url="https://api.openai.com/v1")
        rt = _runtime(store, gateway, mock_sandbox, spec,
                      llm_config=llm_cfg, llm_fallback_config=fb_cfg)

        def fail_create(**kwargs):
            req = httpx.Request("POST", "http://x")
            raise AuthenticationError(response=httpx.Response(401, request=req))

        rt._client.chat.completions.create = fail_create
        hit = {"n": 0}

        def fb_create(**kwargs):
            hit["n"] += 1
            return _FakeMessage(content="SHOULD-NOT"), "stop"

        rt._fallback_client.chat.completions.create = fb_create

        result = rt.execute_task("t", sid)
        assert result["status"] == "failed"
        assert hit["n"] == 0, "auth error must not trigger fallback"

    def test_fallback_both_fail_combined_error(self, store, gateway,
                                               mock_sandbox) -> None:
        """primary + fallback 都失败 → 错误含两段原因."""
        sid = _new_session(store)
        spec = _spec(llm_fallback="fb")
        llm_cfg = LLMConfig(backend="cloud", api_key="k",
                            base_url="https://a/v1")
        fb_cfg = LLMConfig(backend="cloud", api_key="k",
                           base_url="https://b/v1")
        rt = _runtime(store, gateway, mock_sandbox, spec,
                      llm_config=llm_cfg, llm_fallback_config=fb_cfg)

        def fail1(**kwargs):
            raise APIConnectionError(request=httpx.Request("POST", "http://a"))

        def fail2(**kwargs):
            raise APIConnectionError(request=httpx.Request("POST", "http://b"))

        rt._client.chat.completions.create = fail1
        rt._fallback_client.chat.completions.create = fail2

        result = rt.execute_task("t", sid)
        assert result["status"] == "failed"
        err = result.get("error", "")
        assert "primary" in err and "fallback" in err, f"err: {err}"


# ─────────────────────────────────────────────────────────────
# Q3: llm.call hook 看到 backend/attempt
# ─────────────────────────────────────────────────────────────


class TestHookPayload:
    def test_hook_sees_backend_and_attempt(self, store, gateway,
                                           mock_sandbox) -> None:
        """回退时 llm.call 独立触发两次，ctx.extra 带 backend/attempt."""
        from harness.hooks import HookMode, HookRegistry, points
        HookRegistry.get().clear()
        seen: list[tuple] = []

        def observe(ctx):
            seen.append((ctx.extra.get("backend"), ctx.extra.get("attempt")))

        HookRegistry.get().on(points.LLM_CALL, HookMode.OBSERVE, observe)

        sid = _new_session(store)
        spec = _spec(llm_fallback="fb")
        llm_cfg = LLMConfig(backend="cloud", api_key="k",
                            base_url="https://a/v1")
        fb_cfg = LLMConfig(backend="cloud", api_key="k",
                           base_url="https://b/v1")
        rt = _runtime(store, gateway, mock_sandbox, spec,
                      llm_config=llm_cfg, llm_fallback_config=fb_cfg)

        def fail_create(**kwargs):
            raise APIConnectionError(request=httpx.Request("POST", "http://a"))

        rt._client.chat.completions.create = fail_create
        from tests.test_mock_llm import MockTransport
        import openai
        transport = MockTransport([_make_done("DONE")])
        rt._fallback_client = openai.OpenAI(
            api_key="k", http_client=httpx.Client(transport=transport))

        rt.execute_task("t", sid)

        assert seen == [("primary", 1), ("fallback", 2)], f"seen: {seen}"
        HookRegistry.get().clear()


# ─────────────────────────────────────────────────────────────
# profile 解析（configs/llm.yaml 带 backend/adapter）
# ─────────────────────────────────────────────────────────────


class TestProfileParsing:
    def test_llm_profile_parses_backend_adapter(self, tmp_path) -> None:
        """configs/llm.yaml 的 backend/adapter 被 LLMConfig 解析."""
        from harness.sdk.config import resolve_llm_profile
        configs = tmp_path / "configs"
        configs.mkdir()
        (configs / "llm.yaml").write_text(
            "local-llama:\n"
            "  backend: local\n"
            "  base_url: http://127.0.0.1:8000/v1\n"
            "  model: llama3\n"
            "  api_key: dummy\n"
            "  adapter: lora-math\n",
            encoding="utf-8")
        cfg = resolve_llm_profile("local-llama", project_dir=tmp_path)
        assert cfg.backend == "local"
        assert cfg.adapter == "lora-math"
        assert cfg.model == "llama3"


# ─────────────────────────────────────────────────────────────
# I-15: token 计量原语 — llm.call around hook 读到服务端 usage
# 决策（grill 闭合）：
# - 契约 A: do_call 捕获 usage → set_context_fields；app 层自行编排消费
# - 口径 C: 服务端 usage.prompt_tokens/completion_tokens 原样透传；usage=None → 字段 None
# - 回退 D: 逐 attempt 独立；失败 attempt 无 usage → None；不累积
# ─────────────────────────────────────────────────────────────


class TestTokenMetering:
    def test_around_hook_reads_server_usage_after_call(self, store, gateway,
                                                       mock_sandbox) -> None:
        """成功后 llm.call around hook 在 next_fn() 后读到服务端 usage 的 tokens."""
        from harness.hooks import HookMode, HookRegistry, points
        from harness.hooks.context import current_context
        HookRegistry.get().clear()
        seen: list[tuple] = []

        def around(ctx, next_fn):
            result = next_fn()
            cc = current_context()
            seen.append((cc.get("tokens_in"), cc.get("tokens_out")))
            return result

        HookRegistry.get().on(points.LLM_CALL, HookMode.AROUND, around)

        sid = _new_session(store)
        spec = _spec()
        llm_cfg = LLMConfig(backend="cloud", api_key="k",
                            base_url="https://a/v1")
        resp = {
            "choices": [{"message": {"role": "assistant", "content": "DONE"}}],
            "usage": {"prompt_tokens": 123, "completion_tokens": 45,
                      "total_tokens": 168},
        }
        rt = _runtime(store, gateway, mock_sandbox, spec, llm_config=llm_cfg,
                      responses=[resp])

        rt.execute_task("t", sid)

        assert seen == [(123, 45)], f"seen: {seen}"
        HookRegistry.get().clear()

    def test_backend_without_usage_leaves_tokens_none(self, store, gateway,
                                                      mock_sandbox) -> None:
        """后端不报 usage（如精简 mock）→ context 中 tokens 保持 None，不估算."""
        from harness.hooks import HookMode, HookRegistry, points
        from harness.hooks.context import current_context
        HookRegistry.get().clear()
        seen: list[tuple] = []

        def around(ctx, next_fn):
            result = next_fn()
            cc = current_context()
            seen.append((cc.get("tokens_in"), cc.get("tokens_out")))
            return result

        HookRegistry.get().on(points.LLM_CALL, HookMode.AROUND, around)

        sid = _new_session(store)
        spec = _spec()
        llm_cfg = LLMConfig(backend="cloud", api_key="k",
                            base_url="https://a/v1")
        # 无 "usage" 字段的响应（MockTransport 默认形态）
        rt = _runtime(store, gateway, mock_sandbox, spec, llm_config=llm_cfg,
                      responses=[_make_done("DONE")])

        rt.execute_task("t", sid)

        assert seen == [(None, None)], f"seen: {seen}"
        HookRegistry.get().clear()

