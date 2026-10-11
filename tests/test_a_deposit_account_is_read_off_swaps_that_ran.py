"""configure_env reads SOL's and XRP's shared deposit account out of the database.

Role: tests (configure_env.accounts_from_the_database -- what it reads, and what it
      must refuse to read)
Reads: a real swap_terminal.db created by db.init_db() under pytest's tmp_path
Writes: that database only. config.database_path() is redirected at SWAP_DB_PATH, so
        the operator's real database is never opened by any test here.
Can move funds: no
Mainnet-safe: yes -- no socket is opened, no adapter is built, and the one connection
        this exercises is opened mode=ro

=============================================================================
"i don't know any of that fucking information off hand" -- 2026-10-11
=============================================================================

They did not need to. The tool was asking for SOL_DEPOSIT_ACCOUNT and
XRP_DEPOSIT_ACCOUNT, and both values were already written down -- by this system, on
rows it wrote itself. On a tag-attributed chain every swap's `deposit_address` IS the
configured shared account; that is what makes the chain tag-attributed. So the question
was answerable from the database and was being put to the operator instead.

=============================================================================
THE TEST THAT MATTERS MOST IS THE ONE ABOUT THE CHAINS IT MUST NOT READ
=============================================================================

BTC, LTC and GRC derive a FRESH address per swap. A `deposit_address` read off one of
their rows is ONE CUSTOMER'S address, and writing it into .env as a deposit account
would point every future swap on that chain at a stranger -- deposits that are real,
confirmed, and unrecoverable, with no exchange to call. That is the single most
expensive mistake available in configure_env.py.

So SHARED_DEPOSIT_ACCOUNTS is an allowlist rather than a loop over every asset, and
test_a_per_swap_chain_is_never_read_from is the assertion that holds it. Its mutation is
one line: widen the table to include BTC and it fails. ICP is excluded for a different
reason and has its own test -- its subaccounts derive per swap from ICP_OWNER_PRINCIPAL
plus an index, so there is no shared-account variable for it to set.

WHAT EACH TEST WOULD CATCH. EVERY ONE OF THESE MUTATIONS WAS APPLIED AND THE NAMED TEST
FAILED -- which is the only reason this table may be written in the indicative (rule 17).
Eight were run first and the seventh row below PASSED under its mutation; see the wiring
section at the foot of this file for what that meant and what was added.

  the value is read            the row the operator was being asked to remember
  a per-swap chain is not      add "BTC" to SHARED_DEPOSIT_ACCOUNTS -> a customer's
                               address is offered as the desk's deposit account
  the newest wins              drop ORDER BY MAX(created_at) DESC -> a repointed
                               account resolves to whichever row sqlite returns first
  a repoint is visible         drop the `seen` clause -> two accounts silently
                               become one with no sign that older swaps disagree
  .env is not overwritten      drop the `already.get(variable)` guard -> a live
                               account is replaced by a historical one
  an empty address is skipped  drop `deposit_address != ''` -> '' is offered as the
                               account and compose passes it as an empty string, which
                               is the exact state the ATM page showed as NONE
  a missing database is said   let the OSError out -> a configuration tool crashes on
                               a machine whose database has not been created yet
  no swaps on a chain is said  print nothing -> rule 14's blank gap, ambiguous between
                               zero rows and a query that broke
  the handle refuses a write   drop `?mode=ro` -> a bug in a configuration tool can
                               write to the authority it is only supposed to read
  the wiring                   delete the accounts_from_the_database() line from
                               discover_all() -> a correct, fully tested function that
                               nothing calls. THIS IS THE ONE THAT WAS NOT COVERED when
                               the table was first written.
"""

from __future__ import annotations

import sqlite3
import zlib

import db
import pytest
import valid_addresses
from chains.icp_account import account_identifier, subaccount_from_index

import configure_env

#: THE DESK'S SHARED SOL ACCOUNT, and it is the operator's REAL devnet account rather
#: than a derived fixture -- see valid_addresses.SOL_DEPOSIT_ACCOUNT for why Solana is the
#: one chain whose fixture is measured instead of synthesized. It is also the literal value
#: accounts_from_the_database() is expected to find on their machine, which makes this test
#: and that run the same shape.
SOL_SHARED = valid_addresses.SOL_DEPOSIT_ACCOUNT

