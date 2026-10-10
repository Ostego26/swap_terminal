"""The SOL payout path: every guard, disabled one at a time, against seeded responses.

Role: test (the real guards and the real adapter; the transport is a stub,
      nothing else is)
Reads: swap_terminal/chains/solana_signing.py, chains/solana.py,
       services/quote_service.py
Writes: a throwaway keypair file into pytest's tmp_path, and nothing else. No
      keypair in this repository is read, opened or referenced -- every one
      below is generated in-process from a seed this file makes up.
Can move funds: no. `SolanaAdapter.call` is replaced by a table of seeded
      responses, so no test here can reach a cluster even by accident, and
      "nothing was broadcast" is asserted on the recorded call list rather than
      on the absence of an exception.
Mainnet-safe: yes

WHAT THESE TESTS ESTABLISH, AND WHAT THEY DO NOT. Stated first, because a green
suite is the thing most likely to be mistaken for evidence a payout path works.

THEY ESTABLISH, against seeded inputs:

  - a mainnet-beta genesis hash refuses, INCLUDING in preview mode, and refuses
    before any balance is read -- asserted on the recorded call list
  - so do testnet, an unrecognized hash, and a missing one: "I cannot identify
    this cluster" is not "this is devnet"
  - the url is not consulted: a devnet-looking url with mainnet's genesis still
    refuses, and a mainnet-looking url with devnet's genesis does not
  - previewing is the DEFAULT: the exact two-positional call
    services/payout_service.py makes is refused with the preview in the message
    and nothing broadcast
  - the arming token is matched exactly, and an armed call with no keypair path
    still refuses
  - the keypair file is refused when it is missing, oversized, not JSON, the
    wrong length, not bytes, or INTERNALLY INCONSISTENT (its own public half
    disagreeing with the key derived from its secret half)
  - a keypair for another account refuses BEFORE anything is signed
  - THE SECRET REACHES NO LOG RECORD AT DEBUG, NO EXCEPTION MESSAGE, NO RETURN
    VALUE AND NO repr -- not whole, not truncated, not hex, not base58, not as
    the JSON array it is stored as, and not as a sha256 of itself
  - a payout below the rent-exempt minimum to an account that does not exist
    refuses before signing, and the same floor refuses at QUOTE time
  - the signed bytes are parsed back and compared against the plan before
    broadcast, so a serializer that lied would be caught
  - the blockhash is fetched AFTER every refusal and immediately before signing
  - a broadcast transaction that the cluster rejects RAISES rather than
    returning a signature

THEY ESTABLISH NOTHING ABOUT A REAL CLUSTER. Nothing here was broadcast.
api.devnet.solana.com answers 403 at this container's proxy -- re-measured
2026-10-02, `CONNECT tunnel failed, response 403` -- so the payout path is
verified against seeded responses and a byte-level cross-check of the
serialization (tests/test_solana_transaction.py), and the BROADCAST is verified
against nothing. That is the honest boundary and closing it is the operator's
run.

WHY EACH GUARD GETS ITS OWN TEST rather than one test of a refused send: a
guard with no test that fails when it is DISABLED is not a guard.
"""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import sqlite3
from time import time

import base58
import pytest
import valid_addresses
from chains import solana_signing as signing
from chains.registry import why_cannot_pay_out
from chains.solana import SolanaAdapter, SolanaRPCError
from chains.solana_payout_keypair import missing_keypair_refusal, payout_keypair_is_present
from chains.solana_signing import (
    CONFIRM_SOL_SEND,
    KEYPAIR_PATH_VARIABLE,
    PayoutKeypair,
    SolanaClusterRefused,
    SolanaHeadroomRefused,
    SolanaKeypairRefused,
    SolanaRentRefused,
    SolanaSendNotArmed,
    SolanaSigningRefused,
    SolanaSplSendRefused,
    SolanaWireMismatch,
    below_new_account_floor,
    derive_and_check,
    describe_keypair_file,
    devnet_genesis_hash,
    keypair_path_from_environment,
    load_payout_keypair,
    require_destination_rent,
    require_devnet,
    require_lamport_headroom,
    require_send_confirmation,
    sign_message,
    signed_transfer_wire,
)
from chains.solana_transaction import parse_transfer_transaction
from config import Config
from conftest import root_entry_point
from db import SCHEMA, apply_migrations, connect_db, db_session, dict_factory
from network_target import GENESIS_HASHES
from services import quote_service
from services.payout_service import broadcast_payout, process_pending_payouts
from services.pricing import IDS, _cache
from services.quote_service import create_quote
from test_solana_adapter import RecordedCalls, SeededTransportAdapter
from workers.common import get_config_dict

nacl_signing = pytest.importorskip(
    "nacl.signing", reason="PyNaCl absent, so signing, derivation and the leak proofs are UNCHECKED"
)

DEVNET = devnet_genesis_hash()
MAINNET_BETA = "5eykt4UsFv8P8NJdTREpY1vzqKqZKvdpKuc147dw2N9d"

#: A destination, from the shared fixture module rather than typed here. It is
#: derived from a phrase and is on-curve, which chains/solana.py's
#: validate_address() requires -- an off-curve address is a Program Derived
#: Address and native SOL sent to one is unrecoverable.
DESTINATION = valid_addresses.SOL_PAYOUT

#: Figures from the operator's own devnet run on 2026-09-30, so the seeded
#: responses are the shapes and the magnitudes a real cluster returned:
#: getMinimumBalanceForRentExemption(0) answered 650240.
RENT_FLOOR = 650_240
ONE_SOL = 1_000_000_000


def throwaway_keypair(tmp_path, name="payout.json"):
    """A keypair generated in this process and written to pytest's tmp_path.

    NEVER THE OPERATOR'S. Their keypair is at a path this file does not name,
    does not read and does not need: the derivation guard, the file checks and
    the leak proofs all work on a key whose secret this test made up seconds
    earlier. A test that needed the real key would be a test nobody could run
    safely.

    Written in solana-keygen's format -- a JSON array of 64 integers, 32 bytes
    of seed then the 32-byte public key -- because that is what
    load_payout_keypair() must accept, and inventing a friendlier format here
    would be testing something the operator does not have.
    """
    key = nacl_signing.SigningKey.generate()
    seed = bytes(key)
    public = bytes(key.verify_key)
    path = tmp_path / name
    path.write_text(json.dumps(list(seed + public)))
    return path, seed, base58.b58encode(public).decode("ascii")


def payout_adapter(responses: dict, **kwargs) -> SeededTransportAdapter:
    """A real SolanaAdapter whose ONLY stubbed member is the transport.

    A SIBLING OF tests/test_solana_adapter.py::make_adapter() AND NOT A COPY OF
    IT (rule 8), and the difference is what the payout path needs: a response
    may be a LIST, which is consumed one entry per call, because the settlement
    loop asks getSignatureStatuses repeatedly and has to see a different answer
    each time -- unknown, then confirmed. The read tests have no such need and
    that helper answers each method with one value. Each docstring names the
    other.

    `adapter.calls` is the ORDERED list of (method, params), which is how
    "nothing was broadcast" and "the blockhash was fetched last" are asserted:
    on what was done, not on which exception came back.

    THE CLASS IS IMPORTED FROM THAT SIBLING AND NOT RE-DECLARED HERE. What the
    two helpers disagree about is how a response is answered; what they agree on
    is that the adapter they hand back carries `calls`. Declaring that twice is
    one shape with two spellings, which is rule 8 at the smallest scale it comes
    in, and the two copies would have been identical on the day they were
    written. SeededTransportAdapter adds no behavior -- two annotations and
    nothing else, see its docstring -- so this is still the shipped adapter with
    only its transport replaced.
    """
    adapter = SeededTransportAdapter(url=kwargs.pop("url", "http://seeded.invalid"), **kwargs)
    calls: RecordedCalls = []
    sequences = {method: list(value) for method, value in responses.items() if isinstance(value, list)}

    def fake_call(method, *params):
        calls.append((method, params))
        if method not in responses:
            raise AssertionError(f"the adapter called {method}, which this test did not seed")
        if method in sequences:
            queue = sequences[method]
            return queue.pop(0) if len(queue) > 1 else queue[0]
        value = responses[method]
        return value(*params) if callable(value) else value

    adapter.call = fake_call
    adapter.calls = calls
    return adapter


def methods(adapter) -> list[str]:
    return [method for method, _params in adapter.calls]


#: The blockhash every seeded cluster hands back. Thirty-two 0x05 bytes, so it
#: is a real 32-byte base58 value and not a string that happens to look like
#: one -- compile_transfer_message() refuses anything that is not.
SEEDED_BLOCKHASH = base58.b58encode(bytes([5]) * 32).decode("ascii")

#: What a cluster says about a transaction it has finalized without error.
SETTLED = {"value": [{"confirmationStatus": "finalized", "err": None}]}


def echo_signature(blob, _options):
    """What sendTransaction returns: the transaction's own signature.

    Computed by DECODING the base64 the adapter actually sent, which makes the
    stub itself a check -- a blob that is not valid base64, or not a native
    transfer, fails here rather than being accepted by a stub that returns a
    canned string. A real cluster derives the same value the same way, because a
    signature is a property of the bytes and not something a node assigns.
    """
    return parse_transfer_transaction(base64.b64decode(blob))["signature"]


def ready_adapter(*, balance=5 * ONE_SOL, destination_exists=True, genesis=None, statuses=None, **kwargs):
    """An adapter seeded so that every read SUCCEEDS and only the arming is left.

    ONE TABLE FOR EVERY ARMED TEST, rather than a copy per test (rule 8 applies
    to fixtures too -- six copies of a response table drift, and the drift shows
    up as a test that passes for the wrong reason). What each test varies, it
    varies by keyword: the cluster, the balance, whether the destination exists,
    and what the settlement poll says.
    """
    return payout_adapter(
        {
            "getGenesisHash": genesis or DEVNET,
            "getAccountInfo": {"value": {"lamports": 1}} if destination_exists else {"value": None},
            "getMinimumBalanceForRentExemption": RENT_FLOOR,
            "getBalance": {"value": balance},
            "getLatestBlockhash": {"value": {"blockhash": SEEDED_BLOCKHASH, "lastValidBlockHeight": 1234}},
            "sendTransaction": echo_signature,
            "getSignatureStatuses": statuses if statuses is not None else SETTLED,
        },
        hot_wallet=kwargs.pop("hot_wallet", valid_addresses.SOL_DEPOSIT_ACCOUNT),
        **kwargs,
    )


# =============================================================================
# guard 1: the cluster, by its own genesis hash and never by the url
# =============================================================================


def test_GENESIS_HASHES_really_carries_mainnet_beta_and_devnet():
    """MEASURED off the table rather than assumed, because the refusal rests on it.

    The brief for this work said to verify this rather than rely on it, and the
    verification is the point: devnet_genesis_hash() DERIVES the hash it accepts
    from this table, so a table with no DEVNET row would make the payout path
    refuse everything (safe) and a table with no MAINNET-BETA row would make
    solana_cluster() describe real money as "unrecognized" (not safe).

    Both are present. The labels are asserted by substring because the mainnet
    row carries a warning after its name.
    """
    labels = {genesis: label.upper() for genesis, label in GENESIS_HASHES.items()}
    assert MAINNET_BETA in labels, "the mainnet-beta genesis hash must be in the table to be refused BY NAME"
    assert "MAINNET-BETA" in labels[MAINNET_BETA]
    assert "REAL MONEY" in labels[MAINNET_BETA]
    devnet_rows = [genesis for genesis, label in labels.items() if label.startswith("DEVNET")]
    assert len(devnet_rows) == 1, f"exactly one DEVNET row is needed; found {devnet_rows}"
    assert devnet_rows[0] == DEVNET == "EtWTRABZaYq6iMfeYKouRu166VU2xqa1wcaWoxPkrZBG", (
        "this is the hash the operator's own 2026-09-30 devnet run reported, recorded in "
        "chains/solana_units.py's rent section"
    )
    assert len(GENESIS_HASHES) == 3, "mainnet-beta, devnet, testnet -- a fourth row needs a decision, not a default"


