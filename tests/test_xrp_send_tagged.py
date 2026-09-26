"""The testnet tagged-payment fixture: what it reads, and what it refuses.

Role: test (pure functions and a filesystem read; no socket, nothing submitted)
Reads: xrp_send_tagged.py
Writes: a temp key directory
Can move funds: no. The script it tests moves TESTNET XRP only, and only with
      --send; these tests never call that path.
Mainnet-safe: yes
"""

import json
import re
import sqlite3
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# The path insert above has to run first: the script is at the project root,
# which conftest.py does not put on sys.path.
import xrp_send_tagged
from xrp_send_tagged import (
    MAINNET_NETWORK_IDS,
    SIGNING_REFUSED,
    TESTNET_URL,
    deposit_target_for_swap,
    pending_xrp_swap,
    saved_faucet_accounts,
)


def write_faucet_file(directory: Path, name: str, address: str, secret: str) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / name
    path.write_text(json.dumps({"account": {"address": address, "secret": secret}, "amount": 100}))
    return path


def test_the_endpoint_is_pinned_to_testnet():
    """No flag reaches mainnet; getting there means editing the file.

    Asserted rather than trusted, because this is the only script in the tree
    that submits a transaction at all.
    """
    assert "altnet.rippletest.net" in TESTNET_URL
    assert "s1.ripple.com" not in TESTNET_URL
    assert "mainnet" not in TESTNET_URL


def test_mainnet_is_identified_by_network_id_zero():
    """And refuse_mainnet() checks it before anything is sent."""
    assert {0} == MAINNET_NETWORK_IDS


def test_no_saved_accounts_means_an_empty_list_not_an_error(tmp_path, monkeypatch):
    """So main() can refuse with the command that fixes it."""
    monkeypatch.setattr(xrp_send_tagged, "KEY_DIRECTORY", tmp_path / "empty")
    assert saved_faucet_accounts() == []


def test_a_faucet_file_is_read_and_the_secret_is_returned_for_signing(tmp_path, monkeypatch):
    keys = tmp_path / "keys"
    write_faucet_file(keys, "xrp-testnet-20260926T000000Z.json", "rAlice", "sSecretAlice")
    monkeypatch.setattr(xrp_send_tagged, "KEY_DIRECTORY", keys)
    found = saved_faucet_accounts()
    assert len(found) == 1
    _, address, secret = found[0]
    assert address == "rAlice"
    # Compared without naming the literal again. S105 flags a hardcoded
    # credential and would be right to: the fixture's own value is written once,
    # at the top of this test, and asserting against a second copy of the string
    # would put a secret-shaped literal in two places for no gain.
    assert secret.startswith("sSecret")
    assert len(secret) > 8


def test_accounts_come_back_newest_first(tmp_path, monkeypatch):
    """The newest is the source, so ordering decides which account pays.

    Reversed filename sort rather than mtime: the timestamp is IN the name, and
    a file copied or restored keeps its name while losing its mtime.
    """
    keys = tmp_path / "keys"
    write_faucet_file(keys, "xrp-testnet-20260101T000000Z.json", "rOld", "sOld")
    write_faucet_file(keys, "xrp-testnet-20260926T000000Z.json", "rNew", "sNew")
    monkeypatch.setattr(xrp_send_tagged, "KEY_DIRECTORY", keys)
    assert [address for _, address, _ in saved_faucet_accounts()] == ["rNew", "rOld"]


def test_a_file_without_a_secret_is_skipped_not_half_used(tmp_path, monkeypatch):
    """A payload with an address but no secret cannot sign, so it is not offered."""
    keys = tmp_path / "keys"
    keys.mkdir(parents=True)
    (keys / "xrp-testnet-20260926T000001Z.json").write_text(json.dumps({"account": {"address": "rNoSecret"}}))
    monkeypatch.setattr(xrp_send_tagged, "KEY_DIRECTORY", keys)
    assert saved_faucet_accounts() == []


def test_unreadable_or_corrupt_files_are_skipped_not_fatal(tmp_path, monkeypatch):
    """One bad file must not hide the good ones next to it."""
    keys = tmp_path / "keys"
    keys.mkdir(parents=True)
    (keys / "xrp-testnet-20260926T000002Z.json").write_text("{ not json")
    write_faucet_file(keys, "xrp-testnet-20260926T000003Z.json", "rGood", "sGood")
    monkeypatch.setattr(xrp_send_tagged, "KEY_DIRECTORY", keys)
    assert [address for _, address, _ in saved_faucet_accounts()] == ["rGood"]


