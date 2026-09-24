#!/usr/bin/env python3
"""
transactions.py
"""

import csv
import json
import logging
import os
import queue
import threading
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional, TypedDict

import pandas as pd
import requests
import tkinter as tk
import yfinance as yf
from dotenv import load_dotenv

logging.basicConfig(level=logging.DEBUG, format='%(asctime)s - %(levelname)s - %(message)s')

BASE_DIR = Path(__file__).resolve().parent
ENV_PATH = BASE_DIR / ".env"
TRANSACTIONS_JSON_PATH = BASE_DIR / "transactions.json"
CSV_EXPORT_PATH = BASE_DIR / "gridcoin_transactions.csv"

load_dotenv(dotenv_path=ENV_PATH if ENV_PATH.exists() else None)
RPC_USER = os.getenv("GRIDCOIN_RPC_USER", "gridcoinrpc")
RPC_PASSWORD = os.getenv("GRIDCOIN_RPC_PASSWORD", "changeme")
RPC_HOST = os.getenv("GRIDCOIN_RPC_HOST", "127.0.0.1")
RPC_PORT = int(os.getenv("GRIDCOIN_RPC_PORT", "25779"))

COINGECKO_DEMO_API_KEY = os.getenv("COINGECKO_DEMO_API_KEY", "").strip()
COINGECKO_PRO_API_KEY = os.getenv("COINGECKO_PRO_API_KEY", "").strip()
COINGECKO_GENERIC_API_KEY = os.getenv("COINGECKO_API_KEY", "").strip()
COINGECKO_COIN_ID = "gridcoin-research"
COINPAPRIKA_COIN_ID = "grc-gridcoin"
CRYPTOCOMPARE_API_KEY = os.getenv("CRYPTOCOMPARE_API_KEY", "").strip()
CRYPTOCOMPARE_BASE_URL = "https://min-api.cryptocompare.com/data/v2/histoday"

FORCE_REFRESH = False
MIN_LOCAL_TX_COUNT = 50
RPC_BATCH_SIZE = 100
RPC_TIMEOUT_SECONDS = 10
COINGECKO_TIMEOUT_SECONDS = 10
COINPAPRIKA_TIMEOUT_SECONDS = 10
CRYPTOCOMPARE_TIMEOUT_SECONDS = 10
YAHOO_TICKER = "GRC-USD"
MAX_YAHOO_STALENESS_DAYS = 3

class PriceRecord(TypedDict):
    price: float
    source: str
    source_date: str


price_cache: dict[str, Optional[PriceRecord]] = {}
grc_data_cache: Optional[pd.DataFrame] = None
coingecko_disabled = False
coingecko_disable_reason: Optional[str] = None
coinpaprika_latest_disabled = False
coinpaprika_latest_disable_reason: Optional[str] = None
cryptocompare_disabled = False
cryptocompare_disable_reason: Optional[str] = None


def coerce_float(value: Any, default: Optional[float] = 0.0) -> Optional[float]:
    if value is None:
        return default
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        cleaned = value.strip().replace(",", "").replace("$", "")
        if not cleaned:
            return default
        try:
            return float(cleaned)
        except ValueError:
            return default
    return default


def coerce_int(value: Any, default: int = 0) -> int:
    if value is None:
        return default
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    if isinstance(value, str):
        cleaned = value.strip()
        if not cleaned:
            return default
        try:
            return int(float(cleaned))
        except ValueError:
            return default
    return default


def coerce_float_value(value: Any, default: float = 0.0) -> float:
    parsed = coerce_float(value, default)
    return default if parsed is None else parsed


def clean_string(value: Any, default: str = "") -> str:
    if value is None:
        return default
    if isinstance(value, str):
        stripped = value.strip()
        return stripped if stripped else default
    return str(value)


def has_missing_text_value(value: Any) -> bool:
    if value is None:
        return True
    if isinstance(value, str):
        return value.strip().lower() in {"", "n/a", "na", "none", "null"}
    return False


