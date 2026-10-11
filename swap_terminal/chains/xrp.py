"""The XRP Ledger adapter: the same five-method contract, a third transport.

Role: module (chain binding; transport plus the ledger's own vocabulary)
Reads: a rippled server -- account_info, account_tx, server_info
Writes: nothing to disk. ONE Payment transaction to a NON-MAINNET XRP Ledger,
        and only when a caller passes the exact arming token from
        chains/xrp_signing.py plus a signing seed. Everything else, including
        send_to_address()'s default mode, is a read.
Can move funds: CONDITIONALLY, AND NEVER ON MAINNET. A seed and the arming
        token both have to be passed at the call site, and before either is
        used the SERVER is asked for its network_id and a mainnet id refuses.
        See the four structural properties in send_to_address()'s docstring.

        THIS FIELD USED TO ALSO SAY "AND NEVER BY CONFIGURATION", and that
        stopped being true of the SYSTEM on 2026-10-02 even though it is still
        true of this FILE. Corrected rather than left, because a header is the
        first thing a reader trusts and the gap between those two readings is
        the whole safety argument:

          of this module    unchanged. It holds no key, reads no key path, has
                            no Config field and no RPC dict key that would give
                            it one, and does not import
                            xrp_payout_seed.signing_seed(). Setting an
                            environment variable does NOT let this module sign:
                            send_to_address() still refuses without a seed
                            passed as an argument, which
                            tests/test_xrp_payout_wiring.py asserts by calling
                            it that way with the variable set.
          of the system     CHANGED. services/payout_service.py now reads
                            XRP_PAYOUT_SECRET_SEED from its own environment and
                            passes it here, so a process started with that
                            variable set CAN pay out, unattended. That is what
                            the operator asked for ("we should be able to swap
                            any coin for another of any combination") and it is
                            the same exposure GRIDCOIN_WALLET_PASSPHRASE already
                            carries. chains/xrp_payout_seed.py's header lists
                            every narrowing applied to it.
Mainnet-safe: yes, and stronger than that -- mainnet is UNREACHABLE from this
        module's payout path, including in preview mode. Every read method is
        mainnet-safe in the ordinary sense: they are reads.

THE HONEST STATUS

xrpl.org and s1.ripple.com were unreachable from the environment this was
written in (no response through the proxy), so every RPC METHOD NAME and every
RESPONSE FIELD NAME here came from prior knowledge and was NOT read from the
published API reference. If one is wrong, the tests still pass -- they seed the
shapes this file expects -- and it fails on the operator's first real call.
The way that limitation has been resolved before is by fetching the published
docs from the operator's host and checking the names against them, which is the
first thing to do here too.

CONFIRMED AGAINST A LIVE SERVER ON 2026-09-25, from the operator's host --
rippled 3.4.1 on s.altnet.rippletest.net, network_id 1:

    params as a LIST containing one object     ACCEPTED (HTTP 200)
    result.status == "success"                 CONFIRMED -- this is where
                                               errors live, not the HTTP code
    info.validated_ledger.reserve_base_xrp     1    (a NUMBER, not a string)
    info.validated_ledger.reserve_inc_xrp      0.2
    info.network_id / build_version            1 / "3.4.1"
    meta.delivered_amount                      "2500000" -- a STRING, exactly
                                               as chains/xrp_payments.py reads
    meta.TransactionResult                     "tesSUCCESS"
    Amount                                     "2500000" (never credited)

account_tx WAS THEN CONFIRMED TOO, and it corrected an overstatement. A second
run, xrp_chain_check.py against the same server, examined 20 entries and 10
Payments on a real account:

    account_tx             transaction nested under `tx`     <- the shape the
                                                             ORIGINAL code
                                                             already handled
    ledger (expand=true)   transaction FLAT on the entry
    hash, TransactionType, Destination, TransactionResult,
    delivered_amount       present in all 10
    DestinationTag         present in 1 of 10  <- the field DOES appear on real
                                                 traffic; the rest is ordinary
                                                 wallet-to-wallet

The flat shape had been reported as a bug that "would have lost deposits". It
would not have: this module calls only account_tx, which nests under `tx`. The
impact was inferred from one method's response and stated as measured, which is
rule 17's failure inside a change about that failure. The wider _unwrap() stays
because xrp_chain_check.py does call `ledger` -- see the full correction in
chains/xrp_payments.py::_unwrap.

Also measured: chains/xrp_address.py against the ledger's own ACCOUNT_ZERO and
ACCOUNT_ONE constants, 2,000 round trips and every single-character mutation
rejected by checksum.

TWO THINGS ABOUT rippled's JSON-RPC THAT ARE EASY TO GET WRONG

1. `params` IS AN ARRAY CONTAINING ONE OBJECT. Not a bare object, and not a
   positional list as bitcoind takes. The shape is
   {"method": "...", "params": [{...}]} and sending either of the other two
   forms produces an error about parameter parsing, several layers from the
   cause.

2. AN ERROR ARRIVES AS HTTP 200. rippled reports failures inside the response
   body -- result.status == "error" with result.error naming it -- while the
   HTTP status stays 200. A client that only checks raise_for_status() treats
   every failure as a success and reads whatever fields happen to be absent as
   empty. That is the shape this whole codebase keeps being bitten by, so it is
   checked explicitly below.

HOW PAYOUTS WORK, AND WHAT MAKES THEM UNARMED BY DEFAULT

rippled removed transaction signing from its public API. There is no `sign`
method to call on a remote server, deliberately: signing there would mean
sending a secret key over the wire. So paying out XRP means THIS process holds
a signing key and signs locally.

UNTIL 2026-09-26 THIS MODULE SIMPLY REFUSED, and the refusal was an absence
rather than a flag: no signing library was imported and no key path was read,
so it could not have signed if the check had been deleted. That was honest and
it was also a dead end -- it left the operator no way to inspect what a payout
WOULD be before deciding whether to allow one.

What replaced it is a mechanism that is structurally incapable of sending on
mainnet and cannot be armed by configuration alone. In one list, with the
argument for each spelled out at length in send_to_address()'s docstring and in
chains/xrp_signing.py:

  the default is a PREVIEW   send_to_address(address, amount) reads the server,
                             converts the amount, checks the reserve, prints
                             the whole plan, and then REFUSES. A caller that
                             forgets to arm it cannot send.
  mainnet is refused by ID   the network comes from server_info.network_id, not
                             from the url, because a hostname resolves to
                             whatever DNS says today. A missing or unreadable id
                             refuses too: not reading the network is not the
                             same as reading a safe one.
  the arming token           an exact string, not a boolean, so no truthy value
                             or drifted positional argument can supply it.
  no key in THIS file        no seed, no key path, no Config field and no RPC
                             dict key, and signing_seed() is not imported. The
                             seed is an argument. Setting the environment
                             variable does not arm THIS module -- only a caller
                             that passes the value can.

That leaves the custody question answered by the operator rather than here, and
2026-10-02 is the day they answered it: a payout worker started with
XRP_PAYOUT_SECRET_SEED set holds an XRP hot wallet and can spend it, and
XRP_DEPOSIT_ACCOUNT names which account. Both are unset by default and both
refuse rather than guessing, so the decision is made by supplying the values and
not by deploying this file. (This paragraph used to end "nothing here is wired to
the payout worker", which was true until the call site was wired the same day. It
is corrected rather than deleted because the sentence is the one a reader would
have trusted to conclude this path is dead.)

get_new_address() also refuses, but for a happier reason. XRP does not need
one: a DESTINATION TAG is a per-swap identifier on a single account, costs
nothing, creates no key, and is what every exchange on this ledger uses. The
attribution problem does not exist here -- what it needs instead is a tag
allocator, which is a services/ change rather than an adapter one. (Nor, since
2026-09-29, does it exist on Solana: the operator chose the same shape there,
one shared account plus a per-swap Memo instruction, so the two chains now
differ only in what the field carrying the integer is CALLED. This sentence said
"the attribution problem chains/solana.py had to hand back", which was the
difference between them and is no longer one.)

THAT ALLOCATOR EXISTS AND IS NOW WIRED, at services/xrp_tag_service.py, called
by services/swap_service.py::create_swap(). This refusal still stands, and the
reason is unchanged by the wiring: every other chain's deposit instruction is
ONE address and XRP's is the PAIR (account, tag). Returning the account alone
from here would satisfy the method signature while handing the customer half of
an instruction -- a payment to the right account with no tag is exactly the case
chains/xrp_payments.py reports as deferred and cannot credit. The pair is stored
as `swaps.deposit_address` plus `swaps.deposit_tag`, and assembling it is
create_swap()'s job rather than this method's, which is why get_new_address()
refuses instead of being taught about tags.

(This paragraph said "the reason is written out in full under WHAT IS NOT WIRED
in that module" and that `swaps.deposit_address is one column`. Both were true
when written and both were overtaken -- the section is now WHAT IS WIRED and
`deposit_tag` is the second column. Corrected 2026-10-01 rather than left, per
rule 16: a wrong comment is a bug, and this one pointed a reader at a heading
that no longer exists to learn why something was not done that has been done.)
"""

from __future__ import annotations

import json
import time

import requests

# Rootless, matching workers/common.py:60, and NOT a relative import.
#
# `from ..microfortnights import ...` was the first spelling and it fails with
# "attempted relative import beyond top-level package" -- measured, it broke
# collection of three test files. The application imports its own modules
# rootlessly (`from config import Config`), so `chains` is itself a top-level
# package and there is no parent to go up into. That is rule 10's layout gap.
#
# Deliberately NOT fixed with the `sys.path.insert` that chains/base.py uses for
# script_pub_key: an import-time side effect is the one thing rule 12 names that
# a linter cannot check, and a second copy of it would make import order matter
# in one more place. This import needs no path help, because anything that can
# import `chains.xrp` at all already has swap_terminal/ on the path.
from microfortnights import format_duration

