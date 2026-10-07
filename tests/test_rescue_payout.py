#!/usr/bin/env python3
"""A swap may be re-driven only when it is PROVABLY un-broadcast. Seeded, no chain.

Role: tests (read-only)
Reads: rescue_payout.py's own functions and a temporary SQLite database it seeds.
      No network, no chain, no live database.
Writes: nothing outside tmp_path
Can move funds: no
Mainnet-safe: yes

WHY THE TOOL EXISTS. The operator's first GRC -> SOL swap credited 10 GRC and then
failed, because the payout worker was running code loaded before the SOL payout was
armed. 'failed' is deliberately never retried -- a payout that MIGHT be on chain
must not be re-sent -- so the deposit was credited and nothing could move the swap
forward. Operator instruction 2026-10-03: "godamit. let's rescue the swap."

WHY THE REFUSALS ARE THE POINT OF THIS FILE, not the happy path.

"failed with no txid" IS NOT PROOF OF NO BROADCAST. A send can raise AFTER the node
accepted the transaction: a socket timeout on sendTransaction leaves money moving
and the code in an exception handler. So the test that matters is not "the real case
is allowed" -- it is that a timeout, a post-signing refusal, a live payout row, a
txid and a missing reason are each REFUSED. Rule 2's distinction, applied to money:
"I could not find a broadcast" is not "there was no broadcast".
"""

from __future__ import annotations

import sqlite3

import pytest
from db import SCHEMA, dict_factory

from rescue_payout import main, refused_before_signing, rescue_verdict

NOW = "2026-10-03T11:29:19"

#: The operator's real recorded reason, verbatim from their failed swap. It is the
#: pre-arming SOL adapter's refusal, from code that imported nothing able to sign.
REAL_REASON = ("this adapter cannot sign or broadcast a Solana transfer, and holds no key that could.")


def swap_row(status="failed", reason=REAL_REASON):
    return {"status": status, "failed_reason": reason}


def payout_row(status="failed", txid=None):
    return {"status": status, "txid": txid, "amount": 0.00076165}


def test_the_operators_real_failed_swap_IS_allowed():
    """The case this was built for, with their exact recorded reason."""
    allowed, why = rescue_verdict(swap_row(), [payout_row()])
    assert allowed, why
    assert "provably never broadcast" in why, why


@pytest.mark.parametrize(("label", "swap", "rows"), [
    # A 'created' row with no txid is EXACTLY the crash-between-send-and-record case
    # services/payout_service.py's docstring warns about: money possibly on chain.
    ("a LIVE payout row", swap_row(), [payout_row(status="created")]),
    ("a txid on the row", swap_row(), [payout_row(txid="5xAbC")]),
    # The case the whole file is cautious about. A timeout on sendTransaction is the
    # one failure that looks identical to a refusal and is not one.
    ("a TIMEOUT", swap_row(reason="HTTPError: read timeout on sendTransaction"), [payout_row()]),
    # Raised AFTER signing. Nothing was submitted on that path either, but signed
    # bytes exist and that is a person's call, not this tool's.
    ("SolanaWireMismatch", swap_row(reason="SolanaWireMismatch: payer differs"), [payout_row()]),
    ("no recorded reason", swap_row(reason=""), [payout_row()]),
    ("a swap that is not failed", swap_row(status="completed"), [payout_row()]),
])
def test_everything_that_is_not_PROVABLY_unbroadcast_is_REFUSED(label, swap, rows):
    """Six ways to be unsure, and every one of them refuses.

    MUTATION: drop any single guard from rescue_verdict(). Its case here fails, and
    a swap whose money might already be on chain becomes re-drivable.
    """
    allowed, why = rescue_verdict(swap, rows)
    assert not allowed, f"{label} was ALLOWED: {why}"
    assert why.strip(), f"{label} refused with no reason, which an operator will route around"


def test_the_PYNACL_refusal_is_allowed_because_both_raise_sites_precede_the_broadcast():
    """The operator's rescue re-drove the swap and it failed on this instead.

    PyNaCl is an OPTIONAL dependency on purpose: chains/registry imports
    chains/solana.py unconditionally, so a module-level import would make a signing
    library mandatory to start a READ-ONLY deposit watcher on a host that has no
    business holding one. A host without it therefore REFUSES rather than crashing.

    ALLOWED BECAUSE BOTH RAISE SITES WERE CHECKED, not because the message says
    "nothing was broadcast". chains/solana_signing._public_key_bytes():527 is
    reached from derive_and_check(), which runs BEFORE sign_message() in
    signed_transfer_wire(); sign_message():716 is the signing call itself, so if it
    raises there are no signed bytes to submit. Reading the raise site is the
    difference between this and the timeout case, whose message would also sound
    reassuring.
    """
    reason = ("PyNaCl is not importable, so nothing was signed and nothing was broadcast. It is an "
              "optional dependency on purpose")
    allowed, why = rescue_verdict(swap_row(reason=reason), [payout_row()])
    assert allowed, why
    assert "pynacl is not importable" in why, why


