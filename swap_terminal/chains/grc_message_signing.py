"""`verifymessage` against our own Gridcoin daemon: a read-only cryptographic check.

Role: function level (one decision: "does this signature verify this message for
      this address?"). It takes an adapter as an argument and holds no
      connection of its own, so it is callable with a stub.
Reads: a Gridcoin wallet daemon over JSON-RPC -- `verifymessage`, and nothing
      else. No wallet file, no key, no database, no environment.
Writes: nothing. Not to disk, not to the chain, not to the wallet.
Can move funds: no. The single RPC method named in this module cannot spend, and
      the module imports nothing that can. It also never calls
      `walletpassphrase` and holds no passphrase to call it with.
Mainnet-safe: yes. `verifymessage` is a pure read -- see "WHAT WAS ESTABLISHED"
      below -- so running it against a mainnet daemon changes nothing. Pointing
      it at mainnet is nonetheless not what the address-proof panel is for; the
      port the adapter carries is config.Config's business.

=============================================================================
WHY THERE IS A WRAPPER AT ALL AND NOT JUST adapter.call("verifymessage", ...)
=============================================================================

Because `adapter.call()` raises for three completely different conditions and
the caller must tell them apart. chains/base.py::RPCAdapter.call() reads the
body before the status (its own comment records why, and the measurement that
forced it), so what reaches a caller is:

  requests.RequestException   the daemon could not be reached at all -- wrong
                              port, not running, timed out. The question is
                              UNANSWERED and is still answerable later.
  RPCError with code -3/-5    the daemon was reached, understood the request,
                              and refused the INPUT: "Invalid address",
                              "Address does not refer to key", "Malformed
                              base64 encoding". The question is answered: what
                              was pasted cannot be checked as it stands.
  RPCError with any other     something else. Treated as unanswered.
  code
  a plain `False` result      the daemon was reached and the signature DOES NOT
                              VERIFY. This is the real negative answer, and it
                              is the only one that means "this person does not
                              control this address".

CLAUDE.md rule 12 is explicit that a broad catch "is never legitimate when the
caller cannot tell the failure from a real answer", and this is that case in its
purest form: "your signature did not verify" and "we could not check right now"
are different sentences, lead to different HTTP statuses, and only one of them
is a reason for the customer to go and look at what they pasted. Collapsing
them into a bool would be the defect rule 12 names, on the one function the
whole feature rests on.

So the failures are raised as DISTINCT exception types and the success is a
bool. Nothing here returns a plausible value for a failure.

=============================================================================
WHAT WAS ESTABLISHED, AND WHAT IS STILL BELIEVED (rule 17)
=============================================================================

The safety argument for the whole address-proof feature is that `verifymessage`
needs NO WALLET UNLOCK -- that it is pure signature math, touching no key and no
wallet, so this desk never holds a passphrase and never asks for one.

ESTABLISHED, by reading Gridcoin's own source at commit 36bc6a2d (the
gridcoin-community/Gridcoin-Research master branch, cloned and read on
2026-10-02 rather than recalled):

  src/wallet/rpcwallet.cpp, `UniValue verifymessage(const UniValue& params)`.
  Its whole body is `LOCK(cs_main)`, `DecodeDestination`, `DecodeBase64`,
  `CPubKey::RecoverCompact`, and a comparison of the recovered key's ID against
  the address's. It NEVER references `pwalletMain` and it NEVER calls
  `EnsureWalletIsUnlocked()`.

  `signmessage`, twenty lines above it in the same file, DOES both: it takes
  `LOCK2(cs_main, pwalletMain->cs_wallet)`, calls `EnsureWalletIsUnlocked()`,
  and fetches the private key with `pwalletMain->GetKey()`. The asymmetry is
  the point -- signing is the half that needs the wallet, and signing happens
  on the CUSTOMER's machine, in their own client, never here.

  There is no wallet-locked gate in the dispatcher either: the command table in
  src/rpc/server.cpp carries no "requires unlocked" column, and
  `EnsureWalletIsUnlocked()` appears only inside the eleven handlers that need
  it (grepped; `verifymessage` is not among them).

  The PARAMETER ORDER is `address, signature, message` -- read from the help
  declaration and from the three `params[N].get_str()` lines, not assumed. This
  matters because the obvious guess is (address, message, signature): that
  order would hand the challenge to `DecodeBase64` and the signature to the
  hasher, and the daemon would answer "Malformed base64 encoding" or simply
  `false`. A proof system that always says no is indistinguishable from one
  that works and has no honest users.

  CONFIRMED INDEPENDENTLY, 2026-10-02, AGAINST THE OPERATOR'S RUNNING TESTNET
  DAEMON on 127.0.0.1:25715 -- `help verifymessage` printed

      verifymessage <Gridcoinaddress> <signature> <message>

  which is the same order this file read out of master's source. Two
  independent reads, one of the source and one of the installed binary, and
  they agree. Noted because the brief this feature was written from stated the
  order as (address, message, signature), and that guess is exactly the one the
  paragraph above predicts would fail silently.

AND THE TWO METHODS DO NOT SHARE AN ORDER. Measured on the same daemon in the
same session:

    signmessage   <Gridcoinaddress> <message>
    verifymessage <Gridcoinaddress> <signature> <message>

`signmessage` is address-then-MESSAGE. `verifymessage` is
address-then-SIGNATURE-then-message. The second argument means a different
thing in each. THIS ASYMMETRY IS NOT A MISTAKE AND MUST NOT BE TIDIED INTO
CONSISTENCY: it is the daemon's own interface, it is what a customer types into
their own client (`signmessage <their address> "<challenge>"`), and
services/grc_login_service.py builds that exact line for them. A future reader
who "fixes" either call site to match the other breaks the half they did not
run -- which on this path means every verification silently answering no, with
no error anywhere, for as long as it takes somebody to notice that nobody has
ever passed.

STILL BELIEVED AND NOT MEASURED: that the operator's RUNNING DAEMON needs no
unlock for this method. There is no Gridcoin daemon in the environment this was
written in and no shell access to the operator's host, so the source read above
is authoritative for master and not necessarily for whatever binary is
installed.

WHAT IS EVIDENCE AND NOT PROOF, kept in that register on purpose (rule 17). On
the operator's running testnet daemon, `help verifymessage` prints only "Verify
a signed message" and names no key and no wallet, while `help signmessage`
prints "Sign a message with the private key of an address" and names both. That
is consistent with everything above and it is not a measurement of behavior: a
help string is documentation, and documentation is the thing that drifts from
code.

THE DECISIVE READ-ONLY CHECK, which anyone with a prompt on that host can run:
call `verifymessage` with a well-formed address, a DELIBERATELY BOGUS signature
and any message.

  it returns `false`   -> no wallet was consulted. The method reached
                          `RecoverCompact`, failed to recover a key from
                          nonsense, and said so. This is the PASS, and `false`
                          is the expected and desired answer -- it is not an
                          error and not a failure of the check.
  it errors about the  -> the belief is false for this build, the feature's
  wallet being locked     safety argument has changed, and WalletUnlockDemanded
                          below is what the code does about it.
  it errors about       -> also a pass for the question being asked. It still
  malformed base64        got as far as parsing the signature without a wallet,
                          which is the same conclusion.

No passphrase is needed to run it, and nothing in it is a placeholder for one.
The exact command is in the report that accompanied this commit, and it names
the testnet port only.

AND THE FALLBACK IS IN THE CODE RATHER THAN IN A PARAGRAPH. If that daemon ever
answers `verifymessage` with RPC code -13 (`RPC_WALLET_UNLOCK_NEEDED`, the code
`EnsureWalletIsUnlocked()` throws -- read from src/rpc/protocol.h at the same
commit), then the belief above is false for that build and the feature's safety
argument has changed. That specific code therefore raises its own exception type,
`WalletUnlockDemanded`, so the panel says so in those words instead of reporting
a generic outage. NOTHING in this repository will respond to it by unlocking a
wallet.
"""

