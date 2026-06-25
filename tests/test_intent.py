from greenlight.cli import _read_intent
from greenlight.steps import intent as intent_step


class _Args:
    def __init__(self, intent=None, intent_file=None):
        self.intent = intent
        self.intent_file = intent_file


def test_read_intent_inline():
    assert _read_intent(_Args(intent="do the thing")) == "do the thing"


def test_read_intent_from_file(tmp_path):
    p = tmp_path / "intent.txt"
    p.write_text("multi\nline\nintent\n")
    assert _read_intent(_Args(intent_file=str(p))) == "multi\nline\nintent"


def test_read_intent_from_stdin(monkeypatch):
    import io
    import sys

    monkeypatch.setattr(sys, "stdin", io.StringIO("piped intent\n"))
    assert _read_intent(_Args(intent_file="-")) == "piped intent"


def test_read_intent_none():
    assert _read_intent(_Args()) is None


def test_supplied_intent_used_verbatim():
    # No agent call should happen when intent is supplied; pass None to prove it.
    out = intent_step.capture(
        agent=None, work_dir=".", base="x", head="y",
        supplied="  author wrote this  ",
    )
    assert out == "author wrote this"
    assert not out.startswith(intent_step.FALLBACK_PREFIX)


def test_fallback_is_marked(monkeypatch):
    # When no intent is supplied, the diff-reconstructed intent is prefixed so
    # downstream surfaces can flag it as not author-authored.
    class FakeAgent:
        def run(self, *a, **k):
            from greenlight.agent import AgentResult
            return AgentResult(text="reconstructed summary", code=0)

    monkeypatch.setattr(intent_step.gitx, "git",
                        lambda *a, **k: type("R", (), {"out": ""})())
    monkeypatch.setattr(intent_step.gitx, "changed_files", lambda *a, **k: ["a.py"])
    out = intent_step.capture(FakeAgent(), ".", "base", "head", supplied=None)
    assert out.startswith(intent_step.FALLBACK_PREFIX)
    assert "reconstructed summary" in out
