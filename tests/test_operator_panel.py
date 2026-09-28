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
import json
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


# ---------------------------------------------------------------------------
# THE TABS. One per chain, including the ones this panel cannot reach.
# ---------------------------------------------------------------------------


def test_every_chain_gets_a_tab_including_the_ones_with_no_adapter():
    """A missing tab reads as "that chain does not exist here". Saying so beats omitting it.

    routes/admin.py's pair table already made this argument: disabled pairs are listed rather
    than hidden, "because 'is XRP on?' is a question that otherwise gets answered by reading
    source". The same applies to a nav bar -- and the three `kind` values are the honest part,
    because the three sorts of chain are reached in genuinely different ways.
    """
    kinds = {c.asset: c.kind for c in decisions.CHAINS}
    assert kinds["GRC"] == "operator", "the operator runs it; this panel only reads it"
    assert kinds["BTC"] == kinds["LTC"] == "regtest"
    assert kinds["XMR"] == kinds["XRP"] == kinds["SOL"] == "none"
    for chain in decisions.CHAINS:
        assert chain.note, f"{chain.asset} must say what it is, even when unreachable"


def test_a_chain_with_no_adapter_is_a_RESULT_and_never_an_outage(monkeypatch):
    """Five of the six are expected to be unreachable on an ordinary day.

    The regtest daemons only exist while a harness is running and three have no adapter at all,
    so if any of that could raise, the page would be blank exactly when an operator opened it to
    find out why something was down. `resolve_config` is made to explode here to prove the tab
    survives it.
    """
    none_tab = next(c for c in decisions.CHAINS if c.asset == "XMR")
    state = decisions.chain_state(none_tab, None)
    assert state["reachable"] is False and state["error"] == ""
    assert "no adapter in this panel" in state["note"]

    def _explodes(asset):
        raise RuntimeError("no connection parameters for you")

    monkeypatch.setattr(adaptor_steps, "resolve_config", _explodes)
    btc = next(c for c in decisions.CHAINS if c.asset == "BTC")
    state = decisions.chain_state(btc, None)
    assert state["reachable"] is False
    assert "RuntimeError" in state["error"], "and it names the exception TYPE, not just a message"


def test_an_unknown_asset_is_a_NAMED_REFUSAL_rather_than_a_404():
    """The browser only asks for an asset the page listed, so a 404 means they disagree.

    And a 404 reaches the operator as a fetch failure with no text -- the exact shape of the
    "Failed to fetch" that cost an evening. A named refusal in the tab's own body puts the
    disagreement somewhere it can be read.
    """
    entry = _entry()
    state = entry.chain_payload("DOGE", None)
    assert state["reachable"] is False
    assert "'DOGE' is not a chain this panel knows about" in state["note"]


def test_the_nav_is_built_from_the_SERVERS_list_and_not_hard_coded_in_the_page():
    """A chain in decisions.CHAINS but not in the page would exist in one half of one file.

    That is rule 8's duplicate with a delay on it, written in HTML -- and the drift would be
    invisible, because a nav bar missing one button looks exactly like a nav bar.
    """
    entry = _entry()
    payload = entry.state_payload(None, HarnessRunner())
    assert [c["asset"] for c in payload["chains"]] == [c.asset for c in decisions.CHAINS]
    # SCOPED TO THE NAV AND THE SCRIPT, not the whole page. "GRC" legitimately appears in the
    # subtitle, which is prose about the gate this process passed at startup; what must not
    # appear is an asset name the navigation or its default selection depends on.
    nav = entry.PAGE.split("<nav", 1)[1].split("</nav>", 1)[0]
    script = entry.PAGE.split("<script>", 1)[1].split("</script>", 1)[0]
    for chain in decisions.CHAINS:
        assert chain.asset not in nav, f"{chain.asset} is baked into the nav element"
        assert f'"{chain.asset}"' not in script, (
            f"{chain.asset} is a literal in the page's script -- the tab list AND the default "
            f"selection must both come from /api/state, or a chain added to decisions.CHAINS "
            f"exists in one half of this file and not the other"
        )


# ---------------------------------------------------------------------------
# A ROUTE THAT DIES CLOSES THE CONNECTION, AND THE BROWSER CAN ONLY SAY "Failed to fetch"
# ---------------------------------------------------------------------------


def test_a_route_that_RAISES_answers_500_with_the_route_and_the_exception_type():
    """THE DEFECT, 2026-09-28: "could not reach the panel: TypeError: Failed to fetch".

    That sentence is the browser's, and it is all a browser CAN say -- a handler that raises
    inside BaseHTTPRequestHandler never writes a status line, so the connection closes with no
    response and every reason for it stays on the server. The one thing the operator needed,
    which route and which exception, was the one thing that could not reach them.

    The exception TYPE is asserted as well as the message because "RPCError" and
    "AttributeError" ask for completely different reactions, and a bare message often names
    neither.
    """
    entry = _entry()

    def _explodes(path, *rest):
        raise ValueError("the daemon said something unexpected")

    body, content_type, code = entry.guarded(_explodes, "/api/funding")
    assert code == 500 and content_type == "application/json"
    answer = json.loads(body)
    assert "/api/funding failed on the server" in answer["error"]
    assert "ValueError" in answer["error"], "the exception TYPE, not just its message"
    assert "unexpected" in answer["error"]
    assert answer["rows"] == [] and answer["methods"] == [], (
        "and the shape the page expects, so rendering it does not throw a SECOND error"
    )


def test_a_POST_with_a_broken_body_is_refused_without_reaching_the_allowlist():
    """The POST routes are a function of a path and bytes, callable without a socket."""
    entry = _entry()
    runner = HarnessRunner()
    body, _, code = entry.answer_a_post("/api/run", b"{not json", runner)
    assert code == 400 and b"not JSON" in body

    body, _, code = entry.answer_a_post("/api/nope", b"{}", runner)
    assert code == 404

    body, _, code = entry.answer_a_post("/api/stop", b"{}", runner)
    assert code == 200 and b"nothing was running" in body
