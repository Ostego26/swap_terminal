"""The Solana adapter against seeded RPC responses, and its two refusals.

Role: test (the real adapter methods; the transport is stubbed, nothing else is)
Reads: swap_terminal/chains/solana.py and chains/registry.py
Writes: nothing
Can move funds: no. NOTHING HERE OPENS A SOCKET -- every `call` is replaced by
      a table of seeded responses, so no test can reach a cluster even by
      accident, and the adapter holds no key in any case.
Mainnet-safe: yes

HOW THIS VERIFIES, AND WHAT IT CANNOT (CLAUDE.md's verification principle).

The principle asks for the REAL script run against seeded conditions, with
assertions on what actually came out -- never "the code contains a check for
X". So these tests seed real-shaped Solana JSON-RPC responses and call the real
find_deposits_to_address(), get_balance() and build_transfer_plan(). The
decoding, the balance-delta arithmetic, the failed-transaction skip, the SPL
owner+mint matching and the commitment ranking are all exercised as written.

WHAT IS STUBBED IS THE TRANSPORT AND ONLY THE TRANSPORT. `SolanaAdapter.call`
is the one method replaced, and its replacement returns responses shaped as
Solana's documentation and the Node bridge's own usage describe them.

WHAT THAT DOES NOT ESTABLISH, said plainly rather than left to be assumed
(rule 17): **no test here proves the RPC method names, parameter shapes or
response fields match a real cluster.** No Solana endpoint was reachable from
the environment this was written in -- api.devnet.solana.com returned 403 from
the proxy and solana-test-validator could not be installed, because its only
distribution channels are release.anza.xyz and github.com, both of which are
denied as well. If a field name below is wrong, these tests pass and the
adapter fails on the operator's first real call. That is the honest boundary,
and closing it is the operator's run.
"""

from __future__ import annotations

import logging
import tokenize
from pathlib import Path

import chains.solana as chains_solana
import pytest
from chains.registry import build_adapters
from chains.solana import SolanaAdapter, SolanaRPCError, deposit_event
from chains.solana_address import SolanaAddressError
from chains.solana_units import (
    BALANCE_COMMITMENT,
    COMMITMENT_RANKS,
    DISCOVERY_COMMITMENT,
    FINALIZED_RANK,
    LOWEST_COMMITMENT_THE_HISTORY_METHODS_ACCEPT,
)
from config import Config
from services.swap_service import TAG_ATTRIBUTED_ASSETS, TAG_ATTRIBUTION

WALLET = "BGdUSPGWiwStabibSXwiLwJCsk6iXDTeyL6fgWbNcAvN"
WALLET_WSOL_ATA = "2TJwPdpDwgGrcQjW5K5E2uNxEqBTNkQsZ46axFy5bNm3"
WSOL_MINT = "So11111111111111111111111111111111111111112"
OTHER = "SysvarRent111111111111111111111111111111111"
SIG = "5j7s6NiJS3JAkvgkoc18WVAsiSaci2pxB2A6ueCJP4tprA2TFg9wSyTLeYouxPBJEMzJinENTkpA52YStRW5Dia7"
SIG2 = "4k8t7OjKT4KBlwhlpd29XWBtjTbdj3qyC3B7vfDKQ5uqsB3UGh0xTzUMfZpvyQCKFNaKjoFOUlqB63ZTtSX6Ejb8"


def make_adapter(responses: dict, **kwargs) -> SolanaAdapter:
    """A real SolanaAdapter whose ONLY stubbed member is the transport.

    `responses` maps an RPC method name to either a value or a callable taking
    the call's params. Everything else on the adapter -- validation, decoding,
    arithmetic, the branches -- is the shipped code.
    """
    adapter = SolanaAdapter(url="http://seeded.invalid", **kwargs)
    calls = []

    def fake_call(method, *params):
        calls.append((method, params))
        if method not in responses:
            raise AssertionError(f"the adapter called {method}, which this test did not seed")
        value = responses[method]
        return value(*params) if callable(value) else value

    adapter.call = fake_call
    adapter.calls = calls
    return adapter


#: The tag these fixtures' deposits carry. A FIXTURE THAT CARRIES ONE IS THE ORDINARY CASE
#: since 2026-09-29: every SOL swap shares one deposit account under the memo strategy, so a
#: deposit WITHOUT a memo is the exceptional one and gets its own tests below rather than being
#: the silent default here.
FIXTURE_TAG = 4242


def memo_instruction(text):
    return {"programId": "MemoSq4gqABAXKb96qnH8TysNcWxMyWCqXgDLGmfcHr", "parsed": str(text)}


def native_tx(keys, pre, post, err=None, memo=FIXTURE_TAG):
    instructions = [] if memo is None else [memo_instruction(memo)]
    return {
        "meta": {"err": err, "preBalances": pre, "postBalances": post},
        "transaction": {"message": {"accountKeys": [{"pubkey": k} for k in keys],
                                    "instructions": instructions}},
    }


def token_balance(index, owner, mint, amount, decimals=6):
    return {
        "accountIndex": index,
        "owner": owner,
        "mint": mint,
        "uiTokenAmount": {"amount": str(amount), "decimals": decimals},
    }


# --- construction ------------------------------------------------------------


def test_construction_opens_no_socket():
    """workers/common.py's header promises build_adapters() opens no socket, and
    three polling workers rely on it at import. A constructor that called out
    would make importing a worker depend on a cluster being up."""
    adapter = SolanaAdapter(url="http://never.invalid", hot_wallet=WALLET)
    assert adapter.url == "http://never.invalid"
    assert adapter.asset == "SOL"


def test_a_bitcoin_style_confirmation_threshold_is_refused_at_construction():
    """The stall this prevents is silent and permanent -- see
    tests/test_solana_units.py. Refusing at construction means the operator
    sees it when they start a worker, not when a customer complains."""
    with pytest.raises(ValueError, match="not a rung"):
        SolanaAdapter(url="http://x.invalid", min_commitment_rank=6)


def test_a_typo_in_the_mint_or_hot_wallet_is_refused_at_construction():
    with pytest.raises(SolanaAddressError, match="SOL_SPL_MINT"):
        SolanaAdapter(url="http://x.invalid", mint="not-an-address")
    with pytest.raises(SolanaAddressError, match="SOL_HOT_WALLET"):
        SolanaAdapter(url="http://x.invalid", hot_wallet="also-not")


def test_an_unset_url_refuses_loudly_instead_of_defaulting_to_somebody_s_cluster():
    with pytest.raises(SolanaRPCError, match="SOL_RPC_URL"):
        SolanaAdapter().get_slot()


# --- validate_address --------------------------------------------------------


def test_a_wallet_address_validates_and_a_token_account_does_not():
    """The money case. A customer who pastes their token account instead of
    their wallet hands over a string that passes every syntactic check, and
    native SOL sent there is not recoverable by them."""
    adapter = SolanaAdapter(url="http://x.invalid")
    assert adapter.validate_address(WALLET)
    assert not adapter.validate_address(WALLET_WSOL_ATA)


