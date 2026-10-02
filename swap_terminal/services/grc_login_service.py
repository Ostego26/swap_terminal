"""Issue an address-ownership challenge, and decide whether a pasted signature proves it.

Role: submodule -> function (verify_address_proof() is the decision; the
      single-use and expiry GUARANTEES are in db.py's SCHEMA, not here)
Reads: swap_terminal.db (address_proof_challenges, swaps), and -- only through
      chains/grc_message_signing.verify_message() -- one read-only RPC method on
      our own Gridcoin daemon.
Writes: swap_terminal.db (address_proof_challenges: one INSERT per challenge
      issued, and one conditional UPDATE per challenge proven. Never a DELETE,
      and never an UPDATE of a row that already proved something -- the database
      refuses both.)
Can move funds: no. It issues no send, imports nothing that can sign, holds no
      key and holds no passphrase. A proven address does NOT change what gets
      paid, where, how much, or whether a swap may proceed -- see "THIS IS NOT A
      GATE" below, which is the most important paragraph in this file.
Mainnet-safe: yes. Every RPC it can cause is `verifymessage`, which is pure
      signature math (the establishment is in chains/grc_message_signing.py's
      docstring), so it changes nothing on any daemon it is pointed at.

=============================================================================
WHAT THE OPERATOR ASKED FOR, AND WHAT WAS BUILT
=============================================================================

"GRC login ability", specified on being asked what it should let someone do:
PROVE OWNERSHIP OF A GRIDCOIN ADDRESS BY SIGNING A CHALLENGE MESSAGE.

  1. This page issues a CHALLENGE string tied to one swap.
  2. The customer signs it with THEIR OWN wallet, on THEIR OWN machine, outside
     this repository: `signmessage <address> "<challenge>"`, or the GUI's
     Sign Message dialog.
  3. They paste back their ADDRESS and the resulting SIGNATURE.
  4. This desk calls `verifymessage <address> <signature> <challenge>` on our own
     daemon. Read-only, no wallet, no key, no unlock.
  5. On success the address is recorded as PROVEN for that swap.

NO PRIVATE KEY, SEED, WIF OR PASSPHRASE EVER REACHES THIS REPOSITORY. That is
the entire reason this design was chosen over the alternatives -- over "paste
your key and we will sign for you", over "give us your passphrase", over
"import your wallet". It is not a pleasant side effect to be traded away later.
Concretely, in this file:

  nothing asks for a passphrase, and no page this module feeds has a passphrase
  field. The operator's standing instruction is that it never will.
  nothing calls `walletpassphrase`, or any other wallet-mutating RPC. The one
  method reachable from here is `verifymessage`.
  a pasted SECRET IS REFUSED BEFORE IT IS USED, STORED OR LOGGED. See
  "THE PASTED-PRIVATE-KEY PATH" below. That is the single most important
  behavior in this change, and tests/test_grc_address_proof.py proves the value
  reaches neither the database nor any log record.

=============================================================================
THIS IS NOT A GATE. NOTHING ON THE MONEY PATH READS IT (rule 16)
=============================================================================

Recording an address as PROVEN does not authorize anything. As of this commit
no payout, no pricing, no sizing and no status transition reads
address_proof_challenges -- grepped across the tree, and the table is new in
this commit so there is nothing that could have.

That is deliberate and it is a boundary rather than an unfinished edge. Wiring a
proof into payout authorization would change WHERE MONEY GOES: it would mean a
payout address is accepted or refused on the strength of this check, and a
mistake in it -- a replayed challenge, a mis-parsed daemon answer, an address
normalized two ways -- would send a customer's coins somewhere they cannot be
recalled. CLAUDE.md rule 16 reserves exactly that class of change for the
operator: "anything changing what gets traded, at what price, in what size, or
whether a family may trade at all ... the operator makes the call once it is
measured."

So this commit builds the PROOF and the RECORD, and stops. If the operator wants
it to decide something, the gate belongs in SQL as a view over this table joined
to swaps (rules 5 and 20), not as a Python check at a send site -- and it is
their decision to make, not one to arrive by accident because a later reader
assumed a proof must be for something.

=============================================================================
THE REPLAY AND EXPIRY REASONING
=============================================================================

A message signature is valid FOREVER. It is in the customer's shell history, in
the support ticket they pasted it into, in a screenshot. There is no revocation.
So the only thing that can make a captured signature worthless is the message
it signed never being accepted again, and that is why:

  THE CHALLENGE IS SINGLE-USE. Consumption and proof are ONE conditional UPDATE
  (`... WHERE challenge = ? AND swap_id = ? AND proven_at IS NULL AND
  julianday(expires_at) > julianday(?)`), so there is no read that can go stale
  before the write. db.py's `address_proofs_are_single_use` trigger then makes
  it a thing the database refuses rather than a thing this file remembers: a
  future writer who drops the `AND proven_at IS NULL` gets an ABORT, not a
  silent replay. The mechanism and the guarantee are separate on purpose.
  THE CHALLENGE IS UNGUESSABLE. `secrets.token_urlsafe(24)` -- 192 bits of
  CSPRNG -- and never `random`. A guessable challenge would let someone ask a
  victim to sign a string of the attacker's choosing somewhere else entirely and
  bring the result here.
  THE CHALLENGE IS BOUND TO ONE SWAP, in a column the UPDATE matches on. A
  signature captured for swap A updates zero rows when presented against swap B.
  THE CHALLENGE EXPIRES, in SQL. Single-use bounds how many times a captured
  signature can be used; expiry bounds how long a captured CHALLENGE is worth
  capturing -- and it limits the window in which a customer who was socially
  engineered into signing something has a live target to sign it against.

CHALLENGE_TTL_SECONDS is 900s and the reasoning for the number is at the
constant. A reload inside the window REUSES the outstanding challenge rather
than issuing a second one, and why that is not a weakening is written at
issue_challenge().

=============================================================================
THE PASTED-PRIVATE-KEY PATH
=============================================================================

The predictable way this design fails is a customer pasting their private key
into the signature box, because the box says "paste what your wallet printed"
and `dumpprivkey` also prints a thing. When that happens:

  THE PASTE IS REFUSED FIRST, before the database is read, before the daemon is
  called, before anything is logged. secret_key_shapes.secret_key_shape() is the
  first statement in verify_address_proof() for that reason.
  NOTHING IS STORED. No row is written, no column is set, and the value is not
  truncated or hashed into one. A hash of a WIF is a hash of 32 bytes with known
  structure; storing one is storing the key with an extra step.
  NOTHING IS LOGGED THAT CONTAINS IT. The WARNING this emits names the SHAPE --
  a fixed string chosen in secret_key_shapes.py, never derived from the input --
  and the swap id, and nothing else. It is deliberately not even passed as a
  logging argument, because `logger.warning("x=%s", value)` leaves the value in
  `record.args` whether or not any handler formats it, which is the leak
  tests/test_secrets_are_not_logged.py was written for.
  THE CUSTOMER IS TOLD, in plain words: that what they pasted looks like a
  secret key, that we did not store it, and that they should treat it as
  compromised and move their funds. The last part is not hedging. A secret that
  has been through a clipboard, a browser's form history and an HTTP request has
  left their control whatever this end does with it.

AND THE SIGNATURE IS NEVER LOGGED EITHER, which is a separate rule from the one
above. A signature is not secret -- it reveals nothing but that the key exists --
but it is REPLAYABLE against the message it signed, and a log is the artifact
that gets pasted into a chat window. There is no column for it either (db.py's
schema comment says why).
"""

