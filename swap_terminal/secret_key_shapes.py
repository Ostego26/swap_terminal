"""Recognize a pasted SECRET -- a private key, a WIF, a seed phrase -- by its shape alone.

Role: function level (one decision: "does this string look like key material?")
Reads: nothing. Every input is an argument -- no file, no environment, no
      network, no database.
Writes: nothing, and that is the load-bearing property of this module rather
      than an incidental one. Nothing here stores, returns, hashes, truncates
      or logs the value it was given. The only thing that leaves is the NAME OF
      A SHAPE, which is a category chosen from a fixed list in this file and is
      not derived from the characters of the input.
Can move funds: no
Mainnet-safe: yes -- it opens nothing and names no network.

=============================================================================
WHY THIS EXISTS
=============================================================================

The address-proof panel (services/grc_login_service.py) asks a customer for two
strings: their Gridcoin ADDRESS and the base64 SIGNATURE that their own wallet
produced for a challenge. Neither is a secret. The whole reason that design was
chosen over every alternative is that no private key, seed, WIF or passphrase
ever has to reach this host.

A customer who misreads the instructions will paste their private key into the
signature box. That is not a hypothesis about users being careless; it is the
one predictable way this design fails, because the box is labeled "paste the
thing your wallet printed" and `dumpprivkey` also prints a thing. When it
happens, three things must be true at once:

  the paste is REFUSED, so it is never sent anywhere -- not to our daemon as a
  `verifymessage` parameter, where it would land in the daemon's own debug.log
  on a host the customer does not control;
  NOTHING IS STORED. No row, no column, no truncation, no hash. A hash of a
  WIF is a hash of 32 bytes of entropy with a known structure, and storing one
  is storing the key with an extra step;
  NOTHING IS LOGGED. Same reasoning, and a log is worse than a database because
  it is the artifact that gets pasted into a chat window.

And the customer has to be TOLD, in words, that what they pasted looks like a
secret key, that we did not keep it, and that they should treat it as
compromised and move their funds -- because a secret that has been in a
browser's form history, a clipboard and possibly a proxy's buffer is a secret
that has left their control whatever we do at this end.

=============================================================================
WHY SHAPE AND NOT VALIDITY
=============================================================================

This module deliberately does NOT verify a checksum, decode base58, derive a
public key, or confirm that the pasted thing is a working key. Two reasons, and
the second is the one that decides it:

  A VALID-ONLY CHECK WOULD LET THE TYPO THROUGH. A WIF with one character
  mistyped is still a secret -- it is one edit away from the real key, it
  reveals 31 of the 32 bytes, and it is exactly what a customer produces when
  they paste from a screenshot. Refusing only well-formed keys would refuse the
  careful paste and accept the sloppy one.
  DECODING MEANS HOLDING IT. modules/htlc_spend.py::decode_wif() exists in this
  repository and does base58-check decoding properly -- and it RETURNS THE 32
  KEY BYTES, which is correct for the thing it is for (signing an HTLC spend
  with a key the operator configured) and is precisely what must not happen
  here. Importing it would put a function whose return value is a private key
  into an HTTP request handler. The two are named at each other rather than
  merged (rule 8's second case: where two implementations genuinely differ, the
  difference is the point and belongs in a comment at both sites). The
  difference is the return value: that one hands back a key, this one hands
  back the word "WIF".

tests/test_no_key_material_is_tracked.py::looks_like_an_ed25519_keypair() is the
third implementation of "what shape is key material", and it is a GATE OVER THE
TREE'S OWN FILES rather than over customer input -- it reads tracked files and
parses JSON from disk. The 64-integer array rule below is the same rule; it is
spelled here too because this module may not import from tests/, and both
copies name the other.

=============================================================================
HOW THE SHAPES ARE KEPT DISJOINT FROM A LEGITIMATE PASTE
=============================================================================

A false positive is a customer who cannot prove an address they own, so the
rules below are checked against what a REAL submission looks like.

A Gridcoin signature is the base64 of a 65-byte compact signature -- measured
from Gridcoin's own source at commit 36bc6a2d, where
src/wallet/rpcwallet.cpp::signmessage() returns `EncodeBase64` of `SignCompact`
output and `CPubKey::RecoverCompact` consumes 65 bytes. base64 of 65 bytes is 88
characters and 65 = 3*21 + 2, so a PADDED signature always carries exactly one
'=', and '=' is in neither base58 alphabet. An UNPADDED one is 87 characters and
can look base58 about one time in five thousand, which is why the 64-byte base58
rule below decodes and measures rather than trusting length and alphabet --
see ED25519_BASE58_LENGTH_MINIMUM for the arithmetic and for what the false
alarm would have cost the customer who tripped it.

A Gridcoin address is 34 characters. A WIF is 51 or 52. The windows do not
touch, so an address pasted into the signature box is caught as a MISMATCH by
the daemon rather than as a secret by this module -- which is the right division:
this module answers "is this a secret", not "is this the right field".
"""