def test_validate_address_never_raises_and_never_needs_the_network():
    """chains/base.py must RAISE when a daemon is unreachable, because a
    transport failure and a bad address both look like False. That confusion is
    structurally impossible here: this answers locally, always."""
    adapter = SolanaAdapter()  # no URL at all
    assert adapter.validate_address("garbage!!") is False
    assert adapter.validate_address(WALLET) is True


def test_describe_payout_address_names_the_token_account_a_payout_would_land_in():
    adapter = make_adapter({"getAccountInfo": {"value": {"owner": "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA"}}}, mint=WSOL_MINT)
    line = adapter.describe_payout_address(WALLET)
    assert WALLET_WSOL_ATA in line
    assert "associated token account" in line


# --- get_balance -------------------------------------------------------------


def test_native_balance_comes_back_in_sol_not_lamports():
    adapter = make_adapter({"getBalance": {"value": 2_500_000_000}}, hot_wallet=WALLET)
    assert adapter.get_balance() == 2.5


def test_an_spl_balance_is_read_from_the_token_account_at_the_mints_decimals():
    """Not getBalance, which would report the account's LAMPORTS and be wrong by
    whatever the token is worth."""
    adapter = make_adapter(
        {
            "getAccountInfo": {"value": {"owner": "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA"}},
            "getTokenAccountBalance": {"value": {"amount": "1234500000", "decimals": 6}},
        },
        mint=WSOL_MINT,
        hot_wallet=WALLET,
    )
    assert adapter.get_balance() == 1234.5
    assert any(method == "getTokenAccountBalance" for method, _ in adapter.calls)
    assert not any(method == "getBalance" for method, _ in adapter.calls)


def test_a_missing_token_account_reads_as_zero_but_an_outage_does_not():
    """An owner with no token account for a mint holds none of that token, so
    zero is the ANSWER. Every other failure still raises -- the caller must
    never read an outage as an empty wallet (CLAUDE.md rule 12's BLE001)."""
    def absent(*_params):
        raise SolanaRPCError("getTokenAccountBalance failed: {'message': 'Invalid param: could not find account'}")

    def outage(*_params):
        raise SolanaRPCError("getTokenAccountBalance could not reach http://x: timed out")

    common = {"getAccountInfo": {"value": {"owner": "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA"}}}
    assert make_adapter({**common, "getTokenAccountBalance": absent}, mint=WSOL_MINT, hot_wallet=WALLET).get_balance() == 0.0
    with pytest.raises(SolanaRPCError, match="could not reach"):
        make_adapter({**common, "getTokenAccountBalance": outage}, mint=WSOL_MINT, hot_wallet=WALLET).get_balance()


def test_get_balance_refuses_rather_than_reporting_zero_with_no_wallet_configured():
    with pytest.raises(SolanaRPCError, match="SOL_HOT_WALLET"):
        make_adapter({}).get_balance()


# --- find_deposits_to_address: native SOL ------------------------------------


def test_a_native_credit_is_the_balance_delta_at_the_accounts_own_index():
    adapter = make_adapter(
        {
            "getSignaturesForAddress": [{"signature": SIG, "err": None, "confirmationStatus": "finalized"}],
            "getTransaction": native_tx([OTHER, WALLET], [9_000_000_000, 1_000_000_000], [8_000_000_000, 3_000_000_000]),
        }
    )
    events = adapter.find_deposits_to_address(WALLET)
    assert events == [{"txid": SIG, "vout": FIXTURE_TAG, "address": WALLET, "amount": 2.0, "confirmations": 3}]


def test_vout_IS_THE_MEMO_TAG_AND_NO_LONGER_THE_ACCOUNT_INDEX():
    """This test asserted the ACCOUNT INDEX until 2026-09-29, and the change is the deposit
    model rather than a detail.

    Under the one-account-plus-memo strategy the operator chose, every SOL swap shares ONE
    deposit account -- so the address no longer identifies the swap and `vout` is what does.
    services/deposit_service.attributable_events() matches `event["vout"]` against the swap's
    own deposit_tag, exactly as chains/xrp_payments.py already arranged for the DestinationTag.
    One contract, two chains.

    THE OLD PROPERTY IT PROTECTED SURVIVES: `vout` is still never fabricated. The index was
    there for UNIQUE(asset, txid, vout), and the memo serves that too -- Solana's account model
    gives one net balance delta per account per transaction, so a deposit is one row either way.
    What the index could not do is say whose money it is.
    """
    adapter = make_adapter(
        {
            "getSignaturesForAddress": [{"signature": SIG, "err": None, "confirmationStatus": "confirmed"}],
            "getTransaction": native_tx([OTHER, OTHER, WALLET], [5, 5, 100], [5, 5, 600]),
        }
    )
    assert adapter.find_deposits_to_address(WALLET)[0]["vout"] == FIXTURE_TAG


def test_AN_UNATTRIBUTABLE_DEPOSIT_IS_DROPPED_AND_NEVER_FALLS_BACK_TO_THE_INDEX(caplog):
    """A credit with no memo is dropped, and dropping it is safer than stamping it.

    Returning the account index -- what `vout` used to carry -- would be WORSE than returning
    nothing. attributable_events() compares `vout` against the swap's deposit_tag, and a small
    integer read off the transaction could COLLIDE with a real tag and credit a stranger's
    deposit to somebody's swap. The old value was never a discriminator and must not be reused
    as one.

    AND IT IS LOGGED, because the coins are real and arrived. An uncredited deposit is a
    support ticket somebody has to be able to open, so the signature and the reason have to
    exist somewhere.
    """
    adapter = make_adapter(
        {
            "getSignaturesForAddress": [{"signature": SIG, "err": None, "confirmationStatus": "confirmed"}],
            "getTransaction": native_tx([OTHER, OTHER, WALLET], [5, 5, 100], [5, 5, 600], memo=None),
        }
    )
    with caplog.at_level(logging.WARNING):
        assert adapter.find_deposits_to_address(WALLET) == []
    assert SIG in caplog.text
    assert "CANNOT BE ATTRIBUTED" in caplog.text
    assert "pay the wrong person" in caplog.text, "and why dropping it was the safe direction"


def test_A_PROSE_MEMO_IS_ALSO_UNATTRIBUTABLE():
    """Most memos on a real cluster are text. A deposit carrying one is not this swap's.

    int('gm frens') would raise; the danger is a parser that reaches for a number inside prose
    and finds one. chains/solana_memo.deposit_tag_from() refuses the whole string, and this
    pins that the adapter honors the refusal rather than working around it.
    """
    adapter = make_adapter(
        {
            "getSignaturesForAddress": [{"signature": SIG, "err": None, "confirmationStatus": "confirmed"}],
            "getTransaction": native_tx([OTHER, WALLET], [5, 100], [5, 600], memo="thanks for the swap 77"),
        }
    )
    assert adapter.find_deposits_to_address(WALLET) == []


def test_a_failed_transaction_credits_nothing():
    """THE ONE THAT WOULD COST MONEY. A failed Solana transaction still exists,
    still appears in getSignaturesForAddress and still paid a fee -- and moved
    nothing. Crediting one credits a deposit that was never made."""
    adapter = make_adapter(
        {
            "getSignaturesForAddress": [{"signature": SIG, "err": {"InstructionError": [0, "Custom"]}, "confirmationStatus": "finalized"}],
        }
    )
    assert adapter.find_deposits_to_address(WALLET) == []


