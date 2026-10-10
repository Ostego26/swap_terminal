"""SQLite schema and connection handling for the swap terminal.

Role: submodule (persistence; holds no decision of its own)
Reads: swap_terminal.db
Writes: swap_terminal.db -- creates quotes, swaps, deposit_events,
       unattributable_deposits, late_deposits, payouts, wallet_inventory,
       market_context, swap_audit_log, wallet_inventory_corrections,
       xrp_destination_tags
       and address_proof_challenges if they are absent, plus the two triggers
       that make an allocated XRP destination tag immutable and undeletable and
       the three that make an address-proof challenge single-use, unrepointable
       and -- once it has proven something -- undeletable
Can move funds: no
Mainnet-safe: yes

swap_terminal.db is the ONE authority (rule 15). Everything else in this tree
that holds state -- transactions.json, gridcoin_transactions.csv,
grc-sol-swap/.../swap_intents.json -- is either a mirror or, in
swap_intents.json's case, a second system of record that nothing reconciles
with this one. Nothing new may become an authority: there is one.

Two things worth knowing before changing anything here.

WAL is on (`PRAGMA journal_mode=WAL`), so readers do not block the writer. That
is not a concurrency guarantee for the application: SQLite still has exactly
one writer lock, and a guard implemented as a SELECT can go stale between the
read and the write even though the writes themselves are serialized. That is
measured, not supposed -- see tests/test_payout_concurrency.py, where two
payout workers both pay the same swap through a guard that reads correctly.

The `except Exception` around the Flask import is deliberate and is the narrow
kind rule 12 allows: it lets the workers import this module without Flask
installed, and the failure is not silent -- get_db() raises RuntimeError
naming the missing dependency rather than returning something a caller could
mistake for a connection.
"""

import logging
import sqlite3
from contextlib import contextmanager
from pathlib import Path

# Rootless, the same way services/deposit_service.py reaches
# deposit_vout_artifact.py: swap_terminal/ is already on sys.path for `db` to
# have been importable at all. chains/__init__.py is deliberately empty of code
# and chains/xrp_units.py imports only `decimal`, so this pulls in no adapter,
# opens no socket and reads no credential -- which is what makes it safe for a
# module every worker imports at startup.
from chains.xrp_units import FIRST_ALLOCATABLE_TAG, MAX_DESTINATION_TAG

try:
    from flask import current_app, g
except ImportError:
    # Checked, and narrowed from `except Exception` on 2026-09-24: the only
    # thing that legitimately fails here is Flask being absent, which is the
    # supported case -- the workers use db_session() and never touch
    # request-scoped state. A broader catch would also swallow an error INSIDE
    # a Flask that is installed but broken, and then get_db() would report the
    # wrong cause. The failure is not silent either way: get_db() raises
    # RuntimeError naming the missing dependency rather than returning
    # something a caller could mistake for a connection.
    current_app = None
    g = None

logger = logging.getLogger(__name__)

SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;

CREATE TABLE IF NOT EXISTS quotes (
    id TEXT PRIMARY KEY,
    from_asset TEXT NOT NULL,
    to_asset TEXT NOT NULL,
    input_amount REAL NOT NULL,
    quoted_rate REAL NOT NULL,
    fee_bps INTEGER NOT NULL,
    network_fee_reserve REAL NOT NULL,
    output_amount_estimate REAL NOT NULL,
    expires_at TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS swaps (
    id TEXT PRIMARY KEY,
    quote_id TEXT NOT NULL,
    from_asset TEXT NOT NULL,
    to_asset TEXT NOT NULL,
    deposit_address TEXT NOT NULL,
    -- NULL for every chain that attributes a deposit BY ADDRESS, which is all of
    -- them except XRP. For XRP the deposit instruction is a PAIR -- one shared
    -- account plus an integer DestinationTag -- and both halves are mandatory:
    -- the account alone is not an instruction, because every XRP swap shares it.
    --
    -- A second column rather than encoding the pair into deposit_address. The XRP
    -- Ledger does have a single-string form for exactly this (an X-address, which
    -- packs account and tag together), and it was rejected here on rule 5/20
    -- grounds: attribution is a JOIN between a deposit event's tag and the swap
    -- that owns it, and a join against an opaque string that must be decoded in
    -- Python first is the gate-in-the-wrong-place this repo keeps paying for. As
    -- an INTEGER column it is queryable, indexable and readable by anyone with a
    -- sqlite3 prompt. deposit_address still holds the account, so an address
    -- lookup keeps working unchanged for every chain.
    deposit_tag INTEGER,
    payout_address TEXT NOT NULL,
    expected_input_amount REAL NOT NULL,
    actual_input_amount REAL,
    quoted_rate REAL NOT NULL,
    fee_bps INTEGER NOT NULL,
    network_fee_reserve REAL NOT NULL,
    output_amount_estimate REAL NOT NULL,
    status TEXT NOT NULL,
    min_confirmations INTEGER NOT NULL,
    deposit_txid TEXT,
    payout_txid TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    credited_at TEXT,
    completed_at TEXT,
    expires_at TEXT NOT NULL,
    failed_reason TEXT,
    FOREIGN KEY (quote_id) REFERENCES quotes(id)
);

CREATE INDEX IF NOT EXISTS idx_swaps_status ON swaps(status);
CREATE INDEX IF NOT EXISTS idx_swaps_deposit_address ON swaps(deposit_address);
-- The index attribution actually uses on a tag-attributed chain. Every XRP swap
-- shares one deposit_address, so the address index above cannot narrow anything
-- for XRP -- the pair (address, tag) is what identifies a swap, and this is the
-- index that makes "which swap owns this tag" a lookup rather than a scan.
CREATE INDEX IF NOT EXISTS idx_swaps_deposit_address_tag ON swaps(deposit_address, deposit_tag);

CREATE TABLE IF NOT EXISTS deposit_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    swap_id TEXT NOT NULL,
    asset TEXT NOT NULL,
    txid TEXT NOT NULL,
    vout INTEGER NOT NULL,
    address TEXT NOT NULL,
    amount REAL NOT NULL,
    confirmations INTEGER NOT NULL,
    first_seen_at TEXT NOT NULL,
    last_seen_at TEXT NOT NULL,
    credited_at TEXT,
    -- THE ADDRESS IS PART OF THE KEY SINCE 2026-10-10, AND IT COST A DEPOSIT TO LEARN.
    --
    -- This was UNIQUE(asset, txid, vout) and that encodes an assumption: that a txid is
    -- unique per asset. It holds for every chain whose txid is a HASH. It is false on
    -- ICP, because chains/icp.find_deposits_to_address() uses the LEDGER BLOCK INDEX as
    -- the txid -- deliberately, for the reason its docstring gives -- and a block index
    -- is unique only within ONE ledger.
    --
    -- The local replica's ledger is destroyed by `docker compose down` and rebuilt from
    -- genesis (docker-compose.yml's header records that loss on 2026-10-07 and again on
    -- 2026-10-08), so its block indexes restart at 0. Measured on the operator's host
    -- 2026-10-10, from their own admin page:
    --
    --     s_f5cf62e0b7a9342a  ICP  txid 2  vout 0  0.05000000  conf 1   written 2026-10-07
    --     s_968a69b37c3da5c9  ICP  txid 2  vout 0  2.44081155           sent    2026-10-10
    --
    -- Two different payments, to two different subaccounts, on two different ledgers,
    -- colliding on this key. services/deposit_service.upsert_deposit_event() found the
    -- 2026-10-07 row, UPDATEd its confirmations and returned -- it does not re-point
    -- swap_id -- so the live swap got NO ROW and sat at awaiting_deposit while its
    -- 2.44081155 ICP was provably in its subaccount. Nothing errored. That is the
    -- failure chains/icp.py's archived-blocks refusal exists to prevent, reached by a
    -- different road.
    --
    -- THE ADDRESS IS WHAT DISTINGUISHES THEM AND IT WAS ALREADY IN THE ROW. Every one of
    -- the four event producers -- chains/base.py in both its real-vout and
    -- fabricated-fallback shapes, chains/solana.py, chains/xrp_payments.py and
    -- chains/icp.py -- writes the SCANNED address into the event, so this column has
    -- always identified which account the payment landed in.
    --
    -- WIDENING A UNIQUE KEY CANNOT FAIL AGAINST EXISTING ROWS, which is why this was
    -- applied to a database holding real money history rather than named as work for
    -- somebody else. Data satisfying (asset, txid, vout) necessarily satisfies
    -- (asset, txid, vout, address): the new key is strictly weaker, so no pre-check of
    -- the kind duplicate_live_payouts() does for the payout index is possible or needed.
    -- No row is deleted and no evidence is destroyed (rule 7).
    --
    -- ON EVERY OTHER CHAIN THIS CHANGES NOTHING. BTC, LTC and GRC give each swap its own
    -- address and their txids are hashes, so no two rows could have collided anyway; SOL
    -- and XRP share ONE account per chain, so the address is constant across every row
    -- on the asset and the wider key is the narrower one.
    UNIQUE(asset, txid, vout, address),
    FOREIGN KEY (swap_id) REFERENCES swaps(id)
);

CREATE INDEX IF NOT EXISTS idx_deposit_events_swap_id ON deposit_events(swap_id);

-- MONEY THAT ARRIVED AND BELONGS TO NO SWAP. Added 2026-10-01.
--
-- WHY IT CANNOT BE A deposit_events ROW, which was the obvious answer and is wrong by
-- schema rather than by judgment: `deposit_events.swap_id` is NOT NULL with a FOREIGN KEY
-- to swaps(id), and an unattributable deposit belongs to no swap -- that is the definition
-- of unattributable. There is no row it can be written as. `under_review` is no better: it
-- is a status on SWAPS, so there is no swap to move into it either.
--
-- WHY NOT A NULLABLE swap_id INSTEAD. Every existing reader of deposit_events assumes the
-- column is populated -- refresh_swap_from_chain() sums `WHERE swap_id = ?`, the artifact
-- tooling joins on it -- and a NULL would be invisible to each of them until one query
-- somewhere forgot to exclude it and credited an orphan to a swap. That is rule 8's defect
-- with a delay on it: the readers agree today and drift from the day the column changes
-- meaning. A separate table cannot be read by accident.
--
-- NOTHING ON THE ORDER PATH MAY READ THIS. It is a record for a human, not an input to a
-- decision: no gate, no crediting, no payout may join against it. If a deposit in here
-- turns out to belong to a swap, a person resolves it and the resolution is the
-- `resolved_at`/`resolution_note` pair below -- which is also why there is no swap_id to
-- "fill in later". Filling one in would make this table a second source of truth about who
-- owns a deposit, and rule 15's "exactly one reader, no decision read from a buffer"
-- applies to a table in the authority just as much as to a staging file.
--
-- KEYED ON (asset, txid) AND NOT (asset, txid, vout). `vout` is the integer discriminator,
-- and the whole reason a row lands here is that no usable discriminator was found -- so it
-- cannot be part of the key. One transaction credits one shared account once, which is the
-- same reasoning chains/solana.py's header gives for using the account index as `vout` at
-- all: the account model has one net balance delta per account per transaction.
CREATE TABLE IF NOT EXISTS unattributable_deposits (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    asset TEXT NOT NULL,
    txid TEXT NOT NULL,
    -- The shared deposit account the money landed in, carried rather than looked up, so a
    -- row still says where the coins are after the configuration changes.
    address TEXT NOT NULL,
    -- Whole units of the asset, the same scale as deposit_events.amount. A record of money
    -- that omits the amount is not a record of money, which is why chains/solana.py's
    -- UnattributableCredit makes it a required field rather than a defaulted one.
    amount REAL NOT NULL,
    -- How many separate credits in that transaction made up the amount. A human matching it
    -- by hand needs both numbers: what arrived, and in how many pieces.
    credits INTEGER NOT NULL,
    -- The discriminator that WAS found, when one was and it matched no open swap; NULL when
    -- the deposit carried none at all. Those are two different support conversations: the
    -- first is "your reference does not match any order", the second is "you sent without a
    -- reference", and a single table that could not tell them apart would make the operator
    -- open the chain to find out.
    discriminator INTEGER,
    -- The adapter's own reason, in its words, so the row and the log line agree.
    why TEXT NOT NULL,
    confirmations INTEGER NOT NULL,
    first_seen_at TEXT NOT NULL,
    last_seen_at TEXT NOT NULL,
    resolved_at TEXT,
    resolution_note TEXT,
    UNIQUE(asset, txid)
);

