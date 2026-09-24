"""The Werkzeug debugger and the bind address, verified by behavior.

Role: test (read-only)
Reads: swap_terminal/app.py
Writes: a throwaway swap_terminal.db under the temp path conftest.py sets
Can move funds: no
Mainnet-safe: yes -- Flask.run is replaced with a recorder, so no socket is
        ever bound and no request is ever served.

WHY THESE TESTS RUN THE REAL FILE.

The behavioral-verification section of CLAUDE.md is binding: "never accept 'the
code contains a check for X' as evidence X is enforced." A test that greps
app.py for the string "debug=False" would pass against a file that computes
`debug` correctly and then ignores it, and it would fail against a correct file
that spells the default some other way. So test_defaults_reach_app_run below
executes app.py AS `__main__` with flask.Flask.run replaced by a recorder, and
asserts on the keyword arguments the application actually passed.

Against the file as it stood before 2026-09-24 -- a single line reading
`app.run(debug=True, host="0.0.0.0", port=5000)` -- that test records
debug=True and host="0.0.0.0" and fails on both assertions. That is the check
that the test tests the fix.
"""

import runpy
import sys
from pathlib import Path

import app
import flask
import pytest

APP_PY = Path(__file__).resolve().parent.parent / "swap_terminal" / "app.py"

# The wildcard bind address, named once so it appears as a value this test
# compares against rather than as a bind this test performs. Without the name,
# every literal use trips S104 and would need its own suppression.
ALL_INTERFACES = "0.0.0.0"  # noqa: S104 -- a string constant, not a socket

# The environment variables the run path reads. Cleared before each test so a
# developer's own shell cannot make a default look correct.
RUN_ENV_VARS = ("SWAP_TERMINAL_DEBUG", "SWAP_TERMINAL_HOST", "SWAP_TERMINAL_PORT")


@pytest.fixture(autouse=True)
def _clean_run_env(monkeypatch):
    for name in RUN_ENV_VARS:
        monkeypatch.delenv(name, raising=False)


def _run_app_main(monkeypatch):
    """Execute swap_terminal/app.py as __main__ and return the app.run kwargs.

    Returns the dict of keyword arguments the real file passed to Flask.run,
    plus the text it printed is left to capsys in the caller.
    """
    recorded = {}

    def fake_run(self, *args, **kwargs):
        recorded["args"] = args
        recorded["kwargs"] = kwargs

    monkeypatch.setattr(flask.Flask, "run", fake_run)
    # app.py imports its siblings rootlessly; conftest already put
    # swap_terminal/ on sys.path. run_path re-executes module scope, which
    # rebuilds the app against the temp database conftest configured.
    monkeypatch.setattr(sys, "argv", ["app.py"])
    runpy.run_path(str(APP_PY), run_name="__main__")
    return recorded["kwargs"]


def test_defaults_reach_app_run(monkeypatch, capsys):
    kwargs = _run_app_main(monkeypatch)
    # The two keywords that were the highest-severity line in the repository.
    assert kwargs["debug"] is False
    assert kwargs["host"] == "127.0.0.1"
    assert kwargs["port"] == 5000
    banner = capsys.readouterr().out
    assert "debugger        off" in banner
    assert "warnings        (none)" in banner


def test_debug_is_only_on_when_explicitly_requested(monkeypatch, capsys):
    monkeypatch.setenv("SWAP_TERMINAL_DEBUG", "1")
    kwargs = _run_app_main(monkeypatch)
    assert kwargs["debug"] is True
    # Rule 14: the unsafe posture must not be silent. An operator who typed the
    # variable sees why it matters, in the same block that says they typed it.
    banner = capsys.readouterr().out
    assert "debugger        ON" in banner
    assert "wallet RPC credentials" in banner


def test_host_override_is_honored_and_warned_about(monkeypatch, capsys):
    monkeypatch.setenv("SWAP_TERMINAL_HOST", ALL_INTERFACES)
    kwargs = _run_app_main(monkeypatch)
    assert kwargs["host"] == ALL_INTERFACES
    assert "beyond the local machine" in capsys.readouterr().out


def test_port_override_is_honored(monkeypatch):
    monkeypatch.setenv("SWAP_TERMINAL_PORT", "5051")
    assert _run_app_main(monkeypatch)["port"] == 5051


# --- the decisions on their own, with seeded inputs (rule 10) ----------------


def test_debug_enabled_is_false_for_everything_but_an_explicit_true_value():
    for value in ("", "0", "no", "false", "False", "off", "maybe", "2", " "):
        assert app.debug_enabled({"SWAP_TERMINAL_DEBUG": value}) is False, value
    assert app.debug_enabled({}) is False
    for value in ("1", "true", "TRUE", "yes", "on", " on "):
        assert app.debug_enabled({"SWAP_TERMINAL_DEBUG": value}) is True, value


def test_bind_host_defaults_to_loopback_and_strips_whitespace():
    assert app.bind_host({}) == "127.0.0.1"
    assert app.bind_host({"SWAP_TERMINAL_HOST": "   "}) == "127.0.0.1"
    assert app.bind_host({"SWAP_TERMINAL_HOST": " 10.0.0.4 "}) == "10.0.0.4"


def test_bind_port_raises_on_a_typo_rather_than_silently_using_5000():
    assert app.bind_port({}) == 5000
    assert app.bind_port({"SWAP_TERMINAL_PORT": "8080"}) == 8080
    with pytest.raises(ValueError):
        app.bind_port({"SWAP_TERMINAL_PORT": "80 80"})


def test_exposure_warnings_are_empty_only_for_the_safe_default():
    assert app.exposure_warnings("127.0.0.1", False) == []
    assert app.exposure_warnings("localhost", False) == []
    assert len(app.exposure_warnings("127.0.0.1", True)) == 1
    assert len(app.exposure_warnings(ALL_INTERFACES, False)) == 1
    assert len(app.exposure_warnings(ALL_INTERFACES, True)) == 2
