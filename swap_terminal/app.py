from flask import Flask
from config import Config
from db import close_db, init_db
from chains.bitcoin import BitcoinAdapter
from chains.litecoin import LitecoinAdapter
from chains.gridcoin import GridcoinAdapter
from routes.health import bp as health_bp
from routes.quotes import bp as quotes_bp
from routes.rates import bp as rates_bp
from routes.swaps import bp as swaps_bp


def build_adapters(app: Flask) -> dict:
    rpc = app.config["RPC"]
    return {
        "BTC": BitcoinAdapter(**rpc["BTC"]),
        "LTC": LitecoinAdapter(**rpc["LTC"]),
        "GRC": GridcoinAdapter(**rpc["GRC"]),
    }


def create_app() -> Flask:
    app = Flask(__name__, static_folder="static", template_folder="templates")
    for key in dir(Config):
        if key.isupper():
            app.config[key] = getattr(Config, key)
    app.config["ADAPTERS"] = build_adapters(app)
    app.teardown_appcontext(close_db)
    app.register_blueprint(health_bp)
    app.register_blueprint(quotes_bp)
    app.register_blueprint(rates_bp)
    app.register_blueprint(swaps_bp)

    with app.app_context():
        init_db()

    return app


app = create_app()

if __name__ == "__main__":
    app.run(debug=True, host="0.0.0.0", port=5000)
