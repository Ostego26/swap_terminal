"""Proving ownership of a Gridcoin address by signature: the real functions, seeded rows, a stubbed RPC.

Role: test (behavioral verification of services/grc_login_service.py,
      chains/grc_message_signing.py, secret_key_shapes.py, routes/grc_login.py
      and db.py's address_proof_challenges triggers)
Reads: a temporary database on the REAL schema, and the real Flask app's test
      client
Writes: that temporary database only
Can move funds: no. Every adapter here is a stub that records what it was asked
      and can send nothing. No socket is opened to any chain.
Mainnet-safe: yes

WHAT THESE ASSERT ON, AND WHAT THEY REFUSE TO ASSERT ON. Rows and rendered
output, never source text and never SQL text. Every challenge is issued by the
real issue_challenge(), every verdict is reached by the real
verify_address_proof(), and every trigger is exercised by running the statement
it forbids and catching the database's refusal -- not by grepping the schema for
the word RAISE. CLAUDE.md's behavioral-verification principle is absolute about
that, and the reason is specific: a gate's stored text says nothing about
whether the gate runs.

THE MOST IMPORTANT TEST IN THIS FILE is
test_a_pasted_private_key_is_in_neither_the_database_nor_the_log. The whole
feature exists because no private key has to reach this host; the one way that
fails is a customer pasting a key into the signature box, and that test is what
proves the value lands in no column and in no log record -- not truncated, not
hashed, not in `record.args` where a value survives whether or not any handler
formats the message.

NO ADDRESS OR KEY LITERAL IS HAND-WRITTEN HERE. Addresses come from
tests/valid_addresses.py, and the WIF-shaped and signature-shaped fixtures below
are DERIVED by the same method that file uses -- encoded from a hash of a
phrase, so they are correctly shaped by construction and nobody has to trust a
string somebody typed. tests/test_address_literals_are_valid.py is the gate that
makes that a rule rather than a habit.
"""

from __future__ import annotations

import base64
import hashlib
import logging
import sqlite3
from datetime import UTC, datetime, timedelta

import base58
import pytest
import requests
from chains.base import RPCError
from chains.grc_message_signing import (
    MessageVerificationRefusedInput,
    MessageVerificationUnavailable,
    WalletUnlockDemanded,
    rpc_error_code,
    verify_message,
)
from db import SCHEMA, dict_factory
from secret_key_shapes import (
    ALL_SHAPES,
    SHAPE_ED25519_BASE58,
    SHAPE_HEX_PRIVATE_KEY,
    SHAPE_SEED_PHRASE,
    SHAPE_WIF,
    secret_key_shape,
)
from services import grc_login_service as proof
from valid_addresses import GRC_PARTICIPANT, GRC_PAYOUT

# `app` imports and calls create_app() at module scope, and conftest.py has
# already pointed SWAP_DB_PATH at a temp file by the time this import runs. Same
# ordering note as tests/test_web_surfaces.py.
import app as app_module  # isort: skip

# A 65-byte compact signature, base64-encoded: the shape `signmessage` prints.
# DERIVED rather than typed, so it is the right length by construction -- 65
# bytes is what Gridcoin's SignCompact produces and what RecoverCompact consumes,
# read from its source at commit 36bc6a2d.
SIGNATURE_SHAPED = base64.b64encode(hashlib.sha512(b"swap_terminal address proof signature fixture").digest()[:65]).decode()

# A WIF-SHAPED PRIVATE KEY, DERIVED, AND IT CONTROLS NOTHING.
#
# base58-check of one version byte plus 32 bytes of a published hash, so it has
# exactly the encoded length and alphabet of a real WIF -- which is the only
# property under test -- while being nobody's key: the 32 bytes are SHA-256 of a
# phrase written in this file, so anyone who can read this test can derive it,
# which is the definition of not secret.
#
# 0xBE is Gridcoin's mainnet WIF version byte (its pubkey-hash version 0x3E plus
# 0x80, the Bitcoin-family convention). The version is not what
# secret_key_shapes.py tests -- it checks length and alphabet, deliberately, so
# that a MISTYPED key is refused as loudly as a clean one -- but using the real
# one keeps the fixture honest about what it is imitating.
_PRETEND_KEY_BYTES = hashlib.sha256(b"swap_terminal test fixture: not a real private key").digest()
WIF_SHAPED_NOT_A_REAL_KEY = base58.b58encode_check(bytes([0xBE]) + _PRETEND_KEY_BYTES).decode()

# The same non-key written as 64 hex characters, which is the other shape a
# customer can paste.
HEX_SHAPED_NOT_A_REAL_KEY = _PRETEND_KEY_BYTES.hex()


def seed_swap(conn, swap_id: str) -> None:
    """One quote and one swap, so address_proof_challenges' FOREIGN KEY is satisfiable.

    Minimal on purpose: nothing in this feature reads any swap column except the
    id, and a fixture that filled in twenty columns would imply otherwise.
    GRC_PAYOUT rather than a placeholder, because
    tests/test_address_literals_are_valid.py is a clean gate over every
    address-shaped literal in the tree.
    """
    conn.execute(
        "INSERT INTO quotes (id, from_asset, to_asset, input_amount, quoted_rate, fee_bps, "
        "network_fee_reserve, output_amount_estimate, expires_at, created_at) "
        "VALUES (?, 'GRC', 'BTC', 1.0, 1.0, 0, 0.0, 1.0, 'z', 'z')",
        (f"quote_for_{swap_id}",),
    )
    conn.execute(
        "INSERT INTO swaps (id, quote_id, from_asset, to_asset, deposit_address, payout_address, "
        "expected_input_amount, quoted_rate, fee_bps, network_fee_reserve, output_amount_estimate, "
        "status, min_confirmations, created_at, updated_at, expires_at) "
        "VALUES (?, ?, 'GRC', 'BTC', ?, ?, 1.0, 1.0, 0, 0.0, 1.0, 'awaiting_deposit', 6, 'z', 'z', 'z')",
        (swap_id, f"quote_for_{swap_id}", GRC_PAYOUT, GRC_PARTICIPANT),
    )
    conn.commit()


