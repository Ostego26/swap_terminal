"""Would an ICP swap complete in BOTH directions? Run through the real code, measured.

Role: tests (seeds real rows into the real schema and runs the real functions)
Reads: swap_terminal/chains/icp.py, chains/icp_account.py, chains/icp_payout_identity.py,
       services/payout_service.py, services/deposit_service.py, services/helpers.py,
       chains/registry.py, config.py, and db.py's SCHEMA
Writes: a throwaway SQLite database under pytest's tmp_path
Can move funds: no. Every ledger call in this file reaches a function defined in this
       file. No socket is opened, no subprocess is started, no dfx is invoked, no
       identity is read and nothing is signed or broadcast.
Mainnet-safe: yes. There is no replica here -- `dfx` is not installed on the host this
       was written on, which is why every claim below is labeled with what produced it.

=============================================================================
WHAT THIS FILE IS FOR, AND WHAT IT CANNOT ESTABLISH
=============================================================================

ICP became a PAYOUT destination on 2026-10-11, when chains/icp_payout_identity.py
landed and `can_spend` stopped being hardcoded False. Five of Config.ALLOWED_PAIRS'
ten ICP directions now point AT ICP, so the terminal will take a customer's BTC, LTC,
GRC, SOL or XRP against a promise to pay ICP. The existing ICP tests are excellent and
they are all one layer below this question: tests/test_icp_adapter.py exercises
ICPAdapter with an injected transport, tests/test_icp_payout_arming.py exercises the
arming decision, tests/test_the_icp_scan_never_truncates_silently.py exercises the
scan. None of them runs services/payout_service.process_pending_payouts().

So this file asks the end-to-end question in each direction and answers it by observed
outcome rather than by reading that the code looks right:

    * -> ICP    seed a real swap in `payout_pending` with a real ICP account
                identifier as its payout address, hand the real payout worker a real
                ICPAdapter, and look at what reached the ledger and what the database
                says afterwards.
    ICP -> *    seed a real ICP deposit into a seeded ledger reply, hand the real
                deposit watcher a real ICPAdapter, and look at whether the swap is
                credited.

WHAT IT CANNOT ESTABLISH, said once here rather than hedged in every docstring
(CLAUDE.md rule 17). `dfx` is not installed where this was written and docker is not
usable, so NO call in this file reaches a replica. A passing test here is evidence
about THIS REPOSITORY'S behavior given a reply -- the parsing, the refusals, the
arithmetic, the database writes and the ordering. It is not evidence that the ICP
ledger answers the way StubLedger below does, and it is not evidence that
`dfx canister call --network <url> --identity <name>` can sign where
`dfx identity list` could only read. That last one is the single open question the
brief for this work names, and nothing in this file touches it.

StubLedger IS A MODEL OF THE DOCUMENTED INTERFACE, NOT A RECORDING. Its dedup rule --
(from, to, amount, fee, memo, created_at_time) within a 24-hour window -- is the rule
chains/icp.send_to_address()'s docstring states and which that file records having
measured against the real released ledger on the local replica on 2026-10-06
(TxDuplicate on the second call, desk unchanged at 999.7499). The model agreeing with
this repository proves the two agree; it does not re-measure the ledger.
"""

from __future__ import annotations

import json
import re

import pytest
import valid_addresses
from chains.icp import ICPAdapter, ICPCallFailed, transfer_argument
from chains.icp_account import ICP_DECIMALS, account_identifier, principal_to_text
from chains.registry import FINAL_AT_ONE_CONFIRMATION, build_adapters, missing_settings
from config import Config
from db import SCHEMA, apply_migrations, connect_db
from services import deposit_service
from services.helpers import iso_to_epoch_nanos
from services.payout_service import process_pending_payouts

LEDGER_CANISTER = "bkyz2-fmaaa-aaaaa-qaaaq-cai"

#: The desk's principal. DERIVED rather than spelled, like every other ICP test file
#: in this suite: a literal principal in a test is a literal address, and
#: tests/test_address_literals_are_valid.py exists because one rotted.
DESK_PRINCIPAL = principal_to_text(bytes.fromhex("00000000000000020101"))

#: The customer's ICP destination -- a 64-hex account identifier, which is what the
#: legacy `transfer` takes and what a customer's wallet hands out. Derived from a
#: DIFFERENT principal than the desk's so that a payout to it is provably not a
#: payout to ourselves.
CUSTOMER_ACCOUNT = account_identifier(principal_to_text(bytes.fromhex("00000000000000030101")))

#: What the real ledger answered on the local replica, 2026-10-06, recorded in
#: chains/icp.py's header and in config.py's ICP_NETWORK_FEE_RESERVE note. Seeded here
#: so the arithmetic is checked against a number a ledger actually produced.
REAL_FEE_E8S = 10_000
REAL_DESK_BALANCE_E8S = 100_000_000_000

#: Every Config value, so no test here can pass because a key it needed was absent.
#: Same pattern as tests/test_an_unreachable_confirmation_threshold_is_refused.py.
CONFIG = {name: getattr(Config, name) for name in dir(Config) if name.isupper()}

#: What a swap row's created_at looks like, and it is NOT a round number on purpose.
#: services/helpers.iso_to_epoch_nanos()'s docstring records that any key derived from
#: a `datetime` ends in three zeros because a datetime carries microseconds and no
#: finer -- so a fixture whose microseconds are zero would hide a derivation that
#: silently dropped them.
SWAP_CREATED_AT = "2026-10-11T04:17:09.123456+00:00"

#: THE IDEMPOTENCY KEY THE PAYOUT PATH MUST USE, derived here the one way the tree
#: offers. Asserted rather than assumed in
#: test_the_swap_row_already_carries_everything_the_send_needs below.
EXPECTED_KEY_NANOS = iso_to_epoch_nanos(SWAP_CREATED_AT)


# =========================================================================
# A MODEL OF THE LEDGER, WITH ITS DEDUP RULE AND A COUNT OF REAL TRANSFERS
# =========================================================================

_ARG_FIELDS = {
    "memo": re.compile(r"memo\s*=\s*(\d+)"),
    "e8s": re.compile(r"amount\s*=\s*record\s*\{\s*e8s\s*=\s*(\d+)"),
    "fee": re.compile(r"fee\s*=\s*record\s*\{\s*e8s\s*=\s*(\d+)"),
    "created_at": re.compile(r"timestamp_nanos\s*=\s*(\d+)"),
}
_ARG_TO = re.compile(r'to\s*=\s*blob\s*"((?:[^"\\]|\\.)*)"')


