"""How many decimals a chain's amount field actually has, and how to fit one.

Role: function level (the bottom of CLAUDE.md rule 10's stack -- pure functions
      and one table; nothing opens a socket)
Reads: nothing. No chain, no file, no environment.
Writes: nothing
Can move funds: no -- but it DECIDES the number a payout sends, so it is on the
      order path and the truncation direction below is the live-money decision
      in this file.
Live-safe: yes.

WHY THIS EXISTS, MEASURED 2026-10-03 ON THE OPERATOR'S REGTEST NODE.

chains/base.RPCAdapter.send_to_address() did `self.call("sendtoaddress", address,
float(amount))`, and a quoted payout is a float with as many decimals as the
arithmetic produced. What that puts on the wire:

    2701.3495803173805   ->  "2701.3495803173805"    13 decimals
    0.00041198765432109  ->  "0.00041198765432109"   17 decimals

and Bitcoin Core parses an amount with ParseFixedPoint(value, 8), which REJECTS
more than eight decimal places rather than rounding. Proven with
createrawtransaction, which runs the same parser and broadcasts nothing:

    0.00041198765432109  ->  error code -3, "Invalid amount"
    4.1e-07              ->  ACCEPTED, built an output of 0x29 = 41 satoshis

So BTC AND LTC AS DESTINATIONS COULD NOT HAVE PAID OUT AT ALL. GRC->BTC and
GRC->LTC are in ALLOWED_PAIRS, the funding gate passes them, and the send would
have failed with "Invalid amount" AFTER the customer's deposit was confirmed and
irreversible -- the same ordering as the morning's insufficient-funds failure.

GRIDCOIN MASKED IT COMPLETELY, which is why every payout so far worked. Its RPC
accepted 2701.3495803173805 and rounded on chain to 2701.34958032, exactly as the
payout transaction the operator pasted shows. Five swaps settled through that
tolerance and none of them exercised the strict parser.

ONE EXPECTATION WAS REFUTED AND IS RECORDED RATHER THAN DROPPED (rule 17). I
expected scientific notation to fail too -- small payouts serialize as "4.1e-07"
-- and Core accepted it and parsed it correctly. So the defect is the decimal
COUNT alone, and no string formatting is needed: a quantized float serializes
within eight decimals or in an exponent form the parser handles.

WHY amount_to_base_units() MOVED HERE RATHER THAN BEING IMPORTED FROM
chains/solana_units.py. It is chain-agnostic arithmetic that already rounds DOWN
for precisely this reason, and already uses Decimal(str(amount)) rather than
Decimal(amount) to avoid truncating a float's exact binary value. Its first
non-Solana caller arriving is rule 8's moment: "the shared version goes where
both callers can reach it." solana_units re-exports it, so nothing that imports
it from there changes.
"""

from __future__ import annotations

from decimal import Decimal

#: Decimal places each chain's RPC amount field accepts, for the chains whose
#: adapter sends a DECIMAL amount.
#:
#: XRP AND SOL ARE DELIBERATELY ABSENT, and their absence is not an oversight.
#: Both convert to integer base units before sending -- chains/xrp.py through
#: to_drops() and chains/solana.py through amount_to_base_units() -- so no
#: decimal string ever reaches those RPCs and there is nothing here for them to
#: get wrong. Solana's figure is also not a per-CHAIN constant at all: an SPL
#: mint carries its own `decimals`, read from the mint account, and a table entry
#: would be a second answer to a question the chain already answers (rule 8).
#:
#: Eight for all three is the COIN constant these daemons are built on -- one
#: satoshi, one litoshi, and Gridcoin's own 1e-8 -- and it is what Core's
#: ParseFixedPoint(value, 8) enforces, measured above.
CHAIN_DECIMALS = {"BTC": 8, "LTC": 8, "GRC": 8}


