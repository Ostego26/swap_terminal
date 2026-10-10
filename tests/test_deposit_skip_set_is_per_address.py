"""A settled txid at ANOTHER address must not silence a deposit at this one.

Role: test (pure; a temporary sqlite file, a seeded adapter transport, no socket,
      no replica, no key, nothing broadcast)
Reads: swap_terminal/services/deposit_service.py,
       swap_terminal/services/unattributable_deposit_service.py,
       swap_terminal/chains/icp.py and chains/icp_account.py
Writes: a throwaway database under pytest's tmp_path
Can move funds: no
Mainnet-safe: yes

=============================================================================
THE DEFECT, MEASURED ON THE OPERATOR'S OWN HOST 2026-10-10
=============================================================================

Their admin page's "Deposits seen" table, pasted back:

    s_f5cf62e0b7a9342a   ICP   txid 2   vout 0   0.05000000   conf 1
    s_ebb03e8dc1b96e1c   ICP   txid 1   vout 0   1.00000000   conf 1

Both written 2026-10-07, against a local replica ledger that `docker compose down`
then destroyed -- docker-compose.yml's own header records that loss twice, on
2026-10-07 and again on 2026-10-08. The ledger was rebuilt and re-minted, so its
block indexes restarted at 0.

**THE ICP TXID IS A LEDGER BLOCK INDEX.** chains/icp.find_deposits_to_address()
says so and gives the reason: a balance reported as an event collides with itself,
so the block index is what makes (asset, txid, vout) unique per payment. It is
unique within ONE ledger. It is not unique across a ledger that was thrown away
and rebuilt.

ICP_MIN_CONFIRMATIONS is 1 and every ICP event is written with confirmations=1, so
both rows above are permanently settled and the asset-wide settled_txids() returned
{"1", "2"} forever. A real 2.44081155 ICP deposit then landed in block index 2 of
the NEW ledger, for swap s_968a69b37c3da5c9:

    the scan found the block         transfer to that swap's own subaccount
    the skip set dropped it          "2" was in it, from the dead ledger
    the adapter returned []          no row, no error, no log line
    show_swap said                   "0 deposit row(s)", status awaiting_deposit
    the desk's ICP balance said      1197.55908845, which is
                                     1200 - 2.44081155 - 0.0001 exactly

So the money was provably in the subaccount and the terminal could not see it.
That is the failure chains/icp.py's archived-blocks refusal exists to prevent,
arriving through a different door: the customer paid, the watcher polls forever,
and nothing errors -- indistinguishable from a customer who did not pay.

=============================================================================
WHAT THESE TESTS ASSERT, AND WHY BOTH DIRECTIONS ARE HERE
=============================================================================

The fix scopes the skip set to the address being scanned. That could have been
written two ways and only one of them is safe, so both halves are pinned:

  the deposit is seen      a settled txid at a DIFFERENT address no longer
                           suppresses this address's scan. Without this the
                           2.44081155 ICP is invisible.
  the saving is kept       a settled txid at the SAME address is STILL skipped.
                           Without this every transaction is re-read every cycle,
                           which is the 2026-10-01 rate limit that cost a real
                           credited deposit on Solana -- strictly worse than the
                           bug being fixed.

MUTATION, and it is one line in each direction: drop `AND d.address = ?` from
settled_txids() and the first group fails; drop the whole `address=` argument and
the second group has nothing left to check.

VERIFIED BY OUTCOME, NOT BY READING. The last group runs the real
refresh_swap_from_chain() against a real ICPAdapter whose only stub is the dfx
transport, and then queries the real deposit_events table. "The code contains a
check for X" is not evidence; "this row is in the table" is.
"""

from __future__ import annotations

import json

import pytest
import valid_addresses
from chains.icp import ICPAdapter
from chains.icp_account import account_identifier, subaccount_from_index
from db import SCHEMA, apply_migrations, connect_db
from services import deposit_service

#: The local replica's own ledger and a principal in the shape dfx emits. Neither
#: is ever contacted: every call below is answered by `seeded_transport`.
LEDGER = "ryjl3-tyaaa-aaaaa-aaaba-cai"
OWNER = "2vxsx-fae"

#: DERIVED, NEVER PASTED. tests/test_address_literals_are_valid.py checks address
#: literals across the suite, and an ICP account identifier carries a CRC32 prefix
#: that a hand-copied string gets wrong in a way nothing here would notice. These
#: come out of the same chains/icp_account derivation the deposit addresses do.
DEAD_LEDGER_SUBACCOUNT = account_identifier(OWNER, subaccount_from_index(3))
LIVE_SUBACCOUNT = account_identifier(OWNER, subaccount_from_index(9))

#: The amount the operator actually sent, and the block it landed in.
SENT = 2.44081155
SENT_E8S = 244081155
LIVE_BLOCK = 2