def parse_transfer_argument(argument: str) -> dict:
    """The candid record chains/icp.transfer_argument() built, read back as fields.

    A SECOND READER OF ONE FORMAT, AND THAT IS THE POINT RATHER THAN A RULE 8
    VIOLATION. transfer_argument() is the one writer of the money-moving string in
    this repository; what has never existed is anything that reads it back and
    checks the FIELDS rather than asserting on a substring. A test that asserts
    `"e8s = 98500000" in argument` passes just as happily when that number landed in
    the `fee` field instead of the `amount` field, which is exactly the transposition
    transfer_argument()'s keyword-only signature exists to prevent -- so the test for
    it has to be able to tell the two apart.

    The `to` blob is returned as HEX, undoing the uniform \\xx escaping
    chains/icp._hex_to_blob() applies, so a test can compare it against the account
    identifier a customer gave without knowing anything about candid.
    """
    fields = {}
    for name, pattern in _ARG_FIELDS.items():
        found = pattern.search(argument)
        fields[name] = int(found.group(1)) if found else None
    blob = _ARG_TO.search(argument)
    fields["to"] = (
        bytes(int(pair, 16) for pair in blob.group(1).split("\\") if pair).hex() if blob else None
    )
    return fields


class StubLedger:
    """A `call` with the ICP ledger's documented dedup behavior and a transfer count.

    WHY A COUNT OF APPLIED TRANSFERS IS THE ONLY HONEST ASSERTION for the double-pay
    question. Counting calls to `transfer` measures what this repository SENT; counting
    the transfers a ledger would APPLY measures what the customer receives, and the
    whole value of created_at_time is that the two numbers differ -- two sends with one
    key is one payment. A test that counted sends would read a correct retry as a
    double pay and would read a clock-keyed retry as safe. Both backwards.

    `lose_response_on` makes the call APPLY the transfer and then raise, which is the
    one failure this terminal cannot distinguish from a refusal (see
    services/payout_service.py's broad handler: "a timeout after the daemon accepted
    it looks identical to a refusal, and this marks both 'failed'"). It is the case
    the idempotency key exists for.
    """

    def __init__(self, *, fee_e8s: int = REAL_FEE_E8S, lose_response_on: int = 0,
                 err_reply: str = "", fail_with: str = ""):
        self.fee_e8s = fee_e8s
        self.lose_response_on = lose_response_on
        self.err_reply = err_reply
        self.fail_with = fail_with
        #: Every (method, argument) this repository asked for, in order.
        self.calls: list[tuple[str, str]] = []
        #: The transfers a ledger would have APPLIED, keyed as the ledger keys them.
        self.applied: list[dict] = []
        self._by_key: dict[tuple, int] = {}
        self._next_block = 1

    def transfers_attempted(self) -> int:
        return sum(1 for method, _ in self.calls if method == "transfer")

    def __call__(self, canister: str, method: str, argument: str, output: str = "idl") -> str:
        assert canister == LEDGER_CANISTER, f"a call went to {canister!r}, not the configured ledger"
        self.calls.append((method, argument))
        if method == "icrc1_fee":
            return f"({self.fee_e8s} : nat)"
        if method == "icrc1_balance_of":
            return f"({REAL_DESK_BALANCE_E8S} : nat)"
        if method == "transfer":
            return self._transfer(argument)
        raise AssertionError(f"the adapter called {method!r}, which this test did not seed")

    def _transfer(self, argument: str) -> str:
        if self.fail_with:
            # A transport that could not answer. The real one raises ICPCallFailed for a
            # non-zero dfx exit and for a timeout, and this is the same class so the
            # payout path sees what it would really see.
            raise ICPCallFailed(self.fail_with)
        if self.err_reply:
            return self.err_reply
        fields = parse_transfer_argument(argument)
        key = (fields["to"], fields["e8s"], fields["fee"], fields["memo"], fields["created_at"])
        if fields["fee"] != self.fee_e8s:
            # The real ledger's own refusal, and it is not retried by the adapter.
            return (
                "(variant { Err = variant { BadFee = record { expected_fee = record { "
                f"e8s = {self.fee_e8s} : nat64 }} }} }})"
            )
        if key in self._by_key:
            return (
                "(variant { Err = variant { TxDuplicate = record { "
                f"duplicate_of = {self._by_key[key]} : nat64 }} }} }})"
            )
        index = self._next_block
        self._next_block += 1
        self._by_key[key] = index
        self.applied.append(dict(fields, block_index=index))
        if self.lose_response_on and self.transfers_attempted() == self.lose_response_on:
            raise ICPCallFailed(
                f"dfx did not answer within 60s for transfer on {LEDGER_CANISTER}. The question "
                f"was NOT answered -- this is not an empty result."
            )
        return f"(variant {{ Ok = {index} : nat64 }})"


def armed_adapter(ledger: StubLedger, monkeypatch) -> ICPAdapter:
    """A real ICPAdapter, ARMED, over a stub ledger.

    The arming is the environment variable and nothing else, which is the whole of
    chains/icp_payout_identity.py's contract -- so this is what a web container
    running docker-compose.web.armed-icp.yml holds, minus the replica.
    """
    monkeypatch.setenv("ICP_PAYOUT_IDENTITY", "default")
    adapter = ICPAdapter(LEDGER_CANISTER, DESK_PRINCIPAL, call=ledger)
    assert adapter.can_spend is True, "the fixture is not armed; every payout test below is vacuous"
    return adapter


# =========================================================================
# SEEDING, AND IT IS THE REAL SCHEMA
# =========================================================================

PAYOUT_AMOUNT = 0.985


def seed_payout_pending(conn, *, swap_id: str = "s_to_icp", created_at: str = SWAP_CREATED_AT) -> None:
    """One GRC -> ICP swap in `payout_pending`: a customer's GRC taken, ICP owed.

    `payout_pending` is the state services/payout_service.process_pending_payouts()
    selects, and it is reached only after the deposit was credited -- so a swap in it
    is one where this desk already holds the customer's money. That is what makes
    every failure below an exposure rather than an inconvenience.
    """
    conn.execute(
        "INSERT INTO quotes (id, from_asset, to_asset, input_amount, quoted_rate, fee_bps,"
        " network_fee_reserve, output_amount_estimate, expires_at, created_at)"
        " VALUES ('q_to_icp','GRC','ICP',100.0,0.01,150,?,?,'2999-01-01T00:00:00+00:00',?)",
        (Config.ICP_NETWORK_FEE_RESERVE, PAYOUT_AMOUNT, created_at),
    )
    conn.execute(
        "INSERT INTO swaps (id, quote_id, from_asset, to_asset, deposit_address, payout_address,"
        " expected_input_amount, actual_input_amount, quoted_rate, fee_bps, network_fee_reserve,"
        " output_amount_estimate, status, min_confirmations, deposit_txid, created_at, updated_at,"
        " credited_at, expires_at)"
        " VALUES (?, 'q_to_icp','GRC','ICP',?,?,100.0,100.0,0.01,150,?,?,'payout_pending',6,"
        " 'a_real_grc_txid',?,?,?,'2999-01-01T00:00:00+00:00')",
        (swap_id, valid_addresses.GRC_PAYOUT, CUSTOMER_ACCOUNT, Config.ICP_NETWORK_FEE_RESERVE,
         PAYOUT_AMOUNT, created_at, created_at, created_at),
    )
    conn.execute(
        "INSERT INTO wallet_inventory (asset, hot_confirmed, hot_reserved, hot_available, updated_at)"
        " VALUES ('ICP', 1000.0, 0.0, 1000.0, ?)",
        (created_at,),
    )
    conn.commit()


