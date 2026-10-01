"""Create a swap from a quote, and read one back.

Role: submodule -> function (create_swap is the decision)
Reads: swap_terminal.db (quotes, swaps, deposit_events, payouts), the
       destination adapter (validateaddress) and the source adapter
       (getnewaddress)
Writes: swap_terminal.db (swaps, swap_audit_log)
Can move funds: no broadcast. It DERIVES a deposit address in the hot wallet
       and fixes the payout address, and it sets min_confirmations from
       config -- the threshold that later decides when a payout is released.
       Since 2026-09-27 it also REFUSES both of those addresses when they
       cannot receive money, which is the cheapest moment either can be
       stopped: nothing has been written and nothing has been taken. See
       _refuse_unusable_deposit_address() below and the payout check inside
       create_swap().
Mainnet-safe: yes

set_swap_status() writes the status and the audit row together, so every
transition is recorded. It does NOT commit: the caller owns the transaction
boundary, which is what lets create_swap() insert the swap and its first audit
row atomically.
"""

import logging

from chains.registry import unconfigured_chains, why_cannot_pay_out, why_unconfigured
from modules.address_authority import check_address, check_receive_address, expected_network

from .helpers import new_id, parse_iso, utc_now_iso
from .xrp_tag_service import allocate_destination_tag

# No handler and no setLevel: a library module that configures logging decides policy for
# every program that imports it, which is the import-time side effect rule 12 names. The
# three atomic_*_client.py files carry the same note for the same reason.
logger = logging.getLogger(__name__)


def get_min_confirmations(config, asset: str) -> int:
    return int(config[f"{asset}_MIN_CONFIRMATIONS"])


def set_swap_status(db, swap_id: str, new_status: str, message: str | None = None, old_status: str | None = None):
    current = old_status
    if current is None:
        row = db.execute("SELECT status FROM swaps WHERE id = ?", (swap_id,)).fetchone()
        current = row["status"] if row else None
    db.execute(
        "UPDATE swaps SET status = ?, updated_at = ? WHERE id = ?",
        (new_status, utc_now_iso(), swap_id),
    )
    db.execute(
        "INSERT INTO swap_audit_log (swap_id, old_status, new_status, message, created_at) VALUES (?, ?, ?, ?, ?)",
        (swap_id, current, new_status, message, utc_now_iso()),
    )


def get_quote_or_raise(db, quote_id: str) -> dict:
    quote = db.execute("SELECT * FROM quotes WHERE id = ?", (quote_id,)).fetchone()
    if not quote:
        raise ValueError("Quote not found")
    if parse_iso(quote["expires_at"]) <= parse_iso(utc_now_iso()):
        raise ValueError("Quote expired")
    return quote


# The chains whose deposits are told apart by a TAG on one shared account rather
# than by a per-swap address. A set of one today, named rather than written as
# `if from_asset == "XRP"` so the concept is greppable and a second such chain
# (Stellar's memo, Cosmos's memo, several exchange-style deposit models) is one
# entry rather than a second branch to find. Rule 11: one vocabulary, in one place.
TAG_ATTRIBUTED_ASSETS = frozenset({"XRP", "SOL"})

#: WHICH SHARED ACCOUNT each tag-attributed chain pays into, and what the discriminator is
#: CALLED on that chain. Derived from the asset rather than spelled at the use site, which is
#: what adding SOL on 2026-09-29 forced: `deposit_address_for()` read XRP_DEPOSIT_ACCOUNT from
#: inside the branch and every refusal it raised said "DestinationTag" -- so a SOL swap would
#: have been refused for the absence of an XRP variable, in a sentence naming a field the
#: Solana blockchain does not have. Rule 11: one vocabulary, in one place, meaning the same
#: thing for every asset that uses it.
#:
#: THE DISCRIMINATOR NAME IS FOR HUMANS ONLY. Both chains carry the integer in the event's
#: `vout` -- see services/deposit_service.attributable_events() -- and what differs is what the
#: chain's own documentation calls it, which is what an operator will search for.
#: THE NETWORK NAME IS CARRIED, not derived by appending a word to the ticker. Generalizing
#: this on 2026-09-29 first produced "not a valid XRP account" where the message had said "XRP
#: LEDGER account" -- a real loss of precision caught by a test that pinned the wording, and
#: the wording was right: an operator searching for why their account was refused searches the
#: network's name, not the ticker's.
TAG_ATTRIBUTION = {
    "XRP": ("XRP_DEPOSIT_ACCOUNT", "DestinationTag", "XRP Ledger"),
    "SOL": ("SOL_DEPOSIT_ACCOUNT", "Memo instruction", "Solana"),
}

