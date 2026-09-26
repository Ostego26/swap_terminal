"""open_swap.py: what it refuses, what it writes, and what it prints back.

Role: test (seeded temporary databases and stub adapters; opens no socket)
Reads: open_swap.py, and through it services/quote_service.py,
       services/swap_service.py and services/xrp_tag_service.py
Writes: a temporary database per test, created by the tool under test
Can move funds: no. The tool signs nothing; these tests stub every adapter and
      stub the price fetch, so nothing here reaches a chain or CoinGecko.
Mainnet-safe: yes

WHY THE ASSERTIONS ARE ON ROWS AND ON PRINTED LINES, AND NEVER ON ARITHMETIC.

Every number this tool shows comes out of the dicts create_quote() and
create_swap() returned, so the test that matters is "does the screen agree with
the row". Recomputing the payout here -- amount * rate * (1 - bps/10000) -
reserve -- would be a second copy of the fee arithmetic that decides what gets
broadcast, and it would pass while disagreeing with the database. So the
end-to-end tests read the swap back out of SQLite and compare the printed block
against THAT.
"""

import argparse
import sqlite3
import sys
from pathlib import Path

import pytest
from requests.exceptions import ConnectionError as RequestsConnectionError
from requests.exceptions import JSONDecodeError as RequestsJSONDecodeError

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "swap_terminal"))

# The two path inserts above both have to run first: the tool is at the project
# root, which conftest.py does not put on sys.path, and the tool's own imports
# are rootless out of swap_terminal/.
from chains.base import RPCError
from db import connect_db
from report_block import LABEL_WIDTH
from services import quote_service
from workers.common import get_config_dict

import open_swap
from open_swap import (
    SwapRefused,
    apply_command,
    check_payout_address,
    deposit_preview,
    fetch_prices_or_refuse,
    next_command,
    open_swap_warning,
    pair_catalog,
    parse_pair,
    report_lines,
    swappable_amount,
)

# A real XRP classic address (checksum valid) and a Gridcoin-shaped payout
# address. Both are only ever handed to the stub adapters below, which is why a
# fixed pair of strings is enough: no test here asks a daemon anything.
XRP_ACCOUNT = "rnjG8n16JinjqkzZj5Jmw6NDMBMzhhNbVv"
GRC_ADDRESS = "SdzHNW1234567890abcdefghijklmnopq"

PRICES = {"XRP_USD": 2.5, "GRC_USD": 0.05, "BTC_USD": 60000.0, "LTC_USD": 100.0, "fetched_at": 0.0}


class StubXRP:
    """The XRP adapter's two methods that matter here, and one that must not run.

    get_new_address() raises rather than returning something address-shaped, which
    is what the real adapter does and why: deriving a fresh XRP account per swap
    would mean funding each one past the base reserve to solve what the ledger
    solved with an integer tag.
    """

    # DECLARED, because chains/registry.why_cannot_pay_out() fails closed: a stub
    # that says nothing about itself counts as unable to pay, and create_swap()
    # refuses a destination that cannot. These stubs stand in for a chain that CAN
    # be paid out to, so they say so -- the real XRPAdapter and SolanaAdapter both
    # declare False, which is what took GRC -> XRP off the menu on 2026-09-26.
    can_spend = True
    payout_refusal = ""

    def validate_address(self, address):
        return isinstance(address, str) and address.startswith("r") and len(address) > 25

    def get_new_address(self, label):
        raise AssertionError(f"get_new_address must NOT be called for XRP (label={label})")


class StubGRC:
    """A destination adapter that records what it was asked about.

    get_new_address() raises for a different reason than StubXRP's: for GRC it
    would SUCCEED in production, by deriving a key in the hot wallet. A dry run
    that reaches it has written something while announcing it would not, so the
    stub makes that a failure rather than a silent extra key.
    """

    def __init__(self, answer=True):
        self.answer = answer
        self.asked = []

    # DECLARED, because chains/registry.why_cannot_pay_out() fails closed: a stub
    # that says nothing about itself counts as unable to pay, and create_swap()
    # refuses a destination that cannot. These stubs stand in for a chain that CAN
    # be paid out to, so they say so -- the real XRPAdapter and SolanaAdapter both
    # declare False, which is what took GRC -> XRP off the menu on 2026-09-26.
    can_spend = True
    payout_refusal = ""

    def validate_address(self, address):
        self.asked.append(address)
        return self.answer and isinstance(address, str) and address.startswith("S")

    def get_new_address(self, label):
        raise AssertionError(f"a dry run must not derive a GRC address (label={label})")


class UnaskableGRC:
    """A daemon that cannot be asked. RPCError is what chains/base.py raises."""

    # DECLARED, because chains/registry.why_cannot_pay_out() fails closed: a stub
    # that says nothing about itself counts as unable to pay, and create_swap()
    # refuses a destination that cannot. These stubs stand in for a chain that CAN
    # be paid out to, so they say so -- the real XRPAdapter and SolanaAdapter both
    # declare False, which is what took GRC -> XRP off the menu on 2026-09-26.
    can_spend = True
    payout_refusal = ""

    def validate_address(self, address):
        raise RPCError("connection refused to 127.0.0.1:25715")


def real_config(**overrides):
    """The real Config, with XRP custody filled in. Not a hand-built dict.

    get_config_dict() is what the workers pass to these same services, so a key
    the services need and this test forgot would fail here exactly as it would in
    production -- which a hand-written dict of six keys would hide.
    """
    config = dict(get_config_dict())
    config["XRP_DEPOSIT_ACCOUNT"] = XRP_ACCOUNT
    config.update(overrides)
    return config