def test_a_debit_is_not_a_deposit():
    adapter = make_adapter(
        {
            "getSignaturesForAddress": [{"signature": SIG, "err": None, "confirmationStatus": "finalized"}],
            "getTransaction": native_tx([WALLET, OTHER], [5_000_000_000, 0], [1_000_000_000, 4_000_000_000]),
        }
    )
    assert adapter.find_deposits_to_address(WALLET) == []


def test_each_commitment_level_arrives_as_its_rank_on_the_event():
    for level, rank in (("processed", 1), ("confirmed", 2), ("finalized", 3)):
        adapter = make_adapter(
            {
                "getSignaturesForAddress": [{"signature": SIG, "err": None, "confirmationStatus": level}],
                "getTransaction": native_tx([OTHER, WALLET], [0, 0], [0, 1_000_000_000]),
            }
        )
        assert adapter.find_deposits_to_address(WALLET)[0]["confirmations"] == rank


def test_a_transaction_that_cannot_be_read_raises_instead_of_fabricating_a_row():
    """chains/base.py returns a synthetic event here -- vout=0 with the amount
    the caller already believed -- which deposit_service cannot tell from a real
    one, and which migrate_deposit_vouts.py exists to clean up. Not reproduced."""
    adapter = make_adapter(
        {
            "getSignaturesForAddress": [{"signature": SIG, "err": None, "confirmationStatus": "finalized"}],
            "getTransaction": None,
        }
    )
    # THE REFUSAL IS STILL THERE, one level down. _credits_in_transaction is the decision
    # and it still raises; what changed on 2026-09-29 is that find_deposits_to_address
    # CATCHES it per signature instead of letting it abandon the scan.
    with pytest.raises(SolanaRPCError, match="Nothing is credited"):
        adapter._credits_in_transaction(SIG, WALLET, 3)
    # And nothing is fabricated from it at the scan level either.
    assert adapter.find_deposits_to_address(WALLET) == []


def test_a_mismatched_balance_array_refuses_rather_than_guessing():
    adapter = make_adapter(
        {
            "getSignaturesForAddress": [{"signature": SIG, "err": None, "confirmationStatus": "finalized"}],
            "getTransaction": native_tx([OTHER, WALLET], [0], [0]),
        }
    )
    with pytest.raises(SolanaRPCError, match="Refusing to guess"):
        adapter._credits_in_transaction(SIG, WALLET, 3)
    assert adapter.find_deposits_to_address(WALLET) == []


def test_ONE_UNREADABLE_TRANSACTION_DOES_NOT_ABANDON_THE_REST_OF_THE_SCAN(caplog):
    """THE DEFECT THIS PINS COST NOTHING YET ONLY BECAUSE NO SOL PAIR IS ENABLED.

    Until 2026-09-29 find_deposits_to_address() called _credits_in_transaction in a bare
    loop, so any SolanaRPCError propagated out and abandoned every signature after it.

    MEASURED ON DEVNET THAT DAY rather than imagined. `solana_chain_check.py --hunt-memo 20`
    against api.devnet.solana.com, running solana-core 4.3.0, produced both triggers in one
    run: `-32015 Transaction version (1) is not supported by the requesting client` on one
    signature, and HTTP 429 on eleven more. Either would have ended a real scan.

    WHY THE BLAST RADIUS IS THE WHOLE POINT. The SOL deposit account is SHARED by every swap
    (attribution is by memo tag, not by address), so one versioned transaction or one
    throttled response anywhere in its recent history would stop deposit discovery for ALL
    swaps -- and stop it invisibly, because an exception out of a poll loop looks exactly
    like a poll that found nothing.

    The ordering here is deliberate: the UNREADABLE signature is listed FIRST, so a scan
    that aborts returns nothing at all and this fails on an empty list rather than on a log
    message. Reversing it would let the old code pass.
    """
    good = native_tx([OTHER, WALLET], [0, 0], [0, 1_000_000_000])

    def by_signature(signature, *_rest):
        if signature == SIG:
            raise SolanaRPCError(
                "getTransaction failed: {'code': -32015, 'message': 'Transaction version (1) "
                "is not supported by the requesting client.'}"
            )
        return good

    adapter = make_adapter(
        {
            "getSignaturesForAddress": [
                {"signature": SIG, "err": None, "confirmationStatus": "finalized"},
                {"signature": SIG2, "err": None, "confirmationStatus": "finalized"},
            ],
            "getTransaction": by_signature,
        }
    )
    events = adapter.find_deposits_to_address(WALLET)

    assert [event["txid"] for event in events] == [SIG2], (
        "the readable transaction after the unreadable one must still be credited; an empty "
        "list here is the abort this test exists to catch"
    )
    assert all(event["txid"] != SIG for event in events), (
        "nothing may be credited from a transaction that could not be read"
    )
    # Rule 14: skipping must not look like finding nothing. Both the per-signature skip and
    # the run total are said out loud.
    warnings = " ".join(record.getMessage() for record in caplog.records)
    assert SIG in warnings and "SKIPPED" in warnings
    assert "read 1 of 2 listed transaction(s)" in warnings


def test_deposits_across_two_transactions_both_come_back():
    def by_signature(signature, *_rest):
        return {
            SIG: native_tx([OTHER, WALLET], [0, 0], [0, 1_000_000_000]),
            SIG2: native_tx([OTHER, WALLET], [0, 0], [0, 500_000_000]),
        }[signature]

    adapter = make_adapter(
        {
            "getSignaturesForAddress": [
                {"signature": SIG, "err": None, "confirmationStatus": "finalized"},
                {"signature": SIG2, "err": None, "confirmationStatus": "processed"},
            ],
            "getTransaction": by_signature,
        }
    )
    events = sorted(adapter.find_deposits_to_address(WALLET), key=lambda e: e["amount"])
    assert [(e["amount"], e["confirmations"]) for e in events] == [(0.5, 1), (1.0, 3)]


# --- find_deposits_to_address: SPL / wGRC ------------------------------------


def test_an_spl_credit_is_matched_on_owner_and_mint_at_the_mints_decimals():
    adapter = make_adapter(
        {
            "getSignaturesForAddress": [{"signature": SIG, "err": None, "confirmationStatus": "finalized"}],
            "getTransaction": {
                "transaction": {"message": {"accountKeys": [], "instructions": [memo_instruction(FIXTURE_TAG)]}},
                "meta": {
                    "err": None,
                    "preTokenBalances": [token_balance(3, WALLET, WSOL_MINT, 1_000_000)],
                    "postTokenBalances": [token_balance(3, WALLET, WSOL_MINT, 4_500_000)],
                }
            },
        },
        mint=WSOL_MINT,
    )
    assert adapter.find_deposits_to_address(WALLET) == [
        {"txid": SIG, "vout": FIXTURE_TAG, "address": WALLET, "amount": 3.5, "confirmations": 3}
    ]