@pytest.fixture
def db(tmp_path):
    """A connection on the REAL schema, with two swaps seeded."""
    connection = sqlite3.connect(tmp_path / "proof.db")
    connection.row_factory = dict_factory
    connection.executescript(SCHEMA)
    seed_swap(connection, "swap_one")
    seed_swap(connection, "swap_two")
    yield connection
    connection.close()


class StubDaemon:
    """An adapter that records every call and answers however the test says.

    It is the whole RPC surface this feature can reach, which is what makes
    test_verifymessage_is_the_only_method_this_feature_calls meaningful: if any
    code path ever reached for `walletpassphrase`, this stub would have recorded
    it.
    """

    asset = "GRC"

    def __init__(self, answer):
        self.answer = answer
        self.calls: list[tuple] = []

    def call(self, method: str, *params):
        self.calls.append((method, *params))
        if isinstance(self.answer, Exception):
            raise self.answer
        return self.answer


def submission(challenge: str, address: str = GRC_PAYOUT, signature: str = SIGNATURE_SHAPED) -> proof.Submission:
    return proof.Submission(challenge=challenge, address=address, signature=signature)


def iso(seconds_from_now: float = 0.0) -> str:
    return (datetime.now(UTC) + timedelta(seconds=seconds_from_now)).isoformat()


def all_log_text(caplog) -> str:
    """Every rendered message PLUS every raw argument, as one string.

    Both halves, for the reason tests/test_secrets_are_not_logged.py gives:
    `logger.warning("x=%s", value)` leaves `value` in `record.args` whether or
    not the message is ever formatted, so a test that reads only
    `record.getMessage()` can miss a leak that a real handler would print.
    """
    parts = []
    for record in caplog.records:
        parts.append(record.getMessage())
        parts.append(repr(record.args))
    return " ".join(parts)


def proof_rows(db) -> list[dict]:
    return [dict(row) for row in db.execute("SELECT * FROM address_proof_challenges ORDER BY issued_at").fetchall()]


# =============================================================================
# THE HAPPY PATH, AND THE RPC IT ACTUALLY MAKES
# =============================================================================


def test_a_valid_signature_proves_the_address(db):
    challenge = proof.issue_challenge(db, "swap_one")["challenge"]
    daemon = StubDaemon(True)

    outcome = proof.verify_address_proof(db, daemon, "swap_one", submission(challenge))

    assert outcome.proven is True
    assert outcome.outcome == proof.OUTCOME_PROVEN
    assert outcome.address == GRC_PAYOUT
    rows = proof_rows(db)
    assert len(rows) == 1
    assert rows[0]["proven_address"] == GRC_PAYOUT
    assert rows[0]["proven_at"] is not None


def test_the_daemon_is_asked_verifymessage_with_address_signature_message_in_that_order(db):
    """The parameter ORDER, asserted on the recorded call rather than read off the source.

    This is the test that would have caught the order the brief for this feature
    stated -- (address, message, signature). That order hands the challenge to
    the daemon's base64 decoder and the signature to its hasher, so every
    verification fails or errors, forever, with nothing in any log saying why.
    A proof system that always answers no is indistinguishable from one that
    works and has no honest users.

    The order was established twice independently before this was written: from
    Gridcoin's source at commit 36bc6a2d, and from `help verifymessage` on the
    operator's running testnet daemon. Both say
    `verifymessage <address> <signature> <message>`.
    """
    challenge = proof.issue_challenge(db, "swap_one")["challenge"]
    daemon = StubDaemon(True)

    proof.verify_address_proof(db, daemon, "swap_one", submission(challenge))

    assert daemon.calls == [("verifymessage", GRC_PAYOUT, SIGNATURE_SHAPED, challenge)]


def test_verifymessage_is_the_only_method_this_feature_calls(db):
    """NOTHING here unlocks a wallet, and this is what says so about behavior.

    The stub records every method name it is handed, so a code path that reached
    for `walletpassphrase` -- or `dumpprivkey`, or `signmessage`, or anything
    else -- would appear here. The assertion is on the SET of methods, so a new
    call added anywhere behind verify_address_proof() fails this rather than
    passing unnoticed.
    """
    challenge = proof.issue_challenge(db, "swap_one")["challenge"]
    daemon = StubDaemon(True)

    proof.verify_address_proof(db, daemon, "swap_one", submission(challenge))

    assert {call[0] for call in daemon.calls} == {"verifymessage"}


# =============================================================================
# THE REFUSALS, EACH DISTINGUISHABLE FROM THE OTHERS
# =============================================================================


def test_an_invalid_signature_proves_nothing_and_records_nothing(db):
    challenge = proof.issue_challenge(db, "swap_one")["challenge"]

    outcome = proof.verify_address_proof(db, StubDaemon(False), "swap_one", submission(challenge))

    assert outcome.proven is False
    assert outcome.outcome == proof.OUTCOME_SIGNATURE_REFUSED
    assert outcome.address is None
    row = proof_rows(db)[0]
    assert row["proven_address"] is None, "a refused signature must not record an address"
    assert row["proven_at"] is None, "a refused signature must leave the challenge unconsumed, so a typo can be retried"


def test_a_reused_challenge_is_refused_even_with_a_valid_signature(db):
    """Single use. The second attempt presents the SAME valid signature and loses.

    This is the replay case. A message signature is valid forever, so the only
    thing that makes a captured one worthless is the challenge never being
    accepted again.
    """
    challenge = proof.issue_challenge(db, "swap_one")["challenge"]
    first = proof.verify_address_proof(db, StubDaemon(True), "swap_one", submission(challenge))
    assert first.proven is True

    second = proof.verify_address_proof(db, StubDaemon(True), "swap_one", submission(challenge))

    assert second.proven is False
    assert second.outcome == proof.OUTCOME_CHALLENGE_ALREADY_USED
    assert len(proof_rows(db)) == 1, "a replay must not write a second row"