def stub_prices(monkeypatch, prices=PRICES, quote_error=None):
    """Stub the price fetch at BOTH call sites, because there are two references.

    `from .pricing import fetch_usd_prices` binds the function object into the
    importing module at import time, so open_swap and services/quote_service each
    hold their own name for it and patching one leaves the other live. That is
    also why open_swap's cache warming is described as warming the CACHE rather
    than as passing a dict: in production both names resolve to the one function
    that consults services/pricing._cache.

    `quote_error` makes ONLY create_quote()'s fetch fail, which is the
    RATE_CACHE_SECONDS=0 case the ordering of open_swap's except clauses is about.
    """
    monkeypatch.setattr(open_swap, "fetch_usd_prices", lambda *a, **k: prices)
    if quote_error is None:
        monkeypatch.setattr(quote_service, "fetch_usd_prices", lambda *a, **k: prices)
    else:
        def _raise(*_a, **_k):
            raise quote_error
        monkeypatch.setattr(quote_service, "fetch_usd_prices", _raise)


def run_tool(monkeypatch, argv, config=None, adapters=None):
    """Call main() with the process's adapters and config replaced. Returns the exit code."""
    monkeypatch.setattr(open_swap, "get_config_dict", lambda: config if config is not None else real_config())
    monkeypatch.setattr(
        open_swap, "build_adapters_from_config",
        lambda: adapters if adapters is not None else {"XRP": StubXRP(), "GRC": StubGRC()},
    )
    return open_swap.main(argv)


# --- parsing the pair --------------------------------------------------------

@pytest.mark.parametrize("text", ["XRP:GRC", "xrp:grc", " XRP : GRC ", "XRP/GRC"])
def test_a_pair_is_parsed_into_two_upper_case_assets(text):
    """Both separators are shell-safe, and the tokens are normalized here.

    Lower case matters: create_quote() upper-cases its own arguments, so a
    `--pair xrp:grc` that reached it un-normalized would price correctly while
    every adapter lookup in this file looked up "xrp" and reported XRP as
    unconfigured.
    """
    assert parse_pair(text) == ("XRP", "GRC")


def test_a_pair_with_no_separator_refuses_and_names_the_form():
    with pytest.raises(SwapRefused, match="FROM:TO"):
        parse_pair("XRPGRC")


def test_an_arrow_is_not_accepted_because_the_shell_would_eat_it():
    """`XRP->GRC` unquoted is a redirect that creates a file called GRC.

    The refusal has to name the form that works, or the operator retries the
    arrow with quotes and gets a second refusal.
    """
    with pytest.raises(SwapRefused, match="shell redirect"):
        parse_pair("XRP->GRC")


@pytest.mark.parametrize("text", ["XRP:GRC:LTC", "XRP:", ":GRC", ":"])
def test_a_pair_that_does_not_name_exactly_two_assets_refuses(text):
    with pytest.raises(SwapRefused):
        parse_pair(text)


def test_the_allowed_pairs_are_listed_sorted_for_the_header():
    """Sorted, and SAYING which question it answered.

    With no adapters this can only read ALLOWED_PAIRS, and it now says so rather than
    presenting the list as if reachability had been checked -- which is the overclaim
    review found on 2026-09-26: the swap page had learned to mark a pair DISABLED when
    its chain is unreachable or cannot pay out, and this line went on listing all six
    as equivalent, from the tool written to replace that page.
    """
    catalog = pair_catalog({"ALLOWED_PAIRS": {("XRP", "GRC"), ("GRC", "BTC")}})

    assert catalog.startswith("GRC->BTC, XRP->GRC")
    assert "not checked here" in catalog, "it must not imply the reachability half passed"


def test_an_unreachable_pair_is_marked_unavailable_with_the_reason():
    """The whole point of the fix. MUTATION: drop the adapters branch and this fails
    while the no-adapters test above keeps passing, which is why both exist."""
    class Payer:
        can_spend = True
        payout_refusal = ""

    class ViewOnly:
        can_spend = False
        payout_refusal = "cannot pay out in this test"

    config = {"ALLOWED_PAIRS": {("XRP", "GRC"), ("GRC", "XRP")}, "RPC": {}}

    catalog = pair_catalog(config, {"XRP": ViewOnly(), "GRC": Payer()})

    assert "XRP->GRC" in catalog
    assert "GRC->XRP (UNAVAILABLE)" in catalog, catalog
    assert "cannot pay out in this test" in catalog, "the reason must travel with the mark"


def test_every_pair_reachable_prints_no_unavailable_note():
    """So the mark cannot pass by always appearing."""
    class Payer:
        can_spend = True
        payout_refusal = ""

    config = {"ALLOWED_PAIRS": {("XRP", "GRC")}, "RPC": {}}

    catalog = pair_catalog(config, {"XRP": Payer(), "GRC": Payer()})

    assert catalog == "XRP->GRC"
    assert "UNAVAILABLE" not in catalog


def test_an_empty_pair_list_says_so_rather_than_printing_nothing():
    """Rule 14: a blank is ambiguous between "no pairs" and "the query broke"."""
    assert "(none)" in pair_catalog({"ALLOWED_PAIRS": set()})


# --- the payout address, and the three states of asking ----------------------

def test_a_valid_payout_address_reports_valid():
    state, _ = check_payout_address({"GRC": StubGRC()}, "GRC", GRC_ADDRESS)

    assert state == "VALID"


def test_a_rejected_payout_address_reports_invalid():
    state, detail = check_payout_address({"GRC": StubGRC()}, "GRC", "not-a-gridcoin-address")

    assert state == "INVALID"
    assert "rejects" in detail


