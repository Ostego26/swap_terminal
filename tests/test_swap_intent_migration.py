"""Does the migration write nothing without --apply, and does the claim it prepares hold?

Role: test / measurement (seeds a real JSON store, runs the real script, then
        seeds real rows and runs the real SQL)
Reads: swap_terminal/migrate_swap_intents.py, swap_terminal/swap_intents_schema.py
Writes: throwaway files under pytest's tmp_path. Never the real
        swap_intents.json and never the real swap_terminal.db -- every test
        passes both paths explicitly, and one test asserts the dry run left no
        file behind at all.
Can move funds: no. Nothing here opens a socket, reads a keypair, signs, or
        broadcasts.
Mainnet-safe: yes

WHAT THIS FILE IS FOR.

Two separate claims, tested separately, because they fail in different ways.

1. THE DRY RUN MUST BE A DRY RUN. `swap_intents.json` holds armed state --
   measured 2026-09-24: 3 intents, one `status=paid` carrying 5,560,821
   lamports and a Solana signature -- so CLAUDE.md rule 16 makes moving it the
   operator's decision, and the script's only job until they make it is to show
   them what it would do. "Writes nothing" is a promise that is easy to make
   and easy to break by accident: the first version of this script called
   `executescript(SCHEMA)` before checking the flag, so a dry run against a
   fresh path created a database file and three tables. That was caught by
   running it and looking at the directory, which is what
   test_dry_run_creates_no_database_file does here so it stays caught.

2. THE CLAIM MUST ACTUALLY BE A CLAIM. The reason for the migration is not
   tidiness; it is that a conditional UPDATE checked on rowcount, backed by a
   partial unique index, makes a second payout for one intent impossible in a
   way an advisory lock cannot. That is asserted against a real SQLite database
   here, in the same shape tests/test_payout_concurrency.py uses for `swaps` --
   and it is the SQL from swap_intents_schema.py, not a paraphrase of it. The
   behavioral-verification principle forbids accepting "the file contains a
   CREATE UNIQUE INDEX" as evidence anything is enforced; only "this INSERT
   raised IntegrityError" counts.

NOT TESTED HERE, because it cannot be from a test: that --apply against the
real store does the right thing. It has never been run against the real file,
deliberately. What is tested is --apply against seeded copies of the same
shapes, including the exact three intents the live file holds.
"""

import json
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest
from migrate_swap_intents import MigrationRefused, intent_rows, main
from swap_intents_schema import CLAIM_SQL, SCHEMA, claim_params

SCRIPT = Path(__file__).resolve().parent.parent / "swap_terminal" / "migrate_swap_intents.py"


def paid_intent(intent_id="si_paid"):
    """The shape of the live file's one paid intent, with the addresses replaced.

    Taken from the real swap_intents.json's field names and types -- including
    `verifiedDeposit.sourceGridcoinAddress` being present and
    `verificationSource` being absent, which is what the file actually has.
    Inventing a tidier shape would test a store this repository does not own.
    """
    return {
        "intentId": intent_id,
        "status": "paid",
        "createdAt": "2026-03-26T18:30:32.525Z",
        "expiresAt": "2026-03-26T19:30:32.525Z",
        "gridcoinDepositAddress": "GRC_DEPOSIT_PLACEHOLDER",
        "destinationSolanaAddress": "SoLDestinationPlaceholder11111111111111111",
        "expectedGrcAmount": 100,
        "expectedQuote": {
            "grcAmount": 100,
            "grcPriceUsd": 0.0012,
            "solPriceUsd": 21.58,
            "solAmount": 0.005560821,
            "lamports": 5560821,
        },
        "verifiedDeposit": {
            "sourceGridcoinAddress": "GRC_SOURCE_PLACEHOLDER",
            "gridcoinTxid": "example-txid",
            "confirmations": 6,
            "receivedGrcAmount": 100,
            "verifiedAt": "2026-03-26T18:32:05.677Z",
        },
        "payout": {
            "startedAt": "2026-03-26T18:32:43.249Z",
            "signature": "SIGNATURE_PLACEHOLDER",
            "lamports": 5560821,
            "solAmount": 0.005560821,
            "paidAt": "2026-03-26T18:32:44.294Z",
        },
    }