def test_a_deposit_of_a_different_token_to_the_same_wallet_is_not_credited():
    """Matching on owner alone would credit another mint's deposit AT THIS
    MINT'S DECIMALS -- rule 11's silent order-of-magnitude error."""
    adapter = make_adapter(
        {
            "getSignaturesForAddress": [{"signature": SIG, "err": None, "confirmationStatus": "finalized"}],
            "getTransaction": {
                "transaction": {"message": {"accountKeys": [], "instructions": [memo_instruction(FIXTURE_TAG)]}},
                "meta": {
                    "err": None,
                    "preTokenBalances": [],
                    "postTokenBalances": [token_balance(3, WALLET, OTHER, 9_000_000)],
                }
            },
        },
        mint=WSOL_MINT,
    )
    assert adapter.find_deposits_to_address(WALLET) == []


def test_a_first_deposit_into_a_brand_new_token_account_has_no_pre_balance():
    """The ordinary case for a wallet receiving a token for the first time: the
    associated token account did not exist before the transaction."""
    adapter = make_adapter(
        {
            "getSignaturesForAddress": [{"signature": SIG, "err": None, "confirmationStatus": "finalized"}],
            "getTransaction": {
                "transaction": {"message": {"accountKeys": [], "instructions": [memo_instruction(FIXTURE_TAG)]}},
                "meta": {"err": None, "preTokenBalances": [], "postTokenBalances": [token_balance(4, WALLET, WSOL_MINT, 2_000_000)]},
            },
        },
        mint=WSOL_MINT,
    )
    assert adapter.find_deposits_to_address(WALLET)[0]["amount"] == 2.0


def test_deposit_discovery_refuses_a_malformed_address_before_calling_out():
    adapter = make_adapter({})
    with pytest.raises(SolanaAddressError):
        adapter.find_deposits_to_address("not-an-address")
    assert adapter.calls == []


# --- the two refusals --------------------------------------------------------


def test_get_new_address_refuses_because_the_chosen_strategy_has_no_per_swap_address():
    """STILL REFUSES, FOR A DIFFERENT REASON, and the old reason was on screen for eleven days.

    This test was `test_get_new_address_refuses_and_names_the_three_options` and it asserted
    the message listed all three custody options -- "fresh keypair per swap", "one account +
    memo", "derivation from a seed" -- because the strategy was unchosen when it was written.
    The operator chose on 2026-09-29 (one shared account plus a per-swap Memo instruction), and
    the refusal went on offering them the menu: "no strategy has been chosen. ... See README.md
    ... Until the operator chooses, SOL is a payout-side asset only."

    Rule 2: the test changes to pin the stronger invariant, and the stronger invariant is that
    the refusal points at the thing that HAS the answer. An operator who hits this at runtime
    needs `deposit_account()`, not a decision they already made.

    THE REFUSAL ITSELF IS UNCHANGED AND MUST STAY. Under a shared account there is no per-swap
    address to derive, so returning something address-shaped here would be worse than raising:
    services/swap_service.deposit_account() routes SOL by TAG_ATTRIBUTED_ASSETS and never calls
    this, and anything that DOES call it has the wrong model of the chain.
    """
    with pytest.raises(NotImplementedError) as exc:
        make_adapter({}).get_new_address("swap_s_abc")
    message = str(exc.value)

    assert "deposit_account" in message, "point at what has the answer, not at a decision"
    assert "Memo instruction" in message
    assert "2026-09-29" in message, "say WHEN it was decided; a bare claim ages the same way"
    assert "README.md" in message

    # The menu is gone, and each phrase is checked so re-adding any one of them fails here.
    for stale in ("no strategy has been chosen", "fresh keypair per swap",
                  "derivation from a seed", "Until the operator chooses",
                  "payout-side asset only"):
        assert stale not in message, f"the refusal still offers a decision already made: {stale!r}"


def test_send_to_address_refuses_and_holds_no_key_that_could_sign():
    adapter = make_adapter({"getAccountInfo": {"value": {"owner": "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA"}}})
    with pytest.raises(NotImplementedError) as exc:
        adapter.send_to_address(WALLET, 1.0)
    assert "cannot sign or broadcast" in str(exc.value)


def test_the_module_references_no_keypair_anywhere():
    """A structural claim rather than a behavioral one, and it is here because
    'this adapter cannot sign' is the load-bearing sentence of the whole file.
    If a later edit adds a keypair path, this fails."""
    source = Path(__file__).resolve().parent.parent / "swap_terminal" / "chains" / "solana.py"
    # CODE TOKENS ONLY. The module header deliberately NAMES the Node bridge's
    # SOLANA_PAYER_KEYPAIR_PATH, because rule 8 asks that a reader who finds
    # one implementation be told the other exists -- so a plain substring scan
    # over the text reports the documentation as the defect. The denominator
    # here is executable tokens, not lines, which is the same distinction
    # CLAUDE.md draws about counting `noqa` by grep versus by comment token.
    with source.open("rb") as handle:
        names = {token.string for token in tokenize.tokenize(handle.readline) if token.type == tokenize.NAME}
    for forbidden in ("Keypair", "secret_key", "from_secret_key", "sign", "sign_message", "partial_sign"):
        assert forbidden not in names, f"{forbidden} is referenced by executable code in chains/solana.py"


# --- the transfer plan: built, described, unsigned ---------------------------


def test_the_transfer_plan_prices_the_fee_per_signature_and_never_signs():
    plan = make_adapter({}).build_transfer_plan(WALLET, 1.5)
    assert plan["base_units"] == 1_500_000_000
    assert plan["fee_lamports"] == 5_000
    assert plan["signed"] is False
    assert plan["broadcast"] is False
    assert "per SIGNATURE, not per byte" in plan["description"]


def test_an_spl_plan_reports_the_token_account_and_what_creating_it_costs():
    """Rent replaces dust, and it is a DEPOSIT INTO A NEW ACCOUNT rather than a
    fee -- so who pays it is a fund decision the plan reports and does not make."""
    adapter = make_adapter(
        {
            "getAccountInfo": lambda address, *_rest: (
                {"value": {"owner": "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA", "data": {"parsed": {"info": {"decimals": 6}}}}}
                if address == WSOL_MINT
                else {"value": None}
            ),
            "getMinimumBalanceForRentExemption": 2_039_280,
        },
        mint=WSOL_MINT,
    )
    plan = adapter.build_transfer_plan(WALLET, 2.0)
    assert plan["decimals"] == 6
    assert plan["base_units"] == 2_000_000
    assert plan["token_account"] == WALLET_WSOL_ATA
    assert plan["token_account_exists"] is False
    assert plan["rent_lamports"] == 2_039_280
    assert "rent-exempt minimum" in plan["description"]
    assert "fund decision, not a fee" in plan["description"]


def test_a_plan_to_an_off_curve_address_is_refused():
    with pytest.raises(SolanaAddressError, match="OFF-CURVE"):
        make_adapter({}).build_transfer_plan(WALLET_WSOL_ATA, 1.0)


def test_an_amount_that_truncates_to_zero_is_refused_rather_than_sent_as_zero():
    """server.js's quoteGrcToSol refuses a quote that rounds down to zero
    lamports. Same refusal, same reason."""
    with pytest.raises(ValueError, match="rounds down"):
        make_adapter({}).build_transfer_plan(WALLET, 0.0000000001)


# --- mint / token program reads ----------------------------------------------


