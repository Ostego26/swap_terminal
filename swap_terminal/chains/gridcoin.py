"""The Gridcoin adapter: RPCAdapter with asset = "GRC".

Role: submodule (chain binding; adds no behavior, only the asset label)
Reads: a Gridcoin wallet daemon, through the methods inherited from
       chains/base.py.
Writes: nothing to disk. THE WALLET AND THE CHAIN, through inherited methods.
Can move funds: YES, by inheritance -- send_to_address() calls `sendtoaddress`.
Mainnet-safe: the read methods are; send_to_address() is the payout path.

Gridcoin's wallet may be encrypted, in which case `sendtoaddress` fails until
`walletpassphrase` has been called. THIS ADAPTER STILL DOES NOT UNLOCK THE
WALLET, and that is unchanged and deliberate -- an adapter that unlocked on
every send would unlock on reads nobody audited.

WHAT CHANGED IS THAT THERE IS NOW ONE UNLOCK SEQUENCE ABOVE IT RATHER THAN TWO.
This paragraph used to record a divergence: the payout path unlocked one way and
modules/atomic_grc_client.py another (ensure_fully_unlocked), "noted here rather
than resolved". Both now go through chains/gridcoin_wallet_lock.unlocked_for_payout()
-- services/payout_service.py since 2026-09-26, the HTLC client since 2026-09-30
-- so the sequence, the timeout and the guaranteed return to STAKING are stated
once. A wallet lock is the last place two implementations should exist: the half
that gets forgotten is the restore, and forgetting it leaves an operator's wallet
not staking with nothing printed.

AND THERE IS ONE MORE GRIDCOIN-SPECIFIC THING, AND IT IS NOT IN THIS FILE.
chains/grc_message_signing.py wraps `verifymessage` -- the read-only signature
check behind the address-proof panel (services/grc_login_service.py). It is a
free function taking an adapter rather than a method here, for two reasons worth
knowing before anybody moves it: a method would need a live adapter to test,
where a function takes a stub, and `verifymessage` is a Bitcoin-family method
that BTC and LTC answer identically -- so putting it on the GRC subclass would
be the wrong place for it the moment a second chain wants it. A reader who comes
to this file looking for the signing surface must be told where it is (rule 8),
which is what this paragraph is for. IT DOES NOT UNLOCK THE WALLET EITHER, and
it does not need to: the establishment, read out of Gridcoin's own source, is in
that module's docstring.

AND THERE IS A THIRD CALLER OF THAT SEQUENCE SINCE 2026-10-03, which this
paragraph did not name and which a reader of the GRC ADAPTER most needs to know
about: services/swap_service.derive_deposit_address(). DERIVING AN ADDRESS IS A
WALLET WRITE. `getnewaddress` succeeds while the keypool holds spare keys and
answers rpc -13 once it must top the pool up, so an encrypted wallet unlocked
only for staking creates swaps fine until the keypool empties and then stops,
with a customer on the page. That function tries first and unlocks only if the
daemon says to, so the ordinary case costs no unlock at all. Counted
2026-10-11, the full set of callers that can open this wallet past staking is
four: services/payout_service.payout_unlock_context(), the HTLC client's
wallet_open_for_sending(), services/swap_service.derive_deposit_address(), and
chains/wallet_lock.unlocked_for_payout() -- the chain-dispatching wrapper
atomic_swap.py, atomic_swap_xrp.py and fund_desk.py reach it through. One
sequence, four doors; the sentence above said two.

=========================================================================
RULE 11'S TABLE IS NOT MISSING. IT IS SOMEWHERE ELSE, ON PURPOSE.
=========================================================================

THIS PARAGRAPH USED TO READ: "Rule 11 wants the per-chain facts to live in these
subclasses -- decimals (8 for all three Bitcoin-derived chains), the
confirmation requirement and the dust threshold -- rather than only an asset
string. They are not here yet". That is true of THIS FILE and it misleads about
the tree, which is the worse half: it reads as though three facts are nowhere,
and it invites the next reader to "finish rule 11" by writing them in here --
which would create a SECOND home for each and is rule 8's bug with a delay on
it, not rule 11's one vocabulary. Rule 11's actual demand is "derived in ONE
place, applied identically everywhere", and it does not say that place is a
subclass.

MEASURED 2026-10-11, each fact read back out of the tree rather than recalled:

  decimals       chains/coin_amounts.CHAIN_DECIMALS -> {"BTC": 8, "LTC": 8,
                 "GRC": 8}, with Core's own ParseFixedPoint(value, 8) as the
                 establishment. ONE reader that matters -- RPCAdapter.
                 send_to_address() calls fit_to_chain_precision(amount, asset)
                 on every payout, so GRC inherits it from base.py and a table
                 here would be a value nothing reads.
  confirmations  Config.GRC_MIN_CONFIRMATIONS = 6, through
                 services/swap_service.get_min_confirmations(), which validates
                 it and copies it onto swaps.min_confirmations AT CREATION. The
                 copy is the reason it cannot move here: a swap carries the
                 threshold it was created under, so the authority has to be a
                 config value an operator can change without changing what
                 already-open swaps are held to.
  dust           modules/htlc_fee.DUST_RELAY_FEE_SAT_PER_KVB, and GRIDCOIN'S
                 ENTRY IS ZERO BECAUSE GRIDCOIN HAS NO DUST RULE -- read from
                 its policy source, not assumed. A table here would have to
                 invent a threshold to hold the absence of one.

SO NOTHING IS BROKEN BY THIS FILE BEING FOUR LINES, and the one thing that
would be broken is filling it in. What is genuinely absent is a pointer, which
is what the paragraph above now is (rule 8: a reader who finds one site must be
told where the others are).

=========================================================================
THE 6 THIS CHAIN CREDITS AT IS NOT THE 10 ITS OWN WALLET COUNTS AT
=========================================================================

Named here because this is the file a reader opens to ask what GRC's
confirmation number means, and the measurement lives in config.py where they
will not look. Measured on the operator's live testnet desk 2026-10-05, funding
it with 500 tGRC (txid 32294bc8...27728d) and polling every 20s: at depth 6 this
terminal calls the deposit credited while `getbalance` still reads 0.0 with
500.0 unconfirmed, and the wallet first counts it at depth 10. Gridcoin's
wallet.h:1412 `IsConfirmed()` wants depth 10 and GetBalance() skips a
transaction that is `!IsConfirmed() && !fFromMe` -- and fFromMe is false for a
payment we RECEIVE.

WHAT THAT CAN AND CANNOT DO. The first row is a MEASUREMENT and the other two are
readings of which call each side of the gap feeds, which is a difference worth
marking (rule 17):

  it cannot strand a deposit    MEASURED 2026-10-11 by seeding a GRC -> BTC swap
                                into the real tables and running the real
                                services/deposit_service.process_active_swaps()
                                at depths 0, 1, 5, 6 and 10 against a seeded
                                GridcoinAdapter: deposit_seen at 0, confirming
                                at 1 and 5, payout_pending WITH credited_at at
                                6 and 10 -- and the event carried the real
                                vout 1 at each depth, not the fabricated 0.
                                Detection reads `listtransactions` plus
                                `getrawtransaction`, whose `confirmations` come
                                from the CHAIN and not from the wallet's
                                IsConfirmed, which is why the wallet's own
                                depth-10 rule cannot reach it.
                                tests/test_grc_deposit_credit_path.py holds the
                                vout half; the depth ladder above was a one-off
                                probe and is recorded here rather than kept.
  it cannot release one early   the deposit side of a GRC swap spends nothing
                                from this wallet; the payout is on the other
                                chain.
  it understates CAPACITY       services/payout_capacity.py reads get_balance()
                                -> `getbalance`, so for roughly four Gridcoin
                                blocks after a GRC receipt confirms to depth 6
                                the desk looks poorer than it is and an X -> GRC
                                swap is REFUSED AT CREATION rather than taken
                                and failed. That is a halt and the safe
                                direction, and it costs the customer a retry.

Changing 6 is live posture and the operator's (rule 16); config.py holds the
argument and GRC_MIN_CONFIRMATIONS holds the knob.
"""

from .base import RPCAdapter


class GridcoinAdapter(RPCAdapter):
    asset = "GRC"
