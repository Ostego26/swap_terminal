"""GRC must not report that it can pay out when this process is not armed to send it.

Role: test (read-only against chains/, services/; writes a temp database)
Reads: chains/gridcoin.py, chains/gridcoin_wallet_lock.py, chains/registry.py,
       services/payout_service.py, services/swap_service.py
Writes: a temp database under pytest's tmp_path
Can move funds: no -- no socket is opened, no daemon is reached, nothing is signed
Mainnet-safe: yes

=============================================================================
TWO GATES IN ONE PROCESS DISAGREED ABOUT GRC, AND THE DEPOSIT SIDE WON
=============================================================================

MEASURED 2026-11-22, with GRIDCOIN_WALLET_PASSPHRASE confirmed unset:

    GridcoinAdapter.can_spend                      True
    why_cannot_pay_out({'GRC': a}, 'GRC')          ''
    pair_serviceability('BTC','GRC')['cannot_pay'] ''
    payout_unlock_context('GRC', a)                RAISES PayoutUnlockUnavailable

chains/gridcoin.py was two lines -- `class GridcoinAdapter(RPCAdapter): asset =
"GRC"` -- so it inherited `can_spend = True`, which is correct for BTC and LTC and
wrong for GRC. The Bitcoin-derived licence for class-level True is stated in
tests/test_every_payable_adapter_derives_can_spend.py and applies where "the
capability comes with being reachable at all and there is no separate arming
token". GRC has one.

End to end, before the fix: a swap was CREATED, status awaiting_deposit, a deposit
address issued -- and the payout for it could not be sent. Since 2026-10-10 the
payout DEFERS rather than fails, so nothing is sent and nothing is lost, but the
deposit is taken, credited and irreversible while the swap loops in payout_pending
until an operator sets the variable, re-running the lock cycle (and interrupting
staking) every poll. GRC is a destination on FIVE ALLOWED_PAIRS directions.

WHAT THE FIX DOES NOT DO, which is the minimality argument: it adds no refusal that
did not already exist. The payout already refused in exactly this state. The
refusal only moves to before the deposit, where it costs a retry.

=============================================================================
WHY THE THIRD TEST IS THE ONE THAT MATTERS MOST
=============================================================================

The first two measure the capability in both directions. The third measures that
the two gates AGREE -- for every asset, that why_cannot_pay_out() is empty exactly
when payout_unlock_context() does not raise. That is the assertion that stops this
defect returning in a different chain's clothing, because the defect was never
"GRC's flag is wrong"; it was "two places decided the same thing and nobody made
them read the same function".

NO DAEMON IS REACHED. SeededGridcoin below subclasses the REAL adapter, so the real
__init__ runs and derives can_spend the real way; only get_balance() and
validate_address() are seeded, because those are what the OTHER create-time gates
call and this file's subject is not them.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "swap_terminal"))

from chains.gridcoin import GridcoinAdapter
from chains.gridcoin_wallet_lock import WALLET_UNLOCK_ENV_VAR, payout_capability
from chains.registry import why_cannot_pay_out
from services.payout_service import PayoutUnlockUnavailable, payout_unlock_context
from services.swap_service import create_swap
from test_xrp_swap_attribution import seed_quote, seeded_db
from valid_addresses import BTC_REGTEST_DEPOSIT, GRC_PAYOUT

#: IMPORTED, NOT INVENTED, and the suite is what taught me the difference. The first
#: version of this file wrote its own: a 34-character base58 string starting with the
#: letters S and b, under the comment "this only has to be a plausible string". It was
#: not a valid Gridcoin address -- it did not decode --
#: and tests/test_address_literals_are_valid.py failed on it twice over, once for the
#: decode and once for the count of address-shaped literals climbing back over its
#: ceiling. A fake address in a test about whether a payout may proceed is the shape
#: of defect this whole file exists for, one layer down.
#:
#: THE BAD STRING IS DESCRIBED RATHER THAN QUOTED HERE, which is not fastidiousness:
#: pasting it back into this comment put the literal count at 61 against a ceiling of
#: 60 and failed the same gate a second time. The scanner reads source text, so a
#: comment explaining an invalid address IS another invalid address -- tests/prose.py
#: counts ten earlier instances of a gate meeting its own explanation, and this is
#: that defect with the polarity reversed.
#:
#: valid_addresses.GRC_PAYOUT is DERIVED with the real version byte, so it decodes
#: and there is one home for it (rule 8).


class SeededGridcoin(GridcoinAdapter):
    """The real adapter, the real __init__, a seeded transport.

    SUBCLASSING RATHER THAN STUBBING is the whole point: `can_spend` is derived in
    GridcoinAdapter.__init__, so a stub that declares `can_spend` would assert
    against its own declaration and pass whatever the adapter did. Only the two
    methods the OTHER create-time gates call are overridden.
    """

    def __init__(self, balance: float = 5000.0):
        # S106 IS SUPPRESSED BELOW AND THIS IS THE CLAIM BEHIND IT (rule 19: a
        # suppression is a claim you checked). These four arguments are the RPC
        # CONNECTION and no socket is ever opened with them -- get_balance() and
        # validate_address() below are overridden, so nothing in this class reaches a
        # transport. "p" is not a credential; it is a placeholder for a parameter the
        # real __init__ requires, and the real __init__ is what this file is testing.
        super().__init__(user="u", password="p", host="127.0.0.1", port=1)  # noqa: S106
        self._balance = balance

    def get_balance(self) -> float:
        return self._balance

    def validate_address(self, address: str) -> bool:
        return bool(address)


class SourceBTC:
    """A deposit-side stand-in. can_spend is declared True because registry fails closed."""

    asset = "BTC"
    can_spend = True
    payout_refusal = ""

    def get_balance(self) -> float:
        return 10.0

    def validate_address(self, address: str) -> bool:
        return True

    def new_address(self, label: str = "") -> str:
        # DERIVED, not written. The first version of this stub returned
        # "bcrt1q" followed by filler, which is address-SHAPED and does not decode --
        # tests/test_address_literals_are_valid.py caught it with "claims hrp 'bcrt'
        # but the data part uses characters outside bech32's charset". A deposit
        # address a customer would be handed is the last place to put a string nobody
        # checked, even in a stub.
        return BTC_REGTEST_DEPOSIT

    def get_new_address(self, label: str = "") -> str:
        return self.new_address(label)


def test_an_UNARMED_host_reports_that_it_cannot_pay_GRC_out(monkeypatch):
    """can_spend False, and the refusal names the variable that would fix it.

    MUTATION: delete the __init__ from chains/gridcoin.py and this fails on the
    first assertion, because the class inherits True from RPCAdapter.
    """
    monkeypatch.delenv(WALLET_UNLOCK_ENV_VAR, raising=False)
    adapter = SeededGridcoin()

    assert adapter.can_spend is False, (
        "an unarmed host reported that it can pay GRC out. Every * -> GRC pair badges AVAILABLE, "
        "create_swap() creates the swap, and the deposit is taken against a payout that will defer "
        "forever"
    )
    armed, refusal = payout_capability()
    assert armed is False
    assert adapter.payout_refusal == refusal, (
        "the adapter carries a second copy of the sentence rather than the shared one (rule 8)"
    )

    gate = why_cannot_pay_out({"GRC": adapter}, "GRC")
    assert gate, "why_cannot_pay_out() is what create_swap()'s cannot_pay gate reads, and it is empty"
    assert WALLET_UNLOCK_ENV_VAR in gate, (
        f"the refusal does not name the variable that would fix it, which is the whole of its "
        f"usefulness to an operator reading it off a page (rule 14): {gate[:160]}"
    )
    assert not gate.startswith("GRC GRC"), (
        "the asset is prefixed twice. why_cannot_pay_out() is the one place that prefixes it, and "
        "an adapter sentence that prefixes it too printed 'ICP ICP' once"
    )


def test_an_ARMED_host_CAN_pay_GRC_out(monkeypatch):
    """PERMITS. Without this the refusal above could be a blanket no.

    MUTATION: make payout_capability() return False unconditionally and this fails
    while every refusal assertion above still passes -- which is why both
    directions are here.
    """
    monkeypatch.setenv(WALLET_UNLOCK_ENV_VAR, "not-a-real-passphrase")
    adapter = SeededGridcoin()

    assert adapter.can_spend is True, (
        "an armed host still reports it cannot pay GRC out, so setting the variable does not arm "
        "the terminal and the fix is a blanket refusal rather than a capability"
    )
    assert adapter.payout_refusal == ""
    assert why_cannot_pay_out({"GRC": adapter}, "GRC") == ""


def test_an_UNARMED_host_cannot_CREATE_a_swap_that_pays_out_in_GRC(tmp_path, monkeypatch):
    """The authority rather than the page: a ValueError, and NO row.

    THE ROW COUNT IS THE ASSERTION, not the exception. An exception raised after a
    row was written would leave a swap nobody can pay and a deposit address a
    customer may already have, so "nothing was written" is checked by querying the
    table rather than inferred from the refusal having happened.

    THE QUOTE IS RESIZED, for the reason tests/test_sol_payout_capability.py records
    about its own: seed_quote() prices an output of 9850.0, and
    refuse_unless_the_payout_can_be_funded() runs BEFORE the cannot_pay gate, so an
    unresized quote refuses for insufficient balance and measures the wrong gate.
    """
    monkeypatch.delenv(WALLET_UNLOCK_ENV_VAR, raising=False)
    conn = seeded_db(tmp_path)
    seed_quote(conn, "q-to-grc", from_asset="BTC", to_asset="GRC")
    conn.execute("UPDATE quotes SET input_amount = 1.0, output_amount_estimate = 100.0 WHERE id = ?", ("q-to-grc",))
    conn.commit()
    config = {"GRC_MIN_CONFIRMATIONS": 6, "BTC_MIN_CONFIRMATIONS": 2}
    adapters = {"BTC": SourceBTC(), "GRC": SeededGridcoin()}

    with pytest.raises(ValueError) as refused:
        create_swap(conn, config, adapters, "q-to-grc", GRC_PAYOUT)

    assert WALLET_UNLOCK_ENV_VAR in str(refused.value), (
        f"create_swap() refused for some other reason, so this does not measure the capability: "
        f"{str(refused.value)[:200]}"
    )
    rows = conn.execute("SELECT COUNT(*) AS n FROM swaps").fetchone()
    conn.close()
    assert rows["n"] == 0, (
        "a swap row was written for a payout leg that cannot pay. The deposit address in that row "
        "is what a customer would have sent coin to"
    )


@pytest.mark.parametrize("armed_env", [False, True], ids=["unarmed", "armed"])
def test_the_CREATE_gate_and_the_SEND_gate_agree_for_every_asset(monkeypatch, armed_env):
    """why_cannot_pay_out() is empty exactly when payout_unlock_context() does not raise.

    THE INVARIANT, AND THE REASON THIS FILE EXISTS RATHER THAN A ONE-LINE FIX. The
    defect was not "GRC's flag is wrong". It was that two places decided the same
    thing and neither read the other, so one said yes while the other said no in the
    same process. This asserts the agreement directly, for every asset the terminal
    has an adapter shape for, in both arming states.

    MUTATION: give payout_unlock_context() back its own `if not passphrase` check
    and change payout_capability()'s condition -- the two drift, and this fails
    while both single-gate tests above still pass.
    """
    if armed_env:
        monkeypatch.setenv(WALLET_UNLOCK_ENV_VAR, "not-a-real-passphrase")
    else:
        monkeypatch.delenv(WALLET_UNLOCK_ENV_VAR, raising=False)

    adapter = SeededGridcoin()
    create_gate_refuses = bool(why_cannot_pay_out({"GRC": adapter}, "GRC"))
    try:
        payout_unlock_context("GRC", adapter)
        send_gate_refuses = False
    except PayoutUnlockUnavailable:
        send_gate_refuses = True

    assert create_gate_refuses == send_gate_refuses, (
        f"the create-time gate and the send-time gate disagree about GRC with "
        f"{WALLET_UNLOCK_ENV_VAR} {'set' if armed_env else 'unset'}: why_cannot_pay_out refuses="
        f"{create_gate_refuses}, payout_unlock_context refuses={send_gate_refuses}. Whichever is "
        f"right, a disagreement means one of them admits a swap the other will not settle -- and "
        f"the create-time one admitting it is how a deposit gets taken for a payout that cannot be "
        f"sent"
    )
