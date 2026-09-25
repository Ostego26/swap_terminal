"""Configuration for the Flask swap terminal, read from the environment once.

Role: submodule (configuration constants; holds no decision of its own)
Reads: the process environment at IMPORT time -- SWAP_DB_PATH, the per-chain
       RPC credentials and endpoints, fee and tolerance settings
Writes: nothing
Can move funds: no by itself, but it CARRIES the values that decide what
       moves: DEFAULT_FEE_BPS, the *_NETWORK_FEE_RESERVE figures, the
       *_MIN_CONFIRMATIONS thresholds and ALLOWED_PAIRS. Changing any of those
       changes what gets sent or when, which makes them the operator's (rule
       16), not something to adjust in passing.
Mainnet-safe: yes to import. Note the DEFAULTS POINT AT MAINNET: 8332 is
       Bitcoin's mainnet RPC port, 9332 Litecoin's, 15715 Gridcoin's. A
       checkout with no environment set is configured for mainnet daemons, not
       for testnet ones -- set BTC_RPC_PORT=18332, LTC_RPC_PORT=19332 and
       GRC_RPC_PORT=25779 to point it at test chains.

Everything here is evaluated when the module is imported, because `Config` is a
class body. That is why tests/conftest.py sets SWAP_DB_PATH before importing
anything: setting it afterwards is too late, the value is already baked in.
"""

import os
from pathlib import Path
from typing import ClassVar

BASE_DIR = Path(__file__).resolve().parent