# THE COLUMN that holds the tag, named once so no reader can spell it differently.
#
# It is `deposit_tag` and NOT `destination_tag`, and the difference is deliberate
# rather than an accident -- so it is named at BOTH sites, per rule 8's "if they
# genuinely differ, the difference is the point and belongs in a comment at both,
# naming the other one".
#
#   deposit_tag       the SCHEMA's name. Generic, because the column is the
#                     integer discriminator for any tag-attributed chain -- a
#                     Stellar or Cosmos memo would live in the same column, and
#                     `destination_tag` would then be a lie about two of the three.
#   destination tag   the XRP LEDGER's own term, and what a customer sees on the
#                     page. services/swap_view.py renders it under that name
#                     because that is what their wallet's field is labeled.
#
# That split cost a real defect on 2026-09-26: swap_view.py read
# swap["destination_tag"], a key that does not exist, so a swap WITH a tag
# rendered "NO DESTINATION TAG HAS BEEN ISSUED" and told the customer not to send
# anything. It failed safe -- it refused to show a send target rather than showing
# a wrong one -- but the swap was unusable. The agent that wrote the page flagged
# the dependency and named the one .get() to change if the column were called
# something else. It was. This constant is so the next one cannot drift.
DEPOSIT_TAG_COLUMN = "deposit_tag"


def why_cannot_take_deposits(config, adapters: dict, asset: str) -> str:
    """Why this chain cannot be a deposit SOURCE, or "" if it can. The mirror of
    chains/registry.why_cannot_pay_out(), which asks the same question about the other end.

    WHAT THIS CLOSES, MEASURED 2026-10-01 rather than supposed. services/pair_view.py decides a
    pair is `enabled` when both chains are reachable AND the TO asset can pay out. It never
    asked whether the FROM asset can produce a deposit address, so with both shared accounts
    unset the dropdown offered FOUR pairs that cannot create a swap:

        SOL->GRC   SOL_DEPOSIT_ACCOUNT is not set
        XRP->BTC   XRP_DEPOSIT_ACCOUNT is not set
        XRP->GRC   XRP_DEPOSIT_ACCOUNT is not set
        XRP->LTC   XRP_DEPOSIT_ACCOUNT is not set

    Three of those predate the SOL work. The customer picks the pair, is quoted, accepts, and
    gets a refusal where the deposit address should be. That is the same defect
    why_cannot_pay_out()'s docstring records ("badged ENABLED ... a customer would have sent
    GRC, had it credited, and been left with a swap in `failed`") one door over -- offered and
    not completable, failing at the start of the flow instead of the end. Cheaper than that
    one, because nothing has been deposited yet, and still the first thing a customer hits.

    IT CALLS deposit_account() RATHER THAN RE-DERIVING THE RULE (rule 8). That function already
    knows every way a deposit address can be unavailable -- unset variable, invalid account,
    a network mismatch -- and a second copy here would agree today and drift.

    ADDRESS-ATTRIBUTED CHAINS RETURN "" WITHOUT BEING PROBED, and that is not laziness: for
    BTC, LTC and GRC the test deposit_account() applies is get_new_address(), which DERIVES A
    REAL ADDRESS from the wallet. Calling it to answer a question for a page would burn a fresh
    address on every page load. Their reachability is what can go wrong and
    chains/registry.unconfigured_chains() already reports that.

    THE SENTENCE IS SHORTER THAN deposit_account()'S, deliberately, and the difference is
    stated at both sites per rule 8: this one goes in a badge beside a greyed-out pair, where
    the reader needs the variable's name; that one is the API refusal a caller gets back, where
    the reader needs to be told no swap was created and why that is the intended failure.
    """
    if asset not in TAG_ATTRIBUTED_ASSETS:
        return ""
    if asset not in adapters:
        # UNREACHABLE IS A DIFFERENT QUESTION, and unconfigured_chains() answers it. Probing
        # here would raise KeyError on adapters[asset] inside deposit_account(), and two
        # reasons for one pair leaves the operator to work out which to act on -- the same
        # reasoning why_cannot_pay_out() gives for returning "" when there is no adapter.
        return ""
    variable, _discriminator, _network = TAG_ATTRIBUTION[asset]
    try:
        # swap_id="" because this is a PROBE: the tag-attributed branch reads config and
        # validates the account and allocates nothing, so no tag is burned. open_swap.py:568
        # already probes it this way.
        deposit_account(config, adapters, asset, "")
    except ValueError:
        return f"{asset} cannot take deposits: {variable} is unset or not a valid account"
    return ""