def normalize_transaction(tx: Any) -> dict[str, Any]:
    normalized = dict(tx) if isinstance(tx, dict) else {}
    normalized["txid"] = clean_string(normalized.get("txid"), "unknown")
    normalized["category"] = clean_string(normalized.get("category"), "")
    normalized["amount"] = coerce_float(normalized.get("amount"), 0.0)
    normalized["time"] = coerce_int(normalized.get("time"), 0)

    amount_usd_numeric = normalized.get("Amount(USD)_numeric")
    grc_usd_numeric = normalized.get("GRC->USD_numeric")
    if amount_usd_numeric is not None:
        amount_usd_numeric = coerce_float(amount_usd_numeric, None)
    if grc_usd_numeric is not None:
        grc_usd_numeric = coerce_float(grc_usd_numeric, None)

    raw_amount_usd = normalized.get("Amount(USD)")
    raw_price_usd = normalized.get("GRC->USD")

    if amount_usd_numeric is None and not has_missing_text_value(raw_amount_usd):
        parsed = coerce_float(raw_amount_usd, None)
        if parsed is not None:
            amount_usd_numeric = parsed
    if grc_usd_numeric is None and not has_missing_text_value(raw_price_usd):
        parsed = coerce_float(raw_price_usd, None)
        if parsed is not None:
            grc_usd_numeric = parsed

    normalized["Amount(USD)_numeric"] = amount_usd_numeric
    normalized["GRC->USD_numeric"] = grc_usd_numeric
    normalized["Amount(USD)"] = f"${amount_usd_numeric:,.4f}" if amount_usd_numeric is not None else "N/A"
    normalized["GRC->USD"] = f"${grc_usd_numeric:,.6f}" if grc_usd_numeric is not None else "N/A"
    normalized["PriceSource"] = clean_string(normalized.get("PriceSource"), "")
    normalized["PriceSourceDate"] = clean_string(normalized.get("PriceSourceDate"), "")
    return normalized


def normalize_transactions(transactions: Any) -> list[dict[str, Any]]:
    if not isinstance(transactions, list):
        return []
    return [normalize_transaction(tx) for tx in transactions]


def load_full_grc_data() -> Optional[pd.DataFrame]:
    global grc_data_cache
    if grc_data_cache is None:
        try:
            logging.info("Downloading full historical data for %s", YAHOO_TICKER)
            df = yf.download(YAHOO_TICKER, period="max", interval="1d", progress=False, auto_adjust=False)
            if df is None or df.empty:
                logging.error("No data returned for %s", YAHOO_TICKER)
                return None
            df.index = pd.to_datetime(df.index).normalize()
            grc_data_cache = df
        except Exception as exc:
            logging.error("Error downloading %s data: %s", YAHOO_TICKER, exc)
            return None
    return grc_data_cache


def extract_close_series(df: Optional[pd.DataFrame | pd.Series]) -> pd.Series:
    if df is None:
        return pd.Series(dtype=float)

    if isinstance(df, pd.Series):
        series = pd.to_numeric(df, errors="coerce").dropna()
        series.index = pd.to_datetime(series.index).normalize()
        return series

    close_obj = None
    if isinstance(df, pd.DataFrame):
        if "Close" in df.columns:
            close_obj = df["Close"]
        elif isinstance(df.columns, pd.MultiIndex):
            close_candidates = [
                col for col in df.columns
                if any(str(level) == "Close" for level in (col if isinstance(col, tuple) else (col,)))
            ]
            if close_candidates:
                close_obj = df[close_candidates]

    if close_obj is None:
        return pd.Series(dtype=float)

    if isinstance(close_obj, pd.DataFrame):
        if close_obj.shape[1] == 0:
            return pd.Series(dtype=float)
        series = close_obj.iloc[:, 0]
    else:
        series = close_obj

    series = pd.to_numeric(series, errors="coerce").dropna()
    series.index = pd.to_datetime(series.index).normalize()
    return series


def needs_price_refresh(tx: dict[str, Any]) -> bool:
    if coerce_int(tx.get("time"), 0) <= 0:
        return False
    amount_numeric = tx.get("Amount(USD)_numeric")
    price_numeric = tx.get("GRC->USD_numeric")
    price_source = clean_string(tx.get("PriceSource"), "")
    source_date = clean_string(tx.get("PriceSourceDate"), "")
    if amount_numeric is None or price_numeric is None:
        return True
    if not price_source or not source_date:
        return True
    return False


def load_transactions_from_json(file_path: Path) -> list[dict[str, Any]]:
    logging.debug("Loading transactions from %s", file_path)
    try:
        with open(file_path, "r", encoding="utf-8") as file:
            transactions = json.load(file)
        transactions = normalize_transactions(transactions)
        logging.info("Loaded %s transactions from %s", len(transactions), file_path)
        return transactions
    except FileNotFoundError:
        logging.warning("Transactions file not found: %s", file_path)
        return []
    except Exception as exc:
        logging.error("Error loading transactions: %s", exc)
        return []


def save_transactions_to_json(file_path: Path, transactions: list[dict[str, Any]]) -> None:
    logging.debug("Saving %s transactions to %s", len(transactions), file_path)
    try:
        with open(file_path, "w", encoding="utf-8") as file:
            json.dump(transactions, file, indent=4)
        logging.info("Transactions saved to %s", file_path)
    except Exception as exc:
        logging.error("Error saving transactions: %s", exc)


