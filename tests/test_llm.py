import pytest

from pipeline.llm import LLM, FakeProvider, LLMError, QuotaExhaustedError, RetryableLLMError, _classify_429, extract_json


def _llm(*providers, **kw):
    sleeps: list[float] = []
    llm = LLM([(p, 0) for p in providers], sleep=sleeps.append, **kw)
    return llm, sleeps


def test_extract_json_variants():
    assert extract_json('{"a": 1}') == {"a": 1}
    assert extract_json('```json\n{"a": 1}\n```') == {"a": 1}
    assert extract_json('Sure! Here it is: {"a": [1, 2]} hope that helps') == {"a": [1, 2]}
    with pytest.raises(LLMError):
        extract_json("no json here")


def test_retries_on_rate_limit_then_succeeds():
    p = FakeProvider([RetryableLLMError("429"), RetryableLLMError("429"), "ok"])
    llm, sleeps = _llm(p, base_backoff_s=1, max_backoff_s=10)
    assert llm.complete("hi") == "ok"
    assert len(sleeps) == 2 and sleeps[1] >= sleeps[0] * 1.5  # exponential backoff


def test_honours_retry_after():
    p = FakeProvider([RetryableLLMError("429", retry_after=7), "ok"])
    llm, sleeps = _llm(p)
    llm.complete("hi")
    assert 7 <= sleeps[0] <= 7 * 1.25


def test_falls_back_to_next_provider():
    bad = FakeProvider([RetryableLLMError("429")] * 3, name="gemini")
    good = FakeProvider(["from groq"], name="groq")
    calls: list[str] = []
    llm, _ = _llm(bad, good, max_retries=2, on_call=calls.append)
    assert llm.complete("hi") == "from groq"
    assert calls == ["llm.gemini/fake"] * 3 + ["llm.groq/fake"]


def test_non_retryable_error_skips_to_next_provider_immediately():
    bad = FakeProvider([LLMError("401 bad key")], name="gemini")
    good = FakeProvider(["ok"], name="groq")
    llm, sleeps = _llm(bad, good)
    assert llm.complete("hi") == "ok" and sleeps == []


def test_all_providers_fail():
    llm, _ = _llm(FakeProvider([LLMError("nope")]))
    with pytest.raises(LLMError, match="All LLM providers failed"):
        llm.complete("hi")


def test_complete_json_reasks_on_bad_json():
    p = FakeProvider(["not json at all", '{"ok": true}'])
    llm, _ = _llm(p)
    assert llm.complete_json("give json") == {"ok": True}
    assert "ONLY valid JSON" in p.calls[1]


def test_no_providers_configured():
    with pytest.raises(LLMError, match="No LLM provider"):
        LLM([])


def test_daily_quota_is_not_retried_and_is_remembered(tmp_path):
    f = tmp_path / "exhausted.json"
    gem = FakeProvider([QuotaExhaustedError("daily")], name="gemini")
    groq = FakeProvider(["a", "b"], name="groq")
    sleeps: list[float] = []
    llm = LLM([(gem, 0), (groq, 0)], sleep=sleeps.append, exhausted_file=f)
    assert llm.complete("x") == "a" and sleeps == []
    # a new LLM (next stage / rerun) skips gemini without calling it
    llm2 = LLM([(gem, 0), (groq, 0)], sleep=sleeps.append, exhausted_file=f)
    assert llm2.complete("y") == "b" and len(gem.calls) == 1


def test_classify_429():
    assert isinstance(_classify_429("g", "GenerateRequestsPerDayPerProjectPerModel-FreeTier"), QuotaExhaustedError)
    assert isinstance(_classify_429("g", "Please retry in 21h", retry_after=76000), QuotaExhaustedError)
    e = _classify_429("g", "rate limit 'retryDelay': '7s'")
    assert type(e) is RetryableLLMError and e.retry_after == 7
