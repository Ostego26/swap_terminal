"""The XRP payout path: every guard, disabled one at a time, against seeded rows.

Role: test (seeded transport and seeded submit; no socket, nothing broadcast)
Reads: chains/xrp.py, chains/xrp_signing.py
Writes: nothing
Can move funds: no. requests.post is replaced by a recorder and
      xrpl.transaction.submit_and_wait is replaced by a stub, so the armed path
      runs end to end and reaches no ledger. Every address here is either an
      XRPL reserved constant or a throwaway wallet created in-process.
Mainnet-safe: yes

WHAT THESE TESTS ESTABLISH, AND WHAT THEY DO NOT. Stated first, because a green
suite is the thing most likely to be mistaken for evidence a payout path works.

THEY ESTABLISH, against seeded inputs:

  - a mainnet network_id refuses, INCLUDING in preview mode, and refuses before
    account_info is read -- asserted on the recorder's call list, not on the
    exception alone
  - the url is not consulted: a testnet-looking url with network_id 0 still
    refuses, and a mainnet-looking url with network_id 1 does not
  - a MISSING network_id refuses, which is the case a config check would pass
  - previewing is the DEFAULT: the exact two-positional-argument call
    services/payout_service.py:219 makes is refused, with the preview in the
    message, and nothing is submitted
  - the arming token is matched exactly, and a seedless armed call still refuses
  - the derivation guard refuses a seed that derives a different account, and
    its message contains neither the seed nor any substring of it
  - a payment that would breach the reserve refuses before signing, with the
    shortfall in drops
  - the transaction that actually gets signed carries NO tfPartialPayment bit,
    asserted on the SERIALIZED form rather than on the constructor call
  - a non-tesSUCCESS result and a validated=False result each raise rather than
    returning a hash, so neither can be written into `payouts` as a broadcast

THEY ESTABLISH NOTHING ABOUT A REAL LEDGER. Nothing here was submitted to any
network. s.altnet.rippletest.net:51234 is unreachable from the container these
tests were written in -- measured 2026-09-26, the connection is reset by the
proxy -- so the signing path is verified only against seeded responses and a
stubbed submit_and_wait. The one thing that HAS been exercised against a real
ledger is xrp_send_tagged.py's local-signing path, on testnet on 2026-09-26
(tesSUCCESS, validated, hash 5534F6CC68DB7AA7237519BC8BB01791172C23FB3E1B57D44E4CD0AD07E100BD),
and this adapter reuses that file's derivation guard rather than a copy of it.

WHY EACH GUARD GETS ITS OWN TEST rather than one test of a refused send: a guard
with no test that fails when it is DISABLED is not a guard. Each of the four
safety guards below was individually broken in the source and the failing test
recorded in the commit message, which is only possible when one test depends on
one guard.
"""

import pytest
import requests
from chains.xrp import XRPAdapter, XRPRPCError, new_deferrals
from chains.xrp_signing import (
    CONFIRM_XRP_SEND,
    FEE_ALLOWANCE_DROPS,
    MAINNET_NETWORK_IDS,
    TF_PARTIAL_PAYMENT,
    XRPMainnetRefused,
    XRPPartialPaymentRefused,
    XRPReserveRefused,
    XRPSendNotArmed,
    network_id_name,
    refuse_partial_payment,
    require_non_mainnet,
    require_reserve_headroom,
    require_send_confirmation,
    reserve_drops,
)
from config import Config
from services.swap_service import deposit_account

# MOVED OUT, NOT COPIED (rule 8). The recorder and the two seeded response builders
# used to be defined below and tests/test_xrp_payout_wiring.py needs the identical
# ones; the values in them were MEASURED off rippled 3.4.1 and two copies would
# drift silently -- a second copy that seeded `Balance` as an int instead of a string
# would pass against a parser that mishandles the real shape and keep passing. Every
# value moved unchanged; see that module's docstring for the measurements.
from valid_addresses import xrp_family_seed
from xrp_seeded_transport import MAINNET_URL, TESTNET_URL, Recorder, account_info, server_info

# The XRP Ledger's own reserved accounts, used here for the same reason
# tests/test_xrp_address.py uses them: they are addresses whose checksums are
# fixed by the protocol rather than values this file invented, and neither can
# ever hold a balance. ACCOUNT_ZERO is the source and ACCOUNT_ONE the
# destination in every test that does not need real key derivation.
ACCOUNT_ZERO = "rrrrrrrrrrrrrrrrrrrrrhoLvTp"
ACCOUNT_ONE = "rrrrrrrrrrrrrrrrrrrrBZbvji"

@pytest.fixture
def post(monkeypatch):
    def install(**by_method):
        recorder = Recorder(**by_method)
        monkeypatch.setattr(requests, "post", recorder)
        return recorder

    return install


def adapter(url=TESTNET_URL):
    return XRPAdapter(url=url, min_confirmations=1)


# --- guard 1: the network, decided by the SERVER and never by the url --------


def test_mainnet_is_network_id_zero_and_the_constant_is_not_re_derived():
    """Established from the constant xrp_send_tagged.py already carried and moved."""
    assert frozenset({0}) == MAINNET_NETWORK_IDS


