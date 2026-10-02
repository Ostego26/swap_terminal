"""The Solana adapter: an account-model chain behind the same five-method contract.

Role: module (owns the Solana stage of the adapter contract; every decision it
      makes is a function in chains/solana_address.py, chains/solana_units.py,
      chains/solana_signing.py or chains/solana_transaction.py)
Reads: a Solana JSON-RPC endpoint -- getBalance, getSignaturesForAddress,
       getTransaction, getSignatureStatuses, getSlot, getAccountInfo,
       getTokenAccountBalance, getMinimumBalanceForRentExemption, getHealth,
       getGenesisHash, getLatestBlockhash
Writes: THE SOLANA CHAIN, through sendTransaction, and only from
       send_to_address() when it is armed with the exact token
       chains/solana_signing.CONFIRM_SOL_SEND and only on devnet. No file and
       no database.
Can move funds: YES WHEN ARMED, and that sentence replaced "NO, AND THAT IS
       THE DELIBERATE HALF OF THIS FILE" on 2026-10-02 at the operator's
       instruction ("we should be able to swap any coin for another of any
       combination"). What is unchanged is that THIS MODULE HOLDS NO KEYPAIR
       AND READS NO KEY PATH: the secret's entire lifetime is one call to
       chains/solana_signing.signed_transfer_wire(), which returns bytes, so
       this file broadcasts a transaction it could not have produced.
       tests/test_solana_adapter.py::test_the_module_references_no_keypair_
       anywhere tokenizes this file and still refuses the names Keypair,
       secret_key, from_secret_key, sign, sign_message and partial_sign.
       get_new_address() still refuses unconditionally, for a reason that is
       about the deposit strategy rather than about custody.
Mainnet-safe: yes, BY REFUSAL rather than by absence. Every read is a read, and
       the one write refuses any cluster whose genesis hash is not devnet's --
       mainnet-beta, testnet, an unrecognized hash and a missing one all raise,
       in the preview as well as in the send. There is no flag, environment
       variable or argument that turns that off.

=============================================================================
WHAT THE CONTRACT ACTUALLY IS -- MEASURED, NOT ASSUMED
=============================================================================

chains/base.RPCAdapter has a wide surface built around Bitcoin's UTXO model:
call(), get_confirmations(), _raw_tx_for_vouts(), _extract_matching_vouts().
**None of that is the contract.** The contract is what the callers use, and it
was measured (2026-09-25) by walking the AST of every module in services/,
workers/, routes/ and app.py and collecting every attribute accessed on an
adapter object:

    get_new_address(label) -> str                 services/swap_service.py:58
    validate_address(address) -> bool             services/swap_service.py:55
    get_balance() -> float                        services/payout_service.py:267
    send_to_address(address, amount) -> str       services/payout_service.py:219
    find_deposits_to_address(address) -> list     services/deposit_service.py:143

Five methods, one call site each. (`.items()` also appeared, on the adapterS
dict, not on an adapter.) `get_confirmations` is NOT in the contract: it is
base.py's own helper, and confirmations reach the caller INSIDE the dicts
find_deposits_to_address returns. That is the whole reason a Solana adapter can
satisfy this interface without inheriting a UTXO model -- so this class does
not subclass RPCAdapter, and says so here rather than leaving a reader to
wonder whether that was an oversight.

The event dict shape, also measured, from services/deposit_service.py:
upsert_deposit_event() and refresh_swap_from_chain() read exactly
`txid`, `vout`, `address`, `amount` and `confirmations`, and the gate is
`int(row["confirmations"]) >= int(swap["min_confirmations"])`.

=============================================================================
WHAT `vout` MEANS HERE, AND WHY IT IS NOT A FABRICATION
=============================================================================

Solana has no transaction outputs. But `deposit_events` has
UNIQUE(asset, txid, vout) and refresh_swap_from_chain() sums every row, so
`vout` has to be a stable, transaction-local integer that does not collide.

**IT IS THE MEMO TAG, AND THIS SECTION SAID THE ACCOUNT INDEX UNTIL 2026-10-02.**
That was true when it was written and stopped being true when the shared-account
strategy landed: `_attributable()` now stamps each credit with the transaction's
memo tag and DROPS a credit that has none, precisely because the account index
"was never a discriminator and must not be reused as one" -- its own words, one
screen down. A reader who trusted this paragraph would have concluded that
services/deposit_service.attributable_events(), which compares `vout` against the
swap's deposit_tag, was comparing a tag against an account index; on a chain where
that integer decides whose money a credit is, that is the worst sentence in this
file to have wrong.

So: `vout` carries the Memo-instruction tag. It is read from the transaction, it
is the only thing that says which swap a credit belongs to now that every SOL swap
shares one deposit account, and a transaction credits an account exactly once --
the account model has one net balance delta per account per transaction, so one
row per (signature, address) is the correct count rather than a convenient one.

THIS IS SPECIFICALLY NOT chains/base.py's vout=0. That value was FABRICATED by
an except handler when output decoding failed, it carried an amount from the
wallet's summary rather than from the chain, and it caused the double-counting
artifact that migrate_deposit_vouts.py exists to clean up. Nothing here
invents a row: if a transaction cannot be decoded, this adapter RAISES, and
the reasoning for that difference is at find_deposits_to_address().

=============================================================================
ITS RELATIONSHIP TO THE NODE BRIDGE (CLAUDE.md rule 8)
=============================================================================

`grc-sol-swap/abstergo_exchange/server.js` already pays out on Solana, and a
reader who finds one of these must be told the other exists. They are not
duplicates and the difference is real:

    server.js                        this adapter
    ------------------------------   --------------------------------------
    GRC deposit -> SOL payout, one   the Flask app's generic adapter
    direction                        contract, both directions
    native SOL only                  native SOL or an SPL token, from the
    (SystemProgram.transfer)         start -- the operator holds wGRC, which
                                     is an SPL token and not native SOL
    SIGNS AND BROADCASTS, from       SIGNS AND BROADCASTS TOO, since
    SOLANA_PAYER_KEYPAIR_PATH,       2026-10-02 -- but DEVNET ONLY, refusing by
    on whatever cluster              default, armed by an exact string at the
    DEVNET_RPC_URL points at         call site, and from a keypair read by
    (a variable NAME, not a          chains/solana_signing.py and never by this
    guarantee -- its own header      module. The cluster is identified by its
    says so)                         GENESIS HASH, which is the check the Node
                                     side does not have.
    swap_intents.json is its         swap_terminal.db, via the services the
    authority                        contract is called from
    needs no Solana DEPOSIT          get_new_address() is a Solana DEPOSIT
    address -- deposits are GRC      address, which Solana has no
                                     `getnewaddress` for. See below.

server.js's payout path was read before this was written rather than
reinvented: its `quoteGrcToSol` floors to integer lamports and refuses a quote
that rounds to zero, and `sendSolPayout` builds a one-instruction
SystemProgram.transfer. Both behaviors are reflected here --
amount_to_base_units() truncates rather than rounds, and the transfer plan is
one instruction -- and where this file goes further (SPL, rent, ATA) it is
because the Node bridge never had to.

=============================================================================
WHAT IS IMPLEMENTED AND WHAT IS A PROPOSAL (CLAUDE.md rule 16)
=============================================================================

  IMPLEMENTED, READ-ONLY     validate_address, get_balance,
                             find_deposits_to_address, commitment reporting,
                             rent lookup, mint decimals, token program
                             detection, ATA derivation, health/announce.
  REFUSES, BY DESIGN         get_new_address  -- NOT because the strategy is
                             unchosen (it was chosen 2026-09-29: one shared
                             account plus a per-swap Memo instruction) but
                             BECAUSE of that choice. A shared account has no
                             per-swap address to derive, so callers go through
                             services/swap_service.deposit_account(). See the
                             method.
  PREVIEWS, THEN SENDS WHEN   send_to_address -- which refused
  ARMED                       unconditionally until 2026-10-02 and now previews
                              by default. build_transfer_plan() still builds
                              and DESCRIBES the transfer without a key;
                              preview_payout() adds the cluster, the payer, the
                              destination's existence, the rent floor and the
                              balance arithmetic; and the signature happens in
                              chains/solana_signing.py, which this module hands
                              a plan and gets bytes back from. THE BROADCAST
                              HAS NEVER RUN AGAINST A CLUSTER -- see the
                              PROPOSAL note at the end of this header.

**PART OF THIS FILE HAS NOW MET A REAL CLUSTER, AND THE LINE BETWEEN THE TWO
HALVES IS DRAWN BY METHOD.** This header said "NOTHING IN THIS FILE HAS BEEN
EXERCISED AGAINST A SOLANA CLUSTER" until 2026-09-30, which was true when it
was written -- measured 2026-09-25, api.devnet.solana.com and release.anza.xyz
both returned 403 from the development environment's proxy and
`solana-test-validator` could not be installed, its only distribution channels
being those two hosts. Every method name, parameter shape and response field
below was therefore written from Solana's JSON-RPC documentation and from the
shapes server.js already relies on.

The operator ran `solana_chain_check.py` against devnet on 2026-09-30 and
pasted the output back. What that run touched, method by method, because "the
adapter works" is not a thing a run proves and the list is what a reader needs:

  EXERCISED, devnet, solana-core 4.3.0
      getHealth, getVersion, getGenesisHash, getSlot, getEpochInfo
      getMinimumBalanceForRentExemption -- 650240 for 0 bytes and 1488440 for
          165, both matching the 5080 lamports/byte reference
      getSignaturesForAddress -- 50 entries returned for a program account
      getTransaction -- with jsonParsed and maxSupportedTransactionVersion 0,
          ten read, `parsed` present, memo instructions found in all ten

  EXERCISED on the 2026-09-30 run that first read an address
      getBalance -- 28.7786992 SOL returned at BALANCE_COMMITMENT for a real
          devnet account, so the value unwrapping is right

  ATTEMPTED AND FAILED, WHICH IS THE MOST USEFUL RESULT THIS FILE HAS PRODUCED
      find_deposits_to_address -- answered
          `-32602 Method does not support commitment below `confirmed``
          on its first call against a real cluster. DISCOVERY_COMMITMENT was
          `processed`, which getSignaturesForAddress rejects, so SOL deposit
          discovery had never worked and could not have. Fixed by raising the
          constant to the floor the cluster enforces; see
          chains/solana_units.LOWEST_COMMITMENT_THE_HISTORY_METHODS_ACCEPT.
          CONFIRMED by every run since (2026-10-01): discovery lists and
          fetches, and the three lines below depend on it having worked.

  EXERCISED 2026-10-01, over eight devnet runs the operator pasted back
      getTransaction on the DEPOSIT path -- at DISCOVERY_COMMITMENT, which the
          2026-09-30 list had as unexercised because discovery failed before
          reaching it. Five of five listed signatures fetched.
      _spl_credits -- PROVEN. It decoded an amount off a real devnet response
          (`uiTokenAmount.decimals` and `.amount`) and `_attributable` then
          refused the credit for carrying no memo, which is correct and is
          still a proof: the decode happens BEFORE the memo check. These are
          the field names that would lose an SPL deposit silently, and they are
          the ones this header said were the half that matters.
      _native_credits -- PROVEN, on the `--mint`-less run that closed this.
          `(0 credited, 1 refused over 8 signature(s))`: it decoded a native
          credit off `meta.preBalances`/`postBalances` indexed through
          `message.accountKeys`, and `_attributable` refused it for carrying no
          memo. BOTH credit readers now rest on a real response.
          AND THE ARITHMETIC IS CONFIRMED IN LAMPORTS, which the run itself
          could not show. Transaction 2K2Pw1Hz... read directly off devnet,
          2026-10-01:

              account index   1
              preBalances[1]  0
              postBalances[1] 28778699200
              delta           28.7786992 SOL

          So `post - pre` is what the reader returns, the `vout` it would record
          is the account index 1, and base_units_to_amount() divides by
          SOL_DECIMALS correctly. This was worth reading because the run printed
          `28.7786992 SOL` for BOTH the dropped credit and the account's whole
          balance, four lines apart, and a delta equal to the balance is the
          signature of a funding transaction OR of a reader returning the
          balance by mistake. It is the first: pre was zero. The equality was a
          READING until this read made it a measurement (rule 17), and the
          numbers are here so the next person can re-derive it rather than
          re-wonder.
          THIS LINE SAID "NOT re-confirmed since the targeted-proof work
          landed" for exactly one commit, which is the shortest-lived stale
          claim in this header's three revisions and still a stale claim. The
          caveat was correct when written and a run settled it ten minutes
          later; the only reason the fix was two lines rather than a rewrite is
          that tests/test_solana_adapter.py pins the SPLIT -- each reader named
          on exactly one side -- instead of the contents of either list.
      getAccountInfo's owner-program and decimals reads -- Tokenkeg... and
          decimals=9, read off the WSOL mint.
      getTokenAccountBalance -- 103.032164467 WSOL on one run, 0.0 on others.
      ATA derivation -- checked against the cluster for existence, and the
          holders found all had NO associated token account, so their balance
          reads 0.0 while postTokenBalances still credits them. That is the
          non-canonical-token-account case, measured rather than assumed.

  STILL UNEXERCISED, OR ABSENT
      getTokenAccountsByOwner -- still needs a run that reaches it.
      getTokenLargestAccounts -- throttled HTTP 429 on EIGHT consecutive runs
          against public devnet, every one after three attempts. That is why
          solana_chain_check.find_a_holder() has a second route through the
          mint's own traffic; the measurement is the argument for it.
      get_new_address refuses by design and always will -- the shared-account
          deposit strategy has no per-swap address to derive.
      send_to_address, getLatestBlockhash and sendTransaction. **THE PAYOUT
          PATH HAS NEVER TOUCHED A CLUSTER.** Every guard on it is tested
          against seeded responses and the serialization is cross-checked
          against @solana/web3.js byte for byte (chains/solana_transaction.py's
          header has the numbers), and NONE of that is a broadcast:
          api.devnet.solana.com answers 403 at this container's proxy,
          re-measured 2026-10-02. So the send is a PROPOSAL under rule 16 and
          the first real one is the operator's run.

=============================================================================
IS SOL DONE? NO, AND THE THREE REASONS ARE NOT THE SAME KIND OF THING
=============================================================================

Asked directly on 2026-10-01, and worth answering here rather than in a chat
log that nobody reading this file will ever see.

  the READ half      DONE and proven against devnet. Transport, balance,
                     discovery, deposit-path getTransaction, both credit
                     readers, mint decimals, token program, ATA. The list
                     above is what each one rests on.
  the SEND half      BUILT 2026-10-02, DEVNET ONLY, AND UNEXERCISED. This
                     paragraph said "DOES NOT EXIST, by design and by absence"
                     and that was true until the operator asked for it. What
                     exists now: preview_payout() applies seven guards in
                     order, send_to_address() refuses every caller that does
                     not pass CONFIRM_SOL_SEND exactly, the cluster is
                     identified by genesis hash, the keypair is read from
                     SOL_PAYOUT_KEYPAIR_PATH by chains/solana_signing.py and
                     never by this module, the transaction is serialized by
                     chains/solana_transaction.py and parsed back out of its
                     own bytes before broadcast, and the settlement is asserted
                     rather than assumed. What does NOT exist is a run: no
                     transaction built here has reached any cluster. Rule 16's
                     line has moved rather than gone -- the first broadcast,
                     mainnet, and flipping can_spend are all still the
                     operator's.
  the WIRING         ALLOWED_PAIRS contains ZERO entries with SOL on either
                     side (counted 2026-10-01), so no SOL swap can be created
                     today whatever the adapter can do. The deposit side IS
                     wired -- services/swap_service.TAG_ATTRIBUTED_ASSETS
                     holds SOL and TAG_ATTRIBUTION maps it to
                     SOL_DEPOSIT_ACCOUNT with "Memo instruction" as the
                     discriminator -- and enabling a pair is a live-posture
                     decision, so it stays the operator's.

One more gap that is neither, and it is the one that touches money: a credit
this adapter reads and REFUSES (no memo) is reported to the operator by
solana_chain_check.py and logged at WARNING on the live path, and NOTHING ELSE
READS IT. Measured 2026-10-01: `unattributable_drops` is written here and
appears nowhere else under swap_terminal/ -- the only consumer in the tree is
the diagnostic. Eight devnet runs found between one and three such credits
each. On devnet they are nobody's; on mainnet each one is a deposit a human has
to match by hand, and the only thing that would tell them is a log line.

BOTH HALVES ARE CLOSED AS OF 2026-10-01. A deposit with no memo is recorded by
`services/deposit_service.record_what_nobody_can_claim()` off the adapter's own
drop list; a deposit whose memo matches no swap that will ever credit it is
recorded by `reconcile_shared_accounts()`, once per cycle, because the question
"does ANY swap claim this" cannot be answered by a refresh handed one swap. Both
land in `unattributable_deposits`. Nothing is credited and no swap moves.

The paragraph below is kept because the reasoning is what the schema forced,
and a reader reaching for the obvious answer should find out why it was not
taken rather than find it half-built.

AND IT IS NOT A WIRING JOB, WHICH IS THE PART I HAD WRONG. "Route it into
`under_review`" was the obvious next step and it cannot be done, for a reason
that is in the schema rather than in anyone's judgment:

    deposit_events.swap_id   TEXT NOT NULL, FOREIGN KEY -> swaps(id)   db.py:130
    under_review             a status on SWAPS, not on deposits

An unattributable deposit belongs to NO swap -- that is the definition of
unattributable. So there is no row it can be written as and no swap to move
into `under_review`, and nothing in this tree has a concept for a deposit with
no swap. Recording one needs a new place to put it: a nullable swap_id (widens
every existing reader's assumption), a separate orphan table (one more thing to
reconcile), or an operator-facing report generated from the chain on demand
(nothing stored, nothing to drift). Which of those is right is not knowable
from the tree -- it decides what an operator is asked to action -- so it stays
the operator's (rule 20 draws the line at knowable, and this is the side of it
where you ask).

So the transport is proven, the credit path is proven, one real defect on the
deposit path was found and fixed, and the chain still cannot send. The
instrument is the same one throughout: `solana_chain_check.py`, which needs no
arguments, plus `--mint` for the SPL half and `--find-holder` when the
endpoint will not say who holds the token. Read-only, one pasteable block,
every step announced before it runs, and a non-zero exit when any step's
response does not have the shape this file expects -- which is exactly how the
-32602 surfaced.
"""