def fetch_transactions_from_blockchain() -> tuple[list[dict[str, Any]], bool]:
    logging.debug("Fetching transactions from blockchain via RPC (paged)")
    rpc_url = f"http://{RPC_HOST}:{RPC_PORT}"
    transactions = []
    skip = 0
    partial = False

    while True:
        payload = {
            "jsonrpc": "1.0",
            "id": "gridcoin-tx-viewer",
            "method": "listtransactions",
            "params": ["*", RPC_BATCH_SIZE, skip],
        }
        logging.debug("Fetching transactions with skip=%s count=%s", skip, RPC_BATCH_SIZE)
        try:
            response = requests.post(
                rpc_url,
                json=payload,
                auth=(RPC_USER, RPC_PASSWORD),
                timeout=RPC_TIMEOUT_SECONDS,
            )
            response.raise_for_status()
            data = response.json()
            batch = data.get("result", [])
            if not batch:
                break
            transactions.extend(batch)
            logging.info("Fetched %s transactions (skip=%s)", len(batch), skip)
            skip += RPC_BATCH_SIZE
        except Exception as exc:
            partial = bool(transactions)
            logging.error("Error fetching transactions from blockchain: %s", exc)
            break

    normalized = normalize_transactions(transactions)
    logging.info("Total transactions fetched: %s", len(normalized))
    return normalized, partial


def get_coingecko_request_config(target_date: Optional[date] = None) -> Optional[dict[str, Any]]:
    global coingecko_disabled, coingecko_disable_reason

    if coingecko_disabled:
        return None

    if COINGECKO_PRO_API_KEY:
        return {
            "base_url": f"https://pro-api.coingecko.com/api/v3/coins/{COINGECKO_COIN_ID}/history",
            "headers": {"x-cg-pro-api-key": COINGECKO_PRO_API_KEY},
            "label": "CoinGecko Pro",
        }

    demo_key = COINGECKO_DEMO_API_KEY or COINGECKO_GENERIC_API_KEY
    if demo_key:
        if target_date is not None:
            oldest_demo_date = datetime.now(timezone.utc).date() - timedelta(days=365)
            if target_date < oldest_demo_date:
                logging.debug(
                    "Skipping CoinGecko Demo for %s because it is older than the public 365-day historical window",
                    target_date,
                )
                return None
        return {
            "base_url": f"https://api.coingecko.com/api/v3/coins/{COINGECKO_COIN_ID}/history",
            "headers": {"x-cg-demo-api-key": demo_key},
            "label": "CoinGecko Demo",
        }

    coingecko_disabled = True
    coingecko_disable_reason = "No CoinGecko API key configured"
    logging.info("Disabling CoinGecko for this run: %s", coingecko_disable_reason)
    return None


def fetch_grc_price_from_coingecko(date_str: str) -> Optional[PriceRecord]:
    global coingecko_disabled, coingecko_disable_reason

    target_date = datetime.strptime(date_str, "%d-%m-%Y").date()
    config = get_coingecko_request_config(target_date=target_date)
    if not config:
        return None

    logging.debug("Fetching GRC price from %s for %s", config["label"], date_str)
    params = {"date": date_str}

    try:
        response = requests.get(
            config["base_url"],
            params=params,
            headers=config["headers"],
            timeout=COINGECKO_TIMEOUT_SECONDS,
        )
        if response.status_code in {401, 403, 429}:
            coingecko_disabled = True
            coingecko_disable_reason = f"HTTP {response.status_code}"
            logging.error("Disabling CoinGecko for this run after %s on %s", coingecko_disable_reason, date_str)
            return None
        response.raise_for_status()
        data = response.json()
        usd_price = data.get("market_data", {}).get("current_price", {}).get("usd")
        parsed = coerce_float(usd_price, None) if usd_price is not None else None
        if parsed is None:
            logging.warning("%s returned no USD price for %s", config["label"], date_str)
            return None
        return {
            "price": parsed,
            "source": config["label"],
            "source_date": target_date.strftime("%Y-%m-%d"),
        }
    except Exception as exc:
        logging.error("Error fetching from CoinGecko: %s", exc)
        return None


