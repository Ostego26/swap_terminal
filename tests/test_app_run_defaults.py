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
import config as config_module
import flask
import pytest
from conftest import RPC_FIXTURE_AUTH, RPC_FIXTURE_USER
from workers.common import endpoint_lines

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
    """The safe default warns about nothing; every unsafe setting warns.

    THE COUNTS CHANGED ON 2026-09-26 AND THE ASSERTIONS CHANGED SHAPE WITH THEM.
    This test used to assert `len(...) == 1` for a non-loopback bind and `== 2`
    for a non-loopback bind with the debugger on. routes/admin.py added a second
    warning for a non-loopback bind, because /admin is a different exposure in
    kind from "the API is reachable" -- so the numbers are now 2 and 3.

    Asserting on the CONTENT rather than only the count is the stronger
    invariant, and is why this is not simply a count bumped by one: what matters
    is that the debugger warning appears when the debugger is on, that the
    exposure warning appears when the bind is not loopback, that the admin
    surface is named BY PATH when it becomes reachable, and that a loopback bind
    never mentions it. A count cannot tell those apart, and a count is exactly
    what would keep passing if a future edit replaced the admin sentence with a
    duplicate of the one above it.
    """
    assert app.exposure_warnings("127.0.0.1", False) == []
    assert app.exposure_warnings("localhost", False) == []
    assert app.exposure_warnings("::1", False) == []

    debug_only = app.exposure_warnings("127.0.0.1", True)
    assert len(debug_only) == 1
    assert "SWAP_TERMINAL_DEBUG" in debug_only[0]
    # A loopback bind must NOT warn about /admin: it is not reachable off-box,
    # so a warning there would be the log that cries wolf.
    assert not any("/admin" in warning for warning in debug_only)

    exposed = app.exposure_warnings(ALL_INTERFACES, False)
    assert len(exposed) == 2
    assert any(ALL_INTERFACES in warning and "authenticates" in warning for warning in exposed)
    admin_warnings = [warning for warning in exposed if "/admin" in warning]
    assert len(admin_warnings) == 1
    assert "NO authentication" in admin_warnings[0]
    assert "read-only" in admin_warnings[0]

    both = app.exposure_warnings(ALL_INTERFACES, True)
    assert len(both) == 3
    assert sum("SWAP_TERMINAL_DEBUG" in warning for warning in both) == 1
    assert sum("/admin" in warning for warning in both) == 1


# --- the worker banner must not name an adapter that does not exist -----------

def test_an_unconfigured_chain_is_named_not_shown_as_port_zero(monkeypatch):
    """A regression from 2026-09-26, introduced by that same day's own change.

    Making BTC/LTC/GRC default to UNCONFIGURED_PORT was the fix for the terminal
    polling a mainnet wallet. But this banner interpolated the port directly, so it
    then printed `rpc=127.0.0.1:0` — rendering an unconfigured chain as a
    configured one, three lines above three chains correctly marked "not
    configured". Before the change a port always had a real value and
    interpolating it was safe.

    Worse than cosmetic: chains/registry.py SKIPS a port-0 chain, so the banner was
    naming adapters that do not exist. Rule 14's "make did-nothing look different
    from did-work", and rule 16's "a wrong comment is a bug" applied to output.
    """
    for variable in ("BTC_RPC_PORT", "LTC_RPC_PORT", "GRC_RPC_PORT"):
        monkeypatch.delenv(variable, raising=False)

    lines = endpoint_lines()
    text = "\n".join(lines)

    assert ":0 " not in text and not text.endswith(":0"), f"a port of 0 must never be printed:\n{text}"
    for asset in ("BTC", "LTC", "GRC"):
        assert any(line.startswith(f"  {asset}  not configured") for line in lines), (
            f"{asset} is unconfigured and must say so:\n{text}"
        )


def test_a_configured_chain_says_which_network_the_port_belongs_to():
    """Rule 14: say what the number MEANS next to the number.

    A bare `rpc=127.0.0.1:15715` requires the reader to know Gridcoin's port
    table, and this is a worker about to watch for real deposits — "which chain"
    is the number that matters most. Both verdicts asserted, because a version
    that said "test chain" unconditionally would pass a test for one of them.
    """
    # ALL THREE SETTINGS, not just the port. Since 2026-09-26 a Bitcoin-derived
    # chain counts as configured only when its RPC user and password are set too
    # (chains/registry.missing_settings), because chains/base.py authenticates with
    # auth=(user, password) and has no cookie path, so an empty pair is a
    # guaranteed 401. Setting the port alone leaves the chain unconfigured, and this
    # test would then be asserting the banner's UNCONFIGURED line -- which the test
    # directly above already covers. Not credentials: endpoint_lines() opens no
    # socket, and it never prints either value (see its docstring).
    entry = config_module.Config.RPC["GRC"]
    original = {key: entry[key] for key in ("port", "user", "password")}
    try:
        entry["user"] = RPC_FIXTURE_USER
        entry["password"] = RPC_FIXTURE_AUTH

        entry["port"] = 25715
        assert any("test chain (mainnet is 15715)" in line for line in endpoint_lines())

        entry["port"] = 15715
        assert any("MAINNET, REAL MONEY" in line for line in endpoint_lines())
    finally:
        entry.update(original)
