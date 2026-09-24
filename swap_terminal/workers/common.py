from config import Config
from db import db_session
from chains.bitcoin import BitcoinAdapter
from chains.litecoin import LitecoinAdapter
from chains.gridcoin import GridcoinAdapter


def build_adapters() -> dict:
    rpc = Config.RPC
    return {
        "BTC": BitcoinAdapter(**rpc["BTC"]),
        "LTC": LitecoinAdapter(**rpc["LTC"]),
        "GRC": GridcoinAdapter(**rpc["GRC"]),
    }


def get_config_dict() -> dict:
    return {k: getattr(Config, k) for k in dir(Config) if k.isupper()}
