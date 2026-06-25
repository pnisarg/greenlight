import json

from greenlight.agent import AgentResult, _last_assistant_text


def _jsonl(*events) -> str:
    return "\n".join(json.dumps(e) for e in events)


def test_last_assistant_text_from_agent_end():
    out = _jsonl(
        {"type": "session"},
        {
            "type": "agent_end",
            "messages": [
                {"role": "user", "content": [{"type": "text", "text": "hi"}]},
                {"role": "assistant", "content": [{"type": "text", "text": "the answer"}]},
            ],
        },
    )
    assert _last_assistant_text(out) == "the answer"


def test_last_assistant_text_falls_back_to_turn_end():
    out = _jsonl(
        {"type": "turn_end", "message": {"role": "assistant",
         "content": [{"type": "text", "text": "partial"}]}},
    )
    assert _last_assistant_text(out) == "partial"


def test_result_json_from_fence():
    r = AgentResult(text='prose\n```json\n{"findings": [], "summary": "ok"}\n```\nmore', code=0)
    assert r.json() == {"findings": [], "summary": "ok"}


def test_result_json_bare_array():
    r = AgentResult(text='[{"a": 1}]', code=0)
    assert r.json() == [{"a": 1}]


def test_result_json_embedded_object():
    r = AgentResult(text='here it is {"x": 5} done', code=0)
    assert r.json() == {"x": 5}


def test_result_json_none_when_unparseable():
    r = AgentResult(text="no json here", code=0)
    assert r.json() is None
