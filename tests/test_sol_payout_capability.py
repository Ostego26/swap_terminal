"""SolanaAdapter.can_spend must mean "this adapter can produce a payout". The 2026-10-11 gap.

Role: test (the real adapter, the real registry gate, the real pair verdict and
      the real create_swap, against a real database on the real schema; no socket
      is opened and no key file exists)
Reads: chains/solana.py, chains/solana_payout_keypair.py, chains/solana_address.py,
      chains/registry.py, modules/address_authority.py, services/pair_view.py,
      services/quote_service.py, services/swap_service.py, and chains/solana.py's
      own SOURCE (one test walks its AST rather than importing it)
Writes: a temp database under pytest's tmp_path
Can move funds: no. Nothing here signs, broadcasts or reads a key: the keypair
      path used throughout is a SENTINEL naming a file that does not exist, and
      the whole point of two of these tests is that nothing opens it.
Mainnet-safe: yes. No cluster is reached -- the one adapter that answers RPC
      calls answers from a seeded table, and the devnet genesis hash it returns
      is read out of network_target.GENESIS_HASHES rather than written again.

=============================================================================
WHAT WAS MEASURED, AND WHY A SENTENCE WAS NOT THE FIX
=============================================================================

Measured 2026-10-11 by constructing the real SolanaAdapter with
SOL_PAYOUT_KEYPAIR_PATH exported, against seeded cluster responses:

    configuration             can_spend   why_cannot_pay_out   preview_payout()
    ------------------------  ---------   ------------------   ---------------------
    hot wallet set, no mint   True        ""                   OK
    SOL_HOT_WALLET UNSET      True        ""                   SolanaRPCError
    SOL_SPL_MINT set          True        ""                   SolanaSplSendRefused

The second and third rows are a terminal declaring a capability it does not
have. `can_spend` is what chains/registry.why_cannot_pay_out() reads, which is
what services/pair_view.pair_serviceability() badges a pair on, which is what
services/swap_service.create_swap() gates on -- so in both of those rows all
five `* -> SOL` pairs read AVAILABLE, a swap was created, a customer's deposit
was taken and credited, and the payout then refused on its FIRST guard having
read nothing from the cluster. On Solana the deposit is irreversible by then.

THE SECOND ROW IS THE OPERATOR'S NEXT COMMAND. `docker-compose.web.armed-sol.yml`
sets SOL_PAYOUT_KEYPAIR_PATH and nothing else -- read 2026-10-11, its entire
non-comment body is one `volumes` entry and one `environment` entry -- so
`configure_env.py --arm sol --apply` on a host where SOL_HOT_WALLET is unset
produces exactly it. SOL_HOT_WALLET was measured UNSET on that host the same day.

THE TREE ALREADY KNEW AND CHOSE A SENTENCE. tests/test_solana_payout.py::
test_the_default_is_no_keypair_path_and_therefore_no_payout_capability says it in
its own docstring -- "a host with the keypair and no SOL_HOT_WALLET is armed and
still cannot pay" -- and the 2026-10-03 remedy was to make
missing_keypair_refusal() NAME both variables. The sentence is correct and is not
rendered in this state, because in this state there IS no refusal: can_spend is
True. Rule 19's test for a fix is whether it stops the symptom being reported or
stops the cause existing, and a better sentence on the path that is not taken is
the first one.

AND IT IS THE XRP INCIDENT IN A DIFFERENT ALPHABET. chains/xrp_payout_seed.
payout_capability() exists because XRP_PAYOUT_SECRET_SEED held a nine-character
placeholder, can_spend was True, and this terminal offered a payout it could
never have signed. Same shape -- a credential PRESENT and unable to produce a
payout -- and the remedy is the same one: ask the whole question at construction,
where it is cheap and where nothing has been taken.

=============================================================================
WHAT EACH TEST ESTABLISHES, AND THE MUTATION THAT BREAKS IT
=============================================================================

Every test below names its own mutation. The two that matter most are the pair of
DIRECTIONS: a guard that refused in every state would satisfy the refusal tests
forever while making the payout path dead code, so
test_an_armed_host_with_a_hot_wallet_and_no_mint_CAN_pay is not optional
decoration -- it is the half that proves the mechanism is a capability and not a
second refusal.
"""

import ast
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "swap_terminal"))

import valid_addresses
from chains.registry import why_cannot_pay_out
from chains.solana import SolanaAdapter, SolanaRPCError
from chains.solana_address import (
    TOKEN_PROGRAM_ID,
    associated_token_address,
    is_on_curve,
    is_valid_address,
)
from chains.solana_payout_keypair import (
    KEYPAIR_PATH_VARIABLE,
    missing_keypair_refusal,
    payout_capability,
    payout_keypair_is_present,
    spl_mint_payout_refusal,
    unset_hot_wallet_refusal,
)
from chains.solana_signing import SolanaSplSendRefused
from config import Config
from modules.address_authority import check_address
from network_target import GENESIS_HASHES
from services import quote_service
from services.pair_view import pair_serviceability
from services.quote_service import (
    require_deliverable_sol_payout,
    why_cannot_establish_payout_floor,
)
from services.swap_service import create_swap
from test_xrp_swap_attribution import StubGRC, seed_quote, seeded_db
from valid_addresses import SOL_DEPOSIT_ACCOUNT, SOL_PAYOUT

# THE OPERATOR'S REAL DEVNET HOT WALLET, 2026-10-11, and a PUBLIC key.
#
# Pasted from their terminal, where `solana-keygen` wrote the keypair to
# ~/.config/solana/swap_terminal_hot.json. Nothing in this repository holds, reads
# or can derive the secret half, and this file never opens that path.
#
# NOT SYNTHESIZED, for the reason tests/valid_addresses.py gives at length about
# SOL_DEPOSIT_ACCOUNT: roughly half of all valid 32-byte strings are OFF the
# ed25519 curve, and chains/solana.py's payout capability now requires an
# ON-curve payer, so a phrase-derived fixture would pass or fail this file's
# central assertion by luck. Its on-curve-ness is asserted below rather than
# assumed, so a mistyped character fails loudly here instead of making a test
# vacuous.
#
# NOT in tests/valid_addresses.py, deliberately: that module's ALL_VALID sweep
# plus tests/test_address_literals_are_valid.fixture_asset() keys a fixture's
# asset off its NAME, and this one is the SOL hot wallet rather than a SOL
# payout destination -- adding it there is a change to a shared fixture module
# three other chains' tests read, and this agent's scope is the SOL adapter.
# Named here with the measurement beside it until somebody promotes it.
SOL_HOT_WALLET = "BBZAKv8SiuP2fLnanYLk3EfLWiuUMnbgNkrnABDT9Z7G"

#: A path that names no file, used everywhere a keypair path is "exported".
#:
#: NOTHING OPENS IT, which is the property two tests below assert rather than
#: trust. payout_capability() reads bool() of the variable through
#: chains/solana_signing.keypair_path_from_environment(); the only reader of the
#: FILE in this tree is chains/solana_signing.load_payout_keypair(), reached from
#: signed_transfer_wire() after the arming token has already matched. A path with
#: no separator and no such file means that if either of those facts ever changes,
#: these tests fail loudly instead of quietly doing something real.
SENTINEL_KEYPAIR_PATH = "sol-payout-keypair-path-sentinel-not-a-real-file"

#: Devnet's genesis hash, DERIVED from the table that owns it rather than spelled
#: again -- rule 8 applied to the one string that decides whether the payout path
#: may sign. chains/solana_signing.devnet_genesis_hash() does the same derivation
#: for the same reason; this does not import it because this file's seeded
#: transport has to answer getGenesisHash and a test that got the hash from the
#: function under test would prove only that the function agrees with itself.
DEVNET_GENESIS = next(
    genesis for genesis, label in GENESIS_HASHES.items() if label.strip().upper().startswith("DEVNET")
)

#: The rent-exempt minimum for a 0-byte account, as the operator's 2026-09-30
#: devnet run reported it. Seeded rather than hardcoded in the application --
#: chains/solana_units.py records that this repository's reference figures said
#: 890,880 while every cluster answered this, for weeks, and nothing failed.
RENT_FLOOR_LAMPORTS = 650_240