def test_a_reused_challenge_cannot_switch_the_proven_address(db):
    """The replay that would actually be worth mounting, and it is refused.

    Proving the same address twice is harmless. Re-presenting a spent challenge
    with a DIFFERENT address in the box is the attack: it would overwrite whose
    address this swap had proven. The recorded address must not move.
    """
    challenge = proof.issue_challenge(db, "swap_one")["challenge"]
    proof.verify_address_proof(db, StubDaemon(True), "swap_one", submission(challenge))

    outcome = proof.verify_address_proof(
        db, StubDaemon(True), "swap_one", submission(challenge, address=GRC_PARTICIPANT)
    )

    assert outcome.proven is False
    assert proof_rows(db)[0]["proven_address"] == GRC_PAYOUT


def test_the_single_use_predicate_is_in_the_consuming_statement_and_refuses_cleanly(db):
    """A spent challenge is refused BY THE UPDATE, with rowcount 0 and no exception.

    ADDED BECAUSE A MUTATION SURVIVED. Dropping `AND proven_at IS NULL` from
    _CONSUME_SQL left every reuse test passing: verify_address_proof() reads the
    row first to choose its wording, so the Python branch returned
    "already used" before the weakened statement was ever reached. The test
    looked like it was pinning the single-use rule and was pinning the error
    message.

    So this runs the real statement directly against an already-proven row, the
    same way the expiry and swap-binding tests do, and asserts TWO things:

      rowcount == 0 -- the challenge was refused;
      no exception  -- it was refused by the PREDICATE, not by the trigger.

    The second is the part worth having. db.py's address_proofs_are_single_use
    trigger would also stop the write with the predicate gone, but it stops it by
    RAISE(ABORT), which reaches a customer as a 500 rather than as "that
    challenge has already been used". The mechanism must refuse cleanly and the
    trigger must stay the thing that never has to fire.
    """
    challenge = proof.issue_challenge(db, "swap_one")["challenge"]
    proof.verify_address_proof(db, StubDaemon(True), "swap_one", submission(challenge))

    cursor = db.execute(
        proof._CONSUME_SQL,
        {"address": GRC_PARTICIPANT, "now": iso(), "challenge": challenge, "swap_id": "swap_one"},
    )
    db.commit()

    assert cursor.rowcount == 0
    assert proof_rows(db)[0]["proven_address"] == GRC_PAYOUT


def test_an_expired_challenge_is_refused(db):
    """Seeded already-expired, so the clock is an input rather than a wait."""
    issued = proof.issue_challenge(db, "swap_one", now=iso(-proof.CHALLENGE_TTL_SECONDS - 60))

    outcome = proof.verify_address_proof(db, StubDaemon(True), "swap_one", submission(issued["challenge"]))

    assert outcome.proven is False
    assert outcome.outcome == proof.OUTCOME_CHALLENGE_EXPIRED
    assert proof_rows(db)[0]["proven_at"] is None


def test_the_expiry_predicate_is_in_the_consuming_statement_and_not_only_in_python(db):
    """Expiry refuses even when the Python pre-check is bypassed entirely.

    The pre-check exists to choose a sentence; the AUTHORITY is the UPDATE's own
    `julianday(expires_at) > julianday(:now)`. This runs that statement directly
    against an expired row -- the real SQL, the real schema -- and asserts it
    changes nothing. A reader who later deletes the Python branch must still find
    the gate holding.
    """
    issued = proof.issue_challenge(db, "swap_one", now=iso(-proof.CHALLENGE_TTL_SECONDS - 60))

    cursor = db.execute(
        proof._CONSUME_SQL,
        {"address": GRC_PAYOUT, "now": iso(), "challenge": issued["challenge"], "swap_id": "swap_one"},
    )
    db.commit()

    assert cursor.rowcount == 0
    assert proof_rows(db)[0]["proven_at"] is None


def test_a_challenge_for_one_swap_cannot_prove_an_address_for_another(db):
    challenge_for_one = proof.issue_challenge(db, "swap_one")["challenge"]

    outcome = proof.verify_address_proof(db, StubDaemon(True), "swap_two", submission(challenge_for_one))

    assert outcome.proven is False
    assert outcome.outcome == proof.OUTCOME_CHALLENGE_OTHER_SWAP
    assert proof_rows(db)[0]["proven_at"] is None


def test_the_swap_binding_is_in_the_consuming_statement_too(db):
    """Same shape as the expiry test: the UPDATE itself refuses the wrong swap."""
    challenge_for_one = proof.issue_challenge(db, "swap_one")["challenge"]

    cursor = db.execute(
        proof._CONSUME_SQL,
        {"address": GRC_PAYOUT, "now": iso(), "challenge": challenge_for_one, "swap_id": "swap_two"},
    )
    db.commit()

    assert cursor.rowcount == 0
    assert proof_rows(db)[0]["proven_at"] is None


def test_an_unknown_challenge_is_refused(db):
    outcome = proof.verify_address_proof(db, StubDaemon(True), "swap_one", submission("not a challenge we issued"))

    assert outcome.outcome == proof.OUTCOME_CHALLENGE_UNKNOWN
    assert proof_rows(db) == []


def test_an_empty_submission_is_its_own_outcome_and_asks_no_daemon(db):
    daemon = StubDaemon(True)

    outcome = proof.verify_address_proof(db, daemon, "swap_one", submission("", address="", signature=""))

    assert outcome.outcome == proof.OUTCOME_NOTHING_PASTED
    assert daemon.calls == [], "a blank form must not reach the daemon at all"


def test_whitespace_around_a_pasted_challenge_does_not_break_it(db):
    """A trailing newline is what every copy button and terminal selection brings.

    Without stripping, the single most likely cause of a correct proof being
    refused would be an invisible character -- and the message would say "that
    challenge is not one we issued", which reads as "my key is wrong".
    """
    challenge = proof.issue_challenge(db, "swap_one")["challenge"]

    outcome = proof.verify_address_proof(
        db,
        StubDaemon(True),
        "swap_one",
        proof.Submission(challenge=f"  {challenge}\n", address=f" {GRC_PAYOUT} ", signature=f"{SIGNATURE_SHAPED}\n"),
    )

    assert outcome.proven is True
    assert proof_rows(db)[0]["proven_address"] == GRC_PAYOUT, "the stored address must be the stripped one"


# =============================================================================
# "WE COULD NOT CHECK" IS NOT "YOUR SIGNATURE IS WRONG"
# =============================================================================