def test_the_devnet_hash_is_derived_from_the_table_and_not_a_second_copy():
    """MUTATION: rename the table's DEVNET label and this fails rather than silently
    accepting a hash nobody checked."""
    assert DEVNET in GENESIS_HASHES
    assert GENESIS_HASHES[DEVNET].startswith("DEVNET")


def test_a_table_with_two_devnet_rows_refuses_rather_than_picking_one(monkeypatch):
    monkeypatch.setitem(GENESIS_HASHES, "SomeOtherHashEntirely1111111111111111111111", "DEVNET (a fork)")
    with pytest.raises(SolanaClusterRefused, match="exactly one"):
        devnet_genesis_hash()


def test_devnet_is_accepted_and_the_line_says_which_cluster_it_is():
    line = require_devnet(DEVNET, "https://api.devnet.solana.com")
    assert DEVNET in line
    assert "DEVNET" in line
    assert "api.devnet.solana.com" in line


@pytest.mark.parametrize(
    ("genesis", "why"),
    [
        (MAINNET_BETA, "MAINNET-BETA"),
        ("4uhcVJyU9pJkvQyS88uRDiswHXSCkY3zQawwpjk2NsNY", "TESTNET"),
        ("3rMSEyTyFiLGVfVjHcsgZGHMqmvRnNQXCD5rgpWFJdYQ", "UNRECOGNIZED"),
    ],
)
def test_every_cluster_that_is_not_devnet_refuses_and_is_named(genesis, why):
    """Three refusals, and the third is the one a config check would pass.

    An unrecognized genesis is what a local validator reports AND what a private
    fork of mainnet would report, which is why network_target.UNRECOGNIZED_CLUSTER
    is never rendered as "not mainnet".
    """
    with pytest.raises(SolanaClusterRefused) as exc:
        require_devnet(genesis, "https://api.devnet.solana.com")
    message = str(exc.value)
    assert why in message
    assert "NOT devnet" in message
    assert "NOTHING was signed" in message


@pytest.mark.parametrize("missing", ["", "   ", None, 0])
def test_a_missing_genesis_hash_refuses_because_unread_is_not_safe(missing):
    with pytest.raises(SolanaClusterRefused, match="did not report a genesis hash"):
        require_devnet(missing, "https://api.devnet.solana.com")


def test_the_url_is_not_evidence_in_either_direction():
    """A hostname resolves to whatever DNS says today; a genesis hash cannot be mistaken."""
    with pytest.raises(SolanaClusterRefused):
        require_devnet(MAINNET_BETA, "https://api.devnet.solana.com")
    assert require_devnet(DEVNET, "https://api.mainnet-beta.solana.com")


def test_a_mainnet_cluster_refuses_IN_PREVIEW_MODE_before_any_balance_is_read():
    """Wanting a preview is not a reason to let this path talk to mainnet.

    Asserted on the call list: getBalance must never happen, which proves the
    order of the guards rather than just the fact of the refusal.
    """
    adapter = ready_adapter(genesis=MAINNET_BETA)
    with pytest.raises(SolanaClusterRefused):
        adapter.preview_payout(DESTINATION, 1.0)
    assert methods(adapter) == ["getGenesisHash"], (
        "the cluster check must come before every other read on this path"
    )


# =============================================================================
# guard 2: the arming token
# =============================================================================


def test_the_arming_token_is_matched_exactly_not_by_truthiness(tmp_path):
    """`True` is the spelling this deliberately does not accept.

    The path argument is a real tmp_path rather than a literal under /tmp: the
    function does not open it, but a hard-coded temp path in a test is ruff's
    S108 and a suppression here would be a claim nobody needed to make.
    """
    any_path = str(tmp_path / "payout.json")
    for wrong in ("", "yes", "true", CONFIRM_SOL_SEND.lower(), CONFIRM_SOL_SEND[:-1], CONFIRM_SOL_SEND + "!"):
        with pytest.raises(SolanaSendNotArmed):
            require_send_confirmation(wrong, any_path)


def test_the_exact_token_with_a_keypair_path_is_the_only_accepted_combination(tmp_path):
    assert require_send_confirmation(CONFIRM_SOL_SEND, str(tmp_path / "payout.json")) is None


def test_armed_with_no_keypair_path_still_refuses_and_names_the_variable():
    with pytest.raises(SolanaSendNotArmed, match=KEYPAIR_PATH_VARIABLE):
        require_send_confirmation(CONFIRM_SOL_SEND, "")


def test_the_keypair_path_comes_from_the_environment_at_call_time(monkeypatch):
    monkeypatch.delenv(KEYPAIR_PATH_VARIABLE, raising=False)
    assert keypair_path_from_environment() == ""
    monkeypatch.setenv(KEYPAIR_PATH_VARIABLE, "  /keys/payout.json  ")
    assert keypair_path_from_environment() == "/keys/payout.json"
    # And a seeded mapping is honored, which is how the rest of this file avoids
    # touching the real environment at all.
    assert keypair_path_from_environment({KEYPAIR_PATH_VARIABLE: "/other.json"}) == "/other.json"


def test_the_adapter_stores_no_key_and_no_key_path():
    """Asserted over the instance's own attributes rather than by reading the source.

    If a keypair, a seed or a key path is ever added as adapter state, this
    fails. chains/solana.py's promise is that the secret's whole lifetime is one
    call into chains/solana_signing.py.
    """
    for name, value in vars(ready_adapter()).items():
        assert "seed" not in name.lower()
        assert "secret" not in name.lower()
        assert "keypair" not in name.lower()
        assert not (isinstance(value, bytes) and len(value) in (32, 64))


# =============================================================================
# guard 3: the keypair file
# =============================================================================


def test_a_well_formed_keypair_file_loads_and_derives_its_own_public_key(tmp_path):
    path, _seed, public = throwaway_keypair(tmp_path)
    keypair = load_payout_keypair(str(path))
    assert keypair.public_key == public


def test_a_missing_file_refuses_and_names_the_variable_rather_than_the_contents(tmp_path):
    with pytest.raises(SolanaKeypairRefused, match="does not exist"):
        load_payout_keypair(str(tmp_path / "nothing-here.json"))


def test_a_file_too_large_to_be_a_keypair_is_refused_BY_SIZE_before_being_read(tmp_path):
    """A path pointing at a log, a database or a wallet dump must not be slurped."""
    fat = tmp_path / "not-a-key.json"
    fat.write_text("[" + ",".join(["0"] * 5000) + "]")
    with pytest.raises(SolanaKeypairRefused, match="REFUSED BY SIZE"):
        load_payout_keypair(str(fat))


def test_a_file_that_is_not_json_is_refused_without_quoting_itself(tmp_path):
    path = tmp_path / "junk.json"
    path.write_text("-----BEGIN SOMETHING PRIVATE-----\nnot json at all\n")
    with pytest.raises(SolanaKeypairRefused) as exc:
        load_payout_keypair(str(path))
    assert "BEGIN SOMETHING PRIVATE" not in str(exc.value), (
        "a refusal must not echo the file it refused -- the file might be a key"
    )


def test_a_32_integer_array_is_refused_because_it_carries_no_public_half_to_check(tmp_path):
    path = tmp_path / "seed-only.json"
    path.write_text(json.dumps(list(range(32))))
    with pytest.raises(SolanaKeypairRefused, match="exactly 64 integers"):
        load_payout_keypair(str(path))


def test_an_array_whose_elements_are_not_bytes_is_refused_without_reporting_them(tmp_path):
    path = tmp_path / "floats.json"
    path.write_text(json.dumps([0.5] * 64))
    with pytest.raises(SolanaKeypairRefused) as exc:
        load_payout_keypair(str(path))
    assert "not every element is a byte" in str(exc.value)
    assert "0.5" not in str(exc.value)


def test_a_keypair_file_whose_two_halves_DISAGREE_is_refused(tmp_path):
    """THE CHECK THAT CATCHES A CORRUPT OR HAND-ASSEMBLED FILE.

    solana-keygen writes seed || public key, so the public half can be
    re-derived and compared. A file whose halves disagree would otherwise sign
    with one key while this process announced another -- and the message names
    both PUBLIC keys so an operator can see which file they have.

    MUTATION: delete the comparison in load_payout_keypair() and this fails.
    """
    path, seed, _public = throwaway_keypair(tmp_path)
    other = bytes(nacl_signing.SigningKey.generate().verify_key)
    path.write_text(json.dumps(list(seed + other)))
    with pytest.raises(SolanaKeypairRefused) as exc:
        load_payout_keypair(str(path))
    message = str(exc.value)
    assert "internally inconsistent" in message
    assert base58.b58encode(other).decode() in message, "both PUBLIC keys belong in the message"


# =============================================================================
# guard 4: the derivation, which is the account-substitution guard
# =============================================================================


def test_a_keypair_for_another_account_refuses_and_names_both_public_keys(tmp_path):
    """On Solana the fee payer is simply the first account key.

    So a keypair paired with the wrong announced address signs a perfectly valid
    transfer debiting an account the preview never displayed. Nothing in the
    runtime stops that; only this does.
    """
    _path, _seed, public = throwaway_keypair(tmp_path)
    keypair = PayoutKeypair(bytes(nacl_signing.SigningKey.generate()), public)
    with pytest.raises(SolanaKeypairRefused) as exc:
        derive_and_check(keypair, valid_addresses.SOL_DEPOSIT_ACCOUNT)
    message = str(exc.value)
    assert public in message
    assert valid_addresses.SOL_DEPOSIT_ACCOUNT in message
    assert "REFUSING to sign" in message


def test_a_matching_keypair_returns_a_line_for_the_preview(tmp_path):
    _path, _seed, public = throwaway_keypair(tmp_path)
    keypair = PayoutKeypair(bytes(nacl_signing.SigningKey.generate()), public)
    assert "MATCHES" in derive_and_check(keypair, public)


def test_no_announced_payer_at_all_refuses_rather_than_signing_for_whatever_it_derives():
    keypair = PayoutKeypair(bytes(nacl_signing.SigningKey.generate()), DESTINATION)
    with pytest.raises(SolanaKeypairRefused, match="announced no payer"):
        derive_and_check(keypair, "")


# =============================================================================
# THE SECRET: no log record, no exception, no return value, no repr
# =============================================================================