def test_a_daemon_that_cannot_be_asked_is_unaskable_and_not_invalid():
    """THE distinction, and the reason this returns three states instead of a bool.

    chains/base.validate_address() used to end `except Exception: return False`,
    so an outage and a malformed address produced the same answer and the
    operator's response to a down daemon was to go and check the customer's
    address. Collapsing UNASKABLE into INVALID here would rebuild that defect one
    layer up.

    MUTATION: return "INVALID" from the ADAPTER_ERRORS handler in
    check_payout_address() and this test alone fails.
    """
    state, detail = check_payout_address({"GRC": UnaskableGRC()}, "GRC", GRC_ADDRESS)

    assert state == "UNASKABLE"
    assert "RPCError" in detail


# --- the deposit preview, which must not derive anything ---------------------

def test_the_xrp_deposit_preview_names_the_shared_account_and_the_tag():
    lines = "\n".join(deposit_preview(real_config(), {"XRP": StubXRP()}, "XRP"))

    assert XRP_ACCOUNT in lines
    assert "destination tag is allocated when --apply runs" in lines


def test_an_unset_deposit_account_refuses_in_the_dry_run_too():
    """Custody has no default, and the dry run is where an operator wants to hear it.

    deposit_account() is the service's own refusal; this asserts the tool surfaces
    it rather than reporting a plan it cannot carry out.
    """
    with pytest.raises(SwapRefused, match="XRP_DEPOSIT_ACCOUNT is not set"):
        deposit_preview(real_config(XRP_DEPOSIT_ACCOUNT=""), {"XRP": StubXRP()}, "XRP")


def test_an_address_attributed_pair_derives_nothing_during_a_preview():
    """StubGRC.get_new_address() raises, so this passes only if it is never called.

    For BTC, LTC and GRC, deposit_account() calls getnewaddress -- which DERIVES A
    KEY IN THE HOT WALLET. A dry run that reached it would have written something
    while announcing that it would not, and the extra key would be silent: nothing
    fails, the wallet just holds one more address than any swap references.

    MUTATION: delete the `from_asset not in TAG_ATTRIBUTED_ASSETS` branch in
    deposit_preview() and this test alone fails.
    """
    lines = "\n".join(deposit_preview(real_config(), {"GRC": StubGRC()}, "GRC"))

    assert "DERIVED in the hot wallet" in lines
    assert "--apply" in lines


# --- what to say about swaps that are already open ---------------------------

def test_no_open_swap_says_none_rather_than_nothing():
    lines = open_swap_warning([], "XRP")

    assert len(lines) == 1
    assert "(none)" in lines[0]
    assert "latest" in lines[0]


def test_open_swaps_are_listed_with_the_flag_that_selects_them():
    """A warning, not a refusal -- and the justification is in the listing.

    `xrp_send_tagged.py --swap latest` refuses on ambiguity because "which one you
    meant is not knowable from here". It IS knowable here, so this tool warns and
    prints `--swap <the new id>`; the operator still needs the other ids to be able
    to act on them.
    """
    rows = [
        {"id": "s_a", "deposit_tag": 3, "expected_input_amount": 1.0, "created_at": "2026-09-26T01:00:00Z"},
        {"id": "s_b", "deposit_tag": 4, "expected_input_amount": 5.0, "created_at": "2026-09-26T02:00:00Z"},
    ]

    text = "\n".join(open_swap_warning(rows, "XRP"))

    assert "2 XRP swap(s)" in text
    assert "s_a" in text and "s_b" in text
    assert "tag 3" in text and "tag 4" in text
    assert "will refuse" in text


# --- the price fetch, made legible ------------------------------------------

def test_a_price_feed_outage_is_a_sentence_and_not_a_traceback(monkeypatch, capsys):
    """services/pricing.py raises on purpose; this is where that becomes readable.

    MUTATION: drop RequestException from the except tuple in
    fetch_prices_or_refuse() and this test fails with the ConnectionError itself
    instead of SwapRefused.
    """
    monkeypatch.setattr(open_swap, "fetch_usd_prices", lambda *a, **k: (_ for _ in ()).throw(
        RequestsConnectionError("api.coingecko.com: connection refused")
    ))

    with pytest.raises(SwapRefused) as caught:
        fetch_prices_or_refuse(real_config())

    message = str(caught.value)
    assert "NOTHING was written" in message
    assert "ConnectionError" in message
    # The announcement has to be on screen BEFORE the failure, or the operator
    # cannot tell a slow fetch from a hung one (rule 14).
    assert "fetching USD prices" in capsys.readouterr().out


def test_a_response_missing_one_leg_is_also_a_refusal(monkeypatch):
    """pricing.py raises KeyError naming the asset: "a swap priced off a missing
    leg is a swap priced wrong". A KeyError is not a RequestException, so it is
    named separately in the except tuple, and this is the test that says so."""
    def _missing(*_a, **_k):
        raise KeyError("CoinGecko returned no price for GRC")

    monkeypatch.setattr(open_swap, "fetch_usd_prices", _missing)

    with pytest.raises(SwapRefused, match="KeyError"):
        fetch_prices_or_refuse(real_config())


def test_a_successful_fetch_reports_how_many_prices_and_how_long(monkeypatch, capsys):
    """Rule 6: the elapsed time is in microfortnights with the seconds beside it."""
    stub_prices(monkeypatch)

    assert fetch_prices_or_refuse(real_config()) == PRICES

    out = capsys.readouterr().out
    assert "got 4 prices" in out, "the fetched_at stamp must not be counted as a price"
    assert "µfn (" in out, "the micro sign, not an ASCII u, and the seconds in parentheses"


# --- the exact next command, with no placeholder -----------------------------

