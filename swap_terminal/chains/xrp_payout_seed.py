"""The one place that knows the NAME of the XRP signing seed's environment variable.

Role: function layer (rule 10 -- the smallest testable pieces. Three decisions:
      "is a signing seed present in this process", "does the seed in this process
      DECODE", and "what does each kind of unarmed mean", plus one derivation
      (payout_capability()) that composes them into the pair XRPAdapter publishes,
      plus one reader that hands the VALUE to the single caller allowed to have it)
Reads: this process's environment, and nothing else. No file, no socket, no
      Config, no database. os.environ is read AT CALL TIME rather than at
      import, so a test can set it and a long-lived process cannot bake a
      stale answer in.
Writes: nothing.
Can move funds: no, and not one function here signs or submits anything.
      signing_seed() RETURNS a seed to its caller and that is the whole of its
      power; chains/xrp.py's _sign_and_submit() is what signs, and it takes the
      seed as an argument.
Mainnet-safe: yes. Nothing here knows which network anything is on -- the
      mainnet refusal is chains/xrp_signing.require_non_mainnet(), decided from
      the id the SERVER reports, and it runs whether or not a seed exists.

WHY THIS FILE EXISTS AT ALL, AND WHAT IT CHANGED

Until 2026-10-02 chains/xrp.py's header said, correctly, that no environment
variable could arm an XRP payout: the adapter held no key and read no key path,
so the only way to sign was for a caller to pass a seed it already had. The only
such caller was xrp_payout_verify.py, a root script an operator runs by hand
against the testnet faucet files. services/payout_service.py had no seed to pass
and called send_to_address() with two positional arguments, so every XRP payout
raised XRPSendNotArmed and the swap landed in `failed` WITH THE DEPOSIT ALREADY
CREDITED.

The operator asked for the opposite ("we should be able to swap any coin for
another of any combination"), so a seed now has to reach the payout worker, and
a worker cannot be handed one by hand. The environment is where this repository
already puts exactly this kind of value -- see
services/payout_service.WALLET_UNLOCK_ENV_VAR, which is Gridcoin's wallet
passphrase, read from the environment at use time for the reasons restated
below -- so XRP follows the shape that is already here rather than inventing a
second one (rule 8).

WHAT IS HONESTLY WEAKER NOW, said plainly rather than discovered later. Before
this file, configuration alone could not arm an XRP payout. It can now: a
process started with this variable set CAN sign and submit XRP payments, and
anything that can run code in that process can read the variable. That is the
same exposure the Gridcoin passphrase already carries and it is the price of a
worker that pays out without a person present. Every narrowing available is
applied, and they are the same ones that file lists:

  - the value is read from the ENVIRONMENT at use time. It is never written to
    this repository, never written to a file by this code, never placed in argv
    (visible in `ps` to every process on the box, and kept in shell history),
    and never logged at any level.
  - it is never read from Config. Config is echoed on the admin page through an
    allowlist and services/admin_view.py's own comment notes that Config.RPC
    holds wallet credentials "one key away from these". A seed must not be one
    key away from something that renders.
  - no function here returns the value except signing_seed(), whose one caller
    is the send in services/payout_service.broadcast_payout(), where it is
    passed straight into send_to_address() as an argument expression and is
    never bound to a local name, never put in a dict, and never returned
    onward.
  - the ADAPTER cannot reach it. chains/xrp.py imports payout_capability(),
    signing_seed_is_present(), signing_seed_decodes() and the two refusals from
    this module and NOT signing_seed(). payout_capability() DECODES the seed and
    returns a bool and a sentence; the value never leaves this module, so
    setting the variable does not let the adapter sign:
    an armed send still requires the seed to be passed at the call site.
    tests/test_xrp_payout_wiring.py asserts that behaviorally -- the variable
    set, send_to_address() called with no seed, and the refusal is the
    assertion -- rather than by reading this paragraph.

AND THE REFUSAL IS STILL THE DEFAULT. With the variable unset -- which is every
checkout, every test run and every host where the operator has not made the
custody decision -- payout_capability() returns (False, missing_seed_refusal()),
chains/xrp.py sets can_spend False, chains/registry.why_cannot_pay_out() reports
that sentence, and services/swap_service.create_swap() refuses to create a swap
whose payout leg is XRP. Nothing is created, so nothing is taken. Turning it on
is an operator act that requires supplying a secret, which is exactly where rule
16 puts a decision that moves money.

AND SINCE 2026-10-03 A SET VARIABLE IS NOT ENOUGH: THE VALUE HAS TO DECODE.
payout_capability() is the arming question and it asks signing_seed_decodes(),
not signing_seed_is_present(). What that closed, measured on the operator's host
that day: XRP_PAYOUT_SECRET_SEED held NINE CHARACTERS that do not start with
's' -- a placeholder, not a seed -- and chains/xrp.py:369 read PRESENCE, so
can_spend was True, why_cannot_pay_out() returned "", create_swap()'s cannot_pay
gate passed, and this terminal considered XRP a payout destination it could
serve. chains/xrp_signing.py:397 derives the signing wallet with
Wallet.from_seed(), which raises ValueError on that value, so EVERY XRP payout
would have failed at signing AFTER the customer's deposit was confirmed and
irreversible. The only thing that prevented it was XRP_DEPOSIT_ACCOUNT being
unset -- a different gate, which the operator was in the middle of setting.

The narrowing was the operator's to authorize, because it changes which pairs
this terminal OFFERS (rule 16: live posture). They authorized it 2026-10-03.
What is NOT claimed by an armed state: that the seed controls the account this
host pays from. chains/xrp_signing.derive_and_check() still refuses a seed
paired with an account it does not control, before signing.
"""

