"""Every decision the SOL payout path makes, as functions with seeded inputs.

Role: function layer (rule 10 -- the smallest testable pieces, called by
      chains/solana.py's preview_payout() and send_to_address())
Reads: the environment, for ONE variable: SOL_PAYOUT_KEYPAIR_PATH. Then the
      file it names, inside load_payout_keypair() and nowhere else. No socket,
      no database, and no other setting -- every other input is an argument.
Writes: nothing. Not one function here opens a socket or broadcasts anything;
      signed_transfer_wire() returns BYTES and a base64 string for a caller to
      send.
Can move funds: YES, WHEN ARMED. This is the only file in this tree's Python
      that touches a Solana secret key, and the bytes it returns are the bytes
      that move money -- so the field reads YES even though nothing here has a
      transport: the money moves one call later, when chains/solana.py hands
      what this returns to sendTransaction, and an operator scanning headers for
      what can spend has to land on this file.
      THE FIELD DIFFERS FROM chains/xrp_signing.py's, WHICH READS "NO" (rule 8
      asks that a real difference be stated at both sites, and this is the only
      one between the two files' headers). That one is defensible on its own
      terms -- XRP's seed arrives as an argument, so that module never reads a
      secret from anywhere -- but this one DOES: load_payout_keypair() opens the
      file SOL_PAYOUT_KEYPAIR_PATH names. A module that reads a key off disk is
      not in the same class as one handed a value, and the header says which.
Mainnet-safe: YES, AND BY REFUSAL RATHER THAN BY ABSENCE. require_devnet() is
      the function whose whole job is making the rest of this path unreachable
      anywhere but devnet, and it is pure: it decides from the genesis hash the
      caller read off the cluster, so it can be exercised against every hash --
      mainnet-beta, testnet, a local validator's -- without a network.

=============================================================================
WHY THIS IS A SECOND MODULE AND chains/solana.py STILL HOLDS NO KEY
=============================================================================

chains/solana.py's header has said "CANNOT SIGN ... this is not a policy that a
flag turns off, it is an absence" since it was written, and
tests/test_solana_adapter.py::test_the_module_references_no_keypair_anywhere
pins it by tokenizing that file and refusing the NAMES Keypair, secret_key,
from_secret_key, sign, sign_message and partial_sign.

That test still passes, and it was not worked around. The keypair never enters
chains/solana.py: signed_transfer_wire() below is handed a plan and an arming
token and returns a signed transaction, so the secret's entire lifetime is one
function call in THIS file. The adapter broadcasts bytes it cannot have
produced itself.

That is the stronger arrangement rather than a bookkeeping trick. The file a
reader opens to ask "can this thing sign?" is the file with the key in it, and
it is 500 lines of refusals rather than 1,500 lines of chain reading.

=============================================================================
THE ARMING MODEL, AND THE ONE PLACE IT DIFFERS FROM XRP (CLAUDE.md rule 8)
=============================================================================

chains/xrp_signing.py is the model this file follows, and it is followed
deliberately rather than paraphrased: an exact-string arming token, refusal by
the network's OWN identifier rather than by a URL, derive-the-key-and-compare
before signing, and a preview that is the default mode. Where the reasoning is
identical it is NOT restated here at length; the comment names which XRP
function holds it.

WHERE THE SECRET COMES FROM, AND HOW IT DIFFERS FROM XRP'S. Both chains read
the environment at CALL time as of 2026-10-02 -- chains/xrp_payout_seed.py was
added the same day for the same operator instruction -- and the two are not the
same arrangement:

    chains/xrp_payout_seed.py   XRP_PAYOUT_SECRET_SEED holds THE SEED ITSELF.
                                The secret is a value in the process
                                environment.
    this file                   SOL_PAYOUT_KEYPAIR_PATH holds A PATH. The
                                secret is in a file, and the environment holds
                                only its name.

The path is the better half of that trade and it is why this one is a path. An
environment variable holding a secret is readable from /proc/<pid>/environ for
the life of the process, is inherited by every child it spawns, and lands in any
crash report or process dump that records the environment; a filename is none of
those things, and the file itself can be 0600 (describe_keypair_file() below
prints its mode so an operator can see whether it is). What a path CANNOT be is
shorter than the file: a Solana keypair is 64 integers of JSON, which is why
solana-keygen writes a file rather than printing a string.

A COMMAND-LINE ARGUMENT WAS NEVER AN OPTION for either shape: argv is published
to every process on the host through /proc and `ps`. Nor was a literal in the
tree -- tests/test_no_key_material_is_tracked.py exists because that has already
happened here: wgrc.json was a 64-integer array, it was committed, and the
account it controls had already been paid 5,560,821 lamports.

CONFIGURATION ALONE STILL CANNOT ARM A PAYOUT, which is the property the XRP
sentence was protecting. The path being set is not enough and is not even
checked until after the arming token matches: require_send_confirmation()
below demands CONFIRM_SOL_SEND spelled exactly, at the call site, in the same
process. An operator who exports the variable and runs the payout worker gets
a refusal, because services/payout_service.py calls send_to_address() with two
positional arguments.

=============================================================================
WHAT IS NOT HERE
=============================================================================

No SPL token transfer. A token transfer is a different instruction against
different accounts (the Token program's TransferChecked, over two associated
token accounts, with the mint's decimals), and chains/solana_transaction.py
lays out exactly one shape: the native SystemProgram transfer. An adapter
configured with a mint is REFUSED by chains/solana.py rather than quietly paid
in lamports.

No mainnet, no testnet, and no local validator. require_devnet() accepts one
genesis hash.

No broadcast, and no retry of one. Whether a signed transaction reaches a
cluster is chains/solana.py's business, and nothing in this tree has ever done
it -- see that module's header for the measurement (403 at this container's
proxy) and rule 16's line between a fix and a proposal.
"""

