import os
from pathlib import Path

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
    BTC_NETWORK_FEE_RESERVE = float(os.getenv("BTC_NETWORK_FEE_RESERVE", "0.00002"))
    LTC_NETWORK_FEE_RESERVE = float(os.getenv("LTC_NETWORK_FEE_RESERVE", "0.001"))
    GRC_NETWORK_FEE_RESERVE = float(os.getenv("GRC_NETWORK_FEE_RESERVE", "0.01"))
    ALLOWED_PAIRS = {
        ("GRC", "BTC"),
        ("BTC", "GRC"),
        ("GRC", "LTC"),
        ("LTC", "GRC"),
    }
    RPC = {
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
        "GRC": {
            "user": os.getenv("GRC_RPC_USER", ""),
            "password": os.getenv("GRC_RPC_PASS", ""),
            "host": os.getenv("GRC_RPC_HOST", "127.0.0.1"),
            "port": int(os.getenv("GRC_RPC_PORT", "15715")),
            "wallet": os.getenv("GRC_RPC_WALLET", ""),
            "timeout": float(os.getenv("GRC_RPC_TIMEOUT", "30")),
        },
    }