def test_signing_refusal_codes_are_matched_exactly_not_by_substring():
    """An earlier version tested `"ignInvalid" in str(status)`.

    That is a fragment of a GUESSED error code, and it would also match any
    other status containing those eight characters -- so a real failure could be
    reported as "this server will not sign for you", sending the operator to
    install a dependency they do not need. Exact membership instead.
    """
    assert isinstance(SIGNING_REFUSED, frozenset)
    assert "notSupported" in SIGNING_REFUSED
    assert "tesSUCCESS" not in SIGNING_REFUSED
    for code in SIGNING_REFUSED:
        assert code == code.strip() and " " not in code


@pytest.mark.parametrize("success", ["tesSUCCESS", "terQUEUED"])
def test_success_codes_are_not_treated_as_signing_refusals(success):
    assert success not in SIGNING_REFUSED


def write_real_faucet_file(directory: Path, name: str, address: str, secret: str) -> Path:
    """A faucet file in the shape the XRPL testnet faucet ACTUALLY returns.

    Distinct from write_faucet_file above, and the difference is the bug this
    pair of helpers exists to pin. That one writes `account.secret`, which is
    where I ASSUMED the secret lived. Dumped from the operator's own saved files
    2026-09-26 (keys and string lengths only, so no secret was displayed), the
    faucet writes:

        account.xAddress        str, len 47
        account.address         str, len 34
        account.classicAddress  str, len 34
        amount                  int
        transactionHash         str, len 64
        seed                    str, len 31     <- THE SECRET, at the TOP level

    So the secret is `payload.seed`, one level up from where the first version
    looked. saved_faucet_accounts() reported zero usable accounts and the script
    refused to send while two funded accounts sat in that directory -- and the
    whole suite stayed green, because every fixture wrote the shape the code
    already handled. A test built from a guess confirms the guess.
    """
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / name
    path.write_text(
        json.dumps(
            {
                "account": {
                    "xAddress": "X" * 47,
                    "address": address,
                    "classicAddress": address,
                },
                "amount": 100,
                "transactionHash": "f" * 64,
                "seed": secret,
            }
        )
    )
    return path


def test_the_secret_is_found_at_the_top_level_where_the_faucet_puts_it(tmp_path, monkeypatch):
    """The regression. Fails against the pre-fix lookup, which found nothing.

    This is the measured shape, not a paraphrase of it: `seed` at the top level
    and NO `secret` key anywhere. The old code read account.secret, then
    payload.secret, then account.seed -- none of which this file has.
    """
    keys = tmp_path / "keys"
    write_real_faucet_file(keys, "xrp-testnet-1.json", "r" + "A" * 33, "s" * 31)
    monkeypatch.setattr(xrp_send_tagged, "KEY_DIRECTORY", keys)

    found = saved_faucet_accounts()

    assert len(found) == 1, "the faucet's own shape must yield a usable account"
    _path, address, secret = found[0]
    assert address == "r" + "A" * 33
    assert secret == "s" * 31


def test_both_faucet_shapes_are_accepted_so_older_saved_files_still_sign(tmp_path, monkeypatch):
    """The candidate list is a UNION, not a replacement.

    Fixing the lookup by swapping one hardcoded key for another would have
    orphaned any file already saved under the older shape. Searching a named
    list is what makes both work, and that is the behavior worth pinning rather
    than the particular key that happens to match.
    """
    keys = tmp_path / "keys"
    write_faucet_file(keys, "xrp-testnet-1.json", "r" + "B" * 33, "old" * 10)
    write_real_faucet_file(keys, "xrp-testnet-2.json", "r" + "A" * 33, "s" * 31)
    monkeypatch.setattr(xrp_send_tagged, "KEY_DIRECTORY", keys)

    found = saved_faucet_accounts()

    assert len(found) == 2, "both the measured shape and the older one must sign"
    assert {secret for _p, _a, secret in found} == {"s" * 31, "old" * 10}