def test_an_unreachable_daemon_is_distinguishable_from_a_refusal(db):
    """The distinction CLAUDE.md rule 12 is about, on the one path that needs it.

    Both outcomes have `proven is False`, so a caller reading only that bit
    cannot tell them apart -- which is exactly why `outcome` exists and why this
    asserts on it. It also asserts the two codes are not equal, so a later edit
    that collapses them fails here rather than quietly telling a customer their
    key is wrong during an outage.
    """
    challenge = proof.issue_challenge(db, "swap_one")["challenge"]
    down = StubDaemon(requests.ConnectionError("connection refused"))

    outage = proof.verify_address_proof(db, down, "swap_one", submission(challenge))

    assert outage.proven is False
    assert outage.outcome == proof.OUTCOME_DAEMON_UNREACHABLE
    assert outage.outcome != proof.OUTCOME_SIGNATURE_REFUSED
    assert outage.outcome in proof.UNAVAILABLE_OUTCOMES
    assert proof_rows(db)[0]["proven_at"] is None, "an outage must leave the challenge usable"


def test_a_malformed_paste_is_an_answer_about_the_paste(db):
    """Gridcoin RAISES for an unreadable address or signature rather than returning false.

    Read from its source at commit 36bc6a2d: verifymessage() throws
    RPC_INVALID_ADDRESS_OR_KEY (-5) for an address it cannot decode and
    RPC_TYPE_ERROR (-3) for "Malformed base64 encoding". So a malformed paste is
    a THIRD outcome, not a negative verdict -- the customer should look at what
    they copied, not at which key they signed with.
    """
    challenge = proof.issue_challenge(db, "swap_one")["challenge"]
    fussy = StubDaemon(RPCError("Malformed base64 encoding (rpc code -3)"))

    outcome = proof.verify_address_proof(db, fussy, "swap_one", submission(challenge))

    assert outcome.outcome == proof.OUTCOME_INPUT_REFUSED
    assert outcome.outcome not in proof.UNAVAILABLE_OUTCOMES, "it IS an answer; it is just not a verdict"
    assert outcome.outcome != proof.OUTCOME_SIGNATURE_REFUSED
    assert proof_rows(db)[0]["proven_at"] is None


def test_a_daemon_demanding_a_wallet_unlock_gets_its_own_outcome_and_no_unlock(db):
    """The fallback, in code rather than in a paragraph.

    If a Gridcoin build ever answers verifymessage with RPC_WALLET_UNLOCK_NEEDED
    (-13), the safety argument for this whole feature is false for that build.
    It must be visible as itself -- not as a generic outage -- and NOTHING may
    respond by unlocking anything. The stub records every call, so the assertion
    that only `verifymessage` was attempted is what proves no unlock was tried.
    """
    challenge = proof.issue_challenge(db, "swap_one")["challenge"]
    locked = StubDaemon(RPCError("Error: Please enter the wallet passphrase with walletpassphrase first. (rpc code -13)"))

    outcome = proof.verify_address_proof(db, locked, "swap_one", submission(challenge))

    assert outcome.outcome == proof.OUTCOME_DAEMON_WANTS_UNLOCK
    assert outcome.outcome in proof.UNAVAILABLE_OUTCOMES
    assert {call[0] for call in locked.calls} == {"verifymessage"}, "nothing may attempt an unlock in response"
    assert proof_rows(db)[0]["proven_at"] is None


def test_an_unconfigured_chain_is_not_a_refusal_either(db):
    """adapter=None is an operational fact, not a programming error and not a verdict."""
    challenge = proof.issue_challenge(db, "swap_one")["challenge"]

    outcome = proof.verify_address_proof(db, None, "swap_one", submission(challenge))

    assert outcome.outcome == proof.OUTCOME_DAEMON_NOT_CONFIGURED
    assert outcome.outcome in proof.UNAVAILABLE_OUTCOMES
    assert proof_rows(db)[0]["proven_at"] is None


def test_an_unreadable_daemon_error_is_treated_as_unanswered_rather_than_as_a_no(db):
    """Fails CLOSED: an error code this code cannot interpret never becomes a refusal."""
    challenge = proof.issue_challenge(db, "swap_one")["challenge"]
    odd = StubDaemon(RPCError("something nobody has seen before"))

    outcome = proof.verify_address_proof(db, odd, "swap_one", submission(challenge))

    assert outcome.outcome == proof.OUTCOME_DAEMON_UNREACHABLE


def test_a_non_boolean_answer_is_not_read_as_a_verdict():
    """A truthy dict must not read as PROVEN.

    Gridcoin's own help declares this method returns a bool, so anything else
    means something other than the expected daemon is on that port. `bool(result)`
    would turn a dict into a yes.
    """
    with pytest.raises(MessageVerificationUnavailable):
        verify_message(StubDaemon({"verified": True}), GRC_PAYOUT, SIGNATURE_SHAPED, "a challenge")


def test_the_wrapper_raises_the_three_failures_as_three_types():
    """chains/grc_message_signing.py's contract, exercised directly."""
    assert verify_message(StubDaemon(True), GRC_PAYOUT, SIGNATURE_SHAPED, "m") is True
    assert verify_message(StubDaemon(False), GRC_PAYOUT, SIGNATURE_SHAPED, "m") is False
    with pytest.raises(MessageVerificationRefusedInput):
        verify_message(StubDaemon(RPCError("Invalid address (rpc code -5)")), GRC_PAYOUT, SIGNATURE_SHAPED, "m")
    with pytest.raises(WalletUnlockDemanded):
        verify_message(StubDaemon(RPCError("locked (rpc code -13)")), GRC_PAYOUT, SIGNATURE_SHAPED, "m")
    with pytest.raises(MessageVerificationUnavailable):
        verify_message(StubDaemon(requests.Timeout("timed out")), GRC_PAYOUT, SIGNATURE_SHAPED, "m")


def test_wallet_unlock_demanded_is_also_an_unavailable_outcome():
    """The subclass relationship, asserted -- because the except-order depends on it.

    `except MessageVerificationUnavailable` placed above
    `except WalletUnlockDemanded` would make the second branch unreachable, which
    is the dead-branch defect CLAUDE.md rule 19 records finding twice: editing an
    unreachable veto looks exactly like a fix.
    """
    assert issubclass(WalletUnlockDemanded, MessageVerificationUnavailable)


