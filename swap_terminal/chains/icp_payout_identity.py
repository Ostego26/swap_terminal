"""Whether an ICP payout is ARMED in this process, and what an unarmed one means.

Role: function layer (rule 10 -- the smallest testable pieces. Two decisions:
      "is this process armed to pay ICP out" and "what does an unarmed one mean
      to an operator reading it off a page")
Reads: this process's environment, for ONE variable, and nothing else. No file
      -- not the identity PEM, not dfx's identity store -- no socket, no Config,
      no database. os.environ is read AT CALL TIME rather than at import, so a
      test can set it and a long-lived process cannot bake a stale answer in.
Writes: nothing.
Can move funds: no. Nothing here signs or submits anything. The money moves in
      chains/icp.ICPAdapter.send_to_address(), through a transport that
      chains/registry.py builds with the identity this module reports on.
Mainnet-safe: yes. Nothing here knows which network anything is on. ICP's
      mainnet refusal is elsewhere: icp/dfx.json names mainnet's ledger
      (ryjl3-tyaaa-aaaaa-aaaba-cai) under `remote.id.ic` precisely so dfx
      refuses to create one there.

=============================================================================
THE THIRD TIME THIS EXACT INSTRUCTION HAS BEEN GIVEN
=============================================================================

Operator, 2026-10-11: "what the fuck why is this hardcoded to not pay out. it
shouldn't."

They are right, and the reason it is worth a header rather than a line is that
this is the third chain to arrive with `can_spend = False` hardcoded and the
third time an operator had to notice:

    XRP   derived from the seed since 2026-10-02 -- from its PRESENCE then, and
          from the seed actually DECODING since 2026-10-03, after a
          nine-character placeholder on the operator's host made can_spend True
          and offered an XRP payout that could never have been signed.
    SOL   hardcoded False until 2026-10-03. chains/solana_payout_keypair.py's
          own header records what moved it: "Operator, 2026-10-03: 'whoa we have
          to be able to swap TO SOL too'."
    ICP   hardcoded False from 2026-10-06 until today.

EACH TIME THE CAPABILITY WAS ALREADY BUILT. That is what makes this a drift bug
rather than a feature request. For ICP, measured 2026-10-11:

  ICPAdapter.send_to_address()  fully implemented since 2026-10-06, including the
                                legacy `transfer` (icrc1_transfer cannot take a
                                64-hex account identifier, which is SHA224 and
                                not invertible), the fee read from the ledger and
                                passed explicitly, and a created_at_time
                                idempotency key it REFUSES to default -- because
                                with it null the ledger stamps its own time and
                                every retry is a new transaction.
  dfx_transport(..., identity=) grew the identity parameter on 2026-10-07, ONE DAY
                                after ICP landed, for a different caller: a top-up
                                from the minting account, which only the `minter`
                                identity can sign.
  chains/registry.py:280        called dfx_transport(service, timeout, network_url)
                                and passed no identity at all.

So the transport could sign, the adapter could pay, and the one argument joining
them was never passed. `can_spend = False` was an honest statement about the
transport it actually got -- and it was hardcoded where the other two chains
derive the same fact, so nothing in the tree could notice the gap closing.

tests/test_every_payable_adapter_derives_can_spend.py is the gate that stops a
FOURTH chain doing this. It is the rule 19 half of the fix: not "fix this
instance" but "stop the instance being creatable".

=============================================================================
WHY AN IDENTITY NAME AND NOT A PATH, WHICH IS WHERE ICP DIFFERS FROM SOL
=============================================================================

SOL's arming token is a FILE PATH that chains/solana_signing.py opens itself.
ICP's signing is done by `dfx`, which does not take a key on the command line:
it takes `--identity <name>` and resolves that name against its own identity
store (~/.config/dfx/identity/<name>/identity.pem). So the token here is a NAME,
and the PEM has to already be where dfx looks.

THAT MAKES THE MOUNT PART OF THE ARMING RATHER THAN A SEPARATE STEP, which is
what docker-compose.web.armed-icp.yml exists for: it mounts the PEM to the path
dfx resolves and declares the one `${VAR:?}` that refuses to start without it.
A name exported with no PEM behind it reads True here and fails at the send with
dfx saying the identity does not exist -- the same "present is not correct" line
chains/solana_payout_keypair.payout_keypair_is_present() draws, for the same
reason, and narrowing it would mean reading an identity store on every page
render.

WHICH IDENTITY IT MUST BE IS NOT A FREE CHOICE, and this is the part that has no
analogue on SOL or XRP. The ledger's `transfer` debits the CALLER, and every ICP
deposit address this terminal hands out is a SUBACCOUNT OF ICP_OWNER_PRINCIPAL
(chains/icp_account.account_identifier of that principal plus an index). So the
identity that can pay out is the one whose principal IS ICP_OWNER_PRINCIPAL --
on a local replica, the replica container's own default identity, which is where
that principal was read from in the first place. Any other identity is armed,
reaches the ledger, and has no coins.

NOTHING HERE VERIFIES THAT MATCH, and it is named rather than silently skipped
(rule 17): confirming it needs a dfx call to resolve the identity's principal,
which is a socket in a constructor on a page render. chains/icp.py's
verify_derivation() already exists for the owner-principal half and is the right
place for a check that needs the network.
"""