from __future__ import annotations

import json
import logging
import time
from typing import NamedTuple

import requests
from microfortnights import format_duration

from .solana_address import (
    TOKEN_PROGRAM_ID,
    SolanaAddressError,
    associated_token_address,
    describe_address,
    is_on_curve,
    is_valid_address,
)
from .solana_memo import deposit_tag_from
from .solana_signing import (
    SolanaSendNotArmed,
    SolanaSplSendRefused,
    keypair_path_from_environment,
    require_destination_rent,
    require_devnet,
    require_lamport_headroom,
    require_send_confirmation,
    signed_transfer_wire,
)
from .solana_units import (
    BALANCE_COMMITMENT,
    DISCOVERY_COMMITMENT,
    FINALIZED_RANK,
    SOL_DECIMALS,
    SYSTEM_ACCOUNT_SPACE,
    TOKEN_ACCOUNT_SPACE,
    amount_to_base_units,
    base_units_to_amount,
    commitment_rank,
    describe_commitment,
    float_is_exact_for,
    transfer_fee_lamports,
    validate_min_commitment_rank,
)


class UnattributableCredit(NamedTuple):
    """A real credit that arrived and could not be matched to a swap. Money, not an error.

    WHY THIS EXISTS AS A RETURNED VALUE AND NOT ONLY A LOG LINE. Until 2026-09-30 a dropped
    credit was logged at WARNING and nothing else, so `find_deposits_to_address` returned `[]`
    for two situations a caller must never confuse:

        nothing arrived                      normal, and the overwhelmingly common case
        something arrived, unattributable    real money sitting in the shared account that
                                             a human has to match by hand

    The operator's 2026-09-30 devnet run is what made that concrete: a real credit was read,
    correctly refused for carrying no memo, and the check then printed
    "(none)  <- zero credits in the signatures read" -- a sentence its own log line four lines
    above contradicted. CLAUDE.md rule 5 says a measurement that only exists in a log is not
    learning, and rule 14 says "did nothing" must not look like "did work"; this is both, on the
    path where the difference is whether somebody's deposit is stranded.

    THE RETURN VALUE OF find_deposits_to_address IS UNCHANGED. This is recorded alongside it, so
    the five-method contract services/ and workers/ read is untouched -- a caller that wants the
    drops asks for them.

    IT CARRIES THE AMOUNT SINCE 2026-10-01, AND NOT CARRYING IT WAS THE DEFECT. `credits` is a
    COUNT -- `len(credits)` at the call site -- and that was every number this type held. So the
    thing built to say "real money arrived that nobody can claim" could not say HOW MUCH, and
    every consumer inherited that: the diagnostic prints "1 credit(s) dropped" and a durable
    record would have stored a row meaning "something arrived". A record of money that omits the
    amount is not a record of money.

    REQUIRED RATHER THAN DEFAULTED, deliberately. A default of 0.0 would let a future call site
    forget the amount and write a row saying a zero-value deposit is stranded, which reads as
    "nothing to chase" -- the one conclusion that must not be reachable by omission (rule 19: a
    default that makes a check pass is not a fix).

    `amount` IS THE SUM over the dropped credits and `credits` stays the count of them, because
    a transaction can carry more than one credit to the same account and a human matching it by
    hand needs both: what arrived, and in how many pieces.
    """

    signature: str
    credits: int
    why: str
    #: Total of the dropped credits, in whole units of the asset -- NOT lamports or base units.
    #: Same scale as the `amount` key in the event dicts find_deposits_to_address returns, so a
    #: reader comparing a stranded row against a credited one is comparing like with like.
    amount: float
    #: The account the credits landed in: the shared deposit account. Carried rather than
    #: assumed, so a record says where the money is without the reader reconstructing it from
    #: configuration that may have changed since.
    address: str