class SeededCluster(SolanaAdapter):
    """The real adapter with a seeded transport. Overrides call() and nothing else.

    A SUBCLASS RATHER THAN A STUB, which is the same judgment
    tests/test_sol_swap_creation.py records for using the real adapter: every
    decision under test -- can_spend, the refusal sentence, the guard order in
    preview_payout() -- is the real code, and only the socket is replaced. A stub
    would be my opinion of what the adapter does, which is the agreement-by-
    construction that let the 2026-10-11 gap through in the first place.

    EVERY ANSWER IS THE PERMISSIVE ONE, deliberately. The cluster is devnet, the
    destination does not exist (so the rent floor applies, which is the harder
    path), the payer holds 2 SOL, and the rent floor is the measured figure. So a
    preview that refuses in this file refuses for the configuration rather than
    because a response was missing, and a test asserting a refusal cannot pass by
    accident.
    """

    def call(self, method: str, *params):
        if method == "getGenesisHash":
            return DEVNET_GENESIS
        if method == "getMinimumBalanceForRentExemption":
            return RENT_FLOOR_LAMPORTS
        if method == "getAccountInfo":
            # `value: None` is "no account at this address", which is what the
            # cluster answers for an address nothing has ever been sent to.
            return {"value": None}
        if method == "getBalance":
            return {"value": 2_000_000_000}
        raise AssertionError(
            f"the seeded transport was asked {method}, which this file does not answer. A new RPC call on "
            f"the preview path is a change worth a deliberate seed rather than a silent None."
        )


class SourceGRC(StubGRC):
    """StubGRC plus the one method a SOURCE chain is asked for: get_new_address().

    tests/test_xrp_swap_attribution.StubGRC stands in for a payout DESTINATION --
    every test in that file and in tests/test_sol_swap_creation.py has GRC on the
    receiving end -- so it declares can_spend and validates an address and has no
    get_new_address. Here GRC is the SOURCE, and services/swap_service.
    deposit_account() derives a per-swap deposit address for every chain that is not
    in TAG_ATTRIBUTED_ASSETS, which GRC is not. Measured 2026-10-11 on the first run
    of the test below: `AttributeError: 'StubGRC' object has no attribute
    'get_new_address'`.

    SUBCLASSED RATHER THAN COPIED, and the shared stub is left alone deliberately:
    it is read by two other test files whose assets and directions differ, and
    adding a method there to satisfy this file would be a change to a fixture three
    directions rely on (rule 8 asks for the difference to be stated where it is,
    which is here -- GRC is the source in this file and the destination in those).

    THE ADDRESS IS A REAL TESTNET GRC ADDRESS from tests/valid_addresses.py --
    GRC_DESK_DEPOSIT, which is what a Gridcoin daemon's getnewaddress would return
    for the desk -- because the row it lands in is `swaps.deposit_address` and the
    gate above it is StubGRC.validate_address(), which decodes the version byte
    rather than looking at the first letter. Checked 2026-10-11 through that same
    function: it reads as testnet. An invented string would be refused by the stub
    and the failure would be about the fixture.
    """

    def get_new_address(self, label):
        return valid_addresses.GRC_DESK_DEPOSIT


def armed(monkeypatch) -> None:
    """Export the sentinel keypair path for this test, and nothing else.

    tests/conftest.py pops SOL_PAYOUT_KEYPAIR_PATH at import so the suite cannot
    inherit an armed posture from the shell that started it. This is the inverse,
    scoped to one test by monkeypatch, and it is asserted rather than assumed:
    payout_keypair_is_present() has to come back True or the test below is
    measuring an unarmed host and proving nothing.
    """
    monkeypatch.setenv(KEYPAIR_PATH_VARIABLE, SENTINEL_KEYPAIR_PATH)
    assert payout_keypair_is_present() is True, (
        "this test is worthless unless the process really is armed: the keypair path did not read as "
        "present after being exported"
    )


def off_curve_account() -> str:
    """A valid 32-byte key that is OFF the ed25519 curve, so no private key exists.

    DERIVED rather than typed, exactly as tests/test_sol_swap_creation.py derives
    its own: an associated token account is a Program Derived Address, chosen to be
    off the curve. A hand-picked string that happened to be ON the curve would make
    the off-curve test vacuous, so both properties are asserted here.
    """
    ata = associated_token_address(SOL_HOT_WALLET, SOL_DEPOSIT_ACCOUNT, TOKEN_PROGRAM_ID)
    assert is_valid_address(ata), "test setup: the derived ATA is not a valid Solana address"
    assert not is_on_curve(ata), "test setup: the derived ATA is ON the curve, so it proves nothing"
    return ata


def test_the_two_real_accounts_are_on_curve_and_therefore_usable(monkeypatch):
    """The operator's deposit account and hot wallet both pass every local check.

    THE FIXTURE GATE, and it runs first because every other test in this file rests
    on it. chains/solana.py::validate_address() and services/xrp_tag_service.
    _validate_sol_account() both require an ON-curve ed25519 point, and roughly half
    of all valid 32-byte base58 strings are not one -- so a mistyped character in
    either constant would turn the assertions below into a test of the typo.

    Measured 2026-10-11 against the real functions: both accounts are valid and
    both are on-curve, so neither is refused as a deposit account, as a payout
    destination, or as the payer this capability check now requires.

    THEY MUST ALSO BE TWO DIFFERENT ACCOUNTS, which is services/custody_separation.
    solana_account_verdict()'s whole subject: it returns NOT_SEPARATED when
    SOL_DEPOSIT_ACCOUNT == SOL_HOT_WALLET, because then every deposit lands in the
    account that pays every payout and one key holds both. The operator's default
    `solana address` IS the deposit account, which is why a separate keypair was
    made, and this asserts the separation holds in the fixtures too.

    MUTATION: change one character of either constant. is_on_curve() returns False
    for about half of all such changes and this fails; the rest fail the equality
    assertions in the tests below, where the message would be about a capability
    rather than about a fixture -- which is why this test exists separately.
    """
    for label, account in (("SOL_DEPOSIT_ACCOUNT", SOL_DEPOSIT_ACCOUNT), ("SOL_HOT_WALLET", SOL_HOT_WALLET)):
        assert is_valid_address(account), f"{label}={account} is not a valid Solana address"
        assert is_on_curve(account), (
            f"{label}={account} is OFF the ed25519 curve, so no private key can exist for it and it is "
            f"unusable as either a deposit account or a payer"
        )
    assert SOL_DEPOSIT_ACCOUNT != SOL_HOT_WALLET, (
        "the deposit account and the hot wallet are the same account, which services/custody_separation."
        "solana_account_verdict() reports as NOT_SEPARATED -- and these fixtures are supposed to be the "
        "separated configuration"
    )
    # ALSO THROUGH THE ADAPTER, because validate_address() is the function a payout
    # destination is actually checked by and it adds the on-curve requirement on top
    # of is_valid_address(). Asserting the two helpers alone would not establish that
    # the adapter accepts these accounts.
    adapter = SolanaAdapter(url="http://unreachable.invalid")
    assert adapter.validate_address(SOL_HOT_WALLET) is True
    assert adapter.validate_address(SOL_DEPOSIT_ACCOUNT) is True


# --- the capability, in all four states ---------------------------------------


def test_an_armed_host_with_a_hot_wallet_and_no_mint_CAN_pay(monkeypatch):
    """PERMITS. The direction that can regress into dead code without anything failing.

    THIS IS THE HALF THAT PROVES THE MECHANISM IS A CAPABILITY AND NOT A SECOND
    REFUSAL. Every other test in this section asserts a refusal; a guard that
    refused in all four states would satisfy them forever, and the payout path
    would be unreachable from the worker no matter what the operator exported --
    they would arm SOL and the page would still say it cannot pay out. So the set
    of directions is the test rather than any one of them.

    AND THE PREVIEW IS RUN, not just the declaration read. `can_spend` claiming a
    capability is exactly what this file is about, so the claim is checked against
    the thing it claims: preview_payout() applies every guard it can apply without
    a key -- the SPL refusal, the destination, the payer, the cluster's own genesis
    hash, the destination's existence, the rent floor and the payer's balance -- and
    returns a plan. Nothing is signed: the keypair path names no file, and
    preview_payout() never opens one.

    MUTATION: make payout_capability() return (False, ...) unconditionally, or drop
    the final `return True, ""`, and this fails on can_spend with the message below.
    """
    armed(monkeypatch)
    adapter = SeededCluster(url="https://api.devnet.solana.com", hot_wallet=SOL_HOT_WALLET)

    assert adapter.can_spend is True, (
        f"with {KEYPAIR_PATH_VARIABLE} set and SOL_HOT_WALLET configured, SolanaAdapter.can_spend must be "
        f"True. If this fails the payout path is unreachable from the worker whatever the operator "
        f"exports, and every line of it is dead code"
    )
    assert adapter.payout_refusal == ""
    assert why_cannot_pay_out({"SOL": adapter}, "SOL") == "", (
        "an armed adapter with a payer still reports a payout refusal, so the customer page would refuse "
        "a swap the worker could actually pay"
    )

    plan = adapter.preview_payout(SOL_PAYOUT, 1.0)
    assert plan["payer"] == SOL_HOT_WALLET
    assert plan["destination"] == SOL_PAYOUT
    assert plan["signed"] is False and plan["broadcast"] is False
    assert plan["rent_minimum_lamports"] == RENT_FLOOR_LAMPORTS, (
        "the preview did not read the floor off the cluster, so the capability and the preview are not "
        "agreeing about the same configuration"
    )