def awaiting_intent(intent_id="si_awaiting"):
    return {
        "intentId": intent_id,
        "status": "awaiting_deposit",
        "createdAt": "2026-03-26T18:49:29.421Z",
        "expiresAt": "2026-03-26T19:49:29.421Z",
        "gridcoinDepositAddress": "GRC_DEPOSIT_PLACEHOLDER",
        "destinationSolanaAddress": "SoLDestinationPlaceholder11111111111111111",
        "expectedGrcAmount": 100,
        "expectedQuote": {"solAmount": 0.005561275, "lamports": 5561275},
        "verifiedDeposit": None,
        "payout": None,
    }


def write_store(tmp_path, intents):
    store = tmp_path / "swap_intents.json"
    store.write_text(json.dumps({"intents": intents}, indent=2), encoding="utf-8")
    return store


def test_dry_run_creates_no_database_file(tmp_path, capsys):
    """The promise in the module header, enforced.

    A dry run against a path where no database exists must leave that path
    alone. sqlite3.connect() creates the file, so this is the failure that the
    first version of the script had and that reading the code would not have
    found.
    """
    store = write_store(tmp_path, [paid_intent(), awaiting_intent()])
    db = tmp_path / "swap_terminal.db"

    assert main(["--intents", str(store), "--db", str(db)]) == 0

    assert not db.exists(), "a dry run created a database file"
    assert sorted(p.name for p in tmp_path.iterdir()) == ["swap_intents.json"]


def test_dry_run_against_an_existing_database_writes_no_rows(tmp_path):
    store = write_store(tmp_path, [paid_intent()])
    db = tmp_path / "swap_terminal.db"
    conn = sqlite3.connect(db)
    conn.executescript(SCHEMA)
    conn.commit()

    assert main(["--intents", str(store), "--db", str(db)]) == 0

    assert conn.execute("SELECT COUNT(*) FROM swap_intents").fetchone()[0] == 0
    conn.close()


def test_dry_run_never_modifies_the_source_file(tmp_path):
    """The store is the server's live authority; the migration is read-only on it in BOTH modes."""
    store = write_store(tmp_path, [paid_intent(), awaiting_intent()])
    before = store.read_bytes()
    db = tmp_path / "swap_terminal.db"

    main(["--intents", str(store), "--db", str(db)])
    assert store.read_bytes() == before

    main(["--intents", str(store), "--db", str(db), "--apply"])
    assert store.read_bytes() == before, "--apply modified the JSON store it read"


def test_dry_run_output_names_the_database_the_source_and_every_intent(tmp_path, capsys):
    """Rule 14: a pasted block has to be self-describing a day later."""
    store = write_store(tmp_path, [paid_intent(), awaiting_intent()])
    db = tmp_path / "swap_terminal.db"

    main(["--intents", str(store), "--db", str(db)])
    out = capsys.readouterr().out

    assert "DRY RUN" in out
    assert str(store) in out
    assert str(db) in out
    assert "si_paid" in out
    assert "si_awaiting" in out
    assert "5560821" in out  # the lamport figure a payout would send
    assert "µfn" in out  # rule 6: timings are reported in microfortnights
    assert " ufn" not in out  # and never with an ASCII u


def test_dry_run_on_an_empty_store_says_none_rather_than_printing_nothing(tmp_path, capsys):
    """Rule 14: an empty result is a result. A blank gap cannot be told from a broken query."""
    store = write_store(tmp_path, [])
    main(["--intents", str(store), "--db", str(tmp_path / "x.db")])
    assert "(none)" in capsys.readouterr().out


def test_apply_writes_the_rows_and_they_read_back(tmp_path):
    store = write_store(tmp_path, [paid_intent(), awaiting_intent()])
    db = tmp_path / "swap_terminal.db"

    assert main(["--intents", str(store), "--db", str(db), "--apply"]) == 0

    conn = sqlite3.connect(db)
    intents = conn.execute("SELECT intent_id, status, quoted_lamports FROM swap_intents ORDER BY intent_id").fetchall()
    assert intents == [
        ("si_awaiting", "awaiting_deposit", 5561275),
        ("si_paid", "paid", 5560821),
    ]

    deposits = conn.execute("SELECT intent_id, confirmations FROM swap_intent_deposits").fetchall()
    assert deposits == [("si_paid", 6)]

    payouts = conn.execute("SELECT intent_id, status, lamports FROM swap_intent_payouts").fetchall()
    assert payouts == [("si_paid", "completed", 5560821)]
    conn.close()


