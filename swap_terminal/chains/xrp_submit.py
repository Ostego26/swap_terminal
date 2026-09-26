#!/usr/bin/env python3
"""Submit an XRPL transaction, signing wherever the server allows -- theirs or ours.

Role: submodule (it holds one decision -- which machine signs -- and delegates
      the signing itself to xrpl-py and the RPC to chains/xrp_testnet.py)
Reads: the XRPL testnet endpoint in chains/xrp_testnet.py
Writes: nothing on disk
Can move funds: YES, indirectly and by design. It submits whatever transaction
      it is handed, signed with whatever secret it is handed. Treat a change
      here as fund movement.
Mainnet-safe: NO. It submits to whatever endpoint chains/xrp_testnet names, and
      its callers are responsible for having called refuse_mainnet() FIRST.

WHY THIS FILE EXISTS: THE PUBLIC TESTNET SERVER WILL NOT SIGN.

MEASURED 2026-09-26 on the operator's machine, against
s.altnet.rippletest.net build 3.4.1:

    submitting EscrowCreate: Fee=(autofilled) drops
    EscrowCreate -> notSupported: Signing is not supported by this server.

xrp_htlc_escrow.py had been written to use rippled's legacy `submit`, which
takes tx_json plus a `secret` and signs SERVER-SIDE. Its header said a public
testnet server "may" allow that. It does not, and that was a guess sitting in
the register of a fact (rule 17) -- refuted by the first run anyone took.

xrp_send_tagged.py already knew: it submits server-side, and on a refusal code
falls back to signing locally with xrpl-py, which is how today's real XRP
payment actually went out. That fallback lived inside the sender and was
Payment-shaped, so the escrow harness could not reach it -- rule 8's two copies
of one rule, except the second copy had not been written yet and the first one
was unreachable. SIGNING_REFUSED moved here, and both files use this.

WHICH MACHINE HOLDS THE KEY IS THE WHOLE SECURITY QUESTION, so the two paths
stay separately named rather than collapsing into one function with a flag, and
each call says on screen which one ran. Server-side signing sends the seed over
the wire to somebody else's rippled; local signing does not. On testnet with
faucet seeds that is a small matter, and the habit is what carries to a network
where it is not.

WHY NOT submit_and_wait(). xrp_send_tagged uses it deliberately, because a
SENDER must not report "submitted" for a transaction the ledger rejected. A
VERIFIER needs the opposite: steps 5 and 8 of xrp_htlc_escrow.py exist to
observe a REFUSAL, and submit_and_wait raises on one, which would turn the
expected result into an exception. So this returns the raw engine_result and the
caller decides what it means -- and the caller waits for validation separately
when it expects success. That difference is named at both sites (rule 8).
"""

from __future__ import annotations

from chains.xrp_signing import derive_and_check
from chains.xrp_testnet import TESTNET_URL, rpc

# Codes a server returns when it will not sign on your behalf. Matched exactly
# rather than by substring: an earlier version of this set tested
# `"ignInvalid" in str(status)`, a fragment of a GUESSED code, which would also
# match anything else containing those eight characters.
#
# `notSupported` is the one the public testnet actually answers, measured above.
SIGNING_REFUSED = frozenset({"notSupported", "noPermission", "internal", "srcActNotFound"})


class LocalSigningUnavailable(RuntimeError):
    """xrpl-py is not importable, so the fallback has nowhere to go."""


def submit_server_signed(tx_json: dict, secret: str) -> dict:
    """Submit through rippled's legacy server-side signing. THE SEED GOES OVER THE WIRE.

    Returns the `result` object verbatim, including a failure -- a caller
    expecting a refusal needs to see it rather than catch it.
    """
    return rpc("submit", {"secret": secret, "tx_json": tx_json})