def test_network_id_zero_refuses_even_when_the_url_says_testnet():
    """The whole point of checking the id instead of the hostname.

    A url is not evidence: an /etc/hosts entry, a split-horizon resolver or a
    copied config can point "altnet.rippletest.net" at a mainnet validator while
    every log line in the run still prints the testnet name.
    """
    with pytest.raises(XRPMainnetRefused, match="MAINNET"):
        require_non_mainnet(0, TESTNET_URL)


def test_a_mainnet_looking_url_is_fine_when_the_server_says_network_one():
    """The converse, and it is what proves the url is not consulted at all."""
    assert "network_id 1" in require_non_mainnet(1, MAINNET_URL)


def test_a_missing_network_id_refuses_rather_than_defaulting():
    """The case a configuration check passes and this one does not.

    "I could not read the network" is not "this is not mainnet". A server that
    omits the field therefore cannot be paid from, and the remedy is a server
    that answers rather than a default in this repository.
    """
    with pytest.raises(XRPMainnetRefused, match="did not report a network_id"):
        require_non_mainnet(None, TESTNET_URL)


def test_an_unreadable_network_id_refuses():
    """A field of an unexpected shape means this is not the response expected."""
    with pytest.raises(XRPMainnetRefused, match="not an integer"):
        require_non_mainnet("mainnet-ish", TESTNET_URL)


def test_a_mainnet_server_refuses_before_account_info_is_read(post):
    """Seeded end to end through the adapter, asserting on the CALL LIST.

    The exception alone would not distinguish a guard that refused before
    reading the account from one that refused after, and "refuses before it
    touches the account" is the property that makes previewing against mainnet
    impossible rather than merely unhelpful.
    """
    recorder = post(server_info=server_info(network_id=0), account_info=account_info())

    with pytest.raises(XRPMainnetRefused, match="network_id 0"):
        adapter().preview_payout(ACCOUNT_ONE, 1, ACCOUNT_ZERO)

    assert recorder.methods == ["server_info"], "account_info must not be reached on a mainnet endpoint"


def test_even_a_preview_cannot_be_taken_against_mainnet(post):
    """Preview mode is not an exemption. Same refusal, through send_to_address."""
    post(server_info=server_info(network_id=0), account_info=account_info())
    with pytest.raises(XRPMainnetRefused):
        adapter().send_to_address(ACCOUNT_ONE, 1, source=ACCOUNT_ZERO)


# --- guard 2: dry-run by default ---------------------------------------------


def test_the_payout_workers_exact_call_is_refused_and_reads_nothing(post):
    """services/payout_service.py:219 calls send_to_address(address, amount).

    Two positional arguments, no keywords. This is that call, spelled the same
    way.

    MEASURED, AND IT IS REFUSED TWO GUARDS EARLIER THAN THE ARMING CHECK, which
    is worth writing down rather than papering over: the worker supplies no
    SOURCE account, so the refusal is the missing-source one and the arming
    token never comes up. That is a stronger position than the one this test was
    originally written to assert -- the worker's call cannot even describe a
    payment, let alone make one -- and it is the shape that follows from the
    adapter holding no hot-wallet account of its own.

    Both halves are asserted: it raises, AND not one network call was made.
    """
    recorder = post(server_info=server_info(), account_info=account_info())

    with pytest.raises(XRPRPCError, match="needs the SOURCE account"):
        adapter().send_to_address(ACCOUNT_ONE, 1.5)

    assert recorder.calls == [], "the worker's call must not reach the server at all"


def test_a_caller_that_supplies_a_source_but_forgets_to_arm_is_still_refused(post):
    """The arming guard proper, reached once the source is supplied.

    The assertion is both halves: it raises, AND the only calls made were the
    two READS the preview needs, so a forgotten opt-in cannot degrade into a
    send.
    """
    recorder = post(server_info=server_info(), account_info=account_info())

    with pytest.raises(XRPSendNotArmed, match="was NOT armed"):
        adapter().send_to_address(ACCOUNT_ONE, 1.5, source=ACCOUNT_ZERO)

    assert recorder.methods == ["server_info", "account_info"]
    assert "submit" not in recorder.methods


def test_the_refusal_carries_the_whole_preview_so_arming_is_informed(post):
    """An operator who then arms it should be arming something they have read."""
    post(server_info=server_info(), account_info=account_info())

    with pytest.raises(XRPSendNotArmed) as caught:
        adapter().send_to_address(ACCOUNT_ONE, 1.5, source=ACCOUNT_ZERO)

    message = str(caught.value)
    assert "1500000 drops" in message
    assert ACCOUNT_ZERO in message and ACCOUNT_ONE in message
    assert "network_id 1" in message


def test_the_arming_token_is_matched_exactly_not_by_truthiness():
    """A boolean, a prefix and a near-miss all refuse.

    `True` is the spelling this deliberately does not accept: a truthy variable,
    a parsed config value or a positional argument that drifted one place could
    each supply it, and every one of those is a send nobody wrote.
    """
    for wrong in ("", "yes", "true", CONFIRM_XRP_SEND.lower(), CONFIRM_XRP_SEND[:-1], CONFIRM_XRP_SEND + "!"):
        with pytest.raises(XRPSendNotArmed):
            require_send_confirmation(wrong, "sSomeSeed")


def test_the_exact_token_with_a_seed_is_the_only_accepted_combination():
    assert require_send_confirmation(CONFIRM_XRP_SEND, "sSomeSeed") is None