from __future__ import annotations

import json
import re

# Already a declared runtime dependency (requirements.txt) and already imported
# by chains/solana_address.py, so this adds nothing to a deployment. It is used
# for exactly one thing below -- measuring the DECODED LENGTH of an 87/88
# character base58 string -- and the decoded bytes are never returned, stored or
# logged. See _looks_like_ed25519_base58() for why a length-and-alphabet test
# alone was not good enough.
import base58

# The two base58 alphabets this repository already knows about, as one string
# each, so the membership test below is a set operation rather than a regex. The
# Bitcoin alphabet is what a WIF and a Gridcoin address are written in; the
# Ripple one is here because a customer who has several wallets open pastes from
# whichever window is in front, and a 64-byte base58 blob in either alphabet is
# key material either way.
BITCOIN_BASE58 = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"
RIPPLE_BASE58 = "rpshnaf39wBUDNEGHJKLM4PQRST7VWXYZ2bcdeCg65jkm8oFqi1tuvAxyz"
_BASE58_CHARACTERS = frozenset(BITCOIN_BASE58) | frozenset(RIPPLE_BASE58)

# A WIF is base58-check of 1 version byte + 32 key bytes, optionally plus a
# 0x01 compression flag -- 33 or 34 payload bytes plus a 4-byte checksum, which
# encodes to 51 or 52 characters. The window is widened by one on each side
# rather than pinned to {51, 52}: a paste that lost or gained a character is
# still a secret, and this module's whole argument is that the typo must be
# refused as loudly as the clean copy.
#
# The three payload constants are NOT imported from modules/htlc_spend.py even
# though that file defines them (WIF_PAYLOAD_LEN, WIF_PAYLOAD_LEN_COMPRESSED,
# WIF_COMPRESSED_SUFFIX). That module imports `ecdsa` and
# modules/atomic_htlc_scripts, so importing it here would pull a signing library
# into an HTTP request path for the sake of two integers -- and this module needs
# the ENCODED length, which that one does not state. See the module docstring for
# the division between the two.
WIF_LENGTH_MINIMUM = 50
WIF_LENGTH_MAXIMUM = 53

# base58 of 64 bytes -- an ed25519 keypair as Phantom, Solflare and
# `solana-keygen` export it -- is 87 or 88 characters.
#
# A LENGTH-AND-ALPHABET TEST IS NOT ENOUGH HERE, and this is the one rule in
# this module that needed arithmetic before it could be trusted. A Gridcoin
# signature is base64 of 65 bytes, which is 88 characters WITH its single '='
# of padding -- and '=' is not in either base58 alphabet, so a padded signature
# can never look base58. But a wallet GUI that emits the signature UNPADDED
# gives 87 characters, and base58's alphabet is a subset of base64's: the only
# base64 symbols base58 lacks are 0, O, I, l, + and /, six of sixty-four. The
# chance that an unpadded 87-character signature happens to avoid all six is
# (58/64)^87, which is about 1 in 5,000.
#
# One in five thousand legitimate signers being told "what you pasted looks like
# a secret key, treat it as compromised and move your funds" is not an
# acceptable false positive -- it is a false alarm that costs the customer a
# wallet migration. So the decoded LENGTH is checked: 64 bytes is a keypair, 65
# is a signature, and the two cannot be confused.
ED25519_BASE58_LENGTH_MINIMUM = 87
ED25519_BASE58_LENGTH_MAXIMUM = 88
ED25519_KEYPAIR_BYTES = 64

# A bare 32-byte private key written as hex, which is what most Ethereum-lineage
# tools and several key converters print.
HEX_PRIVATE_KEY_LENGTH = 64

# A JSON array of 64 byte-valued integers: 32 secret bytes then 32 public, which
# is the file `solana-keygen` writes. Same rule as
# tests/test_no_key_material_is_tracked.py::looks_like_an_ed25519_keypair(); see
# the docstring for why it is spelled in both places.
ED25519_KEYPAIR_INTEGERS = 64
BYTE_MAXIMUM = 255
# A 64-integer array is around 400 characters. Anything larger cannot be one, so
# it is rejected on length before a JSON parser ever sees it -- a cap rather than
# a parse, because the input is a stranger's paste.
MAXIMUM_KEYPAIR_JSON_CHARACTERS = 2048

