"""The ICP deposit scan reads every block from its floor to the head, or it refuses.

Role: tests (chains/icp.ICPAdapter.find_deposits_to_address()' scan range)
Reads: nothing. Every query_blocks reply is answered by a seeded ledger in this file.
Writes: nothing
Can move funds: no -- no transport is constructed and `call` is never invoked
Mainnet-safe: yes

=============================================================================
IT SCANNED THE LAST 500 BLOCKS AND RETURNED [] FOR ANYTHING OLDER
=============================================================================

The two lines were:

    window = min(int(tx_limit), chain_length)
    start = chain_length - window

with tx_limit defaulting to 500 and NO caller ever passing it --
services/deposit_service.py's per-swap refresh, its shared scan and its unattributable
pass, and services/late_deposit_service.py's late pass, all call
find_deposits_to_address(address, skip_txids=...). So blocks [0, chain_length - 500)
were never examined. A Transfer into the swap's subaccount below that was not in the
page, the loop never saw it, and this returned [] -- with no error and no log line. The
swap sat at awaiting_deposit forever while the money was provably in its subaccount.

AND THE REFUSAL WRITTEN FOR EXACTLY THIS COULD NOT FIRE. The archived_blocks check
exists because "a missing range means a deposit that was made and will never be seen,
which is indistinguishable from a customer who did not pay" -- and it was computed over
the window that WAS requested, so it reported an empty vec and declined. The identical
partial answer produced by the function's own bound was returned as a normal empty
list, with the guard sitting beside it looking at the wrong range.

THE BLOCK INDEX IS GLOBAL, which is why 500 is not 500 of this swap's payments. Every
ledger transaction advances it: every other swap's deposit, every payout, every
fund_desk.py top-up.

LATENT, NOT ACTIVE, ON THE OPERATOR'S REPLICA -- and that distinction is stated because
rule 17 asks which one a reader is holding. chain_length was 2 when this was measured
(today's real deposit landed at block index 2), and below 500 the arithmetic is a no-op:
min(500, 2) is 2 and start is 0, so the whole chain was scanned. What flips it, in order
of likelihood: 500 ledger transactions accumulating locally, one per deposit, payout and
top-up, one-way and unmonitored; the watcher being down across 500 blocks; and pointing
ICP_LEDGER_CANISTER_ID at the ICP MAINNET ledger, where the index is global across the
whole network and 500 blocks is MINUTES.
"""

from __future__ import annotations

import re

import pytest
from chains.icp import MAX_BLOCKS_SCANNED, PAGE_BLOCKS, ICPAdapter, ICPCallFailed
from chains.icp_account import account_identifier

#: The anonymous principal. Valid, well-known, and controls nothing.
PRINCIPAL = "2vxsx-fae"

#: A REAL account identifier, derived rather than typed: 64 hex characters that pass
#: is_account_identifier()'s CRC32 check. A hand-written "a" * 64 does NOT -- the first
#: version of this probe used one and find_deposits_to_address() refused it before
#: scanning anything, which would have measured the address guard and reported it as the
#: scan.
ADDRESS = account_identifier(PRINCIPAL, None)
TARGET = bytes.fromhex(ADDRESS)

#: 2.44081155 ICP in e8s -- the operator's own deposit figure from 2026-10-10.
DEPOSIT_E8S = 244081155
DEPOSIT_ICP = 2.44081155


def transfer_block(to: bytes, e8s: int) -> dict:
    """One ledger block carrying a Transfer.

    `operation` IS AN OPTIONAL VARIANT, so the JSON is a LIST of zero or one element
    rather than the variant directly -- which is not guessable and is read off the live
    reply in chains/icp.transfer_operation()'s docstring. A fixture that nested it
    directly would make transfer_operation() return None for every block and this whole
    file would pass by scanning nothing.
    """
    return {"transaction": {"operation": [{"Transfer": {
        "to": list(to), "amount": {"e8s": str(e8s)},
        "from": [0] * 32, "fee": {"e8s": "10000"},
    }}]}}


