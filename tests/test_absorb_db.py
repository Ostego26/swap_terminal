"""absorb_db.py writes to the authority, so every refusal is exercised, not described.

Role: test (two throwaway databases per case under tmp_path; no socket, no chain)
Reads: absorb_db.py, swap_terminal/db.py's SCHEMA
Writes: throwaway databases under pytest's tmp_path
Can move funds: no
Mainnet-safe: yes

WHY THIS FILE EXISTS. absorb_db.py inserts rows into swap_terminal.db -- the one
database CLAUDE.md rule 15 calls the authority -- and it arrived with no tests at
all. Its refusals are the whole product: a merge that overwrites a live swap with
an unrelated one of the same id, or that commits half a swap's rows, is worse than
no merge tool.

AND ITS FIRST REAL RUN WOULD HAVE ABORTED. The operator pre-checked before
--apply on 2026-10-10 and found that the orphan's discriminator allocation
collided with a live swap's:

    (CUBnQ5QBfYkL71TCqSdecAQ9xjfGmAdu6Hs3fjQeLorp, 1) -> CLASHES with s_aaa81fa6e8538163

The whole merge is one transaction so nothing would have been damaged, but the
operator would have received a traceback instead of a sentence. The clash case
below is that measurement, reproduced.
"""

from __future__ import annotations

import sqlite3
import subprocess
import sys

import pytest
import valid_addresses
from db import SCHEMA, apply_migrations, connect_db
from source_tree import REPOSITORY_ROOT

ACCOUNT = valid_addresses.SOL_DEPOSIT_ACCOUNT
TOOL = REPOSITORY_ROOT / "absorb_db.py"


def build(path, swap_id: str, created: str, status: str, tag: int) -> None:
    """One complete swap and every dependent row, through the REAL schema."""
    conn = connect_db(str(path), create=True)
    conn.executescript(SCHEMA)
    apply_migrations(conn)
    conn.execute(
        "INSERT INTO quotes (id,from_asset,to_asset,input_amount,quoted_rate,fee_bps,"
        "network_fee_reserve,output_amount_estimate,expires_at,created_at) VALUES "
        "(?,'SOL','GRC',0.05,8700.0,150,0.001,430.0,'2999-01-01T00:00:00+00:00',?)",
        (f"q_{swap_id}", created),
    )
    conn.execute(
        "INSERT INTO swaps (id,quote_id,from_asset,to_asset,deposit_address,deposit_tag,"
        "payout_address,expected_input_amount,quoted_rate,fee_bps,network_fee_reserve,"
        "output_amount_estimate,status,min_confirmations,expires_at,created_at,updated_at)"
        " VALUES (?,?,'SOL','GRC',?,?,?,0.05,8700.0,150,0.001,430.0,?,3,"
        "'2999-01-01T00:00:00+00:00',?,?)",
        (swap_id, f"q_{swap_id}", ACCOUNT, tag, valid_addresses.GRC_PAYOUT, status, created, created),
    )
    conn.execute(
        "INSERT INTO xrp_destination_tags (account,destination_tag,swap_id,allocated_at)"
        " VALUES (?,?,?,?)", (ACCOUNT, tag, swap_id, created),
    )
    conn.execute(
        "INSERT INTO deposit_events (swap_id,asset,txid,vout,address,amount,confirmations,"
        "first_seen_at,last_seen_at) VALUES (?,'SOL',?,?,?,0.05,3,?,?)",
        (swap_id, f"sig_{swap_id}", tag, ACCOUNT, created, created),
    )
    conn.commit()
    conn.close()


def run(source, destination, *extra) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(TOOL), "--source", str(source), "--destination", str(destination), *extra],
        capture_output=True, text=True, check=False, cwd=REPOSITORY_ROOT,
    )


@pytest.fixture
def pair(tmp_path):
    """An orphan and an authority, colliding on discriminator 1 exactly as measured."""
    orphan, live = tmp_path / "orphan.db", tmp_path / "live.db"
    build(orphan, "s_b8daedd5ef8101fe", "2026-10-01T22:34:14+00:00", "failed", 1)
    build(live, "s_aaa81fa6e8538163", "2026-10-05T12:00:00+00:00", "completed", 1)
    return orphan, live


def swaps_in(path) -> list[str]:
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    rows = [row[0] for row in conn.execute("SELECT id FROM swaps ORDER BY created_at")]
    conn.close()
    return rows


# --- the dry run writes nothing -----------------------------------------------

def test_a_dry_run_changes_neither_database(pair):
    """MUTATION: drop the `if not args.apply` return and this fails on both sides."""
    orphan, live = pair
    before_live, before_orphan = swaps_in(live), swaps_in(orphan)
    result = run(orphan, live)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "DRY RUN" in result.stdout
    assert swaps_in(live) == before_live, "the dry run wrote to the destination"
    assert swaps_in(orphan) == before_orphan, "the dry run wrote to the source"