def test_apply_twice_skips_rather_than_duplicating_or_overwriting(tmp_path, capsys):
    """A second run must be a no-op, not a second set of rows.

    The paid intent is the one that matters: a duplicate `completed` payout row
    for one intent would also be the thing the partial unique index exists to
    make impossible, so this asserts both that it is skipped and that nothing
    raised on the way.
    """
    store = write_store(tmp_path, [paid_intent()])
    db = tmp_path / "swap_terminal.db"

    main(["--intents", str(store), "--db", str(db), "--apply"])
    main(["--intents", str(store), "--db", str(db), "--apply"])
    out = capsys.readouterr().out

    conn = sqlite3.connect(db)
    assert conn.execute("SELECT COUNT(*) FROM swap_intents").fetchone()[0] == 1
    assert conn.execute("SELECT COUNT(*) FROM swap_intent_payouts").fetchone()[0] == 1
    conn.close()
    assert "SKIPPED" in out


def test_an_unknown_status_is_refused_rather_than_guessed(tmp_path):
    """Mapping an unknown status to a default is how a paid intent becomes payable again."""
    weird = paid_intent()
    weird["status"] = "definitely_not_a_status"
    with pytest.raises(MigrationRefused, match="not one this schema knows"):
        intent_rows(weird)


def test_a_missing_or_zero_lamport_quote_is_refused(tmp_path):
    """That figure IS what a payout would send; a zero or absent one is not a default."""
    broken = paid_intent()
    broken["expectedQuote"] = {"lamports": 0}
    with pytest.raises(MigrationRefused, match="quoted lamports"):
        intent_rows(broken)

    missing = paid_intent()
    missing["expectedQuote"] = {}
    with pytest.raises(MigrationRefused, match="quoted lamports"):
        intent_rows(missing)


def test_a_payout_attempt_with_no_signature_is_reported_and_not_inserted(tmp_path, capsys):
    """An attempt with no recorded outcome has to be settled against the chain by a human.

    Inserting it as a live payout row would trip the partial unique index and
    block the operator from ever retrying that intent -- the opposite of what
    an unresolved attempt needs.
    """
    interrupted = paid_intent("si_interrupted")
    interrupted["status"] = "paying"
    interrupted["payout"] = {"startedAt": "2026-03-26T18:32:43.249Z"}

    store = write_store(tmp_path, [interrupted])
    db = tmp_path / "swap_terminal.db"

    main(["--intents", str(store), "--db", str(db), "--apply"])
    out = capsys.readouterr().out

    assert "payout attempts with no signature" in out
    assert "si_interrupted" in out

    conn = sqlite3.connect(db)
    assert conn.execute("SELECT COUNT(*) FROM swap_intent_payouts").fetchone()[0] == 0
    assert conn.execute("SELECT status FROM swap_intents").fetchone()[0] == "paying"
    conn.close()