@pytest.fixture
def conn(tmp_path):
    connection = connect_db(str(tmp_path / "icp_e2e.db"), create=True)
    connection.executescript(SCHEMA)
    # WHAT payout_worker.py DOES AT STARTUP, and it is load-bearing here rather than
    # ceremonial: apply_migrations() creates idx_payouts_one_live_per_swap, the partial
    # unique index that makes a second LIVE payout row impossible. A retry test run
    # without it would be measuring a different system.
    apply_migrations(connection)
    return connection


def swap_row(conn, swap_id: str = "s_to_icp") -> dict:
    return dict(conn.execute("SELECT * FROM swaps WHERE id = ?", (swap_id,)).fetchone())


def payout_rows(conn, swap_id: str = "s_to_icp") -> list[dict]:
    return [dict(r) for r in conn.execute(
        "SELECT * FROM payouts WHERE swap_id = ? ORDER BY rowid", (swap_id,))]


# =========================================================================
# * -> ICP : THE DEFECT, MEASURED THROUGH THE REAL PAYOUT WORKER
# =========================================================================


def test_the_real_payout_worker_reaches_the_ledger_and_pays_the_swap(conn, monkeypatch):
    """`* -> ICP` pays. INVERTED 2026-10-11 when broadcast_payout() learned about ICP.

    WHAT THIS TEST MEASURED WHEN IT WAS WRITTEN, hours earlier the same day, running
    the real process_pending_payouts() over the real schema with a real ICPAdapter:

        ledger calls              []          not even icrc1_fee
        swaps.status              failed
        swaps.payout_txid         None
        payouts row               status 'failed', txid None
        completed returned        0

    AND WHAT IT MEASURES NOW, same seeded row, same adapter, same stub ledger:

        ledger calls              ['icrc1_fee', 'transfer']
        swaps.status              completed
        swaps.payout_txid         1           the block index
        payouts row               status 'broadcast', txid 1
        completed returned        1 swap

    The before/after is kept in full rather than replaced, because the drift is the
    point (rule 1) and because the two columns together are the only honest statement
    of what the one-line change bought. The diagnosis follows unedited, since it is
    what produced the fix.

    THE CAUSE WAS ONE LINE IN A FILE THIS TEST MAY NOT EDIT.
    services/payout_service.broadcast_payout() dispatches on the asset: SOL gets
    `confirm_send=`, XRP gets a source and a seed, and `asset not in
    SHARED_ACCOUNT_PAYOUT_ASSETS` -- which is every other chain, ICP included -- gets

        return adapter.send_to_address(address, amount)

    two positional arguments. chains/icp.ICPAdapter.send_to_address()'s third parameter
    is `created_at_time_nanos` and it REFUSES None, because with created_at_time null
    the ledger stamps its own time and every retry is a new transaction. Measured:
    `grep -c ICP services/payout_service.py` is 0. The asset is not mentioned in the
    file that pays it.

    SO THE REFUSAL IS THE ADAPTER WORKING EXACTLY AS DESIGNED, and the shape of the
    outcome is the stranded swap this repository has now seen three times: XRP had it
    until 2026-10-02 ("every XRP payout raised XRPSendNotArmed and the swap landed in
    `failed` WITH THE CUSTOMER'S DEPOSIT ALREADY CREDITED"), SOL until 2026-10-03
    ("the same stranded-swap shape XRP had, one chain over"), ICP now. The direction
    is the safe one -- nothing is sent, so nothing is sent twice -- and it is still a
    customer whose money this desk holds and whose payout cannot happen.

    THE INVERSION FOLLOWS THE CONVENTION THIS SUITE ALREADY USES --
    tests/test_htlc_locktime_encoding.py's DEFECT assertions were inverted when the
    locktime encoder landed -- rather than deleting the test and writing a fresh one.
    It asserts the same claim with the sign flipped, so a regression lands HERE rather
    than in a file nobody connects back to the defect.

    THE KEY IS ASSERTED, NOT MERELY THE SUCCESS, and that is the line worth having. A
    payout that completed with `time.time_ns()` in `created_at_time` would satisfy
    every other assertion below and be a DOUBLE PAY waiting on one retry: the ledger
    deduplicates on that field for 24 hours, so a fresh clock value makes each retry a
    new transaction. The swap's own `created_at` is retry-stable, and
    test_the_same_swap_paid_twice_with_its_own_key_is_ONE_transfer below measures the
    consequence directly.
    """
    seed_payout_pending(conn)
    ledger = StubLedger()
    adapter = armed_adapter(ledger, monkeypatch)

    completed = process_pending_payouts(conn, CONFIG, {"ICP": adapter})

    assert len(completed) == 1, f"the swap did not complete: {completed!r}"
    assert [method for method, _ in ledger.calls] == ["icrc1_fee", "transfer"], (
        f"the ledger was asked {ledger.calls!r}; the fee must be read from it immediately "
        f"before the send rather than taken from a constant"
    )
    assert len(ledger.applied) == 1, f"expected exactly one transfer, got {ledger.applied!r}"
    sent = ledger.applied[0]
    assert sent["to"] == CUSTOMER_ACCOUNT, "the destination is not the customer's account"
    assert sent["created_at"] == EXPECTED_KEY_NANOS, (
        f"created_at_time is {sent['created_at']} and the swap's own key is "
        f"{EXPECTED_KEY_NANOS}. Anything else -- a clock read above all -- breaks the "
        f"ledger's 24-hour deduplication and makes a retry a second payment."
    )

    row = swap_row(conn)
    assert row["status"] == "completed", f"the swap is {row['status']!r}: {row['failed_reason']!r}"
    assert row["payout_txid"] == "1", f"the block index did not reach the row: {row['payout_txid']!r}"
    assert not row["failed_reason"], f"a completed payout carries a failure reason: {row['failed_reason']!r}"
    rows = payout_rows(conn)
    assert [r["status"] for r in rows] == ["broadcast"]
    assert rows[0]["txid"] == "1"