from __future__ import annotations

import logging
import secrets
import sqlite3
from datetime import timedelta
from typing import NamedTuple

from chains.grc_message_signing import (
    MessageVerificationRefusedInput,
    MessageVerificationUnavailable,
    WalletUnlockDemanded,
    verify_message,
)
from microfortnights import format_duration
from secret_key_shapes import secret_key_shape

from .helpers import parse_iso, utc_now_iso

logger = logging.getLogger(__name__)

# The one chain this is wired for. `verifymessage` is a Bitcoin-family method
# that BTC and LTC answer identically, and db.py's `asset` column exists so the
# second chain needs no migration -- but nothing is claimed about a chain nobody
# has run this against. A caller that wants another one passes it explicitly and
# is responsible for the adapter it hands in.
PROOF_ASSET = "GRC"

# HOW LONG A CHALLENGE LIVES. 900s, reported as 744.0µfn (rule 6) wherever a
# person reads it.
#
# The number is a judgment and is written down as one rather than presented as
# measured (rule 17). It is bounded on both sides by what the customer has to do
# with it: long enough to open a wallet that may still be syncing, find the Sign
# Message dialog or a console, copy a 70-character string across two windows and
# paste two values back -- several minutes of real work for someone who has never
# done it before -- and short enough that a challenge screenshotted into a
# support thread is dead before anyone reads the thread. If the operator finds it
# too tight in practice, this constant is the only place to change it, and it is
# off the money path entirely.
CHALLENGE_TTL_SECONDS = 900.0

# 24 bytes of CSPRNG, which `secrets.token_urlsafe` renders as 32 base64url
# characters. 192 bits: the challenge has to be unguessable, not merely unique,
# because a guessable one could be put in front of a victim somewhere else and
# the result brought here. services/helpers.new_id() makes the same choice with
# the same reasoning for a swap id.
CHALLENGE_NONCE_BYTES = 24

# The human-readable prefix of the challenge. It is part of the string the
# customer signs, and it is there so that a person who finds a signed message in
# their history months later can tell what they signed and for whom. Plain ASCII,
# no quotes and no backslashes, because the whole challenge goes inside double
# quotes on a command line.
CHALLENGE_PREFIX = "swap_terminal address-proof"