def fetch_grc_price_from_cryptocompare(date_str: str) -> Optional[PriceRecord]:
    global cryptocompare_disabled, cryptocompare_disable_reason

    if cryptocompare_disabled:
        logging.debug("CryptoCompare historical data disabled for this run: %s", cryptocompare_disable_reason)
        return None

    target_date = datetime.strptime(date_str, "%d-%m-%Y").date()
    end_of_day_utc = datetime(target_date.year, target_date.month, target_date.day, 23, 59, 59, tzinfo=timezone.utc)
    params: dict[str, Any] = {
        "fsym": "GRC",
        "tsym": "USD",
        "limit": 1,
        "toTs": int(end_of_day_utc.timestamp()),
        "tryConversion": "true",
    }
    if CRYPTOCOMPARE_API_KEY:
        params["api_key"] = CRYPTOCOMPARE_API_KEY

    logging.debug("Fetching GRC price from CryptoCompare for %s", date_str)

    try:
        response = requests.get(
            CRYPTOCOMPARE_BASE_URL,
            params=params,
            timeout=CRYPTOCOMPARE_TIMEOUT_SECONDS,
        )
        if response.status_code in {401, 403, 429}:
            cryptocompare_disabled = True
            cryptocompare_disable_reason = f"HTTP {response.status_code}"
            logging.error(
                "Disabling CryptoCompare historical data for this run after %s on %s",
                cryptocompare_disable_reason,
                date_str,
            )
            return None

        response.raise_for_status()
        payload = response.json()
        if payload.get("Response") == "Error":
            message = clean_string(payload.get("Message"), "Unknown CryptoCompare error")
            logging.warning("CryptoCompare returned an error for %s: %s", date_str, message)
            return None

        rows = payload.get("Data", {}).get("Data", [])
        if not isinstance(rows, list) or not rows:
            logging.warning("CryptoCompare returned no rows for %s", date_str)
            return None

        best_row = None
        for row in reversed(rows):
            row_time = coerce_int(row.get("time"), 0)
            if row_time <= 0:
                continue
            row_date = datetime.fromtimestamp(row_time, tz=timezone.utc).date()
            if row_date <= target_date:
                best_row = row
                break

        if not isinstance(best_row, dict):
            logging.warning("CryptoCompare returned no usable row for %s", date_str)
            return None

        close_price = coerce_float(best_row.get("close"), None)
        open_price = coerce_float(best_row.get("open"), None)
        parsed = close_price if close_price is not None and close_price > 0 else open_price
        if parsed is None or parsed <= 0:
            logging.warning("CryptoCompare returned no usable USD close/open for %s", date_str)
            return None

        source_timestamp = coerce_int(best_row.get("time"), 0)
        source_date = datetime.fromtimestamp(source_timestamp, tz=timezone.utc).date().strftime("%Y-%m-%d")
        return {
            "price": parsed,
            "source": "CryptoCompare histoday",
            "source_date": source_date,
        }
    except Exception as exc:
        logging.error("Error fetching from CryptoCompare: %s", exc)
        return None


def fetch_grc_price_from_yahoo(date_str: str) -> Optional[PriceRecord]:
    logging.debug("Fetching %s price from Yahoo for %s using full dataset", YAHOO_TICKER, date_str)
    try:
        target_date = pd.Timestamp(datetime.strptime(date_str, "%d-%m-%Y").date())
        df = load_full_grc_data()
        if df is None or df.empty:
            logging.error("Full %s dataset is not available", YAHOO_TICKER)
            return None

        close_series = extract_close_series(df)
        if close_series.empty:
            logging.error("No usable Close series is available for %s", YAHOO_TICKER)
            return None

        if target_date in close_series.index:
            raw_price = close_series.loc[target_date]
            parsed = coerce_float(raw_price, None)
            if parsed is None:
                return None
            return {
                "price": parsed,
                "source": "Yahoo",
                "source_date": target_date.strftime("%Y-%m-%d"),
            }

        prior_series = close_series[close_series.index <= target_date]
        if prior_series.empty:
            logging.warning("No Yahoo price exists on or before %s", target_date.date())
            return None

        closest_timestamp = as_timestamp(prior_series.index[-1])
        gap_days = (target_date - closest_timestamp).days
        if gap_days > MAX_YAHOO_STALENESS_DAYS:
            logging.warning(
                "Rejecting stale Yahoo fallback for %s: closest date %s is %s days old",
                target_date.date(),
                closest_timestamp.date(),
                gap_days,
            )
            return None

        parsed = coerce_float(prior_series.iloc[-1], None)
        if parsed is None:
            return None

        logging.info(
            "Using Yahoo fallback for %s from closest prior date %s (%s day gap)",
            target_date.date(),
            closest_timestamp.date(),
            gap_days,
        )
        return {
            "price": parsed,
            "source": f"Yahoo fallback ({gap_days}d)",
            "source_date": closest_timestamp.strftime("%Y-%m-%d"),
        }
    except Exception as exc:
        logging.error("Error in fetch_grc_price_from_yahoo: %s", exc)
        return None