# THE SHAPE A STRANDED DEPOSIT IS RECORDED IN, IMPORTED AND NOT RE-DECLARED HERE.
#
# services/deposit_service.record_what_nobody_can_claim() reads `unattributable_drops`
# off the adapter by NAME and hands the list to
# services/unattributable_deposit_service.stranded_rows(), which reads five attributes
# off each element: `signature`, `credits`, `why`, `amount`, `address`. So the element
# type is not a detail this module may choose -- it is the contract that writer already
# speaks, and chains/solana.py is where it is declared.
#
# A SECOND NamedTuple WITH THE SAME FIELDS WOULD HAVE BEEN LESS WORK AND IS RULE 8'S
# DEFECT WITH A DELAY ON IT, on exactly the record that says HOW MUCH MONEY IS
# STRANDED. UnattributableCredit's own docstring records the version of itself that
# carried only a COUNT and no amount -- "a record of money that omits the amount is not
# a record of money" -- which is the kind of change a parallel copy here would not have
# received. Rule 8: let the survivor own the concept.
#
# =============================================================================
# THE COUPLING THIS IMPORT CREATES, AND THE DECISION NOT TO REMOVE IT YET
# =============================================================================
#
# NAMED HERE SO THE NEXT READER FINDS A DECISION RATHER THAN REDISCOVERING A QUESTION.
# This line makes the XRP adapter depend on the SOLANA adapter, which is a dependency
# between two siblings that should not know about each other, and it is deliberate.
#
# AS OF 2026-10-11 THE TYPE BELONGS TO NEITHER CHAIN. It used to be Solana's alone:
# chains/solana.py declared it, recorded drops on itself, and the only consumer was its
# own diagnostic script. It is now the shared contract between any adapter and
# services/deposit_service.record_what_nobody_can_claim(), which reads
# `getattr(adapter, "unattributable_drops", None)` GENERICALLY -- by name, with hasattr
# semantics, deliberately not by isinstance or a per-chain list, so that a new
# tag-attributed adapter joins by growing the attribute. A type that two adapters
# produce and one service consumes is not either adapter's.
#
# SO ITS HOME SHOULD BE A SMALL SHARED chains/ MODULE THAT NO CHAIN OWNS, and this tree
# already has that pattern twice: chains/coin_amounts.py and chains/script_pub_key.py.
# The second is the closer precedent -- it exists precisely because one fact (how each
# daemon spells a decoded output's address) was being re-derived per chain, and it was
# found wrong in a FOURTH place before it got a home. This is the same shape one step
# earlier: get it a home before there is a third producer.
#
# NOT MOVED IN THIS CHANGE, AND THAT IS A CHOICE WITH A REASON RATHER THAN AN OMISSION.
# The move is not a move: it is a new module, an edit to chains/solana.py (which must
# re-export or import it so nothing else breaks), and an edit to every importer that
# names it directly -- solana_chain_check.py plus tests/test_deposit_rate_limit.py,
# tests/test_solana_chain_check_units.py and tests/test_unattributable_deposits.py. That
# is a refactor across a fund-moving adapter, and smuggling it inside a defect fix would
# mean the commit that closes a measured money hole also moves a type across five files
# -- so a bisect could not tell which half broke anything. It is named work, not a
# baseline (rule 19).
#
# A SECOND NamedTuple WITH THE SAME FIELDS HERE WAS THE OTHER OPTION AND IS WORSE than
# the coupling, which is the trade this comment exists to record. Rule 8: the copies
# agree the day they are written and drift from then on, and the drift here lands on the
# field that says HOW MUCH MONEY IS STRANDED -- UnattributableCredit's own docstring
# records the version of ITSELF that carried only a count and no amount. A parallel copy
# in this file would not have received that fix. One definition and an ugly import beats
# two definitions and a clean one.
from .solana import UnattributableCredit
from .xrp_address import describe_address, is_valid_classic_address, looks_like_x_address

# EVERY NAME HERE IS PUBLIC, AND THAT IS A DELIBERATE CONSTRAINT RATHER THAN AN
# ACCIDENT. untagged_payments() below re-asks chains/xrp_payments.py which entries it
# held back instead of re-deciding it, so this module never grows a second copy of
# "is this a Payment to us, did it succeed, did it carry a tag" -- the rule whose only
# correct home is that file. See that function's docstring for why calling the same
# public entry point per entry is cheaper to be right about than reading the fields.
#
# =============================================================================
# `_unwrap` IS IMPORTED ACROSS A MODULE BOUNDARY BY ITS PRIVATE NAME, AND THAT
# UNDERSCORE NOW TELLS A READER THE OPPOSITE OF THE TRUTH
# =============================================================================
#
# Said plainly rather than left for someone to notice. A leading underscore means "this
# module may change this freely"; two modules using it means it cannot be changed freely.
# One of those two statements is false, and it is the underscore.
#
# THE REMEDY IS IN THAT FILE AND IS NAMED WORK, NOT A BASELINE (rule 19). Either
# `_unwrap` loses its underscore -- it is already the measured answer to a question two
# callers have -- or chains/xrp_payments.py grows a public `transaction_body(entry)` that
# returns the same pair, with `_unwrap` becoming its one-line caller. Either settles it;
# both are edits to that module and belong with its own tests rather than inside this
# one. Until then THIS COMMENT is the interface declaration the underscore contradicts.
#
# WHAT IT IS USED FOR, AND IT IS A FIELD LOOKUP RATHER THAN A DECISION.
# untagged_payments() needs two values no public function there returns -- the
# transaction HASH, which becomes the row's identifier, and the TransactionResult, which
# says which of the classifier's two ordered refusals fired. Both live under a nesting
# that was MEASURED on rippled 3.4.1 (`account_tx` nests the body under `tx`, `ledger`
# with expand=true puts it flat beside `metaData`, and the v2 API calls it `tx_json`) and
# that was MISCHARACTERIZED once already in this tree, which `_unwrap`'s own docstring
# records against itself.
#
# THE ALTERNATIVE WAS A LOCAL COPY OF THAT NESTING AND IT IS WORSE, which is the trade
# being made: a second place to get a measured wire shape wrong, on the value that
# becomes a money row's primary key, against one honest line of coupling. The copy would
# also lose `_unwrap`'s RAISE on a shape it does not recognize, which is the behavior
# that turns an unreadable response into a visible error instead of {} -- and {} reads to
# a caller as "not a Payment", indistinguishable from a real answer.
#
# IT CANNOT RAISE WHERE IT IS CALLED, because every entry reaching it has already been
# unwrapped once, without raising, by the whole-list scan above it.
#
# FIELD_HASH, FIELD_TRANSACTION_RESULT AND SUCCESS_RESULT ARE PUBLIC and are imported for
# the same reason: so the field names and the one success code live in the module that
# measured them against a real server, not as literals here.
from .xrp_payments import (
    FIELD_HASH,
    FIELD_TRANSACTION_RESULT,
    SUCCESS_RESULT,
    XRPPaymentError,
    _unwrap,
    delivered_drops_to,
    deposit_events_from_transactions,
)

# EVERY DECISION THE PAYOUT PATH MAKES IS IMPORTED, NOT SPELLED HERE. The
# functions below are the guards, they live one layer down in the function layer
# (rule 10), and they are each callable with seeded arguments -- which is what
# makes it possible to disable one at a time and watch a test fail, rather than
# having to run a whole send to find out whether a check is load-bearing.
# THE SEED'S VARIABLE NAME, WHETHER ONE IS PRESENT, AND WHETHER IT DECODES --
# AND DELIBERATELY NOT ITS VALUE.
#
# signing_seed() lives in that module too and is NOT imported here. That is the
# property that keeps this file unable to sign on its own: the environment can say
# that a seed exists AND that it decodes, which is what payout_capability() answers
# as a bool plus a sentence, while the value reaches send_to_address() only as an
# argument from services/payout_service.py. An import of signing_seed() here would
# make configuration alone sufficient to arm a payout, and
# tests/test_xrp_payout_wiring.py fails behaviorally if one is ever added -- the
# variable set, no seed argument, and the refusal is the assertion.
#
# payout_capability() DECODES, as of 2026-10-03, and that is the change that matters
# here: can_spend below used to be signing_seed_is_present(), which is presence. The
# two narrower functions are still imported -- the banner needs to tell three states
# apart (no variable / a value that is not a seed / a seed), and one bool cannot.
from .xrp_payout_seed import (
    SIGNING_SEED_ENV_VAR,
    missing_seed_refusal,
    payout_capability,
    signing_seed_decodes,
    signing_seed_is_present,
)
from .xrp_signing import (
    FEE_ALLOWANCE_DROPS,
    XRPSendNotArmed,
    derive_and_check,
    network_id_name,
    refuse_partial_payment,
    require_non_mainnet,
    require_reserve_headroom,
    require_send_confirmation,
    reserve_drops,
)
from .xrp_units import (
    REFERENCE_BASE_RESERVE_XRP,
    describe_min_confirmations,
    from_drops,
    from_drops_decimal,
    to_drops,
    to_drops_round_half_up,
    validate_min_confirmations,
)

# UNVERIFIED METHOD NAMES -- see THE HONEST STATUS above.
_METHOD_ACCOUNT_INFO = "account_info"
_METHOD_ACCOUNT_TX = "account_tx"
_METHOD_SERVER_INFO = "server_info"


class XRPRPCError(Exception):
    """A rippled call failed, or the server returned an error in a 200 body.

    Raised rather than swallowed: on this path a failure that returns a
    plausible value -- 0 from get_balance, [] from find_deposits_to_address --
    is worse than one that stops the caller, because an empty deposit list
    reads as "no money has arrived yet" and stalls a swap silently forever.
    """


# XRPPayoutDisabled USED TO BE DEFINED HERE and it is deleted rather than kept
# (rule 9: every time you are in a file, leave less of it behind; rule 2:
# delete, do not quarantine -- git history is the archive). Its whole meaning
# was "this module cannot sign and will not try", which stopped being true on
# 2026-09-26, and a class kept for compatibility would be a name a reader finds
# and reasons about before discovering nothing raises it.
#
# What replaced it is FOUR named refusals in chains/xrp_signing.py --
# XRPMainnetRefused, XRPSendNotArmed, XRPReserveRefused and
# XRPPartialPaymentRefused -- because an operator reading a failure needs to
# tell "this is mainnet" from "you did not arm it" from "this breaches the
# reserve" from "the flags would let it under-deliver", and three of those four
# do not mean "retry".
#
# Proven dead before deleting, not assumed (rule 2): grepped the whole tree for
# the NAME rather than the import graph, and the only occurrences were its own
# definition and the single raise in the old send_to_address(). Nothing in
# tests/, scripts, or the two root-level chain-check files named it.


def new_deferrals(deferred: list[str], already_reported: set[str]) -> list[str]:
    """The deferred lines not yet reported, MUTATING already_reported to include them.

    Pure enough to test without a server, which is the point of extracting it
    (rule 10): the decision is "has this been said already", and that is the whole
    behavior worth pinning.

    Deduped on the WHOLE LINE rather than on a parsed txid. The line is built by
    chains/xrp_payments.py and carries the txid plus the reason, so two different
    reasons for one txid are two different facts and both deserve saying -- and
    parsing a txid back out of a formatted string would couple this to that
    format for no gain.
    """
    fresh = [line for line in deferred if line not in already_reported]
    already_reported.update(fresh)
    return fresh


