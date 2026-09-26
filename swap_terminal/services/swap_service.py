"""Create a swap from a quote, and read one back.

Role: submodule -> function (create_swap is the decision)
Reads: swap_terminal.db (quotes, swaps, deposit_events, payouts), the
       destination adapter (validateaddress) and the source adapter
       (getnewaddress)
Writes: swap_terminal.db (swaps, swap_audit_log)
Can move funds: no broadcast. It DERIVES a deposit address in the hot wallet
       and fixes the payout address, and it sets min_confirmations from
       config -- the threshold that later decides when a payout is released.
Mainnet-safe: yes

set_swap_status() writes the status and the audit row together, so every
transition is recorded. It does NOT commit: the caller owns the transaction
boundary, which is what lets create_swap() insert the swap and its first audit
row atomically.
"""

from chains.registry import unconfigured_chains, why_unconfigured

from .helpers import new_id, parse_iso, utc_now_iso
from .xrp_tag_service import allocate_destination_tag


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
TAG_ATTRIBUTED_ASSETS = frozenset({"XRP"})

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
        return adapters[from_asset].get_new_address(f"swap_{swap_id}"), False

    account = (config.get("XRP_DEPOSIT_ACCOUNT") or "").strip()
    if not account:
        raise ValueError(
            f"{from_asset} deposits are attributed by DestinationTag on one shared account, and "
            f"XRP_DEPOSIT_ACCOUNT is not set, so there is no account to pay into. NO SWAP WAS "
            f"CREATED -- which is the intended failure: a swap created now would hand a customer a "
            f"deposit instruction this terminal cannot receive against. Set XRP_DEPOSIT_ACCOUNT to "
            f"an account you hold the key for; it is a custody decision and has no default."
        )
    if not adapters[from_asset].validate_address(account):
        raise ValueError(
            f"XRP_DEPOSIT_ACCOUNT ({account}) is not a valid XRP Ledger account, so no swap was "
            f"created. Checked BEFORE allocating a tag: a tag is never reused, so allocating one "
            f"against a bad account would burn it permanently for a swap that cannot exist."
        )

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
            + " Also: ".join(why_unconfigured(asset) for asset in missing)
            + f" The {from_asset}->{to_asset} pair is in ALLOWED_PAIRS, which is why the quote priced -- "
            f"ALLOWED_PAIRS says what this terminal is WILLING to swap and the adapters say what it can "
            f"REACH, and those are different questions. Nothing was written."
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
        swap["deposit_tag"] = allocate_destination_tag(db, deposit_address, swap_id)
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