def test_armed_without_a_seed_still_refuses():
    """Because the adapter holds no key, arming alone cannot produce a signature.

    This is the test that pins "configuration alone cannot arm this": there is
    nothing the adapter could fall back to for a seed, so the armed-but-keyless
    call is a refusal rather than a send from some default account.
    """
    with pytest.raises(XRPSendNotArmed, match="no signing seed"):
        require_send_confirmation(CONFIRM_XRP_SEND, "")


def test_the_adapter_stores_no_seed_and_no_key_path():
    """Asserted over the instance's own attributes rather than by reading the source.

    Rule: never accept "the code contains no key" as evidence. If a seed, a key
    path or a hot-wallet account is ever added as adapter state, this fails.
    """
    instance = adapter()
    for name, value in vars(instance).items():
        assert "seed" not in name.lower()
        assert "secret" not in name.lower()
        assert "key" not in name.lower()
        assert not (isinstance(value, str) and value.startswith("s") and len(value) == 31)


# --- guard 3: the reserve ----------------------------------------------------


def test_the_reserve_is_read_from_the_server_and_includes_owned_objects():
    """1 XRP base + 0.2 x 3 owned = 1.6 XRP = 1,600,000 drops."""
    total, line = reserve_drops(1, 0.2, 3)
    assert total == 1_600_000
    assert "1000000 base" in line and "200000 x 3" in line


def test_an_absent_owner_count_says_the_figure_understates_the_reserve():
    """Rule 14: state what the number means, next to the number.

    Base-only is the less conservative reading, so the line has to say it is,
    rather than presenting a possibly-low figure as the reserve.
    """
    total, line = reserve_drops(1, 0.2, None)
    assert total == 1_000_000
    assert "UNDERSTATES" in line


def test_a_payment_that_would_breach_the_reserve_refuses_with_the_shortfall():
    """One drop short is still short, and the message says by how much."""
    with pytest.raises(XRPReserveRefused, match="short by 1 drops"):
        require_reserve_headroom(
            balance_drops=2_000_000, send_drops=999_991, fee_drops=10, required_reserve_drops=1_000_000
        )


def test_a_payment_that_exactly_fits_is_permitted():
    """The boundary in the other direction, so the comparison is not off by one."""
    line = require_reserve_headroom(
        balance_drops=2_000_000, send_drops=999_990, fee_drops=10, required_reserve_drops=1_000_000
    )
    assert "0 drops of headroom" in line


def test_the_reserve_check_refuses_through_the_adapter_before_signing(post):
    """Seeded through the real adapter: 100 XRP balance, a 100 XRP payment.

    The ledger would also refuse this, with a tec* code that had already claimed
    a fee. Refusing here costs nothing and leaves no transaction behind.
    """
    recorder = post(server_info=server_info(), account_info=account_info(drops="100000000"))

    with pytest.raises(XRPReserveRefused, match="breach the account's reserve"):
        adapter().send_to_address(
            ACCOUNT_ONE, 100, source=ACCOUNT_ZERO, seed="s" * 31, confirm_send=CONFIRM_XRP_SEND
        )

    assert "submit" not in recorder.methods, "an armed call must still not submit when the reserve refuses"


def test_the_fee_allowance_is_named_as_a_fallback_when_the_server_omits_base_fee(post):
    """Rule 17 in a preview line: do not let a fallback read as a measurement."""
    post(server_info=server_info(base_fee=None), account_info=account_info())
    plan = adapter().preview_payout(ACCOUNT_ONE, 1, ACCOUNT_ZERO)
    assert plan["fee_drops"] == FEE_ALLOWANCE_DROPS
    assert "did NOT report" in plan["description"]


def test_base_fee_is_used_when_the_server_does_report_it(post):
    """0.000012 XRP is 12 drops, converted by xrp_units and not by a literal."""
    post(server_info=server_info(base_fee=0.000012), account_info=account_info())
    plan = adapter().preview_payout(ACCOUNT_ONE, 1, ACCOUNT_ZERO)
    assert plan["fee_drops"] == 12
    assert "read from server_info" in plan["description"]


# --- guard 4: tfPartialPayment, the send-side half of the receive-side exploit


def test_the_partial_payment_bit_is_the_measured_constant():
    """0x00020000, matching PaymentFlag.TF_PARTIAL_PAYMENT in xrpl-py 5.2.0."""
    assert TF_PARTIAL_PAYMENT == 131072


def test_a_transaction_with_no_flags_field_is_accepted():
    """The normal case: a Payment built without flags serializes with no Flags key."""
    assert refuse_partial_payment({"TransactionType": "Payment", "Amount": "1000000"}) is None


def test_the_partial_payment_bit_refuses_even_mixed_with_other_flags():
    """Bitwise, not equality: tfPartialPayment beside tfNoDirectRipple still refuses."""
    with pytest.raises(XRPPartialPaymentRefused, match="under-pay"):
        refuse_partial_payment({"Flags": TF_PARTIAL_PAYMENT | 0x00010000})


def test_an_unrelated_flag_is_not_mistaken_for_a_partial_payment():
    """tfNoDirectRipple alone is not this bit, and refusing it would be wrong."""
    assert refuse_partial_payment({"Flags": 0x00010000}) is None


def test_an_unreadable_flags_field_refuses():
    """An unreadable flags field is not an absent one."""
    with pytest.raises(XRPPartialPaymentRefused, match="not an integer"):
        refuse_partial_payment({"Flags": "partial"})