def every_spelling_of(seed: bytes) -> dict[str, str]:
    """The ways 32 bytes of secret could appear in text, each as its own assertion.

    A TRUNCATION AND A HASH COUNT AS LEAKS, which is the argument
    tests/test_grc_address_proof.py makes about a pasted WIF: a hash of a
    32-byte seed with known structure is the seed with an extra step, and a
    prefix narrows the search space. So each of these is checked separately and
    the failure says which spelling got out.
    """
    return {
        "hex": seed.hex(),
        "hex prefix": seed.hex()[:16],
        "base58": base58.b58encode(seed).decode("ascii"),
        "base58 prefix": base58.b58encode(seed).decode("ascii")[:12],
        "json array": json.dumps(list(seed)),
        "json array prefix": json.dumps(list(seed))[:24],
        "sha256": hashlib.sha256(seed).hexdigest(),
        "latin-1 bytes": seed.decode("latin-1"),
        # BASE64 WAS MISSING FROM THIS TABLE UNTIL 2026-10-02, and it is the one
        # encoding the payout path actually produces: signed_transfer_wire()
        # returns a `base64` field and chains/solana.py passes exactly that
        # string to sendTransaction, so base64 is what gets printed, logged and
        # recorded in the call list this file searches. The seed is not in the
        # wire transaction and so cannot reach it that way -- which is a reason
        # to believe, not a check (rule 17), and the check costs two lines.
        "base64": base64.b64encode(seed).decode("ascii"),
        "base64 prefix": base64.b64encode(seed).decode("ascii")[:16],
    }


def assert_no_secret_in(text: str, seed: bytes, where: str) -> None:
    for spelling, value in every_spelling_of(seed).items():
        assert value not in text, f"the secret leaked into {where} as its {spelling}"


def all_log_text(caplog) -> str:
    """Every rendered message PLUS every raw argument, as one string.

    Both halves, for the reason tests/test_grc_address_proof.py gives:
    `logger.warning("x=%s", value)` leaves `value` in `record.args` whether or
    not the message is ever formatted, so a test that reads only
    `record.getMessage()` can miss a leak a real handler would print.
    """
    parts = []
    for record in caplog.records:
        parts.append(record.getMessage())
        parts.append(repr(record.args))
    return " ".join(parts)


def test_the_keypair_object_cannot_print_its_own_secret(tmp_path):
    path, seed, public = throwaway_keypair(tmp_path)
    keypair = load_payout_keypair(str(path))
    for rendering, where in (
        (repr(keypair), "repr()"),
        (str(keypair), "str()"),
        (f"{keypair}", "an f-string"),
        (f"{keypair!r}", "an f-string with !r"),
    ):
        assert_no_secret_in(rendering, seed, where)
        assert public in rendering, "the PUBLIC key belongs in the repr; it is what identifies the key"
    assert "NEVER PRINTED" in repr(keypair)


def test_the_keypair_has_no_dict_for_a_generic_dumper_to_find(tmp_path):
    """__slots__, so `vars(keypair)` raises instead of handing back the seed.

    That is the route a logging helper, a JSON encoder or a debugger takes, and
    a dataclass -- the obvious spelling of this class -- would have left it wide
    open along with an auto-generated __repr__.
    """
    path, seed, _public = throwaway_keypair(tmp_path)
    keypair = load_payout_keypair(str(path))
    with pytest.raises(TypeError):
        vars(keypair)
    assert_no_secret_in(repr(keypair.__slots__), seed, "__slots__")


def test_the_secret_is_in_no_log_record_at_DEBUG_and_no_return_value(tmp_path, caplog, capsys, monkeypatch):
    """THE WHOLE PATH, at the most verbose level, with every output searched.

    DEBUG rather than INFO because a leak that only appears at DEBUG is still a
    leak: the operator turns DEBUG on precisely when something is wrong, which
    is when the log gets pasted into a chat.
    """
    path, seed, public = throwaway_keypair(tmp_path)
    monkeypatch.setenv(KEYPAIR_PATH_VARIABLE, str(path))
    adapter = ready_adapter(hot_wallet=public)

    with caplog.at_level(logging.DEBUG):
        signature = adapter.send_to_address(DESTINATION, 0.01, confirm_send=CONFIRM_SOL_SEND)

    assert_no_secret_in(all_log_text(caplog), seed, "a log record at DEBUG")
    printed = capsys.readouterr()
    assert_no_secret_in(printed.out, seed, "stdout")
    assert_no_secret_in(printed.err, seed, "stderr")
    assert_no_secret_in(signature, seed, "the returned signature")
    assert_no_secret_in(repr(adapter.calls), seed, "the RPC calls that were made")


def test_the_secret_is_in_no_exception_message_from_any_guard(tmp_path, monkeypatch):
    """Every refusal downstream of the key being read, with its message searched.

    A key-mismatch error is exactly where a secret leaks, because the debugging
    impulse on a mismatch is to print the key.
    """
    path, seed, public = throwaway_keypair(tmp_path)
    monkeypatch.setenv(KEYPAIR_PATH_VARIABLE, str(path))

    # 1. the keypair derives an account nobody announced
    adapter = ready_adapter(hot_wallet=valid_addresses.SOL_DEPOSIT_ACCOUNT)
    with pytest.raises(SolanaKeypairRefused) as mismatch:
        adapter.send_to_address(DESTINATION, 0.01, confirm_send=CONFIRM_SOL_SEND)
    assert_no_secret_in(str(mismatch.value), seed, "the derivation refusal")

    # 2. the file's halves disagree
    path.write_text(json.dumps(list(seed + bytes(32))))
    with pytest.raises(SolanaKeypairRefused) as inconsistent:
        load_payout_keypair(str(path))
    assert_no_secret_in(str(inconsistent.value), seed, "the inconsistent-file refusal")

    # 3. a signature that does not verify
    keypair = PayoutKeypair(seed, public)
    with pytest.raises(SolanaSigningRefused) as bad:
        # A keypair whose public_key is somebody else's: the signature is real
        # and verifies against nothing, which sign_message() checks before
        # returning rather than letting a cluster find out.
        sign_message(b"a message", PayoutKeypair(seed, DESTINATION))
    assert_no_secret_in(str(bad.value), seed, "the signature-verification refusal")
    assert keypair.public_key == public


# =============================================================================
# guard 5: the rent floor, at payout time and at quote time
# =============================================================================


def test_an_existing_destination_has_no_rent_floor_at_all():
    line = require_destination_rent(True, 1, RENT_FLOOR)
    assert "already exists" in line


def test_a_payout_below_the_floor_to_a_NEW_account_refuses_before_signing():
    """It would not arrive small -- it would not arrive.

    MUTATION: delete the require_destination_rent() call from preview_payout()
    and this fails, as does the adapter-level test below it.
    """
    with pytest.raises(SolanaRentRefused) as exc:
        require_destination_rent(False, RENT_FLOOR - 1, RENT_FLOOR)
    message = str(exc.value)
    assert str(RENT_FLOOR) in message
    assert "does NOT exist" in message
    assert "NOTHING was\nsigned" in message or "NOTHING was signed" in message


def test_a_payout_at_the_floor_exactly_is_allowed():
    """The comparison is `<`, not `<=`: the minimum is a minimum, not an exclusive bound."""
    assert "at or above" in require_destination_rent(False, RENT_FLOOR, RENT_FLOOR)


def test_the_floor_comparison_is_one_function_shared_with_the_quote_path():
    assert below_new_account_floor(RENT_FLOOR - 1, RENT_FLOOR) is True
    assert below_new_account_floor(RENT_FLOOR, RENT_FLOOR) is False


def test_the_adapter_refuses_a_small_payout_to_a_new_account_and_asks_the_CHAIN_for_the_floor():
    """The whole preview, with the destination absent from the cluster.

    The floor is whatever getMinimumBalanceForRentExemption answers -- seeded
    here as the 650,240 the operator's devnet run returned -- and NOT a constant:
    chains/solana_units.py records this repository's reference figures being
    stale on every cluster for weeks.
    """
    adapter = ready_adapter(destination_exists=False)
    with pytest.raises(SolanaRentRefused):
        adapter.preview_payout(DESTINATION, 0.0001)
    assert "getMinimumBalanceForRentExemption" in methods(adapter)
    assert (("getMinimumBalanceForRentExemption", (0,)) in adapter.calls), (
        "the floor must be asked for a 0-BYTE account; 165 bytes is a token account and a bigger number"
    )


def test_a_payout_above_the_floor_to_a_new_account_is_fine_and_says_so():
    adapter = ready_adapter(destination_exists=False, balance=5 * ONE_SOL)
    plan = adapter.preview_payout(DESTINATION, 1.0)
    assert plan["destination_exists"] is False
    assert "DOES NOT EXIST -- this transfer would CREATE it" in plan["description"]
    assert "will accept the creation" in plan["description"]


# =============================================================================
# guard 6: the payer's balance
# =============================================================================


def test_the_headroom_arithmetic_is_integer_lamports_throughout():
    line = require_lamport_headroom(ONE_SOL, 100, 5_000, RENT_FLOOR)
    assert f"{ONE_SOL - 100 - 5_000}" in line
    assert "fits" in line


def test_a_payout_the_payer_cannot_cover_refuses_with_the_shortfall_in_lamports():
    with pytest.raises(SolanaHeadroomRefused) as exc:
        require_lamport_headroom(RENT_FLOOR + 5_000, 1, 5_000, RENT_FLOOR)
    assert "short by 1 lamports" in str(exc.value)


def test_the_adapter_refuses_when_the_payer_would_drop_below_its_own_rent_minimum():
    """MUTATION: drop `retain` to 0 in preview_payout() and this fails."""
    adapter = ready_adapter(balance=ONE_SOL)
    with pytest.raises(SolanaHeadroomRefused):
        adapter.preview_payout(DESTINATION, 0.9999999)


# =============================================================================
# the preview: what it prints, and that it is the DEFAULT
# =============================================================================


def test_the_preview_prints_every_figure_an_operator_needs_before_arming():
    adapter = ready_adapter()
    plan = adapter.preview_payout(DESTINATION, 0.5)
    description = plan["description"]
    for expected in (
        "SOL PAYOUT PREVIEW (nothing signed, nothing broadcast)",
        valid_addresses.SOL_DEPOSIT_ACCOUNT,
        DESTINATION,
        "500000000 lamports",
        "5000 lamports  <- per SIGNATURE",
        "EXISTS on this cluster",
        "DEVNET",
        "headroom",
        "blockhash     NOT fetched for a preview",
    ):
        assert expected in description, f"the preview must say {expected!r}"
    assert plan["signed"] is False
    assert plan["broadcast"] is False
    assert "getLatestBlockhash" not in methods(adapter), "a preview must not consume a signing nonce"


def test_an_spl_adapter_refuses_the_payout_path_with_NO_network_call_at_all():
    """Only the native transfer is built, and paying a token holder in lamports
    because the serializer could do that is the silent failure this stops."""
    adapter = payout_adapter({}, mint="So11111111111111111111111111111111111111112", hot_wallet=valid_addresses.SOL_DEPOSIT_ACCOUNT)
    with pytest.raises(SolanaSplSendRefused, match="NATIVE SOL"):
        adapter.preview_payout(DESTINATION, 1.0)
    assert adapter.calls == [], "the SPL refusal must come before any read"


def test_a_payout_to_the_payer_itself_is_refused_before_any_read():
    adapter = ready_adapter()
    with pytest.raises(SolanaRPCError, match="destination is the payer itself"):
        adapter.preview_payout(valid_addresses.SOL_DEPOSIT_ACCOUNT, 1.0)
    assert adapter.calls == []


def test_no_hot_wallet_means_no_announced_payer_and_no_cluster_read():
    adapter = payout_adapter({}, hot_wallet="")
    with pytest.raises(SolanaRPCError, match="SOL_HOT_WALLET is unset"):
        adapter.preview_payout(DESTINATION, 1.0)
    assert adapter.calls == []


# =============================================================================
# the armed path: signing, the artifact check, the broadcast, the settlement
# =============================================================================