def test_the_next_command_carries_the_real_swap_id():
    lines = next_command("s_deadbeef", "XRP")

    assert "python3 xrp_send_tagged.py --swap s_deadbeef" in "\n".join(lines)


@pytest.mark.parametrize("from_asset", ["XRP", "GRC"])
def test_no_printed_command_ever_contains_a_placeholder(from_asset):
    """THE guard this whole tool exists for.

    A placeholder left in a pasted command has cost this project three mis-runs
    and twice put something in a shell that should never have been there. The
    reason open_swap.py exists is that a swap id had to travel from a browser to a
    terminal by hand, so printing `<swap id>` would reintroduce exactly that.

    MUTATION: return `--swap <the new id>` from next_command() and this fails for
    XRP.
    """
    text = "\n".join(next_command("s_deadbeef", from_asset))

    assert "<" not in text and ">" not in text


def test_an_asset_with_no_scripted_sender_says_so_instead_of_inventing_one():
    """There is no root script that pays a GRC deposit into a swap.

    Established by grepping the root rather than assumed -- see DEPOSIT_SENDERS.
    Printing `python3 grc_send_tagged.py` would be a command that does not exist,
    which is worse than saying there is none.
    """
    text = "\n".join(next_command("s_1", "GRC"))

    assert "no scripted sender" in text
    assert "xrp_send_tagged" not in text


def test_the_apply_command_echoes_the_values_that_were_passed():
    """So the second command needs no retyping either -- especially not the address."""
    parsed = open_swap.build_parser().parse_args(
        ["--pair", "xrp:grc", "--amount", "1.5", "--payout-address", GRC_ADDRESS]
    )

    command = apply_command(parsed, "XRP", "GRC")

    assert command == f"python3 open_swap.py --pair XRP:GRC --amount 1.5 --payout-address {GRC_ADDRESS} --apply"
    assert "<" not in command


# --- end to end, against a database the tool creates ------------------------

def swap_row(db_path):
    connection = connect_db(str(db_path))
    try:
        return connection.execute("SELECT * FROM swaps").fetchall()
    finally:
        connection.close()


def test_a_dry_run_writes_nothing_at_all(monkeypatch, tmp_path, capsys):
    """Not the rows, not the schema, and not the database FILE.

    sqlite3.connect() creates the file it is pointed at, so a dry run that opened
    a connection to check for open swaps would leave an empty database behind
    while announcing that it wrote nothing.

    MUTATION: remove the Path(db_path).exists() guard from read_open_swaps() and
    this test alone fails, on the file existing.
    """
    db_path = tmp_path / "nothing-here.db"

    code = run_tool(
        monkeypatch,
        ["--pair", "XRP:GRC", "--amount", "1", "--payout-address", GRC_ADDRESS, "--db", str(db_path)],
    )

    assert code == 0
    assert not db_path.exists(), "a dry run must not create the database file"
    out = capsys.readouterr().out
    assert "DRY RUN" in out
    # --db IS IN THE PRINTED COMMAND, and this assertion pinned the bug until
    # 2026-09-26. Three reviewers found it independently: the block claims "every
    # value is already in it" and the command dropped the one flag that decides WHICH
    # DATABASE gets written -- so a dry run against a scratch file printed a command
    # that would create the swap, and its permanent immutable tag, in the default
    # database instead. Rewritten rather than deleted (rule 2): the stronger
    # invariant is that the command runs as printed against the same database.
    assert (
        f"python3 open_swap.py --pair XRP:GRC --amount 1.0 --payout-address {GRC_ADDRESS} "
        f"--db {db_path} --apply"
    ) in out


def test_the_apply_command_omits_db_when_none_was_given(monkeypatch, tmp_path, capsys):
    """The short form stays short. Paired with the test above so that "carry --db"
    cannot be satisfied by always printing one, which would name a path the operator
    never chose."""
    monkeypatch.setenv("SWAP_DB_PATH", str(tmp_path / "default.db"))
    code = run_tool(
        monkeypatch,
        ["--pair", "XRP:GRC", "--amount", "1", "--payout-address", GRC_ADDRESS],
    )

    assert code == 0
    out = capsys.readouterr().out
    assert "--db" not in out.split("paste this")[-1], "no --db was passed, so none may be printed"


def test_apply_writes_the_quote_the_swap_and_the_tag(monkeypatch, tmp_path, capsys):
    """The whole point, asserted on the rows actually present.

    The tool bootstraps the database it was pointed at -- the same
    executescript(SCHEMA) plus apply_migrations() that init_db() and every worker
    cycle run -- so this starts from a path that does not exist.
    """
    stub_prices(monkeypatch)
    db_path = tmp_path / "opened.db"

    code = run_tool(
        monkeypatch,
        ["--pair", "XRP:GRC", "--amount", "1", "--payout-address", GRC_ADDRESS, "--db", str(db_path), "--apply"],
    )

    assert code == 0
    rows = swap_row(db_path)
    assert len(rows) == 1, "exactly one swap row"
    swap = rows[0]
    assert swap["from_asset"] == "XRP" and swap["to_asset"] == "GRC"
    assert swap["deposit_address"] == XRP_ACCOUNT, "the shared XRP account, from XRP_DEPOSIT_ACCOUNT"
    assert isinstance(swap["deposit_tag"], int), "a destination tag was allocated by the service"
    assert swap["payout_address"] == GRC_ADDRESS
    assert swap["expected_input_amount"] == 1.0
    assert swap["status"] == "awaiting_deposit"

    connection = connect_db(str(db_path))
    try:
        quotes = connection.execute("SELECT id, from_asset, to_asset FROM quotes").fetchall()
        tags = connection.execute(
            "SELECT swap_id, destination_tag FROM xrp_destination_tags"
        ).fetchall()
        audit = connection.execute("SELECT new_status FROM swap_audit_log WHERE swap_id = ?", (swap["id"],)).fetchall()
    finally:
        connection.close()
    assert [q["id"] for q in quotes] == [swap["quote_id"]], "the swap points at the quote that priced it"
    assert [(t["swap_id"], t["destination_tag"]) for t in tags] == [(swap["id"], swap["deposit_tag"])], (
        "the tag table and the swap row must agree, and the tag must belong to THIS swap"
    )
    assert [a["new_status"] for a in audit] == ["awaiting_deposit"]

    out = capsys.readouterr().out
    assert swap["id"] in out
    assert f"python3 xrp_send_tagged.py --swap {swap['id']}" in out, "the next command, with the real id"


