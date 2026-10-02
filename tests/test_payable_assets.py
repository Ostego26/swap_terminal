"""Which assets a payout could actually be BROADCAST on.

The question `supervisor.py` asks immediately before spawning three workers, and
prints as the last thing an operator reads before deciding whether to stop:

    about to spawn    a payout worker CAN broadcast on GRC, XRP. Stop now if this
                      database is pointed at a funded mainnet wallet.

THERE WERE NO TESTS FOR THIS FUNCTION, which is why it shipped answering wrongly
twice. Its own docstring records the first time (it named a payout capability the
process did not have, with no payout chain configured at all) and it did the same
thing again on 2026-10-02 for a different reason: it checked that an adapter
EXISTED and never that the adapter could SIGN.

Measured on the operator's host the moment they exported XRP_RPC_URL. One process
said both of these at once:

    supervisor    a payout worker CAN broadcast on GRC, XRP
    customer page XRP cannot pay out: it holds no signing key ... an XRP payout
                  raises and the swap lands in `failed` with the deposit already
                  credited

THE SIGNATURE IS WHY. It took `configured_assets` -- asset NAMES -- and a set of
strings cannot be asked whether it can sign. The check was unwritable without
changing the shape, so it now takes `adapters` and reads
chains/registry.why_cannot_pay_out(), the same authority services/pair_view.py:77
and services/swap_service.py:354 already read (rule 8: the survivor owns the
concept).
"""

from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))
sys.path.insert(0, str(REPO_ROOT / "swap_terminal"))

import supervisor  # noqa: E402
from chains import registry  # noqa: E402
from chains.registry import why_cannot_pay_out  # noqa: E402
from services import payout_service  # noqa: E402
from services.payout_service import payable_assets  # noqa: E402


class Signs:
    """An adapter that can broadcast -- the shape chains/base.RPCAdapter declares."""

    can_spend = True
    payout_refusal = ""


class CannotSign:
    """An adapter with no signing key. chains/xrp.py and chains/solana.py are both this."""

    can_spend = False
    payout_refusal = "holds no signing key, so a payout raises"


class SaysNothing:
    """An adapter that declares NEITHER attribute.

    chains/base.py's comment says the read is `getattr(adapter, "can_spend", False)`
    -- FAIL-CLOSED -- so an adapter that forgot to say must be treated as unable to
    pay, not as able. A third-party or half-written adapter defaulting to "yes it can
    send money" is the wrong direction to be wrong in.
    """


PAIRS = [("SOL", "GRC"), ("GRC", "XRP"), ("XRP", "GRC")]


def test_an_adapter_that_CANNOT_SIGN_is_not_payable():
    """The defect, exactly as the operator's host produced it.

    XRP is the TO leg of ("GRC","XRP") and had an adapter, so the old intersection
    returned it. It holds no signing key.
    """
    payable = payable_assets({"GRC": Signs(), "SOL": CannotSign(), "XRP": CannotSign()}, PAIRS)
    assert payable == {"GRC"}
    assert "XRP" not in payable, (
        "a destination whose adapter cannot sign is not a chain a payout can be broadcast on"
    )


def test_an_adapter_that_declares_NEITHER_attribute_fails_CLOSED():
    """chains/base.py's own comment calls for getattr(..., False). An adapter that
    does not say must not be read as able to move money."""
    assert payable_assets({"XRP": SaysNothing()}, [("GRC", "XRP")]) == set()


def test_an_asset_that_is_never_a_DESTINATION_is_not_payable():
    """SOL is deliberately only ever a FROM leg -- chains/solana.py cannot sign and
    ALLOWED_PAIRS carries ("SOL","GRC") and not the reverse. Having an adapter for it
    is not a payout capability."""
    assert payable_assets({"SOL": Signs(), "GRC": Signs()}, [("SOL", "GRC")]) == {"GRC"}


def test_a_destination_with_NO_adapter_is_not_payable():
    """The original defect this function was written for: a pair allowed on paper
    against a process that built no adapter for its destination."""
    assert payable_assets({"SOL": Signs()}, [("SOL", "GRC")]) == set()


def test_no_adapters_at_all_is_EMPTY_and_not_an_error():
    """`(none)` is a result (rule 14). The caller prints a banner either way, so this
    must return an empty set rather than raise -- a traceback above a spawn is not an
    answer to 'can this broadcast'."""
    assert payable_assets({}, PAIRS) == set()
    assert payable_assets({"GRC": Signs()}, []) == set()