def mint_block() -> dict:
    """A Mint, which is NOT a deposit.

    The local ledger's own first block is a Mint of the initial supply to the desk.
    Counting it would credit a swap with the desk's entire inventory, which is why the
    filler blocks here are Mints rather than anything inert -- the scan has to walk past
    something it must not credit.
    """
    return {"transaction": {"operation": [{"Mint": {}}]}}


class SeededLedger(ICPAdapter):
    """The REAL adapter with _call_json replaced by a ledger that honors (start, length).

    THE HONORING IS THE WHOLE POINT. tests/test_icp_adapter.py's scanning stub returns
    the same page for every call, so no existing test in this suite can observe a scan
    WINDOW at all -- which is why a 500-block trailing window survived in a file with
    good coverage. This ledger answers exactly the range it is asked for and records
    every request, so the range becomes assertable.
    """

    def __init__(self, chain_length: int, deposits: dict[int, int]):
        super().__init__(
            ledger_canister_id="bkyz2-fmaaa-aaaaa-qaaaq-cai",
            owner_principal=PRINCIPAL,
            # NEVER CALLED. `call` is the transport and every method under test goes
            # through _call_json, which is overridden below -- so a lambda that would
            # fail loudly if it were ever reached is the honest stand-in.
            call=lambda *_a, **_k: pytest.fail("the adapter reached its transport"),
        )
        self.chain_length = chain_length
        self.deposits = deposits
        self.asked: list[tuple[int, int]] = []
        self.archived_from: int | None = None
        self.send_empty_from: int | None = None

    def _call_json(self, _method, argument):
        start, length = (int(group) for group in
                         re.search(r"start = (\d+).*length = (\d+)", argument).groups())
        self.asked.append((start, length))
        archived = []
        if self.archived_from is not None and start >= self.archived_from:
            archived = [{"start": str(self.archived_from), "length": "1"}]
        blocks = [] if (self.send_empty_from is not None and start >= self.send_empty_from) else [
            transfer_block(TARGET, self.deposits[index]) if index in self.deposits else mint_block()
            for index in range(start, min(start + length, self.chain_length))
        ]
        return {"chain_length": str(self.chain_length), "archived_blocks": archived,
                "first_block_index": str(start), "blocks": blocks}


def test_a_deposit_older_than_the_old_window_is_found(caplog):
    """THE REGRESSION TEST. MUTATION: restore `start = chain_length - window`.

    chain_length 600 puts block 10 below the old 500-block window, which is the exact
    arithmetic that dropped it. 400 is the control: the same ledger with a shorter head,
    where the old code also worked -- so the difference between the two runs is the
    truncation and nothing else.
    """
    inside = SeededLedger(400, {10: DEPOSIT_E8S}).find_deposits_to_address(ADDRESS)
    below = SeededLedger(600, {10: DEPOSIT_E8S}).find_deposits_to_address(ADDRESS)

    assert [(event["txid"], event["amount"]) for event in inside] == [("10", DEPOSIT_ICP)]
    assert [(event["txid"], event["amount"]) for event in below] == [("10", DEPOSIT_ICP)], (
        "a deposit at block 10 on a 600-block chain was dropped, which is the trailing "
        "window: blocks 0..99 were never read"
    )
    assert inside == below, "the head's position must not change what a scan finds"


def test_the_scan_starts_at_the_floor_and_not_below_the_head():
    """Asserted on the REQUESTS, because the result alone cannot tell the two apart.

    A scan that read the last 600 of 600 blocks and one that read all 600 return the same
    events -- so a test that only looked at events would pass against the broken code
    whenever the chain was shorter than the window. What distinguishes them is the range
    asked for.
    """
    ledger = SeededLedger(600, {10: DEPOSIT_E8S})
    ledger.find_deposits_to_address(ADDRESS)

    # The first call is the cheap head read for chain_length (start 0, length 1).
    assert ledger.asked[0] == (0, 1)
    scans = ledger.asked[1:]
    assert scans[0][0] == 0, f"the scan began at block {scans[0][0]} rather than at the floor"
    assert sum(length for _start, length in scans) >= 600, "the whole chain was not requested"