def test_the_printed_block_agrees_with_the_row_it_wrote(monkeypatch, tmp_path, capsys):
    """Screen against database, which is the only comparison that catches a lie.

    Asserting the payout figure against arithmetic recomputed here would pass
    while disagreeing with the row that payout_worker actually reads.
    """
    stub_prices(monkeypatch)
    db_path = tmp_path / "agreement.db"

    run_tool(
        monkeypatch,
        ["--pair", "XRP:GRC", "--amount", "2", "--payout-address", GRC_ADDRESS, "--db", str(db_path), "--apply"],
    )

    swap = swap_row(db_path)[0]
    out = capsys.readouterr().out
    for field in ("deposit_address", "payout_address", "quoted_rate", "output_amount_estimate",
                  "expected_input_amount", "min_confirmations"):
        assert str(swap[field]) in out, f"{field} is printed as the row holds it"
    assert str(swap["deposit_tag"]) in out
    assert str(db_path) in out, "the database path, because a swap in another file is invisible to the workers"


def test_the_block_labels_the_tag_as_mandatory_and_names_the_units(monkeypatch, tmp_path):
    """Rule 14: what a number MEANS goes next to the number, because the operator
    reads the screen and not the source. The tag has no checksum behind it and an
    untagged payment to the shared account is credited to nobody, so "MANDATORY"
    is the word that has to be there."""
    stub_prices(monkeypatch)
    db_path = tmp_path / "labels.db"
    run_tool(
        monkeypatch,
        ["--pair", "XRP:GRC", "--amount", "1", "--payout-address", GRC_ADDRESS, "--db", str(db_path), "--apply"],
    )
    swap = swap_row(db_path)[0]

    connection = connect_db(str(db_path))
    try:
        quote = connection.execute("SELECT * FROM quotes WHERE id = ?", (swap["quote_id"],)).fetchone()
    finally:
        connection.close()
    lines = report_lines(swap, quote, str(db_path), real_config())
    text = "\n".join(lines)

    assert "MANDATORY" in text
    assert "a COUNT of confirmations, never a duration" in text
    assert "bps" in text
    assert "µfn (" in text, "the rate window's width is a duration, so it is in microfortnights"


def test_every_printed_label_leaves_a_gap_before_its_value(monkeypatch, tmp_path, capsys):
    """The label column, checked rather than eyeballed.

    open_swap prints five blocks -- header, deposit preview, open-swap warning,
    price lines, report -- through one labeled() helper, so they line up and a
    reader scanning for one number scans a column. A label that fills
    LABEL_WIDTH exactly produces `estimated payout49.24`: still true, still
    unreadable, and exactly what the first draft of report_lines() did with two
    of its labels.

    Only lines of the form `  <label>` are checked: a pasteable command is
    indented four spaces and a continuation line is indented past the column, and
    neither is a label row.

    MUTATION: rename "payout (est.)" back to "estimated payout" in report_lines()
    and this test alone fails, naming the line.
    """
    stub_prices(monkeypatch)
    db_path = tmp_path / "aligned.db"
    run_tool(
        monkeypatch,
        ["--pair", "XRP:GRC", "--amount", "1", "--payout-address", GRC_ADDRESS, "--db", str(db_path), "--apply"],
    )

    checked = 0
    for line in capsys.readouterr().out.splitlines():
        if not line.startswith("  ") or line[2:3] in ("", " "):
            continue
        checked += 1
        assert line[LABEL_WIDTH + 1] == " ", f"the label column overflows: {line!r}"
    assert checked > 10, f"only {checked} label rows were checked, so this asserted almost nothing"


def test_an_address_attributed_swap_reports_no_tag_as_a_result(tmp_path):
    """(none) is the answer for a chain that attributes by address, not a blank.

    GRC->XRP is an allowed pair, so this path is reachable: the deposit is GRC, a
    fresh GRC address identifies the swap, and deposit_tag is NULL. A blank there
    would read as "the tag is missing", which for an XRP swap is the failure that
    credits nobody.
    """
    swap = {
        "id": "s_1", "from_asset": "GRC", "to_asset": "XRP", "deposit_address": "SsomeGrcAddress",
        "deposit_tag": None, "payout_address": XRP_ACCOUNT, "expected_input_amount": 100.0,
        "quoted_rate": 0.02, "fee_bps": 150, "network_fee_reserve": 0.01,
        "output_amount_estimate": 1.96, "status": "awaiting_deposit", "min_confirmations": 6,
        "expires_at": "2026-09-26T17:00:00+00:00",
    }

    text = "\n".join(report_lines(swap, {"id": "q_1"}, str(tmp_path / "x.db"), real_config()))

    assert "destination tag (none)" in text
    assert "attributed by ADDRESS" in text


