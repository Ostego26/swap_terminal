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
divergence from XRP, whose can_spend has been derived from the seed since
2026-10-02 -- from its PRESENCE then, and from the seed actually DECODING since
2026-10-03, after a nine-character placeholder on the operator's host offered an
XRP payout that could never have been signed (chains/xrp_payout_seed.py carries
the measurement). The divergence is over: SOL is now derived too, from this module,
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

THAT POSTURE SWITCH HAS BEEN THROWN, AND THIS PARAGRAPH DENIED IT FOR A WEEK.
It read: "ONE POSTURE SWITCH IS STILL THE OPERATOR'S AND THIS FILE DOES NOT
THROW IT. Config.ALLOWED_PAIRS names no pair that pays out in SOL (counted
2026-10-03: zero entries with SOL in the second position), so arming this
changes what the payout path WOULD do and changes nothing about what it IS
asked to do."

Re-counted 2026-10-11 by importing the real Config: 30 allowed pairs over six
assets, TEN with SOL in them, and FIVE with SOL in the SECOND position --
BTC->SOL, GRC->SOL, ICP->SOL, LTC->SOL, XRP->SOL. The operator enabled the
first three on 2026-10-03 and XRP->SOL on 2026-10-04, in both cases hours after
the sentence above was written. So arming this now changes what the payout path
IS ASKED to do, and the clause that said otherwise was the load-bearing one.

THAT IS WHY payout_capability() EXISTS BELOW. While no pair paid out in SOL, a
capability that over-claimed cost nothing -- nothing asked. With five pairs
enabled, `can_spend` is what decides whether a customer is offered one and
whether a deposit is taken, and on 2026-10-11 it was True in two configurations
whose payout could not even be previewed. The measurements are at that function
and at chains/solana.py's __init__.

AND A TRANSACTION FROM THIS PATH HAS REACHED A CLUSTER, which this header also
denied. It read: "AND NO TRANSACTION FROM THIS PATH HAS EVER REACHED A CLUSTER.
Re-measured 2026-10-03: api.devnet.solana.com answers 403 at this container's
proxy ... and the BROADCAST is verified against nothing." The 403 is still true
of this container and still means no test here can broadcast. The claim about
the WORLD is false: payouts.id=14 and id=15, GRC->SOL, 761654 lamports each,
broadcast 2026-10-03 from the operator's host and read back off devnet through
getTransaction the same day. Arming is a capability rather than a rehearsal, and
the rehearsal has also happened -- twice, on devnet. MAINNET has not, and
chains/solana_signing.require_devnet() refuses it by genesis hash.
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
from env_routes import how_a_value_arrives

from .solana_address import is_on_curve, is_valid_address
from .solana_signing import KEYPAIR_PATH_VARIABLE, keypair_path_from_environment

__all__ = [
    "KEYPAIR_PATH_VARIABLE",
    "missing_keypair_refusal",
    "payout_capability",
    "payout_keypair_is_present",
    "spl_mint_payout_refusal",
    "unset_hot_wallet_refusal",
    "unsignable_hot_wallet_refusal",
]


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

    AND XRP NO LONGER DRAWS THAT LINE HERE, WHICH IS A REAL DIVERGENCE AND IS
    STATED AT BOTH SITES (rule 8). Since 2026-10-03
    chains/xrp_payout_seed.payout_capability() is what sets XRPAdapter.can_spend
    and it DECODES the seed, after a nine-character placeholder on the
    operator's host made can_spend True and offered an XRP payout that could
    never have been signed -- with the deposit irreversible by the time it would
    have been found. SOL CAN REACH THE SAME STATE: SOL_PAYOUT_KEYPAIR_PATH
    naming a file that is missing, not JSON, or the wrong length arms can_spend
    here and is refused only at
    chains/solana_signing.load_payout_keypair(), which is after a deposit is
    taken. What makes the two cases different in COST rather than in kind is
    where the check lives: XRP's is base58check on a string already in this
    process's environment, with no filesystem and no network, so it is free in a
    constructor; SOL's would be an open() and a parse of a KEY FILE on every
    adapter construction, which is a page render. That is a judgment about
    exposure and about live posture -- narrowing it changes which pairs this
    terminal offers -- so it is the operator's call (rule 16) and is NOT made
    here. It is a named proposal, not an oversight, and this paragraph is where
    the next reader finds it.

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
        f"take a deposit it can never settle. "
        + how_a_value_arrives(KEYPAIR_PATH_VARIABLE, armed_chain="SOL")
        + " It names the keypair file that owns the account SOL_HOT_WALLET announces; set "
          "SOL_HOT_WALLET to that account too. Both are custody decisions and neither has a "
          "default. The file's CONTENTS are never read, printed or logged by anything that renders "
          "this sentence."
    )


