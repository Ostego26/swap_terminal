"""Whether a SOL payout is ARMED in this process, and what an unarmed one means.

Role: function layer (rule 10 -- the smallest testable pieces. Two decisions:
      "is this process armed to pay SOL out" and "what does an unarmed one
      mean to an operator reading it off a page")
Reads: this process's environment, for ONE variable, and nothing else. No file
      -- not even the one that variable NAMES -- no socket, no Config, no
      database. os.environ is read AT CALL TIME rather than at import, so a
      test can set it and a long-lived process cannot bake a stale answer in.
Writes: nothing.
Can move funds: no. Nothing here signs, serializes or submits anything, and
      nothing here returns the keypair PATH either, let alone the key: the two
      functions below return a bool and a sentence. The money moves in
      chains/solana_signing.signed_transfer_wire(), which reads the path for
      itself through keypair_path_from_environment().
Mainnet-safe: yes. Nothing here knows which network anything is on -- the
      mainnet refusal is chains/solana_signing.require_devnet(), decided from
      the genesis hash the CLUSTER reports, and it runs in the preview as well
      as in the send whether or not this process is armed.

THIS MODULE DELIBERATELY NEVER OPENS THE KEY FILE, which is the single property
worth reading this header for. It answers "is a path exported", by `bool()` of
the variable's value, exactly as chains/xrp_payout_seed.signing_seed_is_present()
answers `bool()` of the seed's. The ONLY place in this tree's Python where the
secret itself is read is chains/solana_signing.load_payout_keypair(), whose only
non-test caller is signed_transfer_wire() -- so the whole lifetime of a SOL
secret is one function call, after the arming token has already matched and
after the cluster has already been proven to be devnet.

That is not a stylistic preference. can_spend is read by every surface this
application renders -- the customer page, the admin page, the operator panel,
the worker spawn banner -- and a capability check that opened a key file would
mean a key read on page load, by a Flask request handler, in a process that is
not paying anybody. Presence of the path is the question those surfaces are
actually asking, and it is the only one answered here.

=============================================================================
WHY THIS FILE EXISTS, AND WHAT IT REPLACED (2026-10-03)
=============================================================================

Operator, 2026-10-03: "whoa we have to be able to swap TO SOL too".

Until that instruction chains/solana.py carried

    can_spend = False

HARDCODED, with a comment saying flipping it was the operator's call, and
chains/solana_signing.py's header named that hardcoding as a DELIBERATE
divergence from XRP, whose can_spend has been derived from seed presence since
2026-10-02. The divergence is over: SOL is now derived too, from this module,
and both sites' prose has been corrected rather than left describing a
difference that no longer exists (rule 8 asks that a real difference be stated
at both sites; a difference that ENDS makes both of those statements wrong
comments, which rule 16 counts as bugs).

WHAT IS HONESTLY WEAKER NOW, said plainly here rather than discovered later.
Before this, configuration alone could not arm a SOL payout: the call site in
services/payout_service.py passed two positional arguments, so every SOL payout
was refused by chains/solana_signing.require_send_confirmation() no matter what
was exported. It can now: a process started with SOL_PAYOUT_KEYPAIR_PATH
pointing at a readable keypair CAN sign and broadcast a SOL transfer, and
anything that can run code in that process can read the path and open the file.

Every narrowing that was already there is UNCHANGED and none was weakened to do
this -- stated as a list because "I kept the guards" is the kind of claim that
should be checkable rather than trusted:

  - devnet only, by the cluster's OWN genesis hash, in the PREVIEW as well as
    in the send. There is no flag, variable or argument that turns it off.
  - the exact arming token CONFIRM_SOL_SEND at the call site.
  - the derived public key must equal the payer the preview announced, or
    nothing is signed.
  - the rent floor on a destination that does not exist yet, and the lamport
    headroom on the payer, both in integer lamports.
  - the signed bytes are parsed back out and compared against the plan before
    they are broadcast.

AND THE REFUSAL IS STILL THE DEFAULT. With the variable unset -- which is every
checkout, every test run and every host where the operator has not made the
custody decision -- payout_keypair_is_present() is False, chains/solana.py sets
can_spend False on the instance, chains/registry.why_cannot_pay_out() reports
the sentence below, and services/swap_service.create_swap() refuses to create a
swap whose payout leg is SOL. Nothing is created, so nothing is taken.

ONE POSTURE SWITCH IS STILL THE OPERATOR'S AND THIS FILE DOES NOT THROW IT.
Config.ALLOWED_PAIRS names no pair that pays out in SOL (counted 2026-10-03:
zero entries with SOL in the second position), so arming this changes what the
payout path WOULD do and changes nothing about what it IS asked to do. Enabling
such a pair is live posture and is the operator's (rule 16).

AND NO TRANSACTION FROM THIS PATH HAS EVER REACHED A CLUSTER. Re-measured
2026-10-03: api.devnet.solana.com answers 403 at this container's proxy, so the
serialization is cross-checked against an independent implementation
(chains/solana_transaction.py's header has that measurement) and the BROADCAST
is verified against nothing. Arming is a capability, not a rehearsal.
"""

