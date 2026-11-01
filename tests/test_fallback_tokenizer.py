"""The fallback tokenizer must actually be able to count the content.

MEASURED failure (delayed by dozens of runs): on a cloud profile with no local
server, `_count_tokens` fell back to `tokenizer_dir/<provider>` and counted CJK
as ZERO:

    日文 2973 字符 → 0 tokens      (the server reports 2293)
    中文 2160 字符 → 0 tokens      (the server reports 1564)

ROOT CAUSE — not a bad artifact: `AutoTokenizer.from_pretrained(dir)` returned the
SLOW `LlamaTokenizer` (no `tokenizer.model` is present, only `tokenizer.json`),
and that class does not implement the byte-level BPE decode the vocab relies on.
Its 128,000-entry vocab DOES contain ~35,679 byte-level entries (`ä`, `ä¸`, `äº` —
UTF-8 bytes mapped to latin-1), so the artifact is fine; the LOADER was wrong.

Loading the same file as a FAST tokenizer is accurate:

    日文  2289 vs 2293  (-0.2%)
    中文  1560 vs 1564  (-0.3%)
    英文   501 vs  505  (-0.8%)

A tokenizer that cannot encode non-empty text is a correctness bug, not a
precision issue: budgets, water marks and the `history_context_truncated` alarm
all silently misreport.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

TOKDIR = ROOT / "tokenizer_dir" / "deepseek"

pytestmark = pytest.mark.skipif(not TOKDIR.is_dir(),
                                reason="no fallback tokenizer artifacts present")


def _tok():
    from harness.prompt_assembly import _get_tokenizer
    return _get_tokenizer("deepseek", str(ROOT / "tokenizer_dir"))


def test_cjk_is_counted_not_silently_zero():
    """CJK must produce tokens — the whole point of the fallback."""
    tk = _tok()
    assert len(tk.encode("厦门朝日俱乐部")) > 0, (
        "CJK encodes to zero tokens: the loader picked a class that does not "
        "implement the vocab's byte-level BPE (see this module's docstring)")
    assert len(tk.encode("あたしは、ヤンファが")) > 0


def test_counts_are_close_to_the_served_model():
    """Sanity: a full Japanese chunk is counted within a few percent of reality.

    The exact server count for this chunk is 2293 tokens (measured via the
    provider's `usage.prompt_tokens`); the local fallback must be in the same
    ballpark, otherwise budgets are meaningless.
    """
    tk = _tok()
    # OH has no translation corpus; keep the same real chunk as a fixture so
    # this regression still runs (the tokenizer count is artifact-dependent).
    ja = (ROOT / "tests" / "fixtures" / "ja_chunk_sample.txt")
    if not ja.exists():
        pytest.skip("sample chunk not present")
    n = len(tk.encode(ja.read_text(encoding="utf-8")))
    assert 0.9 * 2293 <= n <= 1.1 * 2293, f"got {n}, expected ~2293"


def test_cached_broken_tokenizer_is_refused_at_count_time(monkeypatch):
    """I-65: the COUNT-time guard also covers an already-cached bad tokenizer.

    The loader guard only protects fresh loads.  A tokenizer cached before the
    fix (or injected via monkeypatch) must not silently report 0 for real text.
    """
    monkeypatch.setenv("HARNESS_TOKENIZE_URL", "off")
    from harness import prompt_assembly as pa

    class _Deaf:
        def encode(self, text, *a, **kw):
            return []

    monkeypatch.setitem(pa._TOKENIZERS, "deepseek", _Deaf())
    with pytest.raises(ValueError, match="non-empty text counted as 0 tokens"):
        pa._count_tokens("厦门", "deepseek", str(ROOT / "tokenizer_dir"))


def test_empty_and_whitespace_text_count_as_zero(monkeypatch):
    """Legitimately empty text still returns 0 (no false positive)."""
    monkeypatch.setenv("HARNESS_TOKENIZE_URL", "off")
    from harness import prompt_assembly as pa

    class _Deaf:
        def encode(self, text, *a, **kw):
            return []

    monkeypatch.setitem(pa._TOKENIZERS, "deepseek", _Deaf())
    assert pa._count_tokens("", "deepseek", str(ROOT / "tokenizer_dir")) == 0
    assert pa._count_tokens("   ", "deepseek", str(ROOT / "tokenizer_dir")) == 0


def test_server_tokenizer_path_is_authoritative(monkeypatch):
    """The server's /tokenize result is returned as-is, even if it is 0."""
    from harness import prompt_assembly as pa

    monkeypatch.setattr(pa, "_server_count_tokens", lambda text: 0)
    assert pa._count_tokens("厦门", "deepseek", str(ROOT / "tokenizer_dir")) == 0


def _deaf_loader():
    class _Deaf:
        def encode(self, text, *a, **kw):
            return []
    return _Deaf()


def test_loader_rejects_fast_tokenizer_that_counts_cjk_as_zero(
        monkeypatch, tmp_path):
    """The LOAD-TIME guard must reject a fast tokenizer that encodes CJK as []."""
    import uuid

    import transformers

    from harness import prompt_assembly as pa

    provider = f"guard-fast-{uuid.uuid4().hex[:8]}"
    provider_dir = tmp_path / provider
    provider_dir.mkdir()
    (provider_dir / "tokenizer.json").write_text("{}", encoding="utf-8")
    monkeypatch.setattr(transformers, "PreTrainedTokenizerFast",
                        lambda **kw: _deaf_loader())
    try:
        with pytest.raises(ValueError, match="CJK as 0 tokens"):
            pa._get_tokenizer(provider, str(tmp_path))
    finally:
        pa._TOKENIZERS.pop(provider, None)


def test_loader_rejects_autotokenizer_fallback_that_counts_cjk_as_zero(
        monkeypatch, tmp_path):
    """No tokenizer.json ⇒ AutoTokenizer path; guard must still reject it."""
    import uuid

    import transformers

    from harness import prompt_assembly as pa

    provider = f"guard-slow-{uuid.uuid4().hex[:8]}"
    (tmp_path / provider).mkdir()
    monkeypatch.setattr(transformers.AutoTokenizer, "from_pretrained",
                        classmethod(lambda cls, *a, **kw: _deaf_loader()))
    try:
        with pytest.raises(ValueError, match="CJK as 0 tokens"):
            pa._get_tokenizer(provider, str(tmp_path))
    finally:
        pa._TOKENIZERS.pop(provider, None)