def test_a_token_2022_mint_is_detected_from_the_chain_rather_than_assumed():
    """Both programs derive well-formed ATAs for the same owner and mint, and
    only one holds the balance. Guessing is silent."""
    adapter = make_adapter(
        {"getAccountInfo": {"value": {"owner": "TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb", "data": {"parsed": {"info": {"decimals": 2}}}}}},
        mint=WSOL_MINT,
    )
    assert adapter.token_program_id_or_default() == "TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb"
    assert adapter.associated_token_address_for(WALLET) != WALLET_WSOL_ATA


def test_an_account_that_is_not_a_mint_is_reported_as_such():
    adapter = make_adapter({"getAccountInfo": {"value": {"owner": "x", "data": {"parsed": {"info": {}}}}}}, mint=WSOL_MINT)
    with pytest.raises(SolanaRPCError, match="did not parse as an SPL mint"):
        adapter.mint_decimals()


def test_rent_is_asked_of_the_chain_rather_than_taken_from_the_constant():
    """chains/solana_units.py's RENT_EXEMPT_* are labeled reference values
    because nothing here has read them off a cluster. This is the authority."""
    adapter = make_adapter({"getMinimumBalanceForRentExemption": 2_039_280})
    assert adapter.rent_exempt_minimum(165) == 2_039_280
    assert ("getMinimumBalanceForRentExemption", (165,)) in adapter.calls


# --- commitment reporting ----------------------------------------------------


def test_a_failed_signature_reports_rank_zero_however_settled_it_is():
    """Reporting a failed transaction's commitment level would be reporting how
    firmly the chain agrees that nothing happened."""
    adapter = make_adapter({"getSignatureStatuses": {"value": [{"confirmationStatus": "finalized", "err": {"X": 1}}]}})
    assert adapter.commitment_rank_for(SIG) == 0


def test_an_unknown_signature_reports_rank_zero_rather_than_raising():
    adapter = make_adapter({"getSignatureStatuses": {"value": [None]}})
    assert adapter.commitment_rank_for(SIG) == 0


def test_describe_signature_is_a_pasteable_line_that_says_it_is_not_blocks():
    adapter = make_adapter({"getSignatureStatuses": {"value": [{"confirmationStatus": "confirmed", "err": None}]}})
    line = adapter.describe_signature(SIG)
    assert SIG in line
    assert "NOT a count of blocks" in line
    assert "not yet creditable" in line


# --- the registry ------------------------------------------------------------


def test_the_registry_builds_the_three_bitcoin_chains_and_omits_an_unconfigured_sol():
    """An unconfigured Solana adapter would make
    refresh_wallet_inventory() log a WARNING per worker per cycle, forever,
    about a fault that does not exist."""
    rpc = {
        "BTC": {"user": "u", "password": "p", "host": "h", "port": 1},
        "LTC": {"user": "u", "password": "p", "host": "h", "port": 2},
        "GRC": {"user": "u", "password": "p", "host": "h", "port": 3},
        "SOL": {"url": "", "commitment": "processed", "timeout": 1.0, "mint": "", "hot_wallet": "", "min_commitment_rank": 3},
    }
    adapters = build_adapters(rpc)
    assert sorted(adapters) == ["BTC", "GRC", "LTC"]


def test_the_registry_builds_sol_once_a_url_is_set():
    rpc = {
        "BTC": {"user": "u", "password": "p", "host": "h", "port": 1},
        "SOL": {"url": "http://x.invalid", "commitment": "processed", "timeout": 1.0, "mint": "", "hot_wallet": "", "min_commitment_rank": 3},
    }
    adapters = build_adapters(rpc)
    assert isinstance(adapters["SOL"], SolanaAdapter)


def test_adding_the_adapter_does_not_enable_a_trading_pair():
    """Enabling a pair is live posture and is the operator's (rule 16). The
    adapter being reachable for READING must not imply a SOL swap can be
    quoted."""
    assert not any("SOL" in pair for pair in Config.ALLOWED_PAIRS)


# --- the event shape ---------------------------------------------------------


def test_the_event_dict_carries_exactly_the_keys_deposit_service_reads():
    """Measured out of services/deposit_service.py: upsert_deposit_event() and
    refresh_swap_from_chain() read txid, vout, address, amount, confirmations."""
    event = deposit_event(SIG, 2, WALLET, 1.25, 3)
    assert set(event) == {"txid", "vout", "address", "amount", "confirmations"}
    assert event["vout"] == 2


def test_EVERY_TAG_ATTRIBUTED_ASSET_HAS_THE_CONFIG_KEY_ITS_TABLE_NAMES():
    """TAG_ATTRIBUTION pointed at a config key that did not exist, for about an hour.

    `SOL` was added to TAG_ATTRIBUTED_ASSETS and TAG_ATTRIBUTION was wired to read
    `SOL_DEPOSIT_ACCOUNT` from config -- before config defined it. So
    `config.get("SOL_DEPOSIT_ACCOUNT")` returned None and every SOL swap would have refused
    with "SOL_DEPOSIT_ACCOUNT is not set" NO MATTER WHAT THE OPERATOR EXPORTED.

    That is the worst shape a refusal can have: it reads as a configuration problem on the
    person's side and is missing code on ours, so the only one who can see it is the one who
    cannot fix it.

    ASSERTED OVER THE TABLE rather than for SOL specifically, so the third tag-attributed chain
    -- Stellar's memo, Cosmos's memo, whatever it turns out to be -- cannot land with the same
    gap. TAG_ATTRIBUTED_ASSETS's own comment says it expects one.
    """
    assert set(TAG_ATTRIBUTION) == set(TAG_ATTRIBUTED_ASSETS), (
        "a chain attributed by tag with no entry here would raise KeyError inside "
        "deposit_address_for(), which runs while a customer is waiting for an address"
    )
    for asset, (variable, discriminator, network) in TAG_ATTRIBUTION.items():
        assert hasattr(Config, variable), (
            f"{asset} is attributed by tag and TAG_ATTRIBUTION names {variable}, but config.py "
            f"does not define it -- so config.get({variable!r}) is None and no swap on {asset} "
            f"can ever be created, whatever the operator exports"
        )
        assert discriminator and network, f"{asset}'s refusals would name a blank"