def fetch_latest_grc_price_from_coinpaprika() -> Optional[PriceRecord]:
    global coinpaprika_latest_disabled, coinpaprika_latest_disable_reason

    if coinpaprika_latest_disabled:
        logging.debug("CoinPaprika latest ticker disabled for this run: %s", coinpaprika_latest_disable_reason)
        return None

    url = f"https://api.coinpaprika.com/v1/tickers/{COINPAPRIKA_COIN_ID}"
    logging.debug("Fetching latest GRC price from CoinPaprika")
    try:
        response = requests.get(url, timeout=COINPAPRIKA_TIMEOUT_SECONDS)
        if response.status_code in {401, 403, 429}:
            coinpaprika_latest_disabled = True
            coinpaprika_latest_disable_reason = f"HTTP {response.status_code}"
            logging.error(
                "Disabling CoinPaprika latest ticker for this run after %s",
                coinpaprika_latest_disable_reason,
            )
            return None
        response.raise_for_status()
        data = response.json()
        usd_price = data.get("quotes", {}).get("USD", {}).get("price")
        parsed = coerce_float(usd_price, None)
        if parsed is None:
            logging.warning("CoinPaprika returned no current USD price")
            return None
        last_updated = clean_string(data.get("last_updated"), "")
        source_date = last_updated[:10] if len(last_updated) >= 10 else ""
        return {
            "price": parsed,
            "source": "CoinPaprika ticker",
            "source_date": source_date,
        }
    except Exception as exc:
        logging.error("Error fetching latest price from CoinPaprika: %s", exc)
        return None


def get_grc_price_record_on_date(date_str: str) -> Optional[PriceRecord]:
    cache_key = f"GRC_{date_str}"
    if cache_key in price_cache:
        return price_cache[cache_key]

    record = fetch_grc_price_from_coingecko(date_str)
    if record is None:
        logging.warning("CoinGecko unavailable for %s, falling back to CryptoCompare", date_str)
        record = fetch_grc_price_from_cryptocompare(date_str)
    if record is None:
        logging.warning("CryptoCompare unavailable for %s, falling back to Yahoo", date_str)
        record = fetch_grc_price_from_yahoo(date_str)

    price_cache[cache_key] = record
    return record


def get_latest_grc_price_record() -> Optional[PriceRecord]:
    record = fetch_latest_grc_price_from_coinpaprika()
    if record is not None:
        return record

    try:
        df = load_full_grc_data()
        if df is None or df.empty:
            return None

        close_series = extract_close_series(df)
        if close_series.empty:
            return None

        latest_timestamp = as_timestamp(close_series.index[-1])
        latest_price = coerce_float(close_series.iloc[-1], None)
        if latest_price is None:
            return None

        return {
            "price": latest_price,
            "source": "Yahoo latest close",
            "source_date": latest_timestamp.strftime("%Y-%m-%d"),
        }
    except Exception as exc:
        logging.error("Error getting latest %s price: %s", YAHOO_TICKER, exc)
        return None


def calculate_transaction_totals(transactions: Any) -> dict[str, Any]:
    normalized = normalize_transactions(transactions)
    total_tx_count = len(normalized)
    net_grc = sum(coerce_float_value(tx.get("amount"), 0.0) for tx in normalized)

    priced_transactions = [tx for tx in normalized if tx.get("Amount(USD)_numeric") is not None]
    priced_tx_count = len(priced_transactions)
    unpriced_tx_count = total_tx_count - priced_tx_count
    priced_net_grc = sum(coerce_float_value(tx.get("amount"), 0.0) for tx in priced_transactions)
    historical_usd_total = sum(coerce_float_value(tx.get("Amount(USD)_numeric"), 0.0) for tx in priced_transactions)

    latest_price_record = get_latest_grc_price_record()
    current_value_usd_total = None
    priced_subset_current_value_usd = None
    priced_subset_gain_loss_usd = None
    if latest_price_record and latest_price_record.get("price") is not None:
        latest_price = latest_price_record["price"]
        current_value_usd_total = net_grc * latest_price
        priced_subset_current_value_usd = priced_net_grc * latest_price
        priced_subset_gain_loss_usd = priced_subset_current_value_usd - historical_usd_total

    return {
        "total_tx_count": total_tx_count,
        "net_grc": net_grc,
        "priced_net_grc": priced_net_grc,
        "historical_usd_total": historical_usd_total,
        "priced_tx_count": priced_tx_count,
        "unpriced_tx_count": unpriced_tx_count,
        "latest_price_record": latest_price_record,
        "current_value_usd_total": current_value_usd_total,
        "priced_subset_current_value_usd": priced_subset_current_value_usd,
        "priced_subset_gain_loss_usd": priced_subset_gain_loss_usd,
    }