def test_an_invalid_payout_address_writes_no_rows(monkeypatch, tmp_path, capsys):
    """Refused before the quote, so there is not even a quote row to clean up.

    The payout address cannot be changed after the swap exists, which is why it is
    checked first.
    """
    stub_prices(monkeypatch)
    db_path = tmp_path / "refused.db"

    code = run_tool(
        monkeypatch,
        ["--pair", "XRP:GRC", "--amount", "1", "--payout-address", "definitely-not-gridcoin",
         "--db", str(db_path), "--apply"],
    )

    assert code == 2
    assert not db_path.exists(), "no database, so no quote row and no swap row"
    assert "REFUSED" in capsys.readouterr().err


def test_a_pair_outside_allowed_pairs_refuses_with_the_list(monkeypatch, tmp_path, capsys):
    """validate_pair()'s own refusal, with the catalog attached so it is actionable."""
    code = run_tool(
        monkeypatch,
        ["--pair", "BTC:XRP", "--amount", "1", "--payout-address", XRP_ACCOUNT, "--db", str(tmp_path / "x.db")],
        adapters={"XRP": StubXRP(), "BTC": StubGRC()},
    )

    assert code == 2
    error = capsys.readouterr().err
    assert "Unsupported trading pair" in error
    assert "XRP->GRC" in error, "the allowed directions have to be in the refusal"


def test_a_chain_with_no_adapter_names_the_variable_to_export(monkeypatch, tmp_path, capsys):
    """The 2026-09-26 incident, from the other end.

    The browser said `No swap was created: 'GRC'` -- str(KeyError("GRC")) -- with a
    Gridcoin daemon running and a priced quote. This tool must never print that: it
    asks chains/registry.why_unconfigured() for the sentence, which is the same
    function create_swap() uses.
    """
    code = run_tool(
        monkeypatch,
        ["--pair", "XRP:GRC", "--amount", "1", "--payout-address", GRC_ADDRESS, "--db", str(tmp_path / "x.db")],
        adapters={"XRP": StubXRP()},
    )

    assert code == 2
    error = capsys.readouterr().err
    assert "GRC has no adapter in this process" in error
    assert "GRC_RPC" in error, "the refusal must name a variable, not just the chain"
    assert error.strip() != "REFUSED: 'GRC'"


def test_an_unaskable_destination_daemon_is_refused_as_an_outage(monkeypatch, tmp_path, capsys):
    """Not as a bad address. The operator's next step differs entirely."""
    code = run_tool(
        monkeypatch,
        ["--pair", "XRP:GRC", "--amount", "1", "--payout-address", GRC_ADDRESS, "--db", str(tmp_path / "x.db")],
        adapters={"XRP": StubXRP(), "GRC": UnaskableGRC()},
    )

    assert code == 2
    error = capsys.readouterr().err
    assert "could not be asked" in error
    assert "NOT established" in error


def test_a_second_open_swap_warns_and_still_opens(monkeypatch, tmp_path, capsys):
    """Decision 3, asserted end to end: two swaps, a warning, and a usable command.

    The second run must name the FIRST swap in its warning -- otherwise the
    operator cannot tell which of the two `--swap latest` is now ambiguous
    between -- and must still print the new id in its own next command.
    """
    stub_prices(monkeypatch)
    db_path = tmp_path / "two.db"
    argv = ["--pair", "XRP:GRC", "--amount", "1", "--payout-address", GRC_ADDRESS, "--db", str(db_path), "--apply"]

    assert run_tool(monkeypatch, argv) == 0
    first = swap_row(db_path)[0]["id"]
    capsys.readouterr()

    assert run_tool(monkeypatch, argv) == 0
    out = capsys.readouterr().out

    ids = {row["id"] for row in swap_row(db_path)}
    assert len(ids) == 2, "the second swap is created, not refused"
    assert first in out, "the already-open swap has to be named"
    assert "will refuse" in out, "and what `--swap latest` now does has to be said"
    second = next(swap_id for swap_id in ids if swap_id != first)
    assert f"python3 xrp_send_tagged.py --swap {second}" in out


def test_two_swaps_never_share_a_destination_tag(monkeypatch, tmp_path):
    """The service allocates them; this asserts the tool does not undo that.

    A shared tag on a shared account means one customer's deposit credits another
    customer's swap, with the ledger recording that they paid exactly what they
    were told to pay.
    """
    stub_prices(monkeypatch)
    db_path = tmp_path / "tags.db"
    argv = ["--pair", "XRP:GRC", "--amount", "1", "--payout-address", GRC_ADDRESS, "--db", str(db_path), "--apply"]

    run_tool(monkeypatch, argv)
    run_tool(monkeypatch, argv)

    tags = [row["deposit_tag"] for row in swap_row(db_path)]
    assert len(set(tags)) == 2, f"tags must be distinct, got {tags}"


def test_a_price_failure_inside_create_quote_is_reported_as_the_price_feed(monkeypatch, tmp_path, capsys):
    """The except-clause ORDER, which is the one subtle claim in this file.

    requests' JSONDecodeError subclasses BOTH RequestException and ValueError
    (verified against requests 2.33.1), and create_quote() raises ValueError for
    its own refusals -- so if the ValueError clause came first, a mangled price
    response would be reported as a service refusal. This reaches that path the
    way production would, with RATE_CACHE_SECONDS at 0 so create_quote() refetches
    after the tool's own fetch succeeded.

    MUTATION: move the `except ValueError` clause above `except RequestException`
    in apply_swap() and this test alone fails.
    """
    stub_prices(monkeypatch, quote_error=RequestsJSONDecodeError("Expecting value", "", 0))
    db_path = tmp_path / "mangled.db"

    code = run_tool(
        monkeypatch,
        ["--pair", "XRP:GRC", "--amount", "1", "--payout-address", GRC_ADDRESS, "--db", str(db_path), "--apply"],
        config=real_config(RATE_CACHE_SECONDS=0),
    )

    assert code == 2
    error = capsys.readouterr().err
    assert "price feed failed" in error
    rows = swap_row(db_path)
    assert rows == [], "no swap row for a quote that never priced"