def amount_to_base_units(amount: float, decimals: int) -> int:
    """Convert a float amount to integer base units, rounding DOWN.

    Rounds down (truncates) rather than to-nearest, and the direction is
    deliberate on a payout path: rounding up would send a fraction of a unit
    more than was quoted, every time, out of the hot wallet. Truncation errs
    toward keeping it.

    Decimal(str(amount)) rather than Decimal(amount), because the second
    converts the float's exact binary value -- Decimal(0.1) is
    0.1000000000000000055511151231257827... -- and truncating THAT is a
    different answer from truncating the decimal number the operator typed.

    MOVED HERE FROM chains/solana_units.py ON 2026-10-03, unchanged, when
    chains/base.py became its second caller. See this module's docstring for why
    the move rather than a cross-import; chains/solana_units.py re-exports it so
    every existing importer is untouched.
    """
    if decimals < 0:
        raise ValueError(f"decimals cannot be negative, got {decimals}")
    return int(Decimal(str(amount)) * (Decimal(10) ** decimals))


def fit_to_chain_precision(amount: float, asset: str) -> tuple[float, str]:
    """`amount` reduced to what this chain's RPC accepts. (amount, "" or what changed).

    THE DECISION THIS FILE EXISTS FOR, and it is on the order path: the returned
    number is what a payout sends.

    DOWN, NEVER NEAREST, inheriting amount_to_base_units()'s direction and its
    reason. The customer is short by at most one unit of the chain's smallest
    denomination -- one satoshi, about $0.0008 at today's BTC price -- and the
    desk never sends more than it quoted. Rounding to nearest would overpay half
    the time, every time, out of the hot wallet.

    AND chains/xrp_units.to_drops() DOES THE OPPOSITE, WHICH IS NAMED HERE
    BECAUSE IT IS A REAL DIFFERENCE RATHER THAN AN OVERSIGHT -- rule 8 asks that
    a genuine difference be recorded at BOTH sites naming the other, the other
    site is to_drops()'s own docstring, and chains/payout_quantization.py is the
    one module that dispatches to both. to_drops() uses ROUND_HALF_UP, so an XRP
    payout can be rounded UP to the next whole drop. Measured 2026-10-03 over
    60,000 random amounts between 1e-9 and 1e7 XRP: 24,850 of them -- 41.4% --
    quantize UPWARD, by at most half a drop (0.0000005 XRP). Four chains in this
    terminal truncate and one rounds half up.

    THAT ASYMMETRY IS NOT RESOLVED HERE, and the reason is rule 16 rather than
    indifference: to_drops() is what chains/xrp.py has always called on every
    payout, so ROUND_HALF_UP is what the ledger has been receiving all along.
    Changing it would change what gets sent, which is live posture and the
    operator's decision. What changed on 2026-10-03 is only that the `payouts`
    row records the figure the chain actually sends, whichever way it rounded.

    AN UNKNOWN ASSET IS RETURNED UNTOUCHED, with a sentence saying so. This table
    covers the chains that send a decimal amount; XRP and SOL are absent because
    they send integers, and silently applying eight decimals to a chain nobody
    listed would be this function guessing a precision. A caller that wants to
    refuse an unlisted asset can read the empty table entry itself.

    THE SECOND RETURN VALUE IS FOR THE LOG, not for a decision. A payout that was
    reduced must say so where an operator reconciling a chain explorer against a
    quote can see it -- the figures differ in the last digit and rule 14's "state
    what the number means" applies hardest where two correct numbers disagree.
    """
    decimals = CHAIN_DECIMALS.get(asset)
    if decimals is None:
        return amount, (f"{asset} is not in CHAIN_DECIMALS, so no precision was applied -- that table "
                        f"covers the chains whose RPC takes a decimal amount, and XRP and SOL send "
                        f"integer base units instead")
    units = amount_to_base_units(amount, decimals)
    fitted = float(Decimal(units) / (Decimal(10) ** decimals))
    if fitted == amount:
        return amount, ""
    return fitted, (f"reduced from {amount!r} to {fitted!r} to fit {asset}'s {decimals} decimal places "
                    f"(down, so the send is never more than the quote; the difference is "
                    f"{Decimal(str(amount)) - Decimal(str(fitted))} {asset})")
