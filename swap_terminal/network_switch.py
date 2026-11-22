"""THE SWITCH: which network this deployment is targeting, declared in one place.

Role: submodule (one environment variable in, one decision out)
Reads: ST_NETWORK, and ST_MAINNET_ARMED_<ASSET> when the target is main
Writes: nothing
Can move funds: no directly, AND IT DECIDES WHETHER ANYTHING MAY. Every guard that
      currently refuses a mainnet endpoint should ask this module instead of
      hardcoding, so a wrong answer here is the most expensive wrong answer in the
      tree. Nothing in it touches a socket, a wallet or a key.
Mainnet-safe: yes to run. Its whole subject is mainnet.

=========================================================================
WHY IT EXISTS, in the operator's words
=========================================================================

2026-11-22, two instructions minutes apart:

    "all chains should be their test or dev net"
    "we will need the ability to put it all on mainnet eventually and at a switch throw"

Those pull in opposite directions unless the network is ONE declared fact. Before this
module there was no such fact: `grep` for ST_NETWORK, NETWORK_TARGET and SWAP_NETWORK found
nothing, and the network was an EMERGENT PROPERTY of six independently configured endpoints
-- three ports in network_target.CHAIN_PORTS and three variables in URL_CHAINS. "Throw the
switch" meant editing six values and trusting that no guard hardcoded testnet. Several did.

THE DESIGN CONSEQUENCE IS THE POINT OF THE WHOLE MODULE: a guard that refuses "mainnet,
ever" has to be TORN OUT to go live, and a guard torn out is a guard nobody re-reads. A
guard that refuses "mainnet while the target says test" is the same protection and survives
the transition. So this module's job is to let every refusal in the tree stay a refusal.

=========================================================================
THE FOUR PROPERTIES, AND EACH ONE IS A DEFECT THIS TREE HAS ALREADY HAD
=========================================================================

1. ABSENCE MEANS TEST. An unset ST_NETWORK is TEST, never main. network_target.py's own
   header records what the other default cost: `config.Config.RPC` read
   `int(os.getenv("GRC_RPC_PORT", "15715"))`, 15715 is Gridcoin MAINNET, nothing in the
   serving path loads a .env, and so `refresh_wallet_inventory()` polled the operator's live
   staking wallet on a loop. A silent default toward real money is the worst available bug
   and it has happened here once already.

2. AN UNRECOGNIZED VALUE REFUSES. Not "falls back to test" -- refuses. `ST_NETWORK=mian` is
   a typo whose charitable reading is mainnet and whose safe reading is test, and guessing
   between them is exactly the class of decision this module exists to remove. A caller
   that cannot be told which network it is on must stop.

3. MAINNET NEEDS THE TARGET **AND** PER-CHAIN ARMING. One flag that puts six chains live at
   once is a single keystroke between a test deployment and six real wallets. The tree
   already has the right idiom for this and it is not invented here: the
   `docker-compose.web.armed-{grc,sol,xrp,icp}.yml` overlays mean a chain is deliberately
   armed, one at a time. `ST_MAINNET_ARMED_BTC=yes` is that idea applied to the switch.

4. THE DECISION IS A FUNCTION, NOT A CONSTANT READ AT IMPORT. Nothing here is cached and
   nothing is computed at module scope. CLAUDE.md rule 12 names import-time side effects as
   a thing the linter cannot catch; a cached target would also mean a test that sets the
   variable gets the answer from whenever the module first loaded, which is the shape of a
   test that passes for the wrong reason.

=========================================================================
WHAT THIS MODULE DOES NOT DO, AND WHY NOT
=========================================================================

IT DOES NOT CLASSIFY AN ENDPOINT. "Is port 15715 mainnet" is network_target.classify()'s
question and stays there (rule 8: one vocabulary, derived in one place). This module answers
"which network am I ALLOWED to be on", and the two are composed by callers through
refuse_if_target_forbids() below. Keeping them separate is what lets the classification stay
a pure function over host and port while the target reads the environment.

IT DOES NOT STAMP A NETWORK ONTO A ROW, and that is the biggest piece of owed work this
module makes visible. Measured 2026-11-22: `swaps` has 24 columns, `payouts` 10 and
`deposit_events` 13, and NOT ONE of them records which network the row was made on
(`swaps.network_fee_reserve` is a fee, not a network). So the day the target flips, mainnet
swaps land in the same tables as every testnet swap ever recorded with nothing to tell them
apart -- and services/payout_service and refresh_swap_from_chain read across the lot. That
is a data-integrity problem rather than a configuration one, it CANNOT be fixed after the
fact, and it is named here because this module is what makes the flip possible.

IT DOES NOT MAKE THE TREE LIVE-READY. It makes the question answerable. Every guard listed
in the switch audit still has to be rewired from "refuse mainnet" to "ask the switch", and
until that is done this module is the single source of a fact nobody consults.
"""