def test_the_module_header_names_which_rpc_METHODS_met_a_real_cluster():
    """THE FILE'S TOP-LINE HONESTY CLAIM, and it went false the day a run succeeded.

    It read "NOTHING IN THIS FILE HAS BEEN EXERCISED AGAINST A SOLANA CLUSTER" until
    2026-09-30, which was true from 2026-09-25 -- devnet returned 403 from the development
    proxy and solana-test-validator could not be installed. Then the operator ran
    solana_chain_check.py against devnet twice and --hunt-memo once, and the sentence stopped
    being true without anything failing. That is the fourth stale claim this session, and the
    pattern in every one of them is the same: a measurement arrived and the document that
    describes the code did not hear about it.

    WHAT IS PINNED IS THE SPLIT, NOT THE SENTENCE. "The adapter works" is not a thing a run
    proves, and a header that said so would be worse than the false one it replaced. So the
    header lists methods on both sides, and this asserts the split exists and that every
    reader appears on exactly one side of it.

    THIS TEST ASSERTED THE CREDIT READERS WERE UNPROVEN, AND THAT WENT FALSE TOO -- on
    2026-10-01, when `--mint --find-holder` reached a transaction that credits the holder and
    _spl_credits decoded an amount off it. The test was right when written and the fact moved,
    which is rule 2: its test dies with it or changes to pin the stronger invariant. The
    stronger invariant is the one that survives a reader crossing sides -- EXACTLY ONE side,
    never both and never neither -- because a header that lists a reader twice has stopped
    being a claim about anything.

    MUTATION: declare the file exercised wholesale, drop a reader from both lists, or name one
    in both, and this fails. A field-name error in either reader still passes every seeded
    test in this tree, which is why the list has to stay accurate rather than merely present.
    """
    source = Path(chains_solana.__file__).read_text(encoding="utf-8")
    header = source[:source.index("from __future__")]

    assert "NOTHING IN THIS FILE HAS BEEN EXERCISED" not in header, (
        "false since 2026-09-30: the read plumbing met devnet"
    )
    assert "EXERCISED" in header and "STILL UNEXERCISED" in header, "both sides, named"

    proven, unproven = header.split("STILL UNEXERCISED", 1)
    for method in ("getHealth", "getGenesisHash", "getSignaturesForAddress", "getTransaction",
                   "getMinimumBalanceForRentExemption", "getBalance"):
        assert method in proven, f"{method} answered on a real run and belongs above"

    # getBalance MOVED to the proven side on 2026-09-30, when the ADDRESS section first ran and
    # returned 28.7786992 SOL for a real devnet account. This test failed on that move, which is
    # it working: the list is the file's honesty claim and a method must not cross without a run.
    #
    # WHAT MUST STAY UNPROVEN IS THE READERS, not the RPC methods. `_native_credits` and
    # `_spl_credits` turn a transaction into a credit, and a wrong field name there returns
    # nothing rather than raising -- which reads as "no deposit arrived". Naming the methods and
    # not the readers is how a header could claim the credit path was covered because
    # getSignaturesForAddress answered.
    for reader in ("_native_credits", "_spl_credits"):
        assert (reader in proven) != (reader in unproven), (
            f"{reader} must be named on EXACTLY one side. It is where a wrong field name "
            f"silently loses a deposit, so 'which side is it on' has to have one answer"
        )
    assert "_spl_credits -- PROVEN" in proven, (
        "it decoded an amount off a real devnet response on 2026-10-01, through the targeted "
        "read -- and the header has to say so, the same way it had to stop saying nothing was "
        "exercised"
    )
    assert "_native_credits -- PROVEN" in proven, (
        "closed by the --mint-less run: 1 credit refused over 8 signatures, decoded off "
        "preBalances/postBalances. This assertion read 'NOT re-confirmed' for one commit -- "
        "the caveat was right when written and a run settled it, which is why what gets "
        "pinned is the split and not the contents"
    )
    # NO BLACKLIST OF THE OLD PHRASE. I wrote `assert "NOT re-confirmed" not in proven` here
    # and it failed on the header's own QUOTATION of the sentence it was retiring -- which the
    # header quotes on purpose, because rule 1 keeps the superseded measurement and the drift
    # is the point. A keyword check that cannot tell a quotation from a claim is the same defect
    # this suite keeps finding elsewhere: a test that reads text instead of behavior. The
    # positive assertion above, plus the exactly-one-side invariant, carry this without it.
    assert "4.3.0" in proven, "the solana-core build it was proven against (rule 3)"


def test_the_header_records_the_defect_the_live_run_FOUND_rather_than_only_coverage():
    """A failed step is the most useful thing this check has produced, and it is recorded as one.

    The run did not merely leave the credit path uncovered -- it REFUTED a constant. Discovery
    asked for `processed` and getSignaturesForAddress answered -32602, so SOL deposit discovery
    had never worked. A header that listed the method as "unexercised" and moved on would lose
    the finding, and the next reader would treat the fix as unnecessary.

    AND IT STAYS RECORDED AFTER THE FIX IS CONFIRMED, which is what changed on 2026-10-01.
    This used to assert "NOT YET CONFIRMED BY A RUN", correctly -- the fix was a fix and not a
    measurement until a run said so. Eight runs have now said so. The finding does NOT get
    deleted along with the uncertainty: the next reader has to be able to see that `processed`
    was refused by a real cluster, or the constant looks arbitrary and someone lowers it again.

    MUTATION: delete the ATTEMPTED AND FAILED section, fold it into the unexercised list, or
    drop the confirmation now that it is confirmed, and this fails.
    """
    source = Path(chains_solana.__file__).read_text(encoding="utf-8")
    header = source[:source.index("from __future__")]
    assert "ATTEMPTED AND FAILED" in header
    assert "-32602" in header, "the error the cluster actually returned"
    assert "LOWEST_COMMITMENT_THE_HISTORY_METHODS_ACCEPT" in header, "and where the fix lives"
    assert "CONFIRMED by every run since" in header, (
        "a fix is not a measurement until a run says so, and now one has -- the header tracks "
        "which of the two it is holding rather than leaving the reader to guess"
    )
    assert "NOT YET CONFIRMED BY A RUN" not in header, "stale since 2026-10-01"

# ---------------------------------------------------------------------------
# THE COMMITMENT FLOOR. Found by the operator's 2026-09-30 devnet run, the first
# time solana_chain_check.py's ADDRESS section executed:
#
#   find_deposits_to_address(limit=10)
#   FAIL getSignaturesForAddress failed: {'code': -32602, 'message':
#        'Method does not support commitment below `confirmed`'}
#
# find_deposits_to_address is THE method the deposit watcher calls. It had never
# worked against a real cluster and could not have: DISCOVERY_COMMITMENT asked
# for `processed`, which that method rejects. Every seeded test passed, because a
# stub answers whatever it is asked -- which is the exact failure mode
# chains/solana.py's header warns about and the reason the chain check exists.
# ---------------------------------------------------------------------------


def test_discovery_never_asks_below_the_floor_the_cluster_enforces():
    """THE DEFECT, PINNED AT THE CONSTANT.

    MUTATION: set DISCOVERY_COMMITMENT back to "processed" and this fails. That is the value
    that shipped, and it made SOL deposit discovery impossible while every test stayed green.
    """
    assert COMMITMENT_RANKS[DISCOVERY_COMMITMENT] >= COMMITMENT_RANKS[
        LOWEST_COMMITMENT_THE_HISTORY_METHODS_ACCEPT], (
        f"discovery asks for {DISCOVERY_COMMITMENT!r}, below the floor "
        f"{LOWEST_COMMITMENT_THE_HISTORY_METHODS_ACCEPT!r} that getSignaturesForAddress enforces "
        f"with -32602"
    )
    assert DISCOVERY_COMMITMENT in COMMITMENT_RANKS, "it has to be a rung, not a typo"

    # THE FLOOR ITSELF IS A MEASURED PROTOCOL FACT, NOT A TUNABLE, and this assertion is here
    # because lowering it survived the check above: comparing discovery >= floor passes if BOTH
    # move, so the relative test alone let the whole fix be undone in one edit. The value is what
    # the cluster said, and the error message is the evidence:
    #
    #     -32602  Method does not support commitment below `confirmed`
    #
    # A future reader who believes the floor has changed needs a run that says so, not an edit.
    assert LOWEST_COMMITMENT_THE_HISTORY_METHODS_ACCEPT == "confirmed", (
        "this is what api.devnet.solana.com enforced on 2026-09-30, measured. Lowering it is a "
        "claim about the protocol and needs a run, not an assumption"
    )
    assert COMMITMENT_RANKS[LOWEST_COMMITMENT_THE_HISTORY_METHODS_ACCEPT] == 2