-- The query an operator actually runs: what is still outstanding, oldest first.
CREATE INDEX IF NOT EXISTS idx_unattributable_deposits_unresolved
    ON unattributable_deposits(resolved_at, first_seen_at);

-- MONEY THAT ARRIVED FOR A SWAP THAT HAD ALREADY FINISHED. Added 2026-10-04.
--
-- THE DEFECT, MEASURED END TO END ON THE OPERATOR'S REGTEST BTC THAT DAY. Swap
-- s_10b5946333612e06 (BTC->GRC) completed: expected_input_amount 0.0001, credited_at
-- 2026-10-04T16:13:09.315366+00:00, deposit_events id=24 at vout=1 for txid
-- 7ea61ac9c7038f58..., and 986.89613973 GRC broadcast. The operator then sent a SECOND
-- 0.0001 BTC to the SAME deposit address -- txid 1abce90c0e7fdfe5... -- and the desk's
-- `desk_hot` balance moved 10.00110000 -> 10.00120000, so the coins are in the wallet.
-- Queried read-only afterwards: NO deposit_events row for that txid and NO
-- unattributable_deposits row either. The payment existed on chain, in our wallet, and
-- in no row of this database.
--
-- WHY NOTHING SAW IT, established by running it rather than by reading the code
-- (2026-10-04, services/late_deposit_service.py's docstring carries the full table):
-- services/deposit_service.process_active_swaps() selects ACTIVE_STATUSES --
-- awaiting_deposit, deposit_seen, confirming -- and an address-attributed chain's deposit
-- address is only ever scanned from inside that loop. Seeding one BTC swap per status and
-- counting adapter calls: the address is scanned for those three and scanned ZERO times
-- for payout_pending, paying, completed, under_review and failed. A finished swap's
-- address stops being looked at, so a late payment to it is never seen by anything.
--
-- WHY IT IS NOT A deposit_events ROW. That table is what refresh_swap_from_chain() SUMS to
-- decide whether a deposit is confirmed and a payout may be released. A row here would be
-- summed into a settled swap's confirmed_total and would re-arm or halt a swap that has
-- already paid out. Recording must decide nothing about the money (the same refusal
-- migrate_deposit_vouts.py makes about rewriting settled swaps), and the only way to
-- guarantee that is for the record to live where no gate reads it.
--
-- WHY IT IS NOT AN unattributable_deposits ROW, AND THIS IS THE DISTINCTION THAT EARNS A
-- SECOND TABLE. That table's own comment above says it holds money that "belongs to no
-- swap -- that is the definition of unattributable", that it holds "no swap_id, now or
-- later", and that filling one in "would make this table a second source of truth about
-- who owns a deposit". A late deposit is the OPPOSITE case: the deposit address belongs to
-- exactly one swap, so attribution is known and certain; what is missing is a swap that is
-- still willing to accept it. Writing it there would need either a swap_id column that
-- table forbids, or `discriminator` overloaded to mean a vout on one chain and a
-- DestinationTag on another -- one column meaning two things, which is rule 11's defect.
-- So: a separate table, where swap_id IS a FOREIGN KEY because the attribution is a fact
-- rather than an opinion.
--
-- KEYED ON (asset, txid, vout) AND NOT (asset, txid), which is the other way round from
-- unattributable_deposits and for the reason that table states. There, `vout` carries the
-- integer discriminator and the row exists BECAUSE no usable discriminator was found, so
-- it cannot be part of the key. Here `vout` is a real output index on a UTXO chain and one
-- transaction can pay the same address at two outputs -- two separate payments, two rows.
-- It is the same triple deposit_events is keyed on, deliberately, so the two tables answer
-- "have I seen this output before" the same way.
--
-- NOTHING ON THE ORDER PATH MAY READ THIS, exactly as for unattributable_deposits. No
-- gate, no crediting, no payout, no pricing may join against it. It records and surfaces;
-- the operator decides what happens to the coins, and the decision is the
-- `resolved_at`/`resolution_note` pair, which is a person's note rather than a transition.
CREATE TABLE IF NOT EXISTS late_deposits (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    -- The swap whose deposit address this payment landed on. A FOREIGN KEY, unlike
    -- unattributable_deposits, because on an address-attributed chain the address IS the
    -- attribution: swap_service allocates a fresh address per swap and no other swap
    -- shares it. See the block above for why that difference justifies a second table.
    swap_id TEXT NOT NULL,
    -- The swap's status AT THE MOMENT THIS ROW WAS WRITTEN, carried rather than looked up.
    -- An operator reading the row a week later needs to know whether the money arrived
    -- after a COMPLETED payout (the desk is holding a customer's extra send) or after a
    -- FAILED one (the desk may still owe the original payout too). Joining to swaps would
    -- give today's status, which is not the one that made this a late deposit.
    swap_status TEXT NOT NULL,
    asset TEXT NOT NULL,
    txid TEXT NOT NULL,
    vout INTEGER NOT NULL,
    -- Carried rather than looked up through swap_id, for the same reason
    -- unattributable_deposits carries it: a row still says where the coins are after the
    -- swap row or the configuration changes.
    address TEXT NOT NULL,
    -- Whole units of the asset, the same scale as deposit_events.amount and
    -- unattributable_deposits.amount. A record of money that omits the amount is not a
    -- record of money.
    amount REAL NOT NULL,
    confirmations INTEGER NOT NULL,
    first_seen_at TEXT NOT NULL,
    last_seen_at TEXT NOT NULL,
    resolved_at TEXT,
    resolution_note TEXT,
    -- THE ADDRESS JOINS THE KEY HERE TOO, for the reason stated at length on
    -- deposit_events above: an ICP txid is a ledger block index and is unique only within
    -- one ledger. The comment block above deposit_events' own UNIQUE carries the
    -- measurement; this table has the identical latent collision and is fixed in the same
    -- commit rather than left as a second instance to be found later (rule 19: when you
    -- find N instances, fix them -- not one).
    --
    -- NOT YET OBSERVED HERE, and that is said plainly rather than implied: the collision
    -- was measured on deposit_events and this table has the same key over the same kind of
    -- row. A latent duplicate of a defect is still the defect.
    --
    -- unattributable_deposits is deliberately NOT widened. Its key is UNIQUE(asset, txid)
    -- with no vout -- the comment above it says why the discriminator cannot be part of a
    -- key -- and only TAG-ATTRIBUTED chains ever write to it, because an address-attributed
    -- deposit is attributed BY its address and can never be unattributable. Those chains
    -- share one account each, so every row on an asset already carries the same address and
    -- adding it would change nothing while rebuilding a table for no reason.
    UNIQUE(asset, txid, vout, address),
    FOREIGN KEY (swap_id) REFERENCES swaps(id)
);

-- The query an operator actually runs: what is still outstanding, oldest first. Mirrors
-- idx_unattributable_deposits_unresolved above, for the identical question.
CREATE INDEX IF NOT EXISTS idx_late_deposits_unresolved
    ON late_deposits(resolved_at, first_seen_at);

CREATE TABLE IF NOT EXISTS payouts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    swap_id TEXT NOT NULL,
    asset TEXT NOT NULL,
    destination_address TEXT NOT NULL,
    amount REAL NOT NULL,
    txid TEXT,
    status TEXT NOT NULL,
    created_at TEXT NOT NULL,
    sent_at TEXT,
    FOREIGN KEY (swap_id) REFERENCES swaps(id)
);

CREATE INDEX IF NOT EXISTS idx_payouts_swap_id ON payouts(swap_id);

-- WHAT THE DESK HAS TAKEN OUT OF ITS OWN FEE. One row per sweep attempt, written
-- by collect_fees.py and by nothing else.
--
-- WHY IT EXISTS AT ALL. fee_ledger.py DERIVES what each completed swap retained
-- (realized_gross - paid) from `swaps` and `payouts`, so the accrued fee is a
-- query and needs no table. What is NOT derivable from those rows is whether the
-- desk has already MOVED some of it: the retained fee never had a row of its own,
-- it is simply coin that stayed in the hot wallet, and a sweep that sends it out
-- leaves no trace in `swaps` or `payouts`. Without this table a second run of
-- collect_fees.py would compute the same accrued figure and sweep it again --
-- money out twice against one accrual, with the second send coming out of
-- customer deposits. Operator, 2026-10-04: "i NEED to collect a fee to be
-- profitable", and a collector that cannot be run twice safely is not one.
--
-- SO THIS IS THE ONE FACT A DERIVATION CANNOT SUPPLY, and it is stored for that
-- reason rather than as a convenience cache. The accrued side stays derived
-- (rules 5 and 20); only the irreversible act is recorded.
--
-- THE SHAPE IS `payouts`, DELIBERATELY, down to the column names and the two
-- timestamps. Both tables record "this desk broadcast a transaction for this
-- amount to this address", the sweep is written through the same ordering
-- services/payout_service.py uses -- INSERT 'created' and COMMIT before the send,
-- UPDATE to 'broadcast' with the txid INSIDE the wallet-unlock context before the
-- re-lock can raise -- and a reader who knows one table can read the other. A
-- second vocabulary for the same event would be rule 8's duplicate.
--
--   created    the intent is durable and the send has not returned. A row left in
--              this state is money POSSIBLY on chain with no txid, exactly like a
--              `payouts` row in 'created'; it is counted as ALREADY SWEPT so a
--              rerun cannot send it twice, and collect_fees.py prints it as an
--              alarm rather than a total. That is the direction that cannot lose
--              money: the cost of over-counting is an unswept fee, the cost of
--              under-counting is a double send.
--   broadcast  the txid is recorded. Final; a broadcast cannot be unsent.
--   failed     the send raised and nothing reached a chain. NOT counted as swept,
--              so the fee stays sweepable -- the same reason 'failed' is absent
--              from db.PAYOUT_LIVE_STATUSES.
--
-- NO swap_id AND NO FOREIGN KEY, which is the one structural difference from
-- `payouts` and is a fact about what a sweep IS. A sweep is not attributable to a
-- swap: it moves the pooled retention of every completed swap in that asset, so a
-- swap_id column would have to hold either a lie or a NULL on every row. The
-- accrual it is drawn against is the fee_ledger derivation over ALL of them, and
-- `asset` is the only key that means anything.
--
-- NOTHING GATES A PAYOUT ON THIS TABLE. collect_fees.py is its only reader and
-- only writer; the payout path does not import it, and a customer payout is
-- unaffected by whether a sweep ever ran. What protects a customer is the
-- retention arithmetic in fee_sweep.py, which reads `swaps` -- not this table.
CREATE TABLE IF NOT EXISTS fee_sweeps (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    asset TEXT NOT NULL,
    destination_address TEXT NOT NULL,
    amount REAL NOT NULL,
    txid TEXT,
    status TEXT NOT NULL,
    created_at TEXT NOT NULL,
    sent_at TEXT
);

-- The query collect_fees.py actually runs: how much of this asset has been swept.
CREATE INDEX IF NOT EXISTS idx_fee_sweeps_asset_status ON fee_sweeps(asset, status);

-- THE TABLE IS CALLED INVENTORY AND hot_confirmed IS A WALLET BALANCE. The name
-- stays -- it is an identifier, and renaming a column on a live-money system is a
-- posture change and a large diff with no reader benefit -- but what the column
-- HOLDS is written down here, because every surface that rendered it took the
-- table's name for the number's meaning until 2026-10-03.
--
-- MEASURED BEHAVIORALLY THAT DAY, not read off the source: calling
-- services/payout_service.refresh_wallet_inventory() against a stub adapter made
-- exactly one RPC call, `getbalance`, and stored hot_confirmed equal to what it
-- returned. chains/base.RPCAdapter.get_balance() is `getbalance` with NO
-- arguments, so on a Bitcoin-derived chain this is the WHOLE wallet the endpoint
-- serves: every address, change included.
--
-- AND ON THIS TREE'S DEFAULTS THAT IS THE OPERATOR'S OWN WALLET. BTC_RPC_WALLET,
-- LTC_RPC_WALLET and GRC_RPC_WALLET are all `_env(..., "")` (config.py:447, 455,
-- 520), and chains/base.RPCAdapter.url appends `/wallet/<name>` only for a
-- non-empty value -- so an unset one addresses the daemon with no wallet path and
-- the daemon routes to its DEFAULT wallet, the one `gridcoinresearchd
-- getnewaddress` reaches. Whatever the operator holds there is inside
-- hot_confirmed, recorded as desk stock.
--
--   hot_confirmed   the whole wallet, from get_balance(). NOT committed desk stock.
--   hot_reserved    the sum of payouts claimed and not yet sent, which IS a desk
--                   figure: reserve_inventory() adds and release_inventory_after_send()
--                   subtracts, per payout.
--   hot_available   hot_confirmed - hot_reserved, so it inherits the above.
--
-- NOTHING GATES A PAYOUT ON THIS TABLE, established by grep 2026-10-02 and
-- re-established behaviorally 2026-10-03: zeroing hot_confirmed and re-running
-- services/payout_capacity.largest_fundable_payout() returned the SAME ceiling,
-- because that function calls adapter.get_balance() itself. RE-ESTABLISHED
-- BEHAVIORALLY AGAIN 2026-10-04 against a seeded row carrying the operator's own
-- figures: hot_available=-6102.288412361736 for GRC, and both
-- services/payout_capacity.largest_fundable_payout() and
-- why_the_payout_cannot_be_funded() returned the SAME answers before and after
-- hot_confirmed was zeroed. A negative hot_available cannot refuse a customer
-- payout. wallet_custody.py is the tool that says which wallet an endpoint
-- actually serves.
--
-- THE READER LIST THIS COMMENT USED TO GIVE WAS INCOMPLETE, AND THE OMISSION WAS
-- THE CONSEQUENTIAL ONE. It read "services/admin_view.py's display query, this
-- file's reserve bookkeeping, and rescue_payout.py's release", which is three of
-- four. The fourth is swap_terminal/fee_sweep.obligation(), which reads
-- hot_reserved and sets `floor = max(open_total, reserved)` -- so an inflated
-- reservation raises the retention floor a fee sweep has to clear. Measured the
-- same day, with the operator's figures seeded: obligation() returned
-- floor=9882.372957331736 against a wallet holding 3780.08454497 GRC and
-- open_total=0.0, which refuses every GRC fee sweep for as long as the figure
-- stands. "Nothing gates a PAYOUT on this table" is still true and is a narrower
-- claim than the list was being read as.
--
--   readers, 4 of 4 as of 2026-10-04
--     services/admin_view.py              displays all three columns
--     services/payout_service.py          reserve_inventory / release_inventory_after_send
--     rescue_payout.py                    release
--     swap_terminal/fee_sweep.py          obligation(), reads hot_reserved INTO A FLOOR
--
-- repair_inventory_reservations.py is the tool that reports and repairs a
-- hot_reserved figure the payout rows do not justify, and
-- wallet_inventory_corrections (below) is where it records what it replaced.
CREATE TABLE IF NOT EXISTS wallet_inventory (
    asset TEXT PRIMARY KEY,
    hot_confirmed REAL NOT NULL DEFAULT 0,
    hot_reserved REAL NOT NULL DEFAULT 0,
    hot_available REAL NOT NULL DEFAULT 0,
    updated_at TEXT NOT NULL
);

-- THE MARKET CONTEXT BEHIND A PRICE. Added 2026-09-27 at the operator's
-- instruction: "we should also keep track of the market cap and price comparison
-- to better establish grc prices."
--
-- One row per asset per fetch, written by services/market_context.py from the
-- snapshots services/pricing.py returns. It is EVIDENCE, and the reason it is a
-- table rather than a log line is rule 7: a measurement that only exists in a log
-- is not learning, and "better establish GRC prices" is a question about what
-- GRC's price, cap and volume were doing an hour and a day ago -- which can only
-- be asked of rows.
--
-- IT IS A BUFFER, NOT AN AUTHORITY, AND NOTHING READS IT TO DECIDE ANYTHING
-- (rule 15). It lives in swap_terminal.db rather than in a second file because
-- there is one authority and this is part of it; it earns no exemption. What it
-- must not become is an input to a payout: services/market_context.py's
-- price_confidence() is deliberately not wired into the quote path, and its
-- docstring says what wiring it in would mean.
--
-- APPEND ONLY. There is no UPDATE and no DELETE anywhere in
-- services/market_context.py, and no trigger enforcing that -- which is stated
-- rather than glossed, because the xrp_destination_tags triggers below show what
-- enforcement looks like when it matters. It is not enforced here because a
-- misedited observation cannot pay anybody the wrong amount; a misedited
-- destination tag can. If this table ever becomes an input to a decision, that
-- asymmetry stops holding and the triggers should follow.
--
-- NO UNIQUE CONSTRAINT ON (asset, fetched_at), considered rather than forgotten.
-- Two processes fetching inside the same RATE_CACHE_SECONDS window read the same
-- process cache and therefore report the same fetched_at, so a unique index would
-- turn a harmless duplicate observation into an IntegrityError on a diagnostic
-- path. A duplicate row is deduplicated by whoever reads it; a failed write is
-- lost evidence.
--
-- THE THREE NULLABLE COLUMNS ARE NULLABLE ON PURPOSE AND MUST NOT BE GIVEN
-- DEFAULT 0. CoinGecko returns partial data for thin assets -- exactly the class
-- GRC is in -- so "no market cap datum" is an ordinary response. DEFAULT 0 would
-- make it indistinguishable from a real zero, and price_confidence() would then
-- report a confident THIN verdict about a number nobody measured. NULL means
-- nobody said; 0 means somebody said zero.
--
-- source_updated_at is CoinGecko's own unix second for the quote and fetched_at
-- is when we asked. Both are kept because the GAP between them is how stale the
-- feed itself was, and that is not derivable from either alone. They are REAL /
-- INTEGER unix seconds rather than ISO text because they are arithmetic inputs
-- (a subtraction), while recorded_at is ISO text like every other timestamp
-- column in this schema, because it is only ever read.
--
-- THERE IS NO `verdict` COLUMN, and that is a rule 8 decision. The verdict is a
-- pure function of these columns plus the config window, so storing it would put
-- one rule in two places: change a threshold and every historical row would still
-- assert the old answer, so the table would disagree with the code about a past
-- that cannot be re-measured. Store what CoinGecko said; derive the verdict at
-- read time.
--
-- The column names are the field names of services/pricing.MarketSnapshot, which
-- is where the INSERT's column list is derived from. They are not derived INTO
-- this DDL, because db.py is imported by every worker at startup and importing
-- services/pricing.py here would pull `requests` into that path for the sake of a
-- column list. tests/test_market_context.py closes the gap by asserting PRAGMA
-- table_info against that tuple -- the schema as the database actually built it,
-- not as this text spells it.
CREATE TABLE IF NOT EXISTS market_context (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    asset TEXT NOT NULL,
    coingecko_id TEXT NOT NULL,
    price_usd REAL NOT NULL,
    market_cap_usd REAL,
    volume_24h_usd REAL,
    change_24h_pct REAL,
    source_updated_at INTEGER,
    fetched_at REAL NOT NULL,
    recorded_at TEXT NOT NULL
);

-- The index the one read actually uses: recent_market_context() filters by asset
-- and orders by fetched_at descending, which is this index read backwards.
CREATE INDEX IF NOT EXISTS idx_market_context_asset_fetched_at ON market_context(asset, fetched_at);

CREATE TABLE IF NOT EXISTS swap_audit_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    swap_id TEXT NOT NULL,
    old_status TEXT,
    new_status TEXT NOT NULL,
    message TEXT,
    created_at TEXT NOT NULL,
    FOREIGN KEY (swap_id) REFERENCES swaps(id)
);
-- PROVING OWNERSHIP OF AN ADDRESS BY SIGNATURE. Added 2026-10-02 at the operator's
-- request for "GRC login ability", which they specified as: let someone prove they own a
-- Gridcoin address by signing a challenge with their own wallet.
--
-- THE FLOW, so the columns make sense. This page issues a CHALLENGE bound to one swap. The
-- customer signs it on THEIR OWN MACHINE, in their own client (`signmessage <address>
-- "<challenge>"`). They paste back the ADDRESS and the SIGNATURE. This desk calls
-- `verifymessage` on our own daemon, which is pure signature math, and on success the
-- address is recorded as PROVEN for that swap.
--
-- NO PRIVATE KEY, SEED, WIF OR PASSPHRASE EVER REACHES THIS SYSTEM, and that is the entire
-- reason this design was chosen over every alternative rather than a nice property of it.
-- Nothing in this table can hold one: there is no key column, no passphrase column, and the
-- one thing a customer could plausibly paste in error is refused before it is ever read --
-- see secret_key_shapes.py and services/grc_login_service.py.
--
-- THERE IS NO `signature` COLUMN, and leaving it out is a decision rather than an omission.
-- A signature is not a secret -- it reveals nothing but that the key exists -- but it IS
-- REPLAYABLE against the message it signed. Storing it would create a durable list of
-- (challenge, signature) pairs whose only use is to prove the same thing again, and the one
-- thing that must never happen to a challenge is being accepted twice. The same reasoning
-- keeps it out of the log (rule 7's "never delete evidence" does not ask for evidence that
-- only helps an attacker: the EVIDENCE here is that the address was proven, and that is
-- `proven_address` plus `proven_at`).
--
-- NOTHING ON THE PAYOUT PATH READS THIS TABLE, AS OF THIS COMMIT. A proven address does not
-- change what gets paid, where it gets paid, how much, or whether a swap may proceed.
-- Wiring it into payout authorization would change where money goes, which is live posture
-- and the operator's call (rule 16) -- so it is recorded and not consulted. If that changes,
-- the gate belongs in SQL as a view over this table joined to swaps (rules 5 and 20), not as
-- a Python check at a send site.
--
-- WHY ONE TABLE AND NOT TWO (challenges + proofs). A proof IS a consumed challenge, and
-- splitting them would put "has this challenge been used?" in two places that can disagree
-- -- rule 8's bug with a delay on it, on the exact invariant that stops a replay. With one
-- row, consumption and proof are the same write: a single conditional UPDATE sets
-- proven_address and proven_at only if proven_at IS NULL, so there is no check-then-write
-- window. That window is not hypothetical in this repository: two payout workers paid one
-- swap twice through a correct-looking Python guard on 2026-09-24
-- (tests/test_payout_concurrency.py), and the fix had this shape.
CREATE TABLE IF NOT EXISTS address_proof_challenges (
    -- THE CHALLENGE STRING ITSELF IS THE PRIMARY KEY. It is generated with `secrets`
    -- (192 bits of CSPRNG nonce; never `random`), so it is unguessable, and making it the
    -- key is what lets single-use be expressed as one UPDATE against one row instead of a
    -- SELECT followed by an UPDATE. A surrogate integer id would have needed a UNIQUE index
    -- on this column anyway, and then the key would not be the thing the customer signs.
    challenge TEXT PRIMARY KEY,
    -- WHICH SWAP THIS CHALLENGE BELONGS TO. The binding is what stops a signature captured
    -- for swap A from proving an address on swap B: the consuming UPDATE matches on
    -- (challenge, swap_id), so a challenge presented against the wrong swap updates zero
    -- rows. The challenge TEXT also contains the swap id for a human reading it, which is
    -- visibly redundant on purpose -- the column is the authority and the text is for the
    -- person pasting it.
    swap_id TEXT NOT NULL,
    -- WHICH CHAIN'S ADDRESS IS BEING PROVEN. "GRC" is the only value any code writes today.
    -- It is a column rather than an assumption because `verifymessage` is a Bitcoin-family
    -- method that Litecoin and Bitcoin answer identically, so the second chain to want this
    -- needs no migration -- and because a proof with no chain on it is ambiguous the moment
    -- there is a second one. The same shape as services/xrp_tag_service.ACCOUNT_VALIDATORS,
    -- which became a table keyed by asset after the one-chain version silently refused the
    -- second chain.
    asset TEXT NOT NULL,
    -- WHEN IT WAS ISSUED. Kept alongside expires_at rather than derived from it, because the
    -- TTL may change and a row has to keep saying what window it was actually given.
    issued_at TEXT NOT NULL,
    -- WHEN IT STOPS BEING ACCEPTABLE. THE EXPIRY IS A SQL PREDICATE, NOT A PYTHON
    -- COMPARISON (rules 5 and 20): the consuming UPDATE carries
    -- `julianday(expires_at) > julianday(:now)`, so a challenge cannot be accepted late even
    -- by a caller who forgot to look. A Python check before the write would be the same
    -- stale-read window the single-use rule above is built to avoid, and it would be
    -- answerable only by whoever ran it.
    --
    -- WHY EXPIRE AT ALL, given the challenge is already single-use. A challenge that never
    -- expires is a signature request that stays live forever: it sits in the customer's
    -- terminal history, in a support ticket, in a screenshot. Single-use bounds how MANY
    -- times a captured signature can be used; expiry bounds how LONG a captured challenge is
    -- worth capturing. ISO text, and julianday() rather than a string comparison because
    -- julianday parses the UTC offset instead of trusting two strings to be formatted alike.
    expires_at TEXT NOT NULL,
    -- THE ADDRESS THAT WAS PROVEN, and NULL until one is. A NULL pair (this and proven_at)
    -- is an outstanding challenge; a non-NULL pair is a proof. That is the whole state
    -- machine, and it is two columns rather than a `status` string so that no row can claim
    -- a status its data does not support.
    proven_address TEXT,
    proven_at TEXT,
    -- A HALF-WRITTEN PROOF IS NOT A PROOF. Without this, `UPDATE ... SET proven_at = ?`
    -- alone would mark the challenge consumed while recording no address -- a row that
    -- blocks any further attempt and proves nothing, which is the worst of both outcomes for
    -- the customer. Expressed as a CHECK and not a convention because the two columns are
    -- what every reader of this table joins on. NAMED, so the IntegrityError says which rule
    -- was broken (the xrp_tag_is_allocatable constraint is named for the same reason, and
    -- the message it produces was measured).
    CONSTRAINT address_proof_is_whole CHECK ((proven_address IS NULL) = (proven_at IS NULL)),
    FOREIGN KEY (swap_id) REFERENCES swaps(id)
);