from __future__ import annotations

import os

#: The variable. One name, spelled once.
#:
#: ASCII and no micro sign, per CLAUDE.md rule 6's boundary: a name an operator types into a
#: shell stays ASCII however it is rendered in a report.
TARGET_VARIABLE = "ST_NETWORK"

#: The per-chain arming variable, formatted with the asset. `ST_MAINNET_ARMED_BTC=yes`.
ARMED_VARIABLE = "ST_MAINNET_ARMED_{asset}"

#: The target meaning "a network whose coins cannot be sold" -- testnet, devnet, regtest, a
#: local replica. ONE value for all of them on purpose: which test network a given chain uses
#: is network_target's business, and a target that enumerated them would be a second table to
#: disagree with CHAIN_PORTS (rule 8).
TEST = "test"

#: The target meaning real money.
MAIN = "main"

#: Every value the variable may hold. A tuple so reporting it has a stable order (rule 14:
#: the same run must print the same line twice).
TARGETS = (TEST, MAIN)

#: What counts as "yes" in an arming variable, lowercased and stripped.
#:
#: DELIBERATELY NARROW, AND "1" IS DELIBERATELY ABSENT. An arming variable is an explicit
#: human act, so it takes a word a human typed on purpose. `1` is what a script sets by
#: accident when it means "true" for something else entirely, and the cost of refusing it is
#: one error message while the cost of accepting it is a live wallet.
ARMED_VALUES = frozenset({"yes", "true", "on", "armed"})


class NetworkTargetRefused(Exception):
    """The declared target forbids what a caller is about to do, or cannot be read.

    ONE EXCEPTION FOR BOTH, because a caller's correct response is the same: stop. The
    message says which, since the remedies differ -- a bad value is a typo to fix and a
    forbidden endpoint is either the wrong endpoint or an un-armed chain.
    """


def declared_target() -> str:
    """TEST or MAIN, from the environment. Unset means TEST. Anything unknown raises.

    READ FRESH EVERY CALL, never cached: see property 4 in the module docstring.
    """
    raw = os.environ.get(TARGET_VARIABLE, "").strip().lower()
    if not raw:
        return TEST
    if raw not in TARGETS:
        raise NetworkTargetRefused(
            f"{TARGET_VARIABLE}={raw!r} is not one of {list(TARGETS)}. Refusing rather than "
            f"guessing: the charitable reading of a typo like 'mian' is mainnet and the safe "
            f"reading is test, and choosing between them is exactly the decision this module "
            f"exists to remove. Unset it for {TEST!r}, or spell it {MAIN!r} deliberately."
        )
    return raw


def targeting_mainnet() -> bool:
    """Is this deployment declared live? Raises on an unreadable target rather than guessing."""
    return declared_target() == MAIN


def armed_for_mainnet(asset: str) -> bool:
    """Has THIS chain been individually armed for mainnet?

    SEPARATE FROM THE TARGET, which is the whole of property 3. The target says "this
    deployment is live"; this says "and this particular chain is meant to be". Both are
    required, so no chain goes live by inheriting a global flag.
    """
    raw = os.environ.get(ARMED_VARIABLE.format(asset=asset.strip().upper()), "").strip().lower()
    return raw in ARMED_VALUES