from __future__ import annotations

import base64
import json
import os
import stat
from pathlib import Path

import base58
from network_target import GENESIS_HASHES, solana_cluster

from .solana_address import PUBKEY_BYTES, SolanaAddressError, decode_address
from .solana_transaction import (
    SIGNATURE_BYTES,
    compile_transfer_message,
    parse_transfer_transaction,
    wire_transaction,
)

# THE ARMING TOKEN. Sending requires this exact string at the call site.
#
# The reasoning is chains/xrp_signing.CONFIRM_XRP_SEND's and it transfers
# verbatim, so it is summarized rather than re-argued: a boolean can arrive
# from a truthy variable, a parsed config value, a `**kwargs` splat or a
# positional argument that drifted one place left, and every one of those is a
# send nobody wrote. An exact string has no accidental spelling -- the only way
# to pass it is to have typed it.
#
# It also greps. `grep -rn CONFIRM_SOL_SEND` finds every site in the tree that
# can send SOL, which a `True` never would.
CONFIRM_SOL_SEND = "I-HAVE-READ-THE-PREVIEW-AND-AUTHORIZE-THIS-SOL-SEND"

# THE ONE ENVIRONMENT VARIABLE THIS FILE READS.
#
# Deliberately NOT in config.py, and that is a layering decision rather than an
# oversight: Config reads the environment at class-definition time, which is
# import time, so a keypair path in Config would be read into a long-lived
# process object the moment anything imported it -- including the read-only
# deposit watcher and every test. Read here, at call time, inside the one
# function that needs it, after the arming token has already matched.
#
# It is also NOT the Node bridge's SOLANA_PAYER_KEYPAIR_PATH, and the names are
# different on purpose. That variable arms a separate process
# (swap_terminal/grc-sol-swap/abstergo_exchange/server.js, whose sendSolPayout
# signs a SystemProgram.transfer and calls sendAndConfirmTransaction), and one
# name for two payout paths would mean exporting it armed both of them.
KEYPAIR_PATH_VARIABLE = "SOL_PAYOUT_KEYPAIR_PATH"

#: A keypair file solana-keygen writes is a JSON array of 64 integers: 32 bytes
#: of ed25519 seed followed by the 32-byte public key. MEASURED 2026-10-02
#: against @solana/web3.js: Keypair.fromSeed(seed).secretKey is exactly
#: seed || publicKey, both halves confirmed byte for byte.
KEYPAIR_FILE_INTEGERS = 64
SEED_BYTES = 32
BYTE_MAX = 255

#: A keypair file is about 400 bytes. The cap is here so that a path pointing at
#: something large -- a log, a database, a wallet dump -- is refused by SIZE
#: before any of it is parsed or held in memory, rather than read in full and
#: then rejected for its shape.
KEYPAIR_FILE_MAX_BYTES = 4096


class SolanaSigningRefused(RuntimeError):
    """A guard on the SOL payout path refused. Nothing was signed or broadcast.

    RuntimeError for the reason chains/xrp_signing.XRPSigningRefused gives: it
    is what a caller catching "the payout did not happen" already catches, and
    services/payout_service.py's broad handler around the send treats it as a
    failed payout rather than a crash.

    Every subclass below is a DISTINCT guard, and they are distinct classes
    rather than one exception with a message because an operator reading a
    failure needs to tell "this is not devnet" from "you did not arm it" from
    "the key is for another account" from "the balance will not cover it". Those
    call for different next actions, and most of them are not "retry".
    """


class SolanaClusterRefused(SolanaSigningRefused):
    """The cluster's genesis hash is not devnet's, or could not be read."""


class SolanaSendNotArmed(SolanaSigningRefused):
    """send_to_address() was called without the exact arming token, or with no keypair path."""


class SolanaKeypairRefused(SolanaSigningRefused):
    """The keypair file is missing, malformed, internally inconsistent, or for another account."""


class SolanaHeadroomRefused(SolanaSigningRefused):
    """The payer cannot cover the amount plus the fee plus what it must retain."""


class SolanaRentRefused(SolanaSigningRefused):
    """The transfer would create an account below the rent-exempt minimum, which the runtime rejects."""