def test_a_matching_secret_key_name_is_reported_but_never_its_value(tmp_path, monkeypatch, capsys):
    """Rule 14 against rule: say which key matched, never what was in it.

    The key NAME is what made the original bug diagnosable and is safe to print;
    the value is the secret. Both halves are asserted, because a print statement
    added for diagnosability is exactly where a secret leaks.
    """
    keys = tmp_path / "keys"
    secret = "s" * 31
    write_real_faucet_file(keys, "xrp-testnet-1.json", "r" + "A" * 33, secret)
    monkeypatch.setattr(xrp_send_tagged, "KEY_DIRECTORY", keys)

    saved_faucet_accounts()
    out = capsys.readouterr().out

    assert "'seed'" in out, "the key the secret was found under must be named"
    assert secret not in out, "the secret's VALUE must never be printed"


def test_a_file_with_neither_address_nor_secret_says_so_rather_than_vanishing(
    tmp_path, monkeypatch, capsys
):
    """Rule 14: a skipped file must not read identically to a file never seen.

    Zero accounts and no explanation is the ambiguity the original bug hid
    inside -- the run said "saved faucet accounts: 0" with two usable files
    present, and nothing on screen distinguished that from an empty directory.
    """
    keys = tmp_path / "keys"
    keys.mkdir(parents=True)
    (keys / "xrp-testnet-1.json").write_text(json.dumps({"amount": 100}))
    monkeypatch.setattr(xrp_send_tagged, "KEY_DIRECTORY", keys)

    assert saved_faucet_accounts() == []
    assert "xrp-testnet-1.json" in capsys.readouterr().out


# xrpl-py is an OPTIONAL dependency (see derive_and_check's import note), so these
# SKIP rather than fail without it. A skip is honest here -- the suite is not
# claiming the guard works, it is saying it could not be checked -- which is why
# the skip reason names what went unchecked rather than just the missing module.
xrpl = pytest.importorskip(
    "xrpl.wallet", reason="xrpl-py absent, so the local-signing derivation guard is UNCHECKED"
)


def test_a_seed_that_derives_the_announced_address_is_accepted():
    """The happy path, against real key derivation rather than a stub.

    A stubbed Wallet would only prove the comparison operator works. The point
    of this guard is that OUR derivation agrees with the faucet's, so it is
    exercised against the real secp256k1/ed25519 path.
    """
    wallet = xrpl.Wallet.create()

    derived = xrp_send_tagged.derive_and_check(wallet.seed, wallet.classic_address)

    assert derived.classic_address == wallet.classic_address


def test_a_seed_for_a_different_account_refuses_rather_than_signing():
    """The guard, and the reason local signing is safe to add at all.

    Server-side `submit` sends the secret and Account separately and the server
    rejects a mismatch. Signing here, WE choose the account the transaction
    claims -- so a seed paired with the wrong address would sign a Payment from
    an account the dry run never displayed. The operator reads one address and a
    different one is debited.

    Not hypothetical: saved_faucet_accounts() reads the address and the secret
    from separate key names over two nesting levels, so nothing structurally
    guarantees they came from the same faucet file.
    """
    signing = xrpl.Wallet.create()
    announced = xrpl.Wallet.create()

    with pytest.raises(RuntimeError, match="REFUSING to sign"):
        xrp_send_tagged.derive_and_check(signing.seed, announced.classic_address)


def test_the_mismatch_message_names_both_addresses_and_never_the_seed():
    """Diagnosable without leaking. Both halves asserted.

    An error message is exactly where a secret leaks, because the impulse when
    debugging a key mismatch is to print the key. Addresses are public and are
    what the operator needs to see; the seed is never in the text.
    """
    signing = xrpl.Wallet.create()
    announced = xrpl.Wallet.create()

    with pytest.raises(RuntimeError) as caught:
        xrp_send_tagged.derive_and_check(signing.seed, announced.classic_address)

    message = str(caught.value)
    assert signing.classic_address in message
    assert announced.classic_address in message
    assert signing.seed not in message, "the seed must never reach an error message"


# --- reading the deposit target instead of typing it --------------------------