def test_the_rpc_code_is_recovered_from_the_string_base_py_builds():
    """The coupling to chains/base.py's error formatting, pinned.

    If that f-string changes shape this returns None, and _classify() then falls
    through to "could not check" -- a confusing message, never a wrong verdict.
    """
    assert rpc_error_code("Invalid address (rpc code -5)") == -5
    assert rpc_error_code("Malformed base64 encoding (rpc code -3)") == -3
    assert rpc_error_code("401 Client Error: Unauthorized") is None


# =============================================================================
# THE PASTED PRIVATE KEY. THE MOST IMPORTANT TEST IN THIS FILE.
# =============================================================================


def test_a_pasted_private_key_is_in_neither_the_database_nor_the_log(db, caplog):
    """A key pasted into the signature box: refused, not stored, not logged.

    THE THREE ASSERTIONS THAT MATTER, in the order they would fail:

      the outcome is the key-material refusal, so the customer is TOLD rather
      than given a mystifying "signature did not verify";
      the pasted value appears NOWHERE in the database -- every column of every
      row is searched, not just the ones a reader expects, because the point is
      that no column anywhere holds it;
      the pasted value appears NOWHERE in any log record captured at DEBUG --
      the most verbose level -- including in `record.args`, where a value
      survives whether or not a handler ever formats the message.

    The database is also checked for a TRUNCATION and for a HASH of the value.
    Storing either would be storing the key with an extra step: a WIF is 32
    bytes of entropy with known structure, so its hash is a target, and a prefix
    of it narrows the search space.
    """
    challenge = proof.issue_challenge(db, "swap_one")["challenge"]
    daemon = StubDaemon(True)

    with caplog.at_level(logging.DEBUG):
        outcome = proof.verify_address_proof(
            db, daemon, "swap_one", submission(challenge, signature=WIF_SHAPED_NOT_A_REAL_KEY)
        )

    assert outcome.proven is False
    assert outcome.outcome == proof.OUTCOME_KEY_MATERIAL_PASTED

    assert daemon.calls == [], "a pasted key must never be sent to the daemon, where it would land in ITS log too"

    whole_database = " ".join(
        str(value) for row in db.execute("SELECT * FROM address_proof_challenges").fetchall() for value in row.values()
    )
    assert WIF_SHAPED_NOT_A_REAL_KEY not in whole_database
    assert WIF_SHAPED_NOT_A_REAL_KEY[:16] not in whole_database, "not even a prefix: it narrows the search space"
    assert hashlib.sha256(WIF_SHAPED_NOT_A_REAL_KEY.encode()).hexdigest() not in whole_database, (
        "a hash of a WIF is a hash of 32 bytes with known structure; storing one is storing the key with an extra step"
    )
    assert proof_rows(db)[0]["proven_at"] is None, "nothing was recorded"

    text = all_log_text(caplog)
    assert WIF_SHAPED_NOT_A_REAL_KEY not in text
    assert WIF_SHAPED_NOT_A_REAL_KEY[:16] not in text
    assert hashlib.sha256(WIF_SHAPED_NOT_A_REAL_KEY.encode()).hexdigest() not in text


def test_the_key_material_refusal_still_says_enough_to_be_useful(db, caplog):
    """The replacement has to be USEFUL, or the next person puts the value back.

    The log must name the swap and the SHAPE -- a fixed string chosen in
    secret_key_shapes.py and never built from the input -- so an operator can see
    that it happened and to whom. tests/test_secrets_are_not_logged.py makes the
    same argument about logging a secret's hash instead of the secret.
    """
    challenge = proof.issue_challenge(db, "swap_one")["challenge"]

    with caplog.at_level(logging.DEBUG):
        outcome = proof.verify_address_proof(
            db, StubDaemon(True), "swap_one", submission(challenge, signature=WIF_SHAPED_NOT_A_REAL_KEY)
        )

    text = all_log_text(caplog)
    assert "swap_one" in text
    assert SHAPE_WIF in text
    assert "NOTHING was stored" in text
    # And the customer is told the three things they need: that it looks like a
    # secret, that we did not keep it, and what to do about it.
    assert "SECRET KEY" in outcome.headline
    assert "did not store or log it" in outcome.detail
    assert "COMPROMISED" in outcome.detail
    assert "move those funds" in outcome.detail


def test_a_pasted_private_key_in_the_address_box_is_refused_too(db):
    """A customer who swaps the two boxes round pastes the key into the other one."""
    challenge = proof.issue_challenge(db, "swap_one")["challenge"]
    daemon = StubDaemon(True)

    outcome = proof.verify_address_proof(
        db, daemon, "swap_one", submission(challenge, address=WIF_SHAPED_NOT_A_REAL_KEY)
    )

    assert outcome.outcome == proof.OUTCOME_KEY_MATERIAL_PASTED
    assert "address box" in outcome.detail
    assert daemon.calls == []


def test_a_pasted_hex_key_and_a_pasted_seed_phrase_are_refused_as_well(db):
    """Three shapes, three refusals: the box does not only receive WIFs."""
    for pasted in (HEX_SHAPED_NOT_A_REAL_KEY, f"0x{HEX_SHAPED_NOT_A_REAL_KEY}", " ".join(["abandon"] * 12)):
        challenge = proof.issue_challenge(db, "swap_one")["challenge"]
        daemon = StubDaemon(True)
        outcome = proof.verify_address_proof(db, daemon, "swap_one", submission(challenge, signature=pasted))
        assert outcome.outcome == proof.OUTCOME_KEY_MATERIAL_PASTED, pasted[:12]
        assert daemon.calls == []


def test_a_real_signature_is_not_mistaken_for_a_secret():
    """The false positive that would cost a customer a wallet migration for nothing.

    A base64 signature -- padded, as `signmessage` prints it -- must not be read
    as key material, and neither must an UNPADDED one, which is the case that
    needed the 64-byte decode check rather than a length-and-alphabet test. A
    Gridcoin address must not be either.
    """
    assert secret_key_shape(SIGNATURE_SHAPED) is None
    assert secret_key_shape(SIGNATURE_SHAPED.rstrip("=")) is None
    assert secret_key_shape(GRC_PAYOUT) is None
    assert secret_key_shape("") is None
    assert secret_key_shape("swap_terminal address-proof swap=swap_one nonce=abcdefgh") is None


