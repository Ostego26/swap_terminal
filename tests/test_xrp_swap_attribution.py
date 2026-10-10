"""XRP deposits share one account, so attribution is by tag -- and getting that wrong pays a stranger.

Role: test (pure functions plus one seeded database; opens no socket)
Reads: services/deposit_service.py, services/swap_service.py
Writes: a temp database
Can move funds: no
Mainnet-safe: yes

The defect these exist for was reported on 2026-09-26 by the tag-allocator work
and deliberately not fixed then, because refresh_swap_from_chain() is the one
function that decides, for every chain, that a deposit is confirmed. It is fixed
now that XRP is being taken live.

Its shape: for BTC, LTC and GRC the deposit ADDRESS is the swap, so every event
the adapter returns belongs to the swap being refreshed. For XRP every swap
shares ONE account, so find_deposits_to_address() returns every tagged payment
made to the whole terminal -- and crediting them unfiltered attributes all of
them to whichever swap the worker happened to be refreshing. One customer's
deposit credited to another's swap, with the ledger recording that the sender
paid exactly what they were told to pay.
"""

import hashlib
import sys
from pathlib import Path

import base58
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "swap_terminal"))

from db import SCHEMA, apply_migrations, connect_db
from modules.address_network import is_testnet_address
from services.deposit_service import attributable_events
from services.helpers import utc_now_iso
from services.swap_service import create_swap, deposit_account
from valid_addresses import BTC_PARTICIPANT

ACCOUNT = "rnjG8n16JinjqkzZj5Jmw6NDMBMzhhNbVv"

# A REAL TESTNET GRIDCOIN ADDRESS, derived deterministically, replacing a placeholder.
#
# This was `"S" + "x" * 30` (and `"SdzHNW1234567890abcdefghijklmnopq"` in test_open_swap.py):
# strings that no Gridcoin daemon would accept -- wrong length, no valid checksum, and in the
# `1234567890` case characters outside base58's alphabet. They passed because the stub beside
# them only checked the first letter, so a fake address and a fake check agreed with each
# other and neither matched the daemon they stood in for.
#
# That agreement is what made the pair invisible. Tightening the stub to decode the version
# byte (2026-09-27, after `not startswith("S")` was measured to misclassify 13.08% of mainnet
# GRC addresses) broke 17 tests in this file and test_xrp_swap_attribution.py -- not because
# the stub became wrong, but because the fixtures always had been.
#
# Derived from a fixed phrase rather than written as a literal so the checksum cannot be
# mistyped and the value cannot drift between runs.
_FIXTURE_HASH160 = hashlib.new(
    "ripemd160", hashlib.sha256(b"swap_terminal test fixture GRC payout").digest()
).digest()
GRC_TESTNET_ADDRESS = base58.b58encode_check(b"\x6f" + _FIXTURE_HASH160).decode()


def event(tag, txid="a" * 64, amount=1.0):
    """A deposit event in the adapter contract's shape, tag in `vout`.

    chains/xrp_payments.py::_classify() puts the DestinationTag in `vout` because
    `vout` is the integer discriminator in {txid, vout, address, amount,
    confirmations}. Matching that here rather than inventing a `tag` key is the
    point -- a test that used a different key would pass while the real filter
    looked at the wrong field.
    """
    return {"txid": txid, "vout": tag, "address": ACCOUNT, "amount": amount, "confirmations": 1}


def xrp_swap(tag, swap_id="s-1"):
    return {"id": swap_id, "from_asset": "XRP", "deposit_address": ACCOUNT, "deposit_tag": tag}


# --- the money bug -----------------------------------------------------------

def test_another_swaps_tagged_deposit_is_not_credited_to_this_swap():
    """THE defect. Fails against the unfiltered version.

    Three payments arrive at the shared account carrying three different tags.
    Only the one matching this swap is this swap's.
    """
    events = [event(11, "a" * 64), event(22, "b" * 64), event(33, "c" * 64)]

    kept = attributable_events(events, xrp_swap(22))

    assert [e["txid"] for e in kept] == ["b" * 64]


def test_an_address_attributed_chain_is_untouched():
    """BTC/LTC/GRC must behave exactly as before: the address already identified the swap.

    Written because the obvious wrong fix is to filter on `vout` for every chain,
    which would silently stop crediting Bitcoin deposits -- `vout` there is an
    output index, not a swap identifier, and it would match nothing.
    """
    events = [event(0, "a" * 64), event(1, "b" * 64)]
    swap = {"id": "s-1", "from_asset": "BTC", "deposit_address": BTC_PARTICIPANT, "deposit_tag": None}

    assert attributable_events(events, swap) == events