def test_the_customers_deposit_and_the_payout_both_survive_the_worker(conn, monkeypatch):
    """INVERTED 2026-10-11 with the test above, and the DEPOSIT half is unchanged.

    WHAT IT ASSERTED BEFORE: that the stranded payout was an EXPOSURE rather than an
    inconvenience, because `payout_pending` is only reached through
    services/deposit_service crediting a deposit -- so the desk held 100 GRC and owed
    0.985 ICP it could not send.

    The three deposit assertions below are the same lines they always were; only the
    status flipped. That is deliberate: this test's job is to hold the DEPOSIT side
    still while the payout side changes, so a future regression that loses the
    credited deposit is caught by the same row that caught the unpayable obligation.
    """
    seed_payout_pending(conn)
    process_pending_payouts(conn, CONFIG, {"ICP": armed_adapter(StubLedger(), monkeypatch)})

    row = swap_row(conn)
    assert row["actual_input_amount"] == 100.0
    assert row["deposit_txid"] == "a_real_grc_txid"
    assert row["credited_at"] is not None, "the premise is wrong: this swap was never credited"
    assert row["status"] == "completed", (
        f"the deposit is credited and the payout is {row['status']!r} -- the desk holds the "
        f"customer's 100 GRC and owes them ICP: {row['failed_reason']!r}"
    )


def test_the_swap_row_already_carries_everything_the_send_needs(conn):
    """NO SCHEMA CHANGE AND NO NEW COLUMN: the key is derivable from what is stored.

    This is the measurement that makes the proposed one-line fix a one-line fix.
    `swaps.created_at` is written by services/helpers.utc_now_iso(), which is
    `datetime.now(UTC).isoformat()` and therefore offset-aware, and
    services/helpers.iso_to_epoch_nanos() is the single place in the tree that turns
    one into the nanosecond key the ledger wants. Both already exist.

    THE KEY ENDS IN THREE ZEROS and that is asserted rather than noted, because
    iso_to_epoch_nanos()'s own docstring uses it to establish provenance: HANDOFF.md
    section 4 records a live ICP key, 1791646582186023936, that does NOT end in three
    zeros and therefore cannot have come from a `created_at` string. A key from this
    function is retry-stable; a key from `time.time_ns()` is not, and the last three
    digits are how a reader tells which they are holding.
    """
    seed_payout_pending(conn)
    created_at = swap_row(conn)["created_at"]
    assert created_at == SWAP_CREATED_AT

    key = iso_to_epoch_nanos(created_at)
    assert key == EXPECTED_KEY_NANOS
    assert key % 1000 == 0, (
        f"{key} does not end in three zeros, so it did not come from a datetime's "
        f"microseconds -- see services/helpers.iso_to_epoch_nanos()"
    )
    assert key > 0


def test_a_payout_driven_with_the_swaps_own_key_completes(conn, monkeypatch):
    """THE REPLACEMENT FOR THE DEFECT TEST, AND IT PASSES TODAY.

    The identical seeded swap row, the identical armed adapter, the identical stub
    ledger -- the only difference is that the key the row already carries is passed.
    The transfer goes out and a block index comes back.

    SO THE ADAPTER HALF IS NOT THE GAP. Everything the payout needs is built and
    exercised: the legacy `transfer` (not icrc1_transfer, which cannot take a 64-hex
    account identifier), the fee read from the ledger on this call rather than from a
    constant, the destination as a 32-byte blob, the amount in e8s, and the key in
    `created_at_time`. What is missing is the argument, in one line of a file this test
    may not edit.

    EVERY FIELD IS CHECKED BY NAME rather than by substring, through
    parse_transfer_argument() -- see its docstring for why `"e8s = 98500000" in
    argument` would pass just as happily with that number in the fee field.
    """
    seed_payout_pending(conn)
    ledger = StubLedger()
    adapter = armed_adapter(ledger, monkeypatch)
    row = swap_row(conn)

    block_index = adapter.send_to_address(
        row["payout_address"], row["output_amount_estimate"], iso_to_epoch_nanos(row["created_at"]),
    )

    assert block_index == "1", f"no block index came back: {block_index!r}"
    assert [method for method, _ in ledger.calls] == ["icrc1_fee", "transfer"], (
        "the fee was not read from the ledger immediately before the send"
    )
    assert len(ledger.applied) == 1
    sent = ledger.applied[0]
    assert sent["to"] == CUSTOMER_ACCOUNT, "the destination is not the customer's account"
    assert sent["to"] != account_identifier(DESK_PRINCIPAL), "it was sent to the desk's own account"
    assert sent["e8s"] == 98_500_000, f"0.985 ICP is 98500000 e8s, not {sent['e8s']}"
    assert sent["e8s"] == round(PAYOUT_AMOUNT * 10**ICP_DECIMALS)
    assert sent["fee"] == REAL_FEE_E8S, "the fee the ledger reported is not the fee that was sent"
    assert sent["fee"] != sent["e8s"], "the amount and the fee are the same number; a transposition"
    assert sent["created_at"] == EXPECTED_KEY_NANOS, "the key is not the swap's recorded creation time"
    assert sent["memo"] == 0


def test_the_same_swap_paid_twice_with_its_own_key_is_ONE_transfer(conn, monkeypatch):
    """IDEMPOTENCY, measured on applied transfers rather than on attempted ones.

    Two sends, one payment. The second gets TxDuplicate carrying the FIRST block index,
    which chains/icp.transfer_block_index() returns as the success it is -- a defect
    that file shipped and then fixed, because raising on it would make a worker mark a
    payout that SUCCEEDED as failed.
    """
    seed_payout_pending(conn)
    ledger = StubLedger()
    adapter = armed_adapter(ledger, monkeypatch)
    row = swap_row(conn)
    key = iso_to_epoch_nanos(row["created_at"])

    first = adapter.send_to_address(row["payout_address"], row["output_amount_estimate"], key)
    second = adapter.send_to_address(row["payout_address"], row["output_amount_estimate"], key)

    assert first == second == "1", f"the retry reported a different payment: {first!r} then {second!r}"
    assert ledger.transfers_attempted() == 2, "the fixture did not actually retry"
    assert len(ledger.applied) == 1, (
        f"{len(ledger.applied)} transfers were APPLIED for one swap. The customer was paid twice."
    )