def test_a_keypair_path_with_NO_HOT_WALLET_cannot_pay(monkeypatch):
    """REFUSES. The state `configure_env.py --arm sol` produces on the operator's host.

    THE MEASUREMENT THIS FILE EXISTS FOR, as a passing refusal. Before 2026-10-11
    can_spend was True here, why_cannot_pay_out() returned "", and preview_payout()
    raised before reading one byte from the cluster -- so a deposit would have been
    taken against a payout that had no payer at all.

    THE PREVIEW IS RUN TOO, and its refusal is the proof that the capability is
    describing something real rather than being newly pessimistic. The assertion is
    the OUTCOME -- this configuration produced this refusal -- not that the code
    contains a check.

    MUTATION: revert chains/solana.py's __init__ to
    `self.can_spend = payout_keypair_is_present()` and this fails on can_spend. Drop
    the hot-wallet branch from payout_capability() and it fails the same way.
    """
    armed(monkeypatch)
    adapter = SeededCluster(url="https://api.devnet.solana.com", hot_wallet="")

    assert adapter.can_spend is False, (
        "a host with a signing keypair and NO SOL_HOT_WALLET reported that it can pay SOL out. Every "
        "* -> SOL pair would badge AVAILABLE, create_swap() would create the swap, and the deposit would "
        "be taken against a payout with no payer"
    )
    assert adapter.payout_refusal == unset_hot_wallet_refusal(), (
        "the adapter must carry the one shared sentence rather than a second copy of it (rule 8)"
    )

    refusal = why_cannot_pay_out({"SOL": adapter}, "SOL")
    assert "SOL_HOT_WALLET" in refusal, (
        "the refusal does not name the variable that would fix it, which is the whole of its usefulness "
        "to an operator reading it off a page or a spawn banner (rule 14)"
    )
    assert KEYPAIR_PATH_VARIABLE in refusal, (
        "the refusal names one of the two variables. The pair is one custody decision, and naming half "
        "of it sends the reader back for a second round"
    )

    with pytest.raises(SolanaRPCError) as refused:
        adapter.preview_payout(SOL_PAYOUT, 1.0)
    assert "SOL_HOT_WALLET is unset" in str(refused.value), (
        "the preview refused for some other reason, so this test is not measuring the unset-payer state"
    )


def test_an_SPL_MINT_means_the_payout_path_cannot_pay(monkeypatch):
    """REFUSES. chains/solana.py has refused an SPL send since the path was written.

    The capability did not know. Measured 2026-10-11: with the keypair path
    exported, SOL_HOT_WALLET set and SOL_SPL_MINT set, can_spend was True,
    why_cannot_pay_out() returned "", and preview_payout() raised
    SolanaSplSendRefused on its FIRST guard with no network call -- so the
    declaration and the guard disagreed, and the declaration is the one every
    rendered surface reads.

    CLAUDE.md's "SPL/wGRC PAYOUT SUPPORT IN THE PYTHON TREE IS UNTOUCHED ...
    chains/solana.py and SOL_SPL_MINT pay an SPL token out" describes the SETTINGS,
    which are read on four branches of that file, not a working send. The send is a
    refusal, and this is where a reader finds that out before a customer does.

    THE MINT USED IS AN ON-CURVE ACCOUNT rather than a real mint, and that is fine
    here: the adapter's constructor validates SOL_SPL_MINT as an ADDRESS and nothing
    on this path asks the cluster about it, because the SPL refusal fires before any
    network call. Using SOL_DEPOSIT_ACCOUNT keeps the fixture a measured address
    rather than an invented one.

    MUTATION: drop the mint branch from payout_capability() and this fails on
    can_spend while the preview still raises -- which is precisely the disagreement
    it is here to close.
    """
    armed(monkeypatch)
    adapter = SeededCluster(
        url="https://api.devnet.solana.com", hot_wallet=SOL_HOT_WALLET, mint=SOL_DEPOSIT_ACCOUNT
    )

    assert adapter.is_spl is True, "test setup: the mint did not reach the adapter"
    assert adapter.can_spend is False, (
        "an SPL-configured adapter reported that it can pay out, and preview_payout() refuses an SPL "
        "send outright. A * -> SOL swap would be created and the deposit taken against a payout that "
        "cannot be built"
    )
    assert adapter.payout_refusal == spl_mint_payout_refusal(SOL_DEPOSIT_ACCOUNT)
    assert "SOL_SPL_MINT" in why_cannot_pay_out({"SOL": adapter}, "SOL")

    with pytest.raises(SolanaSplSendRefused):
        adapter.preview_payout(SOL_PAYOUT, 1.0)


def test_an_OFF_CURVE_hot_wallet_cannot_pay(monkeypatch):
    """REFUSES, and it is STRICTER than the preview rather than equal to it.

    An off-curve SOL_HOT_WALLET is a Program Derived Address: a syntactically
    perfect 32-byte key that no keypair can ever derive to, because a keypair's
    public half is an ed25519 point by construction. So
    chains/solana_signing.derive_and_check() refuses every payout from it -- always,
    on any cluster, with any key file -- and that refusal is unconditional rather
    than probable.

    WHICH IS WHY THE PREVIEW STILL SUCCEEDS HERE, and it is stated rather than
    hidden: preview_payout() checks the destination's curve membership and not the
    PAYER's, so the refusal lives one guard later, inside the signing step this file
    never reaches. Measured 2026-10-11: preview OK, derive_and_check() would refuse.
    So the capability is deliberately narrower than the preview on this one
    configuration, in the direction that refuses before a deposit is taken rather
    than after -- which is the only direction worth being wrong in here.

    AND IT IS NOT HYPOTHETICAL ON THIS CHAIN. An Associated Token Account is where
    an SPL balance lives, it is off the curve, and it is the string somebody pastes
    when they mean "the account holding my tokens". chains/solana.py::
    validate_address() refuses one as a customer's payout destination for the same
    reason, and its docstring carries the full argument.

    MUTATION: drop the is_on_curve() branch from payout_capability() and this fails
    on can_spend.
    """
    armed(monkeypatch)
    pda = off_curve_account()
    adapter = SeededCluster(url="https://api.devnet.solana.com", hot_wallet=pda)

    assert adapter.can_spend is False, (
        f"SOL_HOT_WALLET={pda} is off the ed25519 curve, so no keypair can derive to it and "
        f"derive_and_check() would refuse every payout -- and the adapter reported that it can pay out"
    )
    refusal = why_cannot_pay_out({"SOL": adapter}, "SOL")
    assert "OFF THE ED25519 CURVE" in refusal
    assert pda in refusal, (
        "the refusal does not echo the address that decides it, so an operator cannot tell which of "
        "their settings is wrong (rule 14)"
    )


def test_an_UNARMED_host_still_reports_the_keypair_sentence_and_not_the_payer_one(monkeypatch):
    """Order matters: the keypair path chooses the sentence.

    WHY THIS IS A TEST AND NOT A COMMENT. On an unarmed host -- every checkout,
    every test run, every host where the custody decision has not been made --
    NEITHER variable is set, and the three new refusals are all reachable. Reporting
    "SOL_HOT_WALLET is unset" to somebody who has exported nothing at all would send
    them to configure the second half of a decision they have not started, which is
    the mistake chains/registry.why_unconfigured() records from 2026-09-26: a message
    naming one variable where another is the problem sends a reader to check the
    setting that was already correct.

    So the keypair check runs FIRST, exactly as chains/xrp_payout_seed.
    payout_capability() checks presence before decoding and for the same reason.

    MUTATION: move the hot-wallet branch above the keypair branch in
    payout_capability() and this fails on the sentence equality.
    """
    monkeypatch.delenv(KEYPAIR_PATH_VARIABLE, raising=False)
    assert payout_keypair_is_present() is False, "the suite must not inherit an armed posture"

    adapter = SolanaAdapter(url="http://unreachable.invalid", hot_wallet="")
    assert adapter.can_spend is False
    assert adapter.payout_refusal == missing_keypair_refusal(), (
        "an unarmed host with no hot wallet reported the PAYER refusal. The keypair path is the "
        "variable to set first and is what chooses the sentence"
    )
    assert adapter.payout_refusal != unset_hot_wallet_refusal()