#: The account the desk MOVED OFF OF, for the repoint test. Derived with a phrase that says
#: what it is for, which is what valid_addresses exists to make possible: "a 34-character
#: string never says what the address is FOR."
SOL_FORMER = valid_addresses.solana_address_for("the SOL deposit account the desk moved off of")

#: XRP's shared account. On XRP the deposit instruction is a PAIR -- this account plus a
#: per-swap DestinationTag -- so the desk's own account IS the configured value, which is
#: exactly what XRP_HOT_ACCOUNT names.
XRP_SHARED = valid_addresses.XRP_HOT_ACCOUNT

#: A PER-SWAP ADDRESS ON EACH CHAIN THAT DERIVES ONE, which is to say: a CUSTOMER'S address.
#: These are what test_a_per_swap_chain_is_never_read_from seeds, and the point of giving
#: each chain its own correctly-shaped address rather than one shared placeholder is that a
#: reader asking "could this really be in that column?" gets yes for all four.
#:
#: ICP's comes from the REAL derivation, chains/icp_account, rather than an invented 64-hex
#: string -- a subaccount from an index is precisely how chains/icp.ICPAdapter builds a
#: per-swap deposit address, so the row seeded below is the row the application writes.
#: `2vxsx-fae` is the anonymous principal and is already this suite's ICP fixture.
PER_SWAP_ADDRESS = {
    "BTC": valid_addresses.BTC_REGTEST_SOMEBODY_ELSE,
    "LTC": valid_addresses.LTC_REGTEST_DEPOSIT,
    "GRC": valid_addresses.GRC_DESK_DEPOSIT,
    "ICP": account_identifier("2vxsx-fae", subaccount_from_index(7)),
}

#: The payout leg of every seeded swap. It is never read by anything under test -- the
#: column is NOT NULL, so a value is required, and a derived one costs nothing and keeps
#: tests/test_address_literals_are_valid.py's ceiling where it is. `bcrt1qpayout` stood here
#: for one run and tripped that gate at 61 of a ceiling of 60, which is the gate doing its
#: job: an invented address in a fixture is one copy-paste from a real payout.
PAYOUT_ADDRESS = valid_addresses.BTC_TAPROOT_PAYOUT


@pytest.fixture
def database(tmp_path, monkeypatch):
    """A real database at a throwaway path, with the real schema, and no .env in reach.

    db.init_db() RATHER THAN A HAND-WRITTEN CREATE TABLE. CLAUDE.md's verification rule
    is explicit that a test must run the real thing and not a paraphrase of its SQL: a
    copied schema drifts from db.py the first time a column is added, and the copy still
    passes while the real query against the real table does not.

    SWAP_DB_PATH IS THE REDIRECT, because it is the first branch of
    config.database_path() -- the same precedence every process in this tree shares. A
    redirect through .env would exercise a different branch than the one an operator
    hits, and would leave the other branches able to reach the real file.
    """
    path = tmp_path / "swap_terminal.db"
    db.init_db(str(path))
    monkeypatch.setenv("SWAP_DB_PATH", str(path))
    monkeypatch.setattr(configure_env, "ENV_FILE", tmp_path / ".env")
    # discover_icp() SHELLS OUT TO DOCKER, and two tests below run the real discover_all()
    # and the real main(). Stubbed here rather than in those two so this file's header
    # promise -- no socket, docker never invoked -- holds for every test in it, including
    # one added later that reaches further than its author expected.
    monkeypatch.setattr(configure_env, "discover_icp", lambda: {})
    return path


