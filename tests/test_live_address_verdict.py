"""The address field's in-situ verdict, through the REAL route against the REAL app.

Role: test
Reads: services/address_check.py, routes/atm.py through the app's test client,
       static/script.js and templates/_atm_payout_address.html as text
Writes: a temporary SQLite database under pytest's tmp_path
Can move funds: no
Mainnet-safe: yes -- NO CHAIN AND NO NETWORK. Every adapter here is a recorder
       defined in this file. The one test that needs a real transport failure
       raises the exception object directly rather than opening a socket.

=============================================================================
WHAT IS BEING PINNED. Operator, 2026-10-11:
=============================================================================

    "any address must be verfied on the UI in situ after being entered with a
     green check or a red x and not being able to proceed. it should be both a
     regex and a blockchain check."

So there are four claims to prove, and only the last two are about pixels:

  BOTH CHECKS RUN           the local decode AND the chain's own validator. A
                            green check from one of them is the 2026-10-01
                            incident: Brave autofilled a stale Bitcoin testnet
                            address, the chain said isvalid: true because Gridcoin
                            shares Bitcoin testnet's 0xc4 P2SH byte, and 82.65
                            tGRC went to an address nobody held.
  THREE STATES              a daemon that does not answer is NOT a red x. It is
                            its own state with its own sentence, because the
                            address may be perfect and our chain may be down.
  PROCEED IS GATED          may_proceed is False for everything but good, and it
                            is decided on the SERVER. A browser comparing
                            `state === "good"` would be a fund-path decision in a
                            place no test can reach.
  THE GATE IS NOT THE ONLY GUARD   the button is disabled in the markup, and the
                            POST still runs every server check. A disabled
                            attribute is one devtools click from gone.

WHY THE COST OF A KEYSTROKE IS ASSERTED. test_a_local_refusal_asks_no_daemon and
test_one_keystroke_is_at_most_one_pass exist because this endpoint is called from
an `input` handler. An endpoint that issued four RPC calls per character -- which
is what refuse_unusable_payout_address() did until 2026-10-11, measured -- would
make a 34-character paste into 136 round trips against a chain, and against an
unreachable one that is the gunicorn worker timeout several times over.
"""

from __future__ import annotations

import pathlib
import sqlite3
import sys

import pytest

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))
sys.path.insert(0, str(REPO_ROOT / "swap_terminal"))

from chains.base import AddressReport, RPCError  # noqa: E402
from db import SCHEMA  # noqa: E402
from services.address_check import BAD, GOOD, UNKNOWN, live_address_verdict  # noqa: E402
from valid_addresses import GRC_PAYOUT, XRP_CUSTOMER_PAYOUT  # noqa: E402

import app as app_module  # isort: skip  # noqa: E402

SCRIPT_JS = REPO_ROOT / "swap_terminal" / "static" / "script.js"
ADDRESS_TEMPLATE = REPO_ROOT / "swap_terminal" / "templates" / "_atm_payout_address.html"


class Recorder:
    """An adapter with a combined pass, recording what was asked of it.

    `address_report` is what chains/base.RPCAdapter offers, so this stands in for
    BTC, LTC and GRC. The point of recording is that the CALL COUNT is asserted
    rather than described -- rule 17.
    """

    asset = "GRC"

    def __init__(self, report=None, raises=None):
        self._report = report
        self._raises = raises
        self.asked = []

    def address_report(self, address):
        self.asked.append(address)
        if self._raises is not None:
            raise self._raises
        return self._report


class LegacyAdapter:
    """An adapter with NO combined pass -- the XRP/SOL/ICP shape.

    Those three override owns_address() to answer None without an RPC, because
    their ledgers have no `ismine`. This records both methods so the fallback
    path is measured rather than assumed to behave.
    """

    asset = "XRP"

    def __init__(self, valid=True, raises=None):
        self._valid = valid
        self._raises = raises
        self.asked = []

    def validate_address(self, address):
        self.asked.append(("validate_address", address))
        if self._raises is not None:
            raise self._raises
        return self._valid

    def owns_address(self, address):
        self.asked.append(("owns_address", address))