class SolanaWireMismatch(SolanaSigningRefused):
    """The signed bytes do not parse back as the transfer the plan described."""


class SolanaSplSendRefused(SolanaSigningRefused):
    """The adapter is configured for an SPL mint, and only the native transfer is built."""


def devnet_genesis_hash() -> str:
    """Devnet's genesis hash, DERIVED from network_target.GENESIS_HASHES.

    Not written out again here, which is rule 8 applied to the one string that
    decides whether this path may sign: two copies of a hash table agree until
    somebody edits one of them, and nothing fails when they stop agreeing --
    the wrong half just permits a cluster it should refuse.

    GENESIS_HASHES maps hash -> label, so devnet is found by its label. If that
    table ever carries no devnet entry, or more than one, this RAISES rather
    than picking: "I could not identify devnet" is not "this is devnet", which
    is the same distinction require_devnet() is built on.
    """
    matches = [genesis for genesis, label in GENESIS_HASHES.items() if label.strip().upper().startswith("DEVNET")]
    if len(matches) != 1:
        raise SolanaClusterRefused(
            f"network_target.GENESIS_HASHES carries {len(matches)} entries labeled DEVNET and this path "
            f"needs exactly one. NOTHING was signed. The table is the authority for which hash is which "
            f"cluster; a payout path cannot pick between two of them or invent a missing one."
        )
    return matches[0]


def require_devnet(genesis: str, url: str = "") -> str:
    """Refuse unless the CLUSTER's own genesis hash is devnet's.

    Returns a one-line description for the preview (rule 14: always print which
    cluster). Raises SolanaClusterRefused otherwise.

    THE URL IS NOT CONSULTED and is echoed only so the reader can see where the
    answer came from. chains/xrp_signing.MAINNET_NETWORK_IDS makes the argument
    at length and it is the same one: a hostname resolves to whatever DNS says
    today, so an operator's /etc/hosts, a split-horizon resolver or a typo'd
    copy of a config can point `api.devnet.solana.com` at a mainnet validator
    while every log line still prints the devnet name. Solana has no network-id
    field; what a cluster cannot lie about is the hash of its own genesis block.

    FOUR REFUSALS, NOT ONE:

      mainnet-beta    the obvious case. Real money.
      testnet         refused too. This path is devnet-only because that is
                      where it was asked for and where the operator's funded
                      keypair lives; testnet is a different cluster whose
                      acceptance of these bytes is equally unmeasured.
      unrecognized    a hash GENESIS_HASHES does not know -- which includes
                      every local validator, because solana-test-validator
                      generates its own genesis. Refused, because "I cannot
                      identify this cluster" is not "this is devnet" (rule 2's
                      distinction; network_target.UNRECOGNIZED_CLUSTER makes
                      the same point, and names a private fork of mainnet as
                      the case that reads identically).
      missing         an empty or non-string hash, which is what a cluster that
                      did not answer looks like. Same reasoning one step
                      earlier.

    There is no flag, environment variable or argument that turns this off.
    """
    if not isinstance(genesis, str) or not genesis.strip():
        raise SolanaClusterRefused(
            f"the cluster at {url or '(url not given)'} did not report a genesis hash, so this path "
            f"CANNOT establish that it is devnet. NOTHING was signed and nothing was broadcast. Not "
            f"reading the cluster is not the same as reading a safe one -- the hash is what this guard "
            f"is, and without it there is no guard."
        )
    reported = genesis.strip()
    devnet = devnet_genesis_hash()
    if reported != devnet:
        raise SolanaClusterRefused(
            f"the cluster at {url or '(url not given)'} reports genesis {reported}, which is "
            f"{solana_cluster(reported)} -- NOT devnet ({devnet}). NOTHING was signed and nothing was "
            f"broadcast. This is checked against the hash the CLUSTER reports and not against the "
            f"hostname, because a hostname resolves to whatever DNS says today. There is no flag, "
            f"environment variable or argument that turns this off: a payout on any other cluster is the "
            f"operator's decision and it is not made here (CLAUDE.md rule 16)."
        )
    return f"{reported}  <- {solana_cluster(reported)}, confirmed from the cluster itself, via {url or 'the configured url'}"


def keypair_path_from_environment(environ=None) -> str:
    """The keypair path, from the environment. Refuses rather than defaulting.

    `environ` is a parameter with a None default so a test can seed it, and
    os.environ is read only when nothing was passed -- the pattern
    chains/daemon_conf.py uses for the same reason. There is deliberately no
    default path: a default would name SOMEBODY'S keypair, and the wrong key
    silently is worse than no key loudly (the argument config.py already makes
    about SOL_RPC_URL).

    Read at CALL time, never at import. A module that reads the environment on
    import makes every later import order-dependent, which is the import-time
    side effect rule 12 names -- and for a key path it would also mean the
    variable was consulted by processes that will never pay anything out.
    """
    source = os.environ if environ is None else environ
    return str(source.get(KEYPAIR_PATH_VARIABLE, "") or "").strip()