from __future__ import annotations

import logging
import re

import requests

from .base import RPCError

logger = logging.getLogger(__name__)

# The RPC method. Named once so the string that reaches a daemon and the string
# a test asserts on are the same object.
VERIFY_MESSAGE_METHOD = "verifymessage"

# Gridcoin's own JSON-RPC error codes, read from src/rpc/protocol.h at commit
# 36bc6a2d rather than recalled. Only the three that `verifymessage` can
# actually produce are named; the rest of that enum is not this module's
# business.
#
#   -3  RPC_TYPE_ERROR              "Address does not refer to key",
#                                   "Malformed base64 encoding"
#   -5  RPC_INVALID_ADDRESS_OR_KEY  "Invalid address"
#  -13  RPC_WALLET_UNLOCK_NEEDED    what EnsureWalletIsUnlocked() throws, and
#                                   which verifymessage() must never reach. See
#                                   the module docstring: this arriving refutes
#                                   the feature's safety argument for that build.
RPC_TYPE_ERROR = -3
RPC_INVALID_ADDRESS_OR_KEY = -5
RPC_WALLET_UNLOCK_NEEDED = -13

# The two codes that mean "the daemon answered, and the answer is about what you
# pasted". A frozenset rather than two comparisons so adding a third code is one
# edit in one place.
INPUT_REFUSAL_CODES = frozenset({RPC_TYPE_ERROR, RPC_INVALID_ADDRESS_OR_KEY})

