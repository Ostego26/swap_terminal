"""chains/icp.ICPAdapter, exercised through an injected transport.

Role: tests (read-only)
Reads: swap_terminal.chains.icp
Writes: nothing
Can move funds: no -- every call goes to a function defined in this file
Mainnet-safe: yes. No container, no replica, no network, no dfx.

WHY A SEEDED TRANSPORT RATHER THAN A RUNNING LEDGER. The adapter's transport is a
parameter (see its module docstring for why it is dfx and not an HTTP client), so
the parsing, the refusals and the amount arithmetic can be called with seeded
inputs -- which is what rule 10 asks for and what the behavioral-verification
principle means by running the real code. The READ paths were additionally
exercised against the real released ICP ledger on the local replica on 2026-10-06
and agreed; what this file guards is that they keep agreeing, and that every
failure mode produces a refusal rather than a plausible number.

THE ASSERTION THAT MATTERS MOST is
test_output_this_adapter_cannot_read_raises_instead_of_returning_zero. On a payout
path, "this account holds nothing" and "the question could not be asked" lead to
opposite actions, and a regex that silently fails to match would make them the
same value -- which is rule 12's BLE001 defect with no except clause in sight.

No address literals: every principal and account identifier is derived by calling
chains/icp_account.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from swap_terminal.chains import icp as icp_module
from swap_terminal.chains.icp import ICP_DECIMALS, ICPAdapter, ICPCallFailed
from swap_terminal.chains.icp_account import account_identifier, principal_to_text, subaccount_from_index

LEDGER = "bkyz2-fmaaa-aaaaa-qaaaq-cai"
OWNER = principal_to_text(bytes.fromhex("00000000000000020101"))

#: What the real ledger answered on the local replica, 2026-10-06. Seeded here so
#: the arithmetic is checked against values a ledger actually produced.
REAL_BALANCE_E8S = 100_000_000_000
REAL_FEE_E8S = 10_000


def transport(responses: dict, log: list | None = None):
    """A `call` that returns seeded text per method, recording what it was asked."""

    def call(canister: str, method: str, argument: str) -> str:
        if log is not None:
            log.append((canister, method, argument))
        if method not in responses:
            raise AssertionError(f"the adapter called {method!r}, which this test did not seed")
        return responses[method]

    return call


def adapter(responses=None, log=None) -> ICPAdapter:
    seeded = responses if responses is not None else {
        "icrc1_balance_of": f"({REAL_BALANCE_E8S} : nat)",
        "icrc1_fee": f"({REAL_FEE_E8S} : nat)",
    }
    return ICPAdapter(LEDGER, OWNER, call=transport(seeded, log))


# -- construction -----------------------------------------------------------


def test_a_mistyped_owner_principal_refuses_construction():
    """Checked at construction because EVERY address depends on it.

    A mistyped principal makes every deposit address wrong in the same way, and the
    CRC32 inside a textual principal makes that detectable for free -- so the
    failure happens before anything can hand an address to a customer.
    """
    mistyped = OWNER[:-1] + ("b" if OWNER[-1] != "b" else "c")
    with pytest.raises(ValueError, match="canonical"):
        ICPAdapter(LEDGER, mistyped, call=transport({}))


def test_an_empty_ledger_id_refuses_construction():
    with pytest.raises(ValueError, match="no ledger"):
        ICPAdapter("", OWNER, call=transport({}))


# -- derivation -------------------------------------------------------------


def test_the_desks_own_address_is_its_default_subaccount():
    assert adapter().own_address() == account_identifier(OWNER)


@pytest.mark.parametrize("index", [0, -1])
def test_a_deposit_address_is_refused_for_index_below_one(index):
    """A second guard. db.py's CHECK is the guarantee; this catches a caller that
    derived an index some other way, and index 0 is the desk's own account."""
    with pytest.raises(ValueError, match="DESK'S OWN"):
        adapter().deposit_address(index)


def test_each_index_gives_a_distinct_address_and_none_is_the_desks_own():
    a = adapter()
    derived = {a.deposit_address(i) for i in (1, 2, 3)}
    assert len(derived) == 3
    assert a.own_address() not in derived
    assert a.deposit_address(1) == account_identifier(OWNER, subaccount_from_index(1))


def test_validate_address_checks_the_checksum_not_just_the_shape():
    """Any 64 hex characters pass a length-and-alphabet test, which is what a
    truncated copy-paste produces -- and on a payout that is unrecoverable."""
    a = adapter()
    good = a.own_address()
    assert a.validate_address(good)
    assert not a.validate_address(("0" if good[0] != "0" else "1") + good[1:])
    assert not a.validate_address(good[:-2])
    assert not a.validate_address(OWNER), "a principal is not an account identifier"