def may_use_mainnet(asset: str) -> tuple[bool, str]:
    """(allowed?, the sentence saying why not). Never raises; for a caller that reports.

    THE SENTENCE IS THE POINT. A bare False tells an operator nothing about which of the two
    gates stopped them, and the remedies are different variables (rule 14: state what the
    answer means, next to the answer).
    """
    try:
        target = declared_target()
    except NetworkTargetRefused as refused:
        return False, str(refused)
    if target != MAIN:
        return False, (
            f"{TARGET_VARIABLE} is {target!r}, so this deployment is not live. Every chain is "
            f"on its test or dev network. Set {TARGET_VARIABLE}={MAIN} to change that, and "
            f"arm each chain separately."
        )
    variable = ARMED_VARIABLE.format(asset=asset.strip().upper())
    if not armed_for_mainnet(asset):
        return False, (
            f"{TARGET_VARIABLE} is {MAIN!r} but {asset.strip().upper()} is not armed: set "
            f"{variable} to one of {sorted(ARMED_VALUES)}. The target alone arms nothing, "
            f"deliberately -- one flag that put six chains on mainnet at once would be a "
            f"single keystroke between a test deployment and six real wallets."
        )
    return True, ""


def refuse_if_target_forbids(asset: str, classified: str) -> None:
    """Raise unless `classified` is a network this deployment may use for `asset`.

    `classified` IS network_target.classify()'s WORD AND IS NOT RE-DERIVED HERE (rule 8).
    This module never looks at a port; it is handed the classification and answers only
    whether the target permits it. That composition is what keeps classify() a pure function
    over host and port while the target reads the environment.

    THE CASES, and the two that are NOT mainnet are refused for a different reason:

      MAINNET       allowed only when the target is main AND the asset is armed.
      TEST          always allowed. A test endpoint is safe on a live deployment too -- it
                    moves nothing -- and refusing it would make the switch unable to run a
                    single chain in test while others are live, which is the ordinary way a
                    chain gets added.
      anything else UNCONFIGURED and UNRECOGNIZED are refused regardless of target, and that
                    is not this module being strict: an endpoint nobody can name is one
                    whose network is unknown, and "unknown" on a payout path is the absence
                    of evidence that CLAUDE.md's chain-safety rules treat as mainnet.
    """
    word = (classified or "").strip().upper()
    if word == "TEST":
        return
    if word == "MAINNET":
        allowed, why = may_use_mainnet(asset)
        if allowed:
            return
        raise NetworkTargetRefused(
            f"{asset.strip().upper()}: that endpoint is MAINNET and this deployment may not "
            f"use it. {why}"
        )
    raise NetworkTargetRefused(
        f"{asset.strip().upper()}: that endpoint classifies as {word or '(nothing)'}, not "
        f"TEST or MAINNET, so which network it is on is unknown. Refused on any target: an "
        f"absence of evidence about a network is treated as mainnet here (CLAUDE.md's "
        f"chain-safety rules), and network_target.classify() is the one place that names a "
        f"network from an endpoint."
    )


def describe() -> str:
    """One line for a startup banner. Never blank, and it always names the variable.

    Rule 14: a line that does not say which network it is on is a line that will eventually
    be read as the wrong one -- so this is written to be pasted into an operator's output,
    where the variable name is what makes the value actionable a day later.
    """
    try:
        target = declared_target()
    except NetworkTargetRefused as refused:
        return f"network target UNREADABLE: {refused}"
    if target != MAIN:
        return f"network target {target!r} ({TARGET_VARIABLE}) -- every chain on its test or dev network"
    return (
        f"network target {MAIN!r} ({TARGET_VARIABLE}) -- LIVE. Armed chains move REAL funds; "
        f"each needs its own {ARMED_VARIABLE.format(asset='<ASSET>')}"
    )