def test_tag_zero_is_matched_rather_than_read_as_absent():
    """0 is a legal DestinationTag and truthiness drops it.

    Same trap as tests/test_xrp_payments.py pins one layer down. Here the cost is
    different and worse: the filter would credit NOTHING to a swap tagged 0 while
    its deposit sat in the account.
    """
    kept = attributable_events([event(0), event(5, "b" * 64)], xrp_swap(0))

    assert [e["vout"] for e in kept] == [0]


def test_a_tag_attributed_swap_with_no_tag_credits_nothing(caplog):
    """The safe direction when attribution is unknown.

    This should be impossible -- create_swap() allocates the tag in the same
    transaction as the INSERT -- but "should be impossible" is not a reason to
    make the failure mode "credit every customer's payment to this swap". An
    uncredited deposit is a support ticket; a misattributed one is somebody
    else's money.
    """
    kept = attributable_events([event(11), event(22, "b" * 64)], xrp_swap(None))

    assert kept == []
    assert "crediting NOTHING" in caplog.text, "silence here would be the defect"


def test_an_untagged_payment_to_the_shared_account_is_never_credited():
    """A payment with no tag at all cannot be attributed to anyone.

    chains/xrp_payments.py already defers these rather than emitting an event, so
    this is defense at the second layer -- but the two layers disagreeing is how
    the first one's removal goes unnoticed.
    """
    assert attributable_events([{"txid": "a" * 64, "vout": None, "amount": 1.0}], xrp_swap(7)) == []


# --- the deposit instruction -------------------------------------------------

class FakeXRP:
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

    def owns_address(self, address):
        """Not the desk's -- these fixtures all supply a CUSTOMER payout address.

        Added 2026-10-04 with the ownership gate in services/swap_service.
        refuse_unusable_payout_address(). A destination stub without it raises
        AttributeError, so the gate could not be exercised and every create_swap
        test failed on the harness instead of on its own subject. False is the
        honest default here; a test that wants the refusal returns True.
        """
        return False


def test_an_unset_deposit_account_refuses_rather_than_creating_a_swap():
    """Custody has no default, and the failure must come BEFORE a swap exists.

    A swap created now would hand a customer a deposit instruction this terminal
    cannot receive against. A failed creation costs a retry; a swap that takes a
    deposit it cannot see costs the deposit.
    """
    with pytest.raises(ValueError, match="XRP_DEPOSIT_ACCOUNT is not set"):
        deposit_account({"XRP_DEPOSIT_ACCOUNT": ""}, {"XRP": FakeXRP()}, "XRP", "s-1")


def test_an_invalid_deposit_account_refuses_before_a_tag_is_burned():
    """Validated before allocation, because tags are never reused.

    Allocating against a bad account would burn that integer permanently for a
    swap that cannot exist.
    """
    with pytest.raises(ValueError, match="not a valid XRP Ledger account"):
        deposit_account({"XRP_DEPOSIT_ACCOUNT": "not-an-account"}, {"XRP": FakeXRP()}, "XRP", "s-1")


def test_a_configured_account_is_used_and_asks_for_a_tag():
    address, needs_tag = deposit_account({"XRP_DEPOSIT_ACCOUNT": ACCOUNT}, {"XRP": FakeXRP()}, "XRP", "s-1")

    assert address == ACCOUNT
    assert needs_tag is True


def test_xrp_never_asks_the_adapter_to_derive_an_address():
    """FakeXRP.get_new_address raises, so this passes only if it is never called.

    Deriving a fresh XRP account per swap would mean funding each past the base
    reserve and holding a key for it, to solve what the ledger solved with an
    integer. The adapter refuses on purpose; this pins that the caller respects it.
    """
    deposit_account({"XRP_DEPOSIT_ACCOUNT": ACCOUNT}, {"XRP": FakeXRP()}, "XRP", "s-1")


# --- swap creation end to end, against a real database -----------------------

class StubGRC:
    # DECLARED, because chains/registry.why_cannot_pay_out() fails closed: a stub
    # that says nothing about itself counts as unable to pay, and create_swap()
    # refuses a destination that cannot. These stubs stand in for a chain that CAN
    # be paid out to, so they say so -- the real XRPAdapter and SolanaAdapter both
    # declare False, which is what took GRC -> XRP off the menu on 2026-09-26.
    can_spend = True
    payout_refusal = ""

    def validate_address(self, address):
        # THE VERSION BYTE, NOT THE FIRST LETTER. This stub stands in for a Gridcoin
        # daemon's validate_address, and `startswith("S")` had it answering backwards:
        # measured over 200,000 random hash160s, 13.08% of MAINNET GRC addresses start
        # with R (so the stub accepted them) and NO testnet address starts with S (so it
        # rejected every real one). modules/address_network.py decodes the byte that
        # actually names the network.
        return is_testnet_address(address)

    def owns_address(self, address):
        """Not the desk's -- these fixtures all supply a CUSTOMER payout address.

        Added 2026-10-04 with the ownership gate in services/swap_service.
        refuse_unusable_payout_address(). A destination stub without it raises
        AttributeError, so the gate could not be exercised and every create_swap
        test failed on the harness instead of on its own subject. False is the
        honest default here; a test that wants the refusal returns True.
        """
        return False