def require_send_confirmation(token: str, keypair_path: str) -> None:
    """Refuse unless the caller armed this send explicitly AND a keypair path is set.

    Both halves in one function because they are one question -- "did somebody
    mean for money to leave" -- and splitting them would let a call site arm a
    send with no key and get a confusing failure two guards later. Same shape
    as chains/xrp_signing.require_send_confirmation(), whose docstring records
    why the token is compared for EXACT equality: this repository has already
    shipped a substring-matching bug on a status check, and a substring test on
    an arming token is that defect where it costs money instead of an exit code.

    THE DEFAULT IS STILL THE REFUSAL, BUT NOT FOR THE REASON THIS PARAGRAPH
    GAVE UNTIL 2026-10-03. It said: "services/payout_service.py:354 calls
    `send_to_address(swap["payout_address"], amount)` -- two positional
    arguments and nothing else -- so the live payout worker lands here and is
    refused." That was true and is now false in its first clause:
    services/payout_service.broadcast_payout() passes
    confirm_send=CONFIRM_SOL_SEND for SOL, on the operator's 2026-10-03
    instruction, so the live worker now reaches the SECOND branch below rather
    than the first. The default refusal therefore rests on the keypair path
    being unset -- which is every checkout, every test run and every host where
    the operator has not made the custody decision -- rather than on a call site
    that forgot to opt in. Both branches are unchanged; which one the worker
    lands on is what moved, and a forgotten export still cannot degrade into a
    send.
    """
    if token != CONFIRM_SOL_SEND:
        raise SolanaSendNotArmed(
            f"this SOL send was NOT armed, so nothing was signed and nothing was broadcast. "
            f"send_to_address() previews by default and requires confirm_send={CONFIRM_SOL_SEND!r} "
            f"spelled exactly, at the call site, to broadcast anything. "
            f"{'No token was passed.' if not token else 'The token passed did not match.'} A boolean was "
            f"deliberately not used: a truthy variable, a parsed config value or a positional argument "
            f"that drifted could all produce a send nobody wrote."
        )
    if not keypair_path:
        raise SolanaSendNotArmed(
            f"this SOL send was armed but {KEYPAIR_PATH_VARIABLE} is unset or empty, so nothing was "
            f"signed. The adapter holds no key: the signing keypair is read from the file this variable "
            f"names, by this module, at the moment of the send. Export it to the keypair that owns the "
            f"account SOL_HOT_WALLET announces. The file's CONTENTS are never printed, logged or "
            f"returned by anything on this path."
        )


def describe_keypair_file(path: str) -> str:
    """One line about the key file an operator is about to sign with (rule 14).

    Names the PATH and the file MODE, and nothing from inside the file. The path
    is a filename the operator exported themselves, so printing it tells them
    which key is about to be used -- which is the question a payout preview has
    to answer. The mode is printed because solana-keygen writes 0600 and a key
    readable by group or other is a key to treat as published, which is
    docs/key_exposure_runbook.md's subject.

    REPORTS, DOES NOT REFUSE. A mode of 0644 is a real hazard and it is not one
    this function can fix by blocking a payout the operator asked for; the
    remedy is chmod, and the line says so.
    """
    target = Path(path)
    try:
        mode = stat.S_IMODE(target.stat().st_mode)
    except OSError as error:
        return f"{path}  <- COULD NOT BE READ: {error}"
    exposed = bool(mode & (stat.S_IRGRP | stat.S_IROTH | stat.S_IWGRP | stat.S_IWOTH))
    verdict = (
        "GROUP OR OTHER CAN READ OR WRITE THIS KEY -- treat it as published and chmod 600 it"
        if exposed
        else "owner only, which is what solana-keygen writes"
    )
    return f"{path}  mode {mode:04o}  <- {verdict}"


class PayoutKeypair:
    """An ed25519 keypair in memory, whose secret half cannot be printed.

    __slots__ RATHER THAN A DATACLASS, AND THAT IS THE WHOLE DESIGN. A
    dataclass generates a __repr__ that prints every field, so the obvious
    spelling of this class would put 32 bytes of secret into any log line, any
    exception that formats its arguments, and any debugger session. __slots__
    also means there is no __dict__, so `vars(keypair)` raises instead of
    returning the seed -- the generic "dump the object" path that a logging
    helper or a JSON encoder would otherwise find.

    The secret is still in memory, because signing needs it. What is closed off
    is every route by which it reaches a string.
    """

    __slots__ = ("_seed", "public_key")

    def __init__(self, seed: bytes, public_key: str) -> None:
        if len(seed) != SEED_BYTES:
            raise SolanaKeypairRefused(
                f"an ed25519 seed is {SEED_BYTES} bytes, got {len(seed)}. Nothing was signed. The seed's "
                f"VALUE is not printed here or anywhere else on this path."
            )
        self._seed = bytes(seed)
        self.public_key = public_key

    def __repr__(self) -> str:
        return f"PayoutKeypair(public_key={self.public_key}, secret=<{SEED_BYTES} bytes, NEVER PRINTED>)"

    __str__ = __repr__

    def seed_for_signing(self) -> bytes:
        """The seed, for sign_message() alone.

        A method rather than a public attribute so that every reader of the
        secret is one grep (`grep -rn seed_for_signing`) rather than every
        attribute access in the tree. It is called in exactly one place:
        sign_message() below.
        """
        return self._seed