def test_every_shape_the_classifier_can_return_is_in_its_declared_set():
    """ALL_SHAPES is derived from the dispatch table, so it cannot fall behind it."""
    for pasted, expected in (
        (WIF_SHAPED_NOT_A_REAL_KEY, SHAPE_WIF),
        (HEX_SHAPED_NOT_A_REAL_KEY, SHAPE_HEX_PRIVATE_KEY),
        (" ".join(["abandon"] * 24), SHAPE_SEED_PHRASE),
        # 64 bytes of a hash rather than 64 zero bytes: base58 encodes a leading
        # zero byte as a literal "1", so bytes(64) renders as 64 characters and
        # is not the 87-88 an ed25519 keypair export actually is. Caught by this
        # test failing, which is what it is for.
        (base58.b58encode(hashlib.sha512(b"swap_terminal ed25519 keypair shape fixture").digest()).decode(), SHAPE_ED25519_BASE58),
    ):
        shape = secret_key_shape(pasted)
        assert shape == expected
        assert shape in ALL_SHAPES


# =============================================================================
# THE GUARANTEES THAT ARE IN THE DATABASE, EXERCISED BY RUNNING WHAT THEY FORBID
# =============================================================================


def test_the_database_refuses_to_rewrite_a_proof(db):
    """address_proofs_are_single_use, run rather than read.

    This is the guarantee behind the mechanism: a future writer who drops the
    `AND proven_at IS NULL` from the consuming UPDATE gets an ABORT instead of a
    silent replay.
    """
    challenge = proof.issue_challenge(db, "swap_one")["challenge"]
    proof.verify_address_proof(db, StubDaemon(True), "swap_one", submission(challenge))

    with pytest.raises(sqlite3.IntegrityError) as caught:
        db.execute(
            "UPDATE address_proof_challenges SET proven_address = ? WHERE challenge = ?",
            (GRC_PARTICIPANT, challenge),
        )

    assert proof.is_integrity_refusal(caught.value)
    # The message names its own trigger, because SQLite's RAISE(ABORT) carries
    # nothing else -- see db.py's comment on that measurement.
    assert "address_proofs_are_single_use" in str(caught.value)
    assert proof_rows(db)[0]["proven_address"] == GRC_PAYOUT


def test_the_database_refuses_to_repoint_a_live_challenge(db):
    """address_proof_challenges_are_not_repointed, on an UNPROVEN row.

    Separate from the test above because it fires on a different condition: that
    one protects a completed proof, this one protects the question. Re-pointing a
    live challenge at another swap would hand that swap a proof its customer
    never produced.
    """
    challenge = proof.issue_challenge(db, "swap_one")["challenge"]

    for column, value in (("swap_id", "swap_two"), ("expires_at", iso(99999)), ("asset", "LTC")):
        with pytest.raises(sqlite3.IntegrityError) as caught:
            db.execute(
                f"UPDATE address_proof_challenges SET {column} = ? WHERE challenge = ?",  # noqa: S608 -- identifier, not input: `column` is a literal from the tuple on the line above, and the VALUE goes in as a parameter. A SQL parameter cannot bind an identifier in SQLite, which is the one case rule 12 says this suppression is for.
                (value, challenge),
            )
        assert "immutable" in str(caught.value), column

    assert proof_rows(db)[0]["swap_id"] == "swap_one"


def test_the_database_refuses_to_delete_a_proof_but_allows_deleting_a_spent_challenge(db):
    """Evidence is undeletable; an expired challenge that proved nothing is not.

    The asymmetry is deliberate and is where this diverges from
    xrp_destination_tags_are_never_released. There, allocation reads MAX() over
    every row, so no row may ever go. Here an unconsumed challenge feeds no
    sequence and can never be accepted again, so keeping it forever would be
    housekeeping rather than safety.
    """
    proven_challenge = proof.issue_challenge(db, "swap_one")["challenge"]
    proof.verify_address_proof(db, StubDaemon(True), "swap_one", submission(proven_challenge))
    unproven_challenge = proof.issue_challenge(db, "swap_two")["challenge"]

    with pytest.raises(sqlite3.IntegrityError) as caught:
        db.execute("DELETE FROM address_proof_challenges WHERE challenge = ?", (proven_challenge,))
    assert "never deleted" in str(caught.value)

    db.execute("DELETE FROM address_proof_challenges WHERE challenge = ?", (unproven_challenge,))
    db.commit()
    remaining = [row["challenge"] for row in proof_rows(db)]
    assert remaining == [proven_challenge]


def test_a_half_written_proof_is_refused_by_the_database(db):
    """address_proof_is_whole: proven_at without proven_address marks a challenge spent and proves nothing.

    That is the worst of both outcomes for a customer -- the challenge is burned
    and they have no proof -- which is why it is a CHECK and not a convention.
    """
    challenge = proof.issue_challenge(db, "swap_one")["challenge"]

    with pytest.raises(sqlite3.IntegrityError) as caught:
        db.execute("UPDATE address_proof_challenges SET proven_at = ? WHERE challenge = ?", (iso(), challenge))

    assert "address_proof_is_whole" in str(caught.value)
    assert proof_rows(db)[0]["proven_at"] is None


# =============================================================================
# ISSUING, AND THE PANEL AN OPERATOR AND A CUSTOMER READ
# =============================================================================


def test_a_reload_reuses_the_outstanding_challenge_rather_than_minting_a_second(db):
    """Otherwise a reload invalidates the string the customer is part-way through pasting."""
    first = proof.issue_challenge(db, "swap_one")
    second = proof.issue_challenge(db, "swap_one")

    assert first["challenge"] == second["challenge"]
    assert len(proof_rows(db)) == 1


