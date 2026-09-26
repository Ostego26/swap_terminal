"""The testnet tagged-payment fixture: what it reads, and what it refuses.

Role: test (pure functions and a filesystem read; no socket, nothing submitted)
Reads: xrp_send_tagged.py
Writes: a temp key directory
Can move funds: no. The script it tests moves TESTNET XRP only, and only with
      --send; these tests never call that path.
Mainnet-safe: yes
"""

import json
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