def test_the_capability_is_a_FUNCTION_callable_with_seeded_inputs(monkeypatch):
    """Rule 10: the decision is the smallest testable piece, not a side effect of a constructor.

    FOUR STATES, NO ADAPTER, NO URL. payout_capability() takes the two settings as
    arguments, so every branch can be asserted directly rather than only through an
    object that needs an endpoint to exist. That is what makes the refusal above
    checkable without a cluster, and it is the shape chains/xrp_payout_seed.
    payout_capability() already has.

    AN INVALID ADDRESS IS ALSO COVERED HERE AND NOWHERE ELSE, deliberately.
    chains/solana.py's constructor raises SolanaAddressError for a malformed
    SOL_HOT_WALLET on its own account -- the validation loop at the end of __init__
    -- so that branch is unreachable through the adapter and reachable only here. It
    exists because is_on_curve() RAISES for a string that is not an address at all,
    and a capability check that raised would take a page render with it.

    MUTATION: delete the is_valid_address() branch from payout_capability() and this
    fails with SolanaAddressError escaping instead of a refusal coming back.
    """
    monkeypatch.setenv(KEYPAIR_PATH_VARIABLE, SENTINEL_KEYPAIR_PATH)

    assert payout_capability(SOL_HOT_WALLET, "") == (True, "")

    for label, hot_wallet, mint in (
        ("no payer", "", ""),
        ("whitespace payer", "   ", ""),
        ("a mint", SOL_HOT_WALLET, SOL_DEPOSIT_ACCOUNT),
        ("an off-curve payer", off_curve_account(), ""),
        ("a payer that is not an address at all", "not-base58-at-all-O0Il", ""),
    ):
        can_spend, refusal = payout_capability(hot_wallet, mint)
        assert can_spend is False, f"payout_capability() permitted a payout with {label}"
        assert refusal, f"payout_capability() refused {label} and gave no reason (rule 14)"
        assert refusal.startswith("cannot pay out: "), (
            f"the refusal for {label} does not read correctly after chains/registry.why_cannot_pay_out() "
            f"prefixes the asset: {refusal[:80]!r}"
        )


# --- what the refusal must never contain, and never open ----------------------


def test_no_new_refusal_opens_the_key_file_or_echoes_its_path(monkeypatch):
    """The property this module's header exists for, asserted on all four states.

    can_spend is read by every surface this application renders -- the customer
    page, /admin, the operator panel, the worker spawn banner -- so a capability
    check that opened a key file would mean a key read on page load, by a Flask
    request handler, in a process that is not paying anybody. open() and the three
    Path readers are replaced with refusals for the duration of this test, so a read
    FAILS rather than being detected afterwards.

    AND THE PATH VALUE MUST NOT REACH A SENTENCE. The path is not a secret -- the
    operator typed it -- but it is also not the question a refusal answers, and the
    one place it is deliberately printed is chains/solana_signing.
    describe_keypair_file(), on the armed devnet path, immediately before a
    broadcast. A refusal rendered on a customer page has no reason to carry it.

    MUTATION: have payout_capability() read the file -- for instance to check it
    parses -- and this fails with "a capability check opened ...". Append the path to
    any of the three new sentences and it fails on the sentinel.
    """
    opened: list[str] = []

    def refuse_open(path, *args, **kwargs):
        opened.append(str(path))
        raise AssertionError(f"a capability check opened {path!r}")

    def refuse_path_method(name):
        def refuse(self, *args, **kwargs):
            opened.append(str(self))
            raise AssertionError(f"a capability check called Path.{name} on {self!r}")
        return refuse

    # THE PATCH IS BY DOTTED NAME, SO NOTHING HAS TO BE IMPORTED HERE.
    # tests/test_solana_payout.py's equivalent captures the unpatched Path methods
    # into a local first; this does not, because nothing here calls them back, and an
    # unused local with a comment claiming it is load-bearing is a worse defect than
    # the duplication it would have saved (rule 16: a wrong comment is a bug).
    monkeypatch.setenv(KEYPAIR_PATH_VARIABLE, SENTINEL_KEYPAIR_PATH)
    monkeypatch.setattr("builtins.open", refuse_open)
    for name in ("open", "read_text", "read_bytes"):
        monkeypatch.setattr(f"pathlib.Path.{name}", refuse_path_method(name))

    sentences = []
    for hot_wallet, mint in (
        (SOL_HOT_WALLET, ""),
        ("", ""),
        (SOL_HOT_WALLET, SOL_DEPOSIT_ACCOUNT),
        (off_curve_account(), ""),
    ):
        _can_spend, refusal = payout_capability(hot_wallet, mint)
        sentences.append(refusal)
        # The adapter path too, because that is what a surface actually constructs.
        adapter = SolanaAdapter(url="http://unreachable.invalid", hot_wallet=hot_wallet, mint=mint)
        sentences.append(adapter.payout_refusal)
        sentences.append(repr(vars(adapter)))

    assert opened == [], f"a capability check read from the filesystem: {opened}"
    for sentence in sentences:
        assert SENTINEL_KEYPAIR_PATH not in sentence, (
            "the keypair path value reached a refusal sentence or an adapter attribute"
        )


# --- the end-to-end consequence, through the real services --------------------


def test_an_armed_host_with_no_hot_wallet_does_not_OFFER_a_pair_it_cannot_pay(monkeypatch):
    """The verdict a customer page and /api/health actually render.

    VERIFIED BY OBSERVED OUTCOME, through the real services/pair_view.
    pair_serviceability() rather than by reading that can_spend is consulted. This is
    the surface the 2026-10-03 incident that created pair_view's fifth condition was
    found on -- "the customer page badges BTC -> SOL AVAILABLE ... and a real quote
    for it did not price" -- and the state this test seeds is the same failure one
    step worse: the quote WOULD price, because the rent floor needs no hot wallet, so
    nothing between the badge and an irreversible deposit would have refused.

    THE REFUSAL MUST BE `cannot_pay` SPECIFICALLY. A pair refused for some other
    condition would make this test pass while measuring nothing about the capability,
    which is the trap tests/test_allowed_pairs_are_serviceable.py names in its own
    GRC -> XRP assertion.

    MUTATION: revert chains/solana.py's can_spend derivation and this fails with the
    message below.
    """
    armed(monkeypatch)
    # SOL_NETWORK_FEE_RESERVE IS SEEDED SO THE CAPABILITY IS THE ONLY THING LEFT TO
    # REFUSE. Without it services/quote_service.why_cannot_quote() refuses the pair
    # for the reserve instead, and this test would pass on the mutation's behalf --
    # measured 2026-10-11 while mutation-checking it: with the capability reverted
    # and no reserve seeded, the verdict was still unserviceable and the reason was
    # "nobody has recorded what one SOL payout costs this desk". A test that reports
    # the wrong refusal is the prose-reading defect this repository has committed
    # four times, arriving through a fixture.
    config = {
        "ALLOWED_PAIRS": {("GRC", "SOL")},
        "RPC": {},
        "SOL_DEPOSIT_ACCOUNT": SOL_DEPOSIT_ACCOUNT,
        "SOL_NETWORK_FEE_RESERVE": 0.000005,
    }
    adapters = {
        "GRC": SourceGRC(),
        "SOL": SeededCluster(url="https://api.devnet.solana.com", hot_wallet=""),
    }

    verdict = pair_serviceability(config, adapters, "GRC", "SOL")
    assert verdict["serviceable"] is False, (
        "an armed host with no SOL_HOT_WALLET offered GRC -> SOL. The quote prices in this state, so "
        "offering it is the badge-then-take-a-deposit-then-cannot-pay sequence with nothing in between"
    )
    assert verdict["cannot_pay"], (
        f"the pair was refused for something other than the payout, so this is not measuring the "
        f"capability: {verdict['reason'][:140]}"
    )
    assert "SOL_HOT_WALLET" in verdict["reason"]