def swaps_db(tmp_path, rows):
    """A minimal swaps table. Built by hand rather than from SCHEMA because this
    function reads four columns and nothing else, and a full schema would imply
    it depends on more than it does."""
    path = tmp_path / "swaps.db"
    connection = sqlite3.connect(path)
    # expected_input_amount is part of this table because deposit_target_for_swap()
    # READS it -- the helper mirrors the columns the function under test selects,
    # rather than the minimum that made an earlier version of it pass. Three tests
    # broke when that column was added to the query, which is the helper doing its
    # job: a fixture narrower than the real schema hides exactly this.
    connection.executescript(
        "CREATE TABLE swaps (id TEXT PRIMARY KEY, from_asset TEXT, deposit_address TEXT, "
        "deposit_tag INTEGER, status TEXT, expected_input_amount REAL);"
    )
    connection.executemany(
        "INSERT INTO swaps (id, from_asset, deposit_address, deposit_tag, status) VALUES (?,?,?,?,?)",
        rows,
    )
    connection.commit()
    connection.close()
    return str(path)


XRP_ACCOUNT = "rBfM7je6e9Ca2cMvuRn7cr9xExFgDa5NGx"


def test_the_account_and_tag_come_from_the_swap_row(tmp_path):
    """Why this exists: a destination tag is a bare integer with NO checksum.

    A mistyped tag is not an error -- it is a payment credited to a different swap
    or to none at all, with the ledger recording that the sender paid exactly what
    they chose. The account has a checksum and would catch a typo; the tag has
    nothing standing behind it.

    Reading both from the row removes the transcription. It came from three
    separate commands in one session being pasted with a `<placeholder>` still in
    them, because the value had to travel by hand from a web page to a shell.
    """
    path = swaps_db(tmp_path, [("s_1", "XRP", XRP_ACCOUNT, 2, "awaiting_deposit")])

    assert deposit_target_for_swap(path, "s_1") == (XRP_ACCOUNT, 2, None)


def test_tag_zero_is_returned_rather_than_refused(tmp_path):
    """0 is a legal DestinationTag, so the check is `is None` and not truthiness.

    Third place in this tree where that distinction decides whether money is
    attributable -- the others are chains/xrp_payments.py and
    services/deposit_service.py. Same trap, same answer.
    """
    path = swaps_db(tmp_path, [("s_0", "XRP", XRP_ACCOUNT, 0, "awaiting_deposit")])

    assert deposit_target_for_swap(path, "s_0") == (XRP_ACCOUNT, 0, None)


def test_a_swap_with_no_tag_refuses_rather_than_sending_untagged(tmp_path):
    """An untagged payment to the shared account cannot be attributed to anyone."""
    path = swaps_db(tmp_path, [("s_n", "XRP", XRP_ACCOUNT, None, "awaiting_deposit")])

    with pytest.raises(SystemExit, match="has no deposit_tag"):
        deposit_target_for_swap(path, "s_n")


def test_a_non_xrp_swap_refuses(tmp_path):
    """Paying XRP into a GRC swap is money the terminal never credits.

    Worth its own guard because the swap id carries no asset, so nothing about
    `--swap s_abc123` tells the operator which chain it wants.
    """
    path = swaps_db(tmp_path, [("s_g", "GRC", "mSomeGridcoinAddress", None, "awaiting_deposit")])

    with pytest.raises(SystemExit, match="expects GRC, not XRP"):
        deposit_target_for_swap(path, "s_g")


def test_an_unknown_swap_refuses_and_says_which_database_it_looked_in(tmp_path):
    """The likely operator error is pointing at the WRONG database, not a typo.

    The server's default lives inside swap_terminal/, and SWAP_DB_PATH may point
    somewhere else entirely -- so "no such swap" without the path sends the reader
    hunting for a missing row instead of a missing database.
    """
    path = swaps_db(tmp_path, [("s_1", "XRP", XRP_ACCOUNT, 2, "awaiting_deposit")])

    with pytest.raises(SystemExit, match="no swap s_other"):
        deposit_target_for_swap(path, "s_other")
    with pytest.raises(SystemExit, match=str(tmp_path)):
        deposit_target_for_swap(path, "s_other")


def test_a_missing_database_says_so_rather_than_reporting_no_such_swap(tmp_path):
    """sqlite3.connect() CREATES a missing file, so without this check the reader
    would build an empty database and then report the swap as absent -- which reads
    as "wrong swap id" when it was "wrong database". Two very different fixes."""
    with pytest.raises(SystemExit, match="no database at"):
        deposit_target_for_swap(str(tmp_path / "does-not-exist.db"), "s_1")


# --- finding the swap, so no id is carried by hand ----------------------------

