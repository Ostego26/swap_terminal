"""The one place that knows the NAME of the XRP signing seed's environment variable.

Role: function layer (rule 10 -- the smallest testable pieces. Two decisions:
      "is a signing seed present in this process" and "what does an absent one
      mean", plus one reader that hands the VALUE to the single caller allowed
      to have it)
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
  - the ADAPTER cannot reach it. chains/xrp.py imports
    signing_seed_is_present() and missing_seed_refusal() from this module and
    NOT signing_seed(), so setting the variable does not let the adapter sign:
    an armed send still requires the seed to be passed at the call site.
    tests/test_xrp_payout_wiring.py asserts that behaviorally -- the variable
    set, send_to_address() called with no seed, and the refusal is the
    assertion -- rather than by reading this paragraph.

AND THE REFUSAL IS STILL THE DEFAULT. With the variable unset -- which is every
checkout, every test run and every host where the operator has not made the
custody decision -- signing_seed_is_present() is False, chains/xrp.py sets
can_spend False, chains/registry.why_cannot_pay_out() reports the sentence
below, and services/swap_service.create_swap() refuses to create a swap whose
payout leg is XRP. Nothing is created, so nothing is taken. Turning it on is an
operator act that requires supplying a secret, which is exactly where rule 16
puts a decision that moves money.
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
    allowed to learn. chains/xrp.py uses it to set can_spend, which decides
    whether this terminal offers XRP as a payout destination at all -- so the
    question that reaches the customer page is "is one present", and the value
    stays in this function's caller's environment.

    "PRESENT" IS NOT "CORRECT", and the distinction is the same one
    services/payout_service.unlock_readiness_lines() makes about the Gridcoin
    passphrase: a wrong seed still fails, and it fails at
    chains/xrp_signing.derive_and_check() BEFORE anything is signed, because the
    address it derives will not be the account the preview announced. Claiming
    more than presence here would be the reassuring answer rather than the
    measured one (rule 17).

    A SET-BUT-EMPTY VALUE IS ABSENT. `export XRP_PAYOUT_SECRET_SEED=''` is what
    a generator run from a shell without the value writes, and config.py's own
    _env() carries the measurement that cost: five empty exports on
    2026-09-26 turned a missing setting into a crash at import. Here the
    consequence of reading "" as present would be worse than a crash -- it would
    make can_spend True, let an XRP swap be created, take a customer's deposit,
    and then refuse the payout at the send with the deposit already credited.
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
    That function's contract -- a bool, "never says what it is" -- is what
    can_spend reads, and can_spend decides whether a pair is OFFERED. Narrowing it
    from "present" to "decodes" changes what this terminal will trade, which is
    live posture and the operator's call (rule 16). This function exists so the
    DIAGNOSTICS can tell the truth today, while that decision is theirs to make;
    swap_readiness.py and the worker banner read it, chains/xrp.py deliberately
    still does not.

    IT RETURNS THE REASON AND NEVER THE VALUE. A seed is a key: the sentence names
    the variable and the exception type, and xrp_payout_account.seed_shape() is
    where a caller gets shape facts without content.

    OFFLINE BY CONSTRUCTION. Decoding is base58check plus a key derivation; no
    rippled is contacted, so this is safe in a banner that prints before any
    network is reachable.
    """
    seed = signing_seed()
    if not seed:
        return False, f"{SIGNING_SEED_ENV_VAR} is not set in this process"
    try:
        # Imported here, not at module scope: this module is imported by the
        # customer page's path and xrpl-py is an optional dependency, so a missing
        # one must degrade to a sentence rather than an ImportError at import time.
        from xrpl.wallet import Wallet  # noqa: PLC0415
    except ImportError:
        return False, ("xrpl-py is not importable in this interpreter, so whether the seed decodes was "
                       "NOT established -- and a payout could not sign either way")
    try:
        Wallet.from_seed(seed)
    except Exception as error:  # noqa: BLE001 -- checked: returns False with the exception TYPE in the sentence. str(error) is excluded because xrpl-py has echoed the offending seed in its own messages.
        return False, (f"{SIGNING_SEED_ENV_VAR} is set but does NOT decode as a seed "
                       f"({type(error).__name__}); run xrp_payout_account.py, which reports its shape "
                       f"without printing it. chains/xrp_signing.py uses the same Wallet.from_seed(), so "
                       f"every XRP payout would fail at signing -- after the deposit is irreversible")
    return True, f"{SIGNING_SEED_ENV_VAR} decodes as a seed, so a payout can derive its signing wallet"