def test_a_key_taken_from_a_clock_is_a_SECOND_PAYMENT(conn, monkeypatch):
    """THE DOUBLE-PAY, DEMONSTRATED, so the refusal above is not taken on faith.

    The same swap, the same amount, the same destination -- and a key that differs.
    The ledger's dedup is on all six fields together, so a different created_at_time
    makes it a different transaction and the customer is paid twice. This is what
    `created_at_time = null` does on every retry, and it is why chains/icp.py refuses
    None rather than defaulting to `time.time_ns()`.

    IT IS ALSO THE 24-HOUR LIMIT'S OTHER FACE. A swap older than the ledger's window
    cannot be paid with its own created_at (TxTooOld), and the available workaround --
    a fresh key -- is precisely this test. That decision is live posture and belongs to
    the operator, which is why neither the adapter nor this test makes it.
    """
    seed_payout_pending(conn)
    ledger = StubLedger()
    adapter = armed_adapter(ledger, monkeypatch)
    row = swap_row(conn)

    first = adapter.send_to_address(row["payout_address"], row["output_amount_estimate"],
                                   EXPECTED_KEY_NANOS)
    second = adapter.send_to_address(row["payout_address"], row["output_amount_estimate"],
                                     EXPECTED_KEY_NANOS + 1_000_000_000)

    assert first == "1"
    assert second == "2", "the ledger deduplicated two different keys, which it must not"
    assert len(ledger.applied) == 2, "the double pay was not reproduced; this test proves nothing"
    assert sum(t["e8s"] for t in ledger.applied) == 2 * 98_500_000


def test_a_lost_response_retried_with_the_same_key_does_not_pay_twice(conn, monkeypatch):
    """THE CASE THE KEY EXISTS FOR, AND THE ONE services/payout_service.py CANNOT SEE.

    That file's broad handler says it plainly: "a timeout after the daemon accepted it
    looks identical to a refusal, and this marks both 'failed'. That is a real gap on
    the fund path." On every other chain in this terminal the gap stays open -- a BTC
    `sendtoaddress` that timed out after relaying cannot be made idempotent from here.

    ICP IS THE ONE CHAIN WHERE IT CLOSES, and this is the measurement. The first call
    applies the transfer and then loses the response, exactly as a dfx timeout after
    the ledger committed would. The retry with the SAME key returns the original block
    index and applies nothing.

    AND IT CLOSES ONLY IF THE KEY COMES FROM THE SWAP ROW. With a clock key the retry
    is the test above: two payments. So the one-line fix is not merely about getting
    ICP payouts working -- it is the difference between a timeout being recoverable and
    a timeout being a coin flip.
    """
    seed_payout_pending(conn)
    ledger = StubLedger(lose_response_on=1)
    adapter = armed_adapter(ledger, monkeypatch)
    row = swap_row(conn)
    key = iso_to_epoch_nanos(row["created_at"])

    with pytest.raises(ICPCallFailed) as caught:
        adapter.send_to_address(row["payout_address"], row["output_amount_estimate"], key)
    assert "NOT answered" in str(caught.value), (
        "a lost response must not read as an answer -- see chains/icp.ICPCallFailed"
    )
    assert len(ledger.applied) == 1, "the premise is wrong: the ledger did not apply the lost transfer"

    recovered = adapter.send_to_address(row["payout_address"], row["output_amount_estimate"], key)

    assert recovered == "1", f"the retry did not recover the original block index: {recovered!r}"
    assert len(ledger.applied) == 1, (
        f"{len(ledger.applied)} transfers applied. A retry after a lost response paid twice."
    )


@pytest.mark.parametrize(
    ("name", "ledger"),
    [
        ("a dfx transport failure", StubLedger(fail_with="dfx exited 255 for transfer: connection refused")),
        ("a lost response", StubLedger(fail_with="dfx did not answer within 60s. The question was NOT answered")),
        ("InsufficientFunds -- the armed identity is not the owner principal", StubLedger(
            err_reply="(variant { Err = variant { InsufficientFunds = record { "
                      "balance = record { e8s = 0 : nat64 } } } })")),
        ("BadFee", StubLedger(
            err_reply="(variant { Err = variant { BadFee = record { "
                      "expected_fee = record { e8s = 10_000 : nat64 } } } })")),
        ("TxTooOld -- a swap older than the ledger's 24h window", StubLedger(
            err_reply="(variant { Err = variant { TxTooOld = record { "
                      "allowed_window_nanos = 86_400_000_000_000 : nat64 } } })")),
        ("a reply this adapter cannot read", StubLedger(err_reply="(variant { Something = 1 : nat64 })")),
    ],
)
def test_no_failure_mode_produces_a_block_index(name, ledger, monkeypatch):
    """A FAILED PAYOUT CANNOT BE RECORDED AS SENT, and that is a property of the return.

    Every one of these is a real reply or a real transport failure, and the adapter must
    raise rather than return something a caller could write into `payouts.txid`. The
    assertion is on the TYPE of the outcome, not on the message: a block index is the
    only value that may come back from this method, so "did not return one" and "raised"
    have to be the same thing.

    INSUFFICIENTFUNDS IS THE ONE WORTH READING TWICE. It is what happens when
    ICP_PAYOUT_IDENTITY names an identity whose principal is NOT ICP_OWNER_PRINCIPAL:
    armed, reaching the ledger, holding nothing. chains/icp_payout_identity.py says so
    out loud and says nothing verifies the match, because confirming it needs a dfx call
    in a constructor on a page render. The consequence is bounded here: a refusal after
    the deposit was taken, not a send from the wrong account.
    """
    monkeypatch.setenv("ICP_PAYOUT_IDENTITY", "default")
    adapter = ICPAdapter(LEDGER_CANISTER, DESK_PRINCIPAL, call=ledger)

    with pytest.raises(ICPCallFailed):
        adapter.send_to_address(CUSTOMER_ACCOUNT, PAYOUT_AMOUNT, EXPECTED_KEY_NANOS)

    assert ledger.applied == [], f"{name}: a transfer was applied by a call that raised"


def test_a_refused_fee_is_not_retried_at_the_expected_one(monkeypatch):
    """A RETRY IS A SECOND SEND, so BadFee refuses and reports what the ledger said.

    The adapter reads the fee from the ledger immediately before the send, so a BadFee
    means the ledger changed its fee between those two calls -- and the tempting fix,
    sending again at `expected_fee`, is a second transfer with a second key. Measured
    here: one transfer attempted, none applied.
    """
    monkeypatch.setenv("ICP_PAYOUT_IDENTITY", "default")
    # The adapter reads 10_000 from icrc1_fee and this ledger wants 20_000, which is
    # the live race rather than a contrived mismatch.
    class ShiftingFeeLedger(StubLedger):
        def __call__(self, canister, method, argument, output="idl"):
            if method == "icrc1_fee":
                self.calls.append((method, argument))
                return f"({REAL_FEE_E8S} : nat)"
            return super().__call__(canister, method, argument, output)

    ledger = ShiftingFeeLedger(fee_e8s=20_000)
    adapter = ICPAdapter(LEDGER_CANISTER, DESK_PRINCIPAL, call=ledger)

    with pytest.raises(ICPCallFailed) as caught:
        adapter.send_to_address(CUSTOMER_ACCOUNT, PAYOUT_AMOUNT, EXPECTED_KEY_NANOS)

    assert "BadFee" in str(caught.value), "the message does not name the ledger's own refusal"
    assert ledger.transfers_attempted() == 1, "the fee mismatch was retried, which is a second send"
    assert ledger.applied == []