# EVERY OUTCOME THIS DECISION CAN RETURN, AS NAMES.
#
# One vocabulary, in one place, read by the route (for an HTTP status), by the
# template (for a CSS class and a `data-outcome` attribute a test can find) and
# by the tests. A string spelled at three of those sites instead would be rule
# 8's bug with a delay on it: the day one of them is renamed, a page renders a
# refusal with a success class and nothing fails.
OUTCOME_PROVEN = "proven"
OUTCOME_SIGNATURE_REFUSED = "signature_refused"
OUTCOME_INPUT_REFUSED = "input_refused"
OUTCOME_DAEMON_UNREACHABLE = "daemon_unreachable"
OUTCOME_DAEMON_NOT_CONFIGURED = "daemon_not_configured"
OUTCOME_DAEMON_WANTS_UNLOCK = "daemon_wants_wallet_unlock"
OUTCOME_CHALLENGE_UNKNOWN = "challenge_unknown"
OUTCOME_CHALLENGE_EXPIRED = "challenge_expired"
OUTCOME_CHALLENGE_ALREADY_USED = "challenge_already_used"
OUTCOME_CHALLENGE_OTHER_SWAP = "challenge_other_swap"
# NAMED "key material" RATHER THAN "secret", and the rename is answering ruff's
# S105 rather than suppressing it (rule 19: a noqa is a claim you checked, not a
# way to quiet a finding). S105 flags a constant whose NAME suggests it holds a
# credential, and the whole point of this outcome is that NO credential is held
# anywhere -- so a name that makes a reader or a linter think one might be is the
# wrong name. tests/conftest.py makes the same move for the same reason.
OUTCOME_KEY_MATERIAL_PASTED = "key_material_pasted"
OUTCOME_NOTHING_PASTED = "nothing_pasted"

# The outcomes that mean "we could not check", as distinct from "we checked and
# the answer is no". The distinction is the one CLAUDE.md rule 12 insists on and
# the one the operator's brief named explicitly: "your signature did not verify"
# and "we could not check right now" are different sentences and different
# outcomes. The route maps this set to 503 and everything else to 200 or 400.
UNAVAILABLE_OUTCOMES = frozenset({
    OUTCOME_DAEMON_UNREACHABLE,
    OUTCOME_DAEMON_NOT_CONFIGURED,
    OUTCOME_DAEMON_WANTS_UNLOCK,
})


class ProofOutcome(NamedTuple):
    """What verify_address_proof() decided, plus the words to put in front of a person.

    `proven` keeps the one-bit answer every caller needs. `outcome` is why, from
    the vocabulary above, and it exists because bare False collapsed a refusal
    and an outage into one value -- the shape chains/base.py::address_ownership()
    was rewritten to stop doing on 2026-10-01, for the same reason, one directory
    over.

    `headline` and `detail` are prose, not data: the page renders both, and rule
    14 asks that an operator or a customer reading the screen never has to carry
    a code back to a source file to find out what happened.

    `address` is the address that was PROVEN, and it is None for every outcome
    except OUTCOME_PROVEN. It is deliberately not an echo of whatever was
    submitted: a refusal that handed back the submitted string would invite a
    template to print it beside the word "address", which on the secret-paste
    path would render the secret.
    """

    proven: bool
    outcome: str
    headline: str
    detail: str
    address: str | None = None


class Submission(NamedTuple):
    """The three strings the form posted, as ONE value.

    They arrive together, are validated together and mean nothing apart: a
    signature without the message it signed proves nothing, and a challenge
    without an address is a question with no answer. Grouping them is what rule
    10 asks for at this level -- the thing that decides takes one submission, not
    a list of loose parameters -- and it is also what keeps
    verify_address_proof() at four positional arguments instead of seven, which
    is the ceiling ruff's PLR0917 names and which was pointing at exactly this.

    NO FOURTH FIELD MAY BE ADDED FOR A PASSPHRASE, A KEY OR A SEED. The operator's
    standing instruction is that this panel never has a passphrase field, and this
    is the type that would have to grow one first. A signature is not a
    credential; a private key is. Do not blur them.
    """

    challenge: str
    address: str
    signature: str

    def stripped(self) -> Submission:
        """The same submission with surrounding whitespace removed from each field.

        A method rather than three `.strip()` calls at the use site, because a
        pasted value that keeps its trailing newline is the single most likely way
        a correct signature gets refused -- every GUI copy button and every
        terminal selection brings one -- and a stripping that happens in two of
        three places is the bug that is hardest to see.

        The CHALLENGE is stripped too, and must be: it is matched character for
        character against the stored string, so one trailing space is an
        unexplainable "that challenge is not one we issued".
        """
        return Submission(self.challenge.strip(), self.address.strip(), self.signature.strip())


# ONE STATEMENT, AND IT IS THE AUTHORITY (rules 5 and 20).
#
# Consumption and proof are the same write. Every condition that can refuse the
# challenge is a predicate in this UPDATE rather than a Python `if` before it:
#
#   challenge = :challenge     the row, by primary key
#   swap_id = :swap_id         the binding. A challenge for another swap matches
#                              nothing, so a signature captured for swap A cannot
#                              prove an address on swap B.
#   proven_at IS NULL          single use. A spent challenge matches nothing.
#   julianday(...) > ...       expiry, parsed rather than string-compared, so the
#                              UTC offset is handled by SQLite instead of by two
#                              strings happening to be formatted alike.
#
# `rowcount == 0` therefore means "refused", with no window in which a reader
# could have seen a different answer. verify_address_proof() DOES also read the
# row first -- but only to choose the sentence it shows, never to decide. If the
# two ever disagree, the UPDATE wins, and the code says so at the site.
#
# RETURNING is not used here (unlike services/xrp_tag_service._ALLOCATE_SQL,
# which needs the value the statement computed) because this statement computes
# nothing: both written values are already in hand.
_CONSUME_SQL = """
UPDATE address_proof_challenges
   SET proven_address = :address,
       proven_at = :now
 WHERE challenge = :challenge
   AND swap_id = :swap_id
   AND proven_at IS NULL
   AND julianday(expires_at) > julianday(:now)
"""