def unset_hot_wallet_refusal() -> str:
    """ARMED WITH NO PAYER. The sentence for a keypair path and no SOL_HOT_WALLET.

    =========================================================================
    MEASURED 2026-10-11: can_spend WAS True IN THIS STATE AND THE PAYOUT
    COULD NOT EVEN BE PREVIEWED
    =========================================================================

    This is the state the operator's host is one `--arm sol` away from, which is
    why it is a refusal rather than a note. `docker-compose.web.armed-sol.yml`
    sets SOL_PAYOUT_KEYPAIR_PATH inside the container and sets NOTHING ELSE --
    read 2026-10-11, the overlay's whole non-comment body is one `volumes` entry
    and one `environment` entry -- so arming SOL without also setting
    SOL_HOT_WALLET in .env produces exactly this configuration.

    Measured that day by constructing the real adapter with the keypair variable
    exported and `hot_wallet=""`, against seeded cluster responses:

        can_spend                 True
        why_cannot_pay_out("SOL") ""        <- "it CAN pay out"
        preview_payout(...)       SolanaRPCError: "SOL_HOT_WALLET is unset, so
                                  this payout has no payer"

    So every `* -> SOL` pair -- five of them in Config.ALLOWED_PAIRS, counted the
    same day -- badged AVAILABLE, services/swap_service.create_swap()'s
    `cannot_pay` gate passed, a customer's deposit was taken and credited, and the
    payout then refused before it read one byte from the cluster. On this chain
    the deposit is irreversible by the time anybody finds out.

    THE TREE ALREADY KNEW. tests/test_solana_payout.py::
    test_the_default_is_no_keypair_path_and_therefore_no_payout_capability says it
    in its own docstring -- "a host with the keypair and no SOL_HOT_WALLET is
    armed and still cannot pay" -- and the remedy chosen on 2026-10-03 was to make
    missing_keypair_refusal() NAME both variables. That is a reporting fix for a
    capability defect: the sentence read correctly and `can_spend` still said yes.
    Rule 19's test is whether a change stops the symptom being reported or stops
    the cause existing, and naming the variable in a sentence nobody sees until
    the payout fails is the first one.

    AND IT IS THE XRP INCIDENT AGAIN, one chain over. chains/xrp_payout_seed.
    payout_capability() exists because XRP_PAYOUT_SECRET_SEED held a
    nine-character placeholder, can_spend was True, and this terminal offered a
    payout it could never have signed. The shape is identical: a credential that
    is PRESENT but cannot produce a payout. What differs is which half is missing
    -- there the seed did not decode, here there is no account for the key to
    derive TO -- and chains/solana_signing.derive_and_check() refuses an
    unannounced payer for a reason that is about theft rather than tidiness, so
    "no announced payer" can never become a send.

    IT NAMES BOTH VARIABLES ANYWAY, because an operator who hits this has already
    set one of them and needs to know the pair is one decision. The keypair file's
    CONTENTS are not read by anything that renders this sentence, and this
    function does not open it either.
    """
    return (
        f"cannot pay out: {KEYPAIR_PATH_VARIABLE} is set, so this process has a signing keypair, but "
        f"SOL_HOT_WALLET is NOT set -- so there is no account for this payout to debit and no payer for "
        f"the keypair to be checked against. Every SOL payout would refuse before reading the cluster, "
        f"and chains/solana_signing.derive_and_check() would refuse to sign for an account no preview "
        f"announced. Set SOL_HOT_WALLET to the PUBLIC key of the account that keypair owns -- "
        f"`solana-keygen pubkey <the file>` prints it, and that command is the operator's to run, not "
        f"this process's. The two variables are one custody decision and neither has a default."
    )