def load_payout_keypair(path: str) -> PayoutKeypair:
    """Read the keypair solana-keygen wrote, and refuse anything else.

    THE FILE'S CONTENTS NEVER APPEAR IN A MESSAGE. Every refusal below names
    the path, the SIZE, or the SHAPE -- never a value, never a prefix, never a
    hash. A hash of a 32-byte seed with known structure is the seed with an
    extra step, which is the argument
    tests/test_grc_address_proof.py::test_a_pasted_private_key_is_in_neither_
    the_database_nor_the_log already makes about a pasted WIF.

    IT CHECKS THE FILE AGAINST ITSELF, which is the check worth having here.
    A solana-keygen keypair is seed || public key, so the public half can be
    re-derived from the secret half and compared. A file whose two halves
    disagree is corrupt, truncated, or hand-assembled -- and the failure mode
    of using it is signing with one key while announcing another, which is
    exactly what derive_and_check() exists to stop one step later. Catching it
    here names the FILE as the problem instead of the configuration.
    """
    target = Path(path)
    if not target.exists():
        raise SolanaKeypairRefused(
            f"{KEYPAIR_PATH_VARIABLE}={path!r} does not exist, so nothing was signed. The path is named "
            f"because the operator set it; no file was read."
        )
    size = target.stat().st_size
    if size > KEYPAIR_FILE_MAX_BYTES:
        raise SolanaKeypairRefused(
            f"{KEYPAIR_PATH_VARIABLE}={path!r} is {size} bytes and a solana-keygen keypair is about 400. "
            f"REFUSED BY SIZE, before reading or parsing any of it: a path pointing at a log, a database "
            f"or a wallet dump must not be slurped into this process. Nothing was signed."
        )
    try:
        parsed = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        # NAMED exceptions and the file's text is NOT in the message -- a
        # JSONDecodeError's own str() names a position and a token kind, never
        # the document, which is why it is safe to format. The three types are
        # the three ways this read fails and none of them is a blind catch
        # (rule 12, BLE001).
        raise SolanaKeypairRefused(
            f"{KEYPAIR_PATH_VARIABLE}={path!r} is not the JSON array solana-keygen writes "
            f"({type(error).__name__}: {error}). Nothing was signed, and no part of the file is in this "
            f"message."
        ) from error
    if not isinstance(parsed, list) or len(parsed) != KEYPAIR_FILE_INTEGERS:
        raise SolanaKeypairRefused(
            f"{KEYPAIR_PATH_VARIABLE}={path!r} parsed as "
            f"{type(parsed).__name__} of length {len(parsed) if isinstance(parsed, list) else 'n/a'}, and "
            f"a solana-keygen keypair is a JSON array of exactly {KEYPAIR_FILE_INTEGERS} integers -- 32 "
            f"bytes of seed then the 32-byte public key. Nothing was signed. Only the LENGTH is reported."
        )
    if not all(isinstance(number, int) and not isinstance(number, bool) and 0 <= number <= BYTE_MAX for number in parsed):
        raise SolanaKeypairRefused(
            f"{KEYPAIR_PATH_VARIABLE}={path!r} is a {KEYPAIR_FILE_INTEGERS}-element array but not every "
            f"element is a byte (0..{BYTE_MAX}). Nothing was signed, and no element is reported."
        )
    seed = bytes(parsed[:SEED_BYTES])
    declared = bytes(parsed[SEED_BYTES:])
    derived = _public_key_bytes(seed)
    if derived != declared:
        raise SolanaKeypairRefused(
            f"{KEYPAIR_PATH_VARIABLE}={path!r} is internally inconsistent: the public key its last "
            f"{PUBKEY_BYTES} bytes declare is {base58.b58encode(declared).decode('ascii')}, and the key "
            f"derived from its first {SEED_BYTES} bytes is "
            f"{base58.b58encode(derived).decode('ascii')}. Nothing was signed. Both of those are PUBLIC "
            f"keys and are in this message on purpose; the secret half is not. A file whose halves "
            f"disagree is truncated, corrupt, or assembled by hand -- not a keypair solana-keygen wrote."
        )
    return PayoutKeypair(seed, base58.b58encode(derived).decode("ascii"))