# BIP39 and the Electrum-style word lists. A mnemonic is 12, 15, 18, 21 or 24
# words; every BIP39 word is 3 to 8 lowercase letters with no digit and no
# punctuation. Gridcoin's own `dumpseedphrase` / `restoreseedphrase` pair (read
# in its RPC table at src/rpc/server.cpp) makes a seed phrase something a GRC
# holder actually has, so this is not a borrowed hazard from another chain.
MNEMONIC_WORD_COUNTS = frozenset({12, 15, 18, 21, 24})
_MNEMONIC_WORD = re.compile(r"^[a-z]{3,8}$")

# Extended private keys. Every one of these prefixes means "this string contains
# a key AND a chain code", so it is strictly worse to leak than a single WIF: it
# is every address the wallet will ever derive. The public counterparts (xpub,
# tpub, ...) are deliberately absent -- an xpub is not a secret, and refusing one
# would be refusing something harmless with a message that says "move your
# funds", which is its own kind of damage.
EXTENDED_PRIVATE_KEY_PREFIXES = ("xprv", "tprv", "yprv", "zprv", "uprv", "vprv", "Uprv", "Vprv", "Yprv", "Zprv")

# The shape names this module may return. Fixed strings, chosen here, never built
# from the input -- which is what makes it safe for a caller to put one in a log
# line (services/grc_login_service.py does exactly that, and its test asserts the
# pasted value is absent from every log record).
SHAPE_WIF = "WIF private key"
SHAPE_EXTENDED_PRIVATE_KEY = "extended private key (xprv/tprv)"
SHAPE_HEX_PRIVATE_KEY = "32-byte private key in hex"
SHAPE_ED25519_KEYPAIR_JSON = "ed25519 keypair array (solana-keygen format)"
SHAPE_ED25519_BASE58 = "64-byte base58 secret key (wallet export format)"
SHAPE_SEED_PHRASE = "seed phrase / mnemonic"



def _is_pure_base58(value: str) -> bool:
    """Every character is in one of the two base58 alphabets. No checksum, by design."""
    return bool(value) and all(character in _BASE58_CHARACTERS for character in value)


def _looks_like_ed25519_keypair_json(value: str) -> bool:
    """A bare JSON array of 64 byte-valued integers, and nothing else.

    Size-capped before parsing: a keypair is around 400 characters, so anything
    larger cannot be one and does not need to be handed to a JSON parser. The
    narrow catch is a real answer and not a swallowed failure (rule 12) -- a
    string this cannot parse is not key material OF THIS SHAPE, which is exactly
    what the return value claims.
    """
    stripped = value.lstrip()
    if len(value) > MAXIMUM_KEYPAIR_JSON_CHARACTERS or not stripped.startswith("["):
        return False
    try:
        parsed = json.loads(value)
    except (json.JSONDecodeError, ValueError):
        return False
    return (
        isinstance(parsed, list)
        and len(parsed) == ED25519_KEYPAIR_INTEGERS
        and all(isinstance(number, int) and not isinstance(number, bool) and 0 <= number <= BYTE_MAXIMUM for number in parsed)
    )


def _looks_like_ed25519_base58(value: str) -> bool:
    """An 87-88 character base58 string that decodes to exactly 64 bytes.

    The decoded bytes are measured and discarded. They are not returned, not
    stored, not hashed and not logged -- which is the division this module's
    docstring draws against modules/htlc_spend.py::decode_wif(), whose job is to
    hand the key back.

    The length check is what separates a wallet's 64-byte secret-key export from
    an UNPADDED 65-byte Gridcoin signature, which a pure-alphabet test cannot
    tell apart about one time in five thousand. See ED25519_BASE58_LENGTH_MINIMUM
    for the arithmetic.
    """
    if not (ED25519_BASE58_LENGTH_MINIMUM <= len(value) <= ED25519_BASE58_LENGTH_MAXIMUM):
        return False
    if not _is_pure_base58(value):
        return False
    try:
        decoded = base58.b58decode(value)
    except ValueError:
        # Checked, and narrow on purpose: b58decode raises ValueError for a
        # character outside ITS alphabet. _is_pure_base58() above accepts the
        # Ripple alphabet too, so a Ripple-alphabet string legitimately reaches
        # here and legitimately fails -- that is a real answer ("not a
        # Bitcoin-alphabet 64-byte blob"), not a swallowed failure.
        return False
    return len(decoded) == ED25519_KEYPAIR_BYTES