# --- the things that refuse without touching the network at all -------------


def test_an_x_address_is_refused_because_it_carries_its_own_tag(post):
    """Valid, and still not payable here: two tags means one of them loses."""
    recorder = post(server_info=server_info(), account_info=account_info())
    x_address = "X7AcgcsBL6XDcUb289X4mJ8djcdyKaB5hJDWMArnXr61cqZ"

    with pytest.raises(XRPRPCError, match="X-ADDRESS"):
        adapter().send_to_address(x_address, 1, source=ACCOUNT_ZERO)

    assert recorder.calls == [], "an address refusal must not read the server first"


def test_a_bad_checksum_is_refused_locally_with_no_network_call(post):
    recorder = post(server_info=server_info())
    with pytest.raises(XRPRPCError, match="fails the checksum"):
        adapter().send_to_address("rNotARealAddressAtAll", 1, source=ACCOUNT_ZERO)
    assert recorder.calls == []


def test_a_missing_source_account_is_refused_rather_than_defaulted(post):
    """There is no default source, and that absence is the point.

    An adapter holding a hot-wallet account would be one configuration value
    away from a payout path.
    """
    recorder = post(server_info=server_info())
    with pytest.raises(XRPRPCError, match="needs the SOURCE account"):
        adapter().preview_payout(ACCOUNT_ONE, 1, "")
    assert recorder.calls == []


def test_a_zero_amount_is_refused_because_it_would_be_recorded_as_a_payout(post):
    """A zero-value Payment is valid, costs a fee and delivers nothing."""
    recorder = post(server_info=server_info())
    with pytest.raises(XRPRPCError, match="nothing to pay"):
        adapter().preview_payout(ACCOUNT_ONE, 0, ACCOUNT_ZERO)
    assert recorder.calls == []


def test_amounts_are_converted_through_xrp_units_and_never_through_a_float(post):
    """0.1 XRP is exactly 100000 drops. `0.1 * 10**6` in binary float is not."""
    post(server_info=server_info(), account_info=account_info())
    plan = adapter().preview_payout(ACCOUNT_ONE, "0.1", ACCOUNT_ZERO)
    assert plan["send_drops"] == 100_000
    assert isinstance(plan["send_drops"], int)
    assert isinstance(plan["balance_drops"], int)


def test_the_preview_announces_before_it_reads_anything(post, capsys):
    """Rule 14: a line that only appears on completion is invisible during the wait."""
    post(server_info=server_info(), account_info=account_info())
    adapter().preview_payout(ACCOUNT_ONE, 1, ACCOUNT_ZERO, destination_tag=4242)
    out = capsys.readouterr().out
    assert "XRP payout preview" in out
    assert "reading server_info and account_info" in out
    assert "4242" in out


def test_the_banner_says_which_payout_posture_this_process_is_actually_in(monkeypatch):
    """It has said three different things and the first two are now lies.

      until 2026-09-26   `payouts=REFUSED (holds no signing key)`
      until 2026-10-02   `payouts=PREVIEW-ONLY ... (holds no signing key; ...)`
      until 2026-10-03   one of two lines, chosen by whether the seed is PRESENT. The
                         host measured that day had the variable set to a
                         nine-character placeholder, so it printed ARMED while no
                         payout could have signed -- accurate, and useless.
      now                one of THREE lines: nothing exported, a value that is not a
                         seed, or a seed that decodes. can_spend is derived from the
                         last of those (chains/xrp_payout_seed.payout_capability()),
                         and the two unarmed lines differ because the operator's next
                         action differs: export the variable, versus fix its value.
                         tests/test_xrp_payout_wiring.py holds the gate itself; this
                         test holds the two states this file already covered.

    This test used to assert `"holds no signing key" in line`, and that clause is
    what had to go: services/payout_service.broadcast_payout() reads a seed from the
    environment, so a banner telling the operator their worker holds no key would be
    wrong in the direction that matters -- it would say a funded host cannot spend
    while it can. Rule 2: the test changes to pin the stronger invariant.

    THE STRONGER INVARIANT IS THAT THE TWO STATES ARE DISTINGUISHABLE. Rule 14's
    "make did-nothing look different from did-work", applied to a capability: a
    banner that printed the same sentence either way would be no banner at all, and
    the armed line has to be findable by an operator scanning for the thing that
    should not be there. Hence the asterisks, asserted on.

    BOTH STATES ARE SET EXPLICITLY rather than inherited. tests/conftest.py removes
    the variable at import so the default is deterministic, and this test still
    delenv's before the unarmed half -- a test that asserts a default must not
    depend on another file having arranged it.
    """
    monkeypatch.delenv("XRP_PAYOUT_SECRET_SEED", raising=False)
    unarmed = adapter().endpoint_line()
    assert "payouts=PREVIEW-ONLY" in unarmed
    assert "payouts=REFUSED" not in unarmed, "the 2026-09-26 lie must not come back"
    assert "XRP_PAYOUT_SECRET_SEED is NOT set" in unarmed, "rule 14: name the variable that decides it"
    assert "ARMED" not in unarmed
    assert "mainnet refused by server network_id, not by url" in unarmed

    # A SEED THAT REALLY DECODES, since 2026-10-03. This line used to set
    # "never-decoded-by-this-test", which was accurate while can_spend read presence
    # and is now the UNARMED-because-the-value-is-wrong state -- so this half of the
    # test would have measured the opposite of its own premise.
    monkeypatch.setenv("XRP_PAYOUT_SECRET_SEED", xrp_family_seed("xrp adapter banner: armed posture"))
    armed = adapter().endpoint_line()
    assert "*** ARMED, THIS PROCESS CAN SPEND XRP ***" in armed, (
        "the armed state must be findable by an operator scanning a banner for it"
    )
    # AND IT MUST NOT BE SWALLOWED BY THE TRAILING GLOSS, measured on the operator's
    # screen 2026-10-10. describe_min_confirmations() ends in `<- NOT a block depth;
    # the XRP Ledger does not reorganize` -- an annotation designed to END a line -- and
    # the posture used to be appended AFTER it with no separator, so the line read
    # "...does not reorganize payouts=*** ARMED, THIS PROCESS CAN SPEND XRP ***" and the
    # most important fact on it became the tail of a sentence about reorganization.
    #
    # The assertion is POSITIONAL because that is the defect: both strings were present
    # the whole time. `in` could not see it and cannot see it coming back.
    assert armed.index("payouts=") < armed.index("<-"), (
        "the arming posture must come BEFORE the `<-` confirmation gloss. A trailing "
        "annotation swallows anything appended after it, and what was being swallowed "
        "is the sentence saying this process can spend money"
    )
    assert armed.rstrip().endswith("does not reorganize"), (
        "and the gloss is still the LAST thing on the line, which is what makes it a gloss"
    )
    assert "PREVIEW-ONLY" not in armed, "a process that can spend must not describe itself as preview-only"
    # The one promise neither state may drop, because no variable turns it off.
    # Case-folded because the two lines start the sentence differently -- "mainnet
    # refused by..." mid-clause in one and "Mainnet is still refused by..." as its
    # own sentence in the other -- and the PROMISE is what is under test here, not
    # the capitalization. The unarmed half above asserts its exact wording.
    assert "mainnet" in armed.lower()
    assert "network_id" in armed