def test_owns_address_never_returns_false():
    """None, not False, matching SolanaAdapter for the same reason.

    This adapter cannot enumerate the desk's subaccounts, so it does not KNOW an
    arbitrary account is not the desk's -- and the payout-to-the-desk gate reads
    that answer. False would be a claim it cannot support.
    """
    a = adapter()
    assert a.owns_address(a.own_address()) is True
    assert a.owns_address(a.deposit_address(1)) is None
    assert a.owns_address(account_identifier(principal_to_text(bytes([4])))) is None


# -- reads ------------------------------------------------------------------


def test_the_balance_is_e8s_divided_by_the_ledgers_own_decimals():
    assert adapter().get_balance() == REAL_BALANCE_E8S / 10**ICP_DECIMALS == 1000.0


def test_the_default_account_and_a_subaccount_are_asked_as_different_arguments():
    """The argument is the whole difference between reading desk inventory and
    reading one customer's deposit, so it is asserted rather than assumed."""
    log: list = []
    a = adapter(log=log)
    a.get_balance()
    a.subaccount_balance(1)
    default_arg, subaccount_arg = log[0][2], log[1][2]
    assert "subaccount" not in default_arg
    assert "subaccount = opt vec" in subaccount_arg
    assert OWNER in default_arg and OWNER in subaccount_arg
    assert log[0][0] == log[1][0] == LEDGER


def test_the_fee_comes_from_the_ledger_and_is_not_a_constant():
    """Seeding a different fee must change the answer.

    That is the assertion that a hardcoded 10_000 would fail, and the reason the
    adapter reads icrc1_fee() on every call: agreement with mainnet today is not a
    licence to copy the number (rule 8).
    """
    assert adapter().chain_fee() == REAL_FEE_E8S / 10**ICP_DECIMALS == 0.0001
    moved = adapter({"icrc1_fee": "(25_000 : nat)"})
    assert moved.chain_fee() == 0.00025


def test_deposit_confirmations_is_one_and_says_it_is_a_compatibility_value():
    """ICP has no confirmation depth; the watcher asks anyway.

    Pinned so that nobody raises it to 6 by analogy with GRC, which would make
    every ICP deposit wait forever for blocks that carry no meaning.
    """
    a = adapter()
    assert a.deposit_confirmations() == 1
    # __doc__ IS Optional and not only in theory: `python -OO` discards every
    # docstring in the process, so this assertion read through a None there and
    # died with `AttributeError: 'NoneType' object has no attribute 'lower'`
    # instead of saying the method stopped explaining itself (pyright
    # reportOptionalMemberAccess, 2026-10-09). The claim being tested is
    # unchanged: the docstring has to say the word.
    explanation = a.deposit_confirmations.__doc__
    assert explanation is not None, "deposit_confirmations() must keep the docstring that says 1 is a compatibility value"
    assert "compatibility" in explanation.lower()


@pytest.mark.parametrize("junk", ["", "(variant { Err = 3 })", "nonsense", "( : nat)"])
def test_output_this_adapter_cannot_read_raises_instead_of_returning_zero(junk):
    """THE ONE THAT MATTERS. "Holds nothing" and "could not ask" are opposite actions.

    A regex that silently failed to match would make a broken call indistinguishable
    from an empty account: a payout gate would refuse a funded swap, or worse, a
    deposit watcher would report a paid customer as unpaid forever.
    """
    a = adapter({"icrc1_balance_of": junk, "icrc1_fee": junk})
    with pytest.raises(ICPCallFailed, match="not zero"):
        a.get_balance()
    with pytest.raises(ICPCallFailed, match="not zero"):
        a.chain_fee()


def test_underscores_in_the_candid_nat_are_parsed():
    """dfx prints 100_000_000_000, not 100000000000. Parsing the human form is the
    whole reason the regex exists rather than an int() call."""
    assert adapter({"icrc1_balance_of": "(1_234_567_890 : nat)"}).get_balance() == 12.3456789


# -- the path that moves money ---------------------------------------------


#: A fixed nanosecond timestamp, used because these tests never reach a ledger: the
#: same value twice is what dedup needs, and reading a clock here would test the
#: hazard rather than the fix.
#:
#: IT IS NOT A USABLE KEY AGAINST A REAL LEDGER, and that distinction cost a live
#: attempt. This exact constant is 2025-10-06 and the replica refused it with
#: TxTooOld / allowed_window_nanos = 86_400_000_000_000 -- 24 hours. A real caller
#: passes the swap's RECORDED CREATION TIME: a real timestamp, captured once, reused
#: on every retry. See send_to_address's docstring.
FIXED_NANOS = 1_759_700_000_000_000_000