def test_an_armed_host_with_no_hot_wallet_cannot_CREATE_a_swap_that_pays_out_in_SOL(tmp_path, monkeypatch):
    """The authority rather than the page, against a real database on the real schema.

    routes/ui.py stops OFFERING an unserviceable pair, and a POST to /api/swaps does
    not come from the page -- so the gate that decides whether a deposit address is
    ever handed out is services/swap_service.create_swap()'s `cannot_pay` check.
    This asserts on the outcome of calling it: a ValueError, and NO row.

    THE ROW COUNT IS THE ASSERTION, not the exception. An exception raised after a
    row was written would leave a swap nobody can pay and a deposit address a
    customer may already have, so "nothing was written" is checked by querying the
    table rather than inferred from the refusal having happened.

    MUTATION: revert chains/solana.py's can_spend derivation and this fails -- the
    swap is created, and the assertion that fires is the one on the refusal, with
    the row count failing right behind it.
    """
    armed(monkeypatch)
    conn = seeded_db(tmp_path)
    seed_quote(conn, "q-to-sol", from_asset="GRC", to_asset="SOL")
    config = {"SOL_DEPOSIT_ACCOUNT": SOL_DEPOSIT_ACCOUNT, "GRC_MIN_CONFIRMATIONS": 6, "SOL_MIN_CONFIRMATIONS": 3}
    adapters = {
        "GRC": SourceGRC(),
        "SOL": SeededCluster(url="https://api.devnet.solana.com", hot_wallet=""),
    }

    with pytest.raises(ValueError) as refused:
        create_swap(conn, config, adapters, "q-to-sol", SOL_PAYOUT)

    assert "SOL_HOT_WALLET" in str(refused.value), (
        f"create_swap() refused for some other reason, so this does not measure the capability: "
        f"{str(refused.value)[:160]}"
    )
    rows = conn.execute("SELECT COUNT(*) AS n FROM swaps").fetchone()
    conn.close()
    assert rows["n"] == 0, (
        "a swap row was written for a payout leg that cannot pay. The deposit address in that row is "
        "what a customer would have sent coin to"
    )


def test_an_armed_host_WITH_a_hot_wallet_CAN_create_a_swap_that_pays_out_in_SOL(tmp_path, monkeypatch):
    """PERMITS, end to end. Without this the refusal above could be a blanket no.

    The same seeded database, the same quote, the same services -- one setting
    different. A row is written, it carries the SOL payout address, and the swap is
    awaiting a deposit. That is the state the operator's devnet rehearsal needs to
    reach, and it is the direction a pessimistic guard would silently remove.

    MUTATION: make payout_capability() return False whenever a mint is absent, or
    invert either of its last two conditions, and this fails while every refusal
    test above still passes -- which is exactly the regression the pair of
    directions exists to catch.

    THE QUOTE IS RESIZED RATHER THAN RE-SEEDED, and the reason is a second real gate
    this test has to get past rather than a convenience. seed_quote() prices 10 units
    at 1000.0 for an output of 9850.0, which is right for GRC and absurd for SOL:
    services/swap_service.refuse_unless_the_payout_can_be_funded() asks the adapter
    for its balance and refused with "the SOL payout wallet holds 2.0 SOL spendable
    and this payout needs 9850.01 SOL" -- measured 2026-10-11 on the first run of
    this test. That refusal is correct and is a DIFFERENT gate, so a test meaning to
    exercise the capability has to clear it. One shared seeding helper with the one
    field under test updated beats a second copy of the INSERT (rule 8), and the
    figures are chosen to clear both gates with the reasoning stated: a 1.0 gross
    retains 0.015 at 150bps, which covers the 0.01 reserve
    services/swap_service.create_swap() tests the desk's own fee against, and a
    0.985 SOL payout plus that reserve fits inside the seeded 2.0 SOL balance and
    sits well above the cluster's 650,240-lamport rent floor.
    """
    armed(monkeypatch)
    conn = seeded_db(tmp_path)
    seed_quote(conn, "q-to-sol", from_asset="GRC", to_asset="SOL")
    conn.execute(
        "UPDATE quotes SET input_amount = ?, quoted_rate = ?, output_amount_estimate = ? WHERE id = ?",
        (1.0, 1.0, 0.985, "q-to-sol"),
    )
    conn.commit()
    config = {"SOL_DEPOSIT_ACCOUNT": SOL_DEPOSIT_ACCOUNT, "GRC_MIN_CONFIRMATIONS": 6, "SOL_MIN_CONFIRMATIONS": 3}
    adapters = {
        "GRC": SourceGRC(),
        "SOL": SeededCluster(url="https://api.devnet.solana.com", hot_wallet=SOL_HOT_WALLET),
    }

    swap = create_swap(conn, config, adapters, "q-to-sol", SOL_PAYOUT)

    assert swap["to_asset"] == "SOL"
    assert swap["payout_address"] == SOL_PAYOUT
    row = conn.execute("SELECT payout_address, status FROM swaps WHERE id = ?", (swap["id"],)).fetchone()
    conn.close()
    assert row["payout_address"] == SOL_PAYOUT, "the payout address must reach the ROW, not just the dict"
    assert row["status"] == "awaiting_deposit"


def test_an_SPL_configured_desk_cannot_take_a_deposit_it_can_never_pay(tmp_path, monkeypatch):
    """THE LOST-DEPOSIT PATH, measured end to end before and after the fix.

    =========================================================================
    THIS IS THE ONE STATE WHERE THE PRE-FIX CAPABILITY TOOK A DEPOSIT
    =========================================================================

    Measured 2026-10-11, with the keypair path exported, SOL_HOT_WALLET set,
    SOL_SPL_MINT set, and `can_spend` forced to the value the old line produced:

        pair_serviceability("GRC","SOL")  serviceable True, cannot_pay ""
        create_swap(...)                  CREATED, deposit_address handed out
        preview_payout(...)               SolanaSplSendRefused

    A customer sends GRC to that address, it is credited, and the payout refuses on
    its first guard. The deposit is irreversible.

    WHY NO OTHER GATE CAUGHT IT, and this is the part worth reading. The funding
    gate -- services/swap_service.refuse_unless_the_payout_can_be_funded(), which
    exists because a GRC payout died on "Insufficient funds" on 2026-10-03 after a
    deposit was irreversible -- DID run, and it passed: chains/solana.py::
    get_balance() on an SPL-configured adapter returns the TOKEN balance from
    getTokenAccountBalance, and it was compared against a payout denominated in
    NATIVE SOL. 100000.0 of a token read as 100000.0 SOL spendable. That is rule
    11's wrong-precision case -- "a value that crosses a boundary with the wrong
    precision is silently wrong by orders of magnitude" -- sitting on the gate that
    authorizes taking a deposit. Both sides of that comparison are correct in
    isolation; the pair is the defect, and a capability that knows an SPL desk
    cannot pay native SOL is what stops them ever being compared.

    CONTRAST WITH THE UNSET-HOT-WALLET STATE, which is NOT this: there
    get_balance() RAISES, so the funding gate refuses and create_swap() says no
    anyway -- measured the same day. That state's cost is a page offering a pair the
    next click cannot create, which is the 2026-10-01 SOL incident (`POST /api/swaps`
    -> 400) rather than a lost deposit. Saying so is the difference between a
    measurement and a scare, and the two are different rows of the same table.

    MUTATION: revert chains/solana.py's can_spend derivation. Measured 2026-10-11:
    the swap is CREATED and this fails on the row count, which is the assertion that
    describes what a customer lost rather than which exception came back.
    """
    armed(monkeypatch)

    class SplCluster(SeededCluster):
        """A desk holding plenty of the configured token and 2 SOL of lamports.

        getTokenAccountBalance is what chains/solana.py::get_balance() calls for an
        SPL-configured adapter, and getAccountInfo has to answer as a MINT here --
        token_program_id_or_default() reads `owner` and mint_decimals() reads
        data.parsed.info.decimals, both off the same response. Seeded generously on
        purpose: a refusal in this test must come from the capability rather than
        from a response this file forgot to supply.
        """

        def call(self, method: str, *params):
            if method == "getTokenAccountBalance":
                return {"value": {"amount": "100000000000000", "decimals": 9}}
            if method == "getAccountInfo":
                return {
                    "value": {
                        "owner": TOKEN_PROGRAM_ID,
                        "data": {"parsed": {"info": {"decimals": 9}}},
                    }
                }
            return super().call(method, *params)

    adapter = SplCluster(
        url="https://api.devnet.solana.com", hot_wallet=SOL_HOT_WALLET, mint=SOL_DEPOSIT_ACCOUNT
    )
    assert adapter.get_balance() == 100_000.0, (
        "test setup: the seeded token balance did not reach get_balance(), so the funding gate this "
        "test is about would not pass even on the mutation"
    )

    conn = seeded_db(tmp_path)
    seed_quote(conn, "q-spl", from_asset="GRC", to_asset="SOL")
    conn.execute(
        "UPDATE quotes SET input_amount = ?, quoted_rate = ?, output_amount_estimate = ? WHERE id = ?",
        (1.0, 1.0, 0.985, "q-spl"),
    )
    conn.commit()
    config = {"SOL_DEPOSIT_ACCOUNT": SOL_DEPOSIT_ACCOUNT, "GRC_MIN_CONFIRMATIONS": 6, "SOL_MIN_CONFIRMATIONS": 3}
    adapters = {"GRC": SourceGRC(), "SOL": adapter}

    with pytest.raises(ValueError) as refused:
        create_swap(conn, config, adapters, "q-spl", SOL_PAYOUT)
    assert "SOL_SPL_MINT" in str(refused.value), (
        f"create_swap() refused for some other reason, so this does not measure the SPL capability: "
        f"{str(refused.value)[:160]}"
    )

    rows = conn.execute("SELECT COUNT(*) AS n FROM swaps").fetchone()
    conn.close()
    assert rows["n"] == 0, (
        "a swap row was written for a desk whose payout path refuses an SPL send outright. The deposit "
        "address in that row is what a customer would have sent coin to, and the payout would have "
        "refused on its first guard with the deposit already credited"
    )