def javascript_without_comments(text: str) -> str:
    """`text` with every // and /* */ comment removed, strings left intact.

    THE SIXTH AND SEVENTH OCCURRENCES OF ONE DEFECT CLASS, both inside an hour, both
    in this file's own gates. The two scans below assert that static/script.js does
    not contain a per-chain address rule and does not compute the submit gate from the
    state string. Both failed on COMMENTS:

      "base58"            appeared in a sentence about one address being substituted
                          for another, so the file was failed for discussing an
                          encoding it does not implement.
      `state !== "good"`  appeared in the comment explaining why the gate is a
                          PARAMETER and not that comparison -- so the fix's own
                          explanation tripped the gate written to enforce the fix.

    tests/test_daemon_conf.code_without_prose() has been the answer to exactly this
    for Python since 2026-10-10, and CLAUDE.md records four earlier instances. It was
    never applied to JavaScript, because until now no gate read JavaScript. There is
    no ast.unparse here, so this is a scanner rather than a parse.

    WHAT IT HANDLES AND WHAT IT DOES NOT, stated because a scanner that silently
    mishandles a case is worse than one with a known edge. It tracks single quotes,
    double quotes and template literals, so a `//` or `/*` inside a string survives
    and a quote inside a comment does not open a string. It does NOT track regex
    literals: `/foo/.test(x)` would have its slash read as the start of a comment if
    it were ever followed by another slash. static/script.js contains no regex literal
    (checked: zero `.test(`, `.match(`, `.replace(/` and `new RegExp`), and
    test_the_comment_stripper_does_its_job below pins the behavior that is claimed.
    """
    out = []
    i = 0
    quote = ""
    n = len(text)
    while i < n:
        ch = text[i]
        if quote:
            out.append(ch)
            if ch == "\\" and i + 1 < n:
                out.append(text[i + 1])
                i += 2
                continue
            if ch == quote:
                quote = ""
            i += 1
            continue
        if ch in "\"'`":
            quote = ch
            out.append(ch)
            i += 1
            continue
        if ch == "/" and i + 1 < n and text[i + 1] == "/":
            while i < n and text[i] != "\n":
                i += 1
            continue
        if ch == "/" and i + 1 < n and text[i + 1] == "*":
            end = text.find("*/", i + 2)
            i = n if end == -1 else end + 2
            continue
        out.append(ch)
        i += 1
    return "".join(out)


def test_the_comment_stripper_does_its_job():
    """The scanner above, on seeded input. Test infrastructure gets tested too.

    Written because the two gates below are only as trustworthy as this function, and
    a stripper that quietly removed a line of CODE would make them pass by deleting
    the thing they check.
    """
    source = (
        'const a = "http://x//y";  // a comment with "quotes" and /* stars */\n'
        "/* a block\n   over lines, mentioning base58 */\n"
        "const b = `a template // not a comment`;\n"
        "const real = 1;\n"
    )
    stripped = javascript_without_comments(source)
    assert "a comment with" not in stripped
    assert "a block" not in stripped
    assert "base58" not in stripped, "a block comment survived, so the gates below prove nothing"
    assert '"http://x//y"' in stripped, "a // inside a string was eaten as a comment"
    assert "a template // not a comment" in stripped, "a template literal was eaten"
    assert "const real = 1;" in stripped


def test_script_js_has_no_regex_literal_for_the_stripper_to_misread():
    """The one edge javascript_without_comments() does not handle, checked rather than hoped.

    If a regex literal is ever added to static/script.js, this fails and says so --
    which is the point: the stripper's limitation is pinned where it would start to
    matter, instead of being a sentence in a docstring nobody re-reads.
    """
    js = SCRIPT_JS.read_text()
    for marker in (".test(", ".match(", ".replace(/", "new RegExp", ".split(/"):
        assert marker not in js, (
            f"static/script.js now contains {marker!r}, which suggests a regex literal. "
            "javascript_without_comments() does not track those and may read a slash as the "
            "start of a comment -- extend it before adding one."
        )


def good_report(mine=False):
    return AddressReport(True, "validateaddress answered isvalid", mine,
                         "validateaddress answered ismine", "", ("validateaddress",))


# --- both halves actually run -----------------------------------------------------


def test_a_valid_address_that_the_chain_confirms_is_GOOD_and_may_proceed():
    adapter = Recorder(good_report())
    verdict = live_address_verdict({"GRC": adapter}, "GRC", GRC_PAYOUT)
    assert verdict.state == GOOD
    assert verdict.may_proceed is True
    assert verdict.chain_ok is True
    assert adapter.asked == [GRC_PAYOUT], "the chain was asked exactly once"


