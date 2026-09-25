"""The Solana adapter: an account-model chain behind the same five-method contract.

Role: module (owns the Solana stage of the adapter contract; every decision it
      makes is a function in chains/solana_address.py or chains/solana_units.py)
Reads: a Solana JSON-RPC endpoint -- getBalance, getSignaturesForAddress,
       getTransaction, getSignatureStatuses, getSlot, getAccountInfo,
       getTokenAccountBalance, getMinimumBalanceForRentExemption, getHealth
Writes: nothing. No file, no database, no chain.
Can move funds: NO, AND THAT IS THE DELIBERATE HALF OF THIS FILE. Both
       fund-moving methods of the contract -- get_new_address() and
       send_to_address() -- REFUSE, with a message naming the decision the
       operator has to make first. Nothing here loads, reads, derives or holds
       a keypair; there is no code path in this module that can sign.
Mainnet-safe: yes, entirely. Every implemented method is a read.

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

This adapter uses **the account's index in the transaction's account key
list**. It is read from the transaction, it is stable for a given (signature,
address) pair, and a transaction credits an account exactly once -- the
account model has one net balance delta per account per transaction, so one
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
    SIGNS AND BROADCASTS, from       CANNOT SIGN. No keypair is read, loaded
    SOLANA_PAYER_KEYPAIR_PATH        or referenced anywhere in this module.
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
  REFUSES, BY DESIGN         get_new_address  -- the deposit-address strategy
                             is fund movement and is the operator's choice
                             between three options with different custody
                             consequences. See the method.
                             send_to_address -- signing and broadcasting.
                             build_transfer_plan() builds and DESCRIBES the
                             transfer for inspection; it does not sign, and
                             this module holds no key to sign with.

**NOTHING IN THIS FILE HAS BEEN EXERCISED AGAINST A SOLANA CLUSTER.** Measured
2026-09-25: api.devnet.solana.com and release.anza.xyz both return 403 from
this environment's proxy, and `solana-test-validator` cannot be installed
because its only distribution channels are those two hosts. Every RPC method
name, parameter shape and response field below was written from Solana's JSON-
RPC documentation and from the shapes server.js already relies on. The pure
functions are tested; the RPC plumbing is tested against seeded responses. The
proof that it talks to a real cluster is the operator's run, and
`solana_chain_check.py` at the repository root is written to be exactly that
run: read-only, one pasteable block, every step announced before it runs, and
a non-zero exit when any step's response does not have the shape this file
expects.
"""

from __future__ import annotations

import json

import requests