# --- the per-scan attributes, all four of them --------------------------------


def test_every_per_scan_attribute_exists_before_a_scan_has_run():
    """`signatures_skipped` was assigned by the scan and created by nothing.

    MEASURED 2026-10-11 by constructing the real adapter and touching all four:
    unattributable_drops, signatures_listed and unreadable_signatures were
    initialized in __init__ and signatures_skipped was not, so reading it on a fresh
    adapter raised AttributeError. Nothing outside tests/ reads it today -- grepped
    the same day, two lines in chains/solana.py and five assertions in
    tests/test_solana_adapter.py -- so it has cost nothing yet. What it costs is the
    next diagnostic that asks "what did the last scan skip" before a scan has run,
    and an AttributeError out of a reporting line is rule 14's failure wearing a
    traceback.

    THE EMPTY LIST IS THE HONEST ANSWER: a scan that has not run skipped nothing, and
    `signatures_listed == 0` is what tells that apart from a scan that ran and skipped
    nothing.

    MUTATION: delete the `self.signatures_skipped: list[str] = []` line from
    chains/solana.py's __init__ and this fails with AttributeError naming it.
    """
    adapter = SolanaAdapter(url="http://unreachable.invalid")
    assert adapter.unattributable_drops == []
    assert adapter.signatures_listed == 0
    assert adapter.unreadable_signatures == []
    assert adapter.signatures_skipped == [], (
        "signatures_skipped is not initialized in __init__, so a reader that asks what the last scan "
        "skipped before any scan has run gets an AttributeError rather than an answer"
    )
    assert adapter.signatures_read == 0, "listed minus unreadable must be 0 on a fresh adapter"


# --- the preview's round trips, counted ---------------------------------------


class CountingCluster(SeededCluster):
    """SeededCluster that records every (method, params) it is asked.

    THE RECORD IS THE ASSERTION. tests/test_solana_payout.py's payout_adapter()
    does the same thing for the same reason -- "the code contains a check for X"
    is never evidence X holds, and "the preview reads the floor once" is a claim
    about calls, so the calls are what gets counted.
    """

    def __init__(self, *args, **kwargs):
        # BEFORE super().__init__(), because SolanaAdapter's constructor can raise
        # (the SOL_SPL_MINT / SOL_HOT_WALLET validation loop at its end) and a
        # partially built instance whose `calls` list does not exist yet is harder
        # to read in a traceback than one whose list is empty.
        self.calls: list[tuple] = []
        super().__init__(*args, **kwargs)

    def call(self, method: str, *params):
        self.calls.append((method, params))
        return super().call(method, *params)


def test_the_preview_reads_the_rent_floor_ONCE_for_both_of_its_uses(monkeypatch):
    """Two uses of one cluster parameter, one read. It was two reads until 2026-10-11.

    preview_payout() needs getMinimumBalanceForRentExemption(0) twice over: the
    floor a NEW destination account must hold, and the floor the PAYER must retain.
    It asked for each separately. Measured by counting the calls:

        destination does not exist   5 RPC calls, 2 of them this one
        destination exists           4 RPC calls, 1 of them this one

    So the ordinary case -- a payout to a customer address nobody has used, which is
    the only case where the destination floor matters at all -- paid for the
    identical question twice, immediately before a send, against an endpoint whose
    HTTP 429 is a measured failure in this tree.

    THE STRONGER REASON IS THAT IT IS ONE NUMBER (rule 8). chains/solana_units.py's
    rent section records SIMD-0437 moving this figure twice, so two reads can
    disagree -- and a preview that applied one value to the destination and another
    to the payer's retention would be internally inconsistent in the block an
    operator reads before authorizing a transfer.

    `rent_minimum_lamports` IS STILL 0 FOR AN EXISTING DESTINATION, asserted here
    because that is a contract OUTSIDE this file: sol_payout_preview.py reads it and
    treats 0 as "not asked about the destination", after printing an unasked zero as
    a figure on the operator's screen within an hour of shipping.
    tests/test_sol_payout_preview.py::test_an_UNASKED_floor_is_never_printed_as_a_figure
    pins the consequence; this pins the input to it.

    MUTATION: put `self.rent_exempt_minimum(SYSTEM_ACCOUNT_SPACE)` back in the
    `retain` line and this fails on the count of 1.
    """
    armed(monkeypatch)

    class ExistingDestination(CountingCluster):
        def call(self, method: str, *params):
            if method == "getAccountInfo":
                self.calls.append((method, params))
                return {"value": {"lamports": 1}}
            return super().call(method, *params)

    for label, adapter in (
        ("a destination that does not exist", CountingCluster(
            url="https://api.devnet.solana.com", hot_wallet=SOL_HOT_WALLET)),
        ("a destination that already exists", ExistingDestination(
            url="https://api.devnet.solana.com", hot_wallet=SOL_HOT_WALLET)),
    ):
        plan = adapter.preview_payout(SOL_PAYOUT, 1.0)
        methods = [method for method, _params in adapter.calls]
        assert methods.count("getMinimumBalanceForRentExemption") == 1, (
            f"with {label} the preview asked the cluster for the 0-byte rent floor "
            f"{methods.count('getMinimumBalanceForRentExemption')} times. Two reads of one parameter can "
            f"disagree, and the preview would then hold two floors: {methods}"
        )
        assert ("getMinimumBalanceForRentExemption", (0,)) in adapter.calls, (
            "the floor was read for some space other than 0 bytes, which is a different quantity -- a "
            "165-byte token account's minimum was 1,488,440 against a 0-byte account's 650,240"
        )
        assert plan["retain_lamports"] == RENT_FLOOR_LAMPORTS, (
            "the payer's retention is not the floor the cluster reported, so the one reading is not "
            "reaching both uses"
        )

    # AND THE 0-FOR-AN-EXISTING-DESTINATION CONTRACT, stated separately because it
    # is what a DIFFERENT file depends on.
    existing = ExistingDestination(url="https://api.devnet.solana.com", hot_wallet=SOL_HOT_WALLET)
    plan = existing.preview_payout(SOL_PAYOUT, 1.0)
    assert plan["destination_exists"] is True
    assert plan["rent_minimum_lamports"] == 0, (
        "rent_minimum_lamports is no longer 0 for an existing destination. sol_payout_preview.py reads "
        "that 0 as 'the floor was not asked about the destination' and will start printing a real "
        "figure as if it applied to the send"
    )
    assert "does not apply to the DESTINATION" in plan["description"], (
        "the preview still tells the operator the floor was 'not asked', which stopped being true when "
        "it became one reading used for both questions (rule 16: a wrong comment is a bug, and a line "
        "an operator reads off a screen before authorizing a transfer is worse than a comment)"
    )


# --- item 2: a pair that reports AVAILABLE must be able to PRICE ---------------
#
# services/pair_view.pair_serviceability()'s fifth condition exists because of a
# measured failure on 2026-10-03: "the customer page badges BTC -> SOL AVAILABLE --
# Ready to quote now. -- and a real quote for it did not price", because a SOL
# payout has a rent-exemption minimum that comes from the CLUSTER and the page was
# answering from config alone. The two tests below pin both directions of that
# coupling for every `* -> SOL` pair the operator has enabled, rather than for the
# one that happened to be clicked.

SOL_DESTINATION_PAIRS = sorted(pair for pair in Config.ALLOWED_PAIRS if pair[1] == "SOL")