def deposit_account(config, adapters: dict, from_asset: str, swap_id: str) -> tuple[str, bool]:
    """Where a customer sends the deposit. Returns (address, needs_tag). Writes nothing.

    THE DECISION, extracted so it can be called with seeded inputs and asserted on
    directly (rule 10) rather than only through a swap creation that needs a quote,
    a payout address and two live adapters.

    Two shapes, and the difference is not cosmetic:

      by address   BTC, LTC, GRC. A fresh address per swap, so the ADDRESS is the
                   identity and the tag is None. get_new_address() derives it.
      by tag       XRP. One shared account for every swap, told apart by an
                   integer DestinationTag. The account is custody configuration,
                   the tag is allocated from the database, and BOTH halves are
                   mandatory -- the account alone is not an instruction, because
                   every XRP swap has the same one.

    Why XRP does not simply implement get_new_address(): it CANNOT, and the
    adapter refuses on purpose rather than returning something address-shaped.
    Deriving a fresh XRP account per swap would mean funding each one past the
    base reserve (1 XRP measured on testnet 2026-09-26) and holding a key for it,
    to solve a problem the ledger already solved with an integer. Every exchange
    on this ledger uses tags.

    The refusal when XRP_DEPOSIT_ACCOUNT is unset is deliberate and is the whole
    reason this is a decision rather than a lookup: the alternative is a swap
    created with a deposit instruction pointing at nothing, which the customer
    then pays. A swap that fails to be created costs a retry; a swap that takes a
    deposit it cannot see costs the deposit.
    """
    if from_asset not in TAG_ATTRIBUTED_ASSETS:
        derived = adapters[from_asset].get_new_address(f"swap_{swap_id}")
        _refuse_unusable_deposit_address(config, from_asset, derived, "the wallet's own get_new_address()")
        return derived, False

    variable, discriminator, network = TAG_ATTRIBUTION[from_asset]
    account = (config.get(variable) or "").strip()
    if not account:
        raise ValueError(
            f"{from_asset} deposits are attributed by {discriminator} on one shared account, and "
            f"{variable} is not set, so there is no account to pay into. NO SWAP WAS "
            f"CREATED -- which is the intended failure: a swap created now would hand a customer a "
            f"deposit instruction this terminal cannot receive against. Set {variable} to "
            f"an account you hold the key for; it is a custody decision and has no default."
        )
    if not adapters[from_asset].validate_address(account):
        raise ValueError(
            f"{variable} ({account}) is not a valid {network} account, so no swap was "
            f"created. Checked BEFORE allocating a tag: a tag is never reused, so allocating one "
            f"against a bad account would burn it permanently for a swap that cannot exist."
        )
    # AND THE SAME LOCAL DECODE THE ADDRESS CHAINS GET, one line below the adapter's own
    # check rather than instead of it. The adapter is XRPAdapter, whose validate_address()
    # accepts ANY X-address without verifying its checksum -- its own docstring carries that
    # review finding -- so on this one chain the adapter's yes is the weaker of the two
    # answers. Keeping both is not duplication (rule 8): they answer different questions and
    # the difference is named here and at chains/xrp.py.
    _refuse_unusable_deposit_address(config, from_asset, account, variable)

    # The tag is NOT allocated here, and the split is not stylistic. It is a
    # WRITE with a FOREIGN KEY into swaps(id), so it cannot run until the swap row
    # exists -- measured 2026-09-26, allocating first raises XRPTagAllocationError
    # ("there is no swap row with id ..., so no tag was allocated and NOTHING was
    # written"). An earlier version of this function allocated here and carried a
    # comment claiming create_swap() inserted first. It did not. The comment was
    # false the moment it was written, which is the defect rule 16 calls a bug:
    # a reader would have trusted it instead of reading the order.
    #
    # So this function stays a pure read that can be tested without a database,
    # and create_swap() allocates after the INSERT, inside the same transaction.
    return account, True