def test_a_spent_challenge_is_not_reused_and_an_expired_one_is_not_either(db):
    """Reuse applies only to a challenge that is still both unconsumed and unexpired."""
    spent = proof.issue_challenge(db, "swap_one")["challenge"]
    proof.verify_address_proof(db, StubDaemon(True), "swap_one", submission(spent))
    after_spending = proof.issue_challenge(db, "swap_one")["challenge"]
    assert after_spending != spent

    stale = proof.issue_challenge(db, "swap_two", now=iso(-proof.CHALLENGE_TTL_SECONDS - 60))["challenge"]
    after_expiry = proof.issue_challenge(db, "swap_two")["challenge"]
    assert after_expiry != stale


def test_two_challenges_are_never_the_same_string(db):
    """Unguessable, not merely unique. 192 bits from `secrets`, never `random`."""
    issued = set()
    # Issue AND SPEND on every pass. issue_challenge() reuses an outstanding
    # challenge on purpose (a reload must not invalidate what the customer is
    # part-way through signing), so a loop that only issued would collect the
    # same string nine times -- which this test asserted on the first draft and
    # which is exactly the reuse the other test pins.
    for _ in range(9):
        challenge = proof.issue_challenge(db, "swap_one")["challenge"]
        proof.verify_address_proof(db, StubDaemon(True), "swap_one", submission(challenge))
        issued.add(challenge)
    assert len(issued) == 9


def test_the_panel_says_none_rather_than_nothing_when_no_address_is_proven(db):
    """`(none)` is a result and a blank gap is not (rule 14)."""
    panel = proof.proof_panel(db, "swap_one")

    assert panel["proven"] == []
    assert panel["challenge"] in panel["sign_command"]
    assert panel["sign_command"].startswith("signmessage ")
    assert "µfn" in panel["ttl_display"], "rule 6: timings a human reads are microfortnights"
    assert "(900.0s)" in panel["ttl_display"], "with the seconds in parentheses, so the TTL constant is findable"
    assert panel["expired"] is False
    assert panel["result"] is None


def test_the_panel_lists_a_proof_once_there_is_one(db):
    challenge = proof.issue_challenge(db, "swap_one")["challenge"]
    proof.verify_address_proof(db, StubDaemon(True), "swap_one", submission(challenge))

    panel = proof.proof_panel(db, "swap_one")

    assert [row["address"] for row in panel["proven"]] == [GRC_PAYOUT]
    assert "µfn" in panel["proven"][0]["age_display"]


def test_the_panel_reports_an_expired_challenge_as_expired(db):
    proof.issue_challenge(db, "swap_one", now=iso(-proof.CHALLENGE_TTL_SECONDS - 60))

    panel = proof.proof_panel(db, "swap_one", now=iso(-1))

    # A fresh challenge was issued because the stale one is no longer
    # outstanding, so the panel is usable rather than stuck -- which is the
    # behavior that matters, and it is asserted rather than assumed.
    assert panel["expired"] is False
    assert panel["proven"] == []


def test_an_unreadable_proven_timestamp_renders_as_a_state_and_not_as_a_blank(db):
    """A row whose proven_at is not a timestamp is a condition, and the panel says so."""
    challenge = proof.issue_challenge(db, "swap_one")["challenge"]
    proof.verify_address_proof(db, StubDaemon(True), "swap_one", submission(challenge))
    # Written with the triggers' guards in view: this is the one write a reader
    # might think impossible, so it is done by replacing the row wholesale after
    # dropping it, which the DELETE trigger forbids -- so instead a SECOND swap's
    # row is seeded directly in the broken state the display has to survive.
    db.execute(
        "INSERT INTO address_proof_challenges (challenge, swap_id, asset, issued_at, expires_at, proven_address, proven_at) "
        "VALUES ('broken-timestamp-row', 'swap_two', 'GRC', 'z', 'z', ?, 'not-a-timestamp')",
        (GRC_PARTICIPANT,),
    )
    db.commit()

    panel = proof.proof_panel(db, "swap_two")

    assert panel["proven"][0]["age_display"] == "(unreadable timestamp)"


# =============================================================================
# THROUGH THE REAL APP: THE CALL SITE IS WHERE A CORRECT FUNCTION GOES TO DIE
# =============================================================================


@pytest.fixture
def client(tmp_path, monkeypatch):
    """The real Flask app, on the real schema, with a stub GRC adapter."""
    db_path = tmp_path / "surface.db"
    connection = sqlite3.connect(db_path)
    connection.row_factory = dict_factory
    connection.executescript(SCHEMA)
    seed_swap(connection, "swap_one")
    connection.close()

    flask_app = app_module.app
    monkeypatch.setitem(flask_app.config, "DB_PATH", str(db_path))
    flask_app.config["TESTING"] = True
    with flask_app.test_client() as test_client:
        yield test_client


def point_adapter_at(client, monkeypatch, daemon):
    monkeypatch.setitem(client.application.config, "ADAPTERS", {"GRC": daemon})


def challenge_on(page: bytes) -> str:
    """The challenge the page is actually offering, read out of the rendered hidden field.

    Read from the HTML rather than from the database, because what the customer
    posts back is what the page gave them -- and a page that renders a different
    challenge than it stored is precisely the defect this reads around.
    """
    marker = b'name="challenge" value="'
    start = page.index(marker) + len(marker)
    return page[start : page.index(b'"', start)].decode()


def test_the_page_says_what_to_sign_where_to_sign_it_and_what_to_paste_back(client):
    response = client.get("/swap/swap_one/address-proof")

    assert response.status_code == 200
    body = response.data
    assert b"signmessage" in body, "the page must give the literal command to run"
    assert b"your own wallet" in body
    assert b"(none)" in body, "an empty proven-addresses region renders a result, not a gap"
    assert "µfn".encode() in body, "rule 6: the validity window a human reads is in microfortnights"
    assert challenge_on(body) in body.decode()


def test_the_page_has_no_passphrase_password_or_private_key_field(client):
    """The operator's standing instruction, asserted against the rendered bytes.

    Read off the page rather than off the template source, because what matters
    is what a browser is offered. A `type="password"` input, or a field named for
    a passphrase, a seed or a key, would mean this panel had started asking for
    the one thing the design exists to avoid.
    """
    body = client.get("/swap/swap_one/address-proof").data.lower()

    assert b'type="password"' not in body
    for forbidden in (b'name="passphrase"', b'name="password"', b'name="privkey"', b'name="private_key"', b'name="seed"', b'name="mnemonic"', b'name="wif"'):
        assert forbidden not in body, forbidden
    # And the two inputs it DOES have are the two public values.
    assert b'name="address"' in body
    assert b'name="signature"' in body