def test_a_local_refusal_is_BAD_and_asks_no_daemon():
    """The local decode runs FIRST and short-circuits, so a typo costs zero RPC.

    This is the same ordering create_swap uses, for the reason its own comment gives
    -- "refused here, before any daemon was asked" -- and here it has a second job:
    this endpoint fires from a keystroke handler, so a string that cannot possibly be
    an address must not reach a chain.
    """
    adapter = Recorder(good_report())
    verdict = live_address_verdict({"GRC": adapter}, "GRC", "obviously-not-an-address")
    assert verdict.state == BAD
    assert verdict.may_proceed is False
    assert adapter.asked == [], (
        "a locally-refused address reached the daemon. From a keystroke handler that is "
        "one RPC pass per character of whatever the customer is typing."
    )


def test_the_chain_can_refuse_an_address_the_local_decode_accepted():
    """THE 2026-10-01 INCIDENT'S SHAPE, with the arrow pointing the other way.

    A locally-well-formed address that the destination daemon rejects must be a red x.
    The real incident was the reverse -- the chain said yes to an address from another
    network -- and the only defense against that pair is running BOTH checks, which is
    what this test and the one above jointly pin.
    """
    adapter = Recorder(AddressReport(False, "validateaddress answered isvalid", None,
                                     "no `ismine`", "", ("validateaddress",)))
    verdict = live_address_verdict({"GRC": adapter}, "GRC", GRC_PAYOUT)
    assert verdict.state == BAD
    assert verdict.may_proceed is False
    assert "rejected" in verdict.headline


# --- the third state -------------------------------------------------------------


def test_an_unreachable_daemon_is_UNKNOWN_and_never_BAD():
    """THE ASSERTION THIS MODULE EXISTS FOR. Our outage must not read as their typo.

    chains/base.py has been burned by this collapse twice -- validate_address()
    returned False for a daemon that was down until 2026-09-24, so an outage read as a
    bad customer address and sent the operator to check the customer's spelling. A red
    x here would put that same mistake in front of the customer instead.
    """
    adapter = Recorder(AddressReport(None, "ConnectTimeout: host.docker.internal:25715",
                                     None, "ConnectTimeout", "", ("validateaddress", "getaddressinfo")))
    verdict = live_address_verdict({"GRC": adapter}, "GRC", GRC_PAYOUT)
    assert verdict.state == UNKNOWN
    assert verdict.state != BAD
    assert verdict.may_proceed is False, (
        "UNKNOWN must still block Next: a swap created against an unreachable chain chokes "
        "exactly where the operator's GRC -> LTC attempt choked."
    )
    assert "NOT a statement about the address" in verdict.headline, (
        "the sentence must send somebody to the DAEMON, not to the customer's clipboard"
    )


def test_an_unconfigured_chain_is_UNKNOWN_and_says_so_is_not_the_address():
    verdict = live_address_verdict({}, "LTC", GRC_PAYOUT)
    assert verdict.state == UNKNOWN
    assert verdict.may_proceed is False
    assert "not a problem with the address" in verdict.headline.lower()


def test_an_adapter_that_raises_through_the_combined_pass_is_UNKNOWN_not_a_500():
    """address_report() is documented never to raise. If it does, the customer still gets a verdict.

    This is the boundary that turns an unanticipated adapter failure into a sentence
    beside the field instead of an internal server error on a keystroke -- which is the
    failure the whole module was written to end.
    """
    adapter = Recorder(raises=RuntimeError("something nobody anticipated"))
    verdict = live_address_verdict({"GRC": adapter}, "GRC", GRC_PAYOUT)
    assert verdict.state == UNKNOWN
    assert verdict.may_proceed is False