def _refuse_unusable_deposit_address(config, asset: str, address: str, source: str) -> None:
    """Raise unless `address` can actually receive `asset` on the network we believe we are on.

    THE RECEIVE PATH IS NOT THE SEND PATH, AND THIS IS THE DIFFERENCE. Operator, 2026-09-27:
    "make the receive path burn proof too."

    On the payout side a bad address burns OUR fee or a customer's payout, and the address
    came from a stranger typing it. Here we HAND A CUSTOMER an address and they pay into it
    with their own money, so the loss is THEIRS and they cannot detect it before paying. It
    is the worse of the two failures, which is why this one REFUSES where
    services/payout_service.py's guard is careful not to.

    WHAT CAN ACTUALLY GO WRONG HERE, since a typo is not it -- the string comes from our own
    daemon:

      - A DAEMON ON THE WRONG NETWORK. `gridcoinresearchd getnewaddress` with no `-testnet`
        put RyyNX8E7tDRzACL47h8JKKDUXf9aMCz8YV into the operator's live MAINNET staking
        wallet on 2026-09-27. Handed to a customer by a terminal that believes it is on
        testnet, that is real money paid onto a chain nothing here is watching. This is why
        the check is check_receive_address() and not check_address(): the NETWORK half is
        the half that catches the accident that actually happened.
      - A wallet answering with an error string, or a truncated RPC response, reaching
        `swaps.deposit_address` as a non-address.
      - XRP_DEPOSIT_ACCOUNT set by hand to something that is not an account.

    WHY REFUSING IS SAFE HERE AND NOT ON THE PAYOUT PATH. At this point in create_swap()
    NOTHING HAS MOVED: no deposit taken, no swap row written (the INSERT is still ahead of
    us), no tag allocated, no instruction shown to anyone. A refusal costs a retry. Letting
    it through costs a customer's entire deposit. tests/test_xrp_swap_attribution.py::
    test_a_refused_xrp_swap_leaves_no_row_behind already pins the "no row behind" half, and
    raising from here -- a pure function called before the INSERT -- keeps it true by
    construction rather than by cleanup.

    WHAT IT DOES NOT DO. NO_VALIDATOR does not refuse: see modules/address_authority.py's
    header. And `expected_network()` returns None for XRP and SOL, and for any chain on
    a port network_target.py has no convention for, so on those the network half is SKIPPED
    rather than guessed -- rule 17: "I could not tell" must never be written as "it is
    wrong". The decode half still runs everywhere.
    """
    verdict = check_receive_address(asset, address, expected_network(asset, config.get("RPC")))
    if verdict.refuses:
        raise ValueError(
            f"NO SWAP WAS CREATED, and nothing was written. The {asset} deposit address from {source} "
            f"cannot receive a deposit: {verdict.why}. A customer paying into it would lose the money "
            f"with nothing to show for it, so the swap is refused before the row exists rather than "
            f"after they have paid."
        )
    if verdict.unchecked:
        # Rule 14: an unchecked deposit address must not be indistinguishable from a checked
        # one. Not an exception, because it is not a refusal -- see the docstring. `.unchecked`
        # covers BOTH ways of passing unverified (no validator for the chain, and a validator
        # that could not place the address); the second is the one the operator's 2026-09-27
        # regtest run actually produced, via a missing bech32 hrp.
        logger.warning(
            "deposit address for %s from %s was NOT CHECKED (%s): %s  <- the swap is being created anyway, "
            "because refusing an address we cannot place would break a working chain.",
            asset, source, verdict.state, verdict.why,
        )