def seed_swap(path, *, from_asset, deposit_address, created_at, credited=False) -> str:
    """One swap row, through the real columns, with its quote. Returns its id.

    THE QUOTE ROW IS INSERTED TOO because swaps.quote_id carries a FOREIGN KEY to
    quotes(id) and SCHEMA sets `PRAGMA foreign_keys=ON`. A test that inserted an orphan
    swap would be seeding a row the application cannot produce, and the first thing a
    reader would ask of a surprising result is whether the fixture was realistic.

    THE ID IS DERIVED AND RETURNED rather than passed in, which is ruff's PLR0913
    answered rather than suppressed (rule 19): it was the sixth argument and the only
    one carrying no information -- the callers were passing "s1", "old" and "new". Asset,
    timestamp and address make it unique for any two rows a test here can seed, because
    two rows agreeing on all three would be the same row.

    THE DESTINATION TAG IS DERIVED TOO, and `credited` took its place as a parameter when
    the decision started turning on credited_at -- PLR0913 again, and again answered by
    removing the argument that carried the least. It is now set for every XRP swap and
    for no other chain, which is not merely smaller but more faithful: `deposit_tag` is
    "NULL for every chain that attributes a deposit BY ADDRESS, which is all of them
    except XRP" (db.py), so an opt-in tag let a test seed an XRP swap with no tag at all
    -- a row the application cannot produce. crc32 rather than hash() because hash() is
    salted per process and a fixture that differs between runs is not a fixture.
    """
    swap_id = f"{from_asset}-{created_at}-{deposit_address[:8]}"
    tag = zlib.crc32(swap_id.encode()) % 100_000 if from_asset == "XRP" else None
    with sqlite3.connect(path) as connection:
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute(
            "INSERT INTO quotes (id, from_asset, to_asset, input_amount, quoted_rate, fee_bps,"
            " network_fee_reserve, output_amount_estimate, expires_at, created_at)"
            " VALUES (?, ?, 'BTC', 1.0, 1.0, 150, 0.0, 1.0, ?, ?)",
            (f"quote-{swap_id}", from_asset, created_at, created_at),
        )
        connection.execute(
            "INSERT INTO swaps (id, quote_id, from_asset, to_asset, deposit_address, deposit_tag,"
            " payout_address, expected_input_amount, quoted_rate, fee_bps, network_fee_reserve,"
            " output_amount_estimate, status, min_confirmations, created_at, updated_at,"
            " expires_at, credited_at)"
            " VALUES (?, ?, ?, 'BTC', ?, ?, ?, 1.0, 1.0, 150, 0.0, 1.0, ?,"
            " 2, ?, ?, ?, ?)",
            (swap_id, f"quote-{swap_id}", from_asset, deposit_address, tag, PAYOUT_ADDRESS,
             "credited" if credited else "awaiting_deposit",
             created_at, created_at, created_at, created_at if credited else None),
        )
    return swap_id


def test_the_account_is_read_off_a_swap_that_already_ran(database, capsys):
    """The value the operator was asked to remember, taken from a row this system wrote.

    MUTATION: drop accounts_from_the_database() from discover_all() and the account is
    asked for again on a machine that already knows it.
    """
    seed_swap(database, from_asset="SOL",
              deposit_address=SOL_SHARED,
              created_at="2026-10-01T00:00:00Z")

    found = configure_env.accounts_from_the_database({})

    assert found["SOL_DEPOSIT_ACCOUNT"][0] == SOL_SHARED
    # And the reason travels with the value, because a discovered account is a claim
    # about where customer money will be sent and has to be checkable (rule 14).
    why = found["SOL_DEPOSIT_ACCOUNT"][1]
    assert "SOL" in why
    assert "swap_terminal.db" in why
    assert configure_env.SHARED_DEPOSIT_ACCOUNTS["SOL"] == "SOL_DEPOSIT_ACCOUNT"
    del capsys


def test_xrp_is_read_the_same_way_and_the_tag_is_not_part_of_the_account(database):
    """XRP's account is one value; the DestinationTag is per swap and is not read.

    The deposit INSTRUCTION for XRP is a pair -- account plus tag -- and only the
    account half is configuration. Reading the tag into .env would pin every future
    swap to one customer's tag, which is the same defect as a per-swap address one
    column over.
    """
    swap_id = seed_swap(database, from_asset="XRP", deposit_address=XRP_SHARED,
                        created_at="2026-10-01T00:00:00Z")

    found = configure_env.accounts_from_the_database({})

    assert found["XRP_DEPOSIT_ACCOUNT"][0] == XRP_SHARED
    # READ BACK FROM THE COLUMN rather than compared against a value this test passed in,
    # so the assertion is about what is actually stored on the row the tool read.
    with sqlite3.connect(database) as connection:
        stored_tag = connection.execute(
            "SELECT deposit_tag FROM swaps WHERE id = ?", (swap_id,)).fetchone()[0]
    assert stored_tag is not None, "an XRP swap was seeded with no destination tag"
    assert str(stored_tag) not in found["XRP_DEPOSIT_ACCOUNT"][1], (
        "the per-swap destination tag reached the reason line, which would pin every future "
        "swap to one customer's tag"
    )
    assert set(found) == {"XRP_DEPOSIT_ACCOUNT"}, "a chain with no swaps produced a value anyway"