from __future__ import annotations

import os

# THE NAME OF the variable, which is not itself a secret.
#
# Named ..._ENV_VAR rather than ..._SEED or ..._SECRET for the reason
# services/payout_service.WALLET_UNLOCK_ENV_VAR records: a constant whose own
# name says "secret" and whose value is a string literal reads to ruff's S105 as
# a hardcoded credential, and renaming the constant is the honest fix rather
# than a suppression (rule 19). The confusion S105 exists to catch is real --
# this constant holds a NAME and the thing it names holds a seed.
#
# The VALUE spells SECRET on purpose. An operator reading `export
# XRP_PAYOUT_SECRET_SEED=...` in a shell history, a systemd unit or a pasted
# diagnostic should need no documentation to know they are looking at something
# that must not be shared. `XRP_SEED` would have been shorter and would have
# read like a configuration setting.
SIGNING_SEED_ENV_VAR = "XRP_PAYOUT_SECRET_SEED"


def signing_seed_is_present() -> bool:
    """Whether this process HAS an XRP signing seed. Never says what it is.

    A bool, deliberately, and the only thing any caller outside the send is
    allowed to learn: the value stays in this function's caller's environment.

    IT NO LONGER SETS can_spend, AND THAT IS THE 2026-10-03 CHANGE. Until that
    day chains/xrp.py:369 was `self.can_spend = signing_seed_is_present()`, and
    a nine-character placeholder on the operator's host therefore offered XRP as
    a payout destination this terminal could not pay (the measurement is in this
    module's docstring and in signing_seed_decodes() below). The arming question
    is payout_capability() now, which decodes. This function answers the narrower
    question it always answered -- IS THERE A VALUE AT ALL -- and its remaining
    callers want exactly that: payout_capability() asks it first so the two kinds
    of unarmed get different sentences, chains/xrp.py's banner asks it to choose
    which of the three posture lines to print, and
    services/payout_service.broadcast_payout()'s pre-flight asks it to detect the
    variable having been REMOVED between a swap's creation and its payout.

    "PRESENT" IS NOT "CORRECT", and the distinction is the same one
    services/payout_service.unlock_readiness_lines() makes about the Gridcoin
    passphrase: a wrong seed still fails, and it fails at
    chains/xrp_signing.derive_and_check() BEFORE anything is signed, because the
    address it derives will not be the account the preview announced. Claiming
    more than presence here would be the reassuring answer rather than the
    measured one (rule 17) -- which is why the stronger claim lives in
    signing_seed_decodes() and not in a widened version of this function.

    A SET-BUT-EMPTY VALUE IS ABSENT. `export XRP_PAYOUT_SECRET_SEED=''` is what
    a generator run from a shell without the value writes, and config.py's own
    _env() carries the measurement that cost: five empty exports on
    2026-09-26 turned a missing setting into a crash at import. Here the
    consequence of reading "" as present is a WRONG SENTENCE rather than a wrong
    posture, since 2026-10-03: payout_capability() would go on to ask whether ""
    decodes, it does not, and the pair would still be refused -- but the refusal
    would read "the value in it is not a seed" and send the operator to inspect a
    variable they never exported. Before that date it was worse than a crash: it
    made can_spend True, let an XRP swap be created, took a customer's deposit,
    and refused the payout at the send with the deposit already credited.
    """
    return bool(os.environ.get(SIGNING_SEED_ENV_VAR, "").strip())