def update_transactions_with_prices(transactions: Any) -> list[dict[str, Any]]:
    transactions = normalize_transactions(transactions)

    dates_needed = sorted(
        {
            datetime.utcfromtimestamp(tx["time"]).strftime("%d-%m-%Y")
            for tx in transactions
            if needs_price_refresh(tx)
        },
        key=lambda value: datetime.strptime(value, "%d-%m-%Y"),
    )

    logging.debug("%s unique dates need price updates", len(dates_needed))
    for date_str in dates_needed:
        get_grc_price_record_on_date(date_str)

    successful_updates = 0
    skipped_updates = 0
    updated = []

    for tx in transactions:
        if tx["time"] <= 0:
            updated.append(tx)
            continue

        if needs_price_refresh(tx):
            date_str = datetime.utcfromtimestamp(tx["time"]).strftime("%d-%m-%Y")
            price_record = get_grc_price_record_on_date(date_str)
            if price_record and price_record.get("price") and price_record["price"] > 0:
                grc_usd_price = price_record["price"]
                amount_usd = tx["amount"] * grc_usd_price
                tx["GRC->USD_numeric"] = grc_usd_price
                tx["Amount(USD)_numeric"] = amount_usd
                tx["GRC->USD"] = f"${grc_usd_price:,.6f}"
                tx["Amount(USD)"] = f"${amount_usd:,.4f}"
                tx["PriceSource"] = price_record.get("source", "")
                tx["PriceSourceDate"] = price_record.get("source_date", "")
                successful_updates += 1
            else:
                tx["GRC->USD_numeric"] = None
                tx["Amount(USD)_numeric"] = None
                tx["GRC->USD"] = "N/A"
                tx["Amount(USD)"] = "N/A"
                tx["PriceSource"] = ""
                tx["PriceSourceDate"] = ""
                skipped_updates += 1
        else:
            amount_usd = tx["Amount(USD)_numeric"]
            price_numeric = tx.get("GRC->USD_numeric")
            tx["Amount(USD)"] = f"${amount_usd:,.4f}"
            tx["GRC->USD"] = f"${price_numeric:,.6f}" if price_numeric is not None else "N/A"
        updated.append(tx)

    logging.info(
        "Price update summary: %s updated, %s left unpriced, %s total",
        successful_updates,
        skipped_updates,
        len(updated),
    )
    return updated


def as_timestamp(value: Any) -> pd.Timestamp:
    return pd.Timestamp(value)