def test_a_chain_longer_than_the_ceiling_REFUSES_rather_than_truncating():
    """The symmetry the archived-blocks branch had and this did not.

    A partial answer is refused rather than returned, because the caller cannot tell a
    truncated [] from a customer who did not pay. The refusal names the ranges, the
    ceiling, and both remedies -- rule 14: the instruction goes where the number is.
    """
    ledger = SeededLedger(MAX_BLOCKS_SCANNED * 2, {10: DEPOSIT_E8S})

    with pytest.raises(ICPCallFailed) as caught:
        ledger.find_deposits_to_address(ADDRESS)

    message = str(caught.value)
    assert str(MAX_BLOCKS_SCANNED) in message, "the ceiling is not named, so it cannot be raised"
    assert "from_block" in message, "the cursor remedy is not named"
    assert "MAINNET" in message, "the case where neither remedy is enough is not named"
    # AND NOTHING WAS SCANNED. A refusal that had already read 100,000 blocks would be a
    # refusal that cost what it was avoiding.
    assert ledger.asked == [(0, 1)], f"it scanned before refusing: {ledger.asked}"


def test_a_floor_makes_a_long_chain_affordable_again():
    """`from_block` is the escape hatch, and it is the shape a scan cursor would use.

    THERE IS NO CURSOR IN swap_terminal.db YET and this does not pretend otherwise: the
    parameter exists so that a caller which CAN establish a floor is able to pass one,
    and 0 -- the whole chain -- stays the default because that is the only floor the
    adapter can establish on its own from an address and a skip set.
    """
    ledger = SeededLedger(MAX_BLOCKS_SCANNED * 2, {MAX_BLOCKS_SCANNED * 2 - 10: DEPOSIT_E8S})

    events = ledger.find_deposits_to_address(ADDRESS, from_block=MAX_BLOCKS_SCANNED * 2 - 1000)

    assert [(event["txid"], event["amount"])
            for event in events] == [(str(MAX_BLOCKS_SCANNED * 2 - 10), DEPOSIT_ICP)]
    assert ledger.asked[1][0] == MAX_BLOCKS_SCANNED * 2 - 1000, "the floor was not honored"


def test_a_floor_at_or_past_the_head_is_a_result_and_not_a_scan(caplog):
    """Rule 14: `(none)` is a result, and this one must not look like a broken scan.

    A cursor caught up with the head is the normal steady state once one exists, so it
    has to be distinguishable from a scan that read nothing because something failed.
    """
    ledger = SeededLedger(50, {10: DEPOSIT_E8S})

    with caplog.at_level("INFO"):
        assert ledger.find_deposits_to_address(ADDRESS, from_block=50) == []

    assert ledger.asked == [(0, 1)], "it fetched blocks for an empty range"
    assert "nothing new to read" in caplog.text
    assert "RESULT, not a skipped scan" in caplog.text


def test_every_scan_logs_the_range_it_actually_read(caplog):
    """A bounded scan and a complete one used to render identically. That is what let it hide.

    So the range is on EVERY scan, including the one that found nothing -- a count of
    zero over blocks 0..1 and a count of zero over blocks 400000..400500 are different
    answers to the same question, and only one of them is reassuring.
    """
    with caplog.at_level("INFO"):
        SeededLedger(600, {}).find_deposits_to_address(ADDRESS)

    assert "read blocks 0..599" in caplog.text
    assert "chain_length=600" in caplog.text
    assert "(none)" in caplog.text, "a scan that found nothing must say so as a result"


