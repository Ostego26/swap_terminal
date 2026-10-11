"""ICP can pay out when an identity is armed, and says what to set when it cannot.

Role: tests (chains/icp_payout_identity.py and ICPAdapter's derived can_spend)
Reads: ICP_PAYOUT_IDENTITY from a monkeypatched environment. No socket, no dfx, no
       file -- not even the identity PEM, which is the module's whole design.
Writes: nothing
Can move funds: no. No transport is built and no transfer is attempted; the
       adapter under test is constructed with a stub `call` that returns "()".
Mainnet-safe: yes

=============================================================================
"what the fuck why is this hardcoded to not pay out. it shouldn't."
=============================================================================

Operator, 2026-10-11, reading the ATM page's * -> ICP column. They were right, and
the measurement behind it is that the capability had been complete for five days:

  ICPAdapter.send_to_address()  implemented 2026-10-06, with the legacy `transfer`
                                (icrc1_transfer cannot take a 64-hex account
                                identifier, which is SHA224 and not invertible),
                                the fee read from the ledger, and a created_at_time
                                idempotency key it REFUSES to default because a
                                null one makes every retry a new transaction.
  dfx_transport(..., identity=) added 2026-10-07, ONE DAY later, for the
                                minting-account top-up.
  chains/registry.py:280        passed three arguments and no identity.

So the transport could sign, the adapter could pay, and nothing joined them. The
`can_spend = False` in the class body was an honest statement about the transport
it was handed -- and being a literal, it could not go stale when the transport
grew the ability to sign.

WHAT IS NOT TESTED HERE, AND IT IS THE PART THAT MATTERS MOST (rule 17: say which
you have). NO ICP PAYOUT HAS EVER BEEN SIGNED OR BROADCAST BY THIS CODE, on any
network. Everything below is a unit test of the ARMING DECISION -- whether this
process believes it can pay, and what it says when it cannot. That a `transfer`
actually leaves the desk's account on the local replica and returns a block index
is unproven, and it is the proof this repository asks for. The first run belongs
on the local replica and is the operator's.
"""

from __future__ import annotations

import pathlib
import re

import pytest
from chains.icp import ICPAdapter
from chains.icp_payout_identity import (
    PAYOUT_IDENTITY_VARIABLE,
    missing_identity_refusal,
    payout_identity,
    payout_identity_is_present,
)
from chains.registry import why_cannot_pay_out

#: The anonymous principal, already this suite's ICP fixture elsewhere
#: (tests/test_an_unreachable_confirmation_threshold_is_refused.py).
PRINCIPAL = "2vxsx-fae"
LEDGER = "bkyz2-fmaaa-aaaaa-qaaaq-cai"


def stub_call(*_args, **_kwargs) -> str:
    """A transport that reaches nothing. Constructing an adapter must open no socket."""
    return "()"


def adapter() -> ICPAdapter:
    return ICPAdapter(LEDGER, PRINCIPAL, stub_call)


@pytest.mark.parametrize("value", ["", "   ", None])
def test_an_absent_or_blank_identity_is_unarmed(monkeypatch, value):
    """MUTATION: drop the .strip(), or use `is not None` instead of bool().

    A SET-BUT-EMPTY VALUE IS THE REALISTIC ONE, not a contrivance:
    docker-compose.web.yml passes every variable as `${VAR:-}`, so anything unset in
    the shell arrives in the container as an empty STRING. Reading that as armed
    would be worse than a crash -- can_spend True, an * -> ICP swap created, a
    customer's deposit taken, and the payout refused at the send with the deposit
    already credited.
    """
    if value is None:
        monkeypatch.delenv(PAYOUT_IDENTITY_VARIABLE, raising=False)
    else:
        monkeypatch.setenv(PAYOUT_IDENTITY_VARIABLE, value)

    assert payout_identity() == ""
    assert payout_identity_is_present() is False
    assert adapter().can_spend is False


def test_an_armed_identity_makes_ICP_a_payout_destination(monkeypatch):
    """THE ONE THE OPERATOR ASKED FOR. MUTATION: restore the hardcoded can_spend.

    Asserted through chains/registry.why_cannot_pay_out(), which is what the customer
    page, the admin page and the spawn banner all read -- not through the attribute
    directly. The attribute is the mechanism; the empty string from that function is
    what makes a pair offerable, so it is what the operator's question was about.
    """
    monkeypatch.setenv(PAYOUT_IDENTITY_VARIABLE, "desk")
    armed = adapter()

    assert armed.can_spend is True
    assert why_cannot_pay_out({"ICP": armed}, "ICP") == "", (
        "ICP is armed and the registry still refuses it as a destination"
    )
    # AND NO REFUSAL SENTENCE SURVIVES ON AN ARMED INSTANCE. Leaving the class-level
    # one in place would hand any surface reading payout_refusal unconditionally a
    # reason for a chain that can pay -- the wrong-comment bug with a page behind it.
    assert armed.payout_refusal == ""


