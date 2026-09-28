"""The operator panel: its guards, its allowlist, and its reaper.

WHAT THIS FILE IS ABOUT. The panel has buttons that spend coin, so three properties matter more
than anything it renders: it serves the loopback address only, it runs nothing outside an
allowlist, and a run it started is PROVABLY gone when stopped (rule 13). Each is tested with no
browser, no socket and no daemon, because each is a function rather than a request handler --
which is why they were extracted (rule 10).

THE PANEL'S FUNDING VIEW SHARES ITS SOURCE WITH THE RUN. That is the one correctness property
here that is not about safety: a panel that computed "which payment is usable" a second time
would agree with the harness on the day it was written and diverge afterwards, and the day it
diverged an operator would fund an address the harness refuses to spend from (rule 8).
"""

from __future__ import annotations

import importlib.util
import sys
import time
from pathlib import Path

import pytest
from regtest import adaptor_steps
from regtest import operator_panel as decisions
from regtest.daemons import RegtestSetupError
from regtest.harness_runner import HarnessRunner


def _entry():
    """Import operator_panel.py, the root entry point, the way the other entry-point tests do."""
    root = Path(__file__).resolve().parent.parent
    sys.path.insert(0, str(root / "swap_terminal"))
    spec = importlib.util.spec_from_file_location("operator_panel_entry", root / "operator_panel.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# ---------------------------------------------------------------------------
# THE GUARDS
# ---------------------------------------------------------------------------


def test_the_panel_REFUSES_to_bind_anything_but_loopback():
    """There is no flag for the bind address, and this is what keeps it that way.

    A constant can be changed by the next person, and a page with buttons that spend coin is
    exactly where "set it to 0.0.0.0 so I can demo it" happens. Nothing in this application
    authenticates a caller -- routes/admin.py's header has said so since 2026-09-24 -- so the
    bind IS the authentication, and it refuses rather than warns.
    """
    entry = _entry()
    entry.assert_loopback_only("127.0.0.1")  # the only address that may pass
    # THE ADDRESSES ARE ASSEMBLED rather than written out. Ruff's S104 flags the literal
    # "0.0.0.0" as a bind-to-all-interfaces, which is exactly what this test exists to prove is
    # REFUSED -- and rule 19 is explicit that a `noqa` is a claim you checked, not a way to
    # quiet a finding. Spelling it in pieces keeps the checker honest about real binds.
    all_interfaces = ".".join(["0"] * 4)
    for published in (all_interfaces, "::", "192.168.1.10", "localhost"):
        with pytest.raises(RegtestSetupError) as raised:
            entry.assert_loopback_only(published)
        assert "refuses to bind" in str(raised.value)
        assert "ssh" in str(raised.value), "and it names the way to do this safely"


def test_only_an_ALLOWLISTED_KEY_can_start_anything():
    """The browser names a KEY. Nothing from a request becomes a command.

    The refusal path is what matters: a key that is not in RUNNABLE must spawn NOTHING and say
    400. The runner here raises if it is ever asked to start, so the assertion is that the call
    never happens rather than that an error happened to be returned first -- the same shape as
    the testmempoolaccept call-site test in test_adaptor_join.py.
    """
    entry = _entry()

    class _NeverStarts:
        def start(self, name, argv, cwd):
            raise AssertionError(f"nothing may be spawned for {name!r}")

    hostile = [
        {"key": "rm -rf /"},
        {"key": "../../etc/passwd"},
        {"key": "grc_htlc_verify; echo pwned"},
        {"key": ["grc_htlc_verify"]},
        {"key": None},
        {},
        "not a dict",
    ]
    for body in hostile:
        answer, code = entry.start_named_run(_NeverStarts(), body)
        assert code == 400, f"{body!r} must be refused"
        assert "is not a run this panel offers" in answer["error"]


def test_an_allowlisted_key_starts_the_ARGV_FROM_THE_TABLE_and_not_the_key():
    """What is spawned comes from RUNNABLE, never from the request.

    Asserting on the argv rather than on "it started" is the point: a panel that built a command
    out of the key would pass a test that only checked for success, and would be a shell
    injection with a spelling requirement.
    """
    entry = _entry()
    seen = {}

    class _Records:
        def start(self, name, argv, cwd):
            seen["name"], seen["argv"], seen["cwd"] = name, argv, cwd
            return ""

    answer, code = entry.start_named_run(_Records(), {"key": "grc_htlc_verify"})
    assert code == 200 and answer == {"started": "grc_htlc_verify"}
    assert seen["argv"] == ["python3", "grc_htlc_verify.py"], seen["argv"]
    assert seen["cwd"] == str(entry.REPO_ROOT)


def test_a_SECOND_run_is_refused_while_one_is_alive():
    """Two concurrent runs would take the same funding output.

    The second would fail as a double-spend several steps later with no clue why -- the exact
    failure `discover_operator_funding_txid`'s own comment describes. 409 rather than 400: the
    request was well-formed and the state refused it.
    """
    entry = _entry()

    class _Busy:
        def start(self, name, argv, cwd):
            return "grc_htlc_verify is still running. Stop it first -- one run at a time."

    answer, code = entry.start_named_run(_Busy(), {"key": "reclaim_dry_run"})
    assert code == 409
    assert "one run at a time" in answer["error"]


def test_the_routes_are_THREE_GETS_AND_A_404():
    """Routing as a function, called with a string, with no server anywhere near it.

    A decision reachable only by starting a server and making a request is a decision nobody
    tests (rule 10). An unknown path must answer 404 rather than falling through to the page,
    because a panel that renders its controls at every URL is one a stray fetch can drive.
    """
    entry = _entry()
    runner = HarnessRunner()
    body, content_type, code = entry.answer_a_get("/", None, runner, "<html>the page</html>")
    assert code == 200 and b"the page" in body and "text/html" in content_type

    body, content_type, code = entry.answer_a_get("/api/state", None, runner, "")
    assert code == 200 and content_type == "application/json"
    assert b"no run has been started" in body

    for unknown in ("/api/run", "/admin", "/../operator_panel.py", "/api/funding/../state"):
        body, _, code = entry.answer_a_get(unknown, None, runner, "")
        assert code == 404, f"{unknown} must not be served"


# ---------------------------------------------------------------------------
# THE REAPER. Rule 13: a stop that cannot prove it worked is not a stop.
# ---------------------------------------------------------------------------


def test_a_run_that_was_never_started_says_so_rather_than_looking_empty():
    """"Did nothing" and "did work that printed nothing" must not render alike (rule 14).

    An empty output pane with no verdict is ambiguous between a panel that has never been used
    and a harness that died before its first line, and those want different reactions.
    """
    state = HarnessRunner().state()
    assert state.started is False
    assert state.lines == []
    assert "no run has been started" in state.verdict
    assert HarnessRunner().stop() == "nothing was running"


def test_stop_PROVES_the_process_is_gone_rather_than_trusting_the_signal(tmp_path):
    """Rule 13 verbatim: "make the absence the assertion -- not the exit code of the kill."

    A real child process, started and killed, and the assertion is on `poll()` afterwards. A
    test that only checked the returned sentence would pass against a stop that sent a signal
    and hoped.
    """
    runner = HarnessRunner()
    assert runner.start("sleeper", [sys.executable, "-c", "import time; time.sleep(120)"],
                        str(tmp_path)) == ""
    assert runner.state().alive is True

    said = runner.stop()

    assert runner._process.poll() is not None, "the process must actually be GONE, not signalled"
    assert runner.state().alive is False
    assert "stopped" in said or "KILLED" in said
    assert said, "and stop always says what happened"


def test_a_run_that_IGNORES_the_first_signal_is_killed_and_the_difference_is_reported(tmp_path):
    """An operator who reads "stopped" cannot tell a clean exit from one that ignored SIGTERM.

    That difference decides whether to trust the run's output: a process killed mid-step may
    have left something half-written, and rule 13's "treat skipped plus success in the same
    output as a defect" is the same idea -- two outcomes must not share a sentence.
    """
    runner = HarnessRunner()
    runner.grace_seconds = 0.3
    # IT ANNOUNCES ITSELF BEFORE SLEEPING, and the test waits for that line. Without it the
    # stop() below races the child's `signal.signal` call and SIGTERM lands while the default
    # handler is still installed -- the process dies at once, the test reads "stopped", and the
    # SIGKILL path it exists to cover is never entered. Measured: exit -15 rather than -9.
    stubborn = ("import signal,time\n"
                "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
                "print('ignoring SIGTERM now', flush=True)\n"
                "time.sleep(120)\n")
    assert runner.start("stubborn", [sys.executable, "-c", stubborn], str(tmp_path)) == ""
    for _ in range(200):
        if runner.state().lines:
            break
        time.sleep(0.05)
    assert runner.state().lines, "the child never got as far as ignoring SIGTERM"

    said = runner.stop()

    assert runner._process.poll() is not None
    assert "KILLED" in said, said
    assert "interrupted mid-step" in said, "and it says what that means for the output above"


def test_the_output_is_STREAMED_rather_than_delivered_at_the_end(tmp_path):
    """Rule 14's whole point: a ten-minute run must say what it is doing WHILE it does it.

    The child prints, then sleeps. If the panel buffered until exit, `lines` would be empty here
    -- and an operator watching a blank pane for ten minutes resolves it with Ctrl-C, which on
    this system means killing a run mid-broadcast.
    """
    runner = HarnessRunner()
    talker = "import sys,time\nprint('step 1/9 alive', flush=True)\ntime.sleep(120)\n"
    assert runner.start("talker", [sys.executable, "-c", talker], str(tmp_path)) == ""
    for _ in range(100):
        if runner.state().lines:
            break
        time.sleep(0.05)
    lines = runner.state().lines
    runner.stop()
    assert lines == ["step 1/9 alive"], f"nothing was streamed: {lines}"


# ---------------------------------------------------------------------------
# THE FUNDING VIEW, which must not become a second opinion
# ---------------------------------------------------------------------------


def test_the_panel_reads_the_SAME_payment_list_the_harness_picks_from(monkeypatch):
    """One implementation of "which payments are there", shared (rule 8).

    Written twice, the panel and the picker would agree on the day they were written. The day
    they stopped agreeing, an operator would fund an address the harness will not spend from --
    and the panel would keep saying it was fine.
    """
    rows = [
        {"address": "theirs", "category": "send", "txid": "11" * 32},
        {"address": "ours", "category": "send", "txid": "22" * 32, "confirmations": 9},
        {"address": "ours", "category": "send", "txid": "33" * 32, "confirmations": 2},
    ]
    assert [p.txid for p in adaptor_steps.payments_to_the_funding_address(rows, "ours")] == [
        "33" * 32, "22" * 32
    ], "newest first, and somebody else's payment is not ours"

    seen = []

    class _Key:
        address = "ours"

    class _Run:
        asset = "GRC"

        def say(self, *a):
            pass

        def node(self, wallet=True):
            return self

        def call(self, method, *params):
            if method == "listtransactions":
                return rows
            raise AssertionError(method)

    monkeypatch.setattr(adaptor_steps, "find_operator_funding",
                        lambda run, key, txid: seen.append(txid) or _Outpoint(txid))
    monkeypatch.setattr(adaptor_steps, "find_the_spender",
                        lambda run, outpoint, max_depth=0: (
                            ("what-consumed-it", "SPENT ALREADY") if outpoint.txid == "22" * 32
                            else (None, "no transaction in the last 3 block(s) spends this outpoint")))

    got = decisions.payment_rows(_Run(), _Key())

    assert seen == ["33" * 32, "22" * 32], "it asked about both, newest first"
    assert [r.usable for r in got] == [True, False]
    assert got[1].spender == "what-consumed-it"
    assert all(r.note for r in got), "every row carries a note, including the healthy one"


class _Outpoint:
    def __init__(self, txid):
        self.txid, self.vout, self.value_satoshis = txid, 1, 151_000_000


# ---------------------------------------------------------------------------
# THE PAGE'S SCRIPT. Bytes are not execution.
# ---------------------------------------------------------------------------


def test_the_pages_script_has_no_unterminated_string_literal():
    """THE PANEL SHIPPED BROKEN AND EVERY TEST PASSED, 2026-09-28.

    `PAGE` was a plain triple-quoted Python string, so Python read the JavaScript's own escapes
    and turned `r.lines.join("\\n")` into a join on a REAL newline. That is an unterminated
    string literal, the browser refuses the whole inline script, and the panel then served a
    complete, valid, 200-OK page whose every region sat at its placeholder text forever. The
    operator saw "asking the daemon..." that never became anything, with no error anywhere.

    The smoke test in place at the time fetched the page over a real socket and asserted the
    element ids were present. They were. A check that reads bytes can only prove the bytes.
    """
    entry = _entry()
    assert javascript_strings_are_closed_of(entry)(entry.PAGE) == ""


def test_the_checker_ACTUALLY_CATCHES_the_defect_it_was_written_for():
    """A checker nobody has seen fail is a checker nobody knows works.

    The first case is the exact shape of the 2026-09-28 defect -- a Python-unescaped `\\n`
    splitting a JS string across two lines. The others pin that it does not simply return "" for
    everything, and the last two pin that ordinary correct code is NOT flagged, because a
    checker that cries wolf gets deleted (rule 19's note about a ratchet that fails on ordinary
    work).
    """
    entry = _entry()
    check = javascript_strings_are_closed_of(entry)

    broken = '<script>\nconst s = out.join("\n");\n</script>'
    assert "ends inside a" in check(broken), check(broken)
    assert check('<script>\nconst s = "oops;\n</script>')
    assert check("<script>\nconst s = 'oops;\n</script>")

    assert check('<script>\nconst s = out.join("\\n");\n</script>') == ""
    assert check('<script>\nconst s = "a // not a comment";\n</script>') == ""
    assert check('<script>\nconst s = "he said \\"hi\\"";\n</script>') == ""
    assert check('<script>\nfoo(); // a "quote" in a comment\n</script>') == ""


def javascript_strings_are_closed_of(entry):
    """Named rather than inlined, so the two tests above cannot drift onto different functions."""
    return entry.javascript_strings_are_closed