from .solana_address import (
    TOKEN_PROGRAM_ID,
    SolanaAddressError,
    associated_token_address,
    describe_address,
    is_on_curve,
    is_valid_address,
)
from .solana_units import (
    BALANCE_COMMITMENT,
    DISCOVERY_COMMITMENT,
    FINALIZED_RANK,
    SOL_DECIMALS,
    TOKEN_ACCOUNT_SPACE,
    amount_to_base_units,
    base_units_to_amount,
    commitment_rank,
    describe_commitment,
    float_is_exact_for,
    transfer_fee_lamports,
    validate_min_commitment_rank,
)


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
    """


# How many signatures to ask for in one getSignaturesForAddress page. Solana's
# own cap is 1000. 500 matches chains/base.py's listtransactions default so the
# two adapters agree about how far back "recent" reaches.
DEFAULT_SIGNATURE_LIMIT = 500


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


def deposit_event(signature: str, account_index: int, address: str, amount: float, rank: int) -> dict:
    """One deposit event, in the five keys services/deposit_service.py reads.

    A module-level function rather than a method because it is a pure mapping
    from five values to a dict and holds no adapter state -- CLAUDE.md rule
    10's bottom layer, where a test can call it directly.

    `vout` is the ACCOUNT INDEX: read from the transaction, stable for a given
    (signature, address) pair, and never invented. See this module's header for
    why that distinction is load-bearing -- chains/base.py's fabricated vout=0
    is the artifact migrate_deposit_vouts.py exists to clean up, and nothing
    here reproduces it.

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
        for label, value in (("SOL_SPL_MINT", self.mint), ("SOL_HOT_WALLET", self.hot_wallet)):
            if value and not is_valid_address(value):
                raise SolanaAddressError(f"{label}={value!r} is not a valid Solana address")

    # --- plumbing ------------------------------------------------------------

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
            raise SolanaRPCError(f"{method} returned HTTP {response.status_code} from {self.url}: {response.text[:300]}")
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
            result = self.call("getBalance", self.hot_wallet, {"commitment": BALANCE_COMMITMENT})
            lamports = int(result["value"] if isinstance(result, dict) else result)
            return base_units_to_amount(lamports, SOL_DECIMALS)
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

    def find_deposits_to_address(self, address: str, tx_limit: int = DEFAULT_SIGNATURE_LIMIT) -> list[dict]:
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
        signatures = self.call(
            "getSignaturesForAddress",
            address,
            {"limit": int(tx_limit), "commitment": DISCOVERY_COMMITMENT},
        ) or []
        events = []
        for entry in signatures:
            if entry.get("err") is not None:
                # A failed transaction moved nothing. See the docstring.
                continue
            signature = entry.get("signature")
            if not signature:
                continue
            rank = commitment_rank(entry.get("confirmationStatus"))
            events.extend(self._credits_in_transaction(signature, address, rank))
        deduped = {(event["txid"], event["vout"]): event for event in events}
        return list(deduped.values())

    def _credits_in_transaction(self, signature: str, address: str, rank: int) -> list[dict]:
        """The credits `address` received in one transaction, as event dicts.

        Split out from find_deposits_to_address so the per-transaction decision
        can be called with a seeded response (CLAUDE.md rule 10) instead of
        needing a cluster and a signature list to reach it.
        """
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
        if self.is_spl:
            return self._spl_credits(signature, address, meta, rank)
        return self._native_credits(signature, address, transaction, meta, rank)

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

    # --- the contract: the two that refuse -----------------------------------

    def get_new_address(self, label: str) -> str:
        """REFUSES. The deposit-address strategy is the operator's to choose.

        Bitcoin, Litecoin and Gridcoin answer this with `getnewaddress`: the
        DAEMON derives a key, stores it in wallet.dat, and the application
        never holds a secret. **Solana has no equivalent.** There is no wallet
        daemon, no keystore, and nothing on the other end of an RPC that can
        mint an address and remember how to spend from it.

        So the three ways to produce a Solana deposit address differ in WHO
        HOLDS WHAT, and that is a custody decision rather than an
        implementation detail. CLAUDE.md rule 16 puts address derivation
        explicitly on the operator's side of the line, and rule 20's "do not
        ask which" does not reach it: that rule's own exception is fund
        movement, and this is fund movement.

        README.md carries the three options, their trade-offs and a
        recommendation. This method refuses until one is chosen and pays that
        refusal back in the error message, so an operator who hits it at
        runtime does not have to go and find the document.
        """
        raise NotImplementedError(
            f"cannot derive a Solana deposit address for {label!r}: no strategy has been chosen.\n"
            "  Solana has no `getnewaddress` -- there is no wallet daemon to hold the key, so the application\n"
            "  must hold something, and WHICH something is a custody decision (CLAUDE.md rule 16).\n"
            "  The three options, and what each one puts at risk:\n"
            "    fresh keypair per swap  -- a stored secret for EVERY open swap. Largest secret surface.\n"
            "    one account + memo      -- no new secrets; a misattributed deposit pays the wrong person.\n"
            "    derivation from a seed  -- one secret, many addresses; the seed is total loss if it leaks.\n"
            "  See README.md, 'Solana deposit addresses'. The recommendation there is the memo strategy.\n"
            "  Until the operator chooses, SOL is a payout-side asset only and this refusal is the guard."
        )

    def send_to_address(self, address: str, amount: float) -> str:
        """REFUSES. Signing and broadcasting are the operator's (CLAUDE.md rule 16).

        THIS MODULE CANNOT SIGN. It holds no keypair, reads no keypair path,
        and imports nothing that could -- so this is not a policy that a flag
        turns off, it is an absence. The refusal is the honest report of that
        absence rather than a gate over a working implementation.

        build_transfer_plan() below is the half that IS built: it resolves the
        destination (including the associated token account and whether it
        exists), computes the base-unit amount at the right decimals, prices
        the fee and any rent the operator would be paying, and returns all of
        it for inspection. Everything up to the signature.
        """
        plan = self.build_transfer_plan(address, amount)
        raise NotImplementedError(
            "this adapter cannot sign or broadcast a Solana transfer, and holds no key that could.\n"
            f"{plan['description']}\n"
            "  Signing and broadcasting are the operator's (CLAUDE.md rule 16: fund movement comes back).\n"
            "  build_transfer_plan() produced everything above without a key; the signature is the missing step."
        )

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
        """True if the account is already on chain, so no rent is owed to create it.

        This is the read that turns "somebody pays rent" from a hypothetical
        into a number an operator can decide about.
        """
        result = self.call("getAccountInfo", token_account, {"encoding": "base64", "commitment": BALANCE_COMMITMENT})
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

    def commitment_rank_for(self, signature: str) -> int:
        """The rung this signature has reached on the commitment ladder.

        Not part of the five-method contract -- services/ and workers/ read the
        rank out of the deposit event dicts -- and provided because an operator
        asking "why has this not credited yet" needs to be able to ask about
        one signature without running a whole poll.
        """
        result = self.call("getSignatureStatuses", [signature], {"searchTransactionHistory": True})
        values = (result or {}).get("value") or [None]
        status = values[0]
        if not status:
            return commitment_rank(None)
        if status.get("err") is not None:
            # A failed transaction moved nothing, so it is never creditable no
            # matter how settled it is. Reporting its commitment level would be
            # reporting how firmly the chain agrees that nothing happened.
            return commitment_rank(None)
        return commitment_rank(status.get("confirmationStatus"))

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
