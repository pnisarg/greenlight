"""A subprocess timeout must degrade to a clean non-zero Run, not crash the gate.

A single slow subprocess (a hung reviewer agent, a stuck test command) used to
propagate subprocess.TimeoutExpired all the way up and kill the whole pipeline
with a traceback. util.run now catches it and returns exit 124.
"""
import pytest

from greenlight.util import GreenlightError, run


def test_timeout_returns_124_without_raising():
    r = run(["sleep", "5"], timeout=0.2)
    assert r.code == 124
    assert r.ok is False
    assert "timed out" in r.err


def test_timeout_with_check_raises_greenlight_error():
    with pytest.raises(GreenlightError) as exc:
        run(["sleep", "5"], timeout=0.2, check=True)
    assert "124" in str(exc.value)


def test_fast_command_unaffected_by_timeout():
    r = run(["true"], timeout=5)
    assert r.code == 0
    assert r.ok is True
