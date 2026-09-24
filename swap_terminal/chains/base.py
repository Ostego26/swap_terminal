import json
from decimal import Decimal
import requests

class RPCError(Exception):
    pass


class RPCAdapter:
    asset = ""

    def __init__(self, user: str, password: str, host: str, port: int, wallet: str = "", timeout: float = 30.0):
        self.user = user
        self.password = password
        self.host = host
        self.port = port
        self.wallet = wallet.strip("/")
        self.timeout = timeout

    @property
    def url(self) -> str:
        base = f"http://{self.host}:{self.port}"
        if self.wallet:
            return f"{base}/wallet/{self.wallet}"
        return base

    def call(self, method: str, *params):
        payload = {"jsonrpc": "2.0", "id": method, "method": method, "params": list(params)}
        response = requests.post(
            self.url,
            auth=(self.user, self.password),
            headers={"Content-Type": "application/json"},
            data=json.dumps(payload),
            timeout=self.timeout,
        )
        response.raise_for_status()
        data = response.json()
        if data.get("error"):
            raise RPCError(data["error"])
        return data.get("result")

    def get_new_address(self, label: str) -> str:
        return self.call("getnewaddress", label)

    def validate_address(self, address: str) -> bool:
        try:
            result = self.call("validateaddress", address)
            if isinstance(result, dict):
                return bool(result.get("isvalid"))
        except Exception:
            pass
        try:
            result = self.call("getaddressinfo", address)
            if isinstance(result, dict):
                return bool(result.get("isvalid", True))
        except Exception:
            return False
        return False

    def get_balance(self) -> float:
        return float(self.call("getbalance"))

    def send_to_address(self, address: str, amount: float) -> str:
        return self.call("sendtoaddress", address, float(amount))

    def get_transaction(self, txid: str) -> dict:
        try:
            return self.call("gettransaction", txid)
        except Exception:
            return self.call("getrawtransaction", txid, True)

    def get_confirmations(self, txid: str) -> int:
        tx = self.get_transaction(txid)
        return int(tx.get("confirmations", 0))

    def estimate_fee(self) -> float:
        try:
            result = self.call("estimatesmartfee", 2)
            return float(result.get("feerate", 0) or 0)
        except Exception:
            return 0.0

    def _raw_tx_for_vouts(self, txid: str) -> dict:
        return self.call("getrawtransaction", txid, True)

    def _extract_matching_vouts(self, txid: str, address: str, amount: float):
        try:
            raw = self._raw_tx_for_vouts(txid)
        except Exception:
            return [{"txid": txid, "vout": 0, "address": address, "amount": float(amount), "confirmations": self.get_confirmations(txid)}]
        matches = []
        confirmations = int(raw.get("confirmations", 0))
        for vout in raw.get("vout", []):
            script_pub_key = vout.get("scriptPubKey", {})
            addresses = script_pub_key.get("addresses") or []
            value = float(vout.get("value", 0))
            if address in addresses and abs(value - float(amount)) < 1e-8:
                matches.append({
                    "txid": txid,
                    "vout": int(vout.get("n", 0)),
                    "address": address,
                    "amount": value,
                    "confirmations": confirmations,
                })
        if matches:
            return matches
        return [{"txid": txid, "vout": 0, "address": address, "amount": float(amount), "confirmations": confirmations}]

    def find_deposits_to_address(self, address: str, tx_limit: int = 500):
        results = []
        try:
            txs = self.call("listtransactions", "*", tx_limit, 0, True)
        except Exception:
            txs = self.call("listtransactions", "*", tx_limit)
        for tx in txs or []:
            if tx.get("category") != "receive":
                continue
            if tx.get("address") != address:
                continue
            amount = abs(float(tx.get("amount", 0)))
            txid = tx.get("txid")
            if not txid or amount <= 0:
                continue
            results.extend(self._extract_matching_vouts(txid, address, amount))
        deduped = {}
        for item in results:
            deduped[(item["txid"], item["vout"])] = item
        return list(deduped.values())