def test_a_locked_database_is_reported_as_a_lock_and_not_a_traceback(monkeypatch, tmp_path, capsys):
    """The workers hold the one SQLite write lock, and this tool is a writer.

    Provoked by holding an EXCLUSIVE transaction open on the same file, which is
    what a worker mid-write looks like from here.
    """
    stub_prices(monkeypatch)
    db_path = tmp_path / "locked.db"
    holder = sqlite3.connect(str(db_path), isolation_level=None)
    holder.execute("CREATE TABLE placeholder (x INTEGER)")
    holder.execute("BEGIN EXCLUSIVE")
    try:
        code = run_tool(
            monkeypatch,
            ["--pair", "XRP:GRC", "--amount", "1", "--payout-address", GRC_ADDRESS,
             "--db", str(db_path), "--apply"],
        )
    finally:
        holder.close()

    assert code == 2
    error = capsys.readouterr().err
    assert "SQLite refused the write" in error
    assert "database is locked" in error


# --- the orphan quote a refusal used to leave behind ---------------------------

# A DEPOSIT THAT WOULD PAY OUT NOTHING, which is also the only refusal that reaches
# create_swap() after create_quote() has committed -- every earlier check (the pair,
# the chains, the payout address, the deposit account) runs before the quote is
# priced, deliberately. So these two tests share one path: the zero-payout refusal
# from item 3 is what exposes the orphan quote from item 1.
TINY_AMOUNT = "0.0000001"


def test_a_deposit_that_would_pay_out_nothing_is_refused(monkeypatch, tmp_path, capsys):
    """create_quote() computes max(gross * (1 - fee) - reserve, 0.0), so a small enough
    input prices to exactly 0.0 -- the GRC network fee reserve alone exceeds the whole
    payout. This used to print "payout (est.) 0.0 GRC <- what payout_worker
    broadcasts" and exit 0, handing out a deposit instruction for a swap that could
    only ever deliver nothing.

    Refused in create_swap() rather than here, because the web form reaches the same
    arithmetic. MUTATION: remove that guard and this fails.
    """
    db_path = tmp_path / "zero.db"

    stub_prices(monkeypatch)
    code = run_tool(
        monkeypatch,
        ["--pair", "XRP:GRC", "--amount", TINY_AMOUNT, "--payout-address", GRC_ADDRESS,
         "--db", str(db_path), "--apply"],
    )

    assert code == 2
    message = capsys.readouterr().err
    assert "which is nothing" in message, message
    assert "Deposit more XRP" in message, "say what to change"


def test_a_refused_swap_leaves_no_quote_row(monkeypatch, tmp_path):
    """create_quote() COMMITS before returning, so db_session's rollback cannot undo it.

    Every refusal from create_swap() used to leave that row behind while three refusal
    messages said "Nothing was committed". Flagged by review 2026-09-26; the docstring
    had it as "Named, not fixed".

    MUTATION: remove the `finally` that deletes it. This test alone fails, on the
    quotes count -- the swaps count was already 0.
    """
    db_path = tmp_path / "orphan.db"

    stub_prices(monkeypatch)
    code = run_tool(
        monkeypatch,
        ["--pair", "XRP:GRC", "--amount", TINY_AMOUNT, "--payout-address", GRC_ADDRESS,
         "--db", str(db_path), "--apply"],
    )

    assert code == 2, "the swap must be refused"
    connection = sqlite3.connect(db_path)
    try:
        quotes = connection.execute("SELECT COUNT(*) FROM quotes").fetchone()[0]
        swaps = connection.execute("SELECT COUNT(*) FROM swaps").fetchone()[0]
    finally:
        connection.close()
    assert swaps == 0, "no swap row, which was already true"
    assert quotes == 0, (
        "the quote row create_quote() committed must be deleted when the swap is not created -- "
        "otherwise the refusal's claim that nothing was committed is false"
    )


def test_a_successful_swap_keeps_its_quote_row(monkeypatch, tmp_path):
    """So the delete cannot pass by removing the quote every time."""
    db_path = tmp_path / "kept.db"

    stub_prices(monkeypatch)
    code = run_tool(
        monkeypatch,
        ["--pair", "XRP:GRC", "--amount", "1", "--payout-address", GRC_ADDRESS,
         "--db", str(db_path), "--apply"],
    )

    assert code == 0
    connection = sqlite3.connect(db_path)
    try:
        quotes = connection.execute("SELECT COUNT(*) FROM quotes").fetchone()[0]
    finally:
        connection.close()
    assert quotes == 1, "the quote that priced a created swap must survive"


def test_the_refusal_does_not_claim_a_quote_row_survives(capsys):
    """The message was the other half of the defect: it offered 'at most the quote row
    was' written as a caveat. There is no such row any more, so the caveat would be a
    new inaccuracy pointing the other way."""
    source = (Path(__file__).resolve().parent.parent / "open_swap.py").read_text()

    assert "at most the quote row was" not in source
    assert "the quote row is deleted when the swap is not created" in source


# --- an amount a swap can be created for ---------------------------------------
#
# type=float ACCEPTED INFINITY, and I checked that by hand in a shell and shipped it
# without a test. The mutation run is what said so: putting `type=float` back failed
# NOTHING. A fix verified only in a terminal is a fix that leaves with the terminal.