def test_a_page_shorter_than_requested_is_not_counted_as_scanned():
    """The ledger caps a reply, and advancing by `length` would skip what it declined to send.

    MUTATION: advance by `length` instead of `first + len(blocks)`.

    THIS IS THE TRAILING WINDOW'S DEFECT BY A SECOND ROUTE and is why it is tested: a
    gap counted as scanned is a gap that silently holds the deposit. The seeded ledger
    returns min(length, chain_length - start) blocks, so a chain shorter than a page
    exercises exactly this.
    """
    # PAGE_BLOCKS + 1 so the scan needs two pages and the second is one block long.
    ledger = SeededLedger(PAGE_BLOCKS + 1, {PAGE_BLOCKS: DEPOSIT_E8S})

    events = ledger.find_deposits_to_address(ADDRESS)

    assert [(event["txid"], event["amount"])
            for event in events] == [(str(PAGE_BLOCKS), DEPOSIT_ICP)], (
        "the deposit in the final short page was missed, so the loop advanced past it"
    )


def test_an_archived_range_still_refuses_and_now_over_the_range_it_read():
    """The pre-existing refusal, which must survive the rewrite -- and now covers each page.

    It used to be computed over the one trailing window. It is now computed over every
    page the scan reads, which is strictly more of the chain and is the point: the guard
    and the scan finally look at the same blocks.
    """
    ledger = SeededLedger(PAGE_BLOCKS * 2, {10: DEPOSIT_E8S})
    ledger.archived_from = PAGE_BLOCKS  # the SECOND page reports an archive

    with pytest.raises(ICPCallFailed) as caught:
        ledger.find_deposits_to_address(ADDRESS)

    assert "archived block range" in str(caught.value)
    assert "indistinguishable from a customer who did not pay" in str(caught.value)
    # It got as far as the second page before refusing, which is what proves the guard
    # is applied per page rather than once over a window.
    assert len(ledger.asked) >= 3, f"it refused before reaching the archived page: {ledger.asked}"


def test_an_empty_page_with_no_archive_refuses_instead_of_ending_the_loop():
    """Breaking out would turn the unread remainder into an empty list.

    A ledger that sends nothing for a range it did not declare archived has not given a
    reason, and the unread blocks may hold the deposit. Ending the loop there is the
    500-block window all over again -- so it raises, and the message says how much went
    unread.
    """
    ledger = SeededLedger(PAGE_BLOCKS * 2, {10: DEPOSIT_E8S})
    ledger.send_empty_from = PAGE_BLOCKS

    with pytest.raises(ICPCallFailed) as caught:
        ledger.find_deposits_to_address(ADDRESS)

    message = str(caught.value)
    assert "returned no blocks" in message
    assert "went unread" in message
    assert str(PAGE_BLOCKS) in message, "the number of unread blocks is not named"


def test_a_mint_is_not_credited_however_many_blocks_are_scanned():
    """The filler blocks are Mints, and paging over more of them must not change that.

    The local ledger's first block is a Mint of the initial supply to the desk; counting
    it would credit a swap with the desk's entire inventory. Asserted here because the
    rewrite made the scan walk over far MORE blocks than before, so anything it must not
    credit is now encountered much more often.
    """
    assert SeededLedger(PAGE_BLOCKS + 500, {}).find_deposits_to_address(ADDRESS) == []


def test_the_skip_set_still_suppresses_an_already_settled_block():
    """Idempotency, which the wider scan makes MORE important rather than less.

    Scanning from block 0 every cycle means an already-credited deposit is re-encountered
    on every single cycle, where the trailing window would eventually have carried it out
    of range. So the skip set is now what stops a settled payment being re-reported, and
    it is load-bearing in a way it was not before.
    """
    ledger = SeededLedger(600, {10: DEPOSIT_E8S})

    assert ledger.find_deposits_to_address(ADDRESS, skip_txids=frozenset({"10"})) == []
    assert len(ledger.find_deposits_to_address(ADDRESS, skip_txids=frozenset())) == 1
