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

import tokenize
from pathlib import Path

import pytest
from chains.registry import build_adapters
from chains.solana import SolanaAdapter, SolanaRPCError, deposit_event
from chains.solana_address import SolanaAddressError
from config import Config

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


def native_tx(keys, pre, post, err=None):
    return {
        "meta": {"err": err, "preBalances": pre, "postBalances": post},
        "transaction": {"message": {"accountKeys": [{"pubkey": k} for k in keys]}},
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
    assert events == [{"txid": SIG, "vout": 1, "address": WALLET, "amount": 2.0, "confirmations": 3}]


def test_vout_is_the_account_index_read_from_the_transaction():
    """NOT chains/base.py's fabricated vout=0. The index comes out of the
    transaction's own account key list, which is why two different deposit
    addresses in one transaction cannot collide on the UNIQUE(asset, txid, vout)
    key."""
    adapter = make_adapter(
        {
            "getSignaturesForAddress": [{"signature": SIG, "err": None, "confirmationStatus": "confirmed"}],
            "getTransaction": native_tx([OTHER, OTHER, WALLET], [5, 5, 100], [5, 5, 600]),
        }
    )
    assert adapter.find_deposits_to_address(WALLET)[0]["vout"] == 2


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
    with pytest.raises(SolanaRPCError, match="Nothing is credited"):
        adapter.find_deposits_to_address(WALLET)


def test_a_mismatched_balance_array_raises_rather_than_guessing():
    adapter = make_adapter(
        {
            "getSignaturesForAddress": [{"signature": SIG, "err": None, "confirmationStatus": "finalized"}],
            "getTransaction": native_tx([OTHER, WALLET], [0], [0]),
        }
    )
    with pytest.raises(SolanaRPCError, match="Refusing to guess"):
        adapter.find_deposits_to_address(WALLET)


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
        {"txid": SIG, "vout": 3, "address": WALLET, "amount": 3.5, "confirmations": 3}
    ]


def test_a_deposit_of_a_different_token_to_the_same_wallet_is_not_credited():
    """Matching on owner alone would credit another mint's deposit AT THIS
    MINT'S DECIMALS -- rule 11's silent order-of-magnitude error."""
    adapter = make_adapter(
        {
            "getSignaturesForAddress": [{"signature": SIG, "err": None, "confirmationStatus": "finalized"}],
            "getTransaction": {
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


def test_get_new_address_refuses_and_names_the_three_options():
    """Solana has no `getnewaddress`. The strategy is a custody decision and is
    the operator's (CLAUDE.md rule 16), so this refuses and pays the refusal
    back in the message rather than picking one."""
    with pytest.raises(NotImplementedError) as exc:
        make_adapter({}).get_new_address("swap_s_abc")
    message = str(exc.value)
    assert "fresh keypair per swap" in message
    assert "one account + memo" in message
    assert "derivation from a seed" in message
    assert "README.md" in message


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
        "BTC": {"user": "", "password": "", "host": "h", "port": 1},
        "LTC": {"user": "", "password": "", "host": "h", "port": 2},
        "GRC": {"user": "", "password": "", "host": "h", "port": 3},
        "SOL": {"url": "", "commitment": "processed", "timeout": 1.0, "mint": "", "hot_wallet": "", "min_commitment_rank": 3},
    }
    adapters = build_adapters(rpc)
    assert sorted(adapters) == ["BTC", "GRC", "LTC"]


def test_the_registry_builds_sol_once_a_url_is_set():
    rpc = {
        "BTC": {"user": "", "password": "", "host": "h", "port": 1},
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