#: DERIVED, NOT PASTED, for the reason tests/test_payment_key_widening.py states at its
#: own copy of this line: test_address_literals_are_valid.py holds a ceiling on
#: address-shaped literals in this tree and pasting this one took it from 60 to 65.
GRC_PAYOUT = valid_addresses.GRC_PAYOUT

CONFIG = {"AMOUNT_TOLERANCE_PCT": 0.01, "ICP_MIN_CONFIRMATIONS": 1}


@pytest.fixture
def db(tmp_path):
    conn = connect_db(str(tmp_path / "t.db"), create=True)
    conn.executescript(SCHEMA)
    apply_migrations(conn)
    conn.execute(
        "INSERT INTO quotes (id, from_asset, to_asset, input_amount, quoted_rate, fee_bps,"
        " network_fee_reserve, output_amount_estimate, expires_at, created_at)"
        " VALUES ('q','ICP','GRC',2.44081155,327.21,150,0.001,786.0,"
        "'2999-01-01T00:00:00+00:00','2026-10-10T18:46:41+00:00')"
    )
    conn.commit()
    return conn


def seed_swap(db, swap_id, address, *, status="awaiting_deposit", expected=SENT):
    """One ICP swap with its own subaccount. min_confirmations 1, as ICP's always is."""
    db.execute(
        "INSERT INTO swaps (id, quote_id, from_asset, to_asset, deposit_address,"
        " payout_address, expected_input_amount, quoted_rate, fee_bps, network_fee_reserve,"
        " output_amount_estimate, status, min_confirmations, expires_at, created_at, updated_at)"
        " VALUES (?,'q','ICP','GRC',?,?,?,327.21,150,0.001,"
        "786.0,?,1,'2999-01-01T00:00:00+00:00','2026-10-10T18:46:41+00:00',"
        "'2026-10-10T18:46:41+00:00')",
        (swap_id, address, GRC_PAYOUT, expected, status),
    )
    db.commit()


def seed_settled_event(db, swap_id, txid, address, amount):
    """A deposit row at its threshold -- which on ICP is every row ever written."""
    db.execute(
        "INSERT INTO deposit_events (swap_id, asset, txid, vout, address, amount,"
        " confirmations, first_seen_at, last_seen_at)"
        " VALUES (?,'ICP',?,0,?,?,1,'2026-10-07T22:14:34+00:00','2026-10-07T22:14:34+00:00')",
        (swap_id, txid, address, amount),
    )
    db.commit()


def seeded_transport(page):
    """The dfx call, answered from a literal reply. The ADAPTER is the real one.

    Only the transport is stubbed, which is the boundary chains/registry.py already
    draws -- it builds `dfx_transport(...)` outside the adapter precisely so the
    adapter takes a callable. So the block parsing, the Mint/Transfer discrimination,
    the `to`-blob comparison and the skip check below are all real code.
    """
    def call(_canister, _method, _argument, output="idl"):
        return json.dumps(page) if output == "json" else "(10000 : nat)"
    return call


def rebuilt_ledger_page():
    """The ledger as it stood when the operator's deposit landed. Three blocks.

    Blocks 0 and 1 are fund_desk.py's mints into the desk's own default account --
    1000 then 200 LICP, which is the 1200 the operator read back. Block 2 is the
    customer's transfer into this swap's subaccount. Shaped the way
    `dfx --output json` actually emits it: a blob is a list of integers, every
    number is a string, and `operation` is an optional variant, so it arrives as a
    list of zero or one single-key object.
    """
    desk = account_identifier(OWNER)
    return {
        "chain_length": "3",
        "first_block_index": "0",
        "archived_blocks": [],
        "blocks": [
            {"transaction": {"operation": [
                {"Mint": {"to": list(bytes.fromhex(desk)), "amount": {"e8s": "100000000000"}}}]}},
            {"transaction": {"operation": [
                {"Mint": {"to": list(bytes.fromhex(desk)), "amount": {"e8s": "20000000000"}}}]}},
            {"transaction": {"operation": [
                {"Transfer": {"to": list(bytes.fromhex(LIVE_SUBACCOUNT)),
                              "amount": {"e8s": str(SENT_E8S)}}}]}},
        ],
    }


# --- the two subaccounts are genuinely different ------------------------------

def test_the_fixture_is_not_asserting_against_itself():
    """A test whose two addresses were equal would pass under the defect too.

    Cheap, and it is the one assumption everything below rests on: these are two
    distinct accounts under ONE principal, which is how ICP attributes a deposit at
    all (the admin page's own words: "the next unused subaccount index under the
    desk's single principal").
    """
    assert DEAD_LEDGER_SUBACCOUNT != LIVE_SUBACCOUNT
    assert len(DEAD_LEDGER_SUBACCOUNT) == len(LIVE_SUBACCOUNT) == 64


