"""Unit tests for Gemini Client error handling and invariants in Apex-Alpha.
Ensures that missing API keys or upstream model failures fail loudly with explicit errors
rather than silently returning canned or ungrounded responses.
"""

from unittest.mock import MagicMock
import pytest
from app.llm import generate_structured, stream_text
from app.schemas import DiagnosisProposal
from tests.conftest import proposal


def test_generate_structured_raises_when_api_key_missing(monkeypatch):
    """Invariant: When GEMINI_API_KEY is not configured, generate_structured must raise RuntimeError."""
    monkeypatch.setenv("GEMINI_API_KEY", "")
    with pytest.raises(RuntimeError, match="GEMINI_API_KEY environment variable is not configured"):
        generate_structured(
            prompt="Analyze NVDA fundamental thesis",
            system_instruction="Generate Bull Case",
            schema=DiagnosisProposal,
        )


def test_generate_structured_raises_when_gemini_api_fails(monkeypatch):
    """Invariant: When Gemini API raises an exception, generate_structured must fail loudly."""
    monkeypatch.setenv("GEMINI_API_KEY", "test-valid-looking-key-12345")

    mock_client = MagicMock()
    mock_client.models.generate_content.side_effect = Exception("503 Service Unavailable: Model overloaded")
    monkeypatch.setattr("app.llm.get_gemini_client", lambda: mock_client)

    with pytest.raises(RuntimeError, match="Gemini API structured generation failed: .*503 Service Unavailable"):
        generate_structured(
            prompt="Analyze NVDA fundamental thesis",
            system_instruction="Generate Bull Case",
            schema=DiagnosisProposal,
        )


def test_stream_text_raises_when_api_key_missing(monkeypatch):
    """Invariant: When GEMINI_API_KEY is not configured, stream_text must raise RuntimeError."""
    monkeypatch.setenv("GEMINI_API_KEY", "")
    with pytest.raises(RuntimeError, match="GEMINI_API_KEY environment variable is not configured"):
        list(stream_text(prompt="Analyze NVDA", system_instruction="Bull Case"))


def test_stream_text_raises_when_gemini_fails(monkeypatch):
    """Invariant: When Gemini streaming API raises an exception, stream_text must fail loudly."""
    monkeypatch.setenv("GEMINI_API_KEY", "test-valid-looking-key-12345")

    mock_client = MagicMock()
    mock_client.models.generate_content_stream.side_effect = Exception("Connection reset by peer")
    monkeypatch.setattr("app.llm.get_gemini_client", lambda: mock_client)

    with pytest.raises(RuntimeError, match="Gemini streaming generation failed: .*Connection reset by peer"):
        list(stream_text(prompt="Analyze NVDA", system_instruction="Bull Case"))


# --- Model fallback chain ---

from app import llm as llm_module
from app.schemas import DiagnosisProposal
from tests.conftest import proposal


def _client_failing(monkeypatch, failures: dict):
    """Mock client whose models fail with the given error text; others succeed."""
    monkeypatch.setenv("GEMINI_API_KEY", "test-valid-looking-key-12345")
    calls: list[str] = []

    def generate(model, **kwargs):
        calls.append(model)
        if model in failures:
            raise Exception(failures[model])
        response = MagicMock()
        response.parsed = f"note from {model}"
        response.usage_metadata = None
        return response

    def stream(model, **kwargs):
        calls.append(model)
        if model in failures:
            raise Exception(failures[model])
        return iter([MagicMock(text=f"chunk from {model}")])

    client = MagicMock()
    client.models.generate_content.side_effect = generate
    client.models.generate_content_stream.side_effect = stream
    monkeypatch.setattr("app.llm.get_gemini_client", lambda: client)
    return calls


def test_default_chain_is_36_37_38():
    assert llm_module.MODEL_CHAIN == ["gemini-3.6-flash", "gemini-3.7-flash", "gemini-3.8-flash"]


@pytest.mark.parametrize("error", ["429 RESOURCE_EXHAUSTED", "503 UNAVAILABLE", "404 NOT_FOUND model retired"])
def test_structured_falls_back_to_next_model(monkeypatch, error):
    calls = _client_failing(monkeypatch, {"gemini-3.6-flash": error})
    result = generate_structured(prompt="p", system_instruction="s", schema=DiagnosisProposal)
    assert result == "note from gemini-3.7-flash"
    assert calls == ["gemini-3.6-flash", "gemini-3.7-flash"]


def test_structured_does_not_fall_back_on_bad_request(monkeypatch):
    calls = _client_failing(monkeypatch, {"gemini-3.6-flash": "400 INVALID_ARGUMENT schema rejected"})
    with pytest.raises(RuntimeError, match="400 INVALID_ARGUMENT"):
        generate_structured(prompt="p", system_instruction="s", schema=DiagnosisProposal)
    assert calls == ["gemini-3.6-flash"]