_OUTSTANDING_SQL = """
SELECT challenge, swap_id, asset, issued_at, expires_at
  FROM address_proof_challenges
 WHERE swap_id = :swap_id
   AND asset = :asset
   AND proven_at IS NULL
   AND julianday(expires_at) > julianday(:now)
 ORDER BY expires_at DESC
 LIMIT 1
"""

_PROVEN_SQL = """
SELECT proven_address, proven_at, asset, challenge
  FROM address_proof_challenges
 WHERE swap_id = :swap_id
   AND proven_at IS NOT NULL
 ORDER BY proven_at DESC
"""


def challenge_text(swap_id: str, nonce: str) -> str:
    """The exact string the customer signs.

    A FUNCTION, so the page, the database row and any later diagnostic cannot
    come to disagree about what was signed -- the challenge is matched by exact
    string, so one stray space is a refusal that looks like a wrong key.

    It carries the swap id visibly as well as in the `swap_id` column. That
    redundancy is on purpose and is not an authority: the COLUMN is what the
    consuming UPDATE matches on, and the text is for the person who finds this in
    their shell history and needs to know what they signed. Rule 14's "echo the
    parameters that decide the answer", applied to a string a human handles.
    """
    return f"{CHALLENGE_PREFIX} swap={swap_id} nonce={nonce}"


def issue_challenge(db, swap_id: str, asset: str = PROOF_ASSET, now: str | None = None) -> dict:
    """The challenge this swap should be signing: an outstanding one, or a new one.

    REUSING AN OUTSTANDING CHALLENGE IS NOT A WEAKENING, and the distinction is
    the whole replay argument. Replay is re-use of a CONSUMED challenge -- one a
    signature already exists for. An unconsumed challenge has no signature in the
    world yet, so handing the same one back to the same swap reveals nothing and
    creates nothing to capture.

    It is reused because the alternative is worse in a way the customer feels: a
    page that mints a new challenge on every render invalidates the string the
    customer is part-way through pasting into their wallet, so a reload -- or the
    status page's own poll -- makes them start over, and the failure reads as "my
    signature is wrong". It also bounds the table: one row per swap per 15-minute
    window instead of one per page view.

    The lookup-then-insert is NOT atomic, and that is checked rather than
    overlooked. Two simultaneous renders can both miss and both insert, which
    yields two valid outstanding challenges for one swap. That is harmless: each
    is independently single-use, each is bound to the same swap, and proving one
    does not consume the other -- nothing in this file or the schema requires
    there to be exactly one. Contrast services/xrp_tag_service, where the
    equivalent race would hand two customers the same deposit tag and misattribute
    money, which is why that one is a single INSERT ... SELECT MAX and this one
    does not need to be.

    Returns the row as a dict: challenge, swap_id, asset, issued_at, expires_at.
    """
    moment = now or utc_now_iso()
    outstanding = db.execute(_OUTSTANDING_SQL, {"swap_id": swap_id, "asset": asset, "now": moment}).fetchone()
    if outstanding:
        return dict(outstanding)

    issued = parse_iso(moment)
    expires_at = (issued + timedelta(seconds=CHALLENGE_TTL_SECONDS)).isoformat()
    row = {
        "challenge": challenge_text(swap_id, secrets.token_urlsafe(CHALLENGE_NONCE_BYTES)),
        "swap_id": swap_id,
        "asset": asset,
        "issued_at": moment,
        "expires_at": expires_at,
    }
    db.execute(
        "INSERT INTO address_proof_challenges (challenge, swap_id, asset, issued_at, expires_at) "
        "VALUES (:challenge, :swap_id, :asset, :issued_at, :expires_at)",
        row,
    )
    db.commit()
    # The challenge is NOT logged. It is not a secret -- it is printed on the page
    # -- but a log line pairing a challenge with a swap id is half of the pair an
    # attacker needs, and the other half (the signature) is one careless log line
    # away in some future edit. What is logged is that one was issued, and for how
    # long, which is what rule 14 asks for: an operator watching this can tell a
    # page that issued a challenge from a page that failed to.
    logger.info(
        "address proof: issued a challenge for swap %s on %s, valid for %s  <- the challenge string "
        "itself is deliberately not in this log line",
        swap_id,
        asset,
        format_duration(CHALLENGE_TTL_SECONDS),
    )
    return row


def _blank_submission_refusal(submission: Submission) -> ProofOutcome | None:
    """None if all three fields carry something; the refusal if any is empty.

    Named separately from every other refusal because the fix is different and
    trivial, and because an empty submit is a slip rather than a failed proof --
    the same distinction routes/ui.py::swap_lookup() makes about a blank swap id.
    """
    missing = [name for name, value in submission._asdict().items() if not value]
    if not missing:
        return None
    return ProofOutcome(
        False,
        OUTCOME_NOTHING_PASTED,
        "Nothing to check yet",
        f"These are still empty: {', '.join(missing)}. Nothing was sent anywhere and nothing was recorded. "
        f"Sign the challenge below with your own wallet and paste both your address and the signature it printed.",
    )