@pytest.mark.parametrize("asset", ["BTC", "LTC", "GRC", "ICP"])
def test_a_per_swap_chain_is_never_read_from(database, asset, capsys):
    """THE ONE THAT MATTERS MOST. MUTATION: add the asset to SHARED_DEPOSIT_ACCOUNTS.

    Each of these chains gives every swap its own address -- derived, for BTC/LTC/GRC,
    and a derived subaccount for ICP. The row seeded below is therefore a CUSTOMER'S
    deposit address. If it were offered as the desk's shared account and written to
    .env, every later swap on that chain would print it to the next customer, and those
    deposits would land in the first customer's control: real, confirmed, final.

    Asserted on the RETURN VALUE rather than on the table's contents, because the table
    is the mechanism and the money follows the return value. A second mechanism added
    later -- a loop over assets, a per-chain override -- has to pass this too.
    """
    seed_swap(database, from_asset=asset,
              deposit_address=PER_SWAP_ADDRESS[asset],
              created_at="2026-10-01T00:00:00Z")

    found = configure_env.accounts_from_the_database({})

    assert found == {}, f"{asset}'s per-swap customer address was offered as a deposit account"
    for value, _why in found.values():
        assert PER_SWAP_ADDRESS[asset] not in value
    assert PER_SWAP_ADDRESS[asset] not in capsys.readouterr().out, (
        "a customer's deposit address was printed by a configuration tool"
    )


def test_the_newest_account_wins_when_it_is_the_one_money_arrived_at(database, capsys):
    """A repoint where recency and the evidence AGREE, which is the clean case.

    MUTATION: drop `ORDER BY newest DESC` and the answer becomes whichever row sqlite
    happens to return first, which is a coin flip between the desk's current account and
    one it moved off of.

    THE SECOND HALF IS THAT THE DISAGREEMENT IS SAID OUT LOUD. Taking the newest silently
    would resolve the ambiguity without reporting it, and an operator looking at one
    address would have no way to know the database holds two.

    THIS TEST USED TO SEED NEITHER SWAP AS CREDITED, so it ran through the "nothing
    corroborates any of these" branch while its name claimed the newest won on merit. It
    passed either way, which is exactly the kind of test that stops measuring what it is
    named for -- the branch changed under it and nothing said so.
    """
    seed_swap(database, from_asset="SOL", deposit_address=SOL_FORMER,
              created_at="2026-09-01T00:00:00Z", credited=True)
    seed_swap(database, from_asset="SOL", deposit_address=SOL_SHARED,
              created_at="2026-10-05T00:00:00Z", credited=True)

    found = configure_env.accounts_from_the_database({})

    assert found["SOL_DEPOSIT_ACCOUNT"][0] == SOL_SHARED
    why = found["SOL_DEPOSIT_ACCOUNT"][1]
    assert "2 DISTINCT" in why, f"the repoint was resolved silently: {why}"
    assert "repointed" in why
    assert "deposits have arrived there" in why, f"the corroboration was not stated: {why}"
    assert "RECENCY ALONE" not in why, "a corroborated account was labeled a guess"
    del capsys


def test_a_newest_account_nothing_arrived_at_is_refused_rather_than_written(database, capsys):
    """THE ONE THAT GOES BACK TO THE OPERATOR. MUTATION: drop the refusal branch.

    THIS IS THE CASE THE FIRST REAL RUN COULD HAVE BEEN IN. On 2026-10-11 the operator's
    XRP swaps carried TWO distinct deposit accounts and the tool took the newest on
    recency alone, named it, and moved on. A swap's deposit_address is written from the
    configured variable at creation time, so a swap created while that variable held a
    wrong value carries the wrong value -- and this configuration had been rewritten
    repeatedly that week. The newest row can be a value that was configured and never
    worked.

    `credited_at` is the only evidence available that an account RECEIVES: it means a
    deposit arrived there and this system saw it and attributed it. When the newest has
    none and an older one does, the rows cannot say which is live -- so nothing is
    offered, both addresses and their evidence are printed, and the variable falls
    through to the question. A deposit address is where customer money goes (rule 16).
    """
    seed_swap(database, from_asset="SOL", deposit_address=SOL_FORMER,
              created_at="2026-09-01T00:00:00Z", credited=True)
    seed_swap(database, from_asset="SOL", deposit_address=SOL_SHARED,
              created_at="2026-10-05T00:00:00Z", credited=False)

    found = configure_env.accounts_from_the_database({})
    printed = capsys.readouterr().out

    assert "SOL_DEPOSIT_ACCOUNT" not in found, (
        "an account that has never received a credited deposit was offered as the shared "
        "account while an older, proven one existed"
    )
    # BOTH addresses and the evidence for each, because the operator is the one deciding
    # and a refusal with no detail is just a different way of being unhelpful (rule 14).
    assert SOL_SHARED in printed and SOL_FORMER in printed
    assert "0 credited" in printed
    assert "yours to say" in printed