def test_discovery_is_still_BELOW_finalized_so_a_deposit_is_visible_while_confirming():
    """The floor was raised, and the reason for a low discovery commitment survives it.

    If discovery read at `finalized` -- the default SOL_MIN_CONFIRMATIONS -- a deposit would be
    invisible for its whole confirmation window and then appear already credited. That is rule
    14's silence: neither the operator nor the customer could tell "arriving" from "never sent".

    MUTATION: raise DISCOVERY_COMMITMENT to "finalized" to be safe, and this fails. Safe is not
    the axis; crediting is gated on the rank read from the RESPONSE, not on this value.
    """
    assert COMMITMENT_RANKS[DISCOVERY_COMMITMENT] < COMMITMENT_RANKS[BALANCE_COMMITMENT]
    assert COMMITMENT_RANKS[DISCOVERY_COMMITMENT] < FINALIZED_RANK


def test_the_two_calls_that_hit_the_floor_both_read_the_constant(monkeypatch):
    """BOTH call sites, checked at the wire rather than in the source.

    getSignaturesForAddress is the one that failed; getTransaction takes the same constant and
    is documented to enforce the same floor -- it was never reached on the operator's run,
    because discovery failed first. Fixing the constant fixes both, and this asserts both
    actually send it rather than one having been special-cased.
    """
    sent = []

    class Recording(dict):
        pass

    adapter = make_adapter({
        "getSignaturesForAddress": [{"signature": "sig1", "confirmationStatus": "confirmed"}],
        "getTransaction": {
            "transaction": {"message": {"accountKeys": [WALLET], "instructions": []}},
            "meta": {"preBalances": [0], "postBalances": [1_000_000], "err": None},
        },
    })
    original = adapter.call

    def recording_call(method, *params):
        sent.append((method, params))
        return original(method, *params)

    monkeypatch.setattr(adapter, "call", recording_call)
    adapter.find_deposits_to_address(WALLET)

    for method, params in sent:
        if method in ("getSignaturesForAddress", "getTransaction"):
            config = next(p for p in params if isinstance(p, dict))
            assert config.get("commitment") == DISCOVERY_COMMITMENT, (
                f"{method} sent commitment {config.get('commitment')!r}, not the constant -- so "
                f"raising the floor in one place would not have raised it here"
            )
    assert {m for m, _ in sent} >= {"getSignaturesForAddress", "getTransaction"}, (
        f"expected both history calls to run; got {sorted({m for m, _ in sent})}"
    )

# ---------------------------------------------------------------------------
# A DROPPED CREDIT IS REAL MONEY, AND THE CALLER HAS TO BE ABLE TO SEE IT.
#
# The operator's 2026-09-30 devnet run, the first that reached the deposit
# reader: a real credit was read, correctly refused for carrying no memo, and
# solana_chain_check.py printed
#
#   (none)  <- zero credits in the signatures read. This is a RESULT, not a failure.
#
# four lines below the WARNING saying one credit had been dropped. The check's own
# log contradicted its own result line. `find_deposits_to_address` returned [] for
# both "nothing arrived" and "money arrived that nobody can claim", and rule 5
# says a measurement that only exists in a log is not learning.
# ---------------------------------------------------------------------------


def _memoless_credit(address):
    """A real transaction crediting `address` with no memo instruction on it."""
    return {
        "transaction": {"message": {"accountKeys": [address], "instructions": []}},
        "meta": {"preBalances": [0], "postBalances": [5_000], "err": None},
    }


def test_an_unattributable_credit_is_RECORDED_and_not_only_logged():
    """THE DEFECT. The return value stays [] -- correctly -- and the drop is readable.

    MUTATION: delete the self.unattributable_drops.append() in _attributable and this fails,
    which puts the check back to printing "(none)" over somebody's stranded deposit.
    """
    adapter = make_adapter({
        "getSignaturesForAddress": [{"signature": "2K2Pw1Hz", "confirmationStatus": "confirmed"}],
        "getTransaction": _memoless_credit(WALLET),
    })
    events = adapter.find_deposits_to_address(WALLET)

    assert events == [], "an unattributable credit must NOT be credited -- that part was right"
    assert len(adapter.unattributable_drops) == 1
    drop = adapter.unattributable_drops[0]
    assert drop.signature == "2K2Pw1Hz"
    assert drop.credits == 1
    assert "no memo" in drop.why


def test_the_drops_describe_THIS_poll_and_not_every_poll_since_construction():
    """Cleared per call, because a watcher keeps one adapter for its whole lifetime.

    MUTATION: initialize the list in __init__ only and this fails -- a drop from an hour ago
    would be reported as though it had just happened, on every poll, forever.
    """
    adapter = make_adapter({
        "getSignaturesForAddress": [{"signature": "2K2Pw1Hz", "confirmationStatus": "confirmed"}],
        "getTransaction": _memoless_credit(WALLET),
    })
    adapter.find_deposits_to_address(WALLET)
    assert len(adapter.unattributable_drops) == 1

    # THE SAME ADAPTER, POLLED AGAIN, and this is the assertion that matters -- a fresh adapter
    # starting empty proves only __init__. The seeded responses are mutated so the second poll
    # sees a quiet cluster, which is what a watcher's next tick looks like after a drop.
    #
    # The signatures dict is reached through the closure make_adapter() built rather than an
    # attribute: the first attempt here wrote `adapter.responses[...]` and raised AttributeError,
    # because make_adapter keeps the mapping in a closure. A test that cannot drive the second
    # poll is a test that only ever checked the first.
    responses = {"getSignaturesForAddress": [{"signature": "2K2Pw1Hz",
                                              "confirmationStatus": "confirmed"}],
                 "getTransaction": _memoless_credit(WALLET)}
    again = make_adapter(responses)
    again.find_deposits_to_address(WALLET)
    assert len(again.unattributable_drops) == 1

    responses["getSignaturesForAddress"] = []
    again.find_deposits_to_address(WALLET)
    assert again.unattributable_drops == [], "the list must describe the latest poll only"
    assert again.signatures_read == 0


