"""icp_ledger_init: the ICP ledger's init arguments, and what it refuses to write.

Role: tests (read-only except tmp_path)
Reads: icp_ledger_init
Writes: nothing outside pytest's tmp_path
Can move funds: no
Mainnet-safe: yes -- no socket, no replica, no key, no dfx.

EVERY ACCOUNT IDENTIFIER HERE IS DERIVED by calling chains/icp_account rather
than written out, so no assertion can pass by agreeing with a string somebody
typed twice, and the address-literal gate has nothing to count.

WHAT THIS IS GUARDING, because "a config generator" undersells it. The file it
writes decides two things about a ledger that cannot be changed afterwards
without a fresh canister: who may mint, and who holds the opening supply. Both
are 64-hex account identifiers, both are pasted by hand off a `dfx ledger
account-id`, and a ledger built from a mistyped one deploys cleanly and is
useless -- the mint authority or the whole supply belongs to an account no
identity on the replica can spend from, and the first symptom arrives at a
transfer, which is the point at which somebody blames the adapter.

So the refusals are the subject of most of this file, not the happy path.
"""

from __future__ import annotations

import pytest

from icp_ledger_init import (
    ICP_DECIMALS,
    LOCAL_TOKEN_NAME,
    LOCAL_TOKEN_SYMBOL,
    LedgerInitRefused,
    check_account,
    init_argument,
)
from swap_terminal.chains.coin_amounts import amount_to_base_units
from swap_terminal.chains.icp_account import (
    account_identifier,
    principal_to_text,
    subaccount_from_index,
)


def account(index: int) -> str:
    """A distinct, VALID account identifier per index, derived rather than typed.

    One principal and a per-index subaccount, which is the mechanism the ICP
    deposit address uses for real -- so the fixtures are the production shape.
    """
    owner = principal_to_text(bytes.fromhex("00000000000000020101"))
    return account_identifier(owner, subaccount_from_index(index))


def test_a_valid_account_passes_through_unchanged():
    """check_account returns its input so it can be used inline at a call site."""
    assert check_account(account(1), "minter") == account(1)


@pytest.mark.parametrize("role", ["minter", "initial balance holder"])
def test_a_mistyped_account_is_refused_and_the_message_says_which_role(role):
    """The role is in the message because an operator holds two of these at once.

    "invalid account identifier" answers nothing when there are a minter and a
    holder on screen (rule 14: state what the number means, next to the number --
    here, which value).
    """
    good = account(2)
    mistyped = ("0" if good[0] != "0" else "1") + good[1:]
    assert len(mistyped) == len(good)
    with pytest.raises(LedgerInitRefused, match=role):
        check_account(mistyped, role)


def test_a_principal_is_refused_where_an_account_identifier_is_wanted():
    """The most likely paste error of the two, and it must not be hashed anyway."""
    owner = principal_to_text(bytes.fromhex("00000000000000020101"))
    with pytest.raises(LedgerInitRefused, match="PRINCIPAL"):
        check_account(owner, "minter")


def test_the_init_argument_carries_the_minter_the_balance_and_the_fee():
    """The happy path, asserted on content rather than on the whole string.

    Pinning the exact text would make every comment or whitespace change a test
    failure; what matters is that the three values a ledger cannot be rebuilt
    without are present and are the ones passed in.
    """
    minter, holder = account(10), account(11)
    text = init_argument(minter=minter, balances={holder: 10_000_000_000}, transfer_fee_e8s=10_000)
    assert f'minting_account = "{minter}"' in text
    assert f'"{holder}"' in text
    assert "e8s = 10000000000 : nat64" in text
    assert "e8s = 10000 : nat64" in text
    assert f'token_symbol = opt "{LOCAL_TOKEN_SYMBOL}"' in text
    assert f'token_name = opt "{LOCAL_TOKEN_NAME}"' in text
    assert text.count("{") == text.count("}")


def test_several_funded_accounts_each_appear_once():
    """Three holders, three records, and no account silently dropped or doubled."""
    minter = account(20)
    balances = {account(21): 1, account(22): 2, account(23): 3}
    text = init_argument(minter=minter, balances=balances, transfer_fee_e8s=0)
    for holder in balances:
        assert text.count(f'"{holder}"') == 1
    assert text.count("record {\n          \"") == len(balances)


def test_a_minter_that_also_holds_a_balance_is_refused():
    """Mint source and funded holder in one account makes every later reading ambiguous.

    A JUDGMENT, and the docstring in the module says so: the ledger does not
    forbid it. It is refused so that no measurement taken against this ledger has
    to be qualified by which of a mint and a transfer happened.
    """
    minter = account(30)
    with pytest.raises(LedgerInitRefused, match="burns"):
        init_argument(minter=minter, balances={minter: 1}, transfer_fee_e8s=10_000)


def test_no_initial_balances_is_refused():
    """A ledger with zero supply cannot pay a fee and looks exactly like a broken adapter."""
    with pytest.raises(LedgerInitRefused, match="zero"):
        init_argument(minter=account(40), balances={}, transfer_fee_e8s=10_000)


@pytest.mark.parametrize("e8s", [0, -1])
def test_a_non_positive_opening_balance_is_refused(e8s):
    """Writing it would make the deploy look like it funded something."""
    with pytest.raises(LedgerInitRefused, match="opening balance"):
        init_argument(minter=account(50), balances={account(51): e8s}, transfer_fee_e8s=10_000)


def test_a_negative_fee_is_refused():
    with pytest.raises(LedgerInitRefused, match="negative"):
        init_argument(minter=account(60), balances={account(61): 1}, transfer_fee_e8s=-1)


def test_the_arguments_are_keyword_only():
    """Five values, two of them interchangeable-looking 64-hex strings.

    Swapping the minter and a holder positionally would give a ledger whose mint
    authority is the desk and whose supply is held by the minter -- no error, and
    a ledger nobody can explain. Keyword-only is the structural answer to that
    rather than a comment asking callers to be careful.
    """
    with pytest.raises(TypeError):
        init_argument(account(70), {account(71): 1}, 10_000)  # type: ignore[misc]


def test_icp_is_eight_decimals_like_the_other_three_chains():
    """Not a second decimals table: chains/coin_amounts already answers for 8."""
    assert ICP_DECIMALS == 8
    assert amount_to_base_units(1.0, ICP_DECIMALS) == 100_000_000