def test_an_UNRECOGNIZED_reason_refuses_rather_than_assuming_safety():
    """Absence of evidence is not evidence. Rule 2, applied to money.

    A reason this tool does not recognize must refuse, and the refusal must list
    what it would have accepted -- otherwise the operator cannot tell a dangerous
    reason from a merely unfamiliar one.
    """
    allowed, why = rescue_verdict(swap_row(reason="something nobody has seen before"), [payout_row()])
    assert not allowed
    # Matched on the fragments the message ACTUALLY carries. The first version of
    # this looked for "cannot prove" and the sentence reads "is not one this tool
    # can prove happened BEFORE signing" -- a test pinning a phrase I expected
    # rather than the one that is there.
    assert "can prove happened BEFORE signing" in why, why
    assert "AFTER the node accepted the transaction" in why, (
        "the refusal does not state WHY an unrecognized reason is dangerous, so it reads as pedantry "
        "rather than as the double-send risk it is"
    )
    assert "holds no key that could" in why, "the refusal does not say what it would have accepted"


def seeded(tmp_path, *, reserved=0.00076165):
    """The operator's row, as it actually stood: failed, un-broadcast, reservation standing."""
    db_path = tmp_path / "rescue.db"
    conn = sqlite3.connect(db_path)
    conn.row_factory = dict_factory
    conn.executescript(SCHEMA)
    conn.execute("INSERT INTO quotes (id, from_asset, to_asset, input_amount, quoted_rate, fee_bps, "
                 "network_fee_reserve, output_amount_estimate, expires_at, created_at) "
                 "VALUES ('q1','GRC','SOL',10.0,7.73e-05,150,5e-06,0.00076165,?,?)", (NOW, NOW))
    conn.execute("INSERT INTO swaps (id, quote_id, from_asset, to_asset, deposit_address, payout_address, "
                 "expected_input_amount, actual_input_amount, quoted_rate, fee_bps, network_fee_reserve, "
                 "output_amount_estimate, status, failed_reason, min_confirmations, created_at, "
                 "updated_at, credited_at, expires_at) VALUES ('s1','q1','GRC','SOL','mzu','CUB',10.0,"
                 "10.0,7.73e-05,150,5e-06,0.00076165,'failed',?,6,?,?,?,?)",
                 (REAL_REASON, NOW, NOW, NOW, NOW))
    conn.execute("INSERT INTO payouts (swap_id, asset, destination_address, amount, txid, status, "
                 "created_at) VALUES ('s1','SOL','CUB',0.00076165,NULL,'failed',?)", (NOW,))
    conn.execute("INSERT INTO wallet_inventory (asset, hot_confirmed, hot_reserved, hot_available, "
                 "updated_at) VALUES ('SOL', 28.0, ?, ?, ?)", (reserved, 28.0 - reserved, NOW))
    conn.commit()
    conn.close()
    return db_path


def read(db_path, sql):
    conn = sqlite3.connect(db_path)
    conn.row_factory = dict_factory
    try:
        return conn.execute(sql).fetchone()
    finally:
        conn.close()


def test_a_DRY_RUN_writes_nothing(tmp_path, capsys):
    """Rule 14: the checks still run and the verdict still prints."""
    db_path = seeded(tmp_path)
    assert main(["--swap", "s1", "--db", str(db_path)]) == 0
    body = capsys.readouterr().out
    assert "DRY RUN" in body and "ALLOWED" in body, body
    assert read(db_path, "SELECT status FROM swaps WHERE id='s1'")["status"] == "failed"
    assert read(db_path, "SELECT COUNT(*) n FROM swap_audit_log")["n"] == 0