OK_BLOCK = "(variant { Ok = 42 : nat64 })"


def sending_adapter(transfer_reply=OK_BLOCK, log=None) -> ICPAdapter:
    return adapter({"icrc1_fee": f"({REAL_FEE_E8S} : nat)", "transfer": transfer_reply}, log=log)


def test_a_send_without_an_idempotency_key_refuses_and_calls_the_ledger_ZERO_TIMES():
    """THE DEFAULT IS THE DANGEROUS ONE, so the default refuses.

    The ICP ledger deduplicates on (from, to, amount, fee, memo, created_at_time)
    for 24 hours and answers a repeat with TxDuplicate carrying the ORIGINAL block
    index -- but only when created_at_time is given. With it null the ledger stamps
    its own time, every retry is a NEW transaction, and a payout worker that times
    out and retries PAYS TWICE.

    Read from the deployed ledger's interface (rs/ledger_suite/icp/ledger.did), and
    the assertion that matters is the second one: a refusal that had already called
    the ledger would be a refusal reported over a transfer that happened.
    """
    log: list = []
    a = sending_adapter(log=log)
    with pytest.raises(ICPCallFailed, match="created_at_time unset"):
        a.send_to_address(a.deposit_address(1), 0.5)
    assert log == [], "a refused send must not have called the ledger at all"


def test_a_send_passes_the_fee_it_read_from_the_ledger_not_a_constant():
    """Legacy `transfer` REQUIRES the fee and rejects a mismatch with BadFee.

    So the fee is read first and seeded differently here: if the adapter carried a
    hardcoded 10_000 this would still pass against the real ledger today and fail
    the day a ledger changes its fee, which is rule 8's drift with money attached.
    """
    log: list = []
    a = adapter({"icrc1_fee": "(25_000 : nat)", "transfer": OK_BLOCK}, log=log)
    a.send_to_address(a.deposit_address(1), 0.5, created_at_time_nanos=FIXED_NANOS)
    methods = [m for _, m, _ in log]
    assert methods == ["icrc1_fee", "transfer"], "the fee must be read BEFORE the transfer"
    sent = log[1][2]
    assert "fee = record { e8s = 25000 : nat64 }" in sent


def test_the_destination_goes_out_as_a_32_BYTE_BLOB_not_as_text():
    # RAW DOCSTRING, because the quoted dfx error below contains \ee and \cc and a
    # non-raw string makes those invalid escape sequences. That surfaced as a
    # DeprecationWarning from tests/test_xrp_balances.py, which ast.parse()s every
    # .py in the tree -- so a bad escape anywhere becomes a warning in an unrelated
    # test with only "<unknown>:249" to locate it.
    r"""`type AccountIdentifier = blob` in the ledger's own interface.

    THIS TEST PASSED WHILE THE CODE WAS WRONG, and that is the lesson in it. The
    first version asserted the escapes and the byte count and never the `blob`
    KEYWORD, so it agreed with an argument that said `to = "\ee\cc..."` -- which
    candid reads as TEXT. dfx refused it on the live replica with "Not valid unicode
    text", after the test had reported the property in its own name.

    A test named for a property that does not assert the property is worse than no
    test, because the name is what a reader trusts. The keyword is asserted first
    here, before anything else about the literal.
    """
    log: list = []
    a = sending_adapter(log=log)
    destination = a.deposit_address(1)
    a.send_to_address(destination, 0.5, created_at_time_nanos=FIXED_NANOS)
    sent = log[1][2]
    expected = "".join(f"\\{b:02x}" for b in bytes.fromhex(destination))
    assert f'to = blob "{expected}"' in sent, (
        "the destination must carry the `blob` keyword -- a bare quoted string is candid `text` "
        "and dfx refuses 32 arbitrary bytes as UTF-8"
    )
    assert destination not in sent, "the 64-hex text must not appear; the blob does"
    assert sent.count("\\") == 32, "32 bytes, each escaped"


def test_the_amount_and_the_timestamp_are_carried_exactly():
    log: list = []
    a = sending_adapter(log=log)
    assert a.send_to_address(a.deposit_address(1), 0.5, created_at_time_nanos=FIXED_NANOS) == "42"
    sent = log[1][2]
    assert "amount = record { e8s = 50000000 : nat64 }" in sent
    assert f"created_at_time = opt record {{ timestamp_nanos = {FIXED_NANOS} : nat64 }}" in sent
    assert "from_subaccount = null" in sent, "payouts leave the desk's default account"