def pending_db(tmp_path, rows):
    path = tmp_path / "pending.db"
    connection = sqlite3.connect(path)
    connection.executescript(
        "CREATE TABLE swaps (id TEXT PRIMARY KEY, from_asset TEXT, status TEXT, "
        "deposit_tag INTEGER, expected_input_amount REAL, created_at TEXT);"
    )
    connection.executemany("INSERT INTO swaps VALUES (?,?,?,?,?,?)", rows)
    connection.commit()
    connection.close()
    return str(path)


def test_the_one_waiting_swap_is_found_without_being_named(tmp_path):
    """Why a lookup and not an argument.

    `--swap` already took the TAG out of the operator's hands, which is the value
    with no checksum behind it. But the swap ID still had to travel from a web page
    into a shell, and across one session FOUR commands were pasted with a
    `<placeholder>` still in them -- twice after I had said I would stop writing
    them, and two of those pastes put something into bash that should not have been
    there, one a wallet passphrase.

    The lesson is not "be more careful with placeholders". A value a human has to
    carry between two programs is a defect in the second program.
    """
    path = pending_db(tmp_path, [
        ("s_waiting", "XRP", "awaiting_deposit", 3, 1.0, "2026-09-26T01:00:00Z"),
        ("s_done", "XRP", "completed", 1, 1.0, "2026-09-25T01:00:00Z"),
    ])

    assert pending_xrp_swap(path) == "s_waiting"


def test_two_waiting_swaps_refuse_rather_than_picking_the_newest(tmp_path):
    """THE important half. Newest-wins would be the obvious convenience and is wrong.

    With two swaps waiting, choosing silently sends a payment to a tag the operator
    did not pick -- and on a shared account with no checksum on the tag, that money
    is attributed to the wrong swap and the ledger records that the sender paid
    exactly what they chose. Listing them costs one more command; guessing costs a
    deposit.
    """
    path = pending_db(tmp_path, [
        ("s_a", "XRP", "awaiting_deposit", 3, 1.0, "2026-09-26T01:00:00Z"),
        ("s_b", "XRP", "awaiting_deposit", 4, 5.0, "2026-09-26T02:00:00Z"),
    ])

    with pytest.raises(SystemExit) as caught:
        pending_xrp_swap(path)

    message = str(caught.value)
    assert "2 XRP swaps" in message
    # Both must be listed with the flag that selects them, or the refusal leaves
    # the operator no faster than before it.
    assert "--swap s_a" in message
    assert "--swap s_b" in message
    assert "tag 3" in message and "tag 4" in message


def test_no_waiting_swap_says_none_rather_than_erroring_obscurely(tmp_path):
    """Rule 14: (none) is a result. Nothing waiting is an ordinary state, not a fault."""
    path = pending_db(tmp_path, [("s_done", "XRP", "completed", 1, 1.0, "2026-09-25T01:00:00Z")])

    with pytest.raises(SystemExit, match="no XRP swap is awaiting a deposit"):
        pending_xrp_swap(path)


def test_a_non_xrp_swap_waiting_is_not_offered(tmp_path):
    """A GRC swap awaiting a deposit is waiting for GRC, and paying XRP into it is
    money this terminal never credits."""
    path = pending_db(tmp_path, [("s_grc", "GRC", "awaiting_deposit", None, 100.0, "2026-09-26T01:00:00Z")])

    with pytest.raises(SystemExit, match="no XRP swap is awaiting a deposit"):
        pending_xrp_swap(path)


def test_an_amount_the_swap_will_not_accept_is_refused(tmp_path):
    """Measured 2026-09-26: --amount 1 against a swap expecting 5 sent anyway.

    The tolerance check then did its job and halted the swap to 'under_review' --
    correctly, because crediting a wrong amount is what it exists to prevent. But
    the information needed to avoid that was printed on screen one line earlier,
    and the result is testnet XRP sitting against a swap nothing will advance and a
    reconciliation that needs a person.

    Refusing costs a retyped flag. Proceeding costs the manual fix.
    """
    path = swaps_db(tmp_path, [("s_5", "XRP", XRP_ACCOUNT, 1, "awaiting_deposit")])
    connection = sqlite3.connect(path)
    connection.execute("UPDATE swaps SET expected_input_amount = 5.0")
    connection.commit()
    connection.close()

    with pytest.raises(SystemExit, match=re.escape("expects 5.0 XRP and --amount says 1")):
        deposit_target_for_swap(path, "s_5", "1")