-- The two queries that exist: "is there still an unexpired, unconsumed challenge for this
-- swap?" (so a page reload does not invalidate what the customer is part-way through
-- signing) and "what has this swap proven?". Both start from swap_id and discriminate on
-- proven_at, which is this index read in either direction.
CREATE INDEX IF NOT EXISTS idx_address_proof_challenges_swap
    ON address_proof_challenges(swap_id, proven_at, expires_at);

-- SINGLE USE, AS SOMETHING THE DATABASE WILL NOT LET ANYONE UNDO.
--
-- The conditional UPDATE in services/grc_login_service.py is the MECHANISM -- it is what
-- makes consumption atomic. This trigger is the GUARANTEE, and the difference matters: the
-- mechanism depends on every future writer remembering to carry `AND proven_at IS NULL`, and
-- a writer who forgets gets a green test and a replayable proof. The trigger cannot be
-- forgotten.
--
-- WHAT A REPLAY WOULD COST, which is why this is enforced rather than conventional. A
-- signature over a fixed message is valid forever: it is in the customer's shell history and
-- in whatever they pasted it into. If the same challenge could be consumed twice, anyone who
-- ever saw one signature could prove ownership of that address again at any later time,
-- including after the customer stopped controlling the key. Single-use is what makes a
-- captured signature worthless the moment it has been used once.
--
-- WHEN OLD.proven_at IS NOT NULL, so an outstanding challenge can still be consumed -- the
-- trigger forbids rewriting a PROOF, not writing one.
--
-- EACH OF THE THREE MESSAGES BELOW BEGINS WITH ITS OWN TRIGGER'S NAME, and that is not
-- decoration. MEASURED 2026-10-02: SQLite's RAISE(ABORT, msg) puts ONLY msg in the
-- IntegrityError -- the trigger name appears nowhere in it -- so a reader who finds one of
-- these strings in a log has nothing to grep for unless the message says which rule fired.
-- A named CHECK does carry its name ("CHECK constraint failed: address_proof_is_whole"), which
-- is why address_proof_is_whole above needs no prefix and these three do.
CREATE TRIGGER IF NOT EXISTS address_proofs_are_single_use
BEFORE UPDATE ON address_proof_challenges
WHEN OLD.proven_at IS NOT NULL
BEGIN
    SELECT RAISE(ABORT, 'address_proofs_are_single_use: a challenge that has already proven an address is immutable. A message signature is valid forever, so re-accepting a spent challenge would let anyone who ever saw that signature prove the address again at any later time');