def armed(tmp_path, monkeypatch, **kwargs):
    path, seed, public = throwaway_keypair(tmp_path)
    monkeypatch.setenv(KEYPAIR_PATH_VARIABLE, str(path))
    return ready_adapter(hot_wallet=public, **kwargs), seed, public


def test_an_armed_send_broadcasts_base64_with_preflight_ON_and_returns_the_signature(tmp_path, monkeypatch):
    """The armed happy path, asserting on WHAT WAS SENT.

    The transaction handed to sendTransaction is decoded here and compared
    against the plan -- which is the behavioral verification the principle asks
    for: not "the code passes the right destination", which is a claim about the
    call site, but "the bytes that reached the transport carry it".
    """
    adapter, _seed, public = armed(tmp_path, monkeypatch)
    signature = adapter.send_to_address(DESTINATION, 0.25, confirm_send=CONFIRM_SOL_SEND)

    sent = [params for method, params in adapter.calls if method == "sendTransaction"]
    assert len(sent) == 1, "exactly one broadcast"
    blob, options = sent[0]
    assert options == {"encoding": "base64", "skipPreflight": False, "preflightCommitment": "finalized"}, (
        "preflight ON and both commitments the same -- see chains/solana.BROADCAST_COMMITMENT"
    )
    parsed = parse_transfer_transaction(base64.b64decode(blob))
    assert parsed["payer"] == public
    assert parsed["destination"] == DESTINATION
    assert parsed["lamports"] == 250_000_000
    assert signature == parsed["signature"]


def test_the_blockhash_is_fetched_AFTER_every_refusal_and_right_before_the_broadcast(tmp_path, monkeypatch):
    """A nonce read earlier would be stale by the time an operator armed the send.

    MUTATION: move the latest_blockhash() call into preview_payout() and this
    fails on the ORDER, which no test of the return value would catch.
    """
    adapter, _seed, _public = armed(tmp_path, monkeypatch)
    adapter.send_to_address(DESTINATION, 0.25, confirm_send=CONFIRM_SOL_SEND)
    order = methods(adapter)
    assert order.index("getGenesisHash") < order.index("getLatestBlockhash")
    assert order.index("getBalance") < order.index("getLatestBlockhash")
    assert order.index("getLatestBlockhash") == order.index("sendTransaction") - 1


def test_a_serializer_that_disagrees_with_the_plan_is_caught_before_broadcast(tmp_path, monkeypatch):
    """THE ARTIFACT CHECK, with a lying compiler.

    chains/solana_signing.signed_transfer_wire() parses the signed bytes back
    and compares them against the plan. Here the message compiler is replaced
    with one that pays a different destination -- every other guard passes,
    because every other guard inspects the INPUTS.

    MUTATION: delete the comparison and this test broadcasts the wrong
    destination and fails on the missing exception.
    """
    adapter, _seed, _public = armed(tmp_path, monkeypatch)
    real = signing.compile_transfer_message

    def lying(payer, destination, lamports, blockhash):
        return real(payer, valid_addresses.SOL_DEPOSIT_ACCOUNT, lamports, blockhash)

    monkeypatch.setattr(signing, "compile_transfer_message", lying)
    with pytest.raises(SolanaWireMismatch) as exc:
        adapter.send_to_address(DESTINATION, 0.25, confirm_send=CONFIRM_SOL_SEND)
    assert "does NOT match the plan" in str(exc.value)
    assert "sendTransaction" not in methods(adapter)


def test_a_cluster_that_returns_a_different_signature_refuses_and_says_it_may_have_sent(tmp_path, monkeypatch):
    adapter, _seed, _public = armed(tmp_path, monkeypatch)
    adapter_responses_signature = base58.b58encode(bytes([1]) * 64).decode()
    original = adapter.call

    def intercept(method, *params):
        if method == "sendTransaction":
            adapter.calls.append((method, params))
            return adapter_responses_signature
        return original(method, *params)

    adapter.call = intercept
    with pytest.raises(SolanaRPCError, match="NOT PROOF NOTHING WAS SENT"):
        adapter.send_to_address(DESTINATION, 0.25, confirm_send=CONFIRM_SOL_SEND)


def test_a_transaction_the_cluster_REJECTS_raises_rather_than_returning_a_signature(tmp_path, monkeypatch):
    """rule 13: a stop that cannot prove it worked is not a stop.

    sendTransaction returning a signature means a node accepted the bytes for
    forwarding. A transfer that then fails on chain has a real signature and did
    nothing, and recording it as a completed payout tells a customer they were
    paid when they were not.

    MUTATION: return the signature straight out of _sign_and_broadcast() and
    this fails.
    """
    path, _seed, public = throwaway_keypair(tmp_path)
    monkeypatch.setenv(KEYPAIR_PATH_VARIABLE, str(path))
    adapter = ready_adapter(
        hot_wallet=public,
        statuses={"value": [{"confirmationStatus": "finalized", "err": {"InstructionError": [0, "Custom"]}}]},
    )
    with pytest.raises(SolanaRPCError) as exc:
        adapter.send_to_address(DESTINATION, 0.25, confirm_send=CONFIRM_SOL_SEND)
    message = str(exc.value)
    assert "REJECTED it" in message
    assert "InstructionError" in message
    assert "No value was delivered" in message


def test_a_transaction_that_settles_on_a_later_poll_is_waited_for(tmp_path, monkeypatch):
    """The settlement loop, with a sequence: unknown first, finalized second.

    This is why tests/test_solana_payout.py has its own adapter stub -- see
    payout_adapter()'s docstring.
    """
    monkeypatch.setattr("chains.solana.CONFIRMATION_POLL_SECONDS", 0.0)
    path, _seed, public = throwaway_keypair(tmp_path)
    monkeypatch.setenv(KEYPAIR_PATH_VARIABLE, str(path))
    adapter = ready_adapter(hot_wallet=public, statuses=[{"value": [None]}, SETTLED])
    signature = adapter.send_to_address(DESTINATION, 0.25, confirm_send=CONFIRM_SOL_SEND)
    assert methods(adapter).count("getSignatureStatuses") == 2
    assert signature


def test_a_transaction_that_never_settles_raises_and_NEVER_says_it_did_not_send(tmp_path, monkeypatch):
    monkeypatch.setattr("chains.solana.CONFIRMATION_POLL_SECONDS", 0.0)
    monkeypatch.setattr("chains.solana.CONFIRMATION_DEADLINE_SECONDS", 0.0)
    path, _seed, public = throwaway_keypair(tmp_path)
    monkeypatch.setenv(KEYPAIR_PATH_VARIABLE, str(path))
    adapter = ready_adapter(hot_wallet=public, statuses={"value": [None]})
    with pytest.raises(SolanaRPCError) as exc:
        adapter.send_to_address(DESTINATION, 0.25, confirm_send=CONFIRM_SOL_SEND)
    message = str(exc.value)
    assert "NOT PROOF IT DID NOT LAND" in message
    assert "do NOT retry blind" in message
    assert "µfn" in message, "rule 6: a timing this system reports is in microfortnights"


def test_an_unarmed_armed_looking_token_signs_nothing_even_with_a_valid_keypair(tmp_path, monkeypatch):
    """The keypair file is present and valid; only the token is wrong.

    MUTATION: compare the token with `in` instead of `==` and this fails on
    the near-miss.
    """
    adapter, _seed, _public = armed(tmp_path, monkeypatch)
    with pytest.raises(SolanaSendNotArmed):
        adapter.send_to_address(DESTINATION, 0.25, confirm_send=CONFIRM_SOL_SEND[:-1])
    assert "getLatestBlockhash" not in methods(adapter)
    assert "sendTransaction" not in methods(adapter)


def test_signed_transfer_wire_needs_no_environment_when_it_is_handed_one(tmp_path):
    """The seeded-mapping path, so no test has to mutate the real environment."""
    path, _seed, public = throwaway_keypair(tmp_path)
    plan = {"payer": public, "destination": DESTINATION, "base_units": 1_000_000}
    blockhash = base58.b58encode(bytes([6]) * 32).decode()
    signed = signed_transfer_wire(plan, blockhash, CONFIRM_SOL_SEND, {KEYPAIR_PATH_VARIABLE: str(path)})
    parsed = parse_transfer_transaction(signed["wire"])
    assert parsed["lamports"] == 1_000_000
    assert parsed["recent_blockhash"] == blockhash
    assert signed["payer"] == public


# =============================================================================
# THE SAME FLOOR, ONE STAGE EARLIER: at QUOTE time (operator, 2026-10-02:
# "refuse at quote too")
# =============================================================================


def seeded_prices():
    """Every asset priced at $100, so a rate is 1.0 and the arithmetic is readable.

    The same mechanism tests/test_allowed_pairs_are_serviceable.py uses: the
    pricing module's in-process cache is filled directly, so create_quote()
    makes no HTTP request and the only external call left on the path is the one
    this section is about.
    """
    now = time()
    _cache.update(
        {
            "raw": {
                identifier: {
                    "usd": 100.0,
                    "usd_market_cap": 5_000_000_000.0,
                    "usd_24h_vol": 200_000_000.0,
                    "usd_24h_change": 1.0,
                    "last_updated_at": 1_790_717_713,
                }
                for identifier in IDS.values()
            },
            "prices": None,
            "context": None,
            "source": "seeded",
            "fetched_at": now,
            "expires_at": now + 999,
        }
    )


def quote_config(tmp_path):
    """A config dict that ALLOWS a *->SOL pair, which the live one does not.

    ESTABLISHED, NOT ASSUMED: Config.ALLOWED_PAIRS names no pair whose TO asset
    is SOL (asserted below), so validate_pair() refuses every *->SOL quote
    before the floor check can be reached. The gate is therefore unreachable on
    the live configuration and is tested against a config that enables the pair
    -- which is exactly the configuration the operator would create when they
    enable one, and the moment the gate starts mattering.
    """
    database = tmp_path / "quotes.db"
    connection = sqlite3.connect(database)
    connection.row_factory = dict_factory
    connection.executescript(SCHEMA)
    connection.commit()
    connection.close()
    config = dict(get_config_dict())
    config["DB_PATH"] = str(database)
    config["ALLOWED_PAIRS"] = [*config["ALLOWED_PAIRS"], ("GRC", "SOL")]
    config["SOL_NETWORK_FEE_RESERVE"] = 0.000005
    return config, database


def test_the_SOL_quote_gate_IS_REACHABLE_NOW_THAT_A_PAIR_PAYS_OUT_IN_SOL():
    """The pair this gate was built ahead of exists, so the gate is live. That is the news.

    THIS TEST WAS test_no_live_pair_pays_out_in_SOL_so_the_quote_gate_cannot_fire_today
    and it asserted the list was EMPTY. Its own docstring said why it was written
    early: "the moment somebody enables ('GRC', 'SOL') is exactly the moment nobody
    would remember to add it". That moment was 2026-10-03, the operator enabled
    three such pairs, and the gate was there waiting -- which is the mechanism
    working, so the assertion flips rather than the gate changing (rule 2).

    WHAT IS STRONGER. "No pair pays out in SOL" is a config fact one line changes,
    and once it is false the test says nothing at all. What is worth holding is that
    every pair which DOES pay out in SOL goes through the floor gate -- because the
    failure the gate prevents is a customer quoted a payout the network will not
    deliver, and that failure is reachable now in a way it was not before.

    The gate's behavior is asserted by
    test_a_SOL_payout_below_the_network_floor_IS_REFUSED_AT_QUOTE_TIME below, which
    seeds rows and runs the real create_quote(). This one asserts the gate is
    REACHED: that there is at least one pair whose quotes pass through it, and that
    SOL's own reserve exists so the quote gets that far at all. A gate nothing
    reaches is the dead-code half of rule 9, and a gate that fires on a pair nobody
    enabled is what this test used to describe.
    """
    sol_output = sorted(pair for pair in Config.ALLOWED_PAIRS if pair[1] == "SOL")
    assert sol_output, (
        "no pair pays out in SOL any more, so require_deliverable_sol_payout() is unreachable from "
        "any enabled pair. If SOL was deliberately disabled as a payout asset, this test and the "
        "gate's reachability go together -- the gate itself still earns its place for the next time"
    )
    assert hasattr(Config, "SOL_NETWORK_FEE_RESERVE"), (
        f"{sol_output} pay out in SOL and SOL_NETWORK_FEE_RESERVE does not exist, so every one of "
        f"those quotes refuses at get_network_fee_reserve() and never reaches the floor gate at all"
    )


