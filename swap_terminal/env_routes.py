"""How a value REACHES a process here, in one sentence, spelled once.

Role: submodule -> function (one decision -- which routes exist for this value --
      over two inputs it is handed. It reads no file, no environment and no chain.)
Reads: nothing
Writes: nothing
Can move funds: no
Mainnet-safe: yes. Importing it does nothing at all.

=============================================================================
EIGHT MESSAGES SAID ".env DOES NOT REACH HERE" AND FIVE OF THEM WERE WRONG
=============================================================================

Measured 2026-10-13 on the operator's screen, in `swap_stack.py off` step 3:

    settings    21 name(s) loaded from .env; anything already exported in this
                shell wins, as it does for docker compose
    BTC  NOT TOUCHED  BTC has no adapter in this process: BTC_RPC_PORT is unset
         ... A value set only in .env, or only in another shell, does not reach
         here.

One process, 25 lines apart, contradicting itself. env_preamble.py landed on
2026-10-12 and made EVERY root tool load .env -- tests/test_env_preamble.py gates
that exhaustively, with gunicorn.conf.py (the serving path) and configure_env.py
(the writer) the two registered exceptions -- so the clause went false for ~30 of
the ~32 processes that print it, on the day the loader was added, in the sentence
the loader exists to make accurate.

A grep for the clause found EIGHT sites. Classified rather than counted, because
the count is not the finding:

    chains/registry.why_unconfigured()         FALSE from any root tool
    services/payout_service.py (XRP seed)      FALSE from settle_payout.py
    services/payout_service.py (unlock banner) FALSE from any root tool
    chains/xrp_payout_seed.py                  FALSE from swap_readiness.py
    chains/solana_payout_keypair.py            FALSE from swap_readiness.py
    pay_test_deposit.py                        FALSE IN ITS OWN FILE -- it
                                               imports env_preamble at line 96
                                               and says .env does not reach it
                                               at line 149
    regtest/operator_panel.py                  TRUE, and left alone: that file
                                               imports no config and calls no
                                               loader, which was CHECKED rather
                                               than assumed (rule 17)
    services/wizard.py                         a DOCSTRING quoting rendered
                                               output from before the change,
                                               so it is history and stays

env_file.py's own header is about the PREVIOUS version of this same defect: a
message asserting a port was unset while the container was talking to it. The
message was fixed by making the tools load .env; nobody went back for the
sentences that said they did not.

=============================================================================
WHY A MODULE AND NOT A CONSTANT IN env_file.py
=============================================================================

env_file.py is built on one discipline -- "For ENTRY POINTS only, never on
import" -- and the way that holds is that only root tools import it. Putting the
shared sentence there would give chains/, services/ and regtest/ a reason to
import the module whose single public function mutates os.environ, and the next
person to need "something from env_file" in a service would find the import
already there. So the sentence lives in a leaf of its own and env_file keeps its
caller list.

=============================================================================
TWO ROUTES, AND CONFLATING THEM WOULD BE WORSE THAN THE STALE CLAUSE
=============================================================================

Ordinary configuration -- a port, a URL, an RPC credential -- reaches the
container through the web service's own `environment:` block, and `docker compose`
reads .env itself to interpolate it.

SIGNING MATERIAL DOES NOT, BY DESIGN. A payout seed, a keypair path and a wallet
passphrase reach the container only through that chain's
`docker-compose.web.armed-<chain>.yml` overlay, which `configure_env.py --arm
<chain>` adds to COMPOSE_FILE. An unarmed container serves and cannot send, and
that is the safety story rather than a misconfiguration -- so a message that told
an operator to put XRP_PAYOUT_SECRET_SEED in .env and expect the container to
sign would be describing a system this one deliberately is not.

The caller says which it has. It knows: a module that refuses for want of a
signing seed is not guessing about that.
"""

from __future__ import annotations

#: The clause for ordinary configuration. Named rather than inlined so the two
#: branches below read as one sentence each, and so a reader comparing them sees
#: exactly where they diverge.
_PLAIN_CONTAINER_ROUTE = (
    "for the CONTAINER, the web service's own environment: block -- compose reads .env "
    "itself to interpolate it, but passes the container only what that block lists"
)


def how_a_value_arrives(variable: str = "", *, role: str = "this process", armed_chain: str = "") -> str:
    """Every route this value could have taken, for a message saying it took none.

    `variable` is named in the clause only when the sentence around it has NOT already
    named it. "" is the common case for a caller whose own first sentence says which
    variable is unset: chains/registry.why_unconfigured() names up to three, and
    repeating them produced "BTC_RPC_PORT, BTC_RPC_USER and BTC_RPC_PASS" twice in one
    paragraph. Rule 14 asks for the parameter beside the number, not for it twice.

    `role` is what to call the process in "the shell that starts ___" -- "this worker",
    "this panel", "the server". It is prose for a person and never an identifier.

    `armed_chain` is the ticker whose armed overlay carries this value, for signing
    material, or "" for ordinary configuration. The two produce materially different
    sentences and the header says why.

    WHAT IT DOES NOT SAY IS WHICH ROUTE FAILED, and that is deliberate (rule 17). "It is
    not in .env" is knowable from a root tool, where the loader ran, and NOT from inside
    the container, where it did not -- so a single sentence claiming it would be a
    hypothesis in one of the two places it prints. What is true in both is that the value
    did not arrive, and the routes are worth naming because the one an operator does not
    think of is the compose side.
    """
    shell = f"an export in the shell that starts {role}, which wins over .env as it does for compose"
    if armed_chain:
        container = (
            f"for the CONTAINER, {armed_chain.lower()}'s own docker-compose.web.armed-"
            f"{armed_chain.lower()}.yml overlay, which `python3 configure_env.py --arm "
            f"{armed_chain.lower()}` adds to COMPOSE_FILE -- an unarmed container serves and "
            f"CANNOT send, which is the default and is a safety property rather than a mistake"
        )
    else:
        container = _PLAIN_CONTAINER_ROUTE
    named = f" for {variable}" if variable else ""
    return (
        f"THE ROUTES IN{named}, and this process was reached by none of them: .env, which every "
        f"root tool in this tree loads at startup; {container}; or {shell}."
    )