def test_when_nothing_was_ever_credited_the_newest_is_offered_AS_A_GUESS(database, capsys):
    """MUTATION: drop the `guess` suffix.

    No account on record has ever received a credited deposit, so the chain has never
    completed a deposit under any of them. The newest is still the best available answer
    -- refusing here would block a chain that has simply never run yet -- but it is a
    hypothesis, and rule 17's whole subject is not presenting one in the register of a
    measurement.
    """
    seed_swap(database, from_asset="SOL", deposit_address=SOL_FORMER,
              created_at="2026-09-01T00:00:00Z")
    seed_swap(database, from_asset="SOL", deposit_address=SOL_SHARED,
              created_at="2026-10-05T00:00:00Z")

    found = configure_env.accounts_from_the_database({})

    assert found["SOL_DEPOSIT_ACCOUNT"][0] == SOL_SHARED
    why = found["SOL_DEPOSIT_ACCOUNT"][1]
    assert "RECENCY ALONE" in why, f"an uncorroborated pick was reported as a finding: {why}"
    assert "guess rather than a finding" in why
    assert "NOTHING has ever been credited" in why


def test_a_single_account_says_whether_money_has_ever_arrived_at_it(database, capsys):
    """"The only account on record" and "the account that receives" are different claims.

    MUTATION: collapse the two branches into one sentence. One account with credited
    deposits is a measurement; one account with none is the only candidate and nothing
    more, and an operator about to publish it as a deposit address should be told which
    they are holding.
    """
    seed_swap(database, from_asset="XRP", deposit_address=XRP_SHARED,
              created_at="2026-10-01T00:00:00Z", credited=True)
    credited_why = configure_env.accounts_from_the_database({})["XRP_DEPOSIT_ACCOUNT"][1]
    assert "deposits have arrived there" in credited_why
    assert "NOTHING has ever been credited" not in credited_why

    seed_swap(database, from_asset="SOL", deposit_address=SOL_SHARED,
              created_at="2026-10-01T00:00:00Z")
    bare_why = configure_env.accounts_from_the_database({})["SOL_DEPOSIT_ACCOUNT"][1]
    assert "NOTHING has ever been credited" in bare_why
    assert "0 credited deposits" in bare_why
    del capsys


def test_the_decision_function_survives_an_empty_row_list():
    """The caller guards it; a second caller would not. Tested directly, not through main.

    A crash inside a configuration tool, on a machine whose stack is already broken, is
    the one failure mode this file exists against -- so the branch is here even though
    accounts_from_the_database() cannot reach it, and it is tested where it can be.
    """
    address, why = configure_env.choose_shared_account("SOL", [])

    assert address == ""
    assert why.strip(), "an empty list produced an empty explanation"


def test_a_value_already_in_env_is_not_read_over(database):
    """MUTATION: drop the `already.get(variable)` guard.

    This is the one source in discover_all() that has to consult `already` itself rather
    than let main() filter afterwards. Every other discovered value is position
    independent -- a probed port is the port. A deposit account is not: .env may carry
    the account the desk moved TO, while the newest row in the database is from before
    the move. Reporting the older address as "discovered" would offer to overwrite the
    live account with a historical one, and the operator would be reading a reason line
    that says it came from their own swaps.
    """
    seed_swap(database, from_asset="SOL", deposit_address=SOL_FORMER,
              created_at="2026-09-01T00:00:00Z")

    found = configure_env.accounts_from_the_database({"SOL_DEPOSIT_ACCOUNT": SOL_SHARED})

    assert "SOL_DEPOSIT_ACCOUNT" not in found, "a live account was offered a historical replacement"