def test_a_SOL_payout_below_the_network_floor_IS_REFUSED_AT_QUOTE_TIME(tmp_path, monkeypatch):
    """No quote, with a reason a customer can act on, and no row written.

    MUTATION: delete the require_deliverable_sol_payout() call from
    create_quote() and this fails -- and the quote row appears, which the second
    half asserts.
    """
    seeded_prices()
    config, database = quote_config(tmp_path)
    monkeypatch.setitem(quote_service._RENT_FLOOR_CACHE, "clear", (0.0, 0))
    quote_service._RENT_FLOOR_CACHE.clear()
    adapters = {"SOL": ready_adapter()}

    # $100 per coin both sides, so 0.0005 GRC prices to ~0.0005 SOL, which is
    # 500,000 lamports -- under the 650,240 the cluster reported.
    with db_session(str(database)) as db, pytest.raises(ValueError) as exc:
        create_quote(db, config, "GRC", "SOL", 0.0005, adapters=adapters)

    message = str(exc.value)
    assert "No quote:" in message, "the house shape for a refused quote in this file"
    assert "too small to deliver" in message
    assert "0.00065024" in message, "the floor, in SOL, as a decimal a person can read and type"
    assert "Deposit more" in message
    for internal in ("SOL_PAYOUT_KEYPAIR_PATH", "rent-exempt", "lamports", "genesis", "SOL_HOT_WALLET", "ALLOWED_PAIRS"):
        assert internal not in message, (
            f"a customer reads this: {internal!r} is operator-facing text, and commit 6ae5838 stripped "
            f"exactly this kind of sentence off the customer page"
        )
    with db_session(str(database)) as db:
        assert db.execute("SELECT COUNT(*) AS n FROM quotes").fetchone()["n"] == 0, "nothing was written"


def test_a_SOL_payout_above_the_floor_quotes_normally(tmp_path):
    """The gate is strictly conservative: above the floor it refuses nothing.

    Which is the property that makes it safe to apply at quote time, where the
    destination is unknown -- it can never reject a swap that would have worked.
    """
    seeded_prices()
    config, database = quote_config(tmp_path)
    quote_service._RENT_FLOOR_CACHE.clear()
    with db_session(str(database)) as db:
        quote = create_quote(db, config, "GRC", "SOL", 1.0, adapters={"SOL": ready_adapter()})
    assert quote["to_asset"] == "SOL"
    assert quote["output_amount_estimate"] > 0.00065024


def test_the_quote_path_asks_the_CLUSTER_for_the_floor_and_then_caches_it(tmp_path):
    """One RPC per cache window, not one per quote.

    The figure is a cluster parameter -- chains/solana_units.py records the last
    time it changed and by how much -- so asking once per window is right, and a
    hardcoded number is what that section is a monument to.
    """
    seeded_prices()
    config, database = quote_config(tmp_path)
    quote_service._RENT_FLOOR_CACHE.clear()
    adapter = ready_adapter()
    with db_session(str(database)) as db:
        create_quote(db, config, "GRC", "SOL", 1.0, adapters={"SOL": adapter})
        create_quote(db, config, "GRC", "SOL", 2.0, adapters={"SOL": adapter})
    assert methods(adapter).count("getMinimumBalanceForRentExemption") == 1, (
        "the second quote must read the cached floor"
    )
    assert ("getMinimumBalanceForRentExemption", (0,)) in adapter.calls, "0 bytes, not 165"


def test_an_expired_cache_asks_again_rather_than_trusting_an_old_answer(tmp_path):
    """A cluster parameter can change under a long-lived process (SIMD-0437 moved it twice)."""
    seeded_prices()
    quote_service._RENT_FLOOR_CACHE.clear()
    adapter = ready_adapter()
    assert quote_service.new_account_floor_lamports(adapter, now=0.0) == RENT_FLOOR
    assert quote_service.new_account_floor_lamports(adapter, now=0.0) == RENT_FLOOR
    assert methods(adapter).count("getMinimumBalanceForRentExemption") == 1
    assert quote_service.new_account_floor_lamports(adapter, now=quote_service.RENT_FLOOR_CACHE_SECONDS + 1) == RENT_FLOOR
    assert methods(adapter).count("getMinimumBalanceForRentExemption") == 2


def test_the_cache_is_keyed_BY_ENDPOINT_so_two_clusters_cannot_share_an_answer():
    """Devnet and mainnet-beta have answered different figures during a rollout."""
    quote_service._RENT_FLOOR_CACHE.clear()
    devnet = ready_adapter(url="http://devnet.invalid")
    other = payout_adapter({"getMinimumBalanceForRentExemption": 890_880}, url="http://other.invalid")
    assert quote_service.new_account_floor_lamports(devnet) == RENT_FLOOR
    assert quote_service.new_account_floor_lamports(other) == 890_880


def test_a_quote_with_no_SOL_adapter_REFUSES_rather_than_skipping_the_check(tmp_path):
    """A gate that can be silently bypassed is not a gate (rule 19).

    A quote for a payout this process cannot ask about is also a quote
    create_swap() would refuse for the same reason one step later, which is the
    round trip the operator's instruction exists to remove.
    """
    seeded_prices()
    config, database = quote_config(tmp_path)
    with db_session(str(database)) as db, pytest.raises(ValueError, match="cannot reach the Solana network"):
        create_quote(db, config, "GRC", "SOL", 1.0, adapters={})


def test_an_SPL_configured_adapter_is_SKIPPED_at_quote_time_rather_than_priced_wrong(tmp_path):
    """The SPL rent question is a different and larger number, and nobody has decided who pays it.

    165 bytes of token account came back at 1,488,440 lamports on the operator's
    devnet run against 650,240 for a 0-byte account. Quoting an SPL payout
    against the native floor would be the wrong figure in the forgiving
    direction, so the native-only check steps aside -- and chains/solana.py
    refuses an SPL send outright, so no quote this passes becomes an SPL payout.
    """
    seeded_prices()
    config, database = quote_config(tmp_path)
    spl = payout_adapter({}, mint="So11111111111111111111111111111111111111112")
    with db_session(str(database)) as db:
        quote = create_quote(db, config, "GRC", "SOL", 0.0005, adapters={"SOL": spl})
    assert quote["output_amount_estimate"] > 0
    assert spl.calls == [], "the SPL skip must cost no RPC call"


def test_a_non_SOL_payout_never_touches_the_solana_floor_at_all(tmp_path):
    """GRC payouts have no rent question, so the check returns before any lookup."""
    seeded_prices()
    config, database = quote_config(tmp_path)
    adapter = ready_adapter()
    with db_session(str(database)) as db:
        create_quote(db, config, "SOL", "GRC", 1.0, adapters={"SOL": adapter})
    assert "getMinimumBalanceForRentExemption" not in methods(adapter)


# =============================================================================
# the key file's own mode, reported and not refused
# =============================================================================


def test_the_preview_says_whether_the_key_file_is_readable_by_anybody_else(tmp_path):
    """REPORTS, DOES NOT REFUSE, and the line is what an operator acts on.

    solana-keygen writes 0600. A key readable by group or other is a key to
    treat as published -- docs/key_exposure_runbook.md's subject -- and the
    remedy is chmod, not blocking a payout the operator asked for.
    """
    path, _seed, _public = throwaway_keypair(tmp_path)
    path.chmod(0o600)
    assert "owner only" in describe_keypair_file(str(path))
    path.chmod(0o644)
    exposed = describe_keypair_file(str(path))
    assert "GROUP OR OTHER CAN READ" in exposed
    assert "chmod 600" in exposed
    assert "0644" in exposed


def test_a_key_file_that_cannot_be_read_says_so_rather_than_printing_nothing(tmp_path):
    """Rule 14: never let an empty result print nothing."""
    line = describe_keypair_file(str(tmp_path / "absent.json"))
    assert "COULD NOT BE READ" in line


# =============================================================================
# THE THIRD CALLER OF create_quote(), WHICH WAS DISCARDING ITS ADAPTERS
# =============================================================================


def teller_entry():
    """operator_panel.py, the root entry point, through the one copy of the loader.

    By file location rather than by import, because it is a rule 10 entry point at the
    project root and the suite puts swap_terminal/ on sys.path, not the root.

    THE REASON THE OLD DOCSTRING GAVE FOR COPYING IT IS GONE, and it was the right reason
    for the wrong destination. It said: "The helper is a copy of that file's _entry() on
    purpose: importing a test module from another test module to share four lines would make
    the two files' collection order matter." True, and that is why the survivor is
    conftest.py -- pytest imports a conftest BEFORE collecting anything beside it, so
    `from conftest import root_entry_point` depends on no other test module and on no
    collection order. `from test_operator_panel import _entry` would have.

    A DISTINCT MODULE NAME, DELIBERATELY. This loads the same file as
    test_operator_panel.py::_entry() and wants an independent module object, which is why
    the helper takes `module_name` at all rather than always using the path's stem.

    WHAT THIS COPY MEASURED AND THE SHARED HELPER NOW ACTS ON, kept because it is the one
    fact that made the merge better than any of the five (rule 17: measured, not assumed --
    the first version of this comment claimed the opposite and was wrong):

        spec_from_file_location("x", "/nonexistent/operator_panel.py")
          -> ModuleSpec(name='x', loader=<SourceFileLoader>, origin=...)
        spec_from_file_location("y", "/tmp")           # a directory
          -> None

    A path that does not exist still produces a perfectly good spec, so `spec is None` never
    fires for a MOVED entry point -- which is the realistic failure for a rule 10 file named
    by path. root_entry_point() therefore checks the file exists FIRST; the two None guards
    all five copies argued about are for the case where the path is not loadable as a source
    module at all.
    """
    return root_entry_point("operator_panel.py", "operator_panel_entry_sol")