def test_a_payout_to_a_principal_is_refused_before_the_ledger_is_touched(monkeypatch):
    """A PRINCIPAL IS NOT A LEDGER ADDRESS, and it must fail rather than be converted.

    The two encodings are not interchangeable and cannot be: an account identifier is
    SHA224 over (principal, subaccount) and is not invertible, which is the whole reason
    send_to_address() calls the legacy `transfer` instead of icrc1_transfer. A customer
    who pastes a principal into the payout field has given this terminal something it
    cannot pay, and the refusal arrives before the fee is read.
    """
    monkeypatch.setenv("ICP_PAYOUT_IDENTITY", "default")
    ledger = StubLedger()
    adapter = ICPAdapter(LEDGER_CANISTER, DESK_PRINCIPAL, call=ledger)

    assert adapter.validate_address(CUSTOMER_ACCOUNT) is True
    assert adapter.validate_address(DESK_PRINCIPAL) is False, "a principal passed the address check"

    with pytest.raises(ICPCallFailed) as caught:
        adapter.send_to_address(DESK_PRINCIPAL, PAYOUT_AMOUNT, EXPECTED_KEY_NANOS)

    assert "A principal is a different thing" in str(caught.value)
    assert ledger.calls == [], f"the ledger was asked {ledger.calls!r} before the address was checked"


def test_a_one_character_corruption_of_the_destination_is_refused(monkeypatch):
    """THE CHECKSUM, NOT THE SHAPE. Any 64 hex characters pass a length-and-alphabet test.

    A truncated or mistyped copy-paste is 64 hex characters whose CRC32 does not match,
    and on a payout path that is funds sent somewhere nobody can spend them. This mutates
    one character of a real account identifier and asserts the send refuses.
    """
    monkeypatch.setenv("ICP_PAYOUT_IDENTITY", "default")
    ledger = StubLedger()
    adapter = ICPAdapter(LEDGER_CANISTER, DESK_PRINCIPAL, call=ledger)

    swapped = "0" if CUSTOMER_ACCOUNT[0] != "0" else "1"
    corrupted = swapped + CUSTOMER_ACCOUNT[1:]
    assert len(corrupted) == 64 and all(c in "0123456789abcdef" for c in corrupted)

    with pytest.raises(ICPCallFailed):
        adapter.send_to_address(corrupted, PAYOUT_AMOUNT, EXPECTED_KEY_NANOS)
    assert ledger.calls == []


def test_the_candid_this_repository_builds_is_one_string_with_one_writer():
    """transfer_argument() is what the adapter sends, byte for byte.

    chains/icp.py builds the money-moving record in exactly one place and
    fund_desk.py's mint reads the same function, which is why the fee is a parameter
    with no default. This pins that the adapter does not assemble its own variant of
    it, by comparing the argument the ledger received against the function's output.
    """
    expected = transfer_argument(
        to_account=CUSTOMER_ACCOUNT, e8s=98_500_000, fee_e8s=REAL_FEE_E8S,
        created_at_time_nanos=EXPECTED_KEY_NANOS,
    )
    ledger = StubLedger()
    adapter = ICPAdapter(LEDGER_CANISTER, DESK_PRINCIPAL, call=ledger)
    adapter.can_spend = True

    adapter.send_to_address(CUSTOMER_ACCOUNT, PAYOUT_AMOUNT, EXPECTED_KEY_NANOS)

    sent = [argument for method, argument in ledger.calls if method == "transfer"]
    assert sent == [expected], "the adapter sent a candid record transfer_argument() did not build"


# =========================================================================
# ICP -> * : THE DEPOSIT SIDE
# =========================================================================

DEPOSIT_E8S = 232_846_520


def deposit_block(address: str) -> dict:
    """One ledger block holding a Transfer into `address`, in the JSON shape dfx emits.

    TWO SHAPE FACTS, both recorded in chains/icp._call_json()'s docstring as measured
    against the live ledger rather than assumed: a blob is a LIST OF INTEGERS, and every
    number is a STRING because candid nat64 exceeds what JSON numbers promise.
    """
    return {"transaction": {"operation": [{"Transfer": {
        "to": list(bytes.fromhex(address)),
        "from": list(bytes(32)),
        "amount": {"e8s": str(DEPOSIT_E8S)},
    }}]}}


def empty_block() -> dict:
    """Somebody else's block. The ICP ledger is ONE shared history."""
    return {"transaction": {"operation": []}}


class ScanLedger:
    """A `call` answering query_blocks over a seeded chain, honoring `start`.

    THE CALL COUNT IS BOUNDED AND THE BOUND IS AN ASSERTION. A scan that does not
    terminate would hang pytest rather than fail it, and a test that can hang is a test
    nobody runs twice -- so this raises after `max_calls`, which turns non-termination
    into a named failure. That is what makes the two guard tests below mutation-checkable
    at all.
    """

    def __init__(self, blocks: list[dict], *, first_override=None, max_calls: int = 200):
        self.blocks = blocks
        self.first_override = first_override
        self.max_calls = max_calls
        self.requests: list[tuple[int, int]] = []

    def __call__(self, canister: str, method: str, argument: str, output: str = "idl") -> str:
        assert method == "query_blocks", f"the scan called {method!r}"
        if len(self.requests) >= self.max_calls:
            raise AssertionError(
                f"the scan made {len(self.requests)} query_blocks requests without finishing. "
                f"It is not terminating -- see chains/icp._blocks_page()'s first_block_index guard."
            )
        start = int(re.search(r"start\s*=\s*(\d+)", argument).group(1))
        length = int(re.search(r"length\s*=\s*(\d+)", argument).group(1))
        self.requests.append((start, length))
        page = self.blocks[start : start + length]
        first = start if self.first_override is None else self.first_override
        return json.dumps({
            "chain_length": str(len(self.blocks)),
            "first_block_index": str(first),
            "archived_blocks": [],
            "blocks": page or [empty_block()],
        })


def scanning_adapter(ledger) -> ICPAdapter:
    return ICPAdapter(LEDGER_CANISTER, DESK_PRINCIPAL, call=ledger)


