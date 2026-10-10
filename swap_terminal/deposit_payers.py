"""The one place that says how each chain's DEPOSIT leg gets paid, or that it cannot.

Role: submodule (one table and pure lookups; no network, no wallet, no key)
Reads: nothing. Callers pass it an asset name.
Writes: nothing
Can move funds: no. It names the tools that can; it calls none of them.
Mainnet-safe: yes

=============================================================================
WHY THIS FILE EXISTS, AND IT IS A DEFECT MEASURED ON 2026-10-10
=============================================================================

The operator created an ICP -> GRC swap, the page showed a deposit address and
an amount, and NOTHING IN THE TREE COULD PAY IT. That took several attempts to
establish, because the absence of a tool looks exactly like a tool being hard to
find.

Measured that day, against the six assets `Config.ALLOWED_PAIRS` lets a swap
start from:

    asset   could its deposit leg be paid?
    SOL     yes   pay_test_deposit.py, since 2026-10-01, full guards
    XRP     partly  xrp_send_tagged.py, whose own header calls it an
                    "operator test fixture" rather than a swap payer
    ICP     NO    -- the swap that exposed this
    BTC     NO
    LTC     NO
    GRC     NO

Three of six legs unpayable, and the two that worked were unrelated tools with
different names and different levels of swap-awareness. The payer was being
written per chain, ad hoc, whenever somebody hit the wall -- so the wall was
going to be hit three more times.

THE OPERATOR'S INSTRUCTION, 2026-10-10: "make sure this doesn't happen when it's
any other chain/coin."

So this is the registry, and tests/test_deposit_payer_coverage.py is the gate.
The gate's value is not that it lists today's gaps -- a comment could do that. It
is that ADDING AN ASSET TO ALLOWED_PAIRS WITHOUT DECIDING HOW ITS DEPOSIT GETS
PAID FAILS THE SUITE. The next chain cannot arrive the way ICP did.

=============================================================================
WHY THIS IS A REGISTRY AND NOT ONE DISPATCHER THAT SENDS ON ANY CHAIN
=============================================================================

Every adapter has `send_to_address(address, amount, ...)`, so a single function
that paid any leg looks like an afternoon's work. It would be wrong, and the
reason is written into two of the signatures:

  chains/xrp.py:1039    `send_to_address` takes FOUR keyword-only arguments and
      carries a noqa explaining them: "these four keywords ARE the payment, and
      bundling them into one object would add a type without removing a
      parameter AND would let a single object carry both the seed and the arming
      token. Their separation is the reason a forgotten opt-in cannot become a
      send."
  chains/solana.py:1722 the same shape -- `confirm_send` is an arming token, not
      a flag.

Those tokens exist because of a measured incident recorded in
chains/xrp_payout_seed.py: `can_spend` read True on a value it should not have,
an XRP swap was offered, and the desk took a customer's deposit it could not
pay. A generic dispatcher would have to supply those tokens from somewhere, and
supplying them automatically is precisely the failure the separation prevents.

So the registry names WHO pays each leg and WHAT the operator must supply
explicitly. The arming stays in the operator's hands; only the knowledge of
where the tool is becomes shared.

=============================================================================
`can_spend` IS NOT THE ANSWER TO "CAN THIS LEG BE PAID"
=============================================================================

Worth stating because it is the obvious shortcut and it is wrong in both
directions. `chains/*.can_spend` answers "can the PAYOUT path send on this
chain", which is a different question:

  ICP     can_spend is False -- every adapter call is `--identity anonymous` --
          and yet pay_icp_deposit.py sends perfectly well, because it builds its
          own transport with a signing identity. False, and payable.
  SOL/XRP can_spend is DERIVED from whether payout key material is present. A
          deposit is sent from the desk's own wallet, which is the same key, so
          here the two happen to coincide -- but by coincidence, not by meaning.

A caller that gated deposit payment on can_spend would have reported ICP
unpayable on the one day somebody proved it was not.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class DepositPayer:
    """How one asset's deposit leg gets paid, or why it cannot be.

    FROZEN because this is a statement of fact about the tree, not configuration.
    Changing what pays a leg means changing the tree, and then this line.
    """

    #: The command an operator runs, or "" when nothing in the tree can pay it.
    tool: str
    #: What the operator must supply EXPLICITLY for a send to happen. "" means
    #: the chain's own wallet is sufficient. This is never auto-filled by
    #: anything -- see the header on arming tokens.
    arming: str
    #: Why. For an unpayable leg, what specifically is missing.
    note: str

    @property
    def payable(self) -> bool:
        return bool(self.tool)


#: asset -> how its deposit leg gets paid. EVERY asset that can be a swap's
#: `from_asset` must appear here; tests/test_deposit_payer_coverage.py fails
#: otherwise, which is the whole point of the file.
PAYERS: dict[str, DepositPayer] = {
    "ICP": DepositPayer(
        tool="python3 pay_icp_deposit.py --deposit-address <the address on the page> --apply",
        arming="a dfx identity that can sign; the default identity in the replica container "
        "is the one fund_desk.py tops up",
        note="Added 2026-10-10, the swap that exposed this whole gap. It does NOT use the "
        "adapter to send: chains/icp.py carries can_spend = False because every call it "
        "makes is --identity anonymous. The idempotency key is derived from the swap's "
        "created_at, so re-running the command is one transfer rather than two.",
    ),
    "SOL": DepositPayer(
        tool="python3 pay_test_deposit.py",
        arming="SOL_PAYOUT_KEYPAIR_PATH, plus the adapter's own `confirm_send` token",
        note="Since 2026-10-01, written after four failures in one afternoon of doing it in "
        "a shell -- its header records all four, including the 82.65 tGRC that went to an "
        "address the wallet held no key for. Devnet by construction: it cannot reach a "
        "mainnet Solana endpoint.",
    ),
    "XRP": DepositPayer(
        tool="python3 xrp_send_tagged.py",
        arming="XRP_PAYOUT_SECRET_SEED, plus the adapter's arming token; four keyword-only "
        "arguments kept separate so a forgotten opt-in cannot become a send",
        note="PARTIAL, AND THE GAP IS NAMED RATHER THAN GLOSSED: its own header calls it an "
        "'operator test fixture' that sends ONE tagged payment to verify the deposit path, "
        "not a swap payer. It does not look a swap up, so it cannot refuse the things "
        "pay_icp_deposit.py refuses -- an expired swap, a swap that already has a deposit, "
        "a status past awaiting_deposit. Paying a real XRP leg with it means checking those "
        "by hand, which is how the 82.65 tGRC was lost on a different chain.",
    ),
    "BTC": DepositPayer(
        tool="python3 pay_deposit.py --deposit-address <the address on the page> --apply",
        arming="an unlocked wallet, and a daemon reporting a TEST network -- "
        "pay_deposit.refuse_mainnet() has no override flag",
        note="Added 2026-10-10. This entry previously read NOTHING PAYS THIS LEG and "
        "predicted that the wall would be hit three more times; it was hit on LTC the same "
        "day. The send is base.send_to_address() exactly as that note said, and the wrapper "
        "adds the lookup by deposit address plus the refusals. ONE GUARD pay_icp_deposit.py "
        "DOES NOT NEED: a Bitcoin-family send has no idempotency key, so "
        "refuse_already_paid() asks the DAEMON whether the address was already paid. The "
        "database cannot answer that for the 15s before the watcher writes a row, and two "
        "runs inside that window are two transactions.",
    ),
    "LTC": DepositPayer(
        tool="python3 pay_deposit.py --deposit-address <the address on the page> --apply",
        arming="an unlocked wallet, and a daemon reporting a TEST network",
        note="Same tool and same guards as BTC. WRITTEN FROM A MEASUREMENT ON THE "
        "OPERATOR'S HOST 2026-10-10: a swap wanted 0.06030068 LTC, the desk's testnet wallet "
        "held 0.02, and a hand-rolled send was answered with Insufficient funds (rpc code "
        "-6) only AFTER it had been attempted. pay_deposit.py reads the balance and the "
        "network first. tLTC cannot be minted here -- the daemon reports test rather than "
        "regtest, so fund_testnets.py's generatetoaddress does not apply and "
        "testnet_wallets.py names the faucets instead.",
    ),
    "GRC": DepositPayer(
        tool="",
        arming="GRIDCOIN_WALLET_PASSPHRASE -- the desk's Gridcoin wallet is ENCRYPTED "
        "(HANDOFF.md section 5), so unlike BTC and LTC this leg needs a passphrase before "
        "any send, and the passphrase must never appear in a command this repository emits",
        note="NOTHING PAYS THIS LEG, and it is the one of the three that is NOT a small "
        "change. The send is base.send_to_address like the others, but the wallet is "
        "encrypted, so an unlock has to happen first -- and the operator's standing "
        "instruction is that a passphrase never appears in an emitted command and the panel "
        "never has a field for one. That makes the arming question real rather than "
        "mechanical, and it is the operator's (rule 16).",
    ),
}


class UnknownAsset(KeyError):
    """No registry entry, which means nobody decided how this leg gets paid.

    ITS OWN TYPE because the remedy is specific and is not "handle the error":
    an asset reachable as a swap's from_asset with no entry here is the ICP
    situation repeating, and the fix is to add the entry -- deciding, in the
    process, whether a tool exists.
    """


def payer_for(asset: str) -> DepositPayer:
    """How `asset`'s deposit leg gets paid. Raises `UnknownAsset` if nobody said.

    REFUSES RATHER THAN RETURNING A DEFAULT. A default would be either "payable"
    -- which sends an operator looking for a tool that does not exist -- or
    "unpayable", which would have reported ICP correctly by accident and BTC
    correctly by accident and told nobody that the question had never been asked.
    """
    try:
        return PAYERS[asset.upper()]
    except KeyError as error:
        known = ", ".join(sorted(PAYERS))
        raise UnknownAsset(
            f"no deposit payer is registered for {asset!r}. Known: {known}. An asset that can "
            f"be a swap's from_asset with no entry in swap_terminal/deposit_payers.py is the "
            f"2026-10-10 defect repeating: a swap whose deposit nothing can pay."
        ) from error


def unpayable_assets() -> list[str]:
    """Every registered asset whose deposit leg nothing in the tree can pay.

    Sorted, so a diagnostic printing it is stable run to run (rule 14: pasted
    output has to be self-describing a day later, which includes not reordering).
    """
    return sorted(asset for asset, payer in PAYERS.items() if not payer.payable)


def coverage_lines() -> list[str]:
    """The table, for an operator-facing report. Rule 14: `(none)` is a result.

    Prints the unpayable legs LAST and labeled, because that is the half somebody
    needs before they create a swap they cannot fund.
    """
    lines = ["deposit legs, and what pays each one:"]
    for asset in sorted(PAYERS):
        payer = PAYERS[asset]
        if payer.payable:
            lines.append(f"  {asset:5} {payer.tool}")
    unpayable = unpayable_assets()
    if unpayable:
        lines.append(f"  CANNOT BE PAID: {', '.join(unpayable)}")
        lines.append("    creating a swap from one of these takes a deposit nothing can send")
    else:
        lines.append("  every registered leg has a payer  <- (no gaps)")
    return lines