def signing_seed() -> str:
    """The seed itself, for the ONE caller that signs. Returns "" when unset.

    THE ONLY FUNCTION IN THIS TREE THAT READS THIS VARIABLE'S VALUE, and its one
    caller is the send in services/payout_service.broadcast_payout(). Grep for
    the name of this function to enumerate every place an XRP seed can be
    obtained; that enumeration is the point of putting it behind a function
    rather than letting call sites reach for os.environ themselves.

    RETURNS "" RATHER THAN RAISING, and that is not laziness about error
    handling -- it is so that the refusal stays in ONE place. An empty seed
    reaches chains/xrp_signing.require_send_confirmation(), which already
    refuses "armed but no signing seed was supplied" with the reason written
    out, and which is the guard every other unarmed path lands on. A second
    refusal here would be rule 8's two-copies-of-one-rule on the question of
    whether a send is armed, and the copies would drift.

    It is stripped, for the same reason signing_seed_is_present() treats a
    set-but-empty value as absent: a trailing newline from a `read` or a heredoc
    is not part of the seed, and an XRPL seed with whitespace on it fails inside
    xrpl-py's base58 decoder with a message about the encoding rather than about
    the variable.
    """
    return os.environ.get(SIGNING_SEED_ENV_VAR, "").strip()


def missing_seed_refusal() -> str:
    """The sentence a customer page, an admin page and a spawn banner all print.

    ONE sentence, read through chains/registry.why_cannot_pay_out(), because the
    alternative is what was measured on the operator's host on 2026-10-02: in one
    process, the spawn banner said

        about to spawn    a payout worker CAN broadcast on GRC, XRP.

    while the customer page said of the same asset, at the same moment,

        XRP cannot pay out: it holds no signing key, and
        services/payout_service.py calls send_to_address() without the arming
        token

    Two sentences about one asset, disagreeing, in one process. Both are now
    derived from signing_seed_is_present(), so they cannot.

    IT NAMES THE VARIABLE AND NOT ITS VALUE. Rule 14: echo the parameter that
    decides the answer, because the operator reads the screen rather than this
    file, and a worker inherits the shell that spawned it with nothing
    downstream able to see which shell that was.
    """
    return (
        f"cannot pay out: {SIGNING_SEED_ENV_VAR} is not set in this process's environment, so there is "
        f"no signing seed and every XRP payout would refuse before signing. This is the DEFAULT and it "
        f"is why no XRP swap can be created: a swap that cannot be paid out would take a deposit it can "
        f"never settle. Export {SIGNING_SEED_ENV_VAR} in the shell that starts this process -- a value "
        f"set in a file, or in another shell, does not reach here -- and set XRP_DEPOSIT_ACCOUNT to the "
        f"account that seed controls. Both are custody decisions and neither has a default."
    )