def test_the_signature_count_separates_a_quiet_account_from_an_uncrediting_one():
    """Rule 3's denominator. Zero signatures and zero credits are different facts.

    "(none)" over an account nothing has touched is normal. "(none)" over ten transactions that
    all credited somebody else is also normal but means the poll IS seeing traffic -- which is
    what an operator asking "is the watcher even running" needs to know.
    """
    quiet = make_adapter({"getSignaturesForAddress": []})
    quiet.find_deposits_to_address(WALLET)
    assert quiet.signatures_read == 0

    busy = make_adapter({
        "getSignaturesForAddress": [{"signature": f"sig{n}", "confirmationStatus": "confirmed"}
                                    for n in range(3)],
        "getTransaction": {"transaction": {"message": {"accountKeys": ["rOTHER"],
                                                       "instructions": []}},
                           "meta": {"preBalances": [0], "postBalances": [0], "err": None}},
    })
    busy.find_deposits_to_address(WALLET)
    assert busy.signatures_read == 3
    assert busy.unattributable_drops == [], "no credits to this address means nothing was dropped"


def test_an_ATTRIBUTABLE_credit_records_no_drop():
    """The happy path leaves the list empty, so a non-empty list always means something.

    MUTATION: append to unattributable_drops unconditionally and this fails -- a check that
    always reports stranded money is a check nobody reads.
    """
    with_memo = _memoless_credit(WALLET)
    with_memo["transaction"]["message"]["instructions"] = [{
        "program": "spl-memo", "parsed": "4242",
        "programId": "MemoSq4gqABAXKb96qnH8TysNcWxMyWCqXgDLGmfcHr",
    }]
    adapter = make_adapter({
        "getSignaturesForAddress": [{"signature": "sigOK", "confirmationStatus": "confirmed"}],
        "getTransaction": with_memo,
    })
    events = adapter.find_deposits_to_address(WALLET)
    assert len(events) == 1
    assert events[0]["vout"] == 4242, "the memo tag becomes the discriminator"
    assert adapter.unattributable_drops == []

def test_a_SKIPPED_transaction_is_recorded_on_the_adapter_and_not_only_logged():
    """THE OPERATOR'S 2026-10-01 RUN, END TO END THROUGH THE REAL SCAN.

    One getTransaction answered HTTP 429, the scan skipped it correctly, and the only trace was
    a log line -- so `find_deposits_to_address` returned [] and a caller could not tell "nothing
    credited this address over ten transactions" from "over nine, and the tenth is unknown". A
    credit may be in the one that was never fetched.

    MUTATION: delete `self.unreadable_signatures = list(unreadable)` and this fails. That
    mutation SURVIVED the first mutation run of this change, because every adapter test seeded
    only readable transactions -- the attribute stayed at its __init__ default of [] and nothing
    noticed. A mutation surviving because no test drives the path is the useful kind of
    survivor.
    """
    def sometimes(signature, _config):
        if signature == "THROTTLED":
            raise SolanaRPCError("getTransaction returned HTTP 429", status_code=429)
        return {"transaction": {"message": {"accountKeys": ["rOTHER"], "instructions": []}},
                "meta": {"preBalances": [0], "postBalances": [0], "err": None}}

    adapter = make_adapter({
        "getSignaturesForAddress": [{"signature": "THROTTLED", "confirmationStatus": "confirmed"},
                                    {"signature": "FINE", "confirmationStatus": "confirmed"}],
        "getTransaction": sometimes,
    })
    events = adapter.find_deposits_to_address(WALLET)

    assert events == []
    assert adapter.unreadable_signatures == ["THROTTLED"]
    assert adapter.signatures_listed == 2
    assert adapter.signatures_read == 1, "listed minus unreadable -- the name has to be true"


def test_the_unreadable_list_describes_THIS_scan_only():
    """Cleared per call, for the same reason the drops are: a watcher keeps one adapter forever.

    MUTATION: initialize it only in __init__ and a signature skipped an hour ago is reported as
    unread on every later poll, understating coverage permanently.
    """
    responses = {
        "getSignaturesForAddress": [{"signature": "THROTTLED", "confirmationStatus": "confirmed"}],
        "getTransaction": lambda *_a: (_ for _ in ()).throw(
            SolanaRPCError("HTTP 429", status_code=429)),
    }
    adapter = make_adapter(responses)
    adapter.find_deposits_to_address(WALLET)
    assert adapter.unreadable_signatures == ["THROTTLED"]

    responses["getSignaturesForAddress"] = []
    adapter.find_deposits_to_address(WALLET)
    assert adapter.unreadable_signatures == [], "the list must describe the latest scan only"
    assert adapter.signatures_read == 0

def test_native_credits_over_the_REAL_devnet_numbers():
    """The lamport figures read off devnet 2026-10-01, replayed through the real reader.

    Measured directly, not from a run's rendered output:

        signature       2K2Pw1Hz...
        account index   1
        preBalances[1]  0
        postBalances[1] 28778699200
        delta           28.7786992 SOL

    WHY THIS IS WORTH A TEST AND NOT ONLY A HEADER NOTE. The run that proved this reader
    printed 28.7786992 for the dropped credit AND for the account's whole balance, four lines
    apart. A delta equal to the balance is the signature of a funding transaction or of a
    reader returning the balance by mistake, and the two are indistinguishable from the
    screen. Reading preBalances settled it -- pre was zero -- and seeding those exact numbers
    here keeps the arithmetic pinned to a real response rather than to a round number somebody
    chose.

    MUTATION: return post instead of post - pre and this still passes on THIS transaction,
    because pre is zero -- which is why the second case below exists. Together they separate
    the two readings the header had to go to the chain to tell apart.
    """
    adapter = chains_solana.SolanaAdapter(url="http://seeded.invalid")
    signature = "2K2Pw1HzJs3qH5kY2dRZ1HPvmxKnddCyYt3UoChtXEMxCz5LkHe1N2Gq4Fqg5hxTSwj4sENx65j1iBrDA54BUtMc"
    me = "J5wn3xEMDsr9r8qtF6YTWJodmgW5kG3ZThqDb8Xc37JM"

    def transaction(pre, post):
        return {
            "transaction": {"message": {"accountKeys": [
                {"pubkey": "SomeFeePayer1111111111111111111111111111111"},
                {"pubkey": me},
            ]}},
            "meta": {"preBalances": [1_000_000, pre], "postBalances": [995_000, post]},
        }

    real = transaction(0, 28_778_699_200)
    # CALLS THE PRIVATE READER ON PURPOSE: find_deposits_to_address() needs a cluster, and the
    # arithmetic is what the chain confirmed. Same reason solana_chain_check._spl_reader_line()
    # reaches for the per-transaction reader. (No `noqa` -- SLF001 is not in this repo's ruff
    # selection and RUF100 strips the directive, so the reason stays as a comment.)
    events = adapter._native_credits(
        signature, me, real, real["meta"], chains_solana.FINALIZED_RANK)
    assert len(events) == 1
    assert events[0]["amount"] == 28.7786992, "post - pre, divided by SOL_DECIMALS"
    assert events[0]["vout"] == 1, (
        "the ACCOUNT INDEX off the real response -- index 1, as the chain reported it"
    )
    assert events[0]["txid"] == signature

    # THE CASE THE DEVNET TRANSACTION CANNOT DISTINGUISH. Same post, a non-zero pre: a reader
    # returning the balance would still say 28.7786992 and this says 0.7786992.
    moved = transaction(28_000_000_000, 28_778_699_200)
    assert adapter._native_credits(
        signature, me, moved, moved["meta"], chains_solana.FINALIZED_RANK,
    )[0]["amount"] == 0.7786992, "a delta, not a balance"