def _challenge_refusal(row: dict | None, swap_id: str, moment: str) -> ProofOutcome | None:
    """Why this challenge cannot be used, or None if it can. CHOOSES WORDS, NEVER DECIDES.

    THE AUTHORITY IS _CONSUME_SQL, not this function, and the separation is
    deliberate rather than redundant. Every condition below is ALSO a predicate in
    that one UPDATE, so a challenge this function wrongly waved through still
    updates zero rows and is still refused. What this function exists for is that
    `rowcount == 0` cannot tell a customer whether the challenge was already used,
    expired six minutes ago, or belongs to somebody else's swap -- and those are
    three different things to do next.

    Extracted from verify_address_proof() because it is a DECISION and
    verify_address_proof() is the orchestration around it. That is rule 10's
    shape, and the lint code that pointed at it (C901, "orchestration that has
    swallowed decisions") is the one CLAUDE.md rule 12 says to answer by
    extracting rather than by raising a ceiling. It is now callable with a seeded
    dict and no database at all.
    """
    if row is None:
        return ProofOutcome(
            False,
            OUTCOME_CHALLENGE_UNKNOWN,
            "That challenge is not one we issued",
            "No challenge with that exact text exists. Nothing was recorded. Challenges are matched character for "
            "character, so a copy that lost a character or gained a line break will not be found -- reload this page "
            "and copy the current challenge again.",
        )
    if row["swap_id"] != swap_id:
        return ProofOutcome(
            False,
            OUTCOME_CHALLENGE_OTHER_SWAP,
            "That challenge belongs to a different swap",
            f"The challenge was issued for another swap, so it cannot prove an address for swap {swap_id}. Nothing was "
            f"recorded. Each challenge is bound to one swap on purpose: it is what stops a signature made for one order "
            f"from being used on another.",
        )
    if row["proven_at"] is not None:
        return ProofOutcome(
            False,
            OUTCOME_CHALLENGE_ALREADY_USED,
            "That challenge has already been used",
            f"It proved an address at {row['proven_at']} and a challenge is single-use, so it cannot be accepted again. "
            f"Nothing was recorded now. This is not a mistake you made: a message signature stays valid forever, so a "
            f"challenge that could be used twice would let anyone who ever saw the signature prove that address again. "
            f"Reload this page for a fresh challenge.",
        )
    if parse_iso(row["expires_at"]) <= parse_iso(moment):
        return ProofOutcome(
            False,
            OUTCOME_CHALLENGE_EXPIRED,
            "That challenge has expired",
            f"It was valid until {row['expires_at']} and is no longer accepted. Nothing was recorded. Reload this page "
            f"for a fresh challenge -- they last {format_duration(CHALLENGE_TTL_SECONDS)}, which is short so that a "
            f"challenge pasted into a screenshot or a support message is dead before anyone else can use it.",
        )
    return None