#: THE DEPOSIT ACCOUNT EACH SOURCE CHAIN NEEDS IN CONFIG, per asset and not per
#: alphabet. services/swap_service.TAG_ATTRIBUTION names one shared account for each
#: tag-attributed chain -- XRP_DEPOSIT_ACCOUNT and SOL_DEPOSIT_ACCOUNT -- and
#: services/swap_service.why_cannot_take_deposits() checks it with THAT CHAIN'S OWN
#: validator. Measured 2026-10-11 on the first run of the parametrized tests below:
#: seeding XRP_DEPOSIT_ACCOUNT with a Solana address refused XRP -> SOL with "XRP
#: cannot take deposits: XRP_DEPOSIT_ACCOUNT is unset or not a valid account", which
#: is correct and is the wrong refusal for a test about a Solana rent floor.
#:
#: That is the 2026-10-01 defect in a fixture: services/xrp_tag_service.
#: validate_account() checked every tag chain's account against the XRP Ledger's
#: alphabet, so the second tag chain was refused at the last step by the first
#: chain's address format. A test that seeds one address for both chains would make
#: the same mistake in the harness, where it passes while proving nothing.
#:
#: BTC, LTC and GRC need no entry: their deposit address is DERIVED per swap by the
#: adapter's get_new_address(), so nothing in config gates them. ICP needs none
#: either -- it is not in TAG_ATTRIBUTED_ASSETS.
SOURCE_DEPOSIT_SETTINGS = {
    "SOL": {"SOL_DEPOSIT_ACCOUNT": SOL_DEPOSIT_ACCOUNT},
    "XRP": {"XRP_DEPOSIT_ACCOUNT": valid_addresses.XRP_HOT_ACCOUNT},
}


class StubSource:
    """A source chain that can take a deposit, for ANY of the five assets.

    WHY NOT SourceGRC FOR ALL FIVE, which is what the first version of the
    parametrized tests below did. services/swap_service.deposit_account() calls
    `adapters[from_asset].validate_address(account)` on the shared account for a
    tag-attributed chain, and SourceGRC's validator decodes a GRIDCOIN version byte
    -- so XRP -> SOL was refused with "XRP_DEPOSIT_ACCOUNT is unset or not a valid
    account" about an account that is perfectly valid. Measured 2026-10-11 on the
    first run.

    That is the 2026-10-01 blocker reappearing in a fixture: one chain's address
    format applied to another chain's account. tests/test_sol_swap_creation.py exists
    because services/xrp_tag_service.validate_account() did exactly that to SOL, and
    a harness that repeats it passes while proving nothing about the pair it names.

    THE VALIDATOR IS modules/address_authority.check_address(), WHICH IS THE REAL
    AUTHORITY rather than my opinion of each alphabet. It is the same function
    services/swap_service.payout_source_account() and the payout worker's burn guard
    use, it needs no network, and it knows all six assets this tree trades -- so a
    stub built on it cannot agree with the code under test by construction, which is
    the failure mode a hand-written per-chain validator would have.
    """

    can_spend = True
    payout_refusal = ""

    def __init__(self, asset: str) -> None:
        self.asset = asset

    def validate_address(self, address):
        return check_address(self.asset, address).state == "VALID"

    def owns_address(self, address):
        """Not the desk's: every fixture here is a customer destination or a shared account."""
        return False

    def get_new_address(self, label):
        """Only reached for an address-attributed source (BTC, LTC, GRC)."""
        return valid_addresses.GRC_DESK_DEPOSIT


def source_config(from_asset: str, to_asset: str) -> dict:
    """The smallest config under which `from_asset -> to_asset` could be serviceable.

    SOL_NETWORK_FEE_RESERVE IS ALWAYS PRESENT, because without it
    services/quote_service.why_cannot_quote() refuses the pair for the reserve and
    every assertion about the rent floor below would be reporting the wrong
    refusal -- measured 2026-10-11 while mutation-checking these tests.
    """
    config = {
        "ALLOWED_PAIRS": {(from_asset, to_asset)},
        "RPC": {},
        "SOL_NETWORK_FEE_RESERVE": 0.000005,
    }
    config.update(SOURCE_DEPOSIT_SETTINGS.get(from_asset, {}))
    config.update(SOURCE_DEPOSIT_SETTINGS.get(to_asset, {}))
    return config



def test_SOL_is_a_destination_in_five_pairs_and_a_source_in_five():
    """The denominator, measured off the real Config rather than counted in prose.

    Re-counted 2026-10-11: 30 allowed pairs over six assets, TEN with SOL in them,
    FIVE with SOL as the DESTINATION (BTC, GRC, ICP, LTC, XRP -> SOL) and FIVE with
    SOL as the SOURCE. chains/solana.py and chains/solana_payout_keypair.py both
    carried "Config.ALLOWED_PAIRS names no pair that pays out in SOL (counted
    2026-10-03)" until today, which is rule 3's error -- a count in prose beside a
    set that grew -- and it was the clause that made an over-claiming capability
    look harmless.

    ASSERTED SO THE NEXT DRIFT FAILS A TEST rather than aging in a comment. If the
    operator enables or removes a SOL direction, this fails and names the figure to
    correct, which is the only mechanism that has ever worked on these counts.

    MUTATION: remove any `* -> SOL` pair from Config.ALLOWED_PAIRS and this fails
    naming the new total.
    """
    sources = sorted(pair for pair in Config.ALLOWED_PAIRS if pair[0] == "SOL")
    assert len(SOL_DESTINATION_PAIRS) == 5, (
        f"SOL is the payout destination in {len(SOL_DESTINATION_PAIRS)} allowed pairs, not 5: "
        f"{SOL_DESTINATION_PAIRS}. Every one of them is a direction where can_spend decides whether a "
        f"deposit is taken"
    )
    assert len(sources) == 5, f"SOL is the source in {len(sources)} allowed pairs, not 5: {sources}"
    assert [to for _from, to in SOL_DESTINATION_PAIRS] == ["SOL"] * 5


@pytest.mark.parametrize(("from_asset", "to_asset"), SOL_DESTINATION_PAIRS)
def test_an_UNREACHABLE_cluster_makes_every_SOL_destination_pair_UNAVAILABLE(from_asset, to_asset, monkeypatch):
    """A terminal that cannot ask the cluster cannot honestly badge the pair AVAILABLE.

    THE DEGRADATION DIRECTION, which is the one pair_view's fifth condition was
    added for. A SOL payout's floor comes from getMinimumBalanceForRentExemption, so
    a terminal that cannot reach the cluster does not know the smallest deliverable
    payout -- and a page that cannot ask must not offer a pair the next click cannot
    price.

    Measured 2026-10-11 with every RPC call raising: services/quote_service.
    why_cannot_establish_payout_floor() returns "the Solana cluster could not be
    asked for the rent-exempt minimum ..." and pair_serviceability() is not
    serviceable. Asserted for ALL FIVE destination pairs rather than for BTC -> SOL
    alone, because the 2026-10-03 incident was found by clicking one of them.

    THE CACHE IS CLEARED FIRST and that is load-bearing rather than hygiene:
    services/quote_service.new_account_floor_lamports() caches per endpoint for 600
    seconds, so a figure another test read for this url would be returned without
    the cluster being asked and this test would measure the cache instead of the
    degradation. The url is also distinct per parametrization for the same reason.

    MUTATION: drop `cannot_establish` from pair_serviceability()'s conjunction and
    this fails -- which is the regression that would restore the 2026-10-03 page.
    """
    armed(monkeypatch)
    quote_service._RENT_FLOOR_CACHE.clear()

    class DeadCluster(SolanaAdapter):
        """Every call raises, which is what an unreachable endpoint looks like."""

        def call(self, method: str, *params):
            raise SolanaRPCError(f"{method} could not reach {self.url}: seeded transport failure")

    adapter = DeadCluster(url=f"http://unreachable-{from_asset.lower()}.invalid", hot_wallet=SOL_HOT_WALLET)
    assert adapter.can_spend is True, (
        "this test is about an unreachable CLUSTER, not an unarmed host: the capability has to be True "
        "or it measures the wrong refusal"
    )

    floor_refusal = why_cannot_establish_payout_floor({"SOL": adapter}, "SOL")
    assert floor_refusal, (
        "the cluster is unreachable and the page would still claim the smallest deliverable SOL payout "
        "is known"
    )
    assert "rent-exempt minimum" in floor_refusal

    config = source_config(from_asset, to_asset)
    verdict = pair_serviceability(config, {from_asset: StubSource(from_asset), "SOL": adapter}, from_asset, to_asset)
    assert verdict["serviceable"] is False, (
        f"{from_asset} -> {to_asset} was offered while the Solana cluster could not be asked for the "
        f"rent floor, so the quote behind it cannot price. That is the exact page the fifth condition "
        f"in services/pair_view.pair_serviceability() was added for on 2026-10-03"
    )
    assert verdict["cannot_establish"], (
        f"the pair was refused for something other than the unaskable floor, so this does not measure "
        f"the degradation: {verdict['reason'][:140]}"
    )