from __future__ import annotations

# THE NAME OF THE VARIABLE IS IMPORTED, NOT SPELLED AGAIN (rule 8).
#
# chains/solana_signing.KEYPAIR_PATH_VARIABLE has owned the string
# "SOL_PAYOUT_KEYPAIR_PATH" since 2026-10-02 because that is the module that
# READS it, through keypair_path_from_environment(). A second literal here
# would be two copies of one name: they would agree on the day they were
# written, and the day somebody renamed one the refusal on the customer page
# would name a variable nothing reads -- which is exactly the shape of the
# 2026-09-26 incident chains/registry.why_unconfigured() records, where the
# operator's value sat under GRC_TESTNET_RPC_PASS.
#
# So the signing module owns the NAME and the VALUE, and this module owns the
# QUESTION "is it set". Importing it this way round, rather than moving the
# constant here, also keeps every existing importer of
# chains.solana_signing.KEYPAIR_PATH_VARIABLE working unchanged.
from .solana_signing import KEYPAIR_PATH_VARIABLE, keypair_path_from_environment

__all__ = ["KEYPAIR_PATH_VARIABLE", "missing_keypair_refusal", "payout_keypair_is_present"]


def payout_keypair_is_present() -> bool:
    """Whether this process is ARMED to pay SOL out. Never says with what.

    A bool, deliberately, and the only thing any caller outside the send is
    allowed to learn. chains/solana.py uses it to set can_spend, which decides
    whether this terminal offers SOL as a payout destination at all -- so the
    question that reaches the customer page is "is a path exported", and
    neither the path nor one byte of the file it names goes anywhere near a
    rendered surface.

    IT DOES NOT OPEN THE FILE, AND THE DISTINCTION IS THE SAME ONE
    chains/xrp_payout_seed.signing_seed_is_present() DRAWS ABOUT "PRESENT" NOT
    BEING "CORRECT". A path pointing at a file that is missing, oversized, not
    JSON, the wrong length, or holding a key for a DIFFERENT account still
    reads True here -- and every one of those is refused later by
    chains/solana_signing.load_payout_keypair() and derive_and_check(), BEFORE
    anything is signed, because the public key they derive will not be the
    payer the preview announced. Claiming more than presence here would be the
    reassuring answer rather than the measured one (rule 17), and it would cost
    a key file read on every page render to claim it.

    A SET-BUT-EMPTY VALUE IS ABSENT, and keypair_path_from_environment() is
    what strips it. `export SOL_PAYOUT_KEYPAIR_PATH=''` is what a generator run
    from a shell without the value writes; config.py's own _env() carries the
    measurement that cost (five empty exports on 2026-09-26 turned a missing
    setting into a crash at import). Here the consequence of reading "" as
    present would be worse than a crash: can_spend True, a SOL swap created, a
    customer's deposit taken, and then the payout refused at the send with the
    deposit already credited.
    """
    return bool(keypair_path_from_environment())


def missing_keypair_refusal() -> str:
    """The sentence a customer page, an admin page and a spawn banner all print.

    ONE sentence, read through chains/registry.why_cannot_pay_out(), for the
    reason chains/xrp_payout_seed.missing_seed_refusal() records in full and
    which is not re-argued here: on 2026-10-02 the spawn banner and the
    customer page disagreed about XRP in ONE process, because each had its own
    hand-written sentence about the same capability. Both are derived now, and
    SOL is derived from the start rather than after the same incident.

    IT NAMES THE VARIABLE AND NOT ITS VALUE -- not the path, which is not a
    secret but is also not the operator's question, and emphatically not
    anything from inside the file. Rule 14: echo the parameter that decides the
    answer, because the operator reads the screen rather than this file, and a
    worker inherits the shell that spawned it with nothing downstream able to
    see which shell that was.

    IT ALSO NAMES THE SECOND VARIABLE. SOL_HOT_WALLET is what the adapter
    announces as the payer, and chains/solana_signing.derive_and_check()
    refuses to sign when the keypair does not derive to it -- so a host with
    the keypair and no hot wallet is armed and still cannot pay. Naming one of
    two sends the reader back for a second round, which is the mistake
    tests/test_allowed_pairs_are_serviceable.py::
    test_the_XRP_PAYOUT_BLOCKER_IS_NOT_THE_RESERVE is named after.
    """
    return (
        f"cannot pay out: {KEYPAIR_PATH_VARIABLE} is not set in this process's environment, so there "
        f"is no signing keypair and every SOL payout would refuse before signing. This is the DEFAULT "
        f"and it is why no swap paying out in SOL can be created: a swap that cannot be paid out would "
        f"take a deposit it can never settle. Export {KEYPAIR_PATH_VARIABLE} in the shell that starts "
        f"this process -- a value set in a file, or in another shell, does not reach here -- pointing "
        f"at the keypair file that owns the account SOL_HOT_WALLET announces, and set SOL_HOT_WALLET "
        f"to that account. Both are custody decisions and neither has a default. The file's CONTENTS "
        f"are never read, printed or logged by anything that renders this sentence."
    )