def untagged_payments(
    entries, address: str, min_confirmations: int
) -> tuple[list[UnattributableCredit], list[str]]:
    """Payments that arrived with NO DestinationTag, as records a table can hold.

    Returns (credits, lines) -- the drops a durable row can be written from, and the
    payments that arrived untagged and whose amount could NOT be established, as
    sentences for the console. BOTH, because "nothing arrived untagged" and "something
    arrived untagged and this function could not say how much" are different facts and
    rule 14 forbids rendering them the same way.

    PURE: the entries are passed in, so it is callable with a seeded account_tx response
    and no server (rule 10). It is the whole decision this fix adds, which is why it is a
    module-level function and not three lines inside the transport method.

    =========================================================================
    THE DEFECT, MEASURED END TO END 2026-10-11
    =========================================================================

    A customer sends XRP to XRP_DEPOSIT_ACCOUNT and omits the DestinationTag.
    chains/xrp_payments._classify() correctly refuses to guess which swap it is for and
    returns (None, reason), so it never becomes a deposit event. From there it reached NO
    TABLE AT ALL. Measured by seeding one tagged and one UNTAGGED validated Payment into
    the real XRPAdapter with requests.post replaced, then running the real
    services/deposit_service functions against the real db.py SCHEMA:

        events returned                            1   (the TAGGED one only)
        getattr(adapter, "unattributable_drops")    <<ATTRIBUTE ABSENT>>
        record_what_nobody_can_claim(db, "XRP")     0   rows written
        deposit_events                              0 rows
        unattributable_deposits                     0 rows
        late_deposits                               0 rows
        reconcile_shared_accounts(...)              1 row -- for the TAGGED payment whose
                                                    tag matched no swap. `txid` of the
                                                    untagged payment present? False

    So 2.5 XRP arrived, was real, and existed in swap_terminal.db nowhere. Both database
    paths need something this adapter did not produce:

      record_what_nobody_can_claim()   reads `unattributable_drops` off the adapter BY
                                       NAME (hasattr, deliberately -- the three UTXO
                                       adapters cannot have unattributable deposits at
                                       all). XRPAdapter had no such attribute, measured:
                                       hasattr(XRPAdapter, "unattributable_drops") is
                                       False and a constructed instance's vars() were
                                       {_reported_deferrals, can_spend, min_confirmations,
                                       payout_refusal, timeout, url}.
      reconcile_shared_accounts()      hands `events` to unclaimed_rows(), which reads
                                       event["vout"] and SKIPS a None -- its own comment
                                       says "NO DISCRIMINATOR AT ALL is the adapter's
                                       case, not this one". It is right, and the adapter
                                       was not holding up its end.

    The only trace was the `print` in find_deposits_to_address(), filtered through
    `_reported_deferrals`, so it was emitted once per adapter instance and then never
    again until a worker restart -- and nothing on /admin, in show_unattributable.py or
    in any report can see it, because all of those read the table. Rule 5: a measurement
    that only exists in a log is not learning, on the one path where the measurement is
    somebody's deposit.

    THIS IS SOL'S CASE AND SOL ALREADY HAD IT. chains/solana.py appends an
    UnattributableCredit for a credit carrying no usable Memo and deposit_service writes
    the row with no change at its call site. Rule 8: the concept exists, so this grows
    the same attribute rather than inventing a second mechanism.

    =========================================================================
    WHY THE CLASSIFICATION IS RE-ASKED RATHER THAN RE-DECIDED
    =========================================================================

    The obvious simpler version reads the fields directly -- TransactionType == "Payment",
    Destination == address, meta.TransactionResult == "tesSUCCESS", DestinationTag is
    None -- and it is wrong because that conjunction IS
    chains/xrp_payments._classify(), and a second copy of it is rule 8's bug with a delay
    on it on the money path. The two would agree the day this was written. The day
    somebody adds a fifth condition to the classifier, they would not, and nothing would
    fail: the credit path would hold a payment back while this path recorded it as
    stranded, or the reverse.

    So each entry is handed back to `deposit_events_from_transactions`, the SAME public
    entry point the credit path calls, one entry at a time, and the verdict is read off
    the result:

        events non-empty     it became a deposit event, so it carried a tag. Not ours.
        deferred empty       `_classify` returned (None, None) -- not a Payment, or a
                             payment OUT of this account. Not ours.
        otherwise            ours, and held back. `_classify` has exactly two refusals
                             and they are ORDERED: a non-tesSUCCESS result returns BEFORE
                             the tag is looked at. So a held-back entry is either a tec*
                             result or an untagged success, and ONE field comparison --
                             not a second classifier -- says which.

    THE tec* HALF IS DROPPED SILENTLY AND THE UNTAGGED HALF IS NOT, and that asymmetry
    was a DEFECT IN THIS FUNCTION on the day it was written. See the comment at the
    comparison itself: a tec* code claims a fee and transfers nothing, so printing "no
    row was written for it" sends an operator looking for coins that do not exist, while
    the deferred line has already reported the failure. The mixed-list test in
    tests/test_xrp_unattributable_deposits.py is what found it, which is the argument for
    writing that test at all -- every single-case test passed with the defect in place.

    IT CANNOT RAISE WHERE THE WHOLE-LIST CALL DID NOT, and that is what makes the
    per-entry call safe rather than a second place a scan can die. `_classify` is
    per-entry, and the only whole-list check -- `_reject_duplicate_tags` -- cannot fail
    over one entry. find_deposits_to_address() calls the whole list FIRST, so by the time
    this function runs every entry has already been parsed once without raising. A
    one-element list is strictly fewer code paths, not different ones.

    THE COST IS N PURE-PYTHON CALLS AND NO NETWORK. `account_tx` is one request and the
    entries are already in memory; nothing here opens a socket. A scan of 200 entries
    does 200 more passes over one dict each, which is not measurable beside the HTTP call
    that fetched them.

    =========================================================================
    WHAT GETS A ROW, AND WHAT DELIBERATELY DOES NOT
    =========================================================================

    `delivered_drops_to()` is the public reader for "what did the ledger actually move",
    and it is used rather than a field read for the reason its own module's header gives
    at length: `meta.delivered_amount` versus `Amount` is the partial-payment exploit
    that has drained real exchanges, and a second module reading that figure for itself
    is rule 8's duplicate on the single most expensive field this tree knows. It also
    requires `validated is True`, which this function WANTS and does not merely tolerate:

      an untagged payment in an    gets NO ROW THIS CYCLE and a console line saying so.
      unvalidated ledger           An unvalidated ledger can still change, so writing a
                                   money record from it would be recording an amount that
                                   may not have arrived. The watcher polls, the ledger
                                   validates in seconds, and the next cycle writes the
                                   row. The delay is a few seconds; the alternative is a
                                   row that can turn out to be about nothing.
      an untagged payment          gets NO ROW, and a console line. The amount column is
      delivering an ISSUED         whole units of XRP, and a stranger's IOU recorded there
      CURRENCY, or with no         would be a number under the wrong label. There is no
      delivered_amount at all      XRP figure to record, so none is invented (rule 17).

    AND chains/solana.py DECIDED THE WRONG-ASSET CASE THE OTHER WAY ON THE SAME DAY, so
    the difference is named at this site per rule 8. On 2026-10-11 that adapter stopped
    letting SOL_SPL_MINT select its DEPOSIT reader -- a SOL deposit is native SOL -- and
    a configured SPL token arriving instead now gets an UnattributableCredit ROW, with
    `amount` at the MINT's decimals and a `why` that says in so many words that it is not
    a SOL deposit. Two tag-attributed chains, one question, two answers, and these are
    the reasons rather than an oversight:

      what can be read   that adapter already has a measured token reader
                         (`_spl_credits`, proven against a real devnet response) that
                         returns a quantity. Here, `meta.delivered_amount` for an IOU is
                         an OBJECT -- {currency, issuer, value} -- and
                         chains/xrp_payments.py REFUSES to put a number on it rather than
                         parsing `value`. Recording one would mean this file growing a
                         second reader of the field whose single owner is the whole point
                         of that module's header. That is a bigger rule 8 cost than the
                         gap it closes.
      what the number    an SPL mint is a token this terminal is CONFIGURED for, so the
      would mean         row names a known asset and a known decimals. An XRP IOU is
                         minted by whoever sent it, in a currency code they chose, and
                         `SELECT SUM(amount) WHERE asset = 'XRP'` would add it to an XRP
                         total.

    SO THE REMEDY IS NAMED, NOT BASELINED (rule 19): chains/xrp_payments.py gaining a
    PUBLIC reader for the issued-currency shape -- currency, issuer and value, returned
    as what they are rather than as an amount -- after which this case can be recorded
    with the issuer in `why` and the two chains agree. That is an edit to that module and
    belongs with its own tests. Until then the console line is the only report, and that
    is strictly better than the nothing it replaced but it is not the table.

    NEITHER OF THOSE IS SILENT, and that is the whole point of returning two lists. A
    bare `except XRPPaymentError: continue` here would be rule 12's forbidden shape -- a
    caught failure the caller cannot tell from a real answer -- on the path where the
    failure means somebody's deposit has no record. The handler's output IS the return
    value's second half.

    THE REFUSAL IS CAUGHT NARROWLY AND IT MUST NOT BE WIDENED. XRPPaymentError and
    nothing else: the entries have already survived one parse, so the only refusals
    reachable here are the four delivered_drops_to() states above, and a broader catch
    would swallow a programming error in this function as though a customer had sent an
    IOU.
    """
    credits: list[UnattributableCredit] = []
    unreadable: list[str] = []
    for entry in entries or []:
        # ONE ENTRY THROUGH THE REAL CLASSIFIER. See the docstring: the verdict is read
        # off the same function the credit path uses, never re-derived from the fields.
        one = deposit_events_from_transactions([entry], address, min_confirmations)
        if one.events or not one.deferred:
            continue
        # EXACTLY ONE LINE, BY CONSTRUCTION. `_classify` returns at most one reason per
        # entry, and the second thing that can append to `deferred` -- an event in a
        # not-yet-validated ledger -- only runs when an event exists, which the branch
        # above has already excluded. So this is the classifier's own refusal sentence.
        reason = one.deferred[0]
        # ===================================================================
        # WHICH OF THE CLASSIFIER'S TWO REFUSALS WAS IT. THIS IS A
        # DISAMBIGUATION AND NOT A SECOND COPY OF THE CLASSIFICATION
        # ===================================================================
        #
        # `_classify` has exactly two refusals and they are ORDERED: a result that is not
        # tesSUCCESS returns BEFORE the tag is looked at. So a held-back entry is either
        # a tec* result or an untagged success, and one comparison tells them apart.
        # Everything that DECIDES -- is it a Payment, is it to us, did it succeed -- was
        # already decided by the call above; this reads the field to learn which answer
        # came back, through the constants that module exports rather than literals.
        #
        # A tec* PAYMENT IS NOT RECORDED AND IS NOT ANNOUNCED HERE EITHER, and getting
        # that wrong is a defect this function HAD on 2026-10-11 before
        # tests/test_xrp_unattributable_deposits.py's mixed-list test caught it. Without
        # this branch a tecUNFUNDED_PAYMENT fell into the `except XRPPaymentError` handler
        # below and printed "AND NO unattributable_deposits ROW WAS WRITTEN for it", which
        # is true and actively misleading: a tec* code is included in a ledger, claims a
        # fee and transfers NOTHING, so there is no money to record and nothing for an
        # operator to go looking for. The deferred line above has already said it failed.
        # Rule 14 cuts both ways -- a line that invites an operator to chase coins that do
        # not exist is as much a defect as silence about coins that do.
        tx, meta = _unwrap(entry)
        if meta.get(FIELD_TRANSACTION_RESULT) != SUCCESS_RESULT:
            continue
        # THE HASH, from the body this function has already unwrapped. The `or` fallback
        # is `_classify`'s own, for the flat nesting where the hash sits beside the body
        # rather than in it -- see _unwrap()'s docstring for the three measured shapes.
        tx_hash = str(tx.get(FIELD_HASH) or entry.get(FIELD_HASH) or "")
        try:
            drops, _intended = delivered_drops_to(entry, address)
        except XRPPaymentError as refusal:
            # NOT A ROW, AND SAID OUT LOUD. By here the entry is a SUCCESSFUL, UNTAGGED
            # Payment to this account, so only two of delivered_drops_to()'s refusals are
            # reachable: a ledger that has not validated yet, and a delivered_amount that
            # is not an XRP drop string (an issued currency, or absent). Both are
            # "arrived, untagged, and this function cannot put a number on it", which is
            # a different sentence from the row that gets written and must not print like
            # one (rule 14).
            unreadable.append(
                f"{reason}  AND NO unattributable_deposits ROW WAS WRITTEN for it this "
                f"cycle: {refusal}"
            )
            continue
        # `_intended` IS READ AND DISCARDED ON PURPOSE. delivered_drops_to() returns
        # `Amount` second so a caller can NOTICE the two disagree, which on an incoming
        # payment means it carried tfPartialPayment. It is not compared here because
        # nothing downstream would act on it: `drops` is the authority, it is the only
        # figure that reaches the row, and a mismatch changes neither the amount recorded
        # nor the fact that nobody can claim it. Naming it `_intended` rather than
        # dropping it with `[0]` keeps the reader from assuming the second element is
        # some other, better amount.
        credits.append(UnattributableCredit(
            # THE LEDGER'S WORD FOR IT IS `hash` AND THE FIELD IS CALLED `signature`,
            # because the type is declared in chains/solana.py (see the import). Same
            # thing: the transaction's unique identifier, which is what the row's `txid`
            # column holds for both chains.
            signature=tx_hash,
            # ONE, ALWAYS, AND IT IS NOT A PLACEHOLDER. `credits` counts how many separate
            # credits in one transaction made up the amount, and it exists because a
            # Solana transaction CAN carry several to one account. An XRP Ledger Payment
            # has exactly one Destination and delivers exactly once -- which is the same
            # property chains/xrp_payments.py relies on to use the tag as `vout` -- so
            # one is the measured count here and not a default.
            credits=1,
            why=_why_from(reason, tx_hash),
            # THROUGH from_drops(), NEVER a literal 1_000_000 at this call site. Rule 11
            # asks for the base-unit conversion to happen in one function, and
            # chains/xrp_units.py is that function for this chain -- it also accepts the
            # drop STRING the ledger sends, which is the protection a multiplication here
            # would quietly drop.
            amount=from_drops(drops),
            # THE SHARED ACCOUNT, carried rather than looked up later, so the row still
            # says where the coins are after XRP_DEPOSIT_ACCOUNT is changed.
            address=address,
        ))
    return credits, unreadable