@pytest.mark.parametrize("reply,needle", [
    ("(variant { Err = variant { BadFee = record { expected_fee = record { e8s = 10_000 : nat64 } } } })", "BadFee"),
    ("(variant { Err = variant { InsufficientFunds = record { balance = record { e8s = 1 : nat64 } } } })", "NOT established"),
    ("(variant { Err = variant { TxTooOld = record { allowed_window_nanos = 86_400_000_000_000 : nat64 } } })", "24h"),
    ("", "NOT established"),
])
def test_anything_but_a_block_index_raises_and_does_not_claim_nothing_moved(reply, needle):
    """A failed send must not assert that no funds moved, because it does not know.

    TxDuplicate in particular means an earlier identical transfer DID happen and the
    reply carries its block index. A message saying "nothing was sent" there would
    be false, and a BadFee is deliberately NOT retried at the expected figure --
    a retry is a second send, and this method must never make two where one was
    asked for.
    """
    a = sending_adapter(transfer_reply=reply)
    with pytest.raises(ICPCallFailed, match=needle):
        a.send_to_address(a.deposit_address(1), 0.5, created_at_time_nanos=FIXED_NANOS)


def test_a_duplicate_is_SUCCESS_and_returns_the_original_block_index():
    """THE DEFECT THIS FILE SHIPPED, and the live run is what exposed it.

    TxDuplicate means "this exact transfer already happened; its block index is
    <n>". That is precisely what the idempotency key exists to produce, so a retry
    after a timeout must get the SAME answer the first call gave.

    The first version RAISED here, with a message telling the caller to read it as
    success -- which put the decision in prose a payout worker would have had to
    parse. The failure mode is specific: a worker retrying after a timeout sees an
    exception and marks a payout that SUCCEEDED as failed.

    Measured on the local replica 2026-10-06: the same call twice left desk 999.7499
    and subaccount 1 at 0.25 both times, the second answering TxDuplicate. The funds
    did not move twice, so an exception there would have been reporting a failure
    that did not occur.
    """
    a = sending_adapter(
        transfer_reply="(variant { Err = variant { TxDuplicate = record { duplicate_of = 1 : nat64 } } })"
    )
    assert a.send_to_address(a.deposit_address(1), 0.25, created_at_time_nanos=FIXED_NANOS) == "1"


def test_the_same_call_twice_returns_the_same_block_index():
    """What idempotency means at this boundary, asserted end to end.

    First call Ok, second call TxDuplicate, and the caller cannot tell them apart --
    which is the entire point. A payout worker that cannot distinguish "I just paid"
    from "I already paid" cannot double-pay by retrying.
    """
    first = sending_adapter(transfer_reply="(variant { Ok = 1 : nat64 })")
    retry = sending_adapter(
        transfer_reply="(variant { Err = variant { TxDuplicate = record { duplicate_of = 1 : nat64 } } })"
    )
    destination = first.deposit_address(1)
    assert first.send_to_address(destination, 0.25, created_at_time_nanos=FIXED_NANOS) == "1"
    assert retry.send_to_address(destination, 0.25, created_at_time_nanos=FIXED_NANOS) == "1"


def test_a_payout_to_an_invalid_address_refuses_before_reading_the_fee():
    log: list = []
    a = sending_adapter(log=log)
    with pytest.raises(ICPCallFailed, match="NOTHING was sent"):
        a.send_to_address(OWNER, 0.5, created_at_time_nanos=FIXED_NANOS)
    assert log == []


# ---------------------------------------------------------------------------
# The ledger's own derivation, as a cross-check on ours
# ---------------------------------------------------------------------------


def ledger_blob(hex_text: str) -> str:
    return '(blob "' + "".join(f"\\{b:02x}" for b in bytes.fromhex(hex_text)) + '")'


def test_the_ledger_and_this_repository_derive_the_same_account():
    """The one assumption under every ICP address this terminal publishes.

    `account_identifier : (Account) -> (AccountIdentifier) query` makes the ledger do
    the derivation itself, so agreement is checkable for one query call. A
    disagreement would mean a customer pays an address the ledger credits to
    something else, and the watcher polls an account that stays at zero forever.
    """
    a = adapter({"account_identifier": ledger_blob(account_identifier(OWNER))})
    agrees, why = a.verify_derivation()
    assert agrees is True
    assert account_identifier(OWNER) in why


def test_a_disagreement_is_reported_as_total_rather_than_as_one_bad_address():
    """Both sides are printed, and the message says every address is wrong."""
    a = adapter({"account_identifier": ledger_blob(account_identifier(OWNER, subaccount_from_index(9)))})
    agrees, why = a.verify_derivation()
    assert agrees is False
    assert "DISAGREEMENT" in why
    assert "wrong" in why and "do not send" in why