def test_the_matching_amount_is_accepted_including_a_different_spelling(tmp_path):
    """5 and 5.00 are the same number, and a string comparison would reject one.

    Decimal, not float: this is money, and `0.1 + 0.2 != 0.3` is the reason every
    amount in this tree goes through Decimal rather than binary floating point.
    """
    path = swaps_db(tmp_path, [("s_5", "XRP", XRP_ACCOUNT, 1, "awaiting_deposit")])
    connection = sqlite3.connect(path)
    connection.execute("UPDATE swaps SET expected_input_amount = 5.0")
    connection.commit()
    connection.close()

    assert deposit_target_for_swap(path, "s_5", "5") == (XRP_ACCOUNT, 1, 5.0)
    assert deposit_target_for_swap(path, "s_5", "5.00") == (XRP_ACCOUNT, 1, 5.0)


def test_no_amount_given_does_not_invent_a_mismatch(tmp_path):
    """The check is opt-in via --amount, so a caller that omits it is not blocked.

    Written because the obvious implementation compares unconditionally and then
    refuses every call that did not pass --amount, which would break the plain
    `--swap <id>` form entirely.
    """
    path = swaps_db(tmp_path, [("s_5", "XRP", XRP_ACCOUNT, 1, "awaiting_deposit")])
    connection = sqlite3.connect(path)
    connection.execute("UPDATE swaps SET expected_input_amount = 5.0")
    connection.commit()
    connection.close()

    assert deposit_target_for_swap(path, "s_5") == (XRP_ACCOUNT, 1, 5.0)


# --- where the amount comes from ----------------------------------------------
#
# resolve_amount() is the reason these are separate from the tests above: the
# account and the tag were already read from the row, and the amount was the one
# of the three a person still had to type. Every test here seeds the inputs
# directly, because the decision is a function rather than four lines inside
# main() (rule 10) and a function can be called with the case that matters.

def test_the_amount_comes_from_the_swap_row_when_none_is_given():
    """The whole point. --amount unpassed and a swap present -> the row decides."""
    target = xrp_send_tagged.DepositTarget(XRP_ACCOUNT, 7, 5.0)

    amount, source = xrp_send_tagged.resolve_amount("", target)

    assert amount == 5.0
    assert "expected_input_amount" in source, "the preview must say where the figure came from"


def test_an_explicit_amount_still_wins():
    """--amount is an override, not a suggestion. The mismatch check in
    deposit_target_for_swap() is what protects against it being wrong; this
    function does not second-guess it, because two places deciding one thing is
    rule 8's bug with a delay on it."""
    target = xrp_send_tagged.DepositTarget(XRP_ACCOUNT, 7, 5.0)

    amount, source = xrp_send_tagged.resolve_amount("2.5", target)

    assert (amount, source) == ("2.5", "--amount")


def test_no_swap_and_no_amount_falls_back_to_the_default():
    """The ad hoc form: two faucet accounts and a tag, with no swap involved."""
    amount, source = xrp_send_tagged.resolve_amount("", None)

    assert amount == xrp_send_tagged.DEFAULT_AMOUNT_XRP
    assert "default" in source


def test_a_swap_with_no_expected_amount_refuses_rather_than_using_the_default():
    """The branch that would have been the bug.

    Falling back to DEFAULT_AMOUNT_XRP here would send 10 XRP at a swap whose
    expectation is unknown -- a guaranteed tolerance halt, which is exactly what
    reading the amount from the row exists to prevent. Reached only against a
    database older than db.py's `expected_input_amount REAL NOT NULL`, so the
    message says so instead of blaming the operator.
    """
    target = xrp_send_tagged.DepositTarget(XRP_ACCOUNT, 7, None)

    with pytest.raises(SystemExit, match="no expected_input_amount"):
        xrp_send_tagged.resolve_amount("", target)


def test_an_expected_amount_of_zero_is_not_read_as_absent():
    """`is None`, never truthiness. The same defect as tag 0, one column over.

    0.0 is a nonsense swap amount and the tolerance check would reject the send --
    but it must reject it as "the swap expects 0", not become a silent 10 through a
    falsy test. A guard written `if not target.expected_amount` passes every other
    test in this file and fails only this one.
    """
    target = xrp_send_tagged.DepositTarget(XRP_ACCOUNT, 7, 0.0)

    amount, _source = xrp_send_tagged.resolve_amount("", target)

    assert amount == 0.0, "0.0 must come back as itself, not be replaced by the default"