@pytest.mark.parametrize(
    ("raw", "needle"),
    [
        ("1e400", "overflows to infinity"),
        ("-1e400", "overflows to infinity"),
        ("inf", "overflows to infinity"),
        ("nan", "not-a-number"),
        ("-1", "is not positive"),
        ("0", "is not positive"),
        ("abc", "is not a number"),
        ("", "is not a number"),
    ],
)
def test_an_unswappable_amount_is_refused_at_the_argument(raw, needle):
    """Before a database file is created or a price is fetched.

    nan and inf are the two this exists for, and neither is caught by
    create_quote()'s own `input_amount <= 0`: inf passes it, and every comparison
    with nan is False so nan passes it too. A swap written with
    expected_input_amount = inf can never be satisfied, because its tolerance band is
    also infinite.
    """
    with pytest.raises(argparse.ArgumentTypeError, match=needle):
        swappable_amount(raw)


@pytest.mark.parametrize("raw", ["1", "1.0", "0.5", "1e-6", "1000000"])
def test_an_ordinary_amount_is_accepted(raw):
    """So the converter cannot pass by refusing everything."""
    assert swappable_amount(raw) == float(raw)


def test_the_parser_itself_refuses_an_infinite_amount(capsys):
    """THROUGH THE PARSER, because testing the converter alone does not pin its use.

    The mutation run made this necessary twice over: putting `type=float` back failed
    nothing when the only tests called swappable_amount() directly, and it still
    failed nothing when the replacement asserted on the exit code -- argparse exits 2
    and so does every later refusal, so `1e400` reaching the checks as `inf` looks
    identical from outside. The MESSAGE is what distinguishes them.
    """
    with pytest.raises(SystemExit) as caught:
        open_swap.main(["--pair", "XRP:GRC", "--amount", "1e400", "--payout-address", GRC_ADDRESS])

    assert caught.value.code == 2
    message = capsys.readouterr().err
    assert "--amount" in message, message
    assert "overflows to infinity" in message, (
        f"the parser is not using swappable_amount -- an infinite amount reached the checks as a "
        f"number instead of being refused here. stderr was: {message!r}"
    )


# --- a payout that lands back in this terminal's own wallet ---------------------
#
# 2026-09-26: the operator's first end-to-end swap paid 55.52645238 GRC to an address
# in their own Gridcoin wallet. The wallet reported a send AND a matching receive, the
# net movement was the 0.001 GRC fee, and they read that as the swap not having
# worked. The screen had said `VALID <- the GRC daemon accepts it`, which is true and
# answers a different question: validateaddress says WELL-FORMED, never YOURS.


class OwnWalletGRC(StubGRC):
    """Valid, and ismine. What the operator's payout address actually was."""

    def owns_address(self, address):
        return True


class ForeignGRC(StubGRC):
    """Valid, and NOT ismine. What a real customer's payout address is."""

    def owns_address(self, address):
        return False


class SilentGRC(StubGRC):
    """Valid, and cannot say. A daemon that answers validity and not ownership --
    `ismine` is a wallet field Bitcoin Core moved to getaddressinfo in 0.18."""

    def owns_address(self, address):
        return None


def test_a_payout_to_our_own_address_says_so(monkeypatch, tmp_path, capsys):
    """MUTATION: return "" unconditionally from payout_destination_note(). This fails
    and the two below keep passing, which is why all three exist."""
    stub_prices(monkeypatch)
    run_tool(
        monkeypatch,
        ["--pair", "XRP:GRC", "--amount", "1", "--payout-address", GRC_ADDRESS,
         "--db", str(tmp_path / "own.db")],
        adapters={"XRP": StubXRP(), "GRC": OwnWalletGRC()},
    )

    out = capsys.readouterr().out
    assert "THIS WALLET'S OWN address" in out, out
    assert "transaction fee only" in out, "say what the operator will actually observe"
    assert "VALID" in out, "it is still a valid address; this is not a refusal"


@pytest.mark.parametrize("adapter_class", [ForeignGRC, SilentGRC])
def test_no_note_when_the_address_is_not_ours_or_cannot_be_judged(
    monkeypatch, tmp_path, capsys, adapter_class
):
    """False and None must both stay silent.

    None is the one that matters: it means "not established", and printing "not
    yours" for it would be inventing the reassuring answer about the one fact that
    decides how to read the result.
    """
    stub_prices(monkeypatch)
    run_tool(
        monkeypatch,
        ["--pair", "XRP:GRC", "--amount", "1", "--payout-address", GRC_ADDRESS,
         "--db", str(tmp_path / "other.db")],
        adapters={"XRP": StubXRP(), "GRC": adapter_class()},
    )

    out = capsys.readouterr().out
    assert "THIS WALLET'S OWN" not in out
    assert "VALID" in out


def test_an_adapter_without_owns_address_is_not_an_error(monkeypatch, tmp_path, capsys):
    """An adapter predating the method must not crash the dry run.

    hasattr rather than assuming, because this is the header of a read-only preview
    and a missing capability is not a reason to refuse a swap.
    """
    class NoSuchMethod(StubGRC):
        owns_address = None

        def __getattribute__(self, name):
            if name == "owns_address":
                raise AttributeError(name)
            return super().__getattribute__(name)

    stub_prices(monkeypatch)
    code = run_tool(
        monkeypatch,
        ["--pair", "XRP:GRC", "--amount", "1", "--payout-address", GRC_ADDRESS,
         "--db", str(tmp_path / "old.db")],
        adapters={"XRP": StubXRP(), "GRC": NoSuchMethod()},
    )

    assert code == 0
    assert "THIS WALLET'S OWN" not in capsys.readouterr().out