def test_all_models_exhausted_reports_the_daily_quota(monkeypatch):
    _client_failing(monkeypatch, {
        "gemini-3.6-flash": "429 RESOURCE_EXHAUSTED GenerateRequestsPerDayPerProjectPerModel-FreeTier",
        "gemini-3.7-flash": "404 NOT_FOUND",
        "gemini-3.8-flash": "429 RESOURCE_EXHAUSTED GenerateRequestsPerDayPerProjectPerModel-FreeTier",
    })
    with pytest.raises(RuntimeError) as err:
        generate_structured(prompt="p", system_instruction="s", schema=DiagnosisProposal)
    assert all(m in str(err.value) for m in llm_module.MODEL_CHAIN)
    status, message, retryable = llm_module.describe_llm_error(err.value)
    assert (status, retryable) == (429, False)
    assert "daily" in message


def test_stream_falls_back_before_first_chunk(monkeypatch):
    calls = _client_failing(monkeypatch, {"gemini-3.6-flash": "503 UNAVAILABLE"})
    assert list(stream_text(prompt="p", system_instruction="s")) == ["chunk from gemini-3.7-flash"]
    assert calls == ["gemini-3.6-flash", "gemini-3.7-flash"]


def test_stream_does_not_switch_models_after_text_started(monkeypatch):
    calls = _client_failing(monkeypatch, {})

    def half_then_fail(model, **kwargs):
        calls.append(model)
        yield MagicMock(text="partial ")
        raise Exception("503 UNAVAILABLE")

    llm_module.get_gemini_client().models.generate_content_stream.side_effect = half_then_fail
    received = []
    with pytest.raises(RuntimeError):
        for chunk in stream_text(prompt="p", system_instruction="s"):
            received.append(chunk)
    assert received == ["partial "]
    assert calls == ["gemini-3.6-flash"]


def test_daily_quota_plus_overloaded_fallbacks_is_retryable():
    status, message, retryable = llm_module.describe_llm_error(RuntimeError(
        "gemini-3.6-flash: 429 GenerateRequestsPerDayPerProjectPerModel-FreeTier | gemini-3.7-flash: 503 UNAVAILABLE"
    ))
    assert (status, retryable) == (503, True)
    assert "fallback models are overloaded" in message


# --- Groq backup ---

@pytest.fixture(autouse=True)
def no_groq_key_by_default(monkeypatch):
    monkeypatch.delenv("GROQ_API_KEY", raising=False)


def _fake_groq(monkeypatch, calls: list):
    monkeypatch.setenv("GROQ_API_KEY", "test-groq-key")
    json_text = proposal().model_dump_json()

    def fake(prompt, system_instruction="", schema=None, **kwargs):
        calls.append(schema)
        return json_text if schema is not None else (json_text if isinstance(json_text, str) else ("groq prose", 1, 2))

    monkeypatch.setattr("app.llm.groq_chat", fake)


def test_groq_serves_when_every_gemini_model_is_unavailable(monkeypatch):
    gemini_calls = _client_failing(monkeypatch, {m: "429 RESOURCE_EXHAUSTED" for m in llm_module.MODEL_CHAIN})
    groq_calls: list = []
    _fake_groq(monkeypatch, groq_calls)
    result = generate_structured(prompt="p", system_instruction="s", schema=DiagnosisProposal)
    assert result == proposal()
    assert gemini_calls == llm_module.MODEL_CHAIN
    assert groq_calls == [DiagnosisProposal]


def test_groq_serves_when_no_gemini_key(monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "")
    groq_calls: list = []
    _fake_groq(monkeypatch, groq_calls)
    result = generate_structured(prompt="p", system_instruction="s", schema=DiagnosisProposal)
    assert result == proposal()


def test_groq_not_used_for_bad_request(monkeypatch):
    _client_failing(monkeypatch, {"gemini-3.6-flash": "400 INVALID_ARGUMENT"})
    groq_calls: list = []
    _fake_groq(monkeypatch, groq_calls)
    with pytest.raises(RuntimeError, match="400 INVALID_ARGUMENT"):
        generate_structured(prompt="p", system_instruction="s", schema=DiagnosisProposal)
    assert groq_calls == []


def test_groq_failure_is_reported_with_gemini_errors(monkeypatch):
    _client_failing(monkeypatch, {m: "503 UNAVAILABLE" for m in llm_module.MODEL_CHAIN})
    monkeypatch.setenv("GROQ_API_KEY", "test-groq-key")

    def boom(*args, **kwargs):
        raise RuntimeError("429 rate_limit_exceeded")

    monkeypatch.setattr("app.llm.groq_chat", boom)
    with pytest.raises(RuntimeError) as err:
        generate_structured(prompt="p", system_instruction="s", schema=DiagnosisProposal)
    assert "groq" in str(err.value) and "gemini-3.8-flash" in str(err.value)


def test_stream_falls_back_to_groq_as_one_chunk(monkeypatch):
    _client_failing(monkeypatch, {m: "503 UNAVAILABLE" for m in llm_module.MODEL_CHAIN})
    monkeypatch.setenv("GROQ_API_KEY", "test-groq-key")
    monkeypatch.setattr("app.llm.groq_chat", lambda *a, **k: "groq prose" if "aegis-sre" == "aegis-sre" else ("groq prose", 1, 2))
    assert list(stream_text(prompt="p", system_instruction="s")) == ["groq prose"]