def _signature_refusal(adapter, submission: Submission, asset: str) -> ProofOutcome | None:
    """None when the signature VERIFIES. Otherwise the outcome, with the right reason.

    FOUR DISTINGUISHABLE NEGATIVES, which is the whole reason this is a function
    and not a bool:

      the chain is not configured here   we have nothing to check with
      the daemon could not be asked      no answer either way
      the daemon wants a wallet unlock   the feature's safety argument has moved
      the daemon refused the input       an answer about the paste
      the daemon checked, and it is no   the real negative

    Only the last one says anything about the person. CLAUDE.md rule 12: a broad
    catch "is never legitimate when the caller cannot tell the failure from a real
    answer", and a single `except Exception: return False` here would have made
    all five of those the same sentence -- which is precisely what the brief for
    this feature forbade.

    Extracted so it can be called with a stub adapter and no database, which is
    what every one of the five tests in tests/test_grc_address_proof.py covering
    these branches does.
    """
    if adapter is None:
        # An ANSWERLESS outcome, not a refusal. chains/registry.build_adapters()
        # builds a GRC adapter only when the RPC environment is set, so an
        # operator who has not exported GRC_RPC_* gets None -- and telling the
        # customer their signature failed would be reporting our configuration as
        # their error. The same failure chains/base.py::address_ownership() was
        # rewritten for on 2026-10-01: a transport problem read as a verdict.
        return ProofOutcome(
            False,
            OUTCOME_DAEMON_NOT_CONFIGURED,
            "We could not check right now",
            f"This terminal has no {asset} daemon configured, so there is nothing here to verify a signature with. "
            f"This is a problem at our end, not with what you pasted, and NOTHING has been refused -- your challenge is "
            f"still valid. Try again shortly.",
        )
    try:
        verified = verify_message(adapter, submission.address, submission.signature, submission.challenge)
    except WalletUnlockDemanded as error:
        # Its own branch because it refutes the feature's safety argument for that
        # daemon build, and the operator has to see that rather than a generic
        # outage. chains/grc_message_signing.py has the full reasoning and logs it
        # at ERROR. NOTHING HERE UNLOCKS ANYTHING.
        return ProofOutcome(
            False,
            OUTCOME_DAEMON_WANTS_UNLOCK,
            "We could not check right now",
            f"Our daemon asked for a wallet unlock in order to check a signature, which it should never need to do -- "
            f"verifying a message is pure arithmetic and touches no wallet. This desk does not unlock wallets and holds "
            f"no passphrase, so the check cannot be completed. NOTHING has been refused and your challenge is still "
            f"valid. The operator has been told. ({error})",
        )
    except MessageVerificationRefusedInput as error:
        # An ANSWER about the paste, which is why it is not folded into the
        # unreachable branch below: "that is not a Gridcoin address" and "we
        # cannot reach our daemon" send a person to look at completely different
        # things, and only one of them is worth retrying unchanged.
        #
        # ESTABLISHED FROM SOURCE at commit 36bc6a2d and NOT measured against a
        # running daemon: Gridcoin's verifymessage() throws rather than returning
        # false for an unparseable address ("Invalid address", rpc code -5), an
        # address that is not a key hash ("Address does not refer to key", -3) and
        # an unparseable signature ("Malformed base64 encoding", -3). It returns a
        # plain false only when RecoverCompact fails or the recovered key does not
        # match. So a MALFORMED signature is a third outcome rather than a
        # negative verdict, and this is it.
        return ProofOutcome(
            False,
            OUTCOME_INPUT_REFUSED,
            "That address or signature is not readable",
            f"Our daemon could not read one of the two values as an address and a base64 signature, so it never got as "
            f"far as checking them against each other. Nothing was recorded and your challenge is still valid. Check "
            f"that the address is the one you signed with and that the signature was copied whole. ({error})",
        )
    except MessageVerificationUnavailable as error:
        # LAST of the three, because WalletUnlockDemanded is a SUBCLASS of this
        # one. Ordering an `except` for a base class above its subclass would make
        # the subclass branch unreachable -- the shape CLAUDE.md rule 19 records
        # finding twice this year ("two of five recent-buy sell vetoes were
        # UNREACHABLE"), where editing the dead branch looks exactly like a fix.
        return ProofOutcome(
            False,
            OUTCOME_DAEMON_UNREACHABLE,
            "We could not check right now",
            f"Our {asset} daemon could not be asked, so there is no answer either way. This is a problem at our end, "
            f"not with what you pasted: NOTHING has been refused, nothing was recorded, and your challenge is still "
            f"valid until it expires. Try again shortly. ({error})",
        )
    if verified:
        return None
    # THE REAL NEGATIVE. The daemon did the arithmetic and the signature does not
    # belong to that address for that message. Distinct from every branch above,
    # which are all "we did not get an answer".
    return ProofOutcome(
        False,
        OUTCOME_SIGNATURE_REFUSED,
        "That signature did not verify",
        "Our daemon checked it and the signature does not match that address for this challenge. Nothing was "
        "recorded. The usual causes are signing with a different address than the one pasted, or copying only part "
        "of the signature. Your challenge is still valid, so you can sign it again and retry.",
    )