def test_an_empty_deposit_address_is_not_offered_as_an_account(database, capsys):
    """MUTATION: drop `deposit_address != ''` from the WHERE clause.

    `deposit_address` is NOT NULL, which permits the empty string and does not permit
    NULL -- so `!= ''` is the clause that does the work and `IS NOT NULL` is there for a
    database written before the constraint. An empty account written into .env is
    precisely the state that produced six NONE lamps: docker-compose.web.yml passes
    `${SOL_DEPOSIT_ACCOUNT:-}`, an empty string reaches the container, and
    build_adapters() skips the chain. It would have been reported as a discovered value.
    """
    seed_swap(database, from_asset="SOL", deposit_address="",
              created_at="2026-10-01T00:00:00Z")

    found = configure_env.accounts_from_the_database({})
    printed = capsys.readouterr().out

    assert found == {}, "an empty deposit address was offered as the shared account"
    # And the reason it found nothing is stated, not left as a blank gap (rule 14). An
    # empty row and no row at all reach the same `if not rows` branch and print the same
    # line, which is correct: in both cases no swap on that chain carries an address.
    assert "no swap in" in printed, f"the empty result printed no reason: {printed!r}"


def test_a_chain_with_no_swaps_says_so(database, capsys):
    """Rule 14: a blank gap is ambiguous between zero rows and a query that broke."""
    found = configure_env.accounts_from_the_database({})
    printed = capsys.readouterr().out

    assert found == {}
    assert "SOL" in printed and "XRP" in printed
    assert "nothing to read" in printed, f"the empty result printed no reason: {printed!r}"


def test_a_missing_database_is_reported_and_not_raised(tmp_path, monkeypatch, capsys):
    """A configuration tool must not crash on a machine whose database is not created yet.

    MUTATION: remove the `is_file()` branch and let sqlite's own error out. The operator
    reaching for this tool is one whose stack is not working; a traceback from it is a
    second problem on top of the first.
    """
    monkeypatch.setenv("SWAP_DB_PATH", str(tmp_path / "not-created-yet.db"))
    monkeypatch.setattr(configure_env, "ENV_FILE", tmp_path / ".env")

    found = configure_env.accounts_from_the_database({})
    printed = capsys.readouterr().out

    assert found == {}
    assert "no database at" in printed
    assert "not-created-yet.db" in printed, "the path it looked at was not named"


def test_a_database_without_a_swaps_table_is_reported_per_chain(tmp_path, monkeypatch, capsys):
    """An sqlite file that is not this schema. Named error, reported, not raised.

    This is the shape of a mistyped SWAP_DB_PATH that happened to name some other
    sqlite file -- which is not hypothetical in this tree: a mistyped path is what
    created a second database on 2026-10-01.
    """
    path = tmp_path / "something-else.db"
    with sqlite3.connect(path) as connection:
        connection.execute("CREATE TABLE unrelated (x INTEGER)")
    monkeypatch.setenv("SWAP_DB_PATH", str(path))
    monkeypatch.setattr(configure_env, "ENV_FILE", tmp_path / ".env")

    found = configure_env.accounts_from_the_database({})
    printed = capsys.readouterr().out

    assert found == {}
    assert "could not be queried" in printed
    assert "SOL" in printed and "XRP" in printed, "only one chain reported the failure"


def test_the_connection_it_opens_refuses_a_write(database, monkeypatch):
    """MUTATION: drop `?mode=ro` from the URI and this test fails.

    THE ASSERTION IS AN OUTCOME, NOT A READING OF THE SOURCE. CLAUDE.md is explicit that
    "the code contains a check for X" is not evidence X is enforced -- only "condition X
    produced outcome Y" counts. So the real connection the function opened is captured
    as it is handed out, and after the function returns, a WRITE is attempted through
    that same handle. sqlite refusing it is the outcome; the URI is merely how it was
    arranged.

    WHY IT IS WORTH A TEST AT ALL: this tool runs on a machine whose stack is already
    broken, against the one file that is the authority for every pending payout. A
    read-only handle means no defect introduced here later -- a stray execute, a
    copy-paste of a write -- can reach it. The guard is cheap and the thing it guards is
    not replaceable.
    """
    swap_id = seed_swap(database, from_asset="SOL", deposit_address=SOL_SHARED,
                        created_at="2026-10-01T00:00:00Z")
    opened = []
    real_connect = sqlite3.connect

    def capturing_connect(target, *args, **kwargs):
        # THE TARGET ONLY, not the connection object. The write below is attempted on a
        # REOPEN of the same URI rather than on the handle the function used, because
        # that handle is closed by the time this returns -- and a test that kept it open
        # would be asserting on a connection the real code never has.
        opened.append(target)
        return real_connect(target, *args, **kwargs)

    monkeypatch.setattr(configure_env.sqlite3, "connect", capturing_connect)

    found = configure_env.accounts_from_the_database({})

    assert found["SOL_DEPOSIT_ACCOUNT"][0] == SOL_SHARED, (
        "the capture changed the result, so this test is measuring the wrong connection"
    )
    assert len(opened) == 1, f"expected one connection, got {len(opened)}"
    target = opened[0]
    assert "mode=ro" in str(target)
    # THE OUTCOME. The function closed the handle, so it is reopened exactly as the
    # function opened it -- same URI, same flags -- and the write is attempted there.
    with real_connect(str(target), uri=True) as reopened, pytest.raises(sqlite3.OperationalError):
        reopened.execute("UPDATE swaps SET deposit_address = 'OVERWRITTEN' WHERE id = ?", (swap_id,))

    # And the row is untouched, which is the assertion that would survive even if
    # sqlite ever stopped raising on the write.
    with real_connect(database) as after:
        row = after.execute("SELECT deposit_address FROM swaps WHERE id = ?", (swap_id,)).fetchone()
    assert row[0] == SOL_SHARED