def test_the_three_conditions_are_each_NECESSARY_and_none_sufficient():
    """Stated as a table because the function's docstring claims exactly this, and a
    claim in prose that no test drives is how this function was wrong twice."""
    signs, cannot = Signs(), CannotSign()
    # destination + adapter + can sign -> payable
    assert payable_assets({"GRC": signs}, [("SOL", "GRC")]) == {"GRC"}
    # destination + adapter, cannot sign -> not payable
    assert payable_assets({"GRC": cannot}, [("SOL", "GRC")]) == set()
    # destination + can sign, no adapter -> not payable
    assert payable_assets({}, [("SOL", "GRC")]) == set()
    # adapter + can sign, never a destination -> not payable
    assert payable_assets({"SOL": signs}, [("SOL", "GRC")]) == set()


def test_the_supervisor_banner_and_the_customer_page_cannot_disagree_about_XRP():
    """The two sentences one process said at once, now reconciled through one authority.

    chains/registry.why_cannot_pay_out() is what services/pair_view.py and
    services/swap_service.py read, and payable_assets() now reads it too rather than
    carrying a third spelling of the rule. This asserts the AGREEMENT, not the
    wording: whatever why_cannot_pay_out says about an asset, payable_assets must
    agree with it.
    """
    adapters = {"GRC": Signs(), "XRP": CannotSign()}
    payable = payable_assets(adapters, [("SOL", "GRC"), ("GRC", "XRP")])
    for asset in ("GRC", "XRP"):
        refused = bool(why_cannot_pay_out(adapters, asset))
        assert (asset in payable) != refused, (
            f"{asset}: payable_assets and why_cannot_pay_out disagree, which is the "
            f"two-copies-of-one-rule defect this change removed"
        )


# --- the BANNER, because the call site is what the mutation survived -----------


def test_the_spawn_banner_does_not_name_a_chain_that_cannot_sign(monkeypatch):
    """The call-site mutation that survived the first pass.

    Reverting supervisor.py to `payable_assets(adapters.keys(), ...)` passed every
    test above, because they all drive the function and none drove the sentence it
    exists to produce. That is the FIFTH call-site mutation to survive in this
    session -- a correct function whose caller hands it the wrong thing.

    `adapters.keys()` is a KeysView, so why_cannot_pay_out() cannot subscript it to
    reach an adapter. Whatever that does -- raise, or silently answer for nothing --
    the banner is the last line an operator reads before deciding whether to let
    three workers spawn against a possibly-funded wallet, so it is driven here.
    """
    # PATCHED ON chains.registry, NOT ON supervisor: spawn_warning() does a DEFERRED
    # import (`from chains.registry import build_adapters` inside the function body,
    # with a documented noqa), so the name is resolved from the registry module at
    # call time and setting it on supervisor would be setting an attribute nothing
    # reads -- a test that passes while patching nothing.
    monkeypatch.setattr(
        registry, "build_adapters", lambda _rpc: {"GRC": Signs(), "XRP": CannotSign()}
    )
    monkeypatch.setattr(supervisor.Config, "ALLOWED_PAIRS", [("SOL", "GRC"), ("GRC", "XRP")])
    # THE REAL unlock_readiness_lines() RUNS. It reads the environment, so the env var
    # is set rather than the function stubbed -- stubbing it would remove the very
    # interaction spawn_warning() exists to get right (its docstring records the two
    # sentences four lines apart that once disagreed).
    monkeypatch.setenv(payout_service.WALLET_UNLOCK_ENV_VAR, "not-a-real-passphrase")

    warning = supervisor.spawn_warning()
    assert "GRC" in warning, "the chain that CAN broadcast is still named"
    assert "XRP" not in warning, (
        "the banner named a chain whose adapter holds no signing key -- the exact "
        "sentence the operator's host printed on 2026-10-02 while the same process's "
        "customer page said XRP cannot pay out"
    )


def test_the_banner_says_NOTHING_CAN_BROADCAST_when_no_destination_can_sign(monkeypatch):
    """`(none)` is a result, and this one is the loud case: deposits are still watched
    and CREDITED while every payout refuses and lands its swap in 'failed', which
    nothing retries. An empty payable set must produce that sentence, not a cheerful
    one with an empty list in it."""
    monkeypatch.setattr(registry, "build_adapters", lambda _rpc: {"XRP": CannotSign()})
    monkeypatch.setattr(supervisor.Config, "ALLOWED_PAIRS", [("GRC", "XRP")])
    # THE REAL unlock_readiness_lines() RUNS. It reads the environment, so the env var
    # is set rather than the function stubbed -- stubbing it would remove the very
    # interaction spawn_warning() exists to get right (its docstring records the two
    # sentences four lines apart that once disagreed).
    monkeypatch.setenv(payout_service.WALLET_UNLOCK_ENV_VAR, "not-a-real-passphrase")

    warning = supervisor.spawn_warning()
    assert "CANNOT BROADCAST ANYTHING" in warning
    assert "CAN broadcast on" not in warning, "the two sentences must not both appear"
    assert "CREDITED" in warning, "and it says what still happens to the customer's coins"