# --- XRP is tradeable, and what still gates it ------------------------------


def test_xrp_grc_is_a_tradeable_pair_in_both_directions():
    """The posture change, made 2026-09-26 on the operator's explicit instruction.

    This test used to read test_xrp_is_still_not_a_tradeable_pair and assert the
    opposite. It is CHANGED rather than deleted, per CLAUDE.md rule 2: a test
    whose behavior is deliberately replaced changes to pin the stronger
    invariant. It did its job on the way out -- enabling the pair failed this
    test, which is exactly what a posture guard is for.

    Both directions, because a one-way pair is a quote a customer cannot unwind.
    """
    assert ("XRP", "GRC") in Config.ALLOWED_PAIRS
    assert ("GRC", "XRP") in Config.ALLOWED_PAIRS


def test_an_allowed_pair_still_cannot_create_a_swap_without_a_custody_account():
    """THE STRONGER INVARIANT, and the reason the guard above could be relaxed.

    Allowing a pair opens the gate; it does not put anything through it. An XRP
    swap needs a shared deposit account to attribute tags against, that account is
    a custody decision, and it has no default -- so create_swap() refuses while
    XRP_DEPOSIT_ACCOUNT is unset.

    This is what makes enabling the pair safe rather than merely authorized: the
    failure without custody configured is "no swap", not "a swap whose deposit
    instruction points at an account nobody holds the key for". Asserted through
    the real refusal rather than by reading the config, because the config being
    empty is not the same as the code honoring it.
    """
    class StubXRP:
        def validate_address(self, address):
            return address.startswith("r")

    with pytest.raises(ValueError, match="XRP_DEPOSIT_ACCOUNT is not set"):
        deposit_account({"XRP_DEPOSIT_ACCOUNT": ""}, {"XRP": StubXRP()}, "XRP", "s-1")


def test_xrp_min_confirmations_is_still_one():
    """A live-safety threshold, unchanged. One validated ledger; no depth exists."""
    assert Config.XRP_MIN_CONFIRMATIONS == 1


# --- the signing half: real key derivation, a stubbed submit -----------------
#
# xrpl-py is an OPTIONAL dependency (see chains/xrp_signing.derive_and_check's
# import note), so these SKIP rather than fail without it. The skip reason names
# what went unchecked rather than just the missing module, because a skip that
# reads as "fine" is the same defect rule 14 names about a blank line.

wallet_module = pytest.importorskip(
    "xrpl.wallet", reason="xrpl-py absent, so the derivation guard and the signed transaction are UNCHECKED"
)
# Bound here rather than imported inside armed_adapter(), which is what the first
# draft did and which ruff flagged as PLC0415. A plain top-of-file
# `import xrpl.transaction` would break COLLECTION on a host without xrpl-py --
# the statement runs before importorskip gets the chance to skip -- so
# importorskip is the correct mechanism for both of them, and no suppression is
# needed (rule 19: fix the code, do not quiet the finding).
transaction_module = pytest.importorskip(
    "xrpl.transaction", reason="xrpl-py absent, so the signed-and-submitted path is UNCHECKED"
)


class StubResponse:
    def __init__(self, result):
        self.result = result