@pytest.mark.parametrize(("from_asset", "to_asset"), SOL_DESTINATION_PAIRS)
def test_a_REACHABLE_cluster_lets_the_page_and_the_quote_agree_about_the_floor(from_asset, to_asset, monkeypatch):
    """AVAILABLE must mean priceable: the page's condition and the quote's gate, together.

    THE COUPLING, asserted rather than assumed. services/pair_view.
    pair_serviceability()'s fifth condition and services/quote_service.
    require_deliverable_sol_payout() go through the SAME function --
    new_account_floor_lamports(), with the same 600-second per-endpoint cache -- so
    that "the page says available" and "the quote can price" cannot come apart. This
    checks that they do not: when the floor CAN be established, a payout above the
    floor is not refused, and a payout below it is -- for every enabled `* -> SOL`
    direction.

    THE BELOW-FLOOR CASE IS CHECKED TOO, because a gate that permits everything
    would satisfy the half above forever. 1,000 lamports is 0.000001 SOL, far under
    the cluster's 650,240, and it is exactly the shape of payout that would be
    broadcast and never arrive: a native transfer that would CREATE an account
    below its rent-exempt minimum is rejected by the runtime, so it does not arrive
    small -- it does not arrive.

    THE PAGE IS NOT RE-ASKED IN THE BELOW-FLOOR CASE, deliberately, and the
    distinction is the whole reason there are two functions. The pair is serviceable
    either way: whether a PARTICULAR amount clears the floor is a question about one
    quote, and the page knows no amount. Conflating them would badge a pair
    UNAVAILABLE because somebody once asked for a tiny payout.

    MUTATION: make why_cannot_establish_payout_floor() return a reason
    unconditionally and the first assertion fails; make
    require_deliverable_sol_payout() return early for SOL and the last one does.
    """
    armed(monkeypatch)
    quote_service._RENT_FLOOR_CACHE.clear()
    adapter = SeededCluster(url=f"https://devnet-{from_asset.lower()}.invalid", hot_wallet=SOL_HOT_WALLET)

    assert why_cannot_establish_payout_floor({"SOL": adapter}, "SOL") == "", (
        "the floor could not be established against a cluster that answers "
        f"getMinimumBalanceForRentExemption with {RENT_FLOOR_LAMPORTS}"
    )

    config = source_config(from_asset, to_asset)
    verdict = pair_serviceability(config, {from_asset: StubSource(from_asset), "SOL": adapter}, from_asset, to_asset)
    assert verdict["serviceable"] is True, (
        f"{from_asset} -> {to_asset} is in Config.ALLOWED_PAIRS, the source can take a deposit, the "
        f"destination is armed with a payer and the cluster answers -- and the page still would not "
        f"offer it: {verdict['reason'][:160]}"
    )

    # ABOVE THE FLOOR: the quote's own gate must not refuse what the page offered.
    require_deliverable_sol_payout(config, {"SOL": adapter}, "SOL", 1.0)

    # BELOW THE FLOOR: it must refuse, in SOL, naming no internal setting.
    with pytest.raises(ValueError) as refused:
        require_deliverable_sol_payout(config, {"SOL": adapter}, "SOL", 0.000001)
    message = str(refused.value)
    assert "too small to deliver" in message, message
    for internal in ("SOL_PAYOUT_KEYPAIR_PATH", "SOL_HOT_WALLET", "lamport", "getMinimum"):
        assert internal not in message, (
            f"the customer-facing refusal names the internal {internal!r}, which commit 6ae5838 stripped "
            f"off the customer page on 2026-10-02: {message}"
        )



# --- AST helpers for the structural test below --------------------------------
#
# AT MODULE LEVEL BECAUSE OF C901, AND THAT IS THE RIGHT FIX RATHER THAN A
# WORKAROUND. These three were nested closures inside the test and ruff measured the
# function at complexity 11 against a ceiling of 10. CLAUDE.md rule 12 is explicit
# about which direction to resolve that in: "A main() past the ceiling is
# orchestration that has swallowed decisions ... The fix is to extract the decision
# so it can be called with seeded inputs, not to raise the ceiling." So the three
# decisions -- what counts as a mention, what counts as a call, which function
# encloses it -- are functions, and the test is the assertions.

#: chains/solana.py's source, read once at import. Parsed rather than imported: this
#: is a question ABOUT the source, and importing the module to ask it would make the
#: gate depend on the dependency set rather than on the code.
SOLANA_SOURCE = (
    Path(__file__).resolve().parent.parent / "swap_terminal" / "chains" / "solana.py"
).read_text(encoding="utf-8")


def functions_containing(tree, predicate) -> set[str]:
    """The names of every function in `tree` whose body holds a node `predicate` accepts."""
    return {
        node.name
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef) and any(predicate(child) for child in ast.walk(node))
    }


def mentions_string(value: str):
    """A predicate: this node is the string literal `value`."""

    def check(node) -> bool:
        return isinstance(node, ast.Constant) and node.value == value

    return check


def calls_named(name: str):
    """A predicate: this node is a call to `name`, whether as a bare name or an attribute.

    BOTH FORMS, because the three calls this file asks about are spelled both ways:
    `self._sign_and_broadcast(...)` and `self.preview_payout(...)` are attributes,
    `require_devnet(...)` is a bare imported name. A predicate that knew only one
    would report zero callers and the assertion would pass by finding nothing.
    """

    def check(node) -> bool:
        if not isinstance(node, ast.Call):
            return False
        if isinstance(node.func, ast.Attribute):
            return node.func.attr == name
        return isinstance(node.func, ast.Name) and node.func.id == name

    return check


# --- item 4: nothing can reach a send with an unproven cluster -----------------


def test_the_only_route_to_sendTransaction_runs_the_devnet_guard_first():
    """A STRUCTURAL assertion, because the behavioral one cannot cover a route nobody wrote yet.

    tests/test_solana_payout.py already proves the BEHAVIOR from both ends -- an
    unarmed preview and a fully armed send both refuse a mainnet genesis with
    `methods(adapter) == ["getGenesisHash"]`, so the refusal is before the balance
    read rather than merely before the broadcast. What those tests cannot see is a
    NEW method added to chains/solana.py that calls _sign_and_broadcast() without
    going through preview_payout() first: it would pass every existing test by not
    being exercised by any of them.

    So this walks the AST of chains/solana.py and asserts the shape the guard rests
    on, measured 2026-10-11:

        sendTransaction        appears in exactly one function: _sign_and_broadcast
        _sign_and_broadcast    is called from exactly one function: send_to_address
        send_to_address        calls preview_payout, which calls require_devnet

    PARSED RATHER THAN IMPORTED, the same judgment
    tests/test_every_payable_adapter_derives_can_spend.py records: this is a question
    about the source, and importing the module to ask it would make the gate depend
    on the dependency set rather than on the code.

    THE CHAIN IS WHAT MATTERS, NOT ANY ONE LINK. require_devnet() is reached only
    through preview_payout(), so if a second caller of _sign_and_broadcast() ever
    appears this fails and names it -- which is the only way a route that skips the
    cluster check becomes visible before somebody broadcasts through it.

    MUTATION: add a method to chains/solana.py that calls self._sign_and_broadcast()
    and this fails naming it. Move the require_devnet() call out of preview_payout()
    and it fails on the last assertion.
    """
    tree = ast.parse(SOLANA_SOURCE)

    def enclosing_functions(predicate) -> set[str]:
        return functions_containing(tree, predicate)

    senders = enclosing_functions(mentions_string("sendTransaction"))
    assert senders == {"_sign_and_broadcast"}, (
        f"the literal 'sendTransaction' appears in {sorted(senders)}. Every broadcast must go through "
        f"_sign_and_broadcast(), which is the only function downstream of the devnet guard"
    )

    signers = enclosing_functions(calls_named("_sign_and_broadcast"))
    assert signers == {"send_to_address"}, (
        f"_sign_and_broadcast() is called from {sorted(signers)}. A second caller is a route to a "
        f"broadcast that has not been through preview_payout(), and therefore has not been through "
        f"chains/solana_signing.require_devnet()"
    )

    assert "send_to_address" in enclosing_functions(calls_named("preview_payout")), (
        "send_to_address() no longer calls preview_payout(), so the cluster, the payer, the rent floor "
        "and the balance are not checked before the arming token is tested"
    )
    assert "preview_payout" in enclosing_functions(calls_named("require_devnet")), (
        "preview_payout() no longer calls require_devnet(), so this path can be pointed at mainnet-beta "
        "-- which is the one outcome the whole SOL payout path is built to make unreachable"
    )
