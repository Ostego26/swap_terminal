import time
from config import Config
from db import db_session, SCHEMA
from services.deposit_service import process_active_swaps
from services.payout_service import refresh_wallet_inventory
from workers.common import build_adapters, get_config_dict


def main(poll_seconds: int = 60):
    adapters = build_adapters()
    config = get_config_dict()
    while True:
        with db_session(Config.DB_PATH) as db:
            db.executescript(SCHEMA)
            refresh_wallet_inventory(db, adapters)
            process_active_swaps(db, config, adapters)
        time.sleep(poll_seconds)

if __name__ == "__main__":
    main()