# HOW THE CODE IS RECOVERED, AND THE COUPLING THAT MAKES IT POSSIBLE.
#
# chains/base.py::rpc_error_from_body() flattens the daemon's error OBJECT into a
# STRING, deliberately -- its docstring says why: that string is what lands in
# swaps.failed_reason and is read by a person. The format it builds is
#
#     f"{message} (rpc code {code})"
#
# so the code is still there, at the end, and this pattern reads it back. That is
# a coupling between two files and it is named here rather than left for a reader
# to discover (rule 8): if that f-string changes shape, this regex stops matching
# and _classify() below falls through to "unanswered" -- which FAILS CLOSED. A
# refusal the panel cannot classify is reported as "we could not check", never as
# "your signature is invalid", so the drift costs a confusing message and never a
# wrong verdict.
#
# The alternative was reaching past `call()` to the raw error object, and
# chains/base.py::call() is explicitly not to be bypassed: it holds the
# body-before-status ordering and the timeout.
_RPC_CODE_PATTERN = re.compile(r"\(rpc code (-?\d+)\)\s*$")


class MessageVerificationUnavailable(Exception):
    """The daemon could not answer. This is NOT a statement about the signature.

    Raised rather than returning False, because False here would read as "this
    person does not control that address" -- the same collapse of an outage into
    a verdict that chains/base.py::validate_address() used to make about an
    address being malformed, and that CLAUDE.md rule 12 records the cost of.
    """


class WalletUnlockDemanded(MessageVerificationUnavailable):
    """The daemon answered `verifymessage` with RPC_WALLET_UNLOCK_NEEDED (-13).

    A SUBCLASS so that every existing handler of MessageVerificationUnavailable
    keeps working, and the one caller that needs to say something different can
    ask for this specifically.

    IF THIS IS EVER RAISED IN THE FIELD, THE FEATURE'S SAFETY ARGUMENT HAS
    CHANGED for that daemon build, and the change is the operator's call (rule
    16). It is not a bug to route around by unlocking: unlocking the wallet to
    check a customer's signature would put a passphrase on the path of an
    unauthenticated HTTP request, which is the one thing this design exists to
    avoid. The fallback, if a build really does require it, is to verify the
    signature IN PYTHON -- secp256k1 compact-signature recovery against the
    Gridcoin message magic -- and never to ask the daemon at all.
    """


class MessageVerificationRefusedInput(Exception):
    """The daemon understood the request and refused the ADDRESS or the SIGNATURE.

    An ANSWER, not an outage: the pasted strings cannot be checked as they
    stand. Separate from a plain `False` because the two need different words in
    front of a customer -- "that is not a Gridcoin address" and "that signature
    does not match that address" send a person to look at different things.
    """


def rpc_error_code(message: str) -> int | None:
    """The numeric JSON-RPC code chains/base.py put at the end of an error string, or None.

    None means "this error text does not carry a code", which is a real answer
    and the one that fails closed: see _RPC_CODE_PATTERN for the coupling and for
    what a missing code costs.
    """
    match = _RPC_CODE_PATTERN.search(message or "")
    return int(match.group(1)) if match else None