def test_a_page_that_begins_ABOVE_the_requested_start_refuses_instead_of_reporting_no_deposits():
    """THE FIX, 2026-10-11. A silent [] where a deposit existed.

    MEASURED BEFORE THE FIX, through the real find_deposits_to_address() with a seeded
    reply -- chain_length 10, first_block_index 7, three blocks, archived_blocks EMPTY:

        returned                 []
        blocks 0..6              never examined, and block 1 held the deposit
        the rule 14 log line     "read blocks 0..9 of chain_length=10 (10 block(s))
                                 and found (none)"

    That log line is the one this method grew specifically so a bounded scan and a
    complete one would not render identically, and it printed a range the scan had not
    read. The caller advanced `scanned_to = first + len(blocks)` from the ledger's
    `first` rather than from what it asked for, so a page starting above the request
    jumped the gap and counted it as scanned.

    IT IS THE THIRD ROUTE TO ONE DEFECT. archived_blocks was the first and the trailing
    500-block window was the second, and both are refused in this file with the same
    sentence: a range the scan needed and did not read is indistinguishable, to the
    caller, from a customer who did not pay.

    MUTATION, RUN 2026-10-11: delete the `if first != start` branch from
    chains/icp._blocks_page() and this test fails with `[] == [deposit]` -- the scan
    returns no deposits for a chain that contains one. Restored.
    """
    deposit_address = account_identifier(DESK_PRINCIPAL, (1).to_bytes(32, "big"))
    chain = [empty_block(), deposit_block(deposit_address)] + [empty_block()] * 8
    adapter = scanning_adapter(ScanLedger(chain, first_override=7))

    with pytest.raises(ICPCallFailed) as caught:
        adapter.find_deposits_to_address(deposit_address)

    message = str(caught.value)
    assert "page beginning at 7" in message, f"the reply's own first_block_index is not named: {message}"
    assert "never be examined" in message, "the consequence is not stated next to the numbers (rule 14)"
    assert "archived_blocks" in message, "the remedy the ledger would have used is not named"


def test_a_page_that_does_not_ADVANCE_refuses_instead_of_scanning_forever():
    """THE SAME FIX, THE OTHER DIRECTION: a hang rather than a wrong answer.

    MEASURED BEFORE THE FIX with a ledger reporting chain_length 5000 and always
    answering from block 0 with one block: `scanned_to` recomputes to the same value on
    every iteration, so the `while` loop never ends. The probe was still issuing
    requests when it was cut off at 50, each one a dfx subprocess. A deposit watcher
    that hangs is rule 14's worst case -- an operator cannot tell it from one that is
    waiting for a confirmation, and the way that resolves is Ctrl-C.

    THE METHOD'S OWN DOCSTRING HAD ALREADY NAMED NON-TERMINATION as the reason an empty
    page is refused: "there is no progress to make from it and the loop would not
    terminate". A non-empty page that does not advance makes exactly as little progress,
    and the guard written for that hazard was looking at the other case.

    MUTATION, RUN 2026-10-11: delete the `if first != start` branch and this test fails
    on ScanLedger's call bound -- "the scan made 200 query_blocks requests without
    finishing" -- rather than hanging the suite. Restored.
    """
    chain = [empty_block()] * 5000
    adapter = scanning_adapter(ScanLedger(chain, first_override=0, max_calls=200))

    with pytest.raises(ICPCallFailed) as caught:
        adapter.find_deposits_to_address(account_identifier(DESK_PRINCIPAL, (1).to_bytes(32, "big")))

    message = str(caught.value)
    assert "would make no progress" in message, f"the non-termination is not named: {message}"
    assert "would not terminate" in message


def test_a_deposit_low_in_a_long_chain_is_found_across_pages():
    """FORWARD FROM A FLOOR, so a deposit is not pushed out of a trailing window.

    THE BLOCK INDEX IS GLOBAL across the whole ledger -- every other swap's deposit,
    every payout and every fund_desk.py top-up advances it -- so a window measured from
    the head is consumed by traffic that has nothing to do with this address. This seeds
    the deposit at index 1 of a 2,500-block chain, which spans three pages at
    PAGE_BLOCKS=1000, and asserts it is still found.
    """
    deposit_address = account_identifier(DESK_PRINCIPAL, (7).to_bytes(32, "big"))
    chain = [empty_block(), deposit_block(deposit_address)] + [empty_block()] * 2498
    ledger = ScanLedger(chain)
    adapter = scanning_adapter(ledger)

    found = adapter.find_deposits_to_address(deposit_address)

    assert len(found) == 1, f"a deposit at block 1 of a 2500-block chain was not found: {found}"
    assert found[0]["txid"] == "1", "the txid is not the ledger block index"
    assert found[0]["vout"] == 0
    assert found[0]["amount"] == DEPOSIT_E8S / 10**ICP_DECIMALS
    assert found[0]["confirmations"] == 1
    # One head probe plus three pages. Asserted so a regression that re-read the chain
    # per block, or that stopped paging, is visible as a count rather than as a timeout.
    assert [length for _, length in ledger.requests] == [1, 1000, 1000, 500], ledger.requests


def test_a_real_icp_deposit_is_CREDITED_through_the_real_deposit_watcher(conn):
    """ICP -> * END TO END: the real adapter's event, the real watcher, one confirmation.

    tests/test_an_unreachable_confirmation_threshold_is_refused.py already measured that
    ICP_MIN_CONFIRMATIONS=2 strands every ICP deposit forever and that 1 credits it --
    with a stub ledger whose find_deposits_to_address() returns a hand-built event. This
    runs the same path with the REAL ICPAdapter over a seeded query_blocks reply, so what
    is asserted is that the event the real scan BUILDS -- its txid, its e8s division, its
    confirmations -- is one the real watcher credits.

    ONE CONFIRMATION IS FINAL AND THAT IS A PROPERTY OF THE LEDGER rather than a
    threshold this terminal chose. A transfer is in a block or it is not: there is no
    reorg to wait out and no mempool to clear, which is why ICP is the only member of
    chains/registry.FINAL_AT_ONE_CONFIRMATION. That claim is NOT measured here -- no
    replica was reachable -- it is read off the protocol and off the ledger's own
    interface, which returns a block index from `transfer` and offers no depth anywhere.
    What IS measured is that this repository credits at 1 and demands nothing more.
    """
    assert sorted(FINAL_AT_ONE_CONFIRMATION) == ["ICP"]
    deposit_address = account_identifier(DESK_PRINCIPAL, (3).to_bytes(32, "big"))
    conn.execute(
        "INSERT INTO quotes (id, from_asset, to_asset, input_amount, quoted_rate, fee_bps,"
        " network_fee_reserve, output_amount_estimate, expires_at, created_at)"
        " VALUES ('q_from_icp','ICP','GRC',?,409.7,150,0.001,1000.0,"
        "'2999-01-01T00:00:00+00:00',?)",
        (DEPOSIT_E8S / 10**ICP_DECIMALS, SWAP_CREATED_AT),
    )
    conn.execute(
        "INSERT INTO swaps (id, quote_id, from_asset, to_asset, deposit_address, payout_address,"
        " expected_input_amount, quoted_rate, fee_bps, network_fee_reserve,"
        " output_amount_estimate, status, min_confirmations, expires_at, created_at, updated_at)"
        " VALUES ('s_from_icp','q_from_icp','ICP','GRC',?,?,?,409.7,150,0.001,1000.0,"
        "'awaiting_deposit',1,'2999-01-01T00:00:00+00:00',?,?)",
        (deposit_address, valid_addresses.GRC_PAYOUT, DEPOSIT_E8S / 10**ICP_DECIMALS,
         SWAP_CREATED_AT, SWAP_CREATED_AT),
    )
    conn.commit()

    chain = [empty_block(), deposit_block(deposit_address), empty_block()]
    adapter = scanning_adapter(ScanLedger(chain))
    adapter.asset = "ICP"

    deposit_service.process_active_swaps(conn, CONFIG, {"ICP": adapter})

    row = dict(conn.execute("SELECT * FROM swaps WHERE id = 's_from_icp'").fetchone())
    assert row["credited_at"] is not None, (
        f"a final ICP deposit was not credited; the swap is in {row['status']!r} with "
        f"actual_input_amount {row['actual_input_amount']!r}"
    )
    assert row["actual_input_amount"] == DEPOSIT_E8S / 10**ICP_DECIMALS
    events = [dict(r) for r in conn.execute(
        "SELECT * FROM deposit_events WHERE asset = 'ICP' ORDER BY rowid")]
    assert len(events) == 1, f"one payment produced {len(events)} rows: {events}"
    assert events[0]["txid"] == "1", "the block index is not the txid"
    assert events[0]["vout"] == 0
    assert events[0]["confirmations"] == 1