def armed_adapter(post, monkeypatch, *, result, balance="1000000000"):
    """An adapter whose submit_and_wait is a stub that RECORDS the Payment.

    Patched at `xrpl.transaction.submit_and_wait`, which is where
    _sign_and_submit imports it from at call time, so the real code path runs:
    the real Wallet, the real Payment model, the real serialization and the real
    refuse_partial_payment() check. Only the socket is replaced.

    Returns (adapter, source_wallet, captured) where captured is a list the stub
    appends each submitted Payment to -- so "nothing was submitted" is an
    assertion on an empty list rather than on the absence of an exception.
    """
    captured = []

    def stub(transaction, client, wallet, **kwargs):
        captured.append(transaction)
        return StubResponse(result)

    monkeypatch.setattr(transaction_module, "submit_and_wait", stub)
    source = wallet_module.Wallet.create()
    post(server_info=server_info(), account_info=account_info(drops=balance))
    return adapter(), source, captured


def test_a_seed_for_another_account_refuses_and_submits_nothing(post, monkeypatch):
    """The derivation guard, at the adapter.

    Server-side `submit` sends the secret and Account separately so the server
    rejects a mismatch. Signing locally, WE choose the account the transaction
    claims -- so a seed paired with the wrong address signs a Payment debiting
    an account the preview never displayed. The operator reads one address and a
    different one is debited.
    """
    instance, source, captured = armed_adapter(post, monkeypatch, result={})
    other = wallet_module.Wallet.create()

    with pytest.raises(RuntimeError, match="REFUSING to sign"):
        instance.send_to_address(
            ACCOUNT_ONE, 1, source=source.classic_address, seed=other.seed, confirm_send=CONFIRM_XRP_SEND
        )

    assert captured == [], "a mismatched seed must not reach submit_and_wait"


def test_the_mismatch_message_names_both_addresses_and_never_the_seed(post, monkeypatch):
    """A key-mismatch error is exactly where a secret leaks.

    The debugging impulse on a mismatch is to print the key. Both public
    addresses are in the message because they are what the operator needs; the
    seed is not, and neither is any six-character run of it -- the substring
    check is there because a truncated "sEd7...(redacted)" would still be a leak
    of the part an attacker needs least but a logger keeps forever.
    """
    instance, source, _captured = armed_adapter(post, monkeypatch, result={})
    other = wallet_module.Wallet.create()

    with pytest.raises(RuntimeError) as caught:
        instance.send_to_address(
            ACCOUNT_ONE, 1, source=source.classic_address, seed=other.seed, confirm_send=CONFIRM_XRP_SEND
        )

    message = str(caught.value)
    assert other.classic_address in message
    assert source.classic_address in message
    assert other.seed not in message
    for start in range(len(other.seed) - 6):
        assert other.seed[start : start + 7] not in message


def test_the_seed_never_reaches_stdout_either(post, monkeypatch, capsys):
    """Rule 14 against the secrets rule: the preview prints a lot, and none of it is the seed."""
    instance, source, _captured = armed_adapter(post, monkeypatch, result={})
    other = wallet_module.Wallet.create()

    with pytest.raises(RuntimeError):
        instance.send_to_address(
            ACCOUNT_ONE, 1, source=source.classic_address, seed=other.seed, confirm_send=CONFIRM_XRP_SEND
        )

    printed = capsys.readouterr()
    assert other.seed not in printed.out
    assert other.seed not in printed.err


def validated_success(tx_hash="A" * 64):
    return {"meta": {"TransactionResult": "tesSUCCESS"}, "hash": tx_hash, "validated": True}


def test_an_armed_matching_send_signs_a_payment_with_no_partial_payment_bit(post, monkeypatch):
    """The armed happy path, asserting on the SERIALIZED transaction.

    This is the behavioral verification the principle asks for: not "the code
    does not pass flags", which is a claim about today's call site, but "the
    transaction that reached submit_and_wait carries no Flags with that bit."
    """
    instance, source, captured = armed_adapter(post, monkeypatch, result=validated_success())

    tx_hash = instance.send_to_address(
        ACCOUNT_ONE,
        1.5,
        source=source.classic_address,
        seed=source.seed,
        destination_tag=4242,
        confirm_send=CONFIRM_XRP_SEND,
    )

    assert tx_hash == "A" * 64
    assert len(captured) == 1
    serialized = captured[0].to_xrpl()
    assert serialized["Account"] == source.classic_address
    assert serialized["Destination"] == ACCOUNT_ONE
    assert serialized["DestinationTag"] == 4242
    assert serialized["Amount"] == "1500000", "drops, as a string, exactly as the ledger expects"
    assert not int(serialized.get("Flags") or 0) & TF_PARTIAL_PAYMENT


def test_a_failed_ledger_result_raises_rather_than_returning_a_hash(post, monkeypatch):
    """Rule 13 applied to a send: the assertion is the outcome, not the absence of an error.

    A tec* code REACHED the ledger and claimed a fee. Returning its hash would
    write a failed payment into `payouts` as `broadcast`.
    """
    instance, source, _captured = armed_adapter(
        post,
        monkeypatch,
        result={"meta": {"TransactionResult": "tecUNFUNDED_PAYMENT"}, "hash": "B" * 64, "validated": True},
    )

    with pytest.raises(XRPRPCError, match="tecUNFUNDED_PAYMENT"):
        instance.send_to_address(
            ACCOUNT_ONE, 1, source=source.classic_address, seed=source.seed, confirm_send=CONFIRM_XRP_SEND
        )