def test_the_dry_run_prints_exactly_what_apply_will_do(pair):
    """The skip is computed in the PLAN, so the two cannot disagree.

    A tool whose dry run and real run compute their own answers separately is a tool
    whose dry run proves nothing -- which is the only reason to have one.
    """
    orphan, live = pair
    dry = run(orphan, live).stdout
    applied = run(orphan, live, "--apply").stdout
    for line in ("swaps        1", "deposit_events               1", "SKIPPED      1"):
        assert line in dry, f"{line!r} missing from the dry run"
        assert line in applied, f"{line!r} missing from the apply"


# --- THE MEASURED CLASH -------------------------------------------------------

def test_a_dependent_row_already_spoken_for_is_SKIPPED_and_the_rest_still_move(pair):
    """THE OPERATOR'S OWN 2026-10-10 CLASH, reproduced.

    xrp_destination_tags is PRIMARY KEY (account, destination_tag). The orphan's failed
    swap holds discriminator 1 on the shared Solana account and so does a COMPLETED swap
    in the authority. Before this, the insert raised IntegrityError and rolled the whole
    merge back.

    SKIPPING IS CORRECT AND THE TOOL SAYS SO: the orphan's swap is `failed`, which is
    terminal, so its reservation can never be claimed -- while the live swap holding that
    integer is the one the allocator must keep answering for. The attribution survives in
    deposit_events.vout, which carries the same discriminator.

    MUTATION: delete clashing_rows() and this fails with a non-zero exit and an
    IntegrityError on stderr.
    """
    orphan, live = pair
    result = run(orphan, live, "--apply")
    assert result.returncode == 0, result.stdout + result.stderr
    assert "SKIPPED" in result.stdout
    assert "already held by s_aaa81fa6e8538163" in result.stdout
    assert sorted(swaps_in(live)) == sorted(["s_aaa81fa6e8538163", "s_b8daedd5ef8101fe"])

    conn = sqlite3.connect(f"file:{live}?mode=ro", uri=True)
    holder = conn.execute(
        "SELECT swap_id FROM xrp_destination_tags WHERE account = ? AND destination_tag = 1",
        (ACCOUNT,),
    ).fetchall()
    assert holder == [("s_aaa81fa6e8538163",)], (
        f"discriminator 1 must still belong to the live swap; it reads {holder}"
    )
    # AND THE EVIDENCE MOVED ANYWAY, which is the point of skipping rather than refusing.
    moved = conn.execute(
        "SELECT vout FROM deposit_events WHERE swap_id = 's_b8daedd5ef8101fe'"
    ).fetchall()
    assert moved == [(1,)], f"the orphan's deposit row did not move, or lost its discriminator: {moved}"
    conn.close()


# --- the refusals -------------------------------------------------------------

def test_a_swap_id_already_present_is_refused_not_overwritten(tmp_path):
    """Ids are generated per database, so the same id twice is two different swaps."""
    orphan, live = tmp_path / "a.db", tmp_path / "b.db"
    build(orphan, "s_same", "2026-10-01T00:00:00+00:00", "failed", 1)
    build(live, "s_same", "2026-10-05T00:00:00+00:00", "completed", 2)
    result = run(orphan, live, "--apply")
    assert "COLLIDING" in result.stdout, result.stdout
    assert "there is no --force" in result.stdout
    conn = sqlite3.connect(f"file:{live}?mode=ro", uri=True)
    status = conn.execute("SELECT status FROM swaps WHERE id = 's_same'").fetchone()
    conn.close()
    assert status == ("completed",), "the destination's swap was overwritten by the orphan's"


def test_absorbing_a_database_into_itself_is_refused(pair):
    """It would insert every row over itself and report success."""
    _orphan, live = pair
    result = run(live, live, "--apply")
    assert result.returncode == 1, result.stdout
    assert "same file" in result.stdout


def test_a_missing_file_is_refused_with_which_one(tmp_path, pair):
    _orphan, live = pair
    result = run(tmp_path / "nope.db", live, "--apply")
    assert result.returncode == 2
    assert "source" in result.stdout and "does not exist" in result.stdout


def test_the_source_is_opened_READ_ONLY_so_a_bug_cannot_damage_it(pair):
    """mode=ro is a guarantee from sqlite, not an intention in this file.

    The source is the only copy of whatever it holds, so this is asserted by trying to
    write through the same URI the tool uses and requiring sqlite to refuse.
    """
    orphan, _live = pair
    conn = sqlite3.connect(f"file:{orphan}?mode=ro", uri=True)
    with pytest.raises(sqlite3.OperationalError, match="readonly"):
        conn.execute("DELETE FROM swaps")
    conn.close()


def test_running_it_twice_moves_nothing_the_second_time(pair):
    """Idempotent, because every insert is a row the destination did not have."""
    orphan, live = pair
    first = run(orphan, live, "--apply")
    assert first.returncode == 0, first.stdout + first.stderr
    after_first = swaps_in(live)
    second = run(orphan, live, "--apply")
    assert second.returncode == 0, second.stdout + second.stderr
    assert "COLLIDING" in second.stdout, "the second run did not recognize the swap it just moved"
    assert swaps_in(live) == after_first, "the second run changed the destination"
