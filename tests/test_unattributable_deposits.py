"""Money that arrived at the shared account and belongs to no swap gets a durable row.

Behavioral, against the real SCHEMA and the real functions -- no paraphrase of the SQL, no
assertion on the text of a statement. Seed, run, query the table, assert on the rows present
(BEHAVIORAL_VERIFICATION_PRINCIPLE, absorbed into CLAUDE.md).

THE HOLE THESE TESTS CLOSE, measured 2026-10-01 rather than supposed:

    chains/solana.py recorded a dropped credit on `unattributable_drops` and logged it at
    WARNING. That attribute was written there and read NOWHERE under swap_terminal/ -- the
    only consumer in the repository was the diagnostic script. Eight devnet runs found
    between one and three such credits each. On devnet they are nobody's; on mainnet each one
    is a deposit a human has to match by hand, and a log line was the only thing that would
    have told them.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))
sys.path.insert(0, str(REPO_ROOT / "swap_terminal"))

from db import SCHEMA, connect_db  # noqa: E402

from swap_terminal.chains.solana import UnattributableCredit  # noqa: E402
from swap_terminal.services import deposit_service  # noqa: E402
from swap_terminal.services.unattributable_deposit_service import (  # noqa: E402
    StrandedDeposit,
    record,
    stranded_rows,
)

#: The real stranded deposit on the operator's devnet account, from eight runs of
#: solana_chain_check.py. Using the real signature rather than a made-up one keeps the fixture
#: honest about length and alphabet -- two earlier test seeds in this suite were invalid
#: because a signature was built with f"{n:02d}" and `0` is not in base58.
SIGNATURE = "2K2Pw1HzJs3qH5kY2dRZ1HPvmxKnddCyYt3UoChtXEMxCz5LkHe1N2Gq4Fqg5hxTSwj4sENx65j1iBrDA54BUtMc"
ACCOUNT = "J5wn3xEMDsr9r8qtF6YTWJodmgW5kG3ZThqDb8Xc37JM"
WHY = "no memo instruction -- unattributable, and a human has to match it"


@pytest.fixture
def db(tmp_path):
    """A real database on the real SCHEMA, so the table's constraints are the ones under test."""
    conn = connect_db(str(tmp_path / "swap_terminal_test.db"))
    conn.executescript(SCHEMA)
    return conn


def a_drop(signature=SIGNATURE, amount=0.5, credits=1):
    return UnattributableCredit(signature=signature, credits=credits, why=WHY,
                               amount=amount, address=ACCOUNT)


def rows_in(db):
    """Rows as DICTS, because db.connect_db sets row_factory = dict_factory.

    Asserted by name throughout rather than by position: a positional read of a twelve-column
    row is unreadable at the point of failure, and it broke the moment the factory was looked
    at -- the first version of this file indexed `row[3]` and got KeyError: 0.
    """
    return db.execute(
        "SELECT * FROM unattributable_deposits ORDER BY id"
    ).fetchall()


class Adapter:
    """Stands in for a tag-attributed adapter: it records drops during a scan, like the real one.

    NOT A MOCK OF THE RECORDER. The real SolanaAdapter populates `unattributable_drops` inside
    find_deposits_to_address(); this reproduces that ONE behavior so the wiring under test is
    the wiring, not a hand-passed list.
    """

    def __init__(self, drops=(), events=()):
        self.unattributable_drops = list(drops)
        self._events = list(events)

    def find_deposits_to_address(self, _address, **_kwargs):
        return self._events


class UTXOAdapter:
    """A chain where the ADDRESS identifies the swap, so nothing can be unattributable."""

    def find_deposits_to_address(self, _address, **_kwargs):
        return []


def test_a_stranded_deposit_becomes_a_row_with_its_amount():
    """The amount is the point. A record of money that omits it is not a record of money.

    MUTATION: drop `amount` from stranded_rows() -- or restore the pre-2026-10-01
    UnattributableCredit that carried only a COUNT -- and this fails.
    """
    rows = stranded_rows([a_drop(amount=0.5, credits=2)], "SOL")
    assert rows == [StrandedDeposit(asset="SOL", txid=SIGNATURE, address=ACCOUNT, amount=0.5,
                                    credits=2, why=WHY, confirmations=0, discriminator=None)]