def verify_message(adapter, address: str, signature: str, message: str) -> bool:
    """True if `signature` verifies `message` for `address` on the adapter's daemon.

    THE ARGUMENT ORDER OF THIS FUNCTION IS (address, signature, message) AND IT
    IS THE DAEMON'S, on purpose. A wrapper that reordered them to read more
    nicely would be a second convention for one call, and the first time somebody
    compared this file against a `gridcoinresearchd verifymessage` line on a
    terminal they would have to work out which was which. See the module
    docstring for why the order is not a guess, for the two independent
    measurements that agree on it, and -- most importantly -- for why it
    deliberately does NOT match `signmessage`'s order, which the customer uses at
    the other end of this exchange.

    Args:
        adapter: anything with chains/base.py's `.call(method, *params)`. Taken
            as an argument rather than built here so this is callable with a stub
            that records what it was asked (rule 10) -- which is what
            tests/test_grc_address_proof.py does, including asserting the three
            parameters arrive in this order.
        address: the Gridcoin address whose ownership is being claimed.
        signature: the base64 compact signature the CUSTOMER's wallet produced,
            on the customer's own machine. Public by construction: it reveals
            nothing but the fact that the key exists. It is still never logged --
            see services/grc_login_service.py for why a replayable public value
            does not belong in a log.
        message: the challenge string that was signed.

    Returns:
        True  -- verified.
        False -- the daemon checked and it does not verify. A real negative.

    Raises:
        MessageVerificationRefusedInput: the daemon refused the address or the
            signature as malformed. An answer about the paste.
        WalletUnlockDemanded: the daemon asked for a wallet unlock, which per the
            module docstring it should never do for this method. Nothing in this
            repository unlocks anything in response.
        MessageVerificationUnavailable: the daemon could not be reached, or
            answered in a way this function cannot interpret. NOT a statement
            about the signature.
    """
    try:
        result = adapter.call(VERIFY_MESSAGE_METHOD, address, signature, message)
    except RPCError as error:
        raise _classify(str(error)) from error
    except requests.RequestException as error:
        # Checked, and NARROW: this is the transport. A connection refused, a DNS
        # failure or a timeout is unambiguously "the daemon could not be asked",
        # and the exception type says so rather than the handler guessing from a
        # message. requests.HTTPError is a subclass, which covers the
        # raise_for_status() fallback chains/base.py::call() uses for a response
        # that is not a JSON-RPC error at all (a 401 returns no JSON).
        raise MessageVerificationUnavailable(
            f"the {getattr(adapter, 'asset', 'GRC')} daemon could not be reached to check the signature "
            f"({type(error).__name__}: {error}). This is NOT a statement about the signature -- nothing "
            f"has been proven and nothing has been refused."
        ) from error
    except ValueError as error:
        # Checked: chains/base.py::call() ends with `response.json().get("result")`,
        # so a 2xx body that is not JSON surfaces here as a ValueError. That is a
        # daemon speaking something other than JSON-RPC, which is an outage in
        # every sense that matters and emphatically not a verdict.
        raise MessageVerificationUnavailable(
            f"the {getattr(adapter, 'asset', 'GRC')} daemon returned a body that is not JSON-RPC "
            f"({type(error).__name__}: {error}). This is NOT a statement about the signature."
        ) from error
    if isinstance(result, bool):
        return result
    # A non-bool is not a verdict either. Gridcoin's own help declares
    # RPCResult::Type::BOOL, so anything else means this is not the method we
    # think we are talking to -- a proxy, a different daemon on that port, or a
    # build that changed the contract. `bool(result)` would turn a dict, a string
    # or None into an answer, and a truthy dict would read as PROVEN.
    raise MessageVerificationUnavailable(
        f"the {getattr(adapter, 'asset', 'GRC')} daemon answered {VERIFY_MESSAGE_METHOD} with "
        f"{type(result).__name__} rather than a boolean, so there is no verdict to read. Gridcoin's own "
        f"help declares this method returns a bool; something other than the expected daemon is on that "
        f"port. NOTHING has been proven."
    )


def _classify(error_text: str) -> Exception:
    """Turn chains/base.py's flattened RPC error string into the right exception.

    Returns the exception rather than raising it, so the caller's `raise ... from
    error` keeps the original as the cause -- a classifier that raised would bury
    the daemon's own words one frame deeper.
    """
    code = rpc_error_code(error_text)
    if code == RPC_WALLET_UNLOCK_NEEDED:
        logger.error(
            "%s returned RPC code %d (wallet unlock needed). THIS SHOULD BE IMPOSSIBLE: Gridcoin's "
            "verifymessage() never calls EnsureWalletIsUnlocked() at commit 36bc6a2d, which is the whole "
            "basis for this desk never holding a wallet passphrase. The daemon's words: %s  <- if this is "
            "real, the address-proof feature's safety argument does not hold for this build and the "
            "operator has to decide what replaces it. NOTHING here will unlock a wallet in response.",
            VERIFY_MESSAGE_METHOD,
            RPC_WALLET_UNLOCK_NEEDED,
            error_text,
        )
        return WalletUnlockDemanded(
            f"the Gridcoin daemon asked for a wallet unlock in order to check a signature "
            f"(rpc code {RPC_WALLET_UNLOCK_NEEDED}): {error_text}. This desk does not unlock wallets to "
            f"verify a message and does not hold a passphrase to do it with, so the check cannot be "
            f"completed. NOTHING has been proven and nothing has been refused."
        )
    if code in INPUT_REFUSAL_CODES:
        return MessageVerificationRefusedInput(
            f"the Gridcoin daemon refused the address or the signature as malformed (rpc code {code}): "
            f"{error_text}. This IS an answer about what was pasted, and it is not a statement that the "
            f"signature is wrong -- the daemon never got as far as checking it."
        )
    return MessageVerificationUnavailable(
        f"the Gridcoin daemon returned an error this check cannot interpret as a verdict: {error_text}. "
        f"Treated as 'could not check' rather than as 'did not verify', because reading an unrecognized "
        f"error as a refusal would report a daemon problem as a customer's failure."
    )