def unsignable_hot_wallet_refusal(hot_wallet: str) -> str:
    """SOL_HOT_WALLET names an account NO private key can exist for. Off the curve.

    A 32-byte base58 key that is OFF the ed25519 curve is a Program Derived
    Address -- an Associated Token Account or similar -- and no keypair derives to
    it, ever. chains/solana.py::validate_address() already refuses one as a payout
    DESTINATION and services/xrp_tag_service._validate_sol_account() refuses one
    as a deposit account; this is the same fact asked about the account the desk
    pays FROM, where the consequence is that chains/solana_signing.
    derive_and_check() can never match it and every payout refuses after a deposit
    has been credited.

    WHY THIS IS CHEAP ENOUGH TO ASK IN A CAPABILITY CHECK, which is the question
    this module's header answers "no" to for the key file. is_on_curve() is base58
    plus field arithmetic over 32 bytes it already holds -- no file, no socket, no
    key material -- so it costs the same order as the `bool()` beside it. Opening
    the KEY FILE would be a different thing and is still refused: see
    payout_keypair_is_present().

    THE ADDRESS IS IN THE MESSAGE and that is deliberate -- it is a public key the
    operator typed into .env themselves, and the sentence is useless without it
    (rule 14: echo the parameter that decides the answer).
    """
    return (
        f"cannot pay out: SOL_HOT_WALLET={hot_wallet} is a syntactically valid 32-byte Solana key that "
        f"is OFF THE ED25519 CURVE, which means it is a Program Derived Address -- an Associated Token "
        f"Account or similar -- and NO PRIVATE KEY EXISTS FOR IT. No keypair file can derive to it, so "
        f"chains/solana_signing.derive_and_check() would refuse every payout from it and nothing could "
        f"ever sign a transfer out of it. Set SOL_HOT_WALLET to the wallet account itself. "
        f"chains/solana.py::validate_address() refuses the same thing for a customer's payout address "
        f"and its docstring has the full reasoning."
    )


def spl_mint_payout_refusal(mint: str) -> str:
    """A mint is configured and the payout path builds only the NATIVE transfer.

    MEASURED 2026-10-11, the same run that found the unset-hot-wallet state: with
    SOL_PAYOUT_KEYPAIR_PATH exported, SOL_HOT_WALLET set and SOL_SPL_MINT set,
    `can_spend` was True, chains/registry.why_cannot_pay_out() returned "", and
    preview_payout() raised SolanaSplSendRefused -- "this adapter is configured
    for the SPL mint ..., and the payout path builds only the NATIVE SOL
    transfer". Same shape as the hot-wallet case and the same cost: a pair badged
    AVAILABLE, a deposit taken, and a payout that refuses on its first guard with
    no network call.

    SO THIS IS NOT A NEW POLICY. chains/solana.py::preview_payout() has refused an
    SPL send since the payout path was written, for the reason it states at
    length: an SPL transfer is the Token program's TransferChecked over two
    associated token accounts at the mint's own decimals, and
    chains/solana_transaction.py lays out exactly one shape -- SystemProgram.
    transfer. What was missing is that the CAPABILITY did not know about the
    refusal, so the declaration and the guard disagreed, and the declaration is
    the one every rendered surface reads.

    CLAUDE.md's "SPL/wGRC PAYOUT SUPPORT IN THE PYTHON TREE IS UNTOUCHED ...
    chains/solana.py and SOL_SPL_MINT pay an SPL token out" describes the
    SETTINGS, not a working send. The settings are read (get_balance(),
    describe_payout_address(), build_transfer_plan()'s token-account and rent
    lines all branch on the mint); the SEND is a refusal. This sentence is where a
    reader finds that out before a customer does.

    THE DEPOSIT LEG IS UNAFFECTED, which is worth saying because it is the half
    that reads a mint and has no business doing so: chains/solana.py::
    _credits_in_transaction() has credited ONLY native SOL since 2026-10-11, and
    _spl_credits() runs there solely to record an UnattributableCredit when a
    token arrives and there is no native delta. So `SOL -> *` is untouched by this
    refusal and the lamp on it stays lit.
    """
    return (
        f"cannot pay out: SOL_SPL_MINT={mint} is set, and the SOL payout path builds only the NATIVE "
        f"SystemProgram.transfer -- chains/solana.py::preview_payout() refuses an SPL send outright, "
        f"before any network call, because an SPL transfer is a different instruction over different "
        f"accounts at the mint's own decimals. A swap paying out in SOL would be created, take a "
        f"deposit, and then refuse. Unset SOL_SPL_MINT to pay native SOL out. SOL DEPOSITS ARE "
        f"UNAFFECTED: the deposit leg always reads native SOL and never consults this variable."
    )