def _public_key_bytes(seed: bytes) -> bytes:
    """The 32-byte ed25519 public key for a seed. The only derivation in this file.

    PyNaCl is imported HERE rather than at module scope, and PLC0415 is
    suppressed with the reason (rule 19: a noqa is a claim you checked).
    PyNaCl is an OPTIONAL dependency -- it is not in
    swap_terminal/requirements.txt, the same judgment chains/xrp.py records for
    xrpl-py -- and chains/registry.py imports chains/solana.py
    unconditionally, which imports this module. A module-level import would
    therefore make a signing library mandatory in order to start a READ-ONLY
    deposit watcher, on a host that has no business holding one.

    MEASURED 2026-10-02 on this host: PyNaCl 1.6.2 is importable, and the
    public key it derives from a seed agrees with @solana/web3.js's
    Keypair.fromSeed() -- GmaDrppBC7P5ARKV8g3djiwP89vz1jLK23V2GBjuAEGB from a
    seed of 32 0x07 bytes, pinned in tests/test_solana_transaction.py.
    """
    try:
        import nacl.signing  # noqa: PLC0415 -- checked: optional dependency; see above
    except ImportError as error:
        raise SolanaKeypairRefused(
            "PyNaCl is not importable, so nothing was signed and nothing was broadcast. It is an optional "
            "dependency on purpose -- every guard here, the whole preview path and the entire test suite "
            "work without it -- so a missing signing library is a refusal rather than a crash at import. "
            "`pip install pynacl` if this host is meant to pay SOL out."
        ) from error
    return bytes(nacl.signing.SigningKey(bytes(seed)).verify_key)


def derive_and_check(keypair: PayoutKeypair, announced: str) -> str:
    """REFUSE if the keypair is not for the account this run announced.

    THE THREAT MODEL, which is the whole reason local signing is safe to add.
    On Solana WE choose the account the transaction claims: the fee payer is
    just the first account key, and the runtime checks only that the signature
    matches THAT key. So a keypair paired with the wrong announced address
    produces a perfectly valid transfer debiting an account nobody displayed --
    the operator reads "from <SOL_HOT_WALLET>" in a preview and a different
    account is emptied. Nothing in the runtime's rules stops that. The only
    thing that stops it is deriving the key and refusing on a mismatch.

    This is chains/xrp_signing.derive_and_check()'s guard for a different key
    system, and the two are NOT merged (rule 8 asks that the difference be
    stated at the site): XRP derives a base58check classic address from a
    secp256k1-or-ed25519 seed through xrpl-py, Solana base58-encodes a raw
    ed25519 public key. Sharing them would mean an abstraction over two key
    systems to save four lines, and the shared version would have to import
    both signing libraries.

    Returns a line for the preview. Both addresses in the message are PUBLIC.
    """
    if not announced:
        raise SolanaKeypairRefused(
            "this payout announced no payer account, so there is nothing to check the signing key "
            "against, and NOTHING was signed. SOL_HOT_WALLET is the account a payout debits and it must "
            "be set to the PUBLIC key of the keypair that will sign. An unchecked key signs for whatever "
            "account the transaction happens to name."
        )
    if keypair.public_key != announced:
        raise SolanaKeypairRefused(
            f"the keypair at {KEYPAIR_PATH_VARIABLE} derives {keypair.public_key}, not the {announced} "
            f"this run announced. REFUSING to sign: on Solana the fee payer is simply the first account "
            f"key, so this keypair would have signed a transfer debiting an account the preview never "
            f"displayed. Nothing was broadcast. Both addresses here are public keys; the secret is not in "
            f"this message."
        )
    return f"{keypair.public_key}  <- derived from the keypair file and MATCHES the announced payer"


def require_lamport_headroom(
    balance_lamports: int,
    send_lamports: int,
    fee_lamports: int,
    retain_lamports: int,
) -> str:
    """Refuse a transfer the payer cannot cover. Returns the arithmetic for the preview.

    ALL FOUR ARGUMENTS ARE INTEGER LAMPORTS and the arithmetic is integer
    throughout, so there is no float on the money path at any point.
    chains/solana_units.py owns the conversion and explains why: a check done
    in SOL floats could refuse a transfer that fits, or permit one that does
    not, by one lamport in either direction, and which way it went would depend
    on where the binary representation happened to land.

    WHY REFUSE HERE WHEN THE RUNTIME WOULD ALSO REFUSE. The runtime's refusal
    arrives as an InsufficientFundsForRent or an "Attempt to debit an account
    but found no record of a prior credit" AFTER the transaction reached a
    node, and for a transaction that reached the ledger it has already paid its
    fee. Refusing before signing costs nothing, names the shortfall in
    lamports, and leaves nothing behind. This is chains/xrp_signing.
    require_reserve_headroom()'s argument, one chain over; the quantities
    differ (a rent-exempt minimum is per-account-size, an XRPL reserve is a
    network parameter plus owned objects) and so the functions are separate.
    """
    remaining = int(balance_lamports) - int(send_lamports) - int(fee_lamports)
    summary = (
        f"balance {balance_lamports} - send {send_lamports} - fee {fee_lamports} = {remaining} lamports "
        f"remaining, against {retain_lamports} the payer must retain"
    )
    if remaining < int(retain_lamports):
        raise SolanaHeadroomRefused(
            f"this transfer would leave the payer below what it must retain, so NOTHING was signed and "
            f"nothing was broadcast. {summary} -- short by {int(retain_lamports) - remaining} lamports. "
            f"The retained figure is the payer's own rent-exempt minimum, asked of the cluster with "
            f"getMinimumBalanceForRentExemption, plus any reserve the caller passed; an account that "
            f"falls below its rent-exempt minimum is collected by the runtime and its lamports are gone, "
            f"which is the structural difference from dust that chains/solana_units.py records."
        )
    return f"{summary} -- fits, with {remaining - int(retain_lamports)} lamports of headroom"