def verify_address_proof(db, adapter, swap_id: str, submission: Submission, now: str | None = None) -> ProofOutcome:
    """THE DECISION: does this pasted (address, signature) prove this challenge?

    Orchestration plus the one WRITE. The reasons live in the three functions
    above, each callable with seeded inputs and no database (rule 10); what is
    here is the order they run in and the single statement that makes a proof
    real.

    NOTHING IS WRITTEN UNLESS THE PROOF SUCCEEDS. Every refusal returns before the
    UPDATE, and the UPDATE is the only write in this function. A failed
    verification therefore leaves the challenge outstanding -- deliberately: a
    customer who mistyped one character of a signature must be able to try again,
    and burning the challenge on a failure would turn a typo into "start over".
    That costs nothing in replay terms, because an unconsumed challenge is one no
    valid signature has been accepted for.

    THE KEY-MATERIAL CHECK IS FIRST, before the database is read, before the
    daemon is called and before anything is logged. See the module docstring's
    "THE PASTED-PRIVATE-KEY PATH".

    Args:
        db: a connection on db.py's schema.
        adapter: the GRC adapter, or anything with `.call(method, *params)`. May
            be None, which is its own outcome rather than an exception -- an
            unconfigured chain is an operational fact, not a programming error.
        swap_id: the swap the challenge must belong to.
        submission: the three strings the form posted, as one value. They arrive
            together, are validated together and are meaningless apart, which is
            why they are a Submission and not three parameters -- and it keeps
            this signature at four positional arguments instead of seven.
        now: ISO timestamp, injectable so expiry is testable without sleeping.

    Returns:
        ProofOutcome. `proven` is the one-bit answer; `outcome` is which of the
        twelve reasons, from the OUTCOME_* vocabulary above.
    """
    # FIRST, AND BEFORE ANY READ, WRITE OR LOG. Both pasted fields are checked,
    # not just the signature box: a customer who gets the two boxes the wrong way
    # round pastes the key into the address field instead, and the hazard is
    # identical. The CHALLENGE field is not checked, because it is a value this
    # page issued and a customer has no reason to replace it with a key -- and
    # because a false positive there would refuse a legitimate proof.
    for field_name, pasted in (("signature", submission.signature), ("address", submission.address)):
        shape = secret_key_shape(pasted)
        if shape is not None:
            return _refuse_key_material_paste(swap_id, field_name, shape)

    moment = now or utc_now_iso()
    submission = submission.stripped()
    blank = _blank_submission_refusal(submission)
    if blank is not None:
        return blank

    # READ FIRST, TO CHOOSE THE SENTENCE -- NEVER TO DECIDE. See
    # _challenge_refusal()'s docstring and _CONSUME_SQL's comment. If this read
    # and the UPDATE below ever disagree, the UPDATE wins.
    row = db.execute(
        "SELECT challenge, swap_id, asset, issued_at, expires_at, proven_address, proven_at "
        "FROM address_proof_challenges WHERE challenge = ?",
        (submission.challenge,),
    ).fetchone()
    refusal = _challenge_refusal(row, swap_id, moment)
    if refusal is not None:
        return refusal

    refusal = _signature_refusal(adapter, submission, row["asset"])
    if refusal is not None:
        if refusal.outcome == OUTCOME_SIGNATURE_REFUSED:
            logger.info(
                "address proof: REFUSED for swap %s, address %s -- the daemon checked and the signature does not "
                "verify (the signature itself is deliberately not in this log line, because it is replayable)",
                swap_id,
                submission.address,
            )
        return refusal

    # THE AUTHORITY. See _CONSUME_SQL: every refusal condition is a predicate in
    # this one statement, so `rowcount == 0` is a refusal with no stale-read
    # window. Everything above only chose words.
    cursor = db.execute(
        _CONSUME_SQL,
        {"address": submission.address, "now": moment, "challenge": submission.challenge, "swap_id": swap_id},
    )
    db.commit()
    if cursor.rowcount == 0:
        # Reached when something changed between the read above and this write --
        # in practice, a concurrent request that consumed the same challenge
        # first, or a challenge that expired in the gap. The customer is told the
        # truth: it was used.
        logger.warning(
            "address proof: swap %s produced a VALID signature but the challenge was no longer consumable -- another "
            "request consumed it, or it expired between the read and the write. Nothing was recorded for this attempt.",
            swap_id,
        )
        return ProofOutcome(
            False,
            OUTCOME_CHALLENGE_ALREADY_USED,
            "That challenge has already been used",
            "Your signature verified, but the challenge had already been consumed by the time we recorded it -- most "
            "likely by a second submission of the same form. Nothing was recorded for this attempt. Reload this page "
            "for a fresh challenge if you still need to prove an address.",
        )

    logger.info(
        "address proof: PROVEN -- swap %s now has a signature-verified %s address %s (the signature is deliberately "
        "not in this log line, because it is replayable against this challenge)",
        swap_id,
        row["asset"],
        submission.address,
    )
    return ProofOutcome(
        True,
        OUTCOME_PROVEN,
        "Address proven",
        f"Your signature verified against {submission.address}, and that address is now recorded as proven for swap "
        f"{swap_id}. This challenge is now spent and cannot be used again. Nothing about your payout has changed: "
        f"this is a record that you control the address, and it does not by itself redirect any payment.",
        submission.address,
    )


def _refuse_key_material_paste(swap_id: str, field_name: str, shape: str) -> ProofOutcome:
    """Refuse a paste that has the shape of key material. Stores nothing, logs no value.

    SEPARATE FROM verify_address_proof() SO IT CAN BE READ IN ONE SCREEN, because
    what matters about it is what it does NOT do, and that is only checkable by
    reading the whole thing. There is no database handle in this function's
    arguments, so it cannot write a row even by accident. `shape` is one of
    secret_key_shapes.ALL_SHAPES -- a fixed string chosen in that module, never
    built from the input -- which is what makes it safe to put in a log line.

    THE PASTED VALUE IS NOT A PARAMETER OF THIS FUNCTION. That is the point of the
    signature: there is nothing here to leak. The caller passes the field's NAME
    and the shape's NAME and keeps the value to itself, which also means no
    logging call in this function can put the value in `record.args` -- the leak
    path tests/test_secrets_are_not_logged.py exists for, where a value survives
    in the record whether or not any handler ever formats the message.
    """
    logger.warning(
        "address proof: REFUSED a paste on swap %s because the %s field has the shape of key material (%s). "
        "NOTHING was stored and the pasted value is NOT in this log line, at any level, truncated or hashed. "
        "The customer has been told to treat that key as compromised.",
        swap_id,
        field_name,
        shape,
    )
    return ProofOutcome(
        False,
        OUTCOME_KEY_MATERIAL_PASTED,
        "That looks like a SECRET KEY. Treat it as compromised",
        f"What you pasted into the {field_name} box has the shape of {shape}. We refused it, we did not send it "
        f"anywhere, and we did not store or log it. "
        f"BUT YOU SHOULD ASSUME IT IS NOW COMPROMISED and move those funds to a new address with a new key, because a "
        f"secret that has been through a clipboard and a web form has left your control whatever we do at this end. "
        f"This page never needs a private key, a seed phrase or a wallet passphrase, and it never will. What it needs "
        f"is the SIGNATURE your own wallet prints when you run signmessage -- a value that proves you hold the key "
        f"without revealing it.",
    )