def test_an_unarmed_adapter_names_all_three_things_to_set(monkeypatch):
    """MUTATION: shorten the refusal to name only the variable.

    THE SENTENCE THIS REPLACED NAMED NOTHING TO SET, and chains/icp.py's own comment
    records why that mattered: the page printed registry.py's FALLBACK -- "ICP cannot
    pay out, and its adapter does not say why" -- and the fix then written still named
    no setting, "so the page could show `in 0/5` and not say what would change it".

    THREE, BECAUSE THREE ARE NEEDED. The overlay (the PEM must be where dfx looks),
    the variable (dfx resolves a NAME), and the network url (the web image has no
    docker CLI, so the compose transport that would use the replica's own default
    identity cannot run there at all). Naming one of three sends the reader back for
    two more rounds.
    """
    monkeypatch.delenv(PAYOUT_IDENTITY_VARIABLE, raising=False)
    refusal = why_cannot_pay_out({"ICP": adapter()}, "ICP")

    assert refusal.startswith("ICP ")
    for needed in (PAYOUT_IDENTITY_VARIABLE, "docker-compose.web.armed-icp.yml",
                   "ICP_DFX_NETWORK_URL", "ICP_OWNER_PRINCIPAL"):
        assert needed in refusal, f"the refusal does not name {needed}: {refusal}"
    # And it still carries the LEDGER reason, which is the durable half: `transfer`
    # debits the caller, which is why ICP -> * works and * -> ICP does not.
    assert "debits the CALLER" in refusal


def test_the_refusal_never_contains_an_identity_or_a_path(monkeypatch):
    """A refusal is rendered on a customer-facing page. MUTATION: interpolate the value.

    The identity NAME is not a secret, and it is also not the operator's question --
    and the sentence is read by a surface that renders to anyone who can reach the
    app, which is unauthenticated behind a loopback bind. Naming the variable is
    actionable; echoing its value is a habit that ends with a PEM path, then a PEM.
    """
    monkeypatch.setenv(PAYOUT_IDENTITY_VARIABLE, "an-identity-name-that-must-not-be-echoed")

    # Armed, so there is no refusal at all -- and the class-level fallback, which IS
    # rendered by anything reading the class, must not carry it either.
    assert "must-not-be-echoed" not in ICPAdapter.payout_refusal
    assert "must-not-be-echoed" not in missing_identity_refusal()


def test_the_module_reads_the_environment_at_call_time(monkeypatch):
    """MUTATION: read os.environ at import into a module-level constant.

    A worker is long-lived and a test has to be able to set this. An import-time read
    bakes in whichever answer the first import saw -- and this is the mistake I made
    in configure_env.py earlier the same day with a derived dict, where it silently
    redirected six tests to the operator's real files.
    """
    monkeypatch.setenv(PAYOUT_IDENTITY_VARIABLE, "first")
    assert payout_identity() == "first"
    monkeypatch.setenv(PAYOUT_IDENTITY_VARIABLE, "second")
    assert payout_identity() == "second", "the value was captured at import rather than read"


def test_the_overlay_mounts_the_pem_where_dfx_resolves_the_name_it_sets():
    """The two halves of arming must AGREE, and nothing else checks that they do.

    docker-compose.web.armed-icp.yml sets ICP_PAYOUT_IDENTITY to a literal and mounts
    the PEM to a path. dfx resolves `--identity <name>` against
    ~/.config/dfx/identity/<name>/identity.pem, so if the mount path and the literal
    disagree the container is armed, reports can_spend True, and fails at the send
    with dfx saying the identity does not exist -- after a deposit is taken.

    MUTATION: change either the literal or the directory in the mount path.
    """
    overlay = (pathlib.Path(__file__).resolve().parent.parent
               / "docker-compose.web.armed-icp.yml").read_text()
    code = "\n".join(line for line in overlay.splitlines() if not line.lstrip().startswith("#"))

    name = re.search(r"ICP_PAYOUT_IDENTITY:\s*(\S+)", code)
    assert name, "the overlay sets no ICP_PAYOUT_IDENTITY"
    mount = re.search(r"/\.config/dfx/identity/([^/]+)/identity\.pem", code)
    assert mount, "the overlay mounts nothing to a dfx identity path"

    assert name.group(1) == mount.group(1), (
        f"the overlay sets ICP_PAYOUT_IDENTITY={name.group(1)} and mounts the PEM as "
        f"{mount.group(1)}; dfx would not find the identity it was told to sign with"
    )