class Config:
    SECRET_KEY = os.getenv("SECRET_KEY", "swap-terminal-dev")
    DB_PATH = os.getenv("SWAP_DB_PATH", str(BASE_DIR / "swap_terminal.db"))
    QUOTE_TTL_SECONDS = int(os.getenv("QUOTE_TTL_SECONDS", "600"))
    RATE_CACHE_SECONDS = int(os.getenv("RATE_CACHE_SECONDS", "30"))
    DEFAULT_FEE_BPS = int(os.getenv("DEFAULT_FEE_BPS", "150"))
    AMOUNT_TOLERANCE_PCT = float(os.getenv("AMOUNT_TOLERANCE_PCT", "0.01"))
    SMALL_SWAP_MANUAL_REVIEW_USD = float(os.getenv("SMALL_SWAP_MANUAL_REVIEW_USD", "5000"))
    BTC_MIN_CONFIRMATIONS = int(os.getenv("BTC_MIN_CONFIRMATIONS", "2"))
    LTC_MIN_CONFIRMATIONS = int(os.getenv("LTC_MIN_CONFIRMATIONS", "2"))
    GRC_MIN_CONFIRMATIONS = int(os.getenv("GRC_MIN_CONFIRMATIONS", "6"))
    # SOL_MIN_CONFIRMATIONS IS NOT A COUNT OF BLOCKS. Solana has commitment
    # LEVELS -- processed / confirmed / finalized -- and this is a rung on the
    # ladder in chains/solana_units.COMMITMENT_RANKS, where 3 = finalized.
    # The name matches the other three because services/deposit_service.py
    # reads `swap["min_confirmations"]` for every chain, and the alternative
    # was a per-chain branch in the one function that decides whether a deposit
    # is creditable. The vocabulary lives in one place instead (rule 11), and
    # SolanaAdapter.__init__ REFUSES a value that is not a rung -- an operator
    # who copies Gridcoin's 6 here would otherwise stall every SOL swap
    # forever, silently, because no deposit can reach rank 6.
    SOL_MIN_CONFIRMATIONS = int(os.getenv("SOL_MIN_CONFIRMATIONS", "3"))
    BTC_NETWORK_FEE_RESERVE = float(os.getenv("BTC_NETWORK_FEE_RESERVE", "0.00002"))
    LTC_NETWORK_FEE_RESERVE = float(os.getenv("LTC_NETWORK_FEE_RESERVE", "0.001"))
    GRC_NETWORK_FEE_RESERVE = float(os.getenv("GRC_NETWORK_FEE_RESERVE", "0.01"))
    # ClassVar annotations: these are shared configuration read by every
    # request, not per-instance defaults. Config is never instantiated --
    # app.py copies its uppercase attributes into app.config -- so the
    # mutable-default hazard RUF012 warns about does not arise, and saying
    # so in the type is better than suppressing the check.
    ALLOWED_PAIRS: ClassVar[set[tuple[str, str]]] = {
        ("GRC", "BTC"),
        ("BTC", "GRC"),
        ("GRC", "LTC"),
        ("LTC", "GRC"),
    }
    # Monero. No default port: monero-wallet-rpc binds wherever it was told to
    # with --rpc-bind-port, and there is no conventional value the way 8332 is
    # Bitcoin's. XMR_RPC_PORT unset means "no Monero wallet here", and
    # chains/registry.py leaves the adapter unbuilt rather than pointing one at
    # a guess.
    XMR_RPC_PORT = int(os.getenv("XMR_RPC_PORT", "0"))
    # Clamped to Monero's ten-block consensus spend lock by
    # chains/monero_units.effective_min_confirmations(). The default is that
    # floor rather than a number chosen to look like the others: anything lower
    # would release a swap the wallet then refuses to pay.
    XMR_MIN_CONFIRMATIONS = int(os.getenv("XMR_MIN_CONFIRMATIONS", "10"))
    XMR_NETWORK_FEE_RESERVE = float(os.getenv("XMR_NETWORK_FEE_RESERVE", "0.0005"))
    # FALSE BY DEFAULT, AND THIS IS THE ONE CHAIN THAT CAN AFFORD IT. Monero
    # splits the view key from the spend key, so the deposit watcher can run
    # against a wallet that is cryptographically unable to send. The other
    # three chains inherit send_to_address() unconditionally from
    # chains/base.py and have no equivalent. Setting this true is a deliberate
    # act that arms the payout path for XMR.
    XMR_WALLET_CAN_SPEND = os.getenv("XMR_WALLET_CAN_SPEND", "").strip().lower() in {"1", "true", "yes"}

    # XRP. No default URL: a rippled endpoint is either your own server or a
    # public cluster, and guessing one would point this at somebody else's
    # machine. Unset means no XRP adapter is constructed at all.
    XRP_RPC_URL = os.getenv("XRP_RPC_URL", "")
    # Must be 1. The XRP Ledger does not reorganize, so a payment is either in
    # a validated ledger or it is not -- there is no depth to accumulate, and
    # chains/xrp_units.py REFUSES any other value at construction rather than
    # letting every XRP deposit sit below an unreachable threshold forever.
    XRP_MIN_CONFIRMATIONS = int(os.getenv("XRP_MIN_CONFIRMATIONS", "1"))

    RPC: ClassVar[dict[str, dict[str, object]]] = {
        "BTC": {
            "user": os.getenv("BTC_RPC_USER", ""),
            "password": os.getenv("BTC_RPC_PASS", ""),
            "host": os.getenv("BTC_RPC_HOST", "127.0.0.1"),
            "port": int(os.getenv("BTC_RPC_PORT", "8332")),
            "wallet": os.getenv("BTC_RPC_WALLET", ""),
            "timeout": float(os.getenv("BTC_RPC_TIMEOUT", "30")),
        },
        "LTC": {
            "user": os.getenv("LTC_RPC_USER", ""),
            "password": os.getenv("LTC_RPC_PASS", ""),
            "host": os.getenv("LTC_RPC_HOST", "127.0.0.1"),
            "port": int(os.getenv("LTC_RPC_PORT", "9332")),
            "wallet": os.getenv("LTC_RPC_WALLET", ""),
            "timeout": float(os.getenv("LTC_RPC_TIMEOUT", "30")),
        },
        # SOLANA'S ENTRY HAS DIFFERENT KEYS, AND THAT IS THE POINT.
        # The three above are Bitcoin JSON-RPC connections: user, password,
        # host, port, wallet, timeout. A Solana RPC endpoint is ONE URL with no
        # HTTP authentication and no wallet path, so forcing it into that shape
        # would mean four empty strings and a reassembly step that can silently
        # produce "http://:0". Each dict is built for the __init__ signature it
        # is splatted into -- which is the contract RPCAdapter already had --
        # and chains/registry.py is the single place that does the splatting.
        #
        # THERE IS DELIBERATELY NO DEFAULT URL. The three above default to
        # MAINNET ports, which this file's header says out loud. Doing the same
        # here would mean picking somebody's public cluster and defaulting to
        # mainnet on it; an unset URL instead makes every call refuse with a
        # message saying so, which is the loud version of the same fact.
        #
        # SOL_HOT_WALLET is a PUBLIC key. Nothing in chains/solana.py reads,
        # loads or derives a private key, and there is no environment variable
        # here for a keypair path -- unlike the Node bridge's
        # SOLANA_PAYER_KEYPAIR_PATH, which is what signs over there.
        "SOL": {
            "url": os.getenv("SOL_RPC_URL", ""),
            "commitment": os.getenv("SOL_RPC_COMMITMENT", "processed"),
            "timeout": float(os.getenv("SOL_RPC_TIMEOUT", "30")),
            # The SPL mint to operate on, e.g. wGRC. Empty means native SOL.
            "mint": os.getenv("SOL_SPL_MINT", ""),
            "hot_wallet": os.getenv("SOL_HOT_WALLET", ""),
            "min_commitment_rank": int(os.getenv("SOL_MIN_CONFIRMATIONS", "3")),
        },
        "GRC": {
            "user": os.getenv("GRC_RPC_USER", ""),
            "password": os.getenv("GRC_RPC_PASS", ""),
            "host": os.getenv("GRC_RPC_HOST", "127.0.0.1"),
            "port": int(os.getenv("GRC_RPC_PORT", "15715")),
            "wallet": os.getenv("GRC_RPC_WALLET", ""),
            "timeout": float(os.getenv("GRC_RPC_TIMEOUT", "30")),
        },
        # A DIFFERENT SHAPE ON PURPOSE, matching MoneroAdapter.__init__ rather
        # than RPCAdapter's six. There is no `wallet` key because a
        # monero-wallet-rpc process serves exactly one wallet -- the name is
        # chosen when the daemon starts, not per request -- and there is an
        # `account_index` instead, because that is what a subaddress is derived
        # under. chains/registry.py splats this dict, so a key added here
        # without a matching parameter fails at construction.
        # Its own shape again, matching XRPAdapter.__init__. An XRP endpoint is
        # one URL with no HTTP auth, no wallet path and no port of its own --
        # the same reason Config.RPC["SOL"] does not use the Bitcoin six.
        "XRP": {
            "url": XRP_RPC_URL,
            "min_confirmations": XRP_MIN_CONFIRMATIONS,
            "timeout": float(os.getenv("XRP_RPC_TIMEOUT", "30")),
        },
        "XMR": {
            "host": os.getenv("XMR_RPC_HOST", "127.0.0.1"),
            "port": XMR_RPC_PORT,
            "user": os.getenv("XMR_RPC_USER", ""),
            "password": os.getenv("XMR_RPC_PASS", ""),
            "account_index": int(os.getenv("XMR_ACCOUNT_INDEX", "0")),
            "min_confirmations": XMR_MIN_CONFIRMATIONS,
            "can_spend": XMR_WALLET_CAN_SPEND,
            "timeout": float(os.getenv("XMR_RPC_TIMEOUT", "30")),
        },
    }