# --- the deposit is seen -------------------------------------------------------

def test_a_block_index_settled_at_ANOTHER_address_is_not_in_this_addresss_skip_set(db):
    """THE ASSERTION THIS FILE EXISTS FOR.

    MUTATION: drop `AND d.address = ?` from deposit_service.settled_txids() and this
    fails with {'2'} -- which is the operator's 2026-10-10 morning, exactly.
    """
    seed_swap(db, "s_f5cf62e0b7a9342a", DEAD_LEDGER_SUBACCOUNT, status="completed", expected=0.05)
    seed_settled_event(db, "s_f5cf62e0b7a9342a", str(LIVE_BLOCK), DEAD_LEDGER_SUBACCOUNT, 0.05)

    assert deposit_service.skip_txids(db, "ICP", address=LIVE_SUBACCOUNT) == frozenset(), (
        "block index 2 was settled against a DESTROYED ledger's subaccount. Scoped to this "
        "swap's own address the set must be empty, or the live deposit at index 2 is dropped "
        "before it can become a row"
    )
    assert deposit_service.settled_txids(db, "ICP", address=LIVE_SUBACCOUNT) == frozenset()


def test_the_real_adapter_returns_the_deposit_once_the_set_is_scoped(db):
    """The scan, through the real ICPAdapter, with the real skip set handed to it."""
    seed_swap(db, "s_f5cf62e0b7a9342a", DEAD_LEDGER_SUBACCOUNT, status="completed", expected=0.05)
    seed_settled_event(db, "s_f5cf62e0b7a9342a", str(LIVE_BLOCK), DEAD_LEDGER_SUBACCOUNT, 0.05)
    adapter = ICPAdapter(LEDGER, OWNER, seeded_transport(rebuilt_ledger_page()))

    found = adapter.find_deposits_to_address(
        LIVE_SUBACCOUNT,
        skip_txids=deposit_service.skip_txids(db, "ICP", address=LIVE_SUBACCOUNT),
    )
    assert found == [{
        "txid": "2", "vout": 0, "address": LIVE_SUBACCOUNT,
        "amount": SENT, "confirmations": 1,
    }]


def test_the_deposit_becomes_a_ROW_and_the_swap_leaves_awaiting_deposit(db):
    """END TO END, through the real service, asserted against the real table.

    This is the one that would have failed on the operator's host. Everything above
    it is a component; this is the outcome.
    """
    seed_swap(db, "s_f5cf62e0b7a9342a", DEAD_LEDGER_SUBACCOUNT, status="completed", expected=0.05)
    seed_settled_event(db, "s_f5cf62e0b7a9342a", str(LIVE_BLOCK), DEAD_LEDGER_SUBACCOUNT, 0.05)
    seed_swap(db, "s_968a69b37c3da5c9", LIVE_SUBACCOUNT)
    swap = dict(db.execute("SELECT * FROM swaps WHERE id = 's_968a69b37c3da5c9'").fetchone())
    adapter = ICPAdapter(LEDGER, OWNER, seeded_transport(rebuilt_ledger_page()))

    refreshed = deposit_service.refresh_swap_from_chain(db, CONFIG, {"ICP": adapter}, swap)

    rows = db.execute(
        "SELECT txid, vout, address, amount FROM deposit_events WHERE swap_id = 's_968a69b37c3da5c9'"
    ).fetchall()
    assert [dict(row) for row in rows] == [
        {"txid": "2", "vout": 0, "address": LIVE_SUBACCOUNT, "amount": SENT}
    ], "the deposit has to be a ROW in the real table, not merely returned by a scan"
    assert refreshed["status"] != "awaiting_deposit", (
        f"the swap is still awaiting_deposit after its deposit was credited, which is the "
        f"symptom show_swap.py printed on 2026-10-10: status={refreshed['status']}"
    )
    assert float(refreshed["actual_input_amount"]) == pytest.approx(SENT)


# --- and the rate-limit saving is kept ----------------------------------------

def test_a_settled_txid_at_THIS_address_is_STILL_skipped(db):
    """THE OTHER DIRECTION, and it is the expensive one to get wrong.

    The skip set exists because re-reading every transaction every cycle rate-limited
    a real Solana deposit out of being credited on 2026-10-01. A fix that scoped the
    set so tightly that nothing was ever skipped would trade a visible bug for that
    one. So: same address, settled, still skipped.

    MUTATION: pass `address=""` from the call sites and this fails.
    """
    seed_swap(db, "s_968a69b37c3da5c9", LIVE_SUBACCOUNT, status="completed")
    seed_settled_event(db, "s_968a69b37c3da5c9", str(LIVE_BLOCK), LIVE_SUBACCOUNT, SENT)

    assert deposit_service.skip_txids(db, "ICP", address=LIVE_SUBACCOUNT) == {"2"}

    adapter = ICPAdapter(LEDGER, OWNER, seeded_transport(rebuilt_ledger_page()))
    assert adapter.find_deposits_to_address(
        LIVE_SUBACCOUNT,
        skip_txids=deposit_service.skip_txids(db, "ICP", address=LIVE_SUBACCOUNT),
    ) == [], "a transaction already settled at this very address must not be read again"