def _why_from(reason: str, tx_hash: str) -> str:
    """The classifier's refusal, minus the hash it starts with. PURE.

    db.py says of `unattributable_deposits.why`: "the adapter's own reason, in its words,
    so the row and the log line agree". So the sentence is DERIVED from the line
    chains/xrp_payments.py already built rather than written a second time here -- two
    sentences for one refusal would agree today and drift on the first edit to either
    (rule 8), and the row and the console would then describe the same money differently.

    THE HASH IS REMOVED BECAUSE THE ROW ALREADY HAS IT in the `txid` column, and
    show_unattributable.py prints both on adjacent lines. `removeprefix` with the exact
    hash this function was handed, NOT a parse: there is no pattern to match wrongly. If
    that format ever changes so the line no longer starts with the hash, this is a no-op
    and the `why` is the full line -- redundant and still correct, which is the right way
    for a cosmetic step to fail.
    """
    return reason.removeprefix(tx_hash).strip()


class XRPAdapter:
    asset = "XRP"

    # SEE chains/base.RPCAdapter.can_spend for why this name and not another.
    #
    # THESE TWO CLASS-LEVEL VALUES ARE THE FAIL-CLOSED FALLBACK AND ARE DELIBERATELY
    # THE REFUSING PAIR. __init__ overrides both per instance from
    # chains/xrp_payout_seed.signing_seed_is_present(), so anything that reads them
    # off the CLASS -- a stub, a reflective check, a reader who never constructed an
    # adapter -- gets "cannot pay out". That is the safe direction, and it is the
    # same direction chains/registry.why_cannot_pay_out() already fails in when the
    # attribute is missing entirely.
    #
    # WHAT THIS USED TO SAY, AND WHY IT HAD TO CHANGE (2026-10-02). Both values were
    # hardcoded and the sentence read "it holds no signing key, and
    # services/payout_service.py calls send_to_address() without the arming token".
    # BOTH CLAUSES WERE TRUE AND THEY NAMED TWO DIFFERENT BLOCKERS. The first was the
    # real one: there was no environment variable, no Config field, no RPC dict key
    # and no path of any kind by which a seed could reach this adapter, so arming the
    # call site ALONE would have turned one refusal into another. The second was only
    # the call site, and it is now wired --
    # services/payout_service.broadcast_payout() passes the source account, the seed
    # and the arming token.
    #
    # So the refusal is now DERIVED from the one question that remains -- is a
    # signing seed present in this process -- rather than written out. Both halves of
    # the old sentence became false the moment the call site was wired, and a
    # customer page printing a confident false reason is rule 16's wrong comment on a
    # surface a customer reads.
    #
    # THE HAZARD THE OLD COMMENT RECORDED IS UNCHANGED, AND IT IS WHY THE DEFAULT
    # STILL REFUSES. ALLOWED_PAIRS gained ('GRC','XRP') on 2026-09-26 on the
    # operator's instruction, so the swap page offers GRC -> XRP. A customer
    # following that path while nothing can sign would send GRC, have it credited,
    # and then watch the payout refuse permanently with their deposit already taken
    # -- a stranded swap needing a person. With the seed unset, which is every
    # checkout and every test run, services/swap_service.create_swap() still refuses
    # before the swap row exists, which is the only stage at which nothing has been
    # taken.
    #
    # The second hazard the old comment named is also unchanged: validate_address()
    # accepts ANY X-address without checking its checksum. It is closed one layer up
    # rather than here -- preview_payout() refuses an X-address outright, and
    # modules/address_authority.py decodes the destination before the send -- so
    # enabling payouts does not reopen it.
    can_spend = False
    payout_refusal = missing_seed_refusal()

    def __init__(
        self,
        url: str = "",
        min_confirmations: int = 1,
        timeout: float = 30.0,
    ):
        if not url:
            raise XRPRPCError("XRPAdapter needs a rippled JSON-RPC url; there is no sensible default")
        self.url = url
        self.timeout = float(timeout)
        # WHETHER THIS PROCESS CAN SIGN, SETTLED ONCE PER ADAPTER AND NOT PER CALL.
        #
        # Read here rather than in a property for a measured reason: on 2026-10-02 the
        # defect this replaces was the spawn banner and the customer page disagreeing
        # about XRP IN ONE PROCESS. A property re-reads os.environ on every access, so
        # two surfaces rendered from the same adapter could still answer differently if
        # anything mutated the environment between them. One read at construction makes
        # that impossible: every surface in a process sees the same answer, and the
        # answer is the one the shell that started it supplied.
        #
        # NO SEED IS STORED, ONLY A BOOLEAN AND A SENTENCE. payout_capability()
        # returns exactly that pair: a bool, and a refusal that names the VARIABLE
        # and the failure KIND, never a value. tests/test_xrp_adapter.py walks vars()
        # on a constructed instance and asserts no attribute name or value looks like
        # key material, so this is checked by construction rather than by this
        # comment.
        #
        # IT DECODES THE SEED NOW, AND UNTIL 2026-10-03 IT DID NOT. This line was
        #
        #     self.can_spend = signing_seed_is_present()
        #
        # which reads PRESENCE. Measured on the operator's host that day:
        # XRP_PAYOUT_SECRET_SEED held NINE CHARACTERS that do not start with 's' -- a
        # placeholder -- so can_spend was True, chains/registry.why_cannot_pay_out()
        # returned "", services/swap_service.create_swap()'s cannot_pay gate passed,
        # and this terminal considered XRP a payout destination it could serve.
        # chains/xrp_signing.py:397 derives the signing wallet with
        # Wallet.from_seed(), which raises ValueError on that value, so EVERY XRP
        # payout would have failed at signing -- after the customer's deposit was
        # confirmed and irreversible, which is the one state this whole file is
        # arranged to prevent. The only thing that stopped it was XRP_DEPOSIT_ACCOUNT
        # being unset, a different gate, and the operator was in the middle of
        # setting it. They authorized this narrowing the same day; it changes which
        # pairs this terminal OFFERS, which is why it needed authorizing (rule 16).
        #
        # THE DECODE COSTS NO NETWORK. It is base58check plus a key derivation, so it
        # is safe here, in a constructor that runs before any rippled is reachable --
        # the same property that let the banner report it from 2026-10-02.
        #
        # STILL NOT A CLAIM THE SEED IS THE RIGHT ONE. A decodable seed for an
        # account this host does not pay from is refused by
        # chains/xrp_signing.derive_and_check(), before signing, and that guard is
        # unchanged.
        self.can_spend, self.payout_refusal = payout_capability()
        # Deferred lines already printed by THIS adapter instance. See
        # find_deposits_to_address() for why it is per-instance rather than
        # per-call or global: an unattributable payment is one fact, and it should
        # be said once per worker run, not once per swap scanned.
        #
        # Named with a leading underscore and NOT part of the adapter contract --
        # tests/test_xrp_adapter.py asserts the instance holds no key material by
        # walking vars(), so anything added here is visible to that test by
        # construction. This set holds transaction hashes and refusal reasons,
        # both of which are already printed, so nothing secret enters it.
        self._reported_deferrals: set[str] = set()
        #: Payments the LAST find_deposits_to_address() call read that arrived with no
        #: DestinationTag, in the shape services/deposit_service.record_what_nobody_can_claim()
        #: reads BY NAME. See untagged_payments() for the 2026-10-11 measurement: before this
        #: attribute existed, hasattr(XRPAdapter, "unattributable_drops") was False, that
        #: function returned 0 without writing, and an untagged XRP payment reached neither
        #: deposit_events, nor unattributable_deposits, nor late_deposits.
        #:
        #: INITIALIZED HERE AND NOT ONLY IN THE SCAN, so the attribute exists on an adapter
        #: that has never scanned. The reader uses getattr(..., None) and falls back to 0, so
        #: an adapter missing the name is indistinguishable from an account with nothing
        #: stranded in it -- which is the exact ambiguity this whole mechanism exists to
        #: remove, and it must not be reintroduced by construction order.
        #:
        #: NAME MATCHED TO chains/solana.py's, NOT CHOSEN. `drops` there means dropped
        #: credits; on this ledger `drops` is also the base unit of XRP, which is an unlucky
        #: collision and is NOT a reason to spell it differently here -- the reader asks for
        #: this exact string, and a second spelling would be an attribute nothing reads
        #: (which is what the defect was). The elements are whole XRP, not drops: the
        #: conversion happens in untagged_payments() through chains/xrp_units.from_drops().
        self.unattributable_drops: list[UnattributableCredit] = []
        # Validated at CONSTRUCTION, not at poll time. A threshold no XRP
        # payment can reach would leave every deposit below it forever, with
        # nothing in any log saying why (see chains/xrp_units.py).
        self.min_confirmations = validate_min_confirmations(min_confirmations)

    def endpoint_line(self) -> str:
        """The worker banner's XRP line (rule 14).

        Says "validated ledger" rather than a bare number so the figure cannot
        be read as a block depth the way the BTC/LTC/GRC lines are, and states
        the payout posture -- an operator who expects this chain to pay needs to
        learn that here rather than from a refusal hours later.

        THE PAYOUT WORD HAS NOW CHANGED TWICE, and both old spellings would be lies
        today. Rule 16: a wrong banner line is a wrong comment an operator reads every
        cycle.

          until 2026-09-26   `payouts=REFUSED (holds no signing key)`. True while
                             send_to_address() refused unconditionally.
          until 2026-10-02   `payouts=PREVIEW-ONLY unless armed at the call site
                             (holds no signing key; ...)`. True while no caller in
                             this tree had a seed to pass -- but
                             services/payout_service.broadcast_payout() now reads
                             one from the environment, so "holds no signing key" told
                             an operator their worker could not pay when it could.

          until 2026-10-03   two lines, chosen by whether the variable is SET. The
                             host measured that day had it set to a nine-character
                             placeholder, so it printed ARMED while no payout could
                             have signed. Accurate, and useless.
          now                THREE lines, chosen by payout_capability()'s two
                             questions: nothing exported, a value that is not a
                             seed, or a seed.

        IT IS A BOOLEAN NOW, AND THAT IS THE CHANGE. This docstring used to say the
        honest answer was not one, and that was correct while the answer depended on
        a call site a banner cannot see. It depends on one environment variable now,
        which the banner CAN see, so it says which state this process is in -- and
        rule 14's "make did-nothing look different from did-work" applies to a
        capability as much as to a cycle. The third line is that rule again, one
        level finer: two DIFFERENT ways of not being able to pay must not print the
        same sentence, because the operator's next action is different for each.

        THE ARMED STATE IS THE LOUD ONE. Asterisks on `CAN SPEND`, in the same shape
        services/payout_service.unlock_readiness_lines() uses, because an operator
        scanning a banner for the thing that should not be there has to find it: this
        line appearing on a host pointed at a funded account is the one combination
        that moves money without anybody asking. The UNARMED state names the variable
        instead, so the operator who WANTED a payout knows exactly what to export.

        WHAT NEITHER STATE CHANGES, and it is still printed in both: mainnet is
        refused from the id the SERVER reports rather than from this url. There is no
        variable that turns that off.
        """
        # THREE STATES, NOT TWO, AND THE MIDDLE ONE IS THE 2026-10-03 INCIDENT.
        #
        # This block printed two lines until that day -- ARMED or PREVIEW-ONLY, chosen
        # by presence -- and the host that was measured sat in a third state neither
        # described: the variable SET, holding a nine-character placeholder. It
        # printed ARMED, and said so accurately, because can_spend read presence.
        #
        # can_spend decodes now (see __init__), so an undecodable seed is UNARMED and
        # the pair is NOT offered. The banner has to say which of the two unarmed
        # states a process is in, because the operator's next action differs
        # completely: export the variable, versus fix the value in it. That is rule
        # 14's "state what the number means, next to the number" and rule 14's "make
        # did-nothing look different from did-work" applied to one capability.
        #
        # signing_seed_is_present() rather than inspecting self.payout_refusal: a
        # sentence is for reading and a bool is for branching, and matching on refusal
        # text would make this line break when that wording is improved.
        if self.can_spend:
            # ARMED means present AND decoding, both established in __init__.
            # signing_seed_decodes() is asked AGAIN here rather than cached, and the
            # disagreement it can surface is the reason: can_spend is settled once at
            # construction (deliberately -- see __init__ for the two-surfaces-one-
            # process defect of 2026-10-02), so a process whose environment is mutated
            # after construction can be armed while the current value no longer
            # decodes. That is a real state, it is invisible otherwise, and a banner
            # that reported the construction-time answer as if it were current would
            # be the wrong comment rule 16 counts as a bug.
            decodes, why = signing_seed_decodes()
            posture = (
                f"payouts=*** ARMED, THIS PROCESS CAN SPEND XRP *** ({SIGNING_SEED_ENV_VAR} is set, so "
                f"payouts sign locally and submit. "
                + (f"The seed DECODES: {why}. Still not a claim it is the RIGHT account -- "
                   f"derive_and_check() refuses a seed paired with an account it does not control, "
                   f"before signing. "
                   if decodes else
                   f"*** WARNING, THE ENVIRONMENT HAS CHANGED SINCE THIS ADAPTER WAS BUILT: the seed "
                   f"decoded at construction, which is what armed it, and the value in "
                   f"{SIGNING_SEED_ENV_VAR} NO LONGER DECODES -- {why}. This process will keep "
                   f"offering XRP until it is restarted, and its payouts will fail at signing. "
                   f"Restart it. *** ")
                + "Mainnet is still refused by server network_id, not by url)"
            )
        elif signing_seed_is_present():
            # SET, BUT NOT A SEED. The state measured on the live host 2026-10-03, and
            # the one that used to print ARMED. The decode reason carries the exception
            # TYPE and never the value -- chains/xrp_payout_seed.signing_seed_decodes()
            # excludes str(error) because xrpl-py has echoed the offending seed in its
            # own messages, and a banner is pasted into chats.
            _decodes, why = signing_seed_decodes()
            posture = (
                f"payouts=*** REFUSED, THE SEED DOES NOT DECODE *** ({SIGNING_SEED_ENV_VAR} IS set, so "
                f"something was exported, but the value in it is not a usable signing seed -- {why}. "
                f"XRP is therefore NOT offered as a payout destination and no XRP swap can be created, "
                f"which is the safe direction: a swap created here would take a deposit and then fail "
                f"at signing, irreversibly. Fix the VALUE, not the export -- run xrp_payout_account.py, "
                f"which reports its shape without printing it. Measured on the live host 2026-10-03: "
                f"this variable held a nine-character placeholder while this line reported an armed "
                f"posture and the gate agreed with it. Mainnet "
                f"is refused by server network_id, not by url)"
            )
        else:
            posture = (
                f"payouts=PREVIEW-ONLY ({SIGNING_SEED_ENV_VAR} is NOT set, so every payout refuses "
                f"before signing and no XRP swap can be created; mainnet refused by server network_id, "
                f"not by url)"
            )
        # THE POSTURE COMES BEFORE THE CONFIRMATION GLOSS, AND IT USED TO COME AFTER.
        # describe_min_confirmations() ends in a trailing `<- NOT a block depth; the XRP
        # Ledger does not reorganize` -- an annotation designed to END a line -- so
        # appending the posture behind it rendered, on the operator's screen 2026-10-10:
        #
        #     min_confirmations=1 validated ledger  <- NOT a block depth; the XRP
        #     Ledger does not reorganize payouts=*** ARMED, THIS PROCESS CAN SPEND XRP ***
        #
        # with no separator. The single most important fact on the line -- that this
        # process can spend XRP -- read as the tail of a sentence about reorganization.
        # Rule 14's "state what the number means, NEXT TO the number" cuts both ways: a
        # gloss that is trailing by design has to stay trailing, and anything appended
        # after one is swallowed by it. GRC's unlock posture is already its own line for
        # the same reason; XRP's is the only other armed one, which is why only this
        # line showed it.
        return (
            f"  XRP  rpc={self.url} {posture} "
            f"min_confirmations={describe_min_confirmations(self.min_confirmations)}"
        )

    def call(self, method: str, params: dict | None = None):
        """One rippled JSON-RPC call. Note the params shape and the 200-with-error.

        See the two numbered points in this module's docstring: `params` is a
        LIST containing one object, and a failure comes back with HTTP 200 and
        result.status == "error". Both are checked here rather than trusted.
        """
        response = requests.post(
            self.url,
            headers={"Content-Type": "application/json"},
            data=json.dumps({"method": method, "params": [params or {}]}),
            timeout=self.timeout,
        )
        response.raise_for_status()
        payload = response.json()
        result = payload.get("result")
        if not isinstance(result, dict):
            raise XRPRPCError(
                f"{method}: response has no `result` object. NOT treated as an empty answer -- see "
                f"this class's docstring for why a plausible empty value is the more expensive failure."
            )
        if result.get("status") == "error":
            raise XRPRPCError(
                f"{method}: {result.get('error')} -- {result.get('error_message', 'no message')}. "
                f"NOTE rippled returns errors with HTTP 200, so this was NOT caught by the status code."
            )
        return result

    def get_new_address(self, label: str) -> str:
        """Refuses. XRP attributes deposits by destination tag, not by address."""
        raise XRPRPCError(
            f"this adapter does not derive per-swap XRP addresses, and for {label!r} it should not. "
            f"The XRP Ledger attributes deposits with a DESTINATION TAG on a single account: an "
            f"integer per swap, no new key, no funding reserve, and it is what every exchange on this "
            f"ledger uses. That allocator EXISTS and IS WIRED: services/xrp_tag_service.py::"
            f"allocate_destination_tag(db, account, swap_id, asset), called by "
            f"services/swap_service.py::create_swap() after the swap INSERT and inside the same "
            f"transaction. The pair (account, tag) is stored as swaps.deposit_address plus "
            f"swaps.deposit_tag. Deriving a fresh account instead would cost a base reserve per swap "
            f"AND put a signing key per swap on this host."
        )

    def owns_address(self, address: str) -> bool | None:
        """Always None: the XRP Ledger has no "is this mine" to ask.

        See chains/base.RPCAdapter.owns_address for why the question is asked at all.
        There is no wallet here -- this adapter holds no key and the ledger has no
        notion of an account belonging to a caller -- so the honest answer is "not
        established" rather than False, which a caller must not print as "not yours".
        """
        return None

    def validate_address(self, address: str) -> bool:
        """Checksum-verified locally, with no network call.

        Unlike every other adapter here this asks no daemon, because it does
        not need to: an XRPL address carries a double-SHA256 checksum and
        chains/xrp_address.py verifies it against the ledger's own constants.
        Asking the server would answer a DIFFERENT question -- whether the
        account exists -- and an unfunded but perfectly valid address is
        exactly what a first payment creates.

        An X-address is accepted as valid but flagged by describe_address():
        it carries its own destination tag, which the payout path would have to
        decode rather than take a tag separately.
        """
        if looks_like_x_address(address):
            return True
        return is_valid_classic_address(address)

    def describe_address(self, address: str) -> str:
        """One operator-readable line about an address (rule 14)."""
        return describe_address(address)

    def get_balance(self) -> float:
        """Refuses, because this adapter still holds no account. Payouts no longer do.

        THE SENTENCE BELOW USED TO SAY "payouts are refused" AND THAT BECAME FALSE on
        2026-10-02, when services/payout_service.broadcast_payout() began passing a
        source account and a seed. Corrected rather than left (rule 16: a wrong
        comment is a bug, and this one would have a reader conclude the payout path
        is dead while it signs).

        WHAT IS STILL TRUE is the part that matters: this adapter has no account of
        its own, so it cannot answer "what is MY balance". The account is
        XRP_DEPOSIT_ACCOUNT -- one shared account that both receives deposits and
        pays out, the XRP Ledger equivalent of the single Gridcoin wallet -- and it
        is supplied at the call site rather than held here, for the reason
        preview_payout() gives at length: an adapter that held a hot-wallet account
        would be one configuration value away from being a payout path.

        WHAT THIS COSTS, SO IT IS NOT DISCOVERED LATER: refresh_wallet_inventory()
        calls get_balance() on every adapter, so XRP gets no wallet_inventory row and
        the operator sees no XRP figure on the admin page.
        services/payout_service.inventory_note() already reports that as expected
        rather than as a swallowed error ("an asset whose hot wallet is unset refuses
        by design and is expected here").

        IT COSTS NO SAFETY, and that is why it is acceptable rather than owed work.
        Nothing gates a payout on wallet_inventory -- grepped, 2026-10-02: the only
        readers are services/admin_view.py's display query and this file's own
        reserve/release bookkeeping. The real funding gate is
        xrp_signing.require_reserve_headroom(), which runs on a LIVE account_info
        read inside every preview, in integer drops, against the reserve the server
        itself reports. A cached inventory figure would be the weaker of the two
        answers.

        account_balance(address) is the reader for a known account, and
        xrp_balances.py is the operator-facing report built on it.
        """
        raise XRPRPCError(
            f"get_balance() answers 'what is MY balance' and this adapter has no account of its own, "
            f"deliberately -- see this method's docstring. The account XRP payouts are debited from is "
            f"XRP_DEPOSIT_ACCOUNT, supplied at the call site by services/payout_service.py, and "
            f"account_balance(address) is how to read one. This refusal does NOT mean payouts are "
            f"disabled: that question is {SIGNING_SEED_ENV_VAR} being set, which this adapter reports "
            f"through can_spend. Nothing gates a payout on this call."
        )

    def account_balance(self, address: str) -> float:
        """The spendable balance of one account, in XRP.

        NOT called get_balance() on purpose: that name is the adapter contract
        services/payout_service.py uses to decide whether a payout can be
        funded, and answering it while this adapter cannot pay would be a
        method that lies about what it enables.

        The reserve is SUBTRACTED, because it is not spendable. Every funded
        XRPL account must retain a base reserve, and reporting the raw balance
        would let a caller commit to a payment the ledger then refuses -- the
        gate-versus-send disagreement in a third form.
        """
        drops, _owner_count = self.account_drops_and_owner_count(address)
        return from_drops(drops) - float(self.reserve_xrp())

    def account_drops_and_owner_count(self, address: str) -> tuple[int, int | None]:
        """One account_info read, returning RAW drops and OwnerCount. No subtraction.

        Extracted so account_balance() above and the payout preview below share
        ONE reader of this response (rule 8). They need different things from
        it and that is exactly the situation where two copies get written: the
        balance method wants spendable XRP as a float, the reserve check wants
        raw integer drops and the count of owned ledger objects, and a second
        parse would have been the shorter diff. The two would then disagree the
        first time the response shape changed, and only one of them would be
        wrong at a time -- which is rule 8's "nothing fails until a decision
        made through copy A contradicts a decision made through copy B."

        DROPS, AS AN INTEGER, is what this returns. The subtraction and the
        float conversion belong to the caller that needs them; the payout path
        never converts, because integer drops is the only representation in
        which a reserve check cannot be wrong by a rounding.

        OwnerCount comes back as None when the response omits it. That is
        handled rather than assumed away in xrp_signing.reserve_drops(), which
        says on screen that its figure understates the reserve when it happens.

        MEASURED PRESENT 2026-09-26 on the operator's host against rippled 3.4.1:
        a real preview printed "1000000 base + 200000 x 0 owned objects" with no
        UNDERSTATES warning, and that warning is the fallback's marker -- so
        OwnerCount was read and its value was 0. This docstring said the field
        "was not read off a live server from the environment this was written in",
        which was accurate when written: that environment could not reach port
        51234 at all.

        The None branch stays. It is cheap, and a field being present on one
        server on one day is evidence about that server rather than a guarantee
        (rule 17). The failure it guards is also asymmetric: understating the
        reserve lets a payout through that the ledger then rejects, which is why
        it says so on screen instead of quietly using the low number.
        """
        result = self.call(_METHOD_ACCOUNT_INFO, {"account": address, "ledger_index": "validated"})
        data = result.get("account_data") or {}
        raw = data.get("Balance")
        if not isinstance(raw, str):
            raise XRPRPCError(
                f"{_METHOD_ACCOUNT_INFO} returned Balance={raw!r}; the XRP Ledger sends drop counts as "
                f"JSON STRINGS, specifically so a client cannot round them through a double. A "
                f"non-string here means the response shape is not what this adapter expects."
            )
        owner_count = data.get("OwnerCount")
        return int(raw), (None if owner_count is None else int(owner_count))

    def reserve_xrp(self) -> float:
        """The base reserve, ASKED OF THE SERVER rather than hardcoded.

        It has changed more than once -- 20 XRP, then 10, then 1 -- so a
        compiled-in constant would eventually make this terminal believe it has
        spendable balance it does not. chains/xrp_units.py carries a reference
        figure for a banner and explicitly not for this.
        """
        result = self.call(_METHOD_SERVER_INFO)
        ledger = (result.get("info") or {}).get("validated_ledger") or {}
        reserve = ledger.get("reserve_base_xrp")
        if reserve is None:
            raise XRPRPCError(
                f"{_METHOD_SERVER_INFO} did not report validated_ledger.reserve_base_xrp. NOT falling "
                f"back to the reference value {REFERENCE_BASE_RESERVE_XRP} XRP: the reserve is a "
                f"network parameter that has changed before, and guessing it wrong overstates "
                f"spendable balance."
            )
        return float(reserve)

    def network(self) -> str:
        """Which network the SERVER says it is on, not which the URL implies.

        A hostname can lie or be reused; the server's own network id cannot be
        mistaken by the client. solana_chain_check.py makes the same
        distinction with getGenesisHash and for the same reason.
        """
        result = self.call(_METHOD_SERVER_INFO)
        info = result.get("info") or {}
        # NAMED, NOT JUST ECHOED. This returned the bare id until 2026-10-02 and the
        # operator's reachability panel read "it reports its network as 1" in the panel
        # whose whole job is telling mainnet from a test network -- see
        # chains/xrp_signing.network_id_name() for the measurement. The naming lives
        # beside MAINNET_NETWORK_IDS so this module does not decide which id is mainnet
        # (rule 8), and it decides nothing either way: the refusal is unchanged.
        network_id = info.get("network_id")
        if network_id is None:
            return str(info.get("build_version", "unknown"))
        return network_id_name(network_id)

    def find_deposits_to_address(self, address: str, tx_limit: int = 200,
                                 skip_txids=frozenset()) -> list[dict]:
        """Validated payments to one account, as deposit events.

        The filtering and every refusal live in chains/xrp_payments.py -- most
        importantly that the credited figure comes from meta.delivered_amount
        and never from Amount, which is the partial payment exploit. This
        method is transport.

        BOTH tx_limit AND skip_txids ARE ACCEPTED AND IGNORED. XRP discovery
        is a single `account_tx` call, so there is no per-transaction cost to
        avoid and nothing to skip. chains/solana.py is the one implementation
        that uses skip_txids, because Solana discovery costs one
        getTransaction PER TRANSACTION and re-reading settled history
        rate-limited a real deposit out of being credited on 2026-10-01 -- the
        measurement is in that method's comment.

        They are parameters here rather than a branch in the caller so that
        services/deposit_service.py calls every adapter the same way (rule 8).

        IT ALSO RECORDS WHAT IT COULD NOT CREDIT, on `self.unattributable_drops`,
        and the RETURN VALUE IS UNCHANGED by that. The five-method contract
        services/ and workers/ read is untouched -- a caller that wants the
        untagged payments asks for them by name, exactly as it does of
        chains/solana.py. untagged_payments() carries the 2026-10-11 measurement
        of what happened before it did: an untagged payment reached no table in
        swap_terminal.db at all, and the only trace was the print below.
        """
        # CLEARED BEFORE THE CALL, NOT AFTER IT, AND THE ONLY THING THAT LINE BUYS IS THE
        # FAILURE CASE. Worth spelling out, because the obvious reading of it is wrong.
        #
        # The list has to describe THIS scan: a watcher holds one adapter for its whole
        # life and calls this once per active swap per cycle, so a list that accumulated
        # would hand record_what_nobody_can_claim() a payment from an hour ago as though
        # it had just been read. chains/solana.py clears at the same point and for the
        # same reason, and the two adapters answer one contract and must not differ about
        # the lifetime of the state that contract reads.
        #
        # BUT ACCUMULATION IS NOT WHAT THIS LINE PREVENTS. The assignment further down
        # REPLACES the list on every scan that completes, so a successful scan is already
        # exact without it -- MEASURED 2026-10-11 by deleting this line and running
        # tests/test_xrp_unattributable_deposits.py, which passed 16 of 16. That is a test
        # passing both ways, which CLAUDE.md calls decoration, and the fix was to find what
        # the line actually does rather than to delete it or to keep a green test.
        #
        # WHAT IT ACTUALLY DOES: A SCAN THAT RAISES LEAVES THE LIST EMPTY RATHER THAN
        # STALE. Everything below this line can raise -- the HTTP call on a dead endpoint,
        # and the scan itself on a response shape chains/xrp_payments.py does not
        # recognize -- and the assignment is then never reached. Without the clear, a
        # caller reading this attribute after a failed scan gets the PREVIOUS scan's
        # findings with nothing saying they are old; and that caller exists:
        # services/deposit_service.scan_shared_accounts() catches the exception per target
        # and carries on with the rest of the cycle. Empty is the honest answer after a
        # failed scan -- nothing was established -- and it is the same direction that
        # function's own comment argues for when it refuses to record an empty scan
        # result as "this account received nothing".
        #
        # tests/test_xrp_unattributable_deposits.py::test_a_failed_scan_leaves_no_stale_drops
        # is the test that fails when this line goes.
        self.unattributable_drops = []
        result = self.call(
            _METHOD_ACCOUNT_TX,
            {"account": address, "ledger_index_min": -1, "ledger_index_max": -1, "binary": False},
        )
        # READ ONCE INTO A NAME, because it is now used twice -- by the credit scan and
        # by untagged_payments() -- and `result.get("transactions") or []` evaluated
        # twice would be two expressions that have to stay identical by hand.
        entries = result.get("transactions") or []
        try:
            scan = deposit_events_from_transactions(entries, address, self.min_confirmations)
        except XRPPaymentError as error:
            raise XRPRPCError(f"deposit scan for {address} refused: {error}") from error
        # THE WHOLE-LIST SCAN ABOVE RUNS FIRST AND THAT ORDER IS LOAD-BEARING, not
        # stylistic. untagged_payments() re-asks the classifier one entry at a time, so
        # it can only be safe once every entry has already been parsed without raising --
        # see its docstring. Putting it before the scan would turn a malformed response
        # into a refusal from the wrong function, with the wrong message.
        self.unattributable_drops, unreadable = untagged_payments(
            entries, address, self.min_confirmations
        )
        # REPORTED ONCE PER RUN, not once per scan.
        #
        # Measured on the operator's host 2026-09-26: two unattributable payments
        # printed FOUR lines, because find_deposits_to_address() is called once per
        # active swap and every one of those calls scans the SAME shared account,
        # so it sees the same untagged payments every time. Two open swaps doubled
        # it; ten would have printed twenty, and each line ends with "this needs an
        # operator to match it by hand", so the count itself reads as the number of
        # problems. It was two.
        #
        # This is the shape rule 14 warns about from the other direction: not
        # silence, but noise that misrepresents scale. A deferred payment is a fact
        # about the ACCOUNT, and on a tag-attributed chain the account is shared by
        # every swap -- so it is not per-swap news and must not be printed as if it
        # were.
        #
        # State on the instance, deliberately: it lives as long as the worker, so a
        # RESTART re-reports everything still unattributed, which is what an
        # operator starting a worker wants to see. A set that outlived the process
        # would hide the backlog from whoever came next.
        for line in new_deferrals(scan.deferred, self._reported_deferrals):
            print(f"  XRP deferred  {line}", flush=True)
        # THE UNTAGGED PAYMENTS THAT GOT NO ROW, THROUGH THE SAME DEDUPE AND FOR THE SAME
        # REASON (rule 14, and the 2026-09-26 measurement above). These are the two cases
        # untagged_payments() names: a ledger that has not validated yet, and a
        # delivered_amount that is not an XRP drop string. Both are facts about the shared
        # ACCOUNT, so N open swaps must not print N copies of them.
        #
        # A SEPARATE LINE PREFIX FROM `XRP deferred`, deliberately. A deferred payment and
        # a payment that was deferred AND could not be recorded are different states with
        # different operator actions -- wait, versus look at this transaction by hand --
        # and rule 14 forbids them sharing a line. They share the `_reported_deferrals`
        # SET, which is correct: the set is keyed on the whole line, and these lines
        # differ from the deferral's by their suffix, so neither suppresses the other.
        for line in new_deferrals(unreadable, self._reported_deferrals):
            print(f"  XRP NOT RECORDED  {line}", flush=True)
        return scan.events

    def server_parameters(self) -> dict:
        """One server_info read: the network verdict, the reserve figures, the fee.

        ONE CALL, not three, and that is a correctness point rather than a
        performance one. network(), reserve_xrp() and a fee read would each
        issue their own server_info, so the network the payment is checked
        against could differ from the network whose reserve was used -- up to
        three answers from three different moments, with nothing saying they
        disagreed. The payout path reads the server once and decides from that
        one snapshot.

        THE NETWORK CHECK HAPPENS HERE, before anything else in the payout path
        can proceed, and it raises on mainnet. That is why this is called at the
        top of preview_payout(): a mainnet endpoint cannot even be PREVIEWED
        against, let alone sent to.

        base_fee_xrp is read WHEN PRESENT and the fallback is named in the
        returned description (rule 14).

        MEASURED PRESENT 2026-09-26 on the operator's host, against rippled 3.4.1
        on s.altnet.rippletest.net: a real preview printed "fee allowance 10
        drops, read from server_info.validated_ledger.base_fee_xrp", so the read
        branch is the one that ran and the field is confirmed. This docstring said
        the field "was not among the ones confirmed against a live server" until
        that run, which was true when written and is not now.

        The fallback stays, and not as dead code: base_fee_xrp is optional in
        rippled's own reply and a server under load or a different build may omit
        it. One server answering once is evidence about that server, not a
        guarantee about the field (rule 17) -- which is the same reason the
        description names which figure it used rather than printing a bare
        number.
        """
        result = self.call(_METHOD_SERVER_INFO)
        info = result.get("info") or {}
        ledger = info.get("validated_ledger") or {}
        # Raises XRPMainnetRefused on a mainnet id, on a missing id, and on an
        # id of an unexpected shape. See xrp_signing.require_non_mainnet.
        network = require_non_mainnet(info.get("network_id"), self.url)

        base_reserve = ledger.get("reserve_base_xrp")
        if base_reserve is None:
            raise XRPRPCError(
                f"{_METHOD_SERVER_INFO} did not report validated_ledger.reserve_base_xrp, so the "
                f"reserve this payment must respect is unknown. NOT falling back to the reference "
                f"value {REFERENCE_BASE_RESERVE_XRP} XRP: the reserve is a network parameter that has "
                f"changed before (20, then 10, then 1), and guessing it low would let this path commit "
                f"to a payment the ledger refuses after claiming a fee."
            )

        base_fee_xrp = ledger.get("base_fee_xrp")
        if base_fee_xrp is None:
            fee_drops = FEE_ALLOWANCE_DROPS
            fee_source = (
                f"{fee_drops} drops, the conservative allowance in chains/xrp_signing.py -- the server "
                f"did NOT report validated_ledger.base_fee_xrp. The fee actually paid is autofilled by "
                f"xrpl-py at submit time and may differ; this figure is only what the reserve check "
                f"subtracts."
            )
        else:
            # to_drops_round_half_up(), NOT to_drops(), AND THE DIFFERENCE IS
            # DELIBERATE. to_drops() became TRUNCATING on 2026-10-03 by operator
            # decision ("make it all match") so that no payout exceeds its quote.
            # That direction is right for an amount and wrong for a fee: a fee
            # allowance rounded DOWN understates what the ledger will charge, and
            # this figure is what the reserve check subtracts. So this site keeps
            # the rounding it has always had, byte for byte. Both functions live in
            # chains/xrp_units.py and each names the other.
            fee_drops = to_drops_round_half_up(base_fee_xrp)
            fee_source = (
                f"{fee_drops} drops, read from server_info.validated_ledger.base_fee_xrp. The fee "
                f"actually paid is autofilled by xrpl-py at submit time and rises with load."
            )
        return {
            "network": network,
            "build_version": info.get("build_version"),
            "base_reserve_xrp": base_reserve,
            "owner_reserve_xrp": ledger.get("reserve_inc_xrp"),
            "fee_drops": fee_drops,
            "fee_source": fee_source,
        }

    def preview_payout(self, address: str, amount, source: str, destination_tag: int | None = None) -> dict:
        """Everything about an XRP payout except the signature. Read-only.

        THIS IS THE DEFAULT MODE of send_to_address(), not a separate feature.
        It reads the server, does every conversion, applies every guard that can
        be applied without a key, and returns the result plus a `description` an
        operator can read (rule 14: echo the parameters that decide the answer,
        so a pasted block is self-describing a day later). Modeled on
        chains/solana.py's build_transfer_plan() for the reason that one exists:
        the half that can be built without custody should be built, shown, and
        testable.

        NOTHING HERE CAN MOVE MONEY. No key is touched, no signing library is
        imported, and the only network calls are server_info and account_info,
        both reads. Running this against any endpoint is safe -- except that a
        MAINNET endpoint refuses, via server_parameters() above, which is
        deliberate: wanting a preview is not a reason to let this path talk to
        mainnet at all.

        THE ORDER OF THE GUARDS IS THE DESIGN, cheapest-and-most-fatal first:

          1  destination address   local checksum, no network call
          2  the amount            to_drops(), which refuses a negative, a
                                   non-number and a zero
          3  the NETWORK           server_info; mainnet refuses here and
                                   everything below is unreachable
          4  the balance           account_info on the SOURCE account
          5  the reserve           integer drops arithmetic, refusing before any
                                   signature rather than letting the ledger
                                   refuse after it has claimed a fee

        Raises rather than returning a refusal in a field. A dict with
        `ok: False` in it is the shape every caller forgets to check, and on this
        path the cost of forgetting is a send.
        """
        if looks_like_x_address(address):
            raise XRPRPCError(
                f"{address} is an X-ADDRESS, and this payout path will not send to one. It is a valid "
                f"address -- validate_address() accepts it -- but it CARRIES ITS OWN destination tag "
                f"encoded into the string, so sending to it while also passing "
                f"destination_tag={destination_tag!r} would mean two tags, one of which silently loses. "
                f"Decode it to a classic address plus a tag and pass them separately. Nothing was read "
                f"from the server and nothing was sent."
            )
        if not is_valid_classic_address(address):
            raise XRPRPCError(
                f"{address} fails the checksum in chains/xrp_address.py, so it is not a payable XRPL "
                f"address. NOTHING was read from the server and nothing was sent. An XRPL address "
                f"carries a double-SHA256 checksum, so this is decided locally and a typo cannot reach "
                f"the ledger."
            )
        if not source:
            raise XRPRPCError(
                "preview_payout() needs the SOURCE account this payment would debit, and none was "
                "given. It is a required argument rather than adapter state on purpose: an adapter that "
                "held a hot-wallet account would be one configuration value away from being a payout "
                "path, and which account pays is the operator's decision (CLAUDE.md rule 16)."
            )
        if not is_valid_classic_address(source):
            raise XRPRPCError(
                f"the source account {source} fails the checksum in chains/xrp_address.py. NOTHING was "
                f"read and nothing was sent. A malformed source would otherwise produce an account_info "
                f"error several layers from the cause."
            )
        send_drops = to_drops(amount)
        if send_drops <= 0:
            raise XRPRPCError(
                f"{amount!r} XRP is {send_drops} drops, so there is nothing to pay. A zero-value Payment "
                f"is a perfectly valid XRPL transaction that costs a fee and delivers nothing, and it "
                f"would be written into `payouts` as a broadcast payout. Refused."
            )

        # Rule 14: announce BEFORE, not only after. Everything below this line
        # makes network calls, and an operator watching a blinking cursor cannot
        # tell working from hung -- which on this path resolves with a Ctrl-C.
        print(
            f"  XRP payout preview  {from_drops_decimal(send_drops)} XRP ({send_drops} drops) "
            f"{source} -> {address} tag={destination_tag if destination_tag is not None else '(none)'}",
            flush=True,
        )
        print(f"  XRP payout preview  reading server_info and account_info from {self.url}", flush=True)

        parameters = self.server_parameters()
        balance_drops, owner_count = self.account_drops_and_owner_count(source)
        required_reserve, reserve_line = reserve_drops(
            parameters["base_reserve_xrp"], parameters["owner_reserve_xrp"], owner_count
        )
        headroom = require_reserve_headroom(
            balance_drops, send_drops, parameters["fee_drops"], required_reserve
        )

        description = "\n".join(
            [
                f"    network       {parameters['network']}  build {parameters['build_version']}",
                f"    from          {source}  (the account this DEBITS)",
                f"    to            {address}",
                f"    tag           {destination_tag if destination_tag is not None else '(none)'}",
                f"    amount        {from_drops_decimal(send_drops)} XRP = {send_drops} drops",
                f"    fee allowance {parameters['fee_source']}",
                f"    reserve       {reserve_line}",
                f"    headroom      {headroom}",
                "    partial pay   tfPartialPayment will NOT be set, so Amount is an exact figure and "
                "not a ceiling",
            ]
        )
        return {
            "source": source,
            "destination": address,
            "destination_tag": destination_tag,
            "send_drops": send_drops,
            "balance_drops": balance_drops,
            "fee_drops": parameters["fee_drops"],
            "required_reserve_drops": required_reserve,
            "network": parameters["network"],
            "description": description,
        }

    def send_to_address(  # noqa: PLR0913 -- checked: these four keywords ARE the payment, and bundling them into one object would add a type without removing a parameter AND would let a single object carry both the seed and the arming token. Their separation is the reason a forgotten opt-in cannot become a send. Same judgment recorded at chains/base.py:68. PLR0917 is deliberately NOT suppressed beside it: it does not fire, because every one of the four is keyword-only, which is the same property the arming argument relies on.
        self,
        address: str,
        amount: float,
        *,
        source: str = "",
        seed: str = "",
        destination_tag: int | None = None,
        confirm_send: str = "",
    ) -> str:
        """PREVIEWS by default. Signs and submits only when armed, and never on mainnet.

        WHAT CHANGED ON 2026-09-26, AND WHAT DID NOT. This method used to refuse
        unconditionally, and the refusal was an absence: no signing library was
        imported into this module, so it could not have signed if the check had
        been deleted. It can sign now, and the honest statement of what replaced
        that absence is FOUR structural properties, not one of which is a
        configuration value:

          the network       server_parameters() asks the SERVER for its
                            network_id and xrp_signing.require_non_mainnet()
                            refuses id 0, a MISSING id, and an unreadable id.
                            The url is echoed and never consulted, because a
                            hostname resolves to whatever DNS says today. There
                            is no flag, environment variable or argument that
                            turns this off, and the PREVIEW path runs it too --
                            so mainnet cannot even be previewed against.
          the arming token  confirm_send must equal
                            xrp_signing.CONFIRM_XRP_SEND exactly. A caller that
                            omits it gets the preview and a refusal.
                            Deliberately NOT a boolean: a truthy variable, a
                            parsed config value or a positional argument that
                            drifted one place could each produce a send nobody
                            wrote.
          no stored key     this adapter holds no seed, reads no key path, and
                            has no Config field or RPC dict key that would give
                            it one, and it does not import
                            xrp_payout_seed.signing_seed(). The seed arrives as
                            an argument from a caller that already had it, so
                            SETTING THE ENVIRONMENT VARIABLE DOES NOT ARM THIS
                            METHOD -- a call with no seed still refuses, with
                            the variable set.
          the caller        services/payout_service.broadcast_payout() calls
                            this with source=, seed= and
                            confirm_send=CONFIRM_XRP_SEND when the destination
                            asset is XRP, and with the two positional arguments
                            and nothing else for every other chain. It refuses
                            BEFORE reaching this method when either
                            XRP_PAYOUT_SECRET_SEED or XRP_DEPOSIT_ACCOUNT is
                            unset, so a half-configured host costs no network
                            round trip.

        WHAT THESE TWO ENTRIES SAID UNTIL 2026-10-02, because the correction is the
        change: the first ended "CONFIGURATION ALONE CANNOT ARM THIS: there is no
        .env edit that results in a payout", and the second said the call site made
        two positional arguments and "WAS NOT WIRED UP and wiring it is the
        operator's". The operator asked for it to be wired, in those words, so the
        second is done; and the first is now true of this method and false of the
        process, which is the distinction the module header draws at length.

        XRP IS A TRADEABLE PAIR AND HAS BEEN SINCE 2026-09-26. This docstring said
        "Config.ALLOWED_PAIRS is unchanged and names no XRP pair", which was already
        wrong when it was written: ALLOWED_PAIRS carries ('XRP','GRC'), ('GRC','XRP'),
        ('XRP','BTC') and ('XRP','LTC'). What actually kept the payout worker from
        reaching this method was can_spend being False, which refuses the swap at
        creation -- and that is still what gates it, now from whether the seed in
        this process DECODES (chains/xrp_payout_seed.payout_capability(), narrowed
        from mere presence on 2026-10-03) rather than from a constant. ALLOWED_PAIRS is NOT changed by this work:
        enabling a pair is live posture and remains the operator's.

        WHY submit_and_wait() AND NOT submit(). submit() returns when the server
        has accepted the blob for relay, which is not the same as the ledger
        having applied it: a transaction that fails with a tec* code, or that
        never validates, would come back as a txid and be written into `payouts`
        as `broadcast`. submit_and_wait() waits for validation, and the final
        result is checked below against tesSUCCESS AND the validated flag before
        any hash is returned. Rule 13's "a stop that cannot prove it worked is
        not a stop", applied to a send: the assertion is the outcome, not the
        absence of an exception.

        Returns the transaction hash. Raises on every refusal, and the class of
        the exception says WHICH guard fired (see chains/xrp_signing.py).
        """
        plan = self.preview_payout(address, amount, source, destination_tag)

        # The arming check comes AFTER the preview and BEFORE anything that
        # could sign, which is the order that makes the default useful: an
        # unarmed caller gets the full preview inside the refusal message rather
        # than a bare "not armed", so an operator who then arms it is arming
        # something they have read.
        #
        # THE DESCRIPTION IS EMITTED EXACTLY ONCE, and which path emits it is the
        # point. It used to be printed here, before this check, AND appended to
        # the refusal below -- so an unarmed caller that surfaced the exception
        # got the whole eight-line block twice. Measured on the operator's host
        # 2026-09-26, against a real rippled: the preview, the refusal, then the
        # same preview again. Rule 14 asks for output a human can read, and a
        # doubled block is how a reader starts skimming the thing that exists to
        # be read.
        #
        # The copy inside the exception is the one that survives: a caller that
        # catches and logs the refusal still has the plan it refused. So the
        # unarmed path carries it there, and the live print moved BELOW the
        # arming check -- which is also where rule 14's "announce before" most
        # wants it, immediately before the one irreversible step in this file
        # rather than before a guard that usually stops.
        try:
            require_send_confirmation(confirm_send, seed)
        except XRPSendNotArmed as error:
            raise XRPSendNotArmed(f"{error}\n{plan['description']}") from error

        print(plan["description"], flush=True)
        return self._sign_and_submit(plan, seed)

    def _sign_and_submit(self, plan: dict, seed: str) -> str:
        """The only code in this tree's chains/ that signs anything.

        Separate from send_to_address() rather than inlined, and the split is
        exactly where the guards end and the irreversible part begins:
        everything above this call is a read or a refusal, everything inside it
        touches a key. Reaching it requires the arming token, a seed, and a
        server that has already answered with a non-mainnet network id.

        Kept as a private method here rather than a function in xrp_signing.py
        for one reason: it makes network calls, and xrp_signing.py's header
        promises that nothing in it opens a socket. That promise is worth more
        than the symmetry would be.
        """
        try:
            # Lazy, for the reason chains/xrp_signing.derive_and_check() records
            # at length: xrpl-py is an OPTIONAL dependency, chains/registry.py
            # imports this module unconditionally, and a module-level import
            # would make a signing library mandatory in order to start a
            # read-only deposit watcher.
            import httpx  # noqa: PLC0415 -- checked: optional dependency, arrives with xrpl-py
            from xrpl.clients import JsonRpcClient  # noqa: PLC0415 -- checked: optional dependency
            from xrpl.constants import XRPLException  # noqa: PLC0415 -- checked: optional dependency
            from xrpl.models.transactions import Payment  # noqa: PLC0415 -- checked: optional dependency
            from xrpl.transaction import submit_and_wait  # noqa: PLC0415 -- checked: optional dependency
        except ImportError as error:
            raise XRPRPCError(
                "xrpl-py is not importable, so this payment was NOT signed and NOT submitted. It is an "
                "optional dependency on purpose -- the preview path, every guard and the whole test "
                "suite work without it -- so a missing signing library is a refusal here rather than a "
                "crash at import. `pip install xrpl-py` if this host is meant to pay XRP out."
            ) from error

        wallet = derive_and_check(seed, plan["source"])
        print(
            f"  XRP payout  derived address matches the announced source: {wallet.classic_address}",
            flush=True,
        )

        payment = Payment(
            account=plan["source"],
            destination=plan["destination"],
            destination_tag=plan["destination_tag"],
            amount=str(plan["send_drops"]),
            # NO flags. See xrp_signing.TF_PARTIAL_PAYMENT: with tfPartialPayment
            # set, the ledger may deliver LESS than Amount and still return
            # tesSUCCESS, so a payout could under-pay a customer and be recorded
            # as a successful broadcast with a real hash. The next line verifies
            # the SERIALIZED transaction rather than trusting this comment,
            # because "the code does not pass flags" is evidence about today's
            # call site and not about the transaction that gets signed.
        )
        refuse_partial_payment(payment.to_xrpl())

        print(f"  XRP payout  submitting and waiting for validation ({plan['network']})", flush=True)
        started = time.monotonic()
        try:
            response = submit_and_wait(payment, JsonRpcClient(self.url), wallet)
        except (XRPLException, httpx.HTTPError) as error:
            # NAMED types, not `except Exception`. Measured against the installed
            # xrpl-py 5.2.0: XRPLReliableSubmissionException and
            # XRPLRequestFailureException both subclass
            # xrpl.constants.XRPLException, and the only other family that
            # reaches here is transport failure from httpx, which xrpl-py's
            # JSON-RPC client uses. Two names cover it, so no breadth is needed
            # and no BLE001 suppression is required (rule 19: fix the code, do
            # not suppress the finding).
            #
            # WHAT THIS DOES NOT ESTABLISH, said plainly because it is the
            # expensive case: a timeout here does NOT mean nothing was submitted.
            # The transaction may have been relayed and may still validate.
            # Nothing retries, and nothing should retry without looking first.
            raise XRPRPCError(
                f"submit_and_wait failed: {type(error).__name__}: {error}. THIS IS NOT PROOF NOTHING "
                f"WAS SENT -- a transport failure after the blob was relayed looks identical to a "
                f"refusal before it. Look the source account's recent transactions up before retrying; "
                f"a retry that duplicates a validated payment pays twice, and on this ledger that is "
                f"final."
            ) from error
        elapsed = format_duration(time.monotonic() - started)

        result = response.result or {}
        meta = result.get("meta") or {}
        outcome = str(meta.get("TransactionResult") or result.get("engine_result") or "(none)")
        tx_hash = result.get("hash") or (result.get("tx_json") or {}).get("hash")
        validated = result.get("validated")
        print(
            f"  XRP payout  TransactionResult {outcome}  validated={validated}  waited {elapsed}",
            flush=True,
        )

        if outcome != "tesSUCCESS":
            raise XRPRPCError(
                f"the payment was submitted and the ledger's final result is {outcome}, which is NOT "
                f"tesSUCCESS, so no value was delivered. hash={tx_hash or '(none)'}. A tec* result "
                f"REACHED the ledger and claimed a fee, so it was not free; a ter*/tem* result did not. "
                f"Nothing is retried here. Raised rather than returned so that a failed payment cannot "
                f"be written into `payouts` as a broadcast one."
            )
        if validated is not True:
            raise XRPRPCError(
                f"the payment reported {outcome} but validated={validated!r}, so the ledger has NOT "
                f"confirmed it. hash={tx_hash or '(none)'}. Treated as a failure on purpose: the XRP "
                f"Ledger's finality is binary (chains/xrp_units.py), so 'succeeded but not validated' "
                f"is not a weaker yes -- it is an answer this path will not read as delivery. Look the "
                f"hash up before retrying, because the transaction may yet validate."
            )
        if not tx_hash:
            raise XRPRPCError(
                f"the payment reported {outcome} and validated, but the response carried no hash. THE "
                f"PAYMENT WAS ALMOST CERTAINLY MADE: a missing field is not evidence it did not happen. "
                f"Check the source account before retrying, because retrying would pay twice."
            )
        print(f"  XRP payout  DELIVERED and validated  hash={tx_hash}", flush=True)
        return str(tx_hash)