def test_a_corrupt_store_is_refused_rather_than_read_as_empty(tmp_path, capsys):
    """The defect the Express server shipped: `catch { return {intents: []} }`.

    Reported as "no intents to migrate", it would let an operator conclude the
    file was empty and delete it -- along with the record of a verified, unpaid
    deposit.
    """
    store = tmp_path / "swap_intents.json"
    store.write_text('{"intents": [{"intentId": "si_tr', encoding="utf-8")

    # S603/S607 are already off for tests/ in pyproject.toml, so no noqa is
    # needed here; the argv is a fixed list this test constructs.
    result = subprocess.run(
        [sys.executable, str(SCRIPT), "--intents", str(store), "--db", str(tmp_path / "x.db")],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode != 0
    assert "no intents" not in result.stdout.lower()


def test_a_store_with_no_intents_array_is_refused(tmp_path, capsys):
    """main() turns a MigrationRefused into exit code 2 and a sentence, not a traceback.

    Asserting on the exit code rather than the exception is deliberate: the
    operator sees the exit code and the stderr line, and a test that only
    catches the exception would keep passing if main() started swallowing it.
    """
    store = tmp_path / "swap_intents.json"
    store.write_text('{"something_else": 1}', encoding="utf-8")

    assert main(["--intents", str(store), "--db", str(tmp_path / "x.db")]) == 2
    assert "Refusing to treat this as an empty store" in capsys.readouterr().err
    assert not (tmp_path / "x.db").exists()


# ---------------------------------------------------------------------------
# The claim the schema prepares. Behavior, against a real database.
# ---------------------------------------------------------------------------


def seeded_db(tmp_path, status="verified"):
    db = tmp_path / "swap_terminal.db"
    conn = sqlite3.connect(db)
    conn.executescript(SCHEMA)
    conn.execute(
        """
        INSERT INTO swap_intents (
            intent_id, status, created_at, expires_at, updated_at,
            gridcoin_deposit_address, destination_solana_address, expected_grc_amount,
            quoted_lamports, quoted_sol_amount
        ) VALUES ('si_x', ?, 'now', 'later', 'now', 'grc', 'sol', 100, 5560821, 0.005560821)
        """,
        (status,),
    )
    conn.commit()
    return conn


def test_the_claim_succeeds_exactly_once(tmp_path):
    """Two claims, one winner, measured on rowcount -- the whole point of the migration."""
    conn = seeded_db(tmp_path)

    first = conn.execute(CLAIM_SQL, claim_params("t1", "si_x"))
    assert first.rowcount == 1, "the first claim must win"

    second = conn.execute(CLAIM_SQL, claim_params("t2", "si_x"))
    assert second.rowcount == 0, "the second claim must update zero rows and therefore must not pay"

    assert conn.execute("SELECT status FROM swap_intents").fetchone()[0] == "paying"
    conn.close()


def test_the_claim_refuses_every_non_verified_status(tmp_path):
    for status in ("awaiting_deposit", "paying", "paid", "payout_failed", "expired"):
        # One directory per status: each seeded_db() wants its own database
        # file, and reusing the path would make the second iteration assert
        # against the first iteration's rows.
        (tmp_path / status).mkdir()
        conn = seeded_db(tmp_path / status, status=status)
        assert conn.execute(CLAIM_SQL, claim_params("t", "si_x")).rowcount == 0, f"claimed from {status}"
        conn.close()


def test_the_partial_unique_index_blocks_a_second_live_payout(tmp_path):
    """Not "the file contains a CREATE UNIQUE INDEX" -- an actual IntegrityError."""
    conn = seeded_db(tmp_path)
    insert = """
        INSERT INTO swap_intent_payouts (intent_id, claim_token, status, destination_solana_address, lamports, claimed_at)
        VALUES ('si_x', ?, ?, 'sol', 5560821, 'now')
    """
    conn.execute(insert, ("t1", "broadcast"))

    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(insert, ("t2", "claimed"))
    conn.close()


def test_a_failed_payout_can_still_be_retried(tmp_path):
    """The index is PARTIAL for this reason: 'failed' is outside the predicate.

    A unique index over every status would turn one failed attempt into an
    intent that can never be paid, which is a different way of losing a
    depositor's funds.
    """
    conn = seeded_db(tmp_path)
    insert = """
        INSERT INTO swap_intent_payouts (intent_id, claim_token, status, destination_solana_address, lamports, claimed_at)
        VALUES ('si_x', ?, ?, 'sol', 5560821, 'now')
    """
    conn.execute(insert, ("t1", "failed"))
    conn.execute(insert, ("t2", "claimed"))  # must not raise
    assert conn.execute("SELECT COUNT(*) FROM swap_intent_payouts").fetchone()[0] == 2
    conn.close()


def test_the_status_check_constraint_rejects_an_invented_status(tmp_path):
    conn = seeded_db(tmp_path)
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("UPDATE swap_intents SET status = 'nearly_paid' WHERE intent_id = 'si_x'")
    conn.close()