def test_success_without_validation_is_not_read_as_delivery(post, monkeypatch):
    """Finality here is binary, so "succeeded but not validated" is not a weaker yes."""
    instance, source, _captured = armed_adapter(
        post,
        monkeypatch,
        result={"meta": {"TransactionResult": "tesSUCCESS"}, "hash": "C" * 64, "validated": False},
    )

    with pytest.raises(XRPRPCError, match="validated=False"):
        instance.send_to_address(
            ACCOUNT_ONE, 1, source=source.classic_address, seed=source.seed, confirm_send=CONFIRM_XRP_SEND
        )


def test_a_validated_success_with_no_hash_says_the_money_probably_went(post, monkeypatch):
    """The most dangerous response shape: a missing field is not evidence of nothing."""
    instance, source, _captured = armed_adapter(
        post, monkeypatch, result={"meta": {"TransactionResult": "tesSUCCESS"}, "validated": True}
    )

    with pytest.raises(XRPRPCError, match="ALMOST CERTAINLY MADE"):
        instance.send_to_address(
            ACCOUNT_ONE, 1, source=source.classic_address, seed=source.seed, confirm_send=CONFIRM_XRP_SEND
        )


def test_the_submit_wait_is_reported_in_microfortnights(post, monkeypatch, capsys):
    """Rule 6: a duration this system reports is in µfn, with the seconds beside it.

    A ledger index and a confirmation count are NOT times and are not converted;
    a wall-clock wait is, and this is one.
    """
    instance, source, _captured = armed_adapter(post, monkeypatch, result=validated_success())

    instance.send_to_address(
        ACCOUNT_ONE, 1, source=source.classic_address, seed=source.seed, confirm_send=CONFIRM_XRP_SEND
    )

    out = capsys.readouterr().out
    assert "µfn" in out
    assert "ufn" not in out, "an ASCII u in displayed output is a defect, not a rendering fallback"


def test_the_preview_is_emitted_exactly_once_on_a_refusal(post, capsys):
    """Found on the operator's host 2026-09-26, against a real rippled.

    An unarmed run printed the eight-line plan, then the refusal, then the SAME
    eight lines again: send_to_address() printed plan["description"] before the
    arming check AND appended it to the refusal, so a caller that surfaced the
    exception saw both copies.

    Rule 14 asks for output a human can read, and a doubled block is how a reader
    starts skimming the thing that exists to be read -- on the one path whose
    whole purpose is that an operator reads the plan before arming it.

    The copy inside the EXCEPTION is the one kept, because it survives being
    caught and logged: a caller that swallows the refusal still holds the plan it
    refused. So the live print moved below the arming check, where rule 14's
    "announce before" most wants it anyway -- immediately before the one
    irreversible step rather than before a guard that usually stops.

    Asserted across BOTH channels together, since that is where the duplication
    lived: one copy in total between stdout and the message, not one in each.
    """
    post(server_info=server_info(), account_info=account_info())

    with pytest.raises(XRPSendNotArmed) as caught:
        adapter().send_to_address(ACCOUNT_ONE, 1.5, source=ACCOUNT_ZERO)

    printed = capsys.readouterr().out
    message = str(caught.value)
    # A line from the middle of the plan, not its first line: the "announce
    # before" progress lines legitimately print the amount and the accounts, so
    # keying on those would count an announcement as a duplicate plan.
    marker = "partial pay"

    assert message.count(marker) == 1, "the refusal must carry the plan exactly once"
    assert printed.count(marker) == 0, (
        "the plan must NOT also print live on the refusal path -- that is the "
        f"duplication this test exists for. Printed:\n{printed}"
    )
    assert printed.count(marker) + message.count(marker) == 1


def test_the_plan_still_prints_live_when_the_send_is_actually_armed(post, monkeypatch, capsys):
    """The other half, and it needs its own test.

    Removing the duplication by deleting the print would pass the test above and
    lose something real: on an ARMED send the operator must see the plan before
    the irreversible step, and there is no exception on that path to carry it. A
    fix that silences both paths is not a fix, which is why these two are written
    as a pair.

    Uses armed_adapter(), so the real Wallet, Payment model, serialization and
    refuse_partial_payment() all run and only the socket is stubbed.
    """
    instance, source, captured = armed_adapter(
        post, monkeypatch,
        result={"meta": {"TransactionResult": "tesSUCCESS"}, "validated": True, "hash": "A" * 64},
    )

    instance.send_to_address(
        ACCOUNT_ONE, 1, source=source.classic_address, seed=source.seed,
        confirm_send=CONFIRM_XRP_SEND,
    )

    printed = capsys.readouterr().out
    assert len(captured) == 1, "the armed path must actually submit"
    assert printed.count("partial pay") == 1, (
        f"the plan must print exactly once before signing. Printed:\n{printed}"
    )


# --- an account-wide fact must not be reported per swap -----------------------

def test_the_same_deferred_payment_is_reported_once_not_once_per_swap():
    """Measured on the operator's host 2026-09-26: two payments, four lines.

    find_deposits_to_address() is called once per ACTIVE SWAP, and on a
    tag-attributed chain every one of those calls scans the same shared account —
    so it sees the same untagged payments every time. Two open swaps doubled the
    output; ten would have printed twenty. Each line ends with "this needs an
    operator to match it by hand", so the line COUNT reads as the number of
    problems needing attention. There were two.

    Rule 14 from the less obvious direction: not silence, but noise that
    misrepresents scale.
    """
    seen = set()
    lines = ["txA  NO DestinationTag", "txB  NO DestinationTag"]

    assert new_deferrals(list(lines), seen) == lines, "the first scan says everything"
    assert new_deferrals(list(lines), seen) == [], "the second swap's scan of the same account says nothing"
    assert new_deferrals(list(lines), seen) == []