def test_the_legacy_path_turns_a_RAISED_validate_address_into_UNKNOWN():
    """XRP/SOL/ICP have no combined pass, and validate_address RAISES when unanswerable.

    That raise IS the third state on those chains, and catching it is how "nobody
    answered" stays distinguishable from "no".
    """
    # AN XRP ADDRESS AND NOT A GRC ONE. The first draft passed GRC_PAYOUT, and the
    # LOCAL decode refused it before the adapter was ever reached -- so the test
    # asserted UNKNOWN, got BAD, and would have been "fixed" by loosening the
    # assertion rather than by giving it the input it meant. The local half working
    # correctly is what made the test wrong.
    adapter = LegacyAdapter(raises=RPCError("could not validate address; this is NOT a statement"))
    verdict = live_address_verdict({"XRP": adapter}, "XRP", XRP_CUSTOMER_PAYOUT)
    assert verdict.state == UNKNOWN
    assert verdict.may_proceed is False


def test_an_empty_field_is_UNKNOWN_and_asks_nothing():
    adapter = Recorder(good_report())
    verdict = live_address_verdict({"GRC": adapter}, "GRC", "   ")
    assert verdict.state == UNKNOWN
    assert verdict.may_proceed is False
    assert adapter.asked == []


# --- ownership is a warning, not a refusal ----------------------------------------


def test_an_address_in_our_own_wallet_is_still_GOOD_but_says_so():
    """payout_to_the_desk_refusal() owns that decision; this must not pre-empt it.

    The 2026-10-04 incident (swap s_ae76ec53236ffcf6, payout address set to
    XRP_DEPOSIT_ACCOUNT) is real and is refused at create time by a function that reads
    differently per chain family. A red x here would be a second place deciding one
    thing on the field that decides where money lands -- rule 8, on the worst possible
    line. So the customer is TOLD, and the server still refuses.
    """
    verdict = live_address_verdict({"GRC": Recorder(good_report(mine=True))}, "GRC", GRC_PAYOUT)
    assert verdict.state == GOOD
    assert verdict.ours is True
    assert "own wallet" in verdict.headline
    assert verdict.may_proceed is True


# --- the real route, through the real app -----------------------------------------


@pytest.fixture
def client(tmp_path, monkeypatch):
    """The real app, on the real schema, pointed at a per-test database."""
    db_path = tmp_path / "verdict.db"
    conn = sqlite3.connect(db_path)
    conn.executescript(SCHEMA)
    conn.commit()
    conn.close()
    flask_app = app_module.app
    monkeypatch.setitem(flask_app.config, "DB_PATH", str(db_path))
    flask_app.config["TESTING"] = True
    with flask_app.test_client() as test_client:
        yield test_client


def test_the_route_exists_and_answers_200_for_a_bad_address(client, monkeypatch):
    """A refusal is a 200 with a verdict, NOT a 4xx.

    The browser is asking "what should the field say", and every answer -- including
    "that is wrong" -- is a successful answer to that question. A 400 here would make
    the fetch's error path, which cannot carry a sentence, the one that renders the
    most common case.
    """
    monkeypatch.setitem(client.application.config, "ADAPTERS", {"GRC": Recorder(good_report())})
    response = client.post("/api/address/check", json={"asset": "GRC", "address": "nope"})
    assert response.status_code == 200
    body = response.get_json()
    assert body["state"] == BAD
    assert body["may_proceed"] is False
    assert body["headline"]


def test_the_route_carries_every_field_the_browser_reads(client, monkeypatch):
    """`state`, `headline` and `may_proceed` are the three script.js uses. Pinned by name."""
    monkeypatch.setitem(client.application.config, "ADAPTERS", {"GRC": Recorder(good_report())})
    body = client.post("/api/address/check", json={"asset": "GRC", "address": GRC_PAYOUT}).get_json()
    for field in ("state", "headline", "may_proceed", "local_state", "chain_ok", "ours"):
        assert field in body, f"{field} is missing; static/script.js or a future caller reads it"
    assert body["state"] == GOOD
    assert body["may_proceed"] is True


def test_a_missing_or_junk_payload_does_not_500(client, monkeypatch):
    """Called from a browser, so the body is whatever arrives. Every shape gets a verdict."""
    monkeypatch.setitem(client.application.config, "ADAPTERS", {"GRC": Recorder(good_report())})
    for payload in ({}, {"asset": "GRC"}, {"address": GRC_PAYOUT}, {"asset": "", "address": ""}):
        response = client.post("/api/address/check", json=payload)
        assert response.status_code == 200, f"{payload} produced {response.status_code}"
        assert response.get_json()["may_proceed"] is False