class TxViewerApp:
    def __init__(self, master):
        self.master = master
        self.master.title("Gridcoin Transactions Viewer")

        self.status_var = tk.StringVar(value="Loading transactions...")
        self.status_label = tk.Label(master, textvariable=self.status_var, anchor="w")
        self.status_label.pack(fill=tk.X, padx=8, pady=(8, 0))

        self.summary_var = tk.StringVar(value="Totals: calculating...")
        self.summary_label = tk.Label(master, textvariable=self.summary_var, anchor="w", justify=tk.LEFT, wraplength=1100)
        self.summary_label.pack(fill=tk.X, padx=8, pady=(4, 0))

        self.listbox = tk.Listbox(master, width=170, height=25)
        self.listbox.pack(fill=tk.BOTH, expand=True, padx=8, pady=8)

        self.export_button = tk.Button(master, text="Export to CSV", state=tk.DISABLED, command=self.export_to_csv)
        self.export_button.pack(pady=(0, 10))

        self.transactions = []
        self.totals = {}
        self.worker_queue = queue.Queue()
        self.listbox.insert(tk.END, "Loading transaction history in background...")
        self.master.after(100, self.process_worker_queue)
        threading.Thread(target=self.load_data_worker, daemon=True).start()

    def load_data_worker(self):
        try:
            transactions = load_transactions_from_json(TRANSACTIONS_JSON_PATH)
            status_bits = []

            if FORCE_REFRESH or not transactions or len(transactions) < MIN_LOCAL_TX_COUNT:
                logging.info("Fetching full transaction history from blockchain via RPC")
                rpc_transactions, partial = fetch_transactions_from_blockchain()
                if rpc_transactions:
                    transactions = rpc_transactions
                    save_transactions_to_json(TRANSACTIONS_JSON_PATH, transactions)
                    status_bits.append("loaded from RPC")
                    if partial:
                        status_bits.append("RPC partial")
                else:
                    status_bits.append("RPC unavailable; using local cache")
            else:
                status_bits.append("loaded from local JSON")

            transactions = update_transactions_with_prices(transactions)
            save_transactions_to_json(TRANSACTIONS_JSON_PATH, transactions)
            totals = calculate_transaction_totals(transactions)
            self.worker_queue.put({
                "kind": "success",
                "transactions": transactions,
                "totals": totals,
                "status": "; ".join(status_bits) if status_bits else "ready",
            })
        except Exception as exc:
            logging.exception("Fatal error while loading data")
            self.worker_queue.put({"kind": "error", "message": str(exc)})

    def process_worker_queue(self):
        try:
            while True:
                message = self.worker_queue.get_nowait()
                if message["kind"] == "success":
                    try:
                        self.transactions = normalize_transactions(message["transactions"])
                        self.totals = message.get("totals") or {}
                        self.populate_transactions()
                        self.update_summary_label()
                        self.export_button.config(state=tk.NORMAL if self.transactions else tk.DISABLED)
                        self.status_var.set(f"Ready: {len(self.transactions)} transactions ({message['status']})")
                    except Exception as exc:
                        logging.exception("Error while updating GUI from worker results")
                        self.listbox.delete(0, tk.END)
                        self.listbox.insert(tk.END, f"GUI update error: {exc}")
                        self.summary_var.set("Totals unavailable")
                        self.status_var.set("GUI update failed")
                elif message["kind"] == "error":
                    self.listbox.delete(0, tk.END)
                    self.listbox.insert(tk.END, f"Error loading transactions: {message['message']}")
                    self.summary_var.set("Totals unavailable")
                    self.status_var.set("Load failed")
        except queue.Empty:
            pass
        except Exception as exc:
            logging.exception("Unhandled error in process_worker_queue")
            self.listbox.delete(0, tk.END)
            self.listbox.insert(tk.END, f"Queue processing error: {exc}")
            self.summary_var.set("Totals unavailable")
            self.status_var.set("Queue processing failed")
        self.master.after(250, self.process_worker_queue)

    def update_summary_label(self):
        if not self.totals:
            self.summary_var.set("Totals unavailable")
            return

        net_grc = self.totals.get("net_grc", 0.0)
        priced_net_grc = self.totals.get("priced_net_grc", 0.0)
        historical_usd_total = self.totals.get("historical_usd_total", 0.0)
        priced_tx_count = self.totals.get("priced_tx_count", 0)
        unpriced_tx_count = self.totals.get("unpriced_tx_count", 0)
        latest_price_record = self.totals.get("latest_price_record") or {}
        current_value_usd_total = self.totals.get("current_value_usd_total")
        priced_subset_current_value_usd = self.totals.get("priced_subset_current_value_usd")
        priced_subset_gain_loss_usd = self.totals.get("priced_subset_gain_loss_usd")

        if current_value_usd_total is None:
            current_value_text = "N/A"
        else:
            source_date = latest_price_record.get("source_date", "unknown date")
            source_name = latest_price_record.get("source", "unknown source")
            current_value_text = f"${current_value_usd_total:,.4f} @ {source_name} {source_date}".strip()

        if priced_subset_current_value_usd is None:
            priced_subset_value_text = "N/A"
        else:
            priced_subset_value_text = f"${priced_subset_current_value_usd:,.4f}"

        if priced_subset_gain_loss_usd is None:
            gain_loss_text = "N/A"
        else:
            gain_loss_text = f"${priced_subset_gain_loss_usd:,.4f}"

        summary_lines = [
            f"Net GRC (all txs): {net_grc:,.4f}",
            f"Estimated current USD value (all net GRC): {current_value_text}",
            f"Priced vs unpriced txs: {priced_tx_count} priced / {unpriced_tx_count} unpriced",
            f"Priced-subset GRC: {priced_net_grc:,.4f}",
            f"Historical USD total on priced subset: ${historical_usd_total:,.4f}",
            f"Current USD value on priced subset: {priced_subset_value_text}",
            f"Gain/loss on priced subset only: {gain_loss_text}",
        ]
        self.summary_var.set("\n".join(summary_lines))

    def populate_transactions(self):
        self.listbox.delete(0, tk.END)
        if not self.transactions:
            self.listbox.insert(tk.END, "No transactions found.")
            return

        for tx in sorted(self.transactions, key=lambda item: item.get("time", 0), reverse=True):
            txid = tx.get("txid", "unknown")
            category = tx.get("category", "")
            amount_grc = coerce_float_value(tx.get("amount"), 0.0)
            tx_time = coerce_int(tx.get("time"), 0)
            time_string = datetime.utcfromtimestamp(tx_time).strftime("%m/%d/%y %H:%M:%S UTC") if tx_time > 0 else "N/A"
            usd_str = tx.get("Amount(USD)", "N/A")
            price_str = tx.get("GRC->USD", "N/A")
            source_str = tx.get("PriceSource", "")
            source_date = tx.get("PriceSourceDate", "")
            source_suffix = f" | Source={source_str} {source_date}".rstrip() if source_str or source_date else ""
            display_str = (
                f"Date/Time={time_string} | TXID={txid} | Category={category} | "
                f"GRC={amount_grc:.4f} | GRC->USD={price_str} | Amount(USD)={usd_str}{source_suffix}"
            )
            self.listbox.insert(tk.END, display_str)

    def export_to_csv(self):
        logging.debug("Exporting transactions to CSV: %s", CSV_EXPORT_PATH)
        try:
            with open(CSV_EXPORT_PATH, mode="w", newline="", encoding="utf-8") as file:
                writer = csv.writer(file)
                totals = self.totals or calculate_transaction_totals(self.transactions)
                latest_price_record = totals.get("latest_price_record") or {}
                writer.writerow(["Summary", "Value"])
                writer.writerow(["Net GRC (all txs)", f"{totals.get('net_grc', 0.0):.4f}"])
                writer.writerow([
                    "Estimated Current USD Value (all net GRC)",
                    "" if totals.get("current_value_usd_total") is None else f"{totals.get('current_value_usd_total', 0.0):.4f}",
                ])
                writer.writerow(["Latest Price Source", latest_price_record.get("source", "")])
                writer.writerow(["Latest Price Source Date", latest_price_record.get("source_date", "")])
                writer.writerow(["Priced Transaction Count", str(totals.get("priced_tx_count", 0))])
                writer.writerow(["Unpriced Transaction Count", str(totals.get("unpriced_tx_count", 0))])
                writer.writerow(["Priced-subset Net GRC", f"{totals.get('priced_net_grc', 0.0):.4f}"])
                writer.writerow([
                    "Historical USD Total on Priced Subset",
                    f"{totals.get('historical_usd_total', 0.0):.4f}",
                ])
                writer.writerow([
                    "Current USD Value on Priced Subset",
                    "" if totals.get("priced_subset_current_value_usd") is None else f"{totals.get('priced_subset_current_value_usd', 0.0):.4f}",
                ])
                writer.writerow([
                    "Gain/Loss on Priced Subset Only",
                    "" if totals.get("priced_subset_gain_loss_usd") is None else f"{totals.get('priced_subset_gain_loss_usd', 0.0):.4f}",
                ])
                writer.writerow([])
                writer.writerow([
                    "Date/Time",
                    "TXID",
                    "Category",
                    "GRC Amount",
                    "GRC->USD Price",
                    "Amount(USD)",
                    "Price Source",
                    "Price Source Date",
                ])
                for tx in sorted(self.transactions, key=lambda item: item.get("time", 0), reverse=True):
                    txid = tx.get("txid", "unknown")
                    category = tx.get("category", "")
                    amount_grc = coerce_float_value(tx.get("amount"), 0.0)
                    tx_time = coerce_int(tx.get("time"), 0)
                    time_string = datetime.utcfromtimestamp(tx_time).strftime("%m/%d/%y %H:%M:%S UTC") if tx_time > 0 else "N/A"
                    price_numeric = tx.get("GRC->USD_numeric")
                    amount_usd_numeric = tx.get("Amount(USD)_numeric")
                    writer.writerow([
                        time_string,
                        txid,
                        category,
                        f"{amount_grc:.4f}",
                        "" if price_numeric is None else f"{coerce_float_value(price_numeric, 0.0):.6f}",
                        "" if amount_usd_numeric is None else f"{coerce_float_value(amount_usd_numeric, 0.0):.4f}",
                        tx.get("PriceSource", ""),
                        tx.get("PriceSourceDate", ""),
                    ])
            self.status_var.set(f"Exported CSV to {CSV_EXPORT_PATH}")
            self.listbox.insert(tk.END, f"Transactions have been successfully exported to {CSV_EXPORT_PATH}.")
        except Exception as exc:
            logging.error("Error exporting CSV: %s", exc)
            self.status_var.set("CSV export failed")
            self.listbox.insert(tk.END, "Error exporting to CSV.")


def _report_callback_exception(exc, val, tb):
    logging.exception("Tk callback exception", exc_info=(exc, val, tb))

if __name__ == "__main__":
    root = tk.Tk()
    root.report_callback_exception = _report_callback_exception
    app = TxViewerApp(root)
    root.mainloop()