def test_a_blob_this_adapter_cannot_read_raises_rather_than_returning_empty():
    a = adapter({"account_identifier": "(variant { Err = 1 })"})
    with pytest.raises(ICPCallFailed, match="not empty"):
        a.ledger_account_identifier()


# ---------------------------------------------------------------------------
# Deposit detection: blocks, not balances
# ---------------------------------------------------------------------------


def blocks_page(entries, *, first=0, chain_length=None, archived=()):
    """A query_blocks reply in the JSON shape dfx --output json actually emits.

    Copied from the live reply rather than imagined: a blob is a LIST OF INTEGERS,
    every number is a STRING, and `operation` is an OPTIONAL variant so it arrives as
    a list of zero or one one-key object.
    """
    return {
        "chain_length": str(chain_length if chain_length is not None else first + len(entries)),
        "first_block_index": str(first),
        "archived_blocks": list(archived),
        "blocks": [
            {"transaction": {"operation": [{kind: {"to": list(bytes.fromhex(to)),
                                                   "amount": {"e8s": str(e8s)}}}]}}
            for kind, to, e8s in entries
        ],
    }


def scanning_adapter(page, log=None) -> ICPAdapter:
    """An adapter whose JSON calls all return `page`."""
    def call(canister, method, argument, output="idl"):
        if log is not None:
            log.append((method, argument, output))
        return json.dumps(page) if output == "json" else f"({REAL_FEE_E8S} : nat)"
    return ICPAdapter(LEDGER, OWNER, call=call)


def test_a_transfer_into_the_address_becomes_one_event_keyed_on_the_block_index():
    """The block index is the txid, and that choice is the whole design.

    deposit_events is keyed (asset, txid, vout) and refresh_swap_from_chain SUMS every
    row. A balance reported as an event would either collide with the first row -- the
    update path does not touch amount, so a second payment would be invisible -- or
    arrive as a new row that sums with it, crediting 0.75 for a 0.5 balance. A block
    index is unique per payment and never reused.
    """
    sub = account_identifier(OWNER, subaccount_from_index(1))
    a = scanning_adapter(blocks_page([("Transfer", sub, 25_000_000)]))
    assert a.find_deposits_to_address(sub) == [
        {"txid": "0", "vout": 0, "address": sub, "amount": 0.25, "confirmations": 1}
    ]


def test_a_MINT_is_not_a_deposit():
    """The local ledger's first block is a Mint of the desk's entire opening supply.

    Counting it would credit a swap with 1000 ICP of desk inventory. Measured on the
    replica: block 0 is `Mint` to the desk for 100_000_000_000 e8s, which is exactly
    what the init arguments granted.
    """
    desk = account_identifier(OWNER)
    a = scanning_adapter(blocks_page([("Mint", desk, 100_000_000_000)]))
    assert a.find_deposits_to_address(desk) == []


def test_a_transfer_to_a_DIFFERENT_subaccount_is_not_this_swaps_deposit():
    """One customer's payment must not be attributed to another's swap."""
    mine = account_identifier(OWNER, subaccount_from_index(1))
    theirs = account_identifier(OWNER, subaccount_from_index(2))
    a = scanning_adapter(blocks_page([("Transfer", theirs, 25_000_000)]))
    assert a.find_deposits_to_address(mine) == []


def test_the_block_index_accounts_for_first_block_index():
    """A window into the chain does not start at 0, and an off-by-N here mislabels every txid."""
    sub = account_identifier(OWNER, subaccount_from_index(1))
    a = scanning_adapter(blocks_page([("Transfer", sub, 1)], first=97, chain_length=98))
    assert [e["txid"] for e in a.find_deposits_to_address(sub)] == ["97"]


def test_already_recorded_txids_are_skipped():
    sub = account_identifier(OWNER, subaccount_from_index(1))
    a = scanning_adapter(blocks_page([("Transfer", sub, 1), ("Transfer", sub, 2)]))
    assert [e["txid"] for e in a.find_deposits_to_address(sub)] == ["0", "1"]
    # A frozenset, which is what every real caller hands in:
    # services/deposit_service.skip_txids() is annotated `-> frozenset[str]` and
    # is the only producer of this argument in the tree. The parameter defaults
    # to frozenset() too, so a bare `set` was the one shape nothing passes
    # (pyright reportArgumentType, 2026-10-09).
    assert [e["txid"] for e in a.find_deposits_to_address(sub, skip_txids=frozenset({"0"}))] == ["1"]


