from greenlight.steps.types import Finding


def test_blocks_threshold():
    err = Finding("error", "a.py", 1, "boom")
    warn = Finding("warning", "a.py", 2, "meh")
    info = Finding("info", "a.py", 3, "fyi")
    assert err.blocks("warning")
    assert warn.blocks("warning")
    assert not info.blocks("warning")
    assert not warn.blocks("error")
    assert err.blocks("error")


def test_render_includes_location_and_reviewer():
    f = Finding("error", "api/x.py", 12, "bad", reviewer="security")
    out = f.render()
    assert "[security]" in out
    assert "api/x.py:12" in out
    assert "ERROR" in out