def test_the_teller_pane_PASSES_its_adapters_so_it_cannot_disagree_with_the_web_form(tmp_path, monkeypatch):
    """THE DEFECT THIS PINS, found 2026-10-02 while reviewing the surviving work.

    operator_panel.answer_a_teller_quote() read
    `session, config, _adapters = _teller_db_and_config()` -- building the
    adapters and throwing them away -- and then called create_quote() without
    them. services/quote_service.require_deliverable_sol_payout() REFUSES when
    it has no SOL adapter rather than skipping the check (rule 19: a gate that
    can be silently bypassed is not a gate), so the teller pane answered
    "cannot reach the Solana network right now" for a quote the web form
    priced normally. That is the second-opinion failure
    answer_a_teller_quote()'s own docstring forbids in its last paragraph, and
    rule 8's bug with a delay on it: two call sites of one function, disagreeing.

    IT COULD NOT FIRE ON THE LIVE CONFIGURATION -- Config.ALLOWED_PAIRS names no
    pair whose TO asset is SOL (asserted by
    test_no_live_pair_pays_out_in_SOL_so_the_quote_gate_cannot_fire_today), so
    validate_pair() refuses first. It would have fired on the day somebody
    enabled one, which is the day nobody would look here.

    MUTATION, AND IT IS THE CALL-SITE ONE: drop `adapters=adapters` from
    operator_panel.py's create_quote() call and this fails with
    "cannot reach the Solana network" -- the gate itself is untouched and still
    correct, which is precisely why a test of the gate alone could never catch it.
    """
    seeded_prices()
    config, database = quote_config(tmp_path)
    quote_service._RENT_FLOOR_CACHE.clear()
    entry = teller_entry()

    adapter = ready_adapter()
    monkeypatch.setattr(
        entry,
        "_teller_db_and_config",
        lambda: (db_session(str(database)), config, {"SOL": adapter}),
    )
    answer, status = entry.answer_a_teller_quote({"from_asset": "GRC", "to_asset": "SOL", "amount": 1.0})

    assert status == 200
    assert answer["ok"] is True, (
        f"the teller pane refused a SOL quote the web form prices: {answer.get('error')!r} -- "
        f"which means it is not passing the adapters create_quote() needs"
    )
    assert answer["quote"]["to_asset"] == "SOL"
    # And it asked the CHAIN for the floor rather than a constant, through the
    # adapter this pane built -- the proof that the real dict arrived.
    assert ("getMinimumBalanceForRentExemption", (0,)) in adapter.calls


def test_the_teller_pane_still_refuses_a_SOL_quote_below_the_network_floor(tmp_path, monkeypatch):
    """Passing the adapters must not have turned the gate OFF on this path.

    The mutation the test above catches is "the adapters are missing"; this one
    catches "the adapters are passed and the floor stopped applying". Same
    pane, same wiring, an amount under the cluster's rent-exempt minimum, and
    the customer-facing sentence comes back as the pane's error.
    """
    seeded_prices()
    config, database = quote_config(tmp_path)
    quote_service._RENT_FLOOR_CACHE.clear()
    entry = teller_entry()
    monkeypatch.setattr(
        entry,
        "_teller_db_and_config",
        lambda: (db_session(str(database)), config, {"SOL": ready_adapter()}),
    )
    answer, status = entry.answer_a_teller_quote({"from_asset": "GRC", "to_asset": "SOL", "amount": 0.0005})

    assert status == 200
    assert answer["ok"] is False
    assert "too small to deliver" in answer["error"]
    # Rule 16's customer-facing line: it names no internal setting, on this
    # route as well as on the web one.
    for internal in ("SOL_PAYOUT_KEYPAIR_PATH", "SOL_NETWORK_FEE_RESERVE", "rent_exempt_minimum", "lamport"):
        assert internal not in answer["error"], f"the refusal names an internal setting: {internal}"


# =============================================================================
# THE ARMING, AND THE WIRING THAT MAKES IT REACHABLE (2026-10-03)
# =============================================================================
#
# Operator, 2026-10-03, verbatim: "whoa we have to be able to swap TO SOL too".
#
# WHAT WAS MEASURED BEFORE THIS SECTION EXISTED, in this checkout, with the real
# modules:
#
#   chains/solana.py                  can_spend = False, HARDCODED, with a comment
#                                     saying flipping it was the operator's call.
#   services/payout_service.py        broadcast_payout() fell SOL through to
#                                     `send_to_address(address, amount)` -- two
#                                     positional arguments -- so every SOL payout
#                                     raised SolanaSendNotArmed no matter what the
#                                     host had exported. The payout path was
#                                     unreachable code from the worker's side.
#
# Both are changed. can_spend is DERIVED from
# chains/solana_payout_keypair.payout_keypair_is_present(), and broadcast_payout()
# passes confirm_send=CONFIRM_SOL_SEND for SOL. What these tests hold is the pair of
# directions, because only the pair is informative:
#
#   path UNSET -> REFUSES   the default in every checkout and every test run
#                           (tests/conftest.py pops the variable at import so the
#                           suite cannot inherit a different posture from the shell
#                           that started it), and the refusal must NAME the variable
#                           so an operator can act on it from a page (rule 14).
#   path SET   -> PERMITS   so the mechanism is not merely a differently-worded
#                           refusal. A guard that refused in both states would pass
#                           the first assertion forever and nobody would notice the
#                           payout path was dead -- which is the exact property
#                           tests/test_allowed_pairs_are_serviceable.py::
#                           test_the_XRP_PAYOUT_BLOCKER_IS_NOT_THE_RESERVE was
#                           rewritten to hold, one chain over.
#
# AND NOTHING HERE REACHES A CLUSTER. Every adapter below is a real SolanaAdapter
# whose `call` is a seeded table, so "nothing was broadcast" is asserted on the
# recorded call list. No transaction built by this path has reached any cluster from
# any environment, ever; re-measured 2026-10-03, api.devnet.solana.com answers 403 at
# this container's proxy.

#: A KEYPAIR PATH THAT IS NOT A PATH, for every test that needs the variable only to
#: be PRESENT. Deliberately not a filename: it has no separator, no extension and a
#: shouting name, so a stack trace containing it is unambiguous and any code that
#: tried to open it would fail on a missing file rather than reading something real.
#:
#: chains/solana_payout_keypair.payout_keypair_is_present() reads bool() of the
#: variable and never opens the file, which is the whole design of that module, so
#: every capability test below stops before any filesystem call. The tests that need
#: a keypair that actually derives an account use throwaway_keypair() above.
SENTINEL_KEYPAIR_PATH = "SENTINEL-NOT-A-REAL-KEYPAIR-PATH-DO-NOT-OPEN"

#: The config process_pending_payouts() needs for a GRC -> SOL swap. SOL is not in
#: services/payout_service.WALLET_UNLOCK_ASSETS (that set is {"GRC"}), so no wallet
#: passphrase is involved and payout_unlock_context() is a no-op for this asset.
SOL_PAYOUT_CONFIG = {
    "GRC_MIN_CONFIRMATIONS": 6,
    "SOL_MIN_CONFIRMATIONS": 1,
    "AMOUNT_TOLERANCE_PCT": 0.01,
}


class RecordingAdapter:
    """Records exactly how send_to_address() was called, and sends nothing.

    HERE RATHER THAN A REAL ADAPTER because the claim under test is about the CALL
    SITE -- which keywords services/payout_service.broadcast_payout() chooses for an
    asset -- and a real adapter would answer that question through seven guards and a
    seeded cluster. The guards have their own tests above; this one is about the
    dispatch, which is rule 10's "the decision is a function, callable with seeded
    inputs".
    """

    asset = "STUB"
    can_spend = True
    payout_refusal = ""

    def __init__(self):
        self.calls = []

    def send_to_address(self, address, amount, **kwargs):
        self.calls.append((address, amount, kwargs))
        return "stub-signature"


def _unarmed(monkeypatch):
    """Guarantee the keypair path is absent from THIS process for this test.

    tests/conftest.py already pops it at import, so this is belt and braces -- and
    it is here anyway because a test that depends on another file's import-time side
    effect reads as a test that happens to pass.
    """
    monkeypatch.delenv(KEYPAIR_PATH_VARIABLE, raising=False)


def _seed_one_pending_sol_swap(db_path: str, payout_address: str, amount: float, swap_id: str = "s_sol") -> None:
    """One GRC -> SOL swap in `payout_pending`, the state the payout worker acts on.

    A SIBLING OF tests/test_xrp_payout_wiring.py::_seed_one_pending_xrp_swap AND NOT
    A COPY OF ITS VALUES (rule 8 -- the shape is shared because the schema is, the
    assets and the amounts are what differ). `apply_migrations` is run because
    workers/payout_worker.py runs it once at startup and
    idx_payouts_one_live_per_swap is what makes a second live payout row impossible;
    a test that skipped it would be measuring a database the worker never sees.

    THE AMOUNT IS A PARAMETER because the rent floor is real: a payout to an account
    that does not exist must clear getMinimumBalanceForRentExemption, and seeding a
    dust amount would make every test here die on the rent guard instead of on the
    thing it is named for.
    """
    conn = connect_db(db_path)
    conn.executescript(SCHEMA)
    now = "2026-10-03T00:00:00+00:00"
    conn.execute(
        "INSERT INTO quotes (id, from_asset, to_asset, input_amount, quoted_rate, fee_bps, "
        "network_fee_reserve, output_amount_estimate, expires_at, created_at) "
        "VALUES ('q_sol', 'GRC', 'SOL', 1000.0, 0.0002, 150, 0.0, ?, ?, ?)",
        (amount, now, now),
    )
    conn.execute(
        """
        INSERT INTO swaps (
            id, quote_id, from_asset, to_asset, deposit_address, payout_address,
            expected_input_amount, actual_input_amount, quoted_rate, fee_bps,
            network_fee_reserve, output_amount_estimate, status, min_confirmations,
            deposit_txid, payout_txid, created_at, updated_at, credited_at,
            completed_at, expires_at, failed_reason
        ) VALUES (?, 'q_sol', 'GRC', 'SOL', ?, ?,
                  1000.0, 1000.0, 0.0002, 150, 0.0, ?, 'payout_pending', 6,
                  'grc_txid', NULL, ?, ?, ?, NULL, ?, NULL)
        """,
        (swap_id, valid_addresses.GRC_PAYOUT, payout_address, amount, now, now, now, now),
    )
    conn.commit()
    apply_migrations(conn)
    conn.close()


# --- the capability, in both directions --------------------------------------


def test_the_default_is_no_keypair_path_and_therefore_no_payout_capability(monkeypatch):
    """UNSET is the default, and the refusal has to name what to export.

    THE DEFAULT IS THE WHOLE SAFETY PROPERTY. With SOL_PAYOUT_KEYPAIR_PATH unset --
    every checkout, every test run, and every host where the operator has not made
    the custody decision -- can_spend is False, so
    chains/registry.why_cannot_pay_out() refuses SOL as a payout destination and
    services/swap_service.create_swap() refuses a swap whose payout leg is SOL. That
    is the only stage at which nothing has been taken from a customer.

    BOTH VARIABLES ARE ASSERTED, and naming one would be the mistake
    tests/test_allowed_pairs_are_serviceable.py::
    test_the_XRP_PAYOUT_BLOCKER_IS_NOT_THE_RESERVE is named after: a host with the
    keypair and no SOL_HOT_WALLET is armed and still cannot pay, because
    chains/solana_signing.derive_and_check() refuses to sign for an account the plan
    never announced.

    MUTATION: make payout_keypair_is_present() return True unconditionally and this
    fails on can_spend; drop either variable name from missing_keypair_refusal() and
    it fails on the sentence.
    """
    _unarmed(monkeypatch)
    assert payout_keypair_is_present() is False, "the suite must not inherit an armed posture"

    adapter = SolanaAdapter(url="http://unreachable.invalid", hot_wallet=valid_addresses.SOL_DEPOSIT_ACCOUNT)
    assert adapter.can_spend is False
    assert adapter.payout_refusal == missing_keypair_refusal(), (
        "the adapter must carry the one shared sentence rather than a second copy of it (rule 8)"
    )

    refusal = why_cannot_pay_out({"SOL": adapter}, "SOL")
    assert refusal, "an adapter that cannot spend gave no reason"
    assert KEYPAIR_PATH_VARIABLE in refusal, (
        "the refusal no longer names the variable that would fix it, which is the whole of its "
        "usefulness to an operator reading it off a customer page or a spawn banner (rule 14)"
    )
    assert "SOL_HOT_WALLET" in refusal, (
        "the refusal no longer names the SECOND variable; naming one sends the reader to do half "
        "the work"
    )