def below_new_account_floor(send_lamports: int, rent_minimum: int) -> bool:
    """Is this payout too small to CREATE a Solana account? The one comparison.

    THE RULE HAS TWO CALLERS AND THEY ASK IT AT DIFFERENT MOMENTS, which is why
    the comparison is a function rather than an `if` written twice (rule 8: two
    copies of one rule is a bug with a delay on it, and a floor that drifted
    between the quote and the payout would quote a swap the payout then refuses):

      require_destination_rent(), below      PAYOUT time. The destination is
                                             known, so "does this account
                                             exist" is answerable and the floor
                                             only applies when it does not.
      services/quote_service.create_quote()  QUOTE time. The payout address does
                                             NOT exist yet -- create_quote()
                                             takes no address and create_swap()
                                             is what takes one -- so the only
                                             honest test is the conservative
                                             one: a payout below the floor
                                             cannot be delivered to a NEW
                                             account whatever address arrives
                                             later.

    Both read the minimum from the CLUSTER rather than from a constant. See
    chains/solana_units.py's rent section for why: this repository's reference
    figures were stale on every cluster for weeks (890,880 against a live
    650,240) and nothing failed while they were.
    """
    return int(send_lamports) < int(rent_minimum)


def require_destination_rent(destination_exists: bool, send_lamports: int, rent_minimum: int) -> str:
    """Refuse a transfer that would create an account below its rent-exempt minimum.

    A native transfer to an address with no account CREATES that account. The
    runtime rejects a transaction that leaves a created account holding a
    non-zero balance below the rent-exempt minimum for its size -- so a payout
    of a few thousand lamports to a brand-new wallet does not arrive small, it
    does not arrive at all, and the customer's address shows nothing while the
    terminal has recorded a broadcast.

    WHICH HALF IS MEASURED (rule 17). `rent_minimum` is MEASURED: it is the
    cluster's own answer to getMinimumBalanceForRentExemption, which the
    operator's 2026-09-30 devnet run returned 650240 from for a 0-byte account.
    That the runtime REJECTS rather than accepts the below-minimum creation is
    SOURCED from Solana's account model and has NOT been observed from this
    container, because nothing here can broadcast. The refusal is the
    recoverable direction either way: the operator sends more, or sends to a
    funded address.

    An existing destination is unaffected -- its balance only goes up -- and
    the returned line says which case it was, because a reader needs to know
    the check ran (rule 14: never let an empty result print nothing).

    THE SAME FLOOR IS CHECKED ONE STAGE EARLIER, at quote time, in
    services/quote_service.create_quote() -- and the two are not redundant.
    That one runs before a customer is given a number, knows no destination, and
    therefore refuses any payout below the floor regardless of which address
    arrives. This one knows the address and lets a small payout through to an
    account that already exists, which that one cannot establish. A reader who
    finds either must know the other is there, so each names the other.
    """
    if destination_exists:
        return (
            "the destination account already exists on this cluster, so no rent-exempt minimum applies "
            "to the transfer"
        )
    if below_new_account_floor(send_lamports, rent_minimum):
        raise SolanaRentRefused(
            f"the destination account does NOT exist on this cluster, so this transfer would create it -- "
            f"and {send_lamports} lamports is below the {rent_minimum}-lamport rent-exempt minimum the "
            f"cluster reported for a 0-byte account. The runtime rejects a transaction that leaves a new "
            f"account below its minimum, so this would not arrive small: it would not arrive. NOTHING was "
            f"signed. Send at least {rent_minimum} lamports, or pay an address that already exists."
        )
    return (
        f"the destination account does not exist yet, so this transfer creates it; {send_lamports} "
        f"lamports is at or above the cluster's {rent_minimum}-lamport rent-exempt minimum for a 0-byte "
        f"account, so the runtime will accept the creation"
    )