END;

-- The binding columns are immutable from the moment the row exists, proven or not.
--
-- Separate from the trigger above because it fires on a DIFFERENT condition: that one
-- protects a completed proof, this one protects the QUESTION. Re-pointing a live challenge
-- at another swap would hand that swap a proof its customer never produced -- the same hazard
-- xrp_destination_tags_are_never_repointed exists for, one table over, and the reason that
-- one's comment gives applies verbatim here.
CREATE TRIGGER IF NOT EXISTS address_proof_challenges_are_not_repointed
BEFORE UPDATE OF challenge, swap_id, asset, issued_at, expires_at ON address_proof_challenges
BEGIN
    SELECT RAISE(ABORT, 'address_proof_challenges_are_not_repointed: challenge, swap_id, asset, issued_at and expires_at are immutable once the challenge is issued. Re-pointing a live challenge at another swap would give that swap a proof its customer never produced, and extending expires_at would revive a challenge the customer has already been told is dead');
END;

-- A RECORDED PROOF IS EVIDENCE AND IS NEVER DELETED (rule 7).
--
-- Narrowed to rows that actually carry a proof, which is the one place this diverges from
-- xrp_destination_tags_are_never_released. There, allocation reads MAX() over every row, so
-- deleting ANY row lets the sequence go backward and a late payment credit a stranger's swap
-- -- nothing may be deleted at all. Here, an UNCONSUMED challenge that has expired is inert:
-- it can never be accepted again (the UPDATE's own predicate refuses it), it feeds no
-- sequence, and keeping it forever is housekeeping rather than safety. A PROVEN row is the
-- record of what a customer demonstrated, and deleting one would destroy the only evidence
-- that it happened.
CREATE TRIGGER IF NOT EXISTS address_proofs_are_never_deleted
BEFORE DELETE ON address_proof_challenges
WHEN OLD.proven_at IS NOT NULL
BEGIN
    SELECT RAISE(ABORT, 'address_proofs_are_never_deleted: a row recording a proven address is evidence and is never deleted. An expired challenge that proved nothing may be deleted freely -- it feeds no sequence and can never be accepted again');
END;
"""


# THE XRP TAG DDL IS DERIVED, NOT SPELLED A SECOND TIME.
#
# The two bounds in the CHECK come from chains/xrp_units.py, which is where
# the measurement behind them lives. Writing `BETWEEN 1 AND 4294967295` into
# the schema literal would put the same rule in two files, which is rule 8's
# bug-with-a-delay-on-it: the copies agree the day they are written, and the
# one that drifts is whichever file the next reader does not open. Mammon's
# contract_horizon_sql() is generated from _HORIZON_SUFFIXES for exactly this
# reason, and this is the same shape.
#
# An f-string building SQL would normally be S608 territory. It is not
# interpolating input or an identifier here -- both values are module-level
# integer constants from this application's own source, and ruff does not
# flag it because there is no execute() call in sight: this is a DDL string
# handed to executescript() at startup.
XRP_DESTINATION_TAG_SCHEMA = f"""
-- THE XRP DEPOSIT IDENTIFIER. One shared account, one integer per swap.
--
-- Every other chain here hands out a fresh deposit address, so `swaps.deposit_
-- address` alone identifies who paid. The XRP Ledger does not work that way:
-- chains/xrp.py::get_new_address() refuses precisely because deriving an
-- account per swap would cost a base reserve and put a signing key per swap on
-- this host, and the ledger's own answer -- the one every exchange on it uses
-- -- is a `DestinationTag`: an integer carried by the payment, read by
-- chains/xrp_payments.py into the event's `vout` field.
--
-- WHY THIS IS A TABLE AND NOT A COLUMN ON `swaps`.
--
-- The invariant that matters is UNIQUENESS, and it is worth stating what it
-- costs to lose: two open swaps sharing a tag means one customer's deposit is
-- credited to the other customer's swap, and the payout that follows is on
-- chain and final. A nullable `swaps.xrp_destination_tag` could carry a UNIQUE
-- index too, but it could not carry the other three guarantees below, and it
-- would be NULL for five of the six assets -- a column that means nothing for
-- most rows is a column readers have to learn the exception for.
--
-- FOUR GUARANTEES, ALL OF THEM IN THE DATABASE (rules 5, 15 and 20). None is a
-- Python check, because a Python check-then-insert is a read that can go stale
-- before the write -- which is not a hypothesis here: it is exactly how two
-- payout workers paid one swap twice on 2026-09-24, measured in
-- tests/test_payout_concurrency.py, and the fix was the same shape as this.
--
--   PRIMARY KEY (account, destination_tag)
--        No two rows share a tag on one account. Per ACCOUNT rather than
--        globally because that is the real scope -- a tag means nothing except
--        against the account it was sent to -- and it keeps the numbers small
--        if the operator ever moves accounts.
--
--   UNIQUE swap_id (idx_xrp_tag_one_per_swap)
--        No swap gets two tags. A swap with two tags is a swap whose deposit
--        instructions differ depending on which row you read.
--
--   CONSTRAINT xrp_tag_is_allocatable
--        FIRST_ALLOCATABLE_TAG..MAX_DESTINATION_TAG, which is 1..4294967295.
--        The upper bound is the protocol's, MEASURED against xrpl-py's own
--        serializer (chains/xrp_units.py point 4). The LOWER bound is OURS: 0
--        is a perfectly legal tag and is reserved unallocated, because it is
--        what every "no tag to send" integration emits. The reasoning is at
--        chains/xrp_units.RESERVED_DESTINATION_TAG and is not repeated here.
--        The constraint is NAMED so the IntegrityError says which rule was
--        broken -- measured: "CHECK constraint failed: xrp_tag_is_allocatable".
--
--   xrp_destination_tags_are_never_released / _repointed
--        Two BEFORE triggers that RAISE(ABORT). This is the reuse decision,
--        expressed as something the database will not let anyone do rather than
--        as a convention a future writer can forget. TAGS ARE NEVER REUSED:
--        the XRP Ledger puts no expiry on a tag, an address-book entry or a
--        withdrawal retry can carry one months after a swap completed, and a
--        reallocated tag turns that late payment into a credit against a
--        stranger's swap. Allocation reads MAX(destination_tag) over ALL rows,
--        so as long as no row is ever deleted the sequence cannot go backward
--        -- the DELETE trigger is what makes that "cannot" rather than
--        "should not". The cost is exhaustion, and it is not a real cost: at
--        4,294,967,295 tags, 1,000 swaps a day lasts 11,759 years and 10,000 a
--        day lasts 1,176.
--
-- THERE IS NO `retired_at` COLUMN, on purpose. Whether a swap is finished is
-- already `swaps.status`, and a second copy of that fact here would be rule
-- 8's two-copies-drift: the row that says a tag is retired and the swap that
-- says it is still open, each correct in its own table. Join instead.
--
-- ORDERING NOTE FOR A CALLER: the FOREIGN KEY means the swap row must exist
-- before its tag is allocated. Measured 2026-09-26 and worth knowing before
-- relying on it -- `PRAGMA foreign_keys` defaults to OFF on a fresh
-- sqlite3 connection and is turned ON by the pragma at the top of this SCHEMA,
-- so the constraint bites on any connection that ran executescript(SCHEMA)
-- (every worker, every cycle) and does not on a bare connect_db().
CREATE TABLE IF NOT EXISTS xrp_destination_tags (
    account TEXT NOT NULL,
    destination_tag INTEGER NOT NULL,
    swap_id TEXT NOT NULL,
    allocated_at TEXT NOT NULL,
    PRIMARY KEY (account, destination_tag),
    CONSTRAINT xrp_tag_is_allocatable CHECK (destination_tag BETWEEN {FIRST_ALLOCATABLE_TAG} AND {MAX_DESTINATION_TAG}),
    FOREIGN KEY (swap_id) REFERENCES swaps(id)
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_xrp_tag_one_per_swap ON xrp_destination_tags(swap_id);

CREATE TRIGGER IF NOT EXISTS xrp_destination_tags_are_never_released
BEFORE DELETE ON xrp_destination_tags
BEGIN
    SELECT RAISE(ABORT, 'xrp_destination_tags rows are never deleted: allocation reads MAX(destination_tag), so a deleted row lets the next tag repeat one already given out, and a late payment carrying it would credit the wrong swap');
END;

CREATE TRIGGER IF NOT EXISTS xrp_destination_tags_are_never_repointed
BEFORE UPDATE OF account, destination_tag, swap_id ON xrp_destination_tags
BEGIN
    SELECT RAISE(ABORT, 'xrp_destination_tags: account, destination_tag and swap_id are immutable once allocated. Re-pointing a tag at a different swap misattributes every payment already in flight against it');
END;
"""

# One string for executescript(). Concatenated rather than interpolated into
# SCHEMA itself so that SCHEMA stays a plain literal and only the part that
# genuinely needs derived values is an f-string.
#
# ORDER: after `swaps`, because xrp_destination_tags has a FOREIGN KEY into
# it. SQLite resolves foreign key TARGETS at DML time rather than at CREATE
# time, so this ordering is for a human reader rather than for the engine.
SCHEMA = SCHEMA + XRP_DESTINATION_TAG_SCHEMA

# ICP DEPOSIT SUBACCOUNTS -- the same problem as xrp_destination_tags, one chain
# over, and NOT the same table. Read both before changing either (rule 8: where
# two implementations genuinely differ, the difference belongs in a comment at
# BOTH sites naming the other).
#
# WHAT IS THE SAME, and why this is a copy of that shape rather than an invention:
# a shared account the desk owns, a per-swap label that tells one customer's
# payment from another's, allocation that must be UNIQUE by database constraint
# rather than by argument, and a row that may never be deleted or re-pointed. The
# XRP table earned every one of those the hard way and they transfer unchanged.
#
# WHAT DIFFERS, and it is the reason these are two tables instead of one generic
# one. The label lives in a different place on the wire: an XRP destination tag
# is a 32-bit field the PAYER must set, a Solana memo is an instruction the PAYER
# must attach, and an ICP subaccount is 32 bytes the RECEIVER chooses -- the payer
# sends to an ordinary address and needs to know nothing. That changes the range
# (2**32 against 2**256), changes what a missing label means (an unattributable
# payment against an impossible state), and changes who can get it wrong. Merging
# them would also mean editing the table that carries live XRP deposit
# attribution for real swaps, which is a large diff on a live-money path with no
# behavioral benefit -- the trade rule 10 refuses for directory layout.
#
# WHY THE FIRST ALLOCATABLE INDEX IS 1 AND NOT 0, AND THIS IS MEASURED. Subaccount
# 0 (32 zero bytes) is the DEFAULT subaccount, which is to say it is the desk's own
# main account. Confirmed 2026-10-06 against the real ICP ledger on the local
# replica:
#
#     icrc1_balance_of(record { owner = principal "ybr6p-...-cqe" })
#       -> 100_000_000_000 : nat
#
# with no subaccount given -- that is the desk's entire 1000 LICP inventory sitting
# at index 0. Allocating index 0 to a swap would publish the desk's own holding
# account as a customer deposit address, so every arriving payment would land
# indistinguishably among the desk's own funds and the watcher would read the
# inventory as the deposit.
#
# The contrast with XRP is worth keeping: chains/xrp_units.py reserves tag 0 on an
# explicitly-labeled HYPOTHESIS about what senders emit as a placeholder. This
# reservation is not a hypothesis. Index 0 is the desk's account, and the ledger
# said so.
ICP_DEPOSIT_SUBACCOUNT_SCHEMA = """
CREATE TABLE IF NOT EXISTS icp_deposit_subaccounts (
    owner TEXT NOT NULL,
    subaccount_index INTEGER NOT NULL,
    swap_id TEXT NOT NULL,
    allocated_at TEXT NOT NULL,
    PRIMARY KEY (owner, subaccount_index),
    -- >= 1, never >= 0: index 0 is the desk's own account. See the comment above
    -- this schema for the ledger reading that establishes it.
    CONSTRAINT icp_subaccount_is_allocatable CHECK (subaccount_index >= 1),
    FOREIGN KEY (swap_id) REFERENCES swaps(id)
);

-- ONE subaccount per swap. Two would mean two published deposit addresses for one
-- swap, and a payment to the one the watcher is not polling looks exactly like a
-- customer who never paid.
CREATE UNIQUE INDEX IF NOT EXISTS idx_icp_subaccount_one_per_swap
    ON icp_deposit_subaccounts(swap_id);

CREATE TRIGGER IF NOT EXISTS icp_deposit_subaccounts_are_never_released
BEFORE DELETE ON icp_deposit_subaccounts
BEGIN
    SELECT RAISE(ABORT, 'icp_deposit_subaccounts rows are never deleted: allocation reads MAX(subaccount_index), so a deleted row lets the next index repeat one already published as a deposit address, and a late payment to it would credit the wrong swap');
END;

CREATE TRIGGER IF NOT EXISTS icp_deposit_subaccounts_are_never_repointed
BEFORE UPDATE OF owner, subaccount_index, swap_id ON icp_deposit_subaccounts
BEGIN
    SELECT RAISE(ABORT, 'icp_deposit_subaccounts: owner, subaccount_index and swap_id are immutable once allocated. Re-pointing a subaccount at a different swap misattributes every payment already in flight to that address, and on ICP the address is derived from the pair so the customer cannot be told it moved');
END;
"""

SCHEMA = SCHEMA + ICP_DEPOSIT_SUBACCOUNT_SCHEMA


# The one live payout per swap, as a CONSTRAINT rather than a convention.
#
# PARTIAL ON PURPOSE. A payout that genuinely FAILED must still be retryable,
# so 'failed' is not in the list: a plain UNIQUE(swap_id) would turn one
# rejected transaction into a permanently stuck swap. 'created' IS in the list,
# because a payout row written before a send is an intent to pay that may
# already have been relayed -- see services/payout_service.py's ordering note.
#
# It is not in SCHEMA above, and that is deliberate. Every worker runs
# `executescript(SCHEMA)` at the top of every cycle, so an index that FAILED to
# build -- which is exactly what happens on a database that already contains a
# double payout -- would kill all three workers on startup, including the two
# that have nothing to do with payouts. apply_migrations() below checks first
# and reports instead.
PAYOUT_UNIQUE_INDEX_NAME = "idx_payouts_one_live_per_swap"
# THERE IS A SECOND TUPLE OF THIS SHAPE AND IT IS NOT THE SAME TUPLE.
# swap_intents_schema.LIVE_PAYOUT_STATUSES is ("claimed", "broadcast",
# "completed") -- same length, same last two elements, DIFFERENT FIRST ELEMENT,
# and a name that differs from this one only by word order:
#
#     db.PAYOUT_LIVE_STATUSES         ("created", "broadcast", "completed")
#     swap_intents_schema             ("claimed", "broadcast", "completed")
#         .LIVE_PAYOUT_STATUSES
#
# The difference is REAL and must not be merged. They govern two tables with two
# indexes: this one is `payouts` / idx_payouts_one_live_per_swap, written by
# services/payout_service.py; the other is `swap_intent_payouts` /
# idx_swap_intent_payouts_one_live_per_intent, the store swap_intents.json was
# migrated into, whose payer CLAIMS a row before broadcasting. 'created' is not a
# status that table uses and 'claimed' is not one this table uses.
#
# This comment exists because rule 8 asks for it at BOTH sites and neither had
# it. Grepped 2026-10-07: zero mentions of either name in the other's file. The
# hazard is not abstract -- both tuples answer "does this row block a second
# payout for the same swap", and importing the wrong one gives a list whose first
# element names a status the table never writes, so the double-payout guard stops
# blocking the status that actually exists and nothing fails until two payouts for
# one customer are on chain. tests/test_payout_live_statuses_have_one_spelling.py
# asserts they stay different and that each file keeps naming the other.
PAYOUT_LIVE_STATUSES = ("created", "broadcast", "completed")

# THE INDEX'S STATUS LIST IS NOW DERIVED FROM THE CONSTANT ABOVE RATHER THAN
# SPELLED A SECOND TIME. Until 2026-10-04 these two lines read
#
#     PAYOUT_LIVE_STATUSES = ("created", "broadcast", "completed")
#     ... "ON payouts(swap_id) WHERE status IN ('created', 'broadcast', 'completed')"
#
# which is rule 8's shape at its smallest: one vocabulary, two spellings, agreeing
# on the day they were written. The cost of that drift is not hypothetical here --
# the tuple is what tests/ and swap_terminal/ read when they ask "is this payout
# live", and the string is what the DATABASE enforces. A fourth status added to the
# tuple alone would leave the constraint unchanged while every reader believed it
# had moved, and nothing would fail until two payout rows for one swap were
# accepted, on chain, for one customer.
#
# The values are formatted into the text rather than bound as parameters because a
# SQL parameter cannot bind a literal inside a partial index's WHERE clause: the
# index expression is STORED, so it has to be text. Every element is a literal from
# this module and none of them is ever input. Checked 2026-10-04 that the derived
# string is byte-identical to the one it replaced.
PAYOUT_UNIQUE_INDEX_SQL = (
    f"CREATE UNIQUE INDEX IF NOT EXISTS {PAYOUT_UNIQUE_INDEX_NAME} "
    f"ON payouts(swap_id) WHERE status IN ({', '.join(repr(status) for status in PAYOUT_LIVE_STATUSES)})"
)

#: THE STATUSES THAT JUSTIFY A STANDING RESERVATION IN `wallet_inventory`, and this
#: is DELIBERATELY NOT PAYOUT_LIVE_STATUSES. The difference is the point and it is
#: named at both sites (rule 8), because the two lists answer different questions
#: about the same rows and collapsing them would be wrong in both directions.
#:
#: PAYOUT_LIVE_STATUSES asks "may another payout row exist for this swap" -- so a
#: DELIVERED payout still counts, because a second send would be a double payment.
#:
#: This asks "is this asset's hot wallet still holding money back for a payout that
#: has not left". The reservation lifecycle, read off
#: services/payout_service.process_pending_payouts() on 2026-10-04:
#:
#:    reserve_inventory(amount)          hot_reserved += amount
#:    INSERT INTO payouts ... 'created'  the row the reservation is FOR
#:    broadcast_payout(...)
#:      on success  -> payouts 'broadcast' AND release_inventory_after_send()
#:      on failure  -> payouts 'failed'    AND NO RELEASE AT ALL
#:
#: So `created` is the ONLY status whose reservation is still owed. A `broadcast` or
#: `completed` row has already had release_inventory_after_send() subtract it, and
#: counting it again would double every delivered payout into the retention floor. A
#: `failed` row never had a release -- that is the leak this vocabulary exists to
#: measure -- and treating `failed` as justifying would declare the leak correct.
#:
#: MEASURED ON THE OPERATOR'S HOST 2026-10-04, which is why this is a shared constant
#: and not a literal inside one tool: GRC carried hot_reserved=9882.372957331736
#: against hot_confirmed=3780.08454497, so hot_available read -6102.288412361736,
#: beside 5 `failed` GRC payout rows totaling 9993.425862093694.
#: repair_inventory_reservations.py is the tool that reports and repairs it.
PAYOUT_RESERVED_STATUSES = ("created",)

#: WHERE A CORRECTION TO A MONEY COLUMN IS RECORDED, and why it is its own table
#: rather than a `swap_audit_log` row.
#:
#: swap_audit_log is KEYED TO A SWAP -- `swap_id TEXT NOT NULL` with a FOREIGN KEY to
#: swaps(id) -- and these corrections are per ASSET. A `wallet_inventory` row has no
#: swap: GRC's inflated reservation is the residue of several different failed
#: payouts across several different swaps, and the corrected figure belongs to none
#: of them in particular. The two ways to force it into swap_audit_log are both worse
#: than a table:
#:
#:   pick one of the swaps   falsifies that swap's trail -- the one record that
#:                           explains what happened to it -- and leaves the rest
#:                           unmentioned
#:   invent a swap_id        a row whose foreign key points at nothing, found later
#:                           by a reader with no way to tell it from corruption
#:
#: correct_payout_amounts.py uses swap_audit_log and is right to: it corrects
#: `payouts.amount`, and a payout row HAS a swap. The object corrected here is keyed
#: by asset, so the record is keyed by asset. The requirement is identical and is
#: that tool's own -- the original figure must be recoverable from the database
#: alone, with no git history -- answered at the grain of the thing that changed.
#:
#: ALL THREE ORIGINALS ARE STORED, not only the column that moved. An operator
#: undoing this by hand needs the row as it stood, and hot_available is nominally
#: derived from the other two -- so a record holding one figure would require the
#: reader to trust that the derivation held at the time, which is precisely what was
#: broken. The `now_*` columns are NULLable because one action does not write a row
#: back at all: a row DELETED has no "now".
#:
#: NOT IN SCHEMA, for PAYOUT_UNIQUE_INDEX_SQL's reason one line up: it is applied by
#: apply_migrations() and by the tool itself, so a tool run against a database the
#: app has not restarted against still has somewhere to write its audit row.
INVENTORY_CORRECTIONS_TABLE = "wallet_inventory_corrections"
INVENTORY_CORRECTIONS_SQL = f"""
CREATE TABLE IF NOT EXISTS {INVENTORY_CORRECTIONS_TABLE} (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    asset TEXT NOT NULL,
    action TEXT NOT NULL,
    was_hot_confirmed REAL NOT NULL,
    was_hot_reserved REAL NOT NULL,
    was_hot_available REAL NOT NULL,
    now_hot_confirmed REAL,
    now_hot_reserved REAL,
    now_hot_available REAL,
    justified_by TEXT NOT NULL,
    message TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_wallet_inventory_corrections_asset
    ON {INVENTORY_CORRECTIONS_TABLE}(asset);
"""


def duplicate_live_payouts(conn: sqlite3.Connection) -> list:
    """Swaps that already have more than one LIVE payout row.

    A non-empty result is the double-payout this index exists to prevent,
    already in the database and already on chain. It is read out rather than
    inferred, because "the index would have stopped it" says nothing about
    rows written before the index existed.
    """
    # THE STATUS LIST IS DERIVED FROM PAYOUT_LIVE_STATUSES, NOT SPELLED AGAIN.
    # Until 2026-10-06 this query read
    #
    #     "WHERE status IN ('created', 'broadcast', 'completed') "
    #
    # which is the SAME duplication the comment above PAYOUT_UNIQUE_INDEX_SQL is
    # about -- and it SURVIVED the 2026-10-04 commit that removed it, about a
    # hundred lines further down the same file. That is the specific way rule 8's
    # failure hides: the fix was real, the comment explaining it was real, and the
    # third copy was below the fold.
    #
    # It is worse here than at the index, because this function's whole job is to
    # check what that index enforces. A fourth live status added to the tuple would
    # move the constraint (the index derives) and NOT move this check (the string
    # did not), so the diagnostic that exists to find a double payout the index
    # missed would itself stop seeing the new kind. It would report zero and look
    # like good news.
    #
    # Bound as parameters rather than formatted, which the index could not do: a
    # partial index's WHERE clause is STORED text so it has to be interpolated,
    # while an ordinary SELECT takes placeholders. Same vocabulary, one spelling,
    # S608 DOES FIRE HERE and the noqa is a claim about what is interpolated, which
    # I checked after first writing that it would not fire and being wrong. What goes
    # into the f-string is a run of "?" characters whose LENGTH comes from a
    # module-level tuple; the statuses themselves are bound. That is narrower than
    # the identifier-interpolation case CLAUDE.md rule 12 sanctions -- not even a
    # table name reaches the text, only punctuation -- and a reader can see it on the
    # line above.
    placeholders = ", ".join("?" for _ in PAYOUT_LIVE_STATUSES)
    return conn.execute(
        "SELECT swap_id, COUNT(*) AS live_rows FROM payouts "  # noqa: S608
        f"WHERE status IN ({placeholders}) "
        "GROUP BY swap_id HAVING COUNT(*) > 1 ORDER BY swap_id",
        PAYOUT_LIVE_STATUSES,
    ).fetchall()


def add_column_if_missing(conn: sqlite3.Connection, table: str, column: str, declaration: str) -> bool:
    """ALTER TABLE ... ADD COLUMN, but only when the column is absent. Returns whether it added it.

    SQLite has no "ADD COLUMN IF NOT EXISTS", so the presence check is a read of
    PRAGMA table_info rather than a caught exception. Catching the error would
    work and is worse: "duplicate column name" and a genuinely broken ALTER
    arrive as the same OperationalError, so the handler could not tell an
    already-migrated database from a failed migration -- which is rule 12's
    BLE001 complaint in miniature, a broad catch whose caller cannot distinguish
    the failure from a real answer.

    Adding a column is the one schema change SQLite does cheaply and without
    rewriting the table, and a NULLable one cannot fail on existing rows. The
    identifiers are interpolated because parameters cannot bind an identifier;
    both are literals from this module, never input.
    """
    # `table`, `column` and `declaration` are interpolated because a SQL parameter
    # cannot bind an IDENTIFIER in SQLite. All three are literals from this
    # module's own migration code and no VALUE is interpolated anywhere here, so
    # there is nothing for a caller to inject. (S608 does not fire on these, so
    # there is no suppression to add -- noting it because the interpolation looks
    # like the thing that rule exists for.)
    # Read by NAME, not by position. `row[1]` is the column name for a plain
    # sqlite3 connection and for sqlite3.Row, and it raises KeyError on the dict
    # row factory this module actually installs -- which is exactly what happened
    # 2026-09-26: a standalone check with a plain connection passed, and the app's
    # own connection broke collection of the whole suite. A positional read of a
    # PRAGMA is a guess about the caller's row factory; a named one is not.
    rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
    existing = {(row["name"] if hasattr(row, "keys") else row[1]) for row in rows}
    if column in existing:
        return False
    conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {declaration}")
    conn.commit()
    logger.info("migration: added %s.%s (%s)", table, column, declaration)
    return True


#: The identity of ONE PAYMENT, as the two per-payment tables key it.
#:
#: `address` joined this tuple on 2026-10-10. db.py's SCHEMA carries the measurement at
#: deposit_events' own UNIQUE clause -- an ICP txid is a ledger BLOCK INDEX, which is
#: unique only within one ledger, and a row from a destroyed local replica owned the key a
#: live 2.44081155 ICP deposit needed.
PAYMENT_UNIQUE_KEY = ("asset", "txid", "vout", "address")

#: What the key WAS, kept as a value rather than as prose because the migration below has
#: to recognize it in an existing database. A database carrying this key is pre-2026-10-10
#: and is widened on the next start.
PAYMENT_UNIQUE_KEY_BEFORE = ("asset", "txid", "vout")

#: Both tables that record one payment per row. Named here rather than at the call site so
#: there is one list to read and the migration cannot cover one and miss the other -- which
#: is how late_deposits came to carry the same latent collision for six days.
PAYMENT_KEYED_TABLES = ("deposit_events", "late_deposits")


def unique_keys(conn: sqlite3.Connection, table: str) -> list[tuple[str, ...]]:
    """Every UNIQUE key on `table`, each as its tuple of column names, in index order.

    READS THE DATABASE RATHER THAN THE SCHEMA STRING, which is the whole point: the
    question a migration has to answer is what THIS file on disk actually has, not what
    this module would create today. A `CREATE TABLE IF NOT EXISTS` is a no-op against an
    existing table, so a database created last week carries last week's constraints and
    SCHEMA says nothing about it.

    A table-level `UNIQUE(...)` becomes an implicit index named `sqlite_autoindex_<table>_N`
    rather than a named one, so both are read and nothing filters on the name.

    Columns come back in INDEX order, not table order, and the tuple is compared as ordered
    -- `(asset, txid, vout)` and `(txid, asset, vout)` are the same constraint but this
    returns them differently. That is deliberate and harmless here, because both tuples
    this module compares against are spelled in the order SCHEMA declares them; a caller
    wanting set equality should say so.

    Reads PRAGMA rows BY NAME. add_column_if_missing() records what positional access cost
    on 2026-09-26: `row[1]` works for a plain connection and raises KeyError on the dict
    row factory connect_db() actually installs, so a standalone check passed while the
    app's own connection broke collection of the whole suite.
    """
    found: list[tuple[str, ...]] = []
    # Identifiers cannot be bound as SQL parameters in SQLite, and `table` is a literal
    # from PAYMENT_KEYED_TABLES in this module -- never a caller's string, never input.
    for index in conn.execute(f"PRAGMA index_list({table})").fetchall():
        name = index["name"] if hasattr(index, "keys") else index[1]
        unique = index["unique"] if hasattr(index, "keys") else index[2]
        if not unique:
            continue
        columns = conn.execute(f"PRAGMA index_info({name})").fetchall()
        found.append(tuple(
            (column["name"] if hasattr(column, "keys") else column[2]) for column in columns
        ))
    return found


#: The scratch name the rebuild creates and immediately renames away. Spelled once,
#: because rebuilt_create_sql() has to produce the same name _rebuild_widened() copies
#: into and renames -- two spellings of it is a rebuild that creates one table and copies
#: into another, which fails loudly but for a reason nobody would read correctly.
WIDENING_SUFFIX = "_widening"

#: WHAT unique_keys() SAYS ABOUT A TABLE, as the three answers the migration can act on.
#: A total over the possibilities, so a caller can assert it handled all of them rather
#: than discovering a fourth at runtime -- the construction stack_authority.py uses for
#: LISTENER_VERDICTS and services/kill_switch.py for _REMEDY_FOR.
WIDEN_ALREADY = "already-widened"
WIDEN_PROCEED = "proceed"
WIDEN_UNRECOGNIZED = "unrecognized"
WIDEN_VERDICTS = (WIDEN_ALREADY, WIDEN_PROCEED, WIDEN_UNRECOGNIZED)


def widening_verdict(keys) -> str:
    """Does this table need its payment key widened? PURE -- it reads a list of tuples.

    THE DECISION, extracted from widen_payment_unique_key() so it can be called with
    seeded inputs (rule 10). It was three `if` branches inside the rebuild, which is
    exactly the shape rule 12 describes: ruff's C901 on that function was pointing at a
    decision buried in orchestration, not at a line count, and raising the ceiling would
    have left it unreachable from a test.

    `WIDEN_UNRECOGNIZED` IS NOT AN ERROR AND IS NOT A GO-AHEAD. It is rule 2's
    distinction: a table carrying neither the old key nor the new one is a table this
    module does not know the shape of, and "I do not recognize it" is not "it needs
    rebuilding". The rebuild is the one operation in this module that can destroy rows,
    so an unfamiliar shape is left exactly as it is.

    Compares as ORDERED tuples, because both constants are spelled in the order SCHEMA
    declares them and unique_keys() returns index order. unique_keys()' own docstring says
    so; a caller wanting set equality has to ask for it.
    """
    present = [tuple(key) for key in keys]
    if tuple(PAYMENT_UNIQUE_KEY) in present:
        return WIDEN_ALREADY
    if tuple(PAYMENT_UNIQUE_KEY_BEFORE) in present:
        return WIDEN_PROCEED
    return WIDEN_UNRECOGNIZED


def sql_code_and_comment(line: str) -> tuple[str, str]:
    """Split one DDL line into its code and its trailing `-- comment`. PURE.

    WHY THIS EXISTS, AND IT WAS FOUND BY A TEST RATHER THAN BY THINKING. The rebuild below
    finds the old UNIQUE clause by substring and replaces it. SCHEMA's own comment block
    above deposit_events' key QUOTES the old clause -- "This was UNIQUE(asset, txid, vout)
    and that encodes an assumption" -- and sqlite_master stores a CREATE statement with its
    comments intact. So on a database whose DDL carries that sentence, the clause appears
    TWICE and rebuilt_create_sql() refused.

    It refused safely, which is the only reason this is a fragility and not an incident. But
    it refused for a reason no operator would diagnose, and it would have done so on any
    future database created from today's SCHEMA -- so the matching has to see code and not
    prose.

    Measured by tests/test_payment_key_widening.py, which reconstructs the previous schema
    from today's by reversing the one substitution: the reconstruction put the clause in both
    the comment and the constraint, and the rebuild declined. The real legacy database on the
    operator's host predates that comment entirely and would have worked -- which is exactly
    the kind of accident that is worth not relying on.

    NO STRING-LITERAL HANDLING, and that is a stated limit rather than an oversight: a `--`
    inside a quoted literal would be cut as a comment. None of the CREATE statements in
    SCHEMA contains a quoted literal at all, and the caller counts the result to prove the
    shape before substituting anything, so a DDL this is wrong about refuses rather than
    being rewritten incorrectly.
    """
    code, sep, comment = line.partition("--")
    return code, (sep + comment)


def replace_outside_comments(text: str, before: str, after: str) -> tuple[str, int]:
    """`text` with `before` replaced by `after`, ignoring anything after a `--`. PURE.

    Returns the rewritten text and HOW MANY replacements happened, so the caller asserts the
    count rather than hoping -- which is the whole reason this returns a pair instead of a
    string (rule 17: a reason to believe is not a measurement).

    Comments are kept verbatim in the output. Dropping them would be the easier
    implementation and would throw away the documentation sqlite_master carries, leaving a
    rebuilt table whose DDL no longer explains its own constraint.
    """
    lines = []
    replaced = 0
    for line in text.splitlines():
        code, comment = sql_code_and_comment(line)
        replaced += code.count(before)
        lines.append(code.replace(before, after) + comment)
    return "\n".join(lines), replaced


def rebuilt_create_sql(create: str, table: str, rebuilt: str) -> str:
    """The old CREATE statement with the key widened and the table renamed. PURE.

    THE REBUILT TABLE IS THE OLD TABLE WITH ONE CLAUSE CHANGED, not a second copy of the
    DDL kept in step by hand. A copy here would be rule 8's duplicate with a delay on it,
    and the delay would expire the next time a column was added to SCHEMA -- the rebuild
    would then silently drop it, which on these tables means dropping a column of a record
    of money.

    So the statement is read out of sqlite_master and substituted, which carries every
    column, type, default, comment and foreign key across verbatim.

    RAISES RATHER THAN RETURNING SOMETHING APPROXIMATE. Two guards, and each is a shape
    that must not be rebuilt from a guess:

      the UNIQUE clause is not there exactly once   the DDL is not what this module wrote,
                                                    so there is nothing to substitute.
                                                    COUNTED AS CODE, not as text: SCHEMA's
                                                    own comment quotes the old clause, and
                                                    matching prose made this refuse on a
                                                    database it should have rebuilt. See
                                                    sql_code_and_comment().
      the table name is not where it should be      `CREATE TABLE ... <table> (` is the
                                                    only form SCHEMA emits, and renaming by
                                                    a looser match could rewrite a column
                                                    name or a word inside a comment

    Pure so both guards are testable without a database (rule 10).
    """
    before = f"UNIQUE({', '.join(PAYMENT_UNIQUE_KEY_BEFORE)})"
    after = f"UNIQUE({', '.join(PAYMENT_UNIQUE_KEY)})"
    widened, replaced = replace_outside_comments(create, before, after)
    if replaced != 1:
        raise ValueError(
            f"the CREATE statement for {table} contains {before!r} as CODE {replaced} time(s), not "
            f"once, so the clause to replace could not be identified. Nothing was rebuilt. "
            f"(Occurrences inside `--` comments are ignored; see sql_code_and_comment().)"
        )
    renamed, named_count = replace_outside_comments(widened, f" {table} (", f" {rebuilt} (")
    if named_count != 1:
        raise ValueError(
            f"the CREATE statement for {table} names the table as code {named_count} time(s), not "
            f"once, so it could not be renamed without risking a substitution somewhere else in "
            f"the DDL. Nothing was rebuilt."
        )
    return renamed


def foreign_keys_enforced(conn: sqlite3.Connection) -> int:
    """`PRAGMA foreign_keys` for this connection, read by name. 1 or 0.

    A function rather than two lines at each of the three places that need it, and read by
    NAME for the reason add_column_if_missing() records: a positional read of a PRAGMA is a
    guess about the caller's row factory, and that guess broke collection of the whole
    suite on 2026-09-26.
    """
    row = conn.execute("PRAGMA foreign_keys").fetchone()
    if not row:
        return 0
    return int(row["foreign_keys"] if hasattr(row, "keys") else row[0])


def _table_columns(conn: sqlite3.Connection, table: str) -> list[str]:
    """This table's column names, in table order. `table` is a literal from this module."""
    return [
        (column["name"] if hasattr(column, "keys") else column[1])
        for column in conn.execute(f"PRAGMA table_info({table})").fetchall()
    ]


def _named_indexes(conn: sqlite3.Connection, table: str) -> list[str]:
    """The CREATE statements for this table's NAMED indexes.

    Read from sqlite_master rather than listed here, for the same reason the table DDL is:
    DROP TABLE takes its indexes with it, and a list spelled in this module would silently
    lose any index added later. `sql IS NOT NULL` excludes the implicit autoindexes, which
    the rebuilt table's own UNIQUE clause recreates.
    """
    return [
        (row["sql"] if hasattr(row, "keys") else row[0])
        for row in conn.execute(
            "SELECT sql FROM sqlite_master WHERE type = 'index' AND tbl_name = ? AND sql IS NOT NULL",
            (table,),
        ).fetchall()
    ]


def _row_count(conn: sqlite3.Connection, table: str) -> int:
    """COUNT(*), with `table` interpolated because a parameter cannot bind an identifier."""
    row = conn.execute(f"SELECT COUNT(*) AS n FROM {table}").fetchone()  # noqa: S608 -- `table` is a literal from PAYMENT_KEYED_TABLES in this module; no value is interpolated anywhere in this statement
    return int(row["n"] if hasattr(row, "keys") else row[0])


def restore_foreign_keys(conn: sqlite3.Connection, was_enforced: int) -> None:
    """Put `PRAGMA foreign_keys` back to what the caller had. A no-op when it was off.

    Three paths through the rebuild below need this -- a refused substitution, a rolled-back
    transaction, and success -- and spelling it three times was three chances to forget one.
    Rule 8: the copies agree on the day they are written.

    It RESTORES rather than setting ON, because both states are legitimate on a real
    connection: db.py's SCHEMA turns enforcement on and a bare connect_db() leaves it off,
    which services/xrp_tag_service.py's own operator message already says out loud. Which
    one a connection runs with is not this module's to decide.
    """
    if was_enforced:
        conn.execute("PRAGMA foreign_keys = ON")


def _rebuild_widened(conn: sqlite3.Connection, table: str, ddl: str) -> int:
    """Create-copy-drop-rename, in ONE transaction. Raises, having rolled back, on anything.

    SQLite's documented procedure for changing a table constraint, which is the only way:
    it can add and drop a COLUMN and cannot drop a table-level UNIQUE.

    THE ROW COUNT IS CHECKED INSIDE THE TRANSACTION, so a copy that lost a row rolls the
    whole thing back rather than committing a shorter table. Losing a row here is destroying
    the record of a payment, which rule 7 forbids outright -- and a create-copy-drop-rename
    is exactly the shape that can do it quietly.

    Extracted from widen_payment_unique_key() because ruff's C901 on that function was
    pointing at orchestration that had swallowed this, not at a line count (rule 12).

    IT TAKES THREE ARGUMENTS AND DERIVES THE REST, which is the second thing ruff was
    right about: the first version took seven, five of which were values the caller had
    computed from `conn` and `table` and handed over. A parameter that only ever carries
    one derivation of its neighbors is a chance for the caller to pass a mismatched pair --
    a column list from one table with another table's name is a rebuild that copies the
    wrong rows. `ddl` stays a parameter because the caller has to be able to REFUSE on a
    DDL it cannot rewrite, before any of this runs.

    THE ROW COUNT IS READ INSIDE THE TRANSACTION, which is also better than taking it: the
    before and after counts then come from one consistent snapshot rather than from either
    side of the BEGIN. Returns the count carried, so the caller can log a measured figure.
    """
    rebuilt = table + WIDENING_SUFFIX
    columns = ", ".join(_table_columns(conn, table))
    index_sql = _named_indexes(conn, table)
    conn.execute("BEGIN IMMEDIATE")
    try:
        expected = _row_count(conn, table)
        conn.execute(ddl)
        conn.execute(f"INSERT INTO {rebuilt} ({columns}) SELECT {columns} FROM {table}")  # noqa: S608 -- identifiers only, from this module and from PRAGMA table_info; no value is interpolated
        conn.execute(f"DROP TABLE {table}")
        conn.execute(f"ALTER TABLE {rebuilt} RENAME TO {table}")
        for statement in index_sql:
            conn.execute(statement)
        carried = _row_count(conn, table)
        if carried != expected:
            raise RuntimeError(
                f"the rebuild of {table} carried {carried} row(s) out of {expected}. Rolling back: "
                f"losing a row here is destroying the record of a payment."
            )
    except Exception:
        conn.rollback()
        logger.exception(
            "migration: %s was NOT rebuilt and has been rolled back to exactly its previous "
            "state  <- no row was deleted and no constraint was changed.", table,
        )
        raise
    conn.commit()
    return carried


def _verify_widened(conn: sqlite3.Connection, table: str) -> None:
    """Prove the rebuild did what it claimed. Raises otherwise. Both checks are reads.

    THE OUTCOME IS MEASURED, NOT INFERRED FROM THE DDL HAVING RUN (rule 17), and the two
    questions are different:

      foreign_key_check   enforcement was OFF for the rebuild, so this is the check that it
                          was safe to turn off. Nothing in SCHEMA references these two
                          tables -- they are the child side, with a FOREIGN KEY to swaps(id)
                          -- so this is expected to be empty, which is why it is worth
                          asserting rather than assuming.
      the unique key      read back from PRAGMA. A CREATE that ran without producing the
                          constraint would otherwise leave the migration reporting success.

    Neither deletes a row to make itself pass.
    """
    orphans = conn.execute("PRAGMA foreign_key_check").fetchall()
    if orphans:
        raise RuntimeError(
            f"{table} was rebuilt and PRAGMA foreign_key_check now reports {len(orphans)} "
            f"violation(s). The rebuild is committed; the violations are named by the pragma and "
            f"nothing here deletes a row to clear them."
        )
    keys = [tuple(key) for key in unique_keys(conn, table)]
    if tuple(PAYMENT_UNIQUE_KEY) not in keys:
        raise RuntimeError(
            f"{table} was rebuilt and its unique keys read back as {keys}, which does not include "
            f"{PAYMENT_UNIQUE_KEY}. The DDL ran and did not produce the constraint."
        )


def widen_payment_unique_key(conn: sqlite3.Connection, table: str) -> bool:
    """Rebuild `table` so its payment key includes `address`. Returns whether it did.

    IDEMPOTENT AND A NO-OP ON A CURRENT DATABASE: it rebuilds only when the old key is
    present, so a second run reads two PRAGMAs and returns False.

    WHY A REBUILD. SQLite can ADD a column and can DROP one, and cannot drop a table-level
    constraint -- so changing `UNIQUE(asset, txid, vout)` means the documented
    create-copy-drop-rename, which is more machinery than any other migration in this
    module. It is justified by what the narrow key did on 2026-10-10: it silently handed a
    live deposit's row to a swap that had completed three days earlier. SCHEMA's comment on
    deposit_events carries that measurement in full.

    IT CANNOT FAIL AGAINST EXISTING ROWS, and that is a property of the change rather than
    a hope. The new key is strictly WEAKER: any set of rows satisfying (asset, txid, vout)
    satisfies (asset, txid, vout, address) too. So there is no analogue of
    duplicate_live_payouts()' pre-check here -- there is no data that could refuse this
    index -- and nothing is deleted to make it succeed.

    THE NEW TABLE IS THE OLD TABLE WITH ONE CLAUSE CHANGED, not a DDL copy kept in step by
    hand. The old CREATE statement is read out of sqlite_master and the exact clause is
    substituted, so every column, type, default, comment and foreign key is carried over
    verbatim and a column this module has forgotten about cannot be dropped by the rebuild.
    A second copy of the DDL here would be rule 8's duplicate with a delay on it -- and the
    delay would expire the next time a column was added.

    THREE THINGS ARE PROVEN RATHER THAN ASSUMED, inside the transaction, and any of them
    failing rolls the whole rebuild back and leaves the original table untouched:

      the substitution hit exactly once   otherwise the DDL was not the shape expected and
                                          nothing should be rebuilt from a guess
      the row count is unchanged          a copy that lost a row would be destroying the
                                          record of money (rule 7)
      the new key is actually there       read back from PRAGMA, so the outcome is measured
                                          rather than inferred from the DDL having run

    FOREIGN KEY ENFORCEMENT IS TURNED OFF FOR THE REBUILD AND PUT BACK, which is step 1 and
    step 11 of SQLite's own documented procedure for changing a constraint. THIS PARAGRAPH
    USED TO SAY THE OPPOSITE -- that sqlite3 leaves `PRAGMA foreign_keys` OFF, that
    connect_db() does not set it, and that the rebuild therefore refuses if it is ON -- and
    every clause of that was true except the one that mattered: **db.py's own SCHEMA begins
    with `PRAGMA foreign_keys=ON;` (line 68)**, and the real startup path runs
    executescript(SCHEMA) before apply_migrations() on the same connection. So enforcement
    is ON for every connection this migration actually gets, the refusal would have fired
    in production, and the widening would never have happened -- with a log line explaining
    why, which nobody reads on a worker that started fine.

    Caught by tests/test_payment_key_widening.py on the first run rather than by reading,
    which is the whole of rule 17: the claim was plausible, the measurement was cheap, and
    the two disagreed.

    THE PRAGMA IS A NO-OP INSIDE A TRANSACTION, so it is set BEFORE the BEGIN and READ BACK
    rather than trusted -- a transaction already open would otherwise leave the rebuild
    running with enforcement live, which is the one case the old refusal was right about.
    If it cannot be turned off, this refuses and changes nothing.

    AND NOTHING REFERENCES THESE TABLES ANYWAY, which is why the window is safe rather than
    merely brief: deposit_events and late_deposits are the CHILD side -- each has a
    FOREIGN KEY to swaps(id) and no table in SCHEMA references either of them -- so the DROP
    has no dependents to orphan. That is asserted rather than assumed too:
    `PRAGMA foreign_key_check` runs after the rebuild and a violation raises.
    """
    verdict = widening_verdict(unique_keys(conn, table))
    if verdict == WIDEN_ALREADY:
        return False
    if verdict == WIDEN_UNRECOGNIZED:
        logger.warning(
            "migration: %s has neither the old payment key %s nor the new one %s, so it was "
            "LEFT ALONE  <- nothing was rebuilt and nothing was deleted; its unique keys are "
            "%s. A table this module does not recognize is not a table to rebuild from a guess.",
            table, PAYMENT_UNIQUE_KEY_BEFORE, PAYMENT_UNIQUE_KEY, unique_keys(conn, table),
        )
        return False

    # SAVED SO IT CAN BE PUT BACK EXACTLY. See restore_foreign_keys() for why this restores
    # rather than setting ON.
    was_enforced = foreign_keys_enforced(conn)
    conn.execute("PRAGMA foreign_keys = OFF")
    if foreign_keys_enforced(conn):
        logger.error(
            "migration: %s NOT rebuilt -- `PRAGMA foreign_keys = OFF` did not take, which means a "
            "transaction is already open on this connection (the pragma is a no-op inside one). "
            "Rebuilding with enforcement live is the one case that can orphan rows, so this "
            "refuses. Nothing was changed.",
            table,
        )
        return False

    row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = ?", (table,)
    ).fetchone()
    create = (row["sql"] if hasattr(row, "keys") else row[0]) if row else ""
    try:
        ddl = rebuilt_create_sql(create, table, table + WIDENING_SUFFIX)
    except ValueError:
        restore_foreign_keys(conn, was_enforced)
        logger.exception("migration: %s NOT rebuilt and nothing was changed", table)
        return False

    try:
        carried = _rebuild_widened(conn, table, ddl)
    finally:
        # BEFORE _verify_widened(), so foreign_key_check runs under the enforcement the
        # caller had rather than under the pragma this function turned off -- and in a
        # `finally` so a rolled-back rebuild cannot leave enforcement off either.
        restore_foreign_keys(conn, was_enforced)

    _verify_widened(conn, table)
    logger.info(
        "migration: %s rebuilt with UNIQUE%s, %d row(s) carried  <- was UNIQUE%s, which on ICP "
        "let a block index from a destroyed ledger own a live deposit's row",
        table, PAYMENT_UNIQUE_KEY, carried, PAYMENT_UNIQUE_KEY_BEFORE,
    )
    return True


def apply_migrations(conn: sqlite3.Connection) -> dict:
    """Bring an EXISTING database up to the current constraints. Idempotent.

    Safe to run against a database with rows in it, and safe to run repeatedly:
    the index is IF NOT EXISTS, and the pre-check is a read.

    What it will NOT do is destroy evidence to make itself succeed. If a swap
    already has two live payout rows, the index cannot be created -- SQLite
    refuses to build a unique index over data that violates it -- and the
    honest outcome is to say so, name the swap_ids, and leave the rows alone.
    Deleting one of them to get the index built would be deleting the record of
    a payment that may be on chain.

    Returns a dict the caller can print or assert on:
        {"index_created": bool, "duplicates": [{"swap_id":…, "live_rows":…}, …]}
    """
    # ADD COLUMN first, and before the payout-index work, because an early return
    # below (a swap with two live payout rows) must not skip it: the column is
    # what create_swap() writes on every XRP swap, and a database that has the
    # constraint but not the column fails at swap creation rather than at start.
    added_deposit_tag = add_column_if_missing(conn, "swaps", "deposit_tag", "INTEGER")

    # THE PAYMENT KEY GOES UP BEFORE THE INDEX WORK, for the same reason the ADD COLUMN
    # does: the early return below must not skip it. Without it, a deposit whose txid
    # collides with one from a destroyed ICP ledger is credited to the WRONG SWAP and the
    # live one never gets a row -- measured on the operator's host 2026-10-10 and recorded
    # in full at deposit_events' UNIQUE clause in SCHEMA above.
    widened = [table for table in PAYMENT_KEYED_TABLES if widen_payment_unique_key(conn, table)]

    # THE CORRECTIONS TABLE GOES UP BEFORE THE INDEX WORK, for the same reason the
    # ADD COLUMN does: the early return below must not skip it. It is where a
    # repair to a money column records the figure it replaced, and a database with
    # the repair tool available but nowhere for its audit row to land is a database
    # where the repair has to either refuse or overwrite silently. IF NOT EXISTS
    # throughout, so this is idempotent and cannot fail on an existing database.
    conn.executescript(INVENTORY_CORRECTIONS_SQL)
    conn.commit()

    duplicates = duplicate_live_payouts(conn)
    if duplicates:
        rows = ", ".join(f"{row['swap_id']}={row['live_rows']}" for row in duplicates)
        logger.error(
            "%s NOT created: %d swap(s) already have more than one live payout row (%s)  <- each of those is a "
            "payout that was made twice; reconcile them on chain before this constraint can be applied. Nothing "
            "has been deleted.",
            PAYOUT_UNIQUE_INDEX_NAME,
            len(duplicates),
            rows,
        )
        return {"index_created": False, "duplicates": duplicates, "deposit_tag_added": added_deposit_tag,
                "payment_keys_widened": widened}
    conn.execute(PAYOUT_UNIQUE_INDEX_SQL)
    conn.commit()
    return {"index_created": True, "duplicates": [], "deposit_tag_added": added_deposit_tag,
            "payment_keys_widened": widened}


def dict_factory(cursor, row):
    return {col[0]: row[idx] for idx, col in enumerate(cursor.description)}


class DatabaseNotFound(FileNotFoundError):
    """The path names no database, and this connection will not invent one.

    ITS OWN TYPE because the remedy is specific and is never "handle the error": a
    caller that reaches this has been given a path to a database that does not exist,
    and the fix is the path, not a retry or a fallback.
    """


def connect_db(db_path: str, *, create: bool = False) -> sqlite3.Connection:
    """Open `db_path`. REFUSES a missing file unless `create` is asked for.

    =========================================================================
    THIS FUNCTION MADE A SECOND DATABASE OUT OF A TYPO, AND THE TYPO WAS MINE
    =========================================================================

    It was `sqlite3.connect(db_path)` with nothing else, and sqlite3.connect CREATES a
    missing file. workers/common.database_census() records what that cost on
    2026-10-01:

        Three workers were started from a shell whose SWAP_DB_PATH pointed at the wrong
        file -- repo_root/runtime/swap_terminal.db instead of
        repo_root/swap_terminal/swap_terminal.db, A PATH I PUT IN A BLOCK I HANDED
        THEM. They then ran for an hour printing

            deposit_watcher cycle=57 IDLE  active_swaps=0 refreshed=0

        while THREE swaps sat in awaiting_deposit in the database every root tool
        reads. Nothing was wrong with any worker. Nothing failed.

    And on 2026-10-10 the operator found the file still there, nine days later, holding
    one failed SOL swap with a real deposit_events row and five audit rows:

        327,680 bytes   52 swaps   newest 2026-10-10T21:57   the authority
        118,784 bytes    1 swap    newest 2026-10-01T22:34   the orphan

    Their words: "is that fucking bifurcated database fixed as well? or did you just
    gloss over that huge BFD". absorb_db.py merges the orphan, and a merge tool is
    CLEANUP. Rule 19's test is "does it stop the symptom being reported, or stop the
    cause existing? Only the second is a fix." THIS is the second.

    =========================================================================
    WHY REFUSING IS THE RIGHT DEFAULT AND CREATING IS THE EXCEPTION
    =========================================================================

    A missing database has exactly two meanings and they want opposite answers:

      first boot        nothing exists yet and the schema is about to be written.
                        init_db() asks for that explicitly, once, and it is the only
                        place in the serving path that does.
      a wrong path      every other time. SWAP_DB_PATH mistyped, a tool run from the
                        wrong directory, a container started without its volume. There
                        is no case where the right answer is an empty database.

    Creating by default makes the first meaning free and the second SILENT, and the
    second is the one that happens. CLAUDE.md rule 14's whole subject is that a
    terminal pointed at an empty file is indistinguishable from a quiet one -- and the
    quiet one is normal, so the broken one reads as normal.

    THE MESSAGE NAMES THE VARIABLE AND THE TWO USUAL SUSPECTS, because the operator
    reading it is holding a path they believe is right. "No such file" would send them
    to look for a missing database; what is actually wrong is the path.

    KEYWORD-ONLY, so a positional second argument cannot become a silent create: this
    took a single string for its whole life and every existing call still reads the
    same.
    """
    if not create and not Path(db_path).exists():
        raise DatabaseNotFound(
            f"no database at {db_path!r}, and this connection will not create one. Until "
            f"2026-10-10 it did, which is how a mistyped SWAP_DB_PATH became a SECOND "
            f"database that three workers polled for an hour while the real swaps sat "
            f"elsewhere (workers/common.database_census() has the measurement).\n"
            f"  check  SWAP_DB_DIR in .env -- config.database_path() reads it and joins "
            f"swap_terminal.db onto it, and docker compose mounts the same directory at /data. "
            f"It is the one place that moves BOTH the host and the container\n"
            f"  check  SWAP_DB_PATH in the process environment, which OVERRIDES .env -- it must "
            f"name the FILE, not the directory\n"
            f"  check  the working directory, if the path is relative\n"
            f"  check  that a container was started with its /data volume\n"
            f"Only db.init_db() may create a database, and it says so by passing create=True."
        )
    conn = sqlite3.connect(db_path)
    conn.row_factory = dict_factory
    return conn


def get_db() -> sqlite3.Connection:
    if current_app is None or g is None:
        raise RuntimeError("Flask is required for request-scoped database access")
    if "db" not in g:
        g.db = connect_db(current_app.config["DB_PATH"])
    return g.db


def close_db(_=None) -> None:
    if g is None:
        return
    db = g.pop("db", None)
    if db is not None:
        db.close()


def init_db(db_path=None) -> None:
    """Create the database if it is absent, then bring it up to the current schema.

    THE ONLY PLACE IN THIS TREE THAT MAY CREATE ONE, and it opens its OWN connection
    with create=True rather than going through get_db(). That is the whole of the
    2026-10-10 change: get_db() serves every HTTP request, and a request arriving when
    the database has vanished must fail loudly rather than quietly manufacture an empty
    one -- which is exactly what it used to do.

    `db_path` IS THE CONTAINER'S DOOR IN, and it exists because of a startup order this
    change would otherwise have broken. docker/web_workers_entrypoint.main() calls
    start_workers() BEFORE it execs gunicorn, and gunicorn is what imports wsgi -> app ->
    create_app() -> init_db(). So on a fresh volume the three workers reached the
    database FIRST, and what used to happen is that whichever of them got there first
    created the file and applied SCHEMA itself (payout_worker.py:133 still runs
    executescript(SCHEMA) before its loop, for exactly that reason).

    With connect_db() refusing, that worker would instead die at startup on a database
    nobody had created yet -- a loud failure, but a WRONG one: the path was right, the
    volume was mounted, and the only thing missing was a file the container itself owns.
    So the entrypoint now calls this with an explicit path before start_workers(), and
    creation happens once, in one place, announced, before anything polls.

    Taking a path rather than duplicating four lines into the entrypoint is rule 8: two
    copies of "create and migrate" would agree on the day they were written. There is
    one copy and the caller says which database.

    WITHOUT AN ARGUMENT it reads current_app, which is the Flask path and unchanged.

    The connection is closed rather than cached in `g`, because this runs at app
    startup where there is no request context to cache into, and the next get_db() opens
    the file this just created.
    """
    path = str(current_app.config["DB_PATH"]) if db_path is None else str(db_path)
    db = connect_db(path, create=True)
    try:
        db.executescript(SCHEMA)
        db.commit()
        apply_migrations(db)
    finally:
        db.close()


@contextmanager
def db_session(db_path: str, *, create: bool = False):
    """A transaction on `db_path`, committed on success and rolled back on a raise.

    `create` IS PASSED THROUGH AND DEFAULTS TO REFUSING, for connect_db()'s reason: a
    root tool handed a wrong path must fail rather than open an empty database and
    report that nothing is pending.
    """
    conn = connect_db(db_path, create=create)
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
