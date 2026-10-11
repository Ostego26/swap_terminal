"""The Litecoin adapter: RPCAdapter with asset = "LTC".

Role: submodule (chain binding; adds no behavior, only the asset label)
Reads: a Litecoin wallet daemon, through the methods inherited from
       chains/base.py -- validateaddress, getaddressinfo, getbalance,
       gettransaction, getrawtransaction, decoderawtransaction,
       listtransactions, listreceivedbyaddress, getaddressesbylabel,
       createrawtransaction, fundrawtransaction
Writes: nothing to disk. THE WALLET AND THE CHAIN, through inherited methods:
       get_new_address() derives and stores a key, send_to_address()
       broadcasts.
Can move funds: YES, by inheritance -- send_to_address() calls `sendtoaddress`.
Mainnet-safe: the read methods are; send_to_address() is the payout path.

=============================================================================
RULE 11'S TABLE IS NOT MISSING FROM THIS FILE. IT IS SOMEWHERE ELSE.
=============================================================================

THIS PARAGRAPH USED TO READ: "Rule 11 wants the per-chain facts to live in
these subclasses -- decimals (8 for all three Bitcoin-derived chains), the
confirmation requirement and the dust threshold -- rather than only an asset
string. They are not here yet", and then named the home of ONE of the three.
Every clause was true of THIS FILE and the paragraph misled about the tree,
which is the worse half: it reads as though three facts are nowhere, and it
invites the next reader to "finish rule 11" by writing them in here -- which
would give each one a SECOND home and is rule 8's bug with a delay on it, not
rule 11's one vocabulary. Rule 11 asks for "derived in ONE place, applied
identically everywhere"; it does not say that place is a subclass.

Re-established 2026-10-11 by CALLING each one rather than by reading for it.
chains/gridcoin.py carries the same survey for GRC and the two were written
the same day; a reader who finds one is being told the other exists (rule 8).

  decimals          chains/coin_amounts.CHAIN_DECIMALS["LTC"] is 8, and
                    RPCAdapter.send_to_address() already runs
                    fit_to_chain_precision() over every payout before the
                    `sendtoaddress`. NOT A GAP, and copying the table here
                    would make it a second spelling of a number the payout
                    path already reads (rule 8).
  confirmations     config.Config.LTC_MIN_CONFIRMATIONS is 2, and it reaches
                    the swap row through
                    services/swap_service.get_min_confirmations(), which since
                    2026-10-11 passes it through
                    chains/registry.validate_min_confirmations(). NOT A GAP,
                    and moving a fund-moving threshold to a new home is the
                    operator's call (rule 16).
  dust threshold    modules/htlc_fee.DUST_RELAY_FEE_SAT_PER_KVB, measured
                    against Litecoin Core 0.21.4. It is NOT absent, and a
                    table here would be a second home for it. What IS absent
                    is any READER of it on the brokered payout path, and of
                    the three Bitcoin-derived chains that absence binds first
                    on this one. See below.

SO NOTHING IS BROKEN BY THIS FILE BEING FOUR LINES, and filling it in is the
thing that would break something. What was genuinely absent was a pointer,
which is what the survey above now is -- plus the one real gap, below, which is
in a different file entirely.

=============================================================================
THE DUST FLOOR IS MEASURED IN THIS TREE AND THE PAYOUT PATH DOES NOT READ IT
=============================================================================

modules/htlc_fee.DUST_RELAY_FEE_SAT_PER_KVB carries Litecoin Core 0.21.4's dust
relay rate -- 30,000 sat/kvB, TEN TIMES Bitcoin Core's 3,000 -- measured
2026-09-25, and modules/htlc_fee.dust_threshold_satoshis() applies Core's own
GetDustThreshold arithmetic to it. Computed from those two on 2026-10-11, per
output type, in base units:

    output type   BTC   LTC     the LTC figure as a coin amount, and who pays it
    P2WPKH        294   2940    0.00002940 LTC   a `tltc1q...` payout address
    P2WSH         330   3300    0.00003300 LTC
    P2TR          330   3300    0.00003300 LTC   a `tltc1p...` payout address
    P2SH          540   5400    0.00005400 LTC   a `Q...` or `2...` address
    P2PKH         546   5460    0.00005460 LTC   an `m...`/`n...` address

tests/test_ltc_chain_facts.py pins every figure in that table against the
functions that compute it, so this table cannot drift away from the code the
way the paragraph it replaced did.

AND chains/base.RPCAdapter.send_to_address() CONSULTS NONE OF IT. Its only
amount refusal is `fitted <= 0`. The comment beside that refusal USED to say the
dust limit "depends on the output type, and no part of this tree measures it
yet"; base.py was corrected on 2026-10-11 and now retracts that sentence in
place and carries the same per-output-type table this file does. The quote is
kept here rather than deleted so a reader who met the old wording can see it
resolved -- but base.py no longer says it, and attributing it to that file in
the present tense would be this paragraph committing the error it reports. The
architectural fact is unchanged and is the load-bearing half: there is no dust
floor on the brokered payout path, at quote time or at send time. So the brokered
payout path hands litecoind an amount below the floor and lets the DAEMON
refuse it, which is an error arriving AFTER the customer's deposit is credited
and irreversible.

MEASURED 2026-10-11 by running the real
services/payout_service.process_pending_payouts() over one seeded GRC -> LTC
swap whose output_amount_estimate was 0.00002 and whose payout address was a
`tltc1q...`: 2000 base units reached `sendtoaddress`, 2000 is below the 2940
that row of the table gives, and the swap was written `completed` with a
payouts row at status `broadcast`. Nothing refused it -- not at quote time, not
at send time. (What the daemon then does with it is NOT measured from here: no
litecoind is reachable from this container, so the refusal itself is read off
Core's dust rule rather than off a node -- rule 17.)

AND A REFUSAL HERE WOULD NOT BE ENOUGH ON ITS OWN, which is measured too: a
floor added inside send_to_address() and then run over the same seeded swap
leaves it `failed` with "the swap is now 'failed' and this worker will NOT
retry it" -- deposit credited, nothing sent, nobody paid. Better than paying to
an output no node relays, and still a stranded swap. That is the whole argument
for the gate being at QUOTE time, where nothing has been taken yet, with the
send-time check as the backstop rather than as the answer.

WHY THIS CHAIN AND NOT BITCOIN. The arithmetic is identical on both; the RATE
is not. 2000 is ABOVE Bitcoin's 294 and BELOW Litecoin's 2940, so there is a
band -- 295 to 2939 base units -- in which a payout is deliverable on BTC and
refused on LTC. A figure chosen from a Bitcoin-shaped intuition lands in it,
and every amount in this tree is spelled in whole coins, where the two chains
look the same.

THE GATE IS NOT ADDED HERE, AND NOT ANYWHERE, and that is rule 16 rather than
an omission: a threshold deciding whether a swap may proceed at all is fund
movement and is the operator's. The shape it should take already exists one
chain over, in services/quote_service.require_deliverable_sol_payout(), whose
docstring records the operator's 2026-10-02 instruction "refuse at quote too"
for exactly this reason: a refusal at payout time arrives after the deposit has
been taken. There is no Bitcoin-family analogue of that function. This
paragraph exists so that the next reader of this file is not told the number is
unmeasured, which is what the paragraph before it said.
"""

from .base import RPCAdapter


class LitecoinAdapter(RPCAdapter):
    asset = "LTC"