def submit_locally_signed(tx_json: dict, secret: str) -> dict:
    """Sign here with xrpl-py, submit the blob. The seed does not leave this machine.

    ANY transaction type, from its XRPL-cased dict. `Transaction.from_xrpl()`
    dispatches on TransactionType, so EscrowCreate, EscrowFinish, EscrowCancel
    and Payment all arrive through one function -- which is what makes this
    shareable at all, where the sender's Payment-specific version was not.

    autofill fills only what is MISSING, so a Fee already in the dict survives.
    That matters for EscrowFinish, whose fee floor is not the reference fee:
    autofill would put 10 drops there and the ledger would answer
    telINSUF_FEE_P (see xrp_htlc_escrow.finish_fee_drops).

    Returns the same shape submit_server_signed does -- a dict with
    engine_result, engine_result_message and tx_json -- so the caller cannot
    tell which signer ran from the value, only from what this printed.
    """
    # LAZY, and PLC0415 is suppressed on each line for one checked reason that is
    # written here rather than on them: xrpl-py is an OPTIONAL dependency. Nothing
    # read-only in this tree needs it, and importing it at module scope would make
    # every diagnostic that touches chains/xrp_submit depend on it being
    # installed. chains/xrp_signing.py is lazy at its own import for the same
    # reason and carries the longer note. The justification lives above the block
    # because the import sorter re-wraps a long trailing comment onto its own
    # line, which detaches the noqa from the import it is about -- a suppression
    # whose reason has drifted away from its line is rule 12's defect, not a
    # formatting quibble.
    try:
        from xrpl.clients import JsonRpcClient  # noqa: PLC0415
        from xrpl.models.transactions.transaction import Transaction  # noqa: PLC0415
        from xrpl.transaction import autofill_and_sign  # noqa: PLC0415
        from xrpl.transaction import submit as submit_blob  # noqa: PLC0415
    except ImportError as error:
        raise LocalSigningUnavailable(
            "xrpl-py is not importable, so local signing is not available and this server will not sign. "
            "Install it into the active virtualenv: `python3 -m pip install xrpl-py`. Nothing was submitted."
        ) from error

    # derive_and_check REFUSES if the seed does not derive the Account in the
    # transaction. Signing with a key that is not the account's produces
    # `badSecret` or an invalid signature, and this says which key was wrong
    # without printing it.
    wallet = derive_and_check(secret, tx_json["Account"])
    transaction = Transaction.from_xrpl(tx_json)
    client = JsonRpcClient(TESTNET_URL)
    signed = autofill_and_sign(transaction, client, wallet)
    return submit_blob(signed, client).result


class Submitter:
    """Submits transactions, choosing the signer ONCE and saying which.

    Stateful on purpose. A nine-step harness submits six transactions, and
    probing server-side signing before each one would print the same refusal six
    times and cost six round trips to learn something already established. The
    first refusal switches this permanently and announces it; every later call is
    silent about the choice.

    `say` is the caller's own printer, so the announcement lands in the caller's
    format rather than this module's -- there is no print() here (rule 14 is
    about the operator's screen, which only the caller can see).
    """

    def __init__(self, say, *, prefer_local: bool = False) -> None:
        self.say = say
        self.local = prefer_local
        self.announced = False

    def submit(self, tx_json: dict, secret: str) -> dict:
        """One transaction. Falls back from server-side to local signing on a refusal."""
        if not self.local:
            result = submit_server_signed(tx_json, secret)
            status = str(result.get("engine_result") or result.get("error") or "")
            if status not in SIGNING_REFUSED:
                return result
            self.say(
                f"this server answered `{status}` -- it will not sign on your behalf, which public servers "
                "usually disable. That is a fact about the SERVER, not a failure. Switching to LOCAL signing "
                "with xrpl-py for this and every later transaction; the seed does not leave this machine."
            )
            self.local = True
            self.announced = True
        elif not self.announced:
            self.say("signing LOCALLY with xrpl-py; the seed does not leave this machine")
            self.announced = True
        return submit_locally_signed(tx_json, secret)