def proven_addresses(db, swap_id: str) -> list[dict]:
    """Every address this swap has proven, newest first. An empty list is a real answer.

    Returned as a list rather than one row because a customer may legitimately
    prove more than one address -- they are allowed to ask for a second challenge
    -- and because collapsing it to "the proven address" would silently pick one
    of several and look authoritative while doing it.
    """
    rows = db.execute(_PROVEN_SQL, {"swap_id": swap_id}).fetchall()
    return [dict(row) for row in rows]


def proof_panel(db, swap_id: str, asset: str = PROOF_ASSET, now: str | None = None, result: ProofOutcome | None = None) -> dict:
    """Everything the address-proof partial renders. No decision of its own.

    Rule 14 governs what is in here and it is most of the dict: the panel has to
    say WHAT to sign, WHERE to sign it, WHAT to paste back, how long there is, and
    what the last attempt did -- before, during and after, not only on success.
    `sign_command` is the literal line to run, built by this function rather than
    typed into the template, so the command on screen and the challenge in the
    database cannot drift.

    `proven` IS ALWAYS A LIST AND MAY BE EMPTY, and the template renders `(none)`
    for the empty case rather than nothing. A blank region is ambiguous between
    "this swap has proven nothing" and "the query broke", which is the exact
    defect CLAUDE.md rule 14 records being shipped on the day it was written.

    It ISSUES a challenge, which makes this a writing function called from a GET.
    That is named rather than hidden: routes/grc_login.py's header says so, and
    issue_challenge() reuses an outstanding challenge, so a reload writes nothing.
    """
    moment = now or utc_now_iso()
    challenge = issue_challenge(db, swap_id, asset, moment)
    remaining_seconds = max(0.0, (parse_iso(challenge["expires_at"]) - parse_iso(moment)).total_seconds())
    return {
        "swap_id": swap_id,
        "asset": asset,
        "challenge": challenge["challenge"],
        "expires_at": challenge["expires_at"],
        # Rule 6: µfn in anything a human reads, with the seconds in parentheses
        # so a reader can connect it to CHALLENGE_TTL_SECONDS without doing the
        # multiplication.
        "ttl_display": format_duration(CHALLENGE_TTL_SECONDS),
        "remaining_display": format_duration(remaining_seconds),
        "expired": remaining_seconds <= 0,
        # The exact command, with the address placeholder left as a placeholder
        # because only the customer knows which of their addresses they are
        # proving. Double quotes around the challenge because it contains spaces.
        #
        # `signmessage` IS <address> <message> -- TWO ARGUMENTS, AND THAT IS NOT
        # THE SAME SHAPE AS THE `verifymessage` CALL THIS FEATURE MAKES AT THE
        # OTHER END, which is <address> <signature> <message>. Both orders were
        # measured on the operator's running testnet daemon on 2026-10-02 and both
        # are the daemon's own. DO NOT MAKE THEM CONSISTENT: the second argument
        # means a different thing in each, so "tidying" either to match the other
        # breaks the half the tidier did not run -- and the half that breaks
        # silently is verification, which would then answer no to everybody with
        # no error anywhere. chains/grc_message_signing.py's docstring carries the
        # same warning at the other site, because a reader who finds one of these
        # two lines has to be told the other exists (rule 8).
        "sign_command": f'signmessage YOUR_{asset}_ADDRESS "{challenge["challenge"]}"',
        "proven": [_proven_display(row, moment) for row in proven_addresses(db, swap_id)],
        "result": result,
        "result_is_unavailable": bool(result) and result.outcome in UNAVAILABLE_OUTCOMES,
    }


def _proven_display(row: dict, now: str) -> dict:
    """One proven-address row, plus how long ago it was proven, in µfn (rule 6)."""
    try:
        age_seconds = (parse_iso(now) - parse_iso(row["proven_at"])).total_seconds()
    except ValueError:
        # Checked: parse_iso raises ValueError for text that is not a timestamp.
        # A row with an unreadable proven_at is a real condition and the display
        # says so instead of guessing -- the same three-valued treatment
        # services/swap_view.py gives a freshness reading, where "unreadable" is a
        # state and not an absence.
        return {"address": row["proven_address"], "proven_at": row["proven_at"], "age_display": "(unreadable timestamp)"}
    return {
        "address": row["proven_address"],
        "proven_at": row["proven_at"],
        "age_display": format_duration(age_seconds),
    }


def is_integrity_refusal(error: sqlite3.IntegrityError) -> bool:
    """True when an IntegrityError came from this table's own guards.

    A helper for a caller that wants to report "the database refused this" rather
    than crash -- and a place to name, once, the three guards db.py installs:
    `address_proofs_are_single_use`, `address_proof_challenges_are_not_repointed`
    and the `address_proof_is_whole` CHECK. A reader who finds one of those
    messages in a log should be able to grep to exactly one explanation of it.
    """
    text = str(error)
    return any(
        guard in text
        for guard in ("address_proofs_are_single_use", "address_proof_challenges_are_not_repointed", "address_proof_is_whole")
    )