def test_APPLY_releases_the_STANDING_RESERVATION_as_well_as_the_status(tmp_path, capsys):
    """The half that would have been missed, and the reason this needed reading first.

    services/payout_service.py commits the claim, the inventory reservation and the
    payouts row BEFORE the send, and release_inventory_after_send() is called only
    on the correction path -- NOT on a failed send. Measured 2026-10-03: a failed
    attempt leaves its amount standing in hot_reserved.

    Hand the swap back without releasing it and payout_worker reserves the SAME
    amount a second time, which silently halves the hot wallet's apparent
    availability for every later payout.

    MUTATION: delete the wallet_inventory UPDATE. The status still flips, the swap
    still pays, and hot_reserved stays at 0.00076165 forever -- a leak nothing else
    in the tree would report. This test fails on it.
    """
    db_path = seeded(tmp_path)
    assert main(["--swap", "s1", "--db", str(db_path), "--apply"]) == 0

    assert read(db_path, "SELECT status FROM swaps WHERE id='s1'")["status"] == "payout_pending"
    inventory = read(db_path, "SELECT * FROM wallet_inventory WHERE asset='SOL'")
    assert inventory["hot_reserved"] == pytest.approx(0.0), (
        f"the failed attempt's reservation is still standing at {inventory['hot_reserved']}, so "
        f"payout_worker will reserve the same amount again"
    )
    assert inventory["hot_available"] == pytest.approx(28.0), inventory["hot_available"]
    audit = read(db_path, "SELECT * FROM swap_audit_log WHERE swap_id='s1'")
    assert audit["old_status"] == "failed" and audit["new_status"] == "payout_pending", audit
    assert "rescue_payout.py" in (audit["message"] or ""), audit


def test_the_PREVIOUS_refusal_is_kept_in_failed_reason_not_erased(tmp_path):
    """The record of why it failed is evidence and must survive the rescue (rule 7's spirit).

    An operator reading the swap a week later needs to know it was re-driven AND
    what refused it the first time. Overwriting failed_reason with "re-driven" alone
    would lose the only account of the stale-worker incident.
    """
    db_path = seeded(tmp_path)
    main(["--swap", "s1", "--db", str(db_path), "--apply"])
    reason = read(db_path, "SELECT failed_reason FROM swaps WHERE id='s1'")["failed_reason"]
    assert "re-driven" in reason, reason
    assert "holds no key that could" in reason, (
        "the original refusal was erased, so nothing records why the swap failed in the first place"
    )


def test_a_MISSING_swap_refuses_with_a_non_zero_exit(tmp_path, capsys):
    """Rule 13: a run that did nothing must not exit like one that worked."""
    db_path = seeded(tmp_path)
    assert main(["--swap", "s_nope", "--db", str(db_path)]) == 2
    assert "REFUSED" in capsys.readouterr().out


def test_a_REFUSED_swap_exits_3_and_writes_nothing_even_with_APPLY(tmp_path, capsys):
    """--apply must not override a refusal. That is the whole safety property."""
    db_path = seeded(tmp_path)
    conn = sqlite3.connect(db_path)
    conn.execute("UPDATE payouts SET status = 'created' WHERE swap_id = 's1'")
    conn.commit()
    conn.close()

    assert main(["--swap", "s1", "--db", str(db_path), "--apply"]) == 3
    assert "REFUSED" in capsys.readouterr().out
    assert read(db_path, "SELECT status FROM swaps WHERE id='s1'")["status"] == "failed", (
        "a swap with a LIVE payout row was re-driven anyway, which is the double-send this refuses"
    )
    assert read(db_path, "SELECT COUNT(*) n FROM swap_audit_log")["n"] == 0


def test_an_invalid_amount_type_error_is_recognized_as_pre_signing():
    """RPC -3 is raised while CONVERTING the amount, before a transaction exists.

    The first BTC -> LTC swap recorded `Invalid amount (rpc code -3)` and this tool
    refused to re-drive it -- correct for its own reason, since an unrecognized
    message is absence of evidence rather than evidence of safety. The evidence
    exists: -3 is RPC_TYPE_ERROR from Bitcoin Core's AmountFromValue(), which runs
    before the wallet is touched, before an input is selected, before a transaction
    is built.

    MEASURED RATHER THAN REASONED: the operator ran createrawtransaction with the
    same amount class and got the identical -3 while producing NO transaction,
    while a valid amount on the same command returned a hex string. The parser
    fails in isolation with the wallet uninvolved.

    THE CODE IS PART OF THE MARKER. "invalid amount" alone could be another
    daemon's wording for a post-broadcast condition; "(rpc code -3)" pins it to
    parameter conversion, which is what proves nothing was signed.
    """
    assert refused_before_signing("Invalid amount (rpc code -3)")
    assert refused_before_signing("RPCError: Invalid amount (rpc code -3)")


def test_a_bare_invalid_amount_without_the_code_is_NOT_recognized():
    """Because the code is the half that carries the proof.

    Without this, the marker could be shortened to "invalid amount" and the test
    above would still pass -- while matching any daemon that happens to use those
    two words for something that did reach a network.
    """
    assert not refused_before_signing("Invalid amount")
    assert not refused_before_signing("the node reported an invalid amount after relay")