def test_archived_blocks_RAISE_rather_than_being_scanned_past():
    """THE ONE THAT WOULD LOSE A CUSTOMER'S MONEY.

    Old blocks migrate off the ledger into archive canisters; `blocks` then covers only
    what the ledger still holds and `archived_blocks` names the ranges that moved. A
    scan that ignored it would silently miss deposits -- the customer paid, the watcher
    polls forever, and nothing errors, which is indistinguishable from not paying.

    Measured on the local replica: archived_blocks is empty with chain_length 2, so
    this refusal has never fired there. That is precisely why it is written now.
    """
    sub = account_identifier(OWNER, subaccount_from_index(1))
    a = scanning_adapter(blocks_page([("Transfer", sub, 1)], archived=[{"start": "0", "length": "1"}]))
    with pytest.raises(ICPCallFailed, match="never be seen"):
        a.find_deposits_to_address(sub)


def test_an_empty_ledger_yields_no_deposits_without_a_second_call():
    log: list = []
    a = scanning_adapter(blocks_page([], chain_length=0), log=log)
    assert a.find_deposits_to_address(account_identifier(OWNER)) == []
    assert len(log) == 1, "chain_length 0 needs no window scan"


def test_an_invalid_address_refuses_rather_than_reporting_no_deposits():
    """"No deposits" for an unreadable address looks exactly like an unpaid customer."""
    a = scanning_adapter(blocks_page([]))
    with pytest.raises(ICPCallFailed, match="no scan was attempted"):
        a.find_deposits_to_address(OWNER)


def test_output_that_is_not_json_raises_rather_than_being_read_as_empty():
    def call(canister, method, argument, output="idl"):
        return "not json at all"
    a = ICPAdapter(LEDGER, OWNER, call=call)
    with pytest.raises(ICPCallFailed, match="not an empty result"):
        a.find_deposits_to_address(account_identifier(OWNER))


def test_get_new_address_REFUSES_because_the_index_must_come_from_SQL():
    """Returning anything address-shaped here is silent and costs a deposit.

    own_address() would publish the DESK'S OWN account, mixing a customer's payment
    into desk inventory. A fixed subaccount would attribute every customer's payment
    to whichever swap was checked first.
    """
    a = scanning_adapter(blocks_page([]))
    with pytest.raises(ICPCallFailed, match="allocated in SQL"):
        a.get_new_address("swap_s_abc")


# --- the transport's own working directory, which is not the one it was measured in


def test_the_compose_files_the_transport_names_exist_from_ANY_directory(tmp_path, monkeypatch):
    """Found 2026-10-06, before the first ICP swap was attempted through the UI.

    dfx_transport() built `-f docker-compose.yml -f docker-compose.icp.yml` and passed
    no cwd, so both resolved against the CALLING process's working directory. The app
    runs with cwd=swap_terminal/ -- forced, because app.py does `from routes.ui import
    bp` -- and neither file is there. Every ICP call from the web app or a worker would
    have failed with compose's "no configuration file provided", which names the file
    and not the reason.

    Nothing caught it because every measurement behind this adapter was taken from an
    operator shell at the repository root, which is the one working directory the app
    never has.

    The test chdirs somewhere with no compose file at all and asserts the paths the
    transport NAMES are real files. That is the property; a cwd-relative path cannot
    satisfy it.
    """
    monkeypatch.chdir(tmp_path)
    assert not (tmp_path / "docker-compose.yml").exists(), "the fixture has to be a directory without one"

    recorded = {}

    def fake_run(argv, **kwargs):
        recorded["argv"] = argv
        recorded["cwd"] = kwargs.get("cwd")
        raise AssertionError("argv captured; no docker is run in this test")

    monkeypatch.setattr(icp_module.subprocess, "run", fake_run)
    call = icp_module.dfx_transport("icp-replica", 5.0)
    with pytest.raises(AssertionError, match="argv captured"):
        call("a-canister", "a_method", "()")

    argv = recorded["argv"]
    named = [argv[i + 1] for i, element in enumerate(argv) if element == "-f"]
    # ONE `-f`, AND IT WAS TWO UNTIL 2026-10-10. The literal `2` that used to be
    # asserted here was the count of files, not the property this test is about:
    # docker-compose.yml gained an `include:` of docker-compose.icp.yml, so the
    # second `-f` would have been the same file arriving twice and _COMPOSE_FILES
    # was reduced to one entry (its comment says why, and why not guessing what
    # compose does with a doubled file was the point).
    #
    # ASSERTED AGAINST _COMPOSE_FILES RATHER THAN AGAINST A NEW LITERAL. A number
    # written here has to be edited every time that tuple changes and says nothing
    # when it is wrong; comparing the two means the test fails only if the transport
    # stops passing what it declares -- which is the actual property. The literal
    # that remains is `>= 1`, because an argv with no compose file at all is the
    # 2026-10-06 defect returning as "no configuration file provided".
    assert named, f"the exec transport passed no -f at all: {argv}"
    assert len(named) == len(icp_module._COMPOSE_FILES), (
        f"the transport passed {len(named)} compose file(s) and _COMPOSE_FILES declares "
        f"{len(icp_module._COMPOSE_FILES)}: {argv}"
    )
    for path in named:
        assert Path(path).is_absolute(), f"{path} is relative, so it depends on the caller's cwd"
        assert Path(path).is_file(), f"{path} is not a file from {tmp_path}"

    # And the cwd, which is the other half: compose resolves the paths INSIDE those
    # files (build contexts, the ./icp:/repo mount) against the process's directory.
    assert recorded["cwd"] == icp_module._REPO_ROOT
    assert (Path(recorded["cwd"]) / "docker-compose.yml").is_file()