def test_the_discriminator_is_NULL_when_the_deposit_carried_none():
    """Two different support conversations, and the column has to tell them apart.

    NULL means "sent with no reference at all"; an integer would mean "sent with a reference
    that matches no open order". The Solana drop is always the first by construction -- it
    happens because deposit_tag_from() found no usable memo -- and inventing a 0 here would
    make an unreferenced deposit indistinguishable from one referencing tag 0, which is a
    legal DestinationTag.
    """
    assert stranded_rows([a_drop()], "SOL")[0].discriminator is None


def test_confirmations_are_ZERO_rather_than_a_plausible_guess():
    """`_attributable` does not carry the commitment rank, so this source does not know it.

    Zero means "not known from this source". MUTATION: default it to 1 and the table carries
    an invented confirmation count on a money record -- rule 17's register failure, written
    into SQL where the next reader cannot tell it from a measurement.
    """
    assert stranded_rows([a_drop()], "SOL")[0].confirmations == 0
    assert stranded_rows([a_drop()], "SOL", confirmations=32)[0].confirmations == 32, (
        "and a source that DOES know hands it over"
    )


def test_no_drops_is_no_rows_and_not_an_error():
    assert stranded_rows([], "SOL") == []
    assert stranded_rows(None, "SOL") == []


def test_the_row_lands_in_the_real_table(db):
    """Seed, run the real function, query the real table (behavioral verification)."""
    new = record(db, stranded_rows([a_drop(amount=0.25)], "SOL"), now="2026-10-01T00:00:00+00:00")
    assert new == 1
    rows = rows_in(db)
    assert len(rows) == 1
    row = rows[0]
    assert (row["asset"], row["txid"], row["address"]) == ("SOL", SIGNATURE, ACCOUNT)
    assert row["amount"] == 0.25
    assert (row["credits"], row["discriminator"], row["why"]) == (1, None, WHY)
    assert row["confirmations"] == 0
    assert row["first_seen_at"] == row["last_seen_at"] == "2026-10-01T00:00:00+00:00"
    assert (row["resolved_at"], row["resolution_note"]) == (None, None), (
        "nothing is resolved by being recorded"
    )


def test_a_second_sighting_touches_the_row_instead_of_adding_one(db):
    """IDEMPOTENT, BECAUSE THE WATCHER POLLS.

    find_deposits_to_address() runs once per active swap, every cycle, over the SAME shared
    account -- chains/xrp.py already measured this: two unattributable payments printed four
    lines because two swaps each scanned the same account. A row per sighting would turn one
    stranded deposit into a growing pile of identical support tickets.

    MUTATION: plain INSERT instead of ON CONFLICT and this fails on the UNIQUE constraint;
    INSERT OR IGNORE and last_seen_at stops advancing, so "is it still there" becomes
    unanswerable without opening the chain.
    """
    record(db, stranded_rows([a_drop()], "SOL"), now="2026-10-01T00:00:00+00:00")
    new = record(db, stranded_rows([a_drop()], "SOL"), now="2026-10-01T00:05:00+00:00")

    assert new == 0, "not new -- the count is of first sightings, which is what warns"
    rows = rows_in(db)
    assert len(rows) == 1, "one deposit, one row, however many times it is seen"
    assert rows[0]["first_seen_at"] == "2026-10-01T00:00:00+00:00", "when it ARRIVED"
    assert rows[0]["last_seen_at"] == "2026-10-01T00:05:00+00:00", "advances, so staleness shows"