def test_a_successful_post_renders_the_proof_rather_than_discarding_it(client, monkeypatch):
    """THE CALL-SITE TEST. A correct decision whose caller drops the result is the
    failure that keeps surviving in this codebase, and it looks exactly like a
    working feature.

    So this asserts on what the handler PUT ON THE PAGE -- the outcome code in
    `data-outcome`, the word PROVEN, and the address -- not merely that the row
    was written. Dropping `result=outcome` from the render call in
    routes/grc_login.py leaves the row correct and fails this.
    """
    point_adapter_at(client, monkeypatch, StubDaemon(True))
    challenge = challenge_on(client.get("/swap/swap_one/address-proof").data)

    response = client.post(
        "/swap/swap_one/address-proof",
        data={"challenge": challenge, "address": GRC_PAYOUT, "signature": SIGNATURE_SHAPED},
    )

    assert response.status_code == 200
    body = response.data.decode()
    assert 'data-outcome="proven"' in body
    assert "PROVEN" in body
    assert GRC_PAYOUT in body


def test_a_refused_signature_and_an_unreachable_daemon_do_not_share_a_status_or_a_word(client, monkeypatch):
    """The distinction the brief required, asserted on what a monitor outside the browser sees.

    A refusal returning 200 would make an outage and a wrong signature identical
    to a proxy log and to `curl -i`, which is rule 13's "skipped plus success in
    the same output is a defect in the output" applied to HTTP.
    """
    point_adapter_at(client, monkeypatch, StubDaemon(False))
    challenge = challenge_on(client.get("/swap/swap_one/address-proof").data)
    refused = client.post(
        "/swap/swap_one/address-proof",
        data={"challenge": challenge, "address": GRC_PAYOUT, "signature": SIGNATURE_SHAPED},
    )

    point_adapter_at(client, monkeypatch, StubDaemon(requests.ConnectionError("refused")))
    outage = client.post(
        "/swap/swap_one/address-proof",
        data={"challenge": challenge, "address": GRC_PAYOUT, "signature": SIGNATURE_SHAPED},
    )

    assert refused.status_code == 400
    assert outage.status_code == 503
    assert refused.status_code != outage.status_code
    assert 'data-outcome="signature_refused"' in refused.data.decode()
    assert 'data-outcome="daemon_unreachable"' in outage.data.decode()
    assert b"REFUSED" in refused.data
    assert b"NOT CHECKED" in outage.data


def test_a_posted_private_key_reaches_neither_the_database_the_log_nor_the_page(client, monkeypatch, caplog):
    """The end-to-end version of this file's most important test.

    Through the real route, with the real form fields, asserting the value is
    absent from all three places it could surface: the database, the log, and the
    HTML that goes back to the browser. The last one matters on its own -- a
    template that echoed the submitted value back into the form, which is the
    ordinary and helpful thing to do, would put the key into the page, into the
    browser's history and into any screenshot of it.
    """
    daemon = StubDaemon(True)
    point_adapter_at(client, monkeypatch, daemon)
    challenge = challenge_on(client.get("/swap/swap_one/address-proof").data)

    with caplog.at_level(logging.DEBUG):
        response = client.post(
            "/swap/swap_one/address-proof",
            data={"challenge": challenge, "address": GRC_PAYOUT, "signature": WIF_SHAPED_NOT_A_REAL_KEY},
        )

    assert response.status_code == 400
    assert 'data-outcome="key_material_pasted"' in response.data.decode()
    assert WIF_SHAPED_NOT_A_REAL_KEY not in response.data.decode(), "the page must not echo it back"
    assert WIF_SHAPED_NOT_A_REAL_KEY not in all_log_text(caplog)
    assert daemon.calls == []

    connection = sqlite3.connect(client.application.config["DB_PATH"])
    connection.row_factory = dict_factory
    everything = " ".join(
        str(value)
        for row in connection.execute("SELECT * FROM address_proof_challenges").fetchall()
        for value in row.values()
    )
    connection.close()
    assert WIF_SHAPED_NOT_A_REAL_KEY not in everything
    assert WIF_SHAPED_NOT_A_REAL_KEY[:16] not in everything


def test_no_log_record_carries_the_signature(client, monkeypatch, caplog):
    """A signature is not secret, but it IS replayable, and a log is the artifact that gets pasted."""
    point_adapter_at(client, monkeypatch, StubDaemon(True))
    challenge = challenge_on(client.get("/swap/swap_one/address-proof").data)

    with caplog.at_level(logging.DEBUG):
        client.post(
            "/swap/swap_one/address-proof",
            data={"challenge": challenge, "address": GRC_PAYOUT, "signature": SIGNATURE_SHAPED},
        )

    text = all_log_text(caplog)
    assert SIGNATURE_SHAPED not in text
    assert SIGNATURE_SHAPED[:20] not in text
    # The address and the swap ARE logged, because an operator has to be able to
    # read back what was proven. That is the useful half, and it is asserted so
    # nobody removes it while tightening the half above.
    assert GRC_PAYOUT in text
    assert "swap_one" in text


def test_a_post_for_an_unknown_swap_is_a_404_and_writes_nothing(client, monkeypatch):
    point_adapter_at(client, monkeypatch, StubDaemon(True))

    response = client.post(
        "/swap/no_such_swap/address-proof",
        data={"challenge": "anything", "address": GRC_PAYOUT, "signature": SIGNATURE_SHAPED},
    )

    assert response.status_code == 404
    connection = sqlite3.connect(client.application.config["DB_PATH"])
    rows = connection.execute("SELECT COUNT(*) FROM address_proof_challenges").fetchone()[0]
    connection.close()
    assert rows == 0


def test_a_get_for_an_unknown_swap_is_a_404_rather_than_a_database_error(client):
    """The FOREIGN KEY would otherwise turn an ordinary wrong URL into a 500."""
    assert client.get("/swap/no_such_swap/address-proof").status_code == 404