# ===========================================================================
# THE WIRING, which six mutation checks did not cover and one now does
# ===========================================================================
#
# Every test above measures accounts_from_the_database() directly, and all six of their
# mutations failed as intended. A SEVENTH mutation -- deleting the
# `found.update(accounts_from_the_database(already))` line from discover_all() -- left
# the whole file green. The function was correct, fully tested, and called by nothing,
# which is the exact defect this tool was written to fix (a variable nothing sets and
# nothing mentions) reproduced in the fix for it. It is also rule 17's shape: this
# file's own docstring claimed that mutation would be caught, and the claim had not
# been run.
#
# So the two below go through the real discover_all() and the real main(). They assert
# on the OUTPUT an operator reads, not on the call.


def test_discovery_includes_the_account_so_it_is_not_asked_for(database, capsys):
    """MUTATION: delete the accounts_from_the_database() line from discover_all().

    discover_all() is where the tool decides what it knows. An account read from the
    database and not merged there would never reach the dry-run report, the "still
    unset" computation or the write -- three places that all read `discovered`.
    """
    seed_swap(database, from_asset="SOL", deposit_address=SOL_SHARED,
              created_at="2026-10-01T00:00:00Z")

    found = configure_env.discover_all({})
    printed = capsys.readouterr().out

    assert found["SOL_DEPOSIT_ACCOUNT"][0] == SOL_SHARED
    assert SOL_SHARED in printed, "the discovered account was not reported"


def test_a_dry_run_does_not_ask_for_an_account_the_database_knows(database, monkeypatch, capsys):
    """END TO END THROUGH main(). The operator's question, answered without them.

    "i don't know any of that fucking information off hand" is about this exact block of
    output: SOL_DEPOSIT_ACCOUNT appearing under WOULD ASK FOR. It must not be there when
    a SOL swap in the database already carries the account.

    ASSERTED ON THE BLOCK AND NOT ON THE WHOLE SCREEN, because the name legitimately
    appears elsewhere -- in the DISCOVERING table, where it belongs. A test for its
    absence anywhere would pass only by making the output worse.
    """
    seed_swap(database, from_asset="SOL", deposit_address=SOL_SHARED,
              created_at="2026-10-01T00:00:00Z")
    monkeypatch.setattr("sys.argv", ["configure_env.py"])

    assert configure_env.main() == 0

    printed = capsys.readouterr().out
    asked_block = printed.split("WOULD ASK FOR", 1)[1].split("WOULD WRITE", 1)[0]
    assert "SOL_DEPOSIT_ACCOUNT" not in asked_block, (
        f"the tool asked for an account its own database already holds:\n{asked_block}"
    )
    # XRP has no swap seeded, so it IS still a question -- which is what makes the
    # assertion above about discovery rather than about the name having been dropped
    # from ASKED altogether.
    assert "XRP_DEPOSIT_ACCOUNT" in asked_block
    assert "SOL_DEPOSIT_ACCOUNT" in printed.split("WOULD ASK FOR", 1)[0], (
        "the account was neither asked for nor reported as discovered, which is the "
        "silently-absent variable this tool exists to prevent"
    )