#: The refusal exactly as services/payout_service.py wrote it on the operator's host
#: at 20:26:09 on 2026-10-07, for s_ebb03e8dc1b96e1c -- the first ICP deposit this
#: system ever credited. Used verbatim rather than paraphrased: what is under test is
#: that the marker matches THIS string, and a tidied version could pass while the
#: real one did not.
GRC_UNLOCK_REFUSAL = (
    "GRC payouts need the wallet fully unlocked, and GRIDCOIN_WALLET_PASSPHRASE is not set in this "
    "process's environment. A GRC wallet left unlocked for staking CANNOT send -- the daemon answers "
    "rpc code -4 -- so this refuses before attempting a send that would fail and mark the swap "
    "terminally failed. Set GRIDCOIN_WALLET_PASSPHRASE for the worker process only."
)


def test_a_missing_unlock_passphrase_is_recognized_as_pre_signing():
    """The raise happens while the `with` EXPRESSION is evaluated, so no body runs.

    THE EVIDENCE, recorded here rather than inferred from the wording, which is what
    PRE_SIGNING_MARKERS demands of every entry. Three independent places:

      STRUCTURE   services/payout_service.py:828 is `with payout_unlock_context(
                  destination_asset, adapter):` and broadcast_payout() is at :835,
                  INSIDE it. payout_unlock_context() is a plain function returning a
                  context manager -- NOT a @contextmanager generator -- so its
                  `if not passphrase: raise` at :1111 fires while the `with`
                  expression is being evaluated. No context manager exists yet, so
                  the body cannot have been entered. There is no path from that
                  raise to a send.

      TYPE        PayoutUnlockUnavailable's docstring: "no transaction was created,
                  nothing reached any daemon". It is its own class precisely so it
                  cannot be confused with a send that failed.

      LOG         on the operator's host, the amount-quantization line at
                  20:26:09,280 and `payout FAILED` at 20:26:09,285. Five
                  milliseconds, no RPC between them.
    """
    assert refused_before_signing(GRC_UNLOCK_REFUSAL)
    # And through the audit-log form, which is what rescue_payout actually reads --
    # services/payout_service.py prefixes it when writing the row.
    assert refused_before_signing(f"Payout failed: {GRC_UNLOCK_REFUSAL}")


def test_the_marker_does_not_name_GRC_so_a_second_unlock_chain_still_matches():
    """Matched on the ENVIRONMENT clause, not on "GRC payouts need...".

    payout_service.py builds that sentence from `{asset}`, and
    chains/gridcoin_wallet_lock.WALLET_UNLOCK_ASSETS is a frozenset that can grow. A
    marker naming GRC would silently stop matching the day a second chain needed an
    unlock -- rule 8's drift, inside a safety check, where the failure mode is a
    stranded deposit nobody can rescue.
    """
    assert refused_before_signing(GRC_UNLOCK_REFUSAL.replace("GRC", "FUTURECOIN"))
    assert refused_before_signing(
        "FUTURECOIN payouts need the wallet fully unlocked, and FUTURECOIN_WALLET_PASSPHRASE is not "
        "set in this process's environment."
    )


def test_the_new_marker_does_not_widen_into_anything_that_reached_a_daemon():
    """The danger of a phrase-shaped marker: what else now matches that should not.

    "not set in this process's environment" is specific to a PRECONDITION check --
    something was unset, so nothing was attempted. These are the shapes that must
    still refuse, and each one is a case where money may be on chain.

    THE FIRST VERSION OF THIS TEST MISSED THE OBVIOUS WIDENING, found by a mutation
    that shortened the marker to just "not set" -- all nineteen tests passed. None of
    the unsafe strings below contained those two words, so the test could not see it,
    and a bare "not set" matches plenty of things that happened AFTER a broadcast.
    The cases carrying "not set" are therefore first, and they are the ones that
    matter: the marker has to be specific to an UNSET ENVIRONMENT VARIABLE, which is
    a precondition check, rather than to the words "not set" appearing anywhere.
    """
    for unsafe in (
        # THE WIDENING CASES. Each contains "not set" and each is post-broadcast or
        # unknown, so a marker short enough to match them is a marker that would
        # re-drive a payout that may already be on chain.
        "payout failed: txid not set after broadcast",
        "payout failed: sent_at was not set when the worker returned",
        "payout failed: the transaction was accepted but confirmations are not set yet",
        "payout failed: wallet responded but the fee field is not set",
        # And the shapes that never mentioned it.
        "payout failed: socket timeout after 30s",
        "payout failed: connection reset by peer",
        "payout failed: read timed out waiting for sendtoaddress",
        "payout failed: insufficient funds (rpc code -6)",
        "payout failed: SolanaWireMismatch after signing",
        "payout failed: the daemon accepted the transaction and the re-lock raised",
    ):
        assert not refused_before_signing(unsafe), (
            f"{unsafe!r} now reads as pre-signing. Every one of these can leave money moving, which "
            f"is what this file exists to refuse."
        )