def test_a_keypair_path_ALONE_makes_can_spend_True_AND_CLEARS_THE_REFUSAL(monkeypatch):
    """SET -> PERMITS, which is the direction that can regress silently.

    THIS IS THE ASSERTION THAT PROVES THE MECHANISM IS NOT A SECOND REFUSAL. A guard
    that refused in both states would pass the test above forever, and the payout
    path would be dead code nobody noticed -- the operator would export the variable
    and the page would still say SOL cannot pay out. So the pair of directions is
    the test, not either half.

    NOTHING IS OPENED AND NOTHING IS DECODED. The value is a sentinel with no
    separator in it; chains/solana_payout_keypair.payout_keypair_is_present() reads
    bool() of the variable, and the url is unreachable.invalid because construction
    makes no call -- if either of those ever changes, this test fails loudly rather
    than quietly doing something real.

    MUTATION: put `can_spend = False` back as a hardcoded class attribute, or delete
    the two derivation lines from __init__, and this fails.
    """
    monkeypatch.setenv(KEYPAIR_PATH_VARIABLE, SENTINEL_KEYPAIR_PATH)
    assert payout_keypair_is_present() is True

    armed_adapter = SolanaAdapter(url="http://unreachable.invalid", hot_wallet=valid_addresses.SOL_DEPOSIT_ACCOUNT)
    assert armed_adapter.can_spend is True, (
        f"with {KEYPAIR_PATH_VARIABLE} set, SolanaAdapter.can_spend must be True. If this fails the "
        f"payout path is unreachable from the worker no matter what the operator exports, and the "
        f"wiring is dead code"
    )
    assert armed_adapter.payout_refusal == ""
    assert why_cannot_pay_out({"SOL": armed_adapter}, "SOL") == "", (
        "an armed adapter still reports a payout refusal, so the customer page would refuse a swap "
        "the worker could actually pay"
    )


def test_a_SET_BUT_EMPTY_export_is_NOT_armed(monkeypatch):
    """`export SOL_PAYOUT_KEYPAIR_PATH=''` is absent, not present.

    What a generator run from a shell without the value writes, and config.py's own
    _env() carries the measurement that cost: five empty exports on 2026-09-26
    turned a missing setting into a crash at import. Here, reading "" as present
    would be worse than a crash -- can_spend True, a SOL swap created, a customer's
    deposit taken, and the payout refused at the send with the deposit already
    credited.

    WHITESPACE TOO, because `read` and heredocs leave a newline and a path with a
    trailing newline does not open.

    MUTATION: drop the .strip() in keypair_path_from_environment(), or compare with
    `is not None`, and this fails.
    """
    for blank in ("", "   ", "\n"):
        monkeypatch.setenv(KEYPAIR_PATH_VARIABLE, blank)
        assert payout_keypair_is_present() is False, f"{blank!r} must read as unarmed"
        adapter = SolanaAdapter(url="http://unreachable.invalid")
        assert adapter.can_spend is False
        assert KEYPAIR_PATH_VARIABLE in adapter.payout_refusal


def test_the_CLASS_level_values_are_the_REFUSING_pair_whatever_the_environment(monkeypatch):
    """Fail-closed: anything that reads the attributes off the CLASS gets "cannot pay".

    A stub, a reflective check, a reader who never constructed an adapter --
    chains/registry.why_cannot_pay_out() already fails in this direction when the
    attribute is missing entirely, and the class-level pair is the same choice made
    one step earlier. chains/xrp.py's class attributes are the refusing pair for
    exactly this reason and say so.

    ASSERTED WITH THE VARIABLE SET, which is the state where a mistake here would
    matter: the class must not become armed because some process in some shell is.
    """
    monkeypatch.setenv(KEYPAIR_PATH_VARIABLE, SENTINEL_KEYPAIR_PATH)
    assert SolanaAdapter.can_spend is False
    assert KEYPAIR_PATH_VARIABLE in SolanaAdapter.payout_refusal
    assert why_cannot_pay_out({"SOL": SolanaAdapter}, "SOL").startswith("SOL cannot pay out")


# --- the module that answers the question never opens the key file ------------


def test_NOTHING_on_the_capability_path_OPENS_the_file_the_variable_names(monkeypatch):
    """The capability is a bool about an environment variable, not a key read.

    WHY THIS IS A TEST AND NOT A COMMENT. can_spend is read by every surface this
    application renders -- the customer page, the admin page, the operator panel, the
    worker spawn banner -- and a capability check that opened a key file would mean a
    key read on page load, in a Flask request handler, in a process paying nobody.
    chains/solana_payout_keypair.py's header states that it never opens the file; a
    sentence is not evidence (rule 17), so this proves it by making every open of
    that path raise.

    FAILS LOUDLY RATHER THAN SKIPPING, which the brief for this work asked for
    explicitly: the sentinel is not a real file, so a code path that opened it would
    already fail -- but it could fail as a swallowed OSError somewhere and look like
    "no key". So builtins.open and Path.open/Path.read_text/Path.read_bytes are
    patched to raise AssertionError, which no handler on this path catches, and the
    test dies naming the caller.

    THE ONE PLACE THE FILE IS OPENED AT ALL is
    chains/solana_signing.load_payout_keypair(), whose only non-test caller is
    signed_transfer_wire() -- after the arming token has matched and after the
    cluster has been proven to be devnet. That is checked by the armed tests above,
    which read a REAL throwaway keypair.
    """
    monkeypatch.setenv(KEYPAIR_PATH_VARIABLE, SENTINEL_KEYPAIR_PATH)
    opened = []

    def refuse_open(target, *args, **kwargs):
        name = str(getattr(target, "name", target))
        if SENTINEL_KEYPAIR_PATH in name:
            opened.append(name)
            raise AssertionError(f"the capability path OPENED the key file: {name}")
        return _real_open(target, *args, **kwargs)

    def refuse_path_method(name):
        """A replacement for Path.<name> that raises when the sentinel is the target.

        A FACTORY RATHER THAN A LAMBDA IN THE LOOP, because a closure over the loop
        variable would capture its LAST value and every message would name
        read_bytes -- ruff's B023, and here it would make the failure name the wrong
        caller, which is worse than the lint.
        """

        def refused(self, *args, **kwargs):
            if SENTINEL_KEYPAIR_PATH in str(self):
                opened.append(str(self))
                raise AssertionError(f"the capability path called Path.{name} on {self}")
            return _real_path_methods[name](self, *args, **kwargs)

        return refused

    # Imported here and not at the top because it exists only to capture the
    # UNPATCHED methods for the duration of this test; monkeypatch reverts them, and
    # a module-level name would be reachable from every other test in this file.
    from pathlib import Path  # noqa: PLC0415 -- checked: local on purpose, see above

    _real_open = open
    _real_path_methods = {name: getattr(Path, name) for name in ("open", "read_text", "read_bytes")}
    monkeypatch.setattr("builtins.open", refuse_open)
    for name in _real_path_methods:
        monkeypatch.setattr(f"pathlib.Path.{name}", refuse_path_method(name))

    assert payout_keypair_is_present() is True
    adapter = SolanaAdapter(url="http://unreachable.invalid", hot_wallet=valid_addresses.SOL_DEPOSIT_ACCOUNT)
    assert adapter.can_spend is True
    assert adapter.payout_refusal == ""
    assert why_cannot_pay_out({"SOL": adapter}, "SOL") == ""
    assert missing_keypair_refusal()
    assert opened == []


def test_the_keypair_PATH_VALUE_reaches_no_attribute_no_log_and_no_refusal_message(
    tmp_path, monkeypatch, caplog, capsys
):
    """A sentinel path value, and four places it must not appear.

    FOUR PLACES, each of which has leaked a value in some codebase, and the same four
    tests/test_xrp_payout_wiring.py checks for the XRP seed:

      the adapter     vars() on a constructed instance, because an attribute is what
                      a template, a debugger and a generic dumper reach for first
      log records     captured at DEBUG, the most verbose level, and searched in BOTH
                      getMessage() and repr(record.args) -- a value passed as a lazy
                      format argument survives in `args` whether or not any handler
                      formats the message, which is the half a naive test misses
      the refusal     the exception message from a guard that fires with the variable
                      SET, which is where a well-meaning "so you know which key"
                      would have been appended
      stdout/stderr   the preview block is exactly what an operator pastes back

    THE REFUSAL USED HERE IS THE DEVNET GUARD, deliberately, because it is the one
    that fires with the variable set and before anything is signed. An unarmed
    process has no value to leak.

    AND THE ONE DELIBERATE EXCEPTION IS NAMED RATHER THAN HIDDEN, because pretending
    it does not exist would make this test a lie: chains/solana_signing.
    describe_keypair_file() PRINTS the path and its file mode, and
    chains/solana.py's _sign_and_broadcast() prints that line immediately before the
    broadcast. That is rule 14 working as intended -- an operator about to move money
    must see WHICH key is signing and whether it is group-readable -- and it happens
    only on the armed devnet path, after every guard, which is why no assertion here
    covers it. The path is not a secret; it is also not something a customer page, a
    log line or a refusal has any reason to carry.
    """
    monkeypatch.setenv(KEYPAIR_PATH_VARIABLE, SENTINEL_KEYPAIR_PATH)
    caplog.set_level(logging.DEBUG)

    adapter = ready_adapter(genesis=MAINNET_BETA, hot_wallet=valid_addresses.SOL_DEPOSIT_ACCOUNT)
    assert adapter.can_spend is True, "the point of this test is a value PRESENT in the environment"
    with pytest.raises(SolanaClusterRefused) as refused:
        broadcast_payout(adapter, "SOL", SOL_PAYOUT_CONFIG, DESTINATION, 1.0)

    captured = capsys.readouterr()
    for where, text in (
        ("the exception message", str(refused.value)),
        ("a log record", all_log_text(caplog)),
        ("stdout", captured.out),
        ("stderr", captured.err),
        ("an adapter attribute", repr(vars(adapter))),
    ):
        assert SENTINEL_KEYPAIR_PATH not in text, f"the keypair path reached {where}"
    assert "sendTransaction" not in methods(adapter)


# --- the call site: which keywords each chain's send actually gets ------------


def test_a_SOL_payout_gets_the_ARMING_TOKEN_and_nothing_else(monkeypatch):
    """The wiring, asserted on the call rather than on an outcome.

    WHAT THIS REPLACED, MEASURED IN THIS CHECKOUT BEFORE THE CHANGE:
    services/payout_service.broadcast_payout() had no SOL branch, so SOL fell
    through to `send_to_address(address, amount)` and every SOL payout raised
    SolanaSendNotArmed whatever the host had exported. The operator's instruction
    ("whoa we have to be able to swap TO SOL too") is what changed it.

    NO source AND NO seed KEYWORD, which is the half that would be easy to get wrong
    by copying XRP's branch: SOL's payer is SOL_HOT_WALLET, which the adapter already
    holds and the preview announces, and the keypair never passes through this
    process's own code -- chains/solana_signing.signed_transfer_wire() reads it from
    the file for the duration of one call. A `source=` or `seed=` keyword here would
    be a TypeError on a live payout.

    THE TOKEN IS COMPARED TO THE IMPORTED CONSTANT, so a drifted literal at the call
    site fails here rather than refusing every payout in production with a message
    about the token.

    MUTATION: delete the SOL branch from broadcast_payout() and this fails on the
    empty kwargs.
    """
    monkeypatch.setenv(KEYPAIR_PATH_VARIABLE, SENTINEL_KEYPAIR_PATH)
    adapter = RecordingAdapter()
    assert broadcast_payout(adapter, "SOL", SOL_PAYOUT_CONFIG, DESTINATION, 0.25) == "stub-signature"

    (address, amount, kwargs) = adapter.calls[0]
    assert len(adapter.calls) == 1
    assert (address, amount) == (DESTINATION, 0.25)
    assert kwargs == {"confirm_send": CONFIRM_SOL_SEND}, (
        f"a SOL send takes the arming token and nothing else; it got {sorted(kwargs)}"
    )