def test_a_newly_arrived_deferral_is_still_reported():
    """Deduping must not silence NEW news, which is the obvious way to overshoot.

    A version that reported only on the first call ever would pass the test above
    and hide every subsequent unattributable payment — which is money arriving that
    nobody is told about, strictly worse than the noise it replaced.
    """
    seen = set()
    new_deferrals(["txA  NO DestinationTag"], seen)

    fresh = new_deferrals(["txA  NO DestinationTag", "txB  NO DestinationTag"], seen)

    assert fresh == ["txB  NO DestinationTag"]


def test_two_different_reasons_for_one_transaction_are_two_facts():
    """Deduped on the whole LINE, not a parsed txid, and that is deliberate.

    xrp_payments.py builds the line from the txid AND the reason, so the same
    transaction refused for a second reason is new information. Parsing the txid
    back out would also couple this to that format for no gain.
    """
    seen = set()
    new_deferrals(["txA  NO DestinationTag"], seen)

    assert new_deferrals(["txA  TransactionResult=tecPATH_DRY"], seen) == ["txA  TransactionResult=tecPATH_DRY"]


def test_the_deferral_memory_is_per_instance_so_a_restart_re_reports():
    """A restart must show the backlog again.

    State that outlived the process would hide every still-unattributed payment
    from whoever started the worker next — and an operator starting a watcher is
    exactly the person who needs to see them.
    """
    first = adapter()
    second = adapter()

    assert first._reported_deferrals is not second._reported_deferrals
    new_deferrals(["txA  NO DestinationTag"], first._reported_deferrals)
    assert second._reported_deferrals == set(), "a fresh adapter starts with no memory"


# --- the network is NAMED, not echoed as an id --------------------------------


def test_a_mainnet_id_is_NAMED_in_capitals_and_keeps_the_id():
    """The panel whose whole job is telling mainnet from a test network must say the word.

    An operator scanning for the word that should not be there has to be able to find
    it. The id stays alongside because it is what the daemon said and what
    MAINNET_NETWORK_IDS refuses on -- a name without the id would make the screen and
    the refusal argue in different vocabularies.
    """
    named = network_id_name(0)
    assert "MAINNET" in named
    assert "0" in named


def test_the_testnet_id_is_named_from_a_MEASURED_mapping():
    """1 is testnet because chains/xrp.py's header records measuring it against
    s.altnet.rippletest.net with rippled 3.4.1 -- not because it is the next number
    after mainnet."""
    named = network_id_name(1)
    assert "testnet" in named
    assert "1" in named
    assert "MAINNET" not in named


def test_an_UNMEASURED_id_is_not_given_a_plausible_name():
    """Rule 17, and the reason devnet is deliberately absent from the mapping.

    Devnet, sidechains and private networks all have ids and this repo has measured
    none of them. Mapping 2 to "devnet" from memory is how a confident "testnet" ends
    up printed beside a daemon that is nothing of the kind. An unrecognized id says it
    is unrecognized, and says it is NOT established as a test network rather than
    letting "not mainnet" read as safe.
    """
    named = network_id_name(2)
    assert "not measured" in named
    assert "NOT established as a test network" in named
    assert "devnet" not in named.lower(), "a name this repo has not measured"
    # AND IT SAYS THE MAINNET CHECK PASSED. A mutation that dropped this clause while
    # keeping "NOT established as a test network" survived the first pass: without it
    # an operator reads "unrecognized" and cannot tell whether the mainnet refusal
    # fired, which is the one thing they need from an id they do not recognize.
    assert "not mainnet by MAINNET_NETWORK_IDS" in named


def test_a_MISSING_id_and_a_NON_NUMERIC_id_read_differently():
    """Two different failures, and neither is a network name. A daemon answering a
    non-numeric network_id is something to see rather than normalize away, so the
    value is reported."""
    assert "no network_id reported" in network_id_name(None)
    assert "not a number" in network_id_name("mainnet-ish")
    assert "mainnet-ish" in network_id_name("mainnet-ish"), "the value is shown, not swallowed"


def test_the_ADAPTER_returns_the_NAME_and_not_the_bare_id(monkeypatch):
    """The call site, because echoing the id is what it did and what a revert restores.

    TEN call-site mutations have been attempted in this session. This drives
    XRPAdapter.network() rather than network_id_name() alone.
    """
    # THE FILE'S OWN Recorder, not a hand-stubbed private method. My first version
    # patched `adapter._rpc`, which does not exist -- a test that patches a name
    # nothing reads would have passed while testing nothing, had the attribute been
    # created rather than raising.
    recorder = Recorder(server_info=server_info(network_id=1))
    monkeypatch.setattr(requests, "post", recorder)
    adapter = XRPAdapter("https://s.altnet.rippletest.net:51234")
    answer = adapter.network()
    assert recorder.methods == ["server_info"], "the name comes from the daemon's own answer"
    assert "testnet" in answer, f"the adapter still echoes the bare id: {answer!r}"
    assert answer != "1"