class SolanaRPCError(Exception):
    """A Solana JSON-RPC call failed, or the cluster returned an error object.

    Raised rather than swallowed, for the same reason chains/base.RPCError is:
    on this path a failure that returns a plausible value -- False, 0, an empty
    list -- is worse than one that stops the caller, because the caller cannot
    tell it from a real answer. CLAUDE.md rule 12's BLE001 note, in the form it
    takes on a money path: "except Exception: return 0 around a confirmation
    count reads to the caller as 'zero confirmations'."

    A separate class from RPCError rather than a shared one: the two adapters
    share no code and no connection model, and a caller catching one should not
    silently catch the other.

    IT CARRIES THE HTTP STATUS, AND `throttled` IS THE ONE A CALLER ACTUALLY BRANCHES ON.
    Added 2026-09-30 after an operator's `--hunt-memo 50` run: every read after the tenth
    answered HTTP 429, the hunt reported each one as "could not be read", and the two
    program ids came out the other side looking identical -- one of them CONFIRMED off ten
    good reads, the other with nothing read at all. "The endpoint refused to answer" and
    "this transaction cannot be parsed" are different findings and only the second is about
    our code, which is CLAUDE.md rule 12's BLE001 complaint one level up: a handler that
    hands the caller a value it cannot tell from a real answer.

    THE STATUS IS AN ATTRIBUTE AND NOT A STRING TO GREP. The message already contains
    "HTTP 429", and a caller sniffing for that substring would be parsing prose -- which
    breaks the day the wording changes, silently, in the direction of "nothing is throttled
    any more". `status_code` is set at the raise site where the response object is in hand.
    It is None for a failure with no HTTP status: a connection error, or a JSON-RPC error
    object returned inside a 200.
    """

    def __init__(self, message: str, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code

    @property
    def throttled(self) -> bool:
        """Was this the endpoint rate-limiting us, rather than a real answer about the data?

        429 only. A 503 is a node that is unwell and a 500 is one that broke, and neither is
        fixed by waiting a moment and asking again in the way a 429 is -- so neither gets to
        borrow the retry that this enables.
        """
        return self.status_code == HTTP_TOO_MANY_REQUESTS


#: HTTP 429. Named because `status_code == 429` is a magic number to PLR2004 and, more to the
#: point, because a reader meeting `== 429` has to know the protocol to know what it means.
HTTP_TOO_MANY_REQUESTS = 429


# How many signatures to ask for in one getSignaturesForAddress page. Solana's
# own cap is 1000. 500 matches chains/base.py's listtransactions default so the
# two adapters agree about how far back "recent" reaches.
DEFAULT_SIGNATURE_LIMIT = 500


# --- the payout path's own settings (CLAUDE.md rule 6 governs how they PRINT) -

#: SECONDS to keep asking whether a broadcast transaction has settled, and
#: SECONDS between asks. Named in seconds because they are a `time.monotonic()`
#: difference and a `time.sleep()` argument -- rule 6 is explicit that seconds
#: stay where an interface demands them and the conversion happens at the
#: print, which is what format_duration() does on every line below.
#:
#: 90s is bounded by the thing that actually expires: a recent blockhash is
#: valid for 150 slots, which is roughly 60-90 seconds of wall clock, so a
#: transaction that has not been seen by then will never land with THAT
#: blockhash. Waiting longer would be waiting for something that cannot
#: happen; waiting less would report "we do not know" about a transaction that
#: was still perfectly alive.
CONFIRMATION_DEADLINE_SECONDS = 90.0
CONFIRMATION_POLL_SECONDS = 2.0

#: Preflight is left ON, and that is the conservative choice rather than the
#: convenient one. skipPreflight=True sends the transaction without simulating
#: it, which is what a high-throughput trader wants and is exactly wrong here:
#: preflight is where an InsufficientFundsForRent, an expired blockhash, a
#: wrong signer or a malformed instruction is caught BEFORE the transaction is
#: forwarded to a leader. A payout refused by simulation costs a retry; one
#: forwarded and then rejected costs a credited deposit sitting against a
#: failed swap.
SKIP_PREFLIGHT = False

#: The commitment the preflight simulation runs at, AND the commitment the
#: blockhash is fetched at. THE SAME ONE, deliberately: a blockhash read at a
#: looser commitment than the preflight check is a blockhash the simulating
#: node may not know yet, which answers "Blockhash not found" for a
#: transaction that is perfectly good. BALANCE_COMMITMENT is `finalized`,
#: which is also the only rung whose answer cannot be rolled back.
BROADCAST_COMMITMENT = BALANCE_COMMITMENT


#: The last skip set reported IN FULL, per deposit address. Module level because the
#: question it answers is "has this changed since I last told you", which is per
#: PROCESS rather than per adapter instance -- the adapter is rebuilt every cycle, so
#: a per-instance cache would report in full every time, which is the behavior being
#: fixed. Same shape and same reason as payout_service._REPORTED_INVENTORY_FAILURES.
_REPORTED_SKIP_SETS: dict[str, frozenset] = {}


def skip_detail(address: str, skipped: list) -> str:
    """The signature list, in full when it is NEWS and as a count when it is not.

    MEASURED ON THE OPERATOR'S HOST 2026-10-02, hours after the line was added.
    Naming every skipped signature was right when there were two; at nine it is an
    800-character log line, and the scans arrived 527ms apart:

        06:50:33,619  ... did not re-read 9 of them: <9 full signatures>
        06:50:34,147  ... did not re-read 9 of them: <the same 9>
        06:50:34,674  ... did not re-read 9 of them: <the same 9>
        06:50:35,201  ... did not re-read 9 of them: <the same 9>

    The list is also UNBOUNDED: it is every settled or refused transaction the shared
    deposit account has ever had, so it grows for the life of the account and the line
    grows with it.

    THIS IS NOT A WALK-BACK OF RULE 14, it is that rule applied to a line that had
    become unreadable. "Say what you are doing while you do it" is defeated by output
    nobody can scan -- the flood buries the WARNINGs it sits between, which is exactly
    the "a log that cries wolf on the happy path is a log nobody reads the day
    something real happens" failure already recorded in
    unattributable_deposit_service.unclaimed_events(). A line that cannot be read is
    its own kind of silence.

    SO THE COUNT IS ALWAYS THERE and only the DETAIL is conditional. The count is what
    the operator checks (skipped=9 against the outstanding rows show_unattributable.py
    prints); the signatures matter the first time and when the set CHANGES, because a
    change is a new settlement or a new refusal and that is news. An unchanged set is
    the normal path and says nothing the count does not.

    THE EMPTY CASE IS STILL EXPLICIT AND NEVER CACHED. `(none)` is a result, and
    skipped=0 against a non-empty unattributable_deposits table is the 429 leak
    returning, so it is reported on every scan rather than suppressed as unchanged.
    """
    current = frozenset(skipped)
    if not current:
        return " Nothing was skipped: (none)."
    previous = _REPORTED_SKIP_SETS.get(address)
    if previous == current:
        return (
            f" The same {len(current)} signature(s) as the previous scan for this "
            f"account; they were named in full earlier in this log."
        )
    _REPORTED_SKIP_SETS[address] = current
    # THE DIFFERENCE, not the whole set, once a baseline exists. What the reader needs
    # on a change is which signature is new -- reprinting the nine they have already
    # seen to show them the tenth is the flood again, one line later.
    added = sorted(current - previous) if previous else sorted(current)
    label = "now also skipping" if previous else "skipping"
    return f" {label}: {', '.join(added)}."


def assert_amount_fits_a_float(signature: str, address: str, base_units: int) -> None:
    """Refuse a credit too large to survive the application's float columns.

    Split from deposit_event() so that the check is one named thing rather than
    a branch inside a constructor, and because it is the only line in the
    credit path that can REFUSE. services/ and db.py carry swap amounts as
    floats (REAL columns), so a base-unit count above 2^53 silently loses its
    last digits somewhere downstream. Reporting a number that is already wrong
    is worse than stopping: CLAUDE.md rule 12's "the caller cannot tell the
    failure from a real answer", arriving as rounding rather than as an
    exception.

    2^53 lamports is about 9.0 million SOL, far above anything this terminal
    quotes -- but a token with nine decimals and a large supply reaches it, and
    wGRC's supply is not something this code has ever read.
    """
    if not float_is_exact_for(base_units):
        raise SolanaRPCError(
            f"transaction {signature} credits {base_units} base units to {address}, which exceeds 2^53 and "
            "cannot pass through this application's float amount columns without losing a unit. Refusing to "
            "report a number that is already wrong."
        )


#: An arriving deposit that cannot be attributed is REAL MONEY and must not vanish silently.
#: services/deposit_service.attributable_events() states the standard -- "an uncredited deposit
#: is a support ticket, a misattributed one is somebody else's money" -- and a support ticket
#: somebody has to be able to open. WARNING and not INFO: nothing is wrong with the code, and
#: something is wrong for a customer.
logger = logging.getLogger(__name__)


def deposit_event(signature: str, account_index: int, address: str, amount: float, rank: int) -> dict:
    """One deposit event, in the five keys services/deposit_service.py reads.

    A module-level function rather than a method because it is a pure mapping
    from five values to a dict and holds no adapter state -- CLAUDE.md rule
    10's bottom layer, where a test can call it directly.

    `vout` IS THE MEMO TAG SINCE 2026-09-29, NOT THE ACCOUNT INDEX, and the change is the whole
    of Solana's deposit model rather than a detail. The operator chose the one-account-plus-memo
    strategy (README.md, "Solana deposit addresses"), so every SOL swap shares ONE deposit
    account and the address no longer identifies the swap. `vout` is what does:
    services/deposit_service.attributable_events() matches `event["vout"]` against the swap's
    own `deposit_tag`, and chains/xrp_payments.py already puts a DestinationTag there for the
    same reason -- "`vout` is the integer discriminator in the adapter contract". One contract,
    two chains, rather than a second mechanism (rule 8).

    THE ACCOUNT INDEX WAS THERE FOR UNIQUENESS, and the memo serves that too. deposit_events
    has UNIQUE(asset, txid, vout); Solana's account model gives one net balance delta per
    account per transaction, so a deposit is one row either way. What the index could NOT do is
    say whose money it is.

    IT IS STILL NEVER INVENTED, which is the property the old comment was protecting:
    chains/base.py's fabricated vout=0 is the artifact migrate_deposit_vouts.py exists to clean
    up. A transaction with no readable memo produces NO EVENT here rather than an event with a
    guessed discriminator -- see _attributable_credit below.

    `confirmations` carries a COMMITMENT RANK. The key name is
    deposit_service's, not this adapter's; renaming the column would be a
    migration on a live database for a clarity gain, so the rank is documented
    at every site it passes through instead.
    """
    return {
        "txid": signature,
        "vout": int(account_index),
        "address": address,
        "amount": amount,
        "confirmations": int(rank),
    }


class SolanaAdapter:
    """Solana behind the five-method adapter contract. Reads only.

    NOT a subclass of chains/base.RPCAdapter, and the docstring above says why:
    RPCAdapter's shape is Bitcoin's UTXO JSON-RPC (user/password/host/port,
    vouts, a block-count confirmation), and inheriting it would mean inheriting
    methods that cannot mean anything here. The contract is satisfied by
    implementing the five methods the callers use, which is what an interface
    is.
    """

    asset = "SOL"

    # Per-chain facts, in the subclass, which is what CLAUDE.md rule 11 asks the
    # chains/*.py subclasses to carry and which the three Bitcoin-derived ones
    # still do not. Decimals here is NATIVE SOL's; an SPL mint carries its own
    # and mint_decimals() reads it from the chain rather than assuming.
    decimals = SOL_DECIMALS

    # SEE chains/base.RPCAdapter.can_spend. STILL False on 2026-10-02, and the
    # reason changed underneath it: send_to_address() can now sign and broadcast
    # when it is armed, so this is no longer the absence it used to report. It
    # stays False because what it gates is LIVE POSTURE -- chains/registry.
    # payout_refusal() reads it to tell an operator which pairs cannot be
    # serviced, and Config.ALLOWED_PAIRS names no pair that pays out in SOL.
    # Flipping it would make the payout worker try to pay SOL swaps with a path
    # that has never reached a cluster, and that is the operator's call (rule
    # 16: fund movement comes back), not a consequence of this file compiling.
    can_spend = False
    payout_refusal = (
        "has a payout path as of 2026-10-02, and it is NOT enabled: devnet only, refusing "
        "unless armed with chains/solana_signing.CONFIRM_SOL_SEND at the call site, and never "
        "broadcast against any cluster from the environment it was written in. The payout "
        "worker calls send_to_address() with two positional arguments, so it gets a preview and "
        "a refusal. Enabling it is live posture and is the operator's (rule 16)."
    )

    def __init__(  # noqa: PLR0913, PLR0917 -- checked: these six ARE the connection, exactly as RPCAdapter's six are. They arrive as **Config.RPC["SOL"], a dict built for this signature, so bundling them into an object would add a type without removing a parameter.
        self,
        url: str = "",
        commitment: str = DISCOVERY_COMMITMENT,
        timeout: float = 30.0,
        mint: str = "",
        hot_wallet: str = "",
        min_commitment_rank: int = FINALIZED_RANK,
    ):
        """Store the endpoint. Opens no socket (the same promise RPCAdapter makes).

        workers/common.py's header states that build_adapters() "opens no
        socket; RPCAdapter.__init__ only stores credentials and computes a
        URL", and three workers rely on that being true at import. So this
        constructor validates and stores, and nothing more.

        THE TWO VALIDATIONS IT DOES DO ARE BOTH REFUSALS OF A SILENT FAILURE:

        `mint` and `hot_wallet`, when set, must be syntactically valid
        addresses. A typo in either would otherwise surface much later as an
        RPC error inside a poll loop, attributed to the wrong thing.

        `min_commitment_rank` must be a rung on the commitment ladder. An
        operator who copies Gridcoin's 6 gets a threshold no Solana deposit can
        ever reach -- every SOL swap would stall forever in `confirming` with
        nothing logged as wrong. See validate_min_commitment_rank().
        """
        self.url = url.strip()
        self.commitment = commitment
        self.timeout = float(timeout)
        self.mint = (mint or "").strip()
        self.hot_wallet = (hot_wallet or "").strip()
        self.min_commitment_rank = validate_min_commitment_rank(int(min_commitment_rank))
        #: Credits the LAST find_deposits_to_address() call read and refused. See
        #: UnattributableCredit -- returning [] for "nothing arrived" and for "money arrived
        #: that nobody can claim" is the distinction this exists to restore.
        self.unattributable_drops: list[UnattributableCredit] = []
        #: How many signatures that same call LISTED, so a caller can tell "no signatures" from
        #: "signatures with no credits in them" -- rule 3's denominator.
        #:
        #: RENAMED FROM signatures_read ON 2026-10-01, because it never meant that: it is the
        #: length of the getSignaturesForAddress result, before any of them is fetched. A name
        #: that says `read` while counting `listed` is what let the chain check report a filter
        #: as exercised over ten transactions when one of them was never fetched.
        self.signatures_listed = 0
        #: The signatures this same call LISTED and could not fetch -- a throttle, an
        #: unsupported transaction version, anything. A credit may be in any of them, so a
        #: caller reporting "no credits found" has to say over how many it actually looked.
        self.unreadable_signatures: list[str] = []

        # THIS VALIDATION MUST STAY LAST AND IT NEARLY DID NOT. An edit on 2026-10-01 inserted
        # the signatures_read property between the assignments above and this loop, which put a
        # `return` in front of it -- so a malformed SOL_SPL_MINT or SOL_HOT_WALLET was accepted
        # silently and the only thing that noticed was
        # test_a_typo_in_the_mint_or_hot_wallet_is_refused_at_construction. Worth the comment:
        # the construction-time refusal is the one guard that stops a typo'd address reaching
        # every later call, and it is at the bottom of a long __init__ where an insertion lands.
        for label, value in (("SOL_SPL_MINT", self.mint), ("SOL_HOT_WALLET", self.hot_wallet)):
            if value and not is_valid_address(value):
                raise SolanaAddressError(f"{label}={value!r} is not a valid Solana address")

    # --- plumbing ------------------------------------------------------------

    @property
    def signatures_read(self) -> int:
        """Listed minus unreadable: how many transactions the last scan actually fetched.

        A PROPERTY SO THE NAME CANNOT LIE AGAIN. It was an attribute assigned `len(signatures)`,
        which is the LISTED count, and every caller that trusted the word `read` was overstating
        its own coverage by however many were skipped -- measured on the operator's 2026-10-01
        run, where one getTransaction answered HTTP 429 and the chain check still reported the
        reader as having run over all ten.
        """
        return self.signatures_listed - len(self.unreadable_signatures)

    @property
    def is_spl(self) -> bool:
        """True when this adapter is configured for an SPL token rather than native SOL.

        wGRC is an SPL token, not native SOL, and that is the operator's actual
        holding -- so the token path is first-class here rather than a later
        addition. Every method below branches on this ONE predicate rather than
        each re-deriving "is a mint set", which is rule 8 applied small.
        """
        return bool(self.mint)

    def call(self, method: str, *params):
        """One Solana JSON-RPC call. Raises SolanaRPCError on anything but a result.

        Deliberately the same shape as RPCAdapter.call() so a reader moving
        between the two files recognizes it, and deliberately NOT shared code:
        the transports differ (a bare URL with no HTTP auth, versus
        user:password against host:port with an optional /wallet/<name> path),
        and merging them would produce a class with two mutually exclusive
        halves. Rule 8 asks that where they genuinely differ, both sites say
        so -- this paragraph is that, and chains/base.py's header already names
        the other RPC families in the tree.
        """
        if not self.url:
            raise SolanaRPCError(
                "no Solana RPC endpoint is configured. Set SOL_RPC_URL. There is deliberately no default: "
                "a default would point at SOMEBODY'S cluster, and the wrong one silently is worse than none loudly."
            )
        payload = {"jsonrpc": "2.0", "id": method, "method": method, "params": list(params)}
        try:
            response = requests.post(
                self.url,
                headers={"Content-Type": "application/json"},
                data=json.dumps(payload),
                timeout=self.timeout,
            )
        except requests.RequestException as exc:
            # Narrow, and re-raised: this is "the cluster could not be asked",
            # which is never an answer about a balance or a deposit.
            raise SolanaRPCError(f"{method} could not reach {self.url}: {exc}") from exc
        if response.status_code != 200:  # noqa: PLR2004 -- 200 is the JSON-RPC success status, not a tunable.
            # THE STATUS IS PASSED THROUGH, not just formatted into the message. See
            # SolanaRPCError's docstring: a caller needs to tell a rate limit from a real
            # failure, and reading it back out of the sentence would be parsing prose.
            raise SolanaRPCError(
                f"{method} returned HTTP {response.status_code} from {self.url}: {response.text[:300]}",
                status_code=response.status_code,
            )
        data = response.json()
        if data.get("error"):
            raise SolanaRPCError(f"{method} failed: {data['error']}")
        if "result" not in data:
            raise SolanaRPCError(f"{method} returned neither a result nor an error: {str(data)[:300]}")
        return data["result"]

    def _commitment(self, level: str | None = None) -> dict:
        return {"commitment": level or self.commitment}

    # --- the contract: validate_address --------------------------------------

    def validate_address(self, address: str) -> bool:
        """True if this address can receive a payout. Never raises for a bad address.

        NO NETWORK CALL, WHICH IS A REAL DIFFERENCE FROM chains/base.py.
        RPCAdapter asks a daemon and raises when it cannot, because a transport
        failure and a malformed address both produce False and the operator
        goes and checks the customer's address during an outage. A Solana
        address is a self-describing 32-byte ed25519 key in base58, so that
        confusion is structurally impossible here: this can always answer, and
        it can never answer because the network was down.

        IT REQUIRES THE KEY TO BE ON-CURVE, and that is the part worth reading.
        An Associated Token Account -- where an SPL balance, including wGRC,
        actually lives -- is a Program Derived Address: a valid 32-byte key
        chosen to be OFF the curve so no private key exists for it. A customer
        who pastes their token account address instead of their wallet address
        hands over a string that passes every syntactic check, and native SOL
        sent there is not recoverable by them. The chain does not refuse it.

        So an off-curve address is refused here, in BOTH modes:
          native SOL   the destination must be a wallet somebody can sign for.
          SPL token    the destination is the OWNER's wallet; this adapter
                       derives the ATA from it. Taking an ATA directly would
                       mean deriving an ATA of an ATA, which is a different
                       account again.

        Refusal is the recoverable direction: the customer re-pastes. The other
        direction is a transfer that cannot be undone.
        """
        if not is_valid_address(address):
            return False
        return is_on_curve(address)

    def describe_payout_address(self, address: str) -> str:
        """Why validate_address() answered the way it did, in one line (rule 14).

        create_swap() turns a False into `ValueError("Invalid SOL payout
        address")`, which tells a customer nothing and an operator less. This
        is what a diagnostic prints next to it, and for an SPL adapter it also
        names the token account the payout would actually land in -- which is
        not the address the customer gave, and is the single most surprising
        fact about paying an SPL token to somebody.
        """
        line = describe_address(address)
        if self.is_spl and is_valid_address(address) and is_on_curve(address):
            ata = associated_token_address(address, self.mint, self.token_program_id_or_default())
            line += f"\n    SPL payout would land in the owner's associated token account for mint {self.mint}:\n      {ata}"
        return line

    # --- the contract: get_balance -------------------------------------------

    def balance_lamports(self, address: str = "") -> int:
        """An account's balance in LAMPORTS -- integers, for the money path.

        SPLIT OUT OF get_balance() ON 2026-10-02 RATHER THAN COPIED, because
        the payout path needs the integer and get_balance() needs the float,
        and two reads of getBalance that unwrap the response separately is rule
        8's bug with a delay on it. get_balance() now divides this by
        SOL_DECIMALS for the native case; nothing else changed about it.

        WHY THE PAYOUT PATH CANNOT USE THE FLOAT. A balance check decides
        whether a transfer fits, and the comparison has to be exact: the app
        carries amounts as floats (db.py's REAL columns) and
        chains/solana_units.py records that above 2^53 base units a float stops
        being able to hold a distinct lamport. Reading the integer the cluster
        actually reported and keeping it an integer all the way into
        require_lamport_headroom() means there is no float on the money path at
        any point.

        `address` defaults to the configured hot wallet, which is what
        get_balance() asks about. Any other account is a read like any other.
        """
        target = (address or self.hot_wallet or "").strip()
        if not target:
            raise SolanaRPCError(
                "no Solana account was given and SOL_HOT_WALLET is unset, so there is no balance to read. "
                "Set SOL_HOT_WALLET to the PUBLIC key of the wallet this terminal pays out from. This "
                "adapter never reads a private key."
            )
        result = self.call("getBalance", target, {"commitment": BALANCE_COMMITMENT})
        # Both shapes are real: the documented response is
        # {"context": ..., "value": lamports}, and a bare integer is what a
        # stub or an older proxy returns. Handled in one place rather than at
        # each caller.
        return int(result["value"] if isinstance(result, dict) else result)

    def get_balance(self) -> float:
        """The hot wallet's spendable balance, as the float the app carries.

        Native SOL: getBalance on the wallet.
        SPL token:  getTokenAccountBalance on the wallet's associated token
                    account for the configured mint -- NOT getBalance, which
                    would report the account's LAMPORTS and be wrong by
                    whatever the token is worth.

        Read at `finalized` regardless of the discovery commitment, because
        this figure decides whether a payout can be covered and an unsettled
        balance can go away. Reporting less than is there is the safe side.

        A MISSING TOKEN ACCOUNT READS AS ZERO, AND THAT IS CORRECT RATHER THAN
        A SWALLOWED ERROR: an owner with no associated token account for a mint
        holds none of that token. The RPC's own "could not find account" is the
        answer, not a failure, and it is the one case below where an exception
        becomes a number. Every other failure propagates.
        """
        if not self.hot_wallet:
            raise SolanaRPCError(
                "no Solana hot wallet is configured, so there is no balance to read. Set SOL_HOT_WALLET to the "
                "PUBLIC key of the wallet this terminal pays out from. This adapter never reads a private key."
            )
        if not self.is_spl:
            return base_units_to_amount(self.balance_lamports(self.hot_wallet), SOL_DECIMALS)
        account = self.associated_token_account(self.hot_wallet)
        try:
            result = self.call("getTokenAccountBalance", account, {"commitment": BALANCE_COMMITMENT})
        except SolanaRPCError as exc:
            # Narrow by inspection of the message, not by catching everything:
            # only the cluster's specific "this account does not exist" becomes
            # a zero. A timeout, an HTTP 500 or a malformed response still
            # raises, so the caller can never read an outage as an empty
            # wallet (CLAUDE.md rule 12).
            if "could not find account" in str(exc).lower():
                return 0.0
            raise
        value = result["value"] if isinstance(result, dict) and "value" in result else result
        return base_units_to_amount(int(value["amount"]), int(value["decimals"]))

    # --- the contract: find_deposits_to_address ------------------------------

    def find_deposits_to_address(self, address: str, tx_limit: int = DEFAULT_SIGNATURE_LIMIT,
                                 skip_txids=frozenset()) -> list[dict]:
        """Every credit to `address`, in the dict shape deposit_service reads.

        Returns dicts with `txid`, `vout`, `address`, `amount` and
        `confirmations` -- the five keys measured out of
        services/deposit_service.py. `confirmations` is a COMMITMENT RANK, not
        a block count; chains/solana_units.py is the whole argument for why
        those are different and why the rank is what the gate may read.

        HOW A CREDIT IS COMPUTED, and it is not a search for an output:

          native SOL   the account's index in the transaction's account key
                       list, then postBalances[i] - preBalances[i]. A positive
                       delta is a credit. This is the account model's answer
                       and it is exact -- there is one net delta per account
                       per transaction, which is why one row per
                       (signature, address) is the right count.
          SPL token    the same subtraction over meta.preTokenBalances and
                       meta.postTokenBalances, matched on owner AND mint. The
                       `owner` field is what makes this work without deriving
                       an ATA: the cluster reports who the token account
                       belongs to.

        FAILED TRANSACTIONS ARE SKIPPED, and this is the one that would cost
        money if it were wrong. A Solana transaction with `meta.err` set still
        EXISTS, still appears in getSignaturesForAddress, still consumed a fee
        -- and moved nothing. Crediting one would credit a deposit that was
        never made.

        IT RAISES RATHER THAN FABRICATING A ROW. chains/base.py, faced with a
        transaction it could not decode, returns a synthetic event carrying
        vout=0 and the amount the caller already believed, which
        deposit_service cannot tell from a real one -- the artifact
        migrate_deposit_vouts.py exists to clean up. That branch is not
        reproduced here. A transaction that cannot be decoded stops the poll
        with an exception, which is visible, rather than crediting a number
        nobody measured.
        """
        if not is_valid_address(address):
            raise SolanaAddressError(f"cannot search for deposits to {address!r}: not a valid Solana address")
        # CLEARED PER CALL, so the list describes THIS poll rather than every poll since the
        # adapter was constructed. A watcher keeps one adapter for its lifetime, so an
        # accumulating list would report a drop from an hour ago as though it had just happened.
        self.unattributable_drops = []
        signatures = self.call(
            "getSignaturesForAddress",
            address,
            {"limit": int(tx_limit), "commitment": DISCOVERY_COMMITMENT},
        ) or []
        self.signatures_listed = len(signatures)
        events: list[dict] = []
        unreadable: list[str] = []
        skipped: list[str] = []
        for entry in signatures:
            if entry.get("err") is not None:
                # A failed transaction moved nothing. See the docstring.
                continue
            signature = entry.get("signature")
            if not signature:
                continue
            # ALREADY FINISHED WITH, SO NOT RE-READ. THIS IS THE RATE-LIMIT FIX.
            #
            # MEASURED ON THE OPERATOR'S HOST 2026-10-01, and it is the defect that
            # stopped a real deposit being credited. Solana discovery is the only
            # chain here that costs one RPC call PER TRANSACTION -- Bitcoin-family
            # uses one listtransactions, XRP one account_tx, and this lists
            # signatures and then calls getTransaction on each. With seven
            # signatures on the shared account, two open swaps and the reconciler,
            # that was roughly 24 calls every 15 seconds against the public devnet
            # endpoint, and it grows without bound as the account accumulates
            # history. api.devnet.solana.com answered:
            #
            #     read 0 of 7 listed transaction(s); 7 were unreadable
            #     getSignaturesForAddress returned HTTP 429:
            #       "Connection rate limits exceeded"
            #
            # so NOTHING credited, and the signature call itself eventually 429'd
            # and killed the worker (fixed separately in workers/common.py).
            #
            # WHY SKIPPING IS SAFE, and it is a property of the caller rather than
            # an optimistic assumption. services/deposit_service.
            # refresh_swap_from_chain() upserts the scanned events and then reads
            # EVERY stored deposit_events row back out of the database, and makes
            # every decision -- seen total, confirmed total, status transition --
            # from those rows. The scan feeds the upsert and nothing else. A
            # transaction whose stored row already has confirmations at or above
            # the swap's min_confirmations has nothing left to teach the upsert:
            # its amount cannot change and its rank cannot rise past the ceiling
            # the gate reads.
            #
            # A deposit still CONFIRMING is therefore never skipped -- the caller
            # only puts a settled txid in this set once it has reached the threshold.
            #
            # THE SET GAINED A SECOND SOURCE ON 2026-10-02, and the first version of
            # this fix was half of one. `settled_txids` was a JOIN from
            # deposit_events to swaps, so it could only ever name a transaction that
            # reached a swap -- and the two transactions actually burning the
            # operator's rate limit had reached none. They were recorded in
            # `unattributable_deposits`, which is what unattributable MEANS, so
            # nothing could skip them and they were re-read every cycle for two
            # days:
            #
            #     could not read transaction 5rHDrJYp... HTTP 429 ... SKIPPED it
            #     could not read transaction 61otPXfy... HTTP 429 ... SKIPPED it
            #     read 5 of 7 listed transaction(s); 2 were unreadable
            #
            # The rate limit those two burned is what made a REAL deposit come back
            # as "read 5 of 7". services/deposit_service.skip_txids() is now both
            # sources, and the parameter is `skip_txids` rather than
            # `settled_txids` because an unattributable transaction is not settled
            # -- it is the opposite, nothing credited and somebody still owed an
            # answer.
            if signature in skip_txids:
                skipped.append(signature)
                continue
            rank = commitment_rank(entry.get("confirmationStatus"))
            # ONE UNREADABLE TRANSACTION MUST NOT END THE SCAN, and until 2026-09-29 it did.
            # This call was bare, so any SolanaRPCError from getTransaction propagated out of
            # the loop and abandoned every signature after it.
            #
            # MEASURED ON DEVNET THAT DAY, not reasoned about. `solana_chain_check.py
            # --hunt-memo 20` against api.devnet.solana.com (solana-core 4.3.0) hit
            #
            #     -32015  Transaction version (1) is not supported by the requesting client.
            #             Please try the request again with "maxSupportedTransactionVersion": 1
            #
            # on one signature, and HTTP 429 on eleven more. Either would have aborted a real
            # deposit scan. The deposit account is SHARED by every SOL swap, so one versioned
            # transaction or one throttled response anywhere in its recent history stops
            # deposit discovery for ALL of them -- and stops it silently, because an
            # exception out of a poll loop looks like a poll that found nothing.
            #
            # SKIPPED LOUDLY, NEVER SILENTLY, and nothing is credited from a transaction that
            # could not be read -- the module header's rule is unchanged. What changes is the
            # blast radius: one signature instead of the whole account.
            # The try is INSIDE the loop deliberately: per-signature isolation is the whole
            # point, and hoisting it out would restore exactly the abort described above.
            try:
                events.extend(self._credits_in_transaction(signature, address, rank))
            except SolanaRPCError as error:
                unreadable.append(signature)
                logger.warning(
                    "SOL deposit scan could not read transaction %s for account %s and SKIPPED "
                    "it: %s. Nothing was credited from it. The scan continued; if a real "
                    "deposit is in this transaction it is NOT credited and needs a human.",
                    signature, address, error,
                )
        # EXPOSED, NOT ONLY LOGGED, and this is the third log-only fact surfaced in two days
        # -- after the unattributable drops and the rate limits. A caller that sees zero credits
        # and `signatures_listed == 10` concludes the reader was exercised over ten
        # transactions; if one of them could not be read, a credit may be IN it and the
        # conclusion is wrong. solana_chain_check's coverage report was making exactly that
        # claim, measured on the operator's 2026-10-01 run: one getTransaction answered HTTP 429,
        # the scan correctly skipped it and said so in the log, and the summary still reported
        # the filter as having "run over 10 signature(s) and matched nothing".
        self.unreadable_signatures = list(unreadable)
        # EXPOSED FOR THE SAME REASON unreadable_signatures IS. A caller that sees
        # `signatures_listed == 7` and two credits must be able to tell "five were
        # already finished with and deliberately not re-read" from "five could not
        # be read". The first is the scan working; the second is money possibly
        # uncredited. solana_chain_check.py's coverage report makes exactly this
        # kind of claim and was wrong about it once already.
        self.signatures_skipped = list(skipped)
        # AND NOW IT IS LOGGED, which the comment above has been claiming since it was
        # written. `signatures_skipped` was set here and read by NOTHING outside the test
        # suite -- grepped 2026-10-02, one hit in the whole tree, this line. The comment
        # four lines up says the attribute is exposed "FOR THE SAME REASON
        # unreadable_signatures IS", and unreadable gets the logger.warning below; this
        # one got the attribute and no line. So the number that proves the 2026-10-02
        # skip-set fix is working was unobservable on the live host, and I told the
        # operator to read it off the worker log. It was not there to read.
        #
        # ALWAYS PRINTED, INCLUDING AT ZERO (rule 14: `(none)` is a result). A scan that
        # skipped nothing and a scan whose skip set failed to load must not render
        # identically -- the second is the 429 leak coming back, and it comes back
        # silently: a re-read transaction looks exactly like a first read.
        logger.info(
            "SOL deposit scan for %s listed %d transaction(s) and deliberately did not "
            "re-read %d of them (already settled or already recorded unattributable). "
            "skipped=%d is the rate-limit toll this scan did NOT pay; 0 with a non-empty "
            "unattributable_deposits table for SOL means the skip set did not reach the "
            "adapter.%s",
            address, len(signatures), len(skipped), len(skipped),
            skip_detail(address, skipped),
        )
        if unreadable:
            # Rule 14: a scan that silently examined fewer transactions than it listed must
            # not report the same way as one that read them all.
            logger.warning(
                "SOL deposit scan for %s read %d of %d listed transaction(s); %d were "
                "unreadable and are named above. A deposit in any of them is NOT credited.",
                address, len(signatures) - len(unreadable), len(signatures), len(unreadable),
            )
        deduped = {(event["txid"], event["vout"]): event for event in events}
        return list(deduped.values())

    def _credits_in_transaction(self, signature: str, address: str, rank: int) -> list[dict]:
        """The credits `address` received in one transaction, as event dicts.

        Split out from find_deposits_to_address so the per-transaction decision
        can be called with a seeded response (CLAUDE.md rule 10) instead of
        needing a cluster and a signature list to reach it.
        """
        # maxSupportedTransactionVersion IS 0 AND RAISING IT IS A PROPOSAL, NOT A FIX.
        # Measured 2026-09-29 against api.devnet.solana.com (solana-core 4.3.0): a signature
        # came back `-32015 Transaction version (1) is not supported by the requesting
        # client. Please try the request again with "maxSupportedTransactionVersion": 1`.
        # The obvious move is to do what the error says. Do not, yet, and the reason is in
        # this file rather than in the error:
        #
        # `_native_credits` maps the deposit address to a balance index through
        # `message.accountKeys` alone (see its own line), and a versioned transaction can
        # draw account keys from an ADDRESS LOOKUP TABLE, which arrive in
        # `meta.loadedAddresses` instead. Grepped the tree 2026-09-29 for `loadedAddresses`:
        # ZERO hits. Nothing here knows about them.
        #
        # WHAT THAT WOULD AND WOULD NOT COST, stated precisely because the scarier version is
        # easy to reach for and is wrong. Static keys occupy the LEADING indices of the
        # resolved key list, so positional indexing into preBalances/postBalances stays
        # correct for them -- this is not a misattribution risk. What it is, is a MISSED
        # deposit: an address that arrives via a lookup table is simply absent from
        # `accountKeys`, `address not in keys` returns [], and real money is silently not
        # credited. A deposit account named directly by a sender is almost certainly a static
        # key, but "almost certainly" is not the standard for crediting money.
        #
        # So raising this needs loadedAddresses merged into `keys` FIRST, and that cannot be
        # verified from this container -- the environment's network policy denies
        # api.devnet.solana.com, so there is no versioned transaction here to test against
        # (rule 16: a fix that cannot be tested here is a proposal, and this says which).
        # Until then the cap stays at 0 and an unreadable transaction is SKIPPED loudly by
        # find_deposits_to_address above rather than crediting anything.
        #
        # THE MEMO HUNT ASKS FOR VERSION 1 AND THIS STILL ASKS FOR 0, on purpose, and each site
        # names the other per rule 8's "if they genuinely differ, the difference is the point".
        # solana_chain_check.MEMO_HUNT_TRANSACTION_VERSION carries the reasoning: the hunt only
        # reads memo instructions, and chains/solana_memo.py never touches `accountKeys` at all
        # (checked by AST 2026-09-30 -- neither `accountKeys` nor `loadedAddresses` appears in
        # that module), so there is no balance index for a lookup table to misalign. Here there
        # is, and it is somebody's deposit. The operator's 2026-09-30 run showed the cost of the
        # cap: one of fifty reads answered -32015, a real memo this tree could not see. On THIS
        # path that cost is the correct trade until loadedAddresses is merged into `keys`.
        transaction = self.call(
            "getTransaction",
            signature,
            {"encoding": "jsonParsed", "commitment": DISCOVERY_COMMITMENT, "maxSupportedTransactionVersion": 0},
        )
        if not transaction:
            raise SolanaRPCError(
                f"getTransaction returned nothing for signature {signature}, which getSignaturesForAddress had just "
                "listed. Nothing is credited from a transaction that cannot be read -- see this module's header on "
                "why a fabricated row is worse than a stalled swap."
            )
        meta = transaction.get("meta") or {}
        if meta.get("err") is not None:
            return []
        credits = (self._spl_credits(signature, address, meta, rank) if self.is_spl
                   else self._native_credits(signature, address, transaction, meta, rank))
        return self._attributable(signature, transaction, credits)

    def _attributable(self, signature: str, transaction: dict, credits: list[dict]) -> list[dict]:
        """Stamp each credit with the transaction's memo tag, or DROP it and say so.

        THE ADDRESS NO LONGER IDENTIFIES THE SWAP. Every SOL swap shares one deposit account
        under the strategy the operator chose on 2026-09-29, so the memo is the only thing that
        says whose money this is. A credit without one is not this swap's and is not anybody's
        until a human matches it.

        DROPPED, NOT STAMPED WITH A FALLBACK. Returning the account index -- what `vout` used to
        carry -- would be worse than returning nothing: attributable_events() compares `vout`
        against the swap's deposit_tag, and a small integer read off the transaction could
        COLLIDE with a real tag and credit a stranger's deposit to somebody's swap. The old
        value was never a discriminator and must not be reused as one.

        AND IT IS LOGGED AT WARNING, because a dropped credit is real money arriving that
        nobody can attribute. Nothing is wrong with the code; something is wrong for a
        customer, and a support ticket needs the signature and the reason to exist at all.
        """
        if not credits:
            return []
        tag, why = deposit_tag_from(transaction)
        if tag is None:
            # RECORDED AS WELL AS LOGGED. The log is for an operator reading a file later; this
            # is for the caller deciding what to say on a screen now. See UnattributableCredit.
            # THE AMOUNT AND THE ADDRESS COME OFF THE CREDITS, which already hold both -- the
            # old version discarded them and recorded only the count. See UnattributableCredit.
            stranded = sum(float(credit["amount"]) for credit in credits)
            self.unattributable_drops.append(UnattributableCredit(
                signature=signature, credits=len(credits), why=why,
                # OFF THE CREDITS, not a parameter: _attributable() is handed the credits and
                # not the address, and every credit in this list was selected BY that account
                # (owner for SPL, the account key for native), so they agree by construction.
                # Adding a parameter would have been a second source for one fact.
                amount=stranded, address=str(credits[0]["address"])))
            logger.warning(
                # "CREDITED BY THIS TRANSACTION", NOT "IN TOTAL". The operator's 2026-10-01
                # native run printed `28.7786992 SOL in total` four lines under
                # `balance ... ok 28.7786992 SOL`, and the two are different quantities that
                # happened to print the same number: one is this transaction's DELTA
                # (post[index] - pre[index]) and the other is the account's whole balance. I
                # could not tell them apart reading the block either, which is rule 14's "state
                # what the number means, next to the number" -- the reader has the screen, not
                # the source. They are equal when the dropped credit IS the account's funding
                # transaction, and that is worth being able to see rather than infer.
                "SOL deposit %s to the shared account CANNOT BE ATTRIBUTED and was NOT credited: "
                "%s. %d credit(s) dropped, %s SOL credited to it BY THIS TRANSACTION (a balance "
                "delta, not the account's balance). The coins arrived and are real; matching "
                "them to a swap is a human's job, and crediting them to whichever swap was "
                "being refreshed would pay the wrong person.",
                signature, why, len(credits), stranded,
            )
            return []
        return [{**credit, "vout": tag} for credit in credits]

    def _native_credits(self, signature: str, address: str, transaction: dict, meta: dict, rank: int) -> list[dict]:
        """Native SOL: postBalances[i] - preBalances[i] for the account's index."""
        keys = [self._account_key(key) for key in (transaction.get("transaction", {}).get("message", {}).get("accountKeys") or [])]
        pre, post = meta.get("preBalances") or [], meta.get("postBalances") or []
        if address not in keys:
            return []
        index = keys.index(address)
        if index >= len(pre) or index >= len(post):
            raise SolanaRPCError(
                f"transaction {signature} lists {address} at account index {index} but its balance arrays are "
                f"{len(pre)}/{len(post)} long. Refusing to guess a credit from a response this shape."
            )
        delta = int(post[index]) - int(pre[index])
        if delta <= 0:
            return []
        assert_amount_fits_a_float(signature, address, delta)
        return [deposit_event(signature, index, address, base_units_to_amount(delta, SOL_DECIMALS), rank)]

    def _spl_credits(self, signature: str, address: str, meta: dict, rank: int) -> list[dict]:
        """SPL token: the same subtraction over the token balance arrays.

        Matched on `owner` AND `mint`. Matching on owner alone would credit a
        deposit of a DIFFERENT token to the same wallet, at this mint's
        decimals -- which is the rule 11 failure this repository's own rule
        names: a value crossing a boundary at the wrong precision is silently
        wrong by orders of magnitude.

        Pre-balance defaults to zero when the account did not exist before the
        transaction, which is the ordinary case for a first deposit into a
        freshly created associated token account.
        """
        def indexed(entries):
            return {
                int(entry["accountIndex"]): entry
                for entry in entries or []
                if entry.get("owner") == address and entry.get("mint") == self.mint
            }

        pre, post = indexed(meta.get("preTokenBalances")), indexed(meta.get("postTokenBalances"))
        events = []
        for index, entry in sorted(post.items()):
            decimals = int(entry["uiTokenAmount"]["decimals"])
            before = int(pre[index]["uiTokenAmount"]["amount"]) if index in pre else 0
            delta = int(entry["uiTokenAmount"]["amount"]) - before
            if delta > 0:
                assert_amount_fits_a_float(signature, address, delta)
                events.append(deposit_event(signature, index, address, base_units_to_amount(delta, decimals), rank))
        return events

    @staticmethod
    def _account_key(key) -> str:
        """Account keys are bare strings on some responses and dicts on jsonParsed ones.

        Both shapes are real -- `jsonParsed` returns
        {"pubkey": ..., "signer": ..., "writable": ...} while the base encoding
        returns the string -- and a reader who has only seen one will write
        code that works until the other arrives. Handled in one place rather
        than at each of the two call sites.
        """
        return key.get("pubkey", "") if isinstance(key, dict) else str(key)

    # --- the contract: one refusal, and the payout path ----------------------
    #
    # get_new_address() refuses unconditionally and always will: the shared-
    # account strategy has no per-swap address to derive. send_to_address()
    # PREVIEWS by default and refuses every unarmed caller, which is a
    # different kind of no -- see its docstring for the five properties that
    # make the default safe, and chains/solana_signing.py for every guard.

    def get_new_address(self, label: str) -> str:
        """REFUSES, AND THE REASON CHANGED ON 2026-09-29 WITHOUT THIS METHOD NOTICING.

        Bitcoin, Litecoin and Gridcoin answer this with `getnewaddress`: the
        DAEMON derives a key, stores it in wallet.dat, and the application
        never holds a secret. **Solana has no equivalent.** There is no wallet
        daemon, no keystore, and nothing on the other end of an RPC that can
        mint an address and remember how to spend from it. So the application
        must hold something, and WHICH something was a custody decision --
        which is why this used to refuse with "no strategy has been chosen"
        and a menu of three options.

        THE OPERATOR CHOSE, and the refusal outlived the question. On
        2026-09-29 they chose one shared account plus a per-swap Memo
        instruction, and clarified that this terminal takes no custody beyond
        brief escrow. Under that strategy there IS no per-swap Solana address,
        so this method still refuses -- but as a CONSEQUENCE of the decision
        rather than a placeholder waiting on it, and the difference matters to
        whoever hits it: the old message sent them to README.md to choose
        something that is already chosen, and the new one sends them to the
        function that has the answer.

        WHERE A SOL DEPOSIT TARGET ACTUALLY COMES FROM:
        services/swap_service.deposit_account(), which returns the shared
        account from TAG_ATTRIBUTION and `needs_tag=True`, with the integer
        allocated from the database. chains/solana_memo.py reads it back off
        the transaction and _attributable() above drops any credit it cannot
        resolve to one.
        """
        raise NotImplementedError(
            f"cannot derive a per-swap Solana deposit address for {label!r}: there is no such thing here.\n"
            "  Solana has no `getnewaddress` -- no wallet daemon to hold a key -- so the operator chose the\n"
            # "Memo instruction" IS KEPT ON ONE LINE ON PURPOSE. It is the exact value in
            # services/swap_service.TAG_ATTRIBUTION, so it is what an operator greps for after
            # reading it off a screen -- and a phrase broken across a line break is a phrase
            # nothing finds. This message wrapped it as "per-swap Memo\n  instruction" for
            # about a minute and the test asserting the field name failed on the wrap, which is
            # the same class of miss as the fifth stale copy this commit is fixing: the search
            # that would have found it did not match the way the text was written.
            "  strategy that adds no secret at all (2026-09-29): ONE SHARED ACCOUNT plus a per-swap\n"
            "  Memo instruction carrying the swap's integer tag. A shared account has no per-swap\n"
            "  address, so this refusal is a CONSEQUENCE of that choice, not a placeholder awaiting it.\n"
            "  WHAT TO CALL INSTEAD: services/swap_service.deposit_account(config, adapters, 'SOL', swap_id),\n"
            "  which returns (shared account, needs_tag=True) and allocates the tag from the database.\n"
            "  See README.md, 'Solana deposit addresses' -- it records the decision and what was built to it."
        )

    def genesis_hash(self) -> str:
        """The cluster's genesis hash -- what a cluster CANNOT lie about.

        One RPC read, kept as a method so the payout path and any diagnostic
        ask the same question the same way. chains/solana_rpc_map.py already
        records getGenesisHash as the identification of a cluster and why a
        hostname is not one; chains/solana_signing.require_devnet() is what
        DECIDES on the answer, and this only fetches it.
        """
        return str(self.call("getGenesisHash") or "").strip()

    def latest_blockhash(self) -> tuple[str, int]:
        """A fresh blockhash to sign over, and the block height it expires at.

        THIS IS A SIGNING NONCE, NOT THE TIP, which is the sentence
        chains/solana_rpc_map.py's `getbestblockhash` entry leads with. A
        Solana transaction embeds a recent blockhash to bound its lifetime:
        roughly 150 slots, after which the transaction can never be accepted --
        and that expiry is the only thing that makes a signed-but-unbroadcast
        transaction safe to abandon.

        FETCHED AT BROADCAST_COMMITMENT, which is the same commitment the
        preflight simulation uses. See that constant for why the two must
        match.

        NOT CALLED BY preview_payout(), on purpose. A blockhash read during a
        preview would be most of a minute old by the time an operator finished
        reading the preview and armed the send, and a transaction signed over
        an expired blockhash is rejected after it reaches a node. So the
        preview shows everything EXCEPT the nonce, and the nonce is fetched
        immediately before signing.
        """
        result = self.call("getLatestBlockhash", {"commitment": BROADCAST_COMMITMENT}) or {}
        value = result.get("value") if isinstance(result, dict) else None
        value = value or {}
        blockhash = str(value.get("blockhash") or "").strip()
        if not blockhash:
            raise SolanaRPCError(
                f"getLatestBlockhash returned no blockhash: {str(result)[:200]}. NOTHING was signed. A "
                "transaction cannot be built without one, and this path will not substitute any other "
                "hash for it."
            )
        return blockhash, int(value.get("lastValidBlockHeight") or 0)

    def preview_payout(self, address: str, amount: float, *, retain_lamports: int = 0) -> dict:
        """Everything about a SOL payout except the signature. Read-only.

        THIS IS THE DEFAULT MODE of send_to_address(), not a separate feature,
        and it is modeled on chains/xrp.py's preview_payout() which was modeled
        on build_transfer_plan() below. It reads the cluster, does every
        conversion, applies every guard that can be applied without a key, and
        returns the result plus a `description` an operator reads before arming
        anything (rule 14: echo the parameters that decide the answer, so a
        pasted block is self-describing a day later).

        IT DOES NOT DUPLICATE build_transfer_plan() -- it calls it (rule 8).
        That function already owns the destination validation, the decimals,
        the truncation to base units and the per-signature fee, and it is the
        half of this path that existed before any of it could sign. What is
        added here is everything that needs the CLUSTER and the PAYER: which
        cluster this is, what the payer holds, whether the destination account
        exists, and the rent-exempt floor that applies when it does not.

        THE ORDER OF THE GUARDS IS THE DESIGN, cheapest-and-most-fatal first:

          1  an SPL mint            refused with no network call at all. Only
                                    the native transfer is built.
          2  the destination        build_transfer_plan(): base58, length,
             and the amount         ON-CURVE, and an amount that does not
                                    truncate to zero. All local.
          3  the payer              SOL_HOT_WALLET must be set, and must not be
                                    the destination. Local.
          4  the CLUSTER            getGenesisHash, and require_devnet()
                                    refuses mainnet-beta, testnet, an
                                    unrecognized genesis and a missing one.
                                    Everything below is unreachable anywhere
                                    but devnet, INCLUDING in preview mode --
                                    wanting a preview is not a reason to let
                                    this path talk to mainnet.
          5  the destination        getAccountInfo. A native transfer to an
             account                address with no account CREATES it, and
                                    the runtime then holds the new account to
                                    a rent-exempt minimum.
          6  the rent floor         getMinimumBalanceForRentExemption(0),
                                    asked of the CLUSTER, and only when the
                                    destination does not exist. A payout below
                                    it would be rejected rather than arriving
                                    small, so it is refused here.
          7  the payer's balance    getBalance in lamports, and integer
                                    arithmetic against the fee and what the
                                    payer must retain.

        WHY STEP 6 IS A REFUSAL AND NOT A DEDUCTION, because the distinction
        cost a wrong answer upstream: the rent-exempt minimum of a NEW system
        account is a FLOOR ON THE PAYOUT AMOUNT, not a cost somebody pays. The
        165-byte figure build_transfer_plan() prices for an SPL transfer is a
        different quantity -- that one really is a deposit into a new token
        account -- and it does not apply to a native transfer at all. On the
        operator's configuration (SOL_SPL_MINT empty) the only chain cost of a
        payout is the 5,000-lamport signature fee.

        Raises rather than returning a refusal in a field. A dict with
        `ok: False` in it is the shape every caller forgets to check, and on
        this path the cost of forgetting is a send.
        """
        if self.is_spl:
            raise SolanaSplSendRefused(
                f"this adapter is configured for the SPL mint {self.mint}, and the payout path builds only "
                f"the NATIVE SOL transfer. NOTHING was read from the cluster and nothing was signed. An "
                f"SPL transfer is a different instruction (the Token program's TransferChecked, over two "
                f"associated token accounts, at the mint's own decimals) and chains/solana_transaction.py "
                f"lays out one shape: SystemProgram.transfer. Paying a token holder in lamports because "
                f"the code could serialize that instead is the silent failure this refusal exists to stop."
            )
        plan = dict(self.build_transfer_plan(address, amount))
        payer = (self.hot_wallet or "").strip()
        if not payer:
            raise SolanaRPCError(
                "SOL_HOT_WALLET is unset, so this payout has no payer and NOTHING was read from the "
                "cluster. It must be the PUBLIC key of the account the payout debits -- which is also the "
                "account the signing keypair has to derive to, because chains/solana_signing.py refuses a "
                "key that derives anything else. An unannounced payer is an account nobody checked."
            )
        if payer == plan["destination"]:
            raise SolanaRPCError(
                f"the payout destination is the payer itself ({payer}), so this transfer would move "
                f"nothing and pay a fee to do it. NOTHING was read from the cluster. It is also not a "
                f"shape this path can build: a self-transfer de-duplicates the account key list, which "
                f"shifts every instruction index."
            )
        plan["payer"] = payer

        # Rule 14: announce BEFORE, not only after. Everything below this line
        # makes network calls, and an operator watching a blinking cursor
        # cannot tell working from hung -- which on this path resolves with a
        # Ctrl-C.
        print(
            f"  SOL payout preview  {plan['amount']} SOL ({plan['base_units']} lamports) "
            f"{payer} -> {plan['destination']}",
            flush=True,
        )
        print(f"  SOL payout preview  reading the cluster's genesis hash and balances from {self.url}", flush=True)

        cluster = require_devnet(self.genesis_hash(), self.url)
        destination_exists = self.account_exists(plan["destination"])
        if destination_exists:
            rent_minimum = 0
            rent_source = (
                "not asked: the destination account already exists, so no account is created and no "
                "rent-exempt minimum applies"
            )
        else:
            rent_minimum = self.rent_exempt_minimum(SYSTEM_ACCOUNT_SPACE)
            rent_source = (
                f"{rent_minimum} lamports, read from the CLUSTER with "
                f"getMinimumBalanceForRentExemption({SYSTEM_ACCOUNT_SPACE}) rather than from a constant -- "
                f"chains/solana_units.py records that this repository's reference figures were stale on "
                f"every cluster for weeks"
            )
        rent_line = require_destination_rent(destination_exists, plan["base_units"], rent_minimum)
        balance = self.balance_lamports(payer)
        retain = self.rent_exempt_minimum(SYSTEM_ACCOUNT_SPACE) + int(retain_lamports)
        headroom = require_lamport_headroom(balance, plan["base_units"], plan["fee_lamports"], retain)

        plan.update(
            {
                "cluster": cluster,
                "destination_exists": destination_exists,
                "rent_minimum_lamports": rent_minimum,
                "balance_lamports": balance,
                "retain_lamports": retain,
                "signed": False,
                "broadcast": False,
            }
        )
        plan["description"] = "\n".join(
            [
                "  SOL PAYOUT PREVIEW (nothing signed, nothing broadcast):",
                f"    cluster       {cluster}",
                f"    from          {payer}  (the account this DEBITS)",
                f"    to            {plan['destination']}",
                f"    amount        {plan['amount']} SOL = {plan['base_units']} lamports",
                f"    network fee   {plan['fee_lamports']} lamports  <- per SIGNATURE, not per byte",
                f"    destination   {'EXISTS on this cluster' if destination_exists else 'DOES NOT EXIST -- this transfer would CREATE it'}",
                f"    rent floor    {rent_source}",
                f"    rent verdict  {rent_line}",
                f"    payer retains {retain} lamports  <- the payer's own rent-exempt minimum from the "
                f"cluster plus {int(retain_lamports)} passed in by the caller",
                f"    headroom      {headroom}",
                "    blockhash     NOT fetched for a preview: it expires in ~150 slots, so it is read "
                "immediately before signing instead",
            ]
        )
        return plan

    def send_to_address(self, address: str, amount: float, *, confirm_send: str = "", retain_lamports: int = 0) -> str:
        """PREVIEWS by default. Signs and broadcasts only when armed, and only on devnet.

        WHAT CHANGED ON 2026-10-02, AND WHAT DID NOT. This method used to refuse
        unconditionally, and the refusal was an ABSENCE: nothing in this module
        imported anything that could sign, so it could not have signed if the
        check had been deleted. A payout path exists now, and the honest
        statement of what replaced that absence is FIVE structural properties,
        not one of which is a configuration value:

          the cluster       preview_payout() asks the CLUSTER for its genesis
                            hash and chains/solana_signing.require_devnet()
                            refuses mainnet-beta, testnet, an unrecognized
                            genesis and a missing one. The url is echoed and
                            never consulted, because a hostname resolves to
                            whatever DNS says today. There is no flag,
                            environment variable or argument that turns this
                            off, and the PREVIEW path runs it too -- so
                            mainnet cannot even be previewed against.
          the arming token  confirm_send must equal
                            chains/solana_signing.CONFIRM_SOL_SEND exactly. A
                            caller that omits it gets the preview and a
                            refusal. Deliberately NOT a boolean: a truthy
                            variable, a parsed config value or a positional
                            argument that drifted one place could each produce
                            a send nobody wrote.
          no key here       THIS MODULE STILL HOLDS NO KEYPAIR AND READS NO KEY
                            PATH, and that is checked rather than claimed:
                            tests/test_solana_adapter.py::
                            test_the_module_references_no_keypair_anywhere
                            tokenizes this file and refuses the names Keypair,
                            secret_key, from_secret_key, sign, sign_message and
                            partial_sign. The secret's whole lifetime is one
                            call to chains/solana_signing.signed_transfer_wire(),
                            which returns bytes. This adapter broadcasts a
                            transaction it could not have produced.
          the caller        services/payout_service.py:354 calls
                            `send_to_address(swap["payout_address"], amount)` --
                            two positional arguments and no keywords. It
                            therefore gets SolanaSendNotArmed with the preview
                            in the message, and the swap lands in `failed` with
                            the reason recorded. THAT CALL SITE WAS NOT WIRED
                            UP and wiring it is the operator's (rule 16: fund
                            movement comes back).
          can_spend         still False, so chains/registry.payout_refusal()
                            still reports SOL as unable to pay out and
                            Config.ALLOWED_PAIRS still names no pair that pays
                            out in SOL. Flipping either is live posture and is
                            the operator's.

        AND THE BROADCAST ITSELF IS A PROPOSAL (rule 16), which is the one
        sentence in this docstring that matters most. No transaction built by
        this path has ever reached a cluster: api.devnet.solana.com answers 403
        at this container's proxy -- re-measured 2026-10-02 -- so the
        serialization is verified against an independent implementation
        (chains/solana_transaction.py's header has the measurement) and the
        broadcast is verified against nothing. The first real send is the
        operator's run, and it should be for the smallest amount that clears
        the rent floor the preview prints.

        Returns the transaction signature, which is Solana's transaction id.
        Raises on every refusal, and the class of the exception says WHICH
        guard fired (see chains/solana_signing.py).
        """
        plan = self.preview_payout(address, amount, retain_lamports=retain_lamports)

        # The arming check comes AFTER the preview and BEFORE anything that
        # could sign, which is the order that makes the default useful: an
        # unarmed caller gets the full preview inside the refusal message
        # rather than a bare "not armed", so an operator who then arms it is
        # arming something they have read. chains/xrp.py records the same
        # ordering and the one defect it had: the description must be emitted
        # EXACTLY ONCE, so the unarmed path carries it in the exception and the
        # live print happens below, immediately before the irreversible step.
        try:
            require_send_confirmation(confirm_send, keypair_path_from_environment())
        except SolanaSendNotArmed as error:
            raise SolanaSendNotArmed(f"{error}\n{plan['description']}") from error

        print(plan["description"], flush=True)
        return self._sign_and_broadcast(plan, confirm_send)

    def _sign_and_broadcast(self, plan: dict, confirm_send: str) -> str:
        """The only code in chains/solana.py downstream of a signature.

        Separate from send_to_address() rather than inlined, and the split is
        exactly where the guards end and the irreversible part begins:
        everything above this call is a read or a refusal, and the first
        statement inside it fetches the nonce that makes a signature possible.

        It still holds no key. chains/solana_signing.signed_transfer_wire()
        takes the plan, the blockhash and the arming token, reads the keypair
        from the file SOL_PAYOUT_KEYPAIR_PATH names, derives the public key and
        REFUSES if it is not the payer this plan announced, signs, verifies its
        own signature, parses the signed bytes back and compares them against
        the plan. What comes back here is a base64 string and a signature.
        """
        blockhash, last_valid_height = self.latest_blockhash()
        print(
            f"  SOL payout  blockhash {blockhash} valid through block height {last_valid_height} "
            f"<- a signing NONCE, not the tip; after that height this transaction can never be accepted",
            flush=True,
        )
        # THE TOKEN IS A PARAMETER AND NEVER ADAPTER STATE. An attribute
        # holding it would leave the instance armed for every later call --
        # and services/payout_service.refresh_wallet_inventory() reuses one
        # adapter object across a whole cycle.
        signed = signed_transfer_wire(plan, blockhash, confirm_send)
        print(f"  SOL payout  {signed['derivation']}", flush=True)
        print(f"  SOL payout  signing key {signed['keypair_file']}", flush=True)
        print(
            f"  SOL payout  broadcasting {len(signed['wire'])} bytes, skipPreflight={SKIP_PREFLIGHT} "
            f"preflightCommitment={BROADCAST_COMMITMENT}  <- preflight ON so a rent, blockhash or signer "
            f"fault is caught before a leader sees this",
            flush=True,
        )
        signature = str(
            self.call(
                "sendTransaction",
                signed["base64"],
                {
                    "encoding": "base64",
                    "skipPreflight": SKIP_PREFLIGHT,
                    "preflightCommitment": BROADCAST_COMMITMENT,
                },
            )
            or ""
        ).strip()
        if not signature:
            raise SolanaRPCError(
                "sendTransaction returned no signature. THIS IS NOT PROOF NOTHING WAS SENT: the "
                f"transaction was signed and handed to {self.url}, and a response this path cannot read "
                f"is not a refusal. The signature this process computed is {signed['signature']} -- look "
                f"it up on the cluster before retrying, because a retry that duplicates a landed transfer "
                f"pays twice."
            )
        if signature != signed["signature"]:
            raise SolanaRPCError(
                f"the cluster returned signature {signature} and this process signed "
                f"{signed['signature']}. THIS IS NOT PROOF NOTHING WAS SENT. A transaction's signature is "
                f"computed locally from its own bytes, so two different values mean the endpoint did not "
                f"broadcast what it was given. Look BOTH signatures up before doing anything else."
            )
        return self._await_settlement(signature)

    def _await_settlement(self, signature: str) -> str:
        """Poll until the cluster has settled the transaction, and ASSERT the outcome.

        Rule 13's "a stop that cannot prove it worked is not a stop", applied
        to a send: sendTransaction returning a signature means a node accepted
        the bytes for forwarding, which is NOT the same as the transaction
        having executed. A transfer that fails on chain -- insufficient funds,
        a rent violation, an expired blockhash -- has a real signature and did
        nothing, and recording that signature as a completed payout is how a
        customer is told they were paid when they were not.

        So the assertion is the OUTCOME rather than the absence of an
        exception, and a failed transaction raises here rather than returning.

        Every line is announced with its elapsed time in microfortnights
        (rule 6, through the one conversion in swap_terminal/microfortnights.py)
        because this loop is the longest silence on the path -- up to
        CONFIRMATION_DEADLINE_SECONDS of it, which is where an operator reaches
        for Ctrl-C.
        """
        started = time.monotonic()
        polls = 0
        while True:
            status = self.signature_status(signature)
            polls += 1
            elapsed = time.monotonic() - started
            # THIS GUARD WAS WRITTEN `if False and status["err"] is not None`
            # AND WAS THEREFORE DEAD, found 2026-10-02 by the test that names
            # it (tests/test_solana_payout.py::test_a_transaction_the_cluster_
            # REJECTS_raises_rather_than_returning_a_signature, the one red
            # test in the suite). Measured with the guard disabled: a transfer
            # the cluster rejected -- confirmationStatus `finalized` with
            # err={"InstructionError": [0, "Custom"]} -- fell through to the
            # rank check, where signature_status() collapses a failed
            # transaction to rank 0 on purpose, so the loop polled for the full
            # CONFIRMATION_DEADLINE_SECONDS and then raised the TIMEOUT
            # message: "THIS IS NOT PROOF IT DID NOT LAND ... do NOT retry
            # blind." That is the wrong sentence about the one outcome the
            # cluster had already stated definitively. A definite on-chain
            # FAILURE was reported as "we do not know", which is the exact
            # confusion the paragraph above says this function exists to
            # prevent, and it cost 90s of silence per occurrence on the way
            # there (74.4µfn, 90.0s). `if False and X` is not a check; it is a
            # check somebody switched off.
            if status["err"] is not None:
                raise SolanaRPCError(
                    f"the transaction was broadcast and the cluster REJECTED it: err={status['err']!r}. "
                    f"signature={signature}. No value was delivered. Raised rather than returned so that "
                    f"a failed transfer cannot be written into `payouts` as a broadcast one. Nothing is "
                    f"retried here: fix what the error names, then preview again."
                )
            if int(status["rank"]) >= int(self.min_commitment_rank):
                print(
                    f"  SOL payout  SETTLED at rank {status['rank']} (>= the configured "
                    f"{self.min_commitment_rank}) after {polls} poll(s) in {format_duration(elapsed)}  "
                    f"signature={signature}",
                    flush=True,
                )
                return signature
            if elapsed >= CONFIRMATION_DEADLINE_SECONDS:
                raise SolanaRPCError(
                    f"the transaction was broadcast and has NOT reached rank {self.min_commitment_rank} "
                    f"after {format_duration(elapsed)} and {polls} poll(s); it is at rank "
                    f"{status['rank']} and the cluster "
                    f"{'knows it' if status['known'] else 'does not know it yet'}. "
                    f"signature={signature}. THIS IS NOT PROOF IT DID NOT LAND -- a blockhash is valid "
                    f"for roughly 150 slots and a transaction can settle after this gave up waiting. Look "
                    f"the signature up; do NOT retry blind, because a retry that duplicates a landed "
                    f"transfer pays twice and on this chain that is final."
                )
            print(
                f"  SOL payout  waiting for settlement: rank {status['rank']} of "
                f"{self.min_commitment_rank}, poll {polls}, {format_duration(elapsed)} elapsed",
                flush=True,
            )
            time.sleep(CONFIRMATION_POLL_SECONDS)

    # --- the proposal half: built, described, unsigned -----------------------

    def build_transfer_plan(self, address: str, amount: float) -> dict:
        """Everything about a payout except the signature. Read-only.

        Returns a dict AND a human-readable `description`, because both
        consumers are real: a test asserts on the fields, and an operator reads
        the description (CLAUDE.md rule 14 -- echo the parameters that decide
        the answer, so the pasted block is self-describing a day later).

        THE THREE THINGS THAT DIFFER FROM A BITCOIN PAYOUT, ALL PRICED HERE:

          the fee is per signature    5,000 lamports for a one-signature
                                      transfer, regardless of size. Not
                                      `rate x bytes`.
          the destination may not     an SPL transfer needs the recipient's
          exist                       associated token account, and if it does
                                      not exist somebody pays its rent-exempt
                                      minimum to create it. That is not a fee;
                                      it is a deposit into a new account, and
                                      WHO PAYS IT is a decision the operator
                                      makes -- this reports the cost and does
                                      not pick.
          decimals come from the      never SOL's 9. mint_decimals() reads the
          mint                        mint account.
        """
        if not self.validate_address(address):
            raise SolanaAddressError(f"refusing to plan a transfer to {address!r}: {describe_address(address)}")
        decimals = self.mint_decimals() if self.is_spl else SOL_DECIMALS
        base_units = amount_to_base_units(amount, decimals)
        if base_units <= 0:
            raise ValueError(
                f"{amount} of {self.mint or 'SOL'} rounds down to {base_units} base units at {decimals} decimals. "
                "Truncation is deliberate (it errs toward keeping funds), so an amount this small cannot be sent."
            )
        plan = {
            "asset": self.asset,
            "mint": self.mint or None,
            "decimals": decimals,
            "destination": address,
            "amount": amount,
            "base_units": base_units,
            "fee_lamports": transfer_fee_lamports(1),
            "signed": False,
            "broadcast": False,
        }
        lines = [
            "  TRANSFER PLAN (unsigned, not broadcast):",
            f"    asset           {self.mint or 'native SOL'}",
            f"    destination     {address}",
            f"    amount          {amount} -> {base_units} base units at {decimals} decimals",
            f"    network fee     {plan['fee_lamports']} lamports  <- per SIGNATURE, not per byte",
        ]
        if self.is_spl:
            token_account = self.associated_token_address_for(address)
            exists = self.token_account_exists(token_account)
            rent = self.rent_exempt_minimum(TOKEN_ACCOUNT_SPACE)
            plan.update({"token_account": token_account, "token_account_exists": exists, "rent_lamports": rent})
            lines.append(f"    token account   {token_account}")
            lines.append(
                f"    it exists?      {'yes' if exists else 'NO -- creating it costs ' + str(rent) + ' lamports of rent-exempt minimum, paid by whoever signs. That is a fund decision, not a fee.'}"
            )
        plan["description"] = "\n".join(lines)
        return plan

    # --- SPL / wGRC specifics, all read-only ---------------------------------

    def mint_decimals(self) -> int:
        """The configured mint's decimals, read from the mint account.

        NEVER SOL's 9, and never a constant. rule 11: "a value that crosses a
        boundary with the wrong precision is silently wrong by orders of
        magnitude", and a token's decimals are per-mint by definition. wGRC's
        are whatever its mint says, and this asks rather than assuming --
        including in this docstring, which does not state a number because
        nothing here has ever read that mint.
        """
        if not self.is_spl:
            raise SolanaRPCError("mint_decimals() was called on an adapter configured for native SOL, which has no mint")
        result = self.call("getAccountInfo", self.mint, {"encoding": "jsonParsed", "commitment": BALANCE_COMMITMENT})
        value = (result or {}).get("value")
        if not value:
            raise SolanaRPCError(f"mint account {self.mint} does not exist on this cluster. Check SOL_RPC_URL's network.")
        try:
            return int(value["data"]["parsed"]["info"]["decimals"])
        except (KeyError, TypeError, ValueError) as exc:
            raise SolanaRPCError(
                f"account {self.mint} did not parse as an SPL mint: {str(value)[:200]}. "
                "A token account and a mint account are different things and this one is not a mint."
            ) from exc

    def token_program_id_or_default(self) -> str:
        """Which token program owns the configured mint, read from the chain.

        Token-2022 mints derive a DIFFERENT associated token account for the
        same owner and mint. Both answers are well-formed addresses and only
        one of them holds the balance, so guessing is silent. This asks the
        mint account who owns it, and falls back to the original program only
        when there is no mint to ask about.
        """
        if not self.is_spl:
            return TOKEN_PROGRAM_ID
        result = self.call("getAccountInfo", self.mint, {"encoding": "jsonParsed", "commitment": BALANCE_COMMITMENT})
        value = (result or {}).get("value") or {}
        return str(value.get("owner") or TOKEN_PROGRAM_ID)

    def associated_token_address_for(self, owner: str) -> str:
        """The associated token account holding `owner`'s balance of the mint."""
        if not self.is_spl:
            raise SolanaRPCError("associated_token_address_for() needs a mint; this adapter is configured for native SOL")
        return associated_token_address(owner, self.mint, self.token_program_id_or_default())

    def associated_token_account(self, owner: str) -> str:
        """Alias kept deliberately short for the balance path; see the method above."""
        return self.associated_token_address_for(owner)

    def token_account_exists(self, token_account: str) -> bool:
        """True if the token account is already on chain, so no rent is owed to create it.

        This is the read that turns "somebody pays rent" from a hypothetical
        into a number an operator can decide about.

        A ONE-LINE DELEGATION SINCE 2026-10-02, and both names stay on purpose.
        The question "does this account exist" is not token-specific -- the
        native payout path has to ask it about a customer's WALLET, because a
        native transfer to an address with no account CREATES that account and
        the runtime then holds it to a rent-exempt minimum. Writing a second
        getAccountInfo for that would be rule 8's two copies of one read. So
        account_exists() below owns the call, and this name survives because it
        is what the SPL callers and their tests read, and because the rent it
        implies is a DIFFERENT quantity: 165 bytes of token account, not a
        0-byte system account.
        """
        return self.account_exists(token_account)

    def account_exists(self, address: str) -> bool:
        """True if there is an account at this address on this cluster.

        The general form of token_account_exists(), which now calls it. Asked
        with encoding base64 and no data slice because only the EXISTENCE of
        the account matters here -- `value` is null for an address nothing has
        ever been sent to, and on Solana an account that holds nothing IS
        nothing (chains/solana_rpc_map.py's validateaddress note makes the same
        point about valid-versus-in-use).
        """
        result = self.call("getAccountInfo", address, {"encoding": "base64", "commitment": BALANCE_COMMITMENT})
        return bool((result or {}).get("value"))

    def rent_exempt_minimum(self, space: int = TOKEN_ACCOUNT_SPACE) -> int:
        """Lamports an account of `space` bytes needs to be rent-exempt, from the chain.

        ASKED, NOT ASSUMED. chains/solana_units.py carries reference constants
        for this and labels them as reference rather than measurement, because
        nothing in this repository has ever read them off a cluster. This is
        the authority; the constants are what a diagnostic prints as
        "expected".

        Rent is not dust. An account below this minimum is ACCEPTED by the
        chain, exists, and is then collected by the runtime -- the lamports go
        away afterward -- where a dust output is refused by relay and nothing
        is lost. chains/solana_units.py's header has the full argument for why
        that put it outside modules/htlc_fee.py's dust table.
        """
        return int(self.call("getMinimumBalanceForRentExemption", int(space)))

    # --- commitment reporting, read-only -------------------------------------

    def get_slot(self) -> int:
        """The cluster's current slot. A DIAGNOSTIC, never the gate."""
        return int(self.call("getSlot", self._commitment()))

    def signature_status(self, signature: str) -> dict:
        """One signature's rung on the commitment ladder AND its error, separately.

        SPLIT OUT OF commitment_rank_for() ON 2026-10-02 rather than copied,
        and the split exists because the two readers need DIFFERENT answers
        from one response:

          the deposit side   wants a rank, and wants a FAILED transaction to
                             rank 0 forever, because a failed transaction moved
                             nothing and is never creditable however settled it
                             is. That collapse is correct there and is kept.
          the payout side    has to tell "not confirmed yet" from "confirmed
                             and FAILED". Under the collapse they are the same
                             number, so a payout waiting for confirmation would
                             poll a failed transaction until its deadline and
                             then report a timeout -- which reads as "we do not
                             know" when the cluster has already said no.

        So this returns both and commitment_rank_for() below reads the rank out
        of it. One getSignatureStatuses call, one place that unwraps it.
        """
        result = self.call("getSignatureStatuses", [signature], {"searchTransactionHistory": True})
        values = (result or {}).get("value") or [None]
        status = values[0] or {}
        error = status.get("err") if status else None
        if not status or error is not None:
            # A failed transaction moved nothing, so its rank is the same 0 an
            # unknown signature gets. `err` is what distinguishes them, and it
            # is returned rather than folded in.
            return {"rank": commitment_rank(None), "err": error, "known": bool(status)}
        return {
            "rank": commitment_rank(status.get("confirmationStatus")),
            "err": None,
            "known": True,
        }

    def commitment_rank_for(self, signature: str) -> int:
        """The rung this signature has reached on the commitment ladder.

        Not part of the five-method contract -- services/ and workers/ read the
        rank out of the deposit event dicts -- and provided because an operator
        asking "why has this not credited yet" needs to be able to ask about
        one signature without running a whole poll.

        A failed transaction ranks 0 no matter how settled it is; see
        signature_status() above, which this now reads and which keeps the
        error for the one caller that needs to see it.
        """
        return int(self.signature_status(signature)["rank"])

    def describe_signature(self, signature: str) -> str:
        """One pasteable line about a signature's settlement (rule 14)."""
        rank = self.commitment_rank_for(signature)
        return f"{signature}\n    {describe_commitment(rank, self.min_commitment_rank)}"

    # --- announce (rule 14) ---------------------------------------------------

    def endpoint_line(self) -> str:
        """One line describing this adapter, printable before anything is polled.

        The counterpart of workers/common.endpoint_lines() for the three
        Bitcoin-derived chains, and it says the same kinds of things: where it
        points and what its threshold is. It deliberately prints no secret --
        there is none to print, because a Solana RPC URL carries no credentials
        in this configuration, and if an operator puts an API key in the URL
        this line is where they would see it, so it is truncated at the host.
        """
        endpoint = self.url.split("?")[0] if self.url else "(SOL_RPC_URL is UNSET -- every call will refuse)"
        mode = f"SPL mint={self.mint}" if self.is_spl else "native SOL"
        wallet = self.hot_wallet or "(SOL_HOT_WALLET unset -- get_balance() will refuse)"
        return (
            f"  SOL  rpc={endpoint} {mode} wallet={wallet} "
            f"min_commitment_rank={self.min_commitment_rank} "
            f"<- a RUNG on Solana's commitment ladder (3=finalized), NOT a count of blocks"
        )