def derived_payout_account() -> tuple[str, str]:
    """The PUBLIC account this process's signing seed controls, or ("", why not). NEVER RAISES.

    ADDED 2026-10-03 FOR wallet_custody.py, AND IT REMOVED A DUPLICATE RATHER THAN
    ADDING ONE. signing_seed_decodes() below already called Wallet.from_seed() and
    threw the wallet away; it now calls this and keeps its own sentence, so the
    derivation exists ONCE in this module instead of twice (rule 8). Net change to
    the number of places in this module that decode a seed: zero.

    THE SEED NEVER LEAVES THIS MODULE FOR THIS QUESTION, which is the whole reason
    the reader is here and not in the caller. A diagnostic that wanted the desk's
    XRP account would otherwise call signing_seed() and derive the wallet itself,
    which is a third place in the tree holding a live seed in a local variable.
    This returns a classic address -- a public value, safe in a pasted report --
    and the seed is never in the return value, never in the refusal, and never
    logged.

    WHAT IT IS NOT: ownership. Deriving the account a seed controls says nothing
    about whether XRP_DEPOSIT_ACCOUNT names that account; comparing the two is
    services/custody_separation.xrp_desk_account_verdict(), and the refusal before
    signing is chains/xrp_signing.derive_and_check().

    THE OTHER SPELLING, NAMED BECAUSE A READER WHO FINDS ONE MUST BE TOLD THE OTHER
    EXISTS (rule 8): xrp_payout_account.derived_account(seed) takes a seed as an
    ARGUMENT and reports its SHAPE when it will not decode -- a length, a prefix
    family, whether it carries quote characters -- because that tool's whole job is
    to help an operator fix a bad export. The two genuinely differ: that one is
    given a value and diagnoses it, this one reads the environment and will not
    hand the value back. Neither can be expressed as the other without giving a
    caller either the seed or a tool that cannot see it.

    OFFLINE BY CONSTRUCTION. Deriving a classic address is base58check plus a key
    derivation; no rippled is contacted, so this is safe where no network is
    reachable.
    """
    seed = signing_seed()
    if not seed:
        return "", f"{SIGNING_SEED_ENV_VAR} is not set in this process"
    try:
        # Imported here, not at module scope: this module is imported by the
        # customer page's path and xrpl-py is an optional dependency, so a missing
        # one must degrade to a sentence rather than an ImportError at import time.
        from xrpl.wallet import Wallet  # noqa: PLC0415
    except ImportError:
        return "", ("xrpl-py is not importable in this interpreter, so whether the seed decodes was "
                    "NOT established -- and a payout could not sign either way")
    try:
        wallet = Wallet.from_seed(seed)
    except Exception as error:  # noqa: BLE001 -- checked: returns "" plus the exception TYPE in the sentence, so no caller can read a failure as an account. str(error) is excluded because xrpl-py has echoed the offending seed in its own messages.
        return "", (f"{SIGNING_SEED_ENV_VAR} is set but does NOT decode as a seed "
                    f"({type(error).__name__}); run xrp_payout_account.py, which reports its shape "
                    f"without printing it. chains/xrp_signing.py uses the same Wallet.from_seed(), so "
                    f"every XRP payout would fail at signing -- after the deposit is irreversible")
    return str(wallet.classic_address), ""


