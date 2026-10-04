"""SQLite schema and connection handling for the swap terminal.

Role: submodule (persistence; holds no decision of its own)
Reads: swap_terminal.db
Writes: swap_terminal.db -- creates quotes, swaps, deposit_events,
       unattributable_deposits, late_deposits, payouts, wallet_inventory,
       market_context, swap_audit_log, xrp_destination_tags
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
    UNIQUE(asset, txid, vout),
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
    UNIQUE(asset, txid, vout),
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
-- because that function calls adapter.get_balance() itself. The readers are
-- services/admin_view.py's display query, this file's reserve bookkeeping, and
-- rescue_payout.py's release. wallet_custody.py is the tool that says which wallet
-- an endpoint actually serves.
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
PAYOUT_LIVE_STATUSES = ("created", "broadcast", "completed")
PAYOUT_UNIQUE_INDEX_SQL = (
    f"CREATE UNIQUE INDEX IF NOT EXISTS {PAYOUT_UNIQUE_INDEX_NAME} "
    "ON payouts(swap_id) WHERE status IN ('created', 'broadcast', 'completed')"
)


def duplicate_live_payouts(conn: sqlite3.Connection) -> list:
    """Swaps that already have more than one LIVE payout row.

    A non-empty result is the double-payout this index exists to prevent,
    already in the database and already on chain. It is read out rather than
    inferred, because "the index would have stopped it" says nothing about
    rows written before the index existed.
    """
    return conn.execute(
        "SELECT swap_id, COUNT(*) AS live_rows FROM payouts "
        "WHERE status IN ('created', 'broadcast', 'completed') "
        "GROUP BY swap_id HAVING COUNT(*) > 1 ORDER BY swap_id"
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
        return {"index_created": False, "duplicates": duplicates, "deposit_tag_added": added_deposit_tag}
    conn.execute(PAYOUT_UNIQUE_INDEX_SQL)
    conn.commit()
    return {"index_created": True, "duplicates": [], "deposit_tag_added": added_deposit_tag}


def dict_factory(cursor, row):
    return {col[0]: row[idx] for idx, col in enumerate(cursor.description)}


def connect_db(db_path: str) -> sqlite3.Connection:
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


def init_db() -> None:
    db = get_db()
    db.executescript(SCHEMA)
    db.commit()
    apply_migrations(db)


@contextmanager
def db_session(db_path: str):
    conn = connect_db(db_path)
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
