import time
from config import Config
from db import db_session, SCHEMA
from services.deposit_service import process_active_swaps
from workers.common import build_adapters, get_config_dict


def main(poll_seconds: int = 15):
    adapters = build_adapters()
    config = get_config_dict()
    while True:
        with db_session(Config.DB_PATH) as db:
            db.executescript(SCHEMA)
            process_active_swaps(db, config, adapters)
        time.sleep(poll_seconds)

if __name__ == "__main__":
    main()