def signing_seed_decodes() -> tuple[bool, str]:
    """Does the seed in this process DECODE. (yes/no, the sentence). No network, no value.

    PRESENCE IS NOT VALIDITY, AND THE GAP BETWEEN THEM WAS A 9-CHARACTER
    PLACEHOLDER ON THE LIVE HOST.

    Measured 2026-10-03. XRP_PAYOUT_SECRET_SEED was SET, so
    signing_seed_is_present() returned True, so chains/xrp.py:369 set
    can_spend = True, so chains/registry.why_cannot_pay_out() returned "" and this
    terminal considered XRP a payout destination it could serve. The value was nine
    characters and did not start with 's'. Wallet.from_seed() raises ValueError on
    it, and chains/xrp_signing.py:397 -- the PAYOUT's own derivation -- is the same
    call, so every XRP payout would have failed at signing, after the customer's
    deposit was confirmed and irreversible.

    The only reason it had not already happened is that XRP_DEPOSIT_ACCOUNT was
    unset, which blocks XRP swaps through a DIFFERENT gate. The operator was asking
    to have that variable set when this was found.

    WHY THIS IS A SEPARATE FUNCTION AND NOT A CHANGE TO signing_seed_is_present().
    That function's contract -- a bool, "never says what it is" -- was what
    can_spend read, and can_spend decides whether a pair is OFFERED. Narrowing it
    from "present" to "decodes" changes what this terminal will trade, which is
    live posture and the operator's call (rule 16). So this function was added
    first, for the DIAGNOSTICS, while that decision stayed theirs.

    THE OPERATOR AUTHORIZED THE NARROWING LATER THE SAME DAY, 2026-10-03, and
    payout_capability() below is where it landed -- still not inside
    signing_seed_is_present(), because the two questions have to stay separately
    askable: the refusal an unarmed destination produces has to say WHICH of them
    failed, and one bool cannot. "Is a value exported" and "does that value
    decode" send an operator to two different places.

    IT RETURNS THE REASON AND NEVER THE VALUE. A seed is a key: the sentence names
    the variable and the exception type, and xrp_payout_account.seed_shape() is
    where a caller gets shape facts without content.

    OFFLINE BY CONSTRUCTION. Decoding is base58check plus a key derivation; no
    rippled is contacted, so this is safe in a banner that prints before any
    network is reachable.
    """
    # THE DERIVATION IS derived_payout_account()'s, AND USED TO BE SPELLED AGAIN
    # HERE. Both copies called Wallet.from_seed() on signing_seed() and produced
    # the same three refusals; this one discarded the wallet and that one keeps
    # its address. Two copies of a decode is rule 8's bug with a delay on it, and
    # the delay would have been measured in refusals that disagree -- a diagnostic
    # saying the seed decodes while a banner said it does not, from one process.
    #
    # What stays here is the AFFIRMATIVE sentence, which is this function's own:
    # an address is not a thing a banner wants to print, and "decodes" is.
    address, refusal = derived_payout_account()
    if not address:
        return False, refusal
    return True, f"{SIGNING_SEED_ENV_VAR} decodes as a seed, so a payout can derive its signing wallet"