def seeded_db(tmp_path):
    """A real database on the real SCHEMA, with migrations applied."""
    # tmp_path, NOT os.environ["SWAP_DB_PATH"]. Reading the environment here meant
    # opening conftest's SHARED database, so these two tests saw each other's rows
    # -- each passed alone and the pair failed together, which is the signature of
    # shared state rather than of a bug in the code under test. Every service
    # takes `db` as an argument, so the path never needed to be global.
    conn = connect_db(str(tmp_path / "t.db"), create=True)
    conn.executescript(SCHEMA)
    conn.commit()
    apply_migrations(conn)
    return conn


def seed_quote(conn, quote_id, from_asset="XRP", to_asset="GRC"):
    """A priced quote for a pair. The assets are parameters, defaulted to this file's.

    Parameterized 2026-10-01 so tests/test_sol_swap_creation.py can reuse this
    and seeded_db() rather than growing its own copies -- two seeding helpers for
    one schema is rule 8's drift arriving in the harness, where it is worse,
    because a test that seeds a row the application would never write passes
    while proving nothing.
    """
    conn.execute(
        "INSERT INTO quotes (id, from_asset, to_asset, input_amount, quoted_rate, fee_bps, "
        "network_fee_reserve, output_amount_estimate, created_at, expires_at) "
        "VALUES (?,?,?,?,?,?,?,?,?,?)",
        (quote_id, from_asset, to_asset, 10.0, 1000.0, 150, 0.01, 9850.0,
         utc_now_iso(), "2099-01-01T00:00:00+00:00"),
    )
    conn.commit()


def test_xrp_swaps_share_one_account_and_get_distinct_tags(tmp_path):
    """The shape of the whole feature, asserted against a real database.

    Three swaps, one shared deposit account, three different tags. The shared
    address is the part that makes the attribution filter necessary and is worth
    asserting explicitly -- if these ever got DIFFERENT addresses, something has
    started deriving per-swap XRP accounts and the reserve cost of that should
    surface as a failing test rather than as a funding bill.
    """
    conn = seeded_db(tmp_path)
    config = {"XRP_DEPOSIT_ACCOUNT": ACCOUNT, "XRP_MIN_CONFIRMATIONS": 1}
    adapters = {"XRP": FakeXRP(), "GRC": StubGRC()}

    swaps = []
    for n in range(3):
        seed_quote(conn, f"q{n}")
        swaps.append(create_swap(conn, config, adapters, f"q{n}", GRC_TESTNET_ADDRESS))

    assert {s["deposit_address"] for s in swaps} == {ACCOUNT}, "all XRP swaps share one account"
    tags = [s["deposit_tag"] for s in swaps]
    assert len(set(tags)) == 3, f"tags must be distinct, got {tags}"
    assert all(isinstance(tag, int) for tag in tags)

    stored = conn.execute("SELECT deposit_tag FROM swaps ORDER BY deposit_tag").fetchall()
    conn.close()
    assert [row["deposit_tag"] for row in stored] == sorted(tags), "the tag must reach the ROW, not just the dict"


def test_a_refused_xrp_swap_leaves_no_row_behind(tmp_path):
    """The transaction boundary, and it is the reason allocation happens after the INSERT.

    create_swap() inserts the swap, then allocates the tag, then commits. If the
    refusal left a committed swap with no tag, a customer could be handed a
    deposit address with nothing to recognize their payment by -- and
    attributable_events() would then correctly credit them NOTHING.

    Asserted by counting rows after the failure rather than by reading the code,
    because "it is all one transaction" is a claim about the code and this is a
    measurement.
    """
    conn = seeded_db(tmp_path)
    adapters = {"XRP": FakeXRP(), "GRC": StubGRC()}
    seed_quote(conn, "q-doomed")

    with pytest.raises(ValueError, match="XRP_DEPOSIT_ACCOUNT is not set"):
        create_swap(conn, {"XRP_DEPOSIT_ACCOUNT": "", "XRP_MIN_CONFIRMATIONS": 1},
                    adapters, "q-doomed", GRC_TESTNET_ADDRESS)

    count = conn.execute("SELECT COUNT(*) AS c FROM swaps").fetchone()["c"]
    conn.close()
    assert count == 0, "a refused swap must leave no row"