def test_a_resolved_row_is_NOT_reopened_by_a_later_sighting(db):
    """A person wrote that resolution and a poll must not undo it every cycle.

    This is the one field in the table that must not be driven by the scan. The deposit is
    still on chain and still visible, so it will be seen again forever; if a sighting cleared
    `resolved_at`, every resolved ticket would reopen on the next worker cycle.

    MUTATION: add resolved_at or resolution_note to the ON CONFLICT DO UPDATE SET list and
    this fails.
    """
    record(db, stranded_rows([a_drop()], "SOL"), now="2026-10-01T00:00:00+00:00")
    db.execute(
        "UPDATE unattributable_deposits SET resolved_at = ?, resolution_note = ?"
        " WHERE txid = ?",
        ("2026-10-01T01:00:00+00:00", "matched to swap s_123 by hand", SIGNATURE),
    )
    record(db, stranded_rows([a_drop()], "SOL"), now="2026-10-01T02:00:00+00:00")

    row = rows_in(db)[0]
    assert row["resolved_at"] == "2026-10-01T01:00:00+00:00", "the human's resolution survives"
    assert row["resolution_note"] == "matched to swap s_123 by hand"
    assert row["last_seen_at"] == "2026-10-01T02:00:00+00:00", "still touched, so staleness shows"


def test_two_assets_with_the_same_txid_are_two_rows(db):
    """The key is (asset, txid), not txid. Chain-agnostic by construction, so it has to be."""
    record(db, [*stranded_rows([a_drop()], "SOL"), *stranded_rows([a_drop()], "XRP")],
           now="2026-10-01T00:00:00+00:00")
    assert {row["asset"] for row in rows_in(db)} == {"SOL", "XRP"}


def test_the_table_holds_no_swap_id_at_all():
    """Not now and not later, and the reason is in db.py's comment on the table.

    A swap_id here would make this table a second opinion on who owns a deposit, and rule 15's
    "no decision may be read from a buffer" applies to a table in the authority as much as to
    a staging file. A resolution is a person's NOTE.

    MUTATION: add the column (or a nullable swap_id to deposit_events instead) and this fails,
    which is the point -- the next person to reach for the obvious answer gets told why it was
    not taken rather than finding it half-built.
    """
    columns = [line for line in SCHEMA.splitlines()
               if "swap_id" in line and "unattributable" not in line]
    assert columns, "deposit_events still has its swap_id -- this test is about the OTHER table"

    start = SCHEMA.index("CREATE TABLE IF NOT EXISTS unattributable_deposits")
    end = SCHEMA.index(");", start)
    assert "swap_id" not in SCHEMA[start:end], (
        "a swap_id in this table is a second source of truth about who owns a deposit"
    )
    assert "FOREIGN KEY" not in SCHEMA[start:end], (
        "and a foreign key to swaps would make an unattributable deposit unrecordable, which "
        "is exactly why deposit_events could not hold it"
    )


# ---------------------------------------------------------------------------
# THE WIRING. Three main()-level mutations in this session survived because a function was
# right and its call site discarded the result, so the live path is driven here rather than
# the recorder alone.
# ---------------------------------------------------------------------------


def test_the_live_scan_records_what_nobody_can_claim(db):
    """refresh_swap_from_chain()'s scan is where the drops exist, so that is where they are read.

    MUTATION: delete the record_what_nobody_can_claim() call from refresh_swap_from_chain and
    the table stays empty -- which is the state this whole change found, and nothing failed.
    """
    adapter = Adapter(drops=[a_drop(amount=0.75)])
    new = deposit_service.record_what_nobody_can_claim(db, "SOL", adapter)

    assert new == 1
    assert len(rows_in(db)) == 1
    assert rows_in(db)[0]["amount"] == 0.75


def test_a_utxo_adapter_has_no_drops_and_is_not_asked_to_invent_any(db):
    """BTC, LTC and GRC attribute by ADDRESS, so nothing can be unattributable there.

    hasattr is the honest test and keeps this function from holding a list of which chains are
    tag-attributed -- a hand-maintained copy of that vocabulary is rule 11's failure with a
    delay on it.

    MUTATION: index the attribute instead of getattr-ing it and every UTXO swap refresh raises
    AttributeError on the live path.
    """
    assert deposit_service.record_what_nobody_can_claim(db, "BTC", UTXOAdapter()) == 0
    assert rows_in(db) == []


def test_the_drops_list_is_not_cleared_by_recording(db):
    """The adapter owns that list's lifetime -- it resets per scan.

    MUTATION: clear it here and the diagnostic reads an emptied list depending on call order,
    so two owners for one piece of state and a report that depends on who ran first.
    """
    adapter = Adapter(drops=[a_drop()])
    deposit_service.record_what_nobody_can_claim(db, "SOL", adapter)
    assert len(adapter.unattributable_drops) == 1, "still the adapter's"