def undecodable_seed_refusal(reason: str) -> str:
    """The OTHER refusal: the variable IS set, and what is in it is not a seed.

    ADDED 2026-10-03, WITH THE NARROWING OF can_spend, BECAUSE ONE SENTENCE COULD
    NOT COVER BOTH CASES. missing_seed_refusal() says "export
    XRP_PAYOUT_SECRET_SEED in the shell that starts this process", which is the
    right instruction when nothing is exported and the WRONG one when something
    is. An operator whose shell holds a nine-character placeholder -- the state
    measured on their host that day -- would read "is not set in this process's
    environment", check their shell, find it set, and have been sent to inspect
    the one thing that was fine. Rule 14: the line has to say what the number
    means next to the number, and here the fact that decides the answer is not
    presence but CONTENT.

    So the vocabulary is EXTENDED rather than replaced. Two refusals, one per
    case, both reached through payout_capability() so no caller has to choose:

      no variable at all   missing_seed_refusal()      "export it"
      a value, not a seed  this function               "the value is not a seed"

    IT NEVER CARRIES THE VALUE. `reason` is signing_seed_decodes()'s sentence,
    which names the VARIABLE and the EXCEPTION TYPE and nothing else -- that
    function excludes str(error) deliberately, because xrpl-py has echoed the
    offending seed in its own messages, and this refusal goes to the admin page
    and the worker banner. Pass it anything else and a key reaches a screen.

    IT POINTS AT A SHAPE REPORT RATHER THAN ASKING FOR THE VALUE. The operator's
    next question is "what IS in my variable, then", and the answer is
    xrp_payout_account.py, which reports length, prefix and character class
    WITHOUT printing it. Naming that script here is what keeps the operator from
    echoing a seed into a terminal to compare it by eye.
    """
    return (
        f"cannot pay out: {SIGNING_SEED_ENV_VAR} IS set in this process's environment, but the value "
        f"in it is not a usable signing seed -- {reason}. This is NOT the default refusal: something "
        f"was exported, so the thing to fix is the VALUE and not the export. A real XRPL family seed "
        f"is 29 base58 characters beginning with 's' (secp256k1) or 'sEd' (ed25519); run "
        f"xrp_payout_account.py, which reports the shape of what you have WITHOUT printing it. XRP is "
        f"offered as a payout destination only while this value decodes, because "
        f"chains/xrp_signing.py derives the signing wallet with the same Wallet.from_seed() -- so a "
        f"swap created in this state would take a deposit and then fail at signing, after the deposit "
        f"is irreversible. Measured on the live host 2026-10-03: this variable held a nine-character "
        f"placeholder while this terminal was offering XRP."
    )


def payout_capability() -> tuple[bool, str]:
    """(can_spend, refusal) for XRPAdapter. THE arming question, asked in one place.

    Returns (True, "") only when a seed is exported AND it decodes. Otherwise
    (False, <the refusal for whichever case it is>), which is the pair
    chains/xrp.py publishes as `can_spend` / `payout_refusal` and
    chains/registry.why_cannot_pay_out() reads back out.

    WHY A COMPOSED FUNCTION AND NOT TWO IFS IN THE ADAPTER (rule 10). The
    decision is "may this terminal offer XRP as a payout destination", it moves
    money, and it has to be callable with a seeded environment and asserted on
    directly -- not reachable only by constructing an adapter, which needs a url.
    Keeping it here also keeps chains/xrp.py unable to see a seed: this returns a
    bool and a sentence, and signing_seed() is still imported by exactly one
    caller in this tree (the send in services/payout_service.broadcast_payout()).

    ORDER MATTERS AND IT IS PRESENCE FIRST. signing_seed_decodes() answers False
    for an unset variable too, so a single decode check would have collapsed both
    cases into "does not decode" and told an operator with nothing exported that
    their value was wrong. Presence chooses the sentence; decoding chooses the
    posture.

    WHAT CHANGED, measured on the operator's host 2026-10-03 and why this function
    exists at all: chains/xrp.py:369 was `self.can_spend =
    signing_seed_is_present()`. XRP_PAYOUT_SECRET_SEED held nine characters that
    do not start with 's', so can_spend was True, why_cannot_pay_out() returned ""
    and create_swap()'s cannot_pay gate passed -- this terminal was offering a
    payout it would have failed to sign, after the deposit was irreversible.
    XRP_DEPOSIT_ACCOUNT being unset was the only thing stopping it, and it was
    being set. The operator authorized narrowing can_spend that day.

    NO NETWORK, NO VALUE PRINTED, NO FUNDS MOVED. Everything here is one
    environment read plus base58check and a key derivation, so it is safe in an
    adapter constructor that runs before any rippled is reachable -- which is
    where XRPAdapter calls it, once, at construction (see that call site for why
    once and not per access).
    """
    if not signing_seed_is_present():
        return False, missing_seed_refusal()
    decodes, why = signing_seed_decodes()
    if not decodes:
        return False, undecodable_seed_refusal(why)
    return True, ""