def sign_message(message: bytes, keypair: PayoutKeypair) -> bytes:
    """Sign the message bytes, and VERIFY the signature before returning it.

    The verification is not ceremony. A signature computed over the wrong bytes,
    or with a key that is not the one announced, is indistinguishable from a
    correct one until a cluster rejects it -- and a rejected transaction has
    reached a node. Verifying here, against the PUBLIC key that
    derive_and_check() already matched to the announced payer, makes the
    assertion the outcome rather than the absence of an exception (rule 13).

    Raises SolanaSigningRefused on a verification failure, with no secret in the
    message. Returns 64 bytes.
    """
    try:
        import nacl.exceptions  # noqa: PLC0415 -- checked: optional dependency; see _public_key_bytes
        import nacl.signing  # noqa: PLC0415 -- checked: optional dependency; see _public_key_bytes
    except ImportError as error:
        raise SolanaKeypairRefused(
            "PyNaCl is not importable, so nothing was signed. See _public_key_bytes() for why it is an "
            "optional dependency and `pip install pynacl` if this host is meant to pay SOL out."
        ) from error
    signature = nacl.signing.SigningKey(keypair.seed_for_signing()).sign(bytes(message)).signature
    if len(signature) != SIGNATURE_BYTES:
        raise SolanaSigningRefused(
            f"the signing library returned {len(signature)} bytes and an ed25519 signature is "
            f"{SIGNATURE_BYTES}. Nothing was broadcast."
        )
    try:
        nacl.signing.VerifyKey(decode_address(keypair.public_key)).verify(bytes(message), signature)
    except (nacl.exceptions.BadSignatureError, SolanaAddressError) as error:
        raise SolanaSigningRefused(
            f"the signature this process just produced does NOT verify against {keypair.public_key}, the "
            f"public key the keypair file declares and the payer the preview announced "
            f"({type(error).__name__}). NOTHING was broadcast. A signature that does not verify locally "
            f"would be rejected by the cluster after reaching it."
        ) from error
    return signature


def signed_transfer_wire(plan: dict, recent_blockhash: str, confirm_send: str, environ=None) -> dict:
    """The one function in this tree's Python that signs a Solana transaction.

    Everything above is a refusal or a line of text. This is where the secret
    is read, used, and left behind: the keypair is a local, it is passed to
    sign_message() once, and it goes out of scope when this returns. Nothing it
    returns contains the secret -- the caller gets the wire bytes, the same
    bytes base64-encoded for sendTransaction, the signature as base58, the
    public payer and two lines of description.

    THE ORDER IS THE DESIGN, and it is cheapest-and-most-fatal first:

      1  the arming token, and that a keypair path is set at all. Nothing is
         read from disk before this passes, so an unarmed call does not even
         open the key file.
      2  the keypair file: present, small, parseable, 64 bytes, and consistent
         with itself.
      3  the DERIVED key against the payer the plan announced. A mismatch here
         is the account-substitution attack in derive_and_check()'s docstring.
      4  compile and sign.
      5  PARSE THE SIGNED BYTES BACK and compare them against the plan.

    Step 5 is the one that would look redundant and is not. The verification
    principle this repository works to -- never accept "the code contains a
    check for X" as evidence X holds -- applied to the artifact that is about
    to be broadcast: a serializer defect that put the right lamports against
    the wrong account key would pass every other guard here, because every
    other guard inspects the INPUTS. This one inspects the bytes.

    THE PLAN'S PAYER IS WHAT GETS SIGNED, not the derived key, and that is
    deliberate even though step 3 has just proven they are equal. The
    transaction must be the one the operator read in the preview; building it
    from the key would mean the preview and the transaction have two different
    sources and only a test would ever notice them diverge.

    The cluster is NOT re-checked here. chains/solana.py's preview_payout()
    refuses off devnet before this is reachable, and re-asking would mean a
    second getGenesisHash whose answer could differ from the one the operator
    read -- see this module's header for where the cluster decision lives.
    """
    keypair_path = keypair_path_from_environment(environ)
    require_send_confirmation(confirm_send, keypair_path)
    keypair = load_payout_keypair(keypair_path)
    derivation = derive_and_check(keypair, str(plan.get("payer") or ""))
    message = compile_transfer_message(
        str(plan["payer"]), str(plan["destination"]), int(plan["base_units"]), recent_blockhash
    )
    signature = sign_message(message, keypair)
    wire = wire_transaction(signature, message)
    parsed = parse_transfer_transaction(wire)
    expected = {
        "payer": str(plan["payer"]),
        "destination": str(plan["destination"]),
        "lamports": int(plan["base_units"]),
        "recent_blockhash": str(recent_blockhash).strip(),
    }
    differences = [
        f"{field}: the signed transaction says {parsed[field]!r}, the plan said {value!r}"
        for field, value in expected.items()
        if parsed[field] != value
    ]
    if differences:
        raise SolanaWireMismatch(
            "the transaction that was just signed does NOT match the plan the preview described, so "
            "NOTHING was broadcast:\n  " + "\n  ".join(differences) + "\n"
            "  This is read back out of the signed bytes rather than out of the arguments, which is the "
            "only way a serialization defect becomes visible before an explorer shows it."
        )
    return {
        "wire": wire,
        "base64": base64.b64encode(wire).decode("ascii"),
        "signature": parsed["signature"],
        "payer": keypair.public_key,
        "derivation": derivation,
        "keypair_file": describe_keypair_file(keypair_path),
    }
