"""A SOL -> GRC swap is created, and gets a Solana memo tag. The 2026-10-01 blocker.

Role: test (real database on the real schema, real SolanaAdapter, no network)
Reads: services/swap_service.py, services/xrp_tag_service.py
Writes: a temp database under pytest's tmp_path
Can move funds: no -- GRC is a stub that records calls, and the SolanaAdapter
        here is only ever asked validate_address(), which makes no network call
Mainnet-safe: yes

WHAT THIS FILE IS FOR.

Measured on the operator's host 2026-10-01, with a real devnet SOL -> testnet
GRC rehearsal in progress. The page said

    SOL -> GRC  ENABLED

the admin surface said `SOL ... tradeable YES ... deposit attribution: tag`,
`POST /api/quotes` returned 201 with a priced quote, and `POST /api/swaps`
returned 400:

    CUBnQ5QBfYkL71TCqSdecAQ9xjfGmAdu6Hs3fjQeLorp is not a valid XRPL classic
    address (checksum verified locally, no network call). NOTHING was written.

Which is true, and about the wrong ledger. services/xrp_tag_service.
validate_account() checked EVERY tag-attributed chain's account against the XRP
Ledger's alphabet and checksum, because XRP was the only tag chain when it was
written. SOL joined TAG_ATTRIBUTED_ASSETS, every gate and every page learned
about it, and that one function did not -- so the second tag chain was refused
at the last step by the first chain's address format.

NOTHING IN THE SUITE CAUGHT IT, and that is the gap this file closes. The XRP
path had end-to-end coverage (test_xrp_swap_attribution.py) and SOL had none:
every SOL test was an adapter test. A pair can be ENABLED on the page, priced by
the quote path, and impossible to actually create, and the only thing that knows
is an HTTP 400 in front of a customer.

THE REAL SolanaAdapter IS USED rather than a stub, deliberately. A stub's
validate_address() would be my opinion of what a Solana account looks like,
which is exactly the kind of agreement-by-construction that let the defect
through. validate_address() makes no network call -- a Solana address is a
self-describing 32-byte ed25519 key, so it can always answer locally -- which is
what makes the real thing usable in a test with no socket.
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "swap_terminal"))

from chains.solana import SolanaAdapter
from chains.solana_address import associated_token_address, is_on_curve, is_valid_address
from services.swap_service import TAG_ATTRIBUTED_ASSETS, create_swap, deposit_account
from services.xrp_tag_service import ACCOUNT_VALIDATORS, XRPTagAllocationError, validate_account
from test_xrp_swap_attribution import GRC_TESTNET_ADDRESS, StubGRC, seed_quote, seeded_db

# THE ACCOUNT FROM THE OPERATOR'S HOST, not a synthesized one. It is the real
# SOL_DEPOSIT_ACCOUNT of the devnet rehearsal and the exact string the 400 above
# refused, so this file's central assertion is the measurement rather than a
# reconstruction of it.
SOL_ACCOUNT = "CUBnQ5QBfYkL71TCqSdecAQ9xjfGmAdu6Hs3fjQeLorp"

# An Associated Token Account: 32 valid base58 bytes, OFF the ed25519 curve, so
# no private key exists for it. Derived rather than typed, because a hand-picked
# string that happened to be on-curve would make the off-curve test vacuous.
WRAPPED_SOL_MINT = "So11111111111111111111111111111111111111112"


def sol_adapter() -> SolanaAdapter:
    """The real adapter. The URL is never reached -- only validate_address() is called."""
    return SolanaAdapter(url="https://api.devnet.solana.com")


def off_curve_account() -> str:
    ata = associated_token_address(SOL_ACCOUNT, WRAPPED_SOL_MINT)
    assert is_valid_address(ata), "test setup: the derived ATA is not valid base58-32"
    assert not is_on_curve(ata), "test setup: the derived ATA is ON the curve, so it proves nothing"
    return ata


# --- the blocker itself, end to end ------------------------------------------

def test_a_sol_swap_is_created_and_gets_a_tag(tmp_path):
    """The 400 reproduced as a passing creation, against a real database.

    Asserted on the ROW and not only on the returned dict, because the dict is
    what the API echoes and the row is what the deposit watcher reads. A tag
    that reached one and not the other would hand a customer a memo no worker
    could match -- services/deposit_service.py logs exactly that case ("is on
    SOL, which attributes deposits by tag, but has NO deposit_tag") and credits
    nothing.
    """
    conn = seeded_db(tmp_path)
    config = {"SOL_DEPOSIT_ACCOUNT": SOL_ACCOUNT, "SOL_MIN_CONFIRMATIONS": 3}
    adapters = {"SOL": sol_adapter(), "GRC": StubGRC()}
    seed_quote(conn, "q-sol", from_asset="SOL", to_asset="GRC")

    swap = create_swap(conn, config, adapters, "q-sol", GRC_TESTNET_ADDRESS)

    assert swap["from_asset"] == "SOL"
    assert swap["deposit_address"] == SOL_ACCOUNT, "SOL shares one account; the memo tells swaps apart"
    assert isinstance(swap["deposit_tag"], int), f"no memo tag was allocated: {swap['deposit_tag']!r}"
    assert swap["deposit_tag"] >= 1

    row = conn.execute("SELECT deposit_tag, deposit_address FROM swaps WHERE id = ?", (swap["id"],)).fetchone()
    conn.close()
    assert row["deposit_tag"] == swap["deposit_tag"], "the tag must reach the ROW, not just the dict"
    assert row["deposit_address"] == SOL_ACCOUNT


def test_two_sol_swaps_share_the_account_and_get_different_tags(tmp_path):
    """One shared account is the whole reason the memo is mandatory.

    If these ever got different addresses, something has started deriving a
    Solana account per swap, each of which needs rent and a key we hold -- a
    funding and custody change that should surface as a failing test.
    """
    conn = seeded_db(tmp_path)
    config = {"SOL_DEPOSIT_ACCOUNT": SOL_ACCOUNT, "SOL_MIN_CONFIRMATIONS": 3}
    adapters = {"SOL": sol_adapter(), "GRC": StubGRC()}

    swaps = []
    for n in range(2):
        seed_quote(conn, f"q-sol-{n}", from_asset="SOL", to_asset="GRC")
        swaps.append(create_swap(conn, config, adapters, f"q-sol-{n}", GRC_TESTNET_ADDRESS))
    conn.close()

    assert {s["deposit_address"] for s in swaps} == {SOL_ACCOUNT}
    tags = [s["deposit_tag"] for s in swaps]
    assert len(set(tags)) == 2, f"two swaps on one account must not share a memo: {tags}"


def test_the_deposit_account_itself_was_never_the_problem():
    """deposit_account() was already right, and saying so is the point.

    It asks the CHAIN'S OWN adapter -- adapters[from_asset].validate_address() --
    and takes the chain's name from TAG_ATTRIBUTION, so it had no XRP assumption
    to drift from. The defect was one step later, in tag allocation. Pinned
    because "the deposit path validates per chain" is the invariant that makes
    the fix a one-place fix, and a future refactor that moved this check into a
    shared XRP-shaped validator would reintroduce the bug here instead.
    """
    account, needs_tag = deposit_account(
        {"SOL_DEPOSIT_ACCOUNT": SOL_ACCOUNT}, {"SOL": sol_adapter()}, "SOL", "s-1"
    )
    assert account == SOL_ACCOUNT
    assert needs_tag is True


# --- the validator table -----------------------------------------------------

def test_a_solana_account_is_accepted_for_sol_and_refused_for_xrp():
    """The two answers that used to be one answer.

    Both directions, because a validator that accepted everything would satisfy
    the first assertion alone -- and the bug was a validator that refused a
    correct account, so the refusal half has to be the XRP one.
    """
    assert validate_account(SOL_ACCOUNT, "SOL") == SOL_ACCOUNT
    with pytest.raises(XRPTagAllocationError, match="not a valid XRPL classic address"):
        validate_account(SOL_ACCOUNT, "XRP")


def test_an_off_curve_solana_account_is_refused():
    """A token account passes every syntactic check and nobody can sign for it.

    This is worse than the tag collision the validator mainly guards: an
    off-curve deposit account could never be swept, so every customer deposit
    into it would be permanently stranded. chains/solana.py::validate_address()
    refuses the same thing for a payout address.
    """
    with pytest.raises(XRPTagAllocationError, match="OFF THE CURVE"):
        validate_account(off_curve_account(), "SOL")


def test_a_malformed_solana_account_is_refused_before_a_tag_is_burned():
    with pytest.raises(XRPTagAllocationError, match="not a valid Solana account"):
        validate_account("not-a-solana-account", "SOL")


def test_an_empty_account_names_the_asset_it_was_asked_about():
    """One copy of the reason that is true on every chain (rule 8), and it says which.

    A tag is only an identifier relative to the account it is sent to, so an
    empty account numbers a sequence under the empty string and collides with
    nothing. Asserted on BOTH assets, because a message hardcoding one chain's
    name is the defect this whole file is about, one string over.
    """
    for asset in ("SOL", "XRP"):
        with pytest.raises(XRPTagAllocationError, match=f"no {asset} account was given"):
            validate_account("   ", asset)


def test_every_tag_attributed_asset_has_an_account_validator():
    """The drift guard, and it is the one assertion that would have caught this.

    SOL was in TAG_ATTRIBUTED_ASSETS and had no validator. Nothing compared the
    two sets, so the gap was invisible until a customer hit it. Set equality in
    both directions: a validator for an asset that does not allocate tags is
    dead code (rule 9), and an asset with no validator is the 400 above.
    """
    assert set(ACCOUNT_VALIDATORS) == set(TAG_ATTRIBUTED_ASSETS)


def test_an_unregistered_asset_refuses_by_naming_itself():
    """A third tag chain gets a refusal about ITSELF, not XRP's checksum error.

    The defect was legible only to somebody who knew that SOL was being checked
    by XRP's validator; the message said nothing about SOL. An unknown asset now
    says what is missing and where to add it, and lists what IS registered
    (rule 14: say what the number means next to the number).
    """
    with pytest.raises(XRPTagAllocationError, match="no account validator is registered for it"):
        validate_account("rnjG8n16JinjqkzZj5Jmw6NDMBMzhhNbVv", "DOGE")
