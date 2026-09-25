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
    }