def _looks_like_seed_phrase(value: str) -> bool:
    """12, 15, 18, 21 or 24 lowercase words of 3 to 8 letters each.

    The word COUNT is checked first and is the thing that keeps this from firing
    on prose. A challenge string, an error message or a sentence a customer typed
    into the wrong box will contain digits, capitals, punctuation or a word
    outside 3-8 letters, and any one of those is enough to fall through.
    """
    words = value.split()
    if len(words) not in MNEMONIC_WORD_COUNTS:
        return False
    return all(_MNEMONIC_WORD.match(word) for word in words)


def _looks_like_extended_private_key(value: str) -> bool:
    """One of the xprv-family prefixes: a key AND a chain code, so every derived address."""
    return value.startswith(EXTENDED_PRIVATE_KEY_PREFIXES)


def _looks_like_hex_private_key(value: str) -> bool:
    """64 hex characters, with or without an 0x prefix.

    The prefix is stripped before the test because that is how most tools print
    it, and "0x" plus 64 hex digits is the same secret as 64 hex digits.
    """
    candidate = value[2:] if value[:2].lower() == "0x" else value
    return len(candidate) == HEX_PRIVATE_KEY_LENGTH and bool(re.fullmatch(r"[0-9a-fA-F]+", candidate))


def _looks_like_wif(value: str) -> bool:
    """50-53 characters, all base58. See WIF_LENGTH_MINIMUM for why the window is wide."""
    return WIF_LENGTH_MINIMUM <= len(value) <= WIF_LENGTH_MAXIMUM and _is_pure_base58(value)


# THE SHAPES, IN ORDER, AS ONE TABLE.
#
# Ordered by SPECIFICITY rather than by likelihood: the prefix test and the JSON
# test cannot overlap with anything else, the two length-plus-alphabet tests are
# disjoint from each other by length, and the mnemonic test is last because it is
# the only one whose input contains spaces.
#
# A table rather than a chain of `if ... return`, for the reason rule 8 gives: the
# list of shapes this module recognizes is now spelled ONCE. ALL_SHAPES is derived
# from it rather than hand-maintained beside it, so a seventh shape cannot be
# added to the dispatcher and left out of the set a caller validates against --
# which is the hand-written-copy-of-a-vocabulary failure CLAUDE.md rule 11
# describes, at a smaller scale.
SHAPE_TESTS = (
    (_looks_like_extended_private_key, SHAPE_EXTENDED_PRIVATE_KEY),
    (_looks_like_ed25519_keypair_json, SHAPE_ED25519_KEYPAIR_JSON),
    (_looks_like_hex_private_key, SHAPE_HEX_PRIVATE_KEY),
    (_looks_like_wif, SHAPE_WIF),
    (_looks_like_ed25519_base58, SHAPE_ED25519_BASE58),
    (_looks_like_seed_phrase, SHAPE_SEED_PHRASE),
)

ALL_SHAPES = frozenset(shape for _, shape in SHAPE_TESTS)


def secret_key_shape(value: str) -> str | None:
    """The NAME of the secret-key shape `value` has, or None if it has none.

    Returns one of the SHAPE_* constants above -- a fixed string from this
    module -- or None. IT NEVER RETURNS, EMBEDS OR LOGS ANY PART OF `value`, and
    the caller is relied on to keep that true: see
    services/grc_login_service.py, which puts the returned shape name in a
    WARNING and the pasted string nowhere at all.

    None does NOT mean "this is safe". It means "this is not one of the six
    shapes below", which is a narrower claim (rule 3: a count needs its
    denominator, and the denominator here is six shapes, not all secrets). The
    caller must still treat the value as untrusted input; what None licenses is
    sending it to `verifymessage` as a signature, which is a public value by
    construction.

    Order of checks is by SPECIFICITY, not by likelihood: the prefix test and
    the JSON test cannot overlap with anything else, the two length-plus-alphabet
    tests are disjoint from each other, and the mnemonic test is last because it
    is the only one whose input contains spaces.
    """
    candidate = (value or "").strip()
    if not candidate:
        return None
    for recognizes, shape in SHAPE_TESTS:
        if recognizes(candidate):
            return shape
    return None
