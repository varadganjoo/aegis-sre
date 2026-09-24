"""Groq's gpt-oss sometimes double-escapes newlines inside JSON strings; both forms must parse to real newlines."""

from app import llm
from app.llm import generate_structured
from app.schemas import DiagnosisProposal


def test_groq_json_newlines_normal_and_double_escaped(monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "")
    monkeypatch.setenv("GROQ_API_KEY", "test-groq-key")
    raw = (
        r'{"root_cause_hypothesis": "a\nb", "evidence": ["e"], "action_type": "drain_traffic", '
        r'"target_service": "postgres-primary", "rationale": "1. X.\\n2. Y.", "rollback_plan": "r", "confidence": 0.5}'
    )
    monkeypatch.setattr(llm, "groq_chat", lambda *a, **k: raw)

    result = generate_structured("p", "s", DiagnosisProposal)

    assert result.root_cause_hypothesis == "a\nb"
    assert result.rationale == "1. X.\n2. Y."
