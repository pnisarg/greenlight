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


def test_parser_has_version_action():
    parser = build_parser()
    actions = {a.dest for a in parser._actions}
    assert "version" in actions