def test_a_mint_is_never_credited_as_a_deposit():
    """THE LEDGER'S OWN FIRST BLOCK IS A MINT of the desk's entire inventory.

    icp_ledger_init.py writes the initial supply into the ledger's init arguments, which
    the ledger records as a Mint to the desk's account. Counting it would credit whichever
    swap scanned first with the whole inventory. Pinned here because the deposit tests
    above seed a chain whose block 0 is empty, so nothing else in this file would notice
    the filter being removed.
    """
    desk = account_identifier(DESK_PRINCIPAL)
    mint = {"transaction": {"operation": [{"Mint": {
        "to": list(bytes.fromhex(desk)), "amount": {"e8s": str(REAL_DESK_BALANCE_E8S)}}}]}}
    adapter = scanning_adapter(ScanLedger([mint, empty_block()]))

    assert adapter.find_deposits_to_address(desk) == []


def test_every_ICP_direction_the_terminal_offers_is_in_the_pair_table():
    """TEN DIRECTIONS, measured, and five of them now point AT ICP.

    This is the denominator behind the payout tests above: `* -> ICP` being offered is
    what makes the created_at_time gap a customer-facing failure rather than a latent
    one. If an operator ever disables the five inbound directions, the defect test at the
    top of this file stops describing a live exposure -- so the count is asserted here
    rather than left in prose.
    """
    icp_pairs = {pair for pair in Config.ALLOWED_PAIRS if "ICP" in pair}
    assert len(icp_pairs) == 10, f"ICP has {len(icp_pairs)} directions, not ten: {sorted(icp_pairs)}"

    to_icp = sorted(source for source, destination in icp_pairs if destination == "ICP")
    from_icp = sorted(destination for source, destination in icp_pairs if source == "ICP")
    assert to_icp == ["BTC", "GRC", "LTC", "SOL", "XRP"], to_icp
    assert from_icp == ["BTC", "GRC", "LTC", "SOL", "XRP"], from_icp


def test_ICP_CANNOT_BE_ARMED_WITHOUT_A_PAYER(monkeypatch):
    """THE SOL DEFECT'S SHAPE, CHECKED FOR AND MEASURED ABSENT ON ICP.

    A sibling review on 2026-10-11 found that chains/solana.py derived `can_spend` from
    the KEYPAIR alone, so a host holding the keypair and no SOL_HOT_WALLET badged every
    `* -> SOL` pair available with no account to pay from -- the same shape as the XRP
    nine-character placeholder that made can_spend True for a seed that could never
    sign. ICP derives can_spend from ONE variable too (ICP_PAYOUT_IDENTITY), so the
    question has to be asked here rather than assumed answered.

    IT CANNOT HAPPEN ON ICP, and the reason is structural rather than a second check:
    the payer IS ICP_OWNER_PRINCIPAL, and that is one of the two values
    chains/registry.build_adapters() requires before it constructs an ICPAdapter at all.
    No principal means no adapter, which means ICP is not offered as a destination --
    rather than offered with nothing behind it. Measured 2026-10-11 through the real
    build_adapters() with ICP_PAYOUT_IDENTITY set:

        both set                 adapter BUILT, can_spend True, missing_settings []
        owner_principal ""       adapter NOT built, missing_settings ['ICP_OWNER_PRINCIPAL']
        ledger_canister_id ""    adapter NOT built, missing_settings ['ICP_LEDGER_CANISTER_ID']

    WHAT IS NOT CLOSED, AND IT IS NAMED AT ITS OWN SITE RATHER THAN HERE.
    chains/icp_payout_identity.payout_identity_is_present() says out loud that presence
    is not correctness: an identity name with no PEM behind it, or a PEM whose principal
    is not ICP_OWNER_PRINCIPAL, reads armed and fails at the send. That is bounded to a
    post-deposit refusal rather than a wrong-account send --
    test_no_failure_mode_produces_a_block_index covers the InsufficientFunds case, which
    is exactly what a non-owner identity produces -- and closing it needs a dfx call,
    which would mean a subprocess on every page render because an adapter is built on
    one. It belongs in a startup or admin pre-flight, not in a constructor.
    """
    monkeypatch.setenv("ICP_PAYOUT_IDENTITY", "default")
    configured = {"ledger_canister_id": LEDGER_CANISTER, "owner_principal": DESK_PRINCIPAL,
                  "network_url": "http://icp-replica:4943"}

    assert build_adapters({"ICP": dict(configured)})["ICP"].can_spend is True

    for missing in ("owner_principal", "ledger_canister_id"):
        rpc = {"ICP": dict(configured, **{missing: ""})}
        assert "ICP" not in build_adapters(rpc), (
            f"an ICP adapter was built with {missing} empty. Armed with no payer is the "
            f"SOL/XRP defect, and here it would offer five `* -> ICP` directions that "
            f"cannot be paid."
        )
        assert missing_settings(rpc, "ICP"), f"{missing} being empty is not reported as missing"