def test_recording_a_stranded_deposit_credits_nothing(db):
    """It is a record for a human, never an input to a decision.

    No deposit_events row, no swap touched. A stranded deposit is a fact about the ACCOUNT and
    the swap being refreshed has no claim on it -- letting it change that swap's outcome would
    be attributing by proximity, which is the mistake the mechanism exists to refuse.
    """
    deposit_service.record_what_nobody_can_claim(db, "SOL", Adapter(drops=[a_drop(amount=9.0)]))
    assert db.execute("SELECT COUNT(*) AS n FROM deposit_events").fetchone()["n"] == 0
    assert db.execute("SELECT COUNT(*) AS n FROM swaps").fetchone()["n"] == 0


def test_refresh_swap_from_chain_records_the_strandings(db):
    """DRIVEN THROUGH THE LIVE DECISION FUNCTION, and nothing in this suite did that before.

    Removing the `record_what_nobody_can_claim(...)` call from refresh_swap_from_chain SURVIVED
    the first mutation round: every test above calls that function directly, so the function was
    covered and its call site was not. Fifth time in this session a call-site mutation has
    survived for exactly that reason. Grepped while fixing it: no test in this tree drove
    refresh_swap_from_chain() at all, which is why the shape keeps recurring there.

    THE SWAP GETS NOTHING AND THE TABLE GETS THE ROW, which is the whole invariant: the scan
    returned no event this swap can claim, so the swap stays `awaiting_deposit` with no
    deposit_events row, while the money that did arrive is recorded for a person.

    MUTATION: delete the call and the table stays empty while the swap refreshes normally --
    the exact state this change found on the live path, with nothing failing.
    """
    db.execute(
        "INSERT INTO quotes (id, from_asset, to_asset, input_amount, quoted_rate, fee_bps,"
        " network_fee_reserve, output_amount_estimate, expires_at, created_at)"
        " VALUES ('q_u','SOL','GRC',1.0,56.38,150,0.01,55.0,"
        "'2999-01-01T00:00:00+00:00','2026-10-01T00:00:00+00:00')"
    )
    db.execute(
        "INSERT INTO swaps (id, quote_id, from_asset, to_asset, deposit_address, deposit_tag,"
        " payout_address, expected_input_amount, actual_input_amount, quoted_rate, fee_bps,"
        " network_fee_reserve, output_amount_estimate, status, min_confirmations, expires_at,"
        " created_at, updated_at)"
        " VALUES ('s_u','q_u','SOL','GRC',?,7,'GRCpayout',1.0,NULL,56.38,150,0.01,55.0,"
        "'awaiting_deposit',3,'2999-01-01T00:00:00+00:00','2026-10-01T00:00:00+00:00',"
        "'2026-10-01T00:00:00+00:00')",
        (ACCOUNT,),
    )
    swap = db.execute("SELECT * FROM swaps WHERE id = 's_u'").fetchone()

    # NO EVENTS THIS SWAP CAN CLAIM, one stranded credit -- the operator's devnet situation.
    adapter = Adapter(drops=[a_drop(amount=0.5)])
    deposit_service.refresh_swap_from_chain(
        db, {"AMOUNT_TOLERANCE_PCT": 1.0}, {"SOL": adapter}, swap)

    rows = rows_in(db)
    assert len(rows) == 1, "the stranded deposit is recorded by the live refresh"
    assert rows[0]["amount"] == 0.5
    assert rows[0]["txid"] == SIGNATURE

    assert db.execute("SELECT COUNT(*) AS n FROM deposit_events").fetchone()["n"] == 0, (
        "and NOTHING is credited to this swap -- it has no claim on that money"
    )
    after = db.execute("SELECT status FROM swaps WHERE id = 's_u'").fetchone()
    assert after["status"] == "awaiting_deposit", (
        "nor does the swap move: a stranded deposit is a fact about the shared ACCOUNT, and "
        "letting it advance whichever swap was being refreshed is attribution by proximity"
    )