from __future__ import annotations

import os

#: The one variable this module reads. ASCII and no µ, per rule 6's boundary: a
#: name an operator types or a shell exports stays ASCII.
PAYOUT_IDENTITY_VARIABLE = "ICP_PAYOUT_IDENTITY"

__all__ = ["PAYOUT_IDENTITY_VARIABLE", "missing_identity_refusal", "payout_identity",
           "payout_identity_is_present"]


def payout_identity() -> str:
    """The dfx identity name to sign with, or "" when none is armed.

    STRIPPED, AND A SET-BUT-EMPTY VALUE IS ABSENT. `export ICP_PAYOUT_IDENTITY=''`
    is what a generator run from a shell without the value writes, and
    docker-compose passes every unset variable as `${VAR:-}` -- an empty STRING
    inside the container. config.py's own _env() carries what that cost on
    2026-09-26: five empty exports turned a missing setting into a crash at
    import. Here reading "" as present would be worse than a crash -- can_spend
    True, an ICP swap created, a customer's deposit taken, and the payout refused
    at the send with the deposit already credited.

    READ AT CALL TIME, not at import. A worker is long-lived and a test needs to
    set this; an import-time read bakes in whichever answer the first import saw.
    """
    return (os.environ.get(PAYOUT_IDENTITY_VARIABLE) or "").strip()


def payout_identity_is_present() -> bool:
    """Whether this process is ARMED to pay ICP out. Never says with what.

    A bool, deliberately, and the only thing any caller outside the send is
    allowed to learn. chains/icp.py uses it to set can_spend, which decides
    whether this terminal offers ICP as a payout destination at all -- so the
    question that reaches the customer page is "is a name exported", and neither
    the identity store nor one byte of a PEM goes near a rendered surface.

    PRESENCE IS NOT CORRECTNESS and the gap is stated rather than papered over.
    A name with no PEM behind it, or a PEM for an identity whose principal is not
    ICP_OWNER_PRINCIPAL, reads True here and fails later -- at the send for the
    first, and with a `InsufficientFunds` from the ledger for the second, since
    that identity's own account is empty. Both are after a deposit is taken,
    which is the same exposure chains/solana_payout_keypair.py documents for SOL
    and for the same reason: checking further means a dfx call on every adapter
    construction, and an adapter is constructed on a page render.
    """
    return bool(payout_identity())


def missing_identity_refusal() -> str:
    """The sentence a customer page, an admin page and a spawn banner all print.

    ONE sentence, read through chains/registry.why_cannot_pay_out(), for the
    reason chains/xrp_payout_seed.missing_seed_refusal() records in full and which
    is not re-argued here: on 2026-10-02 the spawn banner and the customer page
    disagreed about XRP in ONE process, because each had its own hand-written
    sentence about the same capability.

    WHAT THIS REPLACED IS THE REASON IT IS LONG. Until today chains/icp.py carried
    a hardcoded refusal whose own comment explains that it was written because the
    page had printed registry.py's FALLBACK sentence:

        ICP cannot pay out, and its adapter does not say why
        -- see chains/base.RPCAdapter.can_spend

    and that "every other unpayable chain names what to set: SOL names
    SOL_PAYOUT_KEYPAIR_PATH, XRP names XRP_PAYOUT_SECRET_SEED. ICP named nothing,
    so the page could show `in 0/5` and not say what would change it." The
    sentence it got then named the ledger's semantics and still named nothing to
    set, because at that moment nothing COULD be set. Now something can, so it
    names it -- which is the whole of rule 14's "state what the number means, next
    to the number".

    IT NAMES THREE THINGS BECAUSE THREE ARE NEEDED, and naming one of three sends
    the reader back for two more rounds. The overlay, because the PEM has to be
    where dfx looks. The variable, because dfx resolves a NAME. And the network
    url, because the web image has no docker CLI -- so the compose transport that
    would use the replica's own default identity cannot run there at all, and the
    url transport defaults to `--identity anonymous`.
    """
    return (
        f"cannot pay out from this process: the ledger's `transfer` debits the CALLER, so a payout "
        f"must be signed by the identity whose principal is ICP_OWNER_PRINCIPAL, and no "
        f"{PAYOUT_IDENTITY_VARIABLE} is set in this process -- every ledger call it makes is "
        f"unsigned, which is sufficient for balances, fees and block scans and cannot move funds. "
        f"This is why ICP -> * works and * -> ICP does not. To arm it: mount that identity's PEM "
        f"with docker-compose.web.armed-icp.yml, set {PAYOUT_IDENTITY_VARIABLE} to the dfx identity "
        f"name it is mounted as, and set ICP_DFX_NETWORK_URL (the web image has no docker CLI, so "
        f"the compose transport cannot run there). Placing that identity is key material and the "
        f"operator's."
    )