def test_the_asset_is_upcased_so_a_lowercase_chain_still_resolves(client, monkeypatch):
    monkeypatch.setitem(client.application.config, "ADAPTERS", {"GRC": Recorder(good_report())})
    body = client.post("/api/address/check", json={"asset": "grc", "address": GRC_PAYOUT}).get_json()
    assert body["state"] == GOOD


def test_the_route_is_POST_and_not_GET(client):
    """A customer's payout address must not land in an access log or browser history."""
    assert client.get("/api/address/check").status_code == 405


# --- the browser's half, asserted as text ----------------------------------------


def test_the_submit_button_is_disabled_in_the_MARKUP():
    """No-JS must mean no submit, not a silent pass.

    Without this the markup's button is enabled and nothing checks the address at all
    -- the exact state before this change, reached by a different route.
    """
    html = ADDRESS_TEMPLATE.read_text()
    assert 'type="submit" class="primary" disabled' in html, (
        "the Next button on the address step is not disabled in the template, so a browser "
        "with JavaScript off gets an enabled button and no verdict."
    )


def test_the_template_renders_the_verdict_target_and_the_chain():
    html = ADDRESS_TEMPLATE.read_text()
    assert 'id="payout_address_verdict"' in html
    assert 'data-asset="{{ answers.to_asset }}"' in html, (
        "the chain must come from the server's answer. A browser choosing it would check "
        "the address against the wrong chain -- the shape of the 2026-10-01 incident, where "
        "a Bitcoin testnet address passed a Gridcoin check on a shared version byte."
    )


def test_the_browser_reads_may_proceed_and_never_decides_it():
    """THE GATE IS A FUND-PATH DECISION AND IT LIVES ON THE SERVER.

    `state === "good"` in JavaScript would be that decision in a place no test can
    reach, and a later edit could widen it to include "unknown" without anything
    failing. So the script must read the server's boolean.
    """
    js = javascript_without_comments(SCRIPT_JS.read_text())
    assert "verdict.may_proceed" in js, (
        "static/script.js does not read may_proceed from the server, so the submit gate is "
        "being computed in the browser."
    )
    assert 'state === "good"' not in js and "state == 'good'" not in js, (
        "static/script.js is deciding the gate from the state string. may_proceed exists so "
        "that decision stays in services/address_check.py where a test can assert it."
    )


def test_the_browser_defines_no_address_rules_of_its_own():
    """templates/_atm_payout_address.html's standing rule, enforced rather than stated.

    "NO BROWSER VALIDATION PATTERN. A pattern here would be a third spelling of the
    per-chain address rule, in a language the server cannot check."
    """
    js = javascript_without_comments(SCRIPT_JS.read_text())
    for forbidden in ("bech32", "base58", "^[13]", "tb1", "ltc1", "startsWith(\"1\")"):
        assert forbidden not in js, (
            f"static/script.js contains {forbidden!r}, which is a per-chain address rule in a "
            "language the server cannot check -- a third spelling of modules/address_authority."
        )


def test_a_stale_reply_cannot_overwrite_a_fresh_one():
    """Two checks in flight land out of order, and the slow one must not win.

    The chain half's duration varies by orders of magnitude between a reachable daemon
    and a dropping one -- 0.002s against 5s, measured 2026-10-11 -- so a verdict for an
    address the customer has already edited can arrive after the verdict for the one
    they are looking at. Rendering it would put a green check beside different text.
    """
    js = javascript_without_comments(SCRIPT_JS.read_text())
    assert "mine !== sequence" in js, (
        "static/script.js has no sequence guard on the in-flight check, so a slow reply for an "
        "edited address can overwrite the verdict for the current one."
    )


def test_editing_a_checked_address_disables_Next_before_the_check_is_scheduled():
    """A customer who edits a green address must not have a window to click through.

    The debounce means the new verdict is 450ms away; without an immediate disable, Next
    stays enabled against the OLD address's verdict for that whole window.
    """
    js = javascript_without_comments(SCRIPT_JS.read_text())
    handler = js[js.index('field.addEventListener("input"'):]
    handler = handler[: handler.index("});")]
    assert "submit.disabled = true" in handler, (
        "the input handler does not disable Next immediately, so there is a debounce-long "
        "window in which the button is live against a stale verdict."
    )
    assert handler.index("submit.disabled = true") < handler.index("setTimeout"), (
        "Next is disabled after the check is scheduled rather than before it"
    )
