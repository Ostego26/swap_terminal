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
import re
import sqlite3
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

import pytest
from regtest import daemons, funding_steps, steps
from regtest import operator_panel as decisions
from regtest.daemons import RegtestSetupError
from regtest.harness_runner import HarnessRunner

import supervisor  # isort: skip -- rootless import; see _entry()
import workers.common  # isort: skip -- same
from db import SCHEMA, db_session, dict_factory  # isort: skip -- same
from services.admin_view import overview  # isort: skip -- same
from chains.xrp_rpc_map import CONGRUENT, NO_EQUIVALENT, XRP_ONLY  # isort: skip -- same
from valid_addresses import GRC_PAYOUT  # isort: skip -- a real payout address; see tests/valid_addresses.py


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

    # THE KEY IS TAKEN FROM THE TABLE, not spelled. Renaming reclaim_dry_run ->
    # grc_reclaim_dry_run when the runs became per-chain broke this test, which had nothing to
    # do with renames -- the same "pinned the constant, not the behavior" the funding amounts
    # taught earlier today (rule 8).
    answer, code = entry.start_named_run(_Busy(), {"key": next(iter(decisions.RUNNABLE))})
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
    assert [p.txid for p in funding_steps.payments_to_the_funding_address(rows, "ours")] == [
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
            if method == "getblockcount":
                # payment_rows() reads the tip to size an incremental spend-scan
                # (regtest/operator_panel.scan_depth). A fixed height means "the chain has not
                # moved", which is the case the fast path is about.
                return TIP_HEIGHT
            raise AssertionError(method)

    monkeypatch.setattr(funding_steps, "find_operator_funding",
                        lambda run, key, txid: seen.append(txid) or _Outpoint(txid))
    monkeypatch.setattr(funding_steps, "find_the_spender",
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
    assert kinds["XRP"] == kinds["SOL"] == "foreign"
    for chain in decisions.CHAINS:
        assert chain.note, f"{chain.asset} must say what it is, even when unreachable"


def test_a_chain_with_no_adapter_is_a_RESULT_and_never_an_outage(monkeypatch):
    """Five of the six are expected to be unreachable on an ordinary day.

    The regtest daemons only exist while a harness is running and three have no adapter at all,
    so if any of that could raise, the page would be blank exactly when an operator opened it to
    find out why something was down. `resolve_config` is made to explode here to prove the tab
    survives it.
    """
    foreign = next(c for c in decisions.CHAINS if c.asset == "SOL")
    state = decisions.chain_state(foreign, None)
    assert state["reachable"] is False and state["error"] == ""
    assert "read-only check is the button below" in state["note"]

    def _explodes(asset):
        raise RuntimeError("no connection parameters for you")

    monkeypatch.setattr(funding_steps, "resolve_config", _explodes)
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
    assert set(payload["runnable"]) == {c.asset for c in decisions.CHAINS}, (
        "every chain gets its own run list, including the empty ones -- a tab with no entry "
        "would fall back to another chain's buttons, which is what made six tabs into one"
    )
    # SCOPED TO THE NAV AND THE SCRIPT, not the whole page. "GRC" legitimately appears in the
    # subtitle, which is prose about the gate this process passed at startup; what must not
    # appear is an asset name the navigation or its default selection depends on.
    nav = entry.PAGE.split("<nav", 1)[1].split("</nav>", 1)[0]
    script = entry.PAGE.split("<script>", 1)[1].split("</script>", 1)[0]
    # THE MARKS TABLE IS THE ONE EXCEPTION, and it is named rather than skirted: the coin logos
    # are keyed by asset in the page because they are drawings, not data, and a chain with no
    # mark falls back to a plain disc rather than breaking. What must still come from the server
    # is the tab LIST and the DEFAULT selection, which is what this asserts.
    script = script.split("const MARKS", 1)[0] + script.split("};", 1)[-1]
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


def test_a_SPENT_answer_is_remembered_and_an_UNSPENT_one_is_never_cached(monkeypatch):
    """The asymmetry IS the correctness argument, and it is the whole reason this is safe.

    Measured on the operator's screen 2026-09-28: opening the panel walked 85, 208 and 259
    blocks -- three payments, every one long spent -- and did it again on every reload. Rule 3
    prefers removing work to doing it faster, and remembering "spent by X" removes all of it
    after the first look.

    "Spent by X" is MONOTONE: nothing this harness cares about can unspend an outpoint, and a
    reorganization deep enough to try would take the outpoint with it rather than hand it back
    usable. "Not spent" is NOT monotone -- the very next block can spend it -- so caching that
    would have the panel offering a payment the harness then refuses, which is the exact failure
    the spend scan was written to end. The cache may only ever turn a slow correct answer into a
    fast one.
    """
    walks = []
    spent, fresh = "22" * 32, "33" * 32

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
                return [{"address": "ours", "category": "send", "txid": spent, "confirmations": 9},
                        {"address": "ours", "category": "send", "txid": fresh, "confirmations": 2}]
            if method == "getblockcount":
                # payment_rows() reads the tip to size an incremental spend-scan
                # (regtest/operator_panel.scan_depth). A fixed height means "the chain has not
                # moved", which is the case the fast path is about.
                return TIP_HEIGHT
            raise AssertionError(method)

    monkeypatch.setattr(funding_steps, "find_operator_funding",
                        lambda run, key, txid: _Outpoint(txid))

    def _walk(run, outpoint, max_depth=0):
        walks.append(outpoint.txid)
        return (("what-consumed-it", "SPENT ALREADY") if outpoint.txid == spent
                else (None, "no transaction in the last 3 block(s) spends this outpoint"))

    monkeypatch.setattr(funding_steps, "find_the_spender", _walk)

    cache: dict = {}
    first = decisions.payment_rows(_Run(), _Key(), cache)
    assert walks == [fresh, spent], "the first look walks both"
    assert [r.usable for r in first] == [True, False]

    second = decisions.payment_rows(_Run(), _Key(), cache)
    assert walks == [fresh, spent, fresh], (
        "the SPENT one must not be walked again, and the UNSPENT one must be -- it can become "
        f"spent at any block. walks were {walks}"
    )
    assert [r.usable for r in second] == [True, False], "and the answer is unchanged"
    # THE CACHE'S CONTENTS ARE ASSERTED, not just its effect. Recording the unspent outpoint as
    # None happens to be harmless today, because None reads as falsy at the lookup -- so a
    # mutant that did it SURVIVED the behavioral assertions above. That is an equivalent mutant
    # now and a live defect the moment anyone writes `if key in known_spent`, which is the
    # obvious "improvement" for avoiding a repeated miss. Pinning the contents refuses the whole
    # family rather than the one spelling of it.
    assert list(cache) == [(spent, 1)], (
        f"only SPENT outpoints may be recorded; the cache holds {list(cache)}"
    )

    assert "remembered from an earlier look" in second[1].note, (
        "and it SAYS the answer was remembered rather than re-measured (rule 17: a reader must "
        "be able to tell which they are holding)"
    )


def test_the_cache_is_OPTIONAL_so_every_other_caller_is_unaffected(monkeypatch):
    """`None` means no cache, and nothing is remembered. The harnesses pass nothing."""
    walks = []

    class _Key:
        address = "ours"

    class _Run:
        asset = "GRC"

        def say(self, *a):
            pass

        def node(self, wallet=True):
            return self

        def call(self, method, *params):
            if method == "getblockcount":
                return TIP_HEIGHT
            return [{"address": "ours", "category": "send", "txid": "44" * 32, "confirmations": 9}]

    monkeypatch.setattr(funding_steps, "find_operator_funding", lambda run, key, txid: _Outpoint(txid))
    monkeypatch.setattr(funding_steps, "find_the_spender",
                        lambda run, outpoint, max_depth=0: walks.append(1) or ("x", "SPENT"))

    decisions.payment_rows(_Run(), _Key())
    decisions.payment_rows(_Run(), _Key())
    assert len(walks) == 2, "with no cache, both looks walk"


# ---------------------------------------------------------------------------
# A WEB PAGE THE OPERATOR MERELY VISITS MUST NOT BE ABLE TO SPEND THEIR COIN
# ---------------------------------------------------------------------------


def test_a_POST_from_ANOTHER_ORIGIN_is_refused():
    """THE REAL HOLE IN A LOOPBACK PANEL, and binding 127.0.0.1 does not close it.

    Nothing on the network can reach this port -- but the operator's own BROWSER can, and any
    page they visit can issue requests to 127.0.0.1. Nothing here authenticates a caller, so
    without this check a page on any site could POST /api/run and start a harness that SPENDS
    COIN, and the operator would see only a run they did not start.
    """
    entry = _entry()
    for hostile in ("https://example.com", "http://evil.test:8765", "null",
                    "http://127.0.0.1.attacker.com"):
        refusal = entry.refuse_a_cross_origin_post({"Origin": hostile, "Host": "127.0.0.1:8765"}, 8765)
        assert refusal, f"{hostile} must be refused"
        assert "not this machine" in refusal
        assert "spend coin" in refusal, "and it says WHY, or it reads as a bug to work around"


def test_a_REBOUND_DNS_NAME_is_refused_even_though_it_is_same_origin():
    """An attacker's domain can be made to resolve to 127.0.0.1.

    That makes their page SAME-ORIGIN with this one, so the Origin check above sees its own
    address and waves it through -- the whole point of DNS rebinding. The Host header still
    carries their domain, and that is what gives them away.
    """
    entry = _entry()
    refusal = entry.refuse_a_cross_origin_post(
        {"Origin": "http://rebind.attacker.test", "Host": "rebind.attacker.test:8765"}, 8765)
    assert refusal, "a rebound name must be refused"

    # Same attack with the Origin stripped, which is the shape that defeats an Origin-only check.
    refusal = entry.refuse_a_cross_origin_post({"Host": "rebind.attacker.test:8765"}, 8765)
    assert "DNS-rebinding" in refusal, refusal


def test_the_OPERATORS_OWN_PAGE_is_not_obstructed():
    """A guard that refuses the good case too is an outage, and a test that only checks the
    refusal cannot see it.

    A MISSING Origin is allowed on purpose: curl and these tests send none, and a page-driven
    request always carries one, so the check is aimed at browsers -- which is where the threat
    is. The content-type lock at the call site is what covers the rest.
    """
    entry = _entry()
    for good in ({"Origin": "http://127.0.0.1:8765", "Host": "127.0.0.1:8765"},
                 {"Origin": "http://localhost:8765", "Host": "localhost:8765"},
                 {"Host": "127.0.0.1:8765"},
                 {}):
        assert entry.refuse_a_cross_origin_post(good, 8765) == "", good


# ---------------------------------------------------------------------------
# THE RPC CONSOLE. An allowlist, not a denylist.
# ---------------------------------------------------------------------------


def test_only_READ_ONLY_methods_are_allowed_and_the_refusal_names_the_rule():
    """A denylist is a list of the ways to lose money somebody thought of. This is an allowlist.

    The four groups asserted here are the ones whose absence IS the design: spending, the
    passphrase, stopping the operator's staking daemon, and reading a key back out. A panel that
    could reach any of them would not be a read-only console with exceptions -- it would be a
    wallet with a browser form in front of it.
    """
    for forbidden in ("sendtoaddress", "sendrawtransaction", "signrawtransaction",
                      "walletpassphrase", "walletlock", "encryptwallet", "stop",
                      "dumpprivkey", "dumpwallet", "importprivkey", "getnewaddress",
                      "", None, 42, ["getbalance"]):
        refusal = decisions.refuse_unless_read_only(forbidden)
        assert refusal, f"{forbidden!r} must be refused"

    assert "passphrase never goes through this page" in decisions.refuse_unless_read_only("walletpassphrase")

    for allowed in ("getblockchaininfo", "getbalance", "listunspent", "getrawtransaction"):
        assert decisions.refuse_unless_read_only(allowed) == "", allowed

    # AND NOTHING THAT WRITES IS ON THE LIST, asserted over the LIST rather than over a sample
    # of it -- a write method added later would otherwise slip in unremarked.
    #
    # MATCHED AS A VERB AT THE START, not as a substring anywhere. The first version of this
    # assertion looked for "set" anywhere in the name and rejected `gettxoutsetinfo`, which
    # reads. A check that fires on correct entries is a check somebody edits until it stops
    # firing, and by then it has stopped meaning anything (rule 19).
    writes = ("send", "sign", "wallet", "dump", "import", "encrypt", "create", "stop", "add",
              "set", "generate", "backup", "move")
    for name in decisions.READ_ONLY_RPCS:
        assert not any(name.startswith(verb) for verb in writes), name
    assert "getnewaddress" not in decisions.READ_ONLY_RPCS, (
        "it looks harmless and it WRITES a key into wallet.dat, which a staking-only wallet may "
        "refuse and which changes a file the operator backs up"
    )


def test_a_REFUSED_call_and_a_FAILED_call_read_differently(monkeypatch):
    """One is a boundary this panel holds on purpose; the other is the chain answering.

    Collapsing them would leave an operator unable to tell a policy they can read from a problem
    they must fix -- and the deliberate one would look like a bug worth routing around (rule 14).
    """
    class _Run:
        def node(self, wallet=True):
            return self

        def call(self, method, *args):
            raise RuntimeError("the daemon said -1")

    refused = decisions.call_read_only(_Run(), "sendtoaddress", [])
    assert refused["ok"] is False and refused["refused"] is True

    failed = decisions.call_read_only(_Run(), "getbalance", [])
    assert failed["ok"] is False and failed["refused"] is False
    assert "RuntimeError" in failed["error"], "and the exception TYPE, not just its message"

    class _Works(_Run):
        def call(self, method, *args):
            return {"blocks": 3296406}

    ok = decisions.call_read_only(_Works(), "getblockchaininfo", [])
    assert ok == {"ok": True, "result": {"blocks": 3296406}}


def test_the_rpc_route_refuses_an_asset_it_serves_no_daemon_for():
    """The dropdown is a convenience; both gates are on the server.

    A request naming any other asset or method is refused whatever the page sends, which is what
    makes the page's contents irrelevant to the panel's safety.
    """
    entry = _entry()
    answer, code = entry.answer_an_rpc({"asset": "DOGE", "method": "getbalance"}, {})
    assert code == 403 and answer["refused"] is True

    answer, code = entry.answer_an_rpc("not an object", {})
    assert code == 400


def test_host_of_is_not_fooled_by_a_prefix_or_a_suffix():
    """THE VULNERABILITY THIS FUNCTION EXISTS FOR, live for four minutes on 2026-09-28.

    The first guard asked whether the Origin STARTED WITH "http://127.0.0.1" -- and
    `http://127.0.0.1.attacker.com` starts with exactly that. Anyone who controls any domain
    can register that subdomain and pass a check that looks obviously correct. The mirror-image
    hole defeats an endswith against a suffix, which is why this parses rather than matching at
    either end, and why both shapes are pinned here.
    """
    entry = _entry()
    assert entry.host_of("http://127.0.0.1:8765") == "127.0.0.1"
    assert entry.host_of("127.0.0.1:8765") == "127.0.0.1"
    assert entry.host_of("http://[::1]:8765") == "::1"
    assert entry.host_of("[::1]:8765") == "::1"
    assert entry.host_of("http://localhost") == "localhost"
    assert entry.host_of("http://127.0.0.1.attacker.com") == "127.0.0.1.attacker.com"
    assert entry.host_of("http://evil-localhost") == "evil-localhost"
    assert entry.host_of("http://attacker.com/127.0.0.1") == "attacker.com"


def test_every_chain_has_a_theme_and_a_dark_variant():
    """Several brand colors are unreadable on a dark background -- XRP's near-black is invisible
    on it -- and a theme that is unreadable half the time is worse than none, because the reader
    stops looking at it and the glance-level defense it buys is gone.

    The fallback is deliberately plain rather than a guess: an unthemed chain LOOKS unthemed.
    """
    for chain in decisions.CHAINS:
        theme = decisions.theme_for(chain.asset)
        for key in ("accent", "dark", "glyph", "unit"):
            assert theme.get(key), f"{chain.asset} has no {key}"
        for key in ("accent", "dark"):
            assert theme[key].startswith("#") and len(theme[key]) == 7, f"{chain.asset}.{key}"
    assert decisions.theme_for("DOGE")["glyph"] == "?", "an unthemed chain looks unthemed"


def test_each_chain_offers_ITS_OWN_runs_and_never_another_chains():
    """THE OPERATOR'S REPORT, 2026-09-28: "doesn't matter which tab you click, it's all GRC
    controls too. it doesn't switch per chain."

    It was not a rendering bug. RUNNABLE had no idea which chain each entry belonged to, so
    every tab rendered the whole table -- and a panel with six tabs and one set of controls is a
    panel with one tab and five decorations. Worse: the buttons shown under BTC would have
    spent GRC.

    THE ASSET IS PART OF THE ENTRY rather than inferred from its name. A "grc_" prefix rule
    would work until the first entry that did not follow it, and the failure would be a button
    appearing under the wrong chain, which is the thing being fixed.
    """
    for key, (_what, argv, owner) in decisions.RUNNABLE.items():
        assert owner in {c.asset for c in decisions.CHAINS}, f"{key} claims chain {owner!r}"
        mine = [r["key"] for r in decisions.runs_for(owner)]
        assert key in mine, f"{key} is not offered by its own chain"
        for other in {c.asset for c in decisions.CHAINS} - {owner}:
            assert key not in [r["key"] for r in decisions.runs_for(other)], (
                f"{key} is offered under {other}, and running it would touch {owner}"
            )
        # AND THE ARGV MATCHES THE CHAIN IT CLAIMS. An entry filed under LTC whose command says
        # --chain btc would pass every check above and spend on the wrong chain.
        flat = " ".join(argv).lower()
        if "--chain" in flat:
            assert f"--chain {owner.lower()}" in flat, f"{key} is filed under {owner}: {argv}"


def test_a_chain_with_no_harness_gets_an_empty_list_rather_than_someone_elses():
    """An absent entry is what made the tabs fall back to another chain's buttons.

    Every chain is a key, including the ones with nothing to run, so the page renders "(none)"
    for them instead of whatever it rendered last (rule 14).
    """
    for chain in decisions.CHAINS:
        assert isinstance(decisions.runs_for(chain.asset), list)
    assert decisions.runs_for("DOGE") == [], "an unknown chain offers nothing, not everything"


def test_gridcoin_is_PURPLE_and_the_value_is_gridcoins_own():
    """It was green until the operator said otherwise: a guess presented as a theme, which is
    rule 17's failure wearing a colour.

    #753eef and #3c1b7b are the two gradient stops in src/qt/res/images/gridcoin.svg in the
    Gridcoin wallet's own MIT-licensed source -- measured rather than picked, which is the only
    reason this assertion is worth writing down.
    """
    theme = decisions.theme_for("GRC")
    assert theme["accent"] == "#753eef", "the light stop of Gridcoin's own logo gradient"
    assert theme["dark"] != theme["accent"], "and a lighter variant, readable on a dark page"


def test_the_gridcoin_mark_is_the_REAL_logo_path_and_not_a_typed_letter():
    """Gridcoin's symbol is a G WITH A LINE THROUGH IT, and a font cannot draw one.

    The mark was a hexagon with the letter "G" set in it, which is close enough to look right
    and wrong in the one detail that identifies the currency. The operator said so; the exact
    source was already checked out in this container, which makes "recognisable rather than
    official" the lazy half of that phrase rather than the honest one.

    All three <path> elements of src/qt/res/images/gridcoin.svg are inlined now, with its own
    gradient stops. The assertions are about SHAPE rather than about the bytes: a <text> element
    anywhere in this mark means somebody replaced a drawing with a letter again, and fewer than
    three paths means the stroked G is the one that went missing -- it is the middle path, and
    the one a simplification drops first.
    """
    entry = _entry()
    mark = entry.PAGE.split("GRC: '", 1)[1].split("',", 1)[0]
    assert mark.count("<path") == 3, f"the real logo is three paths, this has {mark.count('<path')}"
    assert "<text" not in mark, "a typed letter cannot draw a G with a line through it"
    assert "#753eef" in mark and "#3c1b7b" in mark, "and it carries Gridcoin's own gradient"
    assert "viewBox=\"0 0 500 500\"" in mark, "at the source's own coordinate system"


def test_every_mark_renders_without_a_network_or_a_file():
    """The page has to render on a machine with no route to the internet, which is exactly when
    an operator opens it.

    That is the same constraint that made the whole page inline, and it is worth an assertion
    because an image URL is the most natural thing in the world to reach for when adding a logo.
    """
    entry = _entry()
    marks = entry.PAGE.split("const MARKS", 1)[1].split("};", 1)[0]
    for forbidden in ("<img", "http://", "https://", "url(http", ".png", ".svg\"", "src="):
        assert forbidden not in marks, f"{forbidden!r} in the marks: this page fetches nothing"
    for asset in ("GRC", "BTC", "LTC", "XRP", "SOL"):
        assert f"{asset}: '<svg" in marks, f"{asset} has no mark"


def test_a_foreign_chain_says_whether_it_is_CONFIGURED_rather_than_just_unprobed(monkeypatch):
    """"this tab does nothing" was the report on a foreign chain, and it was accurate.

    The tab said "no adapter in this panel" and stopped, which is a statement about THIS PANEL
    dressed as a statement about the chain -- and the thing the operator could act on was one
    environment variable away. Gridcoin is the only daemon they keep running; the rest are not
    broken, they are unconfigured, and those read identically until something says so.

    ASKED OF THE ENVIRONMENT, not of a network, so it costs nothing and cannot hang. It answers
    "does the button below have somewhere to connect TO", which is a different question from
    "does that endpoint answer" -- and the tab says which one it answered (rule 17).
    """
    foreign = next(c for c in decisions.CHAINS if c.asset == "XRP")

    monkeypatch.delenv("XRP_RPC_URL", raising=False)
    state = decisions.chain_state(foreign, None)
    assert state["configured"] is False
    assert state["missing_env"] == ["XRP_RPC_URL"], state["missing_env"]

    monkeypatch.setenv("XRP_RPC_URL", "http://127.0.0.1:5005")
    state = decisions.chain_state(foreign, None)
    assert state["configured"] is True and state["missing_env"] == []
    assert state["env"] == ["XRP_RPC_URL"], (
        "and it names what it CHECKED, so a variable renamed in config.py shows up here as a "
        "stale name rather than as a silently wrong answer"
    )


def test_every_foreign_chain_names_the_variables_its_own_entry_point_reads():
    """A chain with no entry in FOREIGN_ENV would report "configured" unconditionally, because
    an empty list of required variables has nothing missing from it.

    That is the shape where a tab reads green on a chain nobody can reach, which is the one
    failure worse than the "does nothing" this replaced.
    """
    for chain in decisions.CHAINS:
        if chain.kind != "foreign":
            continue
        assert decisions.FOREIGN_ENV.get(chain.asset), (
            f"{chain.asset} is foreign and names no environment variable, so it would report "
            f"itself configured whatever the environment holds"
        )


# ---------------------------------------------------------------------------
# THE ON/OFF SWITCH. Not the same decision for all six chains.
# ---------------------------------------------------------------------------


def _tab(asset):
    return next(c for c in decisions.CHAINS if c.asset == asset)


def test_a_regtest_daemon_may_be_started_and_stopped_freely():
    """Throwaway chains whose daemons regtest_htlc_verify.py already starts and stops.

    Nothing is at stake: the coins are minted on demand and the datadir is disposable. This is
    the case where a switch is simply a switch.
    """
    for asset in ("BTC", "LTC"):
        for action in ("start", "stop"):
            assert decisions.refuse_daemon_control(_tab(asset), action) == "", f"{asset} {action}"


def test_STARTING_the_operators_own_daemon_is_refused_because_we_do_not_know_how(monkeypatch):
    """Not caution -- ignorance, stated as such.

    This panel never started that daemon, so it has no binary, no datadir flags and no idea
    whether it runs under a service manager. Inventing a command line for the process that
    stakes the operator's wallet is the guess rule 17 forbids, and "it did not come back up" is
    the worst possible time to discover one.
    """
    monkeypatch.setenv(decisions.MAY_STOP_VARIABLE, "yes")
    refusal = decisions.refuse_daemon_control(_tab("GRC"), "start")
    assert refusal, "arming the STOP must not arm the start"
    assert "never started that daemon" in refusal
    assert "guess" in refusal, "and it says why, or an operator goes looking for a flag"


def test_STOPPING_the_operators_own_daemon_is_armed_OUTSIDE_the_browser(monkeypatch):
    """That daemon is STAKING THEIR WALLET, and this page is unauthenticated behind a loopback
    bind -- so a tab they left open is a tab something else can reach.

    The environment variable means the decision to have the button was made in a shell,
    deliberately, and cannot be made by anything that merely reaches the port. It is not a nag:
    a confirm() in the page defends against a click they did not mean, and this defends against
    a page they did not open. Neither replaces the other.
    """
    monkeypatch.delenv(decisions.MAY_STOP_VARIABLE, raising=False)
    refusal = decisions.refuse_daemon_control(_tab("GRC"), "stop")
    assert refusal and decisions.MAY_STOP_VARIABLE in refusal
    assert "STAKING YOUR WALLET" in refusal

    monkeypatch.setenv(decisions.MAY_STOP_VARIABLE, "yes")
    assert decisions.refuse_daemon_control(_tab("GRC"), "stop") == ""


def test_a_foreign_chains_SWITCH_IS_REFUSED_FOR_A_REASON_ABOUT_THIS_PANEL(monkeypatch):
    """"this panel has no daemon lifecycle for <chain>" was a claim about the CHAIN, and false.

    Both buttons on a foreign tab said it until 2026-09-28, and the operator read it directly
    beneath a tab that had just named the one variable that was unset. That chain has daemons. So
    does rippled, so does a Solana validator. What is true is what is true of GRC one branch up
    -- THIS PANEL DOES NOT KNOW YOUR COMMAND LINE -- and it is true here for a stronger reason:
    nothing in this tree has ever started one. On 2026-09-28 that daemon's name
    appeared in eleven Python files in this tree and a spawn of it in none; regtest/daemons.py
    owns every Popen here and knows bitcoind and litecoind only.

    THE TWO ACTIONS REFUSE FOR DIFFERENT REASONS and must not share a sentence. A start would
    have to invent a command line that chooses WHICH WALLET is opened, which is a custody
    decision. A stop cannot find a process it did not spawn without matching a command line,
    and that pattern hits every daemon of that kind on the host -- rule 13's "prefer a pid file
    to a pgrep pattern", in the case where there is not even a pattern worth having.
    """
    monkeypatch.setenv(decisions.MAY_STOP_VARIABLE, "yes")
    for asset in ("XRP", "SOL"):
        start = decisions.refuse_daemon_control(_tab(asset), "start")
        stop = decisions.refuse_daemon_control(_tab(asset), "stop")
        assert start and stop, f"{asset} must refuse both -- arming the stop conjures no daemon"
        assert start != stop, (
            f"{asset} refuses start and stop for genuinely different reasons and a shared "
            f"sentence teaches neither"
        )
        assert "no daemon lifecycle" not in start + stop, (
            "the claim about the CHAIN, which was never this panel's to make"
        )
        assert "will not START" in start and "will not STOP" in stop
        assert "pid" in stop, "rule 13: the missing handle is the reason, and it is named"


def test_the_four_refusals_are_NOT_interchangeable(monkeypatch):
    """Each sends the operator somewhere different, and a shared "not allowed" sends them
    nowhere.

    One says this panel does not know your GRC command line, one says an environment variable
    arms the GRC stop, one says a foreign start would choose a wallet, one says a foreign stop
    has no pid to aim at. A disabled button with no text teaches none of them,
    which is why the page renders the reason beside every switch it greys out.
    """
    monkeypatch.delenv(decisions.MAY_STOP_VARIABLE, raising=False)
    said = {
        "grc_start": decisions.refuse_daemon_control(_tab("GRC"), "start"),
        "grc_stop": decisions.refuse_daemon_control(_tab("GRC"), "stop"),
        "sol_start": decisions.refuse_daemon_control(_tab("SOL"), "start"),
        "sol_stop": decisions.refuse_daemon_control(_tab("SOL"), "stop"),
    }
    # FOUR NOW, NOT THREE. The foreign start and the foreign stop shared one sentence until
    # 2026-09-28 and the sentence was wrong for both of them; splitting it is what made this
    # count move, and the count is here so a later edit that collapses them back fails.
    assert len(set(said.values())) == 4, said
    assert decisions.refuse_daemon_control(_tab("GRC"), "restart"), "only start and stop exist"


def test_the_switch_route_refuses_before_it_reaches_a_daemon():
    """The gate runs on the server, before any connection, and a request naming a chain this
    panel does not serve is refused whatever the page sends."""
    entry = _entry()
    answer, code = entry.answer_a_daemon_switch({"asset": "DOGE", "action": "stop"}, {})
    assert code == 403 and answer["refused"] is True

    answer, code = entry.answer_a_daemon_switch({"asset": "SOL", "action": "stop"}, {})
    # THE ROUTE GIVES THE DECISION'S OWN SENTENCE, asserted by deriving it rather than by
    # quoting it. This pinned the literal "no daemon lifecycle" and so had to be edited when
    # that sentence was found to be a false claim about the chain -- a test that pins a constant
    # fails on a wording change and passes on a behavior one, which is backwards.
    assert code == 403
    assert answer["error"] == decisions.refuse_daemon_control(_tab("SOL"), "stop")

    answer, code = entry.answer_a_daemon_switch("not an object", {})
    assert code == 400


def test_NO_TAB_HAS_EVER_CARRIED_THE_KIND_TWO_LIVE_BRANCHES_TESTED_FOR():
    """The defect under every fix in this group, 2026-09-28.

    `ChainTab.kind`'s type comment and the block above CHAINS both named a kind `none`, and
    CHAINS has said `foreign` since the tabs were written. Nothing failed, because a comment
    cannot fail -- but two pieces of code were written against the comment rather than against
    the data, and both were dead the moment they were typed:

      main()            skipped building an RPC console on `kind == "none"`, so it never
                        skipped, and printed `no RPC console (KeyError: ...)` once per foreign
                        times at startup -- a Python exception class in an operator's terminal.
      the nav dot       set grey on `d.kind === "none"`, so it never set grey, and painted
                        every foreign chain RED for a probe this panel never makes.

    This test pins the DATA, so the next person to write a branch against a kind finds out here
    whether that kind exists. It is the assertion that would have failed on the day.
    """
    kinds = {c.kind for c in decisions.CHAINS}
    assert "none" not in kinds, (
        "if a `none` kind is ever introduced, the two branches above have to be revisited "
        "together -- they are what this name meant last time"
    )
    assert kinds == {"operator", "regtest", "foreign"}, sorted(kinds)


def test_a_CHAIN_THIS_PANEL_CANNOT_SPEAK_TO_IS_NEVER_RED():
    """Red means "we asked and it did not answer". This panel never asks a foreign chain.

    Until this function existed the page computed `d.reachable ? "yes" : "no"` with its grey
    branch guarded by a kind no tab carries, so every foreign tab was red on a page whose own
    legend says red means the daemon did not answer. That is the stale-green-an-operator-acts-on
    the dot's comment forbids, running the other way: a red dot for a question nobody asked
    sends someone looking for a daemon that may be up and fine.

    AMBER HAD A CSS RULE AND NO WRITER. `nav button[data-up="cfg"]` has been in the stylesheet
    and in the legend under the nav since the tabs were themed, and nothing in the tree ever set
    the value -- so the sentence the operator reads above the tabs described a color the page
    could not produce.
    """
    assert decisions.dot_state({"kind": "foreign", "configured": True}) == decisions.DOT_CONFIGURED
    assert decisions.dot_state({"kind": "foreign", "configured": False}) == decisions.DOT_UNASKED
    assert decisions.DOT_CONFIGURED == "cfg" and decisions.DOT_UNASKED == ""

    # and a chain this panel DOES probe still answers the original question
    assert decisions.dot_state({"kind": "operator", "reachable": True}) == decisions.DOT_ANSWERED
    assert decisions.dot_state({"kind": "regtest", "reachable": False}) == decisions.DOT_SILENT

    page = _entry().PAGE
    # COMMENTS STRIPPED FIRST. The comment at the fix QUOTES the dead branch, because rule 1
    # wants the reason the obvious version was wrong kept next to the code -- so a naive
    # substring check over the whole page fails on the explanation rather than on the defect.
    code = "\n".join(line for line in page.splitlines() if not line.strip().startswith("//"))
    assert 'd.kind === "none"' not in code, "the dead branch, gone rather than left beside the fix"
    assert 'tab.dataset.up = d.dot' in code, "and the page takes the server's answer"
    for value in ("yes", "no", "cfg", ""):
        assert f'nav button[data-up="{value}"]' in page, (
            f"every value dot_state can return needs the CSS rule that colors it -- {value!r} "
            f"has none, so it would render as an uncolored dot with no way to tell"
        )


def test_a_FOREIGN_TAB_IS_NOT_OFFERED_A_CONSOLE_IT_CANNOT_USE():
    """The operator pressed Call on a foreign tab and got REFUSED BY THIS PANEL.

    A dropdown of twenty-five Bitcoin-style methods was rendered for a chain that has none of
    them, and the refusal arrived after the click. Offering a control that cannot work and
    explaining afterwards is rule 14's defect-in-the-output: the page should say so where the
    control would have been.

    AND THE REFUSAL NAMED THE WRONG REASON. It said the chain "has no reachable daemon here",
    which is not established -- the operator's daemon for it may be answering perfectly
    well. What is true is that this console sends one protocol and that chain does not speak it.
    """
    entry = _entry()
    sol = next(c for c in decisions.CHAINS if c.asset == "SOL")
    grc = next(c for c in decisions.CHAINS if c.asset == "GRC")

    refusal = decisions.refuse_an_rpc_console(sol)
    assert refusal and "does not speak" in refusal
    assert "unreachable" not in refusal and "not reachable" not in refusal, (
        "it must not claim anything about whether that daemon is up -- nothing asked it"
    )
    assert decisions.refuse_an_rpc_console(grc) == "", "GRC speaks it, so GRC keeps its console"

    answer, code = entry.answer_an_rpc({"asset": "SOL", "method": "getblockcount"}, {})
    assert code == 403 and answer["refused"] is True
    assert answer["error"] == refusal, (
        "the route gives the same sentence the tab does, from the same function -- two copies "
        "of one refusal is rule 8's bug with a delay on it"
    )

    page = entry.PAGE
    assert 'd.console ? "none" : ""' in page, "and the row is hidden rather than left to refuse"


def test_STARTUP_SAYS_WHY_A_CHAIN_HAS_NO_CONSOLE_IN_WORDS_NOT_IN_A_KeyError():
    """`no RPC console (KeyError: ...)`, printed once per foreign tab, on 2026-09-28.

    Two defects in one line. The condition was knowable before anything was asked of anything
    -- this console speaks a protocol those chains do not -- so the try/except was reached only
    because the skip above it tested a kind that does not exist. And what it printed was a
    Python exception class, for a design decision, in the terminal of an operator deciding
    whether the panel came up correctly (rule 14).

    A CONNECTION FAILURE IS STILL ITS OWN LINE, and deliberately reads differently: one says
    this panel will never have a console here, the other says it could not build one this time.
    """
    entry = _entry()
    # THE INVARIANT MOVED ON 2026-09-30 AND THIS IS THE STRONGER ONE (rule 2: a test pins the
    # stronger invariant rather than the old behavior). It used to read "every foreign tab
    # refuses", which became false when XRP gained a command map and a console. What was
    # actually being protected is that a foreign tab NEVER reaches the bitcoin-style
    # resolve_config path, and that whichever answer it gives is in words -- a refusal that
    # names what to use instead, or a protocol this panel can actually speak. Both are decided
    # before anything is asked of a network; neither may be an exception class.
    for tab in decisions.CHAINS:
        if tab.kind != "foreign":
            continue
        protocol = decisions.console_protocol(tab)
        refusal = decisions.refuse_an_rpc_console(tab)
        assert bool(protocol) != bool(refusal), (
            f"{tab.asset} both has a console protocol ({protocol!r}) and refuses one "
            f"({refusal!r}), or does neither -- exactly one has to be true or the page has no "
            f"answer to give"
        )
        assert protocol != "bitcoin", (
            f"{tab.asset} is foreign and must never be given the bitcoin-style console, which "
            f"is the path that reaches resolve_config"
        )
        assert "KeyError" not in refusal and "Error" not in refusal
        if refusal:
            assert "button under Run" in refusal, (
                "a refusal has to say what to use INSTEAD, or it is a dead end in an operator's "
                "terminal"
            )

    source = Path(entry.__file__).read_text(encoding="utf-8")
    assert 'if tab.kind == "none"' not in source and 'tab.kind == "none"' not in source, (
        "the skip that never skipped"
    )
    assert "decisions.refuse_an_rpc_console(tab)" in source


def test_THE_START_BUTTON_PREPARES_THE_DAEMON_THE_WAY_THE_HARNESS_WOULD(monkeypatch):
    """The defect this button CAUSED on its first day, 2026-09-28.

    The operator pressed Start daemon on the LTC tab and the panel ran

        LTC: starting /usr/local/bin/litecoind -datadir=... -regtest -daemon

    with no -vbparams. The MWEB override lived in a numbered step of the nine-step harness and
    this panel runs no steps, so it started a plain litecoind; `ltc_htlc_verify` then ADOPTED
    that daemon -- correctly, it was answering -- and died at height 288 on bad-txns-vin-empty,
    the exact failure the flag exists to prevent. Two ways to start one daemon, one rule about
    how it must be started, and only one of them had heard of it (rule 8).

    THE ORDER IS THE ASSERTION, not merely that both were called: the override sets a flag on
    the config and -vbparams cannot be given to a process that is already running, which is the
    same reason the adopted-daemon line above it had to stop claiming it had been.
    """
    entry = _entry()
    called: list[str] = []
    monkeypatch.setattr(entry.daemons, "apply_mweb_override",
                        lambda console, config: called.append("override"))
    monkeypatch.setattr(entry.daemons, "start_daemon",
                        lambda console, config: called.append("start") or True)

    class _Run:
        console = None
        config = None

    answer, code = entry.answer_a_daemon_switch({"asset": "LTC", "action": "start"}, {"LTC": _Run()})
    assert code == 200 and answer["ok"] is True
    assert called == ["override", "start"], (
        "the override must be applied BEFORE the spawn -- it sets a startup flag, and afterwards "
        "there is nothing left to give it to"
    )


def test_THE_OVERRIDE_HAS_ONE_IMPLEMENTATION_AND_BOTH_STARTERS_REACH_IT():
    """Rule 8: the merge, and the cull that goes with it (rule 9).

    `steps.apply_mweb_override` held the body until 2026-09-28. It is now a call into
    `daemons.apply_mweb_override`, beside start_daemon, where the other starter can reach it --
    and the two names it used to import for that body, `daemon_help_text` and
    `mweb_override_args`, came off steps.py's import list in the same pass, because a merge
    that leaves the old helpers looking authoritative is rule 9's four-implementations-where-
    there-were-three.
    """
    assert callable(daemons.apply_mweb_override)
    body = Path(steps.__file__).read_text(encoding="utf-8")
    start = body.index("def apply_mweb_override(run: Run)")
    end = body.index("def step_2_daemon(")
    step_body = body[start:end]
    assert "daemons.apply_mweb_override(run.console, run.config)" in step_body
    assert "mweb_override_args(" not in step_body, "the second copy, gone rather than left beside it"
    assert "daemon_help_text" not in body, "and the import it needed went with it"


def test_A_PAGE_DRAW_DOES_NOT_RESTATE_THE_SAME_FIVE_LINES_FOREVER():
    """Forty copies of five lines, from the operator's terminal on 2026-09-28.

    Serving the GRC tab walks the funding payments and says one line per payment. Six tab
    loads and a few daemon switches later their terminal held forty copies of the same five
    `found the operator's funding at ...` lines, in five-line bursts, with the BTC and LTC
    harness output they were actually watching buried between them. Nothing was wrong and
    nothing was slow -- the same five facts were restated every time a page was drawn.

    AND IT IS NOT A SILENCE, which rule 14 forbids outright. Every line is printed in full the
    first time. The first suppression says so, once, and names where the table actually lives,
    so an operator who notices the lines stopped is told they stopped on purpose. A line never
    said before is never suppressed, so a new payment or a refusal still arrives at once.
    """
    class _Recorder:
        def __init__(self):
            self.lines = []

        def say(self, line):
            self.lines.append(line)

        def check(self, *args):
            self.lines.append(("check", *args))

    under = _Recorder()
    console = decisions.SaysEachLineOnce(under)

    console.say("found payment A")
    console.say("found payment B")
    assert under.lines == ["found payment A", "found payment B"], "both are new, both are said"

    console.say("found payment A")
    assert len(under.lines) == 3 and under.lines[2] == decisions.SaysEachLineOnce.NOTICE, (
        "the first suppression explains itself rather than going quiet"
    )
    console.say("found payment B")
    assert len(under.lines) == 3, "and it explains itself ONCE, not per line"

    console.say("a NEW payment nobody has seen")
    assert under.lines[-1] == "a NEW payment nobody has seen", (
        "a line never said before is never suppressed -- that is the whole safety property"
    )

    # EVERYTHING ELSE IS THE REAL CONSOLE'S. `check` tallies into the counts the panel's startup
    # gate reads, and a wrapper that swallowed one would change what that gate saw.
    console.check("a check", "got", "expected", "OK")
    assert under.lines[-1] == ("check", "a check", "got", "expected", "OK")


def test_THE_STARTUP_GATE_IS_NOT_THE_THING_BEING_QUIETED():
    """The wrap happens AFTER the network gate, never before.

    Those OK lines -- the liveness probe and the three-way test-network assertion -- are the
    ones that say this panel is pointed at testnet and not at the operator's staking wallet.
    They run once, they are never repeated, and wrapping them would buy nothing and risk the
    one output on this screen that must never be conditional.
    """
    source = Path(_entry().__file__).read_text(encoding="utf-8")
    wrapped = source.index("run.console = decisions.SaysEachLineOnce(console)")
    gated = source.index("funding_steps.assert_test_network(run)")
    assert gated < wrapped, "the gate prints through the real console, unwrapped"


def test_THE_TERMINAL_SAYS_SPENT_OR_USABLE_AND_NOT_ONLY_THE_PAGE():
    """The misreading this fixes was mine, off the operator's paste, 2026-09-28.

    The panel's page was never wrong -- payment_rows() marked all five payments SPENT in the
    funding table. What the TERMINAL showed was only the candidate line from
    find_operator_funding(), which is emitted before anything asks whether the output is still
    unspent, and which used to read "found the operator's funding at <txid>:1 worth 1.00000000
    GRC". Five long-spent outputs printed as five found fundings, I read it as the answer, and
    told the operator 12.12 GRC was available. The next run of grc_htlc_verify established that
    every one of the five was spent -- at 168, 193, 278, 418 and 479 blocks deep -- in about
    eight seconds of block walking.

    So the verdict is said where it is read. Both words are asserted, because a line that
    appears only for the bad case trains a reader to skim for noise, and the quiet failure here
    looks exactly like the healthy one (rule 14).
    """
    said: list[str] = []
    spent_txid, live_txid = "aa" * 32, "bb" * 32
    spender = "cc" * 32

    class _Run:
        asset = "GRC"

        def say(self, line):
            said.append(line)

        def node(self, wallet=True):
            return self

        def call(self, method, *params):
            if method == "listtransactions":
                return [
                    {"address": "ours", "category": "send", "txid": spent_txid, "confirmations": 9},
                    {"address": "ours", "category": "send", "txid": live_txid, "confirmations": 2},
                ]
            if method == "getblockcount":
                # payment_rows() reads the tip to size an incremental spend-scan
                # (regtest/operator_panel.scan_depth). A fixed height means "the chain has not
                # moved", which is the case the fast path is about.
                return TIP_HEIGHT
            raise AssertionError(method)

    class _Key:
        address = "ours"

    run = _Run()
    outpoints = {
        spent_txid: funding_steps.chain.Outpoint(txid=spent_txid, vout=1, value_satoshis=150_000_000),
        live_txid: funding_steps.chain.Outpoint(txid=live_txid, vout=0, value_satoshis=16_000_000),
    }
    original_find = funding_steps.find_operator_funding
    original_spender = funding_steps.find_the_spender
    try:
        funding_steps.find_operator_funding = lambda r, k, txid: outpoints[txid]
        funding_steps.find_the_spender = lambda r, o, max_depth=None: (
            (spender, "SPENT ALREADY") if o.txid == spent_txid else (None, "no spender found")
        )
        rows = decisions.payment_rows(run, _Key())
    finally:
        funding_steps.find_operator_funding = original_find
        funding_steps.find_the_spender = original_spender

    # NEWEST FIRST is payments_to_the_funding_address()'s order, so the 2-confirmation one
    # leads. Asserted as the order rather than as a set, because "which one would a run pick"
    # is the question the table exists to answer.
    assert [r.usable for r in rows] == [True, False]
    terminal = "\n".join(said)
    assert "SPENT by " + spender[:16] in terminal, "the spent one says so in the terminal"
    assert "USABLE" in terminal, (
        "and so does the healthy one -- a line that only appears when something is wrong "
        "teaches the reader to skim, and this is the case I skimmed"
    )
    assert "1.50000000 GRC" in terminal and "0.16000000 GRC" in terminal, (
        "with the value, because whether there is ENOUGH is the next question"
    )


def test_A_CANDIDATE_OUTPUT_IS_NOT_ANNOUNCED_AS_THE_OPERATORS_FUNDING():
    """`find_operator_funding` says candidate, because that is what it has at that line.

    It locates the output paying the funding address and returns; nothing in it asks whether
    that output is still unspent. In the nine-step harness the verdict follows two lines later
    and the sequence reads correctly -- but the line has to be true on its own, because the
    panel is a second reader that does not print those two lines.
    """
    source = Path(funding_steps.__file__).read_text(encoding="utf-8")
    start = source.index("def find_operator_funding(")
    end = source.index("def ", source.index("raise RegtestSetupError", start))
    body = source[start:end]
    code = "\n".join(line for line in body.splitlines() if not line.strip().startswith("#"))
    assert "candidate funding output" in code
    assert "NOT yet checked" in code
    assert "found the operator's funding" not in code, (
        "the old wording, gone rather than left beside the fix"
    )


# ---------------------------------------------------------------------------
# THE SWAPPER'S OWN WORKERS
#
# Added 2026-09-30 when the operator asked for ONE gui that controls the swapper.
# The panel already had daemon buttons; these are the three supervisor workers,
# and they are a different case in every respect that matters -- see
# regtest/operator_panel.refuse_worker_control()'s comment. What is tested here is
# the part that makes a button safe rather than the part that makes it work: a name
# this tree does not own is refused by NAME, and a stop is PROVEN.
# ---------------------------------------------------------------------------


TIP_HEIGHT = 3_298_078


class _Console:
    """The one thing chain_state() wants from a console: somewhere to say things."""

    def say(self, *_args, **_kwargs):
        pass



SLEEPER = "import time; time.sleep(120)"


@pytest.fixture
def one_fake_worker(monkeypatch):
    """Replace the worker table with one harmless process that sleeps.

    THE TABLE IS PATCHED, NOT THE SPAWN, so start_worker() and stop_worker() run for
    real -- a real fork, a real pid file, a real SIGTERM, a real absence poll. A test
    that stubbed those two would assert that this panel calls two functions, which is
    not the property worth holding; the property is that the process is gone.

    A sleeper rather than a real worker because a real one opens chain adapters and
    exits immediately on a host with none configured, and stop_worker() would then
    report `not-running` -- which passes without ever exercising the reaper. This is
    the shape of the mutation-check failure that keeps happening here: a test that
    passes for the wrong reason.
    """
    table = {"sleeper": [sys.executable, "-c", SLEEPER]}
    monkeypatch.setattr(supervisor, "worker_commands", lambda *_a, **_k: table)
    return table


def test_a_worker_this_tree_does_not_own_is_refused_BY_NAME(one_fake_worker):
    """The allowlist is supervisor's own table, never a copy in the panel.

    MUTATION: have refuse_worker_control() accept any string. Nothing in the tree
    would then stop a request naming a worker with no argv, and this panel would be
    one KeyError away from inventing a command line -- which is the guess rule 17
    forbids, on the surface that can spend coin.
    """
    assert decisions.refuse_worker_control("sleeper", "start") == "", (
        "the patched table's own name was refused, so the allowlist is not being read from it"
    )
    for name in ("payout_worker", "", "sleeper; rm -rf /", None, 7):
        refusal = decisions.refuse_worker_control(name, "start")
        assert refusal, f"{name!r} was accepted while the table holds only {sorted(one_fake_worker)}"
        assert "sleeper" in refusal, "the refusal does not say what this panel DOES know"


def test_only_start_and_stop_are_actions():
    for action in ("restart", "kill", "", None, "START"):
        assert "is not start or stop" in decisions.refuse_worker_control("payout_worker", action)


def test_the_panel_starts_a_worker_and_PROVES_the_stop(tmp_path, one_fake_worker):
    """The round trip, with a real process at the end of it (rule 13).

    Four assertions in one test on purpose: started, alive, stopped, and ABSENT. The
    fourth is the one that matters and the first three are what make it mean
    anything -- asserting an absence without first proving a presence is a test that
    passes against a panel that starts nothing at all.
    """
    entry = _entry()

    answer, code = entry.answer_a_worker_switch({"worker": "sleeper", "action": "start"}, tmp_path)
    assert code == 200 and answer["ok"] is True, answer
    assert answer["result"]["outcome"] == "started", answer
    pid = answer["result"]["pid"]
    assert supervisor.process_alive(pid), "the panel reported `started` for a process that is not there"

    # A SECOND START IS NOT A QUIET SUCCESS. rule 13 calls "skipped" printed beside
    # "ok" a defect in the output, so supervisor's own word is passed through and the
    # page renders it.
    again, _ = entry.answer_a_worker_switch({"worker": "sleeper", "action": "start"}, tmp_path)
    assert again["result"]["outcome"] == "already-running", again
    assert again["result"]["pid"] == pid, "a second start forked a second process"

    stopped, code = entry.answer_a_worker_switch({"worker": "sleeper", "action": "stop"}, tmp_path)
    assert code == 200 and stopped["ok"] is True, stopped
    assert stopped["result"]["outcome"] == "stopped", stopped
    # THE ASSERTION IS THE ABSENCE, not the exit code of the kill, and not the word
    # supervisor returned. This is the line the whole button is judged by.
    assert not supervisor.process_alive(pid), (
        "the panel said `stopped` and the process is still running -- which is the orphan rule 13 "
        "is about, now reachable from a browser"
    )
    assert not supervisor.pid_file(tmp_path, "sleeper").exists(), "the pid file outlived the process"


def test_a_stop_that_could_not_prove_it_is_NOT_reported_as_ok(monkeypatch, tmp_path, one_fake_worker):
    """`failed` means the operator asked for something and did not get it.

    supervisor.stop_worker() returns `failed` when a process is still there after
    SIGTERM, the grace period and SIGKILL. That must not render like `stopped`: a
    stop that cannot prove it worked is not a stop, and a page that says ok to both
    is the "skipped beside success" output defect (rule 13).

    MUTATION: return ok=True for every outcome. This fails, and nothing else does --
    which is the point of asserting on the flag rather than on the word.
    """
    entry = _entry()
    monkeypatch.setattr(
        supervisor, "stop_worker",
        lambda *_a, **_k: {"worker": "sleeper", "outcome": "failed", "pid": 1, "signals": ["SIGTERM", "SIGKILL"]},
    )
    answer, code = entry.answer_a_worker_switch({"worker": "sleeper", "action": "stop"}, tmp_path)
    assert code == 200
    assert answer["ok"] is False, "a stop that could not prove the process is gone was reported as ok"
    assert answer["result"]["outcome"] == "failed"


def test_the_swapper_region_reports_a_broken_database_rather_than_dying(monkeypatch):
    """This panel is opened when something is already wrong (rule 14).

    A swap-state region that raises takes the funding view, the daemon switches and
    the run controls down with it -- on the one screen an operator opens to find out
    why. The failure belongs IN the return value, where the caller can tell it from
    an answer.

    MUTATION: drop the try/except. The panel then 500s on a database it cannot open
    and says nothing about which.
    """
    entry = _entry()
    monkeypatch.setattr(workers.common, "get_config_dict",
                        lambda *_a, **_k: {"DB_PATH": "/nonexistent/dir/swap.db"})
    payload = entry.swapper_payload()
    assert payload["ok"] is False, "an unopenable database was reported as a successful read"
    assert payload.get("error"), "ok=False with no reason is not a report"


def test_the_swapper_region_is_admin_views_overview_and_holds_no_query_of_its_own():
    """Rule 8, as a property of the file rather than of a rendering.

    /admin and this panel are now two renderers of ONE assembly. A SELECT, a
    threshold or a freshness rule appearing in operator_panel.py would be the second
    implementation -- and it would agree on the day it was written and drift after,
    which is the whole failure rule 8 opens with.
    """
    source = Path(_entry().__file__).read_text(encoding="utf-8")
    body = source[source.index("def swapper_payload("):source.index("def answer_a_worker_switch(")]
    code = "\n".join(line for line in body.splitlines() if not line.strip().startswith("#"))
    assert "overview(" in code, "the panel no longer calls the shared assembly"
    for spelling in ("SELECT", "FROM swaps", "STALE_AFTER", "cursor", "execute("):
        assert spelling not in code, (
            f"{spelling!r} appears in the panel's swapper region, which means it has started "
            f"reading the database itself instead of through services/admin_view.overview()"
        )



def test_every_overview_FIELD_the_panel_renders_actually_EXISTS(tmp_path):
    """The defect this exists for was on the operator's screen, twice, in one paste.

    2026-09-30, the panel's swapper region rendered

        0=[object Object] 1=[object Object] 2=[object Object]
        GRC  confirmed undefined  available undefined

    because the script read `status_counts` as a mapping (it is a GROUP BY's list of
    {status, swaps} rows) and read r.confirmed / r.available / r.freshness on an
    inventory row whose real keys are hot_confirmed / hot_available / fresh.

    NOTHING FAILED. That is the whole reason this test exists: a second renderer
    guessing at a payload's field names is rule 8 at its least visible -- no
    exception, no empty region, no test, just the word `undefined` where a balance
    goes. templates/admin.html has the names right and this file had them wrong, and
    the two are supposed to be one payload rendered twice.

    SO THIS WALKS THE SCRIPT and checks each field against a REAL overview() on a
    real empty schema. It is deliberately not a list of expected names -- a list
    would be a third copy, and it would go stale the same way.

    MUTATION: rename any read back (o.status_counts -> o.statusCounts,
    r.hot_available -> r.available) and this names the field and the object it was
    read from.
    """
    db_path = tmp_path / "panel.db"
    conn = sqlite3.connect(db_path)
    conn.row_factory = dict_factory
    conn.executescript(SCHEMA)
    # REAL ROWS, so every list comes back non-empty and its shape is the one the
    # renderer will actually see. The first version of this test derived the shape
    # from PRAGMA table_info instead, and it was wrong in the way that matters here:
    # inventory_rows() attaches a COMPUTED `fresh` reading that no table has a column
    # for, so a column list would have called the panel's correct read a defect.
    # Seed the rows, run the real function, assert on what comes out
    # (swap_terminal/CLAUDE.md: verify by row-level behavioral outcome).
    now = datetime.now(UTC).isoformat()
    conn.execute(
        "INSERT INTO wallet_inventory (asset, hot_confirmed, hot_reserved, hot_available, updated_at)"
        " VALUES (?,?,?,?,?)", ("GRC", 1.5, 0.25, 1.25, now),
    )
    conn.execute(
        "INSERT INTO quotes (id, from_asset, to_asset, input_amount, quoted_rate, fee_bps,"
        " network_fee_reserve, output_amount_estimate, expires_at, created_at)"
        " VALUES (?,?,?,?,?,?,?,?,?,?)", ("q_f", "XRP", "GRC", 1.0, 90.0, 150, 0.01, 88.0, now, now),
    )
    conn.execute(
        "INSERT INTO swaps (id, quote_id, from_asset, to_asset, deposit_address, payout_address,"
        " expected_input_amount, quoted_rate, fee_bps, network_fee_reserve, output_amount_estimate,"
        " status, min_confirmations, created_at, updated_at, expires_at)"
        " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        ("s_field00000000aa", "q_f", "XRP", "GRC", "rDeposit", GRC_PAYOUT, 1.0, 90.0, 150, 0.01,
         88.0, "completed", 6, now, now, now),
    )
    conn.execute(
        "INSERT INTO payouts (swap_id, asset, amount, destination_address, status, created_at)"
        " VALUES (?,?,?,?,?,?)",
        ("s_field00000000aa", "GRC", 88.0, GRC_PAYOUT, "broadcast", now),
    )
    conn.commit()
    conn.close()

    # THE REAL CONFIG with only the path redirected. A minimal dict here raised
    # KeyError on ALLOWED_PAIRS, which is the same class of mistake this test is
    # about: a hand-built stand-in for a real mapping, missing a key its reader
    # needs. get_config_dict() is what the panel itself passes.
    config = dict(workers.common.get_config_dict())
    config["DB_PATH"] = str(db_path)
    with db_session(str(db_path)) as db:
        payload = overview(db, config, {}, run_dir=tmp_path)

    source = Path(_entry().__file__).read_text(encoding="utf-8")
    script = source[source.index("async function loadSwapper("):source.index('$("checkall").onclick')]

    # `const o = d.overview`, so every o.<name> is a top-level key of the payload.
    top = set(re.findall(r"\bo\.([a-zA-Z_]\w*)", script))
    assert top, "the walk found no field reads at all, so it is proving nothing"
    missing = sorted(name for name in top if name not in payload)
    assert not missing, (
        f"the panel renders o.{', o.'.join(missing)}, which services/admin_view.overview() does "
        f"not return. It returns {sorted(payload)}. A field that does not exist renders as the "
        f"word `undefined` and fails nothing"
    )

    # THE ROW FIELDS, PER LIST. Each map callback in the script names its parameter
    # after the list it walks, one identifier per list, precisely so a field read can
    # be attributed here. It could not be at first: `r` was the parameter for
    # inventory rows, payout rows AND pricing rows, so this walk could check only
    # `w` -- and the hole was found by mutating r.hot_available and watching the test
    # pass. A check that cannot attribute a read cannot verify it.
    # `workers` IS NOT WALKED HERE ANY MORE. The worker switches moved out of the swapper
    # region into the daemon-controls pane on 2026-09-30, where they render from
    # /api/controls rather than from overview() -- so loadSwapper() no longer reads that list,
    # and the walk below covers what it does read. The controls payload gets its own walk in
    # test_every_CONTROLS_FIELD_the_panel_renders_actually_EXISTS.
    per_list = {
        "status_counts": "sc",
        "inventory": "iv",
        "payouts": "po",
    }
    for key, identifier in per_list.items():
        names = set(re.findall(rf"\b{identifier}\.([a-zA-Z_]\w*)", script))
        assert names, (
            f"no {identifier}.<field> read was found for {key}, so either the renderer stopped "
            f"reading that list or its callback parameter was renamed and this table is stale"
        )
        rows = payload[key]
        assert rows, (
            f"{key} came back empty from a seeded database, so its field names were NOT checked "
            f"and this test passed without proving anything"
        )
        missing_fields = sorted(name for name in names if name not in rows[0])
        assert not missing_fields, (
            f"the panel renders {key} row field(s) {missing_fields}, which are not on the row. "
            f"The row has {sorted(rows[0])}"
        )


# ---------------------------------------------------------------------------
# THE SPEND-SCAN'S DEPTH
#
# 2026-09-30, from the operator's own terminal, and it is why they pressed Ctrl-C:
#
#   GRC: scanning blocks 3298076 down to 3296986 for anything that spends 34fcaa81…
#   GRC: scanned 100 block(s) ... scanned 1700 block(s), now at height 3296378
#
# 1092 blocks, one getblock each, for the ONE payment that came back usable -- repeated
# on every page draw, with two walks overlapping against the same daemon because the
# funding view and the chain tab both fetch. The seven spent ones were cached after the
# first look; the eighth could not be, because "not spent" is not monotone.
#
# What IS monotone is the pair ("unspent", the height it was established at), and that
# is what scan_depth() turns into work removed rather than work done faster (rule 3).
# ---------------------------------------------------------------------------


def test_scan_depth_examines_only_the_blocks_that_have_ARRIVED():
    """The decision, with seeded numbers. Four cases and one of them is the fast path."""
    full = 1092

    # Never looked: the whole depth, and no claim about what was skipped.
    assert decisions.scan_depth(None, 3_298_078, full) == (full, "")

    # The tip has not moved. ZERO blocks, and that is the correct answer rather than a
    # skipped check -- a spend after height H must appear in a block after H, and there
    # is no such block yet.
    depth, why = decisions.scan_depth(3_298_078, 3_298_078, full)
    assert depth == 0, "an unchanged tip still cost a walk"
    assert "0 block(s) since height 3298078" in why

    # Seven blocks have arrived: seven blocks are examined, not 1092.
    depth, why = decisions.scan_depth(3_298_078, 3_298_085, full)
    assert depth == 7
    assert "7 block(s) since height 3298078" in why
    assert "cannot appear in a block that already exists" in why, (
        "the sentence does not say WHY the rest may be skipped, so a reader cannot check the "
        "reasoning the speed-up rests on"
    )


def test_scan_depth_walks_the_FULL_depth_when_it_cannot_be_sure():
    """Three ways to be unsure, and every one of them costs the full walk.

    MUTATION: return `tip - previous` unconditionally. The reorganization case then returns
    a NEGATIVE depth, and a tip that could not be read returns a TypeError or a depth
    computed from None -- either way the scan silently stops covering the chain, and the
    symptom is a payment reported usable that the harness refuses.
    """
    full = 1092
    # The tip could not be read. A depth computed from a height nobody read is a confident
    # number derived from a failure (rule 17).
    assert decisions.scan_depth(3_298_078, None, full) == (full, "")
    # The tip went BACKWARD: a reorganization, or a different daemon. The blocks examined
    # last time may not be the blocks that exist.
    assert decisions.scan_depth(3_298_078, 3_298_000, full) == (full, "")
    # And never-looked, again, because it is the same answer for a different reason.
    assert decisions.scan_depth(None, None, full) == (full, "")


def test_an_unspent_output_is_RE_EXAMINED_but_not_RE_WALKED(monkeypatch):
    """The behavioral half: the walk still happens, and it costs nothing when nothing changed.

    The older test beside this one asserts the unspent outpoint IS walked again, which is
    the safety property and is still true. It cannot tell a 1092-block walk from a
    zero-block one, which is the entire defect -- so this asserts the DEPTH, which is the
    thing that was costing the operator their daemon.

    MUTATION: drop the unspent_as_of write. Every look then passes the full depth, this
    fails on the second call's depth, and nothing else in the suite notices.
    """
    depths = []
    unspent = "aa" * 32

    class _Key:
        address = "ours"

    class _Run:
        asset = "GRC"

        def say(self, *a):
            pass

        def node(self, wallet=True):
            return self

        def call(self, method, *params):
            if method == "getblockcount":
                return tip["now"]
            if method == "listtransactions":
                return [{"address": "ours", "category": "send", "txid": unspent, "confirmations": 5}]
            raise AssertionError(method)

    tip = {"now": TIP_HEIGHT}
    monkeypatch.setattr(funding_steps, "find_operator_funding", lambda run, key, txid: _Outpoint(txid))

    def _walk(run, outpoint, max_depth=0):
        depths.append(max_depth)
        return (None, f"no transaction in the last {max_depth} block(s) spends this outpoint")

    monkeypatch.setattr(funding_steps, "find_the_spender", _walk)

    spent_cache: dict = {}
    watermark: dict = {}
    first = decisions.payment_rows(_Run(), _Key(), spent_cache, watermark)
    assert [r.usable for r in first] == [True]
    assert depths == [6], f"the first look must walk the full depth (confirmations + 1), got {depths}"
    assert watermark == {(unspent, 1): TIP_HEIGHT}, (
        f"the tip at which it was established unspent was not recorded: {watermark}"
    )
    assert spent_cache == {}, "an unspent outpoint must never reach the SPENT cache"

    # The tip has not moved: nothing can have spent it, and it costs nothing to say so.
    decisions.payment_rows(_Run(), _Key(), spent_cache, watermark)
    assert depths == [6, 0], f"an unchanged tip cost another walk: {depths}"

    # Four blocks arrive. Four blocks are examined.
    tip["now"] = TIP_HEIGHT + 4
    rows = decisions.payment_rows(_Run(), _Key(), spent_cache, watermark)
    assert depths == [6, 0, 4], f"the incremental walk was not sized to the new blocks: {depths}"
    assert watermark == {(unspent, 1): TIP_HEIGHT + 4}, "the watermark did not advance"
    assert "since height" in rows[0].note, (
        "the row does not say the answer rests on an earlier full walk, so a reader cannot tell "
        "it from a fresh one (rule 17)"
    )


def test_a_WATERMARK_IS_DELETED_the_moment_the_output_is_found_spent(monkeypatch):
    """A statement that would be wrong if it outlived its branch (rule 9).

    Once a spend is found, "unspent as of height H" is not stale -- it is false. Leaving it
    behind is a row in a dict that contradicts the row beside it, and the next reader has to
    decide which to trust.
    """
    txid = "bb" * 32

    class _Key:
        address = "ours"

    class _Run:
        asset = "GRC"

        def say(self, *a):
            pass

        def node(self, wallet=True):
            return self

        def call(self, method, *params):
            if method == "getblockcount":
                return tip["now"]
            if method == "listtransactions":
                return [{"address": "ours", "category": "send", "txid": txid, "confirmations": 5}]
            raise AssertionError(method)

    tip = {"now": TIP_HEIGHT}
    monkeypatch.setattr(funding_steps, "find_operator_funding", lambda run, key, txid_: _Outpoint(txid_))
    monkeypatch.setattr(funding_steps, "find_the_spender",
                        lambda run, outpoint, max_depth=0: (None, "nothing spends it yet"))

    spent_cache: dict = {}
    watermark: dict = {}
    decisions.payment_rows(_Run(), _Key(), spent_cache, watermark)
    assert watermark, "precondition: it was recorded as unspent"

    # Now a block spends it.
    tip["now"] = TIP_HEIGHT + 1
    monkeypatch.setattr(funding_steps, "find_the_spender",
                        lambda run, outpoint, max_depth=0: ("what-consumed-it", "SPENT"))
    rows = decisions.payment_rows(_Run(), _Key(), spent_cache, watermark)
    assert [r.usable for r in rows] == [False]
    assert spent_cache == {(txid, 1): "what-consumed-it"}
    assert watermark == {}, (
        f"the unspent watermark survived the spend, so the memory now says both things: {watermark}"
    )


def test_A_TABS_CONSOLE_OFFERS_ONLY_WHAT_THAT_TAB_CAN_ANSWER():
    """The dropdown on the operator's screen, 2026-09-30, offering 11 impossible methods.

    The XRP tab listed all 25 bitcoin names -- getdifficulty, listunspent, decodescript,
    getrawmempool and seven more that chains/xrp_rpc_map records as having NO XRP Ledger
    equivalent -- and none of the five XRPL-only reads that tab is the only place to make.
    Every one of those eleven would have been refused by the server, correctly, after
    the operator picked it.

    TWO CAUSES, BOTH MINE, from the commit that added the map. The dropdown was filled
    ONCE in tick() from a global `rpcs` list, so no tab influenced it; and I published
    the per-tab list under the key `methods`, which on a bitcoin-family tab already
    means probe_methods()' capability report. A name collision and a stale global, and
    the visible symptom of both is a control that can only refuse -- which the comment
    above loadChain() in that same file already calls a defect.

    MUTATION: fill the dropdown from READ_ONLY_RPCS again. The XRP assertions below
    fail; nothing else does.
    """
    entry = _entry()
    source = Path(entry.__file__).read_text(encoding="utf-8")

    # The option list comes from the per-tab field, and the global writer is gone.
    assert 'd.console_methods' in source, "the dropdown is not filled from the tab's own list"
    assert 'd.rpcs' not in source and '"rpcs"' not in source, (
        "the global method list still exists; it has one writer and no reader, which is how it "
        "came to be filling every tab with one vocabulary"
    )

    console = _Console()
    for tab in decisions.CHAINS:
        if tab.kind != "foreign":
            continue
        state = decisions.chain_state(tab, console)
        offered = set(state["console_methods"])
        protocol = state["protocol"]
        # `methods` keeps its OTHER meaning and must not carry the console's list.
        assert state["methods"] == [], (
            f"{tab.asset} publishes its console list under `methods`, which is the capability "
            f"report's key on every bitcoin-family tab"
        )
        if protocol == "xrpl":
            assert offered == set(CONGRUENT) | set(XRP_ONLY), (
                f"the XRP console offers {sorted(offered)}, not the command map's own names"
            )
            impossible = offered & set(NO_EQUIVALENT)
            assert not impossible, (
                f"the XRP console offers {sorted(impossible)}, which chains/xrp_rpc_map records "
                f"as having NO equivalent. Every one would be refused after the operator picked it"
            )
            assert set(XRP_ONLY) <= offered, (
                f"the XRP console omits {sorted(set(XRP_ONLY) - offered)} -- reads that tab is "
                f"the only place to make"
            )
        else:
            assert offered == set(), f"{tab.asset} has no console and offered {sorted(offered)}"


def test_every_CONTROLS_FIELD_the_panel_renders_actually_EXISTS():
    """The same walk, for the daemon-controls pane's own payload.

    THE SWAPPER REGION'S VERSION OF THIS TEST EXISTS BECAUSE I SHIPPED `[object Object]` AND
    `undefined` to the operator's screen by guessing at field names. The controls pane is a
    second renderer of a second payload written the same afternoon, so it gets the same guard
    rather than waiting for the same paste.

    MUTATION: rename any read (row.consequence -> row.note, r.state -> r.status). This names
    the field and the shape it is missing from.
    """
    entry = _entry()
    source = Path(entry.__file__).read_text(encoding="utf-8")
    script = source[source.index("async function loadControls("):source.index("// THE SWAPPER REGION.")]

    payload = entry.controls_payload()
    rows = payload["controls"]
    assert rows, "controls_payload() returned no row, so this test is proving nothing"

    # `r` walks the list building the subtabs; `row` is the selected one.
    for identifier in ("r", "row"):
        names = set(re.findall(rf"\b{identifier}\.([a-zA-Z_]\w*)", script))
        assert names, f"no {identifier}.<field> read was found, so this table is stale"
        for name in sorted(names):
            # `classList`/`dataset` are DOM, not payload: only reads on a payload row count.
            if name in {"classList", "dataset", "toUpperCase", "map", "join", "find", "some"}:
                continue
            assert all(name in candidate for candidate in rows), (
                f"the controls pane renders {identifier}.{name!r}, which is not on every row. "
                f"A row has {sorted(rows[0])}"
            )

    # THE TWO REFUSALS ARE READ BY A COMPUTED KEY (row[action + "_refusal"]), which no regex
    # can attribute -- so they are named here explicitly. Both must exist on every row, because
    # an absent refusal reads as falsy and ENABLES a button that should be off.
    for candidate in rows:
        for field in ("start_refusal", "stop_refusal"):
            assert field in candidate, (
                f"{candidate['id']} carries no {field}; an absent refusal reads as falsy in the "
                f"page and would ENABLE a switch that the server refuses"
            )
            assert isinstance(candidate[field], str)


def test_the_controls_pane_holds_no_POLICY_of_its_own():
    """Rule 10, as a property of the file: the refusals are the server's, not a ternary here.

    Six named refusals live in decisions.refuse_daemon_control() and
    decisions.refuse_worker_control(), which tests call with seeded inputs. A seventh statement
    of the same rules in JavaScript would be the one nothing can check -- and it would agree on
    the day it was written (rule 8).

    So the pane may branch on `kind` for exactly two things: which POST body shape to send, and
    whether a live state exists to show. It may not decide whether a button is allowed.
    """
    source = Path(_entry().__file__).read_text(encoding="utf-8")
    script = source[source.index("async function loadControls("):source.index("// THE SWAPPER REGION.")]
    code = "\n".join(line for line in script.splitlines() if not line.strip().startswith("//"))

    # The refusal text is READ, never composed.
    assert '[action + "_refusal"]' in code, "the pane no longer reads the server's refusals"
    for invented in ("MAY_STOP", "staking", "pid file", "will not START", "will not STOP"):
        assert invented not in code, (
            f"{invented!r} appears in the controls pane's script, which means a refusal is being "
            f"decided or reworded in the browser instead of read from the server"
        )
    # And the route comes from the row, so a new process kind cannot need a branch here.
    assert "row.route" in code, "the POST target is hardcoded rather than carried on the row"