def create_swap(db, config, adapters: dict, quote_id: str, payout_address: str) -> dict:
    quote = get_quote_or_raise(db, quote_id)
    to_asset = quote["to_asset"]
    from_asset = quote["from_asset"]
    payout_address = payout_address.strip()
    # BOTH CHAINS MUST HAVE AN ADAPTER IN THIS PROCESS, AND THE MESSAGE HAS TO SAY
    # SO. Checked before the address validation below, because that line is where
    # the failure used to happen and it happened as a subscript.
    #
    # 2026-09-26, from the operator's browser: `No swap was created: 'GRC'`. That
    # is str(KeyError("GRC")) -- `adapters[to_asset]` raised, routes/swaps.py's
    # HTTP boundary returned str(exc), and a KeyError's str is the repr of the key
    # and nothing else. A running Gridcoin daemon on 25715, three workers printing
    # `GRC rpc=127.0.0.1:25715`, a priced quote, and the page said `'GRC'`.
    #
    # The cause was that the SERVER process had no GRC_RPC_PORT (the workers were
    # started from a shell that did), so build_adapters() skipped Gridcoin. The
    # information needed to fix it was one env var name, and none of it reached the
    # screen.
    #
    # from_asset is checked here as well, though deposit_account() below would
    # raise on it a few lines later: one refusal naming both missing chains beats
    # two consecutive single-chain failures, and a swap whose SOURCE chain has no
    # adapter has no deposit watcher looking at it either.
    missing = unconfigured_chains(adapters, from_asset, to_asset)
    if missing:
        raise ValueError(
            "No swap was created, because "
            + " Also: ".join(why_unconfigured(asset, config.get("RPC")) for asset in missing)
            + f" The {from_asset}->{to_asset} pair is in ALLOWED_PAIRS, which is why the quote priced -- "
            f"ALLOWED_PAIRS says what this terminal is WILLING to swap and the adapters say what it can "
            f"REACH, and those are different questions. Nothing was written."
        )
    # REACHABLE IS NOT THE SAME AS ABLE TO PAY, and this is the authority rather
    # than the page. routes/ui.py stops OFFERING such a pair, but a POST to
    # /api/swaps does not come from the page, so the gate that matters is here.
    #
    # GRC -> XRP on 2026-09-26: an XRP adapter exists and reaches the testnet, so the
    # check above passes. XRPAdapter holds no signing key and payout_service calls
    # send_to_address() unarmed, so the payout RAISES -- the customer's GRC would be
    # taken, credited, and the swap left in `failed` needing a person. Refusing
    # before the swap row exists is the only stage at which nothing has been taken.
    #
    # Checked BEFORE validate_address(), deliberately: for XRP that validator accepts
    # any X-address without verifying its checksum (found by review the same day), so
    # a chain that cannot be a destination must never be asked for one.
    cannot_pay = why_cannot_pay_out(adapters, to_asset)
    if cannot_pay:
        raise ValueError(
            f"No swap was created, because {cannot_pay} The {from_asset}->{to_asset} pair is in "
            f"ALLOWED_PAIRS and both chains are reachable -- but a swap that cannot be paid out takes "
            f"a deposit it can never settle. Nothing was written."
        )
    # A SWAP THAT PAYS OUT ZERO TAKES A DEPOSIT AND DELIVERS NOTHING.
    #
    # create_quote() computes output_amount_estimate as
    # max(gross * (1 - fee) - network_fee_reserve, 0.0), so a small enough input
    # produces exactly 0.0 -- the reserve alone can exceed the whole payout. Flagged
    # by review 2026-09-26: open_swap.py then printed
    # "payout (est.) 0.0 GRC  <- what payout_worker broadcasts" and exited 0, so the
    # deposit instruction went out for a swap that could only ever pay nothing.
    #
    # Refused HERE and not in the CLI, because the web form reaches the same
    # arithmetic. This is not a pricing change: the estimate is already what
    # create_quote() computed and nothing here alters a rate, a fee or a reserve. It
    # declines to CREATE a swap whose own quote says the customer receives zero.
    if float(quote["output_amount_estimate"]) <= 0:
        raise ValueError(
            f"No swap was created: {quote['input_amount']} {from_asset} prices to a payout of "
            f"{quote['output_amount_estimate']} {to_asset}, which is nothing. The "
            f"{quote['network_fee_reserve']} {to_asset} network fee reserve and the {quote['fee_bps']} bps "
            f"fee together exceed the gross output at this rate. Deposit more {from_asset}. Nothing was "
            f"written."
        )
    # THE PAYOUT ADDRESS, DECODED LOCALLY BEFORE THE DAEMON IS ASKED. Added 2026-09-27.
    #
    # This is the EARLIEST point at which a burn can be stopped on the send side, and it is
    # the only one the web form reaches: open_swap.py's CLI carries the same local decode,
    # and routes/swaps.py POSTs straight here. services/payout_service.py guards the send
    # itself as a last resort, but by the time a swap reaches that worker the customer's
    # deposit has already been taken and credited -- a refusal there strands them. Here,
    # nothing has been written and nothing has been taken, so a refusal costs a retry.
    #
    # BEFORE the daemon, not instead of it, and the two are kept because they differ:
    # this one needs no daemon and cannot be fooled by an adapter that answers loosely,
    # and the daemon's answers whether THAT wallet on THAT network will accept it.
    #
    # DECODE ONLY -- deliberately NOT check_receive_address(). A payout address belongs to
    # the CUSTOMER's wallet, and refusing it for being on the wrong network would be this
    # process's own configuration overruling theirs. The daemon's validate_address() below
    # is network-scoped and is the right authority for that half.
    payout_verdict = check_address(to_asset, payout_address)
    if payout_verdict.refuses:
        raise ValueError(
            f"No swap was created: {payout_address!r} cannot receive a {to_asset} payout -- "
            f"{payout_verdict.why}. Refused here, before any daemon was asked and before any row was "
            f"written, because money sent to it would be unspendable by anybody. Nothing was written."
        )
    if payout_verdict.unchecked:
        logger.warning(
            "payout address for a new %s swap was NOT CHECKED locally (%s): %s  <- proceeding to the "
            "daemon's own validate_address(), which is the authority this could not stand in for.",
            to_asset, payout_verdict.state, payout_verdict.why,
        )
    if not adapters[to_asset].validate_address(payout_address):
        raise ValueError(f"Invalid {to_asset} payout address")
    swap_id = new_id("s")
    # The swap row must exist before a tag can reference it: xrp_destination_tags
    # has a FOREIGN KEY to swaps(id). Handled inside create_swap() below by
    # inserting the row first and allocating second -- see the note there.
    # Both config checks happen HERE, before anything is written: an unset or
    # invalid XRP_DEPOSIT_ACCOUNT must abort the swap rather than leave a row
    # behind. The tag itself is allocated after the INSERT, below.
    deposit_address, needs_tag = deposit_account(config, adapters, from_asset, swap_id)
    now = utc_now_iso()
    swap = {
        "id": swap_id,
        "quote_id": quote["id"],
        "from_asset": from_asset,
        "to_asset": to_asset,
        "deposit_address": deposit_address,
        # Filled in after the INSERT for a tag-attributed chain; see below.
        "deposit_tag": None,
        "payout_address": payout_address,
        "expected_input_amount": float(quote["input_amount"]),
        "actual_input_amount": None,
        "quoted_rate": float(quote["quoted_rate"]),
        "fee_bps": int(quote["fee_bps"]),
        "network_fee_reserve": float(quote["network_fee_reserve"]),
        "output_amount_estimate": float(quote["output_amount_estimate"]),
        "status": "awaiting_deposit",
        "min_confirmations": get_min_confirmations(config, from_asset),
        "deposit_txid": None,
        "payout_txid": None,
        "created_at": now,
        "updated_at": now,
        "credited_at": None,
        "completed_at": None,
        "expires_at": quote["expires_at"],
        "failed_reason": None,
    }
    db.execute(
        """
        INSERT INTO swaps (
            id, quote_id, from_asset, to_asset, deposit_address, deposit_tag, payout_address,
            expected_input_amount, actual_input_amount, quoted_rate, fee_bps,
            network_fee_reserve, output_amount_estimate, status, min_confirmations,
            deposit_txid, payout_txid, created_at, updated_at, credited_at,
            completed_at, expires_at, failed_reason
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            swap["id"], swap["quote_id"], swap["from_asset"], swap["to_asset"], swap["deposit_address"],
            swap["deposit_tag"], swap["payout_address"], swap["expected_input_amount"], swap["actual_input_amount"], swap["quoted_rate"],
            swap["fee_bps"], swap["network_fee_reserve"], swap["output_amount_estimate"], swap["status"],
            swap["min_confirmations"], swap["deposit_txid"], swap["payout_txid"], swap["created_at"],
            swap["updated_at"], swap["credited_at"], swap["completed_at"], swap["expires_at"], swap["failed_reason"],
        ),
    )
    # ALLOCATED HERE, after the INSERT and before the commit, so the swap row the
    # FOREIGN KEY needs exists and the whole thing is still one transaction. If
    # allocation raises, nothing is committed: no swap, no tag, no half-created
    # row handing a customer a deposit instruction with no way to recognize the
    # payment. That is the outcome to want -- a failed creation costs a retry,
    # while a swap that takes a deposit it cannot attribute costs the deposit.
    if needs_tag:
        # from_asset is PASSED, and before 2026-10-01 it was not: the account was
        # checked against the XRP Ledger's format on every tag chain, so the first
        # real SOL -> GRC swap was refused 400 with "not a valid XRPL classic
        # address" for a correct Solana account. See ACCOUNT_VALIDATORS in
        # services/xrp_tag_service.py for the measurement.
        swap["deposit_tag"] = allocate_destination_tag(db, deposit_address, swap_id, from_asset)
        db.execute("UPDATE swaps SET deposit_tag = ? WHERE id = ?", (swap["deposit_tag"], swap_id))

    db.execute(
        "INSERT INTO swap_audit_log (swap_id, old_status, new_status, message, created_at) VALUES (?, ?, ?, ?, ?)",
        (swap_id, None, "awaiting_deposit", "Swap created", now),
    )
    db.commit()
    return swap


def get_swap(db, swap_id: str) -> dict | None:
    swap = db.execute("SELECT * FROM swaps WHERE id = ?", (swap_id,)).fetchone()
    if not swap:
        return None
    deposit_events = db.execute(
        "SELECT * FROM deposit_events WHERE swap_id = ? ORDER BY id ASC",
        (swap_id,),
    ).fetchall()
    payouts = db.execute(
        "SELECT * FROM payouts WHERE swap_id = ? ORDER BY id ASC",
        (swap_id,),
    ).fetchall()
    swap["deposit_events"] = deposit_events
    swap["payouts"] = payouts
    return swap
