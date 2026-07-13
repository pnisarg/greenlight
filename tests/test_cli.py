import json

import pytest

from greenlight import __version__
from greenlight.cli import build_parser, main


def test_version_flag_prints_version(capsys):
    with pytest.raises(SystemExit) as exc:
        main(["--version"])
    assert exc.value.code == 0
    assert __version__ in capsys.readouterr().out


def test_doctor_reports_version(capsys):
    main(["doctor"])
    assert __version__ in capsys.readouterr().err


def test_doctor_json_reports_tool_availability(monkeypatch, capsys):
    paths = {"git": "/usr/bin/git", "pi": "/usr/local/bin/pi", "gh": None}
    monkeypatch.setattr("greenlight.cli.which", paths.get)

    assert main(["doctor", "--json"]) == 0
    captured = capsys.readouterr()
    report = json.loads(captured.out)

    assert captured.err == ""
    assert report == {
        "ok": True,
        "tools": {
            "git": {"available": True, "path": "/usr/bin/git", "required": True},
            "pi": {"available": True, "path": "/usr/local/bin/pi", "required": True},
            "gh": {"available": False, "path": None, "required": False},
        },
        "version": __version__,
    }


def test_doctor_json_fails_when_required_tool_is_missing(monkeypatch, capsys):
    monkeypatch.setattr("greenlight.cli.which", lambda tool: None if tool == "pi" else f"/bin/{tool}")

    assert main(["doctor", "--json"]) == 1
    report = json.loads(capsys.readouterr().out)

    assert report["ok"] is False
    assert report["tools"]["pi"] == {
        "available": False,
        "path": None,
        "required": True,
    }


def test_parser_has_version_action():
    parser = build_parser()
    actions = {a.dest for a in parser._actions}
    assert "version" in actions