def payout_capability(hot_wallet: str = "", mint: str = "") -> tuple[bool, str]:
    """(can_spend, refusal) for SolanaAdapter. THE arming question, asked in one place.

    Returns (True, "") only when ALL THREE hold: a keypair path is exported, an
    on-curve SOL_HOT_WALLET is configured for the key to derive to, and no SPL
    mint is configured. Otherwise (False, <the refusal for whichever case it is>),
    which is the pair chains/solana.py publishes as `can_spend` /
    `payout_refusal` and chains/registry.why_cannot_pay_out() reads back out.

    THIS IS chains/xrp_payout_seed.payout_capability()'s SHAPE, FOLLOWED RATHER
    THAN PARAPHRASED (rule 8), and the one place the two genuinely differ is the
    PARAMETERS. XRP's takes none: the account it pays from is DERIVED from the
    seed, so presence and decodability are the whole question and the environment
    holds both halves. Solana's payer is a SEPARATE setting -- the key file says
    which account it owns and SOL_HOT_WALLET says which account the preview
    announces, and derive_and_check() refuses when they disagree -- so the
    capability needs the configured payer handed to it. It arrives as an argument
    rather than being read from os.environ here for the reason config.py owns:
    SOL_HOT_WALLET is read once, by Config.RPC, and a second read in this module
    would be two spellings of one setting that agree until somebody renames one.

    WHY IT IS A COMPOSED FUNCTION AND NOT THREE IFS IN THE ADAPTER (rule 10). The
    decision is "may this terminal offer SOL as a payout destination", it moves
    money, and it has to be callable with seeded inputs and asserted on directly
    rather than being reachable only by constructing an adapter. Keeping it here
    also keeps chains/solana.py unable to see a key: this returns a bool and a
    sentence.

    ORDER MATTERS AND IT IS KEYPAIR FIRST, exactly as XRP's is presence-first. On
    an unarmed host -- every checkout, every test run, every host where the
    custody decision has not been made -- NEITHER of the other two is set either,
    and reporting "SOL_HOT_WALLET is unset" to somebody who has exported nothing
    at all would send them to configure the second half of a decision they have
    not started. The keypair path is the one that chooses the sentence.

    NO FILE, NO SOCKET, AND NO KEY MATERIAL, which is the property this module's
    header exists for: one environment read through
    keypair_path_from_environment(), one string comparison, and base58 plus field
    arithmetic over 32 bytes. chains/solana.py calls this ONCE at construction,
    and construction is a page render.

    WHAT IT STILL CANNOT ESTABLISH, said plainly because the header's own warning
    about claiming more than presence applies to this function too: that the file
    the path names exists, parses, is 64 bytes, or derives to `hot_wallet`. Those
    need the KEY FILE OPENED, which is refused on every surface that reads
    can_spend, and they are checked by chains/solana_signing.
    load_payout_keypair() and derive_and_check() before anything is signed -- which
    is after a deposit has been taken. That residual gap is the one
    payout_keypair_is_present()'s docstring names as the operator's call, and it is
    NARROWER than it was: the two states measured on 2026-10-11 are gone, and what
    is left needs the file itself to be wrong rather than the configuration.
    """
    if not payout_keypair_is_present():
        return False, missing_keypair_refusal()
    payer = (hot_wallet or "").strip()
    if not payer:
        return False, unset_hot_wallet_refusal()
    # is_valid_address() FIRST, because is_on_curve() RAISES SolanaAddressError for a
    # string that is not an address at all -- deliberately, so that "not an address"
    # and "an address nobody can sign for" cannot be reported as one answer. A
    # capability check that raised would take a page render with it, and
    # chains/solana.py's constructor refuses a malformed SOL_HOT_WALLET on its own
    # account anyway (the loop at the end of __init__, which is why this branch is
    # unreachable from the adapter and still has to be here: this function is the
    # decision and is called with seeded inputs by its own tests).
    if not is_valid_address(payer):
        return False, (
            f"cannot pay out: SOL_HOT_WALLET={payer!r} is not a valid Solana address (32 bytes of "
            f"base58), so there is no account for this payout to debit. Nothing was read from the "
            f"cluster and no key file was opened."
        )
    if not is_on_curve(payer):
        return False, unsignable_hot_wallet_refusal(payer)
    token = (mint or "").strip()
    if token:
        return False, spl_mint_payout_refusal(token)
    return True, ""