def test_the_TOKEN_IS_PASSED_WHETHER_OR_NOT_THE_HOST_IS_ARMED(monkeypatch):
    """The arming is the KEYPAIR PATH, not a flag at the call site.

    A DELIBERATE DIVERGENCE FROM XRP, stated at both sites (rule 8). XRP refuses
    inside broadcast_payout(), before any network call, when its seed variable is
    unset. SOL passes the token unconditionally and lets
    chains/solana_signing.require_send_confirmation() refuse on the empty keypair
    path, because THAT refusal arrives with the whole preview inside it -- see the
    test below -- and an operator who then exports the variable is arming something
    they have read.

    So the call made with the variable unset is byte-for-byte the call made with it
    set, and this test pins that: the capability lives in one place, and it is the
    environment rather than a boolean somebody can flip in a config file.
    """
    _unarmed(monkeypatch)
    adapter = RecordingAdapter()
    broadcast_payout(adapter, "SOL", SOL_PAYOUT_CONFIG, DESTINATION, 0.25)
    assert adapter.calls[0][2] == {"confirm_send": CONFIRM_SOL_SEND}


def test_every_other_chain_still_gets_exactly_two_positional_arguments(monkeypatch):
    """BTC, LTC and GRC are unchanged, byte for byte, and that is the point.

    Adding a keyword to a BTC send would be a change to the one function in this
    suite that moves money for no behavioral gain. The daemon holds the wallet, picks
    the inputs and signs; there is nothing for this process to arm.

    ASSERTED WITH THE SOL VARIABLE SET, because the hazard worth excluding is a
    branch that leaks the SOL keyword onto another chain the day somebody exports it.
    """
    monkeypatch.setenv(KEYPAIR_PATH_VARIABLE, SENTINEL_KEYPAIR_PATH)
    for asset in ("BTC", "LTC", "GRC"):
        adapter = RecordingAdapter()
        broadcast_payout(adapter, asset, SOL_PAYOUT_CONFIG, valid_addresses.GRC_PAYOUT, 1.0)
        assert adapter.calls == [(valid_addresses.GRC_PAYOUT, 1.0, {})], f"{asset} gained a keyword"


# --- unarmed: the preview travels with the refusal, and nothing is sent -------


def test_an_UNARMED_SOL_payout_gets_the_PREVIEW_in_the_refusal_and_broadcasts_NOTHING(monkeypatch):
    """The default, through the real service path and a real adapter.

    THREE THINGS IN ONE ASSERTION SET, and the third is the one that matters:

      it refuses        SolanaSendNotArmed, from
                        chains/solana_signing.require_send_confirmation()'s SECOND
                        branch (the token matched; the keypair path is empty), which
                        is where the live worker now lands on an unarmed host.
      it says WHAT to   the message names SOL_PAYOUT_KEYPAIR_PATH, so an operator
      export            reading a failed payout does not have to come back here.
      nothing was sent  asserted on the recorded call list -- `sendTransaction`
                        appears nowhere -- rather than on the absence of a return
                        value, which is what the behavioral-verification principle
                        asks for.

    AND THE PREVIEW IS IN THE MESSAGE, which is why the pre-flight refusal XRP uses
    is deliberately NOT copied here: an operator who then exports the variable has
    read the cluster, the payer, the destination, the lamports, the fee, the rent
    verdict and the headroom.

    MUTATION: delete the require_send_confirmation() call from send_to_address() and
    this fails on the refusal; delete the keypair-path branch inside that function
    and it fails on `sendTransaction` being in the call list.
    """
    _unarmed(monkeypatch)
    adapter = ready_adapter(hot_wallet=valid_addresses.SOL_DEPOSIT_ACCOUNT)
    with pytest.raises(SolanaSendNotArmed) as refused:
        broadcast_payout(adapter, "SOL", SOL_PAYOUT_CONFIG, DESTINATION, 1.0)

    message = str(refused.value)
    assert KEYPAIR_PATH_VARIABLE in message
    assert "SOL PAYOUT PREVIEW" in message
    assert "sendTransaction" not in methods(adapter), "an unarmed call must not reach the chain"


def test_THE_DEVNET_GUARD_STILL_REFUSES_A_MAINNET_GENESIS_EVEN_WHEN_ARMED(tmp_path, monkeypatch):
    """Arming is not a bypass. The cluster refusal is decided by the CLUSTER.

    THE WORST OUTCOME THIS WHOLE FILE EXISTS TO PREVENT is a real mainnet transfer,
    and the thing that would produce it is a guard that only ran while nothing could
    sign. So this test arms the process with a REAL throwaway keypair that derives
    the announced payer -- every other refusal is satisfied -- and the only fault is
    the genesis hash the seeded cluster reports.

    MEASURED, not reasoned: the refusal class is SolanaClusterRefused, it names the
    cluster, and `getBalance`, `getLatestBlockhash` and `sendTransaction` appear
    nowhere in the call list, so the refusal is before the balance read and not
    merely before the broadcast.

    MUTATION: delete the require_devnet() call from preview_payout() and this fails.
    There is no flag, environment variable or argument that disables it, which is
    what makes this assertion possible to write at all.
    """
    adapter, _seed, _public = armed(tmp_path, monkeypatch, genesis=MAINNET_BETA)
    assert adapter.can_spend is True, "this test is worthless unless the process really is armed"
    with pytest.raises(SolanaClusterRefused) as refused:
        broadcast_payout(adapter, "SOL", SOL_PAYOUT_CONFIG, DESTINATION, 1.0)

    # MEASURED off the real refusal rather than written from memory: the message
    # spells the cluster "MAINNET-BETA" in capitals. The first version of this line
    # asserted lower case and failed, which is the small version of the thing rule 17
    # is about -- a plausible string is not a measured one.
    assert "MAINNET-BETA" in str(refused.value)
    assert methods(adapter) == ["getGenesisHash"], (
        f"the cluster must be refused before anything else is read; the calls were {methods(adapter)}"
    )


# --- the whole worker loop, on rows rather than return values -----------------


def test_an_UNARMED_SOL_swap_lands_FAILED_with_the_reason_and_NOTHING_recorded_as_broadcast(
    tmp_path, monkeypatch
):
    """The stranded-swap shape, asserted on the database the worker left behind.

    THIS IS THE OUTCOME THE OPERATOR'S INSTRUCTION WAS ABOUT, seen from the other
    side: before 2026-10-03 this was the ONLY outcome a SOL payout could have, on a
    swap whose deposit had already been credited. It is still the outcome on an
    unarmed host, and that is correct -- what must never happen is a payouts row
    reading `broadcast` with no transaction, or a swap reading `completed`.

    ROWS, NOT A RETURN VALUE (rule 13's "a stop that cannot prove it worked"): the
    swap is `failed` with SOL_PAYOUT_KEYPAIR_PATH in failed_reason, the payouts row
    is `failed` with a NULL txid, and the recorded call list has no sendTransaction
    in it.

    THE AMOUNT CLEARS THE RENT FLOOR on purpose -- 0.05 SOL against a 650240-lamport
    floor measured on the operator's own devnet run -- so that this test dies on the
    arming and not on a guard it is not named for.
    """
    _unarmed(monkeypatch)
    db_path = str(tmp_path / "sol_payout_unarmed.db")
    _seed_one_pending_sol_swap(db_path, DESTINATION, 0.05)
    adapter = ready_adapter(destination_exists=False, hot_wallet=valid_addresses.SOL_DEPOSIT_ACCOUNT)

    conn = connect_db(db_path)
    try:
        completed = process_pending_payouts(conn, SOL_PAYOUT_CONFIG, {"SOL": adapter})
        swap = conn.execute("SELECT * FROM swaps WHERE id = 's_sol'").fetchone()
        payout = conn.execute("SELECT * FROM payouts WHERE swap_id = 's_sol'").fetchone()
    finally:
        conn.close()

    assert completed == []
    assert swap["status"] == "failed"
    assert swap["payout_txid"] is None
    assert KEYPAIR_PATH_VARIABLE in (swap["failed_reason"] or ""), (
        f"the recorded reason must name the variable an operator exports; it was {swap['failed_reason']!r}"
    )
    assert payout["status"] == "failed"
    assert payout["txid"] is None
    assert "sendTransaction" not in methods(adapter)


def test_an_ARMED_SOL_payout_completes_the_swap_and_records_the_SIGNATURE(tmp_path, monkeypatch):
    """The armed outcome, which is the whole of what the operator asked for.

    AND IT IS THE MUTATION CHECK FOR THE WIRING ITSELF. A correct adapter whose
    caller never armed it looks exactly like a working change: the suite is green,
    the preview is beautiful, and every payout fails in production. So the assertion
    is the state the worker left behind -- payouts `broadcast` with the signature,
    the swap `completed` carrying the same value in payout_txid, exactly one
    sendTransaction -- and reverting broadcast_payout()'s SOL branch to two
    positional arguments turns this test red while every other test in this file
    stays green.

    WHAT IS STILL NOT PROVEN, AND IT IS WHY THIS IS NOT EVIDENCE OF A WORKING PAYOUT:
    the transport is a seeded table. `sendTransaction` returns the signature computed
    by DECODING the base64 the adapter actually sent (echo_signature above), so the
    bytes are checked against the plan -- but no transaction from this path has ever
    reached a cluster, from any environment, ever. api.devnet.solana.com answers 403
    at this container's proxy, re-measured 2026-10-03. The first real send is the
    operator's run.
    """
    adapter, _seed, public = armed(tmp_path, monkeypatch, destination_exists=False)
    assert adapter.can_spend is True

    db_path = str(tmp_path / "sol_payout_armed.db")
    _seed_one_pending_sol_swap(db_path, DESTINATION, 0.05)

    conn = connect_db(db_path)
    try:
        completed = process_pending_payouts(conn, SOL_PAYOUT_CONFIG, {"SOL": adapter})
        swap = conn.execute("SELECT * FROM swaps WHERE id = 's_sol'").fetchone()
        payout = conn.execute("SELECT * FROM payouts WHERE swap_id = 's_sol'").fetchone()
    finally:
        conn.close()

    sent = [params for method, params in adapter.calls if method == "sendTransaction"]
    assert len(sent) == 1, "exactly one broadcast"
    parsed = parse_transfer_transaction(base64.b64decode(sent[0][0]))
    assert parsed["payer"] == public
    assert parsed["destination"] == DESTINATION
    assert parsed["lamports"] == 50_000_000

    assert len(completed) == 1
    assert swap["status"] == "completed"
    assert swap["payout_txid"] == parsed["signature"]
    assert payout["status"] == "broadcast"
    assert payout["txid"] == parsed["signature"]
    assert payout["destination_address"] == DESTINATION