# --- the two transports, and which one a given deployment can use


def _captured_argv(network_url, monkeypatch):
    """Build a transport, call it, and return the argv it would have run."""
    recorded = {}

    def fake_run(argv, **kwargs):
        recorded["argv"] = argv
        recorded["cwd"] = kwargs.get("cwd")
        raise AssertionError("argv captured")

    monkeypatch.setattr(icp_module.subprocess, "run", fake_run)
    call = icp_module.dfx_transport("icp-replica", 5.0, network_url)
    with pytest.raises(AssertionError, match="argv captured"):
        call("a-canister", "a_method", "()")
    return recorded


def test_the_URL_transport_runs_dfx_DIRECTLY_with_no_docker(monkeypatch):
    """The transport the web container needs, because it has no docker at all.

    Measured 2026-10-06/07: docker/web.Dockerfile's runtime stage installs only
    dumb-init, and no compose file mounts /var/run/docker.sock. So the containerized
    deployment -- the one that serves the UI under gunicorn with nothing held in a
    terminal, which is what the operator asked for -- could not make an ICP call.
    Mounting the daemon socket would fix it and must not be done: it hands a process
    holding wallet RPC credentials control of the whole Docker daemon.

    The asserted property is that `docker` appears NOWHERE in the argv, which is the
    only thing that makes the call possible in that image -- and that no cwd is
    pinned, because nothing is resolved relative to anything: dfx just opens an HTTP
    connection to the url.
    """
    recorded = _captured_argv("http://icp-replica:4943", monkeypatch)
    argv = recorded["argv"]

    assert argv[0] == "dfx", argv
    assert not any("docker" in element for element in argv), (
        f"the url transport must not invoke docker; got {argv}"
    )
    assert "--network" in argv
    assert argv[argv.index("--network") + 1] == "http://icp-replica:4943"
    assert recorded["cwd"] is None, "nothing is resolved relative to a directory here"

    # --identity anonymous, AND ITS ABSENCE PRINTED A SEED PHRASE. Measured
    # 2026-10-07 on the first real call through this transport from the web
    # container: dfx had no identity, so it CREATED one, wrote
    # /home/swap/.config/dfx/identity/default/identity.pem, and echoed the 24-word
    # mnemonic on stdout -- from a read-only fee lookup. Two defects at once: key
    # material generated and displayed by a read path, and a read path writing to
    # the filesystem on first use.
    #
    # Anonymous is the CORRECT identity here and that is measured, not assumed: the
    # operator ran `dfx --identity anonymous ... icrc1_fee` against their replica
    # and got (10_000 : nat). Every method this transport reaches is public.
    assert "--identity" in argv, (
        "without an explicit identity dfx CREATES one on first use and prints its seed phrase"
    )
    assert argv[argv.index("--identity") + 1] == "anonymous"