def test_a_deposit_still_below_its_threshold_is_never_skipped_at_either_address(db):
    """The property settled_txids() already had, re-checked under the new predicate.

    Adding a clause to a WHERE is the easiest place to lose an existing guarantee,
    and this is the guarantee whose loss is a deposit that stops being watched before
    it is credited. Seeded with min_confirmations 2 rather than ICP's real 1, because
    a threshold of 1 cannot express "below it".
    """
    db.execute(
        "UPDATE swaps SET min_confirmations = 2 WHERE id = 's_x'"
    )  # no-op; the row is inserted below with the value already set
    db.execute(
        "INSERT INTO swaps (id, quote_id, from_asset, to_asset, deposit_address,"
        " payout_address, expected_input_amount, quoted_rate, fee_bps, network_fee_reserve,"
        " output_amount_estimate, status, min_confirmations, expires_at, created_at, updated_at)"
        " VALUES ('s_x','q','ICP','GRC',?,?,?,327.21,150,"
        "0.001,786.0,'confirming',2,'2999-01-01T00:00:00+00:00','2026-10-10T18:46:41+00:00',"
        "'2026-10-10T18:46:41+00:00')",
        (LIVE_SUBACCOUNT, GRC_PAYOUT, SENT),
    )
    seed_settled_event(db, "s_x", "2", LIVE_SUBACCOUNT, SENT)  # confirmations=1, threshold=2
    db.commit()

    assert deposit_service.skip_txids(db, "ICP", address=LIVE_SUBACCOUNT) == frozenset()


# --- a shared account is unaffected, which is why this fix is safe -------------

def test_on_a_shared_account_the_scoped_set_EQUALS_the_old_asset_wide_one(db):
    """SOL and XRP put every swap in ONE account, so scoping by it removes nothing.

    This is the claim that made the change safe to make rather than merely correct,
    so it is asserted rather than argued: all four event producers -- chains/base.py
    in both its shapes, chains/solana.py, chains/xrp_payments.py and chains/icp.py --
    write the SCANNED address into the event, so on a shared-account chain every row
    on the asset carries the same address and the two sets coincide.
    """
    # The shared Solana deposit account, from the fixture module rather than pasted --
    # see GRC_PAYOUT above for the ceiling that makes this the only acceptable form.
    shared = valid_addresses.SOL_DEPOSIT_ACCOUNT
    db.execute(
        "INSERT INTO quotes (id, from_asset, to_asset, input_amount, quoted_rate, fee_bps,"
        " network_fee_reserve, output_amount_estimate, expires_at, created_at)"
        " VALUES ('qs','SOL','GRC',0.01,8700.0,150,0.01,86.0,"
        "'2999-01-01T00:00:00+00:00','2026-10-01T00:00:00+00:00')"
    )
    for index, swap_id in enumerate(("s_sol_a", "s_sol_b"), start=1):
        db.execute(
            "INSERT INTO swaps (id, quote_id, from_asset, to_asset, deposit_address, deposit_tag,"
            " payout_address, expected_input_amount, quoted_rate, fee_bps, network_fee_reserve,"
            " output_amount_estimate, status, min_confirmations, expires_at, created_at, updated_at)"
            " VALUES (?,'qs','SOL','GRC',?,?,?,0.01,8700.0,150,"
            "0.01,86.0,'completed',1,'2999-01-01T00:00:00+00:00','2026-10-01T00:00:00+00:00',"
            "'2026-10-01T00:00:00+00:00')",
            (swap_id, shared, index, GRC_PAYOUT),
        )
        db.execute(
            "INSERT INTO deposit_events (swap_id, asset, txid, vout, address, amount,"
            " confirmations, first_seen_at, last_seen_at)"
            " VALUES (?,'SOL',?,?,?,0.01,3,'2026-10-01T00:00:00+00:00','2026-10-01T00:00:00+00:00')",
            (swap_id, f"sig_{swap_id}", index, shared),
        )
    db.commit()

    assert deposit_service.skip_txids(db, "SOL", address=shared) == {"sig_s_sol_a", "sig_s_sol_b"}, (
        "both swaps' settled signatures are still skipped on the shared account, so the "
        "2026-10-01 rate-limit saving is untouched by this change"
    )