def test_the_COMPOSE_transport_is_unchanged_and_still_the_default(monkeypatch):
    """An empty url keeps the transport that has actually been exercised live.

    Empty by default on purpose: a plausible default is the dangerous kind here, and
    the compose path is the one measured against this operator's replica -- the
    0.25 ICP transfer, the idempotent retry, the block-based deposit read. Switching
    transports silently on an unset variable would move every one of those onto a
    path nothing has run.
    """
    recorded = _captured_argv("", monkeypatch)
    argv = recorded["argv"]

    assert argv[:2] == ["docker", "compose"], argv
    assert "exec" in argv and "icp-replica" in argv
    assert "--network" not in argv, "the compose transport reaches the replica by exec, not by url"
    assert "--identity" not in argv, (
        "the compose transport runs INSIDE the replica container, where the desk's own identity "
        "is the default and is the one a * -> ICP payout must sign with. Forcing anonymous here "
        "would make every ICP payout debit an empty account"
    )
    # WITH AN IDENTITY ASKED FOR, THIS TRANSPORT DOES PASS ONE, and that is tested
    # in tests/test_fund_desk.py::test_the_mint_identity_reaches_dfx_and_the_read_
    # identity_does_not rather than here -- named at both sites (rule 8) because the
    # assertion above reads as "this transport never signs", which has been the
    # whole truth only until 2026-10-07. dfx_transport() gained an `identity`
    # parameter for fund_desk.py's ICP mint, which must sign as `minter`; EMPTY
    # still means no flag, which is what this assertion is about and what keeps
    # payouts signing as the container's default.
    assert recorded["cwd"] == icp_module._REPO_ROOT, (
        "compose resolves the relative paths INSIDE its files against the cwd"
    )


# --- key material in dfx's own output, which this adapter must never pass on


_DFX_BOOTSTRAP = '''Creating the "default" identity.
WARNING: The "default" identity is not stored securely.
  - generating new key at /home/swap/.config/dfx/identity/default/identity.pem
Your seed phrase: inherit decrease finish rotate under town inquiry cotton spend home into hawk
This can be used to reconstruct your key in case of emergency.
Created the "default" identity.
(10_000 : nat)'''


def test_a_mnemonic_from_dfx_NEVER_reaches_a_caller():
    """Measured twice on 2026-10-07, the second time with --identity anonymous already set.

    dfx bootstraps its identity store on first run in a fresh container and prints the
    24-word mnemonic, BEFORE it honors --identity. So the flag was necessary --
    anonymous is the correct caller for every read this transport makes -- and it is
    not sufficient: it cannot stop dfx writing that line.

    What this repository DOES control is whether the line is passed on, and it was
    not: ICPCallFailed embedded dfx's output verbatim, so a mnemonic went into an
    exception message, a worker log and the operator's terminal -- from a read-only
    fee lookup. The operator's standing instruction is that a key is never moved,
    copied, read back or echoed, and a secret arriving from a subprocess is still a
    secret.

    The whole LINE is dropped rather than the words masked: a partial mnemonic is a
    reduced search space, and nothing downstream needs any part of it.
    """
    cleaned = icp_module.redact_secrets(_DFX_BOOTSTRAP)

    for word in ("inherit decrease finish", "seed phrase", "identity.pem"):
        assert word not in cleaned, f"{word!r} survived redaction: {cleaned!r}"
    assert "(10_000 : nat)" in cleaned, "the actual answer must survive -- this is on the read path"
    assert "withheld" in cleaned, (
        "a silently shortened message leaves a reader wondering what they are not being told, "
        "and the fact that dfx emitted a key is itself diagnostic (rule 14)"
    )


def test_redaction_leaves_ordinary_output_untouched():
    """It must not eat a real answer, or every ICP read becomes a parse failure."""
    plain = "(10_000 : nat)"
    assert icp_module.redact_secrets(plain) == plain

    multi = "WARN: Cannot fetch Candid interface for icrc1_fee\n(10_000 : nat)"
    assert icp_module.redact_secrets(multi) == multi


def test_the_transport_redacts_what_it_RETURNS_and_what_it_RAISES(monkeypatch):
    """Both paths, because dfx chooses which stream its bootstrap goes to.

    Behavioral rather than a check that redact_secrets is called somewhere: a stub
    subprocess returns the bootstrap text on stdout for the success case and on
    stderr for the failure case, and the mnemonic must appear in neither outcome.
    """
    class _Done:
        def __init__(self, code, out, err):
            self.returncode, self.stdout, self.stderr = code, out, err

    monkeypatch.setattr(icp_module.subprocess, "run", lambda *a, **k: _Done(0, _DFX_BOOTSTRAP, ""))
    call = icp_module.dfx_transport("icp-replica", 5.0, "http://icp-replica:4943")
    assert "inherit decrease finish" not in call("a-canister", "icrc1_fee", "()")

    monkeypatch.setattr(icp_module.subprocess, "run", lambda *a, **k: _Done(255, "", _DFX_BOOTSTRAP))
    call = icp_module.dfx_transport("icp-replica", 5.0, "http://icp-replica:4943")
    with pytest.raises(ICPCallFailed) as raised:
        call("a-canister", "icrc1_fee", "()")
    assert "inherit decrease finish" not in str(raised.value)
    assert "seed phrase" not in str(raised.value)
